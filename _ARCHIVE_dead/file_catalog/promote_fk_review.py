"""
promote_fk_review.py — the promote-stage twin of the load-stage FK resolution panel.

WHY: promote HOLDS (parks) any cat_* row whose reference-FK value (source, depth_ouom,
volume_ouom, rate_ouom, curve_unit, ...) isn't present in its dv_r_* reference table,
so an unseeded code doesn't 547-crash the batch. Those held rows are otherwise invisible
except for a 'held N (unresolved ...)' line in the promote log. This surfaces them as a
reviewable Add/Map grid — the SAME governance UX as modules/fk_resolve_panel.py — so a
human resolves each unresolved code instead of chasing them per-run or blindly loading
every incoming value (which would defeat the reference tables).

Per unresolved value you choose:
  • Add  → seed the value into its dv_r_* reference table as new vocabulary.
  • Map  → rewrite the held cat_* rows to a chosen existing reference code.
  • neither → leave held (parked; resolve later).
After Apply, the previously-held rows become eligible and lift on the next promote.

Place in:  .../data_wrangler_v3/modules/promote_fk_review.py
Render from the Pipeline/Promote page:
    from dataview.file_catalog.promote_fk_review import render as render_promote_fk
    render_promote_fk(engine, st)     # engine = SQLAlchemy engine; st = streamlit
"""
from __future__ import annotations
import pandas as pd
from sqlalchemy import text

CAT_SCHEMA = "file_catalog"
DV_SCHEMA  = "dataview"

# Canonical UWI normalizer — matches promote_catalog._norm so 'eligible once seeded'
# lines up with what promote will actually move.
def _norm(col: str) -> str:
    stripped = (f"REPLACE(REPLACE(REPLACE(CONVERT(varchar(64),{col}),'-',''),' ',''),'/','')")
    return (f"(CASE WHEN NULLIF(LTRIM(RTRIM({stripped})),'') IS NULL THEN NULL "
            f"ELSE LEFT(LTRIM(RTRIM({stripped})) + '00000000000000', 14) END)")


# ── discovery ────────────────────────────────────────────────────────────────
def _cat_tables(con):
    """All cat_* mirror tables that have a PROMOTED column (the promotable ones)."""
    rows = con.execute(text(
        "SELECT t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id "
        "WHERE s.name=:sc AND t.name LIKE 'cat[_]%' "
        "AND EXISTS (SELECT 1 FROM sys.columns c WHERE c.object_id=t.object_id "
        "            AND c.name='PROMOTED')"), {"sc": CAT_SCHEMA}).fetchall()
    return [r[0] for r in rows]


def _dv_for_cat(cat: str) -> str:
    """cat_well_formation_top -> dv_well_formation_top (mirror naming)."""
    return "dv_" + cat[len("cat_"):]


def _ref_fks(con, dv_table: str):
    """Which columns of dv_table FK into a dv_r_* reference table.
    Returns [(local_col, ref_table, ref_col), ...] — same source of truth promote uses."""
    rows = con.execute(text(
        "SELECT cpa.name, rt.name, cref.name "
        "FROM sys.foreign_keys fk "
        "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id=fk.object_id "
        "JOIN sys.tables pt ON pt.object_id=fk.parent_object_id "
        "JOIN sys.schemas ps ON ps.schema_id=pt.schema_id "
        "JOIN sys.tables rt ON rt.object_id=fk.referenced_object_id "
        "JOIN sys.columns cpa ON cpa.object_id=fkc.parent_object_id AND cpa.column_id=fkc.parent_column_id "
        "JOIN sys.columns cref ON cref.object_id=fkc.referenced_object_id AND cref.column_id=fkc.referenced_column_id "
        "WHERE ps.name=:sc AND pt.name=:t AND rt.name LIKE 'dv[_]r[_]%'"),
        {"sc": DV_SCHEMA, "t": dv_table}).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _cat_has_col(con, cat: str, col: str) -> bool:
    return con.execute(text(
        "SELECT COUNT(*) FROM sys.columns c JOIN sys.tables t ON t.object_id=c.object_id "
        "JOIN sys.schemas s ON s.schema_id=t.schema_id "
        "WHERE s.name=:sc AND t.name=:t AND c.name=:c"),
        {"sc": CAT_SCHEMA, "t": cat, "c": col}).scalar() > 0


