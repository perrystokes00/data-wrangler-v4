"""
page_import_shapefile.py — General well shapefile loader
=========================================================
Loads any well shapefile into dataview.dv_well. Auto-detects API/UWI
and coordinate columns. Works with shapefiles from RRC Texas, KGS,
BOEM, state agencies, IHS, Enverus, or any source.

Wire into page_dv_importer.py:
    with st.expander("🗺️ 0e · Well Shapefile Loader", expanded=False):
        try:
            from dataview.import_data import page_import_shapefile
            page_import_shapefile.render(engine)
        except Exception as e:
            st.error(f"Shapefile loader unavailable: {e}")
"""
from __future__ import annotations

import csv
import hashlib
import os
from pathlib import Path
from typing import Optional

import streamlit as st


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sha1(value: str) -> str:
    return hashlib.sha1(value.upper().strip().encode("utf-8")).hexdigest()

def _trunc(val, n: int) -> Optional[str]:
    if val is None: return None
    s = str(val).strip(); return s[:n] if s else None

def _safe_float(val) -> Optional[float]:
    if val is None or val == "": return None
    try:
        v = float(val)
        return v if v != 0.0 else None
    except (TypeError, ValueError): return None


# ── Column auto-detection ─────────────────────────────────────────────────────

# Patterns to detect API/UWI columns (case-insensitive)
API_PATTERNS = [
    "apinum", "api_num", "api_number", "api", "api14", "api10", "api12",
    "uwi", "unique_well", "well_id", "wellid", "well_api", "apino",
    "api_no", "api_well", "api_well_number",
]

# Patterns for latitude
LAT_PATTERNS = [
    "lat83", "latitude", "lat", "surface_latitude", "surf_lat",
    "lat27", "lat_dd", "y", "lat_nad83", "wgs84_lat", "slat",
]

# Patterns for longitude
LON_PATTERNS = [
    "long83", "longitude", "lon", "long", "surface_longitude", "surf_lon",
    "long27", "lon_dd", "x", "lon_nad83", "wgs84_lon", "slon",
]

# Patterns for well name
NAME_PATTERNS = [
    "well_name", "wellname", "well_nm", "lease_name", "lease",
    "name", "well_label", "label",
]

# Patterns for operator
OPERATOR_PATTERNS = [
    "operator", "operator_name", "curr_operator", "oper", "company",
    "op_name", "operatorname", "current_operator",
]

# Patterns for field
FIELD_PATTERNS = [
    "field", "field_name", "fieldname", "fld", "pool", "reservoir",
]

# Patterns for county
COUNTY_PATTERNS = [
    "county", "county_name", "countyname", "cnty", "parish",
]

# Patterns for state
STATE_PATTERNS = [
    "state", "province_state", "st", "state_code", "statecode",
    "province", "stcode",
]

# Patterns for status
STATUS_PATTERNS = [
    "well_status", "status", "wellstatus", "well_stat", "stat",
]

# Patterns for well type
TYPE_PATTERNS = [
    "well_type", "welltype", "type", "well_typ", "symnum", "sym",
]

# Patterns for total depth
TD_PATTERNS = [
    "final_td", "total_depth", "td", "depth", "totaldepth", "td_ft",
]


def _find_col(columns: list[str], patterns: list[str]) -> Optional[str]:
    """Find the first column matching any pattern (case-insensitive)."""
    cols_lower = {c.lower().strip(): c for c in columns}
    for pat in patterns:
        if pat in cols_lower:
            return cols_lower[pat]
    # Partial match fallback
    for pat in patterns:
        for cl, orig in cols_lower.items():
            if pat in cl:
                return orig
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Shapefile reader
# ══════════════════════════════════════════════════════════════════════════════

