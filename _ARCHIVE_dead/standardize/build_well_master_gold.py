"""
build_well_master_gold.py
Build well_ref.well_master_gold from well_ref.WELL_MASTER:
  standardize type/status  ->  dedup to one row per UWI14  ->  typed golden table.

Resolution order for each well (both type and status):
  1. source legend override (source_code_legend.csv, keyed by SOURCE token + axis)
  2. generic pattern rules (standardize_well_attrs.classify_type/classify_status)
  3. cross-field: run the same classifiers on the OTHER raw column
     (recovers WATER/OIL/GAS misfiled as status, NEW_DRILL/DRY misfiled as type)
  4. UNKNOWN  (never REVIEW, never invented)

Survivorship: one golden row per UWI14, chosen by source priority, then
completeness, then most recent LOADED_AT. The gold PK on UWI14 enforces dedup.

Cleaning happens between layers, never in gold: TOTAL_DEPTH/SPUD_DATE via
TRY_CONVERT, out-of-range coordinates nulled + flagged, state/country normalized.

    python build_well_master_gold.py                 # full build
    python build_well_master_gold.py --dry-run       # classify + emit SQL, no DB writes
    python build_well_master_gold.py --emit-sql gold.sql
"""
import argparse
import csv
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))

# Emit the reference table's spellings where dv_r_well_type uses a different
# code than the standardizer's internal canonical (your vocabulary is authoritative).
OUTPUT_TYPE_MAP = {
    "OIL_GAS":            "O&G",
    "WATER_SOURCE":       "SUPPLY",
    "STRATIGRAPHIC_TEST": "STRATIGRAPHIC TEST",
}

# ── Source priority (best -> worst). Survivorship prefers earlier entries. ───
# Regulators-of-record rank above aggregators; unlisted sources default to 900.
SOURCE_PRIORITY = [
    "TX_RRC", "GOM_BOEM", "LA_SONRIS", "OK_OCC", "KS_KGS", "NM_OCD",
    "CA_CALGEM", "CO_ECMC", "WY_WOGCC", "ND_NDIC", "MT_BOGC", "UT_DOGM",
    "OH_DNR", "PA_DEP", "WV_DEP", "NY_NYSDEC", "KY_KGS", "IL_IGS",
    "MI_EGLE", "MS_MSOGB", "AL_OGB", "AR_AOGC", "NE_DNR", "IN_DNR",
    "TN_OGB", "VA_DMME", "AK_AOGCC", "FL_DEP", "SD_DENR", "ND_OTHER",
]


def _norm(s):
    return (s or "").strip().upper()


def _tokens(source_list):
    """Split a SOURCE_LIST string into source tokens (keeps underscores)."""
    if not source_list:
        return []
    return [t for t in re.split(r"[^A-Z0-9_]+", source_list.upper()) if t]


def load_legend(path):
    """{(SOURCE, axis, RAW): STD}. axis in {'type','status'}."""
    leg = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("std") or "").strip().upper() in ("", "REVIEW"):
                continue
            leg[(_norm(r["source"]), r["axis"].strip().lower(), _norm(r["raw"]))] = \
                r["std"].strip().upper()
    return leg


