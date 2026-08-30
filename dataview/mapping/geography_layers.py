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
        # THE STROKE IS PART OF THE HIT AREA in SVG, so a slightly heavier
        # ring widens the target without growing the dot much: weight 2 adds
        # a pixel each side of a radius that is already screen-fixed.
        marker=folium.CircleMarker(radius=radius, color=color, weight=2,
                                   fill=True, fill_color=fill,
                                   fill_opacity=opacity),
        tooltip=folium.GeoJsonTooltip(fields=["nm"], labels=False),
        popup=(folium.GeoJsonPopup(fields=list(popup_fields),
                                   aliases=list(popup_aliases or popup_fields),
                                   labels=True, max_width=320)
               if popup_fields else None),
    ).add_to(m)
    return len(feats)


# The popup label that tells the map's click handler what it just got. It is
# the SENTINEL, not decoration: page_well_map identifies a dv_well click by
# finding a 14-digit UWI in the popup text, and the master's headers carry
# uwi14 too, so without a label to tell them apart a master header was sent to
# the scout builder for a well that is not in dv_well. Declared here and read
# there, so renaming the layer cannot silently break the check.
FEDWELL_POPUP_LABEL = "Federated well"

# What a LOADED well's popup answers. dv_well has 53 columns; these are the
# ones that identify a well at a glance, and every one is a column the table
# actually has (checked against sys.columns before this list was written).
_LOADED_POPUP = ("uwi", "operator_name", "field_name", "county",
                 "well_type", "well_status", "spud_date")
_LOADED_ALIASES = ["Loaded well", "UWI", "Operator", "Field", "County",
                   "Type", "Status", "Spud"]


def add_well_points(m, engine, show: bool = True, limit: int = 50000) -> int:
    """dv_well.geog as plain CircleMarkers — the native-geography view of the
    well set, independent of the lat/lon columns the main markers use.
    geography .Lat/.Long avoids WKT parsing entirely.

    IT HAS A POPUP NOW, and the old objection no longer holds. points_layer's
    docstring says "a tooltip, never a popup", because the click handler told
    a well marker from a density cell by "markers have popups, cells don't".
    That test was replaced 29 Aug -- the cell branch now requires the H3 layer
    to actually be ON -- so a popup here no longer steals a cell click. And
    unlike the master's headers these ARE dv_well rows, so a click reaching
    the scout builder is the correct outcome rather than a dead end.
    """
    have = _table_columns(engine, "dv_well")
    if not have or "geog" not in have:
        return 0
    nm = "well_name" if "well_name" in have else ("uwi" if "uwi" in have
                                                  else "NULL")
    # ONLY THE COLUMNS THIS DATABASE HAS. dv_well is a PPDM derivative and
    # deployments differ; naming one that is absent fails the whole layer
    # rather than the one field, which is how a map loses its wells over a
    # column nobody needed.
    cols = [c for c in _LOADED_POPUP if c in have]
    sel = "".join(", %s" % c for c in cols)
    try:
        with engine.connect() as con:
            rows = con.execute(text(f"""
                SELECT TOP {int(limit)} {nm} AS nm,
                       geog.Lat AS lat, geog.Long AS lon{sel}
                FROM dataview.dv_well
                WHERE geog IS NOT NULL
            """)).fetchall()
    except Exception as exc:
        print(f"[geography_layers] dv_well points query failed: {exc}")
        return 0
    if not rows:
        return 0
    aliases = ["Loaded well"] + [_LOADED_ALIASES[_LOADED_POPUP.index(c) + 1]
                                 for c in cols]
    return points_layer(
        m, ((r[1], r[2], r[0]) + tuple(r[3:]) for r in rows),
        name=f"⚫ Loaded wells (geog) ({len(rows):,})", show=show,
        extra=cols, popup_fields=["nm"] + cols, popup_aliases=aliases)


import folium as _f

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
    label = (f"🔵 Federated wells ({len(rows):,}"
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
            # BIG ENOUGH TO HIT. radius=2 is a 4px target and the popup was
            # the whole point of this path -- "make the tooltip easier to
            # select for a popup". A CircleMarker radius is already in SCREEN
            # pixels, so it is fixed at every zoom; it was simply too small.
            # 5 gives a 10px target, which is the usual minimum for a pointer.
            name=label, color="#1d4ed8", fill="#60a5fa", radius=5, show=show,
            opacity=0.7, extra=_f, popup_fields=["nm"] + _f,
            # "Reference well", NOT "Well", and it is load-bearing rather
            # than cosmetic. The map click handler identifies a loaded well by
            # digging a 14-digit UWI out of the popup TEXT -- and uwi14 is
            # exactly what these carry, so a reference-well click was being
            # read as a dv_well click and sent to the scout builder for a well
            # that may not be in dv_well at all. The label is the sentinel the
            # handler checks, so the popup says what it is to the reader AND
            # to the code.
            popup_aliases=[FEDWELL_POPUP_LABEL, "UWI", "Operator", "County",
                           "State", "Type", "Status", "TD", "Spud"])
    else:
        # SMALLER ON THE SAMPLED PATH, deliberately. This one has no popup to
        # hit -- it is the three-column probe -- and 48,000 dots at radius 5
        # is a smear, not a map. The path that can be clicked is the one that
        # is worth making clickable.
        n_drawn = points_layer(m, ((la, lo, nm) for nm, la, lo in rows),
                               name=label, color="#1d4ed8", fill="#60a5fa",
                               radius=2.5, show=show, opacity=0.7)
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


_BY_TITLE = {"owner": "Lease owner", "producing": "Lease · producing",
             "status": "Lease status", "vintage": "Lease · effective decade",
             "size": "Lease · size"}


