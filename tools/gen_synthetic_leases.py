r"""Irregular lease tracts with ownership, over the wells that exist.

dv_land_tract is empty, so the Leases chip draws nothing. This fills it with
tracts that look like leases rather than like a chessboard: a jittered lattice
tiled into quads, some of them merged into L-shapes.

IRREGULAR, BUT STILL TILING. The jitter is applied to the LATTICE, not to each
polygon, so neighbouring tracts share the moved corner and the boundaries stay
coincident. Perturbing each polygon on its own would open slivers between them
-- gaps a well can fall into, which is exactly the kind of quietly wrong answer
"which lease is this well on" must not give.

ORIENTATION IS NOT COSMETIC. A SQL Server geography POLYGON is the region to
the LEFT of its ring, so a clockwise ring is the whole planet minus the tract:
STArea comes back in the hundreds of millions of km2 and every well on Earth
"intersects" it. Each ring is checked and reoriented -- the same guard
_load_seis already applies to survey outlines.

SCOPED FOR REMOVAL. Every row is stamped source='SYNTH_LEASE', so --remove
takes exactly these and nothing a real load ever wrote.

    python tools/gen_synthetic_leases.py                 # what it would make
    python tools/gen_synthetic_leases.py --apply
    python tools/gen_synthetic_leases.py --remove --apply
"""
import argparse
import math
import os
import random
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOURCE_STAMP = "SYNTH_LEASE"

# Ownership. The first four are the operators actually present on these wells,
# so a lease map reads against the well data instead of beside it; the rest are
# lessors invented to give the map more than one colour.
OWNERS = [
    ("U.S. DOE", 0.30),
    ("Mammoth Production", 0.18),
    ("RMOTC", 0.12),
    ("FENIX & SCISSON", 0.08),
    ("Salt Creek Royalty LP", 0.12),
    ("Powder River Minerals", 0.10),
    ("Natrona Land & Cattle", 0.10),
]

TRACT_WORDS = ["Teapot", "Shannon", "Sussex", "Tensleep", "Wall Creek",
               "Steele", "Niobrara", "Parkman", "Muddy", "Frontier",
               "Cloverly", "Dakota", "Lakota", "Morrison", "Sundance"]


def _lattice(min_lat, max_lat, min_lon, max_lon, nx, ny, jitter, rnd):
    """(ny+1) x (nx+1) points, each nudged. Shared corners stay shared."""
    dlat = (max_lat - min_lat) / ny
    dlon = (max_lon - min_lon) / nx
    pts = {}
    for j in range(ny + 1):
        for i in range(nx + 1):
            lat = min_lat + j * dlat
            lon = min_lon + i * dlon
            # The outer boundary stays put, so the tracts tile a clean
            # rectangle rather than a ragged edge that looks like a bug.
            if 0 < i < nx:
                lon += rnd.uniform(-jitter, jitter) * dlon
            if 0 < j < ny:
                lat += rnd.uniform(-jitter, jitter) * dlat
            pts[(i, j)] = (lat, lon)
    return pts


def _ring_wkt(coords):
    """A closed WKT ring, counter-clockwise.

    Shoelace on the raw lat/lon is enough at this scale: the tracts are a few
    km across, where the difference between planar and spherical winding does
    not change the sign.
    """
    pts = list(coords)
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    area2 = 0.0
    for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
        area2 += (lo1 * la2) - (lo2 * la1)
    if area2 < 0:                      # clockwise -> flip
        pts = list(reversed(pts))
    return "POLYGON((%s))" % ", ".join("%.8f %.8f" % (lo, la) for la, lo in pts)


def build(min_lat, max_lat, min_lon, max_lon, nx=6, ny=7, seed=20260827):
    """[(name, lease_no, owner, wkt)] tiling the extent, some cells merged."""
    rnd = random.Random(seed)
    pts = _lattice(min_lat, max_lat, min_lon, max_lon, nx, ny, 0.28, rnd)

    # Merge a few horizontal neighbours into L-shaped / wide tracts. Real
    # leases are not all one size, and a map of identical quads reads as
    # generated -- which it is, but it should not look it.
    merged = set()
    pairs = []
    for j in range(ny):
        for i in range(nx - 1):
            if (i, j) in merged or (i + 1, j) in merged:
                continue
            if rnd.random() < 0.22:
                pairs.append(((i, j), (i + 1, j)))
                merged.add((i, j))
                merged.add((i + 1, j))

    def cell_ring(i, j, span=1):
        """The ring, walking EVERY lattice node along each edge.

        A merged tract spanning two cells must still turn at the node
        between them. Cutting the corner draws a straight edge where the
        neighbour above and below jogs, and the two boundaries stop
        coinciding -- which put 92 of 1,373 wells inside two leases at once
        on the first run. Gaps and overlaps are the same mistake; this one
        happened to overlap, so no well fell through and it would have gone
        unnoticed but for counting wells in more than one tract.
        """
        bottom = [pts[(i + k, j)] for k in range(span + 1)]
        top = [pts[(i + k, j + 1)] for k in range(span, -1, -1)]
        return bottom + top

    out = []
    used_names = set()

    def name_for():
        while True:
            nm = "%s %s" % (rnd.choice(TRACT_WORDS),
                            rnd.choice(["Unit", "Tract", "Lease", "Federal",
                                        "State", "Fee"]))
            if nm not in used_names:
                used_names.add(nm)
                return nm

    owners = [o for o, _w in OWNERS]
    weights = [w for _o, w in OWNERS]

    for (i, j), _ in pairs:
        out.append((name_for(), cell_ring(i, j, span=2)))
    for j in range(ny):
        for i in range(nx):
            if (i, j) in merged:
                continue
            out.append((name_for(), cell_ring(i, j)))

    recs = []
    for n, (nm, ring) in enumerate(out, 1):
        owner = rnd.choices(owners, weights=weights, k=1)[0]
        recs.append({
            "name": nm,
            # A PLAIN NUMBER, BECAUSE IT GETS SPOKEN. The point of these is
            # to be asked for out loud -- "wells drilled below 5000 since
            # 1980 in lease 5" -- and "WYW-04207" is not a thing anyone says
            # to a query box. The realistic identifier is the tract NAME,
            # which these also carry.
            "lease_no": str(n),
            "owner": owner,
            "wkt": _ring_wkt(ring),
        })
    return recs


