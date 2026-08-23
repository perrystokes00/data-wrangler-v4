"""
page_file_catalog.py
====================
File Catalog -- all file types, catalog-first architecture.

Tabs:
  1. Scan      -- Phase 1 fast scan (instant) + Phase 2 background enrichment
  2. Header File -- export flat CSV of all header fields for well creation
  3. Manage    -- QC workspace: browse, view, extract, delete
  4. Extract & Load -- batch extract + load by dataset type
"""
import os
import re
import streamlit as st
from pathlib import Path

# ── Optional module imports ───────────────────────────────────────────────────

def _try_import(mod):
    try:
        return __import__(mod, fromlist=[""])
    except ImportError:
        return None

# ── Extension sets ────────────────────────────────────────────────────────────

PDF_EXTS    = {".pdf"}
SHP_EXTS    = {".shp", ".geojson", ".gpkg", ".kml", ".kmz"}
LOG_EXTS    = {".las", ".lis", ".dlis", ".dlf", ".dis"}
SEIS_EXTS   = {".segy", ".sgy", ".seg", ".p190", ".p90"}
OFFICE_EXTS = {".xlsx", ".xls", ".xlsm", ".docx", ".doc", ".csv", ".tsv"}
IMAGE_EXTS  = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}

ALL_PETROLEUM_EXTS = (
    PDF_EXTS | SHP_EXTS | LOG_EXTS | SEIS_EXTS | OFFICE_EXTS | IMAGE_EXTS
)

EXT_GROUP = {}
for e in PDF_EXTS:    EXT_GROUP[e] = "PDF"
for e in SHP_EXTS:    EXT_GROUP[e] = "Shapefile"
for e in LOG_EXTS:    EXT_GROUP[e] = "Well Log"
for e in SEIS_EXTS:   EXT_GROUP[e] = "Seismic"
for e in OFFICE_EXTS: EXT_GROUP[e] = "Office"
for e in IMAGE_EXTS:  EXT_GROUP[e] = "Image"

# ── Report type labels ────────────────────────────────────────────────────────

RT_LABELS = {
    "DIRECTIONAL_SURVEY": "Directional Survey",
    "FORMATION_TOPS":      "Formation Tops",
    "END_OF_WELL":         "End of Well Report",
    "WELL_TEST":           "Well Test",
    "RFT_MDT":             "RFT / MDT",
    "SCOUT_TICKET":        "Scout Ticket",
    "DAILY_DRILLING_REPORT":"Daily Drilling Report",
    "PETROPHYSICAL":       "Petrophysical",
    "CASING_CEMENTING":    "Casing & Cementing",
    "MUD_LOG":             "Mud Log",
    "COMPLETION_REPORT":   "Completion Report",
    "CORE":                "Core Data",
    "UNKNOWN":             "Unknown",
}

# Enrichment chunk size per rerun cycle
ENRICH_CHUNK = 20


# =============================================================================
# Entry point
# =============================================================================

def run(engine=None, dialect: str = "mssql"):
    st.title("🗂️ File Catalog")
    st.caption(
        "Scan & catalog everything first. "
        "QC in Manage. Create wells from Header File. Load in Extract & Load."
    )

    if engine is None:
        st.warning("No database connection — connect first.")
        return

    # Pre-select default tab based on how we were called from nav
    # page_file_manager sets fc_default_tab before importing us
    _tab_map = {
        "scan":    0,
        "manage":  2,
        "header":  1,
        "extract": 3,
    }
    _default_tab = _tab_map.get(
        st.session_state.pop("fc_default_tab", None), 2
    )  # default to Manage

    tabs = st.tabs([
        "🔍 Scan",
        "📋 Header File",
        "🛠️ Manage",
        "🚀 Extract & Load",
        "🧩 Pipeline",
    ])

    with tabs[0]: _tab_scan(engine, dialect)
    with tabs[1]: _tab_header_file(engine, dialect)
    with tabs[2]: _tab_manage(engine, dialect)
    with tabs[3]: _tab_extract_load(engine, dialect)
    with tabs[4]: _tab_pipeline(engine, dialect)


# =============================================================================
# Tab 1 -- Scan
# =============================================================================

def _tab_scan(engine, dialect):
    from sqlalchemy import text as _t
    import pandas as pd
    from datetime import datetime, timezone

    st.markdown("#### 🔍 Scan & Catalog")
    st.caption(
        "**Phase 1** — fast file system walk, bulk insert to catalog. "
        "**Phase 2** — background classification and header extraction."
    )

    # ── Config ────────────────────────────────────────────────────────────────
    scan_path = st.text_input(
        "Root folder to scan",
        placeholder=r"\\server\share\WellData",
        key="fc_scan_path",
    )

    ext_groups = st.multiselect(
        "File types",
        ["PDF", "Shapefile", "Well Log", "Seismic", "Office", "Image"],
        default=["PDF", "Shapefile", "Well Log", "Seismic", "Office"],
        key="fc_scan_exts",
    )

    _exts = set()
    for grp in ext_groups:
        for e, g in EXT_GROUP.items():
            if g == grp:
                _exts.add(e)

    col1, col2, col3 = st.columns(3)

    # ── Phase 1: Fast scan ───────────────────────────────────────────────────
    if col1.button("🔍 Phase 1 — Fast Scan", type="primary",
                   key="fc_scan_p1", use_container_width=True):
        if not scan_path:
            st.error("Enter a folder path.")
        elif not Path(scan_path).exists():
            st.error(f"Folder not found: `{scan_path}`")
        else:
            _run_phase1(engine, dialect, scan_path, _exts)

    # ── Phase 2: Enrich ──────────────────────────────────────────────────────
    if col2.button("⚙️ Phase 2 — Enrich", type="secondary",
                   key="fc_scan_p2", use_container_width=True):
        st.session_state["fc_enriching"] = True
        st.session_state["fc_enrich_offset"] = 0

    if col3.button("⏹ Stop Enrichment", key="fc_scan_stop",
                   use_container_width=True):
        st.session_state["fc_enriching"] = False

    # Phase 2 thread count — small UI to tune parallelism. Default 8 works
    # for mixed extraction (PDF/DLIS/Office). Drop to 2-4 for DLIS-heavy
    # batches that load big chunks into memory; raise to 16 for many
    # small files.
    _w = st.slider(
        "Phase 2 threads",
        min_value=1, max_value=16,
        value=int(st.session_state.get("fc_phase2_workers", 8)),
        key="fc_phase2_workers_slider",
        help="Files per chunk extracted in parallel. Lower for DLIS-heavy "
             "batches; higher for many small files. Default 8.",
    )
    st.session_state["fc_phase2_workers"] = _w

    # ── Phase 2 chunked loop ──────────────────────────────────────────────────
    if st.session_state.get("fc_enriching"):
        _run_phase2_chunk(engine, dialect)

    # ── Catalog summary ───────────────────────────────────────────────────────
    st.divider()
    st.markdown("**Catalog status**")
    try:
        with engine.connect() as con:
            # Path B: EXTRACTION_STATUS replaces CATALOG_READINESS.
            # Buckets are operational (did extraction work?), not workflow
            # (READY/REVIEW/NEEDS_UWI/ATTENTION which were Path A bands).
            counts = con.execute(_t("""
                SELECT
                    FILE_TYPE_GROUP,
                    COUNT(*)                                                 AS total,
                    SUM(CASE WHEN HEADER_EXTRACTED='Y'      THEN 1 ELSE 0 END) AS enriched,
                    SUM(CASE WHEN EXTRACTION_STATUS='SUCCESS' THEN 1 ELSE 0 END) AS success,
                    SUM(CASE WHEN EXTRACTION_STATUS='PARTIAL' THEN 1 ELSE 0 END) AS partial,
                    SUM(CASE WHEN EXTRACTION_STATUS='EMPTY'   THEN 1 ELSE 0 END) AS empty_,
                    SUM(CASE WHEN EXTRACTION_STATUS='FAILED'  THEN 1 ELSE 0 END) AS failed
                FROM file_catalog.GLOBAL_FILE_CATALOG
                GROUP BY FILE_TYPE_GROUP
                ORDER BY total DESC
            """)).fetchall()
        if counts:
            df = pd.DataFrame(counts,
                columns=["Type","Total","Enriched","Success","Partial","Empty","Failed"])
            st.dataframe(df, hide_index=True, use_container_width=True)
            total = sum(r[1] for r in counts)
            enriched = sum(r[2] for r in counts)
            m1,m2,m3 = st.columns(3)
            m1.metric("Total cataloged", f"{total:,}")
            m2.metric("Enriched",        f"{enriched:,}")
            m3.metric("Pending enrichment", f"{total-enriched:,}")
        else:
            st.info("Catalog is empty — run Phase 1 to scan.")
    except Exception as e:
        st.caption(f"Catalog query failed: {e}")


