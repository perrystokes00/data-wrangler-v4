"""
page_federation_search.py  —  DataView V3
==========================================
Federation search page. Pydeck-based rendering, fast at 50K+ wells.
Selection via pre-registered spatial layers (dv_spatial_layer) — no
on-map drawing, no Folium round-trips. Layers register through
modules.dv_spatial_loader once, then become reusable AOIs in this
and any future pages.

Flow:
    State -> Counties (multi) -> Operators (multi, optional) ->
    Fields (multi, optional) -> Load Wells ->
    Sidebar attribute filters (TD, Spud year, Comp year, Status, Type) ->
    Optional: pick a registered Spatial Layer to limit selection to
              wells inside the layer's polygon(s) ->
    Results table + CSV/Excel exports

Wired into app.py's navigation:
    elif S.app_mode == "federation_search":
        try:
            from dataview.db_explorer import page_federation_search
            page_federation_search.render(S.engine)
        except Exception as e:
            st.error(f"Federation Search error: {e}")

Architecture notes:
    - Uses modules.db.fetch_all() for queries
    - Uses modules.db_dialect.get_dialect() for cross-engine SQL
    - Reads spatial layers from dataview.dv_spatial_layer (geometry_wkt)
    - Pydeck for map rendering — fast at 50K+ wells, GPU-accelerated
    - No on-map drawing; AOI selection is layer-based, server-resident

Dependencies (install if not already present):
    pip install pydeck shapely openpyxl
"""
from __future__ import annotations

import io
import time
from typing import Optional

import pandas as pd
import pydeck as pdk
import streamlit as st

try:
    from shapely import wkt as shapely_wkt
    from shapely.geometry import Point
    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False

from dataview.core.db import fetch_all
from dataview.core.db_dialect import get_dialect


DV_SCHEMA = "dataview"
DV_WELL = "dv_well"
DV_SPATIAL = "dv_spatial_layer"
WELLS_LIMIT = 50000
LOOKUP_LIMIT = 500

STATUS_COLORS = {
    "active":  [34, 197, 94],
    "produc":  [34, 197, 94],
    "plug":    [239, 68, 68],
    "abandon": [239, 68, 68],
    "drill":   [59, 130, 246],
    "permit":  [245, 158, 11],
    "shut":    [168, 85, 247],
    "idle":    [168, 85, 247],
}
DEFAULT_COLOR = [107, 114, 128]


