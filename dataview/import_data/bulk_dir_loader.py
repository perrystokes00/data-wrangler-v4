"""
bulk_dir_loader.py — set-based directory loader (BCP staging pipeline).

PHASE 1 (this file): scan a directory of well files, extract + fingerprint each, re-emit
each as a safe-delimited file (correct quote handling — no naive comma splitting),
auto-create an all-varchar staging table per (target, shape), and BCP-load it onto
the server. Nothing is promoted here; staging only.

Later phases build on the stg.* tables: (2) batch Match & Map, (3) set-based FK
analysis, (4) Add/Remap/Null resolution grid, (5) topo-ordered set-based promote.

Reuses page_dir_loader for the catalog/match/fingerprint logic so this pipeline and
the per-table loader agree on tables, fingerprints, and the synonym store.
"""
import os, csv, subprocess, tempfile, urllib.parse

try:
    import streamlit as st
except Exception:                       # allow headless import for tests
    st = None

try:
    from dataview.import_data import page_dir_loader as pdl
except Exception:
    import page_dir_loader as pdl

try:
    from dataview.import_data import las_header_loader as _las
except Exception:
    try:
        import las_header_loader as _las
    except Exception:
        _las = None

def _opt_import(mod):
    try:
        return __import__(f"dataview.import_data.{mod}", fromlist=[mod])
    except Exception:
        try:
            return __import__(mod)
        except Exception:
            return None

_dlis = _opt_import("dlis_header_loader")
_lis = _opt_import("lis_header_loader")
_witsml = _opt_import("witsml_header_loader")
_pdf = _opt_import("pdf_document_loader")
_docx = _opt_import("docx_document_loader")
_pdf_review = _opt_import("pdf_field_review")
_gate = _opt_import("file_gate")          # content-hash file catalog / re-extract gate
_diag = _opt_import("load_diagnostics")   # trap → explain → advise on SQL failures


def _render_diag(exc, table=None, tb=None, sql=None):
    """Call load_diagnostics.render() across versions. Older copies have no `tb` parameter,
    and a TypeError here would replace the error we're trying to EXPLAIN with a worse one —
    the reporting path must never be the thing that breaks."""
    if _diag is None:
        return False
    try:
        import inspect
        params = inspect.signature(_diag.render).parameters
    except Exception:
        params = {}
    try:
        if "tb" in params:
            _diag.render(exc, table=table, tb=tb)
        else:
            _diag.render(exc, table=table)
        return True
    except Exception:
        return False                      # caller falls back to the plain st.error
_qa = _opt_import("staging_qa")           # data-quality report over staging

# which extractor owns which extension, and what it needs — so a scan that finds files
# it cannot extract SAYS SO instead of silently skipping them.
def _extractor_status():
    return [
        ("LAS",    [".las"],                 _las,     "las_header_loader",   "lasio"),
        ("DLIS",   [".dlis"],                _dlis,    "dlis_header_loader",  "dlisio"),
        ("LIS",    [".lis"],                 _lis,     "lis_header_loader",   "—"),
        ("WITSML", [".xml", ".wml"],         _witsml,  "witsml_header_loader", "—"),
        ("PDF",    [".pdf"],                 _pdf,     "pdf_document_loader", "pdfplumber"),
        ("Word",   [".docx", ".doc", ".odt"], _docx,   "docx_document_loader", "python-docx"),
    ]


def _missing_extractors(directory, recursive):
    """[(label, n_files, module, dep)] for formats present on disk with no working extractor."""
    out = []
    for label, exts, mod, modname, dep in _extractor_status():
        if mod is None:
            files = _glob_ext(directory, exts, recursive)
            files = [f for f in files if not os.path.basename(f).startswith("~$")]
            if files:
                out.append((label, len(files), modname, dep))
    return out

STG_SCHEMA = "stg"
FS = "\x01"          # field separator written into the safe file
FS_BCP = "0x01"      # tell bcp the field terminator in hex (unambiguous)
RT = "\r"            # row terminator written into the safe file (bare CR)
BCP_RT = "0x0d"      # tell bcp EXACTLY CR (hex)


# ── connection ────────────────────────────────────────────────────────────────
def _pick_driver():
    import pyodbc
    have = set(pyodbc.drivers())
    for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "SQL Server"):
        if d in have:
            return d
    return "SQL Server"

def make_engine(server, database):
    from sqlalchemy import create_engine
    drv = _pick_driver()
    cs = f"DRIVER={{{drv}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;"
    if "18" in drv:
        cs += "Encrypt=no;TrustServerCertificate=yes;"
    return create_engine("mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(cs),
                         pool_pre_ping=True)

def get_engine(server, database):
    """One engine per (server,db) per session — avoids leaking a new connection pool on
    every Streamlit rerun. Disposes the old one if the target changed."""
    if st is None:
        return make_engine(server, database)
    ss = st.session_state
    key = (server, database)
    if ss.get("_bdl_eng_key") != key or "_bdl_eng" not in ss:
        old = ss.get("_bdl_eng")
        if old is not None:
            try: old.dispose()
            except Exception: pass
        ss["_bdl_eng"] = make_engine(server, database)
        ss["_bdl_eng_key"] = key
    return ss["_bdl_eng"]


# ── safe-delimited re-emit ────────────────────────────────────────────────────
_repair = _opt_import("staging_repair")


def _clean(v):
    """Strip anything that would corrupt the delimited stream, plus encoding damage that is
    never legitimate in this data: NULL bytes, control characters, smart quotes, nbsp.
    Type-dependent repairs happen later (see staging_repair) — here we only undo damage that
    is wrong regardless of what column the value lands in."""
    if v is None:
        return ""
    if _repair is not None:
        try:
            v = _repair.clean_text(v)
        except Exception:
            pass
    return v.replace(FS, "").replace(RT, " ").replace("\r", " ").replace("\n", " ")

def build_safe_file(csv_path, out_path, src_name, out_cols):
    """Emit FS-delimited rows with fields in out_cols order (== the staging table's column
    order), remapped from wherever each column sits in the CSV header. Prefixes _row_id
    (file order) and _src_file. Written UTF-8. Returns n_rows.

    Field order must match the table's column order because BCP loads positionally."""
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as fh, \
         open(out_path, "w", encoding="utf-8-sig", newline="") as out:
        rd = csv.reader(fh)
        header = next(rd, None)
        if not header:
            return 0
        idx = {}
        for j, h in enumerate(header):
            idx.setdefault(pdl._norm(h), j)          # normalized header -> position (first wins)

        # A safe file whose fields are all empty loads as rows of NULLs — which then fails
        # far away with "cannot insert NULL into 'uwi'", or worse, loads silently. Catch the
        # header/column mismatch HERE, where it's obvious what's wrong.
        hit = [c for c in out_cols if idx.get(pdl._norm(c), idx.get(c)) is not None]
        if out_cols and not hit:
            raise ValueError(
                f"{os.path.basename(csv_path)}: none of the {len(out_cols)} staging column(s) "
                f"match the CSV header. staging={list(out_cols)[:6]}… csv={header[:6]}… "
                f"— the safe file would be all-empty. Re-scan so the staging shape matches "
                f"the current extractor output.")
        n = 0
        for i, row in enumerate(rd, start=1):
            vals = [str(i), src_name]
            for c in out_cols:
                # look up NORMALIZED both sides — the index is keyed by _norm(header), so a
                # raw lookup silently misses whenever the CSV's casing/punctuation differs
                # from out_cols (e.g. 'uwi' vs 'UWI'), and every field comes out empty.
                j = idx.get(pdl._norm(c), idx.get(c))
                vals.append(_clean(row[j]) if (j is not None and j < len(row)) else "")
            out.write(FS.join(vals) + RT)
            n += 1
    return n


# ── staging tables ────────────────────────────────────────────────────────────
def load_catalog_live(engine, schema="dataview"):
    """Build the catalog dict (fk_constraints / table_cols / table_kind) directly from
    the live server — no JSON file, always current. Same shape as the on-disk catalog,
    so pdl.load_catalog's rich-shape consumers work unchanged."""
    import pandas as pd
    from sqlalchemy import text
    cols = pd.read_sql(text(
        "SELECT TABLE_NAME n, COLUMN_NAME c FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA=:s ORDER BY TABLE_NAME, ORDINAL_POSITION"),
        engine, params={"s": schema})
    fks = pd.read_sql(text(
        "SELECT fk.name fk, ct.name child, cc.name child_col, pt.name parent, "
        "       fkc.constraint_column_id ord "
        "FROM sys.foreign_keys fk "
        "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id=fk.object_id "
        "JOIN sys.tables ct ON ct.object_id=fk.parent_object_id "
        "JOIN sys.schemas s ON s.schema_id=ct.schema_id "
        "JOIN sys.columns cc ON cc.object_id=fkc.parent_object_id AND cc.column_id=fkc.parent_column_id "
        "JOIN sys.tables pt ON pt.object_id=fk.referenced_object_id "
        "WHERE s.name=:s ORDER BY fk.name, fkc.constraint_column_id"),
        engine, params={"s": schema})

    table_cols = {}
    for r in cols.itertuples(index=False):
        table_cols.setdefault(r.n, []).append(r.c)

    grouped = {}
    for r in fks.itertuples(index=False):
        grouped.setdefault(r.fk, {"child": r.child, "parent": r.parent, "cols": []})["cols"].append(r.child_col)
    fk_constraints = {}
    for name, g in grouped.items():
        fk_constraints.setdefault(g["child"], []).append(
            {"fk_name": name, "child_cols": g["cols"], "parent_table": g["parent"]})

    def kind(t):
        tl = t.lower()
        if tl.startswith("dv_r_"): return "reference"
        if tl in ("dv_business_associate", "dv_field"): return "entity"
        return "data"
    return {"schema": schema, "fk_constraints": fk_constraints, "table_cols": table_cols,
            "table_kind": {t: kind(t) for t in table_cols}}


def _kind_of(table):
    tl = (table or "").lower()
    if tl.startswith("dv_r_"):
        return "reference"
    if tl in ("dv_business_associate", "dv_field"):
        return "entity"
    return "data"


_REF_PREFIXES = {"r", "ref", "dv", "dvr", "lookup", "lu", "code", "codes", "tbl"}

def _norm_tokens(name):
    """Filename/table stem → alnum tokens, minus leading generic prefixes, joined."""
    import re
    toks = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]
    while len(toks) > 1 and toks[0] in _REF_PREFIXES:
        toks = toks[1:]
    return "".join(toks)

def _name_sim(filename, table):
    """Name similarity between a filename and a table: 1.0 exact stem, 0.6 unique suffix, else 0."""
    import os
    fkey = _norm_tokens(os.path.splitext(os.path.basename(filename))[0])
    tk = _norm_tokens(table)
    if not fkey or not tk:
        return 0.0
    if tk == fkey:
        return 1.0
    if tk.endswith(fkey) or fkey.endswith(tk):
        return 0.6
    return 0.0

_AUDIT_COLS = {"active_ind", "row_created_by", "row_created_date", "row_changed_by",
               "row_changed_date", "row_effective_date", "row_expiry_date", "_row_id", "_src_file"}

def _col_overlap(file_cols, table_cols):
    """Fraction of the file's MEANINGFUL columns that exist in the table. Audit columns
    (active_ind, row_created_*, …) are excluded — they're common to every table and aren't
    evidence for any specific one."""
    f = {c.lower() for c in file_cols} - _AUDIT_COLS
    t = {c.lower() for c in table_cols} - _AUDIT_COLS
    if not f:
        return 0.0
    return len(f & t) / len(f)

def _match_reference_by_name(filename, ref_tables, file_cols=None, table_cols_map=None):
    """Match a filename to a dv_r_* table using name AND columns combined. Returns
    (table, combined_score) or (None, 0). A name hit only wins if the columns also agree
    (real overlap) — a file named like a reference but shaped like something else is
    rejected and left for the picker."""
    best, best_score, best_cs = None, 0.0, 0.0
    for t in ref_tables:
        ns = _name_sim(filename, t)
        if ns == 0.0:
            continue
        cs = _col_overlap(file_cols or [], (table_cols_map or {}).get(t, [])) if table_cols_map else None
        combined = 0.5 * ns + 0.5 * (cs if cs is not None else ns)   # both signals contribute
        if combined > best_score:
            best, best_score, best_cs = t, combined, cs
    # accept only when the name hits AND the columns actually overlap (both must match)
    if best and (table_cols_map is None or (best_cs is not None and best_cs >= 0.15)):
        return best, round(best_score, 2)
    return None, 0.0


_SCAN_EXCLUDE_DIRS = {"_las_extract", "_dlis_extract", "_lis_extract", "_witsml_extract", "__pycache__"}

def _iter_dirs(directory, recursive):
    """Yield directory and (if recursive) its subfolders, skipping staging/extract folders."""
    yield directory
    if not recursive:
        return
    for root, dirs, _files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in _SCAN_EXCLUDE_DIRS and not d.startswith(".")]
        for d in dirs:
            yield os.path.join(root, d)


def _glob_ext(directory, exts, recursive):
    """All files under directory matching exts (case-insensitive), flat or recursive."""
    import glob as _glob
    out = []
    for base in _iter_dirs(directory, recursive):
        for ext in exts:
            out += _glob.glob(os.path.join(base, f"*{ext}"))
            out += _glob.glob(os.path.join(base, f"*{ext.upper()}"))
    return sorted(set(out))


def _call_extractor(fn, directory, out_dir, source, files, recursive):
    """Call an extractor's write_staging_csvs across signature variants.

    Older extractors are `write_staging_csvs(directory, out_dir=..., source=...)` and glob
    the directory themselves; newer ones also take `files=[...]` (needed for a recursive
    scan, where the files live in subfolders). Passing `files=` to an older one raises
    TypeError — which used to be swallowed, so the format silently produced nothing.
    Introspect and call whichever form it actually supports."""
    import inspect
    kw = {"out_dir": out_dir, "source": source}
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if "files" in params:
        return fn(directory, files=files, **kw)
    # Extractor globs `directory` itself. That's fine — and identical to passing the list —
    # so long as every file we found actually lives there. Only files in SUBFOLDERS would
    # be missed, so check for those specifically rather than assuming a recursive scan is
    # unsupported (ticking "recursive" on a flat folder must still work).
    base = os.path.abspath(directory)
    nested = [f for f in (files or [])
              if os.path.dirname(os.path.abspath(f)) != base]
    if nested:
        sub = sorted({os.path.dirname(os.path.relpath(f, base)) for f in nested})
        raise TypeError(
            f"{len(nested)} file(s) live in subfolder(s) ({', '.join(sub[:3])}"
            f"{'…' if len(sub) > 3 else ''}), but "
            f"{getattr(fn, '__module__', 'this extractor').split('.')[-1]}"
            f".write_staging_csvs() has no `files` parameter, so it can only read the folder "
            f"it is given. Point the loader at that subfolder, or add `files=[...]` to the "
            f"extractor to enable recursive scans.")
    return fn(directory, **kw)