def _run_phase1(engine, dialect, root: str, exts: set):
    """Phase 1: os.scandir walk + bulk insert to GLOBAL_FILE_CATALOG."""
    import csv, uuid, tempfile
    from datetime import datetime, timezone
    from sqlalchemy import text as _t

    st.info("Phase 1 scanning...")
    prog = st.progress(0.0, text="Walking file system...")

    # Walk
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
                                st_res = entry.stat()
                                found.append((
                                    entry.path,
                                    entry.name,
                                    ext,
                                    round(st_res.st_size / 1024, 2),
                                    datetime.fromtimestamp(
                                        st_res.st_mtime, tz=timezone.utc
                                    ).strftime("%Y-%m-%d %H:%M:%S"),
                                    EXT_GROUP.get(ext, "Other"),
                                    root,
                                ))
                    except OSError:
                        pass
        except (PermissionError, OSError):
            pass
        if folders % 2000 == 0:
            prog.progress(0.3, text=f"Walking... {folders:,} folders, {len(found):,} files")

    prog.progress(0.5, text=f"Found {len(found):,} files — writing to catalog...")

    if not found:
        st.warning("No files found matching selected extensions.")
        return

    # Write CSV
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False,
        newline="", encoding="utf-8"
    )
    csv_path = tmp.name
    # NO escapechar — it doubled every separator in a Windows path and BULK
    # INSERT stored the doubled form. Worse here than in file_inventory: the id
    # is hashed from the CLEAN fpath while the escaped one was written, so the
    # row's INVENTORY_ID and its FILE_PATH described different strings.
    from dataview.core.path_identity import bulk_csv_writer, bulk_field
    writer = bulk_csv_writer(tmp)
    n_sanitised = 0
    for (fpath, fname, fext, size_kb, mod_dt, grp, rpath) in found:
        inv_id = _make_id(fpath)
        row = []
        for v in (inv_id, fpath[:900], fname[:260], fext[:20],
                  grp[:50], size_kb if size_kb else "",
                  "", "", "UNCATALOGED", "",
                  rpath[:900], now, now, now):
            val, changed = bulk_field(v)
            row.append(val)
            n_sanitised += bool(changed)
        writer.writerow(row)
    if n_sanitised:
        st.warning(f"{n_sanitised} field(s) contained a tab, quote or newline "
                   f"and were rewritten to load — the stored value differs "
                   f"from the value on disk.")
    tmp.close()

    prog.progress(0.7, text="Bulk inserting to catalog...")

    try:
        with engine.begin() as con:
            # Staging table
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
                WITH (
                    FIELDTERMINATOR = '\\t',
                    ROWTERMINATOR   = '0x0D0A',
                    CODEPAGE        = '65001',
                    FIRSTROW        = 1,
                    TABLOCK
                );
            """))

            # Merge into catalog
            con.execute(_t("""
                MERGE file_catalog.GLOBAL_FILE_CATALOG AS tgt
                USING file_catalog.fc_stage AS src
                ON tgt.INVENTORY_ID = src.INVENTORY_ID
                WHEN MATCHED THEN UPDATE SET
                    FILE_SIZE_KB     = TRY_CAST(src.FILE_SIZE_KB AS DECIMAL(15,2)),
                    SCAN_DATE        = TRY_CAST(src.SCAN_DATE AS DATETIME2),
                    ROW_CHANGED_DATE = TRY_CAST(src.ROW_CHANGED_DATE AS DATETIME2)
                WHEN NOT MATCHED THEN INSERT (
                    INVENTORY_ID, FILE_PATH, FILE_NAME, FILE_EXT,
                    FILE_TYPE_GROUP, FILE_SIZE_KB, FILE_HASH,
                    DUPLICATE_GROUP, CATALOG_STATUS, CATALOG_TABLE,
                    ROOT_PATH, SCAN_DATE, ROW_CREATED_DATE, ROW_CHANGED_DATE
                ) VALUES (
                    src.INVENTORY_ID, src.FILE_PATH, src.FILE_NAME, src.FILE_EXT,
                    src.FILE_TYPE_GROUP,
                    TRY_CAST(src.FILE_SIZE_KB AS DECIMAL(15,2)),
                    src.FILE_HASH, src.DUPLICATE_GROUP, src.CATALOG_STATUS,
                    src.CATALOG_TABLE, src.ROOT_PATH,
                    TRY_CAST(src.SCAN_DATE AS DATETIME2),
                    TRY_CAST(src.ROW_CREATED_DATE AS DATETIME2),
                    TRY_CAST(src.ROW_CHANGED_DATE AS DATETIME2)
                );
            """))

            con.execute(_t("DROP TABLE IF EXISTS file_catalog.fc_stage;"))

        prog.progress(1.0, text="Done.")
        st.success(
            f"✅ Phase 1 complete — {len(found):,} files across "
            f"{folders:,} folders cataloged."
        )
        st.caption("Click **Phase 2 — Enrich** to extract headers and score files.")

    except Exception as e:
        st.error(f"Bulk insert failed: {e}")
    finally:
        try:
            os.unlink(csv_path)
        except Exception:
            pass


def _run_phase2_chunk(engine, dialect):
    """
    Process ENRICH_CHUNK files per Streamlit rerun.
    Writes classification + header fields back to GLOBAL_FILE_CATALOG.

    Extraction within each chunk runs in parallel across PHASE2_WORKERS
    threads. DB writes stay sequential in the main thread — single-row
    UPDATEs are microseconds each, so the bottleneck is file parsing
    (PDFs/DLIS can take seconds), which is what we parallelize.

    The chunked-rerun pattern stays in place: each chunk finishes, Streamlit
    reruns, the next chunk starts. This keeps the UI responsive (you can hit
    Stop between chunks) without blocking on the whole catalog in one go.
    """
    from sqlalchemy import text as _t
    from concurrent.futures import ThreadPoolExecutor, as_completed

    offset = st.session_state.get("fc_enrich_offset", 0)

    # Phase 2 worker count. Default 8 — balanced for mixed extraction
    # (PDF/DLIS/Office). Read from session state so a future UI toggle
    # could change it without code edits.
    PHASE2_WORKERS = int(st.session_state.get("fc_phase2_workers", 8))

    try:
        with engine.connect() as con:
            total_pending = con.execute(_t("""
                SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG
                WHERE HEADER_EXTRACTED IS NULL OR HEADER_EXTRACTED = 'N'
            """)).scalar() or 0

            rows = con.execute(_t(f"""
                SELECT TOP {ENRICH_CHUNK}
                    INVENTORY_ID, FILE_PATH, FILE_EXT
                FROM file_catalog.GLOBAL_FILE_CATALOG
                WHERE HEADER_EXTRACTED IS NULL OR HEADER_EXTRACTED = 'N'
                ORDER BY SCAN_DATE DESC
            """)).fetchall()
    except Exception as e:
        st.error(f"Enrichment query failed: {e}")
        st.session_state["fc_enriching"] = False
        return

    if not rows:
        st.success("✅ Phase 2 complete — all files enriched.")
        st.session_state["fc_enriching"] = False
        return

    # ── Parallel extraction within this chunk ───────────────────────────
    # Each worker handles one file: read it, extract fields, return result.
    # Workers don't touch the DB — that happens in the main thread below.
    # _extract_fields catches its own exceptions and always returns a dict,
    # so we don't need a worker-level try/except around the call (but we
    # add one anyway for paranoia).
    def _worker(row):
        inv_id, fpath, fext = row
        try:
            fields = _extract_fields(fpath, fext)
            return ("ok", inv_id, fields, None)
        except Exception as e:
            return ("err", inv_id, None, f"{type(e).__name__}: {e}")

    results = []
    with ThreadPoolExecutor(max_workers=PHASE2_WORKERS) as pool:
        # Submit all chunk rows, collect results as they complete (order
        # doesn't matter — we're about to UPDATE them one at a time anyway).
        futures = [pool.submit(_worker, row) for row in rows]
        for fut in as_completed(futures):
            try:
                results.append(fut.result(timeout=300))  # 5 min/file ceiling
            except Exception as e:
                # Timeout or unexpected worker death — record as error.
                # We can't recover the row from the future, so we lose the
                # inventory_id for this specific failure; the next chunk
                # will pick it up again (HEADER_EXTRACTED still NULL).
                results.append(("err", None, None, f"worker died: {e}"))

    # ── Sequential DB writes in main thread ─────────────────────────────
    # Single UPDATE per file, microseconds each. Serializing this avoids
    # SQLAlchemy connection-pool contention from N workers writing at once.
    done_this_chunk = 0
    for outcome, inv_id, fields, err in results:
        if outcome == "ok" and inv_id is not None:
            try:
                _write_enrichment(engine, inv_id, fields, dialect)
                done_this_chunk += 1
            except Exception:
                # Write failed — mark the row as errored so we don't retry
                # forever on it.
                try:
                    with engine.begin() as con:
                        con.execute(_t("""
                            UPDATE file_catalog.GLOBAL_FILE_CATALOG
                            SET HEADER_EXTRACTED  = 'E',
                                EXTRACTION_STATUS = 'FAILED',
                                ROW_CHANGED_DATE  = GETUTCDATE()
                            WHERE INVENTORY_ID = :id
                        """), {"id": inv_id})
                except Exception:
                    pass
        elif inv_id is not None:
            # Extraction errored — mark as attempted so we don't retry forever
            try:
                with engine.begin() as con:
                    con.execute(_t("""
                        UPDATE file_catalog.GLOBAL_FILE_CATALOG
                        SET HEADER_EXTRACTED  = 'E',
                            EXTRACTION_STATUS = 'FAILED',
                            ROW_CHANGED_DATE  = GETUTCDATE()
                        WHERE INVENTORY_ID = :id
                    """), {"id": inv_id})
            except Exception:
                pass

    st.session_state["fc_enrich_offset"] = offset + done_this_chunk
    processed = offset + done_this_chunk
    pct = min(1.0, processed / max(total_pending, 1))

    st.progress(pct,
        text=f"Enriching... {processed:,} / {total_pending:,} "
             f"({done_this_chunk} this pass · {PHASE2_WORKERS} threads)")

    if rows and len(rows) == ENRICH_CHUNK:
        st.rerun()
    else:
        st.success("✅ Phase 2 complete.")
        st.session_state["fc_enriching"] = False


def _extract_fields(fpath: str, fext: str) -> dict:
    """Extract header fields from a file. Returns a flat dict."""
    fields = {
        "report_type": "UNKNOWN",
        "uwi": None, "well_name": None, "operator": None,
        "field": None, "state": None, "county": None,
        "latitude": None, "longitude": None,
        "total_depth": None, "spud_date": None,
        "survey_type": None, "contractor": None,
        "feature_type": None, "ppdm_target": None,
        "confidence": 0.0,
    }
    ext = fext.lower()

    try:
        if ext == ".pdf":
            try:
                from dataview.file_catalog.pdf_survey_catalog import classify_pdf
                cl = classify_pdf(fpath)
                fields.update({k: cl.get(k) for k in fields if k in cl})
                fields["report_type"] = cl.get("report_type", "UNKNOWN")
            except Exception:
                pass

        elif ext in SHP_EXTS:
            try:
                from dataview.mapping.shapefile_catalog import classify_shapefile
                cl = classify_shapefile(fpath)
                fields["feature_type"] = cl.get("feature_type")
                fields["ppdm_target"]  = cl.get("ppdm_target")
                fields["confidence"]   = cl.get("confidence", 0.0)
                fields["report_type"]  = "SHAPEFILE"
            except Exception:
                pass

        elif ext in LOG_EXTS:
            try:
                from dataview.file_catalog.file_summarizer import summarize
                s = summarize(fpath)
                kf = s.get("key_fields", {})
                fields["uwi"]         = s.get("uwi")
                fields["well_name"]   = s.get("well_name")
                fields["operator"]    = kf.get("company")
                fields["total_depth"] = kf.get("depth_stop")
                fields["report_type"] = "WELL_LOG"
            except Exception:
                pass

        elif ext in SEIS_EXTS:
            fields["report_type"] = "SEISMIC"

        elif ext in OFFICE_EXTS:
            try:
                from dataview.file_catalog.file_summarizer import summarize
                s = summarize(fpath)
                fields["uwi"]       = s.get("uwi")
                fields["well_name"] = s.get("well_name")
                fields["ppdm_target"] = ", ".join(s.get("ppdm_hints", []))
                fields["report_type"] = "OFFICE"
            except Exception:
                pass

    except Exception:
        pass

    return fields


def _write_enrichment(engine, inv_id: str, fields: dict, dialect: str):
    """Write extracted fields back to GLOBAL_FILE_CATALOG."""
    from sqlalchemy import text as _t

    # Path B: write extraction status only — score is gone, detailed
    # extracted fields belong in FILE_WELL_HEADER (handled elsewhere).
    status = _infer_extraction_status(fields)

    with engine.begin() as con:
        con.execute(_t("""
            UPDATE file_catalog.GLOBAL_FILE_CATALOG SET
                EXTRACTION_STATUS = :status,
                HEADER_EXTRACTED  = 'Y',
                ROW_CHANGED_DATE  = SYSUTCDATETIME()
            WHERE INVENTORY_ID = :id
        """), {
            "status": status,
            "id":     inv_id,
        })


def _infer_extraction_status(fields: dict) -> str:
    """
    Infer EXTRACTION_STATUS from the extracted-fields dict.

    SUCCESS — identifying field (UWI or well_name) AND metadata
              (operator or lat/lon) both present
    PARTIAL — has one but not the other
    EMPTY   — extraction returned no useful identifying or metadata
              fields at all
    """
    uwi      = fields.get("uwi")
    name     = fields.get("well_name")
    op       = fields.get("operator")
    lat      = fields.get("latitude")
    lon      = fields.get("longitude")
    has_id   = bool(uwi or name)
    has_meta = bool(op or (lat and lon))
    if has_id and has_meta:
        return "SUCCESS"
    if has_id or has_meta:
        return "PARTIAL"
    return "EMPTY"


def _issues(fields: dict) -> list:
    issues = []
    if not fields.get("uwi"):       issues.append("No UWI")
    if not fields.get("well_name"): issues.append("No well name")
    if not fields.get("latitude") or not fields.get("longitude"):
        issues.append("No coordinates")
    return issues


def _make_id(fpath: str) -> str:
    import hashlib
    return hashlib.sha1(fpath.upper().encode("utf-8")).hexdigest().upper()


# =============================================================================
# Tab 2 -- Header File
# =============================================================================

def _tab_header_file(engine, dialect):
    import pandas as pd
    from sqlalchemy import text as _t

    st.markdown("#### 📋 Header Flat File")
    st.caption(
        "Query the catalog and export a consolidated header CSV "
        "for well creation and review."
    )

    # Filters
    f1, f2, f3 = st.columns(3)
    type_filter = f1.multiselect(
        "Report type", list(RT_LABELS.keys()),
        format_func=lambda x: RT_LABELS.get(x, x),
        key="hf_type_filter",
    )
    readiness_filter = f2.multiselect(
        "Readiness",
        ["READY", "REVIEW", "NEEDS_UWI", "ATTENTION"],
        key="hf_readiness_filter",
    )
    has_uwi = f3.checkbox("Has UWI only", key="hf_has_uwi")

    if st.button("🔍 Query Catalog", type="primary", key="hf_query"):
        try:
            conditions = ["HEADER_EXTRACTED = 'Y'"]
            params = {}
            if type_filter:
                placeholders = ",".join(f":rt{i}" for i in range(len(type_filter)))
                conditions.append(f"CATALOG_READINESS IN ({placeholders})")
                params.update({f"rt{i}": v for i, v in enumerate(type_filter)})
            if readiness_filter:
                placeholders = ",".join(f":rd{i}" for i in range(len(readiness_filter)))
                conditions.append(f"CATALOG_READINESS IN ({placeholders})")
                params.update({f"rd{i}": v for i, v in enumerate(readiness_filter)})
            if has_uwi:
                conditions.append("MATCHED_UWI IS NOT NULL AND MATCHED_UWI != ''")

            where = " AND ".join(conditions)
            with engine.connect() as con:
                rows = con.execute(_t(f"""
                    SELECT
                        FILE_PATH, FILE_NAME, FILE_EXT, FILE_TYPE_GROUP,
                        CATALOG_READINESS, CATALOG_SCORE,
                        MATCHED_UWI,
                        CATALOG_ISSUES,
                        FILE_SIZE_KB,
                        SCAN_DATE
                    FROM file_catalog.GLOBAL_FILE_CATALOG
                    WHERE {where}
                    ORDER BY CATALOG_SCORE DESC, FILE_NAME
                """), params).fetchall()

            df = pd.DataFrame(rows, columns=[
                "file_path","file_name","extension","type_group",
                "readiness","score","uwi","issues","size_kb","scan_date",
            ])
            st.session_state["hf_df"] = df
        except Exception as e:
            st.error(f"Query failed: {e}")

    df = st.session_state.get("hf_df")
    if df is None:
        st.info("Click Query Catalog to load header data.")
        return

    m1,m2,m3,m4 = st.columns(4)
    m1.metric("Total",      len(df))
    m2.metric("Has UWI",    int(df["uwi"].notna().sum()))
    m3.metric("Ready",      int((df["readiness"]=="READY").sum()))
    m4.metric("Needs UWI",  int((df["readiness"]=="NEEDS_UWI").sum()))

    st.dataframe(df, hide_index=True, use_container_width=True)

    st.download_button(
        "⬇ Export Header Flat File CSV",
        data=df.to_csv(index=False),
        file_name="header_flat_file.csv",
        mime="text/csv",
        key="hf_export",
    )


# =============================================================================
# Tab 3 -- Manage (QC workspace)
# =============================================================================

def _tab_manage(engine, dialect):
    import pandas as pd
    from sqlalchemy import text as _t

    st.markdown("#### 🛠️ Manage Catalog")

    subtabs = st.tabs(["Browse & Extract", "Delete"])

    # ── Browse & Extract ──────────────────────────────────────────────────────
    with subtabs[0]:
        _manage_browse(engine, dialect)

    # ── Delete ────────────────────────────────────────────────────────────────
    with subtabs[1]:
        _manage_delete(engine, dialect)


def _manage_browse(engine, dialect):
    import pandas as pd
    from sqlalchemy import text as _t

    st.markdown("**Browse cataloged files**")

    # Filters
    f1, f2, f3 = st.columns(3)
    grp_filter = f1.selectbox(
        "File type group",
        ["All", "PDF", "Well Log", "Seismic", "Shapefile", "Office", "Image"],
        key="mb_grp",
    )
    rd_filter = f2.selectbox(
        "Readiness",
        ["All", "READY", "REVIEW", "NEEDS_UWI", "ATTENTION"],
        key="mb_rd",
    )
    search = f3.text_input("Search filename", key="mb_search",
                           placeholder="partial name...")

    if st.button("🔍 Browse", type="primary", key="mb_browse_btn"):
        try:
            conditions = ["1=1"]
            params = {}
            if grp_filter != "All":
                conditions.append("FILE_TYPE_GROUP = :grp")
                params["grp"] = grp_filter
            if rd_filter != "All":
                conditions.append("CATALOG_READINESS = :rd")
                params["rd"] = rd_filter
            if search:
                conditions.append("FILE_NAME LIKE :srch")
                params["srch"] = f"%{search}%"

            with engine.connect() as con:
                rows = con.execute(_t(f"""
                    SELECT TOP 500
                        INVENTORY_ID, FILE_NAME, FILE_EXT,
                        FILE_TYPE_GROUP, FILE_SIZE_KB,
                        CATALOG_READINESS, CATALOG_SCORE,
                        MATCHED_UWI, CATALOG_ISSUES,
                        FILE_PATH
                    FROM file_catalog.GLOBAL_FILE_CATALOG
                    WHERE {" AND ".join(conditions)}
                    ORDER BY CATALOG_SCORE DESC, FILE_NAME
                """), params).fetchall()

            df = pd.DataFrame(rows, columns=[
                "id","name","ext","group","size_kb",
                "readiness","score","uwi","issues","path",
            ])
            st.session_state["mb_df"] = df
        except Exception as e:
            st.error(f"Query failed: {e}")

    df = st.session_state.get("mb_df")
    if df is None:
        st.info("Set filters and click Browse.")
        return

    st.caption(f"{len(df):,} files (max 500 shown)")
    st.dataframe(
        df[["name","ext","group","size_kb","readiness","score","uwi","issues"]],
        hide_index=True, use_container_width=True,
    )

    st.divider()

    # File detail / extract
    if df.empty:
        return

    sel_name = st.selectbox(
        "Select file for detail / extract",
        df["name"].tolist(),
        key="mb_sel_file",
    )
    sel_row = df[df["name"] == sel_name].iloc[0]
    fpath   = sel_row["path"]
    fext    = sel_row["ext"].lower()
    inv_id  = sel_row["id"]

    st.caption(f"`{fpath}`")

    # View panel
    if fext == ".pdf":
        with st.expander("📄 View PDF", expanded=False):
            try:
                import base64
                b64 = base64.b64encode(Path(fpath).read_bytes()).decode()
                h   = st.slider("Height", 400, 1200, 600, 50, key="mb_pdf_h")
                st.markdown(
                    f'<iframe src="data:application/pdf;base64,{b64}" '
                    f'width="100%" height="{h}px" '
                    f'style="border:none;border-radius:8px;"></iframe>',
                    unsafe_allow_html=True)
            except Exception as e:
                st.error(f"PDF render failed: {e}")

    # Header attributes from catalog
    with st.expander("📋 Catalog attributes", expanded=True):
        attr_rows = [
            {"Attribute": k, "Value": str(v)}
            for k, v in {
                "UWI":       sel_row.get("uwi",""),
                "Readiness": sel_row.get("readiness",""),
                "Score":     sel_row.get("score",""),
                "Issues":    sel_row.get("issues",""),
                "Group":     sel_row.get("group",""),
                "Size KB":   sel_row.get("size_kb",""),
            }.items() if v and v not in ("None","nan","")
        ]
        if attr_rows:
            st.dataframe(pd.DataFrame(attr_rows),
                         hide_index=True, use_container_width=True)

    # Re-extract on demand
    if st.button("🔄 Re-extract header", key="mb_reextract"):
        with st.spinner("Extracting..."):
            fields = _extract_fields(fpath, fext)
            _write_enrichment(engine, inv_id, fields, dialect)
        st.success("Re-extracted and written to catalog.")
        st.session_state.pop("mb_df", None)
        st.rerun()

    # Extract structured data
    if fext == ".pdf":
        if st.button("📐 Extract structured data", key="mb_extract_pdf"):
            _extract_and_display_pdf(fpath)


def _extract_and_display_pdf(fpath: str):
    import pandas as pd
    try:
        from dataview.file_catalog.pdf_survey_catalog import (
            classify_pdf, extract_stations,
            extract_eowr, extract_rft_data,
            extract_well_test, extract_petrophysical,
            extract_casing_cement, extract_ddr,
            extract_scout_ticket,
            RT_DIRECTIONAL, RT_EOWR, RT_RFT,
            RT_WELL_TEST, RT_PETRO, RT_CASING,
            RT_DDR, RT_SCOUT,
        )
        cl = classify_pdf(fpath)
        rt = cl.get("report_type", "UNKNOWN")
        st.caption(f"Report type: **{rt}**")

        if rt == RT_DIRECTIONAL:
            r = extract_stations(fpath)
            rows = r.get("stations", [])
        elif rt == RT_EOWR:
            r = extract_eowr(fpath)
            rows = r.get("strat", [])
            if r.get("summary"):
                st.dataframe(pd.DataFrame([{"Field":k,"Value":str(v)}
                    for k,v in r["summary"].items() if v]),
                    hide_index=True, use_container_width=True)
        elif rt == RT_RFT:
            rows = extract_rft_data(fpath).get("rows", [])
        elif rt == RT_WELL_TEST:
            rows = extract_well_test(fpath).get("flow_rows", [])
        elif rt in (RT_PETRO, "PETROPHYSICAL"):
            r = extract_petrophysical(fpath)
            rows = r.get("zones") or r.get("interval") or []
        elif rt == RT_CASING:
            r = extract_casing_cement(fpath)
            rows = r.get("casing", []) + r.get("cement", [])
        elif rt == RT_DDR:
            rows = extract_ddr(fpath).get("ops", [])
        elif rt == RT_SCOUT:
            r = extract_scout_ticket(fpath)
            rows = r.get("ip_rows") or r.get("perf_rows") or []
        else:
            rows = []

        if rows:
            df = pd.DataFrame(rows).fillna("")
            st.metric("Records extracted", len(df))
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.download_button("⬇ Download CSV",
                data=df.to_csv(index=False),
                file_name=f"{Path(fpath).stem}_extract.csv",
                mime="text/csv", key="mb_dl_pdf")
        else:
            st.info("No structured data extracted for this report type.")
    except Exception as e:
        st.error(f"Extraction failed: {e}")


def _manage_delete(engine, dialect):
    import pandas as pd
    from sqlalchemy import text as _t

    st.markdown("**Delete from catalog**")
    st.warning(
        "Deletes remove files from the catalog only — "
        "not from the file system.",
        icon="⚠️",
    )

    d1, d2 = st.columns(2)

    # Delete by extension
    with d1:
        st.markdown("**By extension**")
        del_ext = st.text_input("Extension", placeholder=".tif",
                                key="del_ext")
        if st.button("🗑️ Delete by extension", key="del_ext_btn",
                     type="secondary"):
            if not del_ext.startswith("."):
                st.error("Extension must start with '.'")
            else:
                try:
                    with engine.begin() as con:
                        n = con.execute(_t("""
                            DELETE FROM file_catalog.GLOBAL_FILE_CATALOG
                            WHERE FILE_EXT = :ext
                        """), {"ext": del_ext.lower()}).rowcount
                    st.success(f"Deleted {n:,} rows with extension `{del_ext}`")
                except Exception as e:
                    st.error(f"Delete failed: {e}")

    # Delete by name pattern
    with d2:
        st.markdown("**By filename pattern**")
        del_pattern = st.text_input("Filename contains",
                                    placeholder="thumb",
                                    key="del_pattern")
        if st.button("🗑️ Delete by pattern", key="del_pat_btn",
                     type="secondary"):
            if not del_pattern:
                st.error("Enter a pattern.")
            else:
                try:
                    # Preview first
                    with engine.connect() as con:
                        count = con.execute(_t("""
                            SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG
                            WHERE FILE_NAME LIKE :p
                        """), {"p": f"%{del_pattern}%"}).scalar()
                    st.session_state["del_pat_count"] = count
                    st.session_state["del_pat_val"]   = del_pattern
                except Exception as e:
                    st.error(f"Preview failed: {e}")

        count = st.session_state.get("del_pat_count")
        pat   = st.session_state.get("del_pat_val")
        if count is not None and pat == del_pattern:
            st.caption(f"{count:,} files match `*{del_pattern}*`")
            if count > 0:
                if st.button(f"✅ Confirm delete {count:,} rows",
                             key="del_pat_confirm", type="primary"):
                    try:
                        with engine.begin() as con:
                            n = con.execute(_t("""
                                DELETE FROM file_catalog.GLOBAL_FILE_CATALOG
                                WHERE FILE_NAME LIKE :p
                            """), {"p": f"%{del_pattern}%"}).rowcount
                        st.success(f"Deleted {n:,} rows matching `*{del_pattern}*`")
                        st.session_state.pop("del_pat_count", None)
                    except Exception as e:
                        st.error(f"Delete failed: {e}")

    st.divider()

    # Delete selected file
    st.markdown("**Delete individual file**")
    del_path = st.text_input("Full file path", key="del_single_path",
                             placeholder=r"C:\WellData\file.pdf")
    if st.button("🗑️ Delete this file from catalog", key="del_single_btn"):
        if not del_path:
            st.error("Enter a file path.")
        else:
            try:
                with engine.begin() as con:
                    n = con.execute(_t("""
                        DELETE FROM file_catalog.GLOBAL_FILE_CATALOG
                        WHERE FILE_PATH = :p
                    """), {"p": del_path}).rowcount
                if n:
                    st.success(f"Deleted `{del_path}` from catalog.")
                else:
                    st.warning("File not found in catalog.")
            except Exception as e:
                st.error(f"Delete failed: {e}")

    st.divider()

    # Delete by root path
    st.markdown("**Delete by root path (rescan a folder)**")
    del_root = st.text_input("Root path", key="del_root_path",
                             placeholder=r"\\server\old_share")
    if st.button("🗑️ Delete all files under this path",
                 key="del_root_btn", type="secondary"):
        if not del_root:
            st.error("Enter a root path.")
        else:
            try:
                with engine.connect() as con:
                    count = con.execute(_t("""
                        SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG
                        WHERE FILE_PATH LIKE :p
                    """), {"p": f"{del_root}%"}).scalar()
                st.session_state["del_root_count"] = count
                st.session_state["del_root_val"]   = del_root
            except Exception as e:
                st.error(f"Preview failed: {e}")

    root_count = st.session_state.get("del_root_count")
    root_val   = st.session_state.get("del_root_val")
    if root_count is not None and root_val == del_root and del_root:
        st.caption(f"{root_count:,} files under `{del_root}`")
        if root_count > 0:
            if st.button(f"✅ Confirm delete {root_count:,} rows",
                         key="del_root_confirm", type="primary"):
                try:
                    with engine.begin() as con:
                        n = con.execute(_t("""
                            DELETE FROM file_catalog.GLOBAL_FILE_CATALOG
                            WHERE FILE_PATH LIKE :p
                        """), {"p": f"{del_root}%"}).rowcount
                    st.success(f"Deleted {n:,} rows under `{del_root}`")
                    st.session_state.pop("del_root_count", None)
                except Exception as e:
                    st.error(f"Delete failed: {e}")


# =============================================================================
# Tab 4 -- Extract & Load
# =============================================================================

def _tab_extract_load(engine, dialect):
    import pandas as pd
    from sqlalchemy import text as _t

    st.markdown("#### 🚀 Extract & Load")
    st.caption(
        "Select a dataset type, preview all cataloged files of that type, "
        "then batch extract and load to PPDM tables. "
        "Only files with a matching UWI in dv_well will load."
    )

    dataset_type = st.selectbox(
        "Dataset type",
        list(RT_LABELS.keys()),
        format_func=lambda x: RT_LABELS.get(x, x),
        key="el_type",
    )

    if st.button("🔍 Preview", type="secondary", key="el_preview"):
        try:
            with engine.connect() as con:
                rows = con.execute(_t("""
                    SELECT
                        g.INVENTORY_ID,
                        g.FILE_PATH,
                        g.FILE_NAME,
                        g.MATCHED_UWI,
                        g.CATALOG_READINESS,
                        g.CATALOG_SCORE,
                        CASE
                            WHEN w.uwi IS NOT NULL THEN 'Yes'
                            ELSE 'No'
                        END AS well_exists
                    FROM file_catalog.GLOBAL_FILE_CATALOG g
                    LEFT JOIN dataview.dv_well w
                        ON w.uwi = g.MATCHED_UWI
                        OR REPLACE(REPLACE(REPLACE(w.uwi,'-',''),' ',''),'/','')
                           = REPLACE(REPLACE(REPLACE(g.MATCHED_UWI,'-',''),' ',''),'/','')
                    WHERE g.HEADER_EXTRACTED = 'Y'
                    ORDER BY g.CATALOG_SCORE DESC
                """)).fetchall()

            df = pd.DataFrame(rows, columns=[
                "id","path","name","uwi","readiness","score","well_exists"
            ])
            st.session_state["el_df"] = df
        except Exception as e:
            st.error(f"Preview failed: {e}")

    df = st.session_state.get("el_df")
    if df is None:
        st.info("Click Preview to load files.")
        return

    ready    = df[df["well_exists"] == "Yes"]
    no_well  = df[df["well_exists"] == "No"]

    m1,m2,m3 = st.columns(3)
    m1.metric("Total cataloged", len(df))
    m2.metric("✅ Well matched",  len(ready))
    m3.metric("❌ No well",       len(no_well))

    st.dataframe(
        df[["name","uwi","readiness","score","well_exists"]],
        hide_index=True, use_container_width=True,
    )

    if len(ready) == 0:
        st.warning(
            "No files have a matching well. "
            "Export the Header Flat File, create wells, then re-preview."
        )
        return

    st.divider()

    if not st.button(
        f"🚀 Batch Extract & Load — {len(ready)} files",
        type="primary", key="el_run",
    ):
        return

    prog = st.progress(0.0, text="Starting batch load...")
    loaded = skipped = errors = 0
    err_msgs = []

    for i, row in enumerate(ready.itertuples()):
        prog.progress((i + 1) / len(ready),
                      text=f"Loading {row.name}...")
        fpath = row.path
        fext  = Path(fpath).suffix.lower()
        uwi   = row.uwi

        try:
            if fext == ".pdf":
                _batch_load_pdf(engine, dialect, fpath, uwi)
                loaded += 1
            elif fext in SHP_EXTS:
                _batch_load_shp(engine, dialect, fpath, uwi)
                loaded += 1
            else:
                skipped += 1
        except Exception as ex:
            errors += 1
            err_msgs.append(f"{row.name}: {ex}")

    prog.empty()
    r1,r2,r3 = st.columns(3)
    r1.metric("✅ Loaded",  loaded)
    r2.metric("⏭️ Skipped", skipped)
    r3.metric("❌ Errors",  errors)

    if err_msgs:
        with st.expander(f"⚠️ {len(err_msgs)} error(s)", expanded=True):
            for m in err_msgs[:30]:
                st.text(m)


def _batch_load_pdf(engine, dialect, fpath, uwi):
    from dataview.file_catalog.pdf_survey_catalog import (
        classify_pdf, extract_stations, load_to_ppdm,
        extract_eowr, extract_rft_data, extract_well_test,
        extract_petrophysical, extract_casing_cement,
        extract_ddr, extract_scout_ticket,
        RT_DIRECTIONAL, RT_EOWR, RT_RFT, RT_WELL_TEST,
        RT_PETRO, RT_CASING, RT_DDR, RT_SCOUT,
    )
    cl = classify_pdf(fpath)
    rt = cl.get("report_type", "UNKNOWN")
    well_info = {
        "uwi":       uwi,
        "well_name": cl.get("well_name",""),
        "operator":  cl.get("operator",""),
    }

    if rt == RT_DIRECTIONAL:
        r = extract_stations(fpath)
        rows = r.get("stations", [])
        if rows:
            load_to_ppdm(well_info=well_info, stations=rows,
                         engine=engine, dialect=dialect)
    elif rt == RT_EOWR:
        rows = extract_eowr(fpath).get("strat", [])
        if rows:
            from dataview.file_catalog.pdf_db_loader import load_formation_tops
            load_formation_tops(engine=engine, dialect=dialect,
                                well_info=well_info, rows=rows)
    elif rt == RT_RFT:
        rows = extract_rft_data(fpath).get("rows", [])
        if rows:
            from dataview.file_catalog.pdf_db_loader import load_rft
            load_rft(engine=engine, dialect=dialect,
                     well_info=well_info, rows=rows)
    elif rt == RT_WELL_TEST:
        rows = extract_well_test(fpath).get("flow_rows", [])
        if rows:
            from dataview.file_catalog.pdf_db_loader import load_well_test
            load_well_test(engine=engine, dialect=dialect,
                           well_info=well_info, rows=rows)
    elif rt == RT_CASING:
        r = extract_casing_cement(fpath)
        rows = r.get("casing", []) + r.get("cement", [])
        if rows:
            from dataview.file_catalog.pdf_db_loader import load_casing
            load_casing(engine=engine, dialect=dialect,
                        well_info=well_info, rows=rows)
    elif rt == RT_SCOUT:
        r = extract_scout_ticket(fpath)
        rows = r.get("ip_rows") or r.get("perf_rows") or []
        if rows:
            from dataview.file_catalog.pdf_db_loader import load_scout
            load_scout(engine=engine, dialect=dialect,
                       well_info=well_info, rows=rows)


def _batch_load_shp(engine, dialect, fpath, uwi):
    from dataview.mapping.shapefile_catalog import load_to_ppdm as shp_load
    shp_load(file_path=fpath, engine=engine, dialect=dialect,
             well_info={"uwi": uwi})

# =============================================================================
# Tab 5 -- Pipeline  (post-catalog operations run from the UI)
#
# Each button calls the SAME core function as its standalone CLI script
# (enrich_file_headers.enrich / vault_copy.vault / collect_final_documents.collect).
# The scripts live at the app root; we hand them the app's own DB connection via
# engine.raw_connection() (an mssql+pyodbc raw connection is exactly the pyodbc
# cursor the scripts expect) and capture their log output into the page.
# =============================================================================

def _run_op(engine, fn, a, spinner_label):
    """Run a pipeline core function against the app DB, capturing its log.
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


