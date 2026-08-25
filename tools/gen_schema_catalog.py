#!/usr/bin/env python3
r"""
gen_schema_catalog.py — introspect DataView_Demo and regenerate the schema JSONs.

Read-only. Runs SELECTs against INFORMATION_SCHEMA + the sys catalog views only;
writes NOTHING to the database. Run it on the box that hosts SQL Express.

Emits (into --out, default = current dir):
  dataview_fk_catalog.json    the RICH shape page_dir_loader / load_preflight read:
                              { fk_constraints, table_cols, table_kind }
  dataview_schema_full.json   full per-column metadata (type, nullable, len,
                              precision/scale, primary_key) — a superset, for
                              docs / the ML column-mapper. Written under a NEW name
                              so it never clobbers an existing dataview_schema_domain.json.

Usage:
  python gen_schema_catalog.py
  python gen_schema_catalog.py --server localhost\SQLEXPRESS --db DataView_Demo \
                               --schema dataview --out schemas
  # SQL auth instead of Windows auth:
  python gen_schema_catalog.py --user sa --pwd ****
"""
import sys
import argparse, json, sys, os, datetime
from collections import defaultdict, OrderedDict

try:
    import pyodbc
except ImportError:
    sys.exit("pyodbc not installed:  pip install pyodbc")

# Name-based entity parents (SHA1-seeded). Everything else dv_* is 'data';
# dv_r_* is 'reference'. Adjust if you add more entity parents.
ENTITY_TABLES = {"dv_business_associate", "dv_field"}


def pick_driver():
    prefer = ["ODBC Driver 18 for SQL Server",
              "ODBC Driver 17 for SQL Server",
              "SQL Server Native Client 11.0",
              "SQL Server"]
    have = set(pyodbc.drivers())
    for d in prefer:
        if d in have:
            return d
    sys.exit(f"No SQL Server ODBC driver found. Installed: {sorted(have)}")


def connect(server, db, user, pwd):
    drv = pick_driver()
    parts = [f"DRIVER={{{drv}}}", f"SERVER={server}", f"DATABASE={db}"]
    if user:
        parts += [f"UID={user}", f"PWD={pwd}"]
    else:
        parts += ["Trusted_Connection=yes"]
    if "18" in drv:                       # driver 18 defaults to Encrypt=yes
        parts += ["Encrypt=no", "TrustServerCertificate=yes"]
    return pyodbc.connect(";".join(parts) + ";", timeout=15)


COLS_SQL = """
SELECT c.TABLE_NAME, c.COLUMN_NAME, c.ORDINAL_POSITION,
       c.DATA_TYPE, c.IS_NULLABLE, c.CHARACTER_MAXIMUM_LENGTH,
       c.NUMERIC_PRECISION, c.NUMERIC_SCALE
FROM   INFORMATION_SCHEMA.COLUMNS c
JOIN   INFORMATION_SCHEMA.TABLES  t
       ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
WHERE  c.TABLE_SCHEMA = ? AND t.TABLE_TYPE = 'BASE TABLE'
       -- A DATED BACKUP IS NOT A LOAD TARGET. These are snapshots of the
       -- app's own learned-mapping tables; catalogued, they appear in the
       -- Data Assistant's target dropdown, where picking one writes real
       -- data into a table nothing reads. '_' is a LIKE wildcard: bracketed.
       AND c.TABLE_NAME NOT LIKE '%[_]bak[_]%'
ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
"""

# sys.foreign_key_columns terminology: parent_* = the CHILD (referencing) side,
# referenced_* = the PARENT (PK) side. Aliased below to child_/parent_ correctly.
FK_SQL = """
SELECT fk.name  AS fk_name,
       ct.name  AS child_table,  cc.name AS child_col,
       pt.name  AS parent_table, pc.name AS parent_col,
       fkc.constraint_column_id AS ord
FROM   sys.foreign_keys fk
JOIN   sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN   sys.tables   ct   ON ct.object_id   = fk.parent_object_id
JOIN   sys.schemas  csch ON csch.schema_id = ct.schema_id
JOIN   sys.columns  cc   ON cc.object_id = fkc.parent_object_id
                        AND cc.column_id = fkc.parent_column_id
JOIN   sys.tables   pt   ON pt.object_id   = fk.referenced_object_id
JOIN   sys.schemas  psch ON psch.schema_id = pt.schema_id
JOIN   sys.columns  pc   ON pc.object_id = fkc.referenced_object_id
                        AND pc.column_id = fkc.referenced_column_id
WHERE  csch.name = ?
ORDER BY fk.name, fkc.constraint_column_id
"""

PK_SQL = """
SELECT t.name AS table_name, c.name AS col_name, ic.key_ordinal
FROM   sys.indexes i
JOIN   sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN   sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
JOIN   sys.tables  t ON t.object_id = i.object_id
JOIN   sys.schemas s ON s.schema_id = t.schema_id
WHERE  i.is_primary_key = 1 AND s.name = ?
ORDER BY t.name, ic.key_ordinal
"""


