"""
build_catalog_mirror.py
========================
DataView v3 — schema-driven catalog mirror tables.

Documents are catalogued BEFORE a well header (dv_well) exists, so extracted
detail is captured in `file_catalog.cat_*` mirror tables that share the SAME
column shape as their `dataview.dv_*` targets. A separate step
(promote_catalog.py) copies the rows into dv_* once a header is created.

For each table in DV_TABLES this creates file_catalog.cat_<name> where <name>
is the dv_* table without its leading "dv_". Each mirror has:
  * every dv_* column, same type, but NULLable and with NO foreign keys
    (capture is tolerant and parentless)
  * provenance columns:
      CAT_ROW_ID   BIGINT IDENTITY PRIMARY KEY
      INVENTORY_ID NVARCHAR(64)        -- source file (GLOBAL_FILE_CATALOG)
      SOURCE_PATH  NVARCHAR(1024)
      PROMOTED     BIT  NOT NULL DEFAULT 0
      PROMOTED_AT  DATETIME2 NULL
      CAPTURED_AT  DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
  * a UWI helper column IF the dv_* table has none of its own (so promotion
    can always gate on "does dv_well have this UWI yet")
  * indexes on (UWI, PROMOTED) and INVENTORY_ID

Dry-run by default (prints DDL). Use --apply to execute. Idempotent: existing
mirrors are skipped unless --drop is given.

    python build_catalog_mirror.py                 # print DDL only
    python build_catalog_mirror.py --apply         # create missing mirrors
    python build_catalog_mirror.py --drop --apply  # recreate from scratch

NOTE the --server / --database defaults point at PERRY\\SQLEXPRESS / DataView.
The test database is DataView_Demo on localhost\\SQLEXPRESS — pass BOTH
arguments explicitly rather than trusting the defaults.
"""
from __future__ import annotations

import argparse
import sys

import pyodbc

DV_SCHEMA  = "dataview"
CAT_SCHEMA = "file_catalog"
CAT_PREFIX = "cat_"

# Explicit mirror set — only the tables the document pipeline populates, NOT
# the whole dv_ schema. Mapped from the logical groups: well header, strat/tops,
# directional surveys, core + core analysis, petrophysics, completions,
# production. Edit this list to add/remove groups; ordering is derived from the
# FK graph at runtime, so list order here does not matter.
MIRROR_TABLES = [
    "dv_well",                                       # well header
    "dv_well_formation_top",                         # strat / tops
    "dv_well_dir_srvy_hdr", "dv_well_dir_srvy_sta",  # directional surveys
    "dv_well_log",                                   # LAS/DLIS/LIS log-file header (cat_well_log)
    "dv_well_log_curve",                             # PPDM curves, child of dv_well_log (cat_well_log_curve)
    "dv_well_core", "dv_well_core_sample",           # core + core analysis
    "dv_well_petro_interp", "dv_well_petro_zone",    # petrophysics
    "dv_well_completion",                            # completions
    "dv_well_stimulation",                           # frac stages
    "dv_well_casing",                                # casing + cementing
    "dv_well_perforation",                           # perforated intervals
    "dv_well_dst",                                    # drill-stem tests
    # The per-period flow rows of a well test. Without this the periods had
    # nowhere to land, so load_well_test kept only the MAXIMA and a WELL_TEST
    # document's flow table was reduced to three numbers. A multi-rate test IS
    # its periods; the maxima are a summary of them, not a substitute.
    "dv_well_dst_period",                             # flow periods of a test
    "dv_prod_entity", "dv_prod_volume",              # production
]

# WHAT MIRROR_TABLES MEANS, EXACTLY: it is the set promote_catalog's GENERIC
# LOOP walks — promote calls discover_tables() and loops over whatever it
# returns. It used to double as "which mirrors exist", and that conflation is
# why cat_field could not be rebuilt without breaking promote: adding it here
# would have moved its rows twice, once by the generic loop and once by
# promote_field. The two questions are now separate — see DEDICATED_MIRRORS.
#
# A table in NEITHER list is invisible twice over: no mirror is built, and rows
# written into a hand-made mirror are silently stepped past at promote time,
# reported as neither moved nor held. Casing sat in exactly that state — 148
# rows staged, 0 promoted, no error. check_mirror_registry.py exists to make
# that state loud; run it after editing either list.

