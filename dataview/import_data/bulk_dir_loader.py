"""
bulk_dir_loader.py — Bulk Tabular Loader: set-based CSV/Excel directory loader
(BCP staging pipeline). Constrained to tabular intake 2026-07-20; LAS/PDF/DOCX/
DLIS/LIS/WITSML are handled by the File Catalog.

PHASE 1 (this file): scan a directory of well files, extract + fingerprint each, re-emit
each as a safe-delimited file (correct quote handling — no naive comma splitting),
auto-create an all-varchar staging table per (target, shape), and BCP-load it onto
the server. Nothing is promoted here; staging only.

Later phases build on the stg.* tables: (2) batch Match & Map, (3) set-based FK
analysis, (4) Add/Remap/Null resolution grid, (5) topo-ordered set-based promote.

Reuses page_dir_loader for the catalog/match/fingerprint logic so this pipeline and
the per-table loader agree on tables, fingerprints, and the synonym store.
"""
import os, time, contextlib, csv, subprocess, tempfile, urllib.parse

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
    # Directory Loader is CONSTRAINED TO CSV/EXCEL. The LAS/DLIS/LIS/WITSML/PDF/Word
    # extractors were removed here on 2026-07-20: those formats are owned by the File
    # Catalog (crawl + capture), and the document/log extractors were proven to
    # produce the same rows (compare_extractors.py: PDF/DOCX identical, LAS consistent).
    # Empty list => no non-tabular extraction, no missing-extractor warnings for them.
    return []


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

# Don't read a file bigger than this just to populate the "filled" column in a grid. Over the
# cap the column shows the header count only — honest about what it didn't look at.
_FILLED_MAX_BYTES = 50 * 1024 * 1024

# ── the loader's own work folder ────────────────────────────────────────────────
# Everything this loader creates — extract CSVs, .bcp files, the OCR do-later bucket — goes
# under ONE directory that we own, inside the configured bulk folder.
#
# It used to write straight into the bulk folder: _las_extract\, _pdf_extract\, and every
# .bcp file loose at the top level. That worked while C:\Bulk was scratch. It isn't: on
# 2026-07-17 it held 5,073 MB of .segy, 1,987 MB of .sgy, 1,710 MB of .csv across 632 files,
# 541 MB of .ld and 281 .las — roughly 10 GB of SOURCE DATA sharing a folder with our temp
# files. Any "delete files older than a week" pointed at that folder would have destroyed it.
#
# One containment root makes the sweep safe: it can refuse to run anywhere whose basename
# isn't this. A cleanup that can only ever delete inside a directory it created is a cleanup
# that cannot eat your seismic.
_WORK_SUBDIR = "_dv_work"


def work_dir(bulk_dir, *parts):
    """<bulk_dir>/_dv_work/<parts...> — the only place this loader writes."""
    p = os.path.join(bulk_dir, _WORK_SUBDIR, *parts)
    os.makedirs(p, exist_ok=True)
    return p
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
    # fast_executemany at the ENGINE level (every executemany benefits, not
    # just the ones that remember to set it) and no pre-ping: pre_ping costs
    # a round trip on every checkout, which is pure overhead against a local
    # instance that isn't going away mid-session. pool_recycle keeps a stale
    # connection from being handed out after a long idle. (July 31 — chasing
    # "why is everything slow".)
    return create_engine("mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(cs),
                         fast_executemany=True, pool_pre_ping=False,
                         pool_recycle=1800)

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
                _v = _clean(row[j]) if (j is not None and j < len(row)) else ""
                # canonicalize UWI to 14 chars AT THE WRITE POINT, so every staged (and
                # therefore promoted) uwi is uniform — matching the gate's _uwi14 lookups.
                if _v and str(c).strip().lower() == "uwi":
                    _v = _uwi14(_v)
                vals.append(_v)
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

    # ONE PLACE, EVERY DROPDOWN. Every target list in the mapping UI is
    # built from this map, so dropping derived columns here removes them
    # from all of them at once — and from fingerprint recall, which
    # replays saved mappings.
    table_cols, _dropped = {}, []
    for r in cols.itertuples(index=False):
        if is_derived(r.c):
            _dropped.append(f"{r.n}.{r.c}")
            continue
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
            "table_kind": {t: kind(t) for t in table_cols},
            "derived_excluded": sorted(_dropped)}


# DERIVED COLUMNS ARE NOT LOADABLE.
#
# h3_r4..h3_r7 and h3_coord_hash are COMPUTED from surface_latitude and
# surface_longitude; geog/geometry/shape are computed from the same. A file
# has no business supplying them, and letting one do so is worse than
# leaving them empty: the database can then hold a cell index that
# contradicts its own coordinates, and nothing will ever notice. A well can
# sit in an H3 cell that is not where it is.
#
# This is not hypothetical. The synthetic generator's type fallback wrote
# "h3_r4-869" into dv_well.csv, the loader mapped it faithfully, and the
# column now holds a placeholder that LOOKS like data. Worse, backfill_h3
# defaults to only_missing=True keyed on `h3_r5 IS NULL`, so those rows are
# SKIPPED by the very tool that would compute them correctly — loading the
# junk also disables the repair.
#
# So they are removed from the target list the mapping UI offers. NULL is
# honest; a wrong H3 index is not. Compute them after load with
# dataview.mapping.h3_grids.backfill_h3.
DERIVED_PREFIXES = ("h3_",)
DERIVED_EXACT = {"geog", "geometry", "shape", "h3_coord_hash"}


def is_derived(col):
    """True for a column the database computes and no file may supply."""
    c = (col or "").strip().lower()
    return c in DERIVED_EXACT or c.startswith(DERIVED_PREFIXES)


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


class _Phases:
    """Wall-clock per scan phase, so 'the scan is slow' becomes a number.

    Written because three separate theories about what made a 35s scan slow — extraction,
    hashing, OCR — were each killed by one measurement. The instrument beats the hunch.

    It also answers the only question that matters at scale: WHICH phase grows with file
    count. 35s over 65 files could be 90s or 9 minutes over 1,000, depending entirely on
    where the time sits — and on whether that phase parallelises. Hashing does (hashlib
    drops the GIL, though disk bandwidth caps it well short of core count). DLIS/LIS
    extraction does NOT, safely: frame.curves() holds whole arrays, so N workers multiply
    peak memory.
    """
    def __init__(self):
        self.t = {}
        self.n = {}

    @contextlib.contextmanager
    def phase(self, name, count=None):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.t[name] = self.t.get(name, 0.0) + (time.perf_counter() - t0)
            if count is not None:
                self.n[name] = self.n.get(name, 0) + count

    def result(self, total=None):
        rows = [{"phase": k, "seconds": round(v, 2),
                 "files": self.n.get(k), "pct": None} for k, v in self.t.items()]
        tot = total if total is not None else sum(self.t.values())
        for r in rows:
            r["pct"] = round(100.0 * r["seconds"] / tot, 1) if tot else 0.0
        rows.sort(key=lambda r: -r["seconds"])
        acct = sum(self.t.values())
        return {"phases": rows, "total": round(tot, 2), "accounted": round(acct, 2),
                "unaccounted": round(max(0.0, tot - acct), 2)}


def profile_directory_live(directory, engine, schema="dataview", bulk_dir=r"C:\Bulk",
                           recursive=False, force=False):
    """profile_directory using the JSON catalog when a path is set (fast), else live
    introspection. Adds filename-based matching for dv_r_* reference tables.

    `force` — re-extract every file even if the catalog says its content is unchanged. Needed
    whenever the EXTRACTOR changed rather than the data: the bytes are identical, so the gate
    would otherwise (correctly) skip them."""
    import json, tempfile
    _ph = _Phases()
    _t_start = time.perf_counter()
    cj = _catalog_json()
    tmp = None
    if cj is not None:
        cat = cj[3]                                           # raw catalog dict from JSON
        cat_path = st.session_state.get("bdl_cat")
        dirs = list(_iter_dirs(directory, recursive))
        with _ph.phase("profile (read + fingerprint every file)", count=len(dirs)):
            scan = pdl.profile_directory(dirs[0], cat_path)
            for d in dirs[1:]:                                # merge subfolder scans (recursive)
                sub = pdl.profile_directory(d, cat_path)
                scan["rows"].extend(sub.get("rows", []))
    else:
        with _ph.phase("catalog (live DB introspection)"):
            cat = load_catalog_live(engine, schema)           # fallback: introspect + temp file
        fd, tmp = tempfile.mkstemp(suffix=".json"); os.close(fd)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cat, fh)
        try:
            dirs = list(_iter_dirs(directory, recursive))
            with _ph.phase("profile (read + fingerprint every file)", count=len(dirs)):
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
    _ALL_EXTS = [".csv", ".xlsx", ".xlsm", ".xltx", ".xls"]   # CSV/Excel only (loader is tabular-intake)
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
                with _ph.phase("gate: hash + classify", count=len(cand)):
                    gate_dec = _gate.classify(engine, cand, root=directory, force=force)
                # upsert() returns (n, note) — older copies returned a bare int. Don't let a
                # version-skewed pair of files break a scan over a return shape.
                with _ph.phase("gate: catalog upsert"):
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
    # Promote order = a real topological sort over the FK graph (defined as _topo_order below),
    # computed ONCE after every extractor has added its rows. It used to be five separate
    # hand-maintained `order.insert/remove/append` sites — one per extractor — plus a stale
    # `order` carried across scans. That's how DV_WELL_DIR_SRVY_HDR ended up scheduled after
    # its own child DV_WELL_DIR_SRVY_STA: the stations promoted against a parent not yet
    # inserted, and every station orphaned. Position must come from the FK graph, not from the
    # order rows happen to be appended in. Provisional here; finalized after extraction.
    #
    # Use the PARSED FK catalog (child -> [{parent_table}]), which has the same shape whether
    # the catalog came from JSON or live introspection. The raw JSON dict (cat) keys its FKs
    # differently, so reading cat["fk_constraints"] would be {} on the JSON path — the topo
    # sort would silently see no parents and fall back to alphabetical, re-introducing the bug
    # on exactly the common path. _fk_parsed is authoritative.
    _fk_why = ""
    try:
        _fk_parsed = _live_catalog_parsed(engine, schema)[0] or {}
        if not _fk_parsed:
            _fk_why = "live introspection returned no FK constraints"
    except Exception as _e:
        _fk_parsed = cat.get("fk_constraints", {})            # last-ditch; never crash the scan
        _fk_why = f"live introspection failed ({type(_e).__name__}: {_e}); fell back to the catalog"
    # An empty FK graph makes _topo_order silently emit ALPHABETICAL order — every parent
    # looks unparented, so children promote ahead of their parents and the load fails on a
    # constraint that was two positions from being satisfied. It is the exact bug the block
    # above was written to prevent, so it is recorded and surfaced rather than absorbed.
    scan["fk_edges"] = sum(len(v or []) for v in _fk_parsed.values())
    scan["fk_warning"] = (_fk_why or "") if not scan["fk_edges"] else ""

    def _topo_order(rows):
        targets = {(r.get("table") or "").upper() for r in rows if r.get("table")}

        def parents(tu):
            out = set()
            for fk in _fk_parsed.get(tu, []) or _fk_parsed.get(tu.lower(), []):
                p = (fk.get("parent_table") or fk.get("parent") or "").upper()
                if p and p != tu and p in targets:       # self-refs / off-batch parents don't gate
                    out.add(p)
            return out

        out, seen, stack = [], set(), set()

        def visit(tu):
            if tu in seen or tu in stack:                # seen, or a cycle we break rather than hang
                return
            stack.add(tu)
            for p in sorted(parents(tu)):                # parents first
                visit(p)
            stack.discard(tu)
            seen.add(tu)
            out.append(tu)

        for tu in sorted(targets, key=lambda t: (0 if t.startswith("DV_R_") else 1, t)):
            visit(tu)
        return out

    scan["_topo_order"] = _topo_order                     # extractors add rows; we re-sort at the end
    scan["order"] = _topo_order(scan.get("rows", []))
    scan["all_tables"] = sorted(t.upper() for t in cat["table_cols"])

    # ── Non-CSV/Excel extraction REMOVED (2026-07-20) ──────────────────────────
    # LAS/DLIS/LIS/WITSML/PDF/Word extraction previously ran here. Removed to
    # constrain the Directory Loader to bulk CSV/Excel intake; those formats are
    # handled by the File Catalog. Only CSV/Excel rows (already in scan['rows']
    # from pdl.profile_directory above) proceed from here.

    # formats present on disk that no working extractor can read → reported, never silent
    scan["missing_extractors"] = _missing_extractors(directory, recursive)
    # Finalize the promote order NOW, after every extractor (CSV, LAS, DLIS, WITSML, PDF, DOCX)
    # has added its rows — one topological sort over the FK graph. The CSV-time sort above was
    # provisional; the extractors add tables (survey headers, log curves, DST periods) whose
    # parents must precede them, and re-sorting once here is what guarantees it. This replaces
    # five hand-maintained per-extractor `order.remove/append` blocks that had drifted out of
    # agreement — the drift is what scheduled DV_WELL_DIR_SRVY_STA ahead of its own parent.
    scan["order"] = _topo_order(scan.get("rows", []))
    # Wall clock for the whole call, so `unaccounted` is honest — it's whatever the named
    # phases don't explain (reference-table name matching, ordering, row bookkeeping, and
    # anything nobody has thought to measure yet). A big unaccounted number is a finding.
    scan["timing"] = _ph.result(total=time.perf_counter() - _t_start)
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
    """{normalized source name: db column}.

    Two sources, in priority order: dv_column_map (a human confirmed THIS
    file's mapping — strongest) then the column-level synonym store (seeded
    or learned across all files, July 31 wiring). The store fills names this
    table has never seen from a confirmed load, which is the whole point of
    having it: a new vendor's headers arrive already mapped.
    """
    out = {}
    fn = getattr(pdl, "_synonym_lookup", None)
    try:
        out = dict(fn(engine, table, valid) or {}) if fn else {}
    except Exception:
        out = {}
    try:
        from dataview.import_data import synonym_store as _sstore
        for sn, tgt in _sstore.synonyms_for(engine, "dataview", table).items():
            if valid is not None and tgt not in valid:
                continue
            if _sstore.is_system_column(tgt):
                continue
            key = pdl._norm(sn)
            if key and key not in out:        # never outrank a confirmation
                out[key] = tgt
    except Exception:
        pass
    return out

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


# ── projected X/Y → lat/long (stage-time derivation) ────────────────────────
# Files like the Teapot Dome well headers carry Northing/Easting in a projected
# CRS (NAD27 Wyoming East Central ft = EPSG 32056) and no latitude/longitude.
# Loading them as-is makes every well coordless, REQUIRE_WELL_COORDS holds them
# out of dv_well, and the EXISTS gate then holds every child row — a total
# promote stall with no error. So the conversion happens AT STAGE TIME: distinct
# N/E pairs → pyproj → temp table → ONE set-based UPDATE writing __LAT/__LON
# staging columns, which then map to surface_latitude/longitude like any source
# column. T-SQL cannot project coordinates, hence the Python pass.
_LATLON_TARGETS = [("surface_latitude", "surface_longitude"),
                   ("latitude", "longitude")]


def _detect_ne(src_cols):
    """(north_col, east_col) guessed by canonical name, else Nones. Guess only —
    the UI always shows the pick so a wrong guess is one click to fix."""
    north = east = None
    for c in src_cols:
        toks = set(_canon_col(c).split("|"))
        if north is None and "northing" in toks:
            north = c
        if east is None and "easting" in toks:
            east = c
    return north, east


def _latlon_target_pair(db_cols):
    have = {str(c).lower() for c in db_cols}
    for la, lo in _LATLON_TARGETS:
        if la in have and lo in have:
            return la, lo
    return None, None


def derive_latlong(engine, stg, north_col, east_col, epsg):
    """Reproject staged north/east (projected CRS `epsg`) into __LAT/__LON
    staging columns. Set-based: DISTINCT pairs -> pyproj (Python) -> #llmap ->
    one UPDATE...JOIN on the raw staged strings. Returns (rows_updated,
    distinct_pairs, unusable_pairs). Raises ImportError without pyproj."""
    from sqlalchemy import text
    from pyproj import Transformer
    tf = Transformer.from_crs(f"EPSG:{int(epsg)}", "EPSG:4326", always_xy=True)
    with engine.connect() as cx:
        pairs = cx.execute(text(
            f"SELECT DISTINCT LTRIM(RTRIM([{north_col}])) AS n, "
            f"LTRIM(RTRIM([{east_col}])) AS e FROM {stg} "
            f"WHERE NULLIF(LTRIM(RTRIM([{north_col}])),'') IS NOT NULL "
            f"AND NULLIF(LTRIM(RTRIM([{east_col}])),'') IS NOT NULL")).fetchall()
    rows, bad = [], 0
    for n_raw, e_raw in pairs:
        try:
            n_v = float(str(n_raw).replace(",", ""))
            e_v = float(str(e_raw).replace(",", ""))
        except ValueError:
            bad += 1
            continue
        # transform takes (x, y) = (EASTING, NORTHING); always_xy pins it.
        lon, lat = tf.transform(e_v, n_v)
        if not (abs(lat) <= 90 and abs(lon) <= 180):   # inf on failed transform
            bad += 1
            continue
        rows.append({"n": str(n_raw), "e": str(e_raw),
                     "la": f"{lat:.7f}", "lo": f"{lon:.7f}"})
    with engine.begin() as cx:
        for col in ("__LAT", "__LON"):
            cx.execute(text(
                f"IF COL_LENGTH('{stg}', '{col}') IS NULL "
                f"ALTER TABLE {stg} ADD [{col}] varchar(32) NULL"))
        n_upd = 0
        if rows:
            cx.execute(text("IF OBJECT_ID('tempdb..#llmap') IS NOT NULL DROP TABLE #llmap"))
            cx.execute(text("CREATE TABLE #llmap (n varchar(400), e varchar(400), "
                            "la varchar(32), lo varchar(32))"))
            cx.execute(text("INSERT INTO #llmap (n, e, la, lo) "
                            "VALUES (:n, :e, :la, :lo)"), rows)
            n_upd = cx.execute(text(
                f"UPDATE s SET s.[__LAT] = t.la, s.[__LON] = t.lo "
                f"FROM {stg} s JOIN #llmap t "
                f"ON LTRIM(RTRIM(s.[{north_col}])) = t.n "
                f"AND LTRIM(RTRIM(s.[{east_col}])) = t.e")).rowcount or 0
            cx.execute(text("IF OBJECT_ID('tempdb..#llmap') IS NOT NULL DROP TABLE #llmap"))
    return n_upd, len(rows), bad


# ── vendor vocabulary (RMOTC / Teapot Dome et al.) ──────────────────────────
# Files from outside this shop use their own headers ("API Number", "Common
# Well Name", "Plugback Depth") that the matcher has never been taught — the
# Teapot well headers matched almost nothing. The fix is vocabulary, not a new
# matcher: these pairs are seeded into dv_column_map, the SAME synonym store
# human confirmations land in and _synonym_lookup reads, so BOTH loaders learn
# them at once and the ranking (hits/recency) still lets a human confirmation
# outvote a seed.
#
# Each entry: NORMALIZED source header (pdl._norm: strip/upper/space→_) →
# ordered target CANDIDATES. Only the first candidate that exists on the LIVE
# table is seeded — checked against INFORMATION_SCHEMA at seed time, never a
# DDL snapshot — so a wrong guess about a column name seeds nothing rather
# than a bad synonym. Sources that exact-match after normalization (SPUD_DATE,
# COMPLETION_DATE, WELL_NAME, OPERATOR…) need no entry.
_VENDOR_SYNONYMS = {
    "DV_WELL": [
        ("API_NUMBER",        ["uwi"]),
        ("API_NO",            ["uwi"]),
        ("API",               ["uwi"]),
        ("API_#",             ["uwi"]),
        ("WELL_API",          ["uwi"]),
        ("COMMON_WELL_NAME",  ["well_name"]),
        ("WELL_NUMBER",       ["well_num", "well_number"]),
        ("WELL_STATUS",       ["current_status", "well_status", "status"]),
        ("CLASS",             ["well_class", "class_code"]),
        ("TOTAL_DEPTH",       ["total_depth", "final_td", "drill_td"]),
        ("DATUM_ELEVATION",   ["datum_elev", "datum_elevation", "kb_elev", "kb_elevation"]),
        ("DATUM_TYPE",        ["datum_type", "elev_datum"]),
        ("GROUND_ELEVATION",  ["ground_elev", "ground_elevation", "gl_elev"]),
        ("PLUGBACK_DEPTH",    ["plugback_depth", "plug_back_depth"]),
        ("LEASE_NAME",        ["lease_name"]),
        ("BASIN",             ["basin_name", "basin"]),
        ("STATE",             ["province_state", "state"]),
        ("LEGAL_SURVEY_TYPE", ["legal_survey_type"]),
        # already-converted files (xy_to_latlong / the 🧭 pass) carry these:
        ("LATITUDE",          ["surface_latitude"]),
        ("LONGITUDE",         ["surface_longitude"]),
    ],
    # Tops vocabulary: unambiguous WITHIN this table — "BASE" in a formation-
    # tops file can only mean base_depth. File-agnostic matching rightly
    # refuses one-token→two-token guesses; a per-table synonym is not a guess.
    "DV_WELL_FORMATION_TOP": [
        ("API_NUMBER",  ["uwi"]),
        ("API",         ["uwi"]),
        ("TOP",         ["top_depth"]),
        ("BASE",        ["base_depth"]),
        ("TOP_MD",      ["top_depth"]),
        ("BASE_MD",     ["base_depth"]),
        ("FORMATION",   ["formation_name", "formation"]),
        ("FM",          ["formation_name", "formation"]),
        ("HORIZON",     ["formation_name", "formation"]),
        ("PICK_DEPTH",  ["top_depth"]),
    ],
}


