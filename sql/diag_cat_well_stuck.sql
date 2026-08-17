/* ============================================================================
   diag_cat_well_stuck.sql
   Why a fully-loaded file reads CATALOGED instead of PROMOTED.

   THE SYMPTOM
   -----------
   promotion_lineage reports a file as promoted with its detail rows in dv_*,
   while the catalog scorecard shows the same file as CATALOGED. Both are
   correct; they measure different things.

   catalog_readiness._CASE tests CATALOGED BEFORE PROMOTED:

       WHEN hc.INVENTORY_ID IS NOT NULL THEN 'CATALOGED'   -- pending cat_ rows
       WHEN hd.INVENTORY_ID IS NOT NULL THEN 'PROMOTED'    -- represented in dv_

   and _build_distinct_temp builds hc with no disposition filter:

       SELECT INVENTORY_ID FROM file_catalog.cat_x WHERE INVENTORY_ID IS NOT NULL

   So ONE un-promotable row in ANY cat_ table pins the whole file at CATALOGED,
   outranking all promoted evidence. The ordering encodes an assumption -- that
   a cat_ row means work pending -- which holds only if every cat_ row is
   promotable. A row parked by a gate it can never satisfy is not pending, and
   there is no third disposition to say so. Adding `AND PROMOTED = 0` does NOT
   help: these rows genuinely have PROMOTED = 0.

   WHAT ACTUALLY GATES THE HEADERS
   -------------------------------
   NOT the NOT EXISTS / first-one-in-wins rule. It is REQUIRE_WELL_COORDS in
   promote_catalog._promote_header: a well with no surface coordinates is HELD
   so an unmappable well never reaches dv_well. Document headers arrive with no
   coordinates, so they are excluded from `eligible` before the duplicate check
   is ever reached. The duplicate condition is real but never comes into play.

   MEASURED 17 Aug 2026, DataView_Demo, synth50 corpus
   ---------------------------------------------------
     cat_well  PROMOTED = 0 : 46 rows (all source = 'SHAPE')
     cat_well  PROMOTED = 1 :  0 rows   <-- no header has EVER promoted
     dv_well              : 50 rows (all source = 'SYNTH', from dv_well.csv)

     All 46 stuck rows have NULL surface_latitude/surface_longitude.
     42 of them duplicate a UWI dv_well already holds, across 26 wells.
     GFC readiness: PROMOTED 57, CATALOGED 46, NEEDS_UWI 3, READY 1, REVIEW 1.
     42 of those 46 CATALOGED files are COMPLETE -- their only unpromoted row
     is a single cat_well header. True promoted 99, reported 57.

   THE DOCUMENT HEADERS CARRY NO NEW INFORMATION, BUT THEY ARE NOT IDENTICAL.
   PART 3 measured zero columns where cat_well has a value and dv_well is NULL.
   Two columns genuinely differ: province_state 42/42 and field_name 41/42.
   province_state is an ENCODING difference ('KS' vs '15', postal vs API state
   code). field_name is a real disagreement -- and the documents disagree with
   EACH OTHER (15005208780000 is 'Hugoton' in one file, 'Chase-Silica' in
   another), so no single document header is authoritative. Generator noise in
   synth50; a merge decision on real data. Do not assume redundancy from a
   three-column diff -- that mistake was made and corrected on 17 Aug.

   EXPECTED RESULT AFTER THE FIX
   -----------------------------
   PART 1 should return 4 rows, not 46: the three orphan-well rows (PART 5) and
   COMPLETION_REPORT2641.docx, which has no UWI. Anything else means the fix
   did something unintended. That is the reason this file exists.

   READS ONLY DATA -- no INSERT/UPDATE/DELETE anywhere. It does create one
   helper view, file_catalog.v_cat_well_stuck (DDL; drop line at the bottom).

   UWI PADDING: both sides use the canonical UWI-14 from
   bulk_dir_loader.build_promote_sql (de-sep, right-pad '0', LEFT 14, CAST
   char(14)). MEASURED: cat_well.uwi is already char(14) and all 45 non-null
   stuck rows are exactly 14, so the transform is currently a no-op -- keep it
   anyway. char(14) SPACE-pads and the canonical form ZERO-pads, so a 12-digit
   API would compare equal on one side and not the other. A missing pad on one
   side made an FK clause silently inert for six weeks.

   CAVEAT: promote_catalog compares with its own _norm(). If _norm() differs
   from the expression here, PART 1 will disagree with what promote does, and
   that difference is itself the bug.
============================================================================ */
USE DataView_Demo;
GO
SET NOCOUNT ON;

