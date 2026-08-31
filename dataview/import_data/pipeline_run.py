#!/usr/bin/env python3
r"""
pipeline_run.py
===============
One-call, point-and-walk-away pipeline over a file share:

    scan ─► extract ─► triage ─► vault(plan|apply) ─► report

Each stage reuses the existing code (no reimplementation):
  scan     mirrors page_workbench's stage->GLOBAL_FILE_CATALOG MERGE, plus
           incremental size-change detection (new + changed files become
           pending; unchanged files are left alone -> resumable / re-runnable)
  extract  reuses extract_core._extract_fields + _write_enrichment_on
  triage   triage_inventory.run_all_engine  (sets VALUE_TIER + readiness)
  vault    vault_organizer routing; DRY-RUN by default, places bytes only
           with vault_apply=True
  report   rolls up the catalog, writes a PIPELINE_RUN row and a markdown
           report stating what was extracted, where it went, and what needs
           review.

Safe-by-default: scan/extract/triage only write metadata. The vault step is
dry-run unless asked to place files. Promotion into dv_* tables is intentionally
NOT done here — that heavier load stays an explicit, separate action.

Headless usage (schedulable as a nightly task):

    python pipeline_run.py --root \\share\geo\incoming
    python pipeline_run.py --root D:\data --vault-root D:\Vault --vault-apply --vault-mode hardlink

From the app (reuses the page's engine), with a live log callback:

    from dataview.import_data import pipeline_run
    summary = pipeline_run.run_pipeline(engine, root, log=st.write)
"""

import argparse
import os
import csv
import hashlib
import tempfile
import time
import uuid
from datetime import datetime, timezone


# ── pure helpers (no DB / no streamlit — unit-testable) ───────────────────────
# ONE IDENTITY, ONE FUNCTION. This used to mint its own id —
# sha1(path.upper(), utf-8) — while file_identity.inventory_id used
# sha1(canonical_path(path), utf-16-le). Both produce forty hex characters,
# both look right in the table, and they never join. The difference is not
# academic: the local version skipped normpath, so `C:\\a\\b` and `C:\a\b`
# hashed differently and the SAME FILE was catalogued twice. Measured 16 Aug
# 2026 on DataView_Demo: 2,094 of 3,876 rows (54%) carried a doubled-separator
# path, 1,366 of them duplicating a row that already existed.
#
# Delegating rather than deleting: `inv_id` is the name _stage_scan and several
# tools already call, and it is re-exported below for them.
from dataview.core.file_identity import inventory_id as inv_id   # noqa: E402,F401


# Single source of truth for content fingerprint + duplicate grouping. Both this
# CLI/pipeline scan and the File Manager scan import the same functions so a file
# scanned by either path gets the identical FILE_HASH and is deduped the same.
from dataview.core.fingerprint import file_fingerprint, DEDUPE_SQL   # noqa: E402

# All pipeline reports (run markdown, enrich CSV; the UI also drops its run log +
# inventory CSV here) are centralized in one folder.
REPORTS_DIR = r"C:\Bulk\reports"


def _reports_dir(preferred=None, fallback=None):
    """Return the reports directory — `preferred` if given, else REPORTS_DIR —
    creating it; fall back to `fallback` if it can't be made (e.g. the volume
    isn't present)."""
    target = preferred or REPORTS_DIR
    try:
        os.makedirs(target, exist_ok=True)
        return target
    except Exception:
        if fallback:
            os.makedirs(fallback, exist_ok=True)
            return fallback
        raise


def default_exts():
    """Default scan set for a BLANK Formats-to-scan box.

    TABULAR types (.csv/.tsv and the Excel family) are EXCLUDED and no longer
    opt-in. They belong to the Bulk Tabular Loader, which maps columns and
    resolves foreign keys; the File Catalog has no extractor for them, so a
    scanned CSV becomes an inventory row that can never be extracted and shows
    as "pending" on every subsequent run. Source of truth is
    promotion_lineage.TABULAR_EXTS, with a literal fallback if the import
    fails, so this module and page_workbench cannot disagree.
    """
    try:
        from dataview.file_catalog.promotion_lineage import TABULAR_EXTS as _tab
        opt_in = set(_tab)
    except Exception:
        opt_in = {".csv", ".tsv", ".xlsx", ".xls", ".xlsm", ".xlsb"}
    try:
        from dataview.file_catalog.file_summarizer import SUPPORTED_EXTS
        base = set(SUPPORTED_EXTS)
    except Exception:
        try:
            from dataview.file_catalog.file_summarizer import SUPPORTED_EXTS
            base = set(SUPPORTED_EXTS)
        except Exception:
            base = {".las", ".dlis", ".lis", ".segy", ".sgy", ".pdf", ".shp",
                    ".geojson", ".gpkg", ".xml", ".json", ".xlsx", ".xls",
                    ".docx", ".doc", ".csv", ".tsv", ".p190"}
    return base - opt_in


def _ext_group(ext, ext_group_map):
    return ext_group_map.get(ext, "Other")


def walk_share(root, exts, json_peek=True):
    """os.scandir walk. Returns (found, folders) where found is a list of
    (path, name, ext, size_kb, mtime_iso, mtime_epoch, file_hash)."""
    found, folders, stack = [], 0, [root]
    while stack:
        d = stack.pop()
        folders += 1
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
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
                        found.append((
                            e.path, e.name, ext,
                            round(stt.st_size / 1024, 2),
                            datetime.fromtimestamp(
                                stt.st_mtime, tz=timezone.utc
                            ).strftime("%Y-%m-%d %H:%M:%S"),
                            stt.st_mtime,
                            file_fingerprint(e.path, stt.st_size, stt.st_mtime),
                        ))
                    except OSError:
                        pass
        except (PermissionError, OSError):
            pass
    return found, folders


def _seis_counts(engine):
    """Seismic catalog rollup for the run report. Seismic skips the cat_* path,
    so the 'Captured' section never reflects it; this counts how many seismic
    line-files were cataloged (their survey name reached dv_seis_set) and how
    many distinct surveys resulted. Empty dict if the seismic tables aren't
    present, so the report just omits the section."""
    from sqlalchemy import text as _t
    try:
        with engine.connect() as con:
            ok = con.execute(_t(
                "SELECT CASE WHEN OBJECT_ID('dataview.dv_seis_set') IS NOT NULL "
                "AND OBJECT_ID('file_catalog.FILE_SEIS_HEADER') IS NOT NULL "
                "THEN 1 ELSE 0 END")).scalar()
            if not ok:
                return {}
            row = con.execute(_t("""
                SELECT
                  (SELECT COUNT(*) FROM file_catalog.FILE_SEIS_HEADER) AS headers,
                  (SELECT COUNT(DISTINCT sh.INVENTORY_ID)
                     FROM file_catalog.FILE_SEIS_HEADER sh
                     JOIN dataview.dv_seis_set ss
                       ON ss.seis_set_name = sh.SURVEY_NAME)            AS files_cataloged,
                  (SELECT COUNT(*) FROM dataview.dv_seis_set)           AS surveys
            """)).fetchone()
            if not row:
                return {}
            return {"seis_headers": int(row[0] or 0),
                    "seis_files": int(row[1] or 0),
                    "seis_surveys": int(row[2] or 0)}
    except Exception:
        return {}


def _log_curve_counts(engine):
    """Log-curve catalog rollup for the run report. LAS/DLIS/LIS curves take the
    deep path (las_catalog → dv_log_curve), not the document cat_* capture, so
    the 'Captured' section never reflects them. Counts curves cataloged into
    dv_log_curve and the distinct files they came from. Empty dict if
    dv_log_curve isn't present, so the report just omits the section."""
    from sqlalchemy import text as _t
    try:
        with engine.connect() as con:
            if not con.execute(_t(
                "SELECT CASE WHEN OBJECT_ID('dataview.dv_log_curve') IS NOT NULL "
                "THEN 1 ELSE 0 END")).scalar():
                return {}
            row = con.execute(_t(
                "SELECT COUNT(*) AS curves, "
                "COUNT(DISTINCT INVENTORY_ID) AS files "
                "FROM dataview.dv_log_curve")).fetchone()
            if not row:
                return {}
            return {"curve_total": int(row[0] or 0),
                    "curve_files": int(row[1] or 0)}
    except Exception:
        return {}


def _promotion_counts(engine):
    """Deep-path-aware promotion rollup for the run report.

    The stage counters above describe what each STAGE did; this describes what
    actually LANDED, per extension, using the same INVENTORY_ID lineage the UI
    scorecards use. Without it the report is silent on the formats most likely
    to be doubted: a LAS run that worked perfectly leaves no PROMOTED_AT stamp,
    so nothing in the stage counts says its curves are in dv_*.

    Empty dict on any failure — the report omits the section rather than
    failing a run that has already done its work.
    """
    try:
        from dataview.file_catalog import promotion_lineage as _lin
        df = _lin.file_detail(engine)
        if df is None or df.empty:
            return {}
        rows = []
        for ext, g in df.groupby("ext"):
            rows.append({
                "ext": ext,
                "files": int(len(g)),
                "extracted": int((g["extract"] == "Y").sum()),
                "captured": int((g["capture"] == "Y").sum()),
                "promoted": int((g["promote"] == "Y").sum()),
            })
        rows.sort(key=lambda r: -r["files"])
        return {"promotion_by_ext": rows,
                "promotion_total": int((df["promote"] == "Y").sum()),
                "promotion_files": int(len(df))}
    except Exception:
        return {}


def report_md(summary: dict) -> str:
    s = summary
    dur = s.get("duration_sec", 0)
    lines = [
        f"# {'Batch pipeline' if s.get('batches') else 'Pipeline'} run "
        f"— {s.get('root','')}",
        "",
        f"- Run ID: `{s.get('run_id','')}`",
        f"- Started: {s.get('started','')}  ·  Duration: {dur:.0f}s",
    ]
    if s.get("batches"):
        lines.append(
            f"- Batches: {s.get('batches',0):,} × up to "
            f"{s.get('batch_size',0):,} file(s) · "
            f"{s.get('unprocessed_left',0):,} still unprocessed")
    else:
        lines.append(
            f"- Mode: vault **{'APPLY' if s.get('vault_apply') else 'dry-run'}**"
            f" ({s.get('vault_mode','copy')})")
    lines += [
        "",
        "## Scanned",
        f"- {s.get('scanned',0):,} files across {s.get('folders',0):,} folders",
        f"- {s.get('new',0):,} new · {s.get('changed',0):,} changed · "
        f"{s.get('unchanged',0):,} unchanged",
        "",
        "## Extracted",
        f"- {s.get('extract_ok',0):,} succeeded · "
        f"{s.get('extract_skip',0):,} skipped (too large) · "
        f"{s.get('extract_err',0):,} errored",
    ]
    for grp, n in sorted(s.get("by_group", {}).items()):
        lines.append(f"    - {grp}: {n:,}")
    lines += [
        "",
        "## Captured (documents → cat_*)",
        f"- {s.get('capture_rows',0):,} row(s) from "
        f"{s.get('capture_ok',0):,} of {s.get('capture_files',0):,} "
        f"UWI-resolved document(s)",
    ]
    if s.get("seis_headers") or s.get("seis_surveys"):
        lines += [
            "",
            "## Seismic (surveys → dv_seis_set)",
            f"- {s.get('seis_files',0):,} of {s.get('seis_headers',0):,} "
            f"seismic file(s) cataloged into {s.get('seis_surveys',0):,} survey(s)",
            "",
            "_Seismic skips the cat_* path — surveys are merged straight into "
            "dataview.dv_seis_set, so they aren't counted under Captured above._",
        ]
    if s.get("curve_total"):
        lines += [
            "",
            "## Log curves (LAS/DLIS/LIS → dv_log_curve)",
            f"- {s.get('curve_total',0):,} curve(s) from "
            f"{s.get('curve_files',0):,} file(s) cataloged into dv_log_curve",
            "",
            "_LAS/DLIS/LIS take the deep path (las_catalog → dv_log_curve), so "
            "they aren't counted under Captured above._",
        ]
    _pbe = s.get("promotion_by_ext")
    if s.get("promotion_rollup_skipped"):
        # The section still appears — silently dropping it would look like
        # nothing landed, which is the opposite of what happened.
        lines += [
            "",
            "## Landed in dv_* — by extension",
            "",
            "_Skipped — this describes the database rather than this run. "
            "Use the Stage scorecard (extract · capture · promote per file) "
            "or the Database scorecard, or re-run with deep_rollup=True._",
        ]
    elif _pbe:
        lines += [
            "",
            "## Landed in dv_* — by extension",
            f"- {s.get('promotion_total',0):,} of {s.get('promotion_files',0):,} "
            f"catalogued file(s) have data in dv_*",
            "",
            "| ext | files | extracted | captured | promoted |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for r in _pbe:
            lines.append(f"| {r['ext']} | {r['files']:,} | {r['extracted']:,} "
                         f"| {r['captured']:,} | {r['promoted']:,} |")
        lines += [
            "",
            "_Credit follows INVENTORY_ID lineage into the dv_ tables, not the "
            "PROMOTED_AT stamp. LAS/DLIS/LIS write dv_well_log(_curve) directly "
            "and SEG-Y merges into dv_seis_set, so neither is ever stamped — "
            "the stamp alone reports them as unpromoted when their data is "
            "loaded._",
        ]
    lines += [
        "",
        "## Placed in vault",
        f"- {s.get('vault_total',0):,} placements "
        f"({'written' if s.get('vault_apply') else 'planned'})",
    ]
    for b, n in sorted(s.get("vault_buckets", {}).items()):
        lines.append(f"    - {b}: {n:,}")
    lines += [
        "",
        "## Needs review",
        f"- REVIEW (name, no confident UWI): {s.get('tier_REVIEW',0):,}",
        f"- LOW (no usable identity): {s.get('tier_LOW',0):,}",
        f"- REJECT (blocklisted): {s.get('tier_REJECT',0):,}",
        f"- HIGH (auto-cleared): {s.get('tier_HIGH',0):,}",
        "",
        "_Resolve REVIEW/LOW items in the Triage tab's worklist._",
    ]
    bt = s.get("extract_by_type") or {}
    if bt:
        lines += ["", "## Extract parse cost by type (avg s/file, slowest first)"]
        for ext, d in sorted(bt.items(), key=lambda kv: -kv[1].get("avg", 0)):
            lines.append(f"- {ext or '?'}: {d.get('avg',0):.2f}s/file "
                         f"× {d.get('n',0):,} = {d.get('sec',0):.1f}s")
    st_times = s.get("stage_times") or {}
    if st_times:
        lines += ["", "## Stage timing (slowest first)"]
        for name, dt in sorted(st_times.items(), key=lambda kv: -kv[1]):
            pct = (dt / dur * 100.0) if dur else 0.0
            lines.append(f"- {name}: {dt:.1f}s ({pct:.0f}%)")
    if s.get("errors"):
        lines += ["", "## Stage errors"]
        for stg, err in s["errors"].items():
            lines.append(f"- {stg}: {err}")
    return "\n".join(lines) + "\n"


# ── DB stages ─────────────────────────────────────────────────────────────────
def _stage_scan(engine, root, exts, log):
    """Walk the share and MERGE into GLOBAL_FILE_CATALOG. New + fingerprint-
    changed files become pending (HEADER_EXTRACTED='N'/NULL); a file whose
    FILE_HASH matches the stored one is untouched — seen this fingerprint,
    skip it. Falls back to size when no hash is available (very large files)."""
    from sqlalchemy import text as _t
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dataview.file_catalog.extract_core import EXT_GROUP

    log(f"[scan] walking {root} …")
    found, folders = walk_share(root, exts)
    if not found:
        log("[scan] no matching files found.")
        return {"scanned": 0, "folders": folders, "new": 0, "changed": 0,
                "unchanged": 0}

    # bad-file blocklist
    bad = set()
    try:
        with engine.connect() as con:
            bad = {r[0] for r in con.execute(_t(
                "SELECT INVENTORY_ID FROM file_catalog.BAD_FILE")).fetchall()}
    except Exception:
        pass

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False,
                                      newline="", encoding="utf-8")
    csv_path = tmp.name
    # NO escapechar. This was the FOURTH catalog staging writer, and the one
    # the 16 Aug sweep missed — so the bug came straight back through the
    # pipeline, which is the DEFAULT path. Every scan since has written
    # doubled paths: with QUOTE_NONE the csv module escapes the escape
    # character itself, and BULK INSERT has no escape concept, so it stores
    # the doubled form verbatim. iid is hashed from the CLEAN fpath, so the
    # escaped write left INVENTORY_ID and FILE_PATH describing different
    # strings; root[:900] goes through the same writer, which is why
    # ROOT_PATH was doubled too. See path_identity.bulk_csv_writer.
    from dataview.core.path_identity import bulk_csv_writer, bulk_field
    w = bulk_csv_writer(tmp)
    n = 0
    n_sanitised = 0
    for (fpath, fname, fext, size_kb, mtime_iso, _ep, fhash) in found:
        iid = inv_id(fpath)
        if iid in bad:
            continue
        row = []
        for _v in (iid, fpath[:900], fname[:260], fext[:20],
                   _ext_group(fext, EXT_GROUP)[:50],
                   size_kb if size_kb else "",
                   fhash, "", "UNCATALOGED", "", root[:900], now, now, now):
            _val, _changed = bulk_field(_v)
            row.append(_val)
            n_sanitised += bool(_changed)
        w.writerow(row)
        n += 1
    if n_sanitised:
        # Report, never repair silently — the stored value now differs from
        # the bytes on disk, and a wrong value outlives a missing one.
        log(f"[scan] {n_sanitised} field(s) held a tab, quote or newline and "
            f"were rewritten to load; stored value differs from disk.")
    tmp.close()

    dup_cnt = 0
    try:
        with engine.begin() as con:
            con.execute(_t("""
                IF OBJECT_ID('file_catalog.fc_stage','U') IS NOT NULL
                    DROP TABLE file_catalog.fc_stage;
                CREATE TABLE file_catalog.fc_stage (
                    INVENTORY_ID NVARCHAR(40), FILE_PATH NVARCHAR(900),
                    FILE_NAME NVARCHAR(260), FILE_EXT NVARCHAR(20),
                    FILE_TYPE_GROUP NVARCHAR(50), FILE_SIZE_KB NVARCHAR(30),
                    FILE_HASH NVARCHAR(40), DUPLICATE_GROUP NVARCHAR(64),
                    CATALOG_STATUS NVARCHAR(20), CATALOG_TABLE NVARCHAR(100),
                    ROOT_PATH NVARCHAR(900), SCAN_DATE NVARCHAR(30),
                    ROW_CREATED_DATE NVARCHAR(30), ROW_CHANGED_DATE NVARCHAR(30)
                );
            """))
            con.execute(_t(f"""
                BULK INSERT file_catalog.fc_stage FROM '{csv_path}'
                WITH (FIELDTERMINATOR='\\t', ROWTERMINATOR='0x0D0A',
                      CODEPAGE='65001', FIRSTROW=1, TABLOCK);
            """))
            # counts before MERGE
            new_cnt = con.execute(_t("""
                SELECT COUNT(*) FROM file_catalog.fc_stage s
                LEFT JOIN file_catalog.GLOBAL_FILE_CATALOG g
                    ON g.INVENTORY_ID = s.INVENTORY_ID
                WHERE g.INVENTORY_ID IS NULL
            """)).scalar() or 0
            chg_cnt = con.execute(_t("""
                SELECT COUNT(*) FROM file_catalog.fc_stage s
                JOIN file_catalog.GLOBAL_FILE_CATALOG g
                    ON g.INVENTORY_ID = s.INVENTORY_ID
                WHERE (s.FILE_HASH IS NOT NULL AND s.FILE_HASH <> ''
                       AND ISNULL(g.FILE_HASH,'') <> s.FILE_HASH)
                   OR ((s.FILE_HASH IS NULL OR s.FILE_HASH = '')
                       AND ABS(ISNULL(g.FILE_SIZE_KB,-1)
                               - TRY_CAST(s.FILE_SIZE_KB AS DECIMAL(15,2))) > 0.01)
            """)).scalar() or 0
            # incremental MERGE: changed -> re-extract; new -> insert pending
            con.execute(_t("""
                MERGE file_catalog.GLOBAL_FILE_CATALOG AS tgt
                USING file_catalog.fc_stage AS src
                ON tgt.INVENTORY_ID = src.INVENTORY_ID
                WHEN MATCHED AND (
                        (src.FILE_HASH IS NOT NULL AND src.FILE_HASH <> ''
                         AND ISNULL(tgt.FILE_HASH,'') <> src.FILE_HASH)
                     OR ((src.FILE_HASH IS NULL OR src.FILE_HASH = '')
                         AND ABS(ISNULL(tgt.FILE_SIZE_KB,-1)
                                 - TRY_CAST(src.FILE_SIZE_KB AS DECIMAL(15,2))) > 0.01))
                THEN UPDATE SET
                    FILE_SIZE_KB     = TRY_CAST(src.FILE_SIZE_KB AS DECIMAL(15,2)),
                    FILE_HASH        = src.FILE_HASH,
                    HEADER_EXTRACTED = 'N',
                    SCAN_DATE        = TRY_CAST(src.SCAN_DATE AS DATETIME2),
                    ROW_CHANGED_DATE = TRY_CAST(src.ROW_CHANGED_DATE AS DATETIME2)
                WHEN NOT MATCHED THEN INSERT (
                    INVENTORY_ID,FILE_PATH,FILE_NAME,FILE_EXT,FILE_TYPE_GROUP,
                    FILE_SIZE_KB,FILE_HASH,DUPLICATE_GROUP,CATALOG_STATUS,
                    CATALOG_TABLE,ROOT_PATH,SCAN_DATE,ROW_CREATED_DATE,
                    ROW_CHANGED_DATE
                ) VALUES (
                    src.INVENTORY_ID,src.FILE_PATH,src.FILE_NAME,src.FILE_EXT,
                    src.FILE_TYPE_GROUP,TRY_CAST(src.FILE_SIZE_KB AS DECIMAL(15,2)),
                    src.FILE_HASH,src.DUPLICATE_GROUP,src.CATALOG_STATUS,
                    src.CATALOG_TABLE,src.ROOT_PATH,
                    TRY_CAST(src.SCAN_DATE AS DATETIME2),
                    TRY_CAST(src.ROW_CREATED_DATE AS DATETIME2),
                    TRY_CAST(src.ROW_CHANGED_DATE AS DATETIME2)
                );
            """))
            # Content-dedupe via the shared rule: one canonical per FILE_HASH
            # stays processable (DUPLICATE_GROUP NULL); redundant copies get
            # DUPLICATE_GROUP set and are skipped by extract + capture (and the
            # File Manager's assignments). Identical to the File Manager scan.
            con.execute(_t(DEDUPE_SQL))
            dup_cnt = con.execute(_t(
                "SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG "
                "WHERE DUPLICATE_GROUP IS NOT NULL")).scalar() or 0
            con.execute(_t("DROP TABLE IF EXISTS file_catalog.fc_stage;"))
    finally:
        try:
            os.unlink(csv_path)
        except Exception:
            pass

    log(f"[scan] {n:,} files · {folders:,} folders · "
        f"{new_cnt:,} new · {chg_cnt:,} changed"
        + (f" · {dup_cnt:,} dup-skipped" if dup_cnt else ""))
    return {"scanned": n, "folders": folders, "new": new_cnt,
            "changed": chg_cnt, "unchanged": n - new_cnt - chg_cnt,
            "dup_skipped": dup_cnt}


