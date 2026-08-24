r"""Field outline, leases, boundaries and gathering lines for Teapot Dome.

TIED TO THE SAME MODEL AS EVERYTHING ELSE. The field polygon is not a drawn
blob: it is a CONTOUR OF THE RESERVOIR HORIZON, so the productive outline is
the dome's own structural closure and the wells inside it are the ones the
production model already made good. Leases are a PLSS-style section grid over
that, the reserve boundary encloses it, and the gathering lines actually run
from well clusters to a battery.

A field outline drawn independently of the structure would put producers
outside the field and dry holes inside it, and every one of those is a fact
someone would later have to explain.

SQL Server geography is not planar geometry. A polygon ring must wind
counter-clockwise (left-hand rule) or the shape means the REST OF THE PLANET,
and the give-away is an area of ~510 million km2 rather than a syntax error.
ring_ccw() enforces it here rather than leaving it to MakeValid.
"""
import math

import numpy as np

from dataview.migration.synth_seismic import TEAPOT_AREA, TEAPOT_CREST

CREATED_BY = "SYNTH_GEOGRAPHY"
OPERATOR = "NAVAL PETROLEUM RESERVE OPERATIONS"

# NPR-3's real extent is about 9,481 acres (38 km2). The reserve boundary is a
# survey rectangle, not a geological one -- it was drawn on a plat, so it does
# not follow the structure and should not look as though it does.
RESERVE_HALF_NS_KM = 5.6
RESERVE_HALF_EW_KM = 3.4

SECTION_MI = 1.0                 # a PLSS section is one mile square


# Working-interest owners. NPR-3 is a federal reserve, so the operator of
# record stays with it; the rest are the kind of mix any mature field carries
# once interests have been traded -- a couple of majors, a handful of
# independents, and a royalty holder who never operates anything.
#
# NAMES ARE DELIBERATELY FICTIONAL. Putting a real company on synthetic acreage
# produces a lease map that looks like a public record and is not one, and the
# first person to screenshot it will not add the caveat.
LEASE_OWNERS = [
    ("Naval Petroleum Reserve Operations", "#c0392b", 0.28),
    ("Sweetwater Resources LLC",           "#2980b9", 0.18),
    ("Bighorn Basin Energy Co",            "#27ae60", 0.15),
    ("Salt Creek Minerals Trust",          "#8e44ad", 0.12),
    ("Casper Ridge Petroleum",             "#e67e22", 0.12),
    ("Powder River Royalty Partners",      "#16a085", 0.09),
    ("Unleased federal acreage",           "#7f8c8d", 0.06),
]


def _pick_owner(rng):
    r = rng.random()
    acc = 0.0
    for name, colour, share in LEASE_OWNERS:
        acc += share
        if r <= acc:
            return name, colour
    return LEASE_OWNERS[0][0], LEASE_OWNERS[0][1]


def _km_per_deg(lat):
    """(north-south, east-west) km per degree at a latitude."""
    return 110.574, 111.320 * math.cos(math.radians(lat))


def ring_ccw(pts):
    """Close a ring and wind it counter-clockwise for geography.

    THE SIGNED AREA IS THE TEST. A clockwise exterior ring in SQL Server's
    geography type is not an error -- it is the complement, the whole earth
    minus your polygon, and it draws as a map with a hole in it. Checking the
    shoelace sign here is cheaper than explaining an area of 5.1e8 km2 later.
    """
    p = [(float(a), float(b)) for a, b in pts]
    if p[0] != p[-1]:
        p.append(p[0])
    s = 0.0
    for (x1, y1), (x2, y2) in zip(p, p[1:]):
        s += (x2 - x1) * (y2 + y1)
    if s > 0:                     # clockwise -> reverse
        p.reverse()
    return p


def wkt_polygon(lonlat_ring):
    r = ring_ccw(lonlat_ring)
    return "POLYGON((" + ", ".join(f"{x:.7f} {y:.7f}" for x, y in r) + "))"


def wkt_line(lonlat):
    return "LINESTRING(" + ", ".join(f"{x:.7f} {y:.7f}" for x, y in lonlat) + ")"


