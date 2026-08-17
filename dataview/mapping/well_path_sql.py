"""
well_path_sql.py — compute the paths ON THE SERVER
===================================================

Same geometry as well_path.py, expressed in T-SQL so the stations never
cross the wire. Measured on Perry's box, pyodbc fetches these rows at
~11.6 ms each — 37,738 stations is seven minutes of client-side grinding
for a result that is a few hundred LINESTRINGs. Moving the arithmetic to
where the rows already are removes the transfer entirely.

    python -m dataview.mapping.well_path_sql summary --server X --database Y --like "4902%"
    python -m dataview.mapping.well_path_sql apply   --server X --database Y --like "4902%"

`summary` computes everything and returns ONE ROW PER SURVEY — stations,
MD, TVD, closure, max dogleg. That is the safe first command: it exercises
the whole calculation, returns a few hundred rows, and writes nothing.
`apply` runs the same computation and updates PATH_GEOG in place, with no
result set at all.

WHY THE MATH IS SAFE TO MOVE
----------------------------
Minimum curvature is a running sum over ordered stations, which is exactly
what a window function is for: LAG gives the previous station, the arc
maths is scalar arithmetic per pair, and SUM(...) OVER (ORDER BY md ROWS
UNBOUNDED PRECEDING) accumulates. Nothing about it needs a procedural
language, and the Python version in well_path.py remains the reference —
its selftest proves the formulas against closed-form cases.

THE ONE DELIBERATE DIFFERENCE
-----------------------------
well_path.py projects to UTM with pyproj. T-SQL has no projection
library, so this uses a local tangent-plane approximation: metres per
degree of latitude, and the same divided by cos(latitude) for longitude.
Over a wellbore's horizontal reach — hundreds to a few thousand feet —
that agrees with the projected answer to well under a metre, which is
invisible at map scale. For anything where sub-metre matters, use the
Python path. This is a MAPPING tool and says so.

REQUIREMENTS: SQL Server 2017+ for STRING_AGG. On 2016 the aggregation
needs the FOR XML PATH form instead; everything else is unchanged.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time as _time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

_SAFE = re.compile(r"^[0-9A-Za-z_\-]+$")


def _uwi_where(col, uwi=None, like=None):
    """A seekable literal predicate. A prefix is a RANGE, not a LIKE."""
    def lit(v):
        if not _SAFE.match(v):
            raise ValueError(f"refusing {v!r} as a uwi filter")
        return "'" + v + "'"
    if uwi:
        return f" AND {col} = {lit(uwi)}"
    if not like:
        return ""
    pref = like.rstrip("%")
    if not pref or "%" in pref or "_" in pref:
        return ""
    hi = pref[:-1] + chr(ord(pref[-1]) + 1)
    return f" AND {col} >= {lit(pref)} AND {col} < {lit(hi)}"


# ═════════════════════════════════════════════════════════════════════════ #
# The computation. One CTE chain, four steps:
#   s  narrow + convert the station columns (and filter)
#   p  pair each station with its predecessor (LAG); a missing predecessor
#      means the surface, so pmd=0, pinc=0 — the implied vertical top,
#      which is what the Python version inserts too
#   d  the arc maths per pair: dogleg, ratio factor, dN/dE/dV, DLS
#   c  cumulative sums -> north/east/tvd at every station, plus lat/long
# ═════════════════════════════════════════════════════════════════════════ #
_CORE = """
WITH s AS (
    SELECT  uwi  = CAST(st.{uwi} AS char(14)),
            sid  = CAST({sid} AS varchar(64)),
            md   = TRY_CONVERT(float, st.{md}),
            inc  = RADIANS(TRY_CONVERT(float, st.{inc})),
            azi  = RADIANS(TRY_CONVERT(float, st.{azi}))
    FROM {schema}.{sta} st WITH (NOLOCK)
    WHERE TRY_CONVERT(float, st.{md})  IS NOT NULL
      AND TRY_CONVERT(float, st.{inc}) IS NOT NULL
      AND TRY_CONVERT(float, st.{azi}) IS NOT NULL
      {where}
),
p AS (
    SELECT s.*,
           pmd  = ISNULL(LAG(md)  OVER (PARTITION BY uwi, sid ORDER BY md), 0),
           pinc = ISNULL(LAG(inc) OVER (PARTITION BY uwi, sid ORDER BY md), 0),
           pazi = ISNULL(LAG(azi) OVER (PARTITION BY uwi, sid ORDER BY md), azi)
    FROM s
),
d AS (
    SELECT p.*, z.dmd, w.beta, r.rf,
           dN = z.dmd / 2 * (SIN(pinc) * COS(pazi) + SIN(inc) * COS(azi)) * r.rf,
           dE = z.dmd / 2 * (SIN(pinc) * SIN(pazi) + SIN(inc) * SIN(azi)) * r.rf,
           dV = z.dmd / 2 * (COS(pinc) + COS(inc)) * r.rf,
           dls = CASE WHEN z.dmd > 0
                      THEN DEGREES(w.beta) * 100.0 / z.dmd ELSE 0 END
    FROM p
    CROSS APPLY (SELECT dmd = md - pmd) z
    CROSS APPLY (SELECT cosb = COS(inc - pinc)
                             - SIN(pinc) * SIN(inc) * (1 - COS(azi - pazi))) k
    CROSS APPLY (SELECT beta = ACOS(CASE WHEN k.cosb >  1 THEN  1
                                         WHEN k.cosb < -1 THEN -1
                                         ELSE k.cosb END)) w
    CROSS APPLY (SELECT rf = CASE WHEN w.beta < 1e-9 THEN 1.0
                                  ELSE (2.0 / w.beta) * TAN(w.beta / 2) END) r
    WHERE md > pmd
),
c AS (
    SELECT d.*,
           n = SUM(dN) OVER (PARTITION BY uwi, sid ORDER BY md
                             ROWS UNBOUNDED PRECEDING),
           e = SUM(dE) OVER (PARTITION BY uwi, sid ORDER BY md
                             ROWS UNBOUNDED PRECEDING),
           v = SUM(dV) OVER (PARTITION BY uwi, sid ORDER BY md
                             ROWS UNBOUNDED PRECEDING),
           rn = ROW_NUMBER() OVER (PARTITION BY uwi, sid ORDER BY md),
           rc = COUNT(*)     OVER (PARTITION BY uwi, sid)
    FROM d
    WHERE {dls_filter}
),
g AS (
    SELECT c.*, wl.lat0, wl.lon0,
           -- Metres per degree as a FUNCTION OF LATITUDE, not the flat
           -- 111320 constant — that constant is a 0.4% error in latitude
           -- at these workings, which is metres at the bottom hole. The
           -- series below agree with a projected answer to well under a
           -- metre over a wellbore's horizontal reach.
           lat = wl.lat0 + (c.n * {k}) / m.mlat,
           lon = wl.lon0 + (c.e * {k}) / m.mlon
    FROM c
    JOIN (SELECT uwi  = CAST(uwi AS char(14)),
                 lat0 = TRY_CONVERT(float, {lat}),
                 lon0 = TRY_CONVERT(float, {lon})
          FROM {schema}.{well} WITH (NOLOCK)
          WHERE TRY_CONVERT(float, {lat}) IS NOT NULL
            AND TRY_CONVERT(float, {lon}) IS NOT NULL) wl
      ON wl.uwi = c.uwi
    CROSS APPLY (SELECT phi = RADIANS(wl.lat0)) f
    CROSS APPLY (SELECT mlat = 111132.92 - 559.82 * COS(2 * f.phi)
                             + 1.175 * COS(4 * f.phi),
                        mlon = 111412.84 * COS(f.phi)
                             - 93.5 * COS(3 * f.phi)) m
)
"""

_SUMMARY = _CORE + """
SELECT uwi, sid,
       stations = COUNT(*),
       md_max   = MAX(md),
       tvd_max  = MAX(v),
       closure  = MAX(SQRT(n * n + e * e)),
       max_dls  = MAX(dls)
