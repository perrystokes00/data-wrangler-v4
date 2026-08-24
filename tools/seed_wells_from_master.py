r"""Seed the PARENT wells that staged children reference but dv_well lacks.

A THIN CLI. Everything it does lives in dataview.import_data.seed_from_master,
which the Phase 4 grid in the Data Assistant also calls -- one implementation,
two front doors. The reasoning about what may and may not be seeded is in that
module's docstring; this file is the command-line way to reach it.

WHY A DELIBERATE STEP AND NOT PART OF THE LOAD. Seeding a parent is a DECISION
(Perry's law). A child whose well is missing is held ON PURPOSE and stays held
until somebody decides where the parent should come from. This makes that
decision cheap to carry out and impossible to make by accident: dry run is the
default, and it prints a sample before anything is written.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A Windows console is cp1252 and this prints non-ASCII, so without this the
# tool mojibakes -- or, on a stricter console, dies while REPORTING, which is
# the shape selftest hit: it worked when passing and crashed when it had
# something to say.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dataview.import_data import seed_from_master as sfm      # noqa: E402


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
    ap.add_argument("--created-by", default=sfm.CREATED_BY,
                    help="row_created_by stamp. Says these wells came from the "
                         "reference master, not from a loaded file.")
    ap.add_argument("--apply", action="store_true",
                    help="write. Without it, nothing is inserted.")
    a = ap.parse_args(argv)

    import sqlalchemy as sa
    eng = sa.create_engine(
        f"mssql+pyodbc://{a.server}/{a.database}"
        f"?driver={a.driver.replace(' ', '+')}&trusted_connection=yes")

    with eng.connect() as cx:
        if a.source:
            ok, registered = sfm.validate_source(cx, a.source)
            if not ok:
                print(f"REFUSED: '{a.source}' is not registered in dv_r_source.\n"
                      f"  Promote holds any row whose coded value is unregistered, "
                      f"so seeding one here would push the problem downstream.\n"
                      f"  Registered: {', '.join(registered)}")
                return 2

        orphans = sfm.orphan_uwis(cx)
        if not orphans:
            print("No parentless wells -- every staged child already has its well.")
            return 0

        rows = sfm.master_rows(cx, orphans)
        have = {r["uwi"] for r in rows}
        missing = [o for o in orphans if o not in have]

        print(f"{len(orphans)} well(s) referenced by staged children "
              f"but absent from dv_well")
        print(f"   {len(rows)} found in {sfm.MASTER}")
        print(f"   {len(missing)} found nowhere\n")

        # SAMPLE BEFORE APPLY. A coordinate backfill once nearly wrote 1,436
        # confidently wrong positions; the twenty-row sample caught it.
        print("   sample of what would be written:")
        for r in rows[:10]:
            print(f"      {r['uwi']}  {str(r['well_name'])[:22]:24s} "
                  f"{str(r['operator'])[:20]:22s} "
                  f"{r['surface_latitude']}, {r['surface_longitude']}")
        if len(rows) > 10:
            print(f"      ... and {len(rows) - 10} more")
        if missing:
            print("\n   no master row -- these stay held, which is correct:")
            for m in missing:
                print(f"      {m}")

        print("\n   child rows this would unblock:")
        for tbl, n in sfm.unblocked_counts(cx, have).items():
            print(f"      {tbl:32s} {n:,}")

        if not a.apply:
            print("\nDRY RUN -- nothing written. Re-run with --apply to seed.")
            return 0

    n = sfm.seed(eng, rows, source=a.source, created_by=a.created_by)
    print(f"\nSeeded {n} well(s). Re-run Promote -- the held children lift "
          f"on their own.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
