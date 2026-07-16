# Schema Cleanup for Path B

**Reviewed:** 2026-05-11, against `catalog_dataview.sql` (961 lines, 28 tables)

This document gives a verdict on every table currently in the catalog schemas, recommending **KEEP / REFACTOR / DROP** based on Path B (document-as-truth) architecture.

---

## TL;DR

- **Keep 9 tables.** Core extraction and per-format detail data.
- **Refactor 1 table.** `GLOBAL_FILE_CATALOG` has Path A debt in its columns that should be trimmed.
- **Drop 8 tables.** All governance/assignment/audit layer + some redundant detail tables.
- **Add 1 new table.** `dataview.document_location` (the aggregator output).

Net: 28 tables → 19 tables. ~30% schema reduction.

---

## file_catalog Schema

### KEEP (extraction core)

#### `file_catalog.GLOBAL_FILE_CATALOG` — Master file inventory
**Verdict: REFACTOR — trim Path A columns.**

The inventory table is sound but has Path A creep in its columns. After Path B trimming:

**Keep:**
- INVENTORY_ID (PK), FILE_PATH, FILE_NAME, FILE_EXT, FILE_SIZE_KB
- FILE_HASH, FILE_HASH_FULL, DUPLICATE_GROUP
- MODIFIED_DATE, SCAN_DATE, ROOT_PATH, FILE_TYPE_GROUP
- ROW_CREATED_DATE, ROW_CHANGED_DATE, FLAG_DELETE
- HEADER_EXTRACTED (Y/N flag, drives Phase 2)
- DOC_TYPE, REPORT_TYPE (classifier output)

**Drop these Path A columns:**
- CATALOG_STATUS, CATALOG_TABLE — assignment-workflow leftovers
- PPDM_LOADED_IND, PPDM_TABLE_TARGET — not used in Path B
- SOURCE, ROW_CREATED_BY — Path A bookkeeping
- CATALOG_SCORE, CATALOG_READINESS, MATCHED_UWI, MATCH_METHOD, CATALOG_ISSUES — these are scoring outputs that belong in their own table or in FILE_WELL_HEADER

**Move these to FILE_WELL_HEADER (where they actually belong):**
- WELL_NAME, UWI, OPERATOR, PAGE_COUNT, RECORD_COUNT, SUMMARY_DESCRIPTION — duplication with FILE_WELL_HEADER. Inventory shouldn't carry per-file extracted content.

That cleanup reduces the table from ~35 columns to ~17. Cleaner, faster, single-purpose.

#### `file_catalog.FILE_WELL_HEADER` — Per-file well metadata
**Verdict: KEEP.** 

This is the primary extraction output for well-bearing files. Schema is good. Path B leans on this heavily.

