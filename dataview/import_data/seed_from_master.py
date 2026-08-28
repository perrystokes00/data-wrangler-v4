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

# ── what gets copied: master expression -> dv_well column ──────────────────
# ONE PLACE, and it is keyed by the DV_WELL NAME. The previous list was keyed
# by the master's names and then filtered with `if c in live` against
# dv_well's columns, so "operator", "field" and "total_depth" -- which
# dv_well spells operator_name, field_name and final_td -- were dropped
# SILENTLY. Three columns the master states, never arriving, with nothing
# reporting it. Aliasing in the SELECT makes the two sides agree by
# construction instead of by coincidence.
#
# well_type / well_status prefer the standardised column and fall back to the
# raw one: std_* is NULL for every Wyoming row while raw_* is fully populated,
# so reading only std_* copies nothing. Whichever arrives is then checked
# against the reference table -- see sanitise_fk.
MASTER_TO_DV = (
    ("g.uwi14",                                     "uwi"),
    ("g.well_name",                                 "well_name"),
    ("g.well_num",                                  "well_num"),
    ("g.api_10",                                    "api_num"),
    ("g.operator_name",                             "operator_name"),
    ("g.field_name",                                "field_name"),
    ("g.county",                                    "county"),
    ("g.province_state",                            "province_state"),
    ("g.country",                                   "country"),
    ("g.surface_latitude",                          "surface_latitude"),
    ("g.surface_longitude",                         "surface_longitude"),
    ("g.bottom_hole_latitude",                      "bottom_hole_latitude"),
    ("g.bottom_hole_longitude",                     "bottom_hole_longitude"),
    ("g.total_depth",                               "final_td"),
    ("g.kb_elevation",                              "kb_elevation"),
    ("g.ground_elevation",                          "ground_elevation"),
    ("g.elevation_ouom",                            "elevation_ouom"),
    ("g.spud_date",                                 "spud_date"),
    ("g.completion_date",                           "completion_date"),
    ("g.abandonment_date",                          "abandonment_date"),
    ("g.formation_at_td",                           "formation_at_td"),
    ("g.producing_formation",                       "producing_formation"),
    ("g.lease_name",                                "lease_name"),
    ("g.well_profile_type",                         "well_profile_type"),
    ("g.long_lat_source",                           "long_lat_source"),
    ("COALESCE(g.std_well_type, g.raw_well_type)",  "well_type"),
    ("COALESCE(g.std_well_status, g.raw_well_status)", "well_status"),
    # The master computes these from the same coordinates it hands over, so
    # they cannot disagree with the point. h3_refresh is still the authority
    # for wells loaded any other way.
    ("g.h3_r4", "h3_r4"), ("g.h3_r5", "h3_r5"),
    ("g.h3_r6", "h3_r6"), ("g.h3_r7", "h3_r7"),
)

# dv_well column -> (reference table, its column). A value the reference table
# does not hold is set to NULL rather than inserted: the FK would reject the
# row outright, and inventing a registration to make it fit ARMS A GUARD for
# every other loader (CLAUDE.md: creating a reference value is its own
# decision). Missing is visible; wrong is not.
FK_GUARDED = {
    "well_type":   ("dataview.dv_r_well_type",   "well_type"),
    "well_status": ("dataview.dv_r_well_status", "well_status"),
    "source":      ("dataview.dv_r_source",      "source"),
}

# The staged children that key on dv_well.uwi, and the column each keys with.
# Deliberately a literal list, not an INFORMATION_SCHEMA sweep: this is the set
# whose parents we are willing to seed, and widening it should be a visible
# edit rather than a side effect of some table gaining a uwi column.
CHILDREN = [("stg.dv_well_formation_top", "API_NUMBER"),
            ("stg.dv_prod_entity", "UWI"),
            ("stg.dv_well_dir_srvy_hdr", "UWI"),
            ("stg.dv_well_dir_srvy_sta", "UWI")]

CREATED_BY = "SEED_FROM_WELL_REF"

# dv_r_source code stamped on a seeded well. Registered by Perry 27 Aug 2026.
#
# WHY IT MATTERS MORE THAN IT LOOKS. These wells went in with source NULL, and
# every query filter keyed on source then returned nothing for them -- 5,480
# wells present, correctly keyed, inside the scope, and invisible. The report
# was "none of the wells matched", which reads as a UWI failure and is not
# one: UWI matching was sound (14 chars both sides, no duplicates, no prefix
# collisions). A row that came from somewhere should say so.
#
# STILL REFUSED IF UNREGISTERED. validate_source() is not skipped because this
# is a constant -- a database without the code registered gets a clear refusal
# rather than an FK error, which is the same rule the module applies to
# well_type and well_status.
SEED_SOURCE = "REF_WELLS"

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
    return _select(conn, f"g.uwi14 IN ({inlist})")


