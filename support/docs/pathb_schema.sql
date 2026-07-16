/*
================================================================================
    Path B Schema — Clean Slate Rebuild
================================================================================
    Project:    Data Wrangler v3 / DataView
    Database:   DataView (production catalog DB)
    Generated:  2026-05-11
    Author:     Data Wrangler refactor for document-as-truth architecture

    PURPOSE
    -------
    Drops Path A governance debt from `file_catalog` and creates Path B clean
    tables. Adds `dataview.document_location` (aggregator output) and
    `dataview.state_polygon` (TIGER state lookup, empty until loaded).

    LAS/DLIS/LIS/SEGY detail tables in `las_catalog` are UNCHANGED — they
    work today and the extractors depend on them.

    WHAT GETS DROPPED
    -----------------
    file_catalog.AUDIT_LOG               (Path A audit trail)
    file_catalog.INVENTORY_USER          (Path A user management)
    file_catalog.INVENTORY_GROUP         (Path A cataloger groups)
    file_catalog.INVENTORY_GROUP_FILE    (Path A group-file mapping)
    file_catalog.INVENTORY_ASSIGNMENT    (Path A cataloger assignments)
    file_catalog.ASSIGNMENT_EXTENSION    (Path A due-date extensions)
    file_catalog.WELL_HEADER_STAGING     (empty, Path A staging)
    file_catalog.SEIS_HEADER_STAGING     (empty, Path A staging)
    file_catalog.FILE_HEADER             (predecessor of FILE_WELL_HEADER)

    WHAT GETS REBUILT
    -----------------
    file_catalog.GLOBAL_FILE_CATALOG     (slim — Path A columns removed)
    file_catalog.FILE_WELL_HEADER        (DECIMAL lat/lon, coord_precision)
    file_catalog.FILE_SEIS_HEADER        (typed, otherwise unchanged)
    file_catalog.FILE_CURVE              (unchanged)
    file_catalog.CATALOG_SETTING         (renamed from INVENTORY_SETTING)

    WHAT GETS CREATED
    -----------------
    dataview.document_location           (Path B aggregator output)
    dataview.state_polygon               (TIGER state bbox, empty)

    CRAWLER IMPACT
    --------------
    The crawler and extractors write to these tables. After running this
    script, expect to re-crawl. All existing catalog data is gone.

    RUN ORDER
    ---------
    Top-to-bottom in SSMS. Each DROP is guarded with IF EXISTS so the script
    is idempotent — running it twice produces the same end state.
================================================================================
*/

USE DataView;
GO

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

PRINT '================================================================';
PRINT 'Path B Schema Rebuild — START';
PRINT '================================================================';
GO


-- ============================================================================
-- SECTION 1 — DROP Path A tables and old extraction tables
-- ============================================================================
PRINT 'Dropping Path A governance tables...';
GO

IF OBJECT_ID('file_catalog.ASSIGNMENT_EXTENSION', 'U') IS NOT NULL
    DROP TABLE file_catalog.ASSIGNMENT_EXTENSION;
GO
IF OBJECT_ID('file_catalog.INVENTORY_ASSIGNMENT', 'U') IS NOT NULL
    DROP TABLE file_catalog.INVENTORY_ASSIGNMENT;
GO
IF OBJECT_ID('file_catalog.INVENTORY_GROUP_FILE', 'U') IS NOT NULL
    DROP TABLE file_catalog.INVENTORY_GROUP_FILE;
GO
IF OBJECT_ID('file_catalog.INVENTORY_GROUP', 'U') IS NOT NULL
    DROP TABLE file_catalog.INVENTORY_GROUP;
GO
IF OBJECT_ID('file_catalog.INVENTORY_USER', 'U') IS NOT NULL
    DROP TABLE file_catalog.INVENTORY_USER;
GO
IF OBJECT_ID('file_catalog.AUDIT_LOG', 'U') IS NOT NULL
    DROP TABLE file_catalog.AUDIT_LOG;
GO

PRINT 'Dropping old extraction / staging tables...';
GO

IF OBJECT_ID('file_catalog.FILE_HEADER', 'U') IS NOT NULL
    DROP TABLE file_catalog.FILE_HEADER;
GO
IF OBJECT_ID('file_catalog.WELL_HEADER_STAGING', 'U') IS NOT NULL
    DROP TABLE file_catalog.WELL_HEADER_STAGING;
