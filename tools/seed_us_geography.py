r"""Populate dv_province_state and dv_county from real reference data.

Both tables exist and hold ZERO rows, which is why fill_lease_demo_data had
to do point-in-polygon in Python against the map's boundary file: the
database could not answer "which county is this tract in". These are
reference tables; they should have been seeded on day one.

TWO REAL SOURCES, NEITHER OF THEM INVENTED

  names       us_geo, the boundary file the map already ships. 52 states and
              their counties, already on disk, already trusted by the map.

  FIPS        The Census Bureau's national county file. us_geo carries only
              {state, county} -- no FIPS, no GEOID -- and dv_county has
              columns for all of them. A county without its FIPS code is a
              county you cannot join to anything else, which is most of the
              point of having the table.

              https://www2.census.gov/geo/docs/reference/codes2020/
                  national_county2020.txt

WHERE THE TWO DISAGREE, NEITHER IS GUESSED. Census says "Converse County";
us_geo says "Converse". Matching strips the trailing "County"/"Parish"/
"Borough"/"Census Area" and compares case-insensitively -- and a county that
still does not match is loaded from us_geo with NULL FIPS and SAID SO, rather
than being given the FIPS of something with a similar name.

SOURCE IS A REGISTERED CODE. dv_county.source and dv_province_state.source
both have foreign keys to dv_r_source, and neither "TIGER" nor "CENSUS" is
registered -- registering one is a vocabulary decision the Reference Tables
page owns. USGS is registered and is a US federal mapping authority; the
exact origin goes in remark, which has no guard on it. Same call as
seed_business_associates, for the same reason.

    python tools/seed_us_geography.py            # what it would create
    python tools/seed_us_geography.py --apply
    python tools/seed_us_geography.py --clear --apply
"""
import argparse
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CENSUS = ("https://www2.census.gov/geo/docs/reference/codes2020/"
          "national_county2020.txt")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
STAMP = "SEED_US_GEO"
SOURCE_CODE = "USGS"

# The state FIPS codes, from the same Census file's STATEFP column -- built
# from it at run time rather than typed out, so there is one source of truth.
SUFFIXES = (" county", " parish", " borough", " census area",
            " municipality", " city and borough", " municipio")


def norm(name):
    """'Converse County' and 'Converse' compare equal; nothing else does."""
    n = (name or "").strip().lower()
    for s in SUFFIXES:
        if n.endswith(s):
            n = n[:-len(s)]
            break
    return n.replace(".", "").replace("'", "").strip()