def profile_directory_live(directory, engine, schema="dataview", bulk_dir=r"C:\Bulk",
                           recursive=False, force=False):
    """profile_directory using the JSON catalog when a path is set (fast), else live
    introspection. Adds filename-based matching for dv_r_* reference tables.

    `force` — re-extract every file even if the catalog says its content is unchanged. Needed
    whenever the EXTRACTOR changed rather than the data: the bytes are identical, so the gate
    would otherwise (correctly) skip them."""
    import json, tempfile
    cj = _catalog_json()
    tmp = None
    if cj is not None:
        cat = cj[3]                                           # raw catalog dict from JSON
        cat_path = st.session_state.get("bdl_cat")
        dirs = list(_iter_dirs(directory, recursive))
        scan = pdl.profile_directory(dirs[0], cat_path)
        for d in dirs[1:]:                                    # merge subfolder scans (recursive)
            sub = pdl.profile_directory(d, cat_path)
            scan["rows"].extend(sub.get("rows", []))
    else:
        cat = load_catalog_live(engine, schema)               # fallback: introspect + temp file
        fd, tmp = tempfile.mkstemp(suffix=".json"); os.close(fd)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cat, fh)
        try:
            dirs = list(_iter_dirs(directory, recursive))
            scan = pdl.profile_directory(dirs[0], tmp)
            for d in dirs[1:]:
                sub = pdl.profile_directory(d, tmp)
                scan["rows"].extend(sub.get("rows", []))
        finally:
            try: os.unlink(tmp)
            except OSError: pass

    ref_tables = [t for t in cat["table_cols"] if t.lower().startswith("dv_r_")]
    tcols_map = cat["table_cols"]

    # ── file gate ────────────────────────────────────────────────────────────────
    # Catalog every candidate file (content hash behind a size+mtime pre-filter) and work out
    # which ones actually need extracting. Unchanged, already-loaded files are skipped — the
    # promote's NOT EXISTS guard would drop their rows anyway, so re-extracting them is pure
    # cost. `force` overrides, for when the extractor changed rather than the data.
    # Everything the loader can read — CSV/Excel included. They reach the scan through
    # pdl.profile_directory rather than _glob_ext, so they were invisible to the catalog:
    # dv_global_file_catalog claimed to track what had been loaded and silently omitted them.
    _ALL_EXTS = [".las", ".dlis", ".lis", ".xml", ".wml", ".pdf", ".docx", ".doc", ".odt",
                 ".csv", ".xlsx", ".xlsm", ".xltx", ".xls"]
    skip_files, gate_dec = set(), {}
    if _gate is not None:
        try:
            cand = [f for f in _glob_ext(directory, _ALL_EXTS, recursive)
                    if not os.path.basename(f).startswith("~$")]
            if cand and not _gate.catalog_exists(engine):
                try:
                    _db = engine.url.database
                except Exception:
                    _db = "this database"
                raise RuntimeError(
                    f"{_gate.CAT_SCHEMA}.{_gate.CAT_TABLE} isn't in {_db}. Files will still "
                    f"load — they just won't be catalogued, or skipped when unchanged. Check "
                    f"CAT_SCHEMA in file_gate.py, or point the loader at the database that "
                    f"holds the catalog.")
            if cand:
                # NB: no `schema` — the file catalog lives in its own schema
                # (catalog.GLOBAL_FILE_CATALOG), not the dataview one.
                gate_dec = _gate.classify(engine, cand, root=directory, force=force)
                # upsert() returns (n, note) — older copies returned a bare int. Don't let a
                # version-skewed pair of files break a scan over a return shape.
                _res = _gate.upsert(engine, gate_dec, root=directory)
                _note = _res[1] if isinstance(_res, (tuple, list)) and len(_res) > 1 else None
                keep = set(_gate.to_extract(gate_dec, force))
                skip_files = {os.path.abspath(p) for p in gate_dec if p not in keep}
                scan["gate"] = {"summary": _gate.summary(gate_dec),
                                "skipped": len(skip_files), "total": len(cand),
                                "forced": bool(force), "note": _note,
                                "ids": {os.path.abspath(p): r["inventory_id"]
                                        for p, r in gate_dec.items()}}
        except Exception as e:
            scan.setdefault("extract_errors", {})["file_gate"] = str(e)

    def _ungated(files):
        """Drop files the gate says are unchanged-and-already-loaded."""
        return [f for f in files if os.path.abspath(f) not in skip_files]

    # CSV/Excel rows arrive from pdl.profile_directory already globbed, so _ungated() can't
    # reach them — filter the profiled rows instead, on each row's own path.
    if skip_files and scan.get("rows"):
        _kept = []
        for _r in scan["rows"]:
            _p = _r.get("path")
            if _p and os.path.abspath(_p) in skip_files:
                scan["gate"]["skipped_rows"] = scan["gate"].get("skipped_rows", 0) + 1
                continue
            _kept.append(_r)
        scan["rows"] = _kept
        scan["n_files"] = len(_kept)

    for r in scan.get("rows", []):
        m, score = _match_reference_by_name(r["file"], ref_tables, r.get("cols", []), tcols_map)
        if m and (not r.get("table") or r["table"].upper() != m.upper()):
            r["table"] = m.upper()                            # name + columns agree → route here
            r["kind"] = "reference"
            r["score"] = score
            r["name_matched"] = True
    # ensure every matched target is in the promote order (references sort first)
    order = list(scan.get("order") or [])
    for r in scan.get("rows", []):
        t = r.get("table")
        if t and t not in order:
            order.insert(0, t)
    scan["order"] = order
    scan["all_tables"] = sorted(t.upper() for t in cat["table_cols"])

    # auto-detect LAS files → two rows (dv_well_log, dv_well_log_curve), staged separately
    import csv as _csv
    las_files = _ungated(_glob_ext(directory, [".las"], recursive))
    if las_files and _las is not None:
        try:
            las_out = os.path.join(bulk_dir, "_las_extract")     # accessible to bcp, unlike os temp
            os.makedirs(las_out, exist_ok=True)
            lp, cp, nl, nc = _call_extractor(_las.write_staging_csvs, directory, las_out,
                                             "LAS", las_files, recursive)
            log_cols = next(_csv.reader(open(lp, encoding="utf-8")))
            curve_cols = next(_csv.reader(open(cp, encoding="utf-8")))
            scan["rows"].append({"file": f"LAS → log  ({nl} files)", "path": lp, "cols": log_cols,
                                 "table": "DV_WELL_LOG", "kind": "data", "score": 1.0,
                                 "las": True, "stg_table": "stg.dv_well_log_las"})
            scan["rows"].append({"file": f"LAS → curve  ({nc} curves)", "path": cp, "cols": curve_cols,
                                 "table": "DV_WELL_LOG_CURVE", "kind": "data", "score": 1.0,
                                 "las": True, "stg_table": "stg.dv_well_log_curve_las"})
            for t in ("DV_WELL_LOG", "DV_WELL_LOG_CURVE"):   # parent (log) BEFORE child (curve)
                if t in scan["order"]:
                    scan["order"].remove(t)
                scan["order"].append(t)
            scan["las_count"] = len(las_files)
        except Exception as e:
            scan["las_error"] = str(e)
            scan.setdefault("extract_errors", {})["las"] = str(e)   # or it never reaches the UI

    # DLIS / LIS → dv_well_log + dv_well_log_curve (like LAS, separate _<fmt> staging)
    def _detect_logfmt(ext, mod, tag):
        files = _ungated(_glob_ext(directory, [ext], recursive))
        if not files or mod is None:
            return
        try:
            out = os.path.join(bulk_dir, f"_{tag}_extract"); os.makedirs(out, exist_ok=True)
            lp, cp, nl, nc = mod.write_staging_csvs(directory, out_dir=out, source=tag.upper())
            log_cols = next(_csv.reader(open(lp, encoding="utf-8")))
            curve_cols = next(_csv.reader(open(cp, encoding="utf-8")))
            scan["rows"].append({"file": f"{tag.upper()} → log  ({nl} files)", "path": lp, "cols": log_cols,
                                 "table": "DV_WELL_LOG", "kind": "data", "score": 1.0,
                                 "extracted": tag, "needs_uwi": True,
                                 "stg_table": f"stg.dv_well_log_{tag}"})
            scan["rows"].append({"file": f"{tag.upper()} → curve  ({nc} curves)", "path": cp, "cols": curve_cols,
                                 "table": "DV_WELL_LOG_CURVE", "kind": "data", "score": 1.0,
                                 "extracted": tag, "needs_uwi": True,
                                 "stg_table": f"stg.dv_well_log_curve_{tag}"})
            for t in ("DV_WELL_LOG", "DV_WELL_LOG_CURVE"):
                if t in scan["order"]:
                    scan["order"].remove(t)
                scan["order"].append(t)
        except Exception as e:
            scan.setdefault("extract_errors", {})[tag] = str(e)

    _detect_logfmt(".dlis", _dlis, "dlis")
    _detect_logfmt(".lis", _lis, "lis")

    # WITSML → multiple targets depending on object type (log/trajectory/mudlog)
    wml_files = _ungated(_glob_ext(directory, [".xml", ".wml"], recursive))
    if wml_files and _witsml is not None:
        try:
            out = os.path.join(bulk_dir, "_witsml_extract"); os.makedirs(out, exist_ok=True)
            written = _witsml.write_staging_csvs(directory, out_dir=out, source="WITSML")
            # kind → (target table, staging table, parent-order hint)
            wmap = {
                "log":       ("DV_WELL_LOG", "stg.dv_well_log_witsml"),
                "curve":     ("DV_WELL_LOG_CURVE", "stg.dv_well_log_curve_witsml"),
                "srvy_hdr":  ("DV_WELL_DIR_SRVY_HDR", "stg.dv_well_dir_srvy_hdr_witsml"),
                "srvy_sta":  ("DV_WELL_DIR_SRVY_STA", "stg.dv_well_dir_srvy_sta_witsml"),
                "formation": ("DV_WELL_FORMATION_TOP", "stg.dv_well_formation_top_witsml"),
            }
            for kind, (path, n) in written.items():
                if kind not in wmap:
                    continue
                target, stg = wmap[kind]
                cols = next(_csv.reader(open(path, encoding="utf-8")))
                scan["rows"].append({"file": f"WITSML {kind}  ({n} rows)", "path": path, "cols": cols,
                                     "table": target, "kind": "data", "score": 1.0,
                                     "extracted": "witsml", "needs_uwi": True, "stg_table": stg})
            # parent-before-child ordering for the WITSML targets present
            for parent, child in (("DV_WELL_LOG", "DV_WELL_LOG_CURVE"),
                                  ("DV_WELL_DIR_SRVY_HDR", "DV_WELL_DIR_SRVY_STA")):
                present = [r["table"] for r in scan["rows"]]
                for t in (parent, child):
                    if t in present:
                        if t in scan["order"]:
                            scan["order"].remove(t)
                        scan["order"].append(t)
        except Exception as e:
            scan.setdefault("extract_errors", {})["witsml"] = str(e)

    # PDF documents → many targets depending on doc type (scout/eow/survey/pressure/welltest/casing/petro)
    pdf_files = _ungated(_glob_ext(directory, [".pdf"], recursive))
    if pdf_files and _pdf is not None:
        try:
            out = os.path.join(bulk_dir, "_pdf_extract"); os.makedirs(out, exist_ok=True)
            written = _call_extractor(_pdf.write_staging_csvs, directory, out, "PDF",
                                      pdf_files, recursive)
            for kind, (path, n) in written.items():
                target = _pdf.TARGET.get(kind)
                if not target:
                    continue
                cols = next(_csv.reader(open(path, encoding="utf-8")))
                needs = "UWI" in [c.upper() for c in cols]
                scan["rows"].append({"file": f"PDF {kind}  ({n} rows)", "path": path, "cols": cols,
                                     "table": target, "kind": "data", "score": 1.0,
                                     "extracted": "pdf", "needs_uwi": needs,
                                     "stg_table": f"stg.{target.lower()}_pdf"})
            # parent-before-child order for the PDF target families present
            present = [r["table"] for r in scan["rows"]]
            pdf_order = ["DV_WELL", "DV_WELL_DIR_SRVY_HDR", "DV_WELL_DIR_SRVY_STA",
                         "DV_WELL_DST", "DV_WELL_DST_PERIOD", "DV_WELL_PETRO_INTERP", "DV_WELL_PETRO_ZONE",
                         "DV_WELL_FORMATION_TOP", "DV_WELL_CASING", "DV_WELL_STIMULATION",
                         "DV_WELL_PRESSURE"]
            for t in pdf_order:
                if t in present:
                    if t in scan["order"]:
                        scan["order"].remove(t)
                    scan["order"].append(t)
        except Exception as e:
            scan.setdefault("extract_errors", {})["pdf"] = str(e)

    # Word documents (final well reports, completion/geological summaries) → well, tops,
    # log + curves, core, survey. Same review → map → promote path as the PDF suite.
    docx_files = _ungated(_glob_ext(directory, [".docx", ".doc", ".odt"], recursive))
    docx_files = [f for f in docx_files if not os.path.basename(f).startswith("~$")]
    if docx_files and _docx is not None:
        try:
            out = os.path.join(bulk_dir, "_docx_extract"); os.makedirs(out, exist_ok=True)
            written = _call_extractor(_docx.write_staging_csvs, directory, out, "DOCX",
                                      docx_files, recursive)
            for kind, (path, n) in written.items():
                target = _docx.TARGET.get(kind)
                if not target:
                    continue
                cols = next(_csv.reader(open(path, encoding="utf-8")))
                needs = "UWI" in [c.upper() for c in cols]
                scan["rows"].append({"file": f"DOCX {kind}  ({n} rows)", "path": path, "cols": cols,
                                     "table": target, "kind": "data", "score": 1.0,
                                     "extracted": "docx", "needs_uwi": needs,
                                     "stg_table": f"stg.{target.lower()}_docx"})
            present = [r["table"] for r in scan["rows"]]
            docx_order = ["DV_WELL", "DV_WELL_FORMATION_TOP", "DV_WELL_LOG", "DV_WELL_LOG_CURVE",
                          "DV_WELL_CORE", "DV_WELL_DIR_SRVY_HDR", "DV_WELL_DIR_SRVY_STA"]
            for t in docx_order:
                if t in present:
                    if t in scan["order"]:
                        scan["order"].remove(t)
                    scan["order"].append(t)
        except Exception as e:
            scan.setdefault("extract_errors", {})["docx"] = str(e)

    # formats present on disk that no working extractor can read → reported, never silent
    scan["missing_extractors"] = _missing_extractors(directory, recursive)
    return scan


def _catalog_json():
    """Load + cache the JSON catalog (pdl.load_catalog shape) from the path in session state.
    Returns (FKC, COLS, KIND, raw_dict) or None if no valid path is set."""
    if st is None:
        return None
    ss = st.session_state
    path = ss.get("bdl_cat")
    if not path or not os.path.exists(path):
        return None
    if ss.get("_cat_key") != path or "_cat_parsed" not in ss:
        import json
        FKC, COLS, KIND, _shape = pdl.load_catalog(path)
        raw = json.load(open(path, encoding="utf-8"))
        ss["_cat_parsed"] = (FKC, COLS, KIND, raw)
        ss["_cat_key"] = path
    return ss["_cat_parsed"]


def _live_catalog_parsed(engine, schema="dataview"):
    """(FKC, COLS, KIND). Reads the JSON catalog when a path is set (fast); otherwise
    introspects the live server (fallback)."""
    cj = _catalog_json()
    if cj is not None:
        return cj[0], cj[1], cj[2]
    # fallback: live introspection
    import json, tempfile
    cat = load_catalog_live(engine, schema)
    fd, p = tempfile.mkstemp(suffix=".json"); os.close(fd)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(cat, fh)
    try:
        FKC, COLS, KIND, _ = pdl.load_catalog(p)
        return FKC, COLS, KIND
    finally:
        try: os.unlink(p)
        except OSError: pass


def _required_missing(engine, table, cmap, functions, schema="dataview"):
    """NOT NULL / no-default columns not covered by the map, the audit stamp, or a
    function rule. Self-contained (queries sys) so it doesn't depend on page_dir_loader."""
    import pandas as pd
    from sqlalchemy import text
    try:
        df = pd.read_sql(text(
            "SELECT c.name n FROM sys.columns c WHERE c.object_id=OBJECT_ID(:t) "
            "AND c.is_nullable=0 AND c.default_object_id=0 AND c.is_identity=0 AND c.is_computed=0"),
            engine, params={"t": f"{schema}.{table.lower()}"})
        req = {str(r.n).lower() for r in df.itertuples()}
    except Exception:
        return []
    stamped = {"row_created_by", "row_created_date", "active_ind"}
    covered = {v.lower() for v in cmap.values()} | stamped \
              | {str(f.get("target", "")).lower() for f in (functions or [])}
    return sorted(req - covered)


# safe wrappers — newer page_dir_loader helpers may be absent on an older deploy
def _syn(engine, table, valid):
    fn = getattr(pdl, "_synonym_lookup", None)
    try: return fn(engine, table, valid) if fn else {}
    except Exception: return {}

def _remember(engine, table, fp, cmap):
    fn = getattr(pdl, "_remember_mapping", None)
    if fn:
        try: fn(engine, table, fp, cmap)
        except Exception: pass

def _remember_skips(engine, table, skip_cols):
    """Persist explicit skip decisions to dv_column_map (mapping_method='SKIP'), and
    deactivate skips no longer chosen. Self-contained."""
    if engine is None:
        return
    from sqlalchemy import text
    tt = table.upper()
    keep = [str(c).upper() for c in skip_cols]
    up = text(
        "MERGE dataview.dv_column_map AS t USING (SELECT :mid AS map_id) s ON t.map_id=s.map_id "
        "WHEN MATCHED THEN UPDATE SET active_ind='Y', confirmed_ind='Y', confirmed_by='DIR_LOADER', "
        "  confirmed_date=SYSUTCDATETIME(), row_changed_by='DIR_LOADER', row_changed_date=SYSUTCDATETIME() "
        "WHEN NOT MATCHED THEN INSERT (map_id, source_file_pattern, source_column, target_table, "
        "  target_column, confidence_score, mapping_method, confirmed_ind, confirmed_by, confirmed_date, "
        "  active_ind, row_created_by, row_created_date, source) "
        "VALUES (:mid,'*',:sc,:tt,'__SKIP__',1.0,'SKIP','Y','DIR_LOADER',SYSUTCDATETIME(),"
        "        'Y','DIR_LOADER',SYSUTCDATETIME(),NULL);")
    try:
        with engine.begin() as cx:
            for c in keep:
                cx.execute(up, {"mid": _fn_map_id(f"SKIP|{tt}|{c}"), "sc": c, "tt": tt})
            if keep:
                inlist = ",".join(f":k{i}" for i in range(len(keep)))
                params = {f"k{i}": k for i, k in enumerate(keep)}; params["tt"] = tt
                cx.execute(text(f"UPDATE dataview.dv_column_map SET active_ind='N' "
                                f"WHERE target_table=:tt AND mapping_method='SKIP' AND active_ind='Y' "
                                f"AND UPPER(source_column) NOT IN ({inlist})"), params)
            else:
                cx.execute(text("UPDATE dataview.dv_column_map SET active_ind='N' "
                                "WHERE target_table=:tt AND mapping_method='SKIP' AND active_ind='Y'"),
                           {"tt": tt})
    except Exception:
        pass

