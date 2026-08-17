"""
synonym_store.py — the column-level learning layer for DataView loading.

WHY THIS EXISTS (Perry, July 31): fingerprint recall is keyed to a whole
file's column shape — all-or-nothing, and worthless the moment a vendor
renames one header. Ten sources carrying the same ten kinds of data need
learning at the COLUMN level: teach it INCLINATION -> INCL once, from any
file, and every future file from every future vendor inherits it.

TWO TABLES (not synonym1/2/3 — learned synonyms are unbounded and each
needs its own provenance):

  dataview.dv_target_attribute   one row per TARGET COLUMN, refreshed from
                                 the LIVE schema: type, width, nullability.
                                 Enables a FIT PRE-FLIGHT — "period_date is
                                 nvarchar(7), your values are 10 chars" gets
                                 caught at plan time instead of at promote
                                 (the July-30 failure).

  dataview.dv_column_synonym     many rows per target column: every name a
                                 source might use for it. UNIQUE on
                                 (target_table, synonym_norm) — one meaning
                                 per name per table; ambiguity is a bug, not
                                 a judgement call.

TWO RULES BAKED IN:
  1. Synonyms are scoped PER TARGET TABLE. "DEPTH" means top_depth in
     formation tops and md in directional surveys. A global dictionary
     would be wrong in both places.
  2. Only VERIFIED loads teach. learn_from_load() is called after promote
     + verify, never from a proposal or a Save — a synonym learned from a
     mapping that later failed would poison every future file silently.

The curated pack below is a PROPOSAL. seed_synonyms() validates every row
against the live catalog and drops (with a report) any naming a column that
does not exist — the live schema is always the judge.
"""

import re
from datetime import datetime

try:
    from sqlalchemy import text
except Exception:                                    # import-time safety
    text = None


# ─────────────────────────── normalization ──────────────────────────────────
_UNIT_TOKENS = {"ft", "feet", "foot", "m", "meters", "metres", "mtr",
                "bbl", "bbls", "mcf", "mmcf", "scf", "boe", "deg", "degrees",
                "pct", "percent", "usft", "usfeet"}


def norm(s):
    """'API Number' -> 'apinumber'; 'MD (Feet)' -> 'md'; 'Depth_ft' ->
    'depth'. Case, spaces, underscores and punctuation stop mattering, and
    so do unit qualifiers — a vendor writing 'MD (Feet)' means MD, and no
    synonym list should have to enumerate every unit spelling."""
    s = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", str(s or ""))
    toks = [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t]
    if len(toks) > 1 and toks[-1] in _UNIT_TOKENS \
            and len("".join(toks[:-1])) >= 3:
        toks = toks[:-1]
    return "".join(toks)


# ───────────────────────── the curated seed pack ────────────────────────────
# {TABLE: {target_column: [synonyms...]}}. Validated against the live schema
# at seed time; unknown columns are dropped and reported.

_UWI = ["uwi", "api", "api_number", "api_no", "api_num", "apinum", "api10",
        "api_10", "api12", "api14", "api_14", "well_api", "api_well_number",
        "unique_well_id", "unique_well_identifier", "well_id", "wellid",
        "well_key", "idwell"]

# ── system / audit columns: never mappable from source data ────────────────
# Stamped by the loader (row_created_*, row_changed_*, active_ind, source) or
# derived by the app (geog, h3_*), so no vendor column should ever be routed
# to one — not by seed, not by match, not by learning. Perry, July 31:
# "Don't include system generated attributes for audit control."
_SYSTEM_COLS = {
    "row_created_by", "row_created_date", "row_changed_by", "row_changed_date",
    "active_ind", "source", "inventory_id",
}
_SYSTEM_PATTERNS = (
    re.compile(r"^h3_"),            # h3_r4..r7, h3_coord_hash
    re.compile(r"^geog"),           # geography(-1) built at promote
    re.compile(r"_hash$"),
)


def is_system_column(col):
    """True for audit/system/derived columns. `remark` is NOT one of these —
    a source comment column is legitimate content."""
    c = str(col or "").lower()
    return c in _SYSTEM_COLS or any(p.search(c) for p in _SYSTEM_PATTERNS)


