"""
dataview/migration/db_source.py
==============================
Stage 2 for the migration app: the source is a TABLE THAT HAS DATA, not a file.

WHY A COPY, NOT A POINTER
-------------------------
The obvious shortcut is to tell the pipeline that `dataview.dv_well` IS its
staging table — no copy, nothing to wait for. That would destroy the data.
import_data/staging.py::_load_to_staging_sqlserver opens with:

    IF OBJECT_ID('[stg].[<table>]','U') IS NOT NULL DROP TABLE [stg].[<table>]

and later ALTERs the table to add _batch_loaded_at. Any code path that treats a
dv_ table as staging is one re-run away from dropping it. So the source stage
COPIES into a real stg.* table and the pipeline downstream behaves exactly as it
does for a file.

WHY NO BCP
----------
The file path needs BCP because the data arrives from outside the database. Here
the source and the staging table live in the SAME database, so this is a plain
server-side INSERT…SELECT — no CSV, no bulk utility, no pandas round-trip. On a
table of any size that is the fastest option available and the least code.

WHY EVERYTHING BECOMES TEXT
---------------------------
Staging from a file is all NVARCHAR(4000): BCP lands raw text and typing happens
later, at promote. This module reproduces that shape deliberately with
CONVERT(nvarchar(4000), …) rather than preserving the dv_ table's real types.
Matching staging's existing contract matters more than fidelity here — Stage 6's
check_fk_violations_server and Stage 8's promote were both written against
all-text staging, and handing them typed columns would be a silent behaviour
change. Dates are converted with style 126 (ISO 8601) so they round-trip
unambiguously instead of picking up the server's locale.

USAGE
-----
    from dataview.migration.db_source import list_source_tables, stage_from_table

    tables = list_source_tables(engine)                 # [(name, rows), ...]
    res    = stage_from_table(engine, "dv_well")        # -> StagingResult
    # then: S.stg_schema, S.stg_table, S._mapping_src_cols  (see stage_session)
"""
from __future__ import annotations

import re

from sqlalchemy import text

from dataview.import_data.staging import StagingResult

SRC_SCHEMA = "dataview"
STG_SCHEMA = "stg"

# Provenance/internal columns that exist to serve DataWrangler and mean nothing
# to a target model. Excluded from the copy so they never reach Stage 5's
# mapping grid as noise the user has to skip by hand.
EXCLUDE_COLS = {
    "CAT_ROW_ID", "PROMOTED", "PROMOTED_AT", "CAPTURED_AT",
    "_BATCH_LOADED_AT", "GEOG",
}

_SAFE_RE = re.compile(r"[^a-zA-Z0-9_]")

# Spatial / binary / large types can't be copied into an NVARCHAR staging column
# meaningfully. They're reported rather than silently dropped.
_UNCOPYABLE = {"geography", "geometry", "image", "varbinary", "binary",
               "hierarchyid", "sql_variant", "xml", "timestamp", "rowversion"}

_DATE_TYPES = {"date", "datetime", "datetime2", "smalldatetime",
               "datetimeoffset", "time"}


def _safe(name: str) -> str:
    return _SAFE_RE.sub("_", name or "")


def stg_table_name(src_table: str) -> str:
    """Staging table name for a source table. Deterministic, so re-staging the
    same source replaces its own staging table rather than accumulating."""
    return f"src_{_safe(src_table).lower()}"[:120]


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def list_source_tables(engine, schema: str = SRC_SCHEMA,
                       with_data_only: bool = True) -> list[tuple[str, int]]:
    """(table_name, row_count) for candidate source tables, biggest first.

    Row counts come from sys.dm_db_partition_stats, which is a metadata read —
    no COUNT(*) scan over 60-odd tables.
    """
    sql = """
        SELECT t.name AS table_name,
               ISNULL(SUM(CASE WHEN ps.index_id IN (0,1)
                               THEN ps.row_count END), 0) AS row_count
        FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        LEFT JOIN sys.dm_db_partition_stats ps ON ps.object_id = t.object_id
        WHERE s.name = :schema
        GROUP BY t.name
        ORDER BY row_count DESC, t.name
    """
    with engine.connect() as conn:
        rows = [(r[0], int(r[1] or 0))
                for r in conn.execute(text(sql), {"schema": schema})]
    if with_data_only:
        rows = [r for r in rows if r[1] > 0]
    return rows


