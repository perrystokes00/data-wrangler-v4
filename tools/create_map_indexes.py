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

# (database, table, index name, DDL). Order matters only in that the spatial
# index is the expensive one and is listed last so a --apply that is
# interrupted has already done the cheap ones.
#
# THE DATABASE IS PART OF THE ENTRY because the reference master lives in
# WELL_REF and CREATE INDEX cannot reach across databases -- a three-part
# name in the DDL is a syntax error, not a cross-database create.
INDEXES = [
    ("DataView_Demo", "dataview.dv_well", "IX_dv_well_lat_lon",
     "CREATE NONCLUSTERED INDEX IX_dv_well_lat_lon ON dataview.dv_well "
     "(surface_latitude, surface_longitude) WITH (DATA_COMPRESSION = PAGE)"),
    ("DataView_Demo", "dataview.dv_well", "IX_dv_well_h3_r4",
     "CREATE NONCLUSTERED INDEX IX_dv_well_h3_r4 ON dataview.dv_well "
     "(h3_r4) WITH (DATA_COMPRESSION = PAGE)"),
    ("DataView_Demo", "dataview.dv_well", "IX_dv_well_h3_r7",
     "CREATE NONCLUSTERED INDEX IX_dv_well_h3_r7 ON dataview.dv_well "
     "(h3_r7) WITH (DATA_COMPRESSION = PAGE)"),
    ("DataView_Demo", "dataview.dv_land_tract", "ix_dv_land_tract_geog",
     "CREATE SPATIAL INDEX ix_dv_land_tract_geog ON dataview.dv_land_tract(geog) "
     "USING GEOGRAPHY_AUTO_GRID WITH (DATA_COMPRESSION = PAGE)"),

    # ── THE REFERENCE MASTER, in WELL_REF ────────────────────────────────
    # These were lost, not merely missing. well_master_gold carried
    # IX_wmg_latlon and an index per H3 resolution; the master was rebuilt
    # from the source files as well_master_public_v2 and the new table got
    # only its key and name_norm. Nothing failed -- a hex explode took
    # 20.52s instead of 0.16s, and the density views scanned 3.1M rows.
    # That is the regression this file exists to prevent, so the master's
    # indexes are written down here with the rest.
    ("WELL_REF", "well_ref.well_master_public_v2", "ix_wmp2_latlon",
     "CREATE NONCLUSTERED INDEX ix_wmp2_latlon ON "
     "well_ref.well_master_public_v2 (surface_latitude, surface_longitude) "
     "INCLUDE (uwi14) WITH (DATA_COMPRESSION = PAGE)"),
    ("WELL_REF", "well_ref.well_master_public_v2", "ix_wmp2_name_norm",
     "CREATE NONCLUSTERED INDEX ix_wmp2_name_norm ON "
     "well_ref.well_master_public_v2 (name_norm) "
     "INCLUDE (uwi14, well_name, uwi_suspect) WITH (DATA_COMPRESSION = PAGE)"),
] + [
    # FILTERED TO ROWS THAT HAVE A COORDINATE, matching the density views'
    # own WHERE exactly. A filtered index is only usable when the query's
    # predicate implies the filter, so the two must be written together --
    # a probe that omitted the lat/lon test read 13s and looked like the
    # index had not helped at all.
    ("WELL_REF", "well_ref.well_master_public_v2", "ix_wmp2_h3_r%d" % _r,
     "CREATE NONCLUSTERED INDEX ix_wmp2_h3_r%d ON "
     "well_ref.well_master_public_v2 (h3_r%d) WHERE surface_latitude "
     "IS NOT NULL AND surface_longitude IS NOT NULL "
     "WITH (DATA_COMPRESSION = PAGE)" % (_r, _r))
    for _r in (4, 5, 6, 7)
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

    # ONE CONNECTION PER DATABASE. --database still names the DataView one,
    # so an existing invocation is unchanged; WELL_REF is reached by its own
    # engine because CREATE INDEX runs in the current database's context and
    # a three-part name will not cross that line.
    dbs = {}
    for db, _tb, _n, _d in INDEXES:
        dbs.setdefault(a.database if db == "DataView_Demo" else db, None)
    for db in dbs:
        dbs[db] = make_engine(db)

    todo, absent = [], []
    seen = {}
    for db, table, name, ddl in INDEXES:
        db = a.database if db == "DataView_Demo" else db
        with dbs[db].connect() as cx:
            if (db, table) not in seen:
                seen[(db, table)] = existing(cx, table)
        have = seen[(db, table)]
        where = "%s.%s" % (db, table)
        if have is None:
            absent.append((where, name))
            print("   %-40s %-24s TABLE NOT PRESENT" % (where, name))
        elif name in have:
            print("   %-40s %-24s present" % (where, name))
        else:
            todo.append((db, table, name, ddl))
            print("   %-40s %-24s MISSING" % (where, name))

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

    for db in sorted({d for d, _tb, _n, _dd in todo}):
        with dbs[db].connect().execution_options(
                isolation_level="AUTOCOMMIT") as cx:
            for _db, table, name, ddl in todo:
                if _db != db:
                    continue
                t0 = time.perf_counter()
                cx.execute(t(ddl))
                print("   + %-26s %.1fs  (%s)"
                      % (name, time.perf_counter() - t0, db))
            for table in sorted({tb for d2, tb, _n, _d in todo if d2 == db}):
                cx.execute(t("UPDATE STATISTICS %s" % table))
                print("   statistics updated on %s.%s" % (db, table))
    print("\ncreated %d index(es)." % len(todo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