def _skips(engine, table):
    """Set of normalized source columns explicitly marked skip for this table."""
    if engine is None:
        return set()
    import pandas as pd
    from sqlalchemy import text
    try:
        df = pd.read_sql(text(
            "SELECT source_column FROM dataview.dv_column_map "
            "WHERE target_table=:tt AND mapping_method='SKIP' AND active_ind='Y'"),
            engine, params={"tt": table.upper()})
        return {str(r.source_column).upper() for r in df.itertuples()}
    except Exception:
        return set()

def _suggest(cols, table, COLS, FKC, syn):
    try:
        return pdl.suggest_colmap(cols, table, COLS, FKC, syn)   # newer: synonym-aware
    except TypeError:
        return pdl.suggest_colmap(cols, table, COLS, FKC)        # older signature


# function columns (mirrors page_dir_loader's set) — defined locally for version safety
FUNCTIONS = ["seq_num", "seq_concat", "constant", "concat", "coalesce"]
_FN_HELP = {
    "seq_num":    "row number per partition, file order — arg = part_col[,part_col][;order_col]",
    "seq_concat": "sequence + template in ONE step (no intermediate) — arg = part_cols ; template "
                  "with {seq} and {col}. e.g. uwi,log_id,curve_description;{curve_description}_{seq}",
    "constant":   "stamp a literal — arg = the value",
    "concat":     "build from other columns — arg = template, e.g. SRV_{uwi}_{srvy_seq}",
    "coalesce":   "source else default — arg = source_col|default",
}

def _fn_map_id(s):
    """Deterministic map_id matching entity_id's recipe (utf-16-le, upper, uppercase hex),
    so a rule saved here and one saved by the per-table loader collapse to the same row."""
    import hashlib
    return hashlib.sha1(s.upper().strip().encode("utf-16-le")).hexdigest().upper()

def _remember_funcs(engine, table, functions):
    """Persist FUNCTION rules to dv_column_map directly. active rules not in the set are
    deactivated. Self-contained — no page_dir_loader, no STRING_SPLIT."""
    if engine is None:
        return
    import json
    from sqlalchemy import text
    tt = table.upper()
    up = text(
        "MERGE dataview.dv_column_map AS t USING (SELECT :mid AS map_id) s ON t.map_id=s.map_id "
        "WHEN MATCHED THEN UPDATE SET source_column=:sc, mapping_method='FUNCTION', confirmed_ind='Y', "
        "  confirmed_by='DIR_LOADER', confirmed_date=SYSUTCDATETIME(), active_ind='Y', "
        "  row_changed_by='DIR_LOADER', row_changed_date=SYSUTCDATETIME() "
        "WHEN NOT MATCHED THEN INSERT (map_id, source_file_pattern, source_column, target_table, "
        "  target_column, confidence_score, mapping_method, confirmed_ind, confirmed_by, confirmed_date, "
        "  active_ind, row_created_by, row_created_date, source) "
        "VALUES (:mid,'*',:sc,:tt,:tc,1.0,'FUNCTION','Y','DIR_LOADER',SYSUTCDATETIME(),"
        "        'Y','DIR_LOADER',SYSUTCDATETIME(),NULL);")
    try:
        with engine.begin() as cx:
            keep = []
            for f in functions:
                tc = str(f["target"]).lower(); keep.append(tc)
                cx.execute(up, {"mid": _fn_map_id(f"FN|{tt}|{tc}"),
                                "sc": json.dumps({"fn": f["fn"], "arg": f.get("arg", "")}),
                                "tt": tt, "tc": tc})
            if keep:
                inlist = ",".join(f":k{i}" for i in range(len(keep)))
                params = {f"k{i}": k for i, k in enumerate(keep)}; params["tt"] = tt
                cx.execute(text(f"UPDATE dataview.dv_column_map SET active_ind='N' "
                                f"WHERE target_table=:tt AND mapping_method='FUNCTION' "
                                f"AND active_ind='Y' AND LOWER(target_column) NOT IN ({inlist})"), params)
            else:
                cx.execute(text("UPDATE dataview.dv_column_map SET active_ind='N' "
                                "WHERE target_table=:tt AND mapping_method='FUNCTION' AND active_ind='Y'"),
                           {"tt": tt})
    except Exception:
        pass

def _funcs(engine, table):
    """Read active FUNCTION rules for a table from dv_column_map. Self-contained."""
    if engine is None:
        return []
    import json
    import pandas as pd
    from sqlalchemy import text
    try:
        df = pd.read_sql(text(
            "SELECT target_column, source_column FROM dataview.dv_column_map "
            "WHERE target_table=:tt AND mapping_method='FUNCTION' AND active_ind='Y'"),
            engine, params={"tt": table.upper()})
        out = []
        for r in df.itertuples():
            try: spec = json.loads(r.source_column)
            except Exception: spec = {}
            out.append({"target": r.target_column, "fn": spec.get("fn", ""), "arg": spec.get("arg", "")})
        return out
    except Exception:
        return []


# ── near-match: name it the same way, or don't claim it ────────────────────────────────
# Deliberately NOT token overlap — that mis-mapped catastrophically and was abandoned.
# The only rule here: canonicalize both names through a small abbreviation table, then
# require EXACT equality. `SRVY_ID` and `survey_id` both canonicalize to `survey|id`.
# `TOP_MD` canonicalizes to `top|md` and matches nothing unless the DB really has that
# column — in which case it was an exact match anyway. No guessing, no scoring.
#
# Every pair below is one the DDL alignment actually created. Add to it only when the
# schema proves the pair — never because a name "looks like" another.
_STEM_SYNONYMS = {
    "srvy": "survey", "srv": "survey", "svy": "survey",
    "sta": "station", "stn": "station",
    "incl": "inclination", "inc": "inclination",
    "azim": "azimuth", "az": "azimuth",
    "tvdss": "tvd", "tvdsubsea": "tvd",
    "elev": "elevation",
    "nbr": "num", "no": "num",
    "desc": "description",
}


def _canon_col(name):
    """Canonical form of a column name: split on non-alphanumerics, expand each token
    through _STEM_SYNONYMS, rejoin. Returns a string used for EQUALITY only."""
    import re
    toks = [t for t in re.split(r"[^a-z0-9]+", str(name).lower()) if t]
    return "|".join(_STEM_SYNONYMS.get(t, t) for t in toks)


def _near_matches(unmapped_srcs, free_db_cols):
    """{source: (db_col, why)} for sources whose canonical name equals exactly one free
    target column. Ambiguity (two sources → one target, or two targets → one source)
    yields NO proposal — an ambiguous auto-map is the mis-mapping bug all over again."""
    by_canon_db = {}
    for c in free_db_cols:
        by_canon_db.setdefault(_canon_col(c), []).append(c)
    hits = {}
    for s in unmapped_srcs:
        cands = by_canon_db.get(_canon_col(s)) or []
        if len(cands) == 1:
            hits.setdefault(cands[0], []).append(s)
    out = {}
    for db_col, srcs in hits.items():
        if len(srcs) != 1:
            continue                      # two sources want it — make the human choose
        s = srcs[0]
        out[s] = (db_col, f"`{s}` and `{db_col}` are the same name once abbreviations are "
                          f"expanded (`{_canon_col(s)}`)")
    return out


def _stg_nonnull_counts(engine, stg_table, cols):
    """{column: rows holding a non-blank value} for the staging table. {} when the table
    isn't staged yet or the query fails — the caller must treat {} as UNKNOWN, not zero."""
    if engine is None or not stg_table or not cols:
        return {}
    import pandas as pd
    from sqlalchemy import text
    def _q(c):                                   # bracket-quote; staging cols are all varchar
        return "[" + str(c).replace("]", "]]") + "]"
    try:
        have = pd.read_sql(text("SELECT c.name n FROM sys.columns c "
                                "WHERE c.object_id=OBJECT_ID(:t)"),
                           engine, params={"t": stg_table})
        real = {str(r.n) for r in have.itertuples()}
        use = [c for c in cols if c in real]
        if not use:
            return {}
        sel = ", ".join(
            f"SUM(CASE WHEN {_q(c)} IS NOT NULL AND LTRIM(RTRIM({_q(c)}))<>'' THEN 1 ELSE 0 END) "
            f"AS c{i}" for i, c in enumerate(use))
        df = pd.read_sql(text(f"SELECT {sel} FROM {stg_table}"), engine)
        if df.empty:
            return {}
        row = df.iloc[0]
        return {c: int(row[f"c{i}"] or 0) for i, c in enumerate(use)}
    except Exception:
        return {}


def _suggest_functions(engine, table, src_cols, cmap, existing_funcs, schema="dataview"):
    """Propose function rules for required columns not covered by map/stamp/existing rules.
    Returns [{target, fn, arg, why}] the user can accept or edit.

    Called only AFTER near-match resolution, so a required column whose source column was
    sitting right there unmapped is already mapped and never reaches this function. That
    ordering is the fix — inventing a key for a column a source can fill is what created
    the collisions this loader then complained about."""
    import re
    missing = _required_missing(engine, table, cmap, existing_funcs, schema)
    if not missing:
        return []
    have_fn = {str(f.get("target", "")).lower() for f in (existing_funcs or [])}
    norm_src = {re.sub(r"[^a-z0-9]", "", c.lower()): c for c in src_cols}   # stem -> staging col
    pk = _table_pk_live(engine, table, schema)
    proposals = []
    for col in missing:
        if col in have_fn:
            continue
        stem = re.sub(r"(_?(id|num|no|seq|code))$", "", col.lower())      # curve_id -> curve
        match = None
        for k, orig in norm_src.items():
            if stem and (stem in k or k in stem):
                match = orig; break
        is_key = col in pk
        if col.endswith(("_seq", "_num", "_no")):
            part = ",".join(c for c in pk if c != col) or (pk[0] if pk else "")
            proposals.append({"target": col, "fn": "seq_num", "arg": part,
                              "why": f"sequence column — number within the rest of the PK ({part})"})
        elif is_key and match:
            others = [c for c in pk if c != col]
            if others:
                # one-step: partition includes the repeated value so {seq} counts per value
                part = ",".join(others) + f",{match.lower()}"
                proposals.append({"target": col, "fn": "seq_concat",
                                  "arg": f"{part};{{{match.lower()}}}_{{seq}}",
                                  "why": f"key = {match} + per-value sequence within {'+'.join(others)}"})
            else:
                proposals.append({"target": col, "fn": "concat", "arg": f"{{{match.lower()}}}",
                                  "why": f"key from {match}"})
        elif match:
            proposals.append({"target": col, "fn": "concat", "arg": f"{{{match.lower()}}}",
                              "why": f"copy of {match}"})
        else:
            proposals.append({"target": col, "fn": "constant", "arg": "",
                              "why": "no matching source column — set a constant or map manually"})
    return proposals


def build_map_review(engine, scan_rows, schema="dataview", with_data=False):
    """For every matched staged table, propose a column map (synonym-aware), classify each
    column (exact / mapped / near / skip), tag FK columns, flag required-but-uncovered
    columns, AND flag source columns that hold data but map nowhere.
    Returns a list of per-table review dicts.

    with_data=True queries staging for non-null counts, so a dropped column can be reported
    with the number of rows it would discard. Only pass it once the tables are staged —
    pre-staging callers (data sufficiency / auto-X) leave it False and get counts=unknown."""
    FKC, COLS, KIND = _live_catalog_parsed(engine, schema)
    review = []
    for r in scan_rows:
        t = r.get("table")
        if not t:
            continue
        tu = t.upper()
        src_cols = sorted(r["cols"])
        tcols_up = COLS.get(tu, set())
        db_cols = sorted({c.lower() for c in tcols_up})
        syn = _syn(engine, tu, set(db_cols))
        sug = _suggest(src_cols, tu, COLS, FKC, syn)
        skips = _skips(engine, tu)
        rows = []
        for c in src_cols:
            cu = pdl._norm(c)
            if cu.upper() in skips or c.upper() in skips:      # honor a saved skip decision
                rows.append({"source": c, "target": "— skip —", "status": "skip", "fk": ""})
                continue
            tgt = sug.get(c, "— skip —")
            exact = cu in tcols_up
            # a saved synonym is a prior human confirmation → settled, not "to review"
            confirmed = (not exact) and tgt not in ("— skip —", "", None) and syn.get(cu) == tgt
            fk = pdl._fk_of(tu, tgt, FKC) if tgt not in ("— skip —", "", None) else None
            if exact:
                status = "exact"
            elif confirmed:
                status = "confirmed"
            elif tgt not in ("— skip —", "", None):
                status = "guess"
            else:
                status = "skip"
            rows.append({"source": c, "target": tgt, "status": status,
                         "fk": (fk[1] if fk else ""), "note": ""})

        # ── only one thing can fill a column ────────────────────────────────────────
        # Two sources land on the same target whenever an exact name match coexists with a
        # learned synonym (`mnemonic` matches directly AND dv_column_map remembers
        # CURVE_NAME→mnemonic from when the extractor emitted that name). The loader already
        # knows which claim is stronger, so decide here rather than fail at promote.
        _PRI = {"exact": 0, "confirmed": 1, "guess": 2, "near": 3}
        best = {}
        for x in rows:
            if x["status"] == "skip":
                continue
            _tc = str(x["target"]).lower()      # target COLUMN — never reuse `t`, which is
            if _tc not in best:                 # the target TABLE for this whole loop body
                best[_tc] = x
                continue
            cur = best[_tc]
            win, lose = ((x, cur) if _PRI[x["status"]] < _PRI[cur["status"]] else (cur, x))
            best[_tc] = win
            lose["status"] = "skip"
            lose["target"] = "— skip —"
            lose["note"] = f"`{win['source']}` ({win['status']}) already fills {_tc}"

        # ── near-match: the obvious source column, sitting right there unmapped ─────
        # Auto-X only ever asked "is every required target column covered?". It never
        # asked "is every source column that holds data going somewhere?" — so a source
        # that matched nothing mapped nowhere, silently, because the target was nullable.
        # Formation tops loaded with a name and no depths that way.
        _claimed = {str(x["target"]).lower() for x in rows if x["status"] != "skip"}
        _fn_claimed = {str(f.get("target", "")).lower() for f in _funcs(engine, tu)}
        _free_db = [c for c in db_cols if c not in _claimed and c not in _fn_claimed]
        _open = [x for x in rows
                 if x["status"] == "skip" and not x.get("note")
                 and x["source"].upper() not in skips and pdl._norm(x["source"]).upper() not in skips]
        near = _near_matches([x["source"] for x in _open], _free_db)
        for x in _open:
            hit = near.get(x["source"])
            if hit:
                x["target"], x["status"] = hit[0], "near"
                x["note"] = hit[1]
                x["fk"] = (lambda f: f[1] if f else "")(pdl._fk_of(tu, hit[0], FKC))

        cmap = {x["source"]: x["target"] for x in rows if x["status"] != "skip"}

        # ── what's still going nowhere, and does it hold data? ──────────────────────
        # A column that is 100% empty in staging is not a loss; warning about it would
        # only teach you to ignore the warning. Count first, then speak. No count
        # available (not staged yet) = unknown, say so rather than assume.
        _still = [x["source"] for x in rows
                  if x["status"] == "skip" and not x.get("note")
                  and x["source"].upper() not in skips and pdl._norm(x["source"]).upper() not in skips]
        counts = _stg_nonnull_counts(engine, r.get("stg_table"), _still) if with_data else {}
        dropped_cols = []
        for c in _still:
            n = counts.get(c)
            if n == 0:
                continue                                   # provably empty — not a loss
            dropped_cols.append({"source": c, "rows": n})  # n=None → unknown, still warn

        # A derived rule is only for a column NO source can fill. Once the extractors emit
        # station_id / survey_id / mnemonic directly, yesterday's rules are redundant — drop
        # them rather than collide. (This is why rules that were right this morning are wrong
        # this afternoon: the extractor changed under them.)
        _covered = {str(v).lower() for v in cmap.values()}
        _all_funcs = _funcs(engine, tu)
        funcs, dropped_funcs = [], []
        for f in _all_funcs:
            ft = str(f.get("target", "")).lower()
            if ft and ft in _covered:
                dropped_funcs.append(ft)
            else:
                funcs.append(f)
        req_missing = _required_missing(engine, tu, cmap, funcs, schema)
        suggested = _suggest_functions(engine, tu, src_cols, cmap, funcs, schema)
        settled = ("exact", "confirmed", "skip")   # skip = a reviewed decision (no DB home)
        # auto-map: every source column is an exact 1:1 match and nothing required is missing.
        # A near-match or a dropped column is a DECISION — never made silently on your behalf.
        auto = (bool(rows) and all(x["status"] == "exact" for x in rows)
                and not req_missing and not dropped_cols)
        stg_tbl = r.get("stg_table") or stg_name(tu)
        review.append({"target": t, "skey": stg_tbl, "stg_table": stg_tbl,
                       "src_cols": src_cols, "db_cols": db_cols, "rows": rows,
                       "funcs": funcs, "required_missing": req_missing, "suggested_funcs": suggested,
                       "auto": auto,
                       "near": [x for x in rows if x["status"] == "near"],
                       "dropped_cols": dropped_cols,
                       "dropped_funcs": dropped_funcs,
                       "demoted": [x for x in rows if x.get("note")],
                       "exact": sum(1 for x in rows if x["status"] == "exact"),
                       "confirmed": sum(1 for x in rows if x["status"] == "confirmed"),
                       "exceptions": [x["source"] for x in rows if x["status"] not in settled],
                       "fp": pdl.fingerprint_cols(src_cols)})
    return review


