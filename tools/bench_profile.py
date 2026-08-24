r"""
bench_profile.py — split the per-file capture cost into PARSE vs DB-WRITE, to see
which dominates (and therefore which lever matters). Single-thread on N files.
Synthetic 15999 UWIs; run bench_capture_parallel.py --cleanup after.

  py bench_profile.py --src "C:\...\2020\_triage\good" --n 100
"""
import sys, os, time, argparse, urllib.parse as _u
from pathlib import Path
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "modules")); sys.path.insert(0, HERE)
from sqlalchemy import create_engine

# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. Needed since this script started reading LAS through
# dataview.file_catalog.las_reader. app_v4.py does the same insert.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONN = (r"DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost\SQLEXPRESS;"
        r"DATABASE=DataView_Demo;Trusted_Connection=yes;Encrypt=no")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"C:\Users\perry\OneDrive\Documents\KSGS\LAS Files\2020\_triage\good")
    ap.add_argument("--n", type=int, default=100)
    a = ap.parse_args()

    import lasio, worker_core
    from dataview.file_catalog.las_reader import read_las
    eng = create_engine("mssql+pyodbc:///?odbc_connect=" + _u.quote_plus(CONN),
                        fast_executemany=True)
    files = [str(p) for p in Path(a.src).rglob("*.las")][:a.n]
    if not files:
        print("no files"); return

    def say(*_):
        pass

    t_parse = t_total = 0.0
    ncurves = 0
    for i, fp in enumerate(files, 1):
        # PARSE: header-only read (what capture should be doing)
        p0 = time.time()
        try:
            las = read_las(fp, ignore_data=True)
            ncurves += len(las.curves)
        except Exception:
            pass
        t_parse += time.time() - p0
        # TOTAL: the real capture (parse + all DB writes)
        uwi = "15999" + f"{i:05d}" + "0000"
        w0 = time.time()
        try:
            worker_core._do_las(eng, fp, uwi, None, say)
        except Exception:
            pass
        t_total += time.time() - w0

    n = len(files)
    parse_ms = t_parse / n * 1000
    total_ms = t_total / n * 1000
    write_ms = max(0.0, total_ms - parse_ms)
    print(f"\nprofiled {n} files (avg per file):")
    print(f"  parse (header read) : {parse_ms:6.0f} ms")
    print(f"  total capture       : {total_ms:6.0f} ms")
    print(f"  => DB write portion : {write_ms:6.0f} ms  ({100*write_ms/total_ms:.0f}% of the time)")
    print(f"  avg curves/file     : {ncurves/n:.1f}")
    print()
    if write_ms > parse_ms:
        print("WRITE-bound: DB writes dominate. Parallel workers contend on the SQL")
        print("Express log, so adding cores barely helps. The lever is write throughput:")
        print("  - stream curve rows via a single BCP writer (not per-worker inserts)")
        print("  - or batch many files per transaction / minimal-logged bulk load")
    else:
        print("PARSE-bound: parallelism SHOULD scale. If it isn't, the write path is")
        print("serializing anyway — check for per-row inserts in the curve write.")
    print("\ncleanup: py bench_capture_parallel.py --cleanup")

if __name__ == "__main__":
    main()
