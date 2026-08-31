"""
populate_dv_well_protraction_area.py
====================================
One-time spatial join: assigns each Gulf of America well in
dataview.dv_well a `protraction_area` attribute by intersecting the
well's surface lat/lon with BOEM's protraction-areas shapefile.

After this script runs, the protraction area becomes a regular
queryable column on dv_well — usable in page_well_map's filter UI,
in SQL queries, in exports, anywhere.

WHAT IT DOES:
    1. Read BOEM protraction shapefile
    2. Reproject to EPSG:4326
    3. Inspect attribute columns (so you can verify the area-name column)
    4. ALTER TABLE dv_well ADD protraction_area NVARCHAR(100) NULL
       (idempotent — skipped if column already exists)
    5. Spatial join each GoM well against the polygons
    6. Bulk UPDATE dv_well.protraction_area for matched wells
    7. Print coverage report

USAGE:
    Edit the CONFIG section below if paths or column names differ.
    Then run from V3's project root:
        python populate_dv_well_protraction_area.py

DEPENDENCIES:
    pip install geopandas shapely pyodbc sqlalchemy

RUNTIME:
    ~2-5 minutes total on SQL Express:
      ~30 sec to read + reproject the shapefile
      ~10 sec to fetch 55K GoM wells
      ~30 sec for the spatial join (Python/GeoPandas, in memory)
      ~1-3 min for the UPDATE loop on SQL Express

REVERSIBLE:
    To remove:
        ALTER TABLE dataview.dv_well DROP COLUMN protraction_area;
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from sqlalchemy import create_engine, text


# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════

# Path to BOEM protraction shapefile
SHAPEFILE_PATH = r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\wrangler_view\shapefiles\GOM\protclip\protclip.shp"

# SQL Server connection (adjust if your V3 DSN differs)
SQL_CONN = (
    "mssql+pyodbc://@PERRY\\SQLEXPRESS/DataView_Demo"
    "?driver=ODBC+Driver+17+for+SQL+Server"
    "&trusted_connection=yes"
)

# Candidate column names in the BOEM shapefile that might hold the
# protraction-area name. The script tries them in order; first match
# wins. If none found, prints columns so you can pick the right one.
AREA_NAME_CANDIDATES = [
    "PROT_NAME",     # Most common BOEM column name
    "AREA_NAME",
    "PLAN_AREA1",
    "PROTRACTIO",
    "AREA",
    "NAME",
]

# How many wells to UPDATE per batch. Larger = faster on Express but
# more memory; 1000 is a safe middle.
UPDATE_BATCH = 1000


# ═══════════════════════════════════════════════════════════════════════
# STEP 1 — Read shapefile
# ═══════════════════════════════════════════════════════════════════════

def read_protraction_polygons():
    print("=" * 70)
    print("STEP 1: Read BOEM protraction shapefile")
    print("=" * 70)

    p = Path(SHAPEFILE_PATH)
    if not p.exists():
        print(f"❌ Shapefile not found: {SHAPEFILE_PATH}")
        sys.exit(1)

    print(f"Reading: {SHAPEFILE_PATH}")
    t0 = time.time()
    gdf = gpd.read_file(SHAPEFILE_PATH)
    print(f"  ✓ {len(gdf):,} polygons in {time.time() - t0:.1f}s")
    print(f"  Source CRS: {gdf.crs}")
    print(f"  Geometry types: {gdf.geometry.geom_type.unique().tolist()}")
    print(f"  Available columns: {[c for c in gdf.columns if c != 'geometry']}")

    # Reproject to WGS84 if needed
    if gdf.crs is None:
        print("  ⚠ Source has no CRS. Assuming EPSG:4326 (might be wrong).")
    elif gdf.crs.to_epsg() != 4326:
        print(f"  Reprojecting to EPSG:4326...")
        t0 = time.time()
        gdf = gdf.to_crs("EPSG:4326")
        print(f"  ✓ Reprojected in {time.time() - t0:.1f}s")

    print(f"  Bounds (lon_min, lat_min, lon_max, lat_max):")
    print(f"    {gdf.total_bounds}")

    return gdf


# ═══════════════════════════════════════════════════════════════════════
# STEP 2 — Pick the area-name column
# ═══════════════════════════════════════════════════════════════════════

def pick_area_name_column(gdf):
    print()
    print("=" * 70)
    print("STEP 2: Locate the area-name column")
    print("=" * 70)

    cols_upper = {c.upper(): c for c in gdf.columns}
    for candidate in AREA_NAME_CANDIDATES:
        if candidate.upper() in cols_upper:
            actual = cols_upper[candidate.upper()]
            samples = gdf[actual].dropna().unique()[:8]
            print(f"  ✓ Found area-name column: '{actual}'")
            print(f"  Sample values: {list(samples)}")
            print(f"  Distinct count: {gdf[actual].nunique()}")
            return actual

    print("  ❌ No standard area-name column found.")
    print(f"  Available: {[c for c in gdf.columns if c != 'geometry']}")
    print(f"  Edit AREA_NAME_CANDIDATES at top of script and re-run.")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════
# STEP 3 — Add protraction_area column if missing
# ═══════════════════════════════════════════════════════════════════════

def ensure_column(engine):
    print()
    print("=" * 70)
    print("STEP 3: Ensure dv_well.protraction_area column exists")
    print("=" * 70)

    check_sql = """
        SELECT COUNT(*) AS n
        FROM sys.columns
        WHERE object_id = OBJECT_ID('dataview.dv_well')
          AND name = 'protraction_area'
    """
    with engine.connect() as con:
        row = con.execute(text(check_sql)).fetchone()
        exists = row[0] > 0

    if exists:
        print("  ✓ Column already exists; skipping ALTER TABLE.")
    else:
        print("  Adding column...")
        with engine.begin() as con:
            con.execute(text(
                "ALTER TABLE dataview.dv_well "
                "ADD protraction_area NVARCHAR(100) NULL"
            ))
        print("  ✓ Added dv_well.protraction_area NVARCHAR(100) NULL")


# ═══════════════════════════════════════════════════════════════════════
# STEP 4 — Fetch GoM wells
# ═══════════════════════════════════════════════════════════════════════

def fetch_gom_wells(engine):
    print()
    print("=" * 70)
    print("STEP 4: Fetch Gulf of America wells from dv_well")
    print("=" * 70)

    sql = """
        SELECT uwi,
               surface_latitude AS lat,
               surface_longitude AS lon
        FROM dataview.dv_well
        WHERE province_state = 'Gulf of America'
          AND surface_latitude IS NOT NULL
          AND surface_longitude IS NOT NULL
    """
    t0 = time.time()
    df = pd.read_sql(sql, engine)
    print(f"  ✓ Fetched {len(df):,} GoM wells in {time.time() - t0:.1f}s")
    return df


# ═══════════════════════════════════════════════════════════════════════
# STEP 5 — Spatial join
# ═══════════════════════════════════════════════════════════════════════

def spatial_join(wells_df, polygons_gdf, area_col):
    print()
    print("=" * 70)
    print("STEP 5: Spatial join wells → polygons")
    print("=" * 70)

    t0 = time.time()

    # Build a GeoDataFrame of well points
    wells_gdf = gpd.GeoDataFrame(
        wells_df,
        geometry=[Point(xy) for xy in zip(wells_df["lon"], wells_df["lat"])],
        crs="EPSG:4326",
    )
    print(f"  Built {len(wells_gdf):,} well points "
          f"in {time.time() - t0:.1f}s")

    # Keep only the polygon's area-name and geometry to speed the join
    polys = polygons_gdf[[area_col, "geometry"]].rename(
        columns={area_col: "protraction_area"}
    )

    # Spatial join — 'within' = point inside polygon
    t1 = time.time()
    joined = gpd.sjoin(wells_gdf, polys, how="left", predicate="within")
    print(f"  ✓ sjoin completed in {time.time() - t1:.1f}s")

    # Drop wells that matched multiple polygons (overlap edge cases),
    # keeping the first match.
    joined = joined[~joined.index.duplicated(keep="first")]

    matched = joined["protraction_area"].notna().sum()
    print(f"  Wells matched to a protraction area: "
          f"{matched:,} of {len(wells_gdf):,} "
          f"({100 * matched / len(wells_gdf):.1f}%)")

    # Top areas by well count
    top = joined["protraction_area"].value_counts().head(10)
    print("  Top 10 areas by well count:")
    for name, n in top.items():
        print(f"    {name:25s} {n:>6,}")

    # Return just the columns we'll need
    out = joined[["uwi", "protraction_area"]].copy()
    return out


# ═══════════════════════════════════════════════════════════════════════
# STEP 6 — Bulk UPDATE dv_well
# ═══════════════════════════════════════════════════════════════════════

def bulk_update(engine, df):
    print()
    print("=" * 70)
    print("STEP 6: Update dv_well.protraction_area")
    print("=" * 70)

    # Only update wells that DID match a polygon
    df = df[df["protraction_area"].notna()].copy()
    if df.empty:
        print("  ⚠ No matches to write. Done.")
        return

    print(f"  Writing {len(df):,} matches in batches of {UPDATE_BATCH}...")
    t0 = time.time()

    update_sql = text(
        "UPDATE dataview.dv_well "
        "SET protraction_area = :pa "
        "WHERE uwi = :uwi"
    )

    n_written = 0
    with engine.begin() as con:
        for i in range(0, len(df), UPDATE_BATCH):
            batch = df.iloc[i:i + UPDATE_BATCH]
            params = [{"uwi": r.uwi, "pa": r.protraction_area}
                      for r in batch.itertuples()]
            con.execute(update_sql, params)
            n_written += len(batch)
            # Print every ~10K
            if n_written % (UPDATE_BATCH * 10) == 0 or n_written == len(df):
                elapsed = time.time() - t0
                rate = n_written / elapsed if elapsed > 0 else 0
                print(f"    {n_written:>6,} / {len(df):,} "
                      f"({rate:.0f} wells/sec)")

    print(f"  ✓ Wrote {n_written:,} rows in {time.time() - t0:.1f}s")


# ═══════════════════════════════════════════════════════════════════════
# STEP 7 — Verification
# ═══════════════════════════════════════════════════════════════════════

def verify(engine):
    print()
    print("=" * 70)
    print("STEP 7: Verify")
    print("=" * 70)

    sql = """
        SELECT
            protraction_area,
            COUNT(*) AS wells
        FROM dataview.dv_well
        WHERE province_state = 'Gulf of America'
        GROUP BY protraction_area
        ORDER BY COUNT(*) DESC
    """
    df = pd.read_sql(sql, engine)
    null_row = df[df["protraction_area"].isnull()]
    matched_rows = df[df["protraction_area"].notna()]
    null_count = int(null_row["wells"].iloc[0]) if not null_row.empty else 0
    matched_count = int(matched_rows["wells"].sum())

    print(f"  Matched:   {matched_count:,} wells across "
          f"{len(matched_rows):,} protraction areas")
    print(f"  Unmatched: {null_count:,} wells "
          f"(NULL protraction_area)")
    print()
    print("  All areas with counts:")
    for _, r in df.iterrows():
        name = r["protraction_area"] or "(NULL — no match)"
        print(f"    {name:30s} {r['wells']:>6,}")


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    overall = time.time()

    polygons_gdf = read_protraction_polygons()
    area_col = pick_area_name_column(polygons_gdf)

    print()
    print(f"Connecting to {SQL_CONN.split('@', 1)[-1].split('?')[0]}...")
    engine = create_engine(SQL_CONN)

    ensure_column(engine)
    wells_df = fetch_gom_wells(engine)
    matched = spatial_join(wells_df, polygons_gdf, area_col)
    bulk_update(engine, matched)
    verify(engine)

    print()
    print("=" * 70)
    print(f"DONE in {time.time() - overall:.1f}s")
    print("=" * 70)
    print()
    print("Next: restart Streamlit and protraction_area is queryable in:")
    print("  - page_well_map's filter UI (if it auto-discovers columns)")
    print("  - SQL queries directly")
    print("  - Future page_federation_search filters")


if __name__ == "__main__":
    main()
