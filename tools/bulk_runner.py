"""
bulk_runner.py  --  Data Wrangler Headless Batch Runner
========================================================
Two modes:

  # Run queue once and exit:
  python tools/bulk_runner.py --db-server SERVER\INST --db-name PPDM39 --windows-auth

  # Watch folder continuously:
  python tools/bulk_runner.py --db-server SERVER\INST --db-name PPDM39 --windows-auth --watch
"""

import argparse
import json
import pathlib
import sys
import time
import urllib.parse
from datetime import datetime
import os

# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE_DIR     = pathlib.Path(__file__).parent
_QUEUE_FILE   = _BASE_DIR / "bulk_queue.json"
_HISTORY_FILE = _BASE_DIR / "bulk_history.json"
_WATCHER_FILE = _BASE_DIR / "bulk_watcher.json"


def _log(msg):
    print(f"[bulk_runner {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_queue():
    try:
        if _QUEUE_FILE.exists():
            return json.loads(_QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_queue(q):
    safe = [{k: v for k, v in j.items() if k != "last_result"} for j in q]
    _QUEUE_FILE.write_text(json.dumps(safe, indent=2), encoding="utf-8")


def _load_history():
    try:
        if _HISTORY_FILE.exists():
            return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_history(h):
    _HISTORY_FILE.write_text(json.dumps(h[-500:], indent=2), encoding="utf-8")


def _load_watcher_cfg():
    try:
        if _WATCHER_FILE.exists():
            return json.loads(_WATCHER_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"folder": "", "enabled": False, "pattern": "*.csv", "default_table": ""}


def _connect(args):
    from sqlalchemy import create_engine, text
    if args.windows_auth:
        cs = (f"DRIVER={{ODBC Driver 17 for SQL Server}};"
              f"SERVER={args.db_server};DATABASE={args.db_name};Trusted_Connection=yes;")
    else:
        cs = (f"DRIVER={{ODBC Driver 17 for SQL Server}};"
              f"SERVER={args.db_server};DATABASE={args.db_name};"
              f"UID={args.username};PWD={args.password};")
    engine = create_engine(
        "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(cs),
        fast_executemany=True)
    with engine.connect() as con:
        con.execute(text("SELECT 1"))
    return engine


def _load_schema():
    import pickle
    jpath = _BASE_DIR / "schema_registry" / "ppdm_39_schema_domain.json"
    ppath = jpath.with_suffix(".pkl")
    if ppath.exists():
        with open(ppath, "rb") as f:
            return pickle.load(f)
    if jpath.exists():
        import json as _j
        from dataview.core.schema import load_schema_from_dict
        with open(jpath, encoding="utf-8") as f:
            return load_schema_from_dict(_j.load(f))
    return None


def _detect_fingerprint(file_path, target_table, cache):
    import csv as _csv
    import hashlib
    from dataview.import_data.staging import _sanitize_col, _dedupe_cols
    from dataview.import_data.mapping import mapping_fingerprint
    try:
        with open(file_path, encoding="utf-8-sig", newline="") as f:
            sample = f.read(4096)
        first = sample.split('\n')[0]
        counts = {d: first.count(d) for d in ('|', '\t', ',', ';')}
        delim = next((d for d in ('|', '\t', ';', ',') if counts[d] > 0), ',')
        with open(file_path, encoding="utf-8-sig", newline="") as f:
            hdrs = next(_csv.reader(f, delimiter=delim))
        cols = _dedupe_cols([_sanitize_col(h.strip()) for h in hdrs])
        while cols and cols[-1] in ('', 'col'):
            cols.pop()
        for fp in (mapping_fingerprint(target_table, cols + ["_batch_loaded_at"]),
                   mapping_fingerprint(target_table, cols)):
            saved = cache.get(fp, {})
            n = sum(1 for v in saved.values()
                    if isinstance(v, dict) and v.get("source_col", "").strip())
            if n > 0:
                return fp, n
        fp0 = mapping_fingerprint(target_table, cols)
        return fp0, 0
    except Exception as e:
        _log(f"  Fingerprint error: {e}")
        return None, 0


def run_queue(engine, ppdm_schema):
    queue = _load_queue()
    ready = [j for j in queue if j.get("status") == "ready"]
    if not ready:
        return 0

    sys.path.insert(0, str(_BASE_DIR))
    from dataview.import_data.page_bulk import run_job

    history = _load_history()
    processed = 0
    _run_ref_missing = {}  # {table: {pk_cols, rows, ref_dir}} across all jobs this run

    for job in ready:
        _log(f"Running: {job['file_name']} -> {job['target_table']}")

        for j in queue:
            if j["id"] == job["id"]:
                j["status"] = "running"
        _save_queue(queue)

        def _cb(msg, pct):
            _log(f"  [{pct:>3}%] {msg}")

        result = run_job(job, engine, ppdm_schema, progress_cb=_cb)

        ok  = result.get("ok", False)
        ins = result.get("rows_inserted", 0)
        skp = result.get("rows_skipped", 0)
        dur = result.get("duration_s", 0)
        msg = result.get("message", "")

        icon = "OK" if ok else "FAIL"
        _log(f"  [{icon}] {job['file_name']} -> {job['target_table']} "
             f"| {ins:,} inserted, {skp:,} skipped, {dur}s")
        if msg:
            _log(f"  {msg}")

        status = "done" if ok else "failed"
        for j in queue:
            if j["id"] == job["id"]:
                j["status"] = status
        _save_queue(queue)

        fk_seeded         = result.get("fk_seeded", [])
        fk_seed_err       = result.get("fk_seed_err", "")
        ref_tables_needed = result.get("ref_tables_needed", [])
        if fk_seeded:
            _log(f"  🌱 FK seeded: {' · '.join(fk_seeded)}")
        if fk_seed_err:
            _log(f"  ⚠️  FK seed error: {fk_seed_err[:200]}")
        if ref_tables_needed:
            _log(f"  ⛔ Reference table(s) not seeded: {', '.join(ref_tables_needed)}")
            _log(f"  ➡  Open the interactive app → Stage 6 → RTM to seed these tables, then re-run.")

        # Accumulate missing ref values across all jobs for consolidated CSV write
        for _rm_tbl, _rm_info in result.get("ref_missing", {}).items():
            if _rm_tbl not in _run_ref_missing:
                _run_ref_missing[_rm_tbl] = {
                    "pk_cols": _rm_info["pk_cols"],
                    "fields":  _rm_info["fields"],
                    "rows":    set(),
                    "ref_dir": _rm_info.get("ref_dir", ""),
                }
            _run_ref_missing[_rm_tbl]["rows"].update(_rm_info["rows"])



        history.append({
            "id":            job["id"],
            "file_name":     job["file_name"],
            "target_table":  job["target_table"],
            "status":        status,
            "message":       msg,
            "rows_inserted": ins,
            "rows_skipped":  skp,
            "duration_s":    dur,
            "completed":     datetime.now().isoformat()[:19],
            "fk_seeded":     fk_seeded,
            "fk_seed_err":   fk_seed_err,
        })
        _save_history(history)
        processed += 1

    # Write consolidated deduped reference missing CSVs for the full run
    if _run_ref_missing:
        import csv as _rmc, pathlib as _rmp
        from datetime import datetime as _rmdt
        _rm_ts = _rmdt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        for _rm_tbl, _rm_info in _run_ref_missing.items():
            _rm_dir = _rmp.Path(_rm_info["ref_dir"]) if _rm_info.get("ref_dir") else None
            if not _rm_dir:
                continue
            try:
                _rm_dir.mkdir(parents=True, exist_ok=True)
                _rm_vf = _rm_dir / f"{_rm_tbl}_missing.csv"
                _rm_rows = sorted(_rm_info["rows"])
                _rm_fields = _rm_info["fields"]
                _rm_pks = _rm_info["pk_cols"]
                with open(_rm_vf, "w", newline="", encoding="utf-8") as _rmf:
                    _rmw = _rmc.DictWriter(_rmf, fieldnames=_rm_fields)
                    _rmw.writeheader()
                    for _rmrow in _rm_rows:
                        _rmd = {_rm_pks[i]: _rmrow[i]
                                for i in range(min(len(_rm_pks), len(_rmrow)))}
                        _rmd["timestamp"] = _rm_ts
                        _rmw.writerow(_rmd)
                _log(f"  📋 {_rm_tbl}_missing.csv: {len(_rm_rows)} distinct value(s) → {_rm_vf}")
            except Exception as _rme:
                _log(f"  ⚠️  CSV write error {_rm_tbl}: {_rme}")

    # Auto-clear done/failed
    queue = [j for j in _load_queue() if j.get("status") not in ("done", "failed")]
    _save_queue(queue)

    return processed


def watch_loop(engine, ppdm_schema, poll=30):
    from dataview.import_data.mapping import _load_cache as _lc
    _log("Watcher started. Press Ctrl+C to stop.")
    _seen = {str(pathlib.Path(j.get("file_path","")).resolve())
             for j in _load_queue() + _load_history()
             if j.get("file_path")}

    while True:
        try:
            cfg     = _load_watcher_cfg()
            folder  = cfg.get("folder", "").strip()
            pattern = cfg.get("pattern", "*.csv").strip() or "*.csv"
            enabled = cfg.get("enabled", False)
            tbl     = cfg.get("default_table", "").strip()

            if not enabled:
                _log("Watcher disabled — sleeping.")
                time.sleep(poll)
                continue

            if not folder or not pathlib.Path(folder).exists():
                _log(f"Watch folder not found: {folder!r} — sleeping.")
                time.sleep(poll)
                continue

            cache = _lc()
            new_files = sorted(
                f for f in pathlib.Path(folder).glob(pattern)
                if str(f.resolve()) not in _seen
            )

            for f in new_files:
                _seen.add(str(f.resolve()))
                _log(f"New file: {f.name}")
                if not tbl:
                    _log("  Skipped — no default_table set in watcher config.")
                    continue
                fp, n = _detect_fingerprint(str(f), tbl, cache)
                status = "ready" if n > 0 else "no_mapping"
                queue = _load_queue()
                next_id = max((j["id"] for j in queue), default=0) + 1
                queue.append({
                    "id":           next_id,
                    "file_path":    str(f),
                    "file_name":    f.name,
                    "target_table": tbl,
                    "mode":         "insert",
                    "fingerprint":  fp or "",
                    "mapped_cols":  n,
                    "status":       status,
                    "added":        datetime.now().isoformat()[:19],
                })
                _save_queue(queue)
                _log(f"  Queued -> {tbl} ({status}, {n} mapped cols)")

            n_ran = run_queue(engine, ppdm_schema)
            if not new_files and n_ran == 0:
                _log(f"Idle — watching {folder!r}  (every {poll}s)")

        except KeyboardInterrupt:
            raise
        except Exception as e:
            _log(f"Watcher error: {e}")

        time.sleep(poll)


def main():
    ap = argparse.ArgumentParser(description="Data Wrangler headless batch runner")
    ap.add_argument("--db-server",    required=True)
    ap.add_argument("--db-name",      required=True)
    ap.add_argument("--windows-auth", action="store_true")
    ap.add_argument("--username",     default="")
    ap.add_argument("--password",     default="")
    ap.add_argument("--watch",        action="store_true",
                    help="Run as continuous file watcher")
    ap.add_argument("--poll",         type=int, default=30,
                    help="Watcher poll interval in seconds (default 30)")
    args = ap.parse_args()

    _log(f"Connecting to {args.db_server} / {args.db_name} ...")
    try:
        engine = _connect(args)
        _log("Connected.")
    except Exception as e:
        _log(f"Connection failed: {e}")
        sys.exit(1)

    _log("Loading PPDM schema...")
    try:
        ppdm_schema = _load_schema()
        _log("Schema loaded." if ppdm_schema else "Warning: schema not found.")
    except Exception as e:
        _log(f"Schema load error: {e}")
        ppdm_schema = None

    if args.watch:
        try:
            watch_loop(engine, ppdm_schema, poll=args.poll)
        except KeyboardInterrupt:
            _log("Stopped.")
    else:
        _log("Running queue once...")
        n = run_queue(engine, ppdm_schema)
        _log(f"Done — {n} job(s) processed.")


if __name__ == "__main__":
    main()
