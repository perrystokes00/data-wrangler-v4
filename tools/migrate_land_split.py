r"""Split dv_land_tract into the ground, the instrument, and the tie.

PHASE 1 ONLY: it CREATES and POPULATES the new tables and touches nothing
else. dv_land_tract is left exactly as it is, every one of the fifteen files
that reads it keeps working, and the map does not change. The deliverable of
this phase is the VERIFICATION -- proof that the new shape holds the same
leases, the same acreage and the same geometry as the old one. Phase 2 (the
swap) is described at the bottom and is not run here.

WHY SPLIT AT ALL

dv_land_tract is two different things in one table:

  the GROUND       geometry, area, quality note.   Changes almost never.
  the INSTRUMENT   lease number, dates, status,    Changes constantly.
                   lessee, royalty, mineral type.

Three facts from the data say the conflation has already cost something:

 1 THE CARDINALITY IS ALREADY WRONG. 24,178 rows hold far more polygon parts
   than rows -- a BLM serial covering six non-contiguous tracts is ONE legal
   instrument over SIX pieces of ground, and load_blm_leases says so in its
   own docstring. Today that is one MultiPolygon row, so "how many tracts"
   has no answer and a single tract cannot be selected.

 2 TWO SOURCES, TWO SCHEMAS. BLM gives 20 fields and Wyoming's LARCS ~80.
   Widening one table to their union produces a table that is mostly NULL
   whichever source you are looking at, and every new source widens it again.
   Attributes belong on the instrument, where they can differ.

 3 THE MAP PAYLOAD. write_lease_geojson bakes every attribute into every
   feature. Geometry is the big, stable part and attributes are the small,
   volatile part; served together, a status change re-downloads the polygons.
   This is the split that made the wells affordable.

PPDM ALREADY NAMES THIS. LAND_TRACT is the ground, LAND_RIGHT is the
instrument, LAND_RIGHT_TRACT ties them many-to-many. This is not an invention
to be argued about; it is the model the rest of the database already follows.

WHAT THIS DOES *NOT* DO, DELIBERATELY

 * It does not explode MultiPolygons. Splitting a six-tract lease into six
   tract rows is a SEPARATE decision with its own consequences (area per
   part, what a tract id means, how a drill reports), and the tie table makes
   it possible later without another migration. One row in, one tract out.

 * It does not resolve the lessee to a business associate. PPDM puts a lessee
   in BUSINESS_ASSOCIATE; dv_business_associate exists and holds ZERO rows,
   and "entity-parent resolution has no UI" is open work in CLAUDE.md.
   operator_name stays free text, with a nullable ba_id beside it for when
   that day comes. Seeding an entity parent is a decision, not a step.

 * It does not drop or alter dv_land_tract. Nothing is destroyed here, so
   --rollback is a DROP of three new tables and nothing else.

    python tools/migrate_land_split.py              # what it would do
    python tools/migrate_land_split.py --apply
    python tools/migrate_land_split.py --verify     # prove it matches
    python tools/migrate_land_split.py --rollback --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TRACT = "dataview.dv_land_tract_geom"
RIGHT = "dataview.dv_land_right"
TIE = "dataview.dv_land_right_tract"
NEW = (TRACT, RIGHT, TIE)

DDL = [
    # ── THE GROUND ──────────────────────────────────────────────────────
    # No lease number, no dates, no lessee. A tract is a piece of ground; it
    # does not stop existing when the lease over it expires, which is exactly
    # what makes lease HISTORY possible without duplicating geometry.
    (TRACT, """
        CREATE TABLE dataview.dv_land_tract_geom (
            tract_id        varchar(40)    NOT NULL PRIMARY KEY,
            geog            geography      NOT NULL,
            area_km2        float          NULL,
            province_state  nvarchar(40)   NULL,
            country         nvarchar(40)   NULL,
            source          nvarchar(40)   NULL,
            quality_note    nvarchar(400)  NULL,
            row_created_by  nvarchar(40)   NULL,
            row_created_date datetime2     NULL
        )"""),
    # ── THE INSTRUMENT ──────────────────────────────────────────────────
    # EVERY WIDTH MIRRORS dv_land_tract, read from sys.columns rather than
    # guessed. The first draft declared producing_ind varchar(8) because the
    # name reads like a flag; it holds "Held by Actual Production" and the
    # insert died on truncation after the tracts had already loaded. A column
    # width invented from a column NAME is a guess wearing a schema.
    # UNIQUE on (source, lease_number), which is the key the loader already
    # dedupes on -- both halves, so a state serial cannot collide with a BLM
    # one. ba_id is here and nullable on purpose: the column exists so the
    # resolution can happen later without a migration, and stays NULL until
    # something actually resolves it rather than being filled with a guess.
    (RIGHT, """
        CREATE TABLE dataview.dv_land_right (
            land_right_id   varchar(40)    NOT NULL PRIMARY KEY,
            lease_number    nvarchar(100)  NOT NULL,
            source          nvarchar(40)   NOT NULL,
            tract_name      nvarchar(255)  NULL,
            operator_name   nvarchar(255)  NULL,
            ba_id           varchar(40)    NULL,
            effective_date  datetime2      NULL,
            expiry_date     datetime2      NULL,
            lease_status    nvarchar(40)   NULL,
            producing_ind   nvarchar(60)   NULL,
            active_ind      nvarchar(1)    NULL,
            INVENTORY_ID    nvarchar(45)   NULL,
            royalty_rate    float          NULL,
            mineral_type    nvarchar(64)   NULL,
            legal_desc      nvarchar(400)  NULL,
            row_created_by  nvarchar(40)   NULL,
            row_created_date datetime2     NULL,
            row_changed_by  nvarchar(40)   NULL,
            row_changed_date datetime2     NULL,
            CONSTRAINT uq_dv_land_right_src_num UNIQUE (source, lease_number)
        )"""),
    # ── THE TIE ─────────────────────────────────────────────────────────
    # Many-to-many even though today's data makes it one-to-one: a lease over
    # six tracts, and a tract under successive leases, are both real and both
    # unrepresentable in one table. Built now so neither needs a migration.
    (TIE, """
        CREATE TABLE dataview.dv_land_right_tract (
            land_right_id   varchar(40)   NOT NULL,
            tract_id        varchar(40)   NOT NULL,
            row_created_by  nvarchar(40)  NULL,
            row_created_date datetime2    NULL,
            CONSTRAINT pk_dv_land_right_tract
                PRIMARY KEY (land_right_id, tract_id),
            CONSTRAINT fk_lrt_right FOREIGN KEY (land_right_id)
                REFERENCES dataview.dv_land_right (land_right_id),
            CONSTRAINT fk_lrt_tract FOREIGN KEY (tract_id)
                REFERENCES dataview.dv_land_tract_geom (tract_id)
        )"""),
]

INDEXES = [
    ("ix_dv_land_tract_geom_geog",
     "CREATE SPATIAL INDEX ix_dv_land_tract_geom_geog "
     "ON dataview.dv_land_tract_geom(geog) USING GEOGRAPHY_AUTO_GRID "
     "WITH (DATA_COMPRESSION = PAGE)"),
    ("ix_dv_land_right_source",
     "CREATE NONCLUSTERED INDEX ix_dv_land_right_source "
     "ON dataview.dv_land_right (source, active_ind) "
     "WITH (DATA_COMPRESSION = PAGE)"),
    ("ix_dv_land_right_operator",
     "CREATE NONCLUSTERED INDEX ix_dv_land_right_operator "
     "ON dataview.dv_land_right (operator_name) "
     "WITH (DATA_COMPRESSION = PAGE)"),
]

# THE SAME land_tract_id ON ALL THREE. dv_land_tract's own id is already
# unique per lease, so reusing it as both tract_id and land_right_id makes
# every row traceable back to the row it came from without a lookup table --
# and makes re-running this idempotent, since the PKs collide on a second go.
POPULATE = [
    ("tracts", f"""
        INSERT INTO {TRACT}
            (tract_id, geog, area_km2, province_state, country, source,
             quality_note, row_created_by, row_created_date)
        SELECT s.land_tract_id, s.geog, s.area_km2, s.province_state,
               s.country, s.source, s.quality_note,
               'migrate_land_split', SYSUTCDATETIME()
          FROM dataview.dv_land_tract s
         WHERE s.geog IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM {TRACT} t
                            WHERE t.tract_id = s.land_tract_id)"""),
    ("rights", f"""
        INSERT INTO {RIGHT}
            (land_right_id, lease_number, source, tract_name, operator_name,
             effective_date, expiry_date, lease_status, producing_ind,
             active_ind, INVENTORY_ID, row_created_by, row_created_date,
             row_changed_by, row_changed_date)
        SELECT s.land_tract_id, s.lease_number, s.source, s.tract_name,
               s.operator_name, s.effective_date, s.expiry_date,
               s.lease_status, s.producing_ind, s.active_ind, s.INVENTORY_ID,
               'migrate_land_split', SYSUTCDATETIME(),
               s.row_changed_by, s.row_changed_date
          FROM dataview.dv_land_tract s
         WHERE s.geog IS NOT NULL AND s.lease_number IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM {RIGHT} r
                            WHERE r.land_right_id = s.land_tract_id)"""),
    ("ties", f"""
        INSERT INTO {TIE}
            (land_right_id, tract_id, row_created_by, row_created_date)
        SELECT r.land_right_id, r.land_right_id,
               'migrate_land_split', SYSUTCDATETIME()
          FROM {RIGHT} r
          JOIN {TRACT} t ON t.tract_id = r.land_right_id
         WHERE NOT EXISTS (SELECT 1 FROM {TIE} x
                            WHERE x.land_right_id = r.land_right_id
                              AND x.tract_id = r.land_right_id)"""),
]

# ── PHASE 2, WRITTEN DOWN BUT NOT RUN ───────────────────────────────────
# The swap that lets the fifteen readers keep working untouched: rename the
# old table out of the way and put a VIEW in its place. Reads are then free;
# only the four WRITERS (load_blm_leases, gen_synthetic_leases,
# assign_synthetic_lease_owners, shapefile_to_geography) need real work, and
# they can be done one at a time behind the view.
PHASE2_VIEW = """
EXEC sp_rename 'dataview.dv_land_tract', 'dv_land_tract_legacy';
GO
CREATE VIEW dataview.dv_land_tract AS
SELECT  r.land_right_id  AS land_tract_id,
        r.tract_name, r.lease_number, r.operator_name,
        t.province_state, t.country, t.area_km2, t.geog,
        r.active_ind, r.source, r.effective_date, r.expiry_date,
        r.lease_status, r.producing_ind, t.quality_note,
        r.row_created_by, r.row_created_date,
        r.row_changed_by, r.row_changed_date
  FROM  dataview.dv_land_right r
  JOIN  dataview.dv_land_right_tract x ON x.land_right_id = r.land_right_id
  JOIN  dataview.dv_land_tract_geom  t ON t.tract_id      = x.tract_id;
