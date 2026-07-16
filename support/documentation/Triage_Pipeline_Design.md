# File Triage & Promotion Pipeline — Design Spec

**Project:** Data Wrangler · DataView v3
**Component:** File Catalog — triage, enrichment, promotion
**Status:** Draft for review
**Owner:** Perry Stokes

---

## 1. Purpose

Turn the raw file inventory into governed, well‑linked, high‑value catalog and vault
assets with as little manual effort as possible. Identity resolution and suitability
scoring run automatically and frequently; humans only touch the genuine exceptions.

The current per‑file grid work is replaced by a pipeline: **bulk set‑based triage →
small manual review of leftovers → one promotion step** (deep extraction → catalog →
vault).

---

## 2. Design principles

- **Set‑based, not per‑row.** Identity resolution is `UPDATE … FROM … JOIN` over the
  whole inventory, never a per‑file pyodbc loop.
- **Idempotent and frequent.** Triage only fills blanks and re‑scores, so it is safe to
  run nightly or on demand. As new files arrive carrying a name, UWI‑only wells pick up
  a name on the next pass — no rework.
- **Automate the bulk, review the rest.** Deterministic rules resolve the majority; the
  review grid only ever shows the ambiguous remainder.
- **Governance first.** Never overwrite a non‑blank value. Ambiguous matches are *flagged
  for review*, never silently guessed. Every fill records its source for audit.
- **AI is surgical and advisory.** Used only where rules fall short, gated by confidence
  and corroboration, and always overridable. Rules win ties.
- **Standalone and testable.** The engine is a CLI script that runs and is inspected
  outside Streamlit; the UI just triggers it and shows results.

---

## 3. Pipeline at a glance

| Stage | Name | Mode | Drives |
|------|------|------|--------|
| 0 | Inventory | automatic | crawl + scan (existing) |
| **1** | **Triage** | **automatic, frequent** | **identity enrichment + scoring + tiering (NEW — start here)** |
| 2 | Review | manual, exceptions only | scoped bucket grid |
| 3 | Promote | batch action | deep extraction → catalog load → vault |
| 4 | Golden | batch | `promote_catalog`: `cat_*` → `dv_*` (existing) |

```
Inventory ──► TRIAGE ──► (HIGH/READY) ───────────────► PROMOTE ──► Golden
              │  ▲          │                            │
              │  └──────────┘ re-run frequently          │
              └──► (REVIEW) ──► manual fix/reject ────────┘
              └──► (LOW/REJECT) ──► parked / blocklist
```

---

## 4. Data model

### Tables touched
- `GLOBAL_FILE_CATALOG` — the inventory (and triage flags, below).
- `FILE_WELL_HEADER` — `UWI`, `UWI14`, `WELL_NAME`, `TOTAL_DEPTH`, `SPUD_DATE`.
- `FILE_SEIS_HEADER` — `SURVEY_NAME`.
- `WELL_REF.well_ref.WELL_MASTER` — reference master (~4.7M, read‑only).
- `dv_well` — already‑promoted golden wells (read‑only lookup source).
- `cat_*` — capture mirrors (promotion target).
- `VAULT_FILE` — vault ledger.

### Proposed columns on `GLOBAL_FILE_CATALOG`
| Column | Type | Meaning |
|--------|------|---------|
| `VALUE_TIER` | varchar(10) | `HIGH` / `REVIEW` / `LOW` / `REJECT` |
| `TRIAGE_SCORE` | int | 0–100 from `catalog_rules.score_file` |
| `IDENTITY_SOURCE` | varchar(30) | how UWI/name was resolved (audit) |
| `TRIAGE_REASON` | varchar(200) | human‑readable why (e.g. "name→UWI ambiguous: 3 candidates") |
| `LAST_TRIAGED_AT` | datetime2 | for incremental re‑runs |

`CATALOG_READINESS` remains the lifecycle state (below); `VALUE_TIER` is the triage verdict.

---

## 5. Stage 1 — Triage (the focus)

Three sub‑steps, run in order, all set‑based and idempotent.

### 5a. Canonical UWI14
Refresh a normalized, digits‑only `UWI14` on `FILE_WELL_HEADER` from the dashed
`MATCHED_UWI` / header `UWI`. **All reference joins normalize both sides** so the
dashed‑vs‑digits mismatch can't cause silent misses:

```sql
-- normalize once; reference joins use the same expression
UPDATE h SET UWI14 = <norm14(UWI)>
FROM file_catalog.FILE_WELL_HEADER h
WHERE NULLIF(h.UWI14,'') IS NULL AND NULLIF(h.UWI,'') IS NOT NULL;
```
> **Resolved:** `WELL_MASTER.UWI14` is digits-only **API14** (14 chars, e.g.
> `34045605060000`). `norm14()` = strip non-digits, zero-pad API10/API12 to 14,
> truncate longer, and skip surrogate keys containing letters (e.g. `KGS_…`).
> Reference joins are then plain `UWI14 = UWI14` equality.

### 5b. Identity enrichment cascade
Each step **only fills blanks** and records `IDENTITY_SOURCE`.

