# DataView v3 — Repo Cleanup & Reconciliation Plan
_Updated after Tier 1 completion. Work top-to-bottom; each tier is safe once the one above is done._

## ✅ DONE
- **Git backup** — pushed to GitHub as `origin/backup-clean` (orphan branch, clean snapshot). Restore point secured.
- **Tier 0 — functional fixes deployed to live copies:**
  - Scout `API Number:` regex → `modules\pdf_survey_catalog.py`
  - Survey md/incl/azim column fix → `modules\survey_loader.py`
  - Stage scorecard → File Catalog page (`page_workbench.py`)
  - Synthetic scout ticket generator (`gen_scout_ticket.py`) with `|`-separator fix
- **Tier 1 — root-vs-modules\ duplicates ALL reconciled:**
  - `pdf_survey_catalog.py` — root removed; `modules\` canonical (has regex fix)
  - `page_workbench.py` — dead `modules\` copy removed; root live (has scorecard)
  - `pipeline_batch_ui.py` — dead `modules\` copy removed; root live
  - `catalog_capture.py` — already a correct shim (left as-is; this is the template)
  - `shapefile_catalog.py` — root shimmed → `modules\` **(fixed a data-loss `break` bug)**
  - `bcp_capture.py` — MERGED (nested-pool fix + SEG-Y survey outline) into root; `modules\` shim. Validated: 20 LAS logs, 146 curves, 232/235 SEG-Y outlines.
  - `file_viewer.py` — `modules\` shimmed → root **(fixed a UI crash: nest-safe `_vsection` + PyMuPDF PDF render vs the old base64-iframe/bare-expander that crashed)**

  > Note: 3 of the 7 "duplicates" were hiding LIVE bugs (shapefile data-loss, file_viewer crash) because the app imported the buggy copy. The dedup fixed them.

## TIER 2 — Delete pure junk (near-zero risk; git backup is behind you)
Tool: `cleanup_tier2_junk.py` (previews first; `--apply` moves to recoverable `_trash_tier2_`).
- [ ] `download\` — downloaded copy of AI output paths (`download\mnt\user-data\outputs\...`, `download\app_v3 (8).py`)
- [ ] `.vs\` — Visual Studio cache (also add to `.gitignore`)
- [ ] `_archive_20260703_165400\` and `_archive_20260703_165611\` — already-archived diagnostics (~55 files)
- [ ] `__pycache__\` anywhere — Python bytecode (regenerates)
- [ ] `*.bak`, `*.bak_*`, `*.bak_merge`, `*.bak_shim` — patch/merge backups **(only after Tier 1 merges tested & committed)**
- [ ] earlier `_trash_*\` folders from cleanup runs

## TIER 3 — Version-duplicate files (keep newer, delete old)
- [ ] `modules\db_v3.py` (unused) → keep `modules\db.py`
- [ ] `modules\fk_catalog_v3.py` (unused) → keep `modules\fk_catalog.py`
- [ ] `build_well_geojson_old.py`, other confirmed `*_old.py`
- [ ] Copy artifacts: `*(1).py`, `*(8).py`, `*(12).py`, `* - Copy.py`
      ⚠️ Some show ACTIVE (e.g. `page_well_map - Copy.py`) — a file imports the literal name.
      Fix that import to point at the real file FIRST, then delete the copy.
- [ ] `page_pipeline.py` / `_old` / `_v3` — all three showed ACTIVE; check which the nav uses, keep one
- [ ] `page_selected_documents.py` / `_old` / `(12)` — same situation

## TIER 4 — The 332 orphans
Mostly one-off scripts run by hand (`repromote.py`, `walk_bulk.py`, `trace_*.py`, `seed_refs*.py`, `check_*`, `diag_*`).
- [ ] Move them ALL to a `_dead\` folder (don't delete yet)
- [ ] Run the app + a full pipeline
- [ ] If everything works, delete `_dead\` after ~a week

## TIER 5 — Today's temp diagnostics (the ones generated this session)
KEEP: `crawl_scorecard.py`, `gen_scout_ticket.py`, `analyze_dead_files.py`, `cleanup_tier2_junk.py`, `CLEANUP_PLAN.md`
DELETE: all other `check_*`, `diag_*`, `verify_*`, `patch_*`, `fix_*`, `test_*`, `probe_*`, `reset_*`, `merge_*`, `shim_*`, `preflight_*` from today + `.bak_*` (once confirmed working)

## GOING FORWARD — prevent re-duplication
- **Standardize the shim pattern:** one real implementation in `modules\`; if a root name is needed, root is a 1-line `from modules.X import *` shim (like `catalog_capture.py`). This is what stopped the split-brain.
- **Add `.gitignore`:**
  ```
  venv/
  __pycache__/
  *.pyc
  .vs/
  .idea/
  _trash/
  _trash_*/
  *.bak
  *.bak_*
  download/
  geojson/*.geojson   # >100MB files rejected by GitHub; regenerable
  spatial/
  schema_registry/
  output/
  ```
- **Don't save browser-downloaded copies into the repo** (that's where the `(1)`/`(8)`/`(12)` artifacts came from).
- **Remember:** the app imports a mix of `import X` (root) and `from modules.X import` — the shim pattern keeps both working so you never have to hunt down every import.

## OUTSTANDING (non-cleanup, from the pipeline work)
- [ ] DDR (Daily Drilling Report) — no `load_ddr` / no `RT_DDR` routing in `_do_pdf`. Wire a loader if you want DDR detail to promote.
- [ ] Multi-well scout tickets — `extract_scout_ticket` parses only the first well; iterate per-well for multi-well tickets.
- [ ] `session3-h3-grid` branch history contains >100MB geojson files → will be rejected if pushed. Needs history rewrite (BFG/`git filter-repo`) OR keep using `.gitignore` + fresh branches.