def _add_lease_legend(m, entries, by):
    """A floating legend built from the colours the layer JUST assigned.

    TAKES THE COLOURS RATHER THAN RECOMPUTING THEM. A legend that derives its
    own swatches is a second implementation of the colour rule, and the two
    drift the first time one of them changes -- a legend that disagrees with
    the map is worse than none, because it is believed.

    BOTTOM-LEFT, because _add_status_legend pins bottom-right and both are on
    screen whenever wells and leases are drawn together.

    Same <details> + sessionStorage pattern as the status legend: the map is
    rebuilt on every rerun, so a Python-side fold would cost a redraw to
    close a box. The browser owns this one.
    """
    from branca.element import Template, MacroElement
    rows, extra = [], 0
    if len(entries) > 16:                 # SAY IT, do not silently truncate
        extra = len(entries) - 15
        entries = entries[:15]
    for label, colour, n in entries:
        rows.append(
            "<div style='display:flex;align-items:center;gap:6px;margin:2px 0'>"
            "<span style='width:12px;height:12px;background:%s;"
            "border:1px solid rgba(0,0,0,.35);display:inline-block;"
            "flex:0 0 auto'></span>"
            "<span style='font-size:11px;color:#1e293b;white-space:nowrap'>"
            "%s <span style='color:#64748b'>(%s)</span></span></div>"
            % (colour, label, format(n, ",")))
    if extra:
        rows.append("<div style='font-size:10px;color:#64748b;margin-top:3px'>"
                    "+%d more not listed</div>" % extra)
    html = (
        "<details id='wm-lease-legend' open style='position:fixed;"
        "bottom:22px;left:12px;z-index:9999;"
        "background:rgba(255,255,255,0.94);padding:6px 11px 8px;"
        "border-radius:6px;box-shadow:0 1px 5px rgba(0,0,0,0.35);"
        "max-height:48%;overflow:auto'>"
        "<summary style='font-size:11px;font-weight:700;color:#0f172a;"
        "cursor:pointer;outline:none;user-select:none'>" + _BY_TITLE.get(
            by, "Leases") + "</summary>"
        "<div style='margin-top:4px'>" + "".join(rows) + "</div></details>"
        "<script>(function(){"
        "var d=document.getElementById('wm-lease-legend');"
        "if(!d){return;}"
        "try{if(sessionStorage.getItem('dv_lease_legend_open')==='0')"
        "{d.removeAttribute('open');}}catch(e){}"
        "d.addEventListener('toggle',function(){"
        "try{sessionStorage.setItem('dv_lease_legend_open',d.open?'1':'0');}"
        "catch(e){}});"
        "})();</script>")
    el = MacroElement()
    el._template = Template("{% macro html(this, kwargs) %}" + html
                            + "{% endmacro %}")
    m.get_root().add_child(el)


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
# VALIDATED, NOT CHOSEN BY EYE. The previous set failed on its own terms:
# #27ae60 (Bighorn) against #16a085 (Powder River) measured deltaE 7.5 in
# NORMAL vision -- below the floor of 15, i.e. hard to tell apart with full
# colour vision, never mind deuteranopia. Two owners the map could not
# distinguish is a wrong answer dressed as a legend.
#
# These six are Okabe-Ito, the standard CVD-safe categorical basis. Checked:
# lightness band PASS, chroma floor PASS, normal-vision worst adjacent pair
# deltaE 16.4 PASS. The deutan warning at 7.6 is legal only with secondary
# encoding, and there are two -- the legend labels every group, and each
# owner is its own named FeatureGroup in the layer control.
#
# Grey stays for "unleased", deliberately OUTSIDE the categorical set: it is
# the absence of an owner, the same role the neutral plays in the vintage
# ramp, and it is meant to recede.
LEASE_OWNER_COLOURS = {
    "sweetwater resources llc":           "#0072B2",   # blue
    "powder river royalty partners":      "#E69F00",   # orange
    "bighorn basin energy co":            "#009E73",   # green
    "casper ridge petroleum":             "#CC79A7",   # pink
    "salt creek minerals trust":          "#D55E00",   # vermillion
    "naval petroleum reserve operations": "#56B4E9",   # sky
    "unleased federal acreage":           "#7f8c8d",   # neutral, on purpose
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


def lease_colour_map(names):
    """Colours for a WHOLE set of owners at once: {name: colour}.

    WHY NOT JUST CALL lease_colour() PER NAME. It picks by crc32 % 8, and a
    hash into eight buckets collides long before eight names -- by the
    birthday bound the odds are better than even at five, and two owners
    sharing a colour makes a legend that cannot be read against the map. The
    hash is only there to be STABLE across runs, and sorted order is stable
    too, so nothing is given up by assigning from the set.

    Known owners keep their hand-picked colour. The rest take the fallback
    palette in sorted order, skipping any colour a known owner already holds,
    so a collision needs MORE owners than there are colours -- and past that
    it degrades to the old behaviour rather than failing.

    Deterministic: same set in, same map out, on any machine, before and
    after a reload. That is what makes a screenshot reproducible.
    """
    out, used = {}, set()
    unknown = []
    for n in names:
        key = (n or "").strip().lower()
        if key in LEASE_OWNER_COLOURS:
            out[n] = LEASE_OWNER_COLOURS[key]
            used.add(out[n])
        else:
            unknown.append(n)
    free = [c for c in _FALLBACK_COLOURS if c not in used] or _FALLBACK_COLOURS
    for i, n in enumerate(sorted(unknown, key=lambda x: (x or "").lower())):
        out[n] = free[i % len(free)]
    return out


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


def add_lease_layer(m, engine, show=True, limit=25000, by="owner",
                    legend=True):
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

    # A TOP THAT CLIPS LOOKS EXACTLY LIKE A COMPLETE MAP. The layer returns a
    # count and reports success either way, so a silent truncation is a
    # confident wrong answer -- the failure this codebase names first. Ask the
    # table how many there ARE and say so when they disagree. Cheap: a COUNT
    # on the same predicate, no geometry touched, and only when the returned
    # count actually reached the limit.
    if len(rows) >= int(limit):
        try:
            with engine.connect() as con:
                _total = con.execute(text(
                    "SELECT COUNT(*) FROM dataview.dv_land_tract "
                    "WHERE geog IS NOT NULL AND ISNULL(active_ind,'Y')='Y'"
                )).scalar() or 0
        except Exception:
            _total = 0
        if _total > len(rows):
            print("[geography_layers] LEASE LAYER CLIPPED: drew %d of %d "
                  "(limit=%d) -- the map is NOT showing every lease."
                  % (len(rows), _total, int(limit)))

    by_owner = {}
    for r in rows:
        by_owner.setdefault((r.own or _unknown).strip(), []).append(r)

    # ── LEASES GO UNDER EVERYTHING ──────────────────────────────────────
    # Leaflet draws vector overlays in add order within one pane, and the
    # geography block runs AFTER the wells and H3 blocks -- so 4,618 filled
    # polygons landed on top of the wells and hexagons and bled through.
    #
    # A PANE, NOT bringToBack(). Draw order is only the default: toggling a
    # layer off and on in the layer control re-adds it, which puts it back
    # on top and undoes any one-off reordering. A pane is a standing
    # z-index, so it survives the toggle. 350 sits below the default
    # overlayPane (400), where the wells and hexagons draw.
    #
    # pointer_events=True IS LOAD-BEARING: folium's CustomPane defaults it
    # to False, which emits pointerEvents:none -- the pane would take no
    # clicks at all, silently killing the tooltip and the popup.
    if not getattr(m, "_dv_lease_pane", False):
        from folium.map import CustomPane
        CustomPane("dvleases", z_index=350, pointer_events=True).add_to(m)
        try:
            m._dv_lease_pane = True
        except Exception:
            pass

    drawn = 0
    _legend = []
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
            pane="dvleases",
            # Matches the served-file path. Two paths drawing one layer
            # differently is how a fallback becomes a different map.
            style_function=lambda _f, _c=colour: {
                "color": _c, "weight": 1.0, "opacity": 0.9,
                "fillColor": _c, "fillOpacity": 0.38},
            highlight_function=lambda _f, _c=colour: {
                "weight": 2.4, "fillOpacity": 0.6},
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
        _legend.append((owner, colour, len(feats)))
    # BUILT FROM THE COLOURS JUST ASSIGNED, never recomputed: a legend that
    # derives its own swatches is a second copy of the colour rule, and the
    # two drift the first time one changes. A legend that disagrees with the
    # map is worse than none, because it is believed.
    if legend and _legend:
        _add_lease_legend(m, _legend, by)
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


# ── Leases as a STATIC FILE rather than payload ─────────────────────────────
# Measured on 4,618 real BLM leases: embedded, the layer puts ~4 MB of geometry
# into the map HTML and costs ~1.0s of folium render on EVERY rerun -- the
# largest single item in a render. Served as a file with embed=False, the map
# HTML carries a link instead: ~0 MB and 0.14s, and the browser caches the file
# across renders.
#
# ONE FILE SERVES EVERY COLOURING. The colour for all five dimensions is baked
# into each feature (_c_owner, _c_producing, _c_status, _c_vintage, _c_size),
# and the JS picks the one named at render time -- so changing the colour-by
# costs no rebuild, no re-fetch and no new file. Baking a single colour would
# have made the file a function of the dropdown: a cache key nobody would
# remember to invalidate, and a stale map is a wrong one.
#
# style_function CANNOT be used on this path. folium requires a Python callable
# and rejects JsCode, and with embed=False there are no local features to call
# it on. on_each_feature is typed for JsCode and does the styling, the tooltip
# and the popup in the browser instead.
LEASE_GEOJSON_NAME = "dv_leases.geojson"

# BUMP WHEN THE FILE FORMAT CHANGES, not only when the data does. The
# signature exists to decide whether to rebuild; keyed on the data alone, a
# code change that adds a property serves the old file forever and the new
# feature is silently missing. v2 added _la/_lo for clip-to-selection.
LEASE_GEOJSON_FORMAT = 6      # v6: no dark edge, stroke = fill


def lease_data_signature(engine) -> str:
    """Cheap fingerprint of the lease table, for deciding whether to rebuild.

    Count plus the newest row stamp. A rebuild that never fires shows stale
    data; a rebuild on every render is the cost this whole path exists to
    remove. Both failures are silent, so the signature is deliberately dumb
    and cheap rather than clever.
    """
    try:
        with engine.connect() as con:
            r = con.execute(text(
                "SELECT COUNT(*), CONVERT(varchar(30), MAX(row_created_date), 126) "
                "FROM dataview.dv_land_tract WHERE geog IS NOT NULL")).first()
        return "v%s|%s|%s" % (LEASE_GEOJSON_FORMAT, r[0], r[1])
    except Exception as exc:
        print(f"[geography_layers] lease signature failed: {exc}")
        return ""


def _vintage_label(eff):
    return "" if not eff else "%ss" % ((int(str(eff)[:4]) // 10) * 10)


def _size_label(km2):
    if km2 is None:
        return ""
    ac = float(km2) * 247.105
    for lim, lbl in ((40, "1. under 40 ac"), (160, "2. 40-160 ac"),
                     (320, "3. 160-320 ac"), (640, "4. 320-640 ac"),
                     (1280, "5. 640-1280 ac")):
        if ac < lim:
            return lbl
    return "6. 1280+ ac"


def write_lease_geojson(engine, out_dir, limit=200000):
    """Write every lease to one GeoJSON file, colours for all dimensions baked.

    Returns (path, feature_count, legend) where legend is
    {by: [(label, colour, n), ...]}, already ordered the way each dimension
    wants -- by size for the categorical ones, by time or band for the ramps.
    Built here so the legend and the map cannot disagree, which is the rule the
    embedded path follows too.
    """
    # LOCAL, AND BOTH OF THEM. os is NOT imported at module level in this
    # file (only folium and sqlalchemy.text are), so os.makedirs/join/
    # replace below would have raised NameError the first time anyone
    # built the file -- and only then. The bare-name trap CLAUDE.md opens
    # with, caught by checking rather than by reading.
    import json
    import os
    have = _table_columns(engine, "dv_land_tract")
    if not have or "geog" not in have:
        return None, 0, {}
    # QUALIFIED, because this query joins dv_land_tract_geom and that table
    # carries geog, area_km2, province_state, source and quality_note under
    # the same names. Prefixing in the lambda rather than at each call site
    # means a column added later cannot be the one that forgot.
    _c = lambda n: ("lt." + n) if n in have else "NULL"  # noqa: E731
    with engine.connect() as con:
        _has_twp = con.execute(text(
            "SELECT CASE WHEN OBJECT_ID('dataview.dv_land_tract_geom') "
            "IS NOT NULL AND COL_LENGTH('dataview.dv_land_tract_geom',"
            "'plss_id') IS NOT NULL THEN 1 ELSE 0 END")).scalar()
        _twcol = "tg.plss_id" if _has_twp else "NULL"
        # THE COUNTY COMES OFF THE SAME JOIN. dv_land_tract (the view) does
        # not expose it; dv_land_tract_geom stamps it on 24,177 of 24,178
        # tracts, which is what makes a county filter a column test rather
        # than a spatial join.
        _cocol = "tg.county" if _has_twp else "NULL"
        # ELEVATION AND THE TWO DISTANCES, stamped by tools/stamp_elevation.py
        # and tools/stamp_cultural_distance.py. Guarded individually: the
        # file must still build on a database where those tools have never
        # been run, and _has_twp only proves the geom table exists.
        _extra = {}
        for _c2 in ("elevation_ft", "dist_city_km", "near_city",
                    "dist_hwy_km", "near_hwy"):
            _extra[_c2] = ("tg." + _c2) if (_has_twp and con.execute(text(
                "SELECT COL_LENGTH('dataview.dv_land_tract_geom', :c)"),
                {"c": _c2}).scalar() is not None) else "NULL"
        _twjoin = ("LEFT JOIN dataview.dv_land_tract_geom tg "
                   "ON tg.tract_id = lt.land_tract_id"
                   if _has_twp else "")
        rows = con.execute(text(f"""
            SELECT TOP {int(limit)}
                   {_c('tract_name')}     AS nm,
                   {_c('lease_number')}   AS ln,
                   {_c('operator_name')}  AS opr,
                   {_c('producing_ind')}  AS prd,
                   {_c('lease_status')}   AS lst,
                   {_c('effective_date')} AS eff,
                   {_c('expiry_date')}    AS exp,
                   {_c('area_km2')}       AS km2,
                   {_c('province_state')} AS st,
                   {_c('source')}         AS src,
                   {_c('quality_note')}   AS qly,
                   -- THE TOWNSHIP THE TRACT WAS STAMPED WITH, so a township
                   -- click can select on the same fact its tooltip counted.
                   -- LEFT JOIN and a guard: dv_land_tract is a VIEW and does
                   -- not expose plss_id, and COL_LENGTH alone returns NULL
                   -- for a missing TABLE and a missing COLUMN alike -- so it
                   -- is paired with OBJECT_ID, or the guard skips silently.
                   {_twcol}               AS twp,
                   {_cocol}               AS cty,
                   {_extra['elevation_ft']}  AS el,
                   {_extra['dist_city_km']}  AS dcity,
                   {_extra['near_city']}     AS ncity,
                   {_extra['dist_hwy_km']}   AS dhwy,
                   {_extra['near_hwy']}      AS nhwy,
                   lt.geog.STAsText()     AS wkt
              FROM dataview.dv_land_tract lt
              {_twjoin}
             WHERE lt.geog IS NOT NULL
               AND ISNULL(lt.active_ind, 'Y') = 'Y'
        """)).fetchall()

    DIMS = ("owner", "producing", "status", "vintage", "size")
    feats = []
    counts = {k: {} for k in DIMS}
    for r in rows:
        geom = _wkt_geometry(r.wkt)
        if not geom:
            continue
        lab = {
            "owner":     _t(r.opr) or "Unknown owner",
            "producing": _t(r.prd) or "Unknown status",
            "status":    _t(r.lst) or "Unknown status",
            "vintage":   _vintage_label(r.eff) or "Unknown vintage",
            "size":      _size_label(r.km2) or "Unknown size",
        }
        for k, v in lab.items():
            counts[k][v] = counts[k].get(v, 0) + 1
        props = {
            "nm": (_t(r.nm) or _t(r.ln) or "(unnamed)"), "ln": _t(r.ln),
            "lst": _t(r.lst), "prd": _t(r.prd), "eff": _d(r.eff),
            "exp": _d(r.exp), "opr": _t(r.opr), "st": _t(r.st),
            "src": _t(r.src), "qly": _t(r.qly),
            "ac": ("%s ac" % format(int(float(r.km2) * 247.105), ",")
                   if r.km2 is not None else ""),
            "_tw": _t(r.twp),
            # NORMALISED FOR MATCHING, not for display. Seven tracts straddle
            # two counties and the source recorded both in one field with
            # inconsistent separators -- "Campbell & Converse" and
            # "Campbell,Johnson". Stored as ",campbell,converse," so a filter
            # can ask "does this contain ,campbell,?" and a straddling lease
            # answers yes under BOTH its counties, instead of being silently
            # missed by county = 'Campbell'.
            "_co": _county_key(r.cty),
            # NUMBERS FOR THE FILTER, prose for the popup -- the same split
            # the 640-acre boundary forced: never filter on a display string.
            "_el": (round(float(r.el), 1) if r.el is not None else None),
            "_dc": (round(float(r.dcity), 4) if r.dcity is not None else None),
            "_dh": (round(float(r.dhwy), 4) if r.dhwy is not None else None),
            "el": ("%s ft" % format(int(float(r.el)), ",")
                   if r.el is not None else ""),
            "ncity": ("%s (%.1f mi)" % (_t(r.ncity),
                                        float(r.dcity) * 0.621371)
                      if (r.ncity and r.dcity is not None) else ""),
            "nhwy": ("%s (%.1f mi)" % (_t(r.nhwy),
                                       float(r.dhwy) * 0.621371)
                     if (r.nhwy and r.dhwy is not None) else ""),
            # THE NUMBER, BESIDE THE STRING THAT DISPLAYS IT. "km" is
            # "%.2f km2" for a popup; filtering on it means filtering on a
            # ROUNDED value, and at a threshold that is exactly one section
            # that changes the answer: 2.589988 km2 is 639.9997 acres and
            # fails ">= 640", while the rounded 2.59 is 640.0018 and passes.
            # 1,043 section-sized leases sat on that boundary, so the map
            # drew 7,369 where the count said 6,326. Same source, same
            # arithmetic, both sides.
            "_km2": (round(float(r.km2), 6) if r.km2 is not None else None),
            "km": ("%.2f km2" % float(r.km2) if r.km2 is not None else ""),
        }
        for k, v in lab.items():
            props["_l_" + k] = v
        # BBOX CENTRE, for clip-to-selection. Centre-in-box matches what a
        # drawn box already means for hexagons, so the three layers agree on
        # what "inside" is instead of each having its own rule.
        _xs, _ys = [], []
        _cs = geom.get("coordinates") or []
        _polys = _cs if geom.get("type") == "MultiPolygon" else [_cs]
        for _poly in _polys:
            for _ring in (_poly or []):
                for _pt in (_ring or []):
                    _xs.append(_pt[0]); _ys.append(_pt[1])
        if _xs:
            props["_lo"] = round((min(_xs) + max(_xs)) / 2.0, 6)
            props["_la"] = round((min(_ys) + max(_ys)) / 2.0, 6)
        feats.append({"type": "Feature", "geometry": geom, "properties": props})

    # Colours assigned exactly as the embedded path assigns them: a ramp for
    # the ordered dimensions, the CRC hue for identity.
    legend, cmaps = {}, {}
    for k in DIMS:
        if k in _SEQUENTIAL:
            unk = LEASE_COLOUR_BY[k][1]
            ordered = sorted(x for x in counts[k] if x != unk)
            ramp = _SEQUENTIAL[k]
            n_ord = len(ordered)
            cmap = {x: ramp[round(i * (len(ramp) - 1) / max(n_ord - 1, 1))]
                    for i, x in enumerate(ordered)}
            cmap[unk] = "#9aa0a6"
            order = ordered + ([unk] if unk in counts[k] else [])
        else:
            # THE WHOLE SET AT ONCE, not one name at a time: per-name it is
            # crc32 % 8, which collides at five owners more often than not.
            cmap = lease_colour_map(counts[k])
            order = sorted(counts[k], key=lambda x: -counts[k][x])
        cmaps[k] = cmap
        legend[k] = [(x, cmap[x], counts[k][x]) for x in order]
    for f in feats:
        for k in DIMS:
            f["properties"]["_c_" + k] = cmaps[k][f["properties"]["_l_" + k]]

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, LEASE_GEOJSON_NAME)
    tmp = path + ".tmp"
    # WRITE THEN REPLACE. The browser can request this file while it is being
    # rebuilt, and half a GeoJSON is a layer that fails to parse -- silently,
    # because a fetch error in the browser never reaches Python.
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh)
    os.replace(tmp, path)
    return path, len(feats), legend


_LEASE_ON_EACH = """
function(feature, layer) {
    var p = feature.properties;
    var c = p['_c___BY__'] || '#9aa0a6';
    // OUTSIDE THE CLIP BOX: hidden, and not clickable. opacity alone leaves
    // an invisible polygon still swallowing clicks over the wells beneath.
    var CLIP = __CLIP__;
    var GONE = {opacity: 0, fillOpacity: 0};
    function hide() {
        feature.properties.style = GONE;
        layer.setStyle(GONE);
        if (layer._path) { layer._path.style.pointerEvents = "none"; }
    }
    if (CLIP && (p._la < CLIP[0] || p._la > CLIP[2] ||
                 p._lo < CLIP[1] || p._lo > CLIP[3])) {
        hide();
        return;
    }
    // ── THE PANEL'S FILTERS, APPLIED TO WHAT IS DRAWN ────────────────────
    // The panel said "6,131 lease(s) match" and the map drew all 24,178:
    // source, status, operator and minimum acres reached the COUNT and
    // nothing else. A number beside a map that disagrees with the map is
    // the confident-wrong-value failure, and the count was the believable
    // half.
    //
    // Filtered HERE rather than in the query, because the file is a cached
    // artifact shared by every filter combination -- re-querying and
    // rewriting 29 MB per filter change would undo the whole reason it is
    // served as a file. The browser already holds every lease; deciding
    // which to show is the same client-side job the clip box above does.
    //
    // THE PREDICATES MIRROR THE SQL the count runs, deliberately:
    //   source / status  IN (...)      -> exact match against the list
    //   operator_name LIKE '%op%'      -> case-insensitive substring
    //   area_km2 * 247.105 >= :ac      -> same arithmetic, same constant
    // A lease with no area fails the acres test, exactly as a NULL fails
    // the SQL comparison -- unknown size must not be claimed as a match.
    var FILT = __FILT__;
    if (FILT) {
        if (FILT.src && FILT.src.length &&
                FILT.src.indexOf(p.src) < 0) { hide(); return; }
        if (FILT.lst && FILT.lst.length &&
                FILT.lst.indexOf(p.lst) < 0) { hide(); return; }
        if (FILT.st && FILT.st.length &&
                FILT.st.indexOf(p.st) < 0) { hide(); return; }
        if (FILT.co && FILT.co.length) {
            // CONTAINMENT, NOT EQUALITY. _co is ",campbell,converse," for a
            // lease straddling two counties; asking for Campbell must find
            // it. The wrapping commas keep the test exact.
            var ck = p._co;
            if (!ck) { hide(); return; }
            var anyCo = false;
            for (var ci = 0; ci < FILT.co.length; ci++) {
                if (ck.indexOf(',' + FILT.co[ci] + ',') >= 0) {
                    anyCo = true; break;
                }
            }
            if (!anyCo) { hide(); return; }
        }
        if (FILT.opr) {
            var o = (p.opr || '').toLowerCase();
            if (o.indexOf(FILT.opr) < 0) { hide(); return; }
        }
        // ELEVATION AND DISTANCE, on the numeric properties. A lease with
        // no stamp fails a filter that asks about it, the way a NULL fails
        // the SQL comparison -- unknown must not be reported as a match.
        if (FILT.elmin !== undefined || FILT.elmax !== undefined) {
            var el = p._el;
            if (el === undefined || el === null) { hide(); return; }
            if (FILT.elmin !== undefined && el < FILT.elmin) { hide(); return; }
            if (FILT.elmax !== undefined && el > FILT.elmax) { hide(); return; }
        }
        if (FILT.dcity !== undefined) {
            var dc = p._dc;
            if (dc === undefined || dc === null || dc > FILT.dcity) {
                hide(); return;
            }
        }
        if (FILT.dhwy !== undefined) {
            var dh = p._dh;
            if (dh === undefined || dh === null || dh > FILT.dhwy) {
                hide(); return;
            }
        }
        if (FILT.ac) {
            // _km2 IS THE NUMBER; km is the rounded string for the popup.
            // Filtering on the display value put 1,043 section-sized leases
            // on the wrong side of "at least 640 acres". Falls back to the
            // string for a file written before _km2 existed, and an unknown
            // area fails the test the way a NULL fails the SQL comparison.
            var km = (p._km2 === undefined || p._km2 === null)
                     ? parseFloat(p.km) : p._km2;
            if (!(km * 247.105 >= FILT.ac)) { hide(); return; }
        }
    }
    // NO DARK EDGE AT ALL. Tried three times: the tract's own hue (tracts of
    // one owner merge), black at 2.2 (the edges become the picture at state
    // scale), a 0.9px dark hairline (still too much). Every dark variant
    // reads as a grid over 10,924 tracts, because Leaflet strokes are screen
    // pixels -- zoom out and the tracts shrink while the lines do not.
    //
    // The stroke is the fill colour now, so it separates neighbours of
    // DIFFERENT owners while neighbours of the same owner merge -- which is
    // the honest picture of a lease block anyway. Fill stays at 0.38: the
    // fill is carrying identity on its own, so it can be a little stronger.
    var base = {color: c, weight: 1.0, opacity: 0.9,
                fillColor: c, fillOpacity: 0.38};
    // AND ON THE FEATURE, not only the layer. folium emits its own
    // setStyle(f => f.properties.style) AFTER addData, so a style set only
    // on the layer here is overwritten with undefined a moment later --
    // every polygon would fall back to Leaflet's default blue. Writing the
    // property makes folium's own call apply exactly this style.
    feature.properties.style = base;
    layer.setStyle(base);
    layer.on('mouseover', function(){
        layer.setStyle({weight: 2.4, fillOpacity: 0.6}); });
    layer.on('mouseout', function(){ layer.setStyle(base); });
    layer.bindTooltip('<b>' + p.nm + '</b><br>' + p['_l___BY__'] +
                      (p.ac ? '<br>' + p.ac : ''), {sticky: true});
    var rows = [['Lease', p.nm], ['Lease number', p.ln],
                ['Status', p.lst], ['Producing', p.prd],
                ['Effective', p.eff], ['Expires', p.exp],
                ['Area', p.ac], ['Area (km2)', p.km],
                ['Operator', p.opr], ['State', p.st],
                ['Elevation', p.el],
                ['Nearest town', p.ncity], ['Nearest highway', p.nhwy],
                ['Source', p.src], ['Quality', p.qly]];
    var h = '<table style="font-size:11px;border-collapse:collapse">';
    for (var i = 0; i < rows.length; i++) {
        if (!rows[i][1]) { continue; }
        h += '<tr><td style="color:#64748b;padding-right:8px">' + rows[i][0] +
             '</td><td>' + rows[i][1] + '</td></tr>';
    }
    layer.bindPopup(h + '</table>', {maxWidth: 420});
}"""


def lease_on_each(by, clip=None, filt=None):
    """_LEASE_ON_EACH with EVERY placeholder filled. The only way to use it.

    THE TEMPLATE HAS THREE HOLES AND HAD TWO CALLERS, and when __FILT__ was
    added for the lease filters only one caller learned about it. The other
    is the township click, which injects the same template to style the
    leases it fetches on demand -- so it shipped a literal __FILT__ into the
    browser and died with "ReferenceError: __FILT__ is not defined" the
    moment somebody clicked a township. Reported from the map, not caught
    here, because a Python-side syntax check cannot see an unfilled token in
    a string.

    That is the "lists that must agree" failure at its smallest: two call
    sites, one template, a new hole. One function fills them all now, so a
    fourth placeholder cannot be half-applied.
    """
    key = by if by in LEASE_COLOUR_BY else "producing"
    out = (_LEASE_ON_EACH
           .replace("__BY__", key)
           .replace("__CLIP__",
                    ("[%r, %r, %r, %r]" % (clip[0][0], clip[0][1],
                                           clip[1][0], clip[1][1]))
                    if clip else "null")
           .replace("__FILT__", _filter_literal(filt)))
    # CAUGHT HERE, NOT IN THE BROWSER. An unfilled __TOKEN__ is valid Python,
    # valid JSON and valid-looking JavaScript right up until it runs, so
    # nothing upstream of the user's screen notices. This is the one place
    # that can see the finished text.
    import re as _re          # not module-level in this file; see CLAUDE.md
    _left = _re.findall(r"__[A-Z_]+__", out)
    if _left:
        raise ValueError(
            "lease_on_each: placeholder(s) never filled: %s -- add them to "
            "this function, which is the only filler." % ", ".join(sorted(set(_left))))
    return out


def _county_key(raw):
    """A county string as a matchable token list: ",campbell,converse,".

    ONE SPELLING OF THE RULE, used by the file, the browser filter and the
    SQL count -- three places that must agree about whether a straddling
    lease is "in Campbell County". It is, and equality says otherwise.

    Both separators seen in the data are handled ("&" and ","), and the
    wrapping commas make a containment test exact: ",campbell," cannot match
    inside ",campbell county," or a county whose name merely starts the same.
    """
    if not raw:
        return None
    parts = [p.strip().lower()
             for p in str(raw).replace("&", ",").split(",")]
    parts = [p for p in parts if p]
    return ("," + ",".join(parts) + ",") if parts else None


def _filter_literal(filt):
    """The panel's filters as a JS literal, or "null" when nothing is set.

    ONE PLACE DECIDES WHAT "EMPTY" MEANS. A filter of empty lists and blank
    strings matches everything, and shipping it as an object would run four
    tests per feature across 24,178 features to reach that conclusion --
    and, worse, would make `if (FILT)` true, so a bug in any predicate
    would blank the layer for a user who had filtered nothing.
    """
    import json as _j
    if not filt:
        return "null"
    out = {}
    _src = [s for s in (filt.get("source") or []) if s]
    _lst = [s for s in (filt.get("status") or []) if s]
    _st = [s for s in (filt.get("state") or []) if s]
    # LOWER-CASED HERE so the browser compares like for like against the
    # normalised _co key, rather than lower-casing 24,178 times.
    _co = [str(s).strip().lower() for s in (filt.get("county") or []) if s]
    _opr = str(filt.get("operator") or "").strip().lower()
    try:
        _ac = float(filt.get("min_acres") or 0)
    except (TypeError, ValueError):
        _ac = 0.0
    if _src:
        out["src"] = _src
    if _lst:
        out["lst"] = _lst
    if _st:
        out["st"] = _st
    if _co:
        out["co"] = _co
    if _opr:
        out["opr"] = _opr
    if _ac > 0:
        out["ac"] = _ac
    # MILES IN, KILOMETRES OUT. The panel asks in miles because that is what
    # a land man says; the stamp is in km because that is what the projection
    # measured. Converted once, here, so the browser never sees a unit.
    _elmin = filt.get("elev_min")
    _elmax = filt.get("elev_max")
    if _elmin is not None:
        out["elmin"] = float(_elmin)
    if _elmax is not None:
        out["elmax"] = float(_elmax)
    for _k, _src in (("dcity", "miles_city"), ("dhwy", "miles_hwy")):
        _v = filt.get(_src)
        try:
            _v = float(_v or 0)
        except (TypeError, ValueError):
            _v = 0.0
        if _v > 0:
            out[_k] = _v * 1.609344
    return _j.dumps(out) if out else "null"


def add_lease_layer_file(m, path, url, by="producing", show=True,
                         legend=None, clip=None, filt=None):
    """Leases from a SERVED GeoJSON file, styled entirely in the browser.

    TWO ARGUMENTS FOR ONE FILE, and they are not interchangeable:
      path -- where THIS PROCESS reads it from (./static/...)
      url  -- where the BROWSER fetches it from (/app/static/...)

    folium needs the data even when embed=False: process_data() opens a
    filename or fetches an http URL regardless, because it derives the
    layer bounds from it. Only the EMITTED LINK changes. So handing it the
    browser path raised FileNotFoundError, and handing it an http URL would
    have made the server fetch its own file over the network. It reads the
    local file (~0.15s, once per render) and embed_link is then pointed at
    the URL the browser should use.
    """
    import folium as _f
    if not getattr(m, "_dv_lease_pane", False):
        from folium.map import CustomPane
        CustomPane("dvleases", z_index=350, pointer_events=True).add_to(m)
        try:
            m._dv_lease_pane = True
        except Exception:
            pass
    key = by if by in LEASE_COLOUR_BY else "producing"
    _gj = _f.GeoJson(
        path, embed=False, pane="dvleases", show=show,
        name="▩ Leases",
        on_each_feature=_f.JsCode(lease_on_each(key, clip=clip, filt=filt)),
    )
    # The link folium emits must be the BROWSER's, not ours.
    _gj.embed_link = url
    _gj.add_to(m)
    if legend:
        _add_lease_legend(m, legend, key)
    return url


# ── TOWNSHIPS: THE GRID THE LEASES WERE WRITTEN ON ─────────────────────────
# 2,888 surveyed Wyoming townships from BLM CadNSDI, coloured by leased
# acreage. This is the aggregate the lease layer cannot be: 24,178 polygons
# render as a smear below about zoom 9, and a hexagon would cut across the
# section lines every one of these leases was described against.
#
# THE MEASURE IS ACREAGE, NOT COUNT, and that is the one place lease
# clustering must differ from well clustering. A well is a point, so counting
# points is the honest summary. A 40-acre lease and a 1,280-acre lease are
# not the same fact, so a township holding three big leases is more leased
# than one holding twenty small ones -- and colouring by count would say the
# opposite.
#
# RECTANGLES FROM bbox_wkt, NOT POLYGONS. dv_plss_township stores a centroid
# and a bounding box and no geometry column, which is the schema's own
# judgement that a township is a grid REFERENCE. A township IS a rectangle to
# within the survey's own adjustments, so the box is the shape.
TOWNSHIP_RAMP = ["#f0d9b8", "#dfae72", "#c8813c", "#a55a1c", "#7a3a0d"]


def _twp_bounds(wkt):
    """(s, w, n, e) from the stored POLYGON((...)) box, or None."""
    import re
    nums = [float(x) for x in re.findall(r"-?\d+\.?\d*", wkt or "")]
    if len(nums) < 8:
        return None
    xs, ys = nums[0::2], nums[1::2]
    return (min(ys), min(xs), max(ys), max(xs))


def add_township_layer(m, engine, show=True, state="WY", bounds=None,
                       lease_url=None, lease_by="producing"):
    """PLSS townships shaded by leased acreage. (drawn, leased_townships).

    ONE QUERY, AGGREGATED IN SQL. Pulling 24,178 leases to count them in
    Python is the thing this repo has measured three times; the join is a
    BETWEEN on the township box against the lease centroid, which the
    lat/lon index serves.
    """
    where = ["t.province_state_id = :st", "t.bbox_wkt IS NOT NULL"]
    params = {"st": state}
    if bounds:
        try:
            (s, w), (n, e) = bounds
            where.append("t.centroid_latitude BETWEEN :s AND :n")
            where.append("t.centroid_longitude BETWEEN :w AND :e")
            params.update({"s": float(s), "n": float(n),
                           "w": float(w), "e": float(e)})
        except Exception:
            pass
    clause = " AND ".join(where)
    # LOCAL, and named so: this module has no module-level `json`, and a bare
    # name that resolves only when the line runs is the failure CLAUDE.md
    # opens its list with.
    import json as _json
    _lkey = lease_by if lease_by in LEASE_COLOUR_BY else "producing"
    try:
        with engine.connect() as con:
            rows = con.execute(text(f"""
                SELECT t.plss_id, t.township_label, t.bbox_wkt,
                       COUNT(g.tract_id)                         AS n,
                       ISNULL(SUM(g.area_km2), 0) * 247.105       AS acres
                  FROM dataview.dv_plss_township t
                  -- ON THE STAMPED COLUMN, NOT A SPATIAL FUNCTION. The
                  -- first draft joined on geog.EnvelopeCenter() inside a
                  -- BETWEEN, so the server evaluated a spatial function
                  -- across 2,888 x 24,178 rows and the layer took 75.6
                  -- seconds. assign_tract_townships stamps plss_id once --
                  -- the same trick h3_refresh uses for wells -- and this
                  -- becomes a GROUP BY on an indexed column.
                  LEFT JOIN dataview.dv_land_tract_geom g
                    ON  g.plss_id = t.plss_id
                 WHERE {clause}
                 GROUP BY t.plss_id, t.township_label, t.bbox_wkt
            """), params).fetchall()
    except Exception as exc:
        print(f"[geography_layers] township query failed: {exc}")
        return 0, 0

    feats, leased = [], 0
    acres = sorted(float(r.acres or 0) for r in rows if (r.n or 0) > 0)

    def _colour(ac):
        if not acres or ac <= 0:
            return "#e8e2d6"
        for i, q in enumerate((0.2, 0.45, 0.7, 0.9)):
            if ac <= acres[min(int(len(acres) * q), len(acres) - 1)]:
                return TOWNSHIP_RAMP[i]
        return TOWNSHIP_RAMP[4]

    for r in rows:
        b = _twp_bounds(r.bbox_wkt)
        if not b:
            continue
        s, w, n, e = b
        ac = float(r.acres or 0)
        if (r.n or 0) > 0:
            leased += 1
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Polygon",
                         "coordinates": [[[w, s], [e, s], [e, n],
                                          [w, n], [w, s]]]},
            "properties": {"lab": r.township_label or r.plss_id,
                           "pid": r.plss_id,
                           "n": int(r.n or 0),
                           "ac": int(ac),
                           "_c": _colour(ac)},
        })
    if not feats:
        return 0, 0

    # FEW TOWNSHIPS MEANS WE ARE CLOSE IN. Statewide is 2,888; a drawn box
    # around a field is tens. Below the threshold the layer stops being a
    # choropleth and becomes a grid over the leases -- which is the whole
    # point of clipping to a box. Declared before the layer so the style
    # function can read it; a Python-side flag rather than a zoom listener,
    # because Python already knows the count and never learns the zoom.
    _f.Element(
        "<script>window.DV_TWP_FRAMED = %s;</script>"
        % ("true" if len(feats) <= 250 else "false")
    ).add_to(m.get_root().html)

    _f.GeoJson(
        {"type": "FeatureCollection", "features": feats},
        name=f"▦ Townships ({leased:,} leased of {len(feats):,})",
        show=show,
        # STYLED IN THE BROWSER for the same reason the leases are: one
        # template instead of a style block per feature.
        on_each_feature=_f.JsCode(("""
            function(feature, layer) {
                var p = feature.properties;
                var c = p._c;
                // CLOSE IN, THE TOWNSHIP BECOMES A FRAME, NOT A FILL.
                // Townships render in the default overlay pane and the
                // leases in a custom pane BELOW it, so a 55% fill sits on
                // top of the very thing you zoomed in to see -- "I drew a
                // box and the townships did not expand" is partly this: the
                // leases were under there, smothered.
                //
                // Far out, the fill IS the information: it is a choropleth
                // of leased acreage across 2,888 squares. Clipped or zoomed
                // in, the leases are the information and the township is
                // just the grid line round them. FRAMED is set by the layer
                // when it draws few enough townships to mean "we are close".
                var framed = (typeof DV_TWP_FRAMED !== 'undefined') && DV_TWP_FRAMED;
                var base = framed
                    ? {color: '#7a3a0d', weight: 1.6, opacity: 0.95,
                       fillColor: c, fillOpacity: 0.06}
                    : {color: '#8a7a63', weight: 0.7, opacity: 0.85,
                       fillColor: c, fillOpacity: p.n ? 0.55 : 0.10};
                layer.setStyle(base);
                layer.on('mouseover', function(){
                    layer.setStyle({weight: 2, fillOpacity: 0.72}); });
                layer.on('mouseout', function(){ layer.setStyle(base); });
                layer.bindTooltip('<b>' + p.lab + '</b><br>' +
                    (p.n ? p.n + ' lease(s)<br>' +
                           p.ac.toLocaleString() + ' ac leased'
                         : 'no leases recorded'), {sticky: true});
                layer.bindPopup(
                    '<table style="font-size:11px;border-collapse:collapse">' +
                    '<tr><td style="color:#64748b;padding-right:8px">Township</td>' +
                    '<td><b>' + p.lab + '</b></td></tr>' +
                    '<tr><td style="color:#64748b">PLSS id</td><td>' + p.pid + '</td></tr>' +
                    '<tr><td style="color:#64748b">Leases</td><td>' + p.n + '</td></tr>' +
                    '<tr><td style="color:#64748b">Leased acres</td><td>' +
                    p.ac.toLocaleString() + '</td></tr></table>', {maxWidth: 320});

                // ── CLICK EXPANDS THE TOWNSHIP ──────────────────────────
                // THROUGH THE DRAWING CHANNEL, because the click channels do
                // not work for this layer. Measured, twice: a GeoJSON polygon
                // reports NO popup text to Python (only markers do), and a
                // click on this layer did not change last_object_clicked
                // either -- the log did not grow by one line. Both handlers
                // were written, both were silent.
                //
                // all_drawings DOES arrive. It is how the box tool works, how
                // the ⛶ Use current view control works, and it stays
                // subscribed even under Freeze. So the click builds the
                // township's own rectangle and fires the same draw:created
                // the box tool fires -- and everything downstream is the code
                // that already runs: the 5-point ring test, _clip_box, the
                // clip request, and every layer clipping to it.
                //
                // No new drill, no new channel. The township click becomes a
                // box the map already knows how to honour.
                layer.on('click', function(ev) {
                    var mp = layer._map;
                    if (!mp) { return; }
                    // DECLARED IN THE CLICK, not in on_each_feature: that
                    // runs once per township and would build 2,888 copies of
                    // the same function for a handler most never fire.
                    var LEASE_URL = __LEASE_URL__;
                    var LEASE_ONEACH = __LEASE_ONEACH__;

                    // ── THE GRID STANDS DOWN ONCE THE LEASES ARRIVE ─────
                    // "If both layers are on then the leases and townships
                    // plot together, defeating the purpose of the
                    // townships." The grid is the overview you use to
                    // CHOOSE; the moment it has handed over to the leases
                    // its job is done, and leaving it drawn is the smear
                    // this layer exists to prevent. It also stops taking
                    // the clicks that should reach the leases beneath it.
                    //
                    // ONE FRAME IS KEPT. Removing every township at zoom 12
                    // leaves six polygons floating on blank tiles with no
                    // way to tell WHICH township you opened -- so the one
                    // you clicked stays as an outline. It is a plain
                    // rectangle, non-interactive, so it cannot swallow a
                    // lease click the way the polygon it replaces did.
                    //
                    // ONE-WAY, NOW THAT THE ZOOM RULES ARE GONE. This used
                    // to coexist with a zoom-13 auto-hide and a zoom-10
                    // reset that put the grid back; both were removed on
                    // request. So a grid stood down here stays down for the
                    // life of this render, and the way back is a rerun --
                    // any control change rebuilds the map. Said here because
                    // the next reader will otherwise look for the undo that
                    // the comment above used to promise.
                    function standDownGrid() {
                        var grp = null;
                        mp.eachLayer(function (g) {
                            if (!grp && g !== layer && g.hasLayer &&
                                    g.hasLayer(layer)) { grp = g; }
                        });
                        if (grp && mp.hasLayer(grp)) {
                            mp.removeLayer(grp);
                            mp.__dv_twp_grp = grp;
                        }
                        if (mp.__dv_twp_frame) {
                            try { mp.removeLayer(mp.__dv_twp_frame); }
                            catch (e) {}
                        }
                        mp.__dv_twp_frame = L.rectangle(b, {
                            fill: false, color: '#f59e0b', weight: 2,
                            opacity: 0.9, interactive: false
                        }).addTo(mp);
                    }
                    var b = layer.getBounds();
                    try { L.DomEvent.stopPropagation(ev); } catch (e) {}

                    // ── THE EXPLODE, ENTIRELY IN THE BROWSER ────────────
                    // Python cannot hear this click. Three channels were
                    // tried and each was ruled out with evidence: a polygon
                    // reports no popup text, last_object_clicked never
                    // changes, and firing draw:created makes st_folium's own
                    // onDraw throw on sourceTarget.getPopup.
                    //
                    // None of that matters, because THE BROWSER ALREADY HAS
                    // EVERY LEASE. The served file carries all 24,178
                    // polygons with their geometry -- it was downloaded to
                    // draw them. Expanding a township is therefore a
                    // question about data already in this page: zoom to it,
                    // and let its leases stand out from the rest.
                    //
                    // No rerun, no re-download, no 27 MB round trip, and it
                    // works whether or not Freeze is on -- because nothing
                    // leaves the browser at all.
                    mp.fitBounds(b, {padding: [12, 12], animate: true});
                    window.DV_TWP_FOCUS = b;
                    var inside = 0;
                    mp.eachLayer(function (grp) {
                        if (!grp || !grp.eachLayer) { return; }
                        try {
                            grp.eachLayer(function (c) {
                                var p = c.feature && c.feature.properties;
                                if (!p || p.ln === undefined) { return; }
                                var cb = c.getBounds && c.getBounds();
                                if (!cb) { return; }
                                var hit = b.contains(cb.getCenter());
                                if (hit) { inside++; }
                                // The leases outside do not vanish -- they
                                // fade. A township with nothing around it
                                // looks the same as a broken filter, and
                                // context is what makes the zoom readable.
                                c.setStyle({
                                    opacity:     hit ? 1.0  : 0.10,
                                    fillOpacity: hit ? 0.62 : 0.04,
                                    weight:      hit ? 1.4  : 0.4
                                });
                                if (hit && c.bringToFront) { c.bringToFront(); }
                            });
                        } catch (e) { /* not a lease group */ }
                    });
                    // Leases were ALREADY on the map ("both" mode): they
                    // have just been highlighted, so the grid has done its
                    // job and gets out of their way now rather than waiting
                    // for zoom 13.
                    if (inside > 0) { standDownGrid(); }
                    // Say what happened, on the map, where the click was.
                    // ONE POPUP THAT CAN BE REWRITTEN, because the load below
                    // is asynchronous: the click has to answer immediately
                    // ("loading...") and then again with the result. A second
                    // popup would leave the first one standing and stale.
                    var pop = null;
                    function say(extra) {
                        try {
                            var html =
                                '<div style="font:600 13px system-ui">' + p.lab +
                                '</div><div style="font:12px system-ui">' +
                                p.n + ' lease(s) &middot; ' +
                                p.ac.toLocaleString() + ' ac leased</div>' +
                                '<div style="font:11px system-ui;color:#64748b;' +
                                'margin-top:4px">' + extra + '</div>';
                            if (pop) { pop.setContent(html); }
                            else {
                                // ── NOT OVER THE THING IT DESCRIBES ─────
                                // At the centre it covered the leases it had
                                // just counted: fitBounds zooms so the
                                // township fills the view, so a popup at the
                                // middle sits squarely on the answer.
                                // Reported as "it says 22 leases but where
                                // are the actual leases" -- they were
                                // behind the box.
                                //
                                // Anchored at the NORTH edge instead, where
                                // a Leaflet popup opens upward and clears
                                // the township almost entirely.
                                pop = L.popup({closeButton: true,
                                               autoPan: false})
                                       .setLatLng([b.getNorth(),
                                                   b.getCenter().lng])
                                       .setContent(html).openOn(mp);
                            }
                        } catch (e) {}
                    }
                    say(inside + ' drawn here &middot; zoom out to reset');

                    // ── NOTHING TO EXPAND? FETCH THE LEASES ──────────────
                    // The grid can be drawn with the lease layer off -- the
                    // second screen's "townships" mode does exactly that --
                    // and then this click had nothing to highlight. It zoomed
                    // to an empty rectangle and reported "0 drawn here",
                    // which reads as a broken feature rather than as "you did
                    // not ask for leases".
                    //
                    // Fetching here keeps the bargain the rest of this
                    // handler keeps: NOTHING REACHES PYTHON, so no rerun
                    // rebuilds the map and wipes the highlight the click just
                    // made -- the failure that killed the first version of
                    // this feature. It is the same file the lease layer
                    // serves, so it is usually already in the browser cache,
                    // and it is fetched at most once per page.
                    if (inside === 0 && LEASE_URL) {
                        say('loading leases for this township...');
                        var got = window.__dv_lease_gj
                            ? Promise.resolve(window.__dv_lease_gj)
                            : fetch(LEASE_URL).then(function (r) {
                                  if (!r.ok) {
                                      throw new Error('HTTP ' + r.status);
                                  }
                                  return r.json();
                              }).then(function (gj) {
                                  window.__dv_lease_gj = gj;
                                  return gj;
                              });
                        got.then(function (gj) {
                            // The previous expansion goes when the next one
                            // arrives. Two townships' worth of leases on the
                            // map at once is the smear this layer exists to
                            // avoid.
                            if (mp.__dv_twp_leases) {
                                try { mp.removeLayer(mp.__dv_twp_leases); }
                                catch (e) {}
                                mp.__dv_twp_leases = null;
                            }
                            // THE STAMP, NOT A SECOND GEOMETRY RULE.
                            // The tooltip's count comes from the stamped
                            // plss_id; filtering here by "centre inside the
                            // box" is a DIFFERENT rule that nearly agrees --
                            // 28N 113W read 69 in the tooltip and 70 here,
                            // because a centre in the overlap of two adjacent
                            // township boxes is stamped once but matches both.
                            // Two numbers for one fact, and whichever the
                            // reader trusted less would look like the bug.
                            //
                            // Centre-in-box remains the FALLBACK, for a file
                            // written before the stamp was carried -- near
                            // enough to be useful, and it says so below.
                            var byStamp = (gj.features || []).length > 0 &&
                                (gj.features[0].properties || {})._tw !== undefined;
                            var picked = (gj.features || []).filter(
                                function (f) {
                                    var q = f.properties || {};
                                    if (byStamp) { return q._tw === p.pid; }
                                    if (q._la === undefined ||
                                        q._lo === undefined) { return false; }
                                    return b.contains(L.latLng(q._la, q._lo));
                                });
                            if (!picked.length) {
                                say('no leases fall in this township');
                                return;
                            }
                            var lyr = L.geoJSON(
                                {type: 'FeatureCollection', features: picked},
                                {style: function (f) {
                                     return (f.properties &&
                                             f.properties.style) || {};
                                 },
                                 onEachFeature: LEASE_ONEACH});
                            lyr.addTo(mp);
                            mp.__dv_twp_leases = lyr;
                            // The leases are on the map; the grid steps
                            // aside. Done HERE, not beside the fetch call,
                            // because the fetch is asynchronous -- hiding
                            // the grid before the leases arrive leaves an
                            // empty map for as long as the download takes,
                            // and a blank map is how "it broke" looks.
                            standDownGrid();
                            say(picked.length + ' lease(s) drawn' +
                                (byStamp ? '' : ' (approx)') +
                                ' &middot; zoom out to reset');
                        }).catch(function (e) {
                            // SAID, NOT SWALLOWED: a silent failure here is
                            // indistinguishable from a township that simply
                            // holds no leases.
                            say('could not load leases: ' + e);
                        });
                    }
                    // OUT OF THE CLICK'S CALL STACK, and this is the whole
                    // reason it did not work. Fired inline, streamlit-folium's
                    // onDraw runs while the click event is still live, reads
                    // the click's sourceTarget as if it were a popup-bearing
                    // layer, and throws:
                    //   TypeError: t.sourceTarget.getPopup is not a function
                    //       at onLayerClick ... at onDraw
                    // The exception aborts st_folium's handler, so
                    // all_drawings never updates and NOTHING reaches Python --
                    // silently, because the throw is inside the component.
                    // setTimeout lets the click finish first; onDraw then
                    // sees an ordinary programmatic draw, exactly like the
                    // ⛶ control's, which has always worked.
                    // THE ZOOM NO LONGER RESETS ANYTHING. A zoomend handler
                    // used to clear the focus below zoom 10 -- restoring the
                    // grid, dropping the fetched leases and un-fading the rest.
                    // Removed on request, with the consequence stated rather
                    // than discovered: nothing now puts the grid back inside a
                    // single render. Clicking another township re-frames it,
                    // and ANY control change rebuilds the map, which restores
                    // the grid -- that is the recovery path.

                    // AND IT MUST NOT TELL PYTHON. An earlier version also
                    // fired draw:created here, so that the server would learn
                    // the box and could clip to it as well. That turned out
                    // to DESTROY the very thing it was added beside: the
                    // drawing reaches Python, Streamlit reruns, the map is
                    // rebuilt from scratch, and the highlight -- which lives
                    // only in this page -- is wiped along with it. Measured:
                    // the explode was correct (70 highlighted, 24,108 faded)
                    // and then the rerun replaced the map and left a "name
                    // the shape you drew" panel, so the whole thing read as
                    // "nothing happened".
                    //
                    // A client-side effect and a server rerun cannot share a
                    // gesture. The zoom and the highlight are the feature;
                    // clipping server-side is what ⛶ Use current view and the
                    // box tool are for.
                });
            }""")
        # THE LEASE STYLING IS NOT COPIED, IT IS THE SAME TEMPLATE. An
        # on-demand lease has to look and behave exactly like a drawn one --
        # same colours, same tooltip, same popup -- and a second style block
        # here would be one more list that must agree with another.
        .replace("__LEASE_URL__",
                 _json.dumps(lease_url) if lease_url else "null")
        # THE SAME FILLER THE DRAWN LAYER USES. Spelling the replacements
        # out here is what left __FILT__ unfilled and broke the township
        # click in the browser.
        .replace("__LEASE_ONEACH__", lease_on_each(_lkey))),
    ).add_to(m)

    # THE GRID NO LONGER HIDES ITSELF ON ZOOM. A MacroElement removed
    # the township layer past zoom 13 and re-added it below, so the grid
    # got out of the way when you were close enough to want leases.
    # Removed on request. The grid now stays until it is switched off in
    # the layer control, or until a township click stands it down.
    return len(feats), leased



