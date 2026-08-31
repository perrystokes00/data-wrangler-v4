"""
ingest_to_snowflake_fast.py
===========================
Fast ingest to Snowflake using PUT + COPY INTO.
Writes CSV locally, uploads to Snowflake internal stage, then COPY INTO.
100x faster than pandas.to_sql for large datasets.

Usage:
    python ingest_to_snowflake_fast.py
    python ingest_to_snowflake_fast.py --source tx
    python ingest_to_snowflake_fast.py --source ks
    python ingest_to_snowflake_fast.py --source nd
    python ingest_to_snowflake_fast.py --source gom
    python ingest_to_snowflake_fast.py --source osdu
    python ingest_to_snowflake_fast.py --source all
"""
from __future__ import annotations
import argparse, csv, os, sys, time
from datetime import datetime
from pathlib import Path

try:
    import pandas as pd
    from sqlalchemy import create_engine, text
    import snowflake.connector
except ImportError:
    sys.exit("pip install pandas sqlalchemy pyodbc snowflake-connector-python")


CSV_DIR = Path(r"C:\Bulk")

def get_sqlserver():
    return create_engine(
        "mssql+pyodbc://127.0.0.1\\SQLEXPRESS/DataView_Demo"
        "?driver=ODBC+Driver+17+for+SQL+Server"
        "&trusted_connection=yes"
        "&TrustServerCertificate=yes"
    )

def get_snowflake_conn():
    """Raw snowflake.connector — needed for PUT command."""
    return snowflake.connector.connect(
        account=os.environ.get("SNOWFLAKE_ACCOUNT", "YDWXNCV-VL88062"),
        user=os.environ.get("SNOWFLAKE_USER", "PMSTOKES00"),
        password=os.environ.get("SNOWFLAKE_PASSWORD", ""),
        database="WELL_FEDERATION",
        warehouse="WV_WH",
        role="ACCOUNTADMIN",
    )


def _query_to_csv(ss_engine, sql, csv_path):
    """Query SQL Server via raw pyodbc and write pipe-delimited CSV."""
    import pyodbc
    t0 = time.time()

    conn = pyodbc.connect(
        "DRIVER={ODBC Driver 17 for SQL Server};"
        "SERVER=127.0.0.1\\SQLEXPRESS;"
        "DATABASE=DataView;"
        "Trusted_Connection=yes;"
        "TrustServerCertificate=yes;"
    )
    cursor = conn.cursor()
    print(f"  Executing query…", flush=True)
    cursor.execute(sql)

    columns = [desc[0] for desc in cursor.description]

    total = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        # Header
        f.write("|".join(columns) + "\n")

        while True:
            rows = cursor.fetchmany(50000)
            if not rows:
                break
            for row in rows:
                vals = []
                for v in row:
                    if v is None:
                        vals.append("")
                    else:
                        s = str(v).replace("|", " ").replace("\n", " ").replace("\r", "").replace('"', "")
                        vals.append(s)
                f.write("|".join(vals) + "\n")
            total += len(rows)
            print(f"    {total:>10,} rows…", flush=True)

    cursor.close()
    conn.close()

    if total == 0:
        return 0

    size_mb = Path(csv_path).stat().st_size / (1024 * 1024)
    print(f"  CSV ready: {csv_path} ({size_mb:.1f} MB, {time.time()-t0:.1f}s)")
    return total


def _put_and_copy(sf_conn, csv_path, schema, table):
    """PUT the CSV to a Snowflake stage, then COPY INTO the table."""
    cur = sf_conn.cursor()
    t0 = time.time()

    try:
        # Create schema stage if not exists
        cur.execute(f"USE SCHEMA {schema}")
        cur.execute(f"CREATE STAGE IF NOT EXISTS {schema}.LOAD_STAGE")

        # Drop and recreate table from CSV header
        with open(csv_path, "r", encoding="utf-8") as f:
            header = f.readline().strip().split("|")

        cur.execute(f"DROP TABLE IF EXISTS {schema}.{table}")
        cols_ddl = ", ".join(f'"{c.upper()}" VARCHAR' for c in header)
        cur.execute(f"CREATE TABLE {schema}.{table} ({cols_ddl})")

        # PUT — upload CSV to stage
        print(f"  PUT → @{schema}.LOAD_STAGE… ", flush=True)
        # Snowflake PUT needs forward slashes
        local_path = csv_path.replace("\\", "/")
        size_mb = Path(csv_path).stat().st_size / (1024 * 1024)
        print(f"    Uploading {size_mb:.1f} MB…", flush=True)
        cur.execute(f"PUT 'file://{local_path}' @{schema}.LOAD_STAGE AUTO_COMPRESS=TRUE OVERWRITE=TRUE")
        print(f"    Upload complete ({time.time()-t0:.1f}s)")

        # COPY INTO — bulk load from stage
        t1 = time.time()
        print(f"  COPY INTO {schema}.{table}… ", end="", flush=True)
        csv_name = Path(csv_path).name
        cur.execute(f"""
            COPY INTO {schema}.{table}
            FROM @{schema}.LOAD_STAGE/{csv_name}.gz
            FILE_FORMAT = (
                TYPE = 'CSV'
                FIELD_DELIMITER = '|'
                SKIP_HEADER = 1
                FIELD_OPTIONALLY_ENCLOSED_BY = NONE
                NULL_IF = ('')
                EMPTY_FIELD_AS_NULL = TRUE
            )
            ON_ERROR = 'CONTINUE'
        """)
        print(f"done ({time.time()-t1:.1f}s)")

        # Verify
        cur.execute(f"SELECT COUNT(*) FROM {schema}.{table}")
        n = cur.fetchone()[0]
        print(f"  Verified: {n:,} rows in {schema}.{table}")

        # Clean up stage file
        cur.execute(f"REMOVE @{schema}.LOAD_STAGE/{csv_name}.gz")

        return n

    finally:
        cur.close()


