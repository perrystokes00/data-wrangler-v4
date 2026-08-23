"""
whose_id.py — which file, and which algorithm, produced an orphaned id.

    python tools/whose_id.py --scan C:\\Users\\perry\\OneDrive\\Documents\\PPDM

Read-only. Never writes.

WHY BOTH QUESTIONS AT ONCE
--------------------------
An orphaned INVENTORY_ID has exactly two innocent explanations, and they need
different fixes:

  1 · the file MOVED or was renamed  -> the id can never be recomputed from it
  2 · the id was minted by an OLDER algorithm  -> the file is still right there,
      and the id simply predates the day the three minting functions were
      collapsed into one

Searching for the current algorithm alone cannot tell those apart: both come
back "not found". So this tries every algorithm the codebase has used, and
reports WHICH one matched — which turns "unidentified" into either "here is
the file" or "here is the file AND here is the version it was catalogued
under".
"""
from __future__ import annotations

import argparse
import hashlib
import ntpath
import os
import sys


# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Every id scheme this codebase has minted, newest first. Keeping the dead
# ones here on purpose: a stored id is a historical artefact, and reading it
# requires knowing what was true when it was written.
def _canonical(p):     return ntpath.normpath(str(p).strip()).upper()

ALGORITHMS = {
    "current (normpath+UPPER, UTF-16-LE)":
        lambda p: hashlib.sha1(_canonical(p).encode("utf-16-le")).hexdigest().upper(),
    "legacy file_gate (UPPER, UTF-16-LE, no normpath)":
        lambda p: hashlib.sha1(str(p).upper().encode("utf-16-le")).hexdigest().upper(),
    "legacy pipeline_run (UPPER, UTF-8)":
        lambda p: hashlib.sha1(str(p).upper().encode("utf-8")).hexdigest().upper(),
    "legacy file_inventory (original case, UTF-8)":
        lambda p: hashlib.sha1(str(p).encode("utf-8")).hexdigest()[:40].upper(),
    "legacy file_inventory + normpath":
        lambda p: hashlib.sha1(
            os.path.normpath(str(p)).encode("utf-8")).hexdigest()[:40].upper(),
}


def orphan_ids(engine):
    from sqlalchemy import text as _t
    with engine.connect() as con:
        rows = con.execute(_t("""
            SELECT w.inventory_id, COUNT(*) AS rows
            FROM dataview.dv_well w WITH (NOLOCK)
            WHERE w.inventory_id IS NOT NULL
              AND NOT EXISTS (SELECT 1
                              FROM file_catalog.GLOBAL_FILE_CATALOG g WITH (NOLOCK)
                              WHERE g.INVENTORY_ID = w.inventory_id)
            GROUP BY w.inventory_id
            ORDER BY COUNT(*) DESC
        """)).fetchall()
    return [(r[0], int(r[1])) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--scan", required=True, help="folder to search (searched deeply)")
    ap.add_argument("--ext", default="",
                    help="restrict to these extensions, e.g. .csv,.xlsx (default: all)")
    a = ap.parse_args()

    from dataview.import_data.bulk_dir_loader import make_engine
    eng = make_engine(a.server, a.database)

    orphans = orphan_ids(eng)
    if not orphans:
        print("no orphaned inventory_id")
        return 0
    print(f"orphaned id(s): {len(orphans)}")
    for iid, n in orphans:
        print(f"  {iid}  cited by {n:,} dv_well row(s)")
    wanted = {iid for iid, _ in orphans}

    exts = {e.strip().lower() for e in a.ext.split(",") if e.strip()}
    print(f"\nsearching {a.scan}"
          f"{' for ' + ', '.join(sorted(exts)) if exts else ''} …")

    hits, seen = [], 0
    for dirpath, _d, files in os.walk(a.scan):
        for name in files:
            if exts and os.path.splitext(name)[1].lower() not in exts:
                continue
            p = os.path.join(dirpath, name)
            seen += 1
            for label, fn in ALGORITHMS.items():
                try:
                    if fn(p) in wanted:
                        hits.append((fn(p), label, p))
                except Exception:
                    pass

    print(f"files examined: {seen:,}")
    if not hits:
        print("\nNO MATCH under any algorithm — the file has been moved, renamed "
              "or deleted since it was catalogued. The row's source genuinely "
              "cannot be identified; NULLing the dangling id is more honest "
              "than leaving it pointing at nothing.")
        return 1

    print(f"\nMATCHED {len(hits)}:")
    for iid, label, p in hits:
        print(f"  {iid}\n    algorithm: {label}\n    file:      {p}")
    if any("legacy" in h[1] for h in hits):
        print("\nThe match came from a LEGACY algorithm, so the file is still "
              "there and the id simply predates the unification. Re-registering "
              "it now mints the CURRENT id — which would NOT resolve the "
              "existing dv_well rows. Either re-stamp those rows with the new "
              "id, or restore the entry under the legacy id it was written with.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
