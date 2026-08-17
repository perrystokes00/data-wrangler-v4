"""
dataview/migration/ppdm_model.py
================================
Build a PPDMSchema for the PPDM 3.9 target database, scoped to the domains we
actually migrate, from LIVE introspection — not a hand-authored JSON catalog.

WHY THIS EXISTS
---------------
core/schema.py loads PPDMSchema from a JSON catalog. Hand-authoring a catalog
for PPDM39 is a non-starter: 2,696 tables, 71,245 columns, 19,848 foreign keys.
core/schema_introspect.build_model() already does live introspection and is NOT
tied to one database (its queries use bare sys.* and read whichever database the
engine is connected to) — but it is UNSCOPED, so it would pull all 2,696 tables
to keep the ~423 we need, and it assembles a diagram-shaped dict rather than the
PPDMSchema dataclasses the pipeline consumes.

So this module mirrors those queries with a scope filter and assembles
schema.py's own dataclasses. Nothing in core/ is modified.

SCOPE — measured, not guessed
-----------------------------
PPDM39.dbo bucketed by name:  REFERENCE 1722 · OTHER 655 · WELL 162 ·
SEISMIC 69 · STRATIGRAPHY 44 · PRODUCTION 44  (2,696 total).

Seeding on the five domain prefixes and closing over foreign keys twice gives
864 tables. Of those:

  * 423 are MAPPING TARGETS (WELL 162 + OTHER-pulled-in-by-FK 104 + SEISMIC 69
    + STRATIGRAPHY 44 + PRODUCTION 44). These need full ColumnDef modelling and
    are what this module returns.
  * 441 are REFERENCE tables (r_*, ra_*). They are deliberately EXCLUDED. You
    never map columns INTO r_well_status — FK resolution reads its values to
    validate and populate a code, and core/fk.py already does that lazily off
    the live engine (get_parent_col_defs / get_existing_parent_values /
    load_fk_samples, with its own caching). Modelling all 441 up front would be
    wasted work and a much slower build.

The FK closure is what pulls in tables the names miss — `well` alone references
business_associate, field, area, well_node, source_document,
ppdm_unit_of_measure, strat_unit and sf_platform, none of which match a domain
prefix but all of which are required.

USAGE
-----
    from dataview.migration.ppdm_model import get_ppdm_schema

    schema = get_ppdm_schema(engine)            # cached after the first build
    tbl    = schema.get_table("well")
    cols   = tbl.columns                        # -> S.target_cols for Stage 4

    python -m dataview.migration.ppdm_model --rebuild --stats
"""
from __future__ import annotations

import os
import pickle
import time
from pathlib import Path

from sqlalchemy import text

