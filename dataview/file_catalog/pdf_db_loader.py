"""
modules/pdf_db_loader.py
==========================
Capture loaders for PDF-extracted document types.

Each loader follows the same contract:
    load_<type>(engine, dialect, well_info, rows, source, row_quality) -> dict
    returns {"ok": bool, "loaded": int, "errors": [...], "ids": {...}}

Loaders no longer write dataview.dv_* directly. They build column-keyed row
dicts and hand them to catalog_capture.capture(), which inserts into the
file_catalog.cat_* mirror tables (no FKs, all-nullable). A dv_well header is
NOT required at capture time — rows are keyed by the document UWI and promoted
into dv_* later by promote_catalog once a header exists.

Capture targets (mirror scope):
  Formation Tops  -> cat_well_formation_top      (one flat row per pick)
  Core Data       -> cat_well_core + cat_well_core_sample
  Casing/Cement   -> cat_well_completion
  Scout / IP      -> cat_prod_entity + cat_prod_volume  (tall: per fluid)
  Well Test / DST -> not in mirror scope (no cat_well_test table)
  RFT / MDT       -> not in mirror scope

well_info carries: uwi, well_name, operator, inventory_id, source_path.
"""
from __future__ import annotations
import re
import uuid
from datetime import datetime

# Resilient import: works whether catalog_capture lands in modules/ or root.
try:
    from dataview.file_catalog.catalog_capture import capture
except ImportError:
    from dataview.file_catalog.catalog_capture import capture


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now(dialect: str) -> str:
    return {"oracle": "SYSTIMESTAMP",
            "snowflake": "CURRENT_TIMESTAMP()"}.get(dialect, "GETUTCDATE()")

def _guid(dialect: str) -> str:
    return {"oracle": "SYS_GUID()",
            "snowflake": "UUID_STRING()"}.get(dialect, "NEWID()")

def _ts() -> str:
    """Plain-value timestamp for capture (cat_* rows hold literal values)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _uid() -> str:
    return uuid.uuid4().hex[:40].upper()

def _trunc(v, n=40):
    return str(v)[:n] if v is not None else None

def _safe_float(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None

def _safe_date(v):
    """Coerce an extracted date-ish value to 'YYYY-MM-DD' for a SQL date column,
    or None when it can't be parsed. Prevents a single unparseable scout-ticket
    date (e.g. '12/2021', 'UNKNOWN', '') from failing the whole cat_well insert
    with '22007 Conversion failed when converting date'. A bare YYYY-MM becomes
    the first of the month; a bare YYYY becomes Jan 1."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.upper() in ("UNKNOWN", "N/A", "NA", "NONE", "TBD", "."):
        return None
    # already ISO-ish (YYYY-MM-DD / YYYY/MM/DD)
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _valid_ymd(y, mo, d)
    # M/D/YYYY or MM-DD-YYYY
    m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})", s)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _valid_ymd(y, mo, d)
    # YYYY-MM  -> first of month
    m = re.match(r"^(\d{4})[-/](\d{1,2})$", s)
    if m:
        return _valid_ymd(int(m.group(1)), int(m.group(2)), 1)
    # bare YYYY -> Jan 1
    m = re.match(r"^(\d{4})$", s)
    if m:
        return _valid_ymd(int(m.group(1)), 1, 1)
    # last resort: let dateutil try, if available; else give up (NULL)
    try:
        from dateutil import parser as _dup
        return _dup.parse(s, default=datetime(2000, 1, 1)).strftime("%Y-%m-%d")
    except Exception:
        return None

def _valid_ymd(y, mo, d):
    """Return 'YYYY-MM-DD' if it's a real calendar date, else None."""
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return None

def _as_frac(v):
    """Normalise a porosity/saturation reading to a 0-1 fraction.
    Readings > 1 are assumed to be percent and divided by 100."""
    f = _safe_float(v)
    if f is None:
        return None
    return f / 100.0 if f > 1.0 else f

def _period7(v):
    """Coerce a date-ish value to a 'YYYY-MM' production period string."""
    if not v:
        return None
    s = str(v).strip()
    m = re.search(r"(\d{4})[-/](\d{1,2})", s)            # YYYY-MM / YYYY/M
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.search(r"\b(\d{1,2})[/-]\d{1,2}[/-](\d{4})\b", s)  # M/D/YYYY
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    return s[:7]

