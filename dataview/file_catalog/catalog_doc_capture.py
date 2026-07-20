"""
catalog_doc_capture.py
======================
Consolidated document/data capture for File Catalog — ONE extractor per format,
all from the Directory Loader, mapped into File Catalog's file_catalog.cat_*
mirrors so File Catalog's own promote lifts them to dataview.dv_* unchanged.

    EXTRACTORS[ext] -> (extract_fn, source_label)
    extract_fn(path, source) -> {"uwi": str, <kind>: [row, ...], ...}

Adding a format is a registry entry, not a new branch: give it an extract_fn
that returns the res-shape and make sure its kinds are in KIND_TO_CAT.

Why this exists: File Catalog's own per-format extractors diverged from the
loader's (e.g. LAS log_id `{uwi}-LAS` vs the loader's `LOG_<uwi>`, which minted
duplicate logs). Routing every format through the loader's extractor makes File
Catalog and the loader produce identical dv_* for the same input.

Schema-safe: each row is intersected against the target cat_* table's real
INFORMATION_SCHEMA columns — an unmapped key is dropped and logged, never
inserted blindly; per-table capture is guarded so one bad table can't zero the
rest. Blank UWI after self-resolution -> note 'uwi_unresolved' (the New-well /
UWI review gate's job), never a NULL-uwi insert that would break promote.
"""
import os
import re
import uuid
from datetime import datetime

import importlib


def _load_loader(modname):
    """Import a Directory Loader extractor by module name from whichever
    package holds it (import_data or file_catalog) -> (extract_file, TARGET).
    Returns (None, {}) if not found; the registry treats that as 'no
    extractor for this format'. Avoids hard-coding the wrong package."""
    for _pkg in ("dataview.import_data", "dataview.file_catalog"):
        try:
            _m = importlib.import_module(f"{_pkg}.{modname}")
            return getattr(_m, "extract_file", None), getattr(_m, "TARGET", {})
        except ImportError:
            continue
    return None, {}


_pdf_extract,  _PDF_TARGET  = _load_loader("pdf_document_loader")
_docx_extract, _DOCX_TARGET = _load_loader("docx_document_loader")


# ── LAS: adapt the loader's (log_rows, curve_rows) into the res-shape ─────────
def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _las_extract(path, source="LAS"):
    """dataview.import_data.las_header_loader, per-file, as a res dict.

    The loader emits logs + curves (with its own stable log_id/curve_id) but no
    well header, so we add one from the ~WELL section here. Curve rows carry the
    loader's min/max fields; capture() maps whatever cat_well_log_curve actually
    has and logs the rest."""
    from dataview.import_data.las_header_loader import (
        extract_directory, parse_las, _wget)
    d = os.path.dirname(os.path.abspath(path)) or "."
    log_rows, curve_rows = extract_directory(d, source=source, files=[path])
    well, _c, _dat = parse_las(path)
    uwi = (log_rows[0]["uwi"] if log_rows else _wget(well, "UWI", "API", "WELL")) or ""
    res = {"uwi": uwi, "doc_type": "las",
           "well": [], "well_log": log_rows, "well_log_curve": curve_rows}
    if uwi:
        res["well"] = [{
            "well_name":         _wget(well, "WELL") or uwi,
            "operator_name":     _wget(well, "COMP", "PROV"),
            "field_name":        _wget(well, "FLD", "FIELD"),
            "province_state":    _wget(well, "STAT", "STATE"),
            "county":            _wget(well, "CNTY", "COUNTY"),
            "country":           _wget(well, "CTRY", "COUNTRY"),
            "surface_latitude":  _num(_wget(well, "LATI", "LAT")),
            "surface_longitude": _num(_wget(well, "LONG", "LON")),
            "final_td":          _wget(well, "STOP", "TD"),
        }]
    return res


# ── registry ─────────────────────────────────────────────────────────────────
# .docx is live via docx_document_loader (loaded above); other Office
# (xlsx/xls/odt/ods/csv/doc) stays on dv_office_loader in _do_office.
EXTRACTORS = {
    ".pdf":  (_pdf_extract,  "PDF"),
    ".las":  (_las_extract,  "LAS"),
    ".docx": (_docx_extract, "DOCX"),
}


def has_docx_extractor():
    fn, _ = EXTRACTORS.get(".docx", (None, ""))
    return fn is not None


def _extractor_for(fext):
    return EXTRACTORS.get((fext or "").lower(), (None, ""))


# ── kind -> cat_* table ──────────────────────────────────────────────────────
def _cat_name(dv_table):
    t = dv_table.lower()
    return "cat_" + (t[3:] if t.startswith("dv_") else t)


KIND_TO_CAT = {kind: _cat_name(dv)
               for kind, dv in {**_PDF_TARGET, **_DOCX_TARGET}.items()}
KIND_TO_CAT.update({                     # LAS-adapter kinds (in neither TARGET)
    "well_log": "cat_well_log",
    "well_log_curve": "cat_well_log_curve",
})

# capture() injects these from its kwargs — never pass them inside a row.
_INJECTED = {"UWI", "SOURCE", "INVENTORY_ID", "SOURCE_PATH"}
# parents before children so cat_* provenance reads naturally (promote topo-sorts anyway)
_ORDER = ["well", "well_log", "well_log_curve", "log", "curve", "core"]

