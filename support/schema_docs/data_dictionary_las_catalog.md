# las_catalog — data dictionary

_Generated 2026-06-14 11:35._

**14 tables**, **0 rows** across 2 subject areas.

## 📁 Documents & Catalog

### `DLIS_FILE`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| DLIS_FILE_ID | nvarchar(40) |  | PK |
| REPOSITORY_ID | nvarchar(40) |  | FK |
| UWI | nvarchar(40) |  |  |
| FILE_NAME | nvarchar(500) |  |  |
| FILE_SIZE_KB | numeric(15,2) | ✓ |  |
| FILE_HASH | nvarchar(64) | ✓ |  |
| LOGICAL_FILE_COUNT | numeric(5,0) | ✓ |  |
| CATALOG_DATE | datetime2 | ✓ |  |
| LAST_SEEN_DATE | datetime2 | ✓ |  |
| ACTIVE_IND | nvarchar(1) |  |  |
| REMARK | nvarchar(2000) | ✓ |  |
| SOURCE | nvarchar(40) |  |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |
| ROW_CHANGED_BY | nvarchar(30) | ✓ |  |
| ROW_CHANGED_DATE | datetime2 | ✓ |  |

**Relationships:** → `WL_REPOSITORY` on `REPOSITORY_ID`; ← `DLIS_LOGICAL_FILE` on `DLIS_FILE_ID`; ← `DLIS_CHANNEL` on `DLIS_FILE_ID` (inferred); ← `DLIS_FRAME` on `DLIS_FILE_ID` (inferred); ← `DLIS_PARAMETER` on `DLIS_FILE_ID` (inferred); ← `LIS_FILE` on `LIS_FILE_ID` (inferred)

### `DLIS_LOGICAL_FILE`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| DLIS_FILE_ID | nvarchar(40) |  | PK |
| LOGICAL_FILE_IDX | numeric(5,0) |  | PK |
| DESCRIPTION | nvarchar(255) | ✓ |  |
| WELL_NAME | nvarchar(255) | ✓ |  |
| WELL_ID | nvarchar(100) | ✓ |  |
| COMPANY | nvarchar(255) | ✓ |  |
| FIELD_NAME | nvarchar(255) | ✓ |  |
| PRODUCER_NAME | nvarchar(255) | ✓ |  |
| PRODUCT | nvarchar(255) | ✓ |  |
| VERSION | nvarchar(100) | ✓ |  |
| FILE_SET_NAME | nvarchar(255) | ✓ |  |
| RUN_NUMBER | nvarchar(40) | ✓ |  |
| CREATION_TIME | datetime2 | ✓ |  |
| ORDER_NUMBER | nvarchar(40) | ✓ |  |
| FRAME_COUNT | numeric(5,0) | ✓ |  |
| CHANNEL_COUNT | numeric(5,0) | ✓ |  |
| SOURCE | nvarchar(40) |  |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |
| ROW_CHANGED_BY | nvarchar(30) | ✓ |  |
| ROW_CHANGED_DATE | datetime2 | ✓ |  |

**Relationships:** → `DLIS_FILE` on `DLIS_FILE_ID`; ← `DLIS_FRAME` on `DLIS_FILE_ID`; ← `DLIS_FRAME` on `LOGICAL_FILE_IDX`; ← `DLIS_PARAMETER` on `DLIS_FILE_ID`; ← `DLIS_PARAMETER` on `LOGICAL_FILE_IDX`