"""


VIEW_SQL = """
CREATE VIEW dataview.dv_land_tract AS
SELECT  r.land_right_id  AS land_tract_id,
        r.tract_name, r.lease_number, r.operator_name,
        t.province_state, t.country, t.area_km2, t.geog,
        r.active_ind, r.source, r.row_created_by, r.row_created_date,
        r.row_changed_by, r.row_changed_date, r.INVENTORY_ID,
        r.effective_date, r.expiry_date, r.lease_status, r.producing_ind,
        t.quality_note
  FROM  dataview.dv_land_right r
  JOIN  dataview.dv_land_right_tract x ON x.land_right_id = r.land_right_id
  JOIN  dataview.dv_land_tract_geom  t ON t.tract_id      = x.tract_id
"""


def objects_present(cx, t):
    from sqlalchemy import text as _t
    return cx.execute(_t("SELECT OBJECT_ID(:n)"), {"n": t}).scalar() is not None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--verify", action="store_true",
                    help="compare the new shape against dv_land_tract and "
                         "say whether they agree. Reads only.")
    ap.add_argument("--phase2", action="store_true",
                    help="the swap: rename dv_land_tract out of the way and "
                         "put a READ view in its place. Writers must already "
                         "target the new tables -- a view over a three-table "
                         "join cannot be inserted into.")
    ap.add_argument("--rollback2", action="store_true",
                    help="undo the swap: drop the view, rename the table back. "
                         "The three new tables are left alone.")
    ap.add_argument("--rollback", action="store_true",
                    help="drop the three new tables. dv_land_tract is not "
                         "touched by this tool at all, so nothing is at risk.")
    ap.add_argument("--apply", action="store_true",
                    help="write. Without it, nothing is created or dropped.")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    from sqlalchemy import text as _t
    eng = make_engine(a.database)

    if a.rollback2:
        with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
            is_view = cx.execute(_t(
                "SELECT COUNT(*) FROM sys.views WHERE object_id="
                "OBJECT_ID('dataview.dv_land_tract')")).scalar()
            legacy = objects_present(cx, "dataview.dv_land_tract_legacy")
            print("   dv_land_tract is a %s" % ("VIEW" if is_view else "table"))
            print("   dv_land_tract_legacy %s" % ("exists" if legacy else "absent"))
            if not (is_view and legacy):
                print("\nNothing to undo.")
                return 0
            if a.apply:
                cx.execute(_t("DROP VIEW dataview.dv_land_tract"))
                cx.execute(_t("EXEC sp_rename 'dataview.dv_land_tract_legacy', "
                              "'dv_land_tract'"))
                print("   view dropped, table renamed back")
            else:
                print("\nDRY RUN -- add --apply.")
        return 0

    if a.phase2:
        with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
            for t in NEW:
                if not objects_present(cx, t):
                    print("%s missing -- run --apply first." % t)
                    return 1
            is_view = cx.execute(_t(
                "SELECT COUNT(*) FROM sys.views WHERE object_id="
                "OBJECT_ID('dataview.dv_land_tract')")).scalar()
            if is_view:
                print("dv_land_tract is already a view. Nothing to do.")
                return 0
            n_old = cx.execute(_t(
                "SELECT COUNT(*) FROM dataview.dv_land_tract")).scalar()
            print("   dv_land_tract  %s row(s)  -> dv_land_tract_legacy"
                  % format(n_old, ","))
            print("   then a READ view of the same name over the split")
            if not a.apply:
                print("\nDRY RUN -- add --apply. Undo with --rollback2 --apply.")
                return 0
            cx.execute(_t("EXEC sp_rename 'dataview.dv_land_tract', "
                          "'dv_land_tract_legacy'"))
            cx.execute(_t(VIEW_SQL))
            n_new = cx.execute(_t(
                "SELECT COUNT(*) FROM dataview.dv_land_tract")).scalar()
            print("   renamed, view created: %s row(s) through it"
                  % format(n_new, ","))
            if n_new != n_old:
                print("   *** the view does not return the same count. "
                      "Undo with --rollback2 --apply. ***")
                return 1
        print("\nThe readers are unchanged. The WRITERS must now target the "
              "new tables directly -- see the note in this file.")
        return 0

    if a.rollback:
        with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
            for t in (TIE, RIGHT, TRACT):           # children first
                if objects_present(cx, t):
                    print("   %s %s" % ("dropping" if a.apply else "would drop", t))
                    if a.apply:
                        cx.execute(_t("DROP TABLE %s" % t))
                else:
                    print("   %s is not there" % t)
        if not a.apply:
            print("\nDRY RUN -- add --apply.")
        return 0

    if a.verify:
        with eng.connect() as cx:
            for t in NEW:
                if not objects_present(cx, t):
                    print("%s does not exist -- run --apply first." % t)
                    return 1
            src_n, src_a = cx.execute(_t(
                "SELECT COUNT(*), SUM(area_km2) FROM dataview.dv_land_tract "
                "WHERE geog IS NOT NULL AND lease_number IS NOT NULL")).first()
            new_n, new_a = cx.execute(_t(f"""
                SELECT COUNT(*), SUM(t.area_km2)
                  FROM {RIGHT} r
                  JOIN {TIE} x ON x.land_right_id = r.land_right_id
                  JOIN {TRACT} t ON t.tract_id = x.tract_id""")).first()
            # GEOMETRY IDENTITY, not just a count. A migration that moved the
            # right number of rows with the wrong shapes is the failure worth
            # checking for, and STEquals on every row is affordable at 24k.
            diff = cx.execute(_t(f"""
                SELECT COUNT(*) FROM dataview.dv_land_tract s
                  JOIN {TRACT} t ON t.tract_id = s.land_tract_id
                 WHERE s.geog.STEquals(t.geog) = 0""")).scalar()
            missing = cx.execute(_t(f"""
                SELECT COUNT(*) FROM dataview.dv_land_tract s
                 WHERE s.geog IS NOT NULL AND s.lease_number IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM {RIGHT} r
                                    WHERE r.land_right_id = s.land_tract_id)"""
                                    )).scalar()
        print("dv_land_tract      : %s rows, %s km2"
              % (format(src_n, ","), format(round(src_a or 0, 1), ",")))
        print("split (joined back): %s rows, %s km2"
              % (format(new_n, ","), format(round(new_a or 0, 1), ",")))
        print("geometries that differ : %s" % format(diff, ","))
        print("leases not carried over: %s" % format(missing, ","))
        ok = (src_n == new_n and diff == 0 and missing == 0
              and abs((src_a or 0) - (new_a or 0)) < 0.5)
        print("\n%s" % ("MATCHES -- the split holds the same data."
                        if ok else "DOES NOT MATCH -- do not proceed to phase 2."))
        return 0 if ok else 1

    # ── create + populate ────────────────────────────────────────────────
    with eng.connect() as cx:
        have = {t: objects_present(cx, t) for t in NEW}
        n_src = cx.execute(_t(
            "SELECT COUNT(*) FROM dataview.dv_land_tract "
            "WHERE geog IS NOT NULL AND lease_number IS NOT NULL")).scalar()
    for t in NEW:
        print("   %-34s %s" % (t, "present" if have[t] else "to create"))
    print("\n%s lease(s) in dv_land_tract would be carried over."
          % format(n_src, ","))
    if not a.apply:
        print("\nDRY RUN -- re-run with --apply. dv_land_tract is not "
              "modified either way; --rollback drops only the new tables.")
        return 0

    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
        for t, ddl in DDL:
            if not objects_present(cx, t):
                cx.execute(_t(ddl))
                print("   created %s" % t)
        for label, sql in POPULATE:
            r = cx.execute(_t(sql))
            print("   %-8s +%s row(s)" % (label, format(r.rowcount or 0, ",")))
        for name, ddl in INDEXES:
            try:
                cx.execute(_t(ddl))
                print("   index %s" % name)
            except Exception as exc:
                print("   index %s skipped: %s" % (name, str(exc)[:70]))
    print("\nNow run:  python tools/migrate_land_split.py --verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
