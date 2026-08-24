"""
modules/inv_workbench.py
========================
Viewer and cataloger for the My Work tab.
Dispatches to the right viewer/cataloger based on file extension.
Marks file CATALOGED or SKIPPED in GLOBAL_FILE_CATALOG.
"""

import streamlit as st
import pandas as pd
from pathlib import Path
from sqlalchemy import text

LAS_EXTS  = {".las"}
DLIS_EXTS = {".dlis", ".dlf", ".dis"}
LIS_EXTS  = {".lis"}
SEGY_EXTS = {".segy", ".sgy", ".seg"}
P190_EXTS = {".p190", ".p1", ".p90", ".pa90"}
ALL_VIEWABLE = LAS_EXTS | DLIS_EXTS | LIS_EXTS | SEGY_EXTS | P190_EXTS


def _ext(fp): return Path(fp).suffix.lower()

def _gfc(dialect):
    if dialect == "oracle":    return "FILE_CATALOG_GLOBAL_FILE_CATALOG"
    if dialect == "snowflake": return '"FILE_CATALOG"."GLOBAL_FILE_CATALOG"'
    return "file_catalog.GLOBAL_FILE_CATALOG"

def _gff(dialect):
    if dialect == "oracle":    return "FILE_CATALOG_INVENTORY_GROUP_FILE"
    if dialect == "snowflake": return '"FILE_CATALOG"."INVENTORY_GROUP_FILE"'
    return "file_catalog.INVENTORY_GROUP_FILE"

def _now(dialect):
    return {"mssql":"GETDATE()","oracle":"SYSTIMESTAMP",
            "snowflake":"CURRENT_TIMESTAMP()"}.get(dialect,"GETDATE()")


def mark_cataloged(engine, dialect, inventory_id, group_file_id=None):
    with engine.begin() as conn:
        conn.execute(text(
            f"UPDATE {_gfc(dialect)} SET CATALOG_STATUS='CATALOGED' "
            f"WHERE INVENTORY_ID=:iid"
        ), {"iid": inventory_id})
        if group_file_id:
            try:
                conn.execute(text(
                    f"UPDATE {_gff(dialect)} SET CATALOGED_IND='Y', "
                    f"CATALOGED_DATE={_now(dialect)} WHERE GROUP_FILE_ID=:gfid"
                ), {"gfid": group_file_id})
            except Exception:
                pass  # column may not exist yet — handled by DDL migration


def mark_skipped(engine, dialect, inventory_id, group_file_id, reason):
    with engine.begin() as conn:
        conn.execute(text(
            f"UPDATE {_gfc(dialect)} SET CATALOG_STATUS='SKIPPED' "
            f"WHERE INVENTORY_ID=:iid"
        ), {"iid": inventory_id})
        if group_file_id:
            try:
                conn.execute(text(
                    f"UPDATE {_gff(dialect)} SET SKIPPED_IND='Y', "
                    f"SKIP_REASON=:reason WHERE GROUP_FILE_ID=:gfid"
                ), {"reason": reason, "gfid": group_file_id})
            except Exception:
                pass


def _get_repos(engine):
    try:
        from dataview.file_catalog.las_catalog import list_repositories
        repos = list_repositories(engine)
        if not repos.empty:
            return {"(none — assign later)": ""} | {
                f"{r['REPOSITORY_NAME']} ({r['BASE_PATH']})": r["REPOSITORY_ID"]
                for _, r in repos.iterrows()
            }
    except Exception:
        pass
    return {"(none)": ""}


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

# ── Card CSS injected once per session ───────────────────────────────────────
_CARD_CSS = """
<style>
.wb-card {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 20px 24px 18px;
    margin-bottom: 14px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}
.wb-card-title {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #64748b;
    margin-bottom: 12px;
    border-bottom: 1px solid #f1f5f9;
    padding-bottom: 7px;
}
</style>
"""

def _card(title: str = ""):
    """Inject card CSS and render a card header."""
    st.markdown(_CARD_CSS, unsafe_allow_html=True)
    st.markdown(
        f'<div class="wb-card"><div class="wb-card-title">{title}</div>',
        unsafe_allow_html=True
    )

def _card_end():
    st.markdown('</div>', unsafe_allow_html=True)


