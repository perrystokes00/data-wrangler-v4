#!/usr/bin/env python3
"""
schema_sync.py
==============
Make a schema identical between two SQL Server databases on the same instance —
e.g. bring DataView_Demo's `file_catalog` (and its cat_* tables) up to match
DataView's.

It introspects both databases and emits **additive** DDL:
  * CREATE TABLE      for tables present in source but missing in target
  * ALTER TABLE ADD   for columns present in source but missing in target
  * CREATE INDEX      for (non-PK) indexes present in source but missing

It is conservative on purpose: it never drops or alters existing columns,
indexes or tables (those can lose data) — type / nullability differences on
shared columns are *reported* so you can decide.  Foreign keys are not scripted.

Dry-run by default (prints the DDL + a diff summary, writes a .sql file).
Pass --apply to execute the generated DDL against the target.

    python schema_sync.py                          # DataView -> DataView_Demo, file_catalog (dry run)
    python schema_sync.py --apply                  # actually run it
    python schema_sync.py --schema dataview        # sync a different schema
    python schema_sync.py --source DataView --target DataView_Demo --apply

Requires pyodbc and a trusted connection to the instance.
"""

import argparse
import sys

DEFAULT_SERVER = r"PERRY\SQLEXPRESS"
DEFAULT_DRIVER = "ODBC Driver 17 for SQL Server"

try:
    import pyodbc
except ImportError:
    pyodbc = None


# ── connection ───────────────────────────────────────────────────────────────
def connect(server, database, driver):
    return pyodbc.connect(
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
        "Trusted_Connection=yes;", autocommit=True)


# ── introspection ────────────────────────────────────────────────────────────
_COLS_SQL = """
SELECT t.name AS table_name, c.name AS col_name, ty.name AS type_name,
       c.max_length, c.precision, c.scale, c.is_nullable, c.is_identity,
       CAST(ic.seed_value AS BIGINT)      AS seed_value,
       CAST(ic.increment_value AS BIGINT) AS increment_value,
       dc.definition AS default_def, c.column_id
FROM sys.columns c
JOIN sys.tables   t  ON t.object_id = c.object_id
JOIN sys.schemas  s  ON s.schema_id = t.schema_id
JOIN sys.types    ty ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.identity_columns ic
       ON ic.object_id = c.object_id AND ic.column_id = c.column_id
LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
WHERE s.name = ?
ORDER BY t.name, c.column_id
"""

_IDX_SQL = """
SELECT t.name AS table_name, i.name AS index_name, i.is_unique,
       i.is_primary_key, i.type_desc, c.name AS col_name,
       ic.key_ordinal, ic.is_descending_key
FROM sys.indexes i
JOIN sys.tables  t ON t.object_id = i.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.index_columns ic
     ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c
     ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE s.name = ? AND i.type > 0 AND ic.is_included_column = 0
ORDER BY t.name, i.index_id, ic.key_ordinal
"""

_ROWCOUNT_SQL = """
SELECT t.name AS table_name, SUM(p.rows) AS [rows]
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
WHERE s.name = ?
GROUP BY t.name
"""


def introspect(conn, schema):
    """Return {table_name: {"cols": {name: coldict}, "col_order": [...],
    "indexes": {idx_name: {...}}, "rows": int}}."""
    cur = conn.cursor()
    tables = {}

    for r in cur.execute(_COLS_SQL, schema).fetchall():
        t = tables.setdefault(r.table_name,
                              {"cols": {}, "col_order": [], "indexes": {}, "rows": 0})
        t["cols"][r.col_name.lower()] = {
            "name": r.col_name, "type": r.type_name, "max_length": r.max_length,
            "precision": r.precision, "scale": r.scale,
            "is_nullable": bool(r.is_nullable), "is_identity": bool(r.is_identity),
            "seed": r.seed_value, "increment": r.increment_value,
            "default": r.default_def,
        }
        t["col_order"].append(r.col_name.lower())

    for r in cur.execute(_IDX_SQL, schema).fetchall():
        if r.table_name not in tables:
            continue
        idx = tables[r.table_name]["indexes"].setdefault(r.index_name, {
            "name": r.index_name, "is_unique": bool(r.is_unique),
            "is_pk": bool(r.is_primary_key), "type": r.type_desc, "cols": []})
        idx["cols"].append((r.col_name, bool(r.is_descending_key)))

    for r in cur.execute(_ROWCOUNT_SQL, schema).fetchall():
        if r.table_name in tables:
            tables[r.table_name]["rows"] = int(r.rows or 0)

    return tables