SEED_PACK = {
    "DV_WELL": {
        "uwi": _UWI,
        "well_name": ["well_name", "wellname", "well", "name", "lease_name",
                      "lease", "common_name", "well_label", "wellbore_name",
                      "property_name"],
        "well_num": ["well_number", "well_no", "wellno", "well_num", "wn"],
        # KEY REPOINTED: this schema has operator_name (and operator_ba_id
        # for the resolved id). Named "operator" the seed was dropped, so
        # the single commonest vendor heading had no synonym at all.
        "operator_name": ["operator", "operator_name", "oper", "company",
                     "current_operator", "opco"],
        "operator_ba_id": ["operator_id", "operator_ba_id", "oper_id",
                           "company_id"],
        "field_name": ["field", "field_name", "fieldname", "field_desc"],
        "surface_latitude": ["latitude", "lat", "surface_latitude", "surf_lat",
                             "lat_dd", "dd_lat", "wgs84_lat", "y_lat"],
        "surface_longitude": ["longitude", "long", "lon", "lng",
                              "surface_longitude", "surf_long", "long_dd",
                              "dd_long", "wgs84_long", "x_long"],
        # DELIBERATELY LEFT POINTING AT A COLUMN THIS SCHEMA DOES NOT HAVE,
        # so seed_synonyms keeps dropping it and says so. These are PROJECTED
        # coordinates; the nearest live columns are surface_longitude /
        # surface_latitude, which are DEGREES. Repointing would file an
        # easting as a longitude — silently, plausibly, and wrongly. A
        # dropped seed is visible; a wrong one is not. If projected
        # coordinates need a home, they need their own columns and a CRS.
        "surface_x": ["x", "easting", "east", "surf_x", "surface_easting",
                      "x_coord", "xcoord"],
        "surface_y": ["y", "northing", "north", "surf_y", "surface_northing",
                      "y_coord", "ycoord"],
        "ground_elevation": ["ground_elevation", "ground_elev", "grd_elev",
                             "ge", "elev_ground", "gl", "ground_level"],
        "kb_elevation": ["kb", "kb_elevation", "kb_elev", "kelly_bushing",
                         "elev_kb", "kbe", "derrick_floor"],
        # KEY REPOINTED: dv_well calls it final_td.
        "final_td": ["total_depth", "td", "tot_depth", "td_md",
                        "total_measured_depth", "depth_total", "total_md"],
        "spud_date": ["spud_date", "spud", "spudded", "date_spud", "spud_dt",
                      "spud_day"],
        "completion_date": ["completion_date", "comp_date", "date_completed",
                            "completed", "compl_date", "completion"],
        "well_status": ["well_status", "status", "current_status",
                        "well_stat", "status_code"],
        "county": ["county", "county_name", "parish", "county_parish"],
        # KEY REPOINTED: dv_well calls it province_state (PPDM naming).
        "province_state": ["state", "state_name", "province", "state_prov",
                  "state_province"],
        "country": ["country", "country_name", "nation"],
        "basin": ["basin", "basin_name"],
    },
    "DV_WELL_FORMATION_TOP": {
        "uwi": _UWI,
        "strat_unit_name": ["formation", "formation_name", "fm", "fm_name",
                            "horizon", "horizon_name", "marker",
                            "marker_name", "zone", "zone_name", "unit",
                            "unit_name", "strat_unit", "strat_name",
                            "pick_name", "surface_name", "top_name"],
        "strat_unit_id": ["strat_unit_id", "unit_code", "formation_code",
                          "fm_code", "strat_code", "marker_code",
                          "zone_code", "horizon_code", "code"],
        "strat_unit_type": ["unit_type", "strat_unit_type", "formation_type",
                            "marker_type"],
        "top_depth": ["top_depth", "top", "depth", "md_top", "top_md",
                      "formation_top", "pick_depth", "depth_top",
                      "top_depth_md", "topmd"],
        "base_depth": ["base_depth", "base", "bottom", "base_md", "md_base",
                       "bottom_depth", "depth_base", "basemd"],
        "tvd_top": ["tvd_top", "top_tvd", "tvd"],
        "tvd_base": ["tvd_base", "base_tvd"],
        "gross_thickness": ["thickness", "gross_thickness", "isopach",
                            "gross_thick", "net_gross"],
        "lithology": ["lithology", "lith", "rock_type"],
        "interp_id": ["interp_id", "interpretation", "interp",
                      "interpretation_id"],
        "interpreter_ba_id": ["interpreter", "picked_by", "geologist",
                              "analyst"],
    },
    "DV_WELL_DIR_SRVY_HDR": {
        "uwi": _UWI,
        "survey_id": ["survey_id", "survey", "srvy_id", "survey_no",
                      "survey_name", "survey_number"],
        "survey_type": ["survey_type", "srvy_type", "type", "method",
                        "survey_method", "tool_type"],
        "survey_date": ["survey_date", "srvy_date", "date", "date_surveyed",
                        "run_date"],
        "survey_top_depth": ["survey_top_depth", "start_depth", "from_depth",
                             "survey_top", "top_depth", "depth_from"],
        "survey_base_depth": ["survey_base_depth", "end_depth", "to_depth",
                              "survey_base", "total_depth", "depth_to",
                              "final_depth"],
        "contractor_ba_id": ["contractor", "service_company", "surveyor",
                             "survey_company", "vendor_company"],
        "depth_datum": ["datum", "depth_datum", "reference_datum"],
        "depth_datum_elevation": ["datum_elevation", "datum_elev",
                                  "reference_elevation"],
    },
    "DV_WELL_DIR_SRVY_STA": {
        "uwi": _UWI,
        "survey_id": ["survey_id", "survey", "srvy_id", "survey_no",
                      "survey_name"],
        "station_id": ["station_id", "station", "station_no", "sta",
                       "station_number", "point", "survey_point", "seq",
                       "sequence", "row_no"],
        "md": ["md", "measured_depth", "depth", "meas_depth", "mdepth",
               "depth_md", "md_depth", "measureddepth", "course_depth"],
        "incl": ["incl", "inclination", "inc", "drift", "drift_angle",
                 "deviation", "dev_angle", "angle", "hole_angle", "inclin"],
        "azim": ["azim", "azimuth", "azi", "az", "bearing", "direction",
                 "hole_direction", "azimuth_grid", "azimuth_true"],
        "tvd": ["tvd", "true_vertical_depth", "tv_depth", "vert_depth",
                "vertical_depth"],
        "ns_offset": ["ns_offset", "ns", "north_south", "northing",
                      "north", "y_offset", "n_s", "ns_dist", "northsouth"],
        "ew_offset": ["ew_offset", "ew", "east_west", "easting", "east",
                      "x_offset", "e_w", "ew_dist", "eastwest"],
        "dls": ["dls", "dogleg", "dogleg_severity", "dog_leg", "dog_leg_sev"],
        "surface_latitude": ["latitude", "lat", "surface_latitude"],
        "surface_longitude": ["longitude", "long", "lon", "surface_longitude"],
    },
    "DV_PROD_ENTITY": {
        "uwi": _UWI,
        "prod_entity_id": ["prod_entity_id", "entity_id", "entity",
                           "producing_entity", "completion_id", "prod_id",
                           "production_entity"],
        "prod_entity_name": ["prod_entity_name", "entity_name", "name",
                             "well_name", "lease_name", "property_name"],
        "prod_entity_type": ["prod_entity_type", "entity_type", "type",
                             "producing_entity_type"],
        "first_prod_date": ["first_prod_date", "first_prod",
                            "first_production", "on_production", "onprod",
                            "first_prod_dt", "start_production"],
        "last_prod_date": ["last_prod_date", "last_prod", "last_production",
                           "final_prod", "end_production"],
        "primary_fluid": ["primary_fluid", "main_product", "primary_product",
                          "well_type"],
        "field_id": ["field", "field_id", "field_name", "field_code"],
    },
    "DV_PROD_VOLUME": {
        "prod_entity_id": ["prod_entity_id", "entity_id", "entity",
                           "producing_entity", "completion_id", "prod_id"],
        "period_date": ["period_date", "date", "period", "prod_date",
                        "production_date", "month", "prod_month",
                        "production_month", "report_date", "prod_period",
                        "year_month"],
        "fluid_type": ["fluid_type", "fluid", "product", "phase",
                       "commodity", "product_type", "product_code"],
        "volume": ["volume", "vol", "quantity", "qty", "amount",
                   "production", "prod_volume", "prod_qty", "net_volume"],
        "volume_ouom": ["volume_uom", "vol_uom", "volume_ouom", "uom",
                        "units", "measure", "volume_unit"],
        "days_on_prod": ["days_on_prod", "days", "days_produced", "days_on",
                         "prod_days", "producing_days", "day_prod",
                         "days_online"],
        "avg_daily_rate": ["avg_daily_rate", "daily_rate", "rate",
                           "avg_rate", "average_rate", "bopd", "mcfd"],
    },
    "DV_WELL_LOG": {
        "uwi": _UWI,
        # ALSO LEFT DROPPED: dv_well_log has log_type, which is a CODED
        # value, not a name. Mapping a free-text log name onto a coded column
        # is the same wrong-not-missing trade as surface_x above.
        "log_name": ["log_name", "logname", "curve_set", "run_name",
                     "log_type", "service"],
        # KEY REPOINTED: dv_well_log calls it top_depth.
        "top_depth": ["top_depth", "start_depth", "log_top", "strt",
                          "depth_from"],
        # KEY REPOINTED: dv_well_log calls it base_depth.
        "base_depth": ["base_depth", "stop_depth", "log_base", "stop",
                           "depth_to"],
        "log_date": ["log_date", "date", "run_date", "logged_date"],
        # KEY REPOINTED: dv_well_log calls it run_num.
        "run_num": ["run", "run_number", "run_no"],
    },
}


