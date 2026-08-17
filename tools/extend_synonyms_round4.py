"""
extend_synonyms_round4.py — close the 22 coverage gaps + dump the next
tables' schemas to a FILE (so nobody has to paste 300 lines).

    py extend_synonyms_round4.py

Writes schema_dump.txt in the repo root. Upload that file and round 5 gets
written against real columns for completions, DSTs, cores, casing, shows,
pressures, curves, mud logs and seismic sets.
"""

import sys
import time
import traceback

SERVER = r"localhost\SQLEXPRESS"
DB = "DataView_Demo"
SCHEMA = "dataview"
OUT = "schema_dump.txt"

# Real data tables worth a pack, from the round-3 report. Deliberately NOT
# included: _STG_*, *_BACKUP*, *_EXT_* (vendor-specific extension tables),
# the file catalogs and DOCUMENT_LOCATION (system), DV_SPATIAL_LAYER.
NEXT_TABLES = [
    "DV_WELL_PETRO_ZONE", "DV_WELL_COMPLETION", "DV_WELL_DST",
    "DV_WELL_CORE", "DV_WELL_CORE_SAMPLE", "DV_WELL_CASING",
    "DV_WELL_STIMULATION", "DV_WELL_SHOWS", "DV_WELL_PRESSURE",
    "DV_LOG_CURVE", "DV_WELL_MUD_LOG", "DV_SEIS_SET",
    "DV_WELL_PETRO_INTERP",
]

_REMARK = ["remark", "remarks", "comment", "comments", "note", "notes",
           "description", "desc", "observation"]
_DEPTH_UOM = ["depth_ouom", "depth_uom", "depth_units", "depth_unit",
              "units", "uom", "depth_unit_of_measure"]

PACK4 = {
    "DV_PROD_ENTITY": {
        "operator_ba_id": ["operator_ba_id", "operator_id", "operator",
                           "oper_id", "company_id", "operator_name"],
        "remark": _REMARK,
    },
    "DV_PROD_VOLUME": {
        "rate_ouom": ["rate_ouom", "rate_uom", "rate_units", "rate_unit",
                      "rate_unit_of_measure"],
        "remark": _REMARK,
    },
    "DV_WELL": {
        "field_id": ["field_id", "field_code", "field_num", "field_key"],
        "remark": _REMARK,
    },
    "DV_WELL_DIR_SRVY_HDR": {
        "depth_ouom": _DEPTH_UOM,
        "remark": _REMARK,
    },
    "DV_WELL_DIR_SRVY_STA": {
        "depth_ouom": _DEPTH_UOM,
    },
    "DV_WELL_FORMATION_TOP": {
        "strat_name_set": ["strat_name_set", "name_set", "nomenclature",
                           "strat_nomenclature", "lexicon", "strat_column",
                           "naming_convention"],
        "strat_unit_subtype": ["strat_unit_subtype", "unit_subtype",
                               "subtype", "sub_type"],
        "age_top_ma": ["age_top_ma", "age_top", "top_age", "age_ma_top"],
        "age_base_ma": ["age_base_ma", "age_base", "base_age", "age_ma_base"],
        "depth_ouom": _DEPTH_UOM,
        "depth_datum": ["depth_datum", "datum", "elev_ref",
                        "depth_reference", "reference_datum"],
        "owc_depth": ["owc", "owc_depth", "oil_water_contact",
                      "oil_water_contact_depth", "o_w_contact"],
        "goc_depth": ["goc", "goc_depth", "gas_oil_contact",
                      "gas_oil_contact_depth", "g_o_contact"],
        "gwc_depth": ["gwc", "gwc_depth", "gas_water_contact",
                      "gas_water_contact_depth", "g_w_contact"],
        "interp_date": ["interp_date", "interpretation_date", "pick_date",
                        "date_picked", "date_interpreted"],
        "confidence_level": ["confidence_level", "confidence", "quality",
                             "quality_code", "pick_quality", "reliability",
                             "certainty"],
        "remark": _REMARK,
    },
    "DV_WELL_LOG": {
        "remark": _REMARK,
    },
}


def say(*args):
    sys.stdout.write(" ".join(str(a) for a in args) + "\n")
    sys.stdout.flush()


