"""
dataview/file_catalog/promotion_lineage.py
==========================================
One definition of "this file's data reached the database", shared by every
report.

WHY THIS EXISTS
---------------
Not every format lands its data the same way, and the per-file stamps on
GLOBAL_FILE_CATALOG only tell part of the story:

  * DOCUMENTS (PDF, Office, WITSML) capture into cat_* and are then lifted into
    dv_* by promote. PROMOTED_AT gets stamped.
  * LOGS (LAS/DLIS/LIS) stage into cat_well_log / cat_well_log_curve and are
    promoted from there, but PROMOTED_AT is never stamped for them — and an
    older loader path wrote dv_well_log(_curve) directly with no cat_ stage at
    all. Both routes are covered: the cat_ table is named, and available()
    drops it if the database doesn't have it.
  * SEISMIC (SEG-Y/P190) merges its survey name into dv_seis_set. Also no
    PROMOTED_AT.

So a report that reads PROMOTED_AT alone says logs and seismic were never
promoted, when in fact their data is in dv_* and queryable. That is the
"misleading report" problem: the files most likely to have worked are the ones
most likely to look like they failed.

The honest test is LINEAGE, not the stamp: every dv_ detail table carries the
INVENTORY_ID of the file its rows came from, so a file is promoted when its
INVENTORY_ID appears in any of them. That test is defined ONCE here.

BEFORE THIS MODULE there were three divergent lists — the scorecard counted
dv_prod_entity and dv_well_dir_srvy_hdr but not dv_well or dv_well_petro_interp;
the per-file stage scorecard counted the opposite pair. Same file, two reports,
two answers. Adding a dv_ table meant remembering both. Now it means editing
LINEAGE.

EVERYTHING IS GUARDED. Tables are probed for existence AND for an INVENTORY_ID
column before they enter a query, so this runs unchanged against a database
that has only some of them, and a missing table narrows the answer rather than
raising.
"""
from __future__ import annotations

from sqlalchemy import text as _t

# --------------------------------------------------------------------------- #
# Scan-side: what the File Catalog does NOT crawl
# --------------------------------------------------------------------------- #
# Delimited and spreadsheet tables belong to the Bulk Tabular Loader, which
# loads them into dv_* with mapping and FK resolution. The File Catalog has no
# extractor for them, so inventorying one produces a row that can never drain —
# it sits in "pending" forever and makes a finished run look unfinished.
#
# Kept as its own name (rather than folded into extract_core) so the exclusion
# is visible where the scan universe is assembled, and so one edit here changes
# both the default scan set and what the Formats-to-scan box will accept.
TABULAR_EXTS = {".csv", ".tsv", ".xlsx", ".xls", ".xlsm", ".xlsb"}

