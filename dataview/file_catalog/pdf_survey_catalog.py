"""
modules/pdf_survey_catalog.py
=============================
Directional survey PDF extraction, classification and PPDM loading.

Supports:
  - Text-based PDFs (pdfplumber)
  - Three common report layouts: Landmark, Baker Hughes, Simple
  - Auto-detects column order regardless of layout
  - Loads to dbo.WELL_DIR_SURVEY + dbo.WELL_DIR_SRVY_STATION

Pipeline:
  1. scan_directory()       → find all PDFs
  2. classify_pdf()         → detect if it's a survey + extract well info
  3. extract_stations()     → parse the station table
  4. validate_stations()    → check continuity, flag anomalies
  5. load_to_ppdm()         → insert into PPDM tables
"""
from __future__ import annotations
import re, uuid
from pathlib import Path
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
# PDF parse cache
# ══════════════════════════════════════════════════════════════════════════════
# Every classifier/extractor below used to call pdfplumber.open(path) itself, so
# a single PDF was opened and its text re-extracted 4-5x per file (classify_pdf,
# extended_classify_pdf x2, then the type extractor). pdfplumber.open +
# extract_text is the dominant cost of the capture stage. This cache opens each
# file ONCE, pulls per-page text and per-page tables, and hands them back to
# every caller. Extraction LOGIC is unchanged — only the source of the text and
# tables moves from a fresh open to the cache.
#
# Cache scope: keyed by (path, mtime, size) so a changed file re-parses. The
# grid/word/char scout path (_scout_grid_rows, _scout_has_text_layer) is NOT
# cached — it needs live page objects for word coordinates — so it keeps opening
# its own PDF. That path is the minority of files; the text path is the hot one.

_pdf_cache: dict = {}
_PDF_CACHE_MAX = 64          # cap entries so a huge crawl can't balloon memory
import threading as _threading
_pdf_cache_lock = _threading.Lock()


def _pdf_cache_key(path: str):
    import os
    try:
        stt = os.stat(path)
        return (str(path), int(stt.st_mtime), int(stt.st_size))
    except OSError:
        return (str(path), 0, 0)


def get_pdf(path: str) -> dict:
    """Open `path` once and return a cached dict:
        {page_count, pages:[text,...], tables:[[table,...] per page], text_all,
         text_first, text_first3, error}
    Repeated calls for the same file return the cached parse. Safe to call from
    any classifier/extractor that only needs text or tables."""
    key = _pdf_cache_key(path)
    with _pdf_cache_lock:
        hit = _pdf_cache.get(key)
    if hit is not None:
        return hit

    import pdfplumber
    rec = {"page_count": 0, "pages": [], "tables": [],
           "text_all": "", "text_first": "", "text_first3": "", "error": None}
    try:
        with pdfplumber.open(path) as pdf:
            rec["page_count"] = len(pdf.pages)
            for p in pdf.pages:
                rec["pages"].append(p.extract_text() or "")
                try:
                    rec["tables"].append(p.extract_tables() or [])
                except Exception:
                    rec["tables"].append([])
        rec["text_first"]  = rec["pages"][0] if rec["pages"] else ""
        rec["text_first3"] = " ".join(rec["pages"][:3])
        rec["text_all"]    = "\n".join(rec["pages"])

        # ── Annotation harvest ────────────────────────────────────────────
        # Some PDFs carry the UWI only in a FreeText/Widget ANNOTATION (e.g. a
        # value typed in with a PDF editor), never in the content stream, so
        # extract_text() above misses it. Pull annotation + AcroForm text and
        # append it — in a LABELLED form ("UWI: <digits>") so the existing
        # label-anchored INFO_PATTERNS["uwi"] catches it without a loose
        # bare-digit rule that could mis-grab body numbers. Best-effort; any
        # failure leaves text_all exactly as it was.
        try:
            import re as _re
            from pypdf import PdfReader as _PdfReader
            _atxt = []
            _r = _PdfReader(path)
            for _pg in _r.pages:
                for _a in (_pg.get("/Annots") or []):
                    try:
                        _o = _a.get_object()
                    except Exception:
                        continue
                    for _k in ("/Contents", "/V", "/RC"):
                        _val = _o.get(_k)
                        if not _val:
                            continue
                        _t = str(_val)
                        _t = _re.sub(r"<[^>]+>", " ", _t)   # strip XHTML (/RC rich text)
                        if _t.strip():
                            _atxt.append(_t.strip())
            # AcroForm field values
            try:
                _flds = _r.get_fields() or {}
                for _fv in _flds.values():
                    _v = getattr(_fv, "value", None) or (_fv.get("/V") if hasattr(_fv, "get") else None)
                    if _v:
                        _atxt.append(str(_v).strip())
            except Exception:
                pass
            if _atxt:
                _joined = " ".join(_atxt)
                rec["had_annotations"] = True
                # If a bare 14-digit UWI (or dashed API) is present in the
                # annotation, surface it label-anchored so classify_pdf resolves it.
                _m = _re.search(r"\b(\d{14})\b", _joined) or \
                     _re.search(r"\b(\d{2}-\d{3}-\d{5}(?:-\d{2}){0,2})\b", _joined)
                if _m:
                    rec["annotation_uwi"] = _re.sub(r"[^0-9]", "", _m.group(1))
                    _joined = f"UWI: {_m.group(1)} " + _joined
                rec["text_all"]    = rec["text_all"]   + "\n" + _joined
                rec["text_first"]  = rec["text_first"] + "\n" + _joined
                rec["text_first3"] = rec["text_first3"]+ "\n" + _joined
        except Exception:
            pass
    except Exception as e:                       # noqa: BLE001
        rec["error"] = str(e)

    # simple FIFO cap — drop an arbitrary old entry when full
    with _pdf_cache_lock:
        if len(_pdf_cache) >= _PDF_CACHE_MAX and key not in _pdf_cache:
            try:
                _pdf_cache.pop(next(iter(_pdf_cache)))
            except StopIteration:
                pass
        _pdf_cache[key] = rec
    return rec


def clear_pdf_cache() -> None:
    """Drop all cached PDF parses (e.g. between large runs)."""
    _pdf_cache.clear()


# ── Report type constants ─────────────────────────────────────────────────────
RT_DIRECTIONAL = "DIRECTIONAL_SURVEY"
RT_MUDLOG      = "MUD_LOG"
RT_FORMATION   = "FORMATION_TOPS"
RT_COMPLETION  = "COMPLETION_REPORT"
RT_UNKNOWN     = "UNKNOWN"

# ── Column name synonyms → canonical name ────────────────────────────────────
COL_SYNONYMS = {
    "MD": [
        "md", "measured depth", "meas depth", "depth ft", "depth",
        "md (ft)", "meas dep", "measdepth", "md_ft", "md ft",
    ],
    "INC": [
        "inc", "incl", "inclination", "incl deg", "inc (deg)",
        "inc (°)", "inclination (deg)", "angle", "inc deg",
    ],
    "AZI": [
        "azi", "azim", "azimuth", "azim deg", "azi (deg)",
        "azimuth (deg)", "azimuth (tn)", "azi (°)", "azim (°)", "azim deg",
    ],
    "TVD": [
        "tvd", "true vert dep", "true vertical depth", "tvd ft",
        "tvd (ft)", "tv depth", "vert depth",
    ],
    "NS": [
        "ns", "n/s", "northing", "n/s (ft)", "ns (ft)", "north/south",
        "ns ft", "n-s", "n/s ft",
    ],
    "EW": [
        "ew", "e/w", "easting", "e/w (ft)", "ew (ft)", "east/west",
        "ew ft", "e-w", "e/w ft",
    ],
    "DLS": [
        "dls", "dog leg", "dogleg", "dog leg sev", "dl sev",
        "dls (deg/100)", "dls deg/100", "dogleg severity",
        "dog leg severity", "dl", "d.l.s",
    ],
    "VSEC": [
        "vsec", "v-sec", "closure dist", "closure distance",
        "vertical section", "v sec", "vsec ft",
    ],
}

# Reverse lookup: synonym → canonical
_SYN_MAP = {}
for canon, syns in COL_SYNONYMS.items():
    for s in syns:
        _SYN_MAP[s.lower().strip()] = canon


def _match_col(name: str) -> Optional[str]:
    """Match a column header string to a canonical column name."""
    cleaned = re.sub(r'[\(\)°\n]', ' ', name).lower().strip()
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # Direct match
    if cleaned in _SYN_MAP:
        return _SYN_MAP[cleaned]
    # Partial match
    for syn, canon in _SYN_MAP.items():
        if syn in cleaned or cleaned in syn:
            return canon
    return None


