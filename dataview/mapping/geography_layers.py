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


def add_well_points(m, engine, show: bool = True, limit: int = 5000) -> int:
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
    fg = folium.FeatureGroup(name=f"⚫ Well points (geog) ({len(rows):,})",
                             show=show)
    for nm_v, la, lo in rows:
        try:
            la, lo = float(la), float(lo)
        except (TypeError, ValueError):
            continue
        folium.CircleMarker(
            location=[la, lo], radius=3, color="#222", weight=1,
            fill=True, fill_color="#555", fill_opacity=0.9,
            tooltip=str(nm_v or ""),
        ).add_to(fg)
    fg.add_to(m)
    return len(rows)


def add_all_geography_layers(m, engine) -> int:
    """Back-compat convenience: every configured layer at once."""
    return sum(add_geography_layer(m, engine, k) for k in _LAYER_KEYS)
