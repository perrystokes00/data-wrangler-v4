#!/usr/bin/env python3
r"""copy_reference_data.py — copy reference + spatial DATA between databases on
the same SQL Server instance, so a freshly-rebuilt DataView_Demo has the exact
same reference values DataView does.

seed_references.py seeds *standard* values; this copies DataView's *actual* rows
so the demo matches byte-for-byte and the pipeline's promote stage holds the same
rows in both (the FK governance parks anything whose reference value isn't
present). Pair it with the DDL reload:

    drop/create DataView_Demo  ->  load DDL  ->  copy_reference_data.py

What it copies (all in the `dataview` schema, override with --tables):
    every dv_r_*  +  dv_country, dv_province_state, dv_county

How: a single connection to the TARGET runs cross-database INSERT…SELECT from
[Source].dataview.<tbl>, deduped on the primary key (WHERE NOT EXISTS) so it's
idempotent — safe to re-run. Tables are copied in FK order via a multi-pass loop
(a child table simply succeeds on a later pass once its parent is populated).
IDENTITY columns are handled with SET IDENTITY_INSERT. Computed columns are
skipped automatically.

Usage:
    python copy_reference_data.py --source DataView --target DataView_Demo
    python copy_reference_data.py --dry-run
    python copy_reference_data.py --no-spatial
    python copy_reference_data.py --tables dv_r_uom dv_r_source dv_country
"""
import argparse
import pyodbc

SPATIAL = ["dv_country", "dv_province_state", "dv_county"]


def _conn(server, database):
    return pyodbc.connect(
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={server};DATABASE={database};Trusted_Connection=yes",
        autocommit=True)


def _ref_tables(cur, db, include_spatial):
    extra = ("OR t.name IN ({})".format(
        ",".join(f"'{x}'" for x in SPATIAL)) if include_spatial else "")
    cur.execute(
        f"SELECT s.name, t.name FROM [{db}].sys.tables t "
        f"JOIN [{db}].sys.schemas s ON s.schema_id = t.schema_id "
        f"WHERE s.name = 'dataview' "
        f"  AND (t.name LIKE 'dv[_]r[_]%' {extra}) "
        f"ORDER BY t.name")
    return [(r[0], r[1]) for r in cur.fetchall()]


def _insertable_cols(cur, db, sch, tbl):
    """Column names that can be inserted into (skip computed columns)."""
    cur.execute(
        f"SELECT c.name FROM [{db}].sys.columns c "
        f"JOIN [{db}].sys.tables t  ON t.object_id = c.object_id "
        f"JOIN [{db}].sys.schemas s ON s.schema_id = t.schema_id "
        f"WHERE s.name = ? AND t.name = ? AND c.is_computed = 0 "
        f"ORDER BY c.column_id", sch, tbl)
    return [r[0] for r in cur.fetchall()]


def _pk_cols(cur, db, sch, tbl):
    cur.execute(
        f"SELECT col.name FROM [{db}].sys.indexes i "
        f"JOIN [{db}].sys.index_columns ic "
        f"     ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
        f"JOIN [{db}].sys.columns col "
        f"     ON col.object_id = ic.object_id AND col.column_id = ic.column_id "
        f"JOIN [{db}].sys.tables t  ON t.object_id = i.object_id "
        f"JOIN [{db}].sys.schemas s ON s.schema_id = t.schema_id "
        f"WHERE i.is_primary_key = 1 AND s.name = ? AND t.name = ? "
        f"ORDER BY ic.key_ordinal", sch, tbl)
    return [r[0] for r in cur.fetchall()]


def _has_identity(cur, db, sch, tbl):
    cur.execute(
        f"SELECT COUNT(*) FROM [{db}].sys.identity_columns ic "
        f"JOIN [{db}].sys.tables t  ON t.object_id = ic.object_id "
        f"JOIN [{db}].sys.schemas s ON s.schema_id = t.schema_id "
        f"WHERE s.name = ? AND t.name = ?", sch, tbl)
    return cur.fetchone()[0] > 0


