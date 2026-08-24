r"""
las_scan.py — recursively scan every .las under a root and write a report:
  file_name, uwi_or_api, well_name, status(good/bad), reason, folder

  py las_scan.py                                  # default KSGS\LAS Files tree
  py las_scan.py --src "C:\path" --out "C:\r.csv"
  py las_scan.py --src "C:\path" --workers 8
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

ROOT_DEFAULT = r"C:\Users\perry\OneDrive\Documents\KSGS\LAS Files"
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

def check(path):
    """(path, file_name, uwi, well_name, status, reason)."""
    fn = os.path.basename(path)
    try:
        import lasio
        from dataview.file_catalog.las_reader import read_las
        las = read_las(path, ignore_data=True)
    except Exception as e:
        msg = (str(e).splitlines()[0] if str(e) else "unreadable")[:90]
        return (path, fn, "", "", "bad", msg)
    try:
        nc = len(las.curves)
    except Exception:
        nc = 0
    uwi, wn = _hdr_uwi(las), _hdr_wellname(las)
    if nc == 0:
        return (path, fn, uwi, wn, "bad", "no curves / not a LAS")
    if not valid_uwi(uwi):
        return (path, fn, uwi, wn, "bad", f"no valid UWI (found {uwi!r})")
    return (path, fn, uwi, wn, "good", "")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=ROOT_DEFAULT)
    ap.add_argument("--out", default=None)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    root = a.src
    files = [str(p) for p in Path(root).rglob("*.las")
             if os.path.join("_triage", "") not in str(p)]
    print(f"{len(files):,} .las files under {root} (recursive)", flush=True)
    if not files:
        return

    rows = []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(check, files), 1):
            path, fn, uwi, wn, status, reason = r
            folder = os.path.relpath(os.path.dirname(path), root)
            rows.append((fn, uwi, wn, status, reason, folder))
            if i % 500 == 0:
                print(f"  scanned {i:,}/{len(files):,}", flush=True)

    out = a.out or os.path.join(root, "las_report.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file_name", "uwi_or_api", "well_name", "status", "reason", "folder"])
        w.writerows(rows)

    ng = sum(1 for r in rows if r[3] == "good")
    print(f"\n{len(rows):,} files: {ng:,} good, {len(rows)-ng:,} bad")
    print(f"report -> {out}")

if __name__ == "__main__":
    main()
