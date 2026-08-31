"""
load_nd_gdb.py
==============
Loads North Dakota NDIC wells from the NDOGD.gdb file geodatabase
into DataView dv_well.

Usage:
    python load_nd_gdb.py
    python load_nd_gdb.py --file "path/to/NDOGD.gdb"
    python load_nd_gdb.py --dry-run
"""
from __future__ import annotations
import argparse, csv, hashlib, os, sys, time
from pathlib import Path

try:
    import fiona
    from sqlalchemy import create_engine, text
except ImportError:
    sys.exit("pip install fiona sqlalchemy pyodbc")

DEFAULT_FILE = Path(
    r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai"
    r"\data_wrangler\training\North_Dakota\gdb\NDOGD.gdb"
)

DEFAULT_CONN = (
    "mssql+pyodbc://127.0.0.1\\SQLEXPRESS/DataView_Demo"
    "?driver=ODBC+Driver+17+for+SQL+Server"
    "&trusted_connection=yes"
    "&TrustServerCertificate=yes"
)

SOURCE = "NDIC"


def _sha1(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:40]

def _clean(s):
    return (s or "").replace("\t", " ").replace("|", " ").replace("\n", " ").replace("\r", "").replace('"', "").strip()

def _trunc(s, n):
    s = _clean(s)
    return s[:n] if s else ""

def _parse_date(d):
    """Parse fiona datetime string like '1928-05-27T00:00:01+00:00' → '1928-05-27'."""
    if not d or d == "None":
        return ""
    return str(d)[:10]


