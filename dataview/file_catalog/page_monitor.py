"""
page_monitor.py — Pipeline Monitor / Orchestrator
==================================================
A thin control room for the parallel pipeline. It does NOT do the work itself —
it launches the crawler and the worker pool as background SUBPROCESSES (each owns
its own process tree; the pool uses a ProcessPoolExecutor that can't live inside
a Streamlit thread), then polls work_queue.progress() to render live state.

Flow mirrors the pipeline: Crawl → Queue → Process → Promote.

Wiring (in app_v3.py), same shape as the other pages:
    elif S.app_mode == "monitor":
        from dataview.file_catalog import page_monitor
        page_monitor.run(S.engine, _dialect)
"""
from __future__ import annotations
import os
import sys
import time
import subprocess
import streamlit as st

from dataview.file_catalog import work_queue as wq

# directory of this file = where worker_pool.py / parallel_crawl.py live
_HERE = os.path.dirname(os.path.abspath(__file__))


# ── helpers ──────────────────────────────────────────────────────────────────
def _server_db(engine):
    """Server + database come from the sidebar connection the user already set
    up (session_state keys sb_server / sb_database[_sel]) — the same connection
    this page's engine is bound to. The subprocess then targets that exact DB."""
    ss = st.session_state
    server = ss.get("sb_server") or None
    # database may be the selectbox (sb_database_sel) or the text input fallback
    database = ss.get("sb_database_sel") or ss.get("sb_database") or None
    return server, database


def _proc_alive(p):
    return p is not None and p.poll() is None


def _spawn(args, logfile):
    """Launch a detached python subprocess, tee output to a logfile we can tail."""
    f = open(logfile, "w", encoding="utf-8", errors="replace")
    creationflags = 0
    if os.name == "nt":
        # new process group so it survives a Streamlit reload and we can signal it
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen(
        [sys.executable, "-u", *args],
        cwd=_HERE, stdout=f, stderr=subprocess.STDOUT,
        creationflags=creationflags)
    return p, f


def _tail(path, n=18):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-n:])
    except Exception:
        return ""


def _final_line(path, contains):
    """Return the last log line containing `contains` (e.g. the pool's DONE
    summary), stripped — or None."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            hits = [ln.strip() for ln in fh if contains in ln]
        return hits[-1] if hits else None
    except Exception:
        return None


def _promoted_count(engine):
    """Cheap count of wells lifted into the gold layer (dv_well) — the
    'promoted' scorecard number. Returns 0 if the table isn't there yet."""
    from sqlalchemy import text as _t
    try:
        with engine.connect() as con:
            return con.execute(_t(
                "SELECT COUNT(*) FROM dataview.dv_well")).scalar() or 0
    except Exception:
        return 0




# ── page ─────────────────────────────────────────────────────────────────────
def _render_stage_timeline(ts):
    """Show when each stage started and the elapsed time. ts maps
    stage -> {'start': epoch, 'end': epoch}."""
    if not ts:
        return
    import time as _time
    from datetime import datetime as _dt
    order = ["crawl", "pool", "vault", "promote"]
    present = [s for s in order if s in ts]
    if not present:
        return
    rows = []
    _starts = [ts[s]["start"] for s in present if ts[s].get("start")]
    for s in present:
        d = ts[s]
        start = d.get("start"); end = d.get("end")
        when = _dt.fromtimestamp(start).strftime("%H:%M:%S") if start else "—"
        if start and end:
            secs = f"{end - start:.0f}s"
        elif start:
            secs = f"{_time.time() - start:.0f}s (running)"
        else:
            secs = "—"
        rows.append({"stage": s, "started": when, "elapsed": secs})
    st.markdown("**Run timeline**")
    st.dataframe(rows, hide_index=True, use_container_width=True)
    # total elapsed across the whole chain
    if _starts:
        _first = min(_starts)
        _last_end = max((ts[s].get("end") or _time.time()) for s in present)
        _all_done = all(ts[s].get("end") for s in present)
        _label = "Total elapsed" if _all_done else "Elapsed so far"
        st.markdown(
            f"<div style='font-size:1.1rem;font-weight:700;color:#fff;"
            f"margin-top:4px'>{_label}: "
            f"{_last_end - _first:.0f}s</div>",
            unsafe_allow_html=True)


