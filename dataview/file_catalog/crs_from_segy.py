"""Derive a CRS (EPSG) for a SEG-Y file from its TEXTUAL header.

WHY THE TEXT AND NOT THE NUMBERS: inferring a UTM zone from coordinate
magnitudes is what put the Australian surveys off Norway. The processors
wrote the answer down — "XY COORDINATES:AMG ZONE 54; SURVEY DATUM:GDA2020",
"Projection: [EPSG:28992]" — and reading it is not a guess.

The coordinate values are used ONLY to corroborate, never to derive. A
disagreement is reported, not resolved.
"""
import re

# GDA2020 / MGA zone N  -> 7800+N   (zones 46-59)
# GDA94   / MGA zone N  -> 28300+N  (zones 48-58)
# AGD66   / AMG zone N  -> 20200+N  (zones 48-58)
_AUS = {"GDA2020": 7800, "GDA94": 28300, "AGD84": 20300, "AGD66": 20200}

# Matched as PREFIXES, because these are typed by hand into a fixed-width
# EBCDIC block and the typos survive forever. The Tarata header genuinely
# reads "NEW ZEALAND TRANSVERSE MERCATOT" — an exact match finds nothing and
# reports a file with a perfectly clear CRS as unknown.
_NAMED = [
    ("NEW ZEALAND TRANSVERSE MERCAT", 2193),   # NZGD2000 / NZTM2000
    ("NZTM", 2193),
    ("NEW ZEALAND MAP GRID", 27200),           # NZGD49 / NZMG
    ("NZMG", 27200),
    ("RD NEW", 28992),                         # Amersfoort / RD New
]

# A datum on its own is enough when the country has one obvious grid.
_DATUM_ONLY = [("NZGD2000", 2193), ("NZGD49", 27200)]

# ── US State Plane ───────────────────────────────────────────────────────────
# NO HAND-MAINTAINED ZONE TABLE. There are 192 NAD27 and 338 NAD83 State Plane
# CRSs; a literal dict here would be wrong within a year and wrong silently.
# The zone NAME and the DATUM are READ from the prose exactly as everything
# else in this module is; only the CODE is looked up, from pyproj's copy of the
# EPSG registry. That keeps the law intact — nothing is inferred from
# coordinate magnitude.
#
# Teapot is why this exists. Its navigation file states
#     "Coordinate System: SPCS27 - Wyoming East Central  Datum: NAD 1927"
#     "Data Coordinate System Units: U.S. Survey Feet"
# which is a complete, unambiguous answer (EPSG 32056) — and with no branch to
# read it, read_nav returned epsg=None, extract_core discarded a perfectly
# parsed 532-point nav, and six files held as "no outline or bbox".
_DATUM_WORDS = [("SPCS27", "NAD27"), ("SPCS83", "NAD83"),
                ("NAD 1927", "NAD27"), ("NAD1927", "NAD27"), ("NAD27", "NAD27"),
                ("NAD 1983", "NAD83"), ("NAD1983", "NAD83"), ("NAD83", "NAD83")]

# Longest first, so 'III' is never read as 'II' + a stray 'I'.
_ROMAN_RE = re.compile(r"\bZONE\s+(VIII|VII|III|IX|IV|VI|II|I|V|X)\b")
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
          "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}

_EPSG_PROJECTED = None          # cached: query_crs_info is ~40ms and uncached


def _norm_zone(s):
    """'CALIFORNIA ZONE III' -> 'CALIFORNIA ZONE 3'.

    EPSG names NAD27 California zones in Roman numerals and NAD83 ones in
    Arabic, and a processor writes whichever they please. Normalising BOTH
    sides is a spelling rule, not an interpretation.
    """
    return _ROMAN_RE.sub(lambda m: "ZONE %d" % _ROMAN[m.group(1)], s)


