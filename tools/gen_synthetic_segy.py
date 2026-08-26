r"""Write synthetic 2D SEG-Y files along the dip and strike of the real survey.

WHY FILES AND NOT ROWS. gen_synthetic_seis_grid writes LINE GEOMETRY straight
into dv_seis_line, which is enough to draw a map and nothing else: no section
opens, because there is no SEG-Y behind it, and the chooser correctly refuses
to offer a file that does not exist. This writes the FILES. They then go
through the ordinary File Catalog -- scan, extract, promote -- so the geometry,
the headers, the trace counts and the CRS all arrive the way real data arrives,
and every downstream feature works because nothing downstream is special-cased.

THE STRUCTURE IS REAL EVEN THOUGH THE DATA IS NOT. Reflector time follows the
Tensleep structure contours already loaded from the RMOTC geodatabase (45
contours, TVDSS 1020-1220 ft), interpolated to each CDP. So a section across
the dome shows the dome, dip lines climb over the crest and strike lines run
along it. Synthetic amplitudes over a measured shape beat noise over a
fabricated one: the picture teaches the right thing about Teapot.

WHAT IS FABRICATED, SAID PLAINLY. Amplitudes, the wavelet, the noise, the
velocity used to turn depth into time, and the extra reflectors above and below
the Tensleep. The textual header of every file says SYNTHETIC on line 1 and
names this tool, so nothing here can be mistaken for RMOTC data by anyone
reading the file itself.

THE CRS IS DECLARED, NOT IMPLIED. crs_from_text reads "EPSG:26913" out of the
textual header and calls it declared; coordinates are written in that CRS
(NAD83 / UTM 13N, metres) rather than in degrees, because SEG-Y coordinate
scalars cannot carry decimal degrees at survey precision.

    python tools/gen_synthetic_segy.py                     # plan only
    python tools/gen_synthetic_segy.py --apply
    python tools/gen_synthetic_segy.py --apply --spacing 500
"""
import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

OUT_DIR = os.path.join(
    r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\data_wrangler",
    "training", "Teapot_Field_Model", "seismic", "2d_synthetic")

CDP_SPACING_M = 25.0      # trace every 25 m along the line
SAMPLE_INT_US = 2000      # 2 ms
N_SAMPLES = 1001          # 0 .. 2000 ms
EPSG_OUT = 26913          # NAD83 / UTM zone 13N, metres


def _tensleep_points(engine):
    """[(lat, lon, tvdss)] from every vertex of the Tensleep contours."""
    import json
    from sqlalchemy import text
    with engine.connect() as c:
        gj = c.execute(text(
            "SELECT geometry_wkt FROM dataview.dv_spatial_layer "
            "WHERE layer_name = 'Tensleep Structure'")).scalar()
    if not gj:
        return []
    d = json.loads(gj)
    pts = []
    for f in d.get("features") or []:
        v = (f.get("properties") or {}).get("TVDSS")
        if v is None:
            continue
        g = f.get("geometry") or {}
        rings = []
        t = g.get("type")
        if t == "Polygon":
            rings = g.get("coordinates") or []
        elif t == "MultiPolygon":
            for p in g.get("coordinates") or []:
                rings.extend(p)
        elif t == "LineString":
            rings = [g.get("coordinates") or []]
        elif t == "MultiLineString":
            rings = g.get("coordinates") or []
        for r in rings:
            # Every 4th vertex: a contour has hundreds and the field is 12 km,
            # so the shape survives thinning and the interpolation stays quick.
            for co in r[::4]:
                pts.append((float(co[1]), float(co[0]), float(v)))
    return pts


def _depth_at(lat, lon, pts, power=2.0, k=12):
    """Inverse-distance TVDSS at a point, from the k nearest contour vertices.

    IDW OVER THE CONTOURS THEMSELVES, not a fitted surface. The contours are
    the measurement; anything smoother would be an interpretation, and this
    only has to put the crest in the right place.
    """
    best = []
    for la, lo, v in pts:
        dx = (lo - lon) * math.cos(math.radians(lat))
        dy = la - lat
        d2 = dx * dx + dy * dy
        if d2 < 1e-14:
            return v
        if len(best) < k:
            best.append((d2, v))
            best.sort()
        elif d2 < best[-1][0]:
            best[-1] = (d2, v)
            best.sort()
    if not best:
        return None
    num = den = 0.0
    for d2, v in best:
        w = 1.0 / (d2 ** (power / 2.0))
        num += w * v
        den += w
    return num / den


