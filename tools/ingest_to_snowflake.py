"""
ingest_to_snowflake.py
======================
Reads well data from local SQL Server (DataView) and writes to
Snowflake WELL_FEDERATION raw schemas.

Usage:
    python ingest_to_snowflake.py
    python ingest_to_snowflake.py --source tx
    python ingest_to_snowflake.py --source ks
    python ingest_to_snowflake.py --source osdu
    python ingest_to_snowflake.py --source gom
    python ingest_to_snowflake.py --source all

Environment variables:
    SNOWFLAKE_ACCOUNT   e.g. YDWXNCV-VL88062
    SNOWFLAKE_USER      e.g. PMSTOKES00
    SNOWFLAKE_PASSWORD  your password
"""
from __future__ import annotations
import argparse, os, sys, time
from datetime import datetime

try:
    import pandas as pd
    from sqlalchemy import create_engine, text
except ImportError:
    sys.exit("pip install pandas sqlalchemy pyodbc snowflake-sqlalchemy")


def get_sqlserver():
    return create_engine(
        "mssql+pyodbc://127.0.0.1\\SQLEXPRESS/DataView_Demo"
        "?driver=ODBC+Driver+17+for+SQL+Server"
        "&trusted_connection=yes"
        "&TrustServerCertificate=yes"
    )

def get_snowflake():
    account  = os.environ.get("SNOWFLAKE_ACCOUNT", "YDWXNCV-VL88062")
    user     = os.environ.get("SNOWFLAKE_USER", "PMSTOKES00")
    password = os.environ.get("SNOWFLAKE_PASSWORD", "")
    if not password:
        sys.exit("Set SNOWFLAKE_PASSWORD environment variable")
    return create_engine(
        f"snowflake://{user}:{password}@{account}/WELL_FEDERATION"
        f"?warehouse=WV_WH&role=ACCOUNTADMIN"
    )


def _load(sf_engine, df, schema, table):
    """Load a DataFrame to Snowflake. Drop + recreate to avoid schema mismatches."""
    t0 = time.time()
    print(f"  Loading {len(df):,} rows → {schema}.{table}… ", end="", flush=True)

    # Drop existing table so pandas creates with correct column widths
    try:
        with sf_engine.begin() as con:
            con.execute(text(f"DROP TABLE IF EXISTS {schema}.{table}"))
    except Exception:
        pass

    df.to_sql(table.lower(), sf_engine, schema=schema,
              if_exists="replace", index=False, method="multi",
              chunksize=5000)

    elapsed = time.time() - t0
    print(f"done ({elapsed:.1f}s)")


def ingest_tx(ss, sf):
    """Texas RRC shapefile wells → RAW_TX.WELL"""
    print("\n── Texas RRC wells ──────────────────────────────────")
    with ss.connect() as con:
        df = pd.read_sql(text("""
            SELECT uwi, api_num, well_name, operator_name, field_name,
                   surface_latitude, surface_longitude,
                   county, province_state,
                   well_status, well_type,
                   spud_date, completion_date, final_td,
                   source, area
            FROM dataview.dv_well
            WHERE source = 'RRC_TX_SHP'
              AND surface_latitude IS NOT NULL
        """), con)
    print(f"  Read {len(df):,} wells from SQL Server")
    if df.empty: return
    _load(sf, df, "RAW_TX", "WELL")


def ingest_ks(ss, sf):
    """Kansas KGS wells → RAW_KS.WELL"""
    print("\n── Kansas KGS wells ────────────────────────────────")
    with ss.connect() as con:
        df = pd.read_sql(text("""
            SELECT uwi, api_num, well_name, operator_name, field_name,
                   surface_latitude, surface_longitude,
                   county, province_state,
                   well_status, well_type,
                   spud_date, completion_date, final_td,
                   source, area
            FROM dataview.dv_well
            WHERE source = 'KGS_GEOJSON'
        """), con)
    print(f"  Read {len(df):,} wells from SQL Server")
    if df.empty: return
    _load(sf, df, "RAW_KS", "WELL")


def ingest_osdu(ss, sf):
    """OSDU wells → RAW_OSDU.WELL"""
    print("\n── OSDU wells ──────────────────────────────────────")
    with ss.connect() as con:
        df = pd.read_sql(text("""
            SELECT uwi, api_num, well_name, operator_name, field_name,
                   surface_latitude, surface_longitude,
                   county, province_state,
                   well_status, well_type,
                   spud_date, completion_date, final_td,
                   source, area
            FROM dataview.dv_well
            WHERE source = 'OSDU'
        """), con)
    print(f"  Read {len(df):,} wells from SQL Server")
    if df.empty: return
    _load(sf, df, "RAW_OSDU", "WELL")


def ingest_gom(ss, sf):
    """BOEM GOM wells → RAW_BOEM.WELL"""
    print("\n── BOEM GOM wells ──────────────────────────────────")
    try:
        with ss.connect() as con:
            df = pd.read_sql(text("""
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
            """), con)
    except Exception as e:
        print(f"  GOM query failed: {e}")
        return
    print(f"  Read {len(df):,} wells from SQL Server")
    if df.empty: return
    _load(sf, df, "RAW_BOEM", "WELL")


def verify(sf):
    """Row count verification."""
    print("\n── Verification ────────────────────────────────────")
    checks = [
        ("RAW_TX",   "WELL",   "Texas RRC"),
        ("RAW_KS",   "WELL",   "Kansas KGS"),
        ("RAW_OSDU", "WELL",   "OSDU"),
        ("RAW_BOEM", "WELL",   "BOEM GOM"),
    ]
    total = 0
    with sf.connect() as con:
        for schema, table, label in checks:
            try:
                n = con.execute(text(
                    f"SELECT COUNT(*) FROM {schema}.{table}"
                )).scalar() or 0
                total += n
                print(f"  {label:20s} {schema}.{table:15s} {n:>10,}")
            except Exception:
                print(f"  {label:20s} {schema}.{table:15s}      (empty)")
    print(f"  {'TOTAL':20s} {'':15s} {total:>10,}")


def main():
    ap = argparse.ArgumentParser(
        description="Ingest DataView → Snowflake WELL_FEDERATION")
    ap.add_argument("--source", default="all",
                    choices=["all", "tx", "ks", "osdu", "gom"])
    args = ap.parse_args()

    print("WranglerView v1 — Snowflake Ingest")
    print(f"  Timestamp: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  Source:    {args.source}")

    ss = get_sqlserver()
    sf = get_snowflake()

    print("\n  Testing SQL Server… ", end="")
    try:
        with ss.connect() as c: c.execute(text("SELECT 1"))
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}"); return

    print("  Testing Snowflake…  ", end="")
    try:
        with sf.connect() as c: c.execute(text("SELECT CURRENT_ACCOUNT()"))
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}"); return

    t0 = time.time()

    if args.source in ("all", "tx"):   ingest_tx(ss, sf)
    if args.source in ("all", "ks"):   ingest_ks(ss, sf)
    if args.source in ("all", "osdu"): ingest_osdu(ss, sf)
    if args.source in ("all", "gom"):  ingest_gom(ss, sf)

    verify(sf)

    print(f"\n  Total time: {time.time() - t0:.1f}s")
    print("  Done!")


if __name__ == "__main__":
    main()
