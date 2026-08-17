"""Detached pipeline runner — lets the Streamlit UI use TRUE multi-core parsing.

Spawning a ProcessPoolExecutor from inside a Streamlit worker thread is unsafe
on Windows: the OS uses 'spawn', which re-imports the parent's __main__ module —
and under `streamlit run app_v3.py` that module IS the Streamlit app, so every
spawned worker would re-execute the app. That's why the in-app thread path stays
GIL-bound on one core.

This module sidesteps the problem: the UI launches it as its OWN process
(`python pipeline_proc_runner.py <config.json>`). Here __main__ is THIS file,
which is import-safe (defines functions, no side effects), so the extract
stage's process pool can spawn freely and use every core — exactly like the CLI.

Contract with the UI:
  * argv[1] is a JSON config file (connection URL + all stage toggles).
  * Every pipeline log line is printed to stdout (the UI tails it live).
  * On completion the structured run state (stage_times + a few counts) is
    written to config['state_out'] so the UI's timing panel / scorecard work.
  * Graceful stop: if config['stop_file'] is set, the UI creates that file to
    request an abort; run_pipeline checks it at each stage boundary.
"""

import json
import os
import sys


def _abort_checker(stop_file):
    """should_abort hook: true once the UI drops the stop-file. Mirrors the
    in-app threading.Event so a UI 'Stop' ends the run at the next stage
    boundary (partial results already committed are kept)."""
    if not stop_file:
        return None
    return lambda: os.path.exists(stop_file)


def run(cfg: dict) -> dict:
    from dataview.import_data import pipeline_run as pr

    # Rebuild the EXACT same connection the UI uses. Prefer clean components
    # (server/database/driver) and the proven _engine() builder — the same call
    # the CLI makes — over a rendered URL string, which mangles the driver
    # braces and the host\instance backslash and fails with IM002 in this child.
    db = cfg.get("database")
    if db:
        eng = pr._engine(cfg.get("server") or r"PERRY\SQLEXPRESS", db,
                         cfg.get("driver") or "ODBC Driver 17 for SQL Server")
        print(f"[runner] target {cfg.get('server')}/{db} "
              f"· driver {cfg.get('driver')}", flush=True)
    else:
        from sqlalchemy import create_engine
        eng = create_engine(cfg["url"], fast_executemany=True)
        print("[runner] target (from url)", flush=True)

    exts = set(cfg.get("exts") or []) or None
    _common = dict(
        workers=int(cfg.get("workers") or 8),
        schema=cfg.get("schema", "file_catalog"),
        parse_mode="process",                       # the whole point
        do_enrich=bool(cfg.get("do_enrich", True)),
        do_capture=bool(cfg.get("do_capture", True)),
        # THE RECOGNISER STAGE. _common is spelled out key by key rather than
        # forwarded wholesale, so a new toggle in the UI reaches run_pipeline
        # ONLY if it is named here — which is exactly how the recognise flag
        # sat unused: complete in pipeline_run, offered by the CLI, and
        # dropped on the floor by the detached path the UI actually uses.
        # Default TRUE so a runner from an older UI still gets it; the page
        # sends the key explicitly either way. run_pipeline_batched takes
        # **kw and passes it through, so both paths are covered.
        recognise=bool(cfg.get("recognise", True)),
        pack=cfg.get("pack", "petroleum"),
        # FORCE RE-EXTRACT. Same reason as the note above: named here or it
        # never arrives. Without it the pipeline skips any file already
        # CATALOGED with an unchanged hash — correct on a re-run over a big
        # tree, and wrong the moment the CODE changes, which is how 1,638 LAS
        # files sat "already done" while nothing had ever processed them.
        force=bool(cfg.get("force", False)),
        # PATH SCOPE. Named here for the same reason as the two notes above.
        # 'path' restricts every stage to files under root; 'queue' drains the
        # whole pending inventory (the old behaviour, where only scan was
        # scoped to the folder given). Default 'path' so a runner launched by
        # an older UI gets the bounded behaviour rather than the surprising one.
        scope=cfg.get("scope", "path"),
        # The database-wide rollup is off by default; a caller that
        # actually wants the markdown report can ask for it.
        deep_rollup=bool(cfg.get("deep_rollup", False)),
        dialect=cfg.get("dialect", "mssql"),
        do_deep=bool(cfg.get("do_deep", False)),
        do_vault=bool(cfg.get("do_vault", False)),
        vault_root=cfg.get("vault_root"),
        vault_apply=bool(cfg.get("vault_apply", False)),
        vault_mode=cfg.get("vault_mode", "copy"),
        do_promote=bool(cfg.get("do_promote", False)),
        promote_apply=bool(cfg.get("promote_apply", False)),
        per_type_cap=cfg.get("per_type_cap"),
        single_pass=bool(cfg.get("single_pass", False)),
        stall_timeout=int(cfg.get("stall_timeout", 180)),
        should_abort=_abort_checker(cfg.get("stop_file")),
        ref=cfg.get("ref", "WELL_REF.well_ref.well_master_gold"),
        report_root=cfg.get("report_root"),
        log=lambda m: print(m, flush=True),
    )
    _bs = cfg.get("batch_size")
    if _bs:
        # Inventory once, then process in batches until the queue clears.
        state = pr.run_pipeline_batched(
            eng, cfg["root"], exts=exts,
            batch_size=int(_bs),
            max_batches=cfg.get("max_batches"),
            scan_first=bool(cfg.get("do_scan", True)),
            **_common,
        )
    else:
        state = pr.run_pipeline(
            eng, cfg["root"], exts=exts,
            do_scan=bool(cfg.get("do_scan", True)),
            inventory_only=bool(cfg.get("inventory_only", False)),
            max_files=cfg.get("max_files"),
            **_common,
        )
    return state or {}


def main():
    # The detached child's stdout/stderr default to the Windows locale codec
    # (cp1252), which can't encode the pipeline's ▶ … ✓ log glyphs and dies with
    # UnicodeEncodeError on the first banner. Force UTF-8 (replace as a backstop).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if len(sys.argv) < 2:
        print("[runner] usage: pipeline_proc_runner.py <config.json>", flush=True)
        return 2
    try:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"[runner] cannot read config: {e}", flush=True)
        return 2

    rc = 0
    state = {}
    try:
        state = run(cfg)
    except Exception as e:
        import traceback
        print(f"[runner] FAILED: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        rc = 1

    # Hand the structured state back to the UI (json-safe subset).
    out = {"stage_times": (state.get("stage_times") or {})}
    for k in ("scanned", "new", "changed", "unchanged", "dup_skipped",
              "extracted", "capture_rows", "capture_files", "capture_ok",
              "review", "vault", "duration_sec", "run_id"):
        if k in state:
            out[k] = state[k]
    so = cfg.get("state_out")
    if so:
        try:
            with open(so, "w", encoding="utf-8") as f:
                json.dump(out, f, default=str)
        except Exception as e:
            print(f"[runner] could not write state: {e}", flush=True)
    return rc


if __name__ == "__main__":
    # Guard is essential: spawned parse workers re-import this module as their
    # __main__. Because everything above is behind functions, that import is a
    # no-op — no app, no Streamlit, no recursion. freeze_support() keeps frozen
    # builds safe too.
    try:
        import multiprocessing as _mp
        _mp.freeze_support()
    except Exception:
        pass
    sys.exit(main())
