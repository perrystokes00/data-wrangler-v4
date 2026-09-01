"""
fk_resolution.py — interactive, persisted FK value reconciliation.

Turns per-table FK *policy* into per-VALUE decisions a steward makes once and the
loader reuses forever. For each FK, the distinct source values are diffed against
the reference table; every unmatched value gets a decision:

    ADD    register the value as a new canonical code (seed it into the ref table)
    REMAP  crosswalk the source value to an existing canonical code
    NULL   load NULL for rows carrying this value

Decisions persist in dataview.dv_fk_resolution, keyed by (ref_table, source_value),
so they apply to every future load. At load time the loader applies REMAP/NULL as
staging UPDATEs and ADD as targeted seeds; anything still unresolved is what halts
(or quarantines) — nothing is silently admitted or nulled.

This is the v2 "Code Mappings" rules concept, generalized to Add/Remap/Null and
wired to the v3 mapping-registry loader.
"""
from __future__ import annotations

import difflib

from sqlalchemy import text as _t

STORE = "dataview.dv_fk_resolution"

_DDL = f"""
IF NOT EXISTS (SELECT 1 FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id
              WHERE s.name='dataview' AND t.name='dv_fk_resolution')
BEGIN
    CREATE TABLE {STORE} (
        resolution_id  int IDENTITY(1,1) PRIMARY KEY,
        ref_table      nvarchar(128) NOT NULL,
        fk_column      nvarchar(128) NULL,
        source_value   nvarchar(400) NOT NULL,
        action         nvarchar(10)  NOT NULL,          -- ADD | REMAP | NULL
        mapped_value   nvarchar(400) NULL,              -- canonical code for REMAP/ADD
        active_ind     nvarchar(1)   NOT NULL CONSTRAINT DF_fkres_active DEFAULT 'Y',
        resolved_by    nvarchar(128) NULL,
        resolved_date  datetime2     NOT NULL CONSTRAINT DF_fkres_date   DEFAULT (GETDATE()),
        CONSTRAINT UQ_dv_fk_resolution UNIQUE (ref_table, source_value)
    );
END
"""


def ensure_store(engine):
    """Create the resolution table if it doesn't exist yet."""
    with engine.begin() as con:
        con.execute(_t(_DDL))


# ── reads ────────────────────────────────────────────────────────────────────
def ref_values(engine, ref_table, ref_col):
    """Current canonical values in a reference table."""
    with engine.connect() as con:
        return [r[0] for r in con.execute(_t(
            f"SELECT DISTINCT [{ref_col}] FROM {ref_table} WHERE [{ref_col}] IS NOT NULL"))]


def get_resolutions(engine, ref_table):
    """Active decisions for a ref table -> {source_value_lower: {action, mapped_value}}."""
    with engine.connect() as con:
        rows = con.execute(_t(f"""
            SELECT source_value, action, mapped_value
            FROM   {STORE}
            WHERE  ref_table = :rt AND active_ind = 'Y'
        """), {"rt": ref_table}).fetchall()
    return {r.source_value.lower(): {"action": r.action, "mapped_value": r.mapped_value}
            for r in rows}


# ── engine (pure) ────────────────────────────────────────────────────────────
def analyze(source_values, ref_vals, resolutions):
    """Classify each distinct source value against the reference set + decisions.

    Returns a list of dicts, one per distinct non-empty source value:
        {source_value, status, action, mapped_value, suggestion}
      status = 'valid'      already a canonical code
             | 'resolved'   has a saved decision (ADD/REMAP/NULL)
             | 'unresolved' needs a steward decision (suggestion = closest code)
    """
    ref_lower = {v.lower(): v for v in ref_vals}
    out = []
    seen = set()
    for raw in source_values:
        if raw is None:
            continue
        v = str(raw).strip()
        if v == "" or v.lower() in seen:
            continue
        seen.add(v.lower())
        if v.lower() in ref_lower:
            out.append({"source_value": v, "status": "valid", "action": None,
                        "mapped_value": ref_lower[v.lower()], "suggestion": None})
        elif v.lower() in resolutions:
            r = resolutions[v.lower()]
            out.append({"source_value": v, "status": "resolved", "action": r["action"],
                        "mapped_value": r["mapped_value"], "suggestion": None})
        else:
            sugg = difflib.get_close_matches(v, list(ref_vals), n=1, cutoff=0.6)
            out.append({"source_value": v, "status": "unresolved", "action": None,
                        "mapped_value": None, "suggestion": sugg[0] if sugg else None})
    return sorted(out, key=lambda d: (d["status"] != "unresolved", d["source_value"].lower()))


