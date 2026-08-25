"""Reject a staged row: out of limbo, with a reason, without destroying it.

THE RULE THIS SETTLES. "Hold, don't drop" made held rows permanent residents of
staging -- a queue nobody drains, which is its own kind of wrong: after a while
nobody reads it, and a real problem hides among the ones you decided to live
with. The answer is not to start deleting. It is to make REJECT a state a
person can put a row into deliberately, so every staged row ends as promoted or
rejected and none of them ends as neither.

Rejecting is a MOVE, not a delete:

  * the staged row is copied verbatim into load_rejects as JSON, so nothing
    about it is lost -- including columns no mapping ever used
  * a reason is required, because a rejects table without reasons is a
    landfill, and six months from now the reason is the only thing that makes
    the row worth keeping
  * then, and only then, it leaves stg

Reinstating is the same move backwards, which is why the row is kept whole.

WHY JSON AND ONE TABLE. dataview_archive already has dv_*_orphans tables, but
they are dv_-SHAPED, and a staged row is SOURCE-shaped: API_NUMBER, not uwi.
Copying into them would need the column map and would drop anything unmapped --
which is exactly the data most worth keeping when you are trying to work out
why a row could not load. One table, any shape, nothing lost.
"""
import json

from sqlalchemy import text

TABLE = "dataview.load_rejects"

_DDL = """
IF OBJECT_ID('dataview.load_rejects') IS NULL
CREATE TABLE dataview.load_rejects (
    reject_id       bigint IDENTITY(1,1) PRIMARY KEY,
    rejected_date   datetime2      NOT NULL CONSTRAINT DF_load_rejects_date
                                            DEFAULT SYSUTCDATETIME(),
    rejected_by     nvarchar(60)   NOT NULL,
    source_table    nvarchar(200)  NOT NULL,
    target_table    nvarchar(200)  NULL,
    src_file        nvarchar(400)  NULL,
    key_column      nvarchar(128)  NULL,
    key_value       nvarchar(400)  NULL,
    reason          nvarchar(400)  NOT NULL,
    row_json        nvarchar(max)  NOT NULL,
    reinstated_date datetime2      NULL
)
"""

_IX = """
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_load_rejects_src' AND object_id = OBJECT_ID('dataview.load_rejects'))
CREATE INDEX IX_load_rejects_src ON dataview.load_rejects (source_table, key_value)
"""


def _match(col, pad, alias=""):
    """The expression that decides which staged rows a key value names.

    THE SAME ONE null_parent_link USES. The panel hands us PADDED uwis, while a
    staging column holds the raw source value -- API_NUMBER is "49-025-09764".
    Matching those raw would silently move nothing, and a reject button that
    reports 0 while the rows sit there is worse than no button. pad_sql is
    imported rather than reimplemented: the UWI-14 pad has enough copies.
    """
    ref = ("[%s].[%s]" % (alias, col)) if alias else "[%s]" % col
    if pad:
        from dataview.import_data.seed_from_master import pad_sql
        return pad_sql(ref)
    return "LTRIM(RTRIM(CAST(%s AS nvarchar(400))))" % ref


def ensure_table(engine):
    """Create the rejects table if it is not there. Safe to call every time."""
    with engine.begin() as cx:
        cx.execute(text(_DDL))
        cx.execute(text(_IX))


def preview(engine, stg_table, key_column, key_values, pad=False):
    """How many staged rows a reject would move, without moving them."""
    if not key_values:
        return 0
    with engine.connect() as cx:
        return cx.execute(text(
            "SELECT COUNT(*) FROM stg.[%s] s WHERE %s IN :vals"
            % (stg_table, _match(key_column, pad, "s"))
        ).bindparams(__import__("sqlalchemy").bindparam("vals", expanding=True)),
            {"vals": [str(v) for v in key_values]}).scalar() or 0


def reject(engine, stg_table, key_column, key_values, reason,
           who="BACKLOG_PANEL", target_table=None, pad=False):
    """Move the matching staged rows into load_rejects. Returns how many moved.

    A REASON IS NOT OPTIONAL. The point of the table is that a year later
    someone can tell a decision from an accident.
    """
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("a reject needs a reason")
    if not key_values:
        return 0
    ensure_table(engine)

    import sqlalchemy as sa
    vals = [str(v) for v in key_values]
    moved = 0
    with engine.begin() as cx:
        cols = [r[0] for r in cx.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='stg' AND TABLE_NAME=:t ORDER BY ORDINAL_POSITION"),
            {"t": stg_table}).fetchall()]
        sel = cx.execute(text(
            "SELECT * FROM stg.[%s] s WHERE %s IN :vals"
            % (stg_table, _match(key_column, pad, "s"))
        ).bindparams(sa.bindparam("vals", expanding=True)), {"vals": vals}).fetchall()
        for r in sel:
            d = dict(zip(cols, r))
            cx.execute(text(
                "INSERT INTO %s (rejected_by, source_table, target_table, src_file, "
                "key_column, key_value, reason, row_json) "
                "VALUES (:by, :st, :tt, :sf, :kc, :kv, :rs, :rj)" % TABLE),
                {"by": who, "st": "stg." + stg_table, "tt": target_table,
                 "sf": str(d.get("_src_file") or "")[:400],
                 "kc": key_column,
                 "kv": str(d.get(key_column) or "")[:400],
                 "rs": reason[:400],
                 # default=str so a date or Decimal does not break the move
                 "rj": json.dumps(d, default=str)})
            moved += 1
        if moved:
            cx.execute(text(
                "DELETE FROM stg.[%s] WHERE %s IN :vals"
                % (stg_table, _match(key_column, pad))
            ).bindparams(sa.bindparam("vals", expanding=True)), {"vals": vals})
    return moved


