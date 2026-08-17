# DataView v3 — handoff addendum #2: document identity, OCR budget, scan timing
**Date:** 2026-07-17

---

## 1. The gap that started it

> *"Even if the document has a UWI, the sections that are extracted are orphaned. Without that
> association those extracted sections will never load."* — the operator, and he was right.

One scout ticket feeds **11 target tables**. The UWI gate keyed on `_file_key`, which returned
the first of `LOG_ID` / `SRVY_ID` / `INTERP_ID` / `basename(FILE_PATH)` — all **per-table**
keys. So one document looked like several:

| CSV | old `_file_key` |
|---|---|
| well header | `scout.pdf` |
| formation tops | `ML_scout_1`, `ML_scout_2`, … (one per interval!) |
| survey stations | `SRVY_scout` |
| casing / stim / dst_period / pressure | **`""` — no key at all** |

The last row is the worst of it: `_COLS` for those kinds has no `FILE_PATH` and no `*_ID` the
key function looks at, so `_file_key` returned `""` and the gate did `if not lg: continue`.
**Those rows were invisible to the gate.** The screen said *"12 rows missing UWI — assign here
or in the UWI gate"* about rows the gate could not see.

It worked for logs only by luck: `well_log.csv` and `well_log_curve.csv` happen to share
`LOG_ID`, so the sibling rewrite reached the curves. A per-table key doing a per-document job
by coincidence of naming.

---

## 2. The fix — one key per document

`INVENTORY_ID` — `SHA1(UPPER(abspath), UTF-16-LE)`, 40 chars, identical recipe to
`file_gate.inventory_id`, and already the PK of `file_catalog.GLOBAL_FILE_CATALOG`.

1. **Extractors stamp it on every row.** DLIS/LIS at the row-build sites; PDF in
   `write_staging_csvs` — deliberately **one place** rather than `extract_file`'s ~11
   `res[kind].append(...)` calls, so a new document kind cannot forget it.
2. **`_file_key` checks `INVENTORY_ID` first**, falling back to the old per-table keys so
   pre-stamp extracts still work.
3. **`_extract_uwi_files` dropped the `"curve" not in stg_table` filter** and now gates any
   CSV carrying `INVENTORY_ID` or `UWI`.

**Measured on real data:** blank UWI **57 → 0**. Gate rows for one scout ticket: **4 → 1**,
covering 6 tables. Rows the gate could not see: **5 → 0**.

**Free consequences:** `INVENTORY_ID` stops being 0% on the `dv_*` tables, and
`catalog_docs.render_documents` (built, never called — see `dead_all.txt`) can open the source
PDF from any loaded row. It keys on exactly `UWI14` / `SURVEY_NAME`.

**`docx_document_loader.py` is NOT done** — explicitly deferred. Word reports feed 7 tables and
none of them will carry an identity. Same three lines as the PDF loader.

---

## 3. Assignments persist — and can be erased

`file_gate.get_identity()` / `set_identity()` — new. Keyed to `INVENTORY_ID`, so a UWI typed
once survives a re-run, a Reset, and a restart. Read back at scan time and prefilled; the grid's
**`UWI from`** column says `file` (extracted) or `saved` (you assigned it), so a remembered
value never masquerades as something the document supplied.

**This deliberately widens the loader's remit.** `file_gate._OWNED` says the loader may write
the file-identity columns *and nothing else*, because clobbering a triage decision from a
directory scan would be worse than any bug it prevents. `UWI14` is not in `_OWNED`. It is
written anyway, on purpose: **an operator typing a UWI is the strongest identity claim in the
system**, and re-typing it every run is how it gets typed wrong. The boundary is kept narrow —
`MATCHED_UWI`, `MATCH_METHOD`, `TRIAGE_*`, `PROC_*`, `VAULTED` are never touched.

**Two writers, one column, one rule: the operator beats the extractor.** Before this,
`UWI14` = 25 rows (LAS, from the file) and `MATCHED_UWI` = 19 (PDF/DLIS, inferred by
`score_file`) out of 397 — and nothing filled both. That split still needs a decision:
**`UWI14` = assertion, `MATCHED_UWI` = inference** is the working rule, written here because it
was never written down.

**A FORGET action** is in the grid alongside keep/SKIP. It exists because the current test
assignments are **random wells, picked to watch the data flow**. Without FORGET they would
persist silently into every future run of those files. To wipe them all:

```sql
UPDATE file_catalog.GLOBAL_FILE_CATALOG SET UWI14 = NULL, ROW_CHANGED_DATE = SYSUTCDATETIME()
WHERE UWI14 IS NOT NULL AND FILE_EXT IN ('.pdf','.dlis','.lis');
```
⚠ That also clears the 25 pre-existing LAS-derived `UWI14` values. Check them first.

**Untested against SQL Server.** `set_identity` uses `#temp` + `UPDATE...FROM`, matching
`upsert`'s established pattern, but it was only tested against SQLite (which has neither).
Semantics proven, T-SQL not.

---

## 4. Why INVENTORY_ID hashes the PATH, not the content

Asked, and worth recording because the answer is not obvious and the design is right.

Content **is** hashed — `FILE_HASH` (head+tail+size) and `FILE_HASH_FULL` (whole file), both
SHA-256, both columns in the catalog. `INVENTORY_ID` is a path hash serving a different job:

```python
ids = {p: inventory_id(os.path.abspath(p)) for p in paths}
known = _existing(engine, set(ids.values()), ...)     # ONE round trip, before opening anything
```

**The id must exist before the file is opened**, or the size+mtime pre-filter (`classify()`
step 1: *"not read"*) could never skip a read. A content hash as the key would mean reading
every byte of all 397 files on every scan — the pre-filter would be self-defeating.

And path-keying is the property this design **wants**: `state="changed"` (line 241) means *same
path, different content* — re-OCR a scout ticket, re-save a PDF, and it is still the same
document at the same place, so **the assigned UWI survives**. Content-keyed, an edit would mint
a new identity and orphan the row — losing the UWI, the triage decision, the vault status.

**The cost is the mirror image: move a file and its INVENTORY_ID changes.** `classify()` lines
250–257 detect that (`state="moved"`, via `FILE_HASH_FULL`), but **whether `upsert` re-links
the old row across a move is NOT established.** If it does not, a move loses exactly what a
content key would lose on an edit. **That is the open question, not the hashing.**

---

## 5. OCR — kept, but bounded

Not removed: some scout tickets exist only as scans, and re-adding a deleted pipeline is worse
than flipping a flag. But it now has two limits, in `pdf_document_loader.py`:

```python
OCR_PAGE_TIMEOUT_S = 20   # one page that won't resolve (via pytesseract's own timeout=,
                          # which kills the subprocess rather than abandoning a thread)
OCR_BUDGET_S = 60         # a document that is merely BIG
```

