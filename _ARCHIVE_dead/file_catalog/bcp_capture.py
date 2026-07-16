r"""
bcp_capture.py — high-throughput LAS capture: parallel-parse the files (workers
return row dicts, no DB write), then bulk-load every cat_* table via BCP
(TABLOCK) + a typed INSERT..SELECT. Produces the SAME rows as worker_core._do_las
+ catalog_capture.capture(), just through one bulk stream instead of per-file
executemany, so promote governance is unchanged.

Measured: ~40 files/sec, ~6.6x the executemany path (~3 min capture for 7,326).

  py bcp_capture.py --src "C:\...\good" --n 300 --workers 6   # benchmark on synthetic UWIs
  py bcp_capture.py --cleanup                                  # remove 15999 test rows

Pipeline use:  from bcp_capture import run_bcp_capture
               run_bcp_capture(engine_url, recs, workers=6, log=print)
  where recs = [{"FILE_PATH":..,"MATCHED_UWI":..,"INVENTORY_ID":..}, ...]
"""
import os, sys, csv, time, subprocess, tempfile, argparse
from datetime import datetime
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor

DEV_CONN = (r"DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost\SQLEXPRESS;"
            r"DATABASE=DataView_Demo;Trusted_Connection=yes;Encrypt=no")
CAT_SCHEMA = "file_catalog"
TABLES = ("cat_well", "cat_well_log", "cat_well_log_curve")

# ── parse (faithful to worker_core._do_las, but returns dicts) ───────────────
def _clean(v):
    if v is None:
        return ""
    return (str(v).replace("\t", " ").replace("|", " ")
            .replace("\r", " ").replace("\n", " ").strip())

def _fnum(v):
    try:
        f = float(str(v).strip())
        return f
    except (TypeError, ValueError):
        return None

def _coord(v):
    f = _fnum(v)
    if f is None or f == 0:
        return None
    return f

