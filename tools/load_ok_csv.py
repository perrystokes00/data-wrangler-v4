"""
load_ok_csv.py
==============
Loads Oklahoma OCC RBDMS wells from CSV into DataView dv_well.

Usage:
    python load_ok_csv.py
    python load_ok_csv.py --dry-run
    python load_ok_csv.py --limit 10000
"""
from __future__ import annotations
import argparse, csv, hashlib, os, sys, time
from pathlib import Path

try:
    from sqlalchemy import create_engine, text
except ImportError:
    sys.exit("pip install sqlalchemy pyodbc")

DEFAULT_FILE = Path(
    r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai"
    r"\data_wrangler\training\Oklahoma\rbdms-wells.csv"
)

DEFAULT_CONN = (
    "mssql+pyodbc://127.0.0.1\\SQLEXPRESS/DataView_Demo"
    "?driver=ODBC+Driver+17+for+SQL+Server"
    "&trusted_connection=yes"
    "&TrustServerCertificate=yes"
)

SOURCE = "OCC_OK"


def _sha1(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:40]

def _clean(s):
    return (s or "").replace("\t", " ").replace("|", " ").replace("\n", " ").replace("\r", "").replace('"', "").strip()


def main():
    ap = argparse.ArgumentParser(description="Load Oklahoma OCC CSV into DataView")
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE)
    ap.add_argument("--conn", default=DEFAULT_CONN)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    print("WranglerView — Oklahoma OCC Loader")
    print(f"  File: {args.file}")
    print(f"  Dry run: {args.dry_run}")
    print()

    if not args.file.exists():
        sys.exit(f"File not found: {args.file}")

    # ── Read CSV ──────────────────────────────────────────────────
    print("  Reading CSV…", end=" ", flush=True)
    t0 = time.time()

    wells = []
    skipped = 0

    with open(args.file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if args.limit and i >= args.limit:
                break

            api10 = _clean(row.get("API", ""))
            if not api10 or len(api10) < 5:
                skipped += 1
                continue

            lat = row.get("SH_LAT", "").strip()
            lon = row.get("SH_LON", "").strip()
            if not lat or not lon:
                skipped += 1
                continue
            try:
                flat = float(lat)
                flon = float(lon)
                if flat == 0 and flon == 0:
                    skipped += 1
                    continue
                if abs(flat) > 90 or abs(flon) > 180:
                    skipped += 1
                    continue
            except (ValueError, OverflowError):
                skipped += 1
                continue

            # Build 14-digit UWI: API10 + 0000
            uwi = api10.ljust(14, "0")[:14]

            well_name = _clean(row.get("WELL_NAME", ""))
            well_num = _clean(row.get("WELL_NUM", ""))
            if well_num:
                well_name = f"{well_name} {well_num}".strip()

            operator = _clean(row.get("OPERATOR", ""))
            status = _clean(row.get("WELLSTATUS", ""))
            well_type = _clean(row.get("WELLTYPE", ""))
            county = _clean(row.get("COUNTY", ""))

            wells.append({
                "uwi": uwi,
                "api_num": api10,
                "well_name": well_name,
                "operator_name": operator.upper() if operator else "",
                "field_name": "",
                "province_state": "OK",
                "county": county.title() if county else "",
                "lat": lat,
                "lon": lon,
                "well_status": status,
                "well_type": well_type,
            })

    print(f"{len(wells):,} wells, {skipped:,} skipped ({time.time()-t0:.1f}s)")

    if not wells:
        sys.exit("No wells found")

    if args.dry_run:
        print(f"\n  (DRY RUN — no changes)")
        return

    # ── Seed R_SOURCE ─────────────────────────────────────────────
    engine = create_engine(args.conn)
    with engine.begin() as con:
        con.execute(text("""
            IF NOT EXISTS (SELECT 1 FROM dataview.dv_r_source WHERE source = :src)
            INSERT INTO dataview.dv_r_source (source, short_name, long_name, active_ind)
            VALUES (:src, 'OCC', 'Oklahoma Corporation Commission', 'Y')
        """), {"src": SOURCE})

    # ── Write CSV for BULK INSERT ─────────────────────────────────
    csv_dir = Path(r"C:\Bulk")
    csv_dir.mkdir(parents=True, exist_ok=True)
    wells_csv = str(csv_dir / "ok_wells.csv")

    print("  Writing CSV…", end=" ", flush=True)
    with open(wells_csv, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f, delimiter="|", quoting=csv.QUOTE_NONE, escapechar="\\")
        for w in wells:
            wr.writerow([
                w["uwi"], w["api_num"], _clean(w["well_name"]),
                _clean(w["operator_name"]), w["field_name"],
                w["province_state"], _clean(w["county"]),
                w["lat"], w["lon"],
                w["well_status"], w["well_type"],
            ])

    size_mb = Path(wells_csv).stat().st_size / (1024 * 1024)
    print(f"{size_mb:.1f} MB")

    # ── Load into database ────────────────────────────────────────
    t1 = time.time()
    with engine.begin() as con:
        print("  Loading wells…", end=" ", flush=True)
        con.execute(text("""
            IF OBJECT_ID('tempdb..#w') IS NOT NULL DROP TABLE #w;
            CREATE TABLE #w (
                uwi             NVARCHAR(14),
                api_num         NVARCHAR(10),
                well_name       NVARCHAR(255),
                operator_name   NVARCHAR(255),
                field_name      NVARCHAR(255),
                province_state  NVARCHAR(20),
                county          NVARCHAR(100),
                lat             NVARCHAR(20),
                lon             NVARCHAR(20),
                well_status     NVARCHAR(40),
                well_type       NVARCHAR(40)
            );
        """))
        _sql = ("BULK INSERT #w FROM '" + wells_csv.replace("'", "''") + "' "
                "WITH (FIELDTERMINATOR='|', ROWTERMINATOR='\\n', "
                "CODEPAGE='65001', TABLOCK)")
        con.execute(text(_sql))

        # Dedup
        con.execute(text("""
            DELETE FROM #w WHERE uwi = '00000000000000'
               OR LEN(LTRIM(uwi)) < 10 OR uwi IS NULL
        """))
        con.execute(text("""
            ;WITH dupes AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY uwi ORDER BY well_name) AS rn
                FROM #w
            )
            DELETE FROM dupes WHERE rn > 1
        """))

        # Remove bad coordinates
        con.execute(text("""
            DELETE FROM #w
            WHERE TRY_CAST(lat AS FLOAT) IS NULL
               OR TRY_CAST(lon AS FLOAT) IS NULL
               OR ABS(TRY_CAST(lat AS FLOAT)) > 90
               OR ABS(TRY_CAST(lon AS FLOAT)) > 180
        """))
        n_temp = con.execute(text("SELECT COUNT(*) FROM #w")).scalar()
        print(f"{n_temp:,} staged")

        # MERGE
        print("  Merging into dv_well…", end=" ", flush=True)
        result = con.execute(text("""
            MERGE dataview.dv_well AS tgt
            USING (
                SELECT uwi, api_num, well_name, operator_name, field_name,
                       province_state, county,
                       TRY_CAST(NULLIF(lat, '') AS NUMERIC(15,10)) AS lat,
                       TRY_CAST(NULLIF(lon, '') AS NUMERIC(15,10)) AS lon,
                       NULLIF(well_status, '') AS well_status,
                       NULLIF(well_type, '') AS well_type
                FROM (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY uwi ORDER BY well_name) AS _rn
                    FROM #w
                ) _d WHERE _d._rn = 1
            ) AS src ON tgt.uwi = src.uwi
            WHEN NOT MATCHED THEN
                INSERT (uwi, api_num, well_name, operator_name, field_name,
                        province_state, county, country,
                        surface_latitude, surface_longitude,
                        well_status, well_type,
                        active_ind, source, row_created_by, row_created_date)
                VALUES (src.uwi, src.api_num, src.well_name,
                        src.operator_name, src.field_name,
                        src.province_state, src.county, 'US',
                        src.lat, src.lon,
                        src.well_status, src.well_type,
                        'Y', :src, 'OK_LOADER', GETUTCDATE())
            WHEN MATCHED THEN
                UPDATE SET
                    well_name       = COALESCE(tgt.well_name, src.well_name),
                    operator_name   = COALESCE(tgt.operator_name, src.operator_name),
                    surface_latitude  = COALESCE(tgt.surface_latitude, src.lat),
                    surface_longitude = COALESCE(tgt.surface_longitude, src.lon),
                    well_status     = COALESCE(tgt.well_status, src.well_status),
                    well_type       = COALESCE(tgt.well_type, src.well_type),
                    row_changed_by  = 'OK_LOADER',
                    row_changed_date = GETUTCDATE();
        """), {"src": SOURCE})

        n_affected = result.rowcount
        print(f"{n_affected:,} rows affected")

    # Cleanup
    try: os.unlink(wells_csv)
    except Exception: pass

    elapsed = time.time() - t1
    print(f"\n  Total: {len(wells):,} wells loaded in {elapsed:.1f}s")
    print("  Done!")


if __name__ == "__main__":
    main()
