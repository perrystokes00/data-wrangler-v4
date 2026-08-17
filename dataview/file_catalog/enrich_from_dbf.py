"""
enrich_from_dbf.py
==================
Reads RRC API Data (apiNNN.dbf) and enriches dv_well with operator,
field, lease name, completion date, total depth.

Join: SUBSTRING(uwi, 3, 8) = APINUM

Usage:
    python enrich_from_dbf.py --county 003
    python enrich_from_dbf.py
    python enrich_from_dbf.py --dry-run
"""
from __future__ import annotations
import argparse, hashlib, os, sys, time
from pathlib import Path

# THE CHECK IS RIGHT; EXITING AT IMPORT TIME IS NOT. sys.exit() here
# raises SystemExit the moment anything imports this module — which is not
# an Exception, so it escapes a normal `except Exception` and takes the
# importing process down with it. That is exactly what it did to the
# regression harness: the whole run died part-way through with no summary.
#
# The dependency is still required to RUN, and main() still refuses to
# start without it. It is simply no longer required to READ.
_MISSING = None
try:
    import pandas as pd
    from dbfread import DBF
    from sqlalchemy import create_engine, text
except ImportError as _e:
    pd = DBF = create_engine = text = None
    _MISSING = str(_e)


def _require_deps():
    """Called at the top of main(). Fails loudly, at the right moment."""
    if _MISSING:
        raise SystemExit(
            f"enrich_from_dbf needs pandas, dbfread, sqlalchemy and pyodbc "
            f"({_MISSING}).\n  pip install pandas dbfread sqlalchemy pyodbc")

DEFAULT_DBF_DIR = Path(
    r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai"
    r"\data_wrangler\training\Texas\dbf"
)
DEFAULT_CONN = (
    "mssql+pyodbc://127.0.0.1\\SQLEXPRESS/DataView"
    "?driver=ODBC+Driver+17+for+SQL+Server"
    "&trusted_connection=yes"
    "&TrustServerCertificate=yes"
)


def parse_date(s):
    s = (s or "").strip()
    if not s or s == "0" or len(s) < 8 or s == "00000000":
        return None
    try:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    except Exception:
        return None


def read_dbf(path):
    """Read apiNNN.dbf → dict keyed by APINUM."""
    db = DBF(str(path), encoding="latin-1")
    lookup = {}
    for rec in db:
        api = (rec.get("APINUM") or "").strip()
        if not api or len(api) < 5:
            continue
        op = (rec.get("OPERATOR") or "").strip()
        fld = (rec.get("FIELD_NAME") or "").strip()
        lease = (rec.get("LEASE_NAME") or "").strip()
        if not op and not fld and not lease:
            continue
        td_raw = (rec.get("TOTAL_DEPT") or "0").strip()
        td = float(td_raw) if td_raw.isdigit() and int(td_raw) > 0 else None
        lookup[api] = {
            "operator": op,
            "field_name": fld,
            "lease_name": lease,
            "completion_date": parse_date(rec.get("COMPLETION")),
            "plug_date": parse_date(rec.get("PLUG_DATE")),
            "total_depth": td,
            "oil_gas_code": (rec.get("OIL_GAS_CO") or "").strip(),
        }
    return lookup