def _insufficient_cols(entry):
    """Required NOT-NULL target columns with no real source: either no proposed rule at all,
    or only a bare/empty constant (i.e. a placeholder the operator would have to invent).
    A required column filled from a source column (copy/seq/hash) is NOT insufficient."""
    req = list(entry.get("required_missing") or [])
    if not req:
        return []
    prop = {str(p.get("target", "")).lower(): p for p in (entry.get("suggested_funcs") or [])}
    out = []
    for c in req:
        p = prop.get(str(c).lower())
        if p is None or (p.get("fn") == "constant" and not str(p.get("arg", "")).strip()):
            out.append(c)
    return out


def _data_sufficiency(engine, scan_rows, schema="dataview"):
    """Return (coverage, children):
      coverage — {TABLE: [insufficient required cols]} per matched target (empty list = OK)
      children — {PARENT_TABLE: {child tables}} from the live FK graph, for cascade."""
    cov = {}
    for e in build_map_review(engine, scan_rows, schema):
        cov[str(e["target"]).upper()] = _insufficient_cols(e)
    children = {}
    try:
        FKC, _, _ = _live_catalog_parsed(engine, schema)
        for child, fks in (FKC or {}).items():
            for fk in fks:
                parent = str(fk.get("parent_table", "")).upper()
                if parent:
                    children.setdefault(parent, set()).add(str(child).upper())
    except Exception:
        pass
    return cov, children


def _cascade(skipset, children):
    """Transitive closure: skipping a table also skips everything that FK-depends on it."""
    out = set(skipset)
    stack = list(skipset)
    while stack:
        p = stack.pop()
        for c in children.get(p, ()):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def _file_key(d):
    """Identify which source file a staging row came from — log_id for logs, file path for PDFs."""
    import os as _os
    for k in ("LOG_ID", "SRVY_ID", "INTERP_ID"):
        v = (d.get(k) or "").strip()
        if v:
            return v
    fp = (d.get("FILE_PATH") or "").strip()
    return _os.path.basename(fp) if fp else ""


def _desep(u):
    return "".join(ch for ch in str(u) if ch not in "-. ").strip()


def _uwi_exists(engine, uwis, schema="dataview"):
    """Return the subset of de-separated UWIs that exist in dv_well (one set-based query)."""
    from sqlalchemy import text
    clean = sorted({_desep(u) for u in uwis if u and "'" not in str(u)})
    if not clean:
        return set()
    vals = ", ".join(f"('{u}')" for u in clean)
    q = (f"SELECT v.u FROM (VALUES {vals}) v(u) "
         f"WHERE EXISTS (SELECT 1 FROM {schema}.dv_well d WHERE d.uwi = v.u)")
    with engine.connect() as cx:
        return {row[0] for row in cx.execute(text(q))}


def _extract_uwi_files(scan):
    """Rows in the scan that came from an extractor (DLIS/LIS/WITSML/LAS) and carry a UWI column.
    Returns the log-shaped rows (one per extracted CSV) that gate staging."""
    return [r for r in scan.get("rows", [])
            if r.get("extracted") and "UWI" in [c.upper() for c in r.get("cols", [])]
            and "curve" not in (r.get("stg_table", "") or "")]


def _read_uwi_rows(path):
    """Read an extracted CSV → list of dict rows + header, for UWI inspection/rewrite."""
    import csv
    with open(path, encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        return rd.fieldnames, list(rd)


def render_review_uwi(ss, server, database, schema="dataview"):
    """UWI gate — runs right after Scan, before Stage. Any extracted file with a blank UWI must
    get a valid dv_well UWI or be SKIPPed. Skipped files are dropped from the extract CSV so they
    never stage. Returns True when every extracted file is resolved (UWI or skip)."""
    import pandas as pd, csv, os
    scan = ss.get("bdl_scan")
    if not scan:
        return True
    rows = _extract_uwi_files(scan)
    if not rows:
        return True                                            # no extracted formats → nothing to gate
    eng = get_engine(server, database)

    # inspect the extract CSVs for per-file (log_id) UWI status
    files = ss.get("bdl_uwi_files")
    if files is None:
        files = {}
        for r in rows:
            try:
                _hdr, data = _read_uwi_rows(r["path"])
            except Exception:
                continue
            for d in data:
                lg = _file_key(d)
                uwi = (d.get("UWI") or "").strip()
                wn = (d.get("WELL_NAME") or "").strip()
                if lg:
                    files.setdefault(lg, {"log_id": lg, "uwi": uwi, "well_name": wn,
                                          "fmt": (r.get("extracted") or "").upper(),
                                          "path": r["path"], "assigned": uwi, "skip": False})
        # validate EVERY present UWI against dv_well in one query; blank or not-found → must resolve
        present = _uwi_exists(eng, {v["uwi"] for v in files.values() if v["uwi"]}, schema)
        for v in files.values():
            v["valid"] = bool(v["uwi"]) and _desep(v["uwi"]) in present
        ss["bdl_uwi_files"] = files

    unresolved = [v for v in files.values() if not v.get("valid") and not v["skip"]]
    if not unresolved:
        return True                                            # all resolved → staging may proceed

    st.divider()
    st.header("⚠ Assign UWI before staging")
    st.caption("These extracted files have no UWI — or a UWI that isn't in **dv_well**. Assign one "
               "that exists in dv_well, or check **SKIP** to drop the file from this run. Staging is "
               "blocked until every file is resolved. Skipped files never stage.")

    grid = pd.DataFrame([{"action": "SKIP" if v["skip"] else "keep",
                          "assign UWI": v["assigned"],
                          "file (log_id)": v["log_id"], "format": v["fmt"],
                          "well name": v["well_name"], "header UWI": v["uwi"] or "—"}
                         for v in unresolved])
    edited = st.data_editor(
        grid, hide_index=True, use_container_width=True, key="bdl_uwi_gate",
        column_order=["action", "assign UWI", "file (log_id)", "well name", "format", "header UWI"],
        column_config={
            "action": st.column_config.SelectboxColumn("action (keep/SKIP)", options=["keep", "SKIP"],
                                                       required=True, width="small",
                                                       help="SKIP = drop this file from the run"),
            "assign UWI": st.column_config.TextColumn(help="a UWI that exists in dv_well"),
            "file (log_id)": st.column_config.TextColumn(disabled=True),
            "format": st.column_config.TextColumn(disabled=True, width="small"),
            "well name": st.column_config.TextColumn(disabled=True),
            "header UWI": st.column_config.TextColumn(disabled=True, width="small",
                                                      help="what the file carried")})

    if st.button("Validate & apply", type="primary"):
        assigns = {r["file (log_id)"]: (r["assign UWI"] or "").strip() for _, r in edited.iterrows()}
        skips = {r["file (log_id)"]: (str(r["action"]).upper() == "SKIP") for _, r in edited.iterrows()}
        good = _uwi_exists(eng, {u for lg, u in assigns.items() if u and not skips.get(lg)}, schema)
        invalid = []
        for lg, v in files.items():
            if lg not in assigns:
                continue
            v["skip"] = skips.get(lg, False)
            u = assigns.get(lg, "").strip(); v["assigned"] = u
            if v["skip"]:
                continue
            if u and _desep(u) in good:
                v["uwi"] = _desep(u); v["valid"] = True
            elif u:
                invalid.append(lg)
        # rewrite each extract CSV: stamp assigned UWIs, DROP skipped files' rows (never stage)
        paths = {v["path"] for v in files.values()}
        # a log CSV and its curve CSV share the UWI/skip decision via log_id
        def _rewrite(path):
            try:
                hdr, data = _read_uwi_rows(path)
            except Exception:
                return
            if "UWI" not in (hdr or []):
                return
            keep = []
            for d in data:
                lg = _file_key(d)
                v = files.get(lg)
                if v and v["skip"]:
                    continue                                   # drop skipped file's rows
                if v and v["uwi"]:
                    d["UWI"] = v["uwi"]                         # stamp/overwrite the resolved UWI
                keep.append(d)
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=hdr); w.writeheader(); w.writerows(keep)
        # rewrite every extract CSV in the extract folders (log AND curve), matched by log_id
        seen_dirs = set()
        for p in paths:
            _rewrite(p)
            d = os.path.dirname(p)
            if d in seen_dirs:
                continue
            seen_dirs.add(d)
            for sib in os.listdir(d):                          # catch sibling curve/other CSVs
                sp = os.path.join(d, sib)
                if sp != p and sib.endswith(".csv"):
                    _rewrite(sp)
        ss["bdl_uwi_files"] = files
        if invalid:
            st.error("Not found in dv_well (fix or SKIP): " + ", ".join(invalid))
        else:
            st.success("Applied. All files resolved — you can Stage now.")
        st.rerun()
    return False                                               # unresolved files remain → gate stays


def render_match_map(ss, server, database, schema="dataview"):
    """Phase 2 UI — one consolidated review across all staged tables. Exact matches are
    pre-accepted; only exceptions need a decision. Saves confirmed maps to dv_column_map."""
    import pandas as pd
    scan = ss.get("bdl_scan")
    if not scan:
        return
    st.header("Phase 2 — batch Match & Map")
    st.caption("Exact header matches are auto-accepted. Review the ⚠ exceptions, then save "
               "— confirmed maps persist to dv_column_map (the synonym store) for promote.")

    if st.button("Build mappings", type="primary"):
        try:
            eng = get_engine(server, database)
            review = build_map_review(eng, scan["rows"], schema, with_data=True)
            # silently auto-map tables where every column is an exact 1:1 match
            maps = dict(ss.get("bdl_maps", {}))
            meta = dict(ss.get("bdl_mapmeta", {}))            # skey → (target, stg_table)
            auto_done = []
            for r in review:
                meta[r["skey"]] = (r["target"], r["stg_table"])
                if r["auto"]:
                    cmap = {x["source"]: x["target"] for x in r["rows"] if x["status"] == "exact"}
                    maps[r["skey"]] = cmap
                    _remember(eng, r["target"].upper(), r["fp"], cmap)
                    auto_done.append(r["skey"])
            ss["bdl_maps"] = maps
            ss["bdl_mapmeta"] = meta
            ss["bdl_auto"] = auto_done
            ss["bdl_review"] = review
        except Exception as e:
            st.error(f"Match & Map failed: {e}")

    review = ss.get("bdl_review")
    if not review:
        return
    auto_set = set(ss.get("bdl_auto", []))

    # top summary — auto-mapped tables show ✅ done
    st.dataframe(pd.DataFrame([{
        "target": r["target"] + ("" if r["stg_table"] == stg_name(r["target"].upper())
                                 else f"  ⟵ {r['stg_table'].split('.')[-1]}"),
        "cols": len(r["src_cols"]),
        "exact": r["exact"],
        "confirmed": r["confirmed"],
        "to review": len(r["exceptions"]),
        "required missing": ", ".join(r["required_missing"]) or "—",
        "data dropped": ", ".join(d["source"] for d in r.get("dropped_cols", [])) or "—",
        "status": "✅ auto" if r["skey"] in auto_set else
                  ("⚠" if (r["exceptions"] or r["required_missing"] or r.get("dropped_cols"))
                   else "✅")}
        for r in review]), hide_index=True, use_container_width=True)
    st.caption("**✅ auto** = every column matched exactly, mapped for you (no review needed) · "
               "**to review** = unmapped or a fresh guess · **data dropped** = source columns "
               "holding values that map nowhere and would be discarded silently.")

    review_needed = [r for r in review if r["skey"] not in auto_set]
    if not review_needed:
        st.success(f"All {len(review)} tables auto-mapped (exact matches) — nothing *needs* "
                   "review. Open a table below to change a mapping anyway, then Save.")
    # Render the editor for EVERY table, not just the ones needing review: an exact match is
    # a good default, not a decision the operator is stuck with. Auto tables stay collapsed.

    with st.form("bdl_phase2"):
        editors = {}
        fn_editors = {}
        for r in review:
            n_exc = len(r["exceptions"])
            flag = "⚠" if (n_exc or r["required_missing"] or r.get("dropped_cols")) else "✅"
            settled = ("exact", "confirmed", "skip")
            slabel = r["target"] if r["stg_table"] == stg_name(r["target"].upper()) \
                else f"{r['target']} ⟵ {r['stg_table'].split('.')[-1]}"
            with st.expander(f"{flag}  {slabel}  ·  {len(r['src_cols'])} cols, "
                             f"{n_exc} to review",
                             expanded=bool(n_exc or r["required_missing"] or r.get("dropped_cols"))):
                # ⚠ THE SILENT LOSS — a nullable target means nothing else will ever tell you
                if r.get("dropped_cols"):
                    _d = r["dropped_cols"]
                    st.warning(
                        f"⚠ **{len(_d)} source column(s) hold data but map nowhere — this data "
                        f"will be silently discarded:**\n\n"
                        + "\n".join(
                            f"- `{d['source']}` — "
                            + (f"**{d['rows']:,} row(s)** with a value" if d["rows"] is not None
                               else "row count unknown (staging not queryable)")
                            for d in _d)
                        + "\n\nMap each one below, or set it to **— skip —** to state on the "
                          "record that dropping it is intended. The target column is nullable, "
                          "so the load will otherwise succeed with the values missing.")
                if r.get("near"):
                    st.info("Near-match — proposed **mapping**, not a rule (pre-filled below, "
                            "change or skip it if wrong):\n\n"
                            + "\n".join(f"- `{x['source']}` → **{x['target']}** — {x['note']}"
                                        for x in r["near"]))
                if r["required_missing"]:
                    st.warning("Required, not covered by map/stamp/function: "
                               + ", ".join(r["required_missing"])
                               + ".  Suggested rules are pre-filled below — review and Save, or edit.")
                # Say what was auto-resolved. An automatic decision you can't see is
                # indistinguishable from a bug — and this one silently changes what loads.
                if r.get("demoted"):
                    st.info("Two things wanted the same column, so the stronger claim won:\n\n"
                            + "\n".join(f"- `{x['source']}` → **skipped** — {x['note']}"
                                        for x in r["demoted"])
                            + "\n\nOverride below if the wrong one was kept.")
                if r.get("dropped_funcs"):
                    st.info("Derived rule(s) dropped — a source column now supplies "
                            + ", ".join(f"**{c}**" for c in r["dropped_funcs"])
                            + " directly, so the rule is redundant. (Rules made before the "
                              "extractors emitted these columns are no longer needed.)")
                if r["suggested_funcs"]:
                    st.caption("💡 suggested: " + " · ".join(
                        f"`{p['target']} = {p['fn']}({p['arg']})`  ({p['why']})"
                        for p in r["suggested_funcs"]))
                if r["funcs"]:
                    st.caption("function rules: " + " · ".join(
                        f"{f['target']}={f['fn']}({f.get('arg','')})" for f in r["funcs"]))
                grid = pd.DataFrame([{"⚠": "" if x["status"] in settled else "⚠",
                                      "source": x["source"], "→ DB column": x["target"],
                                      "fk": x["fk"] or ""} for x in r["rows"]])
                label = r["target"] if r["stg_table"] == stg_name(r["target"].upper()) \
                    else f"{r['target']}  ⟵  {r['stg_table'].split('.')[-1]}"
                editors[r["skey"]] = (r["target"], st.data_editor(
                    grid, hide_index=True, use_container_width=True, key=f"bdlmap_{r['skey']}",
                    column_config={
                        "⚠": st.column_config.TextColumn(disabled=True, width="small"),
                        "source": st.column_config.TextColumn(disabled=True),
                        "→ DB column": st.column_config.SelectboxColumn(
                            options=["— skip —"] + r["db_cols"], required=True),
                        "fk": st.column_config.TextColumn(disabled=True, width="small")}),
                    r["src_cols"])

                st.markdown("**Derived columns (functions)** — computed, not from the CSV")
                st.caption(" · ".join(f"`{k}`: {v}" for k, v in _FN_HELP.items()))
                seed_rules = list(r["funcs"])
                have = {str(f.get("target", "")).lower() for f in seed_rules}
                for p in r["suggested_funcs"]:                       # add proposals not already present
                    if str(p["target"]).lower() not in have:
                        seed_rules.append({"target": p["target"], "fn": p["fn"], "arg": p["arg"]})
                if not seed_rules:
                    seed_rules = [{"target": "", "fn": "", "arg": ""}]
                fn_grid = pd.DataFrame([{"Target column": f.get("target", ""),
                                         "Function": f.get("fn", ""),
                                         "Argument": f.get("arg", "")} for f in seed_rules])
                fn_editors[r["skey"]] = st.data_editor(
                    fn_grid, hide_index=True, use_container_width=True, num_rows="dynamic",
                    key=f"bdlfn_{r['skey']}",
                    column_config={
                        "Target column": st.column_config.SelectboxColumn(options=[""] + r["db_cols"]),
                        "Function": st.column_config.SelectboxColumn(options=[""] + FUNCTIONS),
                        "Argument": st.column_config.TextColumn(help="e.g. seq_num arg = uwi")})
        saved = st.form_submit_button("💾 Save all mappings", type="primary",
                                      use_container_width=True)

    if saved:
        eng = get_engine(server, database)
        maps = dict(ss.get("bdl_maps", {}))          # keep the auto-mapped tables
        funcs_all = dict(ss.get("bdl_functions", {}))
        for skey, (target, ed, src_cols) in editors.items():
            cmap = {ed.iloc[i]["source"]: ed.iloc[i]["→ DB column"]
                    for i in range(len(src_cols))
                    if ed.iloc[i]["→ DB column"] not in ("— skip —", "", None)}
            skip_cols = [ed.iloc[i]["source"] for i in range(len(src_cols))
                         if ed.iloc[i]["→ DB column"] in ("— skip —", "", None)]
            maps[skey] = cmap
            fp = pdl.fingerprint_cols(sorted(src_cols))
            _remember(eng, target.upper(), fp, cmap)
            _remember_skips(eng, target.upper(), skip_cols)     # persist skip decisions
            fe = fn_editors.get(skey)
            rules, blocked = [], []
            _mapped = {str(v).lower() for v in cmap.values()}
            if fe is not None:
                for _, fr in fe.iterrows():
                    if not (fr["Target column"] and fr["Function"]):
                        continue
                    tc = str(fr["Target column"]).lower()
                    # Mutual exclusion: a column a source fills cannot also be derived.
                    # Refusing here means the UI can't construct the collision at all —
                    # rather than promote failing three layers away with "only one thing
                    # can fill a column".
                    if tc in _mapped:
                        blocked.append(tc)
                        continue
                    rules.append({"target": fr["Target column"], "fn": fr["Function"],
                                  "arg": fr["Argument"] or ""})
            if blocked:
                st.info(f"**{target}** — rule(s) for " + ", ".join(f"`{c}`" for c in blocked)
                        + " were dropped: a source column is mapped to that column, so the "
                          "rule would collide. Skip the source column if you want the rule instead.")
            funcs_all[skey] = rules
            _remember_funcs(eng, target.upper(), rules)
        ss["bdl_maps"] = maps
        ss["bdl_functions"] = funcs_all
        st.success(f"Saved mappings + function rules for {len(maps)} staged tables to dv_column_map. "
                   "Next: FK analysis across the batch (Phase 3).")


