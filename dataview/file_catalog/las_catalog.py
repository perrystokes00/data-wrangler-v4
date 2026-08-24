"""
modules/las_catalog.py

LAS File Catalog — reads LAS headers and populates the las_catalog schema.

Responsibilities:
  - Create / verify the catalog schema exists
  - Register physical storage repositories
  - Scan directories and catalog LAS files (header only — no curve data)
  - Match catalogued files to PPDM WELL.UWI
  - Query the catalog

The catalog never stores curve sample data — only header metadata.
The LAS file on disk is the authoritative source of curve data.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    import lasio
    from dataview.file_catalog.las_reader import read_las
    import logging as _logging
    # Suppress lasio warnings (wrapped files, missing curve data, etc.)
    _logging.getLogger("lasio").setLevel(_logging.ERROR)
except ImportError as e:
    raise ImportError("pip install lasio") from e


# ─────────────────────────────────────────────────────────────────────────────
# Schema setup
# ─────────────────────────────────────────────────────────────────────────────

CATALOG_SCHEMA = "las_catalog"

_DDL = {
    "WL_REPOSITORY": """
        CREATE TABLE [las_catalog].[WL_REPOSITORY] (
            [REPOSITORY_ID]    NVARCHAR(40)   NOT NULL,
            [REPOSITORY_NAME]  NVARCHAR(200)  NOT NULL,
            [REPOSITORY_TYPE]  NVARCHAR(40)   NOT NULL,
            [BASE_PATH]        NVARCHAR(500)  NOT NULL,
            [ACTIVE_IND]       NVARCHAR(1)    NOT NULL DEFAULT 'Y',
            [REMARK]           NVARCHAR(2000) NULL,
            [SOURCE]           NVARCHAR(40)   NOT NULL,
            [ROW_CREATED_BY]   NVARCHAR(30)   NULL,
            [ROW_CREATED_DATE] DATETIME2      NULL,
            [ROW_CHANGED_BY]   NVARCHAR(30)   NULL,
            [ROW_CHANGED_DATE] DATETIME2      NULL,
            CONSTRAINT [WLREP_PK] PRIMARY KEY ([REPOSITORY_ID])
        )
    """,
    "LAS_FILE": """
        CREATE TABLE [las_catalog].[LAS_FILE] (
            [LAS_FILE_ID]      NVARCHAR(40)   NOT NULL,
            [REPOSITORY_ID]    NVARCHAR(40)   NOT NULL,
            [UWI]              NVARCHAR(40)   NOT NULL,  -- FK → dbo.WELL.UWI
            [WELL_NAME]        NVARCHAR(255)  NULL,
            [FILE_NAME]        NVARCHAR(500)  NOT NULL,
            [FILE_SIZE_KB]     NUMERIC(15,2)  NULL,
            [LAS_VERSION]      NVARCHAR(10)   NULL,
            [OPERATOR]         NVARCHAR(255)  NULL,
            [FIELD]            NVARCHAR(255)  NULL,
            [COUNTRY]          NVARCHAR(255)  NULL,
            [STATE_PROVINCE]   NVARCHAR(255)  NULL,
            [COUNTY]           NVARCHAR(255)  NULL,
            [TOP_DEPTH]        NUMERIC(15,5)  NULL,
            [BASE_DEPTH]       NUMERIC(15,5)  NULL,
            [DEPTH_STEP]       NUMERIC(15,5)  NULL,
            [DEPTH_UOM]        NVARCHAR(10)   NULL,
            [LOG_DATE]         NVARCHAR(50)   NULL,
            [SERVICE_COMPANY]  NVARCHAR(255)  NULL,
            [CURVE_COUNT]      NUMERIC(10,0)  NULL,
            [SAMPLE_COUNT]     NUMERIC(15,0)  NULL,
            [FILE_HASH]        NVARCHAR(64)   NULL,
            [CATALOG_DATE]     DATETIME2      NULL,
            [LAST_SEEN_DATE]   DATETIME2      NULL,
            [ACTIVE_IND]       NVARCHAR(1)    NOT NULL DEFAULT 'Y',
            [REMARK]           NVARCHAR(2000) NULL,
            [SOURCE]           NVARCHAR(40)   NOT NULL,
            [ROW_CREATED_BY]   NVARCHAR(30)   NULL,
            [ROW_CREATED_DATE] DATETIME2      NULL,
            [ROW_CHANGED_BY]   NVARCHAR(30)   NULL,
            [ROW_CHANGED_DATE] DATETIME2      NULL,
            CONSTRAINT [LASFILE_PK] PRIMARY KEY ([LAS_FILE_ID]),
            CONSTRAINT [LASFILE_REP_FK] FOREIGN KEY ([REPOSITORY_ID])
                REFERENCES [las_catalog].[WL_REPOSITORY] ([REPOSITORY_ID]),
            CONSTRAINT [LASFILE_WELL_FK] FOREIGN KEY ([UWI])
                REFERENCES [dbo].[WELL] ([UWI])
        )
    """,
    "LAS_FILE_CURVE": """
        CREATE TABLE [las_catalog].[LAS_FILE_CURVE] (
            [LAS_FILE_ID]       NVARCHAR(40)  NOT NULL,
            [CURVE_ID]          NVARCHAR(40)  NOT NULL,
            [CURVE_UNIT]        NVARCHAR(40)  NULL,
            [CURVE_DESCRIPTION] NVARCHAR(255) NULL,
            [CURVE_TYPE]        NVARCHAR(40)  NULL,
            [API_CODE]          NVARCHAR(40)  NULL,
            [SOURCE]            NVARCHAR(40)  NOT NULL,
            [ROW_CREATED_BY]    NVARCHAR(30)  NULL,
            [ROW_CREATED_DATE]  DATETIME2     NULL,
            [ROW_CHANGED_BY]    NVARCHAR(30)  NULL,
            [ROW_CHANGED_DATE]  DATETIME2     NULL,
            CONSTRAINT [LASCURVE_PK] PRIMARY KEY ([LAS_FILE_ID], [CURVE_ID]),
            CONSTRAINT [LASCURVE_FILE_FK] FOREIGN KEY ([LAS_FILE_ID])
                REFERENCES [las_catalog].[LAS_FILE] ([LAS_FILE_ID])
        )
    """,
    "LAS_FILE_PARAMETER": """
        CREATE TABLE [las_catalog].[LAS_FILE_PARAMETER] (
            [LAS_FILE_ID]       NVARCHAR(40)  NOT NULL,
            [PARAMETER_NAME]    NVARCHAR(40)  NOT NULL,
            [PARAMETER_VALUE]   NVARCHAR(500) NULL,
            [PARAMETER_UNIT]    NVARCHAR(40)  NULL,
            [SECTION]           NVARCHAR(10)  NULL,
            [SOURCE]            NVARCHAR(40)  NOT NULL,
            [ROW_CREATED_BY]    NVARCHAR(30)  NULL,
            [ROW_CREATED_DATE]  DATETIME2     NULL,
            [ROW_CHANGED_BY]    NVARCHAR(30)  NULL,
            [ROW_CHANGED_DATE]  DATETIME2     NULL,
            CONSTRAINT [LASPARM_PK] PRIMARY KEY ([LAS_FILE_ID], [PARAMETER_NAME]),
            CONSTRAINT [LASPARM_FILE_FK] FOREIGN KEY ([LAS_FILE_ID])
                REFERENCES [las_catalog].[LAS_FILE] ([LAS_FILE_ID])
        )
    """,
    "WL_FILE_UWI_MAP": """
        CREATE TABLE [las_catalog].[WL_FILE_UWI_MAP] (
            [MAP_ID]           NVARCHAR(40)   NOT NULL,
            [FILE_PATH]        NVARCHAR(500)  NOT NULL,
            [FILE_NAME]        NVARCHAR(255)  NOT NULL,
            [FILE_FORMAT]      NVARCHAR(10)   NOT NULL,
            [REPOSITORY_ID]    NVARCHAR(40)   NULL,
            [UWI]              NVARCHAR(40)   NULL,
            [HEADER_WELL_ID]   NVARCHAR(255)  NULL,
            [MATCH_METHOD]     NVARCHAR(20)   NULL,
            [MATCH_SCORE]      NUMERIC(5,1)   NULL,
            [MATCH_WELL_NAME]  NVARCHAR(255)  NULL,
            [STATUS]           NVARCHAR(20)   NOT NULL DEFAULT 'PENDING',
            [FILE_SIZE_KB]     NUMERIC(15,2)  NULL,
            [REMARK]           NVARCHAR(2000) NULL,
            [ROW_CREATED_BY]   NVARCHAR(30)   NULL,
            [ROW_CREATED_DATE] DATETIME2      NULL,
            [ROW_CHANGED_BY]   NVARCHAR(30)   NULL,
            [ROW_CHANGED_DATE] DATETIME2      NULL,
            CONSTRAINT [WLMAP_PK]     PRIMARY KEY ([MAP_ID]),
            CONSTRAINT [WLMAP_REP_FK] FOREIGN KEY ([REPOSITORY_ID])
                REFERENCES [las_catalog].[WL_REPOSITORY] ([REPOSITORY_ID])
        )
    """,
}


def ensure_catalog_schema(engine) -> list[str]:
    """
    Create the las_catalog schema and tables if they don't already exist.
    Returns a list of tables that were created.
    """
    from sqlalchemy import text
    created = []

    with engine.begin() as con:
        # Create schema
        con.execute(text(
            "IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'las_catalog') "
            "EXEC('CREATE SCHEMA [las_catalog]')"
        ))

        # Create tables in FK dependency order
        for table, ddl in _DDL.items():
            exists = con.execute(text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = 'las_catalog' AND TABLE_NAME = :t"
            ), {"t": table}).scalar()
            if not exists:
                con.execute(text(ddl))
                created.append(table)

        # Indexes (ignore errors if already exist)
        _indexes = [
            "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'LASFILE_UWI_IDX') "
            "CREATE INDEX [LASFILE_UWI_IDX] ON [las_catalog].[LAS_FILE] ([UWI]) WHERE [UWI] IS NOT NULL",

            "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'LASFILE_DEPTH_IDX') "
            "CREATE INDEX [LASFILE_DEPTH_IDX] ON [las_catalog].[LAS_FILE] ([TOP_DEPTH],[BASE_DEPTH]) "
            "WHERE [TOP_DEPTH] IS NOT NULL AND [BASE_DEPTH] IS NOT NULL",

            "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'LASFILE_FIELD_IDX') "
            "CREATE INDEX [LASFILE_FIELD_IDX] ON [las_catalog].[LAS_FILE] ([FIELD]) WHERE [FIELD] IS NOT NULL",

            "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'LASFILE_HASH_IDX') "
            "CREATE INDEX [LASFILE_HASH_IDX] ON [las_catalog].[LAS_FILE] ([FILE_HASH]) WHERE [FILE_HASH] IS NOT NULL",

            "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'LASCURVE_ID_IDX') "
            "CREATE INDEX [LASCURVE_ID_IDX] ON [las_catalog].[LAS_FILE_CURVE] ([CURVE_ID])",

            "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'WLMAP_STATUS_IDX') "
            "CREATE INDEX [WLMAP_STATUS_IDX] ON [las_catalog].[WL_FILE_UWI_MAP] ([STATUS], [FILE_FORMAT])",
        ]
        for idx_sql in _indexes:
            try:
                con.execute(text(idx_sql))
            except Exception:
                pass

    return created


# ─────────────────────────────────────────────────────────────────────────────
# Repository management
# ─────────────────────────────────────────────────────────────────────────────

def list_repositories(engine) -> pd.DataFrame:
    """Return all repositories from the catalog."""
    from sqlalchemy import text
    with engine.connect() as con:
        rows = con.execute(text(
            "SELECT REPOSITORY_ID, REPOSITORY_NAME, REPOSITORY_TYPE, "
            "BASE_PATH, ACTIVE_IND, REMARK "
            "FROM [las_catalog].[WL_REPOSITORY] "
            "ORDER BY REPOSITORY_NAME"
        )).fetchall()
    return pd.DataFrame(rows, columns=[
        "REPOSITORY_ID", "REPOSITORY_NAME", "REPOSITORY_TYPE",
        "BASE_PATH", "ACTIVE_IND", "REMARK"
    ])


def add_repository(engine, name: str, repo_type: str,
                   base_path: str, remark: str = "",
                   source: str = "DATA_WRANGLER") -> str:
    """
    Register a new repository. Returns the REPOSITORY_ID.
    If a repository with the same BASE_PATH already exists, returns its ID.
    """
    from sqlalchemy import text

    # Check if already exists
    with engine.connect() as con:
        existing = con.execute(text(
            "SELECT REPOSITORY_ID FROM [las_catalog].[WL_REPOSITORY] "
            "WHERE BASE_PATH = :p"
        ), {"p": base_path}).scalar()
    if existing:
        return existing

    repo_id = _make_id(base_path)
    now = _now_str()

    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO [las_catalog].[WL_REPOSITORY]
                (REPOSITORY_ID, REPOSITORY_NAME, REPOSITORY_TYPE, BASE_PATH,
                 ACTIVE_IND, REMARK, SOURCE,
                 ROW_CREATED_BY, ROW_CREATED_DATE, ROW_CHANGED_BY, ROW_CHANGED_DATE)
            VALUES
                (:id, :name, :rtype, :path,
                 'Y', :remark, :source,
                 'DATA_WRANGLER', :now, 'DATA_WRANGLER', :now)
        """), {
            "id": repo_id, "name": name, "rtype": repo_type,
            "path": base_path, "remark": remark,
            "source": source, "now": now,
        })
    return repo_id


