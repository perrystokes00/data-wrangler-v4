"""
run_h3.py — H3 backfill for dv_well via BCP (no pyodbc row reads). Writes ONLY
h3_r4..h3_r7 (nvarchar). Skips h3_coord_hash entirely (optional metadata).

  py tools/run_h3.py --all     # recompute all wells with coordinates
  py tools/run_h3.py           # only wells missing H3 (h3_r5 IS NULL)
  py tools/run_h3.py --grid 4 5 6 7   # backfill, then write those density grids
  py tools/run_h3.py --grid-only 4 5 6 7   # ONLY rebuild grids (no backfill)
"""
import sys, os, csv, time, tempfile, subprocess, urllib.parse as _u


# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from dataview.mapping import h3_grids

def log(m): print(m, flush=True)

SERVER, DATABASE = r"localhost\SQLEXPRESS", "DataView_Demo"
RAW = (r"DRIVER={ODBC Driver 18 for SQL Server};SERVER=" + SERVER +
       r";DATABASE=" + DATABASE + r";Trusted_Connection=yes;Encrypt=no")
eng = create_engine("mssql+pyodbc:///?odbc_connect=" + _u.quote_plus(RAW))
SCHEMA, TABLE, KEY, STG = "dataview", "dv_well", "uwi", "stg"
H3COLS = list(h3_grids.H3_COLUMNS)          # ['h3_r4','h3_r5','h3_r6','h3_r7']


