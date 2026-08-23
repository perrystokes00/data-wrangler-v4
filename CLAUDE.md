# Data Wrangler v4 — working notes

Perry Stokes, sole developer. Petroleum data management: a DataView database
(PPDM 3.9 derivative), a File Catalog that extracts from documents and logs, a
Data Assistant that loads tabular files, and a Streamlit Mapping page.

These notes are what previous sessions cost to learn. Read them before
proposing a design.

---

## The one rule that would have saved the most time

**Read the file before writing the feature.**

Six times in one week a capability already existed and a parallel, worse
version was built beside it:

| Built | Already there |
|---|---|
| A reference-wells map layer (325 lines, 4 bugs, deleted) | `dataview_federation.v_well_density_r*` — one `UNION ALL` |
| A hexagon renderer using CircleMarkers | `_add_h3_layer` + `_h3_cell_boundary_geojson` |
| A density source filter | `_qry_h3_grid(schema_filter=…)` — the parameter existed |
| Region extents queried from wells | `PETROLEUM_REGIONS` states each region's centre |
| A county outline layer | `us_geo.state_feature_collection` + an existing draw block |
| A promotion report | `promotion_lineage.file_detail` |

Every time, reading first made the change a fraction of the size. Grep for the
concept before implementing it.

---

## Design law (Perry's, and they hold)

- **AI proposes, deterministic core verifies and executes, human confirms,
  stores remember.** The model never writes SQL that reaches the database. It
  names an operation from a catalogue; tested Python performs it.
- **Automation may skip ceremony, never a decision.** A one-button load was
  built and removed for this reason: seeding an entity parent
  (`DV_BUSINESS_ASSOCIATE`) is a decision, not a step.
- **Wrong is worse than missing.** A confident wrong value plots, exports and
  gets quoted; a missing one is visible. This is why promote HOLDS rows rather
  than guessing, and why a coordinate is never invented.
- **Hold, don't drop.** A row that can't promote stays in `cat_*` with a
  reason. Held is recoverable; discarded is not.
- **The first one in wins** (provenance). Promote is insert-only with
  `NOT EXISTS`, so whichever load inserts a row owns it.

---

## Things that silently produce wrong answers

**An identifier read as a number stops being an identifier.** Bitten three
times: `INVENTORY_ID` hashed from an unnormalised path; doubled backslashes
from a CSV `escapechar`; and WOGCC's `APINO` (`105001`) read instead of
`CAPINO` (`49-001-05001`), which made 67,229 Wyoming wells unjoinable while
looking like a valid key. Symptom is always "the data is missing" when it
isn't.

**A wrong value defeats every repair keyed on "missing".** The synthetic
generator fills unknown columns with `<column_name>-<random>` —
`legal_survey_type-379`, `h3_r4-869`, `INVENTORY_ID-641`. Non-null, so
backfills skip it, completeness checks pass, and distinct-value counts inflate.
`find_placeholders.sql` detects them (nothing real begins with the name of its
own column).

**Swallowed exceptions cost hours.** `except: return [], 0` made a broken query
look like an empty result twice in one evening. If a diagnostic is discarded,
the next failure is undiagnosable.

**A csv `escapechar` doubles every separator in a Windows path, and BULK
INSERT stores it.** Third instance of the identifier-as-text failure, found
16 Aug. `csv.writer(delimiter='\t', quoting=QUOTE_NONE, escapechar='\\')`
escapes the escape character itself, so `C:\a\b` is written `C:\\a\\b`;
BULK INSERT has no escape concept and stores that verbatim. `INVENTORY_ID` is
a SHA1 of the path, so one file took two identities — 2,094 of 3,876 rows
doubled, 1,301 of them duplicates, and 1,317 `dv_well` rows left citing a
source nothing could resolve. Two of the three writers hashed the id from the
CLEAN path and wrote the ESCAPED one, so id and `FILE_PATH` described
different strings. `canon_root()` does NOT protect against this: it cleans the
pasted root on the way in, and the doubling happens on the way out. One writer
now: `path_identity.bulk_csv_writer` (+ `bulk_field`), no escapechar,
`lineterminator` pinned to `\r\n` to match `ROWTERMINATOR = '0x0D0A'`.

