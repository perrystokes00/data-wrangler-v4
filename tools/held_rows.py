r"""What did a Data Assistant load leave behind, and why.

The Assistant reports holds inline -- "N row(s) held, no match in dv_well" --
but that lives in session state and is gone on the next rerun. This asks the
database instead, so it still answers days later and after a restart.

THE HONEST TEST IS ARITHMETIC, NOT A FLAG. stg keeps its rows after promote
(unlike file_catalog's cat_*, which is a drain), so every staged row is still
there to compare against the target. A row whose key is absent from the target
did not land. That stays true whatever any status column says, and it avoids
the two traps this repo keeps hitting: a report reading a resettable flag, and
a report asking a drained table whether it produced rows.

The PRIMARY KEY comes from the database, never a hand-written list -- a list is
exactly what missed dv_well_dst and dv_prod_volume the last time this was
checked by hand. uwi is compared UWI-14 padded, because that is what promote
writes; without the pad the comparison is 14 chars against 12 and every row
reads as held.

    python tools/held_rows.py
    python tools/held_rows.py --table dv_well_formation_top   # one table, with causes
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _key(alias, col, target_col=None):
    """Promote-time SQL for one key column, using PROMOTE'S OWN expressions.

    Not reimplemented here. The UWI-14 pad already has three sites that must
    agree -- build_promote_sql, the repair UPDATE, and _uwi14_sql -- and it
    pads with ZEROS, not spaces; guessing that wrong is what made 1,188 staged
    wells read as unmatched while sitting two panels up the same screen. A
    fourth copy is a fourth chance to drift, so this imports.
    """
    from dataview.import_data.bulk_dir_loader import _val_expr, _uwi14_sql, _IDENT
    tgt = (target_col or col).lower()
    expr = _val_expr(alias, col, tgt in _IDENT or col.lower() in _IDENT)
    return _uwi14_sql(expr) if tgt == "uwi" else expr


def _pk_feeders(cx, sa, stg_table, target, pk, have):
    """{pk column: staging column that fills it}, via the persisted map.

    A staging table carries the SOURCE headers -- API_NUMBER, FORMATION -- and
    promote renames them, so comparing on the target's PK NAME finds nothing
    and every table reads as "no comparable key". dv_column_map is where those
    decisions were persisted, so the answer is already in the database.
    """
    out = {}
    for p in pk:
        if p.lower() in have:
            out[p] = p
            continue
        rows = cx.execute(sa.text(
            "SELECT DISTINCT source_column FROM dataview.dv_column_map "
            "WHERE UPPER(target_table) = :t AND LOWER(target_column) = :c "
            "AND ISNULL(active_ind,'Y') = 'Y'"),
            {"t": target.upper(), "c": p.lower()}).fetchall()
        for r in rows:
            if str(r[0]).lower() in have:
                out[p] = str(r[0])
                break
    return out


def _fk_causes(cx, sa, s, tgt, have, held):
    """Which missing parent explains the held rows. Prints, returns nothing."""
    print("\n   why those %s row(s) did not land:" % format(held, ","))
    fks = cx.execute(sa.text(
        "SELECT cc.name, OBJECT_NAME(fk.referenced_object_id), pc.name "
        "FROM sys.foreign_keys fk "
        "JOIN sys.foreign_key_columns f ON f.constraint_object_id = fk.object_id "
        "JOIN sys.columns cc ON cc.object_id = fk.parent_object_id "
        "  AND cc.column_id = f.parent_column_id "
        "JOIN sys.columns pc ON pc.object_id = fk.referenced_object_id "
        "  AND pc.column_id = f.referenced_column_id "
        "WHERE fk.parent_object_id = OBJECT_ID('dataview.' + :t)"), {"t": tgt}).fetchall()
    blamed = False
    for child_col, parent, parent_col in fks:
        if child_col.lower() not in have:
            continue
        where = ("s.[%s] IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dataview.[%s] p "
                 "WHERE %s = %s)"
                 % (child_col, parent,
                    _key("p", parent_col, parent_col),
                    _key("s", child_col, parent_col)))
        n = cx.execute(sa.text(
            "SELECT COUNT(*) FROM stg.[%s] s WHERE %s" % (s, where))).scalar()
        if not n:
            continue
        blamed = True
        vals = cx.execute(sa.text(
            "SELECT DISTINCT TOP 3 %s FROM stg.[%s] s WHERE %s"
            % (_key("s", child_col, parent_col), s, where))).fetchall()
        print("      %-22s -> %-24s %8s unmatched, e.g. %s"
              % (child_col, parent, format(n, ","),
                 ", ".join(repr(str(v[0])) for v in vals)))
    if not blamed:
        # NOT "unknown". A held row with every FK satisfied was refused for a
        # different reason, and duplicate keys are far and away the usual one --
        # promote is insert-only, so the second row with a key just vanishes.
        print("      No FK explains these. Every parent resolves, so the rows were")
        print("      refused for another reason -- most often a DUPLICATE KEY:")
        print("      promote is insert-only, so a repeated key is silently skipped.")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Rows the Data Assistant staged that never reached their target.")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--table", help="only this staging table, and explain why")
    a = ap.parse_args(argv)

    import sqlalchemy as sa
    from dataview.core.dw_utils import make_engine
    engine = make_engine(a.database)

    with engine.connect() as cx:
        stg = {}
        for r in cx.execute(sa.text(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA='stg' AND TABLE_TYPE='BASE TABLE' "
            "ORDER BY TABLE_NAME")).fetchall():
            n = cx.execute(sa.text("SELECT COUNT(*) FROM stg.[%s]" % r[0])).scalar()
            if n:
                stg[r[0]] = n
        if a.table:
            stg = {k: v for k, v in stg.items() if k.lower() == a.table.lower()}
            if not stg:
                print("No staged rows in stg.%s" % a.table)
                return 2
        if not stg:
            print("Nothing staged. Run a load first.")
            return 0

        cols = {}
        for r in cx.execute(sa.text(
            "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='stg'")).fetchall():
            cols.setdefault(r[0], set()).add(r[1].lower())

        print("%-30s %9s %9s %9s" % ("staging table", "staged", "landed", "HELD"))
        print("-" * 62)
        total_held = 0
        for s in sorted(stg):
            tgt = re.sub(r"_[0-9a-f]{8}$", "", s)
            pk = [r[0] for r in cx.execute(sa.text(
                "SELECT kc.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
                "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kc "
                "  ON kc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME "
                "WHERE tc.TABLE_SCHEMA='dataview' AND tc.TABLE_NAME = :t "
                "AND tc.CONSTRAINT_TYPE='PRIMARY KEY' "
                "ORDER BY kc.ORDINAL_POSITION"), {"t": tgt}).fetchall()]
            have = cols.get(s, set())
            feeders = _pk_feeders(cx, sa, s, tgt, pk, have) if pk else {}
            if not feeders:
                # SAY WHY IT WAS SKIPPED. A blank line here reads as "clean".
                print("%-30s %9s %9s %9s   no column feeds %s"
                      % (s, format(stg[s], ","), "?", "?",
                         "+".join(pk) if pk else "any key"))
                continue
            # A PARTIAL KEY OVER-MATCHES, so say so rather than quietly
            # reporting fewer holds than there are.
            partial = len(feeders) < len(pk)
            on = " AND ".join(
                "%s = %s" % (_key("s", feeders[p], p), _key("d", p, p))
                for p in pk if p in feeders)
            held = cx.execute(sa.text(
                "SELECT COUNT(*) FROM stg.[%s] s WHERE NOT EXISTS "
                "(SELECT 1 FROM dataview.[%s] d WHERE %s)" % (s, tgt, on))).scalar()
            total_held += held
            note = ""
            if partial:
                note = "   (keyed on %s only — a partial key UNDER-counts holds)" \
                       % "+".join(p for p in pk if p in feeders)
            elif held:
                note = "   <--"
            print("%-30s %9s %9s %9s%s"
                  % (s, format(stg[s], ","), format(stg[s] - held, ","),
                     format(held, ","), note))
            if a.table and held:
                _fk_causes(cx, sa, s, tgt, have, held)

        print("-" * 62)
        if total_held:
            print("%s row(s) staged but never landed." % format(total_held, ","))
            print("Held is recoverable: load the missing parent, then re-run the load.")
            if not a.table:
                print("Re-run with --table <name> to see which parent is missing.")
        else:
            print("Nothing held — every staged row reached its target.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
