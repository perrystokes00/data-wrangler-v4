# file_catalog — data dictionary

_Generated 2026-06-14 11:35._

**12 tables**, **1,920 rows** across 2 subject areas.

## 📁 Documents & Catalog

### `CATALOG_SETTING`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| SETTING_KEY | nvarchar(100) |  | PK |
| SETTING_VALUE | nvarchar(900) | ✓ |  |
| DESCRIPTION | nvarchar(500) | ✓ |  |
| UPDATED_DATE | datetime2 | ✓ |  |
| UPDATED_BY | nvarchar(100) | ✓ |  |

### `FILE_CURVE`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| FILE_CURVE_ID | nvarchar(40) |  | PK |
| FILE_HEADER_ID | nvarchar(40) |  | FK |
| MNEMONIC | nvarchar(40) |  |  |
| UNIT | nvarchar(40) | ✓ |  |
| DESCRIPTION | nvarchar(200) | ✓ |  |
| SORT_ORDER | int | ✓ |  |

**Relationships:** → `FILE_HEADER` on `FILE_HEADER_ID` (inferred)

### `FILE_HEADER`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| FILE_HEADER_ID | nvarchar(40) |  | PK |
| INVENTORY_ID | nvarchar(64) | ✓ | FK |
| FILE_TYPE | nvarchar(10) |  |  |
| FILE_PATH | nvarchar(900) |  |  |
| FILE_NAME | nvarchar(260) |  |  |
| FILE_SIZE_KB | decimal(15,2) | ✓ |  |
| MATCHED_UWI | nvarchar(40) | ✓ |  |
| MATCH_METHOD | nvarchar(20) | ✓ |  |
| MATCH_SCORE | decimal(5,2) | ✓ |  |
| WELL_NAME | nvarchar(200) | ✓ |  |
| HEADER_TEXT | nvarchar(MAX) | ✓ |  |
| CATALOGED_BY | nvarchar(64) | ✓ |  |
| CATALOG_DATE | datetime2 | ✓ |  |
| ACTIVE_IND | nvarchar(1) |  |  |
| SOURCE | nvarchar(100) | ✓ |  |

**Relationships:** → `GLOBAL_FILE_CATALOG` on `INVENTORY_ID` (inferred); ← `FILE_CURVE` on `FILE_HEADER_ID` (inferred)

### `FILE_SEIS_HEADER`

_791 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| SEIS_HEADER_ID | nvarchar(40) |  | PK |
| INVENTORY_ID | nvarchar(40) |  | FK |
| SURVEY_NAME | nvarchar(255) | ✓ |  |
| LINE_NAME | nvarchar(255) | ✓ |  |
| SEIS_SET_TYPE | nvarchar(40) | ✓ |  |
| SURVEY_DATE | nvarchar(20) | ✓ |  |
| CONTRACTOR | nvarchar(255) | ✓ |  |
| BBOX_MIN_LAT | decimal(11,7) | ✓ |  |
| BBOX_MAX_LAT | decimal(11,7) | ✓ |  |
| BBOX_MIN_LON | decimal(11,7) | ✓ |  |
| BBOX_MAX_LON | decimal(11,7) | ✓ |  |
| EPSG_CODE | int | ✓ |  |
| SAMPLE_INTERVAL | decimal(10,3) | ✓ |  |
| TRACE_COUNT | int | ✓ |  |
| SHOT_FIRST | nvarchar(20) | ✓ |  |
| SHOT_LAST | nvarchar(20) | ✓ |  |
| EXTRACTED_DATE | datetime2 |  |  |
| EXTRACTED_BY | nvarchar(64) |  |  |
| IL_MIN | int | ✓ |  |
| IL_MAX | int | ✓ |  |
| XL_MIN | int | ✓ |  |
| XL_MAX | int | ✓ |  |
| SURVEY_OUTLINE | nvarchar(MAX) | ✓ |  |

**Relationships:** → `GLOBAL_FILE_CATALOG` on `INVENTORY_ID` (inferred)

### `FILE_WELL_HEADER`

