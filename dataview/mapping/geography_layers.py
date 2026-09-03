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

import os
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
                 popup_fields=None, popup_aliases=None,
                 extra=None, colour_by=None, colours=None):
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
        # ONE STYLE PER FEATURE, ONLY WHEN ASKED. folium folds identical
        # style dicts into a style map, so N points with six colours cost
        # six entries and not N -- checked before this was written, on the
        # assumption that a style_function per point would bloat the
        # document the way a CircleMarker per point does. It does not.
        style_function=((lambda f: {
            "color": colours.get(f["properties"].get(colour_by), color),
            "fillColor": colours.get(f["properties"].get(colour_by), fill),
        }) if (colour_by and colours) else None),
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


# ── WHAT A REFERENCE WELL CAN BE COLOURED BY ──────────────────────────────
# CHOSEN FROM WHAT WYOMING ACTUALLY HOLDS, not from what the schema offers.
# Measured 2 Sep over the whole master and over two Wyoming boxes, because
# the two disagree sharply and only the second is on screen:
#
#                   whole master        Teapot/NPR-3      Natrona County
#   std_well_type   55.8% usable            0.0%              0.0%
#   std_well_status 55.8% usable            0.0%              0.0%
#   spud_date       35.0%                  78.9%             61.5%
#   total_depth     49.9%                  95.4%             96.5%
#   operator_name   51.9%                 100.0% (43)        99.9% (973)
#   field_name      28.8%                 100.0% (7)         99.9% (126)
#
# TYPE AND STATUS ARE KEPT AND THEY ARE EMPTY HERE. They are the natural way
# to colour a well set and they carry data in other states, so removing them
# would be wrong; drawing Wyoming one flat grey and saying nothing would be
# worse. The layer counts how many points actually got a value and says so,
# which is the difference between "no data" and "the colouring is broken".
#
# 93.7% WAS THE WRONG NUMBER. COUNT(col) counts non-NULL, and 1,526,990 rows
# carry the literal string 'UNKNOWN' -- so the column reads as filled while
# saying nothing. Anything that means "not known" is folded into one bucket
# with one grey.
_REF_UNKNOWN = "#9ca3af"
_REF_NOT_KNOWN = ("", "UNKNOWN", "UNK", "NONE", "N/A", "NA", "(NONE)")

_REF_BY = {
    "spud":     ("spud_date",           "Reference wells · spud decade"),
    "depth":    ("total_depth",         "Reference wells · total depth"),
    "type":     ("std_well_type",       "Reference wells · well type"),
    "status":   ("std_well_status",     "Reference wells · well status"),
    "operator": ("operator_name",       "Reference wells · operator"),
    "field":    ("field_name",          "Reference wells · field"),
}

# ORDERED OPTIONS TAKE A RAMP, the rest take hues -- the same rule the lease
# layer follows, and the same ramps, so blue still reads as time and warm as
# extent across both layers. A rainbow over an ordered thing destroys the one
# structure it has.
def _ref_ramp(by):
    """The ramp for an ordered option, or None for an identity one.

    LOOKED UP INSIDE A FUNCTION because the ramps are defined further
    down this file than this block is. As a module-level dict it raised
    NameError at import -- which takes the whole map down, not just the
    colouring.
    """
    return {"spud": _VINTAGE_RAMP, "depth": _SIZE_RAMP}.get(by)


def _ref_band(by, value):
    """The bucket label for one value, or None when it is not known.

    NONE MEANS NOT KNOWN AND IS NEVER A COLOUR IN THE RAMP. A missing spud
    date drawn in the palest step of a time ramp says "the oldest wells are
    here", which is a confident wrong answer; it gets the reserved grey.
    """
    if value is None:
        return None
    if by == "spud":
        y = getattr(value, "year", None)
        if y is None:
            try:
                y = int(str(value)[:4])
            except (TypeError, ValueError):
                return None
        if y < 1950:
            return "1. before 1950"
        if y < 1970:
            return "2. 1950s-60s"
        if y < 1980:
            return "3. 1970s"
        if y < 1990:
            return "4. 1980s"
        if y < 2010:
            return "5. 1990s-2000s"
        return "6. 2010 and later"
    if by == "depth":
        try:
            d = float(value)
        except (TypeError, ValueError):
            return None
        if d <= 0:
            return None
        if d < 2000:
            return "1. under 2,000 ft"
        if d < 4000:
            return "2. 2,000-4,000 ft"
        if d < 6000:
            return "3. 4,000-6,000 ft"
        if d < 8000:
            return "4. 6,000-8,000 ft"
        if d < 12000:
            return "5. 8,000-12,000 ft"
        return "6. 12,000 ft and deeper"
    s = str(value).strip()
    if s.upper() in _REF_NOT_KNOWN:
        return None
    return s


# HOW MANY IDENTITY COLOURS A READER CAN ACTUALLY USE. Colour by operator
# over Natrona County finds 973 of them: 973 legend swatches, 973 entries in
# the colour map inlined into the page, and no reader able to tell any two
# apart. The top few named and the rest in one bucket is the honest form --
# and it is the same call the lease layer makes about owners.
_REF_IDENTITY_MAX = 12
_REF_OTHER = "#64748b"


