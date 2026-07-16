"""
page_workbench.py
=================
File Catalog & Workbench -- three tabs:

  1. Scan & Extract -- fast scan, bulk insert, Phase 2 header extraction
  2. Browse & View  -- filter catalog, view/plot files, extract and load data
  3. Header Files   -- query FILE_WELL_HEADER / FILE_SEIS_HEADER, export CSV
"""
import os
import re
import uuid
import streamlit as st
import pandas as pd
from pathlib import Path

# ── Extension sets ─────────────────────────────────────────────────────────
# Canonical definitions live in extract_core (shared with the process-pool
# parser) and are imported back here, so there is one source of truth. Add a
# new format extension in extract_core, not here.
from dataview.file_catalog.extract_core import (
    PDF_EXTS, LAS_EXTS, DLIS_EXTS, LIS_EXTS, SEGY_EXTS, P190_EXTS, SHP_EXTS,
    OFFICE_EXTS, CSV_EXTS, IMAGE_EXTS, WITSML_EXTS, JSON_LOG_EXTS, LOG_EXTS,
    _extract_fields,
)

# ALL_EXTS is the DEFAULT scan universe — what a BLANK Formats-to-scan box
# walks. CSV/TSV are deliberately excluded: too generic to crawl blindly
# (every export, log dump and config is a CSV). They scan only when a user
# hand-enters '.csv'/'.tsv' in the box.
ALL_EXTS = (PDF_EXTS | LOG_EXTS | SEGY_EXTS | P190_EXTS |
            SHP_EXTS | OFFICE_EXTS | IMAGE_EXTS |
            WITSML_EXTS | JSON_LOG_EXTS)

# KNOWN_EXTS is everything the run will RECOGNIZE if typed — ALL_EXTS plus the
# opt-in types. Use this (not ALL_EXTS) for the "unknown extension" warning and
# for validating an explicit override, so '.csv' is honored, not rejected.
KNOWN_EXTS = ALL_EXTS | CSV_EXTS

EXT_GROUP = {}
for e in PDF_EXTS:      EXT_GROUP[e] = "PDF"
for e in LOG_EXTS:      EXT_GROUP[e] = "Well Log"
for e in SEGY_EXTS:     EXT_GROUP[e] = "Seismic"
for e in P190_EXTS:     EXT_GROUP[e] = "Seismic"
for e in SHP_EXTS:      EXT_GROUP[e] = "Shapefile"
for e in OFFICE_EXTS:   EXT_GROUP[e] = "Office"
for e in CSV_EXTS:      EXT_GROUP[e] = "CSV / Table"
for e in IMAGE_EXTS:    EXT_GROUP[e] = "Image"
for e in WITSML_EXTS:   EXT_GROUP[e] = "WITSML"
for e in JSON_LOG_EXTS: EXT_GROUP[e] = "OSDU / JSON Well Log"

# Extensions whose loader parses the file itself (it resolves the well +
# INVENTORY_ID internally and writes cat_* via capture()), so _do_extract
# returns nothing for them and the capture loops must NOT skip them on an
# empty row list. Office is wired today; WITSML / OSDU join as their loaders
# land in _load_rows_to_catalog.
# Extensions whose capture path (_load_rows_to_catalog) re-parses the file
# itself and does NOT depend on the pre-extracted `rows` arg. The pipeline's
# capture stage skips a file when _do_extract yields no rows UNLESS its ext is
# here — so self-parsing formats must be listed or they silently no-op.
# PDF belongs here: scout/EOW/well-test tickets carry header/section data, not
# the tabular "rows" a directional survey yields, so _do_extract returns empty
# for them; _load_rows_to_catalog re-classifies and extracts internally. Before
# PDF was added, only directional surveys (which DO yield station rows) passed
# the gate — hence "7 of 40 cataloged".
SELF_PARSING_EXTS = (set(OFFICE_EXTS) | set(WITSML_EXTS)
                     | set(JSON_LOG_EXTS) | set(LAS_EXTS)
                     | set(SHP_EXTS)
                     | set(PDF_EXTS))

ENRICH_CHUNK = 50   # files processed per rerun cycle


# =============================================================================
# Entry point
# =============================================================================

def run(engine=None, dialect: str = "mssql"):
    st.title("🗂️ File Catalog & Workbench")
    st.caption(
        "Scan & extract files · Browse & view · "
        "Extract data · Load to DB"
    )

    if engine is None:
        st.warning("No database connection.")
        return

    # Phase 2 enrichment — runs at top of every rerun while active
    if st.session_state.get("wb_enriching"):
        _enrich_chunk(engine, dialect)
    elif st.session_state.get("wb_enrich_done"):
        st.success(
            f"✅ Extraction complete — "
            f"{st.session_state.get('wb_enrich_total',0):,} files processed."
        )

    # Section selector — a keyed radio instead of st.tabs, because st.tabs
    # resets to the first tab on every rerun, which snaps you off the Pipeline
    # section the moment you click a button in it. The radio's selection is
    # persisted in session_state ("wb_active_tab"), so it survives reruns.
    _TAB_LABELS = [
        "🗂 File Catalog Pipeline",
        "▶ Run Pipeline",
        "📂 Browse and View",
        "📦 Vault",
    ]
    # drop any stale section value persisted from a previous layout
    if st.session_state.get("wb_active_tab") not in _TAB_LABELS:
        st.session_state.pop("wb_active_tab", None)
    active = st.radio(
        "Workbench section",
        _TAB_LABELS,
        key="wb_active_tab",
        horizontal=True,
        label_visibility="collapsed",
    )
    st.divider()

    if active == _TAB_LABELS[0]:
        _tab_pipeline(engine, dialect)
    elif active == _TAB_LABELS[1]:
        # Headless run page — launches pipeline_run.py in a fresh process and
        # tails its log live (replaces the old Fast Track / monitor embed).
        try:
            from dataview.file_catalog import page_run
            page_run.render(engine)
        except Exception as e:
            st.error(f"Run Pipeline unavailable: {e}")
            import traceback
            with st.expander("details"):
                st.code(traceback.format_exc())
    elif active == _TAB_LABELS[3]:
        try:
            from dataview.file_catalog import page_vault
            page_vault.render(engine)
        except Exception as e:
            st.error(f"Vault unavailable: {e}")
            import traceback
            with st.expander("details"):
                st.code(traceback.format_exc())
    else:
        _tab_browse(engine, dialect)


# =============================================================================
# Tab 1 -- Scan & Extract
# =============================================================================

def _tab_scan(engine, dialect):
    from sqlalchemy import text as _t

    st.markdown("#### 🔍 Scan & Extract")

    # ── Config ────────────────────────────────────────────────────────────────
    scan_path = st.text_input(
        "Root folder",
        value=st.session_state.get(
            "wb_last_scan_path",
            r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai"
            r"\data_wrangler\training\test_crawl"),
        placeholder=r"\\server\share\WellData  or  C:\WellData",
        key="wb_scan_path",
    )
    ext_groups = st.multiselect(
        "File types to scan",
        options=[
            "PDF",
            "Well Log",
            "Seismic",
            "Shapefile",
            "Office",
            "WITSML",
            "OSDU / JSON Well Log",
            "Image",
        ],
        default=[
            "PDF",
            "Well Log",
            "Seismic",
            "Shapefile",
            "Office",
            "WITSML",
            "OSDU / JSON Well Log",
        ],
        key="wb_scan_exts",
        help=(
            "**PDF** — .pdf\n\n"
            "**Well Log** — .las  ·  .dlis  ·  .dlf  ·  .dis  ·  .lis\n\n"
            "**Seismic** — .segy  ·  .sgy  ·  .seg  ·  .p190  ·  .p90  ·  .p1\n\n"
            "**Shapefile** — .shp  ·  .gpkg  ·  .kml  ·  .kmz\n\n"
            "**Office** — .xlsx  ·  .xls  ·  .xlsm  ·  .docx  ·  .doc  ·  .csv  ·  .tsv\n\n"
            "**WITSML** — .xml\n"
            "*(WITSML 1.3.1 / 1.4.1 — trajectory, log, mudLog, well, wellbore)*\n\n"
            "**OSDU / JSON Well Log** — .json\n"
            "*(16 OSDU schemas: Well, Wellbore, WellLog, WellboreTrajectory, "
            "WellboreMarkerSet, WellborePressureData, WellboreCompletion, "
            "WellCoreAnalysis, ProductionVolume, RockFluidOrganisation/SCAL, "
            "Field, Reservoir, SeismicAcquisitionSurvey, SeismicHorizon, "
            "SeismicFault, Document — plus JSON Well Log Format/JSONWLF)*\n\n"
            "**Image** — .tif  ·  .tiff  ·  .png  ·  .jpg  ·  .jpeg\n"
            "*(Phase 1 scan only — no extractor. Useful for inventorying "
            "core photos, well plat images, seismic sections etc.)*"
        ),
    )
    _exts = {e for e, g in EXT_GROUP.items() if g in ext_groups}

    c1, c2, c3 = st.columns(3)

    # Phase 1
    if c1.button("🔍 Scan (Phase 1)", type="primary",
                 key="wb_p1", use_container_width=True):
        if not scan_path:
            st.error("Enter a folder path.")
        elif not Path(scan_path).exists():
            st.error(f"Not found: `{scan_path}`")
        else:
            st.session_state["wb_last_scan_path"] = scan_path
            _run_scan(engine, dialect, scan_path, _exts)

    # Phase 2
    if c2.button("⚙️ Extract (Phase 2)", type="secondary",
                 key="wb_p2", use_container_width=True):
        st.session_state["wb_enriching"]     = True
        st.session_state["wb_enrich_offset"] = 0
        st.rerun()

    if c3.button("⏹ Stop", key="wb_stop", use_container_width=True):
        st.session_state["wb_enriching"] = False

    # Phase 2 thread count — tune parallelism. Default 8 works for mixed
    # extraction (PDF/DLIS/Office). Drop to 2-4 for DLIS-heavy batches
    # that load big chunks into memory; raise to 16 for many small files.
    _w = st.slider(
        "Phase 2 threads",
        min_value=1, max_value=16,
        value=int(st.session_state.get("wb_phase2_workers", 8)),
        key="wb_phase2_workers_slider",
        help="Files per chunk extracted in parallel. Lower for "
             "DLIS-heavy batches; higher for many small files. Default 8.",
    )
    st.session_state["wb_phase2_workers"] = _w

    # ── Catalog summary ───────────────────────────────────────────────────────
    st.divider()
    try:
        with engine.connect() as con:
            rows = con.execute(_t("""
                SELECT
                    FILE_TYPE_GROUP,
                    COUNT(*)                                               total,
                    SUM(CASE WHEN HEADER_EXTRACTED='Y' THEN 1 ELSE 0 END) extracted,
                    SUM(CASE WHEN CATALOG_READINESS='READY'     THEN 1 ELSE 0 END) ready,
                    SUM(CASE WHEN CATALOG_READINESS='NEEDS_UWI' THEN 1 ELSE 0 END) needs_uwi,
                    SUM(CASE WHEN CATALOG_READINESS='ATTENTION' THEN 1 ELSE 0 END) attention,
                    SUM(CASE WHEN CATALOG_READINESS='CATALOGED' THEN 1 ELSE 0 END) cataloged,
                    SUM(CASE WHEN ISNULL(FLAG_DELETE,'N')='Y'   THEN 1 ELSE 0 END) flagged,
                    SUM(CASE WHEN HEADER_EXTRACTED='S'  THEN 1 ELSE 0 END) skipped
                FROM file_catalog.GLOBAL_FILE_CATALOG
                GROUP BY FILE_TYPE_GROUP
                ORDER BY total DESC
            """)).fetchall()

        if rows:
            df = pd.DataFrame(rows, columns=[
                "Type","Total","Extracted","Ready","Needs UWI","Attention",
                "Cataloged","Flagged","Skipped"])
            tot  = df["Total"].sum()
            enr  = df["Extracted"].sum()
            skip = int(df["Skipped"].sum())
            m1,m2,m3,m4,m5,m6 = st.columns(6)
            m1.metric("Total cataloged",    f"{tot:,}")
            m2.metric("Extracted",          f"{enr:,}")
            m3.metric("Pending extraction", f"{tot-enr-skip:,}")
            m4.metric("Skipped",            skip,
                      help="Files skipped — too large for extraction. "
                           "HEADER_EXTRACTED='S' in catalog.")
            m5.metric("Cataloged",          int(df["Cataloged"].sum()),
                      help="Files whose rows are captured in the cat_* mirrors "
                           "(CATALOG_READINESS='CATALOGED').")
            m6.metric("Flagged",            int(df["Flagged"].sum()))
            st.dataframe(df, hide_index=True, use_container_width=True)
        else:
            st.info("Catalog is empty — run Phase 1 scan.")
    except Exception as e:
        st.caption(f"Catalog summary: {e}")

    # ── Delete tools ──────────────────────────────────────────────────────────
    st.divider()

    # Extractor reference — behind an expander so it doesn't dominate the page
    with st.expander("📋 Extractor reference — what Phase 2 captures", expanded=False):
        st.caption(
            "Phase 2 reads each file's internal header and writes identifying "
            "metadata to FILE_WELL_HEADER or FILE_SEIS_HEADER. Files on disk "
            "are never modified. File types with no extractor are cataloged in "
            "Phase 1 but skipped in Phase 2 — deleting them before running "
            "extraction keeps the batch fast.\n\n"
            "**Well logs —** "
            "LAS (.las): UWI/API, well name, operator, field, state, county, "
            "lat/lon, total depth, spud date, contractor, curve mnemonics, depth range. "
            "DLIS (.dlis .dlf .dis): well name, field, operator from origins block, "
            "channel count, frame count. "
            "LIS (.lis): well name, UWI, operator, field, state, county, contractor, "
            "depth range, curve mnemonics via dlisio; raw byte scan fallback for "
            "non-standard files.\n\n"
            "**Seismic —** "
            "SEG-Y (.segy .sgy .seg): trace count, sample interval, 2D/3D "
            "classification (updates FILE_TYPE_GROUP), survey name and contractor "
            "from text header, bounding box and survey outline polygon from 400 "
            "strided trace headers with CRS detection and WGS84 reprojection, "
            "inline/crossline range for 3D surveys. "
            "P190 (.p190 .p90 .p1): survey name, contractor, shot count, "
            "bounding box from S-records.\n\n"
            "**Documents —** "
            "PDF (.pdf): report type classification (directional survey, mud log, "
            "scout ticket, completion, DST, core, well proposal), UWI, well name, "
            "operator, field, state, county, lat/lon, total depth, spud date, "
            "rig release, survey type, contractor, confidence score.\n\n"
            "**Spatial —** "
            "Shapefile / GeoPackage / KML (.shp .gpkg .kml .kmz): "
            "feature type classification (well, seismic 2D/3D, field, lease, "
            "pipeline, facility, boundary), CRS, bounding box, DBF column mapping, "
            "sample UWIs/well names/operators/field names/status codes, date ranges. "
            "Well shapefiles promote UWI and operator into the catalog record.\n\n"
            "**Office —** "
            "Excel (.xlsx .xls .xlsm): sheet classification (BOEM borehole, KGS well, "
            "production, completion, formation tops, well header, core, pressure, "
            "survey, reserves), row count, column headers, UWI from data. "
            "Known schemas (BOEM_BOREHOLE, KGS_WELL, RRC_WELL) detected before "
            "generic classification. Read via openpyxl streaming — no hang on "
            "large files. "
            "Word (.docx .doc): document type classification (completion report, "
            "geological, DST, well proposal, regulatory, formation tops, HSE), "
            "headings, table classification, UWI and well name from text. "
            "CSV/TSV: column classification, row count, UWI from data.\n\n"
            "**WITSML (.xml) —** "
            "Trajectory: well name, UWI, survey tool type, station count, depth "
            "range, contractor from commonData. "
            "Log: curve mnemonics from logCurveInfo, depth range, service company, "
            "run number. "
            "MudLog: formation interval count, gas show summary from chromatograph "
            "elements, comments. "
            "File must contain the witsml.org/schemas namespace — other XML files "
            "(config, SVG, RSS) are skipped automatically.\n\n"
            "**JSON Well Log / OSDU (.json) —** "
            "16 OSDU schemas detected by the 'kind' field: "
            "Well (name, UWI, operator, lat/lon, field, spud, TD), "
            "WellLog (curves, depth range, contractor), "
            "WellboreTrajectory (KOP, landing, lateral length, max inc, max DLS), "
            "WellboreMarkerSet (full formation tops list with MD/TVD/subsea/quality), "
            "WellborePressureData (DST pressures, flow rates, permeability, skin), "
            "WellboreCompletion (stages, clusters, fluid/proppant volumes, formations), "
            "WellCoreAnalysis (plug count, porosity/perm stats, full plug list), "
            "ProductionVolume (monthly records, cumulative volumes, peak rate), "
            "RockFluidOrganisation / SCAL (system types, end-point saturations, "
            "capillary pressure method), "
            "Field (discovery year/well, basin, fluid type, bbox, cumulative production), "
            "Reservoir (porosity, perm, net pay, pressure, temperature, GOR, OOIP), "
            "SeismicAcquisitionSurvey (2D/3D, bbox, inline/crossline, fold, bin size), "
            "SeismicHorizon (geologic unit, depth stats, node count, well control), "
            "SeismicFault (fault type, strike/dip, max throw, length, horizons cut), "
            "Document (document type, author, file format, page count). "
            "Also handles JSON Well Log Format (JSONWLF) from NORCE/NPD. "
            "Non-petroleum JSON (config files, package.json) skipped automatically "
            "by a 512-byte header check."
        )

    # Delete section — heading first, then Select All, then grid
    st.markdown("**Delete from catalog**")
    st.caption(
        "Removes selected file types from the catalog index only — "
        "files on disk are never touched."
    )

    # Human-readable label for every extension the catalog might contain.
    # Covers all known sets plus a fallback for anything unexpected.
    _EXT_LABEL = {
        # Well logs
        ".las":   "LAS — Log ASCII Standard well log",
        ".dlis":  "DLIS — Digital Log Interchange Standard",
        ".dlf":   "DLF — DLIS variant",
        ".dis":   "DIS — DLIS variant",
        ".lis":   "LIS — Log Information Standard",
        # Seismic
        ".segy":  "SEG-Y — Seismic data (classified as 2D or 3D after extraction)",
        ".sgy":   "SGY — SEG-Y seismic data (classified as 2D or 3D after extraction)",
        ".seg":   "SEG — SEG-Y seismic data (legacy extension)",
        ".p190":  "P190 — Navigation / shot point data",
        ".p90":   "P90 — P190 variant",
        ".p1":    "P1 — P190 variant",
        # Documents
        ".pdf":   "PDF — Portable Document (scout tickets, reports, surveys)",
        # Office
        ".xlsx":  "XLSX — Excel workbook",
        ".xls":   "XLS — Excel workbook (legacy)",
        ".xlsm":  "XLSM — Excel macro-enabled workbook",
        ".docx":  "DOCX — Word document",
        ".doc":   "DOC — Word document (legacy)",
        ".csv":   "CSV — Comma-separated values",
        ".tsv":   "TSV — Tab-separated values",
        # Shapefiles / spatial
        ".shp":     "SHP — Shapefile geometry",
        ".geojson": "GeoJSON — Geographic JSON",
        ".gpkg":    "GPKG — GeoPackage",
        ".kml":     "KML — Keyhole Markup Language",
        ".kmz":     "KMZ — Compressed KML",
        # WITSML
        ".xml":     "XML / WITSML — trajectory, log, mud log, well header",
        # JSON Well Log / OSDU
        ".json":    "JSON — OSDU (16 schemas: Well, Wellbore, WellLog, WellboreTrajectory, "
                   "WellboreMarkerSet, WellborePressureData, WellboreCompletion, "
                   "WellCoreAnalysis, ProductionVolume, RockFluidOrganisation/SCAL, "
                   "Field, Reservoir, SeismicAcquisitionSurvey, SeismicHorizon, "
                   "SeismicFault, Document) + JSON Well Log Format (JSONWLF)",
        ".tiff":  "TIFF — TIFF image",
        ".png":   "PNG — PNG image",
        ".jpg":   "JPG — JPEG image",
        ".jpeg":  "JPEG — JPEG image",
    }

    # Load distinct extensions currently in the catalog
    try:
        with engine.connect() as con:
            _ext_rows = con.execute(_t("""
                SELECT FILE_EXT, COUNT(*) AS n
                FROM file_catalog.GLOBAL_FILE_CATALOG
                WHERE FILE_EXT IS NOT NULL AND FILE_EXT <> ''
                GROUP BY FILE_EXT
                ORDER BY FILE_EXT
            """)).fetchall()
        _ext_counts = {r[0]: r[1] for r in _ext_rows}
    except Exception as _e:
        _ext_counts = {}
        st.caption(f"Extension list unavailable: {_e}")

    if _ext_counts:
        _total_all_files = sum(_ext_counts.values())

        # Select ALL checkbox — sits above the grid, turns on every Delete
        # checkbox without being a separate toggle-style control. Matches
        # the spreadsheet metaphor: header checkbox selects all rows.
        _sel_all = st.checkbox(
            f"☑ Select ALL  ({len(_ext_counts)} extension types · "
            f"{_total_all_files:,} files)",
            key="wb_sel_all",
            help="Check to mark every extension for deletion. "
                 "Uncheck individual rows in the grid to exclude them.",
        )

        st.markdown("---")

        # Build the dataframe for the editor
        import pandas as _pd
        _rows = []
        for _ext in sorted(_ext_counts.keys()):
            _rows.append({
                "Delete": _sel_all,   # pre-check all rows when Select ALL is on
                "Extension": _ext,
                "Description": _EXT_LABEL.get(
                    _ext.lower(),
                    f"{_ext.lstrip('.').upper()} file"),
                "Files": _ext_counts[_ext],
            })
        _df_exts = _pd.DataFrame(_rows)

        _edited = st.data_editor(
            _df_exts,
            use_container_width=True,
            hide_index=True,
            disabled=["Extension", "Description", "Files"],
            column_config={
                "Delete": st.column_config.CheckboxColumn(
                    "Delete", width="small"),
                "Extension": st.column_config.TextColumn(
                    "Extension", width="small"),
                "Description": st.column_config.TextColumn(
                    "Description"),
                "Files": st.column_config.NumberColumn(
                    "Files", width="small", format="%d"),
            },
            key="wb_ext_editor",
        )

        _checked_exts = _edited.loc[
            _edited["Delete"] == True, "Extension"].tolist()

        st.markdown("---")

        if _checked_exts:
            _total_sel = sum(_ext_counts.get(e, 0) for e in _checked_exts)
            st.caption(
                f"{len(_checked_exts)} extension(s) selected · "
                f"{_total_sel:,} files will be removed from the catalog index "
                f"(files on disk are NOT deleted)"
            )
            if st.button(
                f"🗑️ Delete {_total_sel:,} files from catalog",
                key="wb_del_ext_go",
                type="primary",
            ):
                st.session_state["wb_del_ext_confirm"] = True

            if st.session_state.get("wb_del_ext_confirm"):
                st.warning(
                    f"Remove **{_total_sel:,} files** with "
                    f"{len(_checked_exts)} extension(s) from catalog? "
                    "This cannot be undone."
                )
                _cc1, _cc2 = st.columns(2)
                if _cc1.button("✅ Yes, delete", key="wb_del_ext_yes",
                               type="primary"):
                    try:
                        _deleted = 0
                        with engine.begin() as con:
                            for _ext in _checked_exts:
                                _deleted += con.execute(_t("""
                                    DELETE FROM file_catalog.GLOBAL_FILE_CATALOG
                                    WHERE FILE_EXT = :e
                                """), {"e": _ext}).rowcount
                        st.success(f"Deleted {_deleted:,} files from catalog.")
                        st.session_state.pop("wb_del_ext_confirm", None)
                        st.rerun()
                    except Exception as _de:
                        st.error(f"Delete failed: {_de}")
                if _cc2.button("✗ Cancel", key="wb_del_ext_cancel"):
                    st.session_state.pop("wb_del_ext_confirm", None)
                    st.rerun()
    else:
        st.caption("Catalog is empty — nothing to delete.")

    # ── Delete all flagged ────────────────────────────────────────────────────
    st.divider()
    try:
        with engine.connect() as con:
            nf = con.execute(_t("""
                SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG
                WHERE FLAG_DELETE='Y'
            """)).scalar() or 0
        if nf > 0:
            if st.button(f"🗑️ Delete all {nf:,} flagged files",
                         type="primary", key="wb_del_flagged"):
                st.session_state["wb_del_flagged_confirm"] = True

            if st.session_state.get("wb_del_flagged_confirm"):
                st.warning(
                    f"Remove **{nf:,} flagged files** from catalog? "
                    "Files on disk are NOT deleted."
                )
                cc1, cc2 = st.columns(2)
                if cc1.button("✅ Yes, delete", key="wb_del_flag_yes",
                              type="primary"):
                    with engine.begin() as con:
                        n = con.execute(_t("""
                            DELETE FROM file_catalog.GLOBAL_FILE_CATALOG
                            WHERE FLAG_DELETE='Y'
                        """)).rowcount
                    st.success(f"Deleted {n:,} flagged files from catalog.")
                    st.session_state.pop("wb_del_flagged_confirm", None)
                    st.rerun()
                if cc2.button("❌ Cancel", key="wb_del_flag_no"):
                    st.session_state.pop("wb_del_flagged_confirm", None)
                    st.rerun()
        else:
            st.caption("No files flagged for deletion.")
    except Exception as e:
        st.caption(f"Flag check: {e}")


def _run_scan(engine, dialect, root: str, exts: set):
    """Phase 1: fast os.scandir walk + BULK INSERT to GLOBAL_FILE_CATALOG."""
    import csv, tempfile, hashlib
    from datetime import datetime, timezone
    from sqlalchemy import text as _t
    from dataview.core.fingerprint import file_fingerprint, DEDUPE_SQL   # single source of truth

    prog = st.progress(0.0, text="Walking...")
    found = []
    folders = 0
    stack = [root]

    while stack:
        dirpath = stack.pop()
        folders += 1
        try:
            with os.scandir(dirpath) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            ext = os.path.splitext(entry.name)[1].lower()
                            if ext in exts:
                                # JSON peek — only catalog .json files that
                                # look like OSDU or JSONWLF petroleum data.
                                # Reads the first 100 bytes only (single disk
                                # read) so Phase 1 speed is not affected.
                                # Skips package.json, settings.json, tsconfig,
                                # Streamlit config, and any other non-petroleum
                                # JSON files before they enter the catalog.
                                if ext == ".json":
                                    try:
                                        with open(entry.path, "rb") as _jf:
                                            _peek = _jf.read(100)
                                        # OSDU files always have "kind" near
                                        # the top. JSONWLF files have "header".
                                        # Any other JSON is not petroleum data.
                                        if (b'"kind"'   not in _peek and
                                                b'"header"' not in _peek):
                                            continue
                                    except OSError:
                                        continue
                                st_res = entry.stat()
                                found.append((
                                    entry.path, entry.name, ext,
                                    round(st_res.st_size/1024, 2),
                                    datetime.fromtimestamp(
                                        st_res.st_mtime,
                                        tz=timezone.utc
                                    ).strftime("%Y-%m-%d %H:%M:%S"),
                                    EXT_GROUP.get(ext, "Other"),
                                    root,
                                    file_fingerprint(entry.path,
                                                     st_res.st_size,
                                                     st_res.st_mtime),
                                ))
                    except OSError:
                        pass
        except (PermissionError, OSError):
            pass
        if folders % 2000 == 0:
            prog.progress(0.3, text=f"{folders:,} folders · {len(found):,} files")

    if not found:
        st.warning("No files found.")
        return

    # Blocklist: files fingerprinted as "bad" (junk / unparseable) are skipped
    # so they never re-enter the catalog. Keyed on INVENTORY_ID = SHA1(path) —
    # the same fingerprint computed below — so the check is a cheap set lookup.
    bad_set = set()
    try:
        with engine.connect() as con:
            bad_set = {r[0] for r in con.execute(_t(
                "SELECT INVENTORY_ID FROM file_catalog.BAD_FILE")).fetchall()}
    except Exception:
        bad_set = set()   # table not created yet — nothing to skip

    prog.progress(0.5, text=f"{len(found):,} files — writing CSV...")
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False,
        newline="", encoding="utf-8"
    )
    csv_path = tmp.name
    writer = csv.writer(tmp, delimiter="\t",
                        quoting=csv.QUOTE_NONE, escapechar="\\")
    n_bad = 0   # files skipped because they're on the bad-file blocklist
    for (fpath, fname, fext, size_kb, mod_dt, grp, rpath, fhash) in found:
        inv_id = hashlib.sha1(
            fpath.upper().encode("utf-8")).hexdigest().upper()
        if inv_id in bad_set:
            n_bad += 1
            continue
        writer.writerow([
            inv_id, fpath[:900], fname[:260], fext[:20],
            grp[:50], size_kb if size_kb else "",
            fhash, "", "UNCATALOGED", "",
            rpath[:900], now, now, now,
        ])
    tmp.close()

    prog.progress(0.7, text="Bulk inserting...")
    try:
        with engine.begin() as con:
            con.execute(_t("""
                IF OBJECT_ID('file_catalog.fc_stage','U') IS NOT NULL
                    DROP TABLE file_catalog.fc_stage;
                CREATE TABLE file_catalog.fc_stage (
                    INVENTORY_ID     NVARCHAR(40),
                    FILE_PATH        NVARCHAR(900),
                    FILE_NAME        NVARCHAR(260),
                    FILE_EXT         NVARCHAR(20),
                    FILE_TYPE_GROUP  NVARCHAR(50),
                    FILE_SIZE_KB     NVARCHAR(30),
                    FILE_HASH        NVARCHAR(40),
                    DUPLICATE_GROUP  NVARCHAR(64),
                    CATALOG_STATUS   NVARCHAR(20),
                    CATALOG_TABLE    NVARCHAR(100),
                    ROOT_PATH        NVARCHAR(900),
                    SCAN_DATE        NVARCHAR(30),
                    ROW_CREATED_DATE NVARCHAR(30),
                    ROW_CHANGED_DATE NVARCHAR(30)
                );
            """))
            con.execute(_t(f"""
                BULK INSERT file_catalog.fc_stage
                FROM '{csv_path}'
                WITH (FIELDTERMINATOR='\\t', ROWTERMINATOR='0x0D0A',
                      CODEPAGE='65001', FIRSTROW=1, TABLOCK);
            """))
            con.execute(_t("""
                MERGE file_catalog.GLOBAL_FILE_CATALOG AS tgt
                USING file_catalog.fc_stage AS src
                ON tgt.INVENTORY_ID = src.INVENTORY_ID
                WHEN MATCHED THEN UPDATE SET
                    FILE_SIZE_KB     = TRY_CAST(src.FILE_SIZE_KB AS DECIMAL(15,2)),
                    FILE_HASH        = src.FILE_HASH,
                    SCAN_DATE        = TRY_CAST(src.SCAN_DATE AS DATETIME2),
                    ROW_CHANGED_DATE = TRY_CAST(src.ROW_CHANGED_DATE AS DATETIME2)
                WHEN NOT MATCHED THEN INSERT (
                    INVENTORY_ID,FILE_PATH,FILE_NAME,FILE_EXT,
                    FILE_TYPE_GROUP,FILE_SIZE_KB,FILE_HASH,
                    DUPLICATE_GROUP,CATALOG_STATUS,CATALOG_TABLE,
                    ROOT_PATH,SCAN_DATE,ROW_CREATED_DATE,ROW_CHANGED_DATE
                ) VALUES (
                    src.INVENTORY_ID,src.FILE_PATH,src.FILE_NAME,src.FILE_EXT,
                    src.FILE_TYPE_GROUP,
                    TRY_CAST(src.FILE_SIZE_KB AS DECIMAL(15,2)),
                    src.FILE_HASH,src.DUPLICATE_GROUP,src.CATALOG_STATUS,
                    src.CATALOG_TABLE,src.ROOT_PATH,
                    TRY_CAST(src.SCAN_DATE AS DATETIME2),
                    TRY_CAST(src.ROW_CREATED_DATE AS DATETIME2),
                    TRY_CAST(src.ROW_CHANGED_DATE AS DATETIME2)
                );
            """))
            # Content-dedupe via the shared rule (identical to the CLI + File
            # Manager scans): one canonical per FILE_HASH stays processable;
            # redundant copies get DUPLICATE_GROUP set and are skipped by
            # extract + capture. Idempotent — full recompute each scan.
            con.execute(_t(DEDUPE_SQL))
            n_dup = con.execute(_t(
                "SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG "
                "WHERE DUPLICATE_GROUP IS NOT NULL")).scalar() or 0
            con.execute(_t(
                "DROP TABLE IF EXISTS file_catalog.fc_stage;"))

        prog.progress(1.0, text="Done.")
        st.success(
            f"✅ Phase 1 complete — {len(found) - n_bad:,} files "
            f"across {folders:,} folders"
            + (f" · {n_bad:,} bad file(s) skipped" if n_bad else "")
            + (f" · {n_dup:,} dup(s) skipped" if n_dup else "")
            + ". Click **Extract** to extract headers."
        )
    except Exception as e:
        st.error(f"Bulk insert failed: {e}")
    finally:
        try:
            os.unlink(csv_path)
        except Exception:
            pass