def _select(conn, where, limit=None, params=None):
    """Read MASTER_TO_DV columns, aliased to their dv_well names.

    Every caller comes through here so the orphan path and the scope path
    cannot copy different column sets -- which is the shape of bug this
    module's own docstring warns about.
    """
    import sqlalchemy as sa
    cols = ", ".join("%s AS [%s]" % (expr, name) for expr, name in MASTER_TO_DV)
    top = "TOP (%d) " % int(limit) if limit and int(limit) > 0 else ""
    # ORDERED WHENEVER IT IS CAPPED. TOP without ORDER BY returns an arbitrary
    # subset, so a truncated load would take a different 30,000 each time and
    # re-running could never finish the county. Ordered by uwi and combined
    # with only_new, repeated loads PAGE THROUGH it: each run takes the next
    # block of wells the database does not have yet.
    order = " ORDER BY g.uwi14" if top else ""
    rows = conn.execute(sa.text(
        f"SELECT {top}{cols} FROM {MASTER} g WHERE {where}{order}"),
        params or {})
    names = [name for _e, name in MASTER_TO_DV]
    out = []
    for r in rows:
        d = dict(zip(names, r))
        # char(14): the key is compared as text everywhere downstream.
        d["uwi"] = str(d.get("uwi") or "").strip()
        if d["uwi"]:
            out.append(d)
    return out


def scope_where(state=None, county=None, bbox=None):
    """(sql, params) for a state / county / bounding-box scope.

    BBOX IS (min_lat, max_lat, min_lon, max_lon) -- lats together, then lons.
    That is the order _qry_wells_in_bbox takes and the order the map stores in
    _active_drill_bbox, and this signature agreeing with them is not a detail:
    the codebase has already paid for a box stored in two different shapes
    (see _norm_bounds, "four bare numbers are not rejected by folium; they
    simply put the camera somewhere meaningless"). Latitude and longitude in
    Wyoming are both plausible-looking negatives and positives in the 40-110
    range, so the wrong order returns an EMPTY result rather than an error.
    """
    sql, p = ["1=1"], {}
    if state:
        sql.append("g.province_state = :st")
        p["st"] = state
    if county:
        # The master stores "NATRONA"; a UI usually offers "Natrona County".
        sql.append("UPPER(LTRIM(RTRIM(g.county))) = :co")
        p["co"] = str(county).upper().replace(" COUNTY", "").strip()
    if bbox:
        _mnla, _mxla, _mnlo, _mxlo = bbox
        sql.append("g.surface_latitude BETWEEN :mnla AND :mxla")
        sql.append("g.surface_longitude BETWEEN :mnlo AND :mxlo")
        p.update(mnla=_mnla, mxla=_mxla, mnlo=_mnlo, mxlo=_mxlo)
    # A well with no location cannot be plotted and cannot be checked, which
    # is the whole reason for copying it here rather than minting a key.
    sql.append("g.surface_latitude IS NOT NULL")
    sql.append("g.surface_longitude IS NOT NULL")
    return " AND ".join(sql), p


def scope_count(conn, state=None, county=None, bbox=None):
    """How many the scope holds, and how many are NOT already in dv_well."""
    import sqlalchemy as sa
    where, p = scope_where(state, county, bbox)
    total = conn.execute(sa.text(
        f"SELECT COUNT(*) FROM {MASTER} g WHERE {where}"), p).scalar() or 0
    new = conn.execute(sa.text(
        f"SELECT COUNT(*) FROM {MASTER} g WHERE {where} AND NOT EXISTS "
        f"(SELECT 1 FROM dataview.dv_well w WHERE w.uwi = g.uwi14)"),
        p).scalar() or 0
    return int(total), int(new)


def scope_rows(conn, state=None, county=None, bbox=None, limit=None,
               only_new=True):
    """The wells a scope would seed, as dv_well-keyed dicts.

    only_new skips those dv_well already holds -- not for correctness (the
    insert is NOT EXISTS-guarded either way) but so the preview count is the
    number of rows that will actually appear.
    """
    where, p = scope_where(state, county, bbox)
    if only_new:
        where += (" AND NOT EXISTS (SELECT 1 FROM dataview.dv_well w "
                  "WHERE w.uwi = g.uwi14)")
    return _select(conn, where, limit=limit, params=p)


