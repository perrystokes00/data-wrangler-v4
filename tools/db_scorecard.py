"""
db_scorecard.py — what is actually IN a DataView database, and which columns lie about it.

READ ONLY. Runs SELECTs against sys catalog views and COUNT()s over user tables.
Never writes, never alters, never drops.

Four questions, answered from the data rather than from a schema document:

  1. INVENTORY      — every table, its row count, populated or empty.
  2. FILL RATES     — per column, what fraction of rows hold a non-blank value.
  3. RESTORE        — nullable columns that are 100% populated across a meaningful
                      number of rows. The NOT NULL constraint on these was real; the
                      data has never violated it. This is the evidence-based version
                      of "diff against dataview_schema_full.json" — the DB knows.
  4. SILENT GAPS    — nullable columns that are 100% EMPTY in a populated table.
                      Either the source never carries them, or they are mapping
                      nowhere and nothing says so. Same failure as a dropped source
                      column, one level down.

3 and 4 exist because relaxing a NOT NULL constraint converts a loud failure into a
silent one. This is how you find out which of those trades you actually made.

Usage:
    python db_scorecard.py --database DataView_Demo
    python db_scorecard.py --database DataView_Demo --schema dataview --report card.txt
    python db_scorecard.py --database DataView_Demo --min-rows 50 --max-cols 60
"""
import argparse
import sys

_SKIP_TYPES = {"image", "text", "ntext", "xml", "geography", "geometry",
               "hierarchyid", "sql_variant", "timestamp", "varbinary", "binary"}

# Columns the loader stamps on every row. Their fill rate says nothing about the source.
_STAMPS = {"row_created_by", "row_created_date", "row_changed_by", "row_changed_date",
           "active_ind"}


def _pick_driver():
    try:
        import pyodbc
    except ImportError:
        return None
    names = [d for d in pyodbc.drivers() if "SQL Server" in d]
    for want in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if want in names:
            return want
    return names[-1] if names else None