def field_outline(surfaces, horizon_idx=3, level_frac=0.50):
    """The productive outline: a closing contour of the reservoir horizon.

    level_frac picks the contour between crest and deepest as a fraction of
    the relief -- lower is a tighter outline. The LARGEST closed ring at that
    level is the field; smaller ones are satellite closures and noise.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    lats, lons, vals = surfaces.grids[horizon_idx]
    lo, hi = float(np.nanmin(vals)), float(np.nanmax(vals))
    level = lo + (hi - lo) * level_frac

    fig = plt.figure()
    try:
        ax = fig.add_subplot(111)
        cs = ax.contour(lons, lats, vals, levels=[level])
        best = None
        for segs in cs.allsegs:
            for seg in segs:
                if len(seg) < 8:
                    continue
                # A field outline must CLOSE. An open contour is the level
                # running off the edge of the grid, which is a statement about
                # the grid, not about the field.
                if math.dist(seg[0], seg[-1]) > 0.01:
                    continue
                area = abs(sum((seg[i][0] * seg[i + 1][1]
                                - seg[i + 1][0] * seg[i][1])
                               for i in range(len(seg) - 1))) / 2.0
                if best is None or area > best[0]:
                    best = (area, seg)
        if best is None:
            return None, level
        return [(float(x), float(y)) for x, y in best[1]], level
    finally:
        plt.close(fig)


def reserve_boundary(lon=None, lat=None):
    """The NPR-3 survey rectangle -- a plat boundary, not a geological one."""
    lon = TEAPOT_CREST[0] if lon is None else lon
    lat = TEAPOT_CREST[1] if lat is None else lat
    kns, kew = _km_per_deg(lat)
    dlat = RESERVE_HALF_NS_KM / kns
    dlon = RESERVE_HALF_EW_KM / kew
    return [(lon - dlon, lat - dlat), (lon + dlon, lat - dlat),
            (lon + dlon, lat + dlat), (lon - dlon, lat + dlat)]


def _aliquot_label(cells, ncol):
    """A legal-style description of which quarters a lease covers."""
    QUARTER = {(0, 0): "SW/4", (0, 1): "SE/4", (1, 0): "NW/4", (1, 1): "NE/4"}
    secs = {}
    for r, c in cells:
        secs.setdefault((r // 2, c // 2), []).append(QUARTER[(r % 2, c % 2)])
    parts = []
    for (sr, sc), qs in sorted(secs.items()):
        num = sr * max(1, ncol // 2) + sc + 1
        parts.append(("All" if len(qs) == 4 else ", ".join(sorted(qs)))
                     + f" Sec {num}")
    return "; ".join(parts[:4]) + (" ..." if len(parts) > 4 else "")


def lease_parcels(bounds=None, lat=None, rng=None, quarter_mi=0.5,
                  size=(2, 9), unleased=0.10):
    """[(name, legal, ring, owner, colour)] -- irregular aliquot leases.

    A LEASE IS NOT A SQUARE. In a PLSS state it is assembled out of aliquot
    parts -- "the NW/4 of Section 12, the S/2 of Section 13" -- so its outline
    is rectilinear but STEPPED, and no two are the same size. A tidy grid of
    identical sections is a township diagram, not a lease map, and anyone who
    has read a lease description reads the difference immediately.

    So: quarter the ground, then grow each lease by flood-fill over adjacent
    quarters until it reaches its size. Unioning the cells dissolves the
    internal edges and leaves exactly the stepped outline a real parcel has.

    `unleased` leaves a share of the ground open, because a lease map with no
    gaps in it is a map of something nobody had to negotiate.
    """
    import random
    from shapely.geometry import box
    from shapely.ops import unary_union

    rng = rng or random.Random(7)
    ring = bounds or reserve_boundary()
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    lat = lat if lat is not None else (min(lats) + max(lats)) / 2.0
    kns, kew = _km_per_deg(lat)
    dlat = (quarter_mi * 1.609344) / kns
    dlon = (quarter_mi * 1.609344) / kew
    ncol = max(2, int(round((max(lons) - min(lons)) / dlon)))
    nrow = max(2, int(round((max(lats) - min(lats)) / dlat)))

    free = {(r, c) for r in range(nrow) for c in range(ncol)
            if rng.random() > unleased}
    out, n = [], 0
    while free:
        seed = min(free)                       # deterministic sweep order
        target = rng.randint(*size)
        cells, frontier = set(), [seed]
        while frontier and len(cells) < target:
            cur = frontier.pop(rng.randrange(len(frontier)))
            if cur not in free:
                continue
            free.discard(cur)
            cells.add(cur)
            r, c = cur
            for nb in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
                if nb in free:
                    frontier.append(nb)
        if not cells:
            continue
        geom = unary_union([
            box(min(lons) + c * dlon, max(lats) - (r + 1) * dlat,
                min(lons) + (c + 1) * dlon, max(lats) - r * dlat)
            for r, c in cells])
        if geom.geom_type == "MultiPolygon":
            # Flood-fill over a set with holes in it can strand a cell; keep
            # the body and let the stray go back to unleased rather than
            # writing a lease that is two disconnected pieces.
            geom = max(geom.geoms, key=lambda g: g.area)
        n += 1
        _own, _col = _pick_owner(rng)
        out.append((f"NPR-3 Lease {n:02d}",
                    _aliquot_label(cells, ncol),
                    [(float(x), float(y)) for x, y in geom.exterior.coords],
                    _own, _col))
    return out


def gathering_system(wells, battery=None, max_spur_km=3.0):
    """[(name, [lonlat...])] -- spurs from producers to a battery, plus a trunk.

    Only PRODUCERS are connected. Running a flowline to a dry hole is the kind
    of detail that looks like data and is nonsense on inspection.
    """
    prod = [w for w in wells
            if (w.get("_months") or 0) > 0
            and w.get("surface_latitude") and w.get("surface_longitude")]
    if not prod:
        return []
    bx = battery[0] if battery else float(np.mean([w["surface_longitude"] for w in prod]))
    by = battery[1] if battery else float(np.mean([w["surface_latitude"] for w in prod]))
    kns, kew = _km_per_deg(by)

    lines = []
    for w in prod:
        x, y = float(w["surface_longitude"]), float(w["surface_latitude"])
        d = math.hypot((x - bx) * kew, (y - by) * kns)
        if d > max_spur_km:
            continue
        # One dogleg, so the spur reads as a route rather than a ray: run in
        # the easting first, then the northing, the way a flowline follows a
        # lease road.
        lines.append((f"Flowline {w['well_name']}",
                      [(x, y), (bx, y), (bx, by)]))
    # The sales line out of the battery. Kept short on purpose -- a trunk
    # drawn to its real length doubles the extent of every map that
    # includes it, and the tie-in is the part that matters here.
    lines.append(("NPR-3 Sales Line",
                  [(bx, by), (bx + 0.022, by + 0.018),
                   (bx + 0.046, by + 0.041)]))
    return lines
