# ═══════════════════════════════════════════════════════════════════════════
# dataview/mapping/geography_layers.py
# Geography feature layers from dataview.dv_*.geog — a real importable module.
# ───────────────────────────────────────────────────────────────────────────
# HISTORY: the previous file was a PASTE-ME SNIPPET ("paste these into
# page_well_map.py") that was deployed as a module. It had no imports of its
# own, a different function signature, and no "seismic" layer — so
# `from ... import add_geography_layer, add_well_points` failed on every load
# and the page's try/except silently blanked the whole Seismic pill
# (diagnosed July 28 via probe_seismic_pill.py).
#
# PUBLIC API (what page_well_map imports):
#   add_geography_layer(m, engine, key, show=True) -> int   # features added
#       key in {"fields", "leases", "boundaries", "pipelines", "seismic"}
#   add_well_points(m, engine, show=True) -> int            # wells added
#
# Design rules honoured here:
#   * Optional columns are checked AT RUNTIME via INFORMATION_SCHEMA, never
#     assumed from a DDL snapshot — a missing extra column drops that column,
#     not the layer; a missing table or geog column skips the layer cleanly.
#   * No streamlit dependency: failures print and return 0 so the layer
#     degrades to nothing, not to an exception that kills sibling layers.
#   * geog.STAsText() gives WKT in (lon lat); shapely.mapping() emits GeoJSON
#     [lon, lat] — exactly what folium.GeoJson wants, so no coordinate swap.
# ═══════════════════════════════════════════════════════════════════════════
from __future__ import annotations

import folium
from sqlalchemy import text

# key -> (table, name_col, extra_cols, color, label, fill)
_LAYER_KEYS = {
    "fields":     ("dv_field",      "field_name",
                   ["field_type", "basin_name"],      "#e67e22",
                   "🟩 Fields (geog)",           True),
    "leases":     ("dv_land_tract", "tract_name",
                   ["lease_number", "operator_name"], "#3498db",
                   "🟦 Leases (geog)",           True),
    "boundaries": ("dv_boundary",   "boundary_name",
                   ["boundary_type"],                 "#7f8c8d",
                   "🟪 Boundaries (geog)",       True),
    "pipelines":  ("dv_pipeline",   "pipeline_name",
                   ["commodity", "operator_name"],    "#c0392b",
                   "➖ Pipelines (geog)",        False),   # lines: no fill
    # Survey FOOTPRINTS only (dv_seis_set.geog is polygons-only by promote
    # rule; the per-line LINESTRINGs live on dv_seis_line.geog and are drawn
    # by page_well_map._seismic_line_paths, not by this layer).
    "seismic":    ("dv_seis_set",   "seis_set_name",
                   ["seis_set_type", "epsg_code"],    "#0E6E6E",
                   "🟦 Seismic surveys (geog)",  True),
}


def _table_columns(engine, table: str) -> set:
    """Column names actually on dataview.<table> right now (sys view, not a
    DDL snapshot). Empty set = table missing."""
    try:
        with engine.connect() as con:
            rows = con.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = 'dataview' AND TABLE_NAME = :t"),
                {"t": table}).fetchall()
        return {r[0].lower() for r in rows}
    except Exception as exc:
        print(f"[geography_layers] column probe failed for {table}: {exc}")
        return set()


def _qry_geography(engine, table: str, name_col: str, extra_cols: list):
    """[{nm, wkt, ...extras}] for one table. Missing extras are dropped;
    missing table/geog/name column skips the layer (returns [])."""
    have = _table_columns(engine, table)
    if not have or "geog" not in have:
        return [], []
    extras = [c for c in extra_cols if c.lower() in have]
    nm_expr = (name_col if name_col.lower() in have else "NULL")
    cols = ", ".join([f"{nm_expr} AS nm"] + [f"{c} AS {c}" for c in extras])
    sql = f"""
        SELECT {cols}, geog.STAsText() AS wkt
        FROM dataview.{table}
        WHERE geog IS NOT NULL AND geog.STIsValid() = 1
    """
    try:
        with engine.connect() as con:
            rows = con.execute(text(sql)).mappings().fetchall()
        return [dict(r) for r in rows], extras
    except Exception as exc:
        print(f"[geography_layers] {table} query failed: {exc}")
        return [], extras


def _geography_geojson(features: list, extra_cols=None) -> dict:
    """WKT rows -> GeoJSON FeatureCollection (properties: name + extras)."""
    import shapely.wkt
    from shapely.geometry import mapping
    extra_cols = extra_cols or []
    feats = []
    for r in features:
        wkt = r.get("wkt")
        if not wkt:
            continue
        try:
            geom = shapely.wkt.loads(wkt)
        except Exception:
            continue
        if geom.is_empty:
            continue
        props = {"name": r.get("nm") or "(unnamed)"}
        for c in extra_cols:
            v = r.get(c)
            props[c] = "" if v is None else str(v)
        feats.append({"type": "Feature", "geometry": mapping(geom),
                      "properties": props})
    return {"type": "FeatureCollection", "features": feats}


