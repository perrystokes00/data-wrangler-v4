"""
page_extraction_inspector.py
=============================
Extraction Inspector — a single-file, read-only window onto the consolidated
extraction layer. Pick any file, run it through the *canonical* dispatcher
(extract_core._extract_fields), and see exactly what comes back:

  · identity (uwi, well_name, operator, coords, total_depth, …)
  · the per-format `details` block
  · readiness score / label and the open issues
  · the raw extraction JSON (your diagnostic one-liner, made visual)

This is the visual twin of:
    python -c "from extract_core import _extract_fields; ..."

It touches nothing. No writes, no catalog, no promote. Optional DB cross-check
(if `engine` is passed) only does a COUNT against dataview.dv_well.

ENTRY POINT:  run(engine=None, dialect="mssql")   — same shape as page_workbench.run
DEPLOY:       Deploy-Latest page_extraction_inspector.py .   (ROOT — bare import of extract_core)
"""
import os
import json
from pathlib import Path
from datetime import datetime

import streamlit as st
import pandas as pd


def _cfg_reports() -> str:
    """The configured reports folder, for a text_input's DEFAULT value only.

    Reports are the CUSTOMER's output, not our scratch -- 1,801 of them live
    in C:\\Bulk\\reports today and that stays the default. Configurable via
    DW_REPORTS so a distribution can point it elsewhere; nothing is moved.
    """
    try:
        from dataview.core.config import DW_REPORTS
        return DW_REPORTS
    except Exception:
        return r"C:\Bulk\reports"

# ── Canonical contract ──────────────────────────────────────────────────────
# extract_core is the single source of truth for the extension universe AND the
# dispatcher. We import the SETS from there and rebuild the ext→group map locally
# (mirroring page_workbench.EXT_GROUP) so this page never drifts from the parser.
from dataview.file_catalog.extract_core import (
    _extract_fields,
    PDF_EXTS, LAS_EXTS, DLIS_EXTS, LIS_EXTS, SEGY_EXTS, P190_EXTS, SHP_EXTS,
    OFFICE_EXTS, CSV_EXTS, IMAGE_EXTS, WITSML_EXTS, JSON_LOG_EXTS, LOG_EXTS,
)

# Rebuilt from extract_core sets — keep in lock-step with page_workbench.EXT_GROUP
EXT_GROUP = {}
for e in PDF_EXTS:      EXT_GROUP[e] = "PDF"
for e in LOG_EXTS:      EXT_GROUP[e] = "Well Log"
for e in SEGY_EXTS:     EXT_GROUP[e] = "Seismic"
for e in P190_EXTS:     EXT_GROUP[e] = "Seismic"
for e in SHP_EXTS:      EXT_GROUP[e] = "Shapefile"
for e in OFFICE_EXTS:   EXT_GROUP[e] = "Office"
for e in CSV_EXTS:      EXT_GROUP[e] = "CSV / Table"
for e in IMAGE_EXTS:    EXT_GROUP[e] = "Image"
for e in WITSML_EXTS:   EXT_GROUP[e] = "WITSML"
for e in JSON_LOG_EXTS: EXT_GROUP[e] = "OSDU / JSON Well Log"

KNOWN_EXTS = set(EXT_GROUP.keys())

# Canonical identity keys, in display order. Anything top-level NOT in this list
# (and not `details`) is shown under "Other extracted fields", so a new field
# added in extract_core still surfaces here without a code change.
IDENTITY_KEYS = [
    "uwi", "well_name", "operator", "well_field", "state", "county",
    "latitude", "longitude", "total_depth", "report_type", "contractor",
]

# ─────────────────────────────────────────────────────────────────────────────
# Scoring — MIRRORS page_workbench._score / ._issues verbatim.
# NOTE for Perry: readiness scoring currently lives in THREE places —
#   page_workbench._score, modules/catalog_rules.score_file, and now here.
# Worth promoting one of them into extract_core (or catalog_rules) as the single
# owner and importing it everywhere. Replicated here only to keep this page
# standalone and zero-risk; flagged so it doesn't silently drift.
# ─────────────────────────────────────────────────────────────────────────────

