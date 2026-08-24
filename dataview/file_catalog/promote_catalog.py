"""
promote_catalog.py
==================
DataView v3 — MOVE captured catalog rows up into the curated dv_* tables.

Model: cat_* is a transient staging area. Promotion MOVES rows up — copies the
shared columns into dv_*, then deletes the rows from cat_*. cat_* only ever
holds not-yet-promoted rows; dv_* is the record of truth. There is no PROMOTED
flag dance and no separate cleanup pass — the move IS the cleanup.

Idempotency (so re-cataloging a changed file never duplicates):
  * DETAIL tables carry INVENTORY_ID (see dv_add_inventory_id.sql). Before
    inserting, promote DELETEs the dv_* rows whose INVENTORY_ID matches the
    files being moved — a per-FILE replace. Other files' rows for the same well
    are untouched. A detail table WITHOUT INVENTORY_ID falls back to insert-only
    (and says so), so nothing breaks before the migration is applied.
  * dv_well is the HEADER: create it for UWIs that don't have one, and
    fill-null update existing headers from the latest captured row (never
    clobber a good value). Then the consumed cat_well rows are deleted.

Order: parents first (dv_well before its children), from the FK graph, so a
child only moves once its header exists.

Dry-run by default (reports eligible counts). Use --apply to execute. --uwi
scopes to a single well.

    python promote_catalog.py                 # report what would move
    python promote_catalog.py --apply         # move all eligible
    python promote_catalog.py --uwi 4231712345 --apply
"""
from __future__ import annotations

import argparse
import sys
import uuid

import pyodbc

from dataview.file_catalog.build_catalog_mirror import (
    DV_SCHEMA, CAT_SCHEMA, cat_name, connect,
    fetch_columns, discover_tables,
)

# Provenance kept OUT of dv_*, EXCEPT INVENTORY_ID — that one is now copied as
# lineage so promote can do a per-file replace. (CAT_ROW_ID etc. never travel.)
_NEVER_COPY = {"CAT_ROW_ID", "SOURCE_PATH", "PROMOTED", "PROMOTED_AT",
               "CAPTURED_AT"}

# Catalog-promoted rows carry a registered data-source code in dv_*.source
# (an FK to dv_r_source). The cat_*.SOURCE column is loader provenance
# ("OFFICE" / "OSDU" / "WITSML" …), NOT a registered source — copying it into
# the FK column fails (e.g. dv_prod_entity). So promote writes this one code
# instead; the per-loader provenance stays traceable via INVENTORY_ID lineage.
_CATALOG_SOURCE = "CATALOG"

# Governance: when True, a well is promoted to dv_well only if its captured
# header carries surface coordinates. Coordless wells are HELD in the mirror
# (not lost) until a document or a gold/UWI enrich supplies a location — an
# unmappable well never reaches the gold table. Set False to promote regardless.
REQUIRE_WELL_COORDS = True

# WHAT EACH DEDICATED PROMOTER HANDLES.
#
# The generic loop walks build_catalog_mirror.MIRROR_TABLES. These mirrors are
# moved by a named promoter INSTEAD, so they are deliberately absent from that
# allowlist — a table in both would have its rows moved twice.
#
# This dict exists so the relationship is DECLARED rather than inferred.
# check_mirror_registry.py reads it to answer "is any mirror walked by
# nothing"; without it, that check falls back to scanning this file's source
# text and cannot tell a real handler from a mention in a comment. It is also
# the one place a newcomer can see what promotes what without reading 1,800
# lines.
#
# A mirror in NEITHER this dict nor MIRROR_TABLES is invisible twice over: no
# mirror is built for it, and rows written into a hand-made one are silently
# stepped past — reported as neither moved nor held. cat_well_casing sat in
# exactly that state at 148 rows staged, 0 promoted, no error.
DEDICATED_PROMOTERS = {
    "cat_field":      "promote_field",
    "cat_land_tract": "promote_land_tract",
    "cat_boundary":   "promote_boundary",
    "cat_pipeline":   "promote_pipeline",
    "cat_log_curve":  "promote_las_catalog",
}

# Per-step wall-clock for the promote stage, filled by _safe_promote and printed
# as a "slowest first" summary at the end of run_promote so the promote seconds
# break down by table/promoter (pure-DB, environment-independent profiling).
_STEP_TIMES: dict = {}

# One-time bulk-reflected schema metadata for the promote run, so each per-table
# promoter does dict lookups instead of ~8 INFORMATION_SCHEMA / sys.* round-trips
# (the fixed ~1.3-2s/table overhead — worse over a networked SQL Server). Primed
# by _prime_metadata() at the top of run_promote; falls back to live queries when
# not primed (so the reflect helpers still work when called outside promote).
_META: dict = {"primed": False, "schemas": set(), "obj": set(),
               "cols": {}, "computed": {}, "pk": {}}


def _prime_metadata(cur, schemas=("dataview", "file_catalog")):
    """Bulk-reflect objects / columns / computed columns / PKs for `schemas`.

    The column reflect emits ~2300 catalog rows, which SQL Server materialises
    slowly (~10s standalone, ~24s inside the promote transaction) even though a
    COUNT of the same join is instant — a per-row cost on the sys.* metadata
    views, not our query. Since the schema is static between runs, the whole
    reflect is cached to disk keyed by a cheap CHECKSUM_AGG signature over
    columns + computed-flag + PK key order. On an unchanged signature the reflect
    reloads in ~0.1s; a real DDL change flips the signature and forces one fresh
    reflect. Any cache/signature error falls back to a live reflect."""
    import os, pickle, tempfile, time as _t
    _sub: dict = {}
    _in = ",".join("'" + s + "'" for s in schemas)
    _META.update(obj=set(), cols={}, computed={}, pk={},
                 schemas={s.lower() for s in schemas}, primed=False)

    # cheap schema signature — aggregates only, so no slow per-row emit
    _t0 = _t.monotonic()
    try:
        db, ncol, csig, psig = cur.execute(
            f"SELECT DB_NAME(), "
            f"(SELECT COUNT(*) FROM sys.columns c "
            f"   JOIN sys.objects o ON o.object_id=c.object_id "
            f"   JOIN sys.schemas s ON s.schema_id=o.schema_id "
            f"   WHERE s.name IN ({_in}) AND o.type IN ('U','V')), "
            f"(SELECT CHECKSUM_AGG(CHECKSUM(s.name,o.name,c.name,c.column_id,"
            f"        c.user_type_id,CONVERT(int,c.is_computed))) "
            f"   FROM sys.columns c "
            f"   JOIN sys.objects o ON o.object_id=c.object_id "
            f"   JOIN sys.schemas s ON s.schema_id=o.schema_id "
            f"   WHERE s.name IN ({_in}) AND o.type IN ('U','V')), "
            f"(SELECT CHECKSUM_AGG(CHECKSUM(s.name,o.name,col.name,ic.key_ordinal)) "
            f"   FROM sys.indexes i "
            f"   JOIN sys.index_columns ic ON ic.object_id=i.object_id "
            f"     AND ic.index_id=i.index_id "
            f"   JOIN sys.columns col ON col.object_id=ic.object_id "
            f"     AND col.column_id=ic.column_id "
            f"   JOIN sys.objects o ON o.object_id=i.object_id "
            f"   JOIN sys.schemas s ON s.schema_id=o.schema_id "
            f"   WHERE i.is_primary_key=1 AND s.name IN ({_in}))").fetchone()
        _sig = (db, tuple(sorted(s.lower() for s in schemas)), ncol, csig, psig)
    except Exception:
        db, _sig = "db", None
    _sub["signature"] = _t.monotonic() - _t0

    # cache file (per database); best-effort, never fatal
    _cf = None
    try:
        _base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        _cdir = os.path.join(_base, "DataWrangler", "cache")
        os.makedirs(_cdir, exist_ok=True)
        _cf = os.path.join(_cdir, f"promote_meta_{db}.pkl")
    except Exception:
        _cf = None

    # fast path: reload the cached reflect when the signature matches
    if _sig and _cf and os.path.exists(_cf):
        _t0 = _t.monotonic()
        try:
            with open(_cf, "rb") as _fh:
                _c = pickle.load(_fh)
            if _c.get("sig") == _sig:
                _META.update(obj=_c["obj"], cols=_c["cols"],
                             computed=_c["computed"], pk=_c["pk"], primed=True)
                _sub["cache_hit"] = 1.0
                _sub["reflect"] = _t.monotonic() - _t0
                _META["prime_sub"] = _sub
                return
        except Exception:
            pass
    _sub["cache_hit"] = 0.0

    # slow path: full reflect via sys.* (first run / signature changed), then cache
    def _q(_name, _sql):
        _t0 = _t.monotonic()
        cur.execute(_sql)
        rows = cur.fetchall()
        _sub[_name] = _t.monotonic() - _t0
        return rows
    try:
        for sc, tb in _q("obj",
                f"SELECT LOWER(s.name), LOWER(o.name) FROM sys.objects o "
                f"JOIN sys.schemas s ON s.schema_id=o.schema_id "
                f"WHERE s.name IN ({_in}) AND o.type IN ('U','V')"):
            _META["obj"].add((sc, tb))
        for sc, tb, col, dt in _q("cols",
                f"SELECT LOWER(s.name), LOWER(o.name), c.name, ty.name "
                f"FROM sys.columns c "
                f"JOIN sys.objects o  ON o.object_id  = c.object_id "
                f"JOIN sys.schemas s  ON s.schema_id  = o.schema_id "
                f"JOIN sys.types   ty ON ty.user_type_id = c.user_type_id "
                f"WHERE s.name IN ({_in}) AND o.type IN ('U','V') "
                f"ORDER BY s.name, o.name, c.column_id"):
            _META["cols"].setdefault((sc, tb), []).append(
                {"COLUMN_NAME": col, "DATA_TYPE": dt})
        for sc, tb, col in _q("computed",
                f"SELECT LOWER(s.name), LOWER(o.name), c.name FROM sys.columns c "
                f"JOIN sys.objects o ON o.object_id=c.object_id "
                f"JOIN sys.schemas s ON s.schema_id=o.schema_id "
                f"WHERE c.is_computed=1 AND s.name IN ({_in})"):
            _META["computed"].setdefault((sc, tb), set()).add(col.upper())
        for sc, tb, col in _q("pk",
                f"SELECT LOWER(s.name), LOWER(o.name), col.name "
                f"FROM sys.indexes i "
                f"JOIN sys.index_columns ic ON ic.object_id=i.object_id "
                f"  AND ic.index_id=i.index_id "
                f"JOIN sys.columns col ON col.object_id=ic.object_id "
                f"  AND col.column_id=ic.column_id "
                f"JOIN sys.objects o ON o.object_id=i.object_id "
                f"JOIN sys.schemas s ON s.schema_id=o.schema_id "
                f"WHERE i.is_primary_key=1 AND s.name IN ({_in}) "
                f"ORDER BY s.name, o.name, ic.key_ordinal"):
            _META["pk"].setdefault((sc, tb), []).append(col)
        _META["primed"] = True
        if _sig and _cf:                   # persist for next run
            try:
                with open(_cf, "wb") as _fh:
                    pickle.dump({"sig": _sig, "obj": _META["obj"],
                                 "cols": _META["cols"],
                                 "computed": _META["computed"],
                                 "pk": _META["pk"]}, _fh)
            except Exception:
                pass
    except Exception:
        _META["primed"] = False        # any failure → fall back to live queries
    _META["prime_sub"] = _sub          # per-subquery timing for the phase log


def _meta_hit(schema):
    return _META.get("primed") and schema.lower() in _META["schemas"]


def _cols_cached(cur, schema, table):
    """fetch_columns via the primed cache when available, else live."""
    if _meta_hit(schema):
        return list(_META["cols"].get((schema.lower(), table.lower()), []))
    return fetch_columns(cur, schema, table)

# Housekeeping columns kept NOT NULL that the document never supplies — promote
# fills them with a system value when the captured row left them NULL.
_AUDIT_FILL = {
    "row_created_by":   "'PROMOTE'",
    "row_created_date": "SYSUTCDATETIME()",
    "active_ind":       "'Y'",
}


def _sel(c: str) -> str:
    """SELECT expression for a column: fill housekeeping NULLs, copy the rest.
    The data-source FK column gets the registered catalog source, not the cat
    provenance tag."""
    if c.lower() == "source":
        return f"'{_CATALOG_SOURCE}'"
    d = _AUDIT_FILL.get(c.lower())
    return f"COALESCE(m.[{c}], {d})" if d else f"m.[{c}]"


def _norm(col: str) -> str:
    """Canonical UWI expression: strip separators (dash/space/slash) and zero-pad
    to the standard 14 characters, so the dashed short form (42-317-12345-0)
    matches the 14-char key dv_well stores (42317123450000). Blank/NULL stays
    NULL — never collapses to a bogus all-zero key."""
    stripped = ("REPLACE(REPLACE(REPLACE(CONVERT(varchar(64),"
                f"{col}),'-',''),' ',''),'/','')")
    return (f"(CASE WHEN NULLIF(LTRIM(RTRIM({stripped})),'') IS NULL THEN NULL "
            f"ELSE LEFT(LTRIM(RTRIM({stripped})) + '00000000000000', 14) END)")


def object_exists(cur, schema: str, table: str) -> bool:
    if _meta_hit(schema):
        return (schema.lower(), table.lower()) in _META["obj"]
    cur.execute("SELECT OBJECT_ID(?)", f"{schema}.{table}")
    return cur.fetchone()[0] is not None


def _computed_cols(cur, dv_table: str) -> set:
    """Names of computed columns on dv_table — promote can't INSERT into these;
    dv_* derives them itself."""
    if _meta_hit(DV_SCHEMA):
        return set(_META["computed"].get((DV_SCHEMA.lower(), dv_table.lower()), set()))
    cur.execute(
        "SELECT c.name FROM sys.columns c "
        "WHERE c.object_id = OBJECT_ID(?) AND c.is_computed = 1",
        f"{DV_SCHEMA}.{dv_table}")
    return {r[0].upper() for r in cur.fetchall()}


