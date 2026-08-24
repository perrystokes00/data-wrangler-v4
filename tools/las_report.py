r"""
las_report.py — build a report from already-sorted _triage\Good and _triage\bad
folders. For each .las: extract uwi/api + well_name, mark status by the folder it's
in, and give the reason for bad ones.

  py las_report.py                                   # default 2022\_triage
  py las_report.py --root "C:\...\2022\_triage"
  py las_report.py --good "C:\...\Good" --bad "C:\...\bad"
  py las_report.py --root "..." --out "C:\...\report.csv"
"""
import os, re, csv, argparse
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

def extract(path):
    """(file_name, uwi, well_name, reason) — reason non-empty if problematic."""
    fn = os.path.basename(path)
    try:
        import lasio
        from dataview.file_catalog.las_reader import read_las
        las = read_las(path, ignore_data=True)
    except Exception as e:
        return (fn, "", "", (str(e).splitlines()[0] if str(e) else "unreadable")[:90])
    try:
        nc = len(las.curves)
    except Exception:
        nc = 0
    uwi, wn = _hdr_uwi(las), _hdr_wellname(las)
    if nc == 0:
        reason = "no curves / not a LAS"
    elif not valid_uwi(uwi):
        reason = f"no valid UWI (found {uwi!r})"
    else:
        reason = ""
    return (fn, uwi, wn, reason)

def _subdir(root, names):
    for n in names:
        p = os.path.join(root, n)
        if os.path.isdir(p):
            return p
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"C:\Users\perry\OneDrive\Documents\KSGS\LAS Files\2022\_triage")
    ap.add_argument("--good", default=None)
    ap.add_argument("--bad", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    good_dir = a.good or _subdir(a.root, ("Good", "good", "GOOD"))
    bad_dir = a.bad or _subdir(a.root, ("bad", "Bad", "BAD"))

    rows = []
    for status, d in (("good", good_dir), ("bad", bad_dir)):
        if not d or not os.path.isdir(d):
            print(f"({status} folder not found: {d})"); continue
        files = [str(p) for p in Path(d).rglob("*.las")]
        print(f"{status}: {len(files):,} files in {d}", flush=True)
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for fn, uwi, wn, reason in ex.map(extract, files):
                rows.append((fn, uwi, wn, status, reason if status == "bad" else ""))

    base = a.root or os.path.dirname(good_dir or bad_dir or ".")
    out = a.out or os.path.join(base, "las_report.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file_name", "uwi_or_api", "well_name", "status", "reason"])
        w.writerows(rows)
    ng = sum(1 for r in rows if r[3] == "good")
    print(f"\nreport: {len(rows):,} files ({ng:,} good, {len(rows)-ng:,} bad) -> {out}")

if __name__ == "__main__":
    main()