from dataview.core.schema import (
    CheckConstraint, ColumnDef, PPDMSchema, TableDef,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
PPDM_DB     = "PPDM39"
PPDM_SCHEMA = "dbo"

CACHE_PATH = Path(__file__).parent / "ppdm_model_cache.pkl"
CACHE_VERSION = 3            # bump to invalidate every cached build

# Domain seeds. REFERENCE is deliberately absent — see the module docstring.
DOMAIN_PATTERNS = {
    "WELL":         ["well%"],
    "SEISMIC":      ["seis%"],
    "STRATIGRAPHY": ["strat%", "lith%"],
    "PRODUCTION":   ["pden%", "prod%"],
}
_SEED_LIKE = [p for pats in DOMAIN_PATTERNS.values() for p in pats]

# The canonical bucket list, in the order a picker should show them. OTHER is
# last and isn't a seed pattern — it's where FK closure deposits tables that
# match no domain prefix but are still required (business_associate, field,
# area, well_node...).
DOMAINS = tuple(DOMAIN_PATTERNS) + ("OTHER",)

# Reference tables are excluded from the model entirely.
_REF_LIKE = ["r[_]%", "ra[_]%"]


def domain_of(table_name: str) -> str:
    """Bucket a table for the Stage-4 picker. Tables dragged in by FK closure
    that match no domain prefix land in OTHER — they're real targets
    (business_associate, field, area...), just not domain-named."""
    n = (table_name or "").lower()
    if n.startswith("seis"):
        return "SEISMIC"
    if n.startswith("strat") or n.startswith("lith"):
        return "STRATIGRAPHY"
    if n.startswith("pden") or n.startswith("prod"):
        return "PRODUCTION"
    if n.startswith("well"):
        return "WELL"
    return "OTHER"


# --------------------------------------------------------------------------- #
# Scope CTE — seed + two levels of FK closure, reference tables removed
# --------------------------------------------------------------------------- #
def _scope_cte() -> str:
    seed = " OR ".join(f"t.name LIKE '{p}'" for p in _SEED_LIKE)
    notref = " AND ".join(f"tt.name NOT LIKE '{p}'" for p in _REF_LIKE)
    return f"""
    WITH seed AS (
        SELECT t.object_id
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = :schema AND ({seed})
    ),
    lvl1 AS (
        SELECT DISTINCT fk.referenced_object_id AS object_id
        FROM sys.foreign_keys fk
        JOIN seed ON seed.object_id = fk.parent_object_id
    ),
    lvl2 AS (
        SELECT DISTINCT fk.referenced_object_id AS object_id
        FROM sys.foreign_keys fk
        JOIN lvl1 ON lvl1.object_id = fk.parent_object_id
    ),
    scope_all AS (
        SELECT object_id FROM seed
        UNION SELECT object_id FROM lvl1
        UNION SELECT object_id FROM lvl2
    ),
    scope AS (
        SELECT sa.object_id
        FROM scope_all sa
        JOIN sys.tables tt ON tt.object_id = sa.object_id
        JOIN sys.schemas ss ON ss.schema_id = tt.schema_id
        WHERE ss.name = :schema AND {notref}
    )
    """


_Q_TABLES = _scope_cte() + """
    SELECT t.object_id, t.name AS table_name
    FROM scope sc
    JOIN sys.tables t ON t.object_id = sc.object_id
    ORDER BY t.name
"""

# Type text is assembled here rather than in Python so the shape matches what
# schema.py's JSON catalog carries ("nvarchar(40)", "numeric(8,0)").
_Q_COLUMNS = _scope_cte() + """
    SELECT t.name AS table_name,
           c.name AS column_name,
           ty.name AS type_name,
           c.max_length, c.precision, c.scale, c.is_nullable, c.column_id
    FROM scope sc
    JOIN sys.tables  t  ON t.object_id = sc.object_id
    JOIN sys.columns c  ON c.object_id = t.object_id
    JOIN sys.types   ty ON ty.user_type_id = c.user_type_id
    ORDER BY t.name, c.column_id
"""

_Q_PKS = _scope_cte() + """
    SELECT t.name AS table_name, c.name AS column_name
    FROM scope sc
    JOIN sys.tables  t  ON t.object_id = sc.object_id
    JOIN sys.indexes i  ON i.object_id = t.object_id AND i.is_primary_key = 1
    JOIN sys.index_columns ic ON ic.object_id = i.object_id
                             AND ic.index_id  = i.index_id
    JOIN sys.columns c  ON c.object_id = ic.object_id
                       AND c.column_id = ic.column_id
"""

# Only FKs whose CHILD is in scope — the parent may be a reference table that
# we deliberately don't model, and that's fine: ColumnDef just records its name
# so FK resolution can go and read it live.
_Q_FKS = _scope_cte() + """
    SELECT tp.name AS child_table,  cp.name AS child_col,
           sr.name AS parent_schema, tr.name AS parent_table, cr.name AS parent_col
    FROM scope sc
    JOIN sys.tables tp ON tp.object_id = sc.object_id
    JOIN sys.foreign_keys fk ON fk.parent_object_id = tp.object_id
    JOIN sys.foreign_key_columns fkc
         ON fkc.constraint_object_id = fk.object_id
    JOIN sys.columns cp ON cp.object_id = fkc.parent_object_id
                       AND cp.column_id = fkc.parent_column_id
    JOIN sys.tables  tr ON tr.object_id = fkc.referenced_object_id
    JOIN sys.schemas sr ON sr.schema_id = tr.schema_id
    JOIN sys.columns cr ON cr.object_id = fkc.referenced_object_id
                       AND cr.column_id = fkc.referenced_column_id
"""

_Q_CHECKS = _scope_cte() + """
    SELECT t.name AS table_name, cc.name AS constraint_name,
           c.name AS column_name, cc.definition
    FROM scope sc
    JOIN sys.tables t ON t.object_id = sc.object_id
    JOIN sys.check_constraints cc ON cc.parent_object_id = t.object_id
    LEFT JOIN sys.columns c ON c.object_id = t.object_id
                           AND c.column_id = cc.parent_column_id
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_SIZED   = {"varchar", "nvarchar", "char", "nchar", "varbinary", "binary"}
_SCALED  = {"decimal", "numeric"}


def _type_text(type_name, max_length, precision, scale) -> str:
    t = (type_name or "").lower()
    if t in _SIZED:
        if max_length == -1:
            return f"{t}(max)"
        n = max_length // 2 if t.startswith("n") else max_length
        return f"{t}({n})"
    if t in _SCALED:
        return f"{t}({precision},{scale})"
    return t


def _parse_check_values(definition: str) -> list[str]:
    """Pull the literal list out of a CHECK definition.

    Handles the two shapes PPDM actually uses — an IN list and a chain of ORed
    equalities. Anything else returns [] rather than a wrong guess: an empty
    allowed_values means 'unconstrained as far as we know', which is safe,
    whereas a mis-parsed list would silently reject valid data.
    """
    import re
    if not definition:
        return []
    vals = re.findall(r"'((?:[^']|'')*)'", definition)
    return [v.replace("''", "'") for v in vals] if vals else []


def _rows(conn, sql, schema):
    return list(conn.execute(text(sql), {"schema": schema}).mappings())


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build_ppdm_schema(engine, schema: str = PPDM_SCHEMA, log=None) -> PPDMSchema:
    """Reflect the in-scope PPDM tables and assemble a PPDMSchema.

    Reads whichever database `engine` is connected to — pass an engine built
    for PPDM39. Uses sys.* throughout; INFORMATION_SCHEMA is a view with
    per-row permission checks and is minutes-slow at this scale.
    """
    def _log(m):
        if log:
            log(m)

    t0 = time.perf_counter()
    with engine.connect() as conn:
        _log("reflecting tables…")
        tbl_rows = _rows(conn, _Q_TABLES, schema)
        _log(f"  {len(tbl_rows)} table(s) in scope")
        _log("reflecting columns…")
        col_rows = _rows(conn, _Q_COLUMNS, schema)
        _log(f"  {len(col_rows)} column(s)")
        _log("reflecting keys…")
        pk_rows = _rows(conn, _Q_PKS, schema)
        fk_rows = _rows(conn, _Q_FKS, schema)
        _log(f"  {len(pk_rows)} PK column(s), {len(fk_rows)} FK column(s)")
        try:
            ck_rows = _rows(conn, _Q_CHECKS, schema)
        except Exception as e:          # CHECK metadata is a bonus, not a gate
            _log(f"  (check constraints unavailable: {type(e).__name__})")
            ck_rows = []

    pk_set = {(r["table_name"].lower(), r["column_name"].lower())
              for r in pk_rows}
    fk_map = {}
    for r in fk_rows:
        fk_map.setdefault(
            (r["child_table"].lower(), r["child_col"].lower()),
            (r["parent_schema"], r["parent_table"], r["parent_col"]))

    ck_map: dict = {}
    for r in ck_rows:
        col = (r["column_name"] or "").lower()
        if not col:
            continue                     # table-level check, not column-scoped
        vals = _parse_check_values(r["definition"])
        if vals:
            ck_map.setdefault((r["table_name"].lower(), col), []).append(
                CheckConstraint(name=r["constraint_name"], column=col,
                                allowed_values=vals))

    tables: dict[str, TableDef] = {}
    for r in tbl_rows:
        name = r["table_name"]
        tables[name.lower()] = TableDef(
            table_schema=schema, table_name=name,
            category=domain_of(name), sub_category="", columns=[])

    for r in col_rows:
        tl = r["table_name"].lower()
        td = tables.get(tl)
        if td is None:
            continue
        cl = r["column_name"].lower()
        fk = fk_map.get((tl, cl))
        td.columns.append(ColumnDef(
            table_schema=schema,
            table_name=r["table_name"],
            column_name=r["column_name"],
            data_type=_type_text(r["type_name"], r["max_length"],
                                 r["precision"], r["scale"]),
            not_null=not bool(r["is_nullable"]),
            is_primary_key=(tl, cl) in pk_set,
            is_foreign_key=fk is not None,
            fk_table_schema=fk[0] if fk else None,
            fk_table_name=fk[1] if fk else None,
            fk_column_name=fk[2] if fk else None,
            check_constraints=ck_map.get((tl, cl), []),
        ))

    categories: dict[str, list[str]] = {}
    for td in tables.values():
        categories.setdefault(td.category, []).append(td.table_name)
    for k in categories:
        categories[k].sort()

    _log(f"built in {time.perf_counter() - t0:.1f}s")
    return PPDMSchema(tables=tables, categories=categories)


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def _db_name(engine) -> str:
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT DB_NAME()")).scalar() or "?"
    except Exception:
        return "?"


def get_ppdm_schema(engine, schema: str = PPDM_SCHEMA, refresh: bool = False,
                    cache_path: Path = CACHE_PATH, log=None) -> PPDMSchema:
    """Cached PPDMSchema. The build costs one full reflection of ~423 tables,
    so it is done once and pickled; PPDM's schema doesn't change under us."""
    db = _db_name(engine)
    key = (CACHE_VERSION, db, schema)
    if not refresh and cache_path.exists():
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            if cached.get("key") == key:
                return cached["schema"]
        except Exception:
            pass

    sch = build_ppdm_schema(engine, schema, log=log)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump({"key": key, "schema": sch, "built": time.time()}, f)
    except Exception as e:
        if log:
            log(f"(cache write failed: {type(e).__name__}: {e})")
    return sch


def clear_cache(cache_path: Path = CACHE_PATH) -> bool:
    try:
        if cache_path.exists():
            os.remove(cache_path)
            return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description=f"Build the scoped {PPDM_DB} target model")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default=PPDM_DB)
    ap.add_argument("--schema", default=PPDM_SCHEMA)
    ap.add_argument("--rebuild", action="store_true",
                    help="ignore the cache and reflect again")
    ap.add_argument("--stats", action="store_true",
                    help="print per-domain table counts")
    ap.add_argument("--table", help="dump one table's columns")
    a = ap.parse_args()

    from dataview.core.schema_introspect import make_engine
    engine = make_engine(a.server, a.database)
    print(f"-- {a.server} · {a.database}.{a.schema}")

    sch = get_ppdm_schema(engine, a.schema, refresh=a.rebuild, log=print)
    print("--", sch.summary())

    if a.stats:
        for cat in sorted(sch.categories, key=lambda c: -len(sch.categories[c])):
            names = sch.categories[cat]
            print(f"   {cat:14} {len(names):>5} table(s)")

    if a.table:
        td = sch.get_table(a.table)
        if not td:
            print(f"!! {a.table} not in scope")
            return 2
        print(f"\n{td!r}")
        for c in td.columns:
            print(f"   {c!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