GO
IF OBJECT_ID('file_catalog.SEIS_HEADER_STAGING', 'U') IS NOT NULL
    DROP TABLE file_catalog.SEIS_HEADER_STAGING;
GO


-- ============================================================================
-- SECTION 2 — DROP current versions of tables we are recreating
-- ============================================================================
PRINT 'Dropping current versions of tables being rebuilt...';
GO

IF OBJECT_ID('file_catalog.FILE_CURVE', 'U') IS NOT NULL
    DROP TABLE file_catalog.FILE_CURVE;
GO
IF OBJECT_ID('file_catalog.FILE_WELL_HEADER', 'U') IS NOT NULL
    DROP TABLE file_catalog.FILE_WELL_HEADER;
GO
IF OBJECT_ID('file_catalog.FILE_SEIS_HEADER', 'U') IS NOT NULL
    DROP TABLE file_catalog.FILE_SEIS_HEADER;
GO
IF OBJECT_ID('file_catalog.GLOBAL_FILE_CATALOG', 'U') IS NOT NULL
    DROP TABLE file_catalog.GLOBAL_FILE_CATALOG;
GO
IF OBJECT_ID('file_catalog.INVENTORY_SETTING', 'U') IS NOT NULL
    DROP TABLE file_catalog.INVENTORY_SETTING;
GO
IF OBJECT_ID('file_catalog.CATALOG_SETTING', 'U') IS NOT NULL
    DROP TABLE file_catalog.CATALOG_SETTING;
GO

IF OBJECT_ID('dataview.document_location', 'U') IS NOT NULL
    DROP TABLE dataview.document_location;
GO
IF OBJECT_ID('dataview.state_polygon', 'U') IS NOT NULL
    DROP TABLE dataview.state_polygon;
GO


-- ============================================================================
-- SECTION 3 — CREATE file_catalog tables (Path B)
-- ============================================================================
PRINT 'Creating file_catalog.GLOBAL_FILE_CATALOG (slim)...';
GO

-- ----------------------------------------------------------------------------
-- GLOBAL_FILE_CATALOG — File inventory + Phase 2 enrichment summary
-- ----------------------------------------------------------------------------
-- One row per file discovered by the crawler. Populated in two phases:
--   Phase 1 (crawl)   — path, name, hash, size, modified_date
--   Phase 2 (extract) — doc_type, report_type, header_extracted flag
--
-- Per-file extracted content (UWI, well_name, lat/lon, etc.) lives in
-- FILE_WELL_HEADER / FILE_SEIS_HEADER — NOT here. This table is for
-- inventory + classification only, not for storing extracted facts.
-- ----------------------------------------------------------------------------
CREATE TABLE file_catalog.GLOBAL_FILE_CATALOG (
    INVENTORY_ID        NVARCHAR(40)   NOT NULL,
    -- File system facts (Phase 1)
    FILE_PATH           NVARCHAR(1000) NOT NULL,
    FILE_NAME           NVARCHAR(500)  NOT NULL,
    FILE_EXT            NVARCHAR(20)   NULL,
    FILE_SIZE_KB        NUMERIC(15, 2) NULL,
    FILE_HASH           NVARCHAR(64)   NULL,    -- partial hash for fast dedup
    FILE_HASH_FULL      NVARCHAR(64)   NULL,    -- full hash for confirmation
    DUPLICATE_GROUP     NVARCHAR(64)   NULL,
    MODIFIED_DATE       DATETIME2(7)   NULL,
    SCAN_DATE           DATETIME2(7)   NOT NULL,
    ROOT_PATH           NVARCHAR(500)  NULL,
    FILE_TYPE_GROUP     NVARCHAR(50)   NULL,    -- LOG / SEIS / OFFICE / SHP

    -- Classifier output (Phase 2)
    DOC_TYPE            NVARCHAR(100)  NULL,    -- scout_ticket / formation_tops / ...
    REPORT_TYPE         NVARCHAR(100)  NULL,    -- WELL_LOG / SEISMIC / SHAPEFILE / ...
    HEADER_EXTRACTED    NVARCHAR(1)    NULL,    -- 'Y' once Phase 2 ran

    -- Audit
    FLAG_DELETE         NVARCHAR(1)    NULL,
    ROW_CREATED_DATE    DATETIME2(7)   NOT NULL,
    ROW_CHANGED_DATE    DATETIME2(7)   NOT NULL,

    CONSTRAINT PK_GLOBAL_FILE_CATALOG PRIMARY KEY CLUSTERED (INVENTORY_ID)
);
GO

