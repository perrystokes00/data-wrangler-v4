"""
dataview/migration/page_migrate.py
=================================
Mapping bench for the DataView -> PPDM migration.

WHY A BENCH AND NOT A PIPELINE
------------------------------
Migration is batch work: a dozen-odd tables, each needing the same short recipe.
Walking File Import's eight interactive stages per table would be slower than
the command line, which is why this deliberately isn't a pipeline mode.

What actually costs time is one narrow thing: reading a 30-to-70 column target
schema and deciding which source column pairs with which target column. Steps
that are already instant at the CLI (staging, dry running, applying) stay one
button; the expensive step gets the whole screen.

So the layout is three lists side by side — what matched, what's left on the
source, what's left on the target — and pairing two of them writes a synonym.
That replaces `ppdm_model --table X` for reading the schema and
`synonyms --add X TGT src` for recording the decision, which between them are
the entire bottleneck.

EVERYTHING ELSE STAYS CLI
-------------------------
This page calls the same functions the CLI does — db_source.stage_from_table,
synonyms.build_mapping_with_synonyms, promote_ppdm.promote. No logic lives
here that isn't reachable from a terminal, so anything demonstrated here is
scriptable and anything scripted is visible here.

PERFORMANCE NOTES, LEARNED THE HARD WAY
---------------------------------------
* st.radio, not st.tabs — Streamlit executes EVERY tab body on every rerun, so
  tabs would re-reflect the target schema on each click.
* Reflection is cached and invalidated by an explicit nonce, bumped only when
  something is actually written. Streamlit reruns constantly; the database
  shouldn't.
* The target picker is fed from the domain index (a JSON file), never from a
  live query — PPDM39 has 2,696 tables and shipping that list to the browser
  on every render is felt.
"""
from __future__ import annotations

import streamlit as st

from dataview.migration import db_source as ds
from dataview.migration import promote_ppdm as pp
from dataview.migration import synonyms as syn
from dataview.migration import column_rules as cr
from dataview.migration.ppdm_model import get_ppdm_schema, domain_of, DOMAINS

DEFAULTS = {
    "server":     r"localhost\SQLEXPRESS",
    "src_db":     "DataView_Demo",
    "src_schema": "dataview",
    "stg_schema": "stg",
    "tgt_db":     "PPDM39",
    "tgt_schema": "dbo",
}


# --------------------------------------------------------------------------- #
# Connections and cached reflection
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def _engine(server: str, database: str):
    """One engine per (server, database). cache_resource, not cache_data —
    connections aren't serialisable and shouldn't be copied per session."""
    from dataview.core.schema_introspect import make_engine
    return make_engine(server, database)


def _nonce() -> int:
    return st.session_state.get("mig_nonce", 0)


def _bump():
    st.session_state["mig_nonce"] = _nonce() + 1


@st.cache_data(show_spinner="Reading source tables…")
def _source_tables(server, src_db, src_schema, nonce):
    return ds.list_source_tables(_engine(server, src_db), src_schema)


@st.cache_data(show_spinner="Reading source columns…")
def _source_columns(server, src_db, src_schema, table, nonce):
    return ds.source_columns(_engine(server, src_db), table, src_schema)


@st.cache_data(show_spinner="Building the PPDM target model (one-time)…")
def _target_model(server, tgt_db, tgt_schema, nonce):
    """PPDMSchema for the target. First call reflects ~423 tables and takes a
    few minutes; after that it comes off the pickle cache on disk."""
    sch = get_ppdm_schema(_engine(server, tgt_db), tgt_schema)
    return {name: [(c.column_name, c.data_type, c.not_null, c.is_primary_key,
                    c.is_foreign_key, c.fk_table_name)
                   for c in td.columns]
            for name, td in sch.tables.items()}


def _col_defs(model, table):
    """Rehydrate ColumnDef objects — build_mapping needs the real dataclass,
    but the cache can only hold plain tuples."""
    from dataview.core.schema import ColumnDef
    return [ColumnDef(table_schema="", table_name=table, column_name=n,
                      data_type=t, not_null=nn, is_primary_key=pk,
                      is_foreign_key=fk, fk_table_schema=None,
                      fk_table_name=fkt, fk_column_name=None,
                      check_constraints=[])
            for n, t, nn, pk, fk, fkt in model.get(table.lower(), [])]