**1) Cross‑fill from the inventory itself (cheapest, no reference scan).**
The catalog often already holds the answer in a sibling file for the same well.

```sql
-- name from a sibling that has the same UWI14 and a name
;WITH name_by_uwi AS (
  SELECT UWI14,
         MAX(WELL_NAME) AS WELL_NAME          -- or most-frequent (see open items)
  FROM file_catalog.FILE_WELL_HEADER
  WHERE NULLIF(WELL_NAME,'') IS NOT NULL AND NULLIF(UWI14,'') IS NOT NULL
  GROUP BY UWI14
)
UPDATE h SET WELL_NAME = n.WELL_NAME, IDENTITY_SOURCE = 'inv-xfill-name'
FROM file_catalog.FILE_WELL_HEADER h
JOIN name_by_uwi n ON h.UWI14 = n.UWI14
WHERE NULLIF(h.WELL_NAME,'') IS NULL;
```

```sql
-- UWI from a sibling with the same exact name — ONLY if that name maps to one UWI
;WITH uwi_by_name AS (
  SELECT WELL_NAME, MIN(UWI14) AS UWI14
  FROM file_catalog.FILE_WELL_HEADER
  WHERE NULLIF(UWI14,'') IS NOT NULL AND NULLIF(WELL_NAME,'') IS NOT NULL
  GROUP BY WELL_NAME
  HAVING COUNT(DISTINCT UWI14) = 1               -- collision guard
)
UPDATE h SET UWI14 = u.UWI14, IDENTITY_SOURCE = 'inv-xfill-uwi'
FROM file_catalog.FILE_WELL_HEADER h
JOIN uwi_by_name u ON h.WELL_NAME = u.WELL_NAME
WHERE NULLIF(h.UWI14,'') IS NULL;
```

**2) Reference fill — name from UWI (safe, deterministic).**
```sql
UPDATE h SET WELL_NAME = r.WELL_NAME, IDENTITY_SOURCE = 'ref-name-by-uwi'
FROM file_catalog.FILE_WELL_HEADER h
JOIN WELL_REF.well_ref.WELL_MASTER r ON h.UWI14 = r.UWI14
WHERE NULLIF(h.WELL_NAME,'') IS NULL AND NULLIF(r.WELL_NAME,'') IS NOT NULL;
```

**3) Reference fill — UWI from name (guarded per your rule).**
A name alone resolves only when it is an **exact match to exactly one well**, *or* is
**corroborated by TD or spud date**.

```sql
-- exact + unique
;WITH uniq AS (
  SELECT WELL_NAME, MIN(UWI14) UWI14
  FROM WELL_REF.well_ref.WELL_MASTER
  GROUP BY WELL_NAME HAVING COUNT(*) = 1
)
UPDATE h SET UWI14 = q.UWI14, IDENTITY_SOURCE = 'ref-name-unique'
FROM file_catalog.FILE_WELL_HEADER h
JOIN uniq q ON h.WELL_NAME = q.WELL_NAME
WHERE NULLIF(h.UWI14,'') IS NULL;
```
```sql
-- name not unique → require TD or spud corroboration
UPDATE h SET UWI14 = r.UWI14, IDENTITY_SOURCE = 'ref-name-corroborated'
FROM file_catalog.FILE_WELL_HEADER h
JOIN WELL_REF.well_ref.WELL_MASTER r ON h.WELL_NAME = r.WELL_NAME
WHERE NULLIF(h.UWI14,'') IS NULL
  AND ( ABS(ISNULL(h.TOTAL_DEPTH,-1) - ISNULL(r.TOTAL_DEPTH,-2)) <= 50
        OR h.SPUD_DATE = r.SPUD_DATE )
  AND NOT EXISTS (   -- still ambiguous after corroboration → leave for review
        SELECT 1 FROM WELL_REF.well_ref.WELL_MASTER r2
        WHERE r2.WELL_NAME = h.WELL_NAME AND r2.UWI14 <> r.UWI14
          AND ( ABS(ISNULL(h.TOTAL_DEPTH,-1) - ISNULL(r2.TOTAL_DEPTH,-2)) <= 50
                OR h.SPUD_DATE = r2.SPUD_DATE ) );
```

`dv_well` may be added as an additional fill source (already‑promoted wells) using the
same pattern.

### 5c. Scoring & tiering
- Content/quality score from `catalog_rules.score_file` (0–100) → `TRIAGE_SCORE`.
- **Tier rules:**
  - **HIGH** — has a resolvable **UWI** (a UWI alone is trustworthy and promote‑worthy;
    a missing name can arrive later). Seismic: has a `SURVEY_NAME`.
  - **REVIEW** — has a name but **no confident UWI** (ambiguous / uncorroborated), or
    other unresolved ambiguity.
  - **LOW** — no UWI, no name, low score.
  - **REJECT** — on the bad‑file blocklist, or score below floor.
- Map to `CATALOG_READINESS`: `READY` (HIGH) / `REVIEW` / `NEEDS_UWI` / `LOW` / `SKIPPED`.

