"""
page_las_loader.py

Data Wrangler — Direct LAS → PPDM Loader.

Bypasses the 8-stage pipeline. Assumes WELL already exists in the database.
Loads WELL_LOG, WELL_LOG_CURVE, and optionally WELL_LOG_CURVE_VALUE directly.

Two modes:
  Single file  — upload one LAS, verify/override UWI, promote
  Batch folder — enter a directory path, scan all LAS files, match UWIs
                 in a table, correct mismatches, then promote all
"""

import json
from pathlib import Path

import pandas as pd
import streamlit as st

try:
    from dataview.file_catalog.las_loader import (
        extract_las_uwi,
        fetch_ppdm_uwis,
        fuzzy_match_uwi,
        scan_las_directory,
        auto_match_batch,
        promote_las_file,
    )
    import lasio
    from dataview.file_catalog.las_reader import read_las
    _AVAILABLE = True
except ImportError as _err:
    _AVAILABLE = False
    _IMPORT_ERROR = str(_err)

# Session state key for persisting the last-used folder path
_FOLDER_KEY   = "las_loader_folder"
_SOURCE_KEY   = "las_loader_source"
_SCAN_KEY     = "las_loader_scan_df"
_PPDM_UWI_KEY = "las_loader_ppdm_uwis"


def run():
    st.title("🪵 LAS Loader")
    st.caption("Direct LAS → PPDM promote. WELL must already exist in the database.")

    if not _AVAILABLE:
        st.error(
            f"LAS Loader dependencies missing:\n\n`{_IMPORT_ERROR}`\n\n"
            "Run `pip install lasio` then restart."
        )
        return

    engine = _get_engine()
    if engine is None:
        st.warning("No database connection. Connect via the main pipeline first.")
        return

    # ── Global options (open by default, stacked) ────────────────────
    with st.expander("⚙  Options", expanded=True):
        source = st.text_input(
            "SOURCE tag",
            value=st.session_state.get(_SOURCE_KEY, "LAS_IMPORT"),
            key="las_source_input",
            help="Stamped on every inserted row as SOURCE.",
        )
        st.session_state[_SOURCE_KEY] = source

        load_values = st.checkbox(
            "Load WELL_LOG_CURVE_VALUE (depth samples)",
            value=False,
            key="las_load_values",
            help="Can produce millions of rows per file. Leave off for catalogue-only loads.",
        )

    tab_single, tab_batch = st.tabs(["📄 Single File", "📁 Batch Directory"])

    with tab_single:
        _render_single(engine, source, load_values)

    with tab_batch:
        _render_batch(engine, source, load_values)


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE FILE TAB
# ─────────────────────────────────────────────────────────────────────────────

