"""
catalog_readiness.py
====================
Single authority for the *catalog/promote axis* of GLOBAL_FILE_CATALOG.CATALOG_READINESS.

The problem this replaces
-------------------------
Readiness was asserted in ~8 places by page_workbench._set_readiness_cataloged,
in a transaction separate from the capture() insert, gated inconsistently per
branch. A file could be stamped CATALOGED with no cat_* rows behind it; the
capture WHERE-clause then excludes CATALOGED, so it was skipped forever and
promote had nothing to lift. (The 1-of-3 symptom.)

The model here
--------------
Readiness on the catalog/promote axis is DERIVED from row reality, never
asserted by the loaders:

    rows in file_catalog.cat_*  for this INVENTORY_ID        -> 'CATALOGED'  (pending promote)
    represented in dataview.dv_* (cat_* already consumed)    -> 'PROMOTED'
    stamped CATALOGED but NO rows on either side             -> demote so it retries
    anything else (identity states from triage)              -> left untouched

The dv_* subtlety: promote_catalog MOVES rows (copy up, delete from cat_*), so a
fully promoted file has ZERO cat_* rows. Deriving CATALOGED from cat_* alone
would regress a promoted file to UNCATALOGED and loop it forever. So 'promoted'
is detected two ways, covering header-only wells (no detail rows) too:
    * INVENTORY_ID present in any dataview.dv_* detail table (carries INVENTORY_ID), OR
    * the file's MATCHED_UWI exists in dataview.dv_well (the header landed)

Triage stays the owner of the identity axis (NEEDS_UWI / AWAITING_UWI / REVIEW /
READY) and already preserves CATALOGED/PROMOTED, so the two compose: triage sets
the identity baseline, this overlays the evidence-based catalog/promote state.

Usage
-----
    from dataview.file_catalog.catalog_readiness import reconcile_readiness
    reconcile_readiness(engine, dialect="mssql", log=print)          # apply
    reconcile_readiness(engine, dialect="mssql", dry=True, log=print) # preview only

Set-based throughout (staging temp tables + one CASE UPDATE) — no per-row loops,
per the SQL-Express doctrine. mssql only; other dialects are a no-op with a note.

DEPLOY: Deploy-Latest catalog_readiness.py .   (ROOT — bare import, streamlit-free)
"""
from __future__ import annotations

CAT_SCHEMA = "file_catalog"
DV_SCHEMA = "dataview"
GFC = f"{CAT_SCHEMA}.GLOBAL_FILE_CATALOG"


def _discover(con):
    """Return (cat_tables, dv_tables): tables carrying an INVENTORY_ID column.

    cat_tables -> file_catalog.cat_*   (pending rows)
    dv_tables  -> dataview.dv_*         (promoted detail rows)
    """
    from sqlalchemy import text as _t
    rows = con.execute(_t("""
        SELECT s.name AS sch, t.name AS tbl
          FROM sys.tables t
          JOIN sys.schemas s ON s.schema_id = t.schema_id
          JOIN sys.columns c ON c.object_id = t.object_id
         WHERE c.name = 'INVENTORY_ID'
           AND ( (s.name = :cs AND t.name LIKE 'cat[_]%')
              OR (s.name = :ds AND t.name LIKE 'dv[_]%') )
    """), {"cs": CAT_SCHEMA, "ds": DV_SCHEMA}).fetchall()
    cat = [(r.sch, r.tbl) for r in rows if r.sch == CAT_SCHEMA]
    dv = [(r.sch, r.tbl) for r in rows if r.sch == DV_SCHEMA]
    return cat, dv


def _build_distinct_temp(con, temp: str, sources: list, extra_selects: list | None = None):
    """Create #temp(INVENTORY_ID) with the GFC column type, populate it with the
    DISTINCT union of INVENTORY_ID across `sources` (list of (schema, table)) plus
    any `extra_selects` (raw 'SELECT INVENTORY_ID ...' strings), and dedup so the
    later LEFT JOIN can't fan out. Returns the row count."""
    from sqlalchemy import text as _t
    con.execute(_t(f"IF OBJECT_ID('tempdb..{temp}') IS NOT NULL DROP TABLE {temp};"))
    # Clone the exact INVENTORY_ID type from GFC so comparisons stay sargable.
    con.execute(_t(f"SELECT TOP 0 INVENTORY_ID INTO {temp} FROM {GFC};"))

    selects = [f"SELECT INVENTORY_ID FROM {sch}.{tbl} WHERE INVENTORY_ID IS NOT NULL"
               for sch, tbl in sources]
    if extra_selects:
        selects.extend(extra_selects)
    if not selects:
        return 0

    # Stage raw, then project DISTINCT into the typed temp. UNION ALL into a raw
    # heap is cheaper than UNION across many tables; the DISTINCT happens once.
    raw = temp + "_raw"
    con.execute(_t(f"IF OBJECT_ID('tempdb..{raw}') IS NOT NULL DROP TABLE {raw};"))
    con.execute(_t(f"SELECT TOP 0 INVENTORY_ID INTO {raw} FROM {GFC};"))
    union_all = "\nUNION ALL\n".join(selects)
    con.execute(_t(f"INSERT INTO {raw} (INVENTORY_ID)\n{union_all};"))
    con.execute(_t(
        f"INSERT INTO {temp} (INVENTORY_ID) "
        f"SELECT DISTINCT INVENTORY_ID FROM {raw} WHERE INVENTORY_ID IS NOT NULL;"))
    con.execute(_t(f"CREATE CLUSTERED INDEX ix_{temp.strip('#')} ON {temp}(INVENTORY_ID);"))
    con.execute(_t(f"DROP TABLE {raw};"))
    n = con.execute(_t(f"SELECT COUNT(*) FROM {temp}")).scalar()
    return int(n or 0)