def read_shapefile(shp_path: str, col_map: dict) -> list[dict]:
    """Read a shapefile using the provided column mapping.

    col_map keys: uwi, lat, lon, well_name, operator, field,
                  county, state, status, well_type, td
    Values are the actual column names in the shapefile.
    """
    import geopandas as gpd

    gdf = gpd.read_file(shp_path)
    wells = []

    uwi_col   = col_map.get("uwi")
    lat_col   = col_map.get("lat")
    lon_col   = col_map.get("lon")
    name_col  = col_map.get("well_name")
    op_col    = col_map.get("operator")
    field_col = col_map.get("field")
    county_col= col_map.get("county")
    state_col = col_map.get("state")
    status_col= col_map.get("status")
    type_col  = col_map.get("well_type")
    td_col    = col_map.get("td")

    if not uwi_col:
        raise ValueError("No API/UWI column mapped — cannot load.")

    for _, row in gdf.iterrows():
        uwi_raw = str(row.get(uwi_col, "")).strip()
        if not uwi_raw or len(uwi_raw) < 5:
            continue

        # Normalize UWI — strip dashes, pad to 14 digits
        uwi_clean = uwi_raw.replace("-", "").replace(" ", "")
        if len(uwi_clean) < 14:
            uwi_clean = uwi_clean.ljust(14, "0")
        uwi_14 = uwi_clean[:14]

        # Build dashed API from first 10 meaningful digits
        digits = uwi_clean[:10].zfill(10)
        api_dashed = f"{digits[:2]}-{digits[2:5]}-{digits[5:10]}"

        # Coordinates
        lat = _safe_float(row.get(lat_col)) if lat_col else None
        lon = _safe_float(row.get(lon_col)) if lon_col else None

        # If no explicit lat/lon columns, extract from geometry
        if (lat is None or lon is None) and row.geometry is not None:
            try:
                lat = row.geometry.y
                lon = row.geometry.x
            except Exception:
                pass

        if lat is None or lon is None:
            continue

        wells.append({
            "uwi":        uwi_14,
            "api_num":    api_dashed,
            "well_name":  _trunc(row.get(name_col), 255) if name_col else None,
            "operator":   _trunc(row.get(op_col), 255) if op_col else None,
            "field":      _trunc(row.get(field_col), 255) if field_col else None,
            "county":     _trunc(row.get(county_col), 100) if county_col else None,
            "state":      _trunc(row.get(state_col), 10) if state_col else None,
            "status":     _trunc(row.get(status_col), 40) if status_col else None,
            "well_type":  _trunc(row.get(type_col), 40) if type_col else None,
            "td":         _safe_float(row.get(td_col)) if td_col else None,
            "lat":        lat,
            "lon":        lon,
        })

    return wells


# ══════════════════════════════════════════════════════════════════════════════
# Database loader
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_source(con, source: str) -> None:
    from sqlalchemy import text
    con.execute(text("""
        MERGE dataview.dv_r_source AS tgt
        USING (SELECT :src AS source) s ON tgt.source = s.source
        WHEN NOT MATCHED THEN INSERT (
            source, short_name, long_name, active_ind,
            row_created_by, row_created_date
        ) VALUES (:src, :src, :long, 'Y', 'SHP_LOADER', GETUTCDATE());
    """), {"src": _trunc(source, 40),
           "long": _trunc(f"Shapefile import — {source}", 255)})

def _ensure_ba(con, name: str, source: str) -> Optional[str]:
    from sqlalchemy import text
    if not name or not name.strip(): return None
    ba_id = _sha1(name)
    con.execute(text("""
        MERGE dataview.dv_business_associate AS tgt
        USING (SELECT :id AS ba_id) src ON tgt.ba_id = src.ba_id
        WHEN NOT MATCHED THEN INSERT (
            ba_id, ba_name, ba_type, active_ind,
            row_created_by, row_created_date, source
        ) VALUES (:id, :name, 'COMPANY', 'Y',
                  'SHP_LOADER', GETUTCDATE(), :src);
    """), {"id": ba_id, "name": _trunc(name, 255), "src": _trunc(source, 40)})
    return ba_id

def _ensure_field(con, field_name: str, source: str) -> Optional[str]:
    from sqlalchemy import text
    if not field_name or not field_name.strip(): return None
    fid = _sha1(field_name)
    con.execute(text("""
        MERGE dataview.dv_field AS tgt
        USING (SELECT :fid AS field_id) src ON tgt.field_id = src.field_id
        WHEN NOT MATCHED THEN INSERT (
            field_id, field_name, active_ind,
            row_created_by, row_created_date, source
        ) VALUES (:fid, :fname, 'Y', 'SHP_LOADER', GETUTCDATE(), :src);
    """), {"fid": fid, "fname": _trunc(field_name, 255),
           "src": _trunc(source, 40)})
    return fid


