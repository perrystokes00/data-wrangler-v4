# DataView v3 Architecture — Decisions from 2026-05-28

This doc captures the architectural decisions made during the loader
hardening session. Written at the end of the session so they're not lost.

## Storage / federation model

**Decision:** Schemas-as-isolation-boundary within a single database.

- All sources live in one production DB (`DataView`) and one test DB
  (`DataView_Test`). No source-per-database split.
- `dataview` schema holds the federation layer: common tables for all
  data types (`dv_well`, `dv_well_formation_top`, `dv_well_log`, etc.)
- Per-source ext schemas exist within the same DB:
  `dataview_kgs`, `dataview_gom`, etc. — for *native* well-header shape
  preservation.
- Per-source raw schemas (`raw_<source>`) live in the `wrangler` DB —
  the AI importer's landing zone for ad-hoc imports.

## Wells vs. child data — federation policy

**Decision:** Federation pattern applies to well headers ONLY. Child data
is uniform across sources.

| Data type            | Pattern                                      |
|----------------------|----------------------------------------------|
| Well headers         | Federation: dv_well + dv_well_ext_<source>   |
| Formation tops       | Common table, `source` column                |
| Well logs            | Common table, `source` column                |
| Log curves           | Common table, `source` column                |
| Directional surveys  | Common table, `source` column                |
| Cores                | Common table, `source` column                |
| Production           | Common table, `source` column                |
| Strat sections       | Common table, `source` column                |

Rationale: child-data shapes are genuinely uniform across petroleum data
sources (a formation top is a formation top). Well headers genuinely
differ (KGS has TWP/RGE/SEC, GoM has lease numbers, TX has district
codes). Native-shape preservation has value only where shapes actually
differ.

Exceptions allowed: if a specific source has a quirky child-table column
worth preserving, add it as nullable on the common table, OR stash in
the well's ext row, OR create a one-off ext table for that specific
combination. Don't preemptively build for quirks that don't exist.

## Identity

**Decision:** Stay with UWI string identity (e.g. "KGS_1001184287",
"GOM_<well_id>") for the extension-table model. The SHA1_40 hash idea
explored in `federation_migration_design.md` is shelved — wells live
in dv_well by UWI; child data joins via UWI.

Plugins are responsible for producing stable, source-unique UWIs.

## Loader framework

**Decision:** Stay with the existing `loaders/` plugin architecture.
Today's hardening (preflight, idempotency, error surfacing, BCP
detection) committed it as the production-load path.

### Confirmed plugin contract (no change today)
- One Python file per source plugin in `loaders/plugins/<name>.py`
- Subclass `SourcePlugin`, implement `detect()`, `parse_rows()`,
  `native_column_order()`
- `parse_rows()` yields `ParsedRow` with `uwi`, `native_columns`,
  `well_columns`, `identifiers`
- The runner BCP's three CSVs into `dv_well_ext_<source>`, `dv_well`,
  `dv_well_identifier`

### Current limitations of the plugin contract (acknowledged)
- Wells-only. No support for child data (formation tops, logs, etc.)
- Three-table BCP target is hardcoded; an N-table version is needed
  before any source can load its own child data through the plugin
- Two open paths for future extension:
  - **A1:** extend `ParsedRow` to carry child rows; runner BCPs into
    N+ tables per parsed well
  - **A2:** separate plugins per data type per source
    (KgsWellsPlugin, KgsTopsPlugin, etc.)
  Both keep today's hardened transport/preflight/idempotency intact.
  Decision deferred until a real use case appears (the first source
  needing to load its own child data).

### Today's hardening features
- `find_bcp_exe()` — locates newest BCP on system, not bare PATH
- `bcp_version()` — preflight reporting
- `BcpError.__str__` — prints stdout+stderr inline (no more silent
  failures)
- `bcp_in(error_file=...)` — BCP -e flag for rejected rows
- `PreflightError` — fails fast before parse
- `preflight()` — 7-step validation: source file, BCP, DB connect,
  target tables exist, column counts match plugin / DV_WELL_COLUMNS,
  h3 library, existing-rows check
- `--reload` flag — opt-in destructive re-load
- `--skip-preflight` flag — escape hatch (not recommended)
- Cleanup-on-success-only — staging preserved on failure with paths
  printed
- Exit codes split: 1 = preflight/setup, 2 = BCP failure, 130 = Ctrl+C

### Inline H3 in KGS plugin
- `_compute_h3(lat, lon)` populates h3_r4..r7 + h3_coord_hash during
  `parse_rows()` rather than as a deferred backfill
- h3_coord_hash formula reverse-engineered from existing dv_well rows:
  `SHA256(f"{lat}|{lon}").hexdigest().upper()` using Python default
  float repr (no padding)
- Verified against 3 known coord-hash test vectors

## AI importer — current state and future

**Current state:** `page_dv_importer.py` (1094 lines), wells-only.
Architecture:
- Mechanical alias mapping (`_match_by_aliases`) first
- Claude assist (`_ai_detect_columns`) for unmatched columns
- Loads to `wrangler.raw_<source>.well` (native) + `DataView.dataview.dv_well`
  (federated)
- Uses BULK INSERT, not BCP (less hardened than the plugin framework)
- No preflight, no idempotency safety (just `DELETE WHERE source=X`)

**Decision (updated end-of-session):** Don't extend `page_dv_importer.py` for
multi-table support. Instead, **port v2's eight-stage pipeline to v3** by
generating a dataview JSON schema and pointing the existing pipeline at it.
The v2 pipeline already has everything we'd be rebuilding.

### Why this changed

