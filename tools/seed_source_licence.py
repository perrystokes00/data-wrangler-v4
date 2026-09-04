"""Keep dataview.dv_source_licence and build/state_licence.csv in step.

    python tools/seed_source_licence.py --export   # database -> the CSV
    python tools/seed_source_licence.py --seed     # the CSV -> database
    python tools/seed_source_licence.py            # compare, change nothing

WHY THIS EXISTS. dv_source_licence decides which states may appear in the
public master. It is research -- forty agencies' published terms, read one at
a time, with the URL and the date each was checked -- and until now it lived
ONLY in the database. A table nothing can rebuild is a table that quietly
becomes the sole copy of a fortnight's work, and `build/` is gitignored (the
standard Python packaging rule), so none of the evidence was under version
control either. The CSV is the record; the database is a materialisation of
it.

IT IS DELIBERATELY NOT A dv_r_* TABLE. Those are owned by the Reference
Tables app and a loader refuses on an unregistered code rather than seeding
one. This is evidence, not a controlled domain, so it lives outside that
namespace and this tool may write it.

THE CLASSES, and what each one licenses:

    FREE_EXPLICIT     terms explicitly permit a derived aggregate
    FREE_DISCLAIMER   free to use, with an accuracy disclaimer to reproduce
    RESTRICTED        reproduction or redistribution is restricted
    AGGREGATOR        obtained via an aggregator, not the agency
    UNVERIFIED        no published terms found -- ASK BEFORE PUBLISHING

Only FREE_* states are built into well_master_public_v2. UNVERIFIED is not a
soft yes: Arkansas's metadata names a distribution-liability clause that the
document does not contain, and the agency's own site returns 403 to automated
fetches, so a human has to read it. Letters are in build/Email/.
"""
import argparse
import csv
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text          # noqa: E402

CSV_PATH = "build/state_licence.csv"
TABLE = "dataview.dv_source_licence"
COLS = ["province_state", "agency", "licence_class", "attribution",
        "terms_url", "checked_date", "wells_at_check"]


def engine(database="DataView_Demo"):
    cs = ("DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost\\SQLEXPRESS;"
          "DATABASE=%s;Trusted_Connection=yes;" % database)
    return create_engine("mssql+pyodbc:///?odbc_connect="
                         + urllib.parse.quote_plus(cs))


def from_db(e):
    with e.connect() as c:
        return [{k: ("" if v is None else str(v)) for k, v in zip(COLS, tuple(r))}
                for r in c.execute(text(
                    "SELECT %s FROM %s ORDER BY province_state"
                    % (", ".join(COLS), TABLE)))]


def from_csv():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as fh:
        return [{k: (r.get(k) or "").strip() for k in COLS}
                for r in csv.DictReader(fh)]


def export(e):
    rows = from_db(e)
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    print("wrote %s -- %d states" % (CSV_PATH, len(rows)))


def seed(e):
    rows = from_csv()
    if not rows:
        raise SystemExit("%s is missing or empty" % CSV_PATH)
    with e.begin() as c:
        for r in rows:
            r = dict(r)
            r["wells_at_check"] = int(r["wells_at_check"] or 0)
            c.execute(text("DELETE FROM %s WHERE province_state = :province_state"
                           % TABLE), {"province_state": r["province_state"]})
            c.execute(text("INSERT INTO %s (%s) VALUES (%s)"
                           % (TABLE, ", ".join(COLS),
                              ", ".join(":" + k for k in COLS))), r)
    print("seeded %d states into %s" % (len(rows), TABLE))


def compare(e):
    db = {r["province_state"]: r for r in from_db(e)}
    cs = {r["province_state"]: r for r in from_csv()}
    if not cs:
        print("%s does not exist yet -- run --export" % CSV_PATH)
        return 1
    only_db = sorted(set(db) - set(cs))
    only_cs = sorted(set(cs) - set(db))
    diff = [s for s in sorted(set(db) & set(cs))
            if any(db[s][k] != cs[s][k] for k in COLS)]
    print("database %d states · csv %d states" % (len(db), len(cs)))
    if only_db:
        print("   in the DATABASE only : %s" % ", ".join(only_db))
    if only_cs:
        print("   in the CSV only      : %s" % ", ".join(only_cs))
    for s in diff:
        for k in COLS:
            if db[s][k] != cs[s][k]:
                print("   %s %-14s db=%r csv=%r"
                      % (s, k, db[s][k][:40], cs[s][k][:40]))
    bad = len(only_db) + len(only_cs) + len(diff)
    print("\n%s" % ("IN STEP" if not bad else
                    "*** %d DIFFERENCE(S) -- export or seed ***" % bad))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--export", action="store_true")
    g.add_argument("--seed", action="store_true")
    a = ap.parse_args()
    e = engine()
    if a.export:
        export(e)
        return 0
    if a.seed:
        seed(e)
        return 0
    return compare(e)


if __name__ == "__main__":
    sys.exit(main())
