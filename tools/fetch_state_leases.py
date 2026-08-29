r"""Download Wyoming STATE oil & gas lease polygons as GeoJSON.

    https://gis2.statelands.wyo.gov/arcgis/rest/services/LARCS/
        ActiveMineralLeaseLARCS/MapServer

The other half of the lease picture. dv_land_tract holds 10,924 FEDERAL
leases from BLM MLRS; the Office of State Lands and Investments administers
16,356 more on state trust land, and MLRS knows nothing about them. A map
showing only one source reads as "the leases in Wyoming" and is wrong by more
than half.

WHY THIS IS WORTH THE TROUBLE: IT NAMES THE LESSEE. BLM publishes none --
operator_name is NULL on every federal row, which is why
assign_synthetic_lease_owners.py exists and why the owner colouring is
currently fiction. LARCS carries CompanyName, and it is real: Devon Energy
Production Company L.P., Denbury Onshore LLC. It also carries the royalty
rate, the county, the legal description, the acres as filed, and the auction
the lease came from.

SIX THINGS MEASURED HERE BEFORE ANY OF IT WAS WRITTEN, each of which would
have gone in silently:

 1 THE GEOMETRY AND THE ATTRIBUTES ARE TWO DIFFERENT ENDPOINTS. Layer 0
   (FC_OilAndGasLease) is polygons carrying a LeaseNumber and nothing else --
   no lessee, no dates. Table 1 (T_ActiveMineralLeaseLARCS) holds the ~80
   attribute fields. They join on LeaseNumber, and a fetch of layer 0 alone
   produces shapes nobody can answer a question about.

 2 THE TABLE DOES NOT COVER EVERY LEASE. 16,356 polygons, 3,615 attribute
   rows. So most leases arrive with geometry and a number and no lessee. That
   is the truth of the source, not a bug in the join -- but a loader that
   inner-joined would silently drop three quarters of the state's leases, and
   a summary that did not COUNT the misses would never show it. Reported
   every run, and the unmatched leases are kept.

 3 LeaseNumber IS EMPTY STRING, NOT NULL, on 3,086 polygons. `IS NOT NULL`
   returns 13,308 and ten of the first ten come back blank; `<> ''` returns
   13,270. A filter keyed on NULL therefore passes rows it meant to exclude
   -- the same shape as the placeholder trap in CLAUDE.md, where a non-null
   wrong value defeats every repair keyed on "missing".

 4 IT IS NOT IN WGS84. The service is WyLam -- NAD83 Lambert Conformal Conic
   in metres, central meridian -107.5. Asking for outSR=4326 makes the server
   reproject, which is free here and a reprojection library we do not need.
   Without it the coordinates are metres in the hundred-thousands and land
   the leases off the planet.

 5 maxRecordCount IS 2,000 and the service answers a larger ask with
   exceededTransferLimit and a short file -- a truncated download that looks
   complete. Paged, and it refuses to write a file it believes is short.

 6 DATES ARE EPOCH MILLISECONDS AND HALF ARE NEGATIVE. These leases start in
   the 1940s. time.gmtime() raises on a negative timestamp on Windows; the
   datetime + timedelta form does not, which is the lesson fetch_blm_leases
   already paid for.

    python tools/fetch_state_leases.py                    # counts only
    python tools/fetch_state_leases.py --apply
    python tools/fetch_state_leases.py --iso-dates --apply
    python tools/fetch_state_leases.py --numbered-only --apply
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

BASE = ("https://gis2.statelands.wyo.gov/arcgis/rest/services/LARCS/"
        "ActiveMineralLeaseLARCS/MapServer")
GEOM = BASE + "/0/query"          # FC_OilAndGasLease  -- polygons
ATTR = BASE + "/1/query"          # T_ActiveMineralLeaseLARCS -- the detail
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PAGE = 1000                       # under the service's 2,000 cap
SOURCE = "WY_OSLI"

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "..", "training", "State_Leases")

# What the popup and the loader need. NOT "*": the table has 80 columns
# including every auction fee and bidder id, and carrying those into a
# GeoJSON that the browser downloads is payload nobody reads.
ATTR_FIELDS = (
    "LeaseNumber,CompanyName,LeaseCounty,LeaseAcres,LeaseIssueDate,"
    "LeaseExpirationDate,LeasePrimaryTermExpirationDate,LeaseStatusLabel,"
    "LeaseStatusIsProducing,LeaseStatusIsActive,LeaseRoyaltyRate,"
    "MineralTypeLabel,LeaseLegalDescription,LeaseStateOwnershipPercentage"
)


def _get(url, params, timeout=300, post=False):
    """One request. POST when the parameters are long.

    objectIds is a comma-separated list, and 500 ids is ~4 KB of query
    string -- past what several proxies and some ArcGIS front ends accept on
    a GET, and the failure is a 414 or a silent truncation rather than an
    error worth reading. Esri's own guidance is to POST a large parameter
    list, so the id pages do.
    """
    data = urllib.parse.urlencode(params).encode("utf-8")
    if post:
        req = urllib.request.Request(
            url, data=data,
            headers={"User-Agent": UA,
                     "Content-Type": "application/x-www-form-urlencoded"})
    else:
        req = urllib.request.Request(url + "?" + data.decode("utf-8"),
                                     headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def count(url, where):
    return int(_get(url, {"where": where, "returnCountOnly": "true",
                          "f": "json"}).get("count", 0))


def all_ids(url, where):
    """Every matching OBJECTID, in ONE request.

    returnIdsOnly is exempt from maxRecordCount -- it hands back all 16,356
    ids at once -- and it is what makes id paging possible at all.
    """
    d = _get(url, {"where": where, "returnIdsOnly": "true", "f": "json"})
    return list(d.get("objectIds") or []), d.get("objectIdFieldName", "OBJECTID")


def fetch_by_ids(url, ids, fields, geometry, log=print, simplify=False):
    """Every record, fetched a page of OBJECTIDs at a time.

    NOT resultOffset. Offset paging asks the server to produce and discard
    everything before the offset on every request, so each page costs more
    than the last -- measured here on the real service: page 1 of the lease
    polygons arrived in ~40s and page 2 had not landed two and a half minutes
    later. Seventeen pages of that is most of an hour, and the cost is pure
    re-scanning.

    An explicit id list has no offset to re-scan, so every page costs the
    same. It also makes the short-download guard EXACT rather than a guess:
    we know how many ids we asked for, so a page that returns fewer is a
    fact, not an inference from exceededTransferLimit.

    Geometry pages come back as GeoJSON; the table as ArcGIS json rows --
    f=geojson on a TABLE returns nothing useful, which is why the two are
    asked for differently.
    """
    out = []
    for i in range(0, len(ids), PAGE):
        chunk = ids[i:i + PAGE]
        p = {"objectIds": ",".join(str(x) for x in chunk),
             "outFields": fields,
             "returnGeometry": "true" if geometry else "false"}
        if geometry:
            # FIVE DECIMALS, ~1 m, AND IT IS NOT A COMPROMISE. Measured on
            # 400 real polygons: 0.28 MB as delivered, 0.17 MB at precision
            # 5 -- 39% of the payload was digits past the metre. These
            # boundaries are geocoded from legal descriptions (BLM says so
            # in its own QLTY field: "MIDPOINT, DOES NOT MATCH PM ANGLES"),
            # so sub-metre digits are false precision. points_layer already
            # rounds to 5 for the same reason.
            #
            # maxAllowableOffset is NOT on by default. It halves the payload
            # again (0.08 MB) but GENERALISES the shape, and the loader
            # computes acreage from the geometry it is given -- so a
            # simplified fetch would write a slightly wrong area for every
            # lease, silently. Fine for a picture, wrong for a record.
            p.update(f="geojson", outSR=4326, geometryPrecision=5)
            if simplify:
                p.update(maxAllowableOffset=0.0001)
        else:
            p.update(f="json")
        d = _get(url, p, post=True)
        got = d.get("features") or []
        out.extend(got)
        log("   +%-5d  total %s of %s" % (len(got), format(len(out), ","),
                                          format(len(ids), ",")))
        if len(got) < len(chunk):
            log("      (asked %d, got %d)" % (len(chunk), len(got)))
        time.sleep(0.2)           # a public service; do not hammer it
    return out


def _iso(ms):
    """Epoch milliseconds -> 'yyyy-mm-dd'. Correct BEFORE 1970 too.

    Half of these leases predate the epoch -- the oldest sampled issue dates
    are 1943 -- and time.gmtime() raises OSError on a negative timestamp on
    Windows. datetime + timedelta has no such limit.
    """
    if ms in (None, ""):
        return None
    try:
        return (_dt.datetime(1970, 1, 1)
                + _dt.timedelta(milliseconds=float(ms))).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return ms


def _t(v):
    return None if v is None else (str(v).strip() or None)


def build(geom_feats, attr_rows, iso):
    """Join the polygons to their attributes and report what did NOT match.

    KEEPS THE UNMATCHED. Three quarters of the polygons have no attribute row
    (see note 2), and they are still real leases on real ground with a real
    lease number. Dropping them would lose most of the state's acreage to
    make the join look tidy; held with null attributes, they are visible and
    countable, which is the same call promote makes with a held row.
    """
    by_num = {}
    dupes = 0
    for r in attr_rows:
        a = r.get("attributes") or {}
        k = _t(a.get("LeaseNumber"))
        if not k:
            continue
        if k in by_num:
            dupes += 1
            continue          # first one wins, and we say how many there were
        by_num[k] = a

    feats, matched = [], 0
    for f in geom_feats:
        p = f.get("properties") or {}
        num = _t(p.get("LeaseNumber"))
        a = by_num.get(num) if num else None
        if a:
            matched += 1
        eff = a.get("LeaseIssueDate") if a else None
        exp = a.get("LeaseExpirationDate") if a else None
        prod = a.get("LeaseStatusIsProducing") if a else None
        props = {
            # BLM-COMPATIBLE NAMES where the meaning is the same, so
            # load_blm_leases.py can take this file with a --source flag
            # rather than growing a second loader beside it.
            "CSE_NR": num,
            "CSE_NAME": None,
            "GEO_STATE": "WY",
            "CSE_DISP": _t(a.get("LeaseStatusLabel")) if a else None,
            "EFF_DT": _iso(eff) if iso else eff,
            "EXP_DT": _iso(exp) if iso else exp,
            "PRDCNG": (None if prod is None else ("Y" if prod else "N")),
            "QLTY": None,
            # ...and the ones BLM cannot give us at all.
            "LESSEE": _t(a.get("CompanyName")) if a else None,
            "COUNTY": _t(a.get("LeaseCounty")) if a else None,
            "ACRES": (a.get("LeaseAcres") if a else None),
            "ROYALTY": (a.get("LeaseRoyaltyRate") if a else None),
            "MINERAL": _t(a.get("MineralTypeLabel")) if a else None,
            # `if a else None` LIKE EVERY LINE AROUND IT. This one was written
            # without it and crashed the whole run after a fifteen-minute
            # download -- on the 78% of polygons that HAVE no attribute row,
            # which is the case note 2 exists to describe. The guard belongs
            # on every field precisely because the miss is the common case.
            "LEGAL": ((_t(a.get("LeaseLegalDescription")) or "")[:400] or None)
                     if a else None,
            "STATE_PCT": (a.get("LeaseStateOwnershipPercentage") if a else None),
            "SOURCE": SOURCE,
        }
        feats.append({"type": "Feature", "geometry": f.get("geometry"),
                      "properties": props})
    return feats, matched, dupes


def summarise(feats, matched, dupes, iso):
    kinds, status, lessees = {}, {}, set()
    acres = 0.0
    no_number = 0
    for f in feats:
        g = (f.get("geometry") or {}).get("type", "None")
        kinds[g] = kinds.get(g, 0) + 1
        p = f["properties"]
        if not p.get("CSE_NR"):
            no_number += 1
        s = p.get("CSE_DISP") or "(no attribute row)"
        status[s] = status.get(s, 0) + 1
        if p.get("LESSEE"):
            lessees.add(p["LESSEE"])
        try:
            acres += float(p.get("ACRES") or 0)
        except (TypeError, ValueError):
            pass
    n = len(feats)
    print("\n   polygons          : %s" % format(n, ","))
    print("   with attributes   : %s  (%.0f%%)"
          % (format(matched, ","), 100.0 * matched / max(n, 1)))
    print("   WITHOUT           : %s  -- geometry and lease number only"
          % format(n - matched, ","))
    print("   no lease number   : %s" % format(no_number, ","))
    if dupes:
        print("   duplicate attr rows: %s (first kept)" % format(dupes, ","))
    print("   geometry types    : %s" % kinds)
    print("   distinct lessees  : %s" % format(len(lessees), ","))
    print("   acres (as filed)  : %s" % format(int(acres), ","))
    print("   status            : %s"
          % dict(sorted(status.items(), key=lambda x: -x[1])[:8]))
    print("   dates             : %s" % ("ISO yyyy-mm-dd (converted)" if iso
                                         else "EPOCH MILLISECONDS, as delivered"))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--numbered-only", action="store_true",
                    help="skip polygons whose LeaseNumber is blank (3,086 of "
                         "them; note 3 above -- they are '' and not NULL)")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore the cached raw download and fetch again")
    ap.add_argument("--simplify", action="store_true",
                    help="generalise geometry (~11 m) -- halves the download "
                         "again, but the acreage the loader computes from it "
                         "is then approximate. Map-only.")
    ap.add_argument("--out", default=None, help="output .geojson path")
    ap.add_argument("--iso-dates", action="store_true",
                    help="convert epoch-ms dates to yyyy-mm-dd")
    ap.add_argument("--apply", action="store_true",
                    help="write the file. Without it, only the counts are "
                         "fetched -- two requests, no download.")
    a = ap.parse_args(argv)

    where = "LeaseNumber <> ''" if a.numbered_only else "1=1"
    print("service : %s" % BASE)
    print("where   : %s" % where)
    n_geom = count(GEOM, where)
    n_attr = count(ATTR, "1=1")
    print("polygons: %s" % format(n_geom, ","))
    print("attr rows: %s" % format(n_attr, ","))
    if n_attr < n_geom:
        print("   NOTE: fewer attribute rows than polygons -- most leases "
              "will arrive with geometry and a number only. See note 2.")
    if not a.apply:
        print("\nDRY RUN -- re-run with --apply to download.")
        return 0

    # THE DOWNLOAD IS EXPENSIVE AND THE JOIN IS THE FRAGILE PART. One field
    # written without its `if a else None` crashed a COMPLETED fifteen-minute
    # fetch and threw all of it away. The raw pages are cached beside the
    # output, so fixing build() costs seconds instead of another download.
    out_path = a.out or os.path.join(OUT_DIR, "state_leases_WY.geojson")
    raw_path = out_path + ".raw.json"
    if os.path.exists(raw_path) and not a.refresh:
        print("\nreusing the cached download: %s  (--refresh to fetch again)"
              % raw_path)
        with open(raw_path, "r", encoding="utf-8") as fh:
            _raw = json.load(fh)
        geom, attr, gids = _raw["geom"], _raw["attr"], _raw["gids"]
        print("   %s polygons, %s attribute rows"
              % (format(len(geom), ","), format(len(attr), ",")))
    else:
        print("\ngeometry (by OBJECTID, reprojected to 4326 by the server):")
        gids, _f = all_ids(GEOM, where)
        print("   %s ids" % format(len(gids), ","))
        geom = fetch_by_ids(GEOM, gids, "LeaseNumber", True,
                            simplify=a.simplify)
        print("attributes (by OBJECTID):")
        aids, _f2 = all_ids(ATTR, "1=1")
        print("   %s ids" % format(len(aids), ","))
        attr = fetch_by_ids(ATTR, aids, ATTR_FIELDS, False)
        os.makedirs(os.path.dirname(os.path.abspath(raw_path)), exist_ok=True)
        with open(raw_path, "w", encoding="utf-8") as fh:
            json.dump({"geom": geom, "attr": attr, "gids": gids}, fh)
        print("   cached: %s (%.1f MB)"
              % (raw_path, os.path.getsize(raw_path) / 1048576.0))

    if len(geom) < len(gids):
        print("\nSHORT DOWNLOAD: %s of %s polygons. Refusing to write a file "
              "that looks complete and is not." % (format(len(geom), ","),
                                                   format(len(gids), ",")))
        return 1

    feats, matched, dupes = build(geom, attr, a.iso_dates)
    summarise(feats, matched, dupes, a.iso_dates)

    out = out_path
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh)
    os.replace(tmp, out)
    print("\nwrote %s  (%s features, %.1f MB)"
          % (out, format(len(feats), ","),
             os.path.getsize(out) / 1048576.0))
    print("Load with:  python tools/load_blm_leases.py --file \"%s\" "
          "--source %s --apply     (needs the --source flag; see the note in "
          "this file's docstring)" % (out, SOURCE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