def _annotate(c) -> str:
    """Target column label carrying the things that decide a mapping: whether
    it's mandatory, whether it's a key, and what it points at."""
    bits = []
    if c.is_primary_key:
        bits.append("PK")
    if c.not_null:
        bits.append("NOT NULL")
    if c.is_foreign_key and c.fk_table_name:
        bits.append(f"→{c.fk_table_name}")
    tail = f"  [{', '.join(bits)}]" if bits else ""
    return f"{c.column_name}  ({c.data_type}){tail}"


# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #
def _connections():
    with st.expander("Connections", expanded=not st.session_state.get("mig_ok")):
        c1, c2 = st.columns(2)
        c1.markdown("**Source**")
        server = c1.text_input("Server", DEFAULTS["server"], key="mig_server")
        src_db = c1.text_input("Database", DEFAULTS["src_db"], key="mig_srcdb")
        src_schema = c1.text_input("Schema", DEFAULTS["src_schema"],
                                   key="mig_srcsch")
        stg_schema = c1.text_input("Staging schema", DEFAULTS["stg_schema"],
                                   key="mig_stgsch")

        c2.markdown("**Target**")
        tgt_db = c2.text_input("Database", DEFAULTS["tgt_db"], key="mig_tgtdb")
        tgt_schema = c2.text_input("Schema", DEFAULTS["tgt_schema"],
                                   key="mig_tgtsch")
        c2.caption("Source and target must be on the same SQL Server instance "
                   "— the load writes across databases in one transaction "
                   "using three-part names, with no linked server.")

        if st.button("Connect", key="mig_connect", type="primary"):
            try:
                eng = _engine(server, src_db)
                from sqlalchemy import text
                with eng.connect() as conn:
                    conn.execute(text("SELECT 1"))
                    vis = conn.execute(
                        text(f"SELECT DB_ID('{tgt_db}')")).scalar()
                if vis is None:
                    st.error(f"{tgt_db} isn't visible from {src_db} — are they "
                             f"on the same instance?")
                else:
                    st.session_state["mig_ok"] = True
                    _bump()
                    st.rerun()
            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")

    return (st.session_state.get("mig_server", DEFAULTS["server"]),
            st.session_state.get("mig_srcdb", DEFAULTS["src_db"]),
            st.session_state.get("mig_srcsch", DEFAULTS["src_schema"]),
            st.session_state.get("mig_stgsch", DEFAULTS["stg_schema"]),
            st.session_state.get("mig_tgtdb", DEFAULTS["tgt_db"]),
            st.session_state.get("mig_tgtsch", DEFAULTS["tgt_schema"]))


def _pick_tables(server, src_db, src_schema, tgt_db, tgt_schema):
    tables = _source_tables(server, src_db, src_schema, _nonce())
    if not tables:
        st.warning(f"No tables with data in {src_db}.{src_schema}")
        return None, None

    model = _target_model(server, tgt_db, tgt_schema, _nonce())

    c1, c2, c3 = st.columns([3, 2, 3])
    labels = [f"{n}   ({r:,} rows)" for n, r in tables]
    pick = c1.selectbox("Source table", labels, key="mig_src_table")
    src = tables[labels.index(pick)][0]

    # Default the domain from the source table's own name, so choosing
    # dv_seis_set lands you in SEISMIC without a second decision.
    guess = domain_of(src[3:] if src.lower().startswith("dv_") else src)
    doms = ["(all)"] + list(DOMAINS)
    dom = c2.selectbox("Target domain", doms,
                       index=doms.index(guess) if guess in doms else 0,
                       key="mig_domain")

    names = sorted(model.keys())
    if dom != "(all)":
        names = [n for n in names if domain_of(n) == dom]
    # Same-name match is right often enough to be worth defaulting to.
    stem = src[3:].lower() if src.lower().startswith("dv_") else src.lower()
    idx = names.index(stem) if stem in names else 0
    tgt = c3.selectbox(f"Target table  ({len(names)} in scope)", names,
                       index=idx, key="mig_tgt_table") if names else None
    return src, tgt