### `LAS_FILE`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| LAS_FILE_ID | nvarchar(40) |  | PK |
| REPOSITORY_ID | nvarchar(40) |  | FK |
| UWI | nvarchar(40) |  |  |
| WELL_NAME | nvarchar(255) | ✓ |  |
| FILE_NAME | nvarchar(500) |  |  |
| FILE_SIZE_KB | numeric(15,2) | ✓ |  |
| LAS_VERSION | nvarchar(10) | ✓ |  |
| OPERATOR | nvarchar(255) | ✓ |  |
| FIELD | nvarchar(255) | ✓ |  |
| COUNTRY | nvarchar(255) | ✓ |  |
| STATE_PROVINCE | nvarchar(255) | ✓ |  |
| COUNTY | nvarchar(255) | ✓ |  |
| TOP_DEPTH | numeric(15,5) | ✓ |  |
| BASE_DEPTH | numeric(15,5) | ✓ |  |
| DEPTH_STEP | numeric(15,5) | ✓ |  |
| DEPTH_UOM | nvarchar(10) | ✓ |  |
| LOG_DATE | nvarchar(50) | ✓ |  |
| SERVICE_COMPANY | nvarchar(255) | ✓ |  |
| CURVE_COUNT | numeric(10,0) | ✓ |  |
| SAMPLE_COUNT | numeric(15,0) | ✓ |  |
| FILE_HASH | nvarchar(64) | ✓ |  |
| CATALOG_DATE | datetime2 | ✓ |  |
| LAST_SEEN_DATE | datetime2 | ✓ |  |
| ACTIVE_IND | nvarchar(1) |  |  |
| REMARK | nvarchar(2000) | ✓ |  |
| SOURCE | nvarchar(40) |  |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |
| ROW_CHANGED_BY | nvarchar(30) | ✓ |  |
| ROW_CHANGED_DATE | datetime2 | ✓ |  |

**Relationships:** → `WL_REPOSITORY` on `REPOSITORY_ID`; ← `LAS_FILE_CURVE` on `LAS_FILE_ID`; ← `LAS_FILE_PARAMETER` on `LAS_FILE_ID`

### `LAS_FILE_CURVE`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| LAS_FILE_ID | nvarchar(40) |  | PK |
| CURVE_ID | nvarchar(40) |  | PK |
| CURVE_UNIT | nvarchar(40) | ✓ |  |
| CURVE_DESCRIPTION | nvarchar(255) | ✓ |  |
| CURVE_TYPE | nvarchar(40) | ✓ |  |
| API_CODE | nvarchar(40) | ✓ |  |
| SOURCE | nvarchar(40) |  |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |
| ROW_CHANGED_BY | nvarchar(30) | ✓ |  |
| ROW_CHANGED_DATE | datetime2 | ✓ |  |

**Relationships:** → `LAS_FILE` on `LAS_FILE_ID`

### `LAS_FILE_PARAMETER`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| LAS_FILE_ID | nvarchar(40) |  | PK |
| PARAMETER_NAME | nvarchar(40) |  | PK |
| PARAMETER_VALUE | nvarchar(500) | ✓ |  |
| PARAMETER_UNIT | nvarchar(40) | ✓ |  |
| SECTION | nvarchar(10) | ✓ |  |
| SOURCE | nvarchar(40) |  |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |
| ROW_CHANGED_BY | nvarchar(30) | ✓ |  |
| ROW_CHANGED_DATE | datetime2 | ✓ |  |

**Relationships:** → `LAS_FILE` on `LAS_FILE_ID`

### `LIS_FILE`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| LIS_FILE_ID | nvarchar(40) |  | PK |
| REPOSITORY_ID | nvarchar(40) |  | FK |
| UWI | nvarchar(40) |  |  |
| FILE_NAME | nvarchar(500) |  |  |
| FILE_SIZE_KB | numeric(15,2) | ✓ |  |
| FILE_HASH | nvarchar(64) | ✓ |  |
| WELL_NAME | nvarchar(255) | ✓ |  |
| COMPANY | nvarchar(255) | ✓ |  |
| FIELD_NAME | nvarchar(255) | ✓ |  |
| LOG_DATE | nvarchar(50) | ✓ |  |
| SERVICE_COMPANY | nvarchar(255) | ✓ |  |
| TOP_DEPTH | numeric(15,5) | ✓ |  |
| BASE_DEPTH | numeric(15,5) | ✓ |  |
| DEPTH_UOM | nvarchar(10) | ✓ |  |
| CHANNEL_COUNT | numeric(5,0) | ✓ |  |
| SAMPLE_COUNT | numeric(15,0) | ✓ |  |
| CATALOG_DATE | datetime2 | ✓ |  |
| LAST_SEEN_DATE | datetime2 | ✓ |  |
| ACTIVE_IND | nvarchar(1) |  |  |
| REMARK | nvarchar(2000) | ✓ |  |
| SOURCE | nvarchar(40) |  |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |
| ROW_CHANGED_BY | nvarchar(30) | ✓ |  |
| ROW_CHANGED_DATE | datetime2 | ✓ |  |

