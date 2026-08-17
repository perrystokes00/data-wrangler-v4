"""
run_stage.py — run ONE pipeline stage in a FRESH process.

A new interpreter each call = always the latest code on disk. No Streamlit, no
module cache, no restart. This is the clean iteration loop: patch a stage file,
run this, read the timing. Seconds, not a 90s pipeline.

  py run_stage.py enrich
  py run_stage.py vault  --apply
  py run_stage.py promote --apply
  py run_stage.py capture            # sequential (multi-core needs the detached pipeline)

--apply writes; default is dry-run where the stage supports it.
"""
import sys, os, time
from dataview.import_data import pipeline_run as pr


def main():
    """The whole script, which used to run on IMPORT.

    There was no `if __name__` guard at all: importing this module ran
    a pipeline stage against the live database, and its usage-error
    sys.exit(2) killed the importing process. A tool that DOES things
    belongs behind a guard — anything that walks the package (a test
    harness, a linter, a packaging check) will import it otherwise.
    """

    SERVER     = r"localhost\SQLEXPRESS"
    DB         = "DataView_Demo"
    SCHEMA     = "file_catalog"
    REF        = "WELL_REF.well_ref.well_master_gold"
    VAULT_ROOT = r"C:\Bulk\Vault"

    stage = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    apply = "--apply" in sys.argv

    eng = pr._engine(SERVER, DB)
    t0 = time.monotonic()
    try:
        if stage == "enrich":
            pr._stage_enrich(eng, REF, apply, print)
        elif stage == "vault":
            os.environ.setdefault("VAULT_COPY_WORKERS", "8")
            pr._stage_vault(eng, SCHEMA, VAULT_ROOT, "copy", apply, print)
        elif stage == "promote":
            pr._stage_promote(eng, apply, print)
        elif stage == "capture":
            pr._stage_capture(eng, "mssql", print, workers=6, parallel=False)
        else:
            print("usage: py run_stage.py [enrich|vault|promote|capture] [--apply]")
            sys.exit(2)
        print(f"[run_stage] {stage} {'APPLY' if apply else 'dry-run'} "
              f"done in {time.monotonic() - t0:.1f}s")
    finally:
        eng.dispose()



if __name__ == "__main__":
    raise SystemExit(main())
