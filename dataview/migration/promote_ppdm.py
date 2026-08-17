"""
dataview/migration/promote_ppdm.py
=================================
Stage 8 for the migration app: promote a staged table into PPDM39, and fan out
the columns PPDM models as rows.

TWO THINGS core/promote.py CANNOT DO
------------------------------------
1. CROSS-DATABASE. import_data/promote.py line ~173 builds its target as

       tgt_full = f"[dataview].[{target_table}]"

   hardcoded to the dataview schema — the `schema` argument is used for the
   staging table and the well_area insert, but not for the main INSERT. That's
   correct for the app as built (it loads dv_*), but it means the target can
   never be PPDM39.dbo. Here the target is three-part qualified,
   [PPDM39].[dbo].[well], which works on the existing connection because both
   databases live on the same instance — one connection, one transaction, no
   linked server.

2. GENERAL FAN-OUT. promote.py already has a well_area block, and its shape
   (SELECT DISTINCT … WHERE NOT EXISTS … with audit fill) is the house style
   followed here. But it is hardcoded to target `well`, handles exactly ONE
   area per well (it expects AREA_ID/AREA_TYPE to be mapped columns), and never
   seeds the parent `area` table. dv_well carries country, province_state and
   county — three areas per well, whose parents must exist first.

WHAT IS REUSED
--------------
Everything from Stage 5: `mapping.active_pairs` gives [(ppdm_col, select_expr)]
with transforms and constants already compiled in, and `auto_generated_cols`
carries server expressions (NEWID(), audit). So column mapping, synonyms and
transforms all flow through unchanged — this module only changes where the rows
land and adds the fan-out.

FAN-OUT, GENERICALLY
--------------------
One source ROW becomes many target ROWS, distinguished by a discriminator
column. The pattern recurs across PPDM with only the discriminator changing:

    country/province_state/county  -> area + well_area      (AREA_TYPE)
    wide BASE_/TD_/TOP_ strat cols -> strat_well_section    (INTERP_ID)
    operator/current/original ba   -> well_business_assoc   (BA role)

so it is configuration, not code. Config lives in fanouts.json next to this
module; each item names a source column and the label it carries into the
discriminator. Parents are seeded before links, because
well_area.AREA_ID/AREA_TYPE reference area, and area's key is
(AREA_ID, AREA_TYPE) with both NOT NULL.

DRY RUN BY DEFAULT
------------------
Nothing commits unless apply=True. A dry run reports the counts it would insert
and rolls back — including the fan-out, so you can see whether the AREA_TYPE
codes your instance uses actually match before writing anything.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text

PPDM_DB = "PPDM39"
PPDM_SCHEMA = "dbo"
STG_SCHEMA = "stg"

FANOUT_PATH = Path(__file__).resolve().parent / "fanouts.json"

# Audit columns filled server-side when the mapping doesn't supply them.
_AUDIT_FILL = {
    "row_created_by":   "SYSTEM_USER",
    "row_changed_by":   "SYSTEM_USER",
    "row_created_date": "GETDATE()",
    "row_changed_date": "GETDATE()",
    "active_ind":       "'Y'",
}

# Seeded from the measured dv_well / PPDM39.dbo.well comparison. AREA_TYPE codes
# follow PPDM convention; if this instance uses different codes the dry run
# reports the inserts as rejected rather than writing bad rows — check
# r_area_type and edit fanouts.json.
_SEED_FANOUTS = {
    "well": [
        {
            "name": "areas",
            "target": "well_area",
            "seed": {"table": "area", "name_column": "PREFERRED_NAME"},
            "keys": {"UWI": "uwi"},
            "constants": {"SOURCE": "DATAVIEW"},
            "value_column": "AREA_ID",
            "label_column": "AREA_TYPE",
            "items": [
                {"column": "country",        "label": "COUNTRY"},
                {"column": "province_state", "label": "PROVINCE_STATE"},
                {"column": "county",         "label": "COUNTY"},
            ],
        }
    ]
}


def load_fanouts(path: Path = FANOUT_PATH) -> dict:
    try:
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return json.loads(json.dumps(_SEED_FANOUTS))


def save_fanouts(d: dict, path: Path = FANOUT_PATH) -> bool:
    try:
        path.write_text(json.dumps(d, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Target reflection — three-part, so it works from the staging connection
# --------------------------------------------------------------------------- #
def _q(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _target_columns(conn, table, db=PPDM_DB, schema=PPDM_SCHEMA):
    rows = conn.execute(text(
        f"SELECT c.name, c.is_nullable FROM {db}.sys.columns c "
        f"WHERE c.object_id = OBJECT_ID(:t) ORDER BY c.column_id"),
        {"t": f"{db}.{schema}.{table}"}).fetchall()
    return [(r[0], bool(r[1])) for r in rows]


def _target_pk(conn, table, db=PPDM_DB, schema=PPDM_SCHEMA):
    rows = conn.execute(text(
        f"SELECT col.name FROM {db}.sys.indexes i "
        f"JOIN {db}.sys.index_columns ic ON ic.object_id = i.object_id "
        f"  AND ic.index_id = i.index_id "
        f"JOIN {db}.sys.columns col ON col.object_id = ic.object_id "
        f"  AND col.column_id = ic.column_id "
        f"WHERE i.is_primary_key = 1 AND i.object_id = OBJECT_ID(:t) "
        f"ORDER BY ic.key_ordinal"), {"t": f"{db}.{schema}.{table}"}).fetchall()
    return [r[0] for r in rows]


def _staging_columns(conn, table, schema=STG_SCHEMA):
    rows = conn.execute(text(
        "SELECT c.name FROM sys.columns c WHERE c.object_id = OBJECT_ID(:t) "
        "ORDER BY c.column_id"), {"t": f"{schema}.{table}"}).fetchall()
    return [r[0] for r in rows]


# --------------------------------------------------------------------------- #
# Main promote
# --------------------------------------------------------------------------- #
def promote(engine, staging_table, target_table, mapping,
            stg_schema=STG_SCHEMA, db=PPDM_DB, schema=PPDM_SCHEMA,
            apply=False, seed_refs=False, log=print) -> dict:
    """INSERT the staged rows into [db].[schema].[target_table].

    Uses the Stage-5 mapping verbatim — active_pairs already carries transforms
    and constants — so nothing about the column work changes here.
    """
    pairs = list(getattr(mapping, "active_pairs", []) or [])
    auto = list(getattr(mapping, "auto_generated_cols", []) or [])
    if not pairs:
        return {"ok": False, "message": "mapping has no active columns",
                "inserted": 0}

    tgt_full = f"[{db}].[{schema}].[{target_table}]"
    stg_full = f"[{stg_schema}].[{staging_table}]"

    with engine.connect() as conn:
        tgt_cols = {c.upper() for c, _n in _target_columns(conn, target_table,
                                                           db, schema)}
        if not tgt_cols:
            return {"ok": False, "inserted": 0,
                    "message": f"{tgt_full} not found"}
        pk = _target_pk(conn, target_table, db, schema)

    # A mapped column the target doesn't have would abort the whole INSERT;
    # report it instead of letting SQL Server decide.
    unknown = [c for c, _e in pairs if c.upper() not in tgt_cols]
    if unknown:
        log(f"  ⚠ not columns of {target_table}, skipped: {', '.join(unknown)}")
    pairs = [(c, e) for c, e in pairs if c.upper() in tgt_cols]

    cols = [c for c, _e in pairs]
    exprs = [e for _c, e in pairs]
    # auto_generated_cols returns MappedColumn objects (NOT (col, expr) pairs);
    # each carries its own server-side select_expr, e.g. NEWID().
    for m in auto:
        c = getattr(m, "ppdm_col", None)
        e = getattr(m, "select_expr", None)
        if not c or not e:
            continue
        if c.upper() in tgt_cols and c.upper() not in {x.upper() for x in cols}:
            cols.append(c)
            exprs.append(e)

    mapped_up = {c.upper() for c in cols}

    # The projection goes through a derived table `x`, and the dedupe correlates
    # against x's OUTPUT columns rather than against the raw select expressions.
    #
    # This is not stylistic. mapping.py's select_expr renders a source column
    # unqualified — `[uwi]`, not `s.[uwi]` — and SQL Server resolves an
    # unqualified name in a correlated subquery against the INNERMOST table
    # first. Written the obvious way,
    #     NOT EXISTS (SELECT 1 FROM well t WHERE t.[UWI] = [uwi])
    # binds [uwi] to well.UWI, becomes t.UWI = t.UWI, matches every existing
    # row, and silently reports 0 eligible — which is exactly what happened.
    # Naming the projection first removes the ambiguity entirely.
    proj = ",\n               ".join(f"{e} AS [{c}]" for c, e in zip(cols, exprs))
    inner = f"(SELECT {proj}\n          FROM {stg_full} s) x"

    not_exists = ""
    if pk and all(k.upper() in mapped_up for k in pk):
        on = " AND ".join(f"t.[{k}] = x.[{k}]" for k in pk)
        not_exists = f"\n WHERE NOT EXISTS (SELECT 1 FROM {tgt_full} t WHERE {on})"
    elif pk:
        log(f"  ⚠ PK ({', '.join(pk)}) not fully mapped — insert-only, "
            f"re-runs will duplicate")

    col_list = ", ".join(f"[{c}]" for c in cols)
    sel_list = ", ".join(f"x.[{c}]" for c in cols)
    sql_count = f"SELECT COUNT(*) FROM {inner}{not_exists}"
    sql_insert = (f"INSERT INTO {tgt_full} ({col_list})\n"
                  f"SELECT {sel_list}\n  FROM {inner}{not_exists}")

    out = {"ok": True, "inserted": 0, "eligible": 0, "sql": sql_insert,
           "fanout": []}
    conn = engine.connect()
    trans = conn.begin()
    try:
        if seed_refs:
            n = seed_reference_codes(conn, target_table, db, schema, log=log)
            if n:
                log(f"  seeded {n} reference code(s)")
        out["eligible"] = int(conn.execute(text(sql_count)).scalar() or 0)
        if out["eligible"]:
            out["inserted"] = conn.execute(text(sql_insert)).rowcount or 0
        log(f"  {target_table}: {out['eligible']} eligible, "
            f"{out['inserted']} inserted{'' if apply else ' (dry run)'}")

        out["fanout"] = run_fanouts(conn, staging_table, target_table,
                                    stg_schema, db, schema, log=log)

        trans.commit() if apply else trans.rollback()
        out["message"] = ("committed" if apply else
                          "rolled back (dry run) — re-run with apply=True")
    except Exception as e:
        trans.rollback()
        out.update(ok=False,
                   message=f"{type(e).__name__}: {str(e).splitlines()[0][:300]}")
    finally:
        conn.close()
    return out


# --------------------------------------------------------------------------- #
# Fan-out
# --------------------------------------------------------------------------- #
def run_fanouts(conn, staging_table, target_table, stg_schema=STG_SCHEMA,
                db=PPDM_DB, schema=PPDM_SCHEMA, cfg=None, log=print) -> list:
    """Every configured fan-out for this target. Runs inside the caller's
    transaction, so a dry run rolls the fan-out back too."""
    cfg = cfg if cfg is not None else load_fanouts()
    specs = cfg.get(target_table.lower(), [])
    results = []
    for spec in specs:
        try:
            results.append(_one_fanout(conn, staging_table, spec, stg_schema,
                                       db, schema, log))
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
            log(f"  ⚠ fan-out {spec.get('name', spec.get('target'))}: {msg}")
            results.append({"name": spec.get("name"), "ok": False,
                            "message": msg})
    return results


def _one_fanout(conn, staging_table, spec, stg_schema, db, schema, log) -> dict:
    tgt = spec["target"]
    keys = spec.get("keys") or {}
    consts = spec.get("constants") or {}
    val_col = spec["value_column"]
    lab_col = spec["label_column"]
    seed = spec.get("seed") or None
    items = spec.get("items") or []

    stg_full = f"[{stg_schema}].[{staging_table}]"
    tgt_full = f"[{db}].[{schema}].[{tgt}]"

    stg_cols = {c.upper(): c for c in _staging_columns(conn, staging_table,
                                                       stg_schema)}
    tgt_cols = _target_columns(conn, tgt, db, schema)
    tgt_up = {c.upper() for c, _n in tgt_cols}
    tgt_pk = _target_pk(conn, tgt, db, schema)

    seeded = linked = 0
    detail = []
    for it in items:
        scol, label = it.get("column"), it.get("label")
        if not scol or not label:
            continue
        if scol.upper() not in stg_cols:
            log(f"    {label}: no [{scol}] in staging — skipped")
            continue
        s = stg_cols[scol.upper()]
        v = f"LTRIM(RTRIM(CONVERT(nvarchar(255), s.[{s}])))"
        where = f"s.[{s}] IS NOT NULL AND {v} <> ''"

        # ---- parent rows first: well_area.(AREA_ID,AREA_TYPE) -> area -----
        if seed:
            stab = seed["table"]
            sfull = f"[{db}].[{schema}].[{stab}]"
            scols_t = _target_columns(conn, stab, db, schema)
            sup = {c.upper(): c for c, _n in scols_t}
            names, exprs = [val_col, lab_col], [v, _q(label)]
            nm = seed.get("name_column")
            if nm and nm.upper() in sup:
                names.append(nm)
                exprs.append(v)
            for c, _n in scols_t:
                if c.lower() in _AUDIT_FILL and c.upper() not in {
                        x.upper() for x in names}:
                    names.append(c)
                    exprs.append(_AUDIT_FILL[c.lower()])
            ne = (f" AND NOT EXISTS (SELECT 1 FROM {sfull} p "
                  f"WHERE p.[{val_col}] = {v} AND p.[{lab_col}] = {_q(label)})")
            n = int(conn.execute(text(
                f"SELECT COUNT(DISTINCT {v}) FROM {stg_full} s "
                f"WHERE {where}{ne}")).scalar() or 0)
            if n:
                conn.execute(text(
                    f"INSERT INTO {sfull} "
                    f"({', '.join('[' + c + ']' for c in names)}) "
                    f"SELECT DISTINCT {', '.join(exprs)} "
                    f"FROM {stg_full} s WHERE {where}{ne}"))
            seeded += n

        # ---- link rows ----------------------------------------------------
        names, exprs = [], []
        for tc, sc in keys.items():
            if sc.upper() not in stg_cols:
                raise RuntimeError(f"key column [{sc}] not in staging")
            names.append(tc)
            exprs.append(f"s.[{stg_cols[sc.upper()]}]")
        for tc, cv in consts.items():
            names.append(tc)
            exprs.append(_q(cv))
        names += [val_col, lab_col]
        exprs += [v, _q(label)]
        by = {n.upper(): e for n, e in zip(names, exprs)}
        for c, _n in tgt_cols:
            if c.lower() in _AUDIT_FILL and c.upper() not in by:
                names.append(c)
                exprs.append(_AUDIT_FILL[c.lower()])

        missing = [n for n in names if n.upper() not in tgt_up]
        if missing:
            raise RuntimeError(f"{tgt} has no column(s) {', '.join(missing)}")

        ne = ""
        if tgt_pk and all(k.upper() in by for k in tgt_pk):
            on = " AND ".join(f"t.[{k}] = {by[k.upper()]}" for k in tgt_pk)
            ne = f" AND NOT EXISTS (SELECT 1 FROM {tgt_full} t WHERE {on})"
        n = int(conn.execute(text(
            f"SELECT COUNT(*) FROM {stg_full} s WHERE {where}{ne}")).scalar() or 0)
        if n:
            conn.execute(text(
                f"INSERT INTO {tgt_full} "
                f"({', '.join('[' + c + ']' for c in names)}) "
                f"SELECT {', '.join(exprs)} FROM {stg_full} s WHERE {where}{ne}"))
        linked += n
        detail.append({"label": label, "source": s, "rows": n})
        log(f"    {label}: {n} {tgt} row(s) from [{s}]")

    log(f"  fan-out {spec.get('name', tgt)}: {seeded} parent row(s), "
        f"{linked} {tgt} row(s)")
    return {"name": spec.get("name", tgt), "ok": True, "target": tgt,
            "seeded": seeded, "linked": linked, "items": detail}


def _fk_target(conn, table, column, db=PPDM_DB, schema=PPDM_SCHEMA):
    """(ref_table, ref_column) for a single-column FK on table.column, else None.

    Composite FKs are ignored: seeding one member of a composite in isolation
    would create a half-row that satisfies nothing.
    """
    rows = conn.execute(text(
        f"SELECT rt.name, rc.name, fk.object_id, "
        f"       (SELECT COUNT(*) FROM {db}.sys.foreign_key_columns x "
        f"        WHERE x.constraint_object_id = fk.object_id) AS width "
        f"FROM {db}.sys.foreign_keys fk "
        f"JOIN {db}.sys.foreign_key_columns fkc "
        f"     ON fkc.constraint_object_id = fk.object_id "
        f"JOIN {db}.sys.columns cp ON cp.object_id = fkc.parent_object_id "
        f"     AND cp.column_id = fkc.parent_column_id "
        f"JOIN {db}.sys.tables  rt ON rt.object_id = fkc.referenced_object_id "
        f"JOIN {db}.sys.columns rc ON rc.object_id = fkc.referenced_object_id "
        f"     AND rc.column_id = fkc.referenced_column_id "
        f"WHERE fk.parent_object_id = OBJECT_ID(:t) AND cp.name = :c"),
        {"t": f"{db}.{schema}.{table}", "c": column}).fetchall()
    for rt, rc, _oid, width in rows:
        if int(width) == 1:
            return (rt, rc)
    return None


def check_reference_codes(conn, target_table, db=PPDM_DB, schema=PPDM_SCHEMA,
                          cfg=None) -> list:
    """Which literal values a fan-out writes are missing from the reference
    tables their columns foreign-key to.

    TWO kinds of literal get written, and BOTH can be FK-constrained:

      * the DISCRIMINATOR  — AREA_TYPE = 'COUNTRY', which FKs to r_area_type
      * the CONSTANTS      — SOURCE = 'DATAVIEW', which FKs to r_source

    Checking only the discriminator is why the first run cleared A_R_ARTY_FK and
    then failed WAR_R_S_FK. Every literal the fan-out emits is checked here.
    """
    cfg = cfg if cfg is not None else load_fanouts()
    out = []
    for spec in cfg.get(target_table.lower(), []):
        labels = sorted({str(i.get("label")) for i in spec.get("items", [])
                         if i.get("label")})
        seed_tbl = (spec.get("seed") or {}).get("table")
        lab_col = spec["label_column"]

        # (table, column, values) for every literal this spec writes
        checks = []
        if labels:
            if seed_tbl:
                checks.append((seed_tbl, lab_col, labels))
            checks.append((spec["target"], lab_col, labels))
        for tcol, val in (spec.get("constants") or {}).items():
            checks.append((spec["target"], tcol, [str(val)]))

        seen = set()
        for tbl, col, values in checks:
            ft = _fk_target(conn, tbl, col, db, schema)
            if not ft:
                continue
            ref_tbl, ref_col = ft
            if (ref_tbl.lower(), ref_col.lower()) in seen:
                continue
            seen.add((ref_tbl.lower(), ref_col.lower()))
            have = {str(r[0]).upper() for r in conn.execute(text(
                f"SELECT [{ref_col}] FROM [{db}].[{schema}].[{ref_tbl}]"
            )).fetchall()}
            missing = [v for v in values if v.upper() not in have]
            out.append({"spec": spec.get("name", spec.get("target")),
                        "via": f"{tbl}.{col}", "ref_table": ref_tbl,
                        "ref_column": ref_col, "present": len(have),
                        "missing": missing})
    return out


def _target_columns_sized(conn, table, db=PPDM_DB, schema=PPDM_SCHEMA):
    """(name, nullable, type_name, max_chars) — max_chars is None for
    non-character types, and already halved for n-types (SQL Server reports
    max_length in BYTES, so nvarchar(12) comes back as 24)."""
    rows = conn.execute(text(
        f"SELECT c.name, c.is_nullable, ty.name, c.max_length "
        f"FROM {db}.sys.columns c "
        f"JOIN {db}.sys.types ty ON ty.user_type_id = c.user_type_id "
        f"WHERE c.object_id = OBJECT_ID(:t) ORDER BY c.column_id"),
        {"t": f"{db}.{schema}.{table}"}).fetchall()
    out = []
    for name, nullable, tname, mlen in rows:
        t = (tname or "").lower()
        chars = None
        if t in ("varchar", "char"):
            chars = None if mlen == -1 else int(mlen)
        elif t in ("nvarchar", "nchar"):
            chars = None if mlen == -1 else int(mlen) // 2
        out.append((name, bool(nullable), t, chars))
    return out


def seed_reference_codes(conn, target_table, db=PPDM_DB, schema=PPDM_SCHEMA,
                         cfg=None, log=print) -> int:
    """Insert the missing discriminator codes into their reference tables.

    Fills the key with the code itself and any other NOT NULL column (plus the
    conventional LONG_NAME / SHORT_NAME / ABBREVIATION) with the same text —
    enough to satisfy the constraints without inventing data.

    Fill values are TRUNCATED to each column's width. PPDM's descriptive columns
    are narrow and inconsistent — r_area_type.ABBREVIATION is 12 characters,
    which 'PROVINCE_STATE' overflows — and an untruncated fill aborts the whole
    insert with a truncation error. The KEY column is never truncated: a
    shortened code would silently fail to match the value the fan-out writes,
    which is far worse than a clipped abbreviation. If the code itself doesn't
    fit the key, the code is skipped and reported.

    Runs in the caller's transaction, so a dry run rolls it back.
    """
    added = 0
    for gap in check_reference_codes(conn, target_table, db, schema, cfg):
        if not gap["missing"]:
            continue
        ref_tbl, ref_col = gap["ref_table"], gap["ref_column"]
        cols = _target_columns_sized(conn, ref_tbl, db, schema)
        width = {c.upper(): chars for c, _n, _t, chars in cols}
        fill = [c for c, nullable, _t, _w in cols
                if c.upper() != ref_col.upper()
                and (not nullable
                     or c.upper() in ("LONG_NAME", "SHORT_NAME", "ABBREVIATION"))]

        key_w = width.get(ref_col.upper())
        for code in gap["missing"]:
            if key_w and len(code) > key_w:
                log(f"    ⚠ {ref_tbl}.{ref_col} is {key_w} char(s) — "
                    f"'{code}' does not fit, skipped. Shorten the label in "
                    f"fanouts.json.")
                continue
            names, exprs = [ref_col], [_q(code)]
            for c in fill:
                w = width.get(c.upper())
                v = _AUDIT_FILL.get(c.lower())
                if v is None:
                    v = _q(code[:w] if w else code)
                names.append(c)
                exprs.append(v)
            conn.execute(text(
                f"INSERT INTO [{db}].[{schema}].[{ref_tbl}] "
                f"({', '.join('[' + c + ']' for c in names)}) "
                f"VALUES ({', '.join(exprs)})"))
            added += 1
            log(f"    + {ref_tbl}.{ref_col} = {code}")
    return added


def check_data_refs(conn, staging_table, mapping, target_table,
                    stg_schema=STG_SCHEMA, db=PPDM_DB, schema=PPDM_SCHEMA,
                    sample=8) -> list:
    """Which VALUES in the staged data don't resolve against their reference
    tables.

    Distinct from check_reference_codes, which checks the literals the fan-out
    writes. This checks the DATA: a mapped column that foreign-keys to a
    reference table carries whatever the source happens to hold, and every
    unregistered value is a rejected insert.

    Reports ALL of them in one pass. Discovering these one FK per run — insert,
    fail, fix, repeat — is the slow way to learn something the catalog can
    answer in a single query per column.
    """
    stg_full = f"[{stg_schema}].[{staging_table}]"
    out = []

    # 1. AUTO-GENERATED literals. mapping.py's AUDIT_COLUMNS supplies values
    #    like SOURCE = 'PPDM_LOADER' that never appear in active_pairs, so a
    #    check that only looks at mapped columns misses them entirely — which
    #    is exactly how field.SOURCE reached the database unchecked and failed
    #    FLD_R_S_FK. Expressions that aren't literals (GETDATE(), SYSTEM_USER,
    #    NEWID()) are skipped: there's nothing to verify ahead of time.
    for m in list(getattr(mapping, "auto_generated_cols", []) or []):
        tcol = getattr(m, "ppdm_col", "")
        expr = (getattr(m, "auto_gen_expr", "") or "").strip()
        if not tcol or len(expr) < 2 or not (expr[0] == "'" and expr[-1] == "'"):
            continue
        lit = expr[1:-1].replace("''", "'")
        ft = _fk_target(conn, target_table, tcol, db, schema)
        if not ft:
            continue
        ref_tbl, ref_col = ft
        n = conn.execute(text(
            f"SELECT COUNT(*) FROM [{db}].[{schema}].[{ref_tbl}] "
            f"WHERE [{ref_col}] = :v"), {"v": lit}).scalar() or 0
        if not n:
            out.append({"column": tcol, "ref_table": ref_tbl,
                        "ref_column": ref_col, "missing": [lit],
                        "sample": [lit], "kind": "auto-generated literal"})

    # 2. MAPPED data values.
    for tcol, expr in list(getattr(mapping, "active_pairs", []) or []):
        ft = _fk_target(conn, target_table, tcol, db, schema)
        if not ft:
            continue
        ref_tbl, ref_col = ft
        ref_full = f"[{db}].[{schema}].[{ref_tbl}]"
        rows = conn.execute(text(
            f"SELECT DISTINCT x.[v] FROM (SELECT {expr} AS [v] "
            f"FROM {stg_full} s) x "
            f"WHERE x.[v] IS NOT NULL AND LTRIM(RTRIM(x.[v])) <> '' "
            f"  AND NOT EXISTS (SELECT 1 FROM {ref_full} r "
            f"                  WHERE r.[{ref_col}] = x.[v])")).fetchall()
        vals = [str(r[0]) for r in rows]
        if vals:
            out.append({"column": tcol, "ref_table": ref_tbl,
                        "ref_column": ref_col, "missing": vals,
                        "sample": vals[:sample], "kind": "data"})
    return out


def seed_data_refs(conn, staging_table, mapping, target_table,
                   stg_schema=STG_SCHEMA, db=PPDM_DB, schema=PPDM_SCHEMA,
                   log=print) -> int:
    """Register the unresolved source values in their reference tables.

    This is a convenience for getting a load through, NOT a substitute for
    judgement. Inserting every distinct source value as a reference code
    accepts the source's vocabulary wholesale — misspellings, case variants and
    all. The pipeline's FK Resolution stage exists precisely so a human can map
    'PRODUCING' onto an existing code instead of minting a new one. Use this to
    prove a path; use Stage 6 to do it properly.
    """
    added = 0
    for gap in check_data_refs(conn, staging_table, mapping, target_table,
                               stg_schema, db, schema):
        ref_tbl, ref_col = gap["ref_table"], gap["ref_column"]
        cols = _target_columns_sized(conn, ref_tbl, db, schema)
        width = {c.upper(): chars for c, _n, _t, chars in cols}
        fill = [c for c, nullable, _t, _w in cols
                if c.upper() != ref_col.upper()
                and (not nullable
                     or c.upper() in ("LONG_NAME", "SHORT_NAME", "ABBREVIATION"))]
        key_w = width.get(ref_col.upper())
        for val in gap["missing"]:
            if key_w and len(val) > key_w:
                log(f"    ⚠ '{val}' exceeds {ref_tbl}.{ref_col} "
                    f"({key_w} chars) — skipped")
                continue
            names, exprs = [ref_col], [_q(val)]
            for c in fill:
                w = width.get(c.upper())
                v = _AUDIT_FILL.get(c.lower())
                if v is None:
                    v = _q(val[:w] if w else val)
                names.append(c)
                exprs.append(v)
            conn.execute(text(
                f"INSERT INTO [{db}].[{schema}].[{ref_tbl}] "
                f"({', '.join('[' + c + ']' for c in names)}) "
                f"VALUES ({', '.join(exprs)})"))
            added += 1
            log(f"    + {ref_tbl}.{ref_col} = {val}")
    return added


# --------------------------------------------------------------------------- #
# CLI — fan-out only (it needs no mapping), for checking codes before a load
# --------------------------------------------------------------------------- #
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Fan-out check / run against a staged table")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--staging", default="src_dv_well")
    ap.add_argument("--stg-schema", default=STG_SCHEMA)
    ap.add_argument("--target", default="well",
                    help="the PPDM target whose fan-outs to run")
    ap.add_argument("--ppdm-db", default=PPDM_DB)
    ap.add_argument("--ppdm-schema", default=PPDM_SCHEMA)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--show", action="store_true", help="print the config")
    ap.add_argument("--check-refs", action="store_true",
                    help="report discriminator codes missing from their "
                         "reference tables, and exit")
    ap.add_argument("--seed-refs", action="store_true",
                    help="insert the missing discriminator codes before "
                         "running the fan-out")
    ap.add_argument("--promote", action="store_true",
                    help="build the mapping automatically (auto-match + "
                         "synonyms) and run the full load: wells, then fan-out")
    ap.add_argument("--min-score", type=int, default=60,
                    help="auto-match threshold for --promote")
    ap.add_argument("--check-data", action="store_true",
                    help="with --promote: report every source VALUE that "
                         "doesn't resolve against its reference table, and exit")
    ap.add_argument("--seed-data", action="store_true",
                    help="with --promote: register those unresolved values. "
                         "Convenience for proving a path — Stage 6 FK "
                         "Resolution is the considered way to do this")
    a = ap.parse_args()

    cfg = load_fanouts()
    if a.show:
        print(json.dumps(cfg, indent=2))
        return 0

    from dataview.core.schema_introspect import make_engine
    engine = make_engine(a.server, a.database)

    if a.promote:
        # The target model needs an engine on PPDM39 (sys.* reads whichever
        # database the engine is connected to). The load itself still runs on
        # the staging connection via three-part naming, so it stays one
        # transaction.
        from dataview.migration.ppdm_model import get_ppdm_schema
        from dataview.migration.synonyms import build_mapping_with_synonyms

        sch = get_ppdm_schema(make_engine(a.server, a.ppdm_db), a.ppdm_schema)
        td = sch.get_table(a.target)
        if td is None:
            print(f"!! {a.target} is not in the PPDM model — is it in scope?")
            return 2
        with engine.connect() as c:
            src_cols = _staging_columns(c, a.staging, a.stg_schema)
        if not src_cols:
            print(f"!! {a.stg_schema}.{a.staging} not found — run "
                  f"`py -m dataview.migration.db_source --stage <table>` first")
            return 2

        cm = build_mapping_with_synonyms(a.target, td.columns, src_cols,
                                         a.min_score)
        mapped = [m for m in cm.mapped if getattr(m, "source_col", "")]
        print(f"-- {a.target}: {len(td.columns)} target column(s), "
              f"{len(src_cols)} source column(s), {len(mapped)} mapped")
        for m in mapped:
            print(f"   {m.ppdm_col:28} <- {m.source_col:24} "
                  f"{getattr(m, 'match_label', '')}")
        unmapped = [c for c in src_cols
                    if c.upper() not in {m.source_col.upper() for m in mapped}]
        if unmapped:
            print(f"-- source column(s) with no target ({len(unmapped)}): "
                  + ", ".join(unmapped))

        if a.check_data:
            with engine.connect() as c:
                gaps = check_data_refs(c, a.staging, cm, a.target,
                                       a.stg_schema, a.ppdm_db, a.ppdm_schema)
            if not gaps:
                print("-- every mapped FK value resolves")
                return 0
            print(f"-- {len(gaps)} column(s) with unresolved values")
            for g in gaps:
                print(f"   {g['column']:28} -> {g['ref_table']:24} "
                      f"{len(g['missing']):>5} unregistered"
                      + (f"  [{g['kind']}]" if g.get('kind') != 'data' else ""))
                print(f"      {', '.join(g['sample'])}"
                      + (" …" if len(g['missing']) > len(g['sample']) else ""))
            print("-- re-run with --seed-data to register them, or resolve "
                  "them properly in the pipeline's FK stage")
            return 2

        print(f"-- {'APPLY' if a.apply else 'DRY RUN'}")
        conn0 = engine.connect()
        t0 = conn0.begin()
        try:
            if a.seed_data:
                n = seed_data_refs(conn0, a.staging, cm, a.target,
                                   a.stg_schema, a.ppdm_db, a.ppdm_schema,
                                   log=print)
                print(f"-- registered {n} value(s) from the data")
                t0.commit() if a.apply else t0.rollback()
            else:
                t0.rollback()
        finally:
            conn0.close()
        if a.seed_data and not a.apply:
            print("-- NOTE: dry run rolled the value registration back, so the "
                  "load below will still fail on those FKs. Use --apply to "
                  "keep them.")
        res = promote(engine, a.staging, a.target, cm, a.stg_schema,
                      a.ppdm_db, a.ppdm_schema, apply=a.apply,
                      seed_refs=a.seed_refs, log=print)
        print(f"-- {res.get('message', '')}")
        return 0 if res.get("ok") else 2

    if a.check_refs:
        with engine.connect() as conn:
            gaps = check_reference_codes(conn, a.target, a.ppdm_db,
                                         a.ppdm_schema, cfg)
        if not gaps:
            print("-- no discriminator columns are FK-constrained "
                  "(nothing to seed)")
            return 0
        rc = 0
        for g in gaps:
            state = ("OK" if not g["missing"]
                     else f"MISSING {', '.join(g['missing'])}")
            print(f"   {g['via']} -> {g['ref_table']} "
                  f"({g['present']} code(s) present) : {state}")
            rc = rc or bool(g["missing"])
        if rc:
            print("-- re-run with --seed-refs (add --apply to keep them)")
        return 2 if rc else 0

    print(f"-- {a.database}.{a.stg_schema}.{a.staging} -> "
          f"{a.ppdm_db}.{a.ppdm_schema} · target {a.target} "
          f"· {'APPLY' if a.apply else 'DRY RUN'}")

    conn = engine.connect()
    trans = conn.begin()
    try:
        if a.seed_refs:
            n = seed_reference_codes(conn, a.target, a.ppdm_db,
                                     a.ppdm_schema, cfg, log=print)
            print(f"-- seeded {n} reference code(s)")
        res = run_fanouts(conn, a.staging, a.target, a.stg_schema,
                          a.ppdm_db, a.ppdm_schema, cfg, log=print)
        trans.commit() if a.apply else trans.rollback()
        print("-- committed" if a.apply else "-- rolled back (dry run)")
        return 0 if all(r.get("ok") for r in res) else 2
    except Exception as e:
        trans.rollback()
        print(f"!! {type(e).__name__}: {str(e).splitlines()[0][:300]}")
        print("-- rolled back, nothing written")
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(_main())