def _held_values(con, cat: str, local_col: str, ref_table: str, ref_col: str):
    """Distinct non-NULL values of cat.local_col (PROMOTED=0 rows) that are NOT in the
    reference table — i.e. the values holding rows — with the row count each."""
    q = text(
        f"SELECT LTRIM(RTRIM(CONVERT(varchar(128), m.[{local_col}]))) AS val, COUNT(*) AS n "
        f"FROM {CAT_SCHEMA}.[{cat}] m "
        f"WHERE m.PROMOTED = 0 AND m.[{local_col}] IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM {DV_SCHEMA}.[{ref_table}] r "
        f"               WHERE r.[{ref_col}] = m.[{local_col}]) "
        f"GROUP BY LTRIM(RTRIM(CONVERT(varchar(128), m.[{local_col}]))) "
        f"ORDER BY n DESC")
    return [(r[0], r[1]) for r in con.execute(q).fetchall()]


def _ref_existing(con, ref_table: str, ref_col: str):
    """Existing codes in a reference table (for the Map dropdown)."""
    rows = con.execute(text(
        f"SELECT DISTINCT LTRIM(RTRIM(CONVERT(varchar(128),[{ref_col}]))) "
        f"FROM {DV_SCHEMA}.[{ref_table}] WHERE [{ref_col}] IS NOT NULL "
        f"ORDER BY 1")).fetchall()
    return [r[0] for r in rows if r[0]]


def _ref_insertable_cols(con, ref_table: str):
    """Non-computed, non-identity columns we can insert into (for Add)."""
    rows = con.execute(text(
        "SELECT c.name, c.is_nullable, ty.name AS typ "
        "FROM sys.columns c JOIN sys.tables t ON t.object_id=c.object_id "
        "JOIN sys.schemas s ON s.schema_id=t.schema_id "
        "JOIN sys.types ty ON ty.user_type_id=c.user_type_id "
        "WHERE s.name=:sc AND t.name=:t AND c.is_identity=0 AND c.is_computed=0 "
        "ORDER BY c.column_id"), {"sc": DV_SCHEMA, "t": ref_table}).fetchall()
    return [(r[0], bool(r[1]), r[2]) for r in rows]


# ── the panel ────────────────────────────────────────────────────────────────
def _collect(con):
    """Build the list of held-reference groups across all promotable cat_ tables.
    Each group: one (cat, local_col, ref_table, ref_col) with its held values."""
    groups = []
    for cat in _cat_tables(con):
        dv = _dv_for_cat(cat)
        for local_col, ref_table, ref_col in _ref_fks(con, dv):
            if not _cat_has_col(con, cat, local_col):
                continue
            held = _held_values(con, cat, local_col, ref_table, ref_col)
            if not held:
                continue
            groups.append(dict(
                cat=cat, dv=dv, local_col=local_col,
                ref_table=ref_table, ref_col=ref_col,
                held=held,
                existing=_ref_existing(con, ref_table, ref_col),
                insertable=_ref_insertable_cols(con, ref_table)))
    return groups


