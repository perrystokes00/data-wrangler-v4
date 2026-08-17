"""
load_teapot_seismic_geometry.py — give the Teapot SEG-Y its geometry from the
survey's own paperwork, so promote can lift it. PREVIEW by default.

    python load_teapot_seismic_geometry.py --nav "C:\\...\\2DNavigationLinesA-E.txt"
    python load_teapot_seismic_geometry.py --nav <file> --apply

WHY NOT JUST ARM A FALLBACK CRS AND RE-EXTRACT
----------------------------------------------
Because the coordinates are not where a reader looks. teapot_3d_load.doc states
the trace header layout:

    CDP X_COORD   81- 84 and 189-193
    CDP Y_COORD   85- 88 and 193-196

The SEG-Y standard puts CDP X/Y at bytes 181-188; bytes 81-88 are the GROUP
coordinates. This vendor wrote them somewhere else, so a standard extractor
reads zeros — which is exactly what the pipeline reported: six files held with
"no outline, no bbox". A CRS cannot fix coordinates that were never read.

The paperwork is the better source in any case. The 2D navigation is 535
SURVEYED shotpoints; the 3D corners are stated exactly. Neither depends on
guessing what a 1977 contractor put in byte 81.

THE CRS, STATED THREE TIMES INDEPENDENTLY
-----------------------------------------
    2DNavigationLinesA-E.txt  "SPCS27 - Wyoming East Central  Datum: NAD 1927"
                              "Data Coordinate System Units: U.S. Survey Feet"
    2DdataLoadSheet.doc       "Wyoming East Central State Plane  NAD 1927"
    teapot_3d_load.doc        "State and Zone: Wyoming East Central 4902
                               NAD: 1927"        (4902 = the SPCS27 zone code)

= EPSG 32056. Verified rather than assumed: all 535 navigation points
transform into the published 2D basemap's extent (43deg14' - 43deg20' N,
106deg09' - 106deg15' W), and the 3D corner ring comes to 17,869 acres against
the 18,017 the load sheet implies from 345 x 188 bins at 110 ft — a 0.8%
difference, which is bin edge versus bin centre.

WHAT THIS WRITES
----------------
FILE_SEIS_HEADER.SURVEY_OUTLINE (WGS84 WKT) plus the bbox columns and
EPSG_CODE — the same fields extract_core fills for every other survey. Nothing
downstream changes: promote_seismic already converts SURVEY_OUTLINE into
dv_seis_line.geog and applies the mappable gate.

Columns are read from INFORMATION_SCHEMA, not assumed. Today alone I have
guessed four column names wrong on four different tables.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

SRC_EPSG = 32056           # NAD27 / Wyoming East Central, US survey feet

# From teapot_3d_load.doc, in the order the sheet lists them; ring order is
# fixed below rather than trusted, because a corner table traced in the order
# it happens to be written can cross itself — a bowtie plots as a sliver in
# the right place, which is the kind of wrong that survives review.
CORNERS_3D = [
    ("lower left",  788937, 938846),
    ("lower right", 809502, 939334),
    ("upper right", 808604, 977163),
    ("upper left",  788039, 976675),
]


def read_nav(path: str) -> dict[str, list[tuple[int, int]]]:
    """{'A': [(x, y), ...]} in shotpoint order — the order IS the line."""
    lines: dict[str, list[tuple[int, int]]] = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            m = re.match(r"\s*([A-Z])\s+(\d+)\s+(-?\d+)\s+(-?\d+)\s*$", ln)
            if m:
                lines.setdefault(m.group(1), []).append(
                    (int(m.group(3)), int(m.group(4))))
    return lines


def ring_order(pts):
    """Anticlockwise about the centroid, so the polygon cannot self-cross."""
    import math
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    return sorted(pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))


def to_wgs84(pts):
    from pyproj import Transformer
    t = Transformer.from_crs(f"EPSG:{SRC_EPSG}", "EPSG:4326", always_xy=True)
    return [t.transform(x, y) for x, y in pts]


def wkt_line(ll):     # WKT is lon lat, which is the opposite of how it reads
    return "LINESTRING(" + ", ".join(f"{lon:.7f} {lat:.7f}" for lon, lat in ll) + ")"


def wkt_poly(ll):
    r = list(ll) + [ll[0]]                      # close the ring
    return "POLYGON((" + ", ".join(f"{lon:.7f} {lat:.7f}" for lon, lat in r) + "))"


def bbox(ll):
    lons = [p[0] for p in ll]; lats = [p[1] for p in ll]
    return min(lats), max(lats), min(lons), max(lons)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--nav", required=True, help="2DNavigationLinesA-E.txt")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if not os.path.isfile(a.nav):
        print(f"not found: {a.nav}", file=sys.stderr)
        return 2

    # ── build the geometry ────────────────────────────────────────────────
    nav = read_nav(a.nav)
    geoms = {}
    for name, pts in sorted(nav.items()):
        ll = to_wgs84(pts)
        geoms[f"line{name.lower()}"] = ("LINE", wkt_line(ll), bbox(ll), len(pts))
    ll3 = to_wgs84(ring_order([(c[1], c[2]) for c in CORNERS_3D]))
    geoms["_3d"] = ("POLY", wkt_poly(ll3), bbox(ll3), len(CORNERS_3D))

    print("geometry built from the survey paperwork:")
    for k, (kind, _w, bb, n) in geoms.items():
        print(f"  {k:<8} {kind:<5} {n:>4} pt(s)   "
              f"{bb[0]:.4f}-{bb[1]:.4f} N, {bb[2]:.4f} to {bb[3]:.4f} W")

    # ── what is in the catalog, and which row is which ────────────────────
    import pyodbc
    cn = pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={a.server};"
        f"DATABASE={a.database};Trusted_Connection=yes;", autocommit=False)
    cur = cn.cursor()

    cur.execute("""SELECT c.name FROM sys.columns c
                   WHERE c.object_id = OBJECT_ID('file_catalog.FILE_SEIS_HEADER')""")
    cols = {r[0].upper() for r in cur.fetchall()}
    need = ["SURVEY_OUTLINE", "EPSG_CODE",
            "BBOX_MIN_LAT", "BBOX_MAX_LAT", "BBOX_MIN_LON", "BBOX_MAX_LON"]
    missing = [c for c in need if c not in cols]
    if missing:
        print(f"\nFILE_SEIS_HEADER has no {', '.join(missing)} — "
              f"columns present: {', '.join(sorted(cols))}", file=sys.stderr)
        return 2

    cur.execute("""
        SELECT h.INVENTORY_ID, g.FILE_NAME, h.SURVEY_NAME,
               CASE WHEN h.SURVEY_OUTLINE IS NULL THEN 'no' ELSE 'yes' END
        FROM file_catalog.FILE_SEIS_HEADER h WITH (NOLOCK)
        JOIN file_catalog.GLOBAL_FILE_CATALOG g WITH (NOLOCK)
          ON g.INVENTORY_ID = h.INVENTORY_ID
        WHERE LOWER(g.FILE_EXT) IN ('.sgy', '.segy')
        ORDER BY g.FILE_NAME""")
    rows = cur.fetchall()

    print(f"\n{len(rows)} seismic header row(s) in the catalog:")
    plan = []
    for inv, fname, survey, has_outline in rows:
        stem = os.path.splitext(fname)[0].lower().replace("_", "").replace("-", "")
        key = next((k for k in geoms if k != "_3d" and k == stem), None)
        if key is None and "mig" in stem:
            key = "_3d"                      # the migrated volume
        print(f"  {fname:<20} survey={str(survey)[:34]:<34} outline={has_outline}"
              f"   -> {key or 'NO MATCH — left alone'}")
        if key:
            plan.append((inv, fname, key))

    if not plan:
        print("\nnothing matched; no changes proposed.")
        return 1
    if not a.apply:
        print(f"\n-- report only; {len(plan)} row(s) would be updated. "
              f"Re-run with --apply, then re-run promote.")
        return 0

    try:
        for inv, fname, key in plan:
            _kind, wkt, bb, _n = geoms[key]
            cur.execute("""
                UPDATE file_catalog.FILE_SEIS_HEADER
                   SET SURVEY_OUTLINE = ?, EPSG_CODE = 4326,
                       BBOX_MIN_LAT = ?, BBOX_MAX_LAT = ?,
                       BBOX_MIN_LON = ?, BBOX_MAX_LON = ?
                 WHERE INVENTORY_ID = ?""",
                wkt, bb[0], bb[1], bb[2], bb[3], inv)
            print(f"  {fname}: {cur.rowcount} row(s)")
        cn.commit()
        print("\n-- committed. Now re-run the pipeline with Promote + Apply "
              "(Inventory off, Capture on) — nothing needs re-extracting.")
    except Exception as e:
        cn.rollback()
        print(f"\n-- ROLLED BACK, nothing changed: {e}", file=sys.stderr)
        return 1
    finally:
        cn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
