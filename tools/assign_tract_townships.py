r"""Stamp each tract with the PLSS township it sits in, once.

THE TOWNSHIP LAYER'S FIRST DRAFT TOOK 75.6 SECONDS. It joined 2,888
townships to 24,178 tracts with geog.EnvelopeCenter() computed per row, so
nothing could be indexed and the server evaluated a spatial function
70 million times to answer "how many leases are in this box".

The fix is the one the wells already use: DERIVE IT ONCE AND STORE IT.
h3_refresh does exactly this for dv_well -- the cell is a column, indexed,
and the density layers group by it. A township is the same kind of fact: a
tract does not move, so which township contains it is answered once and read
forever.

After this, the layer's query is a GROUP BY on an indexed column.

A TRACT CAN ONLY BE IN ONE TOWNSHIP HERE, by its centroid. A large lease can
genuinely straddle two, and the honest treatment of that is the area-weighted
intersection the hex-vs-township comparison described -- which needs real
township polygons, not the bounding boxes dv_plss_township stores. Centroid
assignment is what the stored data can support, and it is stated rather than
hidden: the column says which township the tract's CENTRE is in.

    python tools/assign_tract_townships.py             # what it would stamp
    python tools/assign_tract_townships.py --apply
    python tools/assign_tract_townships.py --clear --apply
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def box_of(wkt):
    nums = [float(x) for x in re.findall(r"-?\d+\.?\d*", wkt or "")]
    if len(nums) < 8:
        return None
    xs, ys = nums[0::2], nums[1::2]
    return (min(ys), min(xs), max(ys), max(xs))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    from sqlalchemy import text as t
    eng = make_engine(a.database)

    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
        if cx.execute(t("SELECT COL_LENGTH('dataview.dv_land_tract_geom',"
                        "'plss_id')")).scalar() is None:
            if a.apply:
                cx.execute(t("ALTER TABLE dataview.dv_land_tract_geom "
                             "ADD plss_id nvarchar(20) NULL"))
                cx.execute(t("CREATE NONCLUSTERED INDEX "
                             "ix_dv_land_tract_geom_plss ON "
                             "dataview.dv_land_tract_geom (plss_id) "
                             "WITH (DATA_COMPRESSION = PAGE)"))
                print("   added dv_land_tract_geom.plss_id + index")
            else:
                print("   would add dv_land_tract_geom.plss_id + index")

    if a.clear:
        with eng.begin() as cx:
            n = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_land_tract_geom "
                             "WHERE plss_id IS NOT NULL")).scalar()
            if a.apply:
                cx.execute(t("UPDATE dataview.dv_land_tract_geom "
                             "SET plss_id = NULL"))
        print("%s %s stamp(s)" % ("cleared" if a.apply else "would clear",
                                  format(n, ",")))
        return 0

    with eng.connect() as cx:
        twps = cx.execute(t("SELECT plss_id, bbox_wkt FROM "
                            "dataview.dv_plss_township "
                            "WHERE bbox_wkt IS NOT NULL")).fetchall()
        tracts = cx.execute(t("""
            SELECT tract_id, geog.EnvelopeCenter().Lat,
                   geog.EnvelopeCenter().Long
              FROM dataview.dv_land_tract_geom
             WHERE geog IS NOT NULL""")).fetchall()
    print("townships %s   tracts %s" % (format(len(twps), ","),
                                        format(len(tracts), ",")))

    # A 0.2-degree bucket index, so each tract tests a handful of boxes
    # rather than all 2,888 -- 24,178 x 2,888 is 70 million comparisons and
    # this is a few hundred thousand.
    cells, grid = [], {}
    for pid, wkt in twps:
        b = box_of(wkt)
        if not b:
            continue
        i = len(cells)
        cells.append((pid, b))
        s, w, n, e = b
        for gy in range(int(s * 5), int(n * 5) + 1):
            for gx in range(int(w * 5), int(e * 5) + 1):
                grid.setdefault((gy, gx), []).append(i)

    hits, misses = [], 0
    for tid, la, lo in tracts:
        la, lo = float(la), float(lo)
        found = None
        for i in grid.get((int(la * 5), int(lo * 5)), ()):
            pid, (s, w, n, e) = cells[i]
            if s <= la <= n and w <= lo <= e:
                found = pid
                break
        if found:
            hits.append({"i": tid, "p": found})
        else:
            misses += 1
    print("   tracts placed : %s" % format(len(hits), ","))
    print("   outside the grid: %s" % format(misses, ","))
    if not a.apply:
        print("\nDRY RUN -- re-run with --apply.")
        return 0

    pending = 0
    cxm = eng.begin()
    cx = cxm.__enter__()
    try:
        for h in hits:
            cx.execute(t("UPDATE dataview.dv_land_tract_geom SET plss_id = :p "
                         "WHERE tract_id = :i"), h)
            pending += 1
            if pending >= 1000:        # chunked: never hold the map's table
                cxm.__exit__(None, None, None)
                pending = 0
                cxm = eng.begin()
                cx = cxm.__enter__()
    finally:
        cxm.__exit__(None, None, None)

    with eng.connect() as cx:
        n = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_land_tract_geom "
                         "WHERE plss_id IS NOT NULL")).scalar()
        d = cx.execute(t("SELECT COUNT(DISTINCT plss_id) FROM "
                         "dataview.dv_land_tract_geom "
                         "WHERE plss_id IS NOT NULL")).scalar()
    print("\nstamped %s tract(s) across %s township(s)"
          % (format(n, ","), format(d, ",")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
