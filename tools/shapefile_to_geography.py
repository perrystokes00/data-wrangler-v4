"""
shapefile_to_geography.py — load a polygon/line shapefile into one of DataView's
geography tables (dv_field / dv_land_tract / dv_pipeline / dv_boundary) as valid,
queryable SQL Server `geography`.

Standalone and direct — does NOT go through classify_shapefile / capture_features_
to_catalog. You tell it the feature type; it reads the shapefile, reprojects to
EPSG:4326 (required for `geography`), orients rings (SQL Server wants CCW
exterior), repairs invalid geometry, and writes one row per feature with geog set
via geography::STGeomFromText(@wkt, 4326).

USAGE (from repo root, venv active):
    from dataview.mapping.shapefile_to_geography import load_shapefile_geography
    r = load_shapefile_geography(
            engine,
            shp_path=r"C:\\...\\Oil_Fields_USA.shp",
            feature_type="FIELD",              # FIELD | LEASE | PIPELINE | BOUNDARY
            source="SHAPEFILE",
            name_col=None,                     # auto-detected if None
            inventory_id=None,                 # optional catalog link
            dry_run=False)                     # True = parse+validate, write nothing
    print(r)   # {loaded, skipped, errors, table, srid_in}

Design notes:
- geography requires SRID 4326. Projected shapefiles (State Plane / UTM / Albers)
  are reprojected from their .prj CRS. If a .prj is missing, we assume 4326 and warn.
- geography rejects wrong-wound or invalid polygons ("larger than a hemisphere").
  We orient exterior rings CCW and run make_valid before writing.
- Lines (pipelines) don't need orientation; polygons do.
- Each table's columns come straight from the live schema (see _TABLE_MAP).
- entity_id is generated per feature so re-runs MERGE-dedup on a stable key.
"""
from __future__ import annotations
import os
import hashlib
from typing import Optional


# ── destination table config — columns match the live dataview schema ────────────
# (id_col, name_col, extra attribute columns we populate, measure_col+kind)
_TABLE_MAP = {
    "FIELD": {
        "table": "dataview.dv_field",
        "id_col": "field_id",
        "name_col": "field_name",
        "measure": None,                       # dv_field has no area/length col for geog
        "geom_kinds": ("Polygon", "MultiPolygon"),
    },
    "LEASE": {
        "table": "dataview.dv_land_tract",
        "id_col": "land_tract_id",
        "name_col": "tract_name",
        "measure": ("area_km2", "area"),
        "geom_kinds": ("Polygon", "MultiPolygon"),
    },
    "PIPELINE": {
        "table": "dataview.dv_pipeline",
        "id_col": "pipeline_id",
        "name_col": "pipeline_name",
        "measure": ("length_km", "length"),
        "geom_kinds": ("LineString", "MultiLineString"),
    },
    "BOUNDARY": {
        "table": "dataview.dv_boundary",
        "id_col": "boundary_id",
        "name_col": "boundary_name",
        "measure": ("area_km2", "area"),
        "geom_kinds": ("Polygon", "MultiPolygon"),
    },
}

# common attribute-name patterns to auto-detect the feature name column
_NAME_PATTERNS = ["name", "field_nam", "fieldname", "lease", "tract", "pipeline",
                  "pipe_name", "label", "id", "field", "operator"]


def _entity_id(*parts) -> str:
    """Stable id from feature identity — same recipe family as the app's entity_id
    (UTF-16-LE, upper, SHA1 hex upper), so re-runs dedup on the same key."""
    raw = "|".join(str(p or "").upper().strip() for p in parts)
    return hashlib.sha1(raw.encode("utf-16-le")).hexdigest().upper()


def _pick_name_col(columns) -> Optional[str]:
    low = {c.lower(): c for c in columns if c.lower() != "geometry"}
    for pat in _NAME_PATTERNS:
        for lc, orig in low.items():
            if pat in lc:
                return orig
    # fall back to the first non-geometry attribute
    return next((c for c in columns if c.lower() != "geometry"), None)


def _orient_and_fix(geom):
    """Return a geography-safe geometry: valid, and (for polygons) exterior CCW.
    Returns None if it can't be repaired."""
    from shapely.ops import orient
    from shapely import make_valid
    from shapely.geometry.base import BaseGeometry
    if geom is None or geom.is_empty:
        return None
    try:
        if not geom.is_valid:
            geom = make_valid(geom)
        gt = geom.geom_type
        if gt == "Polygon":
            geom = orient(geom, sign=1.0)              # CCW exterior for geography
        elif gt == "MultiPolygon":
            from shapely.geometry import MultiPolygon
            geom = MultiPolygon([orient(g, sign=1.0) for g in geom.geoms])
        return geom if (isinstance(geom, BaseGeometry) and not geom.is_empty) else None
    except Exception:
        return None