def render(engine):
    if engine is None:
        st.error("No active database connection. "
                 "Connect via the sidebar first.")
        return

    dialect = get_dialect(engine)
    dv_well = dialect.qualified(DV_SCHEMA, DV_WELL)

    st.title("🌐 Federation Search")
    st.caption(f"Search wells in `{DV_SCHEMA}.{DV_WELL}`. "
               "Filter by state/county/operator/field; optionally "
               "narrow with a registered spatial layer (AOI). "
               f"Engine: {dialect.name}")

    st.markdown("### Filters")
    c1, c2, c3, c4, c5 = st.columns([2, 3, 3, 3, 1])

    try:
        states = _states(engine, dialect, dv_well)
    except Exception as e:
        st.error(f"Could not query {DV_SCHEMA}.{DV_WELL}.")
        st.exception(e)
        return

    if not states:
        st.warning(f"No wells found in {DV_SCHEMA}.{DV_WELL}.")
        return

    with c1:
        state_opts = [f"{s['province_state']}  ({s['wells']:,})"
                      for s in states]
        state_choice = st.selectbox("State", options=state_opts,
                                    key="fs_state")
        selected_state = states[state_opts.index(state_choice)] \
                         ["province_state"]

    with c2:
        counties = _counties(engine, dialect, dv_well, selected_state)
        county_opts = [f"{c['county']}  ({c['wells']:,})"
                       for c in counties]
        county_choices = st.multiselect("Counties", options=county_opts,
                                        key="fs_counties")
        selected_counties = tuple(
            counties[county_opts.index(c)]["county"]
            for c in county_choices
        )

    with c3:
        if selected_counties:
            ops = _operators(engine, dialect, dv_well,
                             selected_state, selected_counties)
            op_opts = [f"{o['operator_name']}  ({o['wells']:,})"
                       for o in ops]
            op_choices = st.multiselect("Operators (optional)",
                                        options=op_opts, key="fs_ops")
            selected_ops = tuple(
                ops[op_opts.index(o)]["operator_name"]
                for o in op_choices
            )
        else:
            st.multiselect("Operators (optional)", options=[],
                           disabled=True, key="fs_ops_disabled")
            selected_ops = tuple()

    with c4:
        if selected_counties:
            fields = _fields(engine, dialect, dv_well,
                             selected_state, selected_counties,
                             selected_ops)
            f_opts = [f"{f['field_name']}  ({f['wells']:,})"
                      for f in fields]
            f_choices = st.multiselect("Fields (optional)",
                                       options=f_opts, key="fs_fields")
            selected_fields = tuple(
                fields[f_opts.index(f)]["field_name"]
                for f in f_choices
            )
        else:
            st.multiselect("Fields (optional)", options=[],
                           disabled=True, key="fs_fields_disabled")
            selected_fields = tuple()

    with c5:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        load_clicked = st.button(
            "Load Wells", type="primary",
            use_container_width=True,
            disabled=not selected_counties,
        )

    if load_clicked:
        with st.spinner(f"Querying {DV_SCHEMA}.{DV_WELL}..."):
            wells, elapsed = _load_wells(
                engine, dialect, dv_well,
                selected_state, selected_counties,
                selected_ops, selected_fields,
            )
        st.session_state["_fs_wells"] = wells
        st.session_state["_fs_elapsed"] = elapsed
        st.success(f"Loaded {len(wells):,} wells in {elapsed:.1f}s")

    _all_wells = st.session_state.get("_fs_wells", [])

    sidebar_state = _render_sidebar_filters(_all_wells)

    if _all_wells:
        wells = [w for w in _all_wells if _passes_filters(w, sidebar_state)]
        diff = len(_all_wells) - len(wells)
        if diff > 0:
            st.info(f"🔧 Sidebar filters: **{len(_all_wells):,}** -> "
                    f"**{len(wells):,}** wells ({diff:,} excluded).")
    else:
        wells = []

    st.markdown("### AOI (optional)")
    aoi_layer = _render_aoi_picker(engine, dialect)

    if aoi_layer and wells:
        if not HAS_SHAPELY:
            st.warning("Install `shapely` to enable AOI filtering: "
                       "`pip install shapely`. Showing all wells.")
            selected_wells = wells
        else:
            geom = aoi_layer.get("_shapely_geom")
            if geom is None:
                st.warning(f"Could not parse geometry for layer "
                           f"'{aoi_layer.get('layer_name')}'. "
                           "Showing all wells.")
                selected_wells = wells
            else:
                t0 = time.time()
                selected_wells = [
                    w for w in wells
                    if geom.contains(Point(w["lon"], w["lat"]))
                ]
                dt = time.time() - t0
                st.info(
                    f"📐 AOI **{aoi_layer['layer_name']}** contains "
                    f"**{len(selected_wells):,}** of {len(wells):,} "
                    f"wells ({dt:.2f}s point-in-polygon)."
                )
    else:
        selected_wells = wells

    st.markdown("### Map")
    _render_map(wells, selected_wells, aoi_layer)

    st.markdown("### Selected wells")
    _render_results_table(selected_wells)

    with st.expander("How Federation Search works"):
        st.markdown(
            "**Flow:**\n"
            "1. Pick a state, then one or more counties\n"
            "2. Optionally narrow by operator or field\n"
            f"3. Click **Load Wells** to fetch from `{DV_SCHEMA}.{DV_WELL}`\n"
            "4. Use sidebar filters to narrow attributes "
            "(TD, year, status, type)\n"
            "5. Optionally pick a Spatial Layer (AOI) to limit the "
            "selection to wells inside the layer's polygon(s)\n"
            "6. Results table -> CSV or Excel export\n\n"
            "**Spatial layers:**\n"
            f"- AOI polygons live in `{DV_SCHEMA}.dv_spatial_layer`\n"
            "- Register new layers via the Spatial Layers page "
            "(GeoJSON or shapefile upload)\n"
            "- Layers appear in the picker for any user of this page"
        )