def stg_name(target, fp=None):
    base = f"{STG_SCHEMA}.{target.lower()}"
    return base if not fp else f"{base}_{fp[:8].lower()}"


def ensure_schema(engine):
    from sqlalchemy import text
    with engine.begin() as cx:
        cx.execute(text(f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name='{STG_SCHEMA}') "
                        f"EXEC('CREATE SCHEMA {STG_SCHEMA}')"))

def create_stg(engine, tbl, columns):
    """Drop+recreate an all-varchar staging table (+ _row_id, _src_file)."""
    from sqlalchemy import text
    defs = ["_row_id int", "_src_file varchar(260)"] + [f"[{c}] varchar(4000)" for c in columns]
    with engine.begin() as cx:
        cx.execute(text(f"IF OBJECT_ID('{tbl}') IS NOT NULL DROP TABLE {tbl}"))
        cx.execute(text(f"CREATE TABLE {tbl} ({', '.join(defs)})"))

def count_rows(engine, tbl):
    from sqlalchemy import text
    with engine.connect() as cx:
        return cx.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()


# ── BCP ───────────────────────────────────────────────────────────────────────
def _find_bcp():
    """Return the newest bcp.exe available, preferring the versioned Client SDK tools
    over whatever is first on PATH (which is often the old Driver 11 bcp that lacks
    -C 65001). Falls back to 'bcp' on PATH."""
    import glob, shutil
    roots = [r"C:\Program Files\Microsoft SQL Server\Client SDK\ODBC",
             r"C:\Program Files (x86)\Microsoft SQL Server\Client SDK\ODBC"]
    found = []
    for root in roots:
        for exe in glob.glob(os.path.join(root, "*", "Tools", "Binn", "bcp.exe")):
            # version is the folder after ODBC, e.g. 170, 160, 150
            try:
                ver = int(os.path.basename(os.path.dirname(os.path.dirname(
                    os.path.dirname(exe)))))
            except ValueError:
                ver = 0
            found.append((ver, exe))
    if found:
        return sorted(found, reverse=True)[0][1]      # highest version
    return shutil.which("bcp") or "bcp"


def bcp_cmd(server, database, tbl, safe_file):
    """tbl already includes the schema (stg.dv_well). UTF-8 file, -c -C 65001. Uses the
    newest bcp found (170 tools) so -C 65001 is supported even if PATH has Driver 11."""
    return [_find_bcp(), f"{database}.{tbl}", "in", safe_file, "-S", server, "-T",
            "-c", "-C", "65001", "-t", FS_BCP, "-r", BCP_RT, "-b", "50000", "-m", "100"]

def run_bcp(server, database, tbl, safe_file):
    cmd = bcp_cmd(server, database, tbl, safe_file)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr, " ".join(cmd)
    except FileNotFoundError:
        return 127, "", "bcp not found on PATH — install SQL Server command-line tools", " ".join(cmd)


# ── orchestration ─────────────────────────────────────────────────────────────
def stage_directory(engine, server, database, rows, bulk_dir=r"C:\Bulk", progress=None):
    """rows: profile_directory rows [{file, path, table, cols, ...}]. Groups files by
    (target, fingerprint), creates one staging table per group, BCPs each file in.
    Safe files are written to bulk_dir and KEPT (for inspection / manual re-run)."""
    ensure_schema(engine)
    try:
        os.makedirs(bulk_dir, exist_ok=True)
    except OSError:
        pass
    groups = {}
    for r in rows:
        if not r.get("table"):
            continue
        cols = sorted(r["cols"]); fp = pdl.fingerprint_cols(cols)
        override = r.get("stg_table")                          # LAS rows carry their own stg table
        key = override or (r["table"], fp)
        g = groups.setdefault(key,
                              {"target": r["table"], "fp": fp, "cols": cols, "files": [], "stg": override})
        g["files"].append((r["file"], r["path"]))

    tcount = {}
    for g in groups.values():
        if not g["stg"]:
            tcount[g["target"]] = tcount.get(g["target"], 0) + 1

    results = []
    for i, (key, g) in enumerate(groups.items()):
        t = g["target"]
        tbl = g["stg"] or stg_name(t, g["fp"] if tcount.get(t, 1) > 1 else None)
        if progress:
            progress(i, len(groups), tbl)
        create_stg(engine, tbl, g["cols"])
        expected, errs, logs = 0, [], []
        for fname, path in g["files"]:
            import re as _re
            safe_name = _re.sub(r"[^A-Za-z0-9._-]", "_", f"{tbl.split('.')[-1]}__{fname}")
            safe = os.path.join(bulk_dir, safe_name + ".bcp")
            n = build_safe_file(path, safe, fname, g["cols"])
            rc, out, err, cmd = run_bcp(server, database, tbl, safe)
            logs.append({"file": fname, "safe": safe, "cmd": cmd, "rc": rc, "expected": n,
                         "out": (out or "").strip(), "err": (err or "").strip()})
            if rc != 0:
                errs.append(f"{fname}: {(err or out).strip()[:300]}")
            else:
                expected += n
        loaded = count_rows(engine, tbl)
        results.append({"target": t, "stg_table": tbl, "fingerprint": fp,
                        "files": len(g["files"]), "cols": len(g["cols"]),
                        "expected": expected, "loaded": loaded, "errors": errs, "logs": logs})
    return results


# ── Streamlit page ────────────────────────────────────────────────────────────
_IDENT = {"uwi", "api", "api_num", "api_number", "api_no", "api14", "api_14"}

def _table_cols_db(engine, table, schema="dataview"):
    """Lowercase column-name set for a table, from sys. Self-contained (no page_dir_loader)."""
    import pandas as pd
    from sqlalchemy import text
    try:
        df = pd.read_sql(text("SELECT name FROM sys.columns WHERE object_id=OBJECT_ID(:t)"),
                         engine, params={"t": f"{schema}.{table.lower()}"})
        return {str(r.name).lower() for r in df.itertuples()}
    except Exception:
        return set()


def _table_pk_live(engine, table, schema="dataview"):
    import pandas as pd
    from sqlalchemy import text
    try:
        df = pd.read_sql(text(
            "SELECT c.name n FROM sys.indexes i "
            "JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id "
            "JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id "
            "WHERE i.is_primary_key=1 AND i.object_id=OBJECT_ID(:t) ORDER BY ic.key_ordinal"),
            engine, params={"t": f"{schema}.{table.lower()}"})
        return [str(r.n).lower() for r in df.itertuples()]
    except Exception:
        return []

def _val_expr(alias, col, is_ident):
    """Promote-time SQL for a staging column: trim, and de-separate identifier keys."""
    base = f"LTRIM(RTRIM([{alias}].[{col}]))"
    if is_ident:
        base = f"REPLACE(REPLACE(REPLACE({base},'-',''),' ',''),'.','')"
    return base

def _id_sql(value_expr):
    """Canonical entity id for any SQL value expression. CAST to nvarchar is REQUIRED:
    HASHBYTES hashes raw bytes, and staging columns are varchar — the canonical ba_id was
    hashed from nvarchar (UTF-16LE), so varchar would produce a different, never-matching id."""
    return (f"UPPER(CONVERT(varchar(40),"
            f"HASHBYTES('SHA1',UPPER(LTRIM(RTRIM(CAST({value_expr} AS nvarchar(4000))))))"
            f",2))")

def _id_expr(alias, col):
    return _id_sql(f"[{alias}].[{col}]")

def analyze_fks(engine, maps, schema="dataview", staged=None, meta=None):
    """For every mapped table's FKs, find distinct promote-time values with no parent match.
    Groups results by parent table. Returns {parent: {kind, pk, values:{val:{n, froms}}}}."""
    import pandas as pd
    from sqlalchemy import text
    FKC, COLS, KIND = _live_catalog_parsed(engine, schema)
    meta = meta or {}
    by_parent = {}
    for skey, cmap in maps.items():
        target, stg = meta.get(skey, (skey, skey))          # skey → (target, staging table)
        tu = target.upper()
        inv = {db.lower(): src for src, db in cmap.items()}
        for fk in FKC.get(tu, []):
            childs = [c.lower() for c in fk["child_cols"]]
            parent = fk["parent_table"]
            if len(childs) != 1:
                continue                                   # composite FKs handled later
            child_col = childs[0]
            src_col = inv.get(child_col)
            if not src_col:
                continue                                   # FK column not mapped → loads NULL
            kind = (pdl._fk_of(tu, child_col, FKC) or (parent, "parent"))[1]
            pk = _table_pk_live(engine, parent)
            if not pk:
                continue
            pkc = pk[0]
            is_ident = child_col in _IDENT
            slot0 = by_parent.setdefault(parent, {"kind": kind, "pk": pkc, "values": {},
                                                  "errors": [], "sources": []})
            slot0["sources"].append({"target": target, "child_col": child_col, "stg_table": stg,
                                     "stg_col": src_col, "is_ident": is_ident, "kind": kind})
            if kind == "entity":
                disp = f"LTRIM(RTRIM([s].[{src_col}]))"     # show the name
                match = _id_expr("s", src_col)              # compare hashed id
            else:
                disp = _val_expr("s", src_col, is_ident)
                match = disp
            q = text(
                f"SELECT {disp} AS val, COUNT(*) AS n FROM {stg} s "
                f"WHERE NULLIF(LTRIM(RTRIM([s].[{src_col}])),'') IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM {schema}.{parent.lower()} p WHERE p.[{pkc}] = {match}) "
                f"GROUP BY {disp}")
            try:
                df = pd.read_sql(q, engine)
            except Exception as e:
                slot0["errors"].append(f"{target}.{child_col}: {e}")
                continue
            for r in df.itertuples(index=False):
                v = str(r.val)
                cell = slot0["values"].setdefault(v, {"n": 0, "froms": []})
                cell["n"] += int(r.n)
                cell["froms"].append(f"{target}.{child_col}")
    return by_parent


def render_fk_analysis(ss, server, database, schema="dataview"):
    import pandas as pd
    maps = ss.get("bdl_maps")
    if not maps:
        return
    st.header("Phase 3 — batch FK analysis")
    st.caption("Set-based: each FK's promote-time value (de-sep UWI, SHA1 for entity, raw for "
               "reference) is matched against its parent. Unmatched values would violate the FK "
               "on promote — grouped by parent for resolution.")

    if st.button("Analyze FKs", type="primary"):
        try:
            eng = get_engine(server, database)
            ss["bdl_fk"] = analyze_fks(eng, maps, schema, ss.get("bdl_staged"), ss.get("bdl_mapmeta"))
        except Exception as e:
            st.error(f"FK analysis failed: {e}")

    by_parent = ss.get("bdl_fk")
    if by_parent is None:
        return
    if not by_parent:
        st.success("No FK violations — every mapped FK value already matches its parent. "
                   "Clear to promote (Phase 5).")
        return

    st.dataframe(pd.DataFrame([{
        "parent": p, "kind": d["kind"],
        "unmatched values": len(d["values"]),
        "rows affected": sum(c["n"] for c in d["values"].values()),
        "from": ", ".join(sorted({f for c in d["values"].values() for f in c["froms"]}))}
        for p, d in sorted(by_parent.items())]), hide_index=True, use_container_width=True)

    for p, d in sorted(by_parent.items()):
        with st.expander(f"{p}  ({d['kind']}) · {len(d['values'])} unmatched"):
            if d.get("errors"):
                for e in d["errors"]:
                    st.error(e)
            st.dataframe(pd.DataFrame([{"value": v, "rows": c["n"],
                                        "from": ", ".join(sorted(set(c["froms"])))}
                                       for v, c in sorted(d["values"].items())]),
                         hide_index=True, use_container_width=True)
    st.info("Next: Phase 4 — one Add / Remap / Null grid per parent, applied as set-based UPDATEs.")


def _existing_options(engine, parent, kind, schema="dataview", limit=2000):
    """Dropdown options for Remap: entity → existing names; else → existing PK codes."""
    import pandas as pd
    from sqlalchemy import text
    try:
        if kind == "entity":
            namecol = "ba_name" if parent.upper() == "DV_BUSINESS_ASSOCIATE" else "field_name"
            df = pd.read_sql(text(f"SELECT DISTINCT {namecol} v FROM {schema}.{parent.lower()} "
                                  f"WHERE {namecol} IS NOT NULL ORDER BY {namecol}"), engine)
        else:
            pk = _table_pk_live(engine, parent, schema)
            if not pk:
                return []
            df = pd.read_sql(text(f"SELECT DISTINCT {pk[0]} v FROM {schema}.{parent.lower()} "
                                  f"WHERE {pk[0]} IS NOT NULL ORDER BY {pk[0]}"), engine)
        return [str(r.v) for r in df.itertuples(index=False)][:limit]
    except Exception:
        return []


def _add_sql(parent, pkc, kind, cols_present):
    """Idempotent set-based INSERT for chosen Add values (bound as a VALUES list :v0.. )."""
    p = f"dataview.{parent.lower()}"
    if kind == "entity":
        namecol = "ba_name" if parent.upper() == "DV_BUSINESS_ASSOCIATE" else "field_name"
        idcol   = "ba_id"   if parent.upper() == "DV_BUSINESS_ASSOCIATE" else "field_id"
        idexpr  = _id_sql("v.val")
        extra_c, extra_v = [], []
        if parent.upper() == "DV_BUSINESS_ASSOCIATE":
            extra_c += ["ba_type", "short_name"]; extra_v += ["'COMPANY'", "LEFT(v.val,40)"]
        else:
            extra_c += ["field_type"]; extra_v += ["'UNKNOWN'"]
        for c, val in (("active_ind", "'Y'"), ("row_created_by", "'DIR_LOADER'"),
                       ("row_changed_by", "'DIR_LOADER'"), ("row_created_date", "SYSUTCDATETIME()")):
            if c in cols_present:
                extra_c.append(c); extra_v.append(val)
        collist = ", ".join([idcol, namecol] + extra_c)
        vallist = ", ".join([idexpr, "LTRIM(RTRIM(v.val))"] + extra_v)
        return (f"INSERT INTO {p} ({collist}) SELECT {vallist} FROM (VALUES {{vals}}) v(val) "
                f"WHERE NOT EXISTS (SELECT 1 FROM {p} x WHERE x.{idcol}={idexpr})")
    # reference / other: value is the PK code
    extra_c, extra_v = [], []
    for c, val in (("active_ind", "'Y'"), ("row_created_by", "'DIR_LOADER'"),
                   ("row_created_date", "SYSUTCDATETIME()")):
        if c in cols_present:
            extra_c.append(c); extra_v.append(val)
    collist = ", ".join([pkc] + extra_c)
    vallist = ", ".join(["LTRIM(RTRIM(v.val))"] + extra_v)
    return (f"INSERT INTO {p} ({collist}) SELECT {vallist} FROM (VALUES {{vals}}) v(val) "
            f"WHERE NOT EXISTS (SELECT 1 FROM {p} x WHERE x.{pkc}=LTRIM(RTRIM(v.val)))")


