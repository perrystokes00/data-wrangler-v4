#!/usr/bin/env python3
r"""Copy VIEW definitions from a source database to a target database on the
same SQL Server instance.

Why this exists: clone_schema.py reproduces TABLES (plus FKs and indexes), but
the federation layer lives in the `dataview_federation` schema as VIEWS
(v_well, v_well_density_r3/r4/r5, …). The clone never copied them, so
DataView_Demo is missing them and the mapping page fails with:

    Invalid object name 'dataview_federation.v_well_density_r4'.

This reads each view's exact definition from the source and (re)creates it in
the target, in dependency order via a multi-pass loop (a view that depends on
another view simply succeeds on a later pass). Idempotent: it DROPs then
recreates, so it's safe to re-run.

Usage:
    python copy_views.py --source DataView --target DataView_Demo
    python copy_views.py --schemas dataview_federation dataview
    python copy_views.py --dry-run
    # if the source views use 3-part names (DataView.schema.obj):
    python copy_views.py --rewrite-db
"""
import argparse
import pyodbc


def _conn(server, database):
    return pyodbc.connect(
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={server};DATABASE={database};Trusted_Connection=yes",
        autocommit=True)


def _views(cur, schemas):
    q = ("SELECT s.name, v.name, m.definition "
         "FROM sys.views v "
         "JOIN sys.schemas s ON s.schema_id = v.schema_id "
         "JOIN sys.sql_modules m ON m.object_id = v.object_id")
    if schemas:
        ph = ",".join("?" * len(schemas))
        q += f" WHERE s.name IN ({ph})"
        cur.execute(q, *schemas)
    else:
        cur.execute(q)
    return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser(
        description="Copy view definitions between databases on one server.")
    ap.add_argument("--server", default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--source", default="DataView_Demo")
    ap.add_argument("--target", default="DataView_Demo")
    ap.add_argument("--schemas", nargs="*", default=["dataview_federation"],
                    help="view schemas to copy (default: dataview_federation; "
                         "pass nothing after the flag to copy ALL schemas)")
    ap.add_argument("--rewrite-db", action="store_true",
                    help="rewrite 3-part references from the source DB name to "
                         "the target DB name (only needed if views use "
                         "DataView.schema.object style names)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be created without changing anything")
    a = ap.parse_args()

    schemas = a.schemas if a.schemas else None
    with _conn(a.server, a.source) as src:
        views = _views(src.cursor(), schemas)
    print(f"[READ] {len(views)} view(s) in {a.source} "
          f"schema(s): {', '.join(schemas) if schemas else 'ALL'}")
    if not views:
        print("[DONE] nothing to copy.")
        return

    if a.rewrite_db:
        # naive but bounded: only rewrite explicit 3-part prefixes
        src_pref1 = f"{a.source}."
        src_pref2 = f"[{a.source}]."
        views = [(s, n, d.replace(src_pref2, f"[{a.target}].")
                          .replace(src_pref1, f"{a.target}."))
                 for (s, n, d) in views]

    tgt = _conn(a.server, a.target)
    tcur = tgt.cursor()

    for sch in sorted({v[0] for v in views}):
        if a.dry_run:
            print(f"[schema] would ensure {sch}")
            continue
        tcur.execute(
            "IF SCHEMA_ID(?) IS NULL EXEC('CREATE SCHEMA ' + QUOTENAME(?))",
            sch, sch)

    # Multi-pass: a view that references another view succeeds once its
    # dependency exists, so we keep looping until no further progress.
    pending = list(views)
    created = 0
    last_err = None
    while pending:
        progressed = False
        still = []
        for sch, name, ddl in pending:
            if a.dry_run:
                print(f"[view] would create {sch}.{name}")
                created += 1
                progressed = True
                continue
            try:
                tcur.execute(f"DROP VIEW IF EXISTS [{sch}].[{name}]")
                tcur.execute(ddl)            # exact CREATE VIEW text from source
                created += 1
                progressed = True
                print(f"[view] {sch}.{name}")
            except pyodbc.Error as e:
                last_err = e
                still.append((sch, name, ddl))   # probably a missing dependency
        if not progressed:
            print(f"[ERROR] {len(still)} view(s) could not be created "
                  f"(unresolved dependency or invalid reference):")
            for sch, name, _ in still:
                print(f"         {sch}.{name}")
            if last_err:
                print(f"   last error: {last_err}")
            break
        pending = still

    print(f"[DONE] created/updated {created} view(s) in {a.target}"
          + ("  (dry-run)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
