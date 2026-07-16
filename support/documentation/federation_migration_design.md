# DataView v3 — Per-Source Schema + Federation Migration (v3 design)

**Corrected identity model:** Cross-source identity is a SHA1_40 hash of
`(source, native_key)`. Native keys are stable within a source; UWIs are
inconsistent across sources and change over time, so UWI is carried as a
reference attribute, never as identity.

Each source loads into its own schema **in its original native form**.
A **federation view exposes the COMMON columns + the identity hash** across
sources for the map / search / selection. **Drill-down** to any well's full
native record by source+native_key → generic key-value display.

**Status:** DESIGN — no code changes yet.

---

## 1. Two-layer architecture

```
LAYER 1 — SOURCE SCHEMAS (native, full fidelity, untouched)
  dataview_kgs.well     ← KGS in its ORIGINAL 43-col shape (KID = native key)
  dataview_gom.well     ← GoM in its ORIGINAL BOEM shape (well_id = native key)
  dataview_tx.well      ← Texas RRC native (future)
  …
  Each source's PK = uwi_hash = SHA1_40('<SOURCE>|' + <native_key>)
  Stored as a fixed 40-char column. Carries:
    - uwi_hash       (PK, identity)
    - native_key     (e.g. KID, well_id) — for drill-down lookup
    - native_uwi     (the source's UWI attribute — reference only, not key)
    - … every other native column ...
    - h3_r4..r7, h3_coord_hash (backfilled post-load)
        │
        │  (each contributes a COMMON projection)
        ▼
LAYER 2 — FEDERATION VIEW (thin common denominator, federation IDENTITY)
  dataview_federation.v_well
    = UNION ALL across sources, projecting uwi_hash + COMMON cols + drill-down
      coordinates (source_schema, native_key_col, native_key) + source label
        │
        ├── v_well_density_r4..r7  (H3 aggregations over v_well)
        ▼
  APP queries ONLY v_well (map/cluster/search/bbox-drill).
  DRILL-DOWN: federated row carries source_schema + native_key_col +
              native_key → SELECT * FROM <schema>.well WHERE <key_col>=<val>
              → generic key-value display of full native record.
```

---

## 2. Identity model — SHA1_40 hash

### Why hash, not UWI
- UWIs are inconsistent across states (TX uses 14-digit composites, KGS uses
  10-digit KID, etc.) — collision risk across sources.
- UWIs change over time (rebuilds, format revisions) — joins break.
- KGS data specifically: UWI column has duplicates and missing values; KID
  is the only clean native key.
- A deterministic hash of (source, native_key) is unique across all sources
  AND stable across UWI churn. This is the well's identity, full stop.

### The hash
```
uwi_hash = LOWER(CONVERT(VARCHAR(40),
                  HASHBYTES('SHA1', <source> + '|' + <native_key>), 2))
```
- 40 hex chars (160-bit SHA1)
- `<source>` is the literal source label ('KGS', 'GOM_BOEM', 'TX_RRC')
- `<native_key>` is whatever the source's stable PK is (KID, well_id, …)
- The pipe separator prevents accidental collisions between e.g.
  'KGS' + '01' vs 'KG' + 'S01'
- Matches the SHA1_40 pattern already used in the FK Resolution / ETL
  pipeline — consistent identity across the codebase.

### Child tables (tops, logs, surveys, ...) — same hash
All child tables key on `uwi_hash` (not raw UWI). This is what prevents the
orphan-tops problem we hit: hashes are stable across reloads as long as
(source, native_key) is stable, which is the entire point of using the
native key. If KGS's UWI for a well changes between loads, the hash stays
the same because KID didn't change → tops still join.

---

## 3. Common projection (federation view columns)

What every source must supply to v_well. Lean by design.