def _normalize_uwi(uwi: str) -> str:
    """Strip dashes, spaces and slashes for fuzzy matching."""
    return re.sub(r"[\-\s/]", "", str(uwi or "")).upper()

def _resolve_uwi(con, uwi: str, uwi_override: str = None) -> str | None:
    """Return the document's own UWI to key child records by.

    The catalog no longer depends on dv_well: documents are scanned BEFORE a
    well header exists, so child rows are keyed by the UWI extracted from the
    document (or an explicit override). `con` is accepted for signature
    stability but is not queried.
    """
    check = (uwi_override or uwi or "").strip()
    return check or None

def _bootstrap_well(con, dialect: str, well_info: dict,
                    source: str = "PDF_HEADER") -> str | None:
    """Deprecated passthrough — never writes dv_well; returns the doc UWI."""
    return _resolve_uwi(con, well_info.get("uwi"), None)

def _well_exists(con, uwi: str) -> bool:
    """Legacy helper — use _resolve_uwi for new code."""
    return _resolve_uwi(con, uwi) is not None

def _result(loaded=0, errors=None, **ids):
    return {"ok": not errors, "loaded": loaded,
            "errors": errors or [], "ids": ids}

def _prov(well_info: dict):
    """Pull (uwi, inventory_id, source_path) provenance from well_info."""
    return (_trunc(well_info.get("uwi"), 40),
            well_info.get("inventory_id"),
            well_info.get("source_path"))


# ══════════════════════════════════════════════════════════════════════════════
# Formation Tops -> cat_well_formation_top  (flat: one row per formation pick)
# ══════════════════════════════════════════════════════════════════════════════

def load_formation_tops(engine, dialect: str, well_info: dict,
                        rows: list[dict], source: str = "DATA_LOADER",
                        row_quality: str = "FINAL") -> dict:
    """rows: [{"FORMATION_NAME": str, "DEPTH_TOP_MD": float}, ...]"""
    uwi, inv, sp = _prov(well_info)
    if not uwi:
        return _result(errors=["No UWI provided"])
    if not rows:
        return _result(errors=["No formation tops to load"])

    ts        = _ts()
    well_name = _trunc(well_info.get("well_name") or uwi, 255)
    set_name  = _trunc(f"{well_name} Formation Tops", 255)

    out = []
    for obs_no, row in enumerate(rows, start=1):
        # Accept both key forms: scout tops emit FORMATION_NAME / DEPTH_TOP_MD;
        # extract_eowr's strat table emits FORMATION / TOP (FT MD) / TOP (FT TVD)
        # / THICKNESS (FT). Reading only the scout keys is why EOWR tops landed
        # with generic names and NULL depths.
        form = _trunc(row.get("FORMATION_NAME") or row.get("FORMATION")
                      or f"FORMATION_{obs_no}", 255)
        top = _safe_float(row.get("DEPTH_TOP_MD") or row.get("TOP (FT MD)")
                          or row.get("TOP_MD") or row.get("TOP MD"))
        base = _safe_float(row.get("DEPTH_BASE_MD") or row.get("BASE (FT MD)"))
        thick = _safe_float(row.get("THICKNESS (FT)") or row.get("THICKNESS"))
        if base is None and top is not None and thick is not None:
            base = top + thick
        out.append({
            "strat_unit_id":    _uid(),
            "interp_id":        _uid(),
            "strat_name_set":   set_name,
            "strat_unit_name":  form,
            "strat_unit_type":  "FORMATION",
            "top_depth":        top,
            "base_depth":       base,
            "depth_ouom":       "ft",
            "active_ind":       "Y",
            "row_created_by":   "DataWrangler",
            "row_created_date": ts,
        })

    try:
        n = capture(engine, "cat_well_formation_top", out,
                    uwi=uwi, inventory_id=inv, source_path=sp, source=source)
    except Exception as e:
        return _result(errors=[str(e)])
    return _result(loaded=n, errors=[])


# ══════════════════════════════════════════════════════════════════════════════
# Well Test / DST  -> NOT in mirror scope (no cat_well_test table)
# ══════════════════════════════════════════════════════════════════════════════

