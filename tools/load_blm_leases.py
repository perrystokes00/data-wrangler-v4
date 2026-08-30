r"""Load downloaded BLM lease GeoJSON into dataview.dv_land_tract.

Input is what tools/fetch_blm_leases.py writes. Run add_lease_dates.py first;
this refuses without the temporal columns rather than dropping the dates on
the floor.

AUTHORIZED ONLY, BY DEFAULT, AND THAT IS THE WHOLE DESIGN. Natrona County's
9,785 leases sum to 9.2 MILLION acres in a 3.4-million-acre county: 9,089 of
them are Closed and a century of leases stacks on the same ground. Loading all
of them makes every well intersect a dozen tracts, and the map's well->lease
join takes "first tract wins" -- so a well would report whichever expired 1962
lease happened to sort first. --history loads the closed ones too, on purpose,
for the question they actually answer ("who held this ground in 1985"), and
lease_status is there to keep them out of the default view.

ONE ROW PER LEASE CASE, geometry as delivered. A serial covering six
non-contiguous tracts is ONE legal instrument, and 42% of these arrive as
MultiPolygon for exactly that reason. dv_land_tract.geog has no type
constraint (checked), so the multi-tract leases -- which are the big ones --
go in whole rather than being dropped by a loader that assumed Polygon.

WHAT BLM DOES NOT GIVE US: a lessee. operator_name stays NULL rather than
being filled with the administering office, which would read as ownership and
is not. And QLTY is carried into quality_note, because "MIDPOINT, DOES NOT
MATCH PM ANGLES" is BLM saying the boundary is geocoded from a legal
description and is indicative, not survey-grade.

Insert-only on lease_number, matching promote's first-one-in-wins.

    python tools/load_blm_leases.py --file ..\training\BLM_Leases\blm_leases_WY_teapot.geojson
    python tools/load_blm_leases.py --file <path> --apply
    python tools/load_blm_leases.py --file <path> --history --apply
    python tools/load_blm_leases.py --remove --apply
"""
import argparse
import datetime as _dt
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOURCE = "BLM_MLRS"
REQUIRED = ("effective_date", "expiry_date", "lease_status",
            "producing_ind", "quality_note")

INSERT = """
INSERT INTO dataview.dv_land_tract
    (land_tract_id, tract_name, lease_number, operator_name,
     province_state, country, area_km2, geog, active_ind, source,
     effective_date, expiry_date, lease_status, producing_ind, quality_note,
     row_created_by, row_created_date)
-- REORIENT FIRST, THEN MEASURE. Measuring the raw ring while storing the
-- reoriented one records the size of the COMPLEMENT -- half a billion km2
-- for a tract a few km across. Same guard, and the same ordering, as
-- gen_synthetic_leases and the map's draw-a-boundary writer.
SELECT :id, :nm, :ln, :opr, :st, 'USA',
       g2.STArea()/1000000.0, g2, :act, :src,
       :eff, :exp, :status, :prod, :qlty,
       :src, GETUTCDATE()
  FROM (SELECT CASE WHEN g.STArea()/1000000.0 > 100000
                    THEN g.ReorientObject() ELSE g END AS g2
          FROM (SELECT geography::STGeomFromText(:wkt, 4326).MakeValid() AS g) q1) q
 WHERE NOT EXISTS (SELECT 1 FROM dataview.dv_land_tract t
                    WHERE t.lease_number = :ln AND t.source = :src)
"""


def to_date(v):
    """A date from either shape the file can hold, or None.

    fetch_blm_leases writes epoch milliseconds by default and ISO with
    --iso-dates, so BOTH are legitimate inputs and the loader accepts either
    rather than trusting how the file was made. It also means a file written
    before the pre-1970 conversion bug was fixed -- where old leases kept
    their raw number and new ones did not -- still loads correctly.

    timedelta, not gmtime: half of these leases predate 1970 and gmtime
    raises on a negative timestamp on Windows.
    """
    if v in (None, ""):
        return None
    if isinstance(v, str):
        return v.strip() or None
    try:
        return (_dt.datetime(1970, 1, 1)
                + _dt.timedelta(milliseconds=float(v))).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return None          # never pass a number to a datetime2 column


def _ring_wkt(ring):
    return "(" + ", ".join("%.8f %.8f" % (float(p[0]), float(p[1]))
                           for p in ring) + ")"


def wkt_of(geom):
    """GeoJSON Polygon / MultiPolygon -> WKT. None for anything else."""
    t = (geom or {}).get("type")
    c = (geom or {}).get("coordinates") or []
    if t == "Polygon":
        return "POLYGON (" + ", ".join(_ring_wkt(r) for r in c) + ")"
    if t == "MultiPolygon":
        return ("MULTIPOLYGON ("
                + ", ".join("(" + ", ".join(_ring_wkt(r) for r in poly) + ")"
                            for poly in c) + ")")
    return None


