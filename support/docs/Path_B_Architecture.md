# Path B: Document-as-Truth Architecture

**Date:** 2026-05-11
**Status:** Design — for review before implementation
**Context:** Pivot from manual-curation catalog to automatic provenance database

---

## The Core Insight

You had two competing mental models. Naming them helps.

**Path A (original):** A team of catalogers reviews every file and decides whether it goes into a curated catalog. Files are second-class; they must earn their place. Built around managers, assignments, audit logs, vault promotion.

**Path B (new):** Automatic extractors pull every spatial/contextual fact from every file. Documents are first-class evidence. The well master should be reconciled AGAINST the document evidence, not the other way around.

Path B is the right path because:

- It scales to millions of files without a team
- It surfaces evidence the team didn't know existed
- It captures truth from documents older than the master (old typewritten records, vendor surveys, drillers' reports)
- It allows the well master to be corrected from authoritative source documents
- It produces a separate, useful product: a provenance database

The architecture you've built so far is mostly Path A. Path B mostly reuses the components — the crawler is the crawler, extractors are extractors — but the workflow, governance layer, and scoring philosophy change.

---

## What Path B Looks Like

```
┌────────────────────────────────────────────────────────────────┐
│                                                                │
│   FILE SYSTEM                                                  │
│        │                                                       │
│        ▼                                                       │
│   ┌──────────────────────────────────────────────────────┐     │
│   │ STAGE 1: INVENTORY (fast)                             │     │
│   │ • Threaded crawler walks file system                  │     │
│   │ • Captures path, hash, size, modified date            │     │
│   │ • No file content parsed                              │     │
│   │ • Writes: file_catalog.GLOBAL_FILE_CATALOG            │     │
│   │ • Scales to millions of files                         │     │
│   └──────────────────────────────────────────────────────┘     │
│        │                                                       │
│        ▼                                                       │
│   ┌──────────────────────────────────────────────────────┐     │
│   │ STAGE 2: EXTRACTION (slower, parallel)                │     │
│   │ • Format-specific extractors:                         │     │
│   │     LAS, DLIS, LIS, SEG-Y (2D/3D), P190,              │     │
│   │     PDF, Shapefile, Word, Excel                       │     │
│   │ • Each extractor emits a normalised dict:             │     │
│   │     uwi, well_name, operator, lat, lon,               │     │
│   │     state, county, depth_range, doc_type, ...         │     │
│   │ • Writes:                                             │     │
│   │     file_catalog.FILE_WELL_HEADER  (well-bearing)     │     │
│   │     file_catalog.FILE_SEIS_HEADER  (seismic surveys)  │     │
│   │     GLOBAL_FILE_CATALOG updates  (score, status)      │     │
│   │ • Coordinate precision filter: keep ≥3-4 decimals     │     │
│   └──────────────────────────────────────────────────────┘     │
│        │                                                       │
│        ▼                                                       │
│   ┌──────────────────────────────────────────────────────┐     │
│   │ STAGE 3: AGGREGATION + VALIDATION                     │     │
│   │ • Reads from FILE_WELL_HEADER + FILE_SEIS_HEADER      │     │
│   │   + GLOBAL_FILE_CATALOG (any with lat/lon)            │     │
│   │ • Validates each extracted location:                  │     │
│   │     - Precision ≥3 decimals                           │     │
│   │     - Lat/lon falls inside doc's claimed state bbox   │     │
│   │     - County polygon contains lat/lon                 │     │
│   │     - Cross-check vs other docs nearby                │     │
│   │ • Computes confidence score per location              │     │
│   │ • Marks duplicates (multiple docs, same location)     │     │
│   │ • Writes: dataview.document_location                  │     │
│   └──────────────────────────────────────────────────────┘     │
│        │                                                       │
│        ▼                                                       │
│   ┌──────────────────────────────────────────────────────┐     │
│   │ STAGE 4: PRESENTATION                                 │     │
│   │ • Map overlay: document locations as purple diamonds  │     │
│   │ • Provenance browser: filter, sort, drill, view       │     │
│   │ • QC tools (replace Path A's manager UI):             │     │
│   │     - Inspect any location, see source file           │     │
│   │     - Open document in viewer                         │     │
│   │     - Accept / reject / merge locations               │     │
│   │     - Flag conflicts vs dv_well master                │     │
│   │ • Optional: promote curated locations to dv_well      │     │
│   └──────────────────────────────────────────────────────┘     │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

---

## What Carries Forward From Path A

Reuse, not rewrite:

- **`modules.file_inventory.crawl_and_inventory`** — the threaded crawler. Stage 1.
- **`modules.catalog_rules.extract_file_fields`** — the format dispatcher. Stage 2 (with extractor improvements).
- **`modules.catalog_rules.score_file`** — already partially shifted to document-centric scoring tonight. Keep refining.
- **`modules.file_summarizer.summarize`** — format-specific extractors (LAS, DLIS, PDF, etc.) currently called by `extract_file_fields`. These need work — see "What needs fixing" below.
- **`page_file_workbench.py`** — format-specific viewers. Stage 4 QC tool. Pure win, keep as-is.
- **`page_well_map.py`** — Phase 1 hex grid already shipped. Stage 4 surface. Add document overlay.
- **Database schemas** — `GLOBAL_FILE_CATALOG`, `FILE_WELL_HEADER`, `FILE_SEIS_HEADER` are well-designed. Don't change.

---

## What Gets Built New

Three new pieces:

### 1. `dataview.document_location` (table)
The Stage 3 output. One row per extracted (file, location) pair. See full DDL in next-session worklist.

### 2. `modules/doc_location.py` (module)
Stage 3 logic. Functions:
- `rebuild_document_locations(engine)` — reads sources, validates, populates table
- `validate_location(lat, lon, state, county, tiger_polygons)` — single-row validator
- `find_duplicates(engine, distance_m=50)` — clusters nearby points
- `compute_confidence(row, validation_flags)` — single 0-1 score

### 3. Path B UI (new page or new tabs)
Probably `page_provenance.py` — three tabs:
- **Inventory & Extract** — combines Stage 1 + Stage 2, replaces governance app's Scan tab
- **Provenance Browser** — Stage 3 output, with QC tools
- **Catalog Health** — readiness metrics, extraction coverage by format

---

## What Gets Removed

Path A debt that doesn't earn its keep in Path B:

- **User management** (`INVENTORY_USER`, password hashing, impersonation)
- **Group assignments** (`INVENTORY_GROUP`, `INVENTORY_GROUP_FILE`, `INVENTORY_ASSIGNMENT`)
- **Cataloger workbench** (the "My Work" tab — manual review of assigned files)
- **Manager assignment UI** (creating assignments, reassigning, removing)
- **SMTP notifications** (cataloger email notifications)
- **Audit log** (assignment changes, user actions)

These represent ~60-70% of `page_file_manager.py`'s ~3500 lines. Removing them produces a much simpler app.

If you ever DO want a multi-user team workflow later, the governance layer can be re-added as an optional plugin. But it's not the primary path anymore.

**Files to archive/delete:**
- `page_file_inventory.py` (3,284 lines) — old governance variant
- `page_file_inventory_gov.py` (3,409 lines) — old governance variant
- `page_file_catalog_v3.py` (3,414 lines) — old governance variant

Keep one of them in archive for reference, delete the rest. `page_file_manager.py` (3,492 lines) is the most current; we'll cannibalize the still-useful parts of it for the new app.

---

## What Needs Fixing in Extraction

Tonight's investigation revealed gaps. None blocking; all addressable.

| Format | Status | What's missing |
|---|---|---|
| LAS | Partial | `extract_las_fields()` reads lat/lon/state/county from headers. `extract_file_fields()` calls `summarize()` instead, which doesn't pass these fields through. Quick fix. |
| DLIS | Partial | Similar story to LAS — headers are there, dispatcher doesn't pass them. |
| LIS | Unknown | Need to check whether `summarize()` handles `.lis` and what it returns. |
| SEGY 2D/3D | Partial | Returns survey_name + sample_interval; not lat/lon. BBOX rarely populated (6 of 240 in real data). EBCDIC parsing may need expansion. |
| P190 | Unknown | Has `_view_p190` in workbench (header viewer); extraction path needs verification. |
| PDF | Partial | `classify_pdf` exists. Likely only catches well-shaped PDFs (scout tickets, formation tops). Generic PDFs with embedded coords aren't being mined. |
| Shapefile | None | `classify_shapefile` returns feature_type + ppdm_target but NO coordinates. For shapefiles, the geometry IS the value — each feature point should produce a row. Significant rebuild. |
| Word | Unknown | `summarize` claims to handle .docx/.doc. Behaviour with location-bearing Word docs is untested. |
| Excel | Unknown | Same — `summarize` claims to handle .xlsx/.xls. Most likely just reads the first sheet's first rows. |

Priority order (suggested):
1. **LAS lat/lon wiring** — biggest population of files, easiest fix
2. **Shapefile geometry extraction** — high value per file (hundreds of points each)
3. **DLIS lat/lon wiring** — same fix pattern as LAS
4. **PDF generic extraction** — broad applicability
5. **SEGY/P190 expansion** — less common but pure surface
6. **Word/Excel sanity check** — verify existing behaviour, expand if needed

---

## Open Questions for the Implementation Session

Don't need to answer now. Just flagging for the next session.

1. **One app or two?** Path B could be a single new `page_provenance.py` OR could split as `page_inventory.py` (Stage 1+2) and `page_provenance.py` (Stage 3+4). Single app is simpler; two-page may map better to user mental model.

2. **Map overlay integration.** Document locations on the well map page — separate toggle layer (current plan), or unified rendering with the grid (more complex, more powerful)?

3. **What about files with no lat/lon?** They have UWI, well_name, operator. They're useful but don't fit document_location. Do they need their own surface, or is "files matched to known UWI in dv_well" enough?

4. **Conflict resolution UX.** When a document says lat/lon X and dv_well master says lat/lon Y for the same UWI — what does the UI show, and how does the curator resolve?

5. **Promotion workflow.** When a curator accepts a document_location, does it write back to dv_well immediately, get queued for batch promotion, or just sit in document_location flagged as "accepted"?

---

## Suggested Build Order

Across multiple sessions, each scoped tightly:

**Session 1 — Foundation (2-3 hours)**
- Create `dataview.document_location` table + indexes
- Write `modules/doc_location.py` with rebuild + simple validation (precision only)
- Run rebuild against current data, inspect output
- Decide whether validation rules need adjustment

**Session 2 — Extraction Fixes Round 1 (2-3 hours)**
- Wire `extract_las_fields()` lat/lon/state/county through to `extract_file_fields()`
- Wire DLIS the same way
- Re-run extraction on cataloged files
- Compare document_location growth before/after

**Session 3 — Map Overlay (1-2 hours)**
- Add document overlay layer to `page_well_map.py`
- Toggle UI, purple diamond markers
- Click → file info panel + open-in-viewer button
- Wires into `page_file_workbench.py` viewer

**Session 4 — Provenance Browser (3-4 hours)**
- New `page_provenance.py` with tabs
- Filterable table of document_location rows
- Per-row actions: view file, accept, reject, mark duplicate
- Inspect-and-fix loop for low-confidence rows

**Session 5 — Shapefile Extraction (2-3 hours)**
- Rebuild shapefile extractor to emit per-feature locations
- New table or extended schema for multi-location files

**Session 6 — Path A Strip (1-2 hours)**
- Archive old governance variants
- Strip user/assignment/audit code from the current app
- Single-user simplification

**Session 7+ — Continued extractor improvements**
- PDF generic, SEGY, P190, Word, Excel
- Each as its own focused session

**Total estimated effort:** 15-25 hours across 7-8 sessions. Not a weekend project — a multi-week deliberate build.

---

## What This Looks Like When Done

You sit down at your computer and:

1. Open `page_inventory_extract.py`. Point it at a drive. Click Start. 100,000 files crawl + extract overnight. Coordinates are pulled from everything that has them.

2. Open `page_provenance.py` in the morning. See 20,000 new document locations. Filter by low-confidence to find suspicious ones. Filter by "conflicts with dv_well" to find places where documents disagree with the master.

3. Open `page_well_map.py`. Toggle "Document overlay" — purple diamonds appear scattered across the basin. Click one — see the source file, its claimed lat/lon, the well it claims to be. Open the source LAS file in the workbench viewer to verify.

4. If the document is more authoritative than dv_well, promote it. dv_well gets corrected.

No team of catalogers required. No assignment workflow. Just you, the data, and the architecture doing the work.

That's Path B.

---

## Bottom Line

Path B is the right architecture for what you actually need. Most of the components already exist — they just need to be reorganized around the new workflow, with extraction improvements and one new aggregation layer.

The next session starts with the document_location table and module. Small, contained, validates the architecture. From there we work outward.