def _ref_colours(by, bands):
    """{band: colour} for the bands actually present. Grey is not in it."""
    ramp = _ref_ramp(by)
    if ramp:
        # THE RAMP IS INDEXED BY THE BAND'S OWN NUMBER, not by its position
        # in what happened to be drawn. A field holding only 1980s and 2010s
        # wells must not paint them as the two ENDS of the ramp -- the colour
        # has to mean the same decade on every view, or two screenshots of
        # the same map cannot be compared.
        out = {}
        for band in sorted({b for b in bands if b}):
            try:
                k = int(band.split(".")[0]) - 1
            except (TypeError, ValueError):
                k = 0
            out[band] = ramp[min(k * (len(ramp) - 1) // 5, len(ramp) - 1)]
        return out
    # IDENTITY: THE COMMONEST FEW, BY COUNT, and everything else together.
    import collections as _c
    counts = _c.Counter(b for b in bands if b)
    top = [nm for nm, _n in counts.most_common(_REF_IDENTITY_MAX)]
    out = lease_colour_map(sorted(top))
    for nm in counts:
        if nm not in out:
            out[nm] = _REF_OTHER
    return out

# ── THE 4M-ROW MASTER, AS POINTS ──────────────────────────────────────
# Restored 2 Sep. Removed in the dead-code strip because the chip that
# reached it had been deleted; wanted back because the density hexes
# answer "where are wells" and never "which well is this". Measured
# before restoring, against 4,031,052 rows: a bounded fetch is 0.03s over
# Teapot (1,681 wells) and 0.13s over Natrona County (12,879), so the
# layer is affordable wherever a box or a place has set bounds.

# WHICH MASTER THE MAP READS. THERE IS NOW ONLY ONE (3 Sep).
#
# well_master_gold was DROPPED. It was loaded by hand over months and its keys
# were wrong in ways a row count could never show: all 809 Washington wells
# were keyed into California's number space (WA writes its state code 046, so
# its API is eleven digits and the first ten shift every digit), and 37,318
# Kansas wells into Georgia's. Michigan was missing entirely -- its 92,551
# wells were absent while 5,465 MISSISSIPPI wells sat under Michigan's label.
#
# well_master_public_v2 replaces it: 3,140,361 wells across 19 states, built
# from each agency's own file by tools/build_public_master.py, every state
# reconciled against its source before it counted, zero duplicate keys, and
# every key carrying the API state code of the state that issued it.
#
# IT IS SMALLER, AND THAT IS EXPECTED. Eight agencies whose terms do not yet
# permit a derived aggregate are not in it -- Illinois, Oklahoma, Ohio,
# Wyoming, Kentucky, Montana, Nebraska, Alaska. Letters are out to all eight;
# each reply is one dv_source_licence update and a rebuild from files already
# on disk. dataview.dv_source_licence decides what is in, and
# build/source_manifest.csv is the evidence for each state.
#
# DW_REF_MASTER still overrides this, but nothing needs it now: the density
# views (v_well_density_r4..r7, v_well_master_arm) were repointed at the same
# table, so the points and the hexes agree by default rather than by
# remembering to set a variable.
REFERENCE_MASTER = os.environ.get(
    "DW_REF_MASTER", "WELL_REF.well_ref.well_master_public_v2")

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

# AND A CEILING ON THE BOUNDED PATH, which "the box is the only limit" left
# without one. That was right for every box a person draws and wrong for the
# one the app draws itself: the USA place sets bounds to the lower 48, which
# is a perfectly valid box holding ~3.9M wells, so the uncapped path fetched
# all of them with eleven columns each and wrote half a gigabyte of GeoJSON.
# Twice. And not one of those wells would have been drawn -- that extent is
# zoom 4, below REFWELL_MIN_ZOOM.
#
# 300,000 CLEARS A STATE AND STOPS A CONTINENT. Wyoming's box is 210,337
# wells, 58 MB of file, ~10s -- measured, and it works. The lower 48 is an
# order of magnitude past that. Above the ceiling the layer does what it
# already does when it cannot draw everything: a spread sample that says so
# in its own name, rather than a fetch nobody can use.
BOUNDED_MAX = 300000




def _add_refwell_legend(m, colours, title, known, total):
    """Swatches for the bands the layer JUST assigned, plus the shortfall.

    IT TAKES THE COLOURS RATHER THAN RECOMPUTING THEM, for the reason the
    lease legend does: a legend that derives its own swatches is a second
    implementation of the colour rule, and the two drift the first time one
    changes. A legend that disagrees with the map is worse than none, because
    it is believed.

    AND IT SAYS HOW MANY POINTS ARE UNCOLOURED. Colour by well type over
    Wyoming paints every one of 1,681 Teapot wells the same grey, because the
    master holds no type for any of them. Without the count that reads as a
    broken layer; with it, it reads as what it is -- a column this data does
    not fill. "n of m coloured" is the whole difference.
    """
    import folium as _f
    # Band labels are DATA -- operator and field names come
    # from the master, so they are escaped, not trusted.
    from html import escape as _html_escape
    if not colours and not total:
        return
    # ONE ROW PER COLOUR, NOT PER VALUE. Colouring by operator maps 973
    # names onto twelve hues plus one "other", and a legend that listed
    # every name would be longer than the map is tall.
    named = sorted((b, c) for b, c in colours.items()
                   if c != _REF_OTHER)
    n_other = sum(1 for c in colours.values() if c == _REF_OTHER)
    rows = "".join(
        "<div style='white-space:nowrap'><i style='background:%s;"
        "width:10px;height:10px;display:inline-block;border-radius:50%%;"
        "margin-right:6px;border:1px solid #33415580'></i>%s</div>"
        % (c, _html_escape(b.split(". ", 1)[-1]))
        for b, c in named)
    if n_other:
        rows += ("<div style='white-space:nowrap'><i style='background:"
                 "%s;width:10px;height:10px;display:inline-block;"
                 "border-radius:50%%;margin-right:6px;"
                 "border:1px solid #33415580'></i>%s other</div>"
                 % (_REF_OTHER, "{:,}".format(n_other)))
    miss = total - known
    if miss > 0:
        rows += ("<div style='white-space:nowrap'><i style='background:%s;"
                 "width:10px;height:10px;display:inline-block;"
                 "border-radius:50%%;margin-right:6px;"
                 "border:1px solid #33415580'></i>not known</div>"
                 % _REF_UNKNOWN)
    foot = ("<div style='margin-top:4px;opacity:.7'>%s of %s coloured</div>"
            % ("{:,}".format(known), "{:,}".format(total)))
    html = (
        "<details id='wm-refwell-legend' style='position:absolute;z-index:9999;"
        "bottom:64px;left:10px;background:#ffffffee;border:1px solid #cbd5e1;"
        "border-radius:6px;padding:5px 8px;font:500 11px system-ui;"
        "color:#0f172a;max-height:40vh;overflow:auto'>"
        "<summary style='cursor:pointer;font-weight:600'>%s</summary>%s%s"
        "</details>"
        "<script>(function(){var d=document.getElementById("
        "'wm-refwell-legend'); if(!d){return;} "
        "if(sessionStorage.getItem('dv_refleg_open')==='1'){"
        "d.setAttribute('open','');} "
        "d.addEventListener('toggle',function(){"
        "sessionStorage.setItem('dv_refleg_open', d.open?'1':'0');});})();"
        "</script>" % (_html_escape(title), rows, foot))
    m.get_root().html.add_child(_f.Element(html))



REFWELLS_GEOJSON_PREFIX = "dv_refwells_"

# HOW MANY OF THESE FILES TO KEEP. Unlike the towns, a reference-well file is
# not one artifact: it is one per (box, colouring), so browsing a few fields
# writes a few files. Keeping the last handful means going back to a box you
# just looked at costs nothing, while static/ cannot grow without bound.
REFWELLS_KEEP = 8

# BELOW THIS THE DOTS ARE NOISE, NOT DATA -- but the floor has to clear
# the zoom people actually work at. Set to 13 first, which was wrong by
# four levels: choosing a county fits the map to its extent, and Natrona
# across a ~980px map is zoom 9, so the layer drew all 10,452 wells and
# painted none of them. A whole state is 6-7 and a field is 12-13.
#
# 8 IS PERRY'S NUMBER, and it is a deliberate trade: a state view now
# paints too, where 210,337 dots are a wash rather than an answer. The
# H3 density layer is what answers "where are wells" at that scale. The
# floor exists to stop the layer being drawn at a zoom where it cannot
# be read; where exactly that line falls is a judgement, so it is one
# constant and not a rule spread through the drawing code.
REFWELL_MIN_ZOOM = 8


# STYLE, TOOLTIP AND POPUP ALL IN THE BROWSER, because the geometry is served
# by URL and folium can only run a Python style_function over data it EMBEDS.
# Same constraint the lease layer meets the same way -- see _LEASE_ON_EACH.
#
# THE POPUP LABEL IS A SENTINEL, NOT DECORATION. page_well_map identifies a
# loaded well by digging a 14-digit UWI out of the popup TEXT, and the
# master's headers carry uwi14 too -- so without a label to tell them apart a
# reference header is sent to the scout builder for a well that may not be in
# dv_well at all. FEDWELL_POPUP_LABEL is what the handler checks, so it has to
# survive into this template.
_REFWELL_ON_EACH = """
function(feature, layer) {
    var C = __COLOURS__;
    var UNK = "__UNKNOWN__";
    var p = feature.properties || {};
    var c = C[p._cb] || UNK;
    layer.setStyle({color: c, fillColor: c, fillOpacity: 0.75, weight: 1,
                    className: 'dv-refwell'});
    layer.bindTooltip(p.nm || '', {sticky: true});
    if (!__DETAIL__) { return; }
    var rows = [["UWI", p.uwi], ["Operator", p.op],
                ["County", p.cty], ["State", p.st], ["Type", p.ty],
                ["Status", p.sta], ["TD", p.td], ["Spud", p.spud],
                ["__BYLABEL__", p._cb]];
    // THE SENTINEL ROW IS NEVER SKIPPED. Every other row drops out when
    // the master leaves it NULL, and 105 of the 14,091 wells in one box
    // have no well_name -- so the label row vanished along with the name,
    // and those clicks were read as dv_well clicks and sent to the scout
    // builder for a header that is not in dv_well. The label is what
    // page_well_map looks for; it cannot depend on a column being filled.
    var h = '<table style="font-size:11px;border-collapse:collapse">'
          + '<tr><td style="color:#64748b;padding-right:8px">__LABEL__'
          + '</td><td>' + (p.nm || '(unnamed)') + '</td></tr>';
    for (var i = 0; i < rows.length; i++) {
        if (rows[i][1] === undefined || rows[i][1] === null
                || rows[i][1] === '') { continue; }
        h += '<tr><td style="color:#64748b;padding-right:8px">' + rows[i][0]
           + '</td><td>' + rows[i][1] + '</td></tr>';
    }
    layer.bindPopup(h + '</table>', {maxWidth: 320});
}
"""


def _refwell_zoom_gate(m):
    """Hand the map from the density hexes to the reference points at Z.

    ONE THRESHOLD, BOTH DIRECTIONS. Below Z the hexes answer "where are
    wells" over four million rows and the points are a smear; above it
    the points answer "which well is this" and a 370 km hex is a sheet
    of colour over the wells it was pointing at. Driving both from one
    number means there is never a zoom showing both or neither.

    IT ONLY INSTALLS WHEN THE REFERENCE LAYER DRAWS, which is what makes
    hiding the hexes safe: with no points to hand over to, there is
    nothing to hand over, and the hexes keep the map to themselves.

    A CLASS ON THE CONTAINER, NOT setStyle, for the reason the NAIP dimming
    uses one: the layer restyles itself on every hover, so anything written
    with setStyle is undone the first time the pointer crosses a dot. A rule
    outranks an SVG presentation attribute and keeps outranking it.

    PYTHON NEVER LEARNS THE ZOOM -- the browser owns it -- so the gate is a
    zoomend listener reading a value the map itself set, not a guess made
    here about what the viewport is showing.
    """
    if getattr(m, "_dv_refwell_gate", False):
        return
    try:
        m._dv_refwell_gate = True
    except Exception:
        pass
    from branca.element import Template as _Tpl, MacroElement as _ME
    _css = _ME()
    _css._template = _Tpl(
        "{% macro html(this, kwargs) %}<style>"
        ".dv-refwell-hide .dv-refwell{display:none}"
        ".dv-hex-hide .dv-hex{display:none}"
        "</style>{% endmacro %}")
    m.get_root().add_child(_css)
    _gate = _ME()
    _gate._template = _Tpl(
        "{% macro script(this, kwargs) %}"
        "(function(){"
        " var mp = {{ this._parent.get_name() }};"
        " if (!mp || mp._dvRefZoom) { return; }"
        " mp._dvRefZoom = true;"
        " var Z = " + str(REFWELL_MIN_ZOOM) + ";"
        " function sync(){"
        " var el = mp.getContainer();"
        " if (!el) { return; }"
        " if (mp.getZoom() < Z) {"
        " L.DomUtil.addClass(el, 'dv-refwell-hide');"
        " L.DomUtil.removeClass(el, 'dv-hex-hide'); }"
        " else {"
        " L.DomUtil.removeClass(el, 'dv-refwell-hide');"
        " L.DomUtil.addClass(el, 'dv-hex-hide'); } }"
        " mp.on('zoomend', sync);"
        " sync();"
        "})();"
        "{% endmacro %}")
    m.add_child(_gate)



def ensure_refwells_geojson(feats, key, static_dir=None):
    """(path, url) for one reference-well set, written once per (box, colour).

    THE FILENAME IS THE FRESHNESS RULE. The key is a digest of the bounds, the
    colouring and the row count, so a file that exists is by construction the
    file for that request -- there is no sidecar to disagree with, and no
    stamp that can outlive what it describes.

    WHY SERVE IT AT ALL. Measured 2 Sep: a box round Wyoming holds 210,337
    wells and inlines to a 72.8 MB document, which streamlit-folium injects
    TWICE. Served as a file, the document carries a URL and the browser
    fetches the geometry once. Same fix that took the towns from 2,756 KB to
    75 KB, for the same reason.
    """
    import json as _json
    import os as _os
    if static_dir is None:
        static_dir = _os.path.join(_os.path.dirname(_os.path.dirname(
            _os.path.dirname(_os.path.abspath(__file__)))), "static")
    name = "%s%s.geojson" % (REFWELLS_GEOJSON_PREFIX, key)
    path = _os.path.join(static_dir, name)
    url = "/app/static/" + name
    if _os.path.exists(path) and _os.path.getsize(path) > 0:
        return path, url
    try:
        _os.makedirs(static_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as _fh:
            _json.dump({"type": "FeatureCollection", "features": feats}, _fh)
    except Exception as exc:
        print("[geography_layers] refwells geojson not written: %s"
              % str(exc)[:120])
        return None, None
    # OLDEST FIRST, AND NEVER THE ONE JUST WRITTEN. A cleanup that can delete
    # the file this render is about to serve turns a tidy-up into a blank map.
    try:
        olds = sorted(
            (_os.path.join(static_dir, f) for f in _os.listdir(static_dir)
             if f.startswith(REFWELLS_GEOJSON_PREFIX)
             and f.endswith(".geojson")),
            key=_os.path.getmtime)
        for old in olds[:-REFWELLS_KEEP]:
            if _os.path.abspath(old) != _os.path.abspath(path):
                _os.remove(old)
    except Exception as exc:
        print("[geography_layers] refwells cleanup skipped: %s"
              % str(exc)[:120])
    return path, url



def add_reference_wells(m, engine, bounds=None, limit: int = 50000,
                        show: bool = True, by: str = "spud"):
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
    # ── A BOX THAT DID NOT PARSE IS NOT A BOX ────────────────────────────
    # THIS BRANCH DECIDED TWO THINGS AND ONLY LOOKED AT ONE. Everything
    # downstream keys on "bounds" being truthy: the uncapped fetch, the
    # detail columns, the popup. The unpack sat inside a try whose except
    # fell back to "has coordinates" -- so a bounds object that was present
    # but malformed produced the UNBOUNDED filter on the BOUNDED path, and
    # asked for all 4,031,052 wells with eleven columns each.
    #
    # It ran. Measured 2 Sep: 3,064,871 features and 805 MB of GeoJSON on
    # disk and still climbing, full of Florida wells, while the log said
    # "drew 10452" from the render before it. The cap and the sample exist
    # precisely to stop that, and this path had stepped around both.
    #
    # So the parse happens FIRST and its RESULT is the gate. A box that does
    # not parse is treated as no box at all -- capped, sampled, honest --
    # rather than as a box covering the earth.
    _bx = None
    if bounds:
        try:
            (s, w), (n, e) = bounds
            s, w, n, e = float(s), float(w), float(n), float(e)
            if (s == s and w == w and n == n and e == e   # not NaN
                    and -90 <= s < n <= 90 and -180 <= w < e <= 180):
                _bx = (s, w, n, e)
            else:
                print("[geography_layers] reference wells: bounds out of "
                      "range %r -- treating as unbounded" % (bounds,))
        except Exception as exc:
            print("[geography_layers] reference wells: bounds did not parse "
                  "(%s) -- treating as unbounded" % str(exc)[:80])
    bounds = _bx and ((_bx[0], _bx[1]), (_bx[2], _bx[3]))
    if _bx:
        where = ["surface_latitude BETWEEN :s AND :n",
                 "surface_longitude BETWEEN :w AND :e"]
        params = {"s": _bx[0], "n": _bx[2], "w": _bx[1], "e": _bx[3]}
    else:
        where = ["surface_latitude IS NOT NULL",
                 "surface_longitude IS NOT NULL"]
    clause = " AND ".join(where)
    # THE COLOUR COLUMN RIDES ALONG ON WHICHEVER QUERY RUNS, so the
    # sampled path is coloured too. One more column on a scan that is
    # already happening, not a second query.
    _cb_col, _cb_title = _REF_BY.get(by, _REF_BY["spud"])
    _light = _REF_COLS_LIGHT + ", " + _cb_col + " AS _cb"
    _full = _REF_COLS + ", " + _cb_col + " AS _cb"
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
    detail = False
    # ── A BOX IS THE LIMIT. NOTHING ELSE IS. ──────────────────────────────
    # "The number of wells to post for reference wells should only be limited
    # by the bounding box." So a bounded fetch has no TOP, no sample and no
    # popup threshold: it returns every well inside the box, with the detail
    # columns, and every one of them is clickable.
    #
    # THE CAP AND THE SAMPLE EXIST FOR THE UNBOUNDED CASE, which is a
    # different question. With no box the scope is 4,031,052 wells across the
    # continent, a set no browser can draw and no reader can click; there the
    # spread sample stands in for "too many to draw" and says so in its own
    # layer name. Drawing a box is what turns the layer from an impression
    # into an answer, and that is exactly when the limits get out of the way.
    #
    # WHAT A BIG BOX COSTS, measured 2 Sep: 1,681 wells over Teapot is 0.15s
    # and 0.5 MB of document; 12,879 over Natrona County is 0.61s and 3.6 MB,
    # and streamlit-folium injects that twice. A box drawn round several
    # counties will be slow -- deliberately, because it is what was asked for
    # and a silent truncation reads as completeness.
    try:
        with engine.connect() as con:
            if bounds:
                # PROBE FIRST, WITH THE LIGHT COLUMNS. One row past the
                # ceiling is all we need to know, and asking for it in
                # three columns rather than twelve is what makes the
                # check affordable on a box that turns out to be huge.
                rows = con.execute(text(
                    f"SELECT TOP {BOUNDED_MAX + 1} {_light} "
                    f"FROM {REFERENCE_MASTER} WHERE {clause}"),
                    params).fetchall()
                if len(rows) > BOUNDED_MAX:
                    # TOO BIG TO BE A BOX. Fall through to the sampled
                    # path, which caps, spreads and says "sample" in the
                    # layer name.
                    print("[geography_layers] reference wells: box holds "
                          "more than %s wells -- sampling instead"
                          % "{:,}".format(BOUNDED_MAX))
                    bounds = None
                else:
                    rows = con.execute(text(
                        f"SELECT {_full} FROM {REFERENCE_MASTER} "
                        f"WHERE {clause}"), params).fetchall()
                    detail = bool(rows)
            if not bounds:
                # limit+1 tells us "capped" without a COUNT, which measured
                # 5.90s over a 2M-row bbox for a number that only ever reads
                # "lots".
                rows = con.execute(text(
                    f"SELECT TOP {lim + 1} {_light} "
                    f"FROM {REFERENCE_MASTER} WHERE {clause}"),
                    params).fetchall()
                if len(rows) > lim:
                    # NAME THE TABLE WE ACTUALLY READ. This was hard-coded to
                    # 'well_master_gold'; when that table was dropped the
                    # count came back 0 and k collapsed to 2, thinning every
                    # view differently with nothing on screen to say why.
                    est = con.execute(text(
                        "SELECT SUM(p.rows) FROM WELL_REF.sys.partitions p "
                        "JOIN WELL_REF.sys.objects o "
                        "ON o.object_id = p.object_id "
                        "WHERE o.name = :t AND p.index_id IN (0,1)"),
                        {"t": REFERENCE_MASTER.split(".")[-1]}).scalar() or 0
                    # k FROM THE SCOPE, NOT THE MASTER -- and the first sample
                    # is what measures the scope. Deriving k from the master's
                    # 3.9M rows thins every view by the same 79x, so a bbox
                    # holding ~200k wells drew 2,613 of them: correct in
                    # shape, 19x sparser than the cap allows, and
                    # indistinguishable to the eye from "hardly any wells".
                    k = max(2, int(est // max(lim, 1)) + 1)
                    sampled = True
                    rows = con.execute(text(
                        f"SELECT TOP {lim} {_light} "
                        f"FROM {REFERENCE_MASTER} "
                        f"WHERE {clause} AND ABS(CHECKSUM(uwi14)) % {k} = 0"),
                        params).fetchall()
                    if rows:
                        scope_est = len(rows) * k
                        k2 = max(2, int(scope_est // max(lim, 1)) + 1)
                        # Only worth a second pass if it changes the picture.
                        if k2 < k // 2:
                            q2 = (f"SELECT TOP {lim} {_light} "
                                  f"FROM {REFERENCE_MASTER} WHERE {clause} "
                                  f"AND ABS(CHECKSUM(uwi14)) % {k2} = 0")
                            rows = con.execute(text(q2), params).fetchall()
                elif rows:
                    # Under the cap even unbounded: it can be clicked, so it
                    # gets the detail columns and the popup.
                    rows = con.execute(text(
                        f"SELECT {_full} FROM {REFERENCE_MASTER} "
                        f"WHERE {clause}"), params).fetchall()
                    detail = bool(rows)
    except Exception as exc:
        print(f"[geography_layers] reference wells query failed: {exc}")
        return 0, 0
    if not rows:
        return 0, 0


    in_scope = None if sampled else len(rows)
    # THE COLOUR COLUMN IS ALWAYS LAST, whichever query ran, so one index
    # serves both row shapes and neither has to be unpacked by name.
    bands = [_ref_band(by, r[-1]) for r in rows]
    colours = _ref_colours(by, bands)
    known = sum(1 for _b in bands if _b)
    label = ("\u26ab Reference wells (%s%s)"
             % ("{:,}".format(len(rows)), " sample" if sampled else ""))

    # ── THE POINTS GO IN A FILE, NOT IN THE DOCUMENT ──────────────────────
    # A bounded set is now uncapped, so it can be large: 210,337 wells over
    # Wyoming inlined to 72.8 MB, injected twice by streamlit-folium. Served
    # by URL the document carries a link and the browser fetches the geometry
    # once and caches it -- the towns fix, applied to the layer that needed it
    # more.
    feats = []
    for _i, r in enumerate(rows):
        try:
            la, lo = float(r[1]), float(r[2])
        except (TypeError, ValueError):
            continue
        props = {"nm": "" if r[0] is None else str(r[0]),
                 "_cb": bands[_i] or "not known"}
        if detail:
            for _k, _v in zip(("uwi", "op", "cty", "st", "ty", "sta", "td",
                               "spud"), r[3:11]):
                if _v is not None and str(_v) != "":
                    props[_k] = str(_v)
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point",
                                   "coordinates": [round(lo, 5),
                                                   round(la, 5)]}})
    if not feats:
        return 0, in_scope

    import hashlib as _hl
    import json as _js
    _key = _hl.sha1(repr((
        None if not bounds else [round(float(x), 5)
                                 for x in (bounds[0][0], bounds[0][1],
                                           bounds[1][0], bounds[1][1])],
        by, detail, len(feats))).encode("utf-8")).hexdigest()[:16]
    _path, _url = ensure_refwells_geojson(feats, _key)
    _oneach = (_REFWELL_ON_EACH
               .replace("__COLOURS__", _js.dumps(colours))
               .replace("__UNKNOWN__", _REF_UNKNOWN)
               .replace("__DETAIL__", "true" if detail else "false")
               .replace("__LABEL__", FEDWELL_POPUP_LABEL)
               .replace("__BYLABEL__",
                        _cb_title.split("\u00b7")[-1].strip().title()))
    if "__" in _oneach.replace("__proto__", ""):
        # CAUGHT HERE, NOT IN THE BROWSER. An unfilled __TOKEN__ is
        # valid-looking JavaScript right up until it runs -- the same trap
        # lease_on_each guards, which shipped a literal __FILT__ and died on
        # the first township click.
        _left = [t for t in _oneach.split("__")[1::2]]
        print("[geography_layers] refwell template placeholder(s) unfilled: %s"
              % ", ".join(sorted(set(_left)))[:120])
    if _path:
        # SMALLER WHEN SAMPLED: no popup to hit, and 48,000 dots at radius 5
        # is a smear, not a map.
        _gj = _f.GeoJson(
            _path, embed=False, name=label, show=show,
            marker=_f.CircleMarker(radius=3 if detail else 1.5, weight=1,
                                   fill=True, fill_opacity=0.75),
            on_each_feature=_f.JsCode(_oneach))
        # The link folium emits must be the BROWSER's, not ours.
        _gj.embed_link = _url
        _gj.add_to(m)
        _refwell_zoom_gate(m)
        n_drawn = len(feats)
    else:
        # EMBEDDED WHEN THE FILE CANNOT BE WRITTEN. A read-only static/ should
        # cost payload, not the layer.
        _fl = ["uwi", "operator", "county", "state", "type", "status", "td",
               "spud", "_cb"]
        n_drawn = points_layer(
            m, ((r[1], r[2], r[0], r[3], r[4], r[5], r[6], r[7], r[8], r[9],
                 r[10], bands[_i] or "not known")
                for _i, r in enumerate(rows)) if detail else
               ((r[1], r[2], r[0], bands[_i] or "not known")
                for _i, r in enumerate(rows)),
            name=label, color="#1d4ed8", fill="#60a5fa",
            radius=5 if detail else 2.5, show=show, opacity=0.7,
            extra=_fl if detail else ["_cb"],
            popup_fields=(["nm"] + _fl) if detail else None,
            popup_aliases=([FEDWELL_POPUP_LABEL, "UWI", "Operator", "County",
                            "State", "Type", "Status", "TD", "Spud",
                            _cb_title.split("\u00b7")[-1].strip().title()]
                           if detail else None),
            colour_by="_cb", colours=colours)

    _add_refwell_legend(m, colours, _cb_title, known, len(rows))

    return n_drawn, in_scope


# A 1x1 transparent PNG. Stands in while folium builds an overlay whose real
# image is served by URL -- see add_image_layer for why the URL cannot be
# passed to the constructor.
_BLANK_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAf"
              "FcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


def add_image_layer(m, layer: dict, say=None) -> int:
    """Lay a registered IMAGE layer on the bounds stored with it.

    HERE RATHER THAN IN page_well_map SO EVERY MAP GETS IT. A hypsometric
    fill is one picture, and the lease screen draws onto the same folium map
    today -- but if it ever gets its own, the alternative is a second copy of
    this branch and two renderers that must agree about what an image layer
    is. This file's header already records what that costs: the paste-me
    snippet that drifted from its caller and silently blanked a whole pill.

    The bbox columns are the registry's own, so an image layer needs no new
    table and appears in the same Show grid as everything else.

    Returns 1 if it drew, 0 if it could not -- the same "degrade to nothing"
    contract as the other layers here, and no streamlit import.
    """
    import os

    _note = say or (lambda _m: None)
    png = (layer or {}).get("file_path") or ""
    name = (layer or {}).get("layer_name") or "Image"
    if not png or not os.path.exists(png):
        _note("[map] %s: image missing at %s" % (name, png))
        return 0
    try:
        bounds = [[float(layer["bbox_min_lat"]), float(layer["bbox_min_lon"])],
                  [float(layer["bbox_max_lat"]), float(layer["bbox_max_lon"])]]
    except (TypeError, ValueError, KeyError):
        _note("[map] %s: no usable bounds on the layer row" % name)
        return 0
    try:
        opacity = float(layer.get("style_opacity") or 0.75)
    except (TypeError, ValueError):
        opacity = 0.75
    # A URL WHEN THE FILE IS SERVED, A PATH ONLY WHEN IT IS NOT. folium
    # base64-embeds a local path into the map HTML, which is fine for the
    # 0.7 MB Teapot fill and ruinous for a statewide DEM -- tens of
    # megabytes into every render, the exact cost the lease geojson was
    # moved out of the payload to avoid. Streamlit serves static/ at
    # /app/static/, and the browser then caches the image across renders.
    src = png
    _parts = os.path.normpath(png).split(os.sep)
    if "static" in _parts:
        src = "/app/static/" + "/".join(_parts[_parts.index("static") + 1:])

    from folium.raster_layers import ImageOverlay
    # BELOW EVERYTHING ELSE. This is ground: wells, seismic, leases and the
    # contours it came from all have to sit on top of it, and folium adds in
    # call order, so the overlay asks for a low z-index rather than relying
    # on being added first.
    _ov = ImageOverlay(image=(png if src == png else _BLANK_PNG),
                       bounds=bounds, opacity=opacity,
                       name=name, overlay=True, control=True, zindex=1)
    if src != png:
        # SET THE URL AFTER CONSTRUCTION, because folium's image_to_url only
        # treats a string as a URL when urlparse finds a SCHEME -- so
        # "/app/static/x.png" is read as a file path, and the render died
        # with FileNotFoundError on a file that exists, under a name the
        # browser (not Python) is meant to resolve. An absolute
        # http://localhost:8501/... would satisfy folium and then break the
        # moment the app is reached from anywhere else, so the relative URL
        # is the correct one and this is how it gets past the check. The
        # placeholder above is a 1x1 transparent pixel, never fetched.
        _ov.url = src
    _ov.add_to(m)
    _note("[map] %s: image overlay on %s (%s)"
          % (name, bounds, "served" if src != png else "embedded"))
    return 1


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
        "<details id='wm-lease-legend' style='position:fixed;"
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
        "try{if(sessionStorage.getItem('dv_lease_legend_open')==='1')"
        "{d.setAttribute('open','');}}catch(e){}"
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
    "elevation": ("elevation_ft",   "Unknown elevation"),
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

# Elevation is sequential and gets the TERRAIN family, so a lease reads
# against the hypsometric fill under it rather than fighting it: green low
# through olive and tan to brown high.
#
# DARK IS HIGHER, which is the opposite of the terrain overlay, where the
# summit is pale. Not an oversight: the overlay is an opaque surface where
# pale peaks are the convention, and these are FILLS at 0.32 over a light
# basemap, where the pale end of any ramp simply disappears -- the same
# reason the vintage ramp starts mid-light. Legibility wins over matching a
# convention that cannot be seen.
_ELEV_RAMP = [
    "#3f6b34", "#6f8f45", "#a3974f", "#bb8a4a", "#9c6b3f", "#78482b",
]

# Round thousands, because that is how elevation is spoken about here, and
# the Wyoming lease set sits almost entirely between 4,000 and 8,000 ft:
# 5.1% below 4,000, then 37.5 / 22.6 / 21.1 / 12.4% across the four
# thousand-foot bands, and 1.4% above 8,000. Quantiles would split the
# 4,000s into boundaries nobody could name.
_ELEV_SQL = ("CASE WHEN {c} IS NULL THEN NULL"
             " WHEN {c} <  4000 THEN '1. under 4,000 ft'"
             " WHEN {c} <  5000 THEN '2. 4,000-5,000 ft'"
             " WHEN {c} <  6000 THEN '3. 5,000-6,000 ft'"
             " WHEN {c} <  7000 THEN '4. 6,000-7,000 ft'"
             " WHEN {c} <  8000 THEN '5. 7,000-8,000 ft'"
             " ELSE '6. 8,000 ft and above' END")

# Which options are ORDERED. A hue picked by CRC is right for identity and
# wrong for a quantity, so these take a ramp and everything else does not.
_SEQUENTIAL = {"vintage": _VINTAGE_RAMP, "size": _SIZE_RAMP,
               "elevation": _ELEV_RAMP}

# ── RESTORED 1 Sept 2026 ──────────────────────────────────────────────────
# These three were deleted by accident. Stripping the unused layer functions
# removed everything from each `def` to the next one, and module constants
# living BETWEEN two functions went with them -- so the lease legend and the
# owner colours lost their definitions while the file still parsed, still
# kept its line endings, and still imported. The NameError only fires when a
# lease legend is actually drawn.
#
# The check that would have caught it is the one this codebase already
# names: verify by CONTENT, not by size or syntax. Compare the set of
# module-level names before and after, and anything that disappears has to
# be either intended or referenced by nothing.
_BY_TITLE = {"owner": "Lease owner", "producing": "Lease · producing",
             "status": "Lease status", "vintage": "Lease · effective decade",
             "size": "Lease · size", "elevation": "Lease · elevation"}

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
    # QUALIFIED WITH THE ALIAS, because colouring by elevation joins a second
    # table. Bare column names are fine against one table and ambiguous the
    # moment there are two -- dv_land_tract_geom carries county and source of
    # its own -- and that failure would be a SQL error inside the try/except
    # below, which returns 0 and reads as "the lease layer is broken".
    _c = lambda n: ("t." + n) if n in have else "NULL"  # noqa: E731
    _col, _unknown = LEASE_COLOUR_BY.get(by) or LEASE_COLOUR_BY["owner"]
    # Fall back rather than fail when the column is not there: a map that
    # draws in one colour beats a layer that returns 0 and reads as broken.
    # ELEVATION IS ON THE OTHER TABLE. dv_land_tract does not carry it;
    # dv_land_tract_geom does, stamped by tools/stamp_elevation.py, and the
    # lease FILTERS already read it through this same join. Checked at
    # runtime like every other optional column here, so a database where
    # that tool has never run falls back to owner instead of failing.
    _elev_join = ""
    if by == "elevation":
        try:
            with engine.connect() as _con0:
                _has_elev = _con0.execute(text(
                    "SELECT CASE WHEN OBJECT_ID('dataview.dv_land_tract_geom')"
                    " IS NOT NULL AND COL_LENGTH('dataview.dv_land_tract_geom',"
                    " 'elevation_ft') IS NOT NULL THEN 1 ELSE 0 END")).scalar()
        except Exception:
            _has_elev = 0
        if _has_elev:
            _elev_join = ("LEFT JOIN dataview.dv_land_tract_geom tg "
                          "ON tg.tract_id = t.land_tract_id")
        else:
            by = "owner"

    if by == "vintage" and "effective_date" in have:
        _own_sql = ("CASE WHEN effective_date IS NULL THEN NULL ELSE "
                    "CAST((YEAR(effective_date)/10)*10 AS varchar(4)) + 's' END")
    elif by == "size" and "area_km2" in have:
        _own_sql = _SIZE_SQL
    elif by == "elevation" and _elev_join:
        _own_sql = _ELEV_SQL.format(c="tg.elevation_ft")
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
                       t.geog.STAsText()      AS wkt
                  FROM dataview.dv_land_tract t
                       {_elev_join}
                 WHERE t.geog IS NOT NULL
                   AND ISNULL(t.active_ind, 'Y') = 'Y'
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
                    "dist_hwy_km", "near_hwy",
                    "wetland_pct", "wetland_acres", "wetland_type"):
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
                   {_extra['wetland_pct']}   AS wpct,
                   {_extra['wetland_acres']} AS wac,
                   {_extra['wetland_type']}  AS wty,
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
            # ZERO AND NULL ARE DIFFERENT FACTS. 0 means measured and none
            # found; null means never measured. A filter that treats them
            # alike reports 1,123 wetland-free leases as unknown, or worse.
            "_wp": (round(float(r.wpct), 3) if r.wpct is not None else None),
            "_wt": (_t(r.wty) or None),
            "wet": (("%s%% wetland (%s ac)%s"
                     % (round(float(r.wpct), 1),
                        format(int(float(r.wac or 0)), ","),
                        (" - " + _t(r.wty)) if r.wty else ""))
                    if (r.wpct is not None and float(r.wpct) > 0) else ""),
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
        // ── THE NAMED-PLACE ANSWER, DECIDED BY SQL ──────────────────
        // "Within 5 miles of Casper" needs Casper's geometry, which the
        // browser does not have and should not: the panel's count already
        // asked that question, so the map filters on the SAME answer rather
        // than computing a second one that could disagree. ids is a list of
        // lease_number, unique across all 24,178 features.
        //
        // An EMPTY list means "asked, nothing matched" and hides everything;
        // its ABSENCE means "not asked". Those must stay distinct -- collapse
        // them and a filter that matches nothing silently draws the lot.
        if (FILT.ids) {
            if (!FILT._idset) {
                FILT._idset = {};
                for (var ii = 0; ii < FILT.ids.length; ii++) {
                    FILT._idset[FILT.ids[ii]] = 1;
                }
            }
            if (!FILT._idset[p.ln]) { hide(); return; }
        }
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
        if (FILT.wpmin) {
            // A NULL IS NOT A ZERO. Never-measured fails a wetland question
            // the way it fails every other one here.
            var wp = p._wp;
            if (wp === undefined || wp === null || wp < FILT.wpmin) {
                hide(); return;
            }
        }
        if (FILT.wty && FILT.wty.length &&
                FILT.wty.indexOf(p._wt) < 0) { hide(); return; }
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
                fillColor: c, fillOpacity: 0.38,
                className: 'dv-lease-poly'};
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
                ['Wetland', p.wet],
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
    # PASSED THROUGH VERBATIM, not recomputed. None means the named filter
    # was not asked; a list (even empty) means it was.
    if filt.get("ids") is not None:
        out["ids"] = list(filt.get("ids") or [])
    if _opr:
        out["opr"] = _opr
    if _ac > 0:
        out["ac"] = _ac
    # MILES IN, KILOMETRES OUT. The panel asks in miles because that is what
    # a land man says; the stamp is in km because that is what the projection
    # measured. Converted once, here, so the browser never sees a unit.
    try:
        _wp = float(filt.get("wet_min_pct") or 0)
    except (TypeError, ValueError):
        _wp = 0.0
    if _wp > 0:
        out["wpmin"] = _wp
    _wty = [s for s in (filt.get("wet_types") or []) if s]
    if _wty:
        out["wty"] = _wty
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
                       lease_url=None, lease_by="producing",
                       lease_where=None, lease_params=None):
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
    # ── THE SHADING OBEYS THE LEASE FILTERS ─────────────────────────────
    # "Lease filters don't filter townships?" They did not: this counted
    # every tract in the township, so a map filtered to 42 leases near
    # Casper still coloured all 2,888 townships by their full leased
    # acreage. The panel said one thing and the map said another -- the
    # failure this feature has already paid for twice.
    #
    # AN EXISTS ON THE JOIN, NOT A WHERE. On a LEFT JOIN a WHERE over `g`
    # would drop the unmatched townships entirely, quietly turning this into
    # the other design -- hide what does not match -- when what was asked
    # for is the grid intact and the COLOUR filtered.
    #
    # The clauses come from _lease_filter_sql, the same definition the
    # panel's count uses, with prefixed parameters so they cannot collide
    # with :st / :s / :n / :w / :e above.
    _lease_join = ""
    if lease_where:
        _lease_join = (
            " AND EXISTS (SELECT 1"
            "   FROM dataview.dv_land_right r"
            "   JOIN dataview.dv_land_right_tract x"
            "     ON x.land_right_id = r.land_right_id"
            "  WHERE x.tract_id = g.tract_id AND "
            + " AND ".join(lease_where) + ")")
        params.update(lease_params or {})
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
                    ON  g.plss_id = t.plss_id{_lease_join}
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
                // QUIETER WHEN THERE ARE THOUSANDS OF THEM. At 0.55 fill
                // and a 0.85 grid the statewide layer read as the subject
                // of the map rather than its backdrop: it buried the
                // basemap, the terrain and the wells under 2,888 filled
                // squares. The choropleth still has to WORK -- leased
                // acreage across the state is the reason this view exists
                // -- so the fill is softened rather than removed, and the
                // grid lines are thinned and lightened.
                //
                // HOVER IS WHERE THE STRENGTH GOES. A township you are
                // pointing at can be emphatic; 2,888 of them cannot. So the
                // resting state drops and the mouseover keeps its old
                // weight, which also makes the layer feel more responsive
                // than it did when everything was already loud.
                var base = framed
                    ? {color: '#7a3a0d', weight: 1.6, opacity: 0.95,
                       fillColor: c, fillOpacity: 0.06,
                       className: 'dv-twp-poly'}
                    : {color: '#3f3a33', weight: 0.6, opacity: 0.8,
                       fillColor: c, fillOpacity: p.n ? 0.55 : 0.10,
                       className: 'dv-twp-poly'};
                layer.setStyle(base);
                layer.on('mouseover', function(){
                    layer.setStyle({weight: 2, fillOpacity: 0.62}); });
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
                    // ── THE WAY BACK ────────────────────────────────
                    // The body of the zoom-10 reset that was removed on
                    // request. Removing the ZOOM as a trigger did not mean
                    // removing the capability -- but it left the map with no
                    // undo at all, and the popup still telling people to zoom
                    // out. Offered as a link instead: same work, a trigger
                    // that is visible at the moment it is wanted.
                    //
                    // Restores ONLY what was hidden here: if __dv_twp_grp is
                    // null the grid was never stood down, and a grid switched
                    // off in the layer control is not switched back on behind
                    // the reader's back.
                    // ── THE WAY BACK, WHERE IT CANNOT BE LOST ───────────
                    // "Where is the popup if I am zoomed into several
                    // townships." Nowhere: the popup is anchored to the one
                    // township that was clicked, and it is the only thing
                    // carrying the way back. Close it, or pan off it, and
                    // the map is left focused with no visible undo -- the
                    // amber frame around the focused township is
                    // interactive:false, so it cannot even be clicked to
                    // bring the popup back.
                    //
                    // So the link also goes on a control, which sits in the
                    // corner of the map for exactly as long as the grid is
                    // stood down. Same backOut(), a place that does not move
                    // and cannot be dismissed by accident.
                    //
                    // L.DomEvent.disableClickPropagation, or clicking it
                    // reaches the map underneath and the click that undoes
                    // the focus also drills whatever is beneath the button.
                    function twpBackControl(label) {
                        if (mp.__dv_twp_ctl) {
                            try { mp.removeControl(mp.__dv_twp_ctl); }
                            catch (e) {}
                            mp.__dv_twp_ctl = null;
                        }
                        if (!label) { return; }
                        // BOTTOM LEFT, NOT TOP RIGHT. The top right corner
                        // already stacks move, fullscreen, 3D, 2D, the
                        // wells toggle and the layer control, so a
                        // two-line amber box there pushed into the map and
                        // sat over the very township that had just been
                        // exploded. Bottom left holds only the zoom badge,
                        // and Leaflet stacks controls in that corner
                        // rather than overlapping them.
                        //
                        // Narrower too: the label moved onto one line and
                        // the wording lost a word it did not need. It still
                        // says which township is showing, because "why can
                        // I only see one" is the question it exists to
                        // answer.
                        var K = L.Control.extend({
                            options: {position: 'bottomleft'},
                            onAdd: function () {
                                var d = L.DomUtil.create('div', '');
                                d.style.cssText =
                                    'background:rgba(28,25,23,.92);border:1px ' +
                                    'solid #f59e0b;border-radius:6px;' +
                                    'padding:4px 8px;font:600 11px system-ui;' +
                                    'color:#f59e0b;cursor:pointer;' +
                                    'white-space:nowrap;box-shadow:0 1px 4px ' +
                                    'rgba(0,0,0,.4)';
                                d.innerHTML = '&#8617; all townships' +
                                    '<span style="font:400 10px system-ui;' +
                                    'color:#a8a29e;padding-left:6px">' +
                                    label + ' only</span>';
                                L.DomEvent.disableClickPropagation(d);
                                L.DomEvent.on(d, 'click', function (ev) {
                                    L.DomEvent.preventDefault(ev);
                                    backOut();
                                });
                                return d;
                            }
                        });
                        mp.__dv_twp_ctl = new K();
                        mp.addControl(mp.__dv_twp_ctl);
                    }

                    function backOut() {
                        window.DV_TWP_FOCUS = null;
                        twpBackControl(null);
                        if (mp.__dv_twp_leases) {
                            try { mp.removeLayer(mp.__dv_twp_leases); }
                            catch (e) {}
                            mp.__dv_twp_leases = null;
                        }
                        if (mp.__dv_twp_grp) {
                            try { mp.addLayer(mp.__dv_twp_grp); }
                            catch (e) {}
                            mp.__dv_twp_grp = null;
                        }
                        if (mp.__dv_twp_frame) {
                            try { mp.removeLayer(mp.__dv_twp_frame); }
                            catch (e) {}
                            mp.__dv_twp_frame = null;
                        }
                        // Un-fade every lease the highlight dimmed.
                        mp.eachLayer(function (grp) {
                            if (!grp || !grp.eachLayer) { return; }
                            try {
                                grp.eachLayer(function (c) {
                                    var q = c.feature && c.feature.properties;
                                    if (!q || q.ln === undefined) { return; }
                                    c.setStyle({opacity: 0.9,
                                                fillOpacity: 0.38,
                                                weight: 1.0});
                                });
                            } catch (e) { /* not a lease group */ }
                        });
                        try { mp.closePopup(); } catch (e) {}
                    }

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
                        // The undo appears at the same moment the grid goes,
                        // and names what is being shown -- "showing 31N 97W
                        // only" answers "why can I only see one township"
                        // without anyone having to work it out.
                        twpBackControl(p.lab);
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
                                'margin-top:4px">' + extra + '</div>' +
                                '<div style="margin-top:6px"><a href="#" ' +
                                'id="dvtwpback" style="font:600 11px ' +
                                'system-ui;color:#f59e0b;text-decoration:none"' +
                                '>&#8617; show all townships</a></div>';
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
                            // BOUND AFTER EVERY setContent, because Leaflet
                            // replaces the popup's DOM each time -- a handler
                            // attached once would survive exactly until the
                            // first update, which is the async lease load.
                            try {
                                var lnk = document.getElementById('dvtwpback');
                                if (lnk) {
                                    lnk.addEventListener('click', function (ev) {
                                        ev.preventDefault();
                                        backOut();
                                    });
                                }
                            } catch (e) {}
                        } catch (e) {}
                    }
                    say(inside + ' drawn here');

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
                                (byStamp ? '' : ' (approx)'));
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
    # ── ON TOP OF TOWNSHIPS AND LEASES, WHICH IS A PANE, NOT AN ORDER ───
    # "Can I put wetlands on top of Townships and Leases." Not by reordering
    # the layer control: this is a RASTER, and Leaflet puts tile layers in
    # tilePane (z-index 200) while GeoJson vectors go in overlayPane (400).
    # Raster is structurally underneath vector however the control lists
    # them, so there is no toggle that could have done it.
    #
    # 450 clears the vectors and stays below shadowPane (500), markerPane
    # (600), tooltipPane (650) and popupPane (700) -- so well symbols,
    # tooltips and every popup still read over the top of it. Sliding it
    # above those would hide the thing you clicked.
    #
    # POINTER EVENTS OFF, AND THIS IS THE HALF THAT BREAKS SILENTLY. A pane
    # laid over the vectors swallows the clicks aimed at them: township
    # click-to-expand and lease clicks would simply stop, with no error and
    # nothing on screen to say the raster ate them. The overlay is a
    # picture; it has no business receiving a click.
    _f.map.CustomPane("wetlands", z_index=450,
                      pointer_events=False).add_to(m)
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
        # 0.75 WAS CHOSEN WHEN THIS DREW UNDERNEATH, where washing out the
        # basemap was the whole cost. Over the leases it washes out the
        # colour that says which lease is which, so it comes down.
        opacity=0.5,
        pane="wetlands",
        attr="Wetlands: USFWS National Wetlands Inventory",
    ).add_to(m)
    return _name