Late in the session, looked at v2's `page_pipeline.py` + `promote.py` and
confirmed:
- v2 has a fully-realized 8-stage pipeline (Connect / Stage / Normalize /
  Select Target / Match & Map / FK Resolve / Validate / Promote) with each
  stage as a clean separable module.
- v2's pipeline is **schema-driven by JSON** (`load_schema_from_dict` /
  `load_schema_from_string`). It doesn't hardcode PPDM 3.9 — it walks whatever
  schema you hand it.
- v2 has mature FK resolution (`fk.py` + `fk_entity.py` + `fk_catalog.py`,
  ~130KB total) including topological sort, parent seeding, reference table
  context, and entity-mapping for FK chains.
- v2 has the RTM mapping fingerprint cache, transforms, normalization rules,
  validation engine — all of which we'd otherwise build.

The "dataview is a simplified PPDM" insight is the migration's key lever:
generate a dataview JSON schema in the same shape as
`schema_registry/ppdm_39_schema_domain.json`, hand it to v2's pipeline, and
most of the multi-table loading "just works."

### Migration scope (next session arc)

**Session 1 — Schema generation (mechanical)**
- Write `generate_dataview_schema.py` modeled on v2's `generate_db_schema.py`
- Output: `schema_registry/dataview_schema_domain.json` (tables, columns,
  PKs, types — same shape as PPDM JSON)
- Output: `schema_registry/dataview_fk_catalog.json` (FK relationships)
- Verify the generated JSON loads cleanly via v2's
  `load_schema_from_dict()`

**Session 2 — Pipeline integration**
- Drop v2's `page_pipeline.py` + 10ish supporting modules into v3
- Modules to port: `db.py`, `schema.py`, `staging.py`, `normalize.py`,
  `mapping.py`, `fk.py`, `fk_entity.py`, `fk_catalog.py`, `validate.py`,
  `promote.py`, `user_rules.py`, `audit_log.py`
- Add pipeline page to v3 app's navigation
- Connect to DataView, point at the new dataview schema JSON, test against
  `well_header.csv` from the synth set

**Session 3 (probably) — Cleanup + dialect simplification**
- Strip Oracle + Snowflake branches from `promote.py` (~half its code) since
  v3 is SQL Server-only
- Decide what to do with the existing `page_dv_importer.py` and
  `ppdm_agent.py` — keep, replace, unify?
- Test against all 8 synth data types

### Concerns to address during migration

- v2 promote.py has Oracle + Snowflake branches throughout (~half the code)
- v2's `ppdm_agent.py` is an AI assistant in the pipeline — overlaps with
  v3's AI importer. Decide whether to keep both, replace one, or unify.
- v2 schema includes PPDM reference tables (`r_well_class`, `r_well_status`);
  dataview has a simplified set. The JSON generator must capture what
  dataview ACTUALLY has, not assume PPDM conventions.
- v2 pipeline assumes one target table per run. Synth dataset has 8 files
  to 8 tables → 8 pipeline runs. That's fine (each gets review-as-you-go)
  but not "load all 8 in one click."

### Files to read in detail at start of next session
- `modules/schema.py` — the JSON schema loader (small, KEY to migration)
- `modules/db.py` — dialect handling
- `modules/staging.py`, `mapping.py`, `fk.py` — to confirm portability
- v2's `generate_db_schema.py` — template for the dataview generator

## Two architectures, two purposes — clear hand-off

**Source-defined plugins** (`loaders/plugins/*.py`):
- For known recurring sources (KGS, GoM, future TX/OK)
- Encode source-specific quirks deterministically
- Production load path, fully hardened (today's work)
- Run via CLI: `python -m loaders.run --plugin KGS --file ...`

**v2-pipeline-ported import tool** (next session arc):
- For ad-hoc imports of any data into any dataview table
- Streamlit UI with 8-stage review-as-you-go workflow
- Schema-driven (JSON), FK-aware, validation built in
- Replaces today's `page_dv_importer.py`

These are intentionally different tools for different jobs. Don't try to
make them one tool.

## Synth dataset — status

- 16 files extracted, profiles documented in `synth_loader_spec.md`
- 200 wells, 684 formation tops, 160 logs, 1,120 log curves, 120 survey
  hdrs, 1,966 survey stations, 80 cores, 2,572 production rows, 1,368
  strat sections
- Not loaded yet — intentionally. Multi-table loader needs to exist
  first.
- When loaded: source label = 'SYNTH', target DB = DataView_Test
  initially, ext table = `dataview.dv_well_ext_synth` (to be created)

## Today's lessons logged

1. "Dry-run verified" only exercises parser, not DB side. Real
   verification requires end-to-end against a real schema.
2. Loaders must locate their own tools (BCP); don't trust PATH ordering.
3. BCP failures must surface SQL Server's actual error; exit code alone
   is unactionable.
4. Schema constraints drift independently of loader code (today: h3
   NOT NULL added after the plugin was written). Preflight column-count
   check catches the biggest class of mismatches.
5. Multi-step pipelines need either all-or-nothing transactions OR
   idempotent steps with --reload safety.
6. Staging cleanup should happen on success only. On failure those files
   ARE the diagnostic.
7. Long-running uncommitted SSMS transactions block subsequent reads of
   the affected table; preflight checks appear to "hang" when this
   happens. Future enhancement: detect blocked sessions in preflight.
8. When considering "database per source" vs "schemas per source":
   schemas win unless there's a specific operational reason for DB-level
   isolation (independent backup schedules, customer DB shipping, etc.).
   Cross-database joins, reference data duplication, and Express size
   caps make per-DB an expensive default.