def apply_resolutions(engine, by_parent, decisions, schema="dataview"):
    """decisions: {parent: [{value, action, remap_to}]}. Executes:
      add    → INSERT the value into the parent (set-based, idempotent)
      remap  → UPDATE every source staging col: set value := remap_to where it matches
      null   → UPDATE every source staging col: set NULL where it matches
    Returns a log of what ran."""
    from sqlalchemy import text
    log = []
    for parent, decs in decisions.items():
        info = by_parent.get(parent)
        if not info:
            continue
        kind, pkc, sources = info["kind"], info["pk"], info.get("sources", [])
        cols_present = _table_cols_db(engine, parent)
        adds = [d["value"] for d in decs if d["action"] == "add"]
        remaps = [(d["value"], d["remap_to"]) for d in decs
                  if d["action"] == "remap" and d.get("remap_to") not in (None, "", "— skip —")]
        nulls = [d["value"] for d in decs if d["action"] == "null"]
        try:
            with engine.begin() as cx:
                if adds:
                    tmpl = _add_sql(parent, pkc, kind, cols_present)
                    vals = ", ".join(f"(:v{i})" for i in range(len(adds)))
                    cx.execute(text(tmpl.replace("{vals}", vals)),
                               {f"v{i}": v for i, v in enumerate(adds)})
                    log.append(f"{parent}: seeded {len(adds)} value(s)")
                # remap / null are UPDATEs against every staging source of this parent
                for src in sources:
                    stg, col, is_ident, k = src["stg_table"], src["stg_col"], src["is_ident"], src["kind"]
                    match = (f"LTRIM(RTRIM([s].[{col}]))" if k == "entity"
                             else _val_expr("s", col, is_ident))
                    for value, target in remaps:
                        cx.execute(text(f"UPDATE s SET [{col}]=:nv FROM {stg} s WHERE {match}=:val"),
                                   {"nv": target, "val": value})
                    if nulls:
                        for value in nulls:
                            cx.execute(text(f"UPDATE s SET [{col}]=NULL FROM {stg} s WHERE {match}=:val"),
                                       {"val": value})
                if remaps: log.append(f"{parent}: remapped {len(remaps)} value(s) across {len(sources)} source(s)")
                if nulls:  log.append(f"{parent}: nulled {len(nulls)} value(s) across {len(sources)} source(s)")
        except Exception as e:
            log.append(f"{parent}: ERROR {e}")
    return log


def render_fk_resolution(ss, server, database, schema="dataview"):
    import pandas as pd
    by_parent = ss.get("bdl_fk")
    if not by_parent:
        return
    st.header("Phase 4 — resolve FK violations")
    st.caption("Per parent: Add (seed the parent), Remap (fold onto an existing value), or Null "
               "(blank it). Applied as set-based UPDATEs to staging / INSERTs to the parent. "
               "Data-table parents (e.g. DV_WELL) can't be seeded — resolve by promote order.")

    eng = get_engine(server, database)
    open_parents = {p: info for p, info in by_parent.items() if info.get("values")}
    if not open_parents:
        st.success("No FK violations to resolve — all parents matched. Clear to promote (Phase 5).")
        return
    with st.form("bdl_phase4"):
        editors = {}
        for parent, info in sorted(open_parents.items()):
            kind = info["kind"]
            can_add = kind in ("entity", "reference")     # data-table parents can't be seeded
            opts = ["— skip —"] + _existing_options(eng, parent, kind, schema)
            with st.expander(f"{parent}  ({kind}) · {len(info['values'])} unmatched"
                             + ("" if can_add else "  — Remap/Null only"), expanded=True):
                rows = []
                for v, c in sorted(info["values"].items()):
                    rows.append({"☑ Add": can_add, "value": v, "rows": c["n"],
                                 "Map to existing": "— skip —", "☑ Remap": False, "☑ Null": False})
                grid = pd.DataFrame(rows)
                cfg = {"value": st.column_config.TextColumn(disabled=True),
                       "rows": st.column_config.NumberColumn(disabled=True, width="small"),
                       "☑ Add": st.column_config.CheckboxColumn(disabled=not can_add,
                                 help="Seed this value into the parent"),
                       "Map to existing": st.column_config.SelectboxColumn(options=opts),
                       "☑ Remap": st.column_config.CheckboxColumn(help="Use 'Map to existing'"),
                       "☑ Null": st.column_config.CheckboxColumn(help="Blank the value in staging")}
                editors[parent] = st.data_editor(grid, hide_index=True, use_container_width=True,
                                                 key=f"bdlfk_{parent}", column_config=cfg)
        applied = st.form_submit_button("✅ Apply resolutions (set-based)", type="primary",
                                        use_container_width=True)

    if applied:
        decisions = {}
        for parent, ed in editors.items():
            decs = []
            for _, r in ed.iterrows():
                if r["☑ Remap"] and r["Map to existing"] not in ("— skip —", "", None):
                    decs.append({"value": r["value"], "action": "remap", "remap_to": r["Map to existing"]})
                elif r["☑ Null"]:
                    decs.append({"value": r["value"], "action": "null"})
                elif r["☑ Add"]:
                    decs.append({"value": r["value"], "action": "add"})
            if decs:
                decisions[parent] = decs
        log = apply_resolutions(eng, by_parent, decisions, schema)
        for line in log:
            (st.error if "ERROR" in line else st.success)(line)
        st.info("Re-run Phase 3 (Analyze FKs) to confirm violations cleared, then promote (Phase 5).")


def _fn_expr_sql(rule, cmap_inv, is_ident_of):
    """Render a function rule as (select_expr, partition_cols|None). {col} tokens resolve
    to the staging column mapped from that DB column (or the staging column itself)."""
    import re
    fn, arg = rule["fn"], rule.get("arg", "") or ""

    def col_sql(name):
        # name may be a DB column (resolve via cmap_inv to its staging col) or a staging col
        stg = cmap_inv.get(name.lower(), name)
        expr = f"LTRIM(RTRIM([s].[{stg}]))"
        if is_ident_of(name):
            expr = f"REPLACE(REPLACE(REPLACE({expr},'-',''),' ',''),'.','')"
        return expr

    def render_template(tpl, seq_expr=None):
        # replace {seq} and {col} tokens with SQL, concatenating with +
        parts, last = [], 0
        for m in re.finditer(r"\{(\w+)\}", tpl):
            if m.start() > last:
                parts.append("'" + tpl[last:m.start()].replace("'", "''") + "'")
            tok = m.group(1)
            if tok == "seq" and seq_expr is not None:
                parts.append(f"CAST({seq_expr} AS varchar(20))")
            else:
                parts.append(col_sql(tok))
            last = m.end()
        if last < len(tpl):
            parts.append("'" + tpl[last:].replace("'", "''") + "'")
        return " + ".join(parts) if parts else "''"

    if fn == "constant":
        if "{" in arg:                      # a template written as constant → treat as concat
            fn = "concat"
        else:
            return "'" + arg.replace("'", "''") + "'", None
    if fn == "concat" and (";" in arg or "{seq}" in arg):
        fn = "seq_concat"                   # seq_concat syntax saved under concat → upgrade
    if fn == "concat":
        return render_template(arg), None
    if fn == "coalesce":
        src, _, dflt = arg.partition("|")
        return f"COALESCE(NULLIF({col_sql(src.strip())},''),'" + dflt.replace("'", "''") + "')", None
    if fn == "seq_num":
        part_spec, _, order_spec = arg.partition(";")
        parts = [col_sql(c.strip()) for c in part_spec.split(",") if c.strip()]
        orders = [col_sql(c.strip()) for c in order_spec.split(",") if c.strip()] or ["[s].[_row_id]"]
        seq = (f"ROW_NUMBER() OVER (PARTITION BY {', '.join(parts)} ORDER BY {', '.join(orders)})"
               if parts else f"ROW_NUMBER() OVER (ORDER BY {', '.join(orders)})")
        return f"CAST({seq} AS varchar(20))", None
    if fn == "seq_concat":
        part_spec, _, tpl = arg.partition(";")
        parts = [col_sql(c.strip()) for c in part_spec.split(",") if c.strip()]
        seq = (f"ROW_NUMBER() OVER (PARTITION BY {', '.join(parts)} ORDER BY [s].[_row_id])"
               if parts else "ROW_NUMBER() OVER (ORDER BY [s].[_row_id])")
        return render_template(tpl or "{seq}", seq_expr=seq), None
    return "NULL", None


def _table_col_lens(engine, table, schema="dataview"):
    """{col_lower: max_len or None} — what a value has to fit in. Truncation is the failure
    the operator can't see coming from the CSV alone."""
    import pandas as pd
    from sqlalchemy import text
    try:
        df = pd.read_sql(text(
            "SELECT COLUMN_NAME n, CHARACTER_MAXIMUM_LENGTH L FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:tb"),
            engine, params={"s": schema, "tb": table.lower()})
        out = {}
        for r in df.itertuples():
            v = getattr(r, "L", None)
            try:
                v = int(v) if v is not None and int(v) > 0 else None
            except (TypeError, ValueError):
                v = None
            out[str(r.n).lower()] = v
        return out
    except Exception:
        return {}


def _table_notnull(engine, table, schema="dataview"):
    """{col_lower} that are NOT NULL — a blank here is a promote failure, not a warning."""
    import pandas as pd
    from sqlalchemy import text
    try:
        df = pd.read_sql(text(
            "SELECT COLUMN_NAME n FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:tb AND IS_NULLABLE='NO'"),
            engine, params={"s": schema, "tb": table.lower()})
        return {str(r.n).lower() for r in df.itertuples()}
    except Exception:
        return set()


def _table_col_types(engine, table, schema="dataview"):
    """{col_lower: sql_type} for a table's columns, from INFORMATION_SCHEMA."""
    import pandas as pd
    from sqlalchemy import text
    try:
        df = pd.read_sql(text(
            "SELECT COLUMN_NAME n, DATA_TYPE t FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:tb"),
            engine, params={"s": schema, "tb": table.lower()})
        return {str(r.n).lower(): str(r.t).lower() for r in df.itertuples()}
    except Exception:
        return {}


def _typed(expr, sqltype):
    """Wrap a trimmed varchar expression in TRY_CONVERT for date/numeric targets, so a bad
    value becomes NULL (auditable) instead of aborting the whole INSERT. Strings pass through."""
    if sqltype in ("date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"):
        return f"TRY_CONVERT({sqltype}, {expr})"
    if sqltype in ("int", "bigint", "smallint", "tinyint"):
        return f"TRY_CONVERT({sqltype}, {expr})"
    if sqltype in ("numeric", "decimal", "float", "real", "money", "smallmoney"):
        return f"TRY_CONVERT(float, {expr})"
    if sqltype == "bit":
        return f"TRY_CONVERT(bit, {expr})"
    return expr


def build_promote_sql(engine, target, cmap, functions, schema="dataview", stg=None, parsed=None):
    """Build the idempotent INSERT…SELECT that promotes stg → dataview.<target>.
    Transforms: de-sep identifiers, entity SHA1, function rules, audit stamp; NOT EXISTS on PK.
    Pass `parsed` (FKC, COLS, KIND) to avoid re-introspecting the whole schema per table."""
    FKC, COLS, KIND = parsed if parsed is not None else _live_catalog_parsed(engine, schema)
    tu = target.upper()
    tcols = {c.lower() for c in COLS.get(tu, set())}
    stg = stg or stg_name(target)
    cmap_inv = {db.lower(): src for src, db in cmap.items()}     # db col -> staging col
    ident = lambda name: name.lower() in _IDENT
    coltypes = _table_col_types(engine, target, schema)

    select_cols, insert_cols, collisions = [], [], []
    seen_targets = {}                    # target col -> what first claimed it
    def _add(dbl, expr, who=None):
        if dbl in seen_targets:
            collisions.append((dbl, seen_targets[dbl], who or "a derived rule"))
            return
        seen_targets[dbl] = who or "a derived rule"
        insert_cols.append(dbl); select_cols.append(f"{expr} AS [{dbl}]")
    # 1) mapped columns (with transforms)
    for src, db in cmap.items():
        dbl = db.lower()
        if dbl not in tcols:
            continue
        fk = pdl._fk_of(tu, dbl, FKC)
        if fk and fk[1] == "entity":
            expr = _id_sql(f"[s].[{src}]")                        # name -> ba_id/field_id (char)
        elif ident(dbl):
            expr = f"REPLACE(REPLACE(REPLACE(LTRIM(RTRIM([s].[{src}])),'-',''),' ',''),'.','')"
        else:
            expr = _typed(f"NULLIF(LTRIM(RTRIM([s].[{src}])),'')", coltypes.get(dbl, ""))
        _add(dbl, expr, f"source column `{src}` (① Map columns)")
    # 2) function-derived columns
    for f in (functions or []):
        tgt = str(f.get("target", "")).lower()
        if not tgt or tgt not in tcols:
            continue
        expr, _ = _fn_expr_sql(f, cmap_inv, ident)
        _who = f"derived rule `{f.get('fn', '?')}({f.get('arg', '')})` (④ Derived columns)"
        _add(tgt, _typed(expr, coltypes.get(tgt, "")) if coltypes.get(tgt) in
             ("date", "datetime", "datetime2", "int", "bigint", "smallint", "numeric", "decimal",
              "float") else expr, _who)
    # 3) audit stamp (only where present and not already supplied)
    dbcols = _table_cols_db(engine, target, schema)
    for c, v in (("active_ind", "'Y'"), ("row_created_by", "'DATA_LOADER'"),
                 ("row_created_date", "SYSUTCDATETIME()")):
        if c in dbcols and c not in seen_targets:
            _add(c, v)

    if collisions:
        # Name BOTH claimants. "survey_id is doubled" makes the operator hunt; "SRVY_ID and
        # a concat rule both fill survey_id" says what to delete.
        lines = [f"**{dbl}** ← {first}  ✗ and ✗  {second}"
                 for dbl, first, second in collisions]
        raise ValueError(
            "Two things fill the same column — only one can:\n\n"
            + "\n\n".join(lines)
            + "\n\nKeep whichever actually holds the value and remove the other: set the "
              "source column to `— skip —` in ① Map columns, or delete the rule in "
              "④ Derived columns. A rule is only needed when NO source column has the data.")

    pk = _table_pk_live(engine, target, schema)
    pk_in = [p for p in (pk or []) if p in insert_cols]
    pk_join = " AND ".join(f"d.[{p}] = src.[{p}]" for p in pk_in)
    sel = ",\n       ".join(select_cols)
    inner = f"SELECT {sel}\n  FROM {stg} s"
    if pk_in:
        # keep one row per PK — staging may contain duplicate keys (common in reference data),
        # and NOT EXISTS only guards against rows already in the target, not within the batch
        part = ", ".join(f"[{p}]" for p in pk_in)
        sql = (f"INSERT INTO {schema}.{target.lower()} ({', '.join('['+c+']' for c in insert_cols)})\n"
               f"SELECT {', '.join('src.['+c+']' for c in insert_cols)}\n"
               f"FROM (\n"
               f"  SELECT *, ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY (SELECT NULL)) AS _rn\n"
               f"  FROM (\n    {inner}\n  ) q\n"
               f") src\n"
               f"WHERE src._rn = 1")
        sql += (f"\n  AND NOT EXISTS (SELECT 1 FROM {schema}.{target.lower()} d WHERE {pk_join})")
    else:
        sql = (f"INSERT INTO {schema}.{target.lower()} ({', '.join('['+c+']' for c in insert_cols)})\n"
               f"SELECT {', '.join('src.['+c+']' for c in insert_cols)}\n"
               f"FROM (\n  {inner}\n) src\n")
    return sql, insert_cols, pk


