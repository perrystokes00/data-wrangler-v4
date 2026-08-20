"""
dw_utils.py
===========
Shared utilities for DataView v3 format library and translators.
"""
from __future__ import annotations

import re
import urllib.parse
from datetime import datetime
from pathlib import Path


# ── Connection factory ────────────────────────────────────────────────

def make_engine(database: str = "DataView_Demo"):
    """Create SQLAlchemy engine for local SQL Server Express."""
    from sqlalchemy import create_engine
    cs = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER=127.0.0.1\\SQLEXPRESS;"
        f"DATABASE={database};"
        f"Trusted_Connection=yes;"
    )
    return create_engine(
        "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(cs),
        fast_executemany=True,
    )


# ── Date parsing ──────────────────────────────────────────────────────

DATE_FORMATS = [
    "%Y%m%d",       # 19830128
    "%d-%b-%y",     # 28-Jan-83
    "%d-%b-%Y",     # 28-Jan-1983
    "%Y-%m-%d",     # 1983-01-28
    "%m/%d/%Y",     # 01/28/1983
    "%m/%d/%y",     # 01/28/83
    "%d/%m/%Y",     # 28/01/1983
]

def parse_date(s: str) -> str | None:
    s = (s or "").strip().replace(" ", "")
    if not s or s == "00000000":
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


# ── String cleaning ───────────────────────────────────────────────────

def clean(s: str) -> str:
    """Collapse whitespace."""
    return " ".join(s.split()) if s else ""


def null_if_empty(s: str | None, nulls: tuple = ("unavailable", "unknown", "null", "none", "n/a", "")) -> str | None:
    if s is None:
        return None
    v = s.strip()
    if v.lower() in nulls:
        return None
    return v


# ── UWI builders ─────────────────────────────────────────────────────

def uwi_from_api(api: str, state_fips: str = "42") -> str | None:
    """
    Build UWI from a formatted API number.
    Handles: 42-135-12345-00, 4213512345, 15-007-01154 etc.
    """
    if not api:
        return None
    digits = re.sub(r"[^0-9]", "", str(api))
    if len(digits) < 10:
        return None
    # If api starts with state code use it, otherwise prepend state_fips
    if len(digits) >= 12:
        state   = digits[0:2]
        county  = digits[2:5]
        seq     = digits[5:10]
        side    = digits[10:12]
    elif len(digits) == 10:
        state   = state_fips
        county  = digits[0:3]
        seq     = digits[3:8]
        side    = digits[8:10]
    else:
        state   = state_fips
        county  = digits[0:3]
        seq     = digits[3:].zfill(5)[:5]
        side    = "00"
    return f"US{state}{county}{seq}{side}0000"


def uwi_from_rrc(county_fips: str, seq: str, sidetrack: str) -> str:
    """Build UWI from RRC components."""
    return f"US42{county_fips.zfill(3)}{seq.zfill(6)}{sidetrack.zfill(2)}0000"


# ── Audit row defaults ────────────────────────────────────────────────

def audit_row(loader_tag: str, active: bool = True) -> dict:
    return {
        "active_ind":      "Y" if active else "N",
        "row_created_by":  loader_tag,
        "row_changed_by":  loader_tag,
    }


# ── Bulk loader ───────────────────────────────────────────────────────

def bulk_insert(
    engine,
    table: str,
    schema: str,
    columns: list[str],
    rows: list[dict],
    chunk_size: int = 2000,
    upsert_key: str = "uwi",
) -> tuple[int, int, int]:
    """
    Fast bulk insert using raw pyodbc fast_executemany.
    Skips rows where upsert_key already exists (IF NOT EXISTS pattern).
    Returns (inserted, skipped_existing, errored).
    """
    if not rows:
        return 0, 0, 0

    import pandas as pd
    from sqlalchemy import text

    # Fetch existing keys
    with engine.connect() as con:
        existing = set(
            pd.read_sql(
                text(f"SELECT [{upsert_key}] FROM [{schema}].[{table}]"), con
            )[upsert_key].tolist()
        )

    new_rows = [r for r in rows if r.get(upsert_key) not in existing]
    skipped  = len(rows) - len(new_rows)

    if not new_rows:
        print(f"  All {len(rows):,} rows already exist — nothing to insert")
        return 0, skipped, 0

    col_list     = ", ".join(f"[{c}]" for c in columns)
    placeholders = ", ".join("?" * len(columns))
    check_col    = f"[{upsert_key}]"
    sql = (
        f"IF NOT EXISTS (SELECT 1 FROM [{schema}].[{table}] WHERE {check_col}=?)\n"
        f"INSERT INTO [{schema}].[{table}] ({col_list}) VALUES ({placeholders})"
    )

    inserted = errored = 0
    raw_conn = engine.raw_connection()
    try:
        cursor = raw_conn.cursor()
        cursor.fast_executemany = True
        for i in range(0, len(new_rows), chunk_size):
            batch  = new_rows[i:i+chunk_size]
            params = []
            for r in batch:
                params.append(tuple([r.get(upsert_key)] + [r.get(c) for c in columns]))
            try:
                cursor.executemany(sql, params)
                raw_conn.commit()
                inserted += len(batch)
                print(f"  Inserted {inserted:,} / {len(new_rows):,}...")
            except Exception as e:
                raw_conn.rollback()
                errored += len(batch)
                print(f"  Chunk error (rows {i}–{i+len(batch)}): {e}")
        cursor.close()
    finally:
        raw_conn.close()

    return inserted, skipped, errored


def bulk_update(
    engine,
    table: str,
    schema: str,
    set_columns: list[str],
    key_column: str,
    rows: list[dict],
    chunk_size: int = 2000,
    loader_tag: str = "LOADER",
) -> int:
    """Bulk UPDATE existing rows."""
    if not rows:
        return 0

    set_clause = ", ".join(
        f"[{c}] = COALESCE(?, [{c}])" for c in set_columns
    )
    sql = (
        f"UPDATE [{schema}].[{table}] SET {set_clause}, "
        f"[row_changed_by] = ?, [row_changed_date] = GETDATE() "
        f"WHERE [{key_column}] = ?"
    )

    updated  = 0
    raw_conn = engine.raw_connection()
    try:
        cursor = raw_conn.cursor()
        cursor.fast_executemany = True
        for i in range(0, len(rows), chunk_size):
            batch  = rows[i:i+chunk_size]
            params = [
                tuple([r.get(c) for c in set_columns] + [loader_tag, r[key_column]])
                for r in batch
            ]
            cursor.executemany(sql, params)
            raw_conn.commit()
            updated += len(batch)
            print(f"  Updated {updated:,} / {len(rows):,}...")
        cursor.close()
    finally:
        raw_conn.close()

    return updated


# ── Dedup ─────────────────────────────────────────────────────────────

def dedup(rows: list[dict], key: str = "uwi") -> list[dict]:
    """Keep last occurrence of each key value."""
    seen = {}
    for r in rows:
        if r.get(key):
            seen[r[key]] = r
    return list(seen.values())