def parse_las_rows(arg):
    """Worker: (fpath, uwi, inv[, force]) -> dict of {table: [row dicts]}. No DB
    access. force=True keeps the passed UWI (benchmark); else resolves header UWI."""
    fpath, uwi, inv, *_rest = arg
    _force = bool(_rest[0]) if _rest else False
    try:
        import lasio
        las = lasio.read(fpath, ignore_data=True)
    except Exception:
        return {"cat_well": [], "cat_well_log": [], "cat_well_log_curve": []}

    def wv(*keys):
        for k in keys:
            try:
                v = str(las.well[k].value).strip()
                if v:
                    return v
            except Exception:
                pass
        return None

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d_start = _fnum(wv("STRT", "START"))
    d_stop  = _fnum(wv("STOP"))
    d_uom   = None
    try:
        for c in las.curves:
            if (c.mnemonic or "").upper() in ("DEPT", "DEPTH", "MD"):
                d_uom = (c.unit or "").strip() or None
                break
        if d_uom is None and las.curves:
            d_uom = (las.curves[0].unit or "").strip() or None
    except Exception:
        pass
    # resolve identity like _do_las: valid header UWI wins, else the passed
    # crosswalk MATCHED_UWI; if neither is a valid API, skip (no rows).
    def _d14(v):
        d = "".join(c for c in str(v or "") if c.isdigit())
        return (d + "00000000000000")[:14] if len(d) >= 10 else None
    def _valid(u):
        if not u:
            return False
        d = "".join(c for c in str(u) if c.isdigit())
        if len(d) < 10:
            return False
        try:
            return 1 <= int(d[:2]) <= 62
        except ValueError:
            return False
    if _force:
        uwi = _d14(uwi) or uwi
    else:
        _hdr = _d14(wv("UWI", "API", "APINUM", "API_NUMBER", "APINO", "APIN"))
        _pas = _d14(uwi)
        uwi = _hdr if (_hdr and _valid(_hdr)) else (_pas if (_pas and _valid(_pas)) else None)
    if not uwi:
        return {"cat_well": [], "cat_well_log": [], "cat_well_log_curve": []}

    logid = wv("LOG_ID", "LOGID") or (f"{uwi}-LAS" if uwi else None)

    well = []
    log  = []
    if uwi:
        well.append({
            "uwi": uwi, "well_name": wv("WELL") or uwi,
            "operator_name": wv("COMP", "PROV"), "field_name": wv("FLD", "FIELD"),
            "province_state": wv("STAT", "STATE"), "county": wv("CNTY", "COUNTY"),
            "country": wv("CTRY", "COUNTRY"),
            "surface_latitude": _coord(wv("LATI", "LAT")),
            "surface_longitude": _coord(wv("LONG", "LON")),
            "final_td": _fnum(wv("STOP", "TD")), "active_ind": "Y",
            "row_created_by": "DataWrangler", "row_created_date": now,
        })
        log.append({
            "uwi": uwi, "log_id": logid, "log_type": wv("TYPE", "LOGTYPE"),
            "run_num": wv("RUN", "RUN_NUMBER"), "top_depth": d_start,
            "base_depth": d_stop, "depth_ouom": d_uom, "null_value": _fnum(wv("NULL")),
            "file_path": fpath, "file_format": "LAS", "active_ind": "Y",
            "row_created_by": "DataWrangler", "row_created_date": now,
        })
    curves = []
    try:
        for c in las.curves:
            mnem = (getattr(c, "mnemonic", "") or "").strip()
            if not mnem:
                continue
            curves.append({
                "uwi": uwi, "log_id": logid, "curve_id": mnem[:40], "mnemonic": mnem,
                "curve_description": _clean(getattr(c, "descr", "")) or None,
                "curve_unit": (getattr(c, "unit", "") or "").strip() or None,
                "top_depth": d_start, "base_depth": d_stop, "depth_ouom": d_uom,
                "null_value": _fnum(wv("NULL")), "active_ind": "Y",
                "row_created_by": "DataWrangler", "row_created_date": now,
            })
    except Exception:
        pass
    # FILE_WELL_HEADER row (the extract output) — same shape as _write_well_header,
    # keyed on the deterministic WELL_HEADER_ID so a re-process updates in place.
    import uuid as _uuid
    _hid = _uuid.uuid5(_uuid.NAMESPACE_URL, str(inv) if inv is not None else "_nofid_").hex.upper()
    fwh = [{
        "WELL_HEADER_ID": _hid, "INVENTORY_ID": inv,
        "UWI": uwi, "UWI14": uwi,
        "WELL_NAME": wv("WELL") or uwi, "OPERATOR": wv("COMP", "PROV"),
        "WELL_FIELD": wv("FLD", "FIELD"), "STATE": wv("STAT", "STATE"),
        "COUNTY": wv("CNTY", "COUNTY"),
        "LATITUDE": _coord(wv("LATI", "LAT")), "LONGITUDE": _coord(wv("LONG", "LON")),
        "TOTAL_DEPTH": _fnum(wv("STOP", "TD")), "REPORT_TYPE": "WELL_LOG",
        "EXTRACTED_BY": "DataWrangler",
    }]

    # stamp provenance on every cat_* row
    for t, rows in (("cat_well", well), ("cat_well_log", log), ("cat_well_log_curve", curves)):
        for r in rows:
            r["INVENTORY_ID"] = inv
            r["SOURCE_PATH"] = fpath
            r["source"] = "LAS_HEADER" if t == "cat_well" else "LAS"
    return {"cat_well": well, "cat_well_log": log,
            "cat_well_log_curve": curves, "FILE_WELL_HEADER": fwh}

