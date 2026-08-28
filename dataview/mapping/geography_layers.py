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
                   ["seis_set_type", "epsg_code", "file_path"], "#0E6E6E",
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


def add_geography_layer(m, engine, key: str, show: bool = True,
                        show_names=None) -> int:
    """Add one dv_*.geog feature layer as a toggleable FeatureGroup on `m`.

    Returns the number of features added (0 = nothing to draw / table absent
    / query failed — printed, never raised).

    show_names: for the per-survey seismic split only, the set of survey
    names that should start VISIBLE. This is how the second-screen page
    drives the map: it writes the chosen surveys to the shared prefs file
    and the map applies them here on its next render. None means "the
    `show` flag decides", which is every other caller and the old
    behaviour exactly — an empty SET is different from None and means the
    page asked for none of them."""
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

    tip_fields = ["name"] + extras
    tip_aliases = ["Name"] + [c.replace("_", " ").title() for c in extras]

    # ONE GROUP PER SURVEY, FOR SEISMIC ONLY. Leaflet's layer control gives a
    # checkbox per FeatureGroup, so splitting here is what makes individual
    # surveys switchable -- and it costs no rerun, because the toggling happens
    # entirely in the browser. A Streamlit multiselect would rebuild and
    # re-serialise the whole map to hide one rectangle.
    #
    # SEISMIC ONLY, deliberately. dv_seis_set holds a handful of surveys; the
    # other four keys here are fields, leases, boundaries and pipelines, where
    # the same split would put hundreds of checkboxes in the control and make
    # it useless for everything including seismic.
    # ...AND FOR ANY LAYER SMALL ENOUGH TO NAME. The rule above is about
    # COUNT, not about seismic: a checkbox per feature is exactly what someone
    # wants for the six pools they drew, and exactly what ruins the control
    # for four hundred pipelines. So split when the layer is small enough to
    # read, whatever it is. Hand-drawn boundaries land here; a loaded
    # shapefile of leases stays one group, as before.
    _SPLIT_MAX = 12
    if key == "seismic" or len(gj["features"]) <= _SPLIT_MAX:
        groups, order = {}, []
        for _ft in gj["features"]:
            _nm = str((_ft.get("properties") or {}).get("name") or "(unnamed)")
            if _nm not in groups:
                groups[_nm] = []
                order.append(_nm)
            groups[_nm].append(_ft)
        # The label keeps the layer's icon so the survey rows read as one
        # family in an alphabetical control rather than scattering.
        _icon = label.split()[0]
        parts = [("%s %s (%d)" % (_icon, _nm[:44], len(groups[_nm])),
                  groups[_nm], _nm) for _nm in order]
    else:
        parts = [("%s (%s)" % (label, format(len(gj["features"]), ",")),
                  gj["features"], None)]

    for _gname, _feats, _gkey in parts:
        # A NAME THE PAGE CAN ADDRESS. The group LABEL carries an icon and
        # a count, so it changes when the data changes; the survey name
        # does not, which is what makes a stored selection survive a
        # reload that adds a line.
        _vis = show if show_names is None or _gkey is None else (
            _gkey in show_names)
        fg = folium.FeatureGroup(name=_gname, show=_vis)
        folium.GeoJson(
            {"type": "FeatureCollection", "features": _feats},
            style_function=_style,
            highlight_function=lambda _f, _c=color: {"weight": 4, "color": _c},
            tooltip=folium.GeoJsonTooltip(fields=tip_fields,
                                          aliases=tip_aliases, sticky=True),
            # A TOOLTIP VANISHES WHEN THE POINTER DOES, which is fine for a
            # name and useless for a file path you want to read or copy. The
            # popup holds still. Same fields, one template, so it costs the
            # aliases and nothing per feature.
            popup=folium.GeoJsonPopup(fields=tip_fields, aliases=tip_aliases,
                                      labels=True, max_width=340),
        ).add_to(fg)
        fg.add_to(m)
    return len(gj["features"])