def _crawl_args(ss, engine):
    server, database = _server_db(engine)
    root = (ss.get("mon_root") or "").strip()
    return ["parallel_crawl.py", "--root", root, "--server", server,
            "--database", database, "--walkers", "12", "--batch", "2000"]


def _pool_args(ss, engine):
    server, database = _server_db(engine)
    workers = int(ss.get("mon_workers", 10))
    batch = int(ss.get("mon_batch", 500))
    return ["worker_pool.py", "--server", server, "--database", database,
            "--workers", str(workers), "--batch", str(batch)]


def run(engine=None, dialect: str = "mssql", embedded: bool = False):
    st.markdown(_CSS, unsafe_allow_html=True)

    server, database = _server_db(engine)
    ss = st.session_state
    ss.setdefault("mon_crawl_proc", None)
    ss.setdefault("mon_pool_proc", None)
    ss.setdefault("mon_crawl_log", os.path.join(_HERE, "_mon_crawl.log"))
    ss.setdefault("mon_pool_log", os.path.join(_HERE, "_mon_pool.log"))

    if embedded:
        st.markdown("#### 🚄 Fast Track for Large Datasets")
        st.caption(
            f"Crawl → process (multi-core) → promote, with one-click Run-all. "
            f"·  {server or '?'} / {database or '?'}")
    else:
        st.title("📁 File Catalog")
        st.caption(
            f"Inventory · Catalog · Promote "
            f"·  {server or '?'} / {database or '?'}")

    pool_running = _proc_alive(ss.mon_pool_proc)
    crawl_running = _proc_alive(ss.mon_crawl_proc)

    # ── Stage-transition detection (runs EARLY, before any auto-rerun) ───────
    # Records a timestamp when each stage starts/ends and drives the run-all
    # auto-advance. This must happen before the pool-running block's auto-rerun,
    # which would otherwise restart the page before we notice the pool finished.
    import time as _time
    _now = _time.time()
    ss.setdefault("mon_ts", {})           # stage -> {"start":..,"end":..}

    # crawl edge (timestamps)
    if crawl_running and not ss.get("mon_crawl_prev"):
        ss["mon_ts"]["crawl"] = {"start": _now}
    if not crawl_running and ss.get("mon_crawl_prev"):
        ss["mon_ts"].setdefault("crawl", {})["end"] = _now
    ss["mon_crawl_prev"] = crawl_running

    # run-all: crawl → pool. NOT tied to the running→stopped edge, because a
    # fast crawl ("done in 1s") can finish before the page ever renders it as
    # running — then the edge never fires and run-all stalls at "click Start
    # pool". Instead: if run-all is on, crawl isn't running, and we haven't
    # launched the pool yet, launch it (once) when there's something queued.
    if (ss.get("mon_runall") and not crawl_running and not pool_running
            and not ss.get("mon_runall_pool_started")):
        try:
            _p = wq.progress(engine)
            _q = _p.get("pending", 0) + _p.get("claimed", 0)
        except Exception:
            _q = 1
        if _q > 0:
            ss["mon_ts"].setdefault("crawl", {}).setdefault("end", _now)
            ss.mon_pool_proc, _ = _spawn(_pool_args(ss, engine),
                                         ss.mon_pool_log)
            ss["mon_runall_pool_started"] = True
            pool_running = True
            st.rerun()
        else:
            # crawl produced nothing to do — end the chain cleanly
            ss["mon_runall"] = False

    # pool edge
    if pool_running and not ss.get("mon_pool_prev"):
        ss["mon_ts"]["pool"] = {"start": _now}
    if not pool_running and ss.get("mon_pool_prev"):
        ss["mon_ts"].setdefault("pool", {})["end"] = _now
        # run-all: pool → promote (apply), once
        if ss.get("mon_runall") and not ss.get("mon_runall_promoted"):
            # optional vault stage, between process and promote
            if ss.get("mon_vault_on") and not ss.get("mon_runall_vaulted"):
                ss["mon_runall_vaulted"] = True
                ss["mon_ts"]["vault"] = {"start": _time.time()}
                with st.spinner("Run-all: vaulting catalogued files…"):
                    _run_vault(engine, ss.get("mon_vault_root", ""), apply=True)
                ss["mon_ts"].setdefault("vault", {})["end"] = _time.time()
            ss["mon_runall_promoted"] = True
            ss["mon_ts"]["promote"] = {"start": _time.time()}
            with st.spinner("Run-all: promoting cat_* → dv_*…"):
                _run_promote(engine, apply=True)
            ss["mon_ts"].setdefault("promote", {})["end"] = _time.time()
            ss["mon_runall"] = False
            ss["mon_promote_out"] = (ss.get("mon_promote_out", "")
                                     + "\n[run-all] promote complete.")
    ss["mon_pool_prev"] = pool_running

    # render the stage timeline if we have any timestamps
    _render_stage_timeline(ss.get("mon_ts", {}))


    # While the pool/crawl runs, do NOT touch the DB from this page. Any query
    # here (queue progress, dv_well count) competes with the pool on single-
    # machine SQL Express and slows the run ~4x (403s vs 90s headless). So when
    # busy we render controls only and read nothing; the user clicks Refresh to
    # pull a one-shot snapshot on demand. When idle, read freely.
    # While the POOL runs, do NOT touch the DB from this page — any query here
    # competes with the pool on single-machine SQL Express and slows the run ~4x
    # (403s vs 90s headless). So during a pool run we render controls only and
    # read nothing; the user clicks Refresh for a one-shot snapshot. The CRAWL is
    # short and not contended, so it reads normally and auto-refreshes below.
    skip_db = pool_running and not ss.pop("mon_force_read", False)
    if skip_db:
        total = done = pend = claimed = err = 0
        have_counts = False
    else:
        try:
            p = wq.progress(engine)
        except Exception as e:
            st.error(f"Can't read the queue — is the catalog set up here? ({e})")
            return
        total = p.get("total", 0) or 0
        done = p.get("done", 0); pend = p.get("pending", 0)
        claimed = p.get("claimed", 0); err = p.get("error", 0)
        have_counts = True
        ss["mon_last_counts"] = (total, done, pend, claimed, err)

    if pool_running:
        st.markdown("### 🟢 Pool is RUNNING")
        # surface the pool's own latest progress line right here, so you can see
        # what it's doing without scrolling — e.g. "1,223/1,748 done · ~13/s".
        _last = _final_line(ss.mon_pool_log, "/") or \
                _final_line(ss.mon_pool_log, "pool") or "starting…"
        st.markdown(f"**Latest:** `{_last[:160]}`")
        if have_counts:
            _tot = max(total, 1)
            st.progress(min(done / _tot, 1.0))
            st.caption(f"{done:,} done · {pend:,} pending · {claimed:,} processing "
                       f"· {err:,} error   —   snapshot at refresh")
        _auto = st.checkbox("Auto-update (tails the log every 2s)",
                            value=True, key="mon_pool_auto")
        st.caption("Auto-update only reads the pool's log file — it does NOT "
                   "query the database, so it won't slow the pool. Use "
                   "“Refresh progress” for exact DB counts.")
        if st.button("↻ Refresh progress", type="primary",
                     use_container_width=True):
            ss["mon_force_read"] = True
            st.rerun()
        if _auto or ss.get("mon_runall"):
            # log-only auto-advance: re-read the cheap log file, never the DB.
            # During run-all this MUST keep firing so we notice when the pool
            # ends and can auto-trigger promote.
            import time as _t
            _t.sleep(2.0)
            st.rerun()

    # ── Vault toggle — shown WITH Run-all so it's decided before you click ───
    vt1, vt2 = st.columns([1, 3])
    mon_vault_on = vt1.checkbox(
        "Vault after processing", key="mon_vault_on",
        value=ss.get("mon_vault_on", False))
    vt2.text_input("Vault root", key="mon_vault_root",
                   value=ss.get("mon_vault_root", r"C:\Bulk\Vault"),
                   label_visibility="collapsed", placeholder=r"C:\Bulk\Vault",
                   disabled=not mon_vault_on)

    # ── RUN ALL (auto-advance crawl → pool → promote) ───────────────────────
    _busy = pool_running or crawl_running
    if ss.get("mon_runall"):
        st.markdown("### ▶️ Run-all is ON — auto-advancing crawl → pool → promote")
        st.caption("Vault: " + ("ON — runs after processing, before promote"
                                if ss.get("mon_vault_on") else "off (toggle above)"))
        if st.button("⏹ Cancel run-all", use_container_width=True):
            ss["mon_runall"] = False
            st.rerun()
    elif not _busy:
        if st.button("▶️ Run all (crawl → pool → promote)", type="primary",
                     use_container_width=True):
            _root = (ss.get("mon_root") or "").strip()
            if not _root:
                st.warning("Set the folder to crawl below first.")
            else:
                ss["mon_runall"] = True
                ss["mon_runall_promoted"] = False
                ss["mon_runall_vaulted"] = False
                ss["mon_runall_pool_started"] = False
                ss["mon_ts"] = {}          # fresh timeline for this run
                ss["mon_crawl_prev"] = False
                ss["mon_pool_prev"] = False
                # kick off the crawl now; the auto-advance does the rest
                args = _crawl_args(ss, engine)
                ss.mon_crawl_proc, _ = _spawn(args, ss.mon_crawl_log)
                st.rerun()


    c1, c2 = st.columns([3, 1])
    root = c1.text_input("Folder to crawl", key="mon_root",
                         value=ss.get("mon_root", r"C:\Bulk"),
                         label_visibility="collapsed",
                         placeholder=r"C:\Bulk")
    if c2.button("Crawl", disabled=crawl_running or pool_running,
                 use_container_width=True):
        if not (server and database and root):
            st.warning("Need a server, database, and folder.")
        else:
            args = ["parallel_crawl.py", "--root", root, "--server", server,
                    "--database", database, "--walkers", "12", "--batch", "2000"]
            ss.mon_crawl_proc, _ = _spawn(args, ss.mon_crawl_log)
            st.rerun()
    if crawl_running:
        st.markdown("<div class='mon-run'>Crawling…</div>", unsafe_allow_html=True)
        st.code(_tail(ss.mon_crawl_log), language="text")
        ss["mon_crawl_was_running"] = True
        # The crawl is short (seconds) and writes in bulk, so it is NOT the
        # contention problem the pool is — auto-refresh it so the UI notices when
        # it finishes and advances. (The POOL is the one we never auto-poll.)
        time.sleep(2.0)
        st.rerun()
    elif ss.get("mon_crawl_was_running"):
        # crawl just finished — confirm and hand off to Process
        ss["mon_crawl_was_running"] = False
        ss["mon_crawl_done_msg"] = _final_line(ss.mon_crawl_log, "done") or \
            "Crawl finished."

    if ss.get("mon_crawl_done_msg") and not crawl_running:
        # pend/done at the top may be suppressed (0) when the pool is running, so
        # read the queue fresh here for an accurate message. Also parse the
        # crawl log's "N newly queued" so we never say "nothing queued" when it
        # plainly queued files.
        try:
            _p = wq.progress(engine)
            _pend = _p.get("pending", 0); _done = _p.get("done", 0)
            _claimed = _p.get("claimed", 0)
        except Exception:
            _pend = pend; _done = done; _claimed = claimed
        import re as _re
        _m = _re.search(r"([\d,]+)\s+newly queued", ss["mon_crawl_done_msg"] or "")
        _queued = int(_m.group(1).replace(",", "")) if _m else None
        if (_pend + _claimed) > 0 or (_queued and _queued > 0):
            _n = _pend + _claimed if (_pend + _claimed) > 0 else _queued
            st.success(f"Crawl complete — {_n:,} files queued. "
                       f"Set workers below and click Start pool "
                       f"(or it may already be running).")
        elif _done > 0:
            st.info(f"Crawl complete — no new files (all {_done:,} are already "
                    f"cataloged). To reprocess, use Reset queue below; or skip "
                    f"to Promote if you just want to lift them.")
        else:
            st.warning("Crawl complete — but nothing is queued. Check the folder "
                       "path has files this catalog can handle.")
        st.caption(ss["mon_crawl_done_msg"])
        if st.button("Dismiss", key="mon_dismiss_crawl"):
            ss["mon_crawl_done_msg"] = None
            st.rerun()

    # ── PROCESS ──
    st.markdown("<div class='mon-section'>2 · PROCESS</div>", unsafe_allow_html=True)
    w1, w2, w3 = st.columns([1, 1, 1])
    workers = w1.number_input("Workers", 1, 40, ss.get("mon_workers", 10),
                              key="mon_workers")
    batch = w2.number_input("Batch", 50, 5000, ss.get("mon_batch", 500),
                            step=50, key="mon_batch")
    if not pool_running:
        if w3.button("Start pool", type="primary",
                     disabled=crawl_running or pend + claimed == 0,
                     use_container_width=True):
            args = ["worker_pool.py", "--server", server, "--database", database,
                    "--workers", str(int(workers)), "--batch", str(int(batch))]
            ss.mon_pool_proc, _ = _spawn(args, ss.mon_pool_log)
            st.rerun()
    else:
        if w3.button("Stop pool", use_container_width=True):
            try:
                ss.mon_pool_proc.terminate()
            except Exception:
                pass
            ss.mon_pool_proc = None
            st.rerun()

    # Reset = flip done rows back to pending for a full reprocess. Guarded, since
    # the crawl is idempotent (won't re-queue already-cataloged files) so this is
    # the only way to re-run the pool over files that are already 'done'.
    if not pool_running and not crawl_running and done > 0:
        if not ss.get("mon_confirm_reset"):
            if st.button(f"Reset queue ({done:,} done → pending)",
                         use_container_width=True):
                ss["mon_confirm_reset"] = True
                st.rerun()
        else:
            st.warning(f"Re-queue all {done:,} done files for a full reprocess?")
            r1, r2 = st.columns(2)
            if r1.button("Confirm reset", type="primary",
                         use_container_width=True):
                wq.reset_queue(engine, only_claimed=False)
                ss["mon_confirm_reset"] = False
                st.rerun()
            if r2.button("Cancel", use_container_width=True):
                ss["mon_confirm_reset"] = False
                st.rerun()

    if pool_running:
        st.markdown("<div class='mon-run'>Pool running…</div>",
                    unsafe_allow_html=True)
        st.code(_tail(ss.mon_pool_log), language="text")
        ss["mon_pool_was_running"] = True
    elif ss.get("mon_pool_was_running"):
        # pool just finished this render — surface a completion summary, pulled
        # from the pool's final log line ("[pool] DONE in Xs — processed N…").
        ss["mon_pool_was_running"] = False
        ss["mon_pool_done_msg"] = _final_line(ss.mon_pool_log, "DONE") or \
            "Pool finished."

    if ss.get("mon_pool_done_msg") and not pool_running:
        if err > 0:
            st.warning(f"Processing finished with {err:,} error(s). "
                       f"{ss['mon_pool_done_msg']}")
        else:
            st.success(f"Processing complete — {done:,} files done, 0 errors. "
                       f"Ready to promote.")
        st.caption(ss["mon_pool_done_msg"])
        if st.button("Dismiss", key="mon_dismiss_done"):
            ss["mon_pool_done_msg"] = None
            st.rerun()

    # ── VAULT (optional) ──
    st.markdown("<div class='mon-section'>3 · VAULT (optional)</div>",
                unsafe_allow_html=True)
    st.caption("Copy catalogued files into the governed vault and stamp "
               "VAULT_PATH so the map opens the vault copy. In Run-all this "
               "runs between process and promote when enabled.")
    mon_vault_on = bool(ss.get("mon_vault_on", False))
    _vroot = (ss.get("mon_vault_root") or "").strip()
    st.caption(f"Vault {'ON' if mon_vault_on else 'off'}"
               + (f" · root `{_vroot}`" if _vroot else "")
               + " — auto-vault toggle is up by Run-all. Manual copy below.")
    _vault_blocked = pool_running or done == 0 or not _vroot
    if done == 0 and not pool_running:
        st.info("Process some files first — vault copies what the pool has "
                "catalogued.")
    vc1, vc2 = st.columns([1, 1])
    if vc1.button("Vault dry run", use_container_width=True,
                  disabled=_vault_blocked):
        with st.spinner("Vault (dry run)…"):
            _run_vault(engine, ss.get("mon_vault_root", ""), apply=False)
        st.rerun()
    if vc2.button("Apply vault", type="primary", use_container_width=True,
                  disabled=_vault_blocked):
        with st.spinner("Vaulting catalogued files…"):
            _run_vault(engine, ss.get("mon_vault_root", ""), apply=True)
        st.rerun()
    if ss.get("mon_vault_out"):
        st.code(ss["mon_vault_out"], language="text")

    # ── PROMOTE ──
    st.markdown("<div class='mon-section'>4 · PROMOTE</div>", unsafe_allow_html=True)
    st.caption("Lift catalog rows (cat_* → dv_*) into the gold layer.")
    promote_blocked = pool_running or done == 0
    if done == 0 and not pool_running:
        st.info("Process some files first — promote lifts what the pool has "
                "marked done.")
    pc1, pc2 = st.columns([1, 1])
    if pc1.button("Dry run", use_container_width=True, disabled=promote_blocked):
        ss["mon_promote_out"] = "Starting dry run…"
        with st.spinner("Running promote (dry run)…"):
            _run_promote(engine, apply=False)
        st.rerun()
    if pc2.button("Apply promote", type="primary", use_container_width=True,
                  disabled=promote_blocked):
        ss["mon_promote_out"] = "Starting promote…"
        with st.spinner("Promoting cat_* → dv_*…"):
            _run_promote(engine, apply=True)
        st.rerun()
    if ss.get("mon_promote_out"):
        st.code(ss["mon_promote_out"], language="text")