# --------------------------------------------------------------------------- #
# Lineage: (cat_ staging table, dv_ destination table, label)
# --------------------------------------------------------------------------- #
# cat_ is where a document's rows are STAGED by capture; dv_ is where promote
# lifts them. A file is CAPTURED if rows exist in either, PROMOTED if rows
# exist in dv_. Logs and seismic have no cat_ stage — cat_ is None for them and
# only the dv_ side is tested, which is exactly what makes the deep path
# visible.
#
# ORDER IS THE REPORT ORDER: header first, then the detail types.
LINEAGE = (
    ("cat_well",                "dv_well",                "header"),
    ("cat_well_dir_srvy_hdr",   "dv_well_dir_srvy_hdr",   "survey_hdr"),
    ("cat_well_dir_srvy_sta",   "dv_well_dir_srvy_sta",   "survey"),
    ("cat_well_formation_top",  "dv_well_formation_top",  "tops"),
    ("cat_well_dst",            "dv_well_dst",            "welltest"),
    ("cat_well_dst_period",     "dv_well_dst_period",     "welltest_period"),
    ("cat_well_completion",     "dv_well_completion",     "completion"),
    ("cat_prod_entity",         "dv_prod_entity",         "prod_entity"),
    ("cat_prod_volume",         "dv_prod_volume",         "production"),
    ("cat_well_petro_interp",   "dv_well_petro_interp",   "petro"),
    ("cat_well_core",           "dv_well_core",           "core"),
    # Logs: this build STAGES them like any other document — worker_core writes
    # cat_well_log + cat_well_log_curve, and promote lifts them to dv_. An
    # older path wrote dv_ directly and skipped cat_ entirely, which is where
    # "deep path" comes from; both are covered by naming the cat_ table and
    # letting available() drop it when it isn't there. Naming None here instead
    # HIDES staged curve rows and makes a working capture look like it produced
    # nothing but a header.
    ("cat_well_log",            "dv_well_log",            "log"),
    ("cat_well_log_curve",      "dv_well_log_curve",      "curves"),

    # ── ADDED 16 Aug — TEN PAIRS WERE MISSING AND FOUR HELD REAL DATA ───────
    # A pair absent from this tuple is INVISIBLE to every report: the rows
    # capture, promote lifts them, and file_detail says "no detail rows"
    # because it never looks at that table.
    #
    # That cost an evening. A CASING_CEMENT document whose only detail table
    # is casing reported nothing while 308 dv_well_casing rows sat in the
    # database — the document was perfect, shape_loader worked, capture wrote
    # and promote lifted. Measured when the gap was found: stimulation 816,
    # casing 308, petro_zone 245, perforation 64 rows unreportable.
    #
    # The comment above already warns about the HALF version of this (naming
    # None for a cat_ table that exists). Omitting a pair entirely is the same
    # fault with no warning at all — which is why the check belongs in
    # check_mirror_registry, not in a comment. This is the FOURTH list that
    # must agree with the mirror set, alongside MIRROR_TABLES, the mirror
    # tables themselves and promote's dedicated promoters.
    ("cat_well_casing",         "dv_well_casing",         "casing"),
    ("cat_well_stimulation",    "dv_well_stimulation",    "stimulation"),
    ("cat_well_petro_zone",     "dv_well_petro_zone",     "petro_zone"),
    ("cat_well_perforation",    "dv_well_perforation",    "perforation"),
    ("cat_well_core_sample",    "dv_well_core_sample",    "core_sample"),
    # Geometry from documents. Empty today, listed so the first row that
    # arrives is reported rather than discovered months later.
    ("cat_field",               "dv_field",               "field"),
    ("cat_boundary",            "dv_boundary",            "boundary"),
    ("cat_land_tract",          "dv_land_tract",          "lease"),
    ("cat_pipeline",            "dv_pipeline",            "pipeline"),
    ("cat_log_curve",           "dv_log_curve",           "log_curve"),
)

CAT_SCHEMA = "file_catalog"
DV_SCHEMA = "dataview"


# --------------------------------------------------------------------------- #
# "Pending": one definition each, because there used to be six
# --------------------------------------------------------------------------- #
# Nothing stores a pending flag — every consumer re-derived it from the ABSENCE
# of a stamp, and they did not agree. Counted on DataView_Demo, 16 Aug 2026, all
# at the same instant, all answering "how many files are pending?":
#
#     31   _inventory_report_df   the ELSE branch of a display CASE
#     43   _stage_extract         what the extract claim query selects
#    190   run_pipeline._pending  the gate that decides whether to run stages
#  1,319   _already_done_filter   what capture/recognise will claim
#  2,190   promotion_lineage      no INVENTORY_ID in any dv_ table
#  3,876   work_queue             PROC_STATUS null -> the entire catalog
#
# They answer genuinely different questions, so the fix is not one number — it
# is one DEFINITION per question, in one place, imported. Open-coding any of
# these again re-creates the drift; selftest checks for that.
#
# Fragments, not whole queries: each is a WHERE-clause body over an alias, so a
# caller composes it with its own SELECT and its own extra predicates. `{a}` is
# the table alias ('' for an unaliased table — call as .format(a='g.') or
# via the helper below).