# ── HIGHWAYS: THE LINES THE DISTANCE FILTER MEASURED TO ────────────────────
# The basemaps already draw roads, so this is not "show me roads". It is the
# specific 84 primary routes that tools/stamp_cultural_distance.py measured
# against -- so "within 5 miles of a highway: 3,286 leases" can be checked by
# eye instead of taken on trust.
#
# SERVED, NOT EMBEDDED, for the reason the leases are: 1.09 MB in the map
# HTML would be paid on every rerun, and the browser caches a file.
HIGHWAY_GEOJSON_NAME = "dv_highways.geojson"


# ── PLACES ARE REFERENCE DATA, SO THEY ARE FETCHED ONCE PER PROCESS ───────
# Measured 2 Sep from dev.out.log: add_places_layer cost 0.841s on EVERY
# render -- 76 of them -- re-reading and re-parsing the same 205 municipal
# polygons to draw the same towns again. Nothing about them changes between
# renders; nothing about them changes between months.
#
# THE FETCH IS THE COST, NOT THE DRAWING. Split three ways on this machine:
#   SQL (geography -> WKT)  0.93s
#   shapely parse           0.12s
#   folium objects          0.004s
# So the cache holds the PARSED FEATURES and the label positions, and every
# render still builds its own folium objects. That division is not a
# preference: a folium layer belongs to the map it was added to, so a cached
# FeatureGroup handed to the next render would be attached to two maps and
# drawn on one, which is the kind of bug that reads as "the towns vanished".
#
# KEYED BY STATE and held for the life of the process, because TIGER
# municipal boundaries are a reference table. clear_places_cache() is for the
# case where dv_place_geom is reloaded while the app is running.
_PLACES_CACHE = {}


