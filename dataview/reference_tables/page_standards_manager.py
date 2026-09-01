"""
page_standards_manager.py
=========================
DataView v3 — Standards Manager

Manages canonical reference values for:
  - dv_r_well_type    (well type codes)
  - dv_r_well_status  (well status codes)
  - dv_r_uom          (units of measure)
  - dv_r_source       (data sources)
  - dv_r_ba_type      (business associate types)

All dataset loads must map TO these canonical values.
No new codes added during load — only mapped to existing ones.

Philosophy:
  - Reference tables are the single source of truth
  - New codes must be explicitly added here by a data steward
  - Deactivated codes stay for history but can't be used in new loads
"""
from __future__ import annotations
from dataview.reference_tables.ref_seeder import render_reference_seeder

import streamlit as st
import pandas as pd
from sqlalchemy import text
from pathlib import Path
from datetime import datetime

# ── Reference table definitions ───────────────────────────────────────
REFERENCE_TABLES = {
    "Well Type": {
        "table":   "dataview.dv_r_well_type",
        "pk":      "well_type",
        "cols":    ["well_type", "short_name", "long_name", "remark"],
        "icon":    "🛢",
        "desc":    "Canonical well type codes used across all datasets",
        "defaults": [
            ("OIL",         "Oil",          "Oil producer",                 ""),
            ("GAS",         "Gas",          "Gas producer",                 ""),
            ("OIL_GAS",     "Oil/Gas",      "Oil and gas producer",         ""),
            ("WATER_INJ",   "Water Inj",    "Water injection well",         ""),
            ("WATER_DISP",  "Water Disp",   "Water disposal well",          ""),
            ("GAS_INJ",     "Gas Inj",      "Gas injection well",           ""),
            ("DRY",         "Dry",          "Dry hole",                     ""),
            ("CBM",         "CBM",          "Coal bed methane",             ""),
            ("CORE",        "Core",         "Core hole",                    ""),
            ("MONITOR",     "Monitor",      "Monitoring well",              ""),
            ("STRATIGRAPHIC","Strat",       "Stratigraphic test",           ""),
            ("SERVICE",     "Service",      "Service well",                 ""),
            ("UNKNOWN",     "Unknown",      "Type not determined",          ""),
        ],
    },
    "Well Status": {
        "table":   "dataview.dv_r_well_status",
        "pk":      "well_status",
        "cols":    ["well_status", "short_name", "long_name", "remark"],
        "icon":    "📊",
        "desc":    "Canonical well status codes used across all datasets",
        "defaults": [
            ("ACTIVE",      "Active",       "Currently producing or active",""),
            ("INACTIVE",    "Inactive",     "Not currently producing",      ""),
            ("ABANDONED",   "Abandoned",    "Permanently abandoned",        ""),
            ("DRILLING",    "Drilling",     "Currently being drilled",      ""),
            ("COMPLETED",   "Completed",    "Drilling complete",            ""),
            ("SUSPENDED",   "Suspended",    "Temporarily suspended",        ""),
            ("PERMITTED",   "Permitted",    "Permit issued, not spudded",   ""),
            ("RECOMPLETED", "Recompleted",  "Recompleted in new zone",      ""),
            ("PLUGGED",     "Plugged",      "Plugged and abandoned",        ""),
            ("UNKNOWN",     "Unknown",      "Status not determined",        ""),
        ],
    },
    "Unit of Measure": {
        "table":   "dataview.dv_r_uom",
        "pk":      "uom_code",
        "cols":    ["uom_code", "unit_of_measure", "uom_type",
                    "uom_description"],
        "icon":    "📏",
        "desc":    "Canonical units of measure — depth, pressure, volume etc.",
        "defaults": [
            ("FT",   "Feet",              "LENGTH",   "Imperial length"),
            ("M",    "Metres",            "LENGTH",   "SI length"),
            ("KB",   "Kelly Bushing",     "DATUM",    "Depth datum"),
            ("GL",   "Ground Level",      "DATUM",    "Depth datum"),
            ("MSL",  "Mean Sea Level",    "DATUM",    "Depth datum"),
            ("DF",   "Derrick Floor",     "DATUM",    "Depth datum"),
            ("BBLS", "Barrels",           "VOLUME",   "Imperial volume"),
            ("MCF",  "Thousand Cu Ft",    "VOLUME",   "Gas volume"),
            ("MMCF", "Million Cu Ft",     "VOLUME",   "Gas volume"),
            ("PSI",  "Pounds/Sq Inch",    "PRESSURE", "Imperial pressure"),
            ("KPA",  "Kilopascals",       "PRESSURE", "SI pressure"),
            ("DEGF", "Degrees Fahrenheit","TEMP",     "Temperature"),
            ("DEGC", "Degrees Celsius",   "TEMP",     "Temperature"),
        ],
    },
    "Source": {
        "table":   "dataview.dv_r_source",
        "pk":      "source",
        "cols":    ["source", "short_name", "long_name", "remark"],
        "icon":    "📡",
        "desc":    "Data source identifiers — one per dataset/agency",
        "defaults": [
            ("KGS",         "KGS",      "Kansas Geological Survey",         ""),
            ("RRC",         "RRC",      "Texas Railroad Commission",         ""),
            ("AER",         "AER",      "Alberta Energy Regulator",         ""),
            ("BOEM",        "BOEM",     "Bureau of Ocean Energy Mgmt",      ""),
            ("COGCC",       "COGCC",    "Colorado Oil & Gas Commission",     ""),
            ("PPDM",        "PPDM",     "PPDM Association",                 ""),
            ("DV_IMPORTER", "DV IMP",   "DataView Importer",                ""),
            ("SYSTEM",      "System",   "System generated",                 ""),
        ],
    },
}


