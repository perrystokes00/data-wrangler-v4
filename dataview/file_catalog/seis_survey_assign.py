"""
seis_survey_assign.py — name the surveys the extractor refused to guess.

ONE IMPLEMENTATION, imported by every page that offers it. page_workbench and
page_file_catalog each carried their own `_seis_survey_grid` and they had
already DRIFTED: a paged text_input grid in one, an older `data_editor`
version querying different columns with a LEFT JOIN in the other. Two
spellings of one feature is how the escapechar bug came back through a fourth
writer, and why FILE_SEIS_HEADER's MERGE now lives in exactly one place.

WHY A HUMAN HAS TO DO THIS AT ALL
---------------------------------
An untyped SEG-Y rev-0 textual header is a CARD IMAGE — a blank form. When
nobody filled it in, line C 2 still reads

    C 2 LINE            AREA                        MAP ID

and a survey-name regex captures "AREA MAP ID". The extractor refuses that
(extract_core._is_template_survey_name) rather than promoting five unrelated
files as one invented survey, and leaves SURVEY_NAME NULL with
SURVEY_NAME_SOURCE='rejected-template'. promote_seismic then HOLDS them.

The name is a DECISION, not a step. Automation may skip ceremony, never a
decision — so this panel is where the decision gets made.

THE GROUP IS THE UNIT, NOT THE FILE
-----------------------------------
2D lines arrive as a SET: lineA..lineE are five files of ONE survey. The first
cut of this panel rendered a text box per file and the operator typed the same
name five times, which is transcription, not review — and transcription is
where a typo becomes two surveys that should have been one. The per-file
"guess from path" made it worse by offering LINEA, LINEB, LINEC… — a different
name for every line, as the path of least resistance.

The second cut added "apply to all", which still left five boxes to save and
could not express "these three, not those two".

So the group is the primary object here: ONE name, a selection of the lines it
covers (all of them by default, because that is the common case), one write.
Per-file editing is still available behind a toggle for the genuinely mixed
case — a toggle rather than an expander, because this panel renders INSIDE an
expander on Status & Backlog and Streamlit forbids nesting them.
"""
from __future__ import annotations

import streamlit as st

REVIEW_PAGE = 200      # cap rows rendered at once (text_inputs are cheap, not free)

_SQL_UNNAMED = """
    SELECT sh.SEIS_HEADER_ID AS id, sh.INVENTORY_ID AS inv,
           g.FILE_NAME AS fname, g.FILE_PATH AS path,
           sh.SEIS_SET_TYPE AS stype, sh.SURVEY_NAME AS survey
    FROM file_catalog.FILE_SEIS_HEADER sh
    JOIN file_catalog.GLOBAL_FILE_CATALOG g
           ON g.INVENTORY_ID = sh.INVENTORY_ID
    WHERE sh.SURVEY_NAME IS NULL OR LTRIM(RTRIM(sh.SURVEY_NAME)) = ''
    ORDER BY g.FILE_NAME"""

# SURVEY_NAME_SOURCE='manual' is what makes a typed name STICK: it stops
# enrich_file_headers substituting a filename guess, and extract_core's
# _SQL_SEIS_MERGE treats 'manual' as outranking everything automatic, so a
# re-extract cannot blank it. Writing the name without the source is how a
# survey a person named comes back as 'lineA' with nothing recording the loss.
_SQL_SET_NAME = ("UPDATE file_catalog.FILE_SEIS_HEADER "
                 "SET SURVEY_NAME=:v, SURVEY_NAME_SOURCE='manual' "
                 "WHERE SEIS_HEADER_ID=:id")


def _write(engine, pairs, _t):
    """pairs = [{'id': header_id, 'v': survey_name}] — one transaction."""
    with engine.begin() as con:
        for up in pairs:
            con.execute(_t(_SQL_SET_NAME), up)
    return len(pairs)


