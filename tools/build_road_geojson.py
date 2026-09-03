r"""Turn the TIGER road shapefile into the GeoJSON the map serves.

THE POINT IS TO DRAW THE LINES THE FILTER MEASURES AGAINST. "Within 5 miles
of a highway" returns 3,286 leases, and until these are on the map that
number rests on an invisible reference -- the reader has to take it on
trust. Drawing the same 84 primary roads that tools/stamp_cultural_distance.py
measured to makes the answer checkable.

PRIMARY ONLY, BY DEFAULT, because that is what the stamp used. S1100 is
interstates and US highways -- 84 features, 1.19 MB. The 1,260 secondary
roads (S1200) are available with --secondary and cost 7 MB, but they are NOT
what dist_hwy_km measures, so drawing them beside the filter would invite
exactly the wrong reading.

The basemaps already draw roads. This is not that: it is OUR reference set,
the one the numbers came from.

    python tools/build_road_geojson.py                 # what it would write
    python tools/build_road_geojson.py --apply
    python tools/build_road_geojson.py --apply --secondary
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROAD_SHP = r"C:/Bulk/tiger/tl_2025_56_prisecroads.shp"
OUT_NAME = "dv_highways.geojson"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roads", default=ROAD_SHP)
    ap.add_argument("--secondary", action="store_true",
                    help="include S1200 state highways as well")
    ap.add_argument("--only-secondary", dest="only_secondary",
                    action="store_true",
                    help="write ONLY S1200 state and county roads, "
                         "to their own file; keeps the highway "
                         "layer as the evidence for dist_hwy_km")
    ap.add_argument("--out", default=None,
                    help="output file name under static/ "
                         "(default %s)" % OUT_NAME)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    if not os.path.exists(a.roads):
        print("missing input: %s" % a.roads)
        print("Download TIGER 2025 Wyoming (FIPS 56) PRISECROADS first.")
        return 2

    import geopandas as gpd
    rd = gpd.read_file(a.roads)
    # --only-secondary WRITES A SEPARATE FILE, and that is the point of it.
    # dv_highways.geojson IS the evidence for dist_hwy_km: the same 84
    # primary features the stamp measured to. Folding 1,260 state and county
    # roads into it would leave the map showing roads the filter never
    # measured, under a layer named for the filter -- the reader would have
    # no way to tell which lines the number came from. Two files, two
    # layers, two meanings.
    if a.only_secondary:
        keep = ["S1200"]
    else:
        keep = ["S1100"] + (["S1200"] if a.secondary else [])
    rd = rd[rd["MTFCC"].isin(keep)].copy()
    # WGS84 for the browser; TIGER ships NAD83, which is close but not the
    # same, and "close" is how layers end up 30 m apart.
    rd = rd.to_crs("EPSG:4326")
    print("roads %s  (%s)" % (format(len(rd), ","), ", ".join(keep)))
    if rd.empty:
        print("nothing matched -- refusing to write an empty layer")
        return 2

    feats = []
    for _i, (_, r) in enumerate(rd.iterrows()):
        g = r.geometry
        if g is None or g.is_empty:
            continue
        feats.append({
            # AN EXPLICIT ID, because folium refuses embed=False without a
            # unique identifier per feature. The lease file gets away with
            # none only because lease_number happens to be unique; road
            # names are not -- 84 features share 16 names, so "I- 80" is
            # four separate line segments. LINEARID is TIGER's own unique
            # key and is used when present.
            "id": (str(r.get("LINEARID")) if r.get("LINEARID")
                   else "road_%d" % _i),
            "type": "Feature",
            "geometry": json.loads(gpd.GeoSeries([g]).to_json())
                            ["features"][0]["geometry"],
            "properties": {
                "nm": (str(r.get("FULLNAME")) if r.get("FULLNAME") else ""),
                # PRIMARY AND SECONDARY ARE DRAWN DIFFERENTLY, so the reader
                # can see at a glance which lines the 5-mile filter used.
                "cls": ("primary" if r.get("MTFCC") == "S1100"
                        else "secondary"),
            },
        })
    fc = {"type": "FeatureCollection", "features": feats}

    sdir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "static")
    out = os.path.join(sdir, a.out or OUT_NAME)
    body = json.dumps(fc, separators=(",", ":"))
    print("features %s   %.2f MB" % (format(len(feats), ","), len(body) / 1e6))
    print("target   %s" % out)
    if not a.apply:
        print("\nDRY RUN -- re-run with --apply.")
        return 0

    os.makedirs(sdir, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(body)
    print("\nwrote %.2f MB" % (os.path.getsize(out) / 1e6))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
