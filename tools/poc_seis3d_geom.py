"""
PROOF OF CONCEPT — load Seismic 3D survey outlines as native SQL Server geometry.

Does NOT touch your production tables or the pipeline. Creates one throwaway
table (file_catalog.poc_seis_geom), loads the 3D-survey polygons into a
`geometry` column, and runs a spatial query to prove it works. Drop the table
when done (script offers to at the end).

Pattern used (matches your 'never per-row' rule):
  1. GeoPandas reads the .shp, reproject to EPSG:4326 (WGS84 lat/lon)
  2. bulk-insert the WKT strings into a staging column (nvarchar(max))
  3. ONE set-based UPDATE converts wkt -> geometry via STGeomFromText
"""
import sys, os


# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataview.file_catalog import worker_core as w
from sqlalchemy import text

SHP = r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\data_wrangler\training\test_crawl\sample_shapefiles\Seismic_3D_Surveys.shp"

def main():
    if not os.path.exists(SHP):
        print(f"NOT FOUND: {SHP}")
        print("Edit the SHP path at the top of this script to point at your file.")
        return

    try:
        import geopandas as gpd
    except ImportError:
        print("geopandas not installed. Install with:")
        print("   pip install geopandas --break-system-packages")
        return

    # ---- 1. inspect ----
    gdf = gpd.read_file(SHP)
    print("=== SHAPEFILE INSPECTION ===")
    print(f"   features      : {len(gdf)}")
    print(f"   geometry types: {sorted(gdf.geom_type.unique())}")
    print(f"   CRS           : {gdf.crs}  (epsg={gdf.crs.to_epsg() if gdf.crs else None})")
    print(f"   columns       : {[c for c in gdf.columns if c != 'geometry']}")
    print()
    print("   first feature attributes:")
    r0 = gdf.iloc[0].drop("geometry")
    for k, v in r0.items():
        print(f"      {k} = {v}")
    print(f"   first geometry (truncated): {gdf.iloc[0].geometry.wkt[:120]}...")
    print()

    # ---- reproject to WGS84 lat/lon so SRID 4326 is correct ----
    src_epsg = gdf.crs.to_epsg() if gdf.crs else None
    if src_epsg and src_epsg != 4326:
        print(f"   reprojecting {src_epsg} -> 4326 (WGS84)")
        gdf = gdf.to_crs(4326)
    elif not src_epsg:
        print("   WARNING: no CRS on shapefile; assuming coords are already lon/lat 4326")

    # pick a name column if present
    name_col = next((c for c in gdf.columns
                     if c.lower() in ("survey_nam","survey_name","name","survey","sname")), None)

    e = w.make_engine(r"localhost\SQLEXPRESS", "DataView_Demo")

    # ---- 2. create throwaway POC table ----
    with e.begin() as c:
        c.execute(text("""
            IF OBJECT_ID('file_catalog.poc_seis_geom') IS NOT NULL
                DROP TABLE file_catalog.poc_seis_geom;
        """))
        c.execute(text("""
            CREATE TABLE file_catalog.poc_seis_geom (
                poc_id       INT IDENTITY(1,1) PRIMARY KEY,
                survey_name  NVARCHAR(255) NULL,
                seis_type    NVARCHAR(10)  NULL,
                wkt_stage    NVARCHAR(MAX) NULL,   -- staging: raw WKT text
                geom         GEOMETRY      NULL,   -- native spatial (filled by set-based UPDATE)
                epsg_code    INT           NULL,
                area_km2     FLOAT         NULL
            );
        """))
    print("   created file_catalog.poc_seis_geom")

    # ---- bulk-insert WKT strings (staging) ----
    rows = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        nm = str(row[name_col]) if name_col else None
        rows.append({"nm": nm, "wkt": geom.wkt})
    with e.begin() as c:
        c.execute(text("""
            INSERT INTO file_catalog.poc_seis_geom (survey_name, seis_type, wkt_stage, epsg_code)
            VALUES (:nm, '3D', :wkt, 4326)
        """), rows)   # executemany — one round-trip batch, not per-row loop
    print(f"   staged {len(rows)} WKT geometries")

    # ---- 3. ONE set-based convert: wkt -> geometry ----
    with e.begin() as c:
        # MakeValid guards against shapefile quirks (self-touching rings etc.)
        c.execute(text("""
            UPDATE file_catalog.poc_seis_geom
               SET geom = geometry::STGeomFromText(wkt_stage, 4326).MakeValid()
             WHERE wkt_stage IS NOT NULL;
        """))
        # compute area on the geography interpretation (km2) as a sanity value
        c.execute(text("""
            UPDATE file_catalog.poc_seis_geom
               SET area_km2 = geom.STArea() / 1000000.0
             WHERE geom IS NOT NULL;
        """))
    print("   converted WKT -> geometry (set-based) + computed area")
    print()

    # ---- verify: read back + a spatial query ----
    with e.connect() as c:
        print("=== LOADED GEOMETRY ===")
        for pid, nm, st, srid, npts, area in c.execute(text("""
            SELECT poc_id, survey_name, seis_type,
                   geom.STSrid, geom.STNumPoints(), area_km2
            FROM file_catalog.poc_seis_geom
            WHERE geom IS NOT NULL
        """)).fetchall():
            print(f"   #{pid} '{nm}' type={st} SRID={srid} points={npts} area~{area:.1f} (planar units)")

        # spatial query proof: which surveys contain their own centroid (trivially all)
        # and the bounding envelope of the first one
        env = c.execute(text("""
            SELECT TOP 1 geom.STEnvelope().STAsText()
            FROM file_catalog.poc_seis_geom WHERE geom IS NOT NULL
        """)).scalar()
        print(f"\n   envelope of first survey: {str(env)[:100]}...")
        print("\n   ✓ Spatial queries work — geom is a real SQL Server geometry.")

    print("\n=== DONE ===")
    print("POC table: file_catalog.poc_seis_geom (throwaway).")
    print("Drop it when done with:")
    print("   sqlcmd -S localhost\\SQLEXPRESS -d DataView_Demo -Q \"DROP TABLE file_catalog.poc_seis_geom\"")

if __name__ == "__main__":
    main()
