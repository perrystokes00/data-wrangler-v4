# dataview — schema map

_Generated 2026-06-14 11:35 from the live catalog._

## Subject areas

```mermaid
flowchart LR
    WELLS["🛢 Wells & Wellbores<br/>19 tables · 4,598,369 rows"]
    COMPLETIONS["🔧 Completions & Stimulation<br/>3 tables · 148,373 rows"]
    PRODUCTION["📈 Production & Volumes<br/>2 tables · 1,805 rows"]
    DIRECTIONAL["🧭 Directional Surveys<br/>2 tables · 2,367 rows"]
    FORMATION["🪨 Formations, Tops & Tests<br/>8 tables · 1,409 rows"]
    REFERENCE["📚 Reference & Lookups<br/>6 tables · 181,781 rows"]
    SPATIAL["🗺 Spatial & Political<br/>7 tables · 2 rows"]
    DOCUMENTS["📁 Documents & Catalog<br/>4 tables · 1,283 rows"]
    OTHER["📦 Other<br/>10 tables · 657,643 rows"]
    WELLS --- COMPLETIONS
    WELLS --- PRODUCTION
    WELLS --- DIRECTIONAL
    WELLS --- FORMATION
    WELLS --- REFERENCE
    WELLS --- SPATIAL
    WELLS --- DOCUMENTS
    WELLS --- OTHER
    style WELLS fill:#1D9E7522,stroke:#1D9E75,color:#e8eef2
    style COMPLETIONS fill:#378ADD22,stroke:#378ADD,color:#e8eef2
    style PRODUCTION fill:#EF9F2722,stroke:#EF9F27,color:#e8eef2
    style DIRECTIONAL fill:#B77FDD22,stroke:#B77FDD,color:#e8eef2
    style FORMATION fill:#C96A4B22,stroke:#C96A4B,color:#e8eef2
    style REFERENCE fill:#5B8DA022,stroke:#5B8DA0,color:#e8eef2
    style SPATIAL fill:#7FA65322,stroke:#7FA653,color:#e8eef2
    style DOCUMENTS fill:#9AA0A622,stroke:#9AA0A6,color:#e8eef2
    style OTHER fill:#88878022,stroke:#888780,color:#e8eef2
```

## 🛢 Wells & Wellbores

Core well identity plus the per-source federation extension tables. Everything in the model hangs off the UWI.

*19 tables.*

