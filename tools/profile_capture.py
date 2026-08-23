"""
profile_capture.py  --  measure the capture stage end-to-end, standalone
========================================================================
No app context needed. Makes its own engine, runs the FAST capture, times it
with per-stage reads, then dumps the DMV reports so you can SEE whether the
per-file dv_well SELECT / GLOBAL_FILE_CATALOG UPDATE are gone.

    python tools/profile_capture.py                       # fast path (default)
    python tools/profile_capture.py --old                 # time the ORIGINAL for A/B
    python tools/profile_capture.py --limit 100 --ext .las .pdf

Targets DataView_Demo by default.
"""
from __future__ import annotations
import argparse
import sys

import pyodbc
from sqlalchemy import create_engine
import os

# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataview.import_data.pipeline_profiler import Profiler, analyze


def make_engine(server, database):
    # SQLAlchemy engine over pyodbc (what catalog_rules expects)
    cs = (f"mssql+pyodbc://@{server}/{database}"
          f"?driver=ODBC+Driver+18+for+SQL+Server"
          f"&Trusted_Connection=yes&TrustServerCertificate=yes")
    return create_engine(cs, fast_executemany=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="localhost\\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--ext", nargs="*",
                    default=[".las", ".dlis", ".dlf", ".lis", ".segy", ".sgy"])
    ap.add_argument("--old", action="store_true",
                    help="profile the ORIGINAL score_inventory_batch instead")
    args = ap.parse_args()

    engine = make_engine(args.server, args.database)

    # a separate pyodbc connection just for the profiler's DMV queries
    cs = (f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={args.server};"
          f"DATABASE={args.database};Trusted_Connection=yes;"
          f"TrustServerCertificate=yes")
    pcon = pyodbc.connect(cs)
    pcur = pcon.cursor()
    pcur.execute("SELECT GETDATE()")
    since = pcur.fetchone()[0]

    if args.old:
        from dataview.file_catalog.catalog_rules import score_inventory_batch as cap
        label = "capture_OLD"
    else:
        from score_inventory_batch_fast import score_inventory_batch_fast as cap
        label = "capture_FAST"

    print(f"running {label} on {args.server}/{args.database} "
          f"(limit {args.limit}, ext {args.ext})\n")

    prof = Profiler(pcur)
    with prof.stage(label):
        summary = cap(engine, "mssql", ext_filter=args.ext, limit=args.limit)

    prof.report()
    print("\nsummary:", {k: v for k, v in summary.items() if k != "by_ext"})

    by_ext = summary.get("by_ext") or {}
    if by_ext:
        print("\n=== capture time by file type (slowest type first) ===")
        print(f"  {'ext':<8} {'count':>6} {'total_s':>9} {'avg_s':>8} "
              f"{'max_s':>8}  slowest file")
        for ext, v in by_ext.items():
            print(f"  {ext:<8} {v['n']:>6} {v['total_s']:>9} {v['avg_s']:>8} "
                  f"{v['max_s']:>8}  {v['slowest_file'] or ''}")
    analyze(pcur, since)
    pcon.close()


if __name__ == "__main__":
    main()