### 5d. Idempotency & scheduling
- Re‑runnable any time; only blanks are filled and tiers re‑computed.
- `LAST_TRIAGED_AT` + a "dirty since" filter → incremental runs touch only new/changed
  or still‑unresolved files. Intended to run **frequently** (nightly job + on‑demand
  button).

---

## 6. Where AI helps (Stage 1, optional)

Deterministic rules resolve the clean majority. AI is reserved for the residue that
rules can't decide, and is **advisory** — auto‑applied only above a confidence threshold
*and* with corroboration; otherwise it annotates the file and routes it to REVIEW.

Candidate uses, highest‑value first:
1. **Fuzzy name → reference matching** (sentence‑transformers): catches punctuation /
   case / abbreviation / lease‑name variants that an exact join misses. Output is
   candidate + similarity; accept only with TD/spud corroboration.
2. **Name→UWI disambiguation** (Claude API): when several wells share a name, feed the
   file's extracted header text + the candidate wells and ask for the best match,
   a confidence, and a one‑line rationale. Low confidence → REVIEW.
3. **High‑value document classification**: from filename + a text snippet, label the
   document (final log vs. working copy vs. fax cover vs. junk) and a value signal, to
   sharpen tiering beyond the structural rules.
4. **Header rescue**: pull a well name / UWI from messy header text when regex fails.

Guardrails: rules‑first; AI only on the unresolved set (not the whole inventory, for cost
and latency); every AI decision logs candidate, confidence, and rationale; thresholds are
configurable; the feature is toggleable.

---

## 7. Stage 2 — Review (manual, scoped)

The existing bucket grid, **but scoped to `VALUE_TIER = REVIEW` only** — typically a few
hundred rows, not the whole inventory. Grouped by file type, ordered by completeness.
Per row: accept a surfaced reference/AI suggestion, hand‑enter UWI/well name/survey, or
reject. Edits write back to `FILE_WELL_HEADER` and re‑tier on save.

---

## 8. Stage 3 — Promote (batch, orchestrated)

For files flagged **HIGH/READY** (plus anything approved in review):
1. **Deep extraction** — `deep_catalog` full parse.
2. **Load to catalog** — capture into `cat_*`.
3. **Vault** — `vault_copy` (inventory‑filtered to the promoted set) + `VAULT_FILE` ledger.
4. Set `CATALOG_READINESS = CATALOGED` then `VAULTED`; record provenance.

Resumable, per‑file status + error capture. `promote_catalog` later lifts `cat_*` into the
`dv_*` golden tables (separate existing step).

> **Known promotion gaps to close** so HIGH files of every type actually load:
> LAS curve capture and SEG‑Y trace headers are not yet written by the catalog path;
> RFT / well‑test / DDR PDF loaders are `not_impl`. Tracked as backlog.

---

## 9. Lifecycle (CATALOG_READINESS)

```
NEW ──► TRIAGED ──► READY ───► CATALOGED ───► VAULTED
            │  └──► REVIEW ──(fix)──┘
            ├──► NEEDS_UWI ──(enriched later)──► READY
            ├──► LOW
            └──► SKIPPED / REJECT (blocklist)
```

---

## 10. Components

| Component | New? | Role |
|-----------|------|------|
| `triage_inventory.py` | **NEW** | CLI engine: normalize → cross‑fill → reference‑fill → score → tier. `--dry-run`, `--since`, `--limit`. |
| `catalog_rules.py` | exists | `score_file` content/quality scoring. |
| `ai_assist.py` | **NEW (opt)** | fuzzy match, name→UWI disambiguation, value classification. |
| `deep_catalog.py` | exists | deep extraction. |
| catalog capture (`cat_*`) | exists | load to mirrors. |
| `vault_copy.py` | exists | vault copy (inv‑filtered). |
| `promote_catalog.py` | exists | `cat_*` → `dv_*`. |
| `page_workbench.py` | exists | trigger triage, show counts, review grid, promote. |

---

## 11. Open decisions

1. `norm14()` format vs. `WELL_MASTER.UWI14` (confirm with the parked diagnostic).
2. When multiple sibling names exist for one UWI: **most‑frequent** vs. longest vs. newest.
3. AI provider cost/latency budget; cap AI to the REVIEW set only.
4. Confidence thresholds for auto‑apply vs. route‑to‑review.
5. Schedule: nightly job, on‑demand, or both.

---

## 12. Phased rollout

- **Phase 1 — `triage_inventory.py` (START HERE).** UWI14 normalization, inventory
  cross‑fill (name↔UWI), reference fill (name‑from‑UWI; UWI‑from‑name exact‑unique and
  TD/spud‑corroborated), scoring + tiering, `--dry-run` count mode. Pure set‑based SQL,
  testable standalone.
- **Phase 2 — Review grid** scoped to `REVIEW` tier, writing back + re‑tiering.
- **Phase 3 — Promote** orchestration (deep extract → `cat_*` → vault) over READY.
- **Phase 4 — AI assist** behind a toggle (fuzzy match + disambiguation + value class).
- **Phase 5 — Scheduling & incremental** runs; close LAS/SEG‑Y and RFT/well‑test/DDR
  loader gaps so every HIGH file promotes.
