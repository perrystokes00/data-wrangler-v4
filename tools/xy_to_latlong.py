"""
xy_to_latlong.py — add LATITUDE/LONGITUDE to a spreadsheet from N/E columns.
============================================================================
By default: column 3 = NORTHING, column 4 = EASTING (1-based, as viewed
in Excel). Or name the columns — by header or by 1-based position:

    py xy_to_latlong.py wells.csv
    py xy_to_latlong.py wells.csv --north SURF_N --east SURF_E
    py xy_to_latlong.py wells.xlsx --north 7 --east 8 --epsg 32056
    py xy_to_latlong.py wells.csv --epsg 4267 --out wells_ll.csv

Header matching is case-insensitive. Every other column passes through
untouched — read as TEXT, so 14-digit UWIs survive the trip without
Excel's scientific-notation mangling.

Default CRS is EPSG:32056 (NAD27 / Wyoming State Plane East Central,
US survey feet — Teapot Dome). pyproj knows the zone is defined in feet,
so feed the values exactly as they appear; do NOT pre-convert to meters.

Needs:  pip install pyproj pandas openpyxl
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("infile", help=".csv or .xlsx with northing in col 3, easting in col 4")
    ap.add_argument("--epsg", type=int, default=32056,
                    help="source CRS (default 32056 = NAD27 WY East Central, ft)")
    ap.add_argument("--north", default="3",
                    help="northing column: header name or 1-based position (default 3)")
    ap.add_argument("--east", default="4",
                    help="easting column: header name or 1-based position (default 4)")
    ap.add_argument("--out", default=None,
                    help="output path (default: <infile>_latlong.<ext>)")
    a = ap.parse_args()

    try:
        import pandas as pd
        from pyproj import Transformer
    except ImportError as e:
        print(f"!! pip install pyproj pandas openpyxl  ({e.name} missing)")
        return 2

    ext = os.path.splitext(a.infile)[1].lower()
    if ext in (".xlsx", ".xls", ".xlsm"):
        df = pd.read_excel(a.infile, dtype=str)
    else:
        df = pd.read_csv(a.infile, dtype=str)
    def _pick(spec, what):
        """Resolve a column by header name (case-insensitive) or 1-based
        position. Returns the Series or None (with its own complaint)."""
        s = str(spec).strip()
        lower = {str(c).strip().lower(): c for c in df.columns}
        if s.lower() in lower:
            return df[lower[s.lower()]]
        if s.isdigit():
            i = int(s)
            if 1 <= i <= df.shape[1]:
                return df.iloc[:, i - 1]
            print(f"!! --{what} {s}: file has only {df.shape[1]} column(s)")
            return None
        print(f"!! --{what} {spec!r}: no such header. "
              f"Columns are: {', '.join(map(str, df.columns))}")
        return None

    ncol = _pick(a.north, "north")
    ecol = _pick(a.east, "east")
    if ncol is None or ecol is None:
        return 2
    if ncol.name == ecol.name:
        print(f"!! --north and --east resolve to the same column "
              f"({ncol.name!r})")
        return 2

    north = pd.to_numeric(ncol.str.replace(",", "", regex=False),
                          errors="coerce")
    east = pd.to_numeric(ecol.str.replace(",", "", regex=False),
                         errors="coerce")

    tf = Transformer.from_crs(f"EPSG:{a.epsg}", "EPSG:4326", always_xy=True)
    # transform takes (x, y) = (EASTING, NORTHING); always_xy pins that order.
    lon, lat = tf.transform(east.to_numpy(), north.to_numpy())

    import numpy as np
    lat = np.where(np.isfinite(lat) & (np.abs(lat) <= 90), lat, np.nan)
    lon = np.where(np.isfinite(lon) & (np.abs(lon) <= 180), lon, np.nan)
    df["LATITUDE"] = [None if not (v == v) else round(float(v), 7) for v in lat]
    df["LONGITUDE"] = [None if not (v == v) else round(float(v), 7) for v in lon]

    out = a.out or (os.path.splitext(a.infile)[0] + "_latlong" + ext)
    if out.lower().endswith((".xlsx", ".xlsm")):
        df.to_excel(out, index=False)
    else:
        df.to_csv(out, index=False)

    ok = df["LATITUDE"].notna()
    print(f"-- {int(ok.sum())} of {len(df)} row(s) converted -> {out}")
    bad = int((~ok).sum())
    if bad:
        print(f"-- {bad} row(s) skipped (blank/non-numeric N/E or transform "
              f"out of range) — LATITUDE/LONGITUDE left empty")
    if ok.any():
        la = df.loc[ok, "LATITUDE"].astype(float)
        lo = df.loc[ok, "LONGITUDE"].astype(float)
        print(f"-- extent: lat {la.min():.4f} .. {la.max():.4f} · "
              f"lon {lo.min():.4f} .. {lo.max():.4f}")
        print("   (Teapot Dome should be roughly lat 43.25..43.40, "
              "lon -106.30..-106.10 — anywhere else, the EPSG or the "
              "column order is wrong)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
