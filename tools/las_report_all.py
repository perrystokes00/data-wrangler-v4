r"""
las_report_all.py — report on ALL .las under a root, including files already split
into good/ and bad/ folders. Status is taken from the folder each file is in
(a path part named good/bad, case-insensitive); uwi/api + well_name are pulled
from the header; reason is filled for bad files.

  py las_report_all.py                                  # default KSGS\LAS Files
  py las_report_all.py --src "C:\path" --out "C:\r.csv"
  py las_report_all.py --workers 8
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

def _hdr_latlon(las):
    """Pull LAT/LON from the ~Well (or ~Parameter) section; return (lat, lon) as
    strings ('' if absent). Tries common LAS mnemonics."""
    def get(keys):
        for k in keys:
            for sect in (las.well, getattr(las, "params", [])):
                try:
                    v = str(sect[k].value).strip()
                    if v and v not in ("0", "0.0"):
                        return v
                except Exception:
                    pass
        return ""
    lat = get(("LATI", "LAT", "LATITUDE", "SLAT", "YCOORD", "Y"))
    lon = get(("LONG", "LON", "LONGITUDE", "SLON", "XCOORD", "X"))
    return lat, lon


def check(path):
    """(file_name, uwi, well_name, status, reason). Status from folder if the path
    has a good/bad component; else derived from the check."""
    fn = os.path.basename(path)
    parts = {p.lower() for p in Path(path).parts}
    folder_status = "good" if "good" in parts else ("bad" if "bad" in parts else "")
    try:
        import lasio
        from dataview.file_catalog.las_reader import read_las
        las = read_las(path, ignore_data=True)
    except Exception as e:
        msg = (str(e).splitlines()[0] if str(e) else "unreadable")[:90]
        return (fn, "", "", "", "", folder_status or "bad", msg if (folder_status != "good") else "")
    try:
        nc = len(las.curves)
    except Exception:
        nc = 0
    uwi, wn = _hdr_uwi(las), _hdr_wellname(las)
    lat, lon = _hdr_latlon(las)
    if nc == 0:
        chk = "no curves / not a LAS"
    elif not valid_uwi(uwi):
        chk = f"no valid UWI (found {uwi!r})"
    else:
        chk = ""
    status = folder_status or ("good" if not chk else "bad")
    reason = "" if status == "good" else (chk or "in bad folder")
    return (fn, uwi, wn, lat, lon, status, reason)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=ROOT_DEFAULT)
    ap.add_argument("--out", default=None)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    root = a.src
    files = [str(p) for p in Path(root).rglob("*.las")]   # ALL — no skip
    print(f"{len(files):,} .las files under {root} (recursive, incl. good/bad)", flush=True)
    if not files:
        return

    rows = []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for i, (fn, uwi, wn, lat, lon, status, reason) in enumerate(ex.map(check, files), 1):
            folder = os.path.relpath(os.path.dirname(files[i-1]), root)
            rows.append((fn, uwi, wn, lat, lon, status, reason, folder))
            if i % 500 == 0:
                print(f"  scanned {i:,}/{len(files):,}", flush=True)

    out = a.out or os.path.join(root, "las_report_all.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file_name", "uwi_or_api", "well_name", "latitude", "longitude", "status", "reason", "folder"])
        w.writerows(rows)

    ng = sum(1 for r in rows if r[5] == "good")
    print(f"\n{len(rows):,} files: {ng:,} good, {len(rows)-ng:,} bad")
    print(f"report -> {out}")

if __name__ == "__main__":
    main()
