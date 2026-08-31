r"""Stamp each tract with its distance to the nearest town and highway, once.

WHY STAMPED AND NOT COMPUTED: "leases within 5 miles of a city" asked at
render time is 24,178 tracts x 205 places evaluated per draw, which is the
same shape as the township layer's first draft -- a spatial function across
70 million pairs, 75.6 seconds. A tract does not move and neither does
Laramie, so the distance is answered once and read forever. h3_refresh does
this for wells, assign_tract_townships for the PLSS grid, and the county
column for counties; this is the fourth instance of one pattern.

After this, "within 5 miles of a city" is a numeric column test, and it rides
the client-side filter path the lease strip already uses for minimum acres.

SOURCE: Census TIGER/Line 2025, Wyoming (FIPS 56) -- public domain.
    PLACE        205 features, incorporated cities/towns and CDPs
    PRISECROADS  1,344 features; S1100 primary (interstate/US), S1200 state

DISTANCE IS TO THE FEATURE, NOT ITS CENTRE. A lease three miles from the edge
of Casper is three miles from Casper, not eleven from a point in the middle of
it; places are polygons and roads are lines, so the distance is to the shape.

PROJECTED BEFORE MEASURING. Degrees are not miles and are not even constant
miles -- a degree of longitude at 43 N is 0.73 of one at the equator. EPSG
5070 (NAD83 Albers, CONUS) measures in metres and is accurate well inside the
tolerance a 5-mile filter needs.

    python tools/stamp_cultural_distance.py                 # what it would do
    python tools/stamp_cultural_distance.py --apply
    python tools/stamp_cultural_distance.py --clear --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The columns this writes. Named here so the guard, the clear and the update
# cannot drift apart -- the "lists that must agree" failure, in miniature.
COLS = [
    ("dist_city_km", "float"),
    ("near_city", "nvarchar(100)"),
    ("dist_hwy_km", "float"),
    ("near_hwy", "nvarchar(100)"),
]

PLACE_SHP = r"C:/Bulk/tiger/tl_2025_56_place.shp"
ROAD_SHP = r"C:/Bulk/tiger/tl_2025_56_prisecroads.shp"
METRIC = "EPSG:5070"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--places", default=PLACE_SHP)
    ap.add_argument("--roads", default=ROAD_SHP)
    ap.add_argument("--highway-only", action="store_true", default=True,
                    help="measure to S1100 primary roads only (default)")
    ap.add_argument("--all-roads", dest="highway_only", action="store_false",
                    help="measure to S1200 state highways as well")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    import geopandas as gpd
    from sqlalchemy import text as t
    from dataview.core.dw_utils import make_engine

    eng = make_engine(a.database)

    # ── the columns, added only if missing ────────────────────────────────
    # COL_LENGTH returns NULL for a missing TABLE and a missing COLUMN alike,
    # so it is paired with OBJECT_ID or the guard silently skips.
    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
        if cx.execute(t("SELECT OBJECT_ID('dataview.dv_land_tract_geom')"
                        )).scalar() is None:
            print("dataview.dv_land_tract_geom does not exist -- nothing to do")
            return 2
        for col, typ in COLS:
            have = cx.execute(t("SELECT COL_LENGTH("
                                "'dataview.dv_land_tract_geom', :c)"),
                              {"c": col}).scalar()
            if have is None:
                if a.apply:
                    cx.execute(t("ALTER TABLE dataview.dv_land_tract_geom "
                                 "ADD %s %s NULL" % (col, typ)))
                    print("   added dv_land_tract_geom.%s" % col)
                else:
                    print("   would add dv_land_tract_geom.%s %s" % (col, typ))

    if a.clear:
        with eng.begin() as cx:
            n = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_land_tract_geom "
                             "WHERE dist_city_km IS NOT NULL")).scalar()
            if a.apply:
                cx.execute(t("UPDATE dataview.dv_land_tract_geom SET " +
                             ", ".join("%s = NULL" % c for c, _ in COLS)))
        print("%s %s stamp(s)" % ("cleared" if a.apply else "would clear",
                                  format(n, ",")))
        return 0

    for p in (a.places, a.roads):
        if not os.path.exists(p):
            print("missing input: %s" % p)
            print("Download TIGER 2025 Wyoming (FIPS 56) PLACE and "
                  "PRISECROADS first.")
            return 2

    # ── the tracts ────────────────────────────────────────────────────────
    with eng.connect() as cx:
        tracts = cx.execute(t("""
            SELECT tract_id,
                   geog.EnvelopeCenter().Lat  AS la,
                   geog.EnvelopeCenter().Long AS lo
              FROM dataview.dv_land_tract_geom
             WHERE geog IS NOT NULL""")).fetchall()
    print("tracts %s" % format(len(tracts), ","))
    if not tracts:
        return 0

    pts = gpd.GeoDataFrame(
        {"tract_id": [r[0] for r in tracts]},
        geometry=gpd.points_from_xy([float(r[2]) for r in tracts],
                                    [float(r[1]) for r in tracts]),
        crs="EPSG:4326").to_crs(METRIC)

    places = gpd.read_file(a.places).to_crs(METRIC)[["NAME", "geometry"]]
    roads = gpd.read_file(a.roads)
    if a.highway_only:
        roads = roads[roads["MTFCC"] == "S1100"]
    roads = roads.to_crs(METRIC)[["FULLNAME", "geometry"]]
    print("places %s   roads %s (%s)"
          % (format(len(places), ","), format(len(roads), ","),
             "S1100 primary" if a.highway_only else "S1100+S1200"))
    if roads.empty:
        print("no roads matched the filter -- refusing to stamp NULL over "
              "everything")
        return 2

    # sjoin_nearest measures to the GEOMETRY, so a lease just outside the
    # town boundary reads as just outside the town.
    nc = gpd.sjoin_nearest(pts, places, how="left", distance_col="_d")
    nc = nc[~nc.index.duplicated(keep="first")]
    nh = gpd.sjoin_nearest(pts, roads, how="left", distance_col="_d")
    nh = nh[~nh.index.duplicated(keep="first")]

    rows = []
    for i, tid in enumerate(pts["tract_id"]):
        rows.append({
            "i": tid,
            "dc": (None if nc["_d"].iloc[i] is None
                   else round(float(nc["_d"].iloc[i]) / 1000.0, 4)),
            "nc": (nc["NAME"].iloc[i] or None),
            "dh": (None if nh["_d"].iloc[i] is None
                   else round(float(nh["_d"].iloc[i]) / 1000.0, 4)),
            "nh": (nh["FULLNAME"].iloc[i] or None),
        })

    import statistics
    _dc = [r["dc"] for r in rows if r["dc"] is not None]
    _dh = [r["dh"] for r in rows if r["dh"] is not None]
    print("\n   nearest town  : min %.2f  median %.2f  max %.2f km"
          % (min(_dc), statistics.median(_dc), max(_dc)))
    print("   nearest highway: min %.2f  median %.2f  max %.2f km"
          % (min(_dh), statistics.median(_dh), max(_dh)))
    _5mi = 8.04672
    print("   within 5 miles of a town   : %s"
          % format(sum(1 for d in _dc if d <= _5mi), ","))
    print("   within 5 miles of a highway: %s"
          % format(sum(1 for d in _dh if d <= _5mi), ","))

    if not a.apply:
        print("\nDRY RUN -- re-run with --apply.")
        return 0

    # CHUNKED. 21,799 updates in one transaction once held the map's own
    # table long enough that seven renders queued on LCK_M_S and the leases
    # stopped drawing. Never hold this table for a whole pass.
    pending, done = 0, 0
    cxm = eng.begin()
    cx = cxm.__enter__()
    try:
        for r in rows:
            cx.execute(t("""UPDATE dataview.dv_land_tract_geom
                               SET dist_city_km = :dc, near_city = :nc,
                                   dist_hwy_km  = :dh, near_hwy  = :nh
                             WHERE tract_id = :i"""), r)
            pending += 1
            done += 1
            if pending >= 1000:
                cxm.__exit__(None, None, None)
                pending = 0
                cxm = eng.begin()
                cx = cxm.__enter__()
    finally:
        cxm.__exit__(None, None, None)

    with eng.connect() as cx:
        n = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_land_tract_geom "
                         "WHERE dist_city_km IS NOT NULL")).scalar()
    print("\nstamped %s tract(s)" % format(n, ","))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
