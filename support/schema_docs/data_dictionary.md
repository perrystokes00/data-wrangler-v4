# dataview — data dictionary

_Generated 2026-06-14 11:04._

**61 tables**, **5,593,032 rows** across 9 subject areas.

## 🛢 Wells & Wellbores

### `dv_stg_well`

_10,000 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| _stg_row_id | int |  | PK |
| _stg_source | nvarchar(40) | ✓ |  |
| _stg_loaded_at | datetime2 | ✓ |  |
| UWI | nvarchar(500) | ✓ | FK |
| API_NUMBER | nvarchar(500) | ✓ |  |
| WELL_NAME | nvarchar(500) | ✓ |  |
| WELL_NUM | nvarchar(500) | ✓ |  |
| CURR_OPERATOR | nvarchar(500) | ✓ |  |
| WELL_TYPE | nvarchar(500) | ✓ |  |
| FORMATION_AT_TD | nvarchar(500) | ✓ |  |
| SPUD_DATE | nvarchar(500) | ✓ |  |
| COMPLETION_DATE | nvarchar(500) | ✓ |  |
| KB_ELEV | nvarchar(500) | ✓ |  |
| GROUND_ELEVATION | nvarchar(500) | ✓ |  |
| SURFACE_LATITUDE | nvarchar(500) | ✓ |  |
| SURFACE_LONGITUDE | nvarchar(500) | ✓ |  |

**Relationships:** → `dv_strat_interval` on `uwi` (inferred)

### `dv_well`

_515,064 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| well_name | nvarchar(255) | ✓ |  |
| well_num | nvarchar(40) | ✓ |  |
| operator_ba_id | nvarchar(40) | ✓ |  |
| field_id | nvarchar(40) | ✓ | FK |
| well_type | nvarchar(40) | ✓ |  |
| well_status | nvarchar(40) | ✓ |  |
| country | nvarchar(40) | ✓ |  |
| province_state | nvarchar(100) | ✓ |  |
| county | nvarchar(100) | ✓ |  |
| legal_survey_type | nvarchar(40) | ✓ |  |
| surface_latitude | numeric(15,10) | ✓ |  |
| surface_longitude | numeric(15,10) | ✓ |  |
| ground_elevation | numeric(15,4) | ✓ |  |
| kb_elevation | numeric(15,4) | ✓ |  |
| spud_date | datetime2 | ✓ |  |
| completion_date | datetime2 | ✓ |  |
| final_td | numeric(15,4) | ✓ |  |
| depth_datum | nvarchar(40) | ✓ |  |
| epsg_code | int | ✓ |  |
| api_num | nvarchar(20) | ✓ |  |
| license_num | nvarchar(40) | ✓ |  |
| lease_name | nvarchar(255) | ✓ |  |
| onshore_offshore_ind | nvarchar(10) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ |  |
| abandonment_date | datetime2 | ✓ |  |
| bottom_hole_latitude | numeric(15,10) | ✓ |  |
| bottom_hole_longitude | numeric(15,10) | ✓ |  |
| current_operator_ba_id | nvarchar(40) | ✓ |  |
| original_operator_ba_id | nvarchar(40) | ✓ |  |
| elevation_ouom | nvarchar(40) | ✓ |  |
| formation_at_td | nvarchar(255) | ✓ |  |
| long_lat_source | nvarchar(40) | ✓ |  |
| permit_number | nvarchar(40) | ✓ |  |
| producing_formation | nvarchar(255) | ✓ |  |
| area | nvarchar(100) | ✓ |  |
| operator_name | nvarchar(255) | ✓ |  |
| field_name | nvarchar(255) | ✓ |  |
| protraction_area | nvarchar(100) | ✓ |  |
| h3_r4 | nvarchar(15) | ✓ |  |
| h3_r5 | nvarchar(15) | ✓ |  |
| h3_r6 | nvarchar(15) | ✓ |  |
| h3_r7 | nvarchar(15) | ✓ |  |
| h3_coord_hash | binary(32) | ✓ |  |

**Relationships:** → `dv_strat_interval` on `uwi` (inferred); → `dv_field` on `field_id` (inferred); ← `dv_well_alias` on `uwi`; ← `dv_well_alias` on `uwi`; ← `dv_well_dir_srvy_hdr` on `uwi`; ← `dv_well_dir_srvy_hdr` on `uwi`; ← `dv_well_petro_interp` on `uwi`; ← `dv_well_formation_top` on `uwi`; ← `dv_well_formation_top` on `uwi`; ← `dv_well_pressure` on `uwi`; ← `dv_well_log` on `uwi`; ← `dv_well_log` on `uwi`; ← `dv_well_legal` on `uwi`; ← `dv_well_extension` on `uwi`; ← `dv_prod_entity` on `uwi`; ← `dv_prod_entity` on `uwi`; ← `dv_well_casing` on `uwi`; ← `dv_well_core` on `uwi`; ← `dv_wl_file_catalog` on `uwi`; ← `dv_wl_file_catalog` on `uwi`; ← `dv_well_dst` on `uwi`; ← `dv_well_mud_log` on `uwi`

### `dv_well_alias`

_50 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| alias_id | nvarchar(40) |  | PK |
| alias_name | nvarchar(255) |  |  |
| alias_type | nvarchar(40) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_well` on `uwi`; → `dv_well` on `uwi`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_backup_20260524`

_1,561,042 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | FK |
| well_name | nvarchar(255) | ✓ |  |
| well_num | nvarchar(40) | ✓ |  |
| operator_ba_id | nvarchar(40) | ✓ |  |
| field_id | nvarchar(40) | ✓ | FK |
| well_type | nvarchar(40) | ✓ |  |
| well_status | nvarchar(40) | ✓ |  |
| country | nvarchar(40) | ✓ |  |
| province_state | nvarchar(100) | ✓ |  |
| county | nvarchar(100) | ✓ |  |
| legal_survey_type | nvarchar(40) | ✓ |  |
| surface_latitude | numeric(15,10) | ✓ |  |
| surface_longitude | numeric(15,10) | ✓ |  |
| ground_elevation | numeric(15,4) | ✓ |  |
| kb_elevation | numeric(15,4) | ✓ |  |
| spud_date | datetime2 | ✓ |  |
| completion_date | datetime2 | ✓ |  |
| final_td | numeric(15,4) | ✓ |  |
| depth_datum | nvarchar(40) | ✓ |  |
| epsg_code | int | ✓ |  |
| api_num | nvarchar(20) | ✓ |  |
| license_num | nvarchar(40) | ✓ |  |
| lease_name | nvarchar(255) | ✓ |  |
| onshore_offshore_ind | nvarchar(10) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ |  |
| abandonment_date | datetime2 | ✓ |  |
| bottom_hole_latitude | numeric(15,10) | ✓ |  |
| bottom_hole_longitude | numeric(15,10) | ✓ |  |
| current_operator_ba_id | nvarchar(40) | ✓ |  |
| original_operator_ba_id | nvarchar(40) | ✓ |  |
| elevation_ouom | nvarchar(40) | ✓ |  |
| formation_at_td | nvarchar(255) | ✓ |  |
| long_lat_source | nvarchar(40) | ✓ |  |
| permit_number | nvarchar(40) | ✓ |  |
| producing_formation | nvarchar(255) | ✓ |  |
| area | nvarchar(100) | ✓ |  |
| operator_name | nvarchar(255) | ✓ |  |
| field_name | nvarchar(255) | ✓ |  |

**Relationships:** → `dv_strat_interval` on `uwi` (inferred); → `dv_field` on `field_id` (inferred)

### `dv_well_casing`

_90 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| casing_id | nvarchar(40) |  | PK |
| casing_type | nvarchar(40) |  |  |
| string_num | int | ✓ |  |
| set_date | date |  |  |
| top_depth | float |  |  |
| base_depth | float |  |  |
| depth_ouom | nvarchar(40) |  |  |
| depth_datum | nvarchar(40) |  |  |
| od_in | nvarchar(255) |  |  |
| weight_lb_ft | float |  |  |
| grade | nvarchar(40) |  |  |
| connection_type | nvarchar(40) |  |  |
| cement_top | nvarchar(255) |  |  |
| cement_base | nvarchar(255) |  |  |
| cement_volume_sacks | float |  |  |
| cement_type | nvarchar(40) |  |  |
| burst_rating_psi | float |  |  |
| collapse_rating_psi | float |  |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| source | nvarchar(40) |  | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |

