r"""Seed DATA parents from the reference master -- the one implementation.

Phase 4 seeds ENTITY and REFERENCE parents because those are legitimately
created from the value itself: an operator named "TRIGOOD OIL COMPANY" IS that
row. It refuses DATA parents, and that refusal is right -- seeding dv_well from
a child's foreign key would mint a well that is nothing but a number: no name,
no location, no depth, existing only to satisfy a constraint.

This module is the case that refusal does not cover. It does not conjure a well
from the orphan key; it copies a DESCRIBED well out of
WELL_REF.well_ref.well_master_gold. Different act, different source, so it gets
its own name rather than loosening `can_add`.

BOTH CALLERS COME HERE. tools/seed_wells_from_master.py and the Phase 4 grid
share this module rather than each growing their own copy -- six times in one
week a capability already existed and a worse parallel version was built beside
it, which is the first thing CLAUDE.md says.

WHAT IT WILL NOT DO
  * invent. Every value is one the master states; a column the master leaves
    NULL stays NULL. A confident wrong coordinate plots, exports, and gets
    quoted -- a missing one is visible.
  * register a reference value. dv_well.source is FK-constrained to
    dv_r_source, and creating a domain value arms a guard elsewhere, so an
    unregistered code is refused rather than seeded.
  * overwrite. NOT EXISTS-guarded: whichever load owns a well keeps it, the
    same first-one-in-wins rule promote follows.
"""
MASTER = "WELL_REF.well_ref.well_master_gold"

# The staged children that key on dv_well.uwi, and the column each keys with.
# Deliberately a literal list, not an INFORMATION_SCHEMA sweep: this is the set
# whose parents we are willing to seed, and widening it should be a visible
# edit rather than a side effect of some table gaining a uwi column.
CHILDREN = [("stg.dv_well_formation_top", "API_NUMBER"),
            ("stg.dv_prod_entity", "UWI"),
            ("stg.dv_well_dir_srvy_hdr", "UWI"),
            ("stg.dv_well_dir_srvy_sta", "UWI")]

CREATED_BY = "SEED_FROM_WELL_REF"

# THE ROW'S OWN KEY, not the parent's. Counting repeats of the PARENT key says
# 41,983 of 42,059 stations are duplicates, because a well legitimately has
# thousands of stations -- a confidently wrong number of exactly the kind this
# panel exists to prevent. A table absent here reports duplicates as None
# (unknown) rather than a guess.
NATURAL_KEY = {
    "stg.dv_well_formation_top":  ["API_NUMBER", "UNIT_CODE"],
    "stg.dv_prod_entity":         ["PROD_ENTITY_ID"],
    "stg.dv_well_dir_srvy_hdr":   ["UWI", "SURVEY_ID"],
    "stg.dv_well_dir_srvy_sta":   ["UWI", "SURVEY_ID", "STATION_ID"],
}


# NEVER WRAP THE INDEXED COLUMN. LTRIM(RTRIM(col)) makes a predicate
# non-sargable, and the master is 4,031,052 rows: measured 31.844s wrapped
# against 0.029s bare, a factor of 1,100. It broke correctness too, not just
# speed -- the Phase 4 grid ran this per render, and a lookup that did not
# finish came back empty, which DISABLED the checkboxes it was meant to enable.
#
# Dropping the trim is safe on both sides: the values are already padded to 14
# by pad_sql, and uwi14 / dv_well.uwi are char(14), which SQL Server compares
# trailing-space-insensitively. The wrapped and bare forms match the same rows.
# Trim the VALUE if you must; never the COLUMN.
def pad_sql(expr):
    """The UWI-14 pad, the transform promote applies at the write point.

    BOTH sides of every comparison in this module use it. Padding one side only
    is the most repeated mistake in this repo -- it made a perfect load report
    -1317, and an FK clause inert for six weeks before that.
    """
    return (f"CASE WHEN NULLIF(LTRIM(RTRIM({expr})), '') IS NULL THEN NULL "
            f"ELSE LEFT(CONCAT(LTRIM(RTRIM({expr})), REPLICATE('0', 14)), 14) END")


