"""
page_file_inventory_gov.py
==========================
File Inventory Governance — combined scan, assign, catalog workflow.

Tabs:
  🔍 Scan       — crawl drives, build GLOBAL_FILE_CATALOG
  📊 Dashboard  — inventory summary metrics
  📋 Assign     — manager creates assignments (filter + count + catalogers)
  📋 My Work    — cataloger workbench (view, catalog, skip)
  📈 Progress   — manager view of all assignments, flagged files
  ⚙️ Admin      — users, passwords, SMTP test
"""

import streamlit as st
import streamlit.components.v1 as _stc

def _scroll_down():
    """Scroll the main Streamlit pane down to reveal content below the fold."""
    _stc.html(
        "<script>"
        "var m=window.parent.document.querySelector('section.main');"
        "if(m)m.scrollBy(0,500);"
        "</script>",
        height=0
    )

import pandas as pd
from datetime import date, timedelta
from sqlalchemy import text

# ── Scan module ───────────────────────────────────────────────────────────────
_SCAN_OK  = False
_SCAN_ERR = None
try:
    from dataview.file_catalog.file_inventory import (
        ensure_inventory_schema,
        crawl_and_inventory,
        get_inventory_summary,
        get_inventory_by_type,
        get_duplicates,
        FILE_TYPE_GROUPS,
        _detect_dialect,
    )
    _SCAN_OK = True
except ImportError as _e:
    _SCAN_ERR = str(_e)

# ── Governance module ─────────────────────────────────────────────────────────
from dataview.file_catalog.file_inventory_governance import (
    ensure_governance_schema,
    has_any_user, create_user, authenticate_user,
    list_users, set_user_active, reset_password,
    _table, _new_id, _now_expr,
)
from dataview.file_catalog.audit_log import (
    audit_assign, audit_reassign, audit_remove_assign,
    audit_catalog, audit_skip, audit_crawl, audit_clear,
    audit_password_reset, audit_password_change,
    audit_user_create, audit_user_deactivate, audit_user_delete,
    audit_export, audit_ppdm_update,
    audit_impersonate, audit_impersonate_exit,
)
from dataview.file_catalog.inv_auth import (
    is_logged_in, current_user, login, logout, require_role,
    render_login_screen,
)
from dataview.file_catalog.inv_email import smtp_configured, test_smtp


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _gfc(dialect):
    if dialect == "oracle":    return "FILE_CATALOG_GLOBAL_FILE_CATALOG"
    if dialect == "snowflake": return '"FILE_CATALOG"."GLOBAL_FILE_CATALOG"'
    return "file_catalog.GLOBAL_FILE_CATALOG"

def _atbl(dialect): return _table(dialect, "INVENTORY_ASSIGNMENT")
def _gtbl(dialect): return _table(dialect, "INVENTORY_GROUP")
def _ftbl(dialect): return _table(dialect, "INVENTORY_GROUP_FILE")
def _utbl(dialect): return _table(dialect, "INVENTORY_USER")


def _top(dialect, n):
    if dialect == "mssql": return f"SELECT TOP {n}"
    return "SELECT"

def _limit(dialect, n):
    if dialect == "oracle":    return f"FETCH FIRST {n} ROWS ONLY"
    if dialect == "snowflake": return f"LIMIT {n}"
    return ""  # sqlserver uses TOP


def _count_matching(engine, dialect, ext_filter, root_filter, status_filter):
    """Return count of non-duplicate files matching filters."""
    conditions = ["DUPLICATE_GROUP IS NULL"]
    params: dict = {}
    if ext_filter:
        conditions.append("LOWER(FILE_EXT) = :ext")
        params["ext"] = ext_filter.lower()
    if root_filter:
        conditions.append("ROOT_PATH LIKE :root")
        params["root"] = f"%{root_filter}%"
    if status_filter != "All":
        conditions.append("CATALOG_STATUS = :status")
        params["status"] = status_filter
    where = " AND ".join(conditions)
    with engine.connect() as conn:
        return conn.execute(
            text(f"SELECT COUNT(*) FROM {_gfc(dialect)} WHERE {where}"),
            params
        ).fetchone()[0], where, params


