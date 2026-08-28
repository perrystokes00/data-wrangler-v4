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
    ap.add_argument("--source", default=sfm.SEED_SOURCE,
                    help="dv_r_source code to stamp (default %(default)s). Must "
                         "already be registered -- a loader never seeds a domain "
                         "value. Pass --source '' to leave it NULL, which is what "
                         "this did before REF_WELLS existed: the wells then match "
                         "no query filter keyed on source, which reads as 'none "
                         "of the wells matched' and is not a key problem at all.")
    ap.add_argument("--created-by", default=sfm.CREATED_BY,
                    help="row_created_by stamp. Says these wells came from the "
                         "reference master, not from a loaded file.")
    ap.add_argument("--apply", action="store_true",
                    help="write. Without it, nothing is inserted.")
    # ── scope, and the CSV route ────────────────────────────────────────
    # Without --state/--county this stays the orphan seeder it has always
    # been: wells that staged children reference. WITH them it takes a whole
    # county, which the map panel could already do and this could not.
    #
    # --csv writes the file instead of inserting. seed() is row-by-row, each
    # guarded by its own NOT EXISTS -- right for the handful of orphans it was
    # written for, thousands of round trips for a county. The Bulk Tabular
    # Loader already owns the set-based path, so handing it a file beats
    # growing a second bulk loader here. CLAUDE.md, measured three times:
    # pyodbc for statements, bcp for sets.
    ap.add_argument("--state", default=None,
                    help="province_state code, e.g. WY. With --county, seeds "
                         "that scope instead of the orphan set.")
    ap.add_argument("--county", default=None, help="county name, e.g. Converse")
    ap.add_argument("--limit", type=int, default=30000,
                    help="most wells in one go (default 30000, the map's cap)")
    ap.add_argument("--csv", default=None,
                    help="write a CSV for the Bulk Tabular Loader instead of "
                         "inserting. Far faster for a county.")
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

        # ── scope path: a county, rather than the orphan set ─────────
        if a.state or a.county:
            tot, new = sfm.scope_count(cx, state=a.state, county=a.county)
            rows = sfm.scope_rows(cx, state=a.state, county=a.county,
                                  limit=a.limit)
            rep = sfm.sanitise_fk(cx, rows)
            print("scope %s%s: %s in master, %s not in dv_well, taking %s"
                  % (a.state or "", "/" + a.county if a.county else "",
                     format(tot, ","), format(new, ","), format(len(rows), ",")))
            for col, d in (rep or {}).items():
                print("   %s: %d value(s) NULLed as unregistered -- %s"
                      % (col, d["nulled"],
                         ", ".join("%s x%d" % kv
                                   for kv in list(d["values"].items())[:6])))
            if not rows:
                return 0
            if a.csv:
                if not a.apply:
                    print("\nDRY RUN -- re-run with --apply to write %s" % a.csv)
                    return 0
                path, n, cols = sfm.write_csv(rows, a.csv, source=a.source,
                                              created_by=a.created_by)
                print("\nwrote %s row(s) x %d column(s) to\n   %s"
                      % (format(n, ","), len(cols), path))
                print("\nLoad it with the Data Assistant (Bulk Tabular Loader) "
                      "into dataview.dv_well.\nThe header is dv_well's own "
                      "column names, so it maps without help.")
                return 0
            if not a.apply:
                print("\nDRY RUN -- re-run with --apply to insert, or add "
                      "--csv for the faster bulk route.")
                return 0
            n, present = sfm.seed(eng, rows, source=a.source,
                                  created_by=a.created_by)
            print("\ninserted %s; %s were already here."
                  % (format(n, ","), format(present, ",")))
            return 0

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

    n, already = sfm.seed(eng, rows, source=a.source, created_by=a.created_by)
    if n:
        print("")
        print(f"Seeded {n} well(s)"
              + (f" ({already} were already present)." if already else ".")
              + " Re-run Promote -- the held children lift on their own.")
    elif already:
        print("")
        print(f"Nothing to seed -- all {already} are already in dv_well. "
              f"A clean re-run, not a failure.")
    else:
        # Neither inserted nor already present: the master describes
        # none of what is left. Saying "all 0 are already in dv_well"
        # here was true and useless.
        print("")
        print(f"Nothing to seed -- the reference master describes none "
              f"of the {len(missing)} remaining well(s). They stay held, "
              f"which is correct.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