_484 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| WELL_HEADER_ID | nvarchar(40) |  | PK |
| INVENTORY_ID | nvarchar(40) |  | FK |
| UWI | nvarchar(40) | ✓ |  |
| WELL_NAME | nvarchar(255) | ✓ |  |
| OPERATOR | nvarchar(255) | ✓ |  |
| WELL_FIELD | nvarchar(100) | ✓ |  |
| STATE | nvarchar(50) | ✓ |  |
| COUNTY | nvarchar(100) | ✓ |  |
| LATITUDE | decimal(11,7) | ✓ |  |
| LONGITUDE | decimal(11,7) | ✓ |  |
| COORD_PRECISION | tinyint | ✓ |  |
| TOTAL_DEPTH | decimal(15,5) | ✓ |  |
| SPUD_DATE | nvarchar(20) | ✓ |  |
| RIG_RELEASE | nvarchar(20) | ✓ |  |
| REPORT_TYPE | nvarchar(50) | ✓ |  |
| SURVEY_TYPE | nvarchar(50) | ✓ |  |
| CONTRACTOR | nvarchar(255) | ✓ |  |
| CONFIDENCE | decimal(5,2) | ✓ |  |
| EXTRACTED_DATE | datetime2 |  |  |
| EXTRACTED_BY | nvarchar(64) |  |  |

**Relationships:** → `GLOBAL_FILE_CATALOG` on `INVENTORY_ID` (inferred)

### `GLOBAL_FILE_CATALOG`

_645 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| INVENTORY_ID | nvarchar(40) |  | PK |
| FILE_PATH | nvarchar(1000) |  |  |
| FILE_NAME | nvarchar(500) |  |  |
| FILE_EXT | nvarchar(20) | ✓ |  |
| FILE_SIZE_KB | numeric(15,2) | ✓ |  |
| FILE_HASH | nvarchar(64) | ✓ |  |
| FILE_HASH_FULL | nvarchar(64) | ✓ |  |
| DUPLICATE_GROUP | nvarchar(64) | ✓ |  |
| MODIFIED_DATE | datetime2 | ✓ |  |
| SCAN_DATE | datetime2 |  |  |
| ROOT_PATH | nvarchar(500) | ✓ |  |
| FILE_TYPE_GROUP | nvarchar(50) | ✓ |  |
| DOC_TYPE | nvarchar(100) | ✓ |  |
| REPORT_TYPE | nvarchar(100) | ✓ |  |
| HEADER_EXTRACTED | nvarchar(1) | ✓ |  |
| FLAG_DELETE | nvarchar(1) | ✓ |  |
| ROW_CREATED_DATE | datetime2 |  |  |
| ROW_CHANGED_DATE | datetime2 |  |  |
| EXTRACTION_STATUS | nvarchar(20) | ✓ |  |
| CATALOG_SCORE | int | ✓ |  |
| CATALOG_READINESS | nvarchar(20) | ✓ |  |
| CATALOG_ISSUES | nvarchar(2000) | ✓ |  |
| MATCHED_UWI | nvarchar(40) | ✓ |  |
| MATCH_METHOD | nvarchar(40) | ✓ |  |
| CATALOG_STATUS | nvarchar(20) | ✓ |  |
| CATALOG_TABLE | nvarchar(100) | ✓ |  |

**Relationships:** ← `FILE_HEADER` on `INVENTORY_ID` (inferred); ← `FILE_SEIS_HEADER` on `INVENTORY_ID` (inferred); ← `FILE_WELL_HEADER` on `INVENTORY_ID` (inferred); ← `INVENTORY_GROUP_FILE` on `INVENTORY_ID` (inferred)

### `INVENTORY_ASSIGNMENT`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| ASSIGNMENT_ID | nvarchar(64) |  | PK |
| GROUP_ID | nvarchar(64) |  | FK |
| ASSIGNED_TO | nvarchar(64) |  |  |
| ASSIGNED_BY | nvarchar(64) |  |  |
| ASSIGNED_DATE | datetime2 |  |  |
| DUE_DATE | date | ✓ |  |
| COMPLETED_DATE | datetime2 | ✓ |  |
| STATUS | nvarchar(20) |  |  |
| NOTES | nvarchar(1000) | ✓ |  |
| FILE_COUNT | int |  |  |

**Relationships:** → `ASSIGNMENT_EXTENSION` on `ASSIGNMENT_ID` (inferred); → `INVENTORY_GROUP` on `GROUP_ID` (inferred); ← `ASSIGNMENT_EXTENSION` on `ASSIGNMENT_ID` (inferred); ← `INVENTORY_GROUP_FILE` on `ASSIGNMENT_ID` (inferred)