# ── writes ───────────────────────────────────────────────────────────────────
def save_resolution(engine, ref_table, fk_column, source_value, action,
                    mapped_value=None, resolved_by="STEWARD"):
    """Upsert one decision, keyed by (ref_table, source_value)."""
    action = action.upper()
    assert action in ("ADD", "REMAP", "NULL"), action
    with engine.begin() as con:
        con.execute(_t(f"""
            MERGE {STORE} AS tgt
            USING (SELECT :rt AS ref_table, :sv AS source_value) AS src
              ON (tgt.ref_table = src.ref_table AND tgt.source_value = src.source_value)
            WHEN MATCHED THEN
                UPDATE SET action=:act, mapped_value=:mv, fk_column=:fc,
                           active_ind='Y', resolved_by=:by, resolved_date=GETDATE()
            WHEN NOT MATCHED THEN
                INSERT (ref_table, fk_column, source_value, action, mapped_value, resolved_by)
                VALUES (:rt, :fc, :sv, :act, :mv, :by);
        """), {"rt": ref_table, "fc": fk_column, "sv": source_value,
               "act": action, "mv": mapped_value, "by": resolved_by})


# ── apply (loader-side) ──────────────────────────────────────────────────────
def apply_to_stage(con, stg, fk_col, resolutions):
    """Apply REMAP/NULL decisions as staging UPDATEs on an open transaction.
    Returns the list of ADD canonical values to seed into the ref table.
    REMAP/NULL match the source value case-insensitively."""
    add_values = []
    for sv_lower, r in resolutions.items():
        act = r["action"]
        if act == "REMAP":
            con.execute(_t(f"UPDATE {stg} SET [{fk_col}] = :mv "
                           f"WHERE LOWER([{fk_col}]) = :sv"),
                        {"mv": r["mapped_value"], "sv": sv_lower})
        elif act == "NULL":
            con.execute(_t(f"UPDATE {stg} SET [{fk_col}] = NULL "
                           f"WHERE LOWER([{fk_col}]) = :sv"),
                        {"sv": sv_lower})
        elif act == "ADD":
            add_values.append(r["mapped_value"] or sv_lower)
    return add_values


# ── collect all violations for a table (one pass, before any grid) ───────────
def collect_violations(engine, fk_specs):
    """Gather EVERY unresolved FK code-value violation for the table in one pass,
    before presenting anything. fk_specs is one dict per controlled-vocab FK:
        {"ref_table","ref_col","fk_column","source_values": [...]}

    Returns:
      summary    [{fk_column, ref_table, valid, resolved, unresolved}, ...]
      violations [{field, ref_table, ref_col, source_value, suggestion}, ...]  (all FKs combined)
      ref_values {ref_table: [valid codes]}   (for save-time validation / pickers)
    """
    ensure_store(engine)
    summary, violations, refcache = [], [], {}
    for spec in fk_specs:
        rt, rc, fc = spec["ref_table"], spec["ref_col"], spec["fk_column"]
        cn = spec.get("constraint") or f"FK_{fc}"
        rv = refcache.setdefault(rt, ref_values(engine, rt, rc))
        rows = analyze(spec["source_values"], rv, get_resolutions(engine, rt))
        summary.append({
            "fk_column":  fc, "ref_table": rt, "constraint": cn,
            "valid":      sum(1 for r in rows if r["status"] == "valid"),
            "resolved":   sum(1 for r in rows if r["status"] == "resolved"),
            "unresolved": sum(1 for r in rows if r["status"] == "unresolved"),
        })
        for r in rows:
            if r["status"] == "unresolved":
                violations.append({"constraint": cn, "field": fc, "ref_table": rt,
                                   "ref_col": rc, "source_value": r["source_value"],
                                   "suggestion": r["suggestion"] or ""})
    return {"summary": summary, "violations": violations, "ref_values": refcache}


