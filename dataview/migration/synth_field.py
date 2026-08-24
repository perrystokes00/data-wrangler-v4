r"""A field life over the Teapot Dome model: exploration, delineation, then
development and five years of production.

EVERYTHING COMES OFF THE SAME SURFACE. The wells are placed against the dome
that generated the 2D SEG-Y and the horizons, their tops are read from the
horizon grids at each well's own position, and their production is scaled by
structural height above the reservoir's closing contour. That is what makes
this a field rather than a spreadsheet: crestal wells come in strong and dry,
flank wells come in weak and wet, the tops are conformable because the surfaces
are, and a decline analysis run over it gives an answer that means something.

A field whose crestal wells produce the same as its flank wells is exactly the
kind of data someone would build a study on and be wrong.

THE PHASES ARE A HISTORY, NOT A LABEL. Exploration wells are drilled first, at
the crest, and two of the three are dry because that is what exploration is.
Delineation steps out to find the edge. Development infills what the first two
phases proved, and only development wells get five years of production --
a discovery well that was plugged in 2019 does not have a 2024 rate.
"""
import datetime as _dt
import math

import numpy as np

from dataview.migration.synth_seismic import (
    TEAPOT_AREA, TEAPOT_HORIZONS, teapot_2d_layout, teapot_model)

# Velocity for time-depth. V(z) = V0 + k*z gives
#     z = (V0/k) * (exp(k*t/2) - 1)     t = two-way time, seconds
# V0 = 7200 ft/s and k = 2.4 /s put the shallowest horizon at ~1,000 ft and the
# deepest at ~5,300 ft, which is where Teapot's Steele and Tensleep actually
# sit. A constant velocity would put them at the wrong depths AND flatten the
# structure, since relief in time converts to more relief in depth as velocity
# rises.
V0_FTS, K_PER_S = 7200.0, 2.4

# The strat column, hung off the four seismic horizons.
#
# A FORMATION BETWEEN TWO HORIZONS IS PLACED AT A FRACTION OF THAT INTERVAL,
# not at a fixed offset from one of them. The first cut used fixed offsets and
# put Crow Mountain (Tensleep minus 620 ft) ABOVE Sundance (Frontier plus 980)
# -- a strat column that goes back up the hole, which is wrong everywhere it is
# used and would silently produce negative interval thicknesses.
#
# Fractions cannot do that: the interval thickness changes across the structure
# but the ORDER within it cannot, so the column stays conformable and monotonic
# wherever it is sampled. tops() asserts that, rather than trusting the numbers
# below to stay consistent when someone edits them.
#
# (name, horizon index, fraction of the interval BELOW that horizon, remark)
STRAT_COLUMN = [
    ("Shannon Sandstone", 0, -0.30, ""),
    ("Steele Shale", 0, 0.0, "Seismic marker"),
    ("Niobrara Formation", 1, 0.0, "Seismic marker"),
    ("Frontier 2nd Wall Creek", 2, 0.0, "TARGET"),
    ("Muddy Sandstone", 2, 0.23, "Shows"),
    ("Dakota Sandstone", 2, 0.32, ""),
    ("Morrison Formation", 2, 0.55, ""),
    ("Sundance Formation", 2, 0.69, ""),
    ("Crow Mountain", 2, 0.86, ""),
    ("Tensleep Sandstone", 3, 0.0, "Seismic marker, primary reservoir"),
    ("Amsden Formation", 3, 0.15, ""),
]

RESERVOIR_HORIZON = 3          # Tensleep -- what the production model uses
OPERATOR = "NAVAL PETROLEUM RESERVE OPERATIONS"
FIELD_NAME = "Teapot Dome (NPR-3)"


def twt_to_depth_ft(twt_ms):
    """Two-way time in ms -> depth in feet, for a linear velocity gradient."""
    t = float(twt_ms) / 1000.0
    return (V0_FTS / K_PER_S) * (math.exp(K_PER_S * t / 2.0) - 1.0)


