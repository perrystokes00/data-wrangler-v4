r"""Stamp each tract with the ground elevation at its centre, once.

Same pattern as assign_tract_townships, the county stamp and h3_refresh: a
tract does not move, so the elevation is answered once and read forever.
After this, "leases below 5,000 ft" is a numeric column test that rides the
client-side filter path the lease strip already uses for minimum acres.

SOURCE: USGS NED 10-metre, served in batches of 100 points by OpenTopoData.
The USGS Elevation Point Query Service is the same data and is authoritative,
but takes ONE point per request -- 24,178 requests against a government
endpoint to fill one column is not a reasonable way to ask. The batch service
is 242 requests for the same answer, and --verify spot-checks its results
against EPQS so the shortcut is proved rather than trusted: at 43.0 N
106.5 W both return 5,368 ft.

METRES IN, BOTH STORED. The service answers in metres; a land man reads feet.
Storing only one of them would mean every reader converting, and the first
one to use 3.28 instead of 3.28084 introduces a discrepancy nothing catches.

A WORD ON WYOMING: the state's low point is about 3,100 ft and its mean is
near 6,700, so "below 100 ft" matches nothing here. The filter earns its keep
on elevation BANDS -- basin against mountain, which is what drives access and
drilling cost -- and on the day Gulf Coast leases are loaded.

    python tools/stamp_elevation.py                  # what it would do
    python tools/stamp_elevation.py --apply
    python tools/stamp_elevation.py --verify         # spot-check vs USGS
    python tools/stamp_elevation.py --clear --apply
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BATCH = "https://api.opentopodata.org/v1/ned10m"
EPQS = "https://epqs.nationalmap.gov/v1/json"
COLS = [("elevation_m", "float"), ("elevation_ft", "float")]
FT_PER_M = 3.280839895            # exact, not 3.28


def _get(url, timeout=60):
    req = urllib.request.Request(
        url, headers={"User-Agent": "data-wrangler-v4/1.0 (elevation stamp)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def epqs_ft(lat, lon):
    """One authoritative reading, for checking the batch service."""
    q = urllib.parse.urlencode({"x": lon, "y": lat, "units": "Feet"})
    try:
        return float(_get("%s?%s" % (EPQS, q))["value"])
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--chunk", type=int, default=100,
                    help="points per request (the service allows 100)")
    ap.add_argument("--pause", type=float, default=1.0,
                    help="seconds between requests -- the public instance "
                         "asks for one call a second")
    ap.add_argument("--limit", type=int, default=0,
                    help="stamp only the first N tracts (for a trial run)")
    ap.add_argument("--verify", action="store_true",
                    help="spot-check ten stamped tracts against USGS EPQS")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    from sqlalchemy import text as t
    from dataview.core.dw_utils import make_engine
    eng = make_engine(a.database)

    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
        if cx.execute(t("SELECT OBJECT_ID('dataview.dv_land_tract_geom')"
                        )).scalar() is None:
            print("dataview.dv_land_tract_geom does not exist")
            return 2
        for col, typ in COLS:
            if cx.execute(t("SELECT COL_LENGTH('dataview.dv_land_tract_geom',"
                            " :c)"), {"c": col}).scalar() is None:
                if a.apply:
                    cx.execute(t("ALTER TABLE dataview.dv_land_tract_geom "
                                 "ADD %s %s NULL" % (col, typ)))
                    print("   added dv_land_tract_geom.%s" % col)
                else:
                    print("   would add dv_land_tract_geom.%s %s" % (col, typ))

    if a.clear:
        with eng.begin() as cx:
            n = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_land_tract_geom"
                             " WHERE elevation_m IS NOT NULL")).scalar()
            if a.apply:
                cx.execute(t("UPDATE dataview.dv_land_tract_geom "
                             "SET elevation_m = NULL, elevation_ft = NULL"))
        print("%s %s stamp(s)" % ("cleared" if a.apply else "would clear",
                                  format(n, ",")))
        return 0

    if a.verify:
        with eng.connect() as cx:
            rows = cx.execute(t("""
                SELECT TOP 10 tract_id, elevation_ft,
                       geog.EnvelopeCenter().Lat, geog.EnvelopeCenter().Long
                  FROM dataview.dv_land_tract_geom
                 WHERE elevation_ft IS NOT NULL
                 ORDER BY tract_id""")).fetchall()
        if not rows:
            print("nothing stamped yet -- run with --apply first")
            return 2
        print("%-12s %10s %10s %8s" % ("tract", "stored ft", "USGS ft", "diff"))
        worst = 0.0
        for tid, ft, la, lo in rows:
            u = epqs_ft(float(la), float(lo))
            if u is None:
                print("%-12s %10.1f %10s %8s" % (str(tid)[:12], ft, "n/a", "-"))
                continue
            d = abs(float(ft) - u)
            worst = max(worst, d)
            print("%-12s %10.1f %10.1f %8.1f" % (str(tid)[:12], ft, u, d))
            time.sleep(0.2)
        print("\nworst disagreement: %.1f ft" % worst)
        # 10m DEM cells differ slightly between services at the same point;
        # tens of feet in steep country is the sampling grid, not an error.
        print("(both read USGS NED; a few feet is the 10 m cell, not a bug)")
        return 0

    with eng.connect() as cx:
        tracts = cx.execute(t("""
            SELECT tract_id, geog.EnvelopeCenter().Lat,
                   geog.EnvelopeCenter().Long
              FROM dataview.dv_land_tract_geom
             WHERE geog IS NOT NULL
             ORDER BY tract_id""")).fetchall()
    if a.limit:
        tracts = tracts[:a.limit]
    n_req = (len(tracts) + a.chunk - 1) // a.chunk
    print("tracts %s   %s request(s) of %s, %.1fs apart  (~%.0f min)"
          % (format(len(tracts), ","), format(n_req, ","), a.chunk, a.pause,
             (n_req * a.pause) / 60.0))

    if not a.apply:
        print("\nDRY RUN -- re-run with --apply.")
        return 0

    got, failed = [], 0
    for i in range(0, len(tracts), a.chunk):
        part = tracts[i:i + a.chunk]
        locs = "|".join("%.6f,%.6f" % (float(r[1]), float(r[2])) for r in part)
        try:
            d = _get("%s?locations=%s" % (BATCH, urllib.parse.quote(locs, safe="|,")))
            res = d.get("results") or []
            if len(res) != len(part):
                raise ValueError("asked %d, got %d" % (len(part), len(res)))
            for r, e in zip(part, res):
                v = e.get("elevation")
                got.append((r[0], None if v is None else float(v)))
        except Exception as exc:
            # NOT swallowed. A silent gap here becomes a NULL that reads as
            # "no data" forever, and the next run would skip it.
            failed += len(part)
            print("   batch at %d failed: %s" % (i, str(exc)[:120]))
        if (i // a.chunk) % 20 == 0:
            print("   %s / %s" % (format(len(got), ","),
                                  format(len(tracts), ",")))
        time.sleep(a.pause)

    print("\nreceived %s   failed %s" % (format(len(got), ","),
                                         format(failed, ",")))
    if not got:
        return 2

    pending = 0
    cxm = eng.begin()
    cx = cxm.__enter__()
    try:
        for tid, m in got:
            cx.execute(t("""UPDATE dataview.dv_land_tract_geom
                               SET elevation_m = :m, elevation_ft = :f
                             WHERE tract_id = :i"""),
                       {"i": tid, "m": m,
                        "f": None if m is None else round(m * FT_PER_M, 2)})
            pending += 1
            if pending >= 1000:          # never hold the map's table
                cxm.__exit__(None, None, None)
                pending = 0
                cxm = eng.begin()
                cx = cxm.__enter__()
    finally:
        cxm.__exit__(None, None, None)

    with eng.connect() as cx:
        r = cx.execute(t("""SELECT COUNT(*), MIN(elevation_ft),
                                   AVG(elevation_ft), MAX(elevation_ft)
                              FROM dataview.dv_land_tract_geom
                             WHERE elevation_ft IS NOT NULL""")).first()
    print("stamped %s   min %.0f ft   mean %.0f ft   max %.0f ft"
          % (format(r[0], ","), r[1], r[2], r[3]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
