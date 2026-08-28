r"""Register WOGCC's published well status and class codes in dv_r_*.

WHY THIS IS NEEDED. Loading Converse County from the reference master, the FK
sanitiser NULLed 23,417 of 26,800 well_status values -- 87% -- because
dv_r_well_status had never seen WOGCC's codes. EP alone is 14,604 wells. The
guard was right to refuse them; what was missing was the registration.

EVERY MEANING BELOW IS WOGCC'S OWN, from their published Codes and Symbols
page, not inferred from the letters. That distinction is the whole point: "WP"
could plausibly be Water Producer or Well Plugged; it is "Waiting on Approval".
A registration invented from an abbreviation is a wrong value that will be
believed, exported and quoted, which is worse than the blank it replaces.

    https://wogcc.wyo.gov/public-resources/help-with-website/codes-and-symbols

CODES OBSERVED IN THE DATA BUT NOT PUBLISHED BY WOGCC ARE LEFT ALONE, and the
tool names them. On Converse and Campbell those are well_status DU, and
well_type NA, 01 and WS. Something has to be decided about each; guessing is
not that decision.

REGISTERING A REFERENCE VALUE ARMS A GUARD (CLAUDE.md): promote holds any row
whose coded value is unregistered, and the guard fires for dv_r_* names. So
this is a deliberate, reviewable step with a dry run, not something a loader
does on the way past.

    python tools/register_wogcc_codes.py
    python tools/register_wogcc_codes.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CREATED_BY = "WOGCC_CODES"
REMARK = "WOGCC published code (wogcc.wyo.gov Codes and Symbols)"

# Well File Status Codes, verbatim from WOGCC.
STATUS = [
    ("PO",  "Producing Oil Well"),
    ("PG",  "Producing Gas Well"),
    ("DH",  "Dry Hole"),
    ("SI",  "Shut-In"),
    ("TA",  "Temporarily Abandoned"),
    ("PA",  "Permanently Abandoned"),
    ("AI",  "Active Injector"),
    ("DR",  "Dormant"),
    ("NI",  "Notice of Intent to Abandon"),
    ("SR",  "Subsequent Report of Abandonment"),
    ("EP",  "Expired Permit"),
    ("AP",  "Permit to Drill"),
    ("SP",  "Well Spudded"),
    ("WP",  "Waiting on Approval"),
    ("UNK", "Unknown"),
    ("NR",  "No Report"),
    ("SO",  "Suspended Operations"),
    ("NO",  "Denied Permit"),
    ("WD",  "Withdrawn Permit"),
]

# Well Classification Codes, verbatim from WOGCC.
TYPE = [
    ("O",  "Oil Well"),
    ("G",  "Gas Well"),
    ("C",  "Condensate"),
    ("I",  "Injector Well"),
    ("S",  "Source Well"),
    ("AP", "Active Permit"),
    ("D",  "Disposal"),
    ("M",  "Monitor Well"),
    ("MW", "Monitor Well (Not for Form 2 Reporting)"),
    ("ST", "Strat Test"),
    ("GS", "Gas Storage"),
    ("GO", "Gas Orphaned"),
    ("OO", "Oil Orphaned"),
    ("DO", "Disposal Orphaned"),
    ("IO", "Injector Orphaned"),
    ("MO", "Monitor Well Orphaned"),
    ("LW", "LandOwner Water Well"),
]

TABLES = [("dv_r_well_status", "well_status", STATUS),
          ("dv_r_well_type",   "well_type",   TYPE)]


def observed(conn, col, where):
    """{code: n} seen in the master for a scope, so the report is concrete."""
    from sqlalchemy import text as _t
    return {str(r[0]).strip(): int(r[1]) for r in conn.execute(_t(
        "SELECT %s, COUNT(*) FROM WELL_REF.well_ref.well_master_gold "
        " WHERE %s AND %s IS NOT NULL GROUP BY %s" % (col, where, col, col)))}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--scope", default="province_state='WY'",
                    help="master rows to report coverage against")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    from dataview.core.dw_utils import make_engine
    from sqlalchemy import text as _t
    eng = make_engine(a.database)
    todo_all = []

    with eng.connect() as cx:
        for table, col, codes in TABLES:
            have = {str(r[0]).strip().upper() for r in cx.execute(
                _t("SELECT [%s] FROM dataview.%s" % (col, table)))}
            seen = observed(cx, "raw_" + col, a.scope)
            todo = [(c, m) for c, m in codes if c.upper() not in have]
            known = {c.upper() for c, _m in codes}
            unpublished = {c: n for c, n in seen.items()
                           if c.upper() not in known and c.upper() not in have}

            print("\n%s  (%d registered now)" % (table, len(have)))
            for c, m in codes:
                n = seen.get(c, 0)
                state = "present" if c.upper() in have else "TO ADD"
                print("   %-5s %-42s %8s wells   %s"
                      % (c, m, format(n, ","), state))
            if unpublished:
                print("   -- OBSERVED BUT NOT PUBLISHED BY WOGCC, left alone:")
                for c, n in sorted(unpublished.items(), key=lambda x: -x[1]):
                    print("      %-5s %8s wells   ?? no published meaning"
                          % (c, format(n, ",")))
            todo_all.append((table, col, todo))

    n_add = sum(len(t) for _tb, _c, t in todo_all)
    if not n_add:
        print("\nNothing to add -- every published code is already registered.")
        return 0
    if not a.apply:
        print("\n%d code(s) to add. DRY RUN -- re-run with --apply." % n_add)
        return 0

    with eng.begin() as cx:
        for table, col, todo in todo_all:
            for code, meaning in todo:
                cx.execute(_t(
                    "INSERT INTO dataview.%s ([%s], short_name, long_name, "
                    "remark, active_ind, row_created_by, row_created_date) "
                    "VALUES (:c, :c, :m, :r, 'Y', :b, SYSUTCDATETIME())"
                    % (table, col)),
                    {"c": code, "m": meaning, "r": REMARK, "b": CREATED_BY})
                print("   + %-18s %-5s %s" % (table, code, meaning))
    print("\nregistered %d code(s)." % n_add)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
