"""
extract_core.py
===============
Streamlit-free home for the file header-extraction logic.

This module is imported BOTH by page_workbench (the UI) and, crucially, by the
pipeline's process-pool workers. It deliberately imports NOTHING from streamlit
or page_workbench so a `ProcessPoolExecutor` worker can `import extract_core`
and parse a file in a clean subprocess without dragging the whole UI (and its
streamlit import) into every child process.

`_extract_fields` is the single source of truth for header extraction — it is
defined here and re-exported by page_workbench (`from extract_core import
_extract_fields`), so there is exactly one copy of the dispatch logic. The
per-format parsers it uses (segy_header, modules.pdf_survey_catalog,
modules.lis_catalog, modules.shapefile_catalog, modules.csv_catalog,
modules.file_summarizer, lasio, dlisio) are imported lazily inside the function
in their own try/except, so they resolve at call time in whatever process and
degrade gracefully if a given parser is unavailable.
"""
import os
import sys
import re
import time
import uuid          # _well_params/_seis_params use uuid.uuid5 for PPDM_GUID
from pathlib import Path

# ── Extension sets (canonical) ─────────────────────────────────────────────
# Defined here so the parser and the UI share one definition; page_workbench
# imports these back. Add a new format extension here, not in two places.
PDF_EXTS    = {".pdf"}
LAS_EXTS    = {".las"}
DLIS_EXTS   = {".dlis", ".dlf", ".dis"}
LIS_EXTS    = {".lis"}
SEGY_EXTS   = {".segy", ".sgy", ".seg"}
P190_EXTS   = {".p190", ".p90", ".p1"}
SHP_EXTS    = {".shp", ".gpkg", ".kml", ".kmz"}
OFFICE_EXTS = {".xlsx", ".xls", ".xlsm", ".docx", ".doc",
               ".ods", ".odt", ".odp"}   # ODF: routed through summarize()
CSV_EXTS    = {".csv", ".tsv"}
IMAGE_EXTS  = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}
WITSML_EXTS = {".xml"}
JSON_LOG_EXTS = {".json"}
LOG_EXTS    = LAS_EXTS | DLIS_EXTS | LIS_EXTS


def _clean_survey_name(raw: str) -> str:
    """Strip volume/acquisition metadata from a SEG-Y survey name so the same
    survey's variants (different sample rate, vintage, processing) collapse to
    one survey identity for dedup. Volume detail belongs on dv_seis_line, not in
    the survey name.

    SEG-Y text headers pack everything on one line, e.g.
        "CENTRAL EROMANGA BASIN 80 SEISMIC SURVEY, AUG, 1980, SAMPLE INT:4M"
    We keep the survey identity and cut at the first metadata marker (a date
    token, SAMPLE INT, or a trailing processing tag). Defensive against the
    messy free-text these headers contain (stray control/garbage chars,
    inconsistent spacing). Falls back to the trimmed raw name if nothing matches.
    """
    if not raw:
        return raw
    s = str(raw)
    # Normalize whitespace and drop non-printable/garbage chars (e.g. the stray
    # '¦' seen in real headers) so the cut points match reliably.
    s = re.sub(r"[^\x20-\x7E]", " ", s)          # keep printable ASCII only
    s = re.sub(r"\s+", " ", s).strip()
    # Cut at the first metadata marker. Markers, in priority order:
    #   - ", <MONTH>"  (date: JAN..DEC)  - ", <4-digit year>"
    #   - "SAMPLE INT" / "SAMPLE RATE"   - ", NANOSECOND"/processing tails
    _markers = [
        r",\s*(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b",
        r",\s*(?:19|20)\d{2}\b",
        r"\bSAMPLE\s*(?:INT|RATE)\b",
        r",\s*\d+\s*M?S\b",                       # ", 4MS" trailing sample tag
    ]
    cut = len(s)
    for pat in _markers:
        m = re.search(pat, s, re.IGNORECASE)
        if m and m.start() < cut:
            cut = m.start()
    cleaned = s[:cut].strip().rstrip(",").strip()
    return cleaned or s          # never return empty — fall back to full string


# The SEG-Y rev-0 textual header is a fixed CARD IMAGE: 40 lines of printed
# LABELS a processor is meant to type values between. When nobody typed
# anything, line C 2 still reads
#
#     C 2 LINE            AREA                        MAP ID
#
# and the survey-name regex dutifully captures "AREA MAP ID" — five Teapot 2D
# lines all carrying the same name that is not a name. It is worse than blank:
# NULL is caught by the _HOLD_SEIS_UNNAMED gate, whereas this is non-null, so
# it passes every check and promote_seismic groups all five files into one
# dv_seis_set called AREA MAP ID. The same shape as the placeholder problem
# find_placeholders.sql exists for — a wrong value defeating every repair
# keyed on "missing".
#
# The test is structural, not a blocklist: what survived is nothing but the
# template's own printed labels with no operator text between them. Real names
# ("NAVAL PETROLEUM RESERVE #3 (TEAPOT DOME)", "CENTRAL EROMANGA BASIN 80")
# carry words that are not on the card.
_TEMPLATE_WORDS = {
    "LINE", "AREA", "MAP", "ID", "CLIENT", "COMPANY", "CREW", "NO", "REEL",
    "DAY", "START", "OF", "YEAR", "OBSERVER", "INSTRUMENT", "MFG", "MODEL",
    "SERIAL", "SURVEY", "PROJECT", "NAME", "NUMBER", "TYPE", "AND", "THIS",
}


def _is_template_survey_name(name) -> bool:
    """True when a captured survey name is only SEG-Y card-image labels.

    Requires at least two tokens: a single word like 'LINE' is ambiguous (a
    survey really can be called that), while 'AREA MAP ID' is the card. Any
    token that is not a label — a digit, a place, an operator — makes it a
    real name and this returns False.
    """
    if not name:
        return False
    toks = [t for t in re.split(r"[^A-Za-z0-9#]+", str(name).upper()) if t]
    if len(toks) < 2:
        return False
    return all(t in _TEMPLATE_WORDS for t in toks)