```mermaid
erDiagram
    DV_STG_WELL {
        int _stg_row_id PK
        nvarchar UWI FK
    }
    DV_WELL {
        nvarchar uwi PK
        nvarchar operator_ba_id
        nvarchar field_id FK
        nvarchar current_operator_ba_id
        nvarchar original_operator_ba_id
    }
    DV_WELL_ALIAS {
        nvarchar uwi PK
        nvarchar alias_id PK
        nvarchar source FK
    }
    DV_WELL_BACKUP_20260524 {
        nvarchar uwi FK
        nvarchar operator_ba_id
        nvarchar field_id FK
        nvarchar current_operator_ba_id
        nvarchar original_operator_ba_id
    }
    DV_WELL_CASING {
        nvarchar uwi PK
        nvarchar casing_id PK
        nvarchar source FK
    }
    DV_WELL_EXT_KGS {
        nvarchar uwi PK
        nvarchar OIL_DOR_ID
        nvarchar GAS_DOR_ID
    }
    DV_WELL_EXT_MICHIGAN_WELLS {
        nvarchar uwi FK
    }
    DV_WELL_EXT_WY_WOGCC {
        nvarchar uwi FK
    }
    DV_WELL_EXTENSION {
        nvarchar uwi PK
        nvarchar attr_name PK
        nvarchar source PK
    }
    DV_WELL_GOM_BACKUP {
        nvarchar uwi FK
        nvarchar operator_ba_id
        nvarchar field_id FK
        nvarchar current_operator_ba_id
        nvarchar original_operator_ba_id
    }
    DV_WELL_IDENTIFIER {
        uniqueidentifier well_id PK
        nvarchar identifier_type PK
    }
    DV_WELL_LEGAL {
        nvarchar uwi PK
        nvarchar location_type PK
    }
    DV_WELL_LOG {
        nvarchar uwi PK
        nvarchar log_id PK
        nvarchar service_company_ba_id FK
        nvarchar depth_ouom FK
        nvarchar catalog_id
        nvarchar source FK
    }
    DV_WELL_LOG_CURVE {
        nvarchar uwi PK
        nvarchar log_id PK
        nvarchar curve_id PK
        nvarchar curve_unit FK
        nvarchar depth_ouom FK
        nvarchar source FK
    }
    DV_WELL_MUD_LOG {
        nvarchar uwi PK
        nvarchar mud_log_id PK
        nvarchar contractor_ba_id
        nvarchar catalog_id
        nvarchar source FK
        nvarchar mud_logger_ba_id
    }
    DV_WELL_PETRO_INTERP {
        nvarchar uwi PK
        nvarchar interp_id PK
        nvarchar analyst_ba_id
        nvarchar gr_log_id
        nvarchar res_log_id
        nvarchar density_log_id
        nvarchar neutron_log_id
        nvarchar sonic_log_id
        nvarchar source FK
    }
    DV_WELL_PRESSURE {
        nvarchar uwi PK
        nvarchar pressure_id PK
        nvarchar contractor_ba_id
        nvarchar source FK
    }
    DV_WELL_SHOWS {
        nvarchar uwi PK
        nvarchar mud_log_id PK
        nvarchar show_id PK
        nvarchar source FK
    }
    STG_AI_WELL {
        nvarchar uwi FK
    }
    DV_BUSINESS_ASSOCIATE {
        nvarchar ba_id PK
    }
    DV_FIELD {
        nvarchar field_id PK
    }
    DV_R_SOURCE {
        nvarchar source PK
    }
    DV_R_UOM {
        nvarchar uom_code PK
    }
    DV_STRAT_INTERVAL {
        nvarchar uwi PK
        nvarchar strat_unit_id PK
        nvarchar interp_id PK
        nvarchar interval_id PK
    }
    DV_WELL ||--o{ DV_WELL_ALIAS : "uwi"
    DV_WELL ||--o{ DV_WELL_ALIAS : "uwi"
    DV_R_SOURCE ||--o{ DV_WELL_ALIAS : "source"
    DV_R_SOURCE ||--o{ DV_WELL_ALIAS : "source"
    DV_WELL ||--o{ DV_WELL_PETRO_INTERP : "uwi"
    DV_R_SOURCE ||--o{ DV_WELL_PETRO_INTERP : "source"
    DV_WELL ||--o{ DV_WELL_PRESSURE : "uwi"
    DV_R_SOURCE ||--o{ DV_WELL_PRESSURE : "source"
    DV_WELL ||--o{ DV_WELL_LOG : "uwi"
    DV_WELL ||--o{ DV_WELL_LOG : "uwi"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_WELL_LOG : "service_company_ba_id"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_WELL_LOG : "service_company_ba_id"
    DV_R_UOM ||--o{ DV_WELL_LOG : "depth_ouom"
    DV_R_UOM ||--o{ DV_WELL_LOG : "depth_ouom"
    DV_R_SOURCE ||--o{ DV_WELL_LOG : "source"
    DV_R_SOURCE ||--o{ DV_WELL_LOG : "source"
    DV_WELL_LOG ||--o{ DV_WELL_LOG_CURVE : "uwi"
    DV_WELL_LOG ||--o{ DV_WELL_LOG_CURVE : "log_id"
    DV_R_UOM ||--o{ DV_WELL_LOG_CURVE : "curve_unit"
    DV_R_UOM ||--o{ DV_WELL_LOG_CURVE : "curve_unit"
    DV_R_UOM ||--o{ DV_WELL_LOG_CURVE : "depth_ouom"
    DV_R_UOM ||--o{ DV_WELL_LOG_CURVE : "depth_ouom"
    DV_R_SOURCE ||--o{ DV_WELL_LOG_CURVE : "source"
    DV_R_SOURCE ||--o{ DV_WELL_LOG_CURVE : "source"
    DV_WELL ||--o{ DV_WELL_LEGAL : "uwi"
    DV_WELL ||--o{ DV_WELL_EXTENSION : "uwi"
    DV_WELL ||--o{ DV_WELL_CASING : "uwi"
    DV_R_SOURCE ||--o{ DV_WELL_CASING : "source"
    DV_WELL ||--o{ DV_WELL_MUD_LOG : "uwi"
    DV_R_SOURCE ||--o{ DV_WELL_MUD_LOG : "source"
    DV_WELL_MUD_LOG ||--o{ DV_WELL_SHOWS : "uwi"
    DV_WELL_MUD_LOG ||--o{ DV_WELL_SHOWS : "mud_log_id"
    DV_R_SOURCE ||--o{ DV_WELL_SHOWS : "source"
    DV_STRAT_INTERVAL ||--o{ DV_STG_WELL : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL : "uwi (inf)"
    DV_FIELD ||--o{ DV_WELL : "field_id (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_ALIAS : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_BACKUP_20260524 : "uwi (inf)"
    DV_FIELD ||--o{ DV_WELL_BACKUP_20260524 : "field_id (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_CASING : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_EXT_KGS : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_EXT_MICHIGAN_WELLS : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_EXT_WY_WOGCC : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_EXTENSION : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_GOM_BACKUP : "uwi (inf)"
    DV_FIELD ||--o{ DV_WELL_GOM_BACKUP : "field_id (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_LEGAL : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_LOG : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_LOG_CURVE : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_MUD_LOG : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_PETRO_INTERP : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_PRESSURE : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_SHOWS : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ STG_AI_WELL : "uwi (inf)"
```