def _survey_col_order(line: str) -> list:
    """Ordered canonical columns for a SPACE-ALIGNED survey header, matched by
    position with greedy longest-first, non-overlapping synonyms. Fixes 'Baker'/
    MWD-style headers (Meas Depth / True Vert Dep / Dog Leg Sev / Closure Dist)
    that break word-by-word tokenizing — the caller maps data columns positionally
    against the returned order."""
    low = re.sub(r'\s+', ' ', str(line).lower())
    hits = []                       # (start, length, canon)
    for syn, canon in _SYN_MAP.items():
        if len(syn) < 2:
            continue
        start = 0
        while True:
            p = low.find(syn, start)
            if p < 0:
                break
            hits.append((p, len(syn), canon))
            start = p + 1
    hits.sort(key=lambda h: (h[0], -h[1]))   # by position, longer synonym first
    order, taken = [], []
    for pos, ln, canon in hits:
        if any(pos < e and pos + ln > s for s, e in taken):
            continue                # overlaps an already-claimed span
        taken.append((pos, pos + ln))
        order.append(canon)
    return order


# ── Well info extraction patterns ─────────────────────────────────────────────
INFO_PATTERNS = {
    "uwi": [
        r'(?:UWI|API)(?:\s*(?:NUM(?:BER)?|NO|#|/\s*UWI|/\s*API))?\s*[:#]?\s+([0-9\-]{10,20})',
        r'([0-9]{2}-[0-9]{3}-[0-9]{5}-[0-9]{2}-[0-9]{2})',
    ],
    "well_name": [
        r'(?:WELL\s+NAME|WELLNAME)[:\s]+([A-Z0-9 #\-/]+?)(?:\s+(?:API|UWI|FIELD|OPERATOR|STATE|COUNTY)\b|\n|$)',
        r'(?:WELL)[:\s]+([A-Z0-9 #\-/]+?)(?:\s+(?:API|UWI|FIELD|OPERATOR|STATE|COUNTY)\b|\n|$)',
    ],
    "operator": [
        r'(?:OPERATOR|COMPANY)[:\s]+([A-Za-z0-9 &.,]+?)(?:\n|$)',
    ],
    "field": [
        r'(?:FIELD)[:\s]+([A-Za-z0-9 \-]+?)(?:\n|$)',
    ],
    "state": [
        r'(?:STATE)[:\s]+([A-Z]{2,})',
        r'\b(TX|OK|NM|CO|WY|ND|MT|KS|LA|MS|AL|PA|WV|OH)\b',
    ],
    "contractor": [
        r'(?:CONTRACTOR|SERVICE\s+CO)[:\s]+([A-Za-z0-9 &.,]+?)(?:\n|$)',
    ],
    "survey_type": [
        r'(MWD|Magnetic MWD|Gyroscopic|Gyro|Magnetic|Accelerometer)',
    ],
    "total_depth": [
        r'(?:TOTAL DEPTH|TD|MAX DEPTH)[:\s]+([\d,]+)\s*(?:ft|m)',
        r'(?:TOTAL\s+DEPTH)[:\s]+([\d,]+)',
    ],
    "latitude": [
        r'(?:Surface\s+)?Lat(?:itude)?[:\s]+([+-]?\d{1,3}\.\d+)\s*[°]?\s*([NS])?',
    ],
    "longitude": [
        r'(?:Surface\s+)?Lon(?:g(?:itude)?)?[:\s]+([+-]?\d{1,3}\.\d+)\s*[°]?\s*([EW])?',
    ],
}


def _signed_coord(m):
    """From a lat/long regex match (decimal in group 1, optional N/S/E/W in
    group 2) return a signed float — S/W are negative. None if unparseable."""
    try:
        num = float(m.group(1))
    except (TypeError, ValueError):
        return None
    hemi = ""
    try:
        if m.re.groups >= 2 and m.group(2):
            hemi = m.group(2).upper()
    except Exception:
        pass
    return -abs(num) if hemi in ("S", "W") else num


# ══════════════════════════════════════════════════════════════════════════════
# 1. Scanner
# ══════════════════════════════════════════════════════════════════════════════

def scan_directory(root_path: str) -> list[dict]:
    import pdfplumber
    """Recursively find all PDF files."""
    root  = Path(root_path)
    files = []
    for fp in sorted(root.rglob('*.pdf')):
        files.append({
            "file_id":     uuid.uuid4().hex[:20].upper(),
            "file_path":   str(fp),
            "file_name":   fp.name,
            "file_size_kb":round(fp.stat().st_size/1024, 1),
            "page_count":  0,
            "report_type": RT_UNKNOWN,
            "status":      "PENDING",
        })
    return files


# ══════════════════════════════════════════════════════════════════════════════
# 2. Classifier — detect report type and extract well header
# ══════════════════════════════════════════════════════════════════════════════

def classify_pdf(file_path: str) -> dict:
    import pdfplumber
    """
    Open PDF, detect report type, extract well header information.
    Returns classification dict.
    """
    result = {
        "file_path":    file_path,
        "file_name":    Path(file_path).name,
        "report_type":  RT_UNKNOWN,
        "confidence":   0.0,
        "page_count":   0,
        "well_name":    None,
        "uwi":          None,
        "operator":     None,
        "field":        None,
        "state":        None,
        "contractor":   None,
        "survey_type":  "MWD",
        "total_depth":  None,
        "station_count":0,
        "error":        None,
    }

    try:
        _pc = get_pdf(file_path)
        if _pc.get("error") and _pc["page_count"] == 0:
            raise RuntimeError(_pc["error"])
        result["page_count"] = _pc["page_count"]

        # Extract text from first page
        text = _pc["text_first"]
        text_upper = text.upper()

        # ── Detect report type ────────────────────────────────────────────
        survey_keywords = [
            "DIRECTIONAL SURVEY", "WELLBORE SURVEY",
            "SURVEY REPORT", "MWD", "MEASURED DEPTH",
            "INCLINATION", "AZIMUTH", "TVD", "DOG LEG",
        ]
        # Also catch simple/plain format column headers
        simple_keywords = [
            "INCL DEG", "AZIM DEG", "DEPTH FT",
            "INC (DEG)", "AZI (DEG)", "TVD FT",
            "DOGLEG", "DOG LEG SEV", "DLS",
            "MEASURED DEPTH", "TRUE VERT",
        ]
        score_full   = sum(1 for kw in survey_keywords if kw in text_upper)
        score_simple = sum(1 for kw in simple_keywords  if kw in text_upper)
        score        = score_full

        if score >= 3 or (score >= 1 and score_simple >= 2) or score_simple >= 3:
            result["report_type"] = RT_DIRECTIONAL
            combined = score_full + score_simple
            total    = len(survey_keywords) + len(simple_keywords)
            result["confidence"]  = min(1.0, combined / (total * 0.4))
        elif "MUD LOG" in text_upper or "MUDLOG" in text_upper:
            result["report_type"] = RT_MUDLOG
            result["confidence"]  = 0.8
        elif "FORMATION" in text_upper and "TOPS" in text_upper:
            result["report_type"] = RT_FORMATION
            result["confidence"]  = 0.7
        elif ("PRODUCTION REPORT" in text_upper
              or "MONTHLY PRODUCTION" in text_upper):
            result["report_type"] = RT_SCOUT
            result["confidence"]  = 0.7
        elif "COMPLETION" in text_upper:
            result["report_type"] = RT_COMPLETION
            result["confidence"]  = 0.6

        # ── Extract well info ─────────────────────────────────────────────
        for field, patterns in INFO_PATTERNS.items():
            for pat in patterns:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    if field in ("latitude", "longitude"):
                        result[field] = _signed_coord(m)
                    else:
                        val = m.group(1).strip()
                        if field == "total_depth":
                            val = val.replace(',','')
                        result[field] = val
                    break

        # Count stations if it's a survey
        if result["report_type"] == RT_DIRECTIONAL:
            all_text = _pc["text_all"]
            # Count rows that look like survey stations
            # (line starting with a number)
            station_rows = re.findall(
                r'^\s*(\d[\d,]*\.?\d*)\s+\d',
                all_text, re.MULTILINE
            )
            result["station_count"] = len(station_rows)

    except Exception as e:
        result["error"] = str(e)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 3. Station extractor
# ══════════════════════════════════════════════════════════════════════════════