def _run_promote(engine, apply: bool):
    """Promote in-process (it's a single transaction, fine for the UI thread)."""
    import io, contextlib
    try:
        from dataview.file_catalog import promote_catalog as pc
    except Exception as e:
        st.session_state["mon_promote_out"] = f"promote unavailable: {e}"
        return
    buf = io.StringIO()
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        with contextlib.redirect_stdout(buf):
            pc.run_promote(cur, None, apply, log=lambda m: print(m))
            # Backfill dv_well NULLs (incl. surface lat/long) from
            # well_master_public_v2 — the same enrichment pipeline_run does, so
            # promoted wells get coordinates and can plot on the map. Unscoped
            # sweep (fills all NULLs); fine for the Monitor's batch sizes.
            if apply:
                try:
                    pc.enrich_from_gold(cur, log=lambda m: print(m))
                except Exception as _ee:
                    print(f"[enrich] skipped: {str(_ee)[:200]}")
        if apply:
            raw.commit()
            buf.write("\n[promote] committed (with gold enrichment).")
        else:
            raw.rollback()
            buf.write("\n[dry-run] nothing written.")
    except Exception as e:
        buf.write(f"\n[promote] FAILED: {e}")
        try:
            raw.rollback()
        except Exception:
            pass
    finally:
        raw.close()
    st.session_state["mon_promote_out"] = buf.getvalue()