def _tab_pipeline(engine, dialect):
    import types as _types

    st.markdown("#### 🧩 Pipeline")
    st.caption(
        "Post-catalog operations, run against the current database. Each button "
        "runs the exact same code as its command-line script — no separate logic."
    )

    try:
        _db   = engine.url.database or "?"
        _host = engine.url.host or "?"
    except Exception:
        _db = _host = "?"
    st.caption(f"Target: **{_host} / {_db}**")

    REF = "WELL_REF.well_ref.well_master_gold"   # 3-part cross-DB reference

    # ── ① Enrich Headers ───────────────────────────────────────────────────
    with st.expander(
        "① Enrich Headers — curate UWI14 · resolve by name · fill blanks · reverse-capture",
        expanded=True,
    ):
        st.caption("Adds/refreshes the canonical UWI14 on FILE_WELL_HEADER, "
                   "resolves blank UWIs by name against the reference, fills blank "
                   "attributes, and records document→reference contributions.")
        c1, c2 = st.columns([1, 3])
        en_dry = c1.checkbox("Dry run", value=True, key="pl_en_dry")
        if c2.button("Run enrichment", type="primary", key="pl_en_run",
                     use_container_width=True):
            from dataview.file_catalog import enrich_file_headers as _en
            a = _types.SimpleNamespace(
                server=_host, database=_db, odbc_driver="",
                ref=REF, depth_tol=50.0,
                no_well=False, no_seis=False, no_reverse=False,
                dry_run=en_dry, report=None, reverse_report=None)
            lines, res, err = _run_op(engine, _en.enrich, a, "Enriching headers…")
            if err:
                st.error(err)
            st.code("\n".join(lines) or "(no output)")

    # ── ② Vault Copy ───────────────────────────────────────────────────────
    with st.expander(
        "② Vault Copy — file catalogued wells & seismic into the vault tree"
    ):
        st.caption(r"Wells → <vault>\<COUNTRY>\<STATE>\<UWI14>\<WELL_NAME> · "
                   r"Seismic → <vault>\<COUNTRY>\<STATE>\<2D|3D>\<SURVEY>")
        v1, v2 = st.columns(2)
        v_vault = v1.text_input("Vault root", value=r"C:\Bulk\Vault", key="pl_v_vault")
        v_ctry  = v2.text_input("Default country", value="US", key="pl_v_ctry")
        v3, v4 = st.columns([1, 3])
        v_dry = v3.checkbox("Dry run", value=True, key="pl_v_dry")
        if v4.button("Run vault copy", type="primary", key="pl_v_run",
                     use_container_width=True):
            from dataview.file_catalog import vault_copy as _vc
            a = _types.SimpleNamespace(
                server=_host, database=_db, odbc_driver="",
                ref=REF, vault=v_vault, default_country=v_ctry,
                seis_ext="segy,sgy,seg,segd,sgd,p190,p111",
                no_wells=False, no_seis=False,
                dry_run=v_dry, report=None, limit=0)
            lines, res, err = _run_op(engine, _vc.vault, a, "Copying to vault…")
            if err:
                st.error(err)
            st.code("\n".join(lines) or "(no output)")

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
        if st.button("Run deep catalog", type="primary", key="pl_dc_run",
                     use_container_width=True):
            if not g_sel:
                st.warning("Select at least one format.")
            else:
                exts = sorted({e for g in g_sel for e in _dc.DEEP_GROUPS[g]})
                a = _types.SimpleNamespace(
                    server=_host, database=_db, odbc_driver="",
                    exts=",".join(exts), limit=int(dc_limit),
                    workers=int(dc_workers), dry_run=dc_dry, report=None)
                lines, res, err = _run_op_engine(
                    engine, _dc.deep_catalog, a, "Deep cataloging…")
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
                        with st.expander(f"⚠️ Failures ({len(_errs)})"):
                            for _e in _errs:
                                st.text(_e)
                st.code("\n".join(lines) or "(no output)")


