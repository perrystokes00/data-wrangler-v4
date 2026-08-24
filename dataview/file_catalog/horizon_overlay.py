r"""Horizon times sampled at a seismic line's trace positions.

This is the bridge between dv_seis_horizon_grid and a section plot: given the
ground positions of a line's traces, it returns the two-way time of every
horizon that actually covers them.

TWO RULES, BOTH ABOUT NOT DRAWING SOMETHING FALSE.

  1. OUTSIDE THE GRID IS NOTHING, not the nearest edge. Clamping would run a
     Teapot horizon flat across Wyoming and draw it, confidently, on a section
     that has never seen it.
  2. A HORIZON THAT COVERS ALMOST NONE OF THE LINE IS NOT DRAWN AT ALL. A
     three-trace clip at the very end of a section reads as a pick, and the eye
     will follow it across the whole line.

The grids are cached per process: 25,200 nodes is 4 horizons of 90x70, and
re-reading them for every rerun of a Streamlit page is the kind of quiet cost
that turns into "the viewer is slow".
"""
import numpy as np

_CACHE = {}


def _fetch_grids(engine):
    """{horizon_id: (meta, lats, lons, values)} from the database, or {}."""
    from sqlalchemy import text
    try:
        with engine.connect() as con:
            if con.execute(text(
                    "SELECT OBJECT_ID('dataview.dv_seis_horizon_grid','U')"
            )).scalar() is None:
                return {}
            metas = con.execute(text("""
                SELECT horizon_id, horizon_name, display_colour, seq_no,
                       pick_domain, pick_uom,
                       bbox_min_lat, bbox_max_lat, bbox_min_lon, bbox_max_lon
                  FROM dataview.dv_seis_horizon
                 WHERE ISNULL(active_ind,'Y') = 'Y'
                 ORDER BY seq_no
            """)).fetchall()
            out = {}
            for m in metas:
                rows = con.execute(text("""
                    SELECT row_no, col_no, latitude, longitude, value
                      FROM dataview.dv_seis_horizon_grid
                     WHERE horizon_id = :h AND ISNULL(active_ind,'Y') = 'Y'
                     ORDER BY row_no, col_no
                """), {"h": m[0]}).fetchall()
                if not rows:
                    continue
                nr = max(r[0] for r in rows) + 1
                nc = max(r[1] for r in rows) + 1
                if nr * nc != len(rows):
                    # A ragged grid cannot be reshaped, and reshaping it anyway
                    # would silently transpose the surface.
                    print(f"[horizons] {m[0]}: {len(rows)} nodes is not "
                          f"{nr}x{nc}; skipped")
                    continue
                vals = np.full((nr, nc), np.nan)
                lats = np.zeros(nr)
                lons = np.zeros(nc)
                for r0, c0, la, lo, v in rows:
                    lats[r0] = float(la)
                    lons[c0] = float(lo)
                    if v is not None:
                        vals[r0, c0] = float(v)
                meta = {"horizon_id": m[0], "name": m[1] or m[0],
                        "colour": m[2] or "#E4572E", "seq": m[3] or 0,
                        "domain": m[4] or "TIME", "uom": m[5] or "MS"}
                out[m[0]] = (meta, lats, lons, vals)
            return out
    except Exception as exc:
        print(f"[horizons] grid load failed: {exc}")
        return {}


def grids(engine, refresh=False):
    """Cached {horizon_id: (meta, lats, lons, values)}."""
    key = str(getattr(engine, "url", "default"))
    if refresh or key not in _CACHE:
        _CACHE[key] = _fetch_grids(engine)
    return _CACHE[key]


def _bilinear(lats, lons, vals, lat, lon):
    """Interpolated value, or None outside the grid / next to a hole."""
    if not (lats[0] <= lat <= lats[-1] and lons[0] <= lon <= lons[-1]):
        return None
    i = min(max(int(np.searchsorted(lats, lat) - 1), 0), len(lats) - 2)
    j = min(max(int(np.searchsorted(lons, lon) - 1), 0), len(lons) - 2)
    v = vals[i:i + 2, j:j + 2]
    if not np.all(np.isfinite(v)):
        return None
    dy = (lat - lats[i]) / (lats[i + 1] - lats[i])
    dx = (lon - lons[j]) / (lons[j + 1] - lons[j])
    return float((v[0, 0] * (1 - dx) + v[0, 1] * dx) * (1 - dy)
                 + (v[1, 0] * (1 - dx) + v[1, 1] * dx) * dy)


def for_positions(engine, lat_lons, min_coverage=0.25):
    """[{name, colour, times}] for the horizons covering these trace positions.

    lat_lons is [(lat, lon), ...], one per trace, in trace order. times is a
    list the same length, with None where the horizon does not reach.

    min_coverage is the share of traces a horizon must cover to be offered at
    all -- see rule 2 in the module docstring.
    """
    if not lat_lons:
        return []
    out = []
    for _hid, (meta, lats, lons, vals) in sorted(
            grids(engine).items(), key=lambda kv: kv[1][0]["seq"]):
        times = [_bilinear(lats, lons, vals, la, lo) for la, lo in lat_lons]
        hit = sum(1 for t in times if t is not None)
        if hit < max(2, int(min_coverage * len(times))):
            continue
        out.append({"name": meta["name"], "colour": meta["colour"],
                    "domain": meta["domain"], "uom": meta["uom"],
                    "times": times, "coverage": hit / len(times)})
    return out


def for_segy(path, engine=None, max_traces=None):
    """[{name, colour, times}] for a SEG-Y file, read from its own headers.

    The file states its CRS and carries a coordinate per trace, so nothing has
    to be passed in alongside it. Returns [] rather than raising when the file
    has no usable geometry -- a section with no coordinates simply cannot be
    tied to a horizon, and that is a fact about the file, not an error.
    """
    try:
        from dataview.file_catalog.segy_header import read_segy_header
        from dataview.file_catalog.crs_from_segy import crs_from_text
        from pyproj import Transformer
    except Exception as exc:
        print(f"[horizons] cannot read line geometry: {exc}")
        return []
    if engine is None:
        try:
            from dataview.core.dw_utils import make_engine
            engine = make_engine("DataView_Demo")
        except Exception as exc:
            print(f"[horizons] no database: {exc}")
            return []

    h = read_segy_header(path)
    pts = h.get("cdp_points") or []
    if not h.get("ok") or len(pts) < 2:
        return []
    epsg, _how, _note = crs_from_text(h.get("textual_header"))
    if not epsg:
        # WITHOUT A CRS THE COORDINATES ARE JUST NUMBERS. Guessing one would
        # put the line somewhere plausible and wrong, and the horizon would be
        # sampled there.
        return []
    try:
        to_ll = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326",
                                     always_xy=True)
        ll = [tuple(reversed(to_ll.transform(x, y))) for x, y in pts]
    except Exception as exc:
        print(f"[horizons] reprojection from EPSG:{epsg} failed: {exc}")
        return []
    if max_traces and len(ll) > max_traces:
        idx = np.linspace(0, len(ll) - 1, max_traces).astype(int)
        ll = [ll[i] for i in idx]
    return for_positions(engine, ll)