def _enrich_chunk(engine, dialect):
    """
    Phase 2: process ENRICH_CHUNK files per rerun.
    Extracts headers → FILE_WELL_HEADER / FILE_SEIS_HEADER.
    Shows a persistent progress bar at the top of the page.

    Extraction within each chunk runs in parallel across PHASE2_WORKERS
    threads. DB writes stay sequential in the main thread — single-row
    UPDATEs are microseconds each, so the bottleneck is file parsing
    (PDFs/DLIS can take seconds), which is what we parallelize.

    The chunked-rerun pattern is preserved: each chunk finishes, Streamlit
    reruns, the next chunk starts. The user can still hit Stop between
    chunks to pause without losing progress.
    """
    from sqlalchemy import text as _t
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # ── Ensure new 3D columns exist in FILE_SEIS_HEADER ──────────────────────
    # Added when the 3D extractor was upgraded (inline/crossline range and
    # survey outline polygon). ALTER TABLE is a no-op if column already
    # exists — guarded by the sys.columns check so it's safe to run every
    # time and costs a single metadata query per session.
    try:
        with engine.begin() as _con:
            for _col, _def in [
                ("IL_MIN",         "INT NULL"),
                ("IL_MAX",         "INT NULL"),
                ("XL_MIN",         "INT NULL"),
                ("XL_MAX",         "INT NULL"),
                ("SURVEY_OUTLINE", "NVARCHAR(MAX) NULL"),
            ]:
                _con.execute(_t(f"""
                    IF NOT EXISTS (
                        SELECT 1 FROM sys.columns
                        WHERE object_id = OBJECT_ID(
                            'file_catalog.FILE_SEIS_HEADER')
                          AND name = '{_col}'
                    )
                    ALTER TABLE file_catalog.FILE_SEIS_HEADER
                        ADD [{_col}] {_def}
                """))
    except Exception:
        pass  # Non-fatal — extraction proceeds; new fields just won't write

    # Phase 2 worker count from session state. Default 8 — balanced for
    # mixed extraction (PDF/DLIS/Office). User can tune via the slider in
    # the Scan tab.
    PHASE2_WORKERS = int(st.session_state.get("wb_phase2_workers", 8))

    try:
        with engine.connect() as con:
            total_all = con.execute(_t("""
                SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG
            """)).scalar() or 1

            pending = con.execute(_t("""
                SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG
                WHERE HEADER_EXTRACTED IS NULL OR HEADER_EXTRACTED='N'
            """)).scalar() or 0

            rows = con.execute(_t(f"""
                SELECT TOP {ENRICH_CHUNK}
                    INVENTORY_ID, FILE_PATH, FILE_EXT
                FROM file_catalog.GLOBAL_FILE_CATALOG
                WHERE (HEADER_EXTRACTED IS NULL OR HEADER_EXTRACTED='N')
                  AND ISNULL(HEADER_EXTRACTED,'') <> 'S'
                ORDER BY SCAN_DATE DESC
            """)).fetchall()
    except Exception as e:
        st.error(f"Extraction query failed: {e}")
        st.session_state["wb_enriching"] = False
        return

    if not rows:
        st.success("✅ Phase 2 complete — all files extracted.")
        st.session_state["wb_enriching"] = False
        st.session_state["wb_enrich_done"] = True
        st.session_state["wb_enrich_total"] = total_all
        return

    # Live progress bar — kept as a handle and advanced as each file finishes,
    # so it moves continuously during the chunk instead of only on the rerun
    # between chunks. Reused for the final update after the writes commit.
    _done_before = max(0, total_all - pending)
    _bar = st.progress(
        min(1.0, _done_before / max(total_all, 1)),
        text=f"⚙️ Extracting — {_done_before:,} / {total_all:,} · next {len(rows)}…",
    )

    # ── Parallel extraction within this chunk ────────────────────────────
    # Each worker handles one file: read it, extract fields, return result.
    # Workers don't touch the DB — writes happen in the main thread below.
    # Each result carries its own elapsed time so we can spot slow formats
    # and compare sum-of-times (would-be sequential) vs wall-clock (parallel).
    import time as _time
    _t_chunk_start = _time.monotonic()

    def _worker(row):
        inv_id, fpath, fext = row
        _t0 = _time.monotonic()
        try:
            fields = _extract_fields(fpath, (fext or "").lower())
            # Size-gate skip — surface as a distinct status so it's written
            # as HEADER_EXTRACTED='S' and never re-attempted.
            if fields.get("skip_reason"):
                return ("skip", inv_id, fpath, fext, fields,
                        fields["skip_reason"],
                        _time.monotonic() - _t0)
            return ("ok", inv_id, fpath, fext, fields, None,
                    _time.monotonic() - _t0)
        except Exception as e:
            return ("err", inv_id, fpath, fext, None,
                    f"{type(e).__name__}: {e}",
                    _time.monotonic() - _t0)

    results = []
    _t_pool_start = _time.monotonic()
    with ThreadPoolExecutor(max_workers=PHASE2_WORKERS) as pool:
        futures = [pool.submit(_worker, row) for row in rows]
        _seen = 0
        for fut in as_completed(futures):
            try:
                # Per-file timeout: 60s ceiling. Hung extractor doesn't
                # block the chunk.
                results.append(fut.result(timeout=60))
            except Exception as e:
                results.append(("err", None, "", "", None,
                                f"worker died: {e}", 0.0))
            # Move the bar as each file lands.
            _seen += 1
            _bar.progress(
                min(1.0, (_done_before + _seen) / max(total_all, 1)),
                text=(f"⚙️ Extracting — {_done_before + _seen:,} / "
                      f"{total_all:,} · this batch {_seen}/{len(rows)}…"),
            )
    _t_pool_end = _time.monotonic()
    _pool_elapsed = _t_pool_end - _t_pool_start

    # ── Batched DB writes via executemany (one round-trip per statement) ─
    # Previous attempt at "batched" was just one transaction wrapping per-
    # row execute() calls. Diagnostic showed that didn't help — cost is
    # per-statement, not per-transaction. ODBC was still doing N round-
    # trips, just with one commit at the end.
    #
    # Real fix: build parameter LISTS, call executemany() once per
    # statement-shape. pyodbc has fast_executemany=True enabled in our
    # engine config (db.py line 159), which makes executemany pack many
    # parameter sets into a single network round-trip.
    #
    # Expected: 3 round-trips per chunk (one for GLOBAL_FILE_CATALOG,
    # one for FILE_WELL_HEADER MERGE, one for FILE_SEIS_HEADER MERGE)
    # instead of N. For chunks with mixed file types, the WELL/SEIS
    # MERGEs only run if there's data of that category.
    _t_writes_start = _time.monotonic()

    # Build parameter lists by statement shape
    update_params: list = []     # for GLOBAL_FILE_CATALOG (success path)
    error_params:  list = []     # for GLOBAL_FILE_CATALOG (error path - 'E')
    skip_params:   list = []     # for GLOBAL_FILE_CATALOG (skip path - 'S')
    well_params:   list = []     # for FILE_WELL_HEADER MERGE
    seis_params:   list = []     # for FILE_SEIS_HEADER MERGE
    done = 0
    last = ""
    per_ext_times: dict = {}

    for outcome, inv_id, fpath, fext, fields, err, elapsed in results:
        if fpath:
            last = Path(fpath).name
        ext_key = (fext or "?").lower()
        per_ext_times.setdefault(ext_key, []).append(elapsed)

        if outcome == "ok" and inv_id is not None:
            # Success: queue the GLOBAL_FILE_CATALOG UPDATE and the
            # matching MERGE for whichever header table this category lives in.
            score, readiness = _score(fields)
            category = fields.get("file_category", "UNKNOWN")
            # Extension gate for the seismic table: FILE_SEIS_HEADER is for
            # SEG-Y / P190 files ONLY. A non-seismic file (e.g. a lease/blocks
            # shapefile with a "survey" column, or a JSON) that gets classified
            # SEIS must NOT write a bogus survey there. If the category says SEIS
            # but the extension isn't a real seismic type, drop it out of the
            # seismic path (it stays inventoried; it just doesn't fabricate a
            # seismic survey). Belt-and-suspenders alongside the shapefile
            # classifier fix.
            _SEIS_EXTS = {".segy", ".sgy", ".seg", ".p190", ".p90", ".p1"}
            if category == "SEIS" and (fext or "").lower() not in _SEIS_EXTS:
                category = "UNKNOWN"
            # Canonicalize UWI to bare-14 before any catalog write, so a
            # display-formatted API (scout-ticket PDFs print '42-999-00001-00-00')
            # can't leak into MATCHED_UWI / FILE_WELL_HEADER.UWI and false-fail
            # the dv_well FK gate. One mutation covers both writes below.
            fields["uwi"] = _norm_uwi(fields.get("uwi"))
            # Refine FILE_TYPE_GROUP for seismic files now that we know
            # whether the file is 2D or 3D. Phase 1 sets "Seismic" for all
            # SEG-Y; Phase 2 upgrades it to "Seismic 2D" or "Seismic 3D"
            # based on the trace count heuristic in _extract_fields.
            _seis_type = fields.get("seis_set_type")  # "2D", "3D", or None
            if category == "SEIS" and _seis_type in ("2D", "3D"):
                _type_group = f"Seismic {_seis_type}"
            else:
                # Keep the existing group from Phase 1 scan for non-seismic
                # files and seismic files where type couldn't be determined.
                _type_group = EXT_GROUP.get((fext or "").lower(), "Other")
            update_params.append({
                "score":      score,
                "readiness":  readiness,
                "uwi":        _trunc(fields.get("uwi"), 40),
                "issues":     "; ".join(_issues(fields)),
                "type_group": _type_group,
                "id":         inv_id,
            })
            if category == "WELL":
                well_params.append({
                    "hid":     uuid.uuid5(uuid.NAMESPACE_URL, inv_id).hex.upper(),
                    "inv_id":  inv_id,
                    "uwi":     _trunc(fields.get("uwi"),40),
                    "wn":      _trunc(fields.get("well_name"),255),
                    "op":      _trunc(fields.get("operator"),255),
                    "fld":     _trunc(fields.get("well_field"),100),
                    "st":      _trunc(fields.get("state"),50),
                    "co":      _trunc(fields.get("county"),100),
                    "lat":     _trunc(fields.get("latitude"),30),
                    "lon":     _trunc(fields.get("longitude"),30),
                    "td":      _trunc(fields.get("total_depth"),20),
                    "spud":    _trunc(fields.get("spud_date"),20),
                    "rig":     _trunc(fields.get("rig_release"),20),
                    "rt":      _trunc(fields.get("report_type"),50),
                    "stype":   _trunc(fields.get("survey_type"),50),
                    "contr":   _trunc(fields.get("contractor"),255),
                    "conf":    _safe_num(fields.get("confidence")),
                })
            elif category == "SEIS":
                seis_params.append({
                    "hid":      uuid.uuid5(uuid.NAMESPACE_URL, inv_id+"_s").hex.upper(),
                    "inv_id":   inv_id,
                    "sn":       _trunc(fields.get("survey_name"),255),
                    "ln":       _trunc(fields.get("line_name"),255),
                    "stype":    _trunc(fields.get("seis_set_type"),40),
                    "sd":       _trunc(fields.get("survey_date"),20),
                    "contr":    _trunc(fields.get("contractor"),255),
                    "bmin_lat": _safe_coord(fields.get("bbox_min_lat")),
                    "bmax_lat": _safe_coord(fields.get("bbox_max_lat")),
                    "bmin_lon": _safe_coord(fields.get("bbox_min_lon")),
                    "bmax_lon": _safe_coord(fields.get("bbox_max_lon")),
                    "epsg":     _safe_epsg(fields.get("epsg_code")),
                    "si":       _safe_sample_interval(fields.get("sample_interval")),
                    "tc":       _safe_trace_count(fields.get("trace_count")),
                    "sf":       _trunc(fields.get("shot_first"),20),
                    "sl":       _trunc(fields.get("shot_last"),20),
                    "il_min":   fields.get("il_min"),
                    "il_max":   fields.get("il_max"),
                    "xl_min":   fields.get("xl_min"),
                    "xl_max":   fields.get("xl_max"),
                    "outline":  fields.get("survey_outline"),
                })
            done += 1
        elif outcome == "skip" and inv_id is not None:
            # Size-gate or other deliberate skip — write 'S' so the file
            # is never re-attempted. The skip_reason is stored in err.
            skip_params.append({"id": inv_id, "reason": (err or "SKIPPED")[:500]})
        elif inv_id is not None:
            # Extraction errored — queue the error-marker UPDATE
            error_params.append({"id": inv_id})

    # Execute the batched writes. Each executemany() is a single round-trip
    # with fast_executemany=True. We isolate each statement-shape in its
    # own try/except so a failure in one (e.g. a triggered constraint
    # violation in FILE_WELL_HEADER) doesn't poison the others.
    try:
        with engine.begin() as con:
            if update_params:
                con.execute(_t("""
                    UPDATE file_catalog.GLOBAL_FILE_CATALOG SET
                        CATALOG_SCORE     = :score,
                        CATALOG_READINESS = :readiness,
                        MATCHED_UWI       = :uwi,
                        CATALOG_ISSUES    = :issues,
                        FILE_TYPE_GROUP   = :type_group,
                        HEADER_EXTRACTED  = 'Y',
                        ROW_CHANGED_DATE  = GETUTCDATE()
                    WHERE INVENTORY_ID = :id
                """), update_params)
            if well_params:
                con.execute(_t("""
                    MERGE file_catalog.FILE_WELL_HEADER AS tgt
                    USING (SELECT :hid AS WELL_HEADER_ID) src
                    ON tgt.WELL_HEADER_ID = src.WELL_HEADER_ID
                    WHEN MATCHED THEN UPDATE SET
                        UWI=:uwi, WELL_NAME=:wn, OPERATOR=:op,
                        WELL_FIELD=:fld, STATE=:st, COUNTY=:co,
                        LATITUDE=:lat, LONGITUDE=:lon,
                        TOTAL_DEPTH=:td, SPUD_DATE=:spud,
                        RIG_RELEASE=:rig, REPORT_TYPE=:rt,
                        SURVEY_TYPE=:stype, CONTRACTOR=:contr,
                        CONFIDENCE=:conf, EXTRACTED_DATE=GETUTCDATE()
                    WHEN NOT MATCHED THEN INSERT (
                        WELL_HEADER_ID,INVENTORY_ID,
                        UWI,WELL_NAME,OPERATOR,WELL_FIELD,
                        STATE,COUNTY,LATITUDE,LONGITUDE,
                        TOTAL_DEPTH,SPUD_DATE,RIG_RELEASE,
                        REPORT_TYPE,SURVEY_TYPE,CONTRACTOR,CONFIDENCE,
                        EXTRACTED_DATE,EXTRACTED_BY
                    ) VALUES (
                        :hid,:inv_id,
                        :uwi,:wn,:op,:fld,
                        :st,:co,:lat,:lon,
                        :td,:spud,:rig,
                        :rt,:stype,:contr,:conf,
                        GETUTCDATE(),'DataWrangler'
                    );
                """), well_params)
            if seis_params:
                con.execute(_t("""
                    MERGE file_catalog.FILE_SEIS_HEADER AS tgt
                    USING (SELECT :hid AS SEIS_HEADER_ID) src
                    ON tgt.SEIS_HEADER_ID = src.SEIS_HEADER_ID
                    WHEN MATCHED THEN UPDATE SET
                        SURVEY_NAME=:sn, LINE_NAME=:ln,
                        SEIS_SET_TYPE=:stype, SURVEY_DATE=:sd,
                        CONTRACTOR=:contr,
                        BBOX_MIN_LAT=:bmin_lat, BBOX_MAX_LAT=:bmax_lat,
                        BBOX_MIN_LON=:bmin_lon, BBOX_MAX_LON=:bmax_lon,
                        EPSG_CODE=:epsg, SAMPLE_INTERVAL=:si,
                        TRACE_COUNT=:tc, SHOT_FIRST=:sf, SHOT_LAST=:sl,
                        IL_MIN=:il_min, IL_MAX=:il_max,
                        XL_MIN=:xl_min, XL_MAX=:xl_max,
                        SURVEY_OUTLINE=:outline,
                        EXTRACTED_DATE=GETUTCDATE()
                    WHEN NOT MATCHED THEN INSERT (
                        SEIS_HEADER_ID,INVENTORY_ID,
                        SURVEY_NAME,LINE_NAME,SEIS_SET_TYPE,SURVEY_DATE,
                        CONTRACTOR,BBOX_MIN_LAT,BBOX_MAX_LAT,
                        BBOX_MIN_LON,BBOX_MAX_LON,EPSG_CODE,
                        SAMPLE_INTERVAL,TRACE_COUNT,SHOT_FIRST,SHOT_LAST,
                        IL_MIN,IL_MAX,XL_MIN,XL_MAX,SURVEY_OUTLINE,
                        EXTRACTED_DATE,EXTRACTED_BY
                    ) VALUES (
                        :hid,:inv_id,
                        :sn,:ln,:stype,:sd,
                        :contr,:bmin_lat,:bmax_lat,
                        :bmin_lon,:bmax_lon,:epsg,
                        :si,:tc,:sf,:sl,
                        :il_min,:il_max,:xl_min,:xl_max,:outline,
                        GETUTCDATE(),'DataWrangler'
                    );
                """), seis_params)
            if error_params:
                con.execute(_t("""
                    UPDATE file_catalog.GLOBAL_FILE_CATALOG
                    SET HEADER_EXTRACTED='E',
                        ROW_CHANGED_DATE=GETUTCDATE()
                    WHERE INVENTORY_ID=:id
                """), error_params)
            if skip_params:
                # 'S' = deliberately skipped (too large, format limit).
                # Never re-attempted by the extraction loop.
                # Skip reason stored in CATALOG_READINESS so it's visible
                # in the Browse tab without a separate column.
                con.execute(_t("""
                    UPDATE file_catalog.GLOBAL_FILE_CATALOG
                    SET HEADER_EXTRACTED='S',
                        CATALOG_READINESS='SKIPPED',
                        ROW_CHANGED_DATE=GETUTCDATE()
                    WHERE INVENTORY_ID=:id
                """), skip_params)
    except Exception as e:
        st.error(f"Chunk transaction failed (rolled back): {e}")
        st.session_state["wb_enriching"] = False
        return
    _t_writes_end = _time.monotonic()
    _writes_elapsed = _t_writes_end - _t_writes_start

    # Sum of per-file extraction times. If sum_per_file >> pool_elapsed,
    # parallelism IS helping (threads overlapping). If sum ≈ pool, the
    # GIL or some serializing call is preventing real parallelism.
    _sum_per_file = sum(e for grp in per_ext_times.values() for e in grp)
    _speedup_ratio = (_sum_per_file / _pool_elapsed) if _pool_elapsed > 0 else 0.0

    total_done = total_all - pending + done
    pct = min(1.0, total_done / max(total_all, 1))

    # Final chunk? Flip to done and rerun so the page redraws with ONLY the
    # completion message — no leftover progress bar, "remaining" caption, or
    # chunk diagnostics lingering after extraction finishes.
    if not (len(rows) == ENRICH_CHUNK and pending > done):
        st.session_state["wb_enriching"]    = False
        st.session_state["wb_enrich_done"]  = True
        st.session_state["wb_enrich_total"] = total_all
        st.rerun()

    # Mid-run: advance the bar, show what's left and the chunk diagnostics,
    # then loop to the next chunk.
    _bar.progress(pct, text=(
        f"⚙️ Extracting — {total_done:,} / {total_all:,} "
        f"({pct*100:.0f}%) · {last}"
    ))
    st.caption(
        f"{pending - done:,} remaining · "
        f"{PHASE2_WORKERS} threads · "
        "click **⏹ Stop** to pause"
    )

    # ── Diagnostic: where did time go this chunk? ────────────────────────
    # Sum of per-file extraction times vs wall-clock pool time tells us
    # whether parallelism is actually working.
    _per_ext_summary = " · ".join(
        f"{ext}:{sum(times):.2f}s({len(times)})"
        for ext, times in sorted(per_ext_times.items(),
                                 key=lambda kv: -sum(kv[1]))
    )
    st.caption(
        f"⏱ chunk={_pool_elapsed + _writes_elapsed:.2f}s "
        f"(extract:{_pool_elapsed:.2f}s, writes:{_writes_elapsed:.2f}s) · "
        f"sum-of-files={_sum_per_file:.2f}s · "
        f"speedup={_speedup_ratio:.1f}× (ideal: {PHASE2_WORKERS}×) · "
        f"by-ext: {_per_ext_summary}"
    )

    import time
    time.sleep(0.1)
    st.rerun()


# =============================================================================
# Field extraction and enrichment write
# =============================================================================

# _extract_fields now lives in extract_core (imported above) so the
# pipeline process-pool workers can parse without importing streamlit.
# Single source of truth — edit it there.


def _write_enrichment(engine, inv_id: str, fields: dict):
    """Write extracted header fields to catalog tables.

    Single-file write — opens its own transaction. Kept for the few
    callers outside the Phase 2 chunk loop. The chunk loop uses
    _write_enrichment_on() with a shared connection for batched-commit
    performance.
    """
    with engine.begin() as con:
        _write_enrichment_on(con, inv_id, fields)


# ── Header-write SQL (shared by the per-row and batched writers) ───────────
# Executed one row at a time by _write_enrichment_on (manual review path) and
# as a single executemany batch per chunk by _write_enrichment_batch (the
# pipeline extract loop). fast_executemany on the engine collapses the batch
# into a few round-trips instead of two per file — that write phase was ~90%
# of extract wall-clock on SQL Express.
_SQL_GFC_UPDATE = """
    UPDATE file_catalog.GLOBAL_FILE_CATALOG SET
        CATALOG_SCORE     = :score,
        CATALOG_READINESS = :readiness,
        MATCHED_UWI       = :uwi,
        CATALOG_ISSUES    = :issues,
        SPATIAL_OUTLINE   = :spatial_outline,
        CATALOG_TABLE     = :catalog_table,
        HEADER_EXTRACTED  = 'Y',
        ROW_CHANGED_DATE  = GETUTCDATE()
    WHERE INVENTORY_ID = :id
"""

_SQL_WELL_MERGE = """
    MERGE file_catalog.FILE_WELL_HEADER AS tgt
    USING (SELECT :hid AS WELL_HEADER_ID) src
    ON tgt.WELL_HEADER_ID = src.WELL_HEADER_ID
    WHEN MATCHED THEN UPDATE SET
        UWI=:uwi, WELL_NAME=:wn, OPERATOR=:op,
        WELL_FIELD=:fld, STATE=:st, COUNTY=:co,
        LATITUDE=:lat, LONGITUDE=:lon,
        TOTAL_DEPTH=:td, SPUD_DATE=:spud,
        RIG_RELEASE=:rig, REPORT_TYPE=:rt,
        SURVEY_TYPE=:stype, CONTRACTOR=:contr,
        CONFIDENCE=:conf, EXTRACTED_DATE=GETUTCDATE()
    WHEN NOT MATCHED THEN INSERT (
        WELL_HEADER_ID,INVENTORY_ID,
        UWI,WELL_NAME,OPERATOR,WELL_FIELD,
        STATE,COUNTY,LATITUDE,LONGITUDE,
        TOTAL_DEPTH,SPUD_DATE,RIG_RELEASE,
        REPORT_TYPE,SURVEY_TYPE,CONTRACTOR,CONFIDENCE,
        EXTRACTED_DATE,EXTRACTED_BY
    ) VALUES (
        :hid,:inv_id,
        :uwi,:wn,:op,:fld,
        :st,:co,:lat,:lon,
        :td,:spud,:rig,
        :rt,:stype,:contr,:conf,
        GETUTCDATE(),'DataWrangler'
    );
"""

_SQL_SEIS_MERGE = """
    MERGE file_catalog.FILE_SEIS_HEADER AS tgt
    USING (SELECT :hid AS SEIS_HEADER_ID) src
    ON tgt.SEIS_HEADER_ID = src.SEIS_HEADER_ID
    WHEN MATCHED THEN UPDATE SET
        SURVEY_NAME=:sn, LINE_NAME=:ln,
        SEIS_SET_TYPE=:stype, SURVEY_DATE=:sd,
        CONTRACTOR=:contr,
        BBOX_MIN_LAT=:bmin_lat, BBOX_MAX_LAT=:bmax_lat,
        BBOX_MIN_LON=:bmin_lon, BBOX_MAX_LON=:bmax_lon,
        EPSG_CODE=:epsg, SAMPLE_INTERVAL=:si,
        TRACE_COUNT=:tc, SHOT_FIRST=:sf, SHOT_LAST=:sl,
        IL_MIN=:il_min, IL_MAX=:il_max,
        XL_MIN=:xl_min, XL_MAX=:xl_max,
        SURVEY_OUTLINE=:outline,
        EXTRACTED_DATE=GETUTCDATE()
    WHEN NOT MATCHED THEN INSERT (
        SEIS_HEADER_ID,INVENTORY_ID,
        SURVEY_NAME,LINE_NAME,SEIS_SET_TYPE,SURVEY_DATE,
        CONTRACTOR,BBOX_MIN_LAT,BBOX_MAX_LAT,
        BBOX_MIN_LON,BBOX_MAX_LON,EPSG_CODE,
        SAMPLE_INTERVAL,TRACE_COUNT,SHOT_FIRST,SHOT_LAST,
        IL_MIN,IL_MAX,XL_MIN,XL_MAX,SURVEY_OUTLINE,
        EXTRACTED_DATE,EXTRACTED_BY
    ) VALUES (
        :hid,:inv_id,
        :sn,:ln,:stype,:sd,
        :contr,:bmin_lat,:bmax_lat,
        :bmin_lon,:bmax_lon,:epsg,
        :si,:tc,:sf,:sl,
        :il_min,:il_max,:xl_min,:xl_max,:outline,
        GETUTCDATE(),'DataWrangler'
    );
"""