**Add columns to support Path B:**
- COORD_PRECISION (computed TINYINT, decimal places of LAT/LONG)
- VALIDATION_STATUS (extracted/flagged/reviewed)
- STATE_BBOX_OK BIT (does coord fall in state's TIGER bbox?)
- COUNTY_MATCH_OK BIT (does county polygon contain coord?)

Or — these could live in `document_location` instead of here. Decision: keep `FILE_WELL_HEADER` as raw extraction output, put validation flags in `document_location`. Cleaner separation.

**Change column types:**
- LATITUDE, LONGITUDE are currently `nvarchar(30)` — should be `decimal(11, 7)`. Numeric storage avoids parse-on-read overhead. Migration: convert existing data first.

#### `file_catalog.FILE_SEIS_HEADER` — Per-file seismic metadata
**Verdict: KEEP.**

Same role as FILE_WELL_HEADER but for seismic surveys (2D/3D SEG-Y, P190). Schema fits Path B fine.

Has BBOX_MIN/MAX_LAT/LON which IS the spatial fact for seismic. Good.

Note: 240 rows but only 6 with bbox — extraction is failing for most. That's a Path B fix-up item (improve SEG-Y / P190 extractors).

#### `file_catalog.FILE_CURVE` — LAS curve names per file
**Verdict: KEEP.**

Curve metadata (mnemonic, unit, description). Useful for the workbench viewer and curve-based search. Not specifically Path A.

But: redundant with `las_catalog.LAS_FILE_CURVE`? Two tables tracking the same thing in different schemas. See "WL_* schema" section below.

### REFACTOR

(none beyond GLOBAL_FILE_CATALOG above)

### DROP (Path A debt)

#### `file_catalog.FILE_HEADER` — Older extraction table
**Verdict: DROP.**

Looks like a predecessor to `FILE_WELL_HEADER`. Has `HEADER_TEXT` (raw text), `MATCH_*` (Path A scoring), `CATALOGED_BY` (governance). 

If any data lives here that's not in FILE_WELL_HEADER, migrate it first. Then drop.

#### `file_catalog.WELL_HEADER_STAGING`
**Verdict: DROP.**

Empty per your earlier check. Path A workflow expected catalogers to upload staged data; Path B has direct extraction. Not needed.

#### `file_catalog.SEIS_HEADER_STAGING`
**Verdict: DROP.**

Same as WELL_HEADER_STAGING. Path A staging that Path B doesn't need.

#### `file_catalog.INVENTORY_USER` — User accounts
**Verdict: DROP.**

Path A governance. Single-user Path B doesn't need user management. Strip when you strip the governance code.

#### `file_catalog.INVENTORY_GROUP` — Cataloger work groups
**Verdict: DROP.**

Same — Path A groupings of files for assignment. Not Path B.

#### `file_catalog.INVENTORY_GROUP_FILE` — Files in a group
**Verdict: DROP.**

Path A assignment plumbing.

#### `file_catalog.INVENTORY_ASSIGNMENT` — Cataloger assignments
**Verdict: DROP.**

Path A core. Drop with the rest of the governance layer.

#### `file_catalog.ASSIGNMENT_EXTENSION` — Due date extensions
**Verdict: DROP.**

Path A workflow detail.

#### `file_catalog.AUDIT_LOG` — Audit trail
**Verdict: DROP.**

Tracks user actions on the governance app. No equivalent need in Path B.

#### `file_catalog.INVENTORY_SETTING` — Key-value settings store
**Verdict: KEEP (probably).**

Generic settings table. Could be used by Path B for persisting paths, defaults, last-crawl-time. Worth keeping if you're using it; renaming to `CATALOG_SETTING` would be cleaner since "INVENTORY_*" prefix is Path A.

---

## las_catalog Schema

This schema is doing something different — it's a **WL_REPOSITORY-driven catalog** for log files with full curve/parameter detail. Probably from an earlier iteration of the project.

### KEEP (per-format detail tables)

These are well-designed and capture per-format detail that the file_catalog schema doesn't. Path B can use them.

#### `las_catalog.LAS_FILE` + `LAS_FILE_CURVE` + `LAS_FILE_PARAMETER`
**Verdict: KEEP.**

LAS-specific schema: file metadata + all curves + all parameters. This is RICHER than what FILE_CURVE captures. If your LAS extractor populates these, great — they support deep curve-level queries.

But: redundant with `file_catalog.FILE_WELL_HEADER` + `FILE_CURVE`? Need to decide which is the source of truth. My recommendation: **las_catalog** is the detailed LAS catalog; **file_catalog.FILE_WELL_HEADER** is the unified extraction summary. Both exist, they don't conflict.

#### `las_catalog.DLIS_FILE` + `DLIS_LOGICAL_FILE` + `DLIS_FRAME` + `DLIS_CHANNEL` + `DLIS_PARAMETER`
**Verdict: KEEP.**

DLIS-specific schema. Same role as LAS_* tables but for DLIS. Required for DLIS extraction.

#### `las_catalog.LIS_FILE` + `LIS_CHANNEL`
**Verdict: KEEP.**

LIS-specific schema. Smaller than DLIS but parallel role.

#### `las_catalog.SEIS_FILE_CATALOG` + `SEIS_FILE_HEADER`
**Verdict: KEEP.**

Seismic-specific catalog with FULL bbox/coord-system/inline/crossline metadata. Much richer than FILE_SEIS_HEADER. If your SEG-Y/P190 extractor populates these, keep them.

Confusing overlap: `file_catalog.FILE_SEIS_HEADER` and `las_catalog.SEIS_FILE_CATALOG` are doing similar things. Decision similar to LAS: **las_catalog** has full detail; **file_catalog.FILE_SEIS_HEADER** has the unified summary. Keep both, document the relationship.

### MAYBE-DROP

#### `las_catalog.WL_REPOSITORY` — Repository definitions
**Verdict: KEEP (low cost), or migrate to INVENTORY_SETTING (cleaner).**

Stores root paths and repository metadata. Functionally similar to `INVENTORY_SETTING.ROOT_PATH` keys. Probably keep as-is — it works, and it's used by `WL_FILE_UWI_MAP`.

#### `las_catalog.WL_FILE_UWI_MAP` — File-to-UWI mapping
**Verdict: REFACTOR or DROP.**

Maps files to UWIs with match score. Looks like the original Path A "match this file to a well" idea. Path B does this differently — the file's extracted UWI IS the file's UWI, and `document_location` tracks whether it matches dv_well.

If populated and useful for queries, keep. Otherwise drop in favour of `FILE_WELL_HEADER.UWI` + `MATCHED_UWI` columns.

---

## What gets ADDED for Path B

#### `dataview.document_location` (NEW)
**Verdict: CREATE.**

The Stage 3 aggregator output. Will be specified in detail at Session 1 start. Strawman from architecture doc:

```sql
CREATE TABLE dataview.document_location (
    doc_loc_id          BIGINT IDENTITY PRIMARY KEY,
    inventory_id        NVARCHAR(40) NOT NULL,
    source_table        VARCHAR(50) NOT NULL,
    latitude            DECIMAL(11, 7) NOT NULL,
    longitude           DECIMAL(11, 7) NOT NULL,
    coord_precision     TINYINT,
    file_path           NVARCHAR(1000),
    file_format         NVARCHAR(20),
    doc_type            NVARCHAR(100),
    uwi_in_doc          NVARCHAR(40),
    well_name_in_doc    NVARCHAR(255),
    operator_in_doc     NVARCHAR(255),
    state_in_doc        NVARCHAR(50),
    county_in_doc       NVARCHAR(100),
    precision_ok        BIT,
    state_bbox_ok       BIT,
    county_match_ok     BIT,
    duplicate_of        BIGINT NULL,
    confidence          DECIMAL(5, 4),
    curation_status     NVARCHAR(20) DEFAULT 'extracted',
    curated_by          NVARCHAR(100),
    curated_date        DATETIME2,
    curation_notes      NVARCHAR(MAX),
    promoted_to_well_id BIGINT NULL,
    promoted_date       DATETIME2,
    row_created_date    DATETIME2 DEFAULT SYSUTCDATETIME(),
    row_changed_date    DATETIME2 DEFAULT SYSUTCDATETIME()
);
CREATE INDEX IX_docloc_inv ON dataview.document_location (inventory_id);
CREATE INDEX IX_docloc_coords ON dataview.document_location (latitude, longitude);
CREATE INDEX IX_docloc_curation ON dataview.document_location (curation_status);
```

Lives in `dataview` schema (not `file_catalog`) because it's a curated, validated location product — different conceptually from raw file extraction.

---

## Suggested Cleanup Order

When you're ready to execute (later session, not now):

**Phase 1 — Safe additions:**
1. Create `dataview.document_location` (no risk, additive)
2. Add columns to `FILE_WELL_HEADER`: COORD_PRECISION, type-cast LATITUDE/LONGITUDE to decimal
3. Migrate existing string lat/lon to decimal

**Phase 2 — Path A code removal:**
4. Strip governance code from `page_file_manager.py` → produces simplified single-user version
5. Test that nothing in remaining code uses INVENTORY_* tables

**Phase 3 — Drop Path A tables:**
6. DROP `file_catalog.ASSIGNMENT_EXTENSION`
7. DROP `file_catalog.AUDIT_LOG`
8. DROP `file_catalog.INVENTORY_ASSIGNMENT`
9. DROP `file_catalog.INVENTORY_GROUP`
10. DROP `file_catalog.INVENTORY_GROUP_FILE`
11. DROP `file_catalog.INVENTORY_USER`
12. DROP `file_catalog.WELL_HEADER_STAGING`
13. DROP `file_catalog.SEIS_HEADER_STAGING`
14. DROP `file_catalog.FILE_HEADER` (after migrating any data to FILE_WELL_HEADER)

**Phase 4 — Refactor GLOBAL_FILE_CATALOG:**
15. Drop unused columns from GLOBAL_FILE_CATALOG (CATALOG_STATUS, PPDM_*, MATCH_*, etc.)
16. Move extracted-content columns (WELL_NAME, UWI, OPERATOR) to FILE_WELL_HEADER

**Phase 5 — Cleanup las_catalog overlap (optional):**
17. Decide on WL_FILE_UWI_MAP keep-or-drop based on actual usage
18. Document relationship between file_catalog.* and las_catalog.* tables

---

## Open Questions to Resolve First

Before any DROPs, verify with SSMS that these are safe:

```sql
-- Q1: Are the governance tables actually empty?
USE DataView;
SELECT 'INVENTORY_USER' AS tbl, COUNT(*) FROM file_catalog.INVENTORY_USER UNION ALL
SELECT 'INVENTORY_GROUP', COUNT(*) FROM file_catalog.INVENTORY_GROUP UNION ALL
SELECT 'INVENTORY_GROUP_FILE', COUNT(*) FROM file_catalog.INVENTORY_GROUP_FILE UNION ALL
SELECT 'INVENTORY_ASSIGNMENT', COUNT(*) FROM file_catalog.INVENTORY_ASSIGNMENT UNION ALL
SELECT 'ASSIGNMENT_EXTENSION', COUNT(*) FROM file_catalog.ASSIGNMENT_EXTENSION UNION ALL
SELECT 'AUDIT_LOG', COUNT(*) FROM file_catalog.AUDIT_LOG UNION ALL
SELECT 'WELL_HEADER_STAGING', COUNT(*) FROM file_catalog.WELL_HEADER_STAGING UNION ALL
SELECT 'SEIS_HEADER_STAGING', COUNT(*) FROM file_catalog.SEIS_HEADER_STAGING UNION ALL
SELECT 'FILE_HEADER', COUNT(*) FROM file_catalog.FILE_HEADER;

-- Q2: Is the las_catalog actually populated?
SELECT 'LAS_FILE' AS tbl, COUNT(*) FROM las_catalog.LAS_FILE UNION ALL
SELECT 'DLIS_FILE', COUNT(*) FROM las_catalog.DLIS_FILE UNION ALL
SELECT 'LIS_FILE', COUNT(*) FROM las_catalog.LIS_FILE UNION ALL
SELECT 'SEIS_FILE_CATALOG', COUNT(*) FROM las_catalog.SEIS_FILE_CATALOG UNION ALL
SELECT 'WL_REPOSITORY', COUNT(*) FROM las_catalog.WL_REPOSITORY UNION ALL
SELECT 'WL_FILE_UWI_MAP', COUNT(*) FROM las_catalog.WL_FILE_UWI_MAP;
```

If governance tables have rows, we want to check whether any of that data is worth preserving before drop.

If las_catalog tables are empty, the question becomes: are you planning to populate them, or has that path been abandoned in favour of file_catalog.FILE_WELL_HEADER as the single source of truth?

---

## Summary Table

| Table | Verdict | Notes |
|---|---|---|
| `file_catalog.GLOBAL_FILE_CATALOG` | REFACTOR | Trim Path A columns |
| `file_catalog.FILE_WELL_HEADER` | KEEP | Add coord_precision, type-cast LAT/LONG |
| `file_catalog.FILE_SEIS_HEADER` | KEEP | Improve extractor to populate bbox |
| `file_catalog.FILE_CURVE` | KEEP | Curve metadata for workbench |
| `file_catalog.FILE_HEADER` | DROP | Predecessor of FILE_WELL_HEADER |
| `file_catalog.WELL_HEADER_STAGING` | DROP | Empty, Path A staging |
| `file_catalog.SEIS_HEADER_STAGING` | DROP | Empty, Path A staging |
| `file_catalog.INVENTORY_USER` | DROP | Path A governance |
| `file_catalog.INVENTORY_GROUP` | DROP | Path A governance |
| `file_catalog.INVENTORY_GROUP_FILE` | DROP | Path A governance |
| `file_catalog.INVENTORY_ASSIGNMENT` | DROP | Path A governance |
| `file_catalog.ASSIGNMENT_EXTENSION` | DROP | Path A governance |
| `file_catalog.AUDIT_LOG` | DROP | Path A governance |
| `file_catalog.INVENTORY_SETTING` | KEEP | Generic settings table |
| `las_catalog.LAS_FILE` | KEEP | LAS detail catalog |
| `las_catalog.LAS_FILE_CURVE` | KEEP | LAS curve detail |
| `las_catalog.LAS_FILE_PARAMETER` | KEEP | LAS parameters detail |
| `las_catalog.DLIS_FILE` | KEEP | DLIS detail catalog |
| `las_catalog.DLIS_LOGICAL_FILE` | KEEP | DLIS logical file detail |
| `las_catalog.DLIS_FRAME` | KEEP | DLIS frame detail |
| `las_catalog.DLIS_CHANNEL` | KEEP | DLIS channel detail |
| `las_catalog.DLIS_PARAMETER` | KEEP | DLIS parameter detail |
| `las_catalog.LIS_FILE` | KEEP | LIS detail catalog |
| `las_catalog.LIS_CHANNEL` | KEEP | LIS channel detail |
| `las_catalog.SEIS_FILE_CATALOG` | KEEP | Rich seismic detail |
| `las_catalog.SEIS_FILE_HEADER` | KEEP | Seismic header text |
| `las_catalog.WL_FILE_UWI_MAP` | EVALUATE | Drop if redundant with FILE_WELL_HEADER |
| `las_catalog.WL_REPOSITORY` | KEEP | Repository definitions |
| `dataview.document_location` | CREATE | NEW — Path B aggregator output |

**Final count:** 19 tables in active use, 1 new = 20 total (down from 28).
