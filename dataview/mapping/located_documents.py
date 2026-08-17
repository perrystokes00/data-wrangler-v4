"""
located_documents.py — documents that have a LOCATION, as points or
footprints.

The point path (FILE_WELL_HEADER.LATITUDE/LONGITUDE) is handled by the
map page. This adds the OUTLINE documents it cannot show: field, lease
and pipeline shapefiles carry a footprint WKT in
GLOBAL_FILE_CATALOG.SPATIAL_OUTLINE, not a single coordinate.

WAS A PASTE FRAGMENT. Its header said "paste both functions into
page_well_map_docs.py", and nothing ever did — the functions are not in
that file or any other. Meanwhile it sat in the package looking like a
module, referencing pd/text/folium it never imported, so ANY import of
it raised NameError before reaching a single line of logic. The
annotation `-> pd.DataFrame` is evaluated when the def executes, which
is why it failed at import rather than at call time.

Now a real module: import and call it.

    from dataview.mapping.located_documents import (
        qry_catalog_outlines, add_catalog_outline_overlay)
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import text

try:
    import folium
except Exception:                      # the map layer is optional here
    folium = None


def qry_catalog_outlines(_engine) -> pd.DataFrame:
    """Documents whose location is a footprint (SPATIAL_OUTLINE WKT), not a point.

    These are shapefile/seismic docs: field/lease/pipeline polygons & lines that
    have no single lat/long. One row per file, WKT carried through for the map.

    Returns columns: file_name, file_path, type_group, doc_type, readiness,
    matched_uwi, wkt.  Empty DataFrame on any failure.
    """
    try:
        df = pd.read_sql(text("""
            SELECT g.FILE_NAME        AS file_name,
                   g.FILE_PATH        AS file_path,
                   g.FILE_TYPE_GROUP  AS type_group,
                   g.CATALOG_TABLE    AS doc_type,
                   g.CATALOG_READINESS AS readiness,
                   g.MATCHED_UWI      AS matched_uwi,
                   CAST(g.SPATIAL_OUTLINE AS NVARCHAR(MAX)) AS wkt
            FROM file_catalog.GLOBAL_FILE_CATALOG g
            WHERE g.SPATIAL_OUTLINE IS NOT NULL
              AND LEN(CAST(g.SPATIAL_OUTLINE AS NVARCHAR(MAX))) > 0
        """), _engine)
        return df
    except Exception:
        return pd.DataFrame(columns=[
            "file_name", "file_path", "type_group", "doc_type",
            "readiness", "matched_uwi", "wkt"])


def add_catalog_outline_overlay(m, outline_df: "pd.DataFrame") -> int:
    """Draw footprint documents as polygon/line outlines on their own layer.

    Mirrors _add_catalog_overlay's shape (FeatureGroup, returns a count), but for
    WKT geometry instead of points. Uses shapely→GeoJSON like the geography
    layers; WKT is (lon lat) → GeoJSON [lon,lat], which folium wants, so no swap.
    Invalid/unparseable WKT is skipped, never fatal.
    """
    if outline_df is None or outline_df.empty or folium is None:
        return 0
    import shapely.wkt
    from shapely.geometry import mapping

    # colour by feature type so a lease vs field vs pipeline reads at a glance
    _COLOR = {"FIELD": "#e67e22", "LAND_TRACT": "#3498db",
              "PIPELINE": "#c0392b", "BOUNDARY": "#7f8c8d"}

    feats = []
    for _, r in outline_df.iterrows():
        wkt = r.get("wkt")
        if not wkt:
            continue
        try:
            geom = shapely.wkt.loads(str(wkt))
        except Exception:
            continue
        feats.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {
                "file_name": str(r.get("file_name") or ""),
                "type_group": str(r.get("type_group") or ""),
                "doc_type": str(r.get("doc_type") or ""),
                "readiness": str(r.get("readiness") or ""),
                "_color": _COLOR.get(str(r.get("doc_type") or "").upper(), "#8e44ad"),
            },
        })
    if not feats:
        return 0

    gj = {"type": "FeatureCollection", "features": feats}
    fg = folium.FeatureGroup(name=f"📄 Doc footprints ({len(feats)})", show=True)
    folium.GeoJson(
        gj,
        style_function=lambda f: {
            "color": f["properties"]["_color"], "weight": 2,
            "fillColor": f["properties"]["_color"], "fillOpacity": 0.20,
            "opacity": 0.85},
        highlight_function=lambda f: {"weight": 4},
        tooltip=folium.GeoJsonTooltip(
            fields=["file_name", "type_group", "doc_type", "readiness"],
            aliases=["File", "Type", "Feature", "Readiness"], sticky=True),
    ).add_to(fg)
    fg.add_to(m)
    return len(feats)


# ── LIST: combine point + outline docs into one "documents with a location" table
def qry_located_documents(_engine, point_query=None) -> "pd.DataFrame":
    """Every document that has a location — points AND footprints — one table.

    Unions _qry_catalog (point docs: lat/long) with _qry_catalog_outlines
    (footprint docs: WKT), tagging each row's location_kind so the list shows
    both and the map knows how to draw each. For the list/table view.
    """
    # point_query is the caller's own point fetcher. The original wrote
    # `_qry_catalog(_engine)` — a name that exists in the module this was
    # meant to be pasted into and NOWHERE in the codebase, so calling this
    # would have raised NameError. Passing it in makes the dependency
    # visible instead of implicit.
    pts = point_query(_engine) if point_query else None
    if pts is not None and not pts.empty:
        pts = pts.copy()
        pts["location_kind"] = "POINT"
        pts["location"] = (pts["latitude"].round(5).astype(str) + ", "
                           + pts["longitude"].round(5).astype(str))
        pts = pts[["file_name", "type_group", "well_name", "location_kind",
                   "location", "file_path"]]
    outs = qry_catalog_outlines(_engine)
    if outs is not None and not outs.empty:
        outs = outs.copy()
        outs["location_kind"] = "OUTLINE"
        outs["well_name"] = outs["matched_uwi"]
        outs["location"] = outs["doc_type"].fillna("") + " footprint"
        outs = outs[["file_name", "type_group", "well_name", "location_kind",
                     "location", "file_path"]]
    frames = [d for d in (pts, outs) if d is not None and not d.empty]
    if not frames:
        return pd.DataFrame(columns=["file_name", "type_group", "well_name",
                                     "location_kind", "location", "file_path"])
    return pd.concat(frames, ignore_index=True)