def _projected_crs():
    """[(code, NAME_UPPER), ...] for EPSG projected CRSs, or None without
    pyproj. Cached — the query costs ~40ms every call and this runs per file."""
    global _EPSG_PROJECTED
    if _EPSG_PROJECTED is None:
        try:
            from pyproj.database import query_crs_info
            _EPSG_PROJECTED = [(c.code, c.name.upper()) for c in
                               query_crs_info(auth_name="EPSG",
                                              pj_types=["PROJECTED_CRS"])]
        except Exception:
            _EPSG_PROJECTED = []
    return _EPSG_PROJECTED or None


def spcs_from_text(t):
    """(epsg, note) for a stated US State Plane zone, else (None, reason).

    `t` is the header text already uppercased and whitespace-collapsed.

    Three things must all be present, and each is READ, never assumed:
      * a State Plane statement ("SPCS27", "STATE PLANE")
      * a datum (NAD27 / NAD83) — without it the same zone name is two
        different grids a few hundred metres apart
      * a zone name that matches an EPSG entry for that datum

    UNITS ARE PART OF THE ANSWER, NOT A DETAIL. Every NAD83 zone exists in both
    metres and US survey feet; picking the wrong one scales every coordinate by
    3.28. So when the units are not stated and both variants match, this
    REFUSES rather than choosing — the same rule the rest of the module
    follows. NAD27 zones are defined in feet only, so they need no such
    statement and are unaffected.
    """
    if not re.search(r"\bSPCS\s*(?:27|83)?\b|\bSTATE[\s_]*PLANE\b", t):
        return None, "no State Plane statement"
    datum = next((d for k, d in _DATUM_WORDS if k in t), None)
    if not datum:
        return None, "State Plane stated but no datum — not guessed"
    rows = _projected_crs()
    if rows is None:
        return None, "State Plane stated but pyproj is unavailable to resolve it"

    tn = _norm_zone(t)
    prefix = datum + " / "
    cands = []
    for code, name in rows:
        # Plain NAD83 only: NAD83(HARN)/(NSRS2007)/(2011) are separate
        # realisations and a header saying "NAD 1983" is not claiming one.
        if not name.startswith(prefix):
            continue
        zone = name[len(prefix):]
        ftus = zone.endswith("(FTUS)")
        base = _norm_zone(re.sub(r"\s*\(FTUS\)$", "", zone))
        if re.search(r"\b" + re.escape(base) + r"\b", tn):
            cands.append((int(code), name, ftus, len(base)))
    if not cands:
        return None, f"{datum} State Plane stated but no zone name matched"

    # 'Wyoming East Central' must beat 'Wyoming East', which also matches.
    longest = max(c[3] for c in cands)
    cands = [c for c in cands if c[3] == longest]

    want = None
    if re.search(r"\b(?:US|U\.S\.)\s*SURVEY\s*(?:FEET|FOOT|FT)\b", t) \
            or re.search(r"\bFT\s*US\b|\bFTUS\b", t):
        want = True
    elif re.search(r"\bMET(?:RE|ER)S?\b", t):
        want = False
    pick = cands
    if want is not None:
        # Fall back to every candidate when the stated units match nothing —
        # NAD27 zones carry no '(ftUS)' suffix because feet is all they are.
        pick = [c for c in cands if c[2] == want] or cands

    if len({c[0] for c in pick}) != 1:
        return None, ("State Plane zone matched but the units are not stated "
                      "and it exists as both: "
                      + ", ".join(sorted(c[1] for c in pick)))
    epsg, name, _ftus, _n = pick[0]
    return epsg, f"{name} (zone, datum and units stated in the header)"