# ─────────────────────────────────────────────────────────────────────────────
# File parsing
# ─────────────────────────────────────────────────────────────────────────────

def well_exists(engine, uwi: str) -> bool:
    """Check if a UWI already exists in PPDM WELL table."""
    from sqlalchemy import text
    with engine.connect() as con:
        count = con.execute(text(
            "SELECT COUNT(*) FROM [dbo].[WELL] WHERE UWI = :uwi"
        ), {"uwi": uwi}).scalar() or 0
    return count > 0


def create_well_from_las(engine, header: dict,
                         uwi: str, source: str = "LAS_FILE") -> dict:
    """
    Insert a minimal WELL row from a parsed LAS header.

    Only populates columns that have reliable LAS header equivalents.
    All FK-constrained reference columns (CURRENT_STATUS, OPERATOR etc.)
    are left NULL to avoid constraint violations — users can enrich later
    via the main pipeline.

    Returns { ok, uwi, error }
    """
    from sqlalchemy import text

    result = {"ok": False, "uwi": uwi, "error": ""}
    now = _now_str()

    depth_uom = (header.get("depth_uom") or "").upper() or None

    # Build location note for REMARK since WELL has no country/state columns
    location_parts = [
        p for p in [
            header.get("county"),
            header.get("state_province"),
            header.get("country"),
        ] if p
    ]
    remark = ", ".join(location_parts) if location_parts else None

    row = {
        "UWI":              uwi,
        "WELL_NAME":        header.get("well_name") or None,
        "TOP_DEPTH":        header.get("top_depth"),
        "TOP_DEPTH_OUOM":   depth_uom,
        "BASE_DEPTH":       header.get("base_depth"),
        "BASE_DEPTH_OUOM":  depth_uom,
        "REMARK":           remark,
        "ACTIVE_IND":       "Y",
        "SOURCE":           source,
        "ROW_CREATED_BY":   "DATA_WRANGLER",
        "ROW_CREATED_DATE": now,
        "ROW_CHANGED_BY":   "DATA_WRANGLER",
        "ROW_CHANGED_DATE": now,
    }

    # Remove None values — let DB defaults apply
    row = {k: v for k, v in row.items() if v is not None}

    cols = ", ".join(f"[{k}]" for k in row)
    vals = ", ".join(f":{k}" for k in row)

    try:
        with engine.begin() as con:
            con.execute(
                text(f"INSERT INTO [dbo].[WELL] ({cols}) VALUES ({vals})"),
                row
            )
        result["ok"] = True
    except Exception as e:
        result["error"] = str(e)

    return result



