"""
ppdm_promote.py
===============
DataView v3 — ADDITIVE second promote hop: dataview.dv_*  ->  PPDM39.dbo.*

This file changes NOTHING in the existing repo. It imports no DataView module
except (optionally) the shared connect helper, and it never writes to
dataview / file_catalog. Drop it in, run it, delete it — the existing
cat_* -> dv_* promote is untouched.

WHY IT'S SO SMALL
-----------------
promote_catalog.py already proves the pattern: promotion is driven by COLUMN
NAME INTERSECTION (shared_columns()), not a hand-written column map. Because
dataview.dv_* was modelled on PPDM 3.9 in the first place, dv_well's column
names already largely ARE PPDM's well column names. So the column mapping for
this second hop is automatic too — the only thing that needs stating by hand is
the TABLE name map (dv_well -> well), because the dv_ prefix is the one
deliberate difference.

SOURCE IS dv_*, NOT cat_*
-------------------------
cat_* is transient: promote_catalog MOVES rows out of it and empties it. So
cat_* is not a durable source for a second hop. Reading from dataview.dv_*
instead means this module:
  * never competes with promote_catalog for the same rows,
  * is re-runnable at any time,
  * and matches the agreed architecture — dataview is the staging/quality
    layer, PPDM39 is the curated store.

CROSS-DATABASE, NOT CROSS-SERVER
--------------------------------
DataView_Demo and PPDM39 live on the same SQL Server instance, so this is plain
three-part naming (PPDM39.dbo.well) on ONE connection in ONE transaction. No
second engine, no linked server. If PPDM39 ever moves to another instance this
module is the only thing that has to change.

CODED / REFERENCE-FK COLUMNS
----------------------------
PPDM's dbo.well has 21 FK parents but only UWI is NOT NULL, and an FK is not
checked when its column is NULL. So a well loads clean with the coded columns
empty. Rather than either dropping those columns forever or letting them abort
the batch, the default policy is GUARDED:

    CASE WHEN EXISTS (SELECT 1 FROM <ref> r WHERE r.<refcol> = m.<col>)
         THEN m.<col> ELSE NULL END

i.e. copy the value when the reference row exists, NULL it when it doesn't.
Never rejects, never loses a resolvable value, and as reference tables get
seeded the values start flowing with no code change. Every column that got
NULLed is REPORTED with a row count — that report IS the measured
reference-data gap list.

USAGE
-----
    python ppdm_promote.py --discover        # propose dv_* -> PPDM table matches
    python ppdm_promote.py                   # dry run (rolls back), report only
    python ppdm_promote.py --apply           # commit
    python ppdm_promote.py --table dv_well --apply
    python ppdm_promote.py --uwi 42329100010000
    python ppdm_promote.py --coded null      # omit coded cols entirely
    python ppdm_promote.py --coded copy      # copy raw, let FKs reject (audit)
"""
from __future__ import annotations

import argparse
import sys

import pyodbc

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
PPDM_DB     = "PPDM39"      # target database (same instance as staging)
PPDM_SCHEMA = "dbo"         # target schema  (verified: dbo.well)
SRC_SCHEMA  = "dataview"    # source schema in the staging database

# Fallbacks only — used when build_catalog_mirror.connect() isn't importable.
DEFAULT_SERVER = r"localhost\SQLEXPRESS"
DEFAULT_DB     = "DataView_Demo"
DEFAULT_DRIVER = "ODBC Driver 17 for SQL Server"

# dv_* -> PPDM table name. Confirmed by --discover against the live PPDM39
# instance (same-name match). Tables whose PPDM name differs are proposed by
# `--discover --deep` (column-overlap evidence) — add them here once confirmed.
TABLE_MAP = {
    "dv_business_associate": "business_associate",
    "dv_field":              "field",
    "dv_seis_line":          "seis_line",
    "dv_seis_set":           "seis_set",
    "dv_well":               "well",
    "dv_well_alias":         "well_alias",
    "dv_well_completion":    "well_completion",
    "dv_well_core":          "well_core",
    "dv_well_log":           "well_log",
    "dv_well_log_curve":     "well_log_curve",
    "dv_well_perforation":   "well_perforation",
    "dv_well_pressure":      "well_pressure",
}

# Saved mapping, edited by the PPDM page and read by this CLI. A LIST, not a
# dict, so one dv_ table can map to SEVERAL PPDM tables — PPDM decomposes some
# of ours (well_core_sample -> _desc/_anal/_rmk, well_mud_log -> well_mud_sample
# /_property/_resistivity), which a flat dict can't express.
#   [{"source": "dv_well", "target": "well", "enabled": true,
#     "columns": {"TARGET_COL": "source_col", ...}}]
# "columns" holds only OVERRIDES; anything omitted still matches by name.
MAP_PATH = "ppdm_map.json"


def load_map(path=MAP_PATH):
    """Saved mapping if present AND non-empty, else one entry per TABLE_MAP
    default. An empty saved list falls back rather than sticking: otherwise one
    accidental save of an empty grid locks the map out permanently."""
    import json
    import os
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
    return [{"source": s, "target": t, "enabled": True, "columns": {}}
            for s, t in sorted(TABLE_MAP.items())]


def save_map(entries, path=MAP_PATH):
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    return path


# PPDM puts these on virtually every table, so they carry NO evidence about
# whether two tables are the same entity. Excluded from match scoring — without
# this every table "matches" every other table with ~8 shared columns.
_AUDIT_COLS = {
    "ACTIVE_IND", "EFFECTIVE_DATE", "EXPIRY_DATE", "PPDM_GUID", "REMARK",
    "ROW_CHANGED_BY", "ROW_CHANGED_DATE", "ROW_CREATED_BY", "ROW_CREATED_DATE",
    "ROW_EFFECTIVE_DATE", "ROW_EXPIRY_DATE", "ROW_QUALITY", "SOURCE",
}

