"""
reconcile_reference_csv.py — what would happen if these PPDM reference CSVs
were loaded into DataView. Reports only; writes nothing.

    python reconcile_reference_csv.py --in "C:\\...\\reference_csv"
    python reconcile_reference_csv.py --in <dir> --ddl        # also print DDL
                                                              # for missing tables

WHY LOOK FIRST
--------------
Three of these domains ALREADY EXIST in DataView and are already referenced by
loaded rows:

  dv_r_source            35 codes, written by the loader and the Standards Manager
  dv_r_well_status       18 codes
  dv_r_well_profile_type  5 codes, hand-seeded — and the petroleum pack's
                         well_type splitter WRITES those exact strings
                         (HORIZONTAL / VERTICAL / DIRECTIONAL / SIDETRACK /
                         MULTILATERAL)

A wholesale replace on any of them is a data-loss event with a delay on it:
promote HOLDS a row whose coded value is not in its reference table, so codes
that quietly disappear do not raise an error — rows simply stop arriving, and
the reason shows up in a held-count nobody reads until later.

The profile one is sharper still. If PPDM spells them HORIZ / DIR / H, then
loading that list and switching over leaves every value the recogniser emits
unresolvable, and the well headers freed by the splitter are held again.

So this reports, per file:
  * the target table, and whether it exists
  * codes the CSV would ADD
  * codes already in the table that the CSV does NOT contain — the local
    ones, which a replace would destroy
  * for those, whether any dv_* row is USING them, which is the difference
    between an untidy list and a broken load
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import sys

DV_PREFIX = "dv_"          # PPDM r_well_status  ->  DataView dv_r_well_status


def read_csv(path: str):
    """Header + rows, tolerant of the BOM Excel writes."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return [], []
    return [h.strip() for h in rows[0]], [r for r in rows[1:] if any(c.strip() for c in r)]


def table_exists(cur, schema, table) -> bool:
    cur.execute("SELECT OBJECT_ID(?)", f"{schema}.{table}")
    return cur.fetchone()[0] is not None


def key_column(cur, schema, table) -> str | None:
    """A PPDM reference table is keyed on the code itself: prefer the primary
    key, else the first column."""
    cur.execute("""
        SELECT TOP 1 c.name
        FROM sys.columns c
        LEFT JOIN sys.index_columns ic
               ON ic.object_id = c.object_id AND ic.column_id = c.column_id
              AND ic.index_id = (SELECT index_id FROM sys.indexes
                                  WHERE object_id = c.object_id AND is_primary_key = 1)
        WHERE c.object_id = OBJECT_ID(?)
        ORDER BY CASE WHEN ic.column_id IS NOT NULL THEN 0 ELSE 1 END, c.column_id
    """, f"{schema}.{table}")
    r = cur.fetchone()
    return r[0] if r else None


def live_codes(cur, schema, table, col) -> set[str]:
    cur.execute(f"SELECT DISTINCT LTRIM(RTRIM(CONVERT(nvarchar(200), [{col}]))) "
                f"FROM [{schema}].[{table}] WITH (NOLOCK)")
    return {r[0] for r in cur.fetchall() if r[0]}


def users_of(cur, code_col: str) -> list[tuple[str, str]]:
    """Every dataview table carrying a column of this name — i.e. the places a
    code from this domain is actually stored."""
    cur.execute("""
        SELECT t.name, c.name
        FROM sys.columns c
        JOIN sys.tables t ON t.object_id = c.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = 'dataview' AND c.name = ?
          AND t.name NOT LIKE 'dv[_]r[_]%'
    """, code_col)
    return [(r[0], r[1]) for r in cur.fetchall()]