# ── DB helpers ────────────────────────────────────────────────────────

def _get_all(engine, table: str, pk: str) -> pd.DataFrame:
    try:
        with engine.connect() as con:
            return pd.read_sql(
                text(f"SELECT * FROM {table} ORDER BY {pk}"), con)
    except Exception:
        return pd.DataFrame()


def _upsert(engine, table: str, pk: str, row: dict,
            who: str = "STANDARDS_MGR") -> tuple[bool, str]:
    try:
        with engine.begin() as con:
            cols    = list(row.keys())
            pk_val  = row[pk]
            set_sql = ", ".join(
                f"[{c}] = :{c}" for c in cols if c != pk)
            ins_cols = ", ".join(f"[{c}]" for c in cols)
            ins_vals = ", ".join(f":{c}" for c in cols)
            con.execute(text(f"""
                IF EXISTS (SELECT 1 FROM {table} WHERE [{pk}] = :{pk})
                    UPDATE {table}
                    SET {set_sql},
                        row_changed_by   = '{who}',
                        row_changed_date = GETDATE()
                    WHERE [{pk}] = :{pk}
                ELSE
                    INSERT INTO {table}
                        ({ins_cols}, active_ind,
                         row_created_by, row_created_date)
                    VALUES
                        ({ins_vals}, 'Y', '{who}', GETDATE())
            """), row)
        return True, "Saved"
    except Exception as e:
        return False, str(e)


def _deactivate(engine, table: str, pk: str,
                pk_val: str) -> tuple[bool, str]:
    try:
        with engine.begin() as con:
            con.execute(text(f"""
                UPDATE {table} SET active_ind = 'N',
                    row_changed_by = 'STANDARDS_MGR',
                    row_changed_date = GETDATE()
                WHERE [{pk}] = :v
            """), {"v": pk_val})
        return True, f"Deactivated {pk_val}"
    except Exception as e:
        return False, str(e)


def _load_seed_file() -> dict:
    """Load seed data from schema_registry/dv_standards_seed.json."""
    import json
    # resolve relative to this module first (launch-location-independent, matches
    # fk_catalog.py / page_pipeline.py), then fall back to cwd-relative paths.
    _here = Path(__file__).resolve()
    for path in [
        _here.parent.parent / "schema_registry" / "dv_standards_seed.json",   # dataview\schema_registry\
        _here.parent / "schema_registry" / "dv_standards_seed.json",          # if module sits directly under dataview\
        Path("schema_registry") / "dv_standards_seed.json",
        Path("dv_standards_seed.json"),
    ]:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _seed_defaults(engine, table: str, pk: str,
                   cols: list[str], defaults: list) -> int:
    """
    Seed canonical values — tries JSON seed file first, falls back to
    hardcoded defaults list.
    """
    # Try JSON seed file first
    seed_data = _load_seed_file()
    tbl_short = table.split(".")[-1]  # e.g. dv_r_well_type
    if tbl_short in seed_data.get("tables", {}):
        tbl_info = seed_data["tables"][tbl_short]
        seed_rows = tbl_info.get("rows", [])
    else:
        # Fall back to hardcoded defaults
        seed_rows = [dict(zip(cols, row)) for row in defaults]

    seeded = 0
    with engine.begin() as con:
        for row in seed_rows:
            # Ensure pk exists
            if not row.get(pk):
                continue
            # Only insert columns that exist in the target table schema
            valid_cols = [c for c in row.keys() if row[c] is not None]
            if not valid_cols:
                continue
            col_sql = ", ".join(f"[{c}]" for c in valid_cols)
            val_sql = ", ".join(f":{c}" for c in valid_cols)
            try:
                con.execute(text(f"""
                    IF NOT EXISTS (SELECT 1 FROM {table} WHERE [{pk}] = :{pk})
                    INSERT INTO {table}
                        ({col_sql}, active_ind,
                         row_created_by, row_created_date)
                    VALUES
                        ({val_sql}, 'Y', 'STANDARDS_MGR', GETDATE())
                """), row)
                seeded += 1
            except Exception:
                pass
    return seeded