def shared_columns(cur, dv_table: str, cat: str) -> list:
    """dv_* columns also present in the mirror, in dv order, minus provenance
    we never copy and any computed columns. INVENTORY_ID is included when dv_*
    has it.

    The data-source FK column (`source`) is ALWAYS included when the dv_* table
    has one, even if the mirror doesn't — _sel() writes the registered
    'CATALOG' literal for it (no mirror value needed), so detail tables like
    dv_prod_entity satisfy their dv_r_source FK instead of falling back to an
    unregistered column default."""
    dv_cols  = [c["COLUMN_NAME"] for c in _cols_cached(cur, DV_SCHEMA, dv_table)]
    cat_cols = {c["COLUMN_NAME"].upper()
                for c in _cols_cached(cur, CAT_SCHEMA, cat)}
    computed = _computed_cols(cur, dv_table)
    return [c for c in dv_cols
            if c.upper() not in _NEVER_COPY
            and c.upper() not in computed
            and (c.upper() in cat_cols or c.lower() == "source")]


# --------------------------------------------------------------------------- #
# Header: dv_well — create missing, fill-null update existing, then consume.
# --------------------------------------------------------------------------- #
def _reference_fk_predicates(cur, dv_table, shared, alias="m"):
    """Predicates that HOLD (don't promote) rows whose value in a dv_r_*
    reference-FK column isn't present in the reference table — instead of
    letting the INSERT abort the whole batch on a FK violation (547). NULLs
    pass, since the FK columns are nullable. Mirrors the existing dv_well
    EXISTS gate: an unresolved reference value parks the row in the mirror to be
    audited and resolved (seed the reference or map to a canonical code), never
    silently nulled and never crashing the run.

    Only columns actually being promoted (in ``shared``) are guarded, and only
    reference tables named dv_r_* — parent dv_* tables are populated during the
    same promote pass and must NOT be held. Returns (sql, [held_col, ...]) where
    sql is a string of ' AND (...)' clauses to append to the WHERE."""
    cur.execute(
        "SELECT cpa.name, rt.name, cref.name "
        "FROM sys.foreign_keys fk "
        "JOIN sys.foreign_key_columns fkc "
        "       ON fkc.constraint_object_id = fk.object_id "
        "JOIN sys.tables  pt  ON pt.object_id = fk.parent_object_id "
        "JOIN sys.schemas ps  ON ps.schema_id = pt.schema_id "
        "JOIN sys.tables  rt  ON rt.object_id = fk.referenced_object_id "
        "JOIN sys.columns cpa ON cpa.object_id = fkc.parent_object_id "
        "                    AND cpa.column_id = fkc.parent_column_id "
        "JOIN sys.columns cref ON cref.object_id = fkc.referenced_object_id "
        "                     AND cref.column_id = fkc.referenced_column_id "
        "WHERE ps.name = ? AND pt.name = ? AND rt.name LIKE 'dv[_]r[_]%'",
        DV_SCHEMA, dv_table)
    shared_lower = {s.lower() for s in shared}
    preds, cols = [], []
    for local_col, ref_table, ref_col in cur.fetchall():
        if local_col.lower() not in shared_lower:
            continue
        preds.append(
            f" AND ({alias}.[{local_col}] IS NULL OR EXISTS "
            f"(SELECT 1 FROM {DV_SCHEMA}.[{ref_table}] r "
            f"WHERE r.[{ref_col}] = {alias}.[{local_col}]))")
        cols.append(local_col)
    return "".join(preds), cols


# The discovery half, as ONE query text. catalog_status runs it through
# SQLAlchemy and promote runs it through pyodbc, so the executor differs; the
# QUESTION must not. Two spellings of "which parent FKs does this table have"
# is how the report and the gate come to disagree about the same rows — which
# is exactly what happened when the gate was added here and nowhere else.
PARENT_FK_SQL = (
    "SELECT fk.name, rt.name, cpa.name, cref.name "
    "FROM sys.foreign_keys fk "
    "JOIN sys.foreign_key_columns fkc "
    "       ON fkc.constraint_object_id = fk.object_id "
    "JOIN sys.tables  pt  ON pt.object_id = fk.parent_object_id "
    "JOIN sys.schemas ps  ON ps.schema_id = pt.schema_id "
    "JOIN sys.tables  rt  ON rt.object_id = fk.referenced_object_id "
    "JOIN sys.columns cpa ON cpa.object_id = fkc.parent_object_id "
    "                    AND cpa.column_id = fkc.parent_column_id "
    "JOIN sys.columns cref ON cref.object_id = fkc.referenced_object_id "
    "                     AND cref.column_id = fkc.referenced_column_id "
    "WHERE ps.name = {p0} AND pt.name = {p1} "
    "  AND rt.name NOT LIKE 'dv[_]r[_]%' "
    "  AND rt.name <> 'dv_well' "
    "  AND rt.object_id <> pt.object_id "
    "ORDER BY fk.name, fkc.constraint_column_id")


def parent_fk_sql(by_fk, shared, alias="m"):
    """Build the hold predicates from discovered FK metadata.

    THE ONE DEFINITION, called by promote (to gate) and by catalog_status (to
    explain). `by_fk` is {fk_name: {"ref": table, "cols": [(child, parent)...]}}.
    Returns (sql, labels, bodies) — see _parent_fk_predicates for the reasoning
    about NULL semantics and partially-shared keys.
    """
    shared_lower = {s.lower() for s in shared}
    preds, labels, bodies = [], [], []
    for _fk, d in by_fk.items():
        cols = d["cols"]
        if not all(lc.lower() in shared_lower for lc, _ in cols):
            continue
        nulls = " OR ".join(f"{alias}.[{lc}] IS NULL" for lc, _ in cols)
        join = " AND ".join(f"r.[{rc}] = {alias}.[{lc}]" for lc, rc in cols)
        body = (f"({nulls} OR EXISTS "
                f"(SELECT 1 FROM {DV_SCHEMA}.[{d['ref']}] r WHERE {join}))")
        preds.append(" AND " + body)
        bodies.append(body)
        labels.append(f"{d['ref']}({','.join(lc for lc, _ in cols)})")
    return "".join(preds), labels, bodies


def _parent_fk_predicates(cur, dv_table, shared, alias="m"):
    """Predicates that HOLD rows whose PARENT row does not exist, instead of
    letting the INSERT abort the whole mirror on a 547.

    The sibling of _reference_fk_predicates, for the other half of the FK graph.
    That one guards dv_r_* vocabulary; this one guards real parent rows —
    dv_well_log for dv_well_log_curve, dv_well_core for dv_well_core_sample —
    and, unlike it, handles COMPOUND keys. `if len(ccols) != 1: continue` was
    the documented gap, and fk_log_curve_log (uwi, log_id) is what fell through
    it: 153 curve rows whose log header had never been staged took the whole
    dv_well_log_curve promote down with a FOREIGN KEY 547, every run.

    WHY THIS IS SAFE DESPITE THE "PARENTS PROMOTE IN THE SAME PASS" RULE that
    keeps _reference_fk_predicates away from parent tables: discover_tables
    topologically sorts mirrors parents-first from the live FK edges, so by the
    time a child is promoted its parent has ALREADY had its turn. A parent row
    still missing here is one that is not coming, and holding the child is the
    honest outcome — the row stays in the mirror with a reason instead of
    failing eighteen other tables' worth of work alongside it.

    dv_well is deliberately excluded: the detail path already gates on it
    explicitly (`EXISTS dv_well WHERE UWI = _norm(m.UWI)`) with UWI-14
    normalisation this generic comparison cannot reproduce. Adding a second,
    subtly different test for the same thing is how two gates come to disagree.

    NULL SEMANTICS MATCH SQL SERVER'S. A composite FK is NOT enforced when ANY
    of its columns is NULL, so the predicate passes on any-NULL rather than
    all-NULL — a stricter test here would hold rows the database would have
    accepted.

    Returns (sql, [label, ...], [body, ...]) — the bodies are the same
    predicates unwrapped, so the caller can count what THIS gate actually held
    rather than listing every gate that might have.
    """
    cur.execute(PARENT_FK_SQL.format(p0="?", p1="?"), DV_SCHEMA, dv_table)

    by_fk: dict = {}
    for fk_name, ref_table, local_col, ref_col in cur.fetchall():
        by_fk.setdefault(fk_name, {"ref": ref_table, "cols": []})
        by_fk[fk_name]["cols"].append((local_col, ref_col))

    # Every column of the key must be one we are actually promoting; a key we
    # only half-supply is not a key we can test. See parent_fk_sql.
    return parent_fk_sql(by_fk, shared, alias)


def _fill_cat_coords_from_gold(cur, cat, lat_col, lon_col, uwi_filter, params):
    """Pre-gate coord enrichment: fill cat_well surface coords so a well whose
    location is already KNOWN promotes instead of being held by
    REQUIRE_WELL_COORDS. Fills only NULL/(0,0). Returns rows filled.

    TWO SOURCES, TRIED IN ORDER — gold first, then dv_well.

    Gold is the authority for real wells. But it only covers wells an agency
    published, and this database also holds wells loaded straight from CSV into
    dv_well — every synthetic well, and any direct load. Those match nothing in
    gold, so a document naming one produced a cat_well row with no coordinates,
    the gate held it, and the well it was held for was sitting in dv_well with a
    perfectly good latitude. MEASURED 17 Aug: 29 distinct UWIs held for "no
    coords", 26 of them already in dv_well WITH coordinates; 0 of 46 cat_well
    rows matched gold's 4,031,052.

    The gate was not protecting anything in those 26 cases. _promote_header
    inserts under NOT EXISTS and otherwise COALESCE-fills, so promoting a row
    whose well already exists cannot create an unmappable well — it can only
    fill nulls on a mapped one. Filling from dv_well is the smaller change than
    weakening the gate, and it leaves genuinely coordless NEW wells held, which
    is the behaviour that matters.

    Both joins normalise the mirror side with _norm and compare against a key
    already stored canonical (gold.uwi14, dv_well.uwi char(14)) — the same
    transform on both sides, per the padding rule that once cost six weeks of
    false FK violations.

    Returns (filled, note). Errors go into the NOTE, not into silence: this used
    to `except: return 0`, so a broken enrich looked exactly like a well gold had
    never heard of, and the well was held with a reason that was not the reason.
    The note is rendered in promote's own output line for the mirror.
    """
    filled = 0
    notes = []
    gold = "WELL_REF.well_ref.well_master_gold"
    unset = (f"(m.[{lat_col}] IS NULL OR m.[{lon_col}] IS NULL "
             f"OR (m.[{lat_col}] = 0 AND m.[{lon_col}] = 0))")

    for label, src, join in (
        ("gold", gold, f"JOIN {gold} g ON g.uwi14 = {_norm('m.UWI')}"),
        ("dv_well", f"{DV_SCHEMA}.dv_well",
         f"JOIN {DV_SCHEMA}.dv_well g ON g.uwi = {_norm('m.UWI')}"),
    ):
        try:
            cur.execute(
                f"UPDATE m SET m.[{lat_col}] = g.surface_latitude, "
                f"m.[{lon_col}] = g.surface_longitude "
                f"FROM {CAT_SCHEMA}.{cat} m "
                f"{join} "
                f"WHERE m.PROMOTED = 0{uwi_filter} "
                f"AND {unset} "
                f"AND g.surface_latitude IS NOT NULL "
                f"AND g.surface_longitude IS NOT NULL "
                f"AND NOT (g.surface_latitude = 0 AND g.surface_longitude = 0)",
                *params)
            n = cur.rowcount or 0
            filled += n
            if n:
                notes.append(f"+{n} coords from {label}")
        except Exception as e:
            notes.append(f"{label} coord-enrich FAILED "
                         f"({type(e).__name__}: {str(e)[:80]})")
    return filled, (" · " + " · ".join(notes) if notes else "")


