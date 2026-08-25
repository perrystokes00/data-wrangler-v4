"""
demo_reset.py  —  DataView v3 · clear loaded data

full=True (default): empties every base table in the database by disabling all
FK constraints, deleting, then re-enabling them — order-/cycle-proof. Reference
tables are preserved:
  • dv_r_*  controlled-vocabulary reference tables
  • dv_country / dv_province_state / dv_county spatial reference (seeded from
    authoritative D12A data — must never be auto-cleared)

LEARNED STATE is also preserved, for a different reason:
  • dv_column_map        every approved column mapping / fingerprint recall
  • dv_target_attribute  schema metadata the fit pre-flight reads
Reference tables could be re-seeded from source if lost. These could not be
recovered at all — they are decisions somebody made, one file at a time.

full=False: clears only dv_well + everything that FK-references it (closure),
leaving reference and inventory data in place.

DELETE (not TRUNCATE) is used because SQL Server blocks TRUNCATE on any
FK-referenced table even when constraints are disabled.
"""

from collections import defaultdict, deque

from sqlalchemy import text

from dataview.core import reset_protection as _rp

# Bump this whenever the reset logic changes. app_v3 displays it next to the
# Reset button so you can SEE which version is live after a deploy + restart.
RESET_VERSION = ("2026-08-08 truncate full-wipe "
                 "(keeps reference tables + dv_column_map, "
                 "dv_column_synonym, dv_target_attribute)")

# Tables to clear. dv_well* is a prefix match (covers dv_well and every
# dv_well_<detail> table); the entity parents are matched by exact name.
# Extend _CLEAR_PREFIXES (e.g. add "dv_seis") when a new data domain arrives.
# Spatial domain (per-feature geography): fields, leases/tracts, boundaries,
# pipelines, seismic sets. full=True clears these via the all-tables sweep;
# these entries make the targeted full=False path clear them too.
# ONE SOURCE OF TRUTH. These lived here AND in clear_catalog.PROTECTED,
# and drifted -- see dataview/core/reset_protection.py for why that is the
# same failure as MIRROR_TABLES vs LINEAGE. selftest pins that they agree.
_CLEAR_PREFIXES = _rp.CLEAR_PREFIXES
_CLEAR_EXACT = _rp.CLEAR_EXACT


def _should_clear(name: str) -> bool:
    low = name.lower()
    if low in _PRESERVE_EXACT:
        return False              # learned state, whichever path asks
    return low in _CLEAR_EXACT or any(low.startswith(p) for p in _CLEAR_PREFIXES)


# Controlled-vocabulary + geographic reference tables that should SURVIVE a
# reset — they're seeded standards the pipeline FKs into, not loaded data.
_REFERENCE_EXACT = _rp.REFERENCE_EXACT

# LEARNED STATE — survives a full wipe, and is NOT reference data.
#
# full=True empties every base table in the database except the reference
# ones above. That is right for DATA, and wrong for the tables that hold
# DECISIONS A PERSON MADE:
#
#   dv_column_map        the synonym store and fingerprint recall. Every
#                        column mapping ever approved, keyed by source-file
#                        pattern. It is why a remembered folder loads
#                        without asking a single question. Months of
#                        accumulated decisions — and a reload regenerates
#                        the DATA but cannot regenerate these.
#   dv_column_synonym    the column-level synonym store — the other half of
#                        the same memory. It was protected here only by
#                        ABSENCE from the clear list, which is the
#                        "protected by omission is not protection" weakness
#                        this set exists to remove. clear_catalog.PROTECTED
#                        already names it; THE TWO RESET PATHS MUST PROTECT
#                        THE SAME NAMES, and finding one guarded is not
#                        evidence about the other.
#   dv_target_attribute  schema metadata the fit pre-flight reads.
#
# Kept separate from _REFERENCE_EXACT deliberately: those are seeded
# standards that could be re-seeded from source; these could not be
# recovered at all. Two different reasons to survive, so two sets.
_PRESERVE_EXACT = _rp.PROTECTED


def _is_reference(name: str) -> bool:
    low = name.lower()
    return low.startswith("dv_r_") or low in _REFERENCE_EXACT


def _is_preserved(name: str) -> bool:
    """Learned state. Never cleared, and NOT gated on keep_reference —
    somebody turning keep_reference off wants the seeds gone, not the
    approved mappings."""
    return name.lower() in _PRESERVE_EXACT


def _all_tables(con) -> list:
    rows = con.execute(text("""
        SELECT s.name, t.name
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
    """))
    return [(r[0], r[1]) for r in rows]


