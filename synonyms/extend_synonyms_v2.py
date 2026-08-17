"""
extend_synonyms.py — round 2 of the pack, written against Perry's ACTUAL
dv_well columns (the install's DROPPED list showed where my first guesses
missed: final_td not total_depth, province_state not state, operator_name
not operator, lat/long+epsg not surface_x/y).

    py extend_synonyms.py                       # apply, then dump DV_WELL_LOG
    py extend_synonyms.py DV_WELL_LOG DV_SEIS_LINE   # dump other tables too

Two kinds of change:
  PACK2  — new synonyms for columns the first pack never covered. Seeded the
           normal way: never overwrites anything already stored.
  FIXES  — corrections where a seeded synonym now points at the WRONG column
           because the right one exists after all. Applied as operator-grade
           overrides, because a wrong synonym is worse than a missing one.

Ends by dumping real columns for whatever tables you name, so the next round
of the pack is written against the schema instead of against my memory.
"""

import sys
import time
import traceback

SERVER = r"localhost\SQLEXPRESS"
DB = "DataView_Demo"
SCHEMA = "dataview"

DUMP = [a for a in sys.argv[1:] if not a.startswith("-")] or ["DV_WELL_LOG"]


PACK2 = {
    "DV_WELL": {
        # the six my first pack got wrong, now pointed at the real columns
        "final_td": ["total_depth", "td", "tot_depth", "td_md", "total_md",
                     "total_measured_depth", "depth_total", "final_depth",
                     "final_td", "td_final", "measured_td"],
        "province_state": ["state", "province", "state_name", "state_prov",
                           "state_province", "province_state", "st"],
        "operator_name": ["operator", "operator_name", "oper", "company",
                          "current_operator", "opco", "operator_co",
                          "company_name", "operator_desc"],
        "operator_ba_id": ["operator_id", "oper_id", "company_id",
                           "operator_ba_id", "opco_id"],
        "current_operator_ba_id": ["current_operator_id",
                                   "current_operator_ba_id", "curr_oper_id"],
        "original_operator_ba_id": ["original_operator_id",
                                    "original_operator_ba_id",
                                    "orig_oper_id", "initial_operator"],
        "lease_name": ["lease", "lease_name", "leasename", "lease_desc"],
        # columns the first pack never touched at all
        "well_type": ["well_type", "type", "wellbore_type", "well_class",
                      "class", "well_category", "wellclass"],
        "api_num": ["api_num", "api_number_raw", "api_string"],
        "license_num": ["license", "licence", "license_no", "license_num",
                        "lic_num", "licence_no", "well_license"],
        "permit_number": ["permit", "permit_no", "permit_num",
                          "permit_number", "drilling_permit"],
        "abandonment_date": ["abandonment_date", "abandon_date", "plug_date",
                             "pa_date", "abandoned", "date_abandoned",
                             "plugged_date"],
        "bottom_hole_latitude": ["bottom_hole_latitude", "bh_lat",
                                 "bhl_lat", "bottom_hole_lat", "bh_latitude",
                                 "bottomhole_latitude", "bhlat"],
        "bottom_hole_longitude": ["bottom_hole_longitude", "bh_long",
                                  "bhl_long", "bottom_hole_long",
                                  "bh_longitude", "bottomhole_longitude",
                                  "bhlong", "bh_lon"],
        "depth_datum": ["depth_datum", "datum", "elev_ref", "reference_datum",
                        "depth_reference", "datum_ref"],
        "elevation_ouom": ["elevation_ouom", "elev_uom", "elevation_units",
                           "elev_units", "elevation_uom"],
        "epsg_code": ["epsg", "epsg_code", "srid", "crs_code", "crs",
                      "coord_system_code"],
        "long_lat_source": ["long_lat_source", "coord_source",
                            "location_source", "position_source",
                            "latlong_source"],
        "formation_at_td": ["formation_at_td", "td_formation", "td_fm",
                            "formation_td", "deepest_formation"],
        "producing_formation": ["producing_formation", "prod_formation",
                                "completion_formation", "pay_zone",
                                "producing_zone", "reservoir"],
        "onshore_offshore_ind": ["onshore_offshore_ind", "onshore_offshore",
                                 "offshore_ind", "onshore", "offshore_flag"],
        "legal_survey_type": ["legal_survey_type", "survey_system",
                              "legal_survey", "location_survey_type"],
        "area": ["area", "area_name", "prospect", "prospect_name",
                 "sub_area"],
        "protraction_area": ["protraction_area", "protraction", "block_area",
                             "protraction_name"],
    },
}


# (table, synonym, correct_target) — overrides applied with operator grade.
FIXES = [
    # 'lease' and 'lease_name' were seeded onto well_name before we knew
    # dv_well has a real lease_name column.
    ("DV_WELL", "lease", "lease_name"),
    ("DV_WELL", "lease_name", "lease_name"),
]


def say(*args):
    sys.stdout.write(" ".join(str(a) for a in args) + "\n")
    sys.stdout.flush()


def main():
    from dataview.import_data.bulk_dir_loader import get_engine
    from dataview.import_data import synonym_store as syn
    from sqlalchemy import text

    eng = get_engine(SERVER, DB)
    say("=" * 70)
    say("Synonym pack — round 2 (written against your real columns)")
    say("=" * 70)

    say("\n[1/4] seeding PACK2…")
    t0 = time.time()
    rep = syn.seed_synonyms(eng, SCHEMA, pack=PACK2, by="SEED2")
    say(f"      candidates : {rep['candidates']:,}")
    say(f"      inserted   : {rep['inserted']:,} · {time.time() - t0:.1f}s")
    for c in rep["conflicts"][:10]:
        say(f"      conflict   : {c}")
    for tbl, col, why in rep["dropped"]:
        say(f"      DROPPED    : {tbl}.{col} — {why}")
    if not rep["dropped"]:
        say("      dropped    : none — every column in PACK2 exists")

    say("\n[2/4] purging system/audit columns from the store…")
    n_purged = syn.purge_system_synonyms(eng, SCHEMA)
    say(f"      {n_purged} synonym(s) removed (row_created_*, row_changed_*, "
        f"active_ind, source, inventory_id, h3_*, geog, *_hash)")

    say("\n[3/4] applying corrections…")
    for tbl, s, tgt in FIXES:
        syn.set_synonym(eng, SCHEMA, tbl, s, tgt, by="SEED2_FIX")
        say(f"      {tbl}: '{s}' -> {tgt}")

    say("\n[4/4] totals now stored:")
    with eng.connect() as cx:
        for tt, n in cx.execute(text(f"""
                SELECT target_table, COUNT(*) FROM {SCHEMA}.dv_column_synonym
                WHERE active_ind = 'Y'
                GROUP BY target_table ORDER BY target_table""")).fetchall():
            say(f"        {tt:28s} {n:4d}")

        for t in DUMP:
            say(f"\n      COLUMNS OF {t.upper()} (for the next round):")
            rows = cx.execute(text(f"""
                SELECT target_column, data_type, max_len, is_nullable
                FROM {SCHEMA}.dv_target_attribute
                WHERE target_table = :t ORDER BY ordinal"""),
                {"t": t.upper()}).fetchall()
            if not rows:
                say(f"        (no such table in dv_target_attribute)")
            for r in rows:
                width = f"({r[2]})" if r[2] else ""
                null = "" if r[3] == "Y" else "  NOT NULL"
                say(f"        {r[0]:28s} {r[1]}{width}{null}")

    say("\n" + "=" * 70)
    say("DONE")
    say("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        say("\n!!! FAILED — traceback follows:\n")
        traceback.print_exc()
        sys.exit(1)