def points_layer(m, points, name, *, color="#222", fill="#555",
                 radius=3, show=True, opacity=0.9,
                 popup_fields=None, popup_aliases=None, extra=None):
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
    # A GeoJsonPopup emits ONE template plus the per-feature property values,
    # where a folium.Popup per point would emit a block of HTML each. That is
    # the same reason this is one layer and not N markers, and it is why 50,000
    # popups cost kilobytes of template rather than megabytes of markup.
    feats = []
    extra = list(extra or [])
    for row in points:
        la, lo, label = row[0], row[1], row[2]
        try:
            la, lo = float(la), float(lo)
        except (TypeError, ValueError):
            continue
        props = {"nm": str(label or "")}
        for i, key in enumerate(extra):
            v = row[3 + i] if len(row) > 3 + i else None
            props[key] = "" if v is None else str(v)
        feats.append({
            "type": "Feature",
            "properties": props,
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
        popup=(folium.GeoJsonPopup(fields=list(popup_fields),
                                   aliases=list(popup_aliases or popup_fields),
                                   labels=True, max_width=320)
               if popup_fields else None),
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

# What a reference-well popup answers. Declared once so the two queries below
# cannot drift apart, and ordered to match the tuple unpack at the call site --
# a mismatch there is silent, it just puts the operator in the county field.
_REF_COLS = ("well_name, surface_latitude, surface_longitude, uwi14, "
             "operator_name, county, province_state, std_well_type, "
             "std_well_status, total_depth, spud_date")
_REF_COLS_LIGHT = "well_name, surface_latitude, surface_longitude"

# ABOVE THIS, POPUPS ARE PAID FOR AND NOT USED. A popup costs ~320 bytes of
# feature properties, so 11,291 wells go 2.5 -> 6.1 MB and 48,218 go 10.4 ->
# 25.6 MB, on every rerun -- to attach detail to points nobody can click
# through at that density. Below it, "which well is this" is exactly the
# question being asked and the popup is the answer.
POPUP_MAX = 20000


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
                f"SELECT TOP {lim + 1} {_REF_COLS_LIGHT} "
                f"FROM {REFERENCE_MASTER} WHERE {clause}"),
                params).fetchall()
            if len(rows) > lim:
                est = con.execute(text(
                    "SELECT SUM(p.rows) FROM WELL_REF.sys.partitions p "
                    "JOIN WELL_REF.sys.objects o ON o.object_id = p.object_id "
                    "WHERE o.name = 'well_master_gold' AND p.index_id IN (0,1)"
                )).scalar() or 0
                # k FROM THE SCOPE, NOT THE MASTER -- and the first sample
                # is what measures the scope. Deriving k from the master's
                # 3.9M rows thins every view by the same 79x, so a bbox
                # holding ~200k wells drew 2,613 of them: correct in shape,
                # 19x sparser than the cap allows, and indistinguishable to
                # the eye from "there are hardly any wells here".
                #
                # Counting the scope directly is the expensive thing this
                # code already avoids (5.90s over a 2M-row bbox). But a
                # 1-in-k sample IS an estimator: n * k approximates the
                # population, so one cheap sample tells us what k should have
                # been, and we re-ask only if the answer differs.
                k = max(2, int(est // max(lim, 1)) + 1)
                sampled = True
                rows = con.execute(text(
                    f"SELECT TOP {lim} {_REF_COLS_LIGHT} "
                    f"FROM {REFERENCE_MASTER} "
                    f"WHERE {clause} AND ABS(CHECKSUM(uwi14)) % {k} = 0"),
                    params).fetchall()
                if rows:
                    scope_est = len(rows) * k
                    k2 = max(2, int(scope_est // max(lim, 1)) + 1)
                    # Only worth a second pass if it changes the picture.
                    if k2 < k // 2:
                        q2 = (f"SELECT TOP {lim} {_REF_COLS_LIGHT} "
                              f"FROM {REFERENCE_MASTER} WHERE {clause} "
                              f"AND ABS(CHECKSUM(uwi14)) % {k2} = 0")
                        rows = con.execute(text(q2), params).fetchall()
    except Exception as exc:
        print(f"[geography_layers] reference wells query failed: {exc}")
        return 0, 0
    if not rows:
        return 0, 0

    # A SECOND QUERY, ON PURPOSE. The probe fetches three columns so the
    # common "too many to popup" case never pays for eleven; only when the
    # set is small enough to be clicked does it go back for the detail. That
    # re-read is bounded by POPUP_MAX and measured in tenths of a second,
    # against the ~10s the wide sample scan costs.
    detail = False
    if not sampled and len(rows) <= POPUP_MAX:
        try:
            with engine.connect() as con:
                rows = con.execute(text(
                    f"SELECT TOP {lim} {_REF_COLS} "
                    f"FROM {REFERENCE_MASTER} WHERE {clause}"),
                    params).fetchall()
            detail = True
        except Exception as exc:
            print(f"[geography_layers] reference detail query failed: {exc}")

    in_scope = None if sampled else len(rows)
    label = (f"🔵 Reference wells ({len(rows):,}"
             + (" sample)" if sampled else ")"))
    # The popup is what makes a reference well answerable rather than merely
    # visible -- "which well is this" is the whole reason to draw points at all
    # instead of a density hex. Every field is one the master states; a column
    # it leaves NULL shows blank rather than a guess.
    _f = ["uwi", "operator", "county", "state", "type", "status", "td", "spud"]
    if detail:
        n_drawn = points_layer(
            m, ((la, lo, nm, uwi, op, cty, prov, ty, stat, td, spud)
                for nm, la, lo, uwi, op, cty, prov, ty, stat, td, spud in rows),
            name=label, color="#1d4ed8", fill="#60a5fa", radius=2, show=show,
            opacity=0.7, extra=_f, popup_fields=["nm"] + _f,
            popup_aliases=["Well", "UWI", "Operator", "County", "State",
                           "Type", "Status", "TD", "Spud"])
    else:
        n_drawn = points_layer(m, ((la, lo, nm) for nm, la, lo in rows),
                               name=label, color="#1d4ed8", fill="#60a5fa",
                               radius=2, show=show, opacity=0.7)
    # in_scope is None when capped: "we do not know, and finding out costs
    # more than the answer is worth". Never 0, which would read as "none here".
    return n_drawn, in_scope


def add_all_geography_layers(m, engine) -> int:
    """Back-compat convenience: every configured layer at once."""
    return sum(add_geography_layer(m, engine, k) for k in _LAYER_KEYS)


def _qry_horizon_contours(engine):
    """[{horizon_id, name, colour, value, wkt}] -- every horizon contour.

    Returns [] and says why on any failure rather than raising: a database
    that has never loaded a horizon must not take the map down with it, and a
    swallowed diagnostic is what makes the next failure undiagnosable.
    """
    try:
        with engine.connect() as con:
            if con.execute(text(
                    "SELECT OBJECT_ID('dataview.dv_seis_horizon_contour','U')"
            )).scalar() is None:
                return []
            rows = con.execute(text("""
                SELECT h.horizon_id, h.horizon_name, h.display_colour,
                       h.seq_no, c.contour_value, c.geog.STAsText() AS wkt
                  FROM dataview.dv_seis_horizon_contour c
                  JOIN dataview.dv_seis_horizon h
                    ON h.horizon_id = c.horizon_id
                 WHERE c.geog IS NOT NULL
                   AND ISNULL(c.active_ind,'Y') = 'Y'
                   AND ISNULL(h.active_ind,'Y') = 'Y'
                 ORDER BY h.seq_no, c.contour_value
            """)).fetchall()
    except Exception as exc:
        print(f"[horizons] contour query failed: {exc}")
        return []
    return [{"horizon_id": r[0], "name": r[1] or r[0],
             "colour": r[2] or "#E4572E", "seq": r[3] or 0,
             "value": float(r[4]), "wkt": r[5]} for r in rows]


def _linestring_coords(wkt):
    """[[lon, lat], ...] from a LINESTRING WKT, GeoJSON order."""
    if not wkt or "(" not in wkt:
        return []
    body = wkt[wkt.find("(") + 1: wkt.rfind(")")]
    out = []
    for pair in body.split(","):
        bits = pair.split()
        if len(bits) < 2:
            continue
        try:
            out.append([float(bits[0]), float(bits[1])])
        except ValueError:
            continue
    return out


def add_horizon_contours(m, engine, show=False):
    """Time-structure contours, one toggleable group per horizon.

    ONE GeoJson PER HORIZON, not one per contour. folium serialises each
    object it is given separately, which is what made 14,727 hexagons take 28
    seconds before the H3 layer was rewritten the same way. There are only 62
    contours here, but the pattern is the one that survives someone loading a
    real interpretation with thousands.

    Returns the number of horizons drawn.
    """
    rows = _qry_horizon_contours(engine)
    if not rows:
        return 0

    by_h = {}
    for r in rows:
        by_h.setdefault(r["horizon_id"], []).append(r)

    drawn = 0
    for hid in sorted(by_h, key=lambda k: by_h[k][0]["seq"]):
        group = by_h[hid]
        colour = group[0]["colour"]
        feats = []
        for r in group:
            coords = _linestring_coords(r["wkt"])
            if len(coords) < 2:
                continue
            feats.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {"hz": r["name"],
                               # Pre-formatted: a GeoJsonTooltip prints the
                               # property as-is, and "289.99999999" is what a
                               # raw contour level looks like.
                               "t": f"{r['value']:,.0f} ms"},
            })
        if not feats:
            continue
        fg = folium.FeatureGroup(
            name=f"〰️ {group[0]['name']} ({len(feats)})", show=show)
        folium.GeoJson(
            {"type": "FeatureCollection", "features": feats},
            style_function=lambda _f, _c=colour: {
                "color": _c, "weight": 1.6, "opacity": 0.9, "fillOpacity": 0},
            tooltip=folium.GeoJsonTooltip(fields=["hz", "t"],
                                          aliases=["Horizon", "TWT"],
                                          sticky=True),
        ).add_to(fg)
        fg.add_to(m)
        drawn += 1
    return drawn


# ── Well symbols ─────────────────────────────────────────────────────────────
# A COLOURED DOT IS NOT A WELL SYMBOL. The industry set is a SHAPE vocabulary
# that predates colour printing and is still what a geologist reads first: a
# solid circle is oil, a circle with rays is gas, an open circle with a cross
# is a dry hole, an X through it is plugged. Colour is a second channel on top,
# not a replacement -- rendered in greyscale, or by someone who cannot separate
# red from green, the shapes still say what each well is.
#
# So these are inline SVG rather than CircleMarkers. That costs more per well
# than a circle, which is why this layer is capped and the plain point layer
# still exists for the 50,000-well reference set.
_SYM_OIL = ('<circle cx="9" cy="9" r="5.5" fill="{c}" stroke="#111" '
            'stroke-width="1.1"/>')
_SYM_OPEN = ('<circle cx="9" cy="9" r="5.5" fill="none" stroke="{c}" '
             'stroke-width="1.6"/>')

# status/type -> (label, colour, svg body). Ordered most specific first: a
# well that is INJECTING is an injector whatever fluid it once produced.
WELL_SYMBOLS = [
    ("INJECTING", None, "Injector",   "#2563eb",
     _SYM_OPEN + '<path d="M9 3.2 v11.6 M5.6 11 L9 14.8 L12.4 11" '
                 'fill="none" stroke="{c}" stroke-width="1.5"/>'),
    ("SHUT-IN",   None, "Shut in",    "#d97706",
     _SYM_OPEN + '<path d="M3.5 9 h11" stroke="{c}" stroke-width="1.8"/>'),
    (None, "DRY",       "Dry hole",   "#111827",
     _SYM_OPEN + '<path d="M9 2.6 v12.8 M2.6 9 h12.8" stroke="{c}" '
                 'stroke-width="1.5"/>'),
    ("P&A", None,       "Plugged",    "#6b7280",
     _SYM_OPEN + '<path d="M5 5 L13 13 M13 5 L5 13" stroke="{c}" '
                 'stroke-width="1.6"/>'),
    (None, "GAS",       "Gas",        "#dc2626",
     _SYM_OIL + '<path d="M9 1.4 v2.6 M9 14 v2.6 M1.4 9 h2.6 M14 9 h2.6" '
                'stroke="{c}" stroke-width="1.5"/>'),
    (None, "OIL & GAS", "Oil and gas", "#16a34a",
     '<circle cx="9" cy="9" r="5.5" fill="#16a34a" stroke="#111" '
     'stroke-width="1.1"/><path d="M9 3.5 A5.5 5.5 0 0 1 9 14.5 Z" '
     'fill="#dc2626"/>'),
    (None, "OIL",       "Oil",        "#16a34a", _SYM_OIL),
]
_SYM_FALLBACK = ("Other / unknown", "#7c3aed", _SYM_OPEN)


def _classify_well(status, wtype):
    """(label, colour, svg) for one well. Never returns None."""
    s = (status or "").strip().upper()
    t = (wtype or "").strip().upper()
    for m_status, m_type, label, colour, svg in WELL_SYMBOLS:
        if m_status and s != m_status.upper():
            continue
        if m_type and t != m_type.upper():
            continue
        if not m_status and not m_type:
            continue
        return label, colour, svg
    return _SYM_FALLBACK


def add_well_symbols(m, engine, show=True, limit=4000, uwi_like=None):
    """Wells drawn with industry symbols, one toggleable group per kind.

    ONE GROUP PER SYMBOL, so the layer control doubles as the legend -- there
    is nowhere else on a folium map to put one, and a symbol set nobody can
    decode is decoration.

    Returns the number of wells drawn.
    """
    have = _table_columns(engine, "dv_well")
    if not have:
        return 0
    _c = lambda n: n if n in have else "NULL"          # noqa: E731
    where = "surface_latitude IS NOT NULL AND surface_longitude IS NOT NULL"
    params = {}
    if uwi_like:
        where += " AND uwi LIKE :u"
        params["u"] = uwi_like
    try:
        with engine.connect() as con:
            rows = con.execute(text(f"""
                SELECT TOP {int(limit)}
                       {_c('well_name')} AS nm, uwi,
                       surface_latitude AS la, surface_longitude AS lo,
                       {_c('well_status')} AS st, {_c('well_type')} AS ty,
                       {_c('well_profile_type')} AS pr,
                       {_c('bottom_hole_latitude')}  AS bla,
                       {_c('bottom_hole_longitude')} AS blo
                  FROM dataview.dv_well
                 WHERE {where}
            """), params).fetchall()
    except Exception as exc:
        print(f"[geography_layers] well symbol query failed: {exc}")
        return 0
    if not rows:
        return 0

    groups = {}
    for r in rows:
        label, colour, svg = _classify_well(r.st, r.ty)
        groups.setdefault((label, colour, svg), []).append(r)

    drawn = 0
    for (label, colour, svg), rs in sorted(groups.items(),
                                           key=lambda kv: -len(kv[1])):
        fg = folium.FeatureGroup(name=f"{label} ({len(rs):,})", show=show)
        body = svg.replace("{c}", colour)
        html = (f'<svg width="18" height="18" viewBox="0 0 18 18">{body}</svg>')
        for r in rs:
            _pr = (r.pr or "").strip().upper()
            folium.Marker(
                location=[float(r.la), float(r.lo)],
                icon=folium.DivIcon(
                    icon_size=(18, 18), icon_anchor=(9, 9), html=html),
                tooltip=folium.Tooltip(
                    f"<b>{r.nm or r.uwi}</b><br>{label}"
                    + (f"<br>{_pr.title()}" if _pr else "")),
            ).add_to(fg)
            # A DEVIATED WELL IS TWO PLACES. Drawing only the surface hole puts
            # the well where the rig stood, not where it produces -- and for a
            # horizontal that is most of a mile out. The stick is the honest
            # minimum until the full survey path is drawn.
            if _pr in ("DIRECTIONAL", "HORIZONTAL") and r.bla and r.blo:
                folium.PolyLine(
                    [[float(r.la), float(r.lo)], [float(r.bla), float(r.blo)]],
                    color=colour, weight=1.4, opacity=0.75, dash_array="4,3",
                ).add_to(fg)
                folium.CircleMarker(
                    [float(r.bla), float(r.blo)], radius=2.6, color=colour,
                    weight=1.2, fill=True, fill_color=colour, fill_opacity=0.9,
                    tooltip=f"{r.nm or r.uwi} — bottom hole",
                ).add_to(fg)
            drawn += 1
        fg.add_to(m)
    return drawn


# ── Leases, coloured by who owns them ────────────────────────────────────────
# A LEASE MAP'S WHOLE JOB IS TO SHOW WHO HOLDS WHAT. One colour for every tract
# answers "where is there acreage" and nothing else; the question anyone
# actually brings to it is whose acreage, and where two owners abut.
#
# The colours live HERE rather than in the database because they are a display
# choice, not a fact about the lease -- a second map with a different palette
# must not require an UPDATE. Owners not in the table get a stable colour from
# their own name, so an operator nobody anticipated still draws consistently
# rather than falling into a shared "other" bucket.
LEASE_OWNER_COLOURS = {
    "naval petroleum reserve operations": "#c0392b",
    "sweetwater resources llc":           "#2980b9",
    "bighorn basin energy co":            "#27ae60",
    "salt creek minerals trust":          "#8e44ad",
    "casper ridge petroleum":             "#e67e22",
    "powder river royalty partners":      "#16a085",
    "unleased federal acreage":           "#7f8c8d",
}
_FALLBACK_COLOURS = ["#d35400", "#2c3e50", "#c2185b", "#00838f", "#5d4037",
                     "#455a64", "#6a1b9a", "#00695c"]


def _d(v):
    """A date as YYYY-MM-DD. Empty when absent, never the word None."""
    return "" if v is None else str(v)[:10]


def _t(v):
    """A trimmed string, empty when absent. Keeps "None" off the popup."""
    return "" if v is None else str(v).strip()


def lease_colour(owner):
    """A stable colour for an owner, known or not."""
    key = (owner or "").strip().lower()
    if key in LEASE_OWNER_COLOURS:
        return LEASE_OWNER_COLOURS[key]
    # Deterministic across runs and machines -- hash() is salted per process,
    # so it would give the same lease a different colour on every rerun.
    import zlib
    return _FALLBACK_COLOURS[zlib.crc32(key.encode("utf-8"))
                             % len(_FALLBACK_COLOURS)]


# What the lease colours can mean. THE DEFAULT IS OWNER AND OFTEN CANNOT BE:
# BLM publishes no lessee, so operator_name is NULL on every federal lease and
# colouring by it puts all 288 in one grey pile. The other two are populated on
# exactly the data the first one is not, which is why this is a choice and not
# a constant.
LEASE_COLOUR_BY = {
    "owner":     ("operator_name", "Unknown owner"),
    "producing": ("producing_ind", "Unknown status"),
    "status":    ("lease_status",  "Unknown status"),
    "vintage":   ("effective_date", "Unknown vintage"),
    "size":      ("area_km2",       "Unknown size"),
}

# VINTAGE IS SEQUENTIAL, SO IT GETS A RAMP AND NOT THE HASH. lease_colour()
# picks a hue by CRC, which is right for identity (owner, status) and wrong
# for a quantity: a decade is ordered, and a rainbow over an ordered thing
# destroys the only structure it has. One hue, light to dark, and DARK IS
# OLDER -- the 1920s federal leases read as the heavy ones.
#
# The ramp starts mid-light rather than near-white because these are fills at
# 0.32 opacity over a pale basemap; the first two steps of a full ramp would
# be invisible, which is a legend entry that cannot be found on the map.
_VINTAGE_RAMP = [
    "#0b2a52", "#12406f", "#1a568b", "#246ca6", "#3382bc",
    "#4c97cc", "#68abd8", "#87bfe2", "#a6d1ec", "#c4e1f4", "#dcecf9",
]

# Size is sequential too, and gets its OWN hue so the two are never confused
# at a glance: blue reads as time on this map, warm as extent. Dark is BIGGER
# here, which is the direction the quantity runs -- the opposite convention to
# vintage on purpose, because "more acres" and "further back in time" are not
# the same kind of more.
_SIZE_RAMP = [
    "#fde6c8", "#f8c98c", "#ee9f4f", "#d97528", "#b2530f", "#7d3708",
]

# Which options are ORDERED. A hue picked by CRC is right for identity and
# wrong for a quantity, so these two take a ramp and everything else does not.
_SEQUENTIAL = {"vintage": _VINTAGE_RAMP, "size": _SIZE_RAMP}

# Acreage bands, ordered by a numeric prefix so the legend and the ramp agree.
# Fixed land bands rather than quantiles: 40 / 160 / 320 / 640 are the survey
# subdivisions a landman already reads, and a quantile boundary at 173.4 acres
# would mean nothing to anyone.
_SIZE_SQL = ("CASE WHEN area_km2 IS NULL THEN NULL"
             " WHEN area_km2*247.105 <   40 THEN '1. under 40 ac'"
             " WHEN area_km2*247.105 <  160 THEN '2. 40-160 ac'"
             " WHEN area_km2*247.105 <  320 THEN '3. 160-320 ac'"
             " WHEN area_km2*247.105 <  640 THEN '4. 320-640 ac'"
             " WHEN area_km2*247.105 < 1280 THEN '5. 640-1280 ac'"
             " ELSE '6. 1280+ ac' END")


def add_lease_layer(m, engine, show=True, limit=5000, by="owner"):
    """dv_land_tract coloured by owner (or producing status), grouped for the
    layer control.

    Returns the number of leases drawn.
    """
    have = _table_columns(engine, "dv_land_tract")
    if not have or "geog" not in have:
        return 0
    _c = lambda n: n if n in have else "NULL"          # noqa: E731
    _col, _unknown = LEASE_COLOUR_BY.get(by) or LEASE_COLOUR_BY["owner"]
    # Fall back rather than fail when the column is not there: a map that
    # draws in one colour beats a layer that returns 0 and reads as broken.
    if by == "vintage" and "effective_date" in have:
        _own_sql = ("CASE WHEN effective_date IS NULL THEN NULL ELSE "
                    "CAST((YEAR(effective_date)/10)*10 AS varchar(4)) + 's' END")
    elif by == "size" and "area_km2" in have:
        _own_sql = _SIZE_SQL
    else:
        _own_sql = _c(_col)
        if by in _SEQUENTIAL:
            by = "owner"          # column absent: fall back, never return 0
    try:
        with engine.connect() as con:
            rows = con.execute(text(f"""
                SELECT TOP {int(limit)}
                       {_c('tract_name')}     AS nm,
                       {_c('lease_number')}   AS ln,
                       {_own_sql}             AS own,
                       {_c('area_km2')}       AS km2,
                       {_c('effective_date')} AS eff,
                       {_c('expiry_date')}    AS exp,
                       {_c('lease_status')}   AS lst,
                       {_c('producing_ind')}  AS prd,
                       {_c('operator_name')}  AS opr,
                       {_c('province_state')} AS st,
                       {_c('source')}         AS src,
                       {_c('quality_note')}   AS qly,
                       geog.STAsText()        AS wkt
                  FROM dataview.dv_land_tract
                 WHERE geog IS NOT NULL
                   AND ISNULL(active_ind, 'Y') = 'Y'
            """)).fetchall()
    except Exception as exc:
        print(f"[geography_layers] lease query failed: {exc}")
        return 0
    if not rows:
        return 0

    by_owner = {}
    for r in rows:
        by_owner.setdefault((r.own or _unknown).strip(), []).append(r)

    drawn = 0
    # BY SIZE for identity, BY TIME for vintage. Sorting decades by how many
    # leases they hold would scatter the ramp through the layer control and
    # throw away the ordering the ramp exists to show.
    if by in _SEQUENTIAL:
        _ramp = _SEQUENTIAL[by]
        _dec = sorted(k for k in by_owner if k != _unknown)
        _step = (lambda i: _ramp[
            round(i * (len(_ramp) - 1) / max(len(_dec) - 1, 1))])
        _cmap = {k: _step(i) for i, k in enumerate(_dec)}
        _cmap[_unknown] = "#9aa0a6"          # neutral, outside the ramp
        _order = _dec + ([_unknown] if _unknown in by_owner else [])
        _colour_of = lambda o: _cmap.get(o, "#9aa0a6")   # noqa: E731
    else:
        _order = sorted(by_owner, key=lambda o: -len(by_owner[o]))
        _colour_of = lease_colour
    for owner in _order:
        group = by_owner[owner]
        colour = _colour_of(owner)
        feats = []
        for r in group:
            geom = _wkt_geometry(r.wkt)
            if not geom:
                continue
            feats.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    # tract_name is NULL on every BLM lease, so leading with
                    # it labelled 4,584 of 4,618 polygons "(unnamed)". The
                    # lease number is populated on all of them and is what
                    # the record is actually known by.
                    "nm": (r.nm or r.ln or "(unnamed)"),
                    "ln": r.ln or "",
                    "own": owner,
                    # Pre-formatted: a GeoJsonTooltip prints the property as
                    # it finds it, and Decimal('5.8671') is not an area.
                    "ac": (f"{float(r.km2) * 247.105:,.0f} ac"
                           if r.km2 is not None else ""),
                    "km": (f"{float(r.km2):,.2f} km2"
                           if r.km2 is not None else ""),
                    "eff": _d(getattr(r, "eff", None)),
                    "exp": _d(getattr(r, "exp", None)),
                    "lst": _t(getattr(r, "lst", None)),
                    "prd": _t(getattr(r, "prd", None)),
                    "opr": _t(getattr(r, "opr", None)),
                    "st":  _t(getattr(r, "st", None)),
                    "src": _t(getattr(r, "src", None)),
                    "qly": _t(getattr(r, "qly", None)),
                },
            })
        if not feats:
            continue
        fg = folium.FeatureGroup(name=f"▩ {owner} ({len(feats)})", show=show)
        folium.GeoJson(
            {"type": "FeatureCollection", "features": feats},
            style_function=lambda _f, _c=colour: {
                "color": _c, "weight": 1.6, "opacity": 0.95,
                "fillColor": _c, "fillOpacity": 0.32},
            highlight_function=lambda _f, _c=colour: {
                "weight": 3, "fillOpacity": 0.5},
            # HOVER IDENTIFIES, CLICK REPORTS. Two questions, and one control
            # cannot answer both: a tooltip long enough to hold the record
            # follows the pointer and hides what is under it. The popup is
            # pure Leaflet, so it costs nothing -- and, the point here, it
            # still opens with Freeze map on, where a click never reaches
            # Python at all.
            tooltip=folium.GeoJsonTooltip(
                fields=["nm", "own", "ln", "ac"],
                aliases=["Lease", "Owner", "Number", "Area"], sticky=True),
            popup=folium.GeoJsonPopup(
                fields=["nm", "ln", "lst", "prd", "eff", "exp",
                        "ac", "km", "opr", "st", "src", "qly"],
                aliases=["Lease", "Lease number", "Status", "Producing",
                         "Effective", "Expires", "Area", "Area (km2)",
                         "Operator", "State", "Source", "Quality"],
                labels=True, max_width=420),
        ).add_to(fg)
        fg.add_to(m)
        drawn += len(feats)
    return drawn


