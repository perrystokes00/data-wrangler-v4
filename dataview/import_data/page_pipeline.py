"""page_pipeline.py — Main pipeline stages 0-7."""
import os as _os
import json
import streamlit as st
import pandas as pd
from dataview.core.ui_helpers import shdr, pill, mrow

# ── Pipeline modules ─────────────────────────────────────────────────
from dataview.core.db        import DBConfig, connect, connect_demo
from dataview.core.schema    import load_schema_from_dict, load_schema_from_string
from dataview.import_data.staging   import ingest_file, load_to_staging, load_to_staging_demo, preview_csv, preview_staging_table
from dataview.import_data.normalize import normalize_server, normalize_demo
from dataview.import_data.mapping   import build_mapping, TRANSFORM_OPTIONS, build_transform_sql, mapping_fingerprint, serialise_mapping, restore_mapping, save_mapping_to_disk, restore_mapping_from_disk, save_entity_mapping, restore_entity_mapping
from dataview.core.fk        import (
    introspect_fk_constraints, introspect_fk_demo,
    check_fk_violations, apply_resolutions as apply_fk_resolutions,
    FKViolation, FKNode,
    get_reference_table_context, insert_reference_rows,
    load_fk_samples, load_fk_samples_batch,
    clear_parent_values_cache,
)
from dataview.core.fk_entity import (
    is_reference_table, build_entity_mapping, preview_entity_rows,
    insert_entity_rows, topological_sort, KNOWN_ENTITY_TABLES,
    EntityMapping, build_fk_graph,
)
from dataview.core.validate     import validate
from dataview.import_data.promote      import promote_server, promote_demo, promote_merge, compute_data_hash, _write_file_record
try:
    from dataview.core.ppdm_agent   import PPDMAgent, build_pipeline_context
except ImportError:
    PPDMAgent = None
    build_pipeline_context = None

# ── Stage help "?" badge (uses the global .card-help CSS from app_v3) ─
def _stage_help(text):
    """Drop a gold '?' help badge at the top-right of the current stage.
    Call immediately BEFORE the stage's shdr(...) header. The badge carries
    its own relative wrapper, so it anchors correctly even though stages
    aren't bordered cards. Hover reveals the tooltip via the global
    .card-help CSS injected by app_v3.py. Tune top/right here to nudge all
    stage badges at once."""
    import html as _html
    st.markdown(
        "<div style='position:relative;width:100%;height:0;z-index:7'>"
        "<span class='card-help' style='top:2px;right:2px' "
        f"data-help=\"{_html.escape(str(text), quote=True)}\">?</span>"
        "</div>",
        unsafe_allow_html=True)


# ── Lightweight stage timing (profiling) ─────────────────────────────
import time as _time

_STAGE_LABELS_T = [
    "Stage 1 · Connect", "Stage 2 · Stage", "Stage 3 · Normalize",
    "Stage 4 · Target",  "Stage 5 · Map",   "Stage 6 · FK",
    "Stage 7 · Validate", "Stage 8 · Promote",
]

def _t_now():
    return _time.perf_counter()

# Set True to re-enable stage timing in the console and a sidebar panel.
# Left in place (rather than ripped out) so it's one flag to flip when
# profiling; with it False, _t_mark calls are near-zero-cost no-ops.
_TIMING_DEBUG = False

# Master switch for auto-populating H3 cells after a successful promote.
# True  -> well tables (those with surface_latitude/longitude) get h3_r* columns
#          ensured + filled at promote time. Non-well tables are never touched.
# False -> no H3 work at promote; use the standalone populate_h3.py instead.
_H3_AUTOFILL = False

def _t_log(label, secs):
    """Record a timing mark (console + session log) when _TIMING_DEBUG is on."""
    if not _TIMING_DEBUG:
        return
    try:
        log = st.session_state.setdefault("_timing_log", [])
        log.append((label, float(secs)))
        if len(log) > 50:
            del log[:-50]
        print(f"[TIMING] {label}: {secs*1000:.0f} ms")
    except Exception:
        pass

def _t_mark(label, t0):
    """Log elapsed since t0 (a _t_now() value)."""
    if not _TIMING_DEBUG:
        return
    _t_log(label, _t_now() - t0)

def _render_timing_sidebar():
    if not _TIMING_DEBUG:
        return
    log = st.session_state.get("_timing_log", [])
    if not log:
        return
    with st.sidebar:
        st.markdown("### ⏱ Timing (most recent)")
        for label, secs in log[-14:][::-1]:
            ms = secs * 1000
            color = "#f85149" if ms >= 2000 else "#f0883e" if ms >= 500 else "#56d364"
            st.markdown(
                f"<code style='color:{color}'>{ms:6.0f} ms</code> &nbsp;{label}",
                unsafe_allow_html=True)
        if st.button("Clear timings", key="clear_timing_log"):
            st.session_state["_timing_log"] = []
            st.rerun()

@st.cache_data(show_spinner=False)
def _cached_value_map(_engine, _ver):
    """Cache the dv_value_map fetch so the (always-executed) collapsed
    expander stops re-querying on every rerun. _engine is underscore-prefixed
    so cache_data does not try to hash it; _ver is bumped whenever a mapping
    is added, which invalidates the cache."""
    from dataview.reference_tables.value_standardize import fetch_value_map
    return fetch_value_map(_engine)


def _render_value_standardization(S):
    """Stage-6 panel: show what dv_value_map conformed + manage mappings."""
    _seg0 = _t_now()
    rep = getattr(S, "value_map_report", None)

    # conformance summary (what this cycle changed)
    if rep:
        if len(rep) == 1 and "error" in rep[0]:
            st.caption(f"🔤 Value map unavailable: {rep[0]['error']}")
        else:
            total = sum(r.get("rows", 0) for r in rep)
            st.success(
                f"🔤 Standardized {total} staging value(s) to canonical "
                f"vocabulary across {len({r['column'] for r in rep})} column(s).")
            with st.expander("What was conformed", expanded=False):
                import pandas as _pd
                st.dataframe(_pd.DataFrame(rep), use_container_width=True,
                             hide_index=True)
    _t_mark("S6 render \u00b7 conformance summary", _seg0)

    # editor — view current map + add a mapping
    with st.expander("🔤 Value Standardization · manage canonical mappings",
                     expanded=False):
        st.caption(
            "Maps raw source values to canonical vocabulary at load time so "
            "wells from every source filter consistently "
            "(e.g. well_type · *Gas Well* → **GAS**, "
            "well_status · *Producing* → **ACTIVE**). Collation is "
            "case-insensitive, so one row covers all case variants.")
        try:
            from dataview.reference_tables.value_standardize import fetch_value_map, upsert_value_map
            _seg_f = _t_now()
            _vm = _cached_value_map(S.engine, st.session_state.get("vm_ver", 0))
            _t_mark("S6 render \u00b7 value-map fetch", _seg_f)
            _seg_g = _t_now()
            if _vm is not None and not _vm.empty:
                st.dataframe(
                    _vm[["target_column", "source_value", "canonical_value",
                         "confirmed_ind", "source"]],
                    use_container_width=True, hide_index=True, height=220)
            else:
                st.info("No mappings yet. Add one below.")
            _t_mark(f"S6 render \u00b7 value-map grid ({0 if _vm is None else len(_vm)} rows)", _seg_g)

            _seg_e = _t_now()
            _a, _b, _c, _d = st.columns([2, 2, 2, 1.2])
            with _a:
                _tcol = st.text_input("Target column", key="vm_add_col",
                                      placeholder="well_type")
            with _b:
                _sval = st.text_input("Source value", key="vm_add_src",
                                      placeholder="Gas Well")
            with _c:
                _canon = st.text_input("Canonical value", key="vm_add_canon",
                                       placeholder="GAS")
            with _d:
                st.markdown("<div style='margin-top:1.75rem'></div>",
                            unsafe_allow_html=True)
                if st.button("Add", key="vm_add_btn", use_container_width=True):
                    if _tcol.strip() and _sval.strip() and _canon.strip():
                        try:
                            upsert_value_map(
                                S.engine, "dv_well", _tcol.strip(),
                                _sval.strip(), _canon.strip(), by="UI")
                            st.session_state["vm_ver"] = st.session_state.get("vm_ver", 0) + 1
                            S.fk_checked = False  # re-apply + re-check on rerun
                            st.rerun()
                        except Exception as _ue:
                            st.error(f"Could not save mapping: {_ue}")
                    else:
                        st.warning("Fill all three fields.")

            if st.button("↻ Re-apply map & re-check FKs", key="vm_reapply_btn"):
                S.fk_checked = False
                st.rerun()
            _t_mark("S6 render \u00b7 editor inputs", _seg_e)
        except Exception as _fe:
            st.caption(f"Value map editor unavailable: {_fe}")