def orphan_uwis(conn):
    """Wells the staged children reference that dataview.dv_well does not have."""
    import sqlalchemy as sa
    parts = [f"SELECT DISTINCT {pad_sql(col)} AS u FROM {tbl}" for tbl, col in CHILDREN]
    sql = (f"SELECT x.u FROM ({' UNION '.join(parts)}) x "
           f"WHERE x.u IS NOT NULL "
           f"AND NOT EXISTS (SELECT 1 FROM dataview.dv_well w "
           f"WHERE w.uwi = x.u) ORDER BY x.u")
    return [r[0] for r in conn.execute(sa.text(sql))]


def master_rows(conn, uwis):
    """[{uwi, well_name, ...}] for the given wells, as the master states them.

    Returns only what the master HAS. A uwi absent from the result is one the
    master cannot describe, and the caller must leave it held rather than
    invent a row for it.
    """
    import sqlalchemy as sa
    if not uwis:
        return []
    inlist = ",".join("'" + str(u).replace("'", "''") + "'" for u in uwis)
    rows = conn.execute(sa.text(
        f"SELECT g.uwi14, g.well_name, g.operator_name, g.field_name, "
        f"       g.surface_latitude, g.surface_longitude, g.county, "
        f"       g.province_state, g.total_depth, g.spud_date "
        f"  FROM {MASTER} g "
        f" WHERE g.uwi14 IN ({inlist})"))
    keys = ("uwi", "well_name", "operator", "field", "surface_latitude",
            "surface_longitude", "county", "province_state", "total_depth",
            "spud_date")
    return [dict(zip(keys, (r[0].strip(),) + tuple(r[1:]))) for r in rows]


def validate_source(conn, code):
    """(ok, registered) -- is this dv_r_source code real?

    An unregistered code would be held by promote downstream, so it is refused
    here where the message can still name the alternatives.
    """
    import sqlalchemy as sa
    registered = [r[0] for r in conn.execute(sa.text(
        "SELECT source FROM dataview.dv_r_source ORDER BY source"))]
    return (code in registered), registered


def unblocked_counts(conn, uwis):
    """{staging_table: rows} that would promote once these wells exist."""
    import sqlalchemy as sa
    if not uwis:
        return {t: 0 for t, _ in CHILDREN}
    inlist = ",".join("'" + str(u).replace("'", "''") + "'" for u in uwis)
    out = {}
    for tbl, col in CHILDREN:
        out[tbl] = conn.execute(sa.text(
            f"SELECT COUNT(*) FROM {tbl} s "
            f"WHERE {pad_sql('s.[' + col + ']')} IN ({inlist})")).scalar() or 0
    return out