**Relationships:** → `dv_well` on `uwi`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_ext_kgs`

_514,713 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| KID | nvarchar(500) | ✓ |  |
| API_NUMBER | nvarchar(500) | ✓ |  |
| API_NUM_NODASH | nvarchar(500) | ✓ |  |
| LEASE | nvarchar(500) | ✓ |  |
| WELL | nvarchar(500) | ✓ |  |
| FIELD | nvarchar(500) | ✓ |  |
| LATITUDE | nvarchar(500) | ✓ |  |
| LONGITUDE | nvarchar(500) | ✓ |  |
| LONG_LAT_SOURCE | nvarchar(500) | ✓ |  |
| TOWNSHIP | nvarchar(500) | ✓ |  |
| TWN_DIR | nvarchar(500) | ✓ |  |
| RANGE_ | nvarchar(500) | ✓ |  |
| RANGE_DIR | nvarchar(500) | ✓ |  |
| SECTION_ | nvarchar(500) | ✓ |  |
| SPOT | nvarchar(500) | ✓ |  |
| FEET_NORTH | nvarchar(500) | ✓ |  |
| FEET_EAST | nvarchar(500) | ✓ |  |
| FOOT_REF | nvarchar(500) | ✓ |  |
| ORIG_OPERATOR | nvarchar(500) | ✓ |  |
| CURR_OPERATOR | nvarchar(500) | ✓ |  |
| ELEVATION | nvarchar(500) | ✓ |  |
| ELEV_REF | nvarchar(500) | ✓ |  |
| SURFACE_ELEVATION_LIDAR | nvarchar(500) | ✓ |  |
| DEPTH | nvarchar(500) | ✓ |  |
| FORMATION_AT_TOTAL_DEPTH | nvarchar(500) | ✓ |  |
| PRODUCE_FORM | nvarchar(500) | ✓ |  |
| IP_OIL | nvarchar(500) | ✓ |  |
| IP_GAS | nvarchar(500) | ✓ |  |
| IP_WATER | nvarchar(500) | ✓ |  |
| PERMIT | nvarchar(500) | ✓ |  |
| SPUD | nvarchar(500) | ✓ |  |
| COMPLETION | nvarchar(500) | ✓ |  |
| PLUGGING | nvarchar(500) | ✓ |  |
| MODIFIED | nvarchar(500) | ✓ |  |
| OIL_KID | nvarchar(500) | ✓ |  |
| OIL_DOR_ID | nvarchar(500) | ✓ |  |
| GAS_KID | nvarchar(500) | ✓ |  |
| GAS_DOR_ID | nvarchar(500) | ✓ |  |
| KCC_PERMIT | nvarchar(500) | ✓ |  |
| STATUS | nvarchar(500) | ✓ |  |
| STATUS2 | nvarchar(500) | ✓ |  |
| COMMENTS | nvarchar(500) | ✓ |  |
| LEASE_WELL_NAME | nvarchar(500) | ✓ |  |
| loaded_date | datetime2 |  |  |

**Relationships:** → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_ext_michigan_wells`

_92,551 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | FK |
| OBJECTID | nvarchar(500) | ✓ |  |
| PKey | nvarchar(500) | ✓ |  |
| Constructkey | nvarchar(500) | ✓ |  |
| County_fips | nvarchar(500) | ✓ |  |
| Sidetrack | nvarchar(500) | ✓ |  |
| WellNameFull | nvarchar(500) | ✓ |  |
| CompanyNo | nvarchar(500) | ✓ |  |
| StateLand | nvarchar(500) | ✓ |  |
| FederalLand | nvarchar(500) | ✓ |  |
| TownshipName | nvarchar(500) | ✓ |  |
| TRS | nvarchar(500) | ✓ |  |
| QQQS | nvarchar(500) | ✓ |  |
| QQS | nvarchar(500) | ✓ |  |
| QS | nvarchar(500) | ✓ |  |
| well_type | nvarchar(500) | ✓ |  |
| TopWellType | nvarchar(500) | ✓ |  |
| TopWellStatus | nvarchar(500) | ✓ |  |
| Slant | nvarchar(500) | ✓ |  |
| WellboreType | nvarchar(500) | ✓ |  |
| TVD | nvarchar(500) | ✓ |  |
| KOTVD | nvarchar(500) | ✓ |  |
| ReferenceTops | nvarchar(500) | ✓ |  |
| FieldType | nvarchar(500) | ✓ |  |
| PRUNumber | nvarchar(500) | ✓ |  |
| PRUName | nvarchar(500) | ✓ |  |
| ProdFormationCode | nvarchar(500) | ✓ |  |
| PermitDate | nvarchar(500) | ✓ |  |
| PluggingDate | nvarchar(500) | ✓ |  |
| Concentration_H2S | nvarchar(500) | ✓ |  |
| LogsAvailable | nvarchar(500) | ✓ |  |
| mgr_x | nvarchar(500) | ✓ |  |
| mgr_y | nvarchar(500) | ✓ |  |
| source | nvarchar(20) | ✓ |  |

**Relationships:** → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_ext_wy_wogcc`

_142,929 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | FK |
| LEASE_NO | nvarchar(500) | ✓ |  |
| HORIZ_DIR | nvarchar(500) | ✓ |  |
| LAND_TYPE | nvarchar(500) | ✓ |  |
| COUNTYTXT | nvarchar(500) | ✓ |  |
| SEC | nvarchar(500) | ✓ |  |
| TWP | nvarchar(500) | ✓ |  |
| T_DIR | nvarchar(500) | ✓ |  |
| RGE | nvarchar(500) | ✓ |  |
| R_DIR | nvarchar(500) | ✓ |  |
| QTR1 | nvarchar(500) | ✓ |  |
| QTR2 | nvarchar(500) | ✓ |  |
| FOOT1 | nvarchar(500) | ✓ |  |
| FOOT2 | nvarchar(500) | ✓ |  |
| BSEC | nvarchar(500) | ✓ |  |
| BTWP | nvarchar(500) | ✓ |  |
| BT_DIR | nvarchar(500) | ✓ |  |
| BRGE | nvarchar(500) | ✓ |  |
| BR_DIR | nvarchar(500) | ✓ |  |
| BQTR1 | nvarchar(500) | ✓ |  |
| BQTR2 | nvarchar(500) | ✓ |  |
| BFOOT1 | nvarchar(500) | ✓ |  |
| BFOOT2 | nvarchar(500) | ✓ |  |
| PB | nvarchar(500) | ✓ |  |
| COAL_BED | nvarchar(500) | ✓ |  |
| CAPINO | nvarchar(500) | ✓ |  |
| FORM2MON | nvarchar(500) | ✓ |  |
| FORM2YEAR | nvarchar(500) | ✓ |  |
| UNIT_CODE | nvarchar(500) | ✓ |  |
| RECDATE | nvarchar(500) | ✓ |  |
| MD | nvarchar(500) | ✓ |  |
| PBMD | nvarchar(500) | ✓ |  |
| SYMBOL | nvarchar(500) | ✓ |  |
| source | nvarchar(20) | ✓ |  |

**Relationships:** → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_extension`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| attr_name | nvarchar(100) |  | PK |
| attr_value | nvarchar(500) | ✓ |  |
| source | nvarchar(20) |  | PK |

**Relationships:** → `dv_well` on `uwi`; → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_gom_backup`

_54,675 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | FK |
| well_name | nvarchar(255) | ✓ |  |
| well_num | nvarchar(40) | ✓ |  |
| operator_ba_id | nvarchar(40) | ✓ |  |
| field_id | nvarchar(40) | ✓ | FK |
| well_type | nvarchar(40) | ✓ |  |
| well_status | nvarchar(40) | ✓ |  |
| country | nvarchar(40) | ✓ |  |
| province_state | nvarchar(100) | ✓ |  |
| county | nvarchar(100) | ✓ |  |
| legal_survey_type | nvarchar(40) | ✓ |  |
| surface_latitude | numeric(15,10) | ✓ |  |
| surface_longitude | numeric(15,10) | ✓ |  |
| ground_elevation | numeric(15,4) | ✓ |  |
| kb_elevation | numeric(15,4) | ✓ |  |
| spud_date | datetime2 | ✓ |  |
| completion_date | datetime2 | ✓ |  |
| final_td | numeric(15,4) | ✓ |  |
| depth_datum | nvarchar(40) | ✓ |  |
| epsg_code | int | ✓ |  |
| api_num | nvarchar(20) | ✓ |  |
| license_num | nvarchar(40) | ✓ |  |
| lease_name | nvarchar(255) | ✓ |  |
| onshore_offshore_ind | nvarchar(10) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ |  |
| abandonment_date | datetime2 | ✓ |  |
| bottom_hole_latitude | numeric(15,10) | ✓ |  |
| bottom_hole_longitude | numeric(15,10) | ✓ |  |
| current_operator_ba_id | nvarchar(40) | ✓ |  |
| original_operator_ba_id | nvarchar(40) | ✓ |  |
| elevation_ouom | nvarchar(40) | ✓ |  |
| formation_at_td | nvarchar(255) | ✓ |  |
| long_lat_source | nvarchar(40) | ✓ |  |
| permit_number | nvarchar(40) | ✓ |  |
| producing_formation | nvarchar(255) | ✓ |  |
| area | nvarchar(100) | ✓ |  |
| operator_name | nvarchar(255) | ✓ |  |
| field_name | nvarchar(255) | ✓ |  |
| protraction_area | nvarchar(100) | ✓ |  |

**Relationships:** → `dv_strat_interval` on `uwi` (inferred); → `dv_field` on `field_id` (inferred)

### `dv_well_identifier`

_1,563,795 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| well_id | uniqueidentifier |  | PK |
| identifier_type | nvarchar(20) |  | PK |
| identifier_value | nvarchar(40) |  |  |
| source_system | nvarchar(40) | ✓ |  |
| loaded_date | datetime2 |  |  |
| is_primary | bit |  |  |

### `dv_well_legal`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| location_type | nvarchar(10) |  | PK |
| section | nvarchar(10) | ✓ |  |
| township | nvarchar(10) | ✓ |  |
| township_dir | nvarchar(5) | ✓ |  |
| range_num | nvarchar(10) | ✓ |  |
| range_dir | nvarchar(5) | ✓ |  |
| quarter_1 | nvarchar(10) | ✓ |  |
| quarter_2 | nvarchar(10) | ✓ |  |
| footage_1 | nvarchar(50) | ✓ |  |
| footage_2 | nvarchar(50) | ✓ |  |
| principal_meridian | nvarchar(40) | ✓ |  |
| source | nvarchar(20) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |

**Relationships:** → `dv_well` on `uwi`; → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_log`