def _promote_header(cur, dv, cat, shared, uwi_filter, params, apply):
    base = (f"m.PROMOTED = 0 "
            f"AND NULLIF(LTRIM(RTRIM(m.UWI)),'') IS NOT NULL{uwi_filter}")
    # Governance: a well with no surface coordinates can't be mapped, so HOLD it
    # (park in the mirror) rather than promote. Coords may arrive later — from a
    # document that carries a location, or a gold/UWI enrich of the mirror — at
    # which point the well promotes. Toggle with REQUIRE_WELL_COORDS.
    _lat = next((c for c in shared if c.lower() == "surface_latitude"), None)
    _lon = next((c for c in shared if c.lower() == "surface_longitude"), None)
    coord_pred = ""
    if REQUIRE_WELL_COORDS and _lat and _lon:
        coord_pred = f" AND m.[{_lat}] IS NOT NULL AND m.[{_lon}] IS NOT NULL"
    base = base + coord_pred
    # PRE-GATE: give coordless wells a location — from gold, then from dv_well —
    # so a well whose position is already known promotes instead of being held.
    # It runs BEFORE the eligibility count below, which is the whole point: the
    # count must see the coordinates this just filled.
    coord_note = ""
    if apply and REQUIRE_WELL_COORDS and _lat and _lon:
        _, coord_note = _fill_cat_coords_from_gold(cur, cat, _lat, _lon,
                                                   uwi_filter, params)
    # Hold wells whose reference-FK value (status / province / uom …) isn't in
    # the dv_r_* reference, so an unseeded code parks the well rather than
    # aborting the header insert with a 547. Held wells stay in the mirror to be
    # resolved; their detail rows are held too (the dv_well EXISTS gate).
    ref_pred, held_cols = _reference_fk_predicates(cur, dv, shared, "m")
    base_g = base + ref_pred
    held_note = coord_note

    cur.execute(f"SELECT COUNT(DISTINCT {_norm('m.UWI')}) "
                f"FROM {CAT_SCHEMA}.{cat} m WHERE {base_g}", *params)
    eligible = cur.fetchone()[0]
    if held_cols:
        cur.execute(f"SELECT COUNT(DISTINCT {_norm('m.UWI')}) "
                    f"FROM {CAT_SCHEMA}.{cat} m WHERE {base}", *params)
        held = (cur.fetchone()[0] or 0) - (eligible or 0)
        if held > 0:
            # += , not = : this assignment used to clobber anything already in
            # held_note, which now carries the coord-enrich result.
            held_note += f" · held {held} (unresolved {','.join(held_cols)})"
    if coord_pred:
        cur.execute(
            f"SELECT COUNT(DISTINCT {_norm('m.UWI')}) FROM {CAT_SCHEMA}.{cat} m "
            f"WHERE m.PROMOTED = 0 AND NULLIF(LTRIM(RTRIM(m.UWI)),'') IS NOT NULL"
            f"{uwi_filter} AND (m.[{_lat}] IS NULL OR m.[{_lon}] IS NULL)", *params)
        _ch = cur.fetchone()[0] or 0
        if _ch:
            held_note += f" · held {_ch} (no coords)"
    if not apply or not eligible:
        return (cat, eligible, 0, 0, "header" + held_note)

    collist = ", ".join(f"[{c}]" for c in shared)
    # Normalize the UWI as we stage it, so dv_well stores a clean key and every
    # comparison below is a sargable equality against the dv_well PK — no
    # REPLACE() on the dv_well side scanning all 500k+ rows.
    hdr_sel = ", ".join(
        f"{_norm('UWI')} AS [UWI]" if c.lower() == "uwi"
        else f"'{_CATALOG_SOURCE}' AS [{c}]" if c.lower() == "source"
        else f"[{c}]" for c in shared)

    # one row per normalized UWI (latest captured wins).
    # NOTE: a *parameterized* `SELECT … INTO #hdr` runs under sp_executesql,
    # whose temp tables vanish the moment it returns (error 1088), so a later
    # statement can't see #hdr. With a --uwi filter the WHERE carries a param and
    # this bites. Fix: build the empty table param-FREE (lives in the connection
    # scope), then INSERT the rows with the real, parameterized predicate — an
    # INSERT into an already-existing outer temp table is fine under sp_executesql.
    inner = ("SELECT " + hdr_sel + ", ROW_NUMBER() OVER ("
             "PARTITION BY " + _norm('UWI') + " "
             "ORDER BY CAPTURED_AT DESC, CAT_ROW_ID DESC) AS _rn "
             "FROM " + f"{CAT_SCHEMA}.{cat}" + " m WHERE {where}")
    base_g_nofilter = ("m.PROMOTED = 0 "
                       "AND NULLIF(LTRIM(RTRIM(m.UWI)),'') IS NOT NULL") + ref_pred
    cur.execute("IF OBJECT_ID('tempdb..#hdr') IS NOT NULL DROP TABLE #hdr")
    cur.execute(
        f"SELECT {collist} INTO #hdr FROM ("
        + inner.format(where=base_g_nofilter) + ") q WHERE 1=0")   # param-free
    cur.execute("CREATE INDEX IX_hdr_uwi ON #hdr(UWI)")
    cur.execute(
        f"INSERT INTO #hdr ({collist}) SELECT {collist} FROM ("
        + inner.format(where=base_g) + ") q WHERE q._rn = 1", *params)

    # create headers that don't exist yet (#hdr.UWI and dv_well.UWI both clean)
    cur.execute(
        f"INSERT INTO {DV_SCHEMA}.{dv} ({collist}) "
        f"SELECT {collist} FROM #hdr h "
        f"WHERE NOT EXISTS (SELECT 1 FROM {DV_SCHEMA}.{dv} w "
        f"                  WHERE w.UWI = h.UWI)")
    inserted = cur.rowcount or 0

    # fill-null update for headers that already exist (never clobber)
    setcols = [c for c in shared if c.upper() != "UWI"]
    updated = 0
    if setcols:
        sets = ", ".join(f"w.[{c}] = COALESCE(w.[{c}], h.[{c}])" for c in setcols)
        cur.execute(
            f"UPDATE w SET {sets} "
            f"FROM {DV_SCHEMA}.{dv} w "
            f"JOIN #hdr h ON w.UWI = h.UWI")
        updated = cur.rowcount or 0

    # MOVE: consume the cat_well rows we just promoted
    cur.execute(f"DELETE m FROM {CAT_SCHEMA}.{cat} m WHERE {base_g}", *params)
    cur.execute("IF OBJECT_ID('tempdb..#hdr') IS NOT NULL DROP TABLE #hdr")
    return (cat, eligible, inserted, updated, "header +new/~fill" + held_note)


def _referencing_children(cur, dv_table: str) -> list:
    """dv_* tables whose FK points AT dv_table (immediate children), excluding
    self-references."""
    cur.execute(
        "SELECT DISTINCT OBJECT_NAME(fk.parent_object_id) "
        "FROM sys.foreign_keys fk "
        "WHERE fk.referenced_object_id = OBJECT_ID(?) "
        "AND fk.parent_object_id <> fk.referenced_object_id",
        f"{DV_SCHEMA}.{dv_table}")
    return [r[0] for r in cur.fetchall() if r[0]]


def _has_inventory(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM sys.columns "
        "WHERE object_id = OBJECT_ID(?) AND name = 'INVENTORY_ID'",
        f"{DV_SCHEMA}.{table}")
    return cur.fetchone() is not None


def _purge_children_by_inventory(cur, dv_table, inv_sql, params, _seen=None):
    """Depth-first delete of every descendant row tied to the files in inv_sql,
    so replacing a parent (dv_well_core) never trips a child FK
    (dv_well_core_sample). Scoped strictly to the files' INVENTORY_IDs — only
    the rows we're about to re-promote are removed. Children without an
    INVENTORY_ID column are left to the table's own insert-only path."""
    _seen = set() if _seen is None else _seen
    for child in _referencing_children(cur, dv_table):
        if child.lower() in _seen:
            continue
        _seen.add(child.lower())
        _purge_children_by_inventory(cur, child, inv_sql, params, _seen)
        if _has_inventory(cur, child):
            cur.execute(
                f"DELETE FROM {DV_SCHEMA}.{child} "
                f"WHERE INVENTORY_ID IN ({inv_sql})", *params)


# --------------------------------------------------------------------------- #
# Detail: per-file replace (if INVENTORY_ID present) then move.
# --------------------------------------------------------------------------- #
def _pk_columns(cur, schema, table):
    """PRIMARY KEY column names of schema.table, in key order (empty if none)."""
    if _meta_hit(schema):
        return list(_META["pk"].get((schema.lower(), table.lower()), []))
    cur.execute(
        "SELECT col.name FROM sys.indexes i "
        "JOIN sys.index_columns ic ON ic.object_id=i.object_id "
        "  AND ic.index_id=i.index_id "
        "JOIN sys.columns col ON col.object_id=ic.object_id "
        "  AND col.column_id=ic.column_id "
        "JOIN sys.objects o ON o.object_id=i.object_id "
        "JOIN sys.schemas s ON s.schema_id=o.schema_id "
        "WHERE i.is_primary_key=1 AND o.name=? AND s.name=? "
        "ORDER BY ic.key_ordinal", table, schema)
    return [r[0] for r in cur.fetchall()]


def _promote_detail(cur, dv, cat, shared, uwi_filter, params, apply):
    has_inv = any(s.upper() == "INVENTORY_ID" for s in shared)
    base_where = (f"m.PROMOTED = 0 "
                  f"AND EXISTS (SELECT 1 FROM {DV_SCHEMA}.dv_well w "
                  f"            WHERE w.UWI = {_norm('m.UWI')})"
                  f"{uwi_filter}")
    # Hold rows whose reference-FK value (e.g. rate_ouom -> dv_r_uom) isn't in
    # the reference, so an unseeded code parks the row instead of aborting the
    # batch with a 547 FK violation.
    ref_pred, held_cols = _reference_fk_predicates(cur, dv, shared, "m")
    # ...and hold rows whose PARENT ROW is missing, for the same reason and by
    # the same mechanism. Without this a single orphan takes the whole mirror
    # down with a 547 and every other row in it stays put — 153 log curves did
    # exactly that. See _parent_fk_predicates for why this is safe to evaluate
    # here (discover_tables promotes parents first).
    par_pred, par_labels, par_bodies = _parent_fk_predicates(cur, dv, shared, "m")
    where = base_where + ref_pred + par_pred

    cur.execute(f"SELECT COUNT(*) FROM {CAT_SCHEMA}.{cat} m WHERE {where}",
                *params)
    eligible = cur.fetchone()[0]
    note = "ok" if has_inv else "insert-only (no INVENTORY_ID — run migration)"
    if held_cols or par_labels:
        cur.execute(f"SELECT COUNT(*) FROM {CAT_SCHEMA}.{cat} m "
                    f"WHERE {base_where}", *params)
        held = (cur.fetchone()[0] or 0) - (eligible or 0)
        if held > 0:
            # NAME THE GATE THAT ACTUALLY FIRED. Listing every label that
            # could be responsible sends the reader after three vocabulary
            # codes when the real answer is a missing parent row — the same
            # "say WHICH reason" problem as the well-header hold. The parent
            # gate is counted on its own; the reference labels keep their
            # existing (looser) reporting rather than being changed here.
            _par_held = 0
            if par_bodies:
                _neg = " OR ".join(f"NOT {b}" for b in par_bodies)
                cur.execute(f"SELECT COUNT(*) FROM {CAT_SCHEMA}.{cat} m "
                            f"WHERE {base_where} AND ({_neg})", *params)
                _par_held = cur.fetchone()[0] or 0
            _bits = []
            if held_cols:
                _bits.append("unresolved " + ",".join(held_cols))
            if _par_held:
                _bits.append(f"{_par_held} waiting on a parent row that does "
                             f"not exist: {','.join(par_labels)}")
            note += f" · held {held}" + (
                f" ({'; '.join(_bits)})" if _bits else "")
    if not apply or not eligible:
        return (cat, eligible, 0, 0, note)

    replaced = 0
    if has_inv:
        inv_sql = (f"SELECT DISTINCT m.INVENTORY_ID FROM {CAT_SCHEMA}.{cat} m "
                   f"WHERE {where} AND m.INVENTORY_ID IS NOT NULL")
        # delete dependent child rows for these files FIRST, then this table —
        # so a parent replace (dv_well_core) doesn't conflict with a child FK
        # (dv_well_core_sample) still pointing at the old rows.
        _purge_children_by_inventory(cur, dv, inv_sql, params)
        cur.execute(
            f"DELETE d FROM {DV_SCHEMA}.{dv} d "
            f"WHERE d.INVENTORY_ID IN ({inv_sql})", *params)
        replaced = cur.rowcount or 0

    cols = ", ".join(f"[{c}]" for c in shared)
    has_uwi = any(c.lower() == "uwi" for c in shared)

    # Idempotency guard: skip rows whose target PRIMARY KEY already exists.
    # replace-by-INVENTORY_ID above only clears rows for THIS batch's files, so a
    # child row previously promoted under a different INVENTORY_ID — or a table
    # with no INVENTORY_ID at all (insert-only) — would otherwise duplicate its PK
    # and abort the whole promote with a 23000 (pk_dv_well_*). Comparing against
    # the exact values we insert makes a re-promote a no-op instead of a crash.
    _pk = [c for c in _pk_columns(cur, DV_SCHEMA, dv)
           if any(s.lower() == c.lower() for s in shared)]

    def _pkval(c):
        return f"w.[{c}]" if (has_uwi and c.lower() == "uwi") else _sel(c)
    anti = ""
    if _pk:
        _conds = " AND ".join(f"d2.[{c}] = {_pkval(c)}" for c in _pk)
        anti = (f" AND NOT EXISTS (SELECT 1 FROM {DV_SCHEMA}.{dv} d2 "
                f"WHERE {_conds})")

    # Row source: join to dv_well only when there's a uwi to map to the canonical key.
    inner_cols = ", ".join(
        (f"w.[{c}] AS [{c}]" if (has_uwi and c.lower() == "uwi")
         else f"{_sel(c)} AS [{c}]")
        for c in shared)
    join = (f"JOIN {DV_SCHEMA}.dv_well w ON w.UWI = {_norm('m.UWI')} "
            if has_uwi else "")

    # Two-part idempotency: NOT EXISTS (anti) drops rows already in dv; ROW_NUMBER
    # dedup drops rows that collide with EACH OTHER in this same batch (e.g. two
    # LAS files reusing log_id for one well). Together = 23000-proof insert.
    if _pk:
        part = ", ".join(_pkval(c) for c in _pk)
        cur.execute(
            f"INSERT INTO {DV_SCHEMA}.{dv} ({cols}) "
            f"SELECT {cols} FROM ("
            f"SELECT {inner_cols}, ROW_NUMBER() OVER (PARTITION BY {part} "
            f"ORDER BY (SELECT 1)) AS _rn "
            f"FROM {CAT_SCHEMA}.{cat} m {join}WHERE {where}{anti}"
            f") q WHERE q._rn = 1", *params)
    else:
        cur.execute(
            f"INSERT INTO {DV_SCHEMA}.{dv} ({cols}) "
            f"SELECT {inner_cols} FROM {CAT_SCHEMA}.{cat} m {join}"
            f"WHERE {where}{anti}", *params)
    moved = cur.rowcount or 0

    # MOVE: remove the promoted rows from the mirror
    cur.execute(f"DELETE m FROM {CAT_SCHEMA}.{cat} m WHERE {where}", *params)
    return (cat, eligible, moved, replaced, note)


def promote_table(cur, dv_table: str, uwi, apply: bool) -> tuple:
    """Returns (cat, eligible, moved, replaced_or_updated, note)."""
    cat = cat_name(dv_table)
    if not object_exists(cur, CAT_SCHEMA, cat):
        return (cat, None, None, None, "no mirror")
    if not object_exists(cur, DV_SCHEMA, dv_table):
        return (cat, None, None, None, "no dv target")

    # Fast path: if the mirror has no unpromoted rows, skip all the schema
    # reflection (shared_columns / reference-FK predicates / computed cols) and
    # the eligibility COUNTs — those are ~5 round-trips per table, and most
    # mirrors are empty on any given run. One cheap COUNT short-circuits them.
    _uwi_pred = ""
    _uwi_par = []
    if uwi:
        _uwi_pred = f" AND {_norm('m.UWI')} = {_norm('?')}"
        _uwi_par.append(uwi)
    cur.execute(
        f"SELECT TOP 1 1 FROM {CAT_SCHEMA}.{cat} m "
        f"WHERE m.PROMOTED = 0{_uwi_pred}", *_uwi_par)
    if cur.fetchone() is None:
        return (cat, 0, 0, 0, "ok")

    shared = shared_columns(cur, dv_table, cat)
    if not shared:
        return (cat, None, None, None, "no shared columns")

    uwi_filter, params = "", []
    if uwi:
        uwi_filter = f" AND {_norm('m.UWI')} = {_norm('?')}"
        params.append(uwi)

    if dv_table.lower() == "dv_well":
        return _promote_header(cur, dv_table, cat, shared, uwi_filter,
                               params, apply)
    return _promote_detail(cur, dv_table, cat, shared, uwi_filter,
                           params, apply)