def load_well_test(engine, dialect: str, well_info: dict,
                   rows: list[dict], source: str = "DATA_LOADER",
                   row_quality: str = "FINAL",
                   test_type: str = "DST") -> dict:
    """Capture a well-test / DST document into cat_well_dst (same mirror the scout
    ticket uses), one row per test. Re-extracts header + analysis + flow periods
    from the source PDF so rates/pressures are populated. capture() keeps only the
    columns cat_well_dst actually has."""
    uwi, inv, sp = _prov(well_info)
    if not uwi:
        return _result(errors=["No UWI provided"])

    try:
        from dataview.file_catalog.pdf_survey_catalog import extract_well_test
    except ImportError:
        from dataview.file_catalog.pdf_survey_catalog import extract_well_test

    wt   = extract_well_test(sp) if sp else {}
    hdr  = wt.get("header") or {}
    ana  = wt.get("analysis") or {}
    flow = wt.get("flow_rows") or rows or []

    def _maxcol(*needles):
        best = None
        for r in flow:
            for k, v in r.items():
                if isinstance(v, (int, float)) and any(n in str(k).upper()
                                                       for n in needles):
                    best = v if best is None else max(best, v)
        return best

    ts = _ts()
    rec = {
        "dst_id":               _uid(),
        "dst_num":              1,
        "test_type":            _trunc(hdr.get("TEST_TYPE") or test_type or "DST", 40),
        "test_date":            _safe_date(hdr.get("TEST_DATE")),
        "test_result":          _trunc(hdr.get("ZONE"), 255),
        "max_oil_rate":         _maxcol("OIL"),
        "max_gas_rate":         _maxcol("GAS"),
        "max_water_rate":       _maxcol("WATER"),
        "rate_ouom":            "BBL/D",
        "max_shut_in_pressure": _safe_float(ana.get("STATIC_PRESSURE")),
        "pressure_ouom":        "PSI",
        "depth_ouom":           "ft",
        "active_ind":           "Y",
        "row_created_by":       "DataWrangler",
        "row_created_date":     ts,
    }
    try:
        n = capture(engine, "cat_well_dst", [rec], uwi=uwi,
                    inventory_id=inv, source_path=sp, source=source)

        # THE PERIODS ARE THE TEST. Previously only the maxima above were
        # kept, so a six-period multi-rate test survived as three numbers and
        # the shut-in/buildup periods — the ones the pressure analysis rests
        # on — vanished entirely. cat_well_dst_period is the child mirror
        # (added to MIRROR_TABLES and LINEAGE in the same change; a mirror in
        # neither list is invisible twice over).
        periods = []
        for fr in flow:
            seq = _safe_float(fr.get("PERIOD"))
            if seq is None:
                continue
            hrs = _safe_float(fr.get("HRS"))
            periods.append({
                "dst_id":         rec["dst_id"],
                "period_id":      _uid(),
                "period_seq":     int(seq),
                "period_type":    _trunc(fr.get("TYPE"), 40),
                "duration_min":   hrs * 60.0 if hrs is not None else None,
                "start_pressure": _safe_float(fr.get("FWHP (PSI)")),
                "end_pressure":   _safe_float(fr.get("FBHP (PSI)")),
                "pressure_ouom":  "PSI",
                # A shut-in period reports no rate at all; the source prints
                # an em-dash and _safe_float returns None. That NULL is the
                # true reading, not a failed parse.
                "avg_oil_rate":   _safe_float(fr.get("OIL (BBL/D)")),
                "avg_gas_rate":   _safe_float(fr.get("GAS (MCF/D)")),
                "avg_water_rate": _safe_float(fr.get("WATER (BBL/D)")),
                # One rate_ouom for three fluids, matching the parent row:
                # oil and water are BBL/D, gas is MCF/D. Stated in the remark
                # rather than left for a reader to assume.
                "rate_ouom":      "BBL/D",
                # choke_size stays NULL DELIBERATELY. The source prints a
                # fraction ("32/64") and extract_well_test returns 3264.0 —
                # the slash is lost upstream, so any value written here would
                # be invented. Fix the extractor, then populate this.
                "remark":         "gas rate in MCF/D; choke not parsed",
                "active_ind":     "Y",
                "row_created_by": "DataWrangler",
                "row_created_date": ts,
            })
        n_p = 0
        if periods:
            n_p = capture(engine, "cat_well_dst_period", periods, uwi=uwi,
                          inventory_id=inv, source_path=sp, source=source)
        return _result(loaded=n + n_p, dst_id=rec["dst_id"], periods=n_p)
    except Exception as e:
        return _result(errors=[
            f"cat_well_dst capture: {str(e).splitlines()[0].strip()[:180]}"])


