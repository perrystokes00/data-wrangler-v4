"""
modules/file_inventory.py  —  Data Wrangler Global File Inventory
==================================================================
Crawls one or more root paths, hashes files, detects duplicates,
and cross-references against the las_catalog / seis_catalog tables
to show cataloging progress.

Supports SQL Server, Oracle and Snowflake via dialect-aware DDL.
Schema: file_catalog
Table:  file_catalog.GLOBAL_FILE_CATALOG
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from dataview.core.file_identity import inventory_id as _make_id

import pandas as pd

# Single source of truth for the content fingerprint + duplicate grouping,
# shared with the CLI/pipeline scan so both produce identical FILE_HASH and
# dedupe the same way. Root is on sys.path under `streamlit run app_v3.py`;
# the modules.* fallback covers other import contexts.
try:
    from dataview.core.fingerprint import file_fingerprint, DEDUPE_SQL
except ImportError:
    from dataview.core.fingerprint import file_fingerprint, DEDUPE_SQL

# ── Constants ─────────────────────────────────────────────────────────────────

INVENTORY_SCHEMA = "file_catalog"
INVENTORY_TABLE  = "GLOBAL_FILE_CATALOG"

FILE_TYPE_GROUPS = {
    "Well Logs":   [".las", ".dlis", ".dis", ".lis"],
    "Seismic":     [".segy", ".sgy", ".seg", ".p190", ".p90", ".p1"],
    "Spatial":     [".shp", ".geojson", ".gdb", ".kml", ".kmz"],
    "Data":        [".csv", ".xlsx", ".xls"],
    "Documents":   [".pdf", ".docx", ".doc"],
}

ALL_EXTENSIONS = sorted({
    ext for exts in FILE_TYPE_GROUPS.values() for ext in exts
})

HASH_CHUNK_BYTES = 65536   # 64 KB


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _fast_hash(path: str) -> str:
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            h.update(f.read(HASH_CHUNK_BYTES))
    except Exception:
        pass
    return h.hexdigest().upper()


def _full_hash(path: str) -> str:
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(HASH_CHUNK_BYTES), b""):
                h.update(chunk)
    except Exception:
        pass
    return h.hexdigest().upper()


def _detect_dialect(engine) -> str:
    try:
        name = engine.dialect.name.lower()
        if "oracle" in name:
            return "oracle"
        if "snowflake" in name:
            return "snowflake"
    except Exception:
        pass
    return "sqlserver"


def _ext_to_group(ext: str) -> str:
    ext = ext.lower()
    for group, exts in FILE_TYPE_GROUPS.items():
        if ext in exts:
            return group
    return "Other"


# ── DDL ───────────────────────────────────────────────────────────────────────

def _ddl_create_schema(dialect: str) -> str | None:
    if dialect == "sqlserver":
        return (
            "IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'file_catalog') "
            "EXEC('CREATE SCHEMA [file_catalog]')"
        )
    return None


def _ddl_table_exists(dialect: str) -> str:
    if dialect == "oracle":
        return ("SELECT COUNT(*) FROM ALL_TABLES "
                "WHERE TABLE_NAME = 'GLOBAL_FILE_CATALOG'")
    if dialect == "snowflake":
        return ("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_NAME = 'GLOBAL_FILE_CATALOG'")
    return ("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = 'file_catalog' "
            "AND TABLE_NAME = 'GLOBAL_FILE_CATALOG'")


def _ddl_create_table(dialect: str) -> str:
    if dialect == "oracle":
        return """
            CREATE TABLE GLOBAL_FILE_CATALOG (
                INVENTORY_ID      VARCHAR2(40)    NOT NULL,
                FILE_PATH         VARCHAR2(1000)  NOT NULL,
                FILE_NAME         VARCHAR2(500)   NOT NULL,
                FILE_EXT          VARCHAR2(20),
                FILE_SIZE_KB      NUMBER(15,2),
                FILE_HASH         VARCHAR2(64),
                FILE_HASH_FULL    VARCHAR2(64),
                DUPLICATE_GROUP   VARCHAR2(64),
                MODIFIED_DATE     TIMESTAMP,
                SCAN_DATE         TIMESTAMP       NOT NULL,
                CATALOG_STATUS    VARCHAR2(20),
                CATALOG_TABLE     VARCHAR2(100),
                ROOT_PATH         VARCHAR2(500),
                FILE_TYPE_GROUP   VARCHAR2(50),
                ROW_CREATED_DATE  TIMESTAMP       NOT NULL,
                ROW_CHANGED_DATE  TIMESTAMP       NOT NULL,
                CONSTRAINT PK_GLOBAL_FILE_CATALOG PRIMARY KEY (INVENTORY_ID)
            )"""
    if dialect == "snowflake":
        return """
            CREATE TABLE GLOBAL_FILE_CATALOG (
                INVENTORY_ID      VARCHAR(40)     NOT NULL,
                FILE_PATH         VARCHAR(1000)   NOT NULL,
                FILE_NAME         VARCHAR(500)    NOT NULL,
                FILE_EXT          VARCHAR(20),
                FILE_SIZE_KB      NUMERIC(15,2),
                FILE_HASH         VARCHAR(64),
                FILE_HASH_FULL    VARCHAR(64),
                DUPLICATE_GROUP   VARCHAR(64),
                MODIFIED_DATE     TIMESTAMP_NTZ,
                SCAN_DATE         TIMESTAMP_NTZ   NOT NULL,
                CATALOG_STATUS    VARCHAR(20),
                CATALOG_TABLE     VARCHAR(100),
                ROOT_PATH         VARCHAR(500),
                FILE_TYPE_GROUP   VARCHAR(50),
                ROW_CREATED_DATE  TIMESTAMP_NTZ   NOT NULL,
                ROW_CHANGED_DATE  TIMESTAMP_NTZ   NOT NULL,
                PRIMARY KEY (INVENTORY_ID)
            )"""
    return """
        CREATE TABLE [file_catalog].[GLOBAL_FILE_CATALOG] (
            [INVENTORY_ID]      NVARCHAR(40)    NOT NULL,
            [FILE_PATH]         NVARCHAR(1000)  NOT NULL,
            [FILE_NAME]         NVARCHAR(500)   NOT NULL,
            [FILE_EXT]          NVARCHAR(20)    NULL,
            [FILE_SIZE_KB]      NUMERIC(15,2)   NULL,
            [FILE_HASH]         NVARCHAR(64)    NULL,
            [FILE_HASH_FULL]    NVARCHAR(64)    NULL,
            [DUPLICATE_GROUP]   NVARCHAR(64)    NULL,
            [MODIFIED_DATE]     DATETIME2       NULL,
            [SCAN_DATE]         DATETIME2       NOT NULL,
            [CATALOG_STATUS]    NVARCHAR(20)    NULL,
            [CATALOG_TABLE]     NVARCHAR(100)   NULL,
            [ROOT_PATH]         NVARCHAR(500)   NULL,
            [FILE_TYPE_GROUP]   NVARCHAR(50)    NULL,
            [ROW_CREATED_DATE]  DATETIME2       NOT NULL,
            [ROW_CHANGED_DATE]  DATETIME2       NOT NULL,
            CONSTRAINT [PK_GLOBAL_FILE_CATALOG] PRIMARY KEY ([INVENTORY_ID])
        )"""


def _ddl_indexes(dialect: str) -> list[str]:
    """
    Indexes on GLOBAL_FILE_CATALOG covering the main query patterns:
      - Browse/assign: LOWER(FILE_EXT), CATALOG_STATUS, ROOT_PATH, IS_DUPLICATE
      - Duplicate detection: FILE_HASH
      - Assignment joins: INVENTORY_ID (PK — already indexed)
      - Type grouping: FILE_TYPE_GROUP
    """
    if dialect == "sqlserver":
        def _ix(name, cols, where=""):
            w = f" WHERE {where}" if where else ""
            return (
                f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='{name}')"
                f" CREATE INDEX [{name}] ON [file_catalog].[GLOBAL_FILE_CATALOG] ({cols}){w}"
            )
        return [
            _ix("GFC_EXT_IDX",    "[FILE_EXT]"),
            _ix("GFC_STATUS_IDX", "[CATALOG_STATUS]"),
            _ix("GFC_ROOT_IDX",   "[ROOT_PATH]"),
            _ix("GFC_HASH_IDX",   "[FILE_HASH]", "[FILE_HASH] IS NOT NULL"),
            _ix("GFC_DUP_IDX",    "[DUPLICATE_GROUP]"),
            _ix("GFC_GROUP_IDX",  "[FILE_TYPE_GROUP]"),
            # Composite covering index for the main assign query
            _ix("GFC_ASSIGN_IDX", "[CATALOG_STATUS],[FILE_TYPE_GROUP],[FILE_EXT],[FILE_NAME],[INVENTORY_ID]"),
        ]
    elif dialect == "oracle":
        def _ix(name, cols):
            return (
                f"DECLARE BEGIN "
                f"EXECUTE IMMEDIATE 'CREATE INDEX {name} ON GLOBAL_FILE_CATALOG ({cols})'; "
                f"EXCEPTION WHEN OTHERS THEN NULL; END;"
            )
        return [
            _ix("GFC_EXT_IDX",    "FILE_EXT"),
            _ix("GFC_STATUS_IDX", "CATALOG_STATUS"),
            _ix("GFC_ROOT_IDX",   "ROOT_PATH"),
            _ix("GFC_HASH_IDX",   "FILE_HASH"),
            _ix("GFC_DUP_IDX",    "DUPLICATE_GROUP"),
            _ix("GFC_GROUP_IDX",  "FILE_TYPE_GROUP"),
            _ix("GFC_ASSIGN_IDX", "CATALOG_STATUS,FILE_TYPE_GROUP,FILE_EXT,FILE_NAME,INVENTORY_ID"),
        ]
    elif dialect == "snowflake":
        # Snowflake uses micro-partitioning — explicit indexes not supported.
        # Cluster keys on the most selective columns improve pruning.
        return [
            'ALTER TABLE "FILE_CATALOG"."GLOBAL_FILE_CATALOG" '
            'CLUSTER BY (CATALOG_STATUS, FILE_TYPE_GROUP, FILE_EXT)',
        ]
    return []


# ── Schema creation ───────────────────────────────────────────────────────────

def ensure_inventory_schema(engine, dialect=None) -> list[str]:  # dialect ignored — auto-detected
    """Create file_catalog schema and GLOBAL_FILE_CATALOG if not present."""
    from sqlalchemy import text
    dialect = _detect_dialect(engine)
    created = []

    with engine.begin() as con:
        schema_ddl = _ddl_create_schema(dialect)
        if schema_ddl:
            con.execute(text(schema_ddl))

        exists = con.execute(text(_ddl_table_exists(dialect))).scalar()
        if not exists:
            con.execute(text(_ddl_create_table(dialect)))
            created.append("GLOBAL_FILE_CATALOG")

        for idx_sql in _ddl_indexes(dialect):
            try:
                con.execute(text(idx_sql))
            except Exception:
                pass

    return created


# ── Catalog cross-reference ───────────────────────────────────────────────────

def _get_cataloged_paths(engine) -> dict[str, str]:
    """Return {normalised_upper_path: catalog_table} for all cataloged files."""
    from sqlalchemy import text
    dialect  = _detect_dialect(engine)
    cataloged: dict[str, str] = {}

    if dialect == "sqlserver":
        queries = [
            ("WL_FILE_CATALOG",
             "SELECT FULL_PATH FROM [las_catalog].[WL_FILE_CATALOG]"),
            ("SEIS_FILE_CATALOG",
             "SELECT FULL_PATH FROM [las_catalog].[SEIS_FILE_CATALOG]"),
        ]
    elif dialect == "oracle":
        queries = [
            ("WL_FILE_CATALOG",   "SELECT FULL_PATH FROM WL_FILE_CATALOG"),
            ("SEIS_FILE_CATALOG", "SELECT FULL_PATH FROM SEIS_FILE_CATALOG"),
        ]
    else:
        queries = [
            ("WL_FILE_CATALOG",   'SELECT FULL_PATH FROM "WL_FILE_CATALOG"'),
            ("SEIS_FILE_CATALOG", 'SELECT FULL_PATH FROM "SEIS_FILE_CATALOG"'),
        ]

    with engine.connect() as con:
        for tbl_name, sql in queries:
            try:
                for row in con.execute(text(sql)).fetchall():
                    if row[0]:
                        cataloged[str(row[0]).upper()] = tbl_name
            except Exception:
                pass
    return cataloged


# ── File scanning ─────────────────────────────────────────────────────────────

def _scan_file(file_path: Path, root_path: str,
               cataloged: dict[str, str],
               full_hash: bool,
               hash_status: dict | None = None) -> dict:
    now = _now_str()
    size_bytes = None
    mtime = 0.0
    try:
        stat    = file_path.stat()
        size_bytes = stat.st_size
        mtime   = stat.st_mtime
        size_kb = round(stat.st_size / 1024, 2)
        mod_dt  = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        size_kb = None
        mod_dt  = None

    # CANONICAL PATH, ALWAYS. Windows collapses repeated separators for
    # filesystem access, so a root typed or pasted as
    #     C:\\Users\\perry\\docs
    # opens exactly the same folder as the single-separator form — and
    # the scan works, and every file is found. But str(Path(...)) keeps
    # the spelling it was given, INVENTORY_ID is SHA1 of that string, and
    # the same file therefore lands in the catalog TWICE with different
    # ids. It happened here: 1,050 rows for 525 PDFs, one set from a run
    # whose root had doubled separators and one from a later run that
    # doubled them again. Nothing errored, because nothing was wrong as
    # far as the operating system was concerned.
    #
    # normpath collapses the repeats. Done HERE rather than only at the
    # UI so every caller — scanner, worker pool, CLI — mints the same id
    # for the same file however the root reached it.
    path_str  = os.path.normpath(str(file_path))
    inv_id    = _make_id(path_str)
    ext       = file_path.suffix.lower()
    fast_h    = None
    full_h    = None

    # Content fingerprint — the shared function both scanners use, so the same
    # file gets the same FILE_HASH no matter which path scanned it. This is what
    # makes the server-side duplicate grouping below actually fire (previously
    # FILE_HASH was left NULL, so no duplicates were ever detected).
    if size_bytes is not None:
        try:
            fast_h = file_fingerprint(path_str, size_bytes, mtime)
            if full_hash:
                full_h = _full_hash(path_str)   # exact whole-file digest (opt-in)
        except Exception:
            pass

    cat_status = "UNCATALOGED"
    cat_table  = None
    if path_str.upper() in cataloged:
        # Path matches a known cataloged file
        cat_status = "CATALOGED"
        cat_table  = cataloged[path_str.upper()]


    return {
        "INVENTORY_ID":     inv_id,
        "FILE_PATH":        path_str,
        "FILE_NAME":        file_path.name,
        "FILE_EXT":         ext,
        "FILE_SIZE_KB":     size_kb,
        "FILE_HASH":        fast_h,
        "FILE_HASH_FULL":   full_h,
        "DUPLICATE_GROUP":  None,
        "MODIFIED_DATE":    mod_dt,
        "SCAN_DATE":        now,
        "CATALOG_STATUS":   cat_status,
        "CATALOG_TABLE":    cat_table,
        "ROOT_PATH":        root_path,
        "FILE_TYPE_GROUP":  _ext_to_group(ext),
        "ROW_CREATED_DATE": now,
        "ROW_CHANGED_DATE": now,
    }


# ── Main crawl entry point ────────────────────────────────────────────────────

def crawl_and_inventory(
    engine,
    root_paths: list[str],
    extensions: list[str],
    full_hash: bool = False,
    max_workers: int = None,
    replace_root: bool = True,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """
    Crawl root_paths for files matching extensions.
    Collects metadata only — no header reading.
    Duplicate detection runs server-side after bulk load.

    Performance design:
    - os.scandir() instead of os.walk() — DirEntry caches stat() for free,
      eliminating one syscall per file. Critical on network shares.
    - Lock-free counters via atomic integers (threading.local accumulate
      into per-thread lists, merged once at the end).
    - Unbounded queue — walker never blocks waiting for workers.
    - Single shared lock only for the per-thread result merge at the end.
    - Progress poll every 0.5s driven by atomic counter reads.

    Returns dict: files_found, files_inserted, duplicates, errors
    """
    from sqlalchemy import text
    import threading, queue, time
    from itertools import chain

    if max_workers is None:
        max_workers = max(min((os.cpu_count() or 4) - 1, 8), 2)

    ext_set = {e.lower() for e in extensions}

    cataloged = _get_cataloged_paths(engine)
    dialect   = _detect_dialect(engine)
    gfc       = ("[file_catalog].[GLOBAL_FILE_CATALOG]" if dialect == "sqlserver"
                 else "GLOBAL_FILE_CATALOG" if dialect == "oracle"
                 else '"FILE_CATALOG"."GLOBAL_FILE_CATALOG"')

    # ── Pre-scan: existing catalog status ─────────────────────────────────────
    path_status: dict[str, str] = {}
    try:
        with engine.connect() as con:
            for inv_id, status in con.execute(text(
                f"SELECT INVENTORY_ID, CATALOG_STATUS FROM {gfc}"
            )).fetchall():
                path_status[inv_id] = status
    except Exception:
        pass

    if replace_root:
        with engine.begin() as con:
            for root in root_paths:
                con.execute(text(
                    f"DELETE FROM {gfc} WHERE ROOT_PATH = :r"
                ), {"r": root})

    # ── Shared state — lock-free where possible ───────────────────────────────
    # Use lists-of-one as cheap mutable integers — no lock needed for reads
    # (GIL guarantees atomic int reads on CPython).
    _found   = [0]   # total files matching ext — incremented by walker
    _done    = [0]   # files fully processed — incremented by workers
    _folders = [0]   # folders visited
    _last    = ["Scanning…"]   # last filename seen (display only)
    _walk_done = threading.Event()

    # Per-worker result buckets — no shared list, no lock during scan.
    # Each worker appends only to its own bucket; merge happens once at end.
    _worker_results: list[list[dict]] = [[] for _ in range(max_workers)]
    _worker_errors:  list[list[str]]  = [[] for _ in range(max_workers)]

    _file_queue: queue.Queue = queue.Queue()   # unbounded — walker never blocks

    _SKIP_DIRS = {
        "$recycle.bin", "recycler", "$recycled",
        "system volume information", ".trash", ".trashes",
        "lost+found", "thumbs.db",
    }

    # ── Walker: os.scandir() recursive, stat() cached on DirEntry ─────────────
    def _walker():
        """Single thread. Uses os.scandir() so stat is free on most OSes."""
        dir_stack = []
        for root in root_paths:
            if os.path.isdir(root):
                dir_stack.append((root, root))

        while dir_stack:
            dirpath, root = dir_stack.pop()
            _folders[0] += 1
            try:
                with os.scandir(dirpath) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name.lower() not in _SKIP_DIRS:
                                    dir_stack.append((entry.path, root))
                            elif entry.is_file(follow_symlinks=False):
                                ext = os.path.splitext(entry.name)[1].lower()
                                if ext in ext_set:
                                    _found[0] += 1
                                    # Pass DirEntry so worker can use cached stat
                                    _file_queue.put((entry.path, entry.name,
                                                     ext, root))
                        except OSError:
                            pass
            except PermissionError:
                pass

        _walk_done.set()
        for _ in range(max_workers):
            _file_queue.put(None)   # poison pills

    # ── Workers: consume queue, build per-thread result list ──────────────────
    def _worker(worker_id: int):
        results = _worker_results[worker_id]
        errors  = _worker_errors[worker_id]
        now     = _now_str()

        while True:
            item = _file_queue.get()
            if item is None:
                break
            file_path, fname, ext, root = item
            _size_bytes = None
            _mtime = 0.0
            try:
                # stat() — on Windows/Linux, scandir cached inode info
                # but we need size+mtime so call stat explicitly.
                # Still faster than Path.stat() because we pass the string path.
                st      = os.stat(file_path)
                _size_bytes = st.st_size
                _mtime  = st.st_mtime
                size_kb = round(st.st_size / 1024, 2)
                mod_dt  = datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                size_kb = None
                mod_dt  = None

            inv_id     = _make_id(file_path)
            cat_status = "CATALOGED" if file_path.upper() in cataloged else "UNCATALOGED"
            cat_table  = cataloged.get(file_path.upper())

            # Content fingerprint (shared with the pipeline scan) — without this
            # FILE_HASH stayed NULL and the server-side duplicate grouping below
            # never matched anything. full_hash adds an exact whole-file digest.
            _fast_h = None
            _full_h = None
            if _size_bytes is not None:
                try:
                    _fast_h = file_fingerprint(file_path, _size_bytes, _mtime)
                    if full_hash:
                        _full_h = _full_hash(file_path)
                except Exception:
                    pass

            results.append({
                "INVENTORY_ID":     inv_id,
                "FILE_PATH":        file_path,
                "FILE_NAME":        fname,
                "FILE_EXT":         ext,
                "FILE_SIZE_KB":     size_kb,
                "FILE_HASH":        _fast_h,
                "FILE_HASH_FULL":   _full_h,
                "DUPLICATE_GROUP":  None,
                "MODIFIED_DATE":    mod_dt,
                "SCAN_DATE":        now,
                "CATALOG_STATUS":   cat_status,
                "CATALOG_TABLE":    cat_table or "",
                "ROOT_PATH":        root,
                "FILE_TYPE_GROUP":  _ext_to_group(ext),
                "ROW_CREATED_DATE": now,
                "ROW_CHANGED_DATE": now,
            })
            _done[0] += 1
            _last[0]  = fname

    # ── Launch threads ─────────────────────────────────────────────────────────
    walker_thread = threading.Thread(target=_walker, daemon=True)
    walker_thread.start()

    worker_threads = [
        threading.Thread(target=_worker, args=(i,), daemon=True)
        for i in range(max_workers)
    ]
    for w in worker_threads:
        w.start()

    # ── Progress poll — main thread only ──────────────────────────────────────
    while True:
        found     = _found[0]
        done      = _done[0]
        folders   = _folders[0]
        name      = _last[0]
        walk_done = _walk_done.is_set()

        if progress_callback:
            if found == 0:
                progress_callback(0, 0, f"Searching… {folders:,} folder(s) scanned")
            else:
                progress_callback(done, found, f"{folders:,} folders · {name}")

        if walk_done and done >= found:
            break

        time.sleep(0.5)

    walker_thread.join()
    for w in worker_threads:
        w.join()

    # ── Merge per-thread results — single pass, no repeated locking ───────────
    total   = _found[0]
    records = list(chain.from_iterable(_worker_results))
    errors  = list(chain.from_iterable(_worker_errors))

    good = records   # workers never append None

    # ── UPSERT — preserve catalog status for previously cataloged files ──────
    from sqlalchemy import text as _text
    for rec in good:
        existing = path_status.get(rec["INVENTORY_ID"])
        if existing in ("CATALOGED", "SKIPPED"):
            rec["CATALOG_STATUS"] = existing
        for col in ("FILE_PATH","FILE_NAME","FILE_EXT","ROOT_PATH",
                    "FILE_TYPE_GROUP","CATALOG_TABLE"):
            if rec.get(col) is None:
                rec[col] = ""

    inserted = 0

    if not good:
        pass

    elif dialect == "sqlserver":
        # ── Write to local CSV then BULK INSERT — fastest possible ───────────
        import csv, uuid
        COLS = ["INVENTORY_ID","FILE_PATH","FILE_NAME","FILE_EXT",
                "FILE_TYPE_GROUP","FILE_SIZE_KB","FILE_HASH",
                "DUPLICATE_GROUP","CATALOG_STATUS","CATALOG_TABLE",
                "ROOT_PATH","SCAN_DATE","ROW_CREATED_DATE","ROW_CHANGED_DATE"]

        # SCRATCH ROOT, NOT C:\Bulk -- one place for every staging file so a
        # distribution can redirect them together. C:\Bulk is the VAULT root
        # as well, and mixing a data store with throwaway CSVs is how the two
        # come to share a lifetime.
        from dataview.core.config import scratch_dir
        csv_path = os.path.join(scratch_dir("bulk"),
                                f"inv_stage_{uuid.uuid4().hex[:8]}.csv")

        # bulk_csv_writer carries NO escapechar — see path_identity for why
        # that is the whole fix. The escaped form doubled every separator in a
        # Windows path and BULK INSERT stored it verbatim.
        from dataview.core.path_identity import bulk_csv_writer, bulk_field

        try:
            # Write all records to local CSV
            _sanitised = 0
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = bulk_csv_writer(f)
                for rec in good:
                    row = []
                    for c in COLS:
                        val, changed = bulk_field(rec.get(c, ""))
                        row.append(val)
                        if changed:
                            _sanitised += 1
                    writer.writerow(row)
            if _sanitised:
                errors.append(
                    f"{_sanitised} field(s) contained a tab, quote or newline "
                    f"and were rewritten to load — the stored value differs "
                    f"from the value on disk")

            with engine.begin() as con:
                # Create staging table
                con.execute(_text("""
                    IF OBJECT_ID('file_catalog.inv_bulk_stage','U') IS NOT NULL
                        DROP TABLE file_catalog.inv_bulk_stage;
                    CREATE TABLE file_catalog.inv_bulk_stage (
                        INVENTORY_ID     NVARCHAR(40),
                        FILE_PATH        NVARCHAR(900),
                        FILE_NAME        NVARCHAR(260),
                        FILE_EXT         NVARCHAR(20),
                        FILE_TYPE_GROUP  NVARCHAR(50),
                        FILE_SIZE_KB     NVARCHAR(30),
                        FILE_HASH        NVARCHAR(40),
                        DUPLICATE_GROUP  NVARCHAR(64),
                        CATALOG_STATUS   NVARCHAR(20),
                        CATALOG_TABLE    NVARCHAR(100),
                        ROOT_PATH        NVARCHAR(900),
                        SCAN_DATE        NVARCHAR(30),
                        ROW_CREATED_DATE NVARCHAR(30),
                        ROW_CHANGED_DATE NVARCHAR(30)
                    );
                """))

                # BULK INSERT from local CSV
                con.execute(_text(f"""
                    BULK INSERT file_catalog.inv_bulk_stage
                    FROM '{csv_path}'
                    WITH (
                        FIELDTERMINATOR = '\t',
                        ROWTERMINATOR   = '0x0D0A',
                        CODEPAGE        = '65001',
                        FIRSTROW        = 1,
                        TABLOCK
                    );
                """))

                # Single MERGE from stage → target
                con.execute(_text("""
                    MERGE [file_catalog].[GLOBAL_FILE_CATALOG] AS tgt
                    USING file_catalog.inv_bulk_stage AS src
                    ON tgt.INVENTORY_ID = src.INVENTORY_ID
                    WHEN MATCHED THEN UPDATE SET
                        FILE_SIZE_KB    = TRY_CAST(src.FILE_SIZE_KB AS DECIMAL(15,2)),
                        FILE_HASH       = src.FILE_HASH,
                        DUPLICATE_GROUP = src.DUPLICATE_GROUP,
                        CATALOG_STATUS  = src.CATALOG_STATUS,
                        SCAN_DATE       = TRY_CAST(src.SCAN_DATE AS DATETIME2),
                        ROW_CHANGED_DATE= TRY_CAST(src.ROW_CHANGED_DATE AS DATETIME2)
                    WHEN NOT MATCHED THEN INSERT (
                        INVENTORY_ID,FILE_PATH,FILE_NAME,FILE_EXT,
                        FILE_TYPE_GROUP,FILE_SIZE_KB,FILE_HASH,
                        DUPLICATE_GROUP,CATALOG_STATUS,CATALOG_TABLE,
                        ROOT_PATH,SCAN_DATE,ROW_CREATED_DATE,ROW_CHANGED_DATE
                    ) VALUES (
                        src.INVENTORY_ID,src.FILE_PATH,src.FILE_NAME,src.FILE_EXT,
                        src.FILE_TYPE_GROUP,
                        TRY_CAST(src.FILE_SIZE_KB AS DECIMAL(15,2)),
                        src.FILE_HASH,src.DUPLICATE_GROUP,src.CATALOG_STATUS,
                        src.CATALOG_TABLE,src.ROOT_PATH,
                        TRY_CAST(src.SCAN_DATE AS DATETIME2),
                        TRY_CAST(src.ROW_CREATED_DATE AS DATETIME2),
                        TRY_CAST(src.ROW_CHANGED_DATE AS DATETIME2)
                    );
                """))

                # Server-side duplicate detection (shared rule): keep one
                # canonical per FILE_HASH (DUPLICATE_GROUP NULL); flag the
                # redundant copies. Idempotent — recomputed from the whole table.
                con.execute(_text(DEDUPE_SQL))

                # Drop staging table
                con.execute(_text(
                    "DROP TABLE IF EXISTS file_catalog.inv_bulk_stage"
                ))

                inserted = len(good)

        except Exception as e:
            errors.append(f"Bulk load error: {e}")
        finally:
            # Always clean up the CSV
            try:
                if os.path.exists(csv_path):
                    os.remove(csv_path)
            except Exception:
                pass
    else:
        # Oracle / Snowflake: per-row MERGE
        for rec in good:
            try:
                with engine.begin() as con:
                    if dialect == "oracle":
                        con.execute(_text("""
                            MERGE INTO GLOBAL_FILE_CATALOG tgt
                            USING (SELECT :INVENTORY_ID AS INVENTORY_ID FROM DUAL) src
                            ON (tgt.INVENTORY_ID = src.INVENTORY_ID)
                            WHEN MATCHED THEN UPDATE SET
                                FILE_SIZE_KB=:FILE_SIZE_KB,FILE_HASH=:FILE_HASH,
                                DUPLICATE_GROUP=:DUPLICATE_GROUP,
                                CATALOG_STATUS=:CATALOG_STATUS,
                                SCAN_DATE=:SCAN_DATE,
                                ROW_CHANGED_DATE=:ROW_CHANGED_DATE
                            WHEN NOT MATCHED THEN INSERT (
                                INVENTORY_ID,FILE_PATH,FILE_NAME,FILE_EXT,
                                FILE_TYPE_GROUP,FILE_SIZE_KB,FILE_HASH,
                                DUPLICATE_GROUP,CATALOG_STATUS,CATALOG_TABLE,
                                ROOT_PATH,SCAN_DATE,ROW_CREATED_DATE,ROW_CHANGED_DATE
                            ) VALUES (
                                :INVENTORY_ID,:FILE_PATH,:FILE_NAME,:FILE_EXT,
                                :FILE_TYPE_GROUP,:FILE_SIZE_KB,:FILE_HASH,
                                :DUPLICATE_GROUP,:CATALOG_STATUS,:CATALOG_TABLE,
                                :ROOT_PATH,:SCAN_DATE,:ROW_CREATED_DATE,
                                :ROW_CHANGED_DATE
                            )
                        """), rec)
                    else:
                        con.execute(_text("""
                            MERGE INTO "FILE_CATALOG"."GLOBAL_FILE_CATALOG" tgt
                            USING (SELECT :INVENTORY_ID AS INVENTORY_ID) src
                            ON tgt."INVENTORY_ID" = src.INVENTORY_ID
                            WHEN MATCHED THEN UPDATE SET
                                "FILE_SIZE_KB"=:FILE_SIZE_KB,
                                "FILE_HASH"=:FILE_HASH,
                                "DUPLICATE_GROUP"=:DUPLICATE_GROUP,
                                "CATALOG_STATUS"=:CATALOG_STATUS,
                                "SCAN_DATE"=:SCAN_DATE,
                                "ROW_CHANGED_DATE"=:ROW_CHANGED_DATE
                            WHEN NOT MATCHED THEN INSERT (
                                "INVENTORY_ID","FILE_PATH","FILE_NAME","FILE_EXT",
                                "FILE_TYPE_GROUP","FILE_SIZE_KB","FILE_HASH",
                                "DUPLICATE_GROUP","CATALOG_STATUS","CATALOG_TABLE",
                                "ROOT_PATH","SCAN_DATE","ROW_CREATED_DATE",
                                "ROW_CHANGED_DATE"
                            ) VALUES (
                                :INVENTORY_ID,:FILE_PATH,:FILE_NAME,:FILE_EXT,
                                :FILE_TYPE_GROUP,:FILE_SIZE_KB,:FILE_HASH,
                                :DUPLICATE_GROUP,:CATALOG_STATUS,:CATALOG_TABLE,
                                :ROOT_PATH,:SCAN_DATE,:ROW_CREATED_DATE,
                                :ROW_CHANGED_DATE
                            )
                        """), rec)
                    inserted += 1
            except Exception as e:
                errors.append(f"{rec.get('FILE_NAME','?')}: {e}")

        # Count duplicates from DB
    dup_count = 0
    try:
            gfc = (f"[file_catalog].[GLOBAL_FILE_CATALOG]" if dialect == "sqlserver"
                   else "GLOBAL_FILE_CATALOG" if dialect == "oracle"
                   else '"FILE_CATALOG"."GLOBAL_FILE_CATALOG"')
            with engine.connect() as con:
                row = con.execute(_text(
                    f"SELECT COUNT(*) FROM {gfc} "
                    f"WHERE DUPLICATE_GROUP IS NOT NULL"
                )).fetchone()
                dup_count = row[0] if row else 0
    except Exception:
        pass

    return {
        "files_found":    total,
        "files_inserted": inserted,
        "duplicates":     dup_count,
        "errors":         errors,
    }


# ── Summary queries ───────────────────────────────────────────────────────────

def get_inventory_summary(engine) -> dict:
    from sqlalchemy import text
    dialect = _detect_dialect(engine)

    if dialect == "sqlserver":
        sql = """
            SELECT COUNT(*), SUM(FILE_SIZE_KB)/1024.0,
                   SUM(CASE WHEN CATALOG_STATUS='CATALOGED'   THEN 1 ELSE 0 END),
                   SUM(CASE WHEN CATALOG_STATUS='UNCATALOGED' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN DUPLICATE_GROUP IS NOT NULL  THEN 1 ELSE 0 END),
                   COUNT(DISTINCT ROOT_PATH)
            FROM [file_catalog].[GLOBAL_FILE_CATALOG]"""
    elif dialect == "oracle":
        sql = """
            SELECT COUNT(*), SUM(FILE_SIZE_KB)/1024,
                   SUM(CASE WHEN CATALOG_STATUS='CATALOGED'   THEN 1 ELSE 0 END),
                   SUM(CASE WHEN CATALOG_STATUS='UNCATALOGED' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN DUPLICATE_GROUP IS NOT NULL  THEN 1 ELSE 0 END),
                   COUNT(DISTINCT ROOT_PATH)
            FROM GLOBAL_FILE_CATALOG"""
    else:
        sql = """
            SELECT COUNT(*), SUM(FILE_SIZE_KB)/1024,
                   SUM(CASE WHEN CATALOG_STATUS='CATALOGED'   THEN 1 ELSE 0 END),
                   SUM(CASE WHEN CATALOG_STATUS='UNCATALOGED' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN DUPLICATE_GROUP IS NOT NULL  THEN 1 ELSE 0 END),
                   COUNT(DISTINCT ROOT_PATH)
            FROM "GLOBAL_FILE_CATALOG" """

    try:
        with engine.connect() as con:
            r = con.execute(text(sql)).fetchone()
        return {
            "total_files":   r[0] or 0,
            "total_size_mb": round(float(r[1] or 0), 1),
            "cataloged":     r[2] or 0,
            "uncataloged":   r[3] or 0,
            "duplicates":    r[4] or 0,
            "root_count":    r[5] or 0,
        }
    except Exception:
        return {"total_files": 0, "total_size_mb": 0, "cataloged": 0,
                "uncataloged": 0, "duplicates": 0, "root_count": 0}


def get_inventory_by_type(engine) -> pd.DataFrame:
    from sqlalchemy import text
    dialect = _detect_dialect(engine)

    if dialect == "sqlserver":
        sql = """
            SELECT FILE_TYPE_GROUP, FILE_EXT, CATALOG_STATUS,
                   COUNT(*) AS file_count, SUM(FILE_SIZE_KB)/1024.0 AS size_mb
            FROM [file_catalog].[GLOBAL_FILE_CATALOG]
            GROUP BY FILE_TYPE_GROUP, FILE_EXT, CATALOG_STATUS
            ORDER BY FILE_TYPE_GROUP, FILE_EXT"""
    elif dialect == "oracle":
        sql = """
            SELECT FILE_TYPE_GROUP, FILE_EXT, CATALOG_STATUS,
                   COUNT(*) AS file_count, SUM(FILE_SIZE_KB)/1024 AS size_mb
            FROM GLOBAL_FILE_CATALOG
            GROUP BY FILE_TYPE_GROUP, FILE_EXT, CATALOG_STATUS
            ORDER BY FILE_TYPE_GROUP, FILE_EXT"""
    else:
        sql = """
            SELECT FILE_TYPE_GROUP, FILE_EXT, CATALOG_STATUS,
                   COUNT(*) AS file_count, SUM(FILE_SIZE_KB)/1024 AS size_mb
            FROM "GLOBAL_FILE_CATALOG"
            GROUP BY FILE_TYPE_GROUP, FILE_EXT, CATALOG_STATUS
            ORDER BY FILE_TYPE_GROUP, FILE_EXT"""

    try:
        with engine.connect() as con:
            rows = con.execute(text(sql)).fetchall()
        return pd.DataFrame(rows, columns=[
            "FILE_TYPE_GROUP", "FILE_EXT", "CATALOG_STATUS",
            "file_count", "size_mb"
        ])
    except Exception:
        return pd.DataFrame()


def get_duplicates(engine) -> pd.DataFrame:
    from sqlalchemy import text
    dialect = _detect_dialect(engine)

    if dialect == "sqlserver":
        sql = """
            SELECT DUPLICATE_GROUP, FILE_NAME, FILE_PATH,
                   FILE_SIZE_KB, FILE_TYPE_GROUP, CATALOG_STATUS
            FROM [file_catalog].[GLOBAL_FILE_CATALOG]
            WHERE DUPLICATE_GROUP IS NOT NULL
            ORDER BY DUPLICATE_GROUP, FILE_PATH"""
    elif dialect == "oracle":
        sql = """
            SELECT DUPLICATE_GROUP, FILE_NAME, FILE_PATH,
                   FILE_SIZE_KB, FILE_TYPE_GROUP, CATALOG_STATUS
            FROM GLOBAL_FILE_CATALOG
            WHERE DUPLICATE_GROUP IS NOT NULL
            ORDER BY DUPLICATE_GROUP, FILE_PATH"""
    else:
        sql = """
            SELECT DUPLICATE_GROUP, FILE_NAME, FILE_PATH,
                   FILE_SIZE_KB, FILE_TYPE_GROUP, CATALOG_STATUS
            FROM "GLOBAL_FILE_CATALOG"
            WHERE DUPLICATE_GROUP IS NOT NULL
            ORDER BY DUPLICATE_GROUP, FILE_PATH"""

    try:
        with engine.connect() as con:
            rows = con.execute(text(sql)).fetchall()
        return pd.DataFrame(rows, columns=[
            "DUPLICATE_GROUP", "FILE_NAME", "FILE_PATH",
            "FILE_SIZE_KB", "FILE_TYPE_GROUP", "CATALOG_STATUS"
        ])
    except Exception:
        return pd.DataFrame()


# ── Background header extraction + scoring ────────────────────────────────────

import threading

_bg_thread: threading.Thread | None = None
_bg_status: dict = {"running": False, "scored": 0, "total": 0,
                    "current": "", "errors": 0, "done": False}


def get_bg_status() -> dict:
    """Return current background scoring status."""
    return dict(_bg_status)


def start_background_scoring(engine_factory, dialect: str,
                              ext_filter: list = None,
                              limit: int = 2000) -> bool:
    """
    Start background thread to extract headers and score inventory files.
    engine_factory: callable that returns a fresh SQLAlchemy engine.
    Returns False if already running.
    """
    global _bg_thread, _bg_status

    if _bg_status.get("running"):
        return False

    _bg_status = {"running": True, "scored": 0, "total": 0,
                  "current": "", "errors": 0, "done": False}

    if ext_filter is None:
        ext_filter = [".las", ".dlis", ".dlf", ".lis", ".segy", ".sgy"]

    def _worker():
        global _bg_status
        try:
            engine = engine_factory()
            from dataview.file_catalog.catalog_rules import (
                extract_file_fields, score_file, write_score)
            from sqlalchemy import text

            exts = ",".join(f"\'{e}\'" for e in ext_filter)
            with engine.connect() as con:
                rows = con.execute(text(f"""
                    SELECT TOP {limit} INVENTORY_ID, FILE_PATH, FILE_EXT
                    FROM file_catalog.GLOBAL_FILE_CATALOG
                    WHERE FILE_EXT IN ({exts})
                    AND (HEADER_EXTRACTED IS NULL OR HEADER_EXTRACTED = 'N')
                    ORDER BY SCAN_DATE DESC
                """)).fetchall()

            _bg_status["total"] = len(rows)

            for inv_id, file_path, ext in rows:
                if not _bg_status["running"]:
                    break
                _bg_status["current"] = file_path
                try:
                    fields = extract_file_fields(file_path)
                    scored = score_file(fields, engine)
                    write_score(engine, inv_id, scored, fields)
                    _bg_status["scored"] += 1
                except Exception:
                    _bg_status["errors"] += 1

        except Exception as e:
            _bg_status["errors"] += 1
        finally:
            _bg_status["running"] = False
            _bg_status["done"]    = True

    _bg_thread = threading.Thread(target=_worker, daemon=True)
    _bg_thread.start()
    return True


def stop_background_scoring():
    """Signal background thread to stop."""
    _bg_status["running"] = False
