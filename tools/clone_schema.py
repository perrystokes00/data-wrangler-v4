"""clone_schema.py — copy a database's table schema with pyodbc, no external tools.

Reads tables / columns / primary keys / foreign keys from a SOURCE database via
INFORMATION_SCHEMA + sys, and recreates them in a TARGET database in a dependency-
safe order: every CREATE TABLE first (columns + PK + identity + computed +
defaults), then every FK constraint in a second pass — so a single replay can't
fail on a forward reference. Idempotent: existing tables/constraints are skipped.

Schema only — no row data. Pair with seed_references.py to populate dv_r_*.

Usage:
    python clone_schema.py --source DataView --target DataView_Demo
    python clone_schema.py --source DataView --target DataView_Demo --schemas dataview file_catalog
    python clone_schema.py --source DataView --target DataView_Demo --dry-run
"""

import argparse
import sys


def _connect(server, database):
    import pyodbc
    return pyodbc.connect(
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={server};DATABASE={database};Trusted_Connection=yes",
        autocommit=True, timeout=30)


def _tables(cur, schemas):
    ph = ",".join("?" * len(schemas))
    cur.execute(
        f"SELECT s.name, t.name FROM sys.tables t "
        f"JOIN sys.schemas s ON s.schema_id = t.schema_id "
        f"WHERE s.name IN ({ph}) AND t.is_ms_shipped = 0 "
        f"ORDER BY s.name, t.name", *schemas)
    return [(r[0], r[1]) for r in cur.fetchall()]


