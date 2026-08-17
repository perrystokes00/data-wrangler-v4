"""
dataview/file_catalog/shape_loader.py
====================================
Bridge between docshape (recognition) and DataView (capture, promote).

docshape reads a document and says what its tables ARE. This decides where
those rows land in THIS database — minting the parent keys the schema demands,
stamping the reference codes promote validates, and handing the result to
catalog_capture.capture(), the same function every other loader ends with. So
provenance, idempotent re-capture and the insert itself are unchanged, and
nothing downstream can tell where the rows came from except by the source
column.

WHAT BELONGS HERE AND WHAT DOESN'T
----------------------------------
docshape knows tables and vocabularies; it must not learn about
cat_well_dir_srvy_hdr, dv_r_uom or INVENTORY_ID. Everything DataView-specific
lives in this file — which is why the port was a change of imports rather than
a rewrite.

DRY RUN BY DEFAULT. Nothing is written unless apply=True.

USAGE (from the repo root)
--------------------------
    py -m dataview.file_catalog.shape_loader --dir C:\\docs
    py -m dataview.file_catalog.shape_loader --dir C:\\docs --apply
    py -m dataview.file_catalog.shape_loader --file <doc> --pack petroleum
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
from collections import defaultdict
from datetime import datetime as _dt

SOURCE = "SHAPE"     # provenance marker, parallel to promote_catalog's CATALOG

# ── reference codes promote validates against ────────────────────────────── #
# These columns are FK-constrained, and a NULL reads as "unresolved" — which
# held 19 tops, 360 stations, 564 production rows and 70 zones on the first
# real run, with a message that looked like the source code was unregistered
# when it wasn't. Codes verified present in dataview.dv_r_uom.
DEPTH_UOM = "FT"
FLUID_UOM = {
    "OIL":   {"volume_ouom": "BBL", "rate_ouom": "BOPD"},
    "GAS":   {"volume_ouom": "MCF", "rate_ouom": "MCFD"},
    "WATER": {"volume_ouom": "BBL", "rate_ouom": "BWPD"},
}

_ID_MAX = 40          # every minted key column is nvarchar(40)


# --------------------------------------------------------------------------- #
# Recognition, via docshape
# --------------------------------------------------------------------------- #
def _pack_and_recogniser(pack_name="petroleum"):
    """The vocabulary plus its overlay — the same one the bench page tunes."""
    from docshape import Recogniser
    from docshape.packs.overlay import load_with_overlay
    pack, _ov, _path = load_with_overlay(pack_name)
    return pack, Recogniser(pack)


def recognise(rec, path):
    """[(table_name, result)] for one document."""
    from docshape.readers import read_tables
    out = []
    for name, rows in (read_tables(path) or {}).items():
        if not rows:
            continue
        header = list(rows[0].keys())
        res = rec.read_table(header, [[r.get(k) for k in header] for r in rows])
        res["table"] = name
        out.append((name, res))
    return out


def to_columns(pack, shape, rows, have, default_identity=None):
    """Canonical rows -> rows keyed by this database's column names.

    `have` is the target table's real columns (upper-cased). Only columns that
    exist are emitted; anything the pack maps to a column this table lacks is
    REPORTED rather than dropped silently.

    THE MULTI-WELL RULE: if a row carries its own identity it wins, and only
    rows without one fall back to the document's. That single rule is what
    turns a multi-well tops study into eight wells' rows instead of one.
    """
    from docshape.engine.recognise import to_number, INTERNAL_KEYS

    colmap = (getattr(pack, "columns", {}) or {}).get(shape, {})
    ident_field = getattr(pack, "identity_field", None) or "uwi"
    tf = (getattr(pack, "transforms", {}) or {}).get(shape)
    if tf:
        rows = tf(rows)

    out, unmapped, no_ident = [], set(), 0
    for r in rows:
        rec_out = {}
        for k, v in r.items():
            if k in INTERNAL_KEYS:
                continue
            cands = colmap.get(k)
            if not cands:
                if k != ident_field:
                    unmapped.add(k)
                continue
            col = next((c for c in cands if c.upper() in have), None) if have \
                else cands[0]
            if col is None:
                unmapped.add(k)
                continue
            rec_out[col] = (to_number(v) if k in pack.numeric
                            else (str(v).strip() if v is not None else None))

        iv = r.get(ident_field) or default_identity
        ident = pack.normalise_identity(iv) if iv else None
        if not ident:
            no_ident += 1
        icol = None
        if have:
            icol = next((c for c in colmap.get(ident_field, [ident_field])
                         if c.upper() in have), None)
        else:
            icol = colmap.get(ident_field, [ident_field])[0]
        if icol and ident:
            rec_out[icol] = ident
        out.append(rec_out)

    return {"rows": out, "unmapped_fields": sorted(unmapped),
            "rows_without_identity": no_ident}


import time as _time

# WHERE THE TIME ACTUALLY GOES.
#
# Measured on the group: parsing is ~0.11s a document while a real run
# averaged ~1.1s, so the write half is most of the cost — but "most" is not
# a number, and the fix for a slow schema lookup is different from the fix
# for a slow insert. These counters make the run say which.
#
# perf_counter around each block, accumulated per step. Cheap enough to
# leave on: a few hundred nanoseconds against operations measured in
# milliseconds.
_TIMES = {}
_COUNTS = {}


def _tick(step, t0):
    _TIMES[step] = _TIMES.get(step, 0.0) + (_time.perf_counter() - t0)
    _COUNTS[step] = _COUNTS.get(step, 0) + 1


def reset_timings():
    _TIMES.clear()
    _COUNTS.clear()


def timings():
    """[(step, seconds, calls)] slowest first."""
    return sorted(((k, v, _COUNTS.get(k, 0)) for k, v in _TIMES.items()),
                  key=lambda r: -r[1])


def format_timings(total_files=0):
    rows = timings()
    if not rows:
        return ""
    tot = sum(v for _k, v, _n in rows)
    parts = []
    for k, v, n in rows:
        pct = (100.0 * v / tot) if tot else 0
        parts.append(f"{k} {v:.1f}s ({pct:.0f}%, {n:,}x)")
    head = f"{tot:.1f}s across {total_files:,} file(s)" if total_files else f"{tot:.1f}s"
    return head + " · " + " · ".join(parts)


_COLS_CACHE = {}


def target_columns(engine, table, schema="file_catalog"):
    """Actual column names on a cat_ table, upper-cased. Empty set on failure.

    CACHED FOR THE PROCESS. This is called once per TABLE per DOCUMENT, and
    each call was opening its own connection to read sys.columns — about
    1,600 round trips on a 541-document run, for an answer that cannot
    change while the run is in flight. Measured against the parse half, the
    reading was a tenth of the cost and this kind of per-row lookup was
    most of the rest.

    A miss is NOT cached: a table that does not exist yet may be created by
    a migration between runs, and remembering its absence would outlive the
    reason for it.
    """
    key = (schema, table)
    got = _COLS_CACHE.get(key)
    if got is not None:
        return got
    from sqlalchemy import text as _t
    try:
        with engine.connect() as con:
            rows = con.execute(_t(
                "SELECT c.name FROM sys.columns c "
                "JOIN sys.objects o ON o.object_id = c.object_id "
                "JOIN sys.schemas s ON s.schema_id = o.schema_id "
                "WHERE s.name = :s AND o.name = :t"),
                {"s": schema, "t": table}).fetchall()
        cols = {r[0].upper() for r in rows}
        if cols:
            _COLS_CACHE[key] = cols
        return cols
    except Exception:
        return set()


# --------------------------------------------------------------------------- #
# Parent keys the documents never name
# --------------------------------------------------------------------------- #
# Every dv_ detail table is keyed on a PARENT the document has no concept of:
#   dv_well_formation_top  PK (uwi, strat_unit_id, interp_id)
#   dv_well_dir_srvy_sta   PK (uwi, survey_id, station_id)  FK -> dir_srvy_hdr
#   dv_well_stimulation    PK (uwi, completion_id, stim_id)
#   dv_well_petro_zone     PK (uwi, interp_id, zone_id)     FK -> petro_interp
#   dv_prod_volume         PK (prod_entity_id, period_date, fluid_type)
#
# A scout ticket says a well was fracced in fifteen stages; it does not say
# which COMPLETION those stages belong to. Promote validates references but
# never invents parents, so without these the insert fails on NOT NULL.
def _slug(value, fallback, n=_ID_MAX):
    t = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "")).strip("_").upper()
    return (t or fallback)[:n]


def _doc_tag(inventory_id, path, n=6):
    """A short, stable tag for the DOCUMENT a row came from.

    Without this, completion_id and prod_entity_id derived from the well alone,
    so a scout ticket and a completion report describing one well produced
    IDENTICAL keys and promote's NOT EXISTS de-duplication collapsed them —
    losing 13 stimulation rows and all 564 production rows. Including the
    document is also more honest: they are two observations, not one.
    """
    basis = str(inventory_id or "") or os.path.basename(str(path or ""))
    return hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:n].upper()


def mint_parent_keys(shape, rows, doc_tag, log=print):
    """Fill the parent/key columns a shape's target requires.

    Returns (rows, {cat_table: [parent rows]}) — the parents are those a
    FOREIGN KEY demands, not merely a NOT NULL.
    """
    parents = {}

    if shape in ("formation_tops", "fluid_contacts"):
        for i, r in enumerate(rows):
            r.setdefault("interp_id", "DOC_" + doc_tag)
            r.setdefault("strat_unit_id",
                         _slug(r.get("strat_unit_name"), "TOP%03d" % (i + 1)))
            if r.get("gross_thickness") in (None, ""):
                top, base = r.get("top_depth"), r.get("base_depth")
                r["gross_thickness"] = (round(base - top, 4)
                                        if isinstance(top, float)
                                        and isinstance(base, float)
                                        and base >= top else 0.0)

    elif shape == "directional_survey":
        seen = {}
        for i, r in enumerate(rows):
            u = r.get("uwi") or "NOUWI"
            sid = "SRVY_%s_%s" % (u, doc_tag)
            r.setdefault("survey_id", sid)
            md = r.get("md")
            base = ("MD%d" % int(md)) if isinstance(md, float) \
                else "STA%04d" % (i + 1)
            k = (u, base)
            seen[k] = seen.get(k, 0) + 1
            r.setdefault("station_id",
                         base if seen[k] == 1 else "%s_%d" % (base, seen[k]))
            parents.setdefault("cat_well_dir_srvy_hdr", {})[(u, sid)] = {
                "uwi": u, "survey_id": sid, "active_ind": "Y"}

    elif shape == "frac_stage":
        for i, r in enumerate(rows):
            u = r.get("uwi") or "NOUWI"
            r.setdefault("completion_id", "COMP_%s_%s" % (u, doc_tag))
            stg = r.get("stage_num")
            r.setdefault("stim_id",
                         ("STG%03d" % int(stg)) if isinstance(stg, float)
                         else "STG%03d" % (i + 1))

    elif shape == "petrophysics":
        for i, r in enumerate(rows):
            u = r.get("uwi") or "NOUWI"
            iid = "INTERP_%s_%s" % (u, doc_tag)
            r.setdefault("interp_id", iid)
            r.setdefault("zone_id", _slug(r.get("zone_name"),
                                          "ZONE%03d" % (i + 1)))
            parents.setdefault("cat_well_petro_interp", {})[(u, iid)] = {
                "uwi": u, "interp_id": iid, "active_ind": "Y"}

    elif shape == "production":
        # dv_prod_volume has a FOREIGN KEY to dv_prod_entity, so minting the
        # id is not enough — the entity has to exist. Missing it failed the
        # whole table on apply after every row was eligible, which is the
        # worst place to discover it. Same parent pattern as the survey header
        # and the petrophysical interpretation.
        for r in rows:
            u = r.get("UWI") or r.get("uwi") or "NOUWI"
            pid = "PENT_%s_%s" % (u, doc_tag)
            r.setdefault("prod_entity_id", pid)
            parents.setdefault("cat_prod_entity", {})[pid] = {
                "prod_entity_id": pid, "uwi": u if u != "NOUWI" else None,
                "prod_entity_type": "WELL", "active_ind": "Y"}

    elif shape == "casing":
        # dv_well_casing keys on (uwi, casing_id) and the documents name their
        # strings — "Surface", "Intermediate", "Production" — so the type IS
        # the natural key. Falls back to an ordinal when a row has no type,
        # and disambiguates on repeats, because a well can legitimately have
        # two intermediate strings and the second must not overwrite the first.
        seen = {}
        for i, r in enumerate(rows):
            base = _slug(r.get("casing_type"), "CSG%03d" % (i + 1))
            u = r.get("uwi") or "NOUWI"
            k = (u, base)
            seen[k] = seen.get(k, 0) + 1
            r.setdefault("casing_id",
                         base if seen[k] == 1 else "%s_%d" % (base, seen[k]))

    elif shape == "completion":
        # THE COMPLETION HAD NO BRANCH AT ALL, so completion_id was never
        # minted and dv_well_completion — which requires it — failed the
        # whole table:
        #     promote_table FAILED: Cannot insert the value NULL into
        #     column 'completion_id'
        # Every completion row from every document stayed in cat_. It was
        # not a recognition problem: the shape reads, captures and then
        # cannot promote.
        #
        # THE ID FORMULA IS NOT FREE CHOICE. frac_stage and perforations
        # already mint "COMP_<uwi>_<doctag>" as the completion they belong
        # to, so the completion row MUST use the same string or a document
        # carrying both a completion summary and its frac stages would
        # produce stages pointing at a completion that does not exist.
        # First row takes the shared id; any further rows are suffixed, so
        # a document listing two completions keeps them distinct while the
        # first still matches what the stages reference.
        seen = {}
        for i, r in enumerate(rows):
            u = r.get("uwi") or "NOUWI"
            base = "COMP_%s_%s" % (u, doc_tag)
            seen[u] = seen.get(u, 0) + 1
            r.setdefault("completion_id",
                         base if seen[u] == 1 else "%s_%d" % (base, seen[u]))
            # PPDM numbers repeated observations of the same completion.
            # setdefault, and dropped later if the column doesn't exist —
            # to_columns keeps only real columns — so this is safe whether
            # or not the mirror carries it.
            r.setdefault("completion_obs_no", float(i + 1))

    elif shape == "perforations":
        for i, r in enumerate(rows):
            u = r.get("uwi") or "NOUWI"
            r.setdefault("completion_id", "COMP_%s_%s" % (u, doc_tag))
            r.setdefault("perf_id", "PERF%03d" % (i + 1))

    return rows, {k: list(v.values()) for k, v in parents.items()}


# --------------------------------------------------------------------------- #
def _inventory_id(engine, path):
    """The catalog's INVENTORY_ID for this file, or None if it isn't crawled.

    A cat_ row whose INVENTORY_ID doesn't resolve looks like provenance but
    joins to nothing, so NULL is written rather than something invented.
    """
    from sqlalchemy import text as _t
    try:
        with engine.connect() as con:
            row = con.execute(_t(
                "SELECT TOP 1 INVENTORY_ID FROM file_catalog.GLOBAL_FILE_CATALOG "
                "WHERE FILE_PATH = :p OR FILE_NAME = :n"),
                {"p": path, "n": os.path.basename(path)}).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _document_identity(pack, rec, path, results):
    """Best evidence first: a header table, the document text, the file name."""
    ident_field = getattr(pack, "identity_field", None) or "uwi"
    for _n, res in results:
        if res["shape"] == "UNKNOWN":
            continue
        for r in res["rows"]:
            v = r.get(ident_field)
            if v and str(v).strip():
                got = pack.normalise_identity(v)
                if got:
                    return got
    if hasattr(pack, "identity_from_text"):
        from docshape.readers import read_text
        got = pack.identity_from_text(read_text(path))
        if got:
            return got
    return pack.identity_from_name(path)


# ── PARALLEL PARSING ────────────────────────────────────────────────────────
#
# 541 documents took ten minutes as a serial loop — about 0.9 a second, and
# nearly all of it inside pdfplumber. A few thousand wells at three documents
# each is an overnight job, and an all-or-nothing one.
#
# The seam is already here and clean: recognise() takes no engine. Everything
# it does — read the tables, identify the shape, map the columns — is pure CPU
# on a path, and everything after it is database-bound. So the split is the
# same one bcp_capture.parse_las_rows already proved in this codebase: WORKERS
# PARSE, THE PARENT WRITES.
#
# It has to be that way round on Windows. Spawn re-imports and pickles
# everything crossing the boundary, and a SQLAlchemy engine will not survive
# that. A path in and plain dicts out will.

_WORKER_STATE = {}


def _worker_pack(pack_name):
    """One pack + recogniser per PROCESS, built on first use.

    Not passed in as an argument: building it is not free, and a fresh one
    per document would cost more than the parallelism saves. Not built at
    import either — that would run in the parent too, for nothing.
    """
    got = _WORKER_STATE.get(pack_name)
    if got is None:
        got = _pack_and_recogniser(pack_name)
        _WORKER_STATE[pack_name] = got
    return got


def parse_file(path, pack_name="petroleum"):
    """Everything that does NOT need the database, for one document.

    Returns a plain dict — no engine, no recogniser, no pack — so it can
    cross a process boundary. `results` is what recognise() produced plus
    the document identity, which needs the pack and so is cheaper to work
    out here than to redo in the parent.

    Never raises: a worker that dies takes the pool's queue with it, and one
    unreadable PDF should not end a run of ten thousand.
    """
    _t0 = _time.perf_counter()
    try:
        pack, rec = _worker_pack(pack_name)
        results = recognise(rec, path)
        ident = _document_identity(pack, rec, path, results) or "" if results else ""
        return {"path": path, "results": results, "identity": ident,
                "error": None, "parse_sec": _time.perf_counter() - _t0}
    except BaseException as e:                            # noqa: BLE001
        return {"path": path, "results": [], "identity": "",
                "error": f"{type(e).__name__}: {e}"}


def parse_many(paths, pack_name="petroleum", workers=None, log=print):
    """parse_file over many documents, in parallel, yielding as they finish.

    Falls back to a serial generator when workers <= 1 or the pool cannot
    start — some environments (a frozen build, a restricted host) simply
    cannot spawn, and a slow run beats no run.
    """
    paths = list(paths)
    if not paths:
        return
    if workers is None:
        workers = max(1, (os.cpu_count() or 2) - 1)
    workers = max(1, min(int(workers), len(paths)))

    if workers == 1:
        for pth in paths:
            yield parse_file(pth, pack_name)
        return

    try:
        from concurrent.futures import ProcessPoolExecutor, as_completed
    except Exception as e:
        log(f"  [parse] pool unavailable ({e}) — serial")
        for pth in paths:
            yield parse_file(pth, pack_name)
        return

    try:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(parse_file, pth, pack_name): pth for pth in paths}
            for fut in as_completed(futs):
                try:
                    yield fut.result()
                except BaseException as e:                # noqa: BLE001
                    yield {"path": futs[fut], "results": [], "identity": "",
                           "error": f"{type(e).__name__}: {e}"}
    except BaseException as e:                            # noqa: BLE001
        log(f"  [parse] pool failed ({type(e).__name__}: {e}) — serial")
        for pth in paths:
            yield parse_file(pth, pack_name)


class CaptureBatch:
    """Accumulate rows across many documents, write them in a few calls.

    MEASURED, not guessed. capture_probe timed the real database:

           1 row    59.6 ms   (59.56 ms/row)
           5 rows  120.4 ms   (24.08 ms/row)
          50 rows  142.1 ms   ( 2.84 ms/row)
         500 rows  221.4 ms   ( 0.44 ms/row)

    Five hundred times the data for under four times the time — so the
    cost is almost entirely FIXED PER CALL, and it scales with column
    count (35ms into a 4-column table, 94ms into a 40-column one), not
    with rows. The pipeline was making 1,907 calls of about five rows.

    So the fix is not a faster insert, it is fewer of them: hold rows per
    target table across a batch of documents, then one delete and one
    insert per table. 1,907 calls becomes roughly 40.

    PROVENANCE MOVES INTO THE ROW. capture() stamps uwi / INVENTORY_ID /
    SOURCE_PATH from its keyword arguments, which only works when every
    row in the call came from one document. Here they do not, so each row
    carries its own — and capture() already lets a row's own value win
    over the stamp, so nothing in it needed changing.
    """

    __slots__ = ("engine", "size", "log", "tables", "ids", "docs", "queued",
                 "flushes")

    def __init__(self, engine, size=100, log=print):
        self.engine = engine
        self.size = max(1, int(size or 1))
        self.log = log
        self.tables = {}          # cat_table -> [row dicts]
        self.ids = {}             # cat_table -> {inventory_id} that fed it
        self.docs = 0
        self.queued = 0
        self.flushes = 0

    def add(self, cat_table, rows, uwi=None, inventory_id=None,
            source_path=None, source=None):
        if not rows:
            return 0
        for r in rows:
            if uwi is not None:
                r.setdefault("uwi", uwi)
            if inventory_id is not None:
                r.setdefault("INVENTORY_ID", inventory_id)
            if source_path is not None:
                r.setdefault("SOURCE_PATH", source_path)
            if source is not None:
                r.setdefault("source", source)
        self.tables.setdefault(cat_table, []).extend(rows)
        if inventory_id is not None:
            self.ids.setdefault(cat_table, set()).add(inventory_id)
        self.queued += len(rows)
        return len(rows)

    def end_document(self):
        """One document finished. Flush when the batch is full."""
        self.docs += 1
        if self.docs >= self.size:
            self.flush()

    def flush(self):
        """Write everything held, then reset. Returns rows written."""
        if not self.tables:
            self.docs = 0
            return 0
        from dataview.file_catalog.catalog_capture import capture
        from sqlalchemy import text as _t

        written = 0
        for table, rows in list(self.tables.items()):
            ids = sorted(self.ids.get(table) or ())
            try:
                with self.engine.begin() as con:
                    # ONE delete for every document in the batch that fed
                    # this table — same idempotency as before, scoped the
                    # same way, in one statement instead of one per file.
                    # Chunked because a parameter list has a ceiling.
                    for i in range(0, len(ids), 500):
                        chunk = ids[i:i + 500]
                        marks = ", ".join(f":i{j}" for j in range(len(chunk)))
                        con.execute(
                            _t(f"DELETE FROM file_catalog.{table} "
                               f"WHERE INVENTORY_ID IN ({marks})"),
                            {f"i{j}": v for j, v in enumerate(chunk)})
                    # replace=False: the delete above already did it, and
                    # capture's own per-inventory_id delete would be wrong
                    # here anyway — it keys on a single id.
                    written += capture(self.engine, table, rows,
                                       uwi=None, inventory_id=None,
                                       source_path=None, source=None,
                                       replace=False, conn=con, log=self.log)
            except Exception as e:                       # noqa: BLE001
                # A BATCH FAILURE MUST NOT COST THE WHOLE BATCH. Fall back
                # to one call per document for this table only — the same
                # granularity the unbatched path had, so the blast radius
                # of a bad row is unchanged.
                self.log(f"  [batch] {table}: {type(e).__name__}: "
                         f"{str(e)[:120]} — retrying per document")
                written += self._retry_per_document(table, rows)
        self.flushes += 1
        self.tables, self.ids, self.docs = {}, {}, 0
        return written

    def _retry_per_document(self, table, rows):
        from dataview.file_catalog.catalog_capture import capture
        by_doc = {}
        for r in rows:
            by_doc.setdefault(r.get("INVENTORY_ID"), []).append(r)
        got = 0
        for inv, group in by_doc.items():
            try:
                got += capture(self.engine, table, group, uwi=None,
                               inventory_id=inv, source_path=None,
                               source=None, replace=True, log=self.log)
            except Exception as e:                       # noqa: BLE001
                self.log(f"  [batch] {table} doc {inv}: "
                         f"{type(e).__name__}: {str(e)[:100]} — skipped")
        return got


def load_parsed(engine, parsed, pack=None, apply=False, only_shape=None,
                log=print, inventory_id=None, batch=None):
    """The database half: capture what a worker already recognised.

    Everything here needs the engine, so it stays in the parent — one
    connection, one transaction discipline, and the writes stay ordered.
    """
    path = parsed["path"]
    if parsed.get("error"):
        log("  %-44s parse failed: %s"
            % (os.path.basename(path), parsed["error"]))
        return {"file": path, "captured": 0, "tables": 0, "detail": {},
                "skipped": [], "inventory_id": None, "error": parsed["error"]}
    return _capture_results(engine, path, parsed["results"],
                            parsed.get("identity") or "", pack=pack,
                            apply=apply, only_shape=only_shape, log=log,
                            inventory_id=inventory_id, batch=batch)


def load_file(engine, path, pack=None, rec=None, apply=False, only_shape=None,
              log=print):
    """Recognise every table in one document and capture what has a target."""
    from dataview.file_catalog.catalog_capture import capture

    if pack is None or rec is None:
        pack, rec = _pack_and_recogniser()

    results = recognise(rec, path)
    if not results:
        log("  %-44s no tables" % os.path.basename(path))
        return {"file": path, "captured": 0, "tables": 0, "detail": {},
                "skipped": [], "inventory_id": _inventory_id(engine, path)}
    doc_ident = _document_identity(pack, rec, path, results) or ""
    # One implementation of the write half, shared with the parallel path,
    # so the two can never drift apart.
    return _capture_results(engine, path, results, doc_ident, pack=pack,
                            apply=apply, only_shape=only_shape, log=log)


def _capture_results(engine, path, results, doc_ident, pack=None, apply=False,
                     only_shape=None, log=print, inventory_id=None,
                     batch=None):
    """Capture already-recognised tables. The database half, in one place.

    inventory_id is accepted because the CALLER USUALLY ALREADY HAS IT —
    the pipeline's own file query selects INVENTORY_ID and then threw it
    away, so every document was paying for a second lookup whose WHERE
    clause is `FILE_PATH = :p OR FILE_NAME = :n` and cannot use an index
    well. Passing it through removes one round trip per document.
    """
    from dataview.file_catalog.catalog_capture import capture

    if pack is None:
        pack, _rec = _pack_and_recogniser()
    if inventory_id is not None:
        inv = inventory_id
    else:
        _t = _time.perf_counter()
        inv = _inventory_id(engine, path)
        _tick("inventory_id", _t)
    doc_tag = _doc_tag(inv, path)
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    total, detail, skipped = 0, {}, []

    # ONE CONNECTION FOR THE DOCUMENT, NOT ONE PER TABLE.
    #
    # capture() has always accepted conn= and shape_loader never passed it,
    # so every call fell to `engine.begin()` — acquire, BEGIN, work, COMMIT,
    # release. Measured on 617 documents: 1,410 capture calls at ~109ms
    # each, 75% of the whole stage. The insert itself is already batched by
    # column signature with fast_executemany; it was the ceremony around it
    # that cost.
    #
    # A NESTED transaction per table keeps the error behaviour identical:
    # a table that fails rolls back alone and the loop carries on, exactly
    # as it did when each call had its own transaction. Sharing one
    # transaction for the whole document would have made one bad table
    # discard the good ones.
    _con = None
    if apply:
        try:
            _con = engine.connect()
        except Exception as e:
            log("     !! could not open a connection: %s: %s"
                % (type(e).__name__, e))

    try:
        return _capture_loop(engine, _con, path, results, doc_ident, pack, inv,
                             doc_tag, now, apply, only_shape, log,
                             total, detail, skipped, batch)
    finally:
        if _con is not None:
            try:
                _con.close()
            except Exception:
                pass


def _capture_loop(engine, _con, path, results, doc_ident, pack, inv, doc_tag,
                  now, apply, only_shape, log, total, detail, skipped,
                  batch=None):
    from dataview.file_catalog.catalog_capture import capture

    for tname, res in results:
        shape, target = res["shape"], res["target"]
        if shape == "UNKNOWN" or not target:
            skipped.append((tname, shape))
            continue
        if only_shape and shape != only_shape:
            continue

        _t = _time.perf_counter()
        have = target_columns(engine, target)
        _tick("schema_lookup", _t)
        if not have:
            log("     !! %s not found in file_catalog — skipping" % target)
            skipped.append((tname, shape + " (no table)"))
            continue

        _t = _time.perf_counter()
        built = to_columns(pack, shape, res["rows"], have, doc_ident)
        _tick("map_columns", _t)
        rows = [r for r in built["rows"] if r]
        if not rows:
            continue

        _t = _time.perf_counter()
        rows, parent_rows = mint_parent_keys(shape, rows, doc_tag, log=log)
        _tick("mint_keys", _t)

        for r in rows:
            if "ACTIVE_IND" in have:
                r.setdefault("active_ind", "Y")
            if "ROW_CREATED_BY" in have:
                r.setdefault("row_created_by", "DataWrangler")
            if "ROW_CREATED_DATE" in have:
                r.setdefault("row_created_date", now)
            if "SOURCE" in have:
                r.setdefault("source", SOURCE)
            if "DEPTH_OUOM" in have:
                r.setdefault("depth_ouom", DEPTH_UOM)
            fluid = str(r.get("fluid_type") or "").upper()
            for col, code in FLUID_UOM.get(fluid, {}).items():
                if col.upper() in have:
                    r.setdefault(col, code)

        if built["unmapped_fields"]:
            log("     ~ %s: no column for %s"
                % (shape, ", ".join(built["unmapped_fields"])))
        if built["rows_without_identity"]:
            log("     ~ %s: %d row(s) with no identity — held by the dv_well "
                "gate on promote" % (shape, built["rows_without_identity"]))

        if not apply:
            for ptable, prows in parent_rows.items():
                log("     [dry] %-20s -> %-26s %4d row(s)"
                    % ("(parent)", ptable, len(prows)))
            log("     [dry] %-20s -> %-26s %4d row(s)"
                % (shape, target, len(rows)))
            detail[target] = detail.get(target, 0) + len(rows)
            total += len(rows)
            continue

        try:
            for ptable, prows in parent_rows.items():
                if not target_columns(engine, ptable):
                    log("     !! %s not found — %s children will fail their "
                        "foreign key" % (ptable, shape))
                    continue
                for pr in prows:
                    pr.setdefault("source", SOURCE)
                    pr.setdefault("row_created_by", "DataWrangler")
                    pr.setdefault("row_created_date", now)
                _t = _time.perf_counter()
                if batch is not None:
                    pn = batch.add(ptable, prows, uwi=doc_ident or None,
                                   inventory_id=inv, source_path=path,
                                   source=SOURCE)
                elif _con is not None:
                    with _con.begin():
                        pn = capture(engine, ptable, prows,
                                     uwi=doc_ident or None, inventory_id=inv,
                                     source_path=path, source=SOURCE,
                                     conn=_con)
                else:
                    pn = capture(engine, ptable, prows, uwi=doc_ident or None,
                                 inventory_id=inv, source_path=path,
                                 source=SOURCE)
                _tick("capture_parent", _t)
                if pn:
                    log("     %-20s -> %-26s %4d row(s)" % ("(parent)", ptable, pn))
                    detail[ptable] = detail.get(ptable, 0) + pn
                    total += pn
            _t = _time.perf_counter()
            if batch is not None:
                # Queued, not written. The count is what this document
                # contributed — the flush reports what actually landed.
                n = batch.add(target, rows, uwi=doc_ident or None,
                              inventory_id=inv, source_path=path,
                              source=SOURCE)
            elif _con is not None:
                with _con.begin():
                    n = capture(engine, target, rows, uwi=doc_ident or None,
                                inventory_id=inv, source_path=path,
                                source=SOURCE, conn=_con)
            else:
                n = capture(engine, target, rows, uwi=doc_ident or None,
                            inventory_id=inv, source_path=path, source=SOURCE)
            _tick("capture", _t)
        except Exception as e:
            log("     !! capture into %s failed: %s: %s"
                % (target, type(e).__name__, e))
            continue
        n = n or 0
        log("     %-20s -> %-26s %4d row(s)" % (shape, target, n))
        detail[target] = detail.get(target, 0) + n
        total += n

    log("  %-44s %5d row(s)%s" % (
        os.path.basename(path), total,
        ("   skipped: " + ", ".join(s for _t, s in skipped)) if skipped else ""))
    return {"file": path, "captured": total, "tables": len(results),
            "detail": detail, "skipped": skipped, "inventory_id": inv}


def load_dir(engine, target_dir, pack_name="petroleum", apply=False,
             only_shape=None, log=print):
    from docshape.readers import collect, TABLE_EXTS
    pack, rec = _pack_and_recogniser(pack_name)
    paths = [p for p in collect(target_dir)
             if os.path.splitext(p)[1].lower() in TABLE_EXTS]
    log("-- %d document(s) · pack '%s' · %s\n"
        % (len(paths), pack_name, "APPLY" if apply else "DRY RUN"))
    totals, per_table, no_inv = 0, defaultdict(int), 0
    for p in paths:
        try:
            r = load_file(engine, p, pack, rec, apply=apply,
                          only_shape=only_shape, log=log)
        except Exception as e:
            log("  !! %s: %s: %s" % (os.path.basename(p), type(e).__name__, e))
            continue
        totals += r.get("captured", 0)
        for t, n in (r.get("detail") or {}).items():
            per_table[t] += n
        if r.get("captured", 0) and not r.get("inventory_id"):
            no_inv += 1
    log("")
    log("=" * 72)
    for t, n in sorted(per_table.items(), key=lambda kv: -kv[1]):
        log("  %-32s %7s row(s)" % (t, format(n, ",")))
    log("  %-32s %7s row(s)" % ("TOTAL", format(totals, ",")))
    if no_inv:
        log("\n  ⚠ %d document(s) CONTRIBUTED ROWS but have no INVENTORY_ID — "
            "they aren't in GLOBAL_FILE_CATALOG, so those rows carry no "
            "lineage. Crawl the folder, then backfill by joining on "
            "SOURCE_PATH." % no_inv)
    if not apply:
        log("\n  DRY RUN — nothing written. Add --apply.")
    return {"captured": totals, "per_table": dict(per_table)}


def _main() -> int:
    ap = argparse.ArgumentParser(
        description="Capture docshape-recognised document tables into cat_ tables")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--pack", default="petroleum")
    ap.add_argument("--file")
    ap.add_argument("--dir")
    ap.add_argument("--shape", help="limit to one shape")
    ap.add_argument("--apply", action="store_true",
                    help="write the rows (default is a dry run)")
    a = ap.parse_args()
    if not (a.file or a.dir):
        ap.print_help()
        return 1

    from dataview.core.schema_introspect import make_engine
    eng = make_engine(a.server, a.database, "ODBC Driver 17 for SQL Server")
    if a.file:
        pack, rec = _pack_and_recogniser(a.pack)
        print("-- %s · pack '%s'\n" % ("APPLY" if a.apply else "DRY RUN", a.pack))
        load_file(eng, a.file, pack, rec, apply=a.apply, only_shape=a.shape)
    else:
        load_dir(eng, a.dir, a.pack, apply=a.apply, only_shape=a.shape)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