# Written into PPDM's audit columns when the source row leaves them NULL.
_AUDIT_FILL = {
    "row_created_by":   "'DATAVIEW'",
    "row_created_date": "SYSUTCDATETIME()",
    "active_ind":       "'Y'",
}

# Never carried across — DataView-internal provenance with no PPDM meaning.
# INVENTORY_ID is dropped here too: PPDM has no equivalent column, and it is
# reported (not silently discarded) so the provenance mapping stays a visible
# open item rather than a surprise.
_NEVER_COPY = {"CAT_ROW_ID", "PROMOTED", "PROMOTED_AT", "CAPTURED_AT",
               "SOURCE_PATH", "GEOG"}


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #
def connect(server=None, database=None, driver=None):
    """Reuse the repo's own connect() when importable so this module inherits
    the exact same connection defaults; otherwise build a plain pyodbc
    connection. Either way the caller owns the transaction."""
    if server is None and database is None:
        try:
            from dataview.file_catalog.build_catalog_mirror import connect as _c
            return _c()
        except Exception:
            pass
    cs = (f"DRIVER={{{driver or DEFAULT_DRIVER}}};"
          f"SERVER={server or DEFAULT_SERVER};"
          f"DATABASE={database or DEFAULT_DB};"
          f"Trusted_Connection=yes;")
    return pyodbc.connect(cs, autocommit=False)


# --------------------------------------------------------------------------- #
# Reflection — source is in the current DB, target is three-part
# --------------------------------------------------------------------------- #
def _src_columns(cur, table):
    cur.execute(
        "SELECT c.name FROM sys.columns c "
        "WHERE c.object_id = OBJECT_ID(?) ORDER BY c.column_id",
        f"{SRC_SCHEMA}.{table}")
    return [r[0] for r in cur.fetchall()]


def _tgt_columns(cur, table):
    """Columns of PPDM_DB.PPDM_SCHEMA.table as [(name, is_nullable), ...].

    sys.* not INFORMATION_SCHEMA: the latter is a view with per-row permission
    checks and against PPDM's ~71k columns it is slow enough to be felt on every
    call. OBJECT_ID() with a three-part name resolves cross-database fine."""
    cur.execute(
        f"SELECT c.name, c.is_nullable FROM {PPDM_DB}.sys.columns c "
        f"WHERE c.object_id = OBJECT_ID(?) ORDER BY c.column_id",
        f"{PPDM_DB}.{PPDM_SCHEMA}.{table}")
    return [(r[0], bool(r[1])) for r in cur.fetchall()]


def _tgt_computed(cur, table):
    cur.execute(
        f"SELECT c.name FROM {PPDM_DB}.sys.columns c "
        f"WHERE c.object_id = OBJECT_ID(?) AND c.is_computed = 1",
        f"{PPDM_DB}.{PPDM_SCHEMA}.{table}")
    return {r[0].upper() for r in cur.fetchall()}


def _tgt_pk(cur, table):
    cur.execute(
        f"SELECT col.name FROM {PPDM_DB}.sys.indexes i "
        f"JOIN {PPDM_DB}.sys.index_columns ic ON ic.object_id = i.object_id "
        f"  AND ic.index_id = i.index_id "
        f"JOIN {PPDM_DB}.sys.columns col ON col.object_id = ic.object_id "
        f"  AND col.column_id = ic.column_id "
        f"WHERE i.is_primary_key = 1 AND i.object_id = OBJECT_ID(?) "
        f"ORDER BY ic.key_ordinal",
        f"{PPDM_DB}.{PPDM_SCHEMA}.{table}")
    return [r[0] for r in cur.fetchall()]


def _tgt_ref_fks(cur, table):
    """{local_column_upper: (ref_table, ref_column)} for FKs on the target table.
    Used to guard coded values. Composite FKs are skipped — guarding one member
    of a composite in isolation would be wrong."""
    cur.execute(
        f"SELECT cpa.name, rt.name, cref.name, fk.object_id "
        f"FROM {PPDM_DB}.sys.foreign_keys fk "
        f"JOIN {PPDM_DB}.sys.foreign_key_columns fkc "
        f"       ON fkc.constraint_object_id = fk.object_id "
        f"JOIN {PPDM_DB}.sys.tables  rt  ON rt.object_id = fk.referenced_object_id "
        f"JOIN {PPDM_DB}.sys.columns cpa ON cpa.object_id = fkc.parent_object_id "
        f"                              AND cpa.column_id = fkc.parent_column_id "
        f"JOIN {PPDM_DB}.sys.columns cref ON cref.object_id = fkc.referenced_object_id "
        f"                               AND cref.column_id = fkc.referenced_column_id "
        f"WHERE fk.parent_object_id = OBJECT_ID(?)",
        f"{PPDM_DB}.{PPDM_SCHEMA}.{table}")
    rows = cur.fetchall()
    width: dict = {}
    for _lc, _rt, _rc, oid in rows:
        width[oid] = width.get(oid, 0) + 1
    out: dict = {}
    for lc, rt, rc, oid in rows:
        if width[oid] > 1:        # composite — leave alone
            continue
        out[lc.upper()] = (rt, rc)
    return out


# --------------------------------------------------------------------------- #
# SELECT expression per column
# --------------------------------------------------------------------------- #
# Domain index: {"WELL": ["well", "well_alias", ...], "SEISMIC": [...], ...}
# Built once from PPDM39 and cached to disk, so the page never re-queries 2,696
# table names just to populate a dropdown.
DOMAINS_PATH = "ppdm_domains.json"
DOMAINS = ("WELL", "SEISMIC", "STRATIGRAPHY", "PRODUCTION", "OTHER")