**An invariant keyed on the wrong column can never pass.** "No file catalogued
under two path spellings" grouped by `FILE_NAME`, which is not unique — the
same filename legitimately lives in several folders (194 did). It reported
1,394 where the truth was 1,301, and would have failed on a clean catalog
forever. `FILE_PATH` is the FULL path; that is the identity the hash is a
function of, so that is the key.

**A module-level import missing under a bare name fails only when the line
runs.** `extract_core` used `uuid.uuid5` in `_well_params`/`_seis_params` with
no module-level `import uuid` (only a local alias in a different function), so
every enrichment write — batch AND the per-row fallback — raised
`NameError`. It surfaced only because a test read the log; the stage reported
completion.

**A `sys.path` line pointing at the wrong directory reads exactly like the
fix.** Python puts the SCRIPT's own directory on `sys.path[0]`, never the repo
root, so `python tools/<name>.py` — how every one of them documents itself —
died with `ModuleNotFoundError: No module named 'dataview'`. 26 of the 28
`tools/` scripts that import `dataview` could not be run at all, found 23 Aug
when `reconcile_orphans`, the tool that diagnoses orphaned provenance, was
needed and would not start.

Twelve had no `sys.path` line. The other **fourteen had one, and it was a
no-op**: it pointed at `tools/` (already `sys.path[0]`) or at a `modules/`
directory the v4 reorg deleted — `git ls-files` finds zero paths under it.
`recatalog_seis` still told the reader to "run from the project root so that
modules/ is on the path". So the obvious check — grep the source for
`sys.path` — passes all fourteen and the bug survives it. Same shape as the
invariant keyed on `FILE_NAME`: right question, wrong key. `tier_units`
therefore EVALUATES the argument of every `sys.path.insert/append` with
`__file__` bound to the script and requires the repo root to come back.

One line, the one `app_v4.py` already uses, and a no-op under `python -m` so
it never breaks the module form:

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

**`COL_LENGTH` returns NULL for a missing TABLE and a missing COLUMN alike.**
Pair it with `OBJECT_ID` or a guard silently skips.

**`_` is a wildcard in T-SQL `LIKE`.** `LIKE 'cat_%'` matched `catalog_setting`.
Bracket it: `cat[_]%`.

**UWI-14 padding must agree everywhere.** Promote right-pads `uwi` to 14
(`build_promote_sql`). Any comparison must apply the same transform to BOTH
sides. A missing pad on one side made an FK suppression clause silently inert
for six weeks, reporting 1,188 false violations.

---

## Performance: pyodbc for statements, bcp for sets

Measured, three times:

| Path | pyodbc | bcp |
|---|---|---|
| H3 backfill, 3.9M rows | 225 rows/sec (4 hours) | ~27,000 rows/sec (~4 min) |
| Map well query | slow | 60–100× faster |
| Catalog file list | 658s in `ASYNC_NETWORK_IO` | **was not a fetch problem — see below** |

`ASYNC_NETWORK_IO` on a SELECT means SQL Server has rows ready and Python isn't
consuming them. That's the fetch, not the query. The fix is bcp in both
directions (queryout → CSV → compute → CSV → bcp in → one set-based UPDATE), or
at minimum `cursor.arraysize`.

**BEFORE BLAMING THE FETCH, CHECK ODBC TRACING.** 16 Aug the catalog file list
was reproduced at 804s and the cause was not the query, the columns, or pyodbc:
`HKCU\SOFTWARE\ODBC\ODBC.INI\ODBC\Trace` was **1**, so the Driver Manager was
logging every ODBC call to `%TEMP%\SQL.LOG` (764 MB). Setting it to 0 took the
same query from **804s to 0.11s** — 7,300×, no code changed.

The tell is that it is NOT specific to anything:

- every ODBC driver (11, 17, 18, legacy "SQL Server") equally slow
- .NET SqlClient on the same box: `SELECT 1` in 0.7ms vs pyodbc's 80ms
- a server-side 100k-iteration T-SQL loop: 231ms — the server is fine
- cost scales with rows × **COLUMNS**, because each column fetch is one
  logged call. That is why `SELECT *` over 44 columns looked catastrophic
  and a COUNT(*) looked fine — and why it reads exactly like a fetch problem.

