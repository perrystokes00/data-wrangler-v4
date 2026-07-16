"""geography_layers.py — render DataView v3 spatial geography on the folium map.

Each dv_* table that now carries a `geog GEOGRAPHY` column (fields, land tracts,
boundaries, pipelines, seismic sets — plus well POINTs) is read as WKT and drawn
as its own toggleable folium FeatureGroup, styled per feature type, with a popup
of the row's key attributes.

Kept as a standalone module so page_well_map.py only needs a few one-line calls.

Design notes:
  • geog.STAsText() must run with QUOTED_IDENTIFIER ON (spatial methods require
    it). We set it per-connection via `SET QUOTED_IDENTIFIER ON` at the top of
    each query — SQLAlchemy/pyodbc usually has it on already, but this is a belt-
    and-braces guard so a raw connection can't fail.
  • WKT → GeoJSON via shapely (already a geopandas dependency). No reprojection
    needed: the geography is stored in SRID 4326 (lon/lat) already.
  • Polygons and lines use folium.GeoJson; well points use CircleMarker so they
    read as dots, not shapes.
"""
from __future__ import annotations

import json

try:
    import folium
except Exception:                      # pragma: no cover
    folium = None

try:
    from shapely import wkt as _wkt
    from shapely.geometry import mapping as _mapping
    _HAS_SHAPELY = True
except Exception:                      # pragma: no cover
    _HAS_SHAPELY = False

from sqlalchemy import text


# ── per-layer configuration ────────────────────────────────────────────────
# table, display name, geom kind (poly|line|point), color, the name column and
# the attribute columns to show in the popup.
_LAYERS = {
    "fields": {
        "table": "dataview.dv_field", "name": "🟩 Fields", "kind": "poly",
        "color": "#2e8b57", "fill": "#2e8b57", "fill_op": 0.20,
        "name_col": "field_name",
        "attrs": ["field_name", "field_type", "province_state", "country"],
    },
    "leases": {
        "table": "dataview.dv_land_tract", "name": "🟦 Leases / Tracts", "kind": "poly",
        "color": "#1e6fd6", "fill": "#1e6fd6", "fill_op": 0.15,
        "name_col": "tract_name",
        "attrs": ["tract_name", "lease_number", "operator_name", "province_state"],
    },
    "boundaries": {
        "table": "dataview.dv_boundary", "name": "🟪 Boundaries", "kind": "poly",
        "color": "#7b3fbf", "fill": "#7b3fbf", "fill_op": 0.10,
        "name_col": "boundary_name",
        "attrs": ["boundary_name", "boundary_type", "province_state"],
    },
    "pipelines": {
        "table": "dataview.dv_pipeline", "name": "➖ Pipelines", "kind": "line",
        "color": "#d95f0e", "fill": None, "fill_op": 0.0,
        "name_col": "pipeline_name",
        "attrs": ["pipeline_name", "operator_name", "commodity", "province_state"],
    },
    "seismic": {
        "table": "dataview.dv_seis_set", "name": "🟪 Seismic Surveys", "kind": "poly",
        "color": "#c2185b", "fill": "#c2185b", "fill_op": 0.12,
        "name_col": "seis_set_name",
        "attrs": ["seis_set_name", "seis_set_type"],
    },
}


def _fetch_wkt_rows(engine, table, name_col, attrs):
    """Return [(name, {attr:val,...}, wkt), …] for rows with non-null geog."""
    cols = ", ".join(dict.fromkeys([name_col] + list(attrs)))   # dedupe, keep order
    sql = (f"SET QUOTED_IDENTIFIER ON; "
           f"SELECT {cols}, geog.STAsText() AS _wkt "
           f"FROM {table} WHERE geog IS NOT NULL")
    out = []
    with engine.connect() as con:
        for row in con.execute(text(sql)).mappings():
            wkt = row.get("_wkt")
            if not wkt:
                continue
            props = {a: (None if row.get(a) is None else str(row.get(a)))
                     for a in attrs}
            out.append((row.get(name_col), props, wkt))
    return out