_51 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| log_id | nvarchar(40) |  | PK |
| log_type | nvarchar(40) | ✓ |  |
| run_num | nvarchar(10) | ✓ |  |
| log_date | datetime2 | ✓ |  |
| service_company_ba_id | nvarchar(40) | ✓ | FK |
| depth_datum | nvarchar(40) | ✓ |  |
| top_depth | numeric(15,4) | ✓ |  |
| base_depth | numeric(15,4) | ✓ |  |
| depth_ouom | nvarchar(40) | ✓ | FK |
| null_value | numeric(15,4) | ✓ |  |
| file_path | nvarchar(1000) | ✓ |  |
| file_format | nvarchar(20) | ✓ |  |
| catalog_id | nvarchar(40) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_well` on `uwi`; → `dv_well` on `uwi`; → `dv_business_associate` on `service_company_ba_id`; → `dv_business_associate` on `service_company_ba_id`; → `dv_r_uom` on `depth_ouom`; → `dv_r_uom` on `depth_ouom`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred); ← `dv_well_log_curve` on `uwi`; ← `dv_well_log_curve` on `log_id`

### `dv_well_log_curve`

_355 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| log_id | nvarchar(40) |  | PK |
| curve_id | nvarchar(40) |  | PK |
| mnemonic | nvarchar(40) |  |  |
| mnemonic_alias | nvarchar(40) | ✓ |  |
| curve_description | nvarchar(255) | ✓ |  |
| curve_unit | nvarchar(40) | ✓ | FK |
| null_value | numeric(15,4) | ✓ |  |
| top_depth | numeric(15,4) | ✓ |  |
| base_depth | numeric(15,4) | ✓ |  |
| depth_ouom | nvarchar(40) | ✓ | FK |
| min_value | numeric(20,6) | ✓ |  |
| max_value | numeric(20,6) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_well_log` on `uwi`; → `dv_well_log` on `log_id`; → `dv_r_uom` on `curve_unit`; → `dv_r_uom` on `curve_unit`; → `dv_r_uom` on `depth_ouom`; → `dv_r_uom` on `depth_ouom`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_mud_log`

_20 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| mud_log_id | nvarchar(40) |  | PK |
| log_date | date |  |  |
| top_depth | float |  |  |
| base_depth | float |  |  |
| depth_ouom | nvarchar(40) |  |  |
| contractor_ba_id | nvarchar(40) | ✓ |  |
| rop_avg | float |  |  |
| rop_ouom | nvarchar(255) |  |  |
| mud_type | nvarchar(40) |  |  |
| mud_weight_avg | float |  |  |
| mud_weight_ouom | nvarchar(40) | ✓ |  |
| file_path | nvarchar(500) | ✓ |  |
| catalog_id | nvarchar(40) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| source | nvarchar(40) |  | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| mud_logger_ba_id | nvarchar(40) | ✓ |  |

**Relationships:** → `dv_well` on `uwi`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred); ← `dv_well_shows` on `uwi`; ← `dv_well_shows` on `mud_log_id`

### `dv_well_petro_interp`

_25 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| interp_id | nvarchar(40) |  | PK |
| interp_name | nvarchar(255) |  |  |
| interp_date | date |  |  |
| analyst_ba_id | nvarchar(40) | ✓ |  |
| software | nvarchar(40) |  |  |
| software_version | nvarchar(40) |  |  |
| gr_log_id | nvarchar(40) | ✓ |  |
| res_log_id | nvarchar(40) | ✓ |  |
| density_log_id | nvarchar(40) | ✓ |  |
| neutron_log_id | nvarchar(40) | ✓ |  |
| sonic_log_id | nvarchar(40) | ✓ |  |
| other_log_inputs | nvarchar(500) | ✓ |  |
| formation_water_resist | float |  |  |
| rw_temperature | float |  |  |
| temperature_ouom | nvarchar(40) | ✓ |  |
| archie_a | float |  |  |
| archie_m | float |  |  |
| archie_n | float |  |  |
| shale_volume_method | nvarchar(40) | ✓ |  |
| porosity_method | nvarchar(40) | ✓ |  |
| fluid_density_g_cc | float |  |  |
| matrix_density_g_cc | float |  |  |
| sw_method | nvarchar(40) |  |  |
| output_file_path | nvarchar(500) | ✓ |  |
| interp_status | nvarchar(40) |  |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| source | nvarchar(40) |  | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 | ✓ |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |

**Relationships:** → `dv_well` on `uwi`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred); ← `dv_well_petro_zone` on `uwi`; ← `dv_well_petro_zone` on `interp_id`

### `dv_well_pressure`

_30 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| pressure_id | nvarchar(40) |  | PK |
| pressure_type | nvarchar(40) |  |  |
| test_date | date |  |  |
| depth | nvarchar(255) |  |  |
| depth_ouom | nvarchar(40) |  |  |
| depth_datum | nvarchar(40) |  |  |
| pressure | nvarchar(255) |  |  |
| pressure_ouom | nvarchar(40) |  |  |
| temperature | float |  |  |
| temperature_ouom | nvarchar(40) | ✓ |  |
| fluid_type | nvarchar(40) |  |  |
| mobility | float |  |  |
| strat_unit_name | nvarchar(40) |  |  |
| tool_type | nvarchar(40) |  |  |
| contractor_ba_id | nvarchar(40) |  |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| source | nvarchar(40) |  | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |

**Relationships:** → `dv_well` on `uwi`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_shows`

_50 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| mud_log_id | nvarchar(40) |  | PK |
| show_id | nvarchar(40) |  | PK |
| show_type | nvarchar(40) |  |  |
| show_rating | nvarchar(255) |  |  |
| top_depth | float |  |  |
| base_depth | float |  |  |
| depth_ouom | nvarchar(40) |  |  |
| strat_unit_name | nvarchar(40) |  |  |
| lithology | nvarchar(40) |  |  |
| total_gas_units | float |  |  |
| c1_pct | float |  |  |
| c2_pct | float |  |  |
| c3_pct | float |  |  |
| ic4_pct | float |  |  |
| nc4_pct | float |  |  |
| ic5_pct | float | ✓ |  |
| nc5_pct | float | ✓ |  |
| fluorescence_color | nvarchar(40) | ✓ |  |
| fluorescence_intensity | nvarchar(40) | ✓ |  |
| cut_color | nvarchar(40) | ✓ |  |
| remark | nvarchar(2000) | ✓ |  |
| source | nvarchar(40) |  | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 | ✓ |  |

**Relationships:** → `dv_well_mud_log` on `uwi`; → `dv_well_mud_log` on `mud_log_id`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred)

### `stg_ai_well`

_142,929 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| abandonment_date | nvarchar(500) | ✓ |  |
| area | nvarchar(500) | ✓ |  |
| bottom_hole_latitude | nvarchar(500) | ✓ |  |
| bottom_hole_longitude | nvarchar(500) | ✓ |  |
| completion_date | nvarchar(500) | ✓ |  |
| county | nvarchar(500) | ✓ |  |
| field_name | nvarchar(500) | ✓ |  |
| final_td | nvarchar(500) | ✓ |  |
| formation_at_td | nvarchar(500) | ✓ |  |
| ground_elevation | nvarchar(500) | ✓ |  |
| kb_elevation | nvarchar(500) | ✓ |  |
| lease_name | nvarchar(500) | ✓ |  |
| operator_name | nvarchar(500) | ✓ |  |
| permit_number | nvarchar(500) | ✓ |  |
| producing_formation | nvarchar(500) | ✓ |  |
| source | nvarchar(500) | ✓ |  |
| spud_date | nvarchar(500) | ✓ |  |
| surface_latitude | nvarchar(500) | ✓ |  |
| surface_longitude | nvarchar(500) | ✓ |  |
| uwi | nvarchar(500) | ✓ | FK |
| well_name | nvarchar(500) | ✓ |  |
| well_status | nvarchar(500) | ✓ |  |
| well_type | nvarchar(500) | ✓ |  |

**Relationships:** → `dv_strat_interval` on `uwi` (inferred)

## 🔧 Completions & Stimulation

### `dv_well_completion`

_5,019 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| completion_id | nvarchar(40) |  | PK |
| completion_type | nvarchar(60) |  |  |
| completion_design | nvarchar(60) | ✓ |  |
| well_orientation | nvarchar(20) | ✓ |  |
| completion_date | date |  |  |
| strat_unit_name | nvarchar(60) | ✓ |  |
| top_depth | float | ✓ |  |
| base_depth | float | ✓ |  |
| measured_td_ft | float | ✓ |  |
| lateral_length_ft | float | ✓ |  |
| depth_ouom | nvarchar(20) |  |  |
| depth_datum | nvarchar(20) |  |  |
| completion_status | nvarchar(40) |  |  |
| primary_fluid | nvarchar(20) |  |  |
| stage_count | int | ✓ |  |
| total_clusters | int | ✓ |  |
| avg_cluster_spacing_ft | float | ✓ |  |
| frac_fluid_system | nvarchar(40) | ✓ |  |
| proppant_type | nvarchar(60) | ✓ |  |
| total_fluid_bbl | float | ✓ |  |
| total_proppant_lbs | float | ✓ |  |
| fluid_intensity_bbl_ft | float | ✓ |  |
| proppant_intensity_lbs_ft | float | ✓ |  |
| tubing_size_in | float | ✓ |  |
| tubing_depth | float | ✓ |  |
| artificial_lift_type | nvarchar(40) | ✓ |  |
| operator_ba_id | nvarchar(120) | ✓ |  |
| contractor_ba_id | nvarchar(120) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| source | nvarchar(40) |  |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |

**Relationships:** → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_perforation`

_60 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| completion_id | nvarchar(40) |  | PK |
| perf_id | nvarchar(40) |  | PK |
| perf_date | date |  |  |
| top_depth | float |  |  |
| base_depth | float |  |  |
| depth_ouom | nvarchar(40) |  |  |
| shot_count | int |  |  |
| shot_density | float |  |  |
| shot_density_ouom | nvarchar(40) | ✓ |  |
| perf_diameter_in | nvarchar(255) |  |  |
| gun_type | nvarchar(40) |  |  |
| phasing_deg | nvarchar(40) |  |  |
| strat_unit_name | nvarchar(40) |  |  |
| perf_status | nvarchar(40) |  |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| source | nvarchar(40) |  | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |

**Relationships:** → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_stimulation`