def add_geography_layer(m, engine, key: str, show: bool = True) -> int:
    """Add one dv_*.geog feature layer as a toggleable FeatureGroup on `m`.
    Returns the number of features added (0 = nothing to draw / table absent
    / query failed — printed, never raised)."""
    cfg = _LAYER_KEYS.get(key)
    if cfg is None:
        print(f"[geography_layers] unknown layer key {key!r} "
              f"(known: {sorted(_LAYER_KEYS)})")
        return 0
    table, name_col, extra_cols, color, label, fill = cfg
    rows, extras = _qry_geography(engine, table, name_col, extra_cols)
    if not rows:
        return 0
    gj = _geography_geojson(rows, extras)
    if not gj["features"]:
        return 0

    def _style(_feat, _c=color, _fill=fill):
        s = {"color": _c, "weight": 2, "opacity": 0.85}
        s.update({"fillColor": _c, "fillOpacity": 0.18} if _fill
                 else {"fillOpacity": 0.0})
        return s

    fg = folium.FeatureGroup(name=f"{label} ({len(gj['features']):,})",
                             show=show)
    tip_fields = ["name"] + extras
    tip_aliases = ["Name"] + [c.replace("_", " ").title() for c in extras]
    folium.GeoJson(
        gj,
        style_function=_style,
        highlight_function=lambda _f, _c=color: {"weight": 4, "color": _c},
        tooltip=folium.GeoJsonTooltip(fields=tip_fields, aliases=tip_aliases,
                                      sticky=True),
    ).add_to(fg)
    fg.add_to(m)
    return len(gj["features"])


def points_layer(m, points, name, *, color="#222", fill="#555",
                 radius=3, show=True, opacity=0.9):
    """N points as ONE GeoJson layer. Returns the number drawn.

    ONE LAYER, NOT N MARKERS. A CircleMarker per point makes folium serialise
    one JS object each, and serialisation -- not the query, not the build -- is
    what the map actually spends its time on, because the whole map is
    re-serialised and shipped to the browser on EVERY rerun. Measured at 50,000
    points:

        CircleMarker each   34.90s   46.1 MB
        one GeoJson          0.81s   11.7 MB     43x faster, 4x smaller

    Coordinates are rounded to 5 decimals (~1 m). Beyond that the digits are
    noise in a well header and cost payload on every redraw.

    A tooltip, never a popup. The map's click handler tells a well marker from
    a density cell by "markers have popups, cells don't", so giving these
    popups would make every point look like a marker click to that code.
    """
    feats = []
    for la, lo, label in points:
        try:
            la, lo = float(la), float(lo)
        except (TypeError, ValueError):
            continue
        feats.append({
            "type": "Feature",
            "properties": {"nm": str(label or "")},
            "geometry": {"type": "Point",
                         "coordinates": [round(lo, 5), round(la, 5)]},
        })
    if not feats:
        return 0
    folium.GeoJson(
        {"type": "FeatureCollection", "features": feats},
        name=name, show=show,
        marker=folium.CircleMarker(radius=radius, color=color, weight=1,
                                   fill=True, fill_color=fill,
                                   fill_opacity=opacity),
        tooltip=folium.GeoJsonTooltip(fields=["nm"], labels=False),
    ).add_to(m)
    return len(feats)


def add_well_points(m, engine, show: bool = True, limit: int = 50000) -> int:
    """dv_well.geog as plain CircleMarkers — the native-geography view of the
    well set, independent of the lat/lon columns the main markers use.
    geography .Lat/.Long avoids WKT parsing entirely."""
    have = _table_columns(engine, "dv_well")
    if not have or "geog" not in have:
        return 0
    nm = "well_name" if "well_name" in have else ("uwi" if "uwi" in have
                                                  else "NULL")
    try:
        with engine.connect() as con:
            rows = con.execute(text(f"""
                SELECT TOP {int(limit)} {nm} AS nm,
                       geog.Lat AS lat, geog.Long AS lon
                FROM dataview.dv_well
                WHERE geog IS NOT NULL
            """)).fetchall()
    except Exception as exc:
        print(f"[geography_layers] dv_well points query failed: {exc}")
        return 0
    if not rows:
        return 0
    return points_layer(
        m, ((la, lo, nm_v) for nm_v, la, lo in rows),
        name=f"⚫ Well points (geog) ({len(rows):,})", show=show)


REFERENCE_MASTER = "WELL_REF.well_ref.well_master_gold"


