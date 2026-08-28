r"""Download BLM federal oil & gas lease polygons as GeoJSON.

    https://gis.blm.gov/nlsdb/rest/services/HUB/
        BLM_Natl_MLRS_Oil_and_Gas_Leases/FeatureServer/0

Real lease geometry with real attributes -- the thing gen_synthetic_leases.py
was standing in for. Native GeoJSON in EPSG:4326, so nothing is reprojected on
the way in.

FOUR THINGS THIS HANDLES, EACH OF WHICH BITES SILENTLY

 1 PAGING. maxRecordCount is 2,000 and the service answers a larger request
   with `exceededTransferLimit: true` and a short file -- a truncated download
   that looks like a complete one. This pages on resultOffset until the server
   stops saying there is more, and REFUSES to write a file it believes is
   short. Wyoming alone is 103,735 polygons.

 2 THE USER AGENT. gis.blm.gov answers Python-urllib's default with 403
   Forbidden. That reads exactly like a network or permissions problem and is
   neither; it is the UA string. Sent explicitly here so the next person does
   not spend an hour on it.

 3 EPOCH MILLISECOND DATES. EFF_DT / EXP_DT / SALE_DT come back as numbers
   -- 581126400000 is 1988-06-01. Left AS THEY ARRIVE in the raw file, because
   this tool's job is to fetch faithfully; --iso-dates converts them, and the
   summary always says which the file holds.

 4 MIXED Polygon / MultiPolygon. A lease serial covers non-contiguous tracts,
   so both appear in one file. That is real, not corruption, and a loader that
   assumes Polygon drops the multi-tract leases -- which are the big ones.

RING ORIENTATION IS NOT CHECKED HERE and must be checked on load: a clockwise
ring is the planet minus the lease. dv_ loaders already guard this with
ReorientObject() when STArea()/1e6 > 100000; keep that guard.

    python tools/fetch_blm_leases.py --state WY --bbox teapot
    python tools/fetch_blm_leases.py --state WY --bbox natrona --apply
    python tools/fetch_blm_leases.py --state WY --where "CSE_DISP='Authorized'" --apply
"""
import argparse
import datetime as _dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SERVICE = ("https://gis.blm.gov/nlsdb/rest/services/HUB/"
           "BLM_Natl_MLRS_Oil_and_Gas_Leases/FeatureServer/0/query")
# gis.blm.gov returns 403 to the default Python-urllib UA. See note 2 above.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PAGE = 1000                      # under the service's 2,000 cap, kinder on it
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "..", "training", "BLM_Leases")

# (min_lon, min_lat, max_lon, max_lat) -- the envelope order the service wants,
# which is NOT the (min_lat, max_lat, min_lon, max_lon) the map uses. Named
# boxes rather than four numbers on a command line for exactly that reason.
BOXES = {
    "teapot":  (-106.40, 43.20, -106.05, 43.55),
    "natrona": (-107.543526, 42.431094, -106.072669, 43.501362),
}


def county_box(state, county):
    """A county's envelope from us_geo, in the service's lon/lat order.

    us_geo.bbox returns (min_lat, min_lon, max_lat, max_lon) -- the map's
    order. The service wants (min_lon, min_lat, max_lon, max_lat). Converting
    in one named place beats four numbers swapped at a call site: get it wrong
    and the query returns NOTHING rather than raising, because a Wyoming
    latitude and longitude are both plausible numbers in each other's range.
    """
    from dataview.mapping import us_geo as _g
    bb = _g.bbox(state, county)
    if not bb:
        return None
    _mnla, _mnlo, _mxla, _mxlo = bb
    return (_mnlo, _mnla, _mxlo, _mxla)
DATE_FIELDS = ("EFF_DT", "EXP_DT", "SALE_DT", "Created", "Modified")


def _get(params, timeout=120):
    url = SERVICE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def count(where, bbox=None):
    p = {"where": where, "returnCountOnly": "true", "f": "json"}
    if bbox:
        p.update(geometry=",".join(str(x) for x in bbox),
                 geometryType="esriGeometryEnvelope", inSR=4326,
                 spatialRel="esriSpatialRelIntersects")
    return int(_get(p).get("count", 0))


def fetch(where, bbox=None, log=print):
    """Every matching feature, paged. Raises if the server still says there is
    more and we have stopped -- a short file is the failure to avoid."""
    feats, offset = [], 0
    while True:
        p = {"where": where, "outFields": "*", "outSR": 4326, "f": "geojson",
             "resultOffset": offset, "resultRecordCount": PAGE,
             "returnGeometry": "true"}
        if bbox:
            p.update(geometry=",".join(str(x) for x in bbox),
                     geometryType="esriGeometryEnvelope", inSR=4326,
                     spatialRel="esriSpatialRelIntersects")
        d = _get(p)
        got = d.get("features") or []
        feats.extend(got)
        log("   +%-5d  total %s" % (len(got), format(len(feats), ",")))
        if not d.get("exceededTransferLimit") and len(got) < PAGE:
            break
        if not got:
            break
        offset += len(got)
        time.sleep(0.3)          # a public service; do not hammer it
    return feats


