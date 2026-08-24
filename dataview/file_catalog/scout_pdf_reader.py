"""
scout_pdf_reader.py
==================
Read a *text-based* scout-ticket PDF (the ReportLab export) back into structured
data — one tidy table per section. Works because the ReportLab tickets are drawn
with real ruled tables, so pdfplumber's line-based detection recovers structure
straight from the borders. (The OCR'd image tickets, which have no ruled lines,
still go through extract_scout_ticket's grid parser.)

    from dataview.file_catalog.scout_pdf_reader import extract_scout_pdf_tables
    secs = extract_scout_pdf_tables("Scout_STATE_MAR_035H.pdf")
    secs["Frac Stages"]   # -> pandas DataFrame, all 10 columns
"""
from __future__ import annotations
import re
import pandas as pd

# Canonical headers per section — used instead of the rendered header row so a
# header that wrapped across two lines (e.g. "Stage\ns" -> "Stage s") is always
# clean. Match is by the section banner text.
SECTIONS = {
    "Stratigraphy":       ["Formation", "Top MD (ft)", "Base MD (ft)", "Gross (ft)", "Lithology"],
    "Petrophysics":       ["Zone", "Top MD", "Base MD", "Net", "N/G", "Vsh", "PHIe", "Sw", "Perm", "Fluid", "Pay"],
    "Directional Survey": ["MD (ft)", "Inc", "Azi", "TVD (ft)", "N/S (ft)", "E/W (ft)", "DLS"],
    "DST":                ["Test Date", "Type", "Top MD", "Base MD", "Result", "Max Oil", "Max Gas", "API Grav"],
    "Core Runs":          ["#", "Type", "Show", "Formation", "Top MD", "Base MD", "Length", "Rec %", "Date", "Photos"],
    "Core Sample":        ["Sample", "Type", "Depth", "Lithology", "Show", "Por %", "Perm", "Bulk Den", "Sw", "So"],
    "Completion Summary": ["Completion Date", "Type", "Orientation", "Formation", "Lateral (ft)", "Stages",
                           "Fluid (bbl)", "Proppant (lbs)", "Prop Intensity", "Fluid System"],
    "Frac Stages":        ["Stage", "Top MD", "Base MD", "Clusters", "Cluster Sp", "Fluid (bbl)",
                           "Proppant (lbs)", "ISIP", "Avg Treat", "Max Rate"],
    "Production Summary": ["Date", "Oil (bbl)", "Gas (Mcf)", "Water (bbl)", "Avg Rate"],
    # cat_well_perforation is a mirror WITH NO PRODUCER: it is in
    # MIRROR_TABLES and in LINEAGE, and nothing has ever written it.
    # dv_office_loader still says perforation is "outside the 11-table
    # mirror scope", which stopped being true when the mirror was added.
    # Perforations belong on a scout ticket, so that is where they come
    # from now.
    # THE ONE MEASUREMENT THAT TIES A WELL TO SEISMIC. Every other
    # time-depth here is derived from a velocity model; these are the
    # observations that model is supposed to honour.
    "Checkshots":         ["Station", "MD (ft)", "TVD (ft)", "TWT (ms)",
                           "OWT (ms)", "Avg Vel", "Int Vel"],
    "Perforations":       ["Perf Date", "Top MD", "Base MD", "Shots", "SPF",
                           "Gun", "Phasing", "Formation", "Status"],
}

# Tokens that a narrow column wrapped mid-word, so pdfplumber rejoined them with a
# space ("CONVENTIONA L"). De-spacing these back is safe because they're known
# single-token enums; everything else keeps its spaces.
_WRAP_FIX = {
    "CONVENTIONA L": "CONVENTIONAL", "SIDEWAL L": "SIDEWALL", "Photo s": "Photos",
    "Stage s": "Stages", "Cluster s": "Clusters",
}


def _clean(c) -> str:
    s = re.sub(r"\s+", " ", (c or "").replace("\n", " ")).strip()
    return _WRAP_FIX.get(s, s)


def _is_banner(row) -> bool:
    """A section banner row: first cell has text, the rest are blank."""
    cells = [(_clean(c)) for c in row]
    return bool(cells[0]) and all(not c for c in cells[1:])