def _fetch_inventory_ids(engine, dialect, where, params, n):
    """Fetch N INVENTORY_IDs matching where clause, excluding duplicates."""
    gfc = _gfc(dialect)
    if dialect == "mssql":
        sql = f"SELECT TOP {n} INVENTORY_ID FROM {gfc} WHERE {where} ORDER BY FILE_TYPE_GROUP, FILE_EXT, FILE_NAME"
    elif dialect == "oracle":
        sql = f"SELECT INVENTORY_ID FROM {gfc} WHERE {where} ORDER BY FILE_TYPE_GROUP, FILE_EXT, FILE_NAME FETCH FIRST {n} ROWS ONLY"
    else:
        sql = f"SELECT INVENTORY_ID FROM {gfc} WHERE {where} ORDER BY FILE_TYPE_GROUP, FILE_EXT, FILE_NAME LIMIT {n}"
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [r[0] for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Card styling helpers
# ─────────────────────────────────────────────────────────────────────────────

_CARD_CSS_INJECTED = False

def _inject_card_css():
    global _CARD_CSS_INJECTED
    if _CARD_CSS_INJECTED:
        return
    st.markdown("""
    <style>
    .inv-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 24px 28px 20px;
        margin-bottom: 16px;
        box-shadow: 0 1px 4px rgba(0,0,0,0.06);
    }
    .inv-card-title {
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 2px;
        text-transform: uppercase;
        color: #64748b;
        margin-bottom: 14px;
        border-bottom: 1px solid #f1f5f9;
        padding-bottom: 8px;
    }
    .inv-card-success {
        border-left: 4px solid #22c55e;
        background: #f0fdf4;
    }
    .inv-card-warn {
        border-left: 4px solid #f59e0b;
        background: #fffbeb;
    }
    .inv-card-info {
        border-left: 4px solid #3b82f6;
        background: #eff6ff;
    }
    </style>
    """, unsafe_allow_html=True)
    _CARD_CSS_INJECTED = True


def card(title: str, variant: str = ""):
    """Open a card div. Call card_end() to close."""
    _inject_card_css()
    cls = f"inv-card inv-card-{variant}" if variant else "inv-card"
    st.markdown(
        f'<div class="{cls}"><div class="inv-card-title">{title}</div>',
        unsafe_allow_html=True
    )


def card_end():
    st.markdown("</div>", unsafe_allow_html=True)



# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def render(engine, dialect: str):
    # Guard against no DB connection
    if engine is None:
        st.warning("⚠️ No database connection. Connect via the pipeline first.")
        return

    # Ensure governance tables exist
    try:
        ensure_governance_schema(engine, dialect)
        from dataview.file_catalog.file_inventory_governance import migrate_group_file_columns
        migrate_group_file_columns(engine, dialect)
        # Pre-create header catalog tables — fast no-op if already exist
        try:
            from dataview.file_catalog.file_header_catalog import ensure_header_schema
            ensure_header_schema(engine, dialect)
            st.session_state["wb_schema_ok"] = True
        except Exception:
            pass
        # Pre-create audit table
        try:
            from dataview.file_catalog.audit_log import ensure_audit_table
            ensure_audit_table(engine)
        except Exception:
            pass
    except Exception as ex:
        st.error(f"❌ Governance schema init failed: {ex}")
        return

    # Ensure inventory table exists
    if _SCAN_OK:
        try:
            ensure_inventory_schema(engine)
        except Exception:
            pass

    # Login gate
    if not is_logged_in():
        render_login_screen(engine, dialect, {})
        return

    user = current_user()
    role = user["role"]

    # ── Impersonation banner ──────────────────────────────────────────────────
    if st.session_state.get("inv_impersonating"):
        orig = st.session_state["inv_original_user"]
        st.warning(
            f"👤 **Impersonating: {user['full_name']}** "
            f"({user['role']})  —  "
            f"logged in as **{orig['full_name']}**",
            icon="⚠️"
        )
        if st.button("🚪 Exit Impersonation", key="exit_impersonate"):
            # Restore original user
            st.session_state["inv_user_id"]    = orig["user_id"]
            st.session_state["inv_user_name"]  = orig["full_name"]
            st.session_state["inv_user_email"] = orig["email"]
            st.session_state["inv_user_role"]  = orig["role"]
            st.session_state.pop("inv_impersonating", None)
            st.session_state.pop("inv_original_user", None)
            st.session_state["inv_nav"] = "admin"
            st.rerun()
        st.divider()

    # ── Top bar with data_wrangler.png header ─────────────────────────────────
    import base64, pathlib
    def _img_b64(path):
        p = pathlib.Path(path)
        return base64.b64encode(p.read_bytes()).decode() if p.exists() else None

    _dw_b64 = (_img_b64("assets/data_wrangler.png") or _img_b64("data_wrangler.png"))

    if _dw_b64:
        st.markdown(f"""
        <div style="display:flex;align-items:center;justify-content:space-between;
                    background:linear-gradient(135deg,#1A3A6A 0%,#0D2A5A 100%);
                    border:1px solid #C8922A;border-radius:12px;
                    padding:10px 20px;margin-bottom:8px;
                    box-shadow:0 2px 8px rgba(200,146,42,0.2);">
          <img src="data:image/png;base64,{_dw_b64}"
               style="height:54px;object-fit:contain;"/>
          <div style="text-align:right;">
            <div style="font-size:0.8rem;color:#94a3b8;">
              👤 <b style="color:#e2e8f0;">{user['full_name']}</b>
              &nbsp;·&nbsp;
              <span style="color:#C8922A;font-weight:600;">{role}</span>
            </div>
          </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        # Fallback if image not found
        st.markdown(f"""
        <div style="display:flex;align-items:center;justify-content:space-between;
                    background:linear-gradient(135deg,#1A3A6A 0%,#0D2A5A 100%);
                    border:1px solid #C8922A;border-radius:12px;
                    padding:14px 20px;margin-bottom:8px;">
          <div style="font-size:1.4rem;font-weight:900;color:#C8922A;letter-spacing:-0.5px;">
            📂 File Inventory
          </div>
          <div style="font-size:0.8rem;color:#94a3b8;">
            👤 <b style="color:#e2e8f0;">{user['full_name']}</b>
            &nbsp;·&nbsp;
            <span style="color:#C8922A;font-weight:600;">{role}</span>
          </div>
        </div>
        """, unsafe_allow_html=True)

    # Sign out button on its own line
    col_so = st.columns([8, 1])[1]
    with col_so:
        if st.button("Sign Out", key="inv_logout"):
            logout(); st.rerun()

    st.divider()

    # ── Card navigation ───────────────────────────────────────────────────────
    # ── Navigation grouped into three stages ─────────────────────────
    # Stage 1: Inventory
    INVENTORY_NAV = [
        ("scan",      "🔍", "Scan",        "Crawl drives · build file inventory"),
        ("dashboard", "📊", "Dashboard",   "Metrics · duplicates · file types"),
    ]
    # Stage 2: Catalog
    CATALOG_NAV = [
        ("mywork",    "📋", "My Work",     "Review · classify · catalog files"),
        ("surveys",   "📄", "PDF / Docs",  "Extract surveys · tops · core · DST"),
        ("shapefiles","🗺️",  "Shapefiles",  "Wells · boundaries · seismic extents"),
        ("office",    "📊", "Excel / Word","Production · completion · well data"),
        ("dirload",   "⬇️", "Load to DB",  "Extract → promote docs to dataview"),
        ("progress",  "📈", "Progress",    "Track cataloger assignments"),
    ]
    # Stage 3: Admin
    ADMIN_NAV = [
        ("admin",     "⚙️", "Manager",     "Users · assignments · settings"),
    ]
    NAV_ITEMS = INVENTORY_NAV + CATALOG_NAV + ADMIN_NAV

    # Show only tabs the user has access to
    if role == "USER":
        # End users routed to browse — no catalog nav
        _tab_user_browse(engine)
        return
    visible = NAV_ITEMS if role in ("MANAGER","DELEGATE") else [
        n for n in NAV_ITEMS if n[0] not in ("admin",)
    ]

    # Current active section
    if "inv_nav" not in st.session_state:
        st.session_state["inv_nav"] = "scan"
    active = st.session_state["inv_nav"]

    st.markdown("""
    <style>
    .nav-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
        gap: 12px;
        margin-bottom: 24px;
    }
    .nav-card {
        background: rgba(255,255,255,0.08);
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 18px 14px 14px;
        text-align: center;
        box-shadow: 0 1px 3px rgba(0,0,0,0.06);
        cursor: pointer;
        transition: box-shadow .15s;
    }
    .nav-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.10); }
    .nav-card.active {
        border-color: #C8922A;
        box-shadow: 0 0 0 2px #C8922A33;
    }
    .nav-icon { font-size: 2rem; margin-bottom: 6px; }
    .nav-title {
        font-size: 0.95rem; font-weight: 700;
        color: #1e293b; margin-bottom: 4px;
    }
    .nav-desc {
        font-size: 0.75rem; color: #64748b; line-height: 1.3;
    }
    .nav-card.active .nav-title { color: #C8922A; }
    </style>
    """, unsafe_allow_html=True)

    # Three-stage nav: Inventory → Catalog → Admin
    def _nav_card(col, key, icon, title, desc):
        with col:
            is_active = active == key
            border     = "2px solid #3dd68c" if is_active else "1px solid #e2e8f0"
            title_col  = "#3dd68c" if is_active else "#1e293b"
            st.markdown(f"""
            <div style="background:rgba(255,255,255,0.08);
                        border:{border};border-radius:8px;
                        padding:8px 4px 6px;text-align:center;">
                <div style="font-size:1.2rem;margin-bottom:2px">{icon}</div>
                <div style="font-size:0.75rem;font-weight:700;color:{title_col}">{title}</div>
                <div style="font-size:0.62rem;color:rgba(255,255,255,0.7);line-height:1.2">{desc}</div>
            </div>""", unsafe_allow_html=True)
            if st.button("Select", key=f"nav_{key}",
                         type="primary" if is_active else "secondary",
                         use_container_width=True):
                st.session_state["inv_nav"] = key
                st.rerun()

    # All 8 cards in one row
    all_nav = INVENTORY_NAV + CATALOG_NAV
    if role in ("MANAGER", "DELEGATE"):
        all_nav = all_nav + ADMIN_NAV

    cols = st.columns(len(all_nav))
    for col, (key, icon, title, desc) in zip(cols, all_nav):
        _nav_card(col, key, icon, title, desc)

    st.divider()

    # ── Render active section ─────────────────────────────────────────────────
    if   active == "scan":      _tab_scan(engine, dialect)
    elif active == "dashboard": _tab_dashboard(engine, dialect)
    elif active == "mywork":    _tab_my_work(engine, dialect, user)
    elif active == "shapefiles":
        st.session_state["fc_default_tab"] = "manage"
        st.session_state.setdefault("mb_grp", "Shapefile")
        from dataview.file_catalog import page_file_catalog
        page_file_catalog.run(engine, dialect)
    elif active == "surveys":
        st.session_state["fc_default_tab"] = "manage"
        st.session_state.setdefault("mb_grp", "PDF")
        from dataview.file_catalog import page_file_catalog
        page_file_catalog.run(engine, dialect)
    elif active == "office":
        st.session_state["fc_default_tab"] = "manage"
        st.session_state.setdefault("mb_grp", "Office")
        from dataview.file_catalog import page_file_catalog
        page_file_catalog.run(engine, dialect)
    elif active == "dirload":   _tab_directory_load(engine, dialect, user)
    elif active == "progress":  _tab_progress(engine, dialect, user, role)
    elif active == "admin":     _tab_admin(engine, dialect, user, role)


# ─────────────────────────────────────────────────────────────────────────────
# Tab: Scan
# ─────────────────────────────────────────────────────────────────────────────


def _run_scoring(engine, dialect, ext_filter, file_count, max_workers: int = 8):
    """
    Extract headers from all unprocessed files in parallel, write status back.

    Phase 2 of the crawl pipeline. Pulls every row in GLOBAL_FILE_CATALOG
    that hasn't been extracted yet (HEADER_EXTRACTED NULL or 'N'), runs the
    extractors in parallel across `max_workers` threads, and writes each
    file's EXTRACTION_STATUS back to the inventory row.

    Path B buckets:
      SUCCESS — identifying + metadata fields both extracted
      PARTIAL — one of identifying/metadata extracted, not both
      EMPTY   — extractor ran without error but produced no useful fields
      FAILED  — extractor errored

    The heavy work (PDF/DLIS/SEGY parsing) is what we parallelize. DB writes
    stay sequential in the main thread — single-row UPDATEs are microseconds
    each and benefit nothing from parallelism, while serial writes avoid
    every connection-pool tuning headache.
    """
    from dataview.file_catalog.catalog_rules import extract_files_parallel, write_score
    from sqlalchemy import text
    from pathlib import Path as _P

    try:
        with engine.connect() as con:
            rows = con.execute(text("""
                SELECT INVENTORY_ID, FILE_PATH, FILE_EXT
                FROM file_catalog.GLOBAL_FILE_CATALOG
                WHERE (HEADER_EXTRACTED IS NULL OR HEADER_EXTRACTED = 'N')
                ORDER BY SCAN_DATE DESC
            """)).fetchall()
    except Exception as e:
        st.error(f"Score query failed: {e}")
        return

    total = len(rows)
    if total == 0:
        st.info("All files already extracted.")
        return

    prog = st.progress(0.0, text=f"Extracting 0/{total} (parallel × {max_workers})…")

    # Path B status buckets
    success = partial_ = empty_ = failed = errors = 0
    err_msgs: list[str] = []

    # Progress callback runs in the MAIN thread when extract_files_parallel
    # yields a completed result (safe for Streamlit). The callback only
    # updates the progress bar; result handling happens in the loop below.
    def _on_progress(done: int, total_: int, file_path: str):
        try:
            prog.progress(
                done / total_,
                text=f"Extracting {done}/{total_} · {_P(file_path).name}",
            )
        except Exception:
            pass

    # Stream results: parallel extraction → sequential DB writes
    for result in extract_files_parallel(
        rows,
        engine=engine,
        max_workers=max_workers,
        progress_callback=_on_progress,
    ):
        # Write status back to GLOBAL_FILE_CATALOG. write_score is its own
        # short transaction per row — a single bad write doesn't stop the
        # batch.
        wrote = write_score(
            engine,
            result["inventory_id"],
            result["scored"] or {"status": result["status"]},
            result["fields"],
        )

        status = result["status"]
        if status == "SUCCESS":
            success += 1
        elif status == "PARTIAL":
            partial_ += 1
        elif status == "EMPTY":
            empty_ += 1
        else:  # FAILED
            failed += 1
            errors += 1
            if result.get("error"):
                err_msgs.append(
                    f"{_P(result['file_path']).name}: {result['error']}"
                )

        if not wrote:
            # DB write failed (separate from extraction success). Count as
            # an error but don't double-bucket — the file's extraction
            # status counter already incremented above.
            errors += 1
            err_msgs.append(
                f"{_P(result['file_path']).name}: DB write failed"
            )

    prog.empty()

    # Path B status display — matches the readiness panel labels below
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("✅ Success", success)
    c2.metric("🟡 Partial", partial_)
    c3.metric("⚪ Empty",   empty_)
    c4.metric("❌ Failed",  failed)

    if errors:
        st.caption(f"{errors} files had extraction or write errors.")
        with st.expander(f"⚠️ {errors} error(s)", expanded=False):
            for m in err_msgs[:50]:
                st.text(m)
            if len(err_msgs) > 50:
                st.caption(f"… and {len(err_msgs) - 50} more")


def _tab_scan(engine, dialect):
    if not _SCAN_OK:
        st.error(f"❌ file_inventory module not available: {_SCAN_ERR}")
        return

    st.subheader("🔍 Scan File System")
    st.caption("Crawl drives to build the global file inventory. Duplicates are detected automatically and excluded from assignments.")

    root_input = st.text_area("Root paths (one per line)", height=100, key="inv_roots",
                               placeholder="e.g.\nC:\\Data\\Well_Logs\nD:\\Seismic")

    st.markdown("**File types**")
    cols = st.columns(len(FILE_TYPE_GROUPS))
    selected_exts: list[str] = []
    for i, (group, exts) in enumerate(FILE_TYPE_GROUPS.items()):
        with cols[i]:
            if st.checkbox(group, value=group in ("Well Logs","Seismic"),
                           key=f"inv_grp_{group}"):
                selected_exts.extend(exts)

    custom = st.text_input("Additional extensions (comma-separated)",
                            key="inv_custom", placeholder=".pet, .zgy")
    if custom:
        for e in custom.split(","):
            e = e.strip().lower()
            if e and not e.startswith("."): e = "." + e
            if e: selected_exts.append(e)
    selected_exts = sorted(set(selected_exts))

    if selected_exts:
        st.caption(f"Selected: {' · '.join(selected_exts)}")

    col_a, col_b = st.columns(2)
    with col_a:
        replace = st.checkbox("Replace previous results", value=True, key="inv_replace")
    with col_b:
        max_workers = st.number_input("Threads", min_value=1, max_value=16,
                                       value=4, key="inv_threads")

    run_triage_after = st.checkbox(
        "🔎 Run triage after scan",
        value=True, key="inv_run_triage")

    st.caption("ℹ️ Scan collects file metadata only. Duplicate detection runs server-side after load.")
    st.divider()
    roots = [r.strip() for r in (root_input or st.session_state.get("inv_roots","")).splitlines() if r.strip()]

    col_btn, col_info = st.columns([1, 3])
    with col_btn:
        run_scan = st.button("🚀 Start Scan", type="primary",
                              use_container_width=True, key="inv_scan_btn")
    with col_info:
        if roots and selected_exts:
            st.caption(f"**{len(roots)} path(s)** · **{len(selected_exts)} extension(s)** · "
                       f"{max_workers} threads · Duplicate detection: server-side")

    if not run_scan:
        return

    if not roots:
        st.error("Enter at least one root path."); return
    if not selected_exts:
        st.error("Select at least one file type."); return

    import os
    bad = [r for r in roots if not os.path.isdir(r)]
    for b in bad:
        st.error(f"Not found: `{b}`")
    if bad: return

    discover_box = st.empty()
    prog = st.progress(0, text="Preparing…")
    count_box = st.empty()

    def _cb(done, total, name):
        if total == 0:
            prog.progress(0, text=f"🔎 {name}")
        else:
            pct = min(done / total, 1.0)
            prog.progress(pct, text=f"Indexing {done:,} / {total:,} — {pct*100:.1f}%")
            count_box.caption(f"📄 `{name}`")

    try:
        discover_box.info(f"🔎 Scanning **{len(roots)} path(s)** with **{max_workers} threads**…")
        result = crawl_and_inventory(
            engine=engine, root_paths=roots,
            extensions=selected_exts, full_hash=False,
            max_workers=int(max_workers), replace_root=replace,
            progress_callback=_cb,
        )
        prog.progress(1.0, text="✅ Complete")
        discover_box.empty(); count_box.empty()
        import time; time.sleep(0.4); prog.empty()

        c1,c2,c3,c4 = st.columns(4)
        c1.metric("Files found",   f"{result['files_found']:,}")
        c2.metric("Files indexed", f"{result['files_inserted']:,}")
        c3.metric("Duplicates",    f"{result['duplicates']:,}")
        c4.metric("Errors",        f"{len(result['errors']):,}")

        if result["errors"]:
            with st.expander(f"⚠️ {len(result['errors'])} error(s)"):
                for e in result["errors"]: st.text(e)
        else:
            st.success(f"✅ {result['files_inserted']:,} files indexed · "
                       f"{result['duplicates']:,} duplicates detected and excluded from assignments.")

            # Phase 2: Auto-extract ALL files immediately after scan.
            # Reuse the same thread count as Phase 1 — one tuner, one
            # mental model. User who knows their box has 8 cores sets
            # threads=8 once and both phases honor it.
            if result.get('files_inserted', 0) > 0:
                st.markdown(f"**Phase 2 — Extracting headers (parallel × {int(max_workers)})…**")
                _run_scoring(engine, dialect, selected_exts,
                             result['files_inserted'],
                             max_workers=int(max_workers))

            # Phase 3 — Triage: enrich identity + tier the inventory. Runs after
            # every clean scan, even with 0 new files, so a re-scan re-tiers the
            # existing inventory and parked AWAITING_UWI wells can resolve
            # against newly-extracted siblings. Whole-inventory and idempotent.
            if run_triage_after and dialect not in ("oracle", "snowflake"):
                st.markdown("**Phase 3 — Triage (identity + tiering)…**")
                try:
                    from dataview.file_catalog import triage_inventory
                    tiers = triage_inventory.run_all_engine(
                        engine, log=lambda m: None)
                    tc = st.columns(4)
                    for col, t in zip(tc, ("HIGH", "REVIEW", "LOW", "REJECT")):
                        col.metric(t, f"{tiers.get(t, 0):,}")
                    st.success("✅ Triage complete — inventory enriched "
                               "and tiered.")
                except Exception as e:
                    st.warning(f"Triage step skipped: "
                               f"{type(e).__name__}: {e}")
            elif run_triage_after:
                st.caption("Triage runs on SQL Server inventories; "
                           f"skipped for {dialect}.")
    except Exception as e:
        prog.empty()
        st.error(f"Scan failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Tab: Dashboard
# ─────────────────────────────────────────────────────────────────────────────

def _tab_dashboard(engine, dialect):
    if not _SCAN_OK:
        st.error(f"❌ {_SCAN_ERR}"); return
    st.subheader("📊 Inventory Overview")
    try:
        s = get_inventory_summary(engine)
    except Exception as e:
        st.error(str(e)); return

    if s["total_files"] == 0:
        st.info("No files in inventory yet. Run a scan first.")
        return

    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Total files",  f"{s['total_files']:,}")
    c2.metric("Total size",   f"{s['total_size_mb']:,.0f} MB")
    c3.metric("Cataloged",    f"{s['cataloged']:,}")
    c4.metric("Uncataloged",  f"{s['uncataloged']:,}")
    c5.metric("Duplicates",   f"{s['duplicates']:,}")

    pct = s["cataloged"] / s["total_files"] if s["total_files"] else 0
    st.divider()
    st.markdown(f"**Cataloging Progress — {pct*100:.1f}%**")
    st.progress(pct)

    try:
        df = get_inventory_by_type(engine)
        if not df.empty:
            for group in sorted(df["FILE_TYPE_GROUP"].unique()):
                gdf = df[df["FILE_TYPE_GROUP"] == group]
                total = int(gdf["file_count"].sum())
                catd  = int(gdf[gdf["CATALOG_STATUS"]=="CATALOGED"]["file_count"].sum())
                mb    = float(gdf["size_mb"].sum() or 0)
                gpct  = catd / total if total else 0
                with st.expander(f"**{group}** — {total:,} files · {mb:,.0f} MB · {gpct*100:.0f}% cataloged"):
                    st.progress(gpct, text=f"{catd:,} / {total:,}")
    except Exception:
        pass

    # Duplicates summary
    try:
        dups = get_duplicates(engine)
        if not dups.empty:
            st.divider()
            st.warning(f"**{len(dups):,} duplicate files** detected — excluded from assignments.")
            with st.expander("View duplicates"):
                st.dataframe(dups, hide_index=True, use_container_width=True)
                st.download_button("⬇ Export", dups.to_csv(index=False),
                                   "duplicates.csv", "text/csv", key="dup_dl")
    except Exception:
        pass

    _render_catalog_readiness(engine)


# ─────────────────────────────────────────────────────────────────────────────
# Catalog readiness summary — appended to dashboard
# ─────────────────────────────────────────────────────────────────────────────

def _render_catalog_readiness(engine):
    """Show catalog readiness breakdown from scored inventory."""

    # Show background scoring progress if running
    try:
        from dataview.file_catalog.file_inventory import get_bg_status
        from pathlib import Path as _P
        bg = get_bg_status()
        if bg.get("running"):
            scored = bg.get("scored", 0)
            total  = bg.get("total", 0)
            pct    = scored / total if total else 0
            st.progress(pct,
                text=f"🔍 Scoring {scored:,}/{total:,} · "
                     f"{_P(bg.get('current','')).name}")
            st.button("🔄 Refresh", key="bg_refresh")
        elif bg.get("done") and bg.get("scored", 0) > 0:
            st.success(f"✅ Background scoring complete — "
                      f"{bg['scored']:,} files scored, "
                      f"{bg.get('errors',0)} errors")
    except Exception:
        pass

    st.divider()
    st.markdown("#### 🎯 Extraction Status")
    try:
        from sqlalchemy import text as _t
        with engine.connect() as con:
            # Path B: EXTRACTION_STATUS replaces CATALOG_READINESS.
            # No avg score either — score is gone in Path B.
            rows = con.execute(_t("""
                SELECT
                    EXTRACTION_STATUS,
                    COUNT(*) AS cnt
                FROM file_catalog.GLOBAL_FILE_CATALOG
                WHERE EXTRACTION_STATUS IS NOT NULL
                GROUP BY EXTRACTION_STATUS
                ORDER BY cnt DESC
            """)).fetchall()

        if not rows:
            st.info("No files extracted yet — run a scan to extract headers.")
            return

        import pandas as pd
        LABELS = {
            'SUCCESS': '✅ Success',
            'PARTIAL': '🟡 Partial',
            'EMPTY':   '⚪ Empty',
            'FAILED':  '❌ Failed',
            'SKIPPED': '⏭️ Skipped',
        }
        cols = st.columns(len(rows))
        for col, (status, cnt) in zip(cols, rows):
            label = LABELS.get(status, status)
            col.metric(label, f"{cnt:,}")

        # Breakdown table
        with st.expander("View extraction details"):
            df = pd.DataFrame(rows, columns=["Status", "Files"])
            df["Status"] = df["Status"].map(LABELS).fillna(df["Status"])
            st.dataframe(df, use_container_width=True, hide_index=True)

        # Re-extract button
        if st.button("🔄 Re-extract unprocessed files", key="rescore_btn"):
            with st.spinner("Extracting..."):
                from dataview.file_catalog.catalog_rules import score_inventory_batch
                summary = score_inventory_batch(engine, "mssql", limit=500)
                st.success(f"Processed {summary.get('scored', 0)} files")
                st.rerun()

    except Exception as e:
        st.caption(f"Extraction status unavailable: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Tab: Assign  (manager only)
# ─────────────────────────────────────────────────────────────────────────────

def _tab_assign(engine, dialect, user, role):
    st.subheader("📋 Create Assignment")

    if role not in ("MANAGER", "DELEGATE"):
        st.info("Only Managers and Delegates can create assignments.")
        return
    if not _SCAN_OK:
        st.error(f"❌ {_SCAN_ERR}"); return

    # ── Phase 1: Filters (always shown, no DB calls) ───────────────────────
    st.markdown("**Step 1 — Filter & count**")
    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
    with col1:
        ext_filter = st.text_input("Extension", key="asgn_ext",
                                    placeholder="e.g. las").strip().lower()
        if ext_filter and not ext_filter.startswith("."): ext_filter = "." + ext_filter
    with col2:
        root_filter = st.text_input("Root Path contains", key="asgn_root",
                                     placeholder="e.g. C:\\Data").strip()
    with col3:
        status_filter = st.selectbox("Status", ["UNCATALOGED","All","CATALOGED"],
                                      key="asgn_status")
    with col4:
        st.write("")
        do_count = st.button("🔍 Count", key="asgn_count", type="primary")

    if do_count:
        try:
            cnt, where, params = _count_matching(engine, dialect,
                                                  ext_filter, root_filter, status_filter)
            st.session_state.update({
                "asgn_cnt":      cnt,
                "asgn_where":    where,
                "asgn_params":   params,
                "asgn_ext_used": ext_filter,
                "asgn_root_used": root_filter,
                "asgn_phase":    2,
            })
            _scroll_down()
        except Exception as ex:
            st.error(str(ex)); return

    # Nothing below renders until Count has been clicked
    if st.session_state.get("asgn_phase", 1) < 2:
        st.caption("Set filters above and click **Count** to continue.")
        return

    cnt        = st.session_state["asgn_cnt"]
    where      = st.session_state["asgn_where"]
    params     = st.session_state["asgn_params"]
    ext_used   = st.session_state.get("asgn_ext_used", "")
    root_used  = st.session_state.get("asgn_root_used", "")

    st.metric("Files available (excl. duplicates)", f"{cnt:,}")

    if cnt == 0:
        st.info("No files match — adjust filters and click Count again.")
        if st.button("↩ Reset", key="asgn_reset_1"):
            st.session_state["asgn_phase"] = 1
            st.rerun()
        return

    # Optional preview
    col_prev, col_reset = st.columns([1, 1])
    with col_prev:
        if st.button("👁 Preview 20 files", key="asgn_preview"):
            st.session_state["asgn_show_preview"] = True
    with col_reset:
        if st.button("↩ Change filters", key="asgn_reset_2"):
            st.session_state["asgn_phase"] = 1
            st.session_state.pop("asgn_show_preview", None)
            st.rerun()

    if st.session_state.get("asgn_show_preview"):
        try:
            gfc = _gfc(dialect)
            if dialect == "mssql":
                prev_sql = f"SELECT TOP 20 FILE_NAME,FILE_EXT,FILE_SIZE_KB,ROOT_PATH FROM {gfc} WHERE {where} ORDER BY FILE_TYPE_GROUP,FILE_EXT,FILE_NAME"
            elif dialect == "oracle":
                prev_sql = f"SELECT FILE_NAME,FILE_EXT,FILE_SIZE_KB,ROOT_PATH FROM {gfc} WHERE {where} ORDER BY FILE_TYPE_GROUP,FILE_EXT,FILE_NAME FETCH FIRST 20 ROWS ONLY"
            else:
                prev_sql = f'SELECT FILE_NAME,FILE_EXT,FILE_SIZE_KB,ROOT_PATH FROM {gfc} WHERE {where} ORDER BY FILE_TYPE_GROUP,FILE_EXT,FILE_NAME LIMIT 20'
            with engine.connect() as conn:
                prev = conn.execute(text(prev_sql), params).fetchall()
            st.dataframe(pd.DataFrame(prev, columns=["Name","Ext","KB","Root"]),
                         hide_index=True, use_container_width=True)
        except Exception as ex:
            st.error(str(ex))

    st.divider()

    # ── Phase 2: Assignment form (only renders after Count) ────────────────
    st.markdown("**Step 2 — Assignment details**")
    col_a, col_b = st.columns(2)
    with col_a:
        g_name = st.text_input("Assignment Name", key="asgn_name",
                                placeholder="e.g. LAS Files Week 1")
    with col_b:
        g_type = ext_used or st.text_input("File Type Label", key="asgn_type",
                                            placeholder="e.g. LAS")

    col_c, col_d = st.columns(2)
    with col_c:
        n_files = st.number_input(
            f"How many to assign (of {cnt:,})",
            min_value=1, max_value=cnt, value=min(cnt, 100),
            key="asgn_n_files"
        )
    with col_d:
        due = st.date_input("Due Date",
                             value=date.today() + timedelta(days=14),
                             key="asgn_due")

    st.divider()
    st.markdown("**Step 3 — Pick cataloger(s)**")

    # User list cached for session — only one DB call ever
    if "asgn_users_cache" not in st.session_state:
        st.session_state["asgn_users_cache"] = list_users(engine, dialect)
    active_users = [u for u in st.session_state["asgn_users_cache"]
                    if u["active_ind"] == "Y"]
    user_options = {f"{u['full_name']}": u["user_id"] for u in active_users}

    selected = st.multiselect("Catalogers", list(user_options.keys()),
                               key="asgn_catalogers",
                               placeholder="Pick one or more")

    if selected:
        n   = len(selected)
        per = int(n_files) // n
        rem = int(n_files) % n
        for i, label in enumerate(selected):
            count = per + (1 if i < rem else 0)
            st.caption(f"  • {label} → {count:,} files")

    st.divider()
    if st.button("🚀 Create Assignment", type="primary", key="asgn_create",
                  disabled=not (g_name.strip() and selected)):
        try:
            inv_ids     = _fetch_inventory_ids(engine, dialect, where, params, int(n_files))
            if not inv_ids:
                st.error("No files found."); return

            assignee_ids = [user_options[s] for s in selected]
            n_a   = len(assignee_ids)
            per_a = len(inv_ids) // n_a
            rem_a = len(inv_ids) % n_a

            group_id = _new_id(g_name)
            now      = _now_expr(dialect)
            gt = _gtbl(dialect)
            at = _atbl(dialect)
            ft = _ftbl(dialect)

            with engine.begin() as conn:
                conn.execute(text(f"""
                    INSERT INTO {gt}
                        (GROUP_ID,GROUP_NAME,DESCRIPTION,FILE_TYPE,
                         ROOT_PATH,TOTAL_FILES,STATUS,CREATED_BY,CREATED_DATE)
                    VALUES (:gid,:name,:desc,:ft,:rp,:total,'OPEN',:cb,{now})
                """), {"gid":group_id,"name":g_name.strip(),"desc":"",
                       "ft":g_type,"rp":root_used,"total":len(inv_ids),
                       "cb":user["user_id"]})

                idx = 0
                for i, uid in enumerate(assignee_ids):
                    count    = per_a + (1 if i < rem_a else 0)
                    aid      = _new_id(uid + group_id)
                    my_slice = inv_ids[idx: idx + count]
                    idx     += count

                    conn.execute(text(f"""
                        INSERT INTO {at}
                            (ASSIGNMENT_ID,GROUP_ID,ASSIGNED_TO,ASSIGNED_BY,
                             ASSIGNED_DATE,DUE_DATE,STATUS,FILE_COUNT)
                        VALUES (:aid,:gid,:uid,:ab,{now},:dd,'OPEN',:fc)
                    """), {"aid":aid,"gid":group_id,"uid":uid,
                           "ab":user["user_id"],"dd":due,"fc":count})

                    file_rows = [
                        {"gfid":_new_id(inv_id),"gid":group_id,
                         "aid":aid,"iid":inv_id,"ab":user["user_id"]}
                        for inv_id in my_slice
                    ]
                    if file_rows:
                        conn.execute(text(f"""
                            INSERT INTO {ft}
                                (GROUP_FILE_ID,GROUP_ID,ASSIGNMENT_ID,
                                 INVENTORY_ID,ADDED_BY,ADDED_DATE)
                            VALUES (:gfid,:gid,:aid,:iid,:ab,{now})
                        """), file_rows)

            names = selected
            _scroll_down()
            st.success(f"✅ **{g_name}** — {len(inv_ids):,} files assigned to: {', '.join(names)}")
            st.caption("Click **↩ Change filters** or adjust filters and click Count to create another assignment.")
            # Clear cached state so next Count starts fresh
            for k in ["asgn_cnt","asgn_where","asgn_params","asgn_phase",
                       "asgn_show_preview","asgn_users_cache","asgn_show_preview"]:
                st.session_state.pop(k, None)

        except Exception as ex:
            st.error(str(ex))

# ─────────────────────────────────────────────────────────────────────────────
# Tab: My Work  (cataloger workbench)
# ─────────────────────────────────────────────────────────────────────────────


def _catalog_file_auto(engine, dialect, row, repo_id: str, user: dict):
    """
    Auto-catalog a file from My Work batch mode.
    Dispatches to the correct catalog module based on file extension.
    Updates GLOBAL_FILE_CATALOG status on success.
    """
    from dataview.file_catalog.inv_workbench import mark_cataloged
    from dataview.file_catalog.seis_filename_parser import parse_seis_filename

    fp   = str(row.get("file_path") or row.get("file_name","")).strip()
    ext  = str(row.get("file_ext") or "").lower().lstrip(".")
    # Use empty string if no repo — catalog modules handle None/empty gracefully
    repo_id = repo_id or ""
    iid  = row["inventory_id"]
    gfid = row["group_file_id"]

    # Formats whose capture lives in page_workbench._load_rows_to_catalog
    # (self-parsing → cat_* mirrors → promote → dv_*). All share the same
    # dispatch: resolve the document UWI, then call the pure-logic capture.
    # Shapefile/OSDU/Office/WITSML self-resolve the well internally, but we
    # still pass the matched UWI when we have it so well-keyed rows land right.
    WORKBENCH_EXTS = {
        "las", "pdf", "xlsx", "xls", "xlsm", "docx", "doc",
        "json", "xml", "shp", "csv",
    }

    if ext in WORKBENCH_EXTS:
        # Resolve the document UWI from the row or the global catalog. Some
        # formats (shapefile/OSDU/WITSML/office) can self-resolve from the file,
        # so an empty UWI is not necessarily fatal — _load_rows_to_catalog will
        # still parse and capture what it can.
        from sqlalchemy import text as _t
        _uwi = (row.get("MATCHED_UWI") or row.get("uwi") or row.get("UWI") or "")
        if not _uwi:
            try:
                with engine.connect() as _c:
                    _rr = _c.execute(_t(
                        "SELECT TOP 1 MATCHED_UWI FROM file_catalog.GLOBAL_FILE_CATALOG "
                        "WHERE FILE_PATH = :p"), {"p": fp}).fetchone()
                _uwi = (_rr[0] if _rr and _rr[0] else "")
            except Exception:
                _uwi = ""
        # LAS/PDF need a UWI up front (their header capture keys on it); the
        # self-resolving formats can proceed without one.
        _needs_uwi = ext in ("las", "pdf")
        if _needs_uwi and not _uwi:
            r = {"ok": False,
                 "error": f"No UWI resolved for .{ext} — extract/match headers first"}
        else:
            from dataview.file_catalog.page_workbench import _load_rows_to_catalog
            _lr = _load_rows_to_catalog(engine, dialect, fp, f".{ext}", _uwi, [])
            # A clean "recognised but nothing to capture" (e.g. a non-well OSDU
            # kind, or an office file with no matching sub-loader) is reported
            # via note and should NOT be treated as a hard failure.
            _note = _lr.get("note", "") or ""
            _soft_ok = (_note.startswith("not_impl")
                        or _note.startswith("shapefile_skip")
                        or _note.startswith("no_target"))
            r = {"ok": bool(_lr.get("ok")) or _soft_ok,
                 "action": f"captured {_lr.get('loaded',0)} rows "
                           f"({_lr.get('rt','') or _note or ext})",
                 "error": ("; ".join(_lr.get("errors", []))
                           or f".{ext} capture produced no rows")}
    elif ext in ("dlis","dlf"):
        from dataview.file_catalog.dlis_catalog import catalog_dlis_file
        r = catalog_dlis_file(engine, fp, repo_id)
    elif ext == "lis":
        from dataview.file_catalog.dlis_catalog import catalog_lis_file
        r = catalog_lis_file(engine, fp, repo_id)
    elif ext in ("segy","sgy","seg"):
        from dataview.file_catalog.segy_catalog import catalog_segy_file
        parsed = parse_seis_filename(row["file_name"])
        r = catalog_segy_file(engine, fp, repo_id,
                               survey_name=parsed.get("survey_name") or None)
    elif ext in ("p190","p90","p1"):
        from dataview.file_catalog.p190_catalog import catalog_p190_file
        parsed = parse_seis_filename(row["file_name"])
        r = catalog_p190_file(engine, fp, repo_id,
                               survey_name=parsed.get("survey_name") or None)
    else:
        r = {"ok": False, "error": f"Unsupported format: .{ext}"}

    if r.get("ok"):
        mark_cataloged(engine, dialect, iid, gfid)
        try:
            from dataview.file_catalog.audit_log import audit_catalog
            audit_catalog(engine, user, row["file_name"],
                          ext.upper(), action=r.get("action",""))
        except Exception:
            pass
    else:
        raise Exception(r.get("error", "Catalog failed"))


def _tab_my_work(engine, dialect, user):
    st.subheader(f"📋 My Work — {user['full_name']}")

    # ── Change My Password ────────────────────────────────────────────────────
    with st.expander("🔑 Change My Password", expanded=False):
        c1,c2,c3 = st.columns(3)
        cur_pw  = c1.text_input("Current password",  type="password",
                                 key="chpw_cur")
        new_pw  = c2.text_input("New password",      type="password",
                                 key="chpw_new")
        conf_pw = c3.text_input("Confirm new password", type="password",
                                 key="chpw_conf")
        if st.button("🔑 Change Password", key="chpw_btn", type="primary"):
            if not cur_pw or not new_pw or not conf_pw:
                st.error("All three fields are required.")
            elif new_pw != conf_pw:
                st.error("New passwords do not match.")
            elif len(new_pw) < 8:
                st.error("Password must be at least 8 characters.")
            else:
                try:
                    import hashlib
                    cur_hash = hashlib.sha256(cur_pw.encode()).hexdigest()
                    with engine.connect() as con:
                        stored = con.execute(text(
                            f"SELECT PASSWORD_HASH FROM {_utbl(dialect)} "
                            f"WHERE USER_ID=:uid"
                        ), {"uid": user["user_id"]}).scalar()
                    if stored != cur_hash:
                        st.error("Current password is incorrect.")
                    else:
                        reset_password(engine, dialect,
                                       user["user_id"], new_pw.strip())
                        try:
                            audit_password_change(engine, user)
                        except Exception: pass
                        st.success("✅ Password changed successfully.")
                        # Clear fields
                        for k in ("chpw_cur","chpw_new","chpw_conf"):
                            st.session_state.pop(k, None)
                        st.rerun()
                except Exception as e:
                    st.error(str(e))
    st.divider()

    try:
        from dataview.file_catalog.inv_workbench import render_file_workbench
        _wb_ok = True
    except ImportError:
        _wb_ok = False

    at = _atbl(dialect)
    gt = _gtbl(dialect)
    ut = _utbl(dialect)
    ft = _ftbl(dialect)
    gfc = _gfc(dialect)

    # Get this user's open assignments
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT a.ASSIGNMENT_ID, g.GROUP_NAME, g.FILE_TYPE,
                   a.DUE_DATE, a.STATUS, a.FILE_COUNT
            FROM {at} a
            JOIN {gt} g ON a.GROUP_ID = g.GROUP_ID
            WHERE a.ASSIGNED_TO = :uid AND a.STATUS NOT IN ('COMPLETED','CLOSED')
            ORDER BY a.DUE_DATE
        """), {"uid": user["user_id"]}).fetchall()

    if not rows:
        st.success("🎉 No open assignments.")
        return

    assignments = [dict(zip(
        ["assignment_id","group_name","file_type","due_date","status","file_count"], r
    )) for r in rows]

    for a in assignments:
        due       = a["due_date"]
        today     = date.today()
        days_left = (due - today).days if isinstance(due, date) else None
        badge     = "🟢" if days_left is None or days_left > 3 \
                    else "🟡" if days_left >= 0 else "🔴"

        with st.expander(
            f"{badge} {a['group_name']} · {a['file_type']} · "
            f"{a['file_count']:,} files · Due {due}",
            expanded=False
        ):
            # Result of a "Catalog ALL" from the PREVIOUS run. Reported here
            # because the run that produces it ends in st.rerun(), which
            # throws away anything drawn before it. A bordered box, not an
            # expander — this is already inside one.
            _cat_all = st.session_state.pop(
                f"mw_cat_all_result_{a['assignment_id']}", None)
            if _cat_all:
                with st.container(border=True):
                    st.success(f"✅ {_cat_all['ok']} cataloged · "
                               f"{_cat_all['err']} error(s).")
                    if _cat_all["reasons"]:
                        st.markdown(f"**⚠ {_cat_all['err']} error(s) — details**")
                        for _reason, _cnt in _cat_all["reasons"]:
                            st.write(f"**{_cnt}×** {_reason}")
                        st.caption("first 10 files:")
                        for _m in _cat_all["files"]:
                            st.text(_m)

            # Get files for this assignment
            with engine.connect() as conn:
                file_rows = conn.execute(text(f"""
                    SELECT f.GROUP_FILE_ID, f.INVENTORY_ID,
                           g.FILE_NAME, g.FILE_EXT, g.FILE_SIZE_KB,
                           g.CATALOG_STATUS, g.FILE_PATH
                    FROM {ft} f
                    JOIN {gfc} g ON f.INVENTORY_ID = g.INVENTORY_ID
                    WHERE f.ASSIGNMENT_ID = :aid
                    ORDER BY g.FILE_TYPE_GROUP, g.FILE_EXT, g.FILE_NAME
                """), {"aid": a["assignment_id"]}).fetchall()

            if not file_rows:
                st.warning(
                    "⚠️ No files linked to this assignment. "
                    "The manager may need to re-create the assignment "
                    "from the **Admin → 📋 Assignments** tab."
                ); continue

            df = pd.DataFrame(file_rows, columns=[
                "group_file_id","inventory_id","file_name","file_ext",
                "file_size_kb","catalog_status","file_path"
            ])

            # Progress
            total  = len(df)
            catd   = int((df["catalog_status"] == "CATALOGED").sum())
            skpd   = int((df["catalog_status"] == "SKIPPED").sum())
            done   = catd + skpd
            remain = total - done
            pct    = done / total if total else 0
            st.progress(pct, text=f"{catd:,} cataloged · {skpd:,} skipped · {remain:,} remaining")

            # File list — read only, for context
            st.dataframe(
                df[["file_name","file_ext","file_size_kb","catalog_status","file_path"]],
                hide_index=True, use_container_width=True
            )

            st.download_button(
                "⬇ Export list",
                data=df.to_csv(index=False),
                file_name=f"{a['group_name']}_files.csv",
                mime="text/csv",
                key=f"mw_dl_{a['assignment_id']}"
            )

            # File workbench
            if _wb_ok:
                st.divider()
                todo = df[df["catalog_status"].isin(["UNCATALOGED","SKIPPED","ASSIGNED"])]
                if todo.empty:
                    st.success("✅ All files in this assignment are done!")
                    continue

                aid       = a["assignment_id"]
                mode_key  = f"mw_mode_{aid}"
                if mode_key not in st.session_state:
                    st.session_state[mode_key] = "batch"

                # ── Mode toggle ───────────────────────────────────────────────
                mc1, mc2 = st.columns([3,1])
                mc1.markdown(f"**{len(todo):,} file(s) remaining**")
                mode = mc2.radio("View mode",
                                  ["📋 Batch", "🔍 Single file"],
                                  horizontal=True,
                                  key=f"mw_view_{aid}",
                                  label_visibility="collapsed")

                # ══════════════════════════════════════════════════════════════
                # BATCH MODE — checklist + catalog selected / catalog all
                # ══════════════════════════════════════════════════════════════
                if mode == "📋 Batch":
                    st.caption("Check files you've reviewed and are ready to catalog.")

                    # Repo selector for the whole batch
                    try:
                        from dataview.file_catalog.inv_workbench import _get_repos, _auto_detect_repo
                        repos = _get_repos(engine)
                        # Auto-detect from first file path
                        first_path = todo.iloc[0]["file_path"] or ""
                        auto_id, auto_name = _auto_detect_repo(engine, first_path)
                        default_idx = (list(repos.keys()).index(auto_name)
                                       if auto_name and auto_name in repos else 0)
                        repo_label = st.selectbox(
                            "Repository (applies to all selected)",
                            list(repos.keys()),
                            index=default_idx,
                            key=f"mw_batch_repo_{aid}"
                        )
                        batch_repo_id = repos.get(repo_label, "")
                    except Exception:
                        batch_repo_id = ""
                    if not batch_repo_id:
                        st.warning("⚠️ No repository selected — files will be cataloged without a repository link.")

                    # Checklist grid
                    check_all = st.checkbox(
                        "Select all", key=f"mw_chk_all_{aid}",
                        value=st.session_state.get(f"mw_chk_all_{aid}", False)
                    )

                    checked = {}
                    for _, row in todo.iterrows():
                        gfid  = row["group_file_id"]
                        fname = row["file_name"]
                        ext   = row["file_ext"]
                        sz    = f"{row['file_size_kb']/1024:.1f} MB" if row["file_size_kb"] else ""
                        key   = f"mw_chk_{gfid}"
                        if check_all:
                            st.session_state[key] = True
                        val = st.checkbox(
                            f"{fname}  `{ext}`  {sz}",
                            key=key,
                            value=st.session_state.get(key, False)
                        )
                        checked[gfid] = val

                    n_checked = sum(checked.values())
                    sel_rows  = todo[todo["group_file_id"].isin(
                        [k for k,v in checked.items() if v]
                    )]

                    ca, cb, cc = st.columns(3)

                    # ── Catalog Selected ──────────────────────────────────────
                    if ca.button(
                        f"📥 Catalog {n_checked} selected",
                        type="primary",
                        key=f"mw_cat_sel_{aid}",
                        disabled=n_checked == 0
                    ):
                        ok = err = 0
                        prog = st.progress(0)
                        for i, (_, row) in enumerate(sel_rows.iterrows()):
                            prog.progress((i+1)/len(sel_rows))
                            try:
                                _catalog_file_auto(
                                    engine, dialect, row, batch_repo_id, user
                                )
                                ok += 1
                            except Exception as e:
                                st.warning(f"❌ {row['file_name']}: {e}")
                                err += 1
                        prog.empty()
                        if ok:
                            st.success(f"✅ {ok} file(s) cataloged.")
                        if err:
                            st.error(f"{err} error(s).")
                        st.rerun()

                    # ── Catalog ALL ───────────────────────────────────────────
                    if cb.button(
                        f"📥 Catalog ALL {len(todo)}",
                        key=f"mw_cat_all_{aid}",
                    ):
                        st.session_state[f"mw_confirm_all_{aid}"] = True

                    if st.session_state.get(f"mw_confirm_all_{aid}"):
                        st.warning(
                            f"This will catalog all {len(todo)} remaining "
                            f"files without individual review. Continue?"
                        )
                        y, n = st.columns(2)
                        if y.button("✅ Yes, catalog all",
                                     key=f"mw_cat_all_yes_{aid}",
                                     type="primary"):
                            ok = err = 0
                            _err_msgs = []
                            prog = st.progress(0)
                            for i, (_, row) in enumerate(todo.iterrows()):
                                prog.progress((i+1)/len(todo))
                                try:
                                    _catalog_file_auto(
                                        engine, dialect, row, batch_repo_id, user
                                    )
                                    ok += 1
                                except Exception as e:
                                    err += 1
                                    _msg = str(e).splitlines()[0][:200] if str(e) \
                                        else type(e).__name__
                                    _err_msgs.append(
                                        f"{row.get('file_name','?')}: {_msg}")
                            prog.empty()
                            st.session_state.pop(f"mw_confirm_all_{aid}", None)
                            # Stashed for the run AFTER the st.rerun()
                            # below. Reporting here was lost twice over: an
                            # expander cannot nest inside this assignment's
                            # expander, and the rerun discards everything
                            # drawn before it — so neither the success line
                            # nor the failure detail ever reached the screen.
                            from collections import Counter
                            st.session_state[f"mw_cat_all_result_{aid}"] = {
                                "ok": ok, "err": err,
                                "reasons": Counter(
                                    m.split(": ", 1)[-1]
                                    for m in _err_msgs).most_common(),
                                "files": _err_msgs[:10],
                            }
                            st.rerun()
                        if n.button("❌ Cancel", key=f"mw_cat_all_no_{aid}"):
                            st.session_state.pop(f"mw_confirm_all_{aid}", None)
                            st.rerun()

                    # ── Skip Selected ─────────────────────────────────────────
                    if cc.button(
                        f"⏭ Skip {n_checked} selected",
                        key=f"mw_skip_sel_{aid}",
                        disabled=n_checked == 0
                    ):
                        for _, row in sel_rows.iterrows():
                            try:
                                from dataview.file_catalog.inv_workbench import mark_cataloged
                                mark_cataloged(engine, dialect,
                                               row["inventory_id"],
                                               row["group_file_id"],
                                               status="SKIPPED")
                            except Exception:
                                pass
                        st.rerun()

                # ══════════════════════════════════════════════════════════════
                # SINGLE FILE MODE — full workbench with preview
                # ══════════════════════════════════════════════════════════════
                else:
                    labels = [
                        f"{'⏭' if r['catalog_status']=='SKIPPED' else '⬜'} "
                        f"{r['file_name']}  ({r['file_ext']})"
                        for _, r in todo.iterrows()
                    ]
                    sel_idx = st.selectbox(
                        "Select file", range(len(labels)),
                        format_func=lambda i: labels[i],
                        key=f"mw_sel_{aid}"
                    )
                    sel = todo.iloc[sel_idx]
                    st.caption(f"`{sel['file_path']}`")

                    # Next / Prev navigation
                    nav1, nav2 = st.columns(2)
                    if nav1.button("◀ Prev", key=f"mw_prev_{aid}",
                                    disabled=sel_idx == 0):
                        st.session_state[f"mw_sel_{aid}"] = sel_idx - 1
                        st.rerun()
                    if nav2.button("Next ▶", key=f"mw_next_{aid}",
                                    disabled=sel_idx >= len(todo) - 1):
                        st.session_state[f"mw_sel_{aid}"] = sel_idx + 1
                        st.rerun()

                    render_file_workbench(
                        engine         = engine,
                        dialect        = dialect,
                        inventory_id   = sel["inventory_id"],
                        file_path      = sel["file_path"],
                        catalog_status = sel["catalog_status"],
                        group_file_id  = sel["group_file_id"],
                        context_key    = aid[:12],
                    )

                # Auto-complete assignment when all done
                if done >= total - 1:
                    try:
                        with engine.begin() as conn:
                            conn.execute(text(f"""
                                UPDATE {at} SET STATUS='COMPLETED',
                                COMPLETED_DATE={_now_expr(dialect)}
                                WHERE ASSIGNMENT_ID=:aid
                            """), {"aid": aid})
                    except Exception:
                        pass