def render_promote(ss, server, database, schema="dataview"):
    import pandas as pd
    from sqlalchemy import text
    scan = ss.get("bdl_scan"); maps = ss.get("bdl_maps")
    if not (scan and maps):
        return
    st.header("Phase 5 — promote (set-based)")
    st.caption("INSERT…SELECT stg → dataview in topological order. Transforms as T-SQL: de-sep "
               "UWI, SHA1 entity ids, seq_num/seq_concat via ROW_NUMBER(), audit stamped; "
               "idempotent on PK. Per-table commit, stop on error.")
    order = scan.get("order", [])
    funcs_all = ss.get("bdl_functions", {})
    eng = get_engine(server, database)

    # ── data quality, before anything is promoted ────────────────────────────────────
    # The rows are already in SQL Server as nvarchar, so profiling them is one set-based
    # query per staging table. This is the last point where a bad value is still cheap:
    # after promote it's either a hard error or — worse — a silent NULL in your vault.
    if _qa is not None:
        meta_q = ss.get("bdl_mapmeta", {})
        if st.button("🔎 Check data quality (staging)",
                     help="Profiles every mapped column against its target column's real type "
                          "and width. Nothing is changed — it reports what promote would do."):
            rep = {}
            for skey, cmap in maps.items():
                target, stg = meta_q.get(skey, (skey, skey))
                try:
                    types = _table_col_types(eng, target, schema)
                    lens = _table_col_lens(eng, target, schema)
                    nn = _table_notnull(eng, target, schema)
                    rep[skey] = {"target": target,
                                 "rows": _qa.profile(eng, stg, cmap, types, lens, nn)}
                except Exception as e:
                    rep[skey] = {"target": target, "error": str(e)}
            ss["bdl_qa"] = rep

        qa = ss.get("bdl_qa")
        if qa:
            tot = {"flag": 0, "fix": 0, "ok": 0}
            for v in qa.values():
                for r in (v.get("rows") or []):
                    tot[r["level"]] += 1
            head = f"🔴 {tot['flag']} flag · 🟡 {tot['fix']} fix · ✅ {tot['ok']} ok"
            with st.expander(f"Data quality — {head}", expanded=bool(tot["flag"])):
                st.caption("Checked against each target column's real type and width. "
                           "🔴 = would fail or silently lose data · 🟡 = repairable · "
                           "advisory only, nothing is changed here.")
                for skey, v in qa.items():
                    if v.get("error"):
                        st.caption(f"{v['target']}: could not profile — {v['error'][:120]}")
                        continue
                    bad = [r for r in v["rows"] if r["level"] != "ok"]
                    if not bad:
                        st.caption(f"✅ **{v['target']}** — nothing to flag")
                        continue
                    st.markdown(f"**{v['target']}**")
                    st.dataframe(pd.DataFrame([{
                        "": {"flag": "🔴", "fix": "🟡", "ok": "✅"}[r["level"]],
                        "source": r["source"], "→ column": r["target"], "type": r["type"],
                        "rows": r["rows"], "blank": r["blank"],
                        "what": "; ".join(r["issues"])} for r in bad]),
                        hide_index=True, use_container_width=True)
                if tot["flag"]:
                    st.warning("🔴 items will fail at promote, or load as NULL without an error. "
                               "Fix the source, or map the column elsewhere, before promoting.")

        # ── repair what is deterministically repairable ─────────────────────────────
        if _repair is not None and ss.get("bdl_qa"):
            rc1, rc2 = st.columns([1, 3])
            if rc1.button("🔧 Plan repairs",
                          help="Work out which staged values can be repaired without guessing "
                               "— fractions, units, thousands separators. Nothing changes yet."):
                plan_all, refuse_all = {}, {}
                for skey, cmap in maps.items():
                    target, stg = meta_q.get(skey, (skey, skey))
                    try:
                        types = _table_col_types(eng, target, schema)
                        fx, rf = _repair.plan(eng, stg, cmap, types)
                        if fx: plan_all[skey] = (stg, fx)
                        if rf: refuse_all[skey] = rf
                    except Exception as e:
                        st.caption(f"({target}: repair plan failed — {str(e)[:90]})")
                ss["bdl_repair"] = plan_all
                ss["bdl_refuse"] = refuse_all

            rp, rf = ss.get("bdl_repair") or {}, ss.get("bdl_refuse") or {}
            if rp or rf:
                n_fix = sum(len(v[1]) for v in rp.values())
                n_ref = sum(len(v) for v in rf.values())
                rc2.caption(f"{n_fix} value(s) repairable · {n_ref} refused")
                if rp:
                    with st.expander(f"🔧 {n_fix} value(s) can be repaired", expanded=True):
                        for skey, (stg, fx) in rp.items():
                            st.markdown(f"**{meta_q.get(skey, (skey,))[0]}**")
                            st.dataframe(pd.DataFrame(fx[:40])[["column", "old", "new", "why"]],
                                         hide_index=True, use_container_width=True)
                            if len(fx) > 40:
                                st.caption(f"…and {len(fx) - 40} more")
                        if st.button("Apply repairs to staging", type="primary"):
                            n = 0
                            for skey, (stg, fx) in rp.items():
                                n += _repair.apply(eng, stg, fx)
                            ss.pop("bdl_repair", None); ss.pop("bdl_qa", None)
                            st.success(f"Repaired {n} value(s) in staging. Re-run the data "
                                       f"quality check to confirm.")
                            st.rerun()
                if rf:
                    with st.expander(f"🚫 {n_ref} value(s) REFUSED — cannot be repaired",
                                     expanded=True):
                        st.error("These can't be recovered from what's on disk. Repairing them "
                                 "would invent data that looks right and isn't — so the loader "
                                 "won't. Re-export the source with the column formatted as "
                                 "**text**, or let them load as NULL.")
                        for skey, items in rf.items():
                            st.markdown(f"**{meta_q.get(skey, (skey,))[0]}**")
                            st.dataframe(pd.DataFrame(items[:40]), hide_index=True,
                                         use_container_width=True)

        # ── date formats ────────────────────────────────────────────────────────────
        # 03/04/2021 is 3 April or 4 March depending on who exported it; SQL Server decides
        # by the session's DATEFORMAT, which knows nothing about the source. Wrong is SILENT
        # here — every row loads and some dates are simply wrong — so infer per column from
        # its own values, and where the column genuinely can't say, refuse rather than pick.
        if _repair is not None and ss.get("bdl_qa"):
            if st.button("📅 Check date formats",
                         help="Infers each date column's format from its own values."):
                found = []
                for skey, cmap in maps.items():
                    target, stg = meta_q.get(skey, (skey, skey))
                    try:
                        types = _table_col_types(eng, target, schema)
                        with eng.connect() as _cx:
                            for src, tgt in cmap.items():
                                if (types.get(str(tgt).lower()) or "") not in (
                                        "date", "datetime", "datetime2", "smalldatetime"):
                                    continue
                                vals = [r[0] for r in _cx.execute(text(
                                    f"SELECT TOP 500 [{src}] FROM {stg} "
                                    f"WHERE NULLIF(LTRIM(RTRIM([{src}])),'') IS NOT NULL"))]
                                fmt, conf, why = _repair.detect_date_format(vals)
                                found.append({"table": target, "column": src, "→": tgt,
                                              "format": fmt or "—", "confidence": conf,
                                              "why": why})
                    except Exception as e:
                        st.caption(f"({target}: date check failed — {str(e)[:80]})")
                ss["bdl_dates"] = found

            dts = ss.get("bdl_dates")
            if dts:
                risky = [d for d in dts if d["confidence"] in ("ambiguous", "conflict")]
                with st.expander(f"📅 Date formats — {len(dts)} column(s), {len(risky)} risky",
                                 expanded=bool(risky)):
                    st.dataframe(pd.DataFrame(dts), hide_index=True, use_container_width=True)
                    if risky:
                        st.error("**Ambiguous / conflicting columns can't be inferred.** SQL "
                                 "Server will apply the session's DATEFORMAT, which has nothing "
                                 "to do with where the data came from — the rows will load and "
                                 "some dates will simply be wrong, with no error at all. "
                                 "Re-export these as ISO (yyyy-mm-dd), or confirm the format "
                                 "with whoever produced the file.")
                    else:
                        st.success("Every date column's format is determined by its own values "
                                   "— nothing is being guessed.")

    if st.button("Preview promote", type="primary"):
        import time
        prev = []
        t0 = time.perf_counter()
        parsed = _live_catalog_parsed(eng, schema)          # introspect ONCE, reuse per table
        parse_t = time.perf_counter() - t0
        meta = ss.get("bdl_mapmeta", {})                    # skey → (target, stg_table)
        # promote each staged table (skey); order by its target's position in the FK topo order
        order_ix = {t: i for i, t in enumerate(order)}
        skeys = sorted(maps.keys(),
                       key=lambda k: order_ix.get(meta.get(k, (k, ""))[0], 999))
        for skey in skeys:
            target, stg_tbl = meta.get(skey, (skey, skey))
            try:
                if not _table_cols_db(eng, target, schema):
                    raise ValueError(
                        f"'{schema}.{target}' is not a table — this staged file is mapped to "
                        f"something that looks like a column name. Correct its → table in "
                        f"Files → tables, or ↻ Reset run to clear stale scan state.")
                tb = time.perf_counter()
                sql, cols, pk = build_promote_sql(eng, target, maps[skey], funcs_all.get(skey, []),
                                                  schema, stg=stg_tbl, parsed=parsed)
                build_t = time.perf_counter() - tb
                tc = time.perf_counter()
                with eng.connect() as cx:
                    staged = cx.execute(text(f"SELECT COUNT(*) FROM {stg_tbl}")).scalar()
                count_t = time.perf_counter() - tc
                label = target if stg_tbl == stg_name(target) else f"{target} ⟵ {stg_tbl.split('.')[-1]}"
                prev.append({"table": label, "target": target, "staged": staged,
                             "cols": len(cols), "sql": sql,
                             "err": None, "build_t": build_t, "count_t": count_t})
            except Exception as e:
                label = f"{meta.get(skey,(skey,''))[0]}"
                prev.append({"table": label, "target": meta.get(skey, (skey, ""))[0],
                             "staged": None, "cols": 0, "sql": "", "err": str(e),
                             "build_t": 0, "count_t": 0})
        ss["bdl_promote_preview"] = prev
        ss["bdl_parse_t"] = parse_t

    prev = ss.get("bdl_promote_preview")
    if prev:
        if ss.get("bdl_parse_t") is not None:
            st.caption(f"⏱ catalog introspect: {ss['bdl_parse_t']:.1f}s · "
                       f"per-table build: {sum(p['build_t'] for p in prev):.1f}s · "
                       f"staged counts: {sum(p['count_t'] for p in prev):.1f}s")
        st.dataframe(pd.DataFrame([{"order": i+1, "table": p["table"], "staged rows": p["staged"],
                                    "mapped cols": p["cols"],
                                    "build s": round(p["build_t"], 2), "count s": round(p["count_t"], 2),
                                    "status": "🔴" if p["err"] else "🟢"}
                                   for i, p in enumerate(prev)]), hide_index=True, use_container_width=True)
        for p in prev:
            with st.expander(f"{p['table']} — promote SQL"):
                if p["err"]:
                    st.error(p["err"])
                else:
                    st.code(p["sql"], language="sql")

        if any(p["err"] for p in prev):
            st.error("Fix the 🔴 tables before promoting.")
        else:
            dc1, dc2 = st.columns([1, 1])
            # A TRUE dry run: execute the real promote SQL, in promote order, inside ONE
            # transaction, then roll it back. Exact by construction — it surfaces truncation,
            # NULLs, FK conflicts and duplicate keys with the server's own message, before
            # anything half-commits. One transaction (not per-table) so children see their
            # parents' rows, exactly as they would in the real run.
            if dc1.button("🧪 Dry run (rollback)",
                          help="Runs the real INSERTs and rolls them back — nothing is written. "
                               "Shows what would fail, and what would load."):
                res, failed, p = [], None, None
                with eng.connect() as cx:
                    tr = cx.begin()
                    try:
                        for p in prev:
                            t = p.get("target", p["table"])
                            # A target that isn't a table means the mapping is wrong, not the
                            # SQL. Say so HERE — otherwise it surfaces as SQL Server's
                            # "Invalid object name 'dataview.uwi'" with no hint that a scan row
                            # claimed a COLUMN as its table.
                            if not _table_cols_db(eng, t, schema):
                                raise ValueError(
                                    f"'{schema}.{t}' is not a table. The staged entry "
                                    f"'{p.get('table')}' is mapped to a target called '{t}' — "
                                    f"that looks like a COLUMN name, not a table. Fix the "
                                    f"→ table for that file in Files → tables, or ↻ Reset run "
                                    f"if it's stale state from an earlier scan.")
                            before = cx.execute(text(
                                f"SELECT COUNT(*) FROM {schema}.{t.lower()}")).scalar()
                            cx.execute(text(p["sql"]))
                            after = cx.execute(text(
                                f"SELECT COUNT(*) FROM {schema}.{t.lower()}")).scalar()
                            res.append((p["table"], after - before, None))
                    except Exception as e:
                        import traceback as _tb
                        failed = (p["table"] if p else "?", e, _tb.format_exc(),
                                  (p or {}).get("sql", ""))
                        res.append((p["table"] if p else "?", 0, str(e)))
                    finally:
                        tr.rollback()                       # never keep anything
                ss["bdl_dryrun"] = res
                ss["bdl_dryrun_exc"] = failed

            dry = ss.get("bdl_dryrun")
            if dry:
                st.markdown("**Dry run** — nothing was written:")
                for t, n, err in dry:
                    if err:
                        exc = ss.get("bdl_dryrun_exc")
                        shown = False
                        if exc and exc[0] == t:
                            shown = _render_diag(exc[1], table=t,
                                                 tb=(exc[2] if len(exc) > 2 else None))
                            if shown and len(exc) > 3 and exc[3]:
                                with st.expander("The SQL that would fail"):
                                    st.code(exc[3], language="sql")
                        if not shown:
                            st.error(f"{t}: would FAIL — {err}")
                    else:
                        st.caption(f"✅ {t}: would insert {n} row(s)")
                if not any(e for _, _, e in dry):
                    st.success(f"Dry run clean — {sum(n for _, n, _ in dry)} row(s) would be "
                               f"inserted across {len(dry)} table(s). Safe to promote.")

            if dc2.button("🚀 Promote all (per-table commit)", type="primary"):
                import time
                log = []
                # Server clock, not the client's — row_created_date is SYSUTCDATETIME(), so the
                # "did this run write it?" comparison must use the same clock.
                with eng.connect() as cx:
                    ss["bdl_promote_ts"] = cx.execute(text("SELECT SYSUTCDATETIME()")).scalar()
                for p in prev:
                    label = p["table"]                             # display only (may contain ⟵)
                    t = p.get("target", p["table"])                # real table name for SQL
                    try:
                        ti = time.perf_counter()
                        with eng.begin() as cx:
                            before = cx.execute(text(f"SELECT COUNT(*) FROM {schema}.{t.lower()}")).scalar()
                            cx.execute(text(p["sql"]))
                            after = cx.execute(text(f"SELECT COUNT(*) FROM {schema}.{t.lower()}")).scalar()
                        log.append((label, after - before, round(time.perf_counter() - ti, 2), None))
                    except Exception as e:
                        import traceback as _tb
                        log.append((label, 0, 0, str(e)))
                        # capture the stack HERE — format_exc() is empty once we've left the
                        # except block, and this is rendered on a later rerun
                        ss["bdl_promote_exc"] = (label, e, _tb.format_exc(), p.get("sql", ""))
                        break                                          # stop on error
                ss["bdl_promote_log"] = log
                # Close the loop: the files that fed this promote are now loaded, so a later scan
                # can skip them. Only on a clean run — a partial promote must stay re-runnable.
                if _gate is not None and not any(r[3] for r in log):
                    ids = ((ss.get("bdl_scan") or {}).get("gate") or {}).get("ids") or {}
                    if ids:
                        try:
                            _gate.mark_loaded(eng, list(ids.values()))
                        except Exception as e:
                            st.caption(f"(file catalog not updated: {e})")

    log = ss.get("bdl_promote_log")
    if log:
        st.subheader("Promote results")
        for row in log:
            t, n, secs, err = row
            if err:
                # A wall of generated SQL is not a diagnosis. Explain it in loader terms —
                # which column, which rule, what to change — and tuck the raw text away.
                exc = ss.get("bdl_promote_exc")
                shown = False
                if exc and exc[0] == t:
                    shown = _render_diag(exc[1], table=t,
                                         tb=(exc[2] if len(exc) > 2 else None))
                    if shown and len(exc) > 3 and exc[3]:
                        with st.expander("The SQL that failed"):
                            st.code(exc[3], language="sql")
                    if shown:
                        st.caption("Stopped here; earlier tables were committed.")
                if not shown:
                    st.error(f"{t}: FAILED — {err}  (stopped here; earlier tables committed)")
            else:
                st.success(f"{t}: +{n} rows  ({secs}s)")
        if not any(r[3] for r in log):
            st.success("Promote complete — all tables loaded into dataview.")


def verify_promote(engine, maps, schema="dataview", staged=None, meta=None, since=None):
    """Reconcile staged vs loaded, per table. Matches staged rows to the target on the PK
    columns that exist in staging (de-sep applied to identifier keys); PK columns generated
    at promote (curve_id, station_id) are dropped from the match, marking that row approximate.

    `since` — a UTC timestamp captured just before the promote ran. Rows whose
    row_created_date >= since were inserted BY THAT RUN. This is read from the database, not
    from session state, so it stays true across restarts: "present" alone can't tell a fresh
    load from a NOT EXISTS-skipped re-run."""
    from sqlalchemy import text
    meta = meta or {}
    out = []
    with engine.connect() as cx:
        for skey, cmap in maps.items():
            target, stg = meta.get(skey, (skey, skey))
            pk = _table_pk_live(engine, target, schema)
            inv = {db.lower(): src for src, db in cmap.items()}
            conds, mapped_all = [], bool(pk)
            for pkc in pk:
                src = inv.get(pkc)
                if not src:
                    mapped_all = False                       # generated / unmapped PK part
                    continue
                if pkc in _IDENT:
                    expr = f"REPLACE(REPLACE(REPLACE(LTRIM(RTRIM([s].[{src}])),'-',''),' ',''),'.','')"
                else:
                    expr = f"LTRIM(RTRIM([s].[{src}]))"
                conds.append(f"d.[{pkc}] = {expr}")
            try:
                staged_n = cx.execute(text(f"SELECT COUNT(*) FROM {stg} s")).scalar()
                dv_n = cx.execute(text(f"SELECT COUNT(*) FROM {schema}.{target.lower()}")).scalar()
                present = cx.execute(text(
                    f"SELECT COUNT(*) FROM {stg} s WHERE EXISTS "
                    f"(SELECT 1 FROM {schema}.{target.lower()} d WHERE {' AND '.join(conds)})"
                )).scalar() if conds else None
                err = None
            except Exception as e:
                staged_n = dv_n = present = None; err = str(e)

            # row_created_date evidence, straight from the target: when were the matched rows
            # actually written, and how many of them by THIS run?
            loaded_at, by, fresh = None, None, None
            if conds and not err:
                j = (f"FROM {stg} s JOIN {schema}.{target.lower()} d ON "
                     f"{' AND '.join(conds)}")
                try:
                    r = cx.execute(text(
                        f"SELECT MAX(d.row_created_date), MAX(d.row_created_by) {j}")).first()
                    loaded_at, by = (r[0], r[1]) if r else (None, None)
                    if since is not None:
                        fresh = cx.execute(text(
                            f"SELECT COUNT(*) {j} AND d.row_created_date >= :since"),
                            {"since": since}).scalar()
                except Exception:
                    pass                                   # table may lack the audit columns

            out.append({"table": target, "staged": staged_n, "dataview": dv_n,
                        "present": present, "exact": mapped_all,
                        "loaded_at": loaded_at, "loaded_by": by, "fresh": fresh,
                        "missing": (staged_n - present) if (present is not None and staged_n is not None) else None,
                        "err": err})
    return out


