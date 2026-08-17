"""
catalog_capture.py
==================
Capture extracted document rows into the file_catalog.cat_* mirror tables.

Loaders hand capture() a target cat_* table and a list of column-keyed row
dicts. capture() inserts only the keys that are REAL columns of that table
(case-insensitive), and stamps UWI / INVENTORY_ID / SOURCE_PATH / SOURCE when
those columns exist. Unknown keys are ignored, so a loader does not have to
match the schema exactly — a key that isn't a column is simply dropped (and
reported once). The CAT_ROW_ID / PROMOTED / CAPTURED_AT provenance columns are
left to their defaults. Promotion into dv_* happens later via promote_catalog.

Scan-stage only: this never touches dataview.dv_* or dv_well.
"""
from __future__ import annotations

import contextlib
from sqlalchemy import text, event

CAT_SCHEMA = "file_catalog"

_colcache: dict[str, dict] = {}
_widthcache: dict[str, dict] = {}
_warned: set = set()
_fem_engines: set = set()

# (cat_table, inventory_id) pairs whose existing rows have already been cleared
# in the current run, so the delete-then-insert happens once per file per table
# (later captures to the same table append rather than wipe). Reset per file via
# reset_replace_state() so a re-promote replaces instead of duplicating.
_replace_cleared: set = set()

# ── INTERNAL TIMING ─────────────────────────────────────────────────────
#
# capture() measured 152.2s across 1,410 calls — 108ms each — and three
# rounds of reasoning failed to explain it. The schema lookup was cached
# and became 1% of the stage. The connection was reused and NOTHING moved,
# which ruled out acquisition. The delete has no index, but the tables hold
# only a few hundred rows, so a scan of them costs microseconds.
#
# What is left inside a call is: one DELETE, one executemany per column
# shape, and one COMMIT. Reusing the connection did not reduce the number
# of COMMITS — a nested transaction per table still flushes the log per
# table — so that is the standing suspect. But it is a suspect, and the
# last three were wrong, so it gets measured rather than assumed.
_TIMES: dict = {}
_COUNTS: dict = {}


def _tick(step, t0):
    import time as _t
    _TIMES[step] = _TIMES.get(step, 0.0) + (_t.perf_counter() - t0)
    _COUNTS[step] = _COUNTS.get(step, 0) + 1


def reset_capture_timings() -> None:
    _TIMES.clear()
    _COUNTS.clear()


def capture_timings() -> str:
    """One line, slowest first. Empty when nothing was timed."""
    if not _TIMES:
        return ""
    tot = sum(_TIMES.values())
    parts = [f"{k} {v:.1f}s ({100.0 * v / tot:.0f}%, {_COUNTS.get(k, 0):,}x)"
             for k, v in sorted(_TIMES.items(), key=lambda kv: -kv[1])]
    return f"{tot:.1f}s inside capture · " + " · ".join(parts)


def reset_replace_state() -> None:
    """Forget which (table, inventory_id) pairs have been cleared this run.

    Call once at the start of processing a file (or batch) before re-capturing.
    The next capture() for each pair then deletes the file's existing rows
    before inserting, keeping re-promotes idempotent.
    """
    _replace_cleared.clear()


def _ensure_fast_executemany(engine) -> None:
    """Force pyodbc fast_executemany on this engine's cursors (once per engine).

    Makes ``con.execute(stmt, [list-of-dicts])`` batch into a handful of
    round-trips instead of one per row, even if the engine wasn't created with
    fast_executemany=True. Harmless no-op off mssql/pyodbc (the event just never
    finds the attribute to set). Registered once per engine id.
    """
    key = id(engine)
    if key in _fem_engines:
        return
    _fem_engines.add(key)
    try:
        @event.listens_for(engine, "before_cursor_execute")
        def _fem(conn, cursor, statement, params, context, executemany):
            if executemany:
                try:
                    cursor.fast_executemany = True
                except Exception:
                    pass
    except Exception:
        pass


def _columns(con, cat_table: str) -> dict:
    """Return {UPPER_NAME: actual_name} for file_catalog.<cat_table> (cached)."""
    key = cat_table.lower()
    if key not in _colcache:
        rows = con.execute(text(
            "SELECT c.name FROM sys.columns c "
            "WHERE c.object_id = OBJECT_ID(:full)"),
            {"full": f"{CAT_SCHEMA}.{cat_table}"}).fetchall()
        _colcache[key] = {r[0].upper(): r[0] for r in rows}
    return _colcache[key]