def load_file(path):
    with open(path, encoding="utf-8") as fh:
        return (json.load(fh).get("features") or [])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--file", help="GeoJSON from fetch_blm_leases.py")
    ap.add_argument("--history", action="store_true",
                    help="also load Closed/Pending leases (default: Authorized only)")
    ap.add_argument("--source", default=SOURCE,
                    help="provenance stamp AND half the dedupe key -- the "
                         "other half is lease_number -- so a second source "
                         "cannot collide with BLM's serials. Default %s."
                         % SOURCE)
    ap.add_argument("--remove", action="store_true",
                    help="delete every %s tract" % SOURCE)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    # ONE NAME FOR THE SOURCE, read once. --remove keyed on the CONSTANT
    # while the insert keyed on the flag would delete BLM rows when asked to
    # clear a state load -- the two halves of the dedupe key disagreeing,
    # which is the failure this repo keeps writing down.
    src = a.source

    from dataview.core.dw_utils import make_engine
    from sqlalchemy import text as _t
    eng = make_engine(a.database)

    if a.remove:
        with eng.begin() as cx:
            n = cx.execute(_t("SELECT COUNT(*) FROM dataview.dv_land_tract "
                              "WHERE source=:s"), {"s": src}).scalar()
            if a.apply:
                cx.execute(_t("DELETE FROM dataview.dv_land_tract WHERE source=:s"),
                           {"s": src})
            print("%s %d %s tract(s). SYNTH_LEASE is untouched."
                  % ("removed" if a.apply else "would remove", n, src))
        return 0

    if not a.file:
        print("--file is required (or --remove).")
        return 2

    with eng.connect() as cx:
        have = {r[0].lower() for r in cx.execute(_t(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE "
            "TABLE_SCHEMA='dataview' AND TABLE_NAME='dv_land_tract'"))}
    missing = [c for c in REQUIRED if c not in have]
    if missing:
        # REFUSE, do not silently drop. A lease loaded without its dates is a
        # lease that cannot be filtered to the present, which is the one thing
        # this whole exercise is for.
        print("dv_land_tract is missing %s.\n"
              "Run:  python tools/add_lease_dates.py --apply" % ", ".join(missing))
        return 1

    feats = load_file(a.file)
    keep, skipped_status, no_geom, no_serial = [], 0, 0, 0
    for f in feats:
        p = f.get("properties") or {}
        status = str(p.get("CSE_DISP") or "").strip()
        # CURRENT, NOT "AUTHORIZED". The point of this filter is to keep a
        # century of dead leases out of the default view -- 9,089 of
        # Natrona's 9,785 BLM leases are Closed, and loading them makes
        # every well intersect a dozen tracts. "Authorized" is how BLM
        # spells current; it is not how anyone else does. Wyoming's LARCS
        # says Prospecting, Producing, Suspended -- and its ACTIVE layer
        # says nothing at all for 85% of leases, because the attribute
        # table does not reach them.
        #
        # So a file that CARRIES an explicit ACTIVE flag is believed, and
        # only a file without one falls back to BLM's word. Without this,
        # a state load reported "skipped, not Authorized: 16,356" and
        # loaded nothing -- correct by the letter, useless.
        _explicit = (p.get("ACTIVE") or "").strip().upper()[:1]
        _is_current = (_explicit == "Y" if _explicit
                       else status.lower() == "authorized")
        if not a.history and not _is_current:
            skipped_status += 1
            continue
        w = wkt_of(f.get("geometry"))
        if not w:
            no_geom += 1
            continue
        if not str(p.get("CSE_NR") or "").strip():
            no_serial += 1
            continue
        keep.append((p, w))

    print("\n%s" % os.path.abspath(a.file))
    print("   features in file        : %s" % format(len(feats), ","))
    print("   skipped, not current    : %s%s"
          % (format(skipped_status, ","), "" if not a.history else "  (--history: none)"))
    print("   skipped, no geometry    : %s" % format(no_geom, ","))
    print("   skipped, no CSE_NR      : %s" % format(no_serial, ","))
    print("   TO LOAD                 : %s" % format(len(keep), ","))
    if not keep:
        print("\nNothing to load.")
        return 0
    if not a.apply:
        print("\nDRY RUN -- re-run with --apply to write.")
        return 0

    n = 0
    with eng.begin() as cx:
        for p, w in keep:
            r = cx.execute(_t(INSERT), {
                "id": uuid.uuid4().hex[:40].upper(),
                "nm": (p.get("CSE_NAME") or "").strip() or None,
                "ln": str(p.get("CSE_NR")).strip(),
                "st": (p.get("GEO_STATE") or p.get("ADMIN_STATE") or "").strip() or None,
                # AN EXPLICIT FLAG BEATS AN INFERRED ONE. BLM files carry
                # no ACTIVE property, so they keep inferring from CSE_DISP.
                # A source that KNOWS -- because it fetched from an
                # "active leases" endpoint -- says so, and is believed.
                "act": ((p.get("ACTIVE") or "").strip().upper()[:1]
                        or ("Y" if str(p.get("CSE_DISP") or "").lower() in
                            ("authorized", "producing", "prospecting")
                            else "N")),
                "src": src,
                # THE LESSEE, WHEN THE SOURCE HAS ONE. BLM publishes none, so
                # this stays NULL for MLRS -- which is the truth, and is why
                # the synthetic-owner tool exists at all. Wyoming's LARCS
                # names the company, so a state load fills it for real.
                "opr": (p.get("LESSEE") or "").strip()[:120] or None,
                "eff": to_date(p.get("EFF_DT")),
                "exp": to_date(p.get("EXP_DT")),
                "status": (p.get("CSE_DISP") or "").strip() or None,
                "prod": (p.get("PRDCNG") or "").strip() or None,
                "qlty": (p.get("QLTY") or "").strip()[:400] or None,
                "wkt": w})
            n += r.rowcount or 0
    print("\ninserted %s (the rest were already here -- insert-only on "
          "lease_number)" % format(n, ","))
    with eng.connect() as cx:
        for r in cx.execute(_t("""
            SELECT source, lease_status, COUNT(*), ROUND(SUM(area_km2), 1)
              FROM dataview.dv_land_tract GROUP BY source, lease_status
             ORDER BY 1, 3 DESC""")):
            print("   %-12s %-12s %6s tract(s)  %s km2"
                  % (r[0], r[1] or "(none)", format(r[2], ","), format(r[3] or 0, ",")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