class Surfaces:
    """The horizon grids, sampled by position. Built once, used by every well."""

    def __init__(self, nrow=90, ncol=70, seed=None):
        from dataview.migration.synth_horizons import build_grid
        self.dome, self.to_utm, self.to_ll = (
            teapot_model() if seed is None else teapot_model(seed))
        self.grids = []
        for _name, t_ms, _c, _s in TEAPOT_HORIZONS:
            self.grids.append(build_grid(self.dome, self.to_utm, t_ms,
                                         nrow=nrow, ncol=ncol))

    def twt(self, idx, lat, lon):
        """Two-way time of horizon `idx` at a position, ms."""
        from dataview.migration.synth_horizons import sample
        lats, lons, vals = self.grids[idx]
        return sample(lats, lons, vals, lat, lon)

    def depth(self, idx, lat, lon):
        t = self.twt(idx, lat, lon)
        return None if t is None else twt_to_depth_ft(t)

    def tops(self, lat, lon):
        """[(name, depth_ft, remark)] -- the strat column at one position.

        Raises if the column is not strictly deepening. A tops list that goes
        back up the hole gives negative thicknesses, and every consumer of it
        -- net pay, interval isopachs, the completion interval -- is then wrong
        in a way that still looks like data.
        """
        zs = [self.depth(i, lat, lon) for i in range(len(self.grids))]
        if any(z is None for z in zs):
            return []
        out = []
        for name, hi, frac, note in STRAT_COLUMN:
            if frac == 0.0:
                z = zs[hi]
            elif frac < 0:
                # Above the shallowest horizon: scale by the first interval,
                # since there is no interval above it to take a fraction of.
                z = zs[hi] + frac * (zs[hi + 1] - zs[hi])
            else:
                lo = zs[hi]
                hi_z = zs[hi + 1] if hi + 1 < len(zs) else zs[hi] * 1.08
                z = lo + frac * (hi_z - lo)
            out.append((name, round(z, 1), note))
        for a, b in zip(out, out[1:]):
            assert b[1] > a[1], (
                "strat column is not monotonic: " + a[0] + " at "
                + str(a[1]) + " ft is deeper than " + b[0] + " at "
                + str(b[1]) + " ft")
        return out

    def crest_depth(self):
        """Reservoir depth at the crest -- the datum structure is measured from."""
        lats, lons, vals = self.grids[RESERVOIR_HORIZON]
        return twt_to_depth_ft(float(np.nanmin(vals)))


ON_SEISMIC = {"EXPLORATION": 1.00, "DELINEATION": 0.75,
              "DEVELOPMENT": 0.35}


def _uwi(seq):
    """A Natrona County UWI in the block the old Teapot wells did not use."""
    return f"49025{90000 + seq:05d}0000"


def _ring(rng, cx, cy, r_min, r_max, n):
    """n positions in an annulus, roughly evenly spread in angle."""
    out = []
    for i in range(n):
        a = (2 * math.pi * i / n) + rng.uniform(-0.22, 0.22)
        r = rng.uniform(r_min, r_max)
        out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return out


