# DataView v3 — handoff addendum: shapefiles & the geometry model
**Date:** 2026-07-16 (appended after the main handoff was written)

---

## 1. Shapefile support in v3 does not exist

`dataview\mapping\shapefile_catalog.py` is **270 bytes**:

```python
"""shapefile_catalog.py (root) — shim; canonical implementation lives in
modules/shapefile_catalog.py."""
from dataview.mapping.shapefile_catalog import *
```

**It imports itself.** The July 11 refactor rewrote the shim's import to the package path —
which *is* the shim — and quarantined the real module without bringing it across. The
implementation is 37,067 bytes at:

```
_ARCHIVE_dead\_refactor_quarantine\modules\shapefile_catalog.py
```

Three copies exist, two of them 270-byte shims (`dataview\mapping\`, `_ARCHIVE_dead\mapping\`).

**Do not delete `_ARCHIVE_dead\_refactor_quarantine\` until this is resolved.** The archive
is holding the only real copy.

**Check whether it's a one-off** — any other sub-600-byte file in the package is a candidate
stub:
```powershell
Get-ChildItem dataview -Recurse -Filter *.py |
  Where-Object { $_.Length -lt 600 } |
  Select-Object @{n='Path';e={$_.FullName.Replace($PWD.Path+'\','')}}, Length |
  Format-Table -AutoSize
```

**How this was missed:** `dead_code.py` section 2 listed no unused functions for that module.
Read as "everything is called." It meant "there are no functions." An absence of findings is
not a finding — same misread as `sp_help`'s `Owner = dbo`.

---

## 2. The geometry model is TWO layers, deliberately

From `page_well_map.py` — this is the design, not an accident:

| layer | storage | read by | for |
|---|---|---|---|
| **native geography** | `dv_*.geog` (SQL Server `geography`) | `geography_layers.add_geography_layer` (line 6943) | curated reference: `dv_boundary`, `dv_county`, `dv_province_state` |
| **import registry** | `dv_spatial_layer.geometry_wkt` (`nvarchar(MAX)`) | the registry path (docstring, line 15) | user-imported files — `source_type` ∈ `GEOJSON`, `SHAPEFILE` |

> line 15: *"Read from dv_spatial_layer registry (GEOJSON or SHAPEFILE source_type)"*
> line 6936: *"Native-geography layers (dv_*.geog) via geography_layers module"*

**A dropped-in shapefile is the second kind.** `source_type='SHAPEFILE'` is a documented
value of that column. This is not a fork to be avoided — it is the existing split.

---

## 3. Target tables — checked, not assumed

| want | target | type | pipeline change? |
|---|---|---|---|
| **well points** | `dv_well.surface_latitude` / `surface_longitude` | `numeric` | **none** |
| **bounding boxes** | `dv_spatial_layer.bbox_min/max_lat/lon` | `numeric` | **none** |
| **seismic lines / polygons** | `dv_spatial_layer.geometry_wkt` | `nvarchar(MAX)` | **none** |
| reference geography | `dv_boundary.geog` | `geography` | **yes — see below** |

`dv_spatial_layer` (28 cols, empty) was **built for exactly this job**: `layer_name`,
`layer_type`, `layer_category`, **`epsg_code`**, `file_path`, `feature_count`,
`bbox_min/max_lat/lon`, `geometry_wkt`, `source_type`, plus Folium rendering hints
(`style_color`, `style_weight`, `style_opacity`, `style_fill_color`, `style_dash`,
`tooltip_fields`, `display_order`). Designed, never filled.

**`bulk_dir_loader` has ZERO geography support** — no `STGeomFromText`, no UDT awareness
(grep returned nothing). BCP in character mode cannot load a `geography` column. Loading
`dv_boundary.geog` through route B needs a conversion step in `build_promote_sql`:
staging holds WKT as varchar, promote does `geography::STGeomFromText(wkt, 4326)`.
**None of the three things we actually want requires this.**

**Corrections to earlier assumptions in this session:**
- **`dv_map_area` is NOT spatial.** `center_lat`, `center_lon`, `center_zoom`,
  `queries_allowed`, `where_clause`, `sort_order` — that is map *viewport configuration*.
- **`dv_seis_line` has NO geometry column.** `shot_point_start/end`, `cdp_start/end`,
  `record_length_ms`, `sample_rate_ms`, `trace_count` — it is **SEGY header metadata**. A
  seismic line's *path* has no home there; only its acquisition properties.

---

## 4. A new extractor beats restoring the old module

The 902-line original predates the pipeline: it hand-rolls `map_columns`, `load_to_ppdm`,
`detect_duplicates`, FK handling and inserts, and its `PPDM_TARGETS` point at **`dbo.WELL`,
`dbo.FIELD`, `dbo.LAND_TRACT`, `dbo.SEIS_LINE`, `dbo.FACILITY`** — the v2 `PPDM39_DEMO_1`
schema, not `dataview.*`. Its own comment (line 558) says it *"bypasses the cat_* → dv_*
pipeline."*

An extractor's whole job today is **glob → read → emit a CSV with DDL column names**. The
loader does the rest. That is ~200 lines, same shape as `dlis_header_loader`:
`find_shp()` with normcase dedup, `files=`/`recursive=`, lowercase target columns,
`entity_id()` for `layer_id`, `_num()` for coordinates.

**Worth mining from the old module, then discarding the rest:** the classification
heuristics — `_score_columns`, the feature-type patterns, `FT_WELL` / `FT_FIELD` /
`FT_SEISMIC_2D` / `FT_BOUNDARY`. Knowing that a `.shp` with `API` + `WELL_NAME` columns and
Point geometry is a well list is **domain knowledge**, not plumbing.

---

## 5. Two refusals the new extractor must make

**CRS.** The old `normalize_crs` does:
```python
if gdf.crs is None:
    gdf = gdf.set_crs("EPSG:4326")   # "Assume WGS84 if no CRS defined"
```
A shapefile with no `.prj` is **silently declared WGS84**. Most US regulatory data is State
Plane or UTM — feet or metres reinterpreted as degrees land the wells off the coast of
Africa, or nowhere. No error, no warning. `crs is None` means **unknown**, and unknown must
stop and ask, exactly like the UWI gate. It is the geographic twin of assuming
`depth_ouom = FT`.

It is also wrong twice: `dv_spatial_layer.epsg_code` is the column built to record what was
actually found. Assuming discards it.

**UWIs from DBF.** DBF fields are typed, so this is checkable, not a judgement call:
- `C` (character) holding `42329100010000` → exact, safe.
- `N` (numeric) → 14 digits do not survive as a float. **Unrecoverable**, exactly like the
  Excel-mangled UWIs already on record. **Refuse** rather than load a plausible wrong number.

`fiona`'s schema exposes it: `'API': 'str:14'` vs `'API': 'float:19.11'`.

**Also:** DBF column names are capped at **10 characters** by the format —
`OPERATOR_NAME` → `OPERATOR_N`, `SURFACE_LATITUDE` → `SURFACE_LA`. Canonical near-matching
will NOT rescue these (`operator|n` ≠ `operator|name`), and two 11-char source columns can
collide into one 10-char name. Every shapefile source arrives pre-truncated.

---

## 6. Open questions — not guessed at

1. **Is a `dv_spatial_layer` row one LAYER or one FEATURE?** `layer_id` + `feature_count` +
   a single `geometry_wkt` suggests one row per layer holding a GEOMETRYCOLLECTION. But
   `style_*` and `tooltip_fields` are per-layer concerns while `geometry_wkt` reads as
   per-feature. This decides whether a 500-line seismic survey is **1 row or 500**. Real
   consequences for the map. Decide before writing the extractor.
2. **Which database are the map's spatial layers actually working against?** In
   `DataView_Demo`, `dv_boundary`, `dv_field`, `dv_land_tract`, `dv_pipeline`, `dv_seis_line`
   and `dv_spatial_layer` are **all empty** — so the Fields / Leases / Boundaries / Pipelines
   / Seismic pills render nothing here. Whatever is populated is in `WRANGLER` or `DataView`.
3. **`geography_layers.add_all_geography` is never called** (per `dead_all.txt`). Do the
   reference layers work in `DataView_Demo` at all, given `dv_boundary` and `dv_county` are
   empty?
4. **`PPDM_TARGETS` → what?** If the `dbo` path is ever retired rather than replaced:
   `FT_PIPELINE` and `FT_FACILITY` both map to `dbo.FACILITY`, but the scorecard shows
   `dv_pipeline` and **no facility table at all**. Not a straight rename.

---

## Suggested scope for the next session

**Points → `dv_well` first.** It is the slice where the target already works (200 rows,
`surface_latitude`/`surface_longitude` 100% populated), it needs nothing new from the
pipeline, and success is measurable. Then `dv_spatial_layer` for lines and polygons once
question 1 is decided. `dv_boundary.geog` last, if ever — it needs the promote conversion
and nothing currently wants it.
