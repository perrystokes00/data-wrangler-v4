"""
run_fixture_triage.py
Run the two stages the fixture exercises — enrich (resolve UWIs + fill blank
attributes) then triage (set VALUE_TIER + readiness) — against DataView_Demo,
using the CLEAN mini master as the reference instead of the full WELL_MASTER.

This mirrors exactly what the pipeline's _stage_enrich + _stage_triage do, but
isolated: no scan, no extract — it just works on the catalog/header rows that
seed_triage_fixture.sql already put in the database.

Run it from the repo root (the header insert below puts the repo on sys.path,
so the folder you are standing in does not matter):

    python tools/run_fixture_triage.py --dry-run     # safe: computes, writes nothing
    python tools/run_fixture_triage.py               # for real: writes the backfill
"""
import argparse
import types
import urllib.parse
import os
import sys

# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _engine(server: str, database: str):
    from sqlalchemy import create_engine
    odbc = (f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};"
            f"DATABASE={database};Trusted_Connection=yes")
    return create_engine(
        "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server",   default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--ref",      default="WELL_REF.well_ref.WELL_MASTER_MINI")
    ap.add_argument("--dry-run",  action="store_true",
                    help="enrich computes but does NOT write the backfill")
    args = ap.parse_args()
    apply = not args.dry_run

    try:
        from dataview.file_catalog import enrich_file_headers as en
        from dataview.file_catalog import triage_inventory
    except ImportError as e:
        raise SystemExit(
            f"Can't import the pipeline modules: {e}\n"
            "Run this script from the folder that contains "
            "enrich_file_headers.py and triage_inventory.py.")

    engine = _engine(args.server, args.database)
    print(f"Database : {args.database} on {args.server}")
    print(f"Reference: {args.ref}")
    print(f"Mode     : {'DRY-RUN (no writes)' if not apply else 'APPLY (writes)'}")
    print("-" * 60)

    # ---- Stage A: enrich = resolve missing UWIs + fill blank attributes ----
    print("[enrich] resolve UWIs / fill blank attributes ...")
    a = types.SimpleNamespace(
        server="", database="", odbc_driver="", ref=args.ref, depth_tol=50.0,
        no_well=False, no_seis=False, no_reverse=True,
        dry_run=not apply, report=None, reverse_report=None)
    raw = engine.raw_connection()
    try:
        en.enrich(raw, a, log=lambda m: print("  " + str(m)))
        if apply:
            raw.commit()
        else:
            raw.rollback()
    finally:
        raw.close()

    # ---- Stage B: triage = set VALUE_TIER + readiness ----
    print("[triage] score / tier ...")
    tiers = triage_inventory.run_all_engine(
        engine, ref=args.ref, dry=False, log=lambda m: print("  " + str(m)))
    print("-" * 60)
    print("tier counts:", tiers)
    print("Done.")


if __name__ == "__main__":
    main()