def _score(fields: dict) -> tuple:
    score = 0
    if fields.get("uwi"):        score += 40
    if fields.get("well_name"):  score += 20
    if fields.get("operator"):   score += 10
    if fields.get("latitude") and fields.get("longitude"): score += 20
    if fields.get("total_depth"): score += 10
    if score >= 80:  return score, "READY"
    if score >= 60:  return score, "REVIEW"
    if score >= 30:  return score, "NEEDS_UWI"
    return score, "ATTENTION"


def _issues(fields: dict) -> list:
    out = []
    if fields.get("extract_error"):
        out.append(str(fields["extract_error"])[:200])
    if not fields.get("uwi"):        out.append("No UWI")
    if not fields.get("well_name"):  out.append("No well name")
    if not (fields.get("latitude") and fields.get("longitude")):
        out.append("No coordinates")
    return out


# Readiness → brand-aligned colour (matches page_file_manager card variants)
_READINESS_COLOR = {
    "READY":     "#22c55e",
    "REVIEW":    "#f59e0b",
    "NEEDS_UWI": "#f97316",
    "ATTENTION": "#ef4444",
}

# ─────────────────────────────────────────────────────────────────────────────
# Styling — reuses the blue/gold brand + white-card language from page_file_manager
# ─────────────────────────────────────────────────────────────────────────────

_CSS_DONE = False

def _inject_css():
    global _CSS_DONE
    if _CSS_DONE:
        return
    st.markdown("""
    <style>
    .ei-hero {
        background: linear-gradient(135deg,#1A3A6A 0%,#0D2A5A 100%);
        border: 1px solid #C8922A; border-radius: 12px;
        padding: 16px 22px; margin-bottom: 14px;
        box-shadow: 0 2px 8px rgba(200,146,42,0.20);
        display:flex; align-items:center; justify-content:space-between;
        flex-wrap:wrap; gap:10px;
    }
    .ei-hero .fname { font-size:1.05rem; font-weight:800; color:#e2e8f0;
                      word-break:break-all; }
    .ei-hero .meta  { font-size:0.78rem; color:#94a3b8; margin-top:2px; }
    .ei-hero .grp   { color:#C8922A; font-weight:700; }
    .ei-badge {
        font-size:0.92rem; font-weight:800; letter-spacing:1px;
        color:#fff; padding:6px 16px; border-radius:999px; white-space:nowrap;
    }
    .ei-badge small { font-weight:600; opacity:0.85; letter-spacing:0; }
    .ei-card {
        background:#ffffff; border:1px solid #e2e8f0; border-radius:12px;
        padding:18px 22px 14px; margin-bottom:14px;
        box-shadow:0 1px 4px rgba(0,0,0,0.06);
    }
    .ei-card-title {
        font-size:0.70rem; font-weight:700; letter-spacing:2px;
        text-transform:uppercase; color:#64748b;
        margin-bottom:12px; border-bottom:1px solid #f1f5f9; padding-bottom:7px;
    }
    .ei-kv { display:grid; grid-template-columns:170px 1fr; gap:6px 14px; }
    .ei-k  { color:#64748b; font-size:0.82rem; font-weight:600; }
    .ei-v  { color:#0f172a; font-size:0.88rem; font-family:ui-monospace,monospace;
             word-break:break-all; }
    .ei-v.miss { color:#cbd5e1; font-style:italic; font-family:inherit; }
    .ei-pill { display:inline-block; background:#fef2f2; color:#b91c1c;
               border:1px solid #fecaca; border-radius:6px;
               padding:2px 9px; margin:2px 4px 2px 0; font-size:0.80rem; }
    </style>
    """, unsafe_allow_html=True)
    _CSS_DONE = True


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(engine=None, dialect: str = "mssql"):
    _inject_css()
    st.title("🔬 Extraction Inspector")
    st.caption(
        "Run any file through the canonical extractor "
        "(`extract_core._extract_fields`) and see identity · details · "
        "readiness · raw JSON. Read-only — nothing is written."
    )

    fpath = _file_picker()
    if not fpath:
        st.info("Enter a file path above (or browse a folder) and inspect it.")
        _coverage_footer()
        return

    p = Path(fpath)
    if not p.exists():
        st.error(f"File not found on disk: `{fpath}`")
        return

    fext = p.suffix.lower()
    if fext not in KNOWN_EXTS:
        st.warning(
            f"`{fext}` is not in the recognized extension universe. "
            f"The extractor will still try, but may return nothing. "
            f"Recognized: {', '.join(sorted(KNOWN_EXTS))}"
        )

    # ── Run the canonical dispatcher ──────────────────────────────────────────
    with st.spinner(f"Extracting {p.name} …"):
        t0 = datetime.now()
        try:
            fields = _extract_fields(str(p), fext) or {}
        except Exception as e:
            st.error(f"❌ _extract_fields raised: {e}")
            st.exception(e)
            return
        elapsed = (datetime.now() - t0).total_seconds()

    _render_hero(p, fext, fields, elapsed)
    _render_identity(fields)
    _render_details(fields)
    if engine is not None:
        _render_db_crosscheck(engine, fields)
    _render_raw(p, fields)
    _coverage_footer()


