r"""What a load left behind, and why — the terminal view of the same answer.

The Data Assistant's "After the load" panel and this script both call
dataview.import_data.load_health, so they cannot disagree. Answering one
question two ways is how MIRROR_TABLES and LINEAGE drifted, how demo_reset and
clear_catalog drifted, and how two loaders came to mint provenance
differently — three times in one week.

Why the panel is not enough on its own: the inline "N row(s) held" notice lives
in session state and is gone on the next rerun, and this runs headless, in a
cron job, or against a database whose app is not running.

    python tools/held_rows.py
    python tools/held_rows.py --table dv_well_formation_top   # one table, with causes
    python tools/held_rows.py --provenance                    # orphaned lineage too
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Rows a load staged that never reached their target.")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--table", help="only this staging table, and explain why")
    ap.add_argument("--provenance", action="store_true",
                    help="also list rows citing a file the catalog cannot resolve")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    from dataview.import_data import load_health as lh
    engine = make_engine(a.database)

    rows = lh.held_report(engine)
    if a.table:
        rows = [r for r in rows if r["table"].lower() == a.table.lower()]
        if not rows:
            print("No staged rows in stg.%s" % a.table)
            return 2
    if not rows:
        print("Nothing staged. Run a load first.")
        return 0

    print("%-30s %9s %9s %9s" % ("staging table", "staged", "landed", "HELD"))
    print("-" * 62)
    total = 0
    for r in rows:
        held = r["held"]
        total += held or 0
        note = ""
        if r["note"]:
            note = "   " + r["note"]
        elif held:
            note = "   <--"
        print("%-30s %9s %9s %9s%s"
              % (r["table"], format(r["staged"], ","),
                 "?" if r["landed"] is None else format(r["landed"], ","),
                 "?" if held is None else format(held, ","), note))
        if a.table and held:
            causes = lh.held_causes(engine, r["table"])
            print("\n   why those %s row(s) did not land:" % format(held, ","))
            if causes:
                for c in causes:
                    print("      %-22s -> %-24s %8s unmatched, e.g. %s"
                          % (c["child_col"], c["parent"], format(c["unmatched"], ","),
                             ", ".join(repr(v) for v in c["examples"][:3])))
            else:
                print("      No FK explains these. Every parent resolves, so the rows")
                print("      were refused for another reason -- most often a DUPLICATE")
                print("      KEY: promote is insert-only, so a repeat is skipped.")
            print()

    print("-" * 62)
    if total:
        print("%s row(s) staged but never landed." % format(total, ","))
        print("Held is recoverable: load the missing parent, then re-run the load.")
        if not a.table:
            print("Re-run with --table <name> to see which parent is missing.")
    else:
        print("Nothing held — every staged row reached its target.")

    if a.provenance:
        print()
        orph = lh.orphan_provenance(engine)
        if not orph:
            print("Provenance: every row resolves to a catalogued file.")
        else:
            n = sum(o["rows"] for o in orph)
            print("Provenance: %s row(s) cite a file the catalog cannot resolve."
                  % format(n, ","))
            for o in orph:
                print("   %-26s %s  %s row(s)"
                      % (o["table"], o["inventory_id"][:16], format(o["rows"], ",")))
            print("   Repair with tools/repair_sidecar_provenance.py --dir <folder> --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