def _gfc_params(inv_id, fields):
    """GLOBAL_FILE_CATALOG update params. Caller normalizes fields['uwi'] first."""
    score, readiness = _score(fields)
    _cat = fields.get("file_category")
    # only stamp CATALOG_TABLE for the spatial feature types promote_X reads
    _cat_tbl = _cat if _cat in ("FIELD","LAND_TRACT","BOUNDARY","PIPELINE") else None
    return {"score": score, "readiness": readiness,
            "uwi": _trunc(fields.get("uwi"), 40),
            "issues": "; ".join(_issues(fields)), "id": inv_id,
            "spatial_outline": fields.get("spatial_outline"),
            "catalog_table": _cat_tbl}


def _date_only(v):
    """Keep just the date part (drops any trailing time), so a mm/dd/yyyy
    value stays ~10 chars and never overflows the 20-char date columns.
    Preserves the source date format; only strips the time."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return s.replace("T", " ").split(" ", 1)[0][:20]


def _valid_date(v):
    """Return the date as mm/dd/yyyy if v parses as a real date in a known
    format; else None. Nulls out mis-parsed junk ('Wed', 'Fri', '', long text)."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    from datetime import datetime as _dtm
    cands = [s]
    if " " in s:
        cands.append(s.split(" ", 1)[0])      # drop a trailing time
    fmts = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y",
            "%m-%d-%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y",
            "%B %d, %Y", "%d.%m.%Y", "%m.%d.%Y", "%Y%m%d", "%b-%d-%Y",
            "%d-%B-%Y", "%m/%d/%Y %H:%M:%S")
    for c in cands:
        for f in fmts:
            try:
                return _dtm.strptime(c, f).strftime("%m/%d/%Y")
            except ValueError:
                continue
    return None


def _well_params(inv_id, fields):
    return {
        "hid":    uuid.uuid5(uuid.NAMESPACE_URL, inv_id).hex.upper(),
        "inv_id": inv_id,
        "uwi":    _trunc(fields.get("uwi"), 40),
        "wn":     _trunc(fields.get("well_name"), 255),
        "op":     _trunc(fields.get("operator"), 255),
        "fld":    _trunc(fields.get("well_field"), 100),
        "st":     _trunc(fields.get("state"), 50),
        "co":     _trunc(fields.get("county"), 100),
        "lat":    _trunc(fields.get("latitude"), 30),
        "lon":    _trunc(fields.get("longitude"), 30),
        "td":     _trunc(fields.get("total_depth"), 20),
        "spud":   _valid_date(fields.get("spud_date")),
        "rig":    _valid_date(fields.get("rig_release")),
        "rt":     _trunc(fields.get("report_type"), 50),
        "stype":  _trunc(fields.get("survey_type"), 50),
        "contr":  _trunc(fields.get("contractor"), 255),
        "conf":   _safe_num(fields.get("confidence")),
    }


def _seis_params(inv_id, fields):
    return {
        "hid":      uuid.uuid5(uuid.NAMESPACE_URL, inv_id + "_s").hex.upper(),
        "inv_id":   inv_id,
        "sn":       _trunc(fields.get("survey_name"), 255),
        "ln":       _trunc(fields.get("line_name"), 255),
        "stype":    _trunc(fields.get("seis_set_type"), 40),
        "sd":       _valid_date(fields.get("survey_date")),
        "contr":    _trunc(fields.get("contractor"), 255),
        "bmin_lat": _safe_coord(fields.get("bbox_min_lat")),
        "bmax_lat": _safe_coord(fields.get("bbox_max_lat")),
        "bmin_lon": _safe_coord(fields.get("bbox_min_lon")),
        "bmax_lon": _safe_coord(fields.get("bbox_max_lon")),
        "epsg":     _safe_epsg(fields.get("epsg_code")),
        "si":       _safe_sample_interval(fields.get("sample_interval")),
        "tc":       _safe_trace_count(fields.get("trace_count")),
        "sf":       _trunc(fields.get("shot_first"), 20),
        "sl":       _trunc(fields.get("shot_last"), 20),
        "il_min":   fields.get("il_min"),
        "il_max":   fields.get("il_max"),
        "xl_min":   fields.get("xl_min"),
        "xl_max":   fields.get("xl_max"),
        "outline":  fields.get("survey_outline"),
    }


def _write_enrichment_on(con, inv_id: str, fields: dict):
    """Write extracted header fields for ONE file on a caller-provided
    connection (inside an active engine.begin(); does not commit). Used by the
    manual review path. The pipeline extract loop uses _write_enrichment_batch."""
    from sqlalchemy import text as _t
    fields["uwi"] = _norm_uwi(fields.get("uwi"))      # canonicalize once
    category = fields.get("file_category", "UNKNOWN")
    con.execute(_t(_SQL_GFC_UPDATE), _gfc_params(inv_id, fields))
    if category == "WELL":
        con.execute(_t(_SQL_WELL_MERGE), _well_params(inv_id, fields))
    elif category == "SEIS":
        con.execute(_t(_SQL_SEIS_MERGE), _seis_params(inv_id, fields))


def _clamp_well(rows):
    """Clamp each string well-param to its column width so no value overflows
    (defends against mis-parsed dates/fields and fast_executemany under-sizing)."""
    _w = {"uwi": 40, "wn": 255, "op": 255, "fld": 100, "st": 50, "co": 100,
          "spud": 20, "rig": 20, "rt": 50, "stype": 50, "contr": 255,
          "lat": 30, "lon": 30, "td": 20}
    for r in rows:
        for k, n in _w.items():
            v = r.get(k)
            if isinstance(v, str) and len(v) > n:
                r[k] = v[:n]
    return rows


def _write_enrichment_batch(con, items):
    """Write extracted header fields for MANY files in one batched round-trip
    per statement instead of two per file. items = [(inv_id, fields), …].

    Same proven UPDATE/MERGE SQL as the per-row path, run via executemany so
    fast_executemany collapses ~2 round-trips/file into ~3 calls per chunk —
    the write phase was ~90% of extract time on Express. Connection must be in
    an active engine.begin(); does not commit. Raises on failure so the caller
    can fall back to the per-row path in a fresh transaction."""
    from sqlalchemy import text as _t
    if not items:
        return
    for _iid, _f in items:                            # canonicalize UWI once each
        _f["uwi"] = _norm_uwi(_f.get("uwi"))
    gfc  = [_gfc_params(iid, f) for iid, f in items]
    well = [_well_params(iid, f) for iid, f in items
            if f.get("file_category") == "WELL"]
    seis = [_seis_params(iid, f) for iid, f in items
            if f.get("file_category") == "SEIS"]
    con.execute(_t(_SQL_GFC_UPDATE), gfc)             # executemany
    # per-row MERGE: the USING(SELECT ?) subquery defeats pyodbc fast_executemany
    # column sizing, under-sizing string buffers and truncating a later long
    # value; per-row binds each at its true size (still one transaction).
    for _w in _clamp_well(well):
        con.execute(_t(_SQL_WELL_MERGE), _w)
    for _sp in seis:
        con.execute(_t(_SQL_SEIS_MERGE), _sp)


def _score(fields: dict) -> tuple:
    score = 0
    if fields.get("uwi"):       score += 40
    if fields.get("well_name"): score += 20
    if fields.get("operator"):  score += 10
    if fields.get("latitude") and fields.get("longitude"): score += 20
    if fields.get("total_depth"): score += 10
    if score >= 80:  return score, "READY"
    if score >= 60:  return score, "REVIEW"
    if score >= 30:  return score, "NEEDS_UWI"
    return score, "ATTENTION"


def _issues(fields: dict) -> list:
    out = []
    if fields.get("extract_error"):
        out.append(str(fields["extract_error"])[:200])
    if not fields.get("uwi"):       out.append("No UWI")
    if not fields.get("well_name"): out.append("No well name")
    if not (fields.get("latitude") and fields.get("longitude")):
        out.append("No coordinates")
    return out


def _trunc(v, n):
    return str(v)[:n] if v is not None else None

def _norm_uwi(v):
    """Canonicalize any UWI/API string to bare-14 before it is written to the
    catalog. Reuses path_identity.norm_uwi14 (the same recipe the manual-review
    and FK paths use) so a display-formatted API from a PDF — e.g. a scout
    ticket's '42-999-00001-00-00' — collapses to '42999000010000' and matches
    dv_well at the FK gate instead of false-failing as an unmatched UWI.

    Falls back to a digit-strip if path_identity is unavailable; returns None
    for empty input so the 'extracted - no UWI' path is preserved.
    """
    if v is None or str(v).strip() == "":
        return None
    try:
        from dataview.core import path_identity as _pi
        u = _pi.norm_uwi14(str(v))
        if u:
            return u
    except Exception:
        pass
    d = re.sub(r"\D", "", str(v))
    return d if len(d) == 14 else (d or None)

def _safe_num(v):
    """Convert to float or None. Silently swallows bad input."""
    try:
        return float(str(v).replace(",","").strip()) if v is not None else None
    except (ValueError, TypeError):
        return None

def _safe_int(v):
    """Convert to int or None. Silently swallows bad input."""
    try:
        return int(float(str(v).strip())) if v is not None else None
    except (ValueError, TypeError):
        return None


# ── Bounded variants ────────────────────────────────────────────────────
# pyodbc's fast_executemany pre-checks numeric ranges and rejects the
# whole batch if any value overflows the target column's precision/scale.
# These helpers clamp values to known-safe ranges, dropping outliers to
# NULL rather than letting them poison the batch.

def _safe_coord(v):
    """Latitude or longitude. Returns float in [-180, 180] or None."""
    n = _safe_num(v)
    if n is None or not (-180.0 <= n <= 180.0):
        return None
    return n

def _safe_sample_interval(v):
    """Seismic sample interval (microseconds). Positive, sane upper bound."""
    n = _safe_num(v)
    # SEGY interval is in microseconds; legitimate values are 250-16000.
    # Anything outside [0, 1_000_000] is garbage from a bad header read.
    if n is None or n < 0 or n > 1_000_000:
        return None
    return n

def _safe_trace_count(v):
    """Seismic trace count. Positive int, sane upper bound."""
    n = _safe_int(v)
    if n is None or n < 0 or n > 100_000_000:
        return None
    return n

def _safe_epsg(v):
    """EPSG code. 4 to 6 digit positive int."""
    n = _safe_int(v)
    if n is None or n < 1000 or n > 999_999:
        return None
    return n


# =============================================================================
# Tab 2 -- Browse & View
# =============================================================================

def _set_readiness_cataloged(engine, inv_id):
    """DEPRECATED no-op. Readiness on the catalog/promote axis is now DERIVED
    from row reality by catalog_readiness.reconcile_readiness — the single owner
    — not asserted in-flow. This used to optimistically stamp CATALOGED before
    the cat_* rows were proven to persist, which let a row-less capture mark a
    file done so promote had nothing to lift (the stranded-file bug). Kept as a
    stub so existing call sites compile; it intentionally writes nothing. The
    capture and promote stages call reconcile_readiness at end of pass."""
    return


def _backfill_cataloged(engine):
    """Set CATALOG_READINESS='CATALOGED' for every file already captured in a
    cat_* mirror. One set-based UPDATE per mirror table (EXISTS join — no
    per-row loops), so it stays fast on a large catalog. Returns rows updated."""
    from sqlalchemy import text as _t
    with engine.connect() as con:
        tabs = [r[0] for r in con.execute(_t("""
            SELECT t.name
            FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.columns c ON c.object_id = t.object_id
            WHERE s.name = 'file_catalog'
              AND t.name LIKE 'cat[_]%'
              AND c.name = 'INVENTORY_ID'""")).fetchall()]
    if not tabs:
        return 0
    updated = 0
    with engine.begin() as con:
        for t in tabs:
            r = con.execute(_t(f"""
                UPDATE g SET g.CATALOG_READINESS = 'CATALOGED'
                FROM file_catalog.GLOBAL_FILE_CATALOG g
                WHERE ISNULL(g.CATALOG_READINESS,'') <> 'CATALOGED'
                  AND EXISTS (SELECT 1 FROM file_catalog.{t} m
                              WHERE m.INVENTORY_ID = g.INVENTORY_ID)"""))
            updated += r.rowcount or 0
    return updated


def _tab_browse(engine, dialect):
    from sqlalchemy import text as _t

    st.markdown("#### 📂 Browse & View")

    # ── Filters ───────────────────────────────────────────────────────────────
    f1, f2, f3, f4 = st.columns(4)
    grp = f1.multiselect(
        "File type",
        ["PDF","Well Log","Seismic","Shapefile","Office","Image"],
        key="wb_grp",
        placeholder="All types",
    )
    rd = f2.selectbox(
        "Readiness",
        ["All","CATALOGED","READY","REVIEW","NEEDS_UWI","ATTENTION"],
        key="wb_rd",
    )
    srch = f3.text_input("Filename contains", key="wb_srch",
                          placeholder="partial name...")
    flagged = f4.checkbox("Flagged only", key="wb_flagged")

    if st.button("🔍 Search", type="primary", key="wb_search_btn"):
        conditions = ["1=1"]
        params = {}
        if grp:
            _ph = ",".join(f":grp{i}" for i in range(len(grp)))
            conditions.append(f"FILE_TYPE_GROUP IN ({_ph})")
            for i, _g in enumerate(grp):
                params[f"grp{i}"] = _g
        if rd != "All":
            conditions.append("CATALOG_READINESS=:rd")
            params["rd"] = rd
        if srch:
            conditions.append("FILE_NAME LIKE :srch")
            params["srch"] = f"%{srch}%"
        if flagged:
            conditions.append("ISNULL(FLAG_DELETE,'N')='Y'")
        try:
            with engine.connect() as con:
                rows = con.execute(_t(f"""
                    SELECT TOP 500
                        g.INVENTORY_ID, g.FILE_PATH, g.FILE_NAME, g.FILE_EXT,
                        g.FILE_TYPE_GROUP, g.FILE_SIZE_KB,
                        g.CATALOG_READINESS, g.CATALOG_SCORE,
                        g.MATCHED_UWI,
                        (SELECT TOP 1 h.WELL_NAME
                           FROM file_catalog.FILE_WELL_HEADER h
                          WHERE h.INVENTORY_ID = g.INVENTORY_ID) AS WELL_NAME,
                        g.CATALOG_ISSUES,
                        ISNULL(g.FLAG_DELETE,'N') FLAG_DELETE,
                        g.HEADER_EXTRACTED
                    FROM file_catalog.GLOBAL_FILE_CATALOG g
                    WHERE {" AND ".join(conditions)}
                    ORDER BY g.CATALOG_SCORE DESC, g.FILE_NAME
                """), params).fetchall()
            df = pd.DataFrame(rows, columns=[
                "INVENTORY_ID","FILE_PATH","FILE_NAME","FILE_EXT",
                "FILE_TYPE_GROUP","FILE_SIZE_KB",
                "CATALOG_READINESS","CATALOG_SCORE",
                "MATCHED_UWI","WELL_NAME","CATALOG_ISSUES",
                "FLAG_DELETE","HEADER_EXTRACTED",
            ])
            st.session_state["wb_results"] = df
            st.session_state.pop("wb_nav_idx", None)
        except Exception as e:
            st.error(f"Search failed: {e}")

    if st.button("🗂 Refresh cataloged status", key="wb_backfill_cat",
                 help="Reconcile every file's readiness to row reality: CATALOGED "
                      "where cat_* rows exist, PROMOTED where they reached dv_*, and "
                      "demote any CATALOGED with no rows behind it. Safe to re-run."):
        try:
            try:
                from dataview.file_catalog.catalog_readiness import reconcile_readiness
            except Exception:
                from dataview.file_catalog.catalog_readiness import reconcile_readiness
            _res = reconcile_readiness(engine, "mssql", log=lambda *_: None)
            _n = _res.get("changed", 0)
            st.success(f"Reconciled readiness — {_n:,} file(s) updated.")
            st.session_state.pop("wb_results", None)
            st.rerun()
        except Exception as e:
            st.error(f"Reconcile failed: {e}")

    df = st.session_state.get("wb_results")
    if df is None:
        st.info("Set filters and click Search.")
        return

    st.caption(f"{len(df):,} files (max 500)")
    st.dataframe(
        df[["FILE_NAME","FILE_EXT","FILE_TYPE_GROUP",
            "CATALOG_READINESS","CATALOG_SCORE",
            "MATCHED_UWI","FLAG_DELETE"]].rename(
                columns={"CATALOG_READINESS": "Readiness"}),
        hide_index=True, use_container_width=True,
    )

    if df.empty:
        return

    st.divider()
    mode = st.radio(
        "Mode",
        ["🔎 Interactive — view & load", "📦 Batch load"],
        horizontal=True, key="wb_mode", label_visibility="collapsed",
    )
    st.divider()
    if mode.startswith("🔎"):
        _wb_nav(engine, dialect, df)
    else:
        _wb_batch(engine, dialect, df)


def _wb_nav(engine, dialect, df):
    """Prev/Next nav + file detail + viewer + extract + load."""
    from sqlalchemy import text as _t

    n       = len(df)
    idx_key = "wb_nav_idx"
    if idx_key not in st.session_state:
        st.session_state[idx_key] = 0
    idx = max(0, min(st.session_state[idx_key], n-1))

    # Nav bar
    c_prev, c_info, c_next, c_jump = st.columns([1,4,1,2])
    with c_prev:
        if st.button("◀ Prev", key="wb_prev", disabled=(idx==0)):
            st.session_state[idx_key] = idx-1
            st.rerun()
    with c_next:
        if st.button("Next ▶", key="wb_next", disabled=(idx>=n-1)):
            st.session_state[idx_key] = idx+1
            st.rerun()
    with c_info:
        row = df.iloc[idx]
        badge = " 🚩" if row["FLAG_DELETE"]=="Y" else ""
        st.markdown(f"**{idx+1} / {n}**{badge}  `{row['FILE_NAME']}`")
    with c_jump:
        names  = df["FILE_NAME"].tolist()
        jumped = st.selectbox("Jump to", names, index=idx,
                              key="wb_jump",
                              label_visibility="collapsed")
        ji = names.index(jumped)
        if ji != idx:
            st.session_state[idx_key] = ji
            st.rerun()

    row    = df.iloc[idx]
    fpath  = row["FILE_PATH"]
    fext   = row["FILE_EXT"].lower()
    inv_id = row["INVENTORY_ID"]

    # ── Action bar ────────────────────────────────────────────────────────────
    a1, a2, a3, a4 = st.columns(4)

    is_flagged = row["FLAG_DELETE"] == "Y"
    if a1.button(
            "🚩 Unflag" if is_flagged else "🚩 Flag",
            key="wb_flag_btn"):
        _toggle_flag(engine, inv_id, not is_flagged)
        st.session_state.pop("wb_results", None)
        st.rerun()

    if a2.button("🔄 Re-extract", key="wb_reenrich"):
        with st.spinner("Extracting..."):
            try:
                fields = _extract_fields(fpath, fext)
                _write_enrichment(engine, inv_id, fields)
                st.success("Re-extracted.")
                st.session_state.pop("wb_results", None)
                st.rerun()
            except Exception as e:
                st.error(f"Re-extract failed: {e}")

    # Download
    if Path(fpath).exists():
        try:
            a4.download_button(
                "⬇ Download", data=Path(fpath).read_bytes(),
                file_name=row["FILE_NAME"], key="wb_dl_btn")
        except Exception:
            pass

    # ── Mark as bad ───────────────────────────────────────────────────────────
    # Fingerprint a junk / badly-formatted file so the next crawl skips it and
    # drop it from the catalog now.
    if "wb_bad_reason" not in st.session_state:
        st.session_state["wb_bad_reason"] = "junk / bad format"
    bc1, bc2 = st.columns([3, 1])
    bc1.text_input("Reason", key="wb_bad_reason",
                   label_visibility="collapsed",
                   placeholder="why is this file bad?")
    if bc2.button("🚫 Mark bad", key="wb_bad_btn",
                  help="Fingerprint this file as bad and remove it from the "
                       "catalog — the next crawl will skip it."):
        try:
            _mark_bad(engine, inv_id, fpath, row.get("FILE_NAME"),
                      row.get("FILE_SIZE_KB"),
                      st.session_state.get("wb_bad_reason"))
            st.success(f"Marked bad — the next crawl will skip "
                       f"`{row.get('FILE_NAME', '')}`.")
            st.session_state.pop("wb_results", None)
            st.rerun()
        except Exception as e:
            st.error(f"Mark bad failed: {e}")

    st.divider()

    # ── Catalog attributes from header tables ─────────────────────────────────
    _show_header_attrs(engine, inv_id, row)

    # ── Universal viewer ──────────────────────────────────────────────────────
    if Path(fpath).exists():
        try:
            from dataview.file_catalog.file_viewer import view as _view
            _view(fpath, fext)
        except Exception as e:
            st.error(f"Viewer error: {e}")
    else:
        st.warning(f"File not found on disk: `{fpath}`")

    # ── Extract & Load (automatic) ───────────────────────────────────────────
    _extract_and_load(engine, dialect, fpath, fext, inv_id, row)


def _show_header_attrs(engine, inv_id: str, row):
    """Show enriched header from FILE_WELL_HEADER or FILE_SEIS_HEADER."""
    from sqlalchemy import text as _t

    grp = row.get("FILE_TYPE_GROUP","")
    is_seis = grp in ("Seismic","Shapefile")

    try:
        with engine.connect() as con:
            if is_seis:
                r = con.execute(_t("""
                    SELECT SURVEY_NAME, LINE_NAME, SEIS_SET_TYPE,
                           SURVEY_DATE, CONTRACTOR,
                           BBOX_MIN_LAT, BBOX_MAX_LAT,
                           BBOX_MIN_LON, BBOX_MAX_LON,
                           EPSG_CODE, SAMPLE_INTERVAL, TRACE_COUNT,
                           SHOT_FIRST, SHOT_LAST
                    FROM file_catalog.FILE_SEIS_HEADER
                    WHERE INVENTORY_ID=:id
                """), {"id": inv_id}).fetchone()
                if r:
                    attrs = dict(zip([
                        "Survey Name","Line Name","Set Type","Survey Date",
                        "Contractor","Min Lat","Max Lat","Min Lon","Max Lon",
                        "EPSG","Sample Interval","Trace Count",
                        "Shot First","Shot Last",
                    ], r))
                else:
                    attrs = {}
            else:
                r = con.execute(_t("""
                    SELECT UWI, WELL_NAME, OPERATOR, WELL_FIELD,
                           STATE, COUNTY, LATITUDE, LONGITUDE,
                           TOTAL_DEPTH, SPUD_DATE, RIG_RELEASE,
                           REPORT_TYPE, SURVEY_TYPE, CONTRACTOR, CONFIDENCE
                    FROM file_catalog.FILE_WELL_HEADER
                    WHERE INVENTORY_ID=:id
                """), {"id": inv_id}).fetchone()
                if r:
                    attrs = dict(zip([
                        "UWI","Well Name","Operator","Field",
                        "State","County","Latitude","Longitude",
                        "Total Depth","Spud Date","Rig Release",
                        "Report Type","Survey Type","Contractor","Confidence",
                    ], r))
                else:
                    attrs = {}

        if attrs:
            with st.expander("📋 Extracted header", expanded=True):
                adf = pd.DataFrame(
                    [{"Field": k, "Value": str(v)}
                     for k, v in attrs.items()
                     if v is not None and str(v).strip()
                     not in ("","None","nan")]
                )
                if not adf.empty:
                    st.dataframe(adf, hide_index=True,
                                 use_container_width=True)
        else:
            st.caption("No header extracted yet — click Re-extract.")
    except Exception as e:
        st.caption(f"Header lookup: {e}")


def _extract_and_load(engine, dialect, fpath, fext, inv_id, row):
    """Extract structured data rows from file and offer DB load."""
    from sqlalchemy import text as _t

    st.markdown("#### 📐 Extracted Data")

    rows, label = _do_extract(fpath, fext)

    if not rows:
        st.info(f"No structured data extracted for {fext} files.")
        return

    df = pd.DataFrame(rows).fillna("")
    st.metric(f"{label} extracted", len(df))
    st.dataframe(df, hide_index=True, use_container_width=True)

    c1, c2 = st.columns(2)
    c1.download_button(
        f"⬇ Download {label} CSV",
        data=df.to_csv(index=False),
        file_name=f"{Path(fpath).stem}_{label.lower().replace(' ','_')}.csv",
        mime="text/csv",
        key="wb_extract_dl",
    )

    # Load to DB — check well exists first
    uwi = row.get("MATCHED_UWI","") or ""
    if not uwi:
        c2.warning("No UWI — load wells from Header Files tab first.")
        return

    # Capture writes to the file_catalog.cat_* mirrors and does NOT require a
    # dv_well header to exist — the header is created later by promote_catalog
    # from cat_well. Key everything off the document UWI.
    _resolved = uwi

    if c2.button(
            f"🚀 Capture {len(rows)} records to catalog",
            type="primary", key="wb_load_btn"):
        _do_load(engine, dialect, fpath, fext, _resolved, rows)


def _do_extract(fpath: str, fext: str) -> tuple:
    """Extract structured data rows. Returns (rows, label)."""
    try:
        if fext == ".pdf":
            from dataview.file_catalog.pdf_survey_catalog import (
                classify_pdf, extract_stations,
                extract_eowr, extract_rft_data,
                extract_well_test, extract_petrophysical,
                extract_casing_cement, extract_ddr,
                extract_scout_ticket,
                RT_DIRECTIONAL, RT_EOWR, RT_FORMATION, RT_RFT,
                RT_WELL_TEST, RT_PETRO, RT_CASING,
                RT_DDR, RT_SCOUT,
            )
            # extract_core / RT_CORE are not present in all builds — guard them
            # so a missing symbol can't break the whole PDF preview path.
            try:
                from dataview.file_catalog.pdf_survey_catalog import extract_core, RT_CORE
            except ImportError:
                extract_core, RT_CORE = None, "CORE_ANALYSIS"
            cl = classify_pdf(fpath)
            rt = cl.get("report_type","UNKNOWN")

            if rt == RT_DIRECTIONAL:
                r = extract_stations(fpath)
                return r.get("stations",[]), "Stations"
            elif rt in (RT_EOWR, RT_FORMATION):
                r = extract_eowr(fpath)
                return r.get("strat",[]), "Strat tops"
            elif rt == RT_RFT:
                return extract_rft_data(fpath).get("rows",[]), "RFT rows"
            elif rt == RT_WELL_TEST:
                return extract_well_test(fpath).get("flow_rows",[]), "Flow periods"
            elif rt in (RT_PETRO,"PETROPHYSICAL"):
                from dataview.file_catalog.extract_petro import extract_petro
                r = extract_petro(fpath)
                if r.get("ok"):
                    zones = r.get("zones", [])
                    return zones, f"Petro zones ({len(zones)})"
                else:
                    # Fallback to old extractor
                    r2 = extract_petrophysical(fpath)
                    return r2.get("zones") or r2.get("interval") or [], "Zones"
            elif rt == RT_CASING:
                r = extract_casing_cement(fpath)
                return r.get("casing",[]) + r.get("cement",[]), "Casing"
            elif rt == RT_CORE:
                if extract_core:
                    return extract_core(fpath).get("samples",[]), "Core samples"
                return [], "Core (no extractor)"
            elif rt == RT_DDR:
                return extract_ddr(fpath).get("ops",[]), "Operations"
            elif rt == RT_SCOUT:
                sc = extract_scout_ticket(fpath)
                summary = []
                if sc.get("header"):
                    summary.append({"Section": "Well header",
                                    "Items": len(sc["header"])})
                for _k, _lbl in (("tops", "Formation tops"),
                                 ("dst", "DST tests"),
                                 ("frac", "Frac stages"),
                                 ("core", "Core samples"),
                                 ("ip_rows", "IP / production")):
                    _n = len(sc.get(_k) or [])
                    if _n:
                        summary.append({"Section": _lbl, "Items": _n})
                return summary, "Scout sections"
            else:
                return [], "Records"

        elif fext == ".las":
            import lasio
            las = lasio.read(fpath)
            df  = las.df().reset_index()
            return df.to_dict("records"), "Curve rows"

        elif fext in SEGY_EXTS:
            from dataview.file_catalog.segy_header import sample_trace_rows
            return sample_trace_rows(fpath, limit=100), "Trace headers"

        elif fext in SHP_EXTS:
            import geopandas as gpd
            gdf = gpd.read_file(fpath)
            return gdf.drop(
                columns=["geometry"], errors="ignore"
            ).to_dict("records"), "Features"

    except Exception as e:
        st.error(f"Extraction error: {e}")
    return [], "Records"