def render(S):
    # ── Initialize pipeline state on first load ───────────────────────
    for k, v in dict(
        stage=0, demo=False,
        source_df=None, staging_df=None, norm_df=None,
        ppdm_schema=None, agent_messages=[],
        schema_variant="DataView",
        target_table=None, target_cols=None,
        col_mapping=None, mapping_grid_snapshot=None,
        fk_constraints=None, fk_parent_pks=None,
        fk_violations=None, fk_entity_tables=None,
        fk_entity_mappings=None, fk_entity_resolved=None,
        fk_samples_loaded=False, fk_ref_context=None,
        fk_ref_edits=None, fk_graph=None, fk_node_results=None,
        fk_all_constraints=None,
        fk_success_dismissed=False, fk_checked=False,
        val_report=None, promoted=False,
        stg_name=None, src_filename=None,
        stg_table=None, stg_schema="stg",
    ).items():
        if k not in st.session_state:
            st.session_state[k] = v

    # ── Auto-load DataView schema if not yet loaded ─────────────────
    if S.ppdm_schema is None:
        _schema_dir = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "schema_registry")
        _schema_files = sorted([
            f for f in _os.listdir(_schema_dir)
            if f.endswith(".json") and "schema_domain" in f
        ]) if _os.path.isdir(_schema_dir) else []

        if _schema_files:
            # Build display labels from filenames
            _labels = {f: f.replace("_schema_domain", "").replace(".json", "").replace("_", " ").strip().upper() or "DEFAULT"
                       for f in _schema_files}

            _selected_key = "pipeline_schema_file"
            if _selected_key not in st.session_state:
                # Default to dataview if present, else first
                _default = "dataview_schema_domain.json" if "dataview_schema_domain.json" in _schema_files else _schema_files[0]
                st.session_state[_selected_key] = _default

            st.markdown("### Select Schema")
            _chosen = st.selectbox(
                "Schema catalog",
                _schema_files,
                index=_schema_files.index(st.session_state[_selected_key]),
                format_func=lambda f: _labels.get(f, f),
                key=_selected_key,
            )

            if st.button("Load Schema", type="primary"):
                try:
                    import json as _sj
                    _spath = _os.path.join(_schema_dir, _chosen)
                    with open(_spath, encoding="utf-8") as _sf:
                        _raw = _sj.load(_sf)
                    # Auto-detect root key (first key that maps to a list)
                    _root_key = None
                    for _k, _v in _raw.items():
                        if isinstance(_v, list):
                            _root_key = _k
                            break
                    if _root_key:
                        from dataview.core.schema import load_schema_from_dict, EXPECTED_ROOT_KEY
                        # Temporarily remap if root key differs
                        if _root_key != EXPECTED_ROOT_KEY:
                            _raw[EXPECTED_ROOT_KEY] = _raw.pop(_root_key)
                        S.ppdm_schema = load_schema_from_dict(_raw)
                        st.success(f"Loaded: {S.ppdm_schema.summary()}")
                        print(f"[PIPELINE] Schema loaded from {_chosen}: {S.ppdm_schema.summary()}")
                        st.rerun()
                    else:
                        st.error(f"No record array found in {_chosen}")
                except Exception as _se:
                    st.error(f"Schema load failed: {_se}")
                    print(f"[PIPELINE] Schema load failed: {_se}")
            st.stop()
        else:
            st.warning("No schema files found in schema_registry/. "
                       "Run generate_dataview_schema.py first.")
            st.stop()

    # ── Recover Snowflake engine from db_pool if Streamlit dropped it ──
    if S.engine is None:
        try:
            from dataview.core.db_pool import get_engine as _gpe, has_engine as _he
            if _he():
                S.engine = _gpe()
                S.demo   = False
                # Only set post_connect if we are on stage 0
                if S.stage == 0:
                    S._post_connect_choice = True
        except Exception:
            pass

    # Local stage nav helpers
    _SKIP_STAGES = {2}   # Stage 3 (Normalize) retired — promote + dv_value_map cover it
    def go():
        S.stage += 1
        while S.stage in _SKIP_STAGES:
            S.stage += 1
        st.rerun()
    def back():
        S.stage = max(0, S.stage - 1)
        while S.stage in _SKIP_STAGES and S.stage > 0:
            S.stage -= 1
        st.rerun()
    def reset():
        _DEFAULTS = dict(
            stage=0, engine=None, demo=False,
            source_df=None, staging_df=None, norm_df=None,
            ppdm_schema=None, agent_messages=[],
            schema_variant="DataView",
            target_table=None, target_cols=None,
            col_mapping=None, mapping_grid_snapshot=None,
            fk_constraints=None, fk_parent_pks=None,
            fk_violations=None, fk_entity_tables=None,
            fk_entity_mappings=None, fk_entity_resolved=None,
            fk_samples_loaded=False, fk_ref_context=None,
            fk_ref_edits=None, fk_graph=None, fk_node_results=None, fk_all_constraints=None,
            fk_success_dismissed=False, fk_checked=False,
            val_report=None, promoted=False,
            stg_name=None, src_filename=None,
            stg_table=None, stg_schema="stg",
        )
        for k, v in _DEFAULTS.items():
            st.session_state[k] = v
        # Clear all FK introspection and violation caches
        for _ck in list(st.session_state.keys()):
            if _ck.startswith("_fk_intro_") or _ck.startswith("_fk_viol_"):
                st.session_state.pop(_ck, None)
        st.rerun()

    # Per-render timing: marks the active stage's full render at the end.
    _render_t0 = _t_now()

    if S.stage == 0:
        # No second login: if the sidebar already established a database
        # connection, skip this redundant Stage-1 connect screen entirely and
        # go straight to upload. (Engine present + not demo == already connected.)
        if S.get("engine") is not None and not S.get("demo", False):
            S.stage = 1
            st.rerun()

        _stage_help("Connect to the target database (SQL Server or Snowflake) and choose the data model you're loading into — or run in Demo Mode with no database.")
        shdr("Stage 1 · Connect to Database",
             "Enter connection details below, or run in Demo Mode without a database.")

        # ── Database type selector ────────────────────────────────────
        db_type = st.radio("Database", ["SQL Server", "Oracle", "Snowflake"],
                           horizontal=True, key="connect_db_type")

        col_l, col_r = st.columns([3, 2])

        if db_type == "SQL Server":
            # Detect installed ODBC drivers
            try:
                import pyodbc
                all_drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
            except Exception:
                all_drivers = []
            if not all_drivers:
                all_drivers = ["ODBC Driver 17 for SQL Server",
                               "ODBC Driver 18 for SQL Server",
                               "SQL Server Native Client 11.0",
                               "SQL Server"]
            with col_l:
                server   = st.text_input("Server / Instance",
                                         value="127.0.0.1\\SQLEXPRESS",
                                         placeholder="localhost\\SQLEXPRESS  or  192.168.1.10,1433")
                database = st.text_input("Database", value="DataView")
                auth     = st.radio("Authentication", ["SQL Server Auth", "Windows Auth"],
                                    index=1, horizontal=True)
                username = password = ""
                if auth == "SQL Server Auth":
                    username = st.text_input("Username")
                    password = st.text_input("Password", type="password")
                default_driver = "ODBC Driver 17 for SQL Server"
                driver_index   = all_drivers.index(default_driver) if default_driver in all_drivers else 0
                driver = st.selectbox("ODBC Driver", all_drivers, index=driver_index)
            with col_r:
                st.markdown("#### Connection Notes")
                st.info("**Minimum permissions required:**\n"
                        "- `CREATE TABLE` on staging schema\n"
                        "- `INSERT` / `SELECT` on target tables\n"
                        "- `SELECT` on `r_*` reference tables")
                if not all_drivers:
                    st.error("No SQL Server ODBC drivers detected.\n\n"
                             "Download from Microsoft: *ODBC Driver for SQL Server*")

        elif db_type == "Oracle":
            # Load saved config if present
            _ora_cfg = {"host": "localhost", "port": 1521, "service": "FREEPDB1",
                        "username": "Perry", "password": ""}
            try:
                import json as _oraj, pathlib as _orap
                _oraf = _orap.Path(__file__).parent / "oracle_config.json"
                if _oraf.exists():
                    _ora_cfg.update(_oraj.loads(_oraf.read_text()))
            except Exception:
                pass
            with col_l:
                server   = st.text_input("Host", value=_ora_cfg["host"],
                                         placeholder="localhost or 192.168.1.10")
                ora_port = st.number_input("Port", value=int(_ora_cfg["port"]),
                                           min_value=1, max_value=65535, step=1)
                database = st.text_input("Service Name", value=_ora_cfg["service"],
                                         placeholder="FREEPDB1 or orcl")
                username = st.text_input("Username", value=_ora_cfg["username"])
                password = st.text_input("Password", type="password",
                                         value=_ora_cfg["password"])
                auth     = "SQL Server Auth"   # unused for Oracle
                driver   = ""
                all_drivers = []
            with col_r:
                st.markdown("#### Connection Notes")
                st.info("**Oracle 23c Free / 19c+**\n"
                        "- Pure Python driver (no Instant Client needed)\n"
                        "- Connects to pluggable DB via service name\n"
                        "- User needs CREATE TABLE, INSERT, SELECT")

        elif db_type == "Snowflake":
            # Load saved config if present
            _sf_cfg_defaults = {"account": "BBCUJWW-ZE62438", "username": "PMSTOKES00",
                                "password": "", "database": "PPDM39",
                                "schema": "DEMO", "warehouse": "COMPUTE_WH"}
            try:
                import json as _sfcj, pathlib as _sfcp
                _sfcf = _sfcp.Path(__file__).parent / "snowflake_config.json"
                if _sfcf.exists():
                    _sf_cfg_defaults.update(_sfcj.loads(_sfcf.read_text()))
            except Exception:
                pass
            with col_l:
                sf_account   = st.text_input("Account", value=_sf_cfg_defaults["account"], key="sf_account")
                sf_user      = st.text_input("Username", value=_sf_cfg_defaults["username"], key="sf_user")
                sf_auth_mode = st.radio("Authentication",
                                        ["Password", "External Browser (SSO)"],
                                        horizontal=True)
                sf_password  = ""
                if sf_auth_mode == "Password":
                    sf_password = st.text_input("Password", type="password",
                                                value=_sf_cfg_defaults["password"],
                                                key="sf_password_input")
                sf_database  = st.text_input("Database", value=_sf_cfg_defaults["database"])
                sf_schema_in = st.text_input("Schema", value=_sf_cfg_defaults["schema"])
                sf_warehouse = st.text_input("Warehouse", value=_sf_cfg_defaults["warehouse"])
            with col_r:
                st.markdown("#### Connection Notes")
                st.info("**Snowflake**\n"
                        "- Account ID from your Snowflake URL\n"
                        "- FKs defined but not enforced\n"
                        "- pip install snowflake-connector-python")

        if db_type != "Snowflake":
            sf_account = sf_user = sf_password = sf_database = ""
            sf_schema_in = sf_warehouse = sf_auth_mode = ""
        if db_type != "Oracle":
            ora_port = 1521

        c1, c2 = st.columns(2)
        with c1:
            if st.button("🔌 Connect", use_container_width=True):
                if db_type == "Oracle":
                    cfg = DBConfig(
                        db_type  = "oracle",
                        server   = server,
                        port     = int(ora_port),
                        database = database,
                        username = username,
                        password = password,
                    )
                elif db_type == "Snowflake":
                    cfg = DBConfig(
                        db_type   = "snowflake",
                        account   = sf_account,
                        username  = sf_user,
                        password  = sf_password,
                        database  = sf_database,
                        sf_schema = sf_schema_in,
                        warehouse = sf_warehouse,
                        sf_auth   = ("externalbrowser"
                                     if sf_auth_mode == "External Browser (SSO)"
                                     else "snowflake"),
                    )
                else:
                    cfg = DBConfig(
                        db_type      = "sqlserver",
                        server       = server,
                        database     = database,
                        windows_auth = (auth == "Windows Auth"),
                        username     = username if auth == "SQL Server Auth" else "",
                        password     = password if auth == "SQL Server Auth" else "",
                        driver       = driver,
                    )
                result = connect(cfg)
                if result.ok:
                    S.engine = result.engine
                    S.demo   = False
                    # Pre-load schema immediately so Batch Loader has it ready
                    try:
                        import json as _cj, pickle as _cp, pathlib as _cpp
                        from dataview.core.schema import load_schema_from_dict as _clsfd
                        _cpath = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "schema_registry", "dataview_schema_domain.json")
                        _cpkl  = _cpp.Path(_cpath).with_suffix(".pkl")
                        if _cpkl.exists():
                            with open(_cpkl, "rb") as _cf:
                                S.ppdm_schema = _cp.load(_cf)
                        else:
                            with open(_cpath, encoding="utf-8") as _cf:
                                S.ppdm_schema = _clsfd(_cj.load(_cf))
                    except Exception:
                        pass
                    S._post_connect_choice = True
                    st.rerun()
                else:
                    st.error(f"Connection failed: {result.message}")

        # ── Post-connect branch choice ────────────────────────────────
        if getattr(S, "_post_connect_choice", False) and S.engine:
            st.success("✅ Connected — what would you like to do?")
            st.markdown("""
            <style>
            .branch-btn button {
                background: #2563eb !important;
                color: white !important;
                border: none !important;
                font-weight: 600 !important;
                margin-top: -12px !important;
            }
            .branch-btn button:hover {
                background: #1d4ed8 !important;
            }
            </style>
            """, unsafe_allow_html=True)
            _bc1, _bc2 = st.columns(2)
            with _bc1:
                with st.container():
                    st.markdown('<div class="branch-btn">', unsafe_allow_html=True)
                    if st.button("🔬 Interactive Pipeline",
                                 use_container_width=True,
                                 key="btn_interactive_pipeline"):
                        S._post_connect_choice = False
                        go()
                    st.markdown('</div>', unsafe_allow_html=True)
            with _bc2:
                with st.container():
                    st.markdown('<div class="branch-btn">', unsafe_allow_html=True)
                    if st.button("⚡ Batch Loader",
                                 use_container_width=True,
                                 key="btn_batch_loader"):
                        S._post_connect_choice = False
                        st.session_state.app_mode = "bulk"
                        st.rerun()
                    st.markdown('</div>', unsafe_allow_html=True)

            # ── Initialize Database ───────────────────────────────────────
            st.divider()
            with st.expander("🗄️ Initialize / Migrate Database", expanded=False):
                st.caption(
                    "Creates all Data Wrangler schemas and tables on a fresh "
                    "DataView database. Safe to run on existing databases — "
                    "skips tables that already exist."
                )
                if st.button("🚀 Run Database Initialization",
                             type="primary", key="btn_db_init"):
                    try:
                        from dataview.core.setup_database import run_migrations, get_version_status
                        with st.spinner("Running migrations…"):
                            result = run_migrations(S.engine)
                        if result["failed"]:
                            st.error(
                                f"❌ {len(result['failed'])} migration(s) failed:"
                            )
                            for mid, err in result["failed"]:
                                st.code(f"Migration {mid}: {err}")
                        else:
                            st.success(
                                f"✅ Database ready — "
                                f"{len(result['applied'])} migration(s) applied, "
                                f"{len(result['skipped'])} already up to date."
                            )
                        if result["applied"] or result["skipped"]:
                            rows = get_version_status(S.engine)
                            if rows:
                                import pandas as _pd
                                df = _pd.DataFrame(rows)
                                df["applied"] = _pd.to_datetime(
                                    df["applied"]).dt.strftime("%Y-%m-%d %H:%M")
                                st.dataframe(
                                    df.rename(columns={
                                        "id": "Migration",
                                        "description": "Description",
                                        "applied": "Applied",
                                        "version": "DW Version"
                                    }),
                                    hide_index=True,
                                    use_container_width=True
                                )
                    except Exception as e:
                        st.error(f"Initialization failed: {e}")
        with c2:
            if st.button("🧪 Demo Mode (no DB)", use_container_width=True):
                r = connect_demo()
                S.engine = None
                S.demo   = True
                st.success(r.message)
                go()

    # ═══════════════════════════════════════════════════════════════════════
    # STAGE 2 · UPLOAD & STAGE
    # ═══════════════════════════════════════════════════════════════════════
    elif S.stage == 1:
        _stage_help("Upload your source CSV, TSV, or Excel file. It's BCP-loaded into a staging table as raw text — no typing or cleanup yet; that happens downstream.")
        shdr("Stage 2 · Upload Source File & Load to Staging",
             "Upload CSV, TSV, or Excel. Data is loaded into a staging table as-is (all strings).")

        if True:
            uploaded = st.file_uploader("Source data file",
                                        type=["csv", "tsv", "txt", "xlsx", "xls"])
            if uploaded:
                bytes_data = uploaded.read()
                ext = uploaded.name.rsplit(".", 1)[-1].lower()
                is_excel = ext in ("xlsx", "xls")

                # ── Parse options ─────────────────────────────────────────
                with st.expander("⚙️ Parse options", expanded=False):
                    if is_excel:
                        # Excel-specific: sheet, header row, skip rows
                        try:
                            import pandas as _epd
                            import io as _eio
                            _xl = _epd.ExcelFile(_eio.BytesIO(bytes_data))
                            _sheet_names = _xl.sheet_names
                        except Exception:
                            _sheet_names = ["Sheet1"]

                        _xcol1, _xcol2, _xcol3 = st.columns(3)
                        _sheet = _xcol1.selectbox(
                            "Sheet", _sheet_names, key="stg_xl_sheet"
                        ) if len(_sheet_names) > 1 else _sheet_names[0]
                        _header_row = _xcol2.number_input(
                            "Header row", min_value=1, max_value=50,
                            value=1, key="stg_xl_header") - 1
                        _skip_rows = _xcol3.number_input(
                            "Skip rows after header", min_value=0, max_value=50,
                            value=0, key="stg_xl_skip"
                        )
                        st.divider()
                    else:
                        _sheet, _header_row, _skip_rows = None, 0, 0

                    # Shared parse options for both CSV and Excel
                    _ocol1, _ocol2, _ocol3 = st.columns(3)

                    # Pre-seed delimiter selectbox from detected value —
                    # only on first render for this file (before user has touched it)
                    _prev_ingest    = getattr(S, "_stg_ingest", None)
                    _detected_delim = getattr(_prev_ingest, "delimiter", "") if _prev_ingest else ""
                    _delim_label_map = {
                        ",": "Comma (,)", "\t": "Tab (\t)",
                        "|": "Pipe (|)", ";": "Semicolon (;)"
                    }
                    _delim_default = _delim_label_map.get(_detected_delim, "Auto-detect")
                    # Only pre-seed if session state key not yet set for this file
                    _delim_seed_key = f"stg_delim_seeded_{uploaded.name}"
                    if _detected_delim and _delim_seed_key not in st.session_state:
                        st.session_state["stg_delim"] = _delim_default
                        st.session_state[_delim_seed_key] = True

                    _delim_opts = ["Auto-detect", "Comma (,)", "Tab (\t)", "Pipe (|)", "Semicolon (;)"]
                    _delim_choice = _ocol1.selectbox(
                        "Delimiter", _delim_opts, key="stg_delim"
                    )
                    _delim_map = {
                        "Auto-detect": "", "Comma (,)": ",", "Tab (\t)": "\t",
                        "Pipe (|)": "|", "Semicolon (;)": ";",
                    }
                    _delim = _delim_map[_delim_choice]

                    _enc_choice = _ocol2.selectbox(
                        "Encoding", ["Auto-detect", "UTF-8", "Latin-1", "CP1252"],
                        key="stg_enc")
                    _enc_map = {"Auto-detect": "", "UTF-8": "utf-8",
                                "Latin-1": "latin-1", "CP1252": "cp1252"}
                    _enc = _enc_map[_enc_choice]

                    _quote = _ocol3.selectbox(
                        "Quote char", ['Double (")', "Single (')"], key="stg_quote")
                    _quotechar = '"' if _quote.startswith("D") else "'"

                # ── Re-parse on option change ─────────────────────────────
                _xl_key = f"{_sheet}_{_header_row}_{_skip_rows}" if is_excel else ""
                _parse_key = f"{uploaded.name}_{_delim}_{_enc}_{_quotechar}_{_xl_key}"
                if getattr(S, "_stg_parse_key", None) != _parse_key:
                    ingest = ingest_file(bytes_data, uploaded.name,
                                         delimiter=_delim,
                                         encoding=_enc,
                                         quotechar=_quotechar,
                                         sheet_name=_sheet if is_excel else None,
                                         header=_header_row if is_excel else 0,
                                         skiprows=_skip_rows if is_excel else 0)
                    S._stg_ingest    = ingest
                    S._stg_parse_key = _parse_key
                    S._stg_bytes     = bytes_data
                    S._stg_filename  = uploaded.name
                    S._stg_data_hash = None   # computed after staging load when df available
                    S._stg_file_recorded = False  # reset so new file gets a new record
                    # New file or parse options changed — reset mapping so columns refresh
                    S.col_mapping             = None
                    S.fk_samples_loaded       = False
                    S["mapping_grid_snapshot"] = None
                else:
                    ingest = S._stg_ingest

                if not ingest.ok:
                    st.error(ingest.message)
                else:
                    for w in ingest.warnings:
                        st.warning(w)

                    # ── Stats + Preview ───────────────────────────────────
                    mrow([
                        ("Rows",     f"{ingest.row_count:,}" if ingest.row_count >= 0 else "pending", "#79c0ff"),
                        ("Columns",  ingest.col_count,        "#79c0ff"),
                        ("Staging",  ingest.staging_name,     "#8b949e"),
                        ("Encoding", _enc or "auto",          "#8b949e"),
                    ])
                    _prev_rows = st.slider("Preview rows", 5, 500, 20, 5,
                                           key="stg_preview_rows")
                    _prev_data = preview_csv(ingest, n=_prev_rows)
                    if _prev_data:
                        import pandas as _pd
                        st.dataframe(_pd.DataFrame(_prev_data),
                                     use_container_width=True, height=180)

                    if st.button(f"📥 Load to `{ingest.staging_name}` ({ingest.col_count} columns)",
                                 type="primary", use_container_width=True):
                        with st.spinner("Loading to staging via BCP..."):
                            print(f"[STAGING DEBUG] Starting load: table={ingest.staging_name}, "
                                  f"cols={ingest.col_count}, csv_path={ingest.csv_path}, "
                                  f"delimiter='{ingest.delimiter}'")
                            if S.demo:
                                sr = load_to_staging_demo(ingest)
                            else:
                                # Use staging_name from ingest — unique per filename,
                                # avoids RTM loads overwriting the pipeline staging table
                                _t_stg = _t_now()
                                sr = load_to_staging(S.engine, ingest,
                                                     table_name=ingest.staging_name)
                                _t_mark("Stage 2 · load_to_staging (BCP)", _t_stg)
                                # sr.table_name may be "schema.table" — extract parts
                                _stg_full = sr.table_name or ""
                                if "." in _stg_full:
                                    S.stg_schema, S.stg_table = _stg_full.split(".", 1)
                                else:
                                    S.stg_table = _stg_full
                                    # Oracle has no 'stg' schema — use current user schema
                                    try:
                                        from dataview.core.db import get_dialect as _sg_gd
                                        if S.engine and _sg_gd(S.engine).name == "oracle":
                                            from sqlalchemy import text as _sg_t
                                            with S.engine.connect() as _sg_c:
                                                S.stg_schema = _sg_c.execute(_sg_t(
                                                    "SELECT SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM dual"
                                                )).scalar() or "stg"
                                        else:
                                            S.stg_schema = "stg"
                                    except Exception:
                                        S.stg_schema = "stg"
                        print(f"[STAGING DEBUG] Result: ok={sr.ok}, message={sr.message}, "
                              f"rows={getattr(sr, 'rows_loaded', '?')}, table={sr.table_name}")
                        if sr.ok:
                            import pandas as _pd
                            # Read staging_df from the actual DB table (not CSV preview)
                            # to ensure column names always match the real table
                            try:
                                _stg_sch = getattr(S, "stg_schema", "stg")
                                _stg_tbl = getattr(S, "stg_table", sr.table_name.split(".")[-1])
                                from sqlalchemy import text as _ldt
                                with S.engine.connect() as _ldc:
                                    S.staging_df = _pd.read_sql(
                                        _ldt(f"SELECT TOP 500 * FROM [{_stg_sch}].[{_stg_tbl}]"), _ldc)
                            except Exception:
                                # Fallback to CSV preview
                                _prev = preview_csv(ingest, n=500)
                                _prev_df = _pd.DataFrame(_prev) if _prev else _pd.DataFrame(columns=ingest.columns)
                                _prev_df = _prev_df[[c for c in _prev_df.columns if c is not None and str(c).strip() != '']]
                                S.staging_df = _prev_df
                            # Reset all downstream state so new columns are picked up
                            S.norm_df              = None
                            S.col_mapping          = None
                            S.fk_samples_loaded    = False
                            S.fk_checked           = False
                            S.fk_graph             = None
                            S.fk_violations        = None
                            S.val_report           = None
                            S.promoted             = False
                            S["mapping_grid_snapshot"] = None
                            S._mapping_src_cols    = None
                            # Bump grid version — new file means new source columns
                            st.session_state["mapping_grid_ver"] = (
                                st.session_state.get("mapping_grid_ver", 0) + 1
                            )
                            # Compute hash and write file record at load time
                            try:
                                if not S.demo and S.staging_df is not None and not getattr(S, "_stg_file_recorded", False):
                                    _hash = compute_data_hash(S.staging_df)
                                    S._stg_data_hash = _hash
                                    _is_dup, _dup_ts = _write_file_record(
                                        S.engine, "stg",
                                        getattr(S, "_stg_filename", "unknown"),
                                        _hash,
                                        S.target_table or "unknown",
                                        ingest.row_count,
                                        ingest.col_count,
                                    )
                                    S._stg_file_recorded = True
                                    if _is_dup:
                                        print(f"[STAGING] Duplicate warning: loaded before on {_dup_ts}")
                            except Exception as _fre:
                                print(f"[STAGING] File record step skipped: {_fre}")
                            print(f"[STAGING] Success: {sr.message}")
                            st.success(sr.message)
                            # Show bad lines download if any
                            _bad_lp = getattr(ingest, 'bad_lines_path', '')
                            if _bad_lp and _os.path.exists(_bad_lp):
                                with open(_bad_lp, 'rb') as _blf:
                                    st.download_button(
                                        f"⚠️ Download skipped rows",
                                        data=_blf.read(),
                                        file_name=_os.path.basename(_bad_lp),
                                        mime="text/csv",
                                        key="dl_bad_lines_stage1",
                                    )
                            go()
                        else:
                            st.error(sr.message)


    # ═══════════════════════════════════════════════════════════════════════
    # STAGE 3 · NORMALIZE
    # ═══════════════════════════════════════════════════════════════════════
    elif S.stage == 2:
        # ── Stage 3 (Normalize) RETIRED ────────────────────────────────
        # promote does TRIM / NULLIF / TRY_CONVERT / length-truncation
        # server-side, dv_value_map conforms canonical vocabulary at Stage 6,
        # and the DB collation is case-insensitive — so a separate normalize
        # pass is redundant. Pass through instantly (go()/back() already skip
        # this slot; this handles a direct jump to it). REVERSIBLE: delete
        # this block to restore the legacy normalize UI preserved below.
        S.norm_df = S.staging_df if S.staging_df is not None else S.source_df
        S.stage = 3
        st.rerun()

        # ---- legacy normalize UI (unreachable; kept for one-delete restore) ----
        shdr("Stage 3 · Normalize Staging Data",
             "Auto-applies: TRIM whitespace · UPPER indicators & codes · Standardize dates to ISO")

        # Refresh staging_df from DB to avoid stale column names from bad loads
        if S.engine and not S.demo:
            try:
                _t_s3ref = _t_now()
                _stg_sch = getattr(S, "stg_schema", "stg")
                _stg_tbl = getattr(S, "stg_table", "raw_data")
                from sqlalchemy import text as _rft
                import pandas as _rfpd
                with S.engine.connect() as _rc:
                    _rdf = _rfpd.read_sql(
                        _rft(f"SELECT TOP 500 * FROM [{_stg_sch}].[{_stg_tbl}]"), _rc)
                _t_mark("Stage 3 · staging refresh", _t_s3ref)
                if not _rdf.empty:
                    # Reset norm_df if column count changed (stale from bad load)
                    if S.norm_df is not None and len(S.norm_df.columns) != len(_rdf.columns):
                        S.norm_df = None
                    S.staging_df = _rdf
            except Exception:
                pass

        df = S.staging_df if S.staging_df is not None else S.source_df
        if df is None and not (S.engine or S.demo):
            st.error("No staging data found. Please go back to Stage 2.")
        else:
            # ── Preview stg.raw_data from DB ──────────────────────────────
            _prev_n = st.slider("Preview rows", 5, 100, 10, 5, key="norm_preview_n")
            if S.engine and not S.demo:
                _t_s3prev = _t_now()
                _ok, _msg, _rows = preview_staging_table(S.engine, getattr(S,"stg_table","raw_data"), getattr(S,"stg_schema","stg"), _prev_n)
                _t_mark("Stage 3 · preview query", _t_s3prev)
                if _ok and _rows:
                    import pandas as _pd
                    st.caption(f"📄 **{getattr(S,'stg_schema','stg')}.{getattr(S,'stg_table','raw_data')}** — {_msg}")
                    st.dataframe(_pd.DataFrame(_rows), use_container_width=True, height=220)
                elif _ok and not _rows:
                    st.info(f"{getattr(S,'stg_schema','stg')}.{getattr(S,'stg_table','raw_data')} is empty.")
                else:
                    if df is not None:
                        st.caption("Preview from memory (staging table not yet loaded):")
                        st.dataframe(df.head(_prev_n), use_container_width=True, height=220)
                    else:
                        st.warning(_msg)
            elif df is not None:
                st.dataframe(df.head(_prev_n), use_container_width=True, height=220)

            # Schema col types if available
            col_types = {}
            if S.ppdm_schema and S.target_table:
                tbl = S.ppdm_schema.get_table(S.target_table)
                if tbl:
                    col_types = {c.column_name.lower(): c.data_type for c in tbl.columns}

            if S.norm_df is None:
                # Auto-skip normalization — promote handles trim/date/upper server-side
                S.norm_df = df
                st.rerun()

            if S.norm_df is not None:
                _date_fmt_map = {
                    "DD-MM-YYYY (DMY)": "DMY",
                    "MM-DD-YYYY (MDY)": "MDY",
                    "YYYY-MM-DD (YMD)": "YMD",
                }
                st.divider()
                _c1, _c2, _c3 = st.columns([3, 2, 2])
                with _c1:
                    _date_fmt = st.selectbox(
                        "Date format in source",
                        ["Auto-detect", "DD-MM-YYYY (DMY)", "MM-DD-YYYY (MDY)", "YYYY-MM-DD (YMD)"],
                        key="norm_date_fmt")
                with _c2:
                    st.markdown("<div style='margin-top:1.65rem'></div>", unsafe_allow_html=True)
                    if st.button("⚙️ Run Normalization", use_container_width=True):
                        with st.spinner("Normalizing..."):
                            if S.demo:
                                result = normalize_demo(df, col_types)
                            else:
                                result = normalize_server(
                                    S.engine, getattr(S, "stg_table", "raw_data"),
                                    df, col_types, schema=getattr(S, "stg_schema", "stg"),
                                    date_format=_date_fmt_map.get(_date_fmt)
                                )
                        if not result.ok:
                            st.error(result.message)
                        else:
                            S.norm_df = result.df
                            st.rerun()
                with _c3:
                    st.markdown("<div style='margin-top:1.65rem'></div>", unsafe_allow_html=True)
                    if st.button("Continue →", use_container_width=True, type="primary"):
                        go()

    # ═══════════════════════════════════════════════════════════════════════
    # STAGE 4 · SELECT TARGET TABLE
    # ═══════════════════════════════════════════════════════════════════════
    elif S.stage == 3:
        _stage_help("Pick the DataView golden table to load into (e.g. dv_well) and review its columns and types before mapping.")
        shdr(
            "Stage 4 · Choose Target Database Table",
            "Pick the DataView table to load this data into.",
        )

        # ── 4a. Schema status ───────────────────────────────────────────
        if S.ppdm_schema is None:
            # Auto-load schema from schema_registry
            _schema_dir = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "schema_registry")
            _schema_files = sorted([
                f for f in _os.listdir(_schema_dir)
                if f.endswith(".json") and "schema_domain" in f
            ]) if _os.path.isdir(_schema_dir) else []

            if not _schema_files:
                st.error("No schema files found in schema_registry/. Run generate_dataview_schema.py first.")
                st.stop()

            _labels = {f: f.replace("_schema_domain", "").replace(".json", "").replace("_", " ").strip().upper() or "DEFAULT"
                       for f in _schema_files}
            _default = "dataview_schema_domain.json" if "dataview_schema_domain.json" in _schema_files else _schema_files[0]

            # Auto-load the default data model — no separate page, no button.
            # (If you later keep multiple catalogs and want to switch between
            #  them, that picker belongs in the sidebar next to the connection.)
            _chosen = _default
            try:
                import json as _sj4
                with open(_os.path.join(_schema_dir, _chosen), encoding="utf-8") as _sf4:
                    _raw4 = _sj4.load(_sf4)
                from dataview.core.schema import load_schema_from_dict, EXPECTED_ROOT_KEY
                # Auto-detect root key
                for _k4, _v4 in _raw4.items():
                    if isinstance(_v4, list):
                        if _k4 != EXPECTED_ROOT_KEY:
                            _raw4[EXPECTED_ROOT_KEY] = _raw4.pop(_k4)
                        break
                S.ppdm_schema = load_schema_from_dict(_raw4)
                S.schema_variant = _labels.get(_chosen, _chosen)
                print(f"[PIPELINE] Schema auto-loaded: {S.ppdm_schema.summary()}")
                st.rerun()
            except Exception as _se4:
                st.error(f"Schema load failed: {_se4}")
                st.stop()
        else:
            schema = S.ppdm_schema
            st.caption(f"📐 Schema: **{S.schema_variant}** — {schema.summary()}")

            # ── 4b. Category + table selection ──────────────────────────
            col_left, col_right = st.columns([1, 3])

            with col_left:
                cat_options = ["(all categories)"] + schema.all_categories
                cat = st.selectbox("Filter by category", cat_options)

            with col_right:
                if cat == "(all categories)":
                    table_list = schema.all_table_names
                else:
                    table_list = schema.table_names_for_category(cat)

                if not table_list:
                    st.warning(f"No tables found in category '{cat}'.")
                    st.stop()

                # Rank tables by how many source columns they share, so the
                # best-fitting targets float to the top of the dropdown and the
                # match count shows in each option.
                _src_df_match = (S.staging_df if S.staging_df is not None
                                 else S.source_df)
                _src_cols_norm = ({str(c).strip().upper()
                                   for c in _src_df_match.columns}
                                  if _src_df_match is not None else set())
                _overlap = {}
                if _src_cols_norm:
                    for _tn in table_list:
                        _td = schema.get_table(_tn)
                        _tcols = ({c.column_name.strip().upper()
                                   for c in _td.columns} if _td else set())
                        _overlap[_tn] = len(_src_cols_norm & _tcols)
                    table_list = sorted(
                        table_list, key=lambda t: (-_overlap.get(t, 0), t))
                    # Show only the 5 best-ranked targets. Keep the current pick
                    # if it happens to rank outside the top 5 so it never vanishes.
                    _top5 = table_list[:5]
                    if (S.target_table and S.target_table in table_list
                            and S.target_table not in _top5):
                        _top5.append(S.target_table)
                    table_list = _top5

                def _tbl_label(t):
                    if not _src_cols_norm:
                        return t
                    n = _overlap.get(t, 0)
                    return f"{t}   ·   {n} column match{'' if n == 1 else 'es'}"

                # Pre-select previously chosen table, else the best match (top).
                default_idx = 0
                if S.target_table and S.target_table in table_list:
                    default_idx = table_list.index(S.target_table)

                selected = st.selectbox("Target table", table_list,
                                        index=default_idx,
                                        format_func=_tbl_label)
                if _src_cols_norm:
                    st.caption("Top 5 by source-column overlap — best matches "
                               "first. Narrow the category to surface others.")
                if _src_cols_norm and _overlap.get(selected, 0) == 0:
                    st.caption("⚠️ None of your source columns match this table — "
                               "double-check it's the right target.")

            tbl_def = schema.get_table(selected) if selected else None

            if tbl_def:
                # ── 4c. Table metadata summary ───────────────────────────
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Columns",    len(tbl_def.columns))
                c2.metric("PK columns", len(tbl_def.pk_columns))
                c3.metric("FK columns", len(tbl_def.fk_columns))
                c4.metric("Required",   len(tbl_def.required_columns))

                with st.expander("📋 View column definitions"):
                    _tab_cols, _tab_fko, _tab_fki = st.tabs(["Columns", "FK Out", "FK In"])
                    from sqlalchemy import text as _stext

                    def _run_tab_query(_sql, _tbl, _eng):
                        try:
                            with _eng.connect() as _con:
                                _rows = _con.execute(_stext(_sql), {"tbl": _tbl}).fetchall()
                                if _rows:
                                    return pd.DataFrame(_rows, columns=list(_rows[0]._fields))
                            return pd.DataFrame()
                        except Exception:
                            return pd.DataFrame()

                    with _tab_cols:
                        if S.engine and not S.demo:
                            _df_c = _run_tab_query("""
                                SELECT c.column_id AS [#], c.name AS [Column],
                                    tp.name + CASE
                                        WHEN tp.name IN ('nvarchar','nchar') THEN '('+CASE WHEN c.max_length=-1 THEN 'MAX' ELSE CAST(c.max_length/2 AS VARCHAR) END+')'
                                        WHEN tp.name IN ('varchar','char')   THEN '('+CASE WHEN c.max_length=-1 THEN 'MAX' ELSE CAST(c.max_length   AS VARCHAR) END+')'
                                        WHEN tp.name IN ('decimal','numeric') THEN '('+CAST(c.precision AS VARCHAR)+','+CAST(c.scale AS VARCHAR)+')'
                                        ELSE '' END AS [Type],
                                    CASE WHEN c.is_nullable=1 THEN 'YES' ELSE 'NO' END AS [Nullable],
                                    CASE WHEN pk.column_id IS NOT NULL THEN 'PK' ELSE '' END AS [Key]
                                FROM sys.columns c
                                JOIN sys.types tp ON tp.user_type_id=c.user_type_id
                                JOIN sys.tables t ON t.object_id=c.object_id
                                JOIN sys.schemas s ON s.schema_id=t.schema_id
                                LEFT JOIN (SELECT ic.column_id,ic.object_id FROM sys.index_columns ic
                                    JOIN sys.indexes i ON i.object_id=ic.object_id AND i.index_id=ic.index_id
                                    WHERE i.is_primary_key=1) pk ON pk.object_id=c.object_id AND pk.column_id=c.column_id
                                WHERE s.name='dataview' AND t.name=:tbl ORDER BY c.column_id""", selected, S.engine)
                        else:
                            _df_c = pd.DataFrame([{"#": i+1, "Column": c.column_name, "Type": c.data_type,
                                "Nullable": "NO" if c.not_null else "YES",
                                "Key": "PK" if c.is_primary_key else (f"FK→{c.fk_table_name}" if c.is_foreign_key else "")}
                                for i, c in enumerate(tbl_def.columns)])
                        if not _df_c.empty:
                            st.dataframe(_df_c, use_container_width=True, hide_index=True, height=380,
                                column_config={"#": st.column_config.NumberColumn(width="small"),
                                    "Column": st.column_config.TextColumn(width="medium"),
                                    "Type": st.column_config.TextColumn(width="medium"),
                                    "Nullable": st.column_config.TextColumn(width="small"),
                                    "Key": st.column_config.TextColumn(width="medium")})

                    with _tab_fko:
                        if S.engine and not S.demo:
                            _df_fo = _run_tab_query("""
                                SELECT fk.name AS [FK Name],
                                    STRING_AGG(cc.name,', ') WITHIN GROUP (ORDER BY fkc.constraint_column_id) AS [Column(s)],
                                    ps.name+'.'+pt.name AS [References Table],
                                    STRING_AGG(pc.name,', ') WITHIN GROUP (ORDER BY fkc.constraint_column_id) AS [Ref Column(s)]
                                FROM sys.foreign_keys fk
                                JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id=fk.object_id
                                JOIN sys.tables ct ON ct.object_id=fk.parent_object_id
                                JOIN sys.schemas cs ON cs.schema_id=ct.schema_id
                                JOIN sys.columns cc ON cc.object_id=fk.parent_object_id AND cc.column_id=fkc.parent_column_id
                                JOIN sys.tables pt ON pt.object_id=fk.referenced_object_id
                                JOIN sys.schemas ps ON ps.schema_id=pt.schema_id
                                JOIN sys.columns pc ON pc.object_id=fk.referenced_object_id AND pc.column_id=fkc.referenced_column_id
                                WHERE cs.name='dataview' AND ct.name=:tbl
                                GROUP BY fk.name,ps.name,pt.name ORDER BY fk.name""", selected, S.engine)
                            if not _df_fo.empty:
                                st.dataframe(_df_fo, use_container_width=True, hide_index=True)
                                st.caption(f"{len(_df_fo)} FK constraint(s)")
                            else:
                                st.caption("No outbound FK constraints.")
                        else:
                            st.caption("Connect to a database to view FK constraints.")

                    with _tab_fki:
                        if S.engine and not S.demo:
                            _df_fi = _run_tab_query("""
                                SELECT fk.name AS [FK Name],
                                    cs.name+'.'+ct.name AS [From Table],
                                    STRING_AGG(cc.name,', ') WITHIN GROUP (ORDER BY fkc.constraint_column_id) AS [From Column(s)],
                                    STRING_AGG(pc.name,', ') WITHIN GROUP (ORDER BY fkc.constraint_column_id) AS [Ref Column(s)]
                                FROM sys.foreign_keys fk
                                JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id=fk.object_id
                                JOIN sys.tables ct ON ct.object_id=fk.parent_object_id
                                JOIN sys.schemas cs ON cs.schema_id=ct.schema_id
                                JOIN sys.columns cc ON cc.object_id=fk.parent_object_id AND cc.column_id=fkc.parent_column_id
                                JOIN sys.tables pt ON pt.object_id=fk.referenced_object_id
                                JOIN sys.schemas ps ON ps.schema_id=pt.schema_id
                                JOIN sys.columns pc ON pc.object_id=fk.referenced_object_id AND pc.column_id=fkc.referenced_column_id
                                WHERE ps.name='dataview' AND pt.name=:tbl
                                GROUP BY fk.name,cs.name,ct.name ORDER BY cs.name,ct.name""", selected, S.engine)
                            if not _df_fi.empty:
                                st.dataframe(_df_fi, use_container_width=True, hide_index=True)
                                st.caption(f"{len(_df_fi)} table(s) reference {selected}")
                            else:
                                st.caption("No tables reference this table.")
                        else:
                            st.caption("Connect to a database to view FK constraints.")



                # ── 4d. Confirm selection ────────────────────────────────
                if st.button(
                    f"✅ Use  `{selected}`  as target table →",
                    use_container_width=True,
                    type="primary",
                ):
                    S.target_table = selected
                    # When a live engine is available, supplement the JSON schema
                    # columns with any columns present in the DB but missing from
                    # the JSON (e.g. FORMATION_AT_TD, PRODUCING_FORMATION).
                    _db_cols = []
                    if not S.demo and S.engine:
                        # Cache DB column lookup per table — only query once
                        _db_col_cache = getattr(S, "_db_col_cache", {})
                        if selected not in _db_col_cache:
                            try:
                                from sqlalchemy import text as _t
                                from dataview.core.schema import ColumnDef as _CD
                                with S.engine.connect() as _ec:
                                    _db_rows = _ec.execute(_t(
                                        "SELECT c.name, tp.name, c.is_nullable, "
                                        "       c.max_length, "
                                        "       CASE WHEN pk.column_id IS NOT NULL THEN 1 ELSE 0 END AS is_pk, "
                                        "       CASE WHEN fk.parent_column_id IS NOT NULL THEN 1 ELSE 0 END AS is_fk, "
                                        "       fkt.name AS fk_table "
                                        "FROM sys.columns c "
                                        "JOIN sys.types tp ON tp.user_type_id = c.user_type_id "
                                        "JOIN sys.tables t ON t.object_id = c.object_id "
                                        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
                                        "LEFT JOIN ("
                                        "  SELECT ic.column_id, ic.object_id FROM sys.indexes i "
                                        "  JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id "
                                        "  WHERE i.is_primary_key=1) pk "
                                        "  ON pk.object_id=c.object_id AND pk.column_id=c.column_id "
                                        "LEFT JOIN sys.foreign_key_columns fk "
                                        "  ON fk.parent_object_id=c.object_id AND fk.parent_column_id=c.column_id "
                                        "LEFT JOIN sys.tables fkt ON fkt.object_id=fk.referenced_object_id "
                                        "WHERE LOWER(t.name)=LOWER(:tbl) AND s.name='dataview' "
                                        "ORDER BY c.column_id"
                                    ), {"tbl": selected}).fetchall()
                                _json_names = {c.column_name.upper() for c in tbl_def.columns}
                                _extra = []
                                for _r in _db_rows:
                                    if _r[0].upper() not in _json_names:
                                        _extra.append(_CD(
                                            table_schema   = "dataview",
                                            table_name     = selected,
                                            column_name    = _r[0],
                                            data_type      = _r[1],
                                            not_null       = not bool(_r[2]),
                                            is_primary_key = bool(_r[4]),
                                            is_foreign_key = bool(_r[5]),
                                            fk_table_schema= "dataview" if _r[5] else None,
                                            fk_table_name  = _r[6],
                                            fk_column_name = None,
                                            check_constraints = [],
                                        ))
                                _db_col_cache[selected] = _extra
                                S._db_col_cache = _db_col_cache
                            except Exception:
                                _db_col_cache[selected] = []
                                S._db_col_cache = _db_col_cache
                        _db_cols = _db_col_cache.get(selected, [])
                    S.target_cols  = tbl_def.columns + _db_cols
                    # Clear downstream state so Stage 5 re-runs mapping fresh
                    S.column_map     = None
                    S.fk_resolutions = None
                    S.validation_df  = None
                    # Bump grid version so data_editor discards stale widget cache
                    st.session_state["mapping_grid_ver"] = (
                        st.session_state.get("mapping_grid_ver", 0) + 1
                    )
                    st.session_state["mapping_grid_tbl"] = selected
                    S["mapping_grid_snapshot"] = None
                    go()

    # ═══════════════════════════════════════════════════════════════════════
    # STAGE 5 · MATCH & MAP
    # ═══════════════════════════════════════════════════════════════════════
    elif S.stage == 4:
        _stage_help("Map each source column to a target column. Auto-match suggests mappings; you can set constants, apply transforms, and FK columns show sample reference values.")
        shdr("Stage 5 · Match & Map",
             "Map source columns, set constants, and apply transforms. FK columns show sample values.")

        df = S.staging_df if S.staging_df is not None else S.source_df
        if df is None or not S.target_cols:
            if S.target_table and S.target_cols and S.col_mapping is None:
                _src_cols_est = getattr(S, "_mapping_src_cols", None) or []
                if _src_cols_est:
                    S.col_mapping = build_mapping(
                        S.target_table, S.target_cols, _src_cols_est)
                    _fp_est = mapping_fingerprint(S.target_table, _src_cols_est)
                    _n_est  = restore_mapping_from_disk(S.col_mapping, _fp_est)
                    if _n_est:
                        st.info(
                            f"💾 Mapping restored ({_n_est} column(s)) — "
                            "please re-upload your source file to continue.")
                    else:
                        st.warning(
                            "Session restarted — please re-upload your source file "
                            "in Stage 2 to continue.")
                else:
                    st.warning(
                        "Session restarted — please re-upload your source file "
                        "in Stage 2 to continue.")
            else:
                st.error("Missing data or target schema. Please go back.")
        else:
            src_cols = list(df.columns)

            # Build/rebuild mapping if None OR if source columns differ from last build
            _prior_src_cols = getattr(S, "_mapping_src_cols", None)
            if S.col_mapping is None or _prior_src_cols != sorted(src_cols):
                S.col_mapping              = build_mapping(S.target_table, S.target_cols, src_cols)
                S.fk_samples_loaded        = False
                S["mapping_grid_snapshot"] = None
                S._mapping_src_cols        = sorted(src_cols)
                S._mapping_built_for_parse_key = getattr(S, "_stg_parse_key", None)
                # ── Fingerprint restore (disk-backed) ─────────────────────
                # The saved fingerprint was built after staging which adds
                # Always key fingerprint without _batch_loaded_at so batch loader matches
                _fp_cols_without = [c for c in src_cols if c.lower() != "_batch_loaded_at"]
                _fp = mapping_fingerprint(S.target_table, _fp_cols_without)
                S._mapping_fingerprint = _fp
                _n_restored = restore_mapping_from_disk(S.col_mapping, _fp)
                if _n_restored:
                    st.toast(f"↩ Mapping restored ({_n_restored} column(s))", icon="✅")
                else:
                    # Debug — show fingerprint so we can compare with cache
                    st.caption(f"🔍 Fingerprint: `{_fp}` · src cols: {sorted(_fp_cols_without)[:5]}…")

            mp = S.col_mapping

            # ── Load FK samples once per mapping session ──────────────────
            if not S.fk_samples_loaded and not S.demo and S.engine:
                fk_cols = [m for m in mp.mapped if m.is_fk and m.fk_table and not m.auto_generated]
                if fk_cols:
                    with st.spinner("Loading FK sample values..."):
                        # Batch all parent tables in a single round trip
                        unique_tables = list({m.fk_table.strip() for m in fk_cols})
                        samples_map = load_fk_samples_batch(S.engine, unique_tables)
                        for m in fk_cols:
                            mp.set_fk_samples(m.ppdm_col, samples_map.get(m.fk_table.strip(), []))
                S.fk_samples_loaded = True

            # ── Summary metrics ───────────────────────────────────────────
            n_total        = len(mp.mapped)
            n_mapped       = mp.mapped_count
            n_req          = mp.required_count
            n_unmapped_req = len(mp.unmapped_required)
            mrow([
                ("Total Columns",     n_total,        "#1a6fdb"),
                ("Mapped",            n_mapped,        "#1e7e34" if n_mapped == n_total else "#1a6fdb"),
                ("Required",          n_req,           "#c45c00"),
                ("Required Unmapped", n_unmapped_req,  "#c0392b" if n_unmapped_req else "#1e7e34"),
            ])
            st.markdown("")

            if n_unmapped_req:
                st.warning(f"⚠️ {n_unmapped_req} required column(s) not yet mapped: "
                           f"`{'`, `'.join(m.ppdm_col for m in mp.unmapped_required)}`")

            # ── Clear All / Match All ─────────────────────────────────────
            ca1, ca2, ca3 = st.columns([1, 1, 5])
            with ca1:
                if st.button("✖ Clear All", use_container_width=True):
                    for m in mp.mapped:
                        if not m.auto_generated:
                            mp.set_source(m.ppdm_col, "")
                    S.fk_samples_loaded = False
                    S["mapping_grid_snapshot"] = None
                    st.session_state["mapping_grid_ver"] = st.session_state.get("mapping_grid_ver", 0) + 1
                    st.rerun()
            with ca2:
                if st.button("⚡ Match All", use_container_width=True):
                    S.col_mapping       = None
                    S.fk_samples_loaded = False
                    S["mapping_grid_snapshot"] = None
                    st.session_state["mapping_grid_ver"] = st.session_state.get("mapping_grid_ver", 0) + 1
                    st.rerun()

            # ── Build grid ────────────────────────────────────────────────
            src_options    = ["— skip —"] + src_cols
            trans_options  = TRANSFORM_OPTIONS
            mapped_visible = sorted(
                [m for m in mp.mapped if not m.auto_generated],
                key=lambda m: (0 if m.is_pk else 1 if m.is_fk else 2 if m.not_null else 3,
                               m.ppdm_col)
            )
            n_audit        = len(mp.mapped) - len(mapped_visible)

            if n_audit:
                st.info(f"ℹ️ {n_audit} audit column(s) (PPDM_GUID, ROW_CREATED_BY, "
                        f"ACTIVE_IND, etc.) are auto-filled by the app and excluded from this grid.")

            # ── Frozen grid snapshot ─────────────────────────────────────
            # Build grid_df from the mapping object only when explicitly refreshed
            # (Apply, Clear All, Match All, Reset). Between reruns the snapshot is
            # stable so data_editor keeps the user's in-progress edits intact.
            def _build_stage5_snapshot(visible):
                rows = []
                for m in visible:
                    _flags = " ".join(f for f, v in [("PK", m.is_pk), ("FK", m.is_fk), ("NN", m.not_null)] if v)
                    rows.append({
                        "Flags":        _flags,
                        "Target Column":  m.ppdm_col,
                        "Source Column": m.source_col if m.source_col else "— skip —",
                        "Constant":     m.const_value,
                        "Transform":    m.transform,
                        "Type":         m.data_type,
                        "Match":        m.match_label,
                        "FK Samples":   "\n".join(m.fk_samples) if m.fk_samples else "",
                    })
                return pd.DataFrame(rows)

            _snap_key = "mapping_grid_snapshot"
            # Clear stale snapshot whenever source cols change
            _cur_src_sig = sorted(df.columns.tolist()) if df is not None else []
            if getattr(S, "_snap_src_sig", None) != _cur_src_sig:
                S["mapping_grid_snapshot"] = None
                S._snap_src_sig = _cur_src_sig
            if S.get(_snap_key) is None:
                _snap_df = _build_stage5_snapshot(mapped_visible)
                # Only blank Source Column if no mapping was restored from disk.
                _has_any_mapped = any(
                    m.source_col for m in mapped_visible if not m.auto_generated
                )
                if not _has_any_mapped:
                    _snap_df["Source Column"] = "— skip —"
                S[_snap_key] = _snap_df
            grid_df = S[_snap_key]

            col_config = {
                "Flags":        st.column_config.TextColumn(
                                    "Flags", disabled=True, width="small",
                                    help="PK = Primary Key  FK = Foreign Key  NN = Not Null"
                                ),
                "Target Column":  st.column_config.TextColumn(disabled=True, width="medium"),
                "Source Column": st.column_config.SelectboxColumn(
                                     "Source Column",
                                     options=src_options,
                                     required=True, width="medium",
                                 ),
                "Constant":     st.column_config.TextColumn(
                                    "Constant", width="small",
                                    help="Fixed value applied to every row. "
                                         "If Source Column is also set, Constant is used as COALESCE fallback."
                                ),
                "Transform":    st.column_config.SelectboxColumn(
                                    "Transform",
                                    options=trans_options,
                                    required=False, width="medium",
                                    help="Applied to Source Column value before insert. "
                                         "UPPER/LOWER/TRIM — string ops. "
                                         "LEFT:N — truncate to N chars. "
                                         "DATE:fmt — CONVERT to datetime2 using SQL format code. "
                                         "CASE:{} — edit JSON map of source→target values. "
                                         "SQL: — type a raw T-SQL expression using {col} as placeholder."
                                ),
                "Type":         st.column_config.TextColumn(disabled=True, width="small"),
                "Match":        st.column_config.TextColumn(disabled=True, width="small"),
                "FK Samples":   st.column_config.TextColumn(
                                    "FK Samples", disabled=True, width="medium",
                                    help="Sample values from the FK parent table — shows expected format"
                                ),
            }

            _n_mapped = sum(1 for m in mapped_visible
                            if m.source_col and m.source_col != "— skip —")
            with st.expander(
                    f"🗺️  Column mapping  ·  {_n_mapped}/{len(mapped_visible)} "
                    f"columns mapped  —  expand to review or edit",
                    expanded=False):
                edited_df = st.data_editor(
                    grid_df,
                    column_config=col_config,
                    use_container_width=True,
                    hide_index=True,
                    height=min(35 * len(mapped_visible) + 38, 380),
                    disabled=["Flags", "Target Column", "Type", "Match", "FK Samples"],
                    key=f"mapping_grid_v{st.session_state.get('mapping_grid_ver', 0)}",
                )

            # ── Detect pending (unsaved) edits ───────────────────────────
            def _apply_mapping_edits(edited, visible, mapping):
                """Write data_editor state back to ColumnMapping object."""
                for orig, row in zip(visible, edited.itertuples(index=False)):
                    new_src   = row[2] if row[2] != "— skip —" else ""
                    new_const = str(row[3]).strip() if row[3] is not None else ""
                    new_trans = str(row[4]).strip() if row[4] is not None else ""
                    if new_src   != orig.source_col:   mapping.set_source(orig.ppdm_col,    new_src)
                    if new_const != orig.const_value:  mapping.set_const(orig.ppdm_col,     new_const)
                    if new_trans != orig.transform:    mapping.set_transform(orig.ppdm_col, new_trans)

            def _has_mapping_edits(edited, visible):
                for orig, row in zip(visible, edited.itertuples(index=False)):
                    if (row[2] if row[2] != "— skip —" else "") != orig.source_col: return True
                    if (str(row[3]).strip() if row[3] is not None else "") != orig.const_value: return True
                    if (str(row[4]).strip() if row[4] is not None else "") != orig.transform:   return True
                return False

            _pending_edits = _has_mapping_edits(edited_df, mapped_visible)
            if _pending_edits:
                st.caption("⚠️ You have unsaved changes — click **Apply Changes** to lock them in.")

            # ── SQL preview for selected row ──────────────────────────────
            # Show the generated SELECT expression so user can verify transforms
            has_transforms = any(m.transform or m.const_value for m in mapped_visible)
            if has_transforms:
                with st.expander("🔍 Generated SQL expressions", expanded=False):
                    expr_rows = []
                    for m in mapped_visible:
                        if m.transform or m.const_value or m.auto_generated:
                            expr_rows.append({
                                "Target Column":   m.ppdm_col,
                                "SELECT expr":   m.select_expr,
                            })
                    if expr_rows:
                        st.dataframe(pd.DataFrame(expr_rows),
                                     use_container_width=True, hide_index=True)

            # ── Actions ───────────────────────────────────────────────────
            col1, col2, col3 = st.columns(3)
            with col1:
                _apply_btn = st.button(
                    "💾 Apply Changes",
                    use_container_width=True,
                    type="primary" if _pending_edits else "secondary",
                    disabled=not _pending_edits,

                )
                if _apply_btn:
                    _apply_mapping_edits(edited_df, mapped_visible, mp)
                    S["mapping_grid_snapshot"] = None   # force snapshot rebuild from updated mp
                    st.session_state["mapping_grid_ver"] = st.session_state.get("mapping_grid_ver", 0) + 1
                    _fp = getattr(S, "_mapping_fingerprint", None) or mapping_fingerprint(S.target_table, list(df.columns))
                    save_mapping_to_disk(_fp, mp)
                    st.rerun()
            with col2:
                if st.button("✅ Confirm Mapping →", use_container_width=True):
                    if _pending_edits:
                        _apply_mapping_edits(edited_df, mapped_visible, mp)
                    S["mapping_grid_snapshot"] = None
                    _fp = getattr(S, "_mapping_fingerprint", None) or mapping_fingerprint(S.target_table, list(df.columns))
                    save_mapping_to_disk(_fp, mp)
                    go()
            with col3:
                if st.button("🔄 Reset Auto-match", use_container_width=True):
                    S.col_mapping       = None
                    S.fk_samples_loaded = False
                    S["mapping_grid_snapshot"] = None
                    st.session_state["mapping_grid_ver"] = st.session_state.get("mapping_grid_ver", 0) + 1
                    st.rerun()

    # ═══════════════════════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════
    # STAGE 6 · FK RESOLUTION
    # ═══════════════════════════════════════════════════════════════════════
    elif S.stage == 5:
        _stage_help("Resolve foreign-key violations: tick Add to put a new value into a reference table, or pick an existing value and tick Map to conform it. Maps are remembered in dv_value_map for future loads.")
        shdr("Stage 6 · FK Resolution",
             "Parent tables in dependency order — deepest first. "
             "Fulfilled constraints disappear as you go.")

        # Clear FK cache button — forces recheck of all FK values
        if st.button("🔄 Re-check FK Values", key="clear_fk_cache_btn"):
            for _ck in list(st.session_state.keys()):
                if (_ck.startswith("_fk_exists_") or _ck.startswith("_fk_graph_")
                        or _ck.startswith("_fk_ever_sat_") or _ck.startswith("_fk_prev_counts_")):
                    del st.session_state[_ck]
            S.fk_checked = False
            st.rerun()

        df = S.staging_df if S.staging_df is not None else S.source_df
        if df is None or not S.col_mapping:
            st.error("Missing data. Please go back.")
        else:
            # ── Value standardization · conform source values to canonical ──
            # Runs once per check cycle (gated on fk_checked), BEFORE the FK
            # graph builds, so province_state / county / well_type / well_status
            # are conformed to dv_value_map canonical vocabulary first. Updates
            # the DB staging table (set-based, for promote) AND the in-memory
            # frame (so the Python-side FK check sees canonical values too).
            _t_s6entry = _t_now()
            if S.engine and not S.demo and not S.fk_checked:
                with st.spinner("Standardizing values against the database…"):
                    try:
                        from dataview.reference_tables.value_standardize import (
                            apply_value_map, strip_state_prefix_county)
                        _t_vm = _t_now()
                        # Normalize county: strip '<state>_' prefix where present so
                        # 'LA_BOSSIER' -> 'BOSSIER' (matches dv_county.county_name).
                        _county_fixed = strip_state_prefix_county(
                            S.engine, getattr(S, "stg_schema", "stg"),
                            getattr(S, "stg_table", "raw_data"), S.col_mapping)
                        S.county_prefix_fixed = _county_fixed
                        _vm_rep = apply_value_map(
                            S.engine, getattr(S, "stg_schema", "stg"),
                            getattr(S, "stg_table", "raw_data"), S.col_mapping)
                        _t_mark("Stage 6 \u00b7 value standardization", _t_vm)
                        S.value_map_report = _vm_rep
                        # No client re-read here — build_fk_graph now reads the
                        # (already-UPDATEd) staging table live via ServerFrame.
                    except Exception as _vme:
                        S.value_map_report = [{"error": str(_vme)}]

            _render_value_standardization(S)
            _t_mark("Stage 6 · valuestd+editor", _t_s6entry)
            _t_buildblock = _t_now()

            # ── Build unified FK dependency graph once ─────────────────────
            # Invalidate if source columns OR active mapping changed since last build
            # fk_checked is reset explicitly by Confirm Mapping and Re-check FKs buttons
            # No auto-invalidation needed — avoids render loops
            if not S.fk_checked:
              with st.spinner("Checking foreign keys against the database…"):
                try:
                    # FK check is fully server-side now: build_fk_graph reads
                    # staging live via ServerFrame (below), and the resolution
                    # panel uses check_fk_violations_server. No staging frame is
                    # pulled to the client — value-standardization's in-place
                    # UPDATEs and any panel Map/Add are seen directly on the DB.
                    # Build FK constraints from schema JSON — instant, no DB queries
                    from dataview.core.fk import (FKConstraint, FKColumn, FKIntrospectResult)
                    _EXCL_INTRO = {
                        'well_area','field_area','well_node','well_bore',
                        'r_source','r_ppdm_row_quality','source_document',
                        'ppdm_measurement_system','ppdm_quantity',
                        'r_ppdm_uom_usage','ppdm_unit_of_measure',
                        'strat_unit','strat_name_set',
                    }
                    _mapped_lower_intro = {
                        m.ppdm_col.lower()
                        for m in (S.col_mapping.mapped if S.col_mapping else [])
                        if getattr(m, 'source_col', '') and not getattr(m, 'auto_generated', False)
                    }
                    _constraints_intro = []
                    _parent_pks_intro: dict = {}
                    _tbl_def_intro = S.ppdm_schema.get_table(S.target_table) if S.ppdm_schema else None
                    if _tbl_def_intro:
                        _seen_intro = set()
                        for _fc_i in _tbl_def_intro.fk_columns:
                            if _fc_i.column_name.lower() not in _mapped_lower_intro:
                                continue
                            _pt_i = _fc_i.fk_table_name
                            _pk_i = _fc_i.fk_column_name
                            if not _pt_i or not _pk_i or _pt_i.lower() in _EXCL_INTRO:
                                continue
                            _cname_i = f"FK_{S.target_table}_{_fc_i.column_name}"
                            if _cname_i not in _seen_intro:
                                _seen_intro.add(_cname_i)
                                _constraints_intro.append(FKConstraint(
                                    constraint_name = _cname_i,
                                    child_table     = S.target_table,
                                    child_schema    = "dataview",
                                    parent_table    = _pt_i,
                                    parent_schema   = "dataview",
                                    columns         = [FKColumn(
                                        fk_col  = _fc_i.column_name,
                                        ref_col = _pk_i,
                                        ordinal = 1,
                                        nullable= not _fc_i.not_null,
                                    )],
                                ))
                                _pt_def_i = S.ppdm_schema.get_table(_pt_i)
                                if _pt_def_i:
                                    _pks_i = [c.column_name for c in _pt_def_i.columns if c.is_primary_key]
                                    if _pks_i:
                                        _parent_pks_intro[_pt_i] = _pks_i

                    # ── Fallback: schema JSON carried no usable FK structure ──
                    # dataview_schema_domain.json lists no fk_columns for some
                    # tables (e.g. dv_well), and derived FK columns like
                    # operator_ba_id are never source-mapped, so the loop above
                    # yields nothing. Introspect the REAL FKs from the live DB
                    # (all 11 exist + trusted) — unfiltered, so derived FK cols
                    # come through and can be seeded/resolved.
                    if not _constraints_intro and S.engine is not None:
                        try:
                            _t_intro = _t_now()
                            # Ask the catalog directly whether it served this —
                            # so the log states catalog-hit vs live sys.* plainly
                            # instead of the old hardcoded "from live DB".
                            _src_label = "live sys.*"
                            try:
                                import importlib as _il_fk
                                _cat = _il_fk.import_module(
                                    "dataview.core.fk_catalog").get_catalog(S.engine)
                                if getattr(_cat, "available", False):
                                    _src_label = "FK catalog (cached)"
                            except Exception:
                                pass
                            _dbintro = introspect_fk_constraints(
                                S.engine, S.target_table, "dataview")
                            if _dbintro and _dbintro.constraints:
                                _constraints_intro = list(_dbintro.constraints)
                                if getattr(_dbintro, "parent_pks", None):
                                    _parent_pks_intro.update(_dbintro.parent_pks)
                                print(f"[FK] schema JSON had 0 FKs for "
                                      f"{S.target_table}; got "
                                      f"{len(_constraints_intro)} via {_src_label}")
                            _t_mark(f"Stage 6 \u00b7 FK introspect ({_src_label})", _t_intro)
                        except Exception as _dbe:
                            print(f"[FK] DB introspection fallback failed: {_dbe}")

                    intro = FKIntrospectResult(
                        ok=True,
                        message=("Built from schema JSON" if _constraints_intro
                                 and S.ppdm_schema else "Introspected from live DB"),
                        constraints=_constraints_intro,
                        parent_pks=_parent_pks_intro,
                    )

                    S.fk_constraints   = intro.constraints
                    all_constraints    = intro.constraints
                    ref_constraints    = [c for c in all_constraints if is_reference_table(c.parent_table)]
                    entity_constraints = [c for c in all_constraints if not is_reference_table(c.parent_table)]
                    _POST_PROMOTE = {'WELL_AREA','FIELD_AREA','WELL_NODE','WELL_BORE'}
                    entity_constraints = [c for c in entity_constraints
                                         if c.parent_table.upper() not in _POST_PROMOTE]

                    S.fk_violations    = []
                    entity_order       = topological_sort(entity_constraints)
                    S.fk_entity_tables = entity_order
                    S.fk_entity_mappings = {}
                    S.fk_entity_resolved = {t: False for t in entity_order}
                    for tname in entity_order:
                        tschema = next((c.parent_schema for c in entity_constraints
                                       if c.parent_table.upper() == tname.upper()), "dataview")
                        em = EntityMapping(table_name=tname, schema=tschema, columns=[],
                                          config=KNOWN_ENTITY_TABLES.get(tname.upper()))
                        S.fk_entity_mappings[tname] = em

                    clear_parent_values_cache()
                    S.fk_all_constraints = all_constraints
                    S.fk_node_results = {}
                    S.fk_ref_context  = {}
                    S.fk_ref_edits    = {}
                    S._fk_built_for_src_cols = sorted(df.columns.tolist())
                    S._fk_built_for_map_sig  = sorted(
                        (m.ppdm_col, m.source_col or "")
                        for m in (S.col_mapping.mapped if S.col_mapping else [])
                        if not getattr(m, "auto_generated", False)
                    )
                    # ── Build the FK resolution graph ───────────────────────
                    # build_fk_graph was imported but never called, so S.fk_graph
                    # stayed None and Stage 6 always reported "0 constraints —
                    # all satisfied" without ever resolving operators/fields.
                    # Build it here (once, while fk_checked is False) from the
                    # same constraints + mapping the rest of the stage uses.
                    try:
                        _t_bfg = _t_now()
                        # Non-demo: read staging live from the server (no pull).
                        # Demo: in-memory df (no DB).
                        if S.engine and not S.demo:
                            from dataview.core.fk_entity import ServerFrame as _ServerFrame
                            _src_frame = _ServerFrame(
                                S.engine,
                                getattr(S, "stg_schema", "stg"),
                                getattr(S, "stg_table", "raw_data"))
                        else:
                            _src_frame = df
                        _fk_nodes, _ = build_fk_graph(
                            S.engine,
                            all_constraints,
                            _src_frame,
                            S.col_mapping,
                            S.target_table,
                            S.fk_violations,
                        )
                        S.fk_graph = _fk_nodes
                        _t_mark("Stage 6 · build_fk_graph", _t_bfg)
                        if hasattr(_src_frame, "close"):
                            _src_frame.close()
                    except Exception as _gexc:
                        S.fk_graph = []
                        st.error(f"FK graph build failed: {_gexc}")
                        st.exception(_gexc)

                    # ── DIAGNOSTIC: capture the whole constraint→graph chain ──
                    # Temporary. Tells us exactly where the chain breaks:
                    #   schema_loaded False -> ppdm_schema not loaded
                    #   tbl_def_found False -> get_table(target) returned None
                    #   fk_columns_on_def [] -> schema JSON has no FKs for target
                    #   mapped_nonauto_ppdm missing operator_ba_id/field_id ->
                    #       those FK cols aren't mapped (or are auto_generated)
                    #       so line 1339 filters them out -> empty constraints
                    #   constraints_built [] -> nothing survived the 1339 filter
                    #   graph_nodes [] with constraints_built non-empty ->
                    #       build_fk_graph found no missing values
                    try:
                        S._fk_diag = {
                            "schema_loaded": S.ppdm_schema is not None,
                            "target_table": S.target_table,
                            "tbl_def_found": _tbl_def_intro is not None,
                            "fk_columns_on_def": [
                                getattr(c, "column_name", "?")
                                for c in (getattr(_tbl_def_intro, "fk_columns", []) or [])
                            ] if _tbl_def_intro else [],
                            "mapped_nonauto_ppdm": sorted(_mapped_lower_intro),
                            "all_mapped_ppdm": sorted(
                                (m.ppdm_col, bool(m.source_col),
                                 bool(getattr(m, "auto_generated", False)),
                                 getattr(m, "transform", "") or "")
                                for m in (S.col_mapping.mapped if S.col_mapping else [])
                            ),
                            "constraints_built": [c.parent_table for c in all_constraints],
                            "graph_nodes": [
                                (n.table_name, n.node_type, n.depth)
                                for n in (S.fk_graph or [])
                            ],
                        }
                    except Exception as _dexc:
                        S._fk_diag = {"diag_error": str(_dexc)}

                    S.fk_checked = True

                    # fk_graph is now populated above from build_fk_graph()
                except Exception as _exc:
                    st.error(f"FK setup failed: {_exc}")
                    st.exception(_exc)

            # ── FK resolution — governance panel only (legacy UI removed) ──
            _t_mark("Stage 6 · build block", _t_buildblock)
            _t_panel = _t_now()
            try:
                from dataview.core.fk_resolve_panel import render as _render_fk_panel
                with st.spinner("Loading the resolution panel…"):
                    _render_fk_panel(S, st)
            except Exception as _fkpe:
                st.error(f"FK resolution panel error: {_fkpe}")
                st.exception(_fkpe)
            _t_mark("Stage 6 \u00b7 resolve panel", _t_panel)

            st.divider()
            _b1, _b2, _b3 = st.columns([1, 1, 1])
            with _b1:
                if st.button("\u2190 Back", use_container_width=True, key="fk_back_btn"):
                    back()
            with _b2:
                if st.button("\U0001f504 Re-check FKs", use_container_width=True,
                             key="fk_recheck_btn"):
                    for _k in ("fk_checked", "fk_violations", "fk_constraints",
                               "fk_graph", "fk_node_results", "fk_entity_mappings",
                               "fk_entity_resolved", "fk_all_constraints"):
                        setattr(S, _k, False if _k == "fk_checked" else None)
                    for _ck in list(st.session_state.keys()):
                        if _ck.startswith("_fk_"):
                            st.session_state.pop(_ck, None)
                    st.rerun()
            with _b3:
                if st.button("Continue \u2192", use_container_width=True,
                             type="primary", key="fk_continue_btn"):
                    go()


    # STAGE 7 · VALIDATE
    # ═══════════════════════════════════════════════════════════════════════
    elif S.stage == 6:
        _stage_help("Pre-promote checks on the staged data: NOT NULL, data types, max length, check constraints, business rules, and duplicate primary keys.")
        shdr("Stage 7 · Validate",
             "Checks: NOT NULL · data types · max length · check constraints · business rules · duplicate PKs")

        # validate_server runs entirely on SQL Server — no df needed
        # Use norm_df so date validation sees ISO-formatted dates not raw source
        df = S.norm_df if S.norm_df is not None else (
             S.staging_df if S.staging_df is not None else S.source_df)
        if df is None or not S.col_mapping or not S.target_cols:
            st.error("Missing data. Please go back.")
        else:
            if S.val_report is None:
                with st.spinner("Validating..."):
                    _t_val = _t_now()
                    if S.engine and not S.demo:
                        from dataview.core.validate import validate_server
                        S.val_report = validate_server(
                            S.engine, getattr(S,"stg_table","raw_data"),
                            S.col_mapping, S.target_cols,
                            schema=getattr(S,"stg_schema","stg"),
                            target_table=S.target_table or "",
                            checks=("not_null", "duplicate_pk"),
                        )
                    else:
                        S.val_report = validate(
                            df, S.col_mapping, S.target_cols,
                            engine=None,
                            target_table=S.target_table or "",
                        )
                    _t_mark("Stage 7 · validate", _t_val)

            rpt = S.val_report
            mrow([
                ("Rows checked",  rpt.rows_checked,   "#79c0ff"),
                ("Errors",        len(rpt.errors),     "#f85149"),
                ("Warnings",      len(rpt.warnings),   "#f0883e"),
                ("Clean rows",    rpt.clean_row_count, "#56d364"),
            ])
            st.markdown("")

            with st.expander("📋 Validation rules applied", expanded=False):
                _val_rules = [
                    ("NOT NULL",            "Required columns",    "Columns marked NOT NULL or PK must have a value. Failing rows are skipped at promote."),
                    ("Data type",           "All mapped columns",  "Numeric columns must be parseable numbers. Date columns must be valid dates."),
                    ("Max length",          "All string columns",  "Values exceeding the column's defined max length (e.g. nvarchar(40)) are flagged."),
                    ("Check constraints",   "Constrained columns", "Schema check constraints evaluated — e.g. ACTIVE_IND must be 'Y' or 'N'."),
                    ("Duplicate PKs",       "PK columns",          "Rows sharing the same PK as another source row are flagged before hitting the DB."),
                    ("Business rules",      "Rules Manager",       "Custom validation rules from the Rules Manager applied after built-in checks."),
                ]
                _rules_fired = set()
                if rpt.issues:
                    _rules_fired = set(rpt.to_dataframe()["rule"].unique())
                _vr_rows = []
                for _rn, _scope, _detail in _val_rules:
                    _fired = any(_rn.replace(" ", "_").upper() in r.upper()
                                 or r.upper() in _rn.upper() for r in _rules_fired)
                    _vr_rows.append({"Status": "⚠️ Issues found" if _fired else "✅ Passed",
                                     "Rule": _rn, "Scope": _scope, "Detail": _detail})
                st.dataframe(pd.DataFrame(_vr_rows), use_container_width=True, hide_index=True,
                             column_config={
                                 "Status": st.column_config.TextColumn(width="small"),
                                 "Rule":   st.column_config.TextColumn(width="medium"),
                                 "Scope":  st.column_config.TextColumn(width="medium"),
                                 "Detail": st.column_config.TextColumn(width="large"),
                             })

            if rpt.issues:
                df_issues = rpt.to_dataframe()

                # ── Error breakdown — which rules are firing ───────────────
                if rpt.errors:
                    rule_counts = df_issues[df_issues["severity"]=="ERROR"].groupby("rule").size()
                    st.markdown("**Error breakdown by rule:**")
                    bc1, bc2 = st.columns(2)
                    for i, (rule, count) in enumerate(rule_counts.items()):
                        (bc1 if i % 2 == 0 else bc2).markdown(
                            f"- `{rule}` — **{count}** error(s) on "
                            f"**{df_issues[(df_issues['rule']==rule) & (df_issues['severity']=='ERROR')]['row_idx'].nunique()}** row(s)"
                        )
                    st.markdown("")

                # ── Filterable issues table ────────────────────────────────
                sev_filter = st.multiselect(
                    "Filter by severity", ["ERROR", "WARNING"],
                    default=["ERROR", "WARNING"], key="val_sev_filter"
                )
                rule_filter = st.multiselect(
                    "Filter by rule",
                    sorted(df_issues["rule"].unique().tolist()),
                    default=[], key="val_rule_filter",
                    placeholder="All rules"
                )
                filtered = df_issues[df_issues["severity"].isin(sev_filter)]
                if rule_filter:
                    filtered = filtered[filtered["rule"].isin(rule_filter)]

                st.dataframe(
                    filtered[["row_idx","severity","rule","ppdm_col","src_col","value","message"]],
                    use_container_width=True, height=350,
                    column_config={
                        "row_idx":   st.column_config.NumberColumn("Row",      width="small"),
                        "severity":  st.column_config.TextColumn("Severity",   width="small"),
                        "rule":      st.column_config.TextColumn("Rule",       width="medium"),
                        "ppdm_col":  st.column_config.TextColumn("PPDM Col",   width="medium"),
                        "src_col":   st.column_config.TextColumn("Source Col", width="medium"),
                        "value":     st.column_config.TextColumn("Value",      width="medium"),
                        "message":   st.column_config.TextColumn("Message",    width="large"),
                    },
                    hide_index=True,
                )
                st.caption(f"Showing {len(filtered):,} of {len(df_issues):,} issue(s). "
                           f"{len(rpt.error_row_indices)} row(s) will be skipped at promote.")
            else:
                st.success("✅ All rows passed validation!")

            col1, col2 = st.columns(2)
            with col1:
                if st.button("🔄 Re-run Validation", use_container_width=True):
                    S.val_report = None
                    st.rerun()
            with col2:
                if rpt.clean_row_count > 0:
                    if st.button(f"⚡ Promote {rpt.clean_row_count} clean rows →",
                                 use_container_width=True):
                        go()
                else:
                    st.error("No clean rows to promote.")

    # ═══════════════════════════════════════════════════════════════════════
    # STAGE 8 · PROMOTE
    # ═══════════════════════════════════════════════════════════════════════
    elif S.stage == 7:
        _stage_help("Review the generated INSERT SQL and confirm to write the validated staging rows into the target table. Phantom columns are reconciled on promote.")
        shdr("Stage 8 · Promote to Target Table",
             "Review the SQL and confirm to execute the INSERT into the target table.")

        # ── Recovery / navigation bar — always renders, survives promote errors ──
        _STAGE_LABELS = [
            "Stage 1 · Connect to Database",
            "Stage 2 · Upload & Stage",
            "Stage 3 · Normalize",
            "Stage 4 · Select Target Table",
            "Stage 5 · Match & Map",
            "Stage 6 · FK Resolution",
            "Stage 7 · Validate",
            "Stage 8 · Promote",
        ]
        _nav1, _nav2, _nav3 = st.columns([3, 1, 1])
        with _nav1:
            _jump = st.selectbox(
                "Jump back to a stage (keeps your data)",
                options=list(range(len(_STAGE_LABELS))),
                format_func=lambda i: _STAGE_LABELS[i],
                index=min(S.stage, len(_STAGE_LABELS) - 1),
                key="promote_jump_stage",
            )
        with _nav2:
            st.markdown("<div style='height:1.8em'></div>", unsafe_allow_html=True)
            if st.button("Go to stage", use_container_width=True):
                S.stage = int(_jump)
                S.promoted = False
                st.rerun()
        with _nav3:
            st.markdown("<div style='height:1.8em'></div>", unsafe_allow_html=True)
            if st.button("🔄 Reset all", use_container_width=True):
                st.session_state["_confirm_reset"] = True

        if st.session_state.get("_confirm_reset"):
            st.warning("Reset clears the connection, target, mapping, staging data, and "
                       "all FK work, returning to Stage 1. Your DB tables are untouched. "
                       "Confirm?")
            _rc1, _rc2 = st.columns(2)
            with _rc1:
                if st.button("✅ Yes, reset everything", use_container_width=True):
                    st.session_state["_confirm_reset"] = False
                    reset()
            with _rc2:
                if st.button("Cancel", use_container_width=True):
                    st.session_state["_confirm_reset"] = False
                    st.rerun()
        st.divider()

        df = S.staging_df if S.staging_df is not None else S.source_df

        if not S.promoted:
            if df is None or not S.col_mapping or not S.val_report:
                st.error("Missing data. Please go back.")
            else:
                # Build column lists directly from mapping — don't rely on active_pairs
                mp = S.col_mapping
                # Source-mapped columns: ppdm_col → select_expr (or bare [src_col])
                # Collect ppdm_cols that entity nodes have set to NULL
                _entity_null_cols = set()
                _fnr = S.fk_node_results or {}
                for _en in (S.fk_graph or []):
                    if _fnr.get(_en.table_name.upper(), {}).get("action") == "null":
                        for _ecc in (getattr(_en.constraint, "child_cols", []) or []):
                            _entity_null_cols.add(_ecc.upper())

                _mapped_pairs = []
                for m in mp.mapped:
                    if getattr(m, "auto_generated", False):
                        continue
                    # Skip columns nulled out by entity FK null action
                    if m.ppdm_col.upper() in _entity_null_cols:
                        continue
                    src = getattr(m, "source_col", "") or ""
                    if not src:
                        continue
                    # Use select_expr if present, else bare bracketed src col
                    expr = getattr(m, "select_expr", None) or f"[{src}]"
                    _mapped_pairs.append((m.ppdm_col, expr))
                # Auto-generated (audit) columns — server expressions
                # ROW_QUALITY is a FK to r_ppdm_row_quality — exclude from auto-generated
                # literals to avoid FK violations. User must map it explicitly if needed.
                _FK_AUDIT_SKIP = {"ROW_QUALITY"}
                _auto_pairs = [
                    (getattr(m, "ppdm_col", ""), getattr(m, "auto_gen_expr", getattr(m, "select_expr", "")))
                    for m in mp.mapped
                    if getattr(m, "auto_generated", False)
                    and getattr(m, "ppdm_col", "").upper() not in _FK_AUDIT_SKIP
                ]
                pairs = _mapped_pairs + _auto_pairs
                st.caption(f"🔍 Debug: {len(_mapped_pairs)} mapped cols, {len(_auto_pairs)} audit cols · "
                           f"mapping has {sum(1 for m in mp.mapped if getattr(m,'source_col',''))} source cols set")
                tgt_cols_sql = ", ".join(f"[{p}]" for p, _ in pairs)
                src_cols_sql = ", ".join(expr for _, expr in pairs)
                skip_str = ""  # duplicates handled by NOT EXISTS in promote

                _stg_schema = getattr(S, "stg_schema", "stg")
                # stg_table may still carry schema prefix — strip it
                _stg_tbl = (S.stg_table or "").split(".")[-1]
                sql_preview = (
                    f"INSERT INTO [dataview].[{S.target_table}]\n"
                    f"  ({tgt_cols_sql})\n"
                    f"SELECT\n"
                    f"  {src_cols_sql}\n"
                    f"FROM [{_stg_schema}].[{_stg_tbl}]\n"
                    f"-- duplicates excluded via NOT EXISTS on PK"
                )

                # ── Promote mode selector ─────────────────────────────────
                _promote_mode = st.radio(
                    "Promote mode",
                    ["Insert", "Merge (upsert on PK)"],
                    horizontal=True,
                    key="promote_mode_radio",

                )
                _do_merge = _promote_mode.startswith("Merge")

                # If merge, look up PK cols for target table
                _merge_pk_cols = []
                if _do_merge and S.engine:
                    try:
                        from sqlalchemy import text as _txt
                        _pk_rows = S.engine.connect().execute(_txt("""
                            SELECT c.name
                            FROM sys.indexes i
                            JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
                            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                            JOIN sys.tables t ON t.object_id = i.object_id
                            JOIN sys.schemas s ON s.schema_id = t.schema_id
                            WHERE i.is_primary_key = 1 AND t.name = :tbl AND s.name = 'dataview'
                            ORDER BY ic.key_ordinal
                        """), {"tbl": S.target_table}).fetchall()
                        _merge_pk_cols = [r[0] for r in _pk_rows]
                    except Exception:
                        pass
                    if _merge_pk_cols:
                        st.caption(f"PK columns: {', '.join(_merge_pk_cols)}")
                    else:
                        st.warning("Could not determine PK columns — cannot build MERGE ON clause.")

                # ── SQL preview ───────────────────────────────────────────────
                if _do_merge and _merge_pk_cols:
                    _pk_upper = {p.upper() for p in _merge_pk_cols}
                    _update_pairs = [(col, expr) for col, expr in _mapped_pairs
                                     if col.upper() not in _pk_upper]
                    _on_preview = " AND ".join(f"tgt.[{p}] = src.[{p}]" for p in _merge_pk_cols)
                    _upd_preview = ", ".join(f"tgt.[{c}] = src.[{c}]" for c, _ in _update_pairs)
                    sql_preview = (
                        f"MERGE [dataview].[{S.target_table}] AS tgt\n"
                        f"USING (SELECT {src_cols_sql} FROM [{_stg_schema}].[{_stg_tbl}]\n"
                        f"       ) AS src\n"
                        f"ON ({_on_preview})\n"
                        f"WHEN MATCHED THEN\n"
                        f"  UPDATE SET {_upd_preview}\n"
                        f"WHEN NOT MATCHED THEN\n"
                        f"  INSERT ({tgt_cols_sql}) VALUES ({src_cols_sql});"
                    )

                st.markdown("#### SQL to be executed:")
                st.code(sql_preview, language="sql")

                mrow([
                    ("Rows to insert", S.val_report.clean_row_count, "#56d364"),
                    ("Target table",   S.target_table,                "#79c0ff"),
                ])

                if S.demo:
                    st.info("🧪 Demo Mode — SQL will NOT be executed against a real database.")

                _mode_label = "MERGE" if _do_merge else "INSERT"
                _merge_ready = not _do_merge or bool(_merge_pk_cols)

                c1, c2 = st.columns(2)
                with c1:
                    if st.button(
                        f"{'🧪 Simulate' if S.demo else '⚡ Execute'} {_mode_label}  "
                        f"({S.val_report.clean_row_count} rows)",
                        use_container_width=True,
                        disabled=not _merge_ready,
                    ):
                        _data_hash   = getattr(S, "_stg_data_hash", None)
                        _source_file = getattr(S, "_stg_filename", "unknown")

                        try:
                            with st.spinner("Promoting..."):
                                _t_prom = _t_now()
                                # Seed entity parents (operators/fields/…) from
                                # dv_entity_map BEFORE the insert, so the hashed
                                # FK values in dv_well have matching parent rows.
                                # Idempotent (NOT EXISTS); skipped in demo.
                                if not S.demo:
                                    try:
                                        from dataview.import_data.entity_map_seed import (
                                            seed_from_entity_map,
                                            seed_source_values,
                                            reconcile_entity_fk_mapping)
                                        _t_seed = _t_now()
                                        # 1. Retarget OPERATOR/FIELD_NAME mappings
                                        #    to their *_ba_id/field_id columns with
                                        #    SHA1, so promote writes the hashed PK
                                        #    into dv_well (not a phantom name col).
                                        for _rline in reconcile_entity_fk_mapping(
                                                S.col_mapping, S.engine,
                                                S.target_table):
                                            print(f"[ENTITY_MAP] reconcile: {_rline}")
                                        # 2. Seed the parent rows (same hash recipe)
                                        #    so those FK values have matching parents.
                                        for _sline in seed_from_entity_map(
                                                S.engine,
                                                getattr(S, "stg_schema", "stg"),
                                                S.stg_table):
                                            print(f"[ENTITY_MAP] {_sline}")
                                        # 3. Seed dv_r_source with the values the
                                        #    mapping sends to dv_well.source, so the
                                        #    well -> dv_r_source FK resolves.
                                        for _vline in seed_source_values(
                                                S.col_mapping, S.engine,
                                                getattr(S, "stg_schema", "stg"),
                                                S.stg_table):
                                            print(f"[ENTITY_MAP] {_vline}")
                                        _t_mark("Stage 8 \u00b7 entity-map seed", _t_seed)
                                    except Exception as _ese:
                                        print(f"[ENTITY_MAP] seed skipped: {_ese}")
                                if S.demo:
                                    result = promote_demo(
                                        df, S.col_mapping,
                                        S.val_report,
                                        S.target_table, S.stg_table,
                                    )
                                elif _do_merge:
                                    result = promote_merge(
                                        S.engine, S.stg_table, S.target_table,
                                        S.col_mapping,
                                        pk_cols=_merge_pk_cols,
                                        schema=getattr(S, "stg_schema", "stg"),
                                    )
                                else:
                                    result = promote_server(
                                        S.engine, S.stg_table, S.target_table,
                                        S.col_mapping,
                                        schema=getattr(S, "stg_schema", "stg"),
                                    )
                                _t_mark(f"Stage 8 · promote ({'merge' if _do_merge else 'demo' if S.demo else 'insert'})", _t_prom)
                        except Exception as _exc:
                            if _report_error:
                                _report_error(_exc, "Stage 8 · Promote")
                                st.rerun()
                            else:
                                raise
                        if result.ok:
                            S.promoted = True
                            S.promote_result = result
                            # Store hash on result for success screen
                            result._data_hash   = _data_hash   if "_data_hash"   in dir() else None
                            result._source_file = _source_file if "_source_file" in dir() else None
                            # Populate H3 cells for the rows just promoted so the
                            # density-grid views (GROUP BY h3_rN) include them.
                            # Guarded three ways so it can't touch other projects:
                            #   • _H3_AUTOFILL master switch (set False to disable)
                            #   • has_coord_columns() — only well tables with
                            #     surface_latitude/longitude are ever altered
                            #   • only NULL rows are computed (cheap per load)
                            if _H3_AUTOFILL:
                                try:
                                    from dataview.mapping.h3_grids import (
                                        ensure_h3_columns, backfill_h3,
                                        has_coord_columns)
                                    _t_h3 = _t_now()
                                    _tt = S.target_table or "dv_well"
                                    if "." in _tt:
                                        _h3_sch, _h3_tbl = _tt.split(".", 1)
                                    else:
                                        _h3_sch, _h3_tbl = "dataview", _tt
                                    if not has_coord_columns(
                                            S.engine, schema=_h3_sch, table=_h3_tbl):
                                        print(f"[H3] {_h3_sch}.{_h3_tbl} has no "
                                              f"surface_lat/lon — skipping (not a "
                                              f"well table)")
                                    else:
                                        _added = ensure_h3_columns(
                                            S.engine, schema=_h3_sch, table=_h3_tbl)
                                        if _added:
                                            print(f"[H3] added: {_added}")
                                        _h3 = backfill_h3(
                                            S.engine, schema=_h3_sch, table=_h3_tbl,
                                            only_missing=True)
                                        print(f"[H3] populated {_h3['updated']} / "
                                              f"{_h3['candidates']} wells")
                                        _t_mark("Stage 8 · H3 populate", _t_h3)
                                except Exception as _h3e:
                                    print(f"[H3] populate skipped: {_h3e}")
                            st.rerun()
                        else:
                            st.error(result.message)
                with c2:
                    if st.button("← Back to Validate", use_container_width=True):
                        back()

        else:
            # ── Success screen ────────────────────────────────────────────
            r = S.get("promote_result")
            st.balloons()
            st.success(f"✅ {r.message}" if r else "✅ Promote complete!")
            if r:
                mrow([
                    ("Rows inserted", r.rows_inserted, "#56d364"),
                    ("Rows skipped",  r.rows_skipped,  "#f0883e"),
                    ("Rows errored",  r.rows_error,    "#f85149"),
                    ("Timestamp",     r.timestamp[:16].replace("T", " "), "#8b949e"),
                ])
                _h = getattr(r, "_data_hash", None)
                _fn = getattr(r, "_source_file", None)
                if _h:
                    st.caption(
                        f"📁 **{_fn}** · "
                        f"SHA-256: `{_h[:16]}…{_h[-8:]}`"
                    )
                with st.expander("View executed SQL"):
                    st.code(r.sql_executed, language="sql")

                # ── Data reconciliation table ──────────────────────────────
                with st.expander("📊 Load Reconciliation", expanded=False):
                    try:
                        import pandas as _rpd
                        from sqlalchemy import text as _rt
                        _r_rows = []
                        _stg_tbl = getattr(S, 'stg_table', '')
                        _stg_sch = getattr(S, 'stg_schema', 'stg')
                        _mp = {
                            m.source_col: m.ppdm_col
                            for m in (S.col_mapping.mapped if S.col_mapping else [])
                            if m.source_col and not getattr(m, "auto_generated", False)
                        }
                        if S.engine and _mp and _stg_tbl:
                            # Source counts — query staging table server-side
                            _src_count_sql = ", ".join(
                                f"COUNT([{c}]) AS [{c}]" for c in _mp.keys()
                            )
                            # Target counts — query target table
                            _ppdm_cols   = list(_mp.values())
                            _tgt_count_sql = ", ".join(
                                f"COUNT([{c}]) AS [{c}]" for c in _ppdm_cols
                            )
                            with S.engine.connect() as _rc:
                                _src_row = _rc.execute(_rt(
                                    f"SELECT {_src_count_sql} "
                                    f"FROM [{_stg_sch}].[{_stg_tbl}]"
                                )).fetchone()
                                _src_counts = dict(zip(_mp.keys(), _src_row)) if _src_row else {}
                                _tgt_row = _rc.execute(_rt(
                                    f"SELECT {_tgt_count_sql} FROM [dataview].[{S.target_table}]"
                                )).fetchone()
                                _tgt_counts = dict(zip(_ppdm_cols, _tgt_row)) if _tgt_row else {}

                            for _src_col, _ppdm_col in sorted(_mp.items()):
                                _src_nn = int(_src_counts.get(_src_col, 0))
                                _tgt_nn = int(_tgt_counts.get(_ppdm_col, 0))
                                _r_rows.append({
                                    "Source Column": _src_col,
                                    "Source Count":  _src_nn,
                                    "Target Column":   _ppdm_col,
                                    "Loaded Count":  _tgt_nn,
                                    "Diff":          _src_nn - _tgt_nn,
                                })
                            if _r_rows:
                                _rdf = _rpd.DataFrame(_r_rows)
                                st.dataframe(
                                    _rdf,
                                    use_container_width=True,
                                    hide_index=True,
                                    column_config={
                                        "Source Column": st.column_config.TextColumn(width="medium"),
                                        "Source Count":  st.column_config.NumberColumn(width="small"),
                                        "Target Column":   st.column_config.TextColumn(width="medium"),
                                        "Loaded Count":  st.column_config.NumberColumn(width="small"),
                                        "Diff":          st.column_config.NumberColumn(width="small"),
                                    }
                                )
                        else:
                            st.caption("Connect to database and complete pipeline to see reconciliation.")
                    except Exception as _re:
                        st.caption(f"Reconciliation unavailable: {_re}")

                # Bad rows download
                _bad_f = getattr(r, 'bad_rows_file', '')
                if _bad_f and _os.path.exists(_bad_f):
                    with open(_bad_f, 'rb') as _brf:
                        st.download_button(
                            f"⚠️ Download {r.rows_error} bad row(s)",
                            data=_brf.read(),
                            file_name=_os.path.basename(_bad_f),
                            mime="text/csv",
                            key="dl_bad_rows_promote",
                        )

            col1, col2 = st.columns(2)
            with col1:
                if st.button("🔄 Load Another Dataset", use_container_width=True):
                    reset()
            with col2:
                if not S.demo:
                    st.markdown(f"**Target table:** `dataview.{S.target_table}`")

    # ── Per-render stage timing (runs after the active stage block) ──────
    try:
        _lbl = (_STAGE_LABELS_T[S.stage]
                if 0 <= S.stage < len(_STAGE_LABELS_T) else f"Stage {S.stage}")
        _t_mark(f"{_lbl} · full render", _render_t0)
    except Exception:
        pass
    _render_timing_sidebar()