def _well_key_grid(engine):
    import pandas as pd
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
            SELECT h.WELL_HEADER_ID AS id, g.FILE_PATH AS path,
                   h.WELL_NAME AS well_name, h.UWI AS internal_uwi
            FROM file_catalog.FILE_WELL_HEADER h
            LEFT JOIN file_catalog.GLOBAL_FILE_CATALOG g
                   ON g.INVENTORY_ID = h.INVENTORY_ID
            WHERE h.UWI14 IS NULL OR h.UWI14 = '00000000000000'
            ORDER BY g.FILE_PATH""")).fetchall()

    if not rows:
        st.success("Every well header already has a valid UWI14 — nothing to key.")
        return

    ids, recs = [], []
    for r in rows:
        ids.append(r.id)
        g = _pi.uwi14_from_path(r.path or "")[0] or ""
        recs.append({"file": _pi._basename(r.path or ""), "well_name": r.well_name or "",
                     "internal_uwi": r.internal_uwi or "", "guess (from path)": g,
                     "assign UWI14": g})
    df = pd.DataFrame(recs)

    edited = st.data_editor(
        df, key="pl_well_editor", hide_index=True, use_container_width=True,
        disabled=["file", "well_name", "internal_uwi", "guess (from path)"])

    if st.button("💾 Save well UWIs", type="primary", key="pl_well_save"):
        ups = []
        for i, val in enumerate(edited["assign UWI14"].tolist()):
            u = _pi.norm_uwi14(val)
            if u:
                ups.append({"id": ids[i], "u": u})
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
    import pandas as pd
    from sqlalchemy import text as _t
    from dataview.core import path_identity as _pi

    with engine.connect() as con:
        rows = con.execute(_t("""
            SELECT sh.SEIS_HEADER_ID AS id, g.FILE_PATH AS path,
                   sh.SURVEY_NAME AS survey
            FROM file_catalog.FILE_SEIS_HEADER sh
            LEFT JOIN file_catalog.GLOBAL_FILE_CATALOG g
                   ON g.INVENTORY_ID = sh.INVENTORY_ID
            WHERE sh.SURVEY_NAME IS NULL OR LTRIM(RTRIM(sh.SURVEY_NAME)) = ''
            ORDER BY g.FILE_PATH""")).fetchall()

    if not rows:
        st.success("Every seismic header already has a survey name.")
        return

    ids, recs = [], []
    for r in rows:
        ids.append(r.id)
        g = _pi.survey_from_path(r.path or "") or ""
        recs.append({"file": _pi._basename(r.path or ""), "current": r.survey or "",
                     "guess (from path)": g, "assign survey": g})
    df = pd.DataFrame(recs)

    edited = st.data_editor(
        df, key="pl_seis_editor", hide_index=True, use_container_width=True,
        disabled=["file", "current", "guess (from path)"])

    if st.button("💾 Save survey names", type="primary", key="pl_seis_save"):
        ups = []
        for i, val in enumerate(edited["assign survey"].tolist()):
            v = (str(val) or "").strip()
            if v:
                ups.append({"id": ids[i], "v": v})
        if not ups:
            st.warning("No survey names to write.")
        else:
            with engine.begin() as con:
                for up in ups:
                    con.execute(_t(
                        "UPDATE file_catalog.FILE_SEIS_HEADER "
                        "SET SURVEY_NAME=:v, SURVEY_NAME_SOURCE='manual' "
                        "WHERE SEIS_HEADER_ID=:id"), up)
            st.success(f"Wrote {len(ups)} survey name(s).")
            st.rerun()