def bcp(args):
    r = subprocess.run(["bcp"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("bcp failed: " + (r.stderr or r.stdout or "")[:300])
    for line in r.stdout.splitlines():
        if line.strip().endswith(("rows copied.", "row copied.")):
            try: return int(line.split()[0].replace(",", ""))
            except Exception: return 0
    return 0


def _write_grids():
    """Write density GeoJSON(s) for the resolutions after --grid. Multiple
    resolutions allowed: --grid 4 5 6 7. Defaults to R5."""
    flag = "--grid-only" if "--grid-only" in sys.argv else "--grid"
    i = sys.argv.index(flag)
    res = [int(a) for a in sys.argv[i + 1:] if a.isdigit()] or [5]
    for r in res:
        path = fr"C:\Bulk\mapbox_export\wells_r{r}.geojson"
        log(f"grid R{r}: {h3_grids.write_grid_geojson(eng, path, r):,} cells -> {path}")


def main():
    # grids only — skip the backfill, just re-aggregate counts from dv_well
    if "--grid-only" in sys.argv:
        _write_grids()
        return
    t0 = time.time()
    with eng.begin() as c:
        c.execute(text("IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg')"))
        c.execute(text(f"IF OBJECT_ID('{STG}.dv_well_h3_stage') IS NOT NULL "
                       f"DROP TABLE {STG}.dv_well_h3_stage"))
    try: h3_grids.ensure_h3_columns(eng)
    except Exception as e: log(f"ensure_h3_columns: {e}")

    where = "WHERE surface_latitude IS NOT NULL AND surface_longitude IS NOT NULL"
    if "--all" not in sys.argv:
        where += " AND h3_r5 IS NULL"
    sel = (f"SELECT {KEY}, CAST(surface_latitude AS FLOAT), "
           f"CAST(surface_longitude AS FLOAT) FROM {SCHEMA}.{TABLE} {where}")

    tmp = tempfile.gettempdir()
    coords = os.path.join(tmp, "h3_coords.csv")
    result = os.path.join(tmp, "h3_result.csv")

    log("[1/4] bcp queryout coords -> CSV …")
    n = bcp([" ".join(sel.split()), "queryout", coords, "-c", "-t|",
             "-C", "65001", "-T", f"-S{SERVER}", f"-d{DATABASE}", "-q"])
    log(f"      {n:,} rows in {time.time()-t0:.1f}s")
    if n == 0:
        log("nothing to do."); return

    log("[2/4] computing H3 (r4..r7) -> result CSV …")
    computed_rows = 0
    to_cell, _ = h3_grids._bind_h3()
    with open(coords, encoding="utf-8", errors="replace") as fin, \
         open(result, "w", encoding="utf-8", newline="") as fout:
        w = csv.writer(fout, delimiter="|", lineterminator="\n")
        for i, line in enumerate(fin, 1):
            p = line.rstrip("\r\n").split("|")
            if len(p) < 3:
                continue
            try:
                row = h3_grids.compute_h3_row(float(p[1]), float(p[2]), to_cell=to_cell)
            except Exception:
                continue
            w.writerow([p[0]] + [row.get(c, "") or "" for c in H3COLS])
            computed_rows += 1
            if i % 20000 == 0:
                log(f"      {i:,}")

    log(f"      computed {computed_rows:,} rows -> result CSV")
    if computed_rows == 0:
        log("      ERROR: compute wrote 0 rows. Either h3_grids.compute_h3_row is failing "
            "for every row, or the coords CSV parsed to <3 fields. Inspect: " + result)
        _rsz = os.path.getsize(result) if os.path.exists(result) else -1
        log(f"      result CSV size: {_rsz} bytes (kept for inspection; not deleting temps)")
        return
    log("[3/4] bcp load -> stg.dv_well_h3_stage …")
    coldefs = ", ".join([f"[{KEY}] NVARCHAR(80)"] + [f"[{c}] NVARCHAR(20)" for c in H3COLS])
    with eng.begin() as c:
        # drop any stale staging table from a previous failed run (silent-hole guard)
        c.execute(text(f"IF OBJECT_ID('{STG}.dv_well_h3_stage') IS NOT NULL "
                       f"DROP TABLE {STG}.dv_well_h3_stage"))
        c.execute(text(f"CREATE TABLE {STG}.dv_well_h3_stage ({coldefs})"))
    _errf = os.path.join(tempfile.gettempdir(), "h3_load_err.txt")
    loaded_rows = bcp([f"{STG}.dv_well_h3_stage", "in", result, "-c", "-t|", "-r", "0x0a",
         "-C", "65001", "-T", f"-S{SERVER}", f"-d{DATABASE}", "-q", "-e", _errf])
    log(f"      staged {loaded_rows:,} rows")
    if not loaded_rows:
        log("      WARNING: 0 rows loaded into staging.")
        try:
            if os.path.exists(_errf) and os.path.getsize(_errf):
                log("      --- BCP error file (first 600 chars) ---")
                log(open(_errf, encoding="utf-8", errors="replace").read()[:600])
        except Exception as _e:
            log(f"      (couldn't read error file: {_e})")
        log(f"      result CSV KEPT for inspection: {result}")
        log(f"      first 3 result lines:")
        try:
            with open(result, encoding="utf-8", errors="replace") as _rf:
                for _i, _ln in enumerate(_rf):
                    if _i >= 3: break
                    log(f"        {_ln.rstrip()!r}")
        except Exception as _e:
            log(f"        (couldn't read result: {_e})")
        return  # stop before deleting temps so you can inspect

    log("[4/4] UPDATE dv_well FROM stage …")
    set_clause = ", ".join(f"t.[{c}] = s.[{c}]" for c in H3COLS)
    with eng.begin() as c:
        r = c.execute(text(f"UPDATE t SET {set_clause} FROM {SCHEMA}.{TABLE} t "
                           f"JOIN {STG}.dv_well_h3_stage s ON t.[{KEY}] = s.[{KEY}]"))
        updated = r.rowcount
        c.execute(text(f"DROP TABLE {STG}.dv_well_h3_stage"))
    for f in (coords, result):
        try: os.remove(f)
        except OSError: pass
    log(f"done — {updated:,} rows updated in {time.time()-t0:.0f}s")

    if "--grid" in sys.argv:
        _write_grids()


if __name__ == "__main__":
    main()