@st.cache_data(ttl=300, show_spinner=False)
def _states(_engine, _dialect, dv_well):
    q = _dialect.quote
    sql = f"""
        SELECT {q('province_state')} AS province_state,
               COUNT(*) AS wells
        FROM {dv_well}
        WHERE {q('province_state')}     IS NOT NULL
          AND {q('surface_latitude')}   IS NOT NULL
          AND {q('surface_longitude')}  IS NOT NULL
        GROUP BY {q('province_state')}
        ORDER BY COUNT(*) DESC
    """
    return fetch_all(_engine, sql)


@st.cache_data(ttl=300, show_spinner=False)
def _counties(_engine, _dialect, dv_well, state):
    q = _dialect.quote
    sql = f"""
        SELECT {q('county')} AS county,
               COUNT(*) AS wells
        FROM {dv_well}
        WHERE {q('province_state')}   = :state
          AND {q('county')}           IS NOT NULL
          AND {q('surface_latitude')} IS NOT NULL
        GROUP BY {q('county')}
        ORDER BY COUNT(*) DESC
    """
    return fetch_all(_engine, sql, {"state": state})


@st.cache_data(ttl=300, show_spinner=False)
def _operators(_engine, _dialect, dv_well, state, counties_tuple):
    if not counties_tuple:
        return []
    q = _dialect.quote
    placeholders = ",".join(f":c{i}" for i in range(len(counties_tuple)))
    pfx = _dialect.limit_prefix(LOOKUP_LIMIT)
    sfx = _dialect.limit_suffix(LOOKUP_LIMIT)
    sql = f"""
        SELECT {pfx} {q('operator_name')} AS operator_name,
               COUNT(*) AS wells
        FROM {dv_well}
        WHERE {q('province_state')}    = :state
          AND {q('county')}           IN ({placeholders})
          AND {q('operator_name')}    IS NOT NULL
        GROUP BY {q('operator_name')}
        ORDER BY COUNT(*) DESC
        {sfx}
    """
    params = {"state": state}
    params.update({f"c{i}": c for i, c in enumerate(counties_tuple)})
    return fetch_all(_engine, sql, params)


@st.cache_data(ttl=300, show_spinner=False)
def _fields(_engine, _dialect, dv_well, state,
            counties_tuple, operators_tuple):
    if not counties_tuple:
        return []
    q = _dialect.quote
    placeholders_c = ",".join(f":c{i}"
                              for i in range(len(counties_tuple)))
    where_op = ""
    params = {"state": state}
    params.update({f"c{i}": c for i, c in enumerate(counties_tuple)})
    if operators_tuple:
        placeholders_o = ",".join(f":o{i}"
                                  for i in range(len(operators_tuple)))
        where_op = f"AND {q('operator_name')} IN ({placeholders_o})"
        params.update({f"o{i}": o for i, o in enumerate(operators_tuple)})
    pfx = _dialect.limit_prefix(LOOKUP_LIMIT)
    sfx = _dialect.limit_suffix(LOOKUP_LIMIT)
    sql = f"""
        SELECT {pfx} {q('field_name')} AS field_name,
               COUNT(*) AS wells
        FROM {dv_well}
        WHERE {q('province_state')} = :state
          AND {q('county')}        IN ({placeholders_c})
          {where_op}
          AND {q('field_name')}    IS NOT NULL
        GROUP BY {q('field_name')}
        ORDER BY COUNT(*) DESC
        {sfx}
    """
    return fetch_all(_engine, sql, params)