Over either → **deferred**: copied to `<out_dir>\_do_later\` (idempotent), named in a ⚠ in the
scan with the exact reason, and **not extracted**. Never silently skipped.

**Both numbers are guesses.** The three test scans were ~2 s/page over 3–5 pages, so they would
still pass — meaning the original complaint (22 s for three files) still costs 22 s under these
limits. `OCR_BUDGET_S = 15` would defer them. That is a judgement about what a scan should
spend.

Deferred files stay in `GLOBAL_FILE_CATALOG` untouched. `EXTRACTION_STATUS` / `CATALOG_ISSUES`
exist and would turn the bucket into a work queue rather than a folder to remember.

---

## 6. Scan timing — measured, not guessed

`_Phases` + a `⏱ Scan took Xs — where it went` expander. Built after **four** theories about a
35 s scan were each killed by one measurement. The instrument beats the hunch — the same
lesson as `staging_qa` and `db_scorecard`.

**The finding: 3 OCR test files were 23.86 s of a 35 s scan — 72%.** Removing them: **35 s →
9.65 s**. Not extraction, not hashing, not the pipeline.

Worse, half that was **wasted twice**: those three files spent 10.27 s in `pdfplumber`
returning **zero characters**, decoding every scanned page — and then `_ocr_reconstruct`
rasterized the same pages again. A cheap no-text-layer pre-check might skip the first pass;
whether pdfplumber's lazy parsing makes that actually cheaper is **unmeasured**.

**Rates that predict a bigger directory** (the total does not):

| phase | s/file |
|---|---|
| **DLIS** | **0.42** ← steepest |
| PDF (text layer) | 0.26 |
| Word | 0.14 |
| gate: hash + classify | 0.027 |
| profile | 0.009 |
| gate: upsert | ~2 s **fixed** (one MERGE) |

~1,000 mixed files ≈ 3–5 minutes. Unaccounted: 0.35 s (3.6%) — the instrumentation is honest.

**Parallelism, now answerable:** not worth it at 65 files (would save ~4 s of 9.65). At 1,000+
it pays for **OCR/PDF** — CPU-bound, independent per file, modest memory. **NOT for DLIS**,
despite the worst rate: `frame.curves()` holds whole arrays, so N workers = N× peak memory, and
this machine hard-crashed twice on 2026-07-16. Hashing threads well (`hashlib` drops the GIL)
but caps on **disk bandwidth**, not core count — expect 2–4×, not 10×.

**`res["ocr"] = True` has existed all along and displays nowhere.** Had it been on screen, this
would have taken one look instead of four wrong theories and two probes. Same as `las_error`.
**Fix that.**

`pdf_probe.py` (new, read-only) reports per-PDF seconds/pages/chars and flags `OCR NEEDED`.
Dedups on `normcase(abspath)` — the double-glob trap would otherwise count every PDF twice.

---

## 7. Streamlit bugs fixed — all the same shape

- **Duplicate `skey` → Phase 2 died outright.** Not a warning: `StreamlitDuplicateElementKey`,
  the whole review screen refuses to render — *the worst possible failure for a review screen,
  because the fix lives inside the screen you can no longer see.* **Took three attempts, and
  each failure named the next:**
  1. `skey = stg_table` → two CSVs auto-matching the same target collided
     (`Completion_Parameters_Perforations.csv` and `Production_Data_..._Monthly_Production.csv`
     both matched `DV_WELL_GOM_BACKUP`)
  2. `skey = stg_table + fingerprint` → **identical files in different folders** collided: same
     target *and* same shape, so the fingerprint is equal by construction. `pdf_probe` had
     already printed eight such filenames across `sample_pdfs/` and `more_pdfs/` — the evidence
     was on screen and went unused.
  3. `skey = stg_table#rowindex#filename` → unique by construction. Plus a defensive dedup pass.
- **The gate's error was discarded by its own `st.rerun()`.** `st.error("Not found in
  dv_well…")` then `st.rerun()` — which raises. You assigned a UWI, the gate correctly refused
  it, and the only symptom was the gate re-appearing with no explanation. Now stashed in
  `bdl_uwi_msg` and rendered after. **AST-swept: zero statements after `st.rerun()`/`st.stop()`
  anywhere in this file.** (`dead_code.py` finds the same pattern at `page_dir_loader.py:696`,
  `page_pipeline.py:862`, `page_well_map_docs.py:989`/`:1434`.)
- **Stale `maps` → `Invalid object name 'stg.dv_well_stimulation'`.** `maps` accumulated in
  session state and never dropped: a table auto-mapped BEFORE its skip was ticked in
  Files → tables stayed in the plan all session, and Phase 5 tried to promote a table that was
  never staged. Now pruned to the current review, and an unstaged table says so plainly.
- **Arrow: `Could not convert '—' with type str: tried to convert to int64`.** My timing table
  mixed ints with `"—"` in one column. Cosmetic — Streamlit auto-fixes and renders — but it is
  console noise, and console noise is how real errors hide. Same trap as `file_viewer` /
  `file_header_store`.

**New: ⏭ Skip in Phase 2**, per table, inside each expander. Files → tables skips **staging**;
Phase 2 skips **promoting** (staged rows untouched, never reach dataview). Requested because
the only skip lived at the top of a very long page — and when Phase 2 crashed, it was
unreachable entirely.

---

## 8. DDL changed — the schema JSON is now stale

`INVENTORY_ID` existed on 11 of 29 `dv_well*` tables, and was **missing from four the PDF
extractor writes to**. Added:

```sql
ALTER TABLE dataview.dv_well            ADD INVENTORY_ID nvarchar(40) NULL;
ALTER TABLE dataview.dv_well_casing     ADD INVENTORY_ID nvarchar(40) NULL;
ALTER TABLE dataview.dv_well_dst_period ADD INVENTORY_ID nvarchar(40) NULL;
ALTER TABLE dataview.dv_well_pressure   ADD INVENTORY_ID nvarchar(40) NULL;
```

