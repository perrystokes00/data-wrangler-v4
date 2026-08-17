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
directory on `sys.path`, and the app's shipped interpreter is an EMBEDDED build
(`sys.path` = `python312.zip`, the runtime, site-packages, and
`C:\Program Files\Data Wrangler v4\app` — no `''`, no script dir), so it
imported `dataview` from the DEPLOYED copy. See Environment below.

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
- **THERE ARE TWO COPIES OF THE CODE.** The repo, and a deployed copy at
  `C:\Program Files\Data Wrangler v4\app\`. The app runs the DEPLOYED one
  (`…\python\python.exe -m streamlit run …\app\app_v4.py`), so a repo edit
  changes nothing until it is deployed. Worse, `…\python\python.exe` is an
  EMBEDDED build: it does not put the script's directory or the cwd on
  `sys.path`, but it DOES carry `…\app` — so running a repo script with it
  imports `dataview` from the deployment unless the script inserts its own
  root first. `selftest.py` does; `check_mirror_registry.py` did not, and
  reported five phantom `LINEAGE` failures against a stale deployed copy that
  was ten entries behind. Any new root-level tool must do the same insert, and
  a surprising result from one is worth `print(module.__file__)` before it is
  worth a theory.
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
- Deploying the repo to `C:\Program Files\Data Wrangler v4\app` is a manual
  step with nothing checking the two are in sync — the deployed copy was found
  ten `LINEAGE` entries behind on 16 Aug
- Compound-key FKs are silently unchecked (`if len(ccols) != 1: continue`)
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
