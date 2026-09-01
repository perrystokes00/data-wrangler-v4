"""
page_dv_catalog.py
==================
DataView v3 — Document Inventory & Work Queue

Tabs:
  🗂  Inventory   — crawl C:\\Bulk (or any folder), populate dv_global_file_catalog
  📋  Work Queue  — cataloger's assigned files with two-checkbox grid
  🔍  Inspect     — file detail: summary, header, curves/plot, decision
  ⚙   Admin       — vault setup, user/group management, file assignment

Called from app.py:
    from dataview.file_catalog import page_dv_catalog
    page_dv_catalog.run(engine)
"""
from __future__ import annotations

import os
import hashlib
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# ── DataView adapter — must import before file_inventory ──────────────
try:
    from dataview.file_catalog import dv_catalog_adapter as _adapter
    VAULT_ROOT  = _adapter.VAULT_ROOT
    get_doc_type = _adapter.get_doc_type
    ensure_vault = _adapter.ensure_vault
    _ADAPTER_OK  = True
except Exception as _ae:
    _ADAPTER_OK  = False
    VAULT_ROOT   = r"C:\Bulk"
    def get_doc_type(ext): return ("Other", "UNKNOWN")
    def ensure_vault(root=VAULT_ROOT): return {}

try:
    from sqlalchemy import text
    from dataview.file_catalog.file_inventory import (
        ensure_inventory_schema,
        crawl_paths,
    )
    _INV_OK = True
except Exception as _ie:
    _INV_OK = False
    _INV_ERR = str(_ie)

# ── Optional: LAS/DLIS/LIS inspection ────────────────────────────────
try:
    from dataview.file_catalog.las_catalog import parse_las_header, get_file_curves
    HAS_LAS = True
except Exception:
    HAS_LAS = False

try:
    from dataview.file_catalog.dlis_catalog import parse_dlis_header
    HAS_DLIS = True
except Exception:
    HAS_DLIS = False

try:
    import pdfplumber
    HAS_PDF = True
except Exception:
    HAS_PDF = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MPL = True
except Exception:
    HAS_MPL = False

# =============================================================================
# CONSTANTS
# =============================================================================

GFC_TABLE  = "[dataview].[dv_global_file_catalog]"
STATUS_OPTS = ["UNCATALOGED", "IN_REVIEW", "CATALOGED", "SKIPPED", "DUPLICATE"]

DOC_GROUP_ICONS = {
    "Well Logs":  "🛢",
    "Seismic":    "🌊",
    "Documents":  "📄",
    "Office":     "📊",
    "Spatial":    "🗺",
    "Tabular":    "📋",
    "Images":     "🖼",
    "Other":      "📁",
}

# =============================================================================
# DB HELPERS
# =============================================================================

def _load_inventory(engine, filters: dict | None = None) -> pd.DataFrame:
    where = ["1=1"]
    params = {}
    if filters:
        if filters.get("doc_type_group"):
            where.append("DOC_TYPE_GROUP = :dtg")
            params["dtg"] = filters["doc_type_group"]
        if filters.get("catalog_status"):
            where.append("CATALOG_STATUS = :cs")
            params["cs"] = filters["catalog_status"]
        if filters.get("root_path"):
            where.append("ROOT_PATH = :rp")
            params["rp"] = filters["root_path"]
        if filters.get("assigned_to"):
            where.append("ROW_CHANGED_BY = :at")
            params["at"] = filters["assigned_to"]
    sql = f"""
        SELECT TOP 2000
            INVENTORY_ID, FILE_NAME, FILE_EXT,
            DOC_TYPE_GROUP, DOC_TYPE, CATALOG_STATUS,
            FILE_SIZE_KB, MODIFIED_DATE, SCAN_DATE,
            UWI, WELL_NAME, ROOT_PATH, FULL_PATH,
            DUPLICATE_GROUP, PPDM_LOADED_IND,
            ROW_CHANGED_BY AS ASSIGNED_TO
        FROM {GFC_TABLE}
        WHERE {' AND '.join(where)}
        ORDER BY DOC_TYPE_GROUP, FILE_NAME
    """
    try:
        with engine.connect() as con:
            return pd.read_sql(text(sql), con, params=params)
    except Exception as exc:
        st.error(f"Inventory query failed: {exc}")
        return pd.DataFrame()



