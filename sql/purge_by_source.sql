/* ===========================================================================
   purge_by_source.sql — remove every row belonging to wells from ONE SOURCE.
   PREVIEW BY DEFAULT; the delete ships commented.

   Set @source below. 'SYNTH' = the synthetic generator's wells.

   WHY SOURCE AND NOT A UWI PREFIX
   -------------------------------
   The prefix version (purge_uwi_prefix.sql) targets a FIELD — every well in
   Natrona County, whoever loaded it. This targets a PROVENANCE — everything
   one generator produced, wherever it landed. Different questions, and the
   synthetic set is deliberately scattered across four states, so no prefix
   describes it.

   ⚠ CHILD TABLES MAY NOT CARRY `source`. A row's provenance lives on dv_well;
   its casing strings and log curves inherit it by parentage, not by column. So
   children are selected by their WELL being in the source set, which is why
   this cannot simply be the prefix script with a different WHERE.

   ⚠ AND CHECK THE PREVIEW. `source` is free text written by whatever loaded
   the row — the values here are SYNTH, CATALOG and NULL. A typo deletes
   nothing; a wrong value deletes somebody else's data.

   WHY NOT A HAND-WRITTEN LIST OF TABLES
   -------------------------------------
   Around 35 dataview tables carry `uwi`, and the list changes. A hardcoded
   list is the same failure as clear_catalog's stale fallback: it silently
   stops covering tables somebody added later, and a delete that misses a
   table leaves orphans rather than raising. This discovers the tables from
   the live schema every run.

   THE THREE THINGS THAT MAKE THIS NON-TRIVIAL
   -------------------------------------------
   1 · ORDER. Children must go before parents, and some children are parents
       themselves — dv_well_dir_srvy_sta depends on dv_well_dir_srvy_hdr,
       dv_well_petro_zone on dv_well_petro_interp. So the order comes from a
       recursive walk of sys.foreign_keys, deepest first, not from the order
       tables happen to be listed in.

   2 · dv_prod_volume HAS NO uwi. It keys on prod_entity_id, with the well
       link on dv_prod_entity — the one documented exception in the schema.
       Deleting "everything with a uwi" silently leaves every production row
       behind, pointing at an entity that is about to disappear. It is handled
       explicitly below, BEFORE dv_prod_entity.

   3 · THE STAGING TABLES ARE NOT TOUCHED. stg.* is scratch: the next load
       recreates them, and clearing them here would only hide what a previous
       run staged. If you want them gone, drop them separately.

   WHAT SURVIVES, DELIBERATELY
   ---------------------------
   Reference tables (dv_r_*), dv_country / dv_province_state / dv_county,
   the three memories (dv_column_map, dv_column_synonym, dv_target_attribute),
   the load ledger, and every well whose UWI does not match the prefix.
   =========================================================================== */

SET NOCOUNT ON;
SET ARITHABORT ON;

/* A previous run that errored leaves #order behind in this session, and the
   next attempt then fails on "There is already an object named '#order'" —
   a different error for the same underlying attempt, which is confusing. */
IF OBJECT_ID('tempdb..#order') IS NOT NULL DROP TABLE #order;
IF OBJECT_ID('tempdb..#src_uwi') IS NOT NULL DROP TABLE #src_uwi;

DECLARE @source nvarchar(40) = N'SYNTH';       -- <<< the generator's wells

/* ── 0 · THE WELLS IN SCOPE, captured before anything is deleted ──────────
   dv_well is deleted LAST, but every child predicate needs to know which
   wells were in the set — and by then the source column is gone with the
   rows. Materialise the list first, exactly as clear_catalog captures its
   document ids before emptying the catalog that identifies them. */
IF OBJECT_ID('tempdb..#src_uwi') IS NOT NULL DROP TABLE #src_uwi;
SELECT uwi INTO #src_uwi
FROM dataview.dv_well WITH (NOLOCK)
WHERE ISNULL(source, '') = @source;
CREATE UNIQUE CLUSTERED INDEX IX_src_uwi ON #src_uwi(uwi);

