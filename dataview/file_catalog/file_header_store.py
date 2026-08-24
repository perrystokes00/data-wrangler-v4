"""
modules/file_header_store.py
============================
Store parsed file headers in the catalog DB at catalog time.
Provides export to CSV/Excel for manager review and PPDM update staging.

Tables:
  file_catalog.FILE_WELL_HEADER  — LAS/DLIS/LIS header mnemonics
  file_catalog.FILE_SEIS_HEADER  — SEG-Y/P190 header fields
"""
from __future__ import annotations
import uuid, datetime
from sqlalchemy import text
from dataview.core.catalog_dialect import (
    detect_dialect, now_expr, varchar, timestamp_type,
    timestamp_default, schema_table, if_not_exists_table,
    select_top
)


# ─────────────────────────────────────────────────────────────────────────────
# Schema creation
# ─────────────────────────────────────────────────────────────────────────────

_WELL_HDR_DDL = """
    IF NOT EXISTS (
        SELECT 1 FROM sys.tables t
        JOIN sys.schemas s ON t.schema_id=s.schema_id
        WHERE s.name='file_catalog' AND t.name='FILE_WELL_HEADER'
    )
    CREATE TABLE file_catalog.FILE_WELL_HEADER (
        FILE_HEADER_ID   NVARCHAR(40)   NOT NULL PRIMARY KEY,
        INVENTORY_ID     NVARCHAR(40)   NULL,
        CATALOG_FILE_ID  NVARCHAR(40)   NULL,
        FILE_NAME        NVARCHAR(500)  NOT NULL,
        FILE_FORMAT      NVARCHAR(10)   NOT NULL,
        UWI              NVARCHAR(40)   NULL,
        MNEMONIC         NVARCHAR(50)   NOT NULL,
        VALUE            NVARCHAR(500)  NULL,
        UNIT             NVARCHAR(50)   NULL,
        DESCRIPTION      NVARCHAR(500)  NULL,
        ROW_CREATED_DATE DATETIME2      DEFAULT GETUTCDATE()
    )
"""

_SEIS_HDR_DDL = """
    IF NOT EXISTS (
        SELECT 1 FROM sys.tables t
        JOIN sys.schemas s ON t.schema_id=s.schema_id
        WHERE s.name='file_catalog' AND t.name='FILE_SEIS_HEADER'
    )
    CREATE TABLE file_catalog.FILE_SEIS_HEADER (
        SEIS_HEADER_ID   NVARCHAR(40)   NOT NULL PRIMARY KEY,
        INVENTORY_ID     NVARCHAR(40)   NULL,
        CATALOG_FILE_ID  NVARCHAR(40)   NULL,
        FILE_NAME        NVARCHAR(500)  NOT NULL,
        FILE_FORMAT      NVARCHAR(10)   NOT NULL,
        SURVEY_NAME      NVARCHAR(255)  NULL,
        FIELD_NAME       NVARCHAR(200)  NOT NULL,
        VALUE            NVARCHAR(1000) NULL,
        SOURCE           NVARCHAR(20)   NULL,
        ROW_CREATED_DATE DATETIME2      DEFAULT GETUTCDATE()
    )
"""

_WELL_STAGE_DDL = """
    IF NOT EXISTS (
        SELECT 1 FROM sys.tables t
        JOIN sys.schemas s ON t.schema_id=s.schema_id
        WHERE s.name='file_catalog' AND t.name='WELL_HEADER_STAGING'
    )
    CREATE TABLE file_catalog.WELL_HEADER_STAGING (
        STAGE_ID         NVARCHAR(40)   NOT NULL PRIMARY KEY,
        BATCH_ID         NVARCHAR(40)   NOT NULL,
        FILE_NAME        NVARCHAR(500)  NULL,
        UWI              NVARCHAR(40)   NULL,
        WELL_NAME        NVARCHAR(255)  NULL,
        COMPANY          NVARCHAR(255)  NULL,
        FIELD            NVARCHAR(255)  NULL,
        COUNTY           NVARCHAR(255)  NULL,
        STATE            NVARCHAR(255)  NULL,
        COUNTRY          NVARCHAR(255)  NULL,
        LATITUDE         NVARCHAR(50)   NULL,
        LONGITUDE        NVARCHAR(50)   NULL,
        KB_ELEV          NVARCHAR(50)   NULL,
        GL_ELEV          NVARCHAR(50)   NULL,
        SPUD_DATE        NVARCHAR(50)   NULL,
        COMP_DATE        NVARCHAR(50)   NULL,
        STATUS           NVARCHAR(20)   DEFAULT 'PENDING',
        REVIEW_NOTES     NVARCHAR(500)  NULL,
        UPLOADED_DATE    DATETIME2      DEFAULT GETUTCDATE(),
        APPLIED_DATE     DATETIME2      NULL
    )
"""

