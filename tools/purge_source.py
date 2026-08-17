"""
purge_source.py — remove a body of loaded data, safely
=======================================================

Deletes rows belonging to a SOURCE (e.g. 'SYNTH') across every dv_ table,
children before parents, with a dry run first.

    # what would go — writes nothing
    python -m dataview.tools.purge_source --server localhost\\SQLEXPRESS ^
        --database DataView_Demo --source SYNTH

    # take a copy, then delete
    python -m dataview.tools.purge_source --server ... --database ... ^
        --source SYNTH --backup-schema purge_20260802 --apply

TWO WAYS A ROW BELONGS TO A SOURCE, and both matter
---------------------------------------------------
1. ITS OWN source column says so.
2. ITS WELL came from that source, even though the row itself is marked
   something else. This is the one that bites: promote relabels rows to
   'CATALOG' on the way up, so a document-derived survey attached to a
   synthetic well carries the wrong badge. Deleting only case 1 leaves
   orphans behind that then block the parent delete — or worse, don't.

Default is BOTH. --only-source restricts to case 1 if that is really what
you want.

ORDER
-----
Children first, computed from the live foreign-key graph — never a
hand-kept list. A table nobody references is deleted last; dv_well is the
last of all.

WHAT IT WILL NOT DO
-------------------
Touch a table with no `source` column and no `uwi` column: it cannot tell
which of those rows are yours. Reference tables (dv_r_*) are excluded
outright — a status code is not well data, and deleting one breaks every
well that used it.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

_SAFE = re.compile(r"^[0-9A-Za-z_\- ]+$")

# Tables this tool will not touch whatever the arguments say.
PROTECTED = {
    "dv_column_map",             # the synonym store / fingerprint recall
    "dv_global_file_catalog",    # the load ledger (provenance, not data)
    "dv_target_attribute",       # schema metadata the fit pre-flight reads
}


def get_engine(server, database, driver="ODBC Driver 17 for SQL Server"):
    from sqlalchemy import create_engine, event
    url = (f"mssql+pyodbc://@{server}/{database}"
           f"?driver={driver.replace(' ', '+')}&trusted_connection=yes")
    eng = create_engine(url)

    @event.listens_for(eng, "connect")
    def _settings(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("SET ARITHABORT ON; SET NOCOUNT ON;")
        finally:
            cur.close()
    return eng


def inspect(engine, schema):
    """Tables in the schema, their columns, and the FK graph."""
    from sqlalchemy import text
    with engine.connect() as cx:
        cols = {}
        for t, c in cx.execute(text(
                "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = :s"), {"s": schema}):
            cols.setdefault(t, set()).add(c.lower())
        fks = []
        for child, parent in cx.execute(text("""
                SELECT ct.name, pt.name
                FROM sys.foreign_keys fk
                JOIN sys.tables ct ON ct.object_id = fk.parent_object_id
                JOIN sys.tables pt ON pt.object_id = fk.referenced_object_id
                JOIN sys.schemas s ON s.schema_id = ct.schema_id
                WHERE s.name = :s"""), {"s": schema}):
            if child != parent:
                fks.append((child, parent))
    return cols, fks


def delete_order(tables, fks):
    """Children before parents. A cycle (or a self-reference) degrades to
    alphabetical for the tables involved rather than failing — the delete
    is guarded by a transaction either way."""
    parents = {t: set() for t in tables}
    for child, parent in fks:
        if child in parents and parent in tables:
            parents[child].add(parent)
    out, placed = [], set()
    while True:
        ready = sorted(t for t in tables
                       if t not in placed
                       and not (parents[t] - placed - {t}))
        # a table whose parents are all placed can be deleted only AFTER
        # its own children, so build parents-last and reverse at the end
        if not ready:
            break
        out.extend(ready)
        placed.update(ready)
    out.extend(sorted(t for t in tables if t not in placed))   # cycles
    return list(reversed(out))


def scope_clause(cols, source, only_source, well_sub):
    """The WHERE that selects this source's rows in one table, or None if
    the table cannot be attributed."""
    has_src = "source" in cols
    has_uwi = "uwi" in cols
    if only_source:
        return f"source = '{source}'" if has_src else None
    if has_src and has_uwi:
        return f"(source = '{source}' OR uwi IN ({well_sub}))"
    if has_src:
        return f"source = '{source}'"
    if has_uwi:
        return f"uwi IN ({well_sub})"
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Delete a source's data across dv_ tables, children first.")
    ap.add_argument("--server", required=True)
    ap.add_argument("--database", required=True)
    ap.add_argument("--driver", default="ODBC Driver 17 for SQL Server")
    ap.add_argument("--schema", default="dataview")
    ap.add_argument("--source", required=True, help="e.g. SYNTH")
    ap.add_argument("--prefix", default="dv_",
                    help="only tables starting with this (default dv_)")
    ap.add_argument("--only-source", action="store_true",
                    help="rows whose OWN source matches; do NOT include "
                         "other rows belonging to those wells")
    ap.add_argument("--backup-schema",
                    help="copy every affected table's doomed rows here first")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; without it, report only")
    a = ap.parse_args(argv)

    if not _SAFE.match(a.source):
        ap.error(f"refusing {a.source!r} as a source value")

    from sqlalchemy import text
    engine = get_engine(a.server, a.database, a.driver)
    cols, fks = inspect(engine, a.schema)

    # NEVER DELETE THE LEARNED STATE. dv_r_* are reference codes — delete one
    # and every well that used it fails on reload. dv_column_map is the
    # synonym store: months of approved column mappings and the fingerprint
    # recall that makes a repeat load ask nothing. Neither has a `source`
    # column, so the attribution rule would skip them anyway — but "it would
    # probably be skipped" is not a guarantee, and this is a delete.
    tables = sorted(t for t in cols
                    if t.lower().startswith(a.prefix.lower())
                    and not t.lower().startswith("dv_r_")
                    and t.lower() not in PROTECTED)
    well_sub = (f"SELECT uwi FROM {a.schema}.dv_well "
                f"WHERE source = '{a.source}'")

    order = delete_order(tables, fks)
    plan, skipped = [], []
    with engine.connect() as cx:
        n_wells = cx.execute(text(
            f"SELECT COUNT(*) FROM {a.schema}.dv_well WITH (NOLOCK) "
            f"WHERE source = '{a.source}'")).scalar() or 0
        for t in order:
            where = scope_clause(cols[t], a.source, a.only_source, well_sub)
            if where is None:
                if "source" not in cols[t] and "uwi" not in cols[t]:
                    skipped.append(t)
                continue
            try:
                n = cx.execute(text(
                    f"SELECT COUNT(*) FROM {a.schema}.{t} WITH (NOLOCK) "
                    f"WHERE {where}")).scalar() or 0
            except Exception as e:
                skipped.append(f"{t} (count failed: {str(e)[:60]})")
                continue
            if n:
                plan.append((t, where, n))

    print(f"\nsource '{a.source}' in {a.schema} — "
          f"{n_wells:,} well(s) in dv_well")
    if not a.only_source:
        print("scope: rows marked with this source, PLUS every row belonging "
              "to those wells")
    else:
        print("scope: rows whose OWN source column matches (--only-source)")
    print(f"\n{'table':34} {'rows':>10}")
    total = 0
    for t, _w, n in plan:
        print(f"{t:34} {n:10,}")
        total += n
    print(f"{'TOTAL':34} {total:10,}")
    print(f"\nprotected, never touched: {', '.join(sorted(PROTECTED))}")
    if skipped:
        print(f"\nnot attributable (no source and no uwi column), left alone:")
        print("   " + ", ".join(skipped[:12])
              + (f" … +{len(skipped) - 12}" if len(skipped) > 12 else ""))

    if not a.apply:
        print("\nDRY RUN — nothing deleted. Add --apply (and ideally "
              "--backup-schema) to proceed.")
        return 0
    if not plan:
        print("\nNothing to delete.")
        return 0

    # one transaction: a failure part-way leaves the database as it was
    with engine.begin() as cx:
        if a.backup_schema:
            if not _SAFE.match(a.backup_schema):
                ap.error("bad backup schema name")
            cx.execute(text(
                f"IF SCHEMA_ID('{a.backup_schema}') IS NULL "
                f"EXEC('CREATE SCHEMA {a.backup_schema}')"))
            for t, where, _n in plan:
                cx.execute(text(
                    f"SELECT * INTO {a.backup_schema}.{t} "
                    f"FROM {a.schema}.{t} WHERE {where}"))
            print(f"\nbacked up {len(plan)} table(s) into {a.backup_schema}")
        done = 0
        for t, where, _n in plan:                 # children first
            r = cx.execute(text(
                f"DELETE FROM {a.schema}.{t} WHERE {where}")).rowcount or 0
            print(f"  deleted {r:9,} from {t}")
            done += r
    print(f"\n{done:,} row(s) deleted.")
    if a.backup_schema:
        print(f"A copy is in schema {a.backup_schema} — drop it when you are "
              f"satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
