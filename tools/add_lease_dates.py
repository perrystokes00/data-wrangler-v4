r"""Give dv_land_tract the temporal columns a lease needs.

A LEASE IS A TEMPORAL THING and the table could not say so. It carried
active_ind and nothing else, which is enough for 34 synthetic tracts laid out
as a non-overlapping lattice and not enough for real ones: Natrona County's
9,785 BLM leases sum to 9.2 MILLION acres in a 3.4-million-acre county,
because 9,089 of them are closed and a century of leases stacks on the same
ground. Without dates the table can only ever mean "everything ever leased".

    effective_date  when the lease took effect        (BLM EFF_DT)
    expiry_date     when it ends, NULL if held        (BLM EXP_DT)
    lease_status    Authorized / Closed / Pending     (BLM CSE_DISP)
    producing_ind   BLM PRDCNG, e.g. "Held by Actual Production"
    quality_note    BLM QLTY -- the geocoding caveat, kept rather than dropped

THE QUALITY NOTE IS NOT DECORATION. BLM says of a sampled Teapot lease:
"MIDPOINT, DOES NOT MATCH PM ANGLES". The polygon is geocoded from a legal
description and is indicative, not survey-grade. Dropping that column would
make an approximate boundary look authoritative, which is the failure this
codebase names as wrong-is-worse-than-missing.

Idempotent: each column is added only if absent, so re-running is a no-op.
Dry run by default.

    python tools/add_lease_dates.py
    python tools/add_lease_dates.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TABLE = ("dataview", "dv_land_tract")
COLUMNS = [
    ("effective_date", "datetime2 NULL"),
    ("expiry_date",    "datetime2 NULL"),
    ("lease_status",   "nvarchar(40) NULL"),
    ("producing_ind",  "nvarchar(60) NULL"),
    ("quality_note",   "nvarchar(400) NULL"),
]


def existing(conn):
    from sqlalchemy import text as _t
    return {r[0].lower() for r in conn.execute(_t(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t"),
        {"s": TABLE[0], "t": TABLE[1]})}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    from dataview.core.dw_utils import make_engine
    from sqlalchemy import text as _t
    eng = make_engine(a.database)

    with eng.connect() as cx:
        have = existing(cx)
    todo = [(c, d) for c, d in COLUMNS if c.lower() not in have]

    print("\n%s.%s" % TABLE)
    for c, d in COLUMNS:
        print("   %-16s %-16s %s"
              % (c, d, "present" if c.lower() in have else "TO ADD"))
    if not todo:
        print("\nAll present -- nothing to do.")
        return 0
    if not a.apply:
        print("\nDRY RUN -- re-run with --apply to alter the table.")
        return 0

    with eng.begin() as cx:
        for c, d in todo:
            # One ALTER per column: a failure then names the column that
            # failed instead of rolling back a batch and saying "syntax".
            cx.execute(_t("ALTER TABLE %s.%s ADD [%s] %s" % (TABLE[0], TABLE[1], c, d)))
            print("   added %s" % c)
    with eng.connect() as cx:
        have = existing(cx)
    print("\nnow present: %s"
          % ", ".join(c for c, _d in COLUMNS if c.lower() in have))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