# ─────────────────────────────────────────────────────────────────────────────
# File picker — two independent entry points, both feed one path
# ─────────────────────────────────────────────────────────────────────────────

def _file_picker() -> str:
    """Return a file path to inspect, or '' if none chosen this run."""
    chosen = st.session_state.get("ei_chosen_path", "")

    c_path, c_btn = st.columns([6, 1])
    typed = c_path.text_input(
        "File path",
        value=chosen,
        key="ei_path_input",
        placeholder=r"C:\Bulk\reports\ANADARKO_1H.pdf",
        label_visibility="collapsed",
    )
    go = c_btn.button("🔬 Inspect", key="ei_inspect_btn", type="primary")

    with st.expander("📁 Browse a folder"):
        d = st.text_input(
            "Folder", value=st.session_state.get("ei_dir", _cfg_reports()),
            key="ei_dir", placeholder=r"C:\Bulk\reports",
        )
        if d and os.path.isdir(d):
            try:
                names = sorted(
                    f for f in os.listdir(d)
                    if os.path.isfile(os.path.join(d, f))
                    and Path(f).suffix.lower() in KNOWN_EXTS
                )
            except Exception as e:
                names = []
                st.error(f"Cannot list folder: {e}")
            if names:
                sel = st.selectbox(
                    f"{len(names)} recognized file(s)", names, key="ei_browse_sel"
                )
                if st.button("Inspect selected", key="ei_browse_go"):
                    st.session_state["ei_chosen_path"] = os.path.join(d, sel)
                    st.rerun()
            else:
                st.caption("No recognized file types in this folder.")
        elif d:
            st.caption("Folder not found.")

    if go and typed:
        st.session_state["ei_chosen_path"] = typed
        return typed
    # Persisted choice (from browse) survives the rerun
    return st.session_state.get("ei_chosen_path", "")


# ─────────────────────────────────────────────────────────────────────────────
# Renderers
# ─────────────────────────────────────────────────────────────────────────────

def _render_hero(p: Path, fext: str, fields: dict, elapsed: float):
    score, label = _score(fields)
    color = _READINESS_COLOR.get(label, "#64748b")
    group = EXT_GROUP.get(fext, "Unknown")
    rtype = fields.get("report_type") or "—"
    size_kb = p.stat().st_size / 1024 if p.exists() else 0

    st.markdown(f"""
    <div class="ei-hero">
      <div>
        <div class="fname">{p.name}</div>
        <div class="meta">
          <span class="grp">{group}</span> · <code>{fext}</code> ·
          extractor: <b style="color:#e2e8f0;">{rtype}</b> ·
          {size_kb:,.0f} KB · {elapsed:.2f}s
        </div>
      </div>
      <div class="ei-badge" style="background:{color};">
        {label} <small>· {score}/100</small>
      </div>
    </div>
    """, unsafe_allow_html=True)

    issues = _issues(fields)
    if issues:
        pills = "".join(f'<span class="ei-pill">⚠ {i}</span>' for i in issues)
        st.markdown(pills, unsafe_allow_html=True)


def _fmt_val(v):
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    if isinstance(v, float):
        return f"{v:,.6f}".rstrip("0").rstrip(".")
    return str(v)


