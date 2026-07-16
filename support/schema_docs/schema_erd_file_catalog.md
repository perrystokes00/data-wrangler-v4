# file_catalog — schema map

_Generated 2026-06-14 11:35 from the live catalog._

## Subject areas

```mermaid
flowchart LR
    DOCUMENTS["📁 Documents & Catalog<br/>10 tables · 1,920 rows"]
    OTHER["📦 Other<br/>2 tables · 0 rows"]
    style DOCUMENTS fill:#9AA0A622,stroke:#9AA0A6,color:#e8eef2
    style OTHER fill:#88878022,stroke:#888780,color:#e8eef2
```

## 📁 Documents & Catalog

File inventory, catalog scoring, scout tickets, and the LAS / PDF source assets.

*10 tables.*

```mermaid
erDiagram
    CATALOG_SETTING {
        nvarchar SETTING_KEY PK
    }
    FILE_CURVE {
        nvarchar FILE_CURVE_ID PK
        nvarchar FILE_HEADER_ID FK
    }
    FILE_HEADER {
        nvarchar FILE_HEADER_ID PK
        nvarchar INVENTORY_ID FK
    }
    FILE_SEIS_HEADER {
        nvarchar SEIS_HEADER_ID PK
        nvarchar INVENTORY_ID FK
    }
    FILE_WELL_HEADER {
        nvarchar WELL_HEADER_ID PK
        nvarchar INVENTORY_ID FK
        nvarchar UWI
    }
    GLOBAL_FILE_CATALOG {
        nvarchar INVENTORY_ID PK
    }
    INVENTORY_ASSIGNMENT {
        nvarchar ASSIGNMENT_ID PK
        nvarchar GROUP_ID FK
    }
    INVENTORY_GROUP {
        nvarchar GROUP_ID PK
    }
    INVENTORY_GROUP_FILE {
        nvarchar GROUP_FILE_ID PK
        nvarchar GROUP_ID FK
        nvarchar ASSIGNMENT_ID FK
        nvarchar INVENTORY_ID FK
    }
    INVENTORY_USER {
        nvarchar USER_ID PK
    }
    ASSIGNMENT_EXTENSION {
        nvarchar EXTENSION_ID PK
    }
    FILE_HEADER ||--o{ FILE_CURVE : "FILE_HEADER_ID (inf)"
    GLOBAL_FILE_CATALOG ||--o{ FILE_HEADER : "INVENTORY_ID (inf)"
    GLOBAL_FILE_CATALOG ||--o{ FILE_SEIS_HEADER : "INVENTORY_ID (inf)"
    GLOBAL_FILE_CATALOG ||--o{ FILE_WELL_HEADER : "INVENTORY_ID (inf)"
    ASSIGNMENT_EXTENSION ||--o{ INVENTORY_ASSIGNMENT : "ASSIGNMENT_ID (inf)"
    INVENTORY_GROUP ||--o{ INVENTORY_ASSIGNMENT : "GROUP_ID (inf)"
    INVENTORY_GROUP_FILE ||--o{ INVENTORY_GROUP : "GROUP_ID (inf)"
    INVENTORY_GROUP ||--o{ INVENTORY_GROUP_FILE : "GROUP_ID (inf)"
    INVENTORY_ASSIGNMENT ||--o{ INVENTORY_GROUP_FILE : "ASSIGNMENT_ID (inf)"
    GLOBAL_FILE_CATALOG ||--o{ INVENTORY_GROUP_FILE : "INVENTORY_ID (inf)"
```

## 📦 Other

Tables not yet classified into a subject area — adjust the rules or supply an overrides file to reclassify.

*2 tables.*

```mermaid
erDiagram
    ASSIGNMENT_EXTENSION {
        nvarchar EXTENSION_ID PK
        nvarchar ASSIGNMENT_ID FK
    }
    AUDIT_LOG {
        nvarchar AUDIT_ID PK
        nvarchar USER_ID FK
        nvarchar TARGET_ID
        nvarchar SESSION_ID
    }
    INVENTORY_ASSIGNMENT {
        nvarchar ASSIGNMENT_ID PK
    }
    INVENTORY_USER {
        nvarchar USER_ID PK
    }
    INVENTORY_ASSIGNMENT ||--o{ ASSIGNMENT_EXTENSION : "ASSIGNMENT_ID (inf)"
    INVENTORY_USER ||--o{ AUDIT_LOG : "USER_ID (inf)"
```
