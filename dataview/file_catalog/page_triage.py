"""
page_triage.py — Phase 2 of the triage pipeline: run triage from the app, see
the tier counts, and work the REVIEW queue (the files triage couldn't resolve
automatically — a name but no confident UWI).

Reuses triage_inventory's own functions so the logic is identical to the CLI.

Wire into app_v3.py nav, e.g.:
    from dataview.file_catalog import page_triage
    ...
    page_triage.render(engine, dialect)
"""
import streamlit as st
import pandas as pd
from sqlalchemy import text, bindparam

GFC = "file_catalog.GLOBAL_FILE_CATALOG"
FWH = "file_catalog.FILE_WELL_HEADER"
DEFAULT_REF = "WELL_REF.well_ref.well_master_public_v2"


# ── data helpers ──────────────────────────────────────────────────────────────
def _tier_counts(engine):
    with engine.connect() as c:
        rows = c.execute(text(
            f"SELECT VALUE_TIER, COUNT(*) FROM {GFC} GROUP BY VALUE_TIER"
        )).fetchall()
    return {(r[0] or "(untiered)"): r[1] for r in rows}


def _source_counts(engine):
    with engine.connect() as c:
        rows = c.execute(text(
            f"SELECT IDENTITY_SOURCE, COUNT(*) FROM {FWH} "
            f"WHERE IDENTITY_SOURCE IS NOT NULL GROUP BY IDENTITY_SOURCE"
        )).fetchall()
    return {r[0]: r[1] for r in rows}


def _review_rows(engine):
    with engine.connect() as c:
        return pd.read_sql(text(f"""
            SELECT g.INVENTORY_ID, g.FILE_NAME, g.FILE_PATH,
                   g.FILE_TYPE_GROUP, h.WELL_NAME, h.NAME_NORM,
                   h.OPERATOR, h.STATE, h.COUNTY,
                   h.UWI, h.IDENTITY_SOURCE
            FROM {GFC} g
            JOIN {FWH} h ON h.INVENTORY_ID = g.INVENTORY_ID
            WHERE g.VALUE_TIER = 'REVIEW'
              AND ISNULL(g.CATALOG_READINESS,'') <> 'AWAITING_UWI'
            ORDER BY h.NAME_NORM
        """), c)


def _awaiting_rows(engine):
    """Files kept as 'good, just awaiting a UWI' — out of the active worklist
    but still auto-resolved by future triage runs."""
    with engine.connect() as c:
        return pd.read_sql(text(f"""
            SELECT g.INVENTORY_ID, g.FILE_NAME, h.WELL_NAME
            FROM {GFC} g
            JOIN {FWH} h ON h.INVENTORY_ID = g.INVENTORY_ID
            WHERE g.CATALOG_READINESS = 'AWAITING_UWI'
            ORDER BY h.WELL_NAME
        """), c)


def _reactivate(engine, invs):
    """Move kept files back into the active REVIEW worklist."""
    if not invs:
        return
    q = text(f"UPDATE {GFC} SET CATALOG_READINESS='REVIEW' "
             f"WHERE INVENTORY_ID IN :v AND CATALOG_READINESS='AWAITING_UWI'"
             ).bindparams(bindparam("v", expanding=True))
    with engine.begin() as con:
        con.execute(q, {"v": [str(i) for i in invs]})


def _low_rows(engine):
    """Files triage couldn't identify at all (no name, no UWI) — VALUE_TIER LOW.
    LEFT JOIN so files that never got a FILE_WELL_HEADER row still appear."""
    with engine.connect() as c:
        return pd.read_sql(text(f"""
            SELECT g.INVENTORY_ID, g.FILE_NAME, g.FILE_TYPE_GROUP,
                   g.TRIAGE_REASON, g.FILE_PATH,
                   h.UWI AS EXTRACTED_UWI, h.WELL_NAME
            FROM {GFC} g
            LEFT JOIN {FWH} h ON h.INVENTORY_ID = g.INVENTORY_ID
            WHERE g.VALUE_TIER = 'LOW'
            ORDER BY g.FILE_TYPE_GROUP, g.FILE_NAME
        """), c)