def seed(engine, rows, source=None, created_by=CREATED_BY):
    """Insert the given master rows into dv_well.

    Returns (inserted, already_present). BOTH numbers, because "0" alone is a
    lie of omission: a second Apply reported "Seeded 0 well(s)" after a first
    one had seeded all 55, which reads as failure and is success. The loader's
    own verify panel already draws this distinction -- "0 (already there)"
    versus a fresh count -- and this owes the operator the same.

    NOT EXISTS-guarded per row, so re-running seeds nothing and a well another
    load already owns is left alone: first one in wins.
    """
    import sqlalchemy as sa
    if not rows:
        return 0, 0
    with engine.connect() as cx:
        live = {r[0].lower() for r in cx.execute(sa.text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='dataview' AND TABLE_NAME='dv_well'"))}
    cand = ["uwi", "well_name", "operator", "field", "surface_latitude",
            "surface_longitude", "county", "province_state", "total_depth",
            "spud_date", "active_ind", "row_created_by"]
    use = [c for c in cand if c in live]
    if source and "source" in live:
        use.append("source")
    stamp = ", row_created_date" if "row_created_date" in live else ""
    stampv = ", SYSUTCDATETIME()" if "row_created_date" in live else ""
    # Counted BEFORE the insert: afterwards every row exists and the two
    # outcomes are indistinguishable.
    with engine.connect() as cx:
        present = cx.execute(sa.text(
            "SELECT COUNT(*) FROM dataview.dv_well WHERE uwi IN ("
            + ",".join("'" + str(r["uwi"]).replace("'", "''") + "'" for r in rows)
            + ")")).scalar() or 0

    n = 0
    with engine.begin() as cx:
        for r in rows:
            vals = dict(r)
            vals["active_ind"] = "Y"
            vals["row_created_by"] = created_by
            vals["source"] = source
            ph = ", ".join(f":{c}" for c in use)
            n += cx.execute(sa.text(
                f"INSERT INTO dataview.dv_well ({', '.join(use)}{stamp}) "
                f"SELECT {ph}{stampv} WHERE NOT EXISTS "
                f"(SELECT 1 FROM dataview.dv_well w WHERE w.uwi = :uwi)"),
                {k: vals.get(k) for k in set(use) | {"uwi"}}).rowcount or 0
    return n, present


# ─────────────────────────── the backlog ──────────────────────────────────
# WHAT IS STUCK, WHY, AND THE ONE THING THAT CLEARS IT.
#
# Phases 3-6 only render when session state still holds the batch, so a
# Streamlit restart hides every hold while the rows sit in stg untouched --
# the data persists and the view does not. This reads the DATABASE, so it
# survives a restart, and it pairs each blocker with its remedy: a backlog you
# can only look at teaches you to ignore it (Perry, 24 Aug: "just knowing data
# is being held is not of much use unless you can optionally do something to
# clear the backlog").
#
# Scoped to the dv_well parent on purpose. That is the blocker this codebase
# actually meets, and a generic FK sweep here would need the batch's column
# map -- which is the session state this exists to survive without.
def backlog(conn):
    """[{table, staged, held, recoverable, duplicates, key}] per staging table.

    held        -- rows whose well is not in dv_well. They promote by
                   themselves once it is, so this is a queue, not an error.
    recoverable -- of those, how many the reference master can describe. The
                   difference between the two is what nothing can fix here.
    duplicates  -- rows the promote dedupe will discard because they repeat a
                   key. Reported because ROW_NUMBER discarding them silently is
                   how 5,655 formation tops disappeared.
    """
    import sqlalchemy as sa
    out = []
    for tbl, col in CHILDREN:
        try:
            staged = conn.execute(sa.text(f"SELECT COUNT(*) FROM {tbl}")).scalar() or 0
        except Exception:
            continue                      # table not staged in this database
        if not staged:
            continue
        k = pad_sql("s.[" + col + "]")
        held_uwis = [r[0] for r in conn.execute(sa.text(
            f"SELECT DISTINCT {k} AS u FROM {tbl} s "
            f"WHERE {k} IS NOT NULL AND NOT EXISTS "
            f"(SELECT 1 FROM dataview.dv_well w WHERE w.uwi = {k})"))]
        held = conn.execute(sa.text(
            f"SELECT COUNT(*) FROM {tbl} s WHERE NOT EXISTS "
            f"(SELECT 1 FROM dataview.dv_well w WHERE w.uwi = {k})")).scalar() or 0
        rec = len(master_rows(conn, held_uwis)) if held_uwis else 0
        nk = NATURAL_KEY.get(tbl)
        dups = None
        if nk:
            expr = ", '|', ".join(
                f"ISNULL(CAST(s.[{c2}] AS nvarchar(400)), '')" for c2 in nk)
            dups = conn.execute(sa.text(
                f"SELECT COUNT(*) - COUNT(DISTINCT CONCAT({expr}, '')) "
                f"FROM {tbl} s")).scalar() or 0
            dups = max(0, dups)
        out.append({"table": tbl, "key": col, "staged": staged, "held": held,
                    "held_wells": len(held_uwis), "recoverable_wells": rec,
                    "duplicates": dups, "natural_key": nk})
    return out


def null_parent_link(engine, table, col, uwis):
    """Blank the parent key in staging for these wells, so the rows promote.

    THE LAST RESORT, AND IT IS A REAL DECISION. The row loads with no link to a
    well, which is honest for a measurement whose well genuinely is not in any
    source -- and wrong for one whose well simply has not been loaded yet.
    Offered only for wells the reference master cannot describe, because every
    other case has a better answer one button to the left.
    """
    import sqlalchemy as sa
    if not uwis:
        return 0
    inlist = ",".join("'" + str(u).replace("'", "''") + "'" for u in uwis)
    k = pad_sql("[" + col + "]")
    with engine.begin() as cx:
        return cx.execute(sa.text(
            f"UPDATE {table} SET [{col}] = NULL WHERE {k} IN ({inlist})")).rowcount or 0
