"""
page_well_documents.py
=====================
"Documents on the map" view.

  * Only wells that HAVE documents are dotted on the map. Files come from
    file_catalog.GLOBAL_FILE_CATALOG; the well key (UWI14), name and coordinates
    come from file_catalog.FILE_WELL_HEADER, joined on INVENTORY_ID. Coordinates
    live on the header, so a well shows as soon as it's cataloged — no promote
    needed.
  * Select wells three ways: draw a rectangle/polygon (multi-select), click a
    single dot, or pick from the dropdown fallback.
  * The selected wells' documents appear in a TABLE below the map, with an
    Open hyperlink per row. Because browsers block file:// links on many setups,
    a version-safe "open this file" picker below the table launches the chosen
    document in its native app (local) or downloads it.

Defensive: column names on both tables are discovered at runtime.
"""
from __future__ import annotations
import os
import sys
import subprocess

# MODULE LEVEL, not inside a function: `@st.cache_data(...)` on
# seismic_lines_db is a DECORATOR, and decorators are evaluated
# when the module is imported. Every other streamlit use here is
# inside a function and was importing it locally, so nothing ever
# noticed — until anything imported this module and got
# NameError: name 'st' is not defined.
import streamlit as st

GFC = "file_catalog.GLOBAL_FILE_CATALOG"   # the files
FWH = "file_catalog.FILE_WELL_HEADER"      # per-file well header (UWI14, coords)
DVW = "dataview.dv_well"                   # consolidated well (multi-source coords)
# Map height in px. Used BOTH for the folium Figure and for st_folium's
# iframe — they must agree or the difference shows as dead space.
MAP_H = 480

# Well-dot radius range in px. Area scales with document count between them.
# Kept small: on a basin-scale view the dots crowd, and colour carries the
# count as well, so size does not have to do the work alone.
R_MIN, R_MAX = 4, 9

# Colour ramp by file count. Fixed buckets rather than a ramp normalised to
# whatever is on screen, so a well does not change colour when you filter the
# map — the same well reads the same way in every view. Sequential blues, dark
# = busy, which is the convention people already read on a choropleth.
COUNT_BANDS = [
    (1,  "#A8CBE8", "1"),
    (2,  "#6BA3D6", "2"),
    (4,  "#3277B8", "3-4"),
    (9,  "#1F4E79", "5-9"),
    (10**9, "#0C2C49", "10+"),
]


def _count_colour(n):
    """Fill colour for a well with n files."""
    n = max(1, int(n or 1))
    for upper, colour, _label in COUNT_BANDS:
        if n <= upper:
            return colour
    return COUNT_BANDS[-1][1]

DOC_EXTS = ('.pdf', '.docx', '.doc', '.txt', '.rtf', '.md', '.html', '.htm',
            '.xlsx', '.xls', '.csv', '.tif', '.tiff', '.png', '.jpg', '.jpeg')


# ── column discovery ────────────────────────────────────────────────────────

def _columns(engine, schema, table):
    from sqlalchemy import text
    q = ("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
         "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t")
    with engine.connect() as con:
        return {r[0].lower(): r[0] for r in con.execute(text(q),
                                                        {"s": schema, "t": table})}


def _pick(cols: dict, *candidates):
    for c in candidates:
        if c.lower() in cols:
            return cols[c.lower()]
    return None


# ── geometry: point-in-polygon (ray casting on the exterior ring) ────────────

def _poly_contains(rings, lon, lat):
    try:
        ring = rings[0]
    except Exception:
        return False
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


# ── data ────────────────────────────────────────────────────────────────────

def _uwi_key_sql(c):
    """The well key, as ONE SQL expression used by BOTH the map and the
    document lookup.

    GLOBAL_FILE_CATALOG.MATCHED_UWI leads, FILE_WELL_HEADER.UWI is the
    fallback. MATCHED_UWI matters because it is the column the catalog's own
    assignment paths write — the Browse & View "Assign UWI / Survey" panel and
    the Excel assignment round-trip both UPDATE it. Keying only on the header
    (the original behaviour) meant a manually assigned UWI never reached this
    page at all. The header stays as fallback so nothing that worked before
    stops working.

    Defined once on purpose: the map and the per-well document lookup MUST
    agree on the key, or clicking a dot returns an empty document table.
    """
    parts = []
    if c.get("matched"):
        parts.append(f"NULLIF(LTRIM(RTRIM(g.[{c['matched']}])),'')")
    if c.get("uwi"):
        parts.append(f"NULLIF(LTRIM(RTRIM(h.[{c['uwi']}])),'')")
    if not parts:
        return "NULL"
    return parts[0] if len(parts) == 1 else f"COALESCE({', '.join(parts)})"


def _wells_with_docs(engine, c, doc_only=True):
    """One row per well that has files: uwi, name, field, state, coords, count.

    COORDINATES: COALESCE(dv_well surface coords, header coords).
    dv_well is the consolidated well table — CSV / PDF / DOCX / LAS all promote
    into it — so a well picks up a location from whichever source happened to
    carry one. FILE_WELL_HEADER is per-FILE, so it only holds coordinates when
    that particular file's extraction found them (in practice: LAS yes,
    documents no, which is why a documents-only map plotted almost nothing).
    Keeping the header as the fallback preserves this page's original
    "shows as soon as it's cataloged — no promote needed" behaviour, so the
    change adds coverage without removing any.

    doc_only=False drops the extension filter, so every catalogued file that
    resolves to a well counts — LAS/DLIS logs and well-bearing shapefiles then
    put their wells on the map too.
    """
    from sqlalchemy import text
    import pandas as pd

    ext_filter = ""
    if doc_only and c["ext"]:
        vals = ", ".join("'" + e + "'" for e in DOC_EXTS)
        ext_filter = f"AND LOWER(g.[{c['ext']}]) IN ({vals})"

    key = _uwi_key_sql(c)

    def _agg(parts, sqltype):
        """COALESCE over only the sources that actually EXIST.

        A missing column must never become MAX(NULL) / AVG(NULL): SQL Server
        rejects an untyped NULL as an aggregate operand with
        "Operand data type NULL is invalid for max operator" (error 8117).
        That is exactly what a FILE_WELL_HEADER without FIELD_NAME produced.
        If no source exists at all we emit a TYPED null so the column still
        comes back with the right shape.
        """
        parts = [p for p in parts if p]
        if not parts:
            return f"CAST(NULL AS {sqltype})"
        return parts[0] if len(parts) == 1 else f"COALESCE({', '.join(parts)})"

    TXT = "nvarchar(4000)"

    # per-file (header) values — the fallback layer. Typed placeholders so the
    # CTE column has a data type even when the source column is absent.
    h_name  = f"h.[{c['wname']}]"                  if c["wname"] else f"CAST(NULL AS {TXT})"
    h_field = f"h.[{c['field']}]"                  if c["field"] else f"CAST(NULL AS {TXT})"
    h_state = f"h.[{c['state']}]"                  if c["state"] else f"CAST(NULL AS {TXT})"
    h_lat   = f"TRY_CAST(h.[{c['lat']}] AS float)" if c["lat"]   else "CAST(NULL AS float)"
    h_lon   = f"TRY_CAST(h.[{c['lon']}] AS float)" if c["lon"]   else "CAST(NULL AS float)"

    # consolidated well (dv_well) — the preferred layer. Absent table/columns
    # degrade silently to header-only, matching this module's defensive style.
    if c.get("w_uwi"):
        # CAST to char(14): dv_well.uwi is char(14) and the catalog key is
        # nvarchar. Without the cast SQL Server promotes char -> nvarchar and
        # the dv_well index goes unusable. It also absorbs trailing spaces.
        dvw_join = (f"LEFT JOIN {DVW} w "
                    f"ON w.[{c['w_uwi']}] = CAST(f.uwi AS char(14))")
        w_name  = f"MAX(w.[{c['w_name']}])"                   if c.get("w_name")  else None
        w_field = f"MAX(w.[{c['w_field']}])"                  if c.get("w_field") else None
        w_state = f"MAX(w.[{c['w_state']}])"                  if c.get("w_state") else None
        w_lat   = f"MAX(TRY_CAST(w.[{c['w_lat']}] AS float))" if c.get("w_lat")   else None
        w_lon   = f"MAX(TRY_CAST(w.[{c['w_lon']}] AS float))" if c.get("w_lon")   else None
    else:
        dvw_join = ""
        w_name = w_field = w_state = w_lat = w_lon = None

    # dv_well first, header second — each included only if its column exists
    sel_name  = _agg([w_name,  "MAX(f.h_name)"  if c["wname"] else None], TXT)
    sel_field = _agg([w_field, "MAX(f.h_field)" if c["field"] else None], TXT)
    sel_state = _agg([w_state, "MAX(f.h_state)" if c["state"] else None], TXT)
    sel_lat   = _agg([w_lat,   "AVG(f.h_lat)"   if c["lat"]   else None], "float")
    sel_lon   = _agg([w_lon,   "AVG(f.h_lon)"   if c["lon"]   else None], "float")

    sql = f"""
        WITH f AS (
            SELECT {key}    AS uwi,
                   {h_name}  AS h_name,
                   {h_field} AS h_field,
                   {h_state} AS h_state,
                   {h_lat}   AS h_lat,
                   {h_lon}   AS h_lon
              FROM {GFC} g
              LEFT JOIN {FWH} h ON h.[{c['inv_h']}] = g.[{c['inv_g']}]
             WHERE 1=1 {ext_filter}
        )
        SELECT f.uwi      AS uwi,
               {sel_name}  AS well_name,
               {sel_field} AS field_name,
               {sel_state} AS province_state,
               {sel_lat}   AS lat,
               {sel_lon}   AS lon,
               COUNT(*)    AS n_files
          FROM f
          {dvw_join}
         WHERE f.uwi IS NOT NULL
         GROUP BY f.uwi
    """
    with engine.connect() as con:
        out = pd.read_sql(text(sql), con)
    out["well_name"] = out["well_name"].fillna(out["uwi"])
    return out.sort_values(["uwi", "well_name"]).reset_index(drop=True)