_143,294 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| completion_id | nvarchar(40) |  | PK |
| stim_id | nvarchar(40) |  | PK |
| stage_num | int |  |  |
| stim_type | nvarchar(40) | ✓ |  |
| stage_date | date | ✓ |  |
| stage_top_depth | float | ✓ |  |
| stage_base_depth | float | ✓ |  |
| num_clusters | int | ✓ |  |
| cluster_spacing_ft | float | ✓ |  |
| fluid_system | nvarchar(40) | ✓ |  |
| fluid_volume_bbl | float | ✓ |  |
| proppant_type | nvarchar(60) | ✓ |  |
| proppant_mesh | nvarchar(40) | ✓ |  |
| proppant_mass_lbs | float | ✓ |  |
| max_proppant_conc_ppg | float | ✓ |  |
| breakdown_pressure_psi | float | ✓ |  |
| isip_psi | float | ✓ |  |
| avg_treating_pressure_psi | float | ✓ |  |
| max_treating_pressure_psi | float | ✓ |  |
| avg_rate_bpm | float | ✓ |  |
| max_rate_bpm | float | ✓ |  |
| screen_out_ind | nvarchar(1) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| source | nvarchar(40) |  |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |

**Relationships:** → `dv_strat_interval` on `uwi` (inferred)

## 📈 Production & Volumes

### `dv_prod_entity`

_50 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| prod_entity_id | nvarchar(40) |  | PK |
| uwi | nvarchar(40) | ✓ | FK |
| field_id | nvarchar(40) | ✓ | FK |
| operator_ba_id | nvarchar(40) | ✓ | FK |
| prod_entity_type | nvarchar(40) | ✓ |  |
| prod_entity_name | nvarchar(255) | ✓ |  |
| first_prod_date | datetime2 | ✓ |  |
| last_prod_date | datetime2 | ✓ |  |
| primary_fluid | nvarchar(40) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_well` on `uwi`; → `dv_well` on `uwi`; → `dv_field` on `field_id`; → `dv_field` on `field_id`; → `dv_business_associate` on `operator_ba_id`; → `dv_business_associate` on `operator_ba_id`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred); ← `dv_prod_volume` on `prod_entity_id`; ← `dv_prod_volume` on `prod_entity_id`; ← `dv_data_quality` on `entity_id` (inferred)

### `dv_prod_volume`

_1,755 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| prod_entity_id | nvarchar(40) |  | PK |
| period_date | nvarchar(7) |  | PK |
| fluid_type | nvarchar(40) |  | PK |
| volume | numeric(20,4) | ✓ |  |
| volume_ouom | nvarchar(40) | ✓ | FK |
| days_on_prod | numeric(5,2) | ✓ |  |
| avg_daily_rate | numeric(20,4) | ✓ |  |
| rate_ouom | nvarchar(40) | ✓ | FK |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_prod_entity` on `prod_entity_id`; → `dv_prod_entity` on `prod_entity_id`; → `dv_r_uom` on `volume_ouom`; → `dv_r_uom` on `volume_ouom`; → `dv_r_uom` on `rate_ouom`; → `dv_r_uom` on `rate_ouom`; → `dv_r_source` on `source`; → `dv_r_source` on `source`

## 🧭 Directional Surveys

### `dv_well_dir_srvy_hdr`

_51 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| survey_id | nvarchar(40) |  | PK |
| survey_type | nvarchar(40) | ✓ |  |
| survey_date | datetime2 | ✓ |  |
| contractor_ba_id | nvarchar(40) | ✓ | FK |
| depth_datum | nvarchar(40) | ✓ |  |
| depth_datum_elevation | numeric(15,4) | ✓ |  |
| survey_top_depth | numeric(15,4) | ✓ |  |
| survey_base_depth | numeric(15,4) | ✓ |  |
| depth_ouom | nvarchar(40) | ✓ | FK |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_well` on `uwi`; → `dv_well` on `uwi`; → `dv_business_associate` on `contractor_ba_id`; → `dv_business_associate` on `contractor_ba_id`; → `dv_r_uom` on `depth_ouom`; → `dv_r_uom` on `depth_ouom`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred); ← `dv_well_dir_srvy_sta` on `uwi`; ← `dv_well_dir_srvy_sta` on `survey_id`

### `dv_well_dir_srvy_sta`

_2,316 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| survey_id | nvarchar(40) |  | PK |
| station_id | nvarchar(40) |  | PK |
| md | numeric(15,4) | ✓ |  |
| incl | numeric(10,4) | ✓ |  |
| azim | numeric(10,4) | ✓ |  |
| tvd | numeric(15,4) | ✓ |  |
| ns_offset | numeric(15,4) | ✓ |  |
| ew_offset | numeric(15,4) | ✓ |  |
| surface_latitude | numeric(15,10) | ✓ |  |
| surface_longitude | numeric(15,10) | ✓ |  |
| dls | numeric(10,4) | ✓ |  |
| depth_ouom | nvarchar(40) | ✓ | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_well_dir_srvy_hdr` on `uwi`; → `dv_well_dir_srvy_hdr` on `survey_id`; → `dv_r_uom` on `depth_ouom`; → `dv_r_uom` on `depth_ouom`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred)

## 🪨 Formations, Tops & Tests

### `dv_strat_interval`

_300 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| strat_unit_id | nvarchar(40) |  | PK |
| interp_id | nvarchar(40) |  | PK |
| interval_id | nvarchar(40) |  | PK |
| interval_type | nvarchar(40) | ✓ |  |
| interval_name | nvarchar(255) | ✓ |  |
| top_depth | numeric(15,4) | ✓ |  |
| base_depth | numeric(15,4) | ✓ |  |
| net_thickness | numeric(15,4) | ✓ |  |
| depth_ouom | nvarchar(40) | ✓ | FK |
| porosity | numeric(10,4) | ✓ |  |
| water_saturation | numeric(10,4) | ✓ |  |
| permeability | numeric(15,4) | ✓ |  |
| perm_ouom | nvarchar(40) | ✓ | FK |
| fluid_type | nvarchar(40) | ✓ |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_well_formation_top` on `uwi`; → `dv_well_formation_top` on `strat_unit_id`; → `dv_well_formation_top` on `interp_id`; → `dv_r_uom` on `depth_ouom`; → `dv_r_uom` on `depth_ouom`; → `dv_r_uom` on `perm_ouom`; → `dv_r_uom` on `perm_ouom`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; ← `_stg_kgs_h3_backfill` on `uwi` (inferred); ← `dv_global_file_catalog` on `uwi` (inferred); ← `dv_prod_entity` on `uwi` (inferred); ← `dv_stg_well` on `uwi` (inferred); ← `dv_well` on `uwi` (inferred); ← `dv_well_alias` on `uwi` (inferred); ← `dv_well_backup_20260524` on `uwi` (inferred); ← `dv_well_casing` on `uwi` (inferred); ← `dv_well_completion` on `uwi` (inferred); ← `dv_well_core` on `uwi` (inferred); ← `dv_well_core_photo` on `uwi` (inferred); ← `dv_well_core_sample` on `uwi` (inferred); ← `dv_well_dir_srvy_hdr` on `uwi` (inferred); ← `dv_well_dir_srvy_sta` on `uwi` (inferred); ← `dv_well_dst` on `uwi` (inferred); ← `dv_well_dst_period` on `uwi` (inferred); ← `dv_well_ext_kgs` on `uwi` (inferred); ← `dv_well_ext_michigan_wells` on `uwi` (inferred); ← `dv_well_ext_wy_wogcc` on `uwi` (inferred); ← `dv_well_extension` on `uwi` (inferred); ← `dv_well_formation_top` on `uwi` (inferred); ← `dv_well_gom_backup` on `uwi` (inferred); ← `dv_well_legal` on `uwi` (inferred); ← `dv_well_log` on `uwi` (inferred); ← `dv_well_log_curve` on `uwi` (inferred); ← `dv_well_mud_log` on `uwi` (inferred); ← `dv_well_perforation` on `uwi` (inferred); ← `dv_well_petro_interp` on `uwi` (inferred); ← `dv_well_petro_zone` on `uwi` (inferred); ← `dv_well_pressure` on `uwi` (inferred); ← `dv_well_shows` on `uwi` (inferred); ← `dv_well_stimulation` on `uwi` (inferred); ← `dv_wl_file_catalog` on `uwi` (inferred); ← `stg_ai_ext` on `uwi` (inferred); ← `stg_ai_well` on `uwi` (inferred)

### `dv_well_core`

_20 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| core_id | nvarchar(40) |  | PK |
| core_num | int |  |  |
| core_type | nvarchar(40) |  |  |
| core_show | nvarchar(255) |  |  |
| top_depth | float |  |  |
| base_depth | float |  |  |
| depth_ouom | nvarchar(40) |  |  |
| depth_datum | nvarchar(40) |  |  |
| core_length | nvarchar(255) |  |  |
| recovery_length | float |  |  |
| recovery_pct | float |  |  |
| length_ouom | nvarchar(40) |  |  |
| core_date | date |  |  |
| cutting_company_ba_id | nvarchar(40) |  |  |
| analysis_company_ba_id | nvarchar(40) |  |  |
| strat_unit_name | nvarchar(40) |  |  |
| file_path | nvarchar(500) | ✓ |  |
| photo_count | int |  |  |
| photo_folder_path | nvarchar(500) | ✓ |  |
| has_uv_photos | nvarchar(40) |  |  |
| has_thin_section_photos | nvarchar(40) |  |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| source | nvarchar(40) |  | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |

**Relationships:** → `dv_well` on `uwi`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred); ← `dv_well_core_sample` on `uwi`; ← `dv_well_core_sample` on `core_id`; ← `dv_well_core_photo` on `uwi`; ← `dv_well_core_photo` on `core_id`

### `dv_well_core_photo`

_40 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| core_id | nvarchar(40) |  | PK |
| photo_id | nvarchar(40) |  | PK |
| photo_type | nvarchar(40) |  |  |
| lighting | nvarchar(255) |  |  |
| top_depth | float |  |  |
| base_depth | float |  |  |
| depth_ouom | nvarchar(40) |  |  |
| tray_num | int |  |  |
| photo_date | date |  |  |
| file_path | nvarchar(500) | ✓ |  |
| file_name | nvarchar(255) | ✓ |  |
| file_ext | nvarchar(20) | ✓ |  |
| file_size_kb | float |  |  |
| file_hash | nvarchar(64) | ✓ |  |
| resolution_dpi | float |  |  |
| width_px | float |  |  |
| height_px | float |  |  |
| sample_id | nvarchar(40) | ✓ |  |
| catalog_id | nvarchar(40) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| source | nvarchar(40) |  | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |

**Relationships:** → `dv_well_core` on `uwi`; → `dv_well_core` on `core_id`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_core_sample`