def kind_of(table):
    tl = table.lower()
    if tl.startswith("dv_r_"):  return "reference"
    if tl in ENTITY_TABLES:     return "entity"
    return "data"


for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="Regenerate DataView schema JSONs (read-only).")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--db",     default="DataView_Demo")
    ap.add_argument("--schema", default="dataview")
    ap.add_argument("--user",   default=None, help="SQL auth user (omit for Windows auth)")
    ap.add_argument("--pwd",    default=None)
    ap.add_argument("--out",    default=".")
    args = ap.parse_args()

    cx  = connect(args.server, args.db, args.user, args.pwd)
    cur = cx.cursor()

    # ── columns ──────────────────────────────────────────────────────────────
    table_cols = OrderedDict()          # {table: [col, ...]}  (ordinal order)
    full       = OrderedDict()          # {table: {columns, primary_key, kind}}
    for r in cur.execute(COLS_SQL, args.schema):
        t = r.TABLE_NAME
        table_cols.setdefault(t, []).append(r.COLUMN_NAME)
        full.setdefault(t, OrderedDict([("kind", kind_of(t)),
                                        ("primary_key", []),
                                        ("columns", [])]))
        full[t]["columns"].append(OrderedDict([
            ("name",      r.COLUMN_NAME),
            ("type",      r.DATA_TYPE),
            ("nullable",  r.IS_NULLABLE == "YES"),
            ("max_len",   r.CHARACTER_MAXIMUM_LENGTH),
            ("precision", r.NUMERIC_PRECISION),
            ("scale",     r.NUMERIC_SCALE),
        ]))

    # ── primary keys ─────────────────────────────────────────────────────────
    for r in cur.execute(PK_SQL, args.schema):
        if r.table_name in full:
            full[r.table_name]["primary_key"].append(r.col_name)

    # ── foreign keys (group composite cols by constraint name) ───────────────
    fk_group = OrderedDict()
    for r in cur.execute(FK_SQL, args.schema):
        g = fk_group.setdefault(r.fk_name, {"child_table": r.child_table,
                                            "parent_table": r.parent_table,
                                            "child_cols": [], "parent_cols": []})
        g["child_cols"].append(r.child_col)
        g["parent_cols"].append(r.parent_col)

    fk_constraints = OrderedDict()      # {child_table: [ {fk_name, child_cols, parent_table, parent_cols} ]}
    for name, g in fk_group.items():
        fk_constraints.setdefault(g["child_table"], []).append(OrderedDict([
            ("fk_name",      name),
            ("child_cols",   g["child_cols"]),
            ("parent_table", g["parent_table"]),
            ("parent_cols",  g["parent_cols"]),
        ]))

    table_kind = OrderedDict((t, kind_of(t)) for t in table_cols)
    n_fks = sum(len(v) for v in fk_constraints.values())

    # ── governance check: every FK parent must resolve within the schema ─────
    # (cross-schema or dropped parents get surfaced, never silently ignored.)
    known = set(table_cols)
    orphans = [(child, fk["fk_name"], fk["parent_table"])
               for child, fks in fk_constraints.items()
               for fk in fks if fk["parent_table"] not in known]

    catalog = OrderedDict([
        ("generated_at", datetime.datetime.now().isoformat(timespec="seconds")),
        ("database",  args.db),
        ("schema",    args.schema),
        ("n_tables",  len(table_cols)),
        ("n_fks",     n_fks),
        ("fk_constraints", fk_constraints),
        ("table_cols",     table_cols),
        ("table_kind",     table_kind),
    ])

    os.makedirs(args.out, exist_ok=True)
    fk_path   = os.path.join(args.out, "dataview_fk_catalog.json")
    full_path = os.path.join(args.out, "dataview_schema_full.json")
    with open(fk_path, "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=2)
    with open(full_path, "w", encoding="utf-8") as fh:
        json.dump(OrderedDict([("generated_at", catalog["generated_at"]),
                               ("database", args.db), ("schema", args.schema),
                               ("tables", full)]), fh, indent=2)

    # ── summary ──────────────────────────────────────────────────────────────
    kc = defaultdict(int)
    for k in table_kind.values():
        kc[k] += 1
    print(f"\u2713 {fk_path}")
    print(f"\u2713 {full_path}")
    print(f"  tables: {len(table_cols)}   "
          f"columns: {sum(len(v) for v in table_cols.values())}   FKs: {n_fks}")
    print("  kinds:  " + ", ".join(f"{k}={n}" for k, n in sorted(kc.items())))
    if orphans:
        print(f"\n\u26a0 {len(orphans)} FK(s) point outside schema '{args.schema}' "
              f"(cross-schema or missing parent) \u2014 audit these:")
        for child, fkname, parent in orphans:
            print(f"    {child}.{fkname} -> {parent}")
    else:
        print("  all FK parents resolve within the schema \u2713")


if __name__ == "__main__":
    main()