_SEIS_STAGE_DDL = """
    IF NOT EXISTS (
        SELECT 1 FROM sys.tables t
        JOIN sys.schemas s ON t.schema_id=s.schema_id
        WHERE s.name='file_catalog' AND t.name='SEIS_HEADER_STAGING'
    )
    CREATE TABLE file_catalog.SEIS_HEADER_STAGING (
        STAGE_ID         NVARCHAR(40)   NOT NULL PRIMARY KEY,
        BATCH_ID         NVARCHAR(40)   NOT NULL,
        FILE_NAME        NVARCHAR(500)  NULL,
        SURVEY_NAME      NVARCHAR(255)  NULL,
        LINE_NAME        NVARCHAR(255)  NULL,
        SAMPLE_INTERVAL  NVARCHAR(50)   NULL,
        SAMPLES_PER_TRACE NVARCHAR(50)  NULL,
        DATA_FORMAT_CODE NVARCHAR(50)   NULL,
        ACQ_DATE         NVARCHAR(50)   NULL,
        OPERATOR         NVARCHAR(255)  NULL,
        CLIENT           NVARCHAR(255)  NULL,
        COUNTRY          NVARCHAR(255)  NULL,
        STATUS           NVARCHAR(20)   DEFAULT 'PENDING',
        REVIEW_NOTES     NVARCHAR(500)  NULL,
        UPLOADED_DATE    DATETIME2      DEFAULT GETUTCDATE(),
        APPLIED_DATE     DATETIME2      NULL
    )
"""


def ensure_header_tables(engine) -> list[str]:
    """Create header storage tables if they don't exist. Returns list of created tables."""
    from dataview.core.setup_database import _adapt_ddl
    d = detect_dialect(engine)
    created = []
    for ddl, name in [
        (_WELL_HDR_DDL,   "FILE_WELL_HEADER"),
        (_SEIS_HDR_DDL,   "FILE_SEIS_HEADER"),
        (_WELL_STAGE_DDL, "WELL_HEADER_STAGING"),
        (_SEIS_STAGE_DDL, "SEIS_HEADER_STAGING"),
    ]:
        try:
            adapted = _adapt_ddl(ddl, d)
            with engine.begin() as con:
                con.execute(text(adapted))
            created.append(name)
        except Exception:
            pass
    return created


def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _uid() -> str:
    return uuid.uuid4().hex[:40].upper()


# ─────────────────────────────────────────────────────────────────────────────
# Store headers at catalog time
# ─────────────────────────────────────────────────────────────────────────────

def store_las_headers(engine, file_path: str, inventory_id: str = None,
                       catalog_file_id: str = None, uwi: str = None):
    """Parse LAS ~W section and store every mnemonic in FILE_WELL_HEADER."""
    try:
        import lasio
        from dataview.file_catalog.las_reader import read_las
        from pathlib import Path
        las = read_las(file_path, ignore_header_errors=True)
        rows = []
        for item in las.well:
            rows.append({
                "FILE_HEADER_ID":  _uid(),
                "INVENTORY_ID":    inventory_id,
                "CATALOG_FILE_ID": catalog_file_id,
                "FILE_NAME":       Path(file_path).name,
                "FILE_FORMAT":     "LAS",
                "UWI":             uwi or str(las.well.get("UWI", {}).value or ""),
                "MNEMONIC":        str(item.mnemonic),
                "VALUE":           str(item.value)[:500] if item.value is not None else None,
                "UNIT":            str(item.unit)[:50]   if item.unit  else None,
                "DESCRIPTION":     str(item.descr)[:500] if item.descr else None,
            })
        _bulk_insert_well_headers(engine, rows)
        return len(rows)
    except Exception:
        return 0