def _normalize_uwi(v):
    """Normalize a UWI to bare digits (no dashes/spaces/dots), the canonical
    form used throughout the system (dv_well, gold, scout-ticket resolution).
    A CSV/LAS UWI like '42-329-10001-0000' or '17-031-10035-0000' must become
    '42329100010000' so it matches the bare-14 keys everywhere else.
    Returns None for empty/missing. Leaves non-numeric ids (rare) as-is after
    stripping separators, so a genuinely alphanumeric id isn't destroyed.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("none", "nan"):
        return None
    import re as _re
    stripped = _re.sub(r"[\s\-\.]", "", s)
    return stripped or None


def _identity_from_filename(fpath: str) -> dict:
    """Derive well identity from a filename when the file's own header lacks it.
    Common for binary log formats (DLIS/LIS) where the origin/header carries no
    UWI or a junk internal id, but the filename is meaningful — e.g.
    'WHITING_BURK_177.lis' or 'ANADARKO_BURK_145.dlis'.

    Returns {well_name, operator_hint}. well_name is the filename stem (cleaned);
    operator_hint is the first underscore/space-delimited token if it looks like
    a name (alphabetic), else None. Callers decide whether to trust the hint.
    """
    stem = os.path.splitext(os.path.basename(fpath))[0].strip()
    out = {"well_name": stem or None, "operator_hint": None}
    parts = re.split(r"[_\s]+", stem)
    if parts and parts[0].isalpha() and len(parts[0]) > 2:
        out["operator_hint"] = parts[0].title()
    return out


def _p190_dms(tok, hemi, deg_len):
    """DDMMSS.SS (or DDDMMSS.SS) + hemisphere -> signed decimal degrees.

    Minute/second fields are space-padded in real files ("114 552.59E" is
    114 deg 05 min 52.59 sec), so each part is stripped before conversion.
    """
    d = int((tok[:deg_len] or "").strip() or 0)
    m = int((tok[deg_len:deg_len + 2] or "").strip() or 0)
    sec = float((tok[deg_len + 2:] or "").strip() or 0)
    val = d + m / 60.0 + sec / 3600.0
    return -val if hemi.upper() in ("S", "W") else val


_P190_LAT_RE = re.compile(r"([\d ]{6}\.\d{2})([NS])")
_P190_LON_RE = re.compile(r"([\d ]{7}\.\d{2})([EW])")


def _p190_latlon(line):
    """(lon, lat) in decimal degrees from a P1/90 Type-1 record, else None.

    Tries the SPEC columns first (26-35 lat, 36-46 lon) — unambiguous, and
    correct for conforming files. Falls back to a pattern scan because files
    in the wild are not reliably column-aligned: of three real samples, only
    one put latitude at column 26; the others sat 3 and 4 columns left. A
    fixed-column-only parser silently returns nothing on those.
    """
    try:
        lat_t, lat_h = line[25:34], line[34:35]
        lon_t, lon_h = line[35:45], line[45:46]
        if (lat_h.upper() in ("N", "S") and lon_h.upper() in ("E", "W")
                and any(ch.isdigit() for ch in lat_t)):
            la = _p190_dms(lat_t, lat_h, 2)
            lo = _p190_dms(lon_t, lon_h, 3)
            if -90 <= la <= 90 and -180 <= lo <= 180:
                return (lo, la)
    except Exception:
        pass
    try:
        m1 = _P190_LAT_RE.search(line)
        if not m1:
            return None
        m2 = _P190_LON_RE.search(line, m1.end())
        if not m2:
            return None
        la = _p190_dms(m1.group(1), m1.group(2), 2)
        lo = _p190_dms(m2.group(1), m2.group(2), 3)
        if -90 <= la <= 90 and -180 <= lo <= 180:
            return (lo, la)
    except Exception:
        pass
    return None


# Nav geometry, cached PER FOLDER. _extract_fields is called once per FILE
# and gets only a path — no folder awareness — so without this the same nav
# file would be read and reprojected once per SEG-Y beside it. Six workers
# each build their own cache, which is correct: they are separate processes
# and a shared one would need locking for no gain.
_NAV_CACHE: dict = {}


def _nav_for(fpath: str):
    """The navigation geometry for this file's SURVEY, or None.

    HOW A NAV FILE IS RELATED TO A SURVEY: by FOLDER. A seismic deliverable
    arrives as a folder — the volumes, the navigation, the load sheet — so the
    nav sitting beside a SEG-Y is that SEG-Y's nav. Nothing has to be
    specified because the directory already says it, which is also why one
    survey at a time is the natural unit: two surveys sharing one folder
    cannot be told apart by any rule, and none are delivered that way.

    Returns {"lines": {key: [(lon,lat)...]}, "hull": wkt, "epsg": int} in
    WGS84, or None when there is no nav, no CRS in its header, or pyproj is
    missing. NEVER guesses a CRS — same rule as the SEG-Y branch, same reason:
    a confident wrong position plots, so nobody re-checks it.
    """
    import os as _os
    folder = _os.path.dirname(_os.path.abspath(fpath))
    if folder in _NAV_CACHE:
        return _NAV_CACHE[folder]

    # SEARCH UPWARD, not just beside. A deliverable is a survey FOLDER with
    # subfolders — 2D_Seismic, 3D_Seismic, Documents — and the contractor
    # decides which one the navigation lands in. Teapot keeps its nav one
    # level ABOVE the volumes, so a same-folder-only rule finds nothing.
    #
    # THREE LEVELS, AND NO MORE. Far enough to cross the survey folder and its
    # siblings; short enough that a scan rooted at C:\Data cannot reach a
    # different survey's nav and place these lines in the wrong basin. The
    # depth limit IS the safety rule — a nav found five levels up belongs to
    # something else.
    out = None
    try:
        from dataview.file_catalog.seis_nav import read_nav, to_wgs84, hull_polygon
        def _scan_dir(d, up):
            """Nav in THIS directory only. Returns the parsed result or None."""
            try:
                entries = sorted(_os.listdir(d))
            except OSError:
                return None
            for entry in entries:
                p = _os.path.join(d, entry)
                if not _os.path.isfile(p):
                    continue
                if _os.path.splitext(p)[1].lower() in SEGY_EXTS:
                    continue
                nav = read_nav(p)
                if not nav or not nav.get("epsg"):
                    continue           # not a nav file, or it states no CRS
                ll = to_wgs84(nav["lines"], nav["epsg"])
                allp = [pt for v in ll.values() for pt in v]
                return {"lines": ll, "hull": hull_polygon(allp),
                        "epsg": nav["epsg"], "src": entry,
                        "src_dir": d, "levels_up": up}
            return None

        # UP ONE LEVEL ONLY, PLUS SUBFOLDERS AT EACH STEP. Upward alone is
        # not enough:
        # Teapot keeps its volumes in `CD files\` and the navigation in
        # `CD files\2D_Seismic\` — a SIBLING subfolder, so the nav is neither
        # beside the SEG-Y nor above it. A deliverable is a survey folder whose
        # contractor chose where each piece went, and the only reliable
        # statement is "somewhere in this survey's tree".
        _seen, _dir = set(), folder
        # ONE LEVEL UP IS THE LIMIT, and the limit IS the safety rule.
        # At two levels a scan rooted above several surveys reaches a
        # SIBLING SURVEY's navigation and draws its lines here — proved in
        # test: OtherSurvey/lineX.sgy happily picked up Teapot's nav two
        # levels up. Finding nothing is recoverable; lines in the wrong
        # basin are a confident wrong answer nobody re-checks.
        for _up in range(2):
            if not _dir or _dir in _seen:
                break
            _seen.add(_dir)
            out = _scan_dir(_dir, _up)
            if out:
                break
            # immediate subfolders of this level, one deep only
            try:
                _subs = sorted(_os.path.join(_dir, e) for e in _os.listdir(_dir)
                               if _os.path.isdir(_os.path.join(_dir, e)))
            except OSError:
                _subs = []
            for _sub in _subs:
                if _sub in _seen:
                    continue
                _seen.add(_sub)
                out = _scan_dir(_sub, _up)
                if out:
                    break
            if out:
                break
            _parent = _os.path.dirname(_dir)
            _dir = _parent if _parent != _dir else None
    except Exception:
        out = None
    _NAV_CACHE[folder] = out
    return out


def _segy_fallback_epsg():
    """Optional CRS for SEG-Y whose text header carries no EPSG hint.

    Set DV_SEGY_EPSG to the source EPSG for a batch (e.g. 32754 = WGS84/UTM
    zone 54S, the Cooper/Eromanga basins) and those files reproject instead of
    going unpositioned. Unset means 'unknown', and unknown means no bbox —
    never a guess.
    """
    v = (os.environ.get("DV_SEGY_EPSG") or "").strip()
    try:
        return int(v) if v else None
    except ValueError:
        return None


def _shp_outline_wkt(fpath: str):
    """Read a (seismic) shapefile's geometry and return a single WGS84 WKT
    footprint suitable for a SQL Server geography column.

    - dissolves all features into one geometry (a survey may be several polygons
      or many 2D lines) so dv_seis_set gets one outline per survey file
    - reprojects to EPSG:4326 (geography SRID)
    - fixes ring orientation: shapefiles are commonly wound clockwise, which a
      geography column interprets as the COMPLEMENT (the whole Earth minus the
      polygon). We detect that (a valid-earth polygon can't exceed half the
      globe) via shapely and flip with orient(); the DB side also guards by
      area, but emitting correct WKT here avoids a whole-Earth round-trip.

    Returns a WKT string, or None if geometry can't be read.
    """
    try:
        import geopandas as gpd
        from shapely.ops import unary_union
        from shapely.geometry.polygon import orient
    except Exception:
        return None
    # Above this feature count, dissolving every geometry (unary_union) is the
    # dominant parse cost — a handful of huge shapefiles (e.g. 29k lease blocks)
    # can take seconds in a single worker. For those the exact dissolved outline
    # is overkill for a map footprint, so use the vectorized extent (total_bounds
    # → bbox), which is O(n) and effectively instant.
    SHP_OUTLINE_CAP = 2000
    try:
        gdf = gpd.read_file(fpath)
        if gdf.empty:
            return None
        # reproject to WGS84 lon/lat so the WKT matches geography SRID 4326
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(4326)
        if len(gdf) > SHP_OUTLINE_CAP:
            from shapely.geometry import box
            minx, miny, maxx, maxy = gdf.total_bounds
            geom = box(minx, miny, maxx, maxy)     # survey extent, no dissolve
        else:
            geom = unary_union(list(gdf.geometry))  # exact footprint (small file)
        if geom is None or geom.is_empty:
            return None
        # Reorient polygonal geometry to CCW exterior (geography's left-hand
        # rule). orient(sign=1.0) makes exteriors CCW, holes CW. Lines are
        # returned unchanged. Applies per-polygon for multipolygons.
        gt = geom.geom_type
        if gt == "Polygon":
            geom = orient(geom, sign=1.0)
        elif gt == "MultiPolygon":
            from shapely.geometry import MultiPolygon
            geom = MultiPolygon([orient(p, sign=1.0) for p in geom.geoms])
        return geom.wkt
    except Exception:
        return None


def _extract_fields(fpath: str, fext: str) -> dict:
    """Extract header fields from a file. Returns flat dict.

    Returns a dict with skip_reason set (and all other fields at defaults)
    when the file should be skipped rather than extracted. Callers check
    for skip_reason before attempting any further processing. Skipped files
    are written with HEADER_EXTRACTED='S' so they are not re-attempted.
    """
    # ── Size gate — check before ANY extraction attempt ───────────────────────
    # Large files can hang extractors that parse entire file structures
    # (openpyxl XML parse, pdfplumber on scanned PDFs). Check file size
    # first and skip immediately if over the per-format threshold.
    # Thresholds are conservative — legitimate petroleum data files rarely
    # exceed these sizes for their header-only content.
    _SIZE_LIMITS_MB = {
        ".xlsx": 50,   # openpyxl XML parse scales with file size
        ".xls":  50,   # xlrd same issue
        ".xlsm": 50,
        ".pdf":  150,  # pdfplumber slow on large scanned PDFs
        ".docx": 100,  # python-docx is fast but guard against edge cases
        ".doc":  100,
        ".xml":  100,  # WITSML files with thousands of stations can be large
        ".json": 200,  # OSDU JSON with large production volumes or log data
    }
    _limit_mb = _SIZE_LIMITS_MB.get(fext)
    if _limit_mb is not None:
        try:
            _size_mb = Path(fpath).stat().st_size / (1024 * 1024)
            if _size_mb > _limit_mb:
                return {
                    "file_category": "UNKNOWN",
                    "report_type":   "UNKNOWN",
                    "confidence":    0.0,
                    "uwi": None, "well_name": None, "operator": None,
                    "well_field": None, "state": None, "county": None,
                    "latitude": None, "longitude": None,
                    "total_depth": None, "spud_date": None,
                    "rig_release": None, "survey_type": None,
                    "contractor": None,
                    "survey_name": None, "line_name": None,
                    "seis_set_type": None, "survey_date": None,
                    "bbox_min_lat": None, "bbox_max_lat": None,
                    "bbox_min_lon": None, "bbox_max_lon": None,
                    "epsg_code": None, "sample_interval": None,
                    "trace_count": None, "shot_first": None,
                    "shot_last": None,
                    "skip_reason": (
                        f"TOO_LARGE: {_size_mb:.1f} MB exceeds "
                        f"{_limit_mb} MB limit for {fext}"
                    ),
                }
        except OSError:
            pass  # Can't stat — let extraction proceed and fail naturally
    fields = {
        "file_category": "UNKNOWN",
        "report_type":   "UNKNOWN",
        "confidence":    0.0,
        # Well fields
        "uwi": None, "well_name": None, "operator": None,
        "well_field": None, "state": None, "county": None,
        "latitude": None, "longitude": None,
        "total_depth": None, "spud_date": None,
        "rig_release": None, "survey_type": None, "contractor": None,
        # Log curve fields — populated by LAS, DLIS, LIS, WITSML log, JSON log
        "curve_names": [], "n_curves": 0,
        # Seis fields
        "survey_name": None, "line_name": None,
        "seis_set_type": None, "survey_date": None,
        "bbox_min_lat": None, "bbox_max_lat": None,
        "bbox_min_lon": None, "bbox_max_lon": None,
        "epsg_code": None, "sample_interval": None,
        "trace_count": None, "shot_first": None, "shot_last": None,
        # 3D-specific geometry fields
        "il_min": None, "il_max": None,   # inline range
        "xl_min": None, "xl_max": None,   # crossline range
        "survey_outline": None,            # WKT polygon of survey footprint (WGS84)
    }

    try:
        if fext == ".pdf":
            fields["file_category"] = "WELL"
            try:
                # Single owner of PDF→fields resolution (classify + extended
                # classify + scout grid header). See pdf_survey_catalog.
                from dataview.file_catalog.pdf_survey_catalog import resolve_pdf_fields
                cl = resolve_pdf_fields(fpath)
                fields.update({
                    "report_type": cl.get("report_type","UNKNOWN"),
                    "uwi":         cl.get("uwi"),
                    "well_name":   cl.get("well_name"),
                    "operator":    cl.get("operator"),
                    "well_field":  cl.get("field"),
                    "state":       cl.get("state"),
                    "county":      cl.get("county"),
                    "latitude":    cl.get("latitude"),
                    "longitude":   cl.get("longitude"),
                    "total_depth": cl.get("total_depth"),
                    "spud_date":   cl.get("spud_date"),
                    "rig_release": cl.get("rig_release"),
                    "survey_type": cl.get("survey_type"),
                    "contractor":  cl.get("contractor"),
                    "confidence":  float(cl.get("confidence") or 0),
                })
            except Exception:
                pass

        elif fext == ".las":
            fields["file_category"] = "WELL"
            fields["report_type"]   = "WELL_LOG"
            try:
                import lasio
                # header-only: skip the curve-data array (faster, we only need
                # the ~Well and ~Curve header sections).
                las = lasio.read(fpath, ignore_data=True)
                def _wv(m):
                    try:
                        v = str(las.well[m].value).strip()
                        return v if v and v.lower() not in (
                            "","unknown","none","--") else None
                    except Exception:
                        return None
                # identity fields (top level — what the catalog row needs).
                # operator = COMP/PROV (well owner); SRVC is the service company
                # → contractor. Keep them distinct.
                fields.update({
                    "uwi":         _wv("UWI") or _wv("API"),
                    "well_name":   _wv("WELL"),
                    "operator":    _wv("COMP") or _wv("PROV"),
                    "well_field":  _wv("FLD")  or _wv("FIELD"),
                    "state":       _wv("STAT") or _wv("STATE"),
                    "county":      _wv("CNTY") or _wv("COUNTY"),
                    "latitude":    _wv("SLAT") or _wv("LAT"),
                    "longitude":   _wv("SLON") or _wv("LON") or _wv("LONG"),
                    "total_depth": _wv("STOP") or _wv("TD"),
                    "spud_date":   _wv("SPUD") or _wv("DATE"),
                    "contractor":  _wv("SRVC") or _wv("SERVICE"),
                })
                # curve/log details (format-specific block — what a log
                # consumer needs). Curve names come from the ~Curve header.
                try:
                    cnames = [c.mnemonic for c in las.curves]
                except Exception:
                    cnames = []
                fields["details"] = {
                    "curves":      len(cnames),
                    "curve_names": cnames,
                    "depth_start": _wv("STRT"),
                    "depth_stop":  _wv("STOP"),
                    "depth_step":  _wv("STEP"),
                    "null_value":  _wv("NULL"),
                }
            except Exception:
                pass

        elif fext in DLIS_EXTS:
            # DLIS origins frequently have NO UWI (well_id often empty) and an
            # internal log id for the name. The FILENAME is the authoritative
            # identity for DLIS (e.g. ANADARKO_BURK_145.dlis). Verified against
            # ANADARKO_BURK_145.dlis (2026-06-26): well_id empty, origin name
            # 'A/5-1' (junk), filename gives the real identity.
            fields["file_category"] = "WELL"
            fields["report_type"]   = "WELL_LOG"
            try:
                import dlisio, os as _os
                f, *tail = dlisio.dlis.load(fpath)
                lfs = [f] + list(tail)
                origs = list(f.origins)
                o = origs[0] if origs else None
                def _ov(attr):
                    v = str(getattr(o, attr, "") or "").strip() if o else ""
                    return v or None
                stem = _identity_from_filename(fpath)["well_name"]
                # The origin's well_name is frequently an internal log id
                # (e.g. 'A/5-1') rather than the real well. The FILENAME is the
                # authoritative identity for DLIS (e.g. ANADARKO_BURK_145), so
                # prefer the filename; keep the origin name in details.
                _orig_wn = _ov("well_name")
                fields.update({
                    "uwi":        _ov("well_id"),          # often empty in DLIS
                    "well_name":  stem or _orig_wn,        # filename wins
                    "well_field": _ov("field_name"),
                    "operator":   _ov("company"),
                    "contractor": _ov("producer_name"),
                })
                try:
                    fields["details"] = {
                        "logical_files": len(lfs),
                        "channels": sum(len(lf.channels) for lf in lfs),
                        "frames":   sum(len(lf.frames)   for lf in lfs),
                        "origin_well_name": _ov("well_name"),  # internal log id
                    }
                except Exception:
                    pass
                try:
                    for lf in lfs: lf.close()
                except Exception:
                    pass
            except Exception:
                pass

        elif fext in LIS_EXTS:
            # LIS (older than DLIS) typically yields curves but NO header
            # identity — classify_lis returns null well_name/uwi/operator. Like
            # DLIS, the FILENAME is the authoritative identity (e.g.
            # WHITING_BURK_177.lis). Verified against WHITING_BURK_177.lis
            # (2026-06-26): header identity all null, 4 curves / 2 frames.
            fields["file_category"] = "WELL"
            fields["report_type"]   = "WELL_LOG"
            try:
                import os as _os
                from dataview.file_catalog.lis_catalog import classify_lis
                cl = classify_lis(fpath)
                stem = _identity_from_filename(fpath)["well_name"]
                fields.update({
                    "uwi":         cl.get("uwi"),
                    "well_name":   cl.get("well_name") or stem,  # filename fallback
                    "operator":    cl.get("operator"),
                    "well_field":  cl.get("well_field"),
                    "state":       cl.get("state"),
                    "county":      cl.get("county"),
                    "contractor":  cl.get("contractor"),
                    "confidence":  float(cl.get("confidence") or 0),
                })
                fields["details"] = {
                    "curves":      cl.get("n_curves", 0),
                    "curve_names": cl.get("curve_names", []),
                    "frames":      cl.get("n_frames", 0),
                    "depth_start": cl.get("depth_start"),
                    "depth_stop":  cl.get("depth_stop"),
                }
            except Exception:
                pass

        elif fext in SEGY_EXTS:
            fields["file_category"] = "SEIS"
            fields["report_type"]   = "SEISMIC"
            try:
                import re as _re
                from dataview.file_catalog.segy_header import read_segy_header
                # CRS + corner readers are OPTIONAL: if crs_from_segy is
                # absent the file simply gets no header-derived CRS
                # (DV_SEGY_EPSG still applies) — never a guessed one.
                try:
                    from dataview.file_catalog.crs_from_segy import (
                        crs_from_text as _crs_from_text,
                        survey_corners as _survey_corners)
                except Exception:
                    _crs_from_text = _survey_corners = None
                h = read_segy_header(fpath)
                if h.get("ok"):
                    n_traces = h.get("n_traces") or 0
                    fields["trace_count"] = n_traces
                    if h.get("sample_interval_us"):
                        fields["sample_interval"] = h["sample_interval_us"]
                    # 2D/3D from the real inline/crossline grid; fall back to the
                    # old trace-count rule only when geometry was flat/missing
                    _dims = (h.get("dims") or "").replace("?", "")
                    if _dims not in ("2D", "3D"):
                        _dims = "3D" if n_traces > 10000 else "2D"
                    fields["seis_set_type"] = _dims
                    is_3d = _dims == "3D"
                    # header fields the segyio path never captured
                    if h.get("n_samples"):
                        fields["n_samples"] = h["n_samples"]
                    if h.get("format_desc"):
                        fields["sample_format"] = h["format_desc"]
                    if h.get("measurement_system"):
                        fields["measurement_system"] = h["measurement_system"]

                    # ── Text header — survey name, contractor, CRS hint ───
                    _epsg_hint = None
                    txt = h.get("textual_header") or ""
                    try:
                        m = _re.search(
                            r"(?:LINE|SURVEY|PROJECT|NAME)[:\s]+([^\r\n]+?)\s*$",
                            txt, _re.IGNORECASE | _re.MULTILINE)
                        if m:
                            _raw_name = m.group(1).strip()
                            # Root-cause fix: SEG-Y survey lines pack survey +
                            # acquisition + processing detail into one free-text
                            # line, e.g.
                            #   "CENTRAL EROMANGA BASIN 80 SEISMIC SURVEY, AUG,
                            #    1980, SAMPLE INT:4M"
                            # The trailing date / SAMPLE INT is VOLUME-level, not
                            # survey identity — keeping it makes the same survey's
                            # volumes look like different surveys. Cut the name at
                            # the first metadata marker so survey_name is just the
                            # survey; volume detail (sample rate) lives on
                            # dv_seis_line. _clean_survey_name is defined at module
                            # top; falls back to the raw name if nothing matches.
                            _clean = _clean_survey_name(_raw_name)
                            # An UNTYPED card image yields the card's own
                            # labels ("AREA MAP ID"). Leaving survey_name NULL
                            # hands the file to the _HOLD_SEIS_UNNAMED gate,
                            # which says "assign a survey name in Browse &
                            # View" — the correct instruction. Keeping the
                            # label would silently merge every untouched file
                            # in the corpus into one invented survey.
                            if _is_template_survey_name(_clean):
                                # 'rejected-template' is what stops enrich
                                # substituting the FILE NAME for the name we
                                # just refused — see SEIS_NAME_COLS.
                                fields["survey_name_source"] = "rejected-template"
                                _d = fields.get("details") or {}
                                _d["survey_name_rejected"] = _clean[:120]
                                _d["survey_name_reason"] = (
                                    "SEG-Y card-image labels, not a name")
                                fields["details"] = _d
                            else:
                                fields["survey_name"] = _clean[:255]
                                fields["survey_name_source"] = "header"
                            # If the header carried a sample interval in-text and
                            # we didn't already get one from the binary header,
                            # capture it from the stripped tail.
                            if not fields.get("sample_interval"):
                                _si = _re.search(
                                    r"SAMPLE\s*INT[^\d]*(\d+(?:\.\d+)?)",
                                    _raw_name, _re.IGNORECASE)
                                if _si:
                                    try:
                                        fields["sample_interval"] = float(_si.group(1))
                                    except ValueError:
                                        pass
                        m2 = _re.search(
                            r"CONTRACTOR[:\s]+([A-Za-z0-9_\-\s\.]+)",
                            txt, _re.IGNORECASE)
                        if m2:
                            fields["contractor"] = m2.group(1).strip()[:255]
                        # CRS: read what the header DECLARES, via
                        # crs_from_segy — never inferred from coordinate
                        # magnitudes. The inline regexes this replaces were
                        # DATUM-BLIND: "AMG ZONE 54 ... SURVEY DATUM:GDA2020"
                        # would not match them at all (no "UTM"), and a
                        # datum-less "UTM ZONE 54 S" was assumed WGS84.
                        # crs_from_text handles EPSG stated outright,
                        # AMG/MGA zone + datum (the datum decides: GDA2020 +
                        # zone 54 is MGA 7854, not AMG 20254 — ~200 m apart),
                        # generic UTM + datum, and named national grids, with
                        # PREFIX matching because these blocks are hand-typed
                        # EBCDIC and the typos are permanent (MERCATOT).
                        if _crs_from_text is not None:
                            _epsg, _how, _note = _crs_from_text(txt)
                            if _epsg:
                                _epsg_hint = int(_epsg)
                    except Exception:
                        pass

                    # ── Inline / crossline range (3D only) ─────────
                    ilr = h.get("inline_range")
                    xlr = h.get("crossline_range")
                    if is_3d and ilr:
                        fields["il_min"] = int(ilr[0])
                        fields["il_max"] = int(ilr[1])
                    if is_3d and xlr:
                        fields["xl_min"] = int(xlr[0])
                        fields["xl_max"] = int(xlr[1])

                    # CDP points come back already coordinate-scalar-applied,
                    # IN TRACE ORDER. That order IS the line's shape, so it is
                    # preserved end to end (no set(), no sorting) — a 2D line
                    # becomes a LINESTRING below, not just a bbox.
                    xs, ys = [], []
                    for _px, _py in (h.get("cdp_points") or []):
                        if _px != 0 and _py != 0:
                            xs.append(_px)
                            ys.append(_py)

                    if xs and ys:
                        # ── Coordinate system detection ───────────────────────
                        # If all X values are in [-180, 180] and Y in [-90, 90]
                        # the coords are already geographic (WGS84 or similar).
                        # Otherwise they are projected (UTM, state plane, etc.)
                        # and need reprojection before storing as lat/lon.
                        _is_geo = (
                            all(-180 <= v <= 180 for v in xs) and
                            all(-90  <= v <= 90  for v in ys)
                        )

                        _tf = None        # projected -> WGS84 transformer
                        if _is_geo:
                            lons, lats = list(xs), list(ys)
                            fields["epsg_code"] = _epsg_hint or 4326
                        else:
                            # Projected coordinates — reproject ONLY with a CRS
                            # we actually know. There is NO zone inference: a
                            # UTM easting encodes distance from its own central
                            # meridian, not longitude, so the zone cannot be
                            # recovered from the coordinates alone.
                            #
                            # The previous version guessed:
                            #     approx_lon = (med_x - 500_000) / 111_320
                            #     zone = int((approx_lon + 180) / 6) + 1
                            # Easting is always 100k-900k, so approx_lon could
                            # only ever land in about -3.6..+3.6 and EVERY
                            # projected survey collapsed into UTM zones 29-32
                            # (western Europe). Australian Cooper Basin lines
                            # came out at ~63N off Norway, and one survey even
                            # split across 32630/32631 purely on whether a
                            # file's median easting fell below or above 500km.
                            # It also forced the northern zone via med_y >= 0,
                            # but southern UTM carries a 10,000,000 m false
                            # northing, so southern surveys are always positive
                            # and were always misread as northern.
                            #
                            # A confident wrong position is worse than none: it
                            # plots, so nobody checks it. With no known CRS we
                            # now emit no bbox at all and leave epsg_code NULL,
                            # which reads honestly as 'not georeferenced yet'.
                            # Set the DV_SEGY_EPSG environment variable (e.g.
                            # 32754 for WGS84/UTM 54S) to supply the CRS for a
                            # batch whose headers don't carry one.
                            lons, lats = [], []
                            _src_epsg = _epsg_hint or _segy_fallback_epsg()

                            if _src_epsg:
                                try:
                                    from pyproj import Transformer
                                    _tf = Transformer.from_crs(
                                        f"EPSG:{_src_epsg}", "EPSG:4326",
                                        always_xy=True)
                                    for _x, _y in zip(xs, ys):
                                        _lon, _lat = _tf.transform(_x, _y)
                                        if (-180 <= _lon <= 180 and
                                                -90 <= _lat <= 90):
                                            lons.append(_lon)
                                            lats.append(_lat)
                                    if lons and lats:
                                        fields["epsg_code"] = _src_epsg
                                    else:
                                        _tf = None
                                except Exception:
                                    # pyproj missing or transform failed. Do NOT
                                    # fall back to the raw projected values —
                                    # metres written into a lat/long column are
                                    # silently nonsense downstream.
                                    lons, lats = [], []
                                    _tf = None
                            # no CRS -> nothing emitted; bbox stays NULL

                        if lons and lats:
                            fields.update({
                                "bbox_min_lon": min(lons),
                                "bbox_max_lon": max(lons),
                                "bbox_min_lat": min(lats),
                                "bbox_max_lat": max(lats),
                            })

                            # ── Survey geometry (WKT, WGS84) ─────────────────
                            # Precedence: stated 3D corners -> trace-order
                            # LINESTRING (2D) -> convex hull (3D without
                            # corners). All land in survey_outline; promote
                            # converts to dv_seis_line.geog with
                            # geography::STGeomFromText(..., 4326).
                            _geom = None

                            # 1) A survey that STATES its corners is drawn as
                            #    its outline. Nine-ish samples along trace
                            #    ORDER zigzag across a 3D volume and draw a
                            #    scribble, so corners win outright.
                            #    survey_corners returns them RING-ordered
                            #    (anticlockwise about the centroid): tarata's
                            #    grid table traced literally is a bowtie —
                            #    17,670 m2 for a 222,642,000 m2 survey.
                            if _survey_corners is not None:
                                try:
                                    _cnrs = _survey_corners(txt)
                                except Exception:
                                    _cnrs = None
                                if _cnrs:
                                    _ring = []
                                    for _cx, _cy in _cnrs:
                                        if _is_geo:
                                            _lo, _la = _cx, _cy
                                        elif _tf is not None:
                                            _lo, _la = _tf.transform(_cx, _cy)
                                        else:
                                            continue
                                        if (-180 <= _lo <= 180 and
                                                -90 <= _la <= 90):
                                            _ring.append((_lo, _la))
                                    if len(_ring) >= 3:
                                        _ring.append(_ring[0])    # close ring
                                        _geom = ("POLYGON ((" + ", ".join(
                                            f"{_lo:.6f} {_la:.6f}"
                                            for _lo, _la in _ring) + "))")
                                        # The stated corners are the survey's
                                        # TRUE extent; the trace sample can
                                        # undercut it, so the bbox follows
                                        # the ring.
                                        fields.update({
                                            "bbox_min_lon": min(p[0] for p in _ring),
                                            "bbox_max_lon": max(p[0] for p in _ring),
                                            "bbox_min_lat": min(p[1] for p in _ring),
                                            "bbox_max_lat": max(p[1] for p in _ring),
                                        })

                            # 2) 2D: the sampled trace path, in trace order.
                            if _geom is None and not is_3d and len(lons) >= 2:
                                _geom = ("LINESTRING (" + ", ".join(
                                    f"{_lo:.6f} {_la:.6f}"
                                    for _lo, _la in zip(lons, lats)) + ")")

                            # 3) Fallback — convex hull (3D without stated
                            #    corners, or a 2D file with a single usable
                            #    point). Requires shapely; skip if absent.
                            if _geom is None:
                                try:
                                    from shapely.geometry import MultiPoint
                                    hull = MultiPoint(
                                        list(zip(lons, lats))).convex_hull
                                    if not hull.is_empty:
                                        _geom = hull.wkt
                                except Exception:
                                    pass

                            if _geom:
                                fields["survey_outline"] = _geom

                # ── NAVIGATION WINS OVER THE TRACE HEADERS ────────────────
                # Deliberate precedence, and it is the reverse of what the
                # code did before. Trace headers are best-effort: no CRS field
                # before Rev 2, byte positions vendors move at will (Teapot's
                # own load sheet puts CDP X/Y at 81-88 where the standard says
                # 181-188, so a conforming reader gets ZEROS), scalars applied
                # inconsistently. The NAV FILE is the authoritative geometry —
                # that is why it is delivered — and it states its own CRS
                # because it has to.
                #
                # Applied AFTER the trace-header block so a survey with no nav
                # keeps exactly today's behaviour, and one WITH nav gets the
                # surveyed positions instead of whatever byte 81 happened to
                # hold.
                try:
                    _nav = _nav_for(fpath)
                    if _nav:
                        from dataview.file_catalog.seis_nav import (
                            match_line as _match_line, linestring as _ls)
                        _stem = os.path.splitext(os.path.basename(fpath))[0]
                        _key = _match_line(_stem, _nav["lines"].keys())
                        # A 2D LINE takes its own line; anything the matcher
                        # cannot place takes the survey HULL rather than a
                        # guessed line — a real line drawn in the wrong place
                        # is worse than a footprint.
                        _wkt = (_ls(_nav["lines"][_key]) if _key
                                else _nav.get("hull"))
                        _pts = (_nav["lines"][_key] if _key
                                else [p for v in _nav["lines"].values()
                                      for p in v])

                        # THE HULL IS THE LAST RESORT, NOT THE SECOND CHOICE.
                        # A 3D volume filed beside a 2D survey matches no nav
                        # LINE, and the hull of those lines is a different
                        # survey's footprint — right basin, wrong shape, and
                        # confidently drawn. Teapot's filt_mig.sgy is exactly
                        # that: it STATES its own four corners (see
                        # crs_from_segy.survey_corners) and lacked only a CRS
                        # to place them. The nav file states the CRS. Its
                        # corners, in the nav's CRS, are this file's own
                        # answer; the hull never was.
                        if not _key and _survey_corners is not None \
                                and _nav.get("epsg"):
                            try:
                                _c3 = _survey_corners(txt)
                            except Exception:
                                _c3 = None
                            if _c3 and len(_c3) >= 3:
                                try:
                                    from pyproj import Transformer as _TF
                                    _t3 = _TF.from_crs(f"EPSG:{_nav['epsg']}",
                                                       "EPSG:4326",
                                                       always_xy=True)
                                    _r3 = [_t3.transform(_x, _y) for _x, _y in _c3]
                                    if all(-180 <= p[0] <= 180 and
                                           -90 <= p[1] <= 90 for p in _r3):
                                        _ring = _r3 + [_r3[0]]
                                        _wkt = ("POLYGON ((" + ", ".join(
                                            f"{_lo:.7f} {_la:.7f}"
                                            for _lo, _la in _ring) + "))")
                                        _pts = _r3
                                        _key = "(stated corners)"
                                except Exception:
                                    pass

                        if _wkt:
                            fields["survey_outline"] = _wkt
                            fields["epsg_code"] = 4326
                            _lo = [p[0] for p in _pts]; _la = [p[1] for p in _pts]
                            fields["bbox_min_lon"] = min(_lo)
                            fields["bbox_max_lon"] = max(_lo)
                            fields["bbox_min_lat"] = min(_la)
                            fields["bbox_max_lat"] = max(_la)
                            _d = fields.get("details") or {}
                            _d["nav_source"] = _nav["src"]
                            _d["nav_dir"] = _nav.get("src_dir")
                            _d["nav_levels_up"] = _nav.get("levels_up")
                            _d["nav_epsg"] = _nav["epsg"]
                            _d["nav_line"] = _key or "(survey hull)"
                            fields["details"] = _d
                except Exception:
                    pass

            except Exception:
                pass

        elif fext in P190_EXTS:
            # UKOOA P1/90 seismic navigation. Header records use fixed CODES
            # (H0100 survey area, H0102 vessel, H0103 source, H0200 date), NOT
            # free-text keywords. Data records start with 'S' (source centre) or
            # 'R' (receiver). Coordinates are commonly PROJECTED easting/northing
            # (UTM/grid), not lat/long — so we expose them as a projected bbox in
            # details rather than mislabelling them as lon/lat.
            # Verified against sample_2d.p190 / sample_3d.p190 (UKOOA P1/90).
            fields["file_category"] = "SEIS"
            fields["report_type"]   = "SEISMIC"
            try:
                import os as _os
                pts = []          # (easting, northing) grid pairs
                geo_pts = []      # (lon, lat) decimal degrees — the mappable pair
                survey = vessel = source = sdate = None
                with open(fpath, "r", errors="replace") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        rec = line[0].upper()
                        if rec == "H":
                            code = line[1:5]                 # e.g. '0100'
                            val  = line[5:].strip()
                            # strip a trailing lone counter/zero column
                            val  = re.sub(r"\s{2,}\d+(\s+\d+)*\s*$", "", val).strip()
                            # strip the leading label words (the UKOOA code's
                            # human label precedes the actual value), e.g.
                            # 'SURVEY AREA SOUTH CHINA SEA' -> 'SOUTH CHINA SEA',
                            # 'VESSEL DETAILS M.V.CONTRACTOR' -> 'M.V.CONTRACTOR'
                            for _lbl in ("SURVEY AREA", "VESSEL DETAILS",
                                         "SOURCE DETAILS", "STREAMER DETAILS",
                                         "SURVEY DATE"):
                                if val.upper().startswith(_lbl):
                                    val = val[len(_lbl):].strip()
                                    break
                            if code == "0100" and not survey:
                                survey = val[:255]
                            elif code == "0102" and not vessel:
                                vessel = val[:255]
                            elif code == "0103" and not source:
                                source = val[:255]
                            elif code == "0200" and not sdate:
                                sdate = val[:255]
                        elif rec in ("S", "G", "Q", "A", "T", "C", "V", "Z"):
                            # UKOOA P1/90 "Type 1" position record. These carry
                            # BOTH geographic and grid coordinates:
                            #   cols 26-35  Latitude   DDMMSS.SS + N/S
                            #   cols 36-46  Longitude  DDDMMSS.SS + E/W
                            #   cols 47-55  Easting    (grid)
                            #   cols 56-64  Northing   (grid)
                            # The lat/long is the important pair: it needs NO
                            # CRS, so a P190 is mappable even when nothing tells
                            # us its projection. Reading only the grid pair (the
                            # previous behaviour) left every P190 unpositioned.
                            #
                            # Record IDs "R" (3-D receiver group) and the closing
                            # "EOF" line are deliberately NOT handled here: R uses
                            # a different packed layout (group/E/N triples) with no
                            # lat/long, and "EOF" would otherwise look like record
                            # ID "E" (Echo Sounder).
                            if line[:3].upper() == "EOF":
                                continue
                            _la = _p190_latlon(line)
                            if _la:
                                geo_pts.append(_la)
                            e = n = None
                            try:
                                e = float(line[46:55]); n = float(line[55:64])
                            except Exception:
                                parts = line.split()
                                for i in range(len(parts) - 1):
                                    try:
                                        _e = float(parts[i]); _n = float(parts[i+1])
                                        if abs(_e) > 1000 or abs(_n) > 1000:
                                            e, n = _e, _n; break
                                    except ValueError:
                                        continue
                            if e is not None and n is not None and (e or n):
                                pts.append((e, n))

                if survey:
                    fields["survey_name"] = survey
                if vessel:
                    fields["contractor"] = vessel
                # 2D/3D: P1/90 doesn't encode it directly; infer from filename
                stem = _os.path.basename(fpath).lower()
                fields["seis_set_type"] = ("3D" if "3d" in stem
                                           else "2D" if "2d" in stem else None)
                det = {"survey_area": survey, "vessel": vessel,
                       "source": source, "survey_date": sdate,
                       "n_points": len(pts), "n_geo_points": len(geo_pts)}
                # Geographic pair FIRST — it needs no CRS, so it works even when
                # nothing declares the projection. Only fall through to the grid
                # pair below if a record carried no lat/long at all.
                if geo_pts:
                    _glo = [g[0] for g in geo_pts]
                    _gla = [g[1] for g in geo_pts]
                    fields.update({
                        "bbox_min_lon": min(_glo), "bbox_max_lon": max(_glo),
                        "bbox_min_lat": min(_gla), "bbox_max_lat": max(_gla),
                    })
                    fields["trace_count"] = fields.get("trace_count") or len(geo_pts)
                    # These lat/longs are on the survey's OWN geodetic datum
                    # (H1400/H1500 — e.g. Tokyo, ED50, NZGD49), not necessarily
                    # WGS84. The offset is up to a few hundred metres, which is
                    # immaterial for a survey-coverage outline and far better
                    # than leaving the survey unplaced. Tagged 4326 so downstream
                    # treats them as geographic; refine via H1500 if sub-100 m
                    # accuracy is ever needed here.
                    fields["epsg_code"] = 4326
                if pts:
                    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                    fields["trace_count"] = len(pts)
                    # Geographic only if values fall in lat/long ranges;
                    # otherwise they're projected E/N — keep in details, don't
                    # write bogus lon/lat.
                    _geo = (all(-180 <= v <= 180 for v in xs) and
                            all(-90  <= v <= 90  for v in ys))
                    if _geo and not geo_pts:
                        fields.update({
                            "bbox_min_lon": min(xs), "bbox_max_lon": max(xs),
                            "bbox_min_lat": min(ys), "bbox_max_lat": max(ys),
                        })
                    else:
                        det["projected_bbox"] = {
                            "min_e": min(xs), "max_e": max(xs),
                            "min_n": min(ys), "max_n": max(ys),
                        }
                fields["details"] = det
            except Exception:
                pass

        elif fext in SHP_EXTS:
            # Default category is set from the classifier below, NOT hardcoded to
            # SEIS. A shapefile is only "SEIS" if it's genuinely a seismic feature
            # type — otherwise a lease/field/boundary shapefile (which often has a
            # "survey" column meaning a LAND survey) would fabricate a bogus
            # seismic survey in FILE_SEIS_HEADER.
            fields["file_category"] = "UNKNOWN"
            fields["report_type"]   = "SHAPEFILE"
            try:
                from dataview.mapping.shapefile_catalog import classify_shapefile
                cl = classify_shapefile(fpath)
                fields["confidence"] = float(cl.get("confidence") or 0)
                if cl.get("crs_epsg"):
                    fields["epsg_code"] = cl["crs_epsg"]
                if cl.get("bounds"):
                    b = cl["bounds"]
                    fields.update({
                        "bbox_min_lon": b.get("minx"),
                        "bbox_max_lon": b.get("maxx"),
                        "bbox_min_lat": b.get("miny"),
                        "bbox_max_lat": b.get("maxy"),
                    })
                # Pull sample values from DBF attribute extraction
                sd = cl.get("sample_data", {})
                if sd.get("sample_uwis"):
                    fields["uwi"] = sd["sample_uwis"][0]
                if sd.get("sample_well_names"):
                    fields["well_name"] = sd["sample_well_names"][0]
                if sd.get("top_operators"):
                    fields["operator"] = sd["top_operators"][0]
                if sd.get("sample_fields"):
                    fields["well_field"] = sd["sample_fields"][0]
                if sd.get("sample_surveys"):
                    fields["survey_name"] = sd["sample_surveys"][0]
                # Map feature_type → file_category. Only genuine seismic feature
                # types become SEIS (→ FILE_SEIS_HEADER). WELL → WELL. Everything
                # else routes to its own spatial table (field/lease/boundary/
                # pipeline) and carries its footprint WKT in spatial_outline so
                # promote can build a geography column. _shp_outline_wkt reorients
                # inverted rings (shapefiles are commonly CW; geography wants CCW)
                # so the WKT yields a valid, correctly-sized geography — not the
                # whole-Earth complement a raw CW polygon produces.
                ft = cl.get("feature_type", "")
                # map classifier feature_type -> file_category for routing
                _FT_CATEGORY = {
                    "WELL":       "WELL",
                    "SEISMIC_2D": "SEIS",
                    "SEISMIC_3D": "SEIS",
                    "FIELD":      "FIELD",
                    "LEASE":      "LAND_TRACT",
                    "BOUNDARY":   "BOUNDARY",
                    "PIPELINE":   "PIPELINE",
                }
                cat = _FT_CATEGORY.get(ft, "SPATIAL")
                fields["file_category"] = cat
                if cat == "SEIS":
                    _wkt = _shp_outline_wkt(fpath)
                    if _wkt:
                        fields["survey_outline"] = _wkt   # seismic's own slot
                elif cat in ("FIELD", "LAND_TRACT", "BOUNDARY", "PIPELINE"):
                    _wkt = _shp_outline_wkt(fpath)
                    if _wkt:
                        fields["spatial_outline"] = _wkt  # generic spatial slot
                # canonical details block — the spatial metadata that defines a
                # shapefile (geometry, feature count, CRS, attribute columns).
                fields["details"] = {
                    "feature_type":  ft or None,
                    "feature_count": cl.get("feature_count"),
                    "geometry_type": cl.get("geometry_type"),
                    "crs_epsg":      cl.get("crs_epsg"),
                    "attributes":    cl.get("attributes", []),
                    "bounds":        cl.get("bounds"),
                }
            except Exception:
                pass

        elif fext in CSV_EXTS:
            # Opt-in only: CSV/TSV are never in the default scan set (ALL_EXTS),
            # so a file reaches here only when '.csv'/'.tsv' was hand-entered in
            # the Formats-to-scan box. Dedicated delimited-table extractor —
            # NOT the Office/Excel summarizer, which yields nothing on raw CSV.
            #
            # The import is tried SEPARATELY from the parse: a missing module
            # (csv_catalog deployed to ROOT instead of modules\, or stale
            # __pycache__) is a deploy error, not a per-file parse error — it
            # must surface as a visible issue, not silently leave file_category
            # at 'UNKNOWN' (which skips the FILE_WELL_HEADER write entirely).
            _classify_csv = None
            try:
                from dataview.file_catalog.csv_catalog import classify_csv as _classify_csv
            except Exception as _imp_e:
                fields["report_type"]  = "CSV_NOLOADER"
                fields["extract_error"] = (
                    "csv_catalog not importable — deploy to modules\\, not "
                    f"ROOT; clear __pycache__ ({type(_imp_e).__name__})")
            if _classify_csv is not None:
                try:
                    cl = _classify_csv(fpath)
                    fields["file_category"] = cl.get("file_category", "OTHER")
                    fields["report_type"]   = cl.get("report_type", "CSV")
                    fields.update({
                        "uwi":         cl.get("uwi"),      # raw; writer bare-14s it
                        "well_name":   cl.get("well_name"),
                        "operator":    cl.get("operator"),
                        "well_field":  cl.get("well_field"),
                        "state":       cl.get("state"),
                        "county":      cl.get("county"),
                        "latitude":    cl.get("latitude"),
                        "longitude":   cl.get("longitude"),
                        "total_depth": cl.get("total_depth"),
                        "spud_date":   cl.get("spud_date"),
                        "confidence":  float(cl.get("confidence") or 0),
                    })
                except Exception as _csv_e:
                    fields["extract_error"] = (
                        f"csv parse failed: {type(_csv_e).__name__}: {_csv_e}")[:200]

        elif fext in OFFICE_EXTS:
            fields["file_category"] = "WELL"
            fields["report_type"]   = "OFFICE"
            try:
                from dataview.file_catalog.file_summarizer import summarize
                s = summarize(fpath)
                fields.update({
                    "uwi":        s.get("uwi"),
                    "well_name":  s.get("well_name"),
                    "operator":   s.get("key_fields", {}).get("operator") or
                                  s.get("key_fields", {}).get("company"),
                    "well_field": s.get("key_fields", {}).get("field"),
                    "confidence": float(
                        s.get("key_fields", {}).get("confidence") or 0),
                })
                # Pull report/doc type — check sheet_detail for known schema
                # names (BOEM_BOREHOLE, KGS_WELL etc.) first, then fall back
                # to generic table_type / doc_type.
                _sheet_detail = s.get("key_fields", {}).get("sheet_detail", [])
                _schema = (_sheet_detail[0].get("table_type")
                           if _sheet_detail else None)
                rt = (_schema or
                      s.get("key_fields", {}).get("report_type") or
                      s.get("key_fields", {}).get("doc_type") or
                      s.get("key_fields", {}).get("table_type"))
                if rt and rt not in ("UNKNOWN", "OTHER"):
                    fields["report_type"] = str(rt)[:50]
            except Exception:
                pass

        elif fext in WITSML_EXTS:
            # WITSML 1.3.1 / 1.4.1 — trajectory, log, mudLog, well, wellbore.
            # Gate: only process files that declare the WITSML namespace to
            # avoid parsing unrelated XML (config files, SVG, RSS, etc.).
            try:
                # Cheap namespace check — read first 500 bytes only.
                _witsml_sig = b"witsml.org/schemas"
                with open(fpath, "rb") as _wf:
                    _head = _wf.read(500)
                if _witsml_sig not in _head:
                    fields["file_category"] = "OTHER"
                    fields["report_type"]   = "XML_OTHER"
                else:
                    from dataview.file_catalog.witsml_catalog import classify_witsml
                    cl = classify_witsml(fpath)
                    fields["file_category"] = cl.get("file_category", "WELL")
                    fields["report_type"]   = cl.get("report_type", "WITSML")
                    fields.update({
                        "uwi":        cl.get("uwi"),
                        "well_name":  cl.get("well_name"),
                        "operator":   cl.get("operator"),
                        "contractor": cl.get("contractor"),
                        "well_field": cl.get("well_field"),
                        "state":      cl.get("state"),
                        "county":     cl.get("county"),
                        "spud_date":  cl.get("spud_date"),
                        "total_depth":cl.get("total_depth"),
                        "confidence": float(cl.get("confidence") or 0),
                    })
                    # Curve names for log objects
                    if cl.get("curve_names"):
                        fields["curve_names"] = cl["curve_names"]
                        fields["n_curves"]    = cl.get("n_curves", 0)

                    # Identity fallback: classify_witsml can return null
                    # identity for object types like trajectory, where the well
                    # name lives in <nameWell> and the well ref in the uidWell
                    # attribute. Read them directly when the classifier missed.
                    if not fields.get("well_name") or not fields.get("uwi"):
                        try:
                            _txt = open(fpath, "r", encoding="utf-8",
                                        errors="replace").read(20000)
                            if not fields.get("well_name"):
                                _m = re.search(
                                    r"<nameWell>\s*([^<]+?)\s*</nameWell>",
                                    _txt, re.IGNORECASE)
                                if _m:
                                    fields["well_name"] = _m.group(1).strip()
                            if not fields.get("uwi"):
                                _m = re.search(r'uidWell\s*=\s*"([^"]+)"',
                                               _txt, re.IGNORECASE)
                                if _m:
                                    fields["uwi"] = _m.group(1).strip()
                        except Exception:
                            pass
            except Exception:
                pass

        elif fext in JSON_LOG_EXTS:
            # OSDU WellLog / Well / WellboreMarkerSet / PressureData /
            # SeismicAcquisitionSurvey and JSON Well Log Format (JSONWLF).
            # Gate: only process files that look like petroleum JSON to
            # avoid parsing unrelated JSON (config, GeoJSON already handled
            # by SHP_EXTS as .geojson, package.json, etc.).
            try:
                import json as _json
                with open(fpath, "r", encoding="utf-8-sig",
                          errors="replace") as _jf:
                    _head_text = _jf.read(512)
                # Must have either an OSDU 'kind' field or known JSONWLF keys
                _looks_petroleum = (
                    '"kind"' in _head_text or
                    '"header"' in _head_text or
                    '"WellLog"' in _head_text or
                    '"wellbore"' in _head_text.lower()
                )
                if not _looks_petroleum:
                    fields["file_category"] = "OTHER"
                    fields["report_type"]   = "JSON_OTHER"
                else:
                    from dataview.file_catalog.json_well_log_catalog import classify_json_well_log
                    cl = classify_json_well_log(fpath)
                    fields["file_category"] = cl.get("file_category", "WELL")
                    fields["report_type"]   = cl.get("report_type", "JSON_LOG")
                    fields.update({
                        "uwi":        cl.get("uwi"),
                        "well_name":  cl.get("well_name"),
                        "operator":   cl.get("operator"),
                        "contractor": cl.get("contractor"),
                        "well_field": cl.get("well_field"),
                        "state":      cl.get("state"),
                        "county":     cl.get("county"),
                        "spud_date":  cl.get("spud_date"),
                        "total_depth":cl.get("total_depth"),
                        "confidence": float(cl.get("confidence") or 0),
                    })
                    # Seismic surveys — route bbox to seis fields
                    if cl.get("file_category") == "SEIS":
                        fields.update({
                            "survey_name":  cl.get("survey_name"),
                            "seis_set_type":cl.get("seis_set_type"),
                            "bbox_min_lat": cl.get("bbox_min_lat"),
                            "bbox_max_lat": cl.get("bbox_max_lat"),
                            "bbox_min_lon": cl.get("bbox_min_lon"),
                            "bbox_max_lon": cl.get("bbox_max_lon"),
                            "epsg_code":    cl.get("epsg_code"),
                        })
                    # Curve names for log objects
                    if cl.get("curve_names"):
                        fields["curve_names"] = cl["curve_names"]
                        fields["n_curves"]    = cl.get("n_curves", 0)
            except Exception:
                pass

    except Exception:
        pass

    # Normalize the UWI to bare digits in ONE place, so every format's UWI is
    # consistent with the bare-14 keys used across the system (dv_well, gold,
    # scout resolution). Source files carry dashed/spaced UWIs; strip them here.
    if fields.get("uwi"):
        fields["uwi"] = _normalize_uwi(fields["uwi"])

    # Clean None/"None"/empty strings
    return {k: (v if v is not None and
                str(v).strip() not in ("","None","nan") else None)
            for k, v in fields.items()}


# =============================================================================
# Capture path — MOVED HERE FROM page_workbench 16 Aug 2026
# =============================================================================
# These are the symbols pipeline_run needed, and it used to reach into
# page_workbench for them with five lazy `import page_workbench` calls written
# specifically to keep streamlit out of the CLI and the detached child. That is
# the dependency backwards: the engine importing the UI. This module already
# exists to be the streamlit-free half (see the module docstring), so they live
# here and page_workbench imports them back.
#
# _do_extract takes an optional `log` because its only streamlit call was
# st.error; the page passes st.error, headless callers get stderr.


def _default_log(msg):
    """Fallback sink so a headless extraction error is never swallowed."""
    print(msg, file=sys.stderr)


EXT_GROUP = {}
for e in PDF_EXTS:      EXT_GROUP[e] = "PDF"
for e in LOG_EXTS:      EXT_GROUP[e] = "Well Log"
for e in SEGY_EXTS:     EXT_GROUP[e] = "Seismic"
for e in P190_EXTS:     EXT_GROUP[e] = "Seismic"
for e in SHP_EXTS:      EXT_GROUP[e] = "Shapefile"
for e in OFFICE_EXTS:   EXT_GROUP[e] = "Office"
for e in CSV_EXTS:      EXT_GROUP[e] = "CSV / Table"
for e in IMAGE_EXTS:    EXT_GROUP[e] = "Image"
for e in WITSML_EXTS:   EXT_GROUP[e] = "WITSML"
for e in JSON_LOG_EXTS: EXT_GROUP[e] = "OSDU / JSON Well Log"


# Extensions whose loader parses the file itself (it resolves the well +
# INVENTORY_ID internally and writes cat_* via capture()), so _do_extract
# returns nothing for them and the capture loops must NOT skip them on an
# empty row list. Office is wired today; WITSML / OSDU join as their loaders
# land in _load_rows_to_catalog.
# Extensions whose capture path (_load_rows_to_catalog) re-parses the file
# itself and does NOT depend on the pre-extracted `rows` arg. The pipeline's
# capture stage skips a file when _do_extract yields no rows UNLESS its ext is
# here — so self-parsing formats must be listed or they silently no-op.
# PDF belongs here: scout/EOW/well-test tickets carry header/section data, not
# the tabular "rows" a directional survey yields, so _do_extract returns empty
# for them; _load_rows_to_catalog re-classifies and extracts internally. Before
# PDF was added, only directional surveys (which DO yield station rows) passed
# the gate — hence "7 of 40 cataloged".
SELF_PARSING_EXTS = (set(OFFICE_EXTS) | set(WITSML_EXTS)
                     | set(JSON_LOG_EXTS) | set(LAS_EXTS)
                     | set(SHP_EXTS)
                     | set(PDF_EXTS))


def _norm_uwi(v):
    """Canonicalize any UWI/API string to bare-14 before it is written to the
    catalog. Reuses path_identity.norm_uwi14 (the same recipe the manual-review
    and FK paths use) so a display-formatted API from a PDF — e.g. a scout
    ticket's '42-999-00001-00-00' — collapses to '42999000010000' and matches
    dv_well at the FK gate instead of false-failing as an unmatched UWI.

    Falls back to a digit-strip if path_identity is unavailable; returns None
    for empty input so the 'extracted - no UWI' path is preserved.
    """
    if v is None or str(v).strip() == "":
        return None
    try:
        from dataview.core import path_identity as _pi
        u = _pi.norm_uwi14(str(v))
        if u:
            return u
    except Exception:
        pass
    d = re.sub(r"\D", "", str(v))
    return d if len(d) == 14 else (d or None)


def _safe_num(v):
    """Convert to float or None. Silently swallows bad input."""
    try:
        return float(str(v).replace(",","").strip()) if v is not None else None
    except (ValueError, TypeError):
        return None


def _safe_coord(v):
    """Latitude or longitude. Returns float in [-180, 180] or None."""
    n = _safe_num(v)
    if n is None or not (-180.0 <= n <= 180.0):
        return None
    return n


def _set_readiness_cataloged(engine, inv_id):
    """DEPRECATED no-op. Readiness on the catalog/promote axis is now DERIVED
    from row reality by catalog_readiness.reconcile_readiness — the single owner
    — not asserted in-flow. This used to optimistically stamp CATALOGED before
    the cat_* rows were proven to persist, which let a row-less capture mark a
    file done so promote had nothing to lift (the stranded-file bug). Kept as a
    stub so existing call sites compile; it intentionally writes nothing. The
    capture and promote stages call reconcile_readiness at end of pass."""
    return


def _do_extract(fpath: str, fext: str, log=None) -> tuple:
    """Extract structured data rows. Returns (rows, label)."""
    try:
        if fext == ".pdf":
            from dataview.file_catalog.pdf_survey_catalog import (
                classify_pdf, extract_stations,
                extract_eowr, extract_rft_data,
                extract_well_test, extract_petrophysical,
                extract_casing_cement, extract_ddr,
                extract_scout_ticket,
                RT_DIRECTIONAL, RT_EOWR, RT_FORMATION, RT_RFT,
                RT_WELL_TEST, RT_PETRO, RT_CASING,
                RT_DDR, RT_SCOUT,
            )
            # extract_core / RT_CORE are not present in all builds — guard them
            # so a missing symbol can't break the whole PDF preview path.
            try:
                from dataview.file_catalog.pdf_survey_catalog import extract_core, RT_CORE
            except ImportError:
                extract_core, RT_CORE = None, "CORE_ANALYSIS"
            cl = classify_pdf(fpath)
            rt = cl.get("report_type","UNKNOWN")

            if rt == RT_DIRECTIONAL:
                r = extract_stations(fpath)
                return r.get("stations",[]), "Stations"
            elif rt in (RT_EOWR, RT_FORMATION):
                r = extract_eowr(fpath)
                return r.get("strat",[]), "Strat tops"
            elif rt == RT_RFT:
                return extract_rft_data(fpath).get("rows",[]), "RFT rows"
            elif rt == RT_WELL_TEST:
                return extract_well_test(fpath).get("flow_rows",[]), "Flow periods"
            elif rt in (RT_PETRO,"PETROPHYSICAL"):
                from dataview.file_catalog.extract_petro import extract_petro
                r = extract_petro(fpath)
                if r.get("ok"):
                    zones = r.get("zones", [])
                    return zones, f"Petro zones ({len(zones)})"
                else:
                    # Fallback to old extractor
                    r2 = extract_petrophysical(fpath)
                    return r2.get("zones") or r2.get("interval") or [], "Zones"
            elif rt == RT_CASING:
                r = extract_casing_cement(fpath)
                return r.get("casing",[]) + r.get("cement",[]), "Casing"
            elif rt == RT_CORE:
                if extract_core:
                    return extract_core(fpath).get("samples",[]), "Core samples"
                return [], "Core (no extractor)"
            elif rt == RT_DDR:
                return extract_ddr(fpath).get("ops",[]), "Operations"
            elif rt == RT_SCOUT:
                sc = extract_scout_ticket(fpath)
                summary = []
                if sc.get("header"):
                    summary.append({"Section": "Well header",
                                    "Items": len(sc["header"])})
                for _k, _lbl in (("tops", "Formation tops"),
                                 ("dst", "DST tests"),
                                 ("frac", "Frac stages"),
                                 ("core", "Core samples"),
                                 ("ip_rows", "IP / production")):
                    _n = len(sc.get(_k) or [])
                    if _n:
                        summary.append({"Section": _lbl, "Items": _n})
                return summary, "Scout sections"
            else:
                return [], "Records"

        elif fext == ".las":
            import lasio
            las = lasio.read(fpath)
            df  = las.df().reset_index()
            return df.to_dict("records"), "Curve rows"

        elif fext in SEGY_EXTS:
            from dataview.file_catalog.segy_header import sample_trace_rows
            return sample_trace_rows(fpath, limit=100), "Trace headers"

        elif fext in SHP_EXTS:
            import geopandas as gpd
            gdf = gpd.read_file(fpath)
            return gdf.drop(
                columns=["geometry"], errors="ignore"
            ).to_dict("records"), "Features"

    except Exception as e:
        # NEVER SILENT. page_workbench passes st.error so the page still
        # shows it; headless callers get it on stderr. A swallowed
        # extraction error made a broken parser look like an empty file.
        (log or _default_log)(f"Extraction error: {e}")
    return [], "Records"


def _load_rows_to_catalog(engine, dialect, fpath, fext, uwi, rows):
    """Capture extracted rows into the file_catalog.cat_* mirrors.

    Pure logic — performs NO Streamlit output, so the same path drives both
    the interactive viewer and the batch loader. Returns:
        {ok, loaded, errors:[...], rt, note}
    note is one of "", "petro_fail:<msg>", "not_impl:<rt>",
    "shapefile", "unsupported".  header-capture problems are recorded in
    errors with a "header capture: " prefix (non-fatal).
    """
    from sqlalchemy import text as _t
    res = {"ok": False, "loaded": 0, "errors": [], "rt": "", "note": "",
           "detail": {}}

    well_info = {"uwi": uwi, "well_name": "", "operator": "",
                 "source_path": fpath}
    # resolve INVENTORY_ID for provenance (the cat_* mirrors record it)
    try:
        with engine.connect() as _c:
            _r = _c.execute(_t(
                "SELECT TOP 1 INVENTORY_ID FROM file_catalog.GLOBAL_FILE_CATALOG "
                "WHERE FILE_PATH = :p"), {"p": fpath}).fetchone()
        well_info["inventory_id"] = _r[0] if _r else None
    except Exception:
        well_info["inventory_id"] = None

    try:
        if fext == ".pdf":
            from dataview.file_catalog.pdf_survey_catalog import (
                classify_pdf, load_to_ppdm, RT_DIRECTIONAL, RT_SCOUT)
            # Newer symbols — tolerate an older deployed pdf_survey_catalog by
            # falling back to the known string values if the constants/function
            # aren't exported yet (prevents a partial deploy from breaking ALL
            # PDF cataloging on an ImportError).
            try:
                from dataview.file_catalog.pdf_survey_catalog import (
                    extended_classify_pdf, RT_EOWR as _RT_EOWR,
                    RT_WELL_TEST as _RT_WT, RT_DDR as _RT_DDR,
                    RT_RFT as _RT_RFT, RT_CASING as _RT_CAS)
                RT_EOWR, RT_WELL_TEST = _RT_EOWR, _RT_WT
                RT_DDR, RT_RFT, RT_CASING = _RT_DDR, _RT_RFT, _RT_CAS
            except ImportError:
                extended_classify_pdf = None
                RT_EOWR, RT_WELL_TEST = "END_OF_WELL", "WELL_TEST"
                RT_DDR = "DAILY_DRILLING_REPORT"
                RT_RFT, RT_CASING = "RFT_MDT", "CASING_CEMENTING"
            cl = classify_pdf(fpath)
            rt = cl.get("report_type", "UNKNOWN")
            # The base classifier only knows 5 types and mis-routes the rest:
            # scout tickets fall under its COMPLETION keyword, EOW reports under
            # its survey keywords. Consult the extended classifier, which detects
            # scout / EOW / well-test / DDR / RFT / casing, and let it OVERRIDE
            # for those documents (a real directional survey still wins, since
            # the extended classifier won't claim it). Merge well-header fields
            # from whichever classifier found them.
            try:
                ex = extended_classify_pdf(fpath) if extended_classify_pdf else {}
                ex_rt = ex.get("report_type", "UNKNOWN")
                _EXT_TYPES = {RT_SCOUT, RT_EOWR, RT_WELL_TEST,
                              RT_DDR, RT_RFT, RT_CASING}
                # EOW reports carry survey-like depth/TVD tables, so the base
                # classifier often mislabels them DIRECTIONAL. A confident EOW
                # detection (≥0.5) overrides even DIRECTIONAL; the other extended
                # types only override non-DIRECTIONAL base results.
                if (ex_rt == RT_EOWR and ex.get("confidence", 0) >= 0.5):
                    rt = RT_EOWR
                elif ex_rt in _EXT_TYPES and rt != RT_DIRECTIONAL:
                    rt = ex_rt
                if rt == ex_rt:
                    for _k in ("well_name", "operator", "field", "state",
                               "county", "country", "total_depth",
                               "latitude", "longitude"):
                        if not cl.get(_k) and ex.get(_k):
                            cl[_k] = ex.get(_k)
            except Exception:
                pass
            res["rt"] = rt
            well_info.update({
                "well_name": cl.get("well_name", ""),
                "operator":  cl.get("operator", ""),
            })
            # capture the well header into cat_well so promote_catalog can
            # create the dv_well record downstream from the document header.
            # Scout tickets are skipped here — load_scout writes a richer
            # cat_well header (county, lease, lat/long, dates) itself.
            if uwi and rt != RT_SCOUT:
                try:
                    import uuid as _uuid
                    from datetime import datetime as _dt
                    try:
                        from dataview.file_catalog.catalog_capture import capture as _cap
                    except ImportError:
                        from dataview.file_catalog.catalog_capture import capture as _cap
                    _now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
                    _hn = _cap(engine, "cat_well", [{
                        "WELL_NAME":        cl.get("well_name") or uwi,
                        "OPERATOR_NAME":    cl.get("operator"),
                        "FIELD_NAME":       cl.get("field"),
                        "PROVINCE_STATE":   cl.get("state"),
                        "COUNTY":           cl.get("county"),
                        "COUNTRY":          cl.get("country"),
                        "SURFACE_LATITUDE":  _safe_coord(cl.get("latitude")),
                        "SURFACE_LONGITUDE": _safe_coord(cl.get("longitude")),
                        "FINAL_TD":         cl.get("total_depth"),
                        "ACTIVE_IND":       "Y",
                        "ROW_QUALITY":      "FINAL",
                        "PPDM_GUID":        str(_uuid.uuid4()),
                        "ROW_CREATED_BY":   "DataWrangler",
                        "ROW_CREATED_DATE": _now,
                    }], uwi=uwi, inventory_id=well_info.get("inventory_id"),
                       source_path=fpath, source="PDF_HEADER")
                    if _hn:
                        res["detail"]["cat_well"] = (
                            res["detail"].get("cat_well", 0) + _hn)
                except Exception as _he:
                    res["errors"].append(f"header capture: {_he}")

            if rt == RT_DIRECTIONAL:
                r = load_to_ppdm(well_info=well_info, stations=rows,
                                 engine=engine, dialect=dialect)
            else:
                from dataview.file_catalog.pdf_db_loader import (
                    load_formation_tops, load_casing, load_scout, load_core,
                )
                # load_well_test / load_rft are newer; if a deployed
                # pdf_db_loader predates them, degrade gracefully (header-only
                # capture still marks the file cataloged) instead of breaking
                # the entire PDF branch on an ImportError.
                try:
                    from dataview.file_catalog.pdf_db_loader import load_well_test
                except ImportError:
                    load_well_test = None
                try:
                    from dataview.file_catalog.pdf_db_loader import load_rft
                except ImportError:
                    load_rft = None
                # RT_EOWR/RT_WELL_TEST/RT_DDR/RT_RFT/RT_CASING are already in
                # scope from the defensive block at the top of this branch.
                # Resolve the remaining ones with literal fallbacks so a missing
                # constant in an older pdf_survey_catalog can't ImportError and
                # kill the whole capture (this was the RT_CORE failure).
                try:
                    from dataview.file_catalog.pdf_survey_catalog import (
                        RT_FORMATION as _RT_FORM, RT_PETRO as _RT_PET,
                        RT_CORE as _RT_CORE, RT_SCOUT as _RT_SCOUT)
                    RT_FORMATION, RT_PETRO = _RT_FORM, _RT_PET
                    RT_CORE = _RT_CORE
                except ImportError:
                    RT_FORMATION, RT_PETRO = "FORMATION_TOPS", "PETROPHYSICAL"
                    RT_CORE = "CORE_ANALYSIS"
                kw = dict(engine=engine, dialect=dialect,
                          well_info=well_info, rows=rows)
                if rt in (RT_EOWR, RT_FORMATION):
                    r = load_formation_tops(**kw)
                elif rt == RT_CASING:
                    r = load_casing(**kw)
                elif rt == RT_CORE:
                    r = load_core(**kw)
                elif rt == RT_SCOUT:
                    r = load_scout(**kw)
                elif rt == RT_WELL_TEST and load_well_test:
                    r = load_well_test(**kw)
                elif rt == RT_RFT and load_rft:
                    r = load_rft(**kw)
                elif rt in (RT_PETRO, "PETROPHYSICAL"):
                    from dataview.file_catalog.extract_petro import (
                        extract_petro, load_petro_zones)
                    petro = extract_petro(fpath)
                    if not petro.get("ok"):
                        res["note"] = f"petro_fail:{petro.get('error')}"
                        # header may still have been captured above → mark it
                        if res["detail"].get("cat_well"):
                            res["ok"] = True
                            _set_readiness_cataloged(
                                engine, well_info.get("inventory_id"))
                        return res
                    r = load_petro_zones(engine, dialect, petro, uwi)
                else:
                    # No detail loader for this type (e.g. DDR) — but if we
                    # captured the well header above, that IS a catalog result.
                    res["note"] = f"not_impl:{rt}"
                    if res["detail"].get("cat_well"):
                        res["ok"] = True
                        _set_readiness_cataloged(
                            engine, well_info.get("inventory_id"))
                    return res

            res["loaded"] = r.get("loaded", 0)
            for _k, _v in (r.get("detail") or {}).items():
                res["detail"][_k] = res["detail"].get(_k, 0) + _v
            res["errors"].extend(r.get("errors", []))
            res["ok"] = not [e for e in res["errors"]
                             if not str(e).startswith("header capture:")]
            # Mark cataloged if EITHER detail rows OR a well header landed.
            if res["loaded"] or res["detail"].get("cat_well"):
                _set_readiness_cataloged(engine, well_info.get("inventory_id"))
            return res

        elif fext in SHP_EXTS:
            # Classify first (gives feature_type + column_map), then route WELL
            # point features through the CATALOG-aware loader so rows land in
            # file_catalog.cat_well keyed by INVENTORY_ID — promote_catalog lifts
            # them into dataview.dv_well. (load_to_ppdm writes dbo.WELL directly
            # and bypasses the cat_* → dv_* pipeline, so it is NOT used here.)
            from dataview.mapping.shapefile_catalog import (
                classify_shapefile, capture_wells_to_catalog,
                capture_features_to_catalog, FT_WELL)
            cl = classify_shapefile(fpath)
            ft = cl.get("feature_type")
            _FEAT_CAT = {"FIELD": "FIELD", "LEASE": "LAND_TRACT",
                         "BOUNDARY": "BOUNDARY", "PIPELINE": "PIPELINE"}
            if ft == FT_WELL:
                r = capture_wells_to_catalog(
                    file_path=fpath, column_map=cl.get("column_map") or {},
                    engine=engine, well_info=well_info,
                    dialect=dialect, source="SHAPEFILE")
                res["loaded"] = r.get("loaded", 0)
                res["detail"]["cat_well"] = (
                    res["detail"].get("cat_well", 0) + r.get("loaded", 0))
                res["errors"].extend(r.get("errors", []))
                res["ok"] = not [e for e in res["errors"]
                                 if not str(e).startswith("header capture:")]
                res["note"] = "shapefile"
                if res["loaded"]:
                    _set_readiness_cataloged(engine,
                                             well_info.get("inventory_id"))
            elif ft in _FEAT_CAT:
                # FIELD/LEASE/BOUNDARY/PIPELINE — per-feature capture, one cat_*
                # row per polygon/line with geometry; promote_* builds geography.
                r = capture_features_to_catalog(
                    file_path=fpath, feature_category=_FEAT_CAT[ft],
                    engine=engine, well_info=well_info,
                    dialect=dialect, source="SHAPEFILE")
                n = r.get("loaded", 0)
                res["loaded"] = n
                res["detail"].update(r.get("detail", {}))
                res["errors"].extend(r.get("errors", []))
                res["ok"] = not [e for e in res["errors"]
                                 if not str(e).startswith("header capture:")]
                res["note"] = "shapefile"
                if n:
                    _set_readiness_cataloged(engine,
                                             well_info.get("inventory_id"))
            else:
                # SEISMIC handled via header→dv_seis_set; anything else skips.
                res["ok"] = True
                res["note"] = f"shapefile_skip:{ft}"
            return res

        elif fext in WITSML_EXTS:
            # WITSML self-parses (well header / trajectory / log curves), so the
            # pre-extracted `rows` arg is unused. load_witsml stamps INVENTORY_ID
            # / SOURCE_PATH via capture(), and gates non-WITSML .xml itself.
            try:
                from dataview.file_catalog.witsml_catalog import load_witsml as _witsml
            except ImportError:
                from dataview.file_catalog.witsml_catalog import load_witsml as _witsml
            _w = _witsml(engine, fpath, uwi=uwi,
                         inventory_id=well_info.get("inventory_id"),
                         source_path=fpath, well_info=well_info)
            res["loaded"] = int(_w.get("loaded", 0) or 0)
            res["rt"] = _w.get("rt") or "WITSML"
            res["detail"].update(_w.get("detail") or {})
            res["note"] = _w.get("note", "")
            res["errors"].extend(_w.get("errors") or [])
            if res["loaded"]:
                res["ok"] = True
                _set_readiness_cataloged(engine, well_info.get("inventory_id"))
            return res

        elif fext in JSON_LOG_EXTS:
            # OSDU WKS / JSON-Well-Log self-parses (well header, trajectory,
            # markers, DST, SCAL, log curves), routing by the `kind` field.
            # Non-well kinds (Field/Reservoir/Seismic*) return a no_target note.
            try:
                from dataview.file_catalog.json_well_log_catalog import load_json_well_log as _jwl
            except ImportError:
                from dataview.file_catalog.json_well_log_catalog import load_json_well_log as _jwl
            _j = _jwl(engine, fpath, uwi=uwi,
                      inventory_id=well_info.get("inventory_id"),
                      source_path=fpath, well_info=well_info)
            res["loaded"] = int(_j.get("loaded", 0) or 0)
            res["rt"] = _j.get("rt") or "OSDU"
            res["detail"].update(_j.get("detail") or {})
            res["note"] = _j.get("note", "")
            res["errors"].extend(_j.get("errors") or [])
            if res["loaded"]:
                res["ok"] = True
                _set_readiness_cataloged(engine, well_info.get("inventory_id"))
            return res

        elif fext in OFFICE_EXTS:
            # Office docs parse themselves inside dv_office_loader (it resolves
            # the well + INVENTORY_ID from the file), so the pre-extracted
            # `rows` arg is unused here. dispatch() routes xlsx/docx to the
            # formation-tops / completion / production sub-loader by filename
            # + hint, and each sub-loader stamps INVENTORY_ID/SOURCE_PATH via
            # capture() — same provenance the other branches carry.
            try:
                from dataview.file_catalog.dv_office_loader import dispatch as _office
            except ImportError:
                from dataview.file_catalog.dv_office_loader import dispatch as _office
            _r = _office(engine, fpath, source="OFFICE")
            res["loaded"] = int(_r.get("loaded", 0) or 0)
            res["rt"] = "OFFICE"
            _errs = [str(e) for e in (_r.get("errors") or [])]
            if res["loaded"]:
                res["ok"] = True
                res["detail"]["office"] = res["loaded"]
                _set_readiness_cataloged(engine, well_info.get("inventory_id"))
            elif any("No loader found" in e for e in _errs):
                # recognised office type but no matching sub-loader for this
                # file — report as ⚠ no-loader, not a hard error
                res["note"] = "not_impl:OFFICE"
            else:
                res["errors"].extend(_errs)
            return res

        elif fext in LAS_EXTS:
            # LAS self-parses here (one lasio.read → well header + per-curve
            # metadata), so the pre-extracted `rows` arg is unused. We capture
            # the WELL HEADER into cat_well (so promote can build dv_well) and
            # one row per curve into cat_log_curve — curve *metadata* only
            # (mnemonic / unit / description / API code / depth frame / sample
            # count), never the bulk sample arrays. Those stay the deep stage's
            # job; this is the cheap catalog-level mirror.
            res["rt"] = "WELL_LOG"
            try:
                import lasio, uuid as _uuid
                from datetime import datetime as _dt
                try:
                    from dataview.file_catalog.catalog_capture import capture as _cap
                except ImportError:
                    from dataview.file_catalog.catalog_capture import capture as _cap
                _now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
                inv = well_info.get("inventory_id")
                las = lasio.read(fpath)

                def _wv(*keys):
                    for k in keys:
                        try:
                            v = str(las.well[k].value).strip()
                            if v and v.lower() not in (
                                    "", "unknown", "none", "--"):
                                return v
                        except Exception:
                            pass
                    return None

                def _wnum(*keys):
                    """Numeric LAS well value (lat/long) -> float in range, else None."""
                    return _safe_coord(_wv(*keys))

                # The LAS header carries the authoritative UWI in ~WELL
                # (UWI. 17-031-10035-0000). Read and normalize it, and PREFER it
                # over the caller's filename-derived uwi (which can be an FN_
                # fallback). Mirrors worker_core._do_las so the pool path and this
                # in-page path agree. Only keep the passed-in uwi if the header
                # has none.
                _hdr_uwi = _norm_uwi(_wv("UWI", "API", "APINUM", "APINO",
                                         "API_NO", "WELLID"))
                if _hdr_uwi:
                    uwi = _hdr_uwi      # header UWI wins — real well identity

                # 1) well header → cat_well (promote builds dv_well from this)
                if uwi:
                    try:
                        _hn = _cap(engine, "cat_well", [{
                            "WELL_NAME":        _wv("WELL") or uwi,
                            "OPERATOR_NAME":    _wv("COMP", "PROV"),
                            "FIELD_NAME":       _wv("FLD", "FIELD"),
                            "PROVINCE_STATE":   _wv("STAT", "STATE"),
                            "COUNTY":           _wv("CNTY", "COUNTY"),
                            "COUNTRY":          _wv("CTRY", "CTRY.", "COUNTRY"),
                            "SURFACE_LATITUDE":  _wnum("LATI", "LAT"),
                            "SURFACE_LONGITUDE": _wnum("LONG", "LON"),
                            "FINAL_TD":         _wv("STOP", "TD"),
                            "ACTIVE_IND":       "Y",
                            "ROW_QUALITY":      "FINAL",
                            "PPDM_GUID":        str(_uuid.uuid4()),
                            "ROW_CREATED_BY":   "DataWrangler",
                            "ROW_CREATED_DATE": _now,
                        }], uwi=uwi, inventory_id=inv,
                           source_path=fpath, source="LAS_HEADER")
                        if _hn:
                            res["detail"]["cat_well"] = (
                                res["detail"].get("cat_well", 0) + _hn)
                    except Exception as _he:
                        res["errors"].append(f"header capture: {_he}")

                # depth frame from the LAS index (cheap; no arrays retained)
                try:
                    _idx = las.index
                    d_start = float(_idx[0])  if len(_idx) else None
                    d_stop  = float(_idx[-1]) if len(_idx) else None
                    s_count = int(len(_idx))
                except Exception:
                    d_start = d_stop = None
                    s_count = 0
                d_step = _wv("STEP")
                try:
                    d_uom = (las.curves[0].unit or "").strip() or None
                except Exception:
                    d_uom = None

                # Shared log identity for this LAS — both the log header
                # (cat_well_log) and its curves (cat_well_log_curve) key on it,
                # so curves resolve their FK to dv_well_log.log_id on promote.
                # NOT f"{uwi}-LAS" when there is no uwi: that renders the
                # literal string "None-LAS" — a placeholder of exactly the
                # shape find_placeholders.sql exists to catch, and one that
                # would COLLIDE across every unkeyed LAS in the corpus, so two
                # unrelated files' curves would claim one log. Fall back to
                # something the FILE owns instead: unique per file, stable
                # across re-runs, and still shared by the header and its curves
                # because both read this one variable. LOG_ID is varchar(80).
                _logid = (_wv("LOG_ID", "LOGID")
                          or (f"{uwi}-LAS" if uwi
                              else f"{os.path.basename(fpath or '')}-LAS"[:80]))

                # 2) per-curve metadata → cat_well_log_curve (one row per curve,
                # linked to the log run via log_id). PPDM well_log_curve grain:
                # curves belong to a log run (dv_well_log) which belongs to a well.
                # In a LAS ~Curve line `MNEM.UNIT  API_CODE : DESCRIPTION`, lasio
                # maps API_CODE to curve.value and DESCRIPTION to .descr.
                curve_rows = []
                for i, c in enumerate(las.curves):
                    mnem = (getattr(c, "mnemonic", "") or "").strip()
                    if not mnem:
                        continue
                    curve_rows.append({
                        "UWI":               uwi or None,
                        "LOG_ID":            _logid,
                        "CURVE_ID":          mnem[:40],
                        "MNEMONIC":          mnem,
                        "CURVE_DESCRIPTION": (getattr(c, "descr", "")
                                              or "").strip() or None,
                        "CURVE_UNIT":        (getattr(c, "unit", "")
                                              or "").strip() or None,
                        "TOP_DEPTH":         _safe_num(d_start),
                        "BASE_DEPTH":        _safe_num(d_stop),
                        "DEPTH_OUOM":        d_uom,
                        "NULL_VALUE":        _safe_num(_wv("NULL")),
                        "ACTIVE_IND":        "Y",
                        "ROW_CREATED_BY":    "DataWrangler",
                        "ROW_CREATED_DATE":  _now,
                    })
                if curve_rows:
                    _cn = _cap(engine, "cat_well_log_curve", curve_rows,
                               uwi=uwi, inventory_id=inv,
                               source_path=fpath, source="LAS")
                    res["loaded"] = res.get("loaded", 0) + (_cn or 0)
                    if _cn:
                        res["detail"]["cat_well_log_curve"] = _cn

                # 3) log-file header → cat_well_log (one row per LAS). Reuses the
                # depth frame / uom already computed for the curves and the same
                # _logid the curves carry, so header and curves share one key.
                #
                # STAGED WHENEVER THE CURVES ARE, WITH OR WITHOUT A UWI. This
                # was gated on `if uwi:` while the curves above are gated only
                # on there BEING curves — so a LAS with no UWI staged the
                # CHILD and skipped the PARENT, which is the one combination
                # that cannot work. dv_well_log_curve carries
                # fk_log_curve_log (uwi, log_id) -> dv_well_log, so those
                # curves can never promote; and because a compound FK is not
                # covered by _reference_fk_predicates, nothing HELD them
                # either. Promote attempted the insert and 547'd, failing the
                # whole mirror.
                #
                # MEASURED 23 Aug: cat_well_log empty, cat_well_log_curve 153
                # unpromoted, every one naming LOG_15007205750000_1 and
                # friends with no parent. "Promote table failed."
                #
                # A NULL uwi on the header is fine and is the same bargain the
                # curves already take: catalog_status.apply_fix fills uwi on
                # EVERY staged mirror row that lacks one when the file is
                # keyed, so parent and child are completed together and
                # promote in the same pass. Staging neither would also be
                # consistent, but it throws away header data the file gave us
                # and leaves nothing for the UWI to attach to.
                if curve_rows or uwi:
                    try:
                        _logdt = _wv("DATE", "LOGDATE", "DATE_LOG")
                        # Service company arrives as a raw name (e.g. HALLIBURTON)
                        # from the LAS SRVC mnemonic. dv_well_log.service_company_ba_id
                        # is an FK to dv_business_associate.ba_id, so a raw name would
                        # 547 on promote. Until BA-seeding is wired (SHA1 ba_id, matching
                        # entity_seeder), leave the FK NULL and keep the name in remark
                        # so the provenance is preserved and auditable.
                        _srvc = _wv("SRVC", "SERVICE", "COMP")
                        _wlrow = {
                            "LOG_ID":                _logid,
                            "LOG_TYPE":              _wv("TYPE", "LOGTYPE"),
                            "RUN_NUM":               _wv("RUN", "RUN_NUMBER"),
                            "LOG_DATE":              _logdt,
                            "SERVICE_COMPANY_BA_ID": None,
                            "TOP_DEPTH":             _safe_num(d_start),
                            "BASE_DEPTH":            _safe_num(d_stop),
                            "DEPTH_OUOM":            d_uom,
                            "NULL_VALUE":            _safe_num(_wv("NULL")),
                            "FILE_PATH":             fpath,
                            "FILE_FORMAT":           "LAS",
                            "ACTIVE_IND":            "Y",
                            "REMARK":                (f"service_company={_srvc}"
                                                      if _srvc else None),
                            "ROW_CREATED_BY":        "DataWrangler",
                            "ROW_CREATED_DATE":      _now,
                        }
                        _wln = _cap(engine, "cat_well_log", [_wlrow],
                                    uwi=uwi, inventory_id=inv,
                                    source_path=fpath, source="LAS")
                        if _wln:
                            res["detail"]["cat_well_log"] = (
                                res["detail"].get("cat_well_log", 0) + _wln)
                    except Exception as _wle:
                        res["errors"].append(f"well_log capture: {_wle}")

                if res.get("loaded") or res["detail"]:
                    res["ok"] = True
                    _set_readiness_cataloged(engine, inv)
                else:
                    res["note"] = "las_empty"
            except Exception as e:
                res["errors"].append(str(e))
            return res

        else:
            res["note"] = "unsupported"
            return res

    except Exception as e:
        res["errors"].append(str(e))
        return res


# =============================================================================
# Enrichment write path — MOVED HERE FROM page_workbench 16 Aug 2026
# =============================================================================
# The transitive closure of what _stage_extract needs to WRITE what it parsed:
# the three MERGE/UPDATE statements, their parameter builders, and the small
# coercion helpers those rest on. It came over as one unit because that is what
# it is — splitting it would have left pipeline_run importing the UI for the
# other half, which is the whole defect being removed.

ENRICH_CHUNK = 50   # files processed per rerun cycle


# ── Header-write SQL (shared by the per-row and batched writers) ───────────
# Executed one row at a time by _write_enrichment_on (manual review path) and
# as a single executemany batch per chunk by _write_enrichment_batch (the
# pipeline extract loop). fast_executemany on the engine collapses the batch
# into a few round-trips instead of two per file — that write phase was ~90%
# of extract wall-clock on SQL Express.
_SQL_GFC_UPDATE = """
    UPDATE file_catalog.GLOBAL_FILE_CATALOG SET
        CATALOG_SCORE     = :score,
        CATALOG_READINESS = :readiness,
        MATCHED_UWI       = :uwi,
        CATALOG_ISSUES    = :issues,
        SPATIAL_OUTLINE   = :spatial_outline,
        CATALOG_TABLE     = :catalog_table,
        HEADER_EXTRACTED  = 'Y',
        ROW_CHANGED_DATE  = GETUTCDATE()
    WHERE INVENTORY_ID = :id
