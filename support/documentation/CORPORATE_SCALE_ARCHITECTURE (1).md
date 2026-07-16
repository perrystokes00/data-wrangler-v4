# DataView v3 — Corporate-Scale Pipeline Architecture
### parallel crawl → batch → parallel process, for ~500,000 files on corporate SQL Server

This is the blueprint for the rewrite. The current Streamlit-coupled, mostly-
serial workflow does not scale to a corporate crawl. This design targets high
throughput AND crash-resumability as co-equal goals, with all per-file work
running in parallel across cores, writing back to the database concurrently.

Read this and veto/redirect any decision BEFORE we build. The decisions are the
product; the code follows from them.

---

## 1. The core model: database-backed work queue + stateless worker pool

The whole system is two things:

1. A **work queue that lives in the database** (a status column on a catalog
   row). The queue being in the DB — not in memory — is what gives
   resumability for free: a crash leaves done-rows done and pending-rows
   pending.
2. A **pool of stateless worker processes** that each: claim a batch of pending
   files → process them → mark them done → claim the next batch, until the
   queue is empty. The pool draining the queue across N cores is what gives
   throughput.

```
                    ┌─────────────────────────────────────────┐
   parallel  ─────► │  WORK_QUEUE (DB table)                   │
   crawl            │  file_id | path | ext | status | attempts │
   (fills queue)    │  pending → claimed → done / error         │
                    └─────────────────────────────────────────┘
                          ▲  claim batch        │ mark done/error
                          │                      ▼
        ┌──────────┬──────────┬──────────┬──────────┐
        │ worker 1 │ worker 2 │ worker 3 │  …  N     │   each: own DB conn,
        │ proc     │ proc     │ proc     │           │   stateless, picklable,
        └──────────┴──────────┴──────────┴──────────┘   streamlit-free
```

### Why a DB queue (not an in-memory list / multiprocessing.Queue)
- **Resumable**: 500K files might take hours. A crash at 400K must not restart
  from 0. DB state survives process death.
- **Observable**: you can query progress (`SELECT status, COUNT(*) … GROUP BY`)
  live, from another connection, mid-run.
- **Distributable later**: if one machine isn't enough, multiple machines can
  drain the same queue (claim is atomic — see §4). Not needed day one, but the
  model allows it for free.

---

## 2. The keystone: a streamlit-free per-file worker

**This is the single most important piece. Everything depends on it.**

Today the per-file logic (classify, extract, capture → cat_*) lives inside
`page_workbench.py`, which imports Streamlit. A worker PROCESS cannot use it
(Streamlit in a spawned worker is heavy/broken, and the logic isn't importable
without the UI). So the rewrite's first job is to extract a clean contract:

```python
# worker_core.py  — NO streamlit, fully importable, picklable
def process_file(conn, file_rec) -> FileResult:
    """Process ONE file end to end: classify → extract → write cat_* mirrors.
    conn: this worker's OWN pyodbc/sqlalchemy connection (NOT shared/pickled).
    file_rec: {file_id, path, ext, uwi?, inventory_id?}.
    Returns FileResult{status, rows_written, detail, error}. MUST be idempotent
    — re-processing the same file replaces its rows (capture() already does
    INVENTORY_ID-scoped replace), so a re-claimed file after a crash is safe.
    """
```

`process_file` dispatches by extension to format handlers that ALREADY exist as
streamlit-free modules — they just need to be called from here instead of from
page_workbench:
- PDF    → `pdf_survey_catalog` / `pdf_db_loader` (already streamlit-free)
- LAS    → lasio → cat_well/cat_well_log/cat_well_log_curve (logic exists in
           page_workbench's LAS block; MOVE it into worker_core unchanged —
           same real schema: LOG_ID, MNEMONIC, TOP_DEPTH, BASE_DEPTH, …)
- DLIS/LIS → dlisio / lis_catalog, INVENTORY grain (one row per curve), same
           cat_well_log + cat_well_log_curve schema as LAS (the gap today)
- SEGY   → header only → dv_seis_set (already done in extract)
- shapefile / office / json → existing catalog modules

**Design rule:** worker_core imports NOTHING from page_workbench. page_workbench
becomes a thin UI that, for single-file interactive use, calls the SAME
worker_core.process_file. One code path for both UI and batch — no drift.

### Threads vs processes (settled this session, don't relitigate)
- Pure-Python parsers (pdfplumber) are GIL-bound → need PROCESSES for real
  parallelism. Threads gave 0× speedup.
- C-extension parsers (lasio, dlisio, segyio) release the GIL → processes give
  true multi-core; threads would also help but processes are uniform.