_COLS_CACHE = {}


def _cat_columns(engine, table):
    if table in _COLS_CACHE:
        return _COLS_CACHE[table]
    from sqlalchemy import text as _t
    try:
        with engine.connect() as c:
            rows = c.execute(_t(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = 'file_catalog' AND TABLE_NAME = :tbl"),
                {"tbl": table}).fetchall()
        cols = {r[0].upper() for r in rows}
    except Exception:
        cols = set()
    _COLS_CACHE[table] = cols
    return cols


def _prep_rows(kind, rows, cat_cols, now):
    dropped = set()
    audit = {"ACTIVE_IND": "Y", "ROW_CREATED_BY": "DataWrangler",
             "ROW_CREATED_DATE": now}
    out = []
    for r in rows:
        row = dict(r)
        row.update(audit)
        if kind == "well":
            row.setdefault("ROW_QUALITY", "FINAL")
            row["PPDM_GUID"] = str(uuid.uuid4())
        kept = {}
        for k, v in row.items():
            ku = k.upper()
            if ku in _INJECTED:
                continue
            (kept.__setitem__(k, v) if ku in cat_cols else dropped.add(k))
        out.append(kept)
    return out, dropped


def capture_document(engine, dialect, fpath, fext, uwi, inventory_id,
                     log=lambda m: None):
    """Extract fpath with the registered loader and capture its rows into
    file_catalog.cat_*. Returns the _load_rows_to_catalog shape:
        {ok, loaded, errors:[...], rt, note, detail:{table: n}, uwi}"""
    res = {"ok": False, "loaded": 0, "errors": [], "rt": (fext or "").lstrip(".").upper(),
           "note": "", "detail": {}, "uwi": (uwi or "").strip()}

    extract_fn, source = _extractor_for(fext)
    if extract_fn is None:
        res["note"] = f"no_extractor:{fext}"
        return res

    try:
        doc = extract_fn(fpath, source=source)
    except Exception as e:
        res["errors"].append(f"extract: {e}")
        return res

    if doc.get("deferred"):
        res["note"] = f"deferred:{doc['deferred']}"
        return res
    if doc.get("error"):
        res["errors"].append(str(doc["error"]))
        return res

    uwi = (uwi or "").strip() or (doc.get("uwi") or "").strip()
    res["uwi"] = uwi
    if not uwi:
        res["note"] = "uwi_unresolved"
        log(f"  [~] {os.path.basename(fpath)}: no UWI resolved "
            f"(doc_type={doc.get('doc_type')}) — deferred to UWI gate")
        return res

    if not inventory_id:
        try:
            from sqlalchemy import text as _t
            with engine.connect() as c:
                _r = c.execute(_t(
                    "SELECT TOP 1 INVENTORY_ID FROM file_catalog.GLOBAL_FILE_CATALOG "
                    "WHERE FILE_PATH = :p"), {"p": fpath}).fetchone()
            inventory_id = _r[0] if _r else None
        except Exception:
            inventory_id = None

    try:
        from dataview.file_catalog.catalog_capture import capture as _cap
    except ImportError as e:
        res["errors"].append(f"catalog_capture import: {e}")
        return res

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_dropped = {}

    kinds = _ORDER + [k for k in KIND_TO_CAT if k not in _ORDER]
    for kind in kinds:
        rows = doc.get(kind) or []
        if not rows:
            continue
        table = KIND_TO_CAT[kind]
        cat_cols = _cat_columns(engine, table)
        if not cat_cols:
            res["errors"].append(f"{kind}: file_catalog.{table} not found — skipped")
            log(f"  [x] {table} missing — {len(rows)} {kind} row(s) skipped")
            continue
        prepared, dropped = _prep_rows(kind, rows, cat_cols, now)
        if dropped:
            all_dropped[table] = sorted(dropped)
        try:
            n = int(_cap(engine, table, prepared, uwi=uwi,
                         inventory_id=inventory_id, source_path=fpath, source=source) or 0)
            if n:
                res["loaded"] += n
                res["detail"][table] = res["detail"].get(table, 0) + n
        except Exception as e:
            res["errors"].append(f"{table}: {e}")
            log(f"  [x] {table}: {str(e)[:300]}")

    if all_dropped:
        log("  [~] unmapped columns (add to cat_* or the extractor to capture): "
            + "; ".join(f"{t}: {', '.join(cols)}" for t, cols in all_dropped.items()))

    res["ok"] = bool(res["loaded"]) and not [
        e for e in res["errors"] if not str(e).startswith("header capture:")]

    if res["loaded"] and inventory_id is not None:
        # Streamlit-free readiness stamp (inlined so worker_core / pool children
        # can call capture_document without importing page_workbench).
        try:
            from sqlalchemy import text as _t
            with engine.begin() as _c:
                _c.execute(_t(
                    "UPDATE file_catalog.GLOBAL_FILE_CATALOG "
                    "SET CATALOG_READINESS = 'CATALOGED', ROW_CHANGED_DATE = GETUTCDATE() "
                    "WHERE INVENTORY_ID = :inv"), {"inv": inventory_id})
        except Exception:
            pass
    elif not res["errors"]:
        res["note"] = res["note"] or "no_rows"

    return res