def _load_wells(engine, dialect, dv_well, state,
                counties, operators, fields):
    q = dialect.quote
    where = [
        f"{q('province_state')}    = :state",
        f"{q('surface_latitude')}  IS NOT NULL",
        f"{q('surface_longitude')} IS NOT NULL",
    ]
    params = {"state": state}
    if counties:
        ph = ",".join(f":c{i}" for i in range(len(counties)))
        where.append(f"{q('county')} IN ({ph})")
        params.update({f"c{i}": c for i, c in enumerate(counties)})
    if operators:
        ph = ",".join(f":o{i}" for i in range(len(operators)))
        where.append(f"{q('operator_name')} IN ({ph})")
        params.update({f"o{i}": o for i, o in enumerate(operators)})
    if fields:
        ph = ",".join(f":f{i}" for i in range(len(fields)))
        where.append(f"{q('field_name')} IN ({ph})")
        params.update({f"f{i}": f for i, f in enumerate(fields)})
    where_sql = " AND ".join(where)

    pfx = dialect.limit_prefix(WELLS_LIMIT)
    sfx = dialect.limit_suffix(WELLS_LIMIT)
    cols = ", ".join(q(c) for c in [
        "uwi", "well_name", "operator_name", "field_name",
        "surface_latitude", "surface_longitude",
        "county", "province_state",
        "well_status", "well_type", "source",
        "spud_date", "completion_date", "final_td", "area",
        "bottom_hole_latitude", "bottom_hole_longitude",
    ])

    sql = f"""
        SELECT {pfx} {cols}
        FROM {dv_well}
        WHERE {where_sql}
        {sfx}
    """

    t0 = time.time()
    rows = fetch_all(engine, sql, params)
    elapsed = time.time() - t0

    wells = []
    for d in rows:
        d_lower = {k.lower(): v for k, v in d.items()}
        try:
            d_lower["lat"] = float(d_lower["surface_latitude"])
            d_lower["lon"] = float(d_lower["surface_longitude"])
        except (TypeError, ValueError):
            continue
        wells.append(d_lower)
    return wells, elapsed


@st.cache_data(ttl=60, show_spinner=False)
def _list_spatial_layers(_engine, _dialect):
    q = _dialect.quote
    spat = _dialect.qualified(DV_SCHEMA, DV_SPATIAL)
    sql = f"""
        SELECT {q('layer_id')}             AS layer_id,
               {q('layer_name')}           AS layer_name,
               {q('layer_category')}       AS layer_category,
               {q('layer_type')}           AS layer_type,
               {q('feature_count')}        AS feature_count,
               {q('source_type')}          AS source_type,
               {q('bbox_min_lat')}         AS bbox_min_lat,
               {q('bbox_max_lat')}         AS bbox_max_lat,
               {q('bbox_min_lon')}         AS bbox_min_lon,
               {q('bbox_max_lon')}         AS bbox_max_lon,
               {q('style_color')}          AS style_color,
               {q('style_fill_color')}     AS style_fill_color,
               {q('style_opacity')}        AS style_opacity,
               {q('style_fill_opacity')}   AS style_fill_opacity
        FROM {spat}
        WHERE {q('active_ind')} = 'Y'
        ORDER BY {q('layer_category')}, {q('layer_name')}
    """
    try:
        rows = fetch_all(_engine, sql)
    except Exception:
        return []
    return [{k.lower(): v for k, v in d.items()} for d in rows]


@st.cache_data(ttl=60, show_spinner=False)
def _load_spatial_layer_geom(_engine, _dialect, layer_id):
    if not HAS_SHAPELY:
        return None
    q = _dialect.quote
    spat = _dialect.qualified(DV_SCHEMA, DV_SPATIAL)
    sql = f"""
        SELECT {q('geometry_wkt')} AS geometry_wkt
        FROM {spat}
        WHERE {q('layer_id')} = :lid
    """
    try:
        rows = fetch_all(_engine, sql, {"lid": layer_id})
        if not rows:
            return None
        d = {k.lower(): v for k, v in rows[0].items()}
        wkt_text = d.get("geometry_wkt")
        if not wkt_text:
            return None
        return shapely_wkt.loads(wkt_text)
    except Exception as e:
        st.warning(f"Could not parse layer geometry: {e}")
        return None