"""


_SQL_WELL_MERGE = """
    MERGE file_catalog.FILE_WELL_HEADER AS tgt
    USING (SELECT :hid AS WELL_HEADER_ID) src
    ON tgt.WELL_HEADER_ID = src.WELL_HEADER_ID
    WHEN MATCHED THEN UPDATE SET
        UWI=:uwi, WELL_NAME=:wn, OPERATOR=:op,
        WELL_FIELD=:fld, STATE=:st, COUNTY=:co,
        LATITUDE=:lat, LONGITUDE=:lon,
        TOTAL_DEPTH=:td, SPUD_DATE=:spud,
        RIG_RELEASE=:rig, REPORT_TYPE=:rt,
        SURVEY_TYPE=:stype, CONTRACTOR=:contr,
        CONFIDENCE=:conf, EXTRACTED_DATE=GETUTCDATE()
    WHEN NOT MATCHED THEN INSERT (
        WELL_HEADER_ID,INVENTORY_ID,
        UWI,WELL_NAME,OPERATOR,WELL_FIELD,
        STATE,COUNTY,LATITUDE,LONGITUDE,
        TOTAL_DEPTH,SPUD_DATE,RIG_RELEASE,
        REPORT_TYPE,SURVEY_TYPE,CONTRACTOR,CONFIDENCE,
        EXTRACTED_DATE,EXTRACTED_BY
    ) VALUES (
        :hid,:inv_id,
        :uwi,:wn,:op,:fld,
        :st,:co,:lat,:lon,
        :td,:spud,:rig,
        :rt,:stype,:contr,:conf,
        GETUTCDATE(),'DataWrangler'
    );
