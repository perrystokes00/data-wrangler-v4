r"""Fill the columns the split created and the load left empty.

dv_land_right has royalty_rate, mineral_type and legal_desc; every one of
them is NULL, because load_blm_leases maps BLM's fields and BLM has none of
these. The state file already on disk carries all three -- they were fetched,
written, and then not read, because the loader speaks BLM.

REAL DATA BEFORE SYNTHETIC DATA. There is an obvious temptation to invent a
royalty rate for a demo; Wyoming publishes the actual one (0.125 on the rows
sampled), and a real 12.5% is worth more than a plausible one AND cannot be
wrong. Only the leases the source describes are touched -- the 85% with no
attribute row stay NULL, which is the honest answer for them.

COUNTY GOES ON THE GROUND, NOT THE INSTRUMENT. A county is where a tract IS;
it does not change when the lease over it expires, and putting it on the
right would duplicate it for every future lease on the same ground. That is
the whole point of the split, so it would be a poor first use of it.

    python tools/backfill_state_lease_attrs.py            # what it would set
    python tools/backfill_state_lease_attrs.py --apply
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "training", "State_Leases", "state_leases_WY.geojson")
SOURCE = "WY_OSLI"
STAMP = "STATE_ATTRS"


def _t(v):
    return None if v is None else (str(v).strip() or None)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    from sqlalchemy import text as t
    eng = make_engine(a.database)

    with open(a.file, "r", encoding="utf-8") as fh:
        feats = (json.load(fh).get("features") or [])

    rows = []
    for f in feats:
        p = f.get("properties") or {}
        ln = _t(p.get("CSE_NR"))
        if not ln:
            continue
        roy, mineral = p.get("ROYALTY"), _t(p.get("MINERAL"))
        legal, county = _t(p.get("LEGAL")), _t(p.get("COUNTY"))
        if roy is None and not mineral and not legal and not county:
            continue                      # nothing this row can contribute
        rows.append({"ln": ln, "roy": roy, "min": mineral,
                     "leg": (legal or "")[:400] or None, "cty": county})

    print("%s\n   features            : %s" % (os.path.abspath(a.file),
                                               format(len(feats), ",")))
    print("   with something to set: %s" % format(len(rows), ","))
    have = sum(1 for r in rows if r["roy"] is not None)
    print("      royalty rate      : %s" % format(have, ","))
    print("      mineral type      : %s"
          % format(sum(1 for r in rows if r["min"]), ","))
    print("      legal description : %s"
          % format(sum(1 for r in rows if r["leg"]), ","))
    print("      county            : %s"
          % format(sum(1 for r in rows if r["cty"]), ","))
    if not a.apply:
        print("\nDRY RUN -- re-run with --apply.")
        return 0

    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
        # county belongs to the GROUND, and the column does not exist yet
        exists = cx.execute(t(
            "SELECT COL_LENGTH('dataview.dv_land_tract_geom','county')")).scalar()
        if exists is None:
            cx.execute(t("ALTER TABLE dataview.dv_land_tract_geom "
                         "ADD county nvarchar(64) NULL"))
            print("   added dv_land_tract_geom.county")

    n_r = n_t = 0
    with eng.begin() as cx:
        for r in rows:
            res = cx.execute(t("""
                UPDATE dataview.dv_land_right
                   SET royalty_rate = COALESCE(:roy, royalty_rate),
                       mineral_type = COALESCE(:min, mineral_type),
                       legal_desc   = COALESCE(:leg, legal_desc),
                       row_changed_by = :stamp,
                       row_changed_date = SYSUTCDATETIME()
                 WHERE source = :src AND lease_number = :ln"""),
                {**r, "src": SOURCE, "stamp": STAMP})
            n_r += res.rowcount or 0
            if r["cty"]:
                res2 = cx.execute(t("""
                    UPDATE g SET g.county = :cty
                      FROM dataview.dv_land_tract_geom g
                      JOIN dataview.dv_land_right_tract x
                        ON x.tract_id = g.tract_id
                      JOIN dataview.dv_land_right rr
                        ON rr.land_right_id = x.land_right_id
                     WHERE rr.source = :src AND rr.lease_number = :ln"""),
                    {"cty": r["cty"], "src": SOURCE, "ln": r["ln"]})
                n_t += res2.rowcount or 0

    print("\nrights updated : %s" % format(n_r, ","))
    print("tracts given a county: %s" % format(n_t, ","))
    with eng.connect() as cx:
        for lbl, sql in (
            ("royalty_rate", "royalty_rate IS NOT NULL"),
            ("mineral_type", "mineral_type IS NOT NULL"),
            ("legal_desc", "legal_desc IS NOT NULL"),
        ):
            n = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_land_right "
                             "WHERE %s" % sql)).scalar()
            print("   %-14s now set on %s row(s)" % (lbl, format(n, ",")))
        for r in cx.execute(t("""
            SELECT TOP 5 mineral_type, royalty_rate, COUNT(*) n
              FROM dataview.dv_land_right
             WHERE mineral_type IS NOT NULL
             GROUP BY mineral_type, royalty_rate ORDER BY COUNT(*) DESC""")):
            print("   %-16s royalty %-7s %s" % (r[0], r[1], format(r[2], ",")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