def _geojson_feature(wkt_str, props):
    geom = _wkt.loads(wkt_str)
    return {"type": "Feature", "geometry": _mapping(geom), "properties": props}


def add_geography_layer(m, engine, key, show=False, log=None):
    """Add one configured geography layer (key ∈ _LAYERS) to the folium map `m`.
    Returns the feature count drawn (0 if none / unavailable)."""
    if folium is None or not _HAS_SHAPELY:
        return 0
    cfg = _LAYERS.get(key)
    if not cfg:
        return 0
    try:
        rows = _fetch_wkt_rows(engine, cfg["table"], cfg["name_col"], cfg["attrs"])
    except Exception as e:
        if log:
            log(f"{cfg['name']}: query failed ({str(e).splitlines()[0][:80]})")
        return 0
    if not rows:
        return 0

    feats = []
    for _name, props, wkt_str in rows:
        try:
            feats.append(_geojson_feature(wkt_str, props))
        except Exception:
            continue
    if not feats:
        return 0
    gj = {"type": "FeatureCollection", "features": feats}

    color   = cfg["color"]
    fill    = cfg.get("fill")
    fill_op = cfg.get("fill_op", 0.0)
    is_line = cfg["kind"] == "line"

    def _style(_f, c=color, fc=fill, fo=fill_op, ln=is_line):
        s = {"color": c, "weight": 3 if ln else 1.5,
             "opacity": 0.9, "fillOpacity": 0.0 if ln else fo}
        if fc and not ln:
            s["fillColor"] = fc
        return s

    fg = folium.FeatureGroup(name=cfg["name"], show=show)
    valid_attrs = [a for a in cfg["attrs"]]
    folium.GeoJson(
        gj, style_function=_style,
        highlight_function=lambda _f: {"weight": 5, "color": "#ffcc00"},
        tooltip=folium.GeoJsonTooltip(fields=valid_attrs, sticky=True),
        popup=folium.GeoJsonPopup(fields=valid_attrs, max_width=320),
    ).add_to(fg)
    fg.add_to(m)
    return len(feats)


def add_well_points(m, engine, show=False, color="#f97316", log=None):
    """Draw dv_well.geog POINTs as small orange CircleMarkers (a light spatial
    dot layer, distinct from the rich clustered Wells layer). Returns count."""
    if folium is None:
        return 0
    sql = ("SET QUOTED_IDENTIFIER ON; "
           "SELECT well_name, geog.Lat AS lat, geog.Long AS lon "
           "FROM dataview.dv_well WHERE geog IS NOT NULL")
    try:
        with engine.connect() as con:
            rows = list(con.execute(text(sql)).mappings())
    except Exception as e:
        if log:
            log(f"well points: query failed ({str(e).splitlines()[0][:80]})")
        return 0
    if not rows:
        return 0
    fg = folium.FeatureGroup(name="⚫ Well points (geog)", show=show)
    n = 0
    for r in rows:
        lat, lon = r.get("lat"), r.get("lon")
        if lat is None or lon is None:
            continue
        folium.CircleMarker(
            [float(lat), float(lon)], radius=3,
            color=color, weight=1, fill=True,
            fill_color=color, fill_opacity=0.75,
            tooltip=str(r.get("well_name") or "")).add_to(fg)
        n += 1
    fg.add_to(m)
    return n


def add_all_geography(m, engine, keys=None, well_points=False, log=None):
    """Convenience: add every configured geography layer (or a subset via `keys`).
    Returns {layer_key: count}. LayerControl in the caller lets users toggle them."""
    counts = {}
    for k in (keys or _LAYERS.keys()):
        counts[k] = add_geography_layer(m, engine, k, show=False, log=log)
    if well_points:
        counts["well_points"] = add_well_points(m, engine, show=False, log=log)
    return counts