## 🔧 Completions & Stimulation

Completion intervals, perforations, and stimulation / frac treatments tied back to the well.

*3 tables.*

```mermaid
erDiagram
    DV_WELL_COMPLETION {
        nvarchar uwi PK
        nvarchar completion_id PK
        nvarchar operator_ba_id
        nvarchar contractor_ba_id
    }
    DV_WELL_PERFORATION {
        nvarchar uwi PK
        nvarchar completion_id PK
        nvarchar perf_id PK
        nvarchar source FK
    }
    DV_WELL_STIMULATION {
        nvarchar uwi PK
        nvarchar completion_id PK
        nvarchar stim_id PK
    }
    DV_R_SOURCE {
        nvarchar source PK
    }
    DV_STRAT_INTERVAL {
        nvarchar uwi PK
        nvarchar strat_unit_id PK
        nvarchar interp_id PK
        nvarchar interval_id PK
    }
    DV_R_SOURCE ||--o{ DV_WELL_PERFORATION : "source"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_COMPLETION : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_PERFORATION : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_STIMULATION : "uwi (inf)"
```

## 📈 Production & Volumes

Monthly oil / gas / water volumes joined through the well and completion.

*2 tables.*

```mermaid
erDiagram
    DV_PROD_ENTITY {
        nvarchar prod_entity_id PK
        nvarchar uwi FK
        nvarchar field_id FK
        nvarchar operator_ba_id FK
        nvarchar source FK
    }
    DV_PROD_VOLUME {
        nvarchar prod_entity_id PK
        nvarchar period_date PK
        nvarchar fluid_type PK
        nvarchar volume_ouom FK
        nvarchar rate_ouom FK
        nvarchar source FK
    }
    DV_BUSINESS_ASSOCIATE {
        nvarchar ba_id PK
    }
    DV_FIELD {
        nvarchar field_id PK
    }
    DV_R_SOURCE {
        nvarchar source PK
    }
    DV_R_UOM {
        nvarchar uom_code PK
    }
    DV_STRAT_INTERVAL {
        nvarchar uwi PK
        nvarchar strat_unit_id PK
        nvarchar interp_id PK
        nvarchar interval_id PK
    }
    DV_WELL {
        nvarchar uwi PK
    }
    DV_WELL ||--o{ DV_PROD_ENTITY : "uwi"
    DV_WELL ||--o{ DV_PROD_ENTITY : "uwi"
    DV_FIELD ||--o{ DV_PROD_ENTITY : "field_id"
    DV_FIELD ||--o{ DV_PROD_ENTITY : "field_id"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_PROD_ENTITY : "operator_ba_id"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_PROD_ENTITY : "operator_ba_id"
    DV_R_SOURCE ||--o{ DV_PROD_ENTITY : "source"
    DV_R_SOURCE ||--o{ DV_PROD_ENTITY : "source"
    DV_PROD_ENTITY ||--o{ DV_PROD_VOLUME : "prod_entity_id"
    DV_PROD_ENTITY ||--o{ DV_PROD_VOLUME : "prod_entity_id"
    DV_R_UOM ||--o{ DV_PROD_VOLUME : "volume_ouom"
    DV_R_UOM ||--o{ DV_PROD_VOLUME : "volume_ouom"
    DV_R_UOM ||--o{ DV_PROD_VOLUME : "rate_ouom"
    DV_R_UOM ||--o{ DV_PROD_VOLUME : "rate_ouom"
    DV_R_SOURCE ||--o{ DV_PROD_VOLUME : "source"
    DV_R_SOURCE ||--o{ DV_PROD_VOLUME : "source"
    DV_STRAT_INTERVAL ||--o{ DV_PROD_ENTITY : "uwi (inf)"
```

