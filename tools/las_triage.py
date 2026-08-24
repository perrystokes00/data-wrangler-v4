r"""
las_triage.py — scan a folder of LAS files for valid format + a valid UWI and
sort into two piles.

  good/  = readable LAS (parses, has curves) with a valid API/UWI
  bad/   = unreadable / not a LAS, OR no valid UWI in the header

Writes las_triage_report.csv (file, status, reason, uwi_found) so you can see
exactly why each bad file was rejected.

  py las_triage.py --src "C:\LAS"                      # copy -> C:\LAS\_triage\good|bad
  py las_triage.py --src "C:\LAS" --out "D:\sorted"    # custom output root
  py las_triage.py --src "C:\LAS" --move               # MOVE instead of copy
  py las_triage.py --src "C:\LAS" --report-only        # scan + report, don't touch files
  py las_triage.py --src "C:\LAS" --workers 8
"""
import os, re, csv, shutil, argparse
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor

import sys
# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. Needed since this script started reading LAS through
# dataview.file_catalog.las_reader. app_v4.py does the same insert.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

API_RE = re.compile(r"\d{10,14}")

def valid_uwi(uwi):
    """First 10 digits form a plausible API (state code 01-62)."""
    if not uwi:
        return False
    d = "".join(c for c in str(uwi) if c.isdigit())
    if len(d) < 10:
        return False
    try:
        return 1 <= int(d[:2]) <= 62
    except ValueError:
        return False

def _hdr_uwi(las):
    # common UWI/API mnemonics first, then any API-like run in the ~Well section
    for k in ("UWI", "API", "APINUM", "API_NUMBER", "APINO", "APINUMBER", "APIN"):
        try:
            v = str(las.well[k].value).strip()
            if v:
                return v
        except Exception:
            pass
    try:
        for item in las.well:
            m = API_RE.search(str(item.value).replace("-", "").replace(" ", ""))
            if m:
                return m.group()
    except Exception:
        pass
    return ""

def _hdr_wellname(las):
    for k in ("WELL", "WELL_NAME", "WELLNAME", "LEASE"):
        try:
            v = str(las.well[k].value).strip()
            if v:
                return v
        except Exception:
            pass
    return ""


def check_one(path):
    """(path, status, reason, uwi) — status in good / bad_format / bad_uwi."""
    try:
        import lasio
        from dataview.file_catalog.las_reader import read_las
        las = read_las(path, ignore_data=True)
    except Exception as e:
        msg = (str(e).splitlines()[0] if str(e) else "unreadable")[:90]
        return (path, "bad_format", msg, "", "")
    try:
        ncurves = len(las.curves)
    except Exception:
        ncurves = 0
    if ncurves == 0:
        return (path, "bad_format", "no curves / not a LAS", "", "")
    uwi = _hdr_uwi(las)
    wn  = _hdr_wellname(las)
    if not valid_uwi(uwi):
        return (path, "bad_uwi", f"no valid UWI (found {uwi!r})", uwi, wn)
    return (path, "good", "", uwi, wn)

def uniq(dest_dir, name):
    p = os.path.join(dest_dir, name)
    if not os.path.exists(p):
        return p
    stem, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(os.path.join(dest_dir, f"{stem}_{i}{ext}")):
        i += 1
    return os.path.join(dest_dir, f"{stem}_{i}{ext}")

def main():
    ap = argparse.ArgumentParser(description="triage LAS files into good/bad")
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--move", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()

    out = a.out or os.path.join(a.src, "_triage")
    good_dir, bad_dir = os.path.join(out, "good"), os.path.join(out, "bad")
    for d in (good_dir, bad_dir):
        Path(d).mkdir(parents=True, exist_ok=True)

    files = [str(p) for p in Path(a.src).rglob("*.las")
             if os.path.join("_triage", "") not in str(p)]
    print(f"{len(files):,} .las files under {a.src}", flush=True)
    if not files:
        return

    results = []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(check_one, files), 1):
            results.append(r)
            if i % 200 == 0:
                print(f"  scanned {i:,}/{len(files):,}", flush=True)

    good = [r for r in results if r[1] == "good"]
    badf = [r for r in results if r[1] == "bad_format"]
    badu = [r for r in results if r[1] == "bad_uwi"]

    if not a.report_only:
        op = shutil.move if a.move else shutil.copyfile
        for path, status, reason, uwi, wn in results:
            dest = good_dir if status == "good" else bad_dir
            try:
                op(path, uniq(dest, os.path.basename(path)))
            except Exception as e:
                print(f"  (couldn't place {os.path.basename(path)}: {str(e)[:50]})")

    rep = os.path.join(out, "las_triage_report.csv")
    with open(rep, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file_name", "status", "uwi", "well_name", "reason", "path"])
        for path, status, reason, uwi, wn in results:
            w.writerow([os.path.basename(path), status, uwi, wn, reason, path])

    print(f"\n=== LAS triage ===")
    print(f"  GOOD (format + valid UWI) : {len(good):,}")
    print(f"  BAD  unreadable/format    : {len(badf):,}")
    print(f"  BAD  no valid UWI         : {len(badu):,}")
    verb = "moved" if a.move else ("(report only, not copied)" if a.report_only else "copied")
    print(f"  {verb}: good -> {good_dir}")
    print(f"          bad  -> {bad_dir}")
    print(f"  report -> {rep}")

if __name__ == "__main__":
    main()