**Relationships:** → `WL_REPOSITORY` on `REPOSITORY_ID`; → `DLIS_FILE` on `LIS_FILE_ID` (inferred); ← `LIS_CHANNEL` on `LIS_FILE_ID`

### `SEIS_FILE_CATALOG`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| SEIS_FILE_ID | nvarchar(40) |  | PK |
| REPOSITORY_ID | nvarchar(40) | ✓ | FK |
| FILE_FORMAT | nvarchar(10) |  |  |
| FILE_NAME | nvarchar(500) |  |  |
| FILE_SIZE_KB | numeric(15,2) | ✓ |  |
| FILE_HASH | nvarchar(64) | ✓ |  |
| SEIS_SET_ID | nvarchar(40) | ✓ |  |
| SEIS_LINE_ID | nvarchar(40) | ✓ |  |
| SEIS_SET_SUBID | nvarchar(40) | ✓ |  |
| SURVEY_NAME | nvarchar(255) | ✓ |  |
| LINE_NAME | nvarchar(255) | ✓ |  |
| VESSEL_NAME | nvarchar(255) | ✓ |  |
| CLIENT_NAME | nvarchar(255) | ✓ |  |
| DIMENSIONALITY | nvarchar(10) | ✓ |  |
| SAMPLE_INTERVAL_US | numeric(10,2) | ✓ |  |
| SAMPLE_COUNT | numeric(10,0) | ✓ |  |
| TRACE_COUNT | numeric(15,0) | ✓ |  |
| DATA_FORMAT | nvarchar(40) | ✓ |  |
| SEGY_REVISION | nvarchar(10) | ✓ |  |
| RECORD_COUNT | numeric(10,0) | ✓ |  |
| SHOT_COUNT | numeric(10,0) | ✓ |  |
| FIRST_SHOT_POINT | numeric(10,2) | ✓ |  |
| LAST_SHOT_POINT | numeric(10,2) | ✓ |  |
| NAV_SYSTEM | nvarchar(40) | ✓ |  |
| ACQ_DATE_START | nvarchar(30) | ✓ |  |
| ACQ_DATE_END | nvarchar(30) | ✓ |  |
| MIN_LAT | numeric(12,7) | ✓ |  |
| MAX_LAT | numeric(12,7) | ✓ |  |
| MIN_LON | numeric(12,7) | ✓ |  |
| MAX_LON | numeric(12,7) | ✓ |  |
| MIN_X | numeric(18,3) | ✓ |  |
| MAX_X | numeric(18,3) | ✓ |  |
| MIN_Y | numeric(18,3) | ✓ |  |
| MAX_Y | numeric(18,3) | ✓ |  |
| COORD_SYSTEM | nvarchar(255) | ✓ |  |
| MIN_DEPTH_MS | numeric(12,3) | ✓ |  |
| MAX_DEPTH_MS | numeric(12,3) | ✓ |  |
| MIN_INLINE | numeric(10,0) | ✓ |  |
| MAX_INLINE | numeric(10,0) | ✓ |  |
| MIN_CROSSLINE | numeric(10,0) | ✓ |  |
| MAX_CROSSLINE | numeric(10,0) | ✓ |  |
| CATALOG_DATE | datetime2 | ✓ |  |
| LAST_SEEN_DATE | datetime2 | ✓ |  |
| ACTIVE_IND | nvarchar(1) |  |  |
| REMARK | nvarchar(2000) | ✓ |  |
| SOURCE | nvarchar(40) |  |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |
| ROW_CHANGED_BY | nvarchar(30) | ✓ |  |
| ROW_CHANGED_DATE | datetime2 | ✓ |  |
| DEPTH_UOM | nvarchar(10) | ✓ |  |