def _match_section(banner: str):
    b = banner.upper()
    for key in SECTIONS:
        if key.upper() in b or b in key.upper():
            return key
    if "STRATIGRAPHY" in b or "FORMATION TOPS" in b:
        return "Stratigraphy"
    return None


def extract_scout_pdf_tables(path: str) -> dict:
    """Return {section_name: DataFrame} for every section in a text scout PDF,
    plus 'Well Header' as a flat {label: value} dict under key 'Well Header'."""
    import pdfplumber
    out = {}
    with pdfplumber.open(path) as pdf:
        tables = []
        for p in pdf.pages:
            tables.extend(p.extract_tables() or [])

    for t in tables:
        if not t:
            continue
        rows = [[_clean(c) for c in r] for r in t]

        # Well Header: 4-col key/value, no banner, starts with API.
        if rows[0] and rows[0][0].upper() == "API" and len(rows[0]) >= 4:
            kv = {}
            for r in rows:
                if len(r) >= 2 and r[0]:
                    kv[r[0]] = r[1]
                if len(r) >= 4 and r[2]:
                    kv[r[2]] = r[3]
            out["Well Header"] = kv
            continue

        if not _is_banner(rows[0]):
            continue
        sec = _match_section(rows[0][0])
        if not sec:
            continue
        cols = SECTIONS[sec]
        body = []
        for r in rows[2:]:  # skip banner + rendered header row
            vals = [c for c in r]
            if not any(vals):
                continue
            # the section's "No X data" placeholder
            if len(vals) >= 1 and vals[0].lower().startswith("no "):
                body = []
                break
            vals = (vals + [""] * len(cols))[:len(cols)]
            body.append(vals)
        _df = pd.DataFrame(body, columns=cols)
        out[sec] = (pd.concat([out[sec], _df], ignore_index=True)
                    if sec in out and not out[sec].empty else _df)
    return out


if __name__ == "__main__":
    import sys, json
    secs = extract_scout_pdf_tables(sys.argv[1])
    for name, val in secs.items():
        print("=" * 70)
        print(name)
        print("-" * 70)
        if isinstance(val, dict):
            for k, v in val.items():
                print(f"  {k:16} : {v}")
        else:
            if val.empty:
                print("  (no rows)")
            else:
                print(val.to_string(index=False))
        print()


# ── pipeline adapter ─────────────────────────────────────────────────────────
# Map the tidy section tables into the exact dict shape load_scout consumes, so
# the TEXT ticket flows through capture/promote identically to an OCR ticket —
# same keys, no changes downstream.
import re as _re