# Value map: {ref_table: {SOURCE_VALUE: target_value_or_null}}. null means SKIP
# — write NULL rather than the source value. Applied by _sel() at promote time,
# so a decision made once in the reference grid is honoured on every later run.
VALUE_MAP_PATH = "ppdm_value_map.json"
SKIP = "‹SKIP → NULL›"


def load_value_map(path=VALUE_MAP_PATH):
    import json
    import os
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_value_map(vm, path=VALUE_MAP_PATH):
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(vm, f, indent=1, ensure_ascii=False)
    return path


def ref_tables(cur):
    """Every r_* table in PPDM, names only — cheap."""
    return sorted(n.lower() for n in _table_names(cur, PPDM_DB, PPDM_SCHEMA).values()
                  if n.lower().startswith("r_"))


def _ref_key(cur, ref_table):
    """The reference table's key column — its PK, or failing that its first
    column. PPDM reference tables are keyed on the code itself."""
    pk = _tgt_pk(cur, ref_table)
    if pk:
        return pk[0]
    cols = _tgt_columns(cur, ref_table)
    if not cols:
        raise RuntimeError(f"{ref_table}: no columns")
    return cols[0][0]


def ref_values(cur, ref_table):
    """Existing codes in a reference table."""
    k = _ref_key(cur, ref_table)
    cur.execute(f"SELECT [{k}] FROM {PPDM_DB}.{PPDM_SCHEMA}.[{ref_table}] "
                f"WHERE [{k}] IS NOT NULL ORDER BY [{k}]")
    return [str(r[0]) for r in cur.fetchall()]


def source_values(cur, src_table, src_column):
    """Distinct non-blank values in a staging column, with row counts, commonest
    first — so the values that matter most are decided first."""
    cur.execute(
        f"SELECT LTRIM(RTRIM(CONVERT(nvarchar(400), [{src_column}]))) AS v, "
        f"COUNT(*) AS n FROM {SRC_SCHEMA}.[{src_table}] "
        f"WHERE [{src_column}] IS NOT NULL "
        f"  AND LTRIM(RTRIM(CONVERT(nvarchar(400), [{src_column}]))) <> '' "
        f"GROUP BY LTRIM(RTRIM(CONVERT(nvarchar(400), [{src_column}]))) "
        f"ORDER BY COUNT(*) DESC")
    return [(str(r[0]), int(r[1])) for r in cur.fetchall()]


def add_reference_values(cur, ref_table, values, log=print):
    """Insert new codes into a PPDM reference table.

    Fills the key with the code itself and any other NOT NULL text column with
    the same text (PPDM reference tables are typically key + LONG_NAME/
    ABBREVIATION + audit), so the row satisfies its constraints without
    inventing data. Existing codes are skipped, so this is safely re-runnable."""
    key = _ref_key(cur, ref_table)
    cols = _tgt_columns(cur, ref_table)
    computed = _tgt_computed(cur, ref_table)
    existing = {v.upper() for v in ref_values(cur, ref_table)}

    fill_cols = []
    for c, nullable in cols:
        up = c.upper()
        if up == key.upper() or up in computed:
            continue
        if not nullable:                       # must supply something
            fill_cols.append(c)
        elif up in ("LONG_NAME", "SHORT_NAME", "ABBREVIATION"):
            fill_cols.append(c)                # conventional, helps humans

    added = 0
    for v in values:
        v = (v or "").strip()
        if not v or v.upper() in existing:
            continue
        names = [key] + fill_cols
        exprs, params = ["?"], [v]
        for c in fill_cols:
            low = c.lower()
            if low in _AUDIT_FILL:
                exprs.append(_AUDIT_FILL[low])
            else:
                exprs.append("?")
                params.append(v)
        cur.execute(
            f"INSERT INTO {PPDM_DB}.{PPDM_SCHEMA}.[{ref_table}] "
            f"({', '.join('[' + c + ']' for c in names)}) "
            f"VALUES ({', '.join(exprs)})", *params)
        existing.add(v.upper())
        added += 1
        log(f"  + {ref_table}.{key} = {v}")
    return added


def _fanout_expr(scol):
    """Trimmed, non-blank source value."""
    return f"LTRIM(RTRIM(CONVERT(nvarchar(255), m.[{scol}])))"