def _render_pdf_panel(engine, file_path: str, inv_id: str, idx: int):
    """
    Unified PDF panel: View + Auto-extract + Load to DB.
    Extraction runs automatically on first render.
    """
    import hashlib
    cache_key = f"pdf_extract_{hashlib.md5(file_path.encode()).hexdigest()}"

    # ── Auto-extract on first render ─────────────────────────────────
    if cache_key not in st.session_state:
        with st.spinner("🔍 Extracting data from PDF..."):
            try:
                from dataview.file_catalog.file_summarizer import summarize_file
                result = summarize_file(file_path)
                st.session_state[cache_key] = result
            except Exception as e:
                st.session_state[cache_key] = {"error": str(e)}

    extracted = st.session_state.get(cache_key, {})

    # ── Layout: viewer left, extracted data right ────────────────────
    col_view, col_data = st.columns([3, 2])

    with col_view:
        st.markdown("**📄 Preview**")
        try:
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                total_pages = len(pdf.pages)
                page_num = st.number_input(
                    f"Page (1–{total_pages})", min_value=1,
                    max_value=total_pages, value=1,
                    key=f"pdf_page_{idx}")
                page = pdf.pages[page_num - 1]
                text = page.extract_text() or ""
                st.text_area("", value=text, height=400,
                             key=f"pdf_text_{idx}",
                             label_visibility="collapsed")
        except Exception as e:
            st.warning(f"PDF preview unavailable: {e}")

    with col_data:
        st.markdown("**⚙️ Extracted Data**")
        if "error" in extracted:
            st.error(f"Extraction failed: {extracted['error']}")
        else:
            meta = extracted.get("meta", {})
            doc_type = extracted.get("doc_type", "")
            st.caption(f"Type: **{doc_type}**")

            uwi      = st.text_input("UWI",       value=meta.get("uwi",""),
                                     key=f"pdf_uwi_{idx}")
            well_name= st.text_input("Well name", value=meta.get("well_name",""),
                                     key=f"pdf_wn_{idx}")
            operator = st.text_input("Operator",  value=meta.get("operator",""),
                                     key=f"pdf_op_{idx}")
            summary  = st.text_area("Summary",
                                    value=extracted.get("summary",""),
                                    height=100, key=f"pdf_sum_{idx}")

            # Records preview
            records = extracted.get("records", [])
            if records:
                import pandas as pd
                st.caption(f"{len(records):,} records extracted")
                st.dataframe(pd.DataFrame(records[:10]),
                             use_container_width=True, hide_index=True)

        st.divider()

        # ── Load to DB ───────────────────────────────────────────────
        lc1, lc2, lc3 = st.columns(3)
        if lc1.button("✅ Load to DB", type="primary",
                      key=f"pdf_load_{idx}"):
            try:
                from dataview.file_catalog.doc_catalog_store import catalog_document
                r = catalog_document(
                    engine=engine,
                    file_path=file_path,
                    doc_type=extracted.get("doc_type","PDF"),
                    meta={
                        "uwi":       uwi,
                        "well_name": well_name,
                        "operator":  operator,
                    },
                    records=records,
                    source="PDF_CATALOG",
                )
                if r.get("ok"):
                    _update_status(engine, [inv_id], "CATALOGED", "SYSTEM")
                    st.success(f"✅ Loaded to DB — {r.get('rows_inserted',0)} records")
                    # Clear cache so next file starts fresh
                    st.session_state.pop(cache_key, None)
                else:
                    st.error(f"Failed: {r.get('error')}")
            except Exception as e:
                st.error(f"Load failed: {e}")

        if lc2.button("⏭ Skip", key=f"pdf_skip_{idx}"):
            _update_status(engine, [inv_id], "SKIPPED", "SYSTEM")
            st.session_state.pop(cache_key, None)
            st.session_state["dvc_inspect_idx"] = idx + 1
            st.rerun()

        if lc3.button("🚩 Flag", key=f"pdf_flag_{idx}"):
            _update_status(engine, [inv_id], "FLAGGED", "SYSTEM")
            st.session_state.pop(cache_key, None)
            st.rerun()