def in_use(cur, table, col, codes) -> set[str]:
    """Which of these codes appear in that table's column."""
    if not codes:
        return set()
    marks = ", ".join("?" for _ in codes)
    try:
        cur.execute(f"SELECT DISTINCT LTRIM(RTRIM(CONVERT(nvarchar(200),[{col}]))) "
                    f"FROM [dataview].[{table}] WITH (NOLOCK) "
                    f"WHERE LTRIM(RTRIM(CONVERT(nvarchar(200),[{col}]))) IN ({marks})",
                    *list(codes))
        return {r[0] for r in cur.fetchall() if r[0]}
    except Exception:
        return set()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--in", dest="folder", required=True)
    ap.add_argument("--ddl", action="store_true", help="print CREATE TABLE for missing targets")
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.folder, "r_*.csv")))
    if not files:
        print(f"no r_*.csv under {a.folder}", file=sys.stderr)
        return 2

    from dataview.import_data.bulk_dir_loader import make_engine
    raw = make_engine(a.server, a.database).raw_connection()
    cur = raw.cursor()

    print(f"{len(files)} reference file(s) under {a.folder}\n")
    new_tables, merges, conflicts = [], [], []

    for path in files:
        stem = os.path.splitext(os.path.basename(path))[0]      # r_well_status
        target = DV_PREFIX + stem                               # dv_r_well_status
        header, rows = read_csv(path)
        csv_codes = {r[0].strip() for r in rows if r and r[0].strip()}

        exists = table_exists(cur, "dataview", target)
        print(f"── {stem}.csv  →  dataview.{target}")
        print(f"   csv: {len(rows)} row(s), {len(header)} column(s): {', '.join(header[:6])}"
              + (" …" if len(header) > 6 else ""))

        if not exists:
            print("   TABLE DOES NOT EXIST — a straight create-and-load, nothing at risk")
            new_tables.append((target, header, len(rows)))
            print()
            continue

        col = key_column(cur, "dataview", target)
        have = live_codes(cur, "dataview", target, col)
        add = sorted(csv_codes - have)
        local = sorted(have - csv_codes)
        print(f"   TABLE EXISTS: {len(have)} code(s), keyed on [{col}]")
        print(f"   would ADD    : {len(add):>3}" + (f"  {', '.join(add[:8])}" if add else ""))
        print(f"   LOCAL ONLY   : {len(local):>3}" + (f"  {', '.join(local[:8])}" if local else ""))

        if local:
            # are any of the local-only codes actually in use?
            used_anywhere = {}
            for tbl, ccol in users_of(cur, col):
                hit = in_use(cur, tbl, ccol, local)
                if hit:
                    used_anywhere[tbl] = sorted(hit)
            if used_anywhere:
                print("   ⚠ LOCAL CODES IN USE — a replace would strand these rows:")
                for tbl, hits in used_anywhere.items():
                    print(f"       dataview.{tbl}: {', '.join(hits)}")
                conflicts.append((target, used_anywhere))
            else:
                print("   (no dv_* row uses the local-only codes)")
        merges.append((target, len(add), len(local)))
        print()

    print("=" * 70)
    print(f"{len(new_tables)} new table(s) — create and load, no risk")
    print(f"{len(merges)} existing table(s) — MERGE ONLY, never replace")
    if conflicts:
        print(f"⚠ {len(conflicts)} table(s) hold local codes that loaded rows depend on:")
        for t, _ in conflicts:
            print(f"    {t}")
        print("  Loading these as a REPLACE removes codes that rows reference.")
        print("  Promote HOLDS a row whose code is unregistered rather than failing,")
        print("  so the damage shows up later as rows that stopped arriving.")

    if a.ddl and new_tables:
        print("\n" + "=" * 70)
        print("-- DDL for the missing targets. Column types are a starting point:")
        print("-- widths come from the CSV's own content, not from PPDM's spec.")
        for target, header, _n in new_tables:
            key = header[0] if header else "code"
            cols = ",\n    ".join(
                f"[{h}] nvarchar(200) NULL" for h in header[1:]) or "[remark] nvarchar(2000) NULL"
            print(f"""
IF OBJECT_ID('dataview.{target}','U') IS NULL
CREATE TABLE dataview.{target} (
    [{key}] nvarchar(40) NOT NULL CONSTRAINT PK_{target} PRIMARY KEY,
    {cols},
    active_ind char(1) NOT NULL CONSTRAINT DF_{target}_act DEFAULT 'Y',
    row_created_by nvarchar(30) NULL,
    row_created_date datetime2 NULL,
    row_changed_by nvarchar(30) NULL,
    row_changed_date datetime2 NULL
);""")

    raw.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
