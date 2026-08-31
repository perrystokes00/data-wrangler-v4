r"""
build_well_documents_view.py
============================
DataView v3 — "documented wells" view for the mapping app.

Creates dataview.v_well_documents: one row per UWI that has at least one
catalogued document, with its best-known surface coordinate and a rollup of
the documents we hold for it. The mapping app plots these points and shows the
document breakdown in the popup.

The document→well link already exists (GLOBAL_FILE_CATALOG.MATCHED_UWI), so no
promotion is required. Coordinates are taken from the best available source,
in priority order:
    1. WELL_REF.well_ref.WELL_MASTER  (reference master — keyed on UWI14)
    2. dataview.dv_well               (promoted / curated headers)
    3. file_catalog.cat_well          (coordinates captured from document headers)
Each source is semi-joined to documented UWIs, so the reference scan stays
small even though the master holds millions of rows. A well with documents but
no coordinate in any source simply doesn't plot.

Columns:
    uwi, lat, lon, well_name, coord_source,
    doc_count, pdf_count, log_count, seismic_count, office_count, gis_count,
    doc_types     (distinct FILE_TYPE_GROUP list, e.g. 'PDF, Seismic 2D, Well Log')

Materialized option
-------------------
A view recomputes on every query. For a large catalog, --materialize also
(re)builds dataview.well_documents as a physical table with indexes on the
coordinate columns and UWI, which the map can read instead for speed. Re-run
it whenever you want to refresh the snapshot (it's a full rebuild, idempotent).

    py build_well_documents_view.py                 # create / refresh the view
    py build_well_documents_view.py --materialize    # also (re)build the table
    py build_well_documents_view.py --server X --database Y

Default target: PERRY\SQLEXPRESS / DataView (Windows auth, ODBC Driver 17).
Requires: pip install pyodbc
"""
from __future__ import annotations

import argparse
import sys

import pyodbc


def connect(server: str, database: str):
    cs = (f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};"
          f"DATABASE={database};Trusted_Connection=yes;")
    return pyodbc.connect(cs, autocommit=True)