ALTER TABLE file_catalog.GLOBAL_FILE_CATALOG
    ADD CONSTRAINT DF_GFC_SCAN_DATE        DEFAULT (SYSUTCDATETIME()) FOR SCAN_DATE;
GO
ALTER TABLE file_catalog.GLOBAL_FILE_CATALOG
    ADD CONSTRAINT DF_GFC_ROW_CREATED_DATE DEFAULT (SYSUTCDATETIME()) FOR ROW_CREATED_DATE;
GO
ALTER TABLE file_catalog.GLOBAL_FILE_CATALOG
    ADD CONSTRAINT DF_GFC_ROW_CHANGED_DATE DEFAULT (SYSUTCDATETIME()) FOR ROW_CHANGED_DATE;
GO

CREATE INDEX IX_GFC_HASH        ON file_catalog.GLOBAL_FILE_CATALOG (FILE_HASH);
CREATE INDEX IX_GFC_DUPLICATE   ON file_catalog.GLOBAL_FILE_CATALOG (DUPLICATE_GROUP);
CREATE INDEX IX_GFC_FILE_TYPE   ON file_catalog.GLOBAL_FILE_CATALOG (FILE_TYPE_GROUP);
CREATE INDEX IX_GFC_REPORT_TYPE ON file_catalog.GLOBAL_FILE_CATALOG (REPORT_TYPE);
CREATE INDEX IX_GFC_EXTRACTED   ON file_catalog.GLOBAL_FILE_CATALOG (HEADER_EXTRACTED);
GO

PRINT 'Creating file_catalog.FILE_WELL_HEADER (DECIMAL coords + precision)...';
GO

-- ----------------------------------------------------------------------------
-- FILE_WELL_HEADER — Per-file extracted well metadata
-- ----------------------------------------------------------------------------
-- One row per well-bearing file (LAS, DLIS, LIS, PDF, Word, Excel) where the
-- extractor pulled identifying facts. Lat/lon stored as DECIMAL for indexed
-- spatial filtering. COORD_PRECISION is computed at extraction time and
-- used by the aggregator to decide whether the coord is map-worthy.
-- ----------------------------------------------------------------------------
CREATE TABLE file_catalog.FILE_WELL_HEADER (
    WELL_HEADER_ID      NVARCHAR(40)   NOT NULL,    -- hash of (inv_id + content)
    INVENTORY_ID        NVARCHAR(40)   NOT NULL,    -- FK → GLOBAL_FILE_CATALOG

    -- Identification
    UWI                 NVARCHAR(40)   NULL,
    WELL_NAME           NVARCHAR(255)  NULL,
    OPERATOR            NVARCHAR(255)  NULL,
    WELL_FIELD          NVARCHAR(100)  NULL,
    STATE               NVARCHAR(50)   NULL,
    COUNTY              NVARCHAR(100)  NULL,

    -- Location — typed DECIMAL, indexed
    LATITUDE            DECIMAL(11, 7) NULL,
    LONGITUDE           DECIMAL(11, 7) NULL,
    COORD_PRECISION     TINYINT        NULL,        -- decimal places (0-7)

    -- Depth and timing
    TOTAL_DEPTH         DECIMAL(15, 5) NULL,
    SPUD_DATE           NVARCHAR(20)   NULL,        -- text — formats vary
    RIG_RELEASE         NVARCHAR(20)   NULL,

    -- Document classification
    REPORT_TYPE         NVARCHAR(50)   NULL,
    SURVEY_TYPE         NVARCHAR(50)   NULL,
    CONTRACTOR          NVARCHAR(255)  NULL,

    -- Extraction quality
    CONFIDENCE          DECIMAL(5, 2)  NULL,        -- 0-100, from extractor

    -- Audit
    EXTRACTED_DATE      DATETIME2(7)   NOT NULL,
    EXTRACTED_BY        NVARCHAR(64)   NOT NULL,

    CONSTRAINT PK_FILE_WELL_HEADER PRIMARY KEY CLUSTERED (WELL_HEADER_ID)
);
GO

ALTER TABLE file_catalog.FILE_WELL_HEADER
    ADD CONSTRAINT DF_FWH_EXTRACTED_DATE DEFAULT (SYSUTCDATETIME()) FOR EXTRACTED_DATE;
GO
ALTER TABLE file_catalog.FILE_WELL_HEADER
    ADD CONSTRAINT DF_FWH_EXTRACTED_BY   DEFAULT ('DataWrangler')   FOR EXTRACTED_BY;