def promote_fanout(cur, entry, apply=False, uwi=None, log=print):
    """One source ROW -> many target ROWS.

    PPDM normalises what we hold as columns. country / province_state / county
    aren't columns on `well`; each is an `area` row, and `well_area` links the
    well to it with AREA_TYPE saying which kind. So three dv_well columns become
    three area rows plus three well_area rows.

    The same shape covers well_alias, well_identifier and the _desc/_anal/_rmk
    decompositions — anything where a discriminator column names what the value
    means. That's why this is a mapping KIND and not a country special case.

    Entry shape (kind="fanout"):
        source        dv_well
        target        well_area          the link table
        keys          {"UWI": "uwi"}     target col -> source col
        constants     {"SOURCE": "DATAVIEW"}
        value_column  AREA_ID            receives the source column's value
        label_column  AREA_TYPE          receives the item's label
        items         [{"column": "county", "label": "COUNTY"}, ...]
        seed          {"table": "area", "name_column": "PREFERRED_NAME"}
                      parent rows to create first, keyed on
                      (value_column, label_column)
    """
    src, tgt = entry["source"], entry["target"]
    keys     = entry.get("keys") or {}
    consts   = entry.get("constants") or {}
    val_col  = entry["value_column"]
    lab_col  = entry["label_column"]
    items    = entry.get("items") or []
    seed     = entry.get("seed") or None

    src_cols = {c.upper(): c for c in _src_columns(cur, src)}
    tgt_cols = _tgt_columns(cur, tgt)
    if not tgt_cols:
        raise RuntimeError(f"target {PPDM_DB}.{PPDM_SCHEMA}.{tgt} not found")
    tgt_up = {c.upper() for c, _n in tgt_cols}
    tgt_pk = _tgt_pk(cur, tgt)

    for tc in list(keys) + list(consts) + [val_col, lab_col]:
        if tc.upper() not in tgt_up:
            raise RuntimeError(f"{tgt} has no column {tc}")

    total_e = total_m = 0
    for it in items:
        scol, label = it.get("column"), it.get("label")
        if not scol or not label:
            log(f"  skipping item with no column/label: {it!r}")
            continue
        if scol.upper() not in src_cols:
            log(f"  ⚠ {src} has no column {scol} — skipped")
            continue
        s = src_cols[scol.upper()]
        v = _fanout_expr(s)

        where = f"{v} <> '' AND m.[{s}] IS NOT NULL"
        params: list = []
        if uwi and "UWI" in {k.upper() for k in keys}:
            ukey = next(k for k in keys if k.upper() == "UWI")
            where += f" AND m.[{keys[ukey]}] = ?"
            params.append(uwi)

        # ---- 1. parent rows (area) -------------------------------------- #
        if seed:
            stab = seed["table"]
            sname = seed.get("name_column")
            scols_t = _tgt_columns(cur, stab)
            if not scols_t:
                raise RuntimeError(f"seed table {stab} not found")
            stgt_up = {c.upper(): c for c, _n in scols_t}
            names = [val_col, lab_col]
            exprs = [v, _q(label)]
            if sname and sname.upper() in stgt_up:
                names.append(sname)
                exprs.append(v)
            for c, _n in scols_t:
                if c.lower() in _AUDIT_FILL and c.upper() not in {
                        x.upper() for x in names}:
                    names.append(c)
                    exprs.append(_AUDIT_FILL[c.lower()])
            sql = (f"INSERT INTO {PPDM_DB}.{PPDM_SCHEMA}.[{stab}] "
                   f"({', '.join('[' + c + ']' for c in names)}) "
                   f"SELECT DISTINCT {', '.join(exprs)} "
                   f"FROM {SRC_SCHEMA}.[{src}] m WHERE {where} "
                   f"AND NOT EXISTS (SELECT 1 FROM "
                   f"{PPDM_DB}.{PPDM_SCHEMA}.[{stab}] p "
                   f"WHERE p.[{val_col}] = {v} AND p.[{lab_col}] = {_q(label)})")
            cnt_sql = (f"SELECT COUNT(DISTINCT {v}) FROM {SRC_SCHEMA}.[{src}] m "
                       f"WHERE {where} AND NOT EXISTS (SELECT 1 FROM "
                       f"{PPDM_DB}.{PPDM_SCHEMA}.[{stab}] p "
                       f"WHERE p.[{val_col}] = {v} "
                       f"AND p.[{lab_col}] = {_q(label)})")
            cur.execute(cnt_sql, *params)
            new_parents = cur.fetchone()[0] or 0
            if apply and new_parents:
                cur.execute(sql, *params)
            log(f"    {label}: {new_parents} new {stab} row(s)"
                f"{'' if apply else ' (dry run)'}")

        # ---- 2. link rows (well_area) ----------------------------------- #
        names, exprs = [], []
        for tc, sc in keys.items():
            if sc.upper() not in src_cols:
                raise RuntimeError(f"{src} has no key column {sc}")
            names.append(tc)
            exprs.append(f"m.[{src_cols[sc.upper()]}]")
        for tc, cv in consts.items():
            names.append(tc)
            exprs.append(_q(cv))
        names += [val_col, lab_col]
        exprs += [v, _q(label)]
        expr_by = {n.upper(): e for n, e in zip(names, exprs)}
        for c, _n in tgt_cols:
            if c.lower() in _AUDIT_FILL and c.upper() not in expr_by:
                names.append(c)
                exprs.append(_AUDIT_FILL[c.lower()])

        not_exists = ""
        if tgt_pk and all(k.upper() in expr_by for k in tgt_pk):
            on = " AND ".join(f"t.[{k}] = {expr_by[k.upper()]}" for k in tgt_pk)
            not_exists = (f" AND NOT EXISTS (SELECT 1 FROM "
                          f"{PPDM_DB}.{PPDM_SCHEMA}.[{tgt}] t WHERE {on})")
        elif tgt_pk:
            log(f"    ⚠ {tgt} PK ({', '.join(tgt_pk)}) not fully supplied — "
                f"insert-only, re-runs will duplicate")

        cur.execute(f"SELECT COUNT(*) FROM {SRC_SCHEMA}.[{src}] m "
                    f"WHERE {where}{not_exists}", *params)
        elig = cur.fetchone()[0] or 0
        total_e += elig
        if apply and elig:
            cur.execute(
                f"INSERT INTO {PPDM_DB}.{PPDM_SCHEMA}.[{tgt}] "
                f"({', '.join('[' + c + ']' for c in names)}) "
                f"SELECT {', '.join(exprs)} FROM {SRC_SCHEMA}.[{src}] m "
                f"WHERE {where}{not_exists}", *params)
            total_m += cur.rowcount or 0
        log(f"    {label}: {elig} {tgt} row(s) from {src}.{s}"
            f"{'' if apply else ' (dry run)'}")

    log(f"  {src} -> {tgt}: {total_e} eligible, {total_m} inserted "
        f"across {len(items)} item(s)")
    return total_e, total_m


def area_types(cur):
    """Registered area-type codes, if the reference table exists. Used to warn
    before a label is written that PPDM won't accept."""
    try:
        return set(v.upper() for v in ref_values(cur, "r_area_type"))
    except Exception:
        return set()


