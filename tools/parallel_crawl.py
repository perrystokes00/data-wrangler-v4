"""
parallel_crawl.py — the front of the chain: parallel crawl → batch → process
============================================================================
Walks a directory tree with a POOL OF THREADS instead of a single serial walk.
Directory enumeration is I/O-bound (waiting on the filesystem / network share),
and those waits RELEASE the GIL — so threads genuinely overlap here (unlike
CPU-bound parsing, which needs processes). On a corporate UNC share with high
per-directory latency, this is the difference between a serial crawl that takes
hours and a parallel one that keeps pace with the 10-20 workers draining behind
it.

Two things at once:
  1. Discovers files (same records the serial walk_share produced).
  2. STREAMS them into file_catalog.GLOBAL_FILE_CATALOG as PROC_STATUS='pending'
     in batches — so the worker pool can start processing while the crawl is
     still discovering (crawl and process overlap).

Design:
  * A thread-safe queue of directories, seeded with the root.
  * N walker threads: pop a dir, scandir it, emit file rows, push subdirs back.
  * A pending counter tracks outstanding dirs so we know when the tree is fully
    walked (queue empty AND no dir being processed) — then signal done.
  * File rows are flushed to the DB in batches (default 2000) by the main thread
    draining a results queue, so inserts are batched, not one-per-file.

INVENTORY_ID is a deterministic hash of the file path (matches the existing
catalog's id style — a hex string), so re-crawling is idempotent: the same file
maps to the same row (INSERT … WHERE NOT EXISTS), and its PROC_STATUS is left
alone if already processed.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from datetime import datetime, timezone
from queue import Queue, Empty

from sqlalchemy import text as _t
import sys

# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCHEMA = "file_catalog"
TABLE = "GLOBAL_FILE_CATALOG"
FQ = f"{SCHEMA}.{TABLE}"


def _inv_id(path: str) -> str:
    """Deterministic INVENTORY_ID from the path (SHA-1 hex, matches the
    catalog's existing hex-id style). Same file → same id → idempotent crawl."""
    return hashlib.sha1(path.encode("utf-8", "surrogatepass")).hexdigest().upper()


def crawl(root, exts, *, walkers=12, json_peek=True, log=print):
    """Walk `root` in parallel, returning (found, folders).

    found: list of (path, name, ext, size_kb, mtime_iso, mtime_epoch, inv_id)
    folders: count of directories visited.

    This is the discovery-only form (no DB). Use crawl_to_queue() to also stream
    the results into the work queue.
    """
    exts = {e.lower() for e in exts}
    dirq: Queue = Queue()
    dirq.put(root)
    pending = {"n": 1}                  # outstanding directories (incl. queued)
    pend_lock = threading.Lock()
    found: list = []
    found_lock = threading.Lock()
    folders = {"n": 0}
    fold_lock = threading.Lock()

    def _emit(rec):
        with found_lock:
            found.append(rec)

    def _worker():
        while True:
            try:
                d = dirq.get(timeout=0.25)
            except Empty:
                with pend_lock:
                    if pending["n"] == 0:
                        return          # tree fully walked
                continue
            try:
                _scan_dir(d, exts, json_peek, dirq, pending, pend_lock,
                          _emit, fold_lock, folders)
            finally:
                with pend_lock:
                    pending["n"] -= 1   # this dir done
                dirq.task_done()

    threads = [threading.Thread(target=_worker, daemon=True)
               for _ in range(max(1, walkers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return found, folders["n"]


def _scan_dir(d, exts, json_peek, dirq, pending, pend_lock, emit,
              fold_lock, folders):
    with fold_lock:
        folders["n"] += 1
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        with pend_lock:
                            pending["n"] += 1      # new dir to process
                        dirq.put(e.path)
                        continue
                    ext = os.path.splitext(e.name)[1].lower()
                    if ext not in exts:
                        continue
                    if json_peek and ext == ".json":
                        try:
                            with open(e.path, "rb") as jf:
                                peek = jf.read(100)
                            if b'"kind"' not in peek and b'"header"' not in peek:
                                continue
                        except OSError:
                            continue
                    stt = e.stat()
                    emit((
                        e.path, e.name, ext,
                        round(stt.st_size / 1024, 2),
                        datetime.fromtimestamp(
                            stt.st_mtime, tz=timezone.utc
                        ).strftime("%Y-%m-%d %H:%M:%S"),
                        stt.st_mtime,
                        _inv_id(e.path),
                    ))
                except OSError:
                    pass
    except (PermissionError, OSError):
        pass


def crawl_to_queue(engine, root, exts, *, walkers=12, batch=2000,
                   json_peek=True, log=print):
    """Parallel-crawl `root` AND stream discovered files into the work queue as
    PROC_STATUS='pending', in batches. Idempotent: re-crawling re-inserts only
    new paths (INSERT … WHERE NOT EXISTS on INVENTORY_ID), leaving already-
    processed rows' status untouched.

    Returns {found, folders, inserted, elapsed_s}.
    """
    from dataview.file_catalog import work_queue as wq
    wq.ensure_columns(engine, log=log)

    exts = {e.lower() for e in exts}
    dirq: Queue = Queue()
    dirq.put(root)
    pending = {"n": 1}
    pend_lock = threading.Lock()
    rowq: Queue = Queue()              # discovered file records → DB writer
    folders = {"n": 0}
    fold_lock = threading.Lock()
    counts = {"found": 0, "inserted": 0}
    DONE = object()

    t0 = time.monotonic()

    def _emit(rec):
        rowq.put(rec)

    def _worker():
        while True:
            try:
                d = dirq.get(timeout=0.25)
            except Empty:
                with pend_lock:
                    if pending["n"] == 0:
                        return
                continue
            try:
                _scan_dir(d, exts, json_peek, dirq, pending, pend_lock,
                          _emit, fold_lock, folders)
            finally:
                with pend_lock:
                    pending["n"] -= 1
                dirq.task_done()

    # DB writer thread: batches rows into GLOBAL_FILE_CATALOG as pending
    def _writer():
        buf = []
        while True:
            item = rowq.get()
            if item is DONE:
                if buf:
                    counts["inserted"] += _flush(engine, buf)
                rowq.task_done()
                return
            buf.append(item)
            counts["found"] += 1
            if len(buf) >= batch:
                counts["inserted"] += _flush(engine, buf)
                log(f"[crawl] {counts['found']:,} found · "
                    f"{counts['inserted']:,} new queued")
                buf = []
            rowq.task_done()

    walker_threads = [threading.Thread(target=_worker, daemon=True)
                      for _ in range(max(1, walkers))]
    writer_thread = threading.Thread(target=_writer, daemon=True)
    writer_thread.start()
    for t in walker_threads:
        t.start()
    for t in walker_threads:
        t.join()
    rowq.put(DONE)                     # signal writer to flush + stop
    writer_thread.join()

    elapsed = time.monotonic() - t0
    log(f"[crawl] done in {elapsed:.0f}s — {counts['found']:,} files found, "
        f"{counts['inserted']:,} newly queued, {folders['n']:,} folders")
    return {"found": counts["found"], "folders": folders["n"],
            "inserted": counts["inserted"], "elapsed_s": elapsed}


def _flush(engine, rows, method="batch"):
    """Insert a batch of discovered files as pending, skipping paths already in
    the catalog (idempotent). Returns count newly inserted.

    method='batch' (default): bulk-load the batch into a #temp table in ONE
        executemany round-trip (fast_executemany collapses it), then a SINGLE
        set-based INSERT…SELECT…WHERE NOT EXISTS de-dupes against the catalog.
        ~2 round-trips per batch instead of one per file.
    method='row': the original per-row loop — kept so crawl_bench can measure
        the difference head-to-head.
    """
    if method == "row":
        return _flush_per_row(engine, rows)
    return _flush_batched(engine, rows)


def _flush_batched(engine, rows):
    payload = [{
        "iid": r[6], "path": r[0], "name": r[1], "ext": r[2],
        "size_kb": r[3], "mtime": r[4],
    } for r in rows]
    if not payload:
        return 0
    with engine.begin() as con:
        raw = con.connection
        try:
            cur = raw.cursor()
            try:
                cur.fast_executemany = True
            except Exception:
                pass
            cur.execute("""
                CREATE TABLE #crawl_stage (
                    INVENTORY_ID VARCHAR(64), FILE_PATH NVARCHAR(1000),
                    FILE_NAME NVARCHAR(400), FILE_EXT VARCHAR(40),
                    FILE_SIZE_KB FLOAT, FILE_MTIME VARCHAR(40));
            """)
            cur.executemany(
                "INSERT INTO #crawl_stage "
                "(INVENTORY_ID,FILE_PATH,FILE_NAME,FILE_EXT,FILE_SIZE_KB,FILE_MTIME) "
                "VALUES (?,?,?,?,?,?)",
                [(p["iid"], p["path"], p["name"], p["ext"],
                  p["size_kb"], p["mtime"]) for p in payload])
            # one set-based insert: only paths not already in the catalog
            cur.execute(f"""
                INSERT INTO {FQ} (INVENTORY_ID, FILE_PATH, FILE_NAME, FILE_EXT,
                                  FILE_SIZE_KB, FILE_MTIME, PROC_STATUS, SCAN_DATE)
                SELECT s.INVENTORY_ID, s.FILE_PATH, s.FILE_NAME, s.FILE_EXT,
                       s.FILE_SIZE_KB, s.FILE_MTIME, 'pending', GETUTCDATE()
                  FROM #crawl_stage s
                 WHERE NOT EXISTS (SELECT 1 FROM {FQ} t
                                   WHERE t.INVENTORY_ID = s.INVENTORY_ID);
            """)
            inserted = cur.rowcount
            cur.execute("DROP TABLE #crawl_stage;")
            cur.close()
            return inserted if inserted is not None and inserted >= 0 else 0
        except Exception:
            # if the wide insert fails on a column mismatch, fall back to the
            # minimal-identity batched insert (still set-based, no per-row loop)
            cur2 = raw.cursor()
            try:
                cur2.fast_executemany = True
            except Exception:
                pass
            cur2.execute("""
                CREATE TABLE #crawl_stage2 (
                    INVENTORY_ID VARCHAR(64), FILE_PATH NVARCHAR(1000),
                    FILE_NAME NVARCHAR(400), FILE_EXT VARCHAR(40));
            """)
            cur2.executemany(
                "INSERT INTO #crawl_stage2 "
                "(INVENTORY_ID,FILE_PATH,FILE_NAME,FILE_EXT) VALUES (?,?,?,?)",
                [(p["iid"], p["path"], p["name"], p["ext"]) for p in payload])
            cur2.execute(f"""
                INSERT INTO {FQ} (INVENTORY_ID, FILE_PATH, FILE_NAME,
                                  FILE_EXT, PROC_STATUS, SCAN_DATE)
                SELECT s.INVENTORY_ID, s.FILE_PATH, s.FILE_NAME, s.FILE_EXT,
                       'pending', GETUTCDATE()
                  FROM #crawl_stage2 s
                 WHERE NOT EXISTS (SELECT 1 FROM {FQ} t
                                   WHERE t.INVENTORY_ID = s.INVENTORY_ID);
            """)
            inserted = cur2.rowcount
            cur2.execute("DROP TABLE #crawl_stage2;")
            cur2.close()
            return inserted if inserted is not None and inserted >= 0 else 0


def _flush_per_row(engine, rows):
    """Original per-row insert (one round-trip per file) — kept for benchmarking
    against the batched path."""
    payload = [{
        "iid": r[6], "path": r[0], "name": r[1], "ext": r[2],
        "size_kb": r[3], "mtime": r[4],
    } for r in rows]
    sql = _t(f"""
        INSERT INTO {FQ} (INVENTORY_ID, FILE_PATH, FILE_NAME, FILE_EXT,
                          FILE_SIZE_KB, FILE_MTIME, PROC_STATUS, SCAN_DATE)
        SELECT :iid, :path, :name, :ext, :size_kb, :mtime, 'pending', GETUTCDATE()
        WHERE NOT EXISTS (
            SELECT 1 FROM {FQ} WHERE INVENTORY_ID = :iid)
    """)
    inserted = 0
    with engine.begin() as con:
        for p in payload:
            try:
                inserted += con.execute(sql, p).rowcount
            except Exception:
                pass
    return inserted


# ── CLI ──────────────────────────────────────────────────────────────────────
def main():
    import argparse, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dataview.file_catalog import worker_core as wc

    ap = argparse.ArgumentParser(
        description="Parallel directory crawl → fill the work queue")
    ap.add_argument("--root", required=True)
    ap.add_argument("--server", required=True)
    ap.add_argument("--database", required=True)
    ap.add_argument("--walkers", type=int, default=12)
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--exts", nargs="*", default=None,
                    help="extensions to crawl (default: the pipeline's set)")
    a = ap.parse_args()

    if a.exts:
        exts = [e if e.startswith(".") else "." + e for e in a.exts]
    else:
        # default to the pipeline's standard scan set
        try:
            from dataview.import_data.pipeline_run import default_exts
            exts = list(default_exts())
        except Exception:
            exts = [".las", ".dlis", ".lis", ".segy", ".sgy", ".seg", ".p190",
                    ".pdf", ".shp", ".geojson", ".xml", ".json", ".xlsx",
                    ".xls", ".docx", ".doc"]

    engine = wc.make_engine(a.server, a.database)
    crawl_to_queue(engine, a.root, exts, walkers=a.walkers, batch=a.batch)


if __name__ == "__main__":
    main()