def _ensure_catalog_source(cur):
    """Idempotently register the 'CATALOG' data-source in dv_r_source so promoted
    rows satisfy the source FK. Mirrors entity_seeder's dv_r_source columns."""
    cur.execute(
        "IF NOT EXISTS (SELECT 1 FROM dataview.dv_r_source WHERE source = ?) "
        "INSERT INTO dataview.dv_r_source "
        "  (source, short_name, long_name, active_ind, "
        "   row_created_by, row_created_date, row_changed_by, row_changed_date) "
        "VALUES (?, ?, ?, 'Y', 'PROMOTE', GETDATE(), 'PROMOTE', GETDATE())",
        _CATALOG_SOURCE, _CATALOG_SOURCE, _CATALOG_SOURCE,
        "Promoted from file catalog")


def _promote_spatial(cur, apply, cat_table, dv_table, name_col, cols, note):
    """Generic per-feature spatial promote: cat_<x> -> dv_<x>, one row per
    feature, geog built from that feature's SPATIAL_OUTLINE WKT (with the
    half-Earth reorientation backstop for polygons; lines are unaffected).
    `cols` maps dv_column -> cat_column for the attributes to carry across.
    Keyed on name_col. Returns the promote_table-shaped tuple."""
    if not object_exists(cur, DV_SCHEMA, dv_table):
        return (dv_table, None, None, None, "no dv target")
    if not object_exists(cur, "file_catalog", cat_table):
        return (dv_table, None, None, None, f"no {cat_table}")

    cur.execute(f"""
        SELECT COUNT(*) FROM file_catalog.{cat_table}
        WHERE PROMOTED = 0 AND NULLIF(LTRIM(RTRIM({name_col})),'') IS NOT NULL
    """)
    eligible = cur.fetchone()[0]
    if not apply or not eligible:
        return (dv_table, eligible, 0, 0, note)

    # build the attribute select/set/insert fragments from the cols map
    src_selects = [f"MAX({cat}) AS {dv}" for dv, cat in cols.items()]
    set_frag    = ", ".join(f"{dv} = src.{dv}" for dv in cols)
    ins_cols    = ", ".join(cols.keys())
    ins_vals    = ", ".join(f"src.{dv}" for dv in cols)
    dv_name     = list(cols.keys())[0]   # first mapped col is the name

    geog_expr = """
        CASE
          WHEN src.wkt IS NULL THEN NULL
          WHEN geography::STGeomFromText(src.wkt,4326).MakeValid().STIsValid() = 1
            THEN CASE WHEN geography::STGeomFromText(src.wkt,4326).MakeValid().STArea()/1000000.0 > 255000000
                      THEN geography::STGeomFromText(src.wkt,4326).MakeValid().ReorientObject()
                      ELSE geography::STGeomFromText(src.wkt,4326).MakeValid()
                 END
          ELSE NULL
        END
    """
    # For UPDATE, keep existing geog when the incoming wkt is null/invalid:
    geog_update_expr = f"""
        CASE
          WHEN src.wkt IS NULL THEN tgt.geog
          WHEN geography::STGeomFromText(src.wkt,4326).MakeValid().STIsValid() = 1
            THEN CASE WHEN geography::STGeomFromText(src.wkt,4326).MakeValid().STArea()/1000000.0 > 255000000
                      THEN geography::STGeomFromText(src.wkt,4326).MakeValid().ReorientObject()
                      ELSE geography::STGeomFromText(src.wkt,4326).MakeValid()
                 END
          ELSE tgt.geog
        END
    """

    cur.execute(f"""
        MERGE dataview.{dv_table} AS tgt
        USING (
            SELECT {name_col} AS {dv_name},
                   {", ".join(src_selects[1:]) + "," if len(src_selects) > 1 else ""}
                   MAX(SPATIAL_OUTLINE) AS wkt
            FROM file_catalog.{cat_table}
            WHERE PROMOTED = 0 AND NULLIF(LTRIM(RTRIM({name_col})),'') IS NOT NULL
            GROUP BY {name_col}
        ) src ON tgt.{dv_name} = src.{dv_name}
        WHEN MATCHED THEN UPDATE SET
            {set_frag}, source = 'CATALOG',
            geog = {geog_update_expr},
            row_changed_by = 'PROMOTE', row_changed_date = GETUTCDATE()
        WHEN NOT MATCHED THEN INSERT (
            {dv_table.replace('dv_','')}_id, {ins_cols}, active_ind, source, geog,
            row_created_by, row_created_date, row_changed_by, row_changed_date
        ) VALUES (
            CONVERT(VARCHAR(40), NEWID()), {ins_vals}, 'Y', 'CATALOG',
            {geog_expr},
            'PROMOTE', GETUTCDATE(), 'PROMOTE', GETUTCDATE()
        );
    """)
    merged = cur.rowcount or 0
    cur.execute(f"""
        UPDATE file_catalog.{cat_table} SET PROMOTED = 1, PROMOTED_AT = GETUTCDATE()
        WHERE PROMOTED = 0 AND NULLIF(LTRIM(RTRIM({name_col})),'') IS NOT NULL
    """)
    return (dv_table, eligible, merged, 0, note)


def promote_land_tract(cur, apply, log):
    return _promote_spatial(cur, apply, "cat_land_tract", "dv_land_tract",
        "TRACT_NAME",
        {"tract_name":"TRACT_NAME", "lease_number":"LEASE_NUMBER",
         "operator_name":"OPERATOR_NAME", "province_state":"PROVINCE_STATE",
         "country":"COUNTRY"},
        "lease polygon")


def promote_boundary(cur, apply, log):
    return _promote_spatial(cur, apply, "cat_boundary", "dv_boundary",
        "BOUNDARY_NAME",
        {"boundary_name":"BOUNDARY_NAME", "boundary_type":"BOUNDARY_TYPE",
         "province_state":"PROVINCE_STATE", "country":"COUNTRY"},
        "boundary polygon")


def promote_pipeline(cur, apply, log):
    return _promote_spatial(cur, apply, "cat_pipeline", "dv_pipeline",
        "PIPELINE_NAME",
        {"pipeline_name":"PIPELINE_NAME", "operator_name":"OPERATOR_NAME",
         "commodity":"COMMODITY", "province_state":"PROVINCE_STATE",
         "country":"COUNTRY"},
        "pipeline line")


def promote_well_geog(cur, apply, log):
    """Build POINT geography on dataview.dv_well from surface_latitude/longitude.
    Wells are points (no ring winding, no reorientation) — a straight set-based
    UPDATE. Only fills rows with valid lat/lon degrees and a NULL geog, so it's
    cheap to re-run and won't clobber a manually-set point. Requires dv_well to
    have a geog GEOGRAPHY column (added via ALTER; skipped cleanly if absent)."""
    if not object_exists(cur, DV_SCHEMA, "dv_well"):
        return ("dv_well.geog", None, None, None, "no dv_well")
    # geog column present?
    cur.execute("""
        SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA='dataview' AND TABLE_NAME='dv_well'
          AND COLUMN_NAME='geog' AND DATA_TYPE='geography'
    """)
    if not cur.fetchone()[0]:
        return ("dv_well.geog", None, None, None, "no geog column")

    cur.execute("""
        SELECT COUNT(*) FROM dataview.dv_well
        WHERE geog IS NULL
          AND surface_latitude  IS NOT NULL AND surface_longitude IS NOT NULL
          AND surface_latitude  BETWEEN -90  AND 90
          AND surface_longitude BETWEEN -180 AND 180
    """)
    eligible = cur.fetchone()[0]
    if not apply or not eligible:
        return ("dv_well.geog", eligible, 0, 0, "well point")

    # geography::Point(lat, lon, srid) — note lat first. TRY_CONVERT guards any
    # stray non-numeric coordinate so one bad row can't fail the whole batch.
    cur.execute("""
        UPDATE dataview.dv_well
        SET geog = geography::Point(
                       TRY_CONVERT(float, surface_latitude),
                       TRY_CONVERT(float, surface_longitude), 4326)
        WHERE geog IS NULL
          AND surface_latitude  IS NOT NULL AND surface_longitude IS NOT NULL
          AND surface_latitude  BETWEEN -90  AND 90
          AND surface_longitude BETWEEN -180 AND 180
    """)
    updated = cur.rowcount or 0
    return ("dv_well.geog", eligible, updated, 0, "well point")


def promote_field(cur, apply, log):
    """Promote per-feature FIELD rows from file_catalog.cat_field up into
    dataview.dv_field — one dv_field row per captured field, with its geog
    GEOGRAPHY built from that feature's SPATIAL_OUTLINE WKT. Keyed on field_name.
    Attributes (operator, fluid type, state, area) carry through where present."""
    if not object_exists(cur, DV_SCHEMA, "dv_field"):
        return ("dv_field", None, None, None, "no dv target")
    if not object_exists(cur, "file_catalog", "cat_field"):
        return ("dv_field", None, None, None, "no cat_field")

    cur.execute("""
        SELECT COUNT(*) FROM file_catalog.cat_field
        WHERE PROMOTED = 0 AND NULLIF(LTRIM(RTRIM(FIELD_NAME)),'') IS NOT NULL
    """)
    eligible = cur.fetchone()[0]
    if not apply or not eligible:
        return ("dv_field", eligible, 0, 0, "field polygon")

    # MERGE cat_field -> dv_field, one row per field, geog from its own WKT.
    cur.execute("""
        MERGE dataview.dv_field AS tgt
        USING (
            SELECT FIELD_NAME AS field_name,
                   MAX(FLUID_TYPE)      AS fluid_type,
                   MAX(COUNTRY)         AS country,
                   MAX(PROVINCE_STATE)  AS province_state,
                   MAX(OPERATOR_NAME)   AS operator_name,
                   MAX(SPATIAL_OUTLINE) AS wkt
            FROM file_catalog.cat_field
            WHERE PROMOTED = 0
              AND NULLIF(LTRIM(RTRIM(FIELD_NAME)),'') IS NOT NULL
            GROUP BY FIELD_NAME
        ) src ON tgt.field_name = src.field_name
        WHEN MATCHED THEN UPDATE SET
            field_type = src.fluid_type, country = src.country,
            province_state = src.province_state, source = 'CATALOG',
            geog = CASE WHEN src.wkt IS NULL THEN tgt.geog ELSE (
                       CASE WHEN geography::STGeomFromText(src.wkt,4326).MakeValid()
                                 .STArea()/1000000.0 > 255000000
                            THEN geography::STGeomFromText(src.wkt,4326).MakeValid().ReorientObject()
                            ELSE geography::STGeomFromText(src.wkt,4326).MakeValid()
                       END) END,
            row_changed_by = 'PROMOTE', row_changed_date = GETUTCDATE()
        WHEN NOT MATCHED THEN INSERT (
            field_id, field_name, field_type, country, province_state,
            active_ind, source, geog,
            row_created_by, row_created_date, row_changed_by, row_changed_date
        ) VALUES (
            CONVERT(VARCHAR(40), NEWID()), src.field_name, src.fluid_type,
            src.country, src.province_state, 'Y', 'CATALOG',
            CASE WHEN src.wkt IS NULL THEN NULL ELSE (
                CASE WHEN geography::STGeomFromText(src.wkt,4326).MakeValid()
                          .STArea()/1000000.0 > 255000000
                     THEN geography::STGeomFromText(src.wkt,4326).MakeValid().ReorientObject()
                     ELSE geography::STGeomFromText(src.wkt,4326).MakeValid()
                END) END,
            'PROMOTE', GETUTCDATE(), 'PROMOTE', GETUTCDATE()
        );
    """)
    merged = cur.rowcount or 0

    # mark the promoted cat_field rows so re-runs don't re-promote
    cur.execute("""
        UPDATE file_catalog.cat_field SET PROMOTED = 1, PROMOTED_AT = GETUTCDATE()
        WHERE PROMOTED = 0 AND NULLIF(LTRIM(RTRIM(FIELD_NAME)),'') IS NOT NULL
    """)
    return ("dv_field", eligible, merged, 0, "field polygon")