def _ext_filter(exts, col="FILE_EXT"):
    """SQL fragment ' AND <col> IN (...)' restricting to the given extensions,
    or '' when no scope is requested. Extensions come from a fixed allow-list,
    so a literal IN is safe; the quote-escape is belt-and-braces."""
    if not exts:
        return ""
    vals = ", ".join("'" + str(e).replace("'", "''") + "'" for e in sorted(exts))
    return f" AND {col} IN ({vals})"


def _extract_one_proc(arg):
    """ProcessPoolExecutor worker: parse ONE file's header in a clean subprocess.

    Imports extract_core (streamlit-free) lazily — NEVER page_workbench — so a
    spawned child stays light and never drags streamlit into the process. Pure
    parse, no DB: the parent does every write. Returns the same 5-tuple shape as
    the in-thread _worker so the result handling is identical for both modes.
    Safe to spawn only from a standalone process (the CLI); under Streamlit on
    Windows spawn re-imports the app entry, so the UI path stays on threads.
    """
    iid, fpath, fext = arg
    import os as _os, sys as _sys, time as _time
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    _w0 = _time.monotonic()
    # MOVED/DELETED, not broken — see _worker for why these are separated.
    if not _os.path.exists(fpath):
        return ("missing", iid, "no file at the catalogued path", 0.0, fext)
    try:
        from dataview.file_catalog import extract_core
        fields = extract_core._extract_fields(fpath, fext)
        _sec = _time.monotonic() - _w0
        if fields.get("skip_reason"):
            return ("skip", iid, fields, _sec, fext)
        return ("ok", iid, fields, _sec, fext)
    except Exception as e:
        return ("err", iid, f"{type(e).__name__}: {e}", _time.monotonic() - _w0, fext)


# ── EXTRACT STEP TIMING ─────────────────────────────────────────────────
#
# The arithmetic that prompted this: extract reported 178.8 WORKER-seconds
# of parse across 1,055 files and took 179.9s of WALL time. Those should
# not be the same number — six workers at those per-file costs should give
# ~35 files/sec and the stage delivered 5.9, about 17% of it.
#
# Two explanations fit and they lead to completely different work:
#   * the pool is not actually parallelising   -> fix the pool
#   * the per-chunk database write dominates   -> the reorder
#
# 22 chunks in 179.9s is 8.2s a chunk, of which parse could only be ~1.4s,
# which points at the write. But pointing has been wrong five times running
# on this pipeline, so it gets measured instead.
_EX_TIMES = {}
_EX_COUNTS = {}


def _ex_tick(step, t0):
    _EX_TIMES[step] = _EX_TIMES.get(step, 0.0) + (time.perf_counter() - t0)
    _EX_COUNTS[step] = _EX_COUNTS.get(step, 0) + 1


def _ex_report(log, files=0):
    if not _EX_TIMES:
        return
    tot = sum(_EX_TIMES.values())
    parts = [f"{k} {v:.1f}s ({100.0 * v / tot:.0f}%, {_EX_COUNTS.get(k, 0):,}x)"
             for k, v in sorted(_EX_TIMES.items(), key=lambda kv: -kv[1])]
    log(f"[extract-steps] {tot:.1f}s measured across {files:,} file(s) · "
        + " · ".join(parts))


