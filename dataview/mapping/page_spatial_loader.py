"""Spatial Loader — browse a shapefile, GeoJSON or geodatabase, pick layers, load them.

WHY THIS IS ITS OWN PAGE. The File Catalog walks FILES, and a .gdb is a
DIRECTORY -- it sees a00000001.gdbtable and never the geodatabase. The map's
"Registered layers" panel takes one path at a time and records WHERE the file
is rather than reading it. Neither can answer "this geodatabase has 137 layers,
which twelve do I want" -- which is the actual question when GIS data arrives.

Nothing here is new logic: list_source_layers and import_layer live in
dv_spatial_loader, and tools/load_gdb_layers.py calls the same two. One
definition, three doors.

THE TWO THINGS THAT SILENTLY RUIN A SPATIAL LOAD, both refused here rather
than guessed at:

  * NO CRS. The RMOTC geodatabase is NAD27 Wyoming State Plane in FEET. Read
    as degrees, Teapot Dome lands in the Gulf of Guinea. A layer that declares
    no CRS cannot be reprojected, so it is shown greyed with the reason.
  * AN UNREGISTERED SOURCE. `source` is an FK to dv_r_source; a code that is
    not registered fails the whole load with a 547 naming a constraint. The
    picker only offers codes that exist, because an import must not mint
    standards vocabulary.
"""
import streamlit as st


def _fmt(n):
    try:
        return format(int(n), ",")
    except (TypeError, ValueError):
        return str(n)