"""


# ── survey-name provenance ──────────────────────────────────────────────────
# Modelled on FILE_WELL_HEADER.IDENTITY_SOURCE, including its convention that a
# value derived from a PATH is a candidate rather than a fact (triage_inventory
# reads `IDENTITY_SOURCE NOT LIKE 'path%'` for exactly that reason).
#
#   'header'            the SEG-Y textual header named the survey
#   'rejected-template' the header held only card-image labels; NO name was
#                       taken, and enrich must not substitute a filename guess
#   'path-filename'     enrich_file_headers guessed from the file name
#   'manual'            a person typed it. Outranks everything.
#
# WHY THIS EXISTS: on 23 Aug the extractor correctly refused Teapot's
# "AREA MAP ID" (the printed labels of an untyped rev-0 card image), left
# SURVEY_NAME NULL — and enrich promptly filled the blank from each file name,
# turning ONE wrong survey into FIVE (lineA…lineE). Rejecting a bad value only
# helps if the next stage can tell "refused" from "never looked".
SEIS_NAME_COLS = (("SURVEY_NAME_SOURCE", "varchar(30) NULL"),)
_SEIS_COLS_READY = False


def ensure_seis_columns(con):
    """Add the provenance column if this database predates it. One metadata
    query per process; ALTER only when genuinely missing."""
    global _SEIS_COLS_READY
    if _SEIS_COLS_READY:
        return
    from sqlalchemy import text as _t
    try:
        have = {r[0].upper() for r in con.execute(_t(
            "SELECT name FROM sys.columns WHERE object_id = "
            "OBJECT_ID('file_catalog.FILE_SEIS_HEADER')")).fetchall()}
        for col, typ in SEIS_NAME_COLS:
            if col.upper() not in have:
                con.execute(_t("ALTER TABLE file_catalog.FILE_SEIS_HEADER "
                               f"ADD [{col}] {typ}"))
        _SEIS_COLS_READY = True
    except Exception:
        pass          # non-fatal: the MERGE below degrades, it does not fail


# ONE WRITER. This constant is imported by worker_core (the multicore path,
# which is the default) and page_workbench rather than copied — a second
# spelling of this MERGE is how the escapechar bug came back through a fourth
# writer, and this table had four of them until 23 Aug.
_SQL_SEIS_MERGE = """
    MERGE file_catalog.FILE_SEIS_HEADER AS tgt
    USING (SELECT :hid AS SEIS_HEADER_ID) src
    ON tgt.SEIS_HEADER_ID = src.SEIS_HEADER_ID
    WHEN MATCHED THEN UPDATE SET
        -- A NAME IS NEVER ERASED BY A RE-EXTRACT. "I could not read one" must
        -- not mean "delete the one you have": before this, re-extracting a
        -- file whose header names no survey blanked the column, and enrich
        -- then refilled it from the file name — so a survey a person had
        -- named came back as 'lineA' with nothing recording the loss.
        SURVEY_NAME = CASE
            WHEN ISNULL(tgt.SURVEY_NAME_SOURCE,'') = 'manual'
                THEN tgt.SURVEY_NAME
            ELSE COALESCE(:sn, tgt.SURVEY_NAME) END,
        SURVEY_NAME_SOURCE = CASE
            WHEN ISNULL(tgt.SURVEY_NAME_SOURCE,'') = 'manual'
                THEN tgt.SURVEY_NAME_SOURCE
            WHEN :sn IS NOT NULL          THEN :snsrc
            WHEN tgt.SURVEY_NAME IS NOT NULL THEN tgt.SURVEY_NAME_SOURCE
            ELSE :snsrc END,
        LINE_NAME=:ln,
        SEIS_SET_TYPE=:stype, SURVEY_DATE=:sd,
        CONTRACTOR=:contr,
        BBOX_MIN_LAT=:bmin_lat, BBOX_MAX_LAT=:bmax_lat,
        BBOX_MIN_LON=:bmin_lon, BBOX_MAX_LON=:bmax_lon,
        EPSG_CODE=:epsg, SAMPLE_INTERVAL=:si,
        TRACE_COUNT=:tc, SHOT_FIRST=:sf, SHOT_LAST=:sl,
        IL_MIN=:il_min, IL_MAX=:il_max,
        XL_MIN=:xl_min, XL_MAX=:xl_max,
        SURVEY_OUTLINE=:outline,
        EXTRACTED_DATE=GETUTCDATE()
    WHEN NOT MATCHED THEN INSERT (
        SEIS_HEADER_ID,INVENTORY_ID,
        SURVEY_NAME,SURVEY_NAME_SOURCE,LINE_NAME,SEIS_SET_TYPE,SURVEY_DATE,
        CONTRACTOR,BBOX_MIN_LAT,BBOX_MAX_LAT,
        BBOX_MIN_LON,BBOX_MAX_LON,EPSG_CODE,
        SAMPLE_INTERVAL,TRACE_COUNT,SHOT_FIRST,SHOT_LAST,
        IL_MIN,IL_MAX,XL_MIN,XL_MAX,SURVEY_OUTLINE,
        EXTRACTED_DATE,EXTRACTED_BY
    ) VALUES (
        :hid,:inv_id,
        :sn,:snsrc,:ln,:stype,:sd,
        :contr,:bmin_lat,:bmax_lat,
        :bmin_lon,:bmax_lon,:epsg,
        :si,:tc,:sf,:sl,
        :il_min,:il_max,:xl_min,:xl_max,:outline,
        GETUTCDATE(),'DataWrangler'
    );
