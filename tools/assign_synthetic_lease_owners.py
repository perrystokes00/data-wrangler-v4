r"""Give the BLM leases a synthetic operator, so Owner colouring has something
to colour. Dry run by default; fully reversible with --clear.

READ THIS BEFORE RUNNING IT. These operators are INVENTED. BLM publishes no
lessee in MLRS -- operator_name is NULL on all 4,584 real lease records, and
that NULL is the truth. Filling it makes a confident wrong value, which is the
failure this codebase names first: a wrong value plots, exports and gets
quoted, while a missing one is visible. So:

  * every row written is stamped row_changed_by = 'SYNTH_OWNER', which is what
    makes --clear exact rather than a guess at which rows were touched;
  * the names are deliberately fictional and already live in
    geography_layers.LEASE_OWNER_COLOURS, so they cannot be mistaken for a
    real lessee and they get hand-picked distinct colours instead of the
    CRC32 fallback, which collides at eight groups;
  * nothing else is touched. source stays BLM_MLRS, because the GEOMETRY and
    the dates are still BLM's -- only the operator is fiction, and pretending
    otherwise would lose the real provenance to hide the fake attribute.

WHY DETERMINISTIC. crc32(lease_number) means the same lease gets the same
operator on every run, on any machine, before and after a reload. Random
assignment would reshuffle the map on every re-run and make a screenshot
impossible to reproduce -- and hash() is salted per process, so it is not an
option (the same trap lease_colour already documents).

The weights are shaped like a real basin: two dominant holders, a middle, and
a tail. Even splits look synthetic at a glance, which rather defeats the
purpose of a demo.

    python tools/assign_synthetic_lease_owners.py           # what it would do
    python tools/assign_synthetic_lease_owners.py --apply   # write it
    python tools/assign_synthetic_lease_owners.py --clear --apply   # undo
"""
import argparse
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

STAMP = "SYNTH_OWNER"

# (name, weight). Names must match geography_layers.LEASE_OWNER_COLOURS keys
# case-insensitively, or they fall through to the colliding CRC palette.
OPERATORS = [
    ("Sweetwater Resources LLC",        28),
    ("Powder River Royalty Partners",   24),
    ("Bighorn Basin Energy Co",         18),
    ("Casper Ridge Petroleum",          13),
    ("Salt Creek Minerals Trust",        9),
    ("Naval Petroleum Reserve Operations", 5),
    ("Unleased Federal Acreage",         3),
]
_TOTAL = sum(w for _n, w in OPERATORS)


def operator_for(lease_number: str) -> str:
    """Stable operator for a lease, weighted. Same input, same answer, always."""
    bucket = zlib.crc32(str(lease_number).encode("utf-8")) % _TOTAL
    run = 0
    for name, weight in OPERATORS:
        run += weight
        if bucket < run:
            return name
    return OPERATORS[-1][0]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--clear", action="store_true",
                    help="remove them again: NULLs operator_name on every row "
                         "stamped %s, and nothing else." % STAMP)
    ap.add_argument("--apply", action="store_true",
                    help="write. Without it, nothing is changed.")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    from sqlalchemy import text as t
    eng = make_engine(a.database)

    if a.clear:
        with eng.connect() as cx:
            n = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_land_tract "
                             "WHERE row_changed_by = :s"), {"s": STAMP}).scalar()
        print("%s row(s) carry the %s stamp." % (format(n, ","), STAMP))
        if not n:
            print("Nothing to clear.")
            return 0
        if not a.apply:
            print("DRY RUN -- re-run with --clear --apply to undo.")
            return 0
        with eng.begin() as cx:
            cx.execute(t("UPDATE dataview.dv_land_tract "
                         "SET operator_name = NULL, row_changed_by = NULL, "
                         "    row_changed_date = NULL "
                         "WHERE row_changed_by = :s"), {"s": STAMP})
        print("cleared %s row(s)." % format(n, ","))
        return 0

    with eng.connect() as cx:
        rows = cx.execute(t("""
            SELECT lease_number, operator_name, row_changed_by
              FROM dataview.dv_land_tract
             WHERE source = 'BLM_MLRS' AND lease_number IS NOT NULL""")).fetchall()

    # NEVER OVERWRITE SOMETHING REAL. A row that already carries an operator
    # this tool did not write is data from somewhere else, and guessing over
    # it is exactly the wrong-value failure this file opens with.
    todo, kept = [], 0
    for lease_number, operator, changed_by in rows:
        if operator and changed_by != STAMP:
            kept += 1
            continue
        todo.append((lease_number, operator_for(lease_number)))

    from collections import Counter
    spread = Counter(op for _ln, op in todo)
    print("%s BLM lease(s); %s would get a synthetic operator, %s left alone "
          "(already carry a real one).\n"
          % (format(len(rows), ","), format(len(todo), ","), format(kept, ",")))
    for name, _w in OPERATORS:
        n = spread.get(name, 0)
        print("   %-36s %5s  %5.1f%%"
              % (name, format(n, ","), 100.0 * n / max(len(todo), 1)))

    if not a.apply:
        print("\nDRY RUN -- re-run with --apply. Undo with --clear --apply.")
        return 0

    # ONE STATEMENT PER OPERATOR, NOT ONE PER LEASE. 4,584 single-row
    # UPDATEs is a SET being sent as statements -- the thing this repo has
    # measured three times and written down. Grouping by the value being
    # written makes it seven round trips instead of 4,584.
    by_op = {}
    for lease_number, op in todo:
        by_op.setdefault(op, []).append(lease_number)
    with eng.begin() as cx:
        for op, lns in by_op.items():
            # Chunked because a parameter list is not unbounded, and a
            # statement that works at 600 rows and fails at 3,000 is the
            # kind of thing that only shows up on someone else's data.
            for i in range(0, len(lns), 500):
                chunk = lns[i:i + 500]
                params = {"op": op, "s": STAMP}
                names = []
                for j, ln in enumerate(chunk):
                    params["l%d" % j] = ln
                    names.append(":l%d" % j)
                cx.execute(t(
                    "UPDATE dataview.dv_land_tract "
                    "   SET operator_name = :op, row_changed_by = :s, "
                    "       row_changed_date = SYSUTCDATETIME() "
                    " WHERE lease_number IN (%s)" % ",".join(names)), params)
    print("\nwrote %s row(s), all stamped %s." % (format(len(todo), ","), STAMP))
    print("The map's lease GeoJSON rebuilds itself: its signature includes the "
          "newest row stamp, which this just moved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