def store_dlis_headers(engine, file_path: str, inventory_id: str = None,
                        catalog_file_id: str = None, uwi: str = None):
    """Parse DLIS origins and parameters, store in FILE_WELL_HEADER."""
    try:
        import warnings
        from dlisio import dlis
        from pathlib import Path
        rows = []
        fname = Path(file_path).name
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with dlis.load(file_path) as lfs:
                for lf in lfs:
                    for o in lf.origins:
                        for attr in ["well_name","field_name","company","country",
                                     "province","state","creation_time",
                                     "producer_name","run_nr","well_id"]:
                            v = getattr(o, attr, None)
                            if v is not None:
                                rows.append({
                                    "FILE_HEADER_ID":  _uid(),
                                    "INVENTORY_ID":    inventory_id,
                                    "CATALOG_FILE_ID": catalog_file_id,
                                    "FILE_NAME":       fname,
                                    "FILE_FORMAT":     "DLIS",
                                    "UWI":             uwi,
                                    "MNEMONIC":        attr.upper(),
                                    "VALUE":           str(v)[:500],
                                    "UNIT":            None,
                                    "DESCRIPTION":     None,
                                })
                    for p in lf.parameters:
                        rows.append({
                            "FILE_HEADER_ID":  _uid(),
                            "INVENTORY_ID":    inventory_id,
                            "CATALOG_FILE_ID": catalog_file_id,
                            "FILE_NAME":       fname,
                            "FILE_FORMAT":     "DLIS",
                            "UWI":             uwi,
                            "MNEMONIC":        str(p.name)[:50],
                            "VALUE":           str(p.values)[:500] if p.values else None,
                            "UNIT":            None,
                            "DESCRIPTION":     None,
                        })
        _bulk_insert_well_headers(engine, rows)
        return len(rows)
    except Exception:
        return 0


def store_lis_headers(engine, file_path: str, inventory_id: str = None,
                       catalog_file_id: str = None, uwi: str = None):
    """Parse LIS wellsite data, store in FILE_WELL_HEADER."""
    try:
        import warnings
        from dlisio import lis
        from pathlib import Path
        rows = []
        fname = Path(file_path).name
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with lis.load(file_path) as lfs:
                if not lfs:
                    return 0
                lf = lfs[0]
                for rec in lf.wellsite_data():
                    for c in rec.components():
                        mnem = str(getattr(c, "mnemonic", "") or "")
                        val  = getattr(c, "component", None)
                        unit = str(getattr(c, "units", "") or "")
                        if mnem:
                            rows.append({
                                "FILE_HEADER_ID":  _uid(),
                                "INVENTORY_ID":    inventory_id,
                                "CATALOG_FILE_ID": catalog_file_id,
                                "FILE_NAME":       fname,
                                "FILE_FORMAT":     "LIS",
                                "UWI":             uwi,
                                "MNEMONIC":        mnem[:50],
                                "VALUE":           str(val)[:500] if val is not None else None,
                                "UNIT":            unit[:50] or None,
                                "DESCRIPTION":     None,
                            })
        _bulk_insert_well_headers(engine, rows)
        return len(rows)
    except Exception:
        return 0