# Mirrors that must EXIST and stay in sync with their dv_* table, but are
# promoted by a DEDICATED promoter in promote_catalog rather than by the
# generic loop. They must NOT be added to MIRROR_TABLES — discover_tables()
# drives promote, so a table in both lists gets its rows moved twice.
DEDICATED_MIRRORS = [
    "dv_field",       # promote_field
    "dv_land_tract",  # promote_land_tract
    "dv_boundary",    # promote_boundary
    "dv_pipeline",    # promote_pipeline
    "dv_log_curve",   # promote_las_catalog
]

# Names reserved for provenance — never copied during promote.
PROVENANCE = ("CAT_ROW_ID", "INVENTORY_ID", "SOURCE_PATH",
              "PROMOTED", "PROMOTED_AT", "CAPTURED_AT")


def cat_name(dv_table: str) -> str:
    base = dv_table[3:] if dv_table.lower().startswith("dv_") else dv_table
    return f"{CAT_PREFIX}{base}"


def connect(server: str, database: str):
    cs = (f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};"
          f"DATABASE={database};Trusted_Connection=yes;")
    con = pyodbc.connect(cs, autocommit=True)
    return con


def _render_type(col: dict) -> str:
    """Render a SQL Server column type from INFORMATION_SCHEMA.COLUMNS row."""
    dt  = col["DATA_TYPE"].lower()
    clen = col["CHARACTER_MAXIMUM_LENGTH"]
    prec = col["NUMERIC_PRECISION"]
    scale = col["NUMERIC_SCALE"]
    dtp  = col["DATETIME_PRECISION"]

    if dt in ("char", "varchar", "nchar", "nvarchar", "binary", "varbinary"):
        size = "MAX" if clen in (-1, None) else str(clen)
        return f"{dt}({size})"
    if dt in ("decimal", "numeric"):
        return f"{dt}({prec},{scale or 0})"
    if dt in ("datetime2", "datetimeoffset", "time") and dtp is not None:
        return f"{dt}({dtp})"
    if dt == "float" and prec:
        return f"float({prec})"
    return dt


def fetch_columns(cur, schema: str, table: str) -> list[dict]:
    cur.execute("""
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
               NUMERIC_PRECISION, NUMERIC_SCALE, DATETIME_PRECISION,
               ORDINAL_POSITION
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
    """, schema, table)
    cols = []
    for r in cur.fetchall():
        cols.append({
            "COLUMN_NAME": r[0], "DATA_TYPE": r[1],
            "CHARACTER_MAXIMUM_LENGTH": r[2], "NUMERIC_PRECISION": r[3],
            "NUMERIC_SCALE": r[4], "DATETIME_PRECISION": r[5],
        })
    return cols


def _toposort(nodes: set, edges: list) -> list:
    """Order so a parent (referenced) precedes its child (referencing).

    `edges` is a list of (child, parent). Within a layer, ties break
    alphabetically for deterministic output. A cycle/dangling ref degrades to
    alphabetical for the remainder rather than dropping rows.
    """
    parents = {n: set() for n in nodes}
    for child, parent in edges:
        if child in parents and parent in nodes and child != parent:
            parents[child].add(parent)
    ordered, placed, remaining = [], set(), set(nodes)
    while remaining:
        ready = sorted(n for n in remaining if parents[n] <= placed)
        if not ready:
            ready = sorted(remaining)          # break a cycle, stay stable
        for n in ready:
            ordered.append(n)
            placed.add(n)
            remaining.discard(n)
    return ordered


def discover_tables(cur) -> list:
    """The configured mirror set (MIRROR_TABLES), ordered parents-first.

    The *which* is the explicit MIRROR_TABLES allowlist — only the tables the
    document pipeline populates. The *order* is derived from the FK edges among
    those tables (so promote inserts in an FK-safe order: dv_well before its
    children, dv_prod_entity before dv_prod_volume, hdr before sta, etc.). It
    degrades to alphabetical on a cycle rather than dropping a table.
    """
    tables = set(MIRROR_TABLES)

    edges = []
    cur.execute(f"""
        SELECT OBJECT_NAME(fk.parent_object_id),
               OBJECT_NAME(fk.referenced_object_id)
        FROM sys.foreign_keys fk
        WHERE OBJECT_SCHEMA_NAME(fk.parent_object_id) = '{DV_SCHEMA}'
    """)
    for child, parent in cur.fetchall():
        if child in tables and parent in tables:
            edges.append((child, parent))

    # Every detail logically depends on the dv_well header, so dv_well must
    # promote first. Add that edge for any mirror table that lacks a *declared*
    # FK to dv_well (e.g. dv_log_curve, when dv_well.UWI carries no unique key
    # so no FK can be created) — otherwise the toposort could order it ahead of
    # dv_well and gate out brand-new wells until a second pass.
    if "dv_well" in tables:
        for t in tables:
            if t != "dv_well":
                edges.append((t, "dv_well"))

    return _toposort(tables, edges)