# EXTRACT-pending: the file still needs its header parsed. 'S' (deliberately
# skipped) and duplicates are excluded — this is the claim query's predicate.
EXTRACT_PENDING = (
    "({a}HEADER_EXTRACTED IS NULL OR {a}HEADER_EXTRACTED = 'N') "
    "AND ISNULL({a}HEADER_EXTRACTED,'') <> 'S' "
    "AND {a}DUPLICATE_GROUP IS NULL")

# CAPTURE-pending: extracted, but its rows have not been staged into cat_* at
# this content hash. SKIPPED is an instruction, CATALOGED is a result; both stop
# a re-capture. This is _already_done_filter's non-force branch.
#
# HEADER_EXTRACTED='M' is excluded: the file is no longer at the catalogued
# path, so it cannot be captured, and without this the row is re-claimed and
# re-failed on every run forever — capture logs the failure and writes no state,
# so nothing would ever take it out of the queue. It is HELD, not dropped: the
# row keeps its id and its reason and is reconciled by re-scanning the file
# where it now lives. (EXTRACT_PENDING needs no such clause — it claims only
# NULL and 'N', so 'M' already falls outside it.)
CAPTURE_PENDING = (
    "ISNULL({a}CATALOG_READINESS,'') NOT IN ('SKIPPED','CATALOGED') "
    "AND ISNULL({a}HEADER_EXTRACTED,'') <> 'M' "
    "AND ({a}CAPTURED_HASH IS NULL OR {a}CAPTURED_HASH <> {a}FILE_HASH)")

# The RUN gate: is there any work at all for the processing stages? Extract-
# pending, OR capture-eligible-and-not-yet-captured. The second half exists
# because a re-run over an already-inventoried catalog counts zero extract-
# pending and would otherwise skip capture entirely, so nothing ever captures.
PENDING_ANY = (
    "ISNULL({a}FLAG_DELETE,'N') <> 'Y' AND {a}DUPLICATE_GROUP IS NULL "
    "AND ( ({a}HEADER_EXTRACTED IS NULL OR {a}HEADER_EXTRACTED = 'N') "
    "OR ( LOWER({a}FILE_EXT) IN "
    "('.las','.pdf','.xlsx','.xls','.docx','.doc','.xml','.json') "
    "AND ISNULL({a}CATALOG_READINESS,'') NOT IN ('SKIPPED','CATALOGED') "
    "AND ({a}CAPTURED_HASH IS NULL OR {a}CAPTURED_HASH <> {a}FILE_HASH) "
    "AND NOT EXISTS (SELECT 1 FROM file_catalog.cat_well w "
    "WHERE w.INVENTORY_ID = {a}INVENTORY_ID) ) )")

PENDING_PREDICATES = {
    "extract": EXTRACT_PENDING,
    "capture": CAPTURE_PENDING,
    "any": PENDING_ANY,
}


def pending_sql(which: str, alias: str = "") -> str:
    """The named pending predicate, bound to a table alias.

    alias='g' -> 'g.'-qualified; alias='' -> bare column names.
    """
    frag = PENDING_PREDICATES[which]
    return frag.format(a=(alias + "." if alias else ""))


def _has_inventory_id(con, schema, table):
    """True when the table exists AND carries INVENTORY_ID.

    Both halves matter: a table without the column can't be joined on lineage,
    and probing for it is what lets a partially-built database report on the
    tables it does have instead of failing the whole query.
    """
    try:
        return con.execute(_t(
            "SELECT CASE WHEN OBJECT_ID(:o) IS NOT NULL AND EXISTS("
            "SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID(:o) "
            "AND name='INVENTORY_ID') THEN 1 ELSE 0 END"),
            {"o": f"{schema}.{table}"}).scalar() == 1
    except Exception:
        return False


def available(con):
    """[(cat_or_None, dv, label)] narrowed to what this database can answer.

    A dv_ table without INVENTORY_ID is dropped entirely — it cannot attribute
    a row to a file, so counting it would credit files that produced nothing.
    """
    out = []
    for cat, dv, label in LINEAGE:
        if not _has_inventory_id(con, DV_SCHEMA, dv):
            continue
        keep_cat = cat if (cat and _has_inventory_id(con, CAT_SCHEMA, cat)) else None
        out.append((keep_cat, dv, label))
    return out