GO

CREATE INDEX IX_FWH_INVENTORY ON file_catalog.FILE_WELL_HEADER (INVENTORY_ID);
CREATE INDEX IX_FWH_UWI       ON file_catalog.FILE_WELL_HEADER (UWI);
CREATE INDEX IX_FWH_COORDS    ON file_catalog.FILE_WELL_HEADER (LATITUDE, LONGITUDE);
CREATE INDEX IX_FWH_STATE     ON file_catalog.FILE_WELL_HEADER (STATE);
GO

PRINT 'Creating file_catalog.FILE_SEIS_HEADER (typed bbox)...';
GO

-- ----------------------------------------------------------------------------
-- FILE_SEIS_HEADER — Per-file extracted seismic metadata
-- ----------------------------------------------------------------------------
-- One row per seismic file (SEG-Y 2D/3D, P190). Spatial extent is a bbox,
-- not a point.
-- ----------------------------------------------------------------------------
CREATE TABLE file_catalog.FILE_SEIS_HEADER (
    SEIS_HEADER_ID      NVARCHAR(40)   NOT NULL,
    INVENTORY_ID        NVARCHAR(40)   NOT NULL,

    -- Identification
    SURVEY_NAME         NVARCHAR(255)  NULL,
    LINE_NAME           NVARCHAR(255)  NULL,
    SEIS_SET_TYPE       NVARCHAR(40)   NULL,        -- 2D / 3D / VSP
    SURVEY_DATE         NVARCHAR(20)   NULL,
    CONTRACTOR          NVARCHAR(255)  NULL,

    -- Spatial extent — typed DECIMAL
    BBOX_MIN_LAT        DECIMAL(11, 7) NULL,
    BBOX_MAX_LAT        DECIMAL(11, 7) NULL,
    BBOX_MIN_LON        DECIMAL(11, 7) NULL,
    BBOX_MAX_LON        DECIMAL(11, 7) NULL,
    EPSG_CODE           INT            NULL,

    -- Survey parameters
    SAMPLE_INTERVAL     DECIMAL(10, 3) NULL,
    TRACE_COUNT         INT            NULL,
    SHOT_FIRST          NVARCHAR(20)   NULL,
    SHOT_LAST           NVARCHAR(20)   NULL,

    -- Audit
    EXTRACTED_DATE      DATETIME2(7)   NOT NULL,
    EXTRACTED_BY        NVARCHAR(64)   NOT NULL,

    CONSTRAINT PK_FILE_SEIS_HEADER PRIMARY KEY CLUSTERED (SEIS_HEADER_ID)
);
GO

ALTER TABLE file_catalog.FILE_SEIS_HEADER
    ADD CONSTRAINT DF_FSH_EXTRACTED_DATE DEFAULT (SYSUTCDATETIME()) FOR EXTRACTED_DATE;
GO
ALTER TABLE file_catalog.FILE_SEIS_HEADER
    ADD CONSTRAINT DF_FSH_EXTRACTED_BY   DEFAULT ('DataWrangler')   FOR EXTRACTED_BY;
GO

CREATE INDEX IX_FSH_INVENTORY ON file_catalog.FILE_SEIS_HEADER (INVENTORY_ID);
CREATE INDEX IX_FSH_BBOX_SW   ON file_catalog.FILE_SEIS_HEADER (BBOX_MIN_LAT, BBOX_MIN_LON);
CREATE INDEX IX_FSH_BBOX_NE   ON file_catalog.FILE_SEIS_HEADER (BBOX_MAX_LAT, BBOX_MAX_LON);
GO

PRINT 'Creating file_catalog.FILE_CURVE (LAS curve names)...';
GO

-- ----------------------------------------------------------------------------
-- FILE_CURVE — LAS curve mnemonics per file
-- ----------------------------------------------------------------------------
-- Used by the workbench viewer to list curves before deciding to plot any.
-- Lives in file_catalog because it's an extraction-time index of curves
-- across all files, used at the catalog level (not the deep las_catalog
-- detail level which has full curve data in LAS_FILE_CURVE).
-- ----------------------------------------------------------------------------
CREATE TABLE file_catalog.FILE_CURVE (
    FILE_CURVE_ID       NVARCHAR(40)   NOT NULL,
    FILE_HEADER_ID      NVARCHAR(40)   NOT NULL,    -- FK → FILE_WELL_HEADER
    MNEMONIC            NVARCHAR(40)   NOT NULL,
    UNIT                NVARCHAR(40)   NULL,
    DESCRIPTION         NVARCHAR(200)  NULL,
    SORT_ORDER          INT            NULL,

    CONSTRAINT PK_FILE_CURVE PRIMARY KEY CLUSTERED (FILE_CURVE_ID)
);
GO

