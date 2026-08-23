"""
recatalog_seis.py
-----------------
Standalone script to re-catalog all SEG-Y and P190 files in SEIS_FILE_CATALOG.
Re-parses each file from disk and updates ALL fields including coordinates.

Usage:
    python tools/recatalog_seis.py

Runs from anywhere: the header insert below puts the repo root on sys.path.
(There is no modules/ directory any more -- the reorg moved it into dataview/.)
Reads DB connection from .env (same as Data Wrangler).
"""

import sys, os
from pathlib import Path


# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Load .env ─────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional — env vars may already be set

# ── DB connection ─────────────────────────────────────────────────────────
def get_engine():
    try:
        from dataview.core.db_pool import get_engine as _ge
        return _ge()
    except Exception as e:
        print(f"Could not get engine from db_pool: {e}")
        # Fallback: build from env
        try:
            import sqlalchemy as sa
            server   = os.environ.get("DB_SERVER",   "localhost")
            database = os.environ.get("DB_DATABASE", "PPDM")
            driver   = os.environ.get("DB_DRIVER",   "ODBC Driver 17 for SQL Server")
            conn_str = (
                f"mssql+pyodbc://@{server}/{database}"
                f"?driver={driver.replace(' ', '+')}"
                f"&trusted_connection=yes"
            )
            return sa.create_engine(conn_str, fast_executemany=True)
        except Exception as e2:
            print(f"Could not build engine from env: {e2}")
            sys.exit(1)