def _documents_for(engine, uwis, c, doc_only=True):
    """Documents for a list of UWIs, with the well name on each row.

    The path we hand back prefers the governed VAULT copy and falls back to the
    original network FILE_PATH only when a file hasn't been vaulted yet.

    doc_only MUST match what _wells_with_docs was called with. Without it this
    function listed every catalogued file regardless of the Include setting —
    so a well showing "1 file" in the picker opened a table of three, two of
    them LAS. Same class of drift as the UWI key, which is why both now take
    the setting rather than one assuming it.
    """
    from sqlalchemy import text
    import pandas as pd
    uwis = [str(u) for u in uwis if u]
    if not uwis:
        return pd.DataFrame(columns=["uwi", "well_name", "file_name", "file_path",
                                     "file_ext", "doc_type", "readiness", "loc",
                                     "inventory_id", "catalog_status"])
    # path: COALESCE(vault, network); loc: where that path points
    if c["vault"] and c["path"]:
        path_sel = (f"COALESCE(NULLIF(g.[{c['vault']}],''), g.[{c['path']}]) "
                    "AS file_path")
        loc_sel = (f"CASE WHEN NULLIF(g.[{c['vault']}],'') IS NOT NULL "
                   "THEN 'vault' ELSE 'network' END AS loc")
    elif c["vault"]:
        path_sel, loc_sel = f"g.[{c['vault']}] AS file_path", "'vault' AS loc"
    elif c["path"]:
        path_sel, loc_sel = f"g.[{c['path']}] AS file_path", "'network' AS loc"
    else:
        path_sel, loc_sel = "NULL AS file_path", "'?' AS loc"
    key = _uwi_key_sql(c)
    sels = [f"{key} AS uwi"]
    sels.append(f"h.[{c['wname']}] AS well_name" if c["wname"] else "NULL AS well_name")
    sels.append(f"g.[{c['name']}] AS file_name"  if c["name"]  else "NULL AS file_name")
    sels.append(path_sel)
    sels.append(loc_sel)
    sels.append(f"g.[{c['ext']}] AS file_ext"    if c["ext"]   else "NULL AS file_ext")
    sels.append(f"g.[{c['type']}] AS doc_type"   if c["type"]  else "NULL AS doc_type")
    sels.append(f"g.[{c['ready']}] AS readiness" if c["ready"] else "NULL AS readiness")
    sels.append(f"g.[{c['inv_g']}] AS inventory_id" if c["inv_g"] else "NULL AS inventory_id")
    sels.append(f"g.[{c['status']}] AS catalog_status" if c.get("status") else "NULL AS catalog_status")
    ph = ", ".join(f":u{i}" for i in range(len(uwis)))
    params = {f"u{i}": u for i, u in enumerate(uwis)}
    order = "ORDER BY uwi" + (f", g.[{c['name']}]" if c["name"] else "")
    # LEFT JOIN + the shared key: a file can carry a MATCHED_UWI without ever
    # getting a FILE_WELL_HEADER row (manual assignment, shapefile capture).
    # The original INNER JOIN on the header dropped exactly those files, and
    # keying on the header alone would return nothing for a well the map
    # plotted from MATCHED_UWI.
    ext_filter = ""
    if doc_only and c["ext"]:
        vals = ", ".join("'" + e + "'" for e in DOC_EXTS)
        ext_filter = f"AND LOWER(g.[{c['ext']}]) IN ({vals})"
    sql = (f"SELECT {', '.join(sels)} FROM {GFC} g "
           f"LEFT JOIN {FWH} h ON h.[{c['inv_h']}] = g.[{c['inv_g']}] "
           f"WHERE {key} IN ({ph}) {ext_filter} {order}")
    with engine.connect() as con:
        return pd.read_sql(text(sql), con, params=params)


# ── open ─────────────────────────────────────────────────────────────────────

def _open_native(path):
    """Open a file in its native app on the machine running Streamlit (local)."""
    try:
        if os.name == "nt":
            os.startfile(path)                       # noqa: Windows only
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return None
    except Exception as e:
        return str(e)


def _viewer_exts():
    """Extensions the in-app file_viewer explicitly handles. Pulled from the
    viewer's own constants so this never drifts from what it can actually show;
    falls back to a static list if the module/structure changes."""
    try:
        try:
            from modules import file_viewer as fv
        except ImportError:
            from dataview.file_catalog import file_viewer as fv
        s = set()
        for nm in ("PDF_EXTS", "LAS_EXTS", "DLIS_EXTS", "LIS_EXTS", "SEGY_EXTS",
                   "P190_EXTS", "SHP_EXTS", "EXCEL_EXTS", "CSV_EXTS", "WORD_EXTS",
                   "IMAGE_EXTS"):
            s |= set(getattr(fv, nm, set()) or set())
        out = {str(e).lower().lstrip(".") for e in s}
        if out:
            return out
    except Exception:
        pass
    return {"pdf", "las", "dlis", "dlf", "dis", "lis", "segy", "sgy", "seg",
            "p190", "p90", "p1", "shp", "geojson", "gpkg", "kml", "kmz",
            "xlsx", "xls", "xlsm", "csv", "tsv", "docx", "doc",
            "tif", "tiff", "png", "jpg", "jpeg"}


def _drawn_bbox(polys):
    """Bounding box (min_lat, max_lat, min_lon, max_lon) of all drawn shapes."""
    lats, lons = [], []
    for rings in polys:
        try:
            for pt in rings[0]:           # exterior ring, [lon, lat] pairs
                lons.append(float(pt[0]))
                lats.append(float(pt[1]))
        except Exception:
            continue
    if not lats:
        return None
    return (min(lats), max(lats), min(lons), max(lons))


def _qry_seismic_lines(engine):
    """Seismic LINES from EVERY source, one row per header, ordered
    survey -> line so the page can group by survey.

    NO file-extension filter. An earlier version required
    FILE_EXT IN ('.segy','.sgy','.seg'), which meant P190 navigation files and
    seismic SHAPEFILES were drawn on the map but could never appear in this
    list — the two sources that are best georeferenced, because they carry
    their own CRS.

    Returns SURVEY_OUTLINE too: a survey may have an outline and no BBOX_*,
    and the drawn-area filter needs bounds from whichever exists.
    """
    from sqlalchemy import text
    import pandas as pd
    cols = ["survey_name", "line_name", "set_type", "contractor",
            "file_name", "file_path", "file_ext", "outline",
            "bmin_lat", "bmax_lat", "bmin_lon", "bmax_lon"]
    try:
        with engine.connect() as con:
            return pd.read_sql(text("""
                SELECT sh.SURVEY_NAME    AS survey_name,
                       sh.LINE_NAME      AS line_name,
                       sh.SEIS_SET_TYPE  AS set_type,
                       sh.CONTRACTOR     AS contractor,
                       fc.FILE_NAME      AS file_name,
                       fc.FILE_PATH      AS file_path,
                       fc.FILE_EXT       AS file_ext,
                       sh.SURVEY_OUTLINE AS outline,
                       TRY_CAST(sh.BBOX_MIN_LAT AS float) AS bmin_lat,
                       TRY_CAST(sh.BBOX_MAX_LAT AS float) AS bmax_lat,
                       TRY_CAST(sh.BBOX_MIN_LON AS float) AS bmin_lon,
                       TRY_CAST(sh.BBOX_MAX_LON AS float) AS bmax_lon
                  FROM file_catalog.FILE_SEIS_HEADER sh
                  JOIN file_catalog.GLOBAL_FILE_CATALOG fc
                       ON fc.INVENTORY_ID = sh.INVENTORY_ID
                 ORDER BY sh.SURVEY_NAME, sh.LINE_NAME
            """), con)
    except Exception:
        return pd.DataFrame(columns=cols)