def promote_seismic(cur, apply, log):
    """Promote seismic survey identity from file_catalog.FILE_SEIS_HEADER up
    into dataview.dv_seis_set (one row per survey) and dv_seis_line (one row
    per file, now WITH its geometry). Covers every seismic source the extract
    stage writes there (SEG-Y, P190, shapefiles, OSDU survey JSON).

    THE MAPPABLE GATE (Perry, July 22: "If they don't have the required info
    to be visualized on a map they should not be promoted"): a file is
    mappable when it carries a usable SURVEY_OUTLINE **or** a complete BBOX_*
    set; a survey is mappable when ANY of its files is. Held files/surveys
    are REPORTED by name, never silently dropped — their FILE_SEIS_HEADER
    rows survive untouched, so arming a CRS and re-extracting promotes them
    on the next run.

    GEOMETRY: extract now writes WGS84 WKT into SURVEY_OUTLINE per file —
    trace-order LINESTRINGs for 2D, stated-corner POLYGONs for 3D — and this
    function converts them with geography::STGeomFromText(..., 4326) into
    dv_seis_line.geog (column checked at runtime via INFORMATION_SCHEMA, not
    assumed from a DDL snapshot). The GeoJSON exporter becomes an export, not
    the bridge.

    Returns (target, eligible, merged, held, note) like promote_table."""
    if not object_exists(cur, DV_SCHEMA, "dv_seis_set"):
        return ("dv_seis_set", None, None, None, "no dv target")

    # A REJECTED FILE MUST NOT PROMOTE. Marking a file bad removes its cat_*
    # rows and sets CATALOG_READINESS='SKIPPED', and for documents that is the
    # whole story — the mirrors are where their data lived. Seismic never
    # stages in cat_*, so the cascade has nothing to delete and promote lifted
    # a rejected file's survey straight back into dv_seis_set on the next run.
    # Deleting the dv_ rows by hand does not help: the next promote rebuilds
    # them, because FILE_SEIS_HEADER still carries a name and an outline and
    # both gates pass.
    #
    # Found 23 Aug rejecting Teapot's filt_mig.sgy, whose trace data is
    # unreadable. Its header and stated corners are fine, so nothing about the
    # row LOOKS wrong — which is exactly why the blocklist has to be honoured
    # here rather than left to the operator to remember.
    _NOT_REJECTED = "1=1"
    if object_exists(cur, "file_catalog", "GLOBAL_FILE_CATALOG"):
        _NOT_REJECTED = (
            "NOT EXISTS (SELECT 1 FROM file_catalog.GLOBAL_FILE_CATALOG gg "
            "WHERE gg.INVENTORY_ID = s.INVENTORY_ID "
            "AND ISNULL(gg.CATALOG_READINESS,'') = 'SKIPPED')")
    if object_exists(cur, "file_catalog", "BAD_FILE"):
        _NOT_REJECTED += (
            " AND NOT EXISTS (SELECT 1 FROM file_catalog.BAD_FILE bf "
            "WHERE bf.INVENTORY_ID = s.INVENTORY_ID)")

    _NAMED = ("NULLIF(LTRIM(RTRIM(s.SURVEY_NAME)), '') IS NOT NULL "
              f"AND ({_NOT_REJECTED})")
    _MAPPABLE = (
        "(NULLIF(LTRIM(RTRIM(s.SURVEY_OUTLINE)), '') IS NOT NULL "
        "OR (s.BBOX_MIN_LAT IS NOT NULL AND s.BBOX_MAX_LAT IS NOT NULL "
        "AND s.BBOX_MIN_LON IS NOT NULL AND s.BBOX_MAX_LON IS NOT NULL))")
    # Normalized survey-name key — the SAME key the MERGE groups on, so the
    # gate and the promote agree on what "a survey" is.
    _norm = ("UPPER(LTRIM(RTRIM("
             "REPLACE(REPLACE(REPLACE(s.SURVEY_NAME,CHAR(9),' '),CHAR(13),' '),CHAR(10),' ')"
             ")))")

    cur.execute(
        "SELECT COUNT(DISTINCT SURVEY_NAME) FROM file_catalog.FILE_SEIS_HEADER s "
        f"WHERE {_NAMED}")
    eligible = cur.fetchone()[0]

    # Surveys the gate holds — reported in DRY RUN too, so a run that would
    # promote nothing explains itself before anyone presses apply.
    cur.execute(f"""
        SELECT MAX(s.SURVEY_NAME)
        FROM file_catalog.FILE_SEIS_HEADER s
        WHERE {_NAMED}
        GROUP BY {_norm}
        HAVING MAX(CASE WHEN {_MAPPABLE} THEN 1 ELSE 0 END) = 0""")
    _held_surveys = sorted(r[0] for r in cur.fetchall())
    if _held_surveys:
        log(f"  seismic gate: {len(_held_surveys)} survey(s) HELD — no outline "
            f"and no bbox, nothing to draw. Find/arm the CRS and re-extract, "
            f"then re-run:")
        for _hs in _held_surveys:
            log(f"      - {_hs}")

    # HELD FOR HAVING NO NAME AT ALL — and invisible until now, because the
    # query above filters on _NAMED. A file whose SURVEY_NAME is NULL fails
    # that predicate, so it fell out of BOTH `eligible` and `held` and was
    # reported as neither promoted nor held. That is precisely the collapse
    # this module already warns about at the top ("stepped past — reported as
    # neither moved nor held"), and CLAUDE.md's four states exist to stop it:
    # Held is a file blocked by a NAMED gate, and it must say which.
    #
    # Observed 23 Aug after a reload: five Teapot 2D lines held here while
    # promote logged "1 eligible, 0 held" and Perry reasonably read that as
    # the other five having vanished. They had not — their SEG-Y rev-0 card
    # image names no survey (the extractor refuses "AREA MAP ID", which is the
    # blank form's printed labels), so they wait for a human to name them.
    #
    # A REJECTED file is NOT held: it is deliberately out of scope, which is
    # why this reuses _NOT_REJECTED rather than simply negating _NAMED.
    _gfc = object_exists(cur, "file_catalog", "GLOBAL_FILE_CATALOG")
    _sel = "RIGHT(g.FILE_PATH, 52)" if _gfc else "CAST(s.INVENTORY_ID AS varchar(40))"
    _join = ("LEFT JOIN file_catalog.GLOBAL_FILE_CATALOG g "
             "ON g.INVENTORY_ID = s.INVENTORY_ID") if _gfc else ""
    cur.execute(f"""
        SELECT {_sel}
        FROM file_catalog.FILE_SEIS_HEADER s
        {_join}
        WHERE NULLIF(LTRIM(RTRIM(s.SURVEY_NAME)), '') IS NULL
          AND ({_NOT_REJECTED})""")
    _held_unnamed = sorted(str(r[0]) for r in cur.fetchall() if r[0] is not None)
    if _held_unnamed:
        log(f"  seismic gate: {len(_held_unnamed)} file(s) HELD — no survey "
            f"name. The header named no survey, or named only card-image "
            f"labels, which are refused rather than promoted as a survey. "
            f"Assign a survey name in Browse & View, then re-run:")
        for _hu in _held_unnamed[:10]:
            log(f"      - {_hu}")
        if len(_held_unnamed) > 10:
            log(f"      … and {len(_held_unnamed) - 10} more")

    # SURVEYS and FILES are different units and the note says so rather than
    # quietly adding them: an unnamed file has no survey to be counted as.
    held = len(_held_surveys) + len(_held_unnamed)
    # _gate_note, not _note: this function reuses _note further down for the
    # dv_seis_line log, and a name collision here would make the returned note
    # depend on how far execution got.
    _gate_note = "seismic survey"
    if _held_surveys or _held_unnamed:
        _gate_note += (" · held " + " + ".join(filter(None, [
            f"{len(_held_surveys)} unmappable survey(s)" if _held_surveys else "",
            f"{len(_held_unnamed)} unnamed file(s)" if _held_unnamed else ""])))

    if not apply or not eligible:
        return ("dv_seis_set", eligible, 0, held, _gate_note)

    # Pre-pass: some WKT (e.g. projected SEG-Y coords like 11770231, way outside
    # ±180 lon) throws at geography CONSTRUCTION — before MakeValid/STIsValid can
    # run — so the in-MERGE gate can't catch it. Test each survey's outline here
    # in Python and NULL the un-constructable ones so the MERGE never sees them.
    # Those surveys still promote (identity + bbox); they just get NULL geog.
    cur.execute("IF OBJECT_ID('tempdb..#badseis') IS NOT NULL DROP TABLE #badseis")
    cur.execute("CREATE TABLE #badseis (sn NVARCHAR(400) PRIMARY KEY)")
    try:
        cur.execute(f"""
            SELECT DISTINCT SURVEY_NAME, SURVEY_OUTLINE
            FROM file_catalog.FILE_SEIS_HEADER s
            WHERE SURVEY_OUTLINE IS NOT NULL AND {_NAMED}""")
        _rows = cur.fetchall()
        _bad = set()
        for _sn, _wkt in _rows:
            try:
                cur.execute(
                    "SELECT geography::STGeomFromText(?,4326).MakeValid().STIsValid()",
                    _wkt)
                if cur.fetchone()[0] != 1:
                    _bad.add(_sn)
            except Exception:
                _bad.add(_sn)             # threw at construction — projected/bad
        if _bad:
            # Skip geom for these surveys WITHOUT destroying their source outline:
            # stage the names, then NULL the wkt only inside the MERGE (below), so
            # the geography is never constructed for them AND the raw outline in
            # FILE_SEIS_HEADER survives for a later CRS-aware reprojection.
            cur.executemany("INSERT INTO #badseis (sn) VALUES (?)",
                            [(b,) for b in sorted(_bad)])
            log(f"  seismic geom skipped: {len(_bad)} survey(s) with invalid "
                f"geometry (identity still promoted, outline preserved):")
            for _b in sorted(_bad):
                log(f"      - {_b}")
    except Exception as _pp:
        log(f"  seismic geom pre-pass warning: {str(_pp).splitlines()[0][:80]}")

    # One row per SURVEY (Model A). Group by a NORMALIZED name so the same survey
    # spread across multiple volume files (PSTM/PSDM/stacks/vintages) collapses to
    # ONE dv_seis_set row rather than one-per-file. Normalization is conservative:
    # trim, collapse internal whitespace to single spaces, uppercase. Distinct
    # surveys stay distinct; only near-identical names merge. The display name is
    # the MAX() actual name in each group.
    #
    # NOTE on the set-level wkt now that extract writes per-file LINESTRINGs:
    # MAX(SURVEY_OUTLINE) picks ONE file's geometry as the survey's. For 3D
    # (stated-corner POLYGON, identical across a survey's volumes) that is the
    # right outline; for 2D it is one arbitrary line — acceptable, because the
    # per-line geometry now lives on dv_seis_line.geog and that is what the
    # map draws. The set row's authority is its bbox.
    cur.execute(f"""
        MERGE dataview.dv_seis_set AS tgt
        USING (
            SELECT MAX(s.SURVEY_NAME) AS sn,
                   {_norm}            AS nkey,
                   MAX(s.SEIS_SET_TYPE) AS stype,
                   MAX(g.FILE_PATH)     AS file_path,
                   MAX(s.INVENTORY_ID)  AS catalog_id,
                   MIN(s.BBOX_MIN_LAT)  AS bmin_lat, MAX(s.BBOX_MAX_LAT) AS bmax_lat,
                   MIN(s.BBOX_MIN_LON)  AS bmin_lon, MAX(s.BBOX_MAX_LON) AS bmax_lon,
                   MAX(s.EPSG_CODE)     AS epsg,
                   MAX(s.CONTRACTOR)    AS remark,
                   -- SET-level geog is 3D corner POLYGONs ONLY. 2D
                   -- outlines are per-file LINESTRINGs now, and a LINESTRING
                   -- in the survey-FOOTPRINT column broke the map's
                   -- geography-layer block (July 28) — the lines belong on
                   -- dv_seis_line.geog. 2D sets keep bbox authority, geog
                   -- NULL.
                   MAX(CASE WHEN bs.sn IS NOT NULL THEN NULL
                            WHEN LEFT(LTRIM(s.SURVEY_OUTLINE), 7) = 'POLYGON'
                            THEN s.SURVEY_OUTLINE END) AS wkt
            FROM file_catalog.FILE_SEIS_HEADER s
            LEFT JOIN file_catalog.GLOBAL_FILE_CATALOG g
                   ON g.INVENTORY_ID = s.INVENTORY_ID
            LEFT JOIN #badseis bs ON bs.sn = s.SURVEY_NAME
            WHERE {_NAMED}
            GROUP BY {_norm}
            HAVING MAX(CASE WHEN {_MAPPABLE} THEN 1 ELSE 0 END) = 1
        ) src ON UPPER(LTRIM(RTRIM(tgt.seis_set_name))) = src.nkey
        WHEN MATCHED THEN UPDATE SET
            seis_set_type = src.stype, file_path = src.file_path,
            catalog_id = src.catalog_id,
            bbox_min_lat = src.bmin_lat, bbox_max_lat = src.bmax_lat,
            bbox_min_lon = src.bmin_lon, bbox_max_lon = src.bmax_lon,
            epsg_code = src.epsg, remark = src.remark, source = 'CATALOG',
            geog = CASE
                     WHEN src.wkt IS NULL THEN tgt.geog
                     WHEN geography::STGeomFromText(src.wkt,4326).MakeValid().STIsValid() = 1
                       THEN CASE WHEN geography::STGeomFromText(src.wkt,4326).MakeValid().STArea()/1000000.0 > 255000000
                                 THEN geography::STGeomFromText(src.wkt,4326).MakeValid().ReorientObject()
                                 ELSE geography::STGeomFromText(src.wkt,4326).MakeValid()
                            END
                     ELSE tgt.geog
                   END,
            row_changed_by = 'PROMOTE', row_changed_date = GETUTCDATE()
        WHEN NOT MATCHED THEN INSERT (
            seis_set_id, seis_set_name, seis_set_type, file_path, catalog_id,
            bbox_min_lat, bbox_max_lat, bbox_min_lon, bbox_max_lon,
            epsg_code, remark, geog, active_ind, source,
            row_created_by, row_created_date, row_changed_by, row_changed_date
        ) VALUES (
            CONVERT(VARCHAR(40), NEWID()), src.sn, src.stype, src.file_path,
            src.catalog_id, src.bmin_lat, src.bmax_lat, src.bmin_lon,
            src.bmax_lon, src.epsg, src.remark,
            CASE
              WHEN src.wkt IS NULL THEN NULL
              WHEN geography::STGeomFromText(src.wkt,4326).MakeValid().STIsValid() = 1
                THEN CASE WHEN geography::STGeomFromText(src.wkt,4326).MakeValid().STArea()/1000000.0 > 255000000
                          THEN geography::STGeomFromText(src.wkt,4326).MakeValid().ReorientObject()
                          ELSE geography::STGeomFromText(src.wkt,4326).MakeValid()
                     END
              ELSE NULL
            END,
            'Y', 'CATALOG',
            'PROMOTE', GETUTCDATE(), 'PROMOTE', GETUTCDATE()
        );
    """)
    merged = cur.rowcount or 0
    cur.execute("IF OBJECT_ID('tempdb..#badseis') IS NOT NULL DROP TABLE #badseis")

    # ── Lines/volumes (Model A child): one dv_seis_line row per FILE, linked to
    # its parent survey by seis_set_id (resolved via the same normalized name).
    # dv_seis_line already exists in the schema — we populate it, no new table.
    vol_merged = 0
    if object_exists(cur, DV_SCHEMA, "dv_seis_line"):
        # Optional columns checked AT RUNTIME (sys/INFORMATION_SCHEMA), not
        # against the June DDL snapshot — geog and inventory_id were both
        # added after it, and reading the snapshot instead of the catalog is
        # what produced two nights of phantom ALTER TABLEs.
        def _line_has(col, dtype=None):
            q = ("SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
                 "WHERE TABLE_SCHEMA=? AND TABLE_NAME='dv_seis_line' "
                 "AND COLUMN_NAME=?")
            args = [DV_SCHEMA, col]
            if dtype:
                q += " AND DATA_TYPE=?"
                args.append(dtype)
            cur.execute(q, args)
            return bool(cur.fetchone()[0])
        has_geog = _line_has("geog", "geography")
        has_inv  = _line_has("inventory_id")

        # Report the FILES the gate holds (the survey-level report above can
        # hide a single bad vintage inside an otherwise-mapped survey).
        cur.execute(f"""
            SELECT COALESCE(g.FILE_NAME, s.LINE_NAME, s.SURVEY_NAME)
            FROM file_catalog.FILE_SEIS_HEADER s
            LEFT JOIN file_catalog.GLOBAL_FILE_CATALOG g
                   ON g.INVENTORY_ID = s.INVENTORY_ID
            WHERE {_NAMED} AND NOT {_MAPPABLE}""")
        _held_lines = sorted({r[0] for r in cur.fetchall() if r[0]})
        if _held_lines:
            log(f"  seismic gate: {len(_held_lines)} file(s) held from "
                f"dv_seis_line (no outline, no bbox):")
            for _hl in _held_lines:
                log(f"      - {_hl}")

        # Per-FILE construction pre-pass, mirroring #badseis but keyed on
        # SEIS_HEADER_ID. SELECT TOP 0 ... INTO inherits the id column's real
        # type instead of guessing it.
        _geo_sel = _geo_join = _geo_upd = _geo_insc = _geo_insv = ""
        _inv_upd = _inv_insc = _inv_insv = ""
        if has_geog:
            cur.execute("IF OBJECT_ID('tempdb..#badline') IS NOT NULL DROP TABLE #badline")
            cur.execute("SELECT TOP 0 s.SEIS_HEADER_ID AS hid INTO #badline "
                        "FROM file_catalog.FILE_SEIS_HEADER s")
            try:
                cur.execute(f"""
                    SELECT s.SEIS_HEADER_ID, s.SURVEY_OUTLINE
                    FROM file_catalog.FILE_SEIS_HEADER s
                    WHERE s.SURVEY_OUTLINE IS NOT NULL AND {_NAMED}""")
                _badl = []
                for _hid, _wkt in cur.fetchall():
                    try:
                        cur.execute(
                            "SELECT geography::STGeomFromText(?,4326).MakeValid().STIsValid()",
                            _wkt)
                        if cur.fetchone()[0] != 1:
                            _badl.append((_hid,))
                    except Exception:
                        _badl.append((_hid,))
                if _badl:
                    cur.executemany("INSERT INTO #badline (hid) VALUES (?)", _badl)
                    log(f"  seismic line geom skipped: {len(_badl)} file(s) with "
                        f"un-constructable outline (row still promoted, NULL geog)")
            except Exception as _pp:
                log(f"  seismic line pre-pass warning: {str(_pp).splitlines()[0][:80]}")

            _geo_sel  = (",\n                       CASE WHEN bl.hid IS NOT NULL THEN NULL"
                         "\n                            ELSE s.SURVEY_OUTLINE END AS wkt")
            _geo_join = ("\n                LEFT JOIN #badline bl"
                         "\n                       ON bl.hid = s.SEIS_HEADER_ID")
            # Lines have zero area, and per-file 3D corner polygons are tiny
            # against the hemisphere test — MakeValid alone suffices here.
            _geog_expr = ("CASE WHEN src.wkt IS NULL THEN {onnull} "
                          "WHEN geography::STGeomFromText(src.wkt,4326).MakeValid().STIsValid() = 1 "
                          "THEN geography::STGeomFromText(src.wkt,4326).MakeValid() "
                          "ELSE {onnull} END")
            _geo_upd  = ",\n                geog = " + _geog_expr.format(onnull="tgt.geog")
            _geo_insc = ", geog"
            _geo_insv = ",\n                " + _geog_expr.format(onnull="NULL")
        if has_inv:
            _inv_upd  = ",\n                inventory_id = src.catalog_id"
            _inv_insc = ", inventory_id"
            _inv_insv = ", src.catalog_id"

        cur.execute(f"""
            MERGE dataview.dv_seis_line AS tgt
            USING (
                SELECT s.SEIS_HEADER_ID AS src_id,
                       ss.seis_set_id   AS set_id,
                       COALESCE(NULLIF(LTRIM(RTRIM(s.LINE_NAME)),''),
                                g.FILE_NAME, s.SURVEY_NAME) AS line_name,
                       s.SEIS_SET_TYPE AS line_type,
                       TRY_CAST(s.SHOT_FIRST AS NUMERIC(18,2)) AS sp_start,
                       TRY_CAST(s.SHOT_LAST  AS NUMERIC(18,2)) AS sp_end,
                       s.IL_MIN AS cdp_start, s.IL_MAX AS cdp_end,
                       -- SAMPLE_INTERVAL is MICROseconds (SEG-Y binary
                       -- header); the column is sample_rate_ms. 4000 µs is
                       -- 4 ms — the value was right, the scale was not, and
                       -- anything dividing by it was off by 1000.
                       TRY_CAST(s.SAMPLE_INTERVAL AS NUMERIC(18,4)) / 1000.0 AS samp,
                       s.TRACE_COUNT AS ntrace,
                       g.FILE_PATH   AS file_path,
                       s.INVENTORY_ID AS catalog_id{_geo_sel}
                FROM file_catalog.FILE_SEIS_HEADER s
                LEFT JOIN file_catalog.GLOBAL_FILE_CATALOG g
                       ON g.INVENTORY_ID = s.INVENTORY_ID
                INNER JOIN dataview.dv_seis_set ss
                       ON UPPER(LTRIM(RTRIM(ss.seis_set_name))) = {_norm}{_geo_join}
                WHERE {_NAMED} AND {_MAPPABLE}
            ) src ON tgt.line_id = src.src_id
            WHEN MATCHED THEN UPDATE SET
                seis_set_id = src.set_id, line_name = src.line_name,
                line_type = src.line_type,
                shot_point_start = src.sp_start, shot_point_end = src.sp_end,
                cdp_start = src.cdp_start, cdp_end = src.cdp_end,
                sample_rate_ms = src.samp, trace_count = src.ntrace,
                file_path = src.file_path, source = 'CATALOG'{_inv_upd}{_geo_upd},
                row_changed_by = 'PROMOTE', row_changed_date = GETUTCDATE()
            WHEN NOT MATCHED THEN INSERT (
                seis_set_id, line_id, line_name, line_type,
                shot_point_start, shot_point_end, cdp_start, cdp_end,
                sample_rate_ms, trace_count, file_path{_inv_insc}{_geo_insc},
                active_ind, source, row_created_by, row_created_date,
                row_changed_by, row_changed_date
            ) VALUES (
                src.set_id, src.src_id, src.line_name, src.line_type,
                src.sp_start, src.sp_end, src.cdp_start, src.cdp_end,
                src.samp, src.ntrace, src.file_path{_inv_insv}{_geo_insv},
                'Y', 'CATALOG', 'PROMOTE', GETUTCDATE(), 'PROMOTE', GETUTCDATE()
            );
        """)
        vol_merged = cur.rowcount or 0
        if has_geog:
            cur.execute("IF OBJECT_ID('tempdb..#badline') IS NOT NULL DROP TABLE #badline")
        _extras = []
        if has_geog:
            _extras.append("geog")
        if has_inv:
            _extras.append("inventory_id")
        _note = ("seismic line/volume"
                 + (f" (+{'/'.join(_extras)})" if _extras else ""))
        log(f"{'dv_seis_line':30} {vol_merged:>9} {vol_merged:>8} "
            f"{len(_held_lines):>9}  {_note}")

    # The SAME note the dry run returns, so "what would happen" and "what
    # happened" describe the held files identically.
    return ("dv_seis_set", eligible, merged, held, _gate_note)