def make_resolver(legend, classify_type, classify_status):
    """Returns resolve(source_list, raw_type, raw_status) -> (std_type, std_status)."""
    from dataview.reference_tables.standardize_well_attrs import TYPE as TYPE_SET, STATUS as STATUS_SET

    def legend_lookup(tokens, axis, raw):
        raw = _norm(raw)
        if not raw:
            return None
        for tok in tokens:
            hit = legend.get((tok, axis, raw))
            if hit:
                return hit
        return None

    def resolve(source_list, rt, rs):
        toks = _tokens(source_list)

        # ---- std_type ----
        st = legend_lookup(toks, "type", rt)
        if st is None:
            code, _c, why = classify_type(rt)
            if code != "REVIEW" and "status word" not in (why or ""):
                st = code
        if st is None or st == "UNKNOWN":          # try the other column
            alt = legend_lookup(toks, "type", rs)
            if alt is None:
                code, _c, why = classify_type(rs)
                if code != "REVIEW" and "status word" not in (why or ""):
                    alt = code
            if alt and alt != "UNKNOWN":
                st = alt
        if not st or st == "REVIEW":
            st = "UNKNOWN"

        # ---- std_status ----
        ss = legend_lookup(toks, "status", rs)
        if ss is None:
            code, _c, why = classify_status(rs)
            if code != "REVIEW" and "type word" not in (why or ""):
                ss = code
        if ss is None or ss == "UNKNOWN":          # try the other column
            alt = legend_lookup(toks, "status", rt)
            if alt is None:
                code, _c, why = classify_status(rt)
                if code != "REVIEW" and "type word" not in (why or ""):
                    alt = code
            if alt and alt != "UNKNOWN":
                ss = alt
        if not ss or ss == "REVIEW":
            ss = "UNKNOWN"

        # axis guard: a status code can never be a type, and vice-versa
        if st not in TYPE_SET:
            st = "UNKNOWN"
        if ss not in STATUS_SET:
            ss = "UNKNOWN"
        return st, ss

    return resolve


# ── SQL generation ───────────────────────────────────────────────────────────
GOLD = "well_ref.well_master_gold"
STAGE = "well_ref._gold_stage"
XWALK = "well_ref._gold_xwalk"
MK = ("CONCAT(COALESCE({a},N'~N~'),N'|',COALESCE({t},N'~N~'),N'|',"
      "COALESCE({s},N'~N~'))")


def src_rank_case(col="m.SOURCE_LIST"):
    whens = []
    for i, s in enumerate(SOURCE_PRIORITY):
        whens.append(f"WHEN {col} LIKE '%{s}%' THEN {i}")
    return "CASE " + " ".join(whens) + " ELSE 900 END"


def ddl_sql():
    return f"""
IF OBJECT_ID('{GOLD}') IS NULL
CREATE TABLE {GOLD} (
    uwi14            char(14)      NOT NULL PRIMARY KEY,
    api_10           char(10)      NULL,
    well_name        nvarchar(300) NULL,
    well_num         nvarchar(50)  NULL,
    operator_name    nvarchar(300) NULL,
    field_name       nvarchar(200) NULL,
    surface_latitude  decimal(9,6) NULL,
    surface_longitude decimal(9,6) NULL,
    county           nvarchar(100) NULL,
    province_state   char(2)       NULL,
    country          char(2)       NULL,
    raw_well_type    nvarchar(200) NULL,
    raw_well_status  nvarchar(200) NULL,
    std_well_type    varchar(40)   NULL,
    std_well_status  varchar(40)   NULL,
    total_depth      decimal(9,1)  NULL,
    spud_date        date          NULL,
    name_norm        nvarchar(400) NULL,
    uwi_suspect      bit           NOT NULL DEFAULT 0,
    coord_suspect    bit           NOT NULL DEFAULT 0,
    primary_source   nvarchar(120) NULL,
    source_list      nvarchar(400) NULL,
    source_count     int           NOT NULL DEFAULT 1,
    dup_count        int           NOT NULL DEFAULT 1,
    quality_score    tinyint       NOT NULL DEFAULT 0,
    built_at         datetime2     NOT NULL DEFAULT SYSUTCDATETIME()
);
"""