def plan_field(n_expl=3, n_delin=8, n_dev=109, seed=90210,
               start_year=2014, prod_years=5, surfaces=None):
    """The whole field as well dicts, ready for synth_docs.generate().

    Each dict carries the CSV fields the document generators read, plus the
    dome-derived extras they now honour: _tops, _qi, _months, _decline.
    """
    import random
    rng = random.Random(seed)
    S = surfaces or Surfaces()
    cx, cy = S.to_utm.transform(-106.212, 43.290)
    crest_z = S.crest_depth()

    # The SAME layout the SEG-Y writer uses -- one definition, so a well
    # placed on TPD79-014 sits on the traces of the file called
    # TPD79-014, not near it.
    try:
        _lines = teapot_2d_layout()          # TEAPOT_SEED, as the SEG-Y uses
    except Exception:
        _lines = []
    spots = []
    spots += [("EXPLORATION", p) for p in _ring(rng, cx, cy, 150, 1100, n_expl)]
    spots += [("DELINEATION", p) for p in _ring(rng, cx, cy, 1900, 3600, n_delin)]
    # Development infills the closure: a loose grid, jittered, inside the ring
    # the delineation wells established.
    side = int(math.ceil(math.sqrt(n_dev)))
    step = 5200.0 / max(1, side - 1)
    dev = []
    for i in range(side):
        for j in range(side):
            if len(dev) >= n_dev:
                break
            dev.append((cx - 2600 + i * step + rng.uniform(-260, 260),
                        cy - 2600 + j * step + rng.uniform(-260, 260)))
    spots += [("DEVELOPMENT", p) for p in dev[:n_dev]]

    wells, seq = [], 1
    for phase, (x, y) in spots:
        _on_line = None
        if _lines and rng.random() < ON_SEISMIC.get(phase, 0.0):
            _ln = rng.choice(_lines)
            _ti = rng.randrange(len(_ln["xs"]))
            # A few metres off the trace, not exactly on it: a wellhead
            # is surveyed, a trace is a bin centre, and they never agree
            # to the millimetre.
            x = _ln["xs"][_ti] + rng.uniform(-12, 12)
            y = _ln["ys"][_ti] + rng.uniform(-12, 12)
            _on_line = (_ln["line_id"], _ln["survey"], _ti + 1)
        lon, lat = S.to_ll.transform(x, y)
        if not (TEAPOT_AREA["min_lat"] < lat < TEAPOT_AREA["max_lat"]
                and TEAPOT_AREA["min_lon"] < lon < TEAPOT_AREA["max_lon"]):
            continue
        tops = S.tops(lat, lon)
        if not tops:
            continue
        res_z = S.depth(RESERVOIR_HORIZON, lat, lon)
        # STRUCTURAL HEIGHT above the crest, in feet. Zero at the crest,
        # growing downdip -- this is the single number the production model
        # and the pay flag both come from.
        below_crest = max(0.0, res_z - crest_z)

        # Drilled in phase order, spread across the campaign.
        if phase == "EXPLORATION":
            yr = start_year
        elif phase == "DELINEATION":
            yr = start_year + 1 + (seq % 2)
        else:
            yr = start_year + 3 + (seq % 3)
        spud = _dt.date(yr, rng.randint(1, 12), rng.randint(1, 28))
        comp = spud + _dt.timedelta(days=rng.randint(28, 95))

        td = round(res_z + rng.uniform(280, 620), 0)
        # A DRY HOLE IS PART OF THE HISTORY. Exploration is mostly wrong, the
        # step-outs find the edge, and development drills what was proved --
        # so the dry-hole rate falls by phase rather than being uniform.
        p_dry = {"EXPLORATION": 0.55, "DELINEATION": 0.28,
                 "DEVELOPMENT": 0.07}[phase]
        # Off-structure is dry regardless of phase: below the spill, the
        # reservoir is wet.
        dry = rng.random() < p_dry or below_crest > 520.0

        if dry:
            qi = months = 0
            status, wtype = "P&A", "DRY"
        else:
            # Arps-ish: initial rate falls off with structural height, so the
            # crest is the best of the field and the flank is marginal.
            qi = 1150.0 * math.exp(-below_crest / 210.0) * rng.uniform(0.8, 1.2)
            qi = max(35.0, qi)
            months = 0 if phase == "EXPLORATION" else prod_years * 12
            if phase == "DELINEATION":
                months = min(months, prod_years * 12 - rng.randint(0, 14))
            status, wtype = "PRODUCING", "OIL"

        wells.append({
            "uwi": _uwi(seq),
            "well_name": f"NPR3 {phase[:4].title()} {seq}-{rng.randint(1, 36)}",
            "operator_name": OPERATOR,
            "field_name": FIELD_NAME,
            "county": "NATRONA",
            "province_state": "WY",
            "surface_latitude": round(lat, 6),
            "surface_longitude": round(lon, 6),
            "final_td": td,
            "kb_elevation": round(5200 + rng.uniform(-140, 140), 1),
            "ground_elevation": round(5188 + rng.uniform(-140, 140), 1),
            "spud_date": spud.isoformat(),
            "completion_date": comp.isoformat(),
            "well_status": status,
            "well_type": wtype,
            # Dome-derived extras, honoured by synth_docs where present.
            "_tops": tops,
            "_qi": round(qi, 1),
            "_months": months,
            "_decline": round(rng.uniform(0.018, 0.041), 4),
            "_phase": phase,
            "_below_crest_ft": round(below_crest, 1),
            "_reservoir_depth_ft": round(res_z, 1),
            "_seis_line": _on_line[0] if _on_line else None,
            "_seis_survey": _on_line[1] if _on_line else None,
            "_seis_trace": _on_line[2] if _on_line else None,
        })
        seq += 1
    return wells


CSV_FIELDS = ["uwi", "well_name", "operator_name", "field_name", "county",
              "province_state", "surface_latitude", "surface_longitude",
              "final_td", "kb_elevation", "ground_elevation", "spud_date",
              "completion_date", "well_status", "well_type"]


def write_csv(wells, path):
    """The well list, in the shape synth_docs.load_wells reads.

    THE DOME-DERIVED EXTRAS DO NOT SURVIVE THE CSV, by design -- they are
    per-well structures, not columns. A caller that wants them passes the dicts
    straight to generate() instead of round-tripping through a file.
    """
    import csv
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        wr.writeheader()
        for w in wells:
            wr.writerow(w)
    return len(wells)