# ─────────────────────────────────────────────────────────────────────────────
# Tab: Progress  (manager view)
# ─────────────────────────────────────────────────────────────────────────────

def _tab_progress(engine, dialect, user, role):
    st.subheader("📈 Assignment Progress")

    at  = _atbl(dialect)
    gt  = _gtbl(dialect)
    ut  = _utbl(dialect)
    ft  = _ftbl(dialect)
    gfc = _gfc(dialect)

    # All groups
    with engine.connect() as conn:
        groups = conn.execute(text(f"""
            SELECT g.GROUP_ID, g.GROUP_NAME, g.FILE_TYPE,
                   g.TOTAL_FILES, g.STATUS, g.CREATED_DATE,
                   COUNT(a.ASSIGNMENT_ID) AS ASSIGNEES,
                   SUM(CASE WHEN a.STATUS='COMPLETED' THEN 1 ELSE 0 END) AS COMPLETED
            FROM {gt} g
            LEFT JOIN {at} a ON g.GROUP_ID = a.GROUP_ID
            GROUP BY g.GROUP_ID, g.GROUP_NAME, g.FILE_TYPE,
                     g.TOTAL_FILES, g.STATUS, g.CREATED_DATE
            ORDER BY g.CREATED_DATE DESC
        """)).fetchall()

    if not groups:
        st.info("No assignments created yet.")
        return

    gdf = pd.DataFrame(groups, columns=[
        "group_id","group_name","file_type","total_files",
        "status","created_date","assignees","completed"
    ])

    st.dataframe(
        gdf[["group_name","file_type","total_files","status","assignees","completed","created_date"]],
        hide_index=True, use_container_width=True
    )

    # Drill into a group
    group_map = {r["group_name"]: r["group_id"] for r in gdf.to_dict("records")}
    sel_group = st.selectbox("View group detail", ["— select —"] + list(group_map.keys()),
                              key="prog_sel_group")
    if not sel_group or sel_group == "— select —":
        return

    gid = group_map[sel_group]

    # Assignments in this group
    with engine.connect() as conn:
        asgns = conn.execute(text(f"""
            SELECT a.ASSIGNMENT_ID, u.FULL_NAME, a.DUE_DATE,
                   a.STATUS, a.FILE_COUNT
            FROM {at} a
            JOIN {ut} u ON a.ASSIGNED_TO = u.USER_ID
            WHERE a.GROUP_ID = :gid
            ORDER BY u.FULL_NAME
        """), {"gid": gid}).fetchall()

    adf = pd.DataFrame(asgns, columns=[
        "assignment_id","cataloger","due_date","status","file_count"
    ])

    for _, row in adf.iterrows():
        # Per-assignment file counts
        with engine.connect() as conn:
            counts = conn.execute(text(f"""
                SELECT g.CATALOG_STATUS, COUNT(*) AS cnt
                FROM {ft} f
                JOIN {gfc} g ON f.INVENTORY_ID = g.INVENTORY_ID
                WHERE f.ASSIGNMENT_ID = :aid
                GROUP BY g.CATALOG_STATUS
            """), {"aid": row["assignment_id"]}).fetchall()

        count_map = {r[0]: r[1] for r in counts}
        catd = count_map.get("CATALOGED", 0)
        skpd = count_map.get("SKIPPED", 0)
        total = row["file_count"]
        pct  = (catd + skpd) / total if total else 0

        due = row["due_date"]
        today = date.today()
        days_left = (due - today).days if isinstance(due, date) else None
        badge = "🟢" if days_left is None or days_left > 3 \
                else "🟡" if days_left >= 0 else "🔴"

        with st.expander(
            f"{badge} {row['cataloger']} · {row['status']} · "
            f"{catd:,} cataloged · {skpd:,} skipped · Due {due}"
        ):
            st.progress(pct, text=f"{catd+skpd:,} / {total:,} done ({pct*100:.0f}%)")

            # Flagged/skipped files
            with engine.connect() as conn:
                skipped = conn.execute(text(f"""
                    SELECT g.FILE_NAME, g.FILE_EXT, g.FILE_PATH,
                           f.SKIP_REASON
                    FROM {ft} f
                    JOIN {gfc} g ON f.INVENTORY_ID = g.INVENTORY_ID
                    WHERE f.ASSIGNMENT_ID = :aid
                      AND g.CATALOG_STATUS = 'SKIPPED'
                """), {"aid": row["assignment_id"]}).fetchall()

            if skipped:
                st.markdown(f"**⚠️ {len(skipped)} skipped files:**")
                st.dataframe(
                    pd.DataFrame(skipped, columns=["Name","Ext","Path","Reason"]),
                    hide_index=True, use_container_width=True
                )

            # Manager can view/catalog any file in the group
            if role in ("MANAGER","DELEGATE"):
                try:
                    from dataview.file_catalog.inv_workbench import render_file_workbench
                    with engine.connect() as conn:
                        all_files = conn.execute(text(f"""
                            SELECT f.GROUP_FILE_ID, f.INVENTORY_ID,
                                   g.FILE_NAME, g.FILE_EXT, g.CATALOG_STATUS, g.FILE_PATH
                            FROM {ft} f
                            JOIN {gfc} g ON f.INVENTORY_ID = g.INVENTORY_ID
                            WHERE f.ASSIGNMENT_ID = :aid
                            ORDER BY g.FILE_NAME
                        """), {"aid": row["assignment_id"]}).fetchall()
                    fdf = pd.DataFrame(all_files, columns=[
                        "group_file_id","inventory_id","file_name",
                        "file_ext","catalog_status","file_path"
                    ])
                    # Inline in a bordered box, not an expander: this runs
                    # inside the cataloger expander above and Streamlit
                    # forbids nesting. That outer expander is the disclosure.
                    with st.container(border=True):
                        st.markdown("**👁 Browse & view files**")
                        _aid = row["assignment_id"]

                        # ── Multi-select actions: View · Export · Copy to vault
                        import os, shutil
                        from pathlib import Path as _Pth

                        _bdf = fdf[["file_name", "file_ext",
                                    "catalog_status", "file_path"]].copy()
                        _bdf.insert(0, "View",   False)
                        _bdf.insert(1, "Export", False)
                        _bdf.insert(2, "Vault",  False)

                        _edited = st.data_editor(
                            _bdf,
                            key=f"browse_multi_{_aid}",
                            use_container_width=True, hide_index=True,
                            column_config={
                                "View":   st.column_config.CheckboxColumn("👁 View",   width="small"),
                                "Export": st.column_config.CheckboxColumn("📤 Export", width="small"),
                                "Vault":  st.column_config.CheckboxColumn("📥 Vault",  width="small"),
                                "file_name":      st.column_config.TextColumn("File", disabled=True),
                                "file_ext":       st.column_config.TextColumn("Ext", disabled=True, width="small"),
                                "catalog_status": st.column_config.TextColumn("Status", disabled=True, width="small"),
                                "file_path":      st.column_config.TextColumn("Path", disabled=True, width="large"),
                            },
                        )

                        _view_sel   = _edited.loc[_edited["View"],   "file_path"].tolist()
                        _export_sel = _edited.loc[_edited["Export"], "file_path"].tolist()
                        _vault_sel  = _edited.loc[_edited["Vault"],  "file_path"].tolist()

                        _dd1, _dd2 = st.columns(2)
                        _export_dir = _dd1.text_input(
                            "Export folder", key=f"browse_exp_dir_{_aid}",
                            placeholder=r"C:\Bulk\export")
                        _vault_dir = _dd2.text_input(
                            "Vault folder", key=f"browse_vault_dir_{_aid}",
                            placeholder=r"C:\Bulk\vault\raw")

                        def _copy_to(_paths, _dst, _label):
                            if not (_dst or "").strip():
                                st.warning(f"Set a {_label} folder first.")
                                return
                            _d = _Pth(_dst.strip())
                            try:
                                _d.mkdir(parents=True, exist_ok=True)
                            except Exception as _e:
                                st.error(f"Can't create {_label} folder: {_e}")
                                return
                            _ok = 0
                            for _p in _paths:
                                try:
                                    shutil.copy2(_p, _d / _Pth(_p).name)
                                    _ok += 1
                                except Exception as _e:
                                    st.warning(f"❌ {_Pth(_p).name}: {_e}")
                            st.success(f"Copied {_ok}/{len(_paths)} file(s) to {_d}")

                        _ba1, _ba2, _ba3 = st.columns(3)
                        if _ba1.button(f"👁 View ({len(_view_sel)})",
                                       key=f"browse_do_view_{_aid}",
                                       use_container_width=True,
                                       disabled=not _view_sel):
                            _ok = 0
                            for _p in _view_sel:
                                try:
                                    os.startfile(_p)   # open in OS default app
                                    _ok += 1
                                except Exception as _e:
                                    st.warning(f"❌ {_Pth(_p).name}: {_e}")
                            if _ok:
                                st.success(f"Opened {_ok} file(s) in their default app.")
                        if _ba2.button(f"📤 Export ({len(_export_sel)})",
                                       key=f"browse_do_export_{_aid}",
                                       use_container_width=True,
                                       disabled=not _export_sel):
                            _copy_to(_export_sel, _export_dir, "export")
                        if _ba3.button(f"📥 Copy to vault ({len(_vault_sel)})",
                                       key=f"browse_do_vault_{_aid}",
                                       use_container_width=True,
                                       disabled=not _vault_sel):
                            _copy_to(_vault_sel, _vault_dir, "vault")

                        st.divider()
                        st.caption("Or open one file in the detailed workbench:")

                        labels = [
                            f"{'✅' if r['catalog_status']=='CATALOGED' else '⏭' if r['catalog_status']=='SKIPPED' else '⬜'} "
                            f"{r['file_name']}  ({r['file_ext']})"
                            for _, r in fdf.iterrows()
                        ]
                        si = st.selectbox("Select file", range(len(labels)),
                                          format_func=lambda i: labels[i],
                                          key=f"prog_sel_{row['assignment_id']}")
                        _scroll_down()
                        sel = fdf.iloc[si]
                        st.caption(f"`{sel['file_path']}`")
                        render_file_workbench(
                            engine=engine, dialect=dialect,
                            inventory_id=sel["inventory_id"],
                            file_path=sel["file_path"],
                            catalog_status=sel["catalog_status"],
                            group_file_id=sel["group_file_id"],
                            context_key=f"prog_{row['assignment_id'][:8]}",
                        )
                except ImportError:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# Tab: Admin