def _stage_extract(engine, workers, log, max_files=None, stall_timeout=180,
                   exts=None, per_type_cap=None, parse_mode="thread",
                   root=None, should_abort=None):
    """Reuse extract_core._extract_fields + _write_enrichment_on on every
    pending file, parallel parse + sequential write, chunked.

    max_files     — process at most this many files this run, then stop (the
                    rest stay pending and the next run continues — resumable).
    stall_timeout — per-chunk watchdog (seconds). A hung parser can't be killed
                    in-thread, so once a chunk exceeds this we stop waiting on
                    the stragglers, mark their files 'E' (quarantined, not
                    retried next run), and move on.
    exts          — restrict processing to these file extensions (format scope).
    root          — restrict processing to files UNDER this folder (path scope),
                    already canonical. None means the whole pending queue, which
                    is what this stage always did: only `scan` was ever scoped to
                    the folder you gave it, so a run pointed at one directory
                    still extracted files anywhere in the catalog.
    per_type_cap  — TIMING-TEST knob. When set, process at most this many
                    pending files per FILE_EXT in a single sampling pass (e.g.
                    5 → 5 .sgy + 5 .las + 5 .pdf …), so a test run stays small
                    and balanced instead of drowning in one heavy type. Pairs
                    with the per-type parse timing returned in 'extract_by_type'.
    """
    from sqlalchemy import text as _t
    from concurrent.futures import ThreadPoolExecutor, wait as _wait
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dataview.file_catalog import extract_core as wb
    # ONE definition of extract-pending, shared with _unprocessed_count and the
    # batch loop. Spelled out separately in three places, they drifted.
    from dataview.file_catalog.promotion_lineage import pending_sql
    _PENDING_EXTRACT = pending_sql("extract")

    CHUNK = getattr(wb, "ENRICH_CHUNK", 300)
    ok = skip = err = timeout = missing = 0
    processed = 0
    per_ext: dict = {}                       # ext -> [count, total_parse_secs]
    _extf = _ext_filter(exts)
    _rootf = _root_filter(root, "")          # '' -> bare FILE_PATH, unaliased
    if _rootf:
        log(f"[extract] path scope: files under {root}")

    _EX_TIMES.clear()
    _EX_COUNTS.clear()

    # ── Parse pool: created ONCE and reused across chunks ──────────────────
    # Spawning a process pool per chunk would re-import the parsers in every
    # worker on every chunk — on Windows spawn that startup cost dominates.
    # Create it once; only recycle when a chunk stalls (the sole way to reclaim
    # a worker stuck on a hung file).
    def _worker(r):
        iid, fpath, fext = r
        _ext = (fext or "").lower()
        _w0 = time.monotonic()
        # THE FILE MOVED. Checked before the parser is handed the path, and
        # reported as its own outcome rather than as an error, because they are
        # different facts with different repairs: 'E' means this file is broken
        # or unparseable and wants a look; 'M' means the CATALOG is stale and
        # the row wants re-pointing or clearing. Folded together — which is what
        # happened, since the parser raised FileNotFoundError straight into the
        # 'err' bucket — a folder somebody reorganised reads as a corpus full of
        # corrupt files, and the count that should prompt a rescan instead
        # prompts a hunt for a parser bug.
        #
        # Held, not dropped: 'M' is outside EXTRACT_PENDING (which claims only
        # NULL and 'N'), so the row stops being retried every run but stays in
        # the catalog, named, with its reason in CATALOG_ISSUES. Rescanning the
        # file where it now lives re-catalogues it; the stale row is still there
        # to be reconciled, which a DELETE would have made impossible.
        if not os.path.exists(fpath):
            return ("missing", iid, "no file at the catalogued path",
                    time.monotonic() - _w0, _ext)
        try:
            fields = wb._extract_fields(fpath, _ext)
            _sec = time.monotonic() - _w0
            if fields.get("skip_reason"):
                return ("skip", iid, fields, _sec, _ext)
            return ("ok", iid, fields, _sec, _ext)
        except Exception as e:
            return ("err", iid, f"{type(e).__name__}: {e}",
                    time.monotonic() - _w0, _ext)

    if parse_mode == "process":
        from concurrent.futures import ProcessPoolExecutor
        try:
            _mk_pool = lambda: ProcessPoolExecutor(max_workers=workers)
            pool = _mk_pool()
        except Exception as e:
            log(f"[extract] process pool unavailable ({type(e).__name__}: "
                f"{e}); falling back to threads")
            parse_mode = "thread"
    if parse_mode != "process":
        _mk_pool = lambda: ThreadPoolExecutor(max_workers=workers)
        pool = _mk_pool()
    if parse_mode == "process":
        log(f"[extract] spawning {workers} process workers — Windows imports the "
            f"parsers once per worker, so the first chunk is slow to warm up…")

    def _submit(r):
        if parse_mode == "process":
            return pool.submit(_extract_one_proc,
                               (r[0], r[1], (r[2] or "").lower()))
        return pool.submit(_worker, r)

    # EVERY FILE THIS STAGE HAS ALREADY TRIED. A claim that comes back
    # holding nothing new means the previous attempt cleared no pending
    # flag, so the next claim returns the same rows and this loop never
    # ends. That is not hypothetical: 7 LAS files whose header write
    # failed (nvarchar -> numeric) were re-parsed ~570 times each, wrote
    # 117,640 log lines, and reported "ok 3,995" for 7 files.
    attempted = set()

    while True:
        # The abort hook was reaching only _go(), which runs BETWEEN
        # stages - so the UI's stop file could not interrupt a stage
        # already looping and the process tree had to be killed.
        if should_abort and should_abort():
            log("[extract] abort requested - stopping between chunks "
                f"({processed:,} file(s) processed; the rest stay pending).")
            break
        remaining = None if max_files is None else max_files - processed
        if remaining is not None and remaining <= 0:
            log(f"[extract] reached cap of {max_files:,} — stopping "
                f"(re-run to continue).")
            break
        top = CHUNK if remaining is None else min(CHUNK, remaining)
        _t_chunk = time.perf_counter()
        with engine.connect() as con:
            if per_type_cap:
                # one balanced sampling pass: <= cap pending files per FILE_EXT
                rows = con.execute(_t(f"""
                    SELECT INVENTORY_ID, FILE_PATH, FILE_EXT FROM (
                        SELECT INVENTORY_ID, FILE_PATH, FILE_EXT,
                               ROW_NUMBER() OVER (PARTITION BY FILE_EXT
                                   ORDER BY SCAN_DATE DESC) AS _rn
                        FROM file_catalog.GLOBAL_FILE_CATALOG
                        WHERE {_PENDING_EXTRACT}
                          -- NOTE: .las is deliberately NOT excluded here. It used to be
                          -- ('capture writes FILE_WELL_HEADER'), but capture's BCP lane
                          -- writes ONLY that header table plus a bare HEADER_EXTRACTED='Y'
                          -- stamp. The GFC enrichment columns (CATALOG_SCORE /
                          -- CATALOG_READINESS / MATCHED_UWI / CATALOG_ISSUES) are written
                          -- ONLY by this stage, so skipping .las left every LAS row with a
                          -- NULL MATCHED_UWI: not loadable, unresolved in the assign grid,
                          -- invisible to UWI-keyed queries. Parse is header-only
                          -- (lasio ignore_data=True), so the extra pass is cheap.
                          {_extf}{_rootf}
                    ) q WHERE q._rn <= :cap
                    ORDER BY FILE_EXT, INVENTORY_ID
                """), {"cap": int(per_type_cap)}).fetchall()
            else:
                rows = con.execute(_t(f"""
                    SELECT TOP {top} INVENTORY_ID, FILE_PATH, FILE_EXT
                    FROM file_catalog.GLOBAL_FILE_CATALOG
                    WHERE {_PENDING_EXTRACT}
                      -- .las intentionally NOT excluded - see note above.{_extf}{_rootf}
                    ORDER BY SCAN_DATE DESC
                """)).fetchall()
        _ex_tick("claim_query", _t_chunk)

        if not rows:
            break

        # NO PROGRESS = STOP, AND SAY WHICH FILES. A file whose write
        # failed keeps its pending flag, so it is claimed again next
        # chunk. Only break when EVERY claimed row was already tried:
        # a chunk that still carries new work must run, or a single bad
        # file would halt a queue of thousands.
        claimed = [r[0] for r in rows]
        if all(i in attempted for i in claimed):
            _stuck = ", ".join(str(i)[:12] for i in claimed[:5])
            if len(claimed) > 5:
                _stuck += f", +{len(claimed) - 5} more"
            log(f"[extract] no progress - {len(claimed):,} file(s) came "
                f"back pending after being processed, so the write is "
                f"failing and re-running will not clear them. Stopping "
                f"instead of looping. Stuck: {_stuck}. The reason is "
                f"in the [x] write lines above.")
            break
        attempted.update(claimed)

        results = []
        _t_parse = time.perf_counter()
        fut_row = {_submit(r): r for r in rows}
        log(f"[extract] parsing {len(rows)} file(s)… "
            f"(OneDrive cloud files hydrate on first read — that can be slow)")
        done, not_done = _wait(fut_row, timeout=stall_timeout)
        _ex_tick("parse_wait", _t_parse)
        for f in done:
            try:
                results.append(f.result())
            except Exception as e:
                _r = fut_row[f]
                results.append(("err", _r[0], f"worker error: {e}",
                                0.0, (_r[2] or "").lower()))
        for f in not_done:                       # stalled past stall_timeout
            f.cancel()
            _r = fut_row[f]
            results.append(("timeout", _r[0], f"stalled > {stall_timeout}s",
                            float(stall_timeout), (_r[2] or "").lower()))
        if not_done:
            # a hung worker can't be killed in place; recycle the pool to
            # reclaim its capacity for the next chunk (rare — the size gate
            # prevents most hangs)
            pool.shutdown(wait=False)
            pool = _mk_pool()

        # bucket results, then write the whole chunk in batched round-trips
        ok_items, skip_ids, err_items, missing_items = [], [], [], []
        for outcome, iid, payload, _sec, _ext in results:
            pe = per_ext.setdefault(_ext or "?", [0, 0.0])
            pe[0] += 1
            pe[1] += float(_sec or 0.0)
            if outcome == "ok" and iid is not None:
                ok_items.append((iid, payload))
            elif outcome == "skip" and iid is not None:
                skip_ids.append({"id": iid})
            elif outcome == "missing" and iid is not None:
                missing_items.append({"id": iid, "e": str(payload)[:500]})
                missing += 1
            elif iid is not None:                # 'err' or 'timeout' → quarantine
                err_items.append({"id": iid, "e": str(payload)[:500]})
                if outcome == "timeout":
                    timeout += 1
                else:
                    err += 1

        _SKIP_SQL = ("UPDATE file_catalog.GLOBAL_FILE_CATALOG "
                     "SET HEADER_EXTRACTED='S', ROW_CHANGED_DATE=GETUTCDATE() "
                     "WHERE INVENTORY_ID=:id")
        _ERR_SQL = ("UPDATE file_catalog.GLOBAL_FILE_CATALOG "
                    "SET HEADER_EXTRACTED='E', CATALOG_ISSUES=:e, "
                    "ROW_CHANGED_DATE=GETUTCDATE() WHERE INVENTORY_ID=:id")
        # 'M' — the file is gone from the catalogued path. Its own letter, not
        # 'E', so the two can be told apart and counted apart afterwards.
        _MISSING_SQL = ("UPDATE file_catalog.GLOBAL_FILE_CATALOG "
                        "SET HEADER_EXTRACTED='M', CATALOG_ISSUES=:e, "
                        "ROW_CHANGED_DATE=GETUTCDATE() WHERE INVENTORY_ID=:id")

        def _write_skip_err(con):
            if skip_ids:
                con.execute(_t(_SKIP_SQL), skip_ids)        # executemany
            if err_items:
                con.execute(_t(_ERR_SQL), err_items)        # executemany
            if missing_items:
                con.execute(_t(_MISSING_SQL), missing_items)

        _t_write = time.perf_counter()
        try:
            with engine.begin() as con:
                wb._write_enrichment_batch(con, ok_items)   # batched header write
                _write_skip_err(con)
        except Exception as _be:
            log(f"[extract] batch write failed ({type(_be).__name__}: {_be}); "
                f"per-row fallback for this chunk")
            with engine.begin() as con:
                for _iid, _payload in ok_items:
                    try:
                        wb._write_enrichment_on(con, _iid, _payload)
                    except Exception as _we:
                        log(f"  [x] write {_iid}: {type(_we).__name__}: {_we}")
                _write_skip_err(con)
        _ex_tick("header_write", _t_write)
        ok += len(ok_items)
        skip += len(skip_ids)
        processed += len(rows)
        log(f"[extract] +{len(results)} (ok {ok:,} · skip {skip:,} · "
            f"err {err:,} · timeout {timeout:,} · missing {missing:,})")

        if per_type_cap:
            log(f"[extract] per-type cap {per_type_cap} — single sampling "
                f"pass done ({processed} file(s) across {len(per_ext)} type(s)).")
            break

    try:
        pool.shutdown(wait=False)
    except Exception:
        pass

    # per-type parse cost: avg seconds/file by extension, slowest first
    by_type = {
        ext: {"n": c, "sec": round(t, 2), "avg": round(t / c, 3) if c else 0.0}
        for ext, (c, t) in per_ext.items()
    }
    # REPORTING MUST NOT BE ABLE TO FAIL THE STAGE. My first version called
    # this with a variable that does not exist in this scope, and the whole
    # extract stage — 179 seconds of completed work — reported FAILED at the
    # last line. The parsing and the per-chunk writes had all landed; only
    # the return value was lost, which is why the summary said "extracted 0"
    # for a run that extracted 1,055. Timing is an observation, never a
    # participant.
    try:
        _ex_report(log, files=ok)
    except Exception as _re:                              # noqa: BLE001
        log(f"[extract] (step timing unavailable: {type(_re).__name__})")

    if by_type:
        worst = sorted(by_type.items(), key=lambda kv: -kv[1]["avg"])
        log("[extract] parse cost by type (avg s/file): " + " · ".join(
            f"{e} {d['avg']:.2f}s×{d['n']}" for e, d in worst))

    if missing:
        # Said plainly and separately: this number is not a parser problem and
        # the repair is not a code change. The rows are HELD ('M'), not dropped
        # — they keep their INVENTORY_ID and their reason, so re-scanning the
        # files where they now live reconciles them.
        log(f"[extract] {missing:,} file(s) HELD as missing — catalogued once "
            f"but no longer at the recorded path. Nothing is wrong with these "
            f"files; the catalog is stale. Re-scan wherever they live now, or "
            f"clear the rows. They will not be retried "
            f"(HEADER_EXTRACTED='M', reason in CATALOG_ISSUES).")

    return {"extract_ok": ok, "extract_skip": skip, "extract_err": err,
            "extract_timeout": timeout, "extract_missing": missing,
            "extract_by_type": by_type}


def _stage_triage(engine, ref, log):
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dataview.file_catalog import triage_inventory
    log("[triage] normalize · cross-fill · reference-fill · score/tier …")
    tiers = triage_inventory.run_all_engine(
        engine, ref=ref, dry=False, log=lambda m: log("  " + str(m)))
    norm = {f"tier_{(k or 'NA').upper().replace(' ','_')}": v
            for k, v in (tiers or {}).items()}
    log(f"[triage] {norm}")
    return norm


# One SQLAlchemy engine per capture worker PROCESS, reused across every file that
# worker handles — instead of create_engine() + a fresh SQL Server handshake for
# all ~237 files, which was pure per-file overhead. Populated by the pool
# initializer; process_file borrows pooled connections from it.
_CAP_ENG = {}


def _capture_pool_init(url):
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    try:
        from sqlalchemy import create_engine
        from dataview.file_catalog import worker_core  # noqa: F401 — warm the import once per worker
        _CAP_ENG["eng"] = create_engine(url, fast_executemany=True)
    except Exception:
        _CAP_ENG["eng"] = None


def _capture_proc_one(arg):
    """ProcessPoolExecutor worker: parse + capture ONE file via the streamlit-free
    worker_core.process_file, reusing this worker's shared engine. Mirrors
    _extract_one_proc (imports worker_core, NEVER page_workbench, so the child
    stays light). Safe to spawn only from a standalone process (the detached
    multi-core runner) — under Streamlit spawn re-imports the app, so the in-app
    path keeps the sequential capture below."""
    url, rec = arg
    import os as _os, sys as _sys, time as _time
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    try:
        from dataview.file_catalog import worker_core as wc
        eng = _CAP_ENG.get("eng")
        _own = False
        if eng is None:                       # fallback if the initializer didn't run
            from sqlalchemy import create_engine
            eng = create_engine(url, fast_executemany=True)
            _own = True
        try:
            _t0 = _time.monotonic()
            res = wc.process_file(eng, rec)
            _dt = _time.monotonic() - _t0
            return (rec.get("FILE_NAME"), getattr(res, "status", None),
                    int(getattr(res, "rows_written", 0) or 0),
                    getattr(res, "error", None), _dt)
        finally:
            if _own:
                eng.dispose()
    except Exception as e:
        return (rec.get("FILE_NAME"), "error", 0, f"{type(e).__name__}: {e}", 0.0)


def _extract_capture_proc(arg):
    """Single-pass worker: open the file ONCE, parse the header fields (for
    GLOBAL_FILE_CATALOG) AND capture the cat_* rows, back-to-back so the file
    stays hot in cache (one OneDrive hydration, not two). Streamlit-free:
    extract_core for the header, worker_core.process_file for the capture — the
    parent writes GLOBAL_FILE_CATALOG from the returned fields. Returns
    (outcome, iid, payload, sec, fext, cap_rows, cap_err)."""
    url, iid, fpath, fext, do_cap, sp_exts = arg
    import os as _os, sys as _sys, time as _time
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    _t0 = _time.monotonic()
    try:
        from dataview.file_catalog import extract_core
        fields = extract_core._extract_fields(fpath, fext)
    except Exception as e:
        return ("err", iid, f"{type(e).__name__}: {e}",
                _time.monotonic() - _t0, fext, 0, None)
    _sec = _time.monotonic() - _t0
    if fields.get("skip_reason"):
        return ("skip", iid, fields, _sec, fext, 0, None)

    cap_rows, cap_err = 0, None
    _e = fext.lower().lstrip(".")
    if do_cap and (_e in sp_exts or fields.get("uwi")):
        try:
            from sqlalchemy import create_engine
            from dataview.file_catalog import worker_core as wc
            eng = create_engine(url, fast_executemany=True)
            try:
                res = wc.process_file(eng, {
                    "FILE_PATH": fpath, "FILE_EXT": fext,
                    "UWI": fields.get("uwi") or "",
                    "MATCHED_UWI": fields.get("uwi") or "",
                    "INVENTORY_ID": iid})
                cap_rows = int(getattr(res, "rows_written", 0) or 0)
            finally:
                eng.dispose()
        except Exception as e:
            cap_err = f"capture: {type(e).__name__}: {e}"
    return ("ok", iid, fields, _sec, fext, cap_rows, cap_err)