def render_file_workbench(engine, dialect, inventory_id, file_path,
                           catalog_status, group_file_id=None, context_key=""):
    ext  = _ext(file_path)
    ukey = f"{context_key}_{inventory_id[:10]}"

    if catalog_status == "CATALOGED":
        st.success("✅ Already cataloged.")
        return
    if catalog_status == "SKIPPED":
        st.warning("⏭ Skipped.")
        return

    if not Path(file_path).exists():
        st.warning(f"⚠️ File not found: `{file_path}`")
        # Skip card
        _card("⏭ Skip File")
        _render_skip(engine, dialect, inventory_id, group_file_id,
                     ukey, pre_reason="File not found on disk")
        _card_end()
        return

    # ── Card 1: Header ────────────────────────────────────────────────────────
    _card("📄 File Header")
    if ext in LAS_EXTS:
        _cataloger_las(engine, dialect, inventory_id, file_path, group_file_id, ukey)
    elif ext in DLIS_EXTS:
        _cataloger_dlis(engine, dialect, inventory_id, file_path, group_file_id, ukey)
    elif ext in LIS_EXTS:
        _cataloger_lis(engine, dialect, inventory_id, file_path, group_file_id, ukey)
    elif ext in SEGY_EXTS:
        _cataloger_segy(engine, dialect, inventory_id, file_path, group_file_id, ukey)
    elif ext in P190_EXTS:
        _cataloger_p190(engine, dialect, inventory_id, file_path, group_file_id, ukey)
    else:
        if st.button("✅ Mark Cataloged", key=f"wb_manual_{ukey}"):
            mark_cataloged(engine, dialect, inventory_id, group_file_id)
            st.rerun()
    _card_end()

    # ── Card 2: Raw file view ─────────────────────────────────────────────────
    if ext in (DLIS_EXTS | LIS_EXTS | SEGY_EXTS | P190_EXTS):
        _card("📖 Raw File View")
        with st.expander("View decoded content", expanded=False):
            if ext in DLIS_EXTS:   _viewer_dlis(file_path, ukey)
            elif ext in LIS_EXTS:  _viewer_lis(file_path, ukey)
            elif ext in SEGY_EXTS: _viewer_segy(file_path, ukey)
            elif ext in P190_EXTS: _viewer_p190(file_path, ukey)
        _card_end()

    # ── Card 3: Skip ──────────────────────────────────────────────────────────
    _card("⏭ Skip File")
    _render_skip(engine, dialect, inventory_id, group_file_id, ukey)
    _card_end()


SKIP_REASONS = [
    "— select a reason —",
    "Corrupt / unreadable file",
    "Duplicate file",
    "Wrong format",
    "Wrong well / no UWI match",
    "Empty file",
    "Out of scope",
    "Already in PPDM",
    "Other",
]

def _render_skip(engine, dialect, inventory_id, group_file_id,
                 ukey, pre_reason=""):
    # Pre-seed selectbox if a reason is provided (e.g. file not found)
    _sel_key = f"wb_skip_sel_{ukey}"
    if pre_reason and _sel_key not in st.session_state:
        match = next((r for r in SKIP_REASONS if r.lower() in pre_reason.lower()), "Other")
        st.session_state[_sel_key] = match

    selected = st.selectbox("Skip reason", SKIP_REASONS,
                            key=_sel_key)
    if selected == "Other":
        other = st.text_input("Specify reason",
                              key=f"wb_skip_other_{ukey}",
                              placeholder="Describe why this file is being skipped")
        reason = other.strip()
    elif selected == "— select a reason —":
        reason = ""
    else:
        reason = selected

    if st.button("⏭ Skip", key=f"wb_skip_{ukey}", type="secondary"):
        if not reason:
            st.error("Select or enter a reason before skipping.")
        else:
            mark_skipped(engine, dialect, inventory_id, group_file_id, reason)
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# Viewers
# ─────────────────────────────────────────────────────────────────────────────

def _viewer_las(file_path, ukey):
    """Show raw LAS header text — everything before the ~A data section."""
    try:
        with open(file_path, "r", errors="replace") as f:
            text = f.read()
        a_idx = text.upper().find("\n~A")
        header = text[:a_idx].strip() if a_idx > 0 else text
        st.code(header, language=None)
    except Exception as e:
        st.error(f"LAS viewer: {e}")

def _viewer_dlis(file_path, ukey):
    """Decode DLIS binary and show human-readable text."""
    try:
        import warnings
        from dlisio import dlis
        lines = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with dlis.load(file_path) as lfs:
                for lf_idx, lf in enumerate(lfs):
                    lines.append(f"=== Logical File {lf_idx+1} ===")
                    for o in lf.origins:
                        lines.append(f"Origin: {o.name}")
                        for attr in ("well_name","field_name","company","country",
                                     "creation_time","producer_name","run_nr"):
                            v = getattr(o, attr, None)
                            if v:
                                lines.append(f"  {attr}: {v}")
                    ch_list = list(lf.channels)
                    lines.append(f"\nChannels ({len(ch_list)}):")
                    for ch in ch_list:
                        lines.append(
                            f"  {ch.name:<20s} unit={ch.units or '—':<8s} dim={ch.dimension}"
                        )
                    params = list(lf.parameters)
                    if params:
                        lines.append(f"\nParameters ({len(params)}):")
                        for p in params[:50]:
                            lines.append(f"  {p.name:<20s} = {p.values}")
        st.code("\n".join(lines), language=None)
    except Exception as e:
        st.error(f"DLIS viewer: {e}")