So `ASYNC_NETWORK_IO` says the client is slow to consume; it does NOT say the
client's *code* is wrong. One `SELECT 1` round trip is the cheapest possible
test: 0.5ms healthy, 80ms+ traced. Do that before rewriting anything as bcp.

**A shrink undoes a rebuild, so the order is rebuild-then-shrink NEVER
shrink-then-rebuild — and after a shrink the tool is REORGANIZE.** Measured
20 Aug on `WELL_REF`: `PK_well_master_gold` was 89.8% fragmented; a REBUILD took
it to ~0% and grew the file 6,178 -> 8,081 MB (the old allocation units release
on a deferred drop a few seconds later, so the alarming number is transient).
`DBCC SHRINKFILE` then relocated pages back-to-front and put the clustered index
at **94.2%** — worse than before it was rebuilt — and dragged `IX_wmg_h3_r5/r6`
from 3% to ~63%, which are exactly the indexes the map's density layers read.
Rebuilding again would only regrow the file, so the second pass has to be
`REORGANIZE`: it defragments in place using pages the index already owns, so the
shrink holds.

And REORGANIZE turned out to do what the REBUILD did not: it took the clustered
index 94.2% -> 0.4% AND dropped it from 310,604 to 234,516 pages, freeing 594 MB
(used 6,174 -> 5,570) where the rebuild had left the page count identical. So on
this table the wasted space was page DENSITY, and only the in-place compaction
recovered it. Budget for the log: the reorganize is fully logged and took the
log from 520 MB to 1,160 MB, which a CHECKPOINT plus SHRINKFILE returns.
Timings, 4M rows on Express: rebuild 43s, reorganize 250s.

Also: **type mismatches between staging and target are a performance bug.**
nvarchar staging against `char(14)` targets converted the indexed column on
every comparison — 154s → 0.89s once cast to the target width (173×).

---

## Streamlit scars (seven, all earned)

1. **Fixed-key widgets never re-default** → version the key.
2. **Every rebuild must harvest pending edits first.**
3. **`data_editor` frames must be render-stable** or keyed to their signature.
4. **Expanders cannot nest.** A block inside one needs no second disclosure.
5. **A `data_editor` outside a form reruns the page on every cell change.**
6. **Never assign a widget's own key after instantiation.** Use a request flag
   consumed before the widget is drawn. The error surfaces on a LATER run, on
   whatever page draws next — so the crash appears far from its cause.
7. **A widget key must not depend on state that later code mutates.** The key
   that draws must be the key that reads. Versioning on the wrong variable
   meant every first Apply was silently discarded.

**Corollary to #6:** the sub-page persist loops self-assign every key to survive
a page switch. `_is_action_key()` excludes what can't be set — buttons,
downloads, uploaders, **data editors** (`:sel`, `_editor`), and **form
submitters** (`FormSubmitter:…`). Adding a widget type without adding it there
produces a delayed, misattributed crash. Re-run the both-directions sweep after
adding any button.

**And: Python never learns about a pan or a zoom.** Three designs tried to
follow the viewport and none could work. If the browser owns the state, ask the
user for it (a slider) or read a value the app itself set.

---

## Lists that must agree (nothing checks all of them)

1. `build_catalog_mirror.MIRROR_TABLES` — which mirrors exist
2. The `file_catalog.cat_*` tables themselves
3. `promote_catalog`'s dedicated promoters
4. **`promotion_lineage.LINEAGE`** — which pairs any report can SEE

`check_mirror_registry.py` verifies **all four** (check F, added 16 Aug) and is
wired into `selftest`'s invariants tier; the code-only half of 4 is also a unit
check, so it runs without a database. A missing pair used to be invisible: rows
capture, promote lifts them, and the report says "no detail rows". Casing,
stimulation, petro_zone and perforation were all missing — 1,433 rows the
reports could not see.