# ── DDL rendering ────────────────────────────────────────────────────────────
def render_type(c):
    t = c["type"].lower()
    ml, prec, scale = c["max_length"], c["precision"], c["scale"]
    if t in ("varchar", "char", "binary", "varbinary"):
        n = "MAX" if ml == -1 else str(ml)
        return f"{c['type'].upper()}({n})"
    if t in ("nvarchar", "nchar"):
        n = "MAX" if ml == -1 else str(ml // 2)      # max_length is bytes
        return f"{c['type'].upper()}({n})"
    if t in ("decimal", "numeric"):
        return f"{c['type'].upper()}({prec},{scale})"
    if t in ("datetime2", "datetimeoffset", "time") and scale not in (None, 7):
        return f"{c['type'].upper()}({scale})"
    return c["type"].upper()


def col_ddl(c):
    s = f"[{c['name']}] {render_type(c)}"
    if c["is_identity"]:
        seed = int(c["seed"]) if c["seed"] is not None else 1
        inc = int(c["increment"]) if c["increment"] is not None else 1
        s += f" IDENTITY({seed},{inc})"
    s += " NULL" if c["is_nullable"] else " NOT NULL"
    if c["default"]:
        s += f" DEFAULT {c['default']}"
    return s


def create_table_ddl(schema, tname, tinfo):
    cols = [tinfo["cols"][k] for k in tinfo["col_order"]]
    lines = [col_ddl(c) for c in cols]
    pk = next((i for i in tinfo["indexes"].values() if i["is_pk"]), None)
    if pk:
        pkc = ", ".join(f"[{c}]{' DESC' if d else ''}" for c, d in pk["cols"])
        lines.append(f"CONSTRAINT [{pk['name']}] PRIMARY KEY ({pkc})")
    body = ",\n    ".join(lines)
    return f"CREATE TABLE [{schema}].[{tname}] (\n    {body}\n);"


def index_ddl(schema, tname, idx):
    uniq = "UNIQUE " if idx["is_unique"] else ""
    kind = "CLUSTERED" if idx["type"] == "CLUSTERED" else "NONCLUSTERED"
    cols = ", ".join(f"[{c}]{' DESC' if d else ''}" for c, d in idx["cols"])
    return (f"CREATE {uniq}{kind} INDEX [{idx['name']}] "
            f"ON [{schema}].[{tname}] ({cols});")


def alter_add_ddl(schema, tname, c):
    return f"ALTER TABLE [{schema}].[{tname}] ADD {col_ddl(c)};"


# ── diff ─────────────────────────────────────────────────────────────────────
def build_plan(src, tgt, schema):
    """Return (ddl_statements, warnings)."""
    ddl, warn = [], []

    # 1) tables only in source -> CREATE (table + its non-PK indexes)
    for tname in sorted(src):
        if tname in tgt:
            continue
        ddl.append(f"-- new table: {schema}.{tname}")
        ddl.append(create_table_ddl(schema, tname, src[tname]))
        for idx in src[tname]["indexes"].values():
            if not idx["is_pk"]:
                ddl.append(index_ddl(schema, tname, idx))
        ddl.append("")

    # 2) shared tables -> add missing columns / indexes; report mismatches
    for tname in sorted(src):
        if tname not in tgt:
            continue
        s_cols, t_cols = src[tname]["cols"], tgt[tname]["cols"]

        added = []
        for k in src[tname]["col_order"]:
            if k not in t_cols:
                c = s_cols[k]
                if not c["is_nullable"] and not c["default"] and tgt[tname]["rows"] > 0:
                    warn.append(
                        f"{schema}.{tname}.{c['name']}: NOT NULL without default "
                        f"and target has {tgt[tname]['rows']:,} rows — ADD will fail; "
                        f"add a default or backfill first.")
                added.append(alter_add_ddl(schema, tname, c))

        # type / nullability mismatches on shared columns -> report only
        for k in src[tname]["col_order"]:
            if k in t_cols:
                sc, tc = s_cols[k], t_cols[k]
                if render_type(sc) != render_type(tc):
                    warn.append(f"{schema}.{tname}.{sc['name']}: type differs "
                                f"(source {render_type(sc)} vs target {render_type(tc)}) "
                                f"— not auto-altered.")
                elif sc["is_nullable"] != tc["is_nullable"]:
                    warn.append(f"{schema}.{tname}.{sc['name']}: nullability differs "
                                f"(source {'NULL' if sc['is_nullable'] else 'NOT NULL'}) "
                                f"— not auto-altered.")

        missing_idx = [i for n, i in src[tname]["indexes"].items()
                       if not i["is_pk"] and n not in tgt[tname]["indexes"]]

        if added or missing_idx:
            ddl.append(f"-- update table: {schema}.{tname}")
            ddl.extend(added)
            ddl.extend(index_ddl(schema, tname, i) for i in missing_idx)
            ddl.append("")

    # 3) tables only in target -> report (never auto-drop)
    for tname in sorted(tgt):
        if tname not in src:
            warn.append(f"{schema}.{tname}: exists in target but NOT in source "
                        f"(left as-is).")

    return ddl, warn


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Sync a schema from one DB to another.")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--driver", default=DEFAULT_DRIVER)
    ap.add_argument("--source", default="DataView_Demo", help="source (authoritative) DB")
    ap.add_argument("--target", default="DataView_Demo", help="DB to bring into line")
    ap.add_argument("--schema", default="file_catalog")
    ap.add_argument("--out", default="schema_sync.sql", help="write generated DDL here")
    ap.add_argument("--apply", action="store_true",
                    help="execute the DDL against the target (default: dry run)")
    a = ap.parse_args()

    if pyodbc is None:
        sys.exit("pyodbc is required. pip install pyodbc")

    print(f"[SCHEMA] {a.schema}   {a.source}  ->  {a.target}   @ {a.server}")
    src_conn = connect(a.server, a.source, a.driver)
    tgt_conn = connect(a.server, a.target, a.driver)
    src = introspect(src_conn, a.schema)
    tgt = introspect(tgt_conn, a.schema)
    print(f"[SCHEMA] source: {len(src)} table(s)   target: {len(tgt)} table(s)")

    ddl, warn = build_plan(src, tgt, a.schema)
    stmts = [d for d in ddl if d and not d.startswith("--")]

    script = ("-- schema_sync: " + a.source + " -> " + a.target +
              " / schema " + a.schema + "\n"
              "SET XACT_ABORT ON;\nBEGIN TRAN;\n\n" +
              "\n".join(ddl).rstrip() + "\n\nCOMMIT;\n")
    with open(a.out, "w") as f:
        f.write(script)

    new_tables = sum(1 for t in src if t not in tgt)
    print(f"\n[PLAN] {new_tables} new table(s), {len(stmts)} DDL statement(s).")
    print(f"[PLAN] DDL written to {a.out}")
    if warn:
        print(f"\n[WARN] {len(warn)} item(s) need your attention "
              f"(not auto-applied):")
        for w in warn:
            print(f"   - {w}")

    if not stmts:
        print("\nTarget already matches source for additive changes. Nothing to do.")
        return 0

    if not a.apply:
        print("\n(dry run) Review the .sql above, then re-run with --apply to execute.")
        return 0

    print(f"\n[APPLY] executing {len(stmts)} statement(s) against {a.target}…")
    cur = tgt_conn.cursor()
    try:
        cur.execute("SET XACT_ABORT ON;")
        cur.execute("BEGIN TRAN;")
        for s in stmts:
            cur.execute(s)
        cur.execute("COMMIT;")
        print("[APPLY] committed. Target now matches source (additively).")
    except Exception as e:
        try:
            cur.execute("IF @@TRANCOUNT > 0 ROLLBACK;")
        except Exception:
            pass
        sys.exit(f"[APPLY] failed, rolled back: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
