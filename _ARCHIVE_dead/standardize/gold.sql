
IF OBJECT_ID('well_ref.well_master_gold') IS NULL
CREATE TABLE well_ref.well_master_gold (
    uwi14            char(14)      NOT NULL PRIMARY KEY,
    api_10           char(10)      NULL,
    well_name        nvarchar(300) NULL,
    well_num         nvarchar(50)  NULL,
    operator_name    nvarchar(300) NULL,
    field_name       nvarchar(200) NULL,
    surface_latitude  decimal(9,6) NULL,
    surface_longitude decimal(9,6) NULL,
    county           nvarchar(100) NULL,
    province_state   char(2)       NULL,
    country          char(2)       NULL,
    raw_well_type    nvarchar(200) NULL,
    raw_well_status  nvarchar(200) NULL,
    std_well_type    varchar(40)   NULL,
    std_well_status  varchar(40)   NULL,
    total_depth      decimal(9,1)  NULL,
    spud_date        date          NULL,
    name_norm        nvarchar(400) NULL,
    uwi_suspect      bit           NOT NULL DEFAULT 0,
    coord_suspect    bit           NOT NULL DEFAULT 0,
    primary_source   nvarchar(120) NULL,
    source_list      nvarchar(400) NULL,
    source_count     int           NOT NULL DEFAULT 1,
    dup_count        int           NOT NULL DEFAULT 1,
    quality_score    tinyint       NOT NULL DEFAULT 0,
    built_at         datetime2     NOT NULL DEFAULT SYSUTCDATETIME()
);

IF OBJECT_ID('well_ref._gold_stage') IS NOT NULL DROP TABLE well_ref._gold_stage;