_80 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| core_id | nvarchar(40) |  | PK |
| sample_id | nvarchar(40) |  | PK |
| sample_type | nvarchar(40) | ✓ |  |
| sample_depth | float |  |  |
| top_depth | float |  |  |
| base_depth | float |  |  |
| depth_ouom | nvarchar(40) |  |  |
| porosity_frac | float |  |  |
| permeability_air_md | float |  |  |
| permeability_klinkenberg_md | float |  |  |
| water_saturation_frac | float |  |  |
| grain_density_g_cc | float |  |  |
| bulk_density_g_cc | float |  |  |
| oil_saturation_frac | float | ✓ |  |
| gas_saturation_frac | float | ✓ |  |
| formation_factor | float | ✓ |  |
| cementation_exponent | float | ✓ |  |
| saturation_exponent | float | ✓ |  |
| lithology | nvarchar(40) | ✓ |  |
| visual_porosity | nvarchar(40) | ✓ |  |
| hydrocarbon_show | nvarchar(40) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| source | nvarchar(40) |  | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |

**Relationships:** → `dv_well_core` on `uwi`; → `dv_well_core` on `core_id`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_dst`

_15 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| dst_id | nvarchar(40) |  | PK |
| dst_num | int |  |  |
| test_type | nvarchar(40) |  |  |
| test_date | date |  |  |
| top_depth | float |  |  |
| base_depth | float |  |  |
| depth_ouom | nvarchar(40) |  |  |
| depth_datum | nvarchar(40) |  |  |
| strat_unit_name | nvarchar(40) |  |  |
| tool_type | nvarchar(40) |  |  |
| perforation_top | nvarchar(255) |  |  |
| perforation_base | nvarchar(255) |  |  |
| max_shut_in_pressure | float | ✓ |  |
| final_shut_in_pressure | float |  |  |
| pressure_ouom | nvarchar(40) |  |  |
| max_oil_rate | float | ✓ |  |
| max_gas_rate | float | ✓ |  |
| max_water_rate | float | ✓ |  |
| rate_ouom | nvarchar(40) |  |  |
| gor | float | ✓ |  |
| api_gravity | float | ✓ |  |
| h2s_pct | float |  |  |
| co2_pct | float |  |  |
| test_result | nvarchar(40) |  |  |
| contractor_ba_id | nvarchar(40) |  |  |
| file_path | nvarchar(500) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| source | nvarchar(40) |  | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |

**Relationships:** → `dv_well` on `uwi`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred); ← `dv_well_dst_period` on `uwi`; ← `dv_well_dst_period` on `dst_id`

### `dv_well_dst_period`

_60 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| dst_id | nvarchar(40) |  | PK |
| period_id | nvarchar(40) |  | PK |
| period_type | nvarchar(40) |  |  |
| period_seq | int |  |  |
| duration_min | float |  |  |
| start_pressure | float |  |  |
| end_pressure | float |  |  |
| pressure_ouom | nvarchar(40) |  |  |
| avg_oil_rate | float |  |  |
| avg_gas_rate | float |  |  |
| avg_water_rate | float |  |  |
| rate_ouom | nvarchar(40) |  |  |
| choke_size | nvarchar(20) | ✓ |  |
| remark | nvarchar(2000) | ✓ |  |
| source | nvarchar(40) |  | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |

**Relationships:** → `dv_well_dst` on `uwi`; → `dv_well_dst` on `dst_id`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred)

### `dv_well_formation_top`

_844 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| strat_unit_id | nvarchar(40) |  | PK |
| interp_id | nvarchar(40) |  | PK |
| strat_name_set | nvarchar(255) | ✓ |  |
| strat_unit_name | nvarchar(255) | ✓ |  |
| strat_unit_type | nvarchar(40) | ✓ |  |
| strat_unit_subtype | nvarchar(40) | ✓ |  |
| age_top_ma | numeric(10,3) | ✓ |  |
| age_base_ma | numeric(10,3) | ✓ |  |
| lithology | nvarchar(100) | ✓ |  |
| top_depth | numeric(15,4) | ✓ |  |
| base_depth | numeric(15,4) | ✓ |  |
| gross_thickness | numeric(16,4) | ✓ |  |
| depth_ouom | nvarchar(40) | ✓ | FK |
| depth_datum | nvarchar(40) | ✓ |  |
| tvd_top | numeric(15,4) | ✓ |  |
| tvd_base | numeric(15,4) | ✓ |  |
| owc_depth | numeric(15,4) | ✓ |  |
| goc_depth | numeric(15,4) | ✓ |  |
| gwc_depth | numeric(15,4) | ✓ |  |
| interp_date | datetime2 | ✓ |  |
| interpreter_ba_id | nvarchar(40) | ✓ | FK |
| confidence_level | nvarchar(40) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_well` on `uwi`; → `dv_well` on `uwi`; → `dv_r_uom` on `depth_ouom`; → `dv_r_uom` on `depth_ouom`; → `dv_business_associate` on `interpreter_ba_id`; → `dv_business_associate` on `interpreter_ba_id`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred); ← `dv_strat_interval` on `uwi`; ← `dv_strat_interval` on `strat_unit_id`; ← `dv_strat_interval` on `interp_id`

### `dv_well_petro_zone`

_50 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) |  | PK |
| interp_id | nvarchar(40) |  | PK |
| zone_id | nvarchar(40) |  | PK |
| zone_name | nvarchar(255) |  |  |
| zone_type | nvarchar(40) |  |  |
| top_depth | float |  |  |
| base_depth | float |  |  |
| depth_ouom | nvarchar(40) |  |  |
| depth_datum | nvarchar(40) |  |  |
| tvd_top | float | ✓ |  |
| tvd_base | float | ✓ |  |
| strat_unit_id | nvarchar(40) |  |  |
| strat_interp_id | nvarchar(40) |  |  |
| strat_unit_name | nvarchar(40) |  |  |
| gross_thickness | float |  |  |
| net_thickness | float |  |  |
| net_to_gross | float |  |  |
| vsh_avg | float | ✓ |  |
| vsh_min | float | ✓ |  |
| vsh_max | float | ✓ |  |
| phi_total_avg | float | ✓ |  |
| phi_effective_avg | float | ✓ |  |
| phi_method | nvarchar(40) |  |  |
| sw_avg | float | ✓ |  |
| sw_min | float | ✓ |  |
| sw_max | float | ✓ |  |
| sw_method | nvarchar(40) |  |  |
| sh_avg | float | ✓ |  |
| perm_avg_md | float | ✓ |  |
| perm_geomean_md | float | ✓ |  |
| perm_method | nvarchar(40) |  |  |
| bvw_avg | float |  |  |
| bvh_avg | float |  |  |
| fluid_type | nvarchar(40) |  |  |
| pay_flag | nvarchar(40) |  |  |
| pay_cutoff_phi | float | ✓ |  |
| pay_cutoff_sw | float | ✓ |  |
| pay_cutoff_vsh | float | ✓ |  |
| hcpv | float | ✓ |  |
| hcpv_ouom | nvarchar(40) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| source | nvarchar(40) |  | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 | ✓ |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |

**Relationships:** → `dv_well_petro_interp` on `uwi`; → `dv_well_petro_interp` on `interp_id`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred)

## 📚 Reference & Lookups

### `dv_business_associate`

_117,357 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| ba_id | nvarchar(40) |  | PK |
| ba_type | nvarchar(40) | ✓ |  |
| ba_name | nvarchar(255) |  |  |
| ba_name_alias | nvarchar(255) | ✓ |  |
| short_name | nvarchar(40) | ✓ |  |
| address_1 | nvarchar(255) | ✓ |  |
| address_2 | nvarchar(255) | ✓ |  |
| city | nvarchar(100) | ✓ |  |
| state_province | nvarchar(100) | ✓ |  |
| postal_code | nvarchar(20) | ✓ |  |
| country | nvarchar(40) | ✓ |  |
| phone_num | nvarchar(40) | ✓ |  |
| email_addr | nvarchar(255) | ✓ |  |
| duns_num | nvarchar(20) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_r_source` on `source`; → `dv_r_source` on `source`; ← `dv_well_dir_srvy_hdr` on `contractor_ba_id`; ← `dv_well_dir_srvy_hdr` on `contractor_ba_id`; ← `dv_well_formation_top` on `interpreter_ba_id`; ← `dv_well_formation_top` on `interpreter_ba_id`; ← `dv_well_log` on `service_company_ba_id`; ← `dv_well_log` on `service_company_ba_id`; ← `dv_seis_set` on `contractor_ba_id`; ← `dv_seis_set` on `contractor_ba_id`; ← `dv_seis_set` on `operator_ba_id`; ← `dv_seis_set` on `operator_ba_id`; ← `dv_prod_entity` on `operator_ba_id`; ← `dv_prod_entity` on `operator_ba_id`; ← `dv_field` on `operator_ba_id`; ← `dv_field` on `operator_ba_id`; ← `dv_load_batch` on `operator_ba_id`; ← `dv_load_batch` on `operator_ba_id`

### `dv_field`

_64,173 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| field_id | nvarchar(40) |  | PK |
| field_name | nvarchar(255) |  |  |
| field_type | nvarchar(40) | ✓ |  |
| country | nvarchar(40) | ✓ |  |
| province_state | nvarchar(100) | ✓ |  |
| county | nvarchar(100) | ✓ |  |
| basin_name | nvarchar(255) | ✓ |  |
| operator_ba_id | nvarchar(40) | ✓ | FK |
| discovery_date | datetime2 | ✓ |  |
| field_status | nvarchar(40) | ✓ |  |
| onshore_offshore_ind | nvarchar(10) | ✓ |  |
| surface_latitude | numeric(15,10) | ✓ |  |
| surface_longitude | numeric(15,10) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_business_associate` on `operator_ba_id`; → `dv_business_associate` on `operator_ba_id`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; ← `dv_prod_entity` on `field_id`; ← `dv_prod_entity` on `field_id`; ← `_stg_kgs_h3_backfill` on `field_id` (inferred); ← `dv_well` on `field_id` (inferred); ← `dv_well_backup_20260524` on `field_id` (inferred); ← `dv_well_gom_backup` on `field_id` (inferred)

