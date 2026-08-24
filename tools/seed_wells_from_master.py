r"""Seed the PARENT wells that staged children reference but dv_well lacks.

WHY THIS IS A TOOL AND NOT A PIPELINE STEP. Seeding a parent is a DECISION,
not a step (Perry's law). A child row whose well is missing is HELD, on
purpose, and it stays held until somebody decides where the parent should come
from. This tool makes that decision cheap to carry out and impossible to make
by accident: it reads only what the reference master already states, invents
nothing, and does nothing at all without --apply.

THE CASE IT WAS BUILT FOR (24 Aug). Teapot's tops, production and directional
surveys reference 60 wells that TeapotDomeWellHeaders02-09-10.xlsx does not
contain, so 37 prod entities, 21,204 volumes, 6 survey headers, 1,492 stations
and 599 tops sat held. 55 of those 60 wells are fully described in
WELL_REF.well_ref.well_master_gold -- name, operator, coordinates -- so the
data was never missing, only unjoined. Deleting the children would have thrown
away real measurements belonging to documented wells.

WHAT IT WILL NOT DO
  * invent a coordinate, a name, or an operator. Every value copied is one the
    master states; a column the master leaves NULL stays NULL. A confident
    wrong position plots and gets quoted -- missing is safer.
  * register a reference value. dv_well.source is FK-constrained to
    dv_r_source, and creating a domain value ARMS A GUARD elsewhere, so an
    unregistered --source is refused rather than seeded.
  * overwrite. The insert is NOT EXISTS-guarded, so whichever load owns a well
    keeps it -- first one in wins, the same rule promote follows.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A Windows console is cp1252 and this prints a non-ASCII ellipsis, so without
# this the tool mojibakes -- or, on a stricter console, dies while REPORTING,
# which is the shape selftest hit: it worked when passing and crashed when it
# had something to say.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# The staged children that key on dv_well.uwi, and the column each keys with.
# NOT derived from INFORMATION_SCHEMA on purpose: this is the set whose parents
# we are willing to seed, and widening it should be a visible edit rather than
# a side effect of some other table gaining a uwi column.
CHILDREN = [("stg.dv_well_formation_top", "API_NUMBER"),
            ("stg.dv_prod_entity", "UWI"),
            ("stg.dv_well_dir_srvy_hdr", "UWI"),
            ("stg.dv_well_dir_srvy_sta", "UWI")]

MASTER = "WELL_REF.well_ref.well_master_gold"

# The UWI-14 pad, the same transform promote applies at the write point. Both
# sides of every comparison here use it -- padding one side only is what made
# a perfect load report -1317, and it is the single most repeated mistake in
# this codebase's history.
def _pad(expr):
    return (f"CASE WHEN NULLIF(LTRIM(RTRIM({expr})), '') IS NULL THEN NULL "
            f"ELSE LEFT(CONCAT(LTRIM(RTRIM({expr})), REPLICATE('0', 14)), 14) END")


def _orphan_sql():
    """Distinct wells referenced by staged children that dv_well does not have."""
    parts = [f"SELECT DISTINCT {_pad(col)} AS u FROM {tbl}" for tbl, col in CHILDREN]
    return (f"SELECT x.u FROM ({' UNION '.join(parts)}) x "
            f"WHERE x.u IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM dataview.dv_well w "
            f"WHERE LTRIM(RTRIM(w.uwi)) = x.u)")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Seed parent wells from the reference master. Dry run unless --apply.")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--driver", default="ODBC Driver 17 for SQL Server")
    ap.add_argument("--source", default=None,
                    help="dv_r_source code to stamp. Must already be registered; "
                         "omitted leaves source NULL, which is what the other "
                         "Teapot wells carry.")
    ap.add_argument("--created-by", default="SEED_FROM_WELL_REF",
                    help="row_created_by stamp. Says these wells came from the "
                         "reference master, not from a loaded file.")
    ap.add_argument("--apply", action="store_true",
                    help="write. Without it, nothing is inserted.")
    a = ap.parse_args(argv)

    import sqlalchemy as sa
    url = (f"mssql+pyodbc://{a.server}/{a.database}"
           f"?driver={a.driver.replace(' ', '+')}&trusted_connection=yes")
    eng = sa.create_engine(url)

    with eng.connect() as cx:
        def q(_sql, **p):
            return list(cx.execute(sa.text(_sql), p))

        if a.source:
            ok = q("SELECT 1 FROM dataview.dv_r_source WHERE source = :s", s=a.source)
            if not ok:
                codes = ", ".join(r[0] for r in
                                  q("SELECT source FROM dataview.dv_r_source ORDER BY source"))
                print(f"REFUSED: '{a.source}' is not registered in dv_r_source.\n"
                      f"  Promote holds any row whose coded value is unregistered, so "
                      f"seeding one here would push the problem downstream.\n"
                      f"  Registered: {codes}")
                return 2

        orphans = [r[0] for r in q(_orphan_sql())]
        if not orphans:
            print("No parentless wells -- every staged child already has its well.")
            return 0

        inlist = ",".join("'" + o.replace("'", "''") + "'" for o in orphans)
        found = q(f"""SELECT g.uwi14, g.well_name, g.operator_name, g.field_name,
                             g.surface_latitude, g.surface_longitude,
                             g.county, g.province_state, g.total_depth, g.spud_date
                        FROM {MASTER} g
                       WHERE LTRIM(RTRIM(g.uwi14)) IN ({inlist})""")
        have = {r[0].strip() for r in found}
        missing = [o for o in orphans if o not in have]

        print(f"{len(orphans)} well(s) referenced by staged children but absent from dv_well")
        print(f"   {len(found)} found in {MASTER}")
        print(f"   {len(missing)} found nowhere\n")

        # SAMPLE BEFORE APPLY. A coordinate backfill once nearly wrote 1,436
        # confidently wrong positions; the twenty-row sample caught it.
        print("   sample of what would be written:")
        for r in found[:10]:
            print(f"      {r[0]}  {str(r[1])[:22]:24s} {str(r[2])[:20]:22s} "
                  f"{r[4]}, {r[5]}")
        if len(found) > 10:
            print(f"      … and {len(found) - 10} more")
        if missing:
            print("\n   no master row -- these stay held, which is correct:")
            for m in missing:
                print(f"      {m}")

        # How much this unblocks, counted per child rather than asserted.
        print("\n   child rows this would unblock:")
        for tbl, col in CHILDREN:
            n = q(f"SELECT COUNT(*) FROM {tbl} s WHERE {_pad('s.' + col)} IN "
                  f"({','.join(chr(39) + h + chr(39) for h in have)})")[0][0] if have else 0
            print(f"      {tbl:32s} {n:,}")

        if not a.apply:
            print("\nDRY RUN -- nothing written. Re-run with --apply to seed.")
            return 0

    cols = ["uwi", "well_name", "operator", "field", "surface_latitude",
            "surface_longitude", "county", "province_state", "total_depth",
            "spud_date", "active_ind", "row_created_by", "row_created_date"]
    live = {c.lower() for c in
            [r[0] for r in list(eng.connect().execute(sa.text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA='dataview' AND TABLE_NAME='dv_well'")))]}
    use = [c for c in cols if c in live]
    if a.source and "source" in live:
        use.append("source")

    n = 0
    with eng.begin() as cx:
        for r in found:
            vals = {"uwi": r[0].strip(), "well_name": r[1], "operator": r[2],
                    "field": r[3], "surface_latitude": r[4], "surface_longitude": r[5],
                    "county": r[6], "province_state": r[7], "total_depth": r[8],
                    "spud_date": r[9], "active_ind": "Y",
                    "row_created_by": a.created_by, "source": a.source}
            names = [c for c in use if c != "row_created_date"]
            ph = ", ".join(f":{c}" for c in names)
            extra = ", row_created_date" if "row_created_date" in use else ""
            xval = ", SYSUTCDATETIME()" if "row_created_date" in use else ""
            n += cx.execute(sa.text(
                f"INSERT INTO dataview.dv_well ({', '.join(names)}{extra}) "
                f"SELECT {ph}{xval} WHERE NOT EXISTS "
                f"(SELECT 1 FROM dataview.dv_well w WHERE LTRIM(RTRIM(w.uwi)) = :uwi)"),
                {k: vals.get(k) for k in set(names) | {"uwi"}}).rowcount or 0
    print(f"\nSeeded {n} well(s). Re-run Promote -- the held children lift on their own.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