The tables that already had it are the ones the pipeline tracks provenance for; the ones that
did not are `casing` / `dst_period` / `pressure` (the three `relax_notnull_ddl.sql` touched)
plus `dv_well`. An incomplete rollout, not a design decision.

**`dv_well.INVENTORY_ID` semantics — decided deliberately:** promote uses `NOT EXISTS` on the
PK, so the **FIRST document to create a well row stamps it**. Later documents describing the
same well do not overwrite. That is *"created by this document"*, not *"every document that
mentions this well"* — the latter is a many-to-many, and `GLOBAL_FILE_CATALOG.UWI14` answers it
from the other side. The existing ~200 KGS wells stay NULL: correct, they came from a bulk
load, not a document.

**`dataview_schema_full.json` now disagrees with the database on four tables.** Whatever
regenerates it needs to run.

---

## 9. Environment — a third OneDrive symptom

Deploying `app_v3.py` broke the app; restoring from VS fixed it; **the same file then worked**.
A later diff proved my copy differed from the restored one by the scrollbar CSS **and nothing
else** — so nothing was reverted and the CSS was not at fault.

The best available explanation is **OneDrive locking a file mid-write**: a file copied into a
synced folder can be readable-but-incomplete for a moment. Streamlit reads it in that window
and gets a broken module from a file that is fine seconds later. Same bytes, different outcome.
That is the same mechanism that killed the watchdog thread on 2026-07-16 with
`PermissionError` on `dlis_header_loader.py`.

So the OneDrive tab now reads: **watcher thread death · `(N)` numbered copies · possible
mid-write reads.** Three distinct failures, one cause. **Move the repo to `C:\dev\`.**

Also confirmed: the `(N)` copies are not inert. `load_diagnostics (3).py` was **inside
`dataview\import_data\`** — unimportable (space + parens), so `_opt_import("load_diagnostics")`
silently loaded the *older* file while the newer one sat there being edited. That is
deploy-staleness with a concrete mechanism. Check periodically:

```powershell
Get-ChildItem -Recurse -Filter "*(*)*.py" | Select-Object FullName, LastWriteTime
```

**Process note:** for a file the operator edits directly (`app_v3.py`), hand over a **patch**,
not a whole file. Twelve lines you can read beat 1,227 you have to trust. Whole-file delivery
is fine for the extractors — those are maintained end-to-end here and deployed wholesale.

---

## 10. Open, in priority order

1. **`min_value`/`max_value` 35.7%** — `_curve_minmax` reads `f.frames[0]` only; channels in
   later frames get nothing (`LOG_07HLB0009`: 186 of 192 missing). Deliberately not fixed:
   reading every frame multiplies peak memory and this machine crashed twice. Do it with a
   memory watch, releasing each frame's array between passes.
2. **`log_id` is 37 of 40 chars** — `LOG_A12-A-08_Run4_8375in_RM_600-1175m`. Three characters
   from the same overflow that broke `curve_id`. **Do NOT use `LOG_<uwi>`:** the data disproves
   it — three logs share `17015101080000`, two share `17015101190000`. Bound the stem or hash it.
3. **Does `moved` re-link the identity?** (§4). Decides whether a rename loses an assignment.
4. **WITSML** — untouched, entirely pre-DDL, `SURVEY_SEQ_NO` hardcoded `"1"` for every station.
5. **`docx_document_loader.py`** — no `inventory_id` (deferred by the operator).
6. **`Completion_Parameters_Perforations.csv` → `DV_WELL_GOM_BACKUP`** is almost certainly a
   mis-match (`DV_WELL_PERFORATION` was skipped as unmatched). Both it and
   `Production_Data_..._Monthly_Production.csv` would also stage into the **same**
   `stg.dv_well_gom_backup` with different shapes — and `create_stg` does DROP+CREATE, so the
   second destroys the first's staging. Skipping one avoids it; wanting both needs
   fingerprint-suffixed staging (`stg_name(target, fp)` already supports it).
7. **`_COLS` in the PDF loader is half-aligned** — `well`, `formation`, `srvy_hdr`, `srvy_sta`
   are lowercase/DDL-aligned; `casing`, `stim`, `dst`, `dst_period`, `pressure`,
   `petro_interp`, `petro_zone` are still uppercase. Case-insensitive matching means it mostly
   works — but the 2026-07-15 handoff's claim that PDF was aligned was **half true**, and the
   half that wasn't is exactly the half that could not reach the gate.