# ────────────────────────────── DDL / setup ─────────────────────────────────
_DDL_ATTR = """
IF OBJECT_ID('{schema}.dv_target_attribute') IS NULL
CREATE TABLE {schema}.dv_target_attribute (
    target_table      nvarchar(128) NOT NULL,
    target_column     nvarchar(128) NOT NULL,
    ordinal           int           NULL,
    data_type         nvarchar(64)  NULL,
    max_len           int           NULL,
    num_precision     int           NULL,
    num_scale         int           NULL,
    is_nullable       nchar(1)      NULL,
    refreshed_date    datetime2     NOT NULL,
    CONSTRAINT pk_dv_target_attribute PRIMARY KEY (target_table, target_column)
)"""

_DDL_SYN = """
IF OBJECT_ID('{schema}.dv_column_synonym') IS NULL
CREATE TABLE {schema}.dv_column_synonym (
    target_table      nvarchar(128) NOT NULL,
    synonym_norm      nvarchar(256) NOT NULL,
    target_column     nvarchar(128) NOT NULL,
    synonym           nvarchar(256) NOT NULL,
    source            nvarchar(32)  NOT NULL,   -- seed | verified_load | operator
    confidence        decimal(3,2)  NOT NULL,
    hit_count         int           NOT NULL,
    active_ind        nchar(1)      NOT NULL,
    row_created_by    nvarchar(64)  NOT NULL,
    row_created_date  datetime2     NOT NULL,
    CONSTRAINT pk_dv_column_synonym PRIMARY KEY (target_table, synonym_norm)
)"""