def default_area_fanout(cur, src="dv_well"):
    """A ready-made country/state/county fan-out, built from the columns that
    actually exist in the source rather than assumed names."""
    have = {c.upper(): c for c in _src_columns(cur, src)}
    wanted = [
        (("COUNTRY",), "COUNTRY"),
        (("PROVINCE_STATE", "STATE", "PROVINCE", "STATE_PROVINCE"),
         "PROVINCE_STATE"),
        (("COUNTY", "PARISH", "COUNTY_PARISH"), "COUNTY"),
    ]
    items = []
    for cands, label in wanted:
        for c in cands:
            if c in have:
                items.append({"column": have[c], "label": label})
                break
    ukey = have.get("UWI", "uwi")
    return {
        "source": src, "target": "well_area", "kind": "fanout",
        "enabled": True,
        "keys": {"UWI": ukey},
        "constants": {"SOURCE": "DATAVIEW"},
        "value_column": "AREA_ID", "label_column": "AREA_TYPE",
        "items": items,
        "seed": {"table": "area", "name_column": "PREFERRED_NAME"},
    }


def _q(s: str) -> str:
    """Single-quote a literal for inline SQL."""
    return "'" + str(s).replace("'", "''") + "'"


def _domain_of(name: str) -> str:
    """Classify a PPDM table by name. Reference prefixes are stripped first so
    r_well_status files under WELL with the rest of the well tables. Order is
    most-specific first: well_log_curve is WELL, not swallowed by anything else."""
    n = name.lower()
    for pre in ("r_", "ppdm_", "xref_"):
        if n.startswith(pre):
            n = n[len(pre):]
            break
    tok = set(n.split("_"))
    if {"seis", "seismic", "segy"} & tok or n.startswith("seis"):
        return "SEISMIC"
    if {"strat", "lith", "litho", "biozone", "ecozone"} & tok or n.startswith("strat"):
        return "STRATIGRAPHY"
    if {"pden", "prod", "production", "volume", "decline"} & tok or n.startswith("pden"):
        return "PRODUCTION"
    if {"well", "wells", "uwi", "borehole"} & tok or n.startswith("well"):
        return "WELL"
    return "OTHER"


def build_domains(cur, path=DOMAINS_PATH):
    """One cheap query over table NAMES only (no column reflection), classified
    and written to disk."""
    import json
    names = _table_names(cur, PPDM_DB, PPDM_SCHEMA)
    out: dict = {d: [] for d in DOMAINS}
    for n in names.values():
        out[_domain_of(n)].append(n.lower())
    for d in out:
        out[d].sort()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    return out


def load_domains(path=DOMAINS_PATH):
    """Domain index from disk, or None if it hasn't been built yet."""
    import json
    import os
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data else None
    except Exception:
        return None


def _is_ref_table(name: str) -> bool:
    """True for PPDM REFERENCE tables (r_*, ppdm_*), false for ENTITY tables
    (well, field, business_associate, strat_unit...).

    The distinction matters enormously and promote_catalog already makes it:
    a reference FK holds a CODE, so NULLing an unresolvable one loses a coded
    attribute and nothing else. An entity FK holds a PARENT KEY — NULLing
    well_log_curve.UWI because that well isn't loaded yet doesn't lose an
    attribute, it ORPHANS the row. Entity FKs get the row HELD instead, exactly
    as promote_catalog._reference_fk_predicates does."""
    n = name.lower()
    return n.startswith("r_") or n.startswith("ppdm_")


def _sel(col, refs, coded_policy, value_map=None):
    """How one target column is filled from the source row `m`.

    Order matters: the value map is applied FIRST (translating a source code to
    the PPDM code, or to NULL for SKIP), and only then is the FK guard applied
    to the translated value — otherwise a correctly-mapped value would still be
    NULLed for not matching the reference table under its old spelling."""
    up = col.upper()
    fill = _AUDIT_FILL.get(col.lower())
    expr = f"m.[{col}]"

    rt = rc = None
    if up in refs:
        rt, rc = refs[up]

    vm = (value_map or {}).get((rt or "").lower(), {}) if rt else {}
    if vm:
        whens = []
        for sv, tv in vm.items():
            whens.append(f"WHEN {_q(sv)} THEN "
                         + ("NULL" if tv is None else _q(tv)))
        expr = f"CASE m.[{col}] {' '.join(whens)} ELSE m.[{col}] END"

    if rt and coded_policy == "guarded" and _is_ref_table(rt):
        expr = (f"CASE WHEN EXISTS (SELECT 1 FROM {PPDM_DB}.{PPDM_SCHEMA}.[{rt}] r "
                f"WHERE r.[{rc}] = ({expr})) THEN ({expr}) ELSE NULL END")
    if fill:
        return f"COALESCE({expr}, {fill})"
    return expr