## 🧭 Directional Surveys

Deviation survey stations — measured depth, inclination, azimuth, and TVD per wellbore.

*2 tables.*

```mermaid
erDiagram
    DV_WELL_DIR_SRVY_HDR {
        nvarchar uwi PK
        nvarchar survey_id PK
        nvarchar contractor_ba_id FK
        nvarchar depth_ouom FK
        nvarchar source FK
    }
    DV_WELL_DIR_SRVY_STA {
        nvarchar uwi PK
        nvarchar survey_id PK
        nvarchar station_id PK
        nvarchar depth_ouom FK
        nvarchar source FK
    }
    DV_BUSINESS_ASSOCIATE {
        nvarchar ba_id PK
    }
    DV_R_SOURCE {
        nvarchar source PK
    }
    DV_R_UOM {
        nvarchar uom_code PK
    }
    DV_STRAT_INTERVAL {
        nvarchar uwi PK
        nvarchar strat_unit_id PK
        nvarchar interp_id PK
        nvarchar interval_id PK
    }
    DV_WELL {
        nvarchar uwi PK
    }
    DV_WELL ||--o{ DV_WELL_DIR_SRVY_HDR : "uwi"
    DV_WELL ||--o{ DV_WELL_DIR_SRVY_HDR : "uwi"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_WELL_DIR_SRVY_HDR : "contractor_ba_id"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_WELL_DIR_SRVY_HDR : "contractor_ba_id"
    DV_R_UOM ||--o{ DV_WELL_DIR_SRVY_HDR : "depth_ouom"
    DV_R_UOM ||--o{ DV_WELL_DIR_SRVY_HDR : "depth_ouom"
    DV_R_SOURCE ||--o{ DV_WELL_DIR_SRVY_HDR : "source"
    DV_R_SOURCE ||--o{ DV_WELL_DIR_SRVY_HDR : "source"
    DV_WELL_DIR_SRVY_HDR ||--o{ DV_WELL_DIR_SRVY_STA : "uwi"
    DV_WELL_DIR_SRVY_HDR ||--o{ DV_WELL_DIR_SRVY_STA : "survey_id"
    DV_R_UOM ||--o{ DV_WELL_DIR_SRVY_STA : "depth_ouom"
    DV_R_UOM ||--o{ DV_WELL_DIR_SRVY_STA : "depth_ouom"
    DV_R_SOURCE ||--o{ DV_WELL_DIR_SRVY_STA : "source"
    DV_R_SOURCE ||--o{ DV_WELL_DIR_SRVY_STA : "source"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_DIR_SRVY_HDR : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_DIR_SRVY_STA : "uwi (inf)"
```

## 🪨 Formations, Tops & Tests

Formation tops / markers, drill-stem test intervals, and core data.

*8 tables.*

