"""grids.py — rebuild H3 density GeoJSONs from CURRENT dv_well (no backfill).
Save this in the app folder (next to h3_grids.py / modules), then:
    py grids.py            # rebuilds R4 R5 R6 R7
    py grids.py 5 6        # only those resolutions
"""
import sys, os, urllib.parse as _u
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "modules"))
from sqlalchemy import create_engine
from dataview.mapping import h3_grids

CONN = (r"DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost\SQLEXPRESS;"
        r"DATABASE=DataView_Demo;Trusted_Connection=yes;Encrypt=no")
eng = create_engine("mssql+pyodbc:///?odbc_connect=" + _u.quote_plus(CONN))

res = [int(a) for a in sys.argv[1:] if a.isdigit()] or list(h3_grids.RESOLUTIONS)
out = r"C:\Bulk\mapbox_export"
os.makedirs(out, exist_ok=True)
for r in res:
    path = os.path.join(out, f"wells_r{r}.geojson")
    n = h3_grids.write_grid_geojson(eng, path, r)
    print(f"R{r}: {n:,} cells -> {path}", flush=True)
print("done — density grids rebuilt from current dv_well")
