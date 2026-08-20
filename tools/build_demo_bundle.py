"""build_demo_bundle.py — make a portable demo database pair.

A live demo needs the populated-database argument intact, and that argument is
the gold master: "you don't start with an empty schema, you start with 3.9
million wells." But the full master is 4.1 GB of table in a 7.5 GB database, and
a demo box does not need Alaska to make the point.

This builds WELL_REF_DEMO — the same schema and indexes, restricted to the
states the demo data actually lives in — and optionally backs it up alongside
DataView_Demo so both can be carried to a VM and restored.

    python tools/build_demo_bundle.py --dry-run
    python tools/build_demo_bundle.py
    python tools/build_demo_bundle.py --backup-to C:\\Bulk\\demo_bundle

IT NEVER TOUCHES WELL_REF OR DataView_Demo. It reads them and writes a new
database; the source stays exactly as it was.

WHY THESE STATES: every well in dataview.dv_well carries a UWI beginning 15, 30,
35 or 42 — Kansas, New Mexico, Oklahoma, Texas. Trimming to anything narrower
would leave demo wells with no matching header in the master, and the map would
show a hole exactly where the demo is pointed. Override with --states if the
demo data changes.

WHAT THE BUNDLE MUST CARRY, verified against the code rather than assumed —
WELL_REF holds five tables and only two of them are reachable:

  well_master_gold     4.03M rows  2,438 MB  every dataview_federation view that
                                             crosses databases reads this and
                                             nothing else; enrich/triage default
                                             to it  -> TRIMMED AND CARRIED
  WELL_MASTER_MINI     1,000 rows    0.5 MB  a live option in the workbench's
                                             "Reference master" dropdown, so
                                             leaving it out breaks a control the
                                             demo can visibly click
                                             -> CARRIED WHOLE
  WELL_MASTER          8.79M rows  1,510 MB  reachable only through
                                             run_pipeline's signature default,
                                             which every real caller overrides
                                             -> NOT CARRIED
  well_master_gold_bak 3.89M rows    913 MB  zero references in the repo
                                             -> NOT CARRIED
  WELL_MASTER_TEST         5 rows    0.1 MB  -> NOT CARRIED

SQL EXPRESS CAPS DATA AT 10 GB PER DATABASE. The full master fits today (7.5 GB
of data) but with no room to grow, and the log had reached 11 GB — which does
NOT count against the cap, and is worth shrinking separately. The trimmed
database lands near 2 GB, which leaves the demo box room to breathe.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SRC_DB = "WELL_REF"
SRC_TBL = "well_ref.well_master_gold"
MINI_TBL = "well_ref.WELL_MASTER_MINI"
DST_DB = "WELL_REF_DEMO"
DEMO_DB = "DataView_Demo"
DEFAULT_STATES = ("15", "30", "35", "42")

# Rebuilt on the trimmed copy. The clustered PK is what makes a uwi14 lookup a
# seek; the h3 indexes are what make the density layer usable at continental
# zoom; latlon backs the bbox queries. Skipping them makes the demo feel slow
# in exactly the places the product is supposed to feel fast.
INDEXES = [
    ("PK_well_master_gold", "CREATE UNIQUE CLUSTERED INDEX PK_well_master_gold "
                            "ON well_ref.well_master_gold(uwi14)"),
    ("IX_WM_UWI14",   "CREATE INDEX IX_WM_UWI14   ON well_ref.well_master_gold(uwi14)"),
    # r4 and r7 are deliberately absent: their _cover versions below are
    # supersets for every read the map makes, so plain copies only cost build
    # time, 209 MB each and write cost on every h3_refresh. Dropped from the
    # live master 20 Aug after confirming the density timings did not move.
    # r5/r6 stay — they carry INCLUDE (province_state), which the _cover
    # indexes do not, so something may still use them to filter by state.
    ("IX_wmg_h3_r5",  "CREATE INDEX IX_wmg_h3_r5  ON well_ref.well_master_gold(h3_r5)"),
    ("IX_wmg_h3_r6",  "CREATE INDEX IX_wmg_h3_r6  ON well_ref.well_master_gold(h3_r6)"),
    ("IX_wmg_latlon", "CREATE INDEX IX_wmg_latlon ON well_ref.well_master_gold"
                      "(surface_latitude, surface_longitude)"),
    ("IX_WM_NAME_NORM", "CREATE INDEX IX_WM_NAME_NORM ON well_ref.well_master_gold(name_norm)"),
]

# The map's density layer is the slowest thing in the product and it did not have
# to be. Measured 20 Aug on the full master, warm cache:
#
#     R4  17.73s    3,100 cells      <- the map's DEFAULT resolution
#     R5   2.31s   14,736 cells      <- 7.7x faster returning 4.7x MORE cells
#     R6  18.83s   66,437 cells
#     R7  19.95s  276,703 cells
#
# R5 was fast BY ACCIDENT: IX_wmg_h3_pending, built for the H3 backfill, is keyed
# on h3_r5, INCLUDEs (uwi14, surface_latitude, surface_longitude) and is FILTERED
# to surface_latitude IS NOT NULL — which is precisely what the density
# aggregation reads, so it answers from the index alone. r4/r6/r7 had no
# equivalent (r4 and r7 carried no INCLUDE at all), so each one scanned and
# looked up. Giving every resolution the same shape is the whole fix.
#
# The filter matters as much as the INCLUDE: a well with no latitude cannot
# appear on a map, so excluding those rows shrinks the index AND removes the
# rows the aggregation would only discard.
COVER_INDEXES = [
    (f"IX_wmg_h3_r{r}_cover",
     f"CREATE INDEX IX_wmg_h3_r{r}_cover ON well_ref.well_master_gold(h3_r{r}) "
     f"INCLUDE (uwi14, surface_latitude, surface_longitude) "
     f"WHERE surface_latitude IS NOT NULL")
    for r in (4, 5, 6, 7)
]


DRIVER = "ODBC Driver 17 for SQL Server"


def _engine(server, database):
    from dataview.core.schema_introspect import make_engine
    return make_engine(server, database, DRIVER)


def _autocommit(server, database="master"):
    """A DIRECT pyodbc connection with autocommit genuinely on.

    Do not reach for SQLAlchemy's raw_connection() here. It hands back a POOL
    PROXY, and assigning .autocommit on the proxy never reaches the driver
    connection underneath — so the statement still runs inside the implicit
    transaction. CREATE DATABASE, DBCC SHRINKFILE and BACKUP all refuse that,
    and the error names the STATEMENT ("not allowed within multi-statement
    transaction"), which reads like the SQL is wrong rather than the connection.
    Cost me two separate diagnoses on 20 Aug before it was written down.
    """
    import pyodbc
    return pyodbc.connect(
        f"DRIVER={{{DRIVER}}};SERVER={server};DATABASE={database};"
        f"Trusted_Connection=yes;", autocommit=True)


def _scalar(con, sql, **kw):
    from sqlalchemy import text as _t
    return con.execute(_t(sql), kw).scalar()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--states", default=",".join(DEFAULT_STATES),
                    help="UWI state prefixes to keep (default: %(default)s)")
    ap.add_argument("--backup-to", metavar="DIR",
                    help="also BACKUP both databases to .bak files here")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    states = [s.strip() for s in a.states.split(",") if s.strip()]
    inlist = ",".join(f"'{s}'" for s in states)

    eng = _engine(a.server, "master")
    with eng.connect() as con:
        n_src = _scalar(con, f"SELECT COUNT(*) FROM {SRC_DB}.{SRC_TBL}")
        n_keep = _scalar(con, f"SELECT COUNT(*) FROM {SRC_DB}.{SRC_TBL} "
                              f"WHERE LEFT(uwi14,2) IN ({inlist})")
        # A demo well with no header in the master is a hole on the map exactly
        # where the demo points, so check before building rather than after.
        orphan = _scalar(con, f"""
            SELECT COUNT(*) FROM {DEMO_DB}.dataview.dv_well w
             WHERE LEFT(w.uwi,2) NOT IN ({inlist})""")
        exists = _scalar(con, "SELECT DB_ID(:d)", d=DST_DB) is not None

    print(f"  source rows          {n_src:>12,}")
    print(f"  keeping states       {', '.join(states)}")
    print(f"  rows to copy         {n_keep:>12,}   ({n_keep/n_src*100:.0f}% of the master)")
    print(f"  demo wells outside   {orphan:>12,}   {'<-- THESE WOULD LOSE THEIR HEADER' if orphan else '(good)'}")
    print(f"  {DST_DB:<20} {'already exists — will be dropped' if exists else 'will be created'}")
    if a.dry_run:
        print("\n  (dry run — nothing written)")
        return 0
    if orphan:
        print("\n  refusing: widen --states so every demo well keeps its header.")
        return 1

    from sqlalchemy import text as _t
    raw = _autocommit(a.server)    # CREATE/DROP DATABASE cannot run in a txn
    cur = raw.cursor()
    try:
        if exists:
            cur.execute(f"ALTER DATABASE {DST_DB} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            cur.execute(f"DROP DATABASE {DST_DB}")
            print(f"  dropped existing {DST_DB}")
        cur.execute(f"CREATE DATABASE {DST_DB}")
        # SIMPLE recovery: this is a disposable copy, and it is how the source
        # database grew an 11 GB log in the first place.
        cur.execute(f"ALTER DATABASE {DST_DB} SET RECOVERY SIMPLE")
        cur.execute(f"USE {DST_DB}; EXEC('CREATE SCHEMA well_ref')")
        print(f"  created {DST_DB} (SIMPLE recovery)")

        print("  copying rows…", end="", flush=True)
        cur.execute(f"""
            SELECT * INTO {DST_DB}.well_ref.well_master_gold
              FROM {SRC_DB}.{SRC_TBL}
             WHERE LEFT(uwi14,2) IN ({inlist})""")
        print(f" {cur.rowcount:,}")

        # The mini master is 2,000 rows and the dropdown offers it by name, so
        # copy it whole — a filter here would only make the option lie.
        cur.execute(f"""
            SELECT * INTO {DST_DB}.{MINI_TBL} FROM {SRC_DB}.{MINI_TBL}""")
        print(f"  copied {MINI_TBL} whole ({cur.rowcount:,} rows)")

        cur.execute(f"USE {DST_DB}")
        for name, ddl in INDEXES + COVER_INDEXES:
            try:
                cur.execute(ddl)
                print(f"  index {name}")
            except Exception as e:                       # never silent
                print(f"  index {name} FAILED: {str(e)[:90]}")
    finally:
        cur.close()
        raw.close()

    with _engine(a.server, "master").connect() as con:
        mb = _scalar(con, """
            SELECT CAST(SUM(size)*8.0/1024 AS DECIMAL(10,1))
              FROM sys.master_files WHERE DB_NAME(database_id)=:d""", d=DST_DB)
        demo_mb = _scalar(con, """
            SELECT CAST(SUM(size)*8.0/1024 AS DECIMAL(10,1))
              FROM sys.master_files WHERE DB_NAME(database_id)=:d""", d=DEMO_DB)
    print(f"\n  {DST_DB:<20} {mb:>9} MB")
    print(f"  {DEMO_DB:<20} {demo_mb:>9} MB")
    print(f"  bundle total         {float(mb)+float(demo_mb):>9.1f} MB")

    if a.backup_to:
        os.makedirs(a.backup_to, exist_ok=True)
        raw = _autocommit(a.server)
        cur = raw.cursor()
        try:
            for db in (DEMO_DB, DST_DB):
                dest = os.path.join(a.backup_to, f"{db}.bak")
                print(f"  backing up {db}…", end="", flush=True)
                # Express has no backup COMPRESSION. Try it anyway (on
                # Standard the .bak is a third the size) and fall back,
                # rather than making the caller pick the right flag.
                for opts in ("INIT, COMPRESSION", "INIT"):
                    try:
                        cur.execute(
                            f"BACKUP DATABASE [{db}] TO DISK=? WITH {opts}", dest)
                        while cur.nextset():
                            pass
                        break
                    except Exception as _e:
                        # Retry ONLY when the edition rejected compression.
                        # A blanket except here masks the real fault and makes
                        # the fallback's message a lie — which is exactly what
                        # happened when STATS=0 (out of range) was blamed on
                        # compression.
                        if "COMPRESSION" in opts and "compress" in str(_e).lower():
                            print(" (uncompressed)", end="", flush=True)
                            continue
                        raise
                print(f" {os.path.getsize(dest)/1048576:.0f} MB -> {dest}")
        except Exception as e:
            # COMPRESSION is not available on Express; say so rather than fail
            # with something that reads like a permissions problem.
            print(f"\n  backup failed: {str(e)[:140]}")

        finally:
            cur.close()
            raw.close()

    print("\n  restore on the demo box AS 'WELL_REF' — the app reaches the master "
          "by three-part name, so the database has to carry that name there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