def ensure_store(engine, schema="dataview"):
    """Create both tables if they do not exist. Idempotent."""
    with engine.begin() as cx:
        cx.execute(text(_DDL_ATTR.format(schema=schema)))
        cx.execute(text(_DDL_SYN.format(schema=schema)))
    return True


def refresh_attributes(engine, schema="dataview", tables=None):
    """Re-read the LIVE schema into dv_target_attribute in ONE statement.

    Was a MERGE per column — thousands of round trips that looked like a
    hang (July 31). Set-based, per Perry's standing rule. INFORMATION_SCHEMA
    is the only source of truth for type/width/nullability; nothing here
    guesses. When refreshing everything, columns that no longer exist are
    deleted so the store cannot go stale against the database."""
    params = {"schema": schema}
    filt = ""
    if tables:
        names = [t.lower() for t in tables]
        marks = ", ".join(f":t{i}" for i in range(len(names)))
        filt = f" AND LOWER(c.TABLE_NAME) IN ({marks})"
        params.update({f"t{i}": n for i, n in enumerate(names)})
    prune = "" if tables else "\n        WHEN NOT MATCHED BY SOURCE THEN DELETE"
    sql = f"""
    MERGE {schema}.dv_target_attribute AS t
    USING (
        SELECT UPPER(c.TABLE_NAME)  AS target_table,
               LOWER(c.COLUMN_NAME) AS target_column,
               c.ORDINAL_POSITION, c.DATA_TYPE,
               c.CHARACTER_MAXIMUM_LENGTH, c.NUMERIC_PRECISION,
               c.NUMERIC_SCALE,
               CASE WHEN c.IS_NULLABLE = 'YES' THEN 'Y' ELSE 'N' END AS nul
        FROM INFORMATION_SCHEMA.COLUMNS c
        WHERE c.TABLE_SCHEMA = :schema{filt}
    ) AS s
       ON t.target_table = s.target_table
      AND t.target_column = s.target_column
    WHEN MATCHED THEN UPDATE SET
         ordinal = s.ORDINAL_POSITION, data_type = s.DATA_TYPE,
         max_len = s.CHARACTER_MAXIMUM_LENGTH,
         num_precision = s.NUMERIC_PRECISION, num_scale = s.NUMERIC_SCALE,
         is_nullable = s.nul, refreshed_date = SYSUTCDATETIME()
    WHEN NOT MATCHED BY TARGET THEN INSERT
         (target_table, target_column, ordinal, data_type, max_len,
          num_precision, num_scale, is_nullable, refreshed_date)
         VALUES (s.target_table, s.target_column, s.ORDINAL_POSITION,
                 s.DATA_TYPE, s.CHARACTER_MAXIMUM_LENGTH,
                 s.NUMERIC_PRECISION, s.NUMERIC_SCALE, s.nul,
                 SYSUTCDATETIME()){prune};
    """
    with engine.begin() as cx:
        # Fail fast rather than wait forever: an interrupted earlier run can
        # leave an open transaction holding an exclusive lock, and a silent
        # infinite wait looks exactly like a hang (July 31). 20s then a
        # readable error naming the cause.
        cx.execute(text("SET LOCK_TIMEOUT 20000"))
        try:
            cx.execute(text(sql), params)
        except Exception as e:
            if "1222" in str(e) or "lock request time out" in str(e).lower():
                raise RuntimeError(
                    "dv_target_attribute is LOCKED by another session — "
                    "usually an interrupted earlier run whose transaction is "
                    "still open. In SSMS: SELECT request_session_id FROM "
                    "sys.dm_tran_locks WHERE resource_associated_entity_id = "
                    "OBJECT_ID('dataview.dv_target_attribute'); then KILL "
                    "that session and re-run.") from e
            raise
        n = cx.execute(text(
            f"SELECT COUNT(*) FROM {schema}.dv_target_attribute"),
            {}).scalar()
    return int(n or 0)


