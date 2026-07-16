"""page_selected_documents.py — documents for the entities selected on the map.

Reads file_catalog.GLOBAL_FILE_CATALOG via catalog_docs.list_documents, matching
on the stamped UWI14 (wells) / SURVEY_NAME (surveys) and preferring the governed
VAULT_PATH over the original FILE_PATH. Each document can be opened inline; a
✖ Close button at the end of the rendered file dismisses the view.
"""
from __future__ import annotations

import os
import base64
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd

try:
    from dataview.file_catalog.catalog_docs import list_documents, _open_native
except Exception:                       # pragma: no cover
    try:
        from dataview.file_catalog.catalog_docs import list_documents, _open_native
    except Exception:
        list_documents = _open_native = None

# Prefer the shared file_viewer (rich per-format viewers: LAS curves, SEG-Y
# headers, shapefiles, Excel, Word, images...) over the local basic renderer.
try:
    from dataview.file_catalog import file_viewer as _file_viewer
except Exception:
    try:
        from modules import file_viewer as _file_viewer
    except Exception:
        _file_viewer = None

_TYPE_FILTERS = {
    "All types":  None,
    "PDF":        {".pdf"},
    "Well Log":   {".las", ".dlis", ".lis"},
    "Seismic":    {".segy", ".sgy", ".seg", ".p190", ".p90", ".p1"},
    "Office":     {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                   ".csv", ".txt", ".odf", ".odt", ".ods"},
    "GIS":        {".shp", ".geojson", ".kml", ".kmz", ".gpkg"},
}

_INLINE_MAX_MB = 15          # above this, offer download instead of inline render
_IMG_EXTS  = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
_TEXT_EXTS = {"txt", "csv", "log", "json", "md", "las", "xml", "p190",
              "witsml", "geojson", "kml"}


def _apply_filters(df, search, type_label):
    if df.empty:
        return df
    out = df
    if search:
        s = search.strip().lower()
        out = out[out.apply(lambda r: any(
            s in str(r.get(col, "")).lower()
            for col in ("file_name", "uwi14", "survey_name", "doc_type")), axis=1)]
    exts = _TYPE_FILTERS.get(type_label)
    if exts and not out.empty and "file_ext" in out.columns:
        out = out[out["file_ext"].astype(str).str.lower().isin(exts)]
    return out.reset_index(drop=True)


def _render_inline(path, ext, key):
    """Render a document inline, then a Close button at the end of the file."""
    ext = str(ext or "").lstrip(".").lower()
    try:
        size_mb = os.path.getsize(path) / (1024 * 1024)
    except Exception:
        size_mb = 0.0

    if size_mb > _INLINE_MAX_MB:
        st.info(f"File is {size_mb:.0f} MB — too large to preview inline. "
                "Use Download or Open.")
    else:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except Exception as e:
            st.error(f"Cannot read file: {e}")
            data = None
        if data is not None:
            if ext == "pdf":
                b64 = base64.b64encode(data).decode()
                components.html(
                    f'<iframe src="data:application/pdf;base64,{b64}" '
                    f'width="100%" height="700" style="border:1px solid #333;">'
                    f'</iframe>', height=720)
            elif ext in _IMG_EXTS:
                st.image(data, use_column_width=True)
            elif ext in _TEXT_EXTS:
                txt = data.decode("utf-8", errors="replace")
                st.text(txt[:20000] + ("\n… (truncated)" if len(txt) > 20000 else ""))
            else:
                st.info(f"No inline preview for .{ext} — use Download or Open.")

    # ── close hint at the END of the viewed file ──────────────────────────
    # Opening is driven by the row's Open checkbox — untick it to close this view.
    st.caption("↑ Untick the document's **Open** box to close this preview.")


def _docs_for_wells(engine, uwis, survey_names=None):
    """Back-compat shim: documents for a list of well UWIs (and optional survey
    names) -> DataFrame. Older callers import this directly; it now delegates to
    catalog_docs.list_documents so there's one source of truth."""
    import pandas as pd
    if list_documents is None:
        return pd.DataFrame()
    uwis = [str(u).strip() for u in (uwis or []) if str(u).strip()]
    srvys = [str(s).strip() for s in (survey_names or []) if str(s).strip()]
    if not uwis and not srvys:
        return pd.DataFrame()
    try:
        return list_documents(engine, uwi14=uwis or None,
                              survey_name=srvys or None)
    except Exception:
        return pd.DataFrame()


