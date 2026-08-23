"""
seed_catalog.py  —  PPDM Loader · Seed Catalog
===============================================
Loads ppdm39_seed_catalog.json and seeds reference (r_/ra_) tables.
"""

from __future__ import annotations

import itertools
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
import sys

# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════════
# AUDIT COLUMNS
# ═══════════════════════════════════════════════════════════════════════

_AUDIT_COLS = {
    "ACTIVE_IND", "ROW_CREATED_BY", "ROW_CHANGED_BY", "ROW_QUALITY",
    "ROW_CREATED_DATE", "ROW_CHANGED_DATE", "ROW_EFFECTIVE_DATE",
    "ROW_EXPIRY_DATE", "PPDM_GUID", "ROW_VERSION_NUMBER", "SOURCE",
}

_AUDIT_EXPR = {
    "ACTIVE_IND":         "'Y'",
    "ROW_CREATED_BY":     "'PPDM_LOADER'",
    "ROW_CHANGED_BY":     "'PPDM_LOADER'",
    # ROW_QUALITY omitted — FK to r_ppdm_row_quality, let DB default to NULL
    "ROW_VERSION_NUMBER": "1",
    "SOURCE":             "'PPDM'",
    "ROW_CREATED_DATE":   "GETUTCDATE()",
    "ROW_CHANGED_DATE":   "GETUTCDATE()",
    "ROW_EFFECTIVE_DATE": "CAST('1900-01-01' AS DATETIME2)",
    "ROW_EXPIRY_DATE":    "CAST('2099-12-31' AS DATETIME2)",
    "PPDM_GUID":          "NEWID()",
}

_AUDIT_EXPR_ORACLE = {
    "ACTIVE_IND":         "'Y'",
    "ROW_CREATED_BY":     "'PPDM_LOADER'",
    "ROW_CHANGED_BY":     "'PPDM_LOADER'",
    "ROW_VERSION_NUMBER": "1",
    "SOURCE":             "'PPDM'",
    "ROW_CREATED_DATE":   "SYS_EXTRACT_UTC(SYSTIMESTAMP)",
    "ROW_CHANGED_DATE":   "SYS_EXTRACT_UTC(SYSTIMESTAMP)",
    "ROW_EFFECTIVE_DATE": "TO_DATE('1900-01-01','YYYY-MM-DD')",
    "ROW_EXPIRY_DATE":    "TO_DATE('2099-12-31','YYYY-MM-DD')",
    "PPDM_GUID":          "RAWTOHEX(SYS_GUID())",
}

_AUDIT_EXPR_SNOWFLAKE = {
    "ACTIVE_IND":         "'Y'",
    "ROW_CREATED_BY":     "'PPDM_LOADER'",
    "ROW_CHANGED_BY":     "'PPDM_LOADER'",
    "ROW_VERSION_NUMBER": "1",
    "SOURCE":             "'PPDM'",
    "ROW_CREATED_DATE":   "CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())",
    "ROW_CHANGED_DATE":   "CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())",
    "ROW_EFFECTIVE_DATE": "TO_DATE('1900-01-01','YYYY-MM-DD')",
    "ROW_EXPIRY_DATE":    "TO_DATE('2099-12-31','YYYY-MM-DD')",
    "PPDM_GUID":          "UUID_STRING()",
}


def _get_dialect(engine) -> str:
    """Return dialect name: 'sqlserver', 'oracle', or 'snowflake'."""
    try:
        from dataview.core.db import get_dialect as _gd
        return _gd(engine).name
    except Exception:
        return "sqlserver"


def _q(name: str, dialect: str) -> str:
    """Quote an identifier for the given dialect."""
    if dialect == "oracle":
        return f'"{name.upper()}"'
    if dialect == "snowflake":
        return f'"{name.upper()}"'
    return f"[{name}]"


def _audit_exprs(dialect: str) -> dict:
    if dialect == "oracle":
        return _AUDIT_EXPR_ORACLE
    if dialect == "snowflake":
        return _AUDIT_EXPR_SNOWFLAKE
    return _AUDIT_EXPR


def _get_schema(engine, dialect: str, schema: str) -> str:
    """Resolve effective schema name for the dialect."""
    if dialect == "oracle":
        if schema and schema.upper() not in ("DBO", "STG", ""):
            return schema.upper()
        try:
            from sqlalchemy import text as _t
            with engine.connect() as _c:
                return _c.execute(_t(
                    "SELECT SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM dual"
                )).scalar() or "PERRY"
        except Exception:
            return "PERRY"
    if dialect == "snowflake":
        if schema and schema.upper() not in ("DBO", "STG", ""):
            return schema.upper()
        try:
            from sqlalchemy import text as _t
            with engine.connect() as _c:
                return _c.execute(_t("SELECT CURRENT_SCHEMA()")).scalar() or "PUBLIC"
        except Exception:
            return "PUBLIC"
    return schema  # SQL Server — use as-is


def _full_tbl(schema: str, table: str, dialect: str) -> str:
    """Return fully-qualified table name."""
    if dialect == "oracle":
        return f'"{schema.upper()}"."{table.upper()}"'
    if dialect == "snowflake":
        return f'"{schema}"."{table}"'
    return f"[{schema}].[{table}]"


def _get_db_cols(engine, schema: str, table: str, dialect: str) -> set[str]:
    """Return set of uppercase column names for a table."""
    from sqlalchemy import text as _t
    try:
        if dialect == "oracle":
            with engine.connect() as con:
                rows = con.execute(_t(
                    "SELECT column_name FROM all_tab_columns "
                    "WHERE owner=:sch AND table_name=:tbl"
                ), {"sch": schema.upper(), "tbl": table.upper()}).fetchall()
        elif dialect == "snowflake":
            with engine.connect() as con:
                rows = con.execute(_t(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=:sch AND table_name=:tbl"
                ), {"sch": schema.upper(), "tbl": table.upper()}).fetchall()
        else:
            with engine.connect() as con:
                rows = con.execute(_t(
                    "SELECT c.name FROM sys.columns c "
                    "JOIN sys.tables t ON t.object_id=c.object_id "
                    "JOIN sys.schemas s ON s.schema_id=t.schema_id "
                    "WHERE t.name=:tbl AND s.name=:sch"
                ), {"tbl": table, "sch": schema}).fetchall()
        return {r[0].upper() for r in rows}
    except Exception:
        return set()