# ── bulk load (schema-adaptive: wide staging -> BCP -> typed INSERT..SELECT) ──
def _columns(cur, table):
    """[(name, type, is_identity)] in ordinal order for file_catalog.<table>."""
    oid = cur.execute("SELECT OBJECT_ID(?)", f"{CAT_SCHEMA}.{table}").fetchone()[0]
    return [(r.name, r.typ, bool(r.ident)) for r in cur.execute("""
        SELECT c.name, ty.name typ, c.is_identity ident
        FROM sys.columns c JOIN sys.types ty ON ty.user_type_id=c.user_type_id
        WHERE c.object_id=? ORDER BY c.column_id""", oid).fetchall()]

def _cast(col, typ):
    t = typ.lower()
    src = f"NULLIF([{col}], '')"
    if t in ("numeric", "decimal", "float", "real", "money", "smallmoney",
             "int", "bigint", "smallint", "tinyint"):
        return f"TRY_CONVERT(float, {src})"
    if t in ("datetime2", "datetime", "date", "smalldatetime"):
        return f"TRY_CONVERT(datetime2, {src})"
    if t == "bit":
        return f"TRY_CONVERT(bit, {src})"
    if t in ("binary", "varbinary"):
        return "NULL"
    return f"[{col}]"          # (n)varchar/char — keep as-is

def _load_table(cur, table, rows, log, upsert_key=None):
    """Bulk-load rows (list of dicts) into file_catalog.<table>. Returns count."""
    if not rows:
        return 0
    cols = _columns(cur, table)
    ins = [(n, ty) for (n, ty, ident) in cols if not ident]     # skip IDENTITY
    names = [n for (n, _ty) in ins]
    lower = {n.lower(): n for n in names}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def val(row, name):
        # governance / audit defaults the parse rows don't carry
        if name == "PROMOTED":
            return "0"
        if name in ("CAPTURED_AT", "EXTRACTED_DATE"):
            return now
        if name == "EXTRACTED_BY":
            return row.get("EXTRACTED_BY") or "DataWrangler"
        v = row.get(name, row.get(name.lower(), row.get(name.upper())))
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return _clean(v)

    # wide varchar staging clone (no identity, all nvarchar(max))
    stg = f"stg.bcpcap_{table}"
    cur.execute("IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg')")
    cur.execute(f"IF OBJECT_ID('{stg}') IS NOT NULL DROP TABLE {stg}")
    cur.execute("CREATE TABLE " + stg + " (" +
                ", ".join(f"[{n}] nvarchar(max)" for n in names) + ")")

    _bd = r"C:\bcp_tmp"
    os.makedirs(_bd, exist_ok=True)
    tmp = os.path.join(_bd, f"bcpcap_{table}.csv")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        for r in rows:
            w.writerow([val(r, n) for n in names])

    # server-side BULK INSERT (no external bcp process; can't hang on a prompt).
    # tab-delimited, LF rows, minimally logged with TABLOCK.
    try:
        # BULK INSERT can't bind the path as a parameter — inline it (our own temp file)
        cur.execute(
            "BULK INSERT " + stg + " FROM '" + tmp.replace("'", "''") + "' "
            "WITH (FIELDTERMINATOR='\\t', ROWTERMINATOR='0x0a', "
            "TABLOCK, BATCHSIZE=20000, CODEPAGE='65001')")
    except Exception as e:
        log(f"[bulk] {table} FAILED: {str(e)[:250]}")
        cur.execute(f"IF OBJECT_ID('{stg}') IS NOT NULL DROP TABLE {stg}")
        return 0

    select = ", ".join(_cast(n, ty) for (n, ty) in ins)
    collist = ", ".join(f"[{n}]" for n in names)
    if upsert_key:
        # delete-then-insert = set-based upsert on the key (re-process updates in place)
        cur.execute(f"DELETE t FROM {CAT_SCHEMA}.{table} t "
                    f"JOIN {stg} s ON t.[{upsert_key}] = s.[{upsert_key}]")
    cur.execute(f"INSERT INTO {CAT_SCHEMA}.{table} ({collist}) SELECT {select} FROM {stg}")
    n = cur.rowcount
    cur.execute(f"DROP TABLE {stg}")
    return n