/* ---------------------------------------------------------------------------
   PART 0 -- what cat_well actually has, so PART 3 can be extended safely
--------------------------------------------------------------------------- */
SELECT c.name AS column_name, t.name AS type_name, c.max_length, c.is_nullable
  FROM sys.columns c
  JOIN sys.types   t ON t.user_type_id = c.user_type_id
 WHERE c.object_id = OBJECT_ID('file_catalog.cat_well')
 ORDER BY c.column_id;
GO

/* ---------------------------------------------------------------------------
   The classification. cat_well rows still at PROMOTED = 0, tagged with why.

   CAUSE PRECEDENCE MATTERS: no-UWI is tested FIRST. An earlier version tested
   no_coords first, which labelled COMPLETION_REPORT2641.docx (no UWI at all)
   as "held -- no coords" and hid the real reason.
--------------------------------------------------------------------------- */
CREATE OR ALTER VIEW file_catalog.v_cat_well_stuck AS
SELECT  m.CAT_ROW_ID,
        m.INVENTORY_ID,
        m.uwi                                   AS raw_uwi,
        k.uwi14,
        m.well_name,
        m.source,
        m.surface_latitude,
        m.surface_longitude,
        m.CAPTURED_AT,
        m.SOURCE_PATH,
        CAST(CASE WHEN dw.uwi IS NOT NULL THEN 1 ELSE 0 END AS bit) AS dv_well_exists,
        CAST(CASE WHEN m.surface_latitude IS NULL
                    OR m.surface_longitude IS NULL THEN 1 ELSE 0 END AS bit) AS no_coords,
        CASE WHEN k.uwi14 IS NULL
                  THEN 'D    no UWI on the mirror row'
             WHEN dw.uwi IS NOT NULL AND (m.surface_latitude IS NULL
                                       OR m.surface_longitude IS NULL)
                  THEN 'A+B  duplicate AND no coords'
             WHEN dw.uwi IS NOT NULL
                  THEN 'A    duplicate -- dv_well already owns this UWI'
             WHEN m.surface_latitude IS NULL OR m.surface_longitude IS NULL
                  THEN 'B    held -- no surface coordinates (REQUIRE_WELL_COORDS)'
             ELSE 'C    should have promoted -- investigate'
        END AS cause
  FROM file_catalog.cat_well m
 CROSS APPLY (SELECT REPLACE(REPLACE(REPLACE(LTRIM(RTRIM(m.uwi)),
                     '-',''),' ',''),'.','') AS desep) d
 CROSS APPLY (SELECT CAST(CASE WHEN NULLIF(d.desep,'') IS NULL THEN NULL
                               ELSE LEFT(CONCAT(d.desep, REPLICATE('0',14)), 14)
                          END AS char(14)) AS uwi14) k
  LEFT JOIN dataview.dv_well dw
         ON dw.uwi = k.uwi14
 WHERE m.PROMOTED = 0;
GO

/* ---------------------------------------------------------------------------
   PART 1 -- the headline. Should be 4 rows once the gate order is fixed.
--------------------------------------------------------------------------- */
SELECT  cause,
        COUNT(*)                        AS stuck_rows,
        COUNT(DISTINCT INVENTORY_ID)    AS files_affected,
        COUNT(DISTINCT uwi14)           AS distinct_wells
  FROM file_catalog.v_cat_well_stuck
 GROUP BY cause
 ORDER BY stuck_rows DESC;
GO