def build_sql(src_table):
    mk_master = MK.format(a="m.SOURCE_LIST", t="m.WELL_TYPE", s="m.WELL_STATUS")
    rank = src_rank_case()
    completeness = """(
        CASE WHEN NULLIF(LTRIM(RTRIM(m.WELL_NAME)),'')     IS NOT NULL THEN 1 ELSE 0 END
      + CASE WHEN NULLIF(LTRIM(RTRIM(m.OPERATOR_NAME)),'') IS NOT NULL
                  AND UPPER(LTRIM(RTRIM(m.OPERATOR_NAME))) NOT IN
                      ('UNKNOWN','OPERATOR UNKNOWN','OTC/OCC NOT ASSIGNED',
                       'HISTORIC OWNER','STATE FUND PLUGGING','NONE')
             THEN 1 ELSE 0 END
      + CASE WHEN NULLIF(LTRIM(RTRIM(m.COUNTY)),'')        IS NOT NULL THEN 1 ELSE 0 END
      + CASE WHEN m.SURFACE_LATITUDE  IS NOT NULL THEN 1 ELSE 0 END
      + CASE WHEN m.SURFACE_LONGITUDE IS NOT NULL THEN 1 ELSE 0 END
      + CASE WHEN TRY_CONVERT(decimal(9,1), m.TOTAL_DEPTH) IS NOT NULL THEN 1 ELSE 0 END
      + CASE WHEN TRY_CONVERT(date, m.SPUD_DATE) BETWEEN '1859-01-01' AND CAST(GETDATE() AS date) THEN 1 ELSE 0 END
      + CASE WHEN NULLIF(LTRIM(RTRIM(m.FIELD_NAME)),'')    IS NOT NULL
                  AND UPPER(LTRIM(RTRIM(m.FIELD_NAME))) NOT IN
                      ('UNKNOWN','ANY FIELD','UNNAMED')
             THEN 1 ELSE 0 END )"""
    return f"""
IF OBJECT_ID('{STAGE}') IS NOT NULL DROP TABLE {STAGE};

WITH cleaned AS (
    SELECT
        uwi14   = LEFT(LTRIM(RTRIM(m.UWI14)),14),
        api_10  = LEFT(LTRIM(RTRIM(m.API_10)),10),
        well_name = LEFT(NULLIF(LTRIM(RTRIM(m.WELL_NAME)),''),300),
        well_num  = LEFT(NULLIF(LTRIM(RTRIM(m.WELL_NUM)),''),50),
        operator_name = CASE
            WHEN UPPER(LTRIM(RTRIM(m.OPERATOR_NAME))) IN
                 ('UNKNOWN','OPERATOR UNKNOWN','OTC/OCC NOT ASSIGNED',
                  'HISTORIC OWNER','STATE FUND PLUGGING','NONE')
            THEN NULL
            ELSE LEFT(NULLIF(LTRIM(RTRIM(m.OPERATOR_NAME)),''),300) END,
        field_name    = CASE
            WHEN UPPER(LTRIM(RTRIM(m.FIELD_NAME))) IN
                 ('UNKNOWN','ANY FIELD','UNNAMED')
            THEN NULL
            ELSE LEFT(NULLIF(LTRIM(RTRIM(m.FIELD_NAME)),''),200) END,
        lat_raw = m.SURFACE_LATITUDE, lon_raw = m.SURFACE_LONGITUDE,
        surface_latitude  = CASE WHEN m.SURFACE_LATITUDE  BETWEEN 15 AND 72
                                 THEN TRY_CONVERT(decimal(9,6), m.SURFACE_LATITUDE)  END,
        surface_longitude = CASE WHEN m.SURFACE_LONGITUDE BETWEEN -180 AND -60
                                 THEN TRY_CONVERT(decimal(9,6), m.SURFACE_LONGITUDE) END,
        county = LEFT(NULLIF(LTRIM(RTRIM(m.COUNTY)),''),100),
        province_state = LEFT(UPPER(NULLIF(LTRIM(RTRIM(m.PROVINCE_STATE)),'')),2),
        country = CASE WHEN UPPER(LTRIM(RTRIM(m.COUNTRY))) IN ('US','USA','UNITED STATES')
                       THEN 'US' ELSE LEFT(UPPER(NULLIF(LTRIM(RTRIM(m.COUNTRY)),'')),2) END,
        raw_well_type   = LEFT(m.WELL_TYPE,200),
        raw_well_status = LEFT(m.WELL_STATUS,200),
        std_well_type   = COALESCE(x.std_well_type,'UNKNOWN'),
        std_well_status = COALESCE(x.std_well_status,'UNKNOWN'),
        total_depth = TRY_CONVERT(decimal(9,1), m.TOTAL_DEPTH),
        spud_date   = CASE WHEN TRY_CONVERT(date, m.SPUD_DATE)
                                BETWEEN '1859-01-01' AND CAST(GETDATE() AS date)
                           THEN TRY_CONVERT(date, m.SPUD_DATE) END,
        name_norm   = LEFT(m.NAME_NORM,400),
        uwi_suspect = COALESCE(m.UWI_SUSPECT,0),
        coord_suspect = CASE
            WHEN (m.SURFACE_LATITUDE  IS NOT NULL AND m.SURFACE_LATITUDE  NOT BETWEEN 15 AND 72)
              OR (m.SURFACE_LONGITUDE IS NOT NULL AND m.SURFACE_LONGITUDE NOT BETWEEN -180 AND -60)
            THEN 1 ELSE 0 END,
        source_list  = LEFT(m.SOURCE_LIST,400),
        loaded_at    = m.LOADED_AT,
        ref_id       = m.REF_ID,
        src_rank     = {rank},
        completeness = {completeness}
    FROM {src_table} m
    LEFT JOIN {XWALK} x ON x.mk = {mk_master}
    WHERE m.UWI14 IS NOT NULL AND LEN(LTRIM(RTRIM(m.UWI14))) = 14
),
agg AS (
    SELECT uwi14,
           dup_count    = COUNT(*),
           source_count = COUNT(DISTINCT source_list)
    FROM cleaned GROUP BY uwi14
),
ranked AS (
    SELECT c.*,
           rn = ROW_NUMBER() OVER (PARTITION BY c.uwi14
                ORDER BY c.src_rank ASC, c.completeness DESC,
                         c.loaded_at DESC, c.ref_id ASC)
    FROM cleaned c
)
SELECT
    r.uwi14, r.api_10, r.well_name, r.well_num, r.operator_name, r.field_name,
    r.surface_latitude, r.surface_longitude, r.county, r.province_state, r.country,
    r.raw_well_type, r.raw_well_status, r.std_well_type, r.std_well_status,
    r.total_depth, r.spud_date, r.name_norm, r.uwi_suspect, r.coord_suspect,
    primary_source = r.source_list,
    r.source_list, a.source_count, a.dup_count,
    quality_score = CAST(r.completeness * 100.0 / 8 AS tinyint)
INTO {STAGE}
FROM ranked r
JOIN agg a ON a.uwi14 = r.uwi14
WHERE r.rn = 1;

BEGIN TRAN;
    TRUNCATE TABLE {GOLD};
    INSERT INTO {GOLD} (
        uwi14, api_10, well_name, well_num, operator_name, field_name,
        surface_latitude, surface_longitude, county, province_state, country,
        raw_well_type, raw_well_status, std_well_type, std_well_status,
        total_depth, spud_date, name_norm, uwi_suspect, coord_suspect,
        primary_source, source_list, source_count, dup_count, quality_score)
    SELECT
        uwi14, api_10, well_name, well_num, operator_name, field_name,
        surface_latitude, surface_longitude, county, province_state, country,
        raw_well_type, raw_well_status, std_well_type, std_well_status,
        total_depth, spud_date, name_norm, uwi_suspect, coord_suspect,
        primary_source, source_list, source_count, dup_count, quality_score
    FROM {STAGE};
COMMIT;
DROP TABLE {STAGE};
"""