**The check was written and immediately caught the wrong thing**, which is the
lesson worth keeping: it reported five tables missing from `LINEAGE` that the
repo's `LINEAGE` plainly named. `check_mirror_registry.py` did not put its own
directory on `sys.path`, and the app's shipped interpreter was an EMBEDDED build
carrying `C:\Program Files\Data Wrangler v4\app` but neither `''` nor the script
dir — so it imported `dataview` from the DEPLOYED copy. That specific trap is
gone (the install was removed 18 Aug — see Environment), but the lesson is not:
**a tool that reports a surprising failure may be reading different code than
you are.** `print(module.__file__)` costs one line and settles it.

---

## Reporting: the failure mode to watch

`cat_*` is a **drain**. Promote MOVES rows and deletes from the mirror, so an
empty `cat_` table after a successful load is *success*. Any report asking
`cat_*` "did this file produce rows?" reads success as failure.

The honest test is `INVENTORY_ID` lineage into `dv_*` — which is what
`promotion_lineage` does. Four distinct states, and they must not be collapsed:

- **Loaded** — rows in `dv_*` with this id
- **Staged** — in `cat_*`, not yet promoted
- **Held** — in `cat_*`, blocked by a named gate (say which)
- **Nothing** — neither. The only real failure.

---

## Pipeline scope (a live source of confusion)

~~**Only the `scan` stage is scoped to the folder you give it.**~~ **FIXED
16 Aug — `scope='path'` is now the default.** Every stage used to work on the
whole catalog's pending queue, so a run pointed at a document folder processed
LAS files elsewhere. The filter to prevent it (`_root_filter`/`_root_likes`)
was already built and simply never reached: `_force_root = _canon(root) if
force else None` tied path scope to FORCE. `force` is now orthogonal — it
decides whether already-done files are REDONE, not which files are in SCOPE.
`scope='queue'` restores the old whole-queue behaviour.

Threading it took five changes, and three would have failed silently:
`_stage_extract`/`_stage_extract_capture` took no `root` at all;
`_already_done_filter` applied the root clause only when forcing (its docstring
said scoping the normal path was "a different decision" — it was, and it has
now been taken); `_unprocessed_count`, the batch loop's gauge, would have
counted work no batch could claim and called a finished run "stuck"; and
`pipeline_proc_runner` spells `_common` out key by key, so a new toggle reaches
`run_pipeline` ONLY if named there — the multicore path is the default, so
missing it makes a new control do nothing in the common case.

**A file that has moved is HELD as `'M'`, not dropped.** Its own letter, not
`'E'`: broken-file and stale-catalog are different facts with different
repairs, and folded together a reorganised folder reads as a corpus full of
corrupt files. Capture used to log the failure and write NO state, so those
rows were re-claimed and re-failed every run forever; recognise silently
FILTERED them out of the parse list, so they were never parsed, counted, or
reported. `'M'` is outside both pending predicates, so the row stops being
retried but keeps its id and its reason in `CATALOG_ISSUES` — recoverable by
re-scanning where the file now lives.

**"Run pipeline" does one pass and reports "done" with work remaining.**
`run_pipeline_batched` loops until the queue is clear and already exists —
it's the unticked **Batch mode** checkbox, framed as a performance option when
it's actually the correct default. **Open: make batched the default.**

---

## Key facts about the data

- `dv_well.uwi` is `char(14)`, the PK, and `char(14)` in 35 tables.
  `dv_prod_volume` is the one detail table with **no** uwi (keys on
  `prod_entity_id`).
- **Only 5 `dv_r_*` reference tables** (uom, well_type, source, well_status,
  depth_datum) — deliberately few domains, thoroughly seeded.
  **Creating a reference table ARMS A GUARD**: promote holds any row whose
  coded value isn't registered, and the guard fires only for `dv_r_*` names. So
  a new domain needs its table AND a list covering what the data says, in the
  same step.
- **8 geography columns.** `dv_seis_set.geog` is **POLYGON only** — a LINESTRING
  there once killed the entire seismic layer.
- **H3 cells are derived, not promoted.** `h3_refresh` must run after any load
  that adds wells. `--all` recomputes; the default skips non-null values, so a
  well whose coordinates CHANGED keeps stale cells.
- **H3 hexagons do not nest.** ~6% of points have an r5 cell whose parent isn't
  their r4 cell. That is correct, not corruption.