/* ---------------------------------------------------------------------------
   PART 1b -- has ANY cat_well row ever promoted? If PROMOTED=1 is zero, the
   coord gate is blocking every document-derived header, not just some.
--------------------------------------------------------------------------- */
SELECT 'cat_well'  AS tbl, PROMOTED, ISNULL(source,'(null)') AS src, COUNT(*) AS rows_
  FROM file_catalog.cat_well GROUP BY PROMOTED, source
UNION ALL
SELECT 'dv_well', NULL, ISNULL(source,'(null)'), COUNT(*)
  FROM dataview.dv_well GROUP BY source
 ORDER BY tbl, PROMOTED, rows_ DESC;
GO

/* ---------------------------------------------------------------------------
   PART 2 -- per file, joined to what the scorecard reads.

   The disagreement is one row: CATALOG_READINESS = 'CATALOGED' beside
   dv_well_exists = 1. A NULL FILE_NAME means the INVENTORY_ID does not
   resolve in GLOBAL_FILE_CATALOG at all -- see PART 5.
--------------------------------------------------------------------------- */
SELECT  ISNULL(g.FILE_NAME, '(no GFC row)') AS file_name,
        g.CATALOG_READINESS,                    -- what the scorecard buckets on
        g.PROMOTED_AT,
        s.uwi14,
        s.dv_well_exists,
        s.no_coords,
        s.cause,
        s.well_name,
        s.source,
        s.CAPTURED_AT
  FROM file_catalog.v_cat_well_stuck s
  LEFT JOIN file_catalog.GLOBAL_FILE_CATALOG g
         ON g.INVENTORY_ID = s.INVENTORY_ID
 ORDER BY s.cause, file_name;
GO

/* ---------------------------------------------------------------------------
   PART 3 -- THE DECISION MATERIAL. Per column, is the stuck header POORER
   than the promoted row, RICHER, or in genuine CONFLICT?

       cat_null_dv_has  -> cat is poorer; nothing lost by clearing the row
       cat_has_dv_null  -> cat ADDS information; clearing it loses data
       conflict         -> both set and different; needs a human

   Read all three. A diff over too few columns reads as redundancy.
   Add columns to the VALUES list to widen it -- everything is CAST to
   nvarchar so one comparison shape covers text, numeric and date alike.
   Excludes geog (geography) and h3_coord_hash (binary): not <>-comparable.
--------------------------------------------------------------------------- */
SELECT  v.col,
        SUM(CASE WHEN v.cval IS NULL     AND v.dval IS NOT NULL THEN 1 ELSE 0 END) AS cat_null_dv_has,
        SUM(CASE WHEN v.cval IS NOT NULL AND v.dval IS NULL     THEN 1 ELSE 0 END) AS cat_has_dv_null,
        SUM(CASE WHEN v.cval IS NOT NULL AND v.dval IS NOT NULL
                  AND v.cval <> v.dval                          THEN 1 ELSE 0 END) AS conflict,
        COUNT(*) AS pairs
  FROM file_catalog.cat_well c
  JOIN dataview.dv_well d ON d.uwi = c.uwi
 CROSS APPLY (VALUES
        ('well_name',       CAST(c.well_name       AS nvarchar(4000)), CAST(d.well_name       AS nvarchar(4000))),
        ('well_num',        CAST(c.well_num        AS nvarchar(4000)), CAST(d.well_num        AS nvarchar(4000))),
        ('operator_name',   CAST(c.operator_name   AS nvarchar(4000)), CAST(d.operator_name   AS nvarchar(4000))),
        ('field_name',      CAST(c.field_name      AS nvarchar(4000)), CAST(d.field_name      AS nvarchar(4000))),
        ('county',          CAST(c.county          AS nvarchar(4000)), CAST(d.county          AS nvarchar(4000))),
        ('province_state',  CAST(c.province_state  AS nvarchar(4000)), CAST(d.province_state  AS nvarchar(4000))),
        ('well_type',       CAST(c.well_type       AS nvarchar(4000)), CAST(d.well_type       AS nvarchar(4000))),
        ('well_status',     CAST(c.well_status     AS nvarchar(4000)), CAST(d.well_status     AS nvarchar(4000))),
        ('api_num',         CAST(c.api_num         AS nvarchar(4000)), CAST(d.api_num         AS nvarchar(4000))),
        ('lease_name',      CAST(c.lease_name      AS nvarchar(4000)), CAST(d.lease_name      AS nvarchar(4000))),
        ('spud_date',       CAST(c.spud_date       AS nvarchar(4000)), CAST(d.spud_date       AS nvarchar(4000))),
        ('completion_date', CAST(c.completion_date AS nvarchar(4000)), CAST(d.completion_date AS nvarchar(4000))),
        ('final_td',        CAST(c.final_td        AS nvarchar(4000)), CAST(d.final_td        AS nvarchar(4000))),
        ('kb_elevation',    CAST(c.kb_elevation    AS nvarchar(4000)), CAST(d.kb_elevation    AS nvarchar(4000))),
        ('ground_elevation',CAST(c.ground_elevation AS nvarchar(4000)),CAST(d.ground_elevation AS nvarchar(4000))),
        ('surface_latitude',CAST(c.surface_latitude AS nvarchar(4000)),CAST(d.surface_latitude AS nvarchar(4000))),
        ('surface_longitude',CAST(c.surface_longitude AS nvarchar(4000)),CAST(d.surface_longitude AS nvarchar(4000))),
        ('remark',          CAST(c.remark          AS nvarchar(4000)), CAST(d.remark          AS nvarchar(4000)))
      ) v(col, cval, dval)
 WHERE c.PROMOTED = 0
 GROUP BY v.col
 ORDER BY conflict DESC, cat_null_dv_has DESC, v.col;