def make_engine(server, database, driver=None):
    import urllib.parse
    from sqlalchemy import create_engine
    drv = driver or _pick_driver()
    if not drv:
        raise RuntimeError("No SQL Server ODBC driver found. Install ODBC Driver 17 or 18.")
    odbc = (f"DRIVER={{{drv}}};SERVER={server};DATABASE={database};"
            f"Trusted_Connection=yes;TrustServerCertificate=yes")
    return create_engine("mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc))


def _q(name):
    """Bracket-quote an identifier."""
    return "[" + str(name).replace("]", "]]") + "]"


def tables_of(engine, schema):
    import pandas as pd
    from sqlalchemy import text
    sql = text("""
        SELECT t.name AS table_name,
               SUM(CASE WHEN p.index_id IN (0,1) THEN p.rows ELSE 0 END) AS approx_rows
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        LEFT JOIN sys.partitions p ON p.object_id = t.object_id
        WHERE s.name = :s
        GROUP BY t.name
        ORDER BY t.name""")
    df = pd.read_sql(sql, engine, params={"s": schema})
    return [(r.table_name, int(r.approx_rows or 0)) for r in df.itertuples()]


def columns_of(engine, schema, table):
    import pandas as pd
    from sqlalchemy import text
    sql = text("""
        SELECT c.name AS col, ty.name AS typ, c.is_nullable, c.is_identity, c.is_computed,
               CASE WHEN c.default_object_id = 0 THEN 0 ELSE 1 END AS has_default,
               CASE WHEN ic.column_id IS NULL THEN 0 ELSE 1 END AS in_pk
        FROM sys.columns c
        JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        LEFT JOIN sys.indexes i
               ON i.object_id = c.object_id AND i.is_primary_key = 1
        LEFT JOIN sys.index_columns ic
               ON ic.object_id = c.object_id AND ic.index_id = i.index_id
              AND ic.column_id = c.column_id
        WHERE c.object_id = OBJECT_ID(:t)
        ORDER BY c.column_id""")
    df = pd.read_sql(sql, engine, params={"t": f"{schema}.{table}"})
    return [{"col": r.col, "typ": r.typ, "nullable": bool(r.is_nullable),
             "identity": bool(r.is_identity), "computed": bool(r.is_computed),
             "has_default": bool(r.has_default), "in_pk": bool(r.in_pk)}
            for r in df.itertuples()]


def fill_rates(engine, schema, table, cols, max_cols=80):
    """{col: non_blank_count} plus the true row count.

    One set-based query for the whole table. Character columns treat '' and whitespace
    as empty — a blank string is not a value, and counting it as one would hide exactly
    the gaps this tool exists to find.
    """
    import pandas as pd
    from sqlalchemy import text
    use = [c for c in cols
           if not c["computed"] and c["typ"].lower() not in _SKIP_TYPES][:max_cols]
    if not use:
        return {}, 0
    parts = []
    for i, c in enumerate(use):
        col = _q(c["col"])
        if c["typ"].lower() in ("char", "varchar", "nchar", "nvarchar"):
            expr = f"CASE WHEN {col} IS NOT NULL AND LTRIM(RTRIM({col})) <> '' THEN 1 ELSE 0 END"
        else:
            expr = f"CASE WHEN {col} IS NOT NULL THEN 1 ELSE 0 END"
        parts.append(f"SUM(CAST({expr} AS bigint)) AS c{i}")
    sql = f"SELECT COUNT_BIG(*) AS n, {', '.join(parts)} FROM {_q(schema)}.{_q(table)}"
    df = pd.read_sql(text(sql), engine)
    if df.empty:
        return {}, 0
    row = df.iloc[0]
    n = int(row["n"] or 0)
    return {c["col"]: int(row[f"c{i}"] or 0) for i, c in enumerate(use)}, n


def score(engine, schema, min_rows, max_cols, only=None):
    out = {"schema": schema, "tables": [], "errors": []}
    for tname, approx in tables_of(engine, schema):
        if only and tname.upper() not in only:
            continue
        try:
            cols = columns_of(engine, schema, tname)
            if approx == 0:
                out["tables"].append({"table": tname, "rows": 0, "cols": len(cols),
                                      "fills": {}, "coldefs": cols})
                continue
            fills, n = fill_rates(engine, schema, tname, cols, max_cols)
            out["tables"].append({"table": tname, "rows": n, "cols": len(cols),
                                  "fills": fills, "coldefs": cols})
        except Exception as e:                     # one bad table must not kill the run
            out["errors"].append((tname, str(e).strip().splitlines()[0][:160]))
    out["min_rows"] = min_rows
    return out


def report(sc, min_rows):
    L = []
    w = L.append
    tabs = sc["tables"]
    pop = [t for t in tabs if t["rows"] > 0]
    empty = [t for t in tabs if t["rows"] == 0]

    w("=" * 78)
    w(f"database scorecard — schema {sc['schema']}")
    w(f"{len(tabs)} table(s): {len(pop)} populated, {len(empty)} empty"
      f"   ·   {sum(t['rows'] for t in tabs):,} row(s) total")
    w("=" * 78)

    w("")
    w("1. INVENTORY")
    w("-" * 78)
    for t in sorted(tabs, key=lambda x: (-x["rows"], x["table"])):
        mark = "  " if t["rows"] else "· "
        w(f"  {mark}{t['table']:<40} {t['rows']:>12,}  ({t['cols']} cols)")
    if empty:
        w("")
        w(f"  {len(empty)} empty table(s). Empty is not wrong — a table with no source")
        w("  files yet is expected. It is only a finding if you thought it had loaded.")

    w("")
    w("2. RESTORE CANDIDATES  — nullable, but the data never needed it to be")
    w("-" * 78)
    w(f"  Nullable columns 100% populated across >= {min_rows:,} rows. Every row has a")
    w("  value, so NOT NULL would not have failed once. If you relaxed a constraint to")
    w("  get a load through, these are the ones to put back — the evidence is the data,")
    w("  not a schema document that may not match what shipped.")
    w("")
    found = 0
    for t in sorted(pop, key=lambda x: x["table"]):
        if t["rows"] < min_rows:
            continue
        hits = [c for c in t["coldefs"]
                if c["nullable"] and not c["computed"] and not c["identity"]
                and c["col"].lower() not in _STAMPS
                and t["fills"].get(c["col"]) == t["rows"]]
        if not hits:
            continue
        found += len(hits)
        w(f"  {t['table']}  ({t['rows']:,} rows)")
        for c in hits:
            w(f"      {c['col']:<32} {c['typ']:<12} 100% populated")
        w("")
    if not found:
        w("  none.")
    else:
        w(f"  {found} column(s). Restoring one is a single ALTER:")
        w(f"      ALTER TABLE {sc['schema']}.<table> ALTER COLUMN <col> <type> NOT NULL;")
        w("  Do it AFTER the loaders are proven, not before — a constraint restored onto")
        w("  a pipeline that still has a mapping gap just moves the failure around.")

    w("")
    w("3. SILENT GAPS  — populated table, column always empty")
    w("-" * 78)
    w("  The column is nullable, so nothing complains. Either the source never carries")
    w("  it (fine — expected, e.g. a scout ticket lists tops but never bases), or it is")
    w("  mapping nowhere and the data is being dropped. This tool cannot tell which.")
    w("  Only the source document can.")
    w("")
    gaps = 0
    for t in sorted(pop, key=lambda x: x["table"]):
        if t["rows"] < min_rows:
            continue
        hits = [c for c in t["coldefs"]
                if c["col"] in t["fills"] and t["fills"][c["col"]] == 0
                and not c["identity"] and c["col"].lower() not in _STAMPS]
        if not hits:
            continue
        gaps += len(hits)
        w(f"  {t['table']}  ({t['rows']:,} rows)")
        for c in hits:
            flag = "  ← IN PK" if c["in_pk"] else ""
            w(f"      {c['col']:<32} {c['typ']:<12} 0% populated{flag}")
        w("")
    if not gaps:
        w("  none.")
    else:
        w(f"  {gaps} column(s) hold nothing at all.")

    w("")
    w("4. PARTIAL FILL  — populated table, column sometimes empty")
    w("-" * 78)
    w("  Between 1% and 99%. Usually normal (optional data). Listed lowest-first so a")
    w("  column at 2% — which often means 'one file had it and the rest did not' —")
    w("  is easy to spot.")
    w("")
    part = []
    for t in sorted(pop, key=lambda x: x["table"]):
        if t["rows"] < min_rows:
            continue
        for c in t["coldefs"]:
            n = t["fills"].get(c["col"])
            if n is None or n == 0 or n == t["rows"]:
                continue
            if c["col"].lower() in _STAMPS:
                continue
            part.append((n / t["rows"], t["table"], c["col"], n, t["rows"]))
    if not part:
        w("  none.")
    else:
        for pct, tbl, col, n, tot in sorted(part)[:40]:
            w(f"  {pct*100:5.1f}%  {tbl}.{col:<28} {n:,} of {tot:,}")
        if len(part) > 40:
            w(f"  ... and {len(part) - 40} more")

    if sc["errors"]:
        w("")
        w("ERRORS  — tables that could not be scored")
        w("-" * 78)
        for t, e in sc["errors"]:
            w(f"  {t}: {e}")
        w("")
        w("  These are NOT counted as clean above. A table that failed to score is")
        w("  unknown, not empty.")

    w("")
    w("=" * 78)
    w("Counts are live. Interpretation is not: this tool reports what IS in the")
    w("database, never whether it SHOULD be. A 0% column may be correct.")
    w("=" * 78)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Read-only DataView database scorecard.")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", required=True)
    ap.add_argument("--schema", default="dataview")
    ap.add_argument("--driver", default=None, help="ODBC driver name (auto-detected)")
    ap.add_argument("--min-rows", type=int, default=1,
                    help="ignore tables with fewer rows in sections 2-4 (default 1)")
    ap.add_argument("--max-cols", type=int, default=80,
                    help="max columns counted per table (default 80)")
    ap.add_argument("--table", action="append", default=None,
                    help="only this table; repeatable")
    ap.add_argument("--report", default=None, help="also write the report to this file")
    args = ap.parse_args()

    try:
        eng = make_engine(args.server, args.database, args.driver)
    except Exception as e:
        print(f"could not connect: {e}", file=sys.stderr)
        return 2

    only = {t.upper() for t in args.table} if args.table else None
    sc = score(eng, args.schema, args.min_rows, args.max_cols, only)
    if not sc["tables"] and not sc["errors"]:
        print(f"No tables found in schema '{args.schema}' of database "
              f"'{args.database}'. Check the schema name.", file=sys.stderr)
        return 1
    txt = report(sc, args.min_rows)
    print(txt)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(txt + "\n")
        print(f"\nwritten: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