def _reextract(engine, invs):
    """Reset HEADER_EXTRACTED so the workbench re-runs extraction on these files
    (e.g. after a summarizer/parser fix). Extraction only re-processes files
    flagged 'N'/NULL, so this is what un-sticks already-extracted files."""
    invs = [str(i) for i in invs if str(i).strip()]
    if not invs:
        return 0
    q = text(f"UPDATE {GFC} SET HEADER_EXTRACTED='N' "
             f"WHERE INVENTORY_ID IN :v"
             ).bindparams(bindparam("v", expanding=True))
    with engine.begin() as con:
        con.execute(q, {"v": invs})
    return len(invs)


def _candidates(engine, ref, namenorms):
    """For each NAME_NORM, the candidate UWI14s in the reference (so the
    reviewer can see why it was ambiguous and pick the right one)."""
    out = {}
    norms = [n for n in namenorms if n]
    if not norms:
        return out
    q = text(f"SELECT NAME_NORM, UWI14 FROM {ref} WHERE NAME_NORM IN :v "
             f"AND NULLIF(UWI14,'') IS NOT NULL").bindparams(
                 bindparam("v", expanding=True))
    try:
        with engine.connect() as c:
            for i in range(0, len(norms), 500):
                for nn, u in c.execute(q, {"v": norms[i:i + 500]}).fetchall():
                    out.setdefault(nn, [])
                    if u not in out[nn]:
                        out[nn].append(u)
    except Exception as e:
        st.warning(f"Candidate lookup skipped: {e}")
    return out


@st.cache_data(ttl=600, show_spinner=False)
def _candidates_cached(ref, norms_tuple, _engine):
    """Cached candidate lookup, keyed on (ref, names). The reference master
    changes rarely and the REVIEW set is stable between reruns, so caching stops
    the 2.5M-row WELL_MASTER scan from re-firing on every rerun — which is what
    pinned the app in a permanent RUNNING state. `_engine` is excluded from the
    cache key (leading underscore). Cache clears after 10 min or when the set of
    review names changes (e.g. after a triage run)."""
    return _candidates(_engine, ref, list(norms_tuple))


# ── actions ───────────────────────────────────────────────────────────────────
def _run_triage(engine, ref):
    """Run the full triage pass using the shared entry point."""
    from dataview.file_catalog import triage_inventory as ti
    ti.run_all_engine(engine, ref, dry=False, log=lambda m: None)