# ─────────────────────────────────────────────────────────────────────────────

def _tab_admin(engine, dialect, user, role):
    st.subheader("⚙️ Admin")

    if not require_role("MANAGER","DELEGATE"):
        return

    tab_users, tab_assign_mgr, tab_smtp, tab_data, tab_settings, tab_copy, tab_search, tab_metrics, tab_headers, tab_audit = st.tabs([
        "👤 Users", "📋 Assignments", "📧 Email / SMTP", "🗄️ Data Management",
        "⚙️ Settings", "📦 Copy to Common Location",
        "🔍 Search", "📊 Metrics", "📄 Header Export", "📜 Audit Log"
    ])

    with tab_data:
        st.markdown("#### 🗄️ Clear Inventory / Assignments")
        st.warning("⚠️ These actions are permanent and cannot be undone.", icon="⚠️")

        ht  = _gfc(dialect)
        gt  = _gtbl(dialect)
        at  = _atbl(dialect)
        ft  = _ftbl(dialect)
        fht = ("FILE_CATALOG_FILE_HEADER" if dialect=="oracle"
               else '"FILE_CATALOG"."FILE_HEADER"' if dialect=="snowflake"
               else "file_catalog.FILE_HEADER")
        fct = ("FILE_CATALOG_FILE_CURVE" if dialect=="oracle"
               else '"FILE_CATALOG"."FILE_CURVE"' if dialect=="snowflake"
               else "file_catalog.FILE_CURVE")

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**Clear Assignments**")
            st.caption("Remove assignments by cataloger or clear all. "
                       "Resets affected files to UNCATALOGED. "
                       "Inventory file list is preserved.")

            # Load catalogers who have assignments
            try:
                with engine.connect() as conn:
                    rows = conn.execute(text(f"""
                        SELECT DISTINCT u.USER_ID, u.FULL_NAME, u.EMAIL,
                               COUNT(gf.GROUP_FILE_ID) AS file_count
                        FROM {at} a
                        JOIN file_catalog.INVENTORY_USER u ON a.ASSIGNED_TO = u.USER_ID
                        LEFT JOIN {ft} gf ON gf.ASSIGNMENT_ID = a.ASSIGNMENT_ID
                        GROUP BY u.USER_ID, u.FULL_NAME, u.EMAIL
                        ORDER BY u.FULL_NAME
                    """)).fetchall()
                assignees = [{"user_id": r[0], "name": r[1],
                              "email": r[2], "files": r[3]} for r in rows]
            except Exception:
                assignees = []

            clear_mode = st.radio("Clear by", ["Specific cataloger", "All catalogers"],
                                  key="adm_clear_mode", horizontal=True)

            if clear_mode == "Specific cataloger":
                if not assignees:
                    st.info("No active assignments found.")
                else:
                    opts = {f"{a['name']} ({a['files']:,} files)": a["user_id"]
                            for a in assignees}
                    sel_label = st.selectbox("Select cataloger", list(opts.keys()),
                                             key="adm_clear_who")
                    sel_uid = opts[sel_label]

                    if st.button("🗑️ Clear This Cataloger's Assignments",
                                 key="adm_clear_one", use_container_width=True):
                        st.session_state["adm_confirm_one"] = sel_uid

                    if st.session_state.get("adm_confirm_one") == sel_uid:
                        st.error(f"Remove all assignments for **{sel_label.split('(')[0].strip()}**?")
                        c1, c2 = st.columns(2)
                        if c1.button("✅ Yes, clear", key="adm_confirm_one_yes",
                                     use_container_width=True, type="primary"):
                            try:
                                with engine.begin() as conn:
                                    # Get assignment IDs for this user
                                    aids = conn.execute(text(f"""
                                        SELECT ASSIGNMENT_ID FROM {at}
                                        WHERE ASSIGNED_TO = :uid
                                    """), {"uid": sel_uid}).fetchall()
                                    aid_list = [r[0] for r in aids]
                                    if aid_list:
                                        placeholders = ",".join(f"'{a}'" for a in aid_list)
                                        # Reset file status
                                        inv_ids = conn.execute(text(f"""
                                            SELECT INVENTORY_ID FROM {ft}
                                            WHERE ASSIGNMENT_ID IN ({placeholders})
                                        """)).fetchall()
                                        if inv_ids:
                                            inv_list = ",".join(f"'{r[0]}'" for r in inv_ids)
                                            conn.execute(text(f"""
                                                UPDATE {ht} SET CATALOG_STATUS='UNCATALOGED'
                                                WHERE INVENTORY_ID IN ({inv_list})
                                            """))
                                        conn.execute(text(f"""
                                            DELETE FROM {ft}
                                            WHERE ASSIGNMENT_ID IN ({placeholders})
                                        """))
                                        conn.execute(text(f"""
                                            DELETE FROM {at}
                                            WHERE ASSIGNMENT_ID IN ({placeholders})
                                        """))
                                st.session_state.pop("adm_confirm_one", None)
                                st.success(f"✅ Assignments cleared for {sel_label.split('(')[0].strip()}.")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))
                        if c2.button("❌ Cancel", key="adm_cancel_one",
                                     use_container_width=True):
                            st.session_state.pop("adm_confirm_one", None)
                            st.rerun()
            else:
                if st.button("🗑️ Clear ALL Assignments", key="adm_clear_assign",
                             use_container_width=True):
                    st.session_state["adm_confirm_assign"] = True

                if st.session_state.get("adm_confirm_assign"):
                    st.error("This will remove ALL assignments and reset all file statuses.")
                    c1, c2 = st.columns(2)
                    if c1.button("✅ Yes, clear all", key="adm_confirm_assign_yes",
                                 use_container_width=True, type="primary"):
                        try:
                            with engine.begin() as conn:
                                # SCOPED TO THE FILES ACTUALLY ASSIGNED. This
                                # UPDATE had no WHERE clause, so clearing
                                # assignments reset EVERY row in the catalog --
                                # including 1,701 files that had already
                                # extracted, captured and promoted. Their rows
                                # were never at risk, but catalog_rules selects
                                # on CATALOG_STATUS and never writes CATALOGED
                                # back, so every one of them returned to the
                                # queue permanently. Read the ids BEFORE the
                                # DELETE, the way the single-assignment branch
                                # above already does.
                                _inv = [r[0] for r in conn.execute(text(
                                    f"SELECT DISTINCT INVENTORY_ID FROM {ft} "
                                    f"WHERE INVENTORY_ID IS NOT NULL")).fetchall()]
                                conn.execute(text(f"DELETE FROM {ft}"))
                                conn.execute(text(f"DELETE FROM {at}"))
                                conn.execute(text(f"DELETE FROM {gt}"))
                                for _i in range(0, len(_inv), 500):
                                    _chunk = ",".join(
                                        "'" + str(x).replace("'", "''") + "'"
                                        for x in _inv[_i:_i + 500])
                                    conn.execute(text(
                                        f"UPDATE {ht} SET CATALOG_STATUS='UNCATALOGED' "
                                        f"WHERE INVENTORY_ID IN ({_chunk})"))
                            st.session_state.pop("adm_confirm_assign", None)
                            st.success("✅ All assignments cleared. Inventory status reset.")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))
                    if c2.button("❌ Cancel", key="adm_cancel_assign",
                                 use_container_width=True):
                        st.session_state.pop("adm_confirm_assign", None)
                        st.rerun()

        with col2:
            st.markdown("**Clear Everything**")
            st.caption("Removes ALL inventory records, assignments, groups, "
                       "catalog headers and file curves. Complete reset.")
            if st.button("💥 Clear All Inventory & Assignments", key="adm_clear_all",
                         use_container_width=True):
                st.session_state["adm_confirm_all"] = True

            if st.session_state.get("adm_confirm_all"):
                st.error("This will delete EVERYTHING — inventory, assignments and catalog headers.")
                c1, c2 = st.columns(2)
                if c1.button("✅ Yes, clear everything", key="adm_confirm_all_yes",
                             use_container_width=True, type="primary"):
                    try:
                        with engine.begin() as conn:
                            conn.execute(text(f"DELETE FROM {ft}"))
                            conn.execute(text(f"DELETE FROM {at}"))
                            conn.execute(text(f"DELETE FROM {gt}"))
                            conn.execute(text(f"DELETE FROM {ht}"))
                        st.session_state.pop("adm_confirm_all", None)
                        st.success("✅ All inventory and assignment data cleared.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
                if c2.button("❌ Cancel", key="adm_cancel_all", use_container_width=True):
                    st.session_state.pop("adm_confirm_all", None)
                    st.rerun()

    with tab_assign_mgr:
        _tab_assignment_manager(engine, dialect, user)

    with tab_settings:
        _tab_settings(engine, dialect)

    with tab_copy:
        _tab_copy_to_common(engine, dialect)

    with tab_search:
        _tab_manager_search(engine, dialect)

    with tab_metrics:
        _tab_manager_metrics(engine, dialect)

    with tab_headers:
        _tab_header_export(engine, dialect)

    with tab_audit:
        _tab_audit_log(engine, dialect, user)

    with tab_users:
        users = list_users(engine, dialect)
        if users:
            st.dataframe(
                pd.DataFrame(users)[["full_name","email","role","active_ind","last_login"]],
                hide_index=True, use_container_width=True
            )
            # Login metrics summary
            import pandas as _pd
            df_users = _pd.DataFrame(users)
            if not df_users.empty:
                st.divider()
                st.caption("**Login activity by role:**")
                role_counts = df_users.groupby("role").agg(
                    Users=("user_id","count"),
                    Active=("active_ind", lambda x: (x=="Y").sum()),
                    Last_Login=("last_login","max")
                ).reset_index()
                st.dataframe(role_counts, hide_index=True, use_container_width=True)

        st.divider()
        st.markdown("#### 👥 Cataloger Management")

        if not users:
            st.info("No users yet. Add one below.")
        else:
            # ── User roster ───────────────────────────────────────────────────
            import pandas as _pd
            df_users = _pd.DataFrame(users)
            st.dataframe(
                df_users[["full_name","email","role","active_ind","last_login"]],
                hide_index=True, use_container_width=True,
                column_config={
                    "full_name":  st.column_config.TextColumn("Name"),
                    "email":      st.column_config.TextColumn("Email"),
                    "role":       st.column_config.TextColumn("Role"),
                    "active_ind": st.column_config.TextColumn("Active", width="small"),
                    "last_login": st.column_config.TextColumn("Last Login"),
                }
            )

        st.divider()

        # ── Tabs for each management action ───────────────────────────────────
        (u_add, u_edit, u_resetpw,
         u_impersonate, u_deact, u_delete) = st.tabs([
            "➕ Add", "✏️ Edit", "🔑 Reset Password",
            "👤 Impersonate", "⏸ Activate/Deactivate", "🗑️ Delete User"
        ])

        # ── Add user ─────────────────────────────────────────────────────────
        with u_add:
            st.markdown("**Create a new team member**")
            c1,c2 = st.columns(2)
            name  = c1.text_input("Full Name", key="u_add_name")
            email = c2.text_input("Email",     key="u_add_email")
            c3,c4 = st.columns(2)
            urole = c3.selectbox("Role",
                                  ["USER","CATALOGER","DELEGATE","MANAGER"],
                                  key="u_add_role")
            pw = c4.text_input("Temporary Password", type="password",
                                key="u_add_pw")
            if st.button("Create User", type="primary", key="u_add_btn"):
                if not name.strip() or not email.strip() or not pw:
                    st.error("Name, email and password are required.")
                else:
                    try:
                        create_user(engine, dialect, name.strip(),
                                    email.strip(), pw, urole,
                                    created_by=user["user_id"])
                        st.success(f"✅ {name} created as {urole}.")
                        st.rerun()
                    except Exception as ex:
                        st.error(str(ex))

        # ── Edit user ─────────────────────────────────────────────────────────
        with u_edit:
            if not users:
                st.info("No users to edit.")
            else:
                from sqlalchemy import text as _t
                st.markdown("**Edit name, email, role or reset password**")
                umap = {f"{u['full_name']} ({u['role']})": u for u in users}
                sel  = st.selectbox("Select user", list(umap.keys()),
                                     key="u_edit_sel")
                su   = umap[sel]
                c1,c2 = st.columns(2)
                new_name  = c1.text_input("Full Name", value=su["full_name"],
                                           key="u_edit_name")
                new_email = c2.text_input("Email",     value=su["email"],
                                           key="u_edit_email")
                c3,c4 = st.columns(2)
                roles = ["USER","CATALOGER","DELEGATE","MANAGER"]
                new_role = c3.selectbox("Role", roles,
                                         index=roles.index(su["role"])
                                         if su["role"] in roles else 1,
                                         key="u_edit_role")
                new_pw = c4.text_input("New Password (leave blank to keep)",
                                        type="password", key="u_edit_pw")
                c_save, c_impersonate = st.columns([2,1])
                if c_save.button("💾 Save Changes", type="primary",
                                  key="u_edit_save"):
                    try:
                        with engine.begin() as con:
                            con.execute(_t("""
                                UPDATE file_catalog.INVENTORY_USER
                                SET FULL_NAME=:nm, EMAIL=:em, ROLE=:ro,
                                    ROW_CHANGED_DATE=GETUTCDATE()
                                WHERE USER_ID=:uid
                            """), {"nm":new_name.strip(), "em":new_email.strip(),
                                   "ro":new_role, "uid":su["user_id"]})
                        if new_pw.strip():
                            reset_password(engine, dialect, su["user_id"],
                                           new_pw.strip())
                        st.success("✅ Saved.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

                if c_impersonate.button(
                    f"👤 Login As {su['full_name'].split()[0]}",
                    key="u_edit_impersonate"):
                    # Store original user before switching
                    orig_user = current_user()
                    st.session_state["inv_original_user"] = orig_user
                    st.session_state["inv_impersonating"] = True
                    # Switch to target user
                    st.session_state["inv_user_id"]    = su["user_id"]
                    st.session_state["inv_user_name"]  = su["full_name"]
                    st.session_state["inv_user_email"] = su["email"]
                    st.session_state["inv_user_role"]  = su["role"]
                    # Navigate to appropriate landing for their role
                    st.session_state["inv_nav"] = (
                        "mywork" if su["role"] == "CATALOGER"
                        else "scan"
                    )
                    st.rerun()

        # ── Assign work ───────────────────────────────────────────────────────
        # ── Reset Password ────────────────────────────────────────────────────
        with u_resetpw:
            st.markdown("**Reset a team member's password**")
            st.caption("Manager action — no current password required.")
            umap_pw = {f"{u['full_name']} ({u['role']})": u for u in users}
            sel_pw  = st.selectbox("Select user", list(umap_pw.keys()),
                                    key="u_rspw_sel")
            su_pw   = umap_pw[sel_pw]
            c1, c2  = st.columns(2)
            new_pw  = c1.text_input("New password", type="password",
                                     key="u_rspw_new",
                                     placeholder="Min 8 characters")
            conf_pw = c2.text_input("Confirm password", type="password",
                                     key="u_rspw_conf")
            if st.button("🔑 Reset Password", type="primary",
                          key="u_rspw_btn"):
                if not new_pw.strip():
                    st.error("Password cannot be blank.")
                elif len(new_pw.strip()) < 8:
                    st.error("Password must be at least 8 characters.")
                elif new_pw != conf_pw:
                    st.error("Passwords do not match.")
                else:
                    try:
                        reset_password(engine, dialect,
                                       su_pw["user_id"], new_pw.strip())
                        try:
                            audit_password_reset(engine, user,
                                                 su_pw["full_name"],
                                                 su_pw["user_id"])
                        except Exception: pass
                        st.success(
                            f"✅ Password reset for "
                            f"**{su_pw['full_name']}**."
                        )
                        for k in ("u_rspw_new","u_rspw_conf"):
                            st.session_state.pop(k, None)
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

        # ── Impersonate ───────────────────────────────────────────────────────
        with u_impersonate:
            from dataview.file_catalog.inv_auth import current_user as _cur_user
            st.markdown("**View the app as another user**")
            st.caption(
                "Switches your session to the selected user's role and view. "
                "A banner at the top lets you exit back to your account at any time."
            )
            if not users:
                st.info("No users found.")
            else:
                _me = _cur_user()
                umap_imp = {
                    f"{u['full_name']}  —  {u['role']}": u
                    for u in users
                    if u["user_id"] != _me["user_id"]
                }
                if not umap_imp:
                    st.info("No other users to impersonate.")
                else:
                    sel_imp = st.selectbox(
                        "Select user to impersonate",
                        list(umap_imp.keys()),
                        key="u_imp_sel"
                    )
                    su_imp = umap_imp[sel_imp]
                    st.info(
                        f"You will see the app exactly as "
                        f"**{su_imp['full_name']}** ({su_imp['role']}) sees it."
                    )
                    if st.button(
                        f"👤 Login As {su_imp['full_name']}",
                        type="primary", key="u_imp_btn"
                    ):
                        st.session_state["inv_original_user"] = _me
                        st.session_state["inv_impersonating"] = True
                        st.session_state["inv_user_id"]    = su_imp["user_id"]
                        st.session_state["inv_user_name"]  = su_imp["full_name"]
                        st.session_state["inv_user_email"] = su_imp["email"]
                        st.session_state["inv_user_role"]  = su_imp["role"]
                        st.session_state["inv_nav"] = (
                            "mywork" if su_imp["role"] == "CATALOGER"
                            else "scan"
                        )
                        try:
                            audit_impersonate(engine, _me, su_imp)
                        except Exception: pass
                        st.rerun()

        with u_deact:
            st.markdown("**Activate or deactivate team members**")
            if not users:
                st.info("No users.")
            else:
                umap = {f"{u['full_name']} ({u['role']})": u for u in users}
                sel  = st.selectbox("Select user", list(umap.keys()),
                                     key="u_deact_sel")
                su   = umap[sel]
                is_active = su["active_ind"] == "Y"
                st.info(f"**{su['full_name']}** is currently "
                        f"{'🟢 Active' if is_active else '🔴 Inactive'}")
                c1, c2 = st.columns(2)
                lbl = "⏸ Deactivate" if is_active else "▶ Activate"
                if c1.button(lbl, key="u_deact_btn", type="primary"):
                    set_user_active(engine, dialect, su["user_id"], not is_active)
                    st.success(f"✅ {'Deactivated' if is_active else 'Activated'}.")
                    st.rerun()
                npw = c2.text_input("Reset password", type="password",
                                     key="u_deact_pw",
                                     placeholder="Leave blank to keep current")
                if npw and c2.button("Reset Password", key="u_deact_pw_btn"):
                    reset_password(engine, dialect, su["user_id"], npw)
                    st.success("✅ Password reset.")

        # ── Delete user ───────────────────────────────────────────────────────
        with u_delete:
            st.markdown("**Permanently delete a team member**")
            st.caption("Cannot delete a user with active assignments — "
                       "remove or reassign their work first.")
            if not users:
                st.info("No users.")
            else:
                from sqlalchemy import text as _t
                umap = {f"{u['full_name']} ({u['role']})": u for u in users}
                sel  = st.selectbox("Select user to delete", list(umap.keys()),
                                     key="u_del_sel")
                su   = umap[sel]

                # Check for active assignments
                try:
                    with engine.connect() as con:
                        n_asgn = con.execute(_t(
                            "SELECT COUNT(*) FROM file_catalog.INVENTORY_ASSIGNMENT "
                            "WHERE ASSIGNED_TO=:uid"
                        ), {"uid": su["user_id"]}).scalar() or 0
                except Exception:
                    n_asgn = -1

                if n_asgn > 0:
                    st.warning(f"⚠️ **{su['full_name']}** has {n_asgn} active "
                               f"assignment(s). Use **🗑 Remove Work** or "
                               f"**🔄 Reassign** first.")
                else:
                    st.error(f"This will permanently delete **{su['full_name']}**. "
                             f"This cannot be undone.")
                    if st.button("🗑️ Delete User", key="u_del_btn"):
                        st.session_state["u_del_confirm"] = su["user_id"]

                    if st.session_state.get("u_del_confirm") == su["user_id"]:
                        c1, c2 = st.columns(2)
                        if c1.button("✅ Yes, delete permanently",
                                      key="u_del_yes", type="primary"):
                            try:
                                with engine.begin() as con:
                                    con.execute(_t(
                                        "DELETE FROM file_catalog.INVENTORY_USER "
                                        "WHERE USER_ID=:uid"
                                    ), {"uid": su["user_id"]})
                                st.session_state.pop("u_del_confirm", None)
                                st.success(f"✅ {su['full_name']} deleted.")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))
                        if c2.button("❌ Cancel", key="u_del_cancel"):
                            st.session_state.pop("u_del_confirm", None)
                            st.rerun()

    with tab_smtp:
        st.markdown("### Office 365 SMTP")
        st.info("Add to your `.env` file:")
        st.code("""SMTP_SERVER=smtp.office365.com
SMTP_PORT=587
SMTP_USER=you@yourdomain.com
SMTP_PASSWORD=your_app_password
NOTIFY_FROM=notifications@datawranglersolutions.com""")
        if smtp_configured():
            st.success("✅ SMTP configured.")
            to = st.text_input("Send test email to", value=user.get("email",""))
            if st.button("Send Test"):
                ok = test_smtp(to)
                st.success("Sent.") if ok else st.error("Failed — check logs.")
        else:
            st.warning("⚠️ SMTP not configured — email notifications disabled.")