def crs_from_text(txt):
    """(epsg, how, note) — how is 'declared', 'derived' or None."""
    if not txt:
        return None, None, "no textual header"
    t = " ".join(str(txt).upper().split())

    # 1. an EPSG code written out. Nothing to interpret.
    m = re.search(r"EPSG[:\s]*(\d{4,5})", t)
    if m:
        return int(m.group(1)), "declared", "EPSG stated in the header"

    # 2. a US State Plane zone named in prose. As DECLARED as an EPSG code —
    #    the zone, datum and units are all written down; only the code is
    #    looked up. See spcs_from_text.
    _e, _spcs_note = spcs_from_text(t)
    if _e:
        return _e, "declared", _spcs_note
    if _spcs_note == "no State Plane statement":
        _spcs_note = None       # nothing was claimed; nothing to report

    # 3. Australian grid + datum. NOTE the terminology clash: these headers
    #    say "AMG ZONE 54" (the pre-GDA grid) with "SURVEY DATUM:GDA2020".
    #    The DATUM decides — a GDA2020 survey on zone 54 is MGA zone 54,
    #    EPSG:7854, not the AGD-era AMG 20254. Getting this wrong shifts
    #    everything by roughly 200 m, which looks plausible and is not.
    m = re.search(r"\bAMG\s+ZONE\s+(\d{1,2})|\bMGA\s+ZONE\s+(\d{1,2})", t)
    if m:
        zone = int(m.group(1) or m.group(2))
        for datum, base in _AUS.items():
            if datum in t:
                return base + zone, "derived", (
                    f"{datum} + zone {zone}"
                    + (" (header says AMG but the datum is GDA — MGA applies)"
                       if "AMG" in t and datum.startswith("GDA") else ""))
        return None, None, f"zone {zone} found but no datum stated"

    # 4. generic UTM
    m = re.search(r"UTM\s+ZONE\s+(\d{1,2})\s*([NS])?", t)
    if m:
        zone, hemi = int(m.group(1)), (m.group(2) or "N")
        if "WGS84" in t or "WGS 84" in t:
            return (32600 if hemi == "N" else 32700) + zone, "derived", \
                   f"WGS84 UTM {zone}{hemi}"
        if "ED50" in t:
            return 23000 + zone, "derived", f"ED50 UTM {zone}N"
        return None, None, f"UTM zone {zone}{hemi} but no datum stated"

    # 5. named national grids (prefix match — see _NAMED)
    for name, epsg in _NAMED:
        if name in t:
            return epsg, "derived", name.title()
    for datum, epsg in _DATUM_ONLY:
        if datum in t:
            return epsg, "derived", f"{datum} datum, national grid assumed"
    # A header that CLAIMED State Plane and could not be resolved must say so.
    # "no CRS statement found" would be a lie — one was found and refused — and
    # a discarded diagnostic is what makes the next failure undiagnosable.
    return None, None, _spcs_note or "no CRS statement found"


def corroborate(epsg, x, y):
    """Does the coordinate magnitude FIT the declared CRS? Never derives."""
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        return "no coordinates to check"
    if x == 0 and y == 0:
        return "no coordinates to check"
    if abs(x) <= 180 and abs(y) <= 90:
        shape = "geographic degrees"
    elif 100_000 <= x <= 1_000_000 and 1_000_000 <= y <= 10_000_000:
        shape = "UTM/MGA-shaped metres"
    elif x > 1_000_000:
        shape = "large-easting grid (NZTM/NZMG/State Plane)"
    else:
        shape = "unrecognised magnitude"
    if epsg and 7846 <= epsg <= 7859 and shape != "UTM/MGA-shaped metres":
        return f"MISMATCH: declared MGA but coordinates look like {shape}"
    if epsg == 2193 and not (1_000_000 < x < 2_500_000):
        return f"MISMATCH: declared NZTM but easting {x:,.0f} is out of range"
    return f"consistent ({shape})"

# ── 3D survey corners (also read from the textual header) ────────────────────
# These lived in segy_lines_to_wgs84.py; they moved here so the extract path
# (extract_core) and the GeoJSON exporter share ONE implementation instead of
# two regex sets that drift apart.

def _ring_order(pts):
    """Sort points anticlockwise about their centroid — see survey_corners."""
    import math
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    return sorted(pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))