def _load_rows_to_catalog(engine, dialect, fpath, fext, uwi, rows):
    """Capture extracted rows into the file_catalog.cat_* mirrors.

    Pure logic — performs NO Streamlit output, so the same path drives both
    the interactive viewer and the batch loader. Returns:
        {ok, loaded, errors:[...], rt, note}
    note is one of "", "petro_fail:<msg>", "not_impl:<rt>",
    "shapefile", "unsupported".  header-capture problems are recorded in
    errors with a "header capture: " prefix (non-fatal).
    """
    from sqlalchemy import text as _t
    res = {"ok": False, "loaded": 0, "errors": [], "rt": "", "note": "",
           "detail": {}}

    well_info = {"uwi": uwi, "well_name": "", "operator": "",
                 "source_path": fpath}
    # resolve INVENTORY_ID for provenance (the cat_* mirrors record it)
    try:
        with engine.connect() as _c:
            _r = _c.execute(_t(
                "SELECT TOP 1 INVENTORY_ID FROM file_catalog.GLOBAL_FILE_CATALOG "
                "WHERE FILE_PATH = :p"), {"p": fpath}).fetchone()
        well_info["inventory_id"] = _r[0] if _r else None
    except Exception:
        well_info["inventory_id"] = None

    try:
        if fext == ".pdf":
            from dataview.file_catalog.pdf_survey_catalog import (
                classify_pdf, load_to_ppdm, RT_DIRECTIONAL, RT_SCOUT)
            # Newer symbols — tolerate an older deployed pdf_survey_catalog by
            # falling back to the known string values if the constants/function
            # aren't exported yet (prevents a partial deploy from breaking ALL
            # PDF cataloging on an ImportError).
            try:
                from dataview.file_catalog.pdf_survey_catalog import (
                    extended_classify_pdf, RT_EOWR as _RT_EOWR,
                    RT_WELL_TEST as _RT_WT, RT_DDR as _RT_DDR,
                    RT_RFT as _RT_RFT, RT_CASING as _RT_CAS)
                RT_EOWR, RT_WELL_TEST = _RT_EOWR, _RT_WT
                RT_DDR, RT_RFT, RT_CASING = _RT_DDR, _RT_RFT, _RT_CAS
            except ImportError:
                extended_classify_pdf = None
                RT_EOWR, RT_WELL_TEST = "END_OF_WELL", "WELL_TEST"
                RT_DDR = "DAILY_DRILLING_REPORT"
                RT_RFT, RT_CASING = "RFT_MDT", "CASING_CEMENTING"
            cl = classify_pdf(fpath)
            rt = cl.get("report_type", "UNKNOWN")
            # The base classifier only knows 5 types and mis-routes the rest:
            # scout tickets fall under its COMPLETION keyword, EOW reports under
            # its survey keywords. Consult the extended classifier, which detects
            # scout / EOW / well-test / DDR / RFT / casing, and let it OVERRIDE
            # for those documents (a real directional survey still wins, since
            # the extended classifier won't claim it). Merge well-header fields
            # from whichever classifier found them.
            try:
                ex = extended_classify_pdf(fpath) if extended_classify_pdf else {}
                ex_rt = ex.get("report_type", "UNKNOWN")
                _EXT_TYPES = {RT_SCOUT, RT_EOWR, RT_WELL_TEST,
                              RT_DDR, RT_RFT, RT_CASING}
                # EOW reports carry survey-like depth/TVD tables, so the base
                # classifier often mislabels them DIRECTIONAL. A confident EOW
                # detection (≥0.5) overrides even DIRECTIONAL; the other extended
                # types only override non-DIRECTIONAL base results.
                if (ex_rt == RT_EOWR and ex.get("confidence", 0) >= 0.5):
                    rt = RT_EOWR
                elif ex_rt in _EXT_TYPES and rt != RT_DIRECTIONAL:
                    rt = ex_rt
                if rt == ex_rt:
                    for _k in ("well_name", "operator", "field", "state",
                               "county", "country", "total_depth",
                               "latitude", "longitude"):
                        if not cl.get(_k) and ex.get(_k):
                            cl[_k] = ex.get(_k)
            except Exception:
                pass
            res["rt"] = rt
            well_info.update({
                "well_name": cl.get("well_name", ""),
                "operator":  cl.get("operator", ""),
            })
            # capture the well header into cat_well so promote_catalog can
            # create the dv_well record downstream from the document header.
            # Scout tickets are skipped here — load_scout writes a richer
            # cat_well header (county, lease, lat/long, dates) itself.
            if uwi and rt != RT_SCOUT:
                try:
                    import uuid as _uuid
                    from datetime import datetime as _dt
                    try:
                        from dataview.file_catalog.catalog_capture import capture as _cap
                    except ImportError:
                        from dataview.file_catalog.catalog_capture import capture as _cap
                    _now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
                    _hn = _cap(engine, "cat_well", [{
                        "WELL_NAME":        cl.get("well_name") or uwi,
                        "OPERATOR_NAME":    cl.get("operator"),
                        "FIELD_NAME":       cl.get("field"),
                        "PROVINCE_STATE":   cl.get("state"),
                        "COUNTY":           cl.get("county"),
                        "COUNTRY":          cl.get("country"),
                        "SURFACE_LATITUDE":  _safe_coord(cl.get("latitude")),
                        "SURFACE_LONGITUDE": _safe_coord(cl.get("longitude")),
                        "FINAL_TD":         cl.get("total_depth"),
                        "ACTIVE_IND":       "Y",
                        "ROW_QUALITY":      "FINAL",
                        "PPDM_GUID":        str(_uuid.uuid4()),
                        "ROW_CREATED_BY":   "DataWrangler",
                        "ROW_CREATED_DATE": _now,
                    }], uwi=uwi, inventory_id=well_info.get("inventory_id"),
                       source_path=fpath, source="PDF_HEADER")
                    if _hn:
                        res["detail"]["cat_well"] = (
                            res["detail"].get("cat_well", 0) + _hn)
                except Exception as _he:
                    res["errors"].append(f"header capture: {_he}")

            if rt == RT_DIRECTIONAL:
                r = load_to_ppdm(well_info=well_info, stations=rows,
                                 engine=engine, dialect=dialect)
            else:
                from dataview.file_catalog.pdf_db_loader import (
                    load_formation_tops, load_casing, load_scout, load_core,
                )
                # load_well_test / load_rft are newer; if a deployed
                # pdf_db_loader predates them, degrade gracefully (header-only
                # capture still marks the file cataloged) instead of breaking
                # the entire PDF branch on an ImportError.
                try:
                    from dataview.file_catalog.pdf_db_loader import load_well_test
                except ImportError:
                    load_well_test = None
                try:
                    from dataview.file_catalog.pdf_db_loader import load_rft
                except ImportError:
                    load_rft = None
                # RT_EOWR/RT_WELL_TEST/RT_DDR/RT_RFT/RT_CASING are already in
                # scope from the defensive block at the top of this branch.
                # Resolve the remaining ones with literal fallbacks so a missing
                # constant in an older pdf_survey_catalog can't ImportError and
                # kill the whole capture (this was the RT_CORE failure).
                try:
                    from dataview.file_catalog.pdf_survey_catalog import (
                        RT_FORMATION as _RT_FORM, RT_PETRO as _RT_PET,
                        RT_CORE as _RT_CORE, RT_SCOUT as _RT_SCOUT)
                    RT_FORMATION, RT_PETRO = _RT_FORM, _RT_PET
                    RT_CORE = _RT_CORE
                except ImportError:
                    RT_FORMATION, RT_PETRO = "FORMATION_TOPS", "PETROPHYSICAL"
                    RT_CORE = "CORE_ANALYSIS"
                kw = dict(engine=engine, dialect=dialect,
                          well_info=well_info, rows=rows)
                if rt in (RT_EOWR, RT_FORMATION):
                    r = load_formation_tops(**kw)
                elif rt == RT_CASING:
                    r = load_casing(**kw)
                elif rt == RT_CORE:
                    r = load_core(**kw)
                elif rt == RT_SCOUT:
                    r = load_scout(**kw)
                elif rt == RT_WELL_TEST and load_well_test:
                    r = load_well_test(**kw)
                elif rt == RT_RFT and load_rft:
                    r = load_rft(**kw)
                elif rt in (RT_PETRO, "PETROPHYSICAL"):
                    from dataview.file_catalog.extract_petro import (
                        extract_petro, load_petro_zones)
                    petro = extract_petro(fpath)
                    if not petro.get("ok"):
                        res["note"] = f"petro_fail:{petro.get('error')}"
                        # header may still have been captured above → mark it
                        if res["detail"].get("cat_well"):
                            res["ok"] = True
                            _set_readiness_cataloged(
                                engine, well_info.get("inventory_id"))
                        return res
                    r = load_petro_zones(engine, dialect, petro, uwi)
                else:
                    # No detail loader for this type (e.g. DDR) — but if we
                    # captured the well header above, that IS a catalog result.
                    res["note"] = f"not_impl:{rt}"
                    if res["detail"].get("cat_well"):
                        res["ok"] = True
                        _set_readiness_cataloged(
                            engine, well_info.get("inventory_id"))
                    return res

            res["loaded"] = r.get("loaded", 0)
            for _k, _v in (r.get("detail") or {}).items():
                res["detail"][_k] = res["detail"].get(_k, 0) + _v
            res["errors"].extend(r.get("errors", []))
            res["ok"] = not [e for e in res["errors"]
                             if not str(e).startswith("header capture:")]
            # Mark cataloged if EITHER detail rows OR a well header landed.
            if res["loaded"] or res["detail"].get("cat_well"):
                _set_readiness_cataloged(engine, well_info.get("inventory_id"))
            return res

        elif fext in SHP_EXTS:
            # Classify first (gives feature_type + column_map), then route WELL
            # point features through the CATALOG-aware loader so rows land in
            # file_catalog.cat_well keyed by INVENTORY_ID — promote_catalog lifts
            # them into dataview.dv_well. (load_to_ppdm writes dbo.WELL directly
            # and bypasses the cat_* → dv_* pipeline, so it is NOT used here.)
            from dataview.mapping.shapefile_catalog import (
                classify_shapefile, capture_wells_to_catalog,
                capture_features_to_catalog, FT_WELL)
            cl = classify_shapefile(fpath)
            ft = cl.get("feature_type")
            _FEAT_CAT = {"FIELD": "FIELD", "LEASE": "LAND_TRACT",
                         "BOUNDARY": "BOUNDARY", "PIPELINE": "PIPELINE"}
            if ft == FT_WELL:
                r = capture_wells_to_catalog(
                    file_path=fpath, column_map=cl.get("column_map") or {},
                    engine=engine, well_info=well_info,
                    dialect=dialect, source="SHAPEFILE")
                res["loaded"] = r.get("loaded", 0)
                res["detail"]["cat_well"] = (
                    res["detail"].get("cat_well", 0) + r.get("loaded", 0))
                res["errors"].extend(r.get("errors", []))
                res["ok"] = not [e for e in res["errors"]
                                 if not str(e).startswith("header capture:")]
                res["note"] = "shapefile"
                if res["loaded"]:
                    _set_readiness_cataloged(engine,
                                             well_info.get("inventory_id"))
            elif ft in _FEAT_CAT:
                # FIELD/LEASE/BOUNDARY/PIPELINE — per-feature capture, one cat_*
                # row per polygon/line with geometry; promote_* builds geography.
                r = capture_features_to_catalog(
                    file_path=fpath, feature_category=_FEAT_CAT[ft],
                    engine=engine, well_info=well_info,
                    dialect=dialect, source="SHAPEFILE")
                n = r.get("loaded", 0)
                res["loaded"] = n
                res["detail"].update(r.get("detail", {}))
                res["errors"].extend(r.get("errors", []))
                res["ok"] = not [e for e in res["errors"]
                                 if not str(e).startswith("header capture:")]
                res["note"] = "shapefile"
                if n:
                    _set_readiness_cataloged(engine,
                                             well_info.get("inventory_id"))
            else:
                # SEISMIC handled via header→dv_seis_set; anything else skips.
                res["ok"] = True
                res["note"] = f"shapefile_skip:{ft}"
            return res

        elif fext in WITSML_EXTS:
            # WITSML self-parses (well header / trajectory / log curves), so the
            # pre-extracted `rows` arg is unused. load_witsml stamps INVENTORY_ID
            # / SOURCE_PATH via capture(), and gates non-WITSML .xml itself.
            try:
                from dataview.file_catalog.witsml_catalog import load_witsml as _witsml
            except ImportError:
                from dataview.file_catalog.witsml_catalog import load_witsml as _witsml
            _w = _witsml(engine, fpath, uwi=uwi,
                         inventory_id=well_info.get("inventory_id"),
                         source_path=fpath, well_info=well_info)
            res["loaded"] = int(_w.get("loaded", 0) or 0)
            res["rt"] = _w.get("rt") or "WITSML"
            res["detail"].update(_w.get("detail") or {})
            res["note"] = _w.get("note", "")
            res["errors"].extend(_w.get("errors") or [])
            if res["loaded"]:
                res["ok"] = True
                _set_readiness_cataloged(engine, well_info.get("inventory_id"))
            return res

        elif fext in JSON_LOG_EXTS:
            # OSDU WKS / JSON-Well-Log self-parses (well header, trajectory,
            # markers, DST, SCAL, log curves), routing by the `kind` field.
            # Non-well kinds (Field/Reservoir/Seismic*) return a no_target note.
            try:
                from dataview.file_catalog.json_well_log_catalog import load_json_well_log as _jwl
            except ImportError:
                from dataview.file_catalog.json_well_log_catalog import load_json_well_log as _jwl
            _j = _jwl(engine, fpath, uwi=uwi,
                      inventory_id=well_info.get("inventory_id"),
                      source_path=fpath, well_info=well_info)
            res["loaded"] = int(_j.get("loaded", 0) or 0)
            res["rt"] = _j.get("rt") or "OSDU"
            res["detail"].update(_j.get("detail") or {})
            res["note"] = _j.get("note", "")
            res["errors"].extend(_j.get("errors") or [])
            if res["loaded"]:
                res["ok"] = True
                _set_readiness_cataloged(engine, well_info.get("inventory_id"))
            return res

        elif fext in OFFICE_EXTS:
            # Office docs parse themselves inside dv_office_loader (it resolves
            # the well + INVENTORY_ID from the file), so the pre-extracted
            # `rows` arg is unused here. dispatch() routes xlsx/docx to the
            # formation-tops / completion / production sub-loader by filename
            # + hint, and each sub-loader stamps INVENTORY_ID/SOURCE_PATH via
            # capture() — same provenance the other branches carry.
            try:
                from dataview.file_catalog.dv_office_loader import dispatch as _office
            except ImportError:
                from dataview.file_catalog.dv_office_loader import dispatch as _office
            _r = _office(engine, fpath, source="OFFICE")
            res["loaded"] = int(_r.get("loaded", 0) or 0)
            res["rt"] = "OFFICE"
            _errs = [str(e) for e in (_r.get("errors") or [])]
            if res["loaded"]:
                res["ok"] = True
                res["detail"]["office"] = res["loaded"]
                _set_readiness_cataloged(engine, well_info.get("inventory_id"))
            elif any("No loader found" in e for e in _errs):
                # recognised office type but no matching sub-loader for this
                # file — report as ⚠ no-loader, not a hard error
                res["note"] = "not_impl:OFFICE"
            else:
                res["errors"].extend(_errs)
            return res

        elif fext in LAS_EXTS:
            # LAS self-parses here (one lasio.read → well header + per-curve
            # metadata), so the pre-extracted `rows` arg is unused. We capture
            # the WELL HEADER into cat_well (so promote can build dv_well) and
            # one row per curve into cat_log_curve — curve *metadata* only
            # (mnemonic / unit / description / API code / depth frame / sample
            # count), never the bulk sample arrays. Those stay the deep stage's
            # job; this is the cheap catalog-level mirror.
            res["rt"] = "WELL_LOG"
            try:
                import lasio, uuid as _uuid
                from datetime import datetime as _dt
                try:
                    from dataview.file_catalog.catalog_capture import capture as _cap
                except ImportError:
                    from dataview.file_catalog.catalog_capture import capture as _cap
                _now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
                inv = well_info.get("inventory_id")
                las = lasio.read(fpath)

                def _wv(*keys):
                    for k in keys:
                        try:
                            v = str(las.well[k].value).strip()
                            if v and v.lower() not in (
                                    "", "unknown", "none", "--"):
                                return v
                        except Exception:
                            pass
                    return None

                def _wnum(*keys):
                    """Numeric LAS well value (lat/long) -> float in range, else None."""
                    return _safe_coord(_wv(*keys))

                # The LAS header carries the authoritative UWI in ~WELL
                # (UWI. 17-031-10035-0000). Read and normalize it, and PREFER it
                # over the caller's filename-derived uwi (which can be an FN_
                # fallback). Mirrors worker_core._do_las so the pool path and this
                # in-page path agree. Only keep the passed-in uwi if the header
                # has none.
                _hdr_uwi = _norm_uwi(_wv("UWI", "API", "APINUM", "APINO",
                                         "API_NO", "WELLID"))
                if _hdr_uwi:
                    uwi = _hdr_uwi      # header UWI wins — real well identity

                # 1) well header → cat_well (promote builds dv_well from this)
                if uwi:
                    try:
                        _hn = _cap(engine, "cat_well", [{
                            "WELL_NAME":        _wv("WELL") or uwi,
                            "OPERATOR_NAME":    _wv("COMP", "PROV"),
                            "FIELD_NAME":       _wv("FLD", "FIELD"),
                            "PROVINCE_STATE":   _wv("STAT", "STATE"),
                            "COUNTY":           _wv("CNTY", "COUNTY"),
                            "COUNTRY":          _wv("CTRY", "CTRY.", "COUNTRY"),
                            "SURFACE_LATITUDE":  _wnum("LATI", "LAT"),
                            "SURFACE_LONGITUDE": _wnum("LONG", "LON"),
                            "FINAL_TD":         _wv("STOP", "TD"),
                            "ACTIVE_IND":       "Y",
                            "ROW_QUALITY":      "FINAL",
                            "PPDM_GUID":        str(_uuid.uuid4()),
                            "ROW_CREATED_BY":   "DataWrangler",
                            "ROW_CREATED_DATE": _now,
                        }], uwi=uwi, inventory_id=inv,
                           source_path=fpath, source="LAS_HEADER")
                        if _hn:
                            res["detail"]["cat_well"] = (
                                res["detail"].get("cat_well", 0) + _hn)
                    except Exception as _he:
                        res["errors"].append(f"header capture: {_he}")

                # depth frame from the LAS index (cheap; no arrays retained)
                try:
                    _idx = las.index
                    d_start = float(_idx[0])  if len(_idx) else None
                    d_stop  = float(_idx[-1]) if len(_idx) else None
                    s_count = int(len(_idx))
                except Exception:
                    d_start = d_stop = None
                    s_count = 0
                d_step = _wv("STEP")
                try:
                    d_uom = (las.curves[0].unit or "").strip() or None
                except Exception:
                    d_uom = None

                # Shared log identity for this LAS — both the log header
                # (cat_well_log) and its curves (cat_well_log_curve) key on it,
                # so curves resolve their FK to dv_well_log.log_id on promote.
                _logid = _wv("LOG_ID", "LOGID") or f"{uwi}-LAS"

                # 2) per-curve metadata → cat_well_log_curve (one row per curve,
                # linked to the log run via log_id). PPDM well_log_curve grain:
                # curves belong to a log run (dv_well_log) which belongs to a well.
                # In a LAS ~Curve line `MNEM.UNIT  API_CODE : DESCRIPTION`, lasio
                # maps API_CODE to curve.value and DESCRIPTION to .descr.
                curve_rows = []
                for i, c in enumerate(las.curves):
                    mnem = (getattr(c, "mnemonic", "") or "").strip()
                    if not mnem:
                        continue
                    curve_rows.append({
                        "UWI":               uwi or None,
                        "LOG_ID":            _logid,
                        "CURVE_ID":          mnem[:40],
                        "MNEMONIC":          mnem,
                        "CURVE_DESCRIPTION": (getattr(c, "descr", "")
                                              or "").strip() or None,
                        "CURVE_UNIT":        (getattr(c, "unit", "")
                                              or "").strip() or None,
                        "TOP_DEPTH":         _safe_num(d_start),
                        "BASE_DEPTH":        _safe_num(d_stop),
                        "DEPTH_OUOM":        d_uom,
                        "NULL_VALUE":        _safe_num(_wv("NULL")),
                        "ACTIVE_IND":        "Y",
                        "ROW_CREATED_BY":    "DataWrangler",
                        "ROW_CREATED_DATE":  _now,
                    })
                if curve_rows:
                    _cn = _cap(engine, "cat_well_log_curve", curve_rows,
                               uwi=uwi, inventory_id=inv,
                               source_path=fpath, source="LAS")
                    res["loaded"] = res.get("loaded", 0) + (_cn or 0)
                    if _cn:
                        res["detail"]["cat_well_log_curve"] = _cn

                # 3) log-file header → cat_well_log (one row per LAS). Reuses the
                # depth frame / uom already computed for the curves and the same
                # _logid the curves carry, so header and curves share one key.
                if uwi:
                    try:
                        _logdt = _wv("DATE", "LOGDATE", "DATE_LOG")
                        # Service company arrives as a raw name (e.g. HALLIBURTON)
                        # from the LAS SRVC mnemonic. dv_well_log.service_company_ba_id
                        # is an FK to dv_business_associate.ba_id, so a raw name would
                        # 547 on promote. Until BA-seeding is wired (SHA1 ba_id, matching
                        # entity_seeder), leave the FK NULL and keep the name in remark
                        # so the provenance is preserved and auditable.
                        _srvc = _wv("SRVC", "SERVICE", "COMP")
                        _wlrow = {
                            "LOG_ID":                _logid,
                            "LOG_TYPE":              _wv("TYPE", "LOGTYPE"),
                            "RUN_NUM":               _wv("RUN", "RUN_NUMBER"),
                            "LOG_DATE":              _logdt,
                            "SERVICE_COMPANY_BA_ID": None,
                            "TOP_DEPTH":             _safe_num(d_start),
                            "BASE_DEPTH":            _safe_num(d_stop),
                            "DEPTH_OUOM":            d_uom,
                            "NULL_VALUE":            _safe_num(_wv("NULL")),
                            "FILE_PATH":             fpath,
                            "FILE_FORMAT":           "LAS",
                            "ACTIVE_IND":            "Y",
                            "REMARK":                (f"service_company={_srvc}"
                                                      if _srvc else None),
                            "ROW_CREATED_BY":        "DataWrangler",
                            "ROW_CREATED_DATE":      _now,
                        }
                        _wln = _cap(engine, "cat_well_log", [_wlrow],
                                    uwi=uwi, inventory_id=inv,
                                    source_path=fpath, source="LAS")
                        if _wln:
                            res["detail"]["cat_well_log"] = (
                                res["detail"].get("cat_well_log", 0) + _wln)
                    except Exception as _wle:
                        res["errors"].append(f"well_log capture: {_wle}")

                if res.get("loaded") or res["detail"]:
                    res["ok"] = True
                    _set_readiness_cataloged(engine, inv)
                else:
                    res["note"] = "las_empty"
            except Exception as e:
                res["errors"].append(str(e))
            return res

        else:
            res["note"] = "unsupported"
            return res

    except Exception as e:
        res["errors"].append(str(e))
        return res


def _do_load(engine, dialect, fpath, fext, uwi, rows):
    """Interactive single-file capture — renders Streamlit messages."""
    res = _load_rows_to_catalog(engine, dialect, fpath, fext, uwi, rows)

    for e in res["errors"]:
        if str(e).startswith("header capture:"):
            st.warning(f"Header capture skipped: "
                       f"{str(e).split(':', 1)[1].strip()}")
    real_errs = [e for e in res["errors"]
                 if not str(e).startswith("header capture:")]

    note = res.get("note", "")
    if note.startswith("petro_fail:"):
        st.error(f"Extraction failed: {note.split(':', 1)[1]}")
        return
    if note.startswith("not_impl:"):
        st.warning(f"Load not implemented for {note.split(':', 1)[1]}")
        return
    if note == "shapefile":
        st.success("✅ Shapefile loaded")
        return
    if note == "unsupported":
        st.info("Direct DB load not yet implemented for this file type.")
        return

    if real_errs:
        st.error(f"Load errors: {'; '.join(str(e) for e in real_errs[:3])}")
    else:
        st.success(f"✅ Loaded {res['loaded']} records")


def _wb_batch(engine, dialect, df):
    """Batch mode: checkbox table of files → capture all checked at once."""
    import pandas as _pd

    st.markdown("##### 📦 Batch load to catalog")
    st.caption("Check the files to capture, then load them all into the "
               "`cat_*` mirrors in one pass. A file needs a matched UWI to "
               "be captured.")

    n = len(df)
    _has_uwi = (df["MATCHED_UWI"].fillna("").astype(str).str.strip() != "")
    n_uwi = int(_has_uwi.sum())

    # Select-ALL checkbox above the grid (matches the spreadsheet metaphor
    # used elsewhere in this page). Only flips rows that have a UWI.
    _sel_all = st.checkbox(
        f"☑ Select ALL with a UWI  ({n_uwi} of {n} files loadable)",
        key="wb_batch_sel_all",
        help="Marks every file that has a matched UWI. Uncheck rows in the "
             "grid to exclude them.",
    )

    st.markdown("---")

    _tbl = _pd.DataFrame({
        "Load":      [bool(_sel_all and u) for u in _has_uwi],
        "Bad":       [False] * n,
        "File":      df["FILE_NAME"].values,
        "UWI":       df["MATCHED_UWI"].fillna("").astype(str).values,
        "Well name": df.get("WELL_NAME",
                            _pd.Series([""] * n)).fillna("").astype(str).values,
        "Ext":       df["FILE_EXT"].astype(str).values,
        "_inv":      df["INVENTORY_ID"].values,
        "_path":     df["FILE_PATH"].values,
        "_size":     df.get("FILE_SIZE_KB",
                            _pd.Series([None] * n)).values,
    })

    _edited = st.data_editor(
        _tbl,
        use_container_width=True,
        hide_index=True,
        disabled=["File", "UWI", "Well name", "Ext"],
        column_config={
            "Load": st.column_config.CheckboxColumn("Load", width="small"),
            "Bad": st.column_config.CheckboxColumn(
                "Bad", width="small",
                help="Fingerprint as junk / badly formatted — removed from the "
                     "catalog now and skipped on the next crawl."),
            "File": st.column_config.TextColumn("File"),
            "UWI": st.column_config.TextColumn("UWI", width="medium"),
            "Well name": st.column_config.TextColumn("Well name"),
            "Ext": st.column_config.TextColumn("Ext", width="small"),
            "_inv": None,
            "_path": None,
            "_size": None,
        },
        key="wb_batch_editor",
    )

    _checked = _edited[_edited["Load"] == True].copy()
    _checked["UWI"] = _checked["UWI"].fillna("").astype(str).str.strip()
    _loadable = _checked[_checked["UWI"] != ""]
    _skip = len(_checked) - len(_loadable)

    msg = f"**{len(_loadable)}** file(s) ready to capture"
    if _skip:
        msg += f" · {_skip} checked without UWI (skipped)"
    st.caption(msg)

    if st.button(
            f"🚀 Load {len(_loadable)} checked to catalog",
            type="primary", key="wb_batch_go",
            disabled=_loadable.empty):
        _run_batch_load(engine, dialect, _loadable)

    # Per-file results report (persists across reruns until cleared / next load).
    _render_batch_report()

    # ── Mark checked files as bad ─────────────────────────────────────────────
    # Fingerprint the rows ticked "Bad" so the next crawl skips them, and drop
    # them from the catalog now (shares _mark_bad with the interactive viewer).
    _bad = _edited[_edited["Bad"] == True].copy()
    if not _bad.empty:
        if "wb_batch_bad_reason" not in st.session_state:
            st.session_state["wb_batch_bad_reason"] = "junk / bad format"
        rcol, bcol = st.columns([3, 1])
        rcol.text_input("Reason", key="wb_batch_bad_reason",
                        label_visibility="collapsed",
                        placeholder="why are these files bad?")
        if bcol.button(f"🚫 Mark {len(_bad)} bad", key="wb_batch_bad_btn",
                       help="Fingerprint these files as bad and remove them "
                            "from the catalog — the next crawl will skip them."):
            reason = st.session_state.get("wb_batch_bad_reason")
            done, errs = 0, []
            for _, br in _bad.iterrows():
                try:
                    _mark_bad(engine, br["_inv"], br["_path"],
                              br.get("File"), br.get("_size"), reason)
                    done += 1
                except Exception as e:
                    errs.append(f"{br.get('File', '')}: {e}")
            if done:
                st.success(f"Marked {done} file(s) bad — the next crawl "
                           "will skip them.")
            if errs:
                st.error("Some files could not be marked:\n- "
                         + "\n- ".join(errs[:10]))
            st.session_state.pop("wb_results", None)
            st.rerun()

    # Second table: resolve a UWI for the files in these results that don't
    # have one yet — guess + match against the reference well master.
    _wb_resolve_unmatched(engine, df)


def _fmt_detail(d):
    """Format a {cat_table: count} map as a compact 'tops×12, dst×3' string."""
    if not d:
        return ""
    return ", ".join(f"{k.replace('cat_', '')}×{v}"
                     for k, v in d.items() if v)