### `dv_r_source`

_32 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| source | nvarchar(40) |  | PK |
| short_name | nvarchar(40) | ✓ |  |
| long_name | nvarchar(255) | ✓ |  |
| remark | nvarchar(2000) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |

**Relationships:** ← `dv_well_alias` on `source`; ← `dv_well_alias` on `source`; ← `dv_well_perforation` on `source`; ← `dv_column_map` on `source`; ← `dv_column_map` on `source`; ← `dv_well_dir_srvy_hdr` on `source`; ← `dv_well_dir_srvy_hdr` on `source`; ← `dv_data_quality` on `source`; ← `dv_data_quality` on `source`; ← `dv_well_petro_interp` on `source`; ← `dv_well_dir_srvy_sta` on `source`; ← `dv_well_dir_srvy_sta` on `source`; ← `dv_country` on `source`; ← `dv_country` on `source`; ← `dv_well_petro_zone` on `source`; ← `dv_well_formation_top` on `source`; ← `dv_well_formation_top` on `source`; ← `dv_province_state` on `source`; ← `dv_province_state` on `source`; ← `dv_well_pressure` on `source`; ← `dv_county` on `source`; ← `dv_county` on `source`; ← `dv_strat_interval` on `source`; ← `dv_strat_interval` on `source`; ← `dv_basin` on `source`; ← `dv_basin` on `source`; ← `dv_well_log` on `source`; ← `dv_well_log` on `source`; ← `dv_plss_township` on `source`; ← `dv_plss_township` on `source`; ← `dv_well_log_curve` on `source`; ← `dv_well_log_curve` on `source`; ← `dv_ocs_block` on `source`; ← `dv_ocs_block` on `source`; ← `dv_seis_set` on `source`; ← `dv_seis_set` on `source`; ← `dv_seis_line` on `source`; ← `dv_seis_line` on `source`; ← `dv_prod_entity` on `source`; ← `dv_prod_entity` on `source`; ← `dv_well_casing` on `source`; ← `dv_prod_volume` on `source`; ← `dv_prod_volume` on `source`; ← `dv_well_core` on `source`; ← `dv_source` on `source_ref`; ← `dv_source` on `source_ref`; ← `dv_wl_file_catalog` on `source`; ← `dv_wl_file_catalog` on `source`; ← `dv_well_core_sample` on `source`; ← `dv_business_associate` on `source`; ← `dv_business_associate` on `source`; ← `dv_well_core_photo` on `source`; ← `dv_seis_file_catalog` on `source`; ← `dv_seis_file_catalog` on `source`; ← `dv_field` on `source`; ← `dv_field` on `source`; ← `dv_well_dst` on `source`; ← `dv_global_file_catalog` on `source`; ← `dv_global_file_catalog` on `source`; ← `dv_well_dst_period` on `source`; ← `dv_spatial_layer` on `source`; ← `dv_spatial_layer` on `source`; ← `dv_well_mud_log` on `source`; ← `dv_load_batch` on `source`; ← `dv_load_batch` on `source`; ← `dv_well_shows` on `source`

### `dv_r_uom`

_142 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uom_code | nvarchar(40) |  | PK |
| unit_of_measure | nvarchar(255) | ✓ |  |
| uom_description | nvarchar(2000) | ✓ |  |
| uom_type | nvarchar(40) | ✓ |  |
| si_equivalent | numeric(20,10) | ✓ |  |
| si_uom_code | nvarchar(40) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |

**Relationships:** ← `dv_well_dir_srvy_hdr` on `depth_ouom`; ← `dv_well_dir_srvy_hdr` on `depth_ouom`; ← `dv_well_dir_srvy_sta` on `depth_ouom`; ← `dv_well_dir_srvy_sta` on `depth_ouom`; ← `dv_well_formation_top` on `depth_ouom`; ← `dv_well_formation_top` on `depth_ouom`; ← `dv_strat_interval` on `depth_ouom`; ← `dv_strat_interval` on `depth_ouom`; ← `dv_strat_interval` on `perm_ouom`; ← `dv_strat_interval` on `perm_ouom`; ← `dv_well_log` on `depth_ouom`; ← `dv_well_log` on `depth_ouom`; ← `dv_well_log_curve` on `curve_unit`; ← `dv_well_log_curve` on `curve_unit`; ← `dv_well_log_curve` on `depth_ouom`; ← `dv_well_log_curve` on `depth_ouom`; ← `dv_prod_volume` on `volume_ouom`; ← `dv_prod_volume` on `volume_ouom`; ← `dv_prod_volume` on `rate_ouom`; ← `dv_prod_volume` on `rate_ouom`

### `dv_r_well_status`

_26 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| well_status | nvarchar(40) |  | PK |
| short_name | nvarchar(40) | ✓ |  |
| long_name | nvarchar(255) | ✓ |  |
| remark | nvarchar(2000) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |

### `dv_r_well_type`

_51 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| well_type | nvarchar(40) |  | PK |
| short_name | nvarchar(40) | ✓ |  |
| long_name | nvarchar(255) | ✓ |  |
| remark | nvarchar(2000) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |

## 🗺 Spatial & Political

### `dv_basin`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| basin_id | nvarchar(40) |  | PK |
| basin_name | nvarchar(255) |  |  |
| basin_type | nvarchar(40) | ✓ |  |
| country_code | nvarchar(3) | ✓ | FK |
| region | nvarchar(100) | ✓ |  |
| area_km2 | numeric(15,4) | ✓ |  |
| centroid_latitude | numeric(15,10) | ✓ |  |
| centroid_longitude | numeric(15,10) | ✓ |  |
| primary_play_type | nvarchar(40) | ✓ |  |
| gdm_basin_id | nvarchar(40) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_country` on `country_code`; → `dv_country` on `country_code`; → `dv_r_source` on `source`; → `dv_r_source` on `source`

### `dv_county`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| county_id | nvarchar(40) |  | PK |
| province_state_id | nvarchar(10) |  | FK |
| country_code | nvarchar(3) |  | FK |
| county_name | nvarchar(255) |  |  |
| county_type | nvarchar(40) | ✓ |  |
| fips_state_code | nvarchar(3) | ✓ |  |
| fips_county_code | nvarchar(3) | ✓ |  |
| fips_full | nvarchar(5) | ✓ |  |
| tiger_geoid | nvarchar(20) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_province_state` on `province_state_id`; → `dv_province_state` on `province_state_id`; → `dv_country` on `country_code`; → `dv_country` on `country_code`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; ← `dv_plss_township` on `county_id`; ← `dv_plss_township` on `county_id`

### `dv_ocs_block`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| ocs_block_id | nvarchar(20) |  | PK |
| planning_area | nvarchar(100) | ✓ |  |
| area_code | nvarchar(10) | ✓ |  |
| block_num | nvarchar(10) | ✓ |  |
| block_name | nvarchar(255) | ✓ |  |
| protraction_name | nvarchar(255) | ✓ |  |
| water_depth_m | numeric(10,2) | ✓ |  |
| country_code | nvarchar(3) | ✓ | FK |
| centroid_latitude | numeric(15,10) | ✓ |  |
| centroid_longitude | numeric(15,10) | ✓ |  |
| bbox_wkt | nvarchar(500) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_country` on `country_code`; → `dv_country` on `country_code`; → `dv_r_source` on `source`; → `dv_r_source` on `source`

### `dv_plss_township`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| plss_id | nvarchar(20) |  | PK |
| state_fips | nvarchar(3) | ✓ |  |
| township_num | nvarchar(10) | ✓ |  |
| range_num | nvarchar(10) | ✓ |  |
| section_num | nvarchar(5) | ✓ |  |
| principal_meridian | nvarchar(40) | ✓ |  |
| county_id | nvarchar(40) | ✓ | FK |
| province_state_id | nvarchar(10) | ✓ | FK |
| township_label | nvarchar(100) | ✓ |  |
| centroid_latitude | numeric(15,10) | ✓ |  |
| centroid_longitude | numeric(15,10) | ✓ |  |
| bbox_wkt | nvarchar(500) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_county` on `county_id`; → `dv_county` on `county_id`; → `dv_province_state` on `province_state_id`; → `dv_province_state` on `province_state_id`; → `dv_r_source` on `source`; → `dv_r_source` on `source`