def seis_survey_grid(engine):
    """Assign SURVEY_NAME to seismic headers that have none."""
    from sqlalchemy import text as _t
    from dataview.core import path_identity as _pi

    with engine.connect() as con:
        rows = con.execute(_t(_SQL_UNNAMED)).fetchall()

    if not rows:
        st.success("Every seismic header already has a survey name.")
        return

    total = len(rows)
    rows = rows[:REVIEW_PAGE]
    if total > REVIEW_PAGE:
        st.caption(f"{total} files need a survey name — showing the first "
                   f"{REVIEW_PAGE}.")

    def _label(r):
        t = (r.stype or "").strip()
        return f"{r.fname or _pi._basename(r.path or '') or r.inv}" + (f"  ({t})" if t else "")

    _by_id = {r.id: r for r in rows}
    _labels = {r.id: _label(r) for r in rows}
    _all_ids = list(_by_id)

    # ── the group path: one name, one selection, one write ──────────────────
    st.markdown("**Name one survey**")
    st.caption(f"{total} file(s) need a survey name. 2D lines usually belong to "
               f"ONE survey — name it once here. Everything is selected by "
               f"default; deselect anything that belongs to a different survey.")

    name = st.text_input(
        "Survey name", key="seis_grp_name",
        placeholder="e.g. NPR-3 2D",
        help="Stored with SURVEY_NAME_SOURCE='manual', which stops enrich "
             "and re-extract overwriting it. It does NOT survive a database "
             "reset.")

    # default=all — the common case is that every held line is one survey, so
    # the zero-click path is: type the name, press the button.
    picked = st.multiselect(
        "Files in this survey", options=_all_ids, default=_all_ids,
        format_func=lambda i: _labels.get(i, str(i)),
        key="seis_grp_pick")

    _n = len(picked)
    if st.button(f"💾 Assign this survey to {_n} file(s)", type="primary",
                 key="seis_grp_save", disabled=not _n):
        _v = str(name or "").strip()
        if not _v:
            st.warning("Type a survey name first.")
        else:
            n = _write(engine, [{"id": i, "v": _v} for i in picked], _t)
            st.success(f"Named {n} file(s) “{_v}”. Re-run promote to lift them "
                       f"into dv_seis_set.")
            st.rerun()

    # ── the mixed case, behind a toggle ─────────────────────────────────────
    # A TOGGLE, NOT AN EXPANDER. This panel is rendered inside an expander on
    # Status & Backlog, and Streamlit forbids nesting them — the same reason
    # _pipeline_stages reveals Triage & Review with a toggle instead.
    if not st.checkbox("These are different surveys — let me name them one by one",
                       key="seis_grp_per_file"):
        return

    st.caption("The **Guess (from path)** column is a filename, not a survey: "
               "it offers a different name for every line, which is exactly how "
               "one survey becomes five. Use it only when the files really are "
               "unrelated.")
    h1, h2, h3, h4 = st.columns([3, 2, 2, 2])
    h1.markdown("**File**")
    h2.markdown("**Current**")
    h3.markdown("**Guess (from path)**")
    h4.markdown("**Assign survey**")

    inputs = []
    for r in rows:
        g = _pi.survey_from_path(r.path or "") or ""
        key = f"pl_seis_{r.id}"
        if key not in st.session_state:
            st.session_state[key] = g
        c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
        c1.write(_labels[r.id])
        c2.write(r.survey or "—")
        c3.write(g or "—")
        c4.text_input("assign survey", key=key, label_visibility="collapsed",
                      placeholder="survey name")
        inputs.append((r.id, key))

    if st.button("💾 Save per-file names", key="pl_seis_save"):
        ups = [{"id": rid, "v": str(st.session_state.get(k, "") or "").strip()}
               for rid, k in inputs]
        ups = [u for u in ups if u["v"]]
        if not ups:
            st.warning("No survey names to write.")
        else:
            n = _write(engine, ups, _t)
            st.success(f"Wrote {n} survey name(s). Re-run promote to lift them "
                       f"into dv_seis_set.")
            st.rerun()