# The shared CASE expression — the single definition of catalog/promote readiness.
# hc = has pending cat_* rows; hd = represented in dv_* (promoted).
_CASE = """
    CASE
        WHEN g.CATALOG_READINESS = 'SKIPPED' THEN 'SKIPPED'
        WHEN hc.INVENTORY_ID IS NOT NULL THEN 'CATALOGED'
        WHEN hd.INVENTORY_ID IS NOT NULL THEN 'PROMOTED'
        WHEN g.CATALOG_READINESS = 'CATALOGED'
             THEN CASE WHEN NULLIF(LTRIM(RTRIM(g.MATCHED_UWI)), '') IS NOT NULL
                       THEN 'READY' ELSE 'NEEDS_UWI' END
        ELSE g.CATALOG_READINESS
    END
"""

_FROM = f"""
    FROM {GFC} g
    LEFT JOIN #have_cat hc ON hc.INVENTORY_ID = g.INVENTORY_ID
    LEFT JOIN #have_dv  hd ON hd.INVENTORY_ID = g.INVENTORY_ID
"""


def reconcile_readiness(engine, dialect: str = "mssql", dry: bool = False,
                        log=print) -> dict:
    """Reconcile CATALOG_READINESS to row reality. Returns a summary dict.

    dry=True previews the transitions (old -> new counts) without writing.
    """
    if (dialect or "mssql").lower() != "mssql":
        log(f"[readiness] reconcile skipped — dialect '{dialect}' not supported")
        return {"skipped": True, "dialect": dialect}

    from sqlalchemy import text as _t

    with engine.begin() as con:
        cat_tabs, dv_tabs = _discover(con)

        n_cat = _build_distinct_temp(con, "#have_cat", cat_tabs)

        # dv side: detail tables by INVENTORY_ID, PLUS header-only wells whose
        # MATCHED_UWI already exists in dv_well (no detail rows to key on). The
        # MATCHED_UWI<->dv_well.UWI join is a PK seek (both bare-14), not a scan.
        header_promoted = (
            f"SELECT g.INVENTORY_ID FROM {GFC} g "
            f"JOIN {DV_SCHEMA}.dv_well w ON w.UWI = g.MATCHED_UWI "
            f"WHERE NULLIF(LTRIM(RTRIM(g.MATCHED_UWI)), '') IS NOT NULL"
        )
        n_dv = _build_distinct_temp(con, "#have_dv", dv_tabs,
                                    extra_selects=[header_promoted])

        # Preview: what would change, grouped by old -> new.
        preview = con.execute(_t(f"""
            SELECT g.CATALOG_READINESS AS old_val,
                   {_CASE} AS new_val,
                   COUNT(*) AS n
            {_FROM}
            GROUP BY g.CATALOG_READINESS, {_CASE}
            HAVING g.CATALOG_READINESS <> {_CASE}
            ORDER BY n DESC
        """)).fetchall()

        transitions = [{"from": r.old_val, "to": r.new_val, "n": int(r.n)}
                       for r in preview]
        changed = sum(t["n"] for t in transitions)

        for t in transitions:
            log(f"[readiness] {t['from'] or '(null)'} -> {t['to']}: {t['n']:,}")

        if dry:
            log(f"[readiness] DRY RUN — {changed:,} row(s) would change "
                f"(cat sources={len(cat_tabs)}, dv sources={len(dv_tabs)}, "
                f"pending inv={n_cat:,}, promoted inv={n_dv:,})")
            return {"dry": True, "changed": changed, "transitions": transitions,
                    "cat_sources": len(cat_tabs), "dv_sources": len(dv_tabs)}

        con.execute(_t(f"UPDATE g SET g.CATALOG_READINESS = {_CASE} {_FROM} "
                       f"WHERE g.CATALOG_READINESS <> {_CASE}"))

        con.execute(_t("DROP TABLE #have_cat;"))
        con.execute(_t("DROP TABLE #have_dv;"))

    log(f"[readiness] reconciled — {changed:,} row(s) updated")
    return {"dry": False, "changed": changed, "transitions": transitions,
            "cat_sources": len(cat_tabs), "dv_sources": len(dv_tabs)}


# Optional CLI: python catalog_readiness.py [--apply]
def main() -> int:
    import argparse
    from dataview.file_catalog.build_catalog_mirror import connect  # reuse the project's connector
    ap = argparse.ArgumentParser(description="Reconcile CATALOG_READINESS to row reality.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    a = ap.parse_args()
    # build_catalog_mirror.connect returns a pyodbc connection; wrap minimally.
    from sqlalchemy import create_engine
    # If the project exposes an engine factory, prefer that; otherwise the caller
    # should import reconcile_readiness directly with their engine.
    try:
        from dataview.core.db import make_engine  # type: ignore
        engine = make_engine()
    except Exception:
        print("No engine factory found; import reconcile_readiness(engine) directly.")
        return 2
    reconcile_readiness(engine, dialect="mssql", dry=not a.apply, log=print)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
