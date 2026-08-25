r"""Mark catalog rows CATALOGED when their lineage proves they loaded.

CATALOG_STATUS is not the honest test of whether a file produced rows -- the
mirrors are a DRAIN, and the status flag is separately resettable. Two things
put 1,701 already-loaded files back in the queue:

  * file_manager's "clear all assignments" runs
    UPDATE GLOBAL_FILE_CATALOG SET CATALOG_STATUS='UNCATALOGED' with NO WHERE
    clause, so it resets every row in the catalog, not the assigned ones.
  * catalog_rules selects WHERE CATALOG_STATUS IS NULL OR ='UNCATALOGED' and
    never writes 'CATALOGED' back -- only doc_catalog_store.catalog_document
    does, from one UI page. So a file scored by that path stays selectable and
    the queue cannot drain.

The repair keys on the one thing that cannot lie: INVENTORY_ID lineage into
dv_*. A file with rows there LOADED, whatever the flag says. Files with no
rows are left exactly as they are -- promoted-but-empty and never-promoted are
different facts with different repairs, and collapsing them is how a real
backlog gets hidden.

    python tools/reconcile_catalog_status.py
    python tools/reconcile_catalog_status.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

GFC = "file_catalog.GLOBAL_FILE_CATALOG"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Reconcile CATALOG_STATUS against dv_* lineage. "
                    "Counts only unless --apply.")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    import sqlalchemy as sa
    from dataview.core.dw_utils import make_engine
    engine = make_engine(a.database)

    with engine.begin() as cx:
        tables = [r[0] for r in cx.execute(sa.text(
            "SELECT DISTINCT OBJECT_NAME(c.object_id) FROM sys.columns c "
            "WHERE c.name='INVENTORY_ID' "
            "AND OBJECT_SCHEMA_NAME(c.object_id)='dataview' "
            "AND OBJECTPROPERTY(c.object_id,'IsUserTable')=1 ORDER BY 1")).fetchall()]
        if not tables:
            print("No dataview table carries INVENTORY_ID -- nothing to key on.")
            return 2
        union = " UNION ".join(
            "SELECT INVENTORY_ID FROM dataview.[%s] WHERE INVENTORY_ID IS NOT NULL" % t
            for t in tables)
        cx.execute(sa.text("IF OBJECT_ID('tempdb..#lin') IS NOT NULL DROP TABLE #lin"))
        cx.execute(sa.text("SELECT DISTINCT INVENTORY_ID AS iid INTO #lin FROM (%s) z" % union))

        def n(sql):
            return cx.execute(sa.text(sql)).scalar()

        print("%d dv_ table(s) carry INVENTORY_ID; %s distinct source file(s) "
              "behind their rows\n" % (len(tables), format(n("SELECT COUNT(*) FROM #lin"), ",")))

        total = n("SELECT COUNT(*) FROM %s" % GFC)
        # The four states, kept apart on purpose.
        loaded_wrong = n(
            "SELECT COUNT(*) FROM %s g WHERE g.CATALOG_STATUS='UNCATALOGED' "
            "AND EXISTS (SELECT 1 FROM #lin l WHERE l.iid=g.INVENTORY_ID)" % GFC)
        empty_prom = n(
            "SELECT COUNT(*) FROM %s g WHERE g.PROMOTED_AT IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM #lin l WHERE l.iid=g.INVENTORY_ID)" % GFC)
        backlog = n(
            "SELECT COUNT(*) FROM %s g WHERE g.PROMOTED_AT IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM #lin l WHERE l.iid=g.INVENTORY_ID)" % GFC)

        print("   catalog rows                    %6s" % format(total, ","))
        print("   LOADED but marked UNCATALOGED   %6s   <- to correct"
              % format(loaded_wrong, ","))
        print("   promoted, produced no rows      %6s   <- left alone"
              % format(empty_prom, ","))
        print("   never promoted, no rows         %6s   <- the real backlog"
              % format(backlog, ","))

        if loaded_wrong:
            print("\n   by extension:")
            rows = cx.execute(sa.text(
                "SELECT g.FILE_EXT, COUNT(*) k FROM %s g "
                "WHERE g.CATALOG_STATUS='UNCATALOGED' "
                "AND EXISTS (SELECT 1 FROM #lin l WHERE l.iid=g.INVENTORY_ID) "
                "GROUP BY g.FILE_EXT ORDER BY COUNT(*) DESC" % GFC)).fetchall()
            for r in rows:
                print("      %-8s %s" % (r[0], format(r[1], ",")))

        if not a.apply:
            print("\nCOUNTS ONLY -- nothing written. Re-run with --apply.")
            return 0

        done = cx.execute(sa.text(
            "UPDATE g SET CATALOG_STATUS='CATALOGED', ROW_CHANGED_DATE=SYSUTCDATETIME() "
            "FROM %s g WHERE g.CATALOG_STATUS='UNCATALOGED' "
            "AND EXISTS (SELECT 1 FROM #lin l WHERE l.iid=g.INVENTORY_ID)" % GFC)).rowcount
        print("\n   %s row(s) marked CATALOGED." % format(done, ","))

    with engine.connect() as cx:
        left = cx.execute(sa.text(
            "SELECT COUNT(*) FROM %s WHERE CATALOG_STATUS='UNCATALOGED'" % GFC)).scalar()
        print("   still UNCATALOGED: %s  (the backlog plus the empties)"
              % format(left, ","))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