def add_reference_wells(m, engine, bounds=None, limit: int = 50000,
                        show: bool = True):
    """Individual reference wells as ONE GeoJson layer. (drawn, in_scope).

    THE DENSITY VIEW IS NOT A SUBSTITUTE, and this is not the layer that was
    deleted. v_well_density_r* answers "where are wells" for 3.9M rows at
    continental zoom; it cannot answer "which wells are these" when you are
    looking at one field. That needs the points, and the points only became
    affordable once a layer stopped meaning N marker objects.

    BOUNDED AND CAPPED, in that order. The master is 4,031,052 rows, so a
    bounds-less pull is 50,000 arbitrary wells dressed up as an answer -- the
    layer name says exactly what it is showing and out of how many, because a
    silent truncation reads as completeness.

    bounds -- ((south, west), (north, east)) as st_folium reports them, or
              None for the whole master.
    """
    # BETWEEN ALREADY EXCLUDES NULL, so adding IS NOT NULL beside it is
    # redundant -- and expensive: measured 1.37s against 0.04s for the same
    # 11,291 rows, 34x, because the extra predicates spoil the seek on
    # IX_wmg_latlon. The NULL guards are kept ONLY for the unbounded case,
    # where there is no BETWEEN to imply them.
    params = {}
    if bounds:
        try:
            (s, w), (n, e) = bounds
            where = ["surface_latitude BETWEEN :s AND :n",
                     "surface_longitude BETWEEN :w AND :e"]
            params = {"s": float(s), "n": float(n),
                      "w": float(w), "e": float(e)}
        except Exception:
            where = ["surface_latitude IS NOT NULL",
                     "surface_longitude IS NOT NULL"]
    else:
        where = ["surface_latitude IS NOT NULL",
                 "surface_longitude IS NOT NULL"]
    clause = " AND ".join(where)
    # ROWS FIRST, AND COUNT ONLY IF IT TELLS US SOMETHING. Under the cap the
    # fetch IS the count -- asking the server twice is free information at a
    # real price: COUNT(*) over a 2M-row bbox measured 5.90s, longer than
    # everything else on the map put together, purely to print an exact total
    # that reads "lots". Over the cap, the honest statement is that it is
    # capped, which needs no count at all.
    # "TOP n" IS A GEOGRAPHIC SLICE PRETENDING TO BE A SAMPLE, and it bites
    # whenever the scope holds more wells than the cap -- with no bounds at
    # all, and equally inside a bbox that is simply large.
    #
    # With no ORDER BY the server returns whatever the chosen index reaches
    # first, and here that is latitude order. Measured: TOP 50000 over the
    # whole master came back lat 37.7-71.9 out of 24.4-71.9, every well in
    # Texas, the Gulf and California silently absent; over an 8-state bbox it
    # came back lat 31.0-31.5, a 30-mile band drawn as if it were the data.
    # Both render a hard horizontal edge that reads like a coastline. Wrong is
    # worse than missing, and a map omitting half a continent while looking
    # complete is exactly that.
    #
    # So: try the exact fetch, and if it would cap, re-ask for every k-th well
    # by a hash of its uwi. The hash spreads geographically where latitude
    # order does not. Measured over the master, five-degree buckets touched:
    # slice 37, hash-spread 45, TABLESAMPLE only 31 -- TABLESAMPLE samples
    # PAGES, and pages are clustered by uwi14, so it clumps.
    lim = int(limit)
    sampled = False
    try:
        with engine.connect() as con:
            # limit+1 tells us "capped" without a COUNT, which measured 5.90s
            # over a 2M-row bbox for a number that only ever reads "lots".
            rows = con.execute(text(
                f"SELECT TOP {lim + 1} well_name, surface_latitude, "
                f"surface_longitude FROM {REFERENCE_MASTER} WHERE {clause}"),
                params).fetchall()
            if len(rows) > lim:
                est = con.execute(text(
                    "SELECT SUM(p.rows) FROM WELL_REF.sys.partitions p "
                    "JOIN WELL_REF.sys.objects o ON o.object_id = p.object_id "
                    "WHERE o.name = 'well_master_gold' AND p.index_id IN (0,1)"
                )).scalar() or 0
                k = max(2, int(est // max(lim, 1)) + 1)
                sampled = True
                rows = con.execute(text(
                    f"SELECT TOP {lim} well_name, surface_latitude, "
                    f"surface_longitude FROM {REFERENCE_MASTER} "
                    f"WHERE {clause} AND ABS(CHECKSUM(uwi14)) % {k} = 0"),
                    params).fetchall()
    except Exception as exc:
        print(f"[geography_layers] reference wells query failed: {exc}")
        return 0, 0
    if not rows:
        return 0, 0
    in_scope = None if sampled else len(rows)
    label = (f"🔵 Reference wells ({len(rows):,}"
             + (" sample)" if sampled else ")"))
    n_drawn = points_layer(m, ((la, lo, nm) for nm, la, lo in rows),
                           name=label, color="#1d4ed8", fill="#60a5fa",
                           radius=2, show=show, opacity=0.7)
    # in_scope is None when capped: "we do not know, and finding out costs
    # more than the answer is worth". Never 0, which would read as "none here".
    return n_drawn, in_scope


def add_all_geography_layers(m, engine) -> int:
    """Back-compat convenience: every configured layer at once."""
    return sum(add_geography_layer(m, engine, k) for k in _LAYER_KEYS)