def promote_las_catalog(cur, apply, log):
    """Promote deep-binary log curves from the las_catalog.* tables up into
    dataview.dv_log_curve. Covers all three deep formats:

        LAS  : LAS_FILE        + LAS_FILE_CURVE
        DLIS : DLIS_FILE/FRAME  + DLIS_CHANNEL
        LIS  : LIS_FILE         + LIS_CHANNEL

    Design constraints honoured here:
      * dv_log_curve.INVENTORY_ID is NOT NULL but las_catalog carries only a
        repo-relative FILE_NAME, so INVENTORY_ID is resolved against
        GLOBAL_FILE_CATALOG by file basename and accepted ONLY on an unambiguous
        single match. 0 or >1 matches -> the file is HELD and reported, never
        nulled (governance: unmatched keys halt & audit).
      * dv_log_curve is also populated by curve_registry -> cat_log_curve, so
        rows are inserted ADDITIVELY with NOT EXISTS (keyed on INVENTORY_ID +
        CURVE_MNEMONIC + FRAME_NAME + LOGICAL_FILE). No deletes, so this never
        clobbers that path and is safe to re-run.
      * dv_log_curve has an FK to dv_well, so curves whose UWI is not yet in
        dv_well are HELD (not failed) and reported.

    Returns (target, eligible, promoted, held, note) like promote_table.
    """
    if not object_exists(cur, DV_SCHEMA, "dv_log_curve"):
        return ("dv_log_curve", None, None, None, "no dv target")

    # Only union the formats whose source tables actually exist.
    has_las  = object_exists(cur, "las_catalog", "LAS_FILE_CURVE")
    has_dlis = object_exists(cur, "las_catalog", "DLIS_CHANNEL")
    has_lis  = object_exists(cur, "las_catalog", "LIS_CHANNEL")
    if not (has_las or has_dlis or has_lis):
        return ("dv_log_curve", 0, 0, 0, "no las_catalog curves")

    branches = []
    if has_las:
        branches.append("""
        SELECT f.UWI AS uwi, 'LAS' AS fmt,
               CAST(NULL AS INT) AS logical_file,
               CAST(NULL AS NVARCHAR(128)) AS frame_name,
               c.CURVE_ID AS mnem, c.CURVE_DESCRIPTION AS long_name,
               c.CURVE_UNIT AS unit, c.API_CODE AS api_code,
               CAST(NULL AS NVARCHAR(32)) AS dim, CAST(NULL AS CHAR(1)) AS is_index,
               f.DEPTH_UOM AS uom, f.TOP_DEPTH AS dstart, f.BASE_DEPTH AS dstop,
               f.DEPTH_STEP AS dstep, f.SAMPLE_COUNT AS scount, f.FILE_NAME AS fname
        FROM las_catalog.LAS_FILE f
        JOIN las_catalog.LAS_FILE_CURVE c ON c.LAS_FILE_ID = f.LAS_FILE_ID""")
    if has_dlis:
        branches.append("""
        SELECT f.UWI, 'DLIS',
               ch.LOGICAL_FILE_IDX, ch.FRAME_NAME,
               ch.CHANNEL_NAME, ch.LONG_NAME, ch.UNITS, NULL,
               ch.DIMENSION, ch.IS_INDEX,
               fr.DEPTH_UOM, fr.TOP_DEPTH, fr.BASE_DEPTH, fr.SPACING,
               fr.SAMPLE_COUNT, f.FILE_NAME
        FROM las_catalog.DLIS_FILE f
        JOIN las_catalog.DLIS_CHANNEL ch ON ch.DLIS_FILE_ID = f.DLIS_FILE_ID
        LEFT JOIN las_catalog.DLIS_FRAME fr
               ON fr.DLIS_FILE_ID    = ch.DLIS_FILE_ID
              AND fr.LOGICAL_FILE_IDX = ch.LOGICAL_FILE_IDX
              AND fr.FRAME_NAME       = ch.FRAME_NAME""")
    if has_lis:
        branches.append("""
        SELECT f.UWI, 'LIS', NULL, NULL,
               ch.CHANNEL_NAME, NULL, ch.UNITS, NULL, NULL, ch.IS_INDEX,
               f.DEPTH_UOM, f.TOP_DEPTH, f.BASE_DEPTH, NULL,
               f.SAMPLE_COUNT, f.FILE_NAME
        FROM las_catalog.LIS_FILE f
        JOIN las_catalog.LIS_CHANNEL ch ON ch.LIS_FILE_ID = f.LIS_FILE_ID""")

    union_sql = "\n        UNION ALL\n".join(branches)

    # Common projection: resolve INVENTORY_ID (single unambiguous match only).
    base_cte = f"""
        WITH allc AS (
        {union_sql}
        ),
        resolved AS (
            SELECT a.*,
                   (SELECT CASE WHEN COUNT(*) = 1 THEN MAX(g.INVENTORY_ID) END
                      FROM file_catalog.GLOBAL_FILE_CATALOG g
                     WHERE g.FILE_NAME = RIGHT(
                               ISNULL(REPLACE(a.fname, '/', '\\'), ''),
                               CHARINDEX('\\',
                                   REVERSE(ISNULL(REPLACE(a.fname, '/', '\\'), '')) + '\\') - 1)
                   ) AS inv_id,
                   CASE WHEN EXISTS (SELECT 1 FROM dataview.dv_well w
                                      WHERE w.uwi = a.uwi)
                        THEN 1 ELSE 0 END AS has_well
            FROM allc a
        )"""

    # ---- diagnostics (always computed; cheap relative to the insert) --------
    cur.execute(base_cte + """
        SELECT
            COUNT(*) AS eligible,
            SUM(CASE WHEN inv_id IS NULL THEN 1 ELSE 0 END) AS no_inv,
            SUM(CASE WHEN inv_id IS NOT NULL AND has_well = 0
                     THEN 1 ELSE 0 END) AS no_well
        FROM resolved;""")
    eligible, no_inv, no_well = cur.fetchone()
    eligible = eligible or 0
    no_inv   = no_inv or 0
    no_well  = no_well or 0

    if not apply or not eligible:
        held = no_inv + no_well
        if no_inv:
            log(f"    {'':30}   held {no_inv:>6}  no INVENTORY_ID match (audit)")
        if no_well:
            log(f"    {'':30}   held {no_well:>6}  UWI not yet in dv_well")
        return ("dv_log_curve", eligible, 0, held, "LAS/DLIS/LIS curves")

    # ---- apply: additive insert of resolvable, well-backed, new curves -----
    cur.execute(base_cte + """
        INSERT INTO dataview.dv_log_curve
            (INVENTORY_ID, UWI, UWI14, SOURCE_FORMAT, LOGICAL_FILE, FRAME_NAME,
             CURVE_INDEX, CURVE_MNEMONIC, CURVE_LONG_NAME, CURVE_UNIT, API_CODE,
             CURVE_DIMENSION, IS_INDEX, DEPTH_UOM, DEPTH_START, DEPTH_STOP,
             DEPTH_STEP, SAMPLE_COUNT, NULL_VALUE)
        SELECT r.inv_id, r.uwi, NULL, r.fmt, r.logical_file, r.frame_name,
               NULL, r.mnem, r.long_name, r.unit, r.api_code,
               r.dim, r.is_index, r.uom, r.dstart, r.dstop,
               r.dstep, r.scount, NULL
        FROM resolved r
        WHERE r.inv_id IS NOT NULL
          AND r.mnem IS NOT NULL
          AND r.has_well = 1
          AND NOT EXISTS (
                SELECT 1 FROM dataview.dv_log_curve d
                 WHERE d.INVENTORY_ID = r.inv_id
                   AND d.CURVE_MNEMONIC = r.mnem
                   AND ISNULL(d.FRAME_NAME, '') = ISNULL(r.frame_name, '')
                   AND ISNULL(d.LOGICAL_FILE, -1) = ISNULL(r.logical_file, -1));""")
    promoted = cur.rowcount or 0
    held = no_inv + no_well
    if no_inv:
        log(f"    {'':30}   held {no_inv:>6}  no INVENTORY_ID match (audit)")
    if no_well:
        log(f"    {'':30}   held {no_well:>6}  UWI not yet in dv_well")
    return ("dv_log_curve", eligible, promoted, held, "LAS/DLIS/LIS curves")