def _plan(cur, src_table, tgt_table, coded_policy, col_map=None):
    """Work out which columns travel. Returns a dict describing the move.

    col_map is {TARGET_COL_UPPER: source_col} — explicit overrides for columns
    whose names differ between the two models. Anything not listed still
    matches by name, so a partial map is useful immediately."""
    src = _src_columns(cur, src_table)
    if not src:
        raise RuntimeError(f"source {SRC_SCHEMA}.{src_table} not found / no columns")
    tgt = _tgt_columns(cur, tgt_table)
    if not tgt:
        raise RuntimeError(
            f"target {PPDM_DB}.{PPDM_SCHEMA}.{tgt_table} not found / no columns")

    computed = _tgt_computed(cur, tgt_table)
    refs     = _tgt_ref_fks(cur, tgt_table)
    src_up   = {c.upper(): c for c in src}
    tgt_up   = {c.upper() for c, _ in tgt}
    overrides = {k.upper(): v for k, v in (col_map or {}).items() if v}

    shared, dropped_coded, stale = [], [], []
    for tcol, _nullable in tgt:
        up = tcol.upper()
        if up in computed or up in _NEVER_COPY:
            continue
        if up in overrides:
            ov = overrides[up]
            if ov.upper() not in src_up:      # override names a column that's gone
                stale.append(f"{tcol}<-{ov}")
                continue
            scol = src_up[ov.upper()]
        elif up in src_up:
            scol = src_up[up]
        else:
            continue
        if up in refs and coded_policy == "null":
            dropped_coded.append(tcol)
            continue
        shared.append((tcol, scol))

    mapped_src = {s.upper() for _t, s in shared}
    unmapped_src = sorted(
        s for s in src
        if s.upper() not in mapped_src and s.upper() not in _NEVER_COPY
        and s.upper() not in tgt_up)
    tgt_not_null = {c.upper() for c, nullable in tgt if not nullable}
    missing_required = sorted(
        c for c in tgt_not_null
        if c not in {t.upper() for t, _ in shared} and c not in computed)

    return {
        "src_table": src_table, "tgt_table": tgt_table,
        "shared": shared, "refs": refs,
        "pk": _tgt_pk(cur, tgt_table),
        "unmapped_src": unmapped_src,
        "dropped_coded": dropped_coded,
        "missing_required": missing_required,
        "stale_overrides": stale,
    }


# --------------------------------------------------------------------------- #
# The move
# --------------------------------------------------------------------------- #
def promote_table(cur, src_table, tgt_table, apply=False, uwi=None,
                  coded_policy="guarded", log=print, col_map=None,
                  value_map=None):
    plan = _plan(cur, src_table, tgt_table, coded_policy, col_map)
    shared, pk, refs = plan["shared"], plan["pk"], plan["refs"]
    if plan["stale_overrides"]:
        log(f"  ⚠ override names a missing source column: "
            f"{', '.join(plan['stale_overrides'])}")
    if not shared:
        log(f"  {src_table} -> {tgt_table}: no shared columns — nothing to move")
        return 0, 0

    params: list = []
    where = "1=1"
    tgt2src = {t.upper(): s for t, s in shared}

    # The target's PK names its OWN columns; the source may call them something
    # else or not have them at all. Joining on m.[<target pk name>] is what
    # produced ProgrammingError 42S22 (invalid column name) on every table whose
    # key naming differs. Only use PK columns that actually mapped, and only
    # dedupe when EVERY PK column mapped — a partial key can't identify a row.
    pk_src = [(k, tgt2src[k.upper()]) for k in pk if k.upper() in tgt2src]
    pk_ok = bool(pk) and len(pk_src) == len(pk)

    for _k, s in pk_src:
        where += (f" AND NULLIF(LTRIM(RTRIM(CONVERT(varchar(128), "
                  f"m.[{s}]))),'') IS NOT NULL")
    if uwi and "UWI" in tgt2src:
        where += f" AND m.[{tgt2src['UWI']}] = ?"
        params.append(uwi)

    # Entity-FK holds: a row whose parent isn't in PPDM yet waits rather than
    # being inserted with a NULL parent key. NULLs pass (the column is nullable).
    held_cols = []
    if coded_policy != "copy":
        for tcol, scol in shared:
            up = tcol.upper()
            if up not in refs or _is_ref_table(refs[up][0]):
                continue
            rt, rc = refs[up]
            where += (f" AND (m.[{scol}] IS NULL OR EXISTS (SELECT 1 FROM "
                      f"{PPDM_DB}.{PPDM_SCHEMA}.[{rt}] r "
                      f"WHERE r.[{rc}] = m.[{scol}]))")
            held_cols.append(f"{tcol}->{rt}")

    tgt_full = f"{PPDM_DB}.{PPDM_SCHEMA}.[{tgt_table}]"
    src_full = f"{SRC_SCHEMA}.[{src_table}]"

    # Idempotency: skip rows already present by PK.
    if pk_ok:
        on = " AND ".join(f"t.[{k}] = m.[{s}]" for k, s in pk_src)
        not_exists = f" AND NOT EXISTS (SELECT 1 FROM {tgt_full} t WHERE {on})"
    else:
        not_exists = ""
        if not pk:
            log(f"  {tgt_table}: no PK — insert-only, re-runs will duplicate")
        else:
            log(f"  ⚠ {tgt_table}: PK ({', '.join(pk)}) not fully mapped — "
                f"insert-only, re-runs will duplicate. Map the key column(s) "
                f"in the Column map to enable de-duplication.")

    cur.execute(f"SELECT COUNT(*) FROM {src_full} m WHERE {where}{not_exists}",
                *params)
    eligible = cur.fetchone()[0] or 0

    if held_cols:
        cur.execute(f"SELECT COUNT(*) FROM {src_full} m", *[])
        total = cur.fetchone()[0] or 0
        log(f"  parent not in PPDM yet -> rows HELD (not orphaned): "
            f"{', '.join(held_cols)} · {eligible} of {total} row(s) ready")

    # Report the coded columns that would be NULLed, with counts — this is the
    # measured reference-data gap list.
    if coded_policy == "guarded":
        gaps = []
        for tcol, scol in shared:
            up = tcol.upper()
            if up not in refs or not _is_ref_table(refs[up][0]):
                continue          # entity FKs hold the row, reported above
            rt, rc = refs[up]
            cur.execute(
                f"SELECT COUNT(*) FROM {src_full} m WHERE {where}{not_exists} "
                f"AND m.[{scol}] IS NOT NULL AND NOT EXISTS "
                f"(SELECT 1 FROM {PPDM_DB}.{PPDM_SCHEMA}.[{rt}] r "
                f"WHERE r.[{rc}] = m.[{scol}])", *params)
            n = cur.fetchone()[0] or 0
            if n:
                gaps.append((tcol, rt, n))
        if gaps:
            log(f"  reference gaps (value present, no matching row -> NULLed):")
            for c, rt, n in sorted(gaps, key=lambda g: -g[2]):
                log(f"    {c:32} -> {rt:28} {n:>7} row(s)")

    if plan["missing_required"]:
        log(f"  ⚠ target NOT NULL columns with no source: "
            f"{', '.join(plan['missing_required'])}")
    if plan["unmapped_src"]:
        log(f"  source columns with no PPDM counterpart ({len(plan['unmapped_src'])}): "
            f"{', '.join(plan['unmapped_src'])}")
    if plan["dropped_coded"]:
        log(f"  coded columns omitted (--coded null): "
            f"{', '.join(plan['dropped_coded'])}")

    if not apply or not eligible:
        log(f"  {src_table} -> {tgt_table}: {eligible} eligible, "
            f"{len(shared)} column(s){'' if apply else ' (dry run)'}")
        return eligible, 0

    cols_sql = ", ".join(f"[{t}]" for t, _ in shared)
    sel_sql  = ", ".join(_sel(s, refs, coded_policy, value_map)
                         for _t, s in shared)
    cur.execute(
        f"INSERT INTO {tgt_full} ({cols_sql}) SELECT {sel_sql} "
        f"FROM {src_full} m WHERE {where}{not_exists}", *params)
    moved = cur.rowcount or 0
    log(f"  {src_table} -> {tgt_table}: {eligible} eligible, {moved} inserted, "
        f"{len(shared)} column(s)")
    return eligible, moved


