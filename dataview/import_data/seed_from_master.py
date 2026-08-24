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
           f"WHERE LTRIM(RTRIM(w.uwi)) = x.u) ORDER BY x.u")
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
        f" WHERE LTRIM(RTRIM(g.uwi14)) IN ({inlist})"))
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
    """Insert the given master rows into dv_well. Returns rows written.

    NOT EXISTS-guarded per row, so re-running seeds nothing and a well another
    load already owns is left alone.
    """
    import sqlalchemy as sa
    if not rows:
        return 0
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
                f"(SELECT 1 FROM dataview.dv_well w "
                f" WHERE LTRIM(RTRIM(w.uwi)) = :uwi)"),
                {k: vals.get(k) for k in set(use) | {"uwi"}}).rowcount or 0
    return n