def extract_stations(file_path: str) -> dict:
    import pdfplumber
    """
    Extract survey station data from a directional survey PDF.
    Returns {"stations": [...], "columns_found": [...], "error": None}
    """
    result = {
        "stations":      [],
        "columns_found": [],
        "col_map":       {},
        "error":         None,
    }

    try:
        _pc = get_pdf(file_path)
        if True:
            all_rows = []
            header_found = False
            col_map = {}

            for _pi in range(_pc["page_count"]):
                tables = _pc["tables"][_pi] if _pi < len(_pc["tables"]) else []
                _before = len(all_rows)
                # ── Table branch ──
                for table in (tables or []):
                    if not table:
                        continue
                    # Find header row
                    for i, row in enumerate(table):
                        if row and not header_found:
                            # Check if this looks like a header
                            matches = sum(
                                1 for cell in row
                                if cell and _match_col(str(cell))
                            )
                            if matches >= 3:
                                # Map column indices to canonical names
                                col_map = {}
                                for j, cell in enumerate(row):
                                    if cell:
                                        canon = _match_col(str(cell))
                                        if canon:
                                            col_map[j] = canon
                                header_found = True
                                result["col_map"]       = {v:k for k,v in col_map.items()}
                                result["columns_found"] = list(col_map.values())
                                continue

                        if header_found and row:
                            # Try to parse as a data row
                            station = _parse_station_row(row, col_map)
                            if station:
                                all_rows.append(station)

                # ── Text branch — pdfplumber often mis-detects space-aligned
                #    survey tables (Baker / MWD text exports) as junk single-cell
                #    tables, so the table branch above finds nothing. Fall back to
                #    text parsing whenever this page produced no station rows. ──
                if len(all_rows) == _before:
                    text = _pc["pages"][_pi] if _pi < len(_pc["pages"]) else ""
                    lines = text.split('\n')
                    for line in lines:
                        if not header_found:
                            # Position-based multi-word header parse (handles
                            # "Meas Depth  True Vert Dep  Dog Leg Sev ...").
                            order = _survey_col_order(line)
                            _cm_set = set(order)
                            # Genuine survey header: MD + at least one of INC/AZI/TVD.
                            if (len(order) >= 3 and "MD" in _cm_set
                                    and _cm_set & {"INC", "AZI", "TVD"}):
                                col_map = {i: c for i, c in enumerate(order)}
                                header_found = True
                                result["columns_found"] = list(col_map.values())
                                continue
                        else:
                            # Clean line before splitting:
                            #  - remove commas from numbers: 1,200.00 → 1200.00
                            #  - remove leading + from signed values: +0.00 → 0.00
                            clean = re.sub(r'(?<=\d),(?=\d)', '', line)
                            clean = re.sub(r'(?<![\w])\+(?=[\d])', '', clean)
                            toks = clean.strip().split()
                            station = _parse_token_row(toks, col_map)
                            if station:
                                all_rows.append(station)

            result["stations"] = all_rows

    except Exception as e:
        result["error"] = str(e)

    return result


def _parse_station_row(row: list, col_map: dict) -> Optional[dict]:
    """Parse one table row into a station dict."""
    st = {}
    for j, canon in col_map.items():
        if j < len(row) and row[j] is not None:
            val = str(row[j]).replace(',','').strip()
            try:
                st[canon] = float(val)
            except ValueError:
                pass
    # Must have at least MD and one of INC/AZI
    if "MD" in st and ("INC" in st or "AZI" in st):
        return st
    return None


def _parse_token_row(toks: list, col_map: dict) -> Optional[dict]:
    """Parse whitespace-split tokens into a station dict."""
    if not toks:
        return None
    # First token must be a number (MD)
    try:
        float(toks[0].replace(',',''))
    except ValueError:
        return None

    st = {}
    numeric_toks = []
    for tok in toks:
        try:
            numeric_toks.append(float(tok.replace(',','').replace('+','')))
        except ValueError:
            pass

    # Map by position
    for i, (idx, canon) in enumerate(sorted(col_map.items())):
        if i < len(numeric_toks):
            st[canon] = numeric_toks[i]

    if "MD" in st and ("INC" in st or "AZI" in st):
        return st
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 4. Validation
# ══════════════════════════════════════════════════════════════════════════════

def validate_stations(stations: list[dict]) -> dict:
    """
    Validate station data for common issues.
    Returns {"valid": bool, "warnings": [...], "errors": [...]}
    """
    warnings = []
    errors   = []

    if not stations:
        errors.append("No stations extracted")
        return {"valid": False, "warnings": warnings, "errors": errors}

    mds = [s.get("MD",0) for s in stations]

    # Check MD is monotonically increasing
    for i in range(1, len(mds)):
        if mds[i] <= mds[i-1]:
            errors.append(
                f"MD not increasing at station {i}: "
                f"{mds[i-1]} → {mds[i]}"
            )

    # Check inclination range
    for i, s in enumerate(stations):
        inc = s.get("INC", 0)
        azi = s.get("AZI", 0)
        if inc < 0 or inc > 180:
            errors.append(f"Station {i}: INC={inc} out of range (0-180°)")
        if azi < 0 or azi > 360:
            errors.append(f"Station {i}: AZI={azi} out of range (0-360°)")

    # Check DLS for extreme values
    for i, s in enumerate(stations):
        dls = s.get("DLS", 0)
        if dls > 15:
            warnings.append(
                f"Station {i} MD={s.get('MD','?')}: "
                f"High DLS={dls}°/100ft"
            )

    # Check for gaps
    if len(mds) > 1:
        steps = [mds[i]-mds[i-1] for i in range(1,len(mds))]
        avg_step = sum(steps)/len(steps)
        for i, step in enumerate(steps):
            if step > avg_step * 3:
                warnings.append(
                    f"Large gap between stations {i} and {i+1}: "
                    f"{step:.0f} ft"
                )

    return {
        "valid":    len(errors) == 0,
        "warnings": warnings,
        "errors":   errors,
        "station_count": len(stations),
        "md_range": f"{min(mds):.0f} – {max(mds):.0f} ft",
    }


# ══════════════════════════════════════════════════════════════════════════════
# 5. PPDM Loader
# ══════════════════════════════════════════════════════════════════════════════