def store_segy_headers(engine, file_path: str, inventory_id: str = None,
                        catalog_file_id: str = None, survey_name: str = None):
    """Parse SEG-Y EBCDIC and binary headers, store in FILE_SEIS_HEADER."""
    try:
        import struct
        from pathlib import Path
        rows = []
        fname = Path(file_path).name

        with open(file_path, "rb") as f:
            raw = f.read(3600)

        # EBCDIC — parse C1-C40 lines
        text_raw = raw[:3200]
        try:
            ebcdic = text_raw.decode("cp037", errors="replace")
            # Auto-detect ASCII vs EBCDIC
            ascii_ = text_raw.decode("ascii", errors="replace")
            if sum(1 for c in ascii_ if c.isprintable()) >                sum(1 for c in ebcdic if c.isprintable()):
                ebcdic = ascii_
        except Exception:
            ebcdic = text_raw.decode("ascii", errors="replace")

        for i, line in enumerate([ebcdic[j:j+80].strip() for j in range(0,3200,80)], 1):
            if line.strip():
                rows.append({
                    "SEIS_HEADER_ID":  _uid(),
                    "INVENTORY_ID":    inventory_id,
                    "CATALOG_FILE_ID": catalog_file_id,
                    "FILE_NAME":       fname,
                    "FILE_FORMAT":     "SEGY",
                    "SURVEY_NAME":     survey_name,
                    "FIELD_NAME":      f"C{i:02d}",
                    "VALUE":           line[:1000],
                    "SOURCE":          "EBCDIC",
                })

        # Binary header
        if len(raw) >= 3600:
            fields = [
                (0,  ">i", "Job ID"),
                (4,  ">i", "Line number"),
                (8,  ">i", "Reel number"),
                (12, ">h", "Traces per ensemble"),
                (14, ">h", "Aux traces per ensemble"),
                (16, ">h", "Sample interval (us)"),
                (18, ">h", "Sample interval original (us)"),
                (20, ">h", "Samples per trace"),
                (22, ">h", "Samples per trace original"),
                (24, ">h", "Data sample format code"),
                (26, ">h", "Ensemble fold"),
                (28, ">h", "Trace sorting code"),
                (60, ">h", "SEG-Y format revision"),
                (62, ">h", "Fixed length trace flag"),
            ]
            for off, fmt, name in fields:
                try:
                    sz  = struct.calcsize(fmt)
                    val = struct.unpack(fmt, raw[3200+off:3200+off+sz])[0]
                    rows.append({
                        "SEIS_HEADER_ID":  _uid(),
                        "INVENTORY_ID":    inventory_id,
                        "CATALOG_FILE_ID": catalog_file_id,
                        "FILE_NAME":       fname,
                        "FILE_FORMAT":     "SEGY",
                        "SURVEY_NAME":     survey_name,
                        "FIELD_NAME":      name,
                        "VALUE":           str(val),
                        "SOURCE":          "BINARY",
                    })
                except Exception:
                    pass

        _bulk_insert_seis_headers(engine, rows)
        return len(rows)
    except Exception:
        return 0


def store_p190_headers(engine, file_path: str, inventory_id: str = None,
                        catalog_file_id: str = None, survey_name: str = None):
    """Parse P190 H-records, store in FILE_SEIS_HEADER."""
    try:
        from pathlib import Path
        rows = []
        fname = Path(file_path).name
        with open(file_path, "r", errors="replace") as f:
            for line in f:
                if not line.startswith("H"):
                    continue
                field = line[1:2].strip()
                value = line[2:].strip()[:1000]
                rows.append({
                    "SEIS_HEADER_ID":  _uid(),
                    "INVENTORY_ID":    inventory_id,
                    "CATALOG_FILE_ID": catalog_file_id,
                    "FILE_NAME":       fname,
                    "FILE_FORMAT":     "P190",
                    "SURVEY_NAME":     survey_name,
                    "FIELD_NAME":      f"H{field}" if field else "H",
                    "VALUE":           value,
                    "SOURCE":          "P190",
                })
        _bulk_insert_seis_headers(engine, rows)
        return len(rows)
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Bulk insert helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bulk_insert_well_headers(engine, rows: list[dict]):
    if not rows:
        return
    # Delete existing for same file first (re-catalog overwrites)
    file_names = list({r["FILE_NAME"] for r in rows})
    with engine.begin() as con:
        for fn in file_names:
            con.execute(text(
                f"DELETE FROM {schema_table(detect_dialect(engine), 'file_catalog', 'FILE_WELL_HEADER')} WHERE FILE_NAME=:fn"
            ), {"fn": fn})
        for r in rows:
            con.execute(text("""
                INSERT INTO file_catalog.FILE_WELL_HEADER
                (FILE_HEADER_ID,INVENTORY_ID,CATALOG_FILE_ID,FILE_NAME,
                 FILE_FORMAT,UWI,MNEMONIC,VALUE,UNIT,DESCRIPTION)
                VALUES (:fhid,:iid,:cfid,:fn,:fmt,:uwi,:mn,:val,:unit,:desc)
            """), {
                "fhid": r["FILE_HEADER_ID"],
                "iid":  r.get("INVENTORY_ID"),
                "cfid": r.get("CATALOG_FILE_ID"),
                "fn":   r["FILE_NAME"],
                "fmt":  r["FILE_FORMAT"],
                "uwi":  r.get("UWI"),
                "mn":   r["MNEMONIC"],
                "val":  r.get("VALUE"),
                "unit": r.get("UNIT"),
                "desc": r.get("DESCRIPTION"),
            })


