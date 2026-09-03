"""Verify that everything reading the well master reads the SAME table.

    python tools/set_reference_master.py

WHAT THIS USED TO BE. A two-way switch between well_master_gold and a
licence-filtered copy of it. Both are gone (3 Sep). Gold was dropped because
its keys were wrong in ways a row count could never show -- all 809 Washington
wells keyed into California's number space, 37,318 Kansas wells into Georgia's,
Michigan absent entirely while 5,465 Mississippi wells wore its label -- and
the filtered copy was DERIVED from gold, so it inherited every one of them.

well_ref.well_master_public_v2 replaces both: 3,140,361 wells across 19 states,
rebuilt from each agency's own files by build_public_master.py, every state
reconciled against its source before it counted.

WHY A CHECK IS STILL WORTH HAVING. Two different things read the master and
they are configured in different places:

  * the reference WELL POINTS read geography_layers.REFERENCE_MASTER, which
    honours the DW_REF_MASTER environment variable;
  * the H3 DENSITY HEXES read dataview_federation.v_well_density_r4..r7 and
    v_well_master_arm, which are SQL views with the table name compiled in.

If those two disagree you get one source's points drawn over another's hexes.
That looks like a data fault, not a configuration mistake, and it is very hard
to diagnose from the screen. Nothing enforces the agreement, so this reports
it. Exit code is non-zero when they disagree, so it can gate a deploy.
"""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text          # noqa: E402

EXPECTED = "WELL_REF.well_ref.well_master_public_v2"
SHORT = EXPECTED.split(".")[-1]
VIEWS = ["dataview_federation.v_well_density_r4",
         "dataview_federation.v_well_density_r5",
         "dataview_federation.v_well_density_r6",
         "dataview_federation.v_well_density_r7",
         "dataview_federation.v_well_master_arm",
         "dataview.v_well_documents"]


def engine(database="DataView_Demo"):
    cs = ("DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost\\SQLEXPRESS;"
          "DATABASE=%s;Trusted_Connection=yes;" % database)
    return create_engine("mssql+pyodbc:///?odbc_connect="
                         + urllib.parse.quote_plus(cs))


def master_named_in(defn):
    """Which master a view reads. Longest name first: well_master_public_v2
    contains well_master_public, so a naive scan reports the wrong one."""
    for name in ("well_master_public_v2", "well_master_public",
                 "well_master_gold", "WELL_MASTER"):
        if defn and name in defn:
            return name
    return "(none)"


def main():
    bad = 0
    print("H3 DENSITY HEXES AND DOCUMENT COORDS -- table compiled into each view")
    with engine().connect() as c:
        for v in VIEWS:
            d = c.execute(text(
                "SELECT m.definition FROM sys.sql_modules m "
                "JOIN sys.objects o ON o.object_id = m.object_id "
                "JOIN sys.schemas s ON s.schema_id = o.schema_id "
                "WHERE s.name + '.' + o.name = :n"), {"n": v}).scalar()
            if d is None:
                print("   %-30s (view not found)" % v.split(".")[-1])
                continue
            t = master_named_in(d)
            ok = t == SHORT
            bad += not ok
            print("   %-30s %-24s%s"
                  % (v.split(".")[-1], t, "" if ok else "  <-- DISAGREES"))

    print("\nREFERENCE WELL POINTS")
    print("   DW_REF_MASTER    : %s"
          % (os.environ.get("DW_REF_MASTER")
             or "(not set -- the code default is used)"))
    try:
        from dataview.mapping import geography_layers as g
        print("   REFERENCE_MASTER : %s" % g.REFERENCE_MASTER)
        if g.REFERENCE_MASTER != EXPECTED:
            bad += 1
            print("      <-- DISAGREES with the views above")
    except Exception as ex:                          # noqa: BLE001
        bad += 1
        print("   could not import geography_layers: %s" % ex)

    with engine("WELL_REF").connect() as c:
        n, s, h, nm = tuple(c.execute(text(
            "SELECT COUNT(*), COUNT(DISTINCT province_state), "
            "SUM(CASE WHEN h3_r5 IS NULL THEN 0 ELSE 1 END), "
            "SUM(CASE WHEN name_norm IS NULL THEN 0 ELSE 1 END) "
            "FROM %s" % EXPECTED)).one())
    print("\n%s" % EXPECTED)
    print("   %s wells · %d states · %s with H3 cells · %s with name_norm"
          % (format(n, ","), s, format(h, ","), format(nm, ",")))

    print("\n%s" % ("EVERYTHING AGREES" if not bad else
                    "*** %d DISAGREEMENT(S) -- points and hexes will not match"
                    " ***" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