def _iso(ms):
    """Epoch milliseconds -> 'yyyy-mm-dd'. Handles dates BEFORE 1970.

    time.gmtime() raises OSError on a negative timestamp on Windows, and the
    first version of this caught that and returned the number unchanged --
    so a 1940 lease kept -925689600000 while a 1999 one became '1999-02-01',
    and the column held two kinds of value with nothing saying which. Half of
    these leases predate 1970, so it was not a rare edge.

    datetime + timedelta has no such limit; the arithmetic is the same either
    side of the epoch.
    """
    if ms in (None, ""):
        return None
    try:
        return (_dt.datetime(1970, 1, 1)
                + _dt.timedelta(milliseconds=float(ms))).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return ms


def summarise(feats, iso):
    kinds, disp, acres = {}, {}, 0.0
    for f in feats:
        g = (f.get("geometry") or {}).get("type", "None")
        kinds[g] = kinds.get(g, 0) + 1
        pr = f.get("properties") or {}
        d = str(pr.get("CSE_DISP") or "(blank)")
        disp[d] = disp.get(d, 0) + 1
        try:
            acres += float(pr.get("RCRD_ACRS") or 0)
        except (TypeError, ValueError):
            pass
    print("\n   geometry types : %s" % kinds)
    print("   dispositions   : %s" % dict(sorted(disp.items(), key=lambda x: -x[1])))
    print("   acres (RCRD)   : %s" % format(int(acres), ","))
    print("   dates          : %s" % ("ISO yyyy-mm-dd (converted)" if iso
                                      else "EPOCH MILLISECONDS, as delivered"))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", default="WY", help="GEO_STATE code (default WY)")
    ap.add_argument("--bbox", default=None,
                    help="named box: " + ", ".join(BOXES) + "; or lon,lat,lon,lat")
    ap.add_argument("--county", default=None,
                    help="county name; its envelope comes from us_geo and is "
                         "combined with --state")
    ap.add_argument("--where", default=None,
                    help="extra SQL, e.g. \"CSE_DISP='Authorized'\"")
    ap.add_argument("--out", default=None, help="output .geojson path")
    ap.add_argument("--iso-dates", action="store_true",
                    help="convert epoch-ms date fields to yyyy-mm-dd")
    ap.add_argument("--apply", action="store_true",
                    help="write the file. Without it, only the count is fetched.")
    a = ap.parse_args()

    where = "GEO_STATE='%s'" % a.state.upper().replace("'", "''")
    if a.where:
        where += " AND (%s)" % a.where
    bbox = None
    if a.county:
        bbox = county_box(a.state.upper(), a.county)
        if not bbox:
            print("us_geo has no county %r in %s" % (a.county, a.state.upper()))
            return 2
    elif a.bbox:
        if a.bbox.lower() in BOXES:
            bbox = BOXES[a.bbox.lower()]
        else:
            try:
                bbox = tuple(float(x) for x in a.bbox.split(","))
                assert len(bbox) == 4
            except Exception:
                print("--bbox must be a name (%s) or lon,lat,lon,lat"
                      % ", ".join(BOXES))
                return 2

    print("\nBLM MLRS oil & gas leases")
    print("   where : %s" % where)
    print("   bbox  : %s" % (str(bbox) + "  (lon,lat,lon,lat)" if bbox else "none"))
    try:
        n = count(where, bbox)
    except Exception as e:
        print("\nCOUNT FAILED: %s: %s" % (type(e).__name__, str(e)[:200]))
        return 1
    print("   count : %s feature(s)" % format(n, ","))
    if not n:
        print("\nNothing matches.")
        return 0
    if not a.apply:
        print("\nCOUNT ONLY -- re-run with --apply to download.")
        return 0

    _tag = ("_" + a.county.lower().replace(" ", "_")) if a.county else (
        "_" + a.bbox.lower() if a.bbox and a.bbox.lower() in BOXES else "")
    out = a.out or os.path.join(
        OUT_DIR, "blm_leases_%s%s.geojson" % (a.state.upper(), _tag))
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    print("\npaging (%d per request):" % PAGE)
    feats = fetch(where, bbox)
    if len(feats) < n:
        # SHORT IS THE FAILURE. A truncated GeoJSON opens, draws, and looks
        # complete -- there is nothing to notice.
        print("\nREFUSING TO WRITE: got %s of %s features. Nothing saved."
              % (format(len(feats), ","), format(n, ",")))
        return 1
    if a.iso_dates:
        for f in feats:
            pr = f.get("properties") or {}
            for k in DATE_FIELDS:
                if k in pr:
                    pr[k] = _iso(pr[k])

    gj = {"type": "FeatureCollection",
          "crs": {"type": "name",
                  "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
          "features": feats}
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(gj, fh)
    summarise(feats, a.iso_dates)
    print("\n   wrote %s features to\n   %s  (%.1f MB)"
          % (format(len(feats), ","), out, os.path.getsize(out) / 1048576.0))
    print("\n   Ring orientation is NOT checked here -- keep the "
          "ReorientObject() guard on load.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