def _render_aoi_picker(engine, dialect):
    layers = _list_spatial_layers(engine, dialect)

    if not layers:
        st.caption(
            "No registered spatial layers found in "
            f"`{DV_SCHEMA}.{DV_SPATIAL}`. "
            "Register layers via the Spatial Layers page to enable "
            "AOI selection."
        )
        return None

    options = ["(none — show all loaded wells)"]
    label_to_layer = {}
    for lay in layers:
        cat = lay.get("layer_category") or "Uncategorized"
        name = lay.get("layer_name") or lay.get("layer_id")
        n = lay.get("feature_count")
        suffix = f"  ({n:,} features)" if n else ""
        label = f"{cat} — {name}{suffix}"
        options.append(label)
        label_to_layer[label] = lay

    choice = st.selectbox(
        "Spatial layer (AOI)",
        options=options,
        key="fs_aoi_picker",
        label_visibility="collapsed",

    )

    if choice == options[0]:
        return None

    layer = label_to_layer.get(choice)
    if not layer:
        return None

    layer["_shapely_geom"] = _load_spatial_layer_geom(
        engine, dialect, layer["layer_id"]
    )
    return layer


def _to_year(d):
    if d is None:
        return None
    try:
        s = str(d)[:4]
        if s.startswith("99") or s.startswith("18"):
            return None
        y = int(s)
        return y if 1900 < y <= 2030 else None
    except (ValueError, TypeError):
        return None


def _to_float(v):
    if v is None:
        return None
    try:
        f = float(v)
        return None if f != f else f
    except (ValueError, TypeError):
        return None