def _seis_fill_bounds(sg):
    """Make every row spatially testable: numeric bbox columns, and bounds
    derived from SURVEY_OUTLINE where BBOX_* is missing.

    Two reasons this is needed before any drawn-area filter:
      * BBOX_* is stored as text, so a raw pandas comparison against floats is
        string-vs-float — silently wrong rather than an error.
      * A survey can carry an outline and no bbox (shapefile-derived ones do).
        Without this it fails every comparison and vanishes from the drawn
        area without saying so.
    """
    import pandas as pd
    for c in ("bmin_lat", "bmax_lat", "bmin_lon", "bmax_lon"):
        if c in sg.columns:
            sg[c] = pd.to_numeric(sg[c], errors="coerce")
    if "outline" not in sg.columns or sg.empty:
        return sg
    need = sg["bmin_lat"].isna() & sg["outline"].notna()
    if not need.any():
        return sg
    try:
        from shapely import wkt as _wkt
    except ImportError:
        return sg
    for i in sg.index[need]:
        try:
            g = _wkt.loads(str(sg.at[i, "outline"]))
            if g.is_empty:
                continue
            lo0, la0, lo1, la1 = g.bounds      # (minx, miny, maxx, maxy)
            sg.at[i, "bmin_lon"], sg.at[i, "bmin_lat"] = lo0, la0
            sg.at[i, "bmax_lon"], sg.at[i, "bmax_lat"] = lo1, la1
        except Exception:
            continue
    return sg


def _geog_linestring_pts(wkt):
    """LINESTRING WKT -> [[lat, lon], ...].

    SQL Server geography WKT is (lon lat) — X is longitude; folium wants
    (lat, lon). Plain string parse: no shapely dependency for a layer that
    must degrade to nothing, not to an ImportError.
    """
    s = str(wkt or "")
    if "(" not in s or not s.lstrip().upper().startswith("LINESTRING"):
        return []
    body = s[s.find("(") + 1:s.rfind(")")]
    pts = []
    for pair in body.split(","):
        bits = pair.split()
        if len(bits) >= 2:
            try:
                pts.append([float(bits[1]), float(bits[0])])
            except ValueError:
                continue
    return pts


@st.cache_data(ttl=300, show_spinner=False)
def seismic_lines_db(_engine, _v: int = 2):
    """Real 2D line paths from dataview.dv_seis_line.geog.

    These are the ACTUAL survey lines — trace-order coordinates sampled along
    each file and reprojected to WGS84 from the CRS the file's own textual
    header declares. They replace seismic_2d_zones()'s hulls of bounding
    boxes, which were an honest approximation made when the only coordinate
    available was the first trace.

    Read from the DATABASE now: extract writes the LINESTRINGs into
    FILE_SEIS_HEADER.SURVEY_OUTLINE and promote converts them into
    dv_seis_line.geog, so seismic_lines.geojson is a pure export and no
    longer read here. No rows = no layer, silently — the blobs still draw,
    so nothing is lost on a deployment that has never promoted seismic.
    """
    from sqlalchemy import text
    import pandas as pd
    try:
        with _engine.connect() as con:
            df = pd.read_sql(text("""
                SELECT ss.seis_set_name   AS survey,
                       sl.line_name       AS line_name,
                       sl.trace_count     AS trace_count,
                       ss.epsg_code       AS epsg,
                       sl.geog.STAsText() AS wkt
                  FROM dataview.dv_seis_line sl
                  LEFT JOIN dataview.dv_seis_set ss
                         ON ss.seis_set_id = sl.seis_set_id
                 WHERE sl.geog IS NOT NULL
                   AND sl.geog.STGeometryType() = 'LineString'
                 ORDER BY ss.seis_set_name, sl.line_name
            """), con)
    except Exception as exc:
        print(f"[seismic_lines] dv_seis_line: {exc}")
        return []
    out = []
    for r in df.itertuples():
        pts = _geog_linestring_pts(r.wkt)
        if len(pts) < 2:
            continue
        try:
            _epsg = int(r.epsg) if pd.notna(r.epsg) else None
        except (TypeError, ValueError):
            _epsg = None
        try:
            _tr = int(r.trace_count) if pd.notna(r.trace_count) else None
        except (TypeError, ValueError):
            _tr = None
        out.append({
            "pts": pts,
            "survey": r.survey or "(unnamed survey)",
            "line": r.line_name or "",
            "epsg": _epsg,
            "traces": _tr,
        })
    return out


def seismic_2d_zones(engine, pad_deg=0.02):
    """One rough 'coverage zone' polygon per 2D survey, hulled from its line
    bboxes and softened so it reads as deliberately approximate."""
    from sqlalchemy import text
    import pandas as pd
    try:
        from shapely.geometry import MultiPoint
    except ImportError:
        return []
    try:
        with engine.connect() as con:
            df = pd.read_sql(text("""
                SELECT SURVEY_NAME AS survey,
                       TRY_CAST(BBOX_MIN_LAT AS FLOAT) AS mnla,
                       TRY_CAST(BBOX_MAX_LAT AS FLOAT) AS mxla,
                       TRY_CAST(BBOX_MIN_LON AS FLOAT) AS mnlo,
                       TRY_CAST(BBOX_MAX_LON AS FLOAT) AS mxlo
                  FROM file_catalog.FILE_SEIS_HEADER
                 WHERE SEIS_SET_TYPE = '2D'
                   AND TRY_CAST(BBOX_MIN_LAT AS FLOAT) IS NOT NULL
                   AND TRY_CAST(BBOX_MIN_LON AS FLOAT) IS NOT NULL
                   AND TRY_CAST(BBOX_MAX_LAT AS FLOAT) IS NOT NULL
                   AND TRY_CAST(BBOX_MAX_LON AS FLOAT) IS NOT NULL
            """), con)
    except Exception:
        return []
    zones = []
    for survey, gp in df.groupby("survey", sort=False):
        pts = []
        for r in gp.itertuples():
            pts += [(r.mnlo, r.mnla), (r.mnlo, r.mxla),
                    (r.mxlo, r.mxla), (r.mxlo, r.mnla)]
        try:
            hull = MultiPoint(pts).convex_hull.buffer(pad_deg)
        except Exception:
            continue
        if not hasattr(hull, "exterior") or hull.exterior is None:
            continue                                  # degenerate (single point)
        cen = hull.centroid
        zones.append({"survey": survey or "(unnamed)", "n_lines": len(gp),
                      "ring": [[y, x] for x, y in hull.exterior.coords],
                      "centroid": [cen.y, cen.x]})
    return zones


