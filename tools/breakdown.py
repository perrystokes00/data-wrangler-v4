import os
import sys

# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataview.file_catalog import worker_core as w
from sqlalchemy import text
e = w.make_engine(r"localhost\SQLEXPRESS", "DataView_Demo")
with e.connect() as c:
    for ext, n, rows in c.execute(text(
        "SELECT FILE_EXT, COUNT(*) n, SUM(ISNULL(PROC_ROWS,0)) r "
        "FROM file_catalog.GLOBAL_FILE_CATALOG WHERE PROC_STATUS='done' "
        "GROUP BY FILE_EXT ORDER BY COUNT(*) DESC")).fetchall():
        print(f"{ext:8} {n:>4} files  {rows or 0:>6} rows")