def survey_corners(text):
    """Four corner XY pairs from a 3D survey's textual header, or None.

    A 3D volume is an AREA. Sampling nine trace headers along trace ORDER
    zigzags across it and draws a scribble, not an outline — so where the
    header states corners, they win outright.

    Two layouts occur in Perry's data and they are not variations of one
    pattern, so they are matched separately rather than by a clever regex:

      OpendTect (delft, f3):
        C06   Corner 1:  X: 78401.95  Y: 447374.73  IL: 2500  XL: 3139
      hand-typed grid table (tarata):
        C12                       1       1      1705238.00     5660298.00

    Corners are returned in RING order, sorted by angle about their centroid.
    That reordering is not cosmetic: tarata's grid table lists (1,1) (1,421)
    (590,1) (590,421), which traced literally is a BOWTIE — 17,670 m2 instead
    of the survey's real 222,642,000 m2. Any four corners of a convex quad
    give the right ring once sorted this way, whatever order they were typed.
    """
    if not text:
        return None
    t = str(text)

    # Layout 1 — explicit "Corner n: X: .. Y: .."
    pts = [(float(x), float(y)) for x, y in re.findall(
        r"CORNER\s*\d+\s*:?\s*X\s*:?\s*(-?[\d.]+)\s+Y\s*:?\s*(-?[\d.]+)",
        t, re.I)]
    if len(pts) >= 3:
        return _ring_order(pts[:4])

    # Layout 3 — corners INDEXED BY INLINE/CROSSLINE, three of them:
    #     C 7 INLINE 1, XLINE 1:   X COORDINATE: 788937  Y COORDINATE: 938846
    #     C 8 INLINE 1, XLINE 188: X COORDINATE: 809502  Y COORDINATE: 939334
    #     C 9 INLINE 345, XLINE 1: X COORDINATE: 788039  Y COORDINATE: 976675
    #
    # Three points, not four — and that is enough, because the indices say
    # WHICH three. A 3D bin grid is a parallelogram by construction, so the
    # missing corner is P(il_far,xl_near) + P(il_near,xl_far) - P(near,near),
    # which is arithmetic, not an estimate. Ring-ordering three points instead
    # would draw a TRIANGLE over half the survey — a plausible-looking outline
    # in the right place, which is the kind of wrong nobody re-checks.
    #
    # Verified on Teapot: the derived fourth corner is (808604, 977163), the
    # exact value teapot_3d_load.doc states and the value the one-off loader
    # had hard-coded from that sheet.
    grid = {}
    for il, xl, x, y in re.findall(
            r"INLINE\s+(\d+)\s*,\s*XLINE\s+(\d+)\s*:?\s*"
            r"X\s*COORD(?:INATE)?\s*:?\s*(-?[\d.]+)\s+"
            r"Y\s*COORD(?:INATE)?\s*:?\s*(-?[\d.]+)", t, re.I):
        grid[(int(il), int(xl))] = (float(x), float(y))
    if len(grid) >= 4:
        return _ring_order(list(grid.values())[:4])
    if len(grid) == 3:
        ils = sorted({k[0] for k in grid})
        xls = sorted({k[1] for k in grid})
        if len(ils) == 2 and len(xls) == 2:
            full = [(i, x) for i in ils for x in xls]
            missing = [k for k in full if k not in grid]
            if len(missing) == 1:
                mi, mx = missing[0]
                # the corner diagonally opposite the gap, and its two neighbours
                opp = (ils[0] if mi == ils[1] else ils[1],
                       xls[0] if mx == xls[1] else xls[1])
                a = (mi, opp[1])
                b = (opp[0], mx)
                if opp in grid and a in grid and b in grid:
                    fourth = (grid[a][0] + grid[b][0] - grid[opp][0],
                              grid[a][1] + grid[b][1] - grid[opp][1])
                    return _ring_order(list(grid.values()) + [fourth])

    # Layout 2 — a GRID CORNERS block, then rows of "inline xline X Y".
    # Anchored on the heading so ordinary numeric lines elsewhere in the
    # header cannot be mistaken for corners.
    m = re.search(r"GRID\s+CORNERS", t, re.I)
    if m:
        rows = []
        for line in t[m.end():].splitlines()[:8]:
            nums = re.findall(r"-?\d+(?:\.\d+)?", line)
            # inline, xline, X, Y — take the last two, which are the coords.
            if len(nums) >= 4:
                rows.append((float(nums[-2]), float(nums[-1])))
        if len(rows) >= 3:
            return _ring_order(rows[:4])
    return None

