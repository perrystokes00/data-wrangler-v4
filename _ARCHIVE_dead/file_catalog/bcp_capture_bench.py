r"""
bcp_capture_bench.py — prove the throughput ceiling of the BCP capture design:
  PHASE 1  parallel-parse LAS files (workers return curve rows) — CPU-bound, scales
  PHASE 2  bulk-load ALL curve rows via BCP (TABLOCK, minimal logging) — one stream

Compares against the current per-file executemany path (your 6.14 files/sec).
Safe: writes to a scratch stg table it drops afterward.

  py bcp_capture_bench.py --n 300 --workers 6
  py bcp_capture_bench.py --src "C:\...\good" --n 1000 --workers 6
"""
import sys, os, time, csv, subprocess, argparse, tempfile
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

SERVER   = r"localhost\SQLEXPRESS"
DATABASE = "DataView_Demo"
BCP_CANDS = [
    r"C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\170\Tools\Binn\bcp.exe",
    "bcp",
]

def _clean(s):
    return (s or "").replace("|", " ").replace("\r", " ").replace("\n", " ").strip()

def parse_curves(arg):
    """Worker: parse ONE LAS header (ignore_data), return curve-row tuples."""
    fp, uwi = arg
    try:
        import lasio
        las = lasio.read(fp, ignore_data=True)
    except Exception:
        return []
    def wv(*keys):
        for k in keys:
            try:
                v = str(las.well[k].value).strip()
                if v:
                    return v
            except Exception:
                pass
        return ""
    d_start, d_stop = wv("STRT", "START"), wv("STOP")
    logid = (uwi + "-LAS") if uwi else ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for c in las.curves:
        mnem = (getattr(c, "mnemonic", "") or "").strip()
        if not mnem:
            continue
        rows.append((uwi, logid, mnem[:40], mnem,
                     _clean(getattr(c, "descr", "")), _clean(getattr(c, "unit", "")),
                     d_start, d_stop, "Y", "DataWrangler", now))
    return rows

def find_bcp():
    for b in BCP_CANDS:
        if b == "bcp" or os.path.exists(b):
            return b
    return "bcp"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"C:\Users\perry\OneDrive\Documents\KSGS\LAS Files\2020\_triage\good")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--target", type=int, default=7326)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--batch", type=int, default=10000)
    a = ap.parse_args()

    files = [str(p) for p in Path(a.src).rglob("*.las")][:a.n]
    if not files:
        print("no files under", a.src); return
    work = [(fp, "15999" + f"{i:05d}" + "0000") for i, fp in enumerate(files, 1)]

    # PHASE 1 — parallel parse
    print(f"[parse]  {len(files)} files @ {a.workers} workers …", flush=True)
    t0 = time.time()
    all_rows = []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for rows in ex.map(parse_curves, work):
            all_rows.extend(rows)
    t_parse = time.time() - t0
    print(f"         {len(all_rows):,} curve rows in {t_parse:.1f}s "
          f"({len(all_rows)/t_parse:,.0f} rows/s)")

    # write CSV (pipe-delimited, \n rows to match bcp -c)
    tmp = os.path.join(tempfile.gettempdir(), "bcp_curve_bench.csv")
    t0 = time.time()
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="|", lineterminator="\n")
        w.writerows(all_rows)
    t_csv = time.time() - t0

    # scratch staging table
    import pyodbc
    cn = pyodbc.connect(
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={SERVER};DATABASE={DATABASE};"
        f"Trusted_Connection=yes;Encrypt=no", autocommit=True)
    cur = cn.cursor()
    cur.execute("IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg')")
    cur.execute("IF OBJECT_ID('stg.bcp_curve_bench') IS NOT NULL DROP TABLE stg.bcp_curve_bench")
    cur.execute("""CREATE TABLE stg.bcp_curve_bench(
        UWI varchar(255), LOG_ID varchar(255), CURVE_ID varchar(255), MNEMONIC varchar(255),
        CURVE_DESCRIPTION varchar(4000), CURVE_UNIT varchar(255),
        TOP_DEPTH varchar(255), BASE_DEPTH varchar(255),
        ACTIVE_IND varchar(10), ROW_CREATED_BY varchar(255), ROW_CREATED_DATE varchar(255))""")

    # PHASE 2 — BCP bulk load
    bcp = find_bcp()
    print(f"[bcp]    {len(all_rows):,} rows -> stg.bcp_curve_bench (TABLOCK, batch {a.batch:,}) …", flush=True)
    errf = os.path.join(tempfile.gettempdir(), "bcp_curve_bench.err")
    cmd = [bcp, "stg.bcp_curve_bench", "in", tmp, "-c", "-t", "|", "-r", "0x0a",
           "-S", SERVER, "-d", DATABASE, "-T", "-b", str(a.batch), "-h", "TABLOCK",
           "-m", "10", "-e", errf]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    t_bcp = time.time() - t0
    # bcp reports success even when it copies 0 rows — always show its output
    print("         bcp says:", " ".join((r.stdout or "").split())[:200])
    if r.stderr and r.stderr.strip():
        print("         bcp err:", " ".join(r.stderr.split())[:200])
    loaded = cur.execute("SELECT COUNT(*) FROM stg.bcp_curve_bench").fetchone()[0]
    if loaded == 0:
        with open(tmp, encoding="utf-8", errors="replace") as _f:
            sample = [next(_f, "").rstrip() for _ in range(2)]
        print("         (0 loaded) first CSV lines:")
        for ln in sample:
            print("           ", ln[:120])
        try:
            with open(errf, encoding="utf-8", errors="replace") as _e:
                head = _e.read(500).strip()
            if head:
                print("         bcp errfile:", head[:400])
        except Exception:
            pass
        cur.execute("IF OBJECT_ID('stg.bcp_curve_bench') IS NOT NULL DROP TABLE stg.bcp_curve_bench")
        return
    print(f"         loaded {loaded:,} rows in {t_bcp:.1f}s ({loaded/max(t_bcp,0.1):,.0f} rows/s)")
    cur.execute("DROP TABLE stg.bcp_curve_bench")

    total = t_parse + t_csv + t_bcp
    fps = len(files) / total
    print(f"\n=== {len(files)} files: parse {t_parse:.1f}s + csv {t_csv:.1f}s + bcp {t_bcp:.1f}s "
          f"= {total:.1f}s ===")
    print(f"    {fps:.1f} files/sec   (vs 6.14 files/sec on the executemany path — "
          f"{fps/6.14:.1f}x)")
    scale = a.target / len(files)
    print(f"\nextrapolation to {a.target:,} files:")
    print(f"    parse {scale*t_parse/60:.1f} min + bcp {scale*t_bcp/60:.1f} min "
          f"≈ {scale*total/60:.1f} min capture")
    print(f"    + wells/logs (~2 rows/file, trivial) + promote ~3-5 min "
          f"→ end-to-end ~{scale*total/60 + 4:.0f} min")
    print("    (bcp has ~fixed startup, so full-scale is usually a bit faster than linear)")

if __name__ == "__main__":
    main()