### `dv_province_state`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| province_state_id | nvarchar(10) |  | PK |
| country_code | nvarchar(3) |  | FK |
| province_state_name | nvarchar(255) |  |  |
| province_state_abbrev | nvarchar(10) | ✓ |  |
| province_state_type | nvarchar(40) | ✓ |  |
| fips_code | nvarchar(5) | ✓ |  |
| capital_city | nvarchar(100) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_country` on `country_code`; → `dv_country` on `country_code`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; ← `dv_county` on `province_state_id`; ← `dv_county` on `province_state_id`; ← `dv_plss_township` on `province_state_id`; ← `dv_plss_township` on `province_state_id`

### `dv_spatial_layer`

_2 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| layer_id | nvarchar(40) |  | PK |
| layer_name | nvarchar(255) |  |  |
| layer_type | nvarchar(40) | ✓ |  |
| layer_category | nvarchar(40) | ✓ |  |
| epsg_code | int | ✓ |  |
| file_path | nvarchar(1000) | ✓ |  |
| feature_count | int | ✓ |  |
| bbox_min_lat | numeric(15,10) | ✓ |  |
| bbox_max_lat | numeric(15,10) | ✓ |  |
| bbox_min_lon | numeric(15,10) | ✓ |  |
| bbox_max_lon | numeric(15,10) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |
| geometry_wkt | nvarchar(MAX) | ✓ |  |
| source_type | nvarchar(40) | ✓ |  |
| style_color | nvarchar(20) | ✓ |  |
| style_weight | numeric(5,2) | ✓ |  |
| style_opacity | numeric(5,2) | ✓ |  |
| style_fill_color | nvarchar(20) | ✓ |  |
| style_fill_opacity | numeric(5,2) | ✓ |  |
| style_dash | nvarchar(40) | ✓ |  |
| tooltip_fields | nvarchar(500) | ✓ |  |
| display_order | int | ✓ |  |

**Relationships:** → `dv_r_source` on `source`; → `dv_r_source` on `source`

### `state_polygon`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| state_abbrev | varchar(2) |  | PK |
| state_name | nvarchar(50) |  |  |
| fips_code | varchar(2) | ✓ |  |
| min_lat | decimal(11,7) | ✓ |  |
| max_lat | decimal(11,7) | ✓ |  |
| min_lon | decimal(11,7) | ✓ |  |
| max_lon | decimal(11,7) | ✓ |  |
| state_polygon | geography | ✓ |  |
| loaded_date | datetime2 | ✓ |  |
| source | nvarchar(100) | ✓ |  |

## 📁 Documents & Catalog

### `document_location`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| doc_loc_id | bigint |  | PK |
| inventory_id | nvarchar(40) |  | FK |
| source_table | varchar(50) |  |  |
| latitude | decimal(11,7) |  |  |
| longitude | decimal(11,7) |  |  |
| coord_precision | tinyint | ✓ |  |
| file_path | nvarchar(1000) | ✓ |  |
| file_format | nvarchar(20) | ✓ |  |
| doc_type | nvarchar(100) | ✓ |  |
| uwi_in_doc | nvarchar(40) | ✓ |  |
| well_name_in_doc | nvarchar(255) | ✓ |  |
| operator_in_doc | nvarchar(255) | ✓ |  |
| state_in_doc | nvarchar(50) | ✓ |  |
| county_in_doc | nvarchar(100) | ✓ |  |
| precision_ok | bit | ✓ |  |
| state_bbox_ok | bit | ✓ |  |
| county_match_ok | bit | ✓ |  |
| duplicate_of | bigint | ✓ |  |
| confidence | decimal(5,4) | ✓ |  |
| curation_status | nvarchar(20) |  |  |
| curated_by | nvarchar(100) | ✓ |  |
| curated_date | datetime2 | ✓ |  |
| curation_notes | nvarchar(MAX) | ✓ |  |
| promoted_to_well_id | bigint | ✓ |  |
| promoted_date | datetime2 | ✓ |  |
| row_created_date | datetime2 |  |  |
| row_changed_date | datetime2 |  |  |

**Relationships:** → `dv_global_file_catalog` on `inventory_id` (inferred)

### `dv_global_file_catalog`

_1,283 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| inventory_id | nvarchar(40) |  | PK |
| full_path | nvarchar(1000) |  |  |
| file_name | nvarchar(500) |  |  |
| file_ext | nvarchar(20) | ✓ |  |
| file_size_kb | numeric(15,2) | ✓ |  |
| file_hash | nvarchar(64) | ✓ |  |
| file_hash_full | nvarchar(64) | ✓ |  |
| duplicate_group | nvarchar(64) | ✓ |  |
| modified_date | datetime2 | ✓ |  |
| scan_date | datetime2 |  |  |
| doc_type_group | nvarchar(40) | ✓ |  |
| doc_type | nvarchar(40) | ✓ |  |
| catalog_status | nvarchar(20) | ✓ |  |
| catalog_table | nvarchar(80) | ✓ |  |
| catalog_id | nvarchar(40) | ✓ |  |
| ppdm_loaded_ind | nvarchar(1) |  |  |
| root_path | nvarchar(500) | ✓ |  |
| uwi | nvarchar(40) | ✓ | FK |
| well_name | nvarchar(255) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_r_source` on `source`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred); ← `document_location` on `inventory_id` (inferred)

### `dv_seis_file_catalog`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| catalog_id | nvarchar(40) |  | PK |
| seis_set_id | nvarchar(40) | ✓ | FK |
| full_path | nvarchar(1000) |  |  |
| file_name | nvarchar(500) |  |  |
| file_ext | nvarchar(20) | ✓ |  |
| file_size_kb | numeric(15,2) | ✓ |  |
| file_hash | nvarchar(64) | ✓ |  |
| file_format | nvarchar(20) | ✓ |  |
| segy_revision | nvarchar(10) | ✓ |  |
| trace_count | int | ✓ |  |
| sample_rate_ms | numeric(10,4) | ✓ |  |
| record_length_ms | numeric(10,3) | ✓ |  |
| line_name_in_file | nvarchar(255) | ✓ |  |
| survey_name_in_file | nvarchar(255) | ✓ |  |
| shot_point_count | int | ✓ |  |
| catalog_status | nvarchar(20) | ✓ |  |
| catalog_date | datetime2 | ✓ |  |
| error_msg | nvarchar(2000) | ✓ |  |
| root_path | nvarchar(500) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_seis_set` on `seis_set_id`; → `dv_seis_set` on `seis_set_id`; → `dv_r_source` on `source`; → `dv_r_source` on `source`

### `dv_wl_file_catalog`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| catalog_id | nvarchar(40) |  | PK |
| uwi | nvarchar(40) | ✓ | FK |
| full_path | nvarchar(1000) |  |  |
| file_name | nvarchar(500) |  |  |
| file_ext | nvarchar(20) | ✓ |  |
| file_size_kb | numeric(15,2) | ✓ |  |
| file_hash | nvarchar(64) | ✓ |  |
| file_format | nvarchar(20) | ✓ |  |
| las_version | nvarchar(10) | ✓ |  |
| well_name_in_file | nvarchar(255) | ✓ |  |
| uwi_in_file | nvarchar(40) | ✓ |  |
| service_company | nvarchar(255) | ✓ |  |
| log_date | datetime2 | ✓ |  |
| top_depth | numeric(15,4) | ✓ |  |
| base_depth | numeric(15,4) | ✓ |  |
| depth_ouom | nvarchar(40) | ✓ |  |
| curve_count | int | ✓ |  |
| curve_list | nvarchar(2000) | ✓ |  |
| ppdm_loaded_ind | nvarchar(1) |  |  |
| ppdm_log_id | nvarchar(40) | ✓ |  |
| catalog_status | nvarchar(20) | ✓ |  |
| catalog_date | datetime2 | ✓ |  |
| error_msg | nvarchar(2000) | ✓ |  |
| root_path | nvarchar(500) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_well` on `uwi`; → `dv_well` on `uwi`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; → `dv_strat_interval` on `uwi` (inferred)

## 📦 Other

### `_stg_kgs_h3_backfill`

_514,713 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(40) | ✓ | FK |
| well_name | nvarchar(255) | ✓ |  |
| well_num | nvarchar(40) | ✓ |  |
| operator_ba_id | nvarchar(40) | ✓ |  |
| field_id | nvarchar(40) | ✓ | FK |
| well_type | nvarchar(40) | ✓ |  |
| well_status | nvarchar(40) | ✓ |  |
| country | nvarchar(40) | ✓ |  |
| province_state | nvarchar(40) | ✓ |  |
| county | nvarchar(80) | ✓ |  |
| legal_survey_type | nvarchar(40) | ✓ |  |
| surface_latitude | nvarchar(50) | ✓ |  |
| surface_longitude | nvarchar(50) | ✓ |  |
| ground_elevation | nvarchar(50) | ✓ |  |
| kb_elevation | nvarchar(50) | ✓ |  |
| spud_date | nvarchar(50) | ✓ |  |
| completion_date | nvarchar(50) | ✓ |  |
| final_td | nvarchar(50) | ✓ |  |
| depth_datum | nvarchar(40) | ✓ |  |
| epsg_code | nvarchar(20) | ✓ |  |
| api_num | nvarchar(40) | ✓ |  |
| license_num | nvarchar(40) | ✓ |  |
| lease_name | nvarchar(255) | ✓ |  |
| onshore_offshore_ind | nvarchar(20) | ✓ |  |
| active_ind | nvarchar(5) | ✓ |  |
| remark | nvarchar(MAX) | ✓ |  |
| row_created_by | nvarchar(40) | ✓ |  |
| row_created_date | nvarchar(50) | ✓ |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | nvarchar(50) | ✓ |  |
| source | nvarchar(40) | ✓ |  |
| abandonment_date | nvarchar(50) | ✓ |  |
| bottom_hole_latitude | nvarchar(50) | ✓ |  |
| bottom_hole_longitude | nvarchar(50) | ✓ |  |
| current_operator_ba_id | nvarchar(40) | ✓ |  |
| original_operator_ba_id | nvarchar(40) | ✓ |  |
| elevation_ouom | nvarchar(20) | ✓ |  |
| formation_at_td | nvarchar(255) | ✓ |  |
| long_lat_source | nvarchar(40) | ✓ |  |
| permit_number | nvarchar(40) | ✓ |  |
| producing_formation | nvarchar(255) | ✓ |  |
| area | nvarchar(40) | ✓ |  |
| operator_name | nvarchar(255) | ✓ |  |
| field_name | nvarchar(255) | ✓ |  |
| protraction_area | nvarchar(40) | ✓ |  |
| h3_r4 | nvarchar(15) | ✓ |  |
| h3_r5 | nvarchar(15) | ✓ |  |
| h3_r6 | nvarchar(15) | ✓ |  |
| h3_r7 | nvarchar(15) | ✓ |  |
| h3_coord_hash | nvarchar(80) | ✓ |  |