def mirrors_to_build(cur) -> list:
    """Every mirror that should exist: the generic-loop set plus the ones with
    dedicated promoters.

    discover_tables() is deliberately NOT changed — promote_catalog imports it
    and must keep receiving only the tables its generic loop should walk.
    """
    extra = [t for t in DEDICATED_MIRRORS if t not in MIRROR_TABLES]
    return discover_tables(cur) + sorted(extra)


def build_ddl(cur, dv_table: str, drop: bool) -> str:
    cols = fetch_columns(cur, DV_SCHEMA, dv_table)
    if not cols:
        return f"-- SKIP {DV_SCHEMA}.{dv_table}: not found\n"

    cat = cat_name(dv_table)
    has_uwi = any(c["COLUMN_NAME"].upper() == "UWI" for c in cols)
    has_inv = any(c["COLUMN_NAME"].upper() == "INVENTORY_ID" for c in cols)

    lines = []
    for c in cols:
        # mirror columns: same name/type, always NULLable, never identity
        lines.append(f"    [{c['COLUMN_NAME']}] {_render_type(c)} NULL")

    # provenance
    prov = []
    if not has_uwi:
        prov.append("    [UWI] CHAR(14) NULL")          # gate helper
    if not has_inv:                                     # dv_* may now carry it
        prov.append("    [INVENTORY_ID] NVARCHAR(64) NULL")
    prov += [
        "    [SOURCE_PATH] NVARCHAR(1024) NULL",
        "    [PROMOTED] BIT NOT NULL CONSTRAINT "
        f"DF_{cat}_PROMOTED DEFAULT 0",
        "    [PROMOTED_AT] DATETIME2 NULL",
        "    [CAPTURED_AT] DATETIME2 NOT NULL CONSTRAINT "
        f"DF_{cat}_CAP DEFAULT SYSUTCDATETIME()",
        f"    [CAT_ROW_ID] BIGINT IDENTITY(1,1) "
        f"CONSTRAINT PK_{cat} PRIMARY KEY",
    ]

    body = ",\n".join(lines + prov)
    create = (f"CREATE TABLE {CAT_SCHEMA}.{cat} (\n{body}\n);\n"
              f"CREATE INDEX IX_{cat}_UWI ON {CAT_SCHEMA}.{cat}(UWI, PROMOTED);\n"
              f"CREATE INDEX IX_{cat}_INV ON {CAT_SCHEMA}.{cat}(INVENTORY_ID);\n")

    out = []
    if drop:
        out.append(f"DROP TABLE IF EXISTS {CAT_SCHEMA}.{cat};")
        out.append(create)
    else:
        out.append(
            f"IF OBJECT_ID('{CAT_SCHEMA}.{cat}','U') IS NULL\nBEGIN\n{create}END")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server",   default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--database", default="DataView")
    ap.add_argument("--apply",  action="store_true", help="execute (else print DDL)")
    ap.add_argument("--drop",   action="store_true", help="drop + recreate mirrors")
    a = ap.parse_args()

    con = connect(a.server, a.database)
    cur = con.cursor()

    # ensure the schema exists
    schema_ddl = (f"IF SCHEMA_ID('{CAT_SCHEMA}') IS NULL "
                  f"EXEC('CREATE SCHEMA {CAT_SCHEMA}');")

    print(f"-- target: {a.server} / {a.database}")
    print(f"-- mode  : {'APPLY' if a.apply else 'DRY-RUN (print only)'}"
          f"{'  [DROP+RECREATE]' if a.drop else ''}\n")
    print(schema_ddl + "\n")
    if a.apply:
        cur.execute(schema_ddl)

    tables = mirrors_to_build(cur)
    print(f"-- mirror set: {len(tables)} tables "
          f"({len(discover_tables(cur))} generic-loop, "
          f"{len(tables) - len(discover_tables(cur))} dedicated-promoter):")
    print("--   " + ", ".join(tables) + "\n")

    for dv in tables:
        ddl = build_ddl(cur, dv, a.drop)
        print(ddl)
        if a.apply and not ddl.lstrip().startswith("-- SKIP"):
            try:
                cur.execute(ddl)
                print(f"-- applied {cat_name(dv)}\n")
            except Exception as e:
                print(f"-- ERROR {cat_name(dv)}: {e}\n", file=sys.stderr)

    con.close()
    print("-- done" if a.apply else "-- dry-run complete (use --apply to execute)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