# dv_* tables that are DataWrangler machinery, source-specific extensions or
# backups — they have no PPDM counterpart by design and shouldn't be promoted.
# Matched as substrings; flagged in --deep output rather than silently hidden,
# so the call stays yours.
_INTERNAL_HINTS = ("backup", "_stg_", "dv_stg", "_ext_", "column_map",
                   "load_batch", "export", "file_catalog", "spatial_layer",
                   "data_quality", "extension", "_orphans")


def _leading_tokens(a: str, b: str) -> int:
    """How many leading underscore-tokens two names share. 'well_dir_srvy_hdr'
    vs 'well_dir_srvy' = 3. A far stronger signal than column overlap for
    child tables, whose column NAMES diverge between the two models even when
    the table plainly corresponds."""
    ta, tb = a.split("_"), b.split("_")
    n = 0
    for x, y in zip(ta, tb):
        if x != y:
            break
        n += 1
    return n


def _all_columns(cur, db, schema, log=None):
    """{TABLE_NAME_UPPER: {COL_UPPER, ...}} for a whole schema in one round trip.

    Uses sys.* rather than INFORMATION_SCHEMA.COLUMNS. INFORMATION_SCHEMA is a
    view carrying per-row permission checks, and promote_catalog._prime_metadata
    already measured the cost: ~2300 rows took ~10s. Against PPDM's ~2700 tables
    (~80k columns) that becomes minutes. The sys.* join avoids the per-row
    overhead and returns the same information.

    Only used by --deep; the plain match path reflects just the matched tables.
    """
    p = f"{db}." if db else ""
    if log:
        log(f"-- reflecting columns for {p or ''}{schema} (this is the slow part)…")
    cur.execute(
        f"SELECT t.name, c.name FROM {p}sys.columns c "
        f"JOIN {p}sys.tables  t ON t.object_id = c.object_id "
        f"JOIN {p}sys.schemas s ON s.schema_id = t.schema_id "
        f"WHERE s.name = ?", schema)
    out: dict = {}
    n = 0
    while True:
        rows = cur.fetchmany(5000)
        if not rows:
            break
        for t, c in rows:
            out.setdefault(t.upper(), set()).add(c.upper())
        n += len(rows)
        if log:
            log(f"   …{n:,} columns / {len(out):,} tables")
    return out


def _table_names(cur, db, schema):
    """Table names only — cheap, no column reflect."""
    p = f"{db}." if db else ""
    cur.execute(
        f"SELECT t.name FROM {p}sys.tables t "
        f"JOIN {p}sys.schemas s ON s.schema_id = t.schema_id "
        f"WHERE s.name = ?", schema)
    return {r[0].upper(): r[0] for r in cur.fetchall()}