def _bench(server, src_db, src_schema, stg_schema, tgt_db, tgt_schema,
           src, tgt):
    model = _target_model(server, tgt_db, tgt_schema, _nonce())
    tcols = _col_defs(model, tgt)
    if not tcols:
        st.error(f"{tgt} has no columns in the target model")
        return None

    scols = [c for c, _t in _source_columns(server, src_db, src_schema, src,
                                            _nonce())]
    cm = cr.build_mapping_with_rules(tgt, tcols, scols)
    mapped = [m for m in cm.mapped if getattr(m, "source_col", "")]
    used_src = {m.source_col.upper() for m in mapped}
    used_tgt = {m.ppdm_col.upper() for m in mapped}

    unmapped_src = [c for c in scols if c.upper() not in used_src]
    # Order the leftovers by how much they matter: keys, then mandatory, then
    # foreign keys, then everything else.
    rest = [c for c in tcols if c.column_name.upper() not in used_tgt]
    rest.sort(key=lambda c: (not c.is_primary_key, not c.not_null,
                             not c.is_foreign_key, c.column_name))

    # A PK component with no source is the failure that bit well_log: the
    # insert dies on NOT NULL before dedupe ever matters. Say so up front.
    missing_pk = [c.column_name for c in tcols
                  if c.is_primary_key and c.column_name.upper() not in used_tgt]
    if missing_pk:
        st.error(f"Primary key component(s) unmapped: {', '.join(missing_pk)} "
                 f"— the insert will fail until these are paired.")

    st.caption(f"{len(tcols)} target column(s) · {len(scols)} source "
               f"column(s) · **{len(mapped)} mapped**")

    a, b, c = st.columns(3)
    rules = cr.rules_for(tgt)
    with a:
        st.markdown("**Mapped**")
        for m in mapped:
            st.text(f"{m.ppdm_col}\n   ← {m.source_col}")
        if rules:
            st.markdown("**Rules**")
            for col, r in sorted(rules.items()):
                k, v = next(iter(r.items()))
                st.text(f"{col}\n   {k}: {v}")
    with b:
        st.markdown(f"**Source, unplaced ({len(unmapped_src)})**")
        pick_s = st.radio("source", unmapped_src, key=f"mig_ps_{src}_{tgt}",
                          label_visibility="collapsed") if unmapped_src else None
    with c:
        st.markdown(f"**Target, unfilled ({len(rest)})**")
        opts = [_annotate(x) for x in rest]
        pick_t = st.selectbox("target", opts, key=f"mig_pt_{src}_{tgt}",
                              label_visibility="collapsed") if opts else None

    if pick_t:
        _set_rule(tgt, rest[opts.index(pick_t)])

    if pick_s and pick_t:
        tgt_col = rest[opts.index(pick_t)].column_name
        b1, b2 = st.columns(2)
        if b1.button(f"Pair  {tgt_col}  ←  {pick_s}", key="mig_pair",
                     type="primary", use_container_width=True):
            syn.add(tgt, tgt_col, pick_s)
            _bump()
            st.rerun()
        if b2.button(f"Pair globally (every table)", key="mig_pair_g",
                     use_container_width=True):
            syn.add(None, tgt_col, pick_s)
            _bump()
            st.rerun()
    return cm


def _set_rule(tgt, col):
    """Give the selected TARGET column a value that doesn't come from the
    source — a constant, a SQL expression, or a transform on whatever does
    feed it. This is the 'no source column exists for this' case: a fixed
    SOURCE code, a generated key, a timestamp."""
    with st.expander(f"Set a value for {col.column_name} "
                     f"(no source needed)", expanded=False):
        kind = st.radio("Kind", ["const", "expr", "transform"],
                        horizontal=True, key=f"mig_rk_{tgt}_{col.column_name}")
        val = ""
        if kind == "const":
            val = st.text_input("Value", key=f"mig_rv_{tgt}_{col.column_name}")
        elif kind == "expr":
            preset = st.selectbox("Preset", ["(write my own)"]
                                  + list(cr.PRESETS),
                                  key=f"mig_rp_{tgt}_{col.column_name}")
            default = cr.PRESETS.get(preset, "")
            val = st.text_input("SQL expression", value=default,
                                key=f"mig_re_{tgt}_{col.column_name}")
            if "ROW_NUMBER" in (val or "") and "+" not in (val or ""):
                st.warning("ROW_NUMBER restarts at 1 every batch — a second "
                           "load would re-issue numbers this table already "
                           "holds. For a durable key use NEWID(), or offset "
                           "from the target's current maximum.")
        else:
            val = st.selectbox("Transform", ["UPPER", "LOWER", "TRIM"],
                               key=f"mig_rt_{tgt}_{col.column_name}")

        c1, c2, c3 = st.columns(3)
        if c1.button("Set here", key=f"mig_rs_{tgt}_{col.column_name}") and val:
            cr.set_rule(tgt, col.column_name, kind, val)
            _bump()
            st.rerun()
        if c2.button("Set globally", key=f"mig_rg_{tgt}_{col.column_name}") and val:
            cr.set_rule(None, col.column_name, kind, val)
            _bump()
            st.rerun()
        if c3.button("Clear", key=f"mig_rc_{tgt}_{col.column_name}"):
            cr.clear_rule(tgt, col.column_name)
            cr.clear_rule(None, col.column_name)
            _bump()
            st.rerun()


