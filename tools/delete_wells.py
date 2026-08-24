r"""Delete wells and everything hanging off them, in an order the FKs allow.

WHY A TOOL AND NOT A DELETE STATEMENT. Every one of the 24 foreign keys that
reference dataview.dv_well is NO_ACTION, so

    DELETE FROM dataview.dv_well WHERE uwi LIKE '49025%'

does not orphan anything -- it FAILS, with a conflict naming one child table
and no indication of the other 23. Deleting the children by hand in the order
they occur to you fails the same way, once per table, until the order happens
to be right.

AND ONE CHILD CANNOT BE FILTERED THE OBVIOUS WAY. dv_prod_volume keys on
prod_entity_id and carries no uwi of its own, so "delete the Teapot volumes"
has to go through dv_prod_entity. Deleting the entities first leaves 616,947
volume rows referencing entities that no longer exist -- and because nothing
FKs volumes to wells, no error is raised. That is the one deletion here that
can silently leave a mess.

THE CHILD LIST IS DERIVED FROM sys.foreign_keys, NOT WRITTEN DOWN. A list of
tables in a script is a list that goes stale the first time someone adds a
table, and this codebase has already paid for four lists that had to agree and
did not. The FK graph is the authority; this reads it every run.

    python tools/delete_wells.py --like "49025%"            # counts only
    python tools/delete_wells.py --like "49025%" --apply
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

# Children that reach a well through another table rather than by uwi.
# (table, predicate) -- the predicate must select exactly the rows whose
# ultimate parent well matches, and must run BEFORE the table it goes through.
INDIRECT = [
    ("dv_prod_volume",
     "EXISTS (SELECT 1 FROM dataview.dv_prod_entity e "
     "        WHERE e.prod_entity_id = dataview.dv_prod_volume.prod_entity_id "
     "          AND e.uwi LIKE :pat)"),
]
# These must be deleted after their own children above.
AFTER_INDIRECT = {"dv_prod_entity"}


def _children(cx):
    """[table] -- everything with a FK to dv_well, from the live graph."""
    import sqlalchemy as sa
    return [r[0] for r in cx.execute(sa.text("""
        SELECT DISTINCT OBJECT_NAME(fk.parent_object_id)
          FROM sys.foreign_keys fk
         WHERE fk.referenced_object_id = OBJECT_ID('dataview.dv_well')
         ORDER BY 1"""))]


def _has_uwi(cx, table):
    import sqlalchemy as sa
    return cx.execute(sa.text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA='dataview' AND TABLE_NAME=:t "
        "  AND LOWER(COLUMN_NAME)='uwi'"), {"t": table}).scalar() > 0


def _fk_edges(cx, tables):
    """{child: {parent, ...}} for FKs BETWEEN the given dataview tables."""
    import sqlalchemy as sa
    rows = cx.execute(sa.text("""
        SELECT OBJECT_NAME(fk.parent_object_id)     AS child,
               OBJECT_NAME(fk.referenced_object_id) AS parent
          FROM sys.foreign_keys fk
         WHERE SCHEMA_NAME(fk.schema_id) = 'dataview'""")).fetchall()
    tset = set(tables)
    edges = {t: set() for t in tables}
    for child, parent in rows:
        if child in tset and parent in tset and child != parent:
            edges[child].add(parent)
    return edges


def _delete_order(cx, tables):
    """tables, ordered so each is deleted BEFORE anything it references.

    THE FIRST CUT ONLY LOOKED AT FKs TO dv_well, and children reference each
    OTHER: dv_well_dir_srvy_sta points at dv_well_dir_srvy_hdr, so an
    alphabetical order deletes the header first and the delete fails on
    fk_srvy_sta_hdr. The graph among the children matters as much as the graph
    above them, so this sorts the whole thing.

    A table is safe to delete once nothing still in the list references it.
    """
    edges = _fk_edges(cx, tables)
    remaining = list(tables)
    out = []
    while remaining:
        # referenced_by: who, still remaining, points at me
        refd = {t: 0 for t in remaining}
        for t in remaining:
            for parent in edges.get(t, ()):
                if parent in refd:
                    refd[parent] += 1
        free = [t for t in remaining if refd[t] == 0]
        if not free:
            # A cycle. Emit the rest in the order given rather than looping
            # forever, and say so -- a silent partial order would fail later
            # with a constraint error that looks unrelated.
            print("   ! FK cycle among " + ", ".join(remaining)
                  + " -- order may need a manual pass")
            out.extend(remaining)
            break
        free.sort()
        out.extend(free)
        remaining = [t for t in remaining if t not in set(free)]
    return out


def _plan(cx, pat):
    """[(table, where, count)] in an order the foreign keys accept."""
    import sqlalchemy as sa
    kids = _children(cx)
    handled = {t for t, _p in INDIRECT}

    by_uwi = []
    for t in kids:
        if t in handled:
            continue
        if not _has_uwi(cx, t):
            print(f"   ! {t} has no uwi column and no rule -- NOT handled")
            continue
        by_uwi.append(t)
    by_uwi = _delete_order(cx, by_uwi + ["dv_well"])

    steps = list(INDIRECT)                      # indirect children first
    steps += [(t, "uwi LIKE :pat") for t in by_uwi]

    out = []
    for t, w in steps:
        n = cx.execute(sa.text(
            f"SELECT COUNT(*) FROM dataview.{t} WHERE {w}"), {"pat": pat}).scalar()
        out.append((t, w, n))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Delete wells matching a UWI pattern, children first. "
                    "Counts only unless --apply.")
    ap.add_argument("--like", required=True,
                    help=r"UWI pattern, e.g. \"49025%%\" for Teapot Dome")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    from dataview.core.dw_utils import make_engine
    import sqlalchemy as sa
    engine = make_engine(a.database)

    with engine.connect() as cx:
        plan = _plan(cx, a.like)

    print(f"Wells matching {a.like!r}, and everything below them:\n")
    total = 0
    for t, w, n in plan:
        total += n
        via = "" if w.startswith("uwi") else "   <- via dv_prod_entity"
        print(f"   {t:30s} {n:10,}{via}")
    print(f"\n   {'TOTAL':30s} {total:10,} row(s)")

    if not a.apply:
        print("\nCOUNTS ONLY -- nothing deleted. Re-run with --apply.")
        print("Order matters and is derived from sys.foreign_keys above; the "
              "indirect step runs first because dv_prod_volume has no uwi.")
        return 0
    if not total:
        print("\nNothing to delete.")
        return 0

    # ONE TRANSACTION. A half-finished delete leaves parents without children
    # or children without parents, and both states are worse than either
    # doing it or not doing it.
    with engine.begin() as cx:
        for t, w, _n in plan:
            r = cx.execute(sa.text(
                f"DELETE FROM dataview.{t} WHERE {w}"), {"pat": a.like})
            print(f"   deleted {r.rowcount:10,} from {t}")
    print("\nDone, in one transaction.")

    with engine.connect() as cx:
        left = cx.execute(sa.text(
            "SELECT COUNT(*) FROM dataview.dv_well WHERE uwi LIKE :p"),
            {"p": a.like}).scalar()
        orphan = cx.execute(sa.text("""
            SELECT COUNT(*) FROM dataview.dv_prod_volume v
             WHERE NOT EXISTS (SELECT 1 FROM dataview.dv_prod_entity e
                                WHERE e.prod_entity_id = v.prod_entity_id)""")).scalar()
    print(f"   wells still matching: {left:,}")
    print(f"   production volumes with no entity anywhere: {orphan:,}")
    if orphan:
        print("   ^ pre-existing orphans, not created here -- nothing FKs "
              "volumes to entities, so they were already unreferenced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