def bulk_load_wells(engine, wells: list[dict], source: str,
                    area: str = "", progress_cb=None) -> dict:
    """BULK INSERT wells via CSV → temp table → MERGE into dv_well."""
    from sqlalchemy import text
    stats = {"loaded": 0, "skipped": 0}

    if not wells:
        return stats

    csv_dir = Path(r"C:\temp")
    csv_dir.mkdir(parents=True, exist_ok=True)

    with engine.begin() as con:
        _ensure_source(con, source)
        if progress_cb: progress_cb(0.05)

        # Bulk seed BAs and fields if present
        operators = {w["operator"] for w in wells
                     if w.get("operator") and w["operator"].strip()}
        fields = {w["field"] for w in wells
                  if w.get("field") and w["field"].strip()}

        ba_cache = {}
        if operators:
            ba_csv = str(csv_dir / "shp_ba.csv")
            with open(ba_csv, "w", newline="", encoding="utf-8") as f:
                wr = csv.writer(f, delimiter="\t")
                for op in operators:
                    bid = _sha1(op)
                    ba_cache[op] = bid
                    wr.writerow([bid, _trunc(op, 255)])
            con.execute(text("""
                IF OBJECT_ID('tempdb..#ba') IS NOT NULL DROP TABLE #ba;
                CREATE TABLE #ba (ba_id NVARCHAR(40), ba_name NVARCHAR(255));
            """))
            con.execute(text(f"""
                BULK INSERT #ba FROM '{ba_csv.replace(chr(39),"''")}'
                WITH (FIELDTERMINATOR='\\t', ROWTERMINATOR='\\n',
                      CODEPAGE='65001', TABLOCK);
            """))
            con.execute(text("""
                MERGE dataview.dv_business_associate AS tgt
                USING #ba AS src ON tgt.ba_id = src.ba_id
                WHEN NOT MATCHED THEN INSERT (
                    ba_id, ba_name, ba_type, active_ind,
                    row_created_by, row_created_date, source
                ) VALUES (src.ba_id, src.ba_name, 'COMPANY', 'Y',
                          'SHP_LOADER', GETUTCDATE(), :src);
            """), {"src": _trunc(source, 40)})
            con.execute(text("DROP TABLE #ba;"))
            try: os.unlink(ba_csv)
            except: pass

        field_cache = {}
        if fields:
            fld_csv = str(csv_dir / "shp_fld.csv")
            with open(fld_csv, "w", newline="", encoding="utf-8") as f:
                wr = csv.writer(f, delimiter="\t")
                for fld in fields:
                    fid = _sha1(fld)
                    field_cache[fld] = fid
                    wr.writerow([fid, _trunc(fld, 255)])
            con.execute(text("""
                IF OBJECT_ID('tempdb..#fld') IS NOT NULL DROP TABLE #fld;
                CREATE TABLE #fld (field_id NVARCHAR(40), field_name NVARCHAR(255));
            """))
            con.execute(text(f"""
                BULK INSERT #fld FROM '{fld_csv.replace(chr(39),"''")}'
                WITH (FIELDTERMINATOR='\\t', ROWTERMINATOR='\\n',
                      CODEPAGE='65001', TABLOCK);
            """))
            con.execute(text("""
                MERGE dataview.dv_field AS tgt
                USING #fld AS src ON tgt.field_id = src.field_id
                WHEN NOT MATCHED THEN INSERT (
                    field_id, field_name, active_ind,
                    row_created_by, row_created_date, source
                ) VALUES (src.field_id, src.field_name, 'Y',
                          'SHP_LOADER', GETUTCDATE(), :src);
            """), {"src": _trunc(source, 40)})
            con.execute(text("DROP TABLE #fld;"))
            try: os.unlink(fld_csv)
            except: pass

        if progress_cb: progress_cb(0.15)

        # Write wells CSV
        wells_csv = str(csv_dir / "shp_wells.csv")
        row_count = 0
        with open(wells_csv, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_NONE,
                            escapechar="\\")
            for w in wells:
                op  = (w.get("operator") or "").strip()
                fld = (w.get("field") or "").strip()
                wr.writerow([
                    _trunc(w["uwi"], 40),
                    _trunc(w.get("api_num"), 20) or "",
                    _trunc(w.get("well_name"), 255) or "",
                    ba_cache.get(op) or "",
                    field_cache.get(fld) or "",
                    _trunc(w.get("state"), 10) or "",
                    _trunc(w.get("county"), 100) or "",
                    w.get("lat") or "",
                    w.get("lon") or "",
                    _trunc(w.get("status"), 40) or "",
                    _trunc(w.get("well_type"), 40) or "",
                    str(w.get("td") or ""),
                    _trunc(area or "", 100),
                ])
                row_count += 1

        if progress_cb: progress_cb(0.30)

        # Temp table + BULK INSERT
        con.execute(text("""
            IF OBJECT_ID('tempdb..#shp') IS NOT NULL DROP TABLE #shp;
            CREATE TABLE #shp (
                uwi NVARCHAR(40) NOT NULL, api_num NVARCHAR(20),
                well_name NVARCHAR(255), operator_ba_id NVARCHAR(40),
                field_id NVARCHAR(40), province_state NVARCHAR(10),
                county NVARCHAR(100),
                lat NVARCHAR(30), lon NVARCHAR(30),
                well_status NVARCHAR(40), well_type NVARCHAR(40),
                final_td NVARCHAR(20),
                area NVARCHAR(100)
            );
        """))
        con.execute(text(f"""
            BULK INSERT #shp FROM '{wells_csv.replace(chr(39),"''")}'
            WITH (FIELDTERMINATOR='\\t', ROWTERMINATOR='\\n',
                  CODEPAGE='65001', TABLOCK);
        """))

        if progress_cb: progress_cb(0.55)

        # MERGE
        con.execute(text("""
            MERGE dataview.dv_well AS tgt
            USING (
                SELECT uwi, api_num, well_name,
                       NULLIF(operator_ba_id,'') AS operator_ba_id,
                       NULLIF(field_id,'') AS field_id,
                       NULLIF(province_state,'') AS province_state,
                       NULLIF(county,'') AS county,
                       TRY_CAST(NULLIF(lat,'') AS FLOAT) AS lat,
                       TRY_CAST(NULLIF(lon,'') AS FLOAT) AS lon,
                       NULLIF(well_status,'') AS well_status,
                       NULLIF(well_type,'') AS well_type,
                       TRY_CAST(NULLIF(final_td,'') AS FLOAT) AS final_td,
                       NULLIF(area,'') AS area
                FROM #shp
            ) AS src ON tgt.uwi = src.uwi
            WHEN NOT MATCHED THEN INSERT (
                uwi, api_num, well_name,
                operator_ba_id, field_id,
                province_state, county, country,
                surface_latitude, surface_longitude,
                well_status, well_type, final_td, area,
                active_ind, source,
                row_created_by, row_created_date
            ) VALUES (
                src.uwi, src.api_num, src.well_name,
                src.operator_ba_id, src.field_id,
                src.province_state, src.county, 'US',
                src.lat, src.lon,
                src.well_status, src.well_type, src.final_td, src.area,
                'Y', :src,
                'SHP_LOADER', GETUTCDATE()
            )
            WHEN MATCHED THEN UPDATE SET
                well_name         = COALESCE(tgt.well_name, src.well_name),
                operator_ba_id    = COALESCE(tgt.operator_ba_id, src.operator_ba_id),
                field_id          = COALESCE(tgt.field_id, src.field_id),
                province_state    = COALESCE(tgt.province_state, src.province_state),
                county            = COALESCE(tgt.county, src.county),
                surface_latitude  = COALESCE(tgt.surface_latitude, src.lat),
                surface_longitude = COALESCE(tgt.surface_longitude, src.lon),
                well_status       = COALESCE(tgt.well_status, src.well_status),
                well_type         = COALESCE(tgt.well_type, src.well_type),
                final_td          = COALESCE(tgt.final_td, src.final_td),
                api_num           = COALESCE(tgt.api_num, src.api_num),
                area              = COALESCE(tgt.area, src.area),
                row_changed_by    = 'SHP_LOADER',
                row_changed_date  = GETUTCDATE();
        """), {"src": _trunc(source, 40)})

        stats["loaded"] = row_count
        if progress_cb: progress_cb(0.90)
        con.execute(text("DROP TABLE IF EXISTS #shp;"))
        try: os.unlink(wells_csv)
        except: pass

    if progress_cb: progress_cb(1.0)
    return stats