def _apply_review(engine, edited):
    """Reject / correct identity (name, operator, state, county, UWI) / keep-await,
    then re-tier. Precedence per row: Reject > field corrections (+ optional UWI)
    > Keep."""
    from dataview.file_catalog import triage_inventory as ti
    resolved, rejected, kept, updated = 0, 0, 0, 0
    try:
        with engine.begin() as con:
            for _, r in edited.iterrows():
                inv = str(r["_inv"])
                if bool(r.get("Reject")):
                    con.execute(text(
                        f"UPDATE {GFC} SET VALUE_TIER='REJECT', "
                        f"CATALOG_READINESS='SKIPPED' WHERE INVENTORY_ID=:i"),
                        {"i": inv})
                    rejected += 1
                    continue

                # Field corrections — write whatever's in the grid back to the
                # header (cells are pre-filled with current values, so leaving a
                # cell alone is a no-op rewrite).
                name = str(r.get("Well name", "") or "").strip()
                op   = str(r.get("Operator", "") or "").strip()
                stt  = str(r.get("State", "") or "").strip()
                co   = str(r.get("County", "") or "").strip()
                con.execute(text(
                    f"UPDATE {FWH} SET WELL_NAME=:n, NAME_NORM=:nn, OPERATOR=:op, "
                    f"STATE=:st, COUNTY=:co, IDENTITY_SOURCE='manual-review' "
                    f"WHERE INVENTORY_ID=:i"),
                    {"n": name or None,
                     "nn": ti.name_norm(name) if name else None,
                     "op": op or None, "st": stt or None, "co": co or None,
                     "i": inv})

                uwi = str(r.get("UWI", "") or "").strip()
                if uwi:
                    u14 = ti.norm14(uwi)
                    con.execute(text(
                        f"UPDATE {GFC} SET MATCHED_UWI=:v WHERE INVENTORY_ID=:i"),
                        {"v": uwi, "i": inv})
                    con.execute(text(
                        f"UPDATE {FWH} SET UWI=:v, UWI14=:u "
                        f"WHERE INVENTORY_ID=:i"),
                        {"v": uwi, "u": u14, "i": inv})
                    resolved += 1
                    continue

                if bool(r.get("Keep")):
                    con.execute(text(
                        f"UPDATE {GFC} SET CATALOG_READINESS='AWAITING_UWI' "
                        f"WHERE INVENTORY_ID=:i"), {"i": inv})
                    kept += 1
                else:
                    updated += 1
    except Exception as e:
        st.error(f"Saving review failed: {type(e).__name__}: {e}")
        return

    # Full triage pass: re-normalize, cross-fill, reference-fill (a corrected
    # name may now match WELL_MASTER and auto-resolve a UWI), then re-tier.
    try:
        ti.run_all_engine(engine, log=lambda *_: None)
    except Exception:
        # Fall back to a plain re-tier if the full pass isn't available.
        raw = engine.raw_connection()
        try:
            ti.score_tier(raw, False)
            raw.commit()
        finally:
            raw.close()

    msg = []
    if resolved:
        msg.append(f"resolved {resolved}")
    if updated:
        msg.append(f"corrected {updated}")
    if kept:
        msg.append(f"kept {kept}")
    if rejected:
        msg.append(f"rejected {rejected}")
    st.success(" · ".join(msg) if msg else "No changes.")


def _apply_low(engine, edited):
    """Commit each LOW row by whatever is marked on it — one row, one action,
    in precedence: Reject ticked → reject; Re-extract ticked → reset so Capture
    re-processes the file (e.g. after a parser fix); a typed UWI/name → accept
    (upsert a FILE_WELL_HEADER row, then re-tier — a UWI promotes to HIGH, a name
    moves to REVIEW or HIGH if it matches the reference)."""
    import uuid as _uuid
    from dataview.file_catalog import triage_inventory as ti
    resolved, named, rejected, reextracted = 0, 0, 0, 0
    try:
        with engine.begin() as con:
            for _, r in edited.iterrows():
                inv = str(r["_inv"])
                if bool(r.get("Reject")):
                    con.execute(text(
                        f"UPDATE {GFC} SET VALUE_TIER='REJECT', "
                        f"CATALOG_READINESS='SKIPPED' WHERE INVENTORY_ID=:i"),
                        {"i": inv})
                    rejected += 1
                    continue
                if bool(r.get("Re-extract")):
                    con.execute(text(
                        f"UPDATE {GFC} SET HEADER_EXTRACTED='N' "
                        f"WHERE INVENTORY_ID=:i"), {"i": inv})
                    reextracted += 1
                    continue
                uwi = str(r.get("UWI", "") or "").strip()
                name = str(r.get("Well name", "") or "").strip()
                if not uwi and not name:
                    continue
                hid = _uuid.uuid5(_uuid.NAMESPACE_URL, inv).hex.upper()
                # Upsert the header row. COALESCE(NULLIF(...)) so a blank cell
                # never wipes an existing value. UWI14/NAME_NORM are left to the
                # triage normalize step below.
                con.execute(text(f"""
                    MERGE {FWH} AS tgt
                    USING (SELECT :i AS INVENTORY_ID) src
                    ON tgt.INVENTORY_ID = src.INVENTORY_ID
                    WHEN MATCHED THEN UPDATE SET
                        UWI = COALESCE(NULLIF(:v,''), tgt.UWI),
                        WELL_NAME = COALESCE(NULLIF(:n,''), tgt.WELL_NAME),
                        IDENTITY_SOURCE = 'manual-review'
                    WHEN NOT MATCHED THEN INSERT
                        (WELL_HEADER_ID, INVENTORY_ID, UWI, WELL_NAME,
                         IDENTITY_SOURCE, EXTRACTED_DATE, EXTRACTED_BY)
                        VALUES (:hid, :i, NULLIF(:v,''), NULLIF(:n,''),
                                'manual-review', GETUTCDATE(), 'manual-review');
                """), {"i": inv, "v": uwi, "n": name, "hid": hid})
                if uwi:
                    con.execute(text(
                        f"UPDATE {GFC} SET MATCHED_UWI=:v WHERE INVENTORY_ID=:i"),
                        {"v": uwi, "i": inv})
                    resolved += 1
                else:
                    named += 1
    except Exception as e:
        st.error(f"Saving failed: {type(e).__name__}: {e}")
        return

    try:
        ti.run_all_engine(engine, log=lambda *_: None)
    except Exception:
        raw = engine.raw_connection()
        try:
            ti.score_tier(raw, False)
            raw.commit()
        finally:
            raw.close()

    msg = []
    if resolved:
        msg.append(f"applied {resolved} UWI")
    if named:
        msg.append(f"named {named}")
    if rejected:
        msg.append(f"rejected {rejected}")
    if reextracted:
        msg.append(f"reset {reextracted} for re-extract — run Capture → Extract")
    st.success(" · ".join(msg) if msg else "No changes.")