def _col_widths(con, cat_table: str) -> dict:
    """Return {actual_name: max_chars} for the string columns of
    file_catalog.<cat_table> (cached). Only finite character widths are
    included — non-string columns (CHARACTER_MAXIMUM_LENGTH IS NULL) and
    unbounded MAX columns (-1) are omitted, so values into them are never
    clamped."""
    key = cat_table.lower()
    if key not in _widthcache:
        rows = con.execute(text(
            "SELECT c.name, CASE "
            "WHEN c.max_length = -1 THEN -1 "
            "WHEN t.name IN ('nchar','nvarchar') THEN c.max_length/2 "
            "WHEN t.name IN ('char','varchar','text','ntext') THEN c.max_length "
            "ELSE NULL END "
            "FROM sys.columns c JOIN sys.types t ON t.user_type_id = c.user_type_id "
            "WHERE c.object_id = OBJECT_ID(:full)"),
            {"full": f"{CAT_SCHEMA}.{cat_table}"}).fetchall()
        _widthcache[key] = {r[0]: int(r[1]) for r in rows
                            if r[1] is not None and int(r[1]) > 0}
    return _widthcache[key]


def capture(engine, cat_table: str, rows, *, uwi=None, inventory_id=None,
            source_path=None, source="DOC", replace=True, log=None,
            conn=None) -> int:
    """Insert column-keyed row dicts into file_catalog.<cat_table>.

    Returns the number of rows inserted. Provenance (UWI/INVENTORY_ID/
    SOURCE_PATH/SOURCE) is applied only where the column exists.

    When ``replace`` is true and an INVENTORY_ID is supplied, the file's
    existing rows in this table are deleted before the first insert of the run
    (tracked by reset_replace_state()), so re-promoting a file replaces its
    rows rather than duplicating them. The delete is always scoped to the
    file's INVENTORY_ID — never a blanket wipe.
    """
    say = log or (lambda *_: None)
    rows = [r for r in (rows or []) if r]
    if not rows:
        return 0
    _ensure_fast_executemany(engine)

    n = 0
    import time as _t
    _c0 = _t.perf_counter()
    _cm = contextlib.nullcontext(conn) if conn is not None else engine.begin()
    _own_txn = conn is None
    with _cm as con:
        cols = _columns(con, cat_table)
        if not cols:
            raise RuntimeError(f"{CAT_SCHEMA}.{cat_table} not found "
                               f"(run build_catalog_mirror.py --apply)")
        widths = _col_widths(con, cat_table)

        def _clamp(payload: dict) -> dict:
            """Trim any string longer than its destination column so a single
            over-long value (e.g. a long well name or file path) can't fail the
            whole batch with 'String data, right truncation'. Warned once per
            table.column so silent data loss stays visible."""
            for _c, _v in list(payload.items()):
                _w = widths.get(_c)
                if _w is not None and isinstance(_v, str) and len(_v) > _w:
                    tag = f"{cat_table}.{_c}>{_w}"
                    if tag not in _warned:
                        _warned.add(tag)
                        say(f"[CAPTURE] {cat_table}.{_c}: value clamped to "
                            f"{_w} chars (was {len(_v)})")
                    payload[_c] = _v[:_w]
            return payload

        stamp = {}
        if uwi is not None and "UWI" in cols:
            stamp[cols["UWI"]] = uwi
        if "INVENTORY_ID" in cols:
            stamp[cols["INVENTORY_ID"]] = inventory_id
        if "SOURCE_PATH" in cols:
            stamp[cols["SOURCE_PATH"]] = source_path
        if source is not None and "SOURCE" in cols:
            stamp[cols["SOURCE"]] = source

        # Idempotent re-promote: clear this file's existing rows the first time
        # we capture into this table (this run), then append. Scoped strictly to
        # INVENTORY_ID; skipped when no inventory_id is available.
        if (replace and inventory_id is not None and "INVENTORY_ID" in cols
                and (cat_table, inventory_id) not in _replace_cleared):
            import time as _t
            _d0 = _t.perf_counter()
            con.execute(text(
                f"DELETE FROM {CAT_SCHEMA}.{cat_table} "
                f"WHERE [{cols['INVENTORY_ID']}] = :inv"), {"inv": inventory_id})
            _tick("delete", _d0)
            _replace_cleared.add((cat_table, inventory_id))

        # Build payloads (provenance stamp + only real columns), then group by
        # column signature so each distinct row-shape becomes ONE batched
        # executemany instead of one round-trip per row. With fast_executemany
        # this collapses thousands of survey-station / scout / shapefile rows
        # into a handful of calls — the heavy-capture supercharge. Rows that map
        # to identical columns (the common case) form a single group.
        from collections import OrderedDict
        import time as _t
        _b0 = _t.perf_counter()
        groups: "OrderedDict[tuple, list]" = OrderedDict()
        for r in rows:
            payload = dict(stamp)
            for k, v in r.items():
                uc = str(k).upper()
                if uc in cols:
                    payload[cols[uc]] = v
                else:
                    tag = f"{cat_table}.{k}"
                    if tag not in _warned:
                        _warned.add(tag)
                        say(f"[CAPTURE] {cat_table}: ignoring non-column '{k}'")
            if not payload:
                continue
            payload = _clamp(payload)
            sig = tuple(payload.keys())
            groups.setdefault(sig, []).append(payload)
        _tick("build_payloads", _b0)

        for sig, payloads in groups.items():
            collist = ", ".join(f"[{c}]" for c in sig)
            vallist = ", ".join(f":p{i}" for i in range(len(sig)))
            stmt = text(f"INSERT INTO {CAT_SCHEMA}.{cat_table} "
                        f"({collist}) VALUES ({vallist})")
            binds = [{f"p{i}": p[c] for i, c in enumerate(sig)}
                     for p in payloads]
            try:
                import time as _t
                _i0 = _t.perf_counter()
                con.execute(stmt, binds)        # executemany — one batch / shape
                _tick("insert", _i0)
                n += len(payloads)
            except Exception as _e:             # noqa: BLE001
                if "truncation" not in str(_e).lower():
                    raise
                # A value overflowed its column. The batch error is opaque
                # ("length N buffer M" names no column), so isolate the bad
                # row(s): insert good rows one-by-one and, for each failure,
                # report the exact column / value / true width. Good rows still
                # land; only the genuinely-overlong cell is skipped (loudly).
                n += _insert_isolating(con, cat_table, sig, payloads, say)
    # Everything the call cost MINUS the parts named above. When capture
    # owns the transaction this includes the COMMIT and its log flush;
    # when the caller passes conn= it does not, which is itself the
    # comparison worth having.
    _tick("own_txn_overhead" if _own_txn else "caller_txn_overhead", _c0)
    return n


