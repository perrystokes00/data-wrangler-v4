r"""Load the TIGER town and highway geometries into the database, once.

WHY THEY HAVE TO BE IN SQL. "Leases within 5 miles of Casper" is a different
question from "leases whose NEAREST town is Casper" -- a lease four miles from
Glenrock and four and a half from Casper answers yes to the first and no to
the second. The stamped dist_city_km can only answer the second, because it
measured to whichever town happened to be closest. Answering the first needs
Casper's own geometry, so the geometry has to live somewhere the query can
reach it.

ONE COMPUTATION, TWO CONSUMERS. The panel's count and the map's filter must
agree -- that has cost this repo two rounds already (the 640-acre section, and
filters that reached the count and nothing else). So the query returns BOTH the
count and the matching tract ids, and the browser filters on those ids rather
than recomputing anything. It cannot disagree, because it does not decide.

RING ORIENTATION IS THE TRAP. SQL Server's geography type uses the left-hand
rule: a polygon whose ring runs the wrong way is not a small town, it is the
entire planet minus that town. Shapefiles carry no such convention. Every
polygon is checked on the way in -- anything larger than a sixth of Wyoming is
reoriented, and anything still absurd is refused rather than stored, because a
town the size of a hemisphere silently matches every lease in the state.

    python tools/load_cultural_geometry.py                 # what it would load
    python tools/load_cultural_geometry.py --apply
    python tools/load_cultural_geometry.py --drop --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PLACE_SHP = r"C:/Bulk/tiger/tl_2025_56_place.shp"
ROAD_SHP = r"C:/Bulk/tiger/tl_2025_56_prisecroads.shp"

# Wyoming is ~253,000 km2. A town bigger than a sixth of it is a wrong-way
# ring, not a town; the same reasoning bounds a single road segment.
MAX_PLACE_KM2 = 42000.0
MAX_ROAD_KM = 2000.0

DDL = {
    "dv_place_geom": """
        CREATE TABLE dataview.dv_place_geom (
            place_id       int IDENTITY(1,1) PRIMARY KEY,
            place_name     nvarchar(120) NOT NULL,
            province_state nvarchar(10)  NULL,
            place_type     nvarchar(40)  NULL,
            geog           geography     NULL,
            source         nvarchar(40)  NULL
        )""",
    "dv_road_geom": """
        CREATE TABLE dataview.dv_road_geom (
            road_id        int IDENTITY(1,1) PRIMARY KEY,
            road_name      nvarchar(120) NOT NULL,
            province_state nvarchar(10)  NULL,
            road_class     nvarchar(20)  NULL,
            geog           geography     NULL,
            source         nvarchar(40)  NULL
        )""",
}

# LSAD 25 = city, 43 = town, 57 = CDP. All are places a lease can be "near";
# the type is stored so a caller can narrow if it ever matters.
LSAD = {"25": "city", "43": "town", "57": "CDP"}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--places", default=PLACE_SHP)
    ap.add_argument("--roads", default=ROAD_SHP)
    ap.add_argument("--secondary", action="store_true",
                    help="also load S1200 state and county roads. They are "
                         "recorded as road_class='secondary', so the "
                         "distance stamp can still measure to primaries "
                         "alone and dist_hwy_km keeps its meaning")
    ap.add_argument("--state", default="WY")
    ap.add_argument("--drop", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    import geopandas as gpd
    from sqlalchemy import text as t
    from dataview.core.dw_utils import make_engine
    eng = make_engine(a.database)

    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
        for tb, ddl in DDL.items():
            exists = cx.execute(t("SELECT OBJECT_ID('dataview.' + :t)"),
                                {"t": tb}).scalar() is not None
            if a.drop and exists:
                if a.apply:
                    cx.execute(t("DROP TABLE dataview.%s" % tb))
                    print("   dropped dataview.%s" % tb)
                    exists = False
                else:
                    print("   would drop dataview.%s" % tb)
            if not exists and a.apply:
                cx.execute(t(ddl))
                print("   created dataview.%s" % tb)
            elif not exists:
                print("   would create dataview.%s" % tb)

    for p in (a.places, a.roads):
        if not os.path.exists(p):
            print("missing input: %s" % p)
            return 2

    pl = gpd.read_file(a.places).to_crs("EPSG:4326")
    rd = gpd.read_file(a.roads)
    # ── WHICH CLASSES ─────────────────────────────────────────────────────
    # S1100 is interstates and US routes; S1200 is state and county roads.
    # The class is CARRIED into road_class rather than dropped, because
    # dist_hwy_km was measured against the primaries alone: a road table
    # that cannot tell the two apart would silently redefine what "nearest
    # highway" means for every lease already stamped.
    _want = ["S1100"] + (["S1200"] if a.secondary else [])
    rd = rd[rd["MTFCC"].isin(_want)].copy()
    rd["cls"] = rd["MTFCC"].map({"S1100": "primary", "S1200": "secondary"})
    rd = rd.to_crs("EPSG:4326")
    print("places %s   roads %s (%s)"
          % (format(len(pl), ","), format(len(rd), ","), ", ".join(_want)))

    # Merge road SEGMENTS into ROUTES. TIGER splits I-80 into many features;
    # "within 5 miles of I-80" means the road, not one arbitrary piece of it.
    rd["nm"] = rd["FULLNAME"].astype(str)
    # A ROUTE'S CLASS SURVIVES THE MERGE. dissolve keeps one row's
    # attributes and "one" is arbitrary, so primary is made to win where a
    # name appears in both classes rather than leaving it to row order.
    rd = rd.sort_values("cls")          # 'primary' sorts before 'secondary'
    routes = rd.dissolve(by="nm", aggfunc={"cls": "first"})
    print("routes %s after merging segments by name" % format(len(routes), ","))

    if not a.apply:
        print("\n   would load %s place(s) and %s route(s)"
              % (format(len(pl), ","), format(len(routes), ",")))
        print("\nDRY RUN -- re-run with --apply.")
        return 0

    # ── places ────────────────────────────────────────────────────────────
    ok = bad = 0
    with eng.begin() as cx:
        cx.execute(t("DELETE FROM dataview.dv_place_geom WHERE "
                     "province_state = :s"), {"s": a.state})
        for _, r in pl.iterrows():
            wkt = r.geometry.wkt
            # SCALARS ONLY OUT OF SQL. Selecting the geography itself returns
            # ODBC type -151, which pyodbc cannot represent -- "not yet
            # supported", from a driver that will happily STORE it. Ask for
            # the number the decision needs, not the object.
            row = cx.execute(t("""
                DECLARE @g geography;
                BEGIN TRY
                    SET @g = geography::STGeomFromText(:w, 4326).MakeValid();
                    IF @g.STArea() / 1000000.0 > :cap
                        SET @g = @g.ReorientObject();
                END TRY
                BEGIN CATCH
                    SET @g = NULL;
                END CATCH
                SELECT CASE WHEN @g IS NULL THEN 0 ELSE 1 END,
                       CASE WHEN @g IS NULL THEN NULL
                            ELSE @g.STArea()/1000000.0 END"""),
                {"w": wkt, "cap": MAX_PLACE_KM2}).first()
            if not row[0] or (row[1] or 0) > MAX_PLACE_KM2:
                bad += 1
                continue
            # THE SAME REORIENT ON THE WAY IN, or the check above tested a
            # geometry that is not the one being stored.
            cx.execute(t("""
                DECLARE @g geography =
                    geography::STGeomFromText(:w, 4326).MakeValid();
                IF @g.STArea() / 1000000.0 > :cap
                    SET @g = @g.ReorientObject();
                INSERT INTO dataview.dv_place_geom
                    (place_name, province_state, place_type, geog, source)
                VALUES (:n, :s, :ty, @g, 'TIGER2025')"""),
                {"n": str(r["NAME"]), "s": a.state,
                 "ty": LSAD.get(str(r.get("LSAD")), str(r.get("LSAD"))),
                 "w": wkt, "cap": MAX_PLACE_KM2})
            ok += 1
    print("places loaded %s   refused %s" % (format(ok, ","), format(bad, ",")))

    # ── roads ─────────────────────────────────────────────────────────────
    rok = rbad = 0
    with eng.begin() as cx:
        cx.execute(t("DELETE FROM dataview.dv_road_geom WHERE "
                     "province_state = :s"), {"s": a.state})
        for nm, r in routes.iterrows():
            wkt = r.geometry.wkt
            try:
                cx.execute(t("""
                    INSERT INTO dataview.dv_road_geom
                        (road_name, province_state, road_class, geog, source)
                    VALUES (:n, :s, :c,
                            geography::STGeomFromText(:w, 4326).MakeValid(),
                            'TIGER2025')"""),
                    {"n": str(nm), "s": a.state, "w": wkt,
                     "c": str(r.get("cls") or "primary")})
                rok += 1
            except Exception as exc:
                # NOT swallowed: a route that will not load is a route the
                # filter will silently never offer.
                print("   road %-16s refused: %s" % (str(nm)[:16],
                                                     str(exc)[:70]))
                rbad += 1
    print("routes loaded %s   refused %s" % (format(rok, ","),
                                             format(rbad, ",")))

    with eng.connect() as cx:
        print()
        for tb, col in (("dv_place_geom", "place_name"),
                        ("dv_road_geom", "road_name")):
            n = cx.execute(t("SELECT COUNT(*) FROM dataview.%s" % tb)).scalar()
            print("dataview.%-16s %s row(s)" % (tb, format(n, ",")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
