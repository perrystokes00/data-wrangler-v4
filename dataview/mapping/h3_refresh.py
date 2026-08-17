"""
dataview/mapping/h3_refresh.py — keep dv_well's H3 cells true, automatically.

    from dataview.mapping.h3_refresh import refresh
    refresh(engine, log=print)          # clear junk, then backfill what's missing

    python -m dataview.mapping.h3_refresh --server localhost\\SQLEXPRESS \\
        --database DataView_Demo [--check]

WHY THIS EXISTS
---------------
h3_r4..h3_r7 and h3_coord_hash are DERIVED columns: they are a function of
surface_latitude/longitude and of nothing else. Two things kept them wrong.

1 · A GENERATOR WROTE PLACEHOLDERS. Synthetic well rows arrived carrying
    'h3_r4-869' and similar in those columns — a type-fallback inventing a
    value to make the row look complete.

2 · AND THE PLACEHOLDER DISABLED ITS OWN REPAIR. backfill_h3's default is
    only_missing=True, which keys on `h3_r5 IS NULL`. A junk value is not
    NULL, so the backfill SKIPS exactly the rows that need it. The bad data
    is self-protecting, which is why it survived several backfills.

    That is the general shape worth remembering: a wrong value is worse than
    a missing one, because every repair keyed on "missing" steps over it.

So "missing" has to mean "not a real H3 index", not "NULL". This module
widens the definition and then hands over to the existing backfill_h3 —
it does not reimplement it.

WHAT A REAL H3 INDEX LOOKS LIKE
-------------------------------
15 lowercase hex characters, e.g. 8428309ffffffff, at every resolution 0-15.
So the test is length plus alphabet, and it is expressible in T-SQL — which
matters, because the clear must be ONE set-based UPDATE rather than a row at
a time.

WHERE TO CALL IT
----------------
After anything that creates or moves well headers, because that is exactly
when coordinates appear:

  * promote, after promote_well_geog — the same place the geography column is
    derived, for the same reason
  * the Bulk Tabular Loader, after its promote step

Both call sites should wrap it so a derived-column refresh can never fail a
load that already landed its rows. It is bookkeeping, not the work.
"""
from __future__ import annotations

import argparse

from sqlalchemy import text as _t

CELL_COLS = ("h3_r4", "h3_r5", "h3_r6", "h3_r7")
HASH_COL = "h3_coord_hash"
DV_TABLE = "dataview.dv_well"

# A real H3 index is 15 lowercase hex characters. Anything else in this column
# is not a cell — it is a placeholder, a truncation, or something a loader
# invented. Written as length + alphabet so it is one sargable-enough set-based
# predicate rather than a per-row test in Python.
_INVALID = ("(LEN({c}) <> 15 OR {c} LIKE '%[^0-9a-fA-F]%')")


def _cols_present(con, table=DV_TABLE) -> set[str]:
    """Only touch columns that exist — this runs against several vintages."""
    schema, _, name = table.partition(".")
    rows = con.execute(_t("""
        SELECT c.name FROM sys.columns c
        JOIN sys.tables t ON t.object_id = c.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = :s AND t.name = :t
    """), {"s": schema, "t": name}).fetchall()
    return {r[0].lower() for r in rows}


def _invalid_predicate(cols) -> str:
    """WHERE fragment: any cell column holding a non-H3 value."""
    parts = [_INVALID.format(c=c) + f" AND {c} IS NOT NULL" for c in cols]
    return " OR ".join(f"({p})" for p in parts)


def count_invalid(engine, table=DV_TABLE) -> int:
    with engine.connect() as con:
        cols = [c for c in CELL_COLS if c in _cols_present(con, table)]
        if not cols:
            return 0
        n = con.execute(_t(
            f"SELECT COUNT(*) FROM {table} WITH (NOLOCK) "
            f"WHERE {_invalid_predicate(cols)}")).scalar()
    return int(n or 0)


def clear_invalid(engine, table=DV_TABLE, log=print) -> int:
    """NULL every derived H3 column on rows whose cells are not real indexes.

    ONE set-based UPDATE. Clears the hash too: it is derived from the same
    coordinates, so a row with a junk cell has no reason to be trusted for
    the hash either, and leaving it non-null would make the same
    skip-the-repair mistake one column over.
    """
    with engine.begin() as con:
        present = _cols_present(con, table)
        cols = [c for c in CELL_COLS if c in present]
        if not cols:
            log("  h3: no cell columns on this table — nothing to do")
            return 0
        sets = ", ".join(f"{c} = NULL" for c in cols)
        if HASH_COL in present:
            sets += f", {HASH_COL} = NULL"
        res = con.execute(_t(
            f"UPDATE {table} SET {sets} WHERE {_invalid_predicate(cols)}"))
        n = res.rowcount or 0
    if n:
        log(f"  h3: cleared {n:,} row(s) holding placeholder cells "
            f"(a non-NULL junk value makes only_missing skip the repair)")
    return n


def refresh(engine, table=DV_TABLE, only_missing=True, log=print) -> dict:
    """Clear placeholders, then backfill. Safe to call after every load.

    Returns {"cleared": n, "backfilled": <whatever backfill_h3 returns>}.
    Never raises: a derived-column refresh must not fail a load that has
    already landed its rows.
    """
    out = {"cleared": 0, "backfilled": None, "error": None}
    try:
        out["cleared"] = clear_invalid(engine, table=table, log=log)
    except Exception as e:                       # noqa: BLE001
        out["error"] = f"clear: {type(e).__name__}: {e}"
        log(f"  h3: clear skipped — {out['error']}")

    try:
        from dataview.mapping.h3_grids import backfill_h3
        out["backfilled"] = backfill_h3(engine, only_missing=only_missing)
        log(f"  h3: backfill done ({out['backfilled']})")
    except Exception as e:                       # noqa: BLE001
        out["error"] = (out["error"] or "") + f" backfill: {type(e).__name__}: {e}"
        log(f"  h3: backfill skipped — {type(e).__name__}: {e}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--table", default=DV_TABLE)
    ap.add_argument("--check", action="store_true",
                    help="report how many rows hold placeholders; change nothing")
    ap.add_argument("--all", action="store_true",
                    help="recompute every row, not only the missing ones")
    a = ap.parse_args()

    from dataview.import_data.bulk_dir_loader import make_engine
    eng = make_engine(a.server, a.database)

    if a.check:
        n = count_invalid(eng, a.table)
        print(f"{a.table}: {n:,} row(s) hold placeholder H3 cells")
        return 1 if n else 0

    r = refresh(eng, table=a.table, only_missing=not a.all)
    print(r)
    return 0 if not r["error"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