def seismic_ok(con):
    """Seismic is credited by SURVEY NAME, not INVENTORY_ID lineage — a SEG-Y
    line file is done once its survey reached dv_seis_set. Needs both objects."""
    try:
        return con.execute(_t(
            "SELECT CASE WHEN OBJECT_ID('dataview.dv_seis_set') IS NOT NULL "
            "AND OBJECT_ID('file_catalog.FILE_SEIS_HEADER') IS NOT NULL "
            "THEN 1 ELSE 0 END")).scalar() == 1
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# CTE builder for aggregate reports
# --------------------------------------------------------------------------- #
def promoted_sql(con, alias="g"):
    """SQL fragments crediting deep-path promotion in an aggregate query.

    Returns (cte, join, pred) to be spliced as:

        {cte}SELECT ... SUM(CASE WHEN {alias}.PROMOTED_AT IS NOT NULL {pred}
                                 THEN 1 ELSE 0 END) ...
        FROM file_catalog.GLOBAL_FILE_CATALOG {alias} WITH (NOLOCK)
        {join}GROUP BY ...

    All three are '' when nothing is available, so the caller degrades to a
    plain PROMOTED_AT report rather than breaking.

    NB SQL Server error 130 forbids a subquery inside an aggregate, which is
    why this is a CTE + LEFT JOIN rather than SUM(CASE WHEN EXISTS(...)). The
    DISTINCT keeps each join 1:1 so COUNT(*) is unaffected.
    """
    ctes, joins, preds = [], [], []

    if seismic_ok(con):
        ctes.append("seis_done AS ("
                    "SELECT DISTINCT sh.INVENTORY_ID "
                    "FROM file_catalog.FILE_SEIS_HEADER sh WITH (NOLOCK) "
                    "JOIN dataview.dv_seis_set ss "
                    "ON ss.seis_set_name = sh.SURVEY_NAME)")
        joins.append(f"LEFT JOIN seis_done sd ON sd.INVENTORY_ID = {alias}.INVENTORY_ID ")
        preds.append("OR sd.INVENTORY_ID IS NOT NULL ")

    parts = [f"SELECT INVENTORY_ID FROM {DV_SCHEMA}.{dv} WITH (NOLOCK) "
             f"WHERE INVENTORY_ID IS NOT NULL"
             for _cat, dv, _lbl in available(con)]
    if parts:
        ctes.append("docs_done AS (SELECT DISTINCT INVENTORY_ID FROM ("
                    + " UNION ALL ".join(parts) + ") _u)")
        joins.append(f"LEFT JOIN docs_done dd ON dd.INVENTORY_ID = {alias}.INVENTORY_ID ")
        preds.append("OR dd.INVENTORY_ID IS NOT NULL ")

    cte = ("WITH " + ", ".join(ctes) + " ") if ctes else ""
    return cte, "".join(joins), "".join(preds)


def captured_sql(con, alias="g"):
    """Same shape as promoted_sql, for the CAPTURED test — adds the cat_ side.

    Capture is a SUPERSET of promotion: rows still staged in cat_, plus rows
    already lifted to dv_. Testing only cat_ under-reports (promote drains it),
    and testing only the stamps under-reports the deep path — a LAS file has no
    cat_ rows and no CAPTURED_HASH, yet its curves are in dv_.
    """
    ctes, joins, preds = [], [], []
    avail = available(con)

    parts = [f"SELECT INVENTORY_ID FROM {DV_SCHEMA}.{dv} WITH (NOLOCK) "
             f"WHERE INVENTORY_ID IS NOT NULL"
             for _cat, dv, _lbl in avail]
    parts += [f"SELECT INVENTORY_ID FROM {CAT_SCHEMA}.{cat} WITH (NOLOCK) "
              f"WHERE INVENTORY_ID IS NOT NULL"
              for cat, _dv, _lbl in avail if cat]
    if parts:
        ctes.append("cap_done AS (SELECT DISTINCT INVENTORY_ID FROM ("
                    + " UNION ALL ".join(parts) + ") _u)")
        joins.append(f"LEFT JOIN cap_done cd ON cd.INVENTORY_ID = {alias}.INVENTORY_ID ")
        preds.append("OR cd.INVENTORY_ID IS NOT NULL ")

    if seismic_ok(con):
        ctes.append("seis_cap AS ("
                    "SELECT DISTINCT sh.INVENTORY_ID "
                    "FROM file_catalog.FILE_SEIS_HEADER sh WITH (NOLOCK) "
                    "JOIN dataview.dv_seis_set ss "
                    "ON ss.seis_set_name = sh.SURVEY_NAME)")
        joins.append(f"LEFT JOIN seis_cap sc ON sc.INVENTORY_ID = {alias}.INVENTORY_ID ")
        preds.append("OR sc.INVENTORY_ID IS NOT NULL ")

    cte = ("WITH " + ", ".join(ctes) + " ") if ctes else ""
    return cte, "".join(joins), "".join(preds)


