# las_catalog — schema map

_Generated 2026-06-14 11:35 from the live catalog._

## Subject areas

```mermaid
flowchart LR
    DOCUMENTS["📁 Documents & Catalog<br/>9 tables · 0 rows"]
    OTHER["📦 Other<br/>5 tables · 0 rows"]
    style DOCUMENTS fill:#9AA0A622,stroke:#9AA0A6,color:#e8eef2
    style OTHER fill:#88878022,stroke:#888780,color:#e8eef2
```

## 📁 Documents & Catalog

File inventory, catalog scoring, scout tickets, and the LAS / PDF source assets.

*9 tables.*

```mermaid
erDiagram
    DLIS_FILE {
        nvarchar DLIS_FILE_ID PK
        nvarchar REPOSITORY_ID FK
        nvarchar UWI
    }
    DLIS_LOGICAL_FILE {
        nvarchar DLIS_FILE_ID PK
        numeric LOGICAL_FILE_IDX PK
        nvarchar WELL_ID
    }
    LAS_FILE {
        nvarchar LAS_FILE_ID PK
        nvarchar REPOSITORY_ID FK
        nvarchar UWI
    }
    LAS_FILE_CURVE {
        nvarchar LAS_FILE_ID PK
        nvarchar CURVE_ID PK
    }
    LAS_FILE_PARAMETER {
        nvarchar LAS_FILE_ID PK
        nvarchar PARAMETER_NAME PK
    }
    LIS_FILE {
        nvarchar LIS_FILE_ID PK
        nvarchar REPOSITORY_ID FK
        nvarchar UWI
    }
    SEIS_FILE_CATALOG {
        nvarchar SEIS_FILE_ID PK
        nvarchar REPOSITORY_ID FK
        nvarchar SEIS_SET_ID
        nvarchar SEIS_LINE_ID
    }
    SEIS_FILE_HEADER {
        nvarchar SEIS_FILE_ID PK
        numeric LINE_NO PK
    }
    WL_FILE_UWI_MAP {
        nvarchar MAP_ID PK
        nvarchar REPOSITORY_ID FK
        nvarchar UWI
        nvarchar HEADER_WELL_ID
    }
    WL_REPOSITORY {
        nvarchar REPOSITORY_ID PK
    }
    WL_REPOSITORY ||--o{ SEIS_FILE_CATALOG : "REPOSITORY_ID"
    SEIS_FILE_CATALOG ||--o{ SEIS_FILE_HEADER : "SEIS_FILE_ID"
    WL_REPOSITORY ||--o{ WL_FILE_UWI_MAP : "REPOSITORY_ID"
    WL_REPOSITORY ||--o{ DLIS_FILE : "REPOSITORY_ID"
    DLIS_FILE ||--o{ DLIS_LOGICAL_FILE : "DLIS_FILE_ID"
    WL_REPOSITORY ||--o{ LAS_FILE : "REPOSITORY_ID"
    LAS_FILE ||--o{ LAS_FILE_CURVE : "LAS_FILE_ID"
    LAS_FILE ||--o{ LAS_FILE_PARAMETER : "LAS_FILE_ID"
    WL_REPOSITORY ||--o{ LIS_FILE : "REPOSITORY_ID"
    DLIS_FILE ||--o{ LIS_FILE : "LIS_FILE_ID (inf)"
```

## 📦 Other

Tables not yet classified into a subject area — adjust the rules or supply an overrides file to reclassify.

*5 tables.*

```mermaid
erDiagram
    DLIS_CHANNEL {
        nvarchar DLIS_FILE_ID PK
        numeric LOGICAL_FILE_IDX PK
        nvarchar FRAME_NAME PK
        nvarchar CHANNEL_NAME PK
    }
    DLIS_FRAME {
        nvarchar DLIS_FILE_ID PK
        numeric LOGICAL_FILE_IDX PK
        nvarchar FRAME_NAME PK
    }
    DLIS_PARAMETER {
        nvarchar DLIS_FILE_ID PK
        numeric LOGICAL_FILE_IDX PK
        nvarchar PARAMETER_NAME PK
    }
    LIS_CHANNEL {
        nvarchar LIS_FILE_ID PK
        nvarchar CHANNEL_NAME PK
    }
    WL_REPOSITORY {
        nvarchar REPOSITORY_ID PK
    }
    DLIS_FILE {
        nvarchar DLIS_FILE_ID PK
    }
    DLIS_LOGICAL_FILE {
        nvarchar DLIS_FILE_ID PK
        numeric LOGICAL_FILE_IDX PK
    }
    LIS_FILE {
        nvarchar LIS_FILE_ID PK
    }
    DLIS_FRAME ||--o{ DLIS_CHANNEL : "DLIS_FILE_ID"
    DLIS_FRAME ||--o{ DLIS_CHANNEL : "LOGICAL_FILE_IDX"
    DLIS_FRAME ||--o{ DLIS_CHANNEL : "FRAME_NAME"
    DLIS_LOGICAL_FILE ||--o{ DLIS_FRAME : "DLIS_FILE_ID"
    DLIS_LOGICAL_FILE ||--o{ DLIS_FRAME : "LOGICAL_FILE_IDX"
    DLIS_LOGICAL_FILE ||--o{ DLIS_PARAMETER : "DLIS_FILE_ID"
    DLIS_LOGICAL_FILE ||--o{ DLIS_PARAMETER : "LOGICAL_FILE_IDX"
    LIS_FILE ||--o{ LIS_CHANNEL : "LIS_FILE_ID"
    DLIS_FILE ||--o{ DLIS_CHANNEL : "DLIS_FILE_ID (inf)"
    DLIS_FILE ||--o{ DLIS_FRAME : "DLIS_FILE_ID (inf)"
    DLIS_FILE ||--o{ DLIS_PARAMETER : "DLIS_FILE_ID (inf)"
```
