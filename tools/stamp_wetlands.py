r"""Stamp each tract with how much of it is wetland, and of what kind.

SOURCE: USFWS National Wetlands Inventory, Wyoming geodatabase -- 882,183
polygons, 2.2 million acres. This is the registry a land man would cite
(PEM/PSS/PFO classes), not a basemap's green tint.

THE WETLANDS ARE STREAMED, THE TRACTS ARE HELD. The obvious shape -- load
882,183 polygons into a GeoDataFrame and overlay -- wants several gigabytes
and answers a question about 24,178 tracts. Inverted, the tract polygons sit
in one STRtree (24,178 is nothing) and the wetlands flow past once, each one
touching the handful of tracts it overlaps. Memory stays flat and the pass is
single.

AREA, NOT A FLAG. "Does this lease touch a wetland" is nearly always yes for
a section-sized tract crossed by a creek, so a boolean would mark most of
Wyoming and mean nothing. The acreage and the percentage are what a land man
can act on, and the dominant class says which kind.

MEASURED IN EPSG:5070, like every other distance and area in this repo, so a
number here can be compared with one from stamp_cultural_distance without
anybody having to ask which projection it came from.

    python tools/stamp_wetlands.py                 # what it would do
    python tools/stamp_wetlands.py --apply
    python tools/stamp_wetlands.py --apply --limit 2000   # a trial slice
    python tools/stamp_wetlands.py --clear --apply
"""
import argparse
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GDB = r"C:/Bulk/nwi/WY_geodatabase_wetlands.gdb"
LAYER = "WY_Wetlands"
METRIC = "EPSG:5070"
M2_PER_ACRE = 4046.8564224