# ══════════════════════════════════════════════════════════════════════════════
# RFT / MDT  -> NOT in mirror scope
# ══════════════════════════════════════════════════════════════════════════════

def load_rft(engine, dialect: str, well_info: dict,
             rows: list[dict], source: str = "DATA_LOADER",
             row_quality: str = "FINAL") -> dict:
    return _result(errors=[
        "RFT / MDT is not in the catalog mirror scope (no cat_well_test "
        "table). In-scope types: formation tops, core, completions, "
        "production. Add a mirror table to enable capture."])


# ══════════════════════════════════════════════════════════════════════════════
# Directional Survey -> delegated to the format-agnostic survey_loader
# ══════════════════════════════════════════════════════════════════════════════

def load_directional_survey(engine, dialect: str, well_info: dict,
                            rows: list[dict], source: str = "DATA_LOADER",
                            row_quality: str = "FINAL") -> dict:
    """PDF-path entry point — thin wrapper over the shared, format-agnostic
    survey_loader. Surveys also arrive as Word / Excel / WITSML, so the actual
    header+station mirror logic lives in modules/survey_loader.py and every
    format calls it. `rows` are station dicts (MD/INC/AZI/TVD/...); they are
    normalized to canonical keys, then loaded.
    """
    try:
        from dataview.file_catalog.survey_loader import (
            load_directional_survey as _load, normalize_stations)
    except ImportError:
        from dataview.file_catalog.survey_loader import (
            load_directional_survey as _load, normalize_stations)
    return _load(engine, well_info, normalize_stations(rows),
                 dialect=dialect, source=source)


# ══════════════════════════════════════════════════════════════════════════════
# Core Data -> cat_well_core (header) + cat_well_core_sample (per depth)
# ══════════════════════════════════════════════════════════════════════════════

def load_core(engine, dialect: str, well_info: dict,
              rows: list[dict], source: str = "DATA_LOADER",
              row_quality: str = "FINAL") -> dict:
    """rows: [{"DEPTH", "POROSITY", "PERMEABILITY", "SW"}, ...]"""
    uwi, inv, sp = _prov(well_info)
    if not uwi:
        return _result(errors=["No UWI provided"])
    if not rows:
        return _result(errors=["No core samples to load"])

    ts      = _ts()
    core_id = _uid()
    depths  = [d for d in (_safe_float(r.get("DEPTH")) for r in rows)
               if d is not None]
    top_d   = min(depths) if depths else None
    base_d  = max(depths) if depths else None

    try:
        # core header
        capture(engine, "cat_well_core", [{
            "core_id":          core_id,
            "core_num":         1,
            "core_type":        "CONVENTIONAL",
            "top_depth":        top_d,
            "base_depth":       base_d,
            "depth_ouom":       "ft",
            "active_ind":       "Y",
            "row_created_by":   "DataWrangler",
            "row_created_date": ts,
        }], uwi=uwi, inventory_id=inv, source_path=sp, source=source)

        # per-depth analyses
        samples = []
        for row in rows:
            samples.append({
                "core_id":                core_id,
                "sample_id":              _uid(),
                "sample_type":            "PLUG",
                "sample_depth":           _safe_float(row.get("DEPTH")),
                "depth_ouom":             "ft",
                "porosity_frac":          _as_frac(row.get("POROSITY")),
                "permeability_air_md":    _safe_float(row.get("PERMEABILITY")),
                "water_saturation_frac":  _as_frac(row.get("SW")),
                "active_ind":             "Y",
                "row_created_by":         "DataWrangler",
                "row_created_date":       ts,
            })
        n = capture(engine, "cat_well_core_sample", samples,
                    uwi=uwi, inventory_id=inv, source_path=sp, source=source)
    except Exception as e:
        return _result(errors=[str(e)])
    return _result(loaded=n, errors=[], core_id=core_id)


# ══════════════════════════════════════════════════════════════════════════════
# Casing & Cementing -> cat_well_completion  (one row per string)
# ══════════════════════════════════════════════════════════════════════════════

