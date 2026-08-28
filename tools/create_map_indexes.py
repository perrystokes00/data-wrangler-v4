r"""Create the indexes the Mapping page needs. Idempotent; dry run by default.

WHY THIS FILE EXISTS. Every index below was created by hand against a live
database while chasing a hang, and none of them was written down. A rebuilt
DataView_Demo would have come back without them and simply been slow -- which
is the worst shape a regression can take, because nothing fails and there is
nothing to grep for. The map would just "get slow for no reason" again.

WHAT EACH ONE COST WHEN IT WAS MISSING, measured 28 Aug 2026:

  ix_dv_land_tract_geog   dv_well x dv_land_tract with no spatial index is
                          28,173 x 4,618 = 130,102,914 STIntersects tests.
                          Observed: 706s of CPU, 1.17M logical reads, still
                          running when it was killed. With the index the same
                          join is 15.2s -- and that is the query _well_lease_map
                          runs to resolve a well to the lease it sits in. At 322
                          leases the brute force was survivable; at 4,618 it was
                          not, so this only bites once real BLM data is loaded.

  IX_dv_well_lat_lon      every bbox query -- the rectangle drill, the circle
                          drill, and the clip-to-box predicate -- was a
                          clustered scan. dv_well's docstring claimed this index
                          existed and it did not. 0.053s -> 0.016s at 28K rows;
                          the point is that it stays a seek at 4M.

  IX_dv_well_h3_r4        R4 is the map's DEFAULT resolution and was unindexed,
  IX_dv_well_h3_r7        while r5 and r6 were indexed. R7 is what a close-in
                          box selection uses (_qry_cell_uwis_in_bbox filters on
                          h3_r<res>). An odd pair to have been missing.

PAGE COMPRESSION on all of them: these are narrow keys over a wide table and
the map reads them constantly. The build cost is trivial -- 0.2-0.5s each on
28K rows, 4.6s for the spatial index on 4,618 polygons.

    python tools/create_map_indexes.py            # report what is missing
    python tools/create_map_indexes.py --apply    # create it
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# (table, index name, DDL). Order matters only in that the spatial index is the
# expensive one and is listed last so a --apply that is interrupted has already
# done the cheap ones.
INDEXES = [
    ("dataview.dv_well", "IX_dv_well_lat_lon",
     "CREATE NONCLUSTERED INDEX IX_dv_well_lat_lon ON dataview.dv_well "
     "(surface_latitude, surface_longitude) WITH (DATA_COMPRESSION = PAGE)"),
    ("dataview.dv_well", "IX_dv_well_h3_r4",
     "CREATE NONCLUSTERED INDEX IX_dv_well_h3_r4 ON dataview.dv_well "
     "(h3_r4) WITH (DATA_COMPRESSION = PAGE)"),
    ("dataview.dv_well", "IX_dv_well_h3_r7",
     "CREATE NONCLUSTERED INDEX IX_dv_well_h3_r7 ON dataview.dv_well "
     "(h3_r7) WITH (DATA_COMPRESSION = PAGE)"),
    ("dataview.dv_land_tract", "ix_dv_land_tract_geog",
     "CREATE SPATIAL INDEX ix_dv_land_tract_geog ON dataview.dv_land_tract(geog) "
     "USING GEOGRAPHY_AUTO_GRID WITH (DATA_COMPRESSION = PAGE)"),
]


def existing(cx, table):
    """Index names already on a table, or None when the TABLE is absent.

    None and empty-set are different answers and the caller must not collapse
    them: a missing table means "nothing to do here", a missing index means
    "create it". COL_LENGTH-style guards that treat both as falsy are how a
    check silently skips (CLAUDE.md).
    """
    from sqlalchemy import text as t
    if cx.execute(t("SELECT OBJECT_ID(:n)"), {"n": table}).scalar() is None:
        return None
    return {r[0] for r in cx.execute(
        t("SELECT name FROM sys.indexes WHERE object_id = OBJECT_ID(:n) "
          "AND name IS NOT NULL"), {"n": table})}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--apply", action="store_true",
                    help="create what is missing. Without it, nothing is written.")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    from sqlalchemy import text as t
    eng = make_engine(a.database)

    todo, absent = [], []
    with eng.connect() as cx:
        seen = {}
        for table, name, ddl in INDEXES:
            if table not in seen:
                seen[table] = existing(cx, table)
            have = seen[table]
            if have is None:
                absent.append((table, name))
                print("   %-24s %-26s TABLE NOT PRESENT" % (table, name))
            elif name in have:
                print("   %-24s %-26s present" % (table, name))
            else:
                todo.append((table, name, ddl))
                print("   %-24s %-26s MISSING" % (table, name))

    if absent:
        print("\n%d index(es) skipped because their table does not exist in "
              "%s. That is a different problem from a missing index and is "
              "not fixed here." % (len(absent), a.database))
    if not todo:
        print("\nNothing to create.")
        return 0
    if not a.apply:
        print("\n%d index(es) to create. DRY RUN -- re-run with --apply."
              % len(todo))
        return 0

    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
        for table, name, ddl in todo:
            t0 = time.perf_counter()
            cx.execute(t(ddl))
            print("   + %-26s %.1fs" % (name, time.perf_counter() - t0))
        for table in sorted({tb for tb, _n, _d in todo}):
            cx.execute(t("UPDATE STATISTICS %s" % table))
            print("   statistics updated on %s" % table)
    print("\ncreated %d index(es)." % len(todo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
