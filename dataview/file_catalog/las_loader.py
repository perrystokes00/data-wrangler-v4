"""
modules/las_loader.py

Direct LAS → PPDM promote, bypassing the staging pipeline.

Responsibilities:
  - Extract UWI from a LAS file header
  - Fuzzy-match it against existing WELL rows in the database
  - Build WELL_LOG / WELL_LOG_CURVE / WELL_LOG_CURVE_VALUE DataFrames
  - Insert directly into PPDM tables via dialect-aware methods:

      SQL Server  WELL_LOG / WELL_LOG_CURVE   → executemany (small, < 100 rows)
      SQL Server  WELL_LOG_CURVE_VALUE        → CSV → BULK INSERT → INSERT SELECT
      Oracle                                  → executemany (all tables)
      Snowflake                               → executemany / write_pandas

The WELL row is assumed to already exist. This module never touches WELL.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    import lasio
    from dataview.file_catalog.las_reader import read_las
except ImportError as e:
    raise ImportError("pip install lasio") from e

# Preferred BULK INSERT directory — matches staging.py
_BULK_DIR = r"C:\Bulk"


# ─────────────────────────────────────────────────────────────────────────────
# UWI helpers
# ─────────────────────────────────────────────────────────────────────────────

def extract_las_uwi(las) -> str:
    """Pull UWI/API from a lasio LASFile. Returns '' if not found."""
    for mnemonic in ("UWI", "API", "WELL", "WN"):
        try:
            v = las.well[mnemonic].value
            if v and str(v).strip() not in ("", "None", "UNKNOWN"):
                return str(v).strip()
        except KeyError:
            continue
    return ""


def _normalise(uwi: str) -> str:
    """Strip non-alphanumeric chars and uppercase for fuzzy comparison."""
    return re.sub(r"[^A-Z0-9]", "", uwi.upper())


def fetch_ppdm_uwis(engine) -> list[dict]:
    """
    Return all UWIs and well names from WELL table.
    Each dict: { "UWI": str, "WELL_NAME": str }
    """
    from sqlalchemy import text
    dialect = _detect_dialect(engine)
    try:
        with engine.connect() as con:
            if dialect == "oracle":
                sql = 'SELECT "UWI", "WELL_NAME" FROM "WELL" WHERE ROWNUM <= 50000'
            elif dialect == "snowflake":
                sql = 'SELECT "UWI", "WELL_NAME" FROM "WELL" LIMIT 50000'
            else:
                sql = "SELECT TOP 50000 UWI, WELL_NAME FROM dbo.WELL"
            rows = con.execute(text(sql)).fetchall()
        return [{"UWI": str(r[0]), "WELL_NAME": str(r[1] or "")} for r in rows]
    except Exception as e:
        raise RuntimeError(f"Could not fetch UWIs from WELL table: {e}") from e


def fuzzy_match_uwi(las_uwi: str, ppdm_uwis: list[dict],
                    max_results: int = 5) -> list[dict]:
    """
    Fuzzy-match las_uwi against ppdm_uwis.

    Strategy (in order):
      1. Exact match (normalised)
      2. One string contains the other
      3. Character overlap ratio

    Returns up to max_results candidates, each with a 'score' 0-100.
    """
    norm_las = _normalise(las_uwi)
    results = []

    for row in ppdm_uwis:
        norm_ppdm = _normalise(row["UWI"])
        if not norm_ppdm:
            continue

        if norm_las == norm_ppdm:
            score = 100
        elif norm_las in norm_ppdm or norm_ppdm in norm_las:
            overlap = min(len(norm_las), len(norm_ppdm))
            total   = max(len(norm_las), len(norm_ppdm))
            score   = int(85 * overlap / total) + 10
        else:
            common = sum(c in norm_ppdm for c in set(norm_las))
            score  = int(70 * common / max(len(set(norm_las)), 1))

        if score > 20:
            results.append({**row, "score": score})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:max_results]


# ─────────────────────────────────────────────────────────────────────────────
# Batch directory scan
# ─────────────────────────────────────────────────────────────────────────────

def scan_las_directory(folder: str) -> pd.DataFrame:
    """
    Scan a folder for .las files and extract the UWI from each header.

    Returns a DataFrame with columns:
      FILE_NAME | FILE_PATH | LAS_UWI | PPDM_UWI | STATUS
    PPDM_UWI starts empty — filled in by the UI after fuzzy matching.
    """
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise FileNotFoundError(f"Directory not found: {folder}")

    rows = []
    for las_path in sorted(folder_path.glob("*.las")):
        try:
            las = read_las(str(las_path), ignore_header_errors=True)
            las_uwi = extract_las_uwi(las)
        except Exception as e:
            las_uwi = f"ERROR: {e}"

        rows.append({
            "FILE_NAME": las_path.name,
            "FILE_PATH": str(las_path),
            "LAS_UWI":   las_uwi,
            "PPDM_UWI":  "",
            "STATUS":    "Pending",
        })

    return pd.DataFrame(rows)


def auto_match_batch(scan_df: pd.DataFrame,
                     ppdm_uwis: list[dict]) -> pd.DataFrame:
    """
    Auto-populate PPDM_UWI for each row using fuzzy matching.
    Sets STATUS to 'Matched (n%)', 'No match', or 'Error'.
    """
    df = scan_df.copy()
    for i, row in df.iterrows():
        las_uwi = row["LAS_UWI"]
        if not las_uwi or las_uwi.startswith("ERROR"):
            df.at[i, "STATUS"] = "Error"
            continue
        matches = fuzzy_match_uwi(las_uwi, ppdm_uwis, max_results=1)
        if matches and matches[0]["score"] >= 80:
            df.at[i, "PPDM_UWI"] = matches[0]["UWI"]
            df.at[i, "STATUS"]   = f"Matched ({matches[0]['score']}%)"
        else:
            df.at[i, "PPDM_UWI"] = ""
            df.at[i, "STATUS"]   = "No match — manual required"
    return df


# ─────────────────────────────────────────────────────────────────────────────
# DataFrame builders
# ─────────────────────────────────────────────────────────────────────────────

def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _make_log_set_id(uwi: str, filename: str) -> str:
    seed = f"{uwi}:{filename}"
    return hashlib.sha1(seed.encode()).hexdigest()[:16].upper()


def _next_log_id(engine, uwi: str, log_set_id: str) -> str:
    """Find the next available LOG_ID for this UWI + LOG_SET_ID."""
    from sqlalchemy import text
    dialect = _detect_dialect(engine)
    try:
        with engine.connect() as con:
            if dialect == "oracle":
                sql = text(
                    'SELECT COUNT(*) FROM "WELL_LOG" '
                    'WHERE "UWI" = :uwi AND "LOG_SET_ID" = :lsid'
                )
            elif dialect == "snowflake":
                sql = text(
                    'SELECT COUNT(*) FROM "WELL_LOG" '
                    'WHERE "UWI" = :uwi AND "LOG_SET_ID" = :lsid'
                )
            else:
                sql = text(
                    "SELECT COUNT(*) FROM dbo.WELL_LOG "
                    "WHERE UWI = :uwi AND LOG_SET_ID = :lsid"
                )
            count = con.execute(sql, {"uwi": uwi, "lsid": log_set_id}).scalar() or 0
        return f"{int(count) + 1:04d}"
    except Exception:
        return "0001"


def build_well_log_df(las, uwi: str, log_set_id: str, log_id: str,
                      filename: str, las_path: str, source: str) -> pd.DataFrame:
    def _get(mnemonic, fallback=""):
        try:
            v = las.well[mnemonic].value
            return str(v).strip() if v is not None else fallback
        except KeyError:
            return fallback

    depth_curve = las.curves[0] if las.curves else None
    depth_unit  = depth_curve.unit if depth_curve else ""

    # lasio also exposes unit on the well header items directly
    # las.well["STRT"].unit gives "M" or "FT" — prefer that over curve unit
    try:
        strt_unit = las.well["STRT"].unit.strip().upper() or depth_unit
    except (KeyError, AttributeError):
        strt_unit = depth_unit

    now = _now_str()

    # WELL_LOG_ID is the PK — deterministic SHA1 of UWI + filename + log_id
    well_log_id = hashlib.sha1(
        f"{uwi}:{filename}:{log_id}".encode()
    ).hexdigest()[:20].upper()

    return pd.DataFrame([{
        "WELL_LOG_ID":      well_log_id,
        "UWI":              uwi,
        "LOG_SET_ID":       log_set_id,
        "LOG_ID":           log_id,
        "LOG_TITLE":        Path(las_path).name,
        "LOG_TYPE":         "WIRE",
        "TOP_DEPTH":        _safe_num(_get("STRT")),
        "TOP_DEPTH_OUOM":   strt_unit,
        "BASE_DEPTH":       _safe_num(_get("STOP")),
        "BASE_DEPTH_OUOM":  strt_unit,
        "REMARK":           str(las_path),
        "SOURCE":           source,
        "ROW_CHANGED_DATE": now,
        "ROW_CHANGED_BY":   "DATA_WRANGLER",
        "ROW_CREATED_DATE": now,
        "ROW_CREATED_BY":   "DATA_WRANGLER",
        "ACTIVE_IND":       "Y",
    }])


def build_curve_df(las, uwi: str, log_set_id: str, log_id: str,
                   source: str) -> pd.DataFrame:
    now = _now_str()
    depth_mnemonic = las.curves[0].mnemonic if las.curves else "DEPT"
    rows = []
    for curve in las.curves:
        rows.append({
            "UWI":               uwi,
            "LOG_SET_ID":        log_set_id,
            "LOG_ID":            log_id,
            "CURVE_ID":          curve.mnemonic.strip(),
            "CURVE_MNEMONIC":    curve.mnemonic.strip(),
            "CURVE_UNIT":        curve.unit or "",
            "CURVE_DESCRIPTION": curve.descr or "",
            "CURVE_TYPE":        "DEPT" if curve.mnemonic == depth_mnemonic else "REGULAR",
            "SOURCE":            source,
            "ROW_CHANGED_DATE":  now,
            "ROW_CHANGED_BY":    "DATA_WRANGLER",
            "ROW_CREATED_DATE":  now,
            "ROW_CREATED_BY":    "DATA_WRANGLER",
            "ACTIVE_IND":        "Y",
        })
    return pd.DataFrame(rows)


def build_curve_axis_df(las, uwi: str, source: str) -> pd.DataFrame:
    """
    Build WELL_LOG_CURVE_AXIS rows — one per curve.
    PK: UWI + CURVE_ID + AXIS_ID
    SPACING = LAS STEP value (sample interval)
    SPACING_UOM / AXIS_UOM = depth unit from STRT header
    """
    now = _now_str()
    depth_mnemonic = las.curves[0].mnemonic if las.curves else "DEPT"

    try:
        step  = _safe_num(las.well["STEP"].value)
        unit  = las.well["STRT"].unit.strip().upper()
    except (KeyError, AttributeError):
        step = None
        unit = ""

    rows = []
    for curve in las.curves:
        if curve.mnemonic == depth_mnemonic:
            continue
        rows.append({
            "UWI":              uwi,
            "CURVE_ID":         curve.mnemonic.strip(),
            "AXIS_ID":          "DEPTH",
            "SPACING":          step,
            "SPACING_UOM":      unit,
            "AXIS_UOM":         unit,
            "SOURCE":           source,
            "ROW_CHANGED_DATE": now,
            "ROW_CHANGED_BY":   "DATA_WRANGLER",
            "ROW_CREATED_DATE": now,
            "ROW_CREATED_BY":   "DATA_WRANGLER",
            "ACTIVE_IND":       "Y",
        })
    return pd.DataFrame(rows)


def build_curve_value_df(las, uwi: str, log_set_id: str, log_id: str,
                         source: str) -> pd.DataFrame:
    """
    Build WELL_LOG_CURVE_VALUE rows matching the actual PPDM 3.9 schema:
      PK:  UWI + CURVE_ID + SAMPLE_ID (nvarchar — we use depth as string)
      INDEX_VALUE    = depth (float)
      MEASURED_VALUE = curve sample value (float)
      SAMPLE_ID      = str(depth) — unique identifier within a curve
    """
    now = _now_str()
    depth_mnemonic = las.curves[0].mnemonic if las.curves else "DEPT"
    depths     = las[depth_mnemonic]

    # Read depth unit from well header (STRT.M or STRT.FT)
    try:
        depth_unit = las.well["STRT"].unit.strip().upper()
    except (KeyError, AttributeError):
        depth_unit = las.curves[0].unit if las.curves else ""

    rows = []
    for curve in las.curves:
        if curve.mnemonic == depth_mnemonic:
            continue
        try:
            samples = las[curve.mnemonic]
        except Exception:
            continue
        mnemonic = curve.mnemonic.strip()
        for depth, sample in zip(depths, samples):
            fval = float(sample)
            rows.append({
                "UWI":               uwi,
                "CURVE_ID":          mnemonic,
                "SAMPLE_ID":         f"{log_set_id}:{log_id}:{float(depth)}",  # unique per file + run + depth
                "INDEX_VALUE":       float(depth),
                "INDEX_VALUE_UOM":   depth_unit,
                "MEASURED_VALUE":    None if fval == -999.25 else fval,
                "MEASURED_VALUE_UOM": curve.unit or "",
                "SOURCE":            source,
                "ROW_CHANGED_DATE":  now,
                "ROW_CHANGED_BY":    "DATA_WRANGLER",
                "ROW_CREATED_DATE":  now,
                "ROW_CREATED_BY":    "DATA_WRANGLER",
                "ACTIVE_IND":        "Y",
            })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Insert — small tables (WELL_LOG, WELL_LOG_CURVE) — all dialects
# ─────────────────────────────────────────────────────────────────────────────

def insert_df(engine, table: str, df: pd.DataFrame,
              schema: str = "dbo") -> int:
    """
    Insert a small DataFrame directly via executemany.
    For WELL_LOG_CURVE and WELL_LOG_CURVE_AXIS uses INSERT WHERE NOT EXISTS
    to skip rows that already exist (same curve loaded under a different log run).
    """
    if df.empty:
        return 0

    dialect = _detect_dialect(engine)

    # Tables where PK conflicts are expected — use skip-duplicate logic
    _UPSERT_TABLES = {"WELL_LOG_CURVE", "WELL_LOG_CURVE_AXIS"}

    if dialect == "oracle":
        return _insert_oracle(engine, table, df)
    elif dialect == "snowflake":
        return _insert_snowflake(engine, table, df)
    elif table.upper() in _UPSERT_TABLES:
        return _insert_sqlserver_skip_dupes(engine, table, df, schema)
    else:
        return _insert_sqlserver_small(engine, table, df, schema)


def _insert_sqlserver_small(engine, table: str, df: pd.DataFrame,
                             schema: str) -> int:
    """SQL Server executemany — for small row counts only."""
    from sqlalchemy import text

    cols = _target_columns(engine, table, schema, dialect="sqlserver")
    df   = _align_df(df, cols)

    col_list = ", ".join(f"[{c}]" for c in df.columns)
    params   = ", ".join(f":{c}" for c in df.columns)
    sql      = f"INSERT INTO [{schema}].[{table}] ({col_list}) VALUES ({params})"

    rows = df.where(pd.notnull(df), None).to_dict(orient="records")
    with engine.begin() as con:
        con.execute(text(sql), rows)
    return len(rows)


def _insert_sqlserver_skip_dupes(engine, table: str, df: pd.DataFrame,
                                  schema: str) -> int:
    """
    SQL Server INSERT ... WHERE NOT EXISTS — skips rows whose PK already exists.
    Used for WELL_LOG_CURVE and WELL_LOG_CURVE_AXIS where multiple log runs
    share the same curve definitions for a well.
    """
    from sqlalchemy import text

    cols    = _target_columns(engine, table, schema, dialect="sqlserver")
    pk_cols = _pk_columns(engine, table, schema)
    df      = _align_df(df, cols)

    if df.empty or not pk_cols:
        return 0

    col_list    = ", ".join(f"[{c}]" for c in df.columns)
    val_list    = ", ".join(f":{c}" for c in df.columns)
    pk_check    = " AND ".join(
        f"[{schema}].[{table}].[{c}] = :{c}" for c in pk_cols
    )

    sql = (
        f"IF NOT EXISTS (SELECT 1 FROM [{schema}].[{table}] WHERE {pk_check})\n"
        f"    INSERT INTO [{schema}].[{table}] ({col_list}) VALUES ({val_list})"
    )

    rows = df.where(pd.notnull(df), None).to_dict(orient="records")
    inserted = 0
    with engine.begin() as con:
        for row in rows:
            result = con.execute(text(sql), row)
            inserted += result.rowcount
    return inserted


def _insert_oracle(engine, table: str, df: pd.DataFrame) -> int:
    import oracledb as _odb
    from sqlalchemy import text

    with engine.connect() as _sc:
        ora_schema = _sc.execute(text(
            "SELECT SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM dual"
        )).scalar() or ""

    cols = _target_columns(engine, table, ora_schema, dialect="oracle")
    df   = _align_df(df, cols)

    q_schema = f'"{ora_schema.upper()}"'
    q_table  = f'"{table.upper()}"'
    col_list = ", ".join(f'"{c.upper()}"' for c in df.columns)
    params   = ", ".join(f":{i+1}" for i in range(len(df.columns)))
    sql      = f"INSERT INTO {q_schema}.{q_table} ({col_list}) VALUES ({params})"

    rows = [
        tuple(str(v) if v is not None else None for v in r)
        for r in df.itertuples(index=False, name=None)
    ]

    BATCH = 1000
    with engine.begin() as con:
        cur = con.connection.cursor()
        cur.setinputsizes(*[_odb.DB_TYPE_VARCHAR] * len(df.columns))
        for i in range(0, len(rows), BATCH):
            cur.executemany(sql, rows[i:i + BATCH], batcherrors=True)
            errs = cur.getbatcherrors()
            if errs:
                raise Exception(
                    f"{len(errs)} Oracle batch error(s); first: {errs[0].message}"
                )
        cur.close()
    return len(rows)


def _insert_snowflake(engine, table: str, df: pd.DataFrame) -> int:
    from sqlalchemy import text

    with engine.connect() as _sc:
        sf_schema = _sc.execute(text("SELECT CURRENT_SCHEMA()")).scalar() or ""

    cols = _target_columns(engine, table, sf_schema, dialect="snowflake")
    df   = _align_df(df, cols)

    q_schema = f'"{sf_schema.upper()}"'
    q_table  = f'"{table.upper()}"'
    col_list = ", ".join(f'"{c.upper()}"' for c in df.columns)
    params   = ", ".join(f":{c}" for c in df.columns)
    sql      = f"INSERT INTO {q_schema}.{q_table} ({col_list}) VALUES ({params})"

    rows = df.where(pd.notnull(df), None).to_dict(orient="records")
    BATCH = 5000
    with engine.begin() as con:
        for i in range(0, len(rows), BATCH):
            con.execute(text(sql), rows[i:i + BATCH])
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# BULK INSERT path — SQL Server WELL_LOG_CURVE_VALUE only
# ─────────────────────────────────────────────────────────────────────────────

def _get_bulk_path(stem: str) -> str:
    """
    Return a writable CSV path for BULK INSERT.
    Prefers C:\\Bulk\\ (SQL Server service account accessible),
    falls back to system temp.
    """
    filename = f"las_cv_{stem}.csv"
    try:
        os.makedirs(_BULK_DIR, exist_ok=True)
        candidate = os.path.join(_BULK_DIR, filename)
        with open(candidate, "w") as _probe:
            pass
        os.unlink(candidate)
        return candidate
    except OSError:
        return os.path.join(tempfile.gettempdir(), filename)


def insert_curve_values_sqlserver(engine, df: pd.DataFrame,
                                  schema: str, stem: str) -> int:
    """
    Load WELL_LOG_CURVE_VALUE into SQL Server using the same
    CSV → BULK INSERT → INSERT SELECT pattern as staging.py.

    Steps:
      1. Write df to pipe-delimited CSV at C:\\Bulk\\ (or temp)
      2. CREATE staging table (all NVARCHAR)
      3. BULK INSERT CSV into staging
      4. INSERT INTO dbo.WELL_LOG_CURVE_VALUE SELECT ... FROM staging
         with TRY_CONVERT for numeric columns
      5. DROP staging table
      6. Delete CSV

    Returns row count inserted.
    """
    from sqlalchemy import text

    if df.empty:
        return 0

    # ── Step 1: write CSV ────────────────────────────────────────────
    csv_path  = _get_bulk_path(stem)
    stg_table = f"stg_las_cv_{stem[:40]}"

    # Convert all values to string for CSV; use empty string for None
    str_df = df.copy().astype(str).replace("None", "").replace("nan", "")
    str_df.to_csv(csv_path, sep="|", index=False, encoding="utf-8-sig")

    cols     = list(str_df.columns)
    col_defs = ",\n    ".join(f"[{c}] NVARCHAR(4000) NULL" for c in cols)
    csv_escaped = csv_path.replace("'", "''")

    sql_drop   = (
        f"IF OBJECT_ID('[{schema}].[{stg_table}]', 'U') IS NOT NULL "
        f"DROP TABLE [{schema}].[{stg_table}]"
    )
    sql_create = (
        f"CREATE TABLE [{schema}].[{stg_table}] (\n    {col_defs}\n)"
    )
    sql_bulk   = (
        f"BULK INSERT [{schema}].[{stg_table}]\n"
        f"FROM '{csv_escaped}'\n"
        f"WITH (FIRSTROW=2, FIELDTERMINATOR='|', ROWTERMINATOR='0x0D0A', "
        f"CODEPAGE='65001', TABLOCK)"
    )

    # ── Step 2-3: stage ──────────────────────────────────────────────
    try:
        with engine.begin() as con:
            con.execute(text(sql_drop))
            con.execute(text(sql_create))
            con.execute(text(sql_bulk))
    except Exception as e:
        _cleanup(csv_path)
        raise RuntimeError(f"BULK INSERT failed: {e}") from e

    # ── Step 4: get target columns and build INSERT SELECT ───────────
    tgt_cols = _target_columns(engine, "WELL_LOG_CURVE_VALUE", schema,
                               dialect="sqlserver")
    stg_upper = {c.upper() for c in cols}

    # Columns present in both staging and target
    insert_cols = [tc for tc in tgt_cols if tc.upper() in stg_upper]

    def _expr(col: str) -> str:
        """Wrap numeric/date columns in TRY_CONVERT."""
        u = col.upper()
        if u in ("INDEX_VALUE", "MEASURED_VALUE"):
            return f"TRY_CONVERT(FLOAT, NULLIF(LTRIM(RTRIM([{col}])), ''))"
        if u in ("ROW_CHANGED_DATE", "ROW_CREATED_DATE"):
            return f"TRY_CONVERT(DATETIME2, NULLIF(LTRIM(RTRIM([{col}])), ''))"
        return f"LTRIM(RTRIM([{col}]))"

    tgt_col_sql = ", ".join(f"[{c}]" for c in insert_cols)
    src_col_sql = ", ".join(_expr(c) for c in insert_cols)

    sql_insert = (
        f"INSERT INTO [dbo].[WELL_LOG_CURVE_VALUE] ({tgt_col_sql})\n"
        f"SELECT {src_col_sql}\n"
        f"FROM [{schema}].[{stg_table}]"
    )

    try:
        with engine.begin() as con:
            result = con.execute(text(sql_insert))
            n = result.rowcount
    except Exception as e:
        _cleanup(csv_path)
        raise RuntimeError(f"INSERT SELECT from staging failed: {e}") from e
    finally:
        # ── Step 5: drop staging ─────────────────────────────────────
        try:
            with engine.begin() as con:
                con.execute(text(sql_drop))
        except Exception:
            pass

    # ── Step 6: delete CSV ───────────────────────────────────────────
    _cleanup(csv_path)
    return n


def _cleanup(path: str) -> None:
    try:
        if os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Full single-file promote
# ─────────────────────────────────────────────────────────────────────────────

def promote_las_file(
    las_path: str,
    uwi: str,
    engine,
    source: str = "LAS_IMPORT",
    load_values: bool = False,
    schema: str = "dbo",
    original_path: str = "",        # original file path for catalog — overrides las_path in REMARK
) -> dict:
    """
    Parse a LAS file and insert WELL_LOG, WELL_LOG_CURVE,
    and optionally WELL_LOG_CURVE_VALUE directly into the database.

    SQL Server WELL_LOG_CURVE_VALUE uses BULK INSERT for speed.
    Oracle / Snowflake use executemany.

    Returns:
      { ok, log_rows, curve_rows, value_rows, log_set_id, log_id, error }
    """
    result = {
        "ok": False, "log_rows": 0, "curve_rows": 0, "axis_rows": 0,
        "value_rows": 0, "log_set_id": "", "log_id": "", "error": "",
    }

    try:
        las = read_las(str(las_path), ignore_header_errors=True)
    except Exception as e:
        result["error"] = f"LAS parse failed: {e}"
        return result

    filename     = Path(original_path or las_path).name
    catalog_path = original_path or las_path   # what gets stored in REMARK
    stem       = re.sub(r"[^A-Za-z0-9]", "_", Path(las_path).stem)[:40]
    log_set_id = _make_log_set_id(uwi, filename)
    log_id     = _next_log_id(engine, uwi, log_set_id)
    dialect    = _detect_dialect(engine)

    result["log_set_id"] = log_set_id
    result["log_id"]     = log_id

    try:
        log_df   = build_well_log_df(las, uwi, log_set_id, log_id, filename, catalog_path, source)
        curve_df = build_curve_df(las, uwi, log_set_id, log_id, source)
        axis_df  = build_curve_axis_df(las, uwi, source)

        result["log_rows"]   = insert_df(engine, "WELL_LOG",            log_df,   schema)
        result["curve_rows"] = insert_df(engine, "WELL_LOG_CURVE",      curve_df, schema)
        result["axis_rows"]  = insert_df(engine, "WELL_LOG_CURVE_AXIS", axis_df,  schema)

        if load_values:
            value_df = build_curve_value_df(
                las, uwi, log_set_id, log_id, source
            )

            if dialect == "sqlserver":
                # Server-side BULK INSERT → INSERT SELECT
                result["value_rows"] = insert_curve_values_sqlserver(
                    engine, value_df, schema, stem
                )
            else:
                # Oracle / Snowflake — executemany
                result["value_rows"] = insert_df(
                    engine, "WELL_LOG_CURVE_VALUE", value_df, schema
                )

        result["ok"] = True

    except Exception as e:
        result["error"] = str(e)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _detect_dialect(engine) -> str:
    try:
        name = engine.dialect.name.lower()
        if "oracle" in name:    return "oracle"
        if "snowflake" in name: return "snowflake"
    except Exception:
        pass
    return "sqlserver"


def _target_columns(engine, table: str, schema: str,
                    dialect: str) -> list[str]:
    """Return column names that exist in the target PPDM table."""
    from sqlalchemy import text
    try:
        with engine.connect() as con:
            if dialect == "oracle":
                rows = con.execute(text(
                    "SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS "
                    "WHERE TABLE_NAME = :t AND OWNER = :s"
                ), {"t": table.upper(), "s": schema.upper()}).fetchall()
            elif dialect == "snowflake":
                rows = con.execute(text(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_NAME = :t AND TABLE_SCHEMA = :s"
                ), {"t": table.upper(), "s": schema.upper()}).fetchall()
            else:
                rows = con.execute(text(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_NAME = :t AND TABLE_SCHEMA = :s"
                ), {"t": table, "s": schema}).fetchall()
                if not rows:
                    rows = con.execute(text(
                        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                        "WHERE TABLE_NAME = :t AND TABLE_SCHEMA = 'dbo'"
                    ), {"t": table}).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def _pk_columns(engine, table: str, schema: str) -> list[str]:
    """Return PK column names for a SQL Server table."""
    from sqlalchemy import text
    try:
        with engine.connect() as con:
            rows = con.execute(text("""
                SELECT kcu.COLUMN_NAME
                FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                  ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                WHERE kcu.TABLE_NAME   = :t
                  AND kcu.TABLE_SCHEMA = :s
                  AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                ORDER BY kcu.ORDINAL_POSITION
            """), {"t": table, "s": schema}).fetchall()
            if not rows:
                rows = con.execute(text("""
                    SELECT kcu.COLUMN_NAME
                    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                    JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                      ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                    WHERE kcu.TABLE_NAME   = :t
                      AND kcu.TABLE_SCHEMA = 'dbo'
                      AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                    ORDER BY kcu.ORDINAL_POSITION
                """), {"t": table}).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []
    """
    Keep only DataFrame columns present in the target table (case-insensitive).
    Falls back to the full DataFrame if introspection returned nothing.
    """
    if not target_cols:
        return df
    upper_map = {c.upper(): c for c in df.columns}
    keep = [upper_map[tc.upper()] for tc in target_cols if tc.upper() in upper_map]
    return df[keep] if keep else df


def _align_df(df: pd.DataFrame, target_cols: list[str]) -> pd.DataFrame:
    """
    Keep only DataFrame columns present in the target table (case-insensitive).
    Falls back to the full DataFrame if introspection returned nothing.
    """
    if not target_cols:
        return df
    upper_map = {c.upper(): c for c in df.columns}
    keep = [upper_map[tc.upper()] for tc in target_cols if tc.upper() in upper_map]
    return df[keep] if keep else df


def _safe_num(val) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
