"""
extend_synonyms_round3.py — DV_WELL_LOG pack (LAS-aware) + coverage report.

    py extend_synonyms_round3.py                 # seed + coverage
    py extend_synonyms_round3.py DV_SEIS_LINE    # also dump those columns

Round 1 guessed DV_WELL_LOG had log_name/log_top_depth/log_base_depth/
run_number; the live schema said log_id/log_type/run_num/top_depth/
base_depth/null_value/file_path/file_format/catalog_id. This pack is written
against those, and speaks LAS: a LAS header calls them STRT, STOP, NULL,
SRVC, RUN, DATE — which is exactly what a source file will carry.

Ends with a COVERAGE REPORT: every target table, its mappable column count,
how many have at least one synonym, and the gap. That report picks the next
round instead of guesswork.
"""

import sys
import time
import traceback

SERVER = r"localhost\SQLEXPRESS"
DB = "DataView_Demo"
SCHEMA = "dataview"

DUMP = [a for a in sys.argv[1:] if not a.startswith("-")]


PACK3 = {
    "DV_WELL_LOG": {
        "log_id": ["log_id", "log_key", "curve_set_id", "logid",
                   "log_identifier"],
        "log_type": ["log_type", "type", "log_name", "logname", "log_desc",
                     "tool_type", "log_suite", "suite", "curve_set",
                     "measurement_type", "logtype"],
        "run_num": ["run", "run_num", "run_no", "run_number", "runnumber",
                    "rn", "run_id"],
        "log_date": ["log_date", "date", "run_date", "logged_date",
                     "date_logged", "log_dt", "acquisition_date"],
        "service_company_ba_id": ["service_company", "srvc", "service",
                                  "logging_company", "contractor", "vendor",
                                  "srvc_company", "service_co",
                                  "wireline_company"],
        "depth_datum": ["depth_datum", "datum", "elev_ref",
                        "reference_datum", "depth_reference"],
        "top_depth": ["top_depth", "strt", "start_depth", "start", "top",
                      "first_depth", "depth_start", "log_top", "strt_depth",
                      "from_depth"],
        "base_depth": ["base_depth", "stop", "stop_depth", "end_depth",
                       "bottom", "base", "last_depth", "depth_stop",
                       "log_base", "to_depth", "td_log"],
        "depth_ouom": ["depth_ouom", "depth_uom", "depth_units",
                       "depth_unit", "units", "uom"],
        "null_value": ["null_value", "null", "nullvalue", "absent_value",
                       "no_data", "nodata", "missing_value", "undefined",
                       "absent"],
        "file_path": ["file_path", "filepath", "path", "filename",
                      "file_name", "file", "location", "uri", "full_path"],
        "file_format": ["file_format", "format", "file_type", "filetype",
                        "ext", "extension"],
        "catalog_id": ["catalog_id", "catalog_key", "file_catalog_id",
                       "doc_id", "document_id"],
    },
}


def say(*args):
    sys.stdout.write(" ".join(str(a) for a in args) + "\n")
    sys.stdout.flush()


