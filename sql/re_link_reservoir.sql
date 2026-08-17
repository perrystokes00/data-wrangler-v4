/* ── 4 · APPLY — commented. Run section 3 and read it first. ─────────────── */
;WITH pool AS (
    SELECT DISTINCT
           LTRIM(RTRIM(COALESCE(NULLIF(z.zone_name, ''), z.strat_unit_name))) AS nm,
           z.fluid_type
    FROM dataview.dv_well_petro_zone z
    WHERE LTRIM(RTRIM(COALESCE(NULLIF(z.zone_name, ''), z.strat_unit_name, ''))) <> ''
)
INSERT INTO dataview.dv_reservoir
      (reservoir_id, reservoir_name, fluid_type, source, row_created_by)
SELECT CONVERT(char(40), HASHBYTES('SHA1', UPPER(nm + '|' + ISNULL(fluid_type,''))), 2),
       nm, fluid_type, 'DERIVED', 'ADD_RESERVOIR'
FROM pool p
WHERE NOT EXISTS (SELECT 1 FROM dataview.dv_reservoir r
                   WHERE r.reservoir_id =
                         CONVERT(char(40), HASHBYTES('SHA1', UPPER(p.nm + '|' + ISNULL(p.fluid_type,''))), 2));

UPDATE z
   SET z.reservoir_id =
       CONVERT(char(40), HASHBYTES('SHA1',
           UPPER(LTRIM(RTRIM(COALESCE(NULLIF(z.zone_name,''), z.strat_unit_name)))
                 + '|' + ISNULL(z.fluid_type,''))), 2)
FROM dataview.dv_well_petro_zone z
WHERE LTRIM(RTRIM(COALESCE(NULLIF(z.zone_name,''), z.strat_unit_name, ''))) <> '';