def _viewer_lis(file_path, ukey):
    """Decode LIS binary and show human-readable text."""
    try:
        import warnings
        from dlisio import lis
        lines = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with lis.load(file_path) as lfs:
                if not lfs:
                    st.warning("No logical files found in LIS file.")
                    return
                lf = lfs[0]
                try:
                    lines.append("=== Wellsite Data ===")
                    for rec in lf.wellsite_data():
                        for c in rec.components():
                            mnem = getattr(c, "mnemonic", "")
                            val  = getattr(c, "component", "")
                            if mnem:
                                lines.append(f"  {str(mnem):<12s} = {val}")
                except Exception:
                    lines.append("  (no wellsite data)")

                try:
                    specs = lf.data_format_specs()
                    if specs:
                        lines.append(f"\n=== Curves ({len(specs)} spec(s)) ===")
                        for spec in specs:
                            for ch in spec.entries:
                                mnem  = str(getattr(ch, "mnemonic", "?"))
                                units = str(getattr(ch, "units", "—"))
                                lines.append(f"  {mnem:<12s} unit={units:<8s}")
                except Exception:
                    lines.append("  (could not read curve specs)")

        if lines:
            st.code("\n".join(lines), language=None)
        else:
            st.info("No readable header data found in this LIS file.")
    except Exception as e:
        st.warning(f"⚠️ LIS binary decode failed: {e}")
        st.caption("This file may use an unsupported LIS variant. "
                   "Try opening it in a dedicated LIS viewer.")

def _viewer_segy(file_path, ukey):
    """Show decoded SEG-Y EBCDIC + binary header. Reads headers only — no trace scan."""
    try:
        import segyio
        with segyio.open(file_path, ignore_geometry=True, strict=False) as f:
            ebcdic  = f.text[0].decode("cp037", errors="replace")
            lines   = [ebcdic[i:i+80].rstrip() for i in range(0, len(ebcdic), 80)]
            bin_hdr = {str(k): int(v) for k,v in dict(f.bin).items() if int(v) != 0}
        st.code("\n".join(lines), language=None)
        with st.expander("Binary header fields"):
            st.json(bin_hdr)
    except Exception as segyio_err:
        # ── Fallback: read raw bytes and decode EBCDIC manually ──────────────
        # Works on any SEG-Y regardless of geometry or trace format
        try:
            with open(file_path, "rb") as f:
                raw = f.read(3600)   # textual header = first 3200 bytes
            text_hdr = raw[:3200]
            # Try EBCDIC (cp037) first, then ASCII
            try:
                ebcdic = text_hdr.decode("cp037", errors="replace")
            except Exception:
                ebcdic = text_hdr.decode("ascii", errors="replace")
            lines = [ebcdic[i:i+80].rstrip() for i in range(0, len(ebcdic), 80)]
            # Only show non-empty lines
            lines = [l for l in lines if l.strip()]
            st.caption(f"⚠️ segyio failed ({segyio_err}) — showing raw EBCDIC decode")
            st.code("\n".join(lines) if lines else "(empty header)", language=None)

            # Parse binary header fields (bytes 3200-3600)
            if len(raw) >= 3600:
                import struct
                bin_hdr_raw = raw[3200:3600]
                # Key binary header fields (byte offset, format, name)
                fields = [
                    (0,  ">i", "Job ID"),
                    (4,  ">i", "Line number"),
                    (8,  ">i", "Reel number"),
                    (12, ">h", "Traces per ensemble"),
                    (16, ">h", "Aux traces per ensemble"),
                    (20, ">h", "Sample interval (us)"),
                    (24, ">h", "Sample interval original (us)"),
                    (28, ">h", "Samples per trace"),
                    (30, ">h", "Samples per trace original"),
                    (32, ">h", "Data sample format code"),
                ]
                parsed = {}
                for offset, fmt, name in fields:
                    try:
                        sz   = struct.calcsize(fmt)
                        val  = struct.unpack(fmt, bin_hdr_raw[offset:offset+sz])[0]
                        if val != 0:
                            parsed[name] = val
                    except Exception:
                        pass
                if parsed:
                    with st.expander("Binary header fields"):
                        st.json(parsed)
        except Exception as e2:
            st.error(f"SEG-Y viewer failed: {e2}")