# ── Schema migration ───────────────────────────────────────────────────────
def ensure_columns(engine):
    from sqlalchemy import text
    _new_cols = [
        ("SAMPLE_COUNT",    "ALTER TABLE [las_catalog].[SEIS_FILE_CATALOG] ADD [SAMPLE_COUNT] NUMERIC(10,0) NULL"),
        ("SEGY_REVISION",   "ALTER TABLE [las_catalog].[SEIS_FILE_CATALOG] ADD [SEGY_REVISION] NVARCHAR(10) NULL"),
        ("COORD_SYSTEM",    "ALTER TABLE [las_catalog].[SEIS_FILE_CATALOG] ADD [COORD_SYSTEM] NVARCHAR(255) NULL"),
        ("MIN_INLINE",      "ALTER TABLE [las_catalog].[SEIS_FILE_CATALOG] ADD [MIN_INLINE] NUMERIC(10,0) NULL"),
        ("MAX_INLINE",      "ALTER TABLE [las_catalog].[SEIS_FILE_CATALOG] ADD [MAX_INLINE] NUMERIC(10,0) NULL"),
        ("MIN_CROSSLINE",   "ALTER TABLE [las_catalog].[SEIS_FILE_CATALOG] ADD [MIN_CROSSLINE] NUMERIC(10,0) NULL"),
        ("MAX_CROSSLINE",   "ALTER TABLE [las_catalog].[SEIS_FILE_CATALOG] ADD [MAX_CROSSLINE] NUMERIC(10,0) NULL"),
        ("DEPTH_UOM",       "ALTER TABLE [las_catalog].[SEIS_FILE_CATALOG] ADD [DEPTH_UOM] NVARCHAR(10) NULL"),
        ("MAX_DEPTH_MS",    "ALTER TABLE [las_catalog].[SEIS_FILE_CATALOG] ADD [MAX_DEPTH_MS] NUMERIC(12,3) NULL"),
    ]
    added = []
    with engine.begin() as con:
        existing = {
            r[0] for r in con.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA='las_catalog' AND TABLE_NAME='SEIS_FILE_CATALOG'"
            )).fetchall()
        }
        for col, ddl in _new_cols:
            if col not in existing:
                try:
                    con.execute(text(ddl))
                    added.append(col)
                    print(f"  Added column: {col}")
                except Exception as e:
                    print(f"  Could not add {col}: {e}")
    if added:
        print(f"Schema updated: {added}")
    else:
        print("Schema is up to date.")
    return existing | set(c for c, _ in _new_cols)

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    from sqlalchemy import text

    print("=" * 60)
    print("  Data Wrangler — Seismic Re-catalog")
    print("=" * 60)

    engine = get_engine()
    print(f"\nConnected to DB.")

    print("\nChecking schema...")
    all_cols = ensure_columns(engine)

    # Load all catalogued files
    with engine.connect() as con:
        rows = con.execute(text(
            "SELECT SEIS_FILE_ID, FILE_FORMAT, FILE_NAME, "
            "CASE WHEN r.BASE_PATH IS NULL THEN f.FILE_NAME "
            "     WHEN RIGHT(r.BASE_PATH,1)='\\' THEN r.BASE_PATH + f.FILE_NAME "
            "     ELSE r.BASE_PATH + '\\' + f.FILE_NAME END AS FULL_PATH "
            "FROM [las_catalog].[SEIS_FILE_CATALOG] f "
            "LEFT JOIN [las_catalog].[WL_REPOSITORY] r "
            "  ON r.REPOSITORY_ID = f.REPOSITORY_ID "
            "ORDER BY f.FILE_FORMAT, f.FILE_NAME"
        )).fetchall()

    total   = len(rows)
    ok      = 0
    skipped = 0
    errors  = []

    print(f"\nFound {total} file(s) to re-catalog.\n")

    from dataview.file_catalog.segy_catalog import parse_segy_header
    from dataview.file_catalog.p190_catalog import parse_p190_header
    from datetime import datetime, timezone

    def now():
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    for i, row in enumerate(rows, 1):
        fid  = row[0]
        fmt  = (row[1] or "").upper()
        name = row[2] or ""
        fp   = row[3] or ""

        print(f"[{i:3d}/{total}] {name}", end="  ", flush=True)

        if not os.path.exists(fp):
            print(f"SKIP — not found: {fp}")
            skipped += 1
            continue

        try:
            if fmt == "SEGY":
                hdr = parse_segy_header(fp)
            elif fmt == "P190":
                hdr = parse_p190_header(fp)
            else:
                print(f"SKIP — unknown format: {fmt}")
                skipped += 1
                continue

            ts = now()
            _fields = {
                "SURVEY_NAME":        hdr.get("survey_name") or None,
                "LINE_NAME":          hdr.get("line_name")   or None,
                "DIMENSIONALITY":     hdr.get("dimensionality"),
                "TRACE_COUNT":        hdr.get("trace_count"),
                "SHOT_COUNT":         hdr.get("shot_count"),
                "FIRST_SHOT_POINT":   hdr.get("first_shot_point"),
                "LAST_SHOT_POINT":    hdr.get("last_shot_point"),
                "SAMPLE_INTERVAL_US": hdr.get("sample_interval_us"),
                "SAMPLE_COUNT":       hdr.get("sample_count"),
                "DATA_FORMAT":        hdr.get("data_format")   or None,
                "SEGY_REVISION":      str(hdr.get("segy_revision", "")),
                "VESSEL_NAME":        hdr.get("vessel_name")  or None,
                "CLIENT_NAME":        hdr.get("client_name")  or None,
                "NAV_SYSTEM":         hdr.get("nav_system")   or None,
                "ACQ_DATE_START":     hdr.get("acq_date_start") or None,
                "COORD_SYSTEM":       hdr.get("coord_system")  or None,
                "MIN_LAT":  hdr.get("min_lat"),  "MAX_LAT":  hdr.get("max_lat"),
                "MIN_LON":  hdr.get("min_lon"),  "MAX_LON":  hdr.get("max_lon"),
                "MIN_X":    hdr.get("min_x"),    "MAX_X":    hdr.get("max_x"),
                "MIN_Y":    hdr.get("min_y"),    "MAX_Y":    hdr.get("max_y"),
                "MIN_INLINE":    hdr.get("min_inline"),
                "MAX_INLINE":    hdr.get("max_inline"),
                "MIN_CROSSLINE": hdr.get("min_crossline"),
                "MAX_CROSSLINE": hdr.get("max_crossline"),
                "LAST_SEEN_DATE":   ts,
                "ROW_CHANGED_DATE": ts,
                "ROW_CHANGED_BY":   "RECATALOG",
            }

            # Only include columns that exist in the table
            safe = {k: v for k, v in _fields.items() if k in all_cols}
            safe["_id"] = fid
            set_clause = ", ".join(f"[{k}] = :{k}" for k in safe if k != "_id")

            with engine.begin() as con:
                con.execute(text(
                    f"UPDATE [las_catalog].[SEIS_FILE_CATALOG] "
                    f"SET {set_clause} WHERE SEIS_FILE_ID = :_id"
                ), safe)

            coord_status = (
                f"X={hdr.get('min_x'):,.0f}-{hdr.get('max_x'):,.0f}"
                if hdr.get("min_x") else
                f"Lat={hdr.get('min_lat'):.4f}" if hdr.get("min_lat") else
                "no coords"
            )
            print(f"OK  [{coord_status}]")
            ok += 1

        except Exception as e:
            print(f"ERROR — {e}")
            errors.append(f"{name}: {e}")

    print("\n" + "=" * 60)
    print(f"  Done: {ok} updated, {skipped} skipped, {len(errors)} errors")
    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  {e}")
    print("=" * 60)

if __name__ == "__main__":
    main()