def _rowcount(con, schema: str, table: str) -> int:
    """Instant row count from partition stats — no table scan."""
    n = con.execute(text("""
        SELECT SUM(p.row_count)
        FROM sys.dm_db_partition_stats p
        WHERE p.object_id = OBJECT_ID(:o) AND p.index_id IN (0, 1)
    """), {"o": f"[{schema}].[{table}]"}).scalar()
    return int(n or 0)


_REF_ACTION = {"NO_ACTION": "NO ACTION", "CASCADE": "CASCADE",
               "SET_NULL": "SET NULL", "SET_DEFAULT": "SET DEFAULT"}


def _fk_defs(con) -> list:
    """Capture every FK with enough detail to recreate it exactly: parent and
    referenced schema/table, the ordered column lists, and the ON DELETE/UPDATE
    actions."""
    meta = con.execute(text("""
        SELECT fk.object_id AS oid, fk.name AS name,
               sp.name AS ps, tp.name AS pt,
               sr.name AS rs, tr.name AS rt,
               fk.delete_referential_action_desc AS del_act,
               fk.update_referential_action_desc AS upd_act
        FROM sys.foreign_keys fk
        JOIN sys.tables tp   ON tp.object_id = fk.parent_object_id
        JOIN sys.schemas sp  ON sp.schema_id = tp.schema_id
        JOIN sys.tables tr   ON tr.object_id = fk.referenced_object_id
        JOIN sys.schemas sr  ON sr.schema_id = tr.schema_id
    """)).mappings().all()
    cols = con.execute(text("""
        SELECT fkc.constraint_object_id AS oid,
               cp.name AS pcol, cr.name AS rcol
        FROM sys.foreign_key_columns fkc
        JOIN sys.columns cp ON cp.object_id = fkc.parent_object_id
                           AND cp.column_id = fkc.parent_column_id
        JOIN sys.columns cr ON cr.object_id = fkc.referenced_object_id
                           AND cr.column_id = fkc.referenced_column_id
        ORDER BY fkc.constraint_object_id, fkc.constraint_column_id
    """)).mappings().all()
    bycol = defaultdict(list)
    for c in cols:
        bycol[c["oid"]].append((c["pcol"], c["rcol"]))
    out = []
    for m in meta:
        pairs = bycol[m["oid"]]
        out.append({
            "name": m["name"], "ps": m["ps"], "pt": m["pt"],
            "rs": m["rs"], "rt": m["rt"],
            "pcols": [p for p, _ in pairs], "rcols": [r for _, r in pairs],
            "del_act": m["del_act"], "upd_act": m["upd_act"],
        })
    return out


def _fk_drop_sql(fk: dict) -> str:
    return f'ALTER TABLE [{fk["ps"]}].[{fk["pt"]}] DROP CONSTRAINT [{fk["name"]}]'


def _fk_create_sql(fk: dict, nocheck: bool = False) -> str:
    chk = "WITH NOCHECK" if nocheck else "WITH CHECK"
    pc = ", ".join(f"[{c}]" for c in fk["pcols"])
    rc = ", ".join(f"[{c}]" for c in fk["rcols"])
    return (f'ALTER TABLE [{fk["ps"]}].[{fk["pt"]}] {chk} '
            f'ADD CONSTRAINT [{fk["name"]}] FOREIGN KEY ({pc}) '
            f'REFERENCES [{fk["rs"]}].[{fk["rt"]}] ({rc}) '
            f'ON DELETE {_REF_ACTION.get(fk["del_act"], "NO ACTION")} '
            f'ON UPDATE {_REF_ACTION.get(fk["upd_act"], "NO ACTION")}')


def _all_edges(con) -> list:
    """Every FK as (child(schema,name), parent(schema,name)) across ALL schemas,
    so a referencing table in another schema (e.g. dataview_gom -> dataview) is
    found too, not just intra-schema FKs."""
    rows = con.execute(text("""
        SELECT cs.name, ct.name, ps.name, pt.name
        FROM sys.foreign_keys fk
        JOIN sys.tables  ct ON ct.object_id  = fk.parent_object_id
        JOIN sys.schemas cs ON cs.schema_id   = ct.schema_id
        JOIN sys.tables  pt ON pt.object_id   = fk.referenced_object_id
        JOIN sys.schemas ps ON ps.schema_id   = pt.schema_id
    """))
    out = []
    for cs, cn, ps, pn in rows:
        child, parent = (cs, cn), (ps, pn)
        if child != parent:
            out.append((child, parent))
    return out