def clear_places_cache():
    """Forget the cached municipal geometry. Call after reloading places."""
    _PLACES_CACHE.clear()


def _places_data(engine, state):
    """(features, labels) for one state, parsed once. ([], []) on failure.

    A FAILURE IS NOT CACHED. Caching an empty result would turn one bad
    connection into a session with no towns and no way back short of a
    restart.
    """
    _hit = _PLACES_CACHE.get(str(state))
    if _hit is not None:
        return _hit
    try:
        from shapely import wkt as _wkt
        from shapely.geometry import mapping as _mapping
    except Exception as exc:
        print("[geography_layers] places layer needs shapely: %s" % exc)
        return [], []
    try:
        with engine.connect() as con:
            rows = con.execute(text("""
                SELECT place_name, place_type, geog.STAsText() AS wkt,
                       geog.EnvelopeCenter().Lat  AS clat,
                       geog.EnvelopeCenter().Long AS clon
                  FROM dataview.dv_place_geom
                 WHERE province_state = :st AND geog IS NOT NULL
            """), {"st": state}).fetchall()
    except Exception as exc:
        print("[geography_layers] places query failed: %s" % exc)
        return [], []
    feats, labels = [], []
    for r in rows:
        try:
            geom = _mapping(_wkt.loads(r.wkt))
        except Exception:
            continue
        feats.append({"type": "Feature", "geometry": geom,
                      "properties": {"nm": r.place_name,
                                     "ty": r.place_type}})
        if r.clat is not None:
            labels.append((float(r.clat), float(r.clon),
                           r.place_name, r.place_type))
    if feats:
        _PLACES_CACHE[str(state)] = (feats, labels)
        print("[geography_layers] places cached: %d feature(s), %d label(s)"
              % (len(feats), len(labels)))
    return feats, labels