# ── WETLANDS: THE REGISTRY, NOT A COPY OF IT ───────────────────────────────
# The USFWS National Wetlands Inventory is the registry a land man would
# actually cite -- PEM, PSS, PFO classifications, not a basemap's "green
# bit". Wyoming's download is 1,071 MB; the same data is served live, so for
# LOOKING there is nothing to store and nothing to keep in sync.
#
# Querying is the other half and is deliberately not attempted here: "is this
# lease on a wetland" needs the polygons locally, and that is the gigabyte.
# Display first, because it answers the question that was actually asked.
NWI_SERVICE = ("https://www.fws.gov/wetlandsmapservice/rest/services/"
               "Wetlands/MapServer")
# WMS LIVES UNDER /services/, NOT /rest/services/. That one path segment is
# the whole reason the first attempt concluded there was no WMS.
NWI_WMS = ("https://www.fws.gov/wetlandsmapservice/services/"
           "Wetlands/MapServer/WMSServer")

# THE SERVICE DRAWS NOTHING ABOVE THIS SCALE, by its own configuration
# (minScale 100000 on layer 0). Zoomed out to the state, ticking the layer
# would look broken -- so the layer says so rather than leaving the reader to
# wonder. Web-mercator zoom 11 is roughly 1:144k, 12 roughly 1:72k, so 12 is
# the first zoom that reliably paints.
NWI_MIN_ZOOM = 12