def _run_vault(engine, vault_root, apply: bool):
    """Vault in-process: copy catalogued files into the governed vault and stamp
    VAULT_PATH / VAULTED_AT. Reuses pipeline_run._stage_vault — the same stage
    the full pipeline runs — so the map viewer resolves to the vault copy."""
    import io
    import contextlib
    if not (vault_root or "").strip():
        st.session_state["mon_vault_out"] = "vault skipped: no vault root set."
        return
    try:
        from dataview.import_data import pipeline_run as pr
    except Exception as e:
        st.session_state["mon_vault_out"] = f"vault unavailable: {e}"
        return
    buf = io.StringIO()
    try:
        pr._ensure_catalog_cols(engine)          # ensure VAULT_PATH / VAULTED_AT
        with contextlib.redirect_stdout(buf):
            res = pr._stage_vault(engine, "file_catalog", vault_root.strip(),
                                  "copy", bool(apply), lambda m: print(m))
        if apply:
            buf.write(f"\n[vault] placed {res.get('vault_placed', 0):,} new · "
                      f"{res.get('vault_exists', 0):,} already vaulted · "
                      f"stamped VAULT_PATH on {res.get('vault_stamped', 0):,} "
                      f"file(s).")
        else:
            buf.write(f"\n[dry-run] planned {res.get('vault_total', 0):,} "
                      f"placement(s); nothing copied or stamped.")
    except Exception as e:
        buf.write(f"\n[vault] FAILED: {e}")
    st.session_state["mon_vault_out"] = buf.getvalue()


