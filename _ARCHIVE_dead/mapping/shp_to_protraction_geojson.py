"""
shp_to_protraction_geojson.py

Convert a BOEM OCS protraction-area shapefile (e.g. protclip.shp) into the
GeoJSON that boem_geo.py consumes for constraining GOM wells.

- Reads polygons + the AREA_CODE attribute (the field that matches
  dataview_gom.well.bottom_area_code, e.g. EI, SS, WC, MC, GC).
- Merges all polygons that share an AREA_CODE (areas like Main Pass have
  "Addition" sub-polygons under one code) into a single MultiPolygon, so no
  piece is dropped.
- Writes one feature per code with properties AREA_CODE and AREA_NAME.

Datum note: BOEM protraction files are GCS_North_American_1927 (NAD27). The
NAD27->WGS84 shift in the Gulf is tens of meters — negligible against the size
of a protraction area — so coordinates are written through unchanged. Install
pyproj and reproject here if you ever need survey-grade accuracy.

Usage:
    pip install pyshp
    python shp_to_protraction_geojson.py protclip.shp gom_protraction.geojson
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

import shapefile  # pyshp


def convert(shp_path: str, out_path: str = "gom_protraction.geojson",
            code_field: str = "AREA_CODE", name_field: str = "PROT_NAME"):
    r = shapefile.Reader(shp_path)
    flds = [f[0] for f in r.fields[1:]]
    if code_field not in flds:
        raise SystemExit(
            f"Field {code_field!r} not in shapefile. Available: {flds}")
    ci = flds.index(code_field)
    ni = flds.index(name_field) if name_field in flds else None

    groups: dict[str, list] = defaultdict(list)
    names: dict[str, str] = {}
    for i in range(len(r)):
        code = str(r.record(i)[ci]).strip().upper()
        if not code:
            continue
        if ni is not None and code not in names:
            names[code] = str(r.record(i)[ni]).split(",")[0].strip()
        geo = r.shape(i).__geo_interface__
        if geo["type"] == "Polygon":
            groups[code].append(geo["coordinates"])
        elif geo["type"] == "MultiPolygon":
            for poly in geo["coordinates"]:
                groups[code].append(poly)

    feats = []
    for code in sorted(groups):
        polys = groups[code]
        geom = ({"type": "Polygon", "coordinates": polys[0]}
                if len(polys) == 1
                else {"type": "MultiPolygon", "coordinates": polys})
        feats.append({
            "type": "Feature",
            "properties": {"AREA_CODE": code, "AREA_NAME": names.get(code, "")},
            "geometry": geom,
        })

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh)

    print(f"Polygons read: {len(r)}")
    print(f"Areas written: {len(feats)}  (one MultiPolygon per AREA_CODE)")
    print(f"Wrote:         {out_path}")
    return feats


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    _in = sys.argv[1]
    _out = sys.argv[2] if len(sys.argv) > 2 else "gom_protraction.geojson"
    convert(_in, _out)
