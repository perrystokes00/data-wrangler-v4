"""
backfill_master_h3_bcp.py — H3 cells for the 4-million-row master reference,
using bcp in BOTH directions so pyodbc never carries the data.

    python backfill_master_h3_bcp.py --check
    python backfill_master_h3_bcp.py --apply
    python backfill_master_h3_bcp.py --apply --state TX

Run add_h3_to_master.sql first.

WHY BCP, MEASURED RATHER THAN ASSUMED
-------------------------------------
The pyodbc version managed 225 rows/sec — four hours for this table. Two
diagnoses, both wrong before the right one:

  1 · "the row-by-row UPDATE is the cost"  -> rewrote it as a staged bulk
      UPDATE ... JOIN. No improvement.
  2 · then sys.dm_exec_requests showed the truth: the running statement was a
      SELECT sitting in ASYNC_NETWORK_IO for 200 seconds. SQL Server had the
      rows ready and PYTHON WAS NOT TAKING THEM. The bottleneck was the FETCH,
      not the write.

This page's own map loader already learned that lesson — _bcp_fetch_to_csv
exists because pyodbc was too slow for well queries, and it measured 60-100x
faster. Same tool, same reason, both directions:

    bcp queryout  ->  CSV  ->  python computes cells  ->  CSV  ->  bcp in
                                                                    |
                                              one UPDATE ... JOIN <-+

H3 CANNOT BE COMPUTED SERVER-SIDE. It is an icosahedral projection with
hexagonal indexing, not arithmetic — there is no T-SQL function and no CLR
assembly worth trusting. The coordinates must come out; the only question was
how fast, and bcp is the answer this codebase already reached.

RESUMABLE. Each run reads only rows whose h3_r5 is NULL, so an interrupted run
keeps everything it committed and a re-run continues. No bookmark to maintain.
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import tempfile
import time

RESOLUTIONS = (4, 5, 6, 7)
TABLE = "well_ref.well_master_public_v2"
STAGE = "well_ref.h3_stage"          # a REAL table, not #temp: bcp connects on
                                     # its own session and cannot see a #temp
                                     # created by ours.


def _bcp(args: list[str], server: str, database: str, label: str) -> None:
    """Run bcp, and say enough to diagnose a failure.

    bcp can exit non-zero having printed NOTHING, which is how this codebase
    once produced 'BCP queryout failed:' with nothing after the colon.
    """
    cmd = ["bcp", *args, "-c", "-t|", "-C", "65001", "-T",
           f"-S{server}", f"-d{database}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    # BCP EXITS 0 ON A FAILED COPY. Measured 3 Sep: the filegroup filled at
    # 2.2M of 2.9M rows, bcp printed "BCP copy in failed" and a NativeError,
    # and still returned 0 -- so an exit-code check called a half-written
    # table a success and the run reported completion. The message is the only
    # honest signal, so it is read as well.
    failed = any(s in out for s in ("BCP copy in failed",
                                    "BCP copy out failed",
                                    "NativeError = "))
    if r.returncode != 0 or failed:
        raise RuntimeError(
            f"bcp {label} failed (exit {r.returncode}) · server {server} · "
            f"database {database} · {out or '(no output)'}")


def _cells_and_hash(lat: float, lon: float):
    import h3
    import hashlib
    cs = [h3.latlng_to_cell(lat, lon, r) for r in RESOLUTIONS]
    h = hashlib.sha256(f"{lat}|{lon}".encode("utf-8")).hexdigest().upper()
    return cs, h


def main() -> int:
    global TABLE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS")
    ap.add_argument("--database", default="WELL_REF")
    ap.add_argument("--state", default=None)
    ap.add_argument("--table", default=TABLE,
                    help="table to backfill (default %(default)s); the public "
                         "master built from disk is "
                         "well_ref.well_master_public_v2")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--keep-files", action="store_true",
                    help="leave the CSVs on disk for inspection")
    a = ap.parse_args()

    TABLE = a.table

    try:
        import h3                                    # noqa: F401
    except ImportError:
        print("h3 is not installed:  pip install h3", file=sys.stderr)
        return 2

    import pyodbc
    cn = pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={a.server};"
        f"DATABASE={a.database};Trusted_Connection=yes;", autocommit=True)
    cur = cn.cursor()

    if cur.execute(f"SELECT COL_LENGTH('{TABLE}','h3_r5')").fetchone()[0] is None:
        print("the h3 columns do not exist — run add_h3_to_master.sql first",
              file=sys.stderr)
        return 2

    _st = f" AND province_state = '{a.state}'" if a.state else ""
    pend = cur.execute(
        f"SELECT COUNT(*) FROM {TABLE} WITH (NOLOCK) "
        f"WHERE h3_r5 IS NULL AND surface_latitude IS NOT NULL{_st}").fetchone()[0]
    total = cur.execute(f"SELECT COUNT(*) FROM {TABLE} WITH (NOLOCK)").fetchone()[0]
    print(f"{total:,} well(s); {pend:,} still without cells")
    if a.check or not a.apply:
        print("-- report only; re-run with --apply")
        return 0
    if not pend:
        print("nothing to do")
        return 0

    tmp = tempfile.mkdtemp(prefix="h3_")
    src = os.path.join(tmp, "coords.csv")
    out = os.path.join(tmp, "cells.csv")
    t0 = time.time()

    try:
        # ── 1 · OUT ───────────────────────────────────────────────────────
        # queryout takes ONE LINE — bcp rejects embedded newlines.
        q = (f"SELECT uwi14, surface_latitude, surface_longitude FROM {TABLE} "
             f"WHERE h3_r5 IS NULL AND surface_latitude IS NOT NULL"
             f" AND surface_longitude IS NOT NULL{_st}")
        print("  bcp out …", flush=True)
        _bcp([q, "queryout", src, "-q"], a.server, a.database, "queryout")
        print(f"    {os.path.getsize(src):,} bytes in {time.time()-t0:,.0f}s", flush=True)

        # ── 2 · COMPUTE ───────────────────────────────────────────────────
        t1, n = time.time(), 0
        with open(src, "r", encoding="utf-8", errors="replace", newline="") as fi, \
             open(out, "w", encoding="utf-8", newline="") as fo:
            w = csv.writer(fo, delimiter="|", quoting=csv.QUOTE_NONE,
                           # NOT a backslash. With QUOTE_NONE the writer escapes
                           # the escapechar itself, which is exactly how every
                           # path in GLOBAL_FILE_CATALOG ended up doubled.
                           escapechar="\x01")
            for row in csv.reader(fi, delimiter="|"):
                if len(row) < 3:
                    continue
                try:
                    lat, lon = float(row[1]), float(row[2])
                except ValueError:
                    continue
                cs, h = _cells_and_hash(lat, lon)
                w.writerow([row[0].strip(), *cs, h])
                n += 1
        print(f"  computed {n:,} in {time.time()-t1:,.0f}s "
              f"({n/max(time.time()-t1,1):,.0f}/sec)", flush=True)

        # ── 3 · IN ────────────────────────────────────────────────────────
        # h3_coord_hash is BINARY(32); bcp character mode reads hex text into a
        # binary column, so the staging column is char(64) and the UPDATE
        # CONVERTs with style 2 (hex, no 0x prefix) — the same conversion
        # h3_grids needed on dv_well.
        cur.execute(f"""
            IF OBJECT_ID('{STAGE}','U') IS NOT NULL DROP TABLE {STAGE};
            CREATE TABLE {STAGE} (
                uwi14 char(14) NOT NULL PRIMARY KEY,
                h3_r4 nvarchar(16), h3_r5 nvarchar(16),
                h3_r6 nvarchar(16), h3_r7 nvarchar(16),
                coord_hash char(64));""")
        t2 = time.time()
        print("  bcp in …", flush=True)
        _bcp([STAGE, "in", out, "-b", "50000"], a.server, a.database, "in")
        print(f"    loaded in {time.time()-t2:,.0f}s", flush=True)

        # ── 4 · ONE SET-BASED UPDATE ──────────────────────────────────────
        t3 = time.time()
        print("  update …", flush=True)
        cur.execute(f"""
            UPDATE g
               SET g.h3_r4 = s.h3_r4, g.h3_r5 = s.h3_r5,
                   g.h3_r6 = s.h3_r6, g.h3_r7 = s.h3_r7,
                   g.h3_coord_hash = CONVERT(binary(32), s.coord_hash, 2)
              FROM {TABLE} g
              JOIN {STAGE} s ON s.uwi14 = g.uwi14;""")
        print(f"    {cur.rowcount:,} row(s) in {time.time()-t3:,.0f}s", flush=True)
        cur.execute(f"DROP TABLE {STAGE};")

        left = cur.execute(
            f"SELECT COUNT(*) FROM {TABLE} WITH (NOLOCK) "
            f"WHERE h3_r5 IS NULL AND surface_latitude IS NOT NULL{_st}").fetchone()[0]
        print(f"\ndone in {time.time()-t0:,.0f}s — {left:,} still without cells")
    except Exception as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        return 1
    finally:
        cn.close()
        if not a.keep_files:
            for p in (src, out):
                try:
                    os.remove(p)
                except OSError:
                    pass
            try:
                os.rmdir(tmp)
            except OSError:
                pass
        else:
            print(f"files kept in {tmp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