def _render_identity(fields: dict):
    rows = []
    for k in IDENTITY_KEYS:
        rows.append((k, _fmt_val(fields.get(k))))

    body = ['<div class="ei-card"><div class="ei-card-title">Identity</div><div class="ei-kv">']
    for k, v in rows:
        if v is None:
            body.append(f'<div class="ei-k">{k}</div><div class="ei-v miss">— not found —</div>')
        else:
            body.append(f'<div class="ei-k">{k}</div><div class="ei-v">{v}</div>')
    body.append("</div></div>")
    st.markdown("".join(body), unsafe_allow_html=True)

    # Any top-level field that isn't identity and isn't the details block
    extra = {
        k: v for k, v in fields.items()
        if k not in IDENTITY_KEYS and k != "details"
        and not k.startswith("_") and _fmt_val(v) is not None
    }
    if extra:
        with st.expander(f"Other extracted fields ({len(extra)})"):
            st.dataframe(
                pd.DataFrame(
                    [(k, _fmt_val(v)) for k, v in extra.items()],
                    columns=["field", "value"],
                ),
                hide_index=True, use_container_width=True,
            )


def _render_details(fields: dict):
    details = fields.get("details")
    if not details:
        st.markdown(
            '<div class="ei-card"><div class="ei-card-title">Format details</div>'
            '<div class="ei-v miss">No per-format details block returned.</div></div>',
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        '<div class="ei-card"><div class="ei-card-title">Format details</div></div>',
        unsafe_allow_html=True,
    )

    if not isinstance(details, dict):
        st.json(details)
        return

    scalars = {}
    for k, v in details.items():
        if isinstance(v, list):
            n = len(v)
            label = f"📋 {k} — {n} item(s)"
            if n:
                with st.expander(label):
                    try:
                        st.dataframe(
                            pd.DataFrame(v).fillna(""),
                            hide_index=True, use_container_width=True,
                        )
                    except Exception:
                        st.json(v)
            else:
                st.caption(f"{k}: empty")
        elif isinstance(v, dict):
            with st.expander(f"🔧 {k} ({len(v)} keys)"):
                st.json(v)
        else:
            scalars[k] = v

    if scalars:
        st.dataframe(
            pd.DataFrame(
                [(k, _fmt_val(v)) for k, v in scalars.items()],
                columns=["detail", "value"],
            ),
            hide_index=True, use_container_width=True,
        )


def _render_db_crosscheck(engine, fields: dict):
    """Best-effort, fully guarded: is this UWI already promoted to dv_well?"""
    uwi = fields.get("uwi")
    if not uwi:
        return
    from sqlalchemy import text as _t
    try:
        with engine.connect() as con:
            n = con.execute(
                _t("SELECT COUNT(*) FROM dataview.dv_well WHERE uwi = :u"),
                {"u": str(uwi)},
            ).scalar()
    except Exception:
        return  # schema/connection mismatch — stay silent, never break the page
    if n and n > 0:
        st.success(f"✓ UWI `{uwi}` already present in `dataview.dv_well` ({n} row).")
    else:
        st.info(f"UWI `{uwi}` not yet in `dataview.dv_well` (not promoted).")


def _render_raw(p: Path, fields: dict):
    blob = json.dumps(fields, indent=2, default=str)
    with st.expander("🧪 Raw extraction JSON (the diagnostic one-liner, visualized)"):
        st.code(blob, language="json")
        st.download_button(
            "⬇ Download JSON",
            data=blob,
            file_name=f"{p.stem}_extract.json",
            mime="application/json",
            key="ei_raw_dl",
        )


def _coverage_footer():
    groups = {}
    for ext, grp in sorted(EXT_GROUP.items()):
        groups.setdefault(grp, []).append(ext)
    with st.expander(f"📦 Recognized formats ({len(EXT_GROUP)} extensions)"):
        st.dataframe(
            pd.DataFrame(
                [(g, ", ".join(sorted(exts))) for g, exts in sorted(groups.items())],
                columns=["format group", "extensions"],
            ),
            hide_index=True, use_container_width=True,
        )
        st.caption(
            "Per-format *verification* state (the test_pipeline.py matrix) belongs "
            "on the Coverage / Verification dashboard — next build if you want it."
        )
