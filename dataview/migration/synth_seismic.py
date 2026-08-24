r"""Write synthetic 2D SEG-Y lines over a structural model.

WHY REAL SEG-Y AND NOT NAVIGATION FILES. A nav-only survey is HELD by
promote_catalog._TIED -- deliberately, because a navigation file with no
seismic behind it describes nothing -- so nav lines never reach dv_seis_line
and never draw on the map. And P190 currently reduces a multi-line file to one
bbox rather than per-line geometry. Lines on a map therefore mean SEG-Y files.

WHAT MAKES THESE WORTH HAVING. The samples are not noise. A dome is modelled
once, in ground coordinates, and every line samples THAT SURFACE -- so lines
crossing the crest show closure, lines on the flank show dip, and two lines
that intersect agree at the intersection. Random traces would pass every format
check, draw a plausible section, and fall apart the moment anyone tied two
lines together.

Format 1 (IBM floats) on purpose: it is what the tape-era corpus uses,
including Teapot's own filt_mig.sgy, so these exercise the same decode path as
the real files rather than the easy one.
"""
import math
import os
import struct
import zlib

import numpy as np

TEXT_HDR = 3200
BIN_HDR = 400
TRACE_HDR = 240

# Trace-header byte offsets, rev-1 defaults -- the same map segy_header falls
# back to when a textual header declares no layout of its own.
OFF_SCALAR = 70          # bytes 71-72
OFF_CDP_X = 180          # bytes 181-184
OFF_CDP_Y = 184          # bytes 185-188
OFF_INLINE = 188         # bytes 189-192
OFF_XLINE = 192          # bytes 193-196


def ibm_encode(a):
    """float array -> IBM System/360 uint32 array, vectorised.

    The inverse of segy_header._ibm_to_ieee. Vectorised because a thousand
    lines is 75 million samples and a Python loop over that is minutes of pure
    overhead.

    Sign, 7-bit excess-64 exponent in base SIXTEEN, 24-bit fraction. The
    fiddly part is that rounding the fraction can carry it to 1.0, which is not
    representable -- the mantissa must stay under one -- so that case shifts a
    hexadecimal digit and bumps the exponent.
    """
    a = np.asarray(a, dtype=np.float64)
    out = np.zeros(a.shape, dtype=np.uint32)
    nz = np.isfinite(a) & (a != 0)
    if not nz.any():
        return out
    v = np.abs(a[nz])
    sign = (a[nz] < 0).astype(np.uint32) << np.uint32(31)
    expo = np.floor(np.log(v) / math.log(16.0)).astype(np.int64) + 1
    mant = v / np.power(16.0, expo.astype(np.float64))
    carry = mant >= 1.0
    mant[carry] /= 16.0
    expo[carry] += 1
    frac = np.minimum((mant * float(1 << 24)).astype(np.int64),
                      (1 << 24) - 1).astype(np.uint32)
    # Outside the representable exponent range the honest answer is zero, not
    # a wrapped exponent that decodes to a huge plausible number.
    ok = (expo >= -64) & (expo <= 63)
    biased = ((expo + 64).astype(np.int64) & 0x7F).astype(np.uint32)
    vals = sign | (biased << np.uint32(24)) | frac
    vals[~ok] = 0
    out[nz] = vals
    return out


def _ebcdic(lines):
    """A 3200-byte textual header, 40 lines of 80, EBCDIC as the standard says."""
    buf = []
    for i in range(40):
        txt = lines[i] if i < len(lines) else ""
        buf.append(f"C{i + 1:02d} {txt}"[:80].ljust(80))
    return "".join(buf).encode("cp037")