def _engine(server, database):
    from sqlalchemy import create_engine
    odbc = (f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};"
            f"DATABASE={database};Trusted_Connection=yes")
    return create_engine("mssql+pyodbc:///?odbc_connect=" +
                         urllib.parse.quote_plus(odbc), fast_executemany=True)


def build_crosswalk(conn, src_table, resolve):
    """Classify every distinct (SOURCE_LIST, WELL_TYPE, WELL_STATUS) once."""
    from sqlalchemy import text
    rows = conn.execute(text(
        f"SELECT DISTINCT SOURCE_LIST, WELL_TYPE, WELL_STATUS FROM {src_table}"
    )).fetchall()
    out = []
    for sl, wt, ws in rows:
        st, ss = resolve(sl, wt, ws)
        st = OUTPUT_TYPE_MAP.get(st, st)
        mk = "{}|{}|{}".format(sl if sl is not None else "~N~",
                               wt if wt is not None else "~N~",
                               ws if ws is not None else "~N~")
        out.append((mk, st, ss))
    return out


def load_crosswalk(conn, xwalk):
    from sqlalchemy import text
    conn.execute(text(f"IF OBJECT_ID('{XWALK}') IS NOT NULL DROP TABLE {XWALK}"))
    conn.execute(text(
        f"CREATE TABLE {XWALK} (mk nvarchar(2000) NOT NULL PRIMARY KEY, "
        f"std_well_type varchar(40), std_well_status varchar(40))"))
    raw = conn.connection
    cur = raw.cursor()
    cur.fast_executemany = True
    cur.executemany(
        f"INSERT INTO {XWALK} (mk, std_well_type, std_well_status) VALUES (?,?,?)",
        xwalk)
    cur.close()