def _render_sidebar_filters(all_wells):
    state = {
        "td": None, "sy": None, "cy": None,
        "status": None, "type": None,
        "statuses": [], "types": [],
    }

    with st.sidebar:
        st.markdown("### 🔧 Attribute filters")
        if not all_wells:
            st.caption("Load wells from the main panel first.")
            return state

        st.caption(f"Applied to **{len(all_wells):,} loaded wells**. "
                   "Strict-NULL: missing values fail numeric filters.")

        tds = [t for t in (_to_float(w.get("final_td"))
                           for w in all_wells)
               if t is not None and t > 0]
        spuds = [y for y in (_to_year(w.get("spud_date"))
                             for w in all_wells)
                 if y is not None]
        comps = [y for y in (_to_year(w.get("completion_date"))
                             for w in all_wells)
                 if y is not None]
        statuses = sorted(set(
            (w.get("well_status") or "").strip() or "(unknown)"
            for w in all_wells))
        types = sorted(set(
            (w.get("well_type") or "").strip() or "(unknown)"
            for w in all_wells))
        state["statuses"] = statuses
        state["types"] = types

        st.markdown("---")
        st.markdown("**Final TD (feet)**")
        if tds:
            st.caption(f"{len(tds):,} of {len(all_wells):,} have TD")
            try:
                td_hist = (pd.Series(tds)
                           .clip(upper=min(30000, int(max(tds))))
                           .value_counts(bins=20, sort=False))
                td_hist.index = [int(iv.right) for iv in td_hist.index]
                st.bar_chart(td_hist, height=120)
            except Exception:
                pass
            td_op = st.radio(
                "Compare",
                options=["No filter", ">=", "<=", "between"],
                horizontal=True, key="_fs_td_op",
                label_visibility="collapsed",
            )
            if td_op == ">=":
                v = st.number_input("Min TD", value=float(min(tds)),
                                    step=500.0, key="_fs_td_gte")
                state["td"] = ("gte", v, None)
            elif td_op == "<=":
                v = st.number_input("Max TD", value=float(max(tds)),
                                    step=500.0, key="_fs_td_lte")
                state["td"] = ("lte", None, v)
            elif td_op == "between":
                lo, hi = st.slider("TD range",
                                   min_value=float(min(tds)),
                                   max_value=float(max(tds)),
                                   value=(float(min(tds)),
                                          float(max(tds))),
                                   step=500.0, key="_fs_td_btw")
                state["td"] = ("btw", lo, hi)
        else:
            st.caption("No TD values in loaded wells")

        st.markdown("---")
        st.markdown("**Spud year**")
        if spuds:
            st.caption(f"{len(spuds):,} of {len(all_wells):,} "
                       "have spud date")
            try:
                st.bar_chart(pd.Series(spuds).value_counts().sort_index(),
                             height=120)
            except Exception:
                pass
            sy_op = st.radio(
                "Spud",
                options=["No filter", "After", "Before", "Between"],
                horizontal=True, key="_fs_sy_op",
                label_visibility="collapsed",
            )
            if sy_op == "After":
                v = st.number_input("Spud after", min_value=1800,
                                    max_value=2030, value=2000,
                                    step=1, key="_fs_sy_after")
                state["sy"] = ("after", v, None)
            elif sy_op == "Before":
                v = st.number_input("Spud before", min_value=1800,
                                    max_value=2030, value=2000,
                                    step=1, key="_fs_sy_before")
                state["sy"] = ("before", None, v)
            elif sy_op == "Between":
                lo, hi = st.slider("Spud year range",
                                   min_value=int(min(spuds)),
                                   max_value=int(max(spuds)),
                                   value=(int(min(spuds)),
                                          int(max(spuds))),
                                   step=1, key="_fs_sy_btw")
                state["sy"] = ("btw", lo, hi)
        else:
            st.caption("No spud dates in loaded wells")

        st.markdown("---")
        st.markdown("**Completion year**")
        if comps:
            st.caption(f"{len(comps):,} of {len(all_wells):,} "
                       "have completion date")
            try:
                st.bar_chart(pd.Series(comps).value_counts().sort_index(),
                             height=120)
            except Exception:
                pass
            cy_op = st.radio(
                "Comp",
                options=["No filter", "After", "Before", "Between"],
                horizontal=True, key="_fs_cy_op",
                label_visibility="collapsed",
            )
            if cy_op == "After":
                v = st.number_input("Comp after", min_value=1800,
                                    max_value=2030, value=2000,
                                    step=1, key="_fs_cy_after")
                state["cy"] = ("after", v, None)
            elif cy_op == "Before":
                v = st.number_input("Comp before", min_value=1800,
                                    max_value=2030, value=2000,
                                    step=1, key="_fs_cy_before")
                state["cy"] = ("before", None, v)
            elif cy_op == "Between":
                lo, hi = st.slider("Comp year range",
                                   min_value=int(min(comps)),
                                   max_value=int(max(comps)),
                                   value=(int(min(comps)),
                                          int(max(comps))),
                                   step=1, key="_fs_cy_btw")
                state["cy"] = ("btw", lo, hi)
        else:
            st.caption("No completion dates in loaded wells")

        st.markdown("---")
        st.markdown("**Well status**")
        state["status"] = st.multiselect(
            "Status", options=statuses, default=statuses,
            key="_fs_status", label_visibility="collapsed",
        )
        st.markdown("---")
        st.markdown("**Well type**")
        state["type"] = st.multiselect(
            "Type", options=types, default=types,
            key="_fs_type", label_visibility="collapsed",
        )

        st.markdown("---")
        if st.button("↺ Reset all filters", use_container_width=True,
                     key="_fs_reset"):
            for k in ("_fs_td_op", "_fs_sy_op", "_fs_cy_op",
                      "_fs_status", "_fs_type",
                      "_fs_td_gte", "_fs_td_lte", "_fs_td_btw",
                      "_fs_sy_after", "_fs_sy_before", "_fs_sy_btw",
                      "_fs_cy_after", "_fs_cy_before", "_fs_cy_btw"):
                st.session_state.pop(k, None)
            st.rerun()

    return state