# ═══════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class GridRow:
    target_column:  str
    include:        bool
    is_pk:          bool
    source_column:  str
    constant_value: str


@dataclass
class SeedSpec:
    table_schema: str
    table_name:   str
    pk_columns:   list[str]
    grid_rows:    list[GridRow]
    abs_path:     str = ""
    raw_rows:     list[dict] = field(default_factory=list)

    @property
    def table_fqn(self) -> str:
        return f"{self.table_schema}.{self.table_name}"

    @property
    def included_rows(self) -> list[GridRow]:
        return [r for r in self.grid_rows if r.include]

    @property
    def source_columns(self) -> list[str]:
        return [
            r.source_column for r in self.included_rows
            if r.source_column and r.target_column.upper() not in _AUDIT_COLS
        ]

    @property
    def needs_source(self) -> bool:
        return len(self.source_columns) > 0

    @property
    def is_static(self) -> bool:
        return not self.needs_source


@dataclass
class CatalogEntry:
    table:      str
    file:       str
    mode:       str
    abs_path:   str = ""
    schema:     str = "dbo"
    table_name: str = ""
    spec: Optional[SeedSpec] = field(default=None, repr=False)

    def __post_init__(self):
        parts = self.table.split(".", 1)
        self.schema     = parts[0] if len(parts) == 2 else "dbo"
        self.table_name = parts[1] if len(parts) == 2 else parts[0]


@dataclass
class SeedResult:
    entry:         CatalogEntry
    ok:            bool
    message:       str
    rows_in_spec:  int = 0
    rows_existing: int = 0
    rows_inserted: int = 0
    rows_skipped:  int = 0


@dataclass
class CatalogLoadResult:
    ok:           bool
    message:      str
    entries:      list[CatalogEntry] = field(default_factory=list)
    catalog_path: str = ""
    root:         str = ""


# ═══════════════════════════════════════════════════════════════════════
# CATALOG LOADER
# ═══════════════════════════════════════════════════════════════════════

def load_catalog(catalog_path: str) -> CatalogLoadResult:
    p = Path(catalog_path)
    if not p.exists():
        return CatalogLoadResult(
            ok=False,
            message=f"Catalog file not found: {catalog_path}",
            catalog_path=catalog_path,
        )

    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        return CatalogLoadResult(
            ok=False, message=f"Failed to parse catalog JSON: {exc}",
            catalog_path=catalog_path,
        )

    root = p.parent
    raw_entries = data.get("entries", [])
    if not raw_entries:
        return CatalogLoadResult(
            ok=False, message="Catalog has no entries.",
            catalog_path=catalog_path, root=str(root),
        )

    entries: list[CatalogEntry] = []
    for raw in raw_entries:
        rel      = raw.get("file", "").replace("\\", os.sep).replace("/", os.sep)
        abs_path = str(root / rel)
        entry    = CatalogEntry(
            table    = raw.get("table", ""),
            file     = rel,
            mode     = raw.get("mode", "missing_only"),
            abs_path = abs_path,
        )
        if Path(abs_path).exists():
            ok, _, spec = _parse_seed_file(abs_path, entry.schema, entry.table_name)
            entry.spec = spec if ok else None
        entries.append(entry)

    loaded  = sum(1 for e in entries if e.spec is not None)
    missing = len(entries) - loaded
    suffix  = (f" ({loaded} spec files found, {missing} not yet on disk)"
               if missing else "")

    return CatalogLoadResult(
        ok=True,
        message=f"Catalog loaded — {len(entries)} entries{suffix}",
        entries=entries,
        catalog_path=catalog_path,
        root=str(root),
    )


# ═══════════════════════════════════════════════════════════════════════
# SEED FILE PARSER
# ═══════════════════════════════════════════════════════════════════════

def _parse_seed_file(
    abs_path:   str,
    schema:     str = "dbo",
    table_name: str = "",
) -> tuple[bool, str, Optional[SeedSpec]]:
    p = Path(abs_path)
    if not p.exists():
        return False, f"Seed file not found: {abs_path}", None

    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:
        return False, f"Failed to parse {p.name}: {exc}", None

    # Accept either a plain array [{...}, ...] or a wrapped object
    # {name, model, version, rows: [...]}. Normalise to dict form.
    if isinstance(raw, list):
        raw = {"rows": raw}
    elif not isinstance(raw, dict):
        return False, f"{p.name}: JSON must be an array of objects or a JSON object", None

    raw_schema = raw.get("table_schema", schema)
    raw_table  = raw.get("table_name",   table_name or p.stem)
    raw_pk     = [c.upper() for c in raw.get("pk_columns", [])]
    raw_grid   = raw.get("grid_rows", [])
    raw_rows   = raw.get("rows", [])

    if not raw_grid and not raw_rows:
        return False, f"{p.name}: no grid_rows or rows found", None

    grid_rows: list[GridRow] = []

    if raw_grid:
        for r in raw_grid:
            tgt   = str(r.get("target_column",  "")).upper().strip()
            inc   = str(r.get("include",        "N")).upper().strip() == "Y"
            is_pk = str(r.get("is_pk",          "")).upper().strip() == "Y"
            src   = str(r.get("source_column",  "")).strip()
            const = str(r.get("constant_value", "")).strip()
            if tgt:
                grid_rows.append(GridRow(
                    target_column  = tgt,
                    include        = inc,
                    is_pk          = is_pk,
                    source_column  = src,
                    constant_value = const,
                ))
        if not raw_pk:
            raw_pk = [r.target_column for r in grid_rows if r.is_pk]

    else:
        # rows format — derive PK from table name stem
        first_row = raw_rows[0] if raw_rows else {}
        if not raw_pk:
            stem = raw_table.upper().replace("R_", "", 1)
            raw_pk = [stem] if stem in {k.upper() for k in first_row} else [list(first_row.keys())[0].upper()]

        for col in first_row.keys():
            col_up = col.upper()
            is_pk  = col_up in {pk.upper() for pk in raw_pk}
            grid_rows.append(GridRow(
                target_column  = col_up,
                include        = col_up not in _AUDIT_COLS,
                is_pk          = is_pk,
                source_column  = "",
                constant_value = "",
            ))

    return True, f"{p.name}: {len(grid_rows)} columns", SeedSpec(
        table_schema = raw_schema,
        table_name   = raw_table,
        pk_columns   = raw_pk,
        grid_rows    = grid_rows,
        abs_path     = abs_path,
        raw_rows     = raw_rows,
    )


