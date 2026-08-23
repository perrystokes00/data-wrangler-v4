r"""
bench_capture_parallel.py — measure REAL multi-core capture throughput using the
same ProcessPoolExecutor path the pipeline uses (engine-per-worker +
worker_core.process_file). Reports actual files/sec at W workers and extrapolates
to a full run — no efficiency guess. Synthetic 15999 UWIs; --cleanup removes them.

  py tools/bench_capture_parallel.py --src "C:\...\2020\_triage\good" --n 300 --workers 6
  py tools/bench_capture_parallel.py --n 300 --workers 8
  py tools/bench_capture_parallel.py --cleanup
"""
import sys, os, time, argparse, urllib.parse as _u
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor


# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CONN = (r"DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost\SQLEXPRESS;"
        r"DATABASE=DataView_Demo;Trusted_Connection=yes;Encrypt=no")
URL = "mssql+pyodbc:///?odbc_connect=" + _u.quote_plus(CONN)

def _worker(arg):
    """Pool worker (mirrors pipeline_run._capture_proc_one): own engine + process_file."""
    url, rec = arg
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    try:
        from sqlalchemy import create_engine
        from dataview.file_catalog import worker_core as wc
        eng = create_engine(url, fast_executemany=True)
        try:
            res = wc.process_file(eng, rec)
            return getattr(res, "status", None)
        finally:
            eng.dispose()
    except Exception as e:
        return f"error:{type(e).__name__}"

def cleanup():
    from sqlalchemy import create_engine, text
    eng = create_engine(URL)
    n = 0
    with eng.begin() as c:
        for t in ("cat_well_log_curve", "cat_well_log", "cat_well"):
            try:
                n += c.execute(text(f"DELETE FROM file_catalog.{t} WHERE UWI LIKE '15999%'")).rowcount or 0
            except Exception as e:
                print(f"  {t}: {e}")
    print(f"cleaned {n} synthetic bench row(s)")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"C:\Users\perry\OneDrive\Documents\KSGS\LAS Files\2020\_triage\good")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--target", type=int, default=7326)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--cleanup", action="store_true")
    a = ap.parse_args()
    if a.cleanup:
        cleanup(); return

    files = [str(p) for p in Path(a.src).rglob("*.las")][:a.n]
    if not files:
        print("no files under", a.src); return
    recs = []
    for i, fp in enumerate(files, 1):
        uwi = "15999" + f"{i:05d}" + "0000"     # synthetic valid UWI, unique per file
        recs.append((URL, {"FILE_PATH": fp, "FILE_NAME": os.path.basename(fp),
                           "FILE_EXT": ".las", "MATCHED_UWI": uwi, "INVENTORY_ID": None}))

    print(f"REAL parallel capture: {len(files)} files @ {a.workers} workers "
          f"(writes cat_* as 15999…)\n", flush=True)
    t0 = time.time()
    ok = err = 0
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for i, st in enumerate(ex.map(_worker, recs), 1):
            if st == "done":
                ok += 1
            else:
                err += 1
                if err <= 3:
                    print(f"  ({st})")
            if i % 50 == 0:
                print(f"  {i}/{len(files)}  {time.time()-t0:.1f}s", flush=True)
    dt = time.time() - t0
    fps = len(files) / dt if dt else 0

    print(f"\ncaptured {ok}/{len(files)} in {dt:.1f}s  →  {fps:.2f} files/sec "
          f"@ {a.workers} workers (MEASURED parallel)")
    if fps:
        est = a.target / fps
        print(f"\nfull {a.target:,} files @ {a.workers} workers: ~{est/60:.1f} min  (capture only, measured)")
        print(f"  + single-pass folds extract in (~same capture time, no 2nd read/write)")
        print(f"  + promote (set-based, scales with wells): ~3-5 min")
        print(f"  => est end-to-end single-pass: ~{est/60 + 4:.0f}-{est/60 + 5:.0f} min")
    print("\nrun 'py tools/bench_capture_parallel.py --cleanup' to remove the synthetic rows.")

if __name__ == "__main__":
    main()
