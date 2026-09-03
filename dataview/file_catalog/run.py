r"""
run.py — headless pipeline with your defaults baked in.

  py run.py               # full run: scan -> extract -> enrich -> triage ->
                          #           capture -> promote (apply). Vault is its
                          #           own page now, so it's off here.
  py run.py --dry         # same, but promote is plan/count only (no dv_* writes)
  py run.py --watch 15    # loop every 15 minutes: re-scan, and thanks to the
                          #   new-files-only gate, do work ONLY when files changed
  py run.py --root D:\other\folder   # override the crawl folder for this run
  py run.py --exts .las,.pdf         # only these file types (empty = all)

Shells out to pipeline_run.py's CLI (the known-good headless entry point), so it
always uses the engine code on disk — no Streamlit, no module cache.
"""
import subprocess, sys, time

# ── your defaults ─────────────────────────────────────────────────────────────
ROOT        = r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\data_wrangler\training\test_crawl"
SERVER      = r"localhost\SQLEXPRESS"
DATABASE    = "DataView_Demo"
WORKERS     = 6
REPORT_ROOT = r"C:\Bulk\reports"
REF         = "WELL_REF.well_ref.well_master_public_v2"   # enrich/triage reference master

def _flag(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else default
    return default

def build_cmd(dry):
    cmd = [sys.executable, "pipeline_run.py",
           "--root", _flag("--root", ROOT),
           "--server", SERVER,
           "--database", DATABASE,
           "--workers", str(WORKERS),
           "--parse-mode", "process",      # multi-core (same as "use all cores")
           "--no-vault",                   # vault runs from its own page
           "--report-root", REPORT_ROOT,
           "--ref", _flag("--ref", REF),   # enrich/triage reference master
           "--promote"]                    # cat_* -> dv_*
    exts = _flag("--exts")                  # e.g. --exts .las,.pdf  (empty = all)
    if exts:
        cmd += ["--exts", exts]
    if not dry:
        cmd.append("--promote-apply")       # actually write dv_* (apply)
    return cmd

def run_once(dry):
    cmd = build_cmd(dry)
    print(">", " ".join(cmd))
    t0 = time.monotonic()
    rc = subprocess.run(cmd).returncode
    print(f"[run.py] {'DRY-RUN' if dry else 'APPLY'} finished in "
          f"{time.monotonic() - t0:.1f}s (exit {rc})")
    return rc

def main():
    dry = "--dry" in sys.argv
    watch = _flag("--watch")
    if watch:
        mins = float(watch)
        print(f"[run.py] watching every {mins:g} min — Ctrl+C to stop")
        while True:
            run_once(dry)
            time.sleep(mins * 60)
    else:
        sys.exit(run_once(dry))

if __name__ == "__main__":
    main()
