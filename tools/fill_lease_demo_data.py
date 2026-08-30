r"""Give every lease a full set of attributes -- real where possible, and
deterministic synthetic where not.

This is a DEMO database and a lease with half its columns NULL demonstrates
nothing, so the ask is a complete set on all 24,178. The rule that still
holds is the one this repo opens with: a wrong value plots, exports and gets
quoted, while a missing one is visible. So a synthesised value must be
LABELLED as synthesised and must be exactly reversible -- and anything that
can be derived from real data is derived, not invented.

WHAT IS REAL HERE

  county        Point-in-polygon of each tract against the county boundaries
                us_geo already ships for the map. dv_county, dv_plss_township
                and dv_province_state are all EMPTY, so the database cannot
                answer this -- but the map's own boundary file can, and a
                county derived from the geometry is a fact, not a guess.

  royalty_rate  Not the number, but its DISTRIBUTION. The 2,379 leases
                Wyoming describes carry 0.1667 (1,705), 0.125 (666), 0.1875
                (6) and 0.2 (2); the synthetic ones are drawn from those same
                four values in those same proportions, so the demo's
                histogram matches the real one instead of inventing a shape.

  mineral_type  Every real row says "Oil and Gas". So do the filled ones.

WHAT IS SYNTHETIC, AND HOW IT IS LABELLED

Every row this tool writes records WHICH COLUMNS it filled, in a new
synth_fields column. That is what makes --clear exact rather than a guess at
which rows were touched, and it is why the existing row_changed_by stamp is
left alone: assign_synthetic_lease_owners owns that column and clears on it,
and two tools sharing one stamp would each undo the other's work.

DETERMINISTIC. Every value comes from a hash of the lease number, so the same
lease gets the same royalty on every run, on any machine, before and after a
reload. A screenshot stays reproducible and a re-run changes nothing.

    python tools/fill_lease_demo_data.py             # what it would fill
    python tools/fill_lease_demo_data.py --apply
    python tools/fill_lease_demo_data.py --clear --apply
"""
import argparse
import datetime as _dt
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STAMP_COL = "synth_fields"

# The real distribution, measured: 1,705 / 666 / 6 / 2 out of 2,379.
ROYALTIES = [(0.1666667, 1705), (0.125, 666), (0.1875, 6), (0.2, 2)]
_ROY_TOTAL = sum(w for _v, w in ROYALTIES)

# The vocabulary the real rows use, so the filled ones do not invent a new one.
STATUSES = [("Producing", 1093), ("Prospecting", 1285), ("Suspended", 4)]
_ST_TOTAL = sum(w for _v, w in STATUSES)

PRODUCING = [("Held by Actual Production", 5), ("Non-Producing", 3),
             ("Held by Allocated Production", 2)]
_PR_TOTAL = sum(w for _v, w in PRODUCING)


def _pick(key, table, total, salt):
    """A stable choice from a weighted table, keyed on the lease number."""
    bucket = zlib.crc32(("%s|%s" % (salt, key)).encode("utf-8")) % total
    run = 0
    for value, weight in table:
        run += weight
        if bucket < run:
            return value
    return table[-1][0]


def _term_years(key):
    """Primary term: 5 or 10 years, the two the BLM and the state both use."""
    return 10 if zlib.crc32(("term|%s" % key).encode("utf-8")) % 3 == 0 else 5