def _safe_promote(cur, fn, log, *args):
    """Run one promoter with error isolation. `args` are the promoter's own
    arguments (after cur). On success returns its 5-tuple; on failure logs a
    FAILED line and returns a zero-result so run_promote continues with the rest
    instead of aborting the whole batch.

    A SQL Server savepoint around the call lets a failed statement roll back
    without dooming the shared outer transaction the following promoters use."""
    import time as _time
    _sp = "promote_sp"
    name = getattr(fn, "__name__", "promote")
    # generic loop promotes many tables through promote_table — key the timing on
    # the table name (args[0]) so each cat_* mirror gets its own line.
    _label = str(args[0]) if (name == "promote_table" and args) else name
    _t0 = _time.monotonic()
    try:
        cur.execute(f"SAVE TRANSACTION {_sp}")
    except Exception:
        _sp = None                       # not in a transaction — proceed without
    try:
        return fn(cur, *args)
    except Exception as ex:
        if _sp:
            try:
                cur.execute(f"ROLLBACK TRANSACTION {_sp}")
            except Exception:
                pass
        # NAME THE MIRROR, NOT THE FUNCTION. `name` is "promote_table" for
        # every one of the eighteen generic mirrors, so the FAILED line said
        # nothing about WHICH table failed — while _label, already computed
        # above for the timing, holds exactly that.
        #
        # AND DO NOT TRUNCATE THE ONE LINE THAT EXPLAINS IT. A SQL Server FK
        # error puts the constraint name and the parent table past the 120th
        # character:
        #   ...conflicted with the FOREIGN KEY constraint "fk_log_curve_log".
        #   The conflict occurred in database "DataView_Demo", table
        #   "dataview.dv_well_log".
        # Everything after "the FOREIG" was cut, which is everything worth
        # knowing. A discarded diagnostic is what makes the next failure
        # undiagnosable — this one cost a full reproduce-and-bisect to recover
        # a message the process already had in its hands.
        msg = " ".join(str(ex).split())[:600]
        log(f"{_label:30} {'':>9} {'':>8} {'':>9}  FAILED: {msg}")
        return (_label, None, 0, 0, "FAILED")
    finally:
        _STEP_TIMES[_label] = _STEP_TIMES.get(_label, 0.0) + (_time.monotonic() - _t0)


def tag_catalog_identity(cur, log=print):
    """Stamp GLOBAL_FILE_CATALOG.UWI14 / SURVEY_NAME from the INVENTORY_ID linkage
    so the mapping app can list a well's (or survey's) documents straight off the
    catalog — no FILE_WELL_HEADER join at read time.

      UWI14       from FILE_WELL_HEADER (normalized to the 14-char key, only where
                  a document maps to exactly ONE promoted dv_well — so the stamped
                  value always matches a mappable well and multi-well docs are left
                  to the header join).
      SURVEY_NAME from FILE_SEIS_HEADER (one distinct survey per document).

    Idempotent: only rows whose value actually changes are written.
    """
    gcols = {r[0].upper(): r[0] for r in cur.execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA='file_catalog' "
        "AND TABLE_NAME='GLOBAL_FILE_CATALOG'").fetchall()}
    tagged_u = tagged_s = 0

    # ── UWI14 ────────────────────────────────────────────────────────────────
    if "UWI14" in gcols and object_exists(cur, DV_SCHEMA, "dv_well"):
        try:
            unions = []
            # (a) FILE_WELL_HEADER — header-only PDFs (scout / EOWR / survey).
            if object_exists(cur, "file_catalog", "FILE_WELL_HEADER"):
                hcols = {r[0].upper(): r[0] for r in cur.execute(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA='file_catalog' "
                    "AND TABLE_NAME='FILE_WELL_HEADER'").fetchall()}
                hu = hcols.get("UWI14") or hcols.get("UWI")
                if hu:
                    unions.append(
                        f"SELECT INVENTORY_ID, {_norm(f'[{hu}]')} AS uwi14 "
                        f"FROM file_catalog.FILE_WELL_HEADER "
                        f"WHERE INVENTORY_ID IS NOT NULL AND "
                        f"NULLIF(LTRIM(RTRIM(CONVERT(varchar(64),[{hu}]))),'') "
                        f"IS NOT NULL")
            # (b) every dv_* detail table carrying uwi + INVENTORY_ID (LAS logs,
            #     completions, production, dir-survey, tops …) so those documents
            #     get tagged too — not only the header-writing PDFs.
            for (t,) in cur.execute(
                    "SELECT c1.TABLE_NAME FROM INFORMATION_SCHEMA.COLUMNS c1 "
                    "JOIN INFORMATION_SCHEMA.COLUMNS c2 "
                    "  ON c1.TABLE_SCHEMA=c2.TABLE_SCHEMA "
                    "  AND c1.TABLE_NAME=c2.TABLE_NAME "
                    "WHERE c1.TABLE_SCHEMA='dataview' "
                    "  AND UPPER(c1.COLUMN_NAME)='UWI' "
                    "  AND UPPER(c2.COLUMN_NAME)='INVENTORY_ID' "
                    "  AND c1.TABLE_NAME LIKE 'dv[_]%' "
                    "GROUP BY c1.TABLE_NAME").fetchall():
                unions.append(
                    f"SELECT INVENTORY_ID, {_norm('uwi')} AS uwi14 "
                    f"FROM dataview.[{t}] "
                    f"WHERE INVENTORY_ID IS NOT NULL AND uwi IS NOT NULL")
            if unions:
                usql = " UNION ALL ".join(unions)
                cur.execute(
                    f"WITH src AS ({usql}), "
                    f"iu AS (SELECT INVENTORY_ID, MIN(uwi14) AS uwi14, "
                    f"COUNT(DISTINCT uwi14) AS n FROM src GROUP BY INVENTORY_ID) "
                    f"UPDATE g SET g.[{gcols['UWI14']}] = iu.uwi14 "
                    f"FROM file_catalog.GLOBAL_FILE_CATALOG g "
                    f"JOIN iu ON iu.INVENTORY_ID = g.INVENTORY_ID "
                    f"JOIN {DV_SCHEMA}.dv_well w ON w.uwi = iu.uwi14 "
                    f"WHERE iu.n = 1 AND (g.[{gcols['UWI14']}] IS NULL "
                    f"OR g.[{gcols['UWI14']}] <> iu.uwi14)")
                tagged_u = cur.rowcount or 0
        except Exception as e:
            log(f"-- tag: UWI14 skipped ({str(e).splitlines()[0][:120]})")

    # ── SURVEY_NAME ──────────────────────────────────────────────────────────
    if "SURVEY_NAME" in gcols and object_exists(cur, "file_catalog",
                                                "FILE_SEIS_HEADER"):
        try:
            cur.execute(
                f"UPDATE g SET g.[{gcols['SURVEY_NAME']}] = s.sn\n"
                f"FROM file_catalog.GLOBAL_FILE_CATALOG g\n"
                f"JOIN (SELECT INVENTORY_ID, MAX(SURVEY_NAME) AS sn,\n"
                f"             COUNT(DISTINCT SURVEY_NAME) AS n\n"
                f"      FROM file_catalog.FILE_SEIS_HEADER\n"
                f"      WHERE NULLIF(LTRIM(RTRIM(SURVEY_NAME)),'') IS NOT NULL\n"
                f"      GROUP BY INVENTORY_ID) s ON s.INVENTORY_ID = g.INVENTORY_ID\n"
                f"WHERE s.n = 1\n"
                f"  AND (g.[{gcols['SURVEY_NAME']}] IS NULL "
                f"OR g.[{gcols['SURVEY_NAME']}] <> s.sn)")
            tagged_s = cur.rowcount or 0
        except Exception as e:
            log(f"-- tag: SURVEY_NAME skipped ({str(e).splitlines()[0][:120]})")

    log(f"-- tag: UWI14 on {tagged_u} · SURVEY_NAME on {tagged_s} catalog row(s)")
    return tagged_u, tagged_s


def run_promote(cur, uwi=None, apply=False, log=print):
    """Promote every discovered cat_* mirror up into its dv_* table, logging one
    line per mirror plus a TOTAL. Shared by main() and the Pipeline-page button
    so both run identical logic. The caller owns the connection and its
    transaction — run_promote never commits or rolls back.

    Each promoter is isolated: a failure in one (e.g. an unrepairable geometry)
    is logged and skipped, and the remaining promoters still run. No single bad
    row or table aborts the whole promote."""
    _STEP_TIMES.clear()
    import time as _time
    _wall0 = _time.monotonic()
    _phase: dict = {}
    def _ph(_name, _fn, *_a, **_k):    # time an untimed setup phase (logging only)
        _t = _time.monotonic()
        try:
            return _fn(*_a, **_k)
        finally:
            _phase[_name] = _phase.get(_name, 0.0) + (_time.monotonic() - _t)
    _ph("prime_metadata", _prime_metadata, cur)   # one-time bulk reflect
    # Spatial (geography) and computed-column INSERT/UPDATE REQUIRE these ON.
    # Set them explicitly so promote works regardless of connection defaults.
    try:
        cur.execute("SET QUOTED_IDENTIFIER ON")
        cur.execute("SET ANSI_WARNINGS ON")
    except Exception:
        pass
    if apply:
        _ph("ensure_catalog_source", _ensure_catalog_source, cur)  # FK target
    log(f"{'mirror':30} {'eligible':>9} {'moved':>8} {'repl/upd':>9}  note")
    log("-" * 74)
    total_e = total_m = total_r = 0
    _dt0 = _time.monotonic()
    _dv_tables = list(discover_tables(cur))
    _phase["discover_tables"] = _time.monotonic() - _dt0
    for dv in _dv_tables:
        cat, eligible, moved, repl, note = _safe_promote(
            cur, promote_table, log, dv, uwi, apply)
        e = "" if eligible is None else f"{eligible:>9}"
        m = "" if moved    is None else f"{moved:>8}"
        r = "" if repl     is None else f"{repl:>9}"
        log(f"{cat:30} {e:>9} {m:>8} {r:>9}  {note}")
        total_e += eligible or 0
        total_m += moved or 0
        total_r += repl or 0
    # seismic surveys: FILE_SEIS_HEADER -> dv_seis_set (not a cat_* mirror)
    scat, se, sm, sr, snote = _safe_promote(cur, promote_seismic, log, apply, log)
    se_s = "" if se is None else f"{se:>9}"
    sm_s = "" if sm is None else f"{sm:>8}"
    sr_s = "" if sr is None else f"{sr:>9}"
    log(f"{scat:30} {se_s:>9} {sm_s:>8} {sr_s:>9}  {snote}")
    total_e += se or 0
    total_m += sm or 0
    # field polygons: GLOBAL_FILE_CATALOG (SPATIAL_OUTLINE) -> dv_field.geog
    fcat, fe, fm, fr, fnote = _safe_promote(cur, promote_field, log, apply, log)
    fe_s = "" if fe is None else f"{fe:>9}"
    fm_s = "" if fm is None else f"{fm:>8}"
    fr_s = "" if fr is None else f"{fr:>9}"
    log(f"{fcat:30} {fe_s:>9} {fm_s:>8} {fr_s:>9}  {fnote}")
    total_e += fe or 0
    total_m += fm or 0
    # other spatial feature types: cat_* -> dv_* with geography
    for _pf in (promote_land_tract, promote_boundary, promote_pipeline):
        pcat, pe, pm, pr, pnote = _safe_promote(cur, _pf, log, apply, log)
        pe_s = "" if pe is None else f"{pe:>9}"
        pm_s = "" if pm is None else f"{pm:>8}"
        pr_s = "" if pr is None else f"{pr:>9}"
        log(f"{pcat:30} {pe_s:>9} {pm_s:>8} {pr_s:>9}  {pnote}")
        total_e += pe or 0
        total_m += pm or 0
    # well point geography: dv_well lat/lon -> dv_well.geog (POINT)
    wcat, we, wm, wr, wnote = _safe_promote(cur, promote_well_geog, log, apply, log)
    we_s = "" if we is None else f"{we:>9}"
    wm_s = "" if wm is None else f"{wm:>8}"
    wr_s = "" if wr is None else f"{wr:>9}"
    log(f"{wcat:30} {we_s:>9} {wm_s:>8} {wr_s:>9}  {wnote}")
    total_e += we or 0
    total_m += wm or 0
    # deep log curves: las_catalog.* (LAS/DLIS/LIS) -> dv_log_curve
    lcat, le, lm, lr, lnote = _safe_promote(cur, promote_las_catalog, log, apply, log)
    le_s = "" if le is None else f"{le:>9}"
    lm_s = "" if lm is None else f"{lm:>8}"
    lr_s = "" if lr is None else f"{lr:>9}"
    log(f"{lcat:30} {le_s:>9} {lm_s:>8} {lr_s:>9}  {lnote}")
    total_e += le or 0
    total_m += lm or 0
    # NOTE: file_catalog.cat_log_curve -> dataview.dv_log_curve is handled by the
    # generic discover_tables() loop above (it's an ordinary cat_* mirror), so no
    # dedicated promoter is needed here.
    if apply:
        # Stamp the catalog's UWI14 / SURVEY_NAME off the INVENTORY_ID linkage so
        # the mapping app can list a well's / survey's documents straight off
        # GLOBAL_FILE_CATALOG.
        import time as _time
        _tt0 = _time.monotonic()
        try:
            tag_catalog_identity(cur, log)
        except Exception as _te:
            log(f"-- tag: skipped ({str(_te).splitlines()[0][:120]})")
        _STEP_TIMES["tag_catalog_identity"] = (
            _STEP_TIMES.get("tag_catalog_identity", 0.0)
            + (_time.monotonic() - _tt0))
    log("-" * 74)
    log(f"{'TOTAL':30} {total_e:>9} {total_m:>8} {total_r:>9}")

    # slowest-first per-step timing so the promote seconds are attributable
    _steps = sorted(_STEP_TIMES.items(), key=lambda kv: -kv[1])
    _shown = [(k, v) for k, v in _steps if v >= 0.05][:12]
    if _shown:
        log("-- promote timing (slowest first): " + " · ".join(
            f"{k.split('.')[-1]} {v:.2f}s" for k, v in _shown))

    # ---- instrumentation: reconcile run_promote wall against its parts -------
    _wall = _time.monotonic() - _wall0
    _steps_sum = sum(_STEP_TIMES.values())
    _phase_sum = sum(_phase.values())
    _other = _wall - _phase_sum - _steps_sum
    _psub = _META.get("prime_sub") or {}
    _psub_str = (" [prime: " + " ".join(f"{k} {v:.2f}" for k, v in _psub.items())
                 + "]") if _psub else ""
    log("[promote-phase] wall {:.2f}s = ".format(_wall)
        + " + ".join(f"{k} {v:.2f}" for k, v in
                     sorted(_phase.items(), key=lambda kv: -kv[1]))
        + f" + steps {_steps_sum:.2f} + other {_other:.2f}" + _psub_str)
    log("[promote-steps-all] " + " · ".join(
        f"{k.split('.')[-1]} {v:.2f}s" for k, v in _steps if v >= 0.02))
    # -------------------------------------------------------------------------
    return total_e, total_m, total_r