# ─────────────────────────────────────────────────────────────────────────────
# Settings tab
# ─────────────────────────────────────────────────────────────────────────────

def _tab_settings(engine, dialect):
    from sqlalchemy import text
    st.markdown("#### ⚙️ Settings")

    try:
        with engine.begin() as con:
            con.execute(text("""
                IF NOT EXISTS (
                    SELECT 1 FROM sys.tables t
                    JOIN sys.schemas s ON t.schema_id=s.schema_id
                    WHERE s.name='file_catalog' AND t.name='INVENTORY_SETTING'
                )
                CREATE TABLE file_catalog.INVENTORY_SETTING (
                    SETTING_KEY   NVARCHAR(100) NOT NULL PRIMARY KEY,
                    SETTING_VALUE NVARCHAR(900) NULL,
                    DESCRIPTION   NVARCHAR(500) NULL,
                    UPDATED_DATE  DATETIME2 DEFAULT GETUTCDATE(),
                    UPDATED_BY    NVARCHAR(100) NULL
                )
            """))
    except Exception as e:
        st.error(f"Settings table: {e}"); return

    def _get(key, default=""):
        try:
            with engine.connect() as con:
                v = con.execute(text(
                    "SELECT SETTING_VALUE FROM file_catalog.INVENTORY_SETTING "
                    "WHERE SETTING_KEY=:k"
                ), {"k": key}).scalar()
            return v or default
        except Exception:
            return default

    def _set(key, value, desc=""):
        try:
            with engine.begin() as con:
                con.execute(text("""
                    MERGE file_catalog.INVENTORY_SETTING AS tgt
                    USING (SELECT :k AS SETTING_KEY) src ON tgt.SETTING_KEY=src.SETTING_KEY
                    WHEN MATCHED THEN UPDATE SET
                        SETTING_VALUE=:v, UPDATED_DATE=GETUTCDATE(), UPDATED_BY=:u
                    WHEN NOT MATCHED THEN INSERT
                        (SETTING_KEY,SETTING_VALUE,DESCRIPTION,UPDATED_BY)
                        VALUES (:k,:v,:d,:u);
                """), {"k": key, "v": value, "d": desc,
                       "u": st.session_state.get("inv_user_name","SYSTEM")})
        except Exception as e:
            st.error(str(e))

    st.markdown("**Common File Location**")
    st.caption("Files copied here are organised into a structured folder hierarchy "
               "by format and UWI/survey name.")

    common_root = st.text_input(
        "Common root folder",
        value=_get("COMMON_ROOT", ""),
        key="setting_common_root",
        placeholder=r"e.g. C:\CommonFiles\PetroleumData"
    )
    if st.button("💾 Save", key="setting_save_root", type="primary"):
        _set("COMMON_ROOT", common_root.strip(),
             "Root folder for organised copy of all cataloged files")
        st.success("✅ Saved.")

    st.divider()
    st.markdown("**Folder Structure Preview**")
    root = common_root.strip() or r"C:\CommonFiles"
    st.code(
        root + "\\\n"
        "├── WELL_LOGS\\\n"
        "│   ├── {UWI}\\\n"
        "│   │   ├── LAS\\\n"
        "│   │   ├── DLIS\\\n"
        "│   │   └── LIS\\\n"
        "│   └── UNMATCHED\\\n"
        "└── SEISMIC\\\n"
        "    ├── {SURVEY_NAME}\\\n"
        "    │   ├── SEGY\\\n"
        "    │   └── P190\\\n"
        "    └── UNMATCHED\\",
        language=None
    )


# ─────────────────────────────────────────────────────────────────────────────
# Copy to common location
# ─────────────────────────────────────────────────────────────────────────────