def copy_table(cur, target_db, src_db, sch, tbl, dry_run):
    tcols = _insertable_cols(cur, target_db, sch, tbl)
    scols = set(_insertable_cols(cur, src_db, sch, tbl))
    cols = [c for c in tcols if c in scols]          # intersection, target order
    if not cols:
        return ("skip", 0)
    pk = _pk_cols(cur, target_db, sch, tbl)
    collist = ", ".join(f"[{c}]" for c in cols)
    src = f"[{src_db}].[{sch}].[{tbl}]"
    dst = f"[{sch}].[{tbl}]"
    if pk:
        on = " AND ".join(f"t.[{c}] = s.[{c}]" for c in pk)
    else:                                            # no PK: dedupe on all cols
        on = " AND ".join(
            f"(t.[{c}] = s.[{c}] OR (t.[{c}] IS NULL AND s.[{c}] IS NULL))"
            for c in cols)
    where_new = f"WHERE NOT EXISTS (SELECT 1 FROM {dst} t WHERE {on})"

    if dry_run:
        cur.execute(f"SELECT COUNT(*) FROM {src} s {where_new}")
        return ("would", cur.fetchone()[0])

    ins = (f"INSERT INTO {dst} ({collist}) "
           f"SELECT {collist} FROM {src} s {where_new}")
    ident = _has_identity(cur, target_db, sch, tbl)
    if ident:
        cur.execute(f"SET IDENTITY_INSERT {dst} ON")
    try:
        cur.execute(ins)
        n = cur.rowcount or 0
    finally:
        if ident:
            cur.execute(f"SET IDENTITY_INSERT {dst} OFF")
    return ("ok", n)


def main():
    ap = argparse.ArgumentParser(
        description="Copy dv_r_* + spatial reference data between databases.")
    ap.add_argument("--server", default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--source", default="DataView_Demo")
    ap.add_argument("--target", default="DataView_Demo")
    ap.add_argument("--tables", nargs="*", default=None,
                    help="explicit dataview table names to copy (overrides the "
                         "dv_r_* + spatial default)")
    ap.add_argument("--no-spatial", action="store_true",
                    help="skip dv_country/dv_province_state/dv_county")
    ap.add_argument("--dry-run", action="store_true",
                    help="report row counts that would be inserted, change nothing")
    a = ap.parse_args()

    tgt = _conn(a.server, a.target)
    cur = tgt.cursor()

    if a.tables:
        tables = [("dataview", t) for t in a.tables]
    else:
        tables = _ref_tables(cur, a.target, include_spatial=not a.no_spatial)
    print(f"[PLAN] {len(tables)} table(s) {a.source} -> {a.target}")
    if not tables:
        print("No dv_r_* tables found in target — is the schema loaded?")
        return

    # Multi-pass so FK parents populate before children, without a topo sort.
    pending = list(tables)
    done = 0
    total_rows = 0
    last_err = None
    while pending:
        progressed = False
        still = []
        for sch, tbl in pending:
            try:
                kind, n = copy_table(cur, a.target, a.source, sch, tbl, a.dry_run)
                verb = "would insert" if kind == "would" else (
                    "inserted" if kind == "ok" else "skipped (no shared cols)")
                print(f"   {sch}.{tbl:<28} {verb} {n:,}")
                done += 1
                total_rows += n
                progressed = True
            except pyodbc.Error as e:
                last_err = e
                still.append((sch, tbl))     # most likely an FK parent not yet copied
        if not progressed:
            print(f"[ERROR] {len(still)} table(s) could not be copied "
                  f"(unresolved FK or error):")
            for sch, tbl in still:
                print(f"         {sch}.{tbl}")
            if last_err:
                print(f"   last error: {last_err}")
            break
        pending = still

    print(f"[DONE] {done} table(s), "
          f"{total_rows:,} row(s) {'would be ' if a.dry_run else ''}copied"
          + ("  (dry-run)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