```mermaid
erDiagram
    DV_STRAT_INTERVAL {
        nvarchar uwi PK
        nvarchar strat_unit_id PK
        nvarchar interp_id PK
        nvarchar interval_id PK
        nvarchar depth_ouom FK
        nvarchar perm_ouom FK
        nvarchar source FK
    }
    DV_WELL_CORE {
        nvarchar uwi PK
        nvarchar core_id PK
        nvarchar cutting_company_ba_id
        nvarchar analysis_company_ba_id
        nvarchar source FK
    }
    DV_WELL_CORE_PHOTO {
        nvarchar uwi PK
        nvarchar core_id PK
        nvarchar photo_id PK
        nvarchar sample_id
        nvarchar catalog_id
        nvarchar source FK
    }
    DV_WELL_CORE_SAMPLE {
        nvarchar uwi PK
        nvarchar core_id PK
        nvarchar sample_id PK
        nvarchar source FK
    }
    DV_WELL_DST {
        nvarchar uwi PK
        nvarchar dst_id PK
        nvarchar contractor_ba_id
        nvarchar source FK
    }
    DV_WELL_DST_PERIOD {
        nvarchar uwi PK
        nvarchar dst_id PK
        nvarchar period_id PK
        nvarchar source FK
    }
    DV_WELL_FORMATION_TOP {
        nvarchar uwi PK
        nvarchar strat_unit_id PK
        nvarchar interp_id PK
        nvarchar depth_ouom FK
        nvarchar interpreter_ba_id FK
        nvarchar source FK
    }
    DV_WELL_PETRO_ZONE {
        nvarchar uwi PK
        nvarchar interp_id PK
        nvarchar zone_id PK
        nvarchar strat_unit_id
        nvarchar strat_interp_id
        nvarchar source FK
    }
    DV_BUSINESS_ASSOCIATE {
        nvarchar ba_id PK
    }
    DV_R_SOURCE {
        nvarchar source PK
    }
    DV_R_UOM {
        nvarchar uom_code PK
    }
    DV_WELL {
        nvarchar uwi PK
    }
    DV_WELL_PETRO_INTERP {
        nvarchar uwi PK
        nvarchar interp_id PK
    }
    DV_WELL_PETRO_INTERP ||--o{ DV_WELL_PETRO_ZONE : "uwi"
    DV_WELL_PETRO_INTERP ||--o{ DV_WELL_PETRO_ZONE : "interp_id"
    DV_R_SOURCE ||--o{ DV_WELL_PETRO_ZONE : "source"
    DV_WELL ||--o{ DV_WELL_FORMATION_TOP : "uwi"
    DV_WELL ||--o{ DV_WELL_FORMATION_TOP : "uwi"
    DV_R_UOM ||--o{ DV_WELL_FORMATION_TOP : "depth_ouom"
    DV_R_UOM ||--o{ DV_WELL_FORMATION_TOP : "depth_ouom"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_WELL_FORMATION_TOP : "interpreter_ba_id"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_WELL_FORMATION_TOP : "interpreter_ba_id"
    DV_R_SOURCE ||--o{ DV_WELL_FORMATION_TOP : "source"
    DV_R_SOURCE ||--o{ DV_WELL_FORMATION_TOP : "source"
    DV_WELL_FORMATION_TOP ||--o{ DV_STRAT_INTERVAL : "uwi"
    DV_WELL_FORMATION_TOP ||--o{ DV_STRAT_INTERVAL : "strat_unit_id"
    DV_WELL_FORMATION_TOP ||--o{ DV_STRAT_INTERVAL : "interp_id"
    DV_R_UOM ||--o{ DV_STRAT_INTERVAL : "depth_ouom"
    DV_R_UOM ||--o{ DV_STRAT_INTERVAL : "depth_ouom"
    DV_R_UOM ||--o{ DV_STRAT_INTERVAL : "perm_ouom"
    DV_R_UOM ||--o{ DV_STRAT_INTERVAL : "perm_ouom"
    DV_R_SOURCE ||--o{ DV_STRAT_INTERVAL : "source"
    DV_R_SOURCE ||--o{ DV_STRAT_INTERVAL : "source"
    DV_WELL ||--o{ DV_WELL_CORE : "uwi"
    DV_R_SOURCE ||--o{ DV_WELL_CORE : "source"
    DV_WELL_CORE ||--o{ DV_WELL_CORE_SAMPLE : "uwi"
    DV_WELL_CORE ||--o{ DV_WELL_CORE_SAMPLE : "core_id"
    DV_R_SOURCE ||--o{ DV_WELL_CORE_SAMPLE : "source"
    DV_WELL_CORE ||--o{ DV_WELL_CORE_PHOTO : "uwi"
    DV_WELL_CORE ||--o{ DV_WELL_CORE_PHOTO : "core_id"
    DV_R_SOURCE ||--o{ DV_WELL_CORE_PHOTO : "source"
    DV_WELL ||--o{ DV_WELL_DST : "uwi"
    DV_R_SOURCE ||--o{ DV_WELL_DST : "source"
    DV_WELL_DST ||--o{ DV_WELL_DST_PERIOD : "uwi"
    DV_WELL_DST ||--o{ DV_WELL_DST_PERIOD : "dst_id"
    DV_R_SOURCE ||--o{ DV_WELL_DST_PERIOD : "source"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_CORE : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_CORE_PHOTO : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_CORE_SAMPLE : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_DST : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_DST_PERIOD : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_FORMATION_TOP : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WELL_PETRO_ZONE : "uwi (inf)"
```