# ═══════════════════════════════════════════════════════════════════════
# ROW GENERATOR
# ═══════════════════════════════════════════════════════════════════════

def generate_candidate_rows(
    spec:      SeedSpec,
    source_df: Optional[pd.DataFrame] = None,
) -> tuple[bool, str, list[dict]]:
    # rows format — emit verbatim, uppercasing string values, strip audit cols
    if spec.raw_rows:
        candidates = []
        for row in spec.raw_rows:
            candidates.append({
                k.upper(): (v.upper().strip() if isinstance(v, str) else v)
                for k, v in row.items()
                if k.upper() not in _AUDIT_COLS
            })
        return True, f"{len(candidates)} candidate row(s)", candidates

    included   = [r for r in spec.included_rows if r.target_column not in _AUDIT_COLS]
    src_rows   = [r for r in included if r.source_column]
    const_rows = [r for r in included if r.constant_value and not r.source_column]

    if src_rows:
        if source_df is None:
            return False, "Source data required but not provided.", []
        df_cols_upper = {c.upper() for c in source_df.columns}
        missing = [r.source_column.upper() for r in src_rows
                   if r.source_column.upper() not in df_cols_upper]
        if missing:
            return False, f"Source column(s) not found in data: {missing}", []

    if src_rows and source_df is not None:
        col_map = {c.upper(): c for c in source_df.columns}
        value_lists: list[list[str]] = []
        for r in src_rows:
            actual = col_map[r.source_column.upper()]
            vals = (
                source_df[actual].dropna()
                .astype(str).str.strip().str.upper()
                .replace("", None).dropna()
                .unique().tolist()
            )
            value_lists.append(vals if vals else [""])

        candidate_rows: list[dict] = []
        for combo in itertools.product(*value_lists):
            row: dict[str, str] = {}
            for spec_row, val in zip(src_rows, combo):
                row[spec_row.target_column] = val
            for cr in const_rows:
                row[cr.target_column] = cr.constant_value.upper().strip()
            candidate_rows.append(row)
    else:
        if not const_rows:
            return False, "No included columns with source or constant values.", []
        candidate_rows = [
            {cr.target_column: cr.constant_value.upper().strip()
             for cr in const_rows}
        ]

    return True, f"{len(candidate_rows)} candidate row(s)", candidate_rows


# ═══════════════════════════════════════════════════════════════════════
# DB HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _get_existing_pks(engine, schema: str, table: str,
                      pk_cols: list[str]) -> set[tuple]:
    from sqlalchemy import text
    dialect  = _get_dialect(engine)
    eff_sch  = _get_schema(engine, dialect, schema)
    cols_sql = ", ".join(_q(c, dialect) for c in pk_cols)
    not_null = " AND ".join(f"{_q(c, dialect)} IS NOT NULL" for c in pk_cols)
    full     = _full_tbl(eff_sch, table, dialect)
    with engine.connect() as con:
        rows = con.execute(
            text(f"SELECT DISTINCT {cols_sql} FROM {full} WHERE {not_null}")
        ).fetchall()
    return {tuple(str(v).upper().strip() if v else "" for v in row) for row in rows}


def _get_db_col_names(engine, schema: str, table: str) -> set[str]:
    dialect = _get_dialect(engine)
    eff_sch = _get_schema(engine, dialect, schema)
    return _get_db_cols(engine, eff_sch, table, dialect)


# ═══════════════════════════════════════════════════════════════════════
# SEEDER  — fixed INSERT using named bindparams
# ═══════════════════════════════════════════════════════════════════════

