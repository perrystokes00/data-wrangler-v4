"""
catalog_status.py
=================
One answer to "what happened to every file, and if it is stuck, WHY".

WHAT THIS ADDS THAT DID NOT EXIST
---------------------------------
Three modules already answer part of the question and none answers this part:

  * promotion_lineage.file_detail  -- per file: extracted / captured / promoted,
    with the row counts behind each. Says a file is `(staged)`. Never says why.
  * catalog_readiness              -- derives CATALOGED / PROMOTED from row
    reality. CATALOGED conflates "promote has not run yet" with "promote ran
    and REFUSED these rows" -- the two states a backlog review has to separate.
  * promote_catalog                -- knows exactly why, and says so only as a
    counter in a log line ("held 29 (no coords)"), attributed to a TABLE, never
    to a FILE, and never persisted.

So the reason a file is stuck existed only in the scrollback of whoever last
ran promote. This module derives it per INVENTORY_ID, from the mirrors, on
demand.

DERIVED, NEVER STAMPED -- and that is deliberate
------------------------------------------------
A hold reason could have been written onto GLOBAL_FILE_CATALOG during promote.
It is not, because a stamp is a snapshot and every one of these reasons is
CURABLE BY SOMEONE ELSE'S ACTION: seed one code into dv_r_uom and thirty files
across four mirrors stop being held, with nothing touching their catalog rows.
A stamp would still read "held" until the next promote re-walked them, and a
stale "held" is exactly the confident-wrong-value failure CLAUDE.md warns about
-- it plots, it exports, it gets quoted. Deriving costs one query per non-empty
mirror and cannot go stale.

THE GATES ARE PROMOTE'S, NOT A SECOND OPINION
---------------------------------------------
Every predicate here is the one promote_catalog actually applies, reached
through the same helpers where a helper exists:

  gate                     promote_catalog                     here
  -----------------------  ----------------------------------  ------------------
  no UWI                   _promote_header base predicate      _HOLD_NO_UWI
  no coords                _promote_header coord_pred          _HOLD_NO_COORDS
                           (gated on REQUIRE_WELL_COORDS -- imported, not copied)
  unresolved dv_r_* code   _reference_fk_predicates            _ref_fks (SQLAlchemy
                                                               twin, already in
                                                               promote_fk_review)
  well not in dv_well      _promote_detail base_where EXISTS   _HOLD_NO_WELL
  seismic: unnamed         promote_seismic _NAMED              _HOLD_SEIS_UNNAMED
  seismic: not mappable    promote_seismic _MAPPABLE           _HOLD_SEIS_UNMAPPABLE

`_norm` is IMPORTED from promote_catalog rather than re-spelled. UWI-14 padding
must agree on both sides of every comparison; a re-spelled normalizer is how a
suppression clause once went silently inert for six weeks.

WHICH TABLES COUNT
------------------
Two different questions, two different lists, on purpose -- the same split
catalog_readiness._sources documents:

  * the dv_ side (is this file PROMOTED?) is promotion_lineage, via
    catalog_readiness._sources, so this agrees with every report.
  * the cat_ side (what is this file HOLDING, anywhere?) is a sweep of every
    promotable mirror. A mirror missing from LINEAGE is precisely the case that
    would hold rows invisibly, which is the failure that cost 1,433 unseen rows
    once already. Same reasoning page_workbench._mark_bad gives for its cascade.

Mirrors that cannot be attributed to a file (no INVENTORY_ID column) are
REPORTED in the result's `notes`, never silently skipped.

Usage
-----
    from dataview.file_catalog import catalog_status as cs
    res = cs.file_status(engine)            # -> StatusResult
    res.df                                  # DataFrame, one row per file
    res.notes                               # anything the caller should know

    python -m dataview.file_catalog.catalog_status --held-only --csv out.csv

mssql only, matching promote_catalog.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CAT_SCHEMA = "file_catalog"
DV_SCHEMA = "dataview"
GFC = f"{CAT_SCHEMA}.GLOBAL_FILE_CATALOG"

# ── the states, in precedence order ─────────────────────────────────────────
# HELD outranks PROMOTED deliberately. A file whose header lifted but whose
# detail rows are stuck is a BACKLOG ITEM, and calling it PROMOTED is how the
# remaining rows stop being looked at. Both counts travel with the row
# (rows_dv / rows_cat) so a partial promote stays visible rather than collapsing
# into either word -- the same discipline promotion_lineage applies to its four
# states.
ST_SKIPPED = "SKIPPED"          # rejected / terminal
ST_HELD = "HELD"                # rows in cat_*, at least one gate refuses them
ST_STAGED = "STAGED"            # rows in cat_*, gates pass, promote not yet run
ST_PROMOTED = "PROMOTED"        # represented in dv_*
ST_MOVED = "MOVED"              # HEADER_EXTRACTED='M' -- file is not where the catalog says
ST_ERROR = "ERROR"              # HEADER_EXTRACTED='E' -- extractor failed
ST_EXTRACTED = "EXTRACTED"      # extracted, nothing captured yet
ST_INVENTORIED = "INVENTORIED"  # scanned only

# ── hold reason labels ──────────────────────────────────────────────────────
_HOLD_NO_UWI = "no UWI"
_HOLD_NO_COORDS = "no coords"
# THREE REASONS, NOT ONE, for the same dv_well EXISTS gate. Promote applies a
# single predicate, but the three ways to fail it need three different repairs,
# and collapsing them sends you to the wrong one. MEASURED on this database: of
# 156 held detail rows, most carry NO UWI at all (nothing to wait for), two
# tables wait on headers that ARE staged and held, and one names a well that was
# never staged anywhere. "Clear the parent first" is right for exactly one of
# those three and actively misleading for the other two -- the same failure as
# holding a well "for no coords" when the coords were never the reason.
_HOLD_NO_WELL_UWI = "no UWI on detail row"
_HOLD_WELL_HELD = "well header held"
_HOLD_WELL_MISSING = "well header never staged"
_HOLD_SEIS_UNNAMED = "no survey name"
_HOLD_SEIS_UNMAPPABLE = "no outline or bbox"


def _unresolved(col: str) -> str:
    return f"unresolved {col}"


# What clears each reason. Rendered by the UI so the fix is one click from the
# diagnosis instead of a hunt through four pages. Keep in step with the panel in
# page_workbench._tab_status.
CLEAR_ROUTE = {
    # NAME THE TAB THE PANEL IS ACTUALLY IN. Both of these said "Browse &
    # View" and the panel is in Run Pipeline — 23 Aug, Perry went looking for
    # the survey-name control where this text sent him and there was nothing
    # there. A remedy that points at the wrong page is worse than none: it
    # reads as "the feature does not exist".
    _HOLD_NO_UWI: ("Run Pipeline -> (4) Key Wells & Surveys -> "
                   "\"Wells (need UWI)\", or via the Excel round-trip."),
    _HOLD_NO_COORDS: ("Run coord enrich (gold, then dv_well). If the well is "
                      "genuinely new and coordless, the document must supply a "
                      "location -- the gate is not waived."),
    _HOLD_NO_WELL_UWI: ("These rows carry no UWI, so they are waiting on nothing. "
                        "Assign the file a UWI and re-capture, or reject it."),
    _HOLD_WELL_HELD: ("The well's own header is staged and held. Clear the "
                      "header's reason; these rows lift with it."),
    _HOLD_WELL_MISSING: ("The well is in neither dv_well nor cat_well -- no "
                         "header has ever been captured for it. Catalog the "
                         "document that carries the header, or reject these."),
    _HOLD_SEIS_UNNAMED: ("Run Pipeline -> (4) Key Wells & Surveys -> "
                         "\"Seismic (need survey)\". 2D lines usually share one "
                         "survey -- use \"Apply to all\" rather than typing it "
                         "per file."),
    _HOLD_SEIS_UNMAPPABLE: ("Arm a CRS and re-extract so the file gets a survey "
                            "outline or a complete bbox."),
}
CLEAR_ROUTE_REF = ("Seed the code into its dv_r_* table, or map it to an "
                   "existing code, in Promote -> reference FK review.")


@dataclass
class StatusResult:
    """`df` is the per-file grid; `holds` is per-file reason detail; `notes`
    carries anything that narrowed the answer, because a report that quietly
    covers less than it claims is worse than one that says so."""
    df: object = None
    holds: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def _promotable_mirrors(con, notes: list):
    """Every cat_* mirror promote can actually move, as (cat, dv).

    A mirror qualifies when it has BOTH a PROMOTED column (promote_fk_review's
    test for "promotable") and an INVENTORY_ID (without which a held row cannot
    be attributed to a file at all). One that has PROMOTED but no INVENTORY_ID
    is a real blind spot, so it is named in `notes` rather than dropped.

    'cat[_]%' is bracketed: `_` is a T-SQL LIKE wildcard and the bare form also
    matches catalog_setting.
    """
    from sqlalchemy import text as _t
    rows = con.execute(_t("""
        SELECT t.name,
               MAX(CASE WHEN c.name = 'PROMOTED'     THEN 1 ELSE 0 END) AS has_prom,
               MAX(CASE WHEN c.name = 'INVENTORY_ID' THEN 1 ELSE 0 END) AS has_inv
          FROM sys.tables t
          JOIN sys.schemas s ON s.schema_id = t.schema_id
          JOIN sys.columns c ON c.object_id = t.object_id
         WHERE s.name = :sc AND t.name LIKE 'cat[_]%'
         GROUP BY t.name
         ORDER BY t.name
    """), {"sc": CAT_SCHEMA}).fetchall()

    out = []
    for name, has_prom, has_inv in rows:
        if not has_prom:
            continue                       # not a promote target (e.g. a lookup)
        if not has_inv:
            notes.append(
                f"{CAT_SCHEMA}.{name} is promotable but has no INVENTORY_ID "
                f"column, so rows it holds cannot be attributed to a file and "
                f"are NOT counted here.")
            continue
        out.append((name, "dv_" + name[len("cat_"):]))
    return out


def _table_cols(con, schema: str, table: str) -> set:
    from sqlalchemy import text as _t
    rows = con.execute(_t(
        "SELECT c.name FROM sys.columns c "
        "JOIN sys.tables t ON t.object_id = c.object_id "
        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "WHERE s.name = :sc AND t.name = :t"), {"sc": schema, "t": table}).fetchall()
    return {r[0].lower() for r in rows}


def _ref_fks(con, dv_table: str):
    """Columns of dv_table that FK into a dv_r_* reference table.

    Delegates to promote_fk_review, which already holds the SQLAlchemy twin of
    promote_catalog._reference_fk_predicates' discovery query. A third spelling
    of this query is exactly how two reports come to disagree with nothing
    detecting it, so there is not one here.
    """
    from dataview.file_catalog.promote_fk_review import _ref_fks as _rf
    return _rf(con, dv_table)


# --------------------------------------------------------------------------- #
# the gates, per mirror
# --------------------------------------------------------------------------- #
def _mirror_holds(con, cat: str, dv: str, notes: list):
    """Rows of one mirror that promote would refuse, grouped by file.

    Returns {INVENTORY_ID: {reason: rows}} plus {INVENTORY_ID: total_unpromoted}.

    One query, one GROUP BY, one SUM per gate -- set-based per the SQL-Express
    doctrine. Empty mirrors short-circuit on a TOP 1 probe, the same fast path
    promote_table takes, because most mirrors are empty on any given run.
    """
    from sqlalchemy import text as _t
    from dataview.file_catalog.promote_catalog import _norm, REQUIRE_WELL_COORDS

    holds: dict = {}
    totals: dict = {}

    if con.execute(_t(f"SELECT TOP 1 1 FROM {CAT_SCHEMA}.[{cat}] "
                      f"WHERE PROMOTED = 0")).scalar() is None:
        return holds, totals

    cat_cols = _table_cols(con, CAT_SCHEMA, cat)
    dv_cols = _table_cols(con, DV_SCHEMA, dv)
    if not dv_cols:
        notes.append(f"{CAT_SCHEMA}.{cat} has no {DV_SCHEMA}.{dv} target; its "
                     f"unpromoted rows are counted as held with no reason.")

    is_header = dv.lower() == "dv_well"
    preds, labels = [], []

    def _add(label, pred):
        preds.append(pred)
        labels.append(label)

    # --- gate 1: a header row with no UWI never promotes -------------------- #
    # _promote_header's base predicate. Detail rows are not tested for this:
    # their gate is the dv_well EXISTS below, which a blank UWI also fails, and
    # reporting both would double-count one row against two reasons.
    if "uwi" in cat_cols:
        if is_header:
            _add(_HOLD_NO_UWI, "NULLIF(LTRIM(RTRIM(m.UWI)),'') IS NULL")
        else:
            # --- gate 4: detail rows wait for their well ---------------------
            # _promote_detail's base_where is ONE predicate (NOT EXISTS in
            # dv_well). It is split three ways here because the repair differs;
            # the three are mutually exclusive and their sum is exactly the rows
            # promote refuses, so nothing is double-counted or lost.
            _blank = "NULLIF(LTRIM(RTRIM(m.UWI)),'') IS NULL"
            _in_dv = (f"EXISTS (SELECT 1 FROM {DV_SCHEMA}.dv_well w "
                      f"WHERE w.uwi = {_norm('m.UWI')})")
            _add(_HOLD_NO_WELL_UWI, _blank)
            if con.execute(_t("SELECT CASE WHEN OBJECT_ID("
                              "'file_catalog.cat_well') IS NOT NULL "
                              "THEN 1 ELSE 0 END")).scalar() == 1:
                _in_cat = (
                    f"EXISTS (SELECT 1 FROM {CAT_SCHEMA}.cat_well h "
                    f"WHERE h.PROMOTED = 0 AND {_norm('h.UWI')} = {_norm('m.UWI')})")
                _add(_HOLD_WELL_HELD,
                     f"(NOT {_blank} AND NOT {_in_dv} AND {_in_cat})")
                _add(_HOLD_WELL_MISSING,
                     f"(NOT {_blank} AND NOT {_in_dv} AND NOT {_in_cat})")
            else:
                # No header mirror at all, so "held parent" is not a state this
                # database can be in -- every non-blank miss is a missing header.
                _add(_HOLD_WELL_MISSING, f"(NOT {_blank} AND NOT {_in_dv})")

    # --- gate 2: governance -- an unmappable well is held ------------------- #
    # Imported flag, not a copy: if REQUIRE_WELL_COORDS is ever turned off,
    # this report stops claiming a hold promote would no longer apply.
    if is_header and REQUIRE_WELL_COORDS:
        if "surface_latitude" in cat_cols and "surface_longitude" in cat_cols:
            # ONE ROW, ONE REASON. The UWI test is part of _promote_header's
            # `base` and the coord test is appended to it, so a blank-UWI row is
            # already refused before coordinates are ever considered -- flagging
            # it here too would attribute one row to two reasons and overstate
            # both. MEASURED: cat_well held 4 rows, all coordless, one of them
            # UWI-less; promote reports "held 3 (no coords)" because it counts
            # DISTINCT UWI over rows that HAVE one. Excluding the blank here is
            # what makes the two agree.
            _add(_HOLD_NO_COORDS,
                 "(NULLIF(LTRIM(RTRIM(m.UWI)),'') IS NOT NULL "
                 "AND (m.[surface_latitude] IS NULL "
                 "OR m.[surface_longitude] IS NULL))")

    # --- gate 3: unresolved reference vocabulary ---------------------------- #
    # Guarded only where the column exists on BOTH sides, which is promote's
    # `shared` intersection -- a dv_ column the mirror does not carry is never
    # promoted and so is never a hold.
    for local_col, ref_table, ref_col in _ref_fks(con, dv):
        if local_col.lower() not in cat_cols:
            continue
        _add(_unresolved(local_col),
             f"NOT (m.[{local_col}] IS NULL OR EXISTS "
             f"(SELECT 1 FROM {DV_SCHEMA}.[{ref_table}] r "
             f"WHERE r.[{ref_col}] = m.[{local_col}]))")

    # TWO LEVELS, NOT ONE -- T-SQL forbids an aggregate over an expression that
    # contains a subquery (msg 130), and three of these six gates ARE subqueries
    # (the dv_well EXISTS and every dv_r_* EXISTS). Flagging per row in a derived
    # table and summing the flags outside is the legal shape; folding it back to
    # one level fails on exactly the mirrors that matter (cat_well, every detail
    # table) while the empty ones still pass, so it looks like it works.
    flags = ", ".join(f"CASE WHEN {p} THEN 1 ELSE 0 END AS f{i}"
                      for i, p in enumerate(preds))
    sums_sql = ", ".join(f"SUM(q.f{i}) AS h{i}" for i in range(len(preds)))
    sql = (f"SELECT q.INVENTORY_ID, COUNT(*) AS n"
           + (", " + sums_sql if sums_sql else "")
           + f" FROM (SELECT m.INVENTORY_ID"
           + (", " + flags if flags else "")
           + f" FROM {CAT_SCHEMA}.[{cat}] m WHERE m.PROMOTED = 0) q "
             f"GROUP BY q.INVENTORY_ID")

    for row in con.execute(_t(sql)).fetchall():
        m = row._mapping
        inv = m["INVENTORY_ID"]
        if inv is None:
            # Unattributable rows are real backlog but belong to no file. Report
            # the fact rather than letting them vanish from the totals.
            notes.append(f"{CAT_SCHEMA}.{cat} holds {int(m['n'] or 0):,} "
                         f"unpromoted row(s) with a NULL INVENTORY_ID -- no file "
                         f"owns them; they cannot be cleared from this page.")
            continue
        totals[inv] = totals.get(inv, 0) + int(m["n"] or 0)
        for i, label in enumerate(labels):
            n = int(m[f"h{i}"] or 0)
            if n:
                holds.setdefault(inv, {})
                holds[inv][label] = holds[inv].get(label, 0) + n
    return holds, totals


def _seismic_holds(con, notes: list):
    """Seismic promotes from FILE_SEIS_HEADER on survey name, not from a cat_
    mirror, so its two gates (promote_seismic._NAMED / ._MAPPABLE) need their own
    pass or every stuck SEG-Y reads as 'staged, no reason'."""
    from sqlalchemy import text as _t
    holds: dict = {}
    if con.execute(_t(
            "SELECT CASE WHEN OBJECT_ID('file_catalog.FILE_SEIS_HEADER') IS NOT NULL "
            "AND OBJECT_ID('dataview.dv_seis_set') IS NOT NULL THEN 1 ELSE 0 END"
    )).scalar() != 1:
        return holds

    rows = con.execute(_t("""
        SELECT s.INVENTORY_ID,
               SUM(CASE WHEN NULLIF(LTRIM(RTRIM(s.SURVEY_NAME)),'') IS NULL
                        THEN 1 ELSE 0 END) AS unnamed,
               SUM(CASE WHEN NOT (NULLIF(LTRIM(RTRIM(s.SURVEY_OUTLINE)),'') IS NOT NULL
                        OR (s.BBOX_MIN_LAT IS NOT NULL AND s.BBOX_MAX_LAT IS NOT NULL
                            AND s.BBOX_MIN_LON IS NOT NULL AND s.BBOX_MAX_LON IS NOT NULL))
                        THEN 1 ELSE 0 END) AS unmappable
          FROM file_catalog.FILE_SEIS_HEADER s
         WHERE s.INVENTORY_ID IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM dataview.dv_seis_set ss
                           WHERE ss.seis_set_name = s.SURVEY_NAME)
         GROUP BY s.INVENTORY_ID
    """)).fetchall()
    for inv, unnamed, unmappable in rows:
        d = {}
        if unnamed:
            d[_HOLD_SEIS_UNNAMED] = int(unnamed)
        if unmappable:
            d[_HOLD_SEIS_UNMAPPABLE] = int(unmappable)
        if d:
            holds[inv] = d
    return holds


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #
def file_status(engine, root=None, this_crawl=False, state=None, limit=None,
                log=print) -> StatusResult:
    """Per-file state and hold reason for the whole catalog.

    root / this_crawl scope it the same way promotion_lineage.file_detail does.
    `state` filters to one state AFTER derivation (filtering before would change
    which rows the gates see).
    """
    import pandas as pd
    from sqlalchemy import text as _t
    from dataview.file_catalog import promotion_lineage as _lin

    res = StatusResult(notes=[])
    notes = res.notes

    with engine.connect() as con:
        # ── the dv_ side: LINEAGE, so this agrees with every report ─────────
        avail = _lin.available(con)
        seis_ok = _lin.seismic_ok(con)

        # ── the cat_ side: a sweep, so an unpaired mirror cannot hold rows
        #    invisibly ────────────────────────────────────────────────────────
        mirrors = _promotable_mirrors(con, notes)

        holds: dict = {}
        cat_rows: dict = {}
        for cat, dv in mirrors:
            try:
                h, t = _mirror_holds(con, cat, dv, notes)
            except Exception as e:
                # Never swallow: a mirror this cannot read is a mirror whose
                # backlog is missing from the answer, and the caller must know.
                notes.append(f"{CAT_SCHEMA}.{cat} could not be evaluated "
                             f"({type(e).__name__}: {e}) -- its held rows are "
                             f"NOT in this report.")
                continue
            for inv, d in h.items():
                for label, n in d.items():
                    holds.setdefault(inv, {})
                    holds[inv][label] = holds[inv].get(label, 0) + n
            for inv, n in t.items():
                cat_rows[inv] = cat_rows.get(inv, 0) + n

        try:
            for inv, d in _seismic_holds(con, notes).items():
                holds.setdefault(inv, {})
                holds[inv].update(d)
        except Exception as e:
            notes.append(f"seismic gates could not be evaluated "
                         f"({type(e).__name__}: {e}).")

        # ── promoted: INVENTORY_ID present in any dv_ lineage table, or the
        #    file's well already in dv_well, or its survey merged. Same three
        #    tests catalog_readiness._CASE uses, so the two cannot disagree. ──
        dv_counts: dict = {}
        for _cat, dv, _label in avail:
            try:
                for inv, n in con.execute(_t(
                        f"SELECT INVENTORY_ID, COUNT(*) FROM {DV_SCHEMA}.[{dv}] "
                        f"WHERE INVENTORY_ID IS NOT NULL GROUP BY INVENTORY_ID")).fetchall():
                    dv_counts[inv] = dv_counts.get(inv, 0) + int(n or 0)
            except Exception as e:
                notes.append(f"{DV_SCHEMA}.{dv} could not be counted "
                             f"({type(e).__name__}: {e}) -- files promoted only "
                             f"into it may read as not promoted.")

        seis_promoted = set()
        if seis_ok:
            seis_promoted = {r[0] for r in con.execute(_t("""
                SELECT DISTINCT sh.INVENTORY_ID
                  FROM file_catalog.FILE_SEIS_HEADER sh
                  JOIN dataview.dv_seis_set ss ON ss.seis_set_name = sh.SURVEY_NAME
                 WHERE sh.INVENTORY_ID IS NOT NULL""")).fetchall()}

        # ── the file list ───────────────────────────────────────────────────
        where, params = ["1=1"], {}
        if root:
            where.append("g.ROOT_PATH = :root")
            params["root"] = root
        if this_crawl:
            where.append("CAST(g.SCAN_DATE AS date) = CAST(GETDATE() AS date)")
        files = con.execute(_t(f"""
            SELECT g.INVENTORY_ID, g.FILE_NAME, g.FILE_PATH,
                   ISNULL(NULLIF(g.FILE_EXT,''),'(none)') AS ext,
                   g.FILE_TYPE_GROUP, g.FILE_SIZE_KB,
                   g.CATALOG_READINESS, g.HEADER_EXTRACTED,
                   NULLIF(LTRIM(RTRIM(g.MATCHED_UWI)),'') AS uwi,
                   g.CATALOG_ISSUES, g.PROMOTED_AT, g.VAULTED_AT
              FROM {GFC} g
             WHERE {' AND '.join(where)}
             ORDER BY g.FILE_NAME
        """), params).fetchall()

        # A well already in dv_well means the header landed even when no detail
        # table carries the file's id (header-only documents).
        dv_well_uwis = set()
        try:
            dv_well_uwis = {r[0] for r in con.execute(_t(
                "SELECT uwi FROM dataview.dv_well WHERE uwi IS NOT NULL")).fetchall()}
        except Exception as e:
            notes.append(f"dv_well could not be read ({type(e).__name__}: {e}) "
                         f"-- header-only files may read as not promoted.")

    def _pad14(u):
        if not u:
            return None
        s = "".join(ch for ch in str(u) if ch not in "- /").strip()
        return (s + "0" * 14)[:14] if s else None

    rows = []
    for r in files:
        m = r._mapping
        inv = m["INVENTORY_ID"]
        hx = m["HEADER_EXTRACTED"]
        readiness = (m["CATALOG_READINESS"] or "").strip().upper()

        n_cat = cat_rows.get(inv, 0)
        n_dv = dv_counts.get(inv, 0)
        hold = holds.get(inv, {})
        promoted = bool(n_dv) or inv in seis_promoted or (
            _pad14(m["uwi"]) in dv_well_uwis if m["uwi"] else False)

        # Precedence. SKIPPED first -- the same order catalog_readiness._CASE
        # uses, so a rejected file cannot read as pending work.
        if readiness == "SKIPPED":
            state_v = ST_SKIPPED
        elif hold:
            state_v = ST_HELD
        elif n_cat:
            state_v = ST_STAGED
        elif promoted:
            state_v = ST_PROMOTED
        elif hx == "M":
            state_v = ST_MOVED
        elif hx == "E":
            state_v = ST_ERROR
        elif hx == "Y":
            state_v = ST_EXTRACTED
        else:
            state_v = ST_INVENTORIED

        reason = "; ".join(f"{k} ({v:,})" for k, v in
                           sorted(hold.items(), key=lambda kv: -kv[1]))
        rows.append({
            "file": m["FILE_NAME"],
            "state": state_v,
            "reason": reason,
            "ext": m["ext"],
            "type": m["FILE_TYPE_GROUP"] or "",
            "uwi": m["uwi"] or "",
            "rows_cat": n_cat,
            "rows_dv": n_dv,
            "readiness": m["CATALOG_READINESS"] or "",
            "issues": (m["CATALOG_ISSUES"] or "")[:300],
            "promoted_at": m["PROMOTED_AT"],
            "vaulted_at": m["VAULTED_AT"],
            "inventory_id": inv,
            "size_kb": m["FILE_SIZE_KB"],
            "path": m["FILE_PATH"] or "",
        })

    df = pd.DataFrame(rows, columns=[
        "file", "state", "reason", "ext", "type", "uwi", "rows_cat", "rows_dv",
        "readiness", "issues", "promoted_at", "vaulted_at", "inventory_id",
        "size_kb", "path"])
    if state and not df.empty:
        df = df[df["state"] == state]
    if limit and not df.empty:
        df = df.head(int(limit))

    res.df = df
    res.holds = holds
    for n in notes:
        log(f"[status] {n}")
    return res


def reason_summary(res: StatusResult):
    """Held files and rows per distinct reason, worst first -- the view that says
    which single fix clears the most backlog."""
    import pandas as pd
    agg: dict = {}
    for inv, d in res.holds.items():
        for label, n in d.items():
            a = agg.setdefault(label, {"files": 0, "rows": 0})
            a["files"] += 1
            a["rows"] += n
    out = [{"reason": k, "files": v["files"], "rows": v["rows"],
            "clears_by": (CLEAR_ROUTE_REF if k.startswith("unresolved ")
                          else CLEAR_ROUTE.get(k, ""))}
           for k, v in agg.items()]
    return (pd.DataFrame(out).sort_values(["rows", "files"], ascending=False)
            .reset_index(drop=True)
            if out else pd.DataFrame(columns=["reason", "files", "rows", "clears_by"]))


# --------------------------------------------------------------------------- #
# Repair: supply what the gate is asking for
# --------------------------------------------------------------------------- #
# THE GATE IS NEVER WAIVED. Each of these supplies the missing DATA, then
# promote re-evaluates exactly as before. A file drains because it now satisfies
# the gate, not because the gate stopped being applied.
#
# Three writes, because the backlog has three shapes:
#   set_uwi     -- the file has staged rows with no UWI. Assignment alone does
#                  not reach them: _assign_uwi writes GLOBAL_FILE_CATALOG and
#                  clears CAPTURED_HASH so a LATER capture re-stages them. That
#                  leaves the rows already on disk untouched, which is why a
#                  file could be assigned a UWI and stay held forever.
#   set_coords  -- the header row exists and is coordless.
#   mint_header -- detail rows name a well that has NO header anywhere. Nothing
#                  else in the pipeline can produce one: capture writes what the
#                  document contained, and the document contained no header. A
#                  UWI plus a location is exactly and only what dv_well needs.
#
# Everything is scoped to (INVENTORY_ID, PROMOTED = 0) so a repair can only
# touch the rows this file is holding, never a promoted row and never another
# file's. Hand-supplied values are stamped MANUAL / long_lat_source='MANUAL' so
# they are never mistaken for extracted ones.

MANUAL = "MANUAL"
_CATALOG_SOURCE = "CATALOG"     # matches promote_catalog._CATALOG_SOURCE


def normalize_uwi(raw):
    """Canonicalize to bare-14, or None if it cannot be. Returning None rather
    than a padded/truncated string is deliberate: a malformed key written here
    fails the dv_well FK later, far from its cause."""
    try:
        from dataview.core import path_identity as _pi
        u = _pi.norm_uwi14(str(raw or "").strip())
    except Exception:
        u = "".join(ch for ch in str(raw or "") if ch.isalnum())
    u = (u or "")[:14]
    return u if len(u) == 14 else None


def validate_coords(lat, lon):
    """(lat, lon, error). Refuses (0,0) — Null Island is the classic silent
    wrong answer, and a wrong coordinate plots, exports and gets quoted."""
    if lat in (None, "") and lon in (None, ""):
        return None, None, None
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None, "latitude/longitude must be numbers"
    if not (-90.0 <= la <= 90.0):
        return None, None, f"latitude {la} out of range (-90..90)"
    if not (-180.0 <= lo <= 180.0):
        return None, None, f"longitude {lo} out of range (-180..180)"
    if la == 0 and lo == 0:
        return None, None, "(0, 0) refused — that is Null Island, not a location"
    return la, lo, None


# Columns a minted header supplies itself, beyond uwi/coords/provenance.
# active_ind is the one that matters: dv_well.active_ind is NOT NULL, and while
# it has a default of 'Y', promote INSERTs an explicit column list -- an explicit
# NULL beats a default. A minted row without it fails the dv_well insert, and
# because promote_table wraps the whole header pass, that ONE bad row takes down
# the promote for EVERY header, including the ones that only needed coordinates.
# Measured in simulation before this shipped: 4 good headers blocked by 4 bad.
# The extractor writes 'Y' here, so this matches what a captured header carries.
_MINT_DEFAULTS = {"active_ind": "Y"}


def _mint_unsatisfied(con):
    """dv_well columns that are NOT NULL, exist in cat_well (so promote will
    carry them), and that a minted header would leave NULL.

    Generic rather than a second hardcoded list: promote copies the INTERSECTION
    of dv_well and cat_well columns, so any future NOT NULL addition silently
    re-creates the failure above. This turns that into a named error at preview
    time instead of a promote crash later.
    """
    from sqlalchemy import text as _t
    rows = con.execute(_t("""
        SELECT c.name
          FROM sys.columns c
          JOIN sys.tables t  ON t.object_id = c.object_id
          JOIN sys.schemas s ON s.schema_id = t.schema_id
         WHERE s.name = :ds AND t.name = 'dv_well' AND c.is_nullable = 0
           AND c.is_identity = 0 AND c.is_computed = 0
           AND EXISTS (SELECT 1 FROM sys.columns c2
                        JOIN sys.tables t2  ON t2.object_id = c2.object_id
                        JOIN sys.schemas s2 ON s2.schema_id = t2.schema_id
                       WHERE s2.name = :cs AND t2.name = 'cat_well'
                         AND c2.name = c.name)
    """), {"ds": DV_SCHEMA, "cs": CAT_SCHEMA}).fetchall()
    supplied = ({"uwi", "surface_latitude", "surface_longitude", "source",
                 "long_lat_source", "row_created_by", "row_created_date"}
                | set(_MINT_DEFAULTS))
    return [r[0] for r in rows if r[0].lower() not in supplied]


def _uwi_mirrors(con):
    """cat_* mirrors carrying BOTH INVENTORY_ID and uwi — the rows a UWI fix
    must reach. A sweep, not LINEAGE: leaving one out leaves rows held."""
    from sqlalchemy import text as _t
    rows = con.execute(_t("""
        SELECT t.name FROM sys.tables t
          JOIN sys.schemas s ON s.schema_id = t.schema_id
         WHERE s.name = :sc AND t.name LIKE 'cat[_]%'
           AND EXISTS (SELECT 1 FROM sys.columns c
                       WHERE c.object_id = t.object_id AND c.name = 'INVENTORY_ID')
           AND EXISTS (SELECT 1 FROM sys.columns c
                       WHERE c.object_id = t.object_id AND c.name = 'uwi')
         ORDER BY t.name"""), {"sc": CAT_SCHEMA}).fetchall()
    return [r[0] for r in rows]


def plan_fix(con, inv_id, new_uwi=None, lat=None, lon=None):
    """What applying this edit would write, and what would still block the file.

    Describes the WRITES exactly; it does not predict the final state, because
    re-deriving after Apply answers that by construction rather than by a second
    model of the gates that could drift from the first.
    """
    from sqlalchemy import text as _t
    from dataview.file_catalog.promote_catalog import _norm

    out = {"actions": [], "errors": [], "notes": []}

    uwi14 = None
    if new_uwi not in (None, ""):
        uwi14 = normalize_uwi(new_uwi)
        if not uwi14:
            out["errors"].append(f"'{new_uwi}' is not a valid 14-character UWI")
    la, lo, cerr = validate_coords(lat, lon)
    if cerr:
        out["errors"].append(cerr)
    if out["errors"]:
        return out

    gfc_uwi = con.execute(_t(
        "SELECT NULLIF(LTRIM(RTRIM(MATCHED_UWI)),'') FROM " + GFC +
        " WHERE INVENTORY_ID = :i"), {"i": inv_id}).scalar()
    eff_uwi = uwi14 or normalize_uwi(gfc_uwi)

    # 1) UWI onto staged rows that have none.
    if uwi14:
        n_rows = 0
        for m in _uwi_mirrors(con):
            n_rows += con.execute(_t(
                f"SELECT COUNT(*) FROM {CAT_SCHEMA}.[{m}] WHERE INVENTORY_ID = :i "
                f"AND PROMOTED = 0 AND NULLIF(LTRIM(RTRIM(uwi)),'') IS NULL"),
                {"i": inv_id}).scalar() or 0
        out["actions"].append(
            f"set UWI {uwi14} on GLOBAL_FILE_CATALOG and {n_rows:,} staged row(s)")

    # 2) Coordinates onto an existing coordless header.
    n_hdr = con.execute(_t(
        f"SELECT COUNT(*) FROM {CAT_SCHEMA}.cat_well WHERE INVENTORY_ID = :i "
        f"AND PROMOTED = 0"), {"i": inv_id}).scalar() or 0
    if la is not None and n_hdr:
        out["actions"].append(
            f"set surface coordinates ({la}, {lo}) on {n_hdr} header row(s)")

    # 3) Mint a header when the detail rows have no well to attach to.
    if eff_uwi:
        in_dv = con.execute(_t(
            "SELECT COUNT(*) FROM dataview.dv_well WHERE uwi = :u"),
            {"u": eff_uwi}).scalar() or 0
        n_detail = 0
        for m in _uwi_mirrors(con):
            if m.lower() == "cat_well":
                continue
            n_detail += con.execute(_t(
                f"SELECT COUNT(*) FROM {CAT_SCHEMA}.[{m}] WHERE INVENTORY_ID = :i "
                f"AND PROMOTED = 0"), {"i": inv_id}).scalar() or 0
        if n_detail and not in_dv and not n_hdr:
            if la is not None:
                _missing = _mint_unsatisfied(con)
                if _missing:
                    out["errors"].append(
                        "cannot create a header: dv_well requires "
                        + ", ".join(_missing) + " with no value available")
                else:
                    out["actions"].append(
                        f"create a header for {eff_uwi} at ({la}, {lo}) so its "
                        f"{n_detail:,} detail row(s) have a well to attach to")
            else:
                out["notes"].append(
                    f"{n_detail:,} detail row(s) name well {eff_uwi}, which has "
                    f"no header anywhere — supply lat/long here too, or these "
                    f"stay held")
    elif la is not None and not n_hdr:
        out["notes"].append("coordinates given but this file has no header row "
                            "and no UWI — supply a UWI as well")

    if not out["actions"] and not out["errors"]:
        out["notes"].append("nothing to write for this file")
    return out


def apply_fix(con, inv_id, new_uwi=None, lat=None, lon=None, source_path=None):
    """Perform the writes plan_fix described. Caller owns the transaction.

    Returns {"uwi_rows": n, "coord_rows": n, "header_created": bool}.
    """
    from sqlalchemy import text as _t

    done = {"uwi_rows": 0, "coord_rows": 0, "header_created": False}
    uwi14 = normalize_uwi(new_uwi) if new_uwi not in (None, "") else None
    la, lo, cerr = validate_coords(lat, lon)
    if cerr:
        raise ValueError(cerr)
    if new_uwi not in (None, "") and not uwi14:
        raise ValueError(f"'{new_uwi}' is not a valid 14-character UWI")

    # 1) UWI — catalog row first, then every staged row that lacks one.
    #    CAPTURED_HASH is cleared for the same reason _assign_uwi clears it:
    #    capture skips a file whose stamp matches its content hash, and
    #    assigning a UWI does not change the file.
    if uwi14:
        con.execute(_t(f"""
            UPDATE {GFC} SET MATCHED_UWI = :u, UWI14 = :u,
                   MATCH_METHOD = :mm, CAPTURED_HASH = NULL,
                   ROW_CHANGED_DATE = GETUTCDATE()
             WHERE INVENTORY_ID = :i"""), {"u": uwi14, "mm": MANUAL, "i": inv_id})
        for m in _uwi_mirrors(con):
            r = con.execute(_t(
                f"UPDATE {CAT_SCHEMA}.[{m}] SET uwi = :u "
                f"WHERE INVENTORY_ID = :i AND PROMOTED = 0 "
                f"AND NULLIF(LTRIM(RTRIM(uwi)),'') IS NULL"),
                {"u": uwi14, "i": inv_id})
            done["uwi_rows"] += r.rowcount or 0

    eff_uwi = uwi14 or normalize_uwi(con.execute(_t(
        "SELECT MATCHED_UWI FROM " + GFC + " WHERE INVENTORY_ID = :i"),
        {"i": inv_id}).scalar())

    # 2) Coordinates onto the existing header, only where unset — the same
    #    predicate _fill_cat_coords_from_gold uses, so a real coordinate is
    #    never overwritten by a hand-typed one.
    if la is not None:
        r = con.execute(_t(f"""
            UPDATE {CAT_SCHEMA}.cat_well
               SET surface_latitude = :la, surface_longitude = :lo,
                   long_lat_source = :mm, row_changed_by = :mm,
                   row_changed_date = GETUTCDATE()
             WHERE INVENTORY_ID = :i AND PROMOTED = 0
               AND (surface_latitude IS NULL OR surface_longitude IS NULL
                    OR (surface_latitude = 0 AND surface_longitude = 0))"""),
            {"la": la, "lo": lo, "mm": MANUAL, "i": inv_id})
        done["coord_rows"] = r.rowcount or 0

    # 3) Mint a header when detail rows have no well. Guarded three ways: the
    #    file must have no header of its own, the UWI must not already be in
    #    dv_well, and there must actually be detail rows waiting.
    if eff_uwi and la is not None:
        n_hdr = con.execute(_t(
            f"SELECT COUNT(*) FROM {CAT_SCHEMA}.cat_well WHERE INVENTORY_ID = :i"),
            {"i": inv_id}).scalar() or 0
        in_dv = con.execute(_t(
            "SELECT COUNT(*) FROM dataview.dv_well WHERE uwi = :u"),
            {"u": eff_uwi}).scalar() or 0
        n_detail = 0
        for m in _uwi_mirrors(con):
            if m.lower() == "cat_well":
                continue
            n_detail += con.execute(_t(
                f"SELECT COUNT(*) FROM {CAT_SCHEMA}.[{m}] "
                f"WHERE INVENTORY_ID = :i AND PROMOTED = 0"),
                {"i": inv_id}).scalar() or 0
        if n_detail and not n_hdr and not in_dv:
            _missing = _mint_unsatisfied(con)
            if _missing:
                raise ValueError(
                    "cannot create a header: dv_well requires "
                    + ", ".join(_missing)
                    + " and a minted row has no value for them. Add them to "
                      "catalog_status._MINT_DEFAULTS.")
            _extra = list(_MINT_DEFAULTS)
            _cols = ", ".join(f"[{c}]" for c in _extra)
            _vals = ", ".join(f":d_{c}" for c in _extra)
            _p = {f"d_{c}": v for c, v in _MINT_DEFAULTS.items()}
            _p.update({"u": eff_uwi, "la": la, "lo": lo,
                       "src": _CATALOG_SOURCE, "mm": MANUAL, "i": inv_id,
                       "sp": (source_path or "")[:900]})
            con.execute(_t(f"""
                INSERT INTO {CAT_SCHEMA}.cat_well
                    (uwi, surface_latitude, surface_longitude, source,
                     long_lat_source, INVENTORY_ID, SOURCE_PATH,
                     row_created_by, row_created_date,
                     PROMOTED, CAPTURED_AT, {_cols})
                VALUES (:u, :la, :lo, :src, :mm, :i, :sp,
                        :mm, GETUTCDATE(), 0, GETUTCDATE(), {_vals})"""), _p)
            done["header_created"] = True

    return done


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Per-file catalog status with the reason promote is holding it")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--root")
    ap.add_argument("--this-crawl", action="store_true")
    ap.add_argument("--state", help="INVENTORIED|EXTRACTED|STAGED|HELD|PROMOTED|"
                                    "SKIPPED|MOVED|ERROR")
    ap.add_argument("--held-only", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--csv")
    a = ap.parse_args()

    from dataview.core.schema_introspect import make_engine
    eng = make_engine(a.server, a.database, "ODBC Driver 17 for SQL Server")

    res = file_status(eng, root=a.root, this_crawl=a.this_crawl,
                      state=(ST_HELD if a.held_only else a.state),
                      limit=a.limit)
    df = res.df
    if df.empty:
        print("-- no files in scope")
        return 0

    print(f"\n-- {len(df):,} file(s)")
    print(df["state"].value_counts().to_string())

    rs = reason_summary(res)
    if not rs.empty:
        print("\n-- held by reason (worst first) --")
        print(rs[["reason", "files", "rows"]].to_string(index=False))

    if a.csv:
        df.to_csv(a.csv, index=False)
        print(f"\n-- written to {a.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