"""


def _gfc_params(inv_id, fields):
    """GLOBAL_FILE_CATALOG update params. Caller normalizes fields['uwi'] first."""
    score, readiness = _score(fields)
    _cat = fields.get("file_category")
    # only stamp CATALOG_TABLE for the spatial feature types promote_X reads
    _cat_tbl = _cat if _cat in ("FIELD","LAND_TRACT","BOUNDARY","PIPELINE") else None
    return {"score": score, "readiness": readiness,
            "uwi": _trunc(fields.get("uwi"), 40),
            "issues": "; ".join(_issues(fields)), "id": inv_id,
            "spatial_outline": fields.get("spatial_outline"),
            "catalog_table": _cat_tbl}


def _valid_date(v):
    """Return the date as mm/dd/yyyy if v parses as a real date in a known
    format; else None. Nulls out mis-parsed junk ('Wed', 'Fri', '', long text)."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    from datetime import datetime as _dtm
    cands = [s]
    if " " in s:
        cands.append(s.split(" ", 1)[0])      # drop a trailing time
    fmts = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y",
            "%m-%d-%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y",
            "%B %d, %Y", "%d.%m.%Y", "%m.%d.%Y", "%Y%m%d", "%b-%d-%Y",
            "%d-%B-%Y", "%m/%d/%Y %H:%M:%S")
    for c in cands:
        for f in fmts:
            try:
                return _dtm.strptime(c, f).strftime("%m/%d/%Y")
            except ValueError:
                continue
    return None