# ── Streamlit reconciliation grid (per-constraint grids, shown together) ─────
def render_reconciliation(st, engine, fk_specs, *, resolved_by="STEWARD", key_prefix=""):
    """Collect all FK violations for the table, then present one grid PER
    constraint, stacked together, with a single Save at the bottom. Each grid:

        Add (checkbox) | Unmatched value | Remap to (selectbox of THIS table's codes)

    Action derived per row: Add checked -> ADD; a code chosen -> REMAP;
    neither -> NULL. (Add wins if both.) Remap pre-fills the fuzzy-closest code.
    Returns the count of values presented (0 == nothing to reconcile)."""
    import pandas as pd

    data = collect_violations(engine, fk_specs)

    for s in data["summary"]:
        if s["unresolved"] == 0:
            continue                                  # hide fully-resolved constraints
        st.markdown(f"**{s['constraint']}**  ·  {s['fk_column']} → {s['ref_table']}  ·  "
                    f"{s['valid']} valid · {s['resolved']} resolved · "
                    f"{s['unresolved']} need a decision")

    viol = data["violations"]
    if not viol:
        st.success("No unresolved FK values for this table — ready to load.")
        return 0

    # group by constraint (each maps to one ref table + fk column)
    groups = {}
    for v in viol:
        g = groups.setdefault(v["constraint"],
                              {"ref_table": v["ref_table"], "field": v["field"], "rows": []})
        g["rows"].append(v)

    st.caption(f"{len(viol)} value(s) across {len(groups)} constraint(s) need a decision. "
               f"Check **Add** to register a value as a new code, pick a **Remap to** code "
               f"to crosswalk it, or leave both to NULL it.")

    edited_all = []
    for cn, g in groups.items():
        rt, fc = g["ref_table"], g["field"]
        st.markdown(f"**{cn}**  ·  {fc} → {rt}")
        opts = [""] + sorted(data["ref_values"][rt])

        # bulk actions — set every row at once, then fine-tune individually.
        # A version counter in the editor key forces a re-init when a bulk
        # action fires; between bulk actions per-row edits persist normally.
        ver_key = f"{key_prefix}recon_ver_{cn}"
        mode_key = f"{key_prefix}recon_mode_{cn}"
        st.session_state.setdefault(ver_key, 0)
        st.session_state.setdefault(mode_key, ("none", None))

        bc1, bc2, bc3, bc4 = st.columns([1.5, 1.8, 1.6, 1.1])
        if bc1.button("✓ Add all as new", key=f"{key_prefix}addall_{cn}"):
            st.session_state[mode_key] = ("add", None)
            st.session_state[ver_key] += 1
        bulk_code = bc2.selectbox("Remap all to", opts, key=f"{key_prefix}bulk_{cn}",
                                  label_visibility="collapsed")
        if bc3.button("→ Remap all", key=f"{key_prefix}remapall_{cn}"):
            if bulk_code:
                st.session_state[mode_key] = ("remap", bulk_code)
                st.session_state[ver_key] += 1
        if bc4.button("Clear", key=f"{key_prefix}clearall_{cn}"):
            st.session_state[mode_key] = ("none", None)
            st.session_state[ver_key] += 1

        mode, code = st.session_state[mode_key]
        rows = []
        for r in g["rows"]:
            if mode == "add":
                rows.append({"add": True, "source_value": r["source_value"],
                             "remap_to": ""})
            elif mode == "remap":
                rows.append({"add": False, "source_value": r["source_value"],
                             "remap_to": code})
            else:
                rows.append({"add": False, "source_value": r["source_value"],
                             "remap_to": r["suggestion"] or ""})
        df = pd.DataFrame(rows)
        edited = st.data_editor(
            df, key=f"{key_prefix}recon_{cn}_{st.session_state[ver_key]}",
            hide_index=True, use_container_width=True,
            column_config={
                "add":          st.column_config.CheckboxColumn("Add", help="Register as a new canonical code"),
                "source_value": st.column_config.TextColumn("Unmatched value", disabled=True),
                "remap_to":     st.column_config.SelectboxColumn(
                                    "Remap to", options=opts, required=False,
                                    help="Crosswalk to an existing code"),
            },
        )
        edited_all.append((cn, rt, fc, edited))

    if st.button("Save all decisions", key=f"{key_prefix}fkrecon_save"):
        counts, bad = {"ADD": 0, "REMAP": 0, "NULL": 0}, []
        for cn, rt, fc, edited in edited_all:
            valid = {x.lower() for x in data["ref_values"][rt]}
            for _, row in edited.iterrows():
                sv = row["source_value"]
                remap = str(row["remap_to"]).strip()
                if bool(row["add"]):
                    action, mv = "ADD", sv
                elif remap:
                    if remap.lower() not in valid:
                        bad.append(f"{cn}: {sv}→{remap}")
                        continue
                    action, mv = "REMAP", remap
                else:
                    action, mv = "NULL", None
                save_resolution(engine, rt, fc, sv, action, mv, resolved_by)
                counts[action] += 1
        if bad:
            st.warning("Skipped invalid remaps: " + "; ".join(bad))
        st.success(f"Saved — {counts['ADD']} added, {counts['REMAP']} remapped, "
                   f"{counts['NULL']} nulled. Re-run the load.")
    return len(viol)