def live_columns(engine, schema="dataview", table=None):
    """{TABLE: {column, ...}} straight from dv_target_attribute."""
    sql = f"SELECT target_table, target_column FROM {schema}.dv_target_attribute"
    params = {}
    if table:
        sql += " WHERE target_table = :t"
        params["t"] = table.upper()
    out = {}
    with engine.connect() as cx:
        for tt, tc in cx.execute(text(sql), params).fetchall():
            out.setdefault(str(tt).upper(), set()).add(str(tc).lower())
    return out


# ───────────────────────────── seeding synonyms ─────────────────────────────
def validate_pack(pack, catalog):
    """Pure: (rows, dropped, conflicts) for a pack against {TABLE:{cols}}.

    rows      = [(TABLE, column, synonym, synonym_norm)]
    dropped   = [(TABLE, column, 'no such column in the live schema')]
    conflicts = [(TABLE, synonym_norm, kept_column, rejected_column)]
    """
    rows, dropped, conflicts = [], [], []
    for tbl, cols in pack.items():
        T = tbl.upper()
        live = catalog.get(T)
        if live is None:
            dropped.append((T, "*", "no such table in the live schema"))
            continue
        claimed = {}
        # a column's own real name is always a synonym for itself
        ordered = list(cols.items())
        for col, syns in ordered:
            c = col.lower()
            if c not in live:
                dropped.append((T, c, "no such column in the live schema"))
                continue
            if is_system_column(c):
                dropped.append((T, c, "system/audit column — stamped by the "
                                      "loader, never mapped from source"))
                continue
            for s in [col] + list(syns):
                sn = norm(s)
                if not sn:
                    continue
                if sn in claimed:
                    if claimed[sn] != c:
                        conflicts.append((T, sn, claimed[sn], c))
                    continue
                claimed[sn] = c
                rows.append((T, c, str(s), sn))
    return rows, dropped, conflicts