def enrich_county(engine, dbf_lookup, county_fips, dry_run=False):
    """Match DBF records to dv_well via BULK INSERT + UPDATE."""
    import csv

    stats = {"matched": 0, "updated": 0, "bas": 0, "fields": 0, "fks": 0}
    if not dbf_lookup:
        return stats

    csv_dir = Path(r"C:\Bulk")
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = str(csv_dir / f"dbf_{county_fips}.csv")

    # Write CSV — same pattern as shapefile loader
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f, delimiter="\t")
        for api, d in dbf_lookup.items():
            wr.writerow([
                api,
                d["operator"],
                d["field_name"],
                d["lease_name"],
                d["completion_date"] or "",
                d["plug_date"] or "",
                str(int(d["total_depth"])) if d["total_depth"] else "",
                d["oil_gas_code"],
            ])

    try:
        with engine.begin() as con:
            # Create temp table
            con.execute(text("""
                IF OBJECT_ID('tempdb..#dbf') IS NOT NULL DROP TABLE #dbf;
                CREATE TABLE #dbf (
                    apinum      NVARCHAR(8),
                    operator    NVARCHAR(100),
                    field_name  NVARCHAR(100),
                    lease_name  NVARCHAR(100),
                    comp_date   NVARCHAR(10),
                    plug_date   NVARCHAR(10),
                    total_depth NVARCHAR(10),
                    ogc         NVARCHAR(4)
                );
            """))

            # BULK INSERT — same pattern as shapefile loader
            _bulk_sql = (
                "BULK INSERT #dbf FROM '" + csv_path.replace("'","''") + "' "
                "WITH (FIELDTERMINATOR='\t', ROWTERMINATOR='\n', "
                "CODEPAGE='65001', TABLOCK)"
            )
            con.execute(text(_bulk_sql))

            # Verify
            n_loaded = con.execute(text("SELECT COUNT(*) FROM #dbf")).scalar()
            if n_loaded == 0:
                stats["matched"] = -1  # signal load failure
                return stats

            # Count matches
            stats["matched"] = con.execute(text("""
                SELECT COUNT(*) FROM dataview.dv_well w
                JOIN #dbf d ON SUBSTRING(w.uwi, 3, 8) = d.apinum
            """)).scalar() or 0

            if dry_run:
                return stats

            # ── Seed BAs ──────────────────────────────────────────
            con.execute(text("""
                INSERT INTO dataview.dv_business_associate (ba_id, ba_name, ba_type, active_ind, source)
                SELECT DISTINCT
                    CONVERT(VARCHAR(40), HASHBYTES('SHA1', UPPER(LTRIM(RTRIM(d.operator)))), 2),
                    UPPER(LTRIM(RTRIM(d.operator))),
                    'COMPANY', 'Y', 'RRC_DBF'
                FROM #dbf d
                WHERE d.operator <> ''
                  AND NOT EXISTS (
                    SELECT 1 FROM dataview.dv_business_associate ba
                    WHERE ba.ba_name = UPPER(LTRIM(RTRIM(d.operator)))
                  )
            """))
            stats["bas"] = con.execute(text(
                "SELECT COUNT(DISTINCT operator) FROM #dbf WHERE operator <> ''"
            )).scalar() or 0

            # ── Seed fields ───────────────────────────────────────
            con.execute(text("""
                INSERT INTO dataview.dv_field (field_id, field_name, active_ind, source)
                SELECT DISTINCT
                    CONVERT(VARCHAR(40), HASHBYTES('SHA1', UPPER(LTRIM(RTRIM(d.field_name)))), 2),
                    UPPER(LTRIM(RTRIM(d.field_name))),
                    'Y', 'RRC_DBF'
                FROM #dbf d
                WHERE d.field_name <> ''
                  AND NOT EXISTS (
                    SELECT 1 FROM dataview.dv_field f
                    WHERE f.field_name = UPPER(LTRIM(RTRIM(d.field_name)))
                  )
            """))
            stats["fields"] = con.execute(text(
                "SELECT COUNT(DISTINCT field_name) FROM #dbf WHERE field_name <> ''"
            )).scalar() or 0

            # ── Update well headers ───────────────────────────────
            result = con.execute(text("""
                UPDATE w
                SET w.well_name        = COALESCE(w.well_name, NULLIF(d.lease_name, '')),
                    w.completion_date  = COALESCE(w.completion_date,
                                          TRY_CAST(NULLIF(d.comp_date, '') AS DATE)),
                    w.final_td         = COALESCE(w.final_td,
                                          TRY_CAST(NULLIF(d.total_depth, '') AS FLOAT)),
                    w.row_changed_by   = 'DBF_ENRICH',
                    w.row_changed_date = GETUTCDATE()
                FROM dataview.dv_well w
                JOIN #dbf d ON SUBSTRING(w.uwi, 3, 8) = d.apinum
                WHERE (w.well_name IS NULL
                    OR w.completion_date IS NULL
                    OR w.final_td IS NULL)
            """))
            stats["updated"] = result.rowcount

            # ── Update operator FK ────────────────────────────────
            con.execute(text("""
                UPDATE w
                SET w.operator_ba_id = ba.ba_id
                FROM dataview.dv_well w
                JOIN #dbf d ON SUBSTRING(w.uwi, 3, 8) = d.apinum
                JOIN dataview.dv_business_associate ba
                  ON ba.ba_name = UPPER(LTRIM(RTRIM(d.operator)))
                WHERE w.operator_ba_id IS NULL
                  AND d.operator <> ''
            """))

            # ── Update field FK ───────────────────────────────────
            result = con.execute(text("""
                UPDATE w
                SET w.field_id = f.field_id
                FROM dataview.dv_well w
                JOIN #dbf d ON SUBSTRING(w.uwi, 3, 8) = d.apinum
                JOIN dataview.dv_field f
                  ON f.field_name = UPPER(LTRIM(RTRIM(d.field_name)))
                WHERE w.field_id IS NULL
                  AND d.field_name <> ''
            """))
            stats["fks"] = result.rowcount

    finally:
        try:
            os.unlink(csv_path)
        except Exception:
            pass

    return stats


