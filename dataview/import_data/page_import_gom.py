"""
page_import_gom.py — Streamlit UI for the GOM well header loader
=================================================================

Self-contained render function. Drop into your Import Data page by either:

  Option A: Add a new tab and call render() inside it:
      tab1, tab2, tab3 = st.tabs(["Shapefiles", "Files into DB", "GOM Wells"])
      with tab3:
          from dataview.import_data.page_import_gom import render as render_gom
          render_gom(engine)

  Option B: Standalone section:
      st.divider()
      from dataview.import_data.page_import_gom import render as render_gom
      render_gom(engine)

The render function takes a SQLAlchemy engine and returns nothing.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st


def render(engine) -> None:
    """Render the GOM well loader UI section."""
    st.header("🌊 GOM Well Header Loader")
    st.caption(
        "Bulk-load BOEM Gulf of America well headers into "
        "`dataview_gom.well` with universal IDs registered in "
        "`dataview.dv_well_identifier`. "
        "Re-running on the same file safely updates in place (idempotent)."
    )

    # Two ways to provide the file:
    #   1. Drag-and-drop / browse (preferred — easy, no path typing)
    #   2. Server-side path (fallback for >200 MB files or batch automation)
    # If both are provided, the uploaded file wins.

    uploaded = st.file_uploader(
        "Drop BOEM file here, or browse",
        type=["xlsx", "xls", "tsv", "txt", "csv"],

        key="gom_loader_upload",
    )

    # Server-side path fallback for files too large to upload (>200MB) or
    # for batch / automated runs. Toggle is a checkbox rather than an
    # expander because this render() may itself be called inside an
    # expander (Streamlit forbids nested expanders).
    use_path = st.checkbox(
        "Or use a server-side file path (for large files)",
        value=False,
        key="gom_loader_use_path",
    )
    path_input = ""
    if use_path:
        default_path = st.session_state.get(
            "gom_loader_path",
            r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\data_wrangler"
            r"\training\reference_data\boem_gom_wells.xlsx"
        )
        path_input = st.text_input(
            "Absolute path",
            value=default_path,

            key="gom_loader_path_input",
        )
        st.session_state["gom_loader_path"] = path_input

    chunk_size = st.number_input(
        "Chunk size",
        min_value=500, max_value=20000, value=5000, step=500,

    )

    # ── Resolve which source to use ──────────────────────────────────────
    # Uploaded file wins. If not, use the path. Either way, end up with a
    # filesystem path that the loader can open.
    file_path: str = ""
    cleanup_temp: bool = False

    if uploaded is not None:
        # Streamlit gives us an in-memory UploadedFile. The loader opens
        # files from disk, so we materialize it to a temp file.
        import tempfile
        suffix = Path(uploaded.name).suffix or ".xlsx"
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=suffix, prefix="gom_upload_"
        ) as tmp:
            tmp.write(uploaded.getbuffer())
            file_path = tmp.name
        cleanup_temp = True
        st.caption(
            f"📤 Uploaded **{uploaded.name}** "
            f"({uploaded.size / (1024*1024):.1f} MB)"
        )
    elif path_input:
        p = Path(path_input)
        if not p.exists():
            st.error(f"File not found: `{path_input}`")
            return
        file_path = str(p)
        size_mb = p.stat().st_size / (1024 * 1024)
        st.caption(f"📄 Using server path — {size_mb:.1f} MB")
    else:
        st.info("Drop a file above, or expand the section below to use a server-side path.")
        return

    # ── Load button ──────────────────────────────────────────────────────
    if not st.button(
        "🚀 Load GOM wells",
        type="primary",
        key="gom_loader_btn",
        use_container_width=True,
    ):
        # Clean up the temp file if we created one but the user didn't load yet.
        # Streamlit reruns on every interaction, so leaving temp files behind
        # would accumulate. The uploaded data is in session_state regardless;
        # the temp file is just a path the loader needs.
        if cleanup_temp:
            try:
                import os
                os.unlink(file_path)
            except Exception:
                pass
        return

    # ── Run the load ─────────────────────────────────────────────────────
    from dataview.import_data.gom_well_loader import load_gom_wells

    progress = st.progress(0.0, text="Starting…")
    status   = st.empty()

    def _on_progress(done: int, total: int, msg: str):
        pct = min(1.0, done / max(total, 1)) if total else 0.0
        try:
            progress.progress(pct, text=f"{done:,} / {total:,} · {msg}")
        except Exception:
            pass

    try:
        stats = load_gom_wells(
            engine,
            file_path=file_path,
            chunk_size=int(chunk_size),
            progress_callback=_on_progress,
        )
    except FileNotFoundError as e:
        st.error(f"File error: {e}")
        return
    except ValueError as e:
        st.error("Validation error — file did not match the expected format:")
        st.code(str(e))
        return
    except Exception as e:
        st.error(f"Loader failed: {type(e).__name__}: {e}")
        return
    finally:
        # Always remove the temp file from drag-and-drop, regardless of outcome.
        if cleanup_temp:
            try:
                import os
                os.unlink(file_path)
            except Exception:
                pass

    progress.empty()
    status.empty()

    # ── Summary ──────────────────────────────────────────────────────────
    st.success("✅ Load complete.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📋 Rows read",         f"{stats['total_rows']:,}")
    c2.metric("🆔 Identifiers loaded", f"{stats['loaded_identifiers']:,}")
    c3.metric("🌊 Wells loaded",       f"{stats['loaded_wells']:,}")
    c4.metric("⚠️ Parse errors",      f"{stats['parse_errors']:,}")

    if stats["error_samples"]:
        # Was an expander; flattened to a markdown heading because this
        # whole render() may be called inside an outer expander.
        st.markdown(
            f"**⚠️ {stats['parse_errors']} error(s) — first 10 examples:**"
        )
        for err in stats["error_samples"]:
            st.text(err)

    # Quick post-load sanity check
    st.markdown("**🔎 Post-load sanity check**")
    try:
        from sqlalchemy import text
        with engine.connect() as con:
            gom_count = con.execute(text(
                "SELECT COUNT(*) FROM dataview_gom.well"
            )).scalar() or 0
            ident_count = con.execute(text(
                "SELECT COUNT(*) FROM dataview.dv_well_identifier "
                "WHERE source_system = 'BOEM'"
            )).scalar() or 0
            area_counts = con.execute(text("""
                SELECT TOP 10
                    bottom_area_code,
                    COUNT(*) AS wells
                FROM dataview_gom.well
                WHERE bottom_area_code IS NOT NULL
                GROUP BY bottom_area_code
                ORDER BY wells DESC
            """)).fetchall()

        st.write(f"**Total in `dataview_gom.well`:** {gom_count:,}")
        st.write(
            f"**Total BOEM identifiers in `dataview.dv_well_identifier`:** "
            f"{ident_count:,}"
        )
        if area_counts:
            st.write("**Top 10 OCS areas by well count:**")
            import pandas as pd
            st.dataframe(
                pd.DataFrame(area_counts, columns=["Area", "Wells"]),
                use_container_width=True,
                hide_index=True,
            )
    except Exception as e:
        st.warning(f"Sanity check query failed: {e}")