def _well_params(inv_id, fields):
    return {
        "hid":    uuid.uuid5(uuid.NAMESPACE_URL, inv_id).hex.upper(),
        "inv_id": inv_id,
        "uwi":    _trunc(fields.get("uwi"), 40),
        "wn":     _trunc(fields.get("well_name"), 255),
        "op":     _trunc(fields.get("operator"), 255),
        "fld":    _trunc(fields.get("well_field"), 100),
        "st":     _trunc(fields.get("state"), 50),
        "co":     _trunc(fields.get("county"), 100),
        "lat":    _trunc(fields.get("latitude"), 30),
        "lon":    _trunc(fields.get("longitude"), 30),
        "td":     _trunc(fields.get("total_depth"), 20),
        "spud":   _valid_date(fields.get("spud_date")),
        "rig":    _valid_date(fields.get("rig_release")),
        "rt":     _trunc(fields.get("report_type"), 50),
        "stype":  _trunc(fields.get("survey_type"), 50),
        "contr":  _trunc(fields.get("contractor"), 255),
        "conf":   _safe_num(fields.get("confidence")),
    }


def _seis_params(inv_id, fields):
    return {
        "hid":      uuid.uuid5(uuid.NAMESPACE_URL, inv_id + "_s").hex.upper(),
        "inv_id":   inv_id,
        "sn":       _trunc(fields.get("survey_name"), 255),
        # None when nothing was read AND nothing was rejected — the
        # MERGE keeps whatever provenance the row already had.
        "snsrc":    _trunc(fields.get("survey_name_source"), 30),
        "ln":       _trunc(fields.get("line_name"), 255),
        "stype":    _trunc(fields.get("seis_set_type"), 40),
        "sd":       _valid_date(fields.get("survey_date")),
        "contr":    _trunc(fields.get("contractor"), 255),
        "bmin_lat": _safe_coord(fields.get("bbox_min_lat")),
        "bmax_lat": _safe_coord(fields.get("bbox_max_lat")),
        "bmin_lon": _safe_coord(fields.get("bbox_min_lon")),
        "bmax_lon": _safe_coord(fields.get("bbox_max_lon")),
        "epsg":     _safe_epsg(fields.get("epsg_code")),
        "si":       _safe_sample_interval(fields.get("sample_interval")),
        "tc":       _safe_trace_count(fields.get("trace_count")),
        "sf":       _trunc(fields.get("shot_first"), 20),
        "sl":       _trunc(fields.get("shot_last"), 20),
        "il_min":   fields.get("il_min"),
        "il_max":   fields.get("il_max"),
        "xl_min":   fields.get("xl_min"),
        "xl_max":   fields.get("xl_max"),
        "outline":  fields.get("survey_outline"),
    }