def _seis_footprints(engine, type_sql):
    """Shared footprint builder for any SEIS_SET_TYPE selection.

    Prefers the stored SURVEY_OUTLINE — the real dissolved footprint the
    extractor already wrote in WGS84 — and falls back to the bbox rectangle
    only when a file has no outline. That order matters: on live data more
    rows carry an outline than a bbox, so a bbox-only version silently
    dropped whole surveys. The outline is also the truer shape; a bbox
    over-covers a non-rectangular survey.

    NO source-file-extension filter. An earlier version required
    FILE_EXT IN ('.segy','.sgy','.seg') to keep footprints "backed by a real
    SEG-Y file", but that excluded seismic SHAPEFILES — which are the
    best-georeferenced source there is, since they carry a .prj and need no
    trace-header CRS guessing. It hid a survey sitting correctly in EPSG:4326
    while keeping ones reprojected from an inferred CRS.

    Falls back to bbox-only if shapely isn't installed, so the layer degrades
    instead of disappearing.
    """
    from sqlalchemy import text
    import pandas as pd
    try:
        from shapely import wkt as _wkt
        from shapely.ops import unary_union
    except ImportError:
        _wkt = unary_union = None
    try:
        with engine.connect() as con:
            df = pd.read_sql(text(f"""
                SELECT sh.SURVEY_NAME    AS survey,
                       sh.SURVEY_OUTLINE AS outline,
                       TRY_CAST(sh.BBOX_MIN_LAT AS FLOAT) AS mnla,
                       TRY_CAST(sh.BBOX_MAX_LAT AS FLOAT) AS mxla,
                       TRY_CAST(sh.BBOX_MIN_LON AS FLOAT) AS mnlo,
                       TRY_CAST(sh.BBOX_MAX_LON AS FLOAT) AS mxlo
                  FROM file_catalog.FILE_SEIS_HEADER sh
                 WHERE {type_sql}
            """), con)
    except Exception:
        return []

    out = []
    for survey, gp in df.groupby("survey", sort=False, dropna=False):
        label = survey or "(unnamed)"
        n = len(gp)
        rings = []
        # 1st choice: dissolve this survey's stored outlines
        if _wkt is not None:
            geoms = []
            for w in gp["outline"].dropna():
                try:
                    g = _wkt.loads(str(w))
                    if not g.is_empty:
                        geoms.append(g)
                except Exception:
                    continue
            if geoms:
                try:
                    merged = unary_union(geoms)
                    for p in getattr(merged, "geoms", [merged]):
                        if p.geom_type == "Polygon" and p.exterior is not None:
                            rings.append([[y, x] for x, y in p.exterior.coords])
                except Exception:
                    rings = []
        # 2nd choice: the bbox rectangle
        if not rings:
            g2 = gp.dropna(subset=["mnla", "mxla", "mnlo", "mxlo"])
            if g2.empty:
                continue
            mnla, mxla = float(g2["mnla"].min()), float(g2["mxla"].max())
            mnlo, mxlo = float(g2["mnlo"].min()), float(g2["mxlo"].max())
            if mnla == mxla and mnlo == mxlo:
                continue                              # zero-area point
            rings = [[[mnla, mnlo], [mnla, mxlo], [mxla, mxlo],
                      [mxla, mnlo], [mnla, mnlo]]]
        for ring in rings:
            lats = [p[0] for p in ring]
            lons = [p[1] for p in ring]
            out.append({"survey": label, "n_lines": n, "ring": ring,
                        "centroid": [sum(lats) / len(lats),
                                     sum(lons) / len(lons)]})
    return out


def seismic_3d_footprints(engine):
    """Footprints for surveys explicitly typed '3D'."""
    return _seis_footprints(engine, "sh.SEIS_SET_TYPE = '3D'")


def seismic_untyped_footprints(engine):
    """Footprints for surveys with REAL geometry but no SEIS_SET_TYPE.

    Both typed layers filter on exactly '2D'/'3D', so a survey the classifier
    never typed becomes invisible even when it is perfectly georeferenced —
    e.g. a seismic shapefile carrying EPSG:4326 and a dissolved outline. That
    is the wrong failure: geometry should never disappear because a label is
    missing. These draw in their own style so they read as 'type not set'
    rather than being silently promoted to 2D or 3D.
    """
    return _seis_footprints(
        engine,
        "(sh.SEIS_SET_TYPE IS NULL OR sh.SEIS_SET_TYPE NOT IN ('2D','3D'))")



def _geographic_entities(engine, c):
    """Field / lease / boundary / pipeline outlines for the map.

    These live DIRECTLY in GLOBAL_FILE_CATALOG, not in a dv_ table: when a
    shapefile classifies as FIELD / LAND_TRACT / BOUNDARY / PIPELINE, the
    extract stage stores its dissolved WGS84 footprint as WKT in
    SPATIAL_OUTLINE and stamps the category into CATALOG_TABLE. So, like the
    seismic layers, this needs no promote — an entity draws as soon as it is
    cataloged.

    The WKT is already reprojected to EPSG:4326 and ring-oriented by
    extract_core._shp_outline_wkt (it flips clockwise rings, which a geography
    column would otherwise read as the whole Earth minus the polygon), so
    nothing here has to re-do that work.

    Returns [{label, kind, rings, lines, centroid}] — 'rings' are filled
    polygons, 'lines' are unfilled paths (pipelines).
    """
    import pandas as pd
    if not (c.get("outline") and c.get("cat_tbl")):
        return []
    try:
        from shapely import wkt as _wkt
    except ImportError:
        return []
    from sqlalchemy import text
    try:
        with engine.connect() as con:
            df = pd.read_sql(text(f"""
                SELECT g.[{c['cat_tbl']}] AS kind,
                       g.[{c['outline']}] AS wkt,
                       {f"g.[{c['name']}]" if c['name'] else "NULL"} AS file_name
                  FROM {GFC} g
                 WHERE g.[{c['outline']}] IS NOT NULL
                   AND LTRIM(RTRIM(CAST(g.[{c['outline']}] AS varchar(max)))) <> ''
                   AND g.[{c['cat_tbl']}] IN
                       ('FIELD', 'LAND_TRACT', 'BOUNDARY', 'PIPELINE')
            """), con)
    except Exception:
        return []

    out = []
    for r in df.itertuples():
        try:
            geom = _wkt.loads(str(r.wkt))
        except Exception:
            continue                       # unreadable WKT — skip, never raise
        if geom.is_empty:
            continue
        rings, lines = [], []
        # normalise every geometry type to lists of [lat, lon] paths
        parts = list(getattr(geom, "geoms", [geom]))
        for p in parts:
            gt = p.geom_type
            if gt == "Polygon":
                if p.exterior is not None:
                    rings.append([[y, x] for x, y in p.exterior.coords])
            elif gt in ("LineString", "LinearRing"):
                lines.append([[y, x] for x, y in p.coords])
        if not rings and not lines:
            continue
        try:
            cen = geom.centroid
            centroid = [cen.y, cen.x]
        except Exception:
            centroid = None
        out.append({
            "label": str(r.file_name or "(unnamed)"),
            "kind":  str(r.kind or "").upper(),
            "rings": rings,
            "lines": lines,
            "centroid": centroid,
        })
    return out


# how each geographic category is drawn: (stroke, fill, emoji, pretty name)
GEO_STYLE = {
    "FIELD":      ("#1B5E20", "#4CAF50", "🛢", "Field"),
    "LAND_TRACT": ("#4A148C", "#9C6ADE", "📜", "Lease / tract"),
    "BOUNDARY":   ("#37474F", "#90A4AE", "🧭", "Boundary"),
    "PIPELINE":   ("#B34700", "#FF8A3D", "🚰", "Pipeline"),
}


def _qry_seismic_in_bbox(engine, mn_lat, mx_lat, mn_lon, mx_lon):
    """2D + 3D seismic surveys whose stored bbox OVERLAPS the drawn box.

    We only have each survey's bounding box (3D footprints are real rectangles;
    2D lines store just the box around the line, not the path). So 'intersects
    the box' is an axis-aligned bbox-overlap test. For 3D that's exact; for 2D
    it's an over-match (the line is somewhere in its box), which is the honest
    best with bbox-only geometry.
    """
    from sqlalchemy import text
    import pandas as pd
    cols = ["id", "set_type", "survey_name", "line_name", "contractor",
            "survey_date", "shot_first", "shot_last", "file_name", "file_path"]
    try:
        with engine.connect() as con:
            return pd.read_sql(text("""
                SELECT sh.SEIS_HEADER_ID AS id, sh.SEIS_SET_TYPE AS set_type,
                       sh.SURVEY_NAME AS survey_name, sh.LINE_NAME AS line_name,
                       sh.CONTRACTOR AS contractor, sh.SURVEY_DATE AS survey_date,
                       sh.SHOT_FIRST AS shot_first, sh.SHOT_LAST AS shot_last,
                       fc.FILE_NAME AS file_name, fc.FILE_PATH AS file_path
                  FROM file_catalog.FILE_SEIS_HEADER sh
                  LEFT JOIN file_catalog.GLOBAL_FILE_CATALOG fc
                         ON fc.INVENTORY_ID = sh.INVENTORY_ID
                 WHERE sh.SEIS_SET_TYPE IN ('2D', '3D')
                   AND TRY_CAST(sh.BBOX_MIN_LAT AS FLOAT) IS NOT NULL
                   AND TRY_CAST(sh.BBOX_MAX_LAT AS FLOAT) IS NOT NULL
                   AND TRY_CAST(sh.BBOX_MIN_LON AS FLOAT) IS NOT NULL
                   AND TRY_CAST(sh.BBOX_MAX_LON AS FLOAT) IS NOT NULL
                   AND TRY_CAST(sh.BBOX_MAX_LAT AS FLOAT) >= :mnlat
                   AND TRY_CAST(sh.BBOX_MIN_LAT AS FLOAT) <= :mxlat
                   AND TRY_CAST(sh.BBOX_MAX_LON AS FLOAT) >= :mnlon
                   AND TRY_CAST(sh.BBOX_MIN_LON AS FLOAT) <= :mxlon
                 ORDER BY sh.SEIS_SET_TYPE, sh.SURVEY_NAME, sh.LINE_NAME
            """), con, params={"mnlat": mn_lat, "mxlat": mx_lat,
                               "mnlon": mn_lon, "mxlon": mx_lon})
    except Exception:
        return pd.DataFrame(columns=cols)


