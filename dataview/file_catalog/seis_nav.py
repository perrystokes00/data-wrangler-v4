"""
seis_nav.py — read seismic NAVIGATION files, whatever shape they arrive in.

    from dataview.file_catalog.seis_nav import read_nav, NAV_EXTS
    nav = read_nav(path)        # -> None if this is not a nav file
    nav["lines"]                # {"A": [(lon, lat), ...], ...} WGS84
    nav["epsg"], nav["how"], nav["note"]

Place in dataview/file_catalog/ beside extract_core.py and crs_from_segy.py.

WHY THIS EXISTS
---------------
A SEG-Y's trace headers cannot be relied on for geometry, and that is not an
accident of one vendor:

  * there is no CRS field at all before Rev 2 (2017), and adoption is thin
  * byte positions move. Teapot's own load sheet states
        CDP X_COORD  81-84 and 189-193
        CDP Y_COORD  85-88 and 193-196
    where the standard says 181-188. A conforming reader finds ZEROS, and no
    CRS setting can rescue coordinates that were never read.
  * scalars (bytes 71-72) are applied inconsistently between contractors

The NAVIGATION file exists precisely because the industry already knows this.
It is the authoritative geometry, it states its own CRS because it has to, and
it is what a survey is actually delivered with. Treating it as the source —
rather than as a fallback for when the volume disappoints — is the correct
default, not a workaround.

Teapot arrived with one and the pipeline had nowhere to put it: six files
held, "no outline and no bbox", nothing on screen suggesting the answer was
sitting in the same folder.

WHAT IT READS
-------------
Column-oriented text with a line identifier, a point number and an X/Y pair —
the form GeoGraphix, Petrel, Kingdom and SMT all export, and the form a
contractor types when the survey predates all of them. The CRS comes from the
file's own header prose and is resolved by crs_from_segy.crs_from_text, the
SAME parser the SEG-Y branch uses: one place that knows "SPCS27 Wyoming East
Central + NAD 1927" is EPSG 32056, and "AMG ZONE 54 + GDA2020" is 7854.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It never guesses a CRS. A nav file whose header states no projection yields
`epsg=None` and the caller writes nothing — the same rule the SEG-Y branch
follows, and for the same reason: a confident wrong position plots, so nobody
re-checks it. P1/90 files keep their own reader; they are self-describing in a
formally specified way and do not need this.
"""
from __future__ import annotations

import os
import re

# Nav files are named by convention, not by extension — .txt, .dat, .nav, .csv
# all appear. The NAME is the signal, so the caller checks both.
NAV_EXTS = {".txt", ".dat", ".nav", ".p1", ".xyz"}
NAV_NAME_HINTS = ("nav", "navigation", "shotpoint", "shot_point", "sp_",
                  "coords", "coordinate", "geometry", "srvy", "survey_geom")

# A data row: line id, point number, X, Y — with the line id optional, because
# a single-line file often omits it. Both integer and decimal coordinates.
#
# TRAILING COLUMNS ARE ALLOWED, and must be. Anchoring straight after Y meant a
# row carrying an elevation —
#     A   235   797319  964035 5153
# — failed to match and fell through to the branch that treats an unmatched
# line as header PROSE. Three of Teapot's 535 shotpoints vanished that way,
# silently. A vendor who writes elevation on every row loses the whole file,
# and it fails as "not a nav file", which reads as if the file were absent
# rather than misparsed. Extra columns must still be NUMERIC, so a line of
# words cannot slip in as data.
_ROW = re.compile(
    r"^\s*(?:([A-Za-z0-9][\w\-]{0,19})\s+)?"      # line id (optional)
    r"(\d{1,7})\s+"                                # shot / CDP number
    r"(-?\d{1,9}(?:\.\d+)?)\s+"                    # X / easting
    r"(-?\d{1,9}(?:\.\d+)?)"                       # Y / northing
    r"(?:\s+-?\d{1,9}(?:\.\d+)?)*\s*$")            # elevation, depth, fold...

_MIN_POINTS = 4          # fewer than this is not a survey line


def _looks_like_nav(path: str, head: list[str]) -> bool:
    """Cheap test before committing to a full read."""
    name = os.path.basename(path).lower()
    if any(h in name for h in NAV_NAME_HINTS):
        return True
    # or it simply reads like one: several consecutive parseable rows
    hits = sum(1 for l in head if _ROW.match(l))
    return hits >= _MIN_POINTS