def _write_enrichment_on(con, inv_id: str, fields: dict):
    """Write extracted header fields for ONE file on a caller-provided
    connection (inside an active engine.begin(); does not commit). Used by the
    manual review path. The pipeline extract loop uses _write_enrichment_batch."""
    from sqlalchemy import text as _t
    fields["uwi"] = _norm_uwi(fields.get("uwi"))      # canonicalize once
    category = fields.get("file_category", "UNKNOWN")
    con.execute(_t(_SQL_GFC_UPDATE), _gfc_params(inv_id, fields))
    if category == "WELL":
        con.execute(_t(_SQL_WELL_MERGE), _well_params(inv_id, fields))
    elif category == "SEIS":
        ensure_seis_columns(con)
        con.execute(_t(_SQL_SEIS_MERGE), _seis_params(inv_id, fields))


def _clamp_well(rows):
    """Clamp each string well-param to its column width so no value overflows
    (defends against mis-parsed dates/fields and fast_executemany under-sizing)."""
    _w = {"uwi": 40, "wn": 255, "op": 255, "fld": 100, "st": 50, "co": 100,
          "spud": 20, "rig": 20, "rt": 50, "stype": 50, "contr": 255,
          "lat": 30, "lon": 30, "td": 20}
    for r in rows:
        for k, n in _w.items():
            v = r.get(k)
            if isinstance(v, str) and len(v) > n:
                r[k] = v[:n]
    return rows