## 📚 Reference & Lookups

Business associates, fields, units, and PPDM reference / standard values that the data tables point at.

*6 tables.*

```mermaid
erDiagram
    DV_BUSINESS_ASSOCIATE {
        nvarchar ba_id PK
        nvarchar source FK
    }
    DV_FIELD {
        nvarchar field_id PK
        nvarchar operator_ba_id FK
        nvarchar source FK
    }
    DV_R_SOURCE {
        nvarchar source PK
    }
    DV_R_UOM {
        nvarchar uom_code PK
    }
    DV_R_WELL_STATUS {
        nvarchar well_status PK
    }
    DV_R_WELL_TYPE {
        nvarchar well_type PK
    }
    DV_R_SOURCE ||--o{ DV_BUSINESS_ASSOCIATE : "source"
    DV_R_SOURCE ||--o{ DV_BUSINESS_ASSOCIATE : "source"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_FIELD : "operator_ba_id"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_FIELD : "operator_ba_id"
    DV_R_SOURCE ||--o{ DV_FIELD : "source"
    DV_R_SOURCE ||--o{ DV_FIELD : "source"
```

## 🗺 Spatial & Political

County / state / PLSS / census boundaries, basins and plays, and BOEM lease blocks.

*7 tables.*

```mermaid
erDiagram
    DV_BASIN {
        nvarchar basin_id PK
        nvarchar country_code FK
        nvarchar gdm_basin_id
        nvarchar source FK
    }
    DV_COUNTY {
        nvarchar county_id PK
        nvarchar province_state_id FK
        nvarchar country_code FK
        nvarchar source FK
    }
    DV_OCS_BLOCK {
        nvarchar ocs_block_id PK
        nvarchar country_code FK
        nvarchar source FK
    }
    DV_PLSS_TOWNSHIP {
        nvarchar plss_id PK
        nvarchar county_id FK
        nvarchar province_state_id FK
        nvarchar source FK
    }
    DV_PROVINCE_STATE {
        nvarchar province_state_id PK
        nvarchar country_code FK
        nvarchar source FK
    }
    DV_SPATIAL_LAYER {
        nvarchar layer_id PK
        nvarchar source FK
    }
    STATE_POLYGON {
        varchar state_abbrev PK
    }
    DV_COUNTRY {
        nvarchar country_code PK
    }
    DV_R_SOURCE {
        nvarchar source PK
    }
    DV_COUNTRY ||--o{ DV_PROVINCE_STATE : "country_code"
    DV_COUNTRY ||--o{ DV_PROVINCE_STATE : "country_code"
    DV_R_SOURCE ||--o{ DV_PROVINCE_STATE : "source"
    DV_R_SOURCE ||--o{ DV_PROVINCE_STATE : "source"
    DV_PROVINCE_STATE ||--o{ DV_COUNTY : "province_state_id"
    DV_PROVINCE_STATE ||--o{ DV_COUNTY : "province_state_id"
    DV_COUNTRY ||--o{ DV_COUNTY : "country_code"
    DV_COUNTRY ||--o{ DV_COUNTY : "country_code"
    DV_R_SOURCE ||--o{ DV_COUNTY : "source"
    DV_R_SOURCE ||--o{ DV_COUNTY : "source"
    DV_COUNTRY ||--o{ DV_BASIN : "country_code"
    DV_COUNTRY ||--o{ DV_BASIN : "country_code"
    DV_R_SOURCE ||--o{ DV_BASIN : "source"
    DV_R_SOURCE ||--o{ DV_BASIN : "source"
    DV_COUNTY ||--o{ DV_PLSS_TOWNSHIP : "county_id"
    DV_COUNTY ||--o{ DV_PLSS_TOWNSHIP : "county_id"
    DV_PROVINCE_STATE ||--o{ DV_PLSS_TOWNSHIP : "province_state_id"
    DV_PROVINCE_STATE ||--o{ DV_PLSS_TOWNSHIP : "province_state_id"
    DV_R_SOURCE ||--o{ DV_PLSS_TOWNSHIP : "source"
    DV_R_SOURCE ||--o{ DV_PLSS_TOWNSHIP : "source"
    DV_COUNTRY ||--o{ DV_OCS_BLOCK : "country_code"
    DV_COUNTRY ||--o{ DV_OCS_BLOCK : "country_code"
    DV_R_SOURCE ||--o{ DV_OCS_BLOCK : "source"
    DV_R_SOURCE ||--o{ DV_OCS_BLOCK : "source"
    DV_R_SOURCE ||--o{ DV_SPATIAL_LAYER : "source"
    DV_R_SOURCE ||--o{ DV_SPATIAL_LAYER : "source"
```