def _ricker(n, dt_s, f_hz):
    """A zero-phase Ricker wavelet, odd length, centred."""
    t = (np.arange(n) - n // 2) * dt_s
    x = (math.pi * f_hz * t) ** 2
    return (1.0 - 2.0 * x) * np.exp(-x)


class Dome:
    """A structural model in ground coordinates: reflector time at (x, y).

    ONE SURFACE, SAMPLED BY EVERY LINE. That is the whole point -- it is what
    makes two crossing lines agree at their intersection, and what makes the
    crest look like a crest from any direction. A per-line random structure
    would look right in isolation and be incoherent the moment anything is
    tied.
    """

    def __init__(self, cx, cy, relief_ms, radius_m, horizons_ms, rng):
        self.cx, self.cy = cx, cy
        self.relief = relief_ms
        self.radius = radius_m
        self.horizons = horizons_ms
        # A dome is not a circle. A gentle elongation and rotation keeps the
        # closure from looking machined.
        self.ecc = rng.uniform(1.15, 1.55)
        self.rot = rng.uniform(0, math.pi)
        self.dip_x = rng.uniform(-0.0022, 0.0022)      # regional dip, ms/m
        self.dip_y = rng.uniform(-0.0022, 0.0022)

    def t0(self, x, y, horizon_ms):
        """Two-way time of one horizon at a ground position, in ms."""
        dx, dy = x - self.cx, y - self.cy
        c, s = math.cos(self.rot), math.sin(self.rot)
        u = (dx * c + dy * s) / self.ecc
        v = -dx * s + dy * c
        r2 = (u * u + v * v) / (self.radius * self.radius)
        # Gaussian closure: shallowest over the crest, flattening outward.
        # Deeper horizons carry less relief, the way compaction drapes them.
        scale = 1.0 - 0.35 * (horizon_ms / max(1.0, self.horizons[-1]))
        return (horizon_ms
                - self.relief * scale * math.exp(-r2)
                + self.dip_x * dx + self.dip_y * dy)


def write_line(path, xs, ys, dome, rng, *, n_samples=500, dt_us=2000,
               epsg=32613, survey="", line_name="", scalar=1,
               freq_hz=28.0, line_no=1):
    """One 2D SEG-Y line. xs/ys are ground coordinates per trace.

    Returns the number of traces written.
    """
    n_tr = len(xs)
    dt_s = dt_us / 1_000_000.0
    tms = np.arange(n_samples) * (dt_us / 1000.0)

    wav = _ricker(81, dt_s, freq_hz)
    # Reflection coefficients: alternating polarity so the section reads as
    # layering rather than a stack of identical events.
    rc = [rng.uniform(0.45, 1.0) * (1 if i % 2 == 0 else -1)
          for i in range(len(dome.horizons))]

    data = np.zeros((n_samples, n_tr), dtype=np.float64)
    # ONE NOISE FIELD FOR THE LINE, not one RNG per trace. Building a
    # generator inside the trace loop is 150,000 constructions for a thousand
    # lines and dominates the run; drawing the whole panel once is the same
    # noise for the same seed and a fraction of the time.
    _seed = zlib.crc32(("%s|%s" % (survey, line_name)).encode("utf-8"))
    _nrng = np.random.default_rng(_seed)
    _noise = _nrng.normal(0, 0.035, (n_samples, n_tr))
    # Noise grows with time the way amplitude decay makes it: a constant floor
    # under a decaying signal is the giveaway that a section is synthetic.
    _ramp = (1.0 + tms / tms[-1])[:, None]
    _decay = np.exp(-tms / 2600.0)[:, None]
    for j in range(n_tr):
        spikes = np.zeros(n_samples)
        for hi, h in enumerate(dome.horizons):
            t = dome.t0(xs[j], ys[j], h)
            k = int(round(t / (dt_us / 1000.0)))
            if 0 <= k < n_samples:
                spikes[k] += rc[hi]
        data[:, j] = np.convolve(spikes, wav, mode="same")
    data = (data + _noise * _ramp) * _decay * 3000.0

    textual = [
        f"CLIENT: SYNTHETIC          SURVEY: {survey}",
        f"LINE: {line_name}",
        "AREA: TEAPOT DOME, NATRONA COUNTY, WYOMING",
        f"EPSG: {epsg}",
        "COORDINATE UNITS: METRES",
        f"SAMPLE INTERVAL: {dt_us} US    SAMPLES PER TRACE: {n_samples}",
        f"TRACES: {n_tr}    RECORD LENGTH: {n_samples * dt_us // 1000} MS",
        "DATA FORMAT: IBM FLOATING POINT (FORMAT 1)",
        "BYTES 181-184: CDP X",
        "BYTES 185-188: CDP Y",
        "SYNTHETIC DATA - GENERATED FOR TESTING AND DEMONSTRATION",
        "NOT FIELD DATA. DO NOT USE FOR INTERPRETATION.",
    ]

    binh = bytearray(BIN_HDR)
    struct.pack_into(">H", binh, 16, dt_us)          # sample interval
    struct.pack_into(">H", binh, 20, n_samples)      # samples per trace
    struct.pack_into(">h", binh, 24, 1)              # format 1 = IBM float
    struct.pack_into(">h", binh, 54, 1)              # measurement = metres
    struct.pack_into(">h", binh, 300, 0x0100)        # SEG-Y rev 1.0

    enc = ibm_encode(data)                            # samples x traces
    with open(path, "wb") as f:
        f.write(_ebcdic(textual))
        f.write(bytes(binh))
        for j in range(n_tr):
            th = bytearray(TRACE_HDR)
            struct.pack_into(">i", th, 0, j + 1)             # trace sequence
            struct.pack_into(">i", th, 8, line_no)           # field record
            struct.pack_into(">i", th, 20, j + 1)            # CDP number
            struct.pack_into(">h", th, 28, 1)                # trace id = live
            struct.pack_into(">h", th, OFF_SCALAR, scalar)
            struct.pack_into(">i", th, OFF_CDP_X, int(round(xs[j])))
            struct.pack_into(">i", th, OFF_CDP_Y, int(round(ys[j])))
            struct.pack_into(">i", th, OFF_INLINE, line_no)
            struct.pack_into(">i", th, OFF_XLINE, j + 1)
            struct.pack_into(">h", th, 114, n_samples)       # samples, trace
            struct.pack_into(">h", th, 116, dt_us)           # interval, trace
            f.write(bytes(th))
            f.write(enc[:, j].astype(">u4").tobytes())
    return n_tr


# --------------------------------------------------------------------------- #
# The Teapot Dome model, defined ONCE
# --------------------------------------------------------------------------- #
#
# THE HORIZONS AND THE SEISMIC MUST BE THE SAME SURFACE. A horizon that does
# not sit on its reflector is worse than no horizon: it looks like an
# interpretation, it plots, it exports, and it is wrong everywhere at once.
# Both the SEG-Y writer and the horizon generator therefore build the dome from
# THIS function and nothing else -- duplicating the parameters in two tools is
# how they drift apart by one edit.
#
# Dome.__init__ draws eccentricity, rotation and regional dip from the rng, so
# the SEED IS PART OF THE MODEL. Same seed, same surface, in either tool.

TEAPOT_CREST = (-106.212, 43.290)          # lon, lat -- the dome crest
TEAPOT_AREA = dict(min_lat=43.205, max_lat=43.455,
                   min_lon=-106.345, max_lon=-106.135)
TEAPOT_EPSG = 32613                        # WGS84 / UTM 13N, metres
TEAPOT_SEED = 4317

# name, two-way time at the regional level (ms), display colour, the
# stratigraphy it stands for. The times are what the reflectors are generated
# at, so a pick on the section IS this number plus the structure.
TEAPOT_HORIZONS = [
    ("Steele Shale",     360.0, "#E4572E", "STEELE SHALE"),
    ("Niobrara",         505.0, "#17BEBB", "NIOBRARA FORMATION"),
    ("Frontier 2nd Wall Creek", 660.0, "#FFC914", "FRONTIER FORMATION"),
    ("Tensleep",         830.0, "#4C6EF5", "TENSLEEP SANDSTONE"),
]


def teapot_model(seed=TEAPOT_SEED, epsg=TEAPOT_EPSG):
    """(dome, to_utm, to_ll) for Teapot Dome. The single source of the surface.

    Raises ImportError if pyproj is absent -- the model is defined in ground
    coordinates and there is no honest way to place it without a projection.
    """
    import random
    from pyproj import Transformer

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    to_ll = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    cx, cy = to_utm.transform(*TEAPOT_CREST)
    rng = random.Random(seed)
    dome = Dome(cx, cy, relief_ms=145, radius_m=3900,
                horizons_ms=[h[1] for h in TEAPOT_HORIZONS], rng=rng)
    return dome, to_utm, to_ll


# Acquisition campaigns. Real 2D coverage accumulates like this, which is also
# why one line has several processing versions and its neighbour has none.
TEAPOT_VINTAGES = [
    ("NPR-3 TEAPOT DOME 1977 2D", "TPD77", 78.0),
    ("NPR-3 TEAPOT DOME 1979 2D", "TPD79", 168.0),
    ("NPR-3 TEAPOT DOME 1982 2D", "TPD82", 45.0),
    ("NPR-3 TEAPOT DOME 1987 2D", "TPD87", 120.0),
    ("NPR-3 TEAPOT DOME 1996 2D", "TPD96", 15.0),
]


def teapot_2d_layout(n_lines=250, traces=150, spacing=50.0, seed=TEAPOT_SEED,
                     epsg=TEAPOT_EPSG, area=None):
    """[{survey, code, line_id, xs, ys}] -- where every 2D line actually runs.

    ONE DEFINITION OF THE GRID. The SEG-Y writer needs it to place traces and
    the field planner needs it to put wells ON lines; deriving it twice is how
    the wells end up beside the seismic instead of on it, with nothing to say
    so. Same seed, same grid, from either caller.

    The rng is consumed in exactly the order the writer used, so the layout is
    unchanged from the files already on disk.
    """
    import random
    from pyproj import Transformer

    area = area or TEAPOT_AREA
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    x0, y0 = to_utm.transform(area["min_lon"], area["min_lat"])
    x1, y1 = to_utm.transform(area["max_lon"], area["max_lat"])
    rng = random.Random(seed + 1)          # this function's own generator
    line_m = traces * spacing
    per_vintage = max(1, n_lines // len(TEAPOT_VINTAGES))
    half = (traces - 1) / 2.0

    out = []
    for sv, code, az_deg in TEAPOT_VINTAGES:
        for li in range(per_vintage):
            if len(out) >= n_lines:
                break
            az = math.radians(az_deg + rng.gauss(0, 1.6))
            cxx = rng.uniform(x0 + line_m / 2, x1 - line_m / 2)
            my = rng.uniform(y0 + line_m / 2, y1 - line_m / 2)
            out.append({
                "survey": sv, "code": code, "line_id": f"{code}-{li + 1:03d}",
                "xs": [cxx + (i - half) * spacing * math.cos(az)
                       for i in range(traces)],
                "ys": [my + (i - half) * spacing * math.sin(az)
                       for i in range(traces)],
            })
    return out