def _passes_filters(w, s):
    if s["td"]:
        op, lo, hi = s["td"]
        td = _to_float(w.get("final_td"))
        if td is None or td <= 0:
            return False
        if op == "gte" and td < lo: return False
        if op == "lte" and td > hi: return False
        if op == "btw" and (td < lo or td > hi): return False
    if s["sy"]:
        op, lo, hi = s["sy"]
        y = _to_year(w.get("spud_date"))
        if y is None: return False
        if op == "after" and y < lo: return False
        if op == "before" and y > hi: return False
        if op == "btw" and (y < lo or y > hi): return False
    if s["cy"]:
        op, lo, hi = s["cy"]
        y = _to_year(w.get("completion_date"))
        if y is None: return False
        if op == "after" and y < lo: return False
        if op == "before" and y > hi: return False
        if op == "btw" and (y < lo or y > hi): return False
    if (s["status"] is not None and s["statuses"]
            and s["status"] != s["statuses"]):
        val = (w.get("well_status") or "").strip() or "(unknown)"
        if val not in s["status"]:
            return False
    if (s["type"] is not None and s["types"]
            and s["type"] != s["types"]):
        val = (w.get("well_type") or "").strip() or "(unknown)"
        if val not in s["type"]:
            return False
    return True


def _color_for(status):
    if not status:
        return DEFAULT_COLOR
    s = status.lower()
    for key, rgb in STATUS_COLORS.items():
        if key in s:
            return rgb
    return DEFAULT_COLOR


def _hex_to_rgb(h):
    if not h or not isinstance(h, str):
        return [128, 128, 128]
    h = h.lstrip("#")
    if len(h) != 6:
        return [128, 128, 128]
    try:
        return [int(h[i:i+2], 16) for i in (0, 2, 4)]
    except ValueError:
        return [128, 128, 128]


def _render_map(loaded_wells, selected_wells, aoi_layer):
    if not loaded_wells:
        st.info("Pick a state and one or more counties above, then "
                f"click **Load Wells** to fetch wells from "
                f"`{DV_SCHEMA}.{DV_WELL}` and display them here.")
        return

    target_wells = selected_wells or loaded_wells
    lats = [w["lat"] for w in target_wells]
    lons = [w["lon"] for w in target_wells]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)
    span = max(max(lats) - min(lats), max(lons) - min(lons), 0.01)
    if span < 0.5:    zoom = 9
    elif span < 1:    zoom = 8
    elif span < 3:    zoom = 7
    elif span < 6:    zoom = 6
    elif span < 12:   zoom = 5
    else:             zoom = 4

    view_state = pdk.ViewState(
        latitude=center_lat, longitude=center_lon,
        zoom=zoom, pitch=0,
    )

    selected_uwis = {w.get("uwi") for w in selected_wells}

    bg_rows = []
    sel_rows = []
    for w in loaded_wells:
        row = {
            "lat": w["lat"],
            "lon": w["lon"],
            "uwi": w.get("uwi") or "",
            "name": w.get("well_name") or "",
            "operator": w.get("operator_name") or "",
            "status": w.get("well_status") or "",
            "type": w.get("well_type") or "",
            "county": w.get("county") or "",
            "state": w.get("province_state") or "",
        }
        if w.get("uwi") in selected_uwis:
            row["color"] = _color_for(w.get("well_status"))
            sel_rows.append(row)
        else:
            row["color"] = [180, 180, 180, 100]
            bg_rows.append(row)

    layers = []

    if bg_rows:
        layers.append(pdk.Layer(
            "ScatterplotLayer",
            data=pd.DataFrame(bg_rows),
            get_position=["lon", "lat"],
            get_fill_color="color",
            get_radius=50,
            radius_min_pixels=2,
            radius_max_pixels=6,
            pickable=False,
        ))

    if sel_rows:
        layers.append(pdk.Layer(
            "ScatterplotLayer",
            data=pd.DataFrame(sel_rows),
            get_position=["lon", "lat"],
            get_fill_color="color",
            get_radius=80,
            radius_min_pixels=3,
            radius_max_pixels=8,
            pickable=True,
        ))

    if aoi_layer and aoi_layer.get("_shapely_geom") is not None:
        try:
            aoi_geojson = aoi_layer["_shapely_geom"].__geo_interface__
        except Exception:
            aoi_geojson = None
        if aoi_geojson:
            line_color = _hex_to_rgb(
                aoi_layer.get("style_color") or "#0a8a96")
            fill_color = _hex_to_rgb(
                aoi_layer.get("style_fill_color") or "#0a8a96")
            fill_opacity = float(
                aoi_layer.get("style_fill_opacity") or 0.1) * 255
            line_color_with_alpha = line_color + [200]
            fill_color_with_alpha = fill_color + [int(fill_opacity)]

            layers.append(pdk.Layer(
                "GeoJsonLayer",
                data={"type": "Feature", "geometry": aoi_geojson},
                stroked=True,
                filled=True,
                get_line_color=line_color_with_alpha,
                get_fill_color=fill_color_with_alpha,
                line_width_min_pixels=2,
                pickable=False,
            ))

    tooltip = {
        "html": (
            "<b>{name}</b><br>"
            "UWI: {uwi}<br>"
            "Operator: {operator}<br>"
            "Status: {status}<br>"
            "Type: {type}<br>"
            "County: {county}, {state}"
        ),
        "style": {"backgroundColor": "#1a1a1a", "color": "white"},
    }

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        map_provider="carto",
        map_style="light",
        tooltip=tooltip,
    )

    st.pydeck_chart(deck, height=600, use_container_width=True,
                    key="fs_pydeck")

    cap_parts = []
    if sel_rows:
        cap_parts.append(f"**{len(sel_rows):,}** selected (colored by status)")
    if bg_rows:
        cap_parts.append(f"**{len(bg_rows):,}** loaded-but-not-selected (gray)")
    if aoi_layer:
        cap_parts.append(f"AOI: **{aoi_layer.get('layer_name')}**")
    if cap_parts:
        st.caption(" · ".join(cap_parts))