def seed_synonyms(engine, schema="dataview", pack=None, by="SEED"):
    """Insert the curated pack, validated against the live catalog. Existing
    rows are never overwritten — an operator or learned mapping outranks a
    seed. Returns a report dict."""
    pack = pack or SEED_PACK
    catalog = live_columns(engine, schema)
    if not catalog:
        raise RuntimeError("dv_target_attribute is empty — run "
                           "refresh_attributes() first")
    rows, dropped, conflicts = validate_pack(pack, catalog)
    inserted = 0
    with engine.begin() as cx:
        cx.execute(text("SET LOCK_TIMEOUT 20000"))
        # batched VALUES constructor — one statement per ~200 synonyms
        # instead of one per synonym (set-based rule again)
        B = 200
        for i in range(0, len(rows), B):
            chunk = rows[i:i + B]
            vals = ", ".join(f"(:t{j}, :n{j}, :c{j}, :s{j})"
                             for j in range(len(chunk)))
            p = {"by": by}
            for j, (T, col, syn_txt, sn) in enumerate(chunk):
                p[f"t{j}"], p[f"n{j}"] = T, sn
                p[f"c{j}"], p[f"s{j}"] = col, syn_txt
            res = cx.execute(text(f"""
                INSERT INTO {schema}.dv_column_synonym
                    (target_table, synonym_norm, target_column, synonym,
                     source, confidence, hit_count, active_ind,
                     row_created_by, row_created_date)
                SELECT v.tt, v.sn, v.tc, v.sy, 'seed', 0.80, 0, 'Y', :by,
                       SYSUTCDATETIME()
                FROM (VALUES {vals}) AS v(tt, sn, tc, sy)
                WHERE NOT EXISTS (
                    SELECT 1 FROM {schema}.dv_column_synonym x
                    WHERE x.target_table = v.tt AND x.synonym_norm = v.sn)
            """), p)
            inserted += (res.rowcount or 0)
    return {"candidates": len(rows), "inserted": inserted,
            "dropped": dropped, "conflicts": conflicts}


def synonyms_for(engine, schema, table):
    """{synonym_norm: target_column} for one table."""
    with engine.connect() as cx:
        return {str(a).lower(): str(b).lower() for a, b in cx.execute(text(f"""
            SELECT synonym_norm, target_column FROM {schema}.dv_column_synonym
            WHERE target_table = :t AND active_ind = 'Y'"""),
            {"t": table.upper()}).fetchall()}


# ─────────────────────────── matching (pure core) ───────────────────────────
def match_columns(source_cols, live_cols, syn_map):
    """Pure matcher. Returns (mapping, unmatched, notes).

    Order: exact column name (normalized) -> synonym -> unmatched.
    One target column may only be claimed ONCE; a second claimant is left
    unmatched and reported, because silently overwriting a mapped column is
    how bad loads happen.
    """
    live_by_norm = {norm(c): c.lower() for c in live_cols
                    if not is_system_column(c)}
    mapping, unmatched, notes = {}, [], []
    taken = {}
    for src in source_cols:
        sn = norm(src)
        tgt = live_by_norm.get(sn)
        how = "exact"
        if not tgt:
            tgt = syn_map.get(sn)
            how = "synonym"
        if not tgt:
            unmatched.append(src)
            continue
        if tgt in taken:
            unmatched.append(src)
            notes.append(f"'{src}' also matches {tgt}, already taken by "
                         f"'{taken[tgt]}' — left unmapped for your call")
            continue
        taken[tgt] = src
        mapping[src] = tgt
        if how == "synonym":
            notes.append(f"'{src}' -> {tgt} (synonym)")
    return mapping, unmatched, notes


def suggest_map(engine, schema, table, source_cols):
    """DB-backed match_columns."""
    live = live_columns(engine, schema, table).get(table.upper(), set())
    return match_columns(source_cols, live, synonyms_for(engine, schema, table))


# ───────────────────────── fit pre-flight (pure core) ───────────────────────
_CHAR_TYPES = {"char", "nchar", "varchar", "nvarchar", "text", "ntext"}
_NUM_TYPES = {"int", "bigint", "smallint", "tinyint", "decimal", "numeric",
              "float", "real", "money", "smallmoney"}
_DATE_TYPES = {"date", "datetime", "datetime2", "smalldatetime",
               "datetimeoffset"}


_IDENT_LIKE = ("uwi", "_id", "id_", "api")


def _as_loaded(tgt, v):
    """The value as PROMOTE will write it, not as the file holds it.

    build_promote_sql strips '-', ' ' and '.' from identifier columns and
    right-pads `uwi` to 14. Measuring the raw text blocked a load that would
    have succeeded: '42-329-10001-0000' is 17 chars in the file and exactly
    14 after the transform (Perry, July 31).
    """
    t = str(tgt or "").lower()
    if t == "uwi":
        d = v.replace("-", "").replace(" ", "").replace(".", "").strip()
        return "" if not d else (d + "0" * 14)[:14]
    if t.endswith("_id") or t.startswith("id_") or t == "api_num":
        return v.replace("-", "").replace(" ", "").replace(".", "").strip()
    return v