def _run_batch_load(engine, dialect, loadable):
    """Capture each checked file into the cat_* mirrors, then build a per-file
    results report (status, record count, and which mirror tables got rows) and
    stash it in session so it survives reruns."""
    import pandas as _pd

    total = len(loadable)
    prog  = st.progress(0.0, text="Starting…")
    results = []
    ok_invs = set()   # inv ids that captured rows → mark CATALOGED in the grid

    for i, (_, r) in enumerate(loadable.iterrows(), start=1):
        fpath = r["_path"]
        fext  = str(r["Ext"]).lower()
        uwi   = str(r["UWI"]).strip()
        fname = r["File"]
        prog.progress(i / total, text=f"{i}/{total} · {fname}")

        try:
            rows, label = _do_extract(fpath, fext)
            if not rows and fext not in SELF_PARSING_EXTS:
                results.append({"File": fname, "UWI": uwi, "Type": "—",
                                "Status": "— no data", "Records": 0,
                                "Captured into": ""})
                continue
            res = _load_rows_to_catalog(engine, dialect, fpath, fext, uwi,
                                        rows or [])
            real_errs = [e for e in res["errors"]
                         if not str(e).startswith("header capture:")]
            hdr_errs = [e for e in res["errors"]
                        if str(e).startswith("header capture:")]
            note = res.get("note", "")
            if real_errs:
                status = f"❌ {str(real_errs[0])[:60]}"
            elif note.startswith("petro_fail:"):
                status = f"❌ extract: {note.split(':', 1)[1][:50]}"
            elif note.startswith("not_impl:"):
                status = f"⚠ no loader ({note.split(':', 1)[1]})"
            elif note == "unsupported":
                status = "⚠ unsupported type"
            elif note == "shapefile":
                status = "✅ shapefile"
            elif res["loaded"]:
                status = "✅ loaded"
                if hdr_errs:
                    status += " (header skipped)"
            else:
                status = "— nothing captured"
            if status.startswith("✅"):
                ok_invs.add(r["_inv"])
            results.append({"File": fname, "UWI": uwi,
                            "Type": res.get("rt") or label,
                            "Status": status,
                            "Records": res.get("loaded", 0),
                            "Captured into": _fmt_detail(res.get("detail"))})
        except Exception as e:
            results.append({"File": fname, "UWI": uwi, "Type": "—",
                            "Status": f"❌ {str(e)[:60]}", "Records": 0,
                            "Captured into": ""})

    prog.progress(1.0, text="Done")
    rdf = _pd.DataFrame(results, columns=[
        "File", "UWI", "Type", "Status", "Records", "Captured into"])
    n_ok = int(rdf["Status"].str.startswith("✅").sum()) if not rdf.empty else 0

    # Keep the cached search grid in sync: loaded files now read CATALOGED
    # (the DB rows were updated by _set_readiness_cataloged during capture).
    if ok_invs:
        _gdf = st.session_state.get("wb_results")
        if _gdf is not None and "CATALOG_READINESS" in _gdf.columns:
            _gdf.loc[_gdf["INVENTORY_ID"].isin(ok_invs),
                     "CATALOG_READINESS"] = "CATALOGED"

    # Persist so the report survives the reruns triggered by other widgets on
    # this page (resolver, mark-bad). Rendered by _render_batch_report().
    st.session_state["wb_batch_report"] = {
        "rdf": rdf, "n_ok": n_ok, "total": total,
        "tot_rec": int(rdf["Records"].sum()) if not rdf.empty else 0,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def _render_batch_report():
    """Render the persisted batch results report with a CSV download."""
    rep = st.session_state.get("wb_batch_report")
    if not rep:
        return
    rdf = rep["rdf"]
    st.divider()
    rc1, rc2 = st.columns([5, 1])
    rc1.success(f"Batch complete · {rep['n_ok']}/{rep['total']} file(s) "
                f"captured · {rep['tot_rec']:,} record(s)  ·  {rep['ts']}")
    if rc2.button("✖ Clear", key="wb_report_clear"):
        st.session_state.pop("wb_batch_report", None)
        st.rerun()
    st.dataframe(rdf, hide_index=True, use_container_width=True)
    st.download_button(
        "⬇ Download report CSV",
        data=rdf.to_csv(index=False).encode("utf-8"),
        file_name=f"batch_load_report_{rep['ts'].replace(' ', '_').replace(':', '')}.csv",
        mime="text/csv", key="wb_report_dl")
    st.caption("Then run `py promote_catalog.py --apply` to create headers "
               "and promote the captured rows into the `dv_*` tables.")


# Reference well master used to resolve a UWI from a name/UWI guess.
# Default is the production master; the pipeline UI can override it (e.g. point
# at a mini master for a demo) via the "wb_ref_choice" session_state key.
_WB_REF_DEFAULT = "WELL_REF.well_ref.well_master_gold"
_WB_REF_OPTIONS = {
    "WELL_REF.well_ref.well_master_gold":      "Full master (production)",
    "WELL_REF.well_ref.WELL_MASTER_MINI": "Mini master (demo / test)",
}


def _wb_ref() -> str:
    """The reference table enrich/triage resolve against. Overridable from the
    pipeline UI dropdown; falls back to the production master."""
    try:
        v = st.session_state.get("wb_ref_choice")
        if v and str(v).strip():
            return str(v).strip()
    except Exception:
        pass
    return _WB_REF_DEFAULT


def _wb_resolve_unmatched(engine, df):
    """Second table for the batch flow: the files in the current results that
    have NO matched UWI. Shows an editable well-name + UWI guess, matches each
    against the reference well master, and (on Save) writes the confirmed UWI
    back to MATCHED_UWI + the header so the file becomes loadable."""
    from sqlalchemy import text as _t
    try:
        from dataview.core import path_identity as _pi
    except Exception:
        _pi = None

    nomatch = df[df["MATCHED_UWI"].fillna("").astype(str).str.strip() == ""]
    if nomatch.empty:
        return   # nothing to resolve — stay quiet

    invs = [str(i) for i in nomatch["INVENTORY_ID"].tolist()][:_REVIEW_PAGE]

    # Pull header guesses (well name, internal UWI, corroborating attrs).
    g_by_inv = {}
    try:
        with engine.connect() as con:
            _ph = ",".join(f":i{n}" for n in range(len(invs)))
            q = _t(f"""
                SELECT g.INVENTORY_ID AS inv, g.FILE_NAME AS fname,
                       g.FILE_PATH AS path,
                       h.WELL_NAME AS wn, h.UWI AS uwi, h.OPERATOR AS oper,
                       h.TOTAL_DEPTH AS td, h.SPUD_DATE AS spud
                FROM file_catalog.GLOBAL_FILE_CATALOG g
                LEFT JOIN file_catalog.FILE_WELL_HEADER h
                       ON h.INVENTORY_ID = g.INVENTORY_ID
                WHERE g.INVENTORY_ID IN ({_ph})""")
            for r in con.execute(
                    q, {f"i{n}": v for n, v in enumerate(invs)}).fetchall():
                g_by_inv[str(r.inv)] = r
    except Exception as e:
        st.divider()
        st.error(f"Could not read header guesses: {e}")
        return

    st.divider()
    st.markdown("##### 🧭 Unmatched files — guess & match a UWI")
    st.caption(f"{len(nomatch)} file(s) here have no matched UWI"
               + (f" — showing the first {_REVIEW_PAGE}"
                  if len(nomatch) > _REVIEW_PAGE else "")
               + ". Well name and UWI are editable guesses; **Match against "
                 "reference** resolves them, then **Save** writes the UWI back "
                 "so the file can be captured.")

    # A confirmed UWI from a prior Match is promoted into the UWI field before
    # the widgets are built (Streamlit forbids setting widget state afterward).
    _pending = st.session_state.pop("_wb_um_fill", {})

    h1, h2, h3, h4 = st.columns([3, 2, 2, 3])
    h1.markdown("**File**")
    h2.markdown("**Well name (guess)**")
    h3.markdown("**UWI (guess)**")
    h4.markdown("**Match result**")

    inputs = []   # (inv, nkey, ukey, rkey, guess_row)
    for inv in invs:
        g = g_by_inv.get(inv)
        fname = (g.fname if g else None) or f"(inventory {inv})"
        nkey, ukey, rkey = f"wb_um_n_{inv}", f"wb_um_u_{inv}", f"wb_um_r_{inv}"
        if nkey not in st.session_state:
            st.session_state[nkey] = ((g.wn if g else "") or "").strip()
        if ukey not in st.session_state:
            guess = ""
            if g and _pi:
                guess = (_pi.norm_uwi14(g.uwi or "")
                         or (_pi.uwi14_from_path(g.path or "")[0]
                             if g.path else "") or "")
            elif g:
                guess = str(g.uwi or "").strip()
            st.session_state[ukey] = guess
        if inv in _pending:
            st.session_state[ukey] = _pending[inv]
        c1, c2, c3, c4 = st.columns([3, 2, 2, 3])
        c1.write(fname)
        c2.text_input("n", key=nkey, label_visibility="collapsed",
                      placeholder="well name")
        c3.text_input("u", key=ukey, label_visibility="collapsed",
                      placeholder="UWI / API")
        c4.write(st.session_state.get(rkey, "—"))
        inputs.append((inv, nkey, ukey, rkey, g))

    b1, b2 = st.columns(2)
    if b1.button("🔍 Match against reference", key="wb_um_match",
                 use_container_width=True):
        _wb_um_match(engine, inputs, _pi)
    if b2.button("💾 Save matched UWIs", type="primary", key="wb_um_save",
                 use_container_width=True):
        _wb_um_save(engine, inputs, _pi)


def _wb_um_match(engine, inputs, _pi):
    """Look each guess up in the reference well master (by UWI, else by name
    corroborated with TD / spud / operator) and write the result + a fill."""
    from sqlalchemy import text as _t, bindparam

    def _nu(u):
        return _pi.norm_uwi14(u) if _pi else str(u or "").strip()

    def _norm_op(s):
        return "".join(ch for ch in (s or "").upper() if ch.isalnum())

    def _td_close(a, b, tol=50.0):
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            return False
        return a > 0 and b > 0 and abs(a - b) <= max(tol, 0.01 * max(a, b))

    def _same_spud(a, b):
        if not a or not b:
            return False
        try:
            da = a.date() if hasattr(a, "date") else a
            db = b.date() if hasattr(b, "date") else b
            return da == db
        except Exception:
            return False

    cur = [(inv, nkey, ukey, rkey, g,
            st.session_state.get(nkey, "").strip(),
            _nu(st.session_state.get(ukey, "")))
           for inv, nkey, ukey, rkey, g in inputs]
    names = sorted({nm for *_x, nm, _u in cur if nm})
    uwis  = sorted({u for *_x, _nm, u in cur if u})

    cols = ("WELL_NAME, UWI14, UWI, API_NUM, OPERATOR_NAME, "
            "TOTAL_DEPTH, SPUD_DATE, COUNTY, PROVINCE_STATE")
    name_map, uwi_map = {}, {}
    try:
        with engine.connect() as con:
            if names:
                qn = _t(f"SELECT {cols} FROM {_wb_ref()} WHERE WELL_NAME IN :v"
                        ).bindparams(bindparam("v", expanding=True))
                for m in con.execute(qn, {"v": names}).fetchall():
                    name_map.setdefault((m.WELL_NAME or "").strip().upper(),
                                        []).append(m)
            if uwis:
                qu = _t(f"SELECT {cols} FROM {_wb_ref()} WHERE UWI14 IN :v"
                        ).bindparams(bindparam("v", expanding=True))
                for m in con.execute(qu, {"v": uwis}).fetchall():
                    uwi_map[(m.UWI14 or "").strip()] = m
    except Exception as e:
        st.error(f"Reference lookup failed: {type(e).__name__}: {e}")
        return

    fills = {}
    for inv, nkey, ukey, rkey, g, nm, uw in cur:
        txt, fill = "— enter a name or UWI", ""
        if uw and uw in uwi_map:
            m = uwi_map[uw]
            loc = " · ".join(x for x in (m.COUNTY, m.PROVINCE_STATE) if x)
            txt = (f"✓ UWI in reference · {m.WELL_NAME or ''}"
                   + (f" ({loc})" if loc else ""))
            fill = uw
        elif uw:
            txt = "✗ UWI not found in reference"
        elif nm:
            cands = name_map.get(nm.upper(), [])
            scored = []
            for m in cands:
                sigs = []
                if g and _td_close(g.td, m.TOTAL_DEPTH):
                    sigs.append("TD")
                if g and _same_spud(g.spud, m.SPUD_DATE):
                    sigs.append("spud")
                if g and g.oper and _norm_op(g.oper) == _norm_op(m.OPERATOR_NAME):
                    sigs.append("oper")
                scored.append((len(sigs), sigs, m))
            scored.sort(key=lambda x: x[0], reverse=True)
            if not scored:
                txt = "✗ name not found in reference"
            else:
                top_n, top_sigs, top_m = scored[0]
                tie = len(scored) > 1 and scored[1][0] == top_n
                u14 = top_m.UWI14 or top_m.UWI or top_m.API_NUM
                loc = " · ".join(x for x in (top_m.COUNTY, top_m.PROVINCE_STATE) if x)
                if top_n >= 1 and not tie:
                    txt = (f"✓ {u14} · name+{'+'.join(top_sigs)}"
                           + (f" ({loc})" if loc else ""))
                    fill = (_pi.norm_uwi14(u14 or "") if _pi else str(u14 or ""))
                elif top_n >= 1 and tie:
                    txt = f"⚠ {len(scored)} wells match name+attrs — verify"
                elif len(cands) == 1:
                    txt = (f"? {u14} · name only — verify"
                           + (f" ({loc})" if loc else ""))
                else:
                    txt = f"⚠ {len(cands)} share this name — verify"
        st.session_state[rkey] = txt
        if fill:
            fills[inv] = fill
    st.session_state["_wb_um_fill"] = fills
    st.rerun()


def _wb_um_save(engine, inputs, _pi):
    """Write the confirmed UWI to MATCHED_UWI (so the file is loadable) and to
    the header's UWI / UWI14 (so re-enrichment keeps it)."""
    from sqlalchemy import text as _t

    ups = []
    for inv, nkey, ukey, rkey, g in inputs:
        u = (_pi.norm_uwi14(st.session_state.get(ukey, "")) if _pi
             else str(st.session_state.get(ukey, "") or "").strip())
        if u:
            ups.append({"inv": inv, "u": u})
    if not ups:
        st.warning("No valid UWIs to write.")
        return
    try:
        with engine.begin() as con:
            for up in ups:
                con.execute(_t(
                    "UPDATE file_catalog.GLOBAL_FILE_CATALOG "
                    "SET MATCHED_UWI=:u, ROW_CHANGED_DATE=GETUTCDATE() "
                    "WHERE INVENTORY_ID=:inv"), up)
                con.execute(_t(
                    "UPDATE file_catalog.FILE_WELL_HEADER "
                    "SET UWI=:u, UWI14=:u WHERE INVENTORY_ID=:inv"), up)
    except Exception as e:
        st.error(f"Save failed: {e}")
        return

    # Clear the per-row widget state and the cached results so the batch table
    # rebuilds with the newly matched UWIs.
    for inv, nkey, ukey, rkey, g in inputs:
        for k in (nkey, ukey, rkey):
            st.session_state.pop(k, None)
    st.session_state.pop("wb_results", None)
    st.success(f"Wrote {len(ups)} UWI(s). Re-run Search to load them.")
    st.rerun()


def _toggle_flag(engine, inv_id: str, flag: bool):
    from sqlalchemy import text as _t
    try:
        with engine.begin() as con:
            con.execute(_t("""
                UPDATE file_catalog.GLOBAL_FILE_CATALOG
                SET FLAG_DELETE=:f, ROW_CHANGED_DATE=GETUTCDATE()
                WHERE INVENTORY_ID=:id
            """), {"f": "Y" if flag else "N", "id": inv_id})
    except Exception as e:
        st.error(f"Flag failed: {e}")


def _mark_bad(engine, inv_id, fpath, fname=None, size_kb=None, reason=None):
    """Fingerprint a file as bad (junk / badly formatted) so the next crawl
    skips it, and drop it from the live catalog now.

    The blocklist is keyed on INVENTORY_ID — the same SHA-1(uppercase path)
    fingerprint the crawler computes — so the skip is a cheap set lookup with
    no per-file hashing during the walk. A best-effort content signature
    (first 4 MB) is stored alongside for reference, but is never used to gate
    the crawl (so it can't stall on huge files).
    """
    from sqlalchemy import text as _t
    import hashlib

    fhash = ""
    try:
        with open(fpath, "rb") as _f:
            fhash = hashlib.sha1(_f.read(4 * 1024 * 1024)).hexdigest().upper()
    except Exception:
        fhash = ""

    try:
        sz = float(size_kb) if size_kb not in (None, "") else None
    except (TypeError, ValueError):
        sz = None

    with engine.begin() as con:
        con.execute(_t("""
            IF OBJECT_ID('file_catalog.BAD_FILE','U') IS NULL
            CREATE TABLE file_catalog.BAD_FILE (
                INVENTORY_ID  NVARCHAR(40) NOT NULL PRIMARY KEY,
                FILE_PATH     NVARCHAR(900),
                FILE_NAME     NVARCHAR(260),
                FILE_SIZE_KB  DECIMAL(15,2),
                FILE_HASH     NVARCHAR(40),
                REASON        NVARCHAR(500),
                MARKED_BY     NVARCHAR(100),
                MARKED_DATE   DATETIME2
                    CONSTRAINT DF_BAD_FILE_DT DEFAULT SYSUTCDATETIME()
            );
        """))
        con.execute(_t("""
            MERGE file_catalog.BAD_FILE AS tgt
            USING (SELECT :id AS INVENTORY_ID) AS src
              ON tgt.INVENTORY_ID = src.INVENTORY_ID
            WHEN MATCHED THEN UPDATE SET
                FILE_PATH=:p, FILE_NAME=:n, FILE_SIZE_KB=:sz,
                FILE_HASH=:h, REASON=:r, MARKED_DATE=SYSUTCDATETIME()
            WHEN NOT MATCHED THEN INSERT
                (INVENTORY_ID, FILE_PATH, FILE_NAME, FILE_SIZE_KB,
                 FILE_HASH, REASON, MARKED_BY, MARKED_DATE)
                VALUES (:id, :p, :n, :sz, :h, :r, 'DataWrangler',
                        SYSUTCDATETIME());
        """), {"id": inv_id, "p": (fpath or "")[:900],
               "n": (fname or "")[:260], "sz": sz, "h": fhash,
               "r": (reason or "junk / bad format")[:500]})
        con.execute(_t("DELETE FROM file_catalog.GLOBAL_FILE_CATALOG "
                       "WHERE INVENTORY_ID=:id"), {"id": inv_id})



# =============================================================================
# Tab 3 -- Well Map
# =============================================================================

def _tab_map(engine, dialect):
    from sqlalchemy import text as _t

    st.markdown("#### 🗺️ Well Map")
    st.caption(
        "Wells from FILE_WELL_HEADER with lat/lon. "
        "Click a marker for details and link to source file. "
        "Seismic survey footprints from FILE_SEIS_HEADER."
    )

    # ── Controls ──────────────────────────────────────────────────────────────
    c1, c2, c3 = st.columns(3)
    show_seis  = c1.checkbox("Show seismic footprints", value=True,
                              key="wm_seis")
    show_all   = c2.checkbox("Include suspect coords", value=False,
                              key="wm_all",
                              help="Include wells that may have wrong coordinates")
    tile_style = c3.selectbox("Base map",
        ["CartoDB positron","OpenStreetMap","CartoDB dark_matter"],
        key="wm_tiles")

    # ── Query wells ───────────────────────────────────────────────────────────
    try:
        with engine.connect() as con:
            well_rows = con.execute(_t("""
                SELECT
                    wh.UWI, wh.WELL_NAME, wh.OPERATOR,
                    wh.WELL_FIELD, wh.STATE, wh.COUNTY,
                    CAST(wh.LATITUDE  AS FLOAT) AS LAT,
                    CAST(wh.LONGITUDE AS FLOAT) AS LON,
                    wh.TOTAL_DEPTH, wh.SPUD_DATE,
                    wh.REPORT_TYPE, wh.CONTRACTOR,
                    wh.CONFIDENCE,
                    gfc.FILE_PATH, gfc.FILE_NAME, gfc.FILE_EXT,
                    gfc.CATALOG_READINESS
                FROM file_catalog.FILE_WELL_HEADER wh
                JOIN file_catalog.GLOBAL_FILE_CATALOG gfc
                    ON gfc.INVENTORY_ID = wh.INVENTORY_ID
                WHERE wh.LATITUDE  IS NOT NULL
                  AND wh.LONGITUDE IS NOT NULL
                  AND TRY_CAST(wh.LATITUDE  AS FLOAT) BETWEEN -90  AND 90
                  AND TRY_CAST(wh.LONGITUDE AS FLOAT) BETWEEN -180 AND 180
            """)).fetchall()

            seis_rows = []
            if show_seis:
                try:
                    seis_rows = con.execute(_t("""
                        SELECT
                            sh.SURVEY_NAME, sh.SEIS_SET_TYPE,
                            sh.CONTRACTOR, sh.TRACE_COUNT,
                            CAST(sh.BBOX_MIN_LAT AS FLOAT) AS MIN_LAT,
                            CAST(sh.BBOX_MAX_LAT AS FLOAT) AS MAX_LAT,
                            CAST(sh.BBOX_MIN_LON AS FLOAT) AS MIN_LON,
                            CAST(sh.BBOX_MAX_LON AS FLOAT) AS MAX_LON,
                            gfc.FILE_NAME, gfc.FILE_EXT
                        FROM file_catalog.FILE_SEIS_HEADER sh
                        JOIN file_catalog.GLOBAL_FILE_CATALOG gfc
                            ON gfc.INVENTORY_ID = sh.INVENTORY_ID
                        WHERE sh.BBOX_MIN_LAT IS NOT NULL
                          AND sh.BBOX_MAX_LAT IS NOT NULL
                          AND sh.BBOX_MIN_LON IS NOT NULL
                          AND sh.BBOX_MAX_LON IS NOT NULL
                          AND TRY_CAST(sh.BBOX_MIN_LAT AS FLOAT) BETWEEN -90 AND 90
                          AND TRY_CAST(sh.BBOX_MIN_LON AS FLOAT) BETWEEN -180 AND 0
                    """)).fetchall()
                except Exception:
                    pass

    except Exception as e:
        st.error(f"Query failed: {e}")
        return

    if not well_rows:
        st.warning(
            "No wells with coordinates found. "
            "Run Phase 2 extraction first."
        )
        return

    import pandas as pd
    wells = pd.DataFrame(well_rows, columns=[
        "uwi","well_name","operator","field","state","county",
        "lat","lon","total_depth","spud_date","report_type",
        "contractor","confidence","file_path","file_name",
        "file_ext","readiness",
    ])

    # Optionally filter suspect coordinates
    if not show_all:
        # US bounding box roughly
        wells = wells[
            wells["lat"].between(24, 50) &
            wells["lon"].between(-125, -65)
        ]

    m1, m2, m3 = st.columns(3)
    m1.metric("Wells on map",    len(wells))
    m2.metric("States",          wells["state"].nunique())
    m3.metric("Seismic surveys", len(seis_rows))

    if wells.empty:
        st.warning("No wells in valid US coordinates. "
                   "Check 'Include suspect coords' to show all.")
        return

    # ── Build folium map ──────────────────────────────────────────────────────
    import folium
    center_lat = wells["lat"].mean()
    center_lon = wells["lon"].mean()

    tile_map = {
        "CartoDB positron":    "CartoDB positron",
        "OpenStreetMap":       "OpenStreetMap",
        "CartoDB dark_matter": "CartoDB dark_matter",
    }

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=5,
        tiles=tile_map.get(tile_style, "CartoDB positron"),
    )

    # Color by report type
    type_colors = {
        "WELL_LOG":           "#378ADD",
        "DIRECTIONAL_SURVEY": "#C8922A",
        "OFFICE":             "#888780",
        "UNKNOWN":            "#B4B2A9",
    }

    # ── Well markers ──────────────────────────────────────────────────────────
    for _, w in wells.iterrows():
        color = type_colors.get(w["report_type"], "#378ADD")

        popup_html = f"""
        <div style="font-family:sans-serif;font-size:13px;min-width:200px">
            <b style="font-size:14px">{w['well_name'] or w['uwi']}</b><br>
            <hr style="margin:4px 0">
            <b>UWI:</b> {w['uwi'] or '—'}<br>
            <b>Field:</b> {w['field'] or '—'}<br>
            <b>Operator:</b> {w['operator'] or '—'}<br>
            <b>State:</b> {w['state'] or '—'} · {w['county'] or '—'}<br>
            <b>TD:</b> {w['total_depth'] or '—'} ft<br>
            <b>Type:</b> {w['report_type'] or '—'}<br>
            <b>Readiness:</b> {w['readiness'] or '—'}<br>
            <hr style="margin:4px 0">
            <b>Source:</b> {w['file_name']}<br>
            <small style="color:#666;word-break:break-all">{w['file_path']}</small>
        </div>
        """

        tooltip = f"{w['well_name'] or w['uwi']} · {w['field'] or ''}"

        folium.CircleMarker(
            location=[w["lat"], w["lon"]],
            radius=8,
            color="white",
            weight=1.5,
            fill=True,
            fill_color=color,
            fill_opacity=0.85,
            tooltip=folium.Tooltip(tooltip),
            popup=folium.Popup(popup_html, max_width=280),
        ).add_to(m)

    # ── Seismic footprints ────────────────────────────────────────────────────
    if seis_rows:
        for sr in seis_rows:
            try:
                (sname, stype, contr, traces,
                 min_lat, max_lat, min_lon, max_lon,
                 fname, fext) = sr

                if None in (min_lat, max_lat, min_lon, max_lon):
                    continue

                popup_html = f"""
                <div style="font-family:sans-serif;font-size:13px">
                    <b>{sname or fname}</b><br>
                    <b>Type:</b> {stype or '—'}<br>
                    <b>Contractor:</b> {contr or '—'}<br>
                    <b>Traces:</b> {traces or '—'}<br>
                    <b>File:</b> {fname}
                </div>
                """

                folium.Rectangle(
                    bounds=[[min_lat, min_lon],
                            [max_lat, max_lon]],
                    color="#1D9E75",
                    weight=2,
                    fill=True,
                    fill_color="#1D9E75",
                    fill_opacity=0.1,
                    tooltip=folium.Tooltip(sname or fname),
                    popup=folium.Popup(popup_html, max_width=240),
                ).add_to(m)
            except Exception:
                pass

    # ── Legend ────────────────────────────────────────────────────────────────
    legend_html = """
    <div style="position:fixed;bottom:30px;left:30px;z-index:1000;
                background:white;padding:10px 14px;border-radius:8px;
                border:1px solid #ccc;font-size:12px;font-family:sans-serif">
        <b>Well type</b><br>
        <span style="color:#378ADD">&#9679;</span> Well log (LAS/DLIS)<br>
        <span style="color:#C8922A">&#9679;</span> Directional survey<br>
        <span style="color:#888780">&#9679;</span> Office / other<br>
        <span style="color:#1D9E75">&#9632;</span> Seismic footprint
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    # Fit bounds to data
    m.fit_bounds([
        [wells["lat"].min() - 0.5, wells["lon"].min() - 0.5],
        [wells["lat"].max() + 0.5, wells["lon"].max() + 0.5],
    ])

    # Render map HTML directly — avoids st_folium height issues in tabs
    map_html = m._repr_html_()
    st.components.v1.html(
        f'''<div style="width:100%;height:600px;">{map_html}</div>''',
        height=620,
        scrolling=False,
    )

    # ── Export options ────────────────────────────────────────────────────────
    st.divider()
    st.markdown("**Export**")
    ec1, ec2 = st.columns(2)

    # CSV
    ec1.download_button(
        "⬇ Download well locations CSV",
        data=wells.to_csv(index=False),
        file_name="well_locations.csv",
        mime="text/csv",
        key="wm_csv",
    )

    # GeoJSON
    try:
        import json
        features = []
        for _, w in wells.iterrows():
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [w["lon"], w["lat"]],
                },
                "properties": {
                    "uwi":         w["uwi"],
                    "well_name":   w["well_name"],
                    "operator":    w["operator"],
                    "field":       w["field"],
                    "state":       w["state"],
                    "county":      w["county"],
                    "total_depth": w["total_depth"],
                    "report_type": w["report_type"],
                    "file_path":   w["file_path"],
                    "file_name":   w["file_name"],
                    "readiness":   w["readiness"],
                },
            })
        gj = json.dumps({
            "type": "FeatureCollection",
            "features": features,
        }, indent=2)
        ec2.download_button(
            "⬇ Download GeoJSON",
            data=gj,
            file_name="well_locations.geojson",
            mime="application/geo+json",
            key="wm_geojson",
        )
    except Exception:
        pass


# =============================================================================
# Tab 4 -- Header Files
# =============================================================================

def _tab_headers(engine, dialect):
    from sqlalchemy import text as _t

    st.markdown("#### 📋 Header Files")
    st.caption(
        "Query extracted headers from FILE_WELL_HEADER and "
        "FILE_SEIS_HEADER. Export flat CSV for well creation "
        "or load seismic to dv_seis_set."
    )

    sub = st.tabs(["📋 Well Headers", "📡 Seis Headers"])

    # ── Well Headers ──────────────────────────────────────────────────────────
    with sub[0]:
        st.markdown("**Well header flat file**")
        f1, f2 = st.columns(2)
        has_uwi   = f1.checkbox("Has UWI only",     key="wh2_has_uwi")
        has_coord = f2.checkbox("Has Lat/Lon only",  key="wh2_has_coord")

        if st.button("🔍 Query", type="primary", key="wh2_query"):
            try:
                conds = ["1=1"]
                params = {}
                if has_uwi:
                    conds.append("wh.UWI IS NOT NULL AND wh.UWI!=''")
                if has_coord:
                    conds.append(
                        "wh.LATITUDE IS NOT NULL "
                        "AND wh.LONGITUDE IS NOT NULL")
                with engine.connect() as con:
                    rows = con.execute(_t(f"""
                        SELECT
                            gfc.FILE_PATH, gfc.FILE_NAME, gfc.FILE_EXT,
                            gfc.FILE_TYPE_GROUP,
                            gfc.CATALOG_READINESS, gfc.CATALOG_SCORE,
                            wh.UWI, wh.WELL_NAME, wh.OPERATOR,
                            wh.WELL_FIELD, wh.STATE, wh.COUNTY,
                            wh.LATITUDE, wh.LONGITUDE,
                            wh.TOTAL_DEPTH, wh.SPUD_DATE, wh.RIG_RELEASE,
                            wh.REPORT_TYPE, wh.SURVEY_TYPE,
                            wh.CONTRACTOR, wh.CONFIDENCE
                        FROM file_catalog.FILE_WELL_HEADER wh
                        JOIN file_catalog.GLOBAL_FILE_CATALOG gfc
                            ON gfc.INVENTORY_ID = wh.INVENTORY_ID
                        WHERE {" AND ".join(conds)}
                        ORDER BY gfc.CATALOG_SCORE DESC, gfc.FILE_NAME
                    """), params).fetchall()

                df = pd.DataFrame(rows, columns=[
                    "file_path","file_name","extension","type_group",
                    "readiness","score",
                    "uwi","well_name","operator","well_field",
                    "state","county","latitude","longitude",
                    "total_depth","spud_date","rig_release",
                    "report_type","survey_type","contractor","confidence",
                ])
                st.session_state["wh2_df"] = df
            except Exception as e:
                st.error(f"Query failed: {e}")
                st.caption(
                    "Run Phase 2 extraction to populate FILE_WELL_HEADER.")

        df = st.session_state.get("wh2_df")
        if df is None:
            st.info("Click Query. Run Phase 2 extraction if empty.")
        else:
            m1,m2,m3 = st.columns(3)
            m1.metric("Files",       len(df))
            m2.metric("Has UWI",
                      int(df["uwi"].notna().sum()))
            m3.metric("Has Lat/Lon",
                      int((df["latitude"].notna() &
                           df["longitude"].notna()).sum()))
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.download_button(
                "⬇ Export Well Header Flat File",
                data=df.to_csv(index=False),
                file_name="well_header_flat_file.csv",
                mime="text/csv", key="wh2_export",
            )

    # ── Seis Headers ──────────────────────────────────────────────────────────
    with sub[1]:
        st.markdown("**Seismic header flat file**")
        has_survey = st.checkbox("Has survey name only", key="sh2_has_survey")

        if st.button("🔍 Query", type="primary", key="sh2_query"):
            try:
                conds = ["1=1"]
                if has_survey:
                    conds.append(
                        "sh.SURVEY_NAME IS NOT NULL "
                        "AND sh.SURVEY_NAME!=''")
                with engine.connect() as con:
                    rows = con.execute(_t(f"""
                        SELECT
                            gfc.FILE_PATH, gfc.FILE_NAME, gfc.FILE_EXT,
                            gfc.FILE_TYPE_GROUP, gfc.CATALOG_READINESS,
                            sh.SURVEY_NAME, sh.LINE_NAME,
                            sh.SEIS_SET_TYPE, sh.SURVEY_DATE,
                            sh.CONTRACTOR,
                            sh.BBOX_MIN_LAT, sh.BBOX_MAX_LAT,
                            sh.BBOX_MIN_LON, sh.BBOX_MAX_LON,
                            sh.EPSG_CODE, sh.SAMPLE_INTERVAL,
                            sh.TRACE_COUNT, sh.SHOT_FIRST, sh.SHOT_LAST,
                            sh.SURVEY_OUTLINE,
                            sh.INVENTORY_ID
                        FROM file_catalog.FILE_SEIS_HEADER sh
                        JOIN file_catalog.GLOBAL_FILE_CATALOG gfc
                            ON gfc.INVENTORY_ID = sh.INVENTORY_ID
                        WHERE {" AND ".join(conds)}
                        ORDER BY gfc.FILE_NAME
                    """)).fetchall()

                df = pd.DataFrame(rows, columns=[
                    "file_path","file_name","extension","type_group",
                    "readiness","survey_name","line_name",
                    "seis_set_type","survey_date","contractor",
                    "bbox_min_lat","bbox_max_lat",
                    "bbox_min_lon","bbox_max_lon",
                    "epsg_code","sample_interval",
                    "trace_count","shot_first","shot_last",
                    "survey_outline",
                    "inventory_id",
                ])
                st.session_state["sh2_df"] = df
            except Exception as e:
                st.error(f"Query failed: {e}")
                st.caption(
                    "Run Phase 2 extraction to populate FILE_SEIS_HEADER.")

        df = st.session_state.get("sh2_df")
        if df is None:
            st.info("Click Query. Run Phase 2 extraction if empty.")
        else:
            m1,m2,m3 = st.columns(3)
            m1.metric("Seismic files", len(df))
            m2.metric("Has survey",
                      int(df["survey_name"].notna().sum()))
            m3.metric("Has bbox",
                      int(df["bbox_min_lat"].notna().sum()))

            st.dataframe(
                df.drop(columns=["inventory_id"]),
                hide_index=True, use_container_width=True,
            )

            c1, c2 = st.columns(2)
            c1.download_button(
                "⬇ Export Seis Header CSV",
                data=df.to_csv(index=False),
                file_name="seis_header_flat_file.csv",
                mime="text/csv", key="sh2_export",
            )
            if c2.button("🚀 Load to dv_seis_set",
                         key="sh2_load"):
                _load_seis(engine, dialect, df)


def _load_seis(engine, dialect, df):
    """Insert/update seis headers into dataview.dv_seis_set."""
    from sqlalchemy import text as _t

    to_load = df[
        df["survey_name"].notna() &
        (df["survey_name"].astype(str).str.strip() != "")
    ]
    if to_load.empty:
        st.warning("No rows have a survey name.")
        return

    loaded = errors = 0
    for _, row in to_load.iterrows():
        try:
            sid = uuid.uuid4().hex[:40].upper()
            with engine.begin() as con:
                ex = con.execute(_t("""
                    SELECT seis_set_id FROM dataview.dv_seis_set
                    WHERE seis_set_name=:n
                """), {"n": row["survey_name"]}).fetchone()

                if ex:
                    con.execute(_t("""
                        UPDATE dataview.dv_seis_set SET
                            file_path=:fp, catalog_id=:cid,
                            bbox_min_lat=:bmin_lat, bbox_max_lat=:bmax_lat,
                            bbox_min_lon=:bmin_lon, bbox_max_lon=:bmax_lon,
                            epsg_code=:epsg, remark=:remark,
                            geog = CASE WHEN :wkt IS NULL THEN geog ELSE (
                                CASE WHEN geography::STGeomFromText(:wkt,4326)
                                          .STArea()/1000000.0 > 255000000
                                     THEN geography::STGeomFromText(:wkt,4326)
                                          .ReorientObject()
                                     ELSE geography::STGeomFromText(:wkt,4326)
                                END) END,
                            row_changed_by='DataWrangler',
                            row_changed_date=GETUTCDATE()
                        WHERE seis_set_name=:n
                    """), {
                        "fp":       row["file_path"],
                        "cid":      row.get("inventory_id"),
                        "bmin_lat": _safe_num(row.get("bbox_min_lat")),
                        "bmax_lat": _safe_num(row.get("bbox_max_lat")),
                        "bmin_lon": _safe_num(row.get("bbox_min_lon")),
                        "bmax_lon": _safe_num(row.get("bbox_max_lon")),
                        "epsg":     _safe_int(row.get("epsg_code")),
                        "remark":   str(row.get("contractor",""))[:2000],
                        "wkt":      (row.get("survey_outline") or None),
                        "n":        row["survey_name"],
                    })
                else:
                    con.execute(_t("""
                        INSERT INTO dataview.dv_seis_set (
                            seis_set_id, seis_set_name, seis_set_type,
                            file_path, catalog_id,
                            bbox_min_lat, bbox_max_lat,
                            bbox_min_lon, bbox_max_lon,
                            epsg_code, remark, geog, active_ind, source,
                            row_created_by, row_created_date,
                            row_changed_by, row_changed_date
                        ) VALUES (
                            :sid,:sn,:stype,:fp,:cid,
                            :bmin_lat,:bmax_lat,:bmin_lon,:bmax_lon,
                            :epsg,:remark,
                            CASE WHEN :wkt IS NULL THEN NULL ELSE (
                                CASE WHEN geography::STGeomFromText(:wkt,4326)
                                          .STArea()/1000000.0 > 255000000
                                     THEN geography::STGeomFromText(:wkt,4326)
                                          .ReorientObject()
                                     ELSE geography::STGeomFromText(:wkt,4326)
                                END) END,
                            'Y','FILE_CATALOG',
                            'DataWrangler',GETUTCDATE(),
                            'DataWrangler',GETUTCDATE()
                        )
                    """), {
                        "sid":      sid,
                        "sn":       row["survey_name"],
                        "stype":    str(row.get("seis_set_type","2D"))[:40],
                        "fp":       row["file_path"],
                        "cid":      row.get("inventory_id"),
                        "bmin_lat": _safe_num(row.get("bbox_min_lat")),
                        "bmax_lat": _safe_num(row.get("bbox_max_lat")),
                        "bmin_lon": _safe_num(row.get("bbox_min_lon")),
                        "bmax_lon": _safe_num(row.get("bbox_max_lon")),
                        "epsg":     _safe_int(row.get("epsg_code")),
                        "remark":   str(row.get("contractor",""))[:2000],
                        "wkt":      (row.get("survey_outline") or None),
                    })
            loaded += 1
        except Exception:
            errors += 1

    if errors:
        st.error(f"Loaded {loaded}, {errors} errors.")
    else:
        st.success(f"✅ Loaded {loaded} rows to dv_seis_set.")


# =============================================================================
# Tab 5 -- Pipeline  (post-catalog operations: enrich, vault, collect,
#          key wells/surveys, deep catalog). Each button runs the same code
#          as its standalone CLI script; scripts live in ROOT next to this.
# =============================================================================

def _run_op(engine, fn, a, spinner_label):
    """Run a pipeline core function against the app DB, capturing its log.
    Passes a raw pyodbc connection (what enrich/vault/collect expect).
    Returns (lines, result_or_None, error_or_None)."""
    lines, res, err = [], None, None
    raw = engine.raw_connection()
    try:
        with st.spinner(spinner_label):
            res = fn(raw, a, log=lines.append)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    finally:
        try:
            raw.close()
        except Exception:
            pass
    return lines, res, err


def _run_op_engine(engine, fn, a, spinner_label):
    """Like _run_op, but passes the SQLAlchemy *engine* (not a raw connection)
    to the core function. Used by deep_catalog, whose cataloger modules need
    the engine to open their own pooled connections."""
    lines, res, err = [], None, None
    try:
        with st.spinner(spinner_label):
            res = fn(engine, a, log=lines.append)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return lines, res, err


def _pipeline_formats():
    """Supported file-format reference, shown behind an expander on the page."""
    with st.expander("📋 Supported formats — what the scanner ingests",
                     expanded=False):
        rows = [
            ("PDF", ", ".join(sorted(PDF_EXTS)),
             "Scout tickets, directional surveys, mud logs, completion reports"),
            ("Well Log", ", ".join(sorted(LOG_EXTS)),
             "Curves / channels \u2192 dv_log_curve (LAS, DLIS, LIS)"),
            ("Seismic", ", ".join(sorted(SEGY_EXTS | P190_EXTS)),
             "Survey identity, bounding box, geometry \u2192 dv_seis_set"),
            ("Shapefile", ", ".join(sorted(SHP_EXTS)),
             "Well / lease / survey geometries"),
            ("Office", ", ".join(sorted(OFFICE_EXTS)),
             "Tabular and document data (Excel, Word, CSV / TSV)"),
            ("WITSML", ", ".join(sorted(WITSML_EXTS)) + "  (1.3.1 / 1.4.1)",
             "Wells, trajectories, log curves"),
            ("OSDU / JSON Well Log", ", ".join(sorted(JSON_LOG_EXTS)),
             "Well, WellLog, Trajectory, Markers, Pressure, SCAL, Field, "
             "Reservoir, Seismic"),
            ("Image", ", ".join(sorted(IMAGE_EXTS)),
             "Catalogued only \u2014 no content extractor"),
        ]
        df = pd.DataFrame(
            rows, columns=["Format group", "Extensions", "What we capture"])
        st.dataframe(df, hide_index=True, use_container_width=True)


def _tab_pipeline(engine, dialect):
    """File Catalog / Pipeline page — hero run, advanced stages, clear tools."""
    # Run Full Pipeline is the first thing on the page; explanatory prose stays
    # tucked behind expanders.
    _pipeline_run_hero(engine, dialect)

    with st.expander("ℹ️ About this pipeline", expanded=False):
        st.caption(
            "The system scans structured and unstructured documents on a shared "
            "drive, extracts the contents, associates the contents to the correct "
            "well and/or seismic survey, and loads to the database as a validated "
            "and trustworthy source of data. Optionally files the qualifying "
            "documents in a digital vault.")

    _pipeline_formats()

    _pipeline_report(engine)

    st.divider()
    _pipeline_clear(engine, dialect)


def _pipeline_report(engine):
    """Per-file audit of what the pipeline did to each catalogued file —
    path · file_name · uwi · well/survey name · action taken. A testing aid:
    scope it to the scan path and see at a glance how each file landed."""
    import pandas as pd
    from sqlalchemy import text as _t
    with st.expander("📋 Stage scorecard — extract · capture · promote per file",
                     expanded=False):
        # Scope: default to THIS crawl (files scanned today), with an option to
        # widen to a scan root or the whole catalog.
        try:
            with engine.connect() as _c:
                _roots = [r[0] for r in _c.execute(_t(
                    "SELECT DISTINCT ROOT_PATH "
                    "FROM file_catalog.GLOBAL_FILE_CATALOG "
                    "WHERE NULLIF(LTRIM(RTRIM(ROOT_PATH)),'') IS NOT NULL "
                    "ORDER BY ROOT_PATH")).fetchall()]
        except Exception:
            _roots = []
        sc1, sc2 = st.columns([3, 1])
        _ALL = "(whole catalog)"
        scsel = sc1.selectbox(
            "Scan root", [_ALL] + _roots, index=0, key="score_root_sel",
            help="Limit to a scan root, or the whole catalog.")
        this_crawl = sc2.checkbox(
            "This crawl only", value=True, key="score_this_crawl",
            help="Limit to files scanned today (the current crawl).")
        if not st.button("Run scorecard", key="score_run",
                         use_container_width=True):
            return

        # detail tables: (cat_ staged, dv_ promoted, label). captured = rows in
        # cat_ OR dv_ ; promoted = rows in dv_. dv_ tables carry INVENTORY_ID.
        _DETAIL = [
            ("cat_well", "dv_well", "header"),
            ("cat_well_dir_srvy_sta", "dv_well_dir_srvy_sta", "survey"),
            ("cat_well_formation_top", "dv_well_formation_top", "tops"),
            ("cat_well_dst", "dv_well_dst", "welltest"),
            ("cat_well_completion", "dv_well_completion", "completion"),
            ("cat_prod_volume", "dv_prod_volume", "production"),
            ("cat_well_petro_interp", "dv_well_petro_interp", "petro"),
            ("cat_well_core", "dv_well_core", "core"),
            ("cat_well_log", "dv_well_log", "log"),
            ("cat_well_log_curve", "dv_well_log_curve", "curves"),
        ]
        try:
            with engine.connect() as con:
                where, params = ["1=1"], {}
                if scsel and scsel != _ALL:
                    where.append("g.ROOT_PATH = :root"); params["root"] = scsel
                if this_crawl:
                    where.append(
                        "CAST(g.SCAN_DATE AS date) = CAST(GETDATE() AS date)")
                base = con.execute(_t(f"""
                    SELECT g.FILE_NAME, g.INVENTORY_ID,
                           NULLIF(LTRIM(RTRIM(g.MATCHED_UWI)),'') AS uwi,
                           g.HEADER_EXTRACTED, wh.REPORT_TYPE
                    FROM file_catalog.GLOBAL_FILE_CATALOG g
                    LEFT JOIN file_catalog.FILE_WELL_HEADER wh
                           ON wh.INVENTORY_ID = g.INVENTORY_ID
                    WHERE {' AND '.join(where)}
                    ORDER BY g.FILE_NAME
                """), params).fetchall()

                def _cnt(tbl, inv):
                    try:
                        return con.execute(_t(
                            f"SELECT COUNT(*) FROM {tbl} WHERE INVENTORY_ID=:i"),
                            {"i": inv}).scalar() or 0
                    except Exception:
                        return 0

                rows = []
                for fn, inv, uwi, hx, rtype in base:
                    extracted = ("Y" if hx == "Y" else
                                 "ERR" if hx == "E" else
                                 "skip" if hx == "S" else "N")
                    cap = prom = 0; detail = []
                    for cat, dv, label in _DETAIL:
                        n_dv = _cnt("dataview." + dv, inv)
                        n_ct = _cnt("file_catalog." + cat, inv)
                        if n_dv:
                            prom += n_dv; cap += n_dv; detail.append(f"{label}:{n_dv}")
                        elif n_ct:
                            cap += n_ct; detail.append(f"{label}:{n_ct}(staged)")
                    rows.append({
                        "file": fn,
                        "type": rtype or "?",
                        "extract": extracted,
                        "capture": "Y" if cap else "N",
                        "promote": "Y" if prom else "N",
                        "uwi": uwi or "",
                        "detail": " ".join(detail) if detail else "no detail rows",
                    })
                df = pd.DataFrame(rows)
        except Exception as e:
            st.error(f"Scorecard failed: {type(e).__name__}: {e}")
            return

        if df.empty:
            st.info("No files in scope. Run a crawl, or widen the scope.")
            return

        n_ext = int((df["extract"] == "Y").sum())
        n_cap = int((df["capture"] == "Y").sum())
        n_prom = int((df["promote"] == "Y").sum())
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Files", len(df))
        m2.metric("Extracted", n_ext)
        m3.metric("Captured", n_cap)
        m4.metric("Promoted", n_prom)
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.caption("captured=Y means data reached cat_* or dv_*; promoted=Y means it "
                   "reached dv_*. A promoted file shows captured=Y even though cat_* is "
                   "drained by promote — that's expected.")

        import time as _tm
        _rdir = st.session_state.get("fp_report", r"C:\Bulk\reports")
        try:
            os.makedirs(_rdir, exist_ok=True)
            _ts = _tm.strftime("%Y%m%d_%H%M%S")
            _path = os.path.join(_rdir, f"stage_scorecard_{_ts}.csv")
            df.to_csv(_path, index=False)
            st.success(f"Scorecard written to `{_path}`")
        except Exception as e:
            st.warning(f"Shown above; CSV write failed: {type(e).__name__}: {e}")


_PIPE_STAGES = ["scan", "extract", "enrich", "triage",
                "capture", "vault", "promote", "report"]


def _expected_stages(do_scan, do_capture, do_vault, do_promote):
    """The stages a run will actually execute, given the toggles — used as the
    denominator for the progress bar so it reaches 100% on a real finish."""
    exp = ["scan"] if do_scan else []
    exp += ["extract", "enrich", "triage"]
    if do_capture:
        exp.append("capture")
    if do_vault:
        exp.append("vault")
    if do_promote:
        exp.append("promote")
    exp.append("report")
    return exp


def _pipeline_progress(log_lines, expected):
    """Parse the streamed stage banners ('[stage] ▶ starting…' / '[stage] ✓ done')
    into (fraction, active_stage, per-stage checklist marks)."""
    started, done = set(), set()
    for ln in log_lines:
        for s in _PIPE_STAGES:
            if ln.startswith(f"[{s}]"):
                if "✓ done" in ln:
                    done.add(s)
                elif "▶" in ln or "starting" in ln:
                    started.add(s)
    current = None
    for s in expected:
        if s in started and s not in done:
            current = s
    if current is None:
        for s in expected:
            if s in done:
                current = s
    frac = (len(done & set(expected)) / len(expected)) if expected else 0.0
    marks = [f"{'✅' if s in done else ('⏳' if s == current else '▫')} {s}"
             for s in expected]
    return frac, current, marks


def _inventory_report_df(engine):
    """Full file-level inventory status straight from GLOBAL_FILE_CATALOG — every
    inventoried file, with a synthesized `action` (what actually happened to it),
    plus `readiness`, `duplicate`/`copied`/`promoted` flags and the content
    `file_hash` so duplicates can be traced to their canonical. `promoted`/
    `cataloged` count seismic once its survey reaches dv_seis_set. Written to the
    Report root after a run."""
    from sqlalchemy import text as _t
    import pandas as pd
    with engine.connect() as con:
        try:
            _seis_ok = con.execute(_t(
                "SELECT CASE WHEN OBJECT_ID('dataview.dv_seis_set') IS NOT NULL "
                "AND OBJECT_ID('file_catalog.FILE_SEIS_HEADER') IS NOT NULL "
                "THEN 1 ELSE 0 END")).scalar() == 1
        except Exception:
            _seis_ok = False
        if _seis_ok:
            _cte = ("WITH seis_done AS ("
                    "SELECT DISTINCT sh.INVENTORY_ID "
                    "FROM file_catalog.FILE_SEIS_HEADER sh "
                    "JOIN dataview.dv_seis_set ss "
                    "ON ss.seis_set_name = sh.SURVEY_NAME) ")
            _join = "LEFT JOIN seis_done sd ON sd.INVENTORY_ID = g.INVENTORY_ID "
            _seis = "OR sd.INVENTORY_ID IS NOT NULL "
        else:
            _cte = _join = _seis = ""
        sql = f"""
            {_cte}SELECT g.FILE_NAME AS file_name, g.FILE_EXT AS ext,
                   g.MATCHED_UWI AS uwi,
                   CASE
                     WHEN g.DUPLICATE_GROUP IS NOT NULL THEN 'duplicate — skipped'
                     WHEN g.HEADER_EXTRACTED='S' OR g.CATALOG_READINESS='SKIPPED'
                          THEN 'skipped'
                     WHEN (g.PROMOTED_AT IS NOT NULL {_seis}) AND g.VAULTED_AT IS NOT NULL
                          THEN 'vaulted + promoted'
                     WHEN (g.PROMOTED_AT IS NOT NULL {_seis})
                          THEN 'promoted'
                     WHEN g.VAULTED_AT IS NOT NULL THEN 'vaulted'
                     WHEN g.CATALOG_READINESS='CATALOGED' {_seis}THEN 'cataloged'
                     WHEN g.HEADER_EXTRACTED='Y' THEN 'extracted'
                     ELSE 'inventoried (pending)'
                   END AS action,
                   g.CATALOG_READINESS AS readiness,
                   g.HEADER_EXTRACTED AS extracted,
                   CASE WHEN g.DUPLICATE_GROUP IS NOT NULL THEN 'Y' ELSE '' END AS duplicate,
                   CASE WHEN g.VAULTED_AT  IS NOT NULL THEN 'Y' ELSE '' END AS copied,
                   CASE WHEN g.PROMOTED_AT IS NOT NULL {_seis}THEN 'Y' ELSE '' END AS promoted,
                   g.FILE_HASH AS file_hash,
                   g.VAULTED_AT AS vaulted_at, g.PROMOTED_AT AS promoted_at,
                   g.SCAN_DATE AS scanned, g.FILE_PATH AS path
            FROM file_catalog.GLOBAL_FILE_CATALOG g
            {_join}ORDER BY g.SCAN_DATE DESC, g.FILE_NAME
        """
        res = con.execute(_t(sql))
        return pd.DataFrame(res.fetchall(), columns=list(res.keys()))


def _pipeline_run_hero(engine, dialect):
    """The one-click full-pipeline run, shown as the hero at the top."""
    REF = _wb_ref()
    # ── Run Full Pipeline (always visible — not behind an expander) ──────────
    with st.container():
        # Demo control: pick which reference master enrich/triage resolve
        # against. Defaults to the production master; the mini is for testing.
        st.selectbox(
            "Reference master (enrich / triage lookup)",
            options=list(_WB_REF_OPTIONS),
            format_func=lambda x: f"{_WB_REF_OPTIONS.get(x, x)}  ·  {x}",
            key="wb_ref_choice")
        REF = _wb_ref()
        st.caption(f"Resolving against **{REF}**")
        with st.expander("❓ How to use — fields, the Apply toggle & the stages",
                         expanded=False):
            st.markdown(r"""
