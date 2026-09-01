"""
page_pipeline_tools.py — stage test harness.

Run any single stage on its own against the catalog already in the DB, with
timing + log inline. Uses the SAME pipeline_run._stage_* functions as a full run,
so nothing drifts. Built to kill the deploy-then-full-rerun loop: tweak a stage,
click Run, read the timing — seconds instead of a 90s pipeline.

Wire into the router with:  page_pipeline_tools.render()   (aliases main/show/app).
"""
import os
import time
import streamlit as st
from dataview.import_data import pipeline_run as pr

SCHEMA      = "file_catalog"
DEFAULT_REF = "WELL_REF.well_ref.well_master_gold"

# timing/marker lines worth surfacing above the full log
TAGS = ("[vault-fetch]", "[vault-phase]", "[vault-wait]",
        "[promote-parts]", "[promote-phase]", "[promote-steps-all]",
        "-- promote timing", "[capture-phase]",
        "[TIME ]", "pass 1", "pass 2", "reflect schema", "[enrich]")


def _get_engine():
    for k in ("engine", "eng", "sql_engine", "conn_engine", "db_engine"):
        e = st.session_state.get(k)
        if e is not None and hasattr(e, "connect"):
            return e, f"session_state[{k!r}]"
    server = st.session_state.get("server") or r"localhost\SQLEXPRESS"
    database = st.session_state.get("database") or "DataView_Demo"
    return pr._engine(server, database), f"{server} / {database}"


def _run(label, fn):
    """Run fn(log)->stats, capturing log lines + wall time, and render results."""
    logs = []
    with st.spinner(f"Running {label}…"):
        t0 = time.monotonic()
        try:
            stats = fn(logs.append)
        except Exception as e:
            st.error(f"{label} failed: {e}")
            st.code("\n".join(map(str, logs)) or "(no output)")
            return
        dt = time.monotonic() - t0

    st.success(f"{label} done in {dt:.1f}s")
    if isinstance(stats, dict) and stats:
        items = [(k, v) for k, v in stats.items()
                 if isinstance(v, (int, float, str)) and k != "errors"]
        if items:
            cols = st.columns(min(4, len(items)))
            for col, (k, v) in zip(cols, items[:4]):
                col.metric(k, f"{v:,}" if isinstance(v, int) else v)

    timing = [str(l) for l in logs if any(t in str(l) for t in TAGS)]
    if timing:
        st.subheader("Timing")
        st.code("\n".join(timing))
    with st.expander("Full log", expanded=False):
        st.code("\n".join(map(str, logs)) or "(no output)")


def render():
    st.title("🧰 Pipeline stage tools")
    st.caption("Run one stage at a time against the current catalog — fast "
               "iteration, no full pipeline run.")
    try:
        eng, src = _get_engine()
    except Exception as e:
        st.error(f"Could not get a DB connection: {e}")
        return
    st.caption(f"Connection: {src}  ·  schema: {SCHEMA}")

    t_enrich, t_capture, t_vault, t_promote = st.tabs(
        ["Enrich", "Capture", "Vault", "Promote"])

    with t_enrich:
        ref = st.text_input("Reference master", DEFAULT_REF, key="en_ref")
        apply = st.checkbox("Apply (write resolved UWIs / attrs)", key="en_apply")
        if st.button("▶ Run enrich", key="en_btn", type="primary"):
            _run("enrich", lambda log: pr._stage_enrich(eng, ref, apply, log))

    with t_capture:
        c1, c2 = st.columns(2)
        workers = c1.number_input("Workers", 1, 32, 6, key="cap_workers")
        parallel = c2.checkbox(
            "Multi-core (ProcessPool)", value=False, key="cap_par")
        exts = st.text_input("Limit to extensions (blank = all)", "", key="cap_exts")
        if st.button("▶ Run capture", key="cap_btn", type="primary"):
            _exts = [e.strip() for e in exts.split(",") if e.strip()] or None
            _run("capture", lambda log: pr._stage_capture(
                eng, "mssql", log, exts=_exts, workers=int(workers),
                parallel=parallel))

    with t_vault:
        v1, v2, v3 = st.columns(3)
        vault_root = v1.text_input("Vault root", r"C:\Bulk\Vault", key="v_root")
        mode = v2.selectbox("Mode", ["copy", "hardlink"], key="v_mode")
        vw = v3.number_input("Copy workers", 1, 32, 8, key="v_workers")
        apply = st.checkbox("Apply (place + stamp)", key="v_apply")
        if st.button("▶ Run vault", key="v_btn", type="primary"):
            os.environ["VAULT_COPY_WORKERS"] = str(int(vw))
            _run("vault", lambda log: pr._stage_vault(
                eng, SCHEMA, vault_root, mode, apply, log))

    with t_promote:
        apply = st.checkbox("Apply (lift cat_* → dv_*)", key="p_apply")
        if st.button("▶ Run promote", key="p_btn", type="primary"):
            _run("promote", lambda log: pr._stage_promote(eng, apply, log))


main = show = app = render

if __name__ == "__main__":
    render()