def main():
    from dataview.import_data.bulk_dir_loader import get_engine
    from dataview.import_data import synonym_store as syn
    from sqlalchemy import text

    eng = get_engine(SERVER, DB)
    say("=" * 72)
    say("Synonym pack — round 4 (closing the gaps) + schema dump")
    say("=" * 72)

    say("\n[1/3] seeding PACK4…")
    t0 = time.time()
    rep = syn.seed_synonyms(eng, SCHEMA, pack=PACK4, by="SEED4")
    say(f"      candidates : {rep['candidates']:,}")
    say(f"      inserted   : {rep['inserted']:,} · {time.time() - t0:.1f}s")
    for c in rep["conflicts"][:12]:
        say(f"      conflict   : {c}")
    for tbl, col, why in rep["dropped"]:
        say(f"      DROPPED    : {tbl}.{col} — {why}")
    if not rep["dropped"]:
        say("      dropped    : none")

    sysfilter = """
        LOWER(a.target_column) NOT IN ('row_created_by','row_created_date',
            'row_changed_by','row_changed_date','active_ind','source',
            'inventory_id')
        AND LOWER(a.target_column) NOT LIKE 'h3[_]%'
        AND LOWER(a.target_column) NOT LIKE 'geog%'
        AND LOWER(a.target_column) NOT LIKE '%[_]hash'
    """

    say("\n[2/3] coverage after round 4:")
    with eng.connect() as cx:
        for tt, mappable, covered, syns in cx.execute(text(f"""
                SELECT a.target_table, COUNT(*) AS mappable,
                       SUM(CASE WHEN s.n > 0 THEN 1 ELSE 0 END) AS covered,
                       SUM(ISNULL(s.n, 0)) AS synonyms
                FROM {SCHEMA}.dv_target_attribute a
                OUTER APPLY (
                    SELECT COUNT(*) AS n FROM {SCHEMA}.dv_column_synonym c
                    WHERE c.target_table = a.target_table
                      AND c.target_column = a.target_column
                      AND c.active_ind = 'Y') s
                WHERE {sysfilter}
                  AND EXISTS (SELECT 1 FROM {SCHEMA}.dv_column_synonym x
                              WHERE x.target_table = a.target_table)
                GROUP BY a.target_table
                ORDER BY a.target_table""")).fetchall():
            flag = "  ✓ complete" if mappable == covered else \
                   f"  {mappable - covered} gap(s)"
            say(f"      {tt:30s} {covered:3d}/{mappable:<3d} covered · "
                f"{syns:4d} synonyms{flag}")

        gaps = cx.execute(text(f"""
            SELECT a.target_table, a.target_column
            FROM {SCHEMA}.dv_target_attribute a
            WHERE {sysfilter}
              AND EXISTS (SELECT 1 FROM {SCHEMA}.dv_column_synonym x
                          WHERE x.target_table = a.target_table)
              AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.dv_column_synonym c
                              WHERE c.target_table = a.target_table
                                AND c.target_column = a.target_column
                                AND c.active_ind = 'Y')
            ORDER BY a.target_table, a.ordinal""")).fetchall()
        if gaps:
            say("\n      still uncovered:")
            for tt, tc in gaps:
                say(f"        {tt}.{tc}")
        else:
            say("\n      every mappable column in every seeded table now has "
                "at least one synonym.")

        say(f"\n[3/3] writing {OUT} for the next round…")
        lines = ["Schema dump for synonym pack round 5",
                 "generated by extend_synonyms_round4.py", "=" * 60, ""]
        n_tab = 0
        for t in NEXT_TABLES:
            rows = cx.execute(text(f"""
                SELECT target_column, data_type, max_len, is_nullable
                FROM {SCHEMA}.dv_target_attribute
                WHERE target_table = :t ORDER BY ordinal"""),
                {"t": t}).fetchall()
            if not rows:
                lines.append(f"{t}: (no such table)\n")
                continue
            n_tab += 1
            lines.append(f"{t}  ({len(rows)} columns)")
            lines.append("-" * 60)
            for r in rows:
                width = f"({r[2]})" if r[2] else ""
                null = "  NOT NULL" if r[3] != "Y" else ""
                lines.append(f"  {r[0]:30s} {r[1]}{width}{null}")
            lines.append("")
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        say(f"      {n_tab} table(s) written to {OUT} "
            f"({sum(len(l) for l in lines):,} chars)")
        say(f"      → upload {OUT} and round 5 gets written from it")

    say("\n" + "=" * 72)
    say("DONE")
    say("=" * 72)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        say("\n!!! FAILED — traceback follows:\n")
        traceback.print_exc()
        sys.exit(1)