def census_rows(log=print):
    """[(state_abbrev, statefp, countyfp, county_name)] or [] on failure."""
    try:
        req = urllib.request.Request(CENSUS, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r:
            text = r.read().decode("latin-1")
    except Exception as exc:
        log("   Census file unavailable (%s) -- names only, FIPS NULL"
            % str(exc)[:60])
        return []
    out = []
    for line in text.splitlines()[1:]:
        p = line.split("|")
        if len(p) >= 5:
            out.append((p[0].strip(), p[1].strip(), p[2].strip(), p[4].strip()))
    log("   Census county rows: %s" % format(len(out), ","))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    from sqlalchemy import text as t
    from dataview.mapping import us_geo
    eng = make_engine(a.database)

    if a.clear:
        with eng.begin() as cx:
            for tb in ("dv_county", "dv_province_state", "dv_country"):
                n = cx.execute(t("SELECT COUNT(*) FROM dataview.%s "
                                 "WHERE row_created_by = :s" % tb),
                               {"s": STAMP}).scalar()
                if a.apply:
                    cx.execute(t("DELETE FROM dataview.%s WHERE row_created_by "
                                 "= :s" % tb), {"s": STAMP})
                print("   %-20s %s %s row(s)"
                      % (tb, "removed" if a.apply else "would remove",
                         format(n, ",")))
        if not a.apply:
            print("\nDRY RUN -- add --apply.")
        return 0

    states = us_geo.states()
    counties = {s: list(us_geo.counties(s) or []) for s in states}
    n_cty = sum(len(v) for v in counties.values())
    print("us_geo: %s state(s), %s county/counties"
          % (len(states), format(n_cty, ",")))

    cen = census_rows()
    fips_state = {}
    fips_cty = {}
    for ab, sfp, cfp, nm in cen:
        fips_state.setdefault(ab, sfp)
        fips_cty[(ab, norm(nm))] = (sfp, cfp, nm)
    matched = sum(1 for s in states for c in counties[s]
                  if (s, norm(c)) in fips_cty)
    print("   counties matched to a FIPS code: %s of %s"
          % (format(matched, ","), format(n_cty, ",")))
    if matched < n_cty:
        print("   %s will load with NULL FIPS rather than a guessed one"
              % format(n_cty - matched, ","))

    if not a.apply:
        print("\nDRY RUN -- re-run with --apply. Undo with --clear --apply.")
        return 0

    # THE COUNTRY FIRST. dv_province_state.country_code has an FK to
    # dv_country, and dv_country is empty too -- so seeding states before
    # the country they belong to fails on the first row, which is what it
    # did. The reference chain is country -> state -> county, and it has to
    # be filled in that order.
    with eng.begin() as cx:
        cx.execute(t("""
            INSERT INTO dataview.dv_country
                (country_code, country_code_a2, country_name, continent,
                 un_m49_code, currency_code, active_ind, source, remark,
                 row_created_by, row_created_date)
            SELECT 'USA', 'US', 'United States of America', 'North America',
                   '840', 'USD', 'Y', :src,
                   'Seeded so dv_province_state has a country to point at',
                   :stamp, SYSUTCDATETIME()
             WHERE NOT EXISTS (SELECT 1 FROM dataview.dv_country
                                WHERE country_code = 'USA')"""),
            {"src": SOURCE_CODE, "stamp": STAMP})

    with eng.begin() as cx:
        for s in states:
            cx.execute(t("""
                INSERT INTO dataview.dv_province_state
                    (province_state_id, country_code, province_state_name,
                     province_state_abbrev, province_state_type, fips_code,
                     active_ind, source, remark, row_created_by,
                     row_created_date)
                SELECT :id, 'USA', :nm, :ab, 'STATE', :fp, 'Y', :src, :rem,
                       :stamp, SYSUTCDATETIME()
                 WHERE NOT EXISTS (SELECT 1 FROM dataview.dv_province_state
                                    WHERE province_state_id = :id)"""),
                {"id": s, "nm": us_geo.state_name(s) or s, "ab": s,
                 "fp": fips_state.get(s), "src": SOURCE_CODE,
                 "rem": "Names from us_geo boundary file; FIPS from US Census "
                        "national_county2020",
                 "stamp": STAMP})

    made = 0
    pending = 0
    cxm = eng.begin()
    cx = cxm.__enter__()
    try:
        for s in states:
            for c in counties[s]:
                hit = fips_cty.get((s, norm(c)))
                sfp, cfp = (hit[0], hit[1]) if hit else (None, None)
                cid = "%s_%s" % (s, norm(c).replace(" ", "_").upper()[:36])
                cx.execute(t("""
                    INSERT INTO dataview.dv_county
                        (county_id, province_state_id, country_code,
                         county_name, county_type, fips_state_code,
                         fips_county_code, fips_full, tiger_geoid,
                         active_ind, source, remark, row_created_by,
                         row_created_date)
                    SELECT :id, :st, 'USA', :nm, 'COUNTY', :sfp, :cfp,
                           :full, :geoid, 'Y', :src, :rem, :stamp,
                           SYSUTCDATETIME()
                     WHERE NOT EXISTS (SELECT 1 FROM dataview.dv_county
                                        WHERE county_id = :id)"""),
                    {"id": cid[:40], "st": s, "nm": c[:255],
                     "sfp": sfp, "cfp": cfp,
                     "full": (sfp + cfp) if (sfp and cfp) else None,
                     "geoid": (sfp + cfp) if (sfp and cfp) else None,
                     "src": SOURCE_CODE,
                     "rem": ("Name from us_geo; FIPS from US Census "
                             "national_county2020" if hit else
                             "Name from us_geo; no Census FIPS match"),
                     "stamp": STAMP})
                made += 1
                pending += 1
                # COMMIT IN CHUNKS. The lease fill held one transaction over
                # 21,799 updates and stopped the map drawing; reference data
                # is smaller but the lesson is not size-dependent.
                if pending >= 500:
                    cxm.__exit__(None, None, None)
                    pending = 0
                    cxm = eng.begin()
                    cx = cxm.__enter__()
    finally:
        cxm.__exit__(None, None, None)

    with eng.connect() as cx:
        ns = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_province_state")).scalar()
        nc = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_county")).scalar()
        nf = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_county "
                          "WHERE fips_full IS NOT NULL")).scalar()
    print("\ndv_province_state : %s" % format(ns, ","))
    print("dv_county         : %s  (%s with FIPS)"
          % (format(nc, ","), format(nf, ",")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