def parse_las_header(las_path: str) -> dict:
    """
    Read a LAS file header only (fast — does not load curve data arrays).
    Returns a dict of all extracted metadata.
    """
    las = read_las(str(las_path), ignore_header_errors=True)

    def _get(section, mnemonic, fallback=""):
        try:
            v = section[mnemonic].value
            return str(v).strip() if v is not None else fallback
        except KeyError:
            return fallback

    def _get_unit(section, mnemonic, fallback=""):
        try:
            u = section[mnemonic].unit
            return str(u).strip().upper() if u else fallback
        except KeyError:
            return fallback

    w = las.well
    depth_unit = _get_unit(w, "STRT") or (
        las.curves[0].unit.strip().upper() if las.curves else ""
    )

    # Curve count excludes depth index
    depth_mnemonic = las.curves[0].mnemonic if las.curves else "DEPT"
    curve_count = len([c for c in las.curves if c.mnemonic != depth_mnemonic])

    # Sample count — length of depth array
    try:
        sample_count = len(las[depth_mnemonic])
    except Exception:
        sample_count = 0

    # Curves list
    curves = []
    for curve in las.curves:
        curves.append({
            "mnemonic":    curve.mnemonic.strip(),
            "unit":        curve.unit or "",
            "description": curve.descr or "",
            "type":        "DEPT" if curve.mnemonic == depth_mnemonic else "REGULAR",
            "api_code":    "",
        })

    # Parameters (~P section)
    params = []
    for item in las.params:
        params.append({
            "name":    item.mnemonic,
            "value":   str(item.value) if item.value is not None else "",
            "unit":    item.unit or "",
            "section": "P",
        })

    # Also capture well header items as parameters for completeness
    for item in las.well:
        params.append({
            "name":    item.mnemonic,
            "value":   str(item.value) if item.value is not None else "",
            "unit":    item.unit or "",
            "section": "W",
        })

    return {
        "well_name":      _get(w, "WELL"),
        "uwi":            _get(w, "UWI") or _get(w, "API"),
        "operator":       _get(w, "COMP"),
        "field":          _get(w, "FLD"),
        "country":        _get(w, "CTRY"),
        "state_province": _get(w, "PROV") or _get(w, "STAT"),
        "county":         _get(w, "CNTY"),
        "top_depth":      _safe_float(_get(w, "STRT")),
        "base_depth":     _safe_float(_get(w, "STOP")),
        "depth_step":     _safe_float(_get(w, "STEP")),
        "depth_uom":      depth_unit,
        "log_date":       _get(w, "DATE"),
        "service_company": _get(w, "SRVC"),
        "las_version":    str(las.version[0].value) if las.version else "",
        "curve_count":    curve_count,
        "sample_count":   sample_count,
        "curves":         curves,
        "parameters":     params,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Catalog operations
# ─────────────────────────────────────────────────────────────────────────────

def catalog_file(engine, las_path: str, repository_id: str,
                 uwi: str = "", source: str = "DATA_WRANGLER",
                 _preloaded_header: dict = None) -> dict:
    """
    Parse a LAS file header and insert/update its catalog entries.
    If _preloaded_header is provided (from parallel parse phase), skip re-parsing.

    Returns:
        { ok, las_file_id, action, curves_catalogued, error }
        action is 'inserted', 'updated', or 'skipped'
    """
    result = {
        "ok": False, "las_file_id": "", "action": "",
        "curves_catalogued": 0, "error": "",
    }

    try:
        path = Path(las_path)
        if not path.exists():
            result["error"] = f"File not found: {las_path}"
            return result

        # Use pre-parsed header if provided, otherwise parse now
        header = _preloaded_header if _preloaded_header is not None else parse_las_header(str(path))
        effective_uwi = uwi or header["uwi"] or ""

        if not effective_uwi:
            result["error"] = (
                "No UWI found in LAS header and none provided. "
                "A matched WELL is required before cataloguing."
            )
            return result
        with engine.connect() as con:
            from sqlalchemy import text
            base_path = con.execute(text(
                "SELECT BASE_PATH FROM [las_catalog].[WL_REPOSITORY] "
                "WHERE REPOSITORY_ID = :id"
            ), {"id": repository_id}).scalar() or ""

        try:
            rel_path = str(path.relative_to(base_path))
        except ValueError:
            rel_path = str(path.absolute())  # store full path if not under base_path

        # File metadata
        file_size_kb = round(path.stat().st_size / 1024, 2)
        file_hash    = _sha256_file(str(path))
        las_file_id  = _make_id(str(path))
        now          = _now_str()

        file_row = {
            "LAS_FILE_ID":    las_file_id,
            "REPOSITORY_ID":  repository_id,
            "UWI":            effective_uwi,
            "WELL_NAME":      header["well_name"] or None,
            "FILE_NAME":      rel_path,
            "FILE_SIZE_KB":   file_size_kb,
            "LAS_VERSION":    header["las_version"] or None,
            "OPERATOR":       header["operator"] or None,
            "FIELD":          header["field"] or None,
            "COUNTRY":        header["country"] or None,
            "STATE_PROVINCE": header["state_province"] or None,
            "COUNTY":         header["county"] or None,
            "TOP_DEPTH":      header["top_depth"],
            "BASE_DEPTH":     header["base_depth"],
            "DEPTH_STEP":     header["depth_step"],
            "DEPTH_UOM":      header["depth_uom"] or None,
            "LOG_DATE":       header["log_date"] or None,
            "SERVICE_COMPANY": header["service_company"] or None,
            "CURVE_COUNT":    header["curve_count"],
            "SAMPLE_COUNT":   header["sample_count"],
            "FILE_HASH":      file_hash,
            "CATALOG_DATE":   now,
            "LAST_SEEN_DATE": now,
            "ACTIVE_IND":     "Y",
            "SOURCE":         source,
            "ROW_CREATED_BY": "DATA_WRANGLER",
            "ROW_CREATED_DATE": now,
            "ROW_CHANGED_BY": "DATA_WRANGLER",
            "ROW_CHANGED_DATE": now,
        }

        from sqlalchemy import text

        # Check if already catalogued
        with engine.connect() as con:
            existing = con.execute(text(
                "SELECT LAS_FILE_ID FROM [las_catalog].[LAS_FILE] "
                "WHERE LAS_FILE_ID = :id"
            ), {"id": las_file_id}).scalar()

        if existing:
            # Update LAST_SEEN_DATE and UWI if now matched
            with engine.begin() as con:
                con.execute(text("""
                    UPDATE [las_catalog].[LAS_FILE]
                    SET LAST_SEEN_DATE = :now,
                        UWI = COALESCE(:uwi, UWI),
                        ROW_CHANGED_DATE = :now,
                        ROW_CHANGED_BY = 'DATA_WRANGLER'
                    WHERE LAS_FILE_ID = :id
                """), {"now": now, "uwi": effective_uwi, "id": las_file_id})
            result["action"] = "updated"
        else:
            # Insert new file row
            cols = ", ".join(f"[{k}]" for k in file_row)
            vals = ", ".join(f":{k}" for k in file_row)
            with engine.begin() as con:
                con.execute(text(
                    f"INSERT INTO [las_catalog].[LAS_FILE] ({cols}) VALUES ({vals})"
                ), file_row)

            # Insert curves
            curve_rows = []
            for c in header["curves"]:
                curve_rows.append({
                    "LAS_FILE_ID":       las_file_id,
                    "CURVE_ID":          c["mnemonic"],
                    "CURVE_UNIT":        c["unit"] or None,
                    "CURVE_DESCRIPTION": c["description"] or None,
                    "CURVE_TYPE":        c["type"],
                    "API_CODE":          c["api_code"] or None,
                    "SOURCE":            source,
                    "ROW_CREATED_BY":    "DATA_WRANGLER",
                    "ROW_CREATED_DATE":  now,
                    "ROW_CHANGED_BY":    "DATA_WRANGLER",
                    "ROW_CHANGED_DATE":  now,
                })

            if curve_rows:
                c_cols = ", ".join(f"[{k}]" for k in curve_rows[0])
                c_vals = ", ".join(f":{k}" for k in curve_rows[0])
                with engine.begin() as con:
                    con.execute(
                        text(f"INSERT INTO [las_catalog].[LAS_FILE_CURVE] "
                             f"({c_cols}) VALUES ({c_vals})"),
                        curve_rows
                    )

            # Insert parameters
            param_rows = []
            seen_params = set()
            for p in header["parameters"]:
                key = (las_file_id, p["name"])
                if key in seen_params:
                    continue
                seen_params.add(key)
                param_rows.append({
                    "LAS_FILE_ID":    las_file_id,
                    "PARAMETER_NAME": p["name"],
                    "PARAMETER_VALUE": p["value"][:500] if p["value"] else None,
                    "PARAMETER_UNIT": p["unit"] or None,
                    "SECTION":        p["section"],
                    "SOURCE":         source,
                    "ROW_CREATED_BY": "DATA_WRANGLER",
                    "ROW_CREATED_DATE": now,
                    "ROW_CHANGED_BY": "DATA_WRANGLER",
                    "ROW_CHANGED_DATE": now,
                })

            if param_rows:
                p_cols = ", ".join(f"[{k}]" for k in param_rows[0])
                p_vals = ", ".join(f":{k}" for k in param_rows[0])
                with engine.begin() as con:
                    con.execute(
                        text(f"INSERT INTO [las_catalog].[LAS_FILE_PARAMETER] "
                             f"({p_cols}) VALUES ({p_vals})"),
                        param_rows
                    )

            result["action"] = "inserted"
            result["curves_catalogued"] = len(curve_rows)

        result["ok"] = True
        result["las_file_id"] = las_file_id

    except Exception as e:
        result["error"] = str(e)

    return result


def catalog_directory(engine, folder: str, repository_id: str,
                      source: str = "DATA_WRANGLER",
                      max_workers: int = None,
                      progress_callback=None) -> list[dict]:
    """
    Catalog all LAS files in a directory.
    Headers are parsed in parallel (I/O-bound), DB inserts run sequentially.

    max_workers: parallel threads for parsing (default = cpu_count - 2, max 12)
    progress_callback: optional callable(current, total, filename)
    Returns list of per-file result dicts.
    """
    import concurrent.futures, os

    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise FileNotFoundError(f"Directory not found: {folder}")

    las_files = sorted(folder_path.rglob("*.las"))
    total = len(las_files)
    if total == 0:
        return []

    if max_workers is None:
        cores = os.cpu_count() or 4
        max_workers = min(max(cores - 2, 2), 12)

    # ── Phase 1: Parse headers in parallel ───────────────────────────────────
    parse_results = [None] * total

    def _parse_worker(las_path: Path) -> dict:
        try:
            return {"ok": True, "header": parse_las_header(str(las_path))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_idx = {
            ex.submit(_parse_worker, fp): i
            for i, fp in enumerate(las_files)
        }
        completed = 0
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            completed += 1
            if progress_callback:
                progress_callback(completed, total * 2,
                                  f"Parsing {las_files[idx].name}…")
            try:
                parse_results[idx] = future.result()
            except Exception as e:
                parse_results[idx] = {"ok": False, "error": str(e)}

    # ── Phase 2: DB inserts sequentially ─────────────────────────────────────
    results = []
    for i, (las_path, parsed) in enumerate(zip(las_files, parse_results)):
        if progress_callback:
            progress_callback(total + i + 1, total * 2,
                              f"Cataloguing {las_path.name}…")
        if not parsed or not parsed["ok"]:
            results.append({
                "file_name": las_path.name, "ok": False,
                "error": parsed["error"] if parsed else "Parse failed",
                "action": "", "curves_catalogued": 0,
            })
            continue
        r = catalog_file(engine, str(las_path), repository_id,
                         source=source,
                         _preloaded_header=parsed["header"])
        r["file_name"] = las_path.name
        results.append(r)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Catalog queries
# ─────────────────────────────────────────────────────────────────────────────

def search_catalog(engine,
                   uwi: str = "",
                   well_name: str = "",
                   curve_id: str = "",
                   field: str = "",
                   operator: str = "",
                   country: str = "",
                   state_province: str = "",
                   county: str = "",
                   depth_min: Optional[float] = None,
                   depth_max: Optional[float] = None,
                   depth_uom: str = "",
                   length_min: Optional[float] = None,
                   length_max: Optional[float] = None,
                   sort_cols: Optional[list] = None,
                   ) -> pd.DataFrame:
    """
    Search the catalog by any combination of attributes.

    UWI supports SQL wildcards: % = any characters, _ = single character.
    LENGTH is computed server-side as BASE_DEPTH - TOP_DEPTH.

    sort_cols: list of (column_name, direction) tuples, e.g.
               [("UWI", "ASC"), ("TOP_DEPTH", "DESC")]
               Valid columns: UWI, WELL_NAME, FIELD, OPERATOR, COUNTRY,
               STATE_PROVINCE, COUNTY, TOP_DEPTH, BASE_DEPTH, LENGTH,
               CURVE_COUNT, LOG_DATE, FILE_SIZE_KB, REPOSITORY_NAME
    """
    from sqlalchemy import text

    _SORT_MAP = {
        "UWI":             "f.UWI",
        "WELL_NAME":       "f.WELL_NAME",
        "FIELD":           "f.FIELD",
        "OPERATOR":        "f.OPERATOR",
        "COUNTRY":         "f.COUNTRY",
        "STATE_PROVINCE":  "f.STATE_PROVINCE",
        "COUNTY":          "f.COUNTY",
        "TOP_DEPTH":       "f.TOP_DEPTH",
        "BASE_DEPTH":      "f.BASE_DEPTH",
        "LENGTH":          "(f.BASE_DEPTH - f.TOP_DEPTH)",
        "CURVE_COUNT":     "f.CURVE_COUNT",
        "LOG_DATE":        "f.LOG_DATE",
        "FILE_SIZE_KB":    "f.FILE_SIZE_KB",
        "REPOSITORY_NAME": "r.REPOSITORY_NAME",
    }

    where = ["f.ACTIVE_IND = 'Y'"]
    params = {}

    if uwi:
        uwi_pattern = uwi if ("%" in uwi or "_" in uwi) else f"%{uwi}%"
        where.append("f.UWI LIKE :uwi")
        params["uwi"] = uwi_pattern
    if well_name:
        where.append("f.WELL_NAME LIKE :wn")
        params["wn"] = f"%{well_name}%"
    if field:
        where.append("f.FIELD LIKE :field")
        params["field"] = f"%{field}%"
    if operator:
        where.append("f.OPERATOR LIKE :op")
        params["op"] = f"%{operator}%"
    if country:
        where.append("f.COUNTRY LIKE :country")
        params["country"] = f"%{country}%"
    if state_province:
        where.append("f.STATE_PROVINCE LIKE :state")
        params["state"] = f"%{state_province}%"
    if county:
        where.append("f.COUNTY LIKE :county")
        params["county"] = f"%{county}%"
    if depth_min is not None:
        where.append("f.BASE_DEPTH >= :dmin")
        params["dmin"] = depth_min
    if depth_max is not None:
        where.append("f.TOP_DEPTH <= :dmax")
        params["dmax"] = depth_max
    if depth_uom:
        where.append("f.DEPTH_UOM = :duom")
        params["duom"] = depth_uom.upper()
    if length_min is not None:
        where.append("(f.BASE_DEPTH - f.TOP_DEPTH) >= :lmin")
        params["lmin"] = length_min
    if length_max is not None:
        where.append("(f.BASE_DEPTH - f.TOP_DEPTH) <= :lmax")
        params["lmax"] = length_max

    join = ""
    if curve_id:
        join = "JOIN [las_catalog].[LAS_FILE_CURVE] c ON c.LAS_FILE_ID = f.LAS_FILE_ID"
        where.append("c.CURVE_ID = :curve")
        params["curve"] = curve_id.upper()

    where_sql = " AND ".join(where)

    # Build ORDER BY from whitelist
    order_parts = []
    if sort_cols:
        for col, direction in sort_cols:
            col_upper = col.upper()
            dir_upper = "DESC" if str(direction).upper() == "DESC" else "ASC"
            if col_upper in _SORT_MAP:
                order_parts.append(f"{_SORT_MAP[col_upper]} {dir_upper}")
    order_sql = ", ".join(order_parts) if order_parts else "f.UWI ASC, f.TOP_DEPTH ASC"

    sql = f"""
        SELECT DISTINCT
            f.LAS_FILE_ID,
            f.UWI,
            f.WELL_NAME,
            f.FIELD,
            f.OPERATOR,
            f.COUNTRY,
            f.STATE_PROVINCE,
            f.COUNTY,
            f.TOP_DEPTH,
            f.BASE_DEPTH,
            CASE
                WHEN f.BASE_DEPTH IS NOT NULL AND f.TOP_DEPTH IS NOT NULL
                THEN f.BASE_DEPTH - f.TOP_DEPTH
                ELSE NULL
            END AS LENGTH,
            f.DEPTH_UOM,
            f.CURVE_COUNT,
            f.SAMPLE_COUNT,
            f.LOG_DATE,
            f.SERVICE_COMPANY,
            f.FILE_SIZE_KB,
            CASE WHEN RIGHT(r.BASE_PATH,1) = '\\' THEN r.BASE_PATH ELSE r.BASE_PATH + '\\' END + f.FILE_NAME AS FULL_PATH,
            f.FILE_NAME,
            r.REPOSITORY_NAME,
            CONVERT(NVARCHAR(30), f.CATALOG_DATE, 120) AS CATALOG_DATE,
            CONVERT(NVARCHAR(30), f.LAST_SEEN_DATE, 120) AS LAST_SEEN_DATE
        FROM [las_catalog].[LAS_FILE] f
        JOIN [las_catalog].[WL_REPOSITORY] r
          ON r.REPOSITORY_ID = f.REPOSITORY_ID
        {join}
        WHERE {where_sql}
        ORDER BY {order_sql}
    """

    with engine.connect() as con:
        rows = con.execute(text(sql), params).fetchall()

    cols = [
        "LAS_FILE_ID", "UWI", "WELL_NAME", "FIELD", "OPERATOR",
        "COUNTRY", "STATE_PROVINCE", "COUNTY",
        "TOP_DEPTH", "BASE_DEPTH", "LENGTH", "DEPTH_UOM",
        "CURVE_COUNT", "SAMPLE_COUNT", "LOG_DATE", "SERVICE_COMPANY",
        "FILE_SIZE_KB", "FULL_PATH", "FILE_NAME", "REPOSITORY_NAME",
        "CATALOG_DATE", "LAST_SEEN_DATE",
    ]
    return pd.DataFrame(rows, columns=cols)


def _fetch_ppdm_well_header(engine, uwi: str) -> dict:
    """
    Fetch PPDM WELL values that correspond to LAS ~W header mnemonics.
    Only returns non-empty values. Returns dict of { las_mnemonic: ppdm_value }

    Mnemonics updated (where they exist in the LAS ~W section):
      WELL  <- WELL_NAME
      COMP  <- OPERATOR
      UWI   <- UWI  (or API if that mnemonic is used instead)
      LATI  <- SURFACE_LATITUDE
      LONG  <- SURFACE_LONGITUDE
    """
    from sqlalchemy import text
    try:
        with engine.connect() as con:
            row = con.execute(text(
                "SELECT WELL_NAME, OPERATOR, "
                "SURFACE_LATITUDE, SURFACE_LONGITUDE "
                "FROM [dbo].[WELL] WHERE UWI = :uwi"
            ), {"uwi": uwi}).fetchone()
    except Exception:
        return {}
    if not row:
        return {}

    def _fmtcoord(v):
        try:
            return str(round(float(v), 6))
        except (TypeError, ValueError):
            return None

    candidates = {
        "WELL": row[0],          # WELL_NAME
        "COMP": row[1],          # OPERATOR
        "UWI":  uwi,             # always from PPDM
        "API":  uwi,             # same — used if file has API instead of UWI
        "LATI": _fmtcoord(row[2]),
        "LONG": _fmtcoord(row[3]),
    }
    return {
        k: str(v).strip()
        for k, v in candidates.items()
        if v and str(v).strip() not in ("", "None", "nan")
    }


def _apply_ppdm_header(src_path: str, dst_path: str,
                        ppdm_updates: dict) -> list[str]:
    """
    Read a LAS file and update ONLY the values of existing ~W (Well info)
    section mnemonics from PPDM. Rules:
      - Only existing ~W items are touched — no additions
      - Only .value is changed — mnemonic, unit, description unchanged
      - UWI and API are treated as equivalent identifiers:
        if the file has UWI, update UWI and skip API;
        if the file has API but not UWI, update API instead
      - ~V, ~C, ~P, ~A sections are written back exactly as read
    Writes LAS 2.0. Returns list of change descriptions for audit log.
    """
    las = read_las(src_path, ignore_header_errors=True)
    changed = []

    # Determine which identifier mnemonic this file uses
    file_has_uwi = True
    try:
        las.well["UWI"]
    except KeyError:
        file_has_uwi = False

    for mnemonic, ppdm_value in ppdm_updates.items():
        # Skip API if file uses UWI, skip UWI if file uses API
        if mnemonic == "API" and file_has_uwi:
            continue
        if mnemonic == "UWI" and not file_has_uwi:
            continue

        try:
            item = las.well[mnemonic]
            original = str(item.value).strip()
            if original != ppdm_value:
                item.value = ppdm_value
                changed.append(f"{mnemonic}: '{original}' → '{ppdm_value}'")
        except KeyError:
            pass   # mnemonic absent in this file — skip

    las.write(dst_path, version=2.0)
    return changed


def export_files(results_df: pd.DataFrame,
                 destination: str,
                 overwrite: bool = False,
                 update_headers: bool = False,
                 engine=None) -> dict:
    """
    Copy LAS files to a destination folder.

    If update_headers=True, updates the value of existing ~W (Well info)
    mnemonics from PPDM before writing. Specifically:
      - Only existing ~W mnemonics are touched — no additions
      - Only .value is updated — mnemonic name, unit, description unchanged
      - Only where the PPDM value is non-empty
      - ~V (Version), ~C (Curves), ~P (Parameters), ~A (Data) untouched
      - Output written as LAS 2.0
    Original files are never modified.

    Parameters
    ----------
    results_df     : DataFrame from search_catalog (FULL_PATH + UWI required)
    destination    : destination folder — created if needed
    overwrite      : if False, skip files already in destination
    update_headers : update ~W values from PPDM WELL table
    engine         : SQLAlchemy engine (required if update_headers=True)
    """
    import shutil

    dest_path = Path(destination)
    dest_path.mkdir(parents=True, exist_ok=True)

    result = {"copied": 0, "skipped": 0, "missing": 0, "errors": 0, "details": []}
    _header_cache: dict[str, dict] = {}

    for _, row in results_df.iterrows():
        src = Path(str(row["FULL_PATH"]))
        dst = dest_path / src.name
        detail = {
            "file":    src.name,
            "source":  str(src),
            "status":  "",
            "error":   "",
            "updated": "",
        }

        if not src.exists():
            detail["status"] = "missing"
            result["missing"] += 1

        elif dst.exists() and not overwrite:
            detail["status"] = "skipped"
            result["skipped"] += 1

        else:
            try:
                # Header update only applies to LAS files
                row_fmt = str(row.get("FORMAT", "LAS")).upper()
                if update_headers and engine is not None and row_fmt == "LAS":
                    uwi = str(row.get("UWI", ""))
                    if uwi not in _header_cache:
                        _header_cache[uwi] = _fetch_ppdm_well_header(engine, uwi)
                    ppdm_updates = _header_cache[uwi]
                    changed = _apply_ppdm_header(str(src), str(dst), ppdm_updates)
                    detail["updated"] = ", ".join(changed) if changed else "no changes"
                else:
                    shutil.copy2(str(src), str(dst))
                    if update_headers and row_fmt != "LAS":
                        detail["updated"] = "n/a (not LAS)"

                detail["status"] = "copied"
                result["copied"] += 1

            except Exception as e:
                detail["status"] = "error"
                detail["error"]  = str(e)
                result["errors"] += 1

        result["details"].append(detail)

    return result


def get_file_curves(engine, las_file_id: str) -> pd.DataFrame:
    """Return all curves for a specific catalogued file, with log length."""
    from sqlalchemy import text
    with engine.connect() as con:
        # Get the file length once
        length_row = con.execute(text(
            "SELECT "
            "  TOP_DEPTH, BASE_DEPTH, "
            "  CASE WHEN BASE_DEPTH IS NOT NULL AND TOP_DEPTH IS NOT NULL "
            "       THEN BASE_DEPTH - TOP_DEPTH ELSE NULL END AS LENGTH, "
            "  DEPTH_UOM "
            "FROM [las_catalog].[LAS_FILE] WHERE LAS_FILE_ID = :id"
        ), {"id": las_file_id}).fetchone()

        rows = con.execute(text(
            "SELECT CURVE_ID, CURVE_UNIT, CURVE_DESCRIPTION, CURVE_TYPE "
            "FROM [las_catalog].[LAS_FILE_CURVE] "
            "WHERE LAS_FILE_ID = :id ORDER BY CURVE_ID"
        ), {"id": las_file_id}).fetchall()

    df = pd.DataFrame(rows, columns=[
        "CURVE_ID", "CURVE_UNIT", "CURVE_DESCRIPTION", "CURVE_TYPE"
    ])

    # Annotate each curve row with file-level depth info
    if length_row and not df.empty:
        df["TOP_DEPTH"]  = length_row[0]
        df["BASE_DEPTH"] = length_row[1]
        df["LENGTH"]     = length_row[2]
        df["DEPTH_UOM"]  = length_row[3]

    return df


def get_catalog_summary(engine) -> dict:
    """Return high-level catalog statistics including location and length."""
    from sqlalchemy import text
    with engine.connect() as con:
        stats = con.execute(text("""
            SELECT
                COUNT(DISTINCT f.LAS_FILE_ID)  AS total_files,
                COUNT(DISTINCT f.UWI)           AS matched_wells,
                COUNT(DISTINCT r.REPOSITORY_ID) AS repositories,
                SUM(f.FILE_SIZE_KB) / 1024.0    AS total_size_mb,
                MIN(f.TOP_DEPTH)                AS shallowest,
                MAX(f.BASE_DEPTH)               AS deepest,
                AVG(CASE WHEN f.BASE_DEPTH IS NOT NULL AND f.TOP_DEPTH IS NOT NULL
                         THEN f.BASE_DEPTH - f.TOP_DEPTH ELSE NULL END) AS avg_length,
                MIN(CASE WHEN f.BASE_DEPTH IS NOT NULL AND f.TOP_DEPTH IS NOT NULL
                         THEN f.BASE_DEPTH - f.TOP_DEPTH ELSE NULL END) AS min_length,
                MAX(CASE WHEN f.BASE_DEPTH IS NOT NULL AND f.TOP_DEPTH IS NOT NULL
                         THEN f.BASE_DEPTH - f.TOP_DEPTH ELSE NULL END) AS max_length,
                COUNT(DISTINCT f.COUNTRY)       AS countries,
                COUNT(DISTINCT f.STATE_PROVINCE) AS states,
                COUNT(DISTINCT f.COUNTY)        AS counties
            FROM [las_catalog].[LAS_FILE] f
            JOIN [las_catalog].[WL_REPOSITORY] r
              ON r.REPOSITORY_ID = f.REPOSITORY_ID
            WHERE f.ACTIVE_IND = 'Y'
        """)).fetchone()

        unmatched = con.execute(text(
            "SELECT COUNT(*) FROM [las_catalog].[LAS_FILE] "
            "WHERE UWI IS NULL AND ACTIVE_IND = 'Y'"
        )).scalar() or 0

        top_curves = con.execute(text("""
            SELECT TOP 10 CURVE_ID, COUNT(*) AS file_count
            FROM [las_catalog].[LAS_FILE_CURVE]
            WHERE CURVE_TYPE = 'REGULAR'
            GROUP BY CURVE_ID
            ORDER BY COUNT(*) DESC
        """)).fetchall()

        top_countries = con.execute(text("""
            SELECT TOP 5 COUNTRY, COUNT(*) AS cnt
            FROM [las_catalog].[LAS_FILE]
            WHERE COUNTRY IS NOT NULL AND ACTIVE_IND = 'Y'
            GROUP BY COUNTRY ORDER BY COUNT(*) DESC
        """)).fetchall()

        top_states = con.execute(text("""
            SELECT TOP 5 STATE_PROVINCE, COUNT(*) AS cnt
            FROM [las_catalog].[LAS_FILE]
            WHERE STATE_PROVINCE IS NOT NULL AND ACTIVE_IND = 'Y'
            GROUP BY STATE_PROVINCE ORDER BY COUNT(*) DESC
        """)).fetchall()

    def _f(v):
        return round(float(v), 1) if v is not None else None

    return {
        "total_files":    stats[0] or 0,
        "matched_wells":  stats[1] or 0,
        "unmatched_files": unmatched,
        "repositories":   stats[2] or 0,
        "total_size_mb":  round(float(stats[3] or 0), 1),
        "shallowest":     _f(stats[4]),
        "deepest":        _f(stats[5]),
        "avg_length":     _f(stats[6]),
        "min_length":     _f(stats[7]),
        "max_length":     _f(stats[8]),
        "countries":      stats[9] or 0,
        "states":         stats[10] or 0,
        "counties":       stats[11] or 0,
        "top_curves":     [{"curve": r[0], "count": r[1]} for r in top_curves],
        "top_countries":  [{"country": r[0], "count": r[1]} for r in top_countries],
        "top_states":     [{"state": r[0], "count": r[1]} for r in top_states],
    }

def get_distinct_values(engine, column: str) -> list[str]:
    """
    Return sorted distinct non-null values for a column in LAS_FILE.
    Whitelisted to prevent SQL injection.
    """
    from sqlalchemy import text
    allowed = {
        "WELL_NAME", "FIELD", "OPERATOR", "COUNTRY",
        "STATE_PROVINCE", "COUNTY", "SERVICE_COMPANY",
        "DEPTH_UOM", "LAS_VERSION", "REPOSITORY_NAME",
    }
    if column.upper() not in allowed:
        return []
    if column.upper() == "REPOSITORY_NAME":
        sql = ("SELECT DISTINCT r.REPOSITORY_NAME "
               "FROM [las_catalog].[LAS_FILE] f "
               "JOIN [las_catalog].[WL_REPOSITORY] r "
               "  ON r.REPOSITORY_ID = f.REPOSITORY_ID "
               "WHERE r.REPOSITORY_NAME IS NOT NULL "
               "ORDER BY r.REPOSITORY_NAME")
        with engine.connect() as con:
            rows = con.execute(text(sql)).fetchall()
    else:
        sql = (f"SELECT DISTINCT [{column.upper()}] "
               f"FROM [las_catalog].[LAS_FILE] "
               f"WHERE [{column.upper()}] IS NOT NULL AND ACTIVE_IND = 'Y' "
               f"ORDER BY [{column.upper()}]")
        with engine.connect() as con:
            rows = con.execute(text(sql)).fetchall()
    return [str(r[0]) for r in rows]


def update_uwi_match(engine, las_file_id: str, uwi: str,
                     source: str = "DATA_WRANGLER") -> None:
    """Set or update the UWI for a catalogued file."""
    from sqlalchemy import text
    with engine.begin() as con:
        con.execute(text("""
            UPDATE [las_catalog].[LAS_FILE]
            SET UWI = :uwi,
                ROW_CHANGED_DATE = :now,
                ROW_CHANGED_BY = 'DATA_WRANGLER'
            WHERE LAS_FILE_ID = :id
        """), {"uwi": uwi, "now": _now_str(), "id": las_file_id})


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _make_id(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20].upper()


def _sha256_file(path: str) -> str:
    """Compute SHA256 of file contents for duplicate detection."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _safe_float(val) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