## 📁 Documents & Catalog

File inventory, catalog scoring, scout tickets, and the LAS / PDF source assets.

*4 tables.*

```mermaid
erDiagram
    DOCUMENT_LOCATION {
        bigint doc_loc_id PK
        nvarchar inventory_id FK
        bigint promoted_to_well_id
    }
    DV_GLOBAL_FILE_CATALOG {
        nvarchar inventory_id PK
        nvarchar catalog_id
        nvarchar uwi FK
        nvarchar source FK
    }
    DV_SEIS_FILE_CATALOG {
        nvarchar catalog_id PK
        nvarchar seis_set_id FK
        nvarchar source FK
    }
    DV_WL_FILE_CATALOG {
        nvarchar catalog_id PK
        nvarchar uwi FK
        nvarchar ppdm_log_id
        nvarchar source FK
    }
    DV_R_SOURCE {
        nvarchar source PK
    }
    DV_SEIS_SET {
        nvarchar seis_set_id PK
    }
    DV_STRAT_INTERVAL {
        nvarchar uwi PK
        nvarchar strat_unit_id PK
        nvarchar interp_id PK
        nvarchar interval_id PK
    }
    DV_WELL {
        nvarchar uwi PK
    }
    DV_WELL ||--o{ DV_WL_FILE_CATALOG : "uwi"
    DV_WELL ||--o{ DV_WL_FILE_CATALOG : "uwi"
    DV_R_SOURCE ||--o{ DV_WL_FILE_CATALOG : "source"
    DV_R_SOURCE ||--o{ DV_WL_FILE_CATALOG : "source"
    DV_SEIS_SET ||--o{ DV_SEIS_FILE_CATALOG : "seis_set_id"
    DV_SEIS_SET ||--o{ DV_SEIS_FILE_CATALOG : "seis_set_id"
    DV_R_SOURCE ||--o{ DV_SEIS_FILE_CATALOG : "source"
    DV_R_SOURCE ||--o{ DV_SEIS_FILE_CATALOG : "source"
    DV_R_SOURCE ||--o{ DV_GLOBAL_FILE_CATALOG : "source"
    DV_R_SOURCE ||--o{ DV_GLOBAL_FILE_CATALOG : "source"
    DV_GLOBAL_FILE_CATALOG ||--o{ DOCUMENT_LOCATION : "inventory_id (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_GLOBAL_FILE_CATALOG : "uwi (inf)"
    DV_STRAT_INTERVAL ||--o{ DV_WL_FILE_CATALOG : "uwi (inf)"
```

## 📦 Other

Tables not yet classified into a subject area — adjust the rules or supply an overrides file to reclassify.

*10 tables.*