# ── page ──────────────────────────────────────────────────────────────────────
# ── phase 3: promote (capture to cat_* + copy to vault) ───────────────────────
VAULT_DEFAULT = r"C:\Bulk\Vault"
SEIS_EXTS = ".sgy,.segy,.p190,.p90,.p1,.p2,.p3"


def _promotable(engine):
    with engine.connect() as c:
        return pd.read_sql(text(f"""
            SELECT g.INVENTORY_ID, g.FILE_NAME, g.FILE_PATH, g.FILE_EXT,
                   g.FILE_TYPE_GROUP,
                   COALESCE(NULLIF(LTRIM(RTRIM(h.UWI)),''),
                            g.MATCHED_UWI, '') AS UWI
            FROM {GFC} g
            LEFT JOIN {FWH} h ON h.INVENTORY_ID = g.INVENTORY_ID
            WHERE g.VALUE_TIER = 'HIGH' AND g.CATALOG_READINESS = 'READY'
            ORDER BY g.FILE_TYPE_GROUP, g.FILE_NAME
        """), c)


def _do_promote(engine, dialect, df, vault_root, do_vault):
    """Capture each file into cat_* (reusing the batch loader), copy qualifying
    files into the vault, then mark the set PROMOTED. Returns (n, n_vault)."""
    from types import SimpleNamespace
    from dataview.file_catalog.page_workbench import _run_batch_load
    from dataview.file_catalog import vault_copy

    invs = [str(i) for i in df["INVENTORY_ID"].tolist()]
    loadable = pd.DataFrame({
        "_path": df["FILE_PATH"].values,
        "Ext":   df["FILE_EXT"].astype(str).values,
        "UWI":   df["UWI"].astype(str).values,
        "File":  df["FILE_NAME"].values,
        "_inv":  df["INVENTORY_ID"].values,
    })

    # 1) capture → cat_* (sets CATALOGED on the files that yield rows)
    _run_batch_load(engine, dialect, loadable)

    # 2) vault the qualifying files (vault_copy filters to UWI14 / survey itself)
    n_vault = 0
    if do_vault:
        a = SimpleNamespace(
            vault=vault_root, default_country="US", seis_ext=SEIS_EXTS,
            no_wells=False, no_seis=False, ref=DEFAULT_REF, dry_run=False,
            report=None, limit=None, server=None, database=None,
            inv_filter=invs)
        raw = engine.raw_connection()
        try:
            counts, _ = vault_copy.vault(raw, a, log=lambda m: None)
            raw.commit()
            n_vault = sum(v for k, v in counts.items()
                          if str(k).split("/")[-1]
                          in ("copy", "rename", "skip-exists"))
        except Exception as e:
            st.error(f"Vault step failed: {type(e).__name__}: {e}")
        finally:
            raw.close()

    # 3) mark the attempted set PROMOTED so it leaves the READY queue
    with engine.begin() as con:
        q = text(f"UPDATE {GFC} SET CATALOG_READINESS='PROMOTED' "
                 f"WHERE INVENTORY_ID IN :v").bindparams(
                     bindparam("v", expanding=True))
        for i in range(0, len(invs), 500):
            con.execute(q, {"v": invs[i:i + 500]})
    return len(invs), n_vault