- Directory walking is I/O-bound → threads DO help (GIL released on FS waits).
- Conclusion: **worker pool = PROCESSES** (uniform, CPU-safe). **crawl =
  THREADS** (I/O-bound). Each worker opens its OWN DB connection (engines can't
  pickle across processes; corporate SQL Server has no Express connection
  limits to worry about).

---

## 3. Parallel crawl (fills the queue)

Current `walk_share` is single-threaded `os.scandir` — confirmed. On a UNC
corporate share, per-directory latency makes this the FIRST bottleneck.

Replace with a thread-pool directory walker:
- A thread-safe queue of directories, seeded with the root.
- N worker threads: pop a dir, `scandir` it, emit file rows, push subdirs back.
- Files are written to WORK_QUEUE in batches (e.g. bulk insert every 5,000)
  so the queue starts filling before the crawl finishes — workers can begin
  processing while the crawl is still discovering files (crawl and process
  overlap).
- `file_fingerprint(path, size, mtime)` is metadata-only (fast) — keep. No
  content hashing during crawl.
- Dedup (same fingerprint) handled at insert (existing DEDUPE_SQL pattern).

Output: WORK_QUEUE populated with status='pending'. Drop-in: returns the same
file records the serial walk did, just faster and streaming.

---

## 4. The worker pool (drains the queue)

```python
# orchestrator (parent process)
def run_pool(server, database, workers=N, batch_size=200):
    pool = ProcessPoolExecutor(max_workers=workers)
    # each worker loops: claim → process → mark, until queue empty
    futures = [pool.submit(worker_loop, server, database, batch_size)
               for _ in range(workers)]
    # parent aggregates progress, handles shutdown
```

```python
# worker_loop (in each process) — opens its OWN connection once
def worker_loop(server, database, batch_size):
    conn = connect(server, database)          # this worker's own connection
    while True:
        batch = claim_batch(conn, batch_size) # atomic claim (see below)
        if not batch:
            break                              # queue drained
        for rec in batch:
            try:
                r = process_file(conn, rec)    # the keystone
                mark_done(conn, rec, r)
            except Exception as e:
                mark_error(conn, rec, e)       # status=error, attempts+1
        conn.commit()
```

### Atomic claim (the one tricky bit — gets concurrency right)
Two workers must never grab the same file. SQL Server does this cleanly with an
`UPDATE … OUTPUT` + locking hints so a claim is a single atomic statement:

```sql
;WITH cte AS (
   SELECT TOP (:batch) file_id
   FROM   WORK_QUEUE WITH (READPAST, UPDLOCK, ROWLOCK)
   WHERE  status = 'pending' AND attempts < :max_attempts
   ORDER  BY file_id
)
UPDATE cte SET status = 'claimed'
OUTPUT inserted.file_id, inserted.path, inserted.ext, …;
```
`READPAST` skips rows another worker already locked → no two workers collide,
no blocking. This is the standard SQL-Server work-queue pattern.

### Failure / resumability rules
- Crash mid-batch: claimed-but-not-done rows are reset to 'pending' on restart
  by a one-line sweep (`UPDATE … SET status='pending' WHERE status='claimed'`),
  OR claims carry a timestamp and a stale-claim sweep re-queues them. Either
  way, no file is lost.
- A file that errors `max_attempts` times → status='error', left for review,
  doesn't block the run.
- Idempotent process_file means a re-processed file replaces, never duplicates.

---

## 5. Writing back to the DB at scale