def seed_table(
    engine,
    entry:     CatalogEntry,
    source_df: Optional[pd.DataFrame] = None,
) -> SeedResult:
    schema = entry.schema
    table  = entry.table_name

    spec = entry.spec
    if spec is None:
        ok, msg, spec = _parse_seed_file(entry.abs_path, schema, table)
        if not ok:
            return SeedResult(entry=entry, ok=False, message=msg)
        entry.spec = spec

    ok, msg, candidates = generate_candidate_rows(spec, source_df)
    if not ok:
        return SeedResult(entry=entry, ok=False, message=f"{table}: {msg}")
    if not candidates:
        return SeedResult(entry=entry, ok=True,
                          message=f"{table}: no candidates", rows_in_spec=0)

    try:
        from sqlalchemy import text
        dialect  = _get_dialect(engine)
        eff_sch  = _get_schema(engine, dialect, schema)
        full     = _full_tbl(eff_sch, table, dialect)
        audit_ex = _audit_exprs(dialect)
        pk_cols  = [c.upper() for c in spec.pk_columns]

        # 1. Get DB columns
        db_cols = _get_db_cols(engine, eff_sch, table, dialect)

        # 2. Determine which candidate columns map to DB columns
        first     = candidates[0]
        data_cols = [c.upper() for c in first.keys()
                     if c.upper() not in _AUDIT_COLS and c.upper() in db_cols]
        audit_cols_present = [c for c in audit_ex if c in db_cols]

        if not data_cols:
            return SeedResult(entry=entry, ok=True,
                              message=f"{table}: no matching columns",
                              rows_in_spec=len(candidates))

        tgt_cols     = [_q(c, dialect) for c in data_cols] + [_q(c, dialect) for c in audit_cols_present]
        audit_values = [audit_ex[c] for c in audit_cols_present]

        # 3. Build INSERT ... SELECT with NOT EXISTS dedup
        col_to_idx = {col: i for i, col in enumerate(data_cols)}
        pk_in_data = [pk for pk in pk_cols if pk in col_to_idx]

        params: dict = {}
        rows_inserted = 0
        rows_skipped  = 0

        if dialect == "oracle":
            # Oracle: INSERT INTO t (cols) SELECT vals FROM DUAL WHERE NOT EXISTS (...)
            # Run one statement per row to avoid UNION ALL size limits
            with engine.begin() as con:
                for ri, row in enumerate(candidates):
                    row_vals = []
                    for ci, col in enumerate(data_cols):
                        pname = f"r{ri}c{ci}"
                        val   = row.get(col)
                        params[pname] = str(val).strip() if val is not None else ""
                        row_vals.append(f":{pname}")

                    select_part  = ", ".join(row_vals) + (", " + ", ".join(audit_values) if audit_values else "")
                    if entry.mode == "missing_only" and pk_in_data:
                        ne_parts = " AND ".join(
                            f"t2.{_q(pk, dialect)} = :{f'r{ri}c{col_to_idx[pk]}'}"
                            for pk in pk_in_data
                        )
                        not_exists = (f"WHERE NOT EXISTS ("
                                      f"SELECT 1 FROM {full} t2 WHERE {ne_parts})")
                    else:
                        not_exists = ""
                    sql = (f"INSERT INTO {full} ({', '.join(tgt_cols)}) "
                           f"SELECT {select_part} FROM DUAL {not_exists}")
                    result = con.execute(text(sql), params)
                    if result.rowcount > 0:
                        rows_inserted += 1
                    else:
                        rows_skipped  += 1

        elif dialect == "snowflake":
            # Snowflake: same VALUES pattern as SQL Server but with double-quote identifiers
            val_aliases  = [f"c{i}" for i in range(len(data_cols))]
            col_to_alias = {col: alias for col, alias in zip(data_cols, val_aliases)}
            pk_aliases   = [col_to_alias[pk] for pk in pk_in_data]
            select_exprs = [f"v.{a}" for a in val_aliases] + audit_values

            if entry.mode == "missing_only" and pk_aliases:
                pk_join    = " AND ".join(
                    f"t2.{_q(pk, dialect)} = v.{alias}"
                    for pk, alias in zip(pk_in_data, pk_aliases)
                )
                not_exists = (f"WHERE NOT EXISTS ("
                              f"SELECT 1 FROM {full} t2 WHERE {pk_join})")
            else:
                not_exists = ""

            value_rows: list[str] = []
            for ri, row in enumerate(candidates):
                placeholders = []
                for ci, col in enumerate(data_cols):
                    pname = f"r{ri}c{ci}"
                    val   = row.get(col)
                    params[pname] = str(val).strip() if val is not None else ""
                    placeholders.append(f":{pname}")
                value_rows.append(f"({', '.join(placeholders)})")

            values_clause = f"(VALUES {', '.join(value_rows)}) v({', '.join(val_aliases)})"
            sql = (f"INSERT INTO {full} ({', '.join(tgt_cols)}) "
                   f"SELECT {', '.join(select_exprs)} FROM {values_clause} {not_exists}")
            with engine.begin() as con:
                result = con.execute(text(sql), params)
                rows_inserted = result.rowcount if result.rowcount >= 0 else len(candidates)
                rows_skipped  = len(candidates) - rows_inserted

        else:
            # SQL Server: INSERT INTO [s].[t] SELECT FROM (VALUES ...) v(...)
            val_aliases  = [f"c{i}" for i in range(len(data_cols))]
            col_to_alias = {col: alias for col, alias in zip(data_cols, val_aliases)}
            pk_aliases   = [col_to_alias[pk] for pk in pk_in_data]
            select_exprs = [f"v.{a}" for a in val_aliases] + audit_values

            if entry.mode == "missing_only" and pk_aliases:
                pk_join    = " AND ".join(
                    f"t2.{_q(pk, dialect)} = v.{alias}"
                    for pk, alias in zip(pk_in_data, pk_aliases)
                )
                not_exists = (f"WHERE NOT EXISTS ("
                              f"SELECT 1 FROM {full} t2 WHERE {pk_join})")
            else:
                not_exists = ""

            value_rows = []
            for ri, row in enumerate(candidates):
                placeholders = []
                for ci, col in enumerate(data_cols):
                    pname = f"r{ri}c{ci}"
                    val   = row.get(col)
                    params[pname] = str(val).strip() if val is not None else ""
                    placeholders.append(f":{pname}")
                value_rows.append(f"({', '.join(placeholders)})")

            values_clause = f"(VALUES {', '.join(value_rows)}) v({', '.join(val_aliases)})"
            sql = (f"INSERT INTO {full} ({', '.join(tgt_cols)}) "
                   f"SELECT {', '.join(select_exprs)} FROM {values_clause} {not_exists}")
            with engine.begin() as con:
                result = con.execute(text(sql), params)
                rows_inserted = result.rowcount if result.rowcount >= 0 else len(candidates)
                rows_skipped  = len(candidates) - rows_inserted

        return SeedResult(
            entry=entry, ok=True,
            message=f"{table}: {rows_inserted} inserted, {rows_skipped} already existed",
            rows_in_spec=len(candidates),
            rows_existing=rows_skipped,
            rows_inserted=rows_inserted,
            rows_skipped=rows_skipped,
        )

    except Exception as exc:
        return SeedResult(entry=entry, ok=False, message=f"{table}: {exc}")


def seed_all(
    engine,
    entries:   list[CatalogEntry],
    selected:  Optional[list[str]] = None,
    source_df: Optional[pd.DataFrame] = None,
) -> list[SeedResult]:
    targets = entries
    if selected is not None:
        sel_set = {s.upper() for s in selected}
        targets = [e for e in entries if e.table_name.upper() in sel_set]

    # Sort by FK dependency order so parents are seeded before children
    if engine is not None and targets:
        try:
            targets, _ = sort_entries_by_fk(engine, targets)
        except Exception:
            pass  # Fall back to catalog order if introspection fails

    results: list[SeedResult] = []
    for entry in targets:
        spec = entry.spec
        if spec is not None and spec.needs_source and source_df is None:
            results.append(SeedResult(
                entry=entry, ok=False,
                message=f"{entry.table_name}: requires source data — load a file first",
            ))
            continue
        results.append(seed_table(engine, entry, source_df))
    return results