def _doc_view_page(engine, inv):
    """Second-level sub-page: shows ONE document's viewer at top level (its own
    expanders work — no nesting), with a Back button to the documents list."""
    if st.button("← Back to documents", key="seldoc_back_to_list"):
        st.session_state.pop("seldoc_doc", None)
        st.rerun()

    # fetch the one document's row by inventory_id from the current selection set
    sel = st.session_state.get("selected_entities", [])
    if not sel:
        _uwis = st.session_state.get("selected_doc_uwis", [])
        sel = [{"type": "well", "id": u} for u in _uwis]
    well_ids   = [str(e["id"]) for e in sel if e.get("type") == "well" and e.get("id")]
    survey_ids = [str(e["id"]) for e in sel if e.get("type") == "seismic" and e.get("id")]
    df = list_documents(engine, uwi14=well_ids or None, survey_name=survey_ids or None)
    if df is None or df.empty:
        st.warning("Document not found (selection may have changed).")
        return
    match = df[df["inventory_id"].astype(str) == str(inv)]
    if match.empty:
        st.warning("Document not found in the current selection.")
        return
    row = match.iloc[0]

    fname = row.get("file_name") or "(unnamed)"
    ext   = str(row.get("file_ext") or "").lower()
    path  = row.get("open_path") or row.get("file_path")
    vaulted = bool(row.get("vault_path") and str(row.get("vault_path")).strip())

    st.subheader(f"📄 {fname}")
    who = row.get("well_name") or row.get("survey_name") or row.get("uwi14") or ""
    st.caption(f"{who}  ·  Type: {row.get('doc_type') or '—'}  "
               f"·  {'vault' if vaulted else 'original'}: {path or '—'}")
    if not path:
        st.warning("No file path recorded for this document.")
        return

    b1, b2 = st.columns([1, 1])
    if b1.button("Open in app", key="seldoc_open_app", use_container_width=True):
        if not os.path.exists(path):
            st.warning("File not found on disk at the recorded path.")
        elif _open_native:
            err = _open_native(path)
            st.success("Opened.") if err is None else st.error(err)
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                b2.download_button("Download", fh.read(), file_name=fname,
                                   key="seldoc_dl_page", use_container_width=True)
        except Exception as e:
            b2.caption(f"unreadable: {str(e)[:30]}")

    st.divider()
    # TOP LEVEL — file_viewer.view() can use its own expanders safely here
    if os.path.exists(path):
        if _file_viewer is not None:
            try:
                _file_viewer.view(path, ext)
            except Exception as _ve:
                st.warning(f"viewer error ({str(_ve)[:100]}); basic preview:")
                _render_inline(path, ext, key=str(inv))
        else:
            _render_inline(path, ext, key=str(inv))
    else:
        st.warning("File not found on disk.")


