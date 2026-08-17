"""
reconcile_orphans.py — find the files behind orphaned provenance, and put
their catalog entries back.

    python reconcile_orphans.py --scan C:\\...\\training\\synthetic_data
    python reconcile_orphans.py --scan ... --apply

Repo root. Read-only without --apply.

THE PROBLEM
-----------
`dv_well` rows carry an INVENTORY_ID naming the file they came from, but not
the path. When a catalog clear removes the GLOBAL_FILE_CATALOG entry while
KEEPING those rows, the rows are left citing a source nothing can resolve —
the invariant calls it orphaned provenance, and it is manufactured by the
clear itself: dv_* deletions are document-scoped (a CSV-derived row is
deliberately preserved) while the file_catalog schema is wiped wholesale.
Keep the rows, lose the provenance.

WHY THIS IS RECOVERABLE AT ALL
------------------------------
INVENTORY_ID is a deterministic hash of the canonical path. So for any
candidate file we can compute what its id WOULD be and compare. That turns
"which file was this?" from an unanswerable question into a lookup — and it
means the repair RESTORES the original id rather than inventing a new one,
so not a single dv_* row has to be touched.

If a file has since been moved or renamed, its id no longer matches and it
is reported as unmatched rather than guessed at. That is the honest outcome:
the row's source genuinely cannot be identified any more.
"""
from __future__ import annotations

import argparse
import os
import sys


def orphan_ids(engine) -> list[str]:
    from sqlalchemy import text as _t
    with engine.connect() as con:
        rows = con.execute(_t("""
            SELECT DISTINCT w.inventory_id
            FROM dataview.dv_well w WITH (NOLOCK)
            WHERE w.inventory_id IS NOT NULL
              AND NOT EXISTS (SELECT 1
                              FROM file_catalog.GLOBAL_FILE_CATALOG g WITH (NOLOCK)
                              WHERE g.INVENTORY_ID = w.inventory_id)
        """)).fetchall()
    return [r[0] for r in rows if r[0]]


def candidates(root: str):
    """Every file under root, with the id it would be catalogued under."""
    from dataview.core.file_identity import inventory_id
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            yield inventory_id(p), p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--scan", required=True,
                    help="folder to search for the files behind the orphans")
    ap.add_argument("--apply", action="store_true",
                    help="re-register the matched files (else report only)")
    a = ap.parse_args()

    from dataview.import_data.bulk_dir_loader import make_engine
    eng = make_engine(a.server, a.database)

    orphans = set(orphan_ids(eng))
    print(f"orphaned inventory_id(s) cited by dv_well: {len(orphans):,}")
    if not orphans:
        return 0

    if not os.path.isdir(a.scan):
        print(f"not a directory: {a.scan}", file=sys.stderr)
        return 2

    matched, seen = {}, 0
    for iid, path in candidates(a.scan):
        seen += 1
        if iid in orphans and iid not in matched:
            matched[iid] = path

    print(f"files examined under {a.scan}: {seen:,}")
    print(f"orphans matched to a file:    {len(matched):,}")
    print(f"orphans still unidentified:   {len(orphans) - len(matched):,}")
    for iid, path in sorted(matched.items(), key=lambda kv: kv[1])[:10]:
        print(f"  {iid}  {path}")
    if len(matched) > 10:
        print(f"  … and {len(matched) - 10:,} more")

    if not a.apply:
        print("\n-- report only; re-run with --apply to restore the entries")
        return 1

    from dataview.import_data.load_ledger import register_file
    ok = bad = 0
    for iid, path in matched.items():
        try:
            register_file(eng, path)
            ok += 1
        except Exception as e:                    # noqa: BLE001
            bad += 1
            print(f"  ! {path}: {type(e).__name__}: {e}")
    print(f"\nre-registered {ok:,} file(s), {bad:,} failed")
    print("re-run: python selftest.py --tier invariants "
          f"--server {a.server} --database {a.database}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