def _render_single(engine, source: str, load_values: bool):
    st.subheader("Upload a single LAS file")

    uploaded = st.file_uploader(
        "Choose a LAS file",
        type=["las"],
        key="las_single_uploader",
    )

    if not uploaded:
        return

    # Parse header only (fast)
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".las", delete=False) as tmp:
        tmp.write(uploaded.read())
        tmp_path = tmp.name

    try:
        las = read_las(tmp_path, ignore_header_errors=True)
    except Exception as e:
        st.error(f"Could not parse LAS file: {e}")
        return

    las_uwi = extract_las_uwi(las)

    # ── Header summary ────────────────────────────────────────────────
    col_a, col_b, col_c = st.columns(3)
    col_a.metric("Curves", len(las.curves))
    try:
        n_depth = len(las[las.curves[0].mnemonic]) if las.curves else 0
    except Exception:
        n_depth = 0
    col_b.metric("Depth samples", f"{n_depth:,}")
    col_c.metric("LAS UWI", las_uwi or "Not found")

    st.divider()

    # ── UWI matching ─────────────────────────────────────────────────
    st.markdown("**Match to PPDM well**")

    with st.spinner("Fetching wells from database…"):
        try:
            ppdm_uwis = fetch_ppdm_uwis(engine)
        except Exception as e:
            st.error(str(e))
            return

    matches = fuzzy_match_uwi(las_uwi, ppdm_uwis) if las_uwi else []

    if matches and matches[0]["score"] == 100:
        default_uwi = matches[0]["UWI"]
        st.success(f"Exact match found: **{default_uwi}**")
    elif matches:
        default_uwi = matches[0]["UWI"]
        st.warning(
            f"No exact match. Best guess: **{default_uwi}** "
            f"({matches[0]['score']}% confidence) — verify below."
        )
    else:
        default_uwi = ""
        st.warning("No match found. Enter the correct UWI manually.")

    # Match table
    if matches:
        with st.expander("Show all candidates", expanded=False):
            st.dataframe(
                pd.DataFrame(matches)[["UWI", "WELL_NAME", "score"]],
                use_container_width=True,
                hide_index=True,
            )

    # UWI selector — searchable dropdown from PPDM wells + manual override
    uwi_options = [r["UWI"] for r in ppdm_uwis]
    if default_uwi and default_uwi in uwi_options:
        default_idx = uwi_options.index(default_uwi)
    else:
        default_idx = 0

    selected_uwi = st.selectbox(
        "Select PPDM UWI to load against",
        options=uwi_options,
        index=default_idx,
        key="las_single_uwi_select",
    )

    # ── Promote ───────────────────────────────────────────────────────
    st.divider()
    schema = _get_schema(engine)

    value_warning = (
        f"  \n⚠  This will also insert **{n_depth * max(len(las.curves)-1, 0):,}** "
        f"sample values — may take a while."
        if load_values and n_depth > 0 else ""
    )

    if st.button(
        f"➕ Load '{uploaded.name}' → {selected_uwi}",
        type="primary",
        key="las_single_promote_btn",
    ):
        with st.spinner("Inserting into PPDM…"):
            result = promote_las_file(
                las_path=tmp_path,
                original_path=uploaded.name,   # original filename for catalog
                uwi=selected_uwi,
                engine=engine,
                source=source,
                load_values=load_values,
                schema=schema,
            )

        if result["ok"]:
            st.success(
                f"✅ Loaded successfully  \n"
                f"WELL_LOG: {result['log_rows']} row  \n"
                f"WELL_LOG_CURVE: {result['curve_rows']} rows  \n"
                f"WELL_LOG_CURVE_AXIS: {result['axis_rows']} rows  \n"
                f"WELL_LOG_CURVE_VALUE: {result['value_rows']} rows"
            )
        else:
            st.error(f"Load failed: {result['error']}")


# ─────────────────────────────────────────────────────────────────────────────
# BATCH DIRECTORY TAB
# ─────────────────────────────────────────────────────────────────────────────

def _render_batch(engine, source: str, load_values: bool):
    st.subheader("Load all LAS files from a directory")

    # ── Folder path (persisted) ───────────────────────────────────────
    folder = st.text_input(
        "Directory path",
        value=st.session_state.get(_FOLDER_KEY, ""),
        key="las_batch_folder",
        placeholder=r"e.g. C:\Data\LAS_Files",
        help="All .las files in this folder will be scanned.",
    )
    if folder:
        st.session_state[_FOLDER_KEY] = folder

    col_scan, col_clear = st.columns([2, 1])
    with col_scan:
        scan_clicked = st.button(
            "🔍 Scan directory",
            key="las_batch_scan_btn",
            disabled=not folder,
        )
    with col_clear:
        if st.button("🗑 Clear scan", key="las_batch_clear_btn"):
            for k in (_SCAN_KEY, _PPDM_UWI_KEY):
                st.session_state.pop(k, None)
            st.rerun()

    if scan_clicked and folder:
        with st.spinner("Scanning directory and fetching PPDM wells…"):
            try:
                scan_df   = scan_las_directory(folder)
                ppdm_uwis = fetch_ppdm_uwis(engine)
                scan_df   = auto_match_batch(scan_df, ppdm_uwis)
                st.session_state[_SCAN_KEY]     = scan_df.to_json()
                st.session_state[_PPDM_UWI_KEY] = json.dumps(ppdm_uwis)
            except Exception as e:
                st.error(str(e))
                return
        st.rerun()

    if _SCAN_KEY not in st.session_state:
        return

    # ── Editable match table ──────────────────────────────────────────
    scan_df   = pd.read_json(st.session_state[_SCAN_KEY])
    ppdm_uwis = json.loads(st.session_state[_PPDM_UWI_KEY])
    uwi_options = [""] + [r["UWI"] for r in ppdm_uwis]

    st.markdown(f"**Found {len(scan_df)} LAS file(s)** — verify UWI matches below, then promote.")

    # Status summary
    matched  = (scan_df["STATUS"].str.startswith("Matched")).sum()
    no_match = (scan_df["STATUS"] == "No match — manual required").sum()
    errors   = (scan_df["STATUS"] == "Error").sum()
    c1, c2, c3 = st.columns(3)
    c1.metric("Auto-matched", matched)
    c2.metric("Needs review", no_match)
    c3.metric("Parse errors", errors)

    st.divider()
    st.caption("Edit the PPDM UWI column for any rows that need correction.")

    # Build editable grid using st.data_editor
    display_df = scan_df[["FILE_NAME", "LAS_UWI", "PPDM_UWI", "STATUS"]].copy()

    edited_df = st.data_editor(
        display_df,
        key="las_batch_editor",
        use_container_width=True,
        hide_index=True,
        column_config={
            "FILE_NAME": st.column_config.TextColumn("File", disabled=True),
            "LAS_UWI":   st.column_config.TextColumn("LAS UWI", disabled=True),
            "PPDM_UWI":  st.column_config.SelectboxColumn(
                "PPDM UWI",
                options=uwi_options,
                required=False,
            ),
            "STATUS":    st.column_config.TextColumn("Status", disabled=True),
        },
    )

    # Merge edits back into full scan_df (which has FILE_PATH)
    scan_df["PPDM_UWI"] = edited_df["PPDM_UWI"].values

    ready_df = scan_df[
        scan_df["PPDM_UWI"].notna() & (scan_df["PPDM_UWI"] != "")
    ]
    pending = len(scan_df) - len(ready_df)

    st.divider()

    if pending > 0:
        st.info(f"{pending} file(s) have no PPDM UWI assigned and will be skipped.")

    schema = _get_schema(engine)

    value_note = " + depth samples" if load_values else ""
    if st.button(
        f"🚀 Promote {len(ready_df)} file(s){value_note} to PPDM",
        type="primary",
        key="las_batch_promote_btn",
        disabled=len(ready_df) == 0,
    ):
        _run_batch_promote(ready_df, engine, source, load_values, schema)