WITH cleaned AS (
    SELECT
        uwi14   = LEFT(LTRIM(RTRIM(m.UWI14)),14),
        api_10  = LEFT(LTRIM(RTRIM(m.API_10)),10),
        well_name = LEFT(NULLIF(LTRIM(RTRIM(m.WELL_NAME)),''),300),
        well_num  = LEFT(NULLIF(LTRIM(RTRIM(m.WELL_NUM)),''),50),
        operator_name = LEFT(NULLIF(LTRIM(RTRIM(m.OPERATOR_NAME)),''),300),
        field_name    = LEFT(NULLIF(LTRIM(RTRIM(m.FIELD_NAME)),''),200),
        lat_raw = m.SURFACE_LATITUDE, lon_raw = m.SURFACE_LONGITUDE,
        surface_latitude  = CASE WHEN m.SURFACE_LATITUDE  BETWEEN 15 AND 72
                                 THEN TRY_CONVERT(decimal(9,6), m.SURFACE_LATITUDE)  END,
        surface_longitude = CASE WHEN m.SURFACE_LONGITUDE BETWEEN -180 AND -60
                                 THEN TRY_CONVERT(decimal(9,6), m.SURFACE_LONGITUDE) END,
        county = LEFT(NULLIF(LTRIM(RTRIM(m.COUNTY)),''),100),
        province_state = LEFT(UPPER(NULLIF(LTRIM(RTRIM(m.PROVINCE_STATE)),'')),2),
        country = CASE WHEN UPPER(LTRIM(RTRIM(m.COUNTRY))) IN ('US','USA','UNITED STATES')
                       THEN 'US' ELSE LEFT(UPPER(NULLIF(LTRIM(RTRIM(m.COUNTRY)),'')),2) END,
        raw_well_type   = LEFT(m.WELL_TYPE,200),
        raw_well_status = LEFT(m.WELL_STATUS,200),
        std_well_type   = COALESCE(x.std_well_type,'UNKNOWN'),
        std_well_status = COALESCE(x.std_well_status,'UNKNOWN'),
        total_depth = TRY_CONVERT(decimal(9,1), m.TOTAL_DEPTH),
        spud_date   = TRY_CONVERT(date, m.SPUD_DATE),
        name_norm   = LEFT(m.NAME_NORM,400),
        uwi_suspect = COALESCE(m.UWI_SUSPECT,0),
        coord_suspect = CASE
            WHEN (m.SURFACE_LATITUDE  IS NOT NULL AND m.SURFACE_LATITUDE  NOT BETWEEN 15 AND 72)
              OR (m.SURFACE_LONGITUDE IS NOT NULL AND m.SURFACE_LONGITUDE NOT BETWEEN -180 AND -60)
            THEN 1 ELSE 0 END,
        source_list  = LEFT(m.SOURCE_LIST,400),
        loaded_at    = m.LOADED_AT,
        ref_id       = m.REF_ID,
        src_rank     = CASE WHEN m.SOURCE_LIST LIKE '%TX_RRC%' THEN 0 WHEN m.SOURCE_LIST LIKE '%GOM_BOEM%' THEN 1 WHEN m.SOURCE_LIST LIKE '%LA_SONRIS%' THEN 2 WHEN m.SOURCE_LIST LIKE '%OK_OCC%' THEN 3 WHEN m.SOURCE_LIST LIKE '%KS_KGS%' THEN 4 WHEN m.SOURCE_LIST LIKE '%NM_OCD%' THEN 5 WHEN m.SOURCE_LIST LIKE '%CA_CALGEM%' THEN 6 WHEN m.SOURCE_LIST LIKE '%CO_ECMC%' THEN 7 WHEN m.SOURCE_LIST LIKE '%WY_WOGCC%' THEN 8 WHEN m.SOURCE_LIST LIKE '%ND_NDIC%' THEN 9 WHEN m.SOURCE_LIST LIKE '%MT_BOGC%' THEN 10 WHEN m.SOURCE_LIST LIKE '%UT_DOGM%' THEN 11 WHEN m.SOURCE_LIST LIKE '%OH_DNR%' THEN 12 WHEN m.SOURCE_LIST LIKE '%PA_DEP%' THEN 13 WHEN m.SOURCE_LIST LIKE '%WV_DEP%' THEN 14 WHEN m.SOURCE_LIST LIKE '%NY_NYSDEC%' THEN 15 WHEN m.SOURCE_LIST LIKE '%KY_KGS%' THEN 16 WHEN m.SOURCE_LIST LIKE '%IL_IGS%' THEN 17 WHEN m.SOURCE_LIST LIKE '%MI_EGLE%' THEN 18 WHEN m.SOURCE_LIST LIKE '%MS_MSOGB%' THEN 19 WHEN m.SOURCE_LIST LIKE '%AL_OGB%' THEN 20 WHEN m.SOURCE_LIST LIKE '%AR_AOGC%' THEN 21 WHEN m.SOURCE_LIST LIKE '%NE_DNR%' THEN 22 WHEN m.SOURCE_LIST LIKE '%IN_DNR%' THEN 23 WHEN m.SOURCE_LIST LIKE '%TN_OGB%' THEN 24 WHEN m.SOURCE_LIST LIKE '%VA_DMME%' THEN 25 WHEN m.SOURCE_LIST LIKE '%AK_AOGCC%' THEN 26 WHEN m.SOURCE_LIST LIKE '%FL_DEP%' THEN 27 WHEN m.SOURCE_LIST LIKE '%SD_DENR%' THEN 28 WHEN m.SOURCE_LIST LIKE '%ND_OTHER%' THEN 29 ELSE 900 END,
        completeness = (
        CASE WHEN NULLIF(LTRIM(RTRIM(m.WELL_NAME)),'')     IS NOT NULL THEN 1 ELSE 0 END
      + CASE WHEN NULLIF(LTRIM(RTRIM(m.OPERATOR_NAME)),'') IS NOT NULL THEN 1 ELSE 0 END
      + CASE WHEN NULLIF(LTRIM(RTRIM(m.COUNTY)),'')        IS NOT NULL THEN 1 ELSE 0 END
      + CASE WHEN m.SURFACE_LATITUDE  IS NOT NULL THEN 1 ELSE 0 END
      + CASE WHEN m.SURFACE_LONGITUDE IS NOT NULL THEN 1 ELSE 0 END
      + CASE WHEN TRY_CONVERT(decimal(9,1), m.TOTAL_DEPTH) IS NOT NULL THEN 1 ELSE 0 END
      + CASE WHEN TRY_CONVERT(date, m.SPUD_DATE)           IS NOT NULL THEN 1 ELSE 0 END
      + CASE WHEN NULLIF(LTRIM(RTRIM(m.FIELD_NAME)),'')    IS NOT NULL THEN 1 ELSE 0 END )
    FROM well_ref.WELL_MASTER m
    LEFT JOIN well_ref._gold_xwalk x ON x.mk = CONCAT(COALESCE(m.SOURCE_LIST,N'~N~'),N'|',COALESCE(m.WELL_TYPE,N'~N~'),N'|',COALESCE(m.WELL_STATUS,N'~N~'))
    WHERE m.UWI14 IS NOT NULL AND LEN(LTRIM(RTRIM(m.UWI14))) = 14
),
agg AS (
    SELECT uwi14,
           dup_count    = COUNT(*),
           source_count = COUNT(DISTINCT source_list)
    FROM cleaned GROUP BY uwi14
),
ranked AS (
    SELECT c.*,
           rn = ROW_NUMBER() OVER (PARTITION BY c.uwi14
                ORDER BY c.src_rank ASC, c.completeness DESC,
                         c.loaded_at DESC, c.ref_id ASC)
    FROM cleaned c
)
SELECT
    r.uwi14, r.api_10, r.well_name, r.well_num, r.operator_name, r.field_name,
    r.surface_latitude, r.surface_longitude, r.county, r.province_state, r.country,
    r.raw_well_type, r.raw_well_status, r.std_well_type, r.std_well_status,
    r.total_depth, r.spud_date, r.name_norm, r.uwi_suspect, r.coord_suspect,
    primary_source = r.source_list,
    r.source_list, a.source_count, a.dup_count,
    quality_score = CAST(r.completeness * 100.0 / 8 AS tinyint)
INTO well_ref._gold_stage
FROM ranked r
JOIN agg a ON a.uwi14 = r.uwi14
WHERE r.rn = 1;

BEGIN TRAN;
    TRUNCATE TABLE well_ref.well_master_gold;
    INSERT INTO well_ref.well_master_gold (
        uwi14, api_10, well_name, well_num, operator_name, field_name,
        surface_latitude, surface_longitude, county, province_state, country,
        raw_well_type, raw_well_status, std_well_type, std_well_status,
        total_depth, spud_date, name_norm, uwi_suspect, coord_suspect,
        primary_source, source_list, source_count, dup_count, quality_score)
    SELECT
        uwi14, api_10, well_name, well_num, operator_name, field_name,
        surface_latitude, surface_longitude, county, province_state, country,
        raw_well_type, raw_well_status, std_well_type, std_well_status,
        total_depth, spud_date, name_norm, uwi_suspect, coord_suspect,
        primary_source, source_list, source_count, dup_count, quality_score
    FROM well_ref._gold_stage;
COMMIT;
DROP TABLE well_ref._gold_stage;