def _update_status(engine, inventory_ids: list[str], status: str,
                   assigned_to: str = ""):
    if not inventory_ids:
        return 0
    id_list = ", ".join(f"'{i}'" for i in inventory_ids)
    sql = f"""
        UPDATE {GFC_TABLE}
        SET    CATALOG_STATUS  = :status,
               ROW_CHANGED_BY  = :who,
               ROW_CHANGED_DATE = GETDATE()
        WHERE  INVENTORY_ID IN ({id_list})
    """
    try:
        with engine.begin() as con:
            r = con.execute(text(sql),
                            {"status": status, "who": assigned_to or "SYSTEM"})
            return r.rowcount
    except Exception as exc:
        st.error(f"Status update failed: {exc}")
        return 0


def _inventory_summary(engine) -> dict:
    sql = f"""
        SELECT
            COUNT(*)                                    AS total,
            SUM(CASE WHEN CATALOG_STATUS='UNCATALOGED'  THEN 1 ELSE 0 END) AS uncataloged,
            SUM(CASE WHEN CATALOG_STATUS='IN_REVIEW'    THEN 1 ELSE 0 END) AS in_review,
            SUM(CASE WHEN CATALOG_STATUS='CATALOGED'    THEN 1 ELSE 0 END) AS cataloged,
            SUM(CASE WHEN CATALOG_STATUS='SKIPPED'      THEN 1 ELSE 0 END) AS skipped,
            SUM(CASE WHEN DUPLICATE_GROUP IS NOT NULL   THEN 1 ELSE 0 END) AS duplicates,
            SUM(ISNULL(FILE_SIZE_KB,0)) / 1024.0        AS total_mb
        FROM {GFC_TABLE}
    """
    try:
        with engine.connect() as con:
            row = con.execute(text(sql)).fetchone()
        return dict(zip(
            ["total","uncataloged","in_review","cataloged","skipped","duplicates","total_mb"],
            row
        )) if row else {}
    except Exception:
        return {}


# =============================================================================
# CRAWL HELPERS
# =============================================================================

def _quick_hash(path: str, block_size: int = 65536) -> str:
    """SHA1 of first 64KB — fast dedup signal."""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            h.update(f.read(block_size))
    except Exception:
        pass
    return h.hexdigest()


def _crawl_and_insert(engine, root: str, extensions: list[str],
                      progress_cb=None) -> tuple[int, int]:
    """
    Walk root, insert new files into dv_global_file_catalog.
    Returns (inserted, skipped_existing).
    """
    from sqlalchemy import text as _text

    # Load existing paths for dedup
    try:
        with engine.connect() as con:
            existing = {
                r[0] for r in con.execute(_text(
                    f"SELECT FULL_PATH FROM {GFC_TABLE}"
                )).fetchall()
            }
    except Exception:
        existing = set()

    exts = {e.lower() for e in extensions}
    rows = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            if fp in existing:
                continue
            ext = Path(fn).suffix
            if exts and ext.lower() not in exts:
                continue
            try:
                stat = os.stat(fp)
                dtg, dt = get_doc_type(ext)
                inv_id  = hashlib.sha1(fp.encode()).hexdigest()[:40]
                rows.append({
                    "INVENTORY_ID":    inv_id,
                    "FULL_PATH":       fp,
                    "FILE_NAME":       fn,
                    "FILE_EXT":        ext,
                    "FILE_SIZE_KB":    round(stat.st_size / 1024, 2),
                    "FILE_HASH":       _quick_hash(fp),
                    "MODIFIED_DATE":   datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "SCAN_DATE":       datetime.utcnow().isoformat(),
                    "DOC_TYPE_GROUP":  dtg,
                    "DOC_TYPE":        dt,
                    "CATALOG_STATUS":  "UNCATALOGED",
                    "ROOT_PATH":       root,
                    "SOURCE":          "DATAVIEW",
                    "ROW_CREATED_BY":  "CRAWLER",
                    "PPDM_LOADED_IND": "N",
                })
            except Exception:
                continue

    if not rows:
        return 0, len(existing)

    # Mark duplicates by hash
    hash_seen: dict[str, str] = {}
    for r in rows:
        h = r["FILE_HASH"]
        if h in hash_seen:
            r["DUPLICATE_GROUP"] = h
        else:
            hash_seen[h] = r["INVENTORY_ID"]
            r["DUPLICATE_GROUP"] = None

    # Batch insert
    inserted = 0
    batch_size = 200
    cols = list(rows[0].keys())
    col_list = ", ".join(f"[{c}]" for c in cols)
    val_list = ", ".join(f":{c}" for c in cols)
    sql = (f"IF NOT EXISTS (SELECT 1 FROM {GFC_TABLE} WHERE INVENTORY_ID=:INVENTORY_ID) "
           f"INSERT INTO {GFC_TABLE} ({col_list}) VALUES ({val_list})")

    with engine.begin() as con:
        for i, row in enumerate(rows):
            try:
                con.execute(text(sql), row)
                inserted += 1
            except Exception:
                pass
            if progress_cb and i % 50 == 0:
                progress_cb(i + 1, len(rows))

    return inserted, len(existing)