def source_columns(engine, src_table: str, schema: str = SRC_SCHEMA):
    """[(column_name, type_name)] in ordinal order, excluding internal columns.
    sys.* rather than INFORMATION_SCHEMA — the latter carries per-row permission
    checks and is measurably slow on large catalogs."""
    sql = """
        SELECT c.name, ty.name AS type_name
        FROM sys.columns c
        JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        WHERE c.object_id = OBJECT_ID(:t)
        ORDER BY c.column_id
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"t": f"{schema}.{src_table}"}).fetchall()
    return [(r[0], (r[1] or "").lower()) for r in rows
            if r[0].upper() not in EXCLUDE_COLS]


# --------------------------------------------------------------------------- #
# Copy
# --------------------------------------------------------------------------- #
def _convert_expr(col: str, type_name: str) -> str:
    """Source column -> NVARCHAR(4000), matching what BCP staging produces."""
    if type_name in _DATE_TYPES:
        # 126 = ISO8601. Without a style the server's locale decides, which is
        # how a d/m/y source silently becomes m/d/y downstream.
        return f"CONVERT(nvarchar(4000), [{col}], 126)"
    return f"CONVERT(nvarchar(4000), [{col}])"


def stage_from_table(engine, src_table: str, src_schema: str = SRC_SCHEMA,
                     stg_schema: str = STG_SCHEMA, stg_table: str | None = None,
                     limit: int | None = None, where: str | None = None,
                     log=None) -> StagingResult:
    """Copy a source table into a staging table, all columns as NVARCHAR(4000).

    Returns the same StagingResult the file path returns, so the page can treat
    both sources identically.
    """
    def _log(m):
        if log:
            log(m)

    cols = source_columns(engine, src_table, src_schema)
    if not cols:
        return StagingResult(False,
                             f"{src_schema}.{src_table} not found or has no "
                             f"usable columns")

    skipped = [c for c, t in cols if t in _UNCOPYABLE]
    usable = [(c, t) for c, t in cols if t not in _UNCOPYABLE]
    if not usable:
        return StagingResult(False,
                             f"{src_schema}.{src_table} has no copyable columns")
    if skipped:
        _log(f"skipping {len(skipped)} column(s) that can't be staged as text: "
             + ", ".join(skipped))

    stg = stg_table or stg_table_name(src_table)
    full_stg = f"[{stg_schema}].[{stg}]"
    full_src = f"[{src_schema}].[{src_table}]"

    col_defs = ",\n    ".join(f"[{c}] NVARCHAR(4000) NULL" for c, _t in usable)
    sel = ",\n           ".join(_convert_expr(c, t) for c, t in usable)
    names = ", ".join(f"[{c}]" for c, _t in usable)
    top = f"TOP ({int(limit)}) " if limit else ""
    where_sql = f" WHERE {where}" if where else ""

    stmts = [
        f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{stg_schema}') "
        f"EXEC('CREATE SCHEMA [{stg_schema}]')",
        f"IF OBJECT_ID('{full_stg}', 'U') IS NOT NULL DROP TABLE {full_stg}",
        f"CREATE TABLE {full_stg} (\n    {col_defs}\n)",
        f"INSERT INTO {full_stg} ({names})\n    SELECT {top}{sel}\n"
        f"    FROM {full_src}{where_sql}",
        f"ALTER TABLE {full_stg} ADD [_batch_loaded_at] DATETIME2 NULL",
        f"UPDATE {full_stg} SET [_batch_loaded_at] = GETUTCDATE()",
    ]

    try:
        with engine.begin() as conn:
            for s in stmts:
                conn.execute(text(s))
            n = conn.execute(text(f"SELECT COUNT(*) FROM {full_stg}")).scalar()
    except Exception as e:
        return StagingResult(
            False, f"{type(e).__name__}: {str(e).splitlines()[0][:300]}",
            0, stg)

    n = int(n or 0)
    msg = f"Staged {n:,} row(s) from {src_schema}.{src_table} into {stg_schema}.{stg}"
    if skipped:
        msg += f" ({len(skipped)} column(s) skipped)"
    _log(msg)
    return StagingResult(True, msg, n, stg)


# --------------------------------------------------------------------------- #
# Session wiring
# --------------------------------------------------------------------------- #
def stage_session(S, engine, src_table: str, src_schema: str = SRC_SCHEMA,
                  stg_schema: str = STG_SCHEMA, limit: int | None = None,
                  log=None) -> StagingResult:
    """Stage a source table and populate the session keys the rest of the
    pipeline reads, so Stages 5-8 run unchanged.

    Stage 5 takes its dataframe from `S.staging_df` or `S.source_df`; Stage 6's
    server-side FK check works off `S.stg_schema` / `S.stg_table`;
    `S._mapping_src_cols` is what lets a mapping be restored from the cache when
    the session is resumed without the frame in memory. ppdm_agent's
    build_pipeline_context also reads stg_schema/stg_table, so setting them
    keeps the assistant honest about what's loaded.
    """
    res = stage_from_table(engine, src_table, src_schema, stg_schema,
                           limit=limit, log=log)
    if not res.ok:
        return res

    S.stg_schema = stg_schema
    S.stg_table = res.table_name
    S.source_name = f"{src_schema}.{src_table}"
    S._mapping_src_cols = [c for c, _t in source_columns(engine, src_table,
                                                         src_schema)]
    # Downstream state belongs to the previous source — clear it or Stage 5
    # will restore a mapping built for different columns.
    S.col_mapping = None
    S.column_map = None
    S.fk_resolutions = None
    S.fk_violations = None
    return res


def preview(engine, stg_table: str, stg_schema: str = STG_SCHEMA, n: int = 20):
    """First n rows of the staging table, via the pipeline's own previewer so
    the display path is identical to the file source."""
    from dataview.import_data.staging import preview_staging_table
    return preview_staging_table(engine, stg_table, stg_schema, n)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main() -> int:
    """Run from the REPO ROOT with -m, not by path:

        py -m dataview.migration.db_source --list
        py -m dataview.migration.db_source --stage dv_well --limit 100 --preview

    `py dataview\\migration\\db_source.py` fails with ModuleNotFoundError:
    Python puts the SCRIPT'S directory on sys.path, so the dataview package
    isn't visible. -m puts the current directory on instead.
    """
    import argparse
    ap = argparse.ArgumentParser(
        description="Stage a dataview table as a pipeline source")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--schema", default=SRC_SCHEMA)
    ap.add_argument("--stg-schema", default=STG_SCHEMA)
    ap.add_argument("--list", action="store_true",
                    help="list source tables that have rows")
    ap.add_argument("--columns", help="show the columns that would be staged")
    ap.add_argument("--stage", help="source table to stage, e.g. dv_well")
    ap.add_argument("--limit", type=int, help="stage only the first N rows")
    ap.add_argument("--preview", action="store_true",
                    help="show the first rows after staging")
    a = ap.parse_args()

    from dataview.core.schema_introspect import make_engine
    engine = make_engine(a.server, a.database)
    print(f"-- {a.server} · {a.database}.{a.schema}")

    if a.list:
        rows = list_source_tables(engine, a.schema)
        print(f"-- {len(rows)} table(s) with data")
        for name, n in rows:
            print(f"   {name:40} {n:>12,}")
        return 0

    if a.columns:
        cols = source_columns(engine, a.columns, a.schema)
        skipped = [c for c, t in cols if t in _UNCOPYABLE]
        print(f"-- {a.columns}: {len(cols) - len(skipped)} stageable column(s)")
        for c, t in cols:
            flag = "  << skipped (untranslatable type)" if t in _UNCOPYABLE else ""
            print(f"   {c:40} {t}{flag}")
        return 0

    if not a.stage:
        ap.print_help()
        return 1

    res = stage_from_table(engine, a.stage, a.schema, a.stg_schema,
                           limit=a.limit, log=print)
    print(f"-- ok={res.ok} rows={res.rows_loaded} table={res.table_name}")
    if not res.ok:
        print(f"!! {res.message}")
        return 2

    if a.preview:
        ok, msg, rows = preview(engine, res.table_name, a.stg_schema, 5)
        if ok and rows:
            cols = list(rows[0].keys())
            print("   " + " | ".join(cols[:8]))
            for r in rows:
                print("   " + " | ".join(str(r[c])[:18] for c in cols[:8]))
        else:
            print(f"   (preview unavailable: {msg})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
