"""
export_table_csv.py — write a dv_* table out as a CSV the Data Assistant can
reload.

    python tools/export_table_csv.py --table dataview.dv_reservoir ^
        --out "C:\\...\\training\\synthetic_data\\dv_reservoir.csv"

    python tools/export_table_csv.py --table dataview.dv_reservoir --out x.csv --all

Read-only against the database.

WHY NOT bcp
-----------
`bcp queryout` writes no header row, and the loader identifies a file by the
FINGERPRINT OF ITS COLUMN NAMES — a headerless file is a file it has never
seen. It also has no quoting, so a remark containing a comma silently becomes
two columns. The csv module handles both.

WHAT IS EXCLUDED BY DEFAULT, AND WHY
------------------------------------
  * geography / geometry — a CSV cannot carry these usefully, and the loader
    refuses derived columns anyway (a file has no business supplying a value
    the database computes from other columns).
  * the audit block — row_created_by / row_created_date / row_changed_by /
    row_changed_date. Promote stamps these on the way in. Carrying them
    through a CSV means a reload claims the row was created by whoever
    created the ORIGINAL, on the original date, which is false about the row
    now in the table.

  --all keeps everything, for a straight backup rather than a reload source.

WHAT IS DELIBERATELY KEPT
-------------------------
  reservoir_id and any other key. The zone rows carry that id, so a reload
  that minted NEW ids would leave every zone pointing at a pool that no
  longer exists. The id must be the same id.
"""
from __future__ import annotations

import argparse
import csv
import sys
import os

# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SPATIAL = {"geography", "geometry"}
AUDIT = {"row_created_by", "row_created_date", "row_changed_by", "row_changed_date"}


def columns(cur, schema: str, table: str) -> list[tuple[str, str]]:
    cur.execute("""
        SELECT c.name, ty.name
        FROM sys.columns c
        JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        WHERE c.object_id = OBJECT_ID(?)
        ORDER BY c.column_id
    """, f"{schema}.{table}")
    return [(r[0], r[1].lower()) for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--table", required=True, help="schema.table, e.g. dataview.dv_reservoir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--all", action="store_true",
                    help="keep spatial and audit columns too (backup, not reload)")
    ap.add_argument("--where", default="", help="optional WHERE clause without the keyword")
    a = ap.parse_args()

    if "." not in a.table:
        print("--table must be schema.table", file=sys.stderr)
        return 2
    schema, table = a.table.split(".", 1)

    from dataview.import_data.bulk_dir_loader import make_engine
    eng = make_engine(a.server, a.database)
    raw = eng.raw_connection()
    cur = raw.cursor()

    cols = columns(cur, schema, table)
    if not cols:
        print(f"no such table: {a.table}", file=sys.stderr)
        return 2

    keep, dropped = [], []
    for name, ty in cols:
        if not a.all and (ty in SPATIAL or name.lower() in AUDIT):
            dropped.append(f"{name} ({'spatial' if ty in SPATIAL else 'audit'})")
        else:
            keep.append(name)

    sql = "SELECT " + ", ".join(f"[{c}]" for c in keep) + f" FROM [{schema}].[{table}] WITH (NOLOCK)"
    if a.where:
        sql += " WHERE " + a.where
    cur.execute(sql)

    n = 0
    # newline="" is required or the csv module writes \r\r\n on Windows
    with open(a.out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(keep)
        for row in cur.fetchall():
            out = []
            for v in row:
                if v is None:
                    out.append("")                      # NULL, not the text "None"
                elif hasattr(v, "isoformat"):
                    out.append(v.isoformat())           # dates unambiguous, never locale
                else:
                    out.append(str(v).rstrip() if isinstance(v, str) else v)
            w.writerow(out)
            n += 1

    raw.close()
    print(f"{a.out}  —  {n:,} row(s), {len(keep)} column(s)")
    if dropped:
        print("  excluded: " + " · ".join(dropped))
        print("  (--all keeps them; they are excluded because the database"
              " computes or stamps them)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