# ── page ────────────────────────────────────────────────────────────────────

def run(engine, dialect=None):
    import streamlit as st
    import pandas as pd
    from pathlib import Path

    dlct = dialect or getattr(getattr(engine, "dialect", None), "name", None)

    top = st.columns([6, 1.2])
    top[0].subheader("📂 Well Documents")
    if top[1].button("🔄 Refresh", use_container_width=True):
        try:
            st.cache_data.clear()
        except Exception:
            pass
        st.rerun()
    st.caption("Only wells that have documents are shown. Click a dot or use the "
               "dropdown to pick a well; SEG-Y seismic surveys are listed below "
               "the map.")

    g = _columns(engine, "file_catalog", "GLOBAL_FILE_CATALOG")
    h = _columns(engine, "file_catalog", "FILE_WELL_HEADER")
    # dv_well is optional: if the table or its columns aren't there, every
    # w_* pick comes back None and the page falls back to header-only coords.
    try:
        wcols = _columns(engine, "dataview", "dv_well")
    except Exception:
        wcols = {}
    c = {
        "inv_g": _pick(g, "INVENTORY_ID"),
        "inv_h": _pick(h, "INVENTORY_ID"),
        "uwi":   _pick(h, "UWI14", "UWI"),
        "matched": _pick(g, "MATCHED_UWI"),
        "name":  _pick(g, "FILE_NAME", "FILENAME", "NAME"),
        "path":  _pick(g, "FILE_PATH", "FILEPATH", "PATH"),
        "vault": _pick(g, "VAULT_PATH"),
        "ext":   _pick(g, "FILE_EXT", "EXTENSION", "EXT"),
        "type":  _pick(g, "DOC_TYPE"),
        "ready": _pick(g, "CATALOG_READINESS", "READINESS"),
        "status": _pick(g, "CATALOG_STATUS", "CATALOG_READINESS"),
        # geographic entities (field / lease / boundary / pipeline outlines)
        "outline": _pick(g, "SPATIAL_OUTLINE"),
        "cat_tbl": _pick(g, "CATALOG_TABLE"),
        "wname": _pick(h, "WELL_NAME"),
        "field": _pick(h, "FIELD_NAME"),
        "state": _pick(h, "PROVINCE_STATE", "STATE"),
        "lat":   _pick(h, "SURFACE_LATITUDE", "LATITUDE", "LAT"),
        "lon":   _pick(h, "SURFACE_LONGITUDE", "LONGITUDE", "LON"),
        # consolidated well — preferred coordinate source
        "w_uwi":   _pick(wcols, "UWI", "UWI14"),
        "w_name":  _pick(wcols, "WELL_NAME"),
        "w_field": _pick(wcols, "FIELD_NAME"),
        "w_state": _pick(wcols, "PROVINCE_STATE", "STATE"),
        "w_lat":   _pick(wcols, "SURFACE_LATITUDE", "LATITUDE", "LAT"),
        "w_lon":   _pick(wcols, "SURFACE_LONGITUDE", "LONGITUDE", "LON"),
    }
    # the key can now come from EITHER side, so require only one of them
    if not (c["inv_g"] and c["inv_h"] and (c["uwi"] or c["matched"])):
        st.error("Could not locate the FILE_WELL_HEADER ↔ GLOBAL_FILE_CATALOG "
                 "link (UWI14 / MATCHED_UWI / INVENTORY_ID).")
        return

    scope = st.radio(
        "Include",
        ["Documents only", "All catalogued files"],
        horizontal=True, key="wd_scope")
    # Bound ONCE and passed to both queries. Computing it inline at each call
    # site is how the two drifted in the first place — the map honoured the
    # setting and the document table didn't.
    doc_only = scope.startswith("Documents")
    wells = _wells_with_docs(engine, c, doc_only=doc_only)
    if wells.empty:
        st.info("No files with a resolved well (UWI) found in the catalog yet.")
        return

    # ── field / state filters ────────────────────────────────────────────────
    fc1, fc2 = st.columns(2)
    with fc1:
        fields = ["(all fields)"] + sorted(
            str(x) for x in wells["field_name"].dropna().unique())
        f_sel = st.selectbox("Field", fields)
    with fc2:
        states = ["(all states)"] + sorted(
            str(x) for x in wells["province_state"].dropna().unique())
        s_sel = st.selectbox("State", states)
    if f_sel != "(all fields)":
        wells = wells[wells["field_name"] == f_sel]
    if s_sel != "(all states)":
        wells = wells[wells["province_state"] == s_sel]
    if wells.empty:
        st.info("No wells with documents match that filter.")
        return

    mapped = wells.dropna(subset=["lat", "lon"]).reset_index(drop=True)
    st.write(f"**{len(wells):,}** well(s) with documents · "
             f"**{int(wells['n_files'].sum()):,}** file(s) · "
             f"{len(mapped):,} plotted.")
    show_zones = st.checkbox(
        "Show seismic coverage (2D zones + 3D footprints)", value=False)
    show_geo = st.checkbox(
        "Show geographic entities (fields, leases, boundaries, pipelines)",
        value=False)

    zoom_to = st.radio(
        "Zoom to", ["Wells", "Everything"], horizontal=True, key="wd_zoom")

    # ── map (click a dot to pick a well) ────────────────────────────────────
    click_uwi = None
    drawn = None
    if not mapped.empty:
        import folium
        from streamlit_folium import st_folium
        # An explicit pixel height, matching st_folium's below. Without one,
        # folium wraps the map in a Figure sized by a padding-bottom RATIO of
        # the container WIDTH — on a wide screen that reserves far more than
        # the iframe needs, and the surplus shows as a blank band under the
        # map. Fixing the height removes the ratio from the calculation.
        # BLUNT FIX, and honestly a workaround rather than a diagnosis: clamp
        # the component's height in CSS. Something in the st_folium/Streamlit
        # iframe chain reserves far more vertical space than the map needs on
        # first paint, and neither sizing the folium Figure nor forcing a
        # rerun after mount changed it. Pinning the iframe AND its wrapper
        # removes the surplus whatever the cause.
        #
        # Two selectors because Streamlit's DOM has moved around: the iframe
        # title comes from the component's registered name and is stable, the
        # stCustomComponentV1 test-id covers the wrapper that holds it. An
        # unmatched selector costs nothing.
        st.markdown(
            "<style>"
            f'iframe[title="streamlit_folium.st_folium"]{{height:{MAP_H}px !important;}}'
            f'div[data-testid="stCustomComponentV1"]{{height:{MAP_H}px !important;'
            "overflow:hidden !important;}"
            "</style>", unsafe_allow_html=True)
        m = folium.Map(tiles="CartoDB positron", width="100%", height=MAP_H)
        all_lats = list(mapped["lat"])
        all_lons = list(mapped["lon"])
        # Overlay extents are tracked SEPARATELY from well extents. Seismic and
        # geographic layers are not well-bound and can sit on another continent
        # — and a mis-georeferenced survey can sit in the wrong ocean. Letting
        # them into fit_bounds zooms the map out until the wells are invisible,
        # so by default we frame the WELLS and report anything drawn outside.
        ov_lats, ov_lons = [], []
        # Highlight the currently selected well (dropdown value persists in
        # session state across reruns, so it is known here even though the
        # selectbox renders later). Highlight-only — no recenter — so the
        # user's manual pan/zoom is not reset every rerun.
        _sel_label = st.session_state.get("docwell") or ""
        _sel_uwi = _sel_label.split(" — ")[0].strip() if _sel_label else ""

        # Dot size carries the document count. SQUARE ROOT, not linear: the eye
        # reads a circle's AREA as the magnitude, so scaling the radius
        # directly would make a 4-document well look four times a 1-document
        # one when it is drawing sixteen times the ink.
        #
        # Scaled against the LARGEST count currently on the map rather than a
        # fixed ceiling, so the spread stays legible whether the busiest well
        # has three documents or ninety. Clamped at both ends: below R_MIN a
        # dot is hard to hit, above R_MAX one well swallows its neighbours.
        _counts = [int(x) for x in mapped["n_files"].fillna(1)]
        _max_n = max(_counts) if _counts else 1

        def _radius(n):
            # r = R_MAX * sqrt(n / max) keeps AREA genuinely proportional to
            # the count: double the documents, double the ink. An earlier
            # version stretched [1, max] across [R_MIN, R_MAX], which looked
            # reasonable and was not — with a busiest well of four it drew
            # thirteen times the area for four times the documents, and
            # exaggerated a one-document difference into a huge jump.
            #
            # R_MIN is a floor for clickability, so proportionality does break
            # at the very bottom when the spread is wide. That is a deliberate
            # trade: an unhittable dot is worse than a slightly overstated one.
            #
            # When every well has the SAME count there is nothing to encode,
            # so draw them all small rather than all at maximum — which is
            # what proportional scaling gives you when max == 1.
            n = max(1, int(n or 1))
            if _max_n <= 1:
                return R_MIN
            return max(R_MIN, R_MAX * (n / _max_n) ** 0.5)

        for r in mapped.itertuples():
            _is_sel = (str(r.uwi) == _sel_uwi)
            _n = int(r.n_files or 1)
            # Selection is signalled by COLOUR and stroke, not size — size now
            # means something, and overriding it would misreport the count.
            folium.CircleMarker(
                location=[r.lat, r.lon], radius=_radius(_n),
                # Outline stays dark on every dot so a pale one-file well is
                # still visible against the basemap; only the FILL carries the
                # count. Selection overrides both, since a red dot among blues
                # reads instantly and the tooltip still states the number.
                color="#B00020" if _is_sel else "#14395F", fill=True,
                fill_color="#E53935" if _is_sel else _count_colour(_n),
                fill_opacity=0.95 if _is_sel else 0.88,
                weight=3 if _is_sel else 1,
                tooltip=folium.Tooltip(
                    f"<b>{r.well_name}</b><br>{r.uwi}<br>"
                    f"{_n} file(s)"),
                popup=folium.Popup(str(r.uwi), max_width=200),
            ).add_to(m)
        if _max_n > 1:
            # Only show bands that actually occur. Testing `n <= upper` alone
            # is always true for the open-ended top band, so every swatch
            # showed — a band needs its LOWER bound as well to mean anything.
            _key, _lo = "", 0
            for _up, _c, _lbl in COUNT_BANDS:
                if any(_lo < n <= _up for n in _counts):
                    _key += (
                        f'<span style="display:inline-block;width:11px;'
                        f'height:11px;border-radius:50%;background:{_c};'
                        f'border:1px solid #14395F;vertical-align:middle;'
                        f'margin:0 3px 0 10px;"></span>{_lbl}')
                _lo = _up
            st.markdown(
                '<div style="font-size:0.8rem;opacity:.75;">Files per well:'
                + _key + '<span style="margin-left:14px;">selected well in '
                '<span style="color:#E53935;font-weight:600;">red</span>'
                '</span></div>', unsafe_allow_html=True)
        n_zones = n_foot = n_untyped = n_lines = 0
        if show_zones:
            # Real line paths first. When they exist the 2D BLOBS are
            # suppressed for those surveys — drawing both puts a fat amber
            # hull over the line it was standing in for, which reads as two
            # different pieces of information rather than one superseding the
            # other.
            _lines = seismic_lines_db(engine)
            n_lines = len(_lines)
            _line_surveys = {l["survey"] for l in _lines}
            for _l in _lines:
                folium.PolyLine(
                    locations=_l["pts"], color="#B36A00", weight=2,
                    opacity=0.9,
                    tooltip=folium.Tooltip(
                        f"<b>📈 2D line</b><br>{_l['survey']}<br>"
                        f"{_l['line']}"),
                    popup=folium.Popup(
                        f"<b>📈 2D seismic line</b><br><b>{_l['survey']}</b>"
                        f"<br>{_l['line']}<br>"
                        f"EPSG {_l['epsg'] or '—'}<br>"
                        f"{_l['traces'] or '?'} traces", max_width=280),
                ).add_to(m)
                for la, lo in _l["pts"]:
                    ov_lats.append(la)
                    ov_lons.append(lo)

            _zones = [z for z in seismic_2d_zones(engine)
                      if z["survey"] not in _line_surveys]
            _foot = seismic_3d_footprints(engine)
            _unty = seismic_untyped_footprints(engine)
            n_zones, n_foot, n_untyped = len(_zones), len(_foot), len(_unty)
            if not _zones and not _foot and not _unty:
                st.caption("No seismic coverage to show — need rows in "
                           "FILE_SEIS_HEADER with a stored bbox or "
                           "SURVEY_OUTLINE (and shapely installed for the 2D "
                           "zones and any outline-based footprint).")
            for z in _zones:
                folium.Polygon(
                    locations=z["ring"], color="#8A5A00", weight=1,
                    fill=True, fill_color="#E0A030", fill_opacity=0.15,
                    tooltip=folium.Tooltip(
                        f"<b>📈 2D seismic</b><br>{z['survey']}<br>"
                        f"{z['n_lines']} line(s)"),
                    popup=folium.Popup(
                        f"<b>📈 2D seismic survey</b><br>"
                        f"<b>{z['survey']}</b><br>{z['n_lines']} line(s)",
                        max_width=260),
                ).add_to(m)
                for la, lo in z["ring"]:
                    ov_lats.append(la)
                    ov_lons.append(lo)
            for f in _foot:
                folium.Polygon(
                    locations=f["ring"], color="#0E6E6E", weight=2,
                    fill=True, fill_color="#19A0A0", fill_opacity=0.18,
                    tooltip=folium.Tooltip(
                        f"<b>▦ 3D footprint</b><br>{f['survey']}<br>"
                        f"{f['n_lines']} file(s)"),
                    popup=folium.Popup(
                        f"<b>▦ 3D seismic footprint</b><br>"
                        f"<b>{f['survey']}</b><br>{f['n_lines']} file(s)",
                        max_width=260),
                ).add_to(m)
                for la, lo in f["ring"]:
                    ov_lats.append(la)
                    ov_lons.append(lo)
            # geometry present, type absent — dashed so it reads as provisional
            for u in _unty:
                folium.Polygon(
                    locations=u["ring"], color="#6A3FA0", weight=2,
                    dash_array="6,4",
                    fill=True, fill_color="#9C6ADE", fill_opacity=0.10,
                    tooltip=folium.Tooltip(
                        f"<b>◇ seismic (type not set)</b><br>{u['survey']}<br>"
                        f"{u['n_lines']} file(s)"),
                    popup=folium.Popup(
                        f"<b>◇ Seismic survey — SEIS_SET_TYPE not set</b><br>"
                        f"<b>{u['survey']}</b><br>{u['n_lines']} file(s)<br>"
                        f"<i>Georeferenced but unclassified; set the type to "
                        f"move it into the 2D or 3D layer.</i>",
                        max_width=280),
                ).add_to(m)
                for la, lo in u["ring"]:
                    ov_lats.append(la)
                    ov_lons.append(lo)
        n_geo = 0
        if show_geo:
            _geo = _geographic_entities(engine, c)
            n_geo = len(_geo)
            if not _geo:
                st.caption("No geographic entities to show — need rows in "
                           "GLOBAL_FILE_CATALOG with SPATIAL_OUTLINE set and "
                           "CATALOG_TABLE in FIELD / LAND_TRACT / BOUNDARY / "
                           "PIPELINE (and shapely installed to read the WKT).")
            for ge in _geo:
                stroke, fill, emoji, pretty = GEO_STYLE.get(
                    ge["kind"], ("#555555", "#999999", "▧", ge["kind"] or "Feature"))
                tip = (f"<b>{emoji} {pretty}</b><br>{ge['label']}")
                for ring in ge["rings"]:
                    folium.Polygon(
                        locations=ring, color=stroke, weight=2,
                        fill=True, fill_color=fill, fill_opacity=0.12,
                        tooltip=folium.Tooltip(tip),
                        popup=folium.Popup(tip, max_width=260),
                    ).add_to(m)
                    for la, lo in ring:
                        ov_lats.append(la)
                        ov_lons.append(lo)
                # pipelines/boundaries can be open paths — draw unfilled
                for line in ge["lines"]:
                    folium.PolyLine(
                        locations=line, color=stroke, weight=3, opacity=0.8,
                        tooltip=folium.Tooltip(tip),
                        popup=folium.Popup(tip, max_width=260),
                    ).add_to(m)
                    for la, lo in line:
                        ov_lats.append(la)
                        ov_lons.append(lo)
        # frame the WELLS; only widen to overlays if the user asks
        _fit_lats, _fit_lons = list(all_lats), list(all_lons)
        _off = 0
        if ov_lats and ov_lons:
            if zoom_to.startswith("Everything"):
                _fit_lats += ov_lats
                _fit_lons += ov_lons
            elif all_lats and all_lons:
                # count overlay vertices outside the well frame, to warn about
                # layers the user has drawn but cannot see
                _wla0, _wla1 = min(all_lats), max(all_lats)
                _wlo0, _wlo1 = min(all_lons), max(all_lons)
                _off = sum(1 for la, lo in zip(ov_lats, ov_lons)
                           if not (_wla0 <= la <= _wla1 and _wlo0 <= lo <= _wlo1))
        if _fit_lats and _fit_lons:
            m.fit_bounds([[min(_fit_lats), min(_fit_lons)],
                          [max(_fit_lats), max(_fit_lons)]])
        if n_zones or n_foot or n_geo or n_untyped or n_lines:
            _bits = []
            # Lines first — they are the better information, and a run where
            # the count is 0 while zones are non-zero is the visible signal
            # that the GeoJSON export has not been run.
            if n_lines: _bits.append(f"{n_lines} 2D line(s)")
            if n_zones: _bits.append(f"{n_zones} 2D zone(s)")
            if n_foot:  _bits.append(f"{n_foot} 3D footprint(s)")
            if n_untyped: _bits.append(f"{n_untyped} untyped survey(s)")
            if n_geo:   _bits.append(f"{n_geo} geographic entity(ies)")
            _msg = " · ".join(_bits) + " drawn."
            if zoom_to.startswith("Everything"):
                _msg += " Map zoomed to include them."
            elif _off:
                _msg += (" Some sit outside the well area — switch Zoom to "
                         "**Everything** to see them. (Overlays far from your "
                         "wells usually mean a mis-georeferenced survey: check "
                         "EPSG_CODE / bbox in FILE_SEIS_HEADER.)")
            st.caption(_msg)
        # Draw control: a rectangle/polygon selects every well (and seismic
        # survey) inside it — the multi-select the page intends.
        from folium.plugins import Draw
        Draw(export=False,
             draw_options={"polyline": False, "circle": False,
                           "circlemarker": False, "marker": False,
                           "rectangle": True, "polygon": True},
             edit_options={"edit": False}).add_to(m)
        # A STABLE key is required, not optional. Without one, streamlit-folium
        # derives the component's identity from its position in the widget
        # tree — so moving the viewer block above the map, or skipping the map
        # on some reruns, hands it a new identity each time and the frontend
        # fails to reattach ("trouble loading the streamlit_folium.st_folium
        # component"). A fixed key keeps it the same component throughout.
        sd = st_folium(m, height=MAP_H, use_container_width=True, key="docmap",
                       returned_objects=["last_object_clicked_popup",
                                         "all_drawings"])
        sd = sd or {}
        if sd.get("last_object_clicked_popup"):
            click_uwi = str(sd["last_object_clicked_popup"]).strip()
        drawn = sd.get("all_drawings")
        # Remember the selection: while the viewer is open the map isn't
        # rendered (st_folium re-serialises every marker and overlay on every
        # rerun, which is what made opening a file slow), so the lists below
        # read the drawn area from here instead.
        st.session_state["docmap_drawn"] = drawn

    # Resolve the drawn shape to a bbox ONCE, here, so everything below (the
    # seismic list AND the well selection) is constrained to the same drawn area.
    if drawn is None:
        drawn = st.session_state.get("docmap_drawn")
    draw_bbox = None
    if drawn:
        _rings = []
        for _feat in drawn:
            try:
                _geom = _feat.get("geometry", {})
                if _geom.get("type") == "Polygon":
                    _rings.append(_geom["coordinates"])
            except Exception:
                continue
        draw_bbox = _drawn_bbox(_rings) if _rings else None

    # Extensions the in-app viewer can render. Resolved ONCE here because
    # both the seismic line list and the per-well document list route on it.
    VIEW_EXTS = _viewer_exts()

    # ── seismic surveys — DRIVEN BY THE MAP SELECTION ───────────────────────
    # No selection means no list, and no query either. A production catalog
    # holds hundreds to thousands of survey lines; rendering them all is
    # unusable and pulling them all is wasted work. The map is the filter.
    import pandas as _pd
    sg = _pd.DataFrame()
    n_unplaced = 0
    if draw_bbox is None:
        st.markdown("#### 📈 Seismic surveys")
        st.caption("Draw a rectangle or polygon on the map (the ▧ tools, top-left) "
                   "to list the seismic surveys covering that area.")
    else:
        sg = _seis_fill_bounds(_qry_seismic_lines(engine))
        if not sg.empty and "bmin_lat" in sg.columns:
            mn_lat, mx_lat, mn_lon, mx_lon = draw_bbox
            _placed = sg["bmin_lat"].notna() & sg["bmin_lon"].notna()
            n_unplaced = int((~_placed).sum())
            # a survey is "in the area" if its bbox OVERLAPS the drawn bbox
            sg = sg[_placed
                    & (sg["bmin_lat"] <= mx_lat) & (sg["bmax_lat"] >= mn_lat)
                    & (sg["bmin_lon"] <= mx_lon) & (sg["bmax_lon"] >= mn_lon)
                    ].reset_index(drop=True)
        nsurv = sg["survey_name"].nunique() if not sg.empty else 0
        st.markdown(f"#### 📈 Seismic surveys in drawn area — {nsurv} survey(s) · "
                    f"{len(sg)} line(s)")
        if n_unplaced:
            st.caption(f"{n_unplaced} line(s) have no geometry and can't be tested "
                       f"against the drawn area, so they're not listed here. See "
                       f"the pipeline page's seismic coverage panel.")
        if sg.empty:
            st.caption("No seismic surveys fall in the drawn area.")
        else:
            k = 0
            # query is already ordered survey_name → line_name
            for survey, g in sg.groupby("survey_name", sort=False):
                set_types = "/".join(sorted({str(t) for t in g["set_type"]
                                             if t})) or "—"
                contractor = next((str(c) for c in g["contractor"] if c), "")
                label = (f"📈 {survey or '(unnamed survey)'}  ·  {set_types}"
                         f"  ·  {len(g)} line(s)")
                if contractor:
                    label += f"  ·  {contractor}"
                with st.expander(label, expanded=False):
                    for r in g.itertuples():
                        fp = r.file_path
                        nm = r.file_name or (os.path.basename(fp) if fp else "(line)")
                        line = str(r.line_name or "(no line name)")
                        # Route by the file's OWN extension. These rows are no
                        # longer SEG-Y only — a survey can be backed by .p190 or
                        # .shp — so the viewer choice has to follow the file.
                        _e = str(getattr(r, "file_ext", "") or
                                 os.path.splitext(nm)[1]).lower().lstrip(".")
                        _inline = _e in VIEW_EXTS
                        cc = st.columns([4.6, 1.0, 1.2, 1.2])
                        cc[0].write(f"↳ {line}"
                                    + (f"  ·  📄 {r.file_name}" if r.file_name else ""))
                        cc[1].caption(f".{_e}" if _e else "")
                        with cc[2]:
                            if st.button("View" if _inline else "Open",
                                         key=f"sv_{k}", use_container_width=True):
                                if _inline:
                                    st.session_state["docview"] = {
                                        "path": fp, "name": nm, "ext": "." + _e}
                                    st.rerun()
                                elif not fp or not os.path.exists(fp):
                                    st.warning(f"Not found on disk: {fp}")
                                else:
                                    err = _open_native(fp)
                                    if err:
                                        st.error(err)
                        with cc[3]:
                            if fp and os.path.exists(fp):
                                try:
                                    with open(fp, "rb") as _fh:
                                        st.download_button(
                                            "Download", _fh.read(), file_name=nm,
                                            key=f"sd_{k}", use_container_width=True)
                                except Exception:
                                    st.caption("unreadable")
                        k += 1

    # ── resolve the selection ────────────────────────────────────────────────
    labels = {f"{r.uwi} — {r.well_name}  ({int(r.n_files)} file(s))": str(r.uwi)
              for r in wells.itertuples()}
    options = list(labels)
    uwi_to_label = {u: lab for lab, u in labels.items()}

    # keep the stored dropdown value valid
    if st.session_state.get("docwell") not in options:
        st.session_state["docwell"] = options[0] if options else None
    # sync only a NEW dot click into the dropdown. Clicks persist across reruns,
    # so without this change-guard a stale click would keep overriding whatever
    # the user picks in the dropdown — which is exactly the "dropdown does
    # nothing" bug.
    if click_uwi and click_uwi != st.session_state.get("_last_click_uwi"):
        if click_uwi in uwi_to_label:
            st.session_state["docwell"] = uwi_to_label[click_uwi]
        st.session_state["_last_click_uwi"] = click_uwi

    # ── box-select: a drawn rectangle/polygon picks every well + seismic inside
    box_uwis = []
    box_seis = None
    if draw_bbox is not None:
        mn_lat, mx_lat, mn_lon, mx_lon = draw_bbox
        inside = mapped[
            (mapped["lat"] >= mn_lat) & (mapped["lat"] <= mx_lat) &
            (mapped["lon"] >= mn_lon) & (mapped["lon"] <= mx_lon)]
        box_uwis = [str(u) for u in inside["uwi"].tolist()]
        try:
            box_seis = _qry_seismic_in_bbox(engine, mn_lat, mx_lat,
                                            mn_lon, mx_lon)
        except Exception:
            box_seis = None
        ns = 0 if box_seis is None else len(box_seis)
        st.success(f"Drawn area selects {len(box_uwis)} well(s) and "
                   f"{ns} seismic survey(s).")

    if draw_bbox is not None:
        # a box is on the map — honor it exactly, even if it selects nothing
        # (don't silently fall back to the dropdown's last well, which looks
        # like the map picked wells it didn't).
        selected = box_uwis
        if box_uwis:
            st.caption("Showing documents for the wells in the drawn area. Clear "
                       "the drawing (trash icon on the map) to go back to "
                       "single-pick.")
        else:
            st.info("The drawn area contains no wells. Drag the box over some "
                    "well dots, or clear the drawing to pick from the dropdown.")
        if box_seis is not None and len(box_seis):
            with st.expander(f"📈 Seismic in drawn area ({len(box_seis)})",
                             expanded=True):
                st.dataframe(box_seis, use_container_width=True,
                             hide_index=True)
    else:
        choice = st.selectbox("Well — pick one, or click a dot on the map",
                              options, key="docwell")
        selected = [labels[choice]]

    # ── documents table ──────────────────────────────────────────────────────
    docs = _documents_for(engine, selected, c, doc_only=doc_only).reset_index(drop=True)
    if c["type"] and not docs.empty:
        types = sorted(t for t in docs["doc_type"].dropna().unique())
        if types:
            picks = st.multiselect("Filter by document type", types,
                                   default=types, key="doc_type_filter")
            docs = docs[docs["doc_type"].isin(picks)].reset_index(drop=True)

    st.markdown(f"### Documents — {len(selected)} well(s) · {len(docs)} file(s)")
    if docs.empty:
        st.info("No documents for the current selection.")
        return

    MAXROWS = 500
    shown = docs.head(MAXROWS).reset_index(drop=True)
    if len(docs) > MAXROWS:
        st.warning(f"Showing the first {MAXROWS} of {len(docs)} files — narrow by "
                   f"type or draw a smaller area.")

    def _fname(n, p):
        return n or (os.path.basename(p) if p else "(unnamed)")

    # native grid (no page-wide CSS): tick the rows you want, act on them below
    grid = pd.DataFrame({
        "Open?": [False] * len(shown),
        "Well": shown["well_name"].fillna(shown["uwi"]).astype(str),
        "File": [(("📦 " if l == "vault" else "🌐 " if l == "network" else "")
                  + _fname(n, fp))
                 for l, n, fp in zip(shown["loc"], shown["file_name"],
                                     shown["file_path"])],
    })
    # The grid lives in a FORM: widgets inside a form don't trigger a rerun on
    # every keystroke/tick, so ticking six rows costs one rerun at the end
    # rather than six full page rebuilds (each of which re-renders the map).
    # Selections are kept in session state and invalidated by a signature over
    # the visible files, so changing the filters can't leave stale row indexes
    # pointing at different documents.
    _sig = hash(tuple(shown["file_path"].astype(str)))
    if st.session_state.get("doc_sel_sig") != _sig:
        st.session_state["doc_sel_sig"] = _sig
        st.session_state["doc_sel"] = []

    with st.form("doc_select", border=False):
        edited = st.data_editor(
            grid, hide_index=True, use_container_width=True, key="docgrid",
            column_config={
                "Open?": st.column_config.CheckboxColumn("Open?", default=False,
                                                         width="small"),
                "Well": st.column_config.TextColumn("Well", disabled=True),
                "File": st.column_config.TextColumn("File", disabled=True,
                                                    width="large"),
            },
            disabled=["Well", "File"],
        )
        _apply = st.form_submit_button("✓ Use selected files", type="primary")
    if _apply:
        try:
            st.session_state["doc_sel"] = [
                i for i, v in enumerate(edited["Open?"].tolist()) if v]
        except Exception:
            st.session_state["doc_sel"] = []
    checked = st.session_state.get("doc_sel", [])

    st.markdown("#### Open / view selected")
    if not checked:
        st.caption("Tick the rows you want. LAS / LIS / DLIS / SEG-Y open in the "
                   "in-app viewer (the File Catalog's Browse & view); documents "
                   "open in their native app. Uses the vault copy (📦) when "
                   "available, otherwise the network file (🌐).")
    else:
        for j in checked:
            d = shown.iloc[j]
            fp = d["file_path"]
            nm = _fname(d["file_name"], fp)
            tag = "📦" if d["loc"] == "vault" else "🌐"
            ext = str(d["file_ext"] or os.path.splitext(nm)[1]).lower().lstrip(".")
            is_view = ext in VIEW_EXTS
            col = st.columns([5, 1.4, 1.6])
            col[0].write(f"{tag} {d['well_name'] or d['uwi']} — {nm}")
            with col[1]:
                if st.button("View" if is_view else "Open", key=f"o_{j}",
                             use_container_width=True):
                    if is_view:
                        st.session_state["docview"] = {
                            "path": fp, "name": nm, "ext": "." + ext}
                        st.rerun()
                    elif not fp or not os.path.exists(fp):
                        st.warning(f"Not found on disk: {fp}")
                    else:
                        err = _open_native(fp)
                        if err:
                            st.error(err)
                        elif hasattr(st, "toast"):
                            st.toast(f"Opened {nm}")
                        else:
                            st.success(f"Opened {nm}")
            with col[2]:
                if fp and os.path.exists(fp):
                    try:
                        with open(fp, "rb") as fh:
                            st.download_button("Download", fh.read(), file_name=nm,
                                               key=f"d_{j}", use_container_width=True)
                    except Exception as e:
                        st.caption(f"unreadable: {str(e)[:40]}")
                else:
                    st.caption("not on disk")

    # ── in-app viewer for LAS / LIS / DLIS / SEG-Y / docs ────────────────────
    dv = st.session_state.get("docview")
    if dv:
        st.divider()
        h1, h2 = st.columns([6, 1])
        h1.markdown(f"#### 🔬 Viewer — {dv.get('name', '')}")
        if h2.button("✕ Close", key="docview_close", use_container_width=True):
            st.session_state.pop("docview", None)
            st.rerun()
        pth = dv.get("path")
        if not pth:
            st.warning("No file path was recorded for this item in the catalog.")
        elif not os.path.exists(pth):
            st.error(f"File not found where the catalog points:\n\n`{pth}`\n\n"
                     "The path may be a network location this machine can't reach.")
        else:
            try:
                try:
                    from dataview.file_catalog.file_viewer import view as _view_file
                except ImportError:
                    from dataview.file_catalog.file_viewer import view as _view_file
                _view_file(pth, dv.get("ext"))
            except Exception as e:
                import traceback
                st.error(f"In-app viewer error: "
                         f"{str(e).splitlines()[0][:200]}")
                with st.expander("details"):
                    st.code(traceback.format_exc())
                if st.button("Open in native app instead", key="docview_native"):
                    err = _open_native(pth)
                    if err:
                        st.error(err)