FROM g
GROUP BY uwi, sid
HAVING MAX(SQRT(n * n + e * e)) >= {min_closure}
ORDER BY uwi, sid;
"""

# Generalization happens in the aggregation: keep the first and last
# station, anything with a real dogleg, and every Nth in between. A
# tangent section is a straight line however many points describe it.
_APPLY = _CORE + """
, keep AS (
    SELECT * FROM g
    WHERE rn = 1 OR rn = rc OR dls >= {keep_dls} OR rn % {every} = 0
),
line AS (
    SELECT uwi, sid,
           -- STR, not CONVERT. CONVERT(varchar, float) keeps six
           -- digits by default and can emit scientific notation — both
           -- ruinous for a coordinate inside WKT. STR(x, 20, 8) gives
           -- fixed decimals, and 8 of them is ~1 mm.
           -- CAST TO varchar(max) INSIDE the aggregate. STRING_AGG
           -- returns varchar(8000) unless its INPUT is a LOB type, and a
           -- 2,000-station survey keeps enough vertices to pass that —
           -- which fails the whole statement rather than truncating.
           pts = STRING_AGG(
                   CAST(LTRIM(STR(lon, 20, 8)) + ' '
                        + LTRIM(STR(lat, 20, 8)) AS varchar(max)),
                   ', ') WITHIN GROUP (ORDER BY md),
           npts = COUNT(*)
    FROM keep
    GROUP BY uwi, sid
    -- A NEARLY VERTICAL WELL IS NOT A PATH. These wells close a couple of
    -- hundred feet over a mile of hole; at map scale that is a dot, and
    -- drawing it as a line implies a shape nobody can see and precision
    -- nobody needs. They are already on the map as points from dv_well —
    -- so a path is for wells that actually go somewhere.
    HAVING MAX(SQRT(n * n + e * e)) >= {min_closure}
)
UPDATE h
   SET {col} = geography::STGeomFromText(
                 CAST('LINESTRING(' AS varchar(max))
                 + line.pts + ')', 4326).MakeValid()