**Relationships:** → `dv_strat_interval` on `uwi` (inferred); → `dv_field` on `field_id` (inferred)

### `dv_column_map`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| map_id | nvarchar(40) |  | PK |
| source_file_pattern | nvarchar(255) | ✓ |  |
| source_column | nvarchar(255) |  |  |
| target_table | nvarchar(100) |  |  |
| target_column | nvarchar(100) |  |  |
| confidence_score | numeric(5,4) | ✓ |  |
| mapping_method | nvarchar(20) | ✓ |  |
| confirmed_ind | nvarchar(1) |  |  |
| confirmed_by | nvarchar(40) | ✓ |  |
| confirmed_date | datetime2 | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_r_source` on `source`; → `dv_r_source` on `source`

### `dv_country`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| country_code | nvarchar(3) |  | PK |
| country_code_a2 | nvarchar(2) | ✓ |  |
| country_name | nvarchar(255) |  |  |
| country_name_local | nvarchar(255) | ✓ |  |
| continent | nvarchar(40) | ✓ |  |
| region | nvarchar(100) | ✓ |  |
| un_m49_code | nvarchar(10) | ✓ |  |
| currency_code | nvarchar(3) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_r_source` on `source`; → `dv_r_source` on `source`; ← `dv_province_state` on `country_code`; ← `dv_province_state` on `country_code`; ← `dv_county` on `country_code`; ← `dv_county` on `country_code`; ← `dv_basin` on `country_code`; ← `dv_basin` on `country_code`; ← `dv_ocs_block` on `country_code`; ← `dv_ocs_block` on `country_code`

### `dv_data_quality`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| dq_id | nvarchar(40) |  | PK |
| entity_type | nvarchar(40) |  |  |
| entity_id | nvarchar(40) |  | FK |
| rule_name | nvarchar(100) |  |  |
| rule_type | nvarchar(40) | ✓ |  |
| result | nvarchar(10) | ✓ |  |
| dq_score | numeric(5,4) | ✓ |  |
| detail | nvarchar(2000) | ✓ |  |
| check_date | datetime2 |  |  |
| batch_id | nvarchar(40) | ✓ | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_load_batch` on `batch_id`; → `dv_load_batch` on `batch_id`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; → `dv_prod_entity` on `entity_id` (inferred)

### `dv_load_batch`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| batch_id | nvarchar(40) |  | PK |
| batch_date | datetime2 |  |  |
| source_file | nvarchar(1000) | ✓ |  |
| source_file_hash | nvarchar(64) | ✓ |  |
| file_type | nvarchar(40) | ✓ |  |
| dialect | nvarchar(20) | ✓ |  |
| target_schema | nvarchar(40) | ✓ |  |
| target_table | nvarchar(100) | ✓ |  |
| rows_staged | int | ✓ |  |
| rows_promoted | int | ✓ |  |
| rows_rejected | int | ✓ |  |
| status | nvarchar(20) | ✓ |  |
| error_msg | nvarchar(2000) | ✓ |  |
| duration_sec | numeric(10,2) | ✓ |  |
| operator_ba_id | nvarchar(40) | ✓ | FK |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_business_associate` on `operator_ba_id`; → `dv_business_associate` on `operator_ba_id`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; ← `dv_data_quality` on `batch_id`; ← `dv_data_quality` on `batch_id`

### `dv_map_area`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| area_id | nvarchar(50) |  | PK |
| label | nvarchar(100) |  |  |
| sources | nvarchar(200) |  |  |
| center_lat | float |  |  |
| center_lon | float |  |  |
| center_zoom | int |  |  |
| enabled_ind | char(1) |  |  |
| queries_allowed | nvarchar(500) |  |  |
| sort_order | int |  |  |
| where_clause | nvarchar(500) | ✓ |  |
| created_date | datetime2 | ✓ |  |

### `dv_seis_line`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| seis_set_id | nvarchar(40) |  | PK |
| line_id | nvarchar(40) |  | PK |
| line_name | nvarchar(255) | ✓ |  |
| line_type | nvarchar(40) | ✓ |  |
| shot_point_start | numeric(15,4) | ✓ |  |
| shot_point_end | numeric(15,4) | ✓ |  |
| cdp_start | int | ✓ |  |
| cdp_end | int | ✓ |  |
| record_length_ms | numeric(10,3) | ✓ |  |
| sample_rate_ms | numeric(10,4) | ✓ |  |
| trace_count | int | ✓ |  |
| file_path | nvarchar(1000) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_seis_set` on `seis_set_id`; → `dv_seis_set` on `seis_set_id`; → `dv_r_source` on `source`; → `dv_r_source` on `source`

### `dv_seis_set`

_1 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| seis_set_id | nvarchar(40) |  | PK |
| seis_set_name | nvarchar(255) |  |  |
| seis_set_type | nvarchar(40) | ✓ |  |
| survey_date | datetime2 | ✓ |  |
| contractor_ba_id | nvarchar(40) | ✓ | FK |
| operator_ba_id | nvarchar(40) | ✓ | FK |
| country | nvarchar(40) | ✓ |  |
| province_state | nvarchar(100) | ✓ |  |
| basin_name | nvarchar(255) | ✓ |  |
| survey_area_km2 | numeric(15,4) | ✓ |  |
| bbox_min_lat | numeric(15,10) | ✓ |  |
| bbox_max_lat | numeric(15,10) | ✓ |  |
| bbox_min_lon | numeric(15,10) | ✓ |  |
| bbox_max_lon | numeric(15,10) | ✓ |  |
| epsg_code | int | ✓ |  |
| file_path | nvarchar(1000) | ✓ |  |
| catalog_id | nvarchar(40) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_business_associate` on `contractor_ba_id`; → `dv_business_associate` on `contractor_ba_id`; → `dv_business_associate` on `operator_ba_id`; → `dv_business_associate` on `operator_ba_id`; → `dv_r_source` on `source`; → `dv_r_source` on `source`; ← `dv_seis_line` on `seis_set_id`; ← `dv_seis_line` on `seis_set_id`; ← `dv_seis_file_catalog` on `seis_set_id`; ← `dv_seis_file_catalog` on `seis_set_id`

### `dv_source`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| source | nvarchar(40) |  | PK |
| source_type | nvarchar(40) | ✓ |  |
| short_name | nvarchar(40) | ✓ |  |
| long_name | nvarchar(255) | ✓ |  |
| description | nvarchar(2000) | ✓ |  |
| url | nvarchar(1000) | ✓ |  |
| active_ind | nvarchar(1) |  |  |
| remark | nvarchar(2000) | ✓ |  |
| row_created_by | nvarchar(40) |  |  |
| row_created_date | datetime2 |  |  |
| row_changed_by | nvarchar(40) | ✓ |  |
| row_changed_date | datetime2 | ✓ |  |
| source_ref | nvarchar(40) | ✓ | FK |

**Relationships:** → `dv_r_source` on `source_ref`; → `dv_r_source` on `source_ref`

### `stg_ai_ext`

_142,929 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| uwi | nvarchar(500) | ✓ | FK |
| LEASE_NO | nvarchar(500) | ✓ |  |
| HORIZ_DIR | nvarchar(500) | ✓ |  |
| LAND_TYPE | nvarchar(500) | ✓ |  |
| COUNTYTXT | nvarchar(500) | ✓ |  |
| SEC | nvarchar(500) | ✓ |  |
| TWP | nvarchar(500) | ✓ |  |
| T_DIR | nvarchar(500) | ✓ |  |
| RGE | nvarchar(500) | ✓ |  |
| R_DIR | nvarchar(500) | ✓ |  |
| QTR1 | nvarchar(500) | ✓ |  |
| QTR2 | nvarchar(500) | ✓ |  |
| FOOT1 | nvarchar(500) | ✓ |  |
| FOOT2 | nvarchar(500) | ✓ |  |
| BSEC | nvarchar(500) | ✓ |  |
| BTWP | nvarchar(500) | ✓ |  |
| BT_DIR | nvarchar(500) | ✓ |  |
| BRGE | nvarchar(500) | ✓ |  |
| BR_DIR | nvarchar(500) | ✓ |  |
| BQTR1 | nvarchar(500) | ✓ |  |
| BQTR2 | nvarchar(500) | ✓ |  |
| BFOOT1 | nvarchar(500) | ✓ |  |
| BFOOT2 | nvarchar(500) | ✓ |  |
| PB | nvarchar(500) | ✓ |  |
| COAL_BED | nvarchar(500) | ✓ |  |
| CAPINO | nvarchar(500) | ✓ |  |
| FORM2MON | nvarchar(500) | ✓ |  |
| FORM2YEAR | nvarchar(500) | ✓ |  |
| UNIT_CODE | nvarchar(500) | ✓ |  |
| RECDATE | nvarchar(500) | ✓ |  |
| MD | nvarchar(500) | ✓ |  |
| PBMD | nvarchar(500) | ✓ |  |
| SYMBOL | nvarchar(500) | ✓ |  |
| source | nvarchar(20) | ✓ |  |

**Relationships:** → `dv_strat_interval` on `uwi` (inferred)