# ── Main render ───────────────────────────────────────────────────────

_AUDIT_COLS = {"active_ind", "row_created_by", "row_created_date",
               "row_changed_by", "row_changed_date"}


def discover_reference_tables(engine):
    """The curated tabs, PLUS every dv_r_* table the database actually has.

    REFERENCE_TABLES was a hand-written registry of four while the schema had
    six, so dv_r_depth_datum and dv_r_well_profile_type had no tab and could
    not be seeded from the app at all. That is not cosmetic: creating a dv_r_*
    table ARMS A GUARD -- promote holds any row whose coded value is not
    registered -- so a reference table you cannot reach is a guard you cannot
    satisfy, and the rows it holds are held silently.

    The curated entries keep their icon, description and seed defaults. A
    discovered one gets its primary key and editable columns from the schema
    and no defaults, which is honest: nobody has said what its canonical
    values are, and inventing some would be worse than an empty tab.

    Falls back to the curated dict if introspection fails -- a page that
    shows four tables beats a page that shows an exception.
    """
    out = dict(REFERENCE_TABLES)
    known = {v["table"].split(".")[-1].lower() for v in out.values()}
    try:
        with engine.connect() as con:
            names = [r[0] for r in con.execute(text(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA='dataview' AND TABLE_TYPE='BASE TABLE' "
                "AND TABLE_NAME LIKE 'dv[_]r[_]%' ORDER BY TABLE_NAME")).fetchall()]
            for t in names:
                if t.lower() in known:
                    continue
                cols = [r[0] for r in con.execute(text(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA='dataview' AND TABLE_NAME=:t "
                    "ORDER BY ORDINAL_POSITION"), {"t": t}).fetchall()]
                pk = [r[0] for r in con.execute(text(
                    "SELECT kc.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
                    "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kc "
                    "  ON kc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME "
                    "WHERE tc.TABLE_SCHEMA='dataview' AND tc.TABLE_NAME=:t "
                    "AND tc.CONSTRAINT_TYPE='PRIMARY KEY' "
                    "ORDER BY kc.ORDINAL_POSITION"), {"t": t}).fetchall()]
                # A COMPOSITE KEY IS NOT A CODE LIST. This editor writes one
                # canonical value at a time; showing a tab it cannot save is
                # worse than not showing it.
                if len(pk) != 1:
                    continue
                editable = [c for c in cols if c.lower() not in _AUDIT_COLS]
                if pk[0] not in editable:
                    continue
                _stem = t[5:] if t.lower().startswith("dv_r_") else t
                label = _stem.replace("_", " ").title()
                out[label] = {
                    "table": "dataview." + t,
                    "pk": pk[0],
                    "cols": editable,
                    "icon": "📌",
                    "desc": ("Discovered from the schema. Canonical %s codes — "
                             "add values below; promote HOLDS any row whose "
                             "code is not registered here." % label.lower()),
                    "defaults": [],
                    "discovered": True,
                }
    except Exception as exc:
        print("[standards_manager] reference discovery failed: %s" % exc)
    return out


def render(engine=None):
    st.subheader("📐 Standards Manager")
    st.caption(
        "Manage canonical reference values. All dataset imports must map "
        "to these codes — no new codes are added during load."
    )

    if engine is None:
        st.warning("Connect to a database first.")
        return

    # ── Seed All button ───────────────────────────────────────────────
    seed_data = _load_seed_file()
    if seed_data:
        c1, c2 = st.columns([2, 6])
        if c1.button("🌱 Seed All Reference Tables", type="primary",
                     use_container_width=True):
            total = 0
            errors = []
            for name, cfg in REFERENCE_TABLES.items():
                try:
                    n = _seed_defaults(engine, cfg["table"], cfg["pk"],
                                       cfg["cols"], cfg.get("defaults", []))
                    total += n
                except Exception as e:
                    errors.append(f"{name}: {e}")
            if errors:
                for err in errors:
                    st.error(err)
            else:
                st.success(f"✅ Seeded **{total}** rows across "
                           f"{len(REFERENCE_TABLES)} reference tables")
                st.rerun()
        c2.caption(f"Loads from `schema_registry/dv_standards_seed.json` "
                   f"— {sum(len(v.get('rows',[])) for v in seed_data.get('tables',{}).values())} "
                   f"rows across {len(seed_data.get('tables',{}))} tables")
    else:
        st.warning("⚠️ `schema_registry/dv_standards_seed.json` not found — "
                   "copy it to the project root's schema_registry folder.")

    st.divider()

    with st.expander("🔌 Seed a reference table from a source table", expanded=False):
        render_reference_seeder(engine, schema="dataview", current_user="pmstokes")

    st.divider()

    # THE SCHEMA DECIDES WHICH TABS EXIST, not a hand-written list that
    # went stale two tables ago.
    _tables = discover_reference_tables(engine)

    # A LIST, NOT A TAB STRIP. Tabs were a fixed row of four; every new dv_r_*
    # table makes them narrower and eventually unreadable, and a tab strip has
    # to render EVERY tab body on every run. One selectbox costs the same at
    # six tables as at sixty, and the name of the table is in the option so
    # there is no guessing which dv_r_* a friendly label means.
    _opts = list(_tables)
    if not _opts:
        st.warning("No dv_r_* reference tables found in this database.")
        return

    def _label(k):
        _c = _tables[k]
        _t = _c["table"].split(".")[-1]
        return "%s  %s  —  %s%s" % (
            _c.get("icon", "📌"), k, _t,
            "  (discovered)" if _c.get("discovered") else "")

    _sel = st.selectbox("Reference table", _opts, format_func=_label,
                        key="ref_table_pick")
    if _sel:
        _render_reference_tab(engine, _sel, _tables[_sel])


