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
2D lines arrive as a SET: lineA..lineE are five files of ONE survey. Three
cuts to get the interaction right, each fixing what the last one cost:

  1. a text box per file          — typing one name five times is
                                    transcription, not review, and the
                                    per-file "guess from path" offered LINEA,
                                    LINEB, LINEC…, a different name per line
  2. + "apply to all"             — still two actions after typing, and
                                    all-or-nothing: no way to say "these
                                    three, not those two"
  3. + a multiselect              — expressed a subset, but a wall of chips
                                    once the list is long
  4. a CHECKBOX GRID with select
     all / clear                  — scannable at length, one name field, one
                                    write. Where it stands.

The per-file path survives behind a toggle for the genuinely mixed case — a
toggle rather than an expander, because this panel renders INSIDE an expander
on Status & Backlog and Streamlit forbids nesting them.
"""
from __future__ import annotations

import streamlit as st

REVIEW_PAGE = 200      # cap rows rendered at once (widgets are cheap, not free)

# SELECT-ALL IS A REQUEST, NEVER A DIRECT WRITE. Streamlit scar #6: a widget's
# own key must not be assigned after the widget is instantiated — it raises on
# a LATER run, on whatever page draws next, so the crash lands far from its
# cause. The buttons record an intent and rerun; it is honoured at the top of
# the next run, ahead of every checkbox.
_SEL_REQ = "seis_sel_req"

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
# ONE constant, so neither write path can set the name and forget the source.
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

    # CONSUMED BEFORE ANY CHECKBOX EXISTS. See _SEL_REQ.
    sel_req = st.session_state.pop(_SEL_REQ, None)

    with engine.connect() as con:
        rows = con.execute(_t(_SQL_UNNAMED)).fetchall()

    if not rows:
        st.success("Every seismic header already has a survey name.")
        return

    total = len(rows)
    rows = rows[:REVIEW_PAGE]

    st.markdown("**Name one survey**")
    st.caption(
        f"{total} file(s) need a survey name"
        + (f" — showing the first {REVIEW_PAGE}." if total > REVIEW_PAGE else ".")
        + " 2D lines usually all belong to ONE survey: type it once, tick the "
          "lines it covers, and press Assign.")

    name = st.text_input(
        "Survey name", key="seis_grp_name",
        placeholder="e.g. NPR-3 2D",
        help="Stored with SURVEY_NAME_SOURCE='manual', which stops enrich and "
             "re-extract overwriting it. It does NOT survive a database reset.")

    # ── select all / clear ──────────────────────────────────────────────────
    # Buttons rather than a master checkbox: a checkbox whose state drives
    # other checkboxes has to detect its own transition, and the obvious way
    # to do that is to write their keys — which is the thing scar #6 forbids.
    b1, b2, b3 = st.columns([1, 1, 4])
    if b1.button("☑ Select all", key="seis_sel_all", use_container_width=True):
        st.session_state[_SEL_REQ] = True
        st.rerun()
    if b2.button("☐ Clear", key="seis_sel_none", use_container_width=True):
        st.session_state[_SEL_REQ] = False
        st.rerun()

    # ── the grid ────────────────────────────────────────────────────────────
    h1, h2, h3, h4 = st.columns([4, 1, 2, 3])
    h1.markdown("**File**")
    h2.markdown("**Type**")
    h3.markdown("**Current**")
    h4.markdown("**Guess (from path)**")

    picked = []
    for r in rows:
        k = f"seis_ck_{r.id}"
        if sel_req is not None:
            st.session_state[k] = sel_req      # before the widget is created
        elif k not in st.session_state:
            st.session_state[k] = True         # default: everything is one survey
        c1, c2, c3, c4 = st.columns([4, 1, 2, 3])
        on = c1.checkbox(r.fname or _pi._basename(r.path or "") or str(r.inv),
                         key=k)
        c2.write((r.stype or "—"))
        c3.write(r.survey or "—")
        c4.write(_pi.survey_from_path(r.path or "") or "—")
        if on:
            picked.append(r.id)

    n = len(picked)
    b3.caption(f"{n} of {len(rows)} selected")

    if st.button(f"💾 Assign this survey to {n} file(s)", type="primary",
                 key="seis_grp_save", disabled=not n):
        v = str(name or "").strip()
        if not v:
            st.warning("Type a survey name first.")
        else:
            wrote = _write(engine, [{"id": i, "v": v} for i in picked], _t)
            st.success(f"Named {wrote} file(s) “{v}”. Re-run promote to lift "
                       f"them into dv_seis_set.")
            st.rerun()

    # ── the mixed case, behind a toggle ─────────────────────────────────────
    # A TOGGLE, NOT AN EXPANDER — this panel is rendered inside an expander on
    # Status & Backlog, and Streamlit forbids nesting them. Same reason
    # _pipeline_stages reveals Triage & Review with a toggle.
    if not st.checkbox("These are different surveys — name them one by one",
                       key="seis_grp_per_file"):
        return

    st.caption("The **Guess (from path)** column is a filename, not a survey: "
               "it offers a different name for every line, which is exactly how "
               "one survey becomes five. Use it only when the files really are "
               "unrelated.")
    inputs = []
    for r in rows:
        g = _pi.survey_from_path(r.path or "") or ""
        key = f"pl_seis_{r.id}"
        if key not in st.session_state:
            st.session_state[key] = g
        c1, c2 = st.columns([4, 3])
        c1.write(r.fname or _pi._basename(r.path or "") or str(r.inv))
        c2.text_input("assign survey", key=key, label_visibility="collapsed",
                      placeholder="survey name")
        inputs.append((r.id, key))

    if st.button("💾 Save per-file names", key="pl_seis_save"):
        ups = [{"id": rid, "v": str(st.session_state.get(k, "") or "").strip()}
               for rid, k in inputs]
        ups = [u for u in ups if u["v"]]
        if not ups:
            st.warning("No survey names to write.")
        else:
            wrote = _write(engine, ups, _t)
            st.success(f"Wrote {wrote} survey name(s). Re-run promote to lift "
                       f"them into dv_seis_set.")
            st.rerun()