def render(engine=None):
    st.subheader("🗺 Spatial Loader")
    st.caption(
        "Point at a shapefile, GeoJSON, folder, or a **.gdb geodatabase**, "
        "pick the layers worth having, and load them into `dv_spatial_layer` "
        "— where the map's **Registered layers** panel finds them."
    )
    if engine is None:
        st.warning("Connect to a database first.")
        return

    from sqlalchemy import text
    from dataview.mapping import dv_spatial_loader as sl

    # ── what is already loaded ──────────────────────────────────────────
    with st.expander("Layers already loaded", expanded=False):
        try:
            cur = sl.list_layers(engine)
        except Exception as e:
            cur, _ = [], st.caption(f"Could not list layers: {e}")
        if not cur:
            st.caption("Nothing loaded yet.")
        else:
            import pandas as pd
            st.dataframe(pd.DataFrame([{
                "layer": r["layer_name"], "type": r["layer_type"],
                "category": r["layer_category"],
                "features": r["feature_count"],
                "source": r["source_type"],
            } for r in cur]), hide_index=True, use_container_width=True)
            # ── restyle ─────────────────────────────────────
            # Colour was settable at IMPORT and nowhere else, so changing one
            # meant deleting the layer and re-reading every feature to alter a
            # hex string. The map already reads these six fields at draw time.
            _names = [r["layer_name"] for r in cur]
            _sel = st.selectbox("Restyle a layer", _names, key="sl_style_pick")
            _row = next((r for r in cur if r["layer_name"] == _sel), None)
            if _row:
                # KEYS VERSIONED ON THE LAYER. A fixed-key widget never
                # re-defaults (Streamlit scar #1): switch layers and the
                # picker keeps the PREVIOUS layer's colour, so Apply writes
                # one layer's style onto another and it looks like the save
                # went to the wrong row.
                _k = str(_row["layer_id"])[:12]
                s1, s2, s3, s4 = st.columns([1, 1, 1, 1])
                _col = s1.color_picker(
                    "Line", _row.get("style_color") or "#888888",
                    key="sl_style_col_" + _k)
                _fill = s2.color_picker(
                    "Fill", _row.get("style_fill_color")
                    or _row.get("style_color") or "#888888",
                    key="sl_style_fill_" + _k)
                _w = s3.slider("Width", 0.5, 6.0,
                               float(_row.get("style_weight") or 1.5), 0.5,
                               key="sl_style_w_" + _k)
                _fo = s4.slider("Fill opacity", 0.0, 1.0,
                                float(_row.get("style_fill_opacity") or 0.0), 0.05,
                                key="sl_style_fo_" + _k)
                _dash = st.checkbox(
                    "Dashed", value=bool(_row.get("style_dash")),
                    key="sl_style_dash_" + _k)
                if st.button("🎨 Apply style", key="sl_style_go_" + _k):
                    if sl.set_style(engine, _row["layer_id"], color=_col,
                                    fill_color=_fill, weight=_w,
                                    fill_opacity=_fo,
                                    dash=("6 4" if _dash else "")):
                        st.success(f"Restyled {_sel}. Reload the map to see it.")
                        st.rerun()
                    else:
                        st.error("Could not update the style.")

            _del = st.selectbox("Remove a layer", ["—"] + _names,
                                key="sl_del_pick")
    st.divider()

    # ── the source ──────────────────────────────────────────────────────
    _path = st.text_input(
        "Shapefile, GeoJSON, folder or .gdb",
        key="sl_path",
        placeholder=r"C:\GIS\RMOTC_Data_Set_CD.gdb")
    if st.button("🔍 Read layers", key="sl_scan", disabled=not _path):
        with st.spinner("Opening the source…"):
            try:
                st.session_state["sl_found"] = sl.list_source_layers(_path.strip())
                st.session_state["sl_found_path"] = _path.strip()
            except Exception as e:
                st.error(f"Could not read it: {type(e).__name__}: {e}")
                st.session_state["sl_found"] = []

    found = st.session_state.get("sl_found") or []
    if not found:
        return

    src_path = st.session_state.get("sl_found_path", "")
    ok = [r for r in found if r.get("crs_ok")]
    bad = [r for r in found if not r.get("crs_ok")]
    st.markdown(f"**{len(found)} layer(s)** with geometry in `{src_path}`")
    if bad:
        # NAME THEM. "4 layers skipped" sends the reader looking; the names and
        # the reason let them decide whether it matters.
        st.warning(
            f"{len(bad)} layer(s) declare no CRS and cannot be reprojected, so "
            f"they are not offered: **" + "**, **".join(r["layer"] for r in bad[:6])
            + "**. Loading them would place the data by guesswork.")

    import pandas as pd
    st.dataframe(pd.DataFrame([{
        "layer": r["layer"], "geometry": r["geometry"],
        "features": r["features"], "attributes": len(r.get("props") or []),
    } for r in ok]), hide_index=True, use_container_width=True, height=260)

    _picked = st.multiselect(
        "Layers to load", [r["layer"] for r in ok], key="sl_pick")
    if not _picked:
        return

    c1, c2 = st.columns(2)
    _cat = c1.selectbox(
        "Category (drives colour and fill on the map)",
        ["OTHER", "BOUNDARY", "FIELD", "LEASE", "PIPELINE", "WELL",
         "SEISMIC_2D", "SEISMIC_3D", "BASIN"],
        key="sl_cat")

    # ONLY CODES THAT EXIST. source is an FK to dv_r_source, and an unregistered
    # value fails the load with a 547 naming a constraint rather than the cause.
    try:
        with engine.connect() as con:
            _srcs = [r[0] for r in con.execute(text(
                "SELECT source FROM dataview.dv_r_source ORDER BY source")).fetchall()]
    except Exception:
        _srcs = ["SHAPEFILE"]
    _src = c2.selectbox(
        "Source", _srcs,
        index=_srcs.index("SHAPEFILE") if "SHAPEFILE" in _srcs else 0,
        key="sl_src")

    if st.button(f"⬇ Load {len(_picked)} layer(s)", type="primary",
                 key="sl_load", use_container_width=True):
        prog = st.progress(0.0)
        done, failed, total_f = [], [], 0
        for i, name in enumerate(_picked):
            row = next((r for r in ok if r["layer"] == name), None)
            lay = name if row and row["path"] != name else None
            try:
                res = sl.import_layer(
                    engine, row["path"] if row else src_path,
                    layer=(name if (row and row["path"].lower().endswith(".gdb"))
                           else None),
                    layer_name=name.replace("_", " "),
                    layer_category=_cat,
                    tooltip_fields=(row.get("props") or [])[:4] if row else None,
                    display_order=i, source=_src)
                n = res.get("loaded", 0)
                if n:
                    done.append((name, n))
                    total_f += n
                else:
                    failed.append((name, "; ".join(res.get("errors") or ["0 features"])))
            except Exception as e:
                failed.append((name, f"{type(e).__name__}: {e}"))
            prog.progress((i + 1) / len(_picked))
        prog.empty()
        for name, n in done:
            st.success(f"**{name}** — {_fmt(n)} feature(s)")
        for name, why in failed:
            st.error(f"**{name}** — {why[:200]}")
        # SAY WHEN NOTHING LANDED, rather than showing a total that reads as success.
        if not done:
            st.error("Nothing was loaded. A 547 on dv_r_source means the Source "
                     "code is not registered.")
        else:
            st.info(f"{_fmt(total_f)} feature(s) across {len(done)} layer(s). "
                    f"Tick them in the map's **🗺 Registered layers** panel.")


# router aliases
main = render
show = render
app = render
