r"""Horizons for the Teapot Dome model: the surface, its contours, and the
sampling the section overlay uses.

THE HORIZONS AND THE SEISMIC ARE THE SAME SURFACE. Both are built from
synth_seismic.teapot_model(), so a pick sits on its reflector by construction
rather than because two sets of numbers happen to agree. A horizon that floats
off its reflector is worse than no horizon at all: it looks like an
interpretation, it plots, it exports, and it is wrong everywhere at once.

Three representations, one surface:

    grid       what time is this horizon at this position   -- section overlay
    contours   draw me the structure                        -- map
    sample()   the same question, for arbitrary points      -- section overlay

The map does not read the grid. Shipping tens of thousands of nodes to the
browser to render a picture that twenty polylines already convey is how the H3
layer got to 28 seconds before it was rewritten as one GeoJSON.
"""
import math

import numpy as np

from dataview.migration.synth_seismic import (
    TEAPOT_AREA, TEAPOT_HORIZONS, teapot_model)

CREATED_BY = "SYNTH_HORIZON"


def build_grid(dome, to_utm, horizon_ms, area=None, nrow=90, ncol=70):
    """(lats, lons, values) for one horizon on a regular lat/lon mesh.

    Values are two-way time in ms. The mesh is in GEOGRAPHIC coordinates
    because that is what the map and the section overlay both index by, but
    every node is projected before it asks the dome for a time -- the model
    lives in metres and evaluating it in degrees would stretch the structure
    east-west by the cosine of the latitude.
    """
    area = area or TEAPOT_AREA
    lats = np.linspace(area["min_lat"], area["max_lat"], nrow)
    lons = np.linspace(area["min_lon"], area["max_lon"], ncol)
    vals = np.empty((nrow, ncol), dtype=float)
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            x, y = to_utm.transform(lo, la)
            vals[i, j] = dome.t0(x, y, horizon_ms)
    return lats, lons, vals


def contours(lats, lons, vals, levels=None, step=10.0):
    """[(value, [(lat, lon), ...]), ...] -- contour polylines of the surface.

    matplotlib's contouring is used rather than a hand-rolled marching squares:
    it is already a dependency (the log and section plots use it), it handles
    saddles and open segments correctly, and a contour that closes wrongly is
    the kind of error that looks like structure.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if levels is None:
        lo = math.floor(np.nanmin(vals) / step) * step
        hi = math.ceil(np.nanmax(vals) / step) * step
        levels = np.arange(lo, hi + step, step)

    fig = plt.figure()
    try:
        ax = fig.add_subplot(111)
        cs = ax.contour(lons, lats, vals, levels=levels)
        out = []
        # Matplotlib 3.8 replaced .collections with .allsegs/.levels; allsegs
        # is present in both, so it is what this reads.
        for value, segs in zip(cs.levels, cs.allsegs):
            for seg in segs:
                if len(seg) < 2:
                    continue                    # a point is not a contour
                out.append((float(value),
                            [(float(p[1]), float(p[0])) for p in seg]))
        return out
    finally:
        plt.close(fig)


def sample(lats, lons, vals, lat, lon):
    """The horizon's value at one position, bilinearly interpolated, or None.

    OUTSIDE THE GRID IS None, NOT THE NEAREST EDGE. Clamping to the edge would
    extend a Teapot horizon flat across Wyoming and draw it confidently on a
    section that has never seen it.
    """
    if lat < lats[0] or lat > lats[-1] or lon < lons[0] or lon > lons[-1]:
        return None
    i = int(np.searchsorted(lats, lat) - 1)
    j = int(np.searchsorted(lons, lon) - 1)
    i = min(max(i, 0), len(lats) - 2)
    j = min(max(j, 0), len(lons) - 2)
    dy = (lat - lats[i]) / (lats[i + 1] - lats[i])
    dx = (lon - lons[j]) / (lons[j + 1] - lons[j])
    v00, v01 = vals[i, j], vals[i, j + 1]
    v10, v11 = vals[i + 1, j], vals[i + 1, j + 1]
    if any(v is None or not np.isfinite(v) for v in (v00, v01, v10, v11)):
        return None
    return float((v00 * (1 - dx) + v01 * dx) * (1 - dy)
                 + (v10 * (1 - dx) + v11 * dx) * dy)


def teapot_horizons(seed=None, nrow=90, ncol=70, contour_step=10.0):
    """Every Teapot horizon, ready to load. [(meta, grid, contours), ...].

    meta carries the extent, so a consumer can refuse to draw the horizon on a
    line that lies outside it rather than extrapolating.
    """
    dome, to_utm, _to_ll = teapot_model() if seed is None else teapot_model(seed)
    out = []
    for k, (name, t_ms, colour, strat) in enumerate(TEAPOT_HORIZONS, start=1):
        lats, lons, vals = build_grid(dome, to_utm, t_ms, nrow=nrow, ncol=ncol)
        segs = contours(lats, lons, vals, step=contour_step)
        meta = {
            "horizon_id": f"TPD_H{k}",
            "horizon_name": name,
            "horizon_type": "SEISMIC MARKER",
            "strat_unit_name": strat,
            "seq_no": k,
            "pick_domain": "TIME",
            "pick_uom": "MS",
            "min_value": float(np.nanmin(vals)),
            "max_value": float(np.nanmax(vals)),
            "bbox_min_lat": float(lats[0]), "bbox_max_lat": float(lats[-1]),
            "bbox_min_lon": float(lons[0]), "bbox_max_lon": float(lons[-1]),
            "display_colour": colour,
            "interpreter": CREATED_BY,
            "remark": ("Synthetic. Generated from the same structural model as "
                       "the Teapot Dome 2D SEG-Y, so picks tie to reflectors."),
        }
        out.append((meta, (lats, lons, vals), segs))
    return out