def _render_reference_tab(engine, name: str, cfg: dict):
    table  = cfg["table"]
    pk     = cfg["pk"]
    cols   = cfg["cols"]
    desc   = cfg["desc"]
    defaults = cfg.get("defaults", [])

    st.caption(desc)

    # Load current values
    df = _get_all(engine, table, pk)

    # Metrics
    if not df.empty:
        active = int((df["active_ind"] == "Y").sum()) \
            if "active_ind" in df.columns else len(df)
        m1, m2 = st.columns(2)
        m1.metric("Active codes", active)
        m2.metric("Total codes",  len(df))

    # Seed defaults button
    if st.button(f"🌱 Seed default {name} values",
                 key=f"seed_{pk}"):
        n = _seed_defaults(engine, table, pk, cols, defaults)
        st.success(f"Seeded {n} default values")
        st.rerun()

    # Current values grid
    if not df.empty:
        st.markdown("**Current Canonical Values**")
        display_cols = [c for c in cols if c in df.columns] + \
                       (["active_ind"] if "active_ind" in df.columns else [])
        st.dataframe(
            df[display_cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "active_ind": st.column_config.TextColumn(
                    "Active", width="small"),
            }
        )

    # ── Add / Edit ────────────────────────────────────────────────────
    with st.expander("➕ Add / Edit Value", expanded=False):
        with st.form(f"form_add_{pk}"):
            row_vals = {}
            for col in cols:
                if col == pk:
                    row_vals[col] = st.text_input(
                        f"{col} (primary key)*",
                        key=f"add_{pk}_{col}")
                elif col in ("uom_type",):
                    row_vals[col] = st.selectbox(
                        col,
                        ["LENGTH","PRESSURE","VOLUME","TEMP",
                         "RATE","DATUM","AREA","MASS","OTHER"],
                        key=f"add_{pk}_{col}")
                else:
                    row_vals[col] = st.text_input(
                        col, key=f"add_{pk}_{col}")

            if st.form_submit_button("💾 Save", type="primary"):
                if not row_vals.get(pk, "").strip():
                    st.error(f"{pk} is required")
                else:
                    # Uppercase the PK
                    row_vals[pk] = row_vals[pk].strip().upper()
                    ok, msg = _upsert(engine, table, pk, row_vals)
                    if ok:
                        st.success(f"✅ {msg}")
                        st.rerun()
                    else:
                        st.error(msg)

    # ── Deactivate ────────────────────────────────────────────────────
    if not df.empty:
        active_codes = df[df["active_ind"] == "Y"][pk].tolist() \
            if "active_ind" in df.columns else df[pk].tolist()
        with st.expander("🚫 Deactivate a code", expanded=False):
            st.caption("Deactivated codes remain in history but cannot be "
                       "used in new dataset loads.")
            with st.form(f"form_deact_{pk}"):
                to_deact = st.selectbox(
                    "Select code to deactivate",
                    [""] + active_codes,
                    key=f"deact_{pk}")
                if st.form_submit_button("Deactivate", type="secondary"):
                    if to_deact:
                        ok, msg = _deactivate(engine, table, pk, to_deact)
                        if ok:
                            st.warning(f"⚠️ {msg}")
                            st.rerun()
                        else:
                            st.error(msg)

    # ── Export ────────────────────────────────────────────────────────
    if not df.empty:
        st.download_button(
            f"⬇ Export {name} as CSV",
            data=df.to_csv(index=False),
            file_name=f"dv_{pk}_standards.csv",
            mime="text/csv",
            key=f"export_{pk}")
