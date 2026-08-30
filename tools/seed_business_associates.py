r"""Give every lease operator a BUSINESS_ASSOCIATE row, and resolve ba_id.

dv_business_associate holds ZERO rows, and dv_land_right.ba_id -- added by
the split so the resolution would not need a migration -- is NULL on all
24,178. Meanwhile operator_name is free text repeated on every lease: 2,379
rows carrying 327 distinct spellings, plus 10,924 rows carrying 7 synthetic
ones. That is the shape PPDM's entity model exists to fix.

THIS IS MASTER DATA, NOT AN INVENTED FACT, which is why it is allowed where
filling a NULL royalty rate with a guess would not be. Creating a row that
says "EOG Resources Inc. is a company" asserts nothing the lease did not
already assert; it just says it once instead of 77 times.

AND THE ADDRESSES ARE REAL. LARCS carries CompanyAddress, CompanyCity,
CompanyStateCode, CompanyZipCode and CompanyPhoneNumber, which the lease
fetch did not ask for because a lease popup does not need them -- a business
associate does. Fetched here, from the same service, so the entity rows are
populated rather than being names with empty columns.

DETERMINISTIC ba_id. sha1 of the upper-cased name, first 40 chars: the same
company gets the same id on every run, on any machine, before and after a
reload -- so re-running this cannot fork one operator into two entities.
hash() is salted per process and would do exactly that, which is the trap
lease_colour and assign_synthetic_lease_owners both already document.

THE SYNTHETIC OPERATORS GET ROWS TOO, stamped source='SYNTH_OWNER' rather
than being left out. A demo where two thirds of the leases have no resolvable
operator does not demonstrate the entity model; and the stamp is what keeps
them separable from the 327 real ones -- and what makes --clear exact.

    python tools/seed_business_associates.py             # what it would create
    python tools/seed_business_associates.py --apply
    python tools/seed_business_associates.py --clear --apply
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LARCS = ("https://gis2.statelands.wyo.gov/arcgis/rest/services/LARCS/"
         "ActiveMineralLeaseLARCS/MapServer/1/query")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CO_FIELDS = ("CompanyName,CompanyAddress,CompanyCity,CompanyStateCode,"
             "CompanyZipCode,CompanyPhoneNumber")
STAMP = "SEED_BA"


def ba_id_for(name):
    """The same company, the same id, always."""
    return hashlib.sha1(name.strip().upper().encode("utf-8")).hexdigest()[:40]


def _t(v):
    return None if v is None else (str(v).strip() or None)


def fetch_companies(log=print):
    """Company detail from LARCS, keyed by name. {} on any failure.

    A FAILURE HERE IS NOT FATAL. The names come from the database and the
    addresses are a bonus; if the service is down the entities are still
    worth creating, and saying so beats refusing to run.
    """
    out, offset = {}, 0
    try:
        while True:
            p = {"where": "CompanyName IS NOT NULL", "outFields": CO_FIELDS,
                 "returnGeometry": "false", "f": "json",
                 "resultOffset": offset, "resultRecordCount": 1000}
            req = urllib.request.Request(
                LARCS, data=urllib.parse.urlencode(p).encode(),
                headers={"User-Agent": UA,
                         "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read().decode("utf-8"))
            got = d.get("features") or []
            for f in got:
                a = f.get("attributes") or {}
                nm = _t(a.get("CompanyName"))
                if nm and nm not in out:
                    out[nm] = a
            if not got or (not d.get("exceededTransferLimit")
                           and len(got) < 1000):
                break
            offset += len(got)
            time.sleep(0.2)
        log("   company detail fetched for %s name(s)" % format(len(out), ","))
    except Exception as exc:
        log("   company detail unavailable (%s) -- names only"
            % str(exc)[:60])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--source-code", default="OPERATOR",
                    help="the dv_r_source code to stamp. Defaults to "
                         "OPERATOR, which is registered -- WY_OSLI is not, "
                         "and registering it is a vocabulary decision this "
                         "tool will not make for you. The true origin is "
                         "recorded in remark either way.")
    ap.add_argument("--include-synthetic", action="store_true",
                    help="also create entities for the invented federal "
                         "owners. Off by default: the real lessees are the "
                         "ones worth resolving first.")
    ap.add_argument("--no-detail", action="store_true",
                    help="skip the address lookup; create name-only entities")
    ap.add_argument("--clear", action="store_true",
                    help="remove every row this tool created and NULL the "
                         "ba_id values it set, and nothing else")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    from sqlalchemy import text as t
    eng = make_engine(a.database)

    if a.clear:
        with eng.connect() as cx:
            n = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_business_associate "
                             "WHERE row_created_by = :s"), {"s": STAMP}).scalar()
        print("%s entity row(s) carry the %s stamp." % (format(n, ","), STAMP))
        if not a.apply:
            print("DRY RUN -- add --apply.")
            return 0
        with eng.begin() as cx:
            cx.execute(t("UPDATE dataview.dv_land_right SET ba_id = NULL "
                         "WHERE ba_id IN (SELECT ba_id FROM "
                         "dataview.dv_business_associate "
                         "WHERE row_created_by = :s)"), {"s": STAMP})
            cx.execute(t("DELETE FROM dataview.dv_business_associate "
                         "WHERE row_created_by = :s"), {"s": STAMP})
        print("cleared.")
        return 0

    with eng.connect() as cx:
        names = [(r[0], r[1]) for r in cx.execute(t("""
            SELECT operator_name,
                   MAX(CASE WHEN row_changed_by='SYNTH_OWNER' THEN 1 ELSE 0 END)
              FROM dataview.dv_land_right
             WHERE operator_name IS NOT NULL
             GROUP BY operator_name"""))]
    if not a.include_synthetic:
        names = [(n, s) for n, s in names if not s]
    synth = sum(1 for _n, s in names if s)
    print("operators in dv_land_right : %s" % format(len(names), ","))
    print("   real                    : %s" % format(len(names) - synth, ","))
    print("   synthetic               : %s" % format(synth, ","))

    detail = {} if a.no_detail else fetch_companies()
    matched = sum(1 for n, _s in names if n in detail)
    print("   with an address from LARCS: %s" % format(matched, ","))

    # ── THE REFERENCE GUARD, CHECKED BEFORE ANYTHING IS WRITTEN ─────────
    # dv_business_associate.source has an FK to dv_r_source, and neither
    # WY_OSLI nor BLM_MLRS is registered there -- the lease tables have no
    # such FK, which is why the 24,178 leases loaded and this does not. The
    # first run found out the hard way: a 547 after the first entity row.
    #
    # A LOADER REFUSES ON AN UNREGISTERED CODE; IT NEVER SEEDS ONE. Adding a
    # row to a dv_r_* table is a decision about the vocabulary, and the
    # Reference Tables app owns that decision. Automation may skip ceremony,
    # never a decision.
    wanted = [a.source_code]
    with eng.connect() as cx:
        known = {r[0] for r in cx.execute(t(
            "SELECT source FROM dataview.dv_r_source"))}
    missing = [w for w in wanted if w not in known]
    if missing:
        print("\nREFUSING: dv_business_associate.source is checked against "
              "dv_r_source, and these are not registered:")
        for m in missing:
            print("     %s" % m)
        print("\n   Register them in the Reference Tables page (it owns "
              "dv_r_*), or pass --source-code to reuse one that exists.")
        print("   Already registered and plausible here: %s"
              % ", ".join(sorted(k for k in known
                                 if k in ("SYNTH", "OPERATOR", "INDUSTRY",
                                          "UNKNOWN", "PPDM_LOADER"))))
        return 2

    if not a.apply:
        print("\nDRY RUN -- re-run with --apply. Undo with --clear --apply.")
        return 0

    made = 0
    with eng.begin() as cx:
        for nm, is_synth in names:
            d = detail.get(nm) or {}
            cx.execute(t("""
                INSERT INTO dataview.dv_business_associate
                    (ba_id, ba_type, ba_name, short_name, address_1, city,
                     state_province, postal_code, phone_num, country,
                     active_ind, source, remark,
                     row_created_by, row_created_date)
                SELECT :id, 'OPERATOR', :nm, LEFT(:nm, 40), :ad, :ct, :st,
                       :zp, :ph, 'USA', 'Y', :src, :rem, :stamp,
                       SYSUTCDATETIME()
                 WHERE NOT EXISTS (SELECT 1 FROM dataview.dv_business_associate
                                    WHERE ba_id = :id)"""),
                {"id": ba_id_for(nm), "nm": nm[:255],
                 "ad": (_t(d.get("CompanyAddress")) or "")[:255] or None,
                 "ct": (_t(d.get("CompanyCity")) or "")[:100] or None,
                 "st": (_t(d.get("CompanyStateCode")) or "")[:100] or None,
                 "zp": (_t(d.get("CompanyZipCode")) or "")[:20] or None,
                 "ph": (_t(d.get("CompanyPhoneNumber")) or "")[:40] or None,
                 "src": a.source_code,
                 # THE TRUE ORIGIN, in a column with no reference guard on
                 # it. source has to be a registered code; remark does not,
                 # so the provenance is recorded rather than lost to the
                 # vocabulary the FK happens to allow.
                 "rem": ("Synthetic operator -- see "
                         "assign_synthetic_lease_owners.py"
                         if is_synth else
                         "Lessee from Wyoming OSLI LARCS "
                         "(ActiveMineralLeaseLARCS); address as published"),
                 "stamp": STAMP})
            made += 1
        # resolve the leases to their entity, by the same deterministic id
        cx.execute(t("""
            UPDATE r SET r.ba_id = b.ba_id
              FROM dataview.dv_land_right r
              JOIN dataview.dv_business_associate b
                ON b.ba_name = r.operator_name
             WHERE r.operator_name IS NOT NULL"""))

    with eng.connect() as cx:
        n_ba = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_business_associate")).scalar()
        n_ad = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_business_associate "
                            "WHERE address_1 IS NOT NULL")).scalar()
        n_res = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_land_right "
                             "WHERE ba_id IS NOT NULL")).scalar()
        n_un = cx.execute(t("SELECT COUNT(*) FROM dataview.dv_land_right "
                            "WHERE operator_name IS NOT NULL AND ba_id IS NULL")).scalar()
    print("\nentities            : %s (%s with a real address)"
          % (format(n_ba, ","), format(n_ad, ",")))
    print("leases resolved     : %s" % format(n_res, ","))
    print("named but unresolved: %s" % format(n_un, ","))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