def _refs_and_run(server, src_db, src_schema, stg_schema, tgt_db, tgt_schema,
                  src, tgt, cm):
    eng = _engine(server, src_db)
    stg = ds.stg_table_name(src)

    st.divider()
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("① Stage", key="mig_stage", use_container_width=True):
        res = ds.stage_from_table(eng, src, src_schema, stg_schema)
        (st.success if res.ok else st.error)(res.message)
        _bump()

    # Reference checks need the staged table, so they're a button rather than
    # something that runs on every render.
    if c2.button("② Check references", key="mig_refs",
                 use_container_width=True):
        out = []
        try:
            with eng.connect() as conn:
                for g in pp.check_reference_codes(conn, tgt, tgt_db, tgt_schema):
                    state = ("ok" if not g["missing"]
                             else "MISSING " + ", ".join(g["missing"]))
                    out.append(f"{g['via']} -> {g['ref_table']} "
                               f"({g['present']} present) : {state}")
                for g in pp.check_data_refs(conn, stg, cm, tgt, stg_schema,
                                            tgt_db, tgt_schema):
                    kind = f" [{g.get('kind', 'data')}]"
                    out.append(f"{g['column']} -> {g['ref_table']} : "
                               f"{len(g['missing'])} unregistered{kind}  "
                               + ", ".join(g["sample"]))
        except Exception as e:
            out.append(f"!! {type(e).__name__}: {e}")
        st.session_state["mig_refout"] = out or ["every value resolves"]

    seed = c3.checkbox("seed missing codes", key="mig_seed")

    dry = c4.button("③ Dry run", key="mig_dry", use_container_width=True)
    app = st.button("④ Promote to PPDM (apply)", key="mig_apply",
                    type="primary", use_container_width=True)

    if st.session_state.get("mig_refout"):
        st.code("\n".join(st.session_state["mig_refout"]))

    if dry or app:
        out = []
        try:
            if seed:
                conn = eng.connect()
                t = conn.begin()
                try:
                    n = pp.seed_data_refs(conn, stg, cm, tgt, stg_schema,
                                          tgt_db, tgt_schema, log=out.append)
                    out.append(f"registered {n} value(s)")
                    t.commit() if app else t.rollback()
                finally:
                    conn.close()
                if not app:
                    out.append("NOTE: dry run rolled the registrations back, "
                               "so the load below may still fail those FKs.")
            res = pp.promote(eng, stg, tgt, cm, stg_schema, tgt_db, tgt_schema,
                             apply=app, seed_refs=seed, log=out.append)
            out.append(res.get("message", ""))
        except Exception as e:
            out.append(f"!! {type(e).__name__}: {e}")
        st.code("\n".join(str(o) for o in out))
        if app:
            _bump()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def render(engine=None, dialect=None):
    st.markdown("## ⇄ DataView → PPDM migration")
    server, src_db, src_schema, stg_schema, tgt_db, tgt_schema = _connections()

    if not st.session_state.get("mig_ok"):
        st.info("Set the source and target above, then press Connect.")
        return

    st.caption(f"**{src_db}.{src_schema}**  →  **{tgt_db}.{tgt_schema}**  "
               f"· staging in `{stg_schema}` · synonyms in "
               f"`{syn.SYNONYMS_PATH.name}`")

    src, tgt = _pick_tables(server, src_db, src_schema, tgt_db, tgt_schema)
    if not src or not tgt:
        return

    st.divider()
    cm = _bench(server, src_db, src_schema, stg_schema, tgt_db, tgt_schema,
                src, tgt)
    if cm is not None:
        _refs_and_run(server, src_db, src_schema, stg_schema, tgt_db,
                      tgt_schema, src, tgt, cm)


run = render
