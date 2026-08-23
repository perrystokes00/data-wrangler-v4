"""
segy_lines_to_wgs84.py
======================
Turn captured SEG-Y trace paths into WGS84 lines you can put on a map.

Reads the docshape capture store, resolves each file's CRS from its OWN
textual header (never from coordinate magnitudes — that is what put the
Australian surveys off Norway), reprojects the sampled trace path, and writes
GeoJSON.

    py tools/segy_lines_to_wgs84.py --db seismic.duckdb --out seismic_lines.geojson

Anything whose CRS is not declared is reported and skipped, never guessed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _load_crs_reader():
    """crs_from_segy lives in dataview; fall back to a local copy.

    survey_corners (and its ring ordering) MOVED into crs_from_segy so the
    extract path (extract_core) and this exporter share ONE implementation
    instead of two regex sets that drift apart. Returns (crs_from_text,
    survey_corners)."""
    try:
        from dataview.file_catalog.crs_from_segy import (
            crs_from_text, survey_corners)
        return crs_from_text, survey_corners
    except Exception:
        try:
            from crs_from_segy import crs_from_text, survey_corners
            return crs_from_text, survey_corners
        except Exception:
            print("!! crs_from_segy.py not found (or predates survey_corners)."
                  " Put the updated copy in dataview/file_catalog/ or beside"
                  " this script.")
            raise SystemExit(2)





def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="seismic.duckdb")
    ap.add_argument("--out", default="seismic_lines.geojson")
    ap.add_argument("--table", default="doc_segy_header")
    a = ap.parse_args()

    try:
        import duckdb
    except ImportError:
        print("!! pip install duckdb")
        return 2
    try:
        from pyproj import Transformer
    except ImportError:
        print("!! pip install pyproj")
        return 2

    crs_from_text, survey_corners = _load_crs_reader()

    if not os.path.exists(a.db):
        print(f"!! {a.db} not found — run the capture first, from the repo root.")
        return 2

    con = duckdb.connect(a.db)
    rows = con.execute(f"""
        SELECT doc_file, survey_name, line_name, trace_path, path_points,
               textual_header, trace_count
        FROM {a.table}
        WHERE trace_path IS NOT NULL
    """).fetchall()
    print(f"-- {len(rows)} file(s) with a trace path")

    # One Transformer per EPSG, not per file: building one is expensive and
    # 231 of these share a CRS.
    _tx: dict[int, object] = {}
    feats, skipped, by_epsg = [], [], {}
    n_poly = [0]          # surveys drawn as an outline rather than a path

    for doc_file, survey, line, path, npts, text, ntr in rows:
        epsg, how, note = crs_from_text(text)
        if not epsg:
            skipped.append((doc_file, note))
            continue
        if epsg not in _tx:
            _tx[epsg] = Transformer.from_crs(epsg, 4326, always_xy=True)
        tx = _tx[epsg]

        # A survey that states its corners is drawn as its OUTLINE, not as a
        # path through its traces.
        corners = survey_corners(text)
        if corners:
            ring = []
            for x, y in corners:
                lon, lat = tx.transform(x, y)
                if abs(lon) <= 180 and abs(lat) <= 90:
                    ring.append([round(lon, 6), round(lat, 6)])
            if len(ring) >= 3:
                ring.append(ring[0])            # GeoJSON polygons close
                by_epsg[epsg] = by_epsg.get(epsg, 0) + 1
                n_poly[0] += 1
                feats.append({
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                    "properties": {
                        "doc_file": doc_file, "survey_name": survey,
                        "line_name": line, "epsg": epsg, "crs_source": how,
                        "crs_note": note, "geometry_from": "stated corners",
                        "trace_count": ntr},
                })
                continue

        coords = []
        for pair in str(path).split(";"):
            try:
                x, y = (float(v) for v in pair.split())
            except ValueError:
                continue
            lon, lat = tx.transform(x, y)
            # A failed transform yields inf, not an exception.
            if abs(lon) <= 180 and abs(lat) <= 90:
                coords.append([round(lon, 6), round(lat, 6)])
        if len(coords) < 2:
            skipped.append((doc_file, "fewer than 2 usable points"))
            continue

        by_epsg[epsg] = by_epsg.get(epsg, 0) + 1
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "doc_file": doc_file, "survey_name": survey,
                "line_name": line, "epsg": epsg, "crs_source": how,
                "crs_note": note, "path_points": npts, "trace_count": ntr,
            },
        })

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)

    print(f"-- {len(feats)} feature(s) written to {a.out} "
          f"({len(feats) - n_poly[0]} line(s), {n_poly[0]} survey outline(s))")
    for epsg, n in sorted(by_epsg.items()):
        print(f"     EPSG {epsg}: {n} line(s)")
    if skipped:
        print(f"-- {len(skipped)} skipped (CRS not declared — needs a human):")
        for name, why in skipped[:10]:
            print(f"     {name}: {why}")

    # The check that catches a wrong CRS before it reaches a map. Central
    # Australia must land near -26, 140; if the first line plots in the North
    # Sea the EPSG is wrong and everything downstream inherits it.
    if feats:
        # A LineString's coordinates are [[lon,lat],...]; a Polygon's are
        # [[[lon,lat],...]] — one level deeper. Flattening naively makes c[0]
        # a whole ring, which compares a list against a float.
        def _pts(ft):
            g = ft["geometry"]
            co = g["coordinates"]
            return co[0] if g["type"] == "Polygon" else co
        lons = [c[0] for ft in feats for c in _pts(ft)]
        lats = [c[1] for ft in feats for c in _pts(ft)]
        print(f"-- extent: lon {min(lons):.3f} .. {max(lons):.3f} · "
              f"lat {min(lats):.3f} .. {max(lats):.3f}")
        print("   (Central Australia should be roughly lon 138..142, lat -29..-25)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