CREATE INDEX IX_FC_HEADER   ON file_catalog.FILE_CURVE (FILE_HEADER_ID);
CREATE INDEX IX_FC_MNEMONIC ON file_catalog.FILE_CURVE (MNEMONIC);
GO

PRINT 'Creating file_catalog.CATALOG_SETTING (key-value store)...';
GO

-- ----------------------------------------------------------------------------
-- CATALOG_SETTING — Generic key-value config
-- ----------------------------------------------------------------------------
-- Renamed from INVENTORY_SETTING to drop the Path A name. Same structure,
-- same purpose: store crawler root paths, defaults, last-scan timestamps,
-- whatever Streamlit wants to persist across sessions.
-- ----------------------------------------------------------------------------
CREATE TABLE file_catalog.CATALOG_SETTING (
    SETTING_KEY         NVARCHAR(100)  NOT NULL,
    SETTING_VALUE       NVARCHAR(900)  NULL,
    DESCRIPTION         NVARCHAR(500)  NULL,
    UPDATED_DATE        DATETIME2(7)   NULL,
    UPDATED_BY          NVARCHAR(100)  NULL,

    CONSTRAINT PK_CATALOG_SETTING PRIMARY KEY CLUSTERED (SETTING_KEY)
);
GO


-- ============================================================================
-- SECTION 4 — CREATE dataview tables (Path B aggregator)
-- ============================================================================
PRINT 'Creating dataview.document_location (Path B aggregator output)...';
GO

-- ----------------------------------------------------------------------------
-- document_location — Curated provenance map of locations found in documents
-- ----------------------------------------------------------------------------
-- One row per (source_file, extracted_location) pair. Built by the aggregator
-- module from FILE_WELL_HEADER + FILE_SEIS_HEADER + GLOBAL_FILE_CATALOG.
-- Validated by precision check, state-bbox check, county-polygon check.
--
-- This is the Path B PRIMARY DATA PRODUCT. The well master can be reconciled
-- against this. The map overlay reads from this. Curation flows through this.
-- ----------------------------------------------------------------------------
CREATE TABLE dataview.document_location (
    doc_loc_id          BIGINT IDENTITY(1, 1) NOT NULL,
    inventory_id        NVARCHAR(40)   NOT NULL,    -- FK → GLOBAL_FILE_CATALOG
    source_table        VARCHAR(50)    NOT NULL,    -- which table the row came from

    -- The spatial fact
    latitude            DECIMAL(11, 7) NOT NULL,
    longitude           DECIMAL(11, 7) NOT NULL,
    coord_precision     TINYINT        NULL,

    -- Denormalised facts from source file
    file_path           NVARCHAR(1000) NULL,
    file_format         NVARCHAR(20)   NULL,
    doc_type            NVARCHAR(100)  NULL,
    uwi_in_doc          NVARCHAR(40)   NULL,
    well_name_in_doc    NVARCHAR(255)  NULL,
    operator_in_doc     NVARCHAR(255)  NULL,
    state_in_doc        NVARCHAR(50)   NULL,
    county_in_doc       NVARCHAR(100)  NULL,

    -- Validation flags (computed by aggregator)
    precision_ok        BIT            NULL,        -- coord_precision >= 3
    state_bbox_ok       BIT            NULL,        -- lat/lon in state bbox?
    county_match_ok     BIT            NULL,        -- coord in county polygon?

    -- Deduplication and confidence
    duplicate_of        BIGINT         NULL,        -- → another doc_loc_id
    confidence          DECIMAL(5, 4)  NULL,        -- 0.0000 to 1.0000

    -- Curation workflow
    curation_status     NVARCHAR(20)   NOT NULL,    -- extracted / flagged / accepted / rejected / merged
    curated_by          NVARCHAR(100)  NULL,
    curated_date        DATETIME2(7)   NULL,
    curation_notes      NVARCHAR(MAX)  NULL,

    -- Promotion to dv_well (when curator decides this doc updates the master)
    promoted_to_well_id BIGINT         NULL,        -- → dv_well.well_id
    promoted_date       DATETIME2(7)   NULL,

    -- Audit
    row_created_date    DATETIME2(7)   NOT NULL,
    row_changed_date    DATETIME2(7)   NOT NULL,

    CONSTRAINT PK_document_location PRIMARY KEY CLUSTERED (doc_loc_id)
);
GO