GO

/* ---------------------------------------------------------------------------
   PART 4 -- eyeball the conflicting columns. An encoding difference and a real
   disagreement look identical in a count.
--------------------------------------------------------------------------- */
SELECT TOP 20
        c.uwi,
        c.province_state    AS cat_state,
        d.province_state    AS dv_state,
        c.field_name        AS cat_field,
        d.field_name        AS dv_field,
        c.source            AS cat_source
  FROM file_catalog.cat_well c
  JOIN dataview.dv_well d ON d.uwi = c.uwi
 WHERE c.PROMOTED = 0
   AND (ISNULL(c.province_state,'~') <> ISNULL(d.province_state,'~')
     OR ISNULL(c.field_name,'~')     <> ISNULL(d.field_name,'~'))
 ORDER BY c.uwi;
GO

/* ---------------------------------------------------------------------------
   PART 5 -- provenance break: stuck rows whose INVENTORY_ID does not resolve
   in GLOBAL_FILE_CATALOG. These are invisible to EVERY report that joins
   through GFC, including PART 6 below.

   MEASURED 17 Aug: three rows, all well-formed 40-char SHA1s, and the same
   three files whose UWIs exist nowhere else in the corpus (the 9xxxx
   well-number block). Two independent anomalies on the same three files means
   a different write path, not a coincidence.
--------------------------------------------------------------------------- */
SELECT  c.CAT_ROW_ID,
        c.uwi,
        ISNULL(c.INVENTORY_ID,'(NULL)') AS inv_id,
        LEN(c.INVENTORY_ID)             AS inv_len,
        c.source,
        c.SOURCE_PATH
  FROM file_catalog.cat_well c
 WHERE c.PROMOTED = 0
   AND NOT EXISTS (SELECT 1 FROM file_catalog.GLOBAL_FILE_CATALOG g
                    WHERE g.INVENTORY_ID = c.INVENTORY_ID)
 ORDER BY c.uwi;
GO

/* ---------------------------------------------------------------------------
   PART 6 -- the scorecard undercount, measured.

   Files stamped CATALOGED whose ONLY remaining cat_ rows are unpromoted.
   Every one where all_rows_stuck is true and the header already exists in
   dv_well is complete work reported as pending.

   Builds a UNION ALL over every cat_ table carrying INVENTORY_ID + PROMOTED.
   Note 'cat[_]%': _ is a LIKE wildcard, so unbracketed 'cat_%' also matches
   catalog_setting.
--------------------------------------------------------------------------- */
DECLARE @union nvarchar(max);