def seed_vendor_synonyms(engine, schema="dataview"):
    """Seed _VENDOR_SYNONYMS into dv_column_map (idempotent MERGE, keyed on
    map_id = SHA1('VENDOR|src|table|col')). Tagged source_file_pattern='VENDOR'
    / confirmed_by='VENDOR_SEED' so seeds are distinguishable from human
    confirmations and can be retired wholesale:
        DELETE FROM dataview.dv_column_map WHERE confirmed_by='VENDOR_SEED'
    Returns (n_seeded, skipped_list). Never raises."""
    if engine is None:
        return 0, []
    from sqlalchemy import text
    up = text(
        "MERGE dataview.dv_column_map AS t USING (SELECT :mid AS map_id) s "
        "ON t.map_id = s.map_id "
        "WHEN NOT MATCHED THEN INSERT (map_id, source_file_pattern, source_column, "
        "  target_table, target_column, confidence_score, mapping_method, confirmed_ind, "
        "  confirmed_by, confirmed_date, active_ind, row_created_by, row_created_date, source) "
        "VALUES (:mid,'VENDOR',:sc,:tt,:tc,0.9,'VENDOR','Y','VENDOR_SEED',SYSUTCDATETIME(),"
        "        'Y','VENDOR_SEED',SYSUTCDATETIME(),NULL);")
    seeded, skipped = 0, []
    try:
        with engine.begin() as cx:
            for tt, pairs in _VENDOR_SYNONYMS.items():
                live = {r[0].lower() for r in cx.execute(text(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t"),
                    {"s": schema, "t": tt.lower()}).fetchall()}
                if not live:
                    skipped.append(f"{tt}: table not found")
                    continue
                for sc, cands in pairs:
                    tc = next((c for c in cands if c.lower() in live), None)
                    if tc is None:
                        skipped.append(f"{tt}.{sc}: none of {cands} on the live table")
                        continue
                    mid = pdl.entity_id(f"VENDOR|{sc}|{tt}|{tc.lower()}")
                    cx.execute(up, {"mid": mid, "sc": sc, "tt": tt, "tc": tc.lower()})
                    seeded += 1
    except Exception as e:
        skipped.append(f"seed failed: {str(e)[:120]}")
    return seeded, skipped


# ── 🤖 AI-assisted table mapping ────────────────────────────────────────────
# The operator provides a HINT ("this is a well header"); the model proposes
# the target table + column map, choosing ONLY from the live catalog it is
# fed, and the reply is validated against that same catalog. Proposals land
# in the review grid as ⚠ for human confirmation — Save is what persists to
# dv_column_map, so a confirmed AI proposal teaches the synonym store exactly
# like a hand-picked one, and the deterministic matcher needs the AI less
# every time.
def _ai_api_key():
    """ANTHROPIC_API_KEY from the environment, else the repo .env — the same
    discovery page_well_map's AI filter uses, so one config governs both."""
    import os
    k = os.environ.get("ANTHROPIC_API_KEY", "")
    if k:
        return k
    try:
        with open(".env", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("ANTHROPIC_API_KEY"):
                    return line.split("=", 1)[-1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def ai_suggest_table_map(engine, schema, hint, src_cols, sample_rows,
                         review_target=None, prior=None,
                         transforms_catalog=None):
    """(table, {src: target}, notes) for one staged file, from a human hint +
    the LIVE schema. Raises with a readable message on any failure — the
    caller shows it, nothing is guessed."""
    import json as _json
    import os
    import re as _re2
    import anthropic
    key = _ai_api_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set (env or .env)")
    FKC, COLS, KIND = _live_catalog_parsed(engine, schema)

    def _toks(s):
        return {t for t in _re2.split(r"[^a-z0-9]+", str(s).lower()) if len(t) > 2}
    want = _toks(hint)
    for c in src_cols:
        want |= _toks(c)
    scored = sorted(((len(want & (_toks(t) | {w for c in cols for w in _toks(c)})), t)
                     for t, cols in COLS.items()), reverse=True)
    cand = [t for _, t in scored[:10]]
    if review_target and review_target.upper() not in cand:
        cand.insert(0, review_target.upper())
    # Tables the OPERATOR NAMES (hint or Refine feedback) are always
    # candidates — the top-10 trim is a prompt-size optimization and must
    # never outrank an explicit instruction ("The target table is
    # dv_prod_volume" was rejected because it missed the token cut, July 30).
    _named = f"{hint or ''} {(prior or {}).get('feedback', '')}".upper()
    for _t in COLS:
        if _t.upper() in _named and _t not in cand:
            cand.insert(0, _t)
    catalog = {t: sorted(c.lower() for c in COLS.get(t, set())) for t in cand}
    # Required NOT-NULL columns (no default, not identity/computed) and
    # single-col FK parents per candidate — so the model can judge what's
    # MISSING, what's GENERATABLE, and which PARENT must be populated first,
    # from the live schema rather than its imagination.
    required, parents = {}, {}
    try:
        from sqlalchemy import text as _tq
        with engine.connect() as _cq:
            for t in cand:
                try:
                    required[t] = sorted(r0[0].lower() for r0 in _cq.execute(_tq(
                        "SELECT c.name FROM sys.columns c "
                        "WHERE c.object_id=OBJECT_ID(:t) AND c.is_nullable=0 "
                        "AND c.default_object_id=0 AND c.is_identity=0 "
                        "AND c.is_computed=0"), {"t": f"{schema}.{t.lower()}"}).fetchall())
                except Exception:
                    required[t] = []
                parents[t] = sorted({f'{fk["child_cols"][0].lower()} -> {fk["parent_table"]}'
                                     for fk in FKC.get(t, []) if len(fk["child_cols"]) == 1})
    except Exception:
        pass

    sample_txt = ""
    if sample_rows:
        sample_txt = "\nSample rows (values truncated):\n" + "\n".join(
            " | ".join(str(v)[:24] for v in r) for r in sample_rows[:5])
    tf_txt = ""
    if transforms_catalog:
        # The registered row-shape transforms, so "derive the header" can be
        # a PLAN STEP with arguments — not prose in shape_note that a human
        # then translates into button clicks (Perry, July 30: "the Assistant
        # can only plan simple file-to-table — can't derive headers as part
        # of the plan"). The model SELECTS and PARAMETERIZES from this shelf;
        # it never invents transforms, and the caller validates every param.
        tf_txt = ("\nRegistered row-shape transforms (choose AT MOST ONE, "
                  "only when the file's shape mismatches the table; param "
                  "values naming columns MUST be actual source columns):\n"
                  + _json.dumps(transforms_catalog, indent=1)
                  + '\nAdd to your JSON: "transform": {"name": '
                    '"<registered name>", "params": {"<param>": "<source '
                    'column or constant>"}} — or "transform": null.\n')
    prior_txt = ""
    if prior:
        # A REVISION turn: the previous plan + the operator's objection ride
        # along, so "ask it differently" is a conversation, not a restart.
        prior_txt = ("\nYour PREVIOUS plan for this same file:\n"
                     + _json.dumps(prior.get("plan", {}), indent=1)
                     + f"\nOperator feedback on it: {prior.get('feedback', '')}\n"
                     "Produce a REVISED plan that addresses the feedback. Keep "
                     "what the feedback did not object to.\n")
    prompt = (
        "You map a source data table onto a SQL Server schema.\n"
        f"Operator hint about the file: {hint or '(none)'}\n"
        f"Source columns: {', '.join(src_cols)}\n{sample_txt}\n\n"
        "Candidate target tables and their columns (choose ONLY from these):\n"
        + _json.dumps(catalog, indent=1)
        + "\nRequired NOT-NULL columns per table:\n" + _json.dumps(required, indent=1)
        + "\nForeign keys per table (child col -> parent table):\n"
        + _json.dumps(parents, indent=1)
        + "\n\nRespond with ONLY a JSON object, no markdown fences, shaped:\n"
          '{"table": "<one candidate table>", '
          '"colmap": {"<source col>": "<target col>"}, '
          '"skip": ["<source cols with no sensible target>"], '
          '"required_gaps": [{"column": "<required col with no source>", '
          '"can_generate": true, "how": "<seq_num per X / constant / concat — or '
          'why it needs real source data>"}], '
          '"parents": [{"table": "<parent>", "why": "<one sentence: which column '
          'references it and whether it must be populated first>"}], '
          '"shape_note": "<empty string, OR one short paragraph if the file ROW '
          'SHAPE mismatches the table — e.g. one-pick-per-row vs top/base '
          'interval columns — and what transform would reconcile them>", '
          '"notes": "<one short sentence>"}\n'
          "Rules: every colmap value must be a column of the chosen table; map "
          "only when confident; unmapped sources go in skip; judge required_gaps "
          "against the actual sample values; never invent columns or tables."
        + tf_txt + prior_txt)
    model = os.environ.get("DATAVIEW_AI_MODEL", "claude-sonnet-5")
    client = anthropic.Anthropic(api_key=key)

    # Reply handling, three lessons deep (each earned live):
    #   cap check       — a capped reply truncates MID-JSON ("Unterminated
    #                     string", July 29); 8000 tokens + explicit check.
    #   fence + raw_decode — fences and trailing prose despite "ONLY JSON"
    #                     ("Extra data", July 30); parse the FIRST object.
    #   self-repair     — genuinely malformed JSON (unescaped quote →
    #                     "Expecting ',' delimiter", July 30): quote the parse
    #                     error back, ONE retry, then fail readable.
    def _ask(msgs):
        m = client.messages.create(model=model, max_tokens=8000, messages=msgs)
        if getattr(m, "stop_reason", "") == "max_tokens":
            raise RuntimeError("AI reply hit the token cap and is incomplete — "
                               "try a shorter hint, or tell Perry the cap "
                               "needs raising again")
        return "".join(b.text for b in m.content
                       if getattr(b, "type", "") == "text").strip()

    def _parse(t):
        if t.startswith("```"):
            t = t.split("```", 2)[1]
            t = t[4:] if t.lower().startswith("json") else t
        return _json.JSONDecoder().raw_decode(t.strip())[0]

    msgs = [{"role": "user", "content": prompt}]
    txt = _ask(msgs)
    try:
        data = _parse(txt)
    except Exception as _pe:
        msgs += [{"role": "assistant", "content": txt},
                 {"role": "user", "content":
                  f"That reply was not valid JSON ({str(_pe)[:120]}). Respond "
                  f"again with ONLY the corrected JSON object — no fences, no "
                  f"prose, all string values properly escaped."}]
        try:
            data = _parse(_ask(msgs))
        except Exception as _pe2:
            raise RuntimeError(f"AI returned malformed JSON twice "
                               f"({str(_pe2)[:100]}); first reply began: "
                               f"{txt[:150]!r}")
    table = str(data.get("table", "")).upper()
    if table in catalog:
        live = set(catalog[table])
    elif table in COLS:
        # A real LIVE table outside the trimmed candidate list: accept. The
        # trim keeps the prompt small; it does not define legality — legality
        # is "the table exists in the live schema", and the colmap is still
        # validated against ITS real columns.
        live = {c.lower() for c in COLS[table]}
    else:
        raise RuntimeError(f"AI chose {table!r}, which is not a live table")
    cmap, dropped = {}, []
    for s, t in (data.get("colmap") or {}).items():
        if s in src_cols and str(t).lower() in live:
            cmap[s] = str(t).lower()
        else:
            dropped.append(f"{s}→{t}")
    notes = str(data.get("notes", ""))[:300]
    if dropped:
        notes += f" (dropped invalid: {', '.join(dropped[:6])})"
    extra = {
        "transform": data.get("transform") if isinstance(data.get("transform"), dict) else None,
        "required_gaps": [g for g in (data.get("required_gaps") or [])
                          if isinstance(g, dict) and g.get("column")][:12],
        "parents": [p for p in (data.get("parents") or [])
                    if isinstance(p, dict) and p.get("table")][:8],
        "shape_note": str(data.get("shape_note", ""))[:600],
    }
    return table, cmap, notes, extra


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
    for _ri, r in enumerate(scan_rows):
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
        # skey identifies THIS SCAN ROW. It must be unique across the whole review or
        # Streamlit raises StreamlitDuplicateElementKey and Phase 2 dies outright — not a
        # warning, not a mis-map you could fix, the review screen simply refuses to render,
        # so you cannot even reach the control that would skip the offending file.
        #
        # Two earlier attempts were not enough, and each failure named the next one:
        #   skey = stg_table          -> two CSVs auto-matching the same target collided
        #                                (Completion_Parameters_Perforations.csv and
        #                                 Production_Data_..._Monthly_Production.csv both
        #                                 matched DV_WELL_GOM_BACKUP)
        #   skey = stg_table + fp     -> identical files in DIFFERENT folders collided: same
        #                                target AND same column shape, so the fingerprint is
        #                                the same by construction. This tree has eight such
        #                                filenames across sample_pdfs/ and more_pdfs/.
        # The row's own position is the only thing guaranteed distinct per scan row. The
        # path is carried too, for a key that means something when read in a traceback.
        _tag = os.path.basename(str(r.get("path") or r.get("file") or "")) or "row"
        skey = f"{stg_tbl}#{_ri}#{_tag}"
        review.append({"target": t, "skey": skey, "stg_table": stg_tbl,
                       # the row number the operator SEES in Files → tables. Warnings that say
                       # "re-target one of them" are useless without it: this tree has eight
                       # filenames that appear twice, so a name doesn't identify a row.
                       "row_no": _ri + 1,
                       "src_file": r.get("file", ""), "path": r.get("path", ""),
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
    # Belt and braces. skey is unique by construction (the row index is in it), but this has
    # now collided twice, and each time the symptom was Phase 2 refusing to render at all —
    # the worst possible failure for a review screen, because the fix lives inside the screen
    # you can no longer see. If a future edit reintroduces it, degrade rather than crash.
    _seen = {}
    for _e in review:
        _k = _e["skey"]
        if _k in _seen:
            _seen[_k] += 1
            _e["skey"] = f"{_k}#{_seen[_k]}"
        else:
            _seen[_k] = 0
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


def _dget(d, *keys):
    """Fetch a value from a staging row by column name, IGNORING CASE.

    The gate used to do d.get("LOG_ID") / d.get("UWI") — exact, uppercase. When the
    extractors were realigned to the DDL they began emitting lowercase `log_id` / `uwi`,
    every lookup returned None, _file_key returned "", the file dict came out empty, and the
    gate concluded there was nothing to resolve. It returned True and let logs with no UWI
    stage — silently. A gate that fails open because a column changed case is worse than no
    gate, because it still looks like it ran.
    """
    if not d:
        return ""
    lower = {str(k).strip().lower(): v for k, v in d.items()}
    for k in keys:
        v = lower.get(str(k).strip().lower())
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _file_key(d):
    """Which SOURCE DOCUMENT did this staging row come from?

    INVENTORY_ID first, because it is the only key that is per-DOCUMENT. Everything below it
    is per-TABLE and merely happens to be shared sometimes:

        one scout ticket -> 11 CSVs
            well header    -> _file_key = 'scout.pdf'     (FILE_PATH fallback)
            formation tops -> _file_key = 'ML_scout_1'    (INTERP_ID)
            survey header  -> _file_key = 'SRVY_scout'    (SRVY_ID)

    Three keys, one document. Assign a UWI against one and the other two never see it: they
    stay blank, hit `uwi NOT NULL`, and never load. Orphaned rows from a document whose UWI
    you already typed in.

    It works for logs today only by luck — well_log.csv and well_log_curve.csv happen to
    share LOG_ID, so the sibling rewrite reaches the curves. That is a per-table key doing a
    per-document job by coincidence of naming.

    INVENTORY_ID = file_gate.inventory_id(abspath(path)) — SHA1 of the path, 40 chars, stamped
    by the extractor on every row it emits. Same document, same key, every table.
    """
    import os as _os
    v = _dget(d, "INVENTORY_ID")
    if v:
        return v
    v = _dget(d, "LOG_ID", "SRVY_ID", "INTERP_ID")     # legacy: pre-INVENTORY_ID extracts
    if v:
        return v
    fp = _dget(d, "FILE_PATH")
    return _os.path.basename(fp) if fp else ""


def _uwi14(u):
    """Canonical UWI: strip separators/blanks, then standardize to 14 characters —
    right-pad trailing zeros when short, keep the first 14 when long. Empty stays
    empty (blank UWIs are handled by the resolution gate, not here). Used on BOTH
    the write side (build_safe_file, staging) and the match side (_uwi_exists /
    the gate) so stored and looked-up UWIs are always the same 14-digit form."""
    d = "".join(ch for ch in str(u) if ch not in "-. ").strip()
    return (d + "0" * 14)[:14] if d else ""


# _desep kept as an alias — historically "de-separate only"; now also standardizes
# to 14 so match and storage agree. Any remaining caller gets the canonical form.
def _desep(u):
    return _uwi14(u)


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
    """Every extracted CSV whose rows can be tied to a source document.

    Was: log-shaped rows only — `"curve" not in stg_table` and a UWI column required. That
    excluded most of what a PDF produces (formation tops, survey stations, casing...), so
    those tables never entered the gate at all: no UWI inspected, no UWI stamped, rows
    orphaned. Any CSV carrying INVENTORY_ID or UWI is gateable.
    """
    out = []
    for r in scan.get("rows", []):
        if not r.get("extracted"):
            continue
        cols = [str(c).upper() for c in (r.get("cols") or [])]
        if "INVENTORY_ID" in cols or "UWI" in cols:
            out.append(r)
    return out


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

    # INVENTORY_ID → the real filename. The gate already built {abspath: inventory_id} when it
    # hashed the directory, so invert it: no extractor needs to emit a path column, and it
    # works for every format at once. Without this the grid shows the operator a 40-char SHA1
    # and asks them to identify it — the PDF extractor emits no FILE_PATH, so there was
    # nothing else to fall back to.
    _iid_path = {}
    for _p, _i in ((scan.get("gate") or {}).get("ids") or {}).items():
        _iid_path.setdefault(str(_i), _p)

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
                if not lg:
                    continue
                uwi = _dget(d, "UWI")
                wn = _dget(d, "WELL_NAME")
                # A document now contributes rows from SEVERAL CSVs. setdefault() would let
                # whichever table happened to be read first define the entry — and a curve or
                # station row carries no WELL_NAME, so the operator would be asked to identify
                # a document with nothing to identify it BY. Merge: first non-blank wins.
                v = files.get(lg)
                if v is None:
                    v = {"log_id": lg, "uwi": "", "well_name": "", "fmt": "",
                         "label": "", "paths": set(), "assigned": "", "skip": False}
                    files[lg] = v
                v["uwi"] = v["uwi"] or uwi
                v["well_name"] = v["well_name"] or wn
                v["fmt"] = v["fmt"] or (r.get("extracted") or "").upper()
                v["assigned"] = v["assigned"] or uwi
                v["paths"].add(r["path"])
                # what the OPERATOR sees. INVENTORY_ID is a 40-char hash — useless in a grid.
                # Prefer the real path from the gate's id map; then a path column if the
                # extractor emits one; then the per-table key. Include the parent folder:
                # this directory has eight filenames that appear twice from different
                # folders (Survey_ANADARKO_1H_Landmark.pdf, ...) with DIFFERENT content, so
                # the basename alone would show two identical-looking rows.
                if not v["label"]:
                    fp = _iid_path.get(lg) or _dget(d, "FILE_PATH")
                    if fp:
                        _par = os.path.basename(os.path.dirname(fp))
                        v["label"] = (f"{_par}\\{os.path.basename(fp)}" if _par
                                      else os.path.basename(fp))
                    else:
                        v["label"] = (_dget(d, "LOG_ID", "SRVY_ID", "INTERP_ID")
                                      or lg[:12] + "…")
        # validate EVERY present UWI against dv_well in one query; blank or not-found → must resolve
        # Remembered operator assignments first: a UWI typed into this gate before is keyed to
        # the document's INVENTORY_ID, so it comes back on a re-run, a Reset, or a restart.
        # It OVERRIDES what the file carried — an operator looking at the document beats a
        # parser guessing at it. `origin` records which, so the grid can say so rather than
        # silently presenting a remembered value as if the file supplied it.
        for v in files.values():
            v["origin"] = "file" if v["uwi"] else "—"
        try:
            if _gate is not None:
                saved = _gate.get_identity(eng, list(files.keys()))
                for lg, u in saved.items():
                    v = files.get(lg)
                    if v and u:
                        v["uwi"] = u
                        v["assigned"] = u
                        v["origin"] = "saved"
        except Exception:
            pass                                   # remembering is a convenience, never a gate
        present = _uwi_exists(eng, {v["uwi"] for v in files.values() if v["uwi"]}, schema)
        for v in files.values():
            v["valid"] = bool(v["uwi"]) and _desep(v["uwi"]) in present
        ss["bdl_uwi_files"] = files

    # Outcome of the previous "Validate & apply", stashed because the st.rerun() that follows
    # it discards anything rendered in that pass. Shown here — including on the run where the
    # gate closes, so "Applied. All files resolved" is actually visible.
    _msg = ss.pop("bdl_uwi_msg", None)
    if _msg:
        (st.error if _msg[0] == "error" else st.success)(_msg[1])

    def _stamp_csvs(files):
        """Write each document's resolved UWI into every extract CSV row it produced, and drop
        skipped documents' rows entirely.

        This lives OUT here, not inside the Validate & apply button, because the gate can now
        resolve with no click at all: saved assignments are read back from
        GLOBAL_FILE_CATALOG.UWI14 and prefilled, every document comes up valid, `unresolved` is
        empty, and the gate returns True. Before assignments persisted, a click was the only
        way to resolve — so the stamp lived in the click and that was fine. Adding persistence
        removed the click and took the stamp with it: the CSVs kept their blank uwi and staged
        1,610 blanks into a NOT NULL column. The gate said resolved; the data said otherwise.
        """
        paths = set()
        for v in files.values():
            paths |= v["paths"]

        def _rewrite(path):
            try:
                hdr, data = _read_uwi_rows(path)
            except Exception:
                return
            # find the UWI column AS IT IS SPELLED in this file. Extractors emit lowercase
            # `uwi` since the DDL alignment; older ones emit `UWI`. Testing for the literal
            # "UWI" made this bail on every realigned extract — so the assigned UWIs were
            # never stamped and skipped files were never dropped, with no error either way.
            ucol = next((h for h in (hdr or []) if str(h).strip().lower() == "uwi"), None)
            if not ucol:
                return
            keep = []
            for d in data:
                lg = _file_key(d)
                v = files.get(lg)
                if v and v["skip"]:
                    continue                                   # drop skipped file's rows
                if v and v["uwi"]:
                    d[ucol] = v["uwi"]                          # stamp/overwrite the resolved UWI
                keep.append(d)
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=hdr); w.writeheader(); w.writerows(keep)

        # rewrite every extract CSV in the extract folders (log AND curve), matched by document
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

    unresolved = [v for v in files.values() if not v.get("valid") and not v["skip"]]
    ss["bdl_uwi_pending"] = [v["label"] for v in unresolved]
    if not unresolved:
        # Resolved without a click — every document's UWI came from its own header or from a
        # saved assignment. The CSVs still hold whatever the extractor wrote (blank, for the
        # saved case), so they MUST be stamped before staging. Once per scan: _stamp_csvs is
        # idempotent, but re-reading every extract CSV on each Streamlit rerun is not free.
        # Stamp once per version of the files on disk — not once per session.
        #
        # This was a boolean, and a boolean has to be cleared by hand at every point the world
        # changes. It wasn't: a re-scan rewrote every extract CSV with a blank uwi, the flag
        # was still True from the previous scan, so the stamp was skipped and the gate happily
        # reported "resolved" over blank data. The screen said one thing, the CSV said another.
        #
        # A signature over (path, mtime) can't go stale the way a flag can: stamping bumps the
        # mtimes, so the signature we record afterwards is the signature of the STAMPED files.
        # A rerun matches it and skips. A scan rewrites them, the signature stops matching, and
        # the stamp runs again — with no one having to remember to clear anything.
        def _sig(paths):
            out = []
            for p in sorted(paths):
                try:
                    out.append((p, os.path.getmtime(p)))
                except OSError:
                    out.append((p, 0))
            return tuple(out)

        _paths = set()
        for _v in files.values():
            _paths |= _v["paths"]
        if ss.get("bdl_uwi_stamp_sig") != _sig(_paths):
            try:
                _stamp_csvs(files)
                ss["bdl_uwi_stamp_sig"] = _sig(_paths)     # signature of the STAMPED files
            except Exception as e:
                st.error(f"Couldn't write the resolved UWIs into the extract CSVs: {e}  "
                         f"Staging would load blank UWIs — fix this before continuing.")
                ss["bdl_uwi_pending"] = None       # NOT "go assign something" — see the error
                return False
        # Don't just vanish. Before assignments persisted, "no gate" meant "every file carried
        # its own valid UWI" — nothing to show. Now it can also mean "we silently reused what
        # you typed last time", and that is worth being able to SEE. It also makes FORGET
        # reachable: an assignment you can't find is an assignment you can't take back, and
        # these are currently random wells picked to watch the data flow.
        _saved = [v for v in files.values() if v.get("origin") == "saved" and not v["skip"]]
        _shown = [v for v in files.values() if not v["skip"]]
        if _shown:
            _n_saved = len(_saved)
            with st.expander(
                    f"✅ UWI gate — {len(_shown)} document(s) resolved"
                    + (f", {_n_saved} from a saved assignment" if _n_saved else ""),
                    expanded=False):
                st.caption("Every document has a UWI that exists in **dv_well**, so staging "
                           "isn't blocked. `saved` means you assigned it on a previous run and "
                           "it was remembered — the file itself didn't carry it.")
                _g = pd.DataFrame([{"action": "keep", "UWI": v["uwi"], "document": v["label"],
                                    "format": v["fmt"], "tables": len(v["paths"]),
                                    "UWI from": v.get("origin", "—")} for v in _shown])
                _e = st.data_editor(
                    _g, hide_index=True, use_container_width=True, key="bdl_uwi_done",
                    column_order=["action", "UWI", "UWI from", "document", "tables", "format"],
                    column_config={
                        "action": st.column_config.SelectboxColumn(
                            "action", options=["keep", "FORGET"], required=True, width="small",
                            help="FORGET erases the remembered assignment for this document. "
                                 "Use it when a UWI was assigned just to watch the data flow."),
                        "UWI": st.column_config.TextColumn(disabled=True, width="small"),
                        "UWI from": st.column_config.TextColumn(disabled=True, width="small"),
                        "document": st.column_config.TextColumn(disabled=True),
                        "tables": st.column_config.NumberColumn(disabled=True, width="small"),
                        "format": st.column_config.TextColumn(disabled=True, width="small")})
                if st.button("Apply", key="bdl_uwi_done_apply"):
                    _forget = [v["log_id"] for v, r in zip(_shown, _e.to_dict("records"))
                               if str(r.get("action") or "").upper() == "FORGET"]
                    if _forget and _gate is not None:
                        try:
                            _gate.set_identity(eng, {lg: "" for lg in _forget})
                            for lg in _forget:
                                files[lg]["uwi"] = files[lg]["assigned"] = ""
                                files[lg]["valid"] = False
                                files[lg]["origin"] = "—"
                            ss["bdl_uwi_files"] = files
                            ss.pop("bdl_uwi_stamp_sig", None)  # they must be re-stamped
                            ss["bdl_uwi_msg"] = ("success", f"Forgot {len(_forget)} "
                                                            f"assignment(s) — they're back in "
                                                            f"the gate above.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Couldn't forget: {e}")
                    elif not _forget:
                        st.caption("Nothing marked FORGET.")
        return True                                            # all resolved → staging may proceed

    st.divider()
    st.header("⚠ Assign UWI before staging")
    st.caption("These extracted files have no UWI — or a UWI that isn't in **dv_well**. Assign one "
               "that exists in dv_well, or check **SKIP** to drop the file from this run. Staging is "
               "blocked until every file is resolved. Skipped files never stage.")

    grid = pd.DataFrame([{"action": "SKIP" if v["skip"] else "keep",
                          "assign UWI": v["assigned"],
                          "document": v["label"], "format": v["fmt"],
                          "well name": v["well_name"], "header UWI": v["uwi"] or "—",
                          "tables": len(v["paths"]), "UWI from": v.get("origin", "—")}
                         for v in unresolved])
    edited = st.data_editor(
        grid, hide_index=True, use_container_width=True, key="bdl_uwi_gate",
        column_order=["action", "assign UWI", "document", "tables", "well name", "format",
                      "header UWI", "UWI from"],
        column_config={
            "action": st.column_config.SelectboxColumn(
                "action", options=["keep", "NEW", "SKIP", "FORGET"], required=True,
                width="small",
                help="keep = attach to a UWI that already exists in dv_well.  "
                     "NEW = this document ESTABLISHES a well not yet in dv_well (a scout "
                     "ticket for a brand-new well) — the UWI is accepted and the well is "
                     "created on promote.  SKIP = drop this document from the run.  "
                     "FORGET = erase the remembered assignment for it."),
            "assign UWI": st.column_config.TextColumn(help="a UWI that exists in dv_well (keep) "
                                                           "or a new 14-digit API-14 (NEW) — it is "
                                                           "stamped on EVERY row this document "
                                                           "produced, in every target table, and "
                                                           "remembered for the next run"),
            "document": st.column_config.TextColumn(disabled=True),
            "tables": st.column_config.NumberColumn(disabled=True, width="small",
                                                    help="how many target tables this document "
                                                         "feeds — one decision covers all of them"),
            "format": st.column_config.TextColumn(disabled=True, width="small"),
            "well name": st.column_config.TextColumn(disabled=True),
            "header UWI": st.column_config.TextColumn(disabled=True, width="small",
                                                      help="what the file carried"),
            "UWI from": st.column_config.TextColumn(
                disabled=True, width="small",
                help="file = extracted from the document · saved = you assigned it on a "
                     "previous run")})

    if st.button("Validate & apply", type="primary"):
        # Map edits back BY POSITION, not by the display string. The key is now INVENTORY_ID
        # (a hash) while the grid shows a filename — and this directory has eight filenames
        # that appear twice from different folders. data_editor preserves row order, so zip()
        # is exact where a string lookup would be a guess.
        rowlist = list(edited.to_dict("records"))
        pairs = list(zip(unresolved, rowlist))
        assigns = {v["log_id"]: (r.get("assign UWI") or "").strip() for v, r in pairs}
        acts = {v["log_id"]: str(r.get("action") or "").upper() for v, r in pairs}
        skips = {k: (a == "SKIP") for k, a in acts.items()}
        forgets = [k for k, a in acts.items() if a == "FORGET"]
        # NEW = the operator asserts this document establishes a well not yet in dv_well.
        # A scout ticket is frequently the FIRST record of a well, so requiring the well to
        # pre-exist is backwards. But an unknown UWI must NOT pass silently — that turns a typo
        # into a phantom well. So it's an explicit action: `keep` still requires the UWI to
        # exist; `NEW` accepts an absent one and flags the well to be created on promote.
        news = {k for k, a in acts.items() if a == "NEW"}
        good = _uwi_exists(eng, {u for lg, u in assigns.items() if u and not skips.get(lg)}, schema)
        invalid = []
        for lg, v in files.items():
            if lg not in assigns:
                continue
            v["skip"] = skips.get(lg, False)
            u = assigns.get(lg, "").strip(); v["assigned"] = u
            v["new_well"] = False
            if v["skip"]:
                continue
            if u and _desep(u) in good:
                v["uwi"] = _desep(u); v["valid"] = True         # exists in dv_well
            elif u and lg in news:
                # operator asserts a new well: accept the UWI, mark it to be created. The
                # document's well-header row promotes into dv_well and every child attaches to
                # it. Basic sanity only — a UWI is 14 digits after de-separation; anything else
                # is almost certainly a typo, not a new well.
                du = _desep(u)
                if du.isdigit() and len(du) == 14:
                    v["uwi"] = du; v["valid"] = True; v["new_well"] = True
                else:
                    invalid.append(f"{v['label']} (‘{u}’ isn’t a 14-digit UWI — "
                                   f"NEW needs a valid API-14)")
            elif u:
                invalid.append(f"{v['label']} (‘{u}’ not in dv_well — set action to NEW to "
                               f"create the well, or fix the UWI)")
        # Remember the VALID assignments against the document's INVENTORY_ID — and erase the
        # ones marked FORGET. Only ever UWI14; MATCHED_UWI / MATCH_METHOD / TRIAGE_* / PROC_*
        # belong to the pipeline and are not touched.
        try:
            if _gate is not None:
                _save = {lg: files[lg]["uwi"] for lg in assigns
                         if files.get(lg, {}).get("valid") and not files[lg]["skip"]}
                _save.update({lg: "" for lg in forgets})
                for lg in forgets:                       # clear it here too, not just in the DB
                    if lg in files:
                        files[lg]["uwi"] = files[lg]["assigned"] = ""
                        files[lg]["valid"] = False
                        files[lg]["origin"] = "—"
                if _save:
                    _gate.set_identity(eng, _save)
                    _n_f = len(forgets)
                    st.caption(f"💾 remembered {len(_save) - _n_f} assignment(s)"
                               + (f", forgot {_n_f}" if _n_f else "")
                               + " — keyed to the document, so a re-run won't ask again.")
        except Exception as e:
            st.caption(f"⚠ couldn't save assignments to the file catalog: {e}  "
                       f"(this run is unaffected — you'd just be asked again next time)")
        # stamp assigned UWIs into every extract CSV, and DROP skipped documents' rows
        _stamp_csvs(files)
        _p = set()
        for _v in files.values():
            _p |= _v["paths"]
        ss["bdl_uwi_stamp_sig"] = tuple(sorted(
            (x, (os.path.getmtime(x) if os.path.exists(x) else 0)) for x in _p))
        ss["bdl_uwi_files"] = files
        # st.rerun() RAISES — anything rendered here is discarded before it reaches the
        # screen. So st.error("Not found in dv_well: ...") was written and never seen: you
        # assigned a UWI, the gate correctly refused it, and the only symptom was the gate
        # re-appearing with no explanation. Stash the outcome and render it after the rerun.
        if invalid:
            ss["bdl_uwi_msg"] = ("error", "Not found in **dv_well** — fix or SKIP: "
                                          + ", ".join(invalid))
        else:
            ss["bdl_uwi_msg"] = ("success", "Applied. All files resolved — you can Stage now.")
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
            # Vendor vocabulary first, so this very build already benefits.
            # Once per session; the MERGE is idempotent anyway.
            if not ss.get("bdl_vendor_seeded"):
                _n_seed, _seed_skips = seed_vendor_synonyms(eng, schema)
                ss["bdl_vendor_seeded"] = True
                if _n_seed:
                    st.caption(f"synonym store: {_n_seed} vendor pair(s) present "
                               f"(RMOTC/Teapot vocabulary)")
                for _sk in _seed_skips:
                    st.caption(f"  (vendor synonym skipped — {_sk})")
            review = build_map_review(eng, scan["rows"], schema, with_data=True)
            # Carry forward what's still in the scan — and ONLY that. `maps` used to accumulate
            # and never drop, so a table auto-mapped here BEFORE you ticked its skip in
            # Files → tables stayed in the plan for the rest of the session. Phase 5 then tried
            # to promote it and hit `Invalid object name 'stg.dv_well_stimulation'` — the table
            # was never staged, because you skipped it. The decision outlived the thing it was
            # about, which is the same failure as a stale dv_column_map rule.
            _live = {r["skey"] for r in review}
            _prev_maps = dict(ss.get("bdl_maps", {}))
            maps = {k: v for k, v in _prev_maps.items() if k in _live}
            meta = {k: v for k, v in dict(ss.get("bdl_mapmeta", {})).items() if k in _live}
            _funcs_prev = dict(ss.get("bdl_functions", {}))
            ss["bdl_functions"] = {k: v for k, v in _funcs_prev.items() if k in _live}
            _dropped = [k for k in _prev_maps if k not in _live]
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
            if _dropped:
                st.caption(f"Dropped {len(_dropped)} mapping(s) from a previous pass — those "
                           f"files are no longer in the scan (skipped, re-targeted, or gone).")
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
                   f"review. ⚠ Auto-mapping is PER-RUN: press 💾 **Save all mappings** below "
                   f"to REMEMBER these shapes (fingerprint recall next scan); unsaved shapes "
                   f"are re-decided every time.")
    # Render the editor for EVERY table, not just the ones needing review: an exact match is
    # a good default, not a decision the operator is stuck with. Auto tables stay collapsed.

    # Two scan rows landing on the same staging table only CLASH if their column shapes differ.
    # Identical shapes are normal and handled: stage_directory groups files by (target,
    # fingerprint) and bcp's them into one table together — that is what a group's `files` list
    # is for. Warning on those was wrong, and worse than useless: it told you three files
    # "would stage into the same table with different column shapes" while listing the same
    # 11-col file twice.
    _by_stg = {}
    for r in review:
        _by_stg.setdefault(r["stg_table"], []).append(r)
    _clash = {k: v for k, v in _by_stg.items()
              if len({x["fp"] for x in v}) > 1}          # >1 DISTINCT shape, not >1 file
    if _clash:
        for stg, rs in _clash.items():
            # group by shape so the message shows the real split, and disambiguate same-named
            # files by their folder (this tree has eight filenames that appear twice)
            shapes = {}
            for x in rs:
                shapes.setdefault(x["fp"], []).append(x)
            bullets = []
            for _fp, xs in shapes.items():
                names = []
                for x in xs:
                    p = x.get("path") or ""
                    par = os.path.basename(os.path.dirname(p)) if p else ""
                    nm = x.get("src_file") or os.path.basename(p) or "?"
                    tag = f"**#{x['row_no']}**" if x.get("row_no") else ""
                    names.append(f"{tag} `{par}\\{nm}`" if par else f"{tag} `{nm}`")
                bullets.append(f"- **{len(xs[0]['src_cols'])} cols** — " + ", ".join(names))
            _nums = ", ".join(f"#{x['row_no']}" for x in rs if x.get("row_no"))
            st.warning(
                f"⚠ **{len(rs)} files map to `{rs[0]['target']}` in "
                f"{len(shapes)} different column shapes**, and would stage into the same table "
                f"`{stg}`. `create_stg` does DROP+CREATE, so the last shape wins and the "
                f"others' rows are lost:\n\n"
                + "\n".join(bullets)
                + f"\n\n**Fix:** in **Files → tables** above, find row {_nums} and either "
                  f"change its **→ table** or tick its **skip**. (Files with the SAME shape "
                  f"are fine — they stage together.)")

    # ── 🤖 AI assist — a hint + the live schema → a proposed mapping ─────────
    with st.expander("🤖 AI assist — describe a file, get a proposed mapping",
                     expanded=False):
        st.caption("Give a hint like `well header`, `formation tops`, `deviation "
                   "survey` and the AI proposes the target table and column map — "
                   "choosing only from the live schema. Proposals land in the grid "
                   "below as ⚠ for YOUR review; nothing persists until 💾 Save, and "
                   "Save teaches dv_column_map, so next time the deterministic "
                   "matcher won't need the AI for this shape at all.")
        _ai_lbl = {r["skey"]: (r.get("src_file") or r["target"]) for r in review}
        _ai_pick = st.selectbox("File / staged table",
                                [_ai_lbl[r["skey"]] for r in review], key="bdl_ai_pick")
        _ai_hint = st.text_input("Hint (what IS this table?)", key="bdl_ai_hint",
                                 placeholder="e.g. well header for Teapot Dome wells")
        if st.button("🤖 Propose mapping", key="bdl_ai_go"):
            _r = next(x for x in review if _ai_lbl[x["skey"]] == _ai_pick)
            eng = get_engine(server, database)
            _stg = (ss.get("bdl_mapmeta", {}).get(_r["skey"])
                    or (None, _r.get("stg_table")))[1]
            _samples = []
            try:
                if _stg:
                    from sqlalchemy import text as _t
                    with eng.connect() as _c:
                        _samples = [tuple(row) for row in _c.execute(
                            _t(f"SELECT TOP 5 * FROM {_stg}")).fetchall()]
            except Exception:
                _samples = []
            try:
                _t_ai, _cmap_ai, _notes, _extra = ai_suggest_table_map(
                    eng, schema, _ai_hint, list(_r["src_cols"]), _samples,
                    _r.get("target"))
            except Exception as _ae:
                st.error(f"AI assist failed: {str(_ae)[:250]}")
            else:
                ss["bdl_ai_prop"] = {"skey": _r["skey"], "table": _t_ai,
                                     "cmap": _cmap_ai, "notes": _notes,
                                     "extra": _extra}
        _prop = ss.get("bdl_ai_prop")
        if _prop:
            _r = next((x for x in review if x["skey"] == _prop["skey"]), None)
            if _r is not None:
                st.markdown(f"**Proposal for `{_ai_lbl.get(_prop['skey'], _prop['skey'])}` "
                            f"→ {_prop['table']}**  \n{_prop['notes']}")
                st.dataframe(pd.DataFrame(
                    [{"source": s, "→ column": t} for s, t in _prop["cmap"].items()]
                    or [{"source": "(nothing mapped)", "→ column": ""}]),
                    hide_index=True, use_container_width=True)
                _ex = _prop.get("extra") or {}
                if _ex.get("shape_note"):
                    st.warning("📐 " + _ex["shape_note"])
                if _ex.get("required_gaps"):
                    st.markdown("**Required columns with no source:**")
                    st.dataframe(pd.DataFrame([{
                        "column": g.get("column", ""),
                        "generatable": "✅" if g.get("can_generate") else "✗",
                        "how": g.get("how", "")} for g in _ex["required_gaps"]]),
                        hide_index=True, use_container_width=True)
                    st.caption("Generatable ones go in ④ Derived columns "
                               "(seq_num/constant/concat) — the AI's `how` is "
                               "the suggested rule, verify before saving.")
                for _p in (_ex.get("parents") or []):
                    st.info(f"⛓ **{_p.get('table')}** — {_p.get('why', '')}")
                if _prop["table"] != _r["target"].upper():
                    st.warning(f"AI reads this file as **{_prop['table']}**, but the scan "
                               f"staged it as **{_r['target']}**. Change its → table in "
                               f"Files → tables (Phase 1) and rebuild; a column map only "
                               f"applies to the table it was made for.")
                elif st.button("✅ Apply to the grid below (still needs your 💾 Save)",
                               key="bdl_ai_apply"):
                    for _row in _r["rows"]:
                        _t_new = _prop["cmap"].get(_row["source"])
                        if _t_new and _row["status"] not in ("exact", "confirmed"):
                            _row["target"] = _t_new
                            _row["status"] = "ai"          # not settled -> shows ⚠
                    ss["bdl_review"] = review
                    ss["bdl_grid_ver"] = int(ss.get("bdl_grid_ver", 0)) + 1
                    ss.pop("bdl_ai_prop", None)
                    st.rerun()

    with st.form("bdl_phase2"):
        editors = {}
        fn_editors = {}
        skip_tbl = {}
        for r in review:
            n_exc = len(r["exceptions"])
            flag = "⚠" if (n_exc or r["required_missing"] or r.get("dropped_cols")) else "✅"
            settled = ("exact", "confirmed", "skip")
            slabel = r["target"] if r["stg_table"] == stg_name(r["target"].upper()) \
                else f"{r['target']} ⟵ {r['stg_table'].split('.')[-1]}"
            _no = f"#{r['row_no']}  " if r.get("row_no") else ""
            with st.expander(f"{flag}  {_no}{slabel}  ·  {len(r['src_cols'])} cols, "
                             f"{n_exc} to review",
                             expanded=bool(n_exc or r["required_missing"] or r.get("dropped_cols"))):
                # Skip HERE, not only in Files → tables at the top of the page. This is where
                # you find out a table is wrong — a mis-matched target, a shape clash, a file
                # you never meant to load — so this is where the decision belongs.
                skip_tbl[r["skey"]] = st.checkbox(
                    f"⏭ Skip {_no}**{r['target']}**"
                    + (f"  ⟵ `{r.get('src_file')}`" if r.get("src_file") else "")
                    + " — don't map, don't promote",
                    key=f"bdlskip_{r['skey']}",
                    help="The staged rows stay in the staging table; they just never reach "
                         "dataview. Nothing is deleted.")
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
                    grid, hide_index=True, use_container_width=True, key=f"bdlmap_{r['skey']}_v{ss.get('bdl_grid_ver', 0)}",
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
                    key=f"bdlfn_{r['skey']}_v{ss.get('bdl_grid_ver', 0)}",
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
        _skipped = [k for k, v in (skip_tbl or {}).items() if v]
        for skey, (target, ed, src_cols) in editors.items():
            if skey in _skipped:
                # Skipped here — drop it from the plan entirely. Also remove any map saved on
                # a previous pass, or an earlier decision would quietly outlive this one.
                maps.pop(skey, None)
                funcs_all.pop(skey, None)
                continue
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
        if _skipped:
            _lbl = {r["skey"]: (r.get("src_file") or r["target"]) for r in review}
            st.info("⏭ Skipped (won't promote): "
                    + ", ".join(f"`{_lbl.get(k, k)}`" for k in _skipped)
                    + ".  Their staged rows are untouched — nothing was deleted. Clear the "
                      "checkbox and Save again to put one back.")


def _assert_work_root(root):
    """Refuse to delete anything under a path that isn't our own work folder.

    Containment already comes from construction — sweep_work builds
    <bulk_dir>/_dv_work and walks only that. So this can't fire today, and a check that
    can't fail is worth being honest about: it is a TRIPWIRE for a future edit, not a
    guarantee about the present. The day someone passes a root in from outside, or renames
    the subdir, this is what stops a recursive delete from running somewhere real. Given the
    folder next door holds ~10 GB of seismic, a tripwire is cheap.
    """
    norm = os.path.normpath(root)
    if os.path.basename(norm) != _WORK_SUBDIR:
        raise ValueError(f"refusing to sweep {root!r}: not the loader's work folder "
                         f"(basename must be {_WORK_SUBDIR!r})")
    parent = os.path.dirname(norm)
    if not parent or parent == norm:
        raise ValueError(f"refusing to sweep {root!r}: no parent directory — a bare or "
                         f"filesystem-root work folder is never what was meant")
    return norm


def sweep_work(bulk_dir, days=7, dry_run=True, keep_do_later=True):
    """Delete loader artifacts older than `days`. Returns [(path, age_days, bytes)].

    dry_run=True lists what WOULD go and touches nothing. That is the default, on purpose.

    What this function is really about is where it CANNOT reach:

      * It only walks <bulk_dir>/_dv_work/ — a folder this loader created. The bulk folder
        itself is never touched. C:\\Bulk holds ~10 GB of SOURCE DATA — 5,073 MB of .segy,
        1,987 MB of .sgy, 1,710 MB of .csv, 281 .las. "Delete everything older than a week"
        pointed at that folder would be a catastrophe, and it is exactly what gets written
        by someone who didn't look first.
      * It refuses to follow symlinks/junctions out of the tree.
      * The OCR do-later bucket is kept by default: those documents were deferred FOR work,
        and a queue that empties itself after a week isn't a queue.

    The extract CSVs are safe to lose — the next scan rewrites them. But they're also the
    evidence when staging_qa reports a blank column, so this is opt-in, never automatic.
    """
    import time as _t
    root = _assert_work_root(os.path.join(bulk_dir, _WORK_SUBDIR))
    if not os.path.isdir(root):
        return []
    cutoff = _t.time() - days * 86400
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.islink(dirpath):
            dirnames[:] = []
            continue
        if keep_do_later and "_do_later" in dirpath.split(os.sep):
            continue
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                if os.path.islink(p):
                    continue
                stt = os.stat(p)
                if stt.st_mtime >= cutoff:
                    continue
                age = (_t.time() - stt.st_mtime) / 86400.0
                out.append((p, round(age, 1), stt.st_size))
                if not dry_run:
                    os.remove(p)
            except OSError:
                continue
    if not dry_run:                       # drop directories left empty, never the root
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if os.path.normpath(dirpath) == os.path.normpath(root):
                continue
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass
    return out


def work_usage(bulk_dir):
    """(n_files, bytes) under the work folder — nothing outside it."""
    root = os.path.join(bulk_dir, _WORK_SUBDIR)
    n = tot = 0
    for dirpath, _d, filenames in os.walk(root):
        for fn in filenames:
            try:
                tot += os.path.getsize(os.path.join(dirpath, fn))
                n += 1
            except OSError:
                pass
    return n, tot


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
            # .bcp files used to sit loose in the bulk folder, mixed in with source data
            safe = os.path.join(work_dir(bulk_dir, "_bcp"), safe_name + ".bcp")
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


def _computed_cols(engine, table, schema="dataview"):
    """Lowercase names of COMPUTED columns — these cannot be written.

    SQL Server rejects the whole statement with "The column ... cannot be
    modified because it is either a computed column or is the result of a UNION
    operator" (271), so a single source header matching a computed column fails
    the ENTIRE table load, not just that column. dv_well_formation_top
    .gross_thickness is one, derived from base_depth minus top_depth.
    """
    import pandas as pd
    from sqlalchemy import text
    try:
        df = pd.read_sql(text("SELECT name FROM sys.columns "
                              "WHERE object_id=OBJECT_ID(:t) AND is_computed=1"),
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

def _uwi14_sql(expr: str) -> str:
    """The UWI-14 pad, as ONE expression both sides of an FK comparison use.

    promote right-pads uwi to 14 (build_promote_sql). Any check comparing a
    child value against a parent must apply the SAME transform to BOTH, or it
    compares a padded 12-digit API against an unpadded one and concludes they
    differ. That is the third site this pad has to agree on, after
    build_promote_sql and the repair UPDATE — and it is why every one of
    Teapot's 1,188 staged wells reported as unmatched while sitting in the
    staging table two panels up the same screen.
    """
    return (f"CASE WHEN NULLIF({expr}, '') IS NULL THEN NULL "
            f"ELSE LEFT(CONCAT({expr}, REPLICATE('0', 14)), 14) END")


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
                if child_col == "uwi":
                    # The analysis MUST compare what promote will write, or it
                    # lies in both directions. Promote pads uwi to UWI-14
                    # (build_promote_sql); comparing the unpadded 12-digit API
                    # against a repaired 14-char dv_well.uwi reported every
                    # Teapot top as "unmatched" (July 29). Same expression,
                    # same semantics as _uwi14.
                    match = _uwi14_sql(match)
                    disp = match

            # A parent staged in THIS batch will exist by the time the child is
            # promoted — Phase 1 already put it earlier in the topological order.
            # Checking only the live target reports every parent as missing on a
            # freshly reset schema, which is a wall of false violations and
            # teaches the user to click past a warning that is sometimes real.
            staged_ok = ""
            for _k, (_t, _s) in meta.items():
                if _t.lower() != parent.lower():
                    continue
                _pinv = {db.lower(): src for src, db in (maps.get(_k) or {}).items()}
                _pcol = _pinv.get(pkc.lower())
                if _pcol:
                    # Must mirror the promote-time transform, or a staged parent
                    # that WILL match after promote looks unmatched here.
                    _pexpr = (_id_expr("q", _pcol) if kind == "entity"
                              else _val_expr("q", _pcol, pkc.lower() in _IDENT))
                    # 🔑 AND THAT INCLUDES THE UWI-14 PAD. The child side is
                    # padded a few lines above; without the same pad here the
                    # comparison is 14 chars against 12 and NEVER matches, so
                    # this whole clause silently does nothing and every staged
                    # parent reads as missing. Teapot: 1,188 false unmatched
                    # values on a screen whose own Staged panel showed the
                    # 1,317 wells sitting there. With the pad: 52, which are real.
                    if pkc.lower() == "uwi":
                        _pexpr = _uwi14_sql(_pexpr)
                    staged_ok = (f" AND NOT EXISTS (SELECT 1 FROM {_s} q "
                                 f"WHERE {_pexpr} = {match})")
                    break
                # No break here: a parent mapped in a LATER file still counts.
                # Breaking on the first meta entry that merely NAMES the parent
                # abandoned the search when that entry had no PK mapping.

            q = text(
                f"SELECT {disp} AS val, COUNT(*) AS n FROM {stg} s "
                f"WHERE NULLIF(LTRIM(RTRIM([s].[{src_col}])),'') IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM {schema}.{parent.lower()} p WHERE p.[{pkc}] = {match})"
                f"{staged_ok} "
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


def _anchor(name: str):
    """An invisible target the page can be scrolled to."""
    st.markdown(f'<div id="{name}"></div>', unsafe_allow_html=True)


def _scroll_to(ss, name: str):
    """Scroll the page to _anchor(name), ONCE.

    Streamlit renders top to bottom and leaves the viewport where it was, so
    on a long page a result 4,000 pixels below the button that produced it is
    invisible — the operator sees nothing happen and presses the button again.
    Phase 3 finding violations is exactly that case: the analysis worked, and
    what it found is off screen.

    ONCE is the whole design. The flag is POPPED, not read — a scroll that
    re-fires on every rerun would drag the viewport back every time the
    operator touched a checkbox, which is worse than not scrolling at all.

    Components run in an iframe, hence window.parent.document. If the browser
    blocks that, nothing happens and the page behaves exactly as it does
    today — this is a convenience, and it must not be able to break a load.
    """
    if ss.pop("bdl_scroll_to", None) != name:
        return
    try:
        import streamlit.components.v1 as _c
        _c.html(
            "<script>"
            "const d = window.parent.document;"
            f"const el = d.getElementById({name!r});"
            "if (el) el.scrollIntoView({behavior:'smooth', block:'start'});"
            "</script>", height=0)
    except Exception:
        pass


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
            # Everything else about this batch is settled; the FK violations
            # are the only thing left needing a person. Take them there rather
            # than leaving the answer below the fold.
            if ss["bdl_fk"]:
                ss["bdl_scroll_to"] = "fk-violations"
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
    _n_vals = sum(len(d["values"]) for d in by_parent.values())
    _n_rows = sum(c["n"] for d in by_parent.values() for c in d["values"].values())
    st.warning(f"**Everything else is ready.** {_n_vals:,} unmatched value(s) across "
               f"{len(by_parent)} parent(s) affect {_n_rows:,} row(s) and are the only thing "
               f"left to decide. Phase 4 is directly below — one Add / Remap / Null grid per "
               f"parent, applied as set-based UPDATEs.")


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
    _anchor("fk-violations")
    st.header("Phase 4 — resolve FK violations")
    st.caption("Per parent: Add (seed the parent), Remap (fold onto an existing value), or Null "
               "(blank it). Applied as set-based UPDATEs to staging / INSERTs to the parent. "
               "A data-table parent (e.g. DV_WELL) still cannot be INVENTED from a "
               "child's key — that would mint a well that is only a number — but where "
               "the reference master describes it, ⬇ From master copies the real row.")

    eng = get_engine(server, database)
    open_parents = {p: info for p, info in by_parent.items() if info.get("values")}
    if not open_parents:
        st.success("No FK violations to resolve — all parents matched. Clear to promote (Phase 5).")
        return
    # CHECK ALL, per parent. The Add column defaults to on for seedable
    # parents, which is right for a handful of status codes and wrong the
    # moment there are eighty — so the operator needs one move in BOTH
    # directions. Outside the form on purpose: a form reports only on
    # submit, and this has to change the grid's defaults beforehand.
    _seedable = [p_ for p_, i_ in sorted(open_parents.items())
                 if i_["kind"] in ("entity", "reference")]
    _allmap = {}
    if _seedable:
        _cols = st.columns(min(3, len(_seedable)))
        for _n, _p in enumerate(_seedable):
            _allmap[_p] = _cols[_n % len(_cols)].checkbox(
                f"☑ Add all — {_p} ({len(open_parents[_p]['values'])})",
                value=True, key=f"bdlfkall_{_p}",
                help="Seed every unmatched value into this parent. Untick "
                     "to clear them all and decide row by row.")

    with st.form("bdl_phase4"):
        editors = {}
        for parent, info in sorted(open_parents.items()):
            kind = info["kind"]
            can_add = kind in ("entity", "reference")     # data-table parents can't be seeded
            opts = ["— skip —"] + _existing_options(eng, parent, kind, schema)
            # WHICH OF THESE DOES THE REFERENCE MASTER ACTUALLY DESCRIBE?
            # Asked once per parent, not per row, and only for data parents:
            # Add is refused for them because a well minted from a child's key
            # is just a number, but a well the master describes is a real row
            # with a name, an operator and a location. Offering the checkbox on
            # a value the master does not have would be the invention this
            # screen exists to prevent, so it is offered on nothing else.
            _from_master = {}
            if not can_add:
                try:
                    from dataview.import_data import seed_from_master as _sfm
                    with eng.connect() as _mcx:
                        _from_master = {r["uwi"]: r for r in _sfm.master_rows(
                            _mcx, list(info["values"].keys()))}
                except Exception as _me:
                    st.caption(f"reference master unavailable: "
                               f"{type(_me).__name__}: {_me}")
            with st.expander(f"{parent}  ({kind}) · {len(info['values'])} unmatched"
                             + ("" if can_add else "  — Remap/Null only"), expanded=True):
                rows = []
                _tick = can_add and bool(_allmap.get(parent, True))
                for v, c in sorted(info["values"].items()):
                    _m = _from_master.get(v)
                    rows.append({"☑ Add": _tick, "value": v, "rows": c["n"],
                                 "⬇ From master": bool(_m),
                                 "in master": (str(_m["well_name"])[:28] if _m
                                               else ("—" if not can_add else "")),
                                 "Map to existing": "— skip —", "☑ Remap": False,
                                 "☑ Null": False})
                grid = pd.DataFrame(rows)
                cfg = {"value": st.column_config.TextColumn(disabled=True),
                       "rows": st.column_config.NumberColumn(disabled=True, width="small"),
                       "☑ Add": st.column_config.CheckboxColumn(disabled=not can_add,
                                 help="Seed this value into the parent"),
                       "⬇ From master": st.column_config.CheckboxColumn(
                                 disabled=can_add or not _from_master,
                                 help="Copy this well from the reference master "
                                      "— name, operator, coordinates. Ticked "
                                      "already where the master has a row; a "
                                      "value it cannot describe stays held."),
                       "in master": st.column_config.TextColumn(
                                 disabled=True, width="medium",
                                 help="What the master calls it. — means the "
                                      "master has no row, so nothing can be "
                                      "copied and the children stay held."),
                       "Map to existing": st.column_config.SelectboxColumn(options=opts),
                       "☑ Remap": st.column_config.CheckboxColumn(help="Use 'Map to existing'"),
                       "☑ Null": st.column_config.CheckboxColumn(help="Blank the value in staging")}
                # The key carries the check-all state: a fixed-key
                # data_editor keeps its old cell values forever, so without
                # this the toggle would appear to do nothing.
                editors[parent] = st.data_editor(
                    grid, hide_index=True, use_container_width=True,
                    key=f"bdlfk_{parent}_{int(bool(_allmap.get(parent, True)))}",
                    column_config=cfg)
        applied = st.form_submit_button("✅ Apply resolutions (set-based)", type="primary",
                                        use_container_width=True)

    if applied:
        decisions = {}
        seed_uwis = []                 # data parents to copy from the master
        for parent, ed in editors.items():
            decs = []
            for _, r in ed.iterrows():
                # FROM MASTER IS NOT A RESOLUTION DECISION. The other three
                # rewrite STAGING (remap, null) or invent a parent row (add);
                # this one copies a described row out of the reference master
                # into dv_well. It is collected separately and applied through
                # seed_from_master, so the two paths cannot drift and the
                # command-line tool and this screen do the identical thing.
                if r.get("⬇ From master"):
                    seed_uwis.append(str(r["value"]))
                    continue
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

        if seed_uwis:
            try:
                from dataview.import_data import seed_from_master as _sfm
                with eng.connect() as _cx:
                    _rows = _sfm.master_rows(_cx, seed_uwis)
                _n = _sfm.seed(eng, _rows)
                _skipped = len(seed_uwis) - len(_rows)
                st.success(
                    f"Seeded {_n} well(s) from {_sfm.MASTER} — name, operator "
                    f"and location as the master states them, stamped "
                    f"row_created_by='{_sfm.CREATED_BY}' so they can be found "
                    f"or undone."
                    + (f" {_skipped} had no master row and stay held."
                       if _skipped else ""))
            except Exception as _se:
                st.error(f"Seed from master failed: {type(_se).__name__}: {_se}")

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


def _table_col_widths(engine, table, schema="dataview"):
    """{col_lower: (type, max_len)} — the LENGTH matters, not just the type.

    Staging columns are NVARCHAR; a target like dv_well.uwi is CHAR(14).
    Comparing nvarchar to char makes SQL Server convert the INDEXED COLUMN
    (nvarchar wins precedence), so every seek degrades to a scan: a 5,061-row
    tops promote took 154 SECONDS against 7,904 existing rows (July 31).
    Casting the inserted expression to the target's own type restores the
    seek."""
    import pandas as pd
    from sqlalchemy import text
    try:
        df = pd.read_sql(text(
            "SELECT COLUMN_NAME n, DATA_TYPE t, CHARACTER_MAXIMUM_LENGTH L "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:tb"),
            engine, params={"s": schema, "tb": table.lower()})
        return {str(r.n).lower(): (str(r.t).lower(),
                                   None if pd.isna(r.L) else int(r.L))
                for r in df.itertuples()}
    except Exception:
        return {}


def _cast_char(expr, tw):
    """Wrap expr in a CAST to a char/varchar target's exact type, so the
    comparison is type-aligned and indexes are seekable. Non-char targets and
    unknown widths are returned untouched."""
    if not tw:
        return expr
    t, ln = tw
    if t in ("char", "varchar") and ln and ln > 0:
        return f"CAST({expr} AS {t}({ln}))"
    if t in ("nchar", "nvarchar") and ln and ln > 0:
        return f"CAST({expr} AS {t}({ln}))"
    return expr


def _typed(expr, sqltype):
    """Wrap a trimmed varchar expression in TRY_CONVERT for date/numeric targets, so a bad
    value becomes NULL (auditable) instead of aborting the whole INSERT. Strings pass through."""
    if sqltype in ("date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"):
        # Day-first-safe date parse. Source dates in this data are DD-MM-YYYY (e.g.
        # 18-09-1992 — no month 18), but TRY_CONVERT's default reads US MM-DD-YYYY,
        # which REJECTS day>12 rows and silently TRANSPOSES the rest. So try style
        # 105 (dd-mm-yyyy) first, then fall back to the default parse (ISO 'YYYY-MM-DD'
        # and other unambiguous forms still convert), then NULL if neither works —
        # a bad value stays auditable instead of aborting the INSERT.
        return (f"COALESCE(TRY_CONVERT({sqltype}, {expr}, 105), "
                f"TRY_CONVERT({sqltype}, {expr}))")
    if sqltype in ("int", "bigint", "smallint", "tinyint"):
        return f"TRY_CONVERT({sqltype}, {expr})"
    if sqltype in ("numeric", "decimal", "float", "real", "money", "smallmoney"):
        return f"TRY_CONVERT(float, {expr})"
    if sqltype == "bit":
        return f"TRY_CONVERT(bit, {expr})"
    return expr


def build_promote_sql(engine, target, cmap, functions, schema="dataview", stg=None, parsed=None,
                      holds_out=None, inventory_id=None, stats_out=None):
    """Build the idempotent INSERT…SELECT that promotes stg → dataview.<target>.
    Transforms: de-sep identifiers, entity SHA1, function rules, audit stamp; NOT EXISTS on PK.
    Pass `parsed` (FKC, COLS, KIND) to avoid re-introspecting the whole schema per table.

    HOLD, DON'T FAIL (July 30): single-column FKs to DATA parents (kind
    'parent' — uwi→dv_well, srvy_id→hdr, prod_entity_id→entity; NOT entity/
    reference, which Phase 4 seeds and should fail loudly) become EXISTS
    filters on the promoted SELECT. Rows whose parent is absent are LEFT IN
    STAGING instead of failing the whole INSERT with an FK violation and
    rolling back everything (the July-29 tops load: 50 absent wells sank all
    5,061 rows). Pass holds_out=[] to receive (parent, parent_col, child_col,
    filter_expr) per filter so the caller can COUNT and report the held rows;
    they promote automatically on a re-run once the parent is loaded."""
    FKC, COLS, KIND = parsed if parsed is not None else _live_catalog_parsed(engine, schema)
    tu = target.upper()
    tcols = {c.lower() for c in COLS.get(tu, set())}
    # Computed columns are removed from the target set rather than filtered at
    # INSERT time: a column that isn't in tcols simply doesn't map, so a CSV
    # that happens to carry the header is ignored instead of failing the load.
    tcols -= _computed_cols(engine, target, schema)
    stg = stg or stg_name(target)
    cmap_inv = {db.lower(): src for src, db in cmap.items()}     # db col -> staging col
    ident = lambda name: name.lower() in _IDENT
    coltypes = _table_col_types(engine, target, schema)
    colwidths = _table_col_widths(engine, target, schema)

    select_cols, insert_cols, collisions = [], [], []
    seen_targets = {}                    # target col -> what first claimed it
    col_exprs = {}                       # target col -> the SELECT expression
    def _add(dbl, expr, who=None):
        if dbl in seen_targets:
            collisions.append((dbl, seen_targets[dbl], who or "a derived rule"))
            return
        seen_targets[dbl] = who or "a derived rule"
        col_exprs[dbl] = expr
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
            if dbl == "uwi":
                # Canonical UWI-14 in T-SQL, matching _uwi14 exactly: de-sep,
                # then right-pad zeros / keep first 14. _uwi14 guards the
                # EXTRACTED-file path (build_safe_file), but a direct CSV's
                # only transform is THIS expression — Teapot's 12-digit APIs
                # promoted as 12 chars (July 29) and broke every 14-vs-14
                # uwi join downstream. Only `uwi` gets this: padding other
                # identifier keys (log_id, curve_id) would corrupt them.
                expr = (f"CASE WHEN NULLIF({expr}, '') IS NULL THEN NULL "
                        f"ELSE LEFT(CONCAT({expr}, REPLICATE('0', 14)), 14) END")
            # CAST to the target's OWN char type. Without it the expression is
            # nvarchar (staging is nvarchar) and every comparison against a
            # char/varchar key column converts the COLUMN, killing the index
            # seek — the 154-second tops promote (July 31).
            expr = _cast_char(expr, colwidths.get(dbl))
        else:
            expr = _typed(f"NULLIF(LTRIM(RTRIM([s].[{src}])),'')", coltypes.get(dbl, ""))
            expr = _cast_char(expr, colwidths.get(dbl))
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
    # 3b) PROVENANCE. inventory_id says which document produced this row and
    # WHICH CATALOG holds it: a bare 40-char SHA-1 is the File Catalog's
    # (hashed from the path), "DV-" + SHA-1 is the loader's own ledger
    # (dataview.dv_global_file_catalog, hashed from the file's CONTENT).
    # Only stamped when the caller supplies one — an unverified load has no
    # business claiming provenance, and a mapped source column always wins.
    if inventory_id and "inventory_id" in dbcols \
            and "inventory_id" not in seen_targets:
        _add("inventory_id", "'" + str(inventory_id).replace("'", "''") + "'",
             "the load ledger (provenance)")

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

    # HOLD filters — see docstring. The filter uses the SAME expression that
    # will be inserted (padded uwi etc.), so it compares what promote writes.
    hold_filters = []
    for fk in FKC.get(tu, []):
        ccols = [c.lower() for c in fk.get("child_cols", [])]
        if len(ccols) != 1:
            continue
        ccol = ccols[0]
        if ccol not in col_exprs:
            continue                      # not inserted -> NULL -> FK passes
        info = pdl._fk_of(tu, ccol, FKC)
        if not info or info[1] != "parent":
            continue                      # entity/reference: seedable, fail loudly
        parent = str(info[0]).upper()
        if parent == tu:
            continue                      # self-FK: intra-batch order unknowable
        ppk = _table_pk_live(engine, parent, schema) or []
        if len(ppk) == 1:
            pcol = ppk[0]
        elif ccol in {c.lower() for c in COLS.get(parent, set())}:
            pcol = ccol                   # same-named column fallback
        else:
            continue                      # can't determine the join -> old behavior
        e = col_exprs[ccol]
        hold_filters.append((parent, pcol, ccol,
                             f"({e} IS NULL OR EXISTS (SELECT 1 FROM "
                             f"{schema}.{parent.lower()} p WHERE p.[{pcol}] = {e}))",
                             None))
    pk = _table_pk_live(engine, target, schema)
    pk_in = [p for p in (pk or []) if p in insert_cols]

    # A BLANK PK COMPONENT IS NOT A DUPLICATE KEY. The dedupe below keeps one
    # row per PK because staging legitimately repeats keys (reference data
    # especially). But when a PK column was never mapped, EVERY row of a group
    # lands in the same partition and all but one are discarded -- silently,
    # and the count looks like a successful load minus some holds.
    #
    # Teapot's formation tops, 24 Aug: UNIT_CODE (59 distinct values) was not
    # mapped to strat_unit_id, so the PK (uwi, strat_unit_id, interp_id)
    # collapsed to (uwi, '', '1') and 7,285 staged tops became 1,031 rows --
    # exactly one per well, up to 47 thrown away per well, with nothing said.
    # Wrong is worse than missing, and this was both: fewer rows than the
    # source, presented as complete.
    #
    # Held, not dropped: the rows stay in staging and promote on a later run
    # once the column is mapped, the same as an absent-parent hold.
    for _pkc in pk_in:
        _e = col_exprs.get(_pkc)
        if not _e:
            continue
        hold_filters.append((
            None, _pkc, _pkc,
            f"NULLIF({_e}, '') IS NOT NULL",
            f"[{_pkc}] is part of the primary key of {target} but this row "
            f"supplies no value for it, so every such row would collapse onto "
            f"the same key and all but one be discarded. Map a source column "
            f"to it, then re-run"))

    if holds_out is not None:
        holds_out.extend(hold_filters)
    pk_join = " AND ".join(f"d.[{p}] = src.[{p}]" for p in pk_in)
    sel = ",\n       ".join(select_cols)
    inner = f"SELECT {sel}\n  FROM {stg} s"
    if hold_filters:
        inner += "\n  WHERE " + "\n    AND ".join(f[3] for f in hold_filters)
    # WHAT THE DEDUPE IS ABOUT TO THROW AWAY, counted before it happens.
    #
    # A schema rule cannot tell a harmless unmapped PK column from a ruinous
    # one. dv_well_formation_top.interp_id defaults to '1' and is never mapped;
    # that is FINE once strat_unit_id carries a real value, and fatal when it
    # does not. Same column, same schema, opposite verdicts -- so the question
    # is not "is a PK column unmapped" but "does this data collapse", and only
    # the data can answer it.
    #
    # Teapot's tops, 24 Aug: UNIT_CODE was mapped to __SKIP__, so the key became
    # (uwi, '', '1') and 7,285 staged rows became 1,031 -- one per well, up to
    # 47 discarded each, reported as a successful load. The rows were not
    # rejected or held; ROW_NUMBER simply kept the first and the rest ceased to
    # exist. Wrong is worse than missing, and this was both.
    if stats_out is not None and pk_in:
        _keyexpr = ", '|', ".join(f"ISNULL(CAST({col_exprs[p]} AS nvarchar(400)), '')"
                                  for p in pk_in if p in col_exprs)
        if _keyexpr:
            _w = (" WHERE " + " AND ".join(f[3] for f in hold_filters)
                  if hold_filters else "")
            stats_out["dedupe_sql"] = (
                f"SELECT COUNT(*) - COUNT(DISTINCT CONCAT({_keyexpr}, ''))"
                f" FROM {stg} s{_w}")
            stats_out["pk_cols"] = list(pk_in)
            stats_out["pk_unmapped"] = [p for p in (pk or []) if p not in col_exprs]

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

    # ── projected coordinates → lat/long, before anything else ──────────────────────
    # Eligible: the target has an unmapped lat/long pair AND the staging table exists.
    meta_ll = ss.get("bdl_mapmeta", {})
    _ll_cands = []
    for skey, cmap in maps.items():
        target, stg = meta_ll.get(skey, (skey, skey))
        try:
            db_cols = _table_cols_db(eng, target, schema)
        except Exception:
            continue
        la, lo = _latlon_target_pair(db_cols)
        if not la:
            continue
        mapped = {str(v).lower() for v in (cmap or {}).values()}
        if la in mapped or lo in mapped:
            continue
        try:
            with eng.connect() as _c0:
                scols = [r0[0] for r0 in _c0.execute(text(
                    "SELECT name FROM sys.columns WHERE object_id=OBJECT_ID(:t) "
                    "ORDER BY column_id"), {"t": stg}).fetchall()]
        except Exception:
            continue
        scols = [c for c in scols if c not in ("__LAT", "__LON")]
        if not scols:
            continue
        n_g, e_g = _detect_ne(scols)
        _ll_cands.append((skey, target, stg, scols, la, lo, n_g, e_g))
    if _ll_cands:
        with st.expander(f"🧭 Derive lat/long from projected X/Y — "
                         f"{len(_ll_cands)} table(s) eligible", expanded=False):
            st.caption("For files carrying Northing/Easting in a projected CRS but no "
                       "latitude/longitude (Teapot Dome = EPSG 32056, NAD27 Wyoming East "
                       "Central, US survey ft — feed values as-is, never pre-convert to "
                       "meters). Converts distinct pairs with pyproj into __LAT/__LON "
                       "staging columns and maps them to the target. Without coordinates, "
                       "REQUIRE_WELL_COORDS holds the wells and the EXISTS gate then holds "
                       "every child row — the silent total stall.")
            for skey, target, stg, scols, la, lo, n_g, e_g in _ll_cands:
                st.markdown(f"**{target}**  ⟵  `{stg.split('.')[-1]}`")
                c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
                nsel = c1.selectbox("Northing", ["—"] + scols,
                                    index=(scols.index(n_g) + 1) if n_g in scols else 0,
                                    key=f"bdl_ll_n_{skey}")
                esel = c2.selectbox("Easting", ["—"] + scols,
                                    index=(scols.index(e_g) + 1) if e_g in scols else 0,
                                    key=f"bdl_ll_e_{skey}")
                epsg_v = c3.text_input("EPSG", key=f"bdl_ll_epsg_{skey}",
                                       help="source CRS, e.g. 32056")
                if c4.button("Convert", key=f"bdl_ll_go_{skey}"):
                    if (nsel == "—" or esel == "—" or nsel == esel
                            or not str(epsg_v).strip().isdigit()):
                        st.error("Pick two different columns and a numeric EPSG.")
                    else:
                        try:
                            n_upd, n_pairs, n_bad = derive_latlong(
                                eng, stg, nsel, esel, int(epsg_v))
                        except ImportError:
                            st.error("pyproj is not installed in this environment — "
                                     "pip install pyproj")
                        except Exception as _le:
                            st.error(f"conversion failed: {str(_le)[:200]}")
                        else:
                            _cm = dict(maps.get(skey) or {})
                            _cm["__LAT"], _cm["__LON"] = la, lo
                            maps[skey] = _cm
                            ss["bdl_maps"] = maps
                            st.success(f"{n_upd} staged row(s) updated from {n_pairs} "
                                       f"distinct pair(s), {n_bad} unusable → "
                                       f"__LAT→{la}, __LON→{lo} added to the map "
                                       f"(this run only; the store keeps no synthetic "
                                       f"columns).")
                            with eng.connect() as _c1:
                                ext = _c1.execute(text(
                                    f"SELECT MIN(TRY_CONVERT(float,[__LAT])), "
                                    f"MAX(TRY_CONVERT(float,[__LAT])), "
                                    f"MIN(TRY_CONVERT(float,[__LON])), "
                                    f"MAX(TRY_CONVERT(float,[__LON])) FROM {stg}")).fetchone()
                            if ext and ext[0] is not None:
                                st.caption(f"extent: lat {ext[0]:.4f}..{ext[1]:.4f} · "
                                           f"lon {ext[2]:.4f}..{ext[3]:.4f} — Teapot "
                                           f"should read ~43.25..43.40 / −106.30..−106.10; "
                                           f"anything else = wrong EPSG or swapped columns.")

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
        # promote each staged table (skey); order by its target's position in the FK topo order.
        # BOTH SIDES ARE UPPERCASED. _topo_order builds `order` from table names it has already
        # uppercased, while bdl_mapmeta stores the target exactly as the scan row had it — so a
        # case difference made every lookup miss, every table tie on the 999 default, and the
        # stable sort leave them in maps.keys() order. That reads as alphabetical, which is
        # precisely the symptom the topological sort exists to prevent: DV_PROD_ENTITY promoted
        # ahead of DV_WELL, failing on a parent that was two positions away from being there.
        order_ix = {str(t).upper(): i for i, t in enumerate(order)}
        skeys = sorted(maps.keys(),
                       key=lambda k: order_ix.get(
                           str(meta.get(k, (k, ""))[0]).upper(), 999))
        for skey in skeys:
            target, stg_tbl = meta.get(skey, (skey, skey))
            try:
                if not _table_cols_db(eng, target, schema):
                    raise ValueError(
                        f"'{schema}.{target}' is not a table — this staged file is mapped to "
                        f"something that looks like a column name. Correct its → table in "
                        f"Files → tables, or ↻ Reset run to clear stale scan state.")
                tb = time.perf_counter()
                _holds = []
                # PROVENANCE — FIRST ONE IN WINS (Perry's rule).
                # Several files of the same column shape share one staging
                # table, so at promote time a row cannot say which of them
                # it came from. Rather than abandon provenance, stamp the
                # FIRST file's id: promote is insert-only with a NOT EXISTS
                # guard, so whichever load inserts a row owns it and later
                # ones skip it — the rule is already how the mechanism
                # behaves, this just records it.
                # The computed id also BEATS a mapped INVENTORY_ID column:
                # that column is a claim about which file a row came from,
                # and only the loader reading the file can know it (the
                # synthetic exports carry "INVENTORY_ID-641", which
                # resolves to nothing).
                _cmap_p = dict(maps[skey])
                _inv_p = None
                _inv_err = None
                try:
                    # bdl_review is a LIST of per-table dicts, each with its
                    # own "skey" and "path" — NOT a dict keyed by skey. The
                    # first cut called .get(skey) on the list, which raises,
                    # and the except below swallowed it: no stamp, no drop,
                    # and SQL identical to having made no change at all.
                    _revp = next((x for x in (ss.get("bdl_review") or [])
                                  if x.get("skey") == skey), None)
                    _src_path = (_revp or {}).get("path") or ""
                    if _src_path:
                        from dataview.import_data.file_gate import (
                            inventory_id as _iid_p)
                        _inv_p = _iid_p(os.path.abspath(_src_path))
                    else:
                        _inv_err = "no source path on the review row"
                except Exception as _ie:
                    _inv_p, _inv_err = None, f"{type(_ie).__name__}: {_ie}"
                if _inv_err:
                    # SAY SO. A silent except here is what made this bug
                    # survive four rounds of "it should work now".
                    st.caption(f"⚠ {target}: provenance id not stamped — "
                               f"{_inv_err}")
                if _inv_p:
                    for _s_p in [s for s, t in _cmap_p.items()
                                 if str(t).lower() == "inventory_id"]:
                        _cmap_p.pop(_s_p, None)
                _stats = {}
                sql, cols, pk = build_promote_sql(eng, target, _cmap_p, funcs_all.get(skey, []),
                                                  schema, stg=stg_tbl, parsed=parsed,
                                                  holds_out=_holds,
                                                  inventory_id=_inv_p, stats_out=_stats)
                build_t = time.perf_counter() - tb
                tc = time.perf_counter()
                with eng.connect() as cx:
                    # The staging table may not exist: the file was skipped in Files → tables,
                    # so it never staged — but Build mappings had already auto-mapped it into
                    # `maps` beforehand, and nothing removed it. Querying it raises a raw
                    # "Invalid object name 'stg.x'" (208), which names the symptom and not the
                    # cause. Say what actually happened.
                    if not cx.execute(text("SELECT OBJECT_ID(:t)"),
                                      {"t": stg_tbl}).scalar():
                        raise ValueError(
                            f"`{stg_tbl}` was never staged — this file is skipped in "
                            f"**Files → tables**, so there is nothing to promote. Untick its "
                            f"skip and re-stage to include it, or ignore this row.")
                    staged = cx.execute(text(f"SELECT COUNT(*) FROM {stg_tbl}")).scalar()
                count_t = time.perf_counter() - tc
                label = target if stg_tbl == stg_name(target) else f"{target} ⟵ {stg_tbl.split('.')[-1]}"
                prev.append({"table": label, "target": target, "staged": staged,
                             "cols": len(cols), "sql": sql, "stg": stg_tbl,
                             "holds": _holds, "stats": _stats,
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
                        held_msgs = []
                        # *_rest tolerates the 4-tuple shape this used to be;
                        # a 5th element carries a reason for holds that are NOT
                        # about a missing parent, whose message would otherwise
                        # send the reader looking for a parent table that has
                        # nothing to do with it.
                        for parent, pcol, ccol, _f, *_rest in (p.get("holds") or []):
                            _why = _rest[0] if _rest else None
                            try:
                                # the filter expr references alias [s]
                                n_held = cx.execute(text(
                                    f"SELECT COUNT(*) FROM {p['stg']} s "
                                    f"WHERE NOT {_f}")).scalar() or 0
                            except Exception:
                                n_held = None
                            if n_held and _why:
                                held_msgs.append(
                                    f"⏸ {n_held} row(s) HELD in staging — "
                                    f"{_why}.")
                            elif n_held:
                                held_msgs.append(
                                    f"⏸ {n_held} row(s) HELD in staging — "
                                    f"[{ccol}] has no match in {parent}. They "
                                    f"promote automatically on the next run "
                                    f"once {parent} has those rows.")
                        # THE DEDUPE MUST NOT BE SILENT. ROW_NUMBER keeps one
                        # row per PK, which is right for genuinely repeated
                        # keys and catastrophic when a PK column carries no
                        # real value: Teapot's 7,285 tops became 1,031 that
                        # way, one per well, and the run reported success.
                        # Counting it costs one query and turns an invisible
                        # loss into a line that names the columns to look at.
                        _dsql = (p.get("stats") or {}).get("dedupe_sql")
                        if _dsql:
                            try:
                                _dropped = cx.execute(text(_dsql)).scalar() or 0
                            except Exception:
                                _dropped = 0
                            if _dropped:
                                _pkc = ", ".join((p.get("stats") or {}).get("pk_cols") or [])
                                _unm = (p.get("stats") or {}).get("pk_unmapped") or []
                                held_msgs.append(
                                    f"⚠ {_dropped:,} staged row(s) share a primary key "
                                    f"({_pkc}) and only one of each was kept. "
                                    + (f"Nothing maps to [{', '.join(_unm)}], so that part of "
                                       f"the key is the same on every row — map it and "
                                       f"re-run to keep them all."
                                       if _unm else
                                       "If those are not true duplicates, a key column is "
                                       "carrying the same value on every row."))
                        log.append((label, after - before, round(time.perf_counter() - ti, 2), None,
                                    held_msgs))
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
            t, n, secs, err = row[0], row[1], row[2], row[3]
            for _hm in (row[4] if len(row) > 4 else []):
                st.warning(_hm)
            if err:
                # A wall of generated SQL is not a diagnosis. Explain it in loader terms —
                # which column, which rule, what to change — and tuck the raw text away.
                exc = ss.get("bdl_promote_exc")
                shown = False
                if exc and exc[0] == t:
                    shown = _render_diag(exc[1], table=t,
                                         tb=(exc[2] if len(exc) > 2 else None))
                    # A constraint name is not a diagnosis. Phase 3 (analyze_fks) already found
                    # the exact unmatched values and their row counts and where they came from
                    # — pull that detail in here, keyed to the failing table, so "Unresolved
                    # foreign key (fk_srvy_sta_hdr)" comes with WHICH values, from WHICH column.
                    _fk_detail_for_error(t, err)
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

    # ── derived columns, AFTER promote ────────────────────────────────────────────
    _render_h3_backfill(eng, schema)


def _render_h3_backfill(eng, schema="dataview"):
    """Compute the H3 grid cells for wells that don't have them.

    HERE, AND AFTER PROMOTE, for three reasons. It needs the rows to be IN
    dv_well, so it cannot sit beside the lat/long derivation at the top of
    the phase — that one works on STAGING, this one on the loaded table.
    It belongs in the loader rather than a maintenance page because the
    moment wells arrive is the moment their cells are missing, and a
    derived column nobody computes is the same silent gap as one holding a
    placeholder. And it is a BUTTON, not an automatic post-load hook, per
    the rule the FK seeding established: automation may skip ceremony,
    never a decision.

    Shown only when there is something to do. A phase that always displays
    one more thing to click trains people to ignore it.
    """
    from sqlalchemy import text
    try:
        with eng.connect() as cx:
            n_missing = cx.execute(text(
                f"SELECT COUNT(*) FROM {schema}.dv_well "
                f"WHERE surface_latitude IS NOT NULL "
                f"  AND surface_longitude IS NOT NULL "
                f"  AND h3_r5 IS NULL")).scalar() or 0
            # Junk counts as missing: 'h3_r4-869' is a generator placeholder,
            # and backfill_h3's default only_missing=True SKIPS non-null rows
            # — so the rows most needing repair are the ones it passes over.
            n_junk = cx.execute(text(
                f"SELECT COUNT(*) FROM {schema}.dv_well "
                f"WHERE h3_r5 IS NOT NULL "
                f"  AND (LEN(h3_r5) <> 15 OR h3_r5 LIKE '%[^0-9a-f]%')")).scalar() or 0
            n_nocoord = cx.execute(text(
                f"SELECT COUNT(*) FROM {schema}.dv_well "
                f"WHERE surface_latitude IS NULL "
                f"   OR surface_longitude IS NULL")).scalar() or 0
    except Exception:
        return                       # no dv_well, or no h3 columns — nothing to offer

    if not (n_missing or n_junk):
        return

    # ONE NUMBER, NO DIAGNOSIS. Perry's call: this is a master database and
    # the operator does not need to hear about placeholder values, skipped
    # rows or unresolvable wells. The panel says what will happen and how
    # many wells it affects. Everything else is handled silently below —
    # the behaviour is unchanged, only the narration is gone.
    n_todo = n_missing + n_junk
    with st.expander(f"🔷 Compute H3 grid cells — {n_todo:,} well(s)",
                     expanded=False):
        st.caption(
            "Wells are clustered in hexagonal shapes for visualizing "
            "wells in densely drilled areas.")
        if st.button("🔷 Compute now", type="primary", key="bdl_h3_go"):
            try:
                from dataview.mapping.h3_grids import backfill_h3
            except Exception as e:
                st.error(f"h3_grids not importable: {e}")
                return
            try:
                # Any value that is not a valid cell is cleared first —
                # backfill_h3 skips rows that already hold something, so
                # without this the rows needing it most are passed over.
                # Silent: it is housekeeping, not news.
                if n_junk:
                    with eng.begin() as cx:
                        cx.execute(text(
                            f"UPDATE {schema}.dv_well "
                            f"   SET h3_r4=NULL, h3_r5=NULL, h3_r6=NULL, "
                            f"       h3_r7=NULL, h3_coord_hash=NULL "
                            f" WHERE h3_r5 IS NOT NULL "
                            f"   AND (LEN(h3_r5) <> 15 "
                            f"        OR h3_r5 LIKE '%[^0-9a-f]%')"))
                with st.spinner("Computing cells…"):
                    backfill_h3(eng, only_missing=True)
                with eng.connect() as cx:
                    done = cx.execute(text(
                        f"SELECT COUNT(*) FROM {schema}.dv_well "
                        f"WHERE h3_r5 IS NOT NULL")).scalar() or 0
                st.success(f"{done:,} well(s) clustered.")
            except Exception as e:
                st.error(f"Could not compute grid cells: {e}")


def _fk_detail_for_error(table, err):
    """Turn a bare promote-time FK failure into the concrete detail Phase 3 already computed.

    A FOREIGN KEY error from SQL Server names the constraint and the child table and nothing
    else — 'Unresolved foreign key (fk_srvy_sta_hdr) · DV_WELL_DIR_SRVY_STA'. But Phase 3 ran
    the exact NOT EXISTS query per FK and knows the unmatched VALUES, their row counts, and the
    staging column they came from. It's sitting in ss['bdl_fk']. Surface it here so the error
    says what to fix instead of only which rule was broken.
    """
    import streamlit as st
    import pandas as pd
    err_l = str(err or "").lower()
    if "foreign key" not in err_l and "reference" not in err_l:
        return
    by_parent = st.session_state.get("bdl_fk") or {}
    # analyze_fks groups by PARENT and records each source's child table — find the parents
    # whose sources include this failing child table.
    tu = str(table or "").upper().split("⟵")[0].strip()
    hits = []
    for parent, d in by_parent.items():
        for src in d.get("sources", []):
            if str(src.get("target", "")).upper() == tu:
                hits.append((parent, d, src))
    if not hits:
        st.info("Phase 3 didn't flag this FK — run **Analyze FKs** before promoting and it "
                "will list the exact unmatched values instead of only the constraint name.")
        return
    for parent, d, src in hits:
        vals = d.get("values", {})
        st.error(
            f"**{tu}.{src['child_col']}** references **{parent}**, and **{len(vals)} "
            f"value(s)** in the staged data have no matching row there — that's what the FK "
            f"rejected. Add them to {parent}, remap them, or null the column (Phase 4), then "
            f"re-promote.")
        if vals:
            st.dataframe(pd.DataFrame(
                [{"unmatched value": v, "rows": c["n"],
                  "from": ", ".join(sorted(set(c["froms"])))}
                 for v, c in sorted(vals.items(), key=lambda kv: -kv[1]["n"])][:50]),
                hide_index=True, use_container_width=True)
            if len(vals) > 50:
                st.caption(f"…and {len(vals) - 50} more distinct value(s)")


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
                    # THE PAD, NOT JUST THE DE-SEPARATION. Promote canonicalises
                    # an identifier with _uwi14 at the write point -- strip
                    # separators, then right-pad zeros to 14 -- so the target
                    # holds '49025103970000' while staging holds the 12-digit
                    # '490251039700'. De-separating alone leaves the two
                    # different strings, EXISTS matches nothing, and a load that
                    # worked perfectly reports itself as a total failure: all
                    # 1,317 Teapot wells came back "-1317" while sitting in
                    # dataview.
                    #
                    # _uwi14_sql is the one expression both sides of any
                    # identifier comparison use. It already existed, with this
                    # exact failure in its docstring, for build_promote_sql and
                    # the repair UPDATE. This is the fourth site, and the one
                    # that was still hand-rolling its own version.
                    expr = _uwi14_sql(
                        f"REPLACE(REPLACE(REPLACE(LTRIM(RTRIM([s].[{src}])),"
                        f"'-',''),' ',''),'.','')")
                    conds.append(f"{_uwi14_sql(f'd.[{pkc}]')} = {expr}")
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


# ─────────────────────── bulk triage (Perry's rebuild) ─────────────────────
# One screen, three honest groups, per-FILE skips, nothing hidden. Files that
# need a transform are HANDED to the assistant rather than half-loaded here
# (Perry, July 31: "direct them to the other side").
def clean_path(p):
    """A pasted path, as pasted. Explorer's "Copy as path" wraps in double
    quotes, PowerShell's Copy-as-path can add a leading `& `, and Word or
    a chat window may have turned the quotes into smart ones. Every one of
    those makes os.path.exists say no about a file that is plainly there,
    and the operator gets to hunt an invisible character.
    """
    s = str(p or "").strip()
    if s.startswith("& "):                      # PowerShell call operator
        s = s[2:].strip()
    for q in ('"', "'", "\u201c", "\u201d", "\u2018", "\u2019"):
        if s.startswith(q):
            s = s[1:]
        if s.endswith(q):
            s = s[:-1]
    s = s.strip()
    return os.path.expandvars(os.path.expanduser(s)) if s else s


import hashlib as _hashlib


_SKIP_SENTINEL = "__SKIP__"          # a target that means "deliberately not loaded"


def remember_fp_skip(engine, fps, unskip_fps=()):
    """Record, against a file's COLUMN FINGERPRINT, that it is not to be loaded.

    WHY THE FINGERPRINT AND NOT THE PATH: the decision is about the SHAPE of
    the file, not that one copy of it. Next month's export has a new name, new
    rows and the same columns — and it is the same decision. A path-keyed skip
    would ask again every month.

    Stored in dv_column_map beside the mapping recall, under the same
    source_file_pattern, with target_table = '__SKIP__'. That table already
    answers "what did we decide about this shape"; a skip is one of the
    answers. A separate table would be a second place to look and a second
    thing to keep in step.

    Best-effort: a memory that fails to write must never fail a scan.
    """
    if not fps and not unskip_fps:
        return 0
    import hashlib as _h
    from sqlalchemy import text as _t
    n = 0
    try:
        with engine.begin() as cx:
            for _fp in set(unskip_fps or ()):
                cx.execute(_t("UPDATE dataview.dv_column_map SET active_ind='N' "
                              "WHERE source_file_pattern=:fp AND target_table=:sk"),
                           {"fp": _fp, "sk": _SKIP_SENTINEL})
            for _fp in set(fps or ()):
                _mid = _h.sha1(f"{_fp}|{_SKIP_SENTINEL}".encode("utf-8")).hexdigest()[:40]
                cx.execute(_t(
                    "MERGE dataview.dv_column_map AS t "
                    "USING (SELECT :mid AS map_id) s ON t.map_id = s.map_id "
                    "WHEN MATCHED THEN UPDATE SET active_ind='Y', confirmed_ind='Y' "
                    "WHEN NOT MATCHED THEN INSERT "
                    "  (map_id, source_file_pattern, source_column, target_table, "
                    "   target_column, confirmed_ind, active_ind, row_created_by, "
                    "   row_created_date) "
                    "VALUES (:mid, :fp, '*', :sk, '*', 'Y', 'Y', SUSER_SNAME(), "
                    "        SYSUTCDATETIME());"),
                    {"mid": _mid, "fp": _fp, "sk": _SKIP_SENTINEL})
                n += 1
    except Exception:
        return 0
    return n






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
        # Missing = staged − present. Since the hold filters (July 30), rows
        # whose data parent is absent are ⏸ HELD in staging ON PURPOSE and
        # count here too — that is the system working, not a broken load.
        # Cross-reference the promote results' ⏸ warnings before suspecting
        # the mapping.
        _held_tables = {str(r2[0]).split("⟵")[0].strip()
                        for r2 in (ss.get("bdl_promote_log") or [])
                        if len(r2) > 4 and r2[4]}
        _lines = []
        for r in bad:
            _t0 = str(r["table"]).split("⟵")[0].strip()
            if _t0 in _held_tables:
                _lines.append(f"{r['table']} (−{r['missing']} — includes ⏸ held "
                              f"rows awaiting their parent; loads on a later "
                              f"run once the parent arrives)")
            else:
                _lines.append(f"{r['table']} (−{r['missing']})")
        st.warning("Not fully loaded: " + ", ".join(_lines)
                   + ". Held rows are expected and safe; for the rest, re-run "
                     "Promote (idempotent) or check the mapping.")


def run():
    import pandas as pd
    ss = st.session_state
    hc1, hc2 = st.columns([4, 1])
    hc1.header("Bulk Tabular Loader")
    if hc2.button("↻ Reset run", help="Clear scan, mappings and staged state and start over "
                                       "(keeps server/database/paths)"):
        keep = {k: ss[k] for k in ("bdl_server", "bdl_db", "bdl_dir", "bdl_cat", "bdl_bulk",
                                   "bdl_recursive", "bdl_schema") if k in ss}
        for k in [k for k in ss.keys() if k.startswith("bdl_") or k.startswith("_cat")]:
            del ss[k]
        ss.update(keep)
        st.rerun()
    # CSV AND EXCEL ONLY (caption corrected Aug 10, Perry: "the loader does not
    # load LAS · DLIS · LIS · WITSML · PDF · Word that I am aware of"). He is
    # right — that extraction was removed on 20 July when the role split landed,
    # and the caption kept advertising it. A format list is a PROMISE about what
    # a folder will do; naming formats this loader has not touched in three
    # weeks sends someone to the wrong app with the wrong files and leaves them
    # wondering why nothing was found.
    st.caption("Scan a directory of **CSV or Excel** files — extract to staging, then "
               "map → FK → check → dry run → promote → verify. Excel workbooks are "
               "exploded to one CSV per sheet. Logs (LAS · DLIS · LIS), seismic and "
               "documents (PDF · Word) are the **File Catalog's** job, not this one. "
               "For the per-table flow with inline value repair, use ⇄ (the tabular "
               "loader) above.")

    c1, c2 = st.columns(2)
    server = c1.text_input("Server", value=ss.get("bdl_server", r"localhost\SQLEXPRESS"))
    database = c2.text_input("Database", value=ss.get("bdl_db", "DataView_Demo"))
    # MODE FIRST, then only the inputs that mode uses. The Directory field
    # and the scan options belong to 📦 Load; drawing them in 🧭 gave the
    # assistant page two path boxes and no way to tell which mattered
    # (Perry, July 31: "do I have to fill both path boxes").
    _MODES = ["🧭 Plan & derive (Phase 0) — the AI assistant",
              "📦 Load (Phases 1–6) — scan → stage → promote → verify"]
    # honour a pending request from elsewhere (the triage handoff) BEFORE the
    # widget is created — after that its key is read-only
    _want = ss.pop("bdl_want_mode", None)
    if _want in ("plan", "load"):
        ss["bdl_mode"] = _MODES[0 if _want == "plan" else 1]
    _mode = st.radio("Mode", _MODES,
                     index=0 if not ss.get("bdl_scan") else 1,
                     horizontal=True, key="bdl_mode",
                     label_visibility="collapsed")
    _planning = _mode.startswith("🧭")

    if _planning:
        directory = ss.get("bdl_dir", "")
        recursive = ss.get("bdl_recursive", False)
        force = ss.get("bdl_force", False)
    else:
        directory = clean_path(st.text_input(
            "Directory or single file (CSV / Excel)",
            value=ss.get("bdl_dir", ""),
            help="A folder scans everything in it; a single FILE scans just "
                 "that one and runs the same six phases. Quotes from "
                 "Explorer's 'Copy as path' are stripped for you, as is a "
                 "leading '& ' from PowerShell."))
        if directory and os.path.isfile(directory):
            st.caption(f"📄 Single file — {os.path.basename(directory)}. "
                       f"Phases 1–6 run on it alone.")
        recursive = st.checkbox("Include subdirectories (recursive scan)",
                                value=ss.get("bdl_recursive", False))
        force = st.checkbox(
            "Force re-extract (ignore the file catalog)",
            value=ss.get("bdl_force", False),
            help="Unchanged files are normally skipped — their content hash already "
                 "matches dv_global_file_catalog and their rows are loaded. Tick this "
                 "when the EXTRACTOR changed rather than the data: the bytes are "
                 "identical, so the gate would skip files that now extract differently.")
        ss["bdl_force"] = force
    # Persist the shared config NOW. The 🧭 branch below renders and
    # returns, so the assignment further down never ran in assistant mode and
    # the assistant read a stale (or empty) directory — "Directory not found"
    # (Perry, July 31). Paths also arrive quoted when pasted from Explorer's
    # "Copy as path", so strip that here once for everyone.
    directory = str(directory or "").strip().strip('"').strip("'")
    ss["bdl_server"], ss["bdl_db"], ss["bdl_dir"] = server, database, directory
    ss["bdl_recursive"], ss["bdl_force"] = recursive, force

    # ── TWO MODES, ONE PAGE — radio pills in the app's house style
    # (Perry, July 30: expected the File Catalog-style mode selector, not a
    # tab strip). Station 1 preps and teaches; station 2 loads. Selecting
    # 🧭 renders the assistant and RETURNS — the loader body below never
    # runs that pass, no re-indent needed. Lazy import ON PURPOSE (July
    # circular-import scar).
    if _planning:
        try:
            from dataview.import_data import page_load_assistant as _pla
            _pla.render(ss, server, database, ss.get("bdl_schema", "dataview"),
                        directory=directory)
            st.caption("Derived CSVs land BESIDE their source file. When "
                       "done here: switch to 📦 Load, point the Directory "
                       "at that folder, tick Force re-extract if it's seen "
                       "the names before, and scan.")
        except Exception as _pla_e:
            st.caption(f"(Load Assistant unavailable — {str(_pla_e)[:90]})")
        return

    catalog = st.text_input("FK catalog JSON (fast; blank = introspect live)",
                            value=ss.get("bdl_cat", r"dataview\schema_registry\dataview_fk_catalog.json"))
    bulk_dir = clean_path(st.text_input("Bulk staging folder (safe files kept here)", value=ss.get("bdl_bulk", r"C:\Bulk")))

    # ── the loader's work folder ────────────────────────────────────────────────
    try:
        _wn, _wb = work_usage(bulk_dir)
    except Exception:
        _wn, _wb = 0, 0
    if _wn:
        with st.expander(f"🧹 Work folder — {_wn:,} file(s), {_wb / 1048576:,.1f} MB",
                         expanded=False):
            st.caption(
                f"Everything this loader creates lives in "
                f"`{os.path.join(bulk_dir, _WORK_SUBDIR)}` — extract CSVs, .bcp files, the OCR "
                f"do-later bucket. **Nothing else in `{bulk_dir}` is touched**, by this panel "
                f"or by the loader. (That folder holds your source data — ~10 GB of it.)\n\n"
                "Extract CSVs are rewritten by the next scan, so they're safe to delete — but "
                "they're also what you read when the data-quality check says a column is "
                "blank. So this is opt-in, never automatic.")
            _c1, _c2 = st.columns([1, 2])
            _days = _c1.number_input("older than (days)", min_value=0, max_value=365, value=7,
                                     step=1, key="bdl_sweep_days")
            _keep = _c2.checkbox("keep the OCR do-later bucket", value=True, key="bdl_sweep_keep",
                                 help="Those documents were deferred FOR work. A queue that "
                                      "empties itself after a week isn't a queue.")
            if st.button("Preview sweep", key="bdl_sweep_prev"):
                try:
                    ss["bdl_sweep_list"] = sweep_work(bulk_dir, int(_days), dry_run=True,
                                                      keep_do_later=bool(_keep))
                except Exception as e:
                    st.error(str(e))
            _lst = ss.get("bdl_sweep_list")
            if _lst is not None:
                if not _lst:
                    st.success(f"Nothing older than {int(_days)} day(s).")
                else:
                    _tb = sum(x[2] for x in _lst)
                    st.dataframe(pd.DataFrame(
                        [{"file": os.path.relpath(p, bulk_dir), "age (days)": f"{a:.1f}",
                          "MB": f"{b / 1048576:,.2f}"} for p, a, b in _lst[:200]]),
                        hide_index=True, use_container_width=True)
                    if len(_lst) > 200:
                        st.caption(f"…and {len(_lst) - 200:,} more")
                    st.warning(f"**{len(_lst):,} file(s), {_tb / 1048576:,.1f} MB** would be "
                               f"deleted. This cannot be undone.")
                    if st.button(f"🗑 Delete these {len(_lst):,} file(s)", type="primary",
                                 key="bdl_sweep_go"):
                        gone = sweep_work(bulk_dir, int(_days), dry_run=False,
                                          keep_do_later=bool(_keep))
                        ss.pop("bdl_sweep_list", None)
                        ss["bdl_sweep_msg"] = (f"Deleted {len(gone):,} file(s), "
                                               f"{sum(x[2] for x in gone) / 1048576:,.1f} MB.")
                        st.rerun()
            if ss.get("bdl_sweep_msg"):
                st.success(ss.pop("bdl_sweep_msg"))

    schema = ss.get("bdl_schema", "dataview")
    ss["bdl_server"], ss["bdl_db"], ss["bdl_dir"], ss["bdl_cat"], ss["bdl_bulk"], ss["bdl_recursive"] = \
        server, database, directory, catalog, bulk_dir, recursive

    if directory and not (os.path.isdir(directory) or os.path.isfile(directory)):
        st.error(f"Not found: {directory}")
    if st.button("🔍 Scan", type="primary") and directory \
            and (os.path.isdir(directory) or os.path.isfile(directory)):
        try:
            eng = get_engine(server, database)
            # SINGLE FILE: profile its FOLDER, then keep only its rows.
            # Everything downstream — the gate, the catalog, Excel sheet
            # explosion, the topological promote order — is written against
            # a scan of a directory, and re-implementing any of it for one
            # file would be a second code path to keep in step. Scanning the
            # folder and filtering is the same answer with none of that.
            # A new scan is a new set of decisions — the previous run's
            # confirmation must not carry over and claim to describe it.
            ss["bdl_applied"] = False
            _one = os.path.isfile(directory)
            _scan_dir = os.path.dirname(os.path.abspath(directory)) if _one \
                else directory
            ss["bdl_scan"] = profile_directory_live(_scan_dir, eng, schema, bulk_dir,
                                                    False if _one else recursive,
                                                    force=force)
            if _one:
                # An Excel workbook explodes into one derived CSV per sheet,
                # named from the workbook's stem — those rows ARE this file,
                # so they are kept alongside an exact path match.
                _ap = os.path.abspath(directory)
                _stem = os.path.splitext(os.path.basename(_ap))[0].lower()
                _rows = []
                for _r in ss["bdl_scan"].get("rows", []):
                    _rp = os.path.abspath(_r.get("path") or "")
                    if _rp == _ap or os.path.basename(_rp).lower().startswith(
                            _stem + "__"):
                        _rows.append(_r)
                ss["bdl_scan"]["rows"] = _rows
                ss["bdl_scan"]["order"] = ss["bdl_scan"].get("_topo_order",
                                                             lambda _x: [])(_rows) \
                    if callable(ss["bdl_scan"].get("_topo_order")) else \
                    ss["bdl_scan"].get("order", [])
                ss["bdl_scan"]["single_file"] = _ap

  # catalog from live server
            ss.pop("bdl_uwi_files", None)                     # re-inspect UWIs for the new scan
            # A scan REWRITES every extract CSV — with a blank uwi, because that's what the
            # extractor produces. The stamp guard exists to stop _stamp_csvs re-reading every
            # CSV on each Streamlit rerun, and it survived the scan that invalidated it: the
            # gate resolved from saved assignments, saw the flag, and skipped the stamp. The
            # CSVs kept their blank uwi and the gate reported success. Whatever the scan
            # rewrites, the gate must re-stamp.
            ss.pop("bdl_uwi_stamp_sig", None)
            ss.pop("bdl_uwi_msg", None)
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
        # A whole FORMAT skipped is different from N rows skipped, and much easier to miss:
        # every DLIS file gated out means no DLIS row appears anywhere on this page. Nothing
        # says "skipped" — the format is simply absent, and absence doesn't announce itself.
        _sf = g.get("skipped_fmt") or {}
        if _sf:
            st.warning(
                "📇 **These formats were found but produced nothing** — every file was skipped "
                "as unchanged-and-already-loaded, so no rows appear for them below:\n\n"
                + "\n".join(f"- **{k}** — {n} file(s) found, {n} skipped" for k, n in
                            sorted(_sf.items()))
                + "\n\nTick **Force re-extract** above to process them anyway. (The catalog "
                  "matches on content hash, so a file that was loaded before is skipped even "
                  "if it now extracts differently — that's what Force is for.)")

    # ── where the scan's time went ───────────────────────────────────────────────
    tm = scan.get("timing")
    if tm and tm.get("phases"):
        _n = scan.get("n_files") or len(scan.get("rows") or [])
        with st.expander(f"⏱ Scan took {tm['total']}s — where it went", expanded=False):
            # Every column must hold ONE type. Mixing ints with "—" for the phases that have
            # no file count makes pyarrow fail the whole table ("Could not convert '—' with
            # type str: tried to convert to int64") — Streamlit then auto-fixes and renders
            # anyway, so the only symptom is a traceback in the console. Same Arrow trap as
            # file_viewer / file_header_store. Format to str here; these are for reading.
            _df = pd.DataFrame([{
                "phase": r["phase"],
                "seconds": f"{r['seconds']:.2f}",
                "% of scan": f"{r['pct']:.1f}",
                "files": str(r["files"]) if r["files"] is not None else "—",
                "s / file": (f"{r['seconds'] / r['files']:.3f}" if r["files"] else "—"),
            } for r in tm["phases"]])
            st.dataframe(_df, hide_index=True, use_container_width=True)
            if tm["unaccounted"] > 0.05:
                st.caption(
                    f"**{tm['unaccounted']}s ({round(100*tm['unaccounted']/tm['total'],1)}%) "
                    f"unaccounted**  — reference-table name matching, promote ordering, row "
                    f"bookkeeping, and anything not yet measured. If this is the biggest "
                    f"number on the list, the bottleneck is somewhere nobody has instrumented.")
            st.caption(
                "**s / file** is the number that predicts a bigger directory — the total "
                "doesn't. A phase at 0.1 s/file costs 100s over 1,000 files.  ·  "
                "**Parallelising:** hashing threads well (hashlib releases the GIL) but caps "
                "out on disk bandwidth, not core count. DLIS/LIS extraction does **not** — "
                "`frame.curves()` holds whole arrays in memory, so N workers means N times "
                "the peak. Measure before threading, and watch memory when you do.")

    _dfr = scan.get("pdf_deferred")
    if _dfr:
        _bucket = os.path.dirname(_dfr[0].get("copied_to") or "") or "the do-later bucket"
        st.warning(
            f"⏭ **{len(_dfr)} PDF(s) deferred — OCR over budget, NOT extracted.**\n\n"
            + "\n".join(f"- `{os.path.basename(d['path'])}` — {d['reason']}" for d in _dfr)
            + f"\n\nCopied to `{_bucket}`. These are scanned documents with no text layer; "
              f"they may hold real data. Raise `OCR_PAGE_TIMEOUT_S` / `OCR_BUDGET_S` in "
              f"`pdf_document_loader.py` and re-run the bucket on its own, or work them by "
              f"hand. Nothing from these files has been loaded.")

    for label, n, modname, dep in (scan.get("missing_extractors") or []):
        st.error(f"❌ **{n} {label} file(s) found but not scanned** — the `{modname}` extractor "
                 f"couldn't be imported, so they were skipped."
                 + (f"  Most likely `{dep}` isn't installed: `pip install {dep}`." if dep != "—" else "")
                 + f"  Also confirm `{modname}.py` is deployed next to `bulk_dir_loader.py` "
                   f"in `dataview\\import_data\\`.")
    # (removed 2026-07-20) the LAS/DLIS/LIS/WITSML/PDF/DOCX "not importable" notice —
    # the Directory Loader is CSV/Excel-only now, so those modules aren't its concern.
    if scan.get("extract_errors"):
        for fmt, err in scan["extract_errors"].items():
            st.warning(f"{fmt.upper()} extraction error: {err}")

    # ── FINGERPRINT RECALL, BEFORE THE GRID ─────────────────────────────────
    # This MUST run before the Files → tables grid is built, or the grid shows
    # every file unmatched and the recall lands a moment too late to be seen.
    # Self-guarded by _fp_recalled, so running it here changes nothing else.
    # (It also recalls a remembered SKIP — see remember_fp_skip.)
    if not scan.get("_fp_recalled"):
        _n_recall = 0
        _n_skip_recall = 0
        try:
            from sqlalchemy import text as _t0
            _eng0 = get_engine(server, database)
            with _eng0.connect() as _c0:
                for r in scan["rows"]:
                    _fp = r.get("fp")
                    if not _fp and r.get("cols"):
                        _fp = pdl.fingerprint_cols(sorted(r["cols"]))
                    if not _fp:
                        continue
                    _tt = [x[0] for x in _c0.execute(_t0(
                        "SELECT DISTINCT target_table FROM dataview.dv_column_map "
                        "WHERE source_file_pattern = :fp AND confirmed_ind = 'Y' "
                        "AND active_ind = 'Y'"), {"fp": _fp}).fetchall()]
                    if _SKIP_SENTINEL in {str(x).upper() for x in _tt}:
                        # A REMEMBERED SKIP. The operator has already said this
                        # shape is not loaded here; offering it again as ready
                        # is how the same decision gets made every month.
                        r["_target"] = r["table"] = None
                        r["_fp_skip"] = True
                        r["_fp_known"] = True
                        _n_skip_recall += 1
                    elif len(_tt) == 1:                      # ambiguity = no recall
                        r["_target"] = r["table"] = str(_tt[0]).upper()
                        r["_score0"] = 1.0
                        r["_fp_known"] = True
                        _n_recall += 1
        except Exception:
            pass
        scan["_fp_recalled"] = True
        scan["_fp_recall_n"] = _n_recall
        scan["_fp_skip_n"] = _n_skip_recall

    # The full per-table grid still follows — it is where a target is
    # overridden and where FK-child skipping is expressed. The triage view
    # above answers "what will happen"; this answers "change it".
    st.divider()
    # CLOSED BY DEFAULT (Aug 9, Perry: "why are there two screens asking me
    # what files to skip?"). The tabs above are the everyday surface and skip a
    # FILE; this grid skips a TABLE — every file mapped to it — and exists for
    # the narrower job of RETARGETING a file the scan matched wrongly. Two
    # controls both labelled "skip", both with an Apply button, and nothing on
    # screen saying which was which. Folding it away leaves one obvious surface
    # without removing the one that can override a target.
    # NOT put inside an expander, though it was tempting (Perry: "why are there
    # two screens asking me what files to skip?"). Wrapping this needs the ~200
    # lines below re-indented, and this block contains expanders of its own —
    # and expanders cannot nest. The confusion was never the grid's PLACE, it
    # was that both surfaces said "skip" and meant different scopes. So the
    # labels carry the scope instead: "skip file" in the tabs, "skip table"
    # here.
    if scan.get("_fp_skip_n"):
        st.caption(f"⏭ {scan['_fp_skip_n']} file(s) pre-set to **— skip —** from a previous "
                   f"run — the shape was skipped before. Point one at a table to load it "
                   f"again and the skip is forgotten.")
    st.subheader("Files → tables — full control  ·  table-wide")
    st.caption("**Every file in the folder is listed here — this is where you skip and "
               "retarget.** Ticking **skip table** drops that table AND its FK children (every "
               "file mapped to it); to drop just ONE file, set its **→ table** to '— skip —'. "
               "What survives is summarised below, in load order. "
               "Auto-matched by columns (data) or filename (references). Nothing is ever "
               "skipped automatically — the **why** column carries the warnings (weak match, "
               "uncovered required columns); skipping is YOUR call.")
    all_tables = scan.get("all_tables", [])
    schema = ss.get("bdl_schema", "dataview")
    for r in scan["rows"]:                                  # remember the matched target across skip toggles
        r.setdefault("_target", r.get("table"))

    # ── fingerprint → table recall (Perry's flip, July 29) ───────────────────
    # A column shape confirmed before IS the table assignment — no scoring
    # needed. dv_column_map already keys every confirmed map by fingerprint;
    # this is the read side that was missing: recall the table at 100% for a
    # known shape, so the weak-match pre-tick and the sufficiency auto-skip
    # never fire on a file the operator has mapped before. First encounters
    # still get the scorer's guess for the operator to override in → table;
    # the override + Phase-2 💾 Save is what teaches the store — provide the
    # table once, never again for that shape.
    if scan.get("_fp_recall_n"):
        st.caption(f"🔁 {scan['_fp_recall_n']} file(s) recognized by column shape "
                   f"(fingerprint seen + confirmed before) — table assigned at 100%, "
                   f"no scoring.")

    targets = sorted({(r["_target"] or "").upper() for r in scan["rows"] if r.get("_target")})

    # data-sufficiency (auto-X) + FK child graph (cascade), cached per target set
    cov, kids = {}, {}
    try:
        eng = get_engine(server, database)
        # Vendor vocabulary must exist BEFORE the sufficiency check: it is
        # synonym-aware (build_map_review -> _syn), so without the seeds a
        # Teapot-headed file reads "uwi uncovered" and dv_well gets auto-
        # skipped, cascading to every child. Seeding only in Phase 2 was one
        # phase too late (July 29 screenshot).
        if not ss.get("bdl_vendor_seeded"):
            _n_seed0, _seed_skips0 = seed_vendor_synonyms(eng, schema)
            ss["bdl_vendor_seeded"] = True
            for _sk0 in _seed_skips0:
                st.caption(f"(vendor synonym skipped — {_sk0})")
        if ss.get("bdl_suff_sig") != tuple(targets):
            ss["bdl_suff"] = _data_sufficiency(eng, scan["rows"], schema)
            ss["bdl_suff_sig"] = tuple(targets)
        cov, kids = ss["bdl_suff"]
    except Exception as e:
        st.caption(f"⚠️ data-sufficiency check unavailable ({e}); the skip column is manual only.")

    ss.setdefault("bdl_skips", set())
    # AUTO-SKIP REMOVED (Perry, July 29): after three rounds of the screen
    # re-deriving its own opinion over the operator's (sufficiency auto-tick,
    # weak-match pre-tick, cascade eating overrides), skip is now MANUAL ONLY.
    # The `why` column still carries every warning — uncovered required
    # columns, weak scores — but nothing ever ticks itself.
    # _cascade returns DESCENDANTS only; the skipped tables themselves must
    # be unioned in. Without it, every grid rebuild after an Apply rendered
    # directly-skipped tables UNTICKED (the operator's skips looked "cleared"),
    # and the next Apply read those cleared boxes as unskips and staged
    # everything — Perry hit it twice (July 29 + 30) before this fix.
    closure = _cascade(ss["bdl_skips"], kids) | set(ss["bdl_skips"])

    # A match this weak is a guess, not a match. The auto-matcher scores on column overlap;
    # below this the target is more likely wrong than right (Completion_Parameters_
    # Perforations.csv → DV_WELL_GOM_BACKUP scored low and was still proposed). Pre-tick skip
    # rather than pre-tick load: a wrong target that stages is worse than one you have to
    # un-skip, because staging DROP+CREATEs the table.
    WEAK_MATCH = 0.30

    for _r in scan["rows"]:
        if "_score0" not in _r:
            _r["_score0"] = float(_r.get("score") or 0)

    _matched_targets = {(x.get("_target") or "").upper() for x in scan["rows"] if x.get("_target")}

    def _has_matched_kids(tu):
        """Would skipping this table drag others down with it?"""
        return bool(set(_cascade({tu}, kids)) - {tu} & _matched_targets)

    def _weak(r):
        return bool(r.get("_target")) and float(r.get("_score0") or 0) <= WEAK_MATCH

    def _reason(tu):
        if cov.get(tu):
            return "uncovered required: " + ", ".join(cov[tu])
        if tu in closure and tu not in ss["bdl_skips"]:
            return "child of a skipped table"
        return ""

    def _why(r):
        tu = (r.get("_target") or "").upper()
        base = _reason(tu)
        if _weak(r):
            pct = int(float(r.get("_score0") or 0) * 100)
            w = (f"⚠ weak match ({pct}%) — the auto-matcher is guessing; "
                 f"verify or change the target before loading")
            return f"{base} · {w}" if base else w
        return base

    def _filled_cols(path, ncols):
        """How many columns hold at least one non-blank value? None when not knowable cheaply.

        `cols` (the header count) says what the file CLAIMS; this says what it delivers. A
        12-column extract where 3 columns have data is worth seeing before you map it — that
        is the shape of the dropped-column problem, one screen earlier.

        Bounded on purpose: a full read per file, per rerun, to draw a grid is exactly the kind
        of cost that turned a 35s scan into a mystery. Skips non-CSV sources (an .xlsx read as
        CSV is nonsense, not data), skips files over the cap, caches on (path, mtime), and
        stops early once every column has been seen filled.
        """
        try:
            if not path or not str(path).lower().endswith(".csv") or not os.path.exists(path):
                return None
            if os.path.getsize(path) > _FILLED_MAX_BYTES:
                return None                     # too big to read just to populate a column
            key = (path, os.path.getmtime(path))
            cache = ss.setdefault("_filled_cache", {})
            if key in cache:
                return cache[key]
            seen = set()
            with open(path, encoding="utf-8-sig", newline="") as fh:
                rd = csv.DictReader(fh)
                hdr = rd.fieldnames or []
                for row in rd:
                    for k, v in row.items():
                        if k not in seen and v is not None and str(v).strip() != "":
                            seen.add(k)
                    if len(seen) >= len(hdr):
                        break                   # every column has data — nothing left to learn
            cache[key] = len(seen)
            return cache[key]
        except Exception:
            return None                         # unknown, and says so — never a guessed number

    def _filled_str(r):
        n = len(r["cols"])
        f = _filled_cols(r.get("path"), n)
        return f"{n}" if f is None else f"{f}/{n}"

    grid = pd.DataFrame([{
        "#": _i + 1,
        "skip": ((r["_target"] or "").upper() in closure) or not r["_target"],
        # 📇 = fingerprint RECALL (this exact shape confirmed before) —
        # visually distinct from a scored 100%, which is still a guess.
        # "I kind of liked remembered" — Perry, July 30.
        "match": ("📇 remembered" if r.get("_fp_known")
                  else f"{int(float(r.get('_score0') or 0) * 100)}%"),
        "rows": (f"{r['n_rows']:,}" if isinstance(r.get("n_rows"), int) else "—"),
        "filled": _filled_str(r),
        "→ table": (r["_target"] or "— skip —"),
        "file": r["file"],
        "kind": r["kind"] or "",
        "why": _why(r)}
        for _i, r in enumerate(scan["rows"])])
    # WHAT THIS GRID WAS DRAWN FROM. Compared after the read-back below: if
    # Apply changed anything, the grid on screen is already stale and has to be
    # redrawn, or the operator sees their ticks vanish and ticks them again.
    _built_from = (tuple((r["file"], r.get("_target")) for r in scan["rows"]),
                   frozenset(ss.get("bdl_skips") or set()))
    # 🔑 bdl_unskips IS DELIBERATELY NOT IN THIS SIGNATURE (Aug 10).
    #
    # The signature drives BOTH the frame cache and the widget KEY, and the
    # read-back ~200 lines below MUTATES bdl_unskips on every render — it is
    # empty when this grid is first drawn and full by the end of that same
    # pass. So the key differed between the render that DREW the editor and
    # the render that READ its submission: Streamlit saw a key it had never
    # seen, returned the frame defaults, and the operator's ticks went in the
    # bin. Every FIRST Apply was silently discarded; the second worked because
    # by then bdl_unskips had stopped moving. Perry: "I filled skips, then both
    # grids appeared and my skips were gone... it's like the first grid was
    # ignored."
    #
    # bdl_unskips is a record of intent and changes nothing this grid DISPLAYS,
    # so it has no business deciding widget identity. What the grid shows is a
    # function of the targets and the skips — those two, and nothing else.
    _grid_sig = (tuple((r["file"], r.get("_target")) for r in scan["rows"]),
                 tuple(sorted(ss.get("bdl_skips", set()))))
    if ss.get("bdl_scan_grid_sig") != _grid_sig:
        ss["bdl_scan_grid"] = grid
        ss["bdl_scan_grid_sig"] = _grid_sig
    grid = ss["bdl_scan_grid"]

    # In a form nothing is sent until Apply — so editing a target no longer reruns the whole
    # page on every keystroke, re-deriving the promote order and redrawing the gate half way
    # through your edits. Make all the changes, then commit them once.
    with st.form("bdl_files_tables"):
        edited = st.data_editor(
            grid, hide_index=True, use_container_width=True,
            # key versioned by the grid signature: frame and widget state move
            # TOGETHER, so a rebuilt grid always shows the truthful defaults
            # instead of stale edits over a changed frame
            key=f"bdl_scan_edit_{abs(hash(_grid_sig)) % 100000}",
            column_order=["#", "skip", "match", "rows", "filled", "→ table", "file", "kind",
                          "why"],
            column_config={
                "#": st.column_config.NumberColumn(
                    disabled=True, width="small",
                    help="Row number — warnings elsewhere on this page refer to it"),
                # The DISPLAY label carries the scope; the underlying key stays
                # "skip" so the read-back below and every caller are untouched.
                "skip": st.column_config.CheckboxColumn(
                    "skip table", width="small",
                    help="Drop this table AND its FK children from staging & promote — "
                         "every file mapped to it. To drop ONE file, set its → table "
                         "to '— skip —' on this same row. Never pre-ticked — the why "
                         "column warns, you decide."),
                "match": st.column_config.TextColumn(
                    disabled=True, width="small",
                    help="How much of the target's shape the source columns cover. Low = the "
                         "auto-matcher is guessing."),
                "rows": st.column_config.TextColumn(
                    disabled=True, width="small",
                    help="Rows in the extracted CSV. '—' = not counted (CSV/Excel sources are "
                         "profiled, not extracted)"),
                "filled": st.column_config.TextColumn(
                    disabled=True, width="small",
                    help="Columns holding at least one non-blank value / total columns. "
                         "3/12 means nine columns are empty in every row. A bare number means "
                         "the file wasn't read (not a CSV, or over the size cap) — total only."),
                "→ table": st.column_config.SelectboxColumn(options=["— skip —"] + all_tables,
                                                            required=True),
                "file": st.column_config.TextColumn(disabled=True),
                "kind": st.column_config.TextColumn(disabled=True, width="small"),
                "why": st.column_config.TextColumn("why (uncovered required cols)", disabled=True)})
        _applied_now = st.form_submit_button("Apply targets & table skips",
                                             type="primary",
                                             use_container_width=True)
    if _applied_now:
        ss["bdl_applied"] = True

    # read intended target + explicit skip ticks, then cascade to FK children
    new_skips = set()
    # An explicit UNTICK is a decision with the same standing as a tick.
    # (bdl_unskips predates the July-29 removal of all auto-skip logic; it
    # stays as a record of operator intent and costs nothing.)
    ss.setdefault("bdl_unskips", set())
    _file_skips = ss.get("bdl_file_skips") or set()
    for i, r in enumerate(scan["rows"]):
        pick = edited.iloc[i]["→ table"]
        r["_target"] = None if pick in ("— skip —", "", None) else pick.upper()
        # a PER-FILE skip (triage view) drops just this row, without touching
        # the table or its other files — the per-table checkbox below is the
        # blunt instrument, this is the scalpel
        if (r.get("path") or r.get("file", "")) in _file_skips:
            r["_target"] = None
        if bool(edited.iloc[i]["skip"]) and r["_target"]:
            new_skips.add(r["_target"])
            ss["bdl_unskips"].discard(r["_target"])
        elif r["_target"]:
            ss["bdl_unskips"].add(r["_target"])
    ss["bdl_skips"] = new_skips

    # ── REDRAW IF APPLY CHANGED ANYTHING (Aug 10) ───────────────────────────
    # Perry: "I have to select my skips on the first table twice before it
    # takes."
    #
    # Pressing Apply reruns the script from the top. On THAT run the grid is
    # rebuilt near line 4780 from ss["bdl_skips"] — which still holds the
    # PREVIOUS value, because the line that updates it is the one just above
    # this comment, ~200 lines further down. So the sequence is: grid drawn
    # from the old state, ticks read, new state written, page finishes. The
    # receipt below is correct; the grid above it is one interaction behind,
    # and looks exactly as though the tick did nothing.
    #
    # One more rerun makes the grid agree with the state it just produced.
    # It TERMINATES: the second pass builds from the new state, the read-back
    # yields the same state, the comparison matches and nothing reruns again.
    _now = (tuple((r["file"], r.get("_target")) for r in scan["rows"]),
            frozenset(new_skips))
    if _now != _built_from:
        ss["bdl_scan"] = scan
        st.rerun()

    # REMEMBER THE DECISION AGAINST THE FILE'S SHAPE (Aug 10). Perry: "if I
    # skip the mapping it should update the fingerprint so if I load it again
    # it won't show those files as being mapped."
    #
    # Both directions, or the memory is a trap: pointing a row AT a table must
    # clear a remembered skip, otherwise a file can never be un-skipped and the
    # operator has no way to see why it keeps vanishing.
    _fp_skip_now, _fp_load_now = set(), set()
    for i, r in enumerate(scan["rows"]):
        _fpr = r.get("fp")
        if not _fpr and r.get("cols"):
            _fpr = pdl.fingerprint_cols(sorted(r["cols"]))
        if not _fpr:
            continue
        (_fp_skip_now if not r["_target"] else _fp_load_now).add(_fpr)
    if _fp_skip_now or _fp_load_now:
        try:
            remember_fp_skip(get_engine(server, database),
                             _fp_skip_now, _fp_load_now)
        except Exception:
            pass
    # A skipped PARENT only drags its children down when the parent table is
    # EMPTY in the database. dv_well already loaded (a prior run) means tops/
    # surveys can promote against the EXISTING rows — skipping the headers
    # FILE must not cascade-skip its children. So cascade only through skipped
    # parents that hold no rows; directly-skipped tables still drop.
    _populated = set()
    try:
        from sqlalchemy import text as _tc
        _engc = get_engine(server, database)
        with _engc.connect() as _cc:
            for _tsk in new_skips:
                try:
                    if _cc.execute(_tc(
                            f"SELECT TOP 1 1 FROM {schema}.{_tsk.lower()}")).fetchone():
                        _populated.add(_tsk)
                except Exception:
                    pass                      # table missing -> treat as empty
    except Exception:
        pass
    closure = _cascade(new_skips - _populated, kids) | new_skips
    if _populated:
        st.caption("⛓ skip cascade stopped at populated parent(s): "
                   + ", ".join(sorted(_populated))
                   + " — their children can still load against the existing rows.")

    # effective target: dropped if skipped directly or cascaded from a skipped parent
    for r in scan["rows"]:
        tgt = r["_target"]
        r["table"] = None if (not tgt or tgt in closure) else tgt
        r["kind"] = _kind_of(r["table"]) if r["table"] else None
        # `score` here means "has an effective target", which is NOT what the matcher said.
        # _score0 keeps the real verdict; anything downstream that wants confidence must read
        # that. Overwriting both would make the weak-match rule feed on its own output.
        r.setdefault("_score0", float(r.get("score") or 0))
        r["score"] = 1.0 if r["table"] else 0.0
    order = [t for t in (scan.get("order") or []) if t in {r["table"] for r in scan["rows"] if r["table"]}]
    for r in scan["rows"]:
        t = r.get("table")
        if t and t not in order:
            (order.insert(0, t) if t.startswith("DV_R_") else order.append(t))
    scan["order"] = order
    ss["bdl_scan"] = scan

    # ── WHAT WILL LOAD ──────────────────────────────────────────────────────
    # Three tabs used to sit here — Ready / Needs planning / Skipped — each
    # re-listing every file in the folder. It read as a second scan, because
    # that is what it looked like. Perry: "we don't need the second table as
    # is... what we need is a simple table confirming what will be loaded."
    #
    # The grid above is the control: skip a row, or point it at a different
    # table. This is the receipt. It is derived straight from the effective
    # targets that grid just produced, so it cannot disagree with it.
    _will = [r for r in scan["rows"] if r.get("table")]
    _ordi = {t: i for i, t in enumerate(scan.get("order") or [])}
    _will.sort(key=lambda r: (_ordi.get(r["table"], 999), r.get("file", "")))

    if not ss.get("bdl_applied"):
        # NOTHING TO CONFIRM YET (Aug 10). Perry: "only the first grid should
        # appear and only after I make my selection should the second grid
        # appear confirming my selection."
        #
        # Showing a receipt before any decision was made is what made this read
        # as a second scan: two tables of the same files side by side, one of
        # them describing choices nobody had made yet.
        #
        # NOT an early return — everything below (promote order, the UWI gate,
        # staging, the phases) still has to render. This gates ONE panel.
        st.caption("Set your skips and targets above, then press "
                   "**Apply targets & table skips** — what will load appears here.")
    elif not _will:
        st.subheader("What will be loaded")
        st.info("Nothing selected — every file is set to **— skip —** in the grid above.")
    else:
        st.subheader("What will be loaded")
        st.dataframe(
            pd.DataFrame([{"#": i + 1, "file": r.get("file", ""),
                           "→ table": r["table"],
                           "rows": (f"{r['rows']:,}" if isinstance(r.get("rows"), int) else "—")}
                          for i, r in enumerate(_will)]),
            hide_index=True, use_container_width=True)
        _by = {}
        for r in _will:
            _by[r["table"]] = _by.get(r["table"], 0) + 1
        st.info("Loads in order: " + " → ".join(
            f"**{t}** ({n} file{'s' if n > 1 else ''})"
            for t, n in sorted(_by.items(), key=lambda kv: _ordi.get(kv[0], 999))))



    if scan["order"]:
        if scan.get("fk_warning"):
            st.error(f"⚠ No FK graph — promote order is ALPHABETICAL, not topological. "
                     f"{scan['fk_warning']}. Children will promote before their parents. "
                     f"Clear the 'FK catalog JSON' box to introspect live, then re-scan.")
        st.caption(f"Promote order ({'topological' if scan.get('fk_edges') else '⚠ ALPHABETICAL — no FK graph'}): "
                   + "  →  ".join(scan["order"]))
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
        # "above" is doing a lot of work on a page this long. Name the documents, and say
        # which panel they're in — the gate sits between Files → tables and PDF field review,
        # and this warning is at the bottom of the page.
        _pend = ss.get("bdl_uwi_pending")
        if _pend is None:
            st.error("Staging is blocked — see the error in the **⚠ Assign UWI** section "
                     "above. Nothing to assign; something failed.")
        elif _pend:
            st.warning(
                f"**Staging blocked: {len(_pend)} document(s) still need a UWI.** Scroll up to "
                f"**⚠ Assign UWI before staging** (between *Files → tables* and *PDF field "
                f"review*) and give each one a UWI that exists in `dv_well`, or set its action "
                f"to **SKIP**:\n\n"
                + "\n".join(f"- `{x}`" for x in _pend[:12])
                + (f"\n- …and {len(_pend) - 12} more" if len(_pend) > 12 else ""))
        else:
            st.warning("Staging is blocked by the UWI gate, but nothing is listed as pending — "
                       "↻ **Reset run** and re-scan.")
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
        # bcp's own output is the truth behind the exit code — but as a wall of text it hid the
        # only number that matters (expected vs copied) among the network packet size and the
        # rows-per-second. A grid puts the comparison in one column. The raw text is NOT
        # discarded: it is printed in full for any file that failed or came up short, because
        # that is when you need bcp's actual words, and a parse of them can't be trusted
        # precisely when something has gone wrong.
        with st.expander("bcp — per file", expanded=any(r["loaded"] == 0 for r in staged)):
            import re as _re

            def _copied(out):
                """bcp prints '6 rows copied.' — None if it didn't, never a guess."""
                m = _re.search(r"([\d,]+)\s+rows?\s+copied", out or "", _re.I)
                return int(m.group(1).replace(",", "")) if m else None

            def _ms(out):
                m = _re.search(r"Clock Time \(ms\.?\)[^:]*:\s*([\d,]+)", out or "", _re.I)
                return int(m.group(1).replace(",", "")) if m else None

            grid, troubled = [], []
            for r in staged:
                for lg in r["logs"]:
                    cop = _copied(lg["out"])
                    exp = lg["expected"]
                    if lg["rc"] != 0:
                        status = "🔴 failed"
                    elif cop is None:
                        status = "⚠ unreadable"      # rc=0 but bcp said nothing we understood
                    elif cop != exp:
                        status = f"🔴 short {exp - cop:+,}"
                    else:
                        status = "✅"
                    if status != "✅":
                        troubled.append((r["stg_table"], lg))
                    ms = _ms(lg["out"])
                    grid.append({
                        "status": status,
                        "stg table": r["stg_table"].split(".")[-1],
                        "file": lg["file"],
                        "expected": f"{exp:,}",
                        "copied": f"{cop:,}" if cop is not None else "—",
                        "rc": str(lg["rc"]),
                        "ms": f"{ms:,}" if ms is not None else "—",
                    })
            # every column a single type — mixing ints with "—" makes pyarrow reject the table
            st.dataframe(
                pd.DataFrame(grid), hide_index=True, use_container_width=True,
                column_order=["status", "stg table", "file", "expected", "copied", "rc", "ms"],
                column_config={
                    "status": st.column_config.TextColumn(width="small"),
                    "expected": st.column_config.TextColumn(
                        width="small", help="Rows in the CSV handed to bcp"),
                    "copied": st.column_config.TextColumn(
                        width="small", help="What bcp reported copying. '—' = bcp's output "
                                            "didn't say, so this is unknown rather than 0"),
                    "rc": st.column_config.TextColumn(
                        width="small",
                        help="bcp.exe's exit code. 0 = it believes it succeeded; anything "
                             "else = it failed. NOT the real check: bcp can exit 0 and still "
                             "copy fewer rows than it was given — that's why expected and "
                             "copied sit next to each other."),
                    "ms": st.column_config.TextColumn(
                        width="small", help="bcp's own clock time for the copy"),
                })
            st.caption("**expected** = rows in the CSV · **copied** = what bcp reported. "
                       "They must match; anything else is data lost between the two. "
                       "**rc** is bcp's exit code — necessary, not sufficient.")

            # Only the ones that went wrong get the full text — nested expanders aren't allowed
            # here, and a clean run doesn't need bcp's chatter.
            for stg, lg in troubled:
                st.markdown(f"**{stg}** ⟵ `{lg['file']}`")
                st.code(lg["cmd"], language="text")
                st.code(lg["out"] or lg["err"] or "(no output)", language="text")
            if not troubled:
                if st.checkbox("show raw bcp output anyway", key="bdl_bcp_raw"):
                    for r in staged:
                        for lg in r["logs"]:
                            st.markdown(f"**{r['stg_table']}** ⟵ `{lg['file']}`")
                            st.code(lg["cmd"], language="text")
                            st.code(lg["out"] or lg["err"] or "(no output)", language="text")
        ok = sum(1 for r in staged if not r["errors"] and r["loaded"])
        if ok == len(staged):
            st.success(f"{ok}/{len(staged)} tables staged with rows. Next: batch Match & Map (Phase 2).")
        else:
            # Name them. "see bcp output above for the rest" makes you hunt a 🔴 through a
            # 31-row grid to learn something the message already knew.
            bad = [r for r in staged if r["errors"] or not r["loaded"]]
            lines = []
            for r in bad:
                if r["errors"]:
                    why = "; ".join(r["errors"])[:200]
                elif not r["loaded"]:
                    exp = sum(lg["expected"] for lg in r["logs"])
                    why = ("the source CSV had no data rows"
                           if exp == 0 else
                           f"bcp reported no error, but the table is empty — "
                           f"{exp:,} row(s) were expected")
                else:
                    why = "unknown"
                lines.append(f"- **{r['target']}** ⟵ `{r['stg_table']}` — {why}")
            st.error(f"{ok}/{len(staged)} tables staged with rows. These did not:\n\n"
                     + "\n".join(lines))

    # ── PHASES 2-6 ──────────────────────────────────────────────────────────
    # These are the road for files the store cannot map on its own. When it
    # CAN, the streamlined button above has already done the work and this
    # machinery is just noise on the page — so it collapses. Collapsed, not
    # removed: "show me what it would have done" is a fair thing to want,
    # and hiding the phases outright would make the fast path unauditable.
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

    # LAST, deliberately: the anchor has to exist in the DOM before anything
    # tries to scroll to it, and components run after the markdown around them
    # is emitted. Called unconditionally — it is a no-op unless a phase asked.
    _scroll_to(ss, "fk-violations")


if __name__ == "__main__":
    if st is not None:
        run()