```
-- Identity
uwi_hash         CHAR(40)    -- SHA1_40(source + '|' + native_key)
source           VARCHAR     -- 'KGS', 'GOM_BOEM', 'TX_RRC', …

-- Drill-down coordinates (so the app can fetch the native record)
source_schema    VARCHAR     -- 'dataview_kgs', 'dataview_gom', …
native_key_col   VARCHAR     -- 'kid', 'well_id', …  (the PK column NAME)
native_key       VARCHAR     -- the PK value (e.g. '1001184201')

-- Reference identifiers (carried but NOT identity)
native_uwi       VARCHAR     -- the source's UWI attribute, as-is
api_num          VARCHAR     -- common across many sources (dashed or no)

-- Common well attributes
well_name        VARCHAR
operator_name    VARCHAR     -- GoM: company_name; KGS: CURR_OPERATOR
well_status      VARCHAR
well_type        VARCHAR
field_name       VARCHAR     -- KGS: FIELD; GoM: derived/null
county           VARCHAR     -- may be NULL offshore; KGS: derive from API
province_state   VARCHAR     -- 'KS', 'GOM', …
country          VARCHAR     -- 'USA'

-- Spatial
surface_latitude   NUMERIC
surface_longitude  NUMERIC

-- H3 (every source backfills)
h3_r4 / h3_r5 / h3_r6 / h3_r7   CHAR(15)
```

---

## 4. Federation view (mapping happens HERE)

```sql
CREATE OR ALTER VIEW dataview_federation.v_well AS
-- KGS arm
SELECT
    k.uwi_hash,                            -- already computed at load
    'KGS'                       AS source,
    'dataview_kgs'              AS source_schema,
    'kid'                       AS native_key_col,
    k.kid                       AS native_key,
    k.native_uwi                AS native_uwi,
    k.api_number                AS api_num,
    k.lease_well_name           AS well_name,   -- or LEASE + ' ' + WELL
    k.curr_operator             AS operator_name,
    k.status                    AS well_status,
    NULL                        AS well_type,   -- KGS has no direct type col
    k.field                     AS field_name,
    -- county derived from API county FIPS (positions 4-6 of API_NUMBER)
    NULL                        AS county,      -- or LOOKUP via FIPS
    'KS'                        AS province_state,
    'USA'                       AS country,
    k.latitude                  AS surface_latitude,
    k.longitude                 AS surface_longitude,
    k.h3_r4, k.h3_r5, k.h3_r6, k.h3_r7
FROM dataview_kgs.well k

UNION ALL

-- GoM arm
SELECT
    g.uwi_hash,                            -- computed during reload (see §7.2)
    'GOM_BOEM'                  AS source,
    'dataview_gom'              AS source_schema,
    'well_id'                   AS native_key_col,
    CAST(g.well_id AS VARCHAR)  AS native_key,
    NULL                        AS native_uwi,  -- GoM has no UWI attr
    g.api_well_number           AS api_num,
    CONCAT(g.well_name, ' ',
           ISNULL(g.well_name_suffix,''))    AS well_name,
    g.company_name              AS operator_name,
    g.status_code               AS well_status,
    g.type_code                 AS well_type,
    NULL                        AS field_name,
    NULL                        AS county,
    'GOM'                       AS province_state,
    'USA'                       AS country,
    g.surface_latitude, g.surface_longitude,
    g.h3_r4, g.h3_r5, g.h3_r6, g.h3_r7
FROM dataview_gom.well g;
```

Rules:
- Every arm SELECTs the SAME columns, same order, same types (CAST to align).
- Identity (`uwi_hash`) is computed **at load time** in each source schema's
  table and stored — the view just projects it. Reasons:
    1. View-time HASHBYTES would recompute 514K times every query → slow.
    2. Indexable: uwi_hash gets a unique index in each source schema, used
       for child-table joins.
- Native columns NOT in the common projection are simply omitted here —
  they live in the source schema and surface only on drill-down.

---

## 5. KGS source schema (worked example)

The original KGS file profile (confirmed from `ks_wells.txt`, 514,713 rows):
- 43 columns, comma-delimited, double-quoted
- KID: 10-digit, 100% unique, 0 nulls → native key
- LATITUDE/LONGITUDE: 50 nulls (0.01%), 0 out-of-range
- 105 distinct county FIPS in API_NUMBER (all KS counties)
- Dates in DD-MON-YYYY format (Oracle-style) → parse to SQL date

