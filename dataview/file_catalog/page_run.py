"""
page_run.py — launch the headless pipeline as a separate process and live-tail
its log in the window. The log refreshes inside an st.fragment, so ONLY the log
area updates on each poll — the rest of the page doesn't re-render (no flashing).

Wire into the app with page_run.render(engine)  (aliases main/show/app).
Or run standalone:  streamlit run page_run.py
"""
import os, sys, time, subprocess
import streamlit as st

SERVER      = r"localhost\SQLEXPRESS"
DATABASE    = "DataView_Demo"
REPORT_ROOT = r"C:\Bulk\reports"
REF         = "WELL_REF.well_ref.well_master_public_v2"   # enrich/triage reference master
CONSOLE_LOG = os.path.join(REPORT_ROOT, "_run_console.log")
DEFAULT_ROOT = (r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai"
                r"\data_wrangler\training\test_crawl")
CREATE_NO_WINDOW = 0x08000000     # no console window (Windows)
_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))  # dir containing dataview/


def _read(path=CONSOLE_LOG, tail=16000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()[-tail:]
    except Exception:
        return ""


def _demo_engine():
    """Engine for the demo DB the runs target — so reset and run hit the same
    database regardless of what the app is connected to."""
    from sqlalchemy import create_engine
    import urllib.parse
    odbc = urllib.parse.quote_plus(
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={SERVER};"
        f"DATABASE={DATABASE};Trusted_Connection=yes;Encrypt=no")
    return create_engine(f"mssql+pyodbc:///?odbc_connect={odbc}", fast_executemany=True)


def _stage_summary(text):
    rows = []
    for ln in text.splitlines():
        if "] \u2713 done in" in ln or "] done in" in ln:   # [stage] ✓ done in Xs
            try:
                stage = ln.split("[", 1)[1].split("]", 1)[0]
                secs = ln.split("done in", 1)[1].strip().rstrip("s")
                rows.append((stage, float(secs)))
            except Exception:
                pass
    return rows




def _tail():
    """Tail the log without fragments (they orphan a refresh timer on any button-
    triggered full rerun, and scope='fragment' is invalid on first run). Two
    st.empty() placeholders are updated IN PLACE in a loop, so only the status
    line and log area change — no flashing, no stale-fragment errors. The loop
    blocks until the run exits; the run is a detached process, so it keeps going
    regardless (mid-run Stop takes effect once the loop ends)."""
    proc = st.session_state.get("_run_proc")
    if proc is None:
        st.info("Idle. Set the root and press Run.")
        return
    status_ph = st.empty()
    log_ph = st.empty()
    while True:
        done = proc.poll() is not None
        text = _read()
        elapsed = time.time() - st.session_state.get("_run_started", time.time())
        if done:
            rc = proc.returncode
            (status_ph.success if rc == 0 else status_ph.error)(
                f"finished in {elapsed:.0f}s (exit {rc})")
        else:
            status_ph.warning(f"running… {elapsed:.0f}s elapsed")
        log_ph.code(text or "(starting…)")
        if done:
            rows = _stage_summary(text)
            if rows:
                st.table({"stage": [r[0] for r in rows],
                          "seconds": [r[1] for r in rows]})
            break
        time.sleep(1.5)


def render(engine=None):
    st.title("▶ Run Pipeline (headless)")
    st.caption("Runs pipeline_run.py in a separate process and tails its log "
               "live — fresh process, always the code on disk.")

    proc = st.session_state.get("_run_proc")
    running = proc is not None and proc.poll() is None

    c1, c2, c3 = st.columns([4, 1.3, 1.3])
    root = c1.text_input("Crawl root", DEFAULT_ROOT, disabled=running)
    workers = c2.number_input("Workers", 1, 32, 6, disabled=running)
    apply = c3.checkbox("Apply", value=True, disabled=running)

    ALL_EXTS = [".las", ".dlis", ".lis", ".segy", ".sgy", ".p190", ".pdf",
                ".shp", ".xlsx", ".docx", ".xml", ".json", ".txt", ".witsml"]
    sel_exts = st.multiselect(
        "Extensions to process (leave empty = all)", ALL_EXTS, default=[],
        disabled=running)

    b1, b2 = st.columns(2)
    if b1.button("▶ Run", type="primary", disabled=running,
                 use_container_width=True):
        cmd = [sys.executable, "-u", "-m", "dataview.import_data.pipeline_run",
               "--root", root, "--server", SERVER, "--database", DATABASE,
               "--workers", str(int(workers)), "--parse-mode", "process",
               "--no-vault", "--report-root", REPORT_ROOT,
               "--ref", REF, "--promote"]
        if sel_exts:
            cmd += ["--exts", ",".join(sel_exts)]
        if apply:
            cmd.append("--promote-apply")
        os.makedirs(REPORT_ROOT, exist_ok=True)
        fh = open(CONSOLE_LOG, "w", encoding="utf-8")
        _env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        st.session_state["_run_proc"] = subprocess.Popen(
            cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=_REPO_ROOT,
            creationflags=CREATE_NO_WINDOW, env=_env)
        st.session_state["_run_started"] = time.time()
        st.rerun()

    if b2.button("■ Stop", disabled=not running, use_container_width=True):
        try:
            proc.terminate()
        except Exception:
            pass
        st.rerun()

    _tail()          # auto-refreshing log fragment (only this area updates)

    # ── Reset demo data (cold-start for testing) ─────────────────────────────
    st.divider()
    with st.expander("🗑️ Reset demo data (cold start)", expanded=False):
        st.caption(f"Wipes the catalog + captured/promoted rows in **{DATABASE}** "
                   "so the next run is a full cold run. Reference / spatial / "
                   "bulk-loaded data is left intact.")
        try:
            from dataview.core.demo_reset import RESET_VERSION as _rv
            st.caption(f"reset engine: {_rv}")
        except Exception:
            pass

        if not st.session_state.get("_confirm_reset"):
            if st.button("Reset demo data", disabled=running, key="reset_open"):
                st.session_state["_confirm_reset"] = True
                st.rerun()
        else:
            st.warning("This wipes the catalog and catalog-derived dv_* rows. "
                       "Confirm?")
            r1, r2 = st.columns(2)
            if r1.button("✓ Confirm reset", type="primary", key="reset_go"):
                try:
                    from dataview.core.demo_reset import reset_demo_data
                    st.session_state["_reset_result"] = reset_demo_data(_demo_engine())
                except Exception as e:
                    st.session_state["_reset_result"] = {"error": str(e)}
                st.session_state["_confirm_reset"] = False
                st.rerun()
            if r2.button("✗ Cancel", key="reset_cancel"):
                st.session_state["_confirm_reset"] = False
                st.rerun()

        res = st.session_state.get("_reset_result")
        if res:
            if res.get("error"):
                st.error(f"Reset failed: {res['error']}")
            else:
                st.success("Reset done — " +
                           ", ".join(f"{k}: {v}" for k, v in res.items()))


main = show = app = render

if __name__ == "__main__":
    render()