def _column_defs(cur, schema, table):
    """Return a list of column DDL fragments, preserving type/length/precision,
    identity, computed columns, nullability, and defaults."""
    cur.execute("""
        SELECT c.name, ty.name AS type_name,
               c.max_length, c.precision, c.scale, c.is_nullable,
               c.is_identity,
               CAST(ic.seed_value AS BIGINT)      AS seed_value,
               CAST(ic.increment_value AS BIGINT) AS increment_value,
               c.is_computed, cc.definition AS computed_def,
               dc.definition AS default_def
        FROM sys.columns c
        JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        JOIN sys.objects o ON o.object_id = c.object_id
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        LEFT JOIN sys.identity_columns ic ON ic.object_id = c.object_id
                                         AND ic.column_id = c.column_id
        LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id
                                         AND cc.column_id = c.column_id
        LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
        WHERE s.name = ? AND o.name = ?
        ORDER BY c.column_id
    """, schema, table)

    frags = []
    for (name, tname, max_len, prec, scale, is_null, is_ident, seed, incr,
         is_computed, comp_def, def_def) in cur.fetchall():
        if is_computed and comp_def:
            frags.append(f"[{name}] AS {comp_def}")
            continue
        t = tname.lower()
        if t in ("varchar", "char", "varbinary", "binary"):
            length = "MAX" if max_len == -1 else str(max_len)
            typ = f"{tname}({length})"
        elif t in ("nvarchar", "nchar"):
            length = "MAX" if max_len == -1 else str(max_len // 2)
            typ = f"{tname}({length})"
        elif t in ("decimal", "numeric"):
            typ = f"{tname}({prec},{scale})"
        elif t in ("datetime2", "time", "datetimeoffset") and scale is not None:
            typ = f"{tname}({scale})"
        else:
            typ = tname
        parts = [f"[{name}]", typ]
        if is_ident:
            parts.append(f"IDENTITY({seed or 1},{incr or 1})")
        if def_def:
            parts.append(f"DEFAULT {def_def}")
        parts.append("NULL" if is_null else "NOT NULL")
        frags.append(" ".join(parts))
    return frags


def _pk(cur, schema, table):
    cur.execute("""
        SELECT kc.name, c.name, ic.is_descending_key
        FROM sys.key_constraints kc
        JOIN sys.objects o ON o.object_id = kc.parent_object_id
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        JOIN sys.index_columns ic ON ic.object_id = kc.parent_object_id
                                 AND ic.index_id = kc.unique_index_id
        JOIN sys.columns c ON c.object_id = ic.object_id
                          AND c.column_id = ic.column_id
        WHERE kc.type = 'PK' AND s.name = ? AND o.name = ?
        ORDER BY ic.key_ordinal
    """, schema, table)
    rows = cur.fetchall()
    if not rows:
        return None
    name = rows[0][0]
    cols = ", ".join(f"[{c}]{' DESC' if desc else ''}" for _, c, desc in rows)
    return f"CONSTRAINT [{name}] PRIMARY KEY ({cols})"


def _foreign_keys(cur, schemas):
    ph = ",".join("?" * len(schemas))
    cur.execute(f"""
        SELECT fk.name, ps.name, pt.name, rs.name, rt.name,
               cpa.name, cref.name, fkc.constraint_column_id
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        JOIN sys.tables  pt ON pt.object_id = fk.parent_object_id
        JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
        JOIN sys.tables  rt ON rt.object_id = fk.referenced_object_id
        JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
        JOIN sys.columns cpa  ON cpa.object_id = fkc.parent_object_id
                             AND cpa.column_id = fkc.parent_column_id
        JOIN sys.columns cref ON cref.object_id = fkc.referenced_object_id
                             AND cref.column_id = fkc.referenced_column_id
        WHERE ps.name IN ({ph})
        ORDER BY fk.name, fkc.constraint_column_id
    """, *schemas)
    fks = {}
    for (name, ps, pt, rs, rt, lcol, rcol, _ord) in cur.fetchall():
        e = fks.setdefault(name, {"ps": ps, "pt": pt, "rs": rs, "rt": rt,
                                  "lcols": [], "rcols": []})
        e["lcols"].append(lcol)
        e["rcols"].append(rcol)
    return fks


def _indexes(cur, schemas):
    """Non-PK, non-unique-constraint rowstore indexes (clustered/nonclustered),
    with key columns (ASC/DESC), INCLUDE columns, UNIQUE flag, and filter. The
    PK index comes with the table DDL; unique-constraint indexes are skipped
    (they're constraints, not plain indexes). Returns ready-to-run CREATE stmts,
    each guarded so re-running is a no-op."""
    ph = ",".join("?" * len(schemas))
    cur.execute(f"""
        SELECT s.name, o.name, i.name, i.is_unique, i.type_desc,
               i.has_filter, i.filter_definition,
               c.name, ic.is_descending_key, ic.is_included_column,
               ic.key_ordinal
        FROM sys.indexes i
        JOIN sys.objects o ON o.object_id = i.object_id
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        JOIN sys.index_columns ic ON ic.object_id = i.object_id
                                 AND ic.index_id = i.index_id
        JOIN sys.columns c ON c.object_id = ic.object_id
                          AND c.column_id = ic.column_id
        WHERE s.name IN ({ph}) AND o.type = 'U'
          AND i.is_primary_key = 0 AND i.is_unique_constraint = 0
          AND i.type IN (1, 2) AND i.name IS NOT NULL
        ORDER BY s.name, o.name, i.name,
                 ic.is_included_column, ic.key_ordinal
    """, *schemas)

    idx = {}
    for (sch, tbl, name, is_uniq, type_desc, has_filter, filt,
         col, desc, included, _ord) in cur.fetchall():
        e = idx.setdefault((sch, tbl, name), {
            "unique": bool(is_uniq), "clustered": "CLUSTERED" in (type_desc or ""),
            "has_filter": bool(has_filter), "filter": filt,
            "keys": [], "incl": []})
        if included:
            e["incl"].append(f"[{col}]")
        else:
            e["keys"].append(f"[{col}]{' DESC' if desc else ''}")

    stmts = []
    for (sch, tbl, name), e in idx.items():
        if not e["keys"]:
            continue
        uniq = "UNIQUE " if e["unique"] else ""
        clus = "CLUSTERED" if e["clustered"] else "NONCLUSTERED"
        s = (f"CREATE {uniq}{clus} INDEX [{name}] "
             f"ON [{sch}].[{tbl}] ({', '.join(e['keys'])})")
        if e["incl"]:
            s += f" INCLUDE ({', '.join(e['incl'])})"
        if e["has_filter"] and e["filter"]:
            s += f" WHERE {e['filter']}"
        stmts.append(
            f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = '{name}' "
            f"AND object_id = OBJECT_ID('[{sch}].[{tbl}]'))\n{s};")
    return stmts


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--source", default="DataView_Demo")
    ap.add_argument("--target", default="DataView_Demo")
    ap.add_argument("--schemas", nargs="+", default=["dataview", "file_catalog"])
    ap.add_argument("--no-indexes", action="store_true",
                    help="skip the non-PK index pass (tables + FKs only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the DDL; change nothing")
    a = ap.parse_args()

    try:
        import pyodbc  # noqa: F401
    except ImportError:
        print("pyodbc not installed in this Python — run from the venv that has it.")
        return 2

    src = _connect(a.server, a.source)
    scur = src.cursor()
    tables = _tables(scur, a.schemas)
    if not tables:
        print(f"No tables found in {a.source} for schemas {a.schemas}.")
        return 1

    # ── build CREATE TABLE statements (pass 1) ───────────────────────────────
    creates = []
    for schema, table in tables:
        cols = _column_defs(scur, schema, table)
        pk = _pk(scur, schema, table)
        body = ",\n  ".join(cols + ([pk] if pk else []))
        creates.append((schema, table,
                        f"CREATE TABLE [{schema}].[{table}] (\n  {body}\n);"))

    # ── build ALTER TABLE … ADD CONSTRAINT FK (pass 2) ───────────────────────
    fks = _foreign_keys(scur, a.schemas)
    fk_stmts = []
    for name, e in fks.items():
        lcols = ", ".join(f"[{c}]" for c in e["lcols"])
        rcols = ", ".join(f"[{c}]" for c in e["rcols"])
        fk_stmts.append(
            f"IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = '{name}')\n"
            f"ALTER TABLE [{e['ps']}].[{e['pt']}] WITH CHECK ADD "
            f"CONSTRAINT [{name}] FOREIGN KEY ({lcols}) "
            f"REFERENCES [{e['rs']}].[{e['rt']}] ({rcols});")

    # ── build CREATE INDEX statements (pass 3) ───────────────────────────────
    idx_stmts = [] if a.no_indexes else _indexes(scur, a.schemas)
    src.close()

    if a.dry_run:
        print(f"-- {len(creates)} tables, {len(fk_stmts)} FKs, "
              f"{len(idx_stmts)} indexes from {a.source} → {a.target}\n")
        for _, _, ddl in creates:
            print(ddl)
        print("\n-- foreign keys --")
        for f in fk_stmts:
            print(f)
        print("\n-- indexes --")
        for ix in idx_stmts:
            print(ix)
        return 0

    # ── apply to target ──────────────────────────────────────────────────────
    tgt = _connect(a.server, a.target)
    tcur = tgt.cursor()
    made_schemas = set()
    created = skipped = fk_added = fk_err = 0

    for schema, table, ddl in creates:
        if schema not in made_schemas:
            tcur.execute(
                f"IF SCHEMA_ID('{schema}') IS NULL EXEC('CREATE SCHEMA [{schema}]')")
            made_schemas.add(schema)
        exists = tcur.execute(
            "SELECT 1 FROM sys.tables t JOIN sys.schemas s "
            "ON s.schema_id=t.schema_id WHERE s.name=? AND t.name=?",
            schema, table).fetchone()
        if exists:
            skipped += 1
            continue
        try:
            tcur.execute(ddl)
            created += 1
        except Exception as e:
            print(f"  ✗ CREATE [{schema}].[{table}]: {str(e)[:120]}")

    for stmt in fk_stmts:
        try:
            tcur.execute(stmt)
            fk_added += 1
        except Exception as e:
            fk_err += 1
            print(f"  ✗ FK: {str(e)[:140]}")

    ix_added = ix_err = 0
    for stmt in idx_stmts:
        try:
            tcur.execute(stmt)
            ix_added += 1
        except Exception as e:
            ix_err += 1
            print(f"  ✗ INDEX: {str(e)[:140]}")

    print(f"\n{a.target}: tables created {created}, already-present {skipped}; "
          f"FKs applied {fk_added}" + (f", FK errors {fk_err}" if fk_err else "")
          + f"; indexes applied {ix_added}"
          + (f", index errors {ix_err}" if ix_err else ""))
    tgt.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