def _stage_extract_capture(engine, workers, log, exts=None, do_capture=True,
                           root=None):
    """Single-pass EXTRACT+CAPTURE: parse each pending file once, writing both the
    GLOBAL_FILE_CATALOG header (parent, batched) and the cat_* mirrors (worker).
    Runs only from the detached multi-core process. Opt-in via single_pass=True."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from concurrent.futures import ProcessPoolExecutor
    from sqlalchemy import text as _t
    from dataview.file_catalog import extract_core as wb
    try:
        from dataview.file_catalog.extract_core import SELF_PARSING_EXTS
        sp = sorted({e.lower().lstrip(".") for e in SELF_PARSING_EXTS})
    except Exception:
        sp = ["las", "pdf", "docx", "doc", "xlsx", "xls", "xml", "json"]

    # The SAME extract-pending predicate _stage_extract claims on. This stage is
    # the merged fast route, and it had its own copy — which is exactly how the
    # six spellings accumulated.
    from dataview.file_catalog.promotion_lineage import pending_sql

    try:
        _url = engine.url.render_as_string(hide_password=False)
    except Exception:
        _url = str(engine.url)

    with engine.connect() as con:
        rows = con.execute(_t(f"""
            SELECT INVENTORY_ID, FILE_PATH, FILE_EXT
            FROM file_catalog.GLOBAL_FILE_CATALOG
            WHERE {pending_sql('extract')}{_ext_filter(exts, 'FILE_EXT')}{_root_filter(root, '')}
            ORDER BY SCAN_DATE DESC
        """)).fetchall()
    total = len(rows)
    if not total:
        return {"extract_ok": 0, "capture_rows": 0}
    log(f"[extract+capture] single-pass over {total:,} file(s) on {workers} core(s) "
        f"(one open per file) …")

    args = [(_url, r[0], r[1], str(r[2] or "").lower(), do_capture, sp)
            for r in rows]
    ok_items, skip_ids, err_items = [], [], []
    ok = skip = err = cap_rows = 0
    _seen = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for outcome, iid, payload, _sec, _ext, _cr, _ce in pool.map(
                _extract_capture_proc, args):
            _seen += 1
            if outcome == "ok":
                ok_items.append((iid, payload)); ok += 1
                cap_rows += _cr or 0
                if _ce:
                    log(f"  [x] {iid}: {_ce}")
            elif outcome == "skip":
                skip_ids.append({"id": iid}); skip += 1
            else:
                err_items.append({"id": iid, "e": str(payload)[:500]}); err += 1
            if _seen % 50 == 0 or _seen == total:
                log(f"[extract+capture] {_seen}/{total} "
                    f"(ok {ok} · skip {skip} · err {err} · {cap_rows} cat_* rows)")

    _SKIP = ("UPDATE file_catalog.GLOBAL_FILE_CATALOG SET HEADER_EXTRACTED='S', "
             "ROW_CHANGED_DATE=GETUTCDATE() WHERE INVENTORY_ID=:id")
    _ERR = ("UPDATE file_catalog.GLOBAL_FILE_CATALOG SET HEADER_EXTRACTED='E', "
            "CATALOG_ISSUES=:e, ROW_CHANGED_DATE=GETUTCDATE() WHERE INVENTORY_ID=:id")
    try:
        with engine.begin() as con:
            wb._write_enrichment_batch(con, ok_items)
            if skip_ids:
                con.execute(_t(_SKIP), skip_ids)
            if err_items:
                con.execute(_t(_ERR), err_items)
    except Exception as _be:
        log(f"[extract+capture] batch write failed ({_be}); per-row fallback")
        with engine.begin() as con:
            for _iid, _p in ok_items:
                try:
                    wb._write_enrichment_on(con, _iid, _p)
                except Exception as _we:
                    log(f"  [x] write {_iid}: {_we}")
            if skip_ids:
                con.execute(_t(_SKIP), skip_ids)
            if err_items:
                con.execute(_t(_ERR), err_items)

    log(f"[extract+capture] ok {ok:,} · skip {skip:,} · err {err:,} · "
        f"{cap_rows:,} cat_* row(s) captured")
    return {"extract_ok": ok, "extract_skip": skip, "extract_err": err,
            "capture_rows": cap_rows}


def _root_likes(root):
    r"""LIKE patterns matching every file UNDER `root` — both spellings of it.

    Three details, all load-bearing:

    * ESCAPE '\' — a folder called `Well_Log` or `100%_reprocessed` is a
      literal name, not a wildcard. `_` matches any character in T-SQL, which
      is the same trap CLAUDE.md records for `LIKE 'cat_%'`. Backslashes are
      doubled FIRST so the escape character itself survives; the order is what
      the seismic re-extract block in page_workbench uses.

    * THE TRAILING SEPARATOR IS NOT COSMETIC. `C:\data%` also matches
      `C:\database\...`, so a root would silently drag in its siblings.
      Matching `C:\data\%` cannot. A root already ending in a separator (a bare
      drive, `D:\`) is left alone rather than given a second one.

    * TWO PATTERNS, NOT ONE. canon_root collapses `C:\\a\\b` to `C:\a\b` on the
      way IN — but rows scanned before that fix are STORED doubled, and on the
      database this was written against 54% of the catalog is (2,094 of 3,876
      rows), including 562 files that exist in NO other spelling. Matching only
      the canonical string would silently skip them while reporting a folder
      re-extract. `C:\\a\\b` is not a different folder; it is the same folder
      spelled badly, so both forms are in scope.
    """
    r = str(root or "").strip()
    if not r:
        return []
    if not r.endswith(("\\", "/")):
        r += "\\"

    def _esc(s):
        return (s.replace("\\", "\\\\").replace("%", "\\%")
                 .replace("_", "\\_").replace("[", "\\[")) + "%"

    out = [_esc(r)]
    doubled = r.replace("\\", "\\\\")          # the pre-canon_root spelling
    if doubled != r:
        out.append(_esc(doubled))
    return out


def _root_filter(root, alias="g"):
    """SQL fragment restricting a claim query to files under `root`, or ''.

    The path is embedded as a literal (single quotes doubled) rather than bound,
    to match _ext_filter and keep these filters composable inside the f-string
    claim queries. Callers pass an ALREADY-CANONICAL root — see
    path_identity.canon_root.
    """
    pred = _root_predicate(root, alias)
    return f"\n               AND {pred}" if pred else ""


def _root_predicate(root, alias="g"):
    r"""Just the bracketed OR of LIKEs — no leading AND, or '' for no root.

    Split out from _root_filter so a caller that AND-joins a list of predicates
    (_unprocessed_count) can use the same one clause, rather than string-surgery
    on the fragment or a second spelling that drifts. Two spellings of 'pending'
    already cost this file six different answers to one question.
    """
    likes = _root_likes(root)
    if not likes:
        return ""
    # alias='' -> bare column, the same convention pending_sql uses. Without
    # this an unaliased claim query (extract's) would build '.FILE_PATH'.
    col = (alias + "." if alias else "") + "FILE_PATH"
    ors = " OR ".join(
        f"{col} LIKE '{p.replace(chr(39), chr(39) * 2)}' ESCAPE '\\'"
        for p in likes)
    return f"({ors})"


def _already_done_filter(force=False, alias="g", root=None):
    r"""The 'skip what is already done' clauses — or nothing, when forcing.

    A file is normally passed over when it has been CATALOGED and its content
    hash is unchanged. That is right for a re-run over a big tree and wrong the
    moment the CODE changes: after the recogniser started replacing capture,
    1,638 LAS files were catalogued-and-unprocessed, and every re-run skipped
    them for being "already done" when nothing had ever done them. The only way
    back in was a hand-written DELETE against GLOBAL_FILE_CATALOG.

    'SKIPPED' SURVIVES A FORCE, DELIBERATELY. Forcing means "ignore that this
    was already processed", not "ignore that I told you to leave it alone" —
    those are different instructions and only one of them is the operator's.

    FORCING IS SCOPED TO THE SCAN ROOT, AND THE SCOPE LIVES HERE.
    ------------------------------------------------------------
    Dropping the CATALOGED/hash gate is what makes a forced run enormous: with
    it gone the WHERE clause no longer narrows anything, so every claim query
    selects the WHOLE catalog — every tree ever scanned, not the folder in the
    Scan root box. On a mixed catalog that is the difference between re-parsing
    the folder you just fixed and re-parsing everything anybody ever pointed
    the app at.

    The root clause is emitted by THIS function, next to the clause it bounds,
    rather than added at each call site — a fourth claim query that forgets it
    would silently re-acquire the old behaviour. A blank root (a catalog-wide
    run with no folder given) yields no clause, so a run still means the whole
    catalog when that is genuinely what was asked.

    BOTH PATHS ARE NOW SCOPED, and that is a deliberate reversal.
    ------------------------------------------------------------
    This applied the root clause only when forcing. The reasoning was that the
    normal path is already bounded by CATALOGED + hash, so scoping it "would
    change what a plain re-run processes, which is a different decision".
    It IS a different decision, and it has now been taken: a plain run pointed
    at a folder processed pending files from every other tree ever scanned,
    because CATALOGED + hash bounds by STATE, never by PLACE. Moving files out
    of a folder and rescanning it still processed them, from their old rows.
    The caller chooses with scope= ('path', the default, or 'queue'); by the
    time root arrives here, None already means "the whole queue was asked for".
    """
    if force:
        return (f"AND ISNULL({alias}.CATALOG_READINESS, '') NOT IN ('SKIPPED')"
                + _root_filter(root, alias))
    # THE definition of capture-pending, imported rather than re-spelled —
    # the same fragment the run gate and every report use.
    from dataview.file_catalog.promotion_lineage import pending_sql
    return "AND " + pending_sql("capture", alias) + _root_filter(root, alias)


def _stage_capture(engine, dialect, log, exts=None, workers=8, parallel=False,
                   force=False, root=None):
    """Parse catalogued documents (PDF surveys/scout + shapefiles) into the
    file_catalog.cat_* mirrors — the SAME _do_extract + _load_rows_to_catalog
    path the ④ 'Load checked to catalog' button drives, run headlessly over
    every catalogued file that has a resolved UWI (the grid's 'Select ALL with
    a UWI' rule: GLOBAL_FILE_CATALOG.MATCHED_UWI non-empty). Idempotent per
    file — capture() replaces this file's rows scoped to INVENTORY_ID — so a
    re-run refreshes rather than duplicates.

    Scope note: this covers exactly what the manual button covers — PDF,
    shapefile, and Office (dispatched through dv_office_loader, which parses
    the file itself). WITSML / OSDU join once their loaders are wired into
    _load_rows_to_catalog and added to SELF_PARSING_EXTS. Binary LAS/DLIS/SEG-Y
    are left to the deep stage."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    # extract_core owns the extract+capture helpers. It is streamlit-free by
    # design, so this no longer risks pulling the UI into the CLI or a pool
    # child — which is exactly why these symbols were moved out of
    # page_workbench on 16 Aug 2026.
    try:
        from dataview.file_catalog.extract_core import (_do_extract, _load_rows_to_catalog,
                                     SELF_PARSING_EXTS)
    except Exception as e:
        log(f"[capture] skipped — extract_core helpers unavailable: {e}")
        return {"capture_files": 0, "capture_rows": 0, "capture_ok": 0}
    try:
        from dataview.file_catalog.catalog_capture import reset_replace_state
    except Exception:
        try:
            from dataview.file_catalog.catalog_capture import reset_replace_state
        except Exception:
            reset_replace_state = lambda: None

    from sqlalchemy import text as _t
    # Self-parsing files (LAS/PDF/Office/WITSML/JSON) resolve their own UWI from
    # the file's own header/content, so they must NOT be gated on a pre-resolved
    # MATCHED_UWI — that gate is what made the capture stage skip every LAS file
    # (all had blank MATCHED_UWI from triage) and produce 0 log curves. Non-self-
    # parsing files (rows come from _do_extract / triage) still require a UWI.
    _sp = sorted({e.lower().lstrip(".") for e in SELF_PARSING_EXTS})
    _sp_in = ",".join(f"'.{e}'" for e in _sp) or "''"
    with engine.begin() as _con:                    # incremental-capture flag
        _con.execute(_t("IF COL_LENGTH('file_catalog.GLOBAL_FILE_CATALOG',"
                        "'CAPTURED_HASH') IS NULL ALTER TABLE "
                        "file_catalog.GLOBAL_FILE_CATALOG ADD CAPTURED_HASH NVARCHAR(40) NULL"))
    with engine.connect() as con:
        files = con.execute(_t(f"""
            SELECT g.FILE_PATH, g.FILE_EXT, g.MATCHED_UWI, g.FILE_NAME,
                   g.INVENTORY_ID
              FROM file_catalog.GLOBAL_FILE_CATALOG g
             WHERE (
                       LOWER(g.FILE_EXT) IN ({_sp_in})          -- self-parsing: UWI from content
                       OR (g.MATCHED_UWI IS NOT NULL
                           AND LTRIM(RTRIM(g.MATCHED_UWI)) <> '')
                   )
               AND ISNULL(g.FLAG_DELETE, 'N') <> 'Y'
               AND g.DUPLICATE_GROUP IS NULL
               {_already_done_filter(force, root=root)}
               {_ext_filter(exts, "g.FILE_EXT")}
             ORDER BY g.CATALOG_SCORE DESC, g.FILE_NAME
        """)).fetchall()

    if force:
        log(f"[capture] force ON — scope: "
            + (f"files under {root}" if _root_likes(root)
               else "the WHOLE catalog (no scan root given)"))

    total = len(files)
    _cap_invs = []                                   # stamp only real captures
    _sel_invs = [r[4] for r in files if r[4] is not None]   # candidates this run
    log(f"[capture] {total:,} document(s) with a UWI → cat_* mirrors …")
    ok = rows_total = 0

    # Files claimed by capture that are no longer on disk. Collected rather than
    # written per file so the whole set goes back in one executemany, and marked
    # AFTER the loops so a mid-run failure cannot leave half of them stamped.
    _missing_caps = []

    def _capture_one(fpath, fext, uwi, fname, iid=None):
        nonlocal ok, rows_total
        # MOVED/DELETED. Without this the parser raises, the exception handler
        # below logs '[x] name: ...' and writes NO state — so the row stays
        # capture-pending and is re-claimed, re-opened and re-failed on every
        # run, indefinitely. Recorded here and held as 'M' after the loop.
        if not os.path.exists(fpath):
            if iid is not None:
                _missing_caps.append({"id": iid,
                                      "e": "no file at the catalogued path"})
            return
        reset_replace_state()          # idempotent re-capture, scoped per file
        try:
            rows, _label = _do_extract(fpath, fext)
            if not rows and fext not in SELF_PARSING_EXTS:
                return
            res = _load_rows_to_catalog(engine, dialect, fpath, fext, uwi,
                                        rows or [])
            real_errs = [e for e in res.get("errors", [])
                         if not str(e).startswith("header capture:")]
            n = res.get("loaded", 0)
            rows_total += n
            if real_errs:
                log(f"  [x] {fname}: {str(real_errs[0])[:400]}")
            elif n or res.get("note") == "shapefile":
                ok += 1
        except Exception as e:
            log(f"  [x] {fname}: {str(e)[:400]}")

    _did_parallel = False
    if parallel and total > 1:
        # Multi-core capture: parse + write each file in a streamlit-free child
        # process (worker_core.process_file), the same path the monitor pool uses.
        # Only runs from the detached process (parse_mode='process'); the in-app
        # thread path skips this and uses the sequential loop below.
        from concurrent.futures import ProcessPoolExecutor
        try:
            _url = engine.url.render_as_string(hide_password=False)
        except Exception:
            _url = str(engine.url)
        # Longest-processing-time-first: start the biggest files first so a slow
        # one (large shapefile / multi-page PDF) doesn't strand a worker at the
        # very end while the others idle. File size is a cheap cost proxy; with
        # only ~50 files across 6 workers, tail stragglers dominate the makespan.
        try:
            files = sorted(
                files,
                key=lambda r: (os.path.getsize(r[0]) if r[0] and os.path.exists(r[0]) else 0),
                reverse=True)
        except Exception:
            pass
        # fast path: LAS via bulk BCP capture; non-LAS stay on the pool below
        try:
            from dataview.file_catalog.bcp_capture import run_bcp_capture
        except Exception:
            try:
                from dataview.file_catalog.bcp_capture import run_bcp_capture
            except Exception:
                run_bcp_capture = None
        _las_rows = [r for r in files if str(r[1] or "").lower() == ".las"]
        _oth_rows = [r for r in files if str(r[1] or "").lower() != ".las"]
        if run_bcp_capture and _las_rows:
            import urllib.parse as _upq
            try:
                _odbc = _upq.unquote(engine.url.query.get("odbc_connect", "")) or None
            except Exception:
                _odbc = None
            _las_recs = [{"FILE_PATH": r[0],
                          "MATCHED_UWI": ("" if r[2] is None else str(r[2]).strip()),
                          "INVENTORY_ID": r[4]} for r in _las_rows]
            try:
                _bres = run_bcp_capture(_las_recs, conn_str=_odbc, workers=workers, log=log)
                _bn = sum(_bres.values())
                rows_total += _bn
                ok += len(_las_rows)
                log(f"[capture] LAS fast-path (BCP): {_bn:,} row(s) from {len(_las_rows):,} file(s)")
                # skip .las in extract: mark these files extracted (fast path wrote
                # FILE_WELL_HEADER) so they aren't re-processed by _stage_extract.
                try:
                    from sqlalchemy import text as _t2
                    # INVENTORY_ID is a hex/uuid string -> quote each id safely
                    _iids = [str(r[4]).replace("'", "''") for r in _las_rows if r[4] is not None]
                    with engine.begin() as _c2:
                        for _i in range(0, len(_iids), 1000):
                            _blk = ",".join("'" + x + "'" for x in _iids[_i:_i+1000])
                            if _blk:
                                _c2.execute(_t2(
                                    "UPDATE file_catalog.GLOBAL_FILE_CATALOG "
                                    "SET HEADER_EXTRACTED='Y', ROW_CHANGED_DATE=GETUTCDATE() "
                                    "WHERE INVENTORY_ID IN (" + _blk + ")"))
                except Exception as _me:
                    log(f"[capture] (mark-extracted skipped: {str(_me)[:80]})")
                files = _oth_rows
            except Exception as _e:
                log(f"[capture] BCP fast-path failed ({str(_e)[:120]}); LAS fall back to pool")

        # SEG-Y fast-path: header-only bulk capture (parallel parse -> one BULK
        # INSERT to FILE_SEIS_HEADER). segy_header reads only the file header +
        # a small trace-header geometry sample, never trace data, so it's fast
        # even on multi-GB files. Pulls .segy/.sgy/.seg out of the pool set.
        _segy_exts = (".segy", ".sgy", ".seg")
        _segy_rows = [r for r in files if str(r[1] or "").lower() in _segy_exts]
        if _segy_rows:
            try:
                from dataview.file_catalog.bcp_capture import run_bcp_capture_segy
            except Exception:
                try:
                    from dataview.file_catalog.bcp_capture import run_bcp_capture_segy
                except Exception:
                    run_bcp_capture_segy = None
            if run_bcp_capture_segy:
                import urllib.parse as _upq2
                try:
                    _odbc2 = _upq2.unquote(engine.url.query.get("odbc_connect", "")) or None
                except Exception:
                    _odbc2 = None
                _segy_recs = [{"FILE_PATH": r[0], "INVENTORY_ID": r[4]} for r in _segy_rows]
                try:
                    _sres = run_bcp_capture_segy(_segy_recs, conn_str=_odbc2,
                                                 workers=workers, log=log)
                    _sn = sum(_sres.values())
                    rows_total += _sn
                    ok += len(_segy_rows)
                    log(f"[capture] SEG-Y fast-path (BCP): {_sn:,} header(s) "
                        f"from {len(_segy_rows):,} file(s)")
                    try:
                        from sqlalchemy import text as _t3
                        _siid = [str(r[4]).replace("'", "''") for r in _segy_rows if r[4] is not None]
                        with engine.begin() as _c3:
                            for _j in range(0, len(_siid), 1000):
                                _blk3 = ",".join("'" + x + "'" for x in _siid[_j:_j+1000])
                                if _blk3:
                                    _c3.execute(_t3(
                                        "UPDATE file_catalog.GLOBAL_FILE_CATALOG "
                                        "SET HEADER_EXTRACTED='Y', ROW_CHANGED_DATE=GETUTCDATE() "
                                        "WHERE INVENTORY_ID IN (" + _blk3 + ")"))
                    except Exception as _me3:
                        log(f"[capture] (segy mark-extracted skipped: {str(_me3)[:80]})")
                    files = [r for r in files if str(r[1] or "").lower() not in _segy_exts]
                except Exception as _se:
                    log(f"[capture] SEG-Y fast-path failed ({str(_se)[:120]}); "
                        f"SEG-Y falls back to pool")

        _args = [(_url, {
                    "FILE_PATH":   r[0],
                    "FILE_EXT":    str(r[1] or "").lower(),
                    "MATCHED_UWI": ("" if r[2] is None else str(r[2]).strip()),
                    "UWI":         ("" if r[2] is None else str(r[2]).strip()),
                    "FILE_NAME":   r[3],
                    "INVENTORY_ID": r[4]})
                 for r in files]
        _proc_sum = 0.0
        _times = []
        if not _args:
            _did_parallel = True
            log("[capture] no non-LAS files for the pool — skipped")
        else:
            try:
                with ProcessPoolExecutor(max_workers=workers,
                                         initializer=_capture_pool_init,
                                         initargs=(_url,)) as pool:
                    for fname, status, nrows, err, ptime in pool.map(_capture_proc_one, _args):
                        _proc_sum += ptime or 0.0
                        _times.append((fname, ptime or 0.0))
                        if status == "error" and err:
                            log(f"  [x] {fname}: {str(err)[:400]}")
                        else:
                            rows_total += nrows
                            if status == "done" and nrows:
                                ok += 1
                _did_parallel = True
                log(f"[capture] multi-core parse+capture across {workers} core(s)")
                log(f"[capture-phase] worker process_file sum {_proc_sum:.1f}s / {workers}w "
                    f"→ ideal wall ~{_proc_sum / max(workers, 1):.1f}s")
                _times.sort(key=lambda x: -x[1])
                log("[capture-slow] " + " · ".join(
                    f"{os.path.basename(str(fn))} {pt:.1f}s" for fn, pt in _times[:6]))
            except Exception as e:
                log(f"[capture] process pool failed ({str(e)[:120]}); "
                    f"falling back to single-core")

    if not _did_parallel:
        for r in files:
            _muwi = "" if r[2] is None else str(r[2]).strip()
            _capture_one(r[0], str(r[1] or "").lower(), _muwi, r[3],
                         iid=(r[4] if len(r) > 4 else None))

    # ── pass 2: OSDU master records (Field / Reservoir) have no well UWI, so
    # the UWI-gated pass above skips them. Pick up catalogued .json files with
    # no MATCHED_UWI and let the JSON loader self-route by `kind` — well-domain
    # JSON that failed UWI resolution comes back no_target/error harmlessly,
    # Field/Reservoir land in cat_field / cat_reservoir.
    with engine.connect() as con:
        # f-STRING, and it was not one before. The filter below is an
        # interpolation; in a plain string it is inserted as the literal
        # characters "{_already_done_filter(force)}", which SQL Server rejects
        # with a bare "syntax error, permission violation, or other
        # nonspecific error" — a message that names nothing and sends you
        # looking at permissions. The other two sites were already f-strings,
        # so the same edit worked there and failed only here.
        masters = con.execute(_t(f"""
            SELECT g.FILE_PATH, g.FILE_EXT, g.FILE_NAME, g.INVENTORY_ID
              FROM file_catalog.GLOBAL_FILE_CATALOG g
             WHERE g.FILE_EXT = '.json'
               AND (g.MATCHED_UWI IS NULL OR LTRIM(RTRIM(g.MATCHED_UWI)) = '')
               AND ISNULL(g.FLAG_DELETE, 'N') <> 'Y'
               AND g.DUPLICATE_GROUP IS NULL
               {_already_done_filter(force, root=root)}
        """)).fetchall()
    if masters:
        log(f"[capture] {len(masters):,} master JSON (no UWI) → field/reservoir …")
        for m in masters:
            _capture_one(m[0], str(m[1] or "").lower(), "", m[2],
                         iid=(m[3] if len(m) > 3 else None))
            if len(m) > 3 and m[3] is not None:
                _cap_invs.append(m[3])

    # populate _cap_invs from real cat_well rows (scoped to this run's candidates),
    # so only files that actually captured get their CAPTURED_HASH stamped. Files the
    # fast-path skipped (unresolved UWI) stay eligible for the next run.
    try:
        from sqlalchemy import text as _t_cap
        _cand = [str(x) for x in set(_sel_invs) if x is not None]
        if _cand:
            with engine.begin() as _cc:
                _cc.execute(_t_cap("IF OBJECT_ID('tempdb..#cap_real') IS NOT NULL DROP TABLE #cap_real"))
                _cc.execute(_t_cap("CREATE TABLE #cap_real (inv nvarchar(64) PRIMARY KEY)"))
                _rw = _cc.connection; _cu = _rw.cursor(); _cu.fast_executemany = True
                for _k in range(0, len(_cand), 1000):
                    _cu.executemany("INSERT INTO #cap_real (inv) VALUES (?)",
                                    [(v,) for v in _cand[_k:_k+1000]])
                _rows_real = _cc.execute(_t_cap(
                    "SELECT c.inv FROM #cap_real c WHERE EXISTS "
                    "(SELECT 1 FROM file_catalog.cat_well w WHERE w.INVENTORY_ID = c.inv)")).fetchall()
                _cc.execute(_t_cap("DROP TABLE #cap_real"))
            _cap_invs = list({r[0] for r in _rows_real}) + list(_cap_invs)
    except Exception as _e_cap:
        pass

    # Fingerprint skip: record the hash we captured at so an unchanged
    # re-run of the same file skips capture entirely (SELECTs gate on it).
    if _cap_invs:
        try:
            _ids = [str(x) for x in set(_cap_invs) if x is not None]
            with engine.begin() as _con:
                # temp-table join instead of a giant IN-list (ODBC caps params ~2100,
                # and captures run into the thousands -> 07002). Batched insert + join.
                _con.execute(_t("IF OBJECT_ID('tempdb..#cap_ids') IS NOT NULL "
                                "DROP TABLE #cap_ids"))
                _con.execute(_t("CREATE TABLE #cap_ids (inv nvarchar(64) PRIMARY KEY)"))
                _raw = _con.connection
                _cur = _raw.cursor()
                _cur.fast_executemany = True
                for _i in range(0, len(_ids), 1000):
                    _cur.executemany("INSERT INTO #cap_ids (inv) VALUES (?)",
                                     [(v,) for v in _ids[_i:_i+1000]])
                _con.execute(_t(
                    "UPDATE g SET g.CAPTURED_HASH = g.FILE_HASH "
                    "FROM file_catalog.GLOBAL_FILE_CATALOG g "
                    "JOIN #cap_ids c ON c.inv = g.INVENTORY_ID"))
                _con.execute(_t("DROP TABLE #cap_ids"))
        except Exception as _e:
            log(f"[capture] fingerprint stamp skipped: {str(_e)[:160]}")
    # HOLD the files that were not on disk. One executemany after the loops, so
    # a mid-run failure cannot leave part of the set stamped. 'M' takes them out
    # of CAPTURE_PENDING (see promotion_lineage) — without it these rows are
    # re-claimed and re-failed on every run, forever, because the exception path
    # logs and writes nothing.
    _missing_caps_n = 0
    if _missing_caps:
        _uniq = {d["id"]: d for d in _missing_caps if d.get("id") is not None}
        if _uniq:
            try:
                with engine.begin() as _mc:
                    _mc.execute(_t(
                        "UPDATE file_catalog.GLOBAL_FILE_CATALOG "
                        "SET HEADER_EXTRACTED='M', CATALOG_ISSUES=:e, "
                        "ROW_CHANGED_DATE=GETUTCDATE() "
                        "WHERE INVENTORY_ID=:id"), list(_uniq.values()))
                _missing_caps_n = len(_uniq)
                log(f"[capture] {_missing_caps_n:,} file(s) HELD as missing — "
                    f"catalogued once but no longer at the recorded path. The "
                    f"catalog is stale, not the files. Re-scan where they live "
                    f"now, or clear the rows; they will not be retried.")
            except Exception as _me:
                # Reported, never swallowed: if the hold does not land, these
                # rows come back next run and the reason must be visible.
                log(f"[capture] could not hold {len(_uniq):,} missing file(s) "
                    f"({type(_me).__name__}: {_me}) — they will be retried.")
    grand = total + len(masters)
    log(f"[capture] captured {rows_total:,} row(s) from {ok:,}/{grand:,} file(s)")
    return {"capture_files": grand, "capture_rows": rows_total,
            "capture_ok": ok, "capture_missing": _missing_caps_n}


def _stage_recognise(engine, log, exts=None, pack="petroleum", apply=True, force=False,
                     workers=1, parse_mode="thread", batch_docs=100, root=None):
    """Capture cat_* rows by RECOGNISING tables, instead of classify+extract.

    Drop-in alternative to _stage_capture. Same file selection, same
    catalog_capture.capture() at the end, same CAPTURED_HASH stamp so a re-run
    is incremental — only the middle changes: docshape identifies a table by
    what its columns ARE, rather than a classifier choosing a per-format
    extractor that then hunts for section banners.

    SCOPE IS DELIBERATELY NARROW: .pdf, .docx and .xlsx only. LAS, DLIS, LIS
    and SEG-Y already work through the deep path, which loads real curve and
    trace data; the recogniser reads headers and metadata, so pointing it at
    those formats would trade working data for a description of it.

    Runs INSTEAD of capture, not alongside it. Both write to the same cat_
    tables and capture() replaces per INVENTORY_ID, so interleaving them in
    one run would make the result depend on ordering. To compare the two, run
    each separately and read cat_* by source ('SHAPE' vs 'CATALOG') — the
    source column survives in cat_, though promote relabels it on the way up.
    """
    from sqlalchemy import text as _t
    try:
        from dataview.file_catalog import shape_loader as _sl
    except Exception as e:
        log(f"[recognise] skipped — shape_loader unavailable: {e}")
        return {"capture_files": 0, "capture_rows": 0, "capture_ok": 0}
    try:
        from docshape.readers import TABLE_EXTS
    except Exception as e:
        log(f"[recognise] skipped — docshape unavailable: {e}")
        return {"capture_files": 0, "capture_rows": 0, "capture_ok": 0}

    with engine.begin() as _con:
        _con.execute(_t("IF COL_LENGTH('file_catalog.GLOBAL_FILE_CATALOG',"
                        "'CAPTURED_HASH') IS NULL ALTER TABLE "
                        "file_catalog.GLOBAL_FILE_CATALOG ADD CAPTURED_HASH NVARCHAR(40) NULL"))

    _in = ",".join(f"'{e}'" for e in sorted(TABLE_EXTS))
    with engine.connect() as con:
        files = con.execute(_t(f"""
            SELECT g.FILE_PATH, g.FILE_EXT, g.INVENTORY_ID
              FROM file_catalog.GLOBAL_FILE_CATALOG g
             WHERE LOWER(g.FILE_EXT) IN ({_in})
               AND ISNULL(g.FLAG_DELETE, 'N') <> 'Y'
               AND g.DUPLICATE_GROUP IS NULL
               {_already_done_filter(force, root=root)}
               {_ext_filter(exts, "g.FILE_EXT")}
             ORDER BY g.FILE_NAME
        """)).fetchall()

    total = len(files)
    if force:
        log(f"[recognise] force ON — scope: "
            + (f"files under {root}" if _root_likes(root)
               else "the WHOLE catalog (no scan root given)"))
    log(f"[recognise] {total:,} document(s) · pack '{pack}' · "
        f"{'APPLY' if apply else 'DRY RUN'} …")
    if not total:
        return {"capture_files": 0, "capture_rows": 0, "capture_ok": 0}

    # One pack + recogniser for the whole stage, not one per file.
    _pack, _rec = _sl._pack_and_recogniser(pack)
    ok = rows_total = 0
    done_ids = []

    # WORKERS PARSE, THE PARENT WRITES.
    #
    # 541 documents took ten minutes as a serial loop, almost all of it
    # inside pdfplumber, and a few thousand wells would be an overnight
    # run. shape_loader.parse_many does the reading and recognising across
    # a process pool — no engine crosses the boundary, only a path in and
    # ~2.5KB of plain dicts out — while every write stays here, in one
    # connection, in the order the rest of the stage expects.
    #
    # parse_many falls back to a serial generator when workers <= 1 or the
    # pool cannot start, so this path is safe on a host that cannot spawn.
    # HELD, NOT DROPPED. This was a plain filter — files that had moved were
    # silently removed from the parse list, so they were never parsed, never
    # counted, never reported, and never taken out of the queue: invisible work
    # that reappeared on every run. Partition instead, and mark the absent ones
    # 'M' with their reason, which is what takes them out of CAPTURE_PENDING.
    _paths, _gone = [], []
    for fp, _fe, _iv in files:
        if not fp:
            continue
        (_paths if os.path.exists(fp) else _gone).append((fp, _iv))
    _paths = [fp for fp, _iv in _paths]
    _recog_missing = 0
    if _gone:
        _rows_gone = [{"id": iv, "e": "no file at the catalogued path"}
                      for fp, iv in _gone if iv is not None]
        if _rows_gone:
            try:
                with engine.begin() as _mg:
                    _mg.execute(_t(
                        "UPDATE file_catalog.GLOBAL_FILE_CATALOG "
                        "SET HEADER_EXTRACTED='M', CATALOG_ISSUES=:e, "
                        "ROW_CHANGED_DATE=GETUTCDATE() "
                        "WHERE INVENTORY_ID=:id"), _rows_gone)
                _recog_missing = len(_rows_gone)
            except Exception as _mge:
                log(f"[recognise] could not hold {len(_rows_gone):,} missing "
                    f"file(s) ({type(_mge).__name__}: {_mge}) — they will be "
                    f"retried.")
        log(f"[recognise] {len(_gone):,} file(s) HELD as missing — catalogued "
            f"once but no longer at the recorded path. Re-scan where they live "
            f"now, or clear the rows.")
    _inv_by_path = {fp: iv for fp, _fe, iv in files}
    _pw = max(1, int(workers or 1)) if parse_mode == "process" else 1
    if _pw > 1:
        log(f"[recognise] parsing on {_pw} worker(s)")

    # WHERE THE TIME GOES. Parsing is timed in the worker and carried back
    # on the payload; every database step is timed in the parent. Printed
    # once at the end so a slow run says WHICH step was slow instead of
    # leaving it to be guessed at.
    _sl.reset_timings()
    # capture()'s own internals — delete vs insert vs the transaction —
    # because three rounds of reasoning about its 108ms failed and the
    # only reliable move left is to measure inside it.
    try:
        from dataview.file_catalog import catalog_capture as _cc
        _cc.reset_capture_timings()
    except Exception:
        _cc = None
    _parse_sec = 0.0
    # BATCHED WRITES. The probe measured a ~50ms fixed cost per capture
    # call and almost none per row, so 1,907 calls of five rows was the
    # wrong shape. Rows accumulate per target table across `batch_docs`
    # documents and go in with one delete and one insert per table.
    # Set batch_docs=1 to get the old call-per-document behaviour back.
    # DEGRADE, DON'T DIE. This stage and shape_loader are two files, and a
    # deploy that moves one without the other took out the whole capture
    # stage — 617 documents, nothing written, discovered only at the end of
    # a 283-second run. An optional optimisation must never be able to do
    # that: if CaptureBatch isn't there, say so once and write the old way.
    _batch = None
    if apply:
        if hasattr(_sl, "CaptureBatch"):
            _batch = _sl.CaptureBatch(engine, size=batch_docs, log=log)
        else:
            log("[recognise] shape_loader has no CaptureBatch — writing one "
                "document at a time (deploy the current shape_loader.py for "
                "the batched path)")

    i = 0
    for parsed in _sl.parse_many(_paths, pack_name=pack, workers=_pw, log=log):
        i += 1
        _parse_sec += float(parsed.get("parse_sec") or 0.0)
        fpath = parsed.get("path")
        if parsed.get("error"):
            log(f"[recognise] {os.path.basename(fpath or '?')}: "
                f"{str(parsed['error'])[:120]}")
            continue
        try:
            # The id came back with the file list — don't make shape_loader
            # look it up again for every document.
            # ── LET THE WARNINGS THROUGH ─────────────────────────────
            # This was `log=lambda *_a: None` — every message from
            # load_parsed discarded. The silencing was RIGHT in intent: 541
            # documents of per-row chatter is unreadable. But it also threw
            # away the two lines that say WHY a document produced nothing —
            # "!! <table> not found in file_catalog" and "~ <shape>: no
            # column for …" — so the stage reported "0 row(s)" with no
            # reason, while the SAME code called directly printed exactly
            # what it was doing.
            #
            # Filter instead of silence: pass the diagnostics, drop the
            # routine per-row lines. Prefixed with the file name, because at
            # this point in the stage nothing else says which document a
            # warning belongs to.
            _fname = os.path.basename(fpath or "?")
            def _keep(msg, _f=_fname):
                _m = str(msg)
                if "!!" in _m or "~" in _m:
                    log(f"[recognise] {_f}: {_m.strip()}")

            r = _sl.load_parsed(engine, parsed, pack=_pack, apply=apply,
                                log=_keep,
                                inventory_id=_inv_by_path.get(fpath),
                                batch=_batch)
        except Exception as e:
            log(f"[recognise] {os.path.basename(fpath or '?')}: "
                f"{type(e).__name__}: {str(e)[:120]}")
            continue
        n = r.get("captured", 0)
        rows_total += n
        if n:
            ok += 1
            inv = _inv_by_path.get(fpath)
            if inv is not None:
                done_ids.append(inv)
        if _batch is not None:
            _batch.end_document()
        if i % 25 == 0 or i == total:
            log(f"[recognise] {i}/{total} · {rows_total:,} row(s)")

    if _batch is not None:
        _batch.flush()
        log(f"[recognise] {_batch.flushes} batch flush(es) for {i} document(s)")
    _brk = _sl.format_timings(total_files=i)
    try:
        _inner = _cc.capture_timings() if _cc is not None else ""
    except Exception:
        _inner = ""
    if _inner:
        log(f"[recognise-capture] {_inner}")
    if _brk:
        log(f"[recognise] parse {_parse_sec:.1f}s (worker-seconds) · "
            f"write {_brk}")

    if apply and done_ids:
        try:
            with engine.begin() as _con:
                _con.execute(_t("IF OBJECT_ID('tempdb..#rec_ids') IS NOT NULL "
                                "DROP TABLE #rec_ids"))
                _con.execute(_t("CREATE TABLE #rec_ids (inv nvarchar(64) PRIMARY KEY)"))
                _cur = _con.connection.cursor()
                _cur.fast_executemany = True
                for _i in range(0, len(done_ids), 1000):
                    _cur.executemany("INSERT INTO #rec_ids (inv) VALUES (?)",
                                     [(v,) for v in done_ids[_i:_i + 1000]])
                _con.execute(_t(
                    "UPDATE g SET g.CAPTURED_HASH = g.FILE_HASH "
                    "FROM file_catalog.GLOBAL_FILE_CATALOG g "
                    "JOIN #rec_ids c ON c.inv = g.INVENTORY_ID"))
                _con.execute(_t("DROP TABLE #rec_ids"))
        except Exception as _e:
            log(f"[recognise] fingerprint stamp skipped: {str(_e)[:160]}")

    log(f"[recognise] captured {rows_total:,} row(s) from {ok:,}/{total:,} file(s)")
    return {"capture_files": total, "capture_rows": rows_total,
            "capture_ok": ok, "capture_missing": _recog_missing}


def _stage_vault(engine, schema, vault_root, mode, apply, log):
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dataview.file_catalog import vault_organizer as vo
    from collections import Counter
    import time as _vt
    _v = {}                             # per-phase timing (logging only)

    _t0 = _vt.monotonic()
    with engine.connect() as con:
        rows = vo.fetch_rows(con, schema, None, log=log)
    _v["fetch_rows"] = _vt.monotonic() - _t0

    # build_plan applies per-well consensus identity (one folder per well) and
    # carries sidecars — the same routing the CLI uses, so they can't drift.
    _t0 = _vt.monotonic()
    plan, carried = vo.build_plan(rows, os.path.join(vault_root, "curated"))
    _v["build_plan"] = _vt.monotonic() - _t0

    # map each catalogued source path -> INVENTORY_ID so we can stamp the files
    # we actually vault (sidecars aren't catalog entries, so they get skipped).
    path2inv = {r["file_path"]: r["inventory_id"]
                for r in rows if r.get("file_path") and r.get("inventory_id")}

    buckets = Counter(p[2].split("/")[0] for p in plan)
    detail = " · ".join(f"{b} {n:,}" for b, n in sorted(buckets.items()))
    placed = exists = failed = 0
    placed_invs = set()
    inv2vault = {}
    errs = []
    if apply:
        from concurrent.futures import ThreadPoolExecutor
        _workers = max(1, int(os.environ.get("VAULT_COPY_WORKERS", "8")))
        _t0 = _vt.monotonic()

        def _place_one(item):
            src, dst, _ = item
            try:
                return (src, dst, vo.place(src, dst, mode), None)
            except Exception as e:
                return (src, dst, None, f"{type(e).__name__}: {e}")

        # File copies / stat-exists checks are I/O-bound, so threads scale well
        # (the GIL is released during the OS copy). Placement destinations are
        # one-folder-per-file, so there's no cross-thread write contention; we
        # collect results and tally single-threaded below (no locks needed).
        if _workers > 1 and len(plan) > 1:
            with ThreadPoolExecutor(max_workers=_workers) as _ex:
                results = list(_ex.map(_place_one, plan))
        else:
            results = [_place_one(it) for it in plan]

        for src, dst, res, err in results:
            if err is not None:
                failed += 1
                if len(errs) < 6:
                    errs.append((src, err))
                continue
            if res == "ok":
                placed += 1
            elif res == "exists":
                exists += 1
            inv = path2inv.get(src)
            if inv and res in ("ok", "exists"):       # already-in-vault is vaulted
                placed_invs.add(inv)
                inv2vault[inv] = dst                  # the governed vault location
        _v["place_loop"] = _vt.monotonic() - _t0
        log(f"[vault] placed {placed:,} new · {exists:,} already vaulted · "
            f"{failed:,} failed  (plan {len(plan):,} {mode}, {_workers}w) — {detail}")
        for s, e in errs:
            log(f"[vault]   ✗ {os.path.basename(s)} — {e}")
        if failed > len(errs):
            log(f"[vault]   …and {failed - len(errs):,} more failures "
                f"(same cause shown above)")
        _t0 = _vt.monotonic()
        _stamp_catalog(engine, schema, "VAULTED_AT", placed_invs, log)
        _v["stamp_vaulted"] = _vt.monotonic() - _t0
        _t0 = _vt.monotonic()
        _stamp_vault_path(engine, schema, inv2vault, log)
        _v["stamp_path"] = _vt.monotonic() - _t0
        log("[vault-phase] " + " · ".join(f"{k} {x:.2f}s" for k, x in _v.items())
            + f"  (placed {placed}, exists {exists}, plan {len(plan)})")
    else:
        log(f"[vault] planned {len(plan):,} placements (dry-run) — {detail}")

    return {"vault_total": len(plan), "vault_buckets": dict(buckets),
            "vault_placed": placed, "vault_exists": exists,
            "vault_failed": failed, "vault_stamped": len(placed_invs)}


def _stage_enrich(engine, ref, apply, log, report_dir=None):
    """Resolve missing UWIs + fill blank attributes against the reference — the
    same enrich() the ① button calls. Writes well/seis header metadata only."""
    import sys, types, time as _tm
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dataview.file_catalog import enrich_file_headers as en
    # Centralize the enrich CSV under the report root (default C:\Bulk\reports)
    # instead of littering the working directory.
    _rep = None
    try:
        _rep = os.path.join(
            _reports_dir(report_dir),
            f"enrich_report_{_tm.strftime('%Y%m%d_%H%M%S')}.csv")
    except Exception:
        _rep = None        # fall back to enrich()'s default location
    a = types.SimpleNamespace(
        server="", database="", odbc_driver="", ref=ref, depth_tol=50.0,
        no_well=False, no_seis=False, no_reverse=True,   # reverse-capture off: slow WELL_MASTER scan, not needed
        dry_run=not apply, report=_rep, reverse_report=None)
    log(f"[enrich] {'apply' if apply else 'dry-run'} — resolve UWIs · fill attrs …")
    raw = engine.raw_connection()
    try:
        en.enrich(raw, a, log=lambda m: log("  " + str(m)))
        raw.commit() if apply else raw.rollback()
    finally:
        try:
            raw.close()
        except Exception:
            pass
    return {}


def _stage_deep(engine, header_only, log):
    """Parse binary LAS/DLIS/LIS/SEG-Y/P190 into las_catalog + the curve
    registry — same deep_catalog() the ⑤ button calls. Always writes detail."""
    import sys, types
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import deep_catalog as dc
    a = types.SimpleNamespace(server="", database="", odbc_driver="",
                              header_only=header_only)
    log(f"[deep] {'header-only' if header_only else 'full'} parse …")
    dc.deep_catalog(engine, a, log=lambda m: log("  " + str(m)))
    return {}


def _stage_promote(engine, apply, log):
    """Lift cat_* mirror rows up into dv_* in FK-safe order — same run_promote()
    the ⑧ button calls. Dry-run (counts only) unless apply=True."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dataview.file_catalog import promote_catalog as pc
    log(f"[promote] {'APPLY' if apply else 'dry-run'} — cat_* → dv_* …")
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        # UWIs about to be promoted — grab them BEFORE run_promote, because the
        # MOVE empties cat_well. Normalized to match dv_well's key so enrich can
        # scope to just these rows instead of re-scanning all of dv_well × gold.
        promoted_uwis = []
        if apply:
            cur.execute(
                "SELECT DISTINCT REPLACE(REPLACE(REPLACE(UWI,'-',''),' ',''),'/','') "
                "FROM file_catalog.cat_well WHERE PROMOTED = 0")
            promoted_uwis = [r[0] for r in cur.fetchall() if r[0]]
        import time as _pt
        _p0 = _pt.monotonic()
        pc.run_promote(cur, None, apply, log=lambda m: log("  " + str(m)))
        _p1 = _pt.monotonic()
        # Backfill dv_well NULLs from well_master_gold — same enrichment the
        # standalone `promote_catalog --apply` runs, but SCOPED to the UWIs we
        # just promoted (the unscoped sweep over all dv_well × gold was the bulk
        # of promote's time on small runs). Same transaction as the promote.
        if apply and promoted_uwis:
            try:
                pc.enrich_from_gold(cur, uwis=promoted_uwis,
                                    log=lambda m: log("  " + str(m)))
            except Exception as _ee:
                log(f"  [enrich] skipped: {str(_ee)[:160]}")
        _p2 = _pt.monotonic()
        raw.commit() if apply else raw.rollback()
        _p3 = _pt.monotonic()
        log(f"  [promote-parts] run_promote {_p1 - _p0:.1f}s · "
            f"gold-enrich {_p2 - _p1:.1f}s · commit {_p3 - _p2:.1f}s")
    finally:
        try:
            raw.close()
        except Exception:
            pass
    # THE STAMP LIVES IN run_promote NOW, not here.
    #
    # It was here first, and here is one of FOUR ways promote is reached --
    # page_monitor, page_triage and force_capture all call run_promote directly
    # and none of them stamped, so files promoted through a button kept being
    # re-selected and the loop carried on for everyone not using the pipeline
    # page. Putting it in the function they all share is the difference between
    # fixing a path and fixing the behaviour. Doing it in both places would run
    # the lineage scan twice per pass for no benefit.
    return {}


def _ensure_catalog_cols(engine):
    """Add the per-file VAULTED_AT / PROMOTED_AT stamps to GLOBAL_FILE_CATALOG if
    they aren't there yet. They're populated by the vault/promote stages so the
    inventory-vs-processed scorecard can show how far each file actually got."""
    from sqlalchemy import text as _t
    with engine.begin() as con:
        con.execute(_t(
            "IF COL_LENGTH('file_catalog.GLOBAL_FILE_CATALOG','VAULTED_AT') IS NULL "
            "ALTER TABLE file_catalog.GLOBAL_FILE_CATALOG ADD VAULTED_AT DATETIME2 NULL;"))
        con.execute(_t(
            "IF COL_LENGTH('file_catalog.GLOBAL_FILE_CATALOG','PROMOTED_AT') IS NULL "
            "ALTER TABLE file_catalog.GLOBAL_FILE_CATALOG ADD PROMOTED_AT DATETIME2 NULL;"))
        con.execute(_t(
            "IF COL_LENGTH('file_catalog.GLOBAL_FILE_CATALOG','VAULT_PATH') IS NULL "
            "ALTER TABLE file_catalog.GLOBAL_FILE_CATALOG ADD VAULT_PATH NVARCHAR(900) NULL;"))
    try:
        # Covering index for the inventory-vs-processed scorecard so its per-type
        # aggregate is an index-only scan, not a full table scan — keeps the live
        # read off the writer's back. Separate txn + guarded: never blocks the run.
        with engine.begin() as con:
            con.execute(_t(
                "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_GFC_scorecard' "
                "AND object_id=OBJECT_ID('file_catalog.GLOBAL_FILE_CATALOG')) "
                "CREATE NONCLUSTERED INDEX IX_GFC_scorecard "
                "ON file_catalog.GLOBAL_FILE_CATALOG (FILE_EXT) "
                "INCLUDE (HEADER_EXTRACTED, CATALOG_READINESS, VAULTED_AT, PROMOTED_AT);"))
    except Exception:
        pass


def _stamp_catalog(engine, schema, col, inv_ids, log=lambda m: None):
    """Set GLOBAL_FILE_CATALOG.<col> = now for the given INVENTORY_IDs (only where
    it isn't already set), chunked to keep the IN list sane. Records how far each
    file got through the pipeline. `col` is an internal constant, never user input."""
    ids = [i for i in inv_ids if i]
    if not ids:
        return 0
    n = 0
    with engine.begin() as con:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join(["?"] * len(chunk))
            r = con.exec_driver_sql(
                f"UPDATE {schema}.GLOBAL_FILE_CATALOG SET {col}=SYSUTCDATETIME() "
                f"WHERE {col} IS NULL AND INVENTORY_ID IN ({ph})", tuple(chunk))
            n += r.rowcount or 0
    if n:
        log(f"[stamp] {col}: marked {n:,} file(s).")
    return n


def _stamp_vault_path(engine, schema, inv2path, log=lambda m: None):
    """Persist each file's vault destination on GLOBAL_FILE_CATALOG.VAULT_PATH so
    consumers can open the governed vault copy instead of the original network
    path. Each INVENTORY_ID gets a distinct path, so this stages the pairs in a
    temp table and applies one set-based JOIN UPDATE — never a per-row loop."""
    items = [(i, p) for i, p in (inv2path or {}).items() if i and p]
    if not items:
        return 0
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("IF OBJECT_ID('tempdb..#vp') IS NOT NULL DROP TABLE #vp")
        cur.execute("CREATE TABLE #vp (inv NVARCHAR(128) PRIMARY KEY, vp NVARCHAR(900))")
        try:
            cur.fast_executemany = True
        except Exception:
            pass
        cur.executemany("INSERT INTO #vp (inv, vp) VALUES (?, ?)",
                        [(str(i), str(p)) for i, p in items])
        cur.execute(
            f"UPDATE g SET g.VAULT_PATH = v.vp "
            f"FROM {schema}.GLOBAL_FILE_CATALOG g "
            f"JOIN #vp v ON v.inv = g.INVENTORY_ID")
        n = cur.rowcount or 0
        raw.commit()
        if n:
            log(f"[vault] stamped VAULT_PATH on {n:,} file(s).")
        return n
    except Exception as e:
        try:
            raw.rollback()
        except Exception:
            pass
        log(f"[vault] VAULT_PATH stamp skipped: {str(e).splitlines()[0][:140]}")
        return 0
    finally:
        raw.close()


def _ensure_run_table(engine):
    from sqlalchemy import text as _t
    with engine.begin() as con:
        con.execute(_t("""
            IF OBJECT_ID('file_catalog.PIPELINE_RUN','U') IS NULL
            CREATE TABLE file_catalog.PIPELINE_RUN (
                RUN_ID NVARCHAR(40) PRIMARY KEY,
                ROOT_PATH NVARCHAR(900), STARTED_AT DATETIME2,
                FINISHED_AT DATETIME2, DURATION_SEC INT,
                FILES_SCANNED INT, FILES_NEW INT, FILES_CHANGED INT,
                EXTRACT_OK INT, EXTRACT_SKIP INT, EXTRACT_ERR INT,
                TIER_HIGH INT, TIER_REVIEW INT, TIER_LOW INT, TIER_REJECT INT,
                VAULT_TOTAL INT, VAULT_PLACED INT,
                VAULT_APPLIED BIT, REPORT_PATH NVARCHAR(900)
            );
        """))


def _write_run_row(engine, s):
    from sqlalchemy import text as _t
    with engine.begin() as con:
        con.execute(_t("""
            INSERT INTO file_catalog.PIPELINE_RUN (
                RUN_ID,ROOT_PATH,STARTED_AT,FINISHED_AT,DURATION_SEC,
                FILES_SCANNED,FILES_NEW,FILES_CHANGED,
                EXTRACT_OK,EXTRACT_SKIP,EXTRACT_ERR,
                TIER_HIGH,TIER_REVIEW,TIER_LOW,TIER_REJECT,
                VAULT_TOTAL,VAULT_PLACED,VAULT_APPLIED,REPORT_PATH
            ) VALUES (
                :rid,:root,:start,SYSUTCDATETIME(),:dur,
                :scan,:new,:chg,:eok,:esk,:eer,
                :high,:rev,:low,:rej,:vt,:vp,:va,:rep)
        """), {
            "rid": s["run_id"], "root": s["root"], "start": s["started"],
            "dur": int(s.get("duration_sec", 0)),
            "scan": s.get("scanned", 0), "new": s.get("new", 0),
            "chg": s.get("changed", 0),
            "eok": s.get("extract_ok", 0), "esk": s.get("extract_skip", 0),
            "eer": s.get("extract_err", 0),
            "high": s.get("tier_HIGH", 0), "rev": s.get("tier_REVIEW", 0),
            "low": s.get("tier_LOW", 0), "rej": s.get("tier_REJECT", 0),
            "vt": s.get("vault_total", 0), "vp": s.get("vault_placed", 0),
            "va": 1 if s.get("vault_apply") else 0,
            "rep": s.get("report_path", "")[:900],
        })


def _by_group(engine):
    from sqlalchemy import text as _t
    with engine.connect() as con:
        return {g: n for g, n in con.execute(_t("""
            SELECT FILE_TYPE_GROUP, COUNT(*)
            FROM file_catalog.GLOBAL_FILE_CATALOG
            WHERE HEADER_EXTRACTED='Y'
            GROUP BY FILE_TYPE_GROUP""")).fetchall()}


# ── orchestrator ──────────────────────────────────────────────────────────────
def run_pipeline(engine, root, exts=None, *, workers=8, schema="file_catalog",
                 do_scan=True,
                 do_enrich=True, enrich_apply=True,
                 do_capture=True, dialect="mssql", force=False,
                 recognise=False, pack="petroleum",
                 deep_rollup=False,
                 do_vault=True, vault_root=None, vault_apply=False,
                 vault_mode="copy", do_deep=False, deep_header_only=True,
                 do_promote=False, promote_apply=False,
                 max_files=None, inventory_only=False, stall_timeout=180,
                 per_type_cap=None, parse_mode="thread", single_pass=False,
                 should_abort=None, scope="path",
                 # Set by run_pipeline_batched, which resets ONCE before its
                 # loop. Without it every batch would re-queue the files the
                 # previous batch just did, and the loop would never end —
                 # the same non-termination the reset design exists to avoid.
                 force_reset_done=False,
                 # gold, not WELL_MASTER: every real caller (the workbench's
                 # _wb_ref(), pipeline_proc_runner, enrich_file_headers's own
                 # DEFAULT_REF) resolves against the gold master, and this
                 # signature was the one place still naming the 8.8M-row
                 # predecessor — so a direct run_pipeline() call enriched
                 # against a different table than the button did.
                 ref="WELL_REF.well_ref.well_master_gold", report_root=None,
                 do_report=True, log=print):
    t0 = time.monotonic()
    s = {
        "run_id": uuid.uuid4().hex.upper(),
        "root": root,
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "vault_apply": bool(vault_apply and vault_root),
        "vault_mode": vault_mode,
        "errors": {},
        "stage_times": {},
    }
    exts = exts or default_exts()
    if vault_root is None:
        vault_root = os.path.join(os.getcwd(), "vault")

    # The root every path-scoped stage is restricted to. Canonicalised once
    # here, not per stage: the scan writes FILE_PATH from a walk of the
    # canonical root, so a pasted `"C:\\a\\b"` has to be collapsed to `C:\a\b`
    # before it can prefix-match anything. Same helper the Scan root box uses,
    # from the streamlit-free module so the CLI and the detached child can
    # import it.
    #
    # SCOPE — this used to read `_canon(root) if force else None`, which tied
    # the path filter to FORCE. Only `scan` is scoped to the folder you give
    # it; every later stage claims from the whole catalog's pending queue, so
    # an ordinary run pointed at one folder still processed files anywhere.
    # The filter to prevent that was already built (_root_filter/_root_likes)
    # and simply never reached on a non-forced run.
    #
    #   scope="path"   restrict every stage to files under `root` (default —
    #                  it is what pointing a run at a folder already means)
    #   scope="queue"  the old behaviour: drain the whole pending inventory
    #                  regardless of root, for finishing work already scanned
    #
    # force is now orthogonal: it decides whether ALREADY-DONE files are
    # redone, not which files are in scope.
    from dataview.core.path_identity import canon_root as _canon
    if scope not in ("path", "queue"):
        raise ValueError(f"scope must be 'path' or 'queue', got {scope!r}")
    _scope_root = _canon(root) if scope == "path" else None
    s["scope"] = scope
    s["scope_root"] = _scope_root
    if scope == "path" and not _scope_root:
        log("[pipeline] scope=path with no scan root — nothing to restrict to, "
            "so every catalogued file is in scope. Pass a root, or scope='queue' "
            "to say you meant the whole queue.")
    elif scope == "queue":
        log("[pipeline] scope=queue — every pending file is in scope, including "
            f"files outside {root or 'the scan root'}.")

    from contextlib import contextmanager as _cm

    @_cm
    def _timed(name):
        """Record wall-clock seconds for a stage into s['stage_times'] and log it.
        Logs a start marker too, so if a stage hangs you can see which one you're
        stuck in. Accumulates if a stage name runs more than once."""
        log(f"[{name}] ▶ starting…")
        _ts = time.monotonic()
        try:
            yield
        finally:
            _dt = time.monotonic() - _ts
            s["stage_times"][name] = s["stage_times"].get(name, 0.0) + _dt
            log(f"[{name}] ✓ done in {_dt:.1f}s")

    _ensure_run_table(engine)
    _ensure_catalog_cols(engine)

    # 1) scan (abort the run if this fails — nothing downstream to do)
    if do_scan:
        with _timed("scan"):
            s.update(_stage_scan(engine, root, exts, log))
        if s.get("scanned", 0) == 0 and s.get("new", 0) == 0:
            log("[scan] catalog unchanged — continuing to triage/report anyway.")
    else:
        s["scanned"] = 0
        s["new"] = 0
        log("[scan] skipped — processing the existing catalog (no re-inventory).")

    # "New files only": how many files still need processing? scan sets
    # HEADER_EXTRACTED='N' for new or content-changed files (and leaves
    # already-processed ones at S/E). If none are pending, the pipeline is a
    # no-op beyond scan -> skip every stage so a re-run never re-touches a
    # file it has already seen.
    _pending = 1
    try:
        from sqlalchemy import text as _tt
        with engine.connect() as _c:
            # pending = files needing EXTRACTION *or* capture-eligible-but-not-yet-
            # captured (extracted='Y' but no cat_well row). Without the second half,
            # a re-run over an already-inventoried catalog counts 0 pending and the
            # guard skips capture, so nothing ever gets captured. (capture-pending)
            from dataview.file_catalog.promotion_lineage import pending_sql
            _pending = _c.execute(_tt(
                "SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG g "
                "WHERE " + pending_sql("any", "g")
            )).scalar() or 0
    except Exception:
        _pending = 1                 # any doubt -> process normally
    if _pending == 0 and not inventory_only:
        log(f"[pipeline] no new or changed files to process "
            f"({s.get('scanned',0):,} seen, all already handled) — skipping "
            f"extract / enrich / triage / capture / promote.")

    def _go():
        """May we run the next processing stage? No for an inventory-only run,
        when nothing new needs processing, or if the abort signal has fired."""
        if inventory_only:
            return False
        if _pending == 0:
            return False
        if should_abort and should_abort():
            return False
        return True

    if inventory_only:
        log("[inventory] scan-only run — processing stages skipped.")

    # 2) extract — or single-pass extract+capture when enabled (multi-core only)
    # The merged extract+capture fast route runs the OLD extractors, so it is
    # disabled when the recogniser is requested — otherwise the checkbox would
    # be silently ignored whenever single-pass happened to be on.
    _merged = bool(single_pass and parse_mode == "process" and do_capture
                   and not recognise)
    # FORCE: clear the done-flag once, then the ordinary pending path runs.
    # Placed here — after _scope_root is resolved, before the first stage
    # that claims on it — so the reset is bounded by exactly the scope the
    # run itself uses.
    if _go() and force and not force_reset_done:
        try:
            _force_reset_extract(engine, log, exts, _scope_root)
        except Exception as e:
            s["errors"]["force_reset"] = str(e)
            log(f"[force] re-queue FAILED: {e}")

    if _go():
        if _merged:
            with _timed("extract+capture"):
                try:
                    s.update(_stage_extract_capture(engine, workers, log,
                                                    exts=exts, do_capture=True,
                                                    root=_scope_root))
                except Exception as e:
                    s["errors"]["extract"] = str(e)
                    log(f"[extract+capture] FAILED: {e}")
        else:
            with _timed("extract"):
                try:
                    s.update(_stage_extract(engine, workers, log,
                                            max_files=max_files,
                                            stall_timeout=stall_timeout,
                                            exts=exts, per_type_cap=per_type_cap,
                                            parse_mode=parse_mode,
                                            root=_scope_root,
                                            should_abort=should_abort))
                except Exception as e:
                    s["errors"]["extract"] = str(e)
                    log(f"[extract] FAILED: {e}")

    # 2.5) enrich — resolve UWIs + fill attributes (metadata only)
    if _go() and do_enrich:
        with _timed("enrich"):
            try:
                _stage_enrich(engine, ref, enrich_apply, log,
                              report_dir=report_root)
            except Exception as e:
                s["errors"]["enrich"] = str(e)
                log(f"[enrich] FAILED: {e}")

    # 3) triage
    if _go():
        with _timed("triage"):
            try:
                s.update(_stage_triage(engine, ref, log))
            except Exception as e:
                s["errors"]["triage"] = str(e)
                log(f"[triage] FAILED: {e}")

    # 3.4) capture — parse catalogued documents into cat_* mirrors (gated;
    #       skipped when the single-pass extract+capture stage already ran)
    if _go() and do_capture and not _merged:
        with _timed("capture"):
            try:
                if recognise:
                    # ── THE BINARY LANE IS NOT PART OF THE EITHER/OR ────────
                    # _stage_recognise is .pdf/.docx/.xlsx BY DESIGN — its own
                    # docstring says LAS/DLIS/LIS/SEG-Y "already work through
                    # the deep path". That was true while the recogniser ran
                    # ALONGSIDE capture. Once it REPLACED capture (5 Aug,
                    # recognise defaulted ON), the LAS lane went with it:
                    # run_bcp_capture lives inside _stage_capture, so nothing
                    # claimed a .las file at all. MEASURED 10 Aug: 1,638 LAS
                    # files catalogued, 0 captured, every one reporting "no
                    # detail rows" — which was literally true, no stage ran.
                    #
                    # So run the binary lane FIRST and unconditionally,
                    # scoped to those extensions, then hand the documents to
                    # the recogniser. The two were always independent; only
                    # the DOCUMENT path was ever an either/or.
                    #
                    # parallel=True because the BCP peel lives in the process
                    # branch — the sequential path has no fast lane.
                    _bin_exts = {".las", ".segy", ".sgy", ".seg", ".p190"}
                    if exts:
                        _bin_exts &= {str(e).lower() for e in exts}
                    if _bin_exts:
                        try:
                            _binres = _stage_capture(engine, dialect, log,
                                                     exts=_bin_exts,
                                                     workers=workers,
                                                     parallel=True,
                                                     force=force,
                                                     root=_scope_root)
                            # ADD, never update() — the recogniser writes the
                            # same keys and would silently drop these counts.
                            for _k, _v in (_binres or {}).items():
                                if isinstance(_v, int):
                                    s[_k] = int(s.get(_k, 0) or 0) + _v
                        except Exception as _be:
                            s["errors"]["capture_binary"] = str(_be)
                            log(f"[capture] binary lane FAILED: {_be}")

                    # Same stage, different implementation. Everything before
                    # it (scan, inventory, enrich, triage) and after it
                    # (vault, deep, promote) is untouched.
                    # capture_missing is carried over the update() the same way
                    # the binary lane's counts are ADDED above: both lanes hold
                    # missing files, and a plain update() would drop whichever
                    # ran first — the precise failure the note above records.
                    _recres = _stage_recognise(engine, log, exts=exts,
                                               pack=pack, apply=True,
                                               workers=workers,
                                               parse_mode=parse_mode,
                                               force=force, root=_scope_root)
                    _prev_missing = int(s.get("capture_missing", 0) or 0)
                    s.update(_recres)
                    s["capture_missing"] = _prev_missing + int(
                        (_recres or {}).get("capture_missing", 0) or 0)
                else:
                    s.update(_stage_capture(engine, dialect, log, exts=exts,
                                            workers=workers,
                                            parallel=(parse_mode == "process"),
                                            force=force, root=_scope_root))
            except Exception as e:
                s["errors"]["capture"] = str(e)
                log(f"[capture] FAILED: {e}")

    # 3.5) deep catalog — binary detail into las_catalog (gated)
    if _go() and do_deep:
        with _timed("deep"):
            try:
                _stage_deep(engine, deep_header_only, log)
            except Exception as e:
                s["errors"]["deep"] = str(e)
                log(f"[deep] FAILED: {e}")

    # 4) vault (plan, or apply)
    if _go() and do_vault:
        with _timed("vault"):
            try:
                s.update(_stage_vault(engine, schema, vault_root, vault_mode,
                                      s["vault_apply"], log))
            except Exception as e:
                s["errors"]["vault"] = str(e)
                log(f"[vault] FAILED: {e}")

    # 5) promote — cat_* -> dv_* (dry-run unless promote_apply)
    if _go() and do_promote:
        with _timed("promote"):
            try:
                _stage_promote(engine, promote_apply, log)
            except Exception as e:
                s["errors"]["promote"] = str(e)
                log(f"[promote] FAILED: {e}")

    # 5) report
    #
    # THESE FOUR ROLLUPS ARE NOT A STAGE AND WERE NEVER TIMED, which is why
    # the stage breakdown never summed to the run: 269s of stages against a
    # 343s run, and 449 against 525 the run before — a steady ~75s that no
    # line accounted for, right at the end where the progress bar appears to
    # hang. _promotion_counts is the suspect: a per-extension "what landed in
    # dv_*" rollup joins every dv_ table back to GLOBAL_FILE_CATALOG on
    # INVENTORY_ID, and nothing indexes that column. It costs the same
    # whatever the rest of the run did, and it grows with the database.
    _t_roll = time.perf_counter()
    _roll = {}
    try:
        s["by_group"] = _by_group(engine)
    except Exception:
        s["by_group"] = {}
    _roll["by_group"] = time.perf_counter() - _t_roll

    _t = time.perf_counter()
    s.update(_seis_counts(engine))     # seismic rollup for the report section
    _roll["seis_counts"] = time.perf_counter() - _t

    _t = time.perf_counter()
    s.update(_log_curve_counts(engine))   # LAS/DLIS/LIS log-curve rollup
    _roll["log_curve_counts"] = time.perf_counter() - _t

    # A RUN REPORT SHOULD DESCRIBE THE RUN.
    #
    # _promotion_counts does not: it calls promotion_lineage.file_detail(),
    # which builds a row for EVERY file in the catalog showing whether it
    # extracted, captured and promoted — a description of the DATABASE, not
    # of this run. It ran unconditionally at the end of every pipeline, cost
    # the same whatever the run did, grew with the catalog, and fed exactly
    # ONE of the report's eleven sections ("Landed in dv_* — by extension").
    #
    # That information already exists in two better places, both on demand:
    # the Stage scorecard button (extract · capture · promote per file) and
    # the Database scorecard. Paying for it on every run, whether or not
    # anyone opens the markdown, is the wrong trade.
    #
    # Off by default. deep_rollup=True restores it for a run where the
    # report is the artefact you actually want.
    if deep_rollup:
        _t = time.perf_counter()
        s.update(_promotion_counts(engine))
        _roll["promotion_counts"] = time.perf_counter() - _t
    else:
        s["promotion_rollup_skipped"] = True

    log("[report-rollups] " + " · ".join(
        f"{k} {v:.1f}s" for k, v in sorted(_roll.items(), key=lambda kv: -kv[1]))
        + f"  (total {sum(_roll.values()):.1f}s)")
    s["duration_sec"] = time.monotonic() - t0

    if do_report:
        reports_dir = _reports_dir(
            report_root, fallback=os.path.join(vault_root, "_reports"))
        rep_path = os.path.join(
            reports_dir, f"run_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.md")
        with open(rep_path, "w", encoding="utf-8") as f:
            f.write(report_md(s))
        s["report_path"] = rep_path
        log(f"[report] {rep_path}")

        try:
            _write_run_row(engine, s)
        except Exception as e:
            log(f"[report] PIPELINE_RUN insert skipped: {e}")

    _bd = " · ".join(
        f"{k} {v:.0f}s"
        for k, v in sorted(s.get("stage_times", {}).items(),
                           key=lambda kv: -kv[1]))
    log(f"[done] {s['duration_sec']:.0f}s · "
        f"extracted {s.get('extract_ok',0):,} · "
        f"captured {s.get('capture_rows',0):,} · "
        f"review {s.get('tier_REVIEW',0)+s.get('tier_LOW',0):,} · "
        f"vault {s.get('vault_total',0):,}")
    if _bd:
        log(f"[done] stage breakdown — {_bd}")
    return s


def _engine(server, database, driver="ODBC Driver 17 for SQL Server"):
    from sqlalchemy import create_engine
    drv = driver.replace(" ", "+")
    return create_engine(
        f"mssql+pyodbc://@{server}/{database}"
        f"?driver={drv}&trusted_connection=yes",
        fast_executemany=True)


def _engine_spec(engine):
    """Extract clean (server, database, driver) from a LIVE mssql+pyodbc engine,
    handling both the ``odbc_connect=...`` and the keyword (``?driver=``) URL
    forms. The multi-core runner rebuilds the connection from these parts via
    _engine() — the same call the CLI uses successfully — instead of round-
    tripping engine.url.render_as_string(), which mangles the driver braces and
    the ``host\\instance`` backslash and yields IM002 in the child process."""
    url = engine.url
    q = dict(url.query or {})

    def _one(v):
        return v[0] if isinstance(v, (list, tuple)) else v

    server = database = driver = None
    odbc = _one(q.get("odbc_connect"))
    if odbc:
        import urllib.parse as _up
        raw = odbc
        if "DRIVER" not in raw.upper() and "%" in raw:
            raw = _up.unquote_plus(raw)   # safety net if still encoded
        for part in raw.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            ku, v = k.strip().upper(), v.strip()
            if ku == "SERVER":
                server = v
            elif ku == "DATABASE":
                database = v
            elif ku == "DRIVER":
                driver = v.strip("{}")
    if not server:
        host = url.host or ""
        server = f"{host},{url.port}" if url.port else host
    if not database:
        database = url.database or ""

    # db.py builds the engine as create_engine("mssql+pyodbc://", creator=_creator)
    # — the URL is a bare stub and the real DRIVER/SERVER/DATABASE live only in
    # the creator closure's ODBC string. Recover them from there when the URL
    # carried nothing (best-effort; guarded).
    if not server or not database:
        try:
            creator = getattr(engine.pool, "_creator", None)
            cells = getattr(creator, "__closure__", None) or []
            for cell in cells:
                val = cell.cell_contents
                if (isinstance(val, str) and "DRIVER=" in val.upper()
                        and "SERVER=" in val.upper()):
                    for part in val.split(";"):
                        if "=" not in part:
                            continue
                        k, v = part.split("=", 1)
                        ku, v = k.strip().upper(), v.strip()
                        if ku == "SERVER" and not server:
                            server = v
                        elif ku == "DATABASE" and not database:
                            database = v
                        elif ku == "DRIVER" and not driver:
                            driver = v.strip("{}")
                    break
        except Exception:
            pass

    if not driver:
        d = _one(q.get("driver"))
        driver = d.replace("+", " ") if d else "ODBC Driver 17 for SQL Server"
    return {"server": server, "database": database, "driver": driver}


def _force_reset_extract(engine, log, exts=None, root=None):
    r"""Put already-extracted files back in the extract queue. Returns the count.

    THIS IS WHAT --force IS. Not a second claim predicate: extract claims in
    chunks and re-queries between them, so a predicate that ignores
    HEADER_EXTRACTED never empties and the stage loops forever on the same
    files (measured: ok 14 -> 28 -> 42, no exit). Clearing the flag ONCE puts
    the files into the ordinary pending set, which drains as work completes.

    Everything downstream — the claim query, _unprocessed_count, the batch
    loop's termination test — then needs no knowledge of force at all, which is
    the point: one definition of extract-pending, still.

    SCOPED by root and exts exactly like the run itself, because dropping the
    done-flag catalog-wide would queue every tree ever scanned. 'S' (SKIPPED)
    and 'M' (MOVED) are left alone; see promotion_lineage.EXTRACT_FORCE_RESET.
    """
    from sqlalchemy import text as _t
    from dataview.file_catalog.promotion_lineage import pending_sql
    where = [pending_sql("extract-force-reset")]
    params = {}
    if root:
        where.append(_root_predicate(root, ""))
    if exts:
        ph = []
        for i, e in enumerate(sorted(exts)):
            k = f"e{i}"
            ph.append(f":{k}")
            params[k] = e if e.startswith(".") else "." + e
        where.append(f"LOWER(FILE_EXT) IN ({', '.join(ph)})")
    sql = ("UPDATE file_catalog.GLOBAL_FILE_CATALOG SET HEADER_EXTRACTED = NULL "
           "WHERE " + " AND ".join(where))
    with engine.begin() as con:
        n = con.execute(_t(sql), params).rowcount or 0
    log(f"[force] re-queued {n:,} already-extracted file(s) "
        f"{'under ' + str(root) if root else 'across the whole catalog'} "
        f"(SKIPPED / MOVED / duplicates left alone)")
    return n


def _unprocessed_count(engine, exts=None, root=None):
    """Files still awaiting extract — the same predicate _stage_extract selects
    on (pending, not skipped, not a duplicate). Drives batch-loop termination.
    The ext filter is best-effort; the no-progress guard is the real safety net.

    root MUST match the scope the batches actually run under. This gauge decides
    when the queue is clear, so counting files the batches cannot claim reports
    work remaining that no batch will ever do — the loop then runs until the
    no-progress guard trips and calls a completed run stuck."""
    from sqlalchemy import text as _t
    from dataview.file_catalog.promotion_lineage import pending_sql
    where = [pending_sql("extract")]      # the SAME predicate _stage_extract claims on
    params = {}
    if root:
        where.append(_root_predicate(root, ""))   # '' -> bare FILE_PATH
    if exts:
        ph = []
        for i, e in enumerate(sorted(exts)):
            k = f"e{i}"
            ph.append(f":{k}")
            params[k] = e if e.startswith(".") else "." + e
        where.append(f"LOWER(FILE_EXT) IN ({', '.join(ph)})")
    sql = ("SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG WITH (NOLOCK) "
           "WHERE " + " AND ".join(where))
    with engine.connect() as con:
        return int(con.execute(_t(sql), params).scalar() or 0)


def _catalog_total(engine):
    from sqlalchemy import text as _t
    try:
        with engine.connect() as con:
            return int(con.execute(_t(
                "SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG "
                "WITH (NOLOCK)")).scalar() or 0)
    except Exception:
        return 0


def run_pipeline_batched(engine, root, *, batch_size=1000, max_batches=None,
                         scan_first=True, exts=None, log=print,
                         should_abort=None, **kw):
    """Inventory once (optional), then process the catalog in batches of
    `batch_size` until the queue is clear, the stop-file fires, or a batch makes
    no progress. The filesystem is walked once (scan_first), then every batch is
    process-only (do_scan=False) so a huge corpus isn't re-walked each pass. Per
    batch reports are suppressed; one rollup report is written at the end."""
    t0 = time.monotonic()
    # These are driver-controlled — don't let a caller's value override them.
    for _k in ("do_scan", "max_files", "do_report", "inventory_only"):
        kw.pop(_k, None)

    # The queue gauge must be scoped exactly as the batches are, or the loop
    # counts work no batch can claim and stops on the no-progress guard while
    # reporting files "stuck". Same canonicalisation run_pipeline applies.
    from dataview.core.path_identity import canon_root as _canon
    _batch_root = _canon(root) if kw.get("scope", "path") == "path" else None

    # FORCE, ONCE, BEFORE THE LOOP. Every batch calls run_pipeline, so a
    # per-batch reset would re-queue the files the previous batch just
    # finished and the loop could never drain. Reset here, then tell the
    # batches it is done. Uses _batch_root so the reset is bounded by the
    # SAME scope the gauge and the batches use — a reset that reached wider
    # would queue work no batch will claim, and the no-progress guard would
    # call the finished run stuck.
    if kw.get("force"):
        _force_reset_extract(engine, log, exts, _batch_root)
        kw["force_reset_done"] = True

    if scan_first:
        log(f"[batch] inventory pass — scanning {root}")
        run_pipeline(engine, root, exts=exts, do_scan=True, inventory_only=True,
                     do_report=False, should_abort=should_abort, log=log, **kw)

    agg = {k: 0 for k in ("extract_ok", "extract_err", "capture_rows",
                          "capture_files", "vault_total",
                          "tier_REVIEW", "tier_LOW", "tier_HIGH")}
    batches = 0
    last_remaining = None
    while True:
        if should_abort and should_abort():
            log("[batch] abort requested — stopping between batches")
            break
        remaining = _unprocessed_count(engine, exts, _batch_root)
        if remaining <= 0:
            log("[batch] queue clear — all files processed")
            break
        if last_remaining is not None and remaining >= last_remaining:
            log(f"[batch] no progress ({remaining:,} stuck) — stopping to "
                f"avoid a loop (check the Triage quarantine)")
            break
        if max_batches and batches >= max_batches:
            log(f"[batch] hit max-batches cap ({max_batches}) — "
                f"{remaining:,} file(s) left")
            break
        batches += 1
        log(f"[batch] {batches} · {remaining:,} file(s) remaining")
        s = run_pipeline(engine, root, exts=exts, do_scan=False,
                         max_files=batch_size, do_report=False,
                         should_abort=should_abort, log=log, **kw)
        for k in agg:
            agg[k] += int(s.get(k, 0) or 0)
        last_remaining = remaining

    # one rollup report for the whole batched run
    out = dict(agg)
    out.update({
        "root": root,
        "run_id": uuid.uuid4().hex.upper(),
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "duration_sec": time.monotonic() - t0,
        "batches": batches,
        "batch_size": batch_size,
        "scanned": _catalog_total(engine),
        "unprocessed_left": _unprocessed_count(engine, exts, _batch_root),
        "vault_apply": kw.get("vault_apply", False),
        "vault_mode": kw.get("vault_mode", "copy"),
    })
    try:
        out["by_group"] = _by_group(engine)
    except Exception:
        out["by_group"] = {}
    out.update(_seis_counts(engine))
    out.update(_log_curve_counts(engine))
    out.update(_promotion_counts(engine))
    reports_dir = _reports_dir(
        kw.get("report_root"),
        fallback=os.path.join(kw.get("vault_root") or ".", "_reports"))
    rep_path = os.path.join(
        reports_dir, f"batchrun_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.md")
    try:
        with open(rep_path, "w", encoding="utf-8") as f:
            f.write(report_md(out))
        out["report_path"] = rep_path
        log(f"[report] {rep_path}")
    except Exception as e:
        log(f"[report] batch report skipped: {e}")
    log(f"[batch-done] {batches} batch(es) · extracted {agg['extract_ok']:,} · "
        f"captured {agg['capture_rows']:,} · vault {agg['vault_total']:,} · "
        f"{out['duration_sec']:.0f}s · {out['unprocessed_left']:,} left")
    return out


def main():
    # UTF-8 stdout/stderr so the pipeline log glyphs survive a redirect to
    # a file (Windows Python defaults to cp1252, which cannot encode them).
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Scan→extract→triage→vault→report")
    ap.add_argument("--root", required=True, help="share / folder to crawl")
    ap.add_argument("--server", default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--schema", default="file_catalog")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--exts", default=None,
                    help="comma-separated extensions to process, e.g. "
                         ".las,.pdf (leading dot optional); default: all")
    ap.add_argument("--parse-mode", choices=["process", "thread"],
                    default="process",
                    help="extract parse parallelism. 'process' uses all cores "
                         "(true multi-core, safe in this standalone CLI); "
                         "'thread' is GIL-limited. Default process.")
    ap.add_argument("--single-pass", action="store_true",
                    help="EXPERIMENTAL: fold extract+capture into one pass so "
                         "each file is opened once (process mode only). Verify "
                         "cat_* parity before trusting on real data.")
    ap.add_argument("--per-type-cap", type=int, default=None,
                    help="TIMING TEST: process at most N pending files per "
                         "FILE_EXT in one sampling pass (e.g. 5 = 5 of each type)")
    ap.add_argument("--force", action="store_true",
                    help="re-do files already extracted/captured. SKIPPED ('S'), MOVED ('M') and duplicates are still left alone. Scoped to --root unless --scope queue.")
    ap.add_argument("--scope", choices=["path", "queue"], default="path",
                    help="path (default): process only files under ROOT. "
                         "queue: process the whole pending inventory, wherever "
                         "it lives — the old behaviour, where only the scan was "
                         "ever scoped to the folder you gave it.")
    ap.add_argument("--ref", default="WELL_REF.well_ref.well_master_gold")
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip the enrich (UWI resolve / attr fill) stage")
    ap.add_argument("--no-capture", action="store_true",
                    help="skip document-capture (PDF/shapefile -> cat_* mirrors)")
    ap.add_argument("--recognise", action="store_true",
                    help="use docshape table recognition for the capture "
                         "stage instead of the classifier + per-format "
                         "extractors")
    ap.add_argument("--pack", default="petroleum",
                    help="docshape vocabulary for --recognise")
    ap.add_argument("--no-vault", action="store_true")
    ap.add_argument("--vault-root", default=None)
    ap.add_argument("--report-root", default=None,
                    help="folder for run/enrich/inventory reports "
                         r"(default C:\Bulk\reports)")
    ap.add_argument("--vault-apply", action="store_true",
                    help="actually place files (default: plan only)")
    ap.add_argument("--vault-mode", choices=["copy", "symlink", "hardlink"],
                    default="copy")
    ap.add_argument("--deep", action="store_true",
                    help="run the deep binary catalog stage")
    ap.add_argument("--deep-full", action="store_true",
                    help="deep parse in full (default: header-only)")
    ap.add_argument("--promote", action="store_true",
                    help="promote cat_* -> dv_* (dry-run unless --promote-apply)")
    ap.add_argument("--promote-apply", action="store_true",
                    help="actually move rows up into dv_* (default: count only)")
    ap.add_argument("--batch-size", type=int, default=None,
                    help="inventory once, then process in batches of this many "
                         "files, looping until the queue is clear")
    ap.add_argument("--max-batches", type=int, default=None,
                    help="safety cap on number of batches (with --batch-size)")
    ap.add_argument("--no-scan-first", action="store_true",
                    help="with --batch-size, skip the initial inventory scan "
                         "(catalog is already populated)")
    a = ap.parse_args()
    _exts = None
    if a.exts:
        _exts = {'.' + e.strip().lower().lstrip('.')
                 for e in a.exts.split(',') if e.strip()} or None

    eng = _engine(a.server, a.database)
    _common = dict(
        workers=a.workers, schema=a.schema, parse_mode=a.parse_mode,
        single_pass=a.single_pass, scope=a.scope, force=a.force,
        do_enrich=not a.no_enrich, do_capture=not a.no_capture,
        recognise=a.recognise, pack=a.pack,
        do_vault=not a.no_vault, vault_root=a.vault_root,
        vault_apply=a.vault_apply, vault_mode=a.vault_mode, ref=a.ref,
        do_deep=a.deep, deep_header_only=not a.deep_full,
        do_promote=a.promote, promote_apply=a.promote_apply,
        report_root=a.report_root, per_type_cap=a.per_type_cap)
    if a.batch_size:
        run_pipeline_batched(eng, a.root, batch_size=a.batch_size,
                             max_batches=a.max_batches, exts=_exts,
                             scan_first=not a.no_scan_first, **_common)
    else:
        run_pipeline(eng, a.root, exts=_exts, **_common)


if __name__ == "__main__":
    main()