# =============================================================================
# TAB 1 — INVENTORY
# =============================================================================

def _render_inventory(engine):
    st.subheader("🗂 Document Inventory")
    st.caption(f"Vault root: **{VAULT_ROOT}** — crawl any folder to populate the inventory.")

    # ── Summary metrics ───────────────────────────────────────────────
    summary = _inventory_summary(engine) or {}
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total files",  f"{int(summary.get('total',0) or 0):,}")
    c2.metric("Uncataloged",  f"{int(summary.get('uncataloged',0) or 0):,}")
    c3.metric("In Review",    f"{int(summary.get('in_review',0) or 0):,}")
    c4.metric("Cataloged",    f"{int(summary.get('cataloged',0) or 0):,}")
    c5.metric("Duplicates",   f"{int(summary.get('duplicates',0) or 0):,}")
    c6.metric("Total size",   f"{float(summary.get('total_mb',0) or 0):.1f} MB")
    st.divider()

    # ── Crawl panel ───────────────────────────────────────────────────
    with st.expander("🔍 Crawl a folder", expanded=not bool(summary.get("total"))):
        col1, col2 = st.columns([3, 1])
        with col1:
            crawl_root = st.text_input(
                "Folder to crawl",
                value=VAULT_ROOT,
                key="dvc_crawl_root",
                placeholder=r"e.g. C:\Bulk or C:\WellData\LAS_Files",
            )
        with col2:
            st.markdown("<br>", unsafe_allow_html=True)
            create_vault = st.checkbox(
                "Create vault structure", value=False,
                key="dvc_create_vault")

        # Extension filter
        all_exts = [
            ".las", ".dlis", ".lis",
            ".sgy", ".segy", ".p190",
            ".pdf", ".xlsx", ".xls", ".docx", ".doc",
            ".shp", ".geojson",
            ".csv", ".tsv",
            ".tif", ".tiff", ".jpg", ".jpeg", ".png",
        ]
        sel_exts = st.multiselect(
            "File extensions to include (blank = all)",
            options=all_exts,
            default=[".las", ".dlis", ".lis", ".pdf",
                     ".xlsx", ".docx", ".sgy", ".segy"],
            key="dvc_extensions",
        )

        if st.button("🚀 Start Crawl", type="primary", key="dvc_crawl_btn",
                     disabled=not crawl_root):
            if not os.path.isdir(crawl_root):
                st.error(f"Folder not found: `{crawl_root}`")
            else:
                if create_vault:
                    paths = ensure_vault(crawl_root)
                    st.success(f"Vault structure created: {list(paths.keys())}")

                prog = st.progress(0, text="Starting crawl…")
                status_txt = st.empty()

                def _cb(done, total):
                    pct = min(done / total, 1.0)
                    prog.progress(pct, text=f"Scanning {done:,} / {total:,} files…")
                    status_txt.caption(f"{done:,} files processed")

                with st.spinner("Crawling…"):
                    inserted, skipped = _crawl_and_insert(
                        engine, crawl_root, sel_exts or [], _cb
                    )
                prog.empty(); status_txt.empty()
                st.success(f"✅ Crawl complete — **{inserted:,}** new files added, "
                           f"**{skipped:,}** already in inventory.")
                st.rerun()

    # ── Inventory grid ────────────────────────────────────────────────
    st.markdown("### Current Inventory")

    # Filters
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        dtg_opts = ["All"] + list(DOC_GROUP_ICONS.keys())
        dtg_filter = st.selectbox("Document type", dtg_opts, key="dvc_dtg_filter")
    with fc2:
        status_opts = ["All"] + STATUS_OPTS
        status_filter = st.selectbox("Status", status_opts, key="dvc_status_filter")
    with fc3:
        assignee_filter = st.text_input("Assigned to", key="dvc_assignee_filter",
                                         placeholder="username or blank for all")

    filters = {}
    if dtg_filter != "All":   filters["doc_type_group"] = dtg_filter
    if status_filter != "All": filters["catalog_status"] = status_filter
    if assignee_filter:        filters["assigned_to"]    = assignee_filter

    df = _load_inventory(engine, filters)

    if df.empty:
        st.info("No files in inventory yet. Use the crawl panel above.")
        return

    st.caption(f"**{len(df):,}** files shown")

    # Add icon column
    df.insert(0, "Type", df["DOC_TYPE_GROUP"].map(
        lambda g: DOC_GROUP_ICONS.get(g, "📁") + " " + str(g)
    ))

    display_cols = ["Type", "FILE_NAME", "DOC_TYPE", "CATALOG_STATUS",
                    "FILE_SIZE_KB", "UWI", "WELL_NAME", "ASSIGNED_TO", "FULL_PATH"]
    display_cols = [c for c in display_cols if c in df.columns]

    st.dataframe(df[display_cols], hide_index=True, use_container_width=True,
                 height=400)

    # Quick bulk status update
    st.divider()
    bu1, bu2, bu3 = st.columns([3, 2, 2])
    with bu1:
        st.caption("Bulk update — type INVENTORY_IDs comma-separated or use Work Queue tab")
    with bu2:
        bulk_status = st.selectbox("Set status", STATUS_OPTS, key="dvc_bulk_status")
    with bu3:
        if st.button("Apply to filtered", key="dvc_bulk_apply"):
            ids = df["INVENTORY_ID"].tolist()
            n = _update_status(engine, ids, bulk_status)
            st.success(f"Updated {n} files to {bulk_status}")
            st.rerun()