ALTER TABLE dataview.document_location
    ADD CONSTRAINT DF_docloc_curation_status DEFAULT ('extracted')      FOR curation_status;
GO
ALTER TABLE dataview.document_location
    ADD CONSTRAINT DF_docloc_row_created     DEFAULT (SYSUTCDATETIME()) FOR row_created_date;
GO
ALTER TABLE dataview.document_location
    ADD CONSTRAINT DF_docloc_row_changed     DEFAULT (SYSUTCDATETIME()) FOR row_changed_date;
GO

CREATE INDEX IX_docloc_inventory   ON dataview.document_location (inventory_id);
CREATE INDEX IX_docloc_coords      ON dataview.document_location (latitude, longitude);
CREATE INDEX IX_docloc_curation    ON dataview.document_location (curation_status);
CREATE INDEX IX_docloc_uwi         ON dataview.document_location (uwi_in_doc);
CREATE INDEX IX_docloc_duplicate   ON dataview.document_location (duplicate_of);
CREATE INDEX IX_docloc_confidence  ON dataview.document_location (confidence);
GO

PRINT 'Creating dataview.state_polygon (TIGER state lookup, empty until loaded)...';
GO

-- ----------------------------------------------------------------------------
-- state_polygon — US state polygons for spatial validation
-- ----------------------------------------------------------------------------
-- Empty at creation. Populated separately from TIGER/Line state boundaries
-- (US Census Bureau). The aggregator uses this to check whether an extracted
-- (lat, lon) falls inside the state claimed by the source document.
--
-- The GEOGRAPHY column allows real polygon-contains queries:
--     SELECT * FROM dataview.state_polygon
--     WHERE state_polygon.STIntersects(geography::Point(lat, lon, 4326)) = 1
--
-- Until polygons are loaded, fall back to the simple bbox columns.
-- ----------------------------------------------------------------------------
CREATE TABLE dataview.state_polygon (
    state_abbrev        VARCHAR(2)     NOT NULL,    -- 'TX', 'KS', 'OK', etc.
    state_name          NVARCHAR(50)   NOT NULL,
    fips_code           VARCHAR(2)     NULL,

    -- Simple bbox (always populated, easy to compute)
    min_lat             DECIMAL(11, 7) NULL,
    max_lat             DECIMAL(11, 7) NULL,
    min_lon             DECIMAL(11, 7) NULL,
    max_lon             DECIMAL(11, 7) NULL,

    -- Full polygon (NULL until TIGER load)
    state_polygon       GEOGRAPHY      NULL,

    -- Audit
    loaded_date         DATETIME2(7)   NULL,
    source              NVARCHAR(100)  NULL,        -- 'TIGER_2024', etc.

    CONSTRAINT PK_state_polygon PRIMARY KEY CLUSTERED (state_abbrev)
);
GO


-- ============================================================================
-- SECTION 5 — Verification
-- ============================================================================
PRINT 'Verifying...';
GO

-- Show what's now in file_catalog
SELECT
    TABLE_SCHEMA AS [Schema],
    TABLE_NAME   AS [Table]
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA IN ('file_catalog', 'las_catalog', 'dataview')
  AND TABLE_TYPE = 'BASE TABLE'
ORDER BY TABLE_SCHEMA, TABLE_NAME;
GO

PRINT '================================================================';
PRINT 'Path B Schema Rebuild — DONE';
PRINT '================================================================';
PRINT 'Expected results:';
PRINT '  file_catalog: 5 tables (GLOBAL_FILE_CATALOG, FILE_WELL_HEADER,';
PRINT '                FILE_SEIS_HEADER, FILE_CURVE, CATALOG_SETTING)';
PRINT '  las_catalog:  13 tables (UNCHANGED)';
PRINT '  dataview:     dv_well, dv_business_associate, dv_field, etc.';
PRINT '                + 2 new: document_location, state_polygon';
PRINT '';
PRINT 'Next steps:';
PRINT '  1. Verify no extractor code still references dropped tables';
PRINT '  2. Re-crawl your files into the new GLOBAL_FILE_CATALOG';
PRINT '  3. Build modules/doc_location.py for Stage 3 aggregation';
PRINT '  4. Load TIGER state polygons into dataview.state_polygon';
PRINT '================================================================';
GO