def _bulk_insert_seis_headers(engine, rows: list[dict]):
    if not rows:
        return
    file_names = list({r["FILE_NAME"] for r in rows})
    with engine.begin() as con:
        for fn in file_names:
            con.execute(text(
                f"DELETE FROM {schema_table(detect_dialect(engine), 'file_catalog', 'FILE_SEIS_HEADER')} WHERE FILE_NAME=:fn"
            ), {"fn": fn})
        for r in rows:
            con.execute(text("""
                INSERT INTO file_catalog.FILE_SEIS_HEADER
                (SEIS_HEADER_ID,INVENTORY_ID,CATALOG_FILE_ID,FILE_NAME,
                 FILE_FORMAT,SURVEY_NAME,FIELD_NAME,VALUE,SOURCE)
                VALUES (:shid,:iid,:cfid,:fn,:fmt,:sv,:fn2,:val,:src)
            """), {
                "shid": r["SEIS_HEADER_ID"],
                "iid":  r.get("INVENTORY_ID"),
                "cfid": r.get("CATALOG_FILE_ID"),
                "fn":   r["FILE_NAME"],
                "fmt":  r["FILE_FORMAT"],
                "sv":   r.get("SURVEY_NAME"),
                "fn2":  r["FIELD_NAME"],
                "val":  r.get("VALUE"),
                "src":  r.get("SOURCE"),
            })


# ─────────────────────────────────────────────────────────────────────────────
# Export — pivot headers to wide format for CSV/Excel
# ─────────────────────────────────────────────────────────────────────────────

def export_well_headers(engine, fmt_filter="All",
                         repo_filter=None) -> "pd.DataFrame":
    """
    Pivot FILE_WELL_HEADER to wide format — one row per file,
    mnemonics as columns. Returns DataFrame ready for CSV/Excel export.
    """
    import pandas as pd
    from sqlalchemy import text

    where = "WHERE 1=1"
    params = {}
    if fmt_filter != "All":
        where += " AND h.FILE_FORMAT=:fmt"
        params["fmt"] = fmt_filter

    with engine.connect() as con:
        rows = con.execute(text(f"""
            SELECT h.FILE_NAME, h.UWI, h.FILE_FORMAT,
                   h.MNEMONIC, h.VALUE, h.UNIT
            FROM file_catalog.FILE_WELL_HEADER h
            {where}
            ORDER BY h.FILE_NAME, h.MNEMONIC
        """), params).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["FILE_NAME","UWI","FILE_FORMAT",
                                      "MNEMONIC","VALUE","UNIT"])
    # Pivot — value only (units in separate columns if needed)
    pivot = df.pivot_table(
        index=["FILE_NAME","UWI","FILE_FORMAT"],
        columns="MNEMONIC",
        values="VALUE",
        aggfunc="first"
    ).reset_index()
    pivot.columns.name = None
    for c in pivot.columns:
        pivot[c] = pivot[c].astype("string")
    return pivot


def export_seis_headers(engine, fmt_filter="All") -> "pd.DataFrame":
    """
    Export FILE_SEIS_HEADER to wide format.
    EBCDIC lines C01-C40 as columns, key binary fields as columns.
    """
    import pandas as pd
    from sqlalchemy import text

    where = "WHERE 1=1"
    params = {}
    if fmt_filter != "All":
        where += " AND FILE_FORMAT=:fmt"
        params["fmt"] = fmt_filter

    with engine.connect() as con:
        rows = con.execute(text(f"""
            SELECT FILE_NAME, SURVEY_NAME, FILE_FORMAT,
                   FIELD_NAME, VALUE, SOURCE
            FROM file_catalog.FILE_SEIS_HEADER
            {where}
            ORDER BY FILE_NAME, SOURCE, FIELD_NAME
        """), params).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["FILE_NAME","SURVEY_NAME","FILE_FORMAT",
                                      "FIELD_NAME","VALUE","SOURCE"])
    pivot = df.pivot_table(
        index=["FILE_NAME","SURVEY_NAME","FILE_FORMAT"],
        columns="FIELD_NAME",
        values="VALUE",
        aggfunc="first"
    ).reset_index()
    pivot.columns.name = None
    for c in pivot.columns:
        pivot[c] = pivot[c].astype("string")
    return pivot
