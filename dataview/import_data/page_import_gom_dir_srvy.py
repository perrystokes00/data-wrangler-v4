"""
page_import_gom_dir_srvy.py — Streamlit UI for the GOM directional survey loader
=================================================================================

Self-contained render function, sibling to page_import_gom.py. Drop into
your Import Data page the same way:

  Option A: Add a new tab and call render() inside it:
      with tab4:
          from dataview.import_data.page_import_gom_dir_srvy import render as render_gom_srvy
          render_gom_srvy(engine)

  Option B: Standalone section:
      st.divider()
      from dataview.import_data.page_import_gom_dir_srvy import render as render_gom_srvy
      render_gom_srvy(engine)

The render function takes a SQLAlchemy engine and returns nothing.

Note: the BOEM Azimuth survey file (directfixed.txt) is HEADERLESS
FIXED-WIDTH, so this loader does not accept Excel/CSV — only the .txt
fixed-width file. The loader module parses it by fixed column spans.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st


def render(engine) -> None:
    """Render the GOM directional survey loader UI section."""
    st.header("🌊 GOM Directional Survey Loader")
    st.caption(
        "Bulk-load BOEM Gulf of America directional survey points (the "
        "Azimuth `directfixed.txt` file) into "
        "`dataview_gom.directional_survey_point`. One row per survey "
        "station. `api_well_number` is stored raw — linking each survey "
        "to `dataview_gom.well` (well_id resolution) is a separate "
        "follow-up step. Re-running on the same file safely updates in "
        "place (idempotent)."
    )

    # Two ways to provide the file:
    #   1. Drag-and-drop / browse
    #   2. Server-side path — the realistic choice here, since the BOEM
    #      directional file is large. If both are given, the upload wins.
    # The Azimuth file is fixed-width .txt only — no Excel/CSV here.
    uploaded = st.file_uploader(
        "Drop BOEM directional survey file here, or browse",
        type=["txt"],

        key="gom_srvy_loader_upload",
    )

    # Server-side path fallback — checkbox not expander, because this
    # render() may itself be called inside an expander (Streamlit forbids
    # nested expanders).
    use_path = st.checkbox(
        "Or use a server-side file path (for large files)",
        value=False,
        key="gom_srvy_loader_use_path",
    )
    path_input = ""
    if use_path:
        default_path = st.session_state.get(
            "gom_srvy_loader_path",
            r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\data_wrangler"
            r"\training\GOM\dir_srvy_pts\directfixed.txt"
        )
        path_input = st.text_input(
            "Absolute path",
            value=default_path,

            key="gom_srvy_loader_path_input",
        )
        st.session_state["gom_srvy_loader_path"] = path_input

    chunk_size = st.number_input(
        "Chunk size",
        min_value=500, max_value=20000, value=5000, step=500,

        key="gom_srvy_loader_chunk",
    )

    # ── Resolve which source to use ──────────────────────────────────────
    # Uploaded file wins; otherwise the server path. End up with a
    # filesystem path the loader can open.
    file_path: str = ""
    cleanup_temp: bool = False

    if uploaded is not None:
        # Streamlit gives an in-memory UploadedFile; the loader opens from
        # disk, so materialize it to a temp file.
        import tempfile
        suffix = Path(uploaded.name).suffix or ".txt"
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=suffix, prefix="gom_srvy_upload_"
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
        st.info(
            "Drop a file above, or check the box to use a server-side path."
        )
        return

    # ── Load button ──────────────────────────────────────────────────────
    if not st.button(
        "🚀 Load GOM directional surveys",
        type="primary",
        key="gom_srvy_loader_btn",
        use_container_width=True,
    ):
        # Clean up the temp file if we made one but the user didn't load.
        if cleanup_temp:
            try:
                import os
                os.unlink(file_path)
            except Exception:
                pass
        return

    # ── Run the load ─────────────────────────────────────────────────────
    from dataview.file_catalog.gom_dir_srvy_loader import load_gom_dir_srvy

    progress = st.progress(0.0, text="Starting…")
    status   = st.empty()

    def _on_progress(done: int, total: int, msg: str):
        pct = min(1.0, done / max(total, 1)) if total else 0.0
        try:
            progress.progress(pct, text=f"{done:,} / {total:,} · {msg}")
        except Exception:
            pass

    try:
        stats = load_gom_dir_srvy(
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
        # Always remove the drag-and-drop temp file, regardless of outcome.
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

    c1, c2, c3 = st.columns(3)
    c1.metric("📋 Rows read",          f"{stats['total_rows']:,}")
    c2.metric("📈 Survey points loaded", f"{stats['loaded_points']:,}")
    c3.metric("⚠️ Parse errors",       f"{stats['parse_errors']:,}")

    if stats["error_samples"]:
        st.markdown(
            f"**⚠️ {stats['parse_errors']} error(s) — first 10 examples:**"
        )
        for err in stats["error_samples"]:
            st.text(err)

    # ── Post-load sanity check ───────────────────────────────────────────
    st.markdown("**🔎 Post-load sanity check**")
    try:
        from sqlalchemy import text
        with engine.connect() as con:
            point_count = con.execute(text(
                "SELECT COUNT(*) FROM dataview_gom.directional_survey_point"
            )).scalar() or 0
            well_count = con.execute(text(
                "SELECT COUNT(DISTINCT api_well_number) "
                "FROM dataview_gom.directional_survey_point"
            )).scalar() or 0
            # How many of those survey APIs actually match a borehole
            # record in dataview_gom.well — a preview of what the later
            # well_id-resolution pass will be able to link.
            matched = con.execute(text("""
                SELECT COUNT(DISTINCT s.api_well_number)
                FROM dataview_gom.directional_survey_point s
                WHERE EXISTS (
                    SELECT 1 FROM dataview_gom.well w
                    WHERE w.api_well_number = s.api_well_number
                )
            """)).scalar() or 0
            deepest = con.execute(text("""
                SELECT TOP 10
                    api_well_number,
                    COUNT(*)                 AS stations,
                    MAX(survey_point_md)     AS max_md
                FROM dataview_gom.directional_survey_point
                GROUP BY api_well_number
                ORDER BY MAX(survey_point_md) DESC
            """)).fetchall()

        st.write(
            f"**Total survey points in "
            f"`dataview_gom.directional_survey_point`:** {point_count:,}"
        )
        st.write(f"**Distinct wells with survey data:** {well_count:,}")
        st.write(
            f"**Survey wells that match a `dataview_gom.well` record:** "
            f"{matched:,} of {well_count:,} "
            f"— the rest stay unresolved until the well_id pass runs."
        )
        if deepest:
            st.write("**Deepest 10 wells by max measured depth:**")
            import pandas as pd
            st.dataframe(
                pd.DataFrame(
                    deepest,
                    columns=["API Well Number", "Stations", "Max MD (ft)"],
                ),
                use_container_width=True,
                hide_index=True,
            )
    except Exception as e:
        st.warning(f"Sanity check query failed: {e}")