def _write_enrichment_batch(con, items):
    """Write extracted header fields for MANY files in one batched round-trip
    per statement instead of two per file. items = [(inv_id, fields), …].

    Same proven UPDATE/MERGE SQL as the per-row path, run via executemany so
    fast_executemany collapses ~2 round-trips/file into ~3 calls per chunk —
    the write phase was ~90% of extract time on Express. Connection must be in
    an active engine.begin(); does not commit. Raises on failure so the caller
    can fall back to the per-row path in a fresh transaction."""
    from sqlalchemy import text as _t
    if not items:
        return
    for _iid, _f in items:                            # canonicalize UWI once each
        _f["uwi"] = _norm_uwi(_f.get("uwi"))
    gfc  = [_gfc_params(iid, f) for iid, f in items]
    well = [_well_params(iid, f) for iid, f in items
            if f.get("file_category") == "WELL"]
    seis = [_seis_params(iid, f) for iid, f in items
            if f.get("file_category") == "SEIS"]
    con.execute(_t(_SQL_GFC_UPDATE), gfc)             # executemany
    # per-row MERGE: the USING(SELECT ?) subquery defeats pyodbc fast_executemany
    # column sizing, under-sizing string buffers and truncating a later long
    # value; per-row binds each at its true size (still one transaction).
    # REVERTED, AND THE MEASUREMENT IS WHY. I replaced this loop with a
    # chunked `USING (VALUES …)` MERGE — 22 statements instead of ~1,055 —
    # expecting header_write to collapse. It went 136.4s -> 177.7s: about
    # 8 SECONDS PER 50-ROW STATEMENT. A multi-row VALUES source changes the
    # plan the optimiser picks against FILE_WELL_HEADER; the single-row
    # `USING (SELECT :hid)` form can seek, and evidently does.
    #
    # So the per-row loop is not merely the SAFE form (the truncation
    # reason in _sql_well_merge_many's docstring), it is the FAST one here.
    # Whoever wrote it had already found that out. _sql_well_merge_many and
    # _merge_wells_chunked are left in place, unused, because the next
    # person will have the same idea and should be able to read why it
    # loses before trying it again.
    for _w in _clamp_well(well):
        con.execute(_t(_SQL_WELL_MERGE), _w)
    if seis:
        ensure_seis_columns(con)
    for _sp in seis:
        con.execute(_t(_SQL_SEIS_MERGE), _sp)


def _score(fields: dict) -> tuple:
    # Geography features (field/lease/pipeline/boundary polygons & lines) are
    # NOT wells — they carry no UWI/well_name/depth and must not be scored on
    # well attributes, or they score 0 and get parked at NEEDS_UWI forever
    # (classify_shapefile correctly typed them, but this scorer ignored that).
    # Their readiness comes from having geometry to promote, not a UWI: a
    # recognized spatial category is READY on its own.
    if fields.get("file_category") in ("FIELD", "LAND_TRACT", "BOUNDARY", "PIPELINE"):
        return 70, "READY"
    score = 0
    if fields.get("uwi"):       score += 40
    if fields.get("well_name"): score += 20
    if fields.get("operator"):  score += 10
    if fields.get("latitude") and fields.get("longitude"): score += 20
    if fields.get("total_depth"): score += 10
    if score >= 80:  return score, "READY"
    if score >= 60:  return score, "REVIEW"
    if score >= 30:  return score, "NEEDS_UWI"
    return score, "ATTENTION"


def _issues(fields: dict) -> list:
    out = []
    if fields.get("extract_error"):
        out.append(str(fields["extract_error"])[:200])
    if not fields.get("uwi"):       out.append("No UWI")
    if not fields.get("well_name"): out.append("No well name")
    if not (fields.get("latitude") and fields.get("longitude")):
        out.append("No coordinates")
    return out


def _trunc(v, n):
    return str(v)[:n] if v is not None else None


def _safe_int(v):
    """Convert to int or None. Silently swallows bad input."""
    try:
        return int(float(str(v).strip())) if v is not None else None
    except (ValueError, TypeError):
        return None


def _safe_sample_interval(v):
    """Seismic sample interval (microseconds). Positive, sane upper bound."""
    n = _safe_num(v)
    # SEGY interval is in microseconds; legitimate values are 250-16000.
    # Anything outside [0, 1_000_000] is garbage from a bad header read.
    if n is None or n < 0 or n > 1_000_000:
        return None
    return n


def _safe_trace_count(v):
    """Seismic trace count. Positive int, sane upper bound."""
    n = _safe_int(v)
    if n is None or n < 0 or n > 100_000_000:
        return None
    return n


def _safe_epsg(v):
    """EPSG code. 4 to 6 digit positive int."""
    n = _safe_int(v)
    if n is None or n < 1000 or n > 999_999:
        return None
    return n