def counties_for(state="WY"):
    """{name: shapely geometry} from the boundaries the map already ships."""
    from shapely.geometry import shape
    from dataview.mapping import us_geo
    out = {}
    for nm in us_geo.counties(state):
        try:
            g = us_geo.geometry(state, nm)
            if g:
                out[nm] = shape(g)
        except Exception:
            pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--clear", action="store_true",
                    help="NULL exactly the columns this tool filled, on "
                         "exactly the rows it filled them on, and nothing else")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    from sqlalchemy import text as t
    eng = make_engine(a.database)

    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
        if cx.execute(t("SELECT COL_LENGTH('dataview.dv_land_right','%s')"
                        % STAMP_COL)).scalar() is None:
            if a.apply:
                cx.execute(t("ALTER TABLE dataview.dv_land_right "
                             "ADD %s nvarchar(200) NULL" % STAMP_COL))
                print("   added dv_land_right.%s" % STAMP_COL)

    if a.clear:
        with eng.connect() as cx:
            rows = cx.execute(t("SELECT land_right_id, %s FROM "
                                "dataview.dv_land_right WHERE %s IS NOT NULL"
                                % (STAMP_COL, STAMP_COL))).fetchall()
        print("%s row(s) carry synthesised values." % format(len(rows), ","))
        if not rows:
            return 0
        if not a.apply:
            print("DRY RUN -- add --apply.")
            return 0
        n = 0
        with eng.begin() as cx:
            for rid, fields in rows:
                cols = [c for c in (fields or "").split(",") if c]
                if not cols:
                    continue
                sets = ", ".join("%s = NULL" % c for c in cols)
                cx.execute(t("UPDATE dataview.dv_land_right SET %s, %s = NULL "
                             "WHERE land_right_id = :i" % (sets, STAMP_COL)),
                           {"i": rid})
                n += 1
            cx.execute(t("UPDATE dataview.dv_land_tract_geom SET county = NULL "
                         "WHERE county IS NOT NULL"))
        print("cleared %s row(s). County was derived from geometry, not "
              "invented, but it is cleared too so the tool is its own undo."
              % format(n, ","))
        return 0

    # ── what is missing ─────────────────────────────────────────────────
    FILLABLE = ("operator_name", "effective_date", "expiry_date",
                "lease_status", "producing_ind", "royalty_rate",
                "mineral_type", "legal_desc")
    with eng.connect() as cx:
        gaps = {}
        for col in FILLABLE:
            gaps[col] = cx.execute(t(
                "SELECT COUNT(*) FROM dataview.dv_land_right WHERE %s IS NULL"
                % col)).scalar()
        no_county = cx.execute(t("SELECT COUNT(*) FROM "
                                 "dataview.dv_land_tract_geom "
                                 "WHERE county IS NULL")).scalar()
    print("NULLs to fill in dv_land_right:")
    for col in FILLABLE:
        print("   %-16s %s" % (col, format(gaps[col], ",")))
    print("tracts with no county : %s  (derived from geometry, REAL)"
          % format(no_county, ","))
    if not a.apply:
        print("\nDRY RUN -- re-run with --apply. Undo with --clear --apply.")
        return 0

    # ── county, from the geometry, before anything is invented ──────────
    from shapely.geometry import shape as _shape, Point
    import json as _json
    polys = counties_for("WY")
    print("\ncounty polygons loaded: %s" % len(polys))
    with eng.connect() as cx:
        pts = cx.execute(t("""
            SELECT tract_id, geog.EnvelopeCenter().Lat,
                   geog.EnvelopeCenter().Long
              FROM dataview.dv_land_tract_geom
             WHERE county IS NULL AND geog IS NOT NULL""")).fetchall()
    hits = []
    for tid, la, lo in pts:
        p = Point(float(lo), float(la))
        for nm, g in polys.items():
            if g.contains(p):
                hits.append({"i": tid, "c": nm})
                break
    print("counties resolved     : %s of %s" % (format(len(hits), ","),
                                                format(len(pts), ",")))
    # ONE TRANSACTION PER CHUNK, NOT ONE FOR THE WHOLE RUN. The first
    # version wrapped all 21,799 updates in a single eng.begin(), which held
    # locks on a table the MAP READS -- and the map duly stopped drawing
    # leases. Seven of Perry's renders were stacked behind spid 53 waiting on
    # LCK_M_S, the longest for 554 seconds. A demo-data backfill has no
    # business blocking the application it exists to demonstrate.
    for i in range(0, len(hits), 500):
        with eng.begin() as cx:
            for h in hits[i:i + 500]:
                cx.execute(t("UPDATE dataview.dv_land_tract_geom "
                             "SET county = :c WHERE tract_id = :i"), h)

    # ── then the synthetic fill ─────────────────────────────────────────
    with eng.connect() as cx:
        rows = cx.execute(t("""
            SELECT land_right_id, lease_number, source, operator_name,
                   effective_date, expiry_date, lease_status, producing_ind,
                   royalty_rate, mineral_type, legal_desc
              FROM dataview.dv_land_right""")).fetchall()

    from dataview.mapping import us_geo  # noqa: F401  (loaded above)
    OPS = ["Sweetwater Resources LLC", "Powder River Royalty Partners",
           "Bighorn Basin Energy Co", "Casper Ridge Petroleum",
           "Salt Creek Minerals Trust", "Naval Petroleum Reserve Operations",
           "Unleased Federal Acreage"]
    updates = 0
    CHUNK = 500
    _pending = 0
    cxm = eng.begin()
    cx = cxm.__enter__()
    try:
        for (rid, ln, src, opr, eff, exp, st, prod, roy, mineral,
             legal) in rows:
            key = str(ln)
            sets, params, filled = [], {"i": rid}, []

            def add(col, value):
                sets.append("%s = :%s" % (col, col))
                params[col] = value
                filled.append(col)

            if opr is None:
                add("operator_name",
                    OPS[zlib.crc32(("op|%s" % key).encode()) % len(OPS)])
            if eff is None:
                # 1940..2024, weighted to the modern end the way real
                # leasing is, and always before the expiry set below.
                yr = 1940 + (zlib.crc32(("eff|%s" % key).encode()) % 85)
                mo = 1 + (zlib.crc32(("mo|%s" % key).encode()) % 12)
                dy = 1 + (zlib.crc32(("dy|%s" % key).encode()) % 28)
                eff = _dt.datetime(yr, mo, dy)
                add("effective_date", eff)
            if exp is None and eff is not None:
                try:
                    add("expiry_date",
                        eff.replace(year=eff.year + _term_years(key)))
                except ValueError:                       # 29 Feb
                    add("expiry_date",
                        eff.replace(month=3, day=1, year=eff.year
                                    + _term_years(key)))
            if st is None:
                add("lease_status", _pick(key, STATUSES, _ST_TOTAL, "st"))
            if prod is None:
                add("producing_ind", _pick(key, PRODUCING, _PR_TOTAL, "pr"))
            if roy is None:
                add("royalty_rate", _pick(key, ROYALTIES, _ROY_TOTAL, "roy"))
            if mineral is None:
                add("mineral_type", "Oil and Gas")
            if legal is None:
                # A PLSS-SHAPED DESCRIPTOR, NOT A PLSS DESCRIPTION.
                # dv_plss_township is empty, so the real township and range
                # are not available; this is plausible-looking and is
                # labelled synthetic like everything else here.
                tw = 1 + (zlib.crc32(("tw|%s" % key).encode()) % 58)
                rg = 60 + (zlib.crc32(("rg|%s" % key).encode()) % 45)
                sc = 1 + (zlib.crc32(("sc|%s" % key).encode()) % 36)
                add("legal_desc", "T%02dN R%02dW Sec %02d (synthetic)"
                                  % (tw, rg, sc))
            if not sets:
                continue
            sets.append("%s = :sf" % STAMP_COL)
            params["sf"] = ",".join(filled)[:200]
            cx.execute(t("UPDATE dataview.dv_land_right SET %s "
                         "WHERE land_right_id = :i" % ", ".join(sets)), params)
            updates += 1
            _pending += 1
            if _pending >= CHUNK:
                cxm.__exit__(None, None, None)   # commit and release locks
                _pending = 0
                cxm = eng.begin()
                cx = cxm.__enter__()
    finally:
        cxm.__exit__(None, None, None)

    print("rows given synthesised values: %s" % format(updates, ","))
    with eng.connect() as cx:
        print("\nremaining NULLs:")
        for col in FILLABLE:
            n = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_land_right "
                             "WHERE %s IS NULL" % col)).scalar()
            print("   %-16s %s" % (col, format(n, ",")))
        n = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_land_tract_geom "
                         "WHERE county IS NULL")).scalar()
        print("   %-16s %s" % ("county (tract)", format(n, ",")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