COLS = [
    ("wetland_acres", "float"),
    ("wetland_pct", "float"),
    ("wetland_type", "nvarchar(60)"),
]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--gdb", default=GDB)
    ap.add_argument("--layer", default=LAYER)
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N wetland polygons (trial run)")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    import geopandas as gpd
    import fiona
    from shapely.geometry import shape
    from shapely.strtree import STRtree
    from shapely import wkt as shapely_wkt
    from sqlalchemy import text as t, event
    from dataview.core.dw_utils import make_engine

    eng = make_engine(a.database)

    @event.listens_for(eng, "before_cursor_execute")
    def _fast(conn, cursor, statement, params, context, executemany):
        if executemany:
            cursor.fast_executemany = True

    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
        for col, typ in COLS:
            if cx.execute(t("SELECT COL_LENGTH('dataview.dv_land_tract_geom',"
                            " :c)"), {"c": col}).scalar() is None:
                if a.apply:
                    cx.execute(t("ALTER TABLE dataview.dv_land_tract_geom "
                                 "ADD %s %s NULL" % (col, typ)))
                    print("   added dv_land_tract_geom.%s" % col)
                else:
                    print("   would add dv_land_tract_geom.%s %s" % (col, typ))

    if a.clear:
        with eng.begin() as cx:
            n = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_land_tract_geom"
                             " WHERE wetland_acres IS NOT NULL")).scalar()
            if a.apply:
                cx.execute(t("UPDATE dataview.dv_land_tract_geom SET " +
                             ", ".join("%s = NULL" % c for c, _ in COLS)))
        print("%s %s stamp(s)" % ("cleared" if a.apply else "would clear",
                                  format(n, ",")))
        return 0

    if not os.path.exists(a.gdb):
        print("missing geodatabase: %s" % a.gdb)
        return 2

    # ── the tracts, as real polygons ──────────────────────────────────────
    print("reading tracts...")
    with eng.connect() as cx:
        rows = cx.execute(t("""
            SELECT tract_id, geog.STAsText()
              FROM dataview.dv_land_tract_geom
             WHERE geog IS NOT NULL""")).fetchall()
    tr = gpd.GeoDataFrame(
        {"tract_id": [r[0] for r in rows]},
        geometry=[shapely_wkt.loads(r[1]) for r in rows],
        crs="EPSG:4326").to_crs(METRIC)
    tr["_area"] = tr.geometry.area
    print("tracts %s" % format(len(tr), ","))

    tree = STRtree(list(tr.geometry.values))
    tract_ids = list(tr["tract_id"])
    tract_area = list(tr["_area"])

    # tract index -> {type: intersected m2}
    acc = collections.defaultdict(lambda: collections.defaultdict(float))

    with fiona.open(a.gdb, layer=a.layer) as src:
        src_crs = src.crs
        total = len(src)
        print("wetlands %s   crs=%s" % (format(total, ","), src_crs))
        # One transformer for the whole pass; per-feature to_crs would
        # dominate the runtime.
        import pyproj
        from shapely.ops import transform as shp_transform
        project = pyproj.Transformer.from_crs(
            pyproj.CRS(src_crs), pyproj.CRS(METRIC), always_xy=True).transform

        t0 = time.time()
        seen = hits = 0
        for feat in src:
            seen += 1
            if a.limit and seen > a.limit:
                break
            geom = feat.get("geometry")
            if geom is None:
                continue
            try:
                g = shp_transform(project, shape(geom))
            except Exception:
                continue
            if g.is_empty:
                continue
            wt = (feat["properties"].get("WETLAND_TYPE") or "Unknown")
            for idx in tree.query(g):
                tg = tr.geometry.iloc[int(idx)]
                if not g.intersects(tg):
                    continue
                try:
                    inter = g.intersection(tg).area
                except Exception:
                    continue
                if inter > 0:
                    acc[int(idx)][wt] += inter
                    hits += 1
            if seen % 50000 == 0:
                el = time.time() - t0
                print("   %s / %s   %.0fs   %s tract-overlaps"
                      % (format(seen, ","), format(total, ","), el,
                         format(hits, ",")))

    print("\ntracts touching wetland: %s of %s"
          % (format(len(acc), ","), format(len(tr), ",")))
    if not acc:
        print("nothing overlapped -- refusing to stamp zeros over everything")
        return 2

    out = []
    for idx, per_type in acc.items():
        tot = sum(per_type.values())
        dom = max(per_type.items(), key=lambda kv: kv[1])[0]
        area = tract_area[idx] or 0.0
        out.append({
            "i": tract_ids[idx],
            "ac": round(tot / M2_PER_ACRE, 4),
            "pc": round((100.0 * tot / area), 3) if area else None,
            "ty": dom,
        })
    out.sort(key=lambda r: -(r["ac"] or 0))
    print("\n   wettest tracts:")
    for r in out[:5]:
        print("      %-14s %9.1f ac  %5.1f%%  %s"
              % (str(r["i"])[:14], r["ac"], r["pc"] or 0, r["ty"]))

    if not a.apply:
        print("\nDRY RUN -- re-run with --apply.")
        return 0

    # Tracts with no wetland get an explicit zero, not a NULL: "none here"
    # and "never measured" are different facts and a filter must be able to
    # tell them apart.
    with eng.begin() as cx:
        cx.execute(t("UPDATE dataview.dv_land_tract_geom "
                     "SET wetland_acres = 0, wetland_pct = 0, "
                     "    wetland_type = NULL "
                     "WHERE geog IS NOT NULL"))
    pending = 0
    cxm = eng.begin()
    cx = cxm.__enter__()
    try:
        for r in out:
            cx.execute(t("""UPDATE dataview.dv_land_tract_geom
                               SET wetland_acres = :ac, wetland_pct = :pc,
                                   wetland_type  = :ty
                             WHERE tract_id = :i"""), r)
            pending += 1
            if pending >= 1000:
                cxm.__exit__(None, None, None)
                pending = 0
                cxm = eng.begin()
                cx = cxm.__enter__()
    finally:
        cxm.__exit__(None, None, None)

    with eng.connect() as cx:
        r = cx.execute(t("""SELECT COUNT(*),
                                   SUM(CASE WHEN wetland_acres > 0
                                            THEN 1 ELSE 0 END),
                                   MAX(wetland_pct)
                              FROM dataview.dv_land_tract_geom
                             WHERE wetland_acres IS NOT NULL""")).first()
    print("\nstamped %s tract(s); %s hold wetland; wettest %.1f%%"
          % (format(r[0], ","), format(r[1], ","), r[2] or 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
