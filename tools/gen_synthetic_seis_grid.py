r"""Generate a synthetic 2D seismic grid on the dip and strike of the real lines.

WHY GEOMETRY AND NOT FILES. The catalogued 2D lines at Teapot are five: one
13 km strike line and four short dip lines. That is enough to prove the map
draws seismic and far too few to exercise anything that CHOOSES among lines --
the per-line checkboxes, the survey split, the second screen. Loading a
thousand synthetic SEG-Y files back in gives that, at the cost of a thousand
files and a pipeline run. A grid of LINE GEOMETRY gives the same exercise for
the map in a few seconds, because what the map draws is dv_seis_line.geog.

THE AZIMUTHS ARE MEASURED, NOT CHOSEN. Strike is the azimuth of the LONGEST
catalogued line (lineA, 160 deg, running the length of the field); dip is the
mean azimuth of the rest (lineB..E, 43-63 deg, mean ~54). So the grid lies
parallel to the survey that was actually shot rather than to a structural
interpretation this tool is in no position to make. Note the two are 106 deg
apart, NOT orthogonal -- the real survey is not square, and forcing a right
angle would be inventing a fact to make the picture tidier.

THE EXTENT IS THE FIELD, NOT A BOX. Lines are clipped to the NPR-3 Boundary
POLYGON, so they stop at the field edge the way a real programme does. A
bounding box would run lines out over ground the survey never covered.

EVERY ROW IS MARKED. source='SYNTH' (an already-registered dv_r_source code --
an import must not mint standards vocabulary), the set is named with
SYNTHETIC in it, and row_created_by is this tool. --remove deletes exactly
what it created and nothing else, keyed on the set id.

    python tools/gen_synthetic_seis_grid.py                  # plan only
    python tools/gen_synthetic_seis_grid.py --apply
    python tools/gen_synthetic_seis_grid.py --spacing 500 --apply
    python tools/gen_synthetic_seis_grid.py --remove --apply
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

SET_ID = "SYNTH-TPD-GRID"
SET_NAME = "NPR-3 TEAPOT DOME SYNTHETIC GRID"
BOUNDARY_LAYER = "NPR-3 Boundary"

# Rotated across the generated lines so the Processing filter has something to
# filter. These are part of the SYNTHETIC fiction, like the geometry -- the set
# says SYNTHETIC and the source says SYNTH, so nothing here can be read as a
# claim about real RMOTC data.
_STAGES = ["POSTM", "PRESTM", "PRESDM", "PRE_MIG"]


def _enu(lat, lon, lat0, lon0):
    """Local metres east/north about (lat0, lon0). Good to centimetres over a
    field 12 km across, and it keeps the geometry in plain arithmetic."""
    return ((lon - lon0) * 111320.0 * math.cos(math.radians(lat0)),
            (lat - lat0) * 110574.0)


def _latlon(x, y, lat0, lon0):
    return (lat0 + y / 110574.0,
            lon0 + x / (111320.0 * math.cos(math.radians(lat0))))


def _azimuth(p0, p1):
    """Azimuth of a segment in degrees, folded to 0-180 (a line has no way
    round). lat/lon in, degrees out."""
    la0, lo0 = p0
    la1, lo1 = p1
    x = (lo1 - lo0) * math.cos(math.radians((la0 + la1) / 2.0))
    y = la1 - la0
    return math.degrees(math.atan2(x, y)) % 180.0


def _boundary(engine):
    """[(lat, lon)] exterior ring of the field boundary, or None."""
    from sqlalchemy import text
    with engine.connect() as c:
        gj = c.execute(text(
            "SELECT geometry_wkt FROM dataview.dv_spatial_layer "
            "WHERE layer_name = :n"), {"n": BOUNDARY_LAYER}).scalar()
    if not gj:
        return None
    try:
        d = json.loads(gj)
    except (TypeError, ValueError):
        return None
    # THE BOUNDARY IS A LINE, NOT A POLYGON. RMOTC ships NPR-3 as a
    # MultiLineString -- the outline TRACED rather than filled -- so a reader
    # that only understands Polygon finds nothing and reports "load the GIS
    # layers first", which is both wrong and unhelpful. Take rings from either.
    feats = d.get("features") or []
    best = None
    for f in feats:
        g = (f or {}).get("geometry") or {}
        t = g.get("type")
        rings = []
        if t == "Polygon":
            rings = [g.get("coordinates", [[]])[0]]
        elif t == "MultiPolygon":
            rings = [p[0] for p in g.get("coordinates", []) if p]
        elif t == "LineString":
            rings = [g.get("coordinates", [])]
        elif t == "MultiLineString":
            rings = list(g.get("coordinates", []))
        for r in rings:
            # THE BIGGEST RING IS THE FIELD. A boundary file can carry slivers.
            if r and (best is None or len(r) > len(best)):
                best = r
    if not best:
        return None
    ring = [(float(c[1]), float(c[0])) for c in best]
    # CLOSE IT IF THE FILE DID NOT. Clipping walks edges i -> i+1 mod n, so an
    # open ring silently grows one phantom edge from the last vertex back to
    # the first -- which is exactly the closing edge, but only by luck of the
    # data being nearly closed. Say so instead of relying on it.
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _existing_lines(engine):
    """[(name, [(lat,lon)...])] for every catalogued 2D line with geometry."""
    from dataview.core.dw_utils import make_engine  # noqa: F401  (import check)
    from dataview.mapping.page_well_map import _seismic_line_paths
    out = []
    for sl in _seismic_line_paths(engine):
        pts = sl.get("pts") or []
        if len(pts) >= 2:
            out.append((sl.get("line"), pts))
    return out


def _clip(poly_xy, p0, d, half_len):
    """The longest span of an infinite line INSIDE a polygon, or None.

    EVEN-ODD, NOT OUTERMOST. Taking min(t) and max(t) of the crossings is only
    right for a convex boundary. NPR-3 has 93 vertices and follows section
    lines, so it is emphatically concave, and the outermost pair spans the
    re-entrants as well -- three of fifteen generated lines had their MIDPOINT
    outside the field, and the grid ran past the boundary it was supposed to be
    clipped to. Lines drawn over ground the survey never covered is the exact
    thing clipping exists to prevent, and it looks entirely plausible on a map.

    So: sort the crossings and pair them. Between t[0] and t[1] is inside,
    t[1]..t[2] is outside, and so on. The longest inside span is the line; a
    real crew shoots the main traverse, not every fragment.
    """
    ts = []
    n = len(poly_xy)
    for i in range(n):
        a = poly_xy[i]
        b = poly_xy[(i + 1) % n]
        ex, ey = b[0] - a[0], b[1] - a[1]
        den = d[0] * ey - d[1] * ex
        if abs(den) < 1e-12:
            continue
        ax, ay = a[0] - p0[0], a[1] - p0[1]
        u = (ax * ey - ay * ex) / den          # along the line
        # SIGN MATTERS AND IT WAS WRONG. Solving p0 + u*d = a + v*e by Cramer
        # gives v = (ax*dy - ay*dx)/den; dividing by -den instead flips it, so
        # the 0..1 test accepted crossings on the far side of each edge. The
        # symptom was subtle -- lines poking 70-110 m past the field boundary,
        # entirely plausible on a map and only caught by sampling points along
        # each line and testing them against the ring.
        v = (ax * d[1] - ay * d[0]) / den      # along the edge, 0..1
        if -1e-9 <= v <= 1.0 + 1e-9:
            ts.append(u)
    if len(ts) < 2:
        return None
    ts.sort()
    # A CROSSING THROUGH A VERTEX IS FOUND TWICE, once for each edge that
    # shares it, which flips the parity and turns inside into outside for the
    # rest of the line. Collapse duplicates before pairing.
    uniq = [ts[0]]
    for t in ts[1:]:
        if t - uniq[-1] > 1e-6:
            uniq.append(t)
    best = None
    for i in range(0, len(uniq) - 1, 2):
        lo, hi = uniq[i], uniq[i + 1]
        if best is None or (hi - lo) > (best[1] - best[0]):
            best = (lo, hi)
    if best is None or (best[1] - best[0]) < 50.0:   # a 50 m stub is not a line
        return None
    lo = max(best[0], -half_len)
    hi = min(best[1], half_len)
    return [(p0[0] + d[0] * lo, p0[1] + d[1] * lo),
            (p0[0] + d[0] * hi, p0[1] + d[1] * hi)]


def build(engine, spacing_m):
    """[(line_name, [(lat,lon),(lat,lon)])] plus the azimuths used."""
    ring = _boundary(engine)
    if not ring:
        raise SystemExit("No '%s' polygon in dv_spatial_layer -- load the GIS "
                         "layers first (Spatial Loader)." % BOUNDARY_LAYER)
    lines = _existing_lines(engine)
    if not lines:
        raise SystemExit("No catalogued 2D lines with geometry, so there is no "
                         "dip or strike to copy. Load the real seismic first.")

    # STRIKE IS THE LONGEST LINE. It runs the length of the field; the short
    # ones cross it. Measuring beats assuming: this is a fold, not a grid.
    def _len(pts):
        (la0, lo0), (la1, lo1) = pts[0], pts[-1]
        x, y = _enu(la1, lo1, la0, lo0)
        return math.hypot(x, y)

    lines.sort(key=lambda t: _len(t[1]), reverse=True)
    strike = _azimuth(lines[0][1][0], lines[0][1][-1])
    rest = [_azimuth(p[0], p[-1]) for _n, p in lines[1:]] or [(strike + 90) % 180]
    dip = sum(rest) / len(rest)

    lat0 = sum(p[0] for p in ring) / len(ring)
    lon0 = sum(p[1] for p in ring) / len(ring)
    poly = [_enu(la, lo, lat0, lon0) for la, lo in ring]
    half = max(math.hypot(*p) for p in poly) * 2.0

    out = []
    for tag, az in (("S", strike), ("D", dip)):
        th = math.radians(az)
        d = (math.sin(th), math.cos(th))        # along the line
        p = (math.cos(th), -math.sin(th))       # across it
        offs = [q[0] * p[0] + q[1] * p[1] for q in poly]
        lo, hi = min(offs), max(offs)
        k = 0
        t = lo + spacing_m / 2.0
        while t < hi:
            seg = _clip(poly, (p[0] * t, p[1] * t), d, half)
            t += spacing_m
            if not seg:
                continue
            k += 1
            nm = "TPD-%s%02d_%s_PROCESSED" % (
                tag, k, _STAGES[(k - 1) % len(_STAGES)])
            out.append((nm, [_latlon(x, y, lat0, lon0) for x, y in seg]))
    return out, strike, dip, ring


def remove(engine):
    from sqlalchemy import text
    with engine.begin() as c:
        n = c.execute(text("DELETE FROM dataview.dv_seis_line "
                           "WHERE seis_set_id = :s"), {"s": SET_ID}).rowcount
        m = c.execute(text("DELETE FROM dataview.dv_seis_set "
                           "WHERE seis_set_id = :s"), {"s": SET_ID}).rowcount
    return n, m


def write(engine, rows, ring):
    from sqlalchemy import text
    ring_wkt = "POLYGON((%s))" % ", ".join(
        "%.8f %.8f" % (lo, la) for la, lo in
        (list(ring) + [ring[0]] if ring[0] != ring[-1] else list(ring)))
    with engine.begin() as c:
        c.execute(text("DELETE FROM dataview.dv_seis_line "
                       "WHERE seis_set_id = :s"), {"s": SET_ID})
        c.execute(text("DELETE FROM dataview.dv_seis_set "
                       "WHERE seis_set_id = :s"), {"s": SET_ID})
        # RING ORIENTATION IS NOT COSMETIC. SQL Server geography uses the
        # left-hand rule, so a clockwise exterior ring describes EVERYTHING
        # EXCEPT the field -- a polygon covering the planet, which is a silent
        # wrong answer rather than an error. ReorientObject when the area comes
        # back larger than any oil field could be.
        c.execute(text("""
            DECLARE @g geography = geography::STGeomFromText(:w, 4326).MakeValid();
            IF @g.STArea() > 1.0e10 SET @g = @g.ReorientObject();
            INSERT INTO dataview.dv_seis_set
                  (seis_set_id, seis_set_name, seis_set_type, source,
                   geog, active_ind, row_created_by)
            VALUES (:sid, :nm, '2D', 'SYNTH', @g, 'Y', 'GEN_SYNTH_GRID')
        """), {"w": ring_wkt, "sid": SET_ID, "nm": SET_NAME})
        for i, (nm, pts) in enumerate(rows, 1):
            wkt = "LINESTRING(%s)" % ", ".join(
                "%.8f %.8f" % (lo, la) for la, lo in pts)
            c.execute(text("""
                INSERT INTO dataview.dv_seis_line
                      (seis_set_id, line_id, line_name, source, geog,
                       active_ind, row_created_by)
                VALUES (:sid, :lid, :nm, 'SYNTH',
                        geography::STGeomFromText(:w, 4326),
                        'Y', 'GEN_SYNTH_GRID')
            """), {"sid": SET_ID, "lid": "%s-%03d" % (SET_ID, i),
                   "nm": nm, "w": wkt})
    return len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Synthetic 2D grid on the dip and strike of the real lines.")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--spacing", type=float, default=1000.0,
                    help="line spacing in metres (default 1000)")
    ap.add_argument("--remove", action="store_true",
                    help="delete the generated set and exit")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    engine = make_engine(a.database)

    if a.remove:
        if not a.apply:
            print("Would delete set %s. Re-run with --apply." % SET_ID)
            return 0
        n, m = remove(engine)
        print("Deleted %d line(s) and %d set(s)." % (n, m))
        return 0

    rows, strike, dip, ring = build(engine, a.spacing)
    ns = len([r for r in rows if r[0].startswith("TPD-S")])
    nd = len(rows) - ns
    print("Field boundary : %s, %d vertices" % (BOUNDARY_LAYER, len(ring)))
    print("Strike azimuth : %5.1f deg  (longest catalogued line)" % strike)
    print("Dip azimuth    : %5.1f deg  (mean of the rest)" % dip)
    print("Spacing        : %.0f m" % a.spacing)
    print("Lines          : %d strike + %d dip = %d" % (ns, nd, len(rows)))
    print()
    for nm, pts in rows[:6]:
        (la0, lo0), (la1, lo1) = pts[0], pts[-1]
        x, y = _enu(la1, lo1, la0, lo0)
        print("   %-28s %5.2f km" % (nm, math.hypot(x, y) / 1000.0))
    if len(rows) > 6:
        print("   ... and %d more" % (len(rows) - 6))
    if not a.apply:
        print("\nPLAN ONLY -- nothing written. Re-run with --apply.")
        return 0
    n = write(engine, rows, ring)
    print("\nWrote %d synthetic line(s) into %s." % (n, SET_NAME))
    print("Remove them with:  python tools/gen_synthetic_seis_grid.py "
          "--remove --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