def enrich_from_gold(cur, gold_db="WELL_REF", gold_schema="well_ref",
                     gold_table="well_master_gold", uwi=None, uwis=None,
                     log=print):
    """Post-promote enrichment: fill NULL dv_well columns from well_master_gold.

    Set-based UPDATE keyed on g.uwi14 = dv_well.uwi. COALESCE means the document's
    own value always wins; gold only fills gaps. The well_name guard treats a
    well_name that merely echoes the UWI (a placeholder from a bare header) as
    empty, so gold's real name replaces it.

    Only the 14 gold columns that map to a dv_well column are filled; dv_well
    columns gold doesn't carry (kb_elevation, completion_date, formation_at_td,
    bottomhole coords, h3_*, …) are left for the document extractors or a later
    geocoding pass. Reachability failures (gold db absent) are caught and logged,
    never fatal — promotion has already succeeded by this point.
    """
    gold = f"{gold_db}.{gold_schema}.{gold_table}"
    # gold_col -> dv_well_col   (only columns that exist on both, names aligned)
    M = [
        ("well_name",         "well_name"),          # special-cased below
        ("well_num",          "well_num"),
        ("operator_name",     "operator_name"),
        ("field_name",        "field_name"),
        ("surface_latitude",  "surface_latitude"),
        ("surface_longitude", "surface_longitude"),
        ("county",            "county"),
        ("province_state",    "province_state"),
        ("country",           "country"),
        ("std_well_type",     "well_type"),
        ("std_well_status",   "well_status"),
        ("total_depth",       "final_td"),
        ("spud_date",         "spud_date"),
        ("api_10",            "api_num"),
    ]
    sets = []
    for gcol, dcol in M:
        if dcol == "well_name":
            # placeholder guard: well_name == uwi  → treat as empty
            sets.append("w.well_name = COALESCE(NULLIF(w.well_name, w.uwi), "
                        f"g.{gcol})")
        else:
            sets.append(f"w.{dcol} = COALESCE(w.{dcol}, g.{gcol})")
    set_clause = ",\n        ".join(sets)
    where = ""
    params = []
    scope_join = ""
    if uwis is not None:
        # Scope to just the UWIs promoted THIS run — enrich exists to fill the
        # rows we just moved, not to re-scan every dv_well row against gold on
        # every promote (that unscoped join was the slow part). An empty set
        # means nothing to enrich → skip entirely.
        uset = sorted({u for u in uwis if u})
        if not uset:
            log("-- enrich: 0 dv_well row(s) filled (no promoted UWIs)")
            return 0
        cur.execute("IF OBJECT_ID('tempdb..#enr_uwi') IS NOT NULL DROP TABLE #enr_uwi")
        cur.execute("CREATE TABLE #enr_uwi (uwi nvarchar(40) PRIMARY KEY)")
        cur.executemany("INSERT INTO #enr_uwi (uwi) VALUES (?)", [(u,) for u in uset])
        scope_join = "\n    JOIN #enr_uwi eu ON eu.uwi = w.uwi"
    elif uwi:
        where = f" AND w.uwi = {_norm('?')}"
        params.append(uwi)
    # Pre-cast the candidate UWIs to char(14) ONCE into an indexed temp so the
    # gold join seeks IX_WM_UWI14 instead of scanning 3.5M rows (CAST(w.uwi AS
    # char(14)) on every row is non-sargable). Candidates = rows needing a fill,
    # scoped to this run's uwis when provided.
    cur.execute("IF OBJECT_ID('tempdb..#gk') IS NOT NULL DROP TABLE #gk")
    cur.execute("CREATE TABLE #gk (uwi14 char(14) PRIMARY KEY, src_uwi nvarchar(80))")
    _scope_where = ""
    if uwis:
        cur.execute("IF OBJECT_ID('tempdb..#enr_uwi') IS NOT NULL DROP TABLE #enr_uwi")
        cur.execute("CREATE TABLE #enr_uwi (uwi nvarchar(80) PRIMARY KEY)")
        _uset = sorted({str(u).strip() for u in uwis if u and str(u).strip()})
        if _uset:
            cur.fast_executemany = True
            cur.executemany("INSERT INTO #enr_uwi (uwi) VALUES (?)", [(u,) for u in _uset])
            _scope_where = " AND w.uwi IN (SELECT uwi FROM #enr_uwi)"
    elif uwi:
        _scope_where = f" AND w.uwi = {_norm('?')}"
        params.append(uwi)
    # distinct char(14) keys for the wells that still need a fill
    cur.execute(
        f"INSERT INTO #gk (uwi14, src_uwi)\n"
        f"SELECT MIN(CAST(w.uwi AS char(14))), w.uwi\n"
        f"    FROM {DV_SCHEMA}.dv_well w\n"
        f"    WHERE (w.surface_latitude IS NULL OR w.operator_name IS NULL\n"
        f"           OR w.county IS NULL OR w.spud_date IS NULL\n"
        f"           OR w.well_name = w.uwi)\n"
        f"      AND LEN(LTRIM(RTRIM(w.uwi))) >= 10{_scope_where}\n"
        f"    GROUP BY w.uwi", *params)
    sql = (
        f"UPDATE w SET\n        {set_clause}\n"
        f"    FROM {DV_SCHEMA}.dv_well w\n"
        f"    JOIN #gk k ON k.src_uwi = w.uwi\n"
        f"    JOIN {gold} g ON g.uwi14 = k.uwi14\n"
        f"    WHERE (w.surface_latitude IS NULL OR w.operator_name IS NULL\n"
        f"           OR w.county IS NULL OR w.spud_date IS NULL\n"
        f"           OR w.well_name = w.uwi)")
    try:
        cur.execute(sql)   # #gk is pre-scoped; no params needed on the UPDATE
        n = cur.rowcount or 0
        cur.execute("DROP TABLE #gk")
        log(f"-- enrich: {n} dv_well row(s) filled from {gold} (sargable seek)")
    except Exception as e:
        # gold unreachable / name resolution / permission — non-fatal. Still run
        # the UWI-prefix backfill (it needs no gold) before returning.
        msg = str(e).splitlines()[0][:160]
        log(f"-- enrich: skipped ({msg})")
        enrich_from_uwi(cur, uwis, log)
        return 0

    # Also backfill FILE_WELL_HEADER coordinates from gold. The documents map
    # reads FILE_WELL_HEADER (not dv_well), so without this the map can't plot
    # wells whose own documents (scout tickets, DDRs) carried no surface
    # coordinates. Keyed on UWI14, fills only where the header lat/long is NULL —
    # the document's own coordinate always wins. Non-fatal.
    try:
        cur.execute(
            f"UPDATE h SET\n"
            f"        LATITUDE  = COALESCE(NULLIF(LTRIM(RTRIM(h.LATITUDE)),''),  "
            f"CAST(g.surface_latitude AS nvarchar(30))),\n"
            f"        LONGITUDE = COALESCE(NULLIF(LTRIM(RTRIM(h.LONGITUDE)),''), "
            f"CAST(g.surface_longitude AS nvarchar(30))),\n"
            f"        WELL_NAME = COALESCE(NULLIF(h.WELL_NAME, h.UWI), h.WELL_NAME, "
            f"g.well_name),\n"
            f"        OPERATOR  = COALESCE(NULLIF(LTRIM(RTRIM(h.OPERATOR)),''), "
            f"g.operator_name)\n"
            f"    FROM file_catalog.FILE_WELL_HEADER h\n"
            f"    JOIN {gold} g ON g.uwi14 = h.UWI14\n"
            f"    WHERE g.surface_latitude IS NOT NULL\n"
            f"      AND (NULLIF(LTRIM(RTRIM(h.LATITUDE)),'')  IS NULL\n"
            f"           OR NULLIF(LTRIM(RTRIM(h.LONGITUDE)),'') IS NULL)")
        hn = cur.rowcount or 0
        log(f"-- enrich: {hn} FILE_WELL_HEADER row(s) given coordinates from gold")
    except Exception as e:
        msg = str(e).splitlines()[0][:160]
        log(f"-- enrich: FILE_WELL_HEADER coord backfill skipped ({msg})")
    enrich_from_uwi(cur, uwis, log)
    return n


def enrich_from_uwi(cur, uwis=None, log=print):
    """Backfill province_state / county / country on dv_well from the UWI's
    API-D12A prefix, for rows gold could not resolve. State = first 2 UWI digits
    = dv_province_state.province_state_id (API D12A code, e.g. 42=Texas); county =
    next 3 = dv_county.fips_county_code within that state (e.g. 42-317=Martin).
    COALESCE only — never overrides a value the document or gold already set."""
    if not object_exists(cur, DV_SCHEMA, "dv_well"):
        return 0
    scope = ""
    if uwis is not None:
        uset = sorted({u for u in uwis if u})
        if not uset:
            return 0
        cur.execute("IF OBJECT_ID('tempdb..#enr_uwi2') IS NOT NULL DROP TABLE #enr_uwi2")
        cur.execute("CREATE TABLE #enr_uwi2 (uwi nvarchar(40) PRIMARY KEY)")
        cur.executemany("INSERT INTO #enr_uwi2 (uwi) VALUES (?)",
                        [(u,) for u in uset])
        scope = " JOIN #enr_uwi2 eu ON eu.uwi = w.uwi"
    total = 0
    try:
        if object_exists(cur, DV_SCHEMA, "dv_province_state"):
            cur.execute(
                f"UPDATE w SET "
                f"province_state = COALESCE(w.province_state, ps.province_state_abbrev), "
                f"country = COALESCE(w.country, CASE WHEN ps.country_code='USA' "
                f"THEN 'US' ELSE ps.country_code END) "
                f"FROM {DV_SCHEMA}.dv_well w{scope} "
                f"JOIN {DV_SCHEMA}.dv_province_state ps "
                f"ON ps.province_state_id = LEFT(w.uwi, 2) "
                f"WHERE w.uwi IS NOT NULL AND LEN(w.uwi) >= 2 "
                f"AND (w.province_state IS NULL OR w.country IS NULL)")
            total += cur.rowcount or 0
        if object_exists(cur, DV_SCHEMA, "dv_county"):
            cur.execute(
                f"UPDATE w SET county = c.county_name "
                f"FROM {DV_SCHEMA}.dv_well w{scope} "
                f"JOIN {DV_SCHEMA}.dv_county c "
                f"ON c.province_state_id = LEFT(w.uwi, 2) "
                f"AND c.fips_county_code = SUBSTRING(w.uwi, 3, 3) "
                f"WHERE w.county IS NULL AND w.uwi IS NOT NULL AND LEN(w.uwi) >= 5")
            total += cur.rowcount or 0
        log(f"-- enrich(uwi): {total} state/county/country fill(s) from UWI prefix")
    except Exception as e:
        log(f"-- enrich(uwi): skipped ({str(e).splitlines()[0][:140]})")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server",   default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--database", default="DataView")
    ap.add_argument("--uwi", default=None, help="promote only this UWI")
    ap.add_argument("--apply", action="store_true",
                    help="execute (else report eligible counts)")
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip the post-promote gold enrichment backfill")
    ap.add_argument("--gold-db", default="WELL_REF",
                    help="database holding well_master_gold (default WELL_REF)")
    a = ap.parse_args()

    con = connect(a.server, a.database)
    con.autocommit = not a.apply           # one commit at the end on apply
    cur = con.cursor()

    print(f"-- target: {a.server} / {a.database}")
    print(f"-- mode  : {'APPLY (move)' if a.apply else 'DRY-RUN'}"
          f"{f'  uwi={a.uwi}' if a.uwi else ''}\n")
    try:
        run_promote(cur, a.uwi, a.apply, log=print)
        if a.apply and not a.no_enrich:
            enrich_from_gold(cur, gold_db=a.gold_db, uwi=a.uwi, log=print)
        if a.apply:
            con.commit()
    except Exception as e:
        if a.apply:
            con.rollback()
        print(f"\n-- ERROR (rolled back): {e}", file=sys.stderr)
        con.close()
        return 1

    con.close()
    print("\n-- done (rows moved up; cat_* cleared of promoted rows)" if a.apply
          else "\n-- dry-run complete (use --apply to move)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
