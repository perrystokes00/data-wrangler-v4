"""
modules/dv_spatial_loader.py
============================
DataView v3 — Spatial Layer Loader

Imports shapefiles (.shp) and GeoJSON files into dataview.dv_spatial_layer,
storing the full geometry as a GeoJSON FeatureCollection string in
geometry_wkt (NVARCHAR MAX).

The Well Map reads layers directly from the DB — no files needed at runtime.

Usage:
    from dataview.mapping.dv_spatial_loader import import_shapefile, list_layers, delete_layer

    result = import_shapefile(
        engine, path="C:/spatial/Seismic_2D_Lines_Permian.shp",
        layer_name="2D Seismic Lines",
        layer_category="SEISMIC",
        style={"color": "#FF6B35", "weight": 2, "dash": "6 4"},
        tooltip_fields=["LINE_NAME", "SURVEY", "ACQ_YEAR", "LENGTH_KM"],
    )
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import text


# =============================================================================
# DEFAULT STYLES PER CATEGORY
# =============================================================================

CATEGORY_DEFAULTS = {
    "SEISMIC_2D": {
        "color": "#FF6B35", "weight": 2.0, "opacity": 0.85,
        "fill_color": None, "fill_opacity": 0.0, "dash": "6 4",
    },
    "SEISMIC_3D": {
        "color": "#7B2D8B", "weight": 2.0, "opacity": 0.9,
        "fill_color": "#C490D1", "fill_opacity": 0.15, "dash": None,
    },
    "WELL": {
        "color": "#E24B4A", "weight": 1.5, "opacity": 0.9,
        "fill_color": "#E24B4A", "fill_opacity": 0.7, "dash": None,
    },
    "LEASE": {
        "color": "#2196F3", "weight": 1.5, "opacity": 0.8,
        "fill_color": "#90CAF9", "fill_opacity": 0.12, "dash": None,
    },
    "FIELD": {
        "color": "#4CAF50", "weight": 2.0, "opacity": 0.8,
        "fill_color": "#A5D6A7", "fill_opacity": 0.15, "dash": None,
    },
    "PIPELINE": {
        "color": "#795548", "weight": 2.5, "opacity": 0.9,
        "fill_color": None, "fill_opacity": 0.0, "dash": None,
    },
    "BOUNDARY": {
        "color": "#607D8B", "weight": 1.0, "opacity": 0.7,
        "fill_color": None, "fill_opacity": 0.0, "dash": "3 3",
    },
    "BASIN": {
        "color": "#FF9800", "weight": 2.0, "opacity": 0.8,
        "fill_color": "#FFE0B2", "fill_opacity": 0.1, "dash": None,
    },
    "OTHER": {
        "color": "#9E9E9E", "weight": 1.5, "opacity": 0.8,
        "fill_color": "#EEEEEE", "fill_opacity": 0.1, "dash": None,
    },
}

LAYER_CATEGORY_DISPLAY = {
    "SEISMIC_2D": "🟠 Seismic 2D",
    "SEISMIC_3D": "🟣 Seismic 3D",
    "WELL":       "🛢 Wells",
    "LEASE":      "📋 Leases",
    "FIELD":      "🌿 Fields",
    "PIPELINE":   "🟤 Pipelines",
    "BOUNDARY":   "🔲 Boundaries",
    "BASIN":      "🏔 Basins",
    "OTHER":      "📁 Other",
}


# =============================================================================
# HELPERS
# =============================================================================

def _layer_id(path: str) -> str:
    return hashlib.sha1(str(path).encode()).hexdigest()[:40]


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# =============================================================================
# IMPORT FUNCTIONS
# =============================================================================

def import_shapefile(engine, path: str,
                     layer_name: str = "",
                     layer_category: str = "OTHER",
                     style: dict | None = None,
                     tooltip_fields: list[str] | None = None,
                     display_order: int = 99,
                     source: str = "DATAVIEW") -> dict:
    """
    Read a shapefile and store as GeoJSON in dv_spatial_layer.

    path            : full path to .shp file
    layer_name      : display name (default: filename stem)
    layer_category  : SEISMIC_2D, SEISMIC_3D, WELL, LEASE, FIELD,
                      PIPELINE, BOUNDARY, BASIN, OTHER
    style           : override dict with color, weight, opacity,
                      fill_color, fill_opacity, dash keys
    tooltip_fields  : list of attribute column names to show on hover
    display_order   : sidebar sort position

    Returns: {"loaded": int, "layer_id": str, "errors": [...]}
    """
    try:
        import geopandas as gpd
    except ImportError:
        return {"loaded": 0, "errors": ["geopandas not installed: pip install geopandas"],
                "layer_id": None}

    path = str(path)
    p    = Path(path)

    if not p.exists():
        return {"loaded": 0, "errors": [f"File not found: {path}"], "layer_id": None}

    # Read shapefile
    try:
        gdf = gpd.read_file(path).to_crs("EPSG:4326")
    except Exception as exc:
        return {"loaded": 0, "errors": [f"Could not read shapefile: {exc}"],
                "layer_id": None}

    return _import_geodataframe(
        engine, gdf,
        layer_id=_layer_id(path),
        layer_name=layer_name or p.stem.replace("_", " "),
        layer_type=_detect_geom_type(gdf),
        layer_category=layer_category.upper(),
        file_path=path,
        style=style,
        tooltip_fields=tooltip_fields,
        display_order=display_order,
        source=source,
    )


def import_geojson(engine, path: str,
                   layer_name: str = "",
                   layer_category: str = "OTHER",
                   style: dict | None = None,
                   tooltip_fields: list[str] | None = None,
                   display_order: int = 99,
                   source: str = "DATAVIEW") -> dict:
    """
    Read a GeoJSON file and store in dv_spatial_layer.
    """
    try:
        import geopandas as gpd
    except ImportError:
        return {"loaded": 0, "errors": ["geopandas not installed"],
                "layer_id": None}

    path = str(path)
    p    = Path(path)

    if not p.exists():
        return {"loaded": 0, "errors": [f"File not found: {path}"], "layer_id": None}

    try:
        gdf = gpd.read_file(path).to_crs("EPSG:4326")
    except Exception as exc:
        return {"loaded": 0, "errors": [f"Could not read GeoJSON: {exc}"],
                "layer_id": None}

    return _import_geodataframe(
        engine, gdf,
        layer_id=_layer_id(path),
        layer_name=layer_name or p.stem.replace("_", " "),
        layer_type=_detect_geom_type(gdf),
        layer_category=layer_category.upper(),
        file_path=path,
        style=style,
        tooltip_fields=tooltip_fields,
        display_order=display_order,
        source=source,
    )


def _detect_geom_type(gdf) -> str:
    types = gdf.geometry.geom_type.unique().tolist()
    if any("Polygon" in t for t in types):
        return "POLYGON"
    if any("Line" in t for t in types):
        return "LINE"
    return "POINT"


def _import_geodataframe(engine, gdf, layer_id: str, layer_name: str,
                         layer_type: str, layer_category: str,
                         file_path: str, style: dict | None,
                         tooltip_fields: list[str] | None,
                         display_order: int, source: str) -> dict:
    """Core import — converts GDF to GeoJSON and writes to dv_spatial_layer."""

    # Merge style with category defaults
    defaults = CATEGORY_DEFAULTS.get(layer_category, CATEGORY_DEFAULTS["OTHER"])
    merged   = {**defaults, **(style or {})}

    # Build GeoJSON FeatureCollection
    geojson_str = gdf.to_json()

    # Bounding box
    bounds = gdf.total_bounds  # [minx, miny, maxx, maxy]

    # Tooltip fields
    tt_fields = ",".join(tooltip_fields) if tooltip_fields else ",".join(
        [c for c in gdf.columns if c != "geometry"][:4]
    )

    sql = """
        MERGE dataview.dv_spatial_layer AS tgt
        USING (SELECT :lid AS layer_id) AS src
        ON tgt.layer_id = src.layer_id
        WHEN MATCHED THEN UPDATE SET
            layer_name        = :lname,
            layer_type        = :ltype,
            layer_category    = :lcat,
            source_type       = :stype,
            file_path         = :fpath,
            feature_count     = :fcnt,
            bbox_min_lat      = :minlat,
            bbox_max_lat      = :maxlat,
            bbox_min_lon      = :minlon,
            bbox_max_lon      = :maxlon,
            geometry_wkt      = :geojson,
            style_color       = :color,
            style_weight      = :weight,
            style_opacity     = :opacity,
            style_fill_color  = :fill_color,
            style_fill_opacity= :fill_opacity,
            style_dash        = :dash,
            tooltip_fields    = :ttfields,
            display_order     = :dorder,
            active_ind        = 'Y',
            row_changed_by    = 'IMPORTER',
            row_changed_date  = GETDATE(),
            source            = :src
        WHEN NOT MATCHED THEN INSERT (
            layer_id, layer_name, layer_type, layer_category,
            source_type, file_path, feature_count,
            bbox_min_lat, bbox_max_lat, bbox_min_lon, bbox_max_lon,
            geometry_wkt,
            style_color, style_weight, style_opacity,
            style_fill_color, style_fill_opacity, style_dash,
            tooltip_fields, display_order,
            active_ind, row_created_by, row_created_date, source
        ) VALUES (
            :lid, :lname, :ltype, :lcat,
            :stype, :fpath, :fcnt,
            :minlat, :maxlat, :minlon, :maxlon,
            :geojson,
            :color, :weight, :opacity,
            :fill_color, :fill_opacity, :dash,
            :ttfields, :dorder,
            'Y', 'IMPORTER', GETDATE(), :src
        );
    """

    try:
        with engine.begin() as con:
            con.execute(text(sql), {
                "lid":          layer_id,
                "lname":        layer_name[:255],
                "ltype":        layer_type,
                "lcat":         layer_category[:40],
                "stype":        "GEOJSON",
                "fpath":        file_path[:1000],
                "fcnt":         len(gdf),
                "minlat":       _safe_float(bounds[1]),
                "maxlat":       _safe_float(bounds[3]),
                "minlon":       _safe_float(bounds[0]),
                "maxlon":       _safe_float(bounds[2]),
                "geojson":      geojson_str,
                "color":        merged.get("color", "#888888")[:20],
                "weight":       _safe_float(merged.get("weight", 1.5)),
                "opacity":      _safe_float(merged.get("opacity", 0.8)),
                "fill_color":   (merged.get("fill_color") or "")[:20] or None,
                "fill_opacity": _safe_float(merged.get("fill_opacity", 0.0)),
                "dash":         (merged.get("dash") or "")[:20] or None,
                "ttfields":     tt_fields[:500],
                "dorder":       display_order,
                "src":          source[:40],
            })
        return {"loaded": len(gdf), "layer_id": layer_id, "errors": []}
    except Exception as exc:
        return {"loaded": 0, "layer_id": layer_id, "errors": [str(exc)]}


# =============================================================================
# QUERY FUNCTIONS (used by page_well_map.py)
# =============================================================================

def list_layers(engine) -> list[dict]:
    """Return all active layers from dv_spatial_layer, ordered for display."""
    sql = """
        SELECT
            layer_id, layer_name, layer_type, layer_category,
            source_type, file_path,
            feature_count, bbox_min_lat, bbox_max_lat,
            bbox_min_lon, bbox_max_lon,
            style_color, style_weight, style_opacity,
            style_fill_color, style_fill_opacity, style_dash,
            tooltip_fields, display_order, remark
        FROM dataview.dv_spatial_layer
        WHERE active_ind = 'Y'
        ORDER BY display_order, layer_category, layer_name
    """
    try:
        with engine.connect() as con:
            rows = con.execute(text(sql)).fetchall()
            cols = ["layer_id","layer_name","layer_type","layer_category",
                    "source_type","file_path",
                    "feature_count","bbox_min_lat","bbox_max_lat",
                    "bbox_min_lon","bbox_max_lon",
                    "style_color","style_weight","style_opacity",
                    "style_fill_color","style_fill_opacity","style_dash",
                    "tooltip_fields","display_order","remark"]
            return [dict(zip(cols, r)) for r in rows]
    except Exception:
        return []


def get_layer_geojson(engine, layer_id: str) -> str | None:
    """Fetch the GeoJSON string for a specific layer."""
    sql = """
        SELECT geometry_wkt
        FROM dataview.dv_spatial_layer
        WHERE layer_id = :lid AND active_ind = 'Y'
    """
    try:
        with engine.connect() as con:
            row = con.execute(text(sql), {"lid": layer_id}).fetchone()
            return row[0] if row else None
    except Exception:
        return None


def delete_layer(engine, layer_id: str) -> bool:
    """Remove a layer from dv_spatial_layer."""
    try:
        with engine.begin() as con:
            con.execute(text(
                "DELETE FROM dataview.dv_spatial_layer WHERE layer_id = :lid"
            ), {"lid": layer_id})
        return True
    except Exception:
        return False


def toggle_layer(engine, layer_id: str, active: bool) -> bool:
    """Enable or disable a layer without deleting it."""
    try:
        with engine.begin() as con:
            con.execute(text("""
                UPDATE dataview.dv_spatial_layer
                SET active_ind = :ind, row_changed_date = GETDATE()
                WHERE layer_id = :lid
            """), {"ind": "Y" if active else "N", "lid": layer_id})
        return True
    except Exception:
        return False


# =============================================================================
# BULK IMPORT — load all shapefiles from a folder
# =============================================================================

# Default category guesses from filename keywords
_FILENAME_CATEGORY_MAP = [
    (["2d", "2d_line", "seismic_2d"],           "SEISMIC_2D",  1),
    (["3d", "3d_surv", "seismic_3d"],           "SEISMIC_3D",  2),
    (["pipeline", "pipe", "flowline"],           "PIPELINE",    5),
    (["lease", "leases"],                        "LEASE",       4),
    (["field", "fields", "oil_field"],           "FIELD",       3),
    (["basin", "basins"],                        "BASIN",       6),
    (["county", "counties", "boundary"],         "BOUNDARY",    7),
    (["well", "wells", "uwi", "borehole"],       "WELL",        8),
]

def _guess_category(filename: str) -> tuple[str, int]:
    fn = filename.lower()
    for keywords, cat, order in _FILENAME_CATEGORY_MAP:
        if any(k in fn for k in keywords):
            return cat, order
    return "OTHER", 99


def register_shapefile(engine, path: str,
                       layer_name: str = "",
                       layer_category: str = "OTHER",
                       style: dict | None = None,
                       tooltip_fields: list[str] | None = None,
                       display_order: int = 99,
                       source: str = "DATAVIEW") -> dict:
    """
    Register a shapefile path in dv_spatial_layer WITHOUT importing geometry.
    source_type = SHAPEFILE — geometry is read from disk at map render time.

    Faster than import_shapefile for large files.
    File must remain accessible at the registered path.
    """
    try:
        import geopandas as gpd
    except ImportError:
        return {"loaded": 0, "errors": ["geopandas not installed"], "layer_id": None}

    path = str(path)
    p    = Path(path)
    if not p.exists():
        return {"loaded": 0, "errors": [f"File not found: {path}"], "layer_id": None}

    try:
        gdf = gpd.read_file(path).to_crs("EPSG:4326")
    except Exception as exc:
        return {"loaded": 0, "errors": [f"Could not read: {exc}"], "layer_id": None}

    defaults   = CATEGORY_DEFAULTS.get(layer_category.upper(), CATEGORY_DEFAULTS["OTHER"])
    merged     = {**defaults, **(style or {})}
    bounds     = gdf.total_bounds
    geom_type  = _detect_geom_type(gdf)
    layer_id   = _layer_id(path)
    tt_fields  = ",".join(tooltip_fields) if tooltip_fields else ",".join(
        [c for c in gdf.columns if c != "geometry"][:4]
    )

    sql = """
        MERGE dataview.dv_spatial_layer AS tgt
        USING (SELECT :lid AS layer_id) AS src
        ON tgt.layer_id = src.layer_id
        WHEN MATCHED THEN UPDATE SET
            layer_name        = :lname,
            layer_type        = :ltype,
            layer_category    = :lcat,
            source_type       = 'SHAPEFILE',
            file_path         = :fpath,
            feature_count     = :fcnt,
            bbox_min_lat      = :minlat,
            bbox_max_lat      = :maxlat,
            bbox_min_lon      = :minlon,
            bbox_max_lon      = :maxlon,
            geometry_wkt      = NULL,
            style_color       = :color,
            style_weight      = :weight,
            style_opacity     = :opacity,
            style_fill_color  = :fill_color,
            style_fill_opacity= :fill_opacity,
            style_dash        = :dash,
            tooltip_fields    = :ttfields,
            display_order     = :dorder,
            active_ind        = 'Y',
            row_changed_by    = 'IMPORTER',
            row_changed_date  = GETDATE(),
            source            = :src
        WHEN NOT MATCHED THEN INSERT (
            layer_id, layer_name, layer_type, layer_category,
            source_type, file_path, feature_count,
            bbox_min_lat, bbox_max_lat, bbox_min_lon, bbox_max_lon,
            geometry_wkt,
            style_color, style_weight, style_opacity,
            style_fill_color, style_fill_opacity, style_dash,
            tooltip_fields, display_order,
            active_ind, row_created_by, row_created_date, source
        ) VALUES (
            :lid, :lname, :ltype, :lcat,
            'SHAPEFILE', :fpath, :fcnt,
            :minlat, :maxlat, :minlon, :maxlon,
            NULL,
            :color, :weight, :opacity,
            :fill_color, :fill_opacity, :dash,
            :ttfields, :dorder,
            'Y', 'IMPORTER', GETDATE(), :src
        );
    """
    try:
        with engine.begin() as con:
            con.execute(text(sql), {
                "lid":          layer_id,
                "lname":        (layer_name or p.stem.replace("_", " "))[:255],
                "ltype":        geom_type,
                "lcat":         layer_category.upper()[:40],
                "fpath":        path[:1000],
                "fcnt":         len(gdf),
                "minlat":       _safe_float(bounds[1]),
                "maxlat":       _safe_float(bounds[3]),
                "minlon":       _safe_float(bounds[0]),
                "maxlon":       _safe_float(bounds[2]),
                "color":        merged.get("color", "#888888")[:20],
                "weight":       _safe_float(merged.get("weight", 1.5)),
                "opacity":      _safe_float(merged.get("opacity", 0.8)),
                "fill_color":   (merged.get("fill_color") or "")[:20] or None,
                "fill_opacity": _safe_float(merged.get("fill_opacity", 0.0)),
                "dash":         (merged.get("dash") or "")[:20] or None,
                "ttfields":     tt_fields[:500],
                "dorder":       display_order,
                "src":          source[:40],
            })
        return {"loaded": len(gdf), "layer_id": layer_id, "errors": [],
                "source_type": "SHAPEFILE"}
    except Exception as exc:
        return {"loaded": 0, "layer_id": layer_id, "errors": [str(exc)]}


def list_source_layers(path: str, max_depth: int = 4) -> list[dict]:
    """What is inside a spatial source, without loading any of it.

    Handles the three shapes a source arrives in: a .shp or .geojson FILE
    (one layer), a .gdb DIRECTORY (many), and a folder of files. The File
    Catalog cannot do the middle one at all -- it walks files, and a
    geodatabase is a directory of a00000001.gdbtable, so it sees the parts
    and never the whole.

    Returns [{layer, geometry, features, crs, crs_ok, path}]. crs_ok is False
    when the CRS is missing, because a layer that cannot be reprojected must
    be refused rather than guessed at: the RMOTC geodatabase is NAD27 Wyoming
    State Plane in FEET, and read as degrees it lands in the Gulf of Guinea.
    """
    import os
    out = []
    try:
        import fiona
    except ImportError:
        return out

    def _probe(src, layer=None):
        try:
            with fiona.open(src, layer=layer) if layer else fiona.open(src) as s:
                geom = (s.schema or {}).get("geometry")
                return {"layer": layer or os.path.basename(src),
                        "geometry": geom, "features": len(s),
                        "crs": str(s.crs)[:60] if s.crs else None,
                        "crs_ok": bool(s.crs),
                        "props": list((s.schema or {}).get("properties") or {}),
                        "path": src}
        except Exception:
            return None

    if os.path.isdir(path) and path.lower().endswith(".gdb"):
        try:
            for lay in fiona.listlayers(path):
                r = _probe(path, lay)
                if r and r["geometry"] and r["geometry"] != "None":
                    out.append(r)
        except Exception:
            pass
    elif os.path.isdir(path):
        # WALK SUBFOLDERS. A one-level listing found nothing at
        # ...\DataSets\GIS, because the geodatabase sits in CD_files one level
        # down -- and "0 layers" on a folder that plainly contains GIS data
        # reads as "unsupported" rather than "look deeper". Bounded, and a
        # .gdb is a LEAF: it is a source, not a folder to descend into, or its
        # internals get probed as if they were layers.
        for root, dirs, files in os.walk(path):
            depth = root[len(path):].count(os.sep)
            if depth >= max_depth:
                dirs[:] = []
            for dname in list(dirs):
                if dname.lower().endswith(".gdb"):
                    dirs.remove(dname)
                    out.extend(list_source_layers(os.path.join(root, dname)))
                elif dname.startswith((".", "$")):
                    dirs.remove(dname)
            for f in sorted(files):
                if f.lower().endswith((".shp", ".geojson", ".json")):
                    r = _probe(os.path.join(root, f))
                    if r and r.get("geometry"):
                        out.append(r)
    else:
        r = _probe(path)
        if r:
            out.append(r)
    return out


def import_layer(engine, path: str, layer: str | None = None,
                 layer_name: str = "", layer_category: str = "OTHER",
                 style: dict | None = None,
                 tooltip_fields: list | None = None,
                 display_order: int = 99,
                 source: str = "SHAPEFILE") -> dict:
    """Load ONE layer from a shapefile, GeoJSON or geodatabase.

    import_shapefile handles a whole file; this handles a named layer inside a
    multi-layer source, which is the only way to take 12 useful layers out of
    a 137-layer geodatabase.

    Reprojects to WGS84 and REFUSES a layer with no CRS. Datetime columns are
    stringified first: gdf.to_json() raises "Object of type Timestamp is not
    JSON serializable" on any date attribute, and one such column took down a
    1,401-feature well layer while eleven others loaded.

    source must already exist in dv_r_source -- it is an FK, and an import has
    no business minting standards vocabulary.
    """
    import os
    try:
        import geopandas as gpd
    except ImportError:
        return {"loaded": 0, "layer_id": None,
                "errors": ["geopandas not installed"]}
    try:
        gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    except Exception as exc:
        return {"loaded": 0, "layer_id": None,
                "errors": ["could not read: %s" % exc]}
    if gdf.empty or "geometry" not in gdf or gdf.geometry.isna().all():
        return {"loaded": 0, "layer_id": None, "errors": ["no geometry"]}
    if gdf.crs is None:
        return {"loaded": 0, "layer_id": None,
                "errors": ["no CRS — refused rather than guessed at"]}
    try:
        gdf = gdf.to_crs("EPSG:4326")
    except Exception as exc:
        return {"loaded": 0, "layer_id": None,
                "errors": ["reprojection failed: %s" % exc]}
    for c in gdf.columns:
        if c != "geometry" and str(gdf[c].dtype).startswith("datetime"):
            gdf[c] = gdf[c].astype(str)
    key = os.path.join(path, layer) if layer else path
    return _import_geodataframe(
        engine, gdf,
        layer_id=_layer_id(key),
        layer_name=layer_name or (layer or Path(path).stem).replace("_", " "),
        layer_type=_detect_geom_type(gdf),
        layer_category=(layer_category or "OTHER").upper(),
        file_path=key, style=style,
        tooltip_fields=[t for t in (tooltip_fields or []) if t in gdf.columns] or None,
        display_order=display_order, source=source)

def import_folder(engine, folder: str,
                  extensions: tuple[str, ...] = (".shp", ".geojson", ".json"),
                  source: str = "DATAVIEW") -> dict:
    """
    Bulk import all spatial files from a folder into dv_spatial_layer.
    Returns summary dict.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return {"imported": 0, "errors": [f"Not a directory: {folder}"]}

    results = []
    for ext in extensions:
        for fpath in sorted(folder.glob(f"*{ext}")):
            cat, order = _guess_category(fpath.stem)
            if ext == ".shp":
                r = import_shapefile(engine, str(fpath),
                                     layer_category=cat,
                                     display_order=order,
                                     source=source)
            else:
                r = import_geojson(engine, str(fpath),
                                   layer_category=cat,
                                   display_order=order,
                                   source=source)
            r["file"] = fpath.name
            results.append(r)

    imported = sum(r["loaded"] for r in results)
    errors   = [f"{r['file']}: {e}"
                for r in results for e in r.get("errors", [])]
    return {"imported": imported, "layers": len(results),
            "results": results, "errors": errors}