```sql
CREATE TABLE dataview_kgs.well (
    -- Identity (computed at load)
    uwi_hash             CHAR(40)       NOT NULL,    -- SHA1_40('KGS|' + kid)
    -- Native key (the KGS-stable identifier)
    kid                  VARCHAR(10)    NOT NULL,
    -- Native attributes (43 source columns, native names preserved)
    api_number           VARCHAR(20),    -- e.g. '15-007-20094'
    api_num_nodash       VARCHAR(20),
    lease                VARCHAR(100),
    well                 VARCHAR(50),
    field                VARCHAR(100),
    latitude             NUMERIC(10,7),
    longitude            NUMERIC(11,7),
    long_lat_source      VARCHAR(50),
    township             VARCHAR(10),
    twn_dir              CHAR(1),
    [range]              VARCHAR(10),
    range_dir            CHAR(1),
    section              VARCHAR(10),
    spot                 VARCHAR(30),
    feet_north           INT,
    feet_east            INT,
    foot_ref             VARCHAR(10),
    orig_operator        VARCHAR(100),
    curr_operator        VARCHAR(100),
    elevation            NUMERIC(8,2),
    elev_ref             VARCHAR(20),
    surface_elev_lidar   NUMERIC(10,4),
    depth                NUMERIC(8,2),
    formation_at_td      VARCHAR(50),
    produce_form         VARCHAR(50),
    ip_oil               NUMERIC(12,2),
    ip_gas               NUMERIC(14,2),
    ip_water             NUMERIC(12,2),
    permit               DATE,
    spud                 DATE,
    completion           DATE,
    plugging             DATE,
    modified             DATE,
    oil_kid              VARCHAR(20),
    oil_dor_id           VARCHAR(20),
    gas_kid              VARCHAR(20),
    gas_dor_id           VARCHAR(20),
    kcc_permit           VARCHAR(20),
    status               VARCHAR(20),
    status2              VARCHAR(100),
    comments             NVARCHAR(MAX),
    lease_well_name      VARCHAR(150),
    -- Reference UWI attribute (untrusted, kept for reference)
    native_uwi           VARCHAR(20)    NULL,   -- KGS doesn't supply a UWI
                                                 -- column; could leave NULL
                                                 -- or use api_number as proxy
    -- H3 (backfilled post-load)
    h3_r4                CHAR(15),
    h3_r5                CHAR(15),
    h3_r6                CHAR(15),
    h3_r7                CHAR(15),
    h3_coord_hash        BINARY(8),
    CONSTRAINT pk_kgs_well PRIMARY KEY CLUSTERED (uwi_hash)
);
CREATE UNIQUE INDEX ix_kgs_well_kid    ON dataview_kgs.well (kid);
CREATE INDEX ix_kgs_well_h3_r5         ON dataview_kgs.well (h3_r5);
CREATE INDEX ix_kgs_well_h3_r6         ON dataview_kgs.well (h3_r6);
CREATE INDEX ix_kgs_well_coords        ON dataview_kgs.well (latitude, longitude);
```

Note: the KGS file has NO separate UWI column. The earlier pipe-delimited
version did; this comma version doesn't. `native_uwi` therefore stays NULL
for KGS, or you can fill it from `api_number` if useful (still just a
reference attribute, not identity).

---

## 6. Loader contract (each source, very thin)

Per-source, all that's required:
1. Read raw native format → stage into `dataview_<src>._stg_well` in native
   shape.
2. **Compute `uwi_hash` = SHA1_40('<SOURCE>|' + native_key)** at INSERT time.
3. INSERT staging → `dataview_<src>.well` with uwi_hash + native cols.
4. Validate: PK uniqueness on uwi_hash; lat/lon ranges; date parse success
   rate; report orphans (rows that failed the hash because native_key was
   NULL/empty).
5. Backfill h3_r4..r7 + h3_coord_hash from latitude/longitude.

No conform mapping. No canonical-column squeeze. Each source's table = its
native columns + uwi_hash + h3.

---

## 7. Drill-down (full native record)