# ═══════════════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import tempfile, json as _j, os as _os, shutil

    print("=" * 60)
    print("seed_catalog.py  --  self-test")
    print("=" * 60)

    tmpdir   = tempfile.mkdtemp()
    seed_dir = _os.path.join(tmpdir, "seeds", "core")
    _os.makedirs(seed_dir)

    # [1] Static seed
    with open(_os.path.join(seed_dir, "r_well_class.json"), "w") as f:
        _j.dump({
            "table_schema":"dbo","table_name":"r_well_class",
            "pk_columns":["WELL_CLASS"],
            "grid_rows":[
                {"include":"Y","is_pk":"Y","target_column":"WELL_CLASS",
                 "source_column":"","constant_value":"WILDCAT"},
                {"include":"Y","is_pk":"N","target_column":"LONG_NAME",
                 "source_column":"","constant_value":"Wildcat Well"},
                {"include":"N","is_pk":"","target_column":"ACTIVE_IND",
                 "source_column":"","constant_value":""},
            ]
        }, f)

    ok, msg, spec = _parse_seed_file(_os.path.join(seed_dir,"r_well_class.json"))
    assert ok and spec.is_static and spec.pk_columns == ["WELL_CLASS"]
    ok2, _, rows = generate_candidate_rows(spec)
    assert ok2 and len(rows)==1
    assert rows[0]["WELL_CLASS"]=="WILDCAT" and rows[0]["LONG_NAME"]=="WILDCAT WELL"
    print(f"  [1] static seed: {rows[0]}")

    # [2] Source-driven seed
    with open(_os.path.join(seed_dir,"r_well_status.json"), "w") as f:
        _j.dump({
            "table_schema":"dbo","table_name":"r_well_status",
            "pk_columns":["STATUS_TYPE","STATUS"],
            "grid_rows":[
                {"include":"Y","is_pk":"Y","target_column":"STATUS_TYPE",
                 "source_column":"","constant_value":"OPER"},
                {"include":"Y","is_pk":"Y","target_column":"STATUS",
                 "source_column":"CURRENT_STATUS__NAT","constant_value":""},
                {"include":"N","is_pk":"","target_column":"LONG_NAME",
                 "source_column":"","constant_value":""},
            ]
        }, f)

    ok, _, spec2 = _parse_seed_file(_os.path.join(seed_dir,"r_well_status.json"))
    assert ok and spec2.needs_source and spec2.source_columns==["CURRENT_STATUS__NAT"]
    df = pd.DataFrame({"CURRENT_STATUS__NAT":["DRILLING","PRODUCING","DRILLING","ABANDONED"]})
    ok3, _, rows2 = generate_candidate_rows(spec2, df)
    assert ok3 and len(rows2)==3
    assert {r["STATUS"] for r in rows2} == {"DRILLING","PRODUCING","ABANDONED"}
    assert all(r["STATUS_TYPE"]=="OPER" for r in rows2)
    print(f"  [2] source-driven: {len(rows2)} rows")

    # [3] Missing source_df
    ok4, msg4, _ = generate_candidate_rows(spec2, None)
    assert not ok4
    print(f"  [3] missing df: {msg4}")

    # [4] Missing column
    ok5, msg5, _ = generate_candidate_rows(spec2, pd.DataFrame({"WRONG":["A"]}))
    assert not ok5 and "CURRENT_STATUS__NAT" in msg5
    print(f"  [4] missing col: {msg5[:60]}")

    # [5] Catalog load + rows format
    cat_dir  = _os.path.join(tmpdir, "catalog"); _os.makedirs(cat_dir)
    cat_seed = _os.path.join(cat_dir, "seeds", "core"); _os.makedirs(cat_seed)
    shutil.copy(_os.path.join(seed_dir, "r_well_class.json"),  cat_seed)
    shutil.copy(_os.path.join(seed_dir, "r_well_status.json"), cat_seed)
    rows_file = _os.path.join(cat_seed, "r_fluid_type.json")
    with open(rows_file, "w") as f:
        _j.dump({"name":"dbo.r_fluid_type","model":"ppdm39","version":"1.0",
                 "rows":[
                     {"FLUID_TYPE":"OIL","LONG_NAME":"Oil","SHORT_NAME":"OIL","ACTIVE_IND":"Y","SOURCE":"PPDM"},
                     {"FLUID_TYPE":"GAS","LONG_NAME":"Gas","SHORT_NAME":"GAS","ACTIVE_IND":"Y","SOURCE":"PPDM"},
                 ]}, f)
    cat_path = _os.path.join(cat_dir, "ppdm39_seed_catalog.json")
    with open(cat_path, "w") as f:
        _j.dump({"name":"test","entries":[
            {"table":"dbo.r_well_class","file":"seeds/core/r_well_class.json",
             "format":"json","mode":"missing_only","model":"ppdm39","version":"1.0"},
            {"table":"dbo.r_well_status","file":"seeds/core/r_well_status.json",
             "format":"json","mode":"missing_only","model":"ppdm39","version":"1.0"},
            {"table":"dbo.r_fluid_type","file":"seeds/core/r_fluid_type.json",
             "format":"json","mode":"missing_only","model":"ppdm39","version":"1.0"},
        ]}, f)
    cat = load_catalog(cat_path)
    assert cat.ok and len(cat.entries) == 3
    assert cat.entries[0].spec is not None
    assert cat.entries[0].spec.is_static
    assert cat.entries[1].spec.needs_source
    assert cat.entries[2].spec is not None
    ok_rows, _, fluid_rows = generate_candidate_rows(cat.entries[2].spec)
    assert ok_rows and len(fluid_rows) == 2, f"expected 2 fluid rows, got {fluid_rows}"
    print(f"  [5] catalog: {len(cat.entries)} entries, rows-format: {len(fluid_rows)} rows OK")

    # [6] Bad path
    bad = load_catalog("/nonexistent/path.json")
    assert not bad.ok
    print(f"  [6] bad path: {bad.message}")

    print("\nAll tests passed")





# ═══════════════════════════════════════════════════════════════════════
# SERVER-SIDE BULK SEED  —  OPENROWSET / OPENJSON approach
# ═══════════════════════════════════════════════════════════════════════