SELECT @union = STRING_AGG(
        CAST('SELECT INVENTORY_ID, ''' + t.name + ''' AS cat_table, PROMOTED'
             + ' FROM file_catalog.' + QUOTENAME(t.name)
             + ' WHERE INVENTORY_ID IS NOT NULL' AS nvarchar(max)),
        CHAR(13) + CHAR(10) + 'UNION ALL' + CHAR(13) + CHAR(10))
  FROM sys.tables  t
  JOIN sys.schemas s ON s.schema_id = t.schema_id
  JOIN sys.columns c ON c.object_id = t.object_id AND c.name = 'INVENTORY_ID'
  JOIN sys.columns p ON p.object_id = t.object_id AND p.name = 'PROMOTED'
 WHERE s.name = 'file_catalog'
   AND t.name LIKE 'cat[_]%';

DECLARE @sql nvarchar(max) = N'
WITH allcat AS (
' + @union + N'
), per_file AS (
    SELECT INVENTORY_ID,
           COUNT(*)                                       AS cat_rows,
           SUM(CASE WHEN PROMOTED = 0 THEN 1 ELSE 0 END)  AS unpromoted_rows
      FROM allcat
     GROUP BY INVENTORY_ID
), flagged AS (
    /* the EXISTS must be resolved per file BEFORE aggregating -- SUM() over a
       subquery is Msg 130, "cannot perform an aggregate function on an
       expression containing an aggregate or a subquery". */
    SELECT g.CATALOG_READINESS,
           f.cat_rows,
           f.unpromoted_rows,
           CASE WHEN EXISTS (SELECT 1 FROM dataview.dv_well w
                              WHERE w.uwi = g.MATCHED_UWI)
                THEN 1 ELSE 0 END AS hdr_in_dv_well
      FROM file_catalog.GLOBAL_FILE_CATALOG g
      JOIN per_file f ON f.INVENTORY_ID = g.INVENTORY_ID
)
SELECT CATALOG_READINESS,
       COUNT(*)                                                      AS files,
       SUM(CASE WHEN unpromoted_rows = cat_rows THEN 1 ELSE 0 END)   AS all_rows_stuck,
       SUM(CASE WHEN unpromoted_rows > 0 AND unpromoted_rows < cat_rows
                THEN 1 ELSE 0 END)                                   AS partly_stuck,
       SUM(hdr_in_dv_well)                                           AS header_already_in_dv_well
  FROM flagged
 GROUP BY CATALOG_READINESS
 ORDER BY files DESC;

/* which mirrors hold the stuck rows, per file */
WITH allcat AS (
' + @union + N'
)
SELECT g.FILE_NAME,
       g.CATALOG_READINESS,
       a.cat_table,
       COUNT(*) AS unpromoted_rows
  FROM allcat a
  JOIN file_catalog.GLOBAL_FILE_CATALOG g ON g.INVENTORY_ID = a.INVENTORY_ID
 WHERE a.PROMOTED = 0
 GROUP BY g.FILE_NAME, g.CATALOG_READINESS, a.cat_table
 ORDER BY g.FILE_NAME, a.cat_table;
';

PRINT 'cat_ tables in the union: '
      + CAST((LEN(@union) - LEN(REPLACE(@union, 'UNION ALL', ''))) / 9 + 1 AS varchar(10));
EXEC sys.sp_executesql @sql;
GO

/* ---------------------------------------------------------------------------
   PART 7 -- overall readiness spread, for context on PART 6's numbers.
--------------------------------------------------------------------------- */
SELECT ISNULL(CATALOG_READINESS,'(null)') AS readiness, COUNT(*) AS files
  FROM file_catalog.GLOBAL_FILE_CATALOG
 GROUP BY CATALOG_READINESS
 ORDER BY files DESC;
GO

/* ---------------------------------------------------------------------------
   CLEANUP -- the view is left in place deliberately; it is the before/after
   check for the gate-ordering fix. Drop it when that work is finished.
--------------------------------------------------------------------------- */
-- DROP VIEW file_catalog.v_cat_well_stuck;