### `INVENTORY_GROUP`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| GROUP_ID | nvarchar(64) |  | PK |
| GROUP_NAME | nvarchar(200) |  |  |
| DESCRIPTION | nvarchar(500) | ✓ |  |
| FILE_TYPE | nvarchar(20) | ✓ |  |
| ROOT_PATH | nvarchar(500) | ✓ |  |
| TOTAL_FILES | int |  |  |
| STATUS | nvarchar(20) |  |  |
| CREATED_BY | nvarchar(64) | ✓ |  |
| CREATED_DATE | datetime2 |  |  |

**Relationships:** → `INVENTORY_GROUP_FILE` on `GROUP_ID` (inferred); ← `INVENTORY_ASSIGNMENT` on `GROUP_ID` (inferred); ← `INVENTORY_GROUP_FILE` on `GROUP_ID` (inferred)

### `INVENTORY_GROUP_FILE`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| GROUP_FILE_ID | nvarchar(64) |  | PK |
| GROUP_ID | nvarchar(64) |  | FK |
| ASSIGNMENT_ID | nvarchar(64) |  | FK |
| INVENTORY_ID | nvarchar(64) |  | FK |
| ADDED_BY | nvarchar(64) | ✓ |  |
| ADDED_DATE | datetime2 |  |  |
| CATALOGED_IND | nvarchar(1) |  |  |
| CATALOGED_DATE | datetime2 | ✓ |  |
| SKIPPED_IND | nvarchar(1) |  |  |
| SKIP_REASON | nvarchar(500) | ✓ |  |

**Relationships:** → `INVENTORY_GROUP` on `GROUP_ID` (inferred); → `INVENTORY_ASSIGNMENT` on `ASSIGNMENT_ID` (inferred); → `GLOBAL_FILE_CATALOG` on `INVENTORY_ID` (inferred); ← `INVENTORY_GROUP` on `GROUP_ID` (inferred)

### `INVENTORY_USER`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| USER_ID | nvarchar(64) |  | PK |
| FULL_NAME | nvarchar(200) |  |  |
| EMAIL | nvarchar(200) |  |  |
| PASSWORD_HASH | nvarchar(64) |  |  |
| ROLE | nvarchar(20) |  |  |
| ACTIVE_IND | nvarchar(1) |  |  |
| LAST_LOGIN | datetime2 | ✓ |  |
| CREATED_DATE | datetime2 |  |  |
| CREATED_BY | nvarchar(64) | ✓ |  |

**Relationships:** ← `AUDIT_LOG` on `USER_ID` (inferred)

## 📦 Other

### `ASSIGNMENT_EXTENSION`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| EXTENSION_ID | nvarchar(64) |  | PK |
| ASSIGNMENT_ID | nvarchar(64) |  | FK |
| ORIGINAL_DUE_DATE | date |  |  |
| NEW_DUE_DATE | date |  |  |
| EXTENDED_BY | nvarchar(64) |  |  |
| EXTENDED_DATE | datetime2 |  |  |
| REASON | nvarchar(500) |  |  |

**Relationships:** → `INVENTORY_ASSIGNMENT` on `ASSIGNMENT_ID` (inferred); ← `INVENTORY_ASSIGNMENT` on `ASSIGNMENT_ID` (inferred)

### `AUDIT_LOG`

_0 rows._

| Column | Type | Null | Key |
|--------|------|:----:|:---:|
| AUDIT_ID | nvarchar(40) |  | PK |
| EVENT_TIME | datetime2 |  |  |
| EVENT_TYPE | nvarchar(50) |  |  |
| USER_ID | nvarchar(40) | ✓ | FK |
| USER_NAME | nvarchar(255) | ✓ |  |
| TARGET_ID | nvarchar(40) | ✓ |  |
| TARGET_TYPE | nvarchar(50) | ✓ |  |
| TARGET_NAME | nvarchar(500) | ✓ |  |
| OLD_VALUE | nvarchar(MAX) | ✓ |  |
| NEW_VALUE | nvarchar(MAX) | ✓ |  |
| NOTES | nvarchar(1000) | ✓ |  |
| SESSION_ID | nvarchar(40) | ✓ |  |

**Relationships:** → `INVENTORY_USER` on `USER_ID` (inferred)