def render(engine, st):
    """Render the promote-stage FK review grid. Safe to call every rerun."""
    _inject_style(st)
    st.markdown("<div class='pfk-head'>Promote — reference FK review</div>",
                unsafe_allow_html=True)
    st.caption("Rows held by promote because a reference code isn't in its dv_r_* table. "
               "Add the code as new vocabulary, or map it to an existing code. "
               "Resolved rows promote on the next run.")

    try:
        with engine.connect() as con:
            groups = _collect(con)
    except Exception as exc:
        st.error(f"Could not scan held rows: {exc}")
        return

    if not groups:
        st.success("✅ No held reference values — every promotable row's reference "
                   "codes are present. Nothing to resolve.")
        return

    total_vals = sum(len(g["held"]) for g in groups)
    total_rows = sum(n for g in groups for _, n in g["held"])
    st.markdown(f"<div class='pfk-count'>🔴 {total_vals} unresolved code(s) holding "
                f"{total_rows:,} row(s) across {len(groups)} column(s)</div>",
                unsafe_allow_html=True)

    editors = []
    with st.form(key="promote_fk_form"):
        for gi, g in enumerate(groups):
            vals = [v for v, _ in g["held"]]
            cnts = {v: n for v, n in g["held"]}
            # pre-fill Map if a case-insensitive match already exists in the ref
            exist_upper = {e.upper(): e for e in g["existing"]}
            pre_existing = [exist_upper.get(v.upper(), "") for v in vals]
            pre_map      = [bool(exist_upper.get(v.upper())) for v in vals]
            options = [""] + g["existing"]

            with st.expander(
                    f"🔴 {g['cat']}  ·  [{g['local_col']}] → {g['ref_table']}  ·  "
                    f"{len(vals)} code(s)  ·  {sum(cnts.values()):,} row(s)"
                    + (f"  ·  {sum(pre_map)} case-match pre-filled" if any(pre_map) else ""),
                    expanded=True):
                st.markdown(
                    f"<span class='pfk-sub'>Held <code>{g['cat']}.{g['local_col']}</code> → "
                    f"reference <code>{g['ref_table']}.{g['ref_col']}</code>  ·  "
                    f"{len(g['existing'])} existing code(s)</span>",
                    unsafe_allow_html=True)
                editor_df = pd.DataFrame({
                    "Add":            [False] * len(vals),
                    "Held code":      vals,
                    "Rows":           [cnts[v] for v in vals],
                    "Existing code":  pre_existing,
                    "Map":            pre_map,
                })
                ekey = f"pfk_{gi}_{g['cat']}_{g['local_col']}"
                ret = st.data_editor(
                    editor_df, key=ekey, use_container_width=True, hide_index=True,
                    column_config={
                        "Add": st.column_config.CheckboxColumn(
                            width="small",
                            help="Seed this code into the reference table as new vocabulary."),
                        "Held code": st.column_config.TextColumn(disabled=True, width="medium"),
                        "Rows": st.column_config.NumberColumn(disabled=True, width="small"),
                        "Existing code": st.column_config.SelectboxColumn(
                            width="medium", options=options,
                            help="Map the held rows to this existing reference code."),
                        "Map": st.column_config.CheckboxColumn(
                            width="small",
                            help="Apply the mapping: rewrite the held cat_ rows to the chosen code."),
                    })
                editors.append((g, editor_df, ekey, ret))

        submitted = st.form_submit_button(
            f"✅ Apply all resolutions  ({len(groups)} column(s))",
            use_container_width=True, type="primary")

    if not submitted:
        return

    results, any_change = [], False
    for g, base_df, ekey, ret in editors:
        edited = ret if (isinstance(ret, pd.DataFrame) and "Add" in ret.columns) \
            else _read_editor_state(st, base_df, ekey)
        changed, msgs = _apply(engine, g, edited)
        any_change = any_change or changed
        results.extend((f"{g['cat']}.{g['local_col']}", m) for m in msgs)

    for where, m in results:
        (st.success if m.startswith(("✅", "🔤")) else st.warning)(f"{where}: {m}")
    if any_change:
        st.info("Resolved codes seeded/mapped. Re-run promote to lift the "
                "previously-held rows into dv_*.")