# =============================================================================
# TAB 2 — WORK QUEUE
# =============================================================================

def _render_work_queue(engine):
    st.subheader("📋 Work Queue")
    st.caption("Select files for batch catalog or individual inspection.")

    # Load uncataloged / in-review files
    df = _load_inventory(engine, {"catalog_status": "UNCATALOGED"})
    df2 = _load_inventory(engine, {"catalog_status": "IN_REVIEW"})
    df  = pd.concat([df, df2], ignore_index=True)

    if df.empty:
        st.success("✅ Work queue is empty — all files are cataloged or skipped.")
        return

    st.caption(f"**{len(df):,}** files pending")

    # Add two checkbox columns
    df.insert(0, "🔍 Inspect", False)
    df.insert(0, "✓ Catalog", False)

    display_cols = ["✓ Catalog", "🔍 Inspect", "DOC_TYPE_GROUP", "FILE_NAME",
                    "DOC_TYPE", "FILE_SIZE_KB", "UWI", "WELL_NAME",
                    "CATALOG_STATUS", "FULL_PATH", "INVENTORY_ID"]
    display_cols = [c for c in display_cols if c in df.columns]

    edited = st.data_editor(
        df[display_cols],
        hide_index=True,
        use_container_width=True,
        height=min(40 * len(df) + 38, 500),
        column_config={
            "✓ Catalog":  st.column_config.CheckboxColumn("✓ Catalog",  width="small"),
            "🔍 Inspect": st.column_config.CheckboxColumn("🔍 Inspect", width="small"),
            "INVENTORY_ID": st.column_config.Column(disabled=True, width="small"),
            "FULL_PATH":    st.column_config.Column(disabled=True),
            "FILE_NAME":    st.column_config.Column(disabled=True),
            "DOC_TYPE":     st.column_config.Column(disabled=True, width="small"),
            "DOC_TYPE_GROUP": st.column_config.Column(disabled=True, width="small"),
            "FILE_SIZE_KB": st.column_config.NumberColumn(disabled=True, width="small"),
            "UWI":          st.column_config.Column(disabled=True),
            "WELL_NAME":    st.column_config.Column(disabled=True),
            "CATALOG_STATUS": st.column_config.Column(disabled=True, width="small"),
        },
        disabled=["INVENTORY_ID","FULL_PATH","FILE_NAME","DOC_TYPE",
                  "DOC_TYPE_GROUP","FILE_SIZE_KB","UWI","WELL_NAME","CATALOG_STATUS"],
        key="dvc_queue_editor",
    )

    # Toolbar
    n_catalog = int(edited["✓ Catalog"].sum())
    n_inspect  = int(edited["🔍 Inspect"].sum())

    tb1, tb2, tb3, tb4, tb5 = st.columns([1, 1, 1, 1, 3])
    with tb1:
        if st.button("☑ All Catalog", key="dvc_all_cat"):
            st.session_state["dvc_select_all_cat"] = True
            st.rerun()
    with tb2:
        if st.button("☑ All Inspect", key="dvc_all_insp"):
            st.session_state["dvc_select_all_insp"] = True
            st.rerun()
    with tb3:
        if st.button("☐ Clear All", key="dvc_clear_all"):
            st.session_state.pop("dvc_select_all_cat",  None)
            st.session_state.pop("dvc_select_all_insp", None)
            st.session_state.pop("dvc_queue_editor",    None)
            st.rerun()
    with tb5:
        parts = []
        if n_catalog: parts.append(f"**{n_catalog}** for catalog")
        if n_inspect:  parts.append(f"**{n_inspect}** for inspection")
        if parts:
            st.caption("Selected: " + " · ".join(parts))

    # Catalog selected
    catalog_ids = edited[edited["✓ Catalog"] == True]["INVENTORY_ID"].tolist()
    inspect_paths = edited[edited["🔍 Inspect"] == True]["FULL_PATH"].tolist()
    inspect_ids   = edited[edited["🔍 Inspect"] == True]["INVENTORY_ID"].tolist()

    st.divider()
    act1, act2 = st.columns(2)

    with act1:
        if catalog_ids:
            who = st.text_input("Cataloged by", value="CATALOGER",
                                key="dvc_cat_who")
            if st.button(f"📥 Catalog {len(catalog_ids)} file(s)",
                         type="primary", key="dvc_do_catalog"):
                n = _update_status(engine, catalog_ids, "CATALOGED", who)
                st.success(f"✅ {n} file(s) marked as CATALOGED")
                st.session_state.pop("dvc_queue_editor", None)
                st.rerun()
        else:
            st.caption("Check **✓ Catalog** to batch-catalog files without inspection.")

    with act2:
        if inspect_paths:
            if st.button(f"🔍 Inspect {len(inspect_paths)} file(s)",
                         type="primary", key="dvc_do_inspect"):
                st.session_state["dvc_inspect_paths"] = inspect_paths
                st.session_state["dvc_inspect_ids"]   = inspect_ids
                st.session_state["dvc_inspect_idx"]   = 0
                st.session_state["dvc_active_tab"]    = "inspect"
                st.rerun()
        else:
            st.caption("Check **🔍 Inspect** to review files before cataloging.")

    # Skip selected
    if catalog_ids or inspect_paths:
        all_selected = list(set(catalog_ids + inspect_ids))
        if st.button(f"⏭ Skip {len(all_selected)} selected",
                     key="dvc_skip_selected"):
            n = _update_status(engine, all_selected, "SKIPPED")
            st.warning(f"⏭ {n} file(s) marked as SKIPPED")
            st.session_state.pop("dvc_queue_editor", None)
            st.rerun()