```python
def fetch_native_record(engine, source_schema, key_col, key_val) -> dict:
    # source_schema and key_col come from the federation view — values are
    # whitelisted (we know every schema/col we ship). Validate against the
    # registered set before f-string interpolation as a SQL-injection guard.
    _SAFE_SCHEMAS = {'dataview_kgs', 'dataview_gom', 'dataview_tx'}
    _SAFE_KEYCOLS = {'kid', 'well_id'}
    if source_schema not in _SAFE_SCHEMAS or key_col not in _SAFE_KEYCOLS:
        raise ValueError(f"Bad drill-down target: {source_schema}.{key_col}")
    sql = text(f"SELECT * FROM {source_schema}.well WHERE {key_col} = :v")
    row = engine.connect().execute(sql, {"v": key_val}).mappings().fetchone()
    return dict(row) if row else {}
```
- Returns ALL native columns as an ordered dict for key-value display.
- Same code path for every source.
- Curated per-source layouts can come later; the generic display is the
  floor.

---

## 8. App repoint checklist (page_well_map.py)

- [ ] _qry_wells_bcp / _qry_wells_in_bbox / _qry_well_grid / _qry_well_count_near
      → query dataview_federation.v_well (filter by `source`).
- [ ] H3 density → rebuilt v_well_density_* on v_well.
- [ ] RETIRE _qry_gom_wells_bcp / _qry_gom_wells_in_bbox / _qry_gom_well_grid.
- [ ] Tray identity = uwi_hash. Native_key + source carried alongside for
      drill-down. The current `uwi`-vs-`well_id` special-case can be retired
      (the hash unifies them). Leave the fallback in for defensive reasons.
- [ ] Marker click / scout ticket → look up source_schema + native_key_col +
      native_key from the federated row → call fetch_native_record →
      generic key-value display.
- [ ] Area dropdown = `source` filter on ONE view:
        — Select source —   []           (cold start: load nothing)
        🌾 Kansas (KGS)      ['KGS']
        🌊 Gulf (GOM)        ['GOM_BOEM']
        🌎 All sources       []  (no filter)
- [ ] GoM-specific render style (amber ring) optional — key it on `source`,
      not a separate data path.

---

## 9. Build / reload sequence (DataView_Test first)

1. **KGS** — reload from original `ks_wells.txt`:
   a. CREATE dataview_kgs + well table (§5 DDL).
   b. Loader: read CSV → stage → INSERT with uwi_hash computed
      (SHA1('KGS|' + kid)) → backfill h3.
   c. Verify: 514,713 rows, uwi_hash unique, ~50 null-coord rows expected,
      h3 populated.
2. **GoM** — add uwi_hash to existing dataview_gom.well:
   a. ALTER TABLE add uwi_hash CHAR(40); compute SHA1('GOM_BOEM|' +
      CAST(well_id AS VARCHAR)) UPDATE; add unique index.
   b. No data reload needed — GoM stays native, just gets the identity col.
3. Build dataview_federation.v_well (KGS arm + GoM arm).
4. Rebuild density views on v_well.
5. Repoint app (§8); test cold start, KGS load, GoM load, All-sources,
   bbox drill (one path, both sources via uwi_hash), grid, H3, drill-down.
6. Remove KGS rows from dataview.dv_well (now homed in dataview_kgs).
7. Promote to DataView (prod).

---

## 10. Open / decided
- ✅ Identity: SHA1_40(source + '|' + native_key), stored at load.
- ✅ Federation exposes hash + native_key + native_uwi (when present).
- ✅ KGS native_uwi: NULL (no UWI column in current file). api_number kept
     as api_num in federation projection.
- ✅ KGS loader: reload from ks_wells.txt; no in-place migration from dv_well.
- ✅ GoM: keep native shape; ALTER add uwi_hash; no reload.
- ☐ County derivation for KGS (from API FIPS) — defer; NULL acceptable
     initially. The FIPS lookup table can be added later.
- ☐ Date parsing: KGS dates are 'DD-MON-YYYY' (Oracle-style) — loader uses
     `TRY_CONVERT(DATE, val, 106)` or Python parse.
- ☐ Index strategy on federation view performance — confirm bbox + source
     filter pushes into each UNION arm (EXPLAIN once built).