def reject_unresolved(engine, stg_table, child_col, parent, parent_col,
                      reason, who="BACKLOG_PANEL", target_table=None):
    """Reject every staged row whose FK does not resolve. Returns how many moved.

    BY PREDICATE, NOT BY VALUE LIST. The value-list form needs somebody to
    enumerate the bad keys first, which is fine for the handful of wells the
    backlog panel already knows about and useless for a table it does not
    cover -- dv_prod_volume hangs off dv_prod_entity and was invisible to the
    CHILDREN list, so its three rows would have stayed in limbo while the
    panel reported the backlog empty. That is the coverage gap this exists to
    close: the button now clears everything the panel reports.
    """
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("a reject needs a reason")
    ensure_table(engine)
    from dataview.import_data.load_health import _key
    where = ("s.[%s] IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dataview.[%s] p "
             "WHERE %s = %s)"
             % (child_col, parent,
                _key("p", parent_col, parent_col),
                _key("s", child_col, parent_col)))
    moved = 0
    with engine.begin() as cx:
        cols = [r[0] for r in cx.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='stg' AND TABLE_NAME=:t ORDER BY ORDINAL_POSITION"),
            {"t": stg_table}).fetchall()]
        sel = cx.execute(text(
            "SELECT * FROM stg.[%s] s WHERE %s" % (stg_table, where))).fetchall()
        for r in sel:
            rec = dict(zip(cols, r))
            cx.execute(text(
                "INSERT INTO %s (rejected_by, source_table, target_table, src_file, "
                "key_column, key_value, reason, row_json) "
                "VALUES (:by, :st, :tt, :sf, :kc, :kv, :rs, :rj)" % TABLE),
                {"by": who, "st": "stg." + stg_table, "tt": target_table,
                 "sf": str(rec.get("_src_file") or "")[:400],
                 "kc": child_col,
                 "kv": str(rec.get(child_col) or "")[:400],
                 "rs": reason[:400],
                 "rj": json.dumps(rec, default=str)})
            moved += 1
        if moved:
            # ALIASED DELETE, so the predicate is the SAME STRING that selected
            # the rows. Rewriting it to drop the alias is how a delete comes to
            # match a different set than the copy did.
            cx.execute(text("DELETE s FROM stg.[%s] s WHERE %s"
                            % (stg_table, where)))
    return moved


def reinstate(engine, reject_ids):
    """Put rejected rows back in staging. The move, backwards.

    Only possible because the row was kept whole. Marks rather than deletes the
    reject record, so the fact that it was once rejected -- and why -- survives
    the reinstatement.
    """
    if not reject_ids:
        return 0
    import sqlalchemy as sa
    back = 0
    with engine.begin() as cx:
        rows = cx.execute(text(
            "SELECT reject_id, source_table, row_json FROM %s "
            "WHERE reject_id IN :ids AND reinstated_date IS NULL" % TABLE
        ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": list(reject_ids)}).fetchall()
        for rid, src, rj in rows:
            d = json.loads(rj)
            d.pop("_row_id", None)          # IDENTITY on the staging table
            tbl = str(src).split(".")[-1]
            have = {r[0].lower() for r in cx.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA='stg' AND TABLE_NAME=:t"), {"t": tbl}).fetchall()}
            use = {k: v for k, v in d.items() if k.lower() in have}
            if not use:
                continue
            cx.execute(text(
                "INSERT INTO stg.[%s] (%s) VALUES (%s)"
                % (tbl, ", ".join("[%s]" % c for c in use),
                   ", ".join(":%s" % c for c in use))), use)
            cx.execute(text(
                "UPDATE %s SET reinstated_date = SYSUTCDATETIME() "
                "WHERE reject_id = :i" % TABLE), {"i": rid})
            back += 1
    return back


def summary(engine):
    """[{source_table, reason, rows, last}] — what has been rejected and why."""
    try:
        with engine.connect() as cx:
            if not cx.execute(text("SELECT OBJECT_ID('%s')" % TABLE)).scalar():
                return []
            return [dict(zip(("source_table", "reason", "rows", "last"), r))
                    for r in cx.execute(text(
                        "SELECT source_table, reason, COUNT(*) n, MAX(rejected_date) "
                        "FROM %s WHERE reinstated_date IS NULL "
                        "GROUP BY source_table, reason ORDER BY COUNT(*) DESC" % TABLE
                    )).fetchall()]
    except Exception:
        return []