# ── apply: Add (seed ref) / Map (remap cat rows) ─────────────────────────────
def _apply(engine, g, edited):
    """Seed new codes and/or remap held cat_ rows for ONE held column.
    Returns (changed, [msg, ...]). No rerun — caller flashes messages."""
    ref_table, ref_col = g["ref_table"], g["ref_col"]
    cat, local_col     = g["cat"], g["local_col"]
    insertable         = g["insertable"]

    add_rows, remaps, conflicts = [], [], []
    for _, r in edited.iterrows():
        val    = str(r["Held code"]).strip()
        add    = bool(r.get("Add", False))
        sel    = str(r.get("Existing code", "") or "").strip()
        mapchk = bool(r.get("Map", False))
        do_map = mapchk and bool(sel)
        if add and do_map:
            conflicts.append(f"'{val}' — both Add and Map ticked"); continue
        if mapchk and not sel:
            conflicts.append(f"'{val}' — Map ticked, no code chosen"); continue
        if do_map:
            if sel != val:
                remaps.append((val, sel))
        elif add:
            add_rows.append(val)

    msgs, changed = [], False
    if conflicts:
        msgs.append("⚠️ Skipped: " + "; ".join(conflicts))

    # ADD: insert each new code into the reference table (idempotent — skip if the
    # code already exists, so a double-click or overlap can't error).
    if add_rows:
        try:
            with engine.begin() as con:
                for val in add_rows:
                    exists = con.execute(text(
                        f"SELECT 1 FROM {DV_SCHEMA}.[{ref_table}] "
                        f"WHERE [{ref_col}] = :v"), {"v": val}).fetchone()
                    if exists:
                        continue
                    payload = _ref_insert_payload(ref_col, val, insertable)
                    cols = ", ".join(f"[{c}]" for c in payload)
                    prms = ", ".join(f":{c}" for c in payload)
                    con.execute(text(
                        f"INSERT INTO {DV_SCHEMA}.[{ref_table}] ({cols}) VALUES ({prms})"),
                        payload)
            msgs.append(f"✅ Added {len(add_rows)} code(s) to {ref_table}.")
            changed = True
        except Exception as e:
            msgs.append(f"⚠️ Add failed: {e}")

    # MAP: rewrite the held cat_ rows to the chosen existing code (PROMOTED=0 only).
    if remaps:
        try:
            with engine.begin() as con:
                for old, new in remaps:
                    con.execute(text(
                        f"UPDATE {CAT_SCHEMA}.[{cat}] SET [{local_col}] = :new "
                        f"WHERE PROMOTED = 0 AND LTRIM(RTRIM(CONVERT(varchar(128),"
                        f"[{local_col}]))) = :old"),
                        {"new": new, "old": old})
            msgs.append(f"✅ Mapped {len(remaps)} code(s) → existing reference.")
            changed = True
        except Exception as e:
            msgs.append(f"⚠️ Map failed: {e}")

    if not add_rows and not remaps and not conflicts:
        msgs.append("Nothing ticked — left held.")
    return changed, msgs


def _ref_insert_payload(ref_col, val, insertable):
    """Build a minimal valid INSERT payload for a dv_r_* row: the PK/code = val, a
    name/description column = val where present, active_ind='Y', audit stamps. Only
    columns that actually exist (insertable) are included; NOT NULL string cols
    without a better value get val."""
    names = {c.lower(): c for c, _null, _ty in insertable}
    payload = {ref_col: val}
    for cand in ("short_name", "unit_of_measure", "long_name", "uom_description"):
        if cand in names:
            payload[names[cand]] = val
    if "active_ind" in names:
        payload[names["active_ind"]] = "Y"
    if "row_created_by" in names:
        payload[names["row_created_by"]] = "FK_REVIEW"
    if "row_created_date" in names:
        # let SQL default; if it's NOT NULL with no default we set GETDATE via literal
        pass
    # fill any remaining NOT NULL string columns with val so the insert can't fail
    for c, nullable, ty in insertable:
        if c in payload or c == ref_col:
            continue
        if not nullable and ty in ("nvarchar","varchar","char","nchar"):
            payload[c] = val
    return payload


# ── editor state fallback (form-safe), mirrors fk_resolve_panel ──────────────
def _read_editor_state(st, base_df, key):
    edited = base_df.copy()
    state = st.session_state.get(key)
    if isinstance(state, dict) and "edited_rows" in state:
        for ridx, chg in state["edited_rows"].items():
            try: ridx = int(ridx)
            except Exception: continue
            for col, v in chg.items():
                if col in edited.columns and 0 <= ridx < len(edited):
                    edited.iat[ridx, edited.columns.get_loc(col)] = v
    return edited


def _inject_style(st):
    st.markdown("""<style>
      .pfk-head{font-size:1.15rem;font-weight:700;margin:.2rem 0 .1rem;}
      .pfk-count{color:#b45309;font-weight:600;margin:.2rem 0 .4rem;}
      .pfk-sub{color:#6b7280;font-size:.85rem;}
    </style>""", unsafe_allow_html=True)