def load_to_ppdm(well_info: dict, stations: list[dict],
                  engine, dialect: str = "mssql",
                  source: str = "PDF_SURVEY",
                  dry_run: bool = False,
                  inventory_id: str = None) -> dict:
    """
    Capture the directional survey into the file_catalog cat_* mirrors
    (cat_well_dir_srvy_hdr + cat_well_dir_srvy_sta) so it promotes to
    dv_well_dir_srvy_* like every other type. Column names are discovered at
    runtime and station fields are alias-matched, so this works regardless of
    the exact mirror schema. (Was: legacy dbo.WELL_DIR_SURVEY /
    dbo.WELL_DIR_SRVY_STATION, which don't exist in the catalog schema.)
    """
    from sqlalchemy import text

    result = {"loaded": 0, "skipped": 0, "survey_id": None, "errors": []}
    if not stations:
        result["errors"].append("No stations to load")
        return result

    uwi = (well_info.get("uwi") or "").strip()
    if not uwi:
        result["errors"].append("No UWI — cannot load without a well key")
        return result

    survey_id = uuid.uuid4().hex[:40].upper()
    result["survey_id"] = survey_id
    if dry_run:
        result["loaded"] = len(stations)
        return result

    def _cols(con, sch, tbl):
        return {r[0].upper(): r[0] for r in con.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t"), {"s": sch, "t": tbl})}

    def _pick(colmap, *cands):
        for c in cands:
            if c.upper() in colmap:
                return colmap[c.upper()]
        return None

    def _insert(con, sch, tbl, colmap, row):
        clean = {k: v for k, v in row.items() if k}
        if not clean:
            return
        cols = ", ".join(f"[{c}]" for c in clean)
        vals = ", ".join(f":{c}" for c in clean)
        extra = ""
        for ac in ("ROW_CREATED_DATE", "ROW_CHANGED_DATE"):
            if ac in colmap and colmap[ac] not in clean:
                cols += f", [{colmap[ac]}]"
                extra += ", GETUTCDATE()"
        con.execute(text(f"INSERT INTO {sch}.{tbl} ({cols}) VALUES ({vals}{extra})"),
                    clean)

    STA_ALIAS = {
        "MD":  ("STATION_MD", "MD", "MEASURED_DEPTH"),
        "INC": ("INCLINATION", "INC"),
        "AZI": ("AZIMUTH", "AZI"),
        "TVD": ("STATION_TVD", "TVD"),
        "NS":  ("NS_DEVIATION", "NS", "NORTH_SOUTH"),
        "EW":  ("EW_DEVIATION", "EW", "EAST_WEST"),
        "DLS": ("DOGLEG_SEVERITY", "DLS"),
    }
    try:
        with engine.begin() as con:
            hc = _cols(con, "file_catalog", "cat_well_dir_srvy_hdr")
            sc = _cols(con, "file_catalog", "cat_well_dir_srvy_sta")
            if not hc or not sc:
                result["errors"].append(
                    "cat_well_dir_srvy_hdr/sta mirror table(s) missing")
                return result

            hrow = {}
            for col, val in (
                (_pick(hc, "SURVEY_ID", "WELL_DIR_SURVEY_ID"), survey_id),
                (_pick(hc, "UWI"), uwi[:40]),
                (_pick(hc, "SURVEY_TYPE"),
                 (well_info.get("survey_type") or "MWD")[:40]),
                (_pick(hc, "SOURCE"), source[:40]),
                (_pick(hc, "INVENTORY_ID"), inventory_id),
            ):
                if col and val is not None:
                    hrow[col] = val
            _insert(con, "file_catalog", "cat_well_dir_srvy_hdr", hc, hrow)

            n = 0
            for i, stn in enumerate(stations):
                srow = {}
                for col, val in (
                    (_pick(sc, "SURVEY_ID", "WELL_DIR_SURVEY_ID"), survey_id),
                    (_pick(sc, "STATION_ID", "WELL_DIR_SRVY_STATION_ID"),
                     uuid.uuid4().hex[:40].upper()),
                    (_pick(sc, "UWI"), uwi[:40]),
                    (_pick(sc, "STATION_NUMBER", "STATION_NO", "SEQ_NO"), i + 1),
                    (_pick(sc, "SOURCE"), source[:40]),
                    (_pick(sc, "INVENTORY_ID"), inventory_id),
                ):
                    if col and val is not None:
                        srow[col] = val
                for logical, cands in STA_ALIAS.items():
                    col = _pick(sc, *cands)
                    if col:
                        srow[col] = stn.get(logical)
                if srow:
                    _insert(con, "file_catalog", "cat_well_dir_srvy_sta", sc, srow)
                    n += 1
            result["loaded"] = n

    except Exception as e:
        result["errors"].append(str(e))

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 6. Summary
# ══════════════════════════════════════════════════════════════════════════════

def summarize_scan(files: list[dict]) -> dict:
    by_type = {}
    for f in files:
        rt = f.get("report_type", RT_UNKNOWN)
        by_type[rt] = by_type.get(rt, 0) + 1
    return {
        "total_files":    len(files),
        "by_type":        by_type,
        "surveys":        by_type.get(RT_DIRECTIONAL, 0),
        "unknown":        by_type.get(RT_UNKNOWN, 0),
        "ready_to_load":  by_type.get(RT_DIRECTIONAL, 0),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Extended report type constants
# ══════════════════════════════════════════════════════════════════════════════
RT_RFT       = "RFT_MDT"
RT_SCOUT     = "SCOUT_TICKET"
RT_DDR       = "DAILY_DRILLING_REPORT"
RT_WELL_TEST = "WELL_TEST"
RT_PETRO     = "PETROPHYSICAL"
RT_EOWR      = "END_OF_WELL"
RT_CASING    = "CASING_CEMENTING"
RT_CORE      = "CORE_ANALYSIS"

# PPDM targets for each new type
EXTENDED_PPDM_TARGETS = {
    RT_RFT:       "WELL_TEST + WELL_TEST_RESULT",
    RT_SCOUT:     "WELL + WELL_VERSION",
    RT_DDR:       "WELL_ACTIVITY",
    RT_WELL_TEST: "WELL_TEST + WELL_TEST_RESULT",
    RT_PETRO:     "WELL_LOG_VERSION + WELL_INTERPRETATION",
    RT_EOWR:      "WELL + WELL_VERSION",
    RT_CASING:    "WELL_COMPLETION + WELL_COMPLETION_COMPONENT",
}

# Keyword sets for classification
_EXTENDED_KEYWORDS = {
    RT_RFT: [
        "repeat formation tester", "rft", "mdt", "formation pressure",
        "wireline pressure", "mobility", "fluid gradient", "free water level",
        "modular dynamic tester", "fwl", "owc", "goc", "pressure measurement",
    ],
    RT_SCOUT: [
        "scout ticket", "scout report", "initial production", "ip rate",
        "well scout", "completion information", "perforation", "proppant",
        "choke size", "flowing tubing pressure", "ftp",
    ],
    RT_DDR: [
        "daily drilling report", "ddr", "daily report", "24-hour",
        "drilling parameters", "weight on bit", "wob", "rop",
        "mud properties", "standpipe pressure", "next 24",
        "daily morning report", "operations summary",
    ],
    RT_WELL_TEST: [
        "well test report", "production test", "flow test", "multi-rate",
        "buildup test", "drawdown", "productivity index", "pi test",
        "skin factor", "reservoir pressure", "isochronal",
        "fwhp", "fbhp", "wellhead pressure", "bottomhole pressure",
    ],
    RT_PETRO: [
        "petrophysical", "petrophys", "log interpretation", "well log analysis",
        "porosity", "water saturation", "net pay", "vcl", "clay volume",
        "phie", "effective porosity", "archie", "resistivity",
        "neutron", "density", "gamma ray",
    ],
    RT_EOWR: [
        "end of well", "final well report", "well completion report",
        "elapsed days", "afe", "actual cost", "npt",
        "stratigraphic summary", "well summary", "total depth reached",
    ],
    RT_CASING: [
        "casing record", "cementing record", "cement job", "cbl",
        "cement bond", "centralizer", "float shoe", "displacement",
        "thickening time", "compressive strength", "woc",
        "top of cement", "toc", "slurry",
    ],
}


def extended_classify_pdf(file_path: str) -> dict:
    """
    Extended classifier — detects 7 additional petroleum PDF types
    beyond the base 5 in classify_pdf().

    Returns classification dict with keys:
      report_type, confidence, well_name, uwi, operator,
      page_count, error, + type-specific fields
    """
    import pdfplumber

    result = {
        "file_path":   file_path,
        "file_name":   Path(file_path).name,
        "report_type": RT_UNKNOWN,
        "confidence":  0.0,
        "page_count":  0,
        "well_name":   None,
        "uwi":         None,
        "operator":    None,
        "error":       None,
    }

    try:
        _pc = get_pdf(file_path)
        if _pc.get("error") and _pc["page_count"] == 0:
            raise RuntimeError(_pc["error"])
        result["page_count"] = _pc["page_count"]
        text = _pc["text_first3"].lower()

        best_type  = RT_UNKNOWN
        best_score = 0
        best_conf  = 0.0

        for rt, keywords in _EXTENDED_KEYWORDS.items():
            hits  = sum(1 for kw in keywords if kw in text)
            score = hits / len(keywords)
            if hits > best_score:
                best_score = hits
                best_type  = rt
                best_conf  = min(1.0, score * 3.0)

        if best_score >= 2:
            result["report_type"] = best_type
            result["confidence"]  = best_conf

        # Extract well info using same INFO_PATTERNS (page-0 text from cache)
        hdr_text = _pc["text_first"]
        for field, patterns in INFO_PATTERNS.items():
            for pat in patterns:
                m = re.search(pat, hdr_text, re.IGNORECASE)
                if m:
                    if field in ("latitude", "longitude"):
                        result[field] = _signed_coord(m)
                    else:
                        result[field] = m.group(1).strip()
                    break

        # Scout tickets are often a grid layout (label cell above value cell)
        # with a 'US'-prefixed GID, which the label:value INFO_PATTERNS can't
        # read — leaving result['uwi'] empty → 'extracted - no UWI'. Fall back
        # to the grid extractor's header, which reconstructs the grid and
        # strips the GID to a bare-14 UWI.
        if result.get("report_type") == RT_SCOUT and not _looks_uwi14(result.get("uwi")):
            try:
                sc = extract_scout_ticket(file_path)
                h = sc.get("header") or {}
                bare = h.get("UWI_BARE14") or (
                    re.sub(r"\D", "", h.get("API", ""))[:14] or None)
                if bare and len(bare) == 14:
                    result["uwi"] = bare
                # The label:value regex often grabs the document title
                # ("SCOUT TICKET") as the well name — prefer the grid header's
                # real well name when available.
                wn = result.get("well_name") or ""
                if h.get("WELL_NAME") and (not wn or "SCOUT" in wn.upper()
                                           or "TICKET" in wn.upper()):
                    result["well_name"] = h["WELL_NAME"]
                if not result.get("operator") and h.get("OPERATOR"):
                    result["operator"] = h["OPERATOR"]
            except Exception:
                pass

    except Exception as e:
        result["error"] = str(e)

    return result


def _looks_uwi14(v):
    """True if v is already a clean bare-14 UWI."""
    if not v:
        return False
    return len(re.sub(r"\D", "", str(v))) == 14 and not str(v).upper().startswith("US")


# ══════════════════════════════════════════════════════════════════════════════
# Extractors for new types
# ══════════════════════════════════════════════════════════════════════════════

def extract_rft_data(file_path: str) -> dict:
    """Extract RFT/MDT pressure measurements and fluid samples."""
    import pdfplumber
    rows   = []
    samples = []
    result = {"rows": rows, "samples": samples, "error": None}
    try:
        for _page_tables in get_pdf(file_path)["tables"]:
            for table in _page_tables:
                    if not table or len(table) < 2:
                        continue
                    hdrs = [str(c).strip().upper() for c in (table[0] or [])]
                    def _ci(keys):
                        return next((i for i,h in enumerate(hdrs)
                                     if any(k in h for k in keys)), None)
                    depth_c = _ci(["DEPTH","MD","TVD"])
                    press_c = _ci(["PRESSURE","PRESS","PSI"])
                    form_c  = _ci(["FORMATION","ZONE","INTERVAL"])
                    fluid_c = _ci(["FLUID","TYPE"])
                    mob_c   = _ci(["MOBIL","MD/CP"])
                    grad_c  = _ci(["GRADIENT","GRAD"])
                    if depth_c is None or press_c is None:
                        continue
                    for row in table[1:]:
                        if not row: continue
                        def _v(i):
                            if i is None or i >= len(row): return None
                            v = re.sub(r'[^\d.\-]','',str(row[i]))
                            try: return float(v)
                            except: return None
                        depth = _v(depth_c)
                        press = _v(press_c)
                        if depth and press:
                            rows.append({
                                "DEPTH_MD":   depth,
                                "PRESSURE":   press,
                                "FORMATION":  str(row[form_c]).strip() if form_c is not None and form_c < len(row) else None,
                                "FLUID_TYPE": str(row[fluid_c]).strip() if fluid_c is not None and fluid_c < len(row) else None,
                                "MOBILITY":   _v(mob_c),
                                "GRADIENT":   _v(grad_c),
                            })
    except Exception as e:
        result["error"] = str(e)
    return result


def _extract_scout_ticket_regex(file_path: str) -> dict:
    """LEGACY fallback: label:value regex scout extractor. Works on text-layer
    tickets in a 'Label: value' layout. Superseded by the positional-grid
    extractor (extract_scout_ticket); kept as a fallback for layouts the grid
    parser doesn't recognise."""
    import pdfplumber
    header = {}
    ip_rows = []
    result = {"header": header, "ip_rows": ip_rows, "error": None}
    try:
        full_text = get_pdf(file_path)["text_all"]
        # Header patterns
        # Two-column scout layout: "Label: value   Label2: value2" on one line.
        # Each value must stop at the *next known label*, not at any capitalized
        # word inside the value ("Pioneer Natural Resources" must stay whole).
        _LABELS = (r'Well\s+Name|Well\s+Type|Operator|Company|Lease|Field|'
                   r'County|State|Status|Spud\s+Date|Completion\s+Date|Rig|'
                   r'KB\s+Elevation|Ground\s+Elevation|GL|Total\s+Depth|TVD|'
                   r'Lateral|Azimuth|API|UWI|Well\s+(?:No|Number|#)')
        _STOP = rf'(?=\s+(?:{_LABELS})\s*[:#]|\n|$)'
        patterns = {
            "API":          r'(?:API(?:\s+Number)?|UWI)[:\s]+([0-9\-]{10,20})',
            "WELL_NAME":    r'Well\s+Name[:\s]+([A-Z0-9 #\-/]+?)' + _STOP,
            "OPERATOR":     r'(?:Operator|Company)[:\s]+([A-Za-z0-9 &.,]+?)' + _STOP,
            "FIELD":        r'Field[:\s]+([A-Za-z0-9 \-]+?)' + _STOP,
            "SPUD_DATE":    r'Spud\s+Date[:\s]+([\d\-/]+)',
            "COMPLETION_DATE": r'Completion\s+Date[:\s]+([\d\-/]+)',
            "TOTAL_DEPTH":  r'Total\s+Depth[:\s]+([\d,]+)\s*ft',
            "TVD":          r'TVD[:\s]+([\d,]+)\s*ft',
            "LATERAL":      r'Lateral(?:\s+Length)?[:\s]+([\d,]+)\s*ft',
            # ── Fields the loader (load_scout) already stores but the
            #    extractor previously never produced → captured as NULL. ──
            "COUNTY":       r'County[:\s]+([A-Za-z .\']+?)(?:,|\s+State|\n|$)',
            "STATE":        r'County[:\s]+[A-Za-z .\']+?,\s*([A-Z]{2})\b',
            "WELL_TYPE":    r'Well\s+Type[:\s]+([A-Za-z0-9 \-—–/]+?)' + _STOP,
            "WELL_STATUS":  r'Status[:\s]+([A-Za-z &\-/]+?)' + _STOP,
            "KB_ELEV":      r'KB\s+Elevation[:\s]+([\d,]+)\s*ft',
            "GROUND_ELEV":  r'(?:Ground|GL)\s+Elevation[:\s]+([\d,]+)\s*ft',
            "LEASE":        r'Lease[:\s]+([A-Za-z0-9 \-/]+?)' + _STOP,
            "WELL_NUM":     r'Well\s+(?:No|Number|#)[:\s]+([A-Za-z0-9 \-]+?)' + _STOP,
            "LATITUDE":     r'Lat(?:itude)?[:\s]+([+-]?\d{1,2}\.\d+)',
            "LONGITUDE":    r'Lon(?:gitude)?[:\s]+([+-]?\d{1,3}\.\d+)',
        }
        for field, pat in patterns.items():
            m = re.search(pat, full_text, re.IGNORECASE)
            if m:
                header[field] = m.group(1).strip().replace(',','')
        # IP table
        for _page_tables in get_pdf(file_path)["tables"]:
            for table in _page_tables:
                    if not table: continue
                    hdrs = [str(c).strip().upper() for c in (table[0] or [])]
                    if not any("OIL" in h or "FLUID" in h or "BOE" in h for h in hdrs):
                        continue
                    def _ci(keys):
                        return next((i for i,h in enumerate(hdrs)
                                     if any(k in h for k in keys)), None)
                    date_c  = _ci(["DATE","DAY"])
                    oil_c   = _ci(["OIL","BBL"])
                    gas_c   = _ci(["GAS","MCF","MMCF"])
                    water_c = _ci(["WATER","WTR"])
                    for row in table[1:]:
                        if not row: continue
                        def _v(i):
                            if i is None or i >= len(row): return None
                            v = re.sub(r'[^\d.\-]','',str(row[i]))
                            try: return float(v)
                            except: return None
                        ip_rows.append({
                            "DATE":      str(row[date_c]).strip() if date_c is not None else None,
                            "OIL_BOPD":  _v(oil_c),
                            "GAS_MCFD":  _v(gas_c),
                            "WATER_BWPD":_v(water_c),
                        })
    except Exception as e:
        result["error"] = str(e)
    return result


# =============================================================================
# Positional-grid scout extractor (primary). Reads the visual grid from word
# coordinates so multi-word cells stay whole, and (optionally) OCR-converts an
# image-only PDF first. Falls back to the legacy regex extractor if the grid
# parser finds no header. Output matches what load_scout consumes:
#   {header, tops, dst, core, frac, ip_rows, survey, core_runs, completion}
# =============================================================================
def _scout_has_text_layer(path: str) -> bool:
    import pdfplumber
    try:
        with pdfplumber.open(path) as pdf:
            for p in pdf.pages[:2]:
                if p.chars:
                    return True
    except Exception:
        pass
    return False


def _scout_ensure_text_pdf(path: str) -> str:
    """If the PDF has no text layer, OCR-convert it with ocrmypdf and return the
    new path. If ocrmypdf isn't installed or fails, return the original path
    unchanged (caller still attempts extraction). This is the ONLY OCR-dependent
    step and it degrades gracefully — the extractor works on text-layer PDFs
    with no OCR install at all."""
    if _scout_has_text_layer(path):
        return path
    import os, subprocess, tempfile
    out = os.path.join(tempfile.gettempdir(), "ocr_" + os.path.basename(path))
    try:
        subprocess.run(["ocrmypdf", "--force-ocr", "--output-type", "pdf",
                        "--quiet", path, out],
                       check=True, capture_output=True, timeout=180)
        return out
    except Exception:
        return path


def _scout_num(s):
    if s is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(s))
    try:
        return float(s)
    except Exception:
        return None


def _scout_uwi_bare14(v):
    """Strip a leading country/GID prefix (e.g. 'US') → bare-14 UWI."""
    if not v:
        return None
    s = re.sub(r"^US", "", str(v).strip().upper())
    d = re.sub(r"\D", "", s)
    return d if len(d) == 14 else (d or None)


def _scout_grid_rows(page, y_tol: int = 4):
    """Reconstruct a page's grid: rows by 'top', columns by '|' separator bands."""
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    seps = sorted({round(w["x0"]) for w in words if w["text"].strip() == "|"})
    edges = [0] + seps + [10_000]

    def col_of(x):
        for i in range(len(edges) - 1):
            if edges[i] <= x < edges[i + 1]:
                return i
        return len(edges) - 2

    rowmap = {}
    for w in words:
        if w["text"].strip() == "|":
            continue
        key = round(w["top"] / y_tol) * y_tol
        rowmap.setdefault(key, []).append(w)

    rows = []
    for key in sorted(rowmap):
        cols = {}
        for w in sorted(rowmap[key], key=lambda w: w["x0"]):
            cols.setdefault(col_of(w["x0"]), []).append(w["text"])
        cells = [" ".join(cols.get(i, [])).strip() for i in range(len(edges) - 1)]
        rows.append([c for c in cells if c != ""] or [""])
    return rows


def _scout_all_rows(path: str):
    import pdfplumber
    rows = []
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages:
            rows.extend(_scout_grid_rows(p))
    return rows


def _scout_toks(row):
    return " ".join(row).split()


def _scout_find(rows, *needles, start=0):
    for i in range(start, len(rows)):
        if all(n.upper() in " ".join(rows[i]).upper() for n in needles):
            return i
    return -1


def _scout_parse_header(rows):
    h = {}
    LABELSETS = [
        (["API", "WELL NAME", "WELL TYPE", "STATUS"],
         ["API", "WELL_NAME", "WELL_TYPE", "WELL_STATUS"]),
        (["OPERATOR", "FIELD", "COUNTY", "STATE"],
         ["OPERATOR", "FIELD", "COUNTY", "STATE"]),
        (["SPUD DATE", "COMPLETION DATE", "TOTAL DEPTH", "SURFACE"],
         ["SPUD_DATE", "COMPLETION_DATE", "TOTAL_DEPTH", "SURFACE_LOCATION"]),
        (["UWI", "KB ELEVATION", "DEPTH DATUM"],
         ["UWI", "KB_ELEV", "DEPTH_DATUM"]),
    ]
    for i, row in enumerate(rows):
        ju = " ".join(row).upper()
        for labels, keys in LABELSETS:
            if labels[0] in ju and labels[1] in ju:
                if i + 1 < len(rows):
                    for k, v in zip(keys, rows[i + 1]):
                        h[k] = v.strip()
                    # Surface-location coordinates ('32.127700N 101.560500W')
                    # don't always land in the positionally-mapped cell, because
                    # a label like 'Total Depth MD' splits into two grid cells
                    # and shifts the value row. Scan the whole value row for the
                    # coordinate pattern instead of trusting position.
                    if "SURFACE" in ju:
                        # strip OCR's stray '/' inside numbers (32.127/700N)
                        # BEFORE matching, else the regex captures only the
                        # fragment after the slash.
                        vrow = " ".join(rows[i + 1]).replace("/", "")
                        cm = re.search(r"([\d.]+)\s*N\b.*?([\d.]+)\s*W",
                                       vrow, re.I)
                        if cm:
                            h["SURFACE_LOCATION"] = cm.group(0)
                break
        if "FORMATION TOPS" in ju or "STRATIGRAPHY" in ju:
            break
    if h.get("TOTAL_DEPTH"):
        h["TOTAL_DEPTH"] = _scout_num(h["TOTAL_DEPTH"])
    if h.get("UWI"):
        h["UWI_BARE14"] = _scout_uwi_bare14(h["UWI"])
    if h.get("API"):
        h["API"] = re.sub(r"\D", "", h["API"])[:14] or h["API"]
    sl = h.get("SURFACE_LOCATION", "").replace("/", "")
    m = re.search(r"([\d.]+)\s*N\b.*?([\d.]+)\s*W", sl, re.I)
    if m:
        h["LATITUDE"] = m.group(1)
        h["LONGITUDE"] = "-" + m.group(2)
    return h


def _scout_parse_tops(rows):
    i = _scout_find(rows, "FORMATION TOPS")
    if i < 0:
        i = _scout_find(rows, "STRATIGRAPHY")
    if i < 0:
        return []
    out = []
    for row in rows[i + 1:]:
        ju = " ".join(row).upper()
        if "PETROPHYSICS" in ju or "DIRECTIONAL" in ju or "SURVEY" in ju:
            break
        if "FORMATION" in ju and "TOP" in ju:
            continue
        cells = row if len(row) > 1 else row[0].split()
        if len(cells) < 3:
            continue
        name, top, base = cells[0], _scout_num(cells[1]), _scout_num(cells[2])
        if name and top is not None:
            out.append({"FORMATION_NAME": name,
                        "DEPTH_TOP_MD": top, "DEPTH_BASE_MD": base})
    return out


def _scout_parse_survey(rows):
    i = _scout_find(rows, "DIRECTIONAL SURVEY")
    if i < 0:
        return []
    out = []
    for row in rows[i + 1:]:
        ju = " ".join(row).upper()
        if "MD" in ju and "INC" in ju:
            continue
        toks = _scout_toks(row)
        nums = [_scout_num(t) for t in toks]
        if (len(nums) >= 6 and all(n is not None for n in nums[:6])
                and nums[1] is not None and nums[1] <= 120):
            out.append({"MD": nums[0], "INC": nums[1], "AZI": nums[2],
                        "TVD": nums[3], "NS": nums[4], "EW": nums[5],
                        "DLS": nums[6] if len(nums) > 6 else None})
        elif out:
            break
    return out


def _scout_parse_dst(rows):
    out = []
    for row in rows:
        toks = _scout_toks(row)
        if (len(toks) >= 5 and re.match(r"\d{4}-\d{2}-\d{2}$", toks[0])
                and toks[1].upper() in ("DST", "RFT", "MDT")):
            out.append({"TEST_DATE": toks[0], "TEST_TYPE": toks[1],
                        "TOP": _scout_num(toks[2]), "BASE": _scout_num(toks[3]),
                        "RESULT": toks[4],
                        "OIL_RATE": _scout_num(toks[5]) if len(toks) > 5 else None,
                        "GAS_RATE": _scout_num(toks[6]) if len(toks) > 6 else None})
    return out


def _scout_parse_core(rows):
    out = []
    for row in rows:
        toks = _scout_toks(row)
        if (len(toks) >= 4 and re.match(r"^\d{3}$", toks[0])
                and _scout_num(toks[1]) is not None
                and toks[2].upper() in ("PLUG", "SIDEWALL", "FULL", "WHOLE")):
            litho = next((t for t in toks[3:] if t.upper() in
                          ("DOLOMITE", "LIMESTONE", "SANDSTONE", "SHALE",
                           "CHALK", "ANHYDRITE")), None)
            out.append({"SAMPLE_NO": toks[0], "DEPTH": _scout_num(toks[1]),
                        "SAMPLE_TYPE": toks[2],
                        "POROSITY": _scout_num(toks[3]) if len(toks) > 3 else None,
                        "PERMEABILITY": _scout_num(toks[4]) if len(toks) > 4 else None,
                        "GRAIN_DENSITY": _scout_num(toks[5]) if len(toks) > 5 else None,
                        "SW": _scout_num(toks[6]) if len(toks) > 6 else None,
                        "SO": _scout_num(toks[7]) if len(toks) > 7 else None,
                        "LITHOLOGY": litho})
    return out


def _scout_parse_core_runs(rows):
    out = []
    for row in rows:
        toks = _scout_toks(row)
        if (len(toks) >= 6 and re.match(r"^\d{1,2}$", toks[0])
                and toks[1].upper() in ("CONVENTIONAL", "SIDEWALL", "ROTARY")):
            out.append({"RUN_NO": toks[0], "CORE_TYPE": toks[1],
                        "FORMATION": toks[2] if len(toks) > 2 else None})
    return out


def _scout_parse_frac(rows):
    out = []
    for row in rows:
        toks = _scout_toks(row)
        if (len(toks) >= 7 and re.match(r"^\d{1,2}\.0$", toks[0])
                and _scout_num(toks[1]) is not None
                and _scout_num(toks[2]) is not None):
            out.append({"STAGE": int(float(toks[0])),
                        "TOP_MD": _scout_num(toks[1]), "BASE_MD": _scout_num(toks[2]),
                        "CLUSTERS": _scout_num(toks[3]) if len(toks) > 3 else None,
                        "FLUID_BBL": _scout_num(toks[5]) if len(toks) > 5 else None,
                        "PROPPANT_LBS": _scout_num(toks[6]) if len(toks) > 6 else None,
                        "ISIP": _scout_num(toks[7]) if len(toks) > 7 else None})
    return out


def _scout_parse_production(rows):
    out = []
    for row in rows:
        toks = _scout_toks(row)
        if toks and re.match(r"^\d{4}-\d{2}$", toks[0]) and len(toks) >= 4:
            out.append({"PERIOD": toks[0], "OIL_BBL": _scout_num(toks[1]),
                        "GAS_MCF": _scout_num(toks[2]),
                        "WATER_BBL": _scout_num(toks[3]),
                        "AVG_RATE": _scout_num(toks[4]) if len(toks) > 4 else None})
    return out


def _scout_parse_completion(rows):
    for row in rows:
        ju = " ".join(row).upper()
        toks = _scout_toks(row)
        if (toks and re.match(r"\d{4}-\d{2}-\d{2}$", toks[0])
                and ("FRAC" in ju or "CASED" in ju or "MULTISTAGE" in ju)):
            return [{"COMPLETION_DATE": toks[0], "DETAIL": " ".join(toks[1:])}]
    return []


def extract_scout_ticket(file_path: str) -> dict:
    """Primary scout extractor: OCR-fallback + positional-grid parsing of every
    section. Returns the dict load_scout consumes. Falls back to the legacy
    regex extractor if the grid parser finds no header (e.g. an unfamiliar
    'Label: value' layout)."""
    # Front door: a ruled-table TEXT ticket (the ReportLab export) reads cleanly
    # from its table borders via the line-based reader; only image / pipe-
    # delimited OCR tickets need the positional-grid parser below.
    try:
        try:
            from dataview.file_catalog.scout_pdf_reader import (looks_like_text_ticket,
                                                  extract_scout_ticket_text)
        except ImportError:
            from dataview.file_catalog.scout_pdf_reader import (looks_like_text_ticket,
                                          extract_scout_ticket_text)
        if looks_like_text_ticket(file_path):
            sc = extract_scout_ticket_text(file_path)
            if sc and (sc.get("header") or {}).get("WELL_NAME"):
                return sc
    except Exception:
        pass  # any trouble → fall through to the OCR grid parser

    result = {"header": {}, "tops": [], "dst": [], "core": [], "core_runs": [],
              "frac": [], "ip_rows": [], "survey": [], "completion": [],
              "error": None}
    try:
        text_pdf = _scout_ensure_text_pdf(file_path)
        rows = _scout_all_rows(text_pdf)
        h = _scout_parse_header(rows)
        if not h:                       # grid found nothing → legacy fallback
            return _extract_scout_ticket_regex(file_path)
        result["header"] = h
        result["tops"] = _scout_parse_tops(rows)
        result["dst"] = _scout_parse_dst(rows)
        result["core"] = _scout_parse_core(rows)
        result["core_runs"] = _scout_parse_core_runs(rows)
        result["frac"] = _scout_parse_frac(rows)
        result["ip_rows"] = _scout_parse_production(rows)
        result["survey"] = _scout_parse_survey(rows)
        result["completion"] = _scout_parse_completion(rows)
        if h.get("UWI_BARE14") and h.get("API"):
            api14 = re.sub(r"\D", "", h["API"])[:14]
            if api14 and h["UWI_BARE14"] != api14:
                h["UWI_API_MISMATCH"] = True
    except Exception as e:
        # any grid failure → try the legacy regex extractor before giving up
        try:
            return _extract_scout_ticket_regex(file_path)
        except Exception:
            result["error"] = str(e)
    return result


def extract_ddr(file_path: str) -> dict:
    """Extract daily drilling report — operations, parameters, mud props."""
    import pdfplumber
    ops_rows   = []
    param_rows = []
    mud_rows   = []
    result = {"ops": ops_rows, "params": param_rows,
              "mud": mud_rows, "header": {}, "error": None}
    try:
        full_text = get_pdf(file_path)["text_all"]
        # Header
        hdr = {}
        for field, pat in [
            ("REPORT_DATE", r'(?:Report\s+Date|Date)[:\s]+([\d\-/]+)'),
            ("REPORT_NO",   r'Report\s+#[:\s]+(\d+)'),
            ("MD_START",    r'Measured\s+Depth.*?start[):\s]+([\d,]+)'),
            ("MD_END",      r'Measured\s+Depth.*?end[):\s]+([\d,]+)'),
            ("PROGRESS",    r'Progress[:\s]+([\d,]+)\s*ft'),
        ]:
            m = re.search(pat, full_text, re.IGNORECASE)
            if m:
                hdr[field] = m.group(1).strip().replace(',','')
        result["header"] = hdr
        # Tables
        for _page_tables in get_pdf(file_path)["tables"]:
            for table in _page_tables:
                    if not table or len(table) < 2: continue
                    hdrs = [str(c or '').strip().upper() for c in table[0]]
                    # Operations table
                    if any("ACTIVITY" in h or "OPERATION" in h for h in hdrs):
                        for row in table[1:]:
                            if not row: continue
                            ops_rows.append({h: str(v).strip() for h,v in zip(hdrs,row) if v})
                    # Drilling parameters table
                    elif any("WOB" in h or "ROP" in h or "TORQUE" in h for h in hdrs):
                        for row in table[1:]:
                            if not row: continue
                            param_rows.append({h: str(v).strip() for h,v in zip(hdrs,row) if v})
                    # Mud properties table
                    elif any("MUD" in h or "VISCOSITY" in h or "PH" in h for h in hdrs):
                        for row in table[1:]:
                            if not row: continue
                            mud_rows.append({h: str(v).strip() for h,v in zip(hdrs,row) if v})
    except Exception as e:
        result["error"] = str(e)
    return result


def extract_well_test(file_path: str) -> dict:
    """Extract production/well test — flow periods and reservoir analysis."""
    import pdfplumber
    flow_rows = []
    analysis  = {}
    result = {"flow_rows": flow_rows, "analysis": analysis,
              "header": {}, "error": None}
    try:
        full_text = get_pdf(file_path)["text_all"]
        # Header
        hdr = {}
        for field, pat in [
            ("TEST_DATE",   r'Test\s+Date[:\s]+([\d\-/]+)'),
            ("TEST_TYPE",   r'Test\s+Type[:\s]+([A-Za-z\- ]+?)(?:\n|$)'),
            ("ZONE",        r'Zone[:\s]+([A-Za-z0-9 ]+?)(?:\n|$)'),
            ("PERFS",       r'Perforations[:\s]+([\d,]+ ?[-–] ?[\d,]+\s*ft[^,\n]*)'),
        ]:
            m = re.search(pat, full_text, re.IGNORECASE)
            if m:
                hdr[field] = m.group(1).strip()
        result["header"] = hdr
        # Analysis values
        for field, pat in [
            ("STATIC_PRESSURE",  r'Static\s+Reservoir\s+Pressure[:\s]+([\d,]+)'),
            ("PERMEABILITY",     r'(?:Formation\s+)?Permeability[^:]*[:\s]+([\d.]+)\s*mD'),
            ("SKIN",             r'Skin\s+Factor[^:]*[:\s]+([+-]?[\d.]+)'),
            ("PI",               r'Productivity\s+Index[^:]*[:\s]+([\d.]+)'),
            ("DRAINAGE_RADIUS",  r'Drainage\s+Radius[:\s]+([\d,]+)'),
            ("RESERVOIR_TEMP",   r'Reservoir\s+Temperature[:\s]+([\d.]+)'),
        ]:
            m = re.search(pat, full_text, re.IGNORECASE)
            if m:
                analysis[field] = m.group(1).replace(',','').strip()
        # Flow period table
        for _page_tables in get_pdf(file_path)["tables"]:
            for table in _page_tables:
                    if not table or len(table) < 2: continue
                    hdrs = [str(c or '').strip().upper() for c in table[0]]
                    if not any("FLOW" in h or "PERIOD" in h or "CHOKE" in h
                               or "OIL" in h for h in hdrs):
                        continue
                    for row in table[1:]:
                        if not row: continue
                        r = {}
                        for h, v in zip(hdrs, row):
                            if v:
                                clean = re.sub(r'[^\d.\-]','',str(v))
                                try:
                                    r[h] = float(clean)
                                except:
                                    r[h] = str(v).strip()
                        if r:
                            flow_rows.append(r)
    except Exception as e:
        result["error"] = str(e)
    return result


def extract_petrophysical(file_path: str) -> dict:
    """Extract zone summary and interval analysis from petrophysical report."""
    import pdfplumber
    zones    = []
    interval = []
    result   = {"zones": zones, "interval": interval, "error": None}
    try:
        for _page_tables in get_pdf(file_path)["tables"]:
            for table in _page_tables:
                    if not table or len(table) < 2: continue
                    hdrs = [str(c or '').strip().upper() for c in table[0]]
                    # Zone summary
                    if any("NET PAY" in h or "N/G" in h or "PHIE" in h for h in hdrs):
                        for row in table[1:]:
                            if not row: continue
                            r = {}
                            for h,v in zip(hdrs,row):
                                if v:
                                    clean = re.sub(r'[^\d.\-]','',str(v))
                                    try: r[h] = float(clean)
                                    except: r[h] = str(v).strip()
                            if r: zones.append(r)
                    # Interval detail
                    elif any("GR" in h or "RHOB" in h or "NPHI" in h or "RT" in h for h in hdrs):
                        for row in table[1:]:
                            if not row: continue
                            r = {}
                            for h,v in zip(hdrs,row):
                                if v:
                                    clean = re.sub(r'[^\d.\-]','',str(v))
                                    try: r[h] = float(clean)
                                    except: r[h] = str(v).strip()
                            if r: interval.append(r)
    except Exception as e:
        result["error"] = str(e)
    return result


def extract_eowr(file_path: str) -> dict:
    """Extract end of well report — summary, stratigraphy, NPT."""
    import pdfplumber
    strat_rows = []
    npt_rows   = []
    summary    = {}
    result     = {"summary": summary, "strat": strat_rows,
                  "npt": npt_rows, "error": None}
    try:
        full_text = get_pdf(file_path)["text_all"]
        for field, pat in [
            ("SPUD_DATE",     r'Spud\s+Date[:\s]+([\d\-/]+)'),
            ("RIG_RELEASE",   r'Rig\s+Release[:\s]+([\d\-/]+)'),
            ("TOTAL_DEPTH",   r'Total\s+Depth[:\s]+([\d,]+)\s*ft'),
            ("ELAPSED_DAYS",  r'Elapsed\s+Days[:\s]+(\d+)'),
            ("ACTUAL_COST",   r'Actual\s+Cost[:\s]+\$?([\d.,]+)'),
            ("AFE_COST",      r'AFE\s+Cost[:\s]+\$?([\d.,]+)'),
            ("STAGES",        r'Stages\s+completed[:\s]+(\d+)'),
            ("TOTAL_PROPPANT",r'Total\s+proppant[:\s]+([\d,]+)'),
        ]:
            m = re.search(pat, full_text, re.IGNORECASE)
            if m:
                summary[field] = m.group(1).strip().replace(',','')
        for _page_tables in get_pdf(file_path)["tables"]:
            for table in _page_tables:
                    if not table or len(table) < 2: continue
                    hdrs = [str(c or '').strip().upper() for c in table[0]]
                    if any("FORMATION" in h for h in hdrs) and any("TOP" in h for h in hdrs):
                        for row in table[1:]:
                            if not row: continue
                            r = {h: str(v).strip() for h,v in zip(hdrs,row) if v}
                            if r: strat_rows.append(r)
                    elif any("NPT" in h or "EVENT" in h or "DURATION" in h for h in hdrs):
                        for row in table[1:]:
                            if not row: continue
                            r = {h: str(v).strip() for h,v in zip(hdrs,row) if v}
                            if r: npt_rows.append(r)
    except Exception as e:
        result["error"] = str(e)
    return result


def extract_casing_cement(file_path: str) -> dict:
    """Extract casing programme and cement job data."""
    import pdfplumber
    casing_rows  = []
    cement_rows  = []
    cbl_rows     = []
    result       = {"casing": casing_rows, "cement": cement_rows,
                    "cbl": cbl_rows, "error": None}
    try:
        for _page_tables in get_pdf(file_path)["tables"]:
            for table in _page_tables:
                    if not table or len(table) < 2: continue
                    hdrs = [str(c or '').strip().upper() for c in table[0]]
                    if any("STRING" in h or "CASING" in h for h in hdrs) and any("OD" in h or "WEIGHT" in h for h in hdrs):
                        for row in table[1:]:
                            if not row: continue
                            r = {h: str(v).strip() for h,v in zip(hdrs,row) if v}
                            if r: casing_rows.append(r)
                    elif any("SLURRY" in h or "SACK" in h or "CEMENT" in h for h in hdrs):
                        for row in table[1:]:
                            if not row: continue
                            r = {h: str(v).strip() for h,v in zip(hdrs,row) if v}
                            if r: cement_rows.append(r)
                    elif any("CBL" in h or "BOND" in h or "AMPLITUDE" in h for h in hdrs):
                        for row in table[1:]:
                            if not row: continue
                            r = {h: str(v).strip() for h,v in zip(hdrs,row) if v}
                            if r: cbl_rows.append(r)
    except Exception as e:
        result["error"] = str(e)
    return result


# =============================================================================
# resolve_pdf_fields — THE single owner of "PDF → well fields".
#
# Both field-extraction dispatchers (extract_core._extract_fields and
# file_summarizer._summarize_pdf) must call THIS, rather than each re-doing the
# classify → extended-classify → grid-header dance. Today's NULL-UWI bug took
# four edits precisely because that logic was copied into several places; this
# function exists so a future fix lands in exactly one place.
#
# Returns a flat dict with the canonical keys downstream code reads:
#   report_type, uwi, well_name, operator, field, state, county,
#   latitude, longitude, total_depth, spud_date, rig_release,
#   survey_type, contractor, confidence
# Missing values come back as None (confidence as 0.0). Never raises — on any
# internal error it returns whatever was resolved so far.
# =============================================================================
def resolve_pdf_fields(file_path: str) -> dict:
    out = {
        "report_type": "UNKNOWN", "uwi": None, "well_name": None,
        "operator": None, "field": None, "state": None, "county": None,
        "latitude": None, "longitude": None, "total_depth": None,
        "spud_date": None, "rig_release": None, "survey_type": None,
        "contractor": None, "confidence": 0.0,
    }
    try:
        cl = classify_pdf(file_path)
        out.update({k: cl.get(k) for k in out if k in cl})

        # extended_classify_pdf resolves scout/EOW/well-test report types and,
        # critically, reads the UWI from grid-layout tickets / 'US'-prefixed GIDs
        # that the base classify_pdf label:value patterns miss.
        try:
            ex = extended_classify_pdf(file_path)
            if ex.get("report_type") and ex["report_type"] != "UNKNOWN":
                out["report_type"] = ex["report_type"]
            for k in ("uwi", "well_name", "operator"):
                if not out.get(k) and ex.get(k):
                    out[k] = ex[k]
            wn = (out.get("well_name") or "").upper()
            if ex.get("well_name") and ("SCOUT" in wn or "TICKET" in wn):
                out["well_name"] = ex["well_name"]
        except Exception:
            pass

        # Scout tickets: pull location/identity straight from the grid header
        # (lat/long/state/county/UWI) for anything still missing.
        if out.get("report_type") == "SCOUT_TICKET" and (
                not out.get("latitude") or not out.get("uwi")):
            try:
                h = (extract_scout_ticket(file_path).get("header") or {})
                if not out.get("uwi") and h.get("UWI_BARE14"):
                    out["uwi"] = h["UWI_BARE14"]
                for src, dst in (("LATITUDE", "latitude"),
                                 ("LONGITUDE", "longitude"),
                                 ("STATE", "state"), ("COUNTY", "county"),
                                 ("OPERATOR", "operator"),
                                 ("WELL_NAME", "well_name"),
                                 ("TOTAL_DEPTH", "total_depth"),
                                 ("SPUD_DATE", "spud_date")):
                    if not out.get(dst) and h.get(src) is not None:
                        out[dst] = h[src]
            except Exception:
                pass

        out["confidence"] = float(out.get("confidence") or 0)
    except Exception:
        pass
    return out
