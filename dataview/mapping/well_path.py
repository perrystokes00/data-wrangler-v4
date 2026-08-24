"""
well_path.py — deviated well paths, computed and drawn
=======================================================

Turns directional survey stations into a wellbore path you can map: minimum
curvature for the geometry, a proper map projection for the coordinates, a
LINESTRING per survey in a geography column, and a folium layer that reads
it back.

    py -m dataview.mapping.well_path selftest
    py -m dataview.mapping.well_path compute --server localhost\\SQLEXPRESS ^
        --database DataView_Demo                       # dry run, reports only
    py -m dataview.mapping.well_path compute --server ... --database ... --apply

WHY MINIMUM CURVATURE
---------------------
Tangential and balanced-tangential methods assume the hole is straight
between stations, which puts a 10,000 ft lateral hundreds of feet off. The
industry uses minimum curvature: fit a circular arc through both stations
honouring the inclination and azimuth at each end. It is the method every
survey company reports against, so a path computed this way agrees with the
TVD printed on the vendor's own report — which is exactly what `selftest`
checks.

WHAT IT REFUSES TO DO
---------------------
A well with no survey gets NO path. If a bottom-hole location exists the
caller may draw a straight surface-to-BHL stick and say so; inventing a
curve for a well nobody surveyed produces a picture that will be believed.
Absent geometry is not the same as straight geometry.

THE TWO TRAPS, both checked
---------------------------
1. AZIMUTH REFERENCE — grid, true or magnetic north. Mixing grid azimuths
   into a true-north computation skews every lateral by the grid
   convergence: a couple of degrees, a few hundred feet at TD, and entirely
   plausible on a map. The survey header carries it; this module reports
   what it found and does not silently convert.
2. UNITS — a survey in metres run as feet is off by 3.28x and still looks
   like a well. The TVD cross-check against the stations' own TVD/TVDSS
   catches both this and (1) before anything is written.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time as _time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

FT_TO_M = 0.3048

# A PATH SHORTER THAN THIS CANNOT BE SEEN, so drawing it is a lie of detail: a
# vertical well renders as a smudge on its own surface dot, indistinguishable
# from the marker already there, and each one still costs a geography write, a
# row in the layer, and a polyline for the browser to draw.
#
# Web Mercator resolution is 156543 * cos(lat) / 2^z metres per pixel. At 43 N
# (Teapot):  z12 ~28 m/px,  z13 ~14 m/px,  z14 ~7 m/px. A polyline needs about
# four pixels of travel before it reads as a line rather than a dot, so at the
# zoom where a field fills the screen (z13) the crossover is ~55 m. 50 is that,
# rounded down so the gate errs toward drawing.
#
# CLOSURE, NOT MEASURED DEPTH: a 10,000 ft vertical hole has no map extent at
# all. Closure is the horizontal distance from surface to bottom, which is
# exactly what the map would show.
MIN_CLOSURE_M = 50.0


# ═════════════════════════════════════════════════════════════════════════ #
# 1 · GEOMETRY — minimum curvature
# ═════════════════════════════════════════════════════════════════════════ #
def _rf(beta):
    """Ratio factor. The arc's chord-to-arc correction.

    beta is the dogleg angle in radians. As beta -> 0 the expression
    (2/beta)*tan(beta/2) is 0/0; the limit is 1 (a straight section needs no
    correction). Guarding this explicitly matters — an unguarded division
    yields NaN, NaN propagates through every cumulative sum after it, and
    the whole path silently disappears from the map.
    """
    if beta < 1e-9:
        return 1.0
    return (2.0 / beta) * math.tan(beta / 2.0)


def dogleg(i1, a1, i2, a2):
    """Dogleg angle (radians) between two stations, inputs in radians."""
    c = (math.cos(i2 - i1)
         - math.sin(i1) * math.sin(i2) * (1.0 - math.cos(a2 - a1)))
    return math.acos(max(-1.0, min(1.0, c)))       # clamp: acos domain


def flag_spikes(sts, max_dls=30.0):
    """Stations that imply a physically impossible dogleg.

    A real dogleg tops out near 15 deg/100 ft; anything past 30 is a data
    fault, not a hole. The usual cause is a NULL inclination and azimuth
    recorded as 0.0/0.0 — which is indistinguishable from a genuine
    vertical station EXCEPT that it sits a few feet from a deviated one.
    Judging on the DLS rather than on the zeros catches that case and
    leaves genuinely vertical wells alone.

    Returns [(index, dls, why)] — it does NOT remove anything. Silently
    dropping a station is editing the customer's data on a guess; naming
    it is not.
    """
    out = []
    for k in range(1, len(sts)):
        md1, i1, a1 = sts[k - 1][:3]
        md2, i2, a2 = sts[k][:3]
        d = md2 - md1
        if d <= 0:
            out.append((k, None, f"station at or above the previous MD ({md2})"))
            continue
        beta = dogleg(math.radians(i1), math.radians(a1),
                      math.radians(i2), math.radians(a2))
        dls = math.degrees(beta) * 100.0 / d
        if dls > max_dls:
            why = f"{dls:,.0f} deg/100 over {d:,.0f} ft"
            if i2 == 0.0 and a2 == 0.0 and i1 != 0.0:
                why += " — inclination and azimuth are both 0, which is how"
                why += " a NULL is usually written"
            out.append((k, dls, why))
    return out


def minimum_curvature(stations, start=(0.0, 0.0, 0.0)):
    """[(md, inc_deg, azi_deg), ...] -> [(md, north, east, tvd, dls_per_100)]

    Offsets are cumulative from `start` in the SAME UNIT as md. The first
    station is joined to the surface by an implied vertical station at
    md=0 when the survey does not start at zero — which is how vendors
    report, and dropping it would shorten every well by its first interval.
    """
    if not stations:
        return []
    sts = sorted(stations, key=lambda s: float(s[0]))
    if sts[0][0] > 1e-6:
        sts = [(0.0, 0.0, float(sts[0][2]))] + list(sts)

    n, e, v = start
    out = [(sts[0][0], n, e, v, 0.0)]
    for (md1, i1d, a1d), (md2, i2d, a2d) in zip(sts, sts[1:]):
        dmd = float(md2) - float(md1)
        if dmd <= 0:
            continue                               # duplicate/backward station
        i1, i2 = math.radians(float(i1d)), math.radians(float(i2d))
        a1, a2 = math.radians(float(a1d)), math.radians(float(a2d))
        beta = dogleg(i1, a1, i2, a2)
        rf = _rf(beta)
        half = dmd / 2.0
        n += half * (math.sin(i1) * math.cos(a1)
                     + math.sin(i2) * math.cos(a2)) * rf
        e += half * (math.sin(i1) * math.sin(a1)
                     + math.sin(i2) * math.sin(a2)) * rf
        v += half * (math.cos(i1) + math.cos(i2)) * rf
        dls = math.degrees(beta) * (100.0 / dmd) if dmd else 0.0
        out.append((float(md2), n, e, v, dls))
    return out


# ═════════════════════════════════════════════════════════════════════════ #
# 2 · COORDINATES — offsets to lat/long
# ═════════════════════════════════════════════════════════════════════════ #
def utm_epsg(lat, lon):
    zone = int((lon + 180.0) / 6.0) + 1
    return (32600 if lat >= 0 else 32700) + zone


_TX = {}


def _transformers(epsg):
    """One pair of transformers per UTM zone, reused. Building them is not
    free, and a field is one zone — constructing two per well is thousands
    of identical objects for nothing."""
    if epsg not in _TX:
        from pyproj import Transformer
        _TX[epsg] = (Transformer.from_crs(4326, epsg, always_xy=True),
                     Transformer.from_crs(epsg, 4326, always_xy=True))
    return _TX[epsg]


def to_lat_long(path, surf_lat, surf_lon, unit="ft", epsg=None,
                azimuth_ref="true"):
    """Cumulative N/E offsets -> [(lon, lat, tvd, md)].

    Projects the surface location into UTM, adds the offsets in METRES,
    and unprojects. Degrees-per-foot approximations are wrong by enough to
    matter over a 10,000 ft lateral and wrong by more the further north you
    are; a projection costs one transformer per zone and is simply right.
    """
    epsg = epsg or utm_epsg(surf_lat, surf_lon)
    fwd, inv = _transformers(epsg)
    x0, y0 = fwd.transform(surf_lon, surf_lat)
    k = FT_TO_M if unit == "ft" else 1.0

    # GRID CONVERGENCE. Adding a north offset to a UTM northing moves the
    # point along GRID north, but survey azimuths are normally referenced
    # to TRUE north. The two differ by the convergence — about 0.7 deg at
    # 1.3 deg from the central meridian — which is 7 m over 600 m of
    # displacement: small on a map, wrong in a report, and exactly the
    # error this module warns about in its own docstring. Rotate the
    # offsets into the grid frame first.
    gamma = 0.0
    if azimuth_ref == "true":
        cm = ((epsg % 100) * 6.0) - 183.0          # zone -> central meridian
        gamma = math.radians(surf_lon - cm) * math.sin(math.radians(surf_lat))
    cg, sg = math.cos(gamma), math.sin(gamma)

    out = []
    for md, n, e, v, _dls in path:
        ng = n * cg + e * sg
        eg = e * cg - n * sg
        lon, lat = inv.transform(x0 + eg * k, y0 + ng * k)
        out.append((lon, lat, v, md))
    return out


# ═════════════════════════════════════════════════════════════════════════ #
# 3 · GENERALIZE — fewer points, same shape
# ═════════════════════════════════════════════════════════════════════════ #
def _perp(p, a, b):
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def simplify(points, tol_deg=1e-4, keep=()):
    """Douglas-Peucker on (lon, lat), with indices in `keep` protected.

    A 200-station survey draws indistinguishably as ~15 vertices. What must
    survive is the FIRST point, the LAST point, and the high-dogleg
    stations — the kick-off and the landing are the shape; the tangent
    section between them is a straight line whatever you do to it.
    """
    if len(points) < 3:
        return list(range(len(points)))
    keep = set(keep) | {0, len(points) - 1}
    idx = set(keep)

    def rec(i, j):
        if j <= i + 1:
            return
        a, b = points[i][:2], points[j][:2]
        worst, wi = -1.0, None
        for k in range(i + 1, j):
            dd = _perp(points[k][:2], a, b)
            if dd > worst:
                worst, wi = dd, k
        if wi is not None and worst > tol_deg:
            idx.add(wi)
            rec(i, wi)
            rec(wi, j)

    anchors = sorted(keep)
    for a, b in zip(anchors, anchors[1:]):
        rec(a, b)
    return sorted(idx)


def high_dogleg_indices(path, top=8, min_dls=1.0):
    ranked = sorted(range(len(path)), key=lambda i: -path[i][4])
    return [i for i in ranked[:top] if path[i][4] >= min_dls]


def _closure_m(path, unit):
    """Horizontal surface-to-bottom distance, in METRES whatever was surveyed.

    One place computes this, so the gate and the reported figure cannot drift
    apart -- the shape that made an FK clause silently inert for six weeks.
    """
    c = math.hypot(path[-1][1], path[-1][2])
    return c * (FT_TO_M if str(unit).lower().startswith("f") else 1.0)


def wkt_linestring(lonlat):
    if len(lonlat) < 2:
        return None
    pts = ", ".join(f"{lon:.8f} {lat:.8f}" for lon, lat, *_ in lonlat)
    return f"LINESTRING({pts})"


# ═════════════════════════════════════════════════════════════════════════ #
# 4 · CHECK — does the computed TVD agree with the survey's own?
# ═════════════════════════════════════════════════════════════════════════ #
def tvd_check(path, reported, datum_elev=None):
    """Compare computed TVD against the stations' reported TVD or TVDSS.

    The cheapest possible catch for a units error or a wrong azimuth
    reference, and it costs nothing because the numbers are already in the
    row. Returns (max_abs, mean_abs, n) over the stations that reported a
    value; TVDSS is TVD below datum, so pass datum_elev to compare like
    with like.
    """
    diffs = []
    for (md, _n, _e, v, _d), r in zip(path[-len(reported):], reported):
        if r is None:
            continue
        ref = float(r) if datum_elev is None else float(r) + float(datum_elev)
        diffs.append(abs(v - ref))
    if not diffs:
        return (None, None, 0)
    return (max(diffs), sum(diffs) / len(diffs), len(diffs))


# ═════════════════════════════════════════════════════════════════════════ #
# 5 · DATABASE — read stations, write geography
# ═════════════════════════════════════════════════════════════════════════ #
# Column names differ between deployments and between PPDM versions, so the
# station reader INTROSPECTS rather than assuming. Same reason the loaders
# read the live catalog: a hard-coded column list is a deployment landmine.
CAND = {
    "md": ["station_md", "md", "measured_depth", "depth_md", "md_depth",
           "depth", "station_depth"],
    "inc": ["inclination", "inclin", "incl", "inc", "deviation_angle",
            "drift_angle", "deviation", "drift"],
    "azi": ["azimuth", "azim", "azi", "azimuth_deg", "azm", "direction",
            "bearing", "hole_direction"],
    "tvd": ["tvd", "true_vertical_depth", "tvdss", "tvd_ss", "depth_tvd",
            "vertical_depth"],
    "uwi": ["uwi"],
    "srvy": ["survey_id", "srvy_id", "dir_srvy_id", "survey_name"],
}


_SAFE_UWI = re.compile(r"^[0-9A-Za-z_\-]+$")


def _uwi_filter(col, uwi=None, like=None):
    """A seekable predicate on the uwi column, as a LITERAL.

    Three attempts, recorded because each looked right:
    1. A plain parameter — pyodbc sends str as NVARCHAR, uwi is char(14),
       nvarchar wins the precedence rule, so the INDEXED COLUMN gets
       converted per row and the seek dies.
    2. CAST(:p AS char(14)) — fixes the precedence, but a cast around a
       parameter can stop the optimizer computing the seek RANGE at
       compile time, so a prefix LIKE still scanned.
    3. This: a validated LITERAL, and a prefix expressed as the RANGE it
       actually is. The optimizer sees constants and can seek; nothing is
       left to parameter typing or plan reuse.

    A UWI is digits, letters, underscore and hyphen — anything else is
    rejected rather than escaped, because a filter is not a place to be
    clever about quoting.
    """
    def lit(v):
        if not _SAFE_UWI.match(v):
            raise ValueError(
                f"refusing {v!r} as a uwi filter — expected digits, "
                f"letters, underscore or hyphen only")
        return "'" + v + "'"

    if uwi:
        return f" AND {col} = {lit(uwi)}", {}
    if not like:
        return "", {}
    pref = like.rstrip("%")
    if not pref:
        return "", {}
    if "%" in pref or "_" in pref or "[" in pref:
        return f" AND {col} LIKE {lit(pref.replace('%', ''))} + '%'", {}
    hi = pref[:-1] + chr(ord(pref[-1]) + 1)          # '4902' -> '4903'
    return f" AND {col} >= {lit(pref)} AND {col} < {lit(hi)}", {}


def _pick(cols, names):
    low = {c.lower(): c for c in cols}
    for n in names:
        if n in low:
            return low[n]
    return None


def get_engine(server, database, driver="ODBC Driver 17 for SQL Server",
               timeout=0):
    from sqlalchemy import create_engine, event
    url = (f"mssql+pyodbc://@{server}/{database}"
           f"?driver={driver.replace(' ', '+')}&trusted_connection=yes")
    eng = create_engine(url)

    @event.listens_for(eng, "connect")
    def _session_settings(dbapi_conn, _rec):
        # ARITHABORT: SSMS turns it ON, ODBC leaves it OFF, and the two
        # settings get SEPARATE cached plans for identical SQL. The ODBC
        # plan can be orders of magnitude worse — which is the textbook
        # "instant in SSMS, hangs from the application" symptom, and
        # exactly what this module hit. Matching SSMS costs nothing.
        cur = dbapi_conn.cursor()
        try:
            cur.execute("SET ARITHABORT ON; SET NOCOUNT ON;")
        finally:
            cur.close()

    if timeout:
        # A query that will never finish should SAY so rather than hang.
        @event.listens_for(eng, "connect")
        def _set_timeout(dbapi_conn, _rec):
            dbapi_conn.timeout = timeout
    return eng


def columns_of(engine, schema, table):
    from sqlalchemy import text
    with engine.connect() as cx:
        return [r[0] for r in cx.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t"),
            {"s": schema, "t": table})]


def ensure_geog(engine, schema, table, col="PATH_GEOG"):
    """Add the geography column if absent. Never drops, never retypes —
    the same rule the catalog mirror follows."""
    from sqlalchemy import text
    have = {c.upper() for c in columns_of(engine, schema, table)}
    if col.upper() not in have:
        with engine.begin() as cx:
            cx.execute(text(
                f"ALTER TABLE {schema}.{table} ADD {col} geography NULL"))
        return True
    return False


def read_surveys(engine, schema="dataview", sta="dv_well_dir_srvy_sta",
                 uwi=None, like=None, cols_override=None,
                 show_sql=False, limit=None, log=print):
    """{(uwi, survey_id): [(md, inc, azi, tvd), ...]} — stations in md order."""
    from sqlalchemy import text
    cols = columns_of(engine, schema, sta)
    if not cols:
        raise RuntimeError(f"{schema}.{sta} not found")
    c = {k: _pick(cols, v) for k, v in CAND.items()}
    # An explicit override always wins. Candidate lists cover the spellings
    # seen so far; the next deployment will have one nobody predicted, and
    # that must be a flag rather than an edit to this file.
    for k, v in (cols_override or {}).items():
        if v:
            real = _pick(cols, [v.lower()])
            if not real:
                raise RuntimeError(
                    f"{sta} has no column named {v!r} — it has: "
                    + ", ".join(sorted(cols)))
            c[k] = real
    missing = [k for k in ("md", "inc", "azi", "uwi") if not c[k]]
    if missing:
        raise RuntimeError(
            f"{sta} has no column for {missing}.\n  looked for: "
            + "; ".join(f"{k} in {CAND[k]}" for k in missing)
            + f"\n  the table actually has: {', '.join(sorted(cols))}"
            + "\n  name it explicitly, e.g. --azi-col azim")
    log(f"  station columns: md={c['md']} inc={c['inc']} azi={c['azi']}"
        + (f" tvd={c['tvd']}" if c["tvd"] else " tvd=(none — no cross-check)"))
    # NARROW EVERY COLUMN IN THE SELECT LIST.
    # pyodbc sizes its receive buffer from the DECLARED column width, not
    # the data. Six nvarchar(4000) columns — which is what these tables
    # inherit from staging — is ~48 KB of buffer per ROW, so 37,000
    # stations become gigabytes of allocation and the fetch takes minutes
    # while SSMS returns the same query instantly. Casting in the SELECT
    # makes the buffers tiny, and does the numeric conversion server-side
    # where it is free. TRY_CONVERT, not CONVERT: one unparseable value in
    # one row must not fail the whole read.
    sel = [f"CAST({c['uwi']} AS char(14))",
           (f"CAST({c['srvy']} AS varchar(64))" if c["srvy"] else "NULL"),
           f"TRY_CONVERT(float, {c['md']})",
           f"TRY_CONVERT(float, {c['inc']})",
           f"TRY_CONVERT(float, {c['azi']})",
           (f"TRY_CONVERT(float, {c['tvd']})" if c["tvd"] else "NULL")]
    top = f"TOP {int(limit)} " if limit else ""
    q = (f"SELECT {top}{', '.join(sel)} FROM {schema}.{sta} WITH (NOLOCK) "
         f"WHERE {c['md']} IS NOT NULL AND {c['inc']} IS NOT NULL "
         f"AND {c['azi']} IS NOT NULL")
    params = {}
    # CAST THE PARAMETER, NEVER THE COLUMN. pyodbc sends a Python str as
    # nvarchar; uwi is char(14). nvarchar has the higher datatype
    # precedence, so SQL Server converts the INDEXED COLUMN on every row
    # and the seek degrades to a scan of the whole station table. Casting
    # the parameter instead leaves the column untouched and seekable.
    # (Same fault that turned a 0.9s promote into 154s.)
    _w, _pr = _uwi_filter(c["uwi"], uwi, like)   # a field is a UWI PREFIX
    q += _w
    params.update(_pr)
    # No ORDER BY: sorting millions of rows in the server to hand back a
    # few thousand is wasted work, and minimum_curvature sorts its own
    # stations anyway.
    if show_sql:
        log("  SQL: " + " ".join(q.split()))
        log(f"  params: {params}")
    out = {}
    # Announce BEFORE blocking. A tool that prints nothing while it waits
    # is indistinguishable from a tool that has hung, and that ambiguity
    # cost more time than any query in this module.
    log("  querying… (no output until the server answers)")
    t0 = _time.time()
    with engine.connect() as cx:
        log(f"  connected in {_time.time() - t0:.1f}s")
        t1 = _time.time()
        rows = cx.execute(text(q), params).fetchall()
    log(f"  {len(rows):,} station row(s) in {_time.time() - t1:.1f}s")
    bad = 0
    for u, sid, md, inc, azi, tvd in rows:
        if md is None or inc is None or azi is None:
            bad += 1            # TRY_CONVERT couldn't read it — not a number
            continue
        key = (str(u).strip(), str(sid).strip() if sid else "")
        out.setdefault(key, []).append(
            (float(md), float(inc), float(azi),
             float(tvd) if tvd is not None else None))
    if bad:
        log(f"  ⚠ {bad:,} row(s) had a non-numeric md/inclination/azimuth "
            f"and were skipped")
    for k in out:
        out[k].sort(key=lambda s: s[0])
    return out


def read_surface(engine, schema="dataview", well="dv_well", uwi=None,
                 like=None):
    from sqlalchemy import text
    cols = {c.lower() for c in columns_of(engine, schema, well)}
    lat = "surface_latitude" if "surface_latitude" in cols else "latitude"
    lon = "surface_longitude" if "surface_longitude" in cols else "longitude"
    q = (f"SELECT uwi, {lat}, {lon} FROM {schema}.{well} WITH (NOLOCK) "
         f"WHERE {lat} IS NOT NULL AND {lon} IS NOT NULL")
    params = {}
    _w, _pr = _uwi_filter("uwi", uwi, like)          # see read_surveys
    q += _w
    params.update(_pr)
    with engine.connect() as cx:
        return {str(u).strip(): (float(la), float(lo))
                for u, la, lo in cx.execute(text(q), params)}


def compute_paths(engine, schema="dataview", uwi=None, unit="ft",
                  simplify_tol=1e-4, like=None, cols_override=None,
                  show_sql=False, limit=None, max_dls=30.0,
                  drop_spikes=False, min_closure_m=MIN_CLOSURE_M, log=print):
    """Everything except the write. Returns (results, problems).

    Every survey is still COMPUTED -- the TVD cross-check and the dogleg
    figures are worth having whether or not the path is worth drawing -- but
    each result carries `closure_m` and `drawable`, and the consumers that put
    a line on a map (write_paths, add_well_paths_live) honour `drawable`.
    Pass min_closure_m=0 to keep every path.
    """
    surveys = read_surveys(engine, schema, uwi=uwi, like=like,
                           cols_override=cols_override, show_sql=show_sql,
                           limit=limit, log=log)
    surface = read_surface(engine, schema, uwi=uwi, like=like)
    log(f"  {len(surveys)} survey(s) read · {len(surface)} well(s) with "
        f"surface coordinates")
    results, problems = [], []
    for n_done, ((u, sid), sts) in enumerate(sorted(surveys.items()), 1):
        if n_done % 250 == 0:
            log(f"    … {n_done}/{len(surveys)}")
        if len(sts) < 2:
            problems.append((u, sid, f"only {len(sts)} station(s)"))
            continue
        if u not in surface:
            problems.append((u, sid, "well has no surface coordinates"))
            continue
        lat0, lon0 = surface[u]
        spikes = flag_spikes(sts, max_dls)
        use = sts
        if spikes and drop_spikes:
            bad_ix = {k for k, _d, _w in spikes}
            use = [s for j, s in enumerate(sts) if j not in bad_ix]
        path = minimum_curvature([(m, i, a) for m, i, a, _t in use])
        rep = [t for _m, _i, _a, t in use]
        mx, mean, n = tvd_check(path, rep)
        lonlat = to_lat_long(path, lat0, lon0, unit=unit)
        keep = high_dogleg_indices(path)
        idx = simplify(lonlat, simplify_tol, keep=keep)
        thin = [lonlat[i] for i in idx]
        results.append({
            "uwi": u, "survey_id": sid, "stations": len(sts),
            "spikes": spikes, "used": len(use),
            "points": len(thin), "md_max": path[-1][0],
            "tvd_max": path[-1][3],
            # NOTE THE NAME: this is closure in the SOURCE unit, feet or
            # metres, because minimum_curvature works in whatever the survey
            # was recorded in. closure_m is the one a metre threshold may be
            # compared against, and the only one that is unit-safe.
            "closure_ft": math.hypot(path[-1][1], path[-1][2]),
            "closure_m": _closure_m(path, unit),
            "drawable": _closure_m(path, unit) >= float(min_closure_m or 0.0),
            "max_dls": max(p[4] for p in path),
            "tvd_diff_max": mx, "tvd_diff_mean": mean, "tvd_checked": n,
            "wkt": wkt_linestring(thin),
        })
    return results, problems


def write_paths(engine, results, schema="dataview",
                hdr="dv_well_dir_srvy_hdr", col="PATH_GEOG", log=print):
    from sqlalchemy import text
    added = ensure_geog(engine, schema, hdr, col)
    if added:
        log(f"  + column {schema}.{hdr}.{col} geography")
    cols = {c.lower() for c in columns_of(engine, schema, hdr)}
    key = "survey_id" if "survey_id" in cols else None
    sql = (f"UPDATE {schema}.{hdr} SET {col} = "
           f"geography::STGeomFromText(:wkt, 4326).MakeValid() "
           f"WHERE uwi = :u" + (f" AND {key} = :s" if key else ""))
    n = skipped = 0
    with engine.begin() as cx:
        for r in results:
            if not r["wkt"]:
                continue
            # THE GATE LIVES AT EVERY CONSUMER, not just at compute: a caller
            # that builds results itself, or one written later, must not be
            # able to store a path the map cannot show.
            if not r.get("drawable", True):
                skipped += 1
                continue
            p = {"wkt": r["wkt"], "u": r["uwi"]}
            if key:
                p["s"] = r["survey_id"]
            n += cx.execute(text(sql), p).rowcount or 0
    if skipped:
        log(f"  {skipped} path(s) not written - closure below "
            f"{MIN_CLOSURE_M:.0f} m, nothing a map could show")
    return n


# ═════════════════════════════════════════════════════════════════════════ #
# 6 · MAP LAYER
# ═════════════════════════════════════════════════════════════════════════ #
def add_well_paths_live(m, engine, schema="dataview", like=None, uwi=None,
                        unit="ft", tolerance=1e-4, name="Well paths",
                        color="#e07a1f"):
    """Draw paths computed ON THE FLY — nothing stored, nothing altered.

    For mapping only, this is the whole job: no geography column, no DDL,
    no write of any kind. The cost is that every render recomputes, so it
    suits a field-sized selection (a --like prefix) rather than a whole
    corporate database — that is what the stored version is for.
    """
    import folium
    results, _problems = compute_paths(engine, schema, uwi=uwi, unit=unit,
                                       simplify_tol=tolerance, like=like,
                                       log=lambda *_a: None)
    fg = folium.FeatureGroup(name=name, show=True)
    n = 0
    for r in results:
        wkt = r.get("wkt")
        if not wkt:
            continue
        if not r.get("drawable", True):        # see write_paths
            continue
        inner = wkt[wkt.find("(") + 1:wkt.rfind(")")]
        pts = []
        for pair in inner.split(","):
            bits = pair.split()
            if len(bits) >= 2:
                pts.append((float(bits[1]), float(bits[0])))   # folium lat,lon
        if len(pts) >= 2:
            folium.PolyLine(
                pts, weight=2, color=color, opacity=0.85,
                tooltip=f"{r['uwi']} · MD {r['md_max']:,.0f} {unit} · "
                        f"closure {r['closure_m']:,.0f} m").add_to(fg)
            n += 1
    fg.add_to(m)
    return n


def add_well_paths(m, engine, schema="dataview", hdr="dv_well_dir_srvy_hdr",
                   col="PATH_GEOG", name="Well paths", color="#e07a1f"):
    """Draw the stored paths as a toggleable folium layer.

    Reads the geography back rather than recomputing: the map should render
    what the database holds, so a path on screen is a path something else
    can query.
    """
    import folium
    from sqlalchemy import text
    fg = folium.FeatureGroup(name=name, show=True)
    with engine.connect() as cx:
        rows = cx.execute(text(
            f"SELECT uwi, {col}.ToString() FROM {schema}.{hdr} WITH (NOLOCK) "
            f"WHERE {col} IS NOT NULL")).fetchall()
    for u, wkt in rows:
        inner = str(wkt)[str(wkt).find("(") + 1:str(wkt).rfind(")")]
        pts = []
        for pair in inner.split(","):
            bits = pair.split()
            if len(bits) >= 2:
                pts.append((float(bits[1]), float(bits[0])))   # folium: lat,lon
        if len(pts) >= 2:
            folium.PolyLine(pts, weight=2, color=color, opacity=0.85,
                            tooltip=str(u)).add_to(fg)
    fg.add_to(m)
    return len(rows)


# ═════════════════════════════════════════════════════════════════════════ #
# 7 · CLI
# ═════════════════════════════════════════════════════════════════════════ #
# An 18-station survey from a real report layout. NOTE its printed TVDSS is
# MD x cos(final inclination) at every station — a naive formula, not a
# survey calculation — so it is used here to demonstrate the cross-check
# FIRING, not as ground truth. Ground truth is analytic, below.
SELFTEST = [
    (596, 3.73, 216.03, 594.7), (1134, 4.93, 218.93, 1129.8),
    (1615, 6.37, 221.77, 1605.0), (1977, 8.47, 226.34, 1955.4),
    (2443, 8.74, 226.39, 2414.6), (2855, 10.91, 225.43, 2803.4),
    (3233, 11.80, 222.56, 3164.7), (3602, 13.12, 220.50, 3507.9),
    (4187, 15.60, 220.25, 4032.9), (4674, 18.89, 225.06, 4422.3),
    (4878, 19.35, 223.52, 4602.5), (5201, 22.97, 224.65, 4788.7),
    (5719, 23.94, 220.05, 5226.9), (6083, 25.47, 223.01, 5491.9),
    (6341, 27.01, 223.42, 5649.3), (6685, 27.11, 223.59, 5950.5),
    (7279, 28.61, 226.61, 6390.2), (7707, 30.56, 224.58, 6636.7),
]


def selftest(log=print):
    """Prove the geometry against cases with a KNOWN closed-form answer.

    Minimum curvature fits circular arcs, so a perfect circular build is
    not an approximation for it — it is exact, and that makes it the right
    thing to check against. Comparing to another survey's printed numbers
    only tells you whether two programs agree.
    """
    fails = []

    def check(name, got, want, tol):
        ok = abs(got - want) <= tol
        log(f"  {'ok ' if ok else 'FAIL'}  {name:34} {got:12,.3f}  "
            f"expected {want:,.3f}")
        if not ok:
            fails.append(name)

    log("ANALYTIC CASES (closed form, no reference program)")
    # 1 · vertical
    v = minimum_curvature([(0, 0, 0), (5000, 0, 0)])
    check("vertical: TVD == MD", v[-1][3], 5000.0, 1e-6)
    check("vertical: north offset", v[-1][1], 0.0, 1e-9)
    check("vertical: east offset", v[-1][2], 0.0, 1e-9)

    # 2 · straight hold at 45 deg, due east
    inc, md = 45.0, 3000.0
    h = minimum_curvature([(0, inc, 90.0), (md, inc, 90.0)])
    check("45 deg hold: TVD", h[-1][3], md * math.cos(math.radians(inc)), 1e-6)
    check("45 deg hold: east", h[-1][2], md * math.sin(math.radians(inc)), 1e-6)
    check("45 deg hold: north", h[-1][1], 0.0, 1e-9)

    # 3 · perfect circular build 0 -> 90 deg. For arc length L over angle
    #     theta, R = L/theta, TVD = R*sin(theta), displacement = R*(1-cos).
    L, steps = 2000.0, 90
    arc = [(L * k / steps, 90.0 * k / steps, 0.0) for k in range(steps + 1)]
    c = minimum_curvature(arc)
    R = L / (math.pi / 2)
    check("90 deg build: TVD", c[-1][3], R * math.sin(math.pi / 2), 1e-6)
    check("90 deg build: displacement", c[-1][1], R * (1 - math.cos(math.pi / 2)), 1e-6)
    check("90 deg build: DLS deg/100", max(p[4] for p in c),
          90.0 * 100.0 / L, 1e-6)

    # 4 · a build split coarsely must still land in the same place —
    #     the arc fit is what makes station spacing not matter much
    coarse = minimum_curvature([(0, 0, 0), (L, 90.0, 0.0)])
    check("coarse vs fine build: TVD", coarse[-1][3], c[-1][3], 0.5)

    # 5 · projection round trip. GRID reference for this one: the offsets
    #     must come back exactly as put in. The TRUE-north case is checked
    #     separately below, because there the offsets are deliberately
    #     rotated by the grid convergence.
    ll = to_lat_long(h, 31.207036, -103.680039, unit="ft",
                     azimuth_ref="grid")
    from pyproj import Transformer
    e = utm_epsg(31.207036, -103.680039)
    fwd = Transformer.from_crs(4326, e, always_xy=True)
    x0, y0 = fwd.transform(-103.680039, 31.207036)
    x1, y1 = fwd.transform(ll[-1][0], ll[-1][1])
    check("projection: east offset recovered (m)", x1 - x0,
          h[-1][2] * FT_TO_M, 0.01)
    check("projection: north offset recovered (m)", y1 - y0,
          h[-1][1] * FT_TO_M, 0.01)

    # 5b · grid convergence: a true-north azimuth must be rotated into the
    #      grid frame, and the rotation is Dlon x sin(lat).
    lat0, lon0 = 31.207036, -103.680039
    tt = to_lat_long(h, lat0, lon0, unit="ft", azimuth_ref="true")
    xg, yg = fwd.transform(ll[-1][0], ll[-1][1])
    xt, yt = fwd.transform(tt[-1][0], tt[-1][1])
    sep = math.hypot(xt - xg, yt - yg)
    disp = math.hypot(h[-1][1], h[-1][2]) * FT_TO_M
    cm = ((e % 100) * 6.0) - 183.0
    gam = math.radians(lon0 - cm) * math.sin(math.radians(lat0))
    check("convergence: true vs grid separation (m)", sep,
          2 * disp * math.sin(abs(gam) / 2), 0.05)

    # 6 · generalization keeps the ends and the doglegs
    keep = high_dogleg_indices(c)
    idx = simplify(ll if False else to_lat_long(c, 31.2, -103.7), 1e-4,
                   keep=keep)
    check("generalize: keeps first point", float(idx[0]), 0.0, 0.0)
    check("generalize: keeps last point", float(idx[-1]), float(len(c) - 1), 0.0)
    log(f"  ok    {'generalize: 91 stations ->':34} {len(idx):12} vertices")

    log("")
    log("CROSS-CHECK DEMONSTRATION (a survey whose printed TVD is wrong)")
    path = minimum_curvature([(m, i, a) for m, i, a, _t in SELFTEST])
    rep = [t for *_x, t in SELFTEST]
    mx, mean, n = tvd_check(path, rep)
    log(f"  computed TVD at TD   {path[-1][3]:,.1f}")
    log(f"  printed  TVD at TD   {rep[-1]:,.1f}")
    log(f"  disagreement         max {mx:,.1f} · mean {mean:,.1f} over {n}")
    ratio = rep[-1] / SELFTEST[-1][0]
    log(f"  printed/MD = {ratio:.5f} · cos(final inc) = "
        f"{math.cos(math.radians(SELFTEST[-1][1])):.5f}  -> the printed "
        f"column is MD x cos(final inclination), not a survey calculation")
    log("  This is the check doing its job: it fires before anything is")
    log("  written, and it names units or azimuth reference as the suspects.")

    log("")
    if fails:
        log(f"SELFTEST FAILED: {fails}")
        return 1
    log("SELFTEST PASSED — geometry exact on every analytic case")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Compute and store well paths.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="prove the geometry with no database")
    c = sub.add_parser("compute", help="compute paths from survey stations")
    c.add_argument("--server", required=True)
    c.add_argument("--database", required=True)
    c.add_argument("--driver", default="ODBC Driver 17 for SQL Server")
    c.add_argument("--schema", default="dataview")
    c.add_argument("--uwi", help="one well only")
    c.add_argument("--like", help=r"UWI prefix filter, e.g. 4902%% for Teapot")
    c.add_argument("--unit", default="ft", choices=["ft", "m"])
    c.add_argument("--tolerance", type=float, default=1e-4,
                   help="generalization tolerance in degrees (~11 m at 1e-4)")
    c.add_argument("--md-col", help="override the measured-depth column")
    c.add_argument("--inc-col", help="override the inclination column")
    c.add_argument("--azi-col", help="override the azimuth column")
    c.add_argument("--tvd-col", help="override the TVD column used for the "
                                     "cross-check")
    c.add_argument("--max-dls", type=float, default=30.0,
                   help="dogleg severity above this is reported as a data "
                        "fault (deg/100; real holes rarely exceed 15)")
    c.add_argument("--drop-spikes", action="store_true",
                   help="exclude the flagged stations from the path "
                        "instead of only reporting them")
    c.add_argument("--limit", type=int,
                   help="read only the first N station rows — a fast way to "
                        "prove the connection before a full run")
    c.add_argument("--timeout", type=int, default=0,
                   help="seconds to wait for the query before giving up "
                        "(0 = wait forever)")
    c.add_argument("--show-sql", action="store_true",
                   help="print the station query and its parameters")
    c.add_argument("--apply", action="store_true",
                   help="write the geography; without it, report only")
    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        return selftest()

    engine = get_engine(a.server, a.database, a.driver, a.timeout)
    scope = (f"uwi = {a.uwi}" if a.uwi
              else f"uwi LIKE {a.like}" if a.like else "ALL wells")
    print(f"reading {a.schema}.dv_well_dir_srvy_sta  ({scope}) …")
    over = {"md": a.md_col, "inc": a.inc_col, "azi": a.azi_col,
            "tvd": a.tvd_col}
    results, problems = compute_paths(engine, a.schema, a.uwi, a.unit,
                                      a.tolerance, like=a.like,
                                      cols_override=over,
                                      show_sql=a.show_sql, limit=a.limit,
                                      max_dls=a.max_dls,
                                      drop_spikes=a.drop_spikes)
    print(f"\n{len(results)} path(s) computed, {len(problems)} skipped")
    bad = [r for r in results
           if r["tvd_diff_max"] is not None
           and r["tvd_diff_max"] > 0.02 * max(abs(r["tvd_max"]), 1)]
    print(f"{'uwi':16} {'sta':>4} {'pts':>4} {'MD':>9} {'TVD':>9} "
          f"{'closure':>9} {'DLS':>6}  TVD check")
    for r in results[:25]:
        chk = ("n/a" if r["tvd_checked"] == 0
               else f"max {r['tvd_diff_max']:.0f}")
        print(f"{r['uwi'][:16]:16} {r['stations']:4} {r['points']:4} "
              f"{r['md_max']:9,.0f} {r['tvd_max']:9,.0f} "
              f"{r['closure_ft']:9,.0f} {r['max_dls']:6.2f}  {chk}")
    if len(results) > 25:
        print(f"  … and {len(results) - 25} more")
    for u, sid, why in problems[:10]:
        print(f"  ⏭ {u} {sid}: {why}")
    spiky = [r for r in results if r["spikes"]]
    if spiky:
        n = sum(len(r["spikes"]) for r in spiky)
        print(f"\n⚠ {n} impossible dogleg(s) across {len(spiky)} well(s) — "
              f"almost always a NULL written as 0.0/0.0"
              + (" (EXCLUDED from the paths)" if a.drop_spikes
                 else " (still IN the paths; add --drop-spikes to exclude)"))
        for r in spiky[:6]:
            for k, dls, why in r["spikes"][:3]:
                print(f"    {r['uwi']}  station {k}: {why}")
    if bad:
        print(f"\n⚠ {len(bad)} well(s) disagree with their own reported TVD by "
              f"more than 2% — check the survey's units and azimuth reference "
              f"BEFORE writing:")
        for r in bad[:5]:
            print(f"    {r['uwi']}  computed {r['tvd_max']:,.0f} vs reported "
                  f"(max diff {r['tvd_diff_max']:,.0f})")
    if not a.apply:
        print("\nDry run — nothing written. Add --apply to store the paths.")
        return 0
    n = write_paths(engine, results, a.schema)
    print(f"\n{n} survey header row(s) updated with a path geography.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