def _render_results_table(selected_wells):
    if not selected_wells:
        st.caption("No wells in the current selection. Load wells "
                   "and (optionally) pick an AOI layer to narrow.")
        return

    grid_df = pd.DataFrame([{
        "Select": False,
        "UWI": w.get("uwi", ""),
        "Name": w.get("well_name", "") or "",
        "Operator": w.get("operator_name", "") or "",
        "County": w.get("county", "") or "",
        "State": w.get("province_state", "") or "",
        "Status": w.get("well_status", "") or "",
        "Type": w.get("well_type", "") or "",
        "Field": w.get("field_name", "") or "",
        "Spud Date": str(w.get("spud_date") or "")[:10],
        "Completion Date": str(w.get("completion_date") or "")[:10],
        "Final TD": w.get("final_td", "") or "",
        "Lat": w.get("lat", ""),
        "Lon": w.get("lon", ""),
        "Source": w.get("source", "") or "",
    } for w in selected_wells])

    sa1, sa2, _ = st.columns([1, 1, 4])
    if sa1.button("☑ Select All", use_container_width=True, key="fs_sa"):
        grid_df["Select"] = True
    if sa2.button("☐ Clear All", use_container_width=True, key="fs_ca"):
        grid_df["Select"] = False

    edited = st.data_editor(
        grid_df,
        hide_index=True,
        column_config={
            "Select": st.column_config.CheckboxColumn(width="small"),
        },
        use_container_width=True,
        height=400,
        key="fs_grid",
    )

    n_checked = int(edited["Select"].sum())
    st.caption(f"{n_checked:,} of {len(edited):,} wells checked.")

    exp1, exp2, _ = st.columns([1, 1, 4])

    def _csv_bytes():
        df = (edited[edited["Select"]].drop(columns=["Select"])
              if n_checked else edited.drop(columns=["Select"]))
        return df.to_csv(index=False).encode("utf-8")

    def _xlsx_bytes():
        df = (edited[edited["Select"]].drop(columns=["Select"])
              if n_checked else edited.drop(columns=["Select"]))
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="wells", index=False)
        return buf.getvalue()

    exp1.download_button(
        "📄 CSV", data=_csv_bytes,
        file_name=f"dv_wells_{int(time.time())}.csv",
        mime="text/csv", use_container_width=True,
        key="fs_dl_csv",
    )
    exp2.download_button(
        "📊 Excel", data=_xlsx_bytes,
        file_name=f"dv_wells_{int(time.time())}.xlsx",
        mime=("application/vnd.openxmlformats-"
              "officedocument.spreadsheetml.sheet"),
        use_container_width=True,
        key="fs_dl_xlsx",
    )