def ingest_source(ss, sf_conn, source_key, sql, schema, table):
    """Full pipeline: query → CSV → PUT → COPY INTO."""
    csv_path = str(CSV_DIR / f"sf_{source_key}.csv")

    n = _query_to_csv(ss, sql, csv_path)
    if n == 0:
        print("  No data — skipping")
        return 0

    result = _put_and_copy(sf_conn, csv_path, schema, table)

    # Cleanup local CSV
    try:
        os.unlink(csv_path)
    except Exception:
        pass

    return result


# ── Source queries ────────────────────────────────────────────────────

SQL_TX = """
    SELECT uwi, api_num, well_name, operator_name, field_name,
           surface_latitude, surface_longitude,
           county, province_state,
           well_status, well_type,
           spud_date, completion_date, final_td,
           source, area
    FROM dataview.dv_well
    WHERE source = 'RRC_TX_SHP'
      AND surface_latitude IS NOT NULL
"""

SQL_KS = """
    SELECT uwi, api_num, well_name, operator_name, field_name,
           surface_latitude, surface_longitude,
           county, province_state,
           well_status, well_type,
           spud_date, completion_date, final_td,
           source, area
    FROM dataview.dv_well
    WHERE source = 'KGS_GEOJSON'
"""

SQL_ND = """
    SELECT uwi, api_num, well_name, operator_name, field_name,
           surface_latitude, surface_longitude,
           county, province_state,
           well_status, well_type,
           spud_date, completion_date, final_td,
           source, area
    FROM dataview.dv_well
    WHERE source = 'NDIC'
"""

SQL_OSDU = """
    SELECT uwi, api_num, well_name, operator_name, field_name,
           surface_latitude, surface_longitude,
           county, province_state,
           well_status, well_type,
           spud_date, completion_date, final_td,
           source, area
    FROM dataview.dv_well
    WHERE source = 'OSDU'
"""

SQL_GOM = """
    SELECT CONVERT(VARCHAR(36), well_id) AS well_id,
           api_well_number, well_name, company_name,
           region, bottom_area_code, bottom_block_number,
           surface_latitude, surface_longitude,
           bottom_latitude, bottom_longitude,
           bh_total_md_ft, true_vertical_depth_ft,
           spud_date, status_code, type_code,
           water_depth_ft
    FROM dataview_gom.well
    WHERE surface_latitude IS NOT NULL
"""


def main():
    ap = argparse.ArgumentParser(
        description="Fast ingest to Snowflake via PUT + COPY INTO")
    ap.add_argument("--source", default="all",
                    choices=["all", "tx", "ks", "nd", "osdu", "gom"])
    args = ap.parse_args()

    print("WranglerView v1 — Snowflake Fast Ingest")
    print(f"  Timestamp: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  Source:    {args.source}")
    print()

    CSV_DIR.mkdir(parents=True, exist_ok=True)

    ss = get_sqlserver()

    print("  Testing SQL Server… ", end="")
    try:
        with ss.connect() as c: c.execute(text("SELECT 1"))
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}"); return

    print("  Testing Snowflake…  ", end="")
    try:
        sf = get_snowflake_conn()
        cur = sf.cursor()
        cur.execute("SELECT CURRENT_ACCOUNT()")
        print(f"OK ({cur.fetchone()[0]})")
        cur.close()
    except Exception as e:
        print(f"FAILED: {e}"); return

    t0 = time.time()

    sources = {
        "tx":   ("Texas RRC",       SQL_TX,   "RAW_TX",   "WELL"),
        "ks":   ("Kansas KGS",      SQL_KS,   "RAW_KS",   "WELL"),
        "nd":   ("North Dakota",    SQL_ND,   "RAW_ND",    "WELL"),
        "osdu": ("OSDU",            SQL_OSDU, "RAW_OSDU",  "WELL"),
        "gom":  ("BOEM GOM",        SQL_GOM,  "RAW_BOEM",  "WELL"),
    }

    for key, (label, sql, schema, table) in sources.items():
        if args.source not in ("all", key):
            continue
        print(f"\n── {label} ──────────────────────────────────────")
        try:
            # Create schema if needed
            cur = sf.cursor()
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            cur.close()
            ingest_source(ss, sf, key, sql, schema, table)
        except Exception as e:
            print(f"  ERROR: {e}")

    # Verification
    print("\n── Verification ────────────────────────────────────")
    total = 0
    cur = sf.cursor()
    for key, (label, sql, schema, table) in sources.items():
        try:
            cur.execute(f"SELECT COUNT(*) FROM {schema}.{table}")
            n = cur.fetchone()[0]
            total += n
            print(f"  {label:20s} {schema}.{table:15s} {n:>10,}")
        except Exception:
            print(f"  {label:20s} {schema}.{table:15s}      (empty)")
    cur.close()
    print(f"  {'TOTAL':20s} {'':15s} {total:>10,}")

    sf.close()
    print(f"\n  Total time: {time.time() - t0:.1f}s")
    print("  Done!")


if __name__ == "__main__":
    main()