# =============================================================================
# TAB 3 — INSPECT
# =============================================================================


def _render_inspect(engine):
    st.subheader("🔍 File Inspector")

    paths = st.session_state.get("dvc_inspect_paths", [])
    ids   = st.session_state.get("dvc_inspect_ids",   [])
    idx   = st.session_state.get("dvc_inspect_idx",   0)

    if not paths:
        st.info("Select files in the **📋 Work Queue** tab and click Inspect.")
        st.divider()
        manual = st.text_input("Or enter a file path directly:",
                               key="dvc_manual_inspect",
                               placeholder=r"e.g. C:\Bulk\raw\well_001.las")
        if manual and os.path.exists(manual):
            paths = [manual]
            ids   = ["manual"]
            idx   = 0
        elif manual:
            st.error(f"File not found: `{manual}`")
            return
        else:
            return

    # Navigation
    total = len(paths)
    nav1, nav2, nav3, nav4 = st.columns([1, 2, 1, 4])
    with nav1:
        if st.button("← Prev", key="dvc_prev_file", disabled=idx == 0):
            st.session_state["dvc_inspect_idx"] = idx - 1
            st.rerun()
    with nav2:
        st.caption(f"File **{idx+1}** of **{total}**")
    with nav3:
        if st.button("Next →", key="dvc_next_file", disabled=idx >= total - 1):
            st.session_state["dvc_inspect_idx"] = idx + 1
            st.rerun()

    file_path = paths[idx]
    inv_id    = ids[idx] if idx < len(ids) else ""
    ext       = Path(file_path).suffix.lower()
    dtg, dt   = get_doc_type(ext)

    st.markdown(f"### {DOC_GROUP_ICONS.get(dtg,'📁')} `{Path(file_path).name}`")
    st.caption(file_path)

    # File summary
    try:
        stat = os.stat(file_path)
        sm1, sm2, sm3, sm4 = st.columns(4)
        sm1.metric("Type",      f"{dtg} / {dt}")
        sm2.metric("Size",      f"{stat.st_size/1024:.1f} KB")
        sm3.metric("Modified",  datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d"))
        sm4.metric("Extension", ext)
    except Exception as exc:
        st.error(f"Cannot read file: {exc}")
        return

    st.divider()

    # ── Unified View + Extract + Load panel ─────────────────────────
    if ext == ".pdf":
        _render_pdf_panel(engine, file_path, inv_id, idx)
    else:
        try:
            from dataview.file_catalog.page_file_workbench import render_workbench
            render_workbench(file_path, key=f"wb_{idx}")
        except ImportError:
            st.warning("page_file_workbench.py not found in project root.")
        except Exception as e:
            st.error(f"Inspector error: {e}")

    # ── Decision buttons ──────────────────────────────────────────────
    if inv_id and inv_id != "manual":
        st.divider()
        st.markdown("**Decision:**")
        dec1, dec2, dec3, dec4 = st.columns([2, 2, 2, 4])
        with dec1:
            who = st.text_input("By", value="CATALOGER",
                                key=f"dvc_dec_who_{idx}",
                                label_visibility="collapsed")
        with dec2:
            if st.button("✅ Catalog this file", type="primary",
                         key=f"dvc_cat_{idx}"):
                _update_status(engine, [inv_id], "CATALOGED", who)
                st.success("Marked as CATALOGED")
                if idx < total - 1:
                    st.session_state["dvc_inspect_idx"] = idx + 1
                    st.rerun()
        with dec3:
            if st.button("⏭ Skip", key=f"dvc_skip_{idx}"):
                _update_status(engine, [inv_id], "SKIPPED", who)
                st.warning("Marked as SKIPPED")
                if idx < total - 1:
                    st.session_state["dvc_inspect_idx"] = idx + 1
                    st.rerun()



def _render_admin(engine):
    st.subheader("⚙ Admin")

    with st.expander("🏗 Vault Setup", expanded=True):
        vault_root = st.text_input(
            "Vault root path",
            value=VAULT_ROOT,
            key="dvc_admin_vault")
        if st.button("Create vault structure", key="dvc_create_vault_btn"):
            try:
                paths = ensure_vault(vault_root)
                st.success("Created: " + " · ".join(
                    f"`{tier}`" for tier in paths
                ))
            except Exception as exc:
                st.error(str(exc))

        st.caption("Vault tiers: **raw** (incoming) → **curated** (matched/cataloged) "
                   "→ **enriched** (data extracted) → **archive**")

    with st.expander("📊 Inventory Stats by Type"):
        try:
            sql = f"""
                SELECT DOC_TYPE_GROUP, DOC_TYPE, CATALOG_STATUS,
                       COUNT(*) AS cnt,
                       SUM(FILE_SIZE_KB)/1024.0 AS mb
                FROM {GFC_TABLE}
                GROUP BY DOC_TYPE_GROUP, DOC_TYPE, CATALOG_STATUS
                ORDER BY DOC_TYPE_GROUP, DOC_TYPE, CATALOG_STATUS
            """
            with engine.connect() as con:
                stats_df = pd.read_sql(text(sql), con)
            if not stats_df.empty:
                st.dataframe(stats_df, hide_index=True, use_container_width=True)
        except Exception as exc:
            st.error(str(exc))

    with st.expander("🗑 Maintenance"):
        st.warning("These operations cannot be undone.")
        if st.button("Clear SKIPPED files from inventory",
                     key="dvc_clear_skipped"):
            try:
                with engine.begin() as con:
                    r = con.execute(text(
                        f"DELETE FROM {GFC_TABLE} WHERE CATALOG_STATUS='SKIPPED'"
                    ))
                st.success(f"Removed {r.rowcount} SKIPPED files")
            except Exception as exc:
                st.error(str(exc))

        if st.button("Reset all UNCATALOGED (re-scan)",
                     key="dvc_reset_uncataloged"):
            try:
                with engine.begin() as con:
                    r = con.execute(text(
                        f"DELETE FROM {GFC_TABLE} WHERE CATALOG_STATUS='UNCATALOGED'"
                    ))
                st.success(f"Removed {r.rowcount} UNCATALOGED entries — re-crawl to repopulate")
            except Exception as exc:
                st.error(str(exc))


# =============================================================================
# MAIN ENTRY
# =============================================================================

def run(engine=None):
    st.title("🗂 DataView — Document Inventory")

    if engine is None:
        st.info("Connect to the DataView database first.")
        return

    # Ensure the inventory table exists
    try:
        with engine.begin() as con:
            con.execute(text(f"""
                IF NOT EXISTS (
                    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA='dataview'
                      AND TABLE_NAME='dv_global_file_catalog'
                )
                CREATE TABLE [dataview].[dv_global_file_catalog] (
                    [INVENTORY_ID]     NVARCHAR(40)   NOT NULL,
                    [FULL_PATH]        NVARCHAR(1000) NOT NULL,
                    [FILE_NAME]        NVARCHAR(500)  NOT NULL,
                    [FILE_EXT]         NVARCHAR(20)   NULL,
                    [FILE_SIZE_KB]     NUMERIC(15,2)  NULL,
                    [FILE_HASH]        NVARCHAR(64)   NULL,
                    [FILE_HASH_FULL]   NVARCHAR(64)   NULL,
                    [DUPLICATE_GROUP]  NVARCHAR(64)   NULL,
                    [MODIFIED_DATE]    DATETIME2      NULL,
                    [SCAN_DATE]        DATETIME2      NOT NULL DEFAULT GETDATE(),
                    [DOC_TYPE_GROUP]   NVARCHAR(40)   NULL,
                    [DOC_TYPE]         NVARCHAR(40)   NULL,
                    [CATALOG_STATUS]   NVARCHAR(20)   NULL DEFAULT 'UNCATALOGED',
                    [CATALOG_TABLE]    NVARCHAR(80)   NULL,
                    [CATALOG_ID]       NVARCHAR(40)   NULL,
                    [PPDM_LOADED_IND]  NVARCHAR(1)    NOT NULL DEFAULT 'N',
                    [ROOT_PATH]        NVARCHAR(500)  NULL,
                    [UWI]              NVARCHAR(40)   NULL,
                    [WELL_NAME]        NVARCHAR(255)  NULL,
                    [ROW_CREATED_BY]   NVARCHAR(40)   NOT NULL DEFAULT 'SYSTEM',
                    [ROW_CREATED_DATE] DATETIME2      NOT NULL DEFAULT GETDATE(),
                    [ROW_CHANGED_BY]   NVARCHAR(40)   NULL,
                    [ROW_CHANGED_DATE] DATETIME2      NULL,
                    [SOURCE]           NVARCHAR(40)   NULL,
                    CONSTRAINT [PK_dv_global_file_catalog]
                        PRIMARY KEY ([INVENTORY_ID])
                )
            """))
    except Exception:
        pass  # Already exists

    # ── Tab routing from work queue ───────────────────────────────────
    active = st.session_state.get("dvc_active_tab", "inventory")
    if active == "inspect" and st.session_state.get("dvc_inspect_paths"):
        tab_labels = ["🗂 Inventory", "📋 Work Queue", "🔍 Inspect", "⚙ Admin"]
        default_tab = 2
    else:
        tab_labels = ["🗂 Inventory", "📋 Work Queue", "🔍 Inspect", "⚙ Admin"]
        default_tab = 0

    tab_inv, tab_queue, tab_inspect, tab_admin = st.tabs(tab_labels)

    with tab_inv:
        _render_inventory(engine)

    with tab_queue:
        _render_work_queue(engine)

    with tab_inspect:
        _render_inspect(engine)

    with tab_admin:
        _render_admin(engine)

    # Clear tab routing flag after render
    st.session_state.pop("dvc_active_tab", None)