**Filling out the fields**

- **Scan root folder** — the top folder to crawl. Every supported file beneath
  it (LAS, DLIS, SEG-Y, PDF, shapefile, Excel/Word, WITSML, JSON…) is inventoried.
  Subfolders are included. *Required.*
- **Vault root** — where qualifying documents are filed when **Vault** + **Apply**
  are both on (default `C:\Bulk\Vault`). Files are copied, so the vault is a
  self-contained archive independent of the originals.
- **Report root** — where the run log, enrich report, and inventory report are
  written (default `C:\Bulk\reports`).
- **Formats to scan** — leave blank to scan every supported type, or list a few
  (e.g. `.las, .pdf`) to limit the whole run. The leading dot is optional.
- **Inventory / Capture / Vault / Promote** — which stages run (see below).
- **Parse workers / ⚡ Use all CPU cores** — parsing parallelism. Multi-core runs
  the parse in a detached process pool (fastest for SEG-Y/LAS/PDF); off keeps it
  in-app on one core.

**The Apply toggle — dry-run vs. execute**

This is the safety switch. The metadata stages (**scan, extract, enrich, triage,
capture**) always write — they build the inventory and the `cat_*` staging
mirrors regardless. The **destructive** stages behave differently:

- **Apply OFF (dry-run)** — Vault and Promote only *plan and count*: you see how
  many files would be filed and how many rows would move into `dv_*`, but nothing
  is copied and nothing is written to the golden tables. Safe to preview.
- **Apply ON (execute)** — Vault actually copies files into the vault, and Promote
  actually moves rows from `cat_*` up into the `dv_*` golden tables.

Run with **Apply off first** to review the counts, then turn it on to commit.

**The stages, in order**

1. **Scan** — crawl the folder, fingerprint each file, build/update the inventory
   (`GLOBAL_FILE_CATALOG`), and skip duplicates.
2. **Extract** — parse each file's header/metadata into the file-header tables
   (multi-core).
3. **Enrich** — resolve missing UWIs and fill blank attributes against the
   reference (`WELL_MASTER`); writes the enrich report.
4. **Triage** — normalize UWI/name, cross-fill from inventory and reference, then
   score and tier every file (HIGH / REVIEW / LOW).
5. **Capture** — for files with a resolved UWI, parse the content (PDF surveys &
   scout tickets, shapefiles, LAS curves) into the `cat_*` staging mirrors.
6. **Vault** — file qualifying documents into the curated vault *(plan only unless
   Apply)*.
7. **Promote** — move `cat_*` rows up into the `dv_*` golden tables, holding any
   row whose reference value (UOM, status, datum…) isn't seeded *(plan/count
   unless Apply)*.
8. **Report** — roll up the run, write a markdown summary, and record a
   `PIPELINE_RUN` row.