```mermaid
erDiagram
    _STG_KGS_H3_BACKFILL {
        nvarchar uwi FK
        nvarchar operator_ba_id
        nvarchar field_id FK
        nvarchar current_operator_ba_id
        nvarchar original_operator_ba_id
    }
    DV_COLUMN_MAP {
        nvarchar map_id PK
        nvarchar source FK
    }
    DV_COUNTRY {
        nvarchar country_code PK
        nvarchar source FK
    }
    DV_DATA_QUALITY {
        nvarchar dq_id PK
        nvarchar entity_id FK
        nvarchar batch_id FK
        nvarchar source FK
    }
    DV_LOAD_BATCH {
        nvarchar batch_id PK
        nvarchar operator_ba_id FK
        nvarchar source FK
    }
    DV_MAP_AREA {
        nvarchar area_id PK
    }
    DV_SEIS_LINE {
        nvarchar seis_set_id PK
        nvarchar line_id PK
        nvarchar source FK
    }
    DV_SEIS_SET {
        nvarchar seis_set_id PK
        nvarchar contractor_ba_id FK
        nvarchar operator_ba_id FK
        nvarchar catalog_id
        nvarchar source FK
    }
    DV_SOURCE {
        nvarchar source PK
        nvarchar source_ref FK
    }
    STG_AI_EXT {
        nvarchar uwi FK
    }
    DV_BUSINESS_ASSOCIATE {
        nvarchar ba_id PK
    }
    DV_FIELD {
        nvarchar field_id PK
    }
    DV_PROD_ENTITY {
        nvarchar prod_entity_id PK
    }
    DV_R_SOURCE {
        nvarchar source PK
    }
    DV_STRAT_INTERVAL {
        nvarchar uwi PK
        nvarchar strat_unit_id PK
        nvarchar interp_id PK
        nvarchar interval_id PK
    }
    DV_R_SOURCE ||--o{ DV_COLUMN_MAP : "source"
    DV_R_SOURCE ||--o{ DV_COLUMN_MAP : "source"
    DV_LOAD_BATCH ||--o{ DV_DATA_QUALITY : "batch_id"
    DV_LOAD_BATCH ||--o{ DV_DATA_QUALITY : "batch_id"
    DV_R_SOURCE ||--o{ DV_DATA_QUALITY : "source"
    DV_R_SOURCE ||--o{ DV_DATA_QUALITY : "source"
    DV_R_SOURCE ||--o{ DV_COUNTRY : "source"
    DV_R_SOURCE ||--o{ DV_COUNTRY : "source"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_SEIS_SET : "contractor_ba_id"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_SEIS_SET : "contractor_ba_id"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_SEIS_SET : "operator_ba_id"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_SEIS_SET : "operator_ba_id"
    DV_R_SOURCE ||--o{ DV_SEIS_SET : "source"
    DV_R_SOURCE ||--o{ DV_SEIS_SET : "source"
    DV_SEIS_SET ||--o{ DV_SEIS_LINE : "seis_set_id"
    DV_SEIS_SET ||--o{ DV_SEIS_LINE : "seis_set_id"
    DV_R_SOURCE ||--o{ DV_SEIS_LINE : "source"
    DV_R_SOURCE ||--o{ DV_SEIS_LINE : "source"
    DV_R_SOURCE ||--o{ DV_SOURCE : "source_ref"
    DV_R_SOURCE ||--o{ DV_SOURCE : "source_ref"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_LOAD_BATCH : "operator_ba_id"
    DV_BUSINESS_ASSOCIATE ||--o{ DV_LOAD_BATCH : "operator_ba_id"
    DV_R_SOURCE ||--o{ DV_LOAD_BATCH : "source"
    DV_R_SOURCE ||--o{ DV_LOAD_BATCH : "source"
    DV_STRAT_INTERVAL ||--o{ _STG_KGS_H3_BACKFILL : "uwi (inf)"
    DV_FIELD ||--o{ _STG_KGS_H3_BACKFILL : "field_id (inf)"
    DV_PROD_ENTITY ||--o{ DV_DATA_QUALITY : "entity_id (inf)"
    DV_STRAT_INTERVAL ||--o{ STG_AI_EXT : "uwi (inf)"
```