def _viewer_p190(file_path, ukey):
    """Show P190 raw text — H header records first, then all lines."""
    try:
        with open(file_path, "r", errors="replace") as f:
            lines = f.readlines()
        header = [l.rstrip() for l in lines if l.startswith("H")]
        body   = [l.rstrip() for l in lines[:500]]
        if header:
            st.markdown("**Header (H records):**")
            st.code("\n".join(header), language=None)
            with st.expander("All records (first 500 lines)"):
                st.code("\n".join(body), language=None)
        else:
            st.code("\n".join(body), language=None)
    except Exception as e:
        st.error(f"P190 viewer: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Catalogers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_well_names(engine) -> list[dict]:
    """Return [{uwi, well_name}] from PPDM WELL table."""
    try:
        from dataview.file_catalog.las_loader import fetch_ppdm_uwis
        return fetch_ppdm_uwis(engine)
    except Exception:
        pass
    try:
        from sqlalchemy import text as _t
        with engine.connect() as conn:
            rows = conn.execute(_t(
                "SELECT UWI, WELL_NAME FROM dbo.WELL "
                "WHERE WELL_NAME IS NOT NULL AND UWI IS NOT NULL"
            )).fetchall()
        return [{"UWI": r[0], "WELL_NAME": r[1]} for r in rows]
    except Exception:
        return []


def _fuzzy_match_well_name(candidate: str, wells: list[dict],
                            cutoff: float = 0.55) -> dict | None:
    """Fuzzy match candidate against WELL_NAME. Returns best hit or None."""
    if not candidate or not wells:
        return None
    import difflib
    names = [w.get("WELL_NAME","") for w in wells if w.get("WELL_NAME")]
    matches = difflib.get_close_matches(candidate, names, n=1, cutoff=cutoff)
    if not matches:
        return None
    best = matches[0]
    uwi  = next((w["UWI"] for w in wells if w.get("WELL_NAME") == best), "")
    score = difflib.SequenceMatcher(None, candidate.lower(), best.lower()).ratio()
    return {"uwi": uwi, "well_name": best, "score": round(score, 2)}


def _cataloger_las(engine, dialect, inventory_id, file_path, group_file_id, ukey):
    try:
        from dataview.file_catalog.las_catalog import parse_las_header, catalog_file
    except ImportError as e:
        st.error(f"las_catalog not available: {e}"); return

    repos = _get_repos(engine)
    # Clear stale repo key if value is not a valid label
    if f"wb_repo_{ukey}" in st.session_state:
        if st.session_state[f"wb_repo_{ukey}"] not in repos:
            st.session_state.pop(f"wb_repo_{ukey}", None)
    repo_label = st.selectbox("Repository", list(repos.keys()), key=f"wb_repo_{ukey}")
    repo_id = repos.get(repo_label, "")

    # ── Auto-load header on first render (no button needed) ──────────────────
    if f"wb_raw_{ukey}" not in st.session_state:
        with st.spinner("Reading header…"):
            try:
                import lasio
                from dataview.file_catalog.las_reader import read_las
                las = read_las(file_path, ignore_header_errors=True)
                raw_sections = {}
                for section_name, items in [
                    ("VERSION", las.version),
                    ("WELL",    las.well),
                    ("CURVES",  las.curves),
                    ("PARAMS",  las.params),
                ]:
                    raw_sections[section_name] = [
                        {"mnemonic": str(i.mnemonic), "unit": str(i.unit),
                         "value": str(i.value), "descr": str(i.descr)}
                        for i in items
                    ]
                st.session_state[f"wb_raw_{ukey}"] = raw_sections
                # Extract key fields for UWI resolution
                well_items = {r["mnemonic"].upper(): r["value"]
                              for r in raw_sections.get("WELL", [])}
                hdr_data = {
                    "uwi":        (well_items.get("UWI") or well_items.get("API") or "").strip(),
                    "well_name":  (well_items.get("WELL") or well_items.get("WELL_NAME") or "").strip(),
                    "version":    next((r["value"] for r in raw_sections.get("VERSION",[])
                                       if r["mnemonic"].upper()=="VERS"), ""),
                    "top_depth":  (well_items.get("STRT") or ""),
                    "base_depth": (well_items.get("STOP") or ""),
                    "curve_count": len(raw_sections.get("CURVES",[])),
                }
                st.session_state[f"wb_hdr_data_{ukey}"] = hdr_data
            except Exception as e:
                st.warning(f"Could not read header: {e}")

    hdr = st.session_state.get(f"wb_hdr_data_{ukey}", {})

    # ── UWI resolution: filename > header > fuzzy match ───────────────────────
    from_filename = _extract_uwi_from_filename(file_path)
    from_header   = hdr.get("uwi", "").strip()
    override      = st.session_state.get(f"wb_uwi_override_{ukey}")

    widget_key = f"wb_uwi_{ukey}"
    if widget_key not in st.session_state:
        best = override or from_filename or from_header
        if not best:
            candidate = hdr.get("well_name","").strip() or _guess_name_from_filename(file_path)
            if candidate:
                match = _fuzzy_match_well_name(candidate, _fetch_well_names(engine))
                if match:
                    best = match["uwi"]
                    st.session_state[f"wb_uwi_match_{ukey}"] = match
        st.session_state[widget_key] = best or ""

    # ── Raw LAS header (above UWI) ────────────────────────────────────────────
    with st.expander("📄 Raw LAS Header", expanded=True):
        _viewer_las(file_path, ukey)

    with st.expander("📈 Log Curves (optional)", expanded=False):
        try:
            from page_std_catalog import _render_curve_plot
            _render_curve_plot(file_path, pd.DataFrame(),
                               key_prefix=f"las_{ukey}")
        except Exception as e:
            st.error(f"Curve viewer: {e}")

    # ── Card: UWI & PPDM ──────────────────────────────────────────────────────
    _card("🔑 Well Identification")
    match_info = st.session_state.get(f"wb_uwi_match_{ukey}")
    if from_filename:
        st.caption(f"📄 UWI from filename: `{from_filename}`")
    elif from_header:
        st.caption(f"📋 UWI from header")
    elif match_info:
        st.caption(f"🔍 Fuzzy match: **{match_info['well_name']}** ({match_info['score']:.0%})")

    uwi = st.text_input("UWI", key=widget_key)

    col_chk, col_status = st.columns([1, 3])
    with col_chk:
        if st.button("🔎 Check PPDM", key=f"wb_chk_{ukey}"):
            if uwi.strip():
                try:
                    from dataview.file_catalog.las_catalog import well_exists
                    st.session_state[f"wb_ppdm_ok_{ukey}"] = (
                        well_exists(engine, uwi.strip()), uwi.strip()
                    )
                except Exception:
                    pass
    with col_status:
        chk = st.session_state.get(f"wb_ppdm_ok_{ukey}")
        if chk:
            ok, checked = chk
            if ok:
                st.success(f"✅ **{checked}** found in PPDM.")
            else:
                st.warning(f"⚠️ **{checked}** not in PPDM well header.")
    _card_end()

    # ── Card: Catalog action ───────────────────────────────────────────────────
    _card("📥 Catalog")
    # ── Step 4: Catalog — metadata snapshot only, no curve data ──────────────
    if st.button("📥 Catalog LAS", type="primary", key=f"wb_cat_{ukey}"):
        if not uwi.strip(): st.error("UWI required."); return
        try:
            from dataview.file_catalog.file_header_catalog import (
                catalog_file_header, ensure_header_schema)
            if "wb_schema_ok" not in st.session_state:
                ensure_header_schema(engine, dialect)
                st.session_state["wb_schema_ok"] = True
            match_info = st.session_state.get(f"wb_uwi_match_{ukey}", {})
            fhid = catalog_file_header(
                engine=engine, dialect=dialect,
                file_path=file_path, inventory_id=inventory_id,
                matched_uwi=uwi.strip(),
                match_method="FILENAME" if from_filename else
                             "HEADER"   if from_header   else
                             "FUZZY"    if match_info     else "MANUAL",
                match_score=match_info.get("score", 1.0) if match_info else 1.0,
                guessed_uwi=from_filename,
                guessed_well=hdr.get("well_name","") if hdr else "",
                guessed_survey="",
                cataloged_by=st.session_state.get("inv_user_id",""),
            )
            mark_cataloged(engine, dialect, inventory_id, group_file_id)
            st.success(f"✅ Header cataloged — {uwi.strip()}")
            st.rerun()
        except Exception as e:
            st.error(str(e))
    _card_end()


# ─────────────────────────────────────────────────────────────────────────────
# DLIS / LIS well matching helpers
# ─────────────────────────────────────────────────────────────────────────────

def _guess_name_from_filename(file_path: str) -> str:
    """Strip extension/underscores/leading numbers from filename stem."""
    import re
    from pathlib import Path
    stem = Path(file_path).stem
    name = re.sub(r"[_\-]+", " ", stem).strip()
    name = re.sub(r"^\d+\s*", "", name).strip()
    return name


def _extract_uwi_from_filename(file_path: str) -> str:
    """Try to extract a UWI/API number from the filename stem."""
    import re
    from pathlib import Path
    stem = Path(file_path).stem
    patterns = [
        re.compile(r"(\d{2}[-_]\d{3}[-_]\d{5}[-_]\d{4})"),
        re.compile(r"(\d{2}[-_]\d{3}[-_]\d{5})"),
        re.compile(r"(\d{14})"),
        re.compile(r"(42[-_]\d{3}[-_]\d{5}\d*)"),
    ]
    for variant in [stem, stem.replace("_", "-")]:
        for pat in patterns:
            m = pat.search(variant)
            if m:
                return m.group(1).replace("_", "-")
    return ""


def _extract_survey_from_filename(file_path: str) -> str:
    """Try to extract a survey name from the filename stem."""
    import re
    from pathlib import Path
    stem = Path(file_path).stem
    patterns = [
        re.compile(r"(?i)([A-Za-z0-9]+[-_]?3[Dd])"),
        re.compile(r"(?i)([A-Za-z0-9]+[-_]?2[Dd])"),
        re.compile(r"(?i)(survey[-_]?[A-Za-z0-9]+)"),
    ]
    for pat in patterns:
        m = pat.search(stem)
        if m:
            return m.group(0)
    return stem


def _run_dlis_well_match(engine, file_path: str, hdr: dict, ukey: str):
    """
    Try to find a PPDM UWI for a DLIS/LIS file:
    1. Well name from header (well_name, well_id)
    2. Filename stem
    3. difflib fuzzy match against PPDM WELL.WELL_NAME
    Stores result in session_state[wb_match_{ukey}].
    """
    import difflib

    # Build candidate list — UWI from filename takes priority
    candidates = []
    filename_uwi = _extract_uwi_from_filename(file_path)
    if filename_uwi:
        candidates.append(filename_uwi)
    for key in ("well_name", "well_id"):
        v = (hdr.get(key) or "").strip()
        if v and v not in candidates:
            candidates.append(v)
    filename_guess = _guess_name_from_filename(file_path)
    if filename_guess and filename_guess not in candidates:
        candidates.append(filename_guess)

    # Fetch PPDM wells
    wells = []
    try:
        from dataview.file_catalog.las_loader import fetch_ppdm_uwis
        wells = fetch_ppdm_uwis(engine)
    except Exception:
        pass

    # Fuzzy match against WELL_NAME for each candidate
    match = None
    matched_on = ""
    if wells:
        well_names = [w["WELL_NAME"] for w in wells if w.get("WELL_NAME")]
        for cand in candidates:
            hits = difflib.get_close_matches(cand, well_names, n=1, cutoff=0.55)
            if hits:
                best_name = hits[0]
                best_well = next(w for w in wells if w["WELL_NAME"] == best_name)
                score = difflib.SequenceMatcher(
                    None, cand.lower(), best_name.lower()
                ).ratio()
                match = {
                    "uwi":       best_well["UWI"],
                    "well_name": best_name,
                    "score":     round(score, 2),
                }
                matched_on = cand
                break

    st.session_state[f"wb_match_{ukey}"] = {
        "candidates":  candidates,
        "matched_on":  matched_on,
        "match":       match,
        "filename":    filename_guess,
    }


def _render_dlis_uwi(ukey: str) -> str:
    """
    Show match result and UWI input.
    Returns UWI to use (matched UWI, manual override, or filename).
    """
    data  = st.session_state.get(f"wb_match_{ukey}", {})
    match = data.get("match")
    fname = data.get("filename", "")

    if match:
        st.success(
            f"✅ Matched **'{data.get('matched_on','')}'** → "
            f"**{match['well_name']}**  (UWI: `{match['uwi']}`, "
            f"score: {match['score']:.0%})"
        )
        default = match["uwi"]
    elif data.get("candidates"):
        st.warning(
            f"⚠️ No PPDM match for: *{', '.join(data['candidates'])}*  "
            f"— enter UWI manually or leave blank to use filename."
        )
        default = ""
    else:
        st.info("No well name in header or filename — enter UWI manually.")
        default = ""

    uwi = st.text_input(
        "UWI (override if needed)", value=default,
        key=f"wb_uwi_{ukey}",
        placeholder="Leave blank to catalog under filename"
    )

    if not uwi.strip() and fname:
        st.caption(f"📄 Will catalog under filename: **{fname}**")

    return uwi.strip() or fname


def _cataloger_dlis(engine, dialect, inventory_id, file_path, group_file_id, ukey):
    try:
        from dataview.file_catalog.dlis_catalog import catalog_dlis_file, parse_dlis_header
    except ImportError as e:
        st.error(f"dlis_catalog not available: {e}"); return

    repos = _get_repos(engine)
    if f"wb_repo_{ukey}" in st.session_state:
        if st.session_state[f"wb_repo_{ukey}"] not in repos:
            st.session_state.pop(f"wb_repo_{ukey}", None)
    repo_label = st.selectbox("Repository", list(repos.keys()), key=f"wb_repo_{ukey}")
    repo_id = repos.get(repo_label, "")

    # Auto-load header on first render
    if f"wb_hdr_data_{ukey}" not in st.session_state:
        with st.spinner("Reading header…"):
            try:
                hdr = parse_dlis_header(file_path)
                st.session_state[f"wb_hdr_data_{ukey}"] = hdr
                _run_dlis_well_match(engine, file_path, hdr, ukey)
            except Exception as e:
                st.warning(f"Could not read header: {e}")

    hdr = st.session_state.get(f"wb_hdr_data_{ukey}", {})
    if hdr:
        for k,v in {"Well":hdr.get("well_name",""),"Well ID":hdr.get("well_id",""),
                    "Company":hdr.get("company",""),"Field":hdr.get("field_name","")}.items():
            st.text(f"{k}: {v or '—'}")

    uwi = _render_dlis_uwi(ukey)

    if st.button("📥 Catalog DLIS", type="primary", key=f"wb_cat_{ukey}"):
        if not uwi.strip(): st.error("UWI or filename required."); return
        try:
            from dataview.file_catalog.file_header_catalog import (
                catalog_file_header, ensure_header_schema)
            if "wb_schema_ok" not in st.session_state:
                ensure_header_schema(engine, dialect)
                st.session_state["wb_schema_ok"] = True
            match_data = st.session_state.get(f"wb_match_{ukey}", {})
            m = match_data.get("match") or {}
            fhid = catalog_file_header(
                engine=engine, dialect=dialect,
                file_path=file_path, inventory_id=inventory_id,
                matched_uwi=uwi.strip(),
                match_method="FILENAME" if _extract_uwi_from_filename(file_path) else
                             "FUZZY"    if m else "MANUAL",
                match_score=m.get("score", 1.0),
                guessed_uwi=_extract_uwi_from_filename(file_path),
                guessed_well=match_data.get("candidates",[""])[0] if match_data.get("candidates") else "",
                guessed_survey="",
                cataloged_by=st.session_state.get("inv_user_id",""),
            )
            mark_cataloged(engine, dialect, inventory_id, group_file_id)
            st.success(f"✅ Header cataloged — {uwi.strip()}")
            st.rerun()
        except Exception as e:
            st.error(str(e))


def _cataloger_lis(engine, dialect, inventory_id, file_path, group_file_id, ukey):
    try:
        from dataview.file_catalog.dlis_catalog import catalog_lis_file, parse_lis_header
    except ImportError as e:
        st.error(f"dlis_catalog not available: {e}"); return

    repos = _get_repos(engine)
    if f"wb_repo_{ukey}" in st.session_state:
        if st.session_state[f"wb_repo_{ukey}"] not in repos:
            st.session_state.pop(f"wb_repo_{ukey}", None)
    repo_label = st.selectbox("Repository", list(repos.keys()), key=f"wb_repo_{ukey}")
    repo_id = repos.get(repo_label, "")

    # Auto-load header on first render
    if f"wb_hdr_data_{ukey}" not in st.session_state:
        with st.spinner("Reading header…"):
            try:
                hdr = parse_lis_header(file_path)
                st.session_state[f"wb_hdr_data_{ukey}"] = hdr
                _run_dlis_well_match(engine, file_path, hdr, ukey)
            except Exception as e:
                st.warning(f"Could not read header: {e}")

    hdr = st.session_state.get(f"wb_hdr_data_{ukey}", {})
    if hdr:
        for k,v in {"Well":hdr.get("well_name",""),
                    "Company":hdr.get("company","")}.items():
            st.text(f"{k}: {v or '—'}")

    uwi = _render_dlis_uwi(ukey)

    if st.button("📥 Catalog LIS", type="primary", key=f"wb_cat_{ukey}"):
        if not uwi.strip(): st.error("UWI or filename required."); return
        try:
            from dataview.file_catalog.file_header_catalog import (
                catalog_file_header, ensure_header_schema)
            if "wb_schema_ok" not in st.session_state:
                ensure_header_schema(engine, dialect)
                st.session_state["wb_schema_ok"] = True
            match_data = st.session_state.get(f"wb_match_{ukey}", {})
            m = match_data.get("match") or {}
            fhid = catalog_file_header(
                engine=engine, dialect=dialect,
                file_path=file_path, inventory_id=inventory_id,
                matched_uwi=uwi.strip(),
                match_method="FILENAME" if _extract_uwi_from_filename(file_path) else
                             "FUZZY"    if m else "MANUAL",
                match_score=m.get("score", 1.0),
                guessed_uwi=_extract_uwi_from_filename(file_path),
                guessed_well=match_data.get("candidates",[""])[0] if match_data.get("candidates") else "",
                guessed_survey="",
                cataloged_by=st.session_state.get("inv_user_id",""),
            )
            mark_cataloged(engine, dialect, inventory_id, group_file_id)
            st.success(f"✅ Header cataloged — {uwi.strip()}")
            st.rerun()
        except Exception as e:
            st.error(str(e))


def _cataloger_segy(engine, dialect, inventory_id, file_path, group_file_id, ukey):
    try:
        from dataview.file_catalog.segy_catalog import catalog_segy_file
    except ImportError as e:
        st.error(f"segy_catalog not available: {e}"); return

    repos = _get_repos(engine)
    if f"wb_repo_{ukey}" in st.session_state:
        if st.session_state[f"wb_repo_{ukey}"] not in repos:
            st.session_state.pop(f"wb_repo_{ukey}", None)
    repo_label = st.selectbox("Repository", list(repos.keys()), key=f"wb_repo_{ukey}")
    repo_id = repos.get(repo_label, "")
    seed = st.checkbox("Seed PPDM (SEIS_SET + SEIS_LINE)", key=f"wb_seed_{ukey}")

    survey_from_file = _extract_survey_from_filename(file_path)
    if survey_from_file:
        st.caption(f"📄 Suggested survey name from filename: `{survey_from_file}`")

    if st.button("📥 Catalog SEG-Y", type="primary", key=f"wb_cat_{ukey}"):
        try:
            from dataview.file_catalog.file_header_catalog import (
                catalog_file_header, ensure_header_schema)
            if "wb_schema_ok" not in st.session_state:
                ensure_header_schema(engine, dialect)
                st.session_state["wb_schema_ok"] = True
            survey = _extract_survey_from_filename(file_path)
            fhid = catalog_file_header(
                engine=engine, dialect=dialect,
                file_path=file_path, inventory_id=inventory_id,
                matched_uwi="", match_method="FILENAME" if survey else "",
                match_score=0.0,
                guessed_uwi="", guessed_well="", guessed_survey=survey,
                cataloged_by=st.session_state.get("inv_user_id",""),
            )
            mark_cataloged(engine, dialect, inventory_id, group_file_id)
            st.success("✅ SEG-Y header cataloged.")
            st.rerun()
        except Exception as e:
            st.error(str(e))


def _cataloger_p190(engine, dialect, inventory_id, file_path, group_file_id, ukey):
    try:
        from dataview.file_catalog.p190_catalog import catalog_p190_file
    except ImportError as e:
        st.error(f"p190_catalog not available: {e}"); return

    repos = _get_repos(engine)
    if f"wb_repo_{ukey}" in st.session_state:
        if st.session_state[f"wb_repo_{ukey}"] not in repos:
            st.session_state.pop(f"wb_repo_{ukey}", None)
    repo_label = st.selectbox("Repository", list(repos.keys()), key=f"wb_repo_{ukey}")
    repo_id = repos.get(repo_label, "")

    survey_from_file = _extract_survey_from_filename(file_path)
    if survey_from_file:
        st.caption(f"📄 Suggested survey name from filename: `{survey_from_file}`")

    if st.button("📥 Catalog P190", type="primary", key=f"wb_cat_{ukey}"):
        try:
            from dataview.file_catalog.file_header_catalog import (
                catalog_file_header, ensure_header_schema)
            if "wb_schema_ok" not in st.session_state:
                ensure_header_schema(engine, dialect)
                st.session_state["wb_schema_ok"] = True
            survey = _extract_survey_from_filename(file_path)
            fhid = catalog_file_header(
                engine=engine, dialect=dialect,
                file_path=file_path, inventory_id=inventory_id,
                matched_uwi="", match_method="FILENAME" if survey else "",
                match_score=0.0,
                guessed_uwi="", guessed_well="", guessed_survey=survey,
                cataloged_by=st.session_state.get("inv_user_id",""),
            )
            mark_cataloged(engine, dialect, inventory_id, group_file_id)
            st.success("✅ P190 header cataloged.")
            st.rerun()
        except Exception as e:
            st.error(str(e))