def main():
    _require_deps()
    ap = argparse.ArgumentParser(description="Enrich dv_well from RRC API DBF files")
    ap.add_argument("--dbf-dir", type=Path, default=DEFAULT_DBF_DIR)
    ap.add_argument("--conn", default=DEFAULT_CONN)
    ap.add_argument("--county", type=str, default=None, help="Single county FIPS e.g. 003")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("WranglerView — DBF Enrichment")
    print(f"  DBF dir:  {args.dbf_dir}")
    print(f"  Dry run:  {args.dry_run}")
    print()

    engine = create_engine(args.conn)

    if args.county:
        dbf_files = list(args.dbf_dir.glob(f"api{args.county}.dbf"))
    else:
        dbf_files = sorted(args.dbf_dir.glob("api*.dbf"))

    if not dbf_files:
        sys.exit("No apiNNN.dbf files found")

    print(f"  Found {len(dbf_files)} DBF files\n")

    t0 = time.time()
    total_dbf = 0
    total_matched = 0
    total_updated = 0

    for i, dbf_path in enumerate(dbf_files, 1):
        county_fips = dbf_path.stem.replace("api", "")
        print(f"  [{i:3d}/{len(dbf_files)}] {dbf_path.name} (county {county_fips})… ",
              end="", flush=True)

        try:
            lookup = read_dbf(dbf_path)
            if not lookup:
                print("empty")
                continue
            total_dbf += len(lookup)

            stats = enrich_county(engine, lookup, county_fips, dry_run=args.dry_run)
            total_matched += stats["matched"]
            total_updated += stats["updated"]

            print(f"{len(lookup):>6,} DBF, "
                  f"{stats['matched']:>6,} matched, "
                  f"{stats['updated']:>5,} updated, "
                  f"{stats['bas']:>4,} BAs, "
                  f"{stats['fields']:>4,} fields, "
                  f"{stats['fks']:>4,} FKs")

        except Exception as e:
            print(f"ERROR: {e}")

    elapsed = time.time() - t0
    print(f"\n{'─' * 65}")
    print(f"  Files:    {len(dbf_files)}")
    print(f"  DBF recs: {total_dbf:,}")
    print(f"  Matched:  {total_matched:,}")
    print(f"  Updated:  {total_updated:,}")
    print(f"  Time:     {elapsed:.1f}s")
    if args.dry_run:
        print(f"  (DRY RUN — no changes)")
    print()


if __name__ == "__main__":
    main()