def read_nav(path: str, max_lines: int = 500_000) -> dict | None:
    """Parse a navigation file. Returns None if it is not one.

    The return is deliberately in the SOURCE CRS — reprojection is the
    caller's job, so this module needs no pyproj and can be unit-tested
    without it.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = [next(fh, "") for _ in range(60)]
    except OSError:
        return None
    if not _looks_like_nav(path, raw):
        return None

    header_text, lines, n = [], {}, 0
    order: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                n += 1
                if n > max_lines:
                    break
                m = _ROW.match(ln)
                if m:
                    key = (m.group(1) or "").strip().upper() or "_"
                    if key not in lines:
                        lines[key] = []
                        order.append(key)
                    lines[key].append((float(m.group(3)), float(m.group(4))))
                elif len(header_text) < 60 and ln.strip():
                    # Anything that is not a data row is header prose. Keep it
                    # for the CRS parser rather than pattern-matching it here —
                    # every vendor words it differently and the SEG-Y branch
                    # already owns that problem.
                    header_text.append(ln.rstrip())
    except OSError:
        return None

    lines = {k: v for k, v in lines.items() if len(v) >= _MIN_POINTS}
    if not lines:
        return None

    epsg = how = note = None
    try:
        from dataview.file_catalog.crs_from_segy import crs_from_text
        epsg, how, note = crs_from_text("\n".join(header_text))
    except Exception:
        pass

    return {
        "lines": {k: lines[k] for k in order if k in lines},
        "epsg": epsg, "how": how, "note": note,
        "header": "\n".join(header_text[:20]),
        "n_points": sum(len(v) for v in lines.values()),
    }


def to_wgs84(lines: dict, epsg: int) -> dict:
    """Reproject every line. Raises if pyproj is unavailable — the caller
    decides whether that is fatal, exactly as the SEG-Y branch does."""
    from pyproj import Transformer
    t = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    return {k: [t.transform(x, y) for x, y in v] for k, v in lines.items()}


def linestring(pts) -> str:
    """WKT is lon lat — the opposite order to how coordinates are spoken."""
    return "LINESTRING(" + ", ".join(f"{x:.7f} {y:.7f}" for x, y in pts) + ")"


def hull_polygon(all_pts) -> str | None:
    """A convex hull around every line — the survey's footprint.

    A 2D survey has no true outline; the lines ARE the survey. The hull is an
    honest summary of where it went, and it is what dv_seis_set.geog wants —
    that column takes POLYGONS ONLY, because a LINESTRING in it once broke the
    entire seismic map layer.
    """
    pts = sorted(set(all_pts))
    if len(pts) < 3:
        return None

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lo = []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    up = []
    for p in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], p) <= 0:
            up.pop()
        up.append(p)
    ring = lo[:-1] + up[:-1]
    if len(ring) < 3:
        return None
    ring.append(ring[0])
    return "POLYGON((" + ", ".join(f"{x:.7f} {y:.7f}" for x, y in ring) + "))"


def match_line(file_stem: str, line_keys) -> str | None:
    """Which nav line belongs to this SEG-Y.

    Matches on the line identifier appearing in the file name — 'lineA.sgy'
    against key 'A', 'NPR3_LINE_B_mig.sgy' against 'B'. Returns None rather
    than guessing: a wrong line is a real line drawn in the wrong place, which
    is worse than a survey with one line missing.
    """
    # Tokenise rather than substring-match. My first cut used
    # `stem.endswith(key)`, and 'unrelated_volumE' duly matched line 'E' — a
    # real line drawn in the wrong place, which is worse than a survey missing
    # one line. A single-character key must be a WORD in the name, not a
    # trailing letter.
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", file_stem.lower()) if t]
    best = None
    for k in line_keys:
        kk = str(k).lower()
        if not kk or kk == "_":
            continue
        hit = (kk in toks                          # 'a' in ['line','a','mig']
               or f"line{kk}" in toks              # 'linea'
               or f"ln{kk}" in toks                # 'lna'
               or f"l{kk}" in toks)                # 'la'
        if hit and (best is None or len(kk) > len(str(best))):
            best = k                               # longest key wins: B vs B2
    return best
