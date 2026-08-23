r"""
bench_capture.py — measure the REAL capture path (worker_core._do_las) on a sample
of LAS files, incl. the DB writes the scanner never does, and extrapolate to a
full run. Uses synthetic valid UWIs ('15999…') so every file captures fully;
clean them up with --cleanup afterward.

  py tools/bench_capture.py --src "C:\...\LAS Files" --n 300
  py tools/bench_capture.py --n 300 --workers 6 --target 7326
  py tools/bench_capture.py --cleanup          # delete the synthetic 15999… bench rows
"""
import sys, os, time, argparse, urllib.parse as _u
from pathlib import Path


# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sqlalchemy import create_engine, text

CONN = (r"DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost\SQLEXPRESS;"
        r"DATABASE=DataView_Demo;Trusted_Connection=yes;Encrypt=no")
eng = create_engine("mssql+pyodbc:///?odbc_connect=" + _u.quote_plus(CONN))

def cleanup():
    n = 0
    with eng.begin() as c:
        for t in ("cat_well_log_curve", "cat_well_log", "cat_well"):
            try:
                r = c.execute(text(f"DELETE FROM file_catalog.{t} WHERE UWI LIKE '15999%'"))
                n += r.rowcount or 0
            except Exception as e:
                print(f"  {t}: {e}")
    print(f"cleaned up {n} synthetic bench row(s)")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"C:\Users\perry\OneDrive\Documents\KSGS\LAS Files")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--target", type=int, default=7326)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--cleanup", action="store_true")
    a = ap.parse_args()

    if a.cleanup:
        cleanup(); return

    from dataview.file_catalog import worker_core
    files = [str(p) for p in Path(a.src).rglob("*.las")][:a.n]
    if not files:
        print("no files"); return
    print(f"benchmarking REAL capture on {len(files)} files (writes to cat_* as 15999…)\n", flush=True)

    def say(*_):  # silence per-file logging
        pass

    ok = fail = 0
    t0 = time.time()
    for i, fp in enumerate(files, 1):
        uwi = "15999" + f"{i:05d}" + "0000"      # synthetic valid UWI, unique per file
        try:
            r = worker_core._do_las(eng, fp, uwi, None, say)
            st = getattr(r, "status", "done")
            ok += 1 if st in ("done",) else 0
            fail += 0 if st in ("done",) else 1
        except Exception as e:
            fail += 1
            if fail <= 3:
                print(f"  err {os.path.basename(fp)}: {str(e)[:70]}")
        if i % 50 == 0:
            print(f"  {i}/{len(files)}  {time.time()-t0:.1f}s", flush=True)
    dt = time.time() - t0
    fps = len(files) / dt if dt else 0

    print(f"\ncaptured {ok}/{len(files)} in {dt:.1f}s  →  {fps:.2f} files/sec (single-thread)")
    print(f"per-file: {dt/len(files)*1000:.0f} ms\n")
    print(f"extrapolation to {a.target:,} files:")
    for w, eff, label in ((1, 1.0, "1 worker (serial)"),
                          (a.workers, 0.65, f"{a.workers} workers (~65% eff, DB contention)")):
        est = a.target / (fps * w * eff) if fps else 0
        print(f"  {label:38} ~{est/60:.1f} min")
    print("\n+ extract stage (2nd header read + FILE_WELL_HEADER write) unless --single-pass;")
    print("  single-pass ~= this number; two-pass ~= this + ~50-70%.  promote adds ~3-5 min.")
    print("\nrun 'py tools/bench_capture.py --cleanup' to remove the synthetic bench rows.")

if __name__ == "__main__":
    main()