_CSS = """
<style>
:root { --gold:#ffd400; --accent:#e0a829; --card:#17171a; --line:#2b2b31;
  --dim:#9a9aa2; }
.mon-sub { color:var(--dim); font-size:.92rem; margin:-.4rem 0 1.1rem; }
.mon-sub code { background:#23232a; padding:1px 7px; border-radius:4px;
  font-size:.82rem; color:var(--gold); }
.mon-score { display:grid; grid-template-columns:repeat(3,1fr); gap:.7rem;
  margin:.2rem 0 1.1rem; }
.mon-score .card { background:var(--card); border:1px solid var(--line);
  border-radius:12px; padding:.85rem 1rem; }
.mon-score .num { font: 700 1.9rem/1 'Georgia',serif; color:var(--gold);
  letter-spacing:-.01em; }
.mon-score .lbl { font: 600 10px/1 ui-monospace,monospace; letter-spacing:.18em;
  text-transform:uppercase; color:var(--dim); margin-top:.45rem; }
.mon-section { font: 600 11px/1 ui-monospace,monospace; letter-spacing:.22em;
  color:var(--accent); margin:1.5rem 0 .6rem; border-top:1px solid var(--line);
  padding-top:.7rem; }
.mon-bar { display:flex; height:30px; border-radius:7px; overflow:hidden;
  background:#1f1f24; box-shadow:inset 0 1px 4px rgba(0,0,0,.5);
  border:1px solid var(--line); }
.seg { height:100%; transition:width .6s cubic-bezier(.4,0,.2,1); }
.seg-done    { background:linear-gradient(180deg,#ffd400,#e0a829); }
.seg-claimed { background:repeating-linear-gradient(45deg,#8a7320,#8a7320 7px,
  #6f5d18 7px,#6f5d18 14px); }
.seg-error   { background:#f87171; }
.seg-pending { background:#2b2b31; }
.mon-legend { display:flex; gap:1.3rem; flex-wrap:wrap; margin-top:.6rem;
  font: 500 12.5px/1 ui-monospace,monospace; color:var(--dim);
  align-items:center; }
.mon-legend .dot { display:inline-block; width:9px; height:9px; border-radius:2px;
  margin-right:6px; vertical-align:middle; }
.dot.done{background:var(--gold);} .dot.claimed{background:#8a7320;}
.dot.error{background:#f87171;} .dot.pending{background:#3a3a42;}
.mon-pct { margin-left:auto; font-weight:700; font-size:15px; color:var(--gold); }
.mon-empty { color:var(--dim); font-size:.9rem; padding:1.1rem;
  background:var(--card); border:1px dashed var(--line); border-radius:10px; }
.mon-run { font: 600 12px/1 ui-monospace,monospace; color:var(--gold);
  margin:.3rem 0 .5rem; }
.mon-run::before { content:'●'; margin-right:7px; color:var(--gold);
  animation:mon-pulse 1.3s infinite; }
@keyframes mon-pulse { 0%,100%{opacity:1;} 50%{opacity:.3;} }
</style>
"""