def _sanitize_folder(name):
    import re
    if not name or str(name).strip() in ("", "None", "nan"):
        return "UNMATCHED"
    s = str(name).strip().upper()
    s = re.sub(r'[\\/:*?"<>|\s\-]', "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "UNMATCHED"


def _get_common_root(engine):
    from sqlalchemy import text
    try:
        with engine.connect() as con:
            return con.execute(text(
                "SELECT SETTING_VALUE FROM file_catalog.INVENTORY_SETTING "
                "WHERE SETTING_KEY='COMMON_ROOT'"
            )).scalar() or ""
    except Exception:
        return ""


def _copy_catalog_files(engine, dialect, dest_root, fmt_filter,
                         repo_filter, overwrite, progress_cb=None):
    import shutil, os
    from pathlib import Path
    from sqlalchemy import text

    results = {"copied":0,"skipped":0,"missing":0,"errors":0,"details":[]}
    repo_clause = "AND f.REPOSITORY_ID=:repo" if repo_filter else ""
    params      = {"repo": repo_filter} if repo_filter else {}
    rows        = []

    queries = []
    if fmt_filter in ("All","LAS"):
        queries.append(("LAS",
            f"SELECT f.UWI, NULL, 'LAS', f.FILE_NAME "
            f"FROM [las_catalog].[LAS_FILE] f WHERE 1=1 {repo_clause}"))
    if fmt_filter in ("All","DLIS"):
        queries.append(("DLIS",
            f"SELECT f.UWI, NULL, 'DLIS', f.FILE_NAME "
            f"FROM [las_catalog].[DLIS_FILE] f WHERE 1=1 {repo_clause}"))
    if fmt_filter in ("All","LIS"):
        queries.append(("LIS",
            f"SELECT f.UWI, NULL, 'LIS', f.FILE_NAME "
            f"FROM [las_catalog].[LIS_FILE] f WHERE 1=1 {repo_clause}"))
    if fmt_filter in ("All","SEGY","P190"):
        seis_fmt = ""
        if fmt_filter == "SEGY": seis_fmt = "AND f.FILE_FORMAT IN ('SEGY','SEG-Y')"
        if fmt_filter == "P190": seis_fmt = "AND f.FILE_FORMAT='P190'"
        queries.append(("SEIS",
            f"SELECT NULL, f.SURVEY_NAME, f.FILE_FORMAT, f.FILE_NAME "
            f"FROM [las_catalog].[SEIS_FILE_CATALOG] f "
            f"WHERE 1=1 {seis_fmt} {repo_clause}"))

    for label, sql in queries:
        try:
            with engine.connect() as con:
                for r in con.execute(text(sql), params).fetchall():
                    rows.append({"uwi":r[0],"survey":r[1],"fmt":r[2],"file_name":r[3]})
        except Exception as e:
            results["details"].append({"File":label,"Status":f"❌ Query: {e}","Destination":""})

    total = len(rows)
    for i, row in enumerate(rows):
        fname  = str(row["file_name"] or "")
        fmt    = str(row["fmt"] or "").upper()
        uwi    = _sanitize_folder(row.get("uwi"))
        survey = _sanitize_folder(row.get("survey"))

        if progress_cb:
            progress_cb(i, total, Path(fname).name)

        src = Path(fname)
        if not src.exists():
            results["missing"] += 1
            results["details"].append({"File": src.name, "Status": "❌ Not found", "Destination": ""})
            continue

        if fmt in ("LAS","DLIS","LIS"):
            dst_dir = Path(dest_root) / "WELL_LOGS" / uwi / fmt
        else:
            dst_dir = Path(dest_root) / "SEISMIC" / survey / fmt

        os.makedirs(str(dst_dir), exist_ok=True)
        dst = dst_dir / src.name

        if dst.exists() and not overwrite:
            stem, suffix = src.stem, src.suffix
            n = 1
            while dst.exists():
                dst = dst_dir / f"{stem}_{n}{suffix}"
                n += 1

        try:
            shutil.copy2(str(src), str(dst))
            results["copied"] += 1
            results["details"].append({"File":src.name,"Status":"✅ Copied",
                                        "Destination":str(dst)})
        except Exception as e:
            results["errors"] += 1
            results["details"].append({"File":src.name,"Status":f"❌ {e}","Destination":""})

    return results


def _tab_copy_to_common(engine, dialect):
    from sqlalchemy import text
    st.markdown("#### 📦 Copy Cataloged Files to Common Location")

    common_root = _get_common_root(engine)
    if not common_root:
        st.warning("⚠️ No common root folder configured. "
                   "Set it in the **⚙️ Settings** tab first.")
        return

    st.info(f"**Destination:** `{common_root}`")

    col1, col2, col3 = st.columns(3)
    fmt_filter = col1.selectbox("Format",
        ["All","LAS","DLIS","LIS","SEGY","P190"], key="copy_common_fmt")

    try:
        with engine.connect() as con:
            repos = con.execute(text(
                "SELECT REPOSITORY_ID, REPOSITORY_NAME "
                "FROM [las_catalog].[WL_REPOSITORY] ORDER BY REPOSITORY_NAME"
            )).fetchall()
        repo_opts = {"All repositories": ""} | {r[1]: r[0] for r in repos}
    except Exception:
        repo_opts = {"All repositories": ""}

    repo_label  = col2.selectbox("Repository", list(repo_opts.keys()),
                                  key="copy_common_repo")
    repo_filter = repo_opts[repo_label]
    overwrite   = col3.checkbox("Overwrite existing", value=False,
                                 key="copy_common_ow")

    # Count
    try:
        total_count = 0
        rc = "AND REPOSITORY_ID=:repo" if repo_filter else ""
        pm = {"repo": repo_filter} if repo_filter else {}
        with engine.connect() as con:
            for tbl in ["LAS_FILE","DLIS_FILE","LIS_FILE"]:
                if fmt_filter == "All" or fmt_filter in tbl:
                    total_count += con.execute(text(
                        f"SELECT COUNT(*) FROM [las_catalog].[{tbl}] WHERE 1=1 {rc}"
                    ), pm).scalar() or 0
            sf = ""
            if fmt_filter == "SEGY": sf = "AND FILE_FORMAT IN ('SEGY','SEG-Y')"
            if fmt_filter == "P190": sf = "AND FILE_FORMAT='P190'"
            if fmt_filter in ("All","SEGY","P190"):
                total_count += con.execute(text(
                    f"SELECT COUNT(*) FROM [las_catalog].[SEIS_FILE_CATALOG] "
                    f"WHERE 1=1 {sf} {rc}"
                ), pm).scalar() or 0
        st.caption(f"**{total_count:,}** file(s) match filters.")
    except Exception:
        total_count = 0

    st.divider()
    st.code(
        common_root + "\\\n"
        "├── WELL_LOGS\\  {UWI_SANITIZED}\\  {LAS|DLIS|LIS}\\\n"
        "│               UNMATCHED\\\n"
        "└── SEISMIC\\   {SURVEY_SANITIZED}\\  {SEGY|P190}\\\n"
        "                UNMATCHED\\",
        language=None
    )

    if st.button(f"📦 Copy {total_count:,} file(s)", type="primary",
                  key="copy_common_btn", disabled=total_count == 0):
        prog = st.progress(0, text="Starting…")
        stat = st.empty()
        def _cb(done, total, fname):
            prog.progress(min(done/total if total else 0, 1.0),
                          text=f"Copying {fname}…")
            stat.caption(fname)
        result = _copy_catalog_files(
            engine, dialect, common_root, fmt_filter,
            repo_filter, overwrite, _cb
        )
        prog.empty(); stat.empty()
        if result["errors"] == 0 and result["missing"] == 0:
            st.success(f"✅ {result['copied']:,} copied · {result['skipped']:,} skipped")
        else:
            st.warning(
                f"{result['copied']:,} copied · {result['skipped']:,} skipped · "
                f"{result['missing']:,} not found · {result['errors']:,} error(s)"
            )
        if result["details"]:
            with st.expander("Details", expanded=result["errors"]>0):
                import pandas as pd
                st.dataframe(pd.DataFrame(result["details"]),
                             hide_index=True, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# End user browse portal
# ─────────────────────────────────────────────────────────────────────────────

def _tab_user_browse(engine):
    from dataview.file_catalog import page_file_browser as _fb
    st.info("👤 You are logged in as an **end user**. "
            "Browse and copy files below.")
    _fb.render(engine)



# ─────────────────────────────────────────────────────────────────────────────
# Manager Search — catalog + inventory
# ─────────────────────────────────────────────────────────────────────────────

def _tab_manager_search(engine, dialect):
    """Manager can search both the file inventory and catalog."""
    from sqlalchemy import text
    import pandas as pd

    st.markdown("#### 🔍 Search Catalog & Inventory")

    mode = st.radio("Search in", ["📦 File Inventory", "📋 File Catalog"],
                    horizontal=True, key="mgr_search_mode")

    col1, col2, col3 = st.columns(3)
    q_name   = col1.text_input("File name (partial)", key="mgr_q_name",
                                placeholder="e.g. ANADARKO")
    q_ext    = col2.selectbox("Extension",
                               ["All",".las",".dlis",".lis",".segy",".sgy",".p190"],
                               key="mgr_q_ext")
    q_status = col3.selectbox("Status",
                               ["All","UNCATALOGED","CATALOGED","SKIPPED"],
                               key="mgr_q_status") if "Inventory" in mode else None

    if st.button("🔍 Search", type="primary", key="mgr_search_btn"):
        try:
            if "Inventory" in mode:
                where = ["1=1"]
                params = {}
                if q_name:
                    where.append("FILE_NAME LIKE :nm")
                    params["nm"] = f"%{q_name}%"
                if q_ext != "All":
                    where.append("FILE_EXT=:ext")
                    params["ext"] = q_ext
                if q_status and q_status != "All":
                    where.append("CATALOG_STATUS=:st")
                    params["st"] = q_status
                with engine.connect() as con:
                    rows = con.execute(text(
                        f"SELECT TOP 500 FILE_NAME, FILE_EXT, FILE_TYPE_GROUP, "
                        f"FILE_SIZE_KB, CATALOG_STATUS, ROOT_PATH, SCAN_DATE "
                        f"FROM file_catalog.GLOBAL_FILE_CATALOG "
                        f"WHERE {' AND '.join(where)} "
                        f"ORDER BY SCAN_DATE DESC"
                    ), params).fetchall()
                df = pd.DataFrame(rows, columns=[
                    "File Name","Ext","Type","Size KB","Status","Root Path","Scan Date"
                ])
            else:
                # Catalog search — union LAS/DLIS/LIS/SEIS
                dfs = []
                for tbl, fmt, uwi_col in [
                    ("LAS_FILE","LAS","UWI"),
                    ("DLIS_FILE","DLIS","UWI"),
                    ("LIS_FILE","LIS","UWI"),
                ]:
                    where = ["1=1"]
                    params = {}
                    if q_name:
                        where.append("FILE_NAME LIKE :nm")
                        params["nm"] = f"%{q_name}%"
                    with engine.connect() as con:
                        rows = con.execute(text(
                            f"SELECT TOP 200 FILE_NAME, '{fmt}' AS FMT, "
                            f"{uwi_col} AS UWI, NULL AS SURVEY, "
                            f"FILE_SIZE_KB, CATALOG_DATE "
                            f"FROM [las_catalog].[{tbl}] "
                            f"WHERE {' AND '.join(where)} "
                            f"ORDER BY CATALOG_DATE DESC"
                        ), params).fetchall()
                    if rows:
                        dfs.append(pd.DataFrame(rows, columns=[
                            "File","Format","UWI","Survey","Size KB","Cataloged"
                        ]))
                # Seismic
                if q_ext == "All" or q_ext in (".segy",".sgy",".p190"):
                    where = ["1=1"]
                    params = {}
                    if q_name:
                        where.append("FILE_NAME LIKE :nm")
                        params["nm"] = f"%{q_name}%"
                    with engine.connect() as con:
                        rows = con.execute(text(
                            f"SELECT TOP 200 FILE_NAME, FILE_FORMAT, "
                            f"NULL, SURVEY_NAME, FILE_SIZE_KB, CATALOG_DATE "
                            f"FROM [las_catalog].[SEIS_FILE_CATALOG] "
                            f"WHERE {' AND '.join(where)} "
                            f"ORDER BY CATALOG_DATE DESC"
                        ), params).fetchall()
                    if rows:
                        dfs.append(pd.DataFrame(rows, columns=[
                            "File","Format","UWI","Survey","Size KB","Cataloged"
                        ]))
                df = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

            if df.empty:
                st.info("No results found.")
            else:
                st.caption(f"**{len(df):,}** result(s)")
                st.dataframe(df, hide_index=True, use_container_width=True)
                st.download_button(
                    "⬇ Export CSV", df.to_csv(index=False),
                    file_name="manager_search.csv", mime="text/csv",
                    key="mgr_search_csv"
                )
        except Exception as e:
            st.error(str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Manager Metrics
# ─────────────────────────────────────────────────────────────────────────────

def _tab_manager_metrics(engine, dialect):
    """Manager metrics — who cataloged what and when."""
    from sqlalchemy import text
    import pandas as pd

    st.markdown("#### 📊 Cataloging Metrics")

    try:
        # ── Overall counts ────────────────────────────────────────────────────
        with engine.connect() as con:
            inv_total = con.execute(text(
                "SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG"
            )).scalar() or 0
            inv_cat = con.execute(text(
                "SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG "
                "WHERE CATALOG_STATUS='CATALOGED'"
            )).scalar() or 0
            inv_skip = con.execute(text(
                "SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG "
                "WHERE CATALOG_STATUS='SKIPPED'"
            )).scalar() or 0
            inv_pend = inv_total - inv_cat - inv_skip

        m1,m2,m3,m4 = st.columns(4)
        m1.metric("Total Inventory", f"{inv_total:,}")
        m2.metric("Cataloged", f"{inv_cat:,}",
                  delta=f"{inv_cat/inv_total*100:.0f}%" if inv_total else None)
        m3.metric("Pending", f"{inv_pend:,}")
        m4.metric("Skipped", f"{inv_skip:,}")

        st.divider()

        # ── By assignee ───────────────────────────────────────────────────────
        st.markdown("**By Cataloger**")
        with engine.connect() as con:
            rows = con.execute(text("""
                SELECT
                    u.FULL_NAME,
                    u.ROLE,
                    COUNT(gf.GROUP_FILE_ID)                         AS Assigned,
                    SUM(CASE WHEN g.CATALOG_STATUS='CATALOGED' THEN 1 ELSE 0 END) AS Cataloged,
                    SUM(CASE WHEN g.CATALOG_STATUS='SKIPPED'   THEN 1 ELSE 0 END) AS Skipped,
                    SUM(CASE WHEN g.CATALOG_STATUS='UNCATALOGED' THEN 1 ELSE 0 END) AS Pending,
                    MAX(u.LAST_LOGIN)                               AS Last_Login
                FROM file_catalog.INVENTORY_USER u
                LEFT JOIN file_catalog.INVENTORY_ASSIGNMENT a
                    ON a.ASSIGNED_TO = u.USER_ID
                LEFT JOIN file_catalog.INVENTORY_GROUP grp
                    ON grp.GROUP_ID = a.ASSIGNMENT_ID
                LEFT JOIN file_catalog.INVENTORY_GROUP_FILE gf
                    ON gf.ASSIGNMENT_ID = a.ASSIGNMENT_ID
                LEFT JOIN file_catalog.GLOBAL_FILE_CATALOG g
                    ON g.INVENTORY_ID = gf.INVENTORY_ID
                GROUP BY u.FULL_NAME, u.ROLE
                ORDER BY Cataloged DESC
            """)).fetchall()

        if rows:
            df = pd.DataFrame(rows, columns=[
                "Name","Role","Assigned","Cataloged","Skipped","Pending","Last Login"
            ])
            st.dataframe(df, hide_index=True, use_container_width=True)

            # Bar chart — cataloged by person
            try:
                import plotly.express as px
                chart_df = df[df["Assigned"]>0].copy()
                fig = px.bar(
                    chart_df, x="Name",
                    y=["Cataloged","Pending","Skipped"],
                    title="Cataloging Progress by Team Member",
                    barmode="stack",
                    color_discrete_map={
                        "Cataloged": "#2ecc71",
                        "Pending":   "#f39c12",
                        "Skipped":   "#95a5a6",
                    },
                    text_auto=True,
                )
                fig.update_layout(
                    height=350, xaxis_title="",
                    yaxis_title="Files",
                    margin=dict(t=40,b=20,l=20,r=20),
                    legend=dict(orientation="h", y=1.1),
                )
                st.plotly_chart(fig, use_container_width=True, key="mgr_bar")
            except Exception:
                pass
        else:
            st.info("No assignment data yet.")

        st.divider()

        # ── Catalog rate over time ────────────────────────────────────────────
        st.markdown("**Cataloging Activity (last 30 days)**")
        try:
            with engine.connect() as con:
                rows = con.execute(text("""
                    SELECT
                        CONVERT(DATE, ROW_CHANGED_DATE) AS Day,
                        COUNT(*) AS Files
                    FROM file_catalog.GLOBAL_FILE_CATALOG
                    WHERE CATALOG_STATUS='CATALOGED'
                      AND ROW_CHANGED_DATE >= DATEADD(DAY,-30,GETUTCDATE())
                    GROUP BY CONVERT(DATE, ROW_CHANGED_DATE)
                    ORDER BY Day
                """)).fetchall()
            if rows:
                import plotly.express as px
                df_t = pd.DataFrame(rows, columns=["Day","Files"])
                fig  = px.area(df_t, x="Day", y="Files",
                               title="Files Cataloged Per Day",
                               color_discrete_sequence=["#3498db"])
                fig.update_layout(
                    height=280, margin=dict(t=40,b=20,l=20,r=20),
                    xaxis_title="", yaxis_title="Files cataloged",
                )
                st.plotly_chart(fig, use_container_width=True, key="mgr_timeline")
            else:
                st.info("No cataloging activity in the last 30 days.")
        except Exception as e:
            st.caption(f"Activity chart: {e}")

        # ── Login activity ────────────────────────────────────────────────────
        st.divider()
        st.markdown("**Login Activity**")
        with engine.connect() as con:
            rows = con.execute(text("""
                SELECT FULL_NAME, ROLE, ACTIVE_IND,
                       LAST_LOGIN, CREATED_DATE
                FROM file_catalog.INVENTORY_USER
                ORDER BY LAST_LOGIN DESC NULLS LAST
            """)).fetchall()
        if rows:
            df_u = pd.DataFrame(rows, columns=[
                "Name","Role","Active","Last Login","Created"
            ])
            st.dataframe(df_u, hide_index=True, use_container_width=True)

    except Exception as e:
        st.error(str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Assignment Manager tab (Admin only — MANAGER/DELEGATE)
# ─────────────────────────────────────────────────────────────────────────────

def _tab_assignment_manager(engine, dialect, user):
    """Consolidated assignment management — MANAGER/DELEGATE only."""
    from sqlalchemy import text

    if not require_role("MANAGER","DELEGATE"):
        st.warning("⛔ This tab requires MANAGER or DELEGATE role.")
        return

    st.markdown("#### 📋 Assignment Management")
    st.caption("Create, reassign and remove cataloger work assignments. "
               "Only managers and delegates can access this.")

    at  = _atbl(dialect)
    gt  = _gtbl(dialect)
    ft  = _ftbl(dialect)
    ht  = _gfc(dialect)

    users = list_users(engine, dialect)
    cats  = [u for u in users
             if u["role"] in ("CATALOGER","DELEGATE","MANAGER")
             and u["active_ind"] == "Y"]

    sub_show, sub_create, sub_reassign, sub_remove = st.tabs([
        "📋 Show", "➕ Create Assignment", "🔄 Reassign", "🗑 Remove"
    ])

    # ── Show ──────────────────────────────────────────────────────────────────
    with sub_show:
        st.markdown("**Current assignments**")
        try:
            import pandas as _pd
            with engine.connect() as con:
                rows = con.execute(text(f"""
                    SELECT
                        g.GROUP_NAME,
                        g.FILE_TYPE,
                        u.FULL_NAME       AS Cataloger,
                        a.STATUS,
                        a.FILE_COUNT,
                        SUM(CASE WHEN gc.CATALOG_STATUS='CATALOGED'  THEN 1 ELSE 0 END) AS Cataloged,
                        SUM(CASE WHEN gc.CATALOG_STATUS='SKIPPED'    THEN 1 ELSE 0 END) AS Skipped,
                        SUM(CASE WHEN gc.CATALOG_STATUS='UNCATALOGED'
                                   OR gc.CATALOG_STATUS='ASSIGNED'  THEN 1 ELSE 0 END) AS Remaining,
                        a.DUE_DATE,
                        a.ASSIGNED_DATE
                    FROM {at} a
                    JOIN {gt} g  ON g.GROUP_ID  = a.GROUP_ID
                    JOIN file_catalog.INVENTORY_USER u ON u.USER_ID = a.ASSIGNED_TO
                    LEFT JOIN {ft} gf ON gf.ASSIGNMENT_ID = a.ASSIGNMENT_ID
                    LEFT JOIN {ht} gc ON gc.INVENTORY_ID  = gf.INVENTORY_ID
                    GROUP BY g.GROUP_NAME, g.FILE_TYPE, u.FULL_NAME,
                             a.STATUS, a.FILE_COUNT, a.DUE_DATE, a.ASSIGNED_DATE
                    ORDER BY a.DUE_DATE, u.FULL_NAME
                """)).fetchall()

            if not rows:
                st.info("No active assignments.")
            else:
                df = _pd.DataFrame(rows, columns=[
                    "Group","Type","Cataloger","Status","Total",
                    "Cataloged","Skipped","Remaining","Due","Assigned"
                ])
                df["% Done"] = (
                    (df["Cataloged"] + df["Skipped"]) / df["Total"].replace(0,1) * 100
                ).round(0).astype(int).astype(str) + "%"

                st.dataframe(df, hide_index=True, use_container_width=True,
                             column_config={
                                 "% Done": st.column_config.TextColumn(width="small"),
                                 "Total":  st.column_config.NumberColumn(width="small"),
                                 "Cataloged": st.column_config.NumberColumn(width="small"),
                                 "Skipped":   st.column_config.NumberColumn(width="small"),
                                 "Remaining": st.column_config.NumberColumn(width="small"),
                             })

                # Summary metrics
                m1,m2,m3,m4 = st.columns(4)
                m1.metric("Assignments", len(df))
                m2.metric("Total files",  int(df["Total"].sum()))
                m3.metric("Cataloged",    int(df["Cataloged"].sum()))
                m4.metric("Remaining",    int(df["Remaining"].sum()))
        except Exception as e:
            st.error(str(e))

    # ── Create ────────────────────────────────────────────────────────────────
    with sub_create:
        st.markdown("**Assign inventory files to a cataloger**")
        if not cats:
            st.info("No active catalogers found.")
        else:
            cat_map = {f"{u['full_name']} ({u['email']})": u["user_id"] for u in cats}
            c1,c2,c3 = st.columns(3)
            sel_cat  = c1.selectbox("Assign to", list(cat_map.keys()),
                                     key="am_create_cat")
            cat_uid  = cat_map[sel_cat]
            ext_filter = c2.selectbox(
                "File type", ["All",".las",".dlis",".dlf",".lis",".segy",".sgy",".p190"],
                key="am_create_ext"
            )
            n_files = c3.number_input("Max files", 1, 5000, 100,
                                       key="am_create_n")

            c4,c5 = st.columns(2)
            g_name   = c4.text_input("Group name",
                                      placeholder="e.g. DLIS_Batch_1",
                                      key="am_create_gname")
            from datetime import date as _dt2, timedelta as _td2
            due_date = c5.date_input("Due date",
                                      value=_dt2.today() + _td2(days=14),
                                      key="am_create_due")

            # Count available
            try:
                ext_cl = "AND FILE_EXT=:ext" if ext_filter != "All" else ""
                pm     = {"ext": ext_filter} if ext_filter != "All" else {}
                with engine.connect() as con:
                    avail = con.execute(text(
                        f"SELECT COUNT(*) FROM {ht} "
                        f"WHERE CATALOG_STATUS='UNCATALOGED' {ext_cl}"
                    ), pm).scalar() or 0
                st.caption(f"**{avail:,}** unassigned files available.")
            except Exception:
                avail = 0

            if st.button("📋 Create Assignment", type="primary",
                          key="am_create_btn",
                          disabled=not g_name.strip() or avail == 0):
                try:
                    import uuid as _uuid
                    ext_cl = "AND FILE_EXT=:ext" if ext_filter != "All" else ""
                    pm     = {"ext": ext_filter} if ext_filter != "All" else {}
                    with engine.connect() as con:
                        inv_ids = [r[0] for r in con.execute(text(
                            f"SELECT TOP {int(n_files)} INVENTORY_ID "
                            f"FROM {ht} "
                            f"WHERE CATALOG_STATUS='UNCATALOGED' {ext_cl}"
                        ), pm).fetchall()]

                    if not inv_ids:
                        st.error("No files found.")
                    else:
                        asgn_id  = _uuid.uuid4().hex[:20].upper()
                        group_id = _uuid.uuid4().hex[:20].upper()
                        file_type = ext_filter if ext_filter != "All" else "ALL"
                        with engine.begin() as con:
                            con.execute(text(
                                f"INSERT INTO {gt} "
                                f"(GROUP_ID,GROUP_NAME,FILE_TYPE,CREATED_BY,CREATED_DATE) "
                                f"VALUES (:gid,:gn,:ft,:cb,GETUTCDATE())"
                            ), {"gid":group_id,"gn":g_name.strip(),
                                "ft":file_type,"cb":user["user_id"]})
                            con.execute(text(
                                f"INSERT INTO {at} "
                                f"(ASSIGNMENT_ID,GROUP_ID,ASSIGNED_TO,ASSIGNED_BY,"
                                f"ASSIGNED_DATE,STATUS,FILE_COUNT,DUE_DATE) "
                                f"VALUES (:aid,:gid,:to,:by,GETUTCDATE(),'OPEN',:fc,:dd)"
                            ), {"aid":asgn_id,"gid":group_id,
                                "to":cat_uid,"by":user["user_id"],
                                "fc":len(inv_ids),"dd":str(due_date)})
                            for iid in inv_ids:
                                gf_id = _uuid.uuid4().hex[:20].upper()
                                con.execute(text(
                                    f"INSERT INTO {ft} "
                                    f"(GROUP_FILE_ID,GROUP_ID,ASSIGNMENT_ID,INVENTORY_ID) "
                                    f"VALUES (:gf,:gid,:aid,:iid)"
                                ), {"gf":gf_id,"gid":group_id,
                                    "aid":asgn_id,"iid":iid})
                            ids_str = ",".join(f"'{i}'" for i in inv_ids)
                            con.execute(text(
                                f"UPDATE {ht} SET CATALOG_STATUS='ASSIGNED' "
                                f"WHERE INVENTORY_ID IN ({ids_str})"
                            ))
                        name = sel_cat.split("(")[0].strip()
                        st.success(
                            f"✅ {len(inv_ids):,} files assigned to **{name}** "
                            f"as group **{g_name}**."
                        )
                        st.rerun()
                except Exception as e:
                    st.error(str(e))

    # ── Reassign ──────────────────────────────────────────────────────────────
    with sub_reassign:
        st.markdown("**Move an assignment from one cataloger to another**")
        try:
            with engine.connect() as con:
                asgn_rows = con.execute(text(f"""
                    SELECT a.ASSIGNMENT_ID, g.GROUP_NAME, u.FULL_NAME,
                           COUNT(gf.GROUP_FILE_ID) AS files
                    FROM {at} a
                    JOIN {gt} g ON g.GROUP_ID = a.GROUP_ID
                    JOIN file_catalog.INVENTORY_USER u ON u.USER_ID = a.ASSIGNED_TO
                    LEFT JOIN {ft} gf ON gf.ASSIGNMENT_ID = a.ASSIGNMENT_ID
                    GROUP BY a.ASSIGNMENT_ID, g.GROUP_NAME, u.FULL_NAME
                    ORDER BY g.GROUP_NAME
                """)).fetchall()
        except Exception as e:
            st.error(str(e)); asgn_rows = []

        if not asgn_rows:
            st.info("No active assignments.")
        else:
            opts = {f"{r[1]} → {r[2]} ({r[3]:,} files)": r[0] for r in asgn_rows}
            sel_asgn = st.selectbox("Assignment", list(opts.keys()),
                                     key="am_reassign_sel")
            sel_aid  = opts[sel_asgn]
            cat_map  = {f"{u['full_name']} ({u['email']})": u["user_id"] for u in cats}
            new_cat  = st.selectbox("Reassign to", list(cat_map.keys()),
                                     key="am_reassign_to")
            new_uid  = cat_map[new_cat]
            if st.button("🔄 Reassign", type="primary", key="am_reassign_btn"):
                try:
                    with engine.begin() as con:
                        con.execute(text(
                            f"UPDATE {at} SET ASSIGNED_TO=:uid, "
                            f"ASSIGNED_DATE=GETUTCDATE(), ASSIGNED_BY=:by "
                            f"WHERE ASSIGNMENT_ID=:aid"
                        ), {"uid":new_uid,"by":user["user_id"],"aid":sel_aid})
                    st.success(f"✅ Reassigned to **{new_cat.split('(')[0].strip()}**.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    # ── Remove ────────────────────────────────────────────────────────────────
    with sub_remove:
        st.markdown("**Remove an assignment — files return to unassigned pool**")
        try:
            with engine.connect() as con:
                asgn_rows = con.execute(text(f"""
                    SELECT a.ASSIGNMENT_ID, g.GROUP_NAME, u.FULL_NAME,
                           COUNT(gf.GROUP_FILE_ID) AS files
                    FROM {at} a
                    JOIN {gt} g ON g.GROUP_ID = a.GROUP_ID
                    JOIN file_catalog.INVENTORY_USER u ON u.USER_ID = a.ASSIGNED_TO
                    LEFT JOIN {ft} gf ON gf.ASSIGNMENT_ID = a.ASSIGNMENT_ID
                    GROUP BY a.ASSIGNMENT_ID, g.GROUP_NAME, u.FULL_NAME
                    ORDER BY g.GROUP_NAME
                """)).fetchall()
        except Exception:
            asgn_rows = []

        if not asgn_rows:
            st.info("No active assignments.")
        else:
            opts   = {f"{r[1]} → {r[2]} ({r[3]:,} files)": r[0] for r in asgn_rows}
            sel_rm = st.selectbox("Assignment to remove", list(opts.keys()),
                                   key="am_remove_sel")
            sel_rm_id = opts[sel_rm]
            st.warning("Files will be returned to UNCATALOGED status.")

            if st.button("🗑 Remove Assignment", key="am_remove_btn"):
                st.session_state["am_remove_confirm"] = sel_rm_id

            if st.session_state.get("am_remove_confirm") == sel_rm_id:
                st.error("Confirm removal — this cannot be undone.")
                c1,c2 = st.columns(2)
                if c1.button("✅ Yes, remove", key="am_remove_yes", type="primary"):
                    try:
                        with engine.begin() as con:
                            inv_ids = [r[0] for r in con.execute(text(
                                f"SELECT INVENTORY_ID FROM {ft} "
                                f"WHERE ASSIGNMENT_ID=:aid"
                            ), {"aid":sel_rm_id}).fetchall()]
                            con.execute(text(
                                f"DELETE FROM {ft} WHERE ASSIGNMENT_ID=:aid"
                            ), {"aid":sel_rm_id})
                            con.execute(text(
                                f"DELETE FROM {at} WHERE ASSIGNMENT_ID=:aid"
                            ), {"aid":sel_rm_id})
                            if inv_ids:
                                ids_str = ",".join(f"'{i}'" for i in inv_ids)
                                con.execute(text(
                                    f"UPDATE {ht} SET CATALOG_STATUS='UNCATALOGED' "
                                    f"WHERE INVENTORY_ID IN ({ids_str})"
                                ))
                        st.session_state.pop("am_remove_confirm", None)
                        st.success("✅ Assignment removed.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
                if c2.button("❌ Cancel", key="am_remove_cancel"):
                    st.session_state.pop("am_remove_confirm", None)
                    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# Header Export / Import tab
# ─────────────────────────────────────────────────────────────────────────────

def _tab_header_export(engine, dialect):
    """
    Export stored file headers as CSV/Excel for manager review and DB update.
    Supports staged upload → review → apply to DB.
    """
    import pandas as pd
    import io
    from sqlalchemy import text

    # Ensure header tables exist
    try:
        from dataview.file_catalog.file_header_store import ensure_header_tables
        ensure_header_tables(engine)
    except Exception as e:
        st.error(f"Header tables: {e}"); return

    st.markdown("#### 📄 File Header Export & DB Update")
    st.caption(
        "Headers are captured automatically when files are cataloged. "
        "Export to CSV/Excel, edit, then upload to stage changes for DB."
    )

    tab_well, tab_seis = st.tabs(["🛢 Well Headers", "🌊 Seismic Headers"])

    # ── Well Headers ──────────────────────────────────────────────────────────
    with tab_well:
        st.markdown("**Well file header export — LAS · DLIS · LIS**")

        c1, c2, c3 = st.columns(3)
        fmt_filter = c1.selectbox("Format", ["All","LAS","DLIS","LIS"],
                                   key="hx_well_fmt")
        export_fmt = c2.selectbox("Export as", ["CSV","Excel"],
                                   key="hx_well_expfmt")

        # Count available
        try:
            with engine.connect() as con:
                n = con.execute(text(
                    "SELECT COUNT(DISTINCT FILE_NAME) "
                    "FROM file_catalog.FILE_WELL_HEADER"
                    + (" WHERE FILE_FORMAT=:fmt" if fmt_filter != "All" else "")
                ), {"fmt": fmt_filter} if fmt_filter != "All" else {}).scalar() or 0
            c3.metric("Files with stored headers", n)
        except Exception:
            n = 0

        if st.button("📥 Export Well Headers", type="primary",
                      key="hx_well_export_btn", disabled=n == 0):
            try:
                from dataview.file_catalog.file_header_store import export_well_headers
                df = export_well_headers(engine, fmt_filter)
                if df.empty:
                    st.info("No headers found.")
                else:
                    st.caption(f"**{len(df):,}** files · **{len(df.columns):,}** columns")
                    st.dataframe(df.head(20), hide_index=True,
                                 use_container_width=True)
                    if export_fmt == "CSV":
                        st.download_button(
                            "⬇ Download well_headers.csv",
                            data=df.to_csv(index=False),
                            file_name="well_headers.csv",
                            mime="text/csv",
                            key="hx_well_dl_csv"
                        )
                    else:
                        buf = io.BytesIO()
                        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                            df.to_excel(writer, index=False, sheet_name="Well Headers")
                            # Add staging template on second sheet
                            tmpl_cols = ["FILE_NAME","UWI","WELL_NAME","COMPANY",
                                         "FIELD","COUNTY","STATE","COUNTRY",
                                         "LATITUDE","LONGITUDE","KB_ELEV",
                                         "GL_ELEV","SPUD_DATE","COMP_DATE"]
                            tmpl = df.reindex(columns=tmpl_cols)
                            tmpl.to_excel(writer, index=False, sheet_name="DB Update Template")
                        buf.seek(0)
                        st.download_button(
                            "⬇ Download well_headers.xlsx",
                            data=buf.getvalue(),
                            file_name="well_headers.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            key="hx_well_dl_xlsx"
                        )
            except Exception as e:
                st.error(str(e))

        st.divider()
        st.markdown("**Upload edited headers → stage for DB update**")
        st.caption("Upload the edited 'DB Update Template' sheet. "
                   "Review staged changes then apply to dbo.WELL.")

        uploaded = st.file_uploader("Upload CSV or Excel",
                                     type=["csv","xlsx"],
                                     key="hx_well_upload")
        if uploaded:
            try:
                if uploaded.name.endswith(".csv"):
                    df_up = pd.read_csv(uploaded)
                else:
                    df_up = pd.read_excel(uploaded,
                                          sheet_name="DB Update Template")
                st.caption(f"**{len(df_up):,}** rows uploaded")
                st.dataframe(df_up.head(10), hide_index=True,
                             use_container_width=True)

                if st.button("📋 Stage for Review", type="primary",
                              key="hx_well_stage_btn"):
                    import uuid as _uuid
                    batch_id = _uuid.uuid4().hex[:20].upper()
                    rows_staged = 0
                    with engine.begin() as con:
                        for _, row in df_up.iterrows():
                            con.execute(text("""
                                INSERT INTO file_catalog.WELL_HEADER_STAGING
                                (STAGE_ID,BATCH_ID,FILE_NAME,UWI,WELL_NAME,
                                 COMPANY,FIELD,COUNTY,STATE,COUNTRY,
                                 LATITUDE,LONGITUDE,KB_ELEV,GL_ELEV,
                                 SPUD_DATE,COMP_DATE)
                                VALUES (:sid,:bid,:fn,:uwi,:wn,:co,:fld,:cn,
                                        :st,:ctry,:lat,:lon,:kb,:gl,:sp,:cd)
                            """), {
                                "sid":  _uuid.uuid4().hex[:40].upper(),
                                "bid":  batch_id,
                                "fn":   str(row.get("FILE_NAME",""))[:500],
                                "uwi":  str(row.get("UWI",""))[:40],
                                "wn":   str(row.get("WELL_NAME",""))[:255],
                                "co":   str(row.get("COMPANY",""))[:255],
                                "fld":  str(row.get("FIELD",""))[:255],
                                "cn":   str(row.get("COUNTY",""))[:255],
                                "st":   str(row.get("STATE",""))[:255],
                                "ctry": str(row.get("COUNTRY",""))[:255],
                                "lat":  str(row.get("LATITUDE",""))[:50],
                                "lon":  str(row.get("LONGITUDE",""))[:50],
                                "kb":   str(row.get("KB_ELEV",""))[:50],
                                "gl":   str(row.get("GL_ELEV",""))[:50],
                                "sp":   str(row.get("SPUD_DATE",""))[:50],
                                "cd":   str(row.get("COMP_DATE",""))[:50],
                            })
                            rows_staged += 1
                    st.success(f"✅ {rows_staged:,} rows staged — "
                               f"Batch ID: `{batch_id}`")
                    st.session_state["hx_well_batch"] = batch_id
            except Exception as e:
                st.error(str(e))

        # Show staged and apply
        try:
            with engine.connect() as con:
                staged = con.execute(text(
                    "SELECT COUNT(*) FROM file_catalog.WELL_HEADER_STAGING "
                    "WHERE STATUS='PENDING'"
                )).scalar() or 0
        except Exception:
            staged = 0

        if staged:
            st.info(f"**{staged:,}** staged rows pending review/apply.")
            if st.button(f"🚀 Apply {staged:,} staged rows to dbo.WELL",
                          key="hx_well_apply_btn", type="primary"):
                applied = errors = 0
                try:
                    with engine.connect() as con:
                        rows = con.execute(text(
                            "SELECT STAGE_ID,UWI,WELL_NAME,COMPANY,FIELD,"
                            "COUNTY,STATE,COUNTRY,LATITUDE,LONGITUDE,"
                            "KB_ELEV,GL_ELEV,SPUD_DATE,COMP_DATE "
                            "FROM file_catalog.WELL_HEADER_STAGING "
                            "WHERE STATUS='PENDING' AND UWI IS NOT NULL AND UWI<>''"
                        )).fetchall()
                    for row in rows:
                        try:
                            with engine.begin() as con:
                                con.execute(text("""
                                    UPDATE dataview.dv_well SET
                                        WELL_NAME      = COALESCE(:wn, WELL_NAME),
                                        OPERATOR       = COALESCE(:co, OPERATOR),
                                        FIELD_NAME     = COALESCE(:fld, FIELD_NAME),
                                        COUNTY_NAME    = COALESCE(:cn, COUNTY_NAME),
                                        PROVINCE_STATE = COALESCE(:st, PROVINCE_STATE),
                                        COUNTRY_NAME   = COALESCE(:ctry, COUNTRY_NAME),
                                        ROW_CHANGED_DATE = GETUTCDATE()
                                    WHERE UWI = :uwi
                                """), {
                                    "uwi":  row[1], "wn": row[2] or None,
                                    "co":   row[3] or None, "fld": row[4] or None,
                                    "cn":   row[5] or None, "st": row[6] or None,
                                    "ctry": row[7] or None,
                                })
                                con.execute(text(
                                    "UPDATE file_catalog.WELL_HEADER_STAGING "
                                    "SET STATUS='APPLIED', APPLIED_DATE=GETUTCDATE() "
                                    "WHERE STAGE_ID=:sid"
                                ), {"sid": row[0]})
                            applied += 1
                        except Exception:
                            errors += 1
                    if errors == 0:
                        st.success(f"✅ {applied:,} well records updated in DB.")
                    else:
                        st.warning(f"{applied:,} applied · {errors:,} errors.")
                except Exception as e:
                    st.error(str(e))

    # ── Seismic Headers ───────────────────────────────────────────────────────
    with tab_seis:
        st.markdown("**Seismic file header export — SEG-Y · P190**")

        c1, c2 = st.columns(2)
        fmt_filter_s = c1.selectbox("Format", ["All","SEGY","P190"],
                                     key="hx_seis_fmt")
        export_fmt_s = c2.selectbox("Export as", ["CSV","Excel"],
                                     key="hx_seis_expfmt")

        try:
            with engine.connect() as con:
                n_s = con.execute(text(
                    "SELECT COUNT(DISTINCT FILE_NAME) "
                    "FROM file_catalog.FILE_SEIS_HEADER"
                    + (" WHERE FILE_FORMAT=:fmt" if fmt_filter_s != "All" else "")
                ), {"fmt": fmt_filter_s} if fmt_filter_s != "All" else {}).scalar() or 0
            st.metric("Files with stored headers", n_s)
        except Exception:
            n_s = 0

        if st.button("📥 Export Seismic Headers", type="primary",
                      key="hx_seis_export_btn", disabled=n_s == 0):
            try:
                from dataview.file_catalog.file_header_store import export_seis_headers
                df = export_seis_headers(engine, fmt_filter_s)
                if df.empty:
                    st.info("No seismic headers found.")
                else:
                    st.caption(f"**{len(df):,}** files · **{len(df.columns):,}** columns")
                    st.dataframe(df.head(20), hide_index=True,
                                 use_container_width=True)
                    if export_fmt_s == "CSV":
                        st.download_button(
                            "⬇ Download seis_headers.csv",
                            data=df.to_csv(index=False),
                            file_name="seis_headers.csv",
                            mime="text/csv",
                            key="hx_seis_dl_csv"
                        )
                    else:
                        buf = io.BytesIO()
                        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                            df.to_excel(writer, index=False, sheet_name="Seismic Headers")
                            tmpl_cols = ["FILE_NAME","SURVEY_NAME","LINE_NAME",
                                         "Sample interval (us)","Samples per trace",
                                         "Data sample format code","ACQ_DATE",
                                         "OPERATOR","CLIENT","COUNTRY"]
                            tmpl = df.reindex(columns=tmpl_cols)
                            tmpl.to_excel(writer, index=False,
                                          sheet_name="DB Update Template")
                        buf.seek(0)
                        st.download_button(
                            "⬇ Download seis_headers.xlsx",
                            data=buf.getvalue(),
                            file_name="seis_headers.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            key="hx_seis_dl_xlsx"
                        )
            except Exception as e:
                st.error(str(e))

        st.divider()
        st.markdown("**Upload edited headers → stage for DB update**")
        uploaded_s = st.file_uploader("Upload CSV or Excel",
                                       type=["csv","xlsx"],
                                       key="hx_seis_upload")
        if uploaded_s:
            try:
                if uploaded_s.name.endswith(".csv"):
                    df_s = pd.read_csv(uploaded_s)
                else:
                    df_s = pd.read_excel(uploaded_s,
                                         sheet_name="DB Update Template")
                st.caption(f"**{len(df_s):,}** rows uploaded")
                st.dataframe(df_s.head(10), hide_index=True,
                             use_container_width=True)
                if st.button("📋 Stage Seismic", type="primary",
                              key="hx_seis_stage_btn"):
                    import uuid as _uuid2
                    batch_id = _uuid2.uuid4().hex[:20].upper()
                    staged = 0
                    with engine.begin() as con:
                        for _, row in df_s.iterrows():
                            con.execute(text("""
                                INSERT INTO file_catalog.SEIS_HEADER_STAGING
                                (STAGE_ID,BATCH_ID,FILE_NAME,SURVEY_NAME,
                                 LINE_NAME,SAMPLE_INTERVAL,SAMPLES_PER_TRACE,
                                 DATA_FORMAT_CODE,ACQ_DATE,OPERATOR,
                                 CLIENT,COUNTRY)
                                VALUES (:sid,:bid,:fn,:sv,:ln,:si,:sp,:df,
                                        :ad,:op,:cl,:ct)
                            """), {
                                "sid": _uuid2.uuid4().hex[:40].upper(),
                                "bid": batch_id,
                                "fn":  str(row.get("FILE_NAME",""))[:500],
                                "sv":  str(row.get("SURVEY_NAME",""))[:255],
                                "ln":  str(row.get("LINE_NAME",""))[:255],
                                "si":  str(row.get("Sample interval (us)",""))[:50],
                                "sp":  str(row.get("Samples per trace",""))[:50],
                                "df":  str(row.get("Data sample format code",""))[:50],
                                "ad":  str(row.get("ACQ_DATE",""))[:50],
                                "op":  str(row.get("OPERATOR",""))[:255],
                                "cl":  str(row.get("CLIENT",""))[:255],
                                "ct":  str(row.get("COUNTRY",""))[:255],
                            })
                            staged += 1
                    st.success(f"✅ {staged:,} seismic rows staged — Batch: `{batch_id}`")
            except Exception as e:
                st.error(str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Audit Log tab
# ─────────────────────────────────────────────────────────────────────────────

def _tab_audit_log(engine, dialect, user):
    """Audit log viewer — Manager/Delegate only. Last 30 days by default."""
    import pandas as pd
    from sqlalchemy import text
    from dataview.file_catalog.audit_log import ensure_audit_table, get_recent

    st.markdown("#### 📜 Audit Log")
    st.caption("Complete record of all application events — last 30 days. Append-only.")

    # Ensure table exists
    ensure_audit_table(engine)

    # ── Filters ───────────────────────────────────────────────────────────────
    EVENT_ICONS = {
        "LOGIN": "🔑", "LOGOUT": "🚪",
        "IMPERSONATE": "👤", "IMPERSONATE_EXIT": "👤",
        "CATALOG": "📥", "SKIP": "⏭",
        "ASSIGN": "📋", "REASSIGN": "🔄", "REMOVE_ASSIGN": "🗑",
        "PASSWORD_RESET": "🔑", "PASSWORD_CHANGE": "🔑",
        "EXPORT": "⬇", "PPDM_UPDATE": "🛢",
        "USER_CREATE": "➕", "USER_DEACTIVATE": "⏸", "USER_DELETE": "🗑️",
        "CRAWL": "🔍", "CLEAR": "💥",
    }

    c1, c2, c3, c4 = st.columns(4)
    days = c1.number_input("Days back", 1, 365, 30, key="al_days")
    event_filter = c2.selectbox(
        "Event type", ["All"] + sorted(EVENT_ICONS.keys()),
        key="al_event"
    )

    # User filter — load distinct users from log
    try:
        with engine.connect() as con:
            unames = [r[0] for r in con.execute(text(
                "SELECT DISTINCT USER_NAME FROM file_catalog.AUDIT_LOG "
                "WHERE USER_NAME IS NOT NULL AND USER_NAME <> '' "
                "ORDER BY USER_NAME"
            )).fetchall()]
    except Exception:
        unames = []

    user_filter = c3.selectbox("User", ["All"] + unames, key="al_user")
    max_rows = c4.number_input("Max rows", 50, 5000, 500, key="al_max")

    if st.button("🔍 Load Audit Log", type="primary", key="al_load"):
        rows = get_recent(
            engine,
            days=int(days),
            event_type=event_filter if event_filter != "All" else None,
            limit=int(max_rows)
        )
        # Filter by user name client-side
        if user_filter != "All":
            rows = [r for r in rows if r.get("user_name") == user_filter]
        st.session_state["al_results"] = rows

    rows = st.session_state.get("al_results")
    if rows is None:
        st.info("Click **Load Audit Log** to view events.")
        return

    if not rows:
        st.info("No events found for the selected filters.")
        return

    # ── Summary metrics ───────────────────────────────────────────────────────
    df = pd.DataFrame(rows)
    m1,m2,m3,m4 = st.columns(4)
    m1.metric("Total events",   len(df))
    m2.metric("Catalogs",       int((df["event_type"]=="CATALOG").sum()))
    m3.metric("Logins",         int((df["event_type"]=="LOGIN").sum()))
    m4.metric("Unique users",   int(df["user_name"].nunique()))

    # ── Format for display ────────────────────────────────────────────────────
    df["Icon"]   = df["event_type"].map(lambda e: EVENT_ICONS.get(e, "•"))
    df["Event"]  = df["Icon"] + " " + df["event_type"]
    df["When"]   = pd.to_datetime(df["event_time"]).dt.strftime("%Y-%m-%d %H:%M")
    df["Who"]    = df["user_name"].fillna("")
    df["What"]   = df["target_name"].fillna("")
    df["Notes"]  = df["notes"].fillna("")

    display_df = df[["When","Event","Who","target_type","What","Notes"]].rename(
        columns={"target_type": "Type"}
    )

    st.dataframe(
        display_df, hide_index=True, use_container_width=True,
        column_config={
            "When":  st.column_config.TextColumn(width="small"),
            "Event": st.column_config.TextColumn(width="medium"),
            "Who":   st.column_config.TextColumn(width="small"),
            "Type":  st.column_config.TextColumn(width="small"),
            "What":  st.column_config.TextColumn(width="medium"),
            "Notes": st.column_config.TextColumn(width="large"),
        }
    )

    # ── Export ────────────────────────────────────────────────────────────────
    st.download_button(
        "⬇ Export audit log CSV",
        data=display_df.to_csv(index=False),
        file_name=f"audit_log_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        key="al_export"
    )

    # ── Event breakdown chart ─────────────────────────────────────────────────
    with st.expander("📊 Event breakdown", expanded=False):
        counts = df["event_type"].value_counts().reset_index()
        counts.columns = ["Event Type","Count"]
        st.dataframe(counts, hide_index=True, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# Excel / Word extractor tab (stub — file_summarizer wired in)
# ─────────────────────────────────────────────────────────────────────────────
def _tab_office_extract(engine, dialect, user):
    import streamlit as st
    import pandas as pd
    from pathlib import Path

    st.markdown("#### 📊 Excel / Word Extractor")
    st.caption(
        "Scan a folder for Excel, Word and CSV files. "
        "Auto-detects production data, formation tops, completion parameters, "
        "well headers and more — then loads to DB."
    )

    scan_path = st.text_input(
        "Folder to scan",
        placeholder=r"C:\WellData\Reports",
        key="office_scan_path"
    )

    SUPPORTED = {".xlsx",".xls",".docx",".doc",".csv",".tsv"}

    if st.button("🔍 Scan Files", type="primary", key="office_scan_btn"):
        if not scan_path or not Path(scan_path).exists():
            st.error("Folder not found.")
        else:
            files = []
            for ext in SUPPORTED:
                files.extend(Path(scan_path).rglob(f"*{ext}"))

            if not files:
                st.info("No Office or CSV files found.")
            else:
                prog = st.progress(0, text="Summarizing…")
                results = []
                for i, fp in enumerate(files):
                    prog.progress((i+1)/len(files),
                                  text=f"Reading {fp.name}…")
                    try:
                        from dataview.file_catalog.file_summarizer import summarize
                        s = summarize(str(fp))
                        results.append({
                            "Format":   s.get("format","?"),
                            "File":     fp.name,
                            "Well":     s.get("well_name","—"),
                            "UWI":      s.get("uwi","—"),
                            "Description": s.get("description",""),
                            "PPDM":     ", ".join(s.get("ppdm_hints",[])),
                            "Warnings": len(s.get("warnings",[])),
                            "Error":    "❌" if s.get("error") else "✅",
                            "_path":    str(fp),
                            "_summary": s,
                        })
                    except Exception as e:
                        results.append({
                            "Format":"?","File":fp.name,
                            "Well":"","UWI":"","Description":"",
                            "PPDM":"","Warnings":0,
                            "Error":f"❌ {e}",
                            "_path":str(fp),"_summary":{}
                        })

                prog.empty()
                st.session_state["office_results"] = results
                st.rerun()

    if "office_results" in st.session_state:
        results = st.session_state["office_results"]
        display = [{k:v for k,v in r.items()
                    if not k.startswith("_")}
                   for r in results]
        df = pd.DataFrame(display)
        st.dataframe(df, hide_index=True, use_container_width=True)

        # Detail expander per file
        st.divider()
        st.markdown("**File detail**")
        labels = {r["File"]: r for r in results}
        sel = st.selectbox("Select file", list(labels.keys()),
                           key="office_detail_sel")
        r   = labels[sel]
        s   = r.get("_summary", {})

        # ── Header attributes — always shown ─────────────────────────────────
        kf = s.get("key_fields", {})
        _office_attrs = []

        # Flat scalar fields
        for _k, _v in kf.items():
            if not isinstance(_v, (list, dict)) and _v not in (None, "", 0):
                _office_attrs.append({"Attribute": _k, "Value": str(_v)})

        # UWI / well from top-level summary
        if s.get("uwi"):
            _office_attrs.insert(0, {"Attribute": "UWI", "Value": str(s["uwi"])})
        if s.get("well_name"):
            _office_attrs.insert(1, {"Attribute": "Well Name", "Value": str(s["well_name"])})

        if _office_attrs:
            with st.expander("📋 Extracted header attributes", expanded=True):
                st.dataframe(pd.DataFrame(_office_attrs),
                             hide_index=True, use_container_width=True)

        # ── Optional UWI match against dv_well ───────────────────────────────
        _uwi_val = s.get("uwi") or kf.get("uwi")
        if _uwi_val and engine:
            try:
                from sqlalchemy import text as _t
                with engine.connect() as _c:
                    _wrow = _c.execute(_t(
                        "SELECT uwi, well_name FROM dataview.dv_well WHERE uwi = :u"
                    ), {"u": _uwi_val}).fetchone()
                if _wrow:
                    st.success(f"✅ UWI matched in dv_well — **{_wrow[1] or '—'}**")
                else:
                    st.info(f"ℹ️ UWI `{_uwi_val}` not in dv_well — load continues without well linkage.")
            except Exception as _oe:
                st.caption(f"DB check skipped: {_oe}")

        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**Summary**")
            st.write(s.get("description",""))
            if s.get("warnings"):
                for w in s["warnings"]:
                    st.warning(w)
            if s.get("error"):
                st.error(s["error"])

        with col_b:
            st.markdown("**Sheet / table detail**")
            # Show sheet detail for Excel
            if "sheet_detail" in kf:
                for sd in kf["sheet_detail"][:6]:
                    st.info(
                        f"**{sd['sheet']}** · {sd['table_type']} · "
                        f"{sd['rows']:,} rows · → {sd['ppdm'] or '?'}"
                    )
            # Show tables for Word
            elif "tables_detail" in kf:
                for td in kf["tables_detail"][:6]:
                    st.info(
                        f"**Table {td['table_idx']+1}** · "
                        f"{td['table_type']} · {td['rows']} rows · "
                        f"→ {td['ppdm'] or '?'}"
                    )
            else:
                st.caption("No structured sheet or table data detected.")

        if s.get("ppdm_hints"):
            st.success(f"**Load target:** {', '.join(s['ppdm_hints'])}")
            st.info(
                "Full load-to-DB coming soon. "
                "Use the ETL Pipeline for CSV/Excel well data now."
            )


# ─────────────────────────────────────────────────────────────────────────────
# Directory Load tab — runs the verified Directory Loader (bulk_dir_loader.run)
# in-place. No extractor/staging/promote code is duplicated; the UWI gate,
# NEW-well create, topo FK order, idempotent promote, verify, and Force
# re-extract all come from the loader. Promote target stays `dataview`.
# ─────────────────────────────────────────────────────────────────────────────
def _tab_directory_load(engine, dialect, user):
    import streamlit as st
    from dataview.import_data import bulk_dir_loader

    if dialect != "mssql":
        st.warning(
            "The Directory Loader targets SQL Server (the `dataview` schema). "
            "Connect to the mssql DataView database to use it."
        )
        return

    _seed_loader_connection(engine)          # convenience only — inputs still override
    st.session_state.setdefault("bdl_schema", "dataview")   # unified promote target

    bulk_dir_loader.run()


def _seed_loader_connection(engine):
    """Prefill bulk_dir_loader's server/database session-state from a live engine.
    Handles host/database and odbc_connect URL forms; silent on miss; setdefault
    so a value the cataloger already typed wins."""
    import streamlit as st
    ss = st.session_state
    if ss.get("bdl_server") and ss.get("bdl_db"):
        return

    server = database = None
    try:
        url = engine.url
        server, database = url.host, url.database
        if not server or not database:
            from urllib.parse import unquote
            raw = (url.query or {}).get("odbc_connect", "")
            parts = dict(
                kv.split("=", 1)
                for kv in unquote(raw).split(";")
                if "=" in kv
            )
            server = server or parts.get("SERVER") or parts.get("Server")
            database = database or parts.get("DATABASE") or parts.get("Database")
    except Exception:
        pass

    if server:
        ss.setdefault("bdl_server", server)
    if database:
        ss.setdefault("bdl_db", database)
