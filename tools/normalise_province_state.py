r"""Normalise dv_well.province_state to the two-letter code.

THREE SPELLINGS OF ONE COLUMN reached dv_well by three different doors:

    'Wyoming'   the Teapot tabular load        1,318 rows
    'WY'        seeded from the gold master
    '35', '15'  the Teacup synthetic CSVs -- FIPS codes as text

Nothing is wrong with any single row; what is wrong is that a filter, a
GROUP BY or an export sees two or three states where there is one. The map
does not notice because its state/county scope is SPATIAL (us_geo bbox on
lat/lon), which is exactly why this went unseen.

THE CODE IS CANONICAL, and that is not a preference. promote_catalog's
UWI-prefix enrichment writes `province_state_abbrev`; the master carries
codes for all 4,031,052 of its rows; us_geo -- which the map's Constrain-to
selector runs on -- keys on codes. The full name is the outlier.

MAPPED, NEVER GUESSED. The name/FIPS/code table comes from us_geo, the same
56-state source the map already uses, rather than a second list written here
to drift away from it. A value that does not map is REPORTED AND LEFT ALONE:
'wrong is worse than missing', and a state this tool cannot name is one a
person should look at.

Dry run by default.

    python tools/normalise_province_state.py
    python tools/normalise_province_state.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TABLE = "dataview.dv_well"
COLUMN = "province_state"


def _maps():
    """(name->code, fips->code, valid codes) from us_geo.

    code_for_label is us_geo's public name lookup; the FIPS table has no
    public accessor, so it is read directly and the fallback below keeps this
    working if that private name ever changes.
    """
    from dataview.mapping import us_geo as _g
    name_to_code = dict(getattr(_g, "_NAME_TO_CODE", {}) or {})
    fips = {k: v[0] for k, v in (getattr(_g, "_FIPS", {}) or {}).items()}
    codes = set(getattr(_g, "_CODE_TO_NAME", {}) or {})
    return name_to_code, fips, codes


def canonical(value, name_to_code, fips_to_code, codes, fips=False):
    """The two-letter code for a stored value, or None if it cannot be mapped.

    DIGITS ARE OPT-IN, and the reason is on the map. 'Wyoming' -> WY is a
    fact about spelling. '35' -> NM is a GUESS that the number is a FIPS
    state code, and in this database it is a wrong one: the Teacup synthetic
    wells store province_state '35' and '15' while their coordinates plot in
    Texas, so converting them would replace a meaningless value with a
    confident, plottable, wrong one -- the exact trade CLAUDE.md says never
    to make. Names and existing codes are unambiguous and convert by default;
    numbers need --fips and a person who has looked.
    """
    v = str(value or "").strip()
    if not v:
        return None
    u = v.upper()
    if u in codes:                       # already a code
        return u
    hit = name_to_code.get(v.title()) or name_to_code.get(v)
    if hit:
        return hit
    if fips and v.isdigit():             # FIPS as text, with or without the zero
        return fips_to_code.get(v.zfill(2))
    return None


def survey(conn, fips=False):
    """[(stored, n, canonical_or_None)] for every distinct value present."""
    from sqlalchemy import text as _t
    n2c, f2c, codes = _maps()
    rows = conn.execute(_t(
        "SELECT %s AS v, COUNT(*) AS n FROM %s "
        " WHERE %s IS NOT NULL GROUP BY %s ORDER BY 2 DESC"
        % (COLUMN, TABLE, COLUMN, COLUMN))).fetchall()
    return [(r[0], int(r[1]), canonical(r[0], n2c, f2c, codes, fips)) for r in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--fips", action="store_true",
                    help="also convert bare 2-digit FIPS codes. OFF by default: "
                         "the Teacup synthetic wells store 35/15 while plotting in "
                         "Texas, so converting them invents a state.")
    ap.add_argument("--apply", action="store_true", help="write. Without it, nothing changes.")
    a = ap.parse_args()

    from dataview.core.dw_utils import make_engine
    eng = make_engine(a.database)
    from sqlalchemy import text as _t

    with eng.connect() as cx:
        rows = survey(cx, a.fips)
    if not rows:
        print("No %s values in %s." % (COLUMN, TABLE))
        return 0

    print("\n%-16s %10s   %s" % ("stored", "rows", "-> canonical"))
    print("-" * 52)
    changes, unmapped, already = [], [], 0
    for stored, n, canon in rows:
        if canon is None:
            unmapped.append((stored, n))
            _hint = ("  (a number -- pass --fips if it is a FIPS code)"
                     if str(stored).strip().isdigit() else "")
            print("%-16s %10s   ?? NOT MAPPED - left alone%s"
                  % (stored, "{:,}".format(n), _hint))
        elif str(stored).strip() == canon:
            already += n
            print("%-16s %10s   (already canonical)" % (stored, "{:,}".format(n)))
        else:
            changes.append((stored, canon, n))
            print("%-16s %10s   -> %s" % (stored, "{:,}".format(n), canon))
    print("-" * 52)
    print("%s row(s) already canonical, %s to change, %s unmappable"
          % ("{:,}".format(already),
             "{:,}".format(sum(n for _s, _c, n in changes)),
             "{:,}".format(sum(n for _s, n in unmapped))))

    if not changes:
        print("\nNothing to do.")
        return 0
    if not a.apply:
        print("\nDRY RUN -- re-run with --apply to write.")
        return 0

    done = 0
    with eng.begin() as cx:
        for stored, canon, _n in changes:
            r = cx.execute(_t(
                "UPDATE %s SET %s = :new, row_changed_by = 'NORMALISE_STATE', "
                "row_changed_date = SYSUTCDATETIME() WHERE %s = :old"
                % (TABLE, COLUMN, COLUMN)), {"new": canon, "old": stored})
            done += r.rowcount or 0
            print("   %-16s -> %-4s  %s row(s)" % (stored, canon, r.rowcount))
    print("\nupdated %s row(s)." % "{:,}".format(done))
    with eng.connect() as cx:
        print("now:", [(s, n) for s, n, _c in survey(cx, a.fips)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