- `WELL_REF.well_ref.well_master_gold` — ~4M agency well headers, now with H3
  cells. Reached from `DataView_Demo` by three-part naming and federated into
  `dataview_federation.v_well` / `v_well_density_r*`.

---

## Environment

- SQL Server 2022 Express, `localhost\SQLEXPRESS`, database `DataView_Demo`
  (**not** `DataView` — a different, older database exists).
- The map reads a DataView-SHAPED database only; other sources reach it by
  being federated into `v_well`, not by connecting directly.
- `C:\Bulk` is staging workspace, never input. Reports land in
  `C:\Bulk\reports`.
- DDL exports are UTF-16LE with CRLF — `iconv -f UTF-16LE` before grepping.
- **If EVERYTHING is slow, check `HKCU\SOFTWARE\ODBC\ODBC.INI\ODBC\Trace`
  first.** One click in odbcad32 → Tracing turns it on and it persists across
  reboots, slowing every ODBC call ~165× app-wide. `SELECT 1` is the test.
- **THERE IS NOW ONE COPY OF THE CODE (18 Aug).** The installed build at
  `C:\Program Files\Data Wrangler v4\` was uninstalled and its directory
  removed — application, Start Menu shortcuts and the bundled embedded
  interpreter all gone. The repo is the only copy. Verified before removing:
  all ten files that existed only in the deployment also exist in the repo
  (at tidier paths — `tools/`, `dataview/migration/`, `_attic/`); the install
  held no vault, reports, database, `.env` or licence state; and `bcp` comes
  from the SQL Server Client SDK, not the bundle, so the capture fast path is
  unaffected. The only unique file was the build manifest, saved as
  `build/DIST_MANIFEST_20260710_installed.txt` alongside a file listing.
  A 20-file LAS load was re-run afterwards: same 15 CATALOGED, 180 rows.

  **What this retires.** The old warning here was that a repo edit changed
  nothing until deployed, and that `…\python\python.exe` was an EMBEDDED build
  which put `…\app` on `sys.path` but not the script's directory — so a repo
  script silently imported `dataview` from the deployment. That is how
  `check_mirror_registry.py` reported five phantom `LINEAGE` failures against a
  copy ten entries behind. Neither hazard can occur now: there is no second
  copy and no interpreter carrying one. `promotion_lineage.LINEAGE` reads 22
  pairs, from the repo, full stop.

  **What still holds.** `app_v4.py` inserts its own root at `sys.path[0]`
  before importing anything, and `start.bat` prefers `.venv`/`venv` then PATH.
  Keep both: they are what make "whichever interpreter launches it, the modules
  beside it win" true, and they cost nothing. A surprising import result is
  still worth `print(module.__file__)` before it is worth a theory.

  **The repo now carries its own environment (18 Aug).** `setup.ps1` built
  `.venv` from `requirements.txt` — 154 packages, `include-system-site-packages
  = false`, verified isolated (streamlit / pyodbc / lasio / sqlalchemy /
  geopandas / anthropic / h3 all resolve from `.venv\Lib\site-packages`, none
  from the Store's). `start.bat` and `run.ps1` both prefer it, the pipeline
  runs on it, and `app_v4.py` serves HTTP 200 from it. Rebuild with
  `.\setup.ps1 -Recreate`; never commit it (absolute paths are baked into its
  launchers — that is why a venv copied from `data_wrangler_clean` produced
  "Unable to create process using …").

  Two caveats worth knowing:
  - **The base is the Microsoft Store Python 3.12.10** (`py -3` finds nothing
    else; the `AppData\Local\Programs\Python\Python312` tree has no
    `python.exe` and the chocolatey `python3.14` shim points at a path that
    does not exist). `sys.base_prefix` is inside `C:\Program Files\WindowsApps`,
    so the venv still needs that Store package present. Installing a
    python.org 3.12 and re-running `.\setup.ps1 -Recreate` would cut the last
    tether.
  - **`.venv` is 65,455 files / 1.26 GB inside the OneDrive sync root.**
    `.gitignore` covers it; OneDrive does not read `.gitignore`. If sync starts
    churning, make `.venv` a directory junction to somewhere outside OneDrive
    (`mklink /J`) — `start.bat` and `run.ps1` both test for
    `.venv\Scripts\python.exe`, which a junction satisfies, and OneDrive does
    not follow reparse points.
- `page_well_map.py` is ~520KB and 100% CRLF. Check
  `d.count(b'\r\n')` against total after every edit.

---

## Open work

**High value:**
- Batch mode as the default for a full run; report what's left instead of "done"
- ~~`LINEAGE` into `check_mirror_registry` (the fourth list)~~ **DONE 16 Aug — check F, plus a code-only unit check**
- ~~The catalog file-list query → bcp or `arraysize` (658s observed)~~ **CLOSED
  16 Aug — it was ODBC tracing, not the fetch. 0.11s with tracing off. Do not
  rewrite this as bcp without re-measuring first.**
- Teach `enrich_from_gold` the Wyoming key transform, or reload WY from
  `CAPINO` (the proper fix — `reload_wy_master.py` does this)

**Known gaps:**
- ~~Deploying the repo to `C:\Program Files\Data Wrangler v4\app` is a manual
  step with nothing checking the two are in sync~~ **CLOSED 18 Aug — the
  install was removed; there is nothing to keep in sync.** If a packaged build
  is ever wanted again, `build_installer.ps1` / `make_dist.py` still produce
  one — but re-introducing an installed copy re-introduces the drift, so pin a
  version check to the build if it comes back.
- **~~Three~~ TWO reset paths, two different protection lists.** `demo_reset`
  (v4) preserves `dv_column_map` / `dv_column_synonym` / `dv_target_attribute`;
  `clear_catalog.PROTECTED` preserves those *plus* `dv_global_file_catalog`.
  The two still disagree, and `demo_reset.py`'s own comment says they should
  not — that half is still open.

  **The dangerous third is gone (23 Aug).** `data_wrangler_v3/modules/
  demo_reset.py` protected **none** of those three while pointing at the same
  `DataView_Demo`, and `full=True` was its DEFAULT — so a Reset click in a
  retired app destroyed ~2,604 rows of learned mappings belonging to the app
  that replaced it. Deleted in v3 commit `fa15c0e`; recoverable from there if
  v3 is ever revived, at which point it needs v4's protection list rather than
  a straight restore. Safe to delete outright because both call sites
  (`app_v3.py`, `page_run.py`) import it lazily inside the button handler and
  already catch the failure, so the button now reports "Reset failed: …"
  instead of crashing or silently doing nothing.
- `tools/bulk_runner.py` imports `dataview.import_data.page_bulk`, deleted in
  the v4 reorg; `run_job` exists nowhere in the repo. The import is inside a
  function, so `--help` succeeds and it dies only when a job actually runs.
  Repoint it at the surviving loader or retire the headless runner.
- ~~Compound-key FKs are silently unchecked (`if len(ccols) != 1: continue`)~~
  **CLOSED 23 Aug — `promote_catalog._parent_fk_predicates` holds a child whose
  parent row is missing instead of letting the INSERT 547 and fail the whole
  mirror. Compound keys included; that is what `fk_log_curve_log (uwi, log_id)`
  needed. `dv_well` stays excluded — the detail path already gates it with
  UWI-14 normalisation a generic column comparison cannot reproduce.**
- Entity-parent resolution (name → surrogate id) has no UI
- A retarget doesn't update the fingerprint memory; a skip does
- `_qry_wells_in_bbox` reads `dv_well` directly, so map drills can't reach
  federated sources — but it serves every well query, so scope carefully

---

## Working style that has paid off

- **Get the artifact before the next theory.** Four patches on plausible
  theories cost a morning; reading how the widget key was built solved it in
  one pass.
- **Sample before apply.** A coordinate backfill nearly wrote 1,436 confidently
  wrong positions (every one at `quality_score 100`). The twenty-row sample
  caught it.
- **Simulate before shipping** where the failure is silent — reproduce the bug
  and the fix in a standalone script.
- **Verify by content, not size.** A file can grow while a fix is reverted.
  Check for each change by name.
- **Delete dead code.** A registry nothing reads costs a wrong diagnosis later.