def main():
    from dataview.import_data.bulk_dir_loader import get_engine
    from dataview.import_data import synonym_store as syn
    from sqlalchemy import text

    eng = get_engine(SERVER, DB)
    say("=" * 72)
    say("Synonym pack — round 3 (DV_WELL_LOG, LAS-aware) + coverage")
    say("=" * 72)

    say("\n[1/3] seeding PACK3…")
    t0 = time.time()
    rep = syn.seed_synonyms(eng, SCHEMA, pack=PACK3, by="SEED3")
    say(f"      candidates : {rep['candidates']:,}")
    say(f"      inserted   : {rep['inserted']:,} · {time.time() - t0:.1f}s")
    for c in rep["conflicts"][:12]:
        say(f"      conflict   : {c}")
    for tbl, col, why in rep["dropped"]:
        say(f"      DROPPED    : {tbl}.{col} — {why}")
    if not rep["dropped"]:
        say("      dropped    : none — every column in PACK3 exists")

    say("\n[2/3] coverage — mappable columns with at least one synonym:")
    sysfilter = """
        LOWER(a.target_column) NOT IN ('row_created_by','row_created_date',
            'row_changed_by','row_changed_date','active_ind','source',
            'inventory_id')
        AND LOWER(a.target_column) NOT LIKE 'h3[_]%'
        AND LOWER(a.target_column) NOT LIKE 'geog%'
        AND LOWER(a.target_column) NOT LIKE '%[_]hash'
    """
    with eng.connect() as cx:
        rows = cx.execute(text(f"""
            SELECT a.target_table,
                   COUNT(*) AS mappable,
                   SUM(CASE WHEN s.n > 0 THEN 1 ELSE 0 END) AS covered,
                   SUM(ISNULL(s.n, 0)) AS synonyms
            FROM {SCHEMA}.dv_target_attribute a
            OUTER APPLY (
                SELECT COUNT(*) AS n FROM {SCHEMA}.dv_column_synonym c
                WHERE c.target_table = a.target_table
                  AND c.target_column = a.target_column
                  AND c.active_ind = 'Y') s
            WHERE {sysfilter}
              AND EXISTS (SELECT 1 FROM {SCHEMA}.dv_column_synonym x
                          WHERE x.target_table = a.target_table)
            GROUP BY a.target_table
            ORDER BY a.target_table""")).fetchall()
        say(f"      {'TABLE':30s} {'MAPPABLE':>9} {'COVERED':>8} "
            f"{'SYNONYMS':>9}  GAPS")
        for tt, mappable, covered, syns in rows:
            say(f"      {tt:30s} {mappable:9d} {covered:8d} {syns:9d}"
                f"  {mappable - covered}")

        say("\n      columns with NO synonym yet (in seeded tables):")
        gaps = cx.execute(text(f"""
            SELECT a.target_table, a.target_column
            FROM {SCHEMA}.dv_target_attribute a
            WHERE {sysfilter}
              AND EXISTS (SELECT 1 FROM {SCHEMA}.dv_column_synonym x
                          WHERE x.target_table = a.target_table)
              AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.dv_column_synonym c
                              WHERE c.target_table = a.target_table
                                AND c.target_column = a.target_column
                                AND c.active_ind = 'Y')
            ORDER BY a.target_table, a.ordinal""")).fetchall()
        if not gaps:
            say("        none — every mappable column has a synonym")
        for tt, tc in gaps:
            say(f"        {tt}.{tc}")

        say("\n      target tables with NO pack at all (top 25 by columns):")
        none_yet = cx.execute(text(f"""
            SELECT TOP 25 a.target_table, COUNT(*) AS cols
            FROM {SCHEMA}.dv_target_attribute a
            WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.dv_column_synonym x
                              WHERE x.target_table = a.target_table)
            GROUP BY a.target_table
            ORDER BY COUNT(*) DESC""")).fetchall()
        for tt, n in none_yet:
            say(f"        {tt:40s} {n:4d} columns")

        for t in DUMP:
            say(f"\n[3/3] COLUMNS OF {t.upper()}:")
            for r in cx.execute(text(f"""
                    SELECT target_column, data_type, max_len, is_nullable
                    FROM {SCHEMA}.dv_target_attribute
                    WHERE target_table = :t ORDER BY ordinal"""),
                    {"t": t.upper()}).fetchall():
                width = f"({r[2]})" if r[2] else ""
                null = "" if r[3] == "Y" else "  NOT NULL"
                say(f"        {r[0]:28s} {r[1]}{width}{null}")

    say("\n" + "=" * 72)
    say("DONE")
    say("=" * 72)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        say("\n!!! FAILED — traceback follows:\n")
        traceback.print_exc()
        sys.exit(1)