DECLARE @n_wells int = (SELECT COUNT(*) FROM #src_uwi);
PRINT CONCAT('-- source ', @source, ': ', @n_wells, ' well(s) in scope');
IF @n_wells = 0
BEGIN
    PRINT '-- nothing matches that source. Check the spelling:';
    SELECT source, COUNT(*) AS wells FROM dataview.dv_well WITH (NOLOCK)
    GROUP BY source ORDER BY wells DESC;
    RETURN;
END

/* ── 1 · FK depth: how far each table sits below dv_well ─────────────────── */
;WITH fk AS (
    SELECT DISTINCT
           ct.name AS child, pt.name AS parent
    FROM sys.foreign_keys f
    JOIN sys.tables ct ON ct.object_id = f.parent_object_id
    JOIN sys.tables pt ON pt.object_id = f.referenced_object_id
    JOIN sys.schemas cs ON cs.schema_id = ct.schema_id
    WHERE cs.name = 'dataview' AND ct.object_id <> pt.object_id
),
depth AS (
    SELECT CAST('dv_well' AS sysname) AS tbl, 0 AS lvl
    UNION ALL
    SELECT CAST(fk.child AS sysname), d.lvl + 1
    FROM fk JOIN depth d ON fk.parent = d.tbl
    WHERE d.lvl < 8
),
ranked AS (
    SELECT tbl, MAX(lvl) AS lvl FROM depth GROUP BY tbl
)
SELECT tbl, lvl AS fk_depth          -- keep the name `tbl`: everything below
INTO #order                          -- refers to it, and renaming it here was
FROM ranked;                         -- the "Invalid column name 'tbl'" fault

/* Any uwi-carrying table the FK walk did not reach (no declared constraint)
   still has to be deleted — give it depth 1, i.e. before dv_well. */
INSERT INTO #order (tbl, fk_depth)
SELECT t.name, 1
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.columns c ON c.object_id = t.object_id AND c.name = 'uwi'
WHERE s.name = 'dataview'
  AND t.name LIKE 'dv[_]%' AND t.name NOT LIKE 'dv[_]r[_]%'
  AND t.name NOT IN (SELECT tbl FROM #order);

/* ── 2 · PREVIEW — what would go, deepest first ──────────────────────────── */
DECLARE @sql nvarchar(max) = N'';

SELECT @sql = @sql + N'
SELECT ' + QUOTENAME(o.tbl, '''') + N' AS [table], ' + CAST(o.fk_depth AS nvarchar(4))
    + N' AS fk_depth, COUNT(*) AS rows
FROM dataview.' + QUOTENAME(o.tbl) + N' WITH (NOLOCK) WHERE uwi IN (SELECT uwi FROM #src_uwi)
HAVING COUNT(*) > 0
UNION ALL'
FROM #order o
JOIN sys.tables t ON t.name = o.tbl
JOIN sys.schemas s ON s.schema_id = t.schema_id AND s.name = 'dataview'
JOIN sys.columns c ON c.object_id = t.object_id AND c.name = 'uwi'
ORDER BY o.fk_depth DESC, o.tbl;

/* dv_prod_volume has no uwi — reach it through its entity */
SET @sql = @sql + N'
SELECT ''dv_prod_volume'' AS [table], 99 AS fk_depth, COUNT(*) AS rows
FROM dataview.dv_prod_volume v WITH (NOLOCK)
WHERE EXISTS (SELECT 1 FROM dataview.dv_prod_entity e WITH (NOLOCK)
               WHERE e.prod_entity_id = v.prod_entity_id AND e.uwi IN (SELECT uwi FROM #src_uwi))
HAVING COUNT(*) > 0
ORDER BY fk_depth DESC, [table]';

EXEC sp_executesql @sql;

/* ── 3 · APPLY — commented. Read the preview first. ──────────────────────────
   One transaction: either the whole field goes or none of it does. A partial
   delete would leave children pointing at a well that no longer exists, which
   is harder to diagnose than either end state.

BEGIN TRY
  BEGIN TRAN;

  -- production first: keyed on the entity, not the well
  DELETE v FROM dataview.dv_prod_volume v
   WHERE EXISTS (SELECT 1 FROM dataview.dv_prod_entity e
                  WHERE e.prod_entity_id = v.prod_entity_id
                    AND e.uwi IN (SELECT uwi FROM #src_uwi));

  DECLARE @t sysname, @n int, @d nvarchar(max);
  DECLARE c CURSOR LOCAL FAST_FORWARD FOR
      SELECT o.tbl FROM #order o
      JOIN sys.tables t ON t.name = o.tbl
      JOIN sys.schemas s ON s.schema_id = t.schema_id AND s.name = 'dataview'
      JOIN sys.columns col ON col.object_id = t.object_id AND col.name = 'uwi'
      ORDER BY o.fk_depth DESC, o.tbl;      -- deepest child first, dv_well last
  OPEN c; FETCH NEXT FROM c INTO @t;
  WHILE @@FETCH_STATUS = 0
  BEGIN
      SET @d = N'DELETE FROM dataview.' + QUOTENAME(@t) + N' WHERE uwi IN (SELECT uwi FROM #src_uwi)';
      EXEC sp_executesql @d;
      SET @n = @@ROWCOUNT;
      IF @n > 0 PRINT '  ' + @t + ': ' + CAST(@n AS varchar(12)) + ' row(s)';
      FETCH NEXT FROM c INTO @t;
  END
  CLOSE c; DEALLOCATE c;

  COMMIT;
  PRINT 'done — committed';
END TRY
BEGIN CATCH
  IF @@TRANCOUNT > 0 ROLLBACK;
  PRINT 'ROLLED BACK — nothing deleted: ' + ERROR_MESSAGE();
END CATCH
--------------------------------------------------------------------------- */

/* ── 4 · verify afterwards ───────────────────────────────────────────────── */
/*
SELECT COUNT(*) AS wells_left FROM dataview.dv_well WHERE source = 'SYNTH';
SELECT COUNT(*) AS orphan_prod FROM dataview.dv_prod_volume v
 WHERE NOT EXISTS (SELECT 1 FROM dataview.dv_prod_entity e
                    WHERE e.prod_entity_id = v.prod_entity_id);
*/

DROP TABLE #order;
IF OBJECT_ID('tempdb..#src_uwi') IS NOT NULL DROP TABLE #src_uwi;