def render_verify(ss, server, database, schema="dataview"):
    import pandas as pd
    maps = ss.get("bdl_maps")
    if not maps:
        return
    st.header("Phase 6 — verify (staged vs loaded)")
    st.caption("Matches staged rows to the target on the PK. missing = 0 → fully loaded. "
               "Tables whose PK is generated at promote (curve_id, station_id) match on the "
               "available key and are marked ~approx.")
    order = (ss.get("bdl_scan") or {}).get("order", list(maps.keys()))

    if st.button("Verify load", type="primary"):
        eng = get_engine(server, database)
        res = verify_promote(eng, maps, schema, ss.get("bdl_staged"), ss.get("bdl_mapmeta"),
                             since=ss.get("bdl_promote_ts"))
        by_t = {r["table"]: r for r in res}
        ss["bdl_verify"] = [by_t[t] for t in order if t in by_t] + \
                           [r for r in res if r["table"] not in order]

    ver = ss.get("bdl_verify")
    if not ver:
        return
    def status(r):
        if r["err"]: return "🔴 err"
        if r["missing"] is None: return "—"
        if r["missing"] == 0: return "✅ OK" if r["exact"] else "✅ ~approx"
        return "🔴 CHECK"

    # "missing = 0" only says the rows ARE there — not that THIS run put them there. The
    # promote is NOT EXISTS-guarded, so re-running the same files inserts nothing and would
    # still verify clean. row_created_date on the matched target rows settles it, and unlike
    # a session counter it's read from the database, so it survives a restart.
    def inserted(r):
        if r.get("fresh") is None:
            return "—"
        return f"+{r['fresh']}" if r["fresh"] else "0 (already there)"

    def when(r):
        t = r.get("loaded_at")
        return t.strftime("%Y-%m-%d %H:%M:%S") if t else "—"

    st.dataframe(pd.DataFrame([{
        "table": r["table"], "staged": r["staged"], "inserted this run": inserted(r),
        "row_created_date": when(r), "by": r.get("loaded_by") or "—",
        "in dataview": r["dataview"], "staged present": r["present"],
        "missing": r["missing"], "status": status(r)}
        for r in ver]), hide_index=True, use_container_width=True)
    st.caption("**staged** = rows this run put in staging · **inserted this run** = matched "
               "target rows whose `row_created_date` is at/after this promote started — read "
               "from the database, so `0 (already there)` means the rows pre-existed and the "
               "NOT EXISTS guard skipped them (a clean re-run, not a failure) · "
               "**row_created_date** = when those rows were actually written · "
               "**in dataview** = COUNT(*) of the whole target table, all loads ever · "
               "**missing** = staged − present; 0 is the pass condition.")
    for r in ver:
        if r["err"]:
            st.error(f"{r['table']}: {r['err']}")
    bad = [r for r in ver if r["missing"] not in (0, None)]
    if not bad and not any(r["err"] for r in ver):
        fresh = [r.get("fresh") for r in ver if r.get("fresh") is not None]
        n_new = sum(fresh)
        if not fresh:
            st.success("All staged rows are present in dataview — load verified complete. "
                       "(Promote wasn't run in this session, so 'inserted this run' is unknown — "
                       "check **row_created_date** above for when the rows were written.)")
        elif n_new:
            st.success(f"All staged rows are present in dataview — load verified complete: "
                       f"**{n_new} row(s) written by this run**, confirmed by row_created_date.")
        else:
            st.info("All staged rows are present in dataview — but **this run wrote nothing**. "
                    "Every row already existed (row_created_date pre-dates this promote) and the "
                    "NOT EXISTS guard skipped them. That's a clean re-run, not a new load.")
    elif bad:
        st.warning("Not fully loaded: " + ", ".join(f"{r['table']} (−{r['missing']})" for r in bad)
                   + ". Re-run Promote (idempotent) or check the mapping.")


def run():
    import pandas as pd
    ss = st.session_state
    hc1, hc2 = st.columns([4, 1])
    hc1.header("Directory Loader")
    if hc2.button("↻ Reset run", help="Clear scan, mappings and staged state and start over "
                                       "(keeps server/database/paths)"):
        keep = {k: ss[k] for k in ("bdl_server", "bdl_db", "bdl_dir", "bdl_cat", "bdl_bulk",
                                   "bdl_recursive", "bdl_schema") if k in ss}
        for k in [k for k in ss.keys() if k.startswith("bdl_") or k.startswith("_cat")]:
            del ss[k]
        ss.update(keep)
        st.rerun()
    st.caption("Scan a directory — CSV · Excel · LAS · DLIS · LIS · WITSML · PDF · Word — "
               "extract to staging, then map → FK → check → dry run → promote → verify. "
               "Excel workbooks are exploded to one CSV per sheet. For the per-table flow "
               "with inline value repair, use ⇄ (the tabular loader) above.")

    c1, c2 = st.columns(2)
    server = c1.text_input("Server", value=ss.get("bdl_server", r"localhost\SQLEXPRESS"))
    database = c2.text_input("Database", value=ss.get("bdl_db", "DataView_Demo"))
    directory = st.text_input("Directory (CSV / Excel / LAS / DLIS / LIS / WITSML / PDF / Word)",
                              value=ss.get("bdl_dir", ""))
    recursive = st.checkbox("Include subdirectories (recursive scan)", value=ss.get("bdl_recursive", False))
    force = st.checkbox("Force re-extract (ignore the file catalog)",
                        value=ss.get("bdl_force", False),
                        help="Unchanged files are normally skipped — their content hash already "
                             "matches dv_global_file_catalog and their rows are loaded. Tick this "
                             "when the EXTRACTOR changed rather than the data: the bytes are "
                             "identical, so the gate would skip files that now extract differently.")
    ss["bdl_force"] = force
    catalog = st.text_input("FK catalog JSON (fast; blank = introspect live)",
                            value=ss.get("bdl_cat", r"dataview\schema_registry\dataview_fk_catalog.json"))
    bulk_dir = st.text_input("Bulk staging folder (safe files kept here)", value=ss.get("bdl_bulk", r"C:\Bulk"))
    schema = ss.get("bdl_schema", "dataview")
    ss["bdl_server"], ss["bdl_db"], ss["bdl_dir"], ss["bdl_cat"], ss["bdl_bulk"], ss["bdl_recursive"] = \
        server, database, directory, catalog, bulk_dir, recursive

    if st.button("Scan directory", type="primary") and directory:
        try:
            eng = get_engine(server, database)
            ss["bdl_scan"] = profile_directory_live(directory, eng, schema, bulk_dir, recursive,
                                                    force=force)  # catalog from live server
            ss.pop("bdl_uwi_files", None)                     # re-inspect UWIs for the new scan
        except Exception as e:
            st.error(f"Scan failed: {e}")

    scan = ss.get("bdl_scan")
    if not scan:
        return

    # surface extractor availability + any extraction errors (so nothing fails silently)
    # what the file catalog decided — a skip you can't see is indistinguishable from a bug
    g = scan.get("gate")
    if g:
        s = g.get("summary") or {}
        bits = " · ".join(f"{n} {k}" for k, n in s.items())
        if g.get("forced"):
            st.warning(f"🔁 Force re-extract ON — all {g['total']} file(s) re-processed, "
                       f"ignoring the catalog.  ({bits})")
        elif g.get("skipped"):
            st.info(f"📇 File catalog: {g['skipped']} of {g['total']} file(s) skipped — content "
                    f"unchanged and already loaded.  ({bits})  "
                    f"Tick **Force re-extract** to process them anyway.")
        elif bits:
            st.caption(f"📇 File catalog: {bits}")
        if g.get("note"):
            st.caption(f"📇 {g['note']}")

    for label, n, modname, dep in (scan.get("missing_extractors") or []):
        st.error(f"❌ **{n} {label} file(s) found but not scanned** — the `{modname}` extractor "
                 f"couldn't be imported, so they were skipped."
                 + (f"  Most likely `{dep}` isn't installed: `pip install {dep}`." if dep != "—" else "")
                 + f"  Also confirm `{modname}.py` is deployed next to `bulk_dir_loader.py` "
                   f"in `dataview\\import_data\\`.")
    absent = [n for n, m in (("las", _las), ("dlis", _dlis), ("lis", _lis), ("witsml", _witsml),
                             ("pdf", _pdf), ("docx", _docx)) if m is None]
    if absent:
        st.caption("extractor module(s) not importable: " + ", ".join(absent) +
                   " — those file types won't be detected.")
    if scan.get("extract_errors"):
        for fmt, err in scan["extract_errors"].items():
            st.warning(f"{fmt.upper()} extraction error: {err}")

    st.subheader("Files → tables")
    st.caption("Auto-matched by columns (data) or filename (references). Override the target with the "
               "**→ table** dropdown, or tick **skip** to drop a table (and its FK children) from the run. "
               "Tables whose required NOT-NULL columns have no source are **auto-skipped** — the **why** "
               "column lists them. Untick to keep-and-fill instead.")
    all_tables = scan.get("all_tables", [])
    schema = ss.get("bdl_schema", "dataview")
    for r in scan["rows"]:                                  # remember the matched target across skip toggles
        r.setdefault("_target", r.get("table"))
    targets = sorted({(r["_target"] or "").upper() for r in scan["rows"] if r.get("_target")})

    # data-sufficiency (auto-X) + FK child graph (cascade), cached per target set
    cov, kids = {}, {}
    try:
        eng = get_engine(server, database)
        if ss.get("bdl_suff_sig") != tuple(targets):
            ss["bdl_suff"] = _data_sufficiency(eng, scan["rows"], schema)
            ss["bdl_suff_sig"] = tuple(targets)
        cov, kids = ss["bdl_suff"]
    except Exception as e:
        st.caption(f"⚠️ data-sufficiency check unavailable ({e}); the skip column is manual only.")

    ss.setdefault("bdl_skips", set())
    if not scan.get("_suff_seeded"):                        # auto-X insufficient tables once per scan
        ss["bdl_skips"] = {t for t in targets if cov.get(t)}
        scan["_suff_seeded"] = True
    closure = _cascade(ss["bdl_skips"], kids)

    def _reason(tu):
        if tu in ss["bdl_skips"] and cov.get(tu):
            return ", ".join(cov[tu])
        if tu in closure and tu not in ss["bdl_skips"]:
            return "child of a skipped table"
        return ""

    grid = pd.DataFrame([{
        "file": r["file"], "→ table": (r["_target"] or "— skip —"),
        "skip": ((r["_target"] or "").upper() in closure) or not r["_target"],
        "why": _reason((r["_target"] or "").upper()),
        "match": f"{int(r['score']*100)}%", "kind": r["kind"] or "", "cols": len(r["cols"])}
        for r in scan["rows"]])
    edited = st.data_editor(
        grid, hide_index=True, use_container_width=True, key="bdl_scan_edit",
        column_config={
            "file": st.column_config.TextColumn(disabled=True),
            "→ table": st.column_config.SelectboxColumn(options=["— skip —"] + all_tables, required=True),
            "skip": st.column_config.CheckboxColumn(
                "skip", width="small", help="Drop this table and its FK children from staging & promote"),
            "why": st.column_config.TextColumn("why (uncovered required cols)", disabled=True),
            "match": st.column_config.TextColumn(disabled=True, width="small"),
            "kind": st.column_config.TextColumn(disabled=True, width="small"),
            "cols": st.column_config.NumberColumn(disabled=True, width="small")})

    # read intended target + explicit skip ticks, then cascade to FK children
    new_skips = set()
    for i, r in enumerate(scan["rows"]):
        pick = edited.iloc[i]["→ table"]
        r["_target"] = None if pick in ("— skip —", "", None) else pick.upper()
        if bool(edited.iloc[i]["skip"]) and r["_target"]:
            new_skips.add(r["_target"])
    ss["bdl_skips"] = new_skips
    closure = _cascade(new_skips, kids)

    # effective target: dropped if skipped directly or cascaded from a skipped parent
    for r in scan["rows"]:
        tgt = r["_target"]
        r["table"] = None if (not tgt or tgt in closure) else tgt
        r["kind"] = _kind_of(r["table"]) if r["table"] else None
        r["score"] = 1.0 if r["table"] else 0.0
    order = [t for t in (scan.get("order") or []) if t in {r["table"] for r in scan["rows"] if r["table"]}]
    for r in scan["rows"]:
        t = r.get("table")
        if t and t not in order:
            (order.insert(0, t) if t.startswith("DV_R_") else order.append(t))
    scan["order"] = order
    ss["bdl_scan"] = scan

    if scan["order"]:
        st.caption("Promote order (topological): " + "  →  ".join(scan["order"]))
    dropped = sorted({(r["_target"] or "").upper() for r in scan["rows"] if r.get("_target") and not r["table"]})
    if dropped:
        cascaded = sorted(closure - new_skips)
        msg = "Skipped (won't stage): " + ", ".join(dropped)
        if cascaded:
            msg += "   ·   cascaded from a skipped parent: " + ", ".join(cascaded)
        st.warning(msg)

    src = "JSON file" if (ss.get("bdl_cat") and os.path.exists(ss.get("bdl_cat", ""))) else "live introspection"
    st.caption(f"catalog: {src} · bcp in use: `{_find_bcp()}`")

    # UWI gate — right after Scan, before Stage. Blocks staging until every extracted
    # file has a valid dv_well UWI or is SKIPped.
    uwi_ready = render_review_uwi(ss, server, database, ss.get("bdl_schema", "dataview"))

    # PDF field review — edit extracted PDF fields before staging (only renders if the
    # scan produced PDF extractions). Writes corrected CSVs back so Stage loads the fixes.
    if _pdf_review is not None:
        _pdf_review.render_pdf_review(ss, ss.get("bdl_schema", "dataview"))

    if not uwi_ready:
        st.warning("Resolve the UWIs above (assign or SKIP) before staging.")
    elif st.button("Stage all to server (BCP)", type="primary"):
        eng = get_engine(server, database)
        bar = st.progress(0.0, text="staging…")
        def prog(i, n, tbl): bar.progress(i / max(n, 1), text=f"staging {tbl}")
        ss["bdl_staged"] = stage_directory(eng, server, database, scan["rows"],
                                           bulk_dir=bulk_dir, progress=prog)
        bar.progress(1.0, text="done")

    staged = ss.get("bdl_staged")
    if staged:
        st.subheader("Staged")
        st.dataframe(pd.DataFrame([{"target": r["target"], "stg table": r["stg_table"],
                                    "files": r["files"], "cols": r["cols"],
                                    "rows staged": r["loaded"],
                                    "status": "✅" if (not r["errors"] and r["loaded"]) else "🔴"}
                                   for r in staged]), hide_index=True, use_container_width=True)
        # always surface bcp's own output — the truth behind the exit code
        with st.expander("bcp output (per file)", expanded=any(r["loaded"] == 0 for r in staged)):
            for r in staged:
                for lg in r["logs"]:
                    st.markdown(f"**{r['stg_table']}** ← `{lg['file']}` (expected {lg['expected']} rows, rc={lg['rc']})")
                    st.code(lg["cmd"], language="text")
                    msg = lg["out"] or lg["err"] or "(no output)"
                    st.code(msg, language="text")
        ok = sum(1 for r in staged if not r["errors"] and r["loaded"])
        if ok == len(staged):
            st.success(f"{ok}/{len(staged)} tables staged with rows. Next: batch Match & Map (Phase 2).")
        else:
            st.error(f"{ok}/{len(staged)} tables staged with rows — see bcp output above for the rest.")

    # Phase 2 — batch Match & Map (appears once anything is staged)
    if ss.get("bdl_staged"):
        st.divider()
        render_match_map(ss, server, database, ss.get("bdl_schema", "dataview"))

    # Phase 3 — batch FK analysis (appears once maps are saved)
    if ss.get("bdl_maps"):
        st.divider()
        render_fk_analysis(ss, server, database, ss.get("bdl_schema", "dataview"))

    # Phase 4 — resolve FK violations (appears once analysis found some)
    if ss.get("bdl_fk"):
        st.divider()
        render_fk_resolution(ss, server, database, ss.get("bdl_schema", "dataview"))

    # Phase 5 — promote (appears once maps are saved)
    if ss.get("bdl_maps"):
        st.divider()
        render_promote(ss, server, database, ss.get("bdl_schema", "dataview"))

    # Phase 6 — verify staged vs loaded
    if ss.get("bdl_maps"):
        st.divider()
        render_verify(ss, server, database, ss.get("bdl_schema", "dataview"))


if __name__ == "__main__":
    if st is not None:
        run()