- Each worker writes its OWN rows with its OWN connection (concurrent writes —
  corporate SQL Server handles this; the cat_* mirrors are append/replace per
  INVENTORY_ID so workers don't contend on the same rows).
- Within a file, writes are already batched via `capture()` (executemany).
- Reference-data (dv_r_*) seeding happens ONCE up front (not per worker) — seed
  source/uom/etc. before the pool starts so no worker hits an FK hold.
- Promote stays a SEPARATE, post-crawl, set-based step (cat_* → dv_*), run once
  after the queue drains. It's already bulk/staged. Do NOT promote per-file.
- Binary curve data: INVENTORY grain only (one row per curve, like SEGY
  headers) — never per-sample. This is both a correctness and a scale rule.

---

## 6. Build order (testable pieces, each independently verifiable)

1. **worker_core.process_file** — the keystone. Extract per-file logic from
   page_workbench into a streamlit-free module. Test: process one file of each
   type via a bare connection, confirm identical cat_* rows to today. Wire
   page_workbench's interactive path to call it too (one code path).
2. **WORK_QUEUE schema + claim/mark functions** — the queue table and the
   atomic claim/done/error SQL. Test: simulate N workers claiming, confirm no
   double-claim, confirm crash-resume.
3. **worker pool orchestrator** — ProcessPoolExecutor of worker_loops. Test:
   run the existing 411-file test set through the pool, confirm same final
   cat_*/dv_* counts as today, then confirm a killed-mid-run restart finishes
   clean.
4. **parallel crawl** — thread-pool walker filling WORK_QUEUE, streaming. Test:
   synthetic deep tree, confirm same file set as serial walk, faster.
5. **integration + promote** — full run: parallel crawl → pool drains → promote
   once. Benchmark vs today on 411 files; then a synthetic 50K tree for scale.

Each step is shippable and reversible. Nothing requires a big-bang switchover —
the pool can run alongside the old workflow until it's proven.

---

## 6b. The UI's role — thin orchestrator + monitor (NOT a processor)

This is a delivered product; a customer must be able to start a 500K crawl,
watch it, see errors, and know when it's done. They will NOT do that from a
command line. So there IS a UI — but its role inverts from today.

**Today:** `page_workbench` DOES the work (parses files, holds pipeline state
in-process). That's what doesn't scale.

**New:** the UI is a thin **orchestrator + monitor**. It does NOT process files.
It:
- starts the parallel crawl and launches the worker pool as BACKGROUND
  processes (detached — they survive the UI closing),
- polls the queue for live progress:
  `SELECT status, COUNT(*) FROM GLOBAL_FILE_CATALOG GROUP BY status`
  → "342,108 / 500,000 done · 1,204 error · 9 workers active · ~2.1h left",
- surfaces the 'error' rows for review,
- triggers Promote (incl. vault copy) once the queue drains.

```
   UI (Streamlit)  --start-->  crawl + worker pool (background processes)
   thin monitor    <-poll----  GLOBAL_FILE_CATALOG (queue + status)
   (reads queue, shows               ^
    progress; NOT in            workers drain it
    the work path)
```

Because the queue lives in the DB, UI and workers are DECOUPLED: the UI can be
closed and reopened mid-run and just re-reads current progress. The heavy work
runs in robust background processes, not in the Streamlit session.

**Keystone reinforced:** the UI's interactive "process this one file" button
calls the SAME `worker_core.process_file` the background pool calls. One code
path, driven by either a human click or a worker loop -> no UI/batch drift.

## 6c. Vault copy folds into Promote (not the per-file workers)

The old workbench step "copy file to the digital vault" (raw -> \curated\) moves
into PROMOTE. Rationale:
- It's the "commit to the gold layer" action — same moment as cat_* -> dv_*.
- Keeping file I/O OUT of the 10 parsing workers avoids 10 procs saturating
  disk/network I/O while they should be parsing. Workers = CPU-bound metadata;
  Promote = the set-based DB lift + the vault file copies (can be its own
  parallel I/O pass if needed).
So: workers parse + write metadata (parallel, CPU). Promote (after queue drains)
does dv_* lift AND vault copies, once, as a controlled step.

## 7. Open decisions for Perry — ANSWERED

- **Workers**: 10 (matches 10 cores).
- **Batch size per claim**: 500.
- **Work queue**: extend GLOBAL_FILE_CATALOG with status columns (one source of
  truth) — STATUS, CLAIMED_AT, ATTEMPTS, WORKER_ID, ERROR_MSG.
- **max_attempts**: 3, then park as status='error'.
- **Promote**: separate step after the queue drains; INCLUDES the vault copy.
- **Connection per worker**: TBD by what capture() needs — if it requires a
  SQLAlchemy engine, each worker builds its own lightweight engine; if a raw
  pyodbc connection suffices, use that (lighter). Verify capture()'s signature
  before building worker_core.
- **UI**: thin orchestrator + monitor (see §6b), Streamlit, reads the queue,
  starts/monitors background pool, triggers promote. Not in the work path.

### Original open-decision notes (kept for context)

- **Worker count default**: cores? cores×2? (I/O wait suggests slightly over
  core count; benchmark in step 3.)
- **Batch size per claim**: 200? Tradeoff: bigger = fewer queue round-trips,
  smaller = finer resume granularity + better load balancing. Benchmark.
- **WORK_QUEUE: reuse GLOBAL_FILE_CATALOG (add status columns) or a new
  table?** Reusing keeps one source of truth; a new table keeps the queue
  concern separate. Lean: add status columns to the existing catalog — it's
  already the file registry.
- **max_attempts** before a file is parked as 'error': 3?
- **Promote**: still a manual/explicit step after crawl, or auto-run when queue
  drains?
- **Connection strategy**: pyodbc directly per worker, or SQLAlchemy engine
  per worker? (pyodbc is lighter for this; SQLAlchemy if the existing capture()
  needs it — check what capture() requires.)
```