def main():
    ap = argparse.ArgumentParser(description="Load ND NDIC GDB into DataView")
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE)
    ap.add_argument("--conn", default=DEFAULT_CONN)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("WranglerView — North Dakota GDB Loader")
    print(f"  File: {args.file}")
    print(f"  Dry run: {args.dry_run}")
    print()

    if not args.file.exists():
        sys.exit(f"File not found: {args.file}")

    # ── Read GDB wells layer ──────────────────────────────────────
    print("  Reading OGD_Wells layer…", end=" ", flush=True)
    t0 = time.time()

    wells = []
    operators = set()
    fields = set()

    with fiona.open(str(args.file), layer="OGD_Wells") as src:
        for feat in src:
            props = feat["properties"]
            geom = feat.get("geometry")
            if not geom:
                continue
            coords = geom.get("coordinates", [])
            if len(coords) < 2:
                continue

            lon, lat = coords[0], coords[1]
            if lat == 0 and lon == 0:
                continue

            # UWI from api field (14-digit no dashes)
            uwi = _trunc(props.get("api"), 14)
            if not uwi or len(uwi) < 10:
                continue

            op_name = _trunc(props.get("operator"), 255)
            fld_name = _trunc(props.get("field_name"), 255)
            well_name = _trunc(props.get("well_name"), 255)
            county = _trunc(props.get("County"), 100)
            status = _trunc(props.get("status"), 40)
            well_type = _trunc(props.get("well_type"), 40)
            td = props.get("td")
            spud = _parse_date(props.get("spud_date"))

            # Operator
            op_id = ""
            if op_name:
                op_upper = op_name.upper()
                op_id = _sha1(op_upper)
                operators.add((op_id, op_upper))

            # Field
            fld_id = ""
            if fld_name and fld_name.upper() != "WILDCAT":
                fld_upper = fld_name.upper()
                fld_id = _sha1(fld_upper)
                fields.add((fld_id, fld_upper))

            wells.append({
                "uwi": uwi,
                "well_name": well_name,
                "api_num": uwi[:10],
                "operator_name": op_name.upper() if op_name else "",
                "field_name": fld_name.upper() if fld_name else "",
                "operator_ba_id": op_id,
                "field_id": fld_id,
                "county": county.title(),
                "province_state": "ND",
                "lat": lat,
                "lon": lon,
                "well_status": status,
                "well_type": well_type,
                "final_td": td,
                "spud_date": spud,
            })

    print(f"{len(wells):,} wells, {len(operators):,} operators, {len(fields):,} fields ({time.time()-t0:.1f}s)")

    if not wells:
        sys.exit("No wells found")

    if args.dry_run:
        print("\n  (DRY RUN — no changes)")
        return

    # ── Seed R_SOURCE ─────────────────────────────────────────────
    engine = create_engine(args.conn)
    with engine.begin() as con:
        con.execute(text("""
            IF NOT EXISTS (SELECT 1 FROM dataview.dv_r_source WHERE source = :src)
            INSERT INTO dataview.dv_r_source (source, short_name, long_name, active_ind)
            VALUES (:src, 'NDIC', 'North Dakota Industrial Commission', 'Y')
        """), {"src": SOURCE})

    # ── Write CSVs ────────────────────────────────────────────────
    csv_dir = Path(r"C:\Bulk")
    csv_dir.mkdir(parents=True, exist_ok=True)

    # BAs
    ba_csv = str(csv_dir / "nd_ba.csv")
    with open(ba_csv, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f, delimiter="|", quoting=csv.QUOTE_NONE, escapechar="\\")
        for ba_id, name in operators:
            wr.writerow([ba_id, _clean(name)[:255]])

    # Fields
    fld_csv = str(csv_dir / "nd_fld.csv")
    with open(fld_csv, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f, delimiter="|", quoting=csv.QUOTE_NONE, escapechar="\\")
        for fid, name in fields:
            wr.writerow([fid, _clean(name)[:255]])

    # Wells
    wells_csv = str(csv_dir / "nd_wells.csv")
    with open(wells_csv, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f, delimiter="|", quoting=csv.QUOTE_NONE, escapechar="\\")
        for w in wells:
            wr.writerow([
                w["uwi"], w["api_num"], _clean(w["well_name"]),
                _clean(w["operator_name"]), _clean(w["field_name"]),
                w["operator_ba_id"], w["field_id"],
                w["province_state"], _clean(w["county"]),
                w["lat"], w["lon"],
                _clean(w["well_status"]), _clean(w["well_type"]),
                w["final_td"] if w["final_td"] else "",
                w["spud_date"],
            ])

    print(f"  Wrote CSVs to {csv_dir}")

    # ── Load into database ────────────────────────────────────────
    t1 = time.time()
    with engine.begin() as con:
        # ── Seed BAs ──────────────────────────────────────────
        print("  Seeding operators…", end=" ", flush=True)
        con.execute(text("""
            IF OBJECT_ID('tempdb..#ba') IS NOT NULL DROP TABLE #ba;
            CREATE TABLE #ba (ba_id NVARCHAR(40), ba_name NVARCHAR(500));
        """))
        _sql = ("BULK INSERT #ba FROM '" + ba_csv.replace("'","''") + "' "
                "WITH (FIELDTERMINATOR='|', ROWTERMINATOR='\\n', "
                "CODEPAGE='65001', TABLOCK)")
        con.execute(text(_sql))
        con.execute(text("""
            MERGE dataview.dv_business_associate AS tgt
            USING #ba AS src ON tgt.ba_id = src.ba_id
            WHEN NOT MATCHED THEN
                INSERT (ba_id, ba_name, ba_type, active_ind, source)
                VALUES (src.ba_id, src.ba_name, 'COMPANY', 'Y', :src);
        """), {"src": SOURCE})
        print(f"{len(operators):,} done")

        # ── Seed fields ───────────────────────────────────────
        print("  Seeding fields…", end=" ", flush=True)
        con.execute(text("""
            IF OBJECT_ID('tempdb..#fld') IS NOT NULL DROP TABLE #fld;
            CREATE TABLE #fld (field_id NVARCHAR(40), field_name NVARCHAR(500));
        """))
        _sql = ("BULK INSERT #fld FROM '" + fld_csv.replace("'","''") + "' "
                "WITH (FIELDTERMINATOR='|', ROWTERMINATOR='\\n', "
                "CODEPAGE='65001', TABLOCK)")
        con.execute(text(_sql))
        con.execute(text("""
            MERGE dataview.dv_field AS tgt
            USING #fld AS src ON tgt.field_id = src.field_id
            WHEN NOT MATCHED THEN
                INSERT (field_id, field_name, active_ind, source)
                VALUES (src.field_id, src.field_name, 'Y', :src);
        """), {"src": SOURCE})
        print(f"{len(fields):,} done")

        # ── Load wells ────────────────────────────────────────
        print("  Loading wells…", end=" ", flush=True)
        con.execute(text("""
            IF OBJECT_ID('tempdb..#w') IS NOT NULL DROP TABLE #w;
            CREATE TABLE #w (
                uwi             NVARCHAR(14),
                api_num         NVARCHAR(10),
                well_name       NVARCHAR(255),
                operator_name   NVARCHAR(255),
                field_name      NVARCHAR(255),
                operator_ba_id  NVARCHAR(40),
                field_id        NVARCHAR(40),
                province_state  NVARCHAR(20),
                county          NVARCHAR(100),
                lat             NVARCHAR(20),
                lon             NVARCHAR(20),
                well_status     NVARCHAR(40),
                well_type       NVARCHAR(40),
                final_td        NVARCHAR(20),
                spud_date       NVARCHAR(20)
            );
        """))
        _sql = ("BULK INSERT #w FROM '" + wells_csv.replace("'","''") + "' "
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

        n_temp = con.execute(text("SELECT COUNT(*) FROM #w")).scalar()
        print(f"{n_temp:,} staged")

        # MERGE
        print("  Merging into dv_well…", end=" ", flush=True)
        result = con.execute(text("""
            MERGE dataview.dv_well AS tgt
            USING (
                SELECT uwi, api_num, well_name, operator_name, field_name,
                       NULLIF(operator_ba_id, '') AS operator_ba_id,
                       NULLIF(field_id, '') AS field_id,
                       province_state, county,
                       TRY_CAST(NULLIF(lat, '') AS FLOAT) AS lat,
                       TRY_CAST(NULLIF(lon, '') AS FLOAT) AS lon,
                       NULLIF(well_status, '') AS well_status,
                       NULLIF(well_type, '') AS well_type,
                       TRY_CAST(NULLIF(final_td, '') AS FLOAT) AS final_td,
                       TRY_CAST(NULLIF(spud_date, '') AS DATE) AS spud_date
                FROM (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY uwi ORDER BY well_name) AS _rn
                    FROM #w
                ) _d WHERE _d._rn = 1
            ) AS src ON tgt.uwi = src.uwi
            WHEN NOT MATCHED THEN
                INSERT (uwi, api_num, well_name, operator_name, field_name,
                        operator_ba_id, field_id,
                        province_state, county, country,
                        surface_latitude, surface_longitude,
                        well_status, well_type, final_td, spud_date,
                        active_ind, source, row_created_by, row_created_date)
                VALUES (src.uwi, src.api_num, src.well_name,
                        src.operator_name, src.field_name,
                        src.operator_ba_id, src.field_id,
                        src.province_state, src.county, 'US',
                        src.lat, src.lon,
                        src.well_status, src.well_type,
                        src.final_td, src.spud_date,
                        'Y', :src, 'ND_LOADER', GETUTCDATE())
            WHEN MATCHED THEN
                UPDATE SET
                    well_name       = COALESCE(tgt.well_name, src.well_name),
                    operator_name   = COALESCE(tgt.operator_name, src.operator_name),
                    field_name      = COALESCE(tgt.field_name, src.field_name),
                    operator_ba_id  = COALESCE(tgt.operator_ba_id, src.operator_ba_id),
                    field_id        = COALESCE(tgt.field_id, src.field_id),
                    surface_latitude  = COALESCE(tgt.surface_latitude, src.lat),
                    surface_longitude = COALESCE(tgt.surface_longitude, src.lon),
                    well_status     = COALESCE(tgt.well_status, src.well_status),
                    well_type       = COALESCE(tgt.well_type, src.well_type),
                    final_td        = COALESCE(tgt.final_td, src.final_td),
                    spud_date       = COALESCE(tgt.spud_date, src.spud_date),
                    row_changed_by  = 'ND_LOADER',
                    row_changed_date = GETUTCDATE();
        """), {"src": SOURCE})

        n_affected = result.rowcount
        print(f"{n_affected:,} rows affected")

    # Cleanup
    for f in [ba_csv, fld_csv, wells_csv]:
        try: os.unlink(f)
        except Exception: pass

    elapsed = time.time() - t1
    print(f"\n  Total: {len(wells):,} wells loaded in {elapsed:.1f}s")
    print("  Done!")


if __name__ == "__main__":
    main()