def run_bcp_capture(recs, conn_str=None, workers=6, log=print, force_uwi=False):
    """Parallel-parse recs -> bulk-load cat_* via BULK INSERT. recs need FILE_PATH,
    MATCHED_UWI, INVENTORY_ID. conn_str is a pyodbc ODBC connection string (falls
    back to the dev instance). Returns {table: rows_inserted}."""
    import pyodbc
    conn_str = conn_str or DEV_CONN
    args = [(r.get("FILE_PATH") or r.get("path"),
             (r.get("MATCHED_UWI") or r.get("uwi") or "").strip() or None,
             r.get("INVENTORY_ID") or r.get("inventory_id"), force_uwi) for r in recs]
    args = [a for a in args if a[0]]

    t0 = time.time()
    _all_tabs = list(TABLES) + ["FILE_WELL_HEADER"]
    buckets = {t: [] for t in _all_tabs}
    _n = len(args)
    _step = max(50, _n // 20)          # ~20 updates over the batch, min every 50
    _done = 0
    # nested-pool safe: if we're already inside a spawned child process (the
    # pipeline's detached multi-core runner calls this), spawning a nested
    # ProcessPoolExecutor raises the Windows "start a new process before the
    # current process has finished its bootstrapping" error and the parse yields
    # nothing. Detect that and parse with THREADS instead (lasio I/O releases the
    # GIL, so we still get parallelism) — or serially if workers<=1.
    import multiprocessing as _mp  # nested-pool safe
    _in_child = _mp.parent_process() is not None
    _use_threads = _in_child or workers <= 1

    def _drain(_iterable):
        _d = 0
        for out in _iterable:
            for t in _all_tabs:
                buckets[t].extend(out.get(t, []))
            _d += 1
            if _d % _step == 0 or _d == _n:
                log(f"[bcp-capture] parsing {_d:,}/{_n:,} files… "
                    f"({time.time()-t0:.0f}s, OneDrive files hydrate on first read)")

    if _use_threads:
        from concurrent.futures import ThreadPoolExecutor
        _mode = "threads (nested-process-safe)" if _in_child else "threads"
        log(f"[bcp-capture] parse pool: {_mode}, {max(1, workers)} worker(s)")
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            _drain(ex.map(parse_las_rows, args))
    else:
        try:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                _drain(ex.map(parse_las_rows, args))
        except Exception as _pe:
            # last-resort fallback: the process pool failed (e.g. bootstrapping);
            # parse serially so we still capture rather than writing nothing.
            log(f"[bcp-capture] process pool failed ({str(_pe)[:80]}); "
                f"parsing serially")
            buckets.clear()
            for t in _all_tabs:
                buckets[t] = []
            _drain(map(parse_las_rows, args))
    t_parse = time.time() - t0
    log(f"[bcp-capture] parsed {sum(len(v) for v in buckets.values()):,} rows "
        f"from {len(args):,} files in {t_parse:.1f}s")

    cn = pyodbc.connect(conn_str)
    cn.autocommit = False
    cur = cn.cursor()
    cur.execute("SET LOCK_TIMEOUT 15000")   # 15s: error instead of hang on a blocker
    out = {}
    t1 = time.time()
    try:
        for t in TABLES:
            log(f"[bcp-capture] loading {t} ({len(buckets[t]):,} rows) …")
            out[t] = _load_table(cur, t, buckets[t], log)
        # FILE_WELL_HEADER = the extract output, upserted on WELL_HEADER_ID
        log(f"[bcp-capture] loading FILE_WELL_HEADER ({len(buckets['FILE_WELL_HEADER']):,} rows) …")
        out["FILE_WELL_HEADER"] = _load_table(cur, "FILE_WELL_HEADER",
                                              buckets["FILE_WELL_HEADER"], log,
                                              upsert_key="WELL_HEADER_ID")
        cn.commit()
    except Exception as e:
        cn.rollback()
        log(f"[bcp-capture] load error, rolled back: {e}")
        raise
    finally:
        cn.close()
    log(f"[bcp-capture] loaded " + " · ".join(f"{t}={out[t]:,}" for t in _all_tabs) +
        f" in {time.time()-t1:.1f}s (parse {t_parse:.1f}s)")
    return out



# ── SEG-Y fast-path ──────────────────────────────────────────────────────────
# Header-only capture: segy_header.read_segy_header reads the 3600-byte file
# header + up to ~50 trace headers for the CDP bbox — never the trace samples —
# so even multi-GB SEG-Y files parse in ms. Produces one FILE_SEIS_HEADER row per
# file, bulk-loaded in a single INSERT (same _load_table path LAS uses).
import uuid as _uuid_segy

SEIS_TABLE = "FILE_SEIS_HEADER"

def parse_segy_rows(arg):
    """Worker: (fpath, inv) -> {'FILE_SEIS_HEADER': [row]} or empty on failure.
    No DB access. Uses the dependency-free header reader (header-only)."""
    fpath, inv, *_ = arg
    try:
        # import inside the worker (spawn-safe on Windows)
        try:
            from dataview.file_catalog.segy_header import read_segy_header
        except Exception:
            from dataview.file_catalog.segy_header import read_segy_header
    except Exception:
        return {SEIS_TABLE: []}
    try:
        h = read_segy_header(fpath, max_geom_traces=50)   # fewer seeks: bbox stays representative
    except Exception:
        return {SEIS_TABLE: []}
    if not h or not h.get("ok"):
        return {SEIS_TABLE: []}

    # survey name: from the textual header (segy_header leaves it in notes/text);
    # fall back to the filename stem so a survey always has a name for promote.
    import os as _os, re as _re
    survey = ""
    txt = h.get("textual_header") or ""
    m = _re.search(r"(?:LINE|SURVEY|PROJECT|NAME)[:\s]+([^\r\n]+?)\s*$",
                   txt, _re.IGNORECASE | _re.MULTILINE)
    if m:
        survey = m.group(1).strip()[:255]
    if not survey:
        survey = _os.path.splitext(_os.path.basename(fpath))[0][:255]

    def _rng(pair, i):
        return pair[i] if (pair and pair[i] is not None) else None

    ilr = h.get("inline_range");    xlr = h.get("crossline_range")
    cxr = h.get("cdp_x_range");     cyr = h.get("cdp_y_range")
    # Survey outline: convex hull of the sampled CDP points (same as the pool
    # extract path). For 2D it's the line corridor; for 3D the survey polygon.
    # cdp_points already read for the bbox, so this adds negligible time.
    _outline = None
    try:
        _pts = [(x, y) for (x, y) in (h.get("cdp_points") or [])
                if x is not None and y is not None and (x != 0 or y != 0)]
        if len(_pts) >= 3:
            from shapely.geometry import MultiPoint
            _hull = MultiPoint(_pts).convex_hull
            if not _hull.is_empty:
                _outline = _hull.wkt
    except Exception:
        _outline = None
    hid = _uuid_segy.uuid5(_uuid_segy.NAMESPACE_URL,
                           str(inv) if inv is not None else fpath).hex.upper()
    row = {
        "SEIS_HEADER_ID": hid,
        "INVENTORY_ID":   inv,
        "SURVEY_NAME":    survey,
        "SEIS_SET_TYPE":  h.get("dims") or None,
        "SAMPLE_INTERVAL": h.get("sample_interval_us"),
        "TRACE_COUNT":    h.get("n_traces"),
        "IL_MIN": _rng(ilr, 0), "IL_MAX": _rng(ilr, 1),
        "XL_MIN": _rng(xlr, 0), "XL_MAX": _rng(xlr, 1),
        # CDP X/Y bbox -> the LON/LAT bbox columns (best-effort; these are survey
        # coords, not necessarily WGS84 — promote/geo can reproject if EPSG known)
        "BBOX_MIN_LON": _rng(cxr, 0), "BBOX_MAX_LON": _rng(cxr, 1),
        "BBOX_MIN_LAT": _rng(cyr, 0), "BBOX_MAX_LAT": _rng(cyr, 1),
        "SURVEY_OUTLINE": _outline,
        "EXTRACTED_BY": "DataWrangler",
    }
    return {SEIS_TABLE: [row]}


def run_bcp_capture_segy(recs, conn_str=None, workers=6, log=print):
    """Parallel-parse SEG-Y headers -> one BULK INSERT into FILE_SEIS_HEADER.
    recs = [{"FILE_PATH":.., "INVENTORY_ID":..}, ...]. Mirrors run_bcp_capture."""
    import pyodbc
    conn_str = conn_str or DEV_CONN
    args = [(r.get("FILE_PATH") or r.get("file_path"),
             r.get("INVENTORY_ID") or r.get("inventory_id")) for r in recs]
    rows = []
    t0 = time.time()
    _n = len(args)
    _step = max(1, _n // 8)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for _i, out in enumerate(ex.map(parse_segy_rows, args), 1):
            rows.extend(out.get(SEIS_TABLE, []))
            if _i % _step == 0 or _i == _n:
                log(f"[bcp-segy] parsing {_i:,}/{_n:,} headers… ({time.time()-t0:.0f}s)")
    t_parse = time.time() - t0
    log(f"[bcp-segy] parsed {len(rows):,} header(s) from {_n:,} file(s) in {t_parse:.1f}s")
    if not rows:
        return {SEIS_TABLE: 0}
    cn = pyodbc.connect(conn_str); cn.autocommit = False
    cur = cn.cursor(); cur.execute("SET LOCK_TIMEOUT 15000")
    try:
        n = _load_table(cur, SEIS_TABLE, rows, log, upsert_key="SEIS_HEADER_ID")
        cn.commit()
    except Exception as e:
        cn.rollback(); log(f"[bcp-segy] load error, rolled back: {e}"); raise
    finally:
        cn.close()
    log(f"[bcp-segy] loaded FILE_SEIS_HEADER={n:,} in {time.time()-t0-t_parse:.1f}s")
    return {SEIS_TABLE: n}


# ── standalone benchmark / cleanup ───────────────────────────────────────────
def _cleanup():
    import pyodbc
    cn = pyodbc.connect(DEV_CONN, autocommit=True)
    cur = cn.cursor()
    n = 0
    for t in TABLES:
        try:
            n += cur.execute(f"DELETE FROM {CAT_SCHEMA}.{t} WHERE uwi LIKE '15999%'").rowcount or 0
        except Exception as e:
            print(f"  {t}: {e}")
    print(f"cleaned {n} synthetic bench row(s)")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"C:\Users\perry\OneDrive\Documents\KSGS\LAS Files\2020\_triage\good")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--target", type=int, default=7326)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--cleanup", action="store_true")
    a = ap.parse_args()
    if a.cleanup:
        _cleanup(); return

    files = [str(p) for p in Path(a.src).rglob("*.las")][:a.n]
    if not files:
        print("no files under", a.src); return
    recs = [{"FILE_PATH": fp, "MATCHED_UWI": "15999" + f"{i:05d}" + "0000",
             "INVENTORY_ID": None} for i, fp in enumerate(files, 1)]

    t0 = time.time()
    out = run_bcp_capture(recs, workers=a.workers, log=print, force_uwi=True)
    dt = time.time() - t0
    fps = len(files) / dt if dt else 0
    print(f"\n=== {len(files)} files end-to-end capture in {dt:.1f}s → {fps:.1f} files/sec "
          f"({fps/6.14:.1f}x executemany) ===")
    print(f"extrapolate {a.target:,}: ~{a.target/fps/60:.1f} min capture "
          f"(+ promote ~3-5 min)")
    print("cleanup: py bcp_capture.py --cleanup")

if __name__ == "__main__":
    main()