def load_shapefile_geography(engine, shp_path: str, feature_type: str,
                             source: str = "SHAPEFILE",
                             name_col: Optional[str] = None,
                             inventory_id: Optional[str] = None,
                             dry_run: bool = False) -> dict:
    from sqlalchemy import text
    import geopandas as gpd

    ft = (feature_type or "").upper()
    if ft not in _TABLE_MAP:
        return {"loaded": 0, "skipped": 0,
                "errors": [f"unknown feature_type {feature_type!r}; "
                           f"use one of {list(_TABLE_MAP)}"], "table": None}
    cfg = _TABLE_MAP[ft]
    out = {"loaded": 0, "skipped": 0, "errors": [], "table": cfg["table"],
           "srid_in": None, "dry_run": dry_run}

    if not os.path.exists(shp_path):
        out["errors"].append(f"not found: {shp_path}")
        return out

    # ── read + reproject to 4326 ────────────────────────────────────────────────
    try:
        gdf = gpd.read_file(shp_path)
    except Exception as e:
        out["errors"].append(f"read failed: {e}")
        return out
    if gdf.empty:
        out["errors"].append("shapefile has 0 features")
        return out

    out["srid_in"] = str(gdf.crs) if gdf.crs is not None else None
    if gdf.crs is None:
        out["errors"].append("no CRS (.prj missing) — assuming EPSG:4326; verify!")
    else:
        try:
            if gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(epsg=4326)          # geography REQUIRES 4326
        except Exception as e:
            out["errors"].append(f"reproject to 4326 failed: {e}")
            return out

    ncol = name_col or _pick_name_col(list(gdf.columns))
    _measure = cfg["measure"]

    # area/length are computed in a metric CRS (equal-area) — 4326 degrees are not metric
    gdf_m = None
    if _measure:
        try:
            gdf_m = gdf.to_crs(epsg=6933)            # equal-area, metres
        except Exception:
            gdf_m = None

    rows = []
    for i, row in gdf.iterrows():
        geom = _orient_and_fix(row.geometry)
        if geom is None:
            out["skipped"] += 1
            continue
        if geom.geom_type not in cfg["geom_kinds"]:
            out["skipped"] += 1
            out["errors"].append(
                f"row {i}: {geom.geom_type} not valid for {ft} "
                f"(want {cfg['geom_kinds']})")
            continue
        nm = str(row.get(ncol, "") if ncol else "").strip() or f"{ft}_{i}"
        measure_val = None
        if _measure and gdf_m is not None:
            try:
                g_m = gdf_m.geometry.iloc[i]
                measure_val = (g_m.area / 1_000_000.0) if _measure[1] == "area" \
                    else (g_m.length / 1000.0)
            except Exception:
                measure_val = None
        rows.append({
            "id": _entity_id(ft, nm, source, str(i)),
            "name": nm[:255],
            "wkt": geom.wkt,
            "measure": measure_val,
            "inv": inventory_id,
        })

    if dry_run:
        out["loaded"] = 0
        out["would_load"] = len(rows)
        return out

    # ── write: MERGE per feature, geog via STGeomFromText(@wkt,4326) ─────────────
    id_col, name_col_db = cfg["id_col"], cfg["name_col"]
    meas_sql = f", {_measure[0]}" if _measure else ""
    meas_val = ", :measure" if _measure else ""
    meas_upd = f", {_measure[0]} = :measure" if _measure else ""

    merge = text(f"""
        MERGE {cfg['table']} AS tgt
        USING (SELECT :id AS id) src ON tgt.{id_col} = src.id
        WHEN MATCHED THEN UPDATE SET
            {name_col_db} = :name,
            geog          = geography::STGeomFromText(:wkt, 4326).MakeValid(),
            source        = :source,
            INVENTORY_ID  = :inv,
            active_ind    = 'Y'{meas_upd},
            row_changed_by = 'SHP_GEOG', row_changed_date = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN INSERT
            ({id_col}, {name_col_db}, geog, source, INVENTORY_ID, active_ind,
             row_created_by, row_created_date{meas_sql})
            VALUES (:id, :name,
                    geography::STGeomFromText(:wkt, 4326).MakeValid(),
                    :source, :inv, 'Y', 'SHP_GEOG', SYSUTCDATETIME(){meas_val});
    """)

    try:
        with engine.begin() as con:
            for r in rows:
                params = {"id": r["id"], "name": r["name"], "wkt": r["wkt"],
                          "source": source, "inv": r["inv"]}
                if _measure:
                    params["measure"] = r["measure"]
                try:
                    con.execute(merge, params)
                    out["loaded"] += 1
                except Exception as e:
                    out["skipped"] += 1
                    out["errors"].append(f"{r['name']}: {str(e)[:160]}")
    except Exception as e:
        out["errors"].append(f"write txn failed: {e}")
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python shapefile_to_geography.py <shp_path> <FIELD|LEASE|PIPELINE|BOUNDARY> [--dry]")
        sys.exit(1)
    # standalone smoke path needs an engine; wire your own connection here if running direct.
    print("Import load_shapefile_geography(engine, ...) from your app; "
          "this __main__ is a placeholder for a direct-connection test.")
