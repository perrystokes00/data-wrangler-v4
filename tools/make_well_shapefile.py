"""
make_well_shapefile.py
======================
DataView v3 — Export dv_well to a shapefile color-coded by well status.

Run from the v3 root after connecting to DataView:

    python make_well_shapefile.py
    python make_well_shapefile.py --db DataView_Test
    python make_well_shapefile.py --out C:\\GIS\\wells.shp --register

Options:
    --db       Database name          (default: DataView)
    --server   SQL Server instance    (default: 127.0.0.1\\SQLEXPRESS)
    --out      Output shapefile path  (default: output\\wells_by_status.shp)
    --register Auto-register shapefile in dv_spatial_layer table
    --help     Show this message

The shapefile includes:
    uwi, well_name, well_type, well_status, operator, field,
    county, state, spud_date, completion_date, final_td,
    lat, lon, status_color (hex), status_group

Color coding matches page_well_map.py STATUS_COLORS:
    ACTIVE / PRODUCING    #1D9E75  green
    COMPLETED             #378ADD  blue
    SHUT_IN / SUSPENDED   #EF9F27  amber
    ABANDONED             #E24B4A  red
    DRILLING              #B77FDD  purple
    PERMITTED             #888780  gray
    (default)             #888780  gray
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

# ── STATUS COLOR MAP ─────────────────────────────────────────────────────────
STATUS_COLORS = {
    "ACTIVE":                "#1D9E75",
    "PRODUCING":             "#1D9E75",
    "PRODUCER":              "#1D9E75",
    "COMPLETED":             "#378ADD",
    "SHUT_IN":               "#EF9F27",
    "SHUT-IN":               "#EF9F27",
    "SUSPENDED":             "#EF9F27",
    "ABANDONED":             "#E24B4A",
    "PLUGGED_AND_ABANDONED": "#E24B4A",
    "PLUGGED & ABANDONED":   "#E24B4A",
    "DRY_HOLE":              "#E24B4A",
    "DRILLING":              "#B77FDD",
    "PERMITTED":             "#888780",
    "LOCATION":              "#888780",
    "MONITORING":            "#378ADD",
    "UNKNOWN":               "#888780",
}
DEFAULT_COLOR = "#888780"

STATUS_GROUPS = {
    "ACTIVE":                "Active",
    "PRODUCING":             "Active",
    "PRODUCER":              "Active",
    "COMPLETED":             "Active",
    "SHUT_IN":               "Shut-In",
    "SHUT-IN":               "Shut-In",
    "SUSPENDED":             "Shut-In",
    "ABANDONED":             "P&A",
    "PLUGGED_AND_ABANDONED": "P&A",
    "PLUGGED & ABANDONED":   "P&A",
    "DRY_HOLE":              "P&A",
    "DRILLING":              "Drilling",
    "PERMITTED":             "Permitted",
    "LOCATION":              "Permitted",
    "MONITORING":            "Monitoring",
}
DEFAULT_GROUP = "Other"


def get_engine(server: str, db: str):
    """Create SQLAlchemy engine for SQL Server Express with Windows Auth."""
    try:
        from sqlalchemy import create_engine
        conn = (
            f"mssql+pyodbc://@{server}/{db}"
            "?driver=ODBC+Driver+17+for+SQL+Server"
            "&trusted_connection=yes"
        )
        engine = create_engine(conn, fast_executemany=True)
        # Test connection
        with engine.connect() as con:
            con.execute(__import__("sqlalchemy").text("SELECT 1"))
        return engine
    except Exception as e:
        print(f"  ERROR: Could not connect to {server}/{db}: {e}")
        sys.exit(1)


def export_wells(engine, out_path: Path, register: bool) -> int:
    """Query dv_well, build GeoDataFrame, write shapefile."""
    try:
        import geopandas as gpd
        from shapely.geometry import Point
        from sqlalchemy import text
        import pandas as pd
    except ImportError as e:
        print(f"  ERROR: Missing dependency — {e}")
        print("  Install: pip install geopandas shapely")
        sys.exit(1)

    print("  Querying dv_well...")
    sql = """
        SELECT
            w.uwi,
            w.well_name,
            w.well_type,
            w.well_status,
            w.api_num,
            CAST(w.surface_latitude  AS FLOAT) AS lat,
            CAST(w.surface_longitude AS FLOAT) AS lon,
            w.county,
            w.province_state       AS state,
            CONVERT(VARCHAR(10), w.spud_date,       120) AS spud_date,
            CONVERT(VARCHAR(10), w.completion_date, 120) AS completion_date,
            CAST(w.final_td AS FLOAT)           AS final_td,
            ISNULL(ba.ba_name,   'Unknown')     AS operator,
            ISNULL(f.field_name, 'Unknown')     AS field_name
        FROM dataview.dv_well w
        LEFT JOIN dataview.dv_business_associate ba
               ON ba.ba_id = w.operator_ba_id
        LEFT JOIN dataview.dv_field f
               ON f.field_id = w.field_id
        WHERE w.surface_latitude  IS NOT NULL
          AND w.surface_longitude IS NOT NULL
          AND w.surface_latitude  BETWEEN -90 AND 90
          AND w.surface_longitude BETWEEN -180 AND 180
        ORDER BY w.well_name
    """

    with engine.connect() as con:
        df = pd.read_sql(text(sql), con)

    if df.empty:
        print("  WARNING: No wells with coordinates found.")
        return 0

    total = len(df)
    print(f"  {total:,} wells loaded.")

    # Add color and group columns
    df["status_up"]    = df["well_status"].fillna("UNKNOWN").str.upper()
    df["status_color"] = df["status_up"].map(STATUS_COLORS).fillna(DEFAULT_COLOR)
    df["status_grp"]   = df["status_up"].map(STATUS_GROUPS).fillna(DEFAULT_GROUP)

    # Truncate text fields to shapefile 254-char limit
    str_cols = ["uwi","well_name","well_type","well_status","api_num",
                "county","state","spud_date","completion_date",
                "operator","field_name","status_color","status_grp"]
    for col in str_cols:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str).str[:254]

    # Build geometry
    print("  Building geometry...")
    geometry = [Point(lon, lat) for lat, lon in zip(df["lat"], df["lon"])]

    gdf = gpd.GeoDataFrame(
        df[["uwi","well_name","well_type","well_status","api_num",
            "lat","lon","county","state","spud_date","completion_date",
            "final_td","operator","field_name","status_color","status_grp"]],
        geometry=geometry,
        crs="EPSG:4326",
    )

    # Write shapefile
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Writing {out_path}...")
    gdf.to_file(str(out_path), driver="ESRI Shapefile")

    # Write companion CSV (full text, no 254 truncation)
    csv_path = out_path.with_suffix(".csv")
    df.drop(columns=["status_up"]).to_csv(csv_path, index=False)
    print(f"  Companion CSV: {csv_path}")

    # Write a simple style legend
    legend_path = out_path.with_suffix(".legend.txt")
    legend_path.write_text(
        "Well Status Color Legend\n"
        "========================\n" +
        "\n".join(f"  {grp:<20} {color}"
                  for grp, color in {
                      "Active/Producing": "#1D9E75",
                      "Completed":        "#378ADD",
                      "Shut-In":          "#EF9F27",
                      "P&A / Dry Hole":   "#E24B4A",
                      "Drilling":         "#B77FDD",
                      "Permitted/Location":"#888780",
                      "Other":            "#888780",
                  }.items()) +
        "\n"
    )

    if register:
        _register_layer(engine, out_path, total)

    return total


def _register_layer(engine, shp_path: Path, feature_count: int):
    """Register the shapefile in dv_spatial_layer for the map overlay."""
    try:
        from sqlalchemy import text
        import uuid

        layer_id = uuid.uuid4().hex[:40].upper()
        name     = f"Wells by Status ({datetime.now():%Y-%m-%d})"

        with engine.begin() as con:
            # Check if table exists
            exists = con.execute(text("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = 'dataview'
                  AND TABLE_NAME   = 'dv_spatial_layer'
            """)).scalar()

            if not exists:
                print("  WARNING: dv_spatial_layer table not found — skipping auto-register.")
                print(f"  Register manually in the Map → Registered Layers panel.")
                print(f"  Path: {shp_path}")
                return

            # Remove old wells-by-status layers
            con.execute(text("""
                DELETE FROM dataview.dv_spatial_layer
                WHERE layer_name LIKE 'Wells by Status%'
            """))

            con.execute(text("""
                INSERT INTO dataview.dv_spatial_layer (
                    layer_id, layer_name, layer_category, source_type,
                    file_path, feature_count,
                    style_color, style_weight, style_opacity,
                    style_fill_color, style_fill_opacity,
                    tooltip_fields, active_ind, source,
                    row_created_by, row_created_date,
                    row_changed_by, row_changed_date
                ) VALUES (
                    :lid, :name, 'WELL', 'SHAPEFILE',
                    :path, :cnt,
                    '#1D9E75', 1, 1.0,
                    '#1D9E75', 0.8,
                    'well_name,well_status,operator,field_name,final_td',
                    'Y', 'EXPORT',
                    'DataWrangler', GETUTCDATE(),
                    'DataWrangler', GETUTCDATE()
                )
            """), {
                "lid":  layer_id,
                "name": name,
                "path": str(shp_path),
                "cnt":  feature_count,
            })

        print(f"  Registered as: '{name}'")
        print(f"  Visible in Map → Registered Layers → WELL category.")

    except Exception as e:
        print(f"  WARNING: Auto-register failed: {e}")
        print(f"  Register manually — Path: {shp_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Export DataView wells to a color-coded shapefile."
    )
    parser.add_argument("--db",       default="DataView_Demo",
                        help="Database name (default: DataView)")
    parser.add_argument("--server",   default=r"127.0.0.1\SQLEXPRESS",
                        help=r"SQL Server instance (default: 127.0.0.1\SQLEXPRESS)")
    parser.add_argument("--out",      default=r"output\wells_by_status.shp",
                        help=r"Output .shp path (default: output\wells_by_status.shp)")
    parser.add_argument("--register", action="store_true",
                        help="Auto-register shapefile in dv_spatial_layer")
    args = parser.parse_args()

    out_path = Path(args.out)

    print("=" * 60)
    print("  DataView — Well Shapefile Export")
    print("=" * 60)
    print(f"  Server   : {args.server}")
    print(f"  Database : {args.db}")
    print(f"  Output   : {out_path.resolve()}")
    print(f"  Register : {args.register}")
    print()

    engine = get_engine(args.server, args.db)
    print(f"  Connected to {args.server}/{args.db}")

    n = export_wells(engine, out_path, args.register)

    print()
    print(f"  Done. {n:,} wells exported.")
    if not args.register:
        print()
        print("  To register in DataView map:")
        print(f"    Go to Map → Registered Layers → Register a shapefile")
        print(f"    Path: {out_path.resolve()}")
        print(f"    Category: WELL")
        print()
        print("  Or re-run with --register to auto-register:")
        print(f"    python make_well_shapefile.py --register")
    print("=" * 60)


if __name__ == "__main__":
    main()