def load_casing(engine, dialect: str, well_info: dict,
                rows: list[dict], source: str = "DATA_LOADER",
                row_quality: str = "FINAL") -> dict:
    """rows: [{"STRING","OD (IN)","WEIGHT (PPF)","GRADE",
               "SHOE DEPTH (FT MD)", ...}, ...]"""
    uwi, inv, sp = _prov(well_info)
    if not uwi:
        return _result(errors=["No UWI provided"])
    if not rows:
        return _result(errors=["No casing strings to load"])

    ts  = _ts()
    out = []
    for row in rows:
        base = _safe_float(
            row.get("SHOE DEPTH (FT MD)") or row.get("SHOE DEPTH") or
            row.get("BASE_DEPTH"))
        remark = (
            f"{row.get('STRING','')} {row.get('OD (IN)','')}\" "
            f"{row.get('WEIGHT (PPF)','')} ppf {row.get('GRADE','')}"
        ).strip()
        out.append({
            "completion_id":    _uid(),
            "completion_type":  _trunc(row.get("STRING") or "CASING", 40),
            "base_depth":       base,
            "depth_ouom":       "ft",
            "remark":           _trunc(remark, 2000),
            "active_ind":       "Y",
            "row_created_by":   "DataWrangler",
            "row_created_date": ts,
        })

    try:
        n = capture(engine, "cat_well_completion", out,
                    uwi=uwi, inventory_id=inv, source_path=sp, source=source)
    except Exception as e:
        return _result(errors=[str(e)])
    return _result(loaded=n, errors=[])


# ══════════════════════════════════════════════════════════════════════════════
# Scout / IP -> cat_prod_entity (1 entity) + cat_prod_volume (tall, per fluid)
# ══════════════════════════════════════════════════════════════════════════════