def render(engine, dialect=None):
    st.markdown("## 🔎 Review & resolve flagged files")
    st.caption("Files the scan couldn't confidently key — a name but no certain "
               "UWI. Run the matcher to enrich and tier them, then work the queue "
               "to assign UWIs or reject. (This was the old \"Triage\" tab.)")

    c1, c2 = st.columns([3, 1])
    ref = c1.text_input("Reference table", DEFAULT_REF, key="triage_ref")
    if c2.button("▶ Run triage", type="primary", key="triage_run"):
        with st.spinner("Running triage…"):
            _run_triage(engine, ref)
        st.success("Triage complete.")

    counts = _tier_counts(engine)
    m = st.columns(4)
    for col, tier in zip(m, ("HIGH", "REVIEW", "LOW", "REJECT")):
        col.metric(tier, counts.get(tier, 0))

    src = _source_counts(engine)
    if src:
        st.caption("Resolved by: " + " · ".join(
            f"{k} {v}" for k, v in sorted(src.items())))

    rev = _review_rows(engine)
    awaiting = _awaiting_rows(engine)
    st.markdown(f"### Review queue ({len(rev)})")
    if len(awaiting):
        st.caption(f"📌 {len(awaiting)} kept, awaiting a UWI from a future load "
                   "(still auto-resolved on every triage run).")

    if not rev.empty:
        cands = _candidates_cached(
            ref,
            tuple(sorted(rev["NAME_NORM"].dropna().unique().tolist())),
            engine)
        n = len(rev)
        tbl = pd.DataFrame({
            "Reject":     [False] * n,
            "Keep":       [False] * n,
            "File":       rev["FILE_NAME"].astype(str).values,
            "Well name":  rev["WELL_NAME"].fillna("").astype(str).values,
            "Operator":   rev["OPERATOR"].fillna("").astype(str).values,
            "State":      rev["STATE"].fillna("").astype(str).values,
            "County":     rev["COUNTY"].fillna("").astype(str).values,
            "UWI":        rev["UWI"].fillna("").astype(str).values,
            "Source":     rev["IDENTITY_SOURCE"].fillna("").astype(str).values,
            "Candidates": [", ".join(cands.get(nn, [])) or "—"
                           for nn in rev["NAME_NORM"]],
            "_inv":       rev["INVENTORY_ID"].values,
        })
        edited = st.data_editor(
            tbl, use_container_width=True, hide_index=True,
            disabled=["File", "Source", "Candidates"],
            column_config={
                "Reject": st.column_config.CheckboxColumn("Reject", width="small"),
                "Keep": st.column_config.CheckboxColumn(
                    "Keep", width="small",
                    help="Good file — park it, awaiting a UWI from a later load."),
                "Well name": st.column_config.TextColumn(
                    "Well name", help="Correct the extracted well name."),
                "Operator": st.column_config.TextColumn("Operator"),
                "State": st.column_config.TextColumn("State", width="small"),
                "County": st.column_config.TextColumn("County"),
                "UWI": st.column_config.TextColumn(
                    "UWI", help="Confirm/edit the candidate, or paste one from "
                                "Candidates."),
                "Source": st.column_config.TextColumn(
                    "Source", width="small",
                    help="Where the candidate came from "
                         "(path-* = parsed from the file path/name — confirm it)."),
                "Candidates": st.column_config.TextColumn(
                    "Reference candidates", width="large"),
                "_inv": None,
            },
            key="triage_review_ed")
        st.caption("Path-derived candidates (Source = path-*) are pre-filled — "
                   "confirm or lightly edit, then apply. A fixed well name can "
                   "match the reference and auto-resolve a UWI; an accepted UWI "
                   "promotes the file to HIGH.")
        if st.button("✅ Apply review", type="primary",
                     key="triage_review_apply"):
            _apply_review(engine, edited)
            st.rerun()
    else:
        st.caption("Nothing to review — everything is resolved, kept, or "
                   "low-value.")

    if len(awaiting):
        with st.expander(f"📌 Awaiting UWI ({len(awaiting)})", expanded=False):
            atbl = pd.DataFrame({
                "Back to review": [False] * len(awaiting),
                "File":           awaiting["FILE_NAME"].astype(str).values,
                "Well name":      awaiting["WELL_NAME"].astype(str).values,
                "_inv":           awaiting["INVENTORY_ID"].values,
            })
            aedit = st.data_editor(
                atbl, use_container_width=True, hide_index=True,
                disabled=["File", "Well name"],
                column_config={
                    "Back to review": st.column_config.CheckboxColumn(
                        "Back to review", width="small"),
                    "_inv": None},
                key="triage_await_ed")
            if st.button("↩ Move selected back to review", key="triage_reactivate"):
                _reactivate(engine, aedit.loc[aedit["Back to review"],
                                              "_inv"].tolist())
                st.rerun()

    # ── LOW worklist — files triage couldn't identify (no name, no UWI) ────────
    low = _low_rows(engine)
    if not low.empty:
        with st.expander(f"❓ Unidentified · LOW ({len(low)})", expanded=False):
            st.caption(
                "Triage found neither a name nor a UWI for these. Re-extract them "
                "(e.g. after a parser fix) to try again, type a UWI to resolve one "
                "by hand, or reject it. Re-extract resets the file so Workbench → "
                "Extract re-processes it.")
            n = len(low)
            ltbl = pd.DataFrame({
                "Re-extract": [False] * n,
                "Reject":     [False] * n,
                "UWI":        [""] * n,
                "Well name":  low["WELL_NAME"].fillna("").astype(str).values,
                "File":       low["FILE_NAME"].astype(str).values,
                "Type":       low["FILE_TYPE_GROUP"].astype(str).values,
                "Reason":     low["TRIAGE_REASON"].astype(str).values,
                "_inv":       low["INVENTORY_ID"].values,
            })
            ledit = st.data_editor(
                ltbl, use_container_width=True, hide_index=True,
                disabled=["File", "Type", "Reason"],
                column_config={
                    "Re-extract": st.column_config.CheckboxColumn(
                        "Re-extract", width="small",
                        help="Reset this file so Workbench → Extract re-processes "
                             "it (use after a parser/summarizer fix)."),
                    "Reject": st.column_config.CheckboxColumn("Reject", width="small"),
                    "UWI": st.column_config.TextColumn(
                        "UWI", help="Type or paste a UWI to resolve this file."),
                    "Well name": st.column_config.TextColumn(
                        "Well name",
                        help="Give the file a well name; it can reference-match "
                             "to a UWI on apply."),
                    "Reason": st.column_config.TextColumn("Triage reason", width="large"),
                    "_inv": None,
                },
                key="triage_low_ed")
            st.caption("Mark each row, then **Apply** — one action per row in "
                       "this order: tick **Reject** to drop a non-well file · tick "
                       "**Re-extract** to re-process it after a parser fix · or type "
                       "a **UWI / Well name** to accept it (a header row is created "
                       "if the file never had one — a UWI promotes to HIGH, a name "
                       "moves to REVIEW or HIGH if it matches the reference).")
            if st.button("✅ Apply", type="primary", key="triage_low_apply",
                         use_container_width=True):
                _apply_low(engine, ledit)
                st.rerun()


