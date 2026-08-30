r"""Load real PLSS townships from BLM CadNSDI into dv_plss_township.

    https://gis.blm.gov/arcgis/rest/services/Cadastral/
        BLM_Natl_PLSS_CadNSDI/MapServer/1     (PLSS Township)

dv_plss_township exists and holds ZERO rows. That is why the hex-versus-
square comparison had to draw an APPROXIMATE 6-mile grid snapped to the 6th
Principal Meridian -- close enough to judge the shape, not close enough to
label. This is the real survey grid, so a township can be named rather than
guessed at.

WHY IT MATTERS MORE FOR LEASES THAN FOR WELLS. A well is a point and falls
wherever it falls; H3 suits it. Every one of these leases was written as a
legal description ON THIS GRID -- township, range, section -- so the grid is
not a way of summarising leases, it is the coordinate system they were
already expressed in. "T43N R71W is 62% leased" is a sentence; "cell
8526a01bfffffff is 62% leased" is not.

THE SCHEMA WANTS A CENTROID AND A BOX, NOT A POLYGON. dv_plss_township has
centroid_latitude, centroid_longitude and bbox_wkt and no geometry column,
so the full ring is fetched, measured, and thrown away. That is deliberate on
the schema's part: a township is a grid REFERENCE, and anything needing its
exact boundary can ask BLM.

    python tools/load_plss_townships.py                 # count only
    python tools/load_plss_townships.py --state WY --apply
    python tools/load_plss_townships.py --state WY --remove --apply
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SERVICE = ("https://gis.blm.gov/arcgis/rest/services/Cadastral/"
           "BLM_Natl_PLSS_CadNSDI/MapServer/1/query")
# gis.blm.gov answers Python-urllib's default UA with 403 -- the lesson
# fetch_blm_leases already paid for, and the same host.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
FIELDS = ("PLSSID,STATEABBR,TWNSHPNO,TWNSHPDIR,RANGENO,RANGEDIR,"
          "PRINMER,TWNSHPLAB")
PAGE = 500
STAMP = "BLM_CADNSDI"
SOURCE_CODE = "BLM"          # registered in dv_r_source


def _get(params, post=False, timeout=300):
    data = urllib.parse.urlencode(params).encode("utf-8")
    if post:
        req = urllib.request.Request(
            SERVICE, data=data,
            headers={"User-Agent": UA,
                     "Content-Type": "application/x-www-form-urlencoded"})
    else:
        req = urllib.request.Request(SERVICE + "?" + data.decode(),
                                     headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def all_ids(where):
    d = _get({"where": where, "returnIdsOnly": "true", "f": "json"})
    return list(d.get("objectIds") or [])


def fetch(ids, log=print):
    """Township features by OBJECTID -- the same id paging the state lease
    fetch settled on, for the same reason: no offset for the server to
    re-scan, and an exact short-download check."""
    out = []
    for i in range(0, len(ids), PAGE):
        chunk = ids[i:i + PAGE]
        d = _get({"objectIds": ",".join(str(x) for x in chunk),
                  "outFields": FIELDS, "returnGeometry": "true",
                  "outSR": 4326, "f": "geojson",
                  # The ring is measured and discarded, so precision past a
                  # few metres is payload for nothing.
                  "geometryPrecision": 5}, post=True)
        got = d.get("features") or []
        out.extend(got)
        log("   +%-4d total %s of %s" % (len(got), format(len(out), ","),
                                         format(len(ids), ",")))
        time.sleep(0.2)
    return out


def bounds_of(geom):
    """(min_lat, min_lon, max_lat, max_lon) from any polygon shape."""
    def pts(c):
        if not c:
            return
        if isinstance(c[0], (int, float)):
            yield c
            return
        for x in c:
            for p in pts(x):
                yield p
    xs, ys = [], []
    for p in pts((geom or {}).get("coordinates")):
        xs.append(float(p[0]))
        ys.append(float(p[1]))
    if not xs:
        return None
    return (min(ys), min(xs), max(ys), max(xs))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--state", default="WY")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    from sqlalchemy import text as t
    eng = make_engine(a.database)

    if a.remove:
        with eng.begin() as cx:
            n = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_plss_township "
                             "WHERE province_state_id = :s"),
                           {"s": a.state}).scalar()
            if a.apply:
                cx.execute(t("DELETE FROM dataview.dv_plss_township "
                             "WHERE province_state_id = :s"), {"s": a.state})
        print("%s %s township(s) for %s"
              % ("removed" if a.apply else "would remove",
                 format(n, ","), a.state))
        return 0

    where = "STATEABBR='%s'" % a.state.upper().replace("'", "''")
    n = int(_get({"where": where, "returnCountOnly": "true",
                  "f": "json"}).get("count", 0))
    print("BLM CadNSDI townships in %s : %s" % (a.state, format(n, ",")))
    if not a.apply:
        print("\nDRY RUN -- re-run with --apply.")
        return 0

    ids = all_ids(where)
    print("   %s id(s)" % format(len(ids), ","))
    feats = fetch(ids)
    if len(feats) < len(ids):
        print("\nSHORT DOWNLOAD: %s of %s. Refusing to load a partial grid."
              % (format(len(feats), ","), format(len(ids), ",")))
        return 1

    rows, skipped = [], 0
    for f in feats:
        p = f.get("properties") or {}
        pid = (p.get("PLSSID") or "").strip()
        b = bounds_of(f.get("geometry"))
        if not pid or not b:
            skipped += 1
            continue
        mnla, mnlo, mxla, mxlo = b
        rows.append({
            "id": pid[:20],
            "st": (p.get("STATEABBR") or a.state)[:10],
            "tw": ((p.get("TWNSHPNO") or "").strip()
                   + (p.get("TWNSHPDIR") or "").strip())[:10] or None,
            "rg": ((p.get("RANGENO") or "").strip()
                   + (p.get("RANGEDIR") or "").strip())[:10] or None,
            "pm": (p.get("PRINMER") or "").strip()[:40] or None,
            "lab": (p.get("TWNSHPLAB") or "").strip()[:100] or None,
            "la": round((mnla + mxla) / 2.0, 4),
            "lo": round((mnlo + mxlo) / 2.0, 4),
            "bb": ("POLYGON((%.5f %.5f,%.5f %.5f,%.5f %.5f,%.5f %.5f,"
                   "%.5f %.5f))" % (mnlo, mnla, mxlo, mnla, mxlo, mxla,
                                    mnlo, mxla, mnlo, mnla))[:500],
        })
    print("   usable: %s   skipped (no id or no geometry): %s"
          % (format(len(rows), ","), format(skipped, ",")))

    made, pending = 0, 0
    cxm = eng.begin()
    cx = cxm.__enter__()
    try:
        for r in rows:
            cx.execute(t("""
                INSERT INTO dataview.dv_plss_township
                    (plss_id, province_state_id, township_num, range_num,
                     principal_meridian, township_label, centroid_latitude,
                     centroid_longitude, bbox_wkt, active_ind, source,
                     row_created_by, row_created_date)
                SELECT :id, :st, :tw, :rg, :pm, :lab, :la, :lo, :bb, 'Y',
                       :src, :stamp, SYSUTCDATETIME()
                 WHERE NOT EXISTS (SELECT 1 FROM dataview.dv_plss_township
                                    WHERE plss_id = :id)"""),
                {**r, "src": SOURCE_CODE, "stamp": STAMP})
            made += 1
            pending += 1
            if pending >= 500:            # chunked; see fill_lease_demo_data
                cxm.__exit__(None, None, None)
                pending = 0
                cxm = eng.begin()
                cx = cxm.__enter__()
    finally:
        cxm.__exit__(None, None, None)

    with eng.connect() as cx:
        tot = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_plss_township")).scalar()
        print("\ndv_plss_township: %s row(s)" % format(tot, ","))
        for r in cx.execute(t("""SELECT TOP 5 township_label, township_num,
                                        range_num, principal_meridian,
                                        centroid_latitude, centroid_longitude
                                   FROM dataview.dv_plss_township
                                  ORDER BY township_label""")):
            print("   %-16s %-6s %-6s %-26s %s, %s"
                  % (r[0] or "", r[1] or "", r[2] or "",
                     (r[3] or "")[:26], r[4], r[5]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