def run(engine, dialect=None):
    # Second-level sub-page: if a document is open, show ONLY its viewer page.
    _open_inv = st.session_state.get("seldoc_doc")
    if _open_inv:
        _doc_view_page(engine, _open_inv)
        return

    st.subheader("📂 Documents for Selected Wells")

    if list_documents is None:
        st.error("catalog_docs.list_documents not importable.")
        return

    sel = st.session_state.get("selected_entities", [])
    if not sel:
        _uwis = st.session_state.get("selected_doc_uwis", [])
        sel = [{"type": "well", "id": u, "name": u} for u in _uwis]
    if not sel:
        st.info("No wells selected. Draw a box on the map and choose "
                "**📄 Open Documents** to see their documents here.")
        return

    well_ids = [str(e["id"]) for e in sel
                if e.get("type") == "well" and e.get("id")]
    survey_ids = [str(e["id"]) for e in sel
                  if e.get("type") == "seismic" and e.get("id")]
    st.caption(f"{len(well_ids):,} selected well(s). "
               "Search, filter by type, then open any document inline.")

    # ── Filters: gated in a FORM so the grid does NOT update on every keystroke /
    # dropdown change. Nothing re-queries or re-filters until 'Apply filters' is
    # clicked; the applied values are held in session_state.
    with st.form("seldoc_filters", clear_on_submit=False):
        fc1, fc2, fc3 = st.columns([3, 2, 1])
        _search_in = fc1.text_input(
            "🔎 Search", value=st.session_state.get("seldoc_search_applied", ""),
            placeholder="UWI, well / survey name, line, or file name",
            label_visibility="collapsed")
        _type_in = fc2.selectbox(
            "Type", list(_TYPE_FILTERS.keys()),
            index=list(_TYPE_FILTERS.keys()).index(
                st.session_state.get("seldoc_type_applied", "All types")),
            label_visibility="collapsed")
        _applied = fc3.form_submit_button("Apply filters", use_container_width=True)
    if _applied:
        st.session_state["seldoc_search_applied"] = _search_in
        st.session_state["seldoc_type_applied"] = _type_in
        st.session_state["seldoc_view"] = None      # reset any open view on re-filter

    search     = st.session_state.get("seldoc_search_applied", "")
    type_label = st.session_state.get("seldoc_type_applied", "All types")

    docs = list_documents(engine, uwi14=well_ids or None,
                          survey_name=survey_ids or None)
    if docs is None or docs.empty:
        st.warning("No catalogued documents found for the selection. Documents "
                   "are tagged during promote — UWI14 / SURVEY_NAME must be "
                   "populated on GLOBAL_FILE_CATALOG first.")
        return

    view = _apply_filters(docs, search, type_label)
    st.caption(f"{len(view):,} document(s) shown (of {len(docs):,} for the selection).")
    if view.empty:
        st.info("No documents match the current search / type filter.")
        return

    # ── Expander per well/survey ─────────────────────────────────────────────
    # One collapsible group per entity (surveys can have dozens of line files, so
    # a flat grid is unwieldy). Inside each: a checkbox grid of THAT entity's docs;
    # tick a row to open it inline below its group.
    import pandas as _pd
    from collections import OrderedDict as _OD

    _n = len(view)
    _uwi  = view.get("uwi14",       _pd.Series([""] * _n)).fillna("").astype(str).tolist()
    _srv  = view.get("survey_name", _pd.Series([""] * _n)).fillna("").astype(str).tolist()
    _wnm  = view.get("well_name",   _pd.Series([""] * _n)).fillna("").astype(str).tolist()

    # group the ORIGINAL view rows by entity key (UWI for wells, survey for seismic)
    _groups = _OD()
    for k in range(_n):
        key  = _uwi[k] or _srv[k] or "(untagged)"
        name = _wnm[k] or _srv[k] or ""
        _groups.setdefault(key, {"name": name, "rows": []})
        _groups[key]["rows"].append(k)

    # sort groups by display name (then key), and expand a group if one of its
    # docs is the one currently being viewed.
    _viewing = st.session_state.get("seldoc_view")   # (key, local_idx) or None
    _sorted_keys = sorted(_groups.keys(),
                          key=lambda kk: ((_groups[kk]["name"] or kk).upper(), kk.upper()))

    # Expander per well/survey; each document row has a View button that opens the
    # dedicated document sub-page (sets seldoc_doc + reruns). No inline viewer here,
    # so nothing nests inside these expanders.
    st.caption("Open a group, then **View** a document to open it on its own page.")
    for key in _sorted_keys:
        grp   = _groups[key]
        idxs  = grp["rows"]
        name  = grp["name"]
        ndocs = len(idxs)
        is_survey = bool(_srv[idxs[0]]) and not _uwi[idxs[0]]
        icon  = "🌊" if is_survey else "🛢"
        title = (f"{icon}  {name or key}  ·  {key if name else ''}"
                 f"  —  {ndocs} document{'s' if ndocs != 1 else ''}").replace("  ·   —", "  —")
        with st.expander(title, expanded=False):
            rows = [view.iloc[k] for k in idxs]
            for j, r in enumerate(rows):
                inv   = str(r.get("inventory_id") or "")
                fname = r.get("file_name") or "(unnamed)"
                line  = str(r.get("line_name") or "")
                dtype = str(r.get("doc_type") or "")
                c1, c2, c3, c4 = st.columns([5, 2, 2, 1.4])
                c1.write(f"**{fname}**")
                c2.caption(line or "—")
                c3.caption(dtype or "—")
                if c4.button("View", key=f"seldoc_view_{key}_{j}",
                             use_container_width=True):
                    st.session_state["seldoc_doc"] = inv
                    st.rerun()