def report(conn, src_table):
    from sqlalchemy import text
    q = lambda s: conn.execute(text(s)).scalar()
    master = q(f"SELECT COUNT(*) FROM {src_table}")
    gold = q(f"SELECT COUNT(*) FROM {GOLD}")
    dups = master - gold
    ut = q(f"SELECT COUNT(*) FROM {GOLD} WHERE std_well_type='UNKNOWN'")
    us = q(f"SELECT COUNT(*) FROM {GOLD} WHERE std_well_status='UNKNOWN'")
    cs = q(f"SELECT COUNT(*) FROM {GOLD} WHERE coord_suspect=1")
    print(f"\nmaster rows      {master:>12,}")
    print(f"golden wells     {gold:>12,}   (one row per UWI14)")
    print(f"dups collapsed   {dups:>12,}")
    print(f"std_type  known  {100*(gold-ut)/max(gold,1):>11.1f}%   ({ut:,} UNKNOWN)")
    print(f"std_status known {100*(gold-us)/max(gold,1):>11.1f}%   ({us:,} UNKNOWN)")
    print(f"coord_suspect    {cs:>12,}")
    print("\ntop std_well_status:")
    for k, n in conn.execute(text(
            f"SELECT TOP 12 std_well_status, COUNT(*) n FROM {GOLD} "
            f"GROUP BY std_well_status ORDER BY n DESC")):
        print(f"    {k:24} {n:>10,}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server",    default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--database",  default="WELL_REF")
    ap.add_argument("--src-table", default="well_ref.WELL_MASTER")
    ap.add_argument("--legend",    default=os.path.join(HERE, "source_code_legend.csv"))
    ap.add_argument("--dry-run", action="store_true",
                    help="classify + write crosswalk/SQL, no DB writes")
    ap.add_argument("--emit-sql", default=None, help="write generated SQL to this file")
    args = ap.parse_args()

    sys.path.insert(0, HERE)
    from dataview.reference_tables.standardize_well_attrs import classify_type, classify_status
    legend = load_legend(args.legend)
    resolve = make_resolver(legend, classify_type, classify_status)
    print(f"legend entries: {len(legend)}")

    full_sql = ddl_sql() + build_sql(args.src_table)
    if args.emit_sql:
        open(args.emit_sql, "w", encoding="utf-8").write(full_sql)
        print(f"wrote SQL -> {args.emit_sql}")

    if args.dry_run:
        print("dry-run: no DB writes. Resolver + SQL generated only.")
        return

    eng = _engine(args.server, args.database)
    with eng.begin() as conn:
        from sqlalchemy import text
        print("building attribute crosswalk (distinct source/type/status)...")
        xwalk = build_crosswalk(conn, args.src_table, resolve)
        print(f"  {len(xwalk):,} distinct combos classified")
        load_crosswalk(conn, xwalk)
        print("creating gold table if needed...")
        conn.exec_driver_sql(ddl_sql())
        print("building golden records (dedup + survivorship)...")
        # build_sql is multi-statement; run as a script via exec_driver_sql
        conn.exec_driver_sql(build_sql(args.src_table))
        conn.exec_driver_sql(f"IF OBJECT_ID('{XWALK}') IS NOT NULL DROP TABLE {XWALK}")
        report(conn, args.src_table)
    print("\ndone.")


if __name__ == "__main__":
    main()
