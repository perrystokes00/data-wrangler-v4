r"""Precompute tract-to-town and tract-to-route distances, once.

WHY NOT JUST ASK SQL. The obvious query --

    WHERE EXISTS (SELECT 1 FROM dv_place_geom p
                   WHERE p.place_name IN (...)
                     AND p.geog.STDistance(g.geog) <= @m)

-- ran for 626 SECONDS without answering and had to be killed, because
STDistance inside a correlated EXISTS gets no help from the spatial index and
degenerates into a scan over 24,178 polygons. That is the township layer's
75.6-second first draft wearing a different hat, and it has the same fix:
derive it once, store it, and let the filter be an indexed lookup.

    tract x route : 24,178 x 16, kept in full
    tract x town  : only pairs within --max-miles, because nobody filters
                    "within 400 miles of Casper" and the full cross product
                    is five million rows to answer a question no one asks

DISTANCE IS TO THE SHAPE, not to a centre: three miles from the edge of
Casper is three miles from Casper. Measured in EPSG:5070 metres for the same
reason stamp_cultural_distance does -- a degree of longitude at 43 N is not a
degree at the equator.

    python tools/build_cultural_distance_pairs.py            # counts only
    python tools/build_cultural_distance_pairs.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PLACE_SHP = r"C:/Bulk/tiger/tl_2025_56_place.shp"
ROAD_SHP = r"C:/Bulk/tiger/tl_2025_56_prisecroads.shp"
METRIC = "EPSG:5070"
KM_PER_MILE = 1.609344

DDL = {
    "dv_tract_place_dist": """
        CREATE TABLE dataview.dv_tract_place_dist (
            tract_id   nvarchar(64)  NOT NULL,
            place_name nvarchar(120) NOT NULL,
            dist_km    float         NOT NULL
        )""",
    "dv_tract_road_dist": """
        CREATE TABLE dataview.dv_tract_road_dist (
            tract_id  nvarchar(64)  NOT NULL,
            road_name nvarchar(120) NOT NULL,
            dist_km   float         NOT NULL
        )""",
}
IX = {
    "dv_tract_place_dist": ("ix_tpd_name_dist",
                            "(place_name, dist_km) INCLUDE (tract_id)"),
    "dv_tract_road_dist": ("ix_trd_name_dist",
                           "(road_name, dist_km) INCLUDE (tract_id)"),
}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--places", default=PLACE_SHP)
    ap.add_argument("--roads", default=ROAD_SHP)
    ap.add_argument("--max-miles", type=float, default=50.0)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    import geopandas as gpd
    import numpy as np
    from sqlalchemy import text as t, event
    from dataview.core.dw_utils import make_engine
    eng = make_engine(a.database)

    # pyodbc one row at a time is the 225-rows-per-second path this repo has
    # measured three times. fast_executemany is the supported accelerator and
    # turns a half-million-row load from hours into a couple of minutes.
    @event.listens_for(eng, "before_cursor_execute")
    def _fast(conn, cursor, statement, params, context, executemany):
        if executemany:
            cursor.fast_executemany = True

    for p in (a.places, a.roads):
        if not os.path.exists(p):
            print("missing input: %s" % p)
            return 2

    with eng.connect() as cx:
        tracts = cx.execute(t("""
            SELECT tract_id, geog.EnvelopeCenter().Lat,
                   geog.EnvelopeCenter().Long
              FROM dataview.dv_land_tract_geom
             WHERE geog IS NOT NULL""")).fetchall()
    print("tracts %s" % format(len(tracts), ","))

    pts = gpd.GeoDataFrame(
        {"tract_id": [r[0] for r in tracts]},
        geometry=gpd.points_from_xy([float(r[2]) for r in tracts],
                                    [float(r[1]) for r in tracts]),
        crs="EPSG:4326").to_crs(METRIC)
    geom = pts.geometry.values

    places = gpd.read_file(a.places).to_crs(METRIC)
    roads = gpd.read_file(a.roads)
    roads = roads[roads["MTFCC"] == "S1100"].to_crs(METRIC)
    roads["nm"] = roads["FULLNAME"].astype(str)
    routes = roads.dissolve(by="nm")
    print("places %s   routes %s" % (format(len(places), ","),
                                     format(len(routes), ",")))

    cap_m = a.max_miles * KM_PER_MILE * 1000.0
    place_rows, road_rows = [], []

    # ONE PLACE AT A TIME, VECTORISED ACROSS TRACTS. shapely 2 measures a
    # whole array against one geometry in a single call, so 205 calls cover
    # 5 million pairs -- a Python loop over the pairs themselves would not
    # finish in a useful time.
    for _, r in places.iterrows():
        d = geom.distance(r.geometry)
        keep = np.nonzero(d <= cap_m)[0]
        nm = str(r["NAME"])
        for i in keep:
            place_rows.append((pts["tract_id"].iloc[int(i)], nm,
                               round(float(d[int(i)]) / 1000.0, 4)))

    for nm, r in routes.iterrows():
        d = geom.distance(r.geometry)
        for i in range(len(d)):
            road_rows.append((pts["tract_id"].iloc[i], str(nm),
                              round(float(d[i]) / 1000.0, 4)))

    print("\n   tract x town pairs within %.0f mi : %s"
          % (a.max_miles, format(len(place_rows), ",")))
    print("   tract x route pairs (all)        : %s"
          % format(len(road_rows), ","))
    if not a.apply:
        print("\nDRY RUN -- re-run with --apply.")
        return 0

    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
        for tb, ddl in DDL.items():
            if cx.execute(t("SELECT OBJECT_ID('dataview.' + :t)"),
                          {"t": tb}).scalar() is not None:
                cx.execute(t("DROP TABLE dataview.%s" % tb))
            cx.execute(t(ddl))
            print("   (re)created dataview.%s" % tb)

    import time
    for tb, rows, cols in (
            ("dv_tract_place_dist", place_rows, "tract_id, place_name, dist_km"),
            ("dv_tract_road_dist", road_rows, "tract_id, road_name, dist_km")):
        t0 = time.time()
        with eng.begin() as cx:
            for i in range(0, len(rows), 20000):
                chunk = rows[i:i + 20000]
                cx.exec_driver_sql(
                    "INSERT INTO dataview.%s (%s) VALUES (?, ?, ?)" % (tb, cols),
                    chunk)
        print("   %-22s %9s rows in %5.1fs"
              % (tb, format(len(rows), ","), time.time() - t0))

    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
        for tb, (ixn, cols) in IX.items():
            cx.execute(t("CREATE NONCLUSTERED INDEX %s ON dataview.%s %s"
                         % (ixn, tb, cols)))
            print("   indexed %s" % ixn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