def _fk_closure(base, edges):
    """Expand `base` with every table that (transitively) REFERENCES a table in
    it, so a DELETE of the base can't be blocked by a child left outside the set
    (the dv_strat_interval -> dv_well_formation_top case). Walks only the
    referencing direction, so upstream reference tables (dv_r_*) and the
    file_catalog inventory — which are *parents* of dv_well — are never pulled
    in."""
    clear = set(base)
    changed = True
    while changed:
        changed = False
        for child, parent in edges:
            if parent in clear and child not in clear:
                clear.add(child)
                changed = True
    return clear


def _delete_order(tables, edges):
    """Tables in safe DELETE order — children before parents. Nodes are
    (schema, name) tuples; edges are restricted to the set being deleted."""
    tset = set(tables)
    children = defaultdict(set)
    indeg = {t: 0 for t in tables}
    seen = set()
    for child, parent in edges:
        if child not in tset or parent not in tset:
            continue
        if (child, parent) in seen:
            continue
        seen.add((child, parent))
        children[parent].add(child)
        indeg[child] += 1
    # Kahn topological sort: parents first …
    q = deque([t for t in tables if indeg[t] == 0])
    topo = []
    while q:
        n = q.popleft()
        topo.append(n)
        for c in children[n]:
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    topo += [t for t in tables if t not in topo]   # FK cycles (if any) appended
    topo.reverse()                                  # … reversed = children first
    return topo