def _run_batch_promote(ready_df: pd.DataFrame, engine, source: str,
                       load_values: bool, schema: str):
    """Promote all ready rows with a progress bar."""
    results = []
    progress = st.progress(0, text="Starting…")
    total = len(ready_df)

    for i, (_, row) in enumerate(ready_df.iterrows()):
        progress.progress(
            (i) / total,
            text=f"Loading {row['FILE_NAME']} ({i+1}/{total})…"
        )
        result = promote_las_file(
            las_path=row["FILE_PATH"],
            uwi=row["PPDM_UWI"],
            engine=engine,
            source=source,
            load_values=load_values,
            schema=schema,
        )
        results.append({
            "File":        row["FILE_NAME"],
            "UWI":         row["PPDM_UWI"],
            "Log rows":    result["log_rows"],
            "Curve rows":  result["curve_rows"],
            "Axis rows":   result["axis_rows"],
            "Value rows":  result["value_rows"],
            "Status":      "✅ OK" if result["ok"] else f"❌ {result['error']}",
        })

    progress.progress(1.0, text="Done.")

    result_df = pd.DataFrame(results)
    ok_count  = (result_df["Status"] == "✅ OK").sum()
    err_count = len(result_df) - ok_count

    if err_count == 0:
        st.success(f"All {ok_count} file(s) loaded successfully.")
    else:
        st.warning(f"{ok_count} succeeded, {err_count} failed.")

    st.dataframe(result_df, use_container_width=True, hide_index=True)

    # Save results to session for review
    st.session_state["las_batch_results"] = result_df.to_json()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_engine():
    """Get engine from session state — same object used by the main pipeline."""
    engine = st.session_state.get("engine")
    if engine is not None:
        return engine
    # Fallback: db_pool for cases where session state was reset
    try:
        from dataview.core.db_pool import get_engine
        return get_engine()
    except ImportError:
        return None


def _get_schema(engine) -> str:
    """Return the appropriate schema/owner for the dialect."""
    try:
        name = engine.dialect.name.lower()
        if "oracle" in name:
            from sqlalchemy import text
            with engine.connect() as con:
                return con.execute(text(
                    "SELECT SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM dual"
                )).scalar() or "dbo"
        if "snowflake" in name:
            from sqlalchemy import text
            with engine.connect() as con:
                return con.execute(text("SELECT CURRENT_SCHEMA()")).scalar() or "dbo"
    except Exception:
        pass
    return "dbo"


if __name__ == "__main__":
    run()