def _num(v):
    """Parse a number out of a table cell ('19,482,650', '13,444 ft', '42.6')."""
    if v is None:
        return None
    s = _re.sub(r"[^0-9.\-]", "", str(v))
    if s in ("", "-", ".", "-.", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _txt(v):
    s = (str(v).strip() if v is not None else "")
    return s or None


def _parse_surface_loc(s):
    """'32.127700N 101.560500W' -> (lat, lon) with W/S negative."""
    if not s:
        return None, None
    m = _re.findall(r"(-?\d+\.?\d*)\s*([NSEW])", str(s).upper())
    lat = lon = None
    for val, hemi in m:
        f = float(val)
        if hemi in "NS":
            lat = -f if hemi == "S" else f
        else:
            lon = -f if hemi == "W" else f
    return lat, lon


def looks_like_text_ticket(path: str) -> bool:
    """True if the PDF is a ruled-table TEXT ticket (selectable text + drawn
    table lines), vs an image-only / pipe-delimited OCR ticket. Cheap: just the
    first page's text layer and rule count."""
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            p = pdf.pages[0]
            has_text = bool((p.extract_text() or "").strip())
            rules = len(p.rects) + len(p.lines)
        return has_text and rules >= 8
    except Exception:
        return False


def extract_scout_ticket_text(path: str) -> dict:
    """Extract a TEXT (ReportLab) scout ticket into the load_scout contract.
    Returns {} if the ticket can't be read as ruled tables."""
    secs = extract_scout_pdf_tables(path)
    if not secs:
        return {}
    h = secs.get("Well Header", {}) or {}
    lat, lon = _parse_surface_loc(h.get("Surface Loc"))

    header = {
        "WELL_NAME":       _txt(h.get("Well Name")),
        "WELL_TYPE":       _txt(h.get("Well Type")),
        "WELL_STATUS":     _txt(h.get("Status")),
        "STATE":           _txt(h.get("State")),
        "COUNTY":          _txt(h.get("County")),
        "API":             _txt(h.get("API")),
        "OPERATOR":        _txt(h.get("Operator")),
        "FIELD":           _txt(h.get("Field")),
        "SPUD_DATE":       _txt(h.get("Spud Date")),
        "COMPLETION_DATE": _txt(h.get("Completion")),
        "TOTAL_DEPTH":     _num(h.get("Total Depth MD")),
        "LATITUDE":        lat,
        "LONGITUDE":       lon,
    }

    def recs(name):
        df = secs.get(name)
        return [] if df is None or df.empty else df.to_dict("records")

    tops = [{"FORMATION_NAME": _txt(r.get("Formation")),
             "DEPTH_TOP_MD":   _num(r.get("Top MD (ft)")),
             "DEPTH_BASE_MD":  _num(r.get("Base MD (ft)"))} for r in recs("Stratigraphy")]

    dst = [{"TEST_TYPE":   _txt(r.get("Type")),
            "TEST_DATE":   _txt(r.get("Test Date")),
            "TOP":         _num(r.get("Top MD")),
            "BASE":        _num(r.get("Base MD")),
            "RESULT":      _txt(r.get("Result")),
            "OIL_RATE":    _num(r.get("Max Oil")),
            "GAS_RATE":    _num(r.get("Max Gas")),
            "API_GRAVITY": _num(r.get("API Grav"))} for r in recs("DST")]

    frac = [{"STAGE":        _num(r.get("Stage")),
             "TOP":          _num(r.get("Top MD")),
             "BASE":         _num(r.get("Base MD")),
             "FLUID_BBL":    _num(r.get("Fluid (bbl)")),
             "PROPPANT_LBS": _num(r.get("Proppant (lbs)")),
             "ISIP":         _num(r.get("ISIP")),
             "MAX_PRESS":    _num(r.get("Avg Treat"))} for r in recs("Frac Stages")]

    core = [{"DEPTH":        _num(r.get("Depth")),
             "POROSITY":     _num(r.get("Por %")),
             "PERMEABILITY": _num(r.get("Perm")),
             "SW":           _num(r.get("Sw"))} for r in recs("Core Sample")]

    survey = [{"MD":  _num(r.get("MD (ft)")), "INC": _num(r.get("Inc")),
               "AZI": _num(r.get("Azi")),     "TVD": _num(r.get("TVD (ft)")),
               "NS":  _num(r.get("N/S (ft)")), "EW": _num(r.get("E/W (ft)")),
               "DLS": _num(r.get("DLS"))} for r in recs("Directional Survey")]

    ip_rows = [{"DATE":       _txt(r.get("Date")),
                "OIL_BOPD":   _num(r.get("Oil (bbl)")),
                "GAS_MCFD":   _num(r.get("Gas (Mcf)")),
                "WATER_BWPD": _num(r.get("Water (bbl)"))} for r in recs("Production Summary")]

    perfs = [{"PERF_DATE":  _txt(r.get("Perf Date")),
              "TOP":        _num(r.get("Top MD")),
              "BASE":       _num(r.get("Base MD")),
              "SHOTS":      _num(r.get("Shots")),
              "SPF":        _num(r.get("SPF")),
              "GUN":        _txt(r.get("Gun")),
              "PHASING":    _num(r.get("Phasing")),
              "FORMATION":  _txt(r.get("Formation")),
              "STATUS":     _txt(r.get("Status"))}
             for r in recs("Perforations")]

    checkshots = [{"STATION":  _txt(r.get("Station")),
                   "MD":       _num(r.get("MD (ft)")),
                   "TVD":      _num(r.get("TVD (ft)")),
                   "TWT":      _num(r.get("TWT (ms)")),
                   "OWT":      _num(r.get("OWT (ms)")),
                   "AVG_VEL":  _num(r.get("Avg Vel")),
                   "INT_VEL":  _num(r.get("Int Vel"))}
                  for r in recs("Checkshots")]

    return {"header": header, "tops": tops, "dst": dst, "frac": frac,
            "core": core, "core_runs": [], "survey": survey, "ip_rows": ip_rows,
            "perfs": perfs, "checkshots": checkshots,
            "completion": recs("Completion Summary")}
