"""Snapshot the catalog/gold row counts so you can compare what the REGULAR
pipeline vs the FAST TRACK pool actually produced from the same corpus.

Usage:
  1. Reset demo data.
  2. Run ONE path (regular pipeline --apply, OR Fast Track run-all).
  3. py tools/compare_extract.py snapshot regular     # saves counts
  4. Reset demo data again.
  5. Run the OTHER path.
  6. py tools/compare_extract.py snapshot fasttrack
  7. py tools/compare_extract.py diff                  # shows differences
"""
import sys, json, os


# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataview.file_catalog import worker_core as w
from sqlalchemy import text

SNAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cmp")
os.makedirs(SNAP_DIR, exist_ok=True)

TABLES = [
    # file catalog
    ("file_catalog", "GLOBAL_FILE_CATALOG"),
    ("file_catalog", "FILE_WELL_HEADER"),
    ("file_catalog", "FILE_SEIS_HEADER"),
    ("file_catalog", "cat_well"),
    ("file_catalog", "cat_well_formation_top"),
    ("file_catalog", "cat_well_completion"),
    ("file_catalog", "cat_well_dir_srvy_hdr"),
    ("file_catalog", "cat_well_dir_srvy_sta"),
    ("file_catalog", "cat_well_log"),
    ("file_catalog", "cat_well_log_curve"),
    ("file_catalog", "cat_prod_entity"),
    ("file_catalog", "cat_prod_volume"),
    # gold layer
    ("dataview", "dv_well"),
    ("dataview", "dv_seis_set"),
    ("dataview", "dv_well_formation_top"),
    ("dataview", "dv_log_curve"),
]

def counts(engine):
    out = {}
    with engine.connect() as c:
        for schema, tbl in TABLES:
            try:
                n = c.execute(text(
                    f"SELECT COUNT(*) FROM {schema}.{tbl}")).scalar()
                out[f"{schema}.{tbl}"] = int(n)
            except Exception as e:
                out[f"{schema}.{tbl}"] = f"err: {str(e)[:40]}"
    return out

def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    cmd = sys.argv[1]
    e = w.make_engine(r"localhost\SQLEXPRESS", "DataView_Demo")
    if cmd == "snapshot":
        label = sys.argv[2] if len(sys.argv) > 2 else "snap"
        data = counts(e)
        with open(os.path.join(SNAP_DIR, f"{label}.json"), "w") as f:
            json.dump(data, f, indent=2)
        print(f"saved snapshot '{label}':")
        for k, v in data.items():
            print(f"  {k:42} {v}")
    elif cmd == "diff":
        a = json.load(open(os.path.join(SNAP_DIR, "regular.json")))
        b = json.load(open(os.path.join(SNAP_DIR, "fasttrack.json")))
        print(f"{'table':42} {'regular':>10} {'fasttrack':>10}  {'match'}")
        print("-" * 78)
        allmatch = True
        for k in a:
            va, vb = a.get(k), b.get(k)
            m = "✓" if va == vb else "✗ DIFFERS"
            if va != vb:
                allmatch = False
            print(f"{k:42} {str(va):>10} {str(vb):>10}  {m}")
        print("-" * 78)
        print("ALL MATCH ✓" if allmatch else "DIFFERENCES FOUND ✗ — "
              "the two paths produced different data")

if __name__ == "__main__":
    main()