""")
        # Clear inventory is an ACTION button, which Streamlit forbids inside a
        # form — so it sits above the run-config form below.
        ci1, ci2 = st.columns([1, 4])
        _arm = ci1.checkbox(
            "Arm clear", value=False, key="fp_clearinv_arm",
            help="Safety latch — tick this, then press Clear inventory.")
        if ci2.button("🧹 Clear inventory", key="fp_clearinv", disabled=not _arm,
                      help="Empty the inventory list (GLOBAL_FILE_CATALOG). File IDs "
                           "are deterministic, so re-scanning rebuilds it and any "
                           "extracted headers re-link. dv_* / reference data left "
                           "untouched."):
            try:
                from sqlalchemy import text as _t
                with engine.begin() as _c:
                    _r = _c.execute(_t("DELETE FROM file_catalog.GLOBAL_FILE_CATALOG"))
                st.session_state.pop("fp_scorecard", None)   # force recompute
                st.success(f"Inventory cleared ({_r.rowcount:,} row(s)). Re-scan to "
                           "rebuild.")
            except Exception as e:
                st.error(f"Clear failed: {type(e).__name__}: {e}")

        running = st.session_state.get("fp_running", False)

        # Run config sits in a plain container — NOT a form. A form would make
        # Enter in the scan-root field submit and start the crawl; a container
        # keeps the grouped layout while leaving Run as the only trigger.
        with st.container():
            fp1, fp2, fp3 = st.columns(3)
            fp_root  = fp1.text_input("Scan root folder", value="", key="fp_root",
                                      placeholder=r"D:\data")
            fp_vault = fp2.text_input("Vault root", value=r"C:\Bulk\Vault",
                                      key="fp_vault")
            fp_report = fp3.text_input("Report root", value=r"C:\Bulk\reports",
                                       key="fp_report",
                                       help="Folder for the run log, enrich "
                                            "report, and inventory report.")
            fp_exts_raw = st.text_input(
                "Formats to scan", value="", key="fp_exts",
                placeholder="all supported types — or e.g.  .las, .dlis, .pdf",
                help="Blank = scan every supported type. To narrow the whole run "
                     "(scan · extract · capture), type a comma-separated list of "
                     "extensions, e.g.  .las, .dlis  — the leading dot is optional.")
            s1, s2, s4, s5 = st.columns(4)
            fp_inventory = s1.checkbox(
                "Inventory", value=True, key="fp_inventory",
                help="Scan the folder and build/update the catalog. On = (re)inventory "
                     "first; off = skip the scan and process the existing catalog.")
            fp_capture = s2.checkbox(
                "Capture", value=True, key="fp_capture",
                help="After inventory, parse documents (PDF surveys / scout tickets + "
                     "shapefiles) for every file with a resolved UWI. Off = stop after "
                     "inventory.")
            fp_vaulton = False   # Vault removed from the pipeline; run it from
            #                      the "📦 Vault" section of this page.
            fp_promote = s4.checkbox("Promote", value=True,  key="fp_promote")
            fp_apply   = s5.checkbox("Apply",   value=False, key="fp_apply",
                                     help="Move promote rows into dv_*. "
                                          "Off = plan/count only.")
            tcol, ccol = st.columns([1.5, 4])
            fp_workers = tcol.number_input(
                "Parse workers", min_value=1, max_value=64,
                value=int(st.session_state.get("fp_workers", 6)),
                step=1, key="fp_workers",
                help="Parallel parse workers for the extract stage.")
            fp_multicore = ccol.checkbox(
                "⚡ Use all CPU cores (multi-core parse)",
                value=bool(st.session_state.get("fp_multicore", True)),  # default: multi-core ON
                key="fp_multicore",
                help="ON: run the pipeline in a detached process so the extract "
                     "stage uses a true process pool across every core — same speed "
                     "as the CLI's --parse-mode process. Best for the SEG-Y / LAS / "
                     "PDF parsers, which are GIL-bound otherwise. OFF: run in-app on "
                     "threads (safe default; parsing is capped to one core).")
            bcol1, bcol2 = st.columns([1.5, 4])
            fp_batch = bcol2.checkbox(
                "📦 Batch mode — inventory all, then process N at a time",
                value=bool(st.session_state.get("fp_batch", False)),
                key="fp_batch",
                help="ON: walk the whole tree once to inventory it, then process "
                     "the catalog in batches until the queue is clear. Best for a "
                     "large corpus — bounds memory, gives resume points, and the "
                     "filesystem is only walked once. OFF: a single straight-through "
                     "run.")
            fp_batch_size = bcol1.number_input(
                "Batch size", min_value=50, max_value=100000,
                value=int(st.session_state.get("fp_batch_size", 1000)),
                step=50, key="fp_batch_size",
                disabled=not fp_batch,
                help="Files processed per batch (extract→…→promote), looped until "
                     "the inventory is cleared.")
            run_clicked = st.button(
                "▶ Run pipeline", type="primary", key="fp_run",
                use_container_width=True,
                disabled=(running or not fp_root.strip()
                          or not (fp_inventory or fp_capture)))

        fp_exts_sel = {
            "." + t.strip().lower().lstrip(".")
            for t in fp_exts_raw.replace(";", ",").split(",")
            if t.strip()
        } or None
        if fp_exts_sel:
            _unknown = sorted(e for e in fp_exts_sel if e not in KNOWN_EXTS)
            if _unknown:
                st.warning("Not a known type (will match nothing): "
                           + ", ".join(_unknown))
            _optin = sorted(e for e in fp_exts_sel if e in CSV_EXTS)
            if _optin:
                st.caption("CSV is opt-in — scanning delimited tables because "
                           "you listed " + ", ".join(_optin) + " explicitly.")
            st.caption("This run is limited to: " + ", ".join(sorted(fp_exts_sel)))

        # Enrich always runs (core to the chain). The per-batch stall watchdog is
        # fixed at 180s; Max-files / Sample-N were retired from the UI (CLI only).
        _STALL_TIMEOUT = 180
        import threading, time as _t_mod
        # Surface the REAL write target so a stale sidebar selection (the engine
        # only rebuilds on Connect, not on selectbox change) can't silently send
        # a run to the wrong database.
        try:
            from dataview.import_data import pipeline_run as _plc
            _tgt = (st.session_state.get("dw_conn_spec")
                    or _plc._engine_spec(engine))
            _tdb, _tsrv = _tgt.get("database") or "?", _tgt.get("server") or "?"
            st.caption(f"Target: **{_tsrv} / {_tdb}** — reconnect from the sidebar "
                       f"to change. Promote writes into `dataview.dv_*` here.")
        except Exception:
            pass

        stop_clicked = st.button(
            "⏹ Stop", key="fp_stop", use_container_width=True,
            disabled=not running,
            help="Abort after the current stage finishes — partial results kept.")

        # Guard against re-entry: don't start a second run if one is already
        # going (e.g. a stray rerun while running).
        if run_clicked and not st.session_state.get("fp_running"):
            from dataview.import_data import pipeline_run as _pl
            _ev = threading.Event()
            _log_buf, _result = [], {}
            _stopfile = None

            if fp_multicore:
                # Multi-core: run the pipeline in a DETACHED process (spawn-safe)
                # so the extract stage's process pool can use every core. A
                # reader thread tails the child's stdout into the same log list
                # the poller renders, and loads the child's state file on exit —
                # so timing panel / scorecard keep working. Stop is cooperative
                # via a stop-file the child polls at each stage boundary.
                import tempfile, json as _json, subprocess, sys as _sys
                _runner = os.path.join(
                    os.path.dirname(os.path.abspath(_pl.__file__)),
                    "pipeline_proc_runner.py")
                _tmpd = tempfile.mkdtemp(prefix="dwpipe_")
                _cfgp = os.path.join(_tmpd, "cfg.json")
                _statep = os.path.join(_tmpd, "state.json")
                _stopfile = os.path.join(_tmpd, "stop.flag")
                _spec = (st.session_state.get("dw_conn_spec")
                         or _pl._engine_spec(engine))
                _cfg = {
                    "url":  engine.url.render_as_string(hide_password=False),
                    "server":   _spec.get("server"),
                    "database": _spec.get("database"),
                    "driver":   _spec.get("driver"),
                    "root": fp_root.strip(),
                    "exts": sorted(fp_exts_sel) if fp_exts_sel else None,
                    "workers": int(fp_workers),
                    "dialect": str(dialect),
                    "do_scan":  bool(fp_inventory),
                    "do_enrich": True,
                    "do_capture": bool(fp_capture),
                    "inventory_only": not bool(fp_capture),
                    "do_vault": bool(fp_vaulton),
                    "vault_root": fp_vault.strip(),
                    "report_root": fp_report.strip() or None,
                    "vault_apply": bool(fp_apply and fp_vaulton),
                    "vault_mode": "copy",
                    "do_promote": bool(fp_promote),
                    "promote_apply": bool(fp_apply and fp_promote),
                    "do_deep": False,
                    "parse_mode": "process" if fp_multicore else "thread",
                    "batch_size": int(fp_batch_size) if fp_batch else None,
                    "stall_timeout": _STALL_TIMEOUT,
                    "ref": REF,
                    "state_out": _statep,
                    "stop_file": _stopfile,
                }
                _logfile = os.path.join(_tmpd, "console.log")
                _cfg["console_log"] = _logfile
                with open(_cfgp, "w", encoding="utf-8") as _f:
                    _json.dump(_cfg, _f)

                # DETACHED launch (page_run.py model): child writes stdout to a LOG
                # FILE; no pipe, no daemon reader thread, no rerun-held handles. The
                # poll loop reads the file. This is what stops orphan/respawn trees.
                if not os.path.exists(_runner):
                    _result["ok"] = False
                    _result["err"] = ("pipeline_proc_runner.py not deployed "
                                      "next to pipeline_run.py")
                    _result["done"] = True
                    _th = None
                else:
                    _env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
                    _CREATE_NO_WINDOW = 0x08000000
                    _fh = open(_logfile, "w", encoding="utf-8")
                    _proc = subprocess.Popen(
                        [_sys.executable, "-u", _runner, _cfgp],
                        stdout=_fh, stderr=subprocess.STDOUT,
                        cwd=os.path.dirname(_runner) or None, env=_env,
                        creationflags=_CREATE_NO_WINDOW)
                    _result["proc"] = _proc
                    st.session_state["fp_logfile"] = _logfile
                    st.session_state["fp_proc"] = _proc
                    st.session_state["fp_statep"] = _statep
                    _th = None      # no reader thread — the poll loop reads the file
            else:
                # In-app thread path (safe default): parsing stays GIL-bound to
                # one core, but no subprocess. The worker touches no Streamlit
                # APIs; it only appends to the shared log list and watches _ev.
                def _worker(_engine=engine, _root=fp_root.strip(), _exts=fp_exts_sel,
                            _vault=fp_vault.strip(), _cap=fp_capture,
                            _von=fp_vaulton, _prom=fp_promote, _apply=fp_apply,
                            _inv=fp_inventory, _workers=int(fp_workers),
                            _report=fp_report.strip() or None,
                            _batch=(int(fp_batch_size) if fp_batch else None),
                            _dialect=dialect):
                    try:
                        _common = dict(
                            workers=_workers, do_enrich=True,
                            do_capture=_cap, dialect=_dialect, do_deep=False,
                            do_vault=_von, vault_root=_vault,
                            vault_apply=_apply and _von, vault_mode="copy",
                            do_promote=_prom, promote_apply=_apply and _prom,
                            per_type_cap=None, stall_timeout=_STALL_TIMEOUT,
                            should_abort=_ev.is_set, ref=REF,
                            parse_mode=("process" if fp_multicore else "thread"),
                            report_root=_report, log=_log_buf.append)
                        if _batch:
                            _state = _pl.run_pipeline_batched(
                                _engine, _root, exts=_exts,
                                batch_size=_batch, scan_first=_inv, **_common)
                        else:
                            _state = _pl.run_pipeline(
                                _engine, _root, exts=_exts,
                                do_scan=_inv,
                                inventory_only=not _cap,  # Capture off ⇒ stop after inventory
                                max_files=None, **_common)
                        _result["ok"] = True
                        _result["state"] = _state or {}
                    except Exception as e:
                        _result["ok"] = False
                        _result["err"] = f"{type(e).__name__}: {e}"
                    _result["done"] = True

                _th = threading.Thread(target=_worker, daemon=True)

            st.session_state["fp_abort"]     = _ev
            st.session_state["fp_stopfile"]  = _stopfile
            st.session_state["fp_log"]       = _log_buf
            st.session_state["fp_result"]    = _result
            st.session_state["fp_thread"]    = _th
            st.session_state["fp_running"]   = True
            # per-run scorecard anchor: mark when THIS run began (UTC), so the
            # current-run scorecard can scope to files scanned since now.
            from datetime import datetime as _dtu, timezone as _tzu
            st.session_state["fp_run_started"] = _dtu.now(_tzu.utc).strftime("%Y-%m-%d %H:%M:%S")
            st.session_state["fp_apply_run"] = fp_apply
            st.session_state["fp_vault_run"] = fp_vault.strip()
            st.session_state["fp_report_run"] = fp_report.strip() or r"C:\Bulk\reports"
            if _th is not None:          # detached run has no thread to start
                _th.start()
            # NB: no st.rerun() here. Calling it in the same script run as the
            # form submission can re-deliver the submit, re-enter this handler,
            # and spawn a restart loop ("uncontrolled reruns"). The poll loop at
            # the end (gated on fp_running) drives the live refresh instead, so
            # the progress view still appears on this very run.

        if stop_clicked:
            _ev = st.session_state.get("fp_abort")
            if _ev:
                _ev.set()
            # Multi-core runs in a separate process that can't see the Event, so
            # signal it via the stop-file it polls at each stage boundary.
            _sf = st.session_state.get("fp_stopfile")
            if _sf:
                try:
                    open(_sf, "w").close()
                except Exception:
                    pass
            st.session_state.get("fp_log", []).append(
                "⏹ abort requested — stopping after the current stage…")

        # Live log + completion handling. While the worker runs, poll a few times
        # a second so the log streams and the Stop click can land.
        if st.session_state.get("fp_running"):
            # fp_logfile poll: detached multi-core run writes to a log file — read it
            # fresh each cycle so the log/scorecard update without a reader thread.
            _lf = st.session_state.get("fp_logfile")
            if _lf:
                try:
                    with open(_lf, "r", encoding="utf-8", errors="replace") as _lfh:
                        st.session_state["fp_log"] = _lfh.read().splitlines()
                except Exception:
                    pass
                _pr = st.session_state.get("fp_proc")
                if _pr is not None and _pr.poll() is not None:
                    # process exited — load state file, mark done
                    _res2 = st.session_state.get("fp_result", {})
                    import json as _json2
                    try:
                        with open(st.session_state.get("fp_statep",""), "r",
                                  encoding="utf-8") as _sf2:
                            _res2["state"] = _json2.load(_sf2)
                    except Exception:
                        pass
                    _res2["ok"] = (_pr.returncode == 0)
                    if _pr.returncode != 0 and "err" not in _res2:
                        _res2["err"] = f"runner exit {_pr.returncode}"
                    _res2["done"] = True
                    st.session_state["fp_result"] = _res2
            _log = st.session_state.get("fp_log", [])
            _exp = _expected_stages(fp_inventory, fp_capture, fp_vaulton,
                                    fp_promote)
            _frac, _cur, _marks = _pipeline_progress(_log, _exp)
            _res = st.session_state.get("fp_result", {})
            _th = st.session_state.get("fp_thread")
            _proc_live = st.session_state.get("fp_proc")
            _done = bool(_res.get("done")
                         or (_th is not None and not _th.is_alive())
                         or (_proc_live is not None and _proc_live.poll() is not None))
            st.progress(1.0 if _done else _frac,
                        text=(f"▶ {(_cur or 'starting').upper()} · "
                              f"{int((1.0 if _done else _frac) * 100)}%"))
            st.caption("   ".join(_marks))
            if _done:
                st.session_state["fp_running"] = False
                for _k in ("fp_logfile", "fp_proc", "fp_statep"):
                    st.session_state.pop(_k, None)
                st.session_state["fp_stage_times"] = \
                    (_res.get("state") or {}).get("stage_times") or {}
                try:
                    st.session_state["fp_scorecard"] = _inventory_scorecard(engine)
                except Exception:
                    pass
                # Tee the run log + inventory report to the Report root
                _rr = st.session_state.get("fp_report_run", r"C:\Bulk\reports")
                try:
                    import time as _tm
                    _rdir = _rr
                    os.makedirs(_rdir, exist_ok=True)
                    _ts = _tm.strftime("%Y%m%d_%H%M%S")
                    _logp = os.path.join(_rdir, f"pipeline_{_ts}.log")
                    with open(_logp, "w", encoding="utf-8") as _lf:
                        _lf.write("\n".join(st.session_state.get("fp_log", [])))
                    _csvp = os.path.join(_rdir, f"inventory_{_ts}.csv")
                    _inventory_report_df(engine).to_csv(_csvp, index=False)
                    st.session_state["fp_report_paths"] = (_logp, _csvp)
                except Exception as _e:
                    st.session_state["fp_report_paths"] = None
                    st.warning(f"Couldn't write reports to {_rr}: "
                               f"{type(_e).__name__}: {_e}")
                _aborted = bool(st.session_state.get("fp_abort")
                                and st.session_state["fp_abort"].is_set())
                if _res.get("ok"):
                    if _aborted:
                        st.warning("Pipeline aborted — stages completed before the "
                                   "stop are kept; the rest were skipped.")
                    else:
                        _ap = st.session_state.get("fp_apply_run")
                        st.success(
                            f"Pipeline finished ({'APPLY' if _ap else 'dry-run'}). "
                            f"All reports saved under {_rr}.")
                        # accurate 'what actually promoted' report (reads dv_* directly,
                        # unlike the per-file 'promoted' flag which only covers seismic)
                        if _ap:
                            try:
                                from modules.promote_report_ui import render as _render_promote_report
                                _render_promote_report(engine, st)
                            except Exception as _rx:
                                st.caption(f"(report unavailable: {str(_rx)[:100]})")
                    _paths = st.session_state.get("fp_report_paths")
                    if _paths:
                        st.caption(f"Reports in `{_rr}`  ·  "
                                   f"run report, enrich report, "
                                   f"{os.path.basename(_paths[0])} · "
                                   f"{os.path.basename(_paths[1])}")
                else:
                    st.error(f"Pipeline failed: {_res.get('err', '?')}")
            else:
                # live scorecard: refresh at most every ~5s so the funnel updates
                # without re-aggregating the catalog often enough to slow the run
                _now = _t_mod.monotonic()
                if _now - st.session_state.get("fp_scorecard_at", 0.0) >= 5.0:
                    try:
                        st.session_state["fp_scorecard"] = _inventory_scorecard(engine)
                        st.session_state["fp_scorecard_at"] = _now
                    except Exception:
                        pass
        elif st.session_state.get("fp_report_paths"):
            st.caption(
                f"Last run's reports are in "
                f"`{st.session_state.get('fp_report_run', r'C:\Bulk\reports')}`.")

        _render_stage_timing()
        _render_scorecard(engine)        # rendered every cycle, including mid-run
        # per-run scorecard: what THIS run just did (cumulative table is all crawls)
        try:
            from dataview.file_catalog.current_run_scorecard import render as _render_run_scorecard
            _render_run_scorecard(engine, st, since=st.session_state.get("fp_run_started"))
        except Exception as _rsx:
            st.caption(f"(per-run scorecard unavailable: {str(_rsx)[:100]})")

        # keep polling AFTER the scorecard renders, so each cycle shows fresh numbers
        if st.session_state.get("fp_running"):
            _t_mod.sleep(0.5)
            st.rerun()



_CATALOG_COLS_ENSURED = False


def _inventory_scorecard(engine):
    """Per-file-type funnel: how many files were inventoried vs how far each got
    (extracted → cataloged → vaulted → promoted) and how many are still pending.
    Reads only GLOBAL_FILE_CATALOG with NOLOCK so it can poll live during a run
    without blocking — or being blocked by — the writing worker (dirty reads are
    fine for a progress view; the post-run refresh reads a settled catalog)."""
    global _CATALOG_COLS_ENSURED
    import pandas as pd
    from sqlalchemy import text as _t
    if not _CATALOG_COLS_ENSURED:                 # ensure stamp columns once/process
        with engine.begin() as con:
            con.execute(_t(
                "IF COL_LENGTH('file_catalog.GLOBAL_FILE_CATALOG','VAULTED_AT') IS NULL "
                "ALTER TABLE file_catalog.GLOBAL_FILE_CATALOG ADD VAULTED_AT DATETIME2 NULL;"))
            con.execute(_t(
                "IF COL_LENGTH('file_catalog.GLOBAL_FILE_CATALOG','PROMOTED_AT') IS NULL "
                "ALTER TABLE file_catalog.GLOBAL_FILE_CATALOG ADD PROMOTED_AT DATETIME2 NULL;"))
        try:
            # Covering index so the per-type aggregate is an index-only scan (no
            # base-table scan) — keeps the live read cheap and off the writer's back.
            with engine.begin() as con:
                con.execute(_t(
                    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_GFC_scorecard' "
                    "AND object_id=OBJECT_ID('file_catalog.GLOBAL_FILE_CATALOG')) "
                    "CREATE NONCLUSTERED INDEX IX_GFC_scorecard "
                    "ON file_catalog.GLOBAL_FILE_CATALOG (FILE_EXT) "
                    "INCLUDE (HEADER_EXTRACTED, CATALOG_READINESS, VAULTED_AT, PROMOTED_AT);"))
        except Exception:
            pass                                  # perf aid only — never block on it
        _CATALOG_COLS_ENSURED = True
    # Seismic skips the cat_* / PROMOTED_AT path entirely: a SEG-Y line file is
    # "cataloged" + "promoted" once its survey name has been merged into
    # dataview.dv_seis_set. Credit each line file (one GFC row) whose INVENTORY_ID
    # carries a survey name that landed there.
    #
    # NB: SQL Server forbids a subquery *inside* an aggregate (error 130:
    # "Cannot perform an aggregate function on an expression containing a
    # subquery"), so the seismic test can't live in SUM(CASE WHEN EXISTS(...)).
    # Instead precompute the set of "survey-promoted" INVENTORY_IDs as a CTE and
    # LEFT JOIN it — then the per-row flag is just a joined column. The DISTINCT
    # keeps the join 1:1 so COUNT(*) is unaffected. Guard on the tables existing
    # so the live poll never crashes on a DB without the seismic objects.
    with engine.connect() as con:
        try:
            _seis_ok = con.execute(_t(
                "SELECT CASE WHEN OBJECT_ID('dataview.dv_seis_set') IS NOT NULL "
                "AND OBJECT_ID('file_catalog.FILE_SEIS_HEADER') IS NOT NULL "
                "THEN 1 ELSE 0 END")).scalar() == 1
        except Exception:
            _seis_ok = False
        if _seis_ok:
            _seis_cte = ("seis_done AS ("
                    "SELECT DISTINCT sh.INVENTORY_ID "
                    "FROM file_catalog.FILE_SEIS_HEADER sh WITH (NOLOCK) "
                    "JOIN dataview.dv_seis_set ss "
                    "ON ss.seis_set_name = sh.SURVEY_NAME) ")
            _join = "LEFT JOIN seis_done sd ON sd.INVENTORY_ID = g.INVENTORY_ID "
            _seis = "OR sd.INVENTORY_ID IS NOT NULL "
        else:
            _seis_cte = _join = _seis = ""

        # ── document promotion credit ────────────────────────────────────────
        # A .pdf/.xlsx/.csv/.docx is 'promoted' once its deep data (tops, dir
        # survey, production, completion, log curves) lands in dv_* — tracked by
        # the INVENTORY_ID lineage promote copies into each dv_ detail table.
        # Build a UNION over whichever dv_ document tables exist + carry
        # INVENTORY_ID, then credit any GFC row whose INVENTORY_ID appears there.
        _doc_tables = ("dv_well_formation_top", "dv_well_dir_srvy_hdr",
                       "dv_well_dir_srvy_sta", "dv_well_completion",
                       "dv_prod_volume", "dv_prod_entity", "dv_well_log",
                       "dv_well_log_curve", "dv_well_core", "dv_well_dst")
        _parts = []
        for _dt in _doc_tables:
            try:
                _has = con.execute(_t(
                    "SELECT CASE WHEN OBJECT_ID(:o) IS NOT NULL AND EXISTS("
                    "SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID(:o) "
                    "AND name='INVENTORY_ID') THEN 1 ELSE 0 END"),
                    {"o": f"dataview.{_dt}"}).scalar() == 1
            except Exception:
                _has = False
            if _has:
                _parts.append(f"SELECT INVENTORY_ID FROM dataview.{_dt} WITH (NOLOCK) "
                              f"WHERE INVENTORY_ID IS NOT NULL")
        if _parts:
            _docs_cte = ("docs_done AS (SELECT DISTINCT INVENTORY_ID FROM ("
                         + " UNION ALL ".join(_parts) + ") _u) ")
            _djoin = "LEFT JOIN docs_done dd ON dd.INVENTORY_ID = g.INVENTORY_ID "
            _dprom = "OR dd.INVENTORY_ID IS NOT NULL "
        else:
            _docs_cte = _djoin = _dprom = ""

        # assemble the combined WITH clause from whichever CTEs are present
        _ctes = [c for c in (_seis_cte, _docs_cte) if c]
        _cte = ("WITH " + ", ".join(_ctes) + " ") if _ctes else ""
        _join = _join + _djoin
        sql = _t(f"""
            {_cte}SELECT
                ISNULL(NULLIF(g.FILE_EXT,''),'(none)')                           AS [type],
                COUNT(*)                                                          AS inventoried,
                SUM(CASE WHEN g.HEADER_EXTRACTED='Y' THEN 1 ELSE 0 END)           AS extracted,
                -- captured (durable): count files that WENT THROUGH capture via the
                -- CAPTURED_HASH stamp (survives promote, unlike the cat_* staging rows
                -- or the CATALOG_READINESS flag that LAS never gets set to CATALOGED).
                SUM(CASE WHEN g.CAPTURED_HASH IS NOT NULL
                         OR g.CATALOG_READINESS='CATALOGED'
                         OR g.PROMOTED_AT IS NOT NULL {_seis}THEN 1 ELSE 0 END)   AS cataloged,
                SUM(CASE WHEN g.VAULTED_AT  IS NOT NULL THEN 1 ELSE 0 END)        AS vaulted,
                SUM(CASE WHEN g.PROMOTED_AT IS NOT NULL {_seis}{_dprom}THEN 1 ELSE 0 END) AS promoted,
                -- pending: NOT extracted AND NOT captured (a captured-then-promoted
                -- file is done, not pending — key off CAPTURED_HASH so the drain of
                -- cat_* on promote doesn't resurrect it as 'pending').
                SUM(CASE WHEN (g.HEADER_EXTRACTED IS NULL OR g.HEADER_EXTRACTED IN ('N',''))
                          AND g.CAPTURED_HASH IS NULL
                          AND g.PROMOTED_AT IS NULL
                         THEN 1 ELSE 0 END)                                       AS pending,
                SUM(CASE WHEN g.HEADER_EXTRACTED='S' THEN 1 ELSE 0 END)           AS skipped
            FROM file_catalog.GLOBAL_FILE_CATALOG g WITH (NOLOCK)
            {_join}GROUP BY ISNULL(NULLIF(g.FILE_EXT,''),'(none)')
            ORDER BY pending DESC, inventoried DESC
        """)
        rows = con.execute(sql).fetchall()
    return pd.DataFrame(rows, columns=["type", "inventoried", "extracted",
                                       "cataloged", "vaulted", "promoted",
                                       "pending", "skipped"])


def _render_stage_timing():
    """Show the last run's per-stage wall-clock, slowest first, so the bottleneck
    is obvious at a glance. Populated from run_pipeline's stage_times on finish."""
    _times = st.session_state.get("fp_stage_times") or {}
    if not _times:
        return
    import pandas as pd
    total = sum(_times.values()) or 1.0
    rows = [{"stage": k, "seconds": round(v, 1), "% of run": round(v / total * 100)}
            for k, v in sorted(_times.items(), key=lambda kv: -kv[1])]
    st.divider()
    st.markdown(f"**⏱ Last run — stage timing (slowest first) · {total:.1f}s total**")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def _render_scorecard(engine):
    """Render the inventory-vs-processed scorecard from the cached snapshot in
    session_state. Refreshes happen elsewhere (live throttle while running,
    on-complete, manual button, first open) so this just displays."""
    st.divider()
    live = bool(st.session_state.get("fp_running"))
    sc1, sc2 = st.columns([3, 1])
    sc1.markdown("**📊 Inventory vs processed — by file type**"
                 + ("  🟢 live" if live else ""))
    if sc2.button("↻ Refresh", key="fp_scorecard_refresh",
                  use_container_width=True, disabled=live):
        try:
            st.session_state["fp_scorecard"] = _inventory_scorecard(engine)
        except Exception as e:
            st.caption(f"scorecard unavailable: {type(e).__name__}: {e}")
    if "fp_scorecard" not in st.session_state and not live:   # first open, idle
        try:
            st.session_state["fp_scorecard"] = _inventory_scorecard(engine)
        except Exception as e:
            st.caption(f"scorecard unavailable: {type(e).__name__}: {e}")
    _sc = st.session_state.get("fp_scorecard")
    if _sc is not None and not _sc.empty:
        _tot = _sc.sum(numeric_only=True)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Inventoried", f"{int(_tot['inventoried']):,}")
        m2.metric("Cataloged",   f"{int(_tot['cataloged']):,}")
        m3.metric("Vaulted",     f"{int(_tot['vaulted']):,}")
        m4.metric("Pending",     f"{int(_tot['pending']):,}")
        st.dataframe(_sc, hide_index=True, use_container_width=True)
        st.caption("**Pending** = not yet extracted · **Cataloged** = document "
                   "rows captured · **Vaulted** = file placed in the vault · "
                   "**Promoted** = cataloged rows lifted into dv_* on a clean "
                   "apply. Binaries (LAS/DLIS/SEG-Y) extract + vault but never "
                   "catalog/promote, so those columns stay 0 for them — expected, "
                   "not a backlog.")
    elif _sc is not None:
        st.caption("Inventory is empty — run an inventory to populate this.")


def _pipeline_stages(engine, dialect):
    """The individual ①–⑧ stages, shown inside the Advanced expander."""
    import types as _types
    try:
        _db   = engine.url.database or "?"
        _host = engine.url.host or "?"
    except Exception:
        _db = _host = "?"
    REF = _wb_ref()
    # ── ① Enrich Headers ───────────────────────────────────────────────────
    with st.expander(
        "① Enrich Headers — curate UWI14 · resolve by name · fill blanks",
        expanded=True,
    ):
        st.caption("Adds/refreshes the canonical UWI14 on FILE_WELL_HEADER, "
                   "resolves blank UWIs by name against the reference, fills blank "
                   "attributes, and records document→reference contributions.")
        c1, c2 = st.columns([1, 3])
        en_dry = c1.checkbox("Dry run", value=True, key="pl_en_dry")
        en_rev = c1.checkbox(
            "Reverse-capture", value=False, key="pl_en_rev",
            help="Write document values back into the reference where it's "
                 "missing them. Slow (full WELL_MASTER scan) — off by default.")
        if c2.button("Run enrichment", type="primary", key="pl_en_run",
                     use_container_width=True):
            from dataview.file_catalog import enrich_file_headers as _en
            a = _types.SimpleNamespace(
                server=_host, database=_db, odbc_driver="",
                ref=REF, depth_tol=50.0,
                no_well=False, no_seis=False, no_reverse=not en_rev,
                dry_run=en_dry, report=None, reverse_report=None)

            _bar   = st.progress(0.0, text="Starting enrichment…")
            _logbox = st.empty()
            _lines = []

            def _log(line):
                _lines.append(str(line))
                # stream live; keep the tail so the box doesn't grow unbounded
                _logbox.code("\n".join(_lines[-400:]))

            def _prog(step, total, label):
                _bar.progress(min(1.0, step / max(total, 1)),
                              text=f"[{step}/{total}] {label}")

            err = None
            raw = engine.raw_connection()
            try:
                _en.enrich(raw, a, log=_log, progress=_prog)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            finally:
                try:
                    raw.close()
                except Exception:
                    pass
            _bar.empty()
            if err:
                st.error(err)
            _logbox.code("\n".join(_lines) or "(no output)")

    # ── ② Vault Copy ───────────────────────────────────────────────────────
    with st.expander(
        "② Vault Copy — file catalogued wells & seismic into the vault tree"
    ):
        st.caption(r"Wells → <vault>\curated\wells\<STATE>\<COUNTY>\<UWI>__<NAME>\<class> · "
                   r"Seismic → <vault>\curated\seismic\<2D|3D>\<SURVEY>\<class> · "
                   "spatial / _unmatched as applicable. Files are copied, never moved. "
                   "Same routing as `python vault_organizer.py`.")
        v1, v2 = st.columns(2)
        v_vault = v1.text_input("Vault root", value=r"C:\Bulk\Vault", key="pl_v_vault")
        v_mode  = v2.selectbox("Mode", ["copy", "hardlink", "symlink"], index=0,
                               key="pl_v_mode",
                               help="copy = duplicate bytes (any volume). "
                                    "hardlink / symlink = no extra disk, same "
                                    "volume only.")
        v3, v4 = st.columns([1, 3])
        v_dry = v3.checkbox("Dry run", value=True, key="pl_v_dry")
        if v4.button("Run vault copy", type="primary", key="pl_v_run",
                     use_container_width=True):
            import os as _os
            from dataview.file_catalog import vault_organizer as _vo
            from collections import Counter as _Counter
            try:
                with engine.connect() as _con:
                    rows = _vo.fetch_rows(_con, "file_catalog", None)
                plan, _carried = _vo.build_plan(
                    rows, _os.path.join(v_vault, "curated"))
                if not plan:
                    st.info("Nothing to place — catalog is empty or no files have "
                            "resolved identities yet.")
                else:
                    buckets = _Counter(p[2].split("/")[0] for p in plan)
                    detail = " · ".join(f"{b} {n:,}"
                                        for b, n in sorted(buckets.items()))
                    if v_dry:
                        st.info(f"Planned {len(plan):,} placements (dry-run) — "
                                f"{detail}. Uncheck **Dry run** to write them.")
                    else:
                        placed = exists = errored = 0
                        bar = st.progress(0.0, text="Copying to vault…")
                        for i, (src, dst, _b) in enumerate(plan, 1):
                            try:
                                r = _vo.place(src, dst, v_mode)
                                placed += (r == "ok")
                                exists += (r == "exists")
                            except Exception:
                                errored += 1
                            if i % 50 == 0 or i == len(plan):
                                bar.progress(i / len(plan))
                        bar.empty()
                        st.success(f"Placed {placed:,} ({v_mode}) · {exists:,} "
                                   f"already present · {errored:,} errored — of "
                                   f"{len(plan):,} total. {detail}")
            except Exception as e:
                st.error(f"Vault step failed: {e}")

    # ── ③ Collect Final Documents ──────────────────────────────────────────
    with st.expander(
        "③ Collect Final Documents — gather files whose path contains a keyword"
    ):
        st.caption("Copies catalogued documents whose path contains the keyword "
                   "(as a whole word) into the vault, classified well/seismic/other.")
        d1, d2 = st.columns(2)
        d_vault = d1.text_input("Vault root", value=r"C:\Bulk\Vault", key="pl_d_vault")
        d_word  = d2.text_input("Keyword", value="final", key="pl_d_word")
        d3, d4 = st.columns([1, 3])
        d_dry = d3.checkbox("Dry run", value=True, key="pl_d_dry")
        if d4.button("Run collect", type="primary", key="pl_d_run",
                     use_container_width=True):
            from dataview.file_catalog import collect_final_documents as _cf
            a = _types.SimpleNamespace(
                server=_host, database=_db, odbc_driver="",
                vault=d_vault, dest=None, word=d_word, types=None,
                limit=0, all_ext=False, substring=False, dry_run=d_dry)
            lines, res, err = _run_op(engine, _cf.collect, a, "Collecting documents…")
            if err:
                st.error(err)
            st.code("\n".join(lines) or "(no output)")

    # ── ④ Key Wells & Surveys — review & assign from filename ───────────────
    with st.expander(
        "④ Key Wells & Surveys — review filename-derived UWIs / survey names and assign"
    ):
        st.caption("Files the extractor couldn't key internally, with a guess "
                   "parsed from the path. Edit the assign column and Save — wells "
                   "write UWI + UWI14 (so re-enrichment keeps them), seismic writes "
                   "SURVEY_NAME.")
        sub = st.radio("Review set", ["Wells (need UWI)", "Seismic (need survey)"],
                       horizontal=True, key="pl_rev_kind")
        if sub.startswith("Wells"):
            _well_key_grid(engine)
        else:
            _seis_survey_grid(engine)

    # ── ⑤ Deep Catalog ─────────────────────────────────────────────────────
    with st.expander(
        "⑤ Deep Catalog — parse LAS/DLIS/LIS/SEG-Y/P190 into las_catalog detail tables"
    ):
        st.caption(
            "Runs the per-format deep cataloger over files already in the "
            "catalog, populating las_catalog.* (LAS curves, DLIS frames/channels, "
            "LIS channels, seismic headers). Separate from Phase 2 extraction — "
            "it reads each file once for the detail tables and can run over files "
            "extracted earlier. Failures are tallied per file, never fatal."
        )
        import deep_catalog as _dc
        _groups = list(_dc.DEEP_GROUPS.keys())
        g_sel = st.multiselect("Formats", _groups, default=_groups,
                               key="pl_dc_groups")
        dc1, dc2, dc3 = st.columns([1, 1, 1])
        dc_limit = dc1.number_input("Limit (0 = all)", min_value=0, value=0,
                                    step=10, key="pl_dc_limit")
        dc_workers = dc2.number_input(
            "Workers", min_value=1, max_value=16, value=1, step=1,
            key="pl_dc_workers",
            help="1 = serial (safe). >1 only if the cataloger modules open "
                 "their own connection per call.")
        dc_dry = dc3.checkbox("Dry run", value=True, key="pl_dc_dry")
        dc_hdr = st.checkbox(
            "Headers only (DLIS/LIS skip the channel walk — much faster; "
            "LAS keeps its curve definitions)",
            value=True, key="pl_dc_hdr")
        if st.button("Run deep catalog", type="primary", key="pl_dc_run",
                     use_container_width=True):
            if not g_sel:
                st.warning("Select at least one format.")
            else:
                exts = sorted({e for g in g_sel for e in _dc.DEEP_GROUPS[g]})
                a = _types.SimpleNamespace(
                    server=_host, database=_db, odbc_driver="",
                    exts=",".join(exts), limit=int(dc_limit),
                    workers=int(dc_workers), dry_run=dc_dry,
                    header_only=dc_hdr, report=None)

                # Live progress: a bar + tally driven by deep_catalog's
                # per-file `progress` callback, so a long serial run shows
                # movement instead of an opaque spinner.
                _bar   = st.progress(0.0, text="Starting deep catalog…")
                _tally = st.empty()
                _lines = []

                def _prog(done, total, ok, failed, skipped, name):
                    _bar.progress(
                        min(1.0, done / max(total, 1)),
                        text=f"🧩 Deep catalog — {done:,}/{total:,} · {name}")
                    _tally.caption(
                        f"ok {ok:,} · failed {failed:,} · skipped {skipped:,}")

                res, err = None, None
                try:
                    res = _dc.deep_catalog(
                        engine, a, log=_lines.append, progress=_prog)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"

                _bar.empty()
                if err:
                    st.error(err)
                if isinstance(res, dict):
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Scanned", f"{res.get('scanned', 0):,}")
                    m2.metric("OK", f"{res.get('ok', 0):,}")
                    m3.metric("Failed", f"{res.get('failed', 0):,}")
                    m4.metric("Skipped", f"{res.get('skipped', 0):,}")
                    _errs = res.get("errors") or []
                    if _errs:
                        st.warning(f"⚠️ {len(_errs)} failure(s):")
                        st.code("\n".join(_errs))
                st.code("\n".join(_lines) or "(no output)")

    # ── ⑥ Load Scorecard ────────────────────────────────────────────────────
    with st.expander(
        "⑥ Load Scorecard — files & rows loaded, cataloged, vaulted, by data type"
    ):
        st.caption("Runs catalog_scorecard.py against this database: files and "
                   "rows captured into the cat_* mirrors (to date / this month), "
                   "files cataloged and vaulted, and a per-mirror data-type "
                   "breakdown. Same report as the command-line script.")
        sc1, sc2 = st.columns([1, 3])
        sc_month = sc1.text_input("Month (YYYY-MM · blank = current)", value="",
                                  key="pl_sc_month", placeholder="2026-06")
        if sc2.button("Generate scorecard", type="primary", key="pl_sc_run",
                      use_container_width=True):
            from dataview.file_catalog import catalog_scorecard as _sc
            raw = engine.raw_connection()
            data, err = None, None
            try:
                with st.spinner("Building scorecard…"):
                    data = _sc.gather(raw.cursor(), sc_month.strip() or None)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            finally:
                try:
                    raw.close()
                except Exception:
                    pass
            if err:
                st.session_state["pl_sc_err"] = err
                st.session_state.pop("pl_sc_html", None)
            else:
                st.session_state["pl_sc_html"] = _sc.render_html(data, _host, _db)
                st.session_state["pl_sc_text"] = _sc.render_report(data, _host, _db)
                st.session_state["pl_sc_label"] = data["month_label"]
                st.session_state.pop("pl_sc_err", None)

        if st.session_state.get("pl_sc_err"):
            st.error(st.session_state["pl_sc_err"])
        if st.session_state.get("pl_sc_html"):
            import streamlit.components.v1 as _components
            _components.html(st.session_state["pl_sc_html"], height=1500,
                             scrolling=True)
            st.download_button(
                "⬇ Download scorecard HTML",
                data=st.session_state["pl_sc_html"].encode("utf-8"),
                file_name=f"scorecard_{st.session_state.get('pl_sc_label','')}.html",
                mime="text/html", key="pl_sc_dl")
            with st.expander("Plain-text version", expanded=False):
                st.code(st.session_state["pl_sc_text"])

    # ── ⑦ Triage & Review ──────────────────────────────────────────────────
    # Not an expander: page_triage.render() has its own expanders, and Streamlit
    # forbids nesting. A toggle reveals it inline instead.
    st.markdown("---")
    st.markdown("**⑦ Triage & Review** — tier the catalog · work the "
                "REVIEW / LOW queues")
    st.caption("The full triage panel, on demand: run a triage pass, then "
               "resolve the REVIEW and LOW worklists. It loads only when you "
               "open it, so it never scans the reference on its own.")
    if st.toggle("Open Triage & Review", key="pl_triage_open"):
        from dataview.file_catalog import page_triage as _ptri
        _ptri.render(engine, dialect)

    # ── ⑧ Promote to Database ───────────────────────────────────────────────
    with st.expander(
        "⑧ Promote to Database — lift cat_* mirror rows up into the dv_* tables"
    ):
        st.caption("Moves captured rows from the file_catalog.cat_* mirrors into "
                   "dataview.dv_*, then clears the promoted rows from the mirrors. "
                   "Same code as `python promote_catalog.py`. Dry run reports "
                   "eligible counts without moving anything.")
        p1, p2 = st.columns(2)
        p_uwi = p1.text_input("Only this UWI (blank = all)", value="",
                              key="pl_pr_uwi", placeholder="42…")
        p_dry = p2.checkbox("Dry run", value=True, key="pl_pr_dry")
        if st.button("Run promote", type="primary", key="pl_pr_run",
                     use_container_width=True):
            from dataview.file_catalog import promote_catalog as _pc
            apply = not p_dry
            raw = engine.raw_connection()
            lines, err = [], None
            try:
                cur = raw.cursor()
                with st.spinner("Promoting…" if apply else "Counting eligible…"):
                    _pc.run_promote(cur, (p_uwi.strip() or None), apply,
                                    log=lines.append)
                if apply:
                    raw.commit()
                else:
                    raw.rollback()      # nothing written; release read locks
            except Exception as e:
                try:
                    raw.rollback()
                except Exception:
                    pass
                err = f"{type(e).__name__}: {e}"
            finally:
                try:
                    raw.close()
                except Exception:
                    pass
            if err:
                st.error(err)
            st.code("\n".join(lines) or "(no output)")
            if not err and apply:
                st.success("Promote complete — rows moved into dv_* and cleared "
                           "from the cat_* mirrors.")

        # ── reference FK review — resolve rows promote HELD on unseeded codes ──
        # Rows whose source/uom/etc. code isn't in its dv_r_* reference are parked
        # (not crashed). This grid surfaces those held codes so you can Add them as
        # new vocabulary or Map them to an existing code, then re-run promote.
        st.divider()
        try:
            from dataview.file_catalog.promote_fk_review import render as _render_promote_fk
            _render_promote_fk(engine, st)
        except Exception as _fkx:
            st.caption(f"(FK review unavailable: {str(_fkx)[:100]})")

    # ── ⑨ Clear / Reset ─────────────────────────────────────────────────────
    with st.expander(
        "⑨ Clear / Reset — wipe file_catalog, las_catalog & dv_* catalog rows "
        "(+ optional vault)"
    ):
        st.caption("Deletes everything the document pipeline produced: all "
                   "file_catalog + las_catalog tables and the catalog-derived "
                   "dv_* tables. Reference / spatial tables are left intact. "
                   "Same code as `python clear_catalog.py`. Destructive — type "
                   "CLEAR to enable.")
        x1, x2 = st.columns(2)
        x_dv    = x1.checkbox("Include dv_* catalog tables", value=True,
                              key="pl_x_dv")
        x_vault = x2.checkbox(r"Also delete vault\curated", value=False,
                              key="pl_x_vault")
        x_vroot = st.text_input("Vault root (for the vault delete)",
                                value=r"C:\Bulk\Vault", key="pl_x_vroot")
        x_conf = st.text_input(
            "⚠ Type CLEAR in this box to enable the delete button",
            value="", key="pl_x_conf", placeholder="CLEAR")
        bcol1, bcol2 = st.columns(2)
        run_clear = bcol2.button(
            "🗑 Clear now", type="primary", key="pl_x_apply",
            use_container_width=True,
            disabled=(x_conf.strip().upper() != "CLEAR"))

        if bcol1.button("Show counts (dry run)", key="pl_x_dry",
                        use_container_width=True):
            from dataview.file_catalog import clear_catalog as _cc
            raw = engine.raw_connection()
            out = []
            try:
                cur = raw.cursor()
                cur.execute(_cc._SET_OPTS)
                tbls = _cc.gather(cur, do_dv=x_dv, keep=[])
                total = sum(n for _, _, n, sc in tbls if n > 0 and sc != "skip")
                out.append(f"{'table':46} {'rows':>10}  scope")
                out.append("-" * 70)
                for sch, t, n, sc in tbls:
                    rows = "  (skip)" if sc == "skip" else f"{n:>10,}"
                    tag = {"all": "all rows",
                           "inventory": "catalog rows only",
                           "skip": "no INVENTORY_ID"}[sc]
                    out.append(f"{sch + '.' + t:46} {rows}  {tag}")
                out.append("-" * 70)
                out.append(f"{'TOTAL rows to delete':46} {total:>10,}")
                raw.rollback()
            except Exception as e:
                out.append(f"ERROR: {e}")
            finally:
                try:
                    raw.close()
                except Exception:
                    pass
            st.code("\n".join(out) or "(nothing)")

        if run_clear:
            from dataview.file_catalog import clear_catalog as _cc
            raw = engine.raw_connection()
            out, err = [], None
            try:
                cur = raw.cursor()
                cur.execute(_cc._SET_OPTS)
                tbls = _cc.gather(cur, do_dv=x_dv, keep=[])
                _cc.clear(cur, tbls, out.append)
                raw.commit()
            except Exception as e:
                try:
                    raw.rollback()
                except Exception:
                    pass
                err = f"{type(e).__name__}: {e}"
            finally:
                try:
                    raw.close()
                except Exception:
                    pass
            if not err and x_vault:
                try:
                    _cc.clear_vault(x_vroot.strip(), True, out.append)
                except Exception as e:
                    out.append(f"vault delete failed: {e}")
            if err:
                st.error(err)
            else:
                st.success("Cleared.")
            st.code("\n".join(out) or "(no output)")


_REVIEW_PAGE = 200   # cap rows rendered at once (text_inputs are cheap but not free)



def _pipeline_clear(engine, dialect):
    """Destructive maintenance — empty the file catalog and remove only the
    catalog-promoted rows from the dv_* tables (INVENTORY_ID-scoped). Bulk- or
    hand-loaded rows and every reference / spatial table are left intact. Files
    on disk are never touched."""
    st.markdown("##### \U0001f9f9 Clear catalog & data rows")
    st.caption(
        "Empties the file catalog (GLOBAL_FILE_CATALOG, headers, cat_*) and the "
        "deep las_catalog tables, and removes only the catalog-promoted rows from "
        "the dv_* tables. Bulk-loaded data and all reference / spatial tables are "
        "left intact. Files on disk are never touched."
    )
    from dataview.file_catalog import clear_catalog as _cc

    if st.button("Preview what would be cleared", key="clr_preview"):
        try:
            raw = engine.raw_connection(); cur = raw.cursor()
            rows = _cc.gather(cur, do_dv=True, keep=set())
            raw.close()
            import pandas as _pd
            df = _pd.DataFrame(
                [(f"{s}.{t}", "" if n < 0 else f"{n:,}", scope)
                 for s, t, n, scope in rows],
                columns=["table", "rows", "scope"])
            st.dataframe(df, hide_index=True, use_container_width=True)
        except Exception as e:
            st.error(f"{type(e).__name__}: {e}")

    cc1, cc2 = st.columns([3, 1])
    confirm = cc1.checkbox(
        "Yes, clear the catalog and catalog-derived dv_* rows", key="clr_ok")
    if cc2.button("Clear now", type="primary", key="clr_run",
                  use_container_width=True, disabled=not confirm):
        _box, _lines = st.empty(), []
        def _log(m):
            _lines.append(str(m)); _box.code("\n".join(_lines[-400:]))
        try:
            raw = engine.raw_connection(); cur = raw.cursor()
            cur.execute(_cc._SET_OPTS)          # required for dv_well spatial DML
            rows = _cc.gather(cur, do_dv=True, keep=set())
            _cc.clear(cur, rows, _log)
            # reset capture stamps on clear: clearing cat_*/dv_* leaves CAPTURED_HASH
            # stamped, which makes the next crawl's capture skip these files as
            # "already captured" so they never re-capture. Null the stamps so a
            # cleared catalog truly re-processes every file.
            try:
                _rc = cur.execute(
                    "UPDATE file_catalog.GLOBAL_FILE_CATALOG "
                    "SET CAPTURED_HASH = NULL, VAULTED_AT = NULL, PROMOTED_AT = NULL "
                    "WHERE CAPTURED_HASH IS NOT NULL OR VAULTED_AT IS NOT NULL "
                    "OR PROMOTED_AT IS NOT NULL").rowcount
                _log(f"reset capture/vault/promote stamps on {_rc or 0} catalog row(s)")
            except Exception as _se:
                _log(f"(stamp reset skipped: {str(_se)[:80]})")
            raw.commit(); raw.close()
            st.success("Catalog and catalog-derived dv_* rows cleared "
                       "(capture stamps reset — files will re-capture on next run).")
        except Exception as e:
            st.error(f"Clear failed: {type(e).__name__}: {e}")


def _well_key_grid(engine):
    from sqlalchemy import text as _t
    from dataview.core import path_identity as _pi

    with engine.connect() as con:
        has = con.execute(_t(
            "SELECT 1 FROM sys.columns WHERE name='UWI14' "
            "AND object_id=OBJECT_ID('file_catalog.FILE_WELL_HEADER')")).fetchone()
        if not has:
            st.info("No UWI14 column yet — run ① Enrich Headers first.")
            return
        rows = con.execute(_t("""
            SELECT h.WELL_HEADER_ID AS id, h.INVENTORY_ID AS inv,
                   g.FILE_NAME AS fname, g.FILE_PATH AS path,
                   h.WELL_NAME AS well_name, h.UWI AS internal_uwi,
                   h.OPERATOR AS operator, h.TOTAL_DEPTH AS td, h.SPUD_DATE AS spud
            FROM file_catalog.FILE_WELL_HEADER h
            JOIN file_catalog.GLOBAL_FILE_CATALOG g
                   ON g.INVENTORY_ID = h.INVENTORY_ID
            WHERE h.UWI14 IS NULL OR h.UWI14 = '00000000000000'
            ORDER BY g.FILE_NAME""")).fetchall()

    if not rows:
        st.success("Every well header already has a valid UWI14 — nothing to key.")
        return

    total = len(rows)
    rows = rows[:_REVIEW_PAGE]
    if total > _REVIEW_PAGE:
        st.caption(f"{total} files need a UWI — showing the first {_REVIEW_PAGE}. "
                   "Save these, then the next batch appears.")
    else:
        st.caption(f"{total} file(s) need a UWI. Edit the value, then Save.")

    # ---- match well names against the reference, corroborated by total
    #      depth / spud date / operator, so a shared name alone never
    #      auto-fills the wrong well ---------------------------------------- #
    from sqlalchemy import bindparam
    REF = _wb_ref()

    def _norm_op(s):
        return "".join(ch for ch in (s or "").upper() if ch.isalnum())

    def _td_close(a, b, tol=50.0):
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            return False
        if a <= 0 or b <= 0:
            return False
        return abs(a - b) <= max(tol, 0.01 * max(a, b))

    def _same_spud(a, b):
        if not a or not b:
            return False
        try:
            da = a.date() if hasattr(a, "date") else a
            db = b.date() if hasattr(b, "date") else b
            return da == db
        except Exception:
            return False

    st.caption("Both **Well name** and **UWI** are editable guesses. Fix them, "
               "then **Match against reference** to look each row up — by UWI if "
               "you gave one, otherwise by name — and see what the reference "
               "returns. **Save** writes the UWI to the file's header.")

    # A confirmed UWI from a prior Match is promoted into the UWI field here,
    # before the widget is created (Streamlit forbids setting a widget's state
    # after it's been instantiated in the same run).
    _pending = st.session_state.pop("_pl_well_fill", {})

    h1, h2, h3, h4 = st.columns([3, 2, 2, 3])
    h1.markdown("**File**")
    h2.markdown("**Well name (guess)**")
    h3.markdown("**UWI (guess)**")
    h4.markdown("**Match result**")

    inputs = []   # (id, name_key, uwi_key, result_key, row)
    for r in rows:
        nkey = f"pl_wn_{r.id}"
        ukey = f"pl_wu_{r.id}"
        rkey = f"pl_wr_{r.id}"
        if nkey not in st.session_state:
            st.session_state[nkey] = (r.well_name or "").strip()
        if ukey not in st.session_state:
            st.session_state[ukey] = (_pi.norm_uwi14(r.internal_uwi or "")
                                      or (_pi.uwi14_from_path(r.path or "")[0] or ""))
        if r.id in _pending:
            st.session_state[ukey] = _pending[r.id]   # promote confirmed UWI
        c1, c2, c3, c4 = st.columns([3, 2, 2, 3])
        c1.write(r.fname or _pi._basename(r.path or "") or f"(inventory {r.inv})")
        c2.text_input("name", key=nkey, label_visibility="collapsed",
                      placeholder="well name")
        c3.text_input("uwi", key=ukey, label_visibility="collapsed",
                      placeholder="UWI / API")
        c4.write(st.session_state.get(rkey, "—"))
        inputs.append((r.id, nkey, ukey, rkey, r))

    b1, b2 = st.columns(2)
    do_match = b1.button("🔍 Match against reference", key="pl_well_match",
                         use_container_width=True)
    do_save = b2.button("💾 Save UWIs", type="primary", key="pl_well_save",
                        use_container_width=True)

    if do_match:
        cur = [(rid, nkey, ukey, rkey, r,
                st.session_state.get(nkey, "").strip(),
                _pi.norm_uwi14(st.session_state.get(ukey, "")))
               for rid, nkey, ukey, rkey, r in inputs]
        name_set = sorted({nm for *_x, nm, _u in cur if nm})
        uwi_set = sorted({u for *_x, _nm, u in cur if u})

        cols = ("WELL_NAME, UWI14, UWI, API_NUM, OPERATOR_NAME, "
                "TOTAL_DEPTH, SPUD_DATE, COUNTY, PROVINCE_STATE")
        name_map, uwi_map, ok = {}, {}, True
        try:
            with engine.connect() as con:
                if name_set:
                    qn = _t(f"SELECT {cols} FROM {REF} WHERE WELL_NAME IN :v"
                            ).bindparams(bindparam("v", expanding=True))
                    for m in con.execute(qn, {"v": name_set}).fetchall():
                        name_map.setdefault((m.WELL_NAME or "").strip().upper(),
                                            []).append(m)
                if uwi_set:
                    qu = _t(f"SELECT {cols} FROM {REF} WHERE UWI14 IN :v"
                            ).bindparams(bindparam("v", expanding=True))
                    for m in con.execute(qu, {"v": uwi_set}).fetchall():
                        uwi_map[(m.UWI14 or "").strip()] = m
        except Exception as e:
            st.error(f"Reference lookup failed: {type(e).__name__}: {e}")
            ok = False

        if ok:
            fills = {}
            for rid, nkey, ukey, rkey, r, nm, uw in cur:
                txt, fill = "enter a name or UWI", ""
                if uw and uw in uwi_map:
                    m = uwi_map[uw]
                    loc = " · ".join(x for x in (m.COUNTY, m.PROVINCE_STATE) if x)
                    txt = (f"✓ UWI in reference · {m.WELL_NAME or ''}"
                           + (f" ({loc})" if loc else ""))
                    fill = uw
                elif uw:
                    txt = "✗ UWI not found in reference"
                elif nm:
                    cands = name_map.get(nm.upper(), [])
                    scored = []
                    for m in cands:
                        sigs = []
                        if _td_close(r.td, m.TOTAL_DEPTH):
                            sigs.append("TD")
                        if _same_spud(r.spud, m.SPUD_DATE):
                            sigs.append("spud")
                        if r.operator and _norm_op(r.operator) == _norm_op(m.OPERATOR_NAME):
                            sigs.append("oper")
                        scored.append((len(sigs), sigs, m))
                    scored.sort(key=lambda x: x[0], reverse=True)
                    if not scored:
                        txt = "✗ name not found in reference"
                    else:
                        top_n, top_sigs, top_m = scored[0]
                        tie = len(scored) > 1 and scored[1][0] == top_n
                        u14 = top_m.UWI14 or top_m.UWI or top_m.API_NUM
                        loc = " · ".join(x for x in (top_m.COUNTY, top_m.PROVINCE_STATE) if x)
                        if top_n >= 1 and not tie:
                            txt = (f"✓ {u14} · name+{'+'.join(top_sigs)}"
                                   + (f" ({loc})" if loc else ""))
                            fill = _pi.norm_uwi14(u14 or "")
                        elif top_n >= 1 and tie:
                            txt = f"⚠ {len(scored)} wells match name+attrs — verify"
                        elif len(cands) == 1:
                            txt = (f"? {u14} · name only — verify"
                                   + (f" ({loc})" if loc else ""))
                        else:
                            txt = f"⚠ {len(cands)} share this name — verify"
                st.session_state[rkey] = txt
                if fill:
                    fills[rid] = fill
            st.session_state["_pl_well_fill"] = fills
            st.rerun()

    if do_save:
        ups = []
        for rid, nkey, ukey, rkey, r in inputs:
            u = _pi.norm_uwi14(st.session_state.get(ukey, ""))
            if u:
                ups.append({"id": rid, "u": u})
        if not ups:
            st.warning("No valid 10–14 digit UWIs to write.")
        else:
            with engine.begin() as con:
                for up in ups:
                    con.execute(_t("UPDATE file_catalog.FILE_WELL_HEADER "
                                   "SET UWI=:u, UWI14=:u WHERE WELL_HEADER_ID=:id"), up)
            st.success(f"Wrote {len(ups)} UWI(s). They'll flow into vault/enrich next run.")
            st.rerun()


def _seis_survey_grid(engine):
    from sqlalchemy import text as _t
    from dataview.core import path_identity as _pi

    with engine.connect() as con:
        rows = con.execute(_t("""
            SELECT sh.SEIS_HEADER_ID AS id, sh.INVENTORY_ID AS inv,
                   g.FILE_NAME AS fname, g.FILE_PATH AS path,
                   sh.SURVEY_NAME AS survey
            FROM file_catalog.FILE_SEIS_HEADER sh
            JOIN file_catalog.GLOBAL_FILE_CATALOG g
                   ON g.INVENTORY_ID = sh.INVENTORY_ID
            WHERE sh.SURVEY_NAME IS NULL OR LTRIM(RTRIM(sh.SURVEY_NAME)) = ''
            ORDER BY g.FILE_NAME""")).fetchall()

    if not rows:
        st.success("Every seismic header already has a survey name.")
        return

    total = len(rows)
    rows = rows[:_REVIEW_PAGE]
    if total > _REVIEW_PAGE:
        st.caption(f"{total} files need a survey name — showing the first "
                   f"{_REVIEW_PAGE}. Save these, then the next batch appears.")
    else:
        st.caption(f"{total} file(s) need a survey name. Edit the value, then Save.")

    h1, h2, h3, h4 = st.columns([3, 2, 2, 2])
    h1.markdown("**File**")
    h2.markdown("**Current**")
    h3.markdown("**Guess (from path)**")
    h4.markdown("**Assign survey**")

    inputs = []  # (header_id, widget_key)
    for r in rows:
        g = _pi.survey_from_path(r.path or "") or ""
        key = f"pl_seis_{r.id}"
        if key not in st.session_state:
            st.session_state[key] = g
        c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
        c1.write(r.fname or _pi._basename(r.path or "") or f"(inventory {r.inv})")
        c2.write(r.survey or "—")
        c3.write(g or "—")
        c4.text_input("assign survey", key=key, label_visibility="collapsed",
                      placeholder="survey name")
        inputs.append((r.id, key))

    if st.button("💾 Save survey names", type="primary", key="pl_seis_save"):
        ups = []
        for rid, key in inputs:
            v = str(st.session_state.get(key, "") or "").strip()
            if v:
                ups.append({"id": rid, "v": v})
        if not ups:
            st.warning("No survey names to write.")
        else:
            with engine.begin() as con:
                for up in ups:
                    con.execute(_t("UPDATE file_catalog.FILE_SEIS_HEADER "
                                   "SET SURVEY_NAME=:v WHERE SEIS_HEADER_ID=:id"), up)
            st.success(f"Wrote {len(ups)} survey name(s).")
            st.rerun()