PLACES_GEOJSON_NAME = "dv_places_%s.geojson"

# STYLED AND LABELLED IN THE BROWSER, which is not a preference. The file is
# served by URL, and folium can only run a Python style_function over data it
# EMBEDS -- so the moment the geometry left the document, style_function and
# GeoJsonTooltip stopped being options. add_lease_layer learned this first;
# this is the same template shape for the same reason.
_PLACES_ON_EACH = """
function(feature, layer) {
    layer.setStyle({color: '#334155', weight: 1.2, opacity: 0.9,
                    fillColor: '#94a3b8', fillOpacity: 0.35});
    var p = feature.properties || {};
    layer.bindTooltip('<b>' + (p.nm || '') + '</b><br>' + (p.ty || ''),
                      {sticky: true});
}
"""


def ensure_places_geojson(engine, state="WY", static_dir=None):
    """(path, url, n) for the state's places file, written if it is stale.

    THE CACHE ABOVE FIXED THE FETCH AND NOT THE PAYLOAD, which is the half
    that actually hurt. Measured 2 Sep: a folium map with no places renders
    to 3.2 KB and the same map with them to 2,756 KB -- 205 municipal
    polygons pasted into the document on EVERY render, 848 times the rest of
    the map, and st_folium ships that to the browser twice because the block
    is injected twice. Served as a file the browser fetches once and caches,
    the document carries a URL instead.

    THE SIGNATURE IS THE ROW COUNT AND A FORMAT NUMBER. dv_place_geom has no
    change stamp -- place_id, place_name, province_state, place_type, geog,
    source and nothing else -- so an edit that leaves the count alone will
    not be noticed. That is acceptable for TIGER municipal boundaries and it
    is why _FMT exists: bump it whenever what goes INTO the file changes, or
    the new content will never be written and the change will look applied.
    """
    import json as _json
    import os as _os
    _FMT = 1
    if static_dir is None:
        static_dir = _os.path.join(_os.path.dirname(_os.path.dirname(
            _os.path.dirname(_os.path.abspath(__file__)))), "static")
    name = PLACES_GEOJSON_NAME % str(state).lower()
    path = _os.path.join(static_dir, name)
    url = "/app/static/" + name
    feats, _labels = _places_data(engine, state)
    if not feats:
        return None, None, 0
    sig = {"fmt": _FMT, "n": len(feats), "state": str(state)}
    sigpath = path + ".sig"
    try:
        if _os.path.exists(path) and _os.path.exists(sigpath):
            with open(sigpath, encoding="utf-8") as _sf:
                if _json.load(_sf) == sig:
                    return path, url, len(feats)
    except Exception as exc:
        # NOT swallowed: an unreadable sidecar rewrites, and says why.
        print("[geography_layers] places sidecar unreadable, rewriting: %s"
              % str(exc)[:120])
    try:
        _os.makedirs(static_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as _fh:
            _json.dump({"type": "FeatureCollection", "features": feats}, _fh)
        # STAMPED ONLY AFTER THE WRITE SUCCEEDED, so a failed write cannot
        # leave a stamp claiming a file it did not produce.
        with open(sigpath, "w", encoding="utf-8") as _sf:
            _json.dump(sig, _sf)
        print("[geography_layers] places geojson written: %d feature(s), %.0f KB"
              % (len(feats), _os.path.getsize(path) / 1024.0))
    except Exception as exc:
        print("[geography_layers] places geojson not written: %s"
              % str(exc)[:120])
        return None, None, 0
    return path, url, len(feats)



def add_places_layer(m, engine, state="WY", show=True,
                     label_types=("city", "town")):
    """Towns and cities from dv_place_geom. Returns the number drawn.

    THE EVIDENCE FOR THE "NEAR A TOWN" FILTER, exactly as the highway layer
    is for the distance-to-highway one. dv_tract_place_dist holds 325,390
    measured pairs and the lease panel filters on them, but nothing drew the
    places -- so "leases within five miles of Casper" returned a set nobody
    could check by eye, and Casper itself was invisible under the terrain
    overlay, which paints over the basemap's own labels.
    #
    THEY ARE POLYGONS, NOT PINS. TIGER gives municipal boundaries, so the
    town is drawn at its real extent. That matters for a five-mile filter:
    measured from the boundary, five miles from Casper is a different set
    than five miles from a dot in the middle of it.

    Labels are drawn for `label_types`: the nineteen cities and the eighty
    incorporated towns. The 106 CDPs stay unlabelled and keep their hover
    tooltip -- a census designated place is mostly a named road junction,
    and putting all 205 on at once is a wall of text at state scale.
    """
    import folium as _f
    feats, labels = _places_data(engine, state)
    if not feats:
        return 0
    grp = _f.FeatureGroup(name="🏘 Towns and cities", show=show)
    labelled = 0
    for _la, _lo, _nm, _ty in labels:
        if _ty not in label_types:
            continue
        # A LABEL WITH A HALO, because it has to read over a terrain
        # fill, a satellite tile and a pale topo map alike.
        #
        # %% ON PURPOSE: the CSS translate is a percentage and this
        # string is %-formatted, so a bare % is read as a format spec.
        _f.Marker(
            [_la, _lo],
            icon=_f.DivIcon(icon_size=(0, 0), icon_anchor=(0, 0), html=(
                "<div style=\"font:600 11px system-ui;color:#0f172a;"
                "white-space:nowrap;transform:translate(-50%%,-50%%);"
                "text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff,"
                "0 0 3px #fff\">%s</div>" % _nm)),
        ).add_to(grp)
        labelled += 1
    if not feats:
        return 0
    # SERVED BY URL, NOT PASTED IN. The polygons are 2.7 MB of the map
    # document; as a file the browser fetches them once and caches them
    # across renders, and st_folium ships a URL instead -- twice, since
    # it injects the whole block twice.
    _path, _url, _n = ensure_places_geojson(engine, state)
    if _path:
        _gj = _f.GeoJson(_path, embed=False,
                         on_each_feature=_f.JsCode(_PLACES_ON_EACH))
        # The link folium emits must be the BROWSER's, not ours.
        _gj.embed_link = _url
        _gj.add_to(grp)
    else:
        # EMBEDDED WHEN THE FILE CANNOT BE WRITTEN. A read-only static/
        # should cost payload, not the towns -- and the layer that is
        # the evidence for the "near a town" filter has to draw.
        _f.GeoJson(
            {"type": "FeatureCollection", "features": feats},
            style_function=lambda _x: {"color": "#334155", "weight": 1.2,
                                       "opacity": 0.9,
                                       "fillColor": "#94a3b8",
                                       "fillOpacity": 0.35},
            tooltip=_f.GeoJsonTooltip(fields=["nm", "ty"],
                                      aliases=["Place", "Type"],
                                      sticky=True),
        ).add_to(grp)
    grp.add_to(m)
    print("[geography_layers] places: %d drawn, %d labelled"
          % (len(feats), labelled))
    return len(feats)


NAIP_WMS = ("https://imagery.nationalmap.gov/arcgis/services/USGSNAIPPlus"
            "/ImageServer/WMSServer")


def add_naip_layer(m, show=False, infrared=False):
    """USGS NAIP aerial photography as a WMS overlay. Returns the name.

    ONE METRE AERIAL, WHICH THE BASEMAP IMAGERY IS NOT. Esri's World Imagery
    is global and cached; NAIP is the USDA's US farm-season survey at about
    a metre, and at that scale a well pad, its access two-track and the scar
    of an old location are all separable. Public domain, no key.

    IT IS A WMS, NOT A TILE CACHE, and that is why it is off by default: the
    server renders each request. Measured over Teapot Dome, a 512px request
    took 13.5s cold and 1.2s once the server had it -- fine as a layer you
    switch on to look at something, wrong as the background you pan around
    on. Esri Satellite stays the basemap for that.

    INFRARED IS THE ONE WORTH KNOWING ABOUT. NAIP carries a near infrared
    band, and the false colour composite puts vegetation in red and bare or
    disturbed ground in pale cyan -- so pads, roads and old locations read
    at a glance, and riparian ground separates from dry, which is the same
    distinction the wetland filter makes numerically.
    """
    import folium as _f
    _name = "🛰 NAIP infrared (1 m)" if infrared else "🛰 NAIP aerial (1 m)"
    _f.raster_layers.WmsTileLayer(
        url=NAIP_WMS,
        layers=("USGSNAIPPlus:FalseColorComposite" if infrared
                else "USGSNAIPPlus:NaturalColor"),
        fmt="image/jpeg",
        transparent=False,
        version="1.3.0",
        attr="USGS NAIP · public domain",
        name=_name, overlay=True, control=True, show=show,
        # ── FEWER, BIGGER REQUESTS, AND ONLY WHEN THE MAP SETTLES ────────
        # THE TILES CANNOT BE CACHED, so every one is paid for every time.
        # Measured 2 Sep: USGS answers "Cache-Control: private" with no
        # ETag, Expires or Last-Modified, so the browser is forbidden from
        # reusing a tile it fetched a second ago. That is why the layer
        # never gets faster the longer you look at it, and why a Streamlit
        # rerun re-fetches the whole screen.
        #
        # 512 IS SIX TIMES FASTER FOR THE SAME GROUND. Per-request overhead
        # dominates this service: one 512px tile took 5.4s where the four
        # 256px tiles covering the same area took 32.5s, 8.1s each. It also
        # quarters the request count, which matters when none can be reused.
        tile_size=512,
        # AND NOT WHILE THE MAP IS MOVING. Every intermediate frame of a pan
        # otherwise starts its own round of 14-second requests that are
        # thrown away before they arrive.
        update_when_idle=True,
        update_when_zooming=False,
        # The BAKE is the real answer for an area you return to -- see
        # tools/build_terrain_overlay.py --naip. This only makes the live
        # layer as cheap as a live layer can be.
    ).add_to(m)
    return _name


def add_roads_overlay(m, show=False):
    """Every road, as a transparent tile overlay. Returns the layer name.

    NOT THE SAME THING AS THE HIGHWAY LAYER, and both are worth having.
    dv_road_geom holds sixteen Wyoming primary routes because those are the
    lines tools/stamp_cultural_distance.py MEASURED to -- they are evidence
    for a filter, and a client can check "within five miles of a highway"
    against them. This is context: secondary roads, county roads, the street
    grid in Casper, drawn by Esri and thinned by zoom.
    """
    import folium as _f
    _name = "🛣 Roads (all)"
    # HALF STRENGTH, BECAUSE ITS LABELS CANNOT BE RESTYLED. Esri renders the
    # road names INTO the tile image, rotated along the line and small, and
    # at full strength they compete with -- and lose to -- the names this
    # app draws itself. The geometry is what this layer is for: where the
    # county roads and the street grid run. So the whole tile is dialled
    # back to context, and the legible names come from the highway layer.
    #
    # If the names are wanted at this level of detail, the answer is not a
    # different opacity: it is TIGER secondary roads loaded into
    # dv_road_geom, where they can be labelled the same way the sixteen
    # primary routes are.
    _f.TileLayer(
        tiles=("https://server.arcgisonline.com/ArcGIS/rest/services/"
               "Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}"),
        attr="Esri, HERE, Garmin, &copy; OpenStreetMap contributors",
        name=_name, overlay=True, control=True, show=show, max_zoom=19,
        opacity=0.55,
    ).add_to(m)
    return _name


def add_highway_layer(m, path, url, show=False):
    """TIGER primary roads as a served overlay. Returns the layer name."""
    import folium as _f
    _name = "🛣 Highways (I- and US)"
    _gj = _f.GeoJson(
        path, embed=False, show=show, name=_name,
        # HEAVIER, BECAUSE THESE ARE THE LINES THE FILTER MEASURED TO.
        # A 1.8px amber thread over a 35% casing disappeared against the
        # topo basemap and again over the terrain fill, which is the one
        # place it matters: "within five miles of a highway" cannot be
        # checked by eye if the highway cannot be seen. The casing carries
        # most of the increase -- a dark outline is what makes a bright line
        # legible over BOTH a pale basemap and a dark one, and widening only
        # the amber would have made a fat stripe that still washed out.
        #
        # The two sites below must agree: style_function paints the feature
        # and on_each_feature repaints it before adding the inner line, so a
        # value changed in one place and not the other shows as a flicker on
        # load.
        style_function=lambda _f_: {
            # A ROAD IS A LINE, NOT A REGION: no fill, and a casing so it
            # reads over both the pale topo basemap and the satellite one.
            "color": "#111827", "weight": 4.0, "opacity": 0.40,
        },
        on_each_feature=_f.JsCode("""
            function(feature, layer) {
                var p = feature.properties || {};
                // The visible line, drawn over its own casing.
                layer.setStyle({color: '#111827', weight: 4.0, opacity: 0.40});
                var inner = L.polyline(layer.getLatLngs(), {
                    color: '#fbbf24', weight: 2.0, opacity: 0.95,
                    interactive: false
                });
                layer.on('add', function () {
                    if (layer._map) { inner.addTo(layer._map); }
                });
                layer.on('remove', function () {
                    if (inner._map) { inner.remove(); }
                });
                if (p.nm) {
                    // A NAME ON THE LINE, NOT ON HOVER. One label per ROUTE
                    // at its longest segment was clean statewide and gave
                    // nothing at field zoom: the labelled piece of I-25 is
                    // a hundred miles from the lease you are looking at. So
                    // every segment carries the name, and the map hides
                    // them when zoomed out far enough for that to be a
                    // wall of repeated text.
                    layer.bindTooltip(p.nm, {
                        permanent: true, direction: 'center',
                        className: 'dv-hwy-label', opacity: 1
                    });
                    var mp2 = null;
                    function hwyZoom() {
                        if (!mp2) { return; }
                        var on = mp2.getZoom() >= 8;
                        var el = layer.getTooltip && layer.getTooltip();
                        if (!el) { return; }
                        var n = el.getElement && el.getElement();
                        if (n) { n.style.display = on ? '' : 'none'; }
                    }
                    layer.on('add', function () {
                        mp2 = layer._map;
                        if (mp2) {
                            mp2.on('zoomend', hwyZoom);
                            setTimeout(hwyZoom, 0);
                        }
                    });
                    layer.on('remove', function () {
                        if (mp2) { mp2.off('zoomend', hwyZoom); }
                    });
                }
            }"""),
    )
    _gj.embed_link = url
    _gj.add_to(m)
    # THE LABEL'S LOOK LIVES IN CSS, because a Leaflet tooltip is a div and
    # styling it inline per feature would ship the same rules 84 times.
    # pointer-events off so a road name never blocks a click on the road, or
    # on the lease under it.
    _f.Element(
        "<style>.leaflet-tooltip.dv-hwy-label{background:rgba(255,255,255,.82);"
        "border:none;box-shadow:none;color:#7c2d12;font:700 10px system-ui;"
        "padding:0 3px;pointer-events:none;white-space:nowrap}"
        ".leaflet-tooltip.dv-hwy-label:before{display:none}</style>"
    ).add_to(m.get_root().header)
    return _name