def _wavelet(f_hz, dt_s, length=101):
    """A Ricker wavelet, the standard zero-phase synthetic pulse."""
    import numpy as np
    t = (np.arange(length) - length // 2) * dt_s
    a = (math.pi * f_hz * t) ** 2
    return (1.0 - 2.0 * a) * np.exp(-a)


def _trace(t_tensleep_ms, rng):
    """One synthetic trace: reflectors, wavelet, noise.

    The Tensleep is the strong event; a shallower and a deeper marker follow
    the same structure with different amplitudes, which is what makes a section
    readable as a fold rather than as one bright line.
    """
    import numpy as np
    refl = np.zeros(N_SAMPLES, dtype=np.float32)
    dt_ms = SAMPLE_INT_US / 1000.0
    for dt_off, amp in ((-260.0, 0.45), (-120.0, -0.30), (0.0, 1.00),
                        (85.0, -0.55), (240.0, 0.35)):
        i = int(round((t_tensleep_ms + dt_off) / dt_ms))
        if 0 <= i < N_SAMPLES:
            refl[i] += amp
    w = _wavelet(28.0, SAMPLE_INT_US / 1e6)
    tr = np.convolve(refl, w, mode="same").astype(np.float32)
    tr += rng.normal(0.0, 0.035, N_SAMPLES).astype(np.float32)
    # Gentle gain decay with time, so it looks like data rather than a plot.
    tr *= np.exp(-np.arange(N_SAMPLES) * dt_ms / 2600.0).astype(np.float32)
    return tr


def _to_utm(lat, lon):
    from pyproj import Transformer
    global _TR
    try:
        _TR
    except NameError:
        _TR = Transformer.from_crs("EPSG:4326", "EPSG:%d" % EPSG_OUT,
                                   always_xy=True)
    x, y = _TR.transform(lon, lat)
    return x, y


def _cdps(p0, p1, spacing_m):
    """[(lat, lon)] every `spacing_m` along the segment, ends included."""
    la0, lo0 = p0
    la1, lo1 = p1
    x0, y0 = _to_utm(la0, lo0)
    x1, y1 = _to_utm(la1, lo1)
    n = max(2, int(round(math.hypot(x1 - x0, y1 - y0) / spacing_m)) + 1)
    out = []
    for i in range(n):
        f = i / float(n - 1)
        out.append((la0 + (la1 - la0) * f, lo0 + (lo1 - lo0) * f))
    return out


def _textual(name, az, n_traces, tmin, tmax):
    L = [
        "C 1 SYNTHETIC SEISMIC - NOT REAL DATA - GENERATED BY "
        "gen_synthetic_segy.py",
        "C 2 SURVEY: NPR-3 TEAPOT DOME SYNTHETIC 2D",
        "C 3 LINE  : %s" % name,
        "C 4 AZIMUTH %.1f DEG - PARALLEL TO THE DIP/STRIKE OF THE RMOTC 2D "
        "LINES" % az,
        "C 5 COORDINATE SYSTEM: NAD83 / UTM ZONE 13N   EPSG:26913   UNITS "
        "METRES",
        "C 6 CDP X/Y IN TRACE HEADER BYTES 181/185, SCALAR 1",
        "C 7 TRACES %d   SAMPLE INTERVAL %d US   SAMPLES %d"
        % (n_traces, SAMPLE_INT_US, N_SAMPLES),
        "C 8 ",
        "C 9 REFLECTOR GEOMETRY IS REAL: TWO-WAY TIME FOLLOWS THE TENSLEEP",
        "C10 STRUCTURE CONTOURS FROM THE RMOTC GEODATABASE, INTERPOLATED TO",
        "C11 EACH CDP.  TENSLEEP EVENT SPANS %.0f - %.0f MS ON THIS LINE."
        % (tmin, tmax),
        "C12 ",
        "C13 EVERYTHING ELSE IS FABRICATED: AMPLITUDES, THE 28 HZ RICKER",
        "C14 WAVELET, RANDOM NOISE, THE DEPTH-TO-TIME VELOCITY, AND THE FOUR",
        "C15 MARKERS ABOVE AND BELOW THE TENSLEEP.  DO NOT INTERPRET FOR",
        "C16 AMPLITUDE, PHASE, FREQUENCY CONTENT OR ANY ROCK PROPERTY.",
        "C17 ",
        "C18 PROCESSING TOKEN IN THE FILE NAME IS PART OF THE SYNTHETIC",
        "C19 FICTION AND DOES NOT DESCRIBE A REAL PROCESSING SEQUENCE.",
        "C20 ",
    ]
    while len(L) < 40:
        L.append("C%2d " % (len(L) + 1))
    return "\n".join(s[:80].ljust(80) for s in L[:40])


def build_lines(engine, spacing_m):
    """Reuse the grid tool's geometry -- one definition, two doors."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gen_synthetic_seis_grid as G
    rows, strike, dip, ring = G.build(engine, spacing_m)
    return rows, strike, dip


def write_line(path, name, pts, az, depth_pts, seed):
    import numpy as np
    import segyio

    cdps = []
    for i in range(len(pts) - 1):
        seg = _cdps(pts[i], pts[i + 1], CDP_SPACING_M)
        cdps.extend(seg if not cdps else seg[1:])
    if len(cdps) < 2:
        return 0, None, None

    # DEPTH -> TIME. A single average velocity is a fiction, and a stated one
    # is an honest fiction: 3200 m/s two-way over a field whose reservoir sits
    # around 1500 ms. The SHAPE comes from the contours; only the scaling is
    # invented, and the textual header says so.
    zs = []
    for la, lo in cdps:
        z = _depth_at(la, lo, depth_pts)
        zs.append(z)
    known = [z for z in zs if z is not None]
    if not known:
        return 0, None, None
    zmid = sum(known) / len(known)
    zs = [zmid if z is None else z for z in zs]
    # TVDSS grows downward, so a HIGH value is a LOW structure: the crest of
    # the dome must come back as the EARLIEST time, not the latest.
    ts = [1500.0 + (z - zmid) * 0.3048 * 2.0 / 3200.0 * 1000.0 for z in zs]

    rng = np.random.default_rng(seed)
    spec = segyio.spec()
    spec.format = 1                      # 4-byte IBM float
    spec.samples = list(range(0, N_SAMPLES * (SAMPLE_INT_US // 1000),
                              SAMPLE_INT_US // 1000))
    spec.tracecount = len(cdps)
    spec.sorting = segyio.TraceSortingFormat.INLINE_SORTING

    with segyio.create(path, spec) as f:
        for i, (la, lo) in enumerate(cdps):
            x, y = _to_utm(la, lo)
            f.header[i] = {
                segyio.su.tracl: i + 1,
                segyio.su.tracr: i + 1,
                segyio.su.cdp: i + 1,
                segyio.su.cdpt: 1,
                segyio.su.trid: 1,
                segyio.su.scalco: 1,
                segyio.su.sx: int(round(x)),
                segyio.su.sy: int(round(y)),
                segyio.su.gx: int(round(x)),
                segyio.su.gy: int(round(y)),
                segyio.su.cdpx: int(round(x)),
                segyio.su.cdpy: int(round(y)),
                segyio.su.ns: N_SAMPLES,
                segyio.su.dt: SAMPLE_INT_US,
                segyio.su.offset: 0,
            }
            f.trace[i] = _trace(ts[i], rng)
        f.bin[segyio.BinField.Interval] = SAMPLE_INT_US
        f.bin[segyio.BinField.Samples] = N_SAMPLES
        f.bin[segyio.BinField.Format] = 1
        f.bin[segyio.BinField.Traces] = len(cdps)
        f.text[0] = _textual(name, az, len(cdps), min(ts), max(ts))
    return len(cdps), min(ts), max(ts)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Synthetic 2D SEG-Y on the dip/strike of the real survey.")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--spacing", type=float, default=1000.0,
                    help="line spacing in metres (default 1000)")
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    try:
        import segyio            # noqa: F401
        import numpy             # noqa: F401
        from pyproj import Transformer   # noqa: F401
    except ImportError as e:
        print("Needs segyio, numpy and pyproj: %s" % e)
        return 2

    from dataview.core.dw_utils import make_engine
    engine = make_engine(a.database)

    rows, strike, dip = build_lines(engine, a.spacing)
    depth_pts = _tensleep_points(engine)
    if not depth_pts:
        print("No 'Tensleep Structure' contours in dv_spatial_layer -- load "
              "the GIS layers first, or the sections would be flat.")
        return 2

    print("Lines        : %d  (strike %.1f deg, dip %.1f deg)"
          % (len(rows), strike, dip))
    print("Structure    : %d Tensleep contour vertices" % len(depth_pts))
    print("Traces       : one every %.0f m, %d samples at %d us"
          % (CDP_SPACING_M, N_SAMPLES, SAMPLE_INT_US))
    print("Output       : %s" % a.out)
    if not a.apply:
        print("\nPLAN ONLY -- nothing written. Re-run with --apply.")
        return 0

    os.makedirs(a.out, exist_ok=True)
    n_files = n_tr = 0
    for i, (name, pts) in enumerate(rows):
        az = strike if name.startswith("TPD-S") else dip
        p = os.path.join(a.out, name + ".sgy")
        try:
            nt, t0, t1 = write_line(p, name, pts, az, depth_pts, seed=1000 + i)
        except Exception as e:
            print("   %-28s FAILED %s: %s" % (name, type(e).__name__, e))
            continue
        if not nt:
            print("   %-28s skipped (no CDPs)" % name)
            continue
        n_files += 1
        n_tr += nt
        print("   %-28s %5d traces  Tensleep %4.0f-%4.0f ms  %6.1f MB"
              % (name, nt, t0, t1, os.path.getsize(p) / 1048576.0))
    print("\n%d file(s), %s trace(s) written to %s"
          % (n_files, format(n_tr, ","), a.out))
    print("Now scan that folder in the File Catalog to load them properly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