FROM {schema}.{hdr} h
JOIN line ON CAST(h.uwi AS char(14)) = line.uwi
         {sid_join}
WHERE line.npts >= 2;
"""


def _fmt(sql, schema, cols, unit, where, dls_filter, **extra):
    return sql.format(
        schema=schema, sta="dv_well_dir_srvy_sta", well="dv_well",
        uwi=cols["uwi"], sid=cols["sid"], md=cols["md"], inc=cols["inc"],
        azi=cols["azi"], lat=cols["lat"], lon=cols["lon"],
        k=(0.3048 if unit == "ft" else 1.0),
        where=where, dls_filter=dls_filter, **extra)


def get_engine(server, database, driver="ODBC Driver 17 for SQL Server"):
    from sqlalchemy import create_engine, event
    url = (f"mssql+pyodbc://@{server}/{database}"
           f"?driver={driver.replace(' ', '+')}&trusted_connection=yes")
    eng = create_engine(url)

    @event.listens_for(eng, "connect")
    def _settings(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("SET ARITHABORT ON; SET NOCOUNT ON;")
        finally:
            cur.close()
    return eng


def resolve_columns(engine, schema, log=print):
    """Same introspection as well_path.py — deployments name these
    differently and a hard-coded list is a landmine."""
    from sqlalchemy import text
    from dataview.mapping.well_path import CAND, _pick
    with engine.connect() as cx:
        sta = [r[0] for r in cx.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=:s AND TABLE_NAME='dv_well_dir_srvy_sta'"),
            {"s": schema})]
        wel = [r[0] for r in cx.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=:s AND TABLE_NAME='dv_well'"), {"s": schema})]
    c = {k: _pick(sta, v) for k, v in CAND.items()}
    low = {x.lower(): x for x in wel}
    out = {
        "uwi": c["uwi"] or "uwi",
        "sid": (f"st.{c['srvy']}" if c["srvy"] else "''"),
        "md": c["md"], "inc": c["inc"], "azi": c["azi"],
        "lat": low.get("surface_latitude") or low.get("latitude"),
        "lon": low.get("surface_longitude") or low.get("longitude"),
        "_sid_col": c["srvy"],
    }
    missing = [k for k in ("md", "inc", "azi", "lat", "lon") if not out[k]]
    if missing:
        raise RuntimeError(f"could not resolve columns for {missing}")
    log(f"  stations: md={out['md']} inc={out['inc']} azi={out['azi']}"
        f"   well: lat={out['lat']} lon={out['lon']}")
    return out


def ensure_geog(engine, schema, hdr, col, log=print):
    from sqlalchemy import text
    with engine.connect() as cx:
        have = {r[0].upper() for r in cx.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t"),
            {"s": schema, "t": hdr})}
    if col.upper() not in have:
        with engine.begin() as cx:
            cx.execute(text(f"ALTER TABLE {schema}.{hdr} ADD {col} geography NULL"))
        log(f"  + column {schema}.{hdr}.{col} geography")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Compute well paths on the server — no station rows "
                    "cross the wire.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (("summary", "compute and return one row per survey"),
                           ("apply", "compute and store PATH_GEOG"),
                           ("sql", "print the SQL and exit")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--server", required=(name != "sql"))
        s.add_argument("--database", required=(name != "sql"))
        s.add_argument("--driver", default="ODBC Driver 17 for SQL Server")
        s.add_argument("--schema", default="dataview")
        s.add_argument("--hdr", default="dv_well_dir_srvy_hdr")
        s.add_argument("--col", default="PATH_GEOG")
        s.add_argument("--uwi")
        s.add_argument("--like")
        s.add_argument("--unit", default="ft", choices=["ft", "m"])
        s.add_argument("--max-dls", type=float, default=0.0,
                       help="exclude stations implying a dogleg above this "
                            "(deg/100). 0 = keep everything")
        s.add_argument("--min-closure", type=float, default=500.0,
                       help="only wells whose bottom hole is at least this "
                            "far from the surface get a path (same unit as "
                            "the survey; 0 = every well)")
        s.add_argument("--every", type=int, default=10,
                       help="keep every Nth station between the doglegs")
        s.add_argument("--keep-dls", type=float, default=1.0,
                       help="always keep a station with at least this dogleg")
    a = ap.parse_args(argv)

    if a.cmd == "sql":
        cols = {"uwi": "uwi", "sid": "st.survey_id", "md": "md",
                "inc": "incl", "azi": "azim", "lat": "surface_latitude",
                "lon": "surface_longitude", "_sid_col": "survey_id"}
        where = _uwi_where("st.uwi", a.uwi, a.like)
        dls_f = "1 = 1" if not a.max_dls else f"dls <= {a.max_dls}"
        print(_fmt(_SUMMARY, a.schema, cols, a.unit, where, dls_f,
                   min_closure=a.min_closure))
        return 0

    from sqlalchemy import text
    engine = get_engine(a.server, a.database, a.driver)
    print(f"resolving columns in {a.schema} …")
    cols = resolve_columns(engine, a.schema)
    where = _uwi_where(f"st.{cols['uwi']}", a.uwi, a.like)
    dls_f = "1 = 1" if not a.max_dls else f"dls <= {a.max_dls}"

    if a.cmd == "summary":
        sql = _fmt(_SUMMARY, a.schema, cols, a.unit, where, dls_f,
                   min_closure=a.min_closure)
        print("computing on the server …")
        t0 = _time.time()
        with engine.connect() as cx:
            rows = cx.execute(text(sql)).fetchall()
        print(f"{len(rows)} survey(s) with at least "
              f"{a.min_closure:,.0f} of closure, in "
              f"{_time.time() - t0:.1f}s\n")
        print(f"{'uwi':16} {'sta':>5} {'MD':>10} {'TVD':>10} "
              f"{'closure':>9} {'DLS':>7}")
        for u, _sid, n, md, tvd, clo, dls in rows[:40]:
            print(f"{str(u).strip()[:16]:16} {n:5} {md:10,.0f} {tvd:10,.0f} "
                  f"{clo:9,.0f} {dls:7.1f}")
        if len(rows) > 40:
            print(f"  … and {len(rows) - 40} more")
        odd = [r for r in rows if r[4] and r[3] and r[4] > r[3] * 1.001]
        if odd:
            print(f"\n⚠ {len(odd)} survey(s) computed a TVD greater than MD — "
                  f"impossible; check the inclination units (degrees?)")
        return 0

    ensure_geog(engine, a.schema, a.hdr, a.col)
    sid_join = ("AND CAST(h.survey_id AS varchar(64)) = line.sid"
                if cols["_sid_col"] else "")
    sql = _fmt(_APPLY, a.schema, cols, a.unit, where, dls_f,
               hdr=a.hdr, col=a.col, sid_join=sid_join,
               every=max(1, a.every), keep_dls=a.keep_dls,
               min_closure=a.min_closure)
    print("computing and storing on the server …")
    t0 = _time.time()
    with engine.begin() as cx:
        n = cx.execute(text(sql)).rowcount
    print(f"{n} survey header row(s) updated in {_time.time() - t0:.1f}s")
    print(f"(wells closing less than {a.min_closure:,.0f} were skipped — "
          f"they are vertical at map scale and remain points from dv_well)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