def discover(cur, deep=False, log=print):
    """Propose dv_* -> PPDM matches from evidence.

    Pass 1 is the same-name match (dv_<x> -> <x>). Pass 2 (--deep) scores every
    remaining dv_* table against every PPDM table by shared column names,
    EXCLUDING audit columns — otherwise the audit block alone scores ~8 on
    every pair and the ranking is meaningless.

    Nothing here is guessed from knowledge of PPDM naming; every proposal is
    backed by a column-overlap count you can check."""
    src_tabs = _table_names(cur, None, SRC_SCHEMA)
    tgt_tabs = _table_names(cur, PPDM_DB, PPDM_SCHEMA)

    dv_tables = sorted(t for t in src_tabs
                       if t.lower().startswith("dv_")
                       and not t.lower().startswith("dv_r_"))
    log(f"-- {len(dv_tables)} dv_* tables vs {len(tgt_tabs)} "
        f"{PPDM_DB}.{PPDM_SCHEMA} tables (dv_r_* reference tables excluded)")

    # Pass 1 reflects ONLY the same-name pairs — a dozen small queries, fast.
    matched, unmatched = [], []
    for dv in dv_tables:
        cand = dv[3:].upper()
        if cand not in tgt_tabs:
            unmatched.append(dv)
            continue
        s = {c.upper() for c in _src_columns(cur, src_tabs[dv])}
        t = {c.upper() for c, _ in _tgt_columns(cur, tgt_tabs[cand])}
        shared = s & t
        matched.append((dv, cand, len(shared), len(shared - _AUDIT_COLS)))

    log("")
    log("TABLE_MAP = {")
    for dv, tgt, n, nreal in matched:
        flag = "" if nreal >= 3 else "   <-- audit columns only, verify"
        log(f'    "{dv.lower()}": "{tgt.lower()}",'.ljust(52)
            + f"# {n} shared, {nreal} real{flag}")
    log("}")
    log(f"-- same-name match: {len(matched)} · no same-name table: {len(unmatched)}")

    if not deep:
        log("-- run with --deep to propose PPDM names for the "
            f"{len(unmatched)} unmatched table(s) by column overlap")
        return

    # Pass 2 needs every PPDM table's columns — the one genuinely expensive step.
    src_all  = _all_columns(cur, None, SRC_SCHEMA)
    tgt_all  = _all_columns(cur, PPDM_DB, PPDM_SCHEMA, log=log)
    tgt_real = {t: (c - _AUDIT_COLS) for t, c in tgt_all.items()}

    log("")
    log("-- proposals for unmatched tables")
    log("   name = shared leading name-tokens · cols = shared non-audit columns")
    for dv in unmatched:
        stem  = dv.lower()[3:]
        scols = src_all.get(dv, set()) - _AUDIT_COLS
        tag = "  [internal? — probably not promoted]" if any(
            h in dv.lower() for h in _INTERNAL_HINTS) else ""
        scored = []
        for t, tcols in tgt_real.items():
            nm = _leading_tokens(stem, t.lower())
            nc = len(scols & tcols)
            if nm >= 2 or nc >= 3:
                scored.append((nm, nc, t.lower()))
        # name agreement first, then column overlap — ties broken alphabetically
        scored.sort(key=lambda r: (-r[0], -r[1], r[2]))
        if not scored:
            log(f"  {dv.lower():34} (no candidate){tag}")
            continue
        log(f"  {dv.lower()}  [{len(scols)} non-audit column(s)]{tag}")
        for nm, nc, t in scored[:3]:
            cov = (nc / len(scols) * 100) if scols else 0.0
            log(f"      {t:40} name {nm}  cols {nc:>3}  {cov:5.1f}% covered")


def main() -> int:
    ap = argparse.ArgumentParser(description="Promote dataview.dv_* -> PPDM39.dbo.*")
    ap.add_argument("--apply", action="store_true",
                    help="commit (default is a dry run that rolls back)")
    ap.add_argument("--discover", action="store_true",
                    help="propose dv_* -> PPDM table matches and exit")
    ap.add_argument("--domains", action="store_true",
                    help=f"build the {DOMAINS_PATH} domain index and exit")
    ap.add_argument("--deep", action="store_true",
                    help="with --discover: propose PPDM names for unmatched "
                         "tables by non-audit column overlap")
    ap.add_argument("--table", help="promote one source table only, e.g. dv_well")
    ap.add_argument("--uwi", help="scope to a single UWI")
    ap.add_argument("--coded", choices=("guarded", "null", "copy"),
                    default="guarded",
                    help="reference-FK columns: guarded (copy if resolvable, "
                         "else NULL), null (omit), copy (raw, may reject)")
    ap.add_argument("--server", help="override connection server")
    ap.add_argument("--database", help="override staging database")
    a = ap.parse_args()

    con = connect(a.server, a.database)
    cur = con.cursor()
    try:
        cur.execute("SELECT DB_NAME()")
        log_db = cur.fetchone()[0]
        cur.execute(f"SELECT DB_ID('{PPDM_DB}')")
        if cur.fetchone()[0] is None:
            print(f"!! {PPDM_DB} not visible from this connection — is it on the "
                  f"same instance as {log_db}?")
            return 2
        print(f"-- source {log_db}.{SRC_SCHEMA}  ->  target {PPDM_DB}.{PPDM_SCHEMA}")

        if a.domains:
            d = build_domains(cur)
            print(f"-- wrote {DOMAINS_PATH}")
            for k in DOMAINS:
                print(f"   {k:14} {len(d[k]):>5} table(s)")
            return 0

        if a.discover:
            discover(cur, deep=a.deep)
            return 0

        entries = [e for e in load_map() if e.get("enabled", True)]
        vmap = load_value_map()
        if a.table:
            entries = [e for e in entries if e["source"].lower() == a.table.lower()]
            if not entries:      # not in the map yet — allow an ad-hoc run
                entries = [{"source": a.table, "target": a.table[3:],
                            "columns": {}}]
        print(f"-- {'APPLY' if a.apply else 'DRY RUN'} · coded={a.coded} "
              f"· {len(entries)} mapping(s)")
        te = tm = 0
        for e in entries:
            src, tgt = e["source"], e["target"]
            print(f"{src} -> {tgt}"
                  + ("  [fan-out]" if e.get("kind") == "fanout" else ""))
            try:
                if e.get("kind") == "fanout":
                    n_e, n_m = promote_fanout(cur, e, a.apply, a.uwi)
                else:
                    n_e, n_m = promote_table(cur, src, tgt, a.apply, a.uwi,
                                             a.coded,
                                             col_map=e.get("columns") or {},
                                             value_map=vmap)
                te += n_e
                tm += n_m
            except Exception as ex:
                print(f"  !! {type(ex).__name__}: {str(ex).splitlines()[0][:200]}")
        print(f"-- TOTAL eligible {te} · inserted {tm}")
        if a.apply:
            con.commit()
            print("-- committed")
        else:
            con.rollback()
            print("-- rolled back (dry run) — re-run with --apply to keep")
        return 0
    finally:
        try:
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