# ══════════════════════════════════════════════════════════════════════════════
# Streamlit render
# ══════════════════════════════════════════════════════════════════════════════

def render(engine) -> None:
    st.caption(
        "Loads well shapefiles from any source into dv_well. "
        "Auto-detects API/UWI and coordinate columns. "
        "Point at a directory or a single .shp file."
    )

    path_input = st.text_input(
        "Shapefile or directory path",
        placeholder=r"C:\Data\wells.shp  or  C:\RRC\shapefiles",
        key="shp_well_path",

    )

    c_s, c_a = st.columns(2)
    source_label = c_s.text_input(
        "Source label", value="SHAPEFILE", key="shp_well_source")
    area_label = c_a.text_input(
        "Area", value="", key="shp_well_area",
        placeholder="e.g. Permian Basin, Andrews County")

    if not path_input.strip():
        st.info("Enter a shapefile path or directory to begin.")
        return

    target = Path(path_input.strip())
    if not target.exists():
        st.error(f"Path not found: `{path_input}`")
        return

    # Find shapefiles
    if target.is_file() and target.suffix.lower() == ".shp":
        shp_files = [target]
    elif target.is_dir():
        shp_files = sorted(target.glob("*.shp"))
    else:
        st.error("Path must be a .shp file or a directory containing .shp files.")
        return

    if not shp_files:
        st.warning(f"No .shp files found in `{path_input}`")
        return

    st.info(f"Found **{len(shp_files)}** shapefile(s): "
            + ", ".join(f.stem for f in shp_files[:10])
            + ("…" if len(shp_files) > 10 else ""))

    # State machine
    _cache_key = f"shp|{path_input}|{source_label}"
    if st.session_state.get("_shpw_key") != _cache_key:
        st.session_state.pop("_shpw_wells", None)
        st.session_state.pop("_shpw_stats", None)
        st.session_state.pop("_shpw_col_map", None)
        st.session_state["_shpw_key"] = _cache_key

    wells = st.session_state.get("_shpw_wells")
    stats = st.session_state.get("_shpw_stats")

    # ── IDLE: Read columns + show mapping ─────────────────────────────
    if wells is None:
        try:
            import geopandas as gpd
        except ImportError:
            st.error("geopandas is required: `pip install geopandas`")
            return

        # Read first shapefile to detect columns
        try:
            sample = gpd.read_file(str(shp_files[0]), rows=5)
        except Exception as e:
            st.error(f"Cannot read {shp_files[0].name}: {e}")
            return

        cols = [c for c in sample.columns if c != "geometry"]
        st.caption(f"Columns in **{shp_files[0].name}**: {', '.join(cols)}")

        # Auto-detect column mapping
        auto_map = {
            "uwi":       _find_col(cols, API_PATTERNS),
            "lat":       _find_col(cols, LAT_PATTERNS),
            "lon":       _find_col(cols, LON_PATTERNS),
            "well_name": _find_col(cols, NAME_PATTERNS),
            "operator":  _find_col(cols, OPERATOR_PATTERNS),
            "field":     _find_col(cols, FIELD_PATTERNS),
            "county":    _find_col(cols, COUNTY_PATTERNS),
            "state":     _find_col(cols, STATE_PATTERNS),
            "status":    _find_col(cols, STATUS_PATTERNS),
            "well_type": _find_col(cols, TYPE_PATTERNS),
            "td":        _find_col(cols, TD_PATTERNS),
        }

        # Let user override with selectboxes
        st.markdown("**Column mapping** (auto-detected — override if needed)")
        opts = ["— none —"] + cols

        c1, c2, c3 = st.columns(3)
        col_map = {}

        def _sel(label, key, default, container):
            idx = opts.index(default) if default and default in opts else 0
            picked = container.selectbox(label, opts, index=idx,
                                         key=f"shp_map_{key}")
            return picked if picked != "— none —" else None

        col_map["uwi"]       = _sel("API / UWI *",  "uwi",   auto_map["uwi"], c1)
        col_map["lat"]       = _sel("Latitude",      "lat",   auto_map["lat"], c1)
        col_map["lon"]       = _sel("Longitude",     "lon",   auto_map["lon"], c1)
        col_map["well_name"] = _sel("Well name",     "name",  auto_map["well_name"], c1)
        col_map["operator"]  = _sel("Operator",      "op",    auto_map["operator"], c2)
        col_map["field"]     = _sel("Field",         "fld",   auto_map["field"], c2)
        col_map["county"]    = _sel("County",        "cty",   auto_map["county"], c2)
        col_map["state"]     = _sel("State",         "st",    auto_map["state"], c2)
        col_map["status"]    = _sel("Well status",   "stat",  auto_map["status"], c3)
        col_map["well_type"] = _sel("Well type",     "wtype", auto_map["well_type"], c3)
        col_map["td"]        = _sel("Total depth",   "td",    auto_map["td"], c3)

        if not col_map["uwi"]:
            st.error("An API / UWI column is required.")
            return

        if not col_map["lat"] and not col_map["lon"]:
            st.caption("No lat/lon columns mapped — coordinates will be "
                       "extracted from geometry if available.")

        st.session_state["_shpw_col_map"] = col_map

        if not st.button("🔍 Parse & Preview", type="primary",
                         key="shp_parse_btn", use_container_width=True):
            return

        # Parse all shapefiles
        all_wells = []
        prog = st.progress(0.0, text="Reading shapefiles…")
        for i, shp in enumerate(shp_files):
            prog.progress(i / len(shp_files), text=f"Reading {shp.name}…")
            try:
                batch = read_shapefile(str(shp), col_map)
                all_wells.extend(batch)
            except Exception as e:
                st.warning(f"Skipped {shp.name}: {e}")
        prog.empty()

        if not all_wells:
            st.warning("No wells with coordinates found.")
            return

        # Dedup
        seen = {}
        for w in all_wells:
            seen[w["uwi"]] = w

        st.session_state["_shpw_wells"] = list(seen.values())
        st.rerun()

    # ── DONE: show results ────────────────────────────────────────────
    if stats is not None:
        st.success(f"✅ Loaded {stats['loaded']:,} wells.")
        if st.button("🔄 Reset", key="shp_reset"):
            st.session_state.pop("_shpw_wells", None)
            st.session_state.pop("_shpw_stats", None)
            st.rerun()
        return

    # ── PARSED: preview + load ────────────────────────────────────────
    import pandas as pd

    n_with_name = sum(1 for w in wells if w.get("well_name"))
    n_with_op   = sum(1 for w in wells if w.get("operator"))
    n_with_td   = sum(1 for w in wells if w.get("td"))

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Wells with coords", f"{len(wells):,}")
    m2.metric("With name",         f"{n_with_name:,}")
    m3.metric("With operator",     f"{n_with_op:,}")
    m4.metric("With TD",           f"{n_with_td:,}")

    df = pd.DataFrame(wells)
    st.dataframe(df.head(50), use_container_width=True, hide_index=True)
    if len(wells) > 50:
        st.caption(f"Showing 50 of {len(wells):,} rows.")

    if not st.button("🚀 Load into DataView", type="primary",
                     key="shp_load_btn", use_container_width=True):
        return

    # ── LOADING ───────────────────────────────────────────────────────
    src = (source_label or "SHAPEFILE").strip()
    prog = st.progress(0.0, text=f"Loading {len(wells):,} wells…")
    try:
        result = bulk_load_wells(engine, wells, src,
                                 area=area_label.strip(),
                                 progress_cb=lambda p: prog.progress(p))
    except Exception as e:
        st.error(f"Load failed: {type(e).__name__}: {e}")
        prog.empty()
        return

    prog.empty()
    st.session_state["_shpw_stats"] = result
    st.rerun()