_STAGE_TABLE  = "ppdm_seed_stage"
_STAGE_SCHEMA = "stg"


def check_adhoc_queries(engine) -> tuple[bool, str]:
    """
    Check whether Ad Hoc Distributed Queries is enabled on the server.
    Required for OPENROWSET(BULK ...).
    Returns (enabled: bool, message: str).
    Fails open — if sys.configurations is unreadable, assumes enabled
    and lets OPENROWSET surface its own error if truly disabled.
    """
    from sqlalchemy import text
    try:
        with engine.connect() as con:
            row = con.execute(text(
                "SELECT value_in_use FROM sys.configurations "
                "WHERE name = 'Ad Hoc Distributed Queries'"
            )).fetchone()
        if row is None:
            # Can't read config — assume enabled, let OPENROWSET fail if not
            return True, "Could not read sys.configurations — assuming Ad Hoc Distributed Queries is enabled."
        if not bool(row[0]):
            return False, (
                "Ad Hoc Distributed Queries is DISABLED. "
                "Run as sysadmin: EXEC sp_configure 'show advanced options',1; RECONFIGURE; "
                "EXEC sp_configure 'Ad Hoc Distributed Queries',1; RECONFIGURE;"
            )
        return True, "Ad Hoc Distributed Queries is enabled."
    except Exception:
        # No permission to read sys.configurations — fail open
        return True, "Could not verify Ad Hoc Distributed Queries — proceeding anyway."


def _build_openrowset_tsql(
    catalog_path:    str,
    entries:         list[CatalogEntry],
    db_cols_map:     dict[str, set[str]],
    schema:          str = "dbo",
) -> str:
    """
    Build a single T-SQL script that uses OPENROWSET(BULK...) + OPENJSON
    to read each seed JSON file directly from disk and insert into the
    target reference table — no staging table needed.

    Each entry becomes one dynamic-SQL block:
        DECLARE @json NVARCHAR(MAX);
        SELECT @json = BulkColumn FROM OPENROWSET(BULK '...', SINGLE_CLOB) AS j;
        INSERT INTO [dbo].[r_xxx] (cols...)
        SELECT col_exprs
        FROM OPENJSON(@json, '$.rows')
        WITH (col1 type '$."COL1"', ...)
        WHERE NOT EXISTS (...);
    """
    import os

    # Root dir of the catalog file — seed paths are relative to it
    catalog_dir = os.path.dirname(os.path.abspath(catalog_path))

    blocks: list[str] = []

    for entry in entries:
        tup     = entry.table_name.upper()
        db_cols = db_cols_map.get(tup, set())
        if not db_cols:
            blocks.append(f"-- SKIP {entry.table_name}: table not found in DB\n")
            continue

        spec = entry.spec
        if spec is None:
            blocks.append(f"-- SKIP {entry.table_name}: no spec loaded\n")
            continue

        # Resolve absolute path to the seed JSON file
        rel      = entry.file.replace("/", os.sep).replace("\\", os.sep)
        abs_path = os.path.join(catalog_dir, rel)

        # Columns present in both seed file and DB.
        # Exclude audit cols UNLESS they are also PK columns
        # (e.g. r_source has SOURCE as its PK, which is normally an audit col)
        pk_set_upper = {c.upper() for c in spec.pk_columns}
        if spec.raw_rows:
            seed_cols = [c.upper() for c in spec.raw_rows[0].keys()
                         if (c.upper() not in _AUDIT_COLS or c.upper() in pk_set_upper)
                         and c.upper() in db_cols]
        else:
            seed_cols = [r.target_column for r in spec.included_rows
                         if (r.target_column not in _AUDIT_COLS or r.target_column in pk_set_upper)
                         and r.target_column in db_cols]

        if not seed_cols:
            blocks.append(f"-- SKIP {entry.table_name}: no matching columns\n")
            continue

        pk_cols       = [c.upper() for c in spec.pk_columns]
        # Exclude audit cols that are already in seed_cols (e.g. SOURCE is PK on r_source)
        audit_present = [c for c in _AUDIT_EXPR if c in db_cols and c not in seed_cols]

        # OPENJSON WITH clause — all cols are NVARCHAR(4000) for simplicity
        with_cols = ",\n            ".join(
            "[{c}] NVARCHAR(4000) '$.{c}'".format(c=c)
            for c in seed_cols
        )

        # INSERT column list
        ins_cols = ", ".join("[{c}]".format(c=c) for c in seed_cols)
        ins_cols += "".join(", [{c}]".format(c=c) for c in audit_present)

        # SELECT expressions
        sel_exprs = ", ".join("j.[{c}]".format(c=c) for c in seed_cols)
        sel_exprs += "".join(", {expr}".format(expr=_AUDIT_EXPR[c]) for c in audit_present)

        # WHERE NOT EXISTS
        if entry.mode == "missing_only" and pk_cols:
            pk_join = " AND ".join(
                "t.[{pk}] = j.[{pk}]".format(pk=pk)
                for pk in pk_cols if pk in db_cols
            )
            where_clause = (
                "WHERE NOT EXISTS (\n"
                "        SELECT 1 FROM [{s}].[{t}] t\n"
                "        WHERE {j}\n"
                "    )"
            ).format(s=schema, t=entry.table_name, j=pk_join)
        else:
            where_clause = ""

        block = (
            "-- {tbl}\n"
            "BEGIN\n"
            "    DECLARE @json_{safe} NVARCHAR(MAX);\n"
            "    SELECT @json_{safe} = BulkColumn\n"
            "    FROM OPENROWSET(BULK '{path}', SINGLE_CLOB) AS f;\n"
            "\n"
            "    INSERT INTO [{s}].[{tbl}] ({ins})\n"
            "    SELECT {sel}\n"
            "    FROM OPENJSON(@json_{safe}, '$.rows')\n"
            "    WITH (\n"
            "        {with_cols}\n"
            "    ) j\n"
            "    {where};\n"
            "END\n"
        ).format(
            tbl      = entry.table_name,
            safe     = entry.table_name.replace(".", "_"),
            path     = abs_path.replace("'", "''"),
            s        = schema,
            ins      = ins_cols,
            sel      = sel_exprs,
            with_cols= with_cols,
            where    = where_clause,
        )
        blocks.append(block)

    return "\n".join(blocks)