def _insert_isolating(con, cat_table, sig, payloads, say) -> int:
    """Row-by-row insert after a batch truncation, naming the offender.

    Returns the number of rows actually inserted. For any row that still
    truncates, every string field is probed against its true column width
    (queried live, so columns _col_widths excluded — e.g. surprising narrow
    ones — are also reported) and the row is skipped with a loud log."""
    collist = ", ".join(f"[{c}]" for c in sig)
    vallist = ", ".join(f":p{i}" for i in range(len(sig)))
    stmt = text(f"INSERT INTO {CAT_SCHEMA}.{cat_table} "
                f"({collist}) VALUES ({vallist})")
    ins = 0
    for p in payloads:
        bind = {f"p{i}": p[c] for i, c in enumerate(sig)}
        try:
            con.execute(stmt, bind)
            ins += 1
        except Exception as _e:                 # noqa: BLE001
            if "truncation" not in str(_e).lower():
                raise
            offenders = []
            for c in sig:
                v = p.get(c)
                if not isinstance(v, str):
                    continue
                w, dt = _true_col(con, cat_table, c)
                if w is not None and len(v) > w:
                    offenders.append(f"{c} ({dt}({w})) <- {len(v)} chars: "
                                     f"{v[:60]!r}")
            if offenders:
                say(f"[CAPTURE] {cat_table}: row skipped, value(s) exceed "
                    f"column width -> " + " | ".join(offenders))
            else:
                say(f"[CAPTURE] {cat_table}: row skipped on truncation but no "
                    f"string field exceeds its width — check non-string/coded "
                    f"columns. error: {_e}")
    return ins


def _true_col(con, cat_table, col):
    """(CHARACTER_MAXIMUM_LENGTH, DATA_TYPE) for one column, live (uncached)."""
    r = con.execute(text(
        "SELECT CASE WHEN c.max_length = -1 THEN -1 "
        "WHEN t.name IN ('nchar','nvarchar') THEN c.max_length/2 "
        "ELSE c.max_length END, t.name "
        "FROM sys.columns c JOIN sys.types t ON t.user_type_id = c.user_type_id "
        "WHERE c.object_id = OBJECT_ID(:full) AND c.name = :c"),
        {"full": f"{CAT_SCHEMA}.{cat_table}", "c": col}).fetchone()
    if not r:
        return (None, None)
    w = int(r[0]) if r[0] is not None and int(r[0]) > 0 else None
    return (w, r[1])
