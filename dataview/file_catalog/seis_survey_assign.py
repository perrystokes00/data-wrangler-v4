"""
seis_survey_assign.py — name the surveys the extractor refused to guess.

ONE IMPLEMENTATION, imported by both pages that offer it. page_workbench and
page_file_catalog each carried their own `_seis_survey_grid`, and they had
already DRIFTED: the workbench's was a paged text_input grid, the file
catalog's an older `data_editor` version querying different columns with a
LEFT JOIN. Two spellings of one feature is how the escapechar bug came back
through a fourth writer, and it is why FILE_SEIS_HEADER's MERGE now lives in
exactly one place. This is the same move for the UI that writes it.

WHY A HUMAN HAS TO DO THIS AT ALL
---------------------------------
An untyped SEG-Y rev-0 textual header is a CARD IMAGE — a blank form. When
nobody filled it in, line C 2 still reads

    C 2 LINE            AREA                        MAP ID

and a survey-name regex captures "AREA MAP ID". The extractor refuses that
(extract_core._is_template_survey_name) rather than promoting five unrelated
files as one invented survey, and leaves SURVEY_NAME NULL with
SURVEY_NAME_SOURCE='rejected-template'. promote_seismic then HOLDS them.

So the name is a DECISION, not a step, and the design law says automation may
skip ceremony but never a decision. This panel is where the decision gets made.

THE GROUP CASE IS THE COMMON ONE
--------------------------------
2D lines arrive as a set: lineA..lineE are five files of ONE survey. Typing the
same name five times is not review, it is transcription — and transcription is
where typos become two surveys that should have been one. So "apply to all
shown" fills the boxes and Save still writes them, keeping the confirm step
that makes it a decision rather than a bulk overwrite.
"""
from __future__ import annotations

import streamlit as st

REVIEW_PAGE = 200      # cap rows rendered at once (text_inputs are cheap, not free)

_BULK_REQ = "pl_seis_bulk_req"


def seis_survey_grid(engine):
    """Assign SURVEY_NAME to seismic headers that have none."""
    from sqlalchemy import text as _t
    from dataview.core import path_identity as _pi

    # CONSUME THE BULK REQUEST BEFORE ANY WIDGET IS DRAWN. Streamlit scar #6:
    # a widget's own key must never be assigned after instantiation — it raises
    # on a LATER run, on whatever page draws next, so the crash appears far
    # from its cause. The button below therefore only records a REQUEST and
    # reruns; this is where it is honoured, ahead of the text_inputs.
    bulk = st.session_state.pop(_BULK_REQ, None)

    with engine.connect() as con:
        rows = con.execute(_t("""
            SELECT sh.SEIS_HEADER_ID AS id, sh.INVENTORY_ID AS inv,
                   g.FILE_NAME AS fname, g.FILE_PATH AS path,
                   sh.SURVEY_NAME AS survey
            FROM file_catalog.FILE_SEIS_HEADER sh
            JOIN file_catalog.GLOBAL_FILE_CATALOG g
                   ON g.INVENTORY_ID = sh.INVENTORY_ID
            WHERE sh.SURVEY_NAME IS NULL OR LTRIM(RTRIM(sh.SURVEY_NAME)) = ''
            ORDER BY g.FILE_NAME""")).fetchall()

    if not rows:
        st.success("Every seismic header already has a survey name.")
        return

    total = len(rows)
    rows = rows[:REVIEW_PAGE]
    if total > REVIEW_PAGE:
        st.caption(f"{total} files need a survey name — showing the first "
                   f"{REVIEW_PAGE}. Save these, then the next batch appears.")
    else:
        st.caption(f"{total} file(s) need a survey name. Edit the value, then Save.")

    # ── group assign ────────────────────────────────────────────────────────
    # 2D lines come as a set; one survey spans all of them. Filling the boxes
    # rather than writing straight through keeps Save as the confirm step.
    with st.container(border=True):
        st.caption("**Same survey for all of these?** Fill every box below in "
                   "one go, then review and Save. Typing one name per file is "
                   "how five lines of one survey become five surveys.")
        _b1, _b2 = st.columns([3, 1])
        _bulk_val = _b1.text_input(
            "survey name for all shown", key="pl_seis_bulk",
            label_visibility="collapsed",
            placeholder="e.g. NPR-3 2D — applies to all files listed below")
        if _b2.button("Apply to all", key="pl_seis_bulk_apply",
                      use_container_width=True):
            _v = str(_bulk_val or "").strip()
            if not _v:
                st.warning("Type a survey name first.")
            else:
                # request only — honoured at the top of the NEXT run, before
                # the per-file widgets exist. See the note above.
                st.session_state[_BULK_REQ] = _v
                st.rerun()

    h1, h2, h3, h4 = st.columns([3, 2, 2, 2])
    h1.markdown("**File**")
    h2.markdown("**Current**")
    h3.markdown("**Guess (from path)**")
    h4.markdown("**Assign survey**")

    inputs = []  # (header_id, widget_key)
    for r in rows:
        g = _pi.survey_from_path(r.path or "") or ""
        key = f"pl_seis_{r.id}"
        if bulk is not None:
            st.session_state[key] = bulk        # before the widget is created
        elif key not in st.session_state:
            st.session_state[key] = g
        c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
        c1.write(r.fname or _pi._basename(r.path or "") or f"(inventory {r.inv})")
        c2.write(r.survey or "—")
        c3.write(g or "—")
        c4.text_input("assign survey", key=key, label_visibility="collapsed",
                      placeholder="survey name")
        inputs.append((r.id, key))

    if bulk is not None:
        st.info(f"Filled {len(inputs)} box(es) with **{bulk}**. "
                "Review them, then Save.")

    if st.button("💾 Save survey names", type="primary", key="pl_seis_save"):
        ups = []
        for rid, key in inputs:
            v = str(st.session_state.get(key, "") or "").strip()
            if v:
                ups.append({"id": rid, "v": v})
        if not ups:
            st.warning("No survey names to write.")
        else:
            with engine.begin() as con:
                for up in ups:
                    # SURVEY_NAME_SOURCE='manual' is what stops enrich
                    # substituting a filename guess later, and what stops a
                    # re-extract blanking the column — see extract_core's
                    # _SQL_SEIS_MERGE, which treats 'manual' as outranking
                    # everything automatic.
                    con.execute(_t(
                        "UPDATE file_catalog.FILE_SEIS_HEADER "
                        "SET SURVEY_NAME=:v, SURVEY_NAME_SOURCE='manual' "
                        "WHERE SEIS_HEADER_ID=:id"), up)
            st.success(f"Wrote {len(ups)} survey name(s). Re-run promote to "
                       f"lift them into dv_seis_set.")
            st.rerun()