def _run_promote_cli(apply: bool):
    """Run promote_catalog.py as a subprocess and show its report. Uses the same
    interpreter/cwd as the app, so it picks up the deployed script and the same
    DataView connection defaults. Output is shown when it finishes."""
    import subprocess
    import sys
    import os
    args = [sys.executable, "promote_catalog.py"] + (["--apply"] if apply else [])
    with st.spinner("Lifting cat_* → dv_*…" if apply else "Checking what would move…"):
        try:
            r = subprocess.run(args, capture_output=True, text=True,
                               cwd=os.getcwd(), timeout=1800)
        except Exception as e:
            st.error(f"Couldn't run promote_catalog.py: {type(e).__name__}: {e}")
            return
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    st.code(out or "(no output)")
    if r.returncode == 0:
        st.success("Lifted into dv_* — cat_* cleared of promoted rows."
                   if apply else "Dry-run complete — review, then apply.")
    else:
        st.error(f"promote_catalog exited {r.returncode} (rolled back).")


def render_capture(engine, dialect=None):
    """Capture the HIGH / READY documents into the cat_* mirrors (+ optional
    vault). This is the document half of the Capture stage; the deep / binary
    capture (LAS/DLIS/LIS/SEG-Y → las_catalog) runs alongside it in the same
    Capture tab. Lifting cat_* → dv_* happens later in Promote."""
    st.markdown("### 📄 Documents → cat_*")
    prom = _promotable(engine)
    st.metric("Ready to capture", len(prom))
    if prom.empty:
        st.caption("Nothing is READY — run triage, or resolve REVIEW items first.")
        return

    by_type = (prom.groupby("FILE_TYPE_GROUP").size()
               .reset_index(name="count")
               .rename(columns={"FILE_TYPE_GROUP": "Type", "count": "Files"}))
    st.dataframe(by_type, hide_index=True, use_container_width=True)

    c1, c2 = st.columns([3, 1])
    vault_root = c1.text_input("Vault root", VAULT_DEFAULT, key="promote_vault")
    do_vault = c2.checkbox("Copy to vault", value=True, key="promote_do_vault")

    if st.button(f"📄 Capture {len(prom)} document(s) → cat_*", type="primary",
                 key="promote_run"):
        with st.spinner("Capturing and vaulting…"):
            n, nv = _do_promote(engine, dialect, prom, vault_root, do_vault)
        st.success(f"Captured {n} file(s)" +
                   (f" · {nv} vaulted" if do_vault else " · vault skipped"))
        try:
            from dataview.file_catalog.page_workbench import _render_batch_report
            _render_batch_report()
        except Exception:
            pass


def render_promote(engine, dialect=None):
    """Lift the captured cat_* rows up into the golden dv_* tables. Capture (both
    documents and deep/binary) now lives in its own Capture tab; this is the
    move / lift step only."""
    st.markdown("## 🚀 Promote — Lift cat_* → dv_*")
    st.caption("Move captured rows into the golden dv_* tables — create/fill well "
               "headers, per-file-replace detail, then clear cat_*. Dry-run first "
               "to see the counts; apply to move. Safe to run anytime (only "
               "unpromoted rows move).")
    pc1, pc2 = st.columns(2)
    if pc1.button("🔍 Dry-run (preview)", key="promote_dv_dry",
                  use_container_width=True):
        _run_promote_cli(apply=False)
    if pc2.button("⬆️ Lift to dv_* (apply)", type="primary",
                  key="promote_dv_apply", use_container_width=True):
        _run_promote_cli(apply=True)