def reset_demo_data(engine,
                    target_schema: str = "dataview",
                    stg_schema: str = "stg",
                    stg_table: str = "stg_well_header",
                    drop_seeded_sources: bool = False,
                    full: bool = True,
                    keep_reference: bool = True,
                    method: str = "truncate",
                    log=None,
                    lock_timeout_ms: int = 60000) -> dict:
    """
    Clear loaded data. Returns {schema.table: rows_cleared} for every table that
    had rows, so the caller can see what was cleared.

    full=True (default): empties every base table in the database (all user
    schemas). With keep_reference=True (default) the controlled-vocabulary
    (dv_r_*) and geographic (dv_country/dv_province_state/dv_county) reference
    tables are preserved, so the pipeline can still promote without re-seeding.

      method="truncate" (default): drop every FK, TRUNCATE the tables, recreate
        the FKs — all in one atomic transaction. Near-instant regardless of row
        counts or expensive indexes (e.g. the dv_well spatial index), because
        TRUNCATE deallocates pages instead of logging and re-indexing per row.
        Recreated FKs are validated WITH CHECK; any that can't validate are
        re-added WITH NOCHECK and counted in result["_untrusted_fks"].
      method="delete": disable FKs, DELETE every table, re-enable. Slower but
        does not touch FK definitions — a fallback if TRUNCATE is undesirable.

    full=False: clears only the target schema's well/entity data plus everything
    that references it (FK closure), leaving reference + inventory data in place.

    log:  optional callable(str) for live progress (the UI passes one so a long
          wipe shows per-table messages instead of a silent spinner).
    lock_timeout_ms:  if the wipe is BLOCKED waiting on another connection's lock
          for this long, it fails with SQL Server error 1222 instead of hanging
          forever. It does NOT cap a legitimately long-running statement — only
          the time spent waiting to acquire a lock.
    """
    _log = log if callable(log) else (lambda *_a, **_k: None)
    result: dict = {}

    with engine.begin() as con:
        # Fail fast on a block instead of hanging with no message.
        con.execute(text(f"SET LOCK_TIMEOUT {int(lock_timeout_ms)}"))
        all_tabs = _all_tables(con)            # [(schema, name)]
        user = [(s, n) for (s, n) in all_tabs
                if s.lower() not in ("sys", "information_schema")]

        if full:
            to_clear = [(s, t) for (s, t) in user
                        if not (keep_reference and _is_reference(t))
                        and not _is_preserved(t)]
            kept = len(user) - len(to_clear)

            if method == "truncate":
                # Fast path: drop all FKs, TRUNCATE, recreate FKs — atomically.
                fks = _fk_defs(con)
                _log(f"Dropping {len(fks)} FK constraints…")
                for fk in fks:
                    con.execute(text(_fk_drop_sql(fk)))
                _log(f"Truncating {len(to_clear)} tables "
                     f"(keeping {kept} reference tables)…")
                for s, t in to_clear:
                    cnt = _rowcount(con, s, t)
                    _log(f"  • {s}.{t} ({cnt:,} rows)…")
                    try:
                        con.execute(text(f"TRUNCATE TABLE [{s}].[{t}]"))
                    except Exception:
                        con.execute(text(f"DELETE FROM [{s}].[{t}]"))
                    if cnt:
                        result[f"{s}.{t}"] = cnt
                _log(f"Recreating {len(fks)} FK constraints…")
                untrusted = 0
                for fk in fks:
                    try:
                        con.execute(text(_fk_create_sql(fk)))         # WITH CHECK
                    except Exception:
                        con.execute(text(_fk_create_sql(fk, nocheck=True)))
                        untrusted += 1
                if untrusted:
                    result["_untrusted_fks"] = untrusted
            else:
                # Fallback: disable FKs, DELETE every table, re-enable.
                _log(f"Disabling FK constraints on {len(user)} tables…")
                for s, t in user:
                    con.execute(text(
                        f"ALTER TABLE [{s}].[{t}] NOCHECK CONSTRAINT ALL"))
                _log(f"Deleting {len(to_clear)} tables "
                     f"(keeping {kept} reference tables)…")
                for s, t in to_clear:
                    _log(f"  • {s}.{t} …")
                    n = con.execute(text(f"DELETE FROM [{s}].[{t}]")).rowcount
                    if n and n > 0:
                        result[f"{s}.{t}"] = int(n)
                _log("Re-enabling FK constraints…")
                for s, t in user:
                    try:
                        con.execute(text(
                            f"ALTER TABLE [{s}].[{t}] WITH CHECK CHECK CONSTRAINT ALL"))
                    except Exception:
                        pass                   # leave it disabled rather than fail
        else:
            edges = _all_edges(con)
            base = {(s, n) for (s, n) in user
                    if s == target_schema and _should_clear(n)
                    and not n.lower().startswith("stg_")}
            clear = _fk_closure(base, edges)
            clear = {(s, n) for (s, n) in clear
                     if not n.lower().startswith("stg_")}
            order = _delete_order(
                list(clear),
                [(c, p) for (c, p) in edges if c in clear and p in clear])
            for s, t in order:
                n = con.execute(text(f"DELETE FROM [{s}].[{t}]")).rowcount
                if n and n > 0:
                    result[f"{s}.{t}"] = int(n)
            if drop_seeded_sources:
                con.execute(text(
                    f"DELETE FROM {target_schema}.dv_r_source "
                    f"WHERE row_created_by IN ('ENTITY_MAP', 'MANUAL')"))

        # Fixed staging table (stg.<stg_table>) — TRUNCATE if it exists.
        if con.execute(text(
                f"SELECT OBJECT_ID('{stg_schema}.{stg_table}')")).scalar():
            con.execute(text(f"TRUNCATE TABLE {stg_schema}.{stg_table}"))

        # Any leftover stg_* scratch tables in the target schema — drop them.
        for s, t in all_tabs:
            if s == target_schema and t.lower().startswith("stg_"):
                try:
                    con.execute(text(f"DROP TABLE [{s}].[{t}]"))
                    result[f"(drop) {t}"] = 1
                except Exception:
                    pass

    # reset capture-progress columns on any PRESERVED inventory rows, so a reset
    # that keeps GLOBAL_FILE_CATALOG doesn't leave files stamped 'captured' with no
    # cat_* rows (which makes the capture stage skip them forever).
    try:
        with engine.begin() as _cc:
            if _cc.execute(text(
                    "SELECT OBJECT_ID('file_catalog.GLOBAL_FILE_CATALOG')")).scalar():
                _sets = []
                for _col in ("CAPTURED_HASH", "HEADER_EXTRACTED", "CATALOG_READINESS",
                             "VAULTED_AT", "PROMOTED_AT"):
                    if _cc.execute(text(
                            f"SELECT COL_LENGTH('file_catalog.GLOBAL_FILE_CATALOG','{_col}')")).scalar():
                        _sets.append(f"{_col} = NULL")
                if _sets:
                    _nrc = _cc.execute(text(
                        "UPDATE file_catalog.GLOBAL_FILE_CATALOG SET "
                        + ", ".join(_sets))).rowcount
                    if _nrc:
                        result["(reset capture-progress columns)"] = int(_nrc)
    except Exception as _e:
        result["(capture-state reset skipped)"] = str(_e)[:80]

    if not result:
        result["(already empty)"] = 0
    result["_reset_version"] = RESET_VERSION
    _log("Done.")
    return result