def load_scout(engine, dialect: str, well_info: dict,
               rows: list[dict], source: str = "DATA_LOADER",
               row_quality: str = "FINAL") -> dict:
    """Capture ALL sections of a scout ticket into the cat_* mirrors.

    Re-extracts the full ticket from well_info['source_path'] so every section
    is captured — well header, formation tops, DST, completion + frac stages,
    core analysis, and IP/production — not just the IP table passed in `rows`.
    """
    uwi, inv, sp = _prov(well_info)
    if not uwi:
        return _result(errors=["No UWI provided"])

    try:
        from dataview.file_catalog.pdf_survey_catalog import extract_scout_ticket
    except ImportError:
        from dataview.file_catalog.pdf_survey_catalog import extract_scout_ticket

    sc = extract_scout_ticket(sp) if sp else {}
    header = sc.get("header") or {}
    tops   = sc.get("tops")    or []
    dst    = sc.get("dst")     or []
    frac   = sc.get("frac")    or []
    core   = sc.get("core")    or []
    ip     = sc.get("ip_rows") or rows or []

    ts = _ts()
    loaded = 0
    skipped = []          # sections whose cat_* table is missing or errored

    def _cap(table, recs):
        nonlocal loaded
        if not recs:
            return
        try:
            loaded += capture(engine, table, recs, uwi=uwi,
                              inventory_id=inv, source_path=sp, source=source)
        except Exception as e:
            # A missing mirror table (42000 'Invalid object name') or any other
            # per-table failure must not abort the remaining sections. Record
            # the skip so it is visible (never silently dropped) and carry on,
            # so the sections that DO have a home still land.
            msg = str(e).splitlines()[0].strip()[:180] if str(e) else type(e).__name__
            skipped.append((table, len(recs), msg))

    well_name = _trunc(header.get("WELL_NAME") or well_info.get("well_name")
                       or uwi, 255)

    try:
        # ── Well header → cat_well ────────────────────────────────────────
        if header:
            _cap("cat_well", [{
                "well_name":         well_name,
                "well_num":          _trunc(header.get("WELL_NUM"), 40),
                "well_type":         _trunc(header.get("WELL_TYPE"), 40),
                "well_status":       _trunc(header.get("WELL_STATUS"), 40),
                "province_state":    _trunc(header.get("STATE"), 40),
                "county":            _trunc(header.get("COUNTY"), 40),
                "surface_latitude":  _safe_float(header.get("LATITUDE")),
                "surface_longitude": _safe_float(header.get("LONGITUDE")),
                "ground_elevation":  _safe_float(header.get("GROUND_ELEV")),
                "kb_elevation":      _safe_float(header.get("KB_ELEV")),
                "spud_date":         _safe_date(header.get("SPUD_DATE")),
                "completion_date":   _safe_date(header.get("COMPLETION_DATE")),
                "final_td":          _safe_float(header.get("TOTAL_DEPTH")),
                "api_num":           _trunc(header.get("API"), 40),
                "lease_name":        _trunc(header.get("LEASE"), 255),
                "operator_name":     _trunc(header.get("OPERATOR"), 255),
                "field_name":        _trunc(header.get("FIELD"), 255),
                "active_ind":        "Y",
                "row_created_by":    "DataWrangler",
                "row_created_date":  ts,
            }])

        # ── Formation tops → cat_well_formation_top ───────────────────────
        if tops:
            set_name = _trunc(f"{well_name} Formation Tops", 255)
            _cap("cat_well_formation_top", [{
                "strat_unit_id":    _uid(),
                "interp_id":        _uid(),
                "strat_name_set":   set_name,
                "strat_unit_name":  _trunc(t.get("FORMATION_NAME"), 255),
                "strat_unit_type":  "FORMATION",
                "top_depth":        _safe_float(t.get("DEPTH_TOP_MD")),
                "base_depth":       _safe_float(t.get("DEPTH_BASE_MD")),
                "depth_ouom":       "ft",
                "active_ind":       "Y",
                "row_created_by":   "DataWrangler",
                "row_created_date": ts,
            } for t in tops])

        # ── DST → cat_well_dst ────────────────────────────────────────────
        if dst:
            _cap("cat_well_dst", [{
                "dst_id":               _uid(),
                "dst_num":              i + 1,
                "test_type":            _trunc(d.get("TEST_TYPE") or "DST", 40),
                "test_date":            _safe_date(d.get("TEST_DATE")),
                "top_depth":            _safe_float(d.get("TOP")),
                "base_depth":           _safe_float(d.get("BASE")),
                "depth_ouom":           "ft",
                "test_result":          _trunc(d.get("RESULT"), 255),
                "max_oil_rate":         _safe_float(d.get("OIL_RATE")),
                "max_gas_rate":         _safe_float(d.get("GAS_RATE")),
                "max_water_rate":       _safe_float(d.get("WATER_RATE")),
                "rate_ouom":            "BBL/D",
                "max_shut_in_pressure": _safe_float(d.get("SHUT_IN_PRESS")),
                "pressure_ouom":        "PSI",
                "api_gravity":          _safe_float(d.get("API_GRAVITY")),
                "active_ind":           "Y",
                "row_created_by":       "DataWrangler",
                "row_created_date":     ts,
            } for i, d in enumerate(dst)])

        # ── Completion + frac stages ──────────────────────────────────────
        comp_id = None
        if frac or header.get("COMPLETION_DATE"):
            comp_id = _uid()
            _cap("cat_well_completion", [{
                "completion_id":     comp_id,
                "completion_type":   "HYDRAULIC_FRACTURE" if frac else "COMPLETION",
                "completion_date":   _safe_date(header.get("COMPLETION_DATE")),
                "completion_status": _trunc(header.get("WELL_STATUS"), 40),
                "depth_ouom":        "ft",
                "active_ind":        "Y",
                "row_created_by":    "DataWrangler",
                "row_created_date":  ts,
            }])
        if frac:
            # Column names differ across schema revisions; include both the
            # per-stage (stage_*) and summary (top_/fluid_volume) variants —
            # capture() keeps whichever actually exist on cat_well_stimulation.
            _cap("cat_well_stimulation", [{
                "completion_id":            comp_id,
                "stim_id":                  _uid(),
                "stim_type":                "HYDRAULIC_FRACTURE",
                "stim_date":                _safe_date(header.get("COMPLETION_DATE")),
                "stage_num":                _safe_float(s.get("STAGE")),
                "stage_count":              _safe_float(s.get("STAGE")),
                "top_depth":                _safe_float(s.get("TOP")),
                "base_depth":               _safe_float(s.get("BASE")),
                "stage_top_depth":          _safe_float(s.get("TOP")),
                "stage_base_depth":         _safe_float(s.get("BASE")),
                "depth_ouom":               "ft",
                "fluid_volume":             _safe_float(s.get("FLUID_BBL")),
                "fluid_volume_bbl":         _safe_float(s.get("FLUID_BBL")),
                "fluid_volume_ouom":        "BBL",
                "proppant_mass":            _safe_float(s.get("PROPPANT_LBS")),
                "proppant_mass_lbs":        _safe_float(s.get("PROPPANT_LBS")),
                "proppant_mass_ouom":       "LB",
                "isip":                     _safe_float(s.get("ISIP")),
                "isip_psi":                 _safe_float(s.get("ISIP")),
                "max_treating_pressure":    _safe_float(s.get("MAX_PRESS")),
                "max_treating_pressure_psi":_safe_float(s.get("MAX_PRESS")),
                "pressure_ouom":            "PSI",
                "active_ind":               "Y",
                "row_created_by":           "DataWrangler",
                "row_created_date":         ts,
            } for s in frac])

        # ── Core → cat_well_core + cat_well_core_sample ───────────────────
        if core:
            core_id = _uid()
            depths  = [d for d in (_safe_float(c.get("DEPTH")) for c in core)
                       if d is not None]
            _cap("cat_well_core", [{
                "core_id":          core_id,
                "core_num":         1,
                "core_type":        "CONVENTIONAL",
                "top_depth":        min(depths) if depths else None,
                "base_depth":       max(depths) if depths else None,
                "depth_ouom":       "ft",
                "active_ind":       "Y",
                "row_created_by":   "DataWrangler",
                "row_created_date": ts,
            }])
            _cap("cat_well_core_sample", [{
                "core_id":               core_id,
                "sample_id":             _uid(),
                "sample_type":           "PLUG",
                "sample_depth":          _safe_float(c.get("DEPTH")),
                "depth_ouom":            "ft",
                "porosity_frac":         _as_frac(c.get("POROSITY")),
                "permeability_air_md":   _safe_float(c.get("PERMEABILITY")),
                "water_saturation_frac": _as_frac(c.get("SW")),
                "active_ind":            "Y",
                "row_created_by":        "DataWrangler",
                "row_created_date":      ts,
            } for c in core])

        # ── IP / production → cat_prod_entity + cat_prod_volume ───────────
        if ip:
            ent_id = _uid()
            _cap("cat_prod_entity", [{
                "prod_entity_id":   ent_id,
                "prod_entity_type": "WELL",
                "prod_entity_name": well_name,
                "primary_fluid":    "OIL",
                "active_ind":       "Y",
                "row_created_by":   "DataWrangler",
                "row_created_date": ts,
            }])
            fluids = (("OIL", "OIL_BOPD", "BBL"),
                      ("GAS", "GAS_MCFD", "MCF"),
                      ("WATER", "WATER_BWPD", "BBL"))
            vols = []
            for r in ip:
                period = _period7(r.get("DATE"))
                # period_date is NOT NULL in dv_prod_volume — skip rows whose
                # date did not parse (a None here is what breaks promote).
                if not period or len(str(period)) < 7:
                    continue
                for fluid, key, uom in fluids:
                    rate = _safe_float(r.get(key))
                    if rate is None:
                        continue
                    vols.append({
                        "prod_entity_id":   ent_id,
                        "period_date":      period,
                        "fluid_type":       fluid,
                        "volume":           rate,
                        "volume_ouom":      uom,
                        "days_on_prod":     1,
                        "avg_daily_rate":   rate,
                        "rate_ouom":        f"{uom}/D",
                        "active_ind":       "Y",
                        "row_created_by":   "DataWrangler",
                        "row_created_date": ts,
                    })
            if vols:
                _seen = {}
                for _vr in vols:
                    _seen[(_vr['prod_entity_id'], _vr['period_date'],
                           _vr['fluid_type'])] = _vr
                vols = list(_seen.values())
            _cap("cat_prod_volume", vols)

    except Exception as e:
        return _result(errors=[str(e)], loaded=loaded)

    errs = [f"{t}: {n} row(s) not captured ({msg})" for t, n, msg in skipped]
    return _result(loaded=loaded, errors=errs,
                   sections={"header": 1 if header else 0, "tops": len(tops),
                             "dst": len(dst), "frac": len(frac),
                             "core": len(core), "ip": len(ip)},
                   skipped=[{"table": t, "rows": n, "error": m}
                            for t, n, m in skipped])
