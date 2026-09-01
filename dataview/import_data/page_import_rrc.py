"""
page_import_rrc.py — RRC Texas direct file loader
==================================================
Loads RRC Texas data directly into dataview.dv_well.
Calls prep_rrc_texas.parse_maf016() and parse_w1() internally —
no preprocessing step or intermediate CSV required.

Accepts:
  - RRC Header file (MAF016 — county-specific or statewide)
  - RRC Location file (W-1 permits — optional, provides lat/lon)
  - County filter (optional — comma-separated RRC county codes)

Wire into page_dv_importer.py:
    with st.expander("🤠 0d · RRC Texas Loader", expanded=False):
        try:
            from dataview.import_data import page_import_rrc
            page_import_rrc.render(engine)
        except Exception as e:
            st.error(f"RRC loader unavailable: {e}")
"""
from __future__ import annotations

import hashlib
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
    try: return float(val)
    except (TypeError, ValueError): return None

def _safe_date(val) -> Optional[str]:
    if not val: return None
    s = str(val).strip()[:10]
    return s if len(s) == 10 else None


# ── FK seed helpers ───────────────────────────────────────────────────────────

def _ensure_source(con, source: str) -> None:
    from sqlalchemy import text
    con.execute(text("""
        MERGE dataview.dv_r_source AS tgt
        USING (SELECT :src AS source) s ON tgt.source = s.source
        WHEN NOT MATCHED THEN INSERT (
            source, short_name, long_name, active_ind,
            row_created_by, row_created_date
        ) VALUES (:src, :src, :long, 'Y', 'RRC_LOADER', GETUTCDATE());
    """), {"src": _trunc(source, 40),
           "long": _trunc(f"RRC Texas — {source}", 255)})

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
                  'RRC_LOADER', GETUTCDATE(), :src);
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
        ) VALUES (:fid, :fname, 'Y', 'RRC_LOADER', GETUTCDATE(), :src);
    """), {"fid": fid, "fname": _trunc(field_name, 255),
           "src": _trunc(source, 40)})
    return fid


# ══════════════════════════════════════════════════════════════════════════════
# Database loader
# ══════════════════════════════════════════════════════════════════════════════

def _load_wells(engine, wells: list[dict], source: str,
                progress_cb=None) -> dict:
    """Bulk-load RRC wells via 3x CSV BULK INSERT + MERGE (BA, Field, Wells)."""
    import csv
    import os
    from sqlalchemy import text
    stats = {"loaded": 0, "skipped": 0, "errors": []}

    if not wells:
        return stats

    csv_dir = Path(r"C:\temp")
    csv_dir.mkdir(parents=True, exist_ok=True)

    with engine.begin() as con:
        _ensure_source(con, source)
        if progress_cb: progress_cb(0.05)

        # ── Collect unique operators and fields ───────────────────────
        operators = {w.get("CURR_OPERATOR","").strip()
                     for w in wells if w.get("CURR_OPERATOR","").strip()}
        fields    = {w.get("FIELD","").strip()
                     for w in wells if w.get("FIELD","").strip()}

        # ── Bulk seed BAs ─────────────────────────────────────────────
        ba_csv = str(csv_dir / "rrc_ba.csv")
        ba_cache = {}
        with open(ba_csv, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f, delimiter="\t")
            for op in operators:
                bid = _sha1(op)
                ba_cache[op] = bid
                wr.writerow([bid, _trunc(op, 255) or ""])

        con.execute(text("""
            IF OBJECT_ID('tempdb..#ba') IS NOT NULL DROP TABLE #ba;
            CREATE TABLE #ba (ba_id NVARCHAR(40), ba_name NVARCHAR(255));
        """))
        con.execute(text(f"""
            BULK INSERT #ba FROM '{ba_csv.replace(chr(39),"''")}'
            WITH (FIELDTERMINATOR='\t', ROWTERMINATOR='\n',
                  CODEPAGE='65001', TABLOCK);
        """))
        con.execute(text("""
            MERGE dataview.dv_business_associate AS tgt
            USING #ba AS src ON tgt.ba_id = src.ba_id
            WHEN NOT MATCHED THEN INSERT (
                ba_id, ba_name, ba_type, active_ind,
                row_created_by, row_created_date, source
            ) VALUES (src.ba_id, src.ba_name, 'COMPANY', 'Y',
                      'RRC_LOADER', GETUTCDATE(), :src);
        """), {"src": _trunc(source, 40)})
        con.execute(text("DROP TABLE #ba;"))
        try: os.unlink(ba_csv)
        except: pass
        if progress_cb: progress_cb(0.10)

        # ── Bulk seed Fields ──────────────────────────────────────────
        fld_csv = str(csv_dir / "rrc_fld.csv")
        field_cache = {}
        with open(fld_csv, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f, delimiter="\t")
            for fld in fields:
                fid = _sha1(fld)
                field_cache[fld] = fid
                wr.writerow([fid, _trunc(fld, 255) or ""])

        con.execute(text("""
            IF OBJECT_ID('tempdb..#fld') IS NOT NULL DROP TABLE #fld;
            CREATE TABLE #fld (field_id NVARCHAR(40), field_name NVARCHAR(255));
        """))
        con.execute(text(f"""
            BULK INSERT #fld FROM '{fld_csv.replace(chr(39),"''")}'
            WITH (FIELDTERMINATOR='\t', ROWTERMINATOR='\n',
                  CODEPAGE='65001', TABLOCK);
        """))
        con.execute(text("""
            MERGE dataview.dv_field AS tgt
            USING #fld AS src ON tgt.field_id = src.field_id
            WHEN NOT MATCHED THEN INSERT (
                field_id, field_name, active_ind,
                row_created_by, row_created_date, source
            ) VALUES (src.field_id, src.field_name, 'Y',
                      'RRC_LOADER', GETUTCDATE(), :src);
        """), {"src": _trunc(source, 40)})
        con.execute(text("DROP TABLE #fld;"))
        try: os.unlink(fld_csv)
        except: pass
        if progress_cb: progress_cb(0.20)

        # ── Write wells CSV ───────────────────────────────────────────
        wells_csv = str(csv_dir / "rrc_wells.csv")
        row_count = 0
        with open(wells_csv, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_NONE,
                            escapechar="\\")
            for w in wells:
                uwi = (w.get("API_NUM_NODASH") or "").strip()
                if not uwi:
                    stats["skipped"] += 1
                    continue
                op  = w.get("CURR_OPERATOR","").strip()
                fld = w.get("FIELD","").strip()
                wr.writerow([
                    _trunc(uwi, 40) or "",
                    _trunc(w.get("API_NUMBER"), 20) or "",
                    _trunc(w.get("LEASE") or w.get("LEASE_WELL_NAME"), 255) or "",
                    ba_cache.get(op) or "",
                    field_cache.get(fld) or "",
                    _trunc(w.get("STATE","TX"), 10) or "TX",
                    _trunc(w.get("COUNTY"), 100) or "",
                    _trunc(w.get("COUNTRY","US"), 10) or "US",
                    str(_safe_float(w.get("LATITUDE")) or ""),
                    str(_safe_float(w.get("LONGITUDE")) or ""),
                    _safe_date(w.get("SPUD")) or "",
                    _safe_date(w.get("COMPLETION")) or "",
                    str(_safe_float(w.get("DEPTH")) or ""),
                    _trunc(w.get("ELEV_REF","KB"), 40) or "KB",
                    _trunc(w.get("STATUS"), 40) or "",
                    _trunc(source, 40) or "RRC_TX",
                ])
                row_count += 1
        if progress_cb: progress_cb(0.35)

        # ── BULK INSERT wells ─────────────────────────────────────────
        con.execute(text("""
            IF OBJECT_ID('tempdb..#w') IS NOT NULL DROP TABLE #w;
            CREATE TABLE #w (
                uwi NVARCHAR(40) NOT NULL, api_num NVARCHAR(20),
                well_name NVARCHAR(255), operator_ba_id NVARCHAR(40),
                field_id NVARCHAR(40), province_state NVARCHAR(10),
                county NVARCHAR(100), country NVARCHAR(10),
                lat NVARCHAR(30), lon NVARCHAR(30),
                spud_date NVARCHAR(10), completion_date NVARCHAR(10),
                final_td NVARCHAR(20), depth_datum NVARCHAR(40),
                well_status NVARCHAR(40), source NVARCHAR(40)
            );
        """))
        con.execute(text(f"""
            BULK INSERT #w FROM '{wells_csv.replace(chr(39),"''")}'
            WITH (FIELDTERMINATOR='\t', ROWTERMINATOR='\n',
                  CODEPAGE='65001', TABLOCK);
        """))
        if progress_cb: progress_cb(0.60)

        # ── MERGE ─────────────────────────────────────────────────────
        con.execute(text("""
            MERGE dataview.dv_well AS tgt
            USING (
                SELECT uwi, api_num, well_name,
                       NULLIF(operator_ba_id,'') AS operator_ba_id,
                       NULLIF(field_id,'')       AS field_id,
                       province_state, county, country,
                       TRY_CAST(NULLIF(lat,'') AS FLOAT) AS lat,
                       TRY_CAST(NULLIF(lon,'') AS FLOAT) AS lon,
                       TRY_CAST(NULLIF(spud_date,'') AS DATE) AS spud_date,
                       TRY_CAST(NULLIF(completion_date,'') AS DATE) AS completion_date,
                       TRY_CAST(NULLIF(final_td,'') AS FLOAT) AS final_td,
                       depth_datum, well_status, source
                FROM #w
            ) AS src ON tgt.uwi = src.uwi
            WHEN NOT MATCHED THEN INSERT (
                uwi, api_num, well_name,
                operator_ba_id, field_id,
                province_state, county, country,
                surface_latitude, surface_longitude,
                spud_date, completion_date,
                final_td, depth_datum, well_status,
                active_ind, source,
                row_created_by, row_created_date
            ) VALUES (
                src.uwi, src.api_num, src.well_name,
                src.operator_ba_id, src.field_id,
                src.province_state, src.county, src.country,
                src.lat, src.lon,
                src.spud_date, src.completion_date,
                src.final_td, src.depth_datum, src.well_status,
                'Y', :src,
                'RRC_LOADER', GETUTCDATE()
            )
            WHEN MATCHED THEN UPDATE SET
                well_name        = COALESCE(tgt.well_name, src.well_name),
                operator_ba_id   = COALESCE(tgt.operator_ba_id, src.operator_ba_id),
                field_id         = COALESCE(tgt.field_id, src.field_id),
                surface_latitude = COALESCE(tgt.surface_latitude, src.lat),
                surface_longitude= COALESCE(tgt.surface_longitude, src.lon),
                spud_date        = COALESCE(tgt.spud_date, src.spud_date),
                completion_date  = COALESCE(tgt.completion_date, src.completion_date),
                final_td         = COALESCE(tgt.final_td, src.final_td),
                well_status      = COALESCE(tgt.well_status, src.well_status),
                row_changed_by   = 'RRC_LOADER',
                row_changed_date = GETUTCDATE();
        """), {"src": _trunc(source, 40)})

        stats["loaded"] = row_count
        if progress_cb: progress_cb(0.95)
        con.execute(text("DROP TABLE IF EXISTS #w;"))
        try: os.unlink(wells_csv)
        except: pass

    if progress_cb: progress_cb(1.0)
    return stats


# Streamlit render
# ══════════════════════════════════════════════════════════════════════════════

def render(engine) -> None:
    st.caption(
        "Loads RRC Texas data directly into dv_well. "
        "Uses the same parser as prep_rrc_texas.py — "
        "no intermediate CSV required. "
        "Operator and field names are stored as-is; "
        "ba_id and field_id use SHA1(UPPER(name))."
    )

    c1, c2 = st.columns(2)
    hdr_path = c1.text_input(
        "RRC Header file",
        placeholder=r"C:\RRC\maf016.cc003",
        key="rrc_hdr_path",

    )
    loc_path = c2.text_input(
        "RRC Location file (optional)",
        placeholder=r"C:\RRC\w1permits.txt",
        key="rrc_loc_path",

    )

    c3, c4, c5 = st.columns(3)
    source_label = c3.text_input(
        "Source label", value="RRC_TX", key="rrc_source")
    county_filter = c4.text_input(
        "County filter (optional)",
        placeholder="220,310,240",
        key="rrc_county_filter",

    )
    limit = c5.number_input(
        "Row limit (0 = all)", min_value=0, value=0, key="rrc_limit")

    if not hdr_path.strip():
        st.info("Enter the path to your RRC Header file to begin.")
        return

    hdr_file = Path(hdr_path.strip())
    if not hdr_file.exists():
        st.error(f"Header file not found: `{hdr_path}`")
        return

    loc_file = None
    if loc_path.strip():
        loc_file = Path(loc_path.strip())
        if not loc_file.exists():
            st.error(f"Location file not found: `{loc_path}`")
            return

    # Parse county filter
    cf = None
    if county_filter.strip():
        cf = {c.strip() for c in county_filter.strip().split(",")}

    _lim = int(limit) if limit else None

    # ── State machine: IDLE → PARSED → LOADING → DONE ────────────────
    # All state lives in session_state so it survives reruns.
    _cache_key = f"rrc|{hdr_path}|{loc_path}|{county_filter}|{limit}"

    # If inputs changed, reset state
    if st.session_state.get("_rrc_key") != _cache_key:
        st.session_state.pop("_rrc_wells", None)
        st.session_state.pop("_rrc_stats", None)
        st.session_state["_rrc_key"] = _cache_key

    wells = st.session_state.get("_rrc_wells")
    stats = st.session_state.get("_rrc_stats")

    # ── IDLE: show Parse button ───────────────────────────────────────
    if wells is None:
        if not st.button("🔍 Parse & Preview", type="primary",
                         key="rrc_parse_btn", use_container_width=True):
            return

        # Parse now
        try:
            from dataview.import_data.prep_rrc_texas import parse_maf016, parse_w1
        except ImportError as e:
            st.error(f"Cannot import prep_rrc_texas: {e}")
            return

        with st.spinner("Parsing RRC Header file…"):
            try:
                parsed = parse_maf016(str(hdr_file), cf, _lim)
            except Exception as e:
                st.error(f"Parse failed: {type(e).__name__}: {e}")
                return
            if not parsed:
                st.warning("No wells parsed — check file and county filter.")
                return

            if loc_file:
                try:
                    coords = parse_w1(str(loc_file), cf, _lim)
                    for w in parsed:
                        c = coords.get(w["API_NUM_NODASH"])
                        if c:
                            w["LATITUDE"]  = c[0]
                            w["LONGITUDE"] = c[1]
                except Exception as e:
                    st.warning(f"Location file: {e}")

            # Dedup
            seen = {}
            for w in parsed:
                uwi = w.get("API_NUM_NODASH")
                if uwi:
                    seen[uwi] = w

            st.session_state["_rrc_wells"] = list(seen.values())
            st.rerun()

    # ── DONE: show results if we already loaded ───────────────────────
    if stats is not None:
        if stats["skipped"] == 0:
            st.success(f"✅ Loaded {stats['loaded']:,} wells.")
        else:
            st.warning(
                f"✅ Loaded {stats['loaded']:,} wells · "
                f"⚠️ {stats['skipped']:,} skipped")
        if stats.get("errors"):
            for err in stats["errors"]:
                st.caption(f"  • {err}")
        if st.button("🔄 Reset", key="rrc_reset_btn"):
            st.session_state.pop("_rrc_wells", None)
            st.session_state.pop("_rrc_stats", None)
            st.rerun()
        return

    # ── PARSED: show preview + Load button ────────────────────────────
    import pandas as pd
    n_coords = sum(1 for w in wells if _safe_float(w.get("LATITUDE")))
    n_td     = sum(1 for w in wells if _safe_float(w.get("DEPTH")))
    n_spud   = sum(1 for w in wells if w.get("SPUD"))

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Wells",       f"{len(wells):,}")
    m2.metric("With coords", f"{n_coords:,}")
    m3.metric("With TD",     f"{n_td:,}")
    m4.metric("With spud",   f"{n_spud:,}")

    df = pd.DataFrame(wells)
    st.dataframe(df.head(50), use_container_width=True, hide_index=True)
    if len(wells) > 50:
        st.caption(f"Showing 50 of {len(wells):,} rows.")
    if not loc_file and n_coords == 0:
        st.caption("No Location file — wells will load without coordinates.")

    if not st.button("🚀 Load into DataView", type="primary",
                     key="rrc_load_btn", use_container_width=True):
        return

    # ── LOADING ───────────────────────────────────────────────────────
    src = (source_label or "RRC_TX").strip()
    prog = st.progress(0.0, text=f"Loading {len(wells):,} wells…")
    try:
        def _cb(pct):
            prog.progress(pct,
                text=f"Loading… {int(pct * len(wells)):,} / {len(wells):,}")
        result = _load_wells(engine, wells, src, progress_cb=_cb)
    except Exception as e:
        st.error(f"Load failed: {type(e).__name__}: {e}")
        prog.empty()
        return

    prog.empty()
    st.session_state["_rrc_stats"] = result
    st.rerun()
