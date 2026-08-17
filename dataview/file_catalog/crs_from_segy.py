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


def crs_from_text(txt):
    """(epsg, how, note) — how is 'declared', 'derived' or None."""
    if not txt:
        return None, None, "no textual header"
    t = " ".join(str(txt).upper().split())

    # 1. an EPSG code written out. Nothing to interpret.
    m = re.search(r"EPSG[:\s]*(\d{4,5})", t)
    if m:
        return int(m.group(1)), "declared", "EPSG stated in the header"

    # 2. Australian grid + datum. NOTE the terminology clash: these headers
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

    # 3. generic UTM
    m = re.search(r"UTM\s+ZONE\s+(\d{1,2})\s*([NS])?", t)
    if m:
        zone, hemi = int(m.group(1)), (m.group(2) or "N")
        if "WGS84" in t or "WGS 84" in t:
            return (32600 if hemi == "N" else 32700) + zone, "derived", \
                   f"WGS84 UTM {zone}{hemi}"
        if "ED50" in t:
            return 23000 + zone, "derived", f"ED50 UTM {zone}N"
        return None, None, f"UTM zone {zone}{hemi} but no datum stated"

    # 4. named national grids (prefix match — see _NAMED)
    for name, epsg in _NAMED:
        if name in t:
            return epsg, "derived", name.title()
    for datum, epsg in _DATUM_ONLY:
        if datum in t:
            return epsg, "derived", f"{datum} datum, national grid assumed"
    return None, None, "no CRS statement found"


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

