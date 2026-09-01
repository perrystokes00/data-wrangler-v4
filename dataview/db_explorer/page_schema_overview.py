"""
page_schema_overview.py
========================
A deliberately high-level view of what the DataView model covers: the subject
areas, how many tables and rows sit in each, and how they relate to the Wells
hub. Drill into an area to see its tables.

Wire-up (in app_v3.py), same pattern as the other pages:

    from dataview.db_explorer import page_schema_overview
    ...
    elif nav == "Model Overview":
        page_schema_overview.run(engine)

Reads the live catalog through schema_introspect.build_model(), so it never
drifts from the actual database.
"""
import streamlit as st
import streamlit.components.v1 as components

from dataview.core import schema_introspect as si


def _render_mermaid(code: str, height: int = 420):
    """Render a Mermaid diagram in an iframe via the CDN build."""
    html = f"""
    <div class="mermaid" style="text-align:center">{code}</div>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <script>
      mermaid.initialize({{ startOnLoad: true, theme: 'dark',
                            securityLevel: 'loose',
                            flowchart: {{ htmlLabels: true }} }});
    </script>
    """
    components.html(html, height=height, scrolling=True)


def _get_model(engine, schema: str):
    """Session-cached model so we don't re-query the catalog every rerun."""
    key = f"_schema_model::{schema}"
    if key not in st.session_state:
        st.session_state[key] = si.build_model(engine, schema)
    return st.session_state[key]


def run(engine=None):
    st.title("🗺 DataView — Model Overview")
    st.caption("The subject areas the data model covers, and how they connect.")

    if engine is None:
        st.info("Connect to the database to view the model overview.")
        return

    # Schema selector — restricted to the three project schemas (only those
    # that actually exist in the connected database).
    _wanted = ["dataview", "file_catalog", "las_catalog"]
    try:
        _avail = set(si.list_schemas(engine))
    except Exception:
        _avail = set()
    _schemas = [s for s in _wanted if s in _avail] or _wanted
    _default = "dataview" if "dataview" in _schemas else _schemas[0]

    top = st.columns([3, 1, 1])
    with top[0]:
        schema = st.selectbox("Schema", _schemas,
                              index=_schemas.index(_default), key="ov_schema")
    with top[2]:
        st.write("")  # nudge the button down to align with the selectbox
        if st.button("🔄 Refresh", use_container_width=True):
            st.session_state.pop(f"_schema_model::{schema}", None)
            st.rerun()

    try:
        _srv, _db = si.connection_info(engine)
        st.caption(f"Reading **{_db}** on `{_srv}` · schema `{schema}`")
    except Exception:
        pass

    try:
        model = _get_model(engine, schema)
    except Exception as e:
        st.error(f"Could not read the schema: {e}")
        return

    tables = model["tables"]
    if not tables:
        st.warning(f"No tables found in schema '{schema}'.")
        return

    total_rows = sum(t["row_count"] for t in tables.values())
    _wc = model.get("well_count")
    if _wc is not None:
        m = st.columns(4)
        m[0].metric("Wells", f"{_wc:,}")
        m[1].metric("Tables", len(tables))
        m[2].metric("Rows (all tables)", f"{total_rows:,}")
        m[3].metric("Subject areas", len(model["areas"]))
    else:
        m = st.columns(3)
        m[0].metric("Tables", len(tables))
        m[1].metric("Rows (all tables)", f"{total_rows:,}")
        m[2].metric("Subject areas", len(model["areas"]))

    st.markdown("#### How the areas connect")
    _render_mermaid(si.build_overview_mermaid(model), height=380)

    st.markdown("#### Subject areas")
    area_keys = list(model["areas"].keys())
    # three cards per row
    for i in range(0, len(area_keys), 3):
        cols = st.columns(3, gap="medium")
        for col, area in zip(cols, area_keys[i:i + 3]):
            meta = si.AREA_META[area]
            tabs = model["areas"][area]
            rows = sum(tables[t]["row_count"] for t in tabs)
            with col:
                with st.container(border=True):
                    st.markdown(
                        f"<div style='border-top:3px solid {meta['color']};"
                        f"margin:-16px -16px 8px -16px;border-radius:8px 8px 0 0'>"
                        f"</div>"
                        f"<div style='font-size:1.05rem;font-weight:700;"
                        f"color:{meta['color']}'>{meta['icon']} "
                        f"{meta['label']}</div>",
                        unsafe_allow_html=True)
                    st.caption(meta["desc"])
                    c1, c2 = st.columns(2)
                    c1.metric("Tables", len(tabs))
                    c2.metric("Rows", f"{rows:,}")
                    with st.expander(f"Tables ({len(tabs)})"):
                        for t in tabs:
                            st.markdown(
                                f"`{t}` · {tables[t]['row_count']:,} rows")