def write(engine, recs, apply=False):
    from sqlalchemy import text
    if not apply:
        return 0
    n = 0
    with engine.begin() as c:
        for r in recs:
            # REORIENT IF THE RING CAME OUT INSIDE-OUT. Same guard as
            # _load_seis: anything larger than a continent is the complement
            # of the tract, not the tract.
            c.execute(text("""
                INSERT INTO dataview.dv_land_tract
                    (land_tract_id, tract_name, lease_number, operator_name,
                     province_state, country, area_km2, geog, active_ind,
                     source, row_created_by, row_created_date)
                -- REORIENT FIRST, THEN MEASURE. Measuring the raw ring while
                -- storing the reoriented one records the size of the
                -- COMPLEMENT -- half a billion km2 for a tract a few km
                -- across. Latent here because none of these rings came out
                -- clockwise; found when the same pattern, copied into the
                -- map's draw-a-boundary writer, met one that did.
                SELECT :id, :nm, :ln, :own, 'Wyoming', 'USA',
                       g2.STArea()/1000000.0, g2,
                       'Y', :src, :src, GETUTCDATE()
                  FROM (SELECT CASE WHEN g.STArea()/1000000.0 > 100000
                                    THEN g.ReorientObject() ELSE g END AS g2
                          FROM (SELECT geography::STGeomFromText(:wkt, 4326)
                                       .MakeValid() AS g) q1) q
            """), {"id": uuid.uuid4().hex[:40].upper(), "nm": r["name"],
                   "ln": r["lease_no"], "own": r["owner"],
                   "wkt": r["wkt"], "src": SOURCE_STAMP})
            n += 1
    return n


def remove(engine, apply=False):
    from sqlalchemy import text
    with engine.connect() as c:
        n = c.execute(text("SELECT COUNT(*) FROM dataview.dv_land_tract "
                           "WHERE source = :s"), {"s": SOURCE_STAMP}).scalar()
    if not apply:
        return n
    with engine.begin() as c:
        r = c.execute(text("DELETE FROM dataview.dv_land_tract "
                           "WHERE source = :s"), {"s": SOURCE_STAMP})
        return r.rowcount if r.rowcount and r.rowcount > 0 else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--nx", type=int, default=6)
    ap.add_argument("--ny", type=int, default=7)
    a = ap.parse_args()

    from dataview.core.dw_utils import make_engine
    from sqlalchemy import text
    engine = make_engine(a.database)

    if a.remove:
        n = remove(engine, apply=a.apply)
        print("%s %d synthetic lease(s)"
              % ("removed" if a.apply else "would remove", n))
        return 0

    with engine.connect() as c:
        b = c.execute(text("""
            SELECT MIN(surface_latitude), MAX(surface_latitude),
                   MIN(surface_longitude), MAX(surface_longitude)
              FROM dataview.dv_well WHERE surface_latitude IS NOT NULL""")
        ).fetchone()
    if not b or b[0] is None:
        print("No located wells, so nothing to lay leases over.")
        return 1
    # A margin, so the outermost wells sit INSIDE a tract rather than on its
    # edge -- a well exactly on a boundary belongs to both or neither.
    mlat = (float(b[1]) - float(b[0])) * 0.04 + 0.002
    mlon = (float(b[3]) - float(b[2])) * 0.04 + 0.002
    recs = build(float(b[0]) - mlat, float(b[1]) + mlat,
                 float(b[2]) - mlon, float(b[3]) + mlon, a.nx, a.ny)

    print("wells span lat %.4f..%.4f  lon %.4f..%.4f"
          % (b[0], b[1], b[2], b[3]))
    print("%d tract(s) over it:" % len(recs))
    from collections import Counter
    for owner, k in Counter(r["owner"] for r in recs).most_common():
        print("   %-26s %d" % (owner, k))
    if not a.apply:
        print("\nCOUNTS ONLY -- re-run with --apply.")
        return 0
    n = write(engine, recs, apply=True)
    with engine.connect() as c:
        tot, area, bad = c.execute(text("""
            SELECT COUNT(*), ROUND(SUM(area_km2), 1),
                   SUM(CASE WHEN area_km2 > 100000 THEN 1 ELSE 0 END)
              FROM dataview.dv_land_tract WHERE source = :s"""),
            {"s": SOURCE_STAMP}).fetchone()
    print("\ninserted %d; table now holds %d tract(s), %.1f km2 total"
          % (n, tot, area or 0))
    print("   inside-out rings remaining: %d  (must be 0)" % (bad or 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