def seed_all_server(
    engine,
    entries:      list[CatalogEntry],
    selected:     Optional[list[str]] = None,
    source_df:    Optional[pd.DataFrame] = None,
    catalog_path: str = "",
) -> list[SeedResult]:
    """
    Seeds reference tables by building a T-SQL script that reads each
    seed JSON file directly from disk using OPENROWSET(BULK...) + OPENJSON.
    Executes the entire script in one engine.begin() call.

    Requires:
      - SQL Server Ad Hoc Distributed Queries enabled
      - Seed JSON files accessible from the SQL Server machine (local Express: same box)
    """
    from sqlalchemy import text

    schema = entries[0].schema if entries else "dbo"

    # ── 1. Resolve and sort targets ───────────────────────────────────
    targets = entries
    if selected is not None:
        sel_set = {s.upper() for s in selected}
        targets = [e for e in entries if e.table_name.upper() in sel_set]

    if engine is not None and targets:
        try:
            targets, _ = sort_entries_by_fk(engine, targets)
        except Exception:
            pass

    # Filter out entries that need source_df but don't have it
    results: list[SeedResult] = []
    valid_targets: list[CatalogEntry] = []
    for entry in targets:
        spec = entry.spec
        if spec is not None and spec.needs_source and source_df is None:
            results.append(SeedResult(
                entry=entry, ok=False,
                message=f"{entry.table_name}: requires source data — load a file first",
            ))
        else:
            valid_targets.append(entry)

    if not valid_targets:
        return results

    # ── 2. Get DB columns for all tables in one query ─────────────────
    all_names  = [e.table_name for e in valid_targets]
    ph         = ", ".join(f":t{i}" for i in range(len(all_names)))
    col_params = {f"t{i}": n for i, n in enumerate(all_names)}
    col_params["sch"] = schema

    # seed_all_server is SQL Server only (uses OPENROWSET/T-SQL)
    # For Oracle/Snowflake use seed_all() instead
    dialect = _get_dialect(engine)
    if dialect != "sqlserver":
        # Fall back to row-by-row seed_all for non-SQL-Server dialects
        return seed_all(engine, entries, selected, source_df)

    with engine.connect() as con:
        col_rows = con.execute(text(
            "SELECT t.name, c.name "
            "FROM sys.columns c "
            "JOIN sys.tables  t ON t.object_id = c.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            f"WHERE s.name = :sch AND t.name IN ({ph})"
        ), col_params).fetchall()

    db_cols_map: dict[str, set[str]] = {}
    for tbl, col in col_rows:
        db_cols_map.setdefault(tbl.upper(), set()).add(col.upper())

    # ── 3. Build the T-SQL script ─────────────────────────────────────
    tsql = _build_openrowset_tsql(catalog_path, valid_targets, db_cols_map, schema)

    # ── 4. Execute one block per table so failures are isolated ─────
    # Re-build per-entry blocks so we can run and report individually
    for entry in valid_targets:
        tup = entry.table_name.upper()
        if tup not in db_cols_map:
            results.append(SeedResult(
                entry=entry, ok=False,
                message=f"{entry.table_name}: table not found in DB",
            ))
            continue

        block_tsql = _build_openrowset_tsql(
            catalog_path, [entry], db_cols_map, schema
        )
        if block_tsql.startswith("-- SKIP"):
            results.append(SeedResult(
                entry=entry, ok=True,
                message=f"{entry.table_name}: skipped — {block_tsql.strip()}",
            ))
            continue

        try:
            with engine.begin() as con:
                con.execute(text(block_tsql))
            results.append(SeedResult(
                entry=entry, ok=True,
                message=f"{entry.table_name}: seeded OK",
                rows_inserted=-1,
            ))
        except Exception as exc:
            results.append(SeedResult(
                entry=entry, ok=False,
                message=f"{entry.table_name}: {exc}",
            ))

    return results

# ═══════════════════════════════════════════════════════════════════════
# FK DEPENDENCY SORTER
# ═══════════════════════════════════════════════════════════════════════

def sort_entries_by_fk(
    engine,
    entries: list[CatalogEntry],
) -> tuple[list[CatalogEntry], list[str]]:
    """
    Returns entries sorted so parents are seeded before children.
    Uses a single T-SQL recursive CTE to do the topological sort
    entirely in SQL Server — one round-trip, no Python graph logic.
    """
    from sqlalchemy import text

    if not entries:
        return entries, []

    schema      = entries[0].schema
    table_names = [e.table_name for e in entries]
    entry_map   = {e.table_name.upper(): e for e in entries}

    ph     = ", ".join(f":t{i}" for i in range(len(table_names)))
    params = {f"t{i}": n for i, n in enumerate(table_names)}
    params["sch"] = schema

    # One query: get FK edges, then sort in Python using Kahn's algorithm
    dialect = _get_dialect(engine)
    eff_sch = _get_schema(engine, dialect, schema)
    params["sch"] = eff_sch

    with engine.connect() as con:
        if dialect == "oracle":
            tbl_in = ",".join(f"'{t.upper()}'" for t in table_names)
            rows = con.execute(text(
                "SELECT con.table_name AS child, rcon.table_name AS parent "
                "FROM all_constraints con "
                "JOIN all_constraints rcon ON rcon.constraint_name=con.r_constraint_name "
                " AND rcon.owner=con.r_owner "
                f"WHERE con.constraint_type='R' AND con.owner=:sch "
                f"AND con.table_name IN ({tbl_in}) "
                f"AND rcon.table_name IN ({tbl_in})"
            ), {"sch": eff_sch.upper()}).fetchall()
        elif dialect == "snowflake":
            tbl_in = ",".join(f"'{t.upper()}'" for t in table_names)
            rows = con.execute(text(
                "SELECT kcu.table_name AS child, rc.table_name AS parent "
                "FROM information_schema.referential_constraints rc "
                "JOIN information_schema.key_column_usage kcu "
                " ON kcu.constraint_name=rc.constraint_name "
                " AND kcu.constraint_schema=rc.constraint_schema "
                f"WHERE UPPER(rc.constraint_schema)=:sch "
                f"AND UPPER(kcu.table_name) IN ({tbl_in}) "
                f"AND UPPER(rc.table_name) IN ({tbl_in})"
            ), {"sch": eff_sch.upper()}).fetchall()
        else:
            rows = con.execute(text(
                "SELECT fk_tbl.name AS child, pk_tbl.name AS parent "
                "FROM sys.foreign_keys fk "
                "JOIN sys.tables  fk_tbl ON fk_tbl.object_id = fk.parent_object_id "
                "JOIN sys.tables  pk_tbl ON pk_tbl.object_id = fk.referenced_object_id "
                "JOIN sys.schemas fk_sch ON fk_sch.schema_id = fk_tbl.schema_id "
                "JOIN sys.schemas pk_sch ON pk_sch.schema_id = pk_tbl.schema_id "
                f"WHERE fk_sch.name = :sch AND pk_sch.name = :sch "
                f"AND fk_tbl.name IN ({ph}) AND pk_tbl.name IN ({ph})"
            ), params).fetchall()

    # Kahn's algorithm
    from collections import deque
    table_set  = {t.upper() for t in table_names}
    in_degree  = {t: 0 for t in table_set}
    dependents: dict[str, list[str]] = {t: [] for t in table_set}

    for child, parent in rows:
        c, p = child.upper(), parent.upper()
        if c != p and c in table_set and p in table_set:
            dependents[p].append(c)
            in_degree[c] += 1

    queue = deque(sorted(t for t, d in in_degree.items() if d == 0))
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for dep in sorted(dependents[node]):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)
    # Append any cyclic remainder
    order.extend(sorted(t for t in table_set if t not in order))

    sorted_entries = [entry_map[t] for t in order if t in entry_map]
    return sorted_entries, [e.table_name for e in sorted_entries]