VIEW_SQL = r"""
CREATE OR ALTER VIEW dataview.v_well_documents AS
WITH docs AS (
    SELECT
        MATCHED_UWI AS uwi,
        COUNT(*)                                                  AS doc_count,
        SUM(CASE WHEN FILE_TYPE_GROUP = 'PDF'      THEN 1 ELSE 0 END) AS pdf_count,
        SUM(CASE WHEN FILE_TYPE_GROUP = 'Well Log' THEN 1 ELSE 0 END) AS log_count,
        SUM(CASE WHEN FILE_TYPE_GROUP IN ('Seismic','Seismic 2D','Seismic 3D')
                                                   THEN 1 ELSE 0 END) AS seismic_count,
        SUM(CASE WHEN FILE_TYPE_GROUP = 'Office'   THEN 1 ELSE 0 END) AS office_count,
        SUM(CASE WHEN FILE_TYPE_GROUP = 'Shapefile' THEN 1 ELSE 0 END) AS gis_count
    FROM file_catalog.GLOBAL_FILE_CATALOG
    WHERE NULLIF(LTRIM(RTRIM(MATCHED_UWI)), '') IS NOT NULL
      AND ISNULL(FLAG_DELETE, 'N') <> 'Y'
    GROUP BY MATCHED_UWI
),
types AS (
    SELECT uwi, STRING_AGG(ft, ', ') WITHIN GROUP (ORDER BY ft) AS doc_types
    FROM (
        SELECT DISTINCT MATCHED_UWI AS uwi, FILE_TYPE_GROUP AS ft
        FROM file_catalog.GLOBAL_FILE_CATALOG
        WHERE NULLIF(LTRIM(RTRIM(MATCHED_UWI)), '') IS NOT NULL
          AND ISNULL(FLAG_DELETE, 'N') <> 'Y'
          AND NULLIF(LTRIM(RTRIM(FILE_TYPE_GROUP)), '') IS NOT NULL
    ) x
    GROUP BY uwi
),
coords AS (
    SELECT uwi, lat, lon, well_name, coord_source
    FROM (
        SELECT uwi, lat, lon, well_name, coord_source,
               ROW_NUMBER() OVER (PARTITION BY uwi ORDER BY pr) AS rn
        FROM (
            -- 1) reference well master — broad, clean coordinates (keyed UWI14)
            SELECT UWI14 AS uwi, SURFACE_LATITUDE AS lat,
                   SURFACE_LONGITUDE AS lon, WELL_NAME AS well_name,
                   'reference' AS coord_source, 1 AS pr
            FROM WELL_REF.well_ref.WELL_MASTER
            WHERE SURFACE_LATITUDE IS NOT NULL AND SURFACE_LONGITUDE IS NOT NULL
              AND NULLIF(LTRIM(RTRIM(UWI14)), '') IS NOT NULL
              AND UWI14 IN (SELECT uwi FROM docs)
            UNION ALL
            -- 2) our promoted / curated headers
            SELECT uwi, surface_latitude, surface_longitude,
                   well_name, 'dv_well', 2
            FROM dataview.dv_well
            WHERE surface_latitude IS NOT NULL AND surface_longitude IS NOT NULL
              AND uwi IN (SELECT uwi FROM docs)
            UNION ALL
            -- 3) coordinates captured from document headers
            SELECT uwi, surface_latitude, surface_longitude,
                   well_name, 'cat_well', 3
            FROM file_catalog.cat_well
            WHERE surface_latitude IS NOT NULL AND surface_longitude IS NOT NULL
              AND uwi IN (SELECT uwi FROM docs)
        ) s
    ) r
    WHERE rn = 1
)
SELECT
    d.uwi,
    c.lat,
    c.lon,
    COALESCE(NULLIF(LTRIM(RTRIM(c.well_name)), ''), d.uwi) AS well_name,
    c.coord_source,
    d.doc_count, d.pdf_count, d.log_count, d.seismic_count,
    d.office_count, d.gis_count,
    t.doc_types
FROM docs d
JOIN coords c ON c.uwi = d.uwi
LEFT JOIN types t ON t.uwi = d.uwi;
"""

MATERIALIZE_SQL = r"""
IF OBJECT_ID('dataview.well_documents', 'U') IS NOT NULL
    DROP TABLE dataview.well_documents;

SELECT * INTO dataview.well_documents FROM dataview.v_well_documents;

CREATE CLUSTERED INDEX IX_well_documents_latlon
    ON dataview.well_documents (lat, lon);
CREATE NONCLUSTERED INDEX IX_well_documents_uwi
    ON dataview.well_documents (uwi);
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server",   default=r"PERRY\SQLEXPRESS")
    ap.add_argument("--database", default="DataView_Demo")
    ap.add_argument("--materialize", action="store_true",
                    help="also (re)build dataview.well_documents physical table")
    a = ap.parse_args()

    print(f"-- target: {a.server} / {a.database}")
    try:
        con = connect(a.server, a.database)
    except Exception as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        return 2
    cur = con.cursor()

    try:
        cur.execute(VIEW_SQL)
        print("-- created/updated view dataview.v_well_documents")
        cur.execute("SELECT COUNT(*) FROM dataview.v_well_documents")
        n = cur.fetchone()[0]
        print(f"-- documented wells with a coordinate: {n:,}")

        if a.materialize:
            for stmt in [s for s in MATERIALIZE_SQL.split(";") if s.strip()]:
                cur.execute(stmt)
            cur.execute("SELECT COUNT(*) FROM dataview.well_documents")
            m = cur.fetchone()[0]
            print(f"-- materialized dataview.well_documents: {m:,} rows "
                  f"(indexed on lat/lon and uwi)")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