def counties(conn, state):
    """[(county, n)] in the master for a state, most wells first."""
    import sqlalchemy as sa
    return [(str(r[0]).strip(), int(r[1])) for r in conn.execute(sa.text(
        f"SELECT LTRIM(RTRIM(g.county)) c, COUNT(*) n FROM {MASTER} g "
        f" WHERE g.province_state = :st AND g.county IS NOT NULL "
        f"   AND g.surface_latitude IS NOT NULL "
        f" GROUP BY LTRIM(RTRIM(g.county)) ORDER BY n DESC"), {"st": state})]


def write_csv(rows, path, source=SEED_SOURCE, created_by=CREATED_BY):
    """Write scope rows as a CSV the Bulk Tabular Loader can ingest.

    WHY A CSV AT ALL. seed() inserts row by row, each guarded by its own
    NOT EXISTS -- correct, and the right shape for the handful of orphans it
    was written for. For a county it is thousands of round trips, and
    CLAUDE.md's own measurement applies: pyodbc for statements, bcp for sets.
    The Bulk Tabular Loader already owns the set-based path, with mapping and
    FK resolution, so the fastest route is to hand it a file rather than to
    grow a second bulk loader here.

    THE HEADER IS dv_well's OWN COLUMN NAMES, which is what makes the loader
    map it without help -- the same shape as synth_data\\dv_well.csv.

    Plain csv.writer with default quoting, NOT the tab/QUOTE_NONE/escapechar
    combination path_identity exists to prevent: that one doubles every
    backslash and is the reason a file once took two identities. No column
    here carries a path, but the default dialect is correct anyway and
    choosing it deliberately is cheaper than explaining it later.

    Returns (path, n_rows, columns).
    """
    import csv as _csv
    cols = [name for _e, name in MASTER_TO_DV] + [
        "active_ind", "source", "row_created_by"]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            d = dict(r)
            d["active_ind"] = "Y"
            d["source"] = source
            d["row_created_by"] = created_by
            w.writerow(["" if d.get(c) is None else d.get(c) for c in cols])
    return path, len(rows), cols


def sanitise_fk(conn, rows):
    """NULL any FK-guarded value the reference table does not hold.

    Returns {column: {"nulled": n, "values": {value: n}}} so the caller can
    SAY what it dropped. Silently blanking a column the operator can see in
    the source is the same class of dishonesty as inventing one.

    The alternative -- letting the INSERT fail on the FK -- loses the whole
    row for one unregistered code, and the row's name, operator and location
    are worth having without its well type.
    """
    import sqlalchemy as sa
    report = {}
    for col, (table, refcol) in FK_GUARDED.items():
        try:
            ok = {str(r[0]).strip().upper() for r in conn.execute(
                sa.text(f"SELECT [{refcol}] FROM {table}"))}
        except Exception:
            continue                      # no reference table: nothing to check
        dropped = {}
        for r in rows:
            v = r.get(col)
            if v is None:
                continue
            if str(v).strip().upper() not in ok:
                dropped[str(v)] = dropped.get(str(v), 0) + 1
                r[col] = None
        if dropped:
            report[col] = {"nulled": sum(dropped.values()), "values": dropped}
    return report


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
    # Keyed by the DV_WELL name, so `c in live` narrows to what this database
    # actually has rather than quietly discarding a rename. The old list said
    # "operator", "field", "total_depth"; dv_well says operator_name,
    # field_name, final_td; all three failed the test and were dropped without
    # a word. Whatever MASTER_TO_DV carries is offered here.
    cand = [name for _e, name in MASTER_TO_DV] + ["active_ind", "row_created_by"]
    use = [c for c in cand if c in live]
    if source and "source" in live:
        use.append("source")
    stamp = ", row_created_date" if "row_created_date" in live else ""
    stampv = ", SYSUTCDATETIME()" if "row_created_date" in live else ""
    # Counted BEFORE the insert: afterwards every row exists and the two
    # outcomes are indistinguishable.
    #
    # CHUNKED. This built ONE IN-list from every row, which was fine for the
    # handful of orphans it was written for and is a ~200 KB literal for a
    # county of ten thousand -- the scope path this now also serves.
    present = 0
    _uwis = [str(r["uwi"]) for r in rows]
    with engine.connect() as cx:
        for _i in range(0, len(_uwis), 1000):
            _chunk = _uwis[_i:_i + 1000]
            present += cx.execute(sa.text(
                "SELECT COUNT(*) FROM dataview.dv_well WHERE uwi IN ("
                + ",".join("'" + u.replace("'", "''") + "'" for u in _chunk)
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