**Relationships:** → `WL_REPOSITORY` on `REPOSITORY_ID`; ← `SEIS_FILE_HEADER` on `SEIS_FILE_ID`

### `SEIS_FILE_HEADER`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| SEIS_FILE_ID | nvarchar(40) |  | PK |
| LINE_NO | numeric(5,0) |  | PK |
| HEADER_TEXT | nvarchar(80) | ✓ |  |
| SOURCE | nvarchar(40) |  |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |

**Relationships:** → `SEIS_FILE_CATALOG` on `SEIS_FILE_ID`

### `WL_FILE_UWI_MAP`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| MAP_ID | nvarchar(40) |  | PK |
| FILE_PATH | nvarchar(500) |  |  |
| FILE_NAME | nvarchar(255) |  |  |
| FILE_FORMAT | nvarchar(10) |  |  |
| REPOSITORY_ID | nvarchar(40) | ✓ | FK |
| UWI | nvarchar(40) | ✓ |  |
| HEADER_WELL_ID | nvarchar(255) | ✓ |  |
| MATCH_METHOD | nvarchar(20) | ✓ |  |
| MATCH_SCORE | numeric(5,1) | ✓ |  |
| MATCH_WELL_NAME | nvarchar(255) | ✓ |  |
| STATUS | nvarchar(20) |  |  |
| FILE_SIZE_KB | numeric(15,2) | ✓ |  |
| REMARK | nvarchar(2000) | ✓ |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |
| ROW_CHANGED_BY | nvarchar(30) | ✓ |  |
| ROW_CHANGED_DATE | datetime2 | ✓ |  |

**Relationships:** → `WL_REPOSITORY` on `REPOSITORY_ID`

## 📦 Other

### `DLIS_CHANNEL`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| DLIS_FILE_ID | nvarchar(40) |  | PK |
| LOGICAL_FILE_IDX | numeric(5,0) |  | PK |
| FRAME_NAME | nvarchar(100) |  | PK |
| CHANNEL_NAME | nvarchar(40) |  | PK |
| LONG_NAME | nvarchar(255) | ✓ |  |
| UNITS | nvarchar(40) | ✓ |  |
| DIMENSION | nvarchar(40) | ✓ |  |
| IS_INDEX | nvarchar(1) |  |  |
| SOURCE | nvarchar(40) |  |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |
| ROW_CHANGED_BY | nvarchar(30) | ✓ |  |
| ROW_CHANGED_DATE | datetime2 | ✓ |  |

**Relationships:** → `DLIS_FRAME` on `DLIS_FILE_ID`; → `DLIS_FRAME` on `LOGICAL_FILE_IDX`; → `DLIS_FRAME` on `FRAME_NAME`; → `DLIS_FILE` on `DLIS_FILE_ID` (inferred)