def _polygon_rings(wkt):
    """[[ [lon,lat], ... ]] from a POLYGON WKT, GeoJSON ring order.

    Interior rings are kept: a lease with a hole in it is a lease somebody
    else owns the middle of, and dropping the hole silently claims it.
    """
    if not wkt or "POLYGON" not in wkt.upper():
        return None
    body = wkt[wkt.find("(") + 1: wkt.rfind(")")]
    rings, depth, cur = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
            if depth == 1:
                cur = ""
                continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                rings.append(cur)
                continue
        if depth >= 1:
            cur += ch
    if not rings:
        rings = [body]
    out = []
    for r in rings:
        pts = []
        for pair in r.split(","):
            bits = pair.split()
            if len(bits) >= 2:
                try:
                    pts.append([float(bits[0]), float(bits[1])])
                except ValueError:
                    pass
        if len(pts) >= 4:
            out.append(pts)
    return out or None


def _wkt_geometry(wkt):
    """GeoJSON geometry from POLYGON *or* MULTIPOLYGON WKT. None if neither.

    _polygon_rings returns None for a MULTIPOLYGON -- its ring walker sees the
    extra nesting level and gives up -- and the caller's `if not ring: continue`
    turned that into a SILENT DROP. Harmless while dv_land_tract held 34
    synthetic single-part tracts; not harmless once real BLM leases arrived,
    where 40 of 288 are MultiPolygon carrying 101 parts between them. A lease
    serial covering non-contiguous tracts is one legal instrument and the map
    was drawing none of it.
    """
    if not wkt:
        return None
    head = wkt.strip().upper()
    if head.startswith("MULTIPOLYGON"):
        body = wkt[wkt.find("(") + 1: wkt.rfind(")")]
        polys, depth, cur = [], 0, ""
        for ch in body:
            if ch == "(":
                depth += 1
                if depth == 1:
                    cur = ""
                    continue
            if ch == ")":
                depth -= 1
                if depth == 0:
                    polys.append(cur)
                    continue
            if depth >= 1:
                cur += ch
        coords = []
        for p in polys:
            # Each part is the inside of a POLYGON's parentheses, so hand it
            # back through the single-polygon walker rather than repeating it.
            rings = _polygon_rings("POLYGON (" + p + ")")
            if rings:
                coords.append(rings)
        return {"type": "MultiPolygon", "coordinates": coords} if coords else None
    rings = _polygon_rings(wkt)
    return {"type": "Polygon", "coordinates": rings} if rings else None