# ═══════════════════════════════════════════════════════════════════════
# CATALOG VALIDATOR
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class EntryValidation:
    """Validation result for one catalog entry against the live DB."""
    entry:           CatalogEntry
    table_exists:    bool
    missing_columns: list[str]   # columns in seed file not in DB
    extra_columns:   list[str]   # columns in seed file not in DB (non-audit)
    pk_valid:        bool        # all PK columns exist in DB
    missing_pk_cols: list[str]
    db_columns:      set[str]    # full set of DB columns for this table

    @property
    def ok(self) -> bool:
        return self.table_exists and not self.missing_columns and self.pk_valid

    @property
    def status(self) -> str:
        if not self.table_exists:
            return "TABLE NOT FOUND"
        if self.missing_pk_cols:
            return "BAD PK COLUMNS"
        if self.missing_columns:
            return "BAD COLUMNS"
        return "OK"


def validate_catalog(
    engine,
    entries: list[CatalogEntry],
) -> list[EntryValidation]:
    """
    For each catalog entry, introspect the DB to check:
      1. The table exists in the schema
      2. Every column in the seed file exists in the table
      3. Every PK column exists in the table

    Returns a list of EntryValidation — one per entry.
    """
    from sqlalchemy import text

    # Bulk-fetch all columns for all tables in one query
    schema = entries[0].schema if entries else "dbo"
    table_names = [e.table_name for e in entries]

    placeholders = ", ".join(f":t{i}" for i in range(len(table_names)))
    params = {f"t{i}": name for i, name in enumerate(table_names)}
    params["sch"] = schema

    dialect = _get_dialect(engine)
    eff_sch = _get_schema(engine, dialect, schema)
    params["sch"] = eff_sch

    with engine.connect() as con:
        if dialect == "oracle":
            tbl_in = ",".join(f"'{n.upper()}'" for n in table_names)
            rows = con.execute(text(
                f"SELECT table_name, column_name FROM all_tab_columns "
                f"WHERE owner=:sch AND table_name IN ({tbl_in})"
            ), {"sch": eff_sch.upper()}).fetchall()
        elif dialect == "snowflake":
            tbl_in = ",".join(f"'{n.upper()}'" for n in table_names)
            rows = con.execute(text(
                f"SELECT table_name, column_name FROM information_schema.columns "
                f"WHERE UPPER(table_schema)=:sch AND UPPER(table_name) IN ({tbl_in})"
            ), {"sch": eff_sch.upper()}).fetchall()
        else:
            rows = con.execute(text(f"""
                SELECT t.name AS table_name, c.name AS column_name
                FROM sys.columns c
                JOIN sys.tables  t ON t.object_id = c.object_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE s.name = :sch
                  AND t.name IN ({placeholders})
            """), params).fetchall()

    # Build {table_upper: {col_upper, ...}}
    db_schema: dict[str, set[str]] = {}
    for tbl, col in rows:
        db_schema.setdefault(tbl.upper(), set()).add(col.upper())

    results: list[EntryValidation] = []

    for entry in entries:
        tbl_up = entry.table_name.upper()
        db_cols = db_schema.get(tbl_up, set())
        table_exists = tbl_up in db_schema

        spec = entry.spec
        if spec is None:
            # No spec loaded — can only check table existence
            results.append(EntryValidation(
                entry=entry,
                table_exists=table_exists,
                missing_columns=[],
                extra_columns=[],
                pk_valid=table_exists,
                missing_pk_cols=[],
                db_columns=db_cols,
            ))
            continue

        # Columns referenced in the seed file (excluding audit cols)
        if spec.raw_rows:
            seed_cols = {k.upper() for k in (spec.raw_rows[0] if spec.raw_rows else {})}
        else:
            seed_cols = {r.target_column.upper() for r in spec.included_rows}
        seed_cols -= _AUDIT_COLS

        missing_cols = sorted(seed_cols - db_cols) if table_exists else []
        pk_cols      = [c.upper() for c in spec.pk_columns]
        missing_pks  = sorted(c for c in pk_cols if c not in db_cols) if table_exists else pk_cols

        results.append(EntryValidation(
            entry=entry,
            table_exists=table_exists,
            missing_columns=missing_cols,
            extra_columns=[],        # reserved for future use
            pk_valid=table_exists and not missing_pks,
            missing_pk_cols=missing_pks,
            db_columns=db_cols,
        ))

    return results