def add_wetlands_layer(m, show=False):
    """USFWS NWI wetlands as a live WMS overlay. Returns the layer name.

    IT IS A WMS AFTER ALL. The first version of this hand-wrote a Leaflet
    tile layer that computed each tile's bbox and called the ArcGIS `export`
    endpoint, because .../rest/services/Wetlands/MapServer/WMSServer 404s.
    That was the wrong URL: ArcGIS serves WMS from /services/, not
    /rest/services/, and the service advertises it plainly --
    supportedExtensions: WMSServer. Asking the service what it supports
    would have settled it before fifteen lines of tile arithmetic.

    So this is folium's own WmsTileLayer now: a standard protocol, no custom
    JavaScript, and it registers itself in the layer control instead of
    hunting for one in window. Verified against the live service --
    GetCapabilities returns WMS 1.3.0 with EPSG:3857 and image/png, and
    GetMap returns real PNGs.

    THE SERVICE DRAWS NOTHING ZOOMED OUT (minScale 100000 on its one layer),
    so the name carries the zoom rather than leaving a blank overlay looking
    broken.

    DISPLAY ONLY. "Which leases sit on wetland" needs the polygons locally,
    and Wyoming's NWI download is 1,071 MB -- a different decision, not a
    bigger version of this one.
    """
    import folium as _f
    _name = "🟩 Wetlands (NWI, zoom 12+)"
    _f.raster_layers.WmsTileLayer(
        url=NWI_WMS,
        layers="0",
        fmt="image/png",
        transparent=True,
        version="1.3.0",
        name=_name,
        overlay=True,
        control=True,
        show=show,
        opacity=0.75,
        attr="Wetlands: USFWS National Wetlands Inventory",
    ).add_to(m)
    return _name