def fit_issues(attrs, mapping, rows, unmapped_required=True):
    """Pure pre-flight. `attrs` = {column: {data_type, max_len, is_nullable}},
    `mapping` = {source_col: target_col}, `rows` = list of dicts (a sample or
    the whole file).

    Catches BEFORE staging what promote would otherwise catch after:
      · values wider than the target column   (the period_date nvarchar(7) bug)
      · non-numeric text heading for a numeric column
      · unparseable dates heading for a date column
      · blanks heading for a NOT NULL column
      · NOT NULL target columns with no source at all
    Returns [(severity, column, message)] — 'error' blocks, 'warn' informs.
    """
    out = []
    inv = {t: s for s, t in mapping.items()}
    for tgt, src in inv.items():
        a = attrs.get(tgt) or {}
        dt = str(a.get("data_type") or "").lower()
        ml = a.get("max_len")
        nullable = str(a.get("is_nullable") or "Y").upper() != "N"
        raw = [("" if r.get(src) is None else str(r.get(src)).strip())
               for r in rows]
        vals = [_as_loaded(tgt, v) for v in raw]
        # promote TRUNCATES an over-long uwi with LEFT(...,14) rather than
        # failing, so it never trips the width check — but silently dropping
        # digits off a well identifier is data corruption and must be said
        # out loud (July 31).
        if str(tgt).lower() == "uwi":
            _cut = [v for v in raw
                    if len(v.replace("-", "").replace(" ", "")
                            .replace(".", "").strip()) > 14]
            if _cut:
                out.append(("warn", tgt,
                            f"{len(_cut)} value(s) are longer than 14 "
                            f"characters after separators are removed and "
                            f"will be TRUNCATED — e.g. '{_cut[0][:30]}'"))
        nonblank = [v for v in vals if v != ""]
        if dt in _CHAR_TYPES and ml and ml > 0:
            longest = max((len(v) for v in nonblank), default=0)
            if longest > ml:
                sample = next((v for v in nonblank if len(v) > ml), "")
                out.append(("error", tgt,
                            f"values up to {longest} chars but {tgt} is "
                            f"{dt}({ml}) — e.g. '{sample[:40]}'"))
        if dt in _NUM_TYPES:
            bad = [v for v in nonblank
                   if not re.fullmatch(r"[-+]?[\d,]*\.?\d+([eE][-+]?\d+)?", v)]
            if bad:
                out.append(("error", tgt,
                            f"{len(bad)} non-numeric value(s) for numeric "
                            f"{tgt} — e.g. '{bad[0][:40]}'"))
        if dt in _DATE_TYPES:
            bad = [v for v in nonblank if not _looks_like_date(v)]
            if bad:
                out.append(("warn", tgt,
                            f"{len(bad)} value(s) may not parse as a date "
                            f"for {tgt} — e.g. '{bad[0][:40]}'"))
        if not nullable and len(nonblank) < len(vals):
            out.append(("error", tgt,
                        f"{len(vals) - len(nonblank)} blank value(s) but "
                        f"{tgt} is NOT NULL"))
    if unmapped_required:
        for tgt, a in (attrs or {}).items():
            if tgt in inv:
                continue
            if str(a.get("is_nullable") or "Y").upper() == "N" \
                    and not _is_audit(tgt):
                out.append(("warn", tgt,
                            f"{tgt} is NOT NULL and has no source — it must "
                            f"be derived, defaulted, or supplied"))
    return out


def _is_audit(col):
    return is_system_column(col)


def _looks_like_date(v):
    v = v.strip()
    pats = (r"\d{4}-\d{2}(-\d{2})?", r"\d{1,2}/\d{1,2}/\d{2,4}",
            r"[A-Za-z]{3,9}[- ]\d{2,4}", r"\d{1,2}-[A-Za-z]{3}-\d{2,4}",
            r"\d{8}")
    return any(re.fullmatch(p, v.split(" ")[0]) for p in pats)


def attributes(engine, schema, table):
    """{column: {data_type, max_len, is_nullable, ordinal}}"""
    with engine.connect() as cx:
        return {str(r[0]).lower(): {"data_type": r[1], "max_len": r[2],
                                    "is_nullable": r[3], "ordinal": r[4]}
                for r in cx.execute(text(f"""
                    SELECT target_column, data_type, max_len, is_nullable,
                           ordinal
                    FROM {schema}.dv_target_attribute
                    WHERE target_table = :t"""),
                    {"t": table.upper()}).fetchall()}


def check_fit(engine, schema, table, mapping, rows):
    """DB-backed fit_issues."""
    return fit_issues(attributes(engine, schema, table), mapping, rows)