### `DLIS_FRAME`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| DLIS_FILE_ID | nvarchar(40) |  | PK |
| LOGICAL_FILE_IDX | numeric(5,0) |  | PK |
| FRAME_NAME | nvarchar(100) |  | PK |
| INDEX_CHANNEL | nvarchar(40) | ✓ |  |
| INDEX_TYPE | nvarchar(40) | ✓ |  |
| TOP_DEPTH | numeric(15,5) | ✓ |  |
| BASE_DEPTH | numeric(15,5) | ✓ |  |
| DEPTH_UOM | nvarchar(20) | ✓ |  |
| DEPTH_UOM_STD | nvarchar(5) | ✓ |  |
| TOP_DEPTH_M | numeric(15,3) | ✓ |  |
| BASE_DEPTH_M | numeric(15,3) | ✓ |  |
| SPACING | numeric(15,5) | ✓ |  |
| CHANNEL_COUNT | numeric(5,0) | ✓ |  |
| SAMPLE_COUNT | numeric(15,0) | ✓ |  |
| SOURCE | nvarchar(40) |  |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |
| ROW_CHANGED_BY | nvarchar(30) | ✓ |  |
| ROW_CHANGED_DATE | datetime2 | ✓ |  |

**Relationships:** → `DLIS_LOGICAL_FILE` on `DLIS_FILE_ID`; → `DLIS_LOGICAL_FILE` on `LOGICAL_FILE_IDX`; → `DLIS_FILE` on `DLIS_FILE_ID` (inferred); ← `DLIS_CHANNEL` on `DLIS_FILE_ID`; ← `DLIS_CHANNEL` on `LOGICAL_FILE_IDX`; ← `DLIS_CHANNEL` on `FRAME_NAME`

### `DLIS_PARAMETER`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| DLIS_FILE_ID | nvarchar(40) |  | PK |
| LOGICAL_FILE_IDX | numeric(5,0) |  | PK |
| PARAMETER_NAME | nvarchar(40) |  | PK |
| LONG_NAME | nvarchar(255) | ✓ |  |
| VALUE | nvarchar(500) | ✓ |  |
| UNITS | nvarchar(40) | ✓ |  |
| SOURCE | nvarchar(40) |  |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |
| ROW_CHANGED_BY | nvarchar(30) | ✓ |  |
| ROW_CHANGED_DATE | datetime2 | ✓ |  |

**Relationships:** → `DLIS_LOGICAL_FILE` on `DLIS_FILE_ID`; → `DLIS_LOGICAL_FILE` on `LOGICAL_FILE_IDX`; → `DLIS_FILE` on `DLIS_FILE_ID` (inferred)

### `LIS_CHANNEL`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| LIS_FILE_ID | nvarchar(40) |  | PK |
| CHANNEL_NAME | nvarchar(40) |  | PK |
| UNITS | nvarchar(40) | ✓ |  |
| DESCRIPTION | nvarchar(255) | ✓ |  |
| IS_INDEX | nvarchar(1) |  |  |
| SOURCE | nvarchar(40) |  |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |
| ROW_CHANGED_BY | nvarchar(30) | ✓ |  |
| ROW_CHANGED_DATE | datetime2 | ✓ |  |

**Relationships:** → `LIS_FILE` on `LIS_FILE_ID`

### `WL_REPOSITORY`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| REPOSITORY_ID | nvarchar(40) |  | PK |
| REPOSITORY_NAME | nvarchar(200) |  |  |
| REPOSITORY_TYPE | nvarchar(40) |  |  |
| BASE_PATH | nvarchar(500) |  |  |
| ACTIVE_IND | nvarchar(1) |  |  |
| REMARK | nvarchar(2000) | ✓ |  |
| SOURCE | nvarchar(40) |  |  |
| ROW_CREATED_BY | nvarchar(30) | ✓ |  |
| ROW_CREATED_DATE | datetime2 | ✓ |  |
| ROW_CHANGED_BY | nvarchar(30) | ✓ |  |
| ROW_CHANGED_DATE | datetime2 | ✓ |  |

**Relationships:** ← `SEIS_FILE_CATALOG` on `REPOSITORY_ID`; ← `WL_FILE_UWI_MAP` on `REPOSITORY_ID`; ← `DLIS_FILE` on `REPOSITORY_ID`; ← `LAS_FILE` on `REPOSITORY_ID`; ← `LIS_FILE` on `REPOSITORY_ID`