# --------------------------------------------------------------------------- #
# Set-based per-file detail
# --------------------------------------------------------------------------- #
def file_detail(engine, root=None, this_crawl=False, limit=None):
    """Per-file extract/capture/promote with the row counts behind each.

    ONE query. The previous implementation ran two COUNT(*) per lineage table
    per file — 24 round-trips a file, so ~38,000 on a 1,600-file catalog, which
    is why it was gated behind a button and scoped to one crawl. This pivots
    the whole thing server-side: each lineage table is aggregated once by
    INVENTORY_ID and LEFT JOINed, so cost tracks the catalog, not the product
    of catalog and table count.

    Returns a DataFrame: file, type, ext, extract, capture, promote, uwi,
    detail, path.
    """
    import pandas as pd

    with engine.connect() as con:
        avail = available(con)

        sel, joins = [], []
        for i, (cat, dv, label) in enumerate(avail):
            a = f"d{i}"
            joins.append(
                f"LEFT JOIN (SELECT INVENTORY_ID, COUNT(*) AS n FROM {DV_SCHEMA}.{dv} "
                f"WITH (NOLOCK) WHERE INVENTORY_ID IS NOT NULL GROUP BY INVENTORY_ID) "
                f"{a} ON {a}.INVENTORY_ID = g.INVENTORY_ID ")
            sel.append(f"ISNULL({a}.n,0) AS dv_{i}")
            if cat:
                b = f"c{i}"
                joins.append(
                    f"LEFT JOIN (SELECT INVENTORY_ID, COUNT(*) AS n FROM {CAT_SCHEMA}.{cat} "
                    f"WITH (NOLOCK) WHERE INVENTORY_ID IS NOT NULL GROUP BY INVENTORY_ID) "
                    f"{b} ON {b}.INVENTORY_ID = g.INVENTORY_ID ")
                sel.append(f"ISNULL({b}.n,0) AS ct_{i}")
            else:
                sel.append(f"CAST(0 AS int) AS ct_{i}")

        # Seismic joins on survey name, so it can't be an INVENTORY_ID rollup
        # like the rest — a separate flag column keeps it out of the counts
        # while still crediting the file.
        if seismic_ok(con):
            joins.append(
                "LEFT JOIN (SELECT DISTINCT sh.INVENTORY_ID FROM "
                "file_catalog.FILE_SEIS_HEADER sh WITH (NOLOCK) "
                "JOIN dataview.dv_seis_set ss ON ss.seis_set_name = sh.SURVEY_NAME) "
                "sq ON sq.INVENTORY_ID = g.INVENTORY_ID ")
            sel.append("CASE WHEN sq.INVENTORY_ID IS NULL THEN 0 ELSE 1 END AS seis")
        else:
            sel.append("CAST(0 AS int) AS seis")

        where, params = ["1=1"], {}
        if root:
            where.append("g.ROOT_PATH = :root"); params["root"] = root
        if this_crawl:
            where.append("CAST(g.SCAN_DATE AS date) = CAST(GETDATE() AS date)")

        top = f"TOP {int(limit)} " if limit else ""
        sql = _t(f"""
            SELECT {top}g.FILE_NAME, g.INVENTORY_ID, g.FILE_PATH,
                   ISNULL(NULLIF(g.FILE_EXT,''),'(none)') AS ext,
                   NULLIF(LTRIM(RTRIM(g.MATCHED_UWI)),'') AS uwi,
                   g.HEADER_EXTRACTED, wh.REPORT_TYPE,
                   {', '.join(sel)}
            FROM file_catalog.GLOBAL_FILE_CATALOG g WITH (NOLOCK)
            LEFT JOIN file_catalog.FILE_WELL_HEADER wh WITH (NOLOCK)
                   ON wh.INVENTORY_ID = g.INVENTORY_ID
            {''.join(joins)}
            WHERE {' AND '.join(where)}
            ORDER BY g.FILE_NAME
        """)
        raw = con.execute(sql, params).fetchall()

    rows = []
    for r in raw:
        m = r._mapping
        hx = m["HEADER_EXTRACTED"]
        extracted = ("Y" if hx == "Y" else "ERR" if hx == "E"
                     else "skip" if hx == "S" else "N")
        cap = prom = 0
        detail = []
        for i, (_cat, _dv, label) in enumerate(avail):
            n_dv, n_ct = int(m[f"dv_{i}"] or 0), int(m[f"ct_{i}"] or 0)
            if n_dv:
                prom += n_dv; cap += n_dv; detail.append(f"{label}:{n_dv}")
            elif n_ct:
                cap += n_ct; detail.append(f"{label}:{n_ct}(staged)")
        if int(m["seis"] or 0):
            prom += 1; cap += 1; detail.append("seismic:survey")
        rows.append({
            "file": m["FILE_NAME"],
            "type": m["REPORT_TYPE"] or "?",
            "ext": m["ext"],
            "extract": extracted,
            "capture": "Y" if cap else "N",
            "promote": "Y" if prom else "N",
            "uwi": m["uwi"] or "",
            "detail": " ".join(detail) if detail else "no detail rows",
            # Kept last so it doesn't crowd the on-screen grid, but present so
            # an export can turn the file name into a clickable link.
            "path": m["FILE_PATH"] or "",
        })
    import pandas as pd
    return pd.DataFrame(rows)


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Deep-path-aware promotion report (logs and seismic included)")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--root")
    ap.add_argument("--this-crawl", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--csv")
    a = ap.parse_args()

    from dataview.core.schema_introspect import make_engine
    eng = make_engine(a.server, a.database, "ODBC Driver 17 for SQL Server")

    with eng.connect() as con:
        av = available(con)
        print(f"-- lineage tables available: {len(av)}/{len(LINEAGE)}")
        _declared = {l: c for c, _d, l in LINEAGE}
        for cat, dv, label in av:
            if cat:
                note = cat
            elif _declared.get(label) is None:
                note = "(deep path — no cat_ stage by design)"
            else:
                note = f"({_declared[label]} absent — dv_ side only)"
            print(f"   {label:12} dv={dv:24} cat={note}")
        print(f"-- seismic credit: {'on' if seismic_ok(con) else 'off'}")

    df = file_detail(eng, root=a.root, this_crawl=a.this_crawl, limit=a.limit)
    if df.empty:
        print("-- no files in scope")
        return 0
    print(f"\n-- {len(df)} file(s): "
          f"{(df['extract'] == 'Y').sum()} extracted · "
          f"{(df['capture'] == 'Y').sum()} captured · "
          f"{(df['promote'] == 'Y').sum()} promoted")
    print("\n-- by extension --")
    g = df.groupby("ext").agg(
        files=("file", "size"),
        extracted=("extract", lambda s: (s == "Y").sum()),
        captured=("capture", lambda s: (s == "Y").sum()),
        promoted=("promote", lambda s: (s == "Y").sum()))
    print(g.to_string())
    if a.csv:
        df.to_csv(a.csv, index=False)
        print(f"\n-- written to {a.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