# ──────────────────────────── learning (the point) ──────────────────────────
def learn_from_load(engine, schema, table, mapping, by="VERIFIED_LOAD"):
    """Write back every source name that is not already a synonym.

    CALL THIS AFTER PROMOTE + VERIFY, never from a proposal or a Save. A
    synonym learned from a mapping that later failed would poison every
    future file silently — and it would be invisible, which is worse.

    Conflicts (this name already means a different column in this table) are
    NOT overwritten; they are returned for a human call.
    """
    existing = synonyms_for(engine, schema, table)
    learned, conflicts = [], []
    with engine.begin() as cx:
        now = datetime.utcnow()
        for src, tgt in (mapping or {}).items():
            sn, t = norm(src), str(tgt).lower()
            if not sn or not t or is_system_column(t):
                continue
            if sn in existing:
                if existing[sn] != t:
                    conflicts.append((src, existing[sn], t))
                else:
                    cx.execute(text(f"""
                        UPDATE {schema}.dv_column_synonym
                        SET hit_count = hit_count + 1
                        WHERE target_table = :tb AND synonym_norm = :sn"""),
                        {"tb": table.upper(), "sn": sn})
                continue
            cx.execute(text(f"""
                INSERT INTO {schema}.dv_column_synonym
                    (target_table, synonym_norm, target_column, synonym,
                     source, confidence, hit_count, active_ind,
                     row_created_by, row_created_date)
                VALUES (:tb, :sn, :c, :s, 'verified_load', 0.95, 1, 'Y',
                        :by, :now)"""),
                {"tb": table.upper(), "sn": sn, "c": t, "s": str(src),
                 "by": by, "now": now})
            learned.append((src, t))
    return {"learned": learned, "conflicts": conflicts}


def purge_system_synonyms(engine, schema="dataview"):
    """Delete any stored synonym pointing at a system/audit column. Set-based:
    the list is short and fixed, so one DELETE with an IN list plus the
    derived-column patterns."""
    names = sorted(_SYSTEM_COLS)
    marks = ", ".join(f":c{i}" for i in range(len(names)))
    params = {f"c{i}": n for i, n in enumerate(names)}
    with engine.begin() as cx:
        cx.execute(text("SET LOCK_TIMEOUT 20000"))
        res = cx.execute(text(f"""
            DELETE FROM {schema}.dv_column_synonym
            WHERE LOWER(target_column) IN ({marks})
               OR LOWER(target_column) LIKE 'h3[_]%'
               OR LOWER(target_column) LIKE 'geog%'
               OR LOWER(target_column) LIKE '%[_]hash'
        """), params)
    return int(res.rowcount or 0)


def retire_synonym(engine, schema, table, synonym, by="OPERATOR"):
    """Deactivate one bad synonym (learning is reversible, by design)."""
    with engine.begin() as cx:
        cx.execute(text(f"""
            UPDATE {schema}.dv_column_synonym
            SET active_ind = 'N', row_created_by = :by
            WHERE target_table = :t AND synonym_norm = :sn"""),
            {"t": table.upper(), "sn": norm(synonym), "by": by})
    return True


def set_synonym(engine, schema, table, synonym, target_column, by="OPERATOR"):
    """Operator override — outranks seed and learned alike."""
    with engine.begin() as cx:
        cx.execute(text(f"""
            MERGE {schema}.dv_column_synonym AS t
            USING (SELECT :tb AS target_table, :sn AS synonym_norm) AS s
               ON t.target_table = s.target_table
              AND t.synonym_norm = s.synonym_norm
            WHEN MATCHED THEN UPDATE SET
                 target_column = :c, synonym = :s0, source = 'operator',
                 confidence = 1.00, active_ind = 'Y', row_created_by = :by
            WHEN NOT MATCHED THEN INSERT
                 (target_table, synonym_norm, target_column, synonym, source,
                  confidence, hit_count, active_ind, row_created_by,
                  row_created_date)
                 VALUES (:tb, :sn, :c, :s0, 'operator', 1.00, 0, 'Y', :by,
                         :now);"""),
            {"tb": table.upper(), "sn": norm(synonym),
             "c": str(target_column).lower(), "s0": str(synonym), "by": by,
             "now": datetime.utcnow()})
    return True


def install(engine, schema="dataview", tables=None):
    """One call: create tables, refresh attributes from the live schema,
    seed the curated pack. Safe to re-run."""
    ensure_store(engine, schema)
    n_attr = refresh_attributes(engine, schema, tables)
    rep = seed_synonyms(engine, schema)
    rep["attributes"] = n_attr
    return rep
