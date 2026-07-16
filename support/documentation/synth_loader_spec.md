# Synthetic Test-Set Loader — Mapping Spec

Source: `well_status.zip` (16 files). 200 synthetic wells with full child data.
Target: `dataview.dv_*` tables in DataView (or DataView_Test first).
Key principle: **every child keys on UWI in the dashed `42-329-10001-0000`
format**, which already matches dv_well. This is what prevents the orphaned-
tops problem that started this thread.

## Load order (FK-safe)
1. well_header.csv      → dataview.dv_well                 (200 rows)  PARENT
2. well_picks.csv       → dataview.dv_well_formation_top   (684)
3. well_log.csv         → dataview.dv_well_log             (160)
4. well_log_curve.csv   → dataview.dv_well_log_curve       (1120)
5. well_dir_survey_hdr  → dataview.dv_well_dir_srvy_hdr    (120)
6. well_dir_survey_data → dataview.dv_well_dir_srvy_sta    (1966)
7. well_core.csv        → dataview.dv_well_core            (80)
8. well_production.csv  → dataview.dv_prod_entity + dv_prod_volume (2572) SPECIAL
9. strat_well_section   → dataview.dv_strat_interval       (1368)  (verify)

Lookups (load first or ignore — may already be seeded):
- well_area.csv   → dv_map_area? (27 rows) — VERIFY target
- well_status.csv → dv_r_well_status / dv_r_well_type (4 rows) — reference data

Not obviously well-data (decide later):
- seismic_checkshot.csv, ppdm_uom_mwd.csv, state_and_county.csv,
  state_county.csv, PPDM_Training_Manual.docx

## Column mappings (CSV → dv_ table)

### well_header.csv → dv_well
CSV: UWI, WELL_NAME, OPERATOR, WELL_CLASS, STATUS, STATUS_TYPE, SPUD_DATE,
     COMPLETION_DATE, COUNTRY, PROVINCE_STATE, COUNTY, SURFACE_LATITUDE,
     SURFACE_LONGITUDE, DRILLERS_TD, DEPTH_UNITS, KB_ELEV, GL_ELEV,
     DEPTH_DATUM, FIELD_NAME, FORMATION_AT_TD, LICENSEE, DATA_SOURCE

  UWI               -> uwi
  WELL_NAME         -> well_name
  OPERATOR          -> operator_name  (dv_well HAS this denormalized column,
                        confirmed. Optionally also resolve operator_ba_id, but
                        for test data writing the name directly is enough —
                        federation COALESCE reads operator_name first anyway.)
  WELL_CLASS        -> well_type
  STATUS            -> well_status
  STATUS_TYPE       -> ? (no direct col; maybe remark or a status subtype)
  SPUD_DATE         -> spud_date
  COMPLETION_DATE   -> completion_date
  COUNTRY           -> country
  PROVINCE_STATE    -> province_state
  COUNTY            -> county
  SURFACE_LATITUDE  -> surface_latitude
  SURFACE_LONGITUDE -> surface_longitude
  DRILLERS_TD       -> final_td
  DEPTH_UNITS       -> elevation_ouom? or depth-units (CHECK; FT)
  KB_ELEV           -> kb_elevation
  GL_ELEV           -> ground_elevation
  DEPTH_DATUM       -> depth_datum
  FIELD_NAME        -> field_name  (dv_well HAS this denormalized column,
                        confirmed. Same as operator — write name directly.)
  FORMATION_AT_TD   -> formation_at_td
  LICENSEE          -> license_num? (string name, not a number — CHECK)
  DATA_SOURCE       -> source

  RESOLVED:
  - dv_well HAS operator_name AND field_name denormalized columns (confirmed
    from full DDL). Write CSV OPERATOR/FIELD_NAME straight into them. No BA or
    field reference seeding required for the test set to display correctly.
  - STATUS_TYPE has no obvious dv_well home; candidates: append to remark, or
    map into well_type vs well_status split. Decide at build (low stakes).
  - LICENSEE is a name string ("ANADARKO_PETRO"), not a numeric license. Put
    in license_num as text, or a remark. Decide at build (low stakes).

### well_picks.csv → dv_well_formation_top
CSV: UWI, STRAT_NAME_SET_ID, STRAT_UNIT_ID, INTERP_ID, SOURCE, TOP_MD,
     BASE_MD, INTERP_DATE, INTERP_BY
  UWI               -> uwi
  STRAT_NAME_SET_ID -> strat_name_set
  STRAT_UNIT_ID     -> strat_unit_name (CHECK: unit_id vs unit_name; CSV value
                       is a name like "GLORIETA", so -> strat_unit_name, and
                       synthesize strat_unit_id or leave null)
  INTERP_ID         -> interp_id
  SOURCE            -> source
  TOP_MD            -> top_depth
  BASE_MD           -> base_depth
  INTERP_DATE       -> (no col seen; maybe row_created_date or ignore)
  INTERP_BY         -> row_created_by
  gross_thickness   -> compute (base_depth - top_depth) or leave null

### well_log.csv → dv_well_log
CSV: UWI, LOG_ID, LOG_TYPE, RUN_NO, LOG_DATE, TOP_DEPTH, BASE_DEPTH, SOURCE
  UWI -> uwi; LOG_ID -> log_id; LOG_TYPE -> log_type; RUN_NO -> run_num;
  LOG_DATE -> log_date; TOP_DEPTH -> top_depth; BASE_DEPTH -> base_depth;
  SOURCE -> source

### well_log_curve.csv → dv_well_log_curve
CSV: UWI, LOG_ID, CURVE_NAME, CURVE_UNIT, MIN_VALUE, MAX_VALUE, SOURCE
  UWI -> uwi; LOG_ID -> log_id; CURVE_NAME -> mnemonic; CURVE_UNIT -> curve_unit;
  MIN_VALUE -> min_value; MAX_VALUE -> max_value; SOURCE -> source
  curve_id -> synthesize (sequence per log_id) — table has curve_id col

### well_dir_survey_hdr.csv → dv_well_dir_srvy_hdr
CSV: UWI, SRVY_ID, SURVEY_SEQ_NO, SOURCE, <trailing empty col>
  UWI -> uwi; SRVY_ID -> survey_id; SURVEY_SEQ_NO -> ? (no col; remark or skip);
  SOURCE -> source
  NOTE: strip trailing empty column on read.

### well_dir_survey_data.csv → dv_well_dir_srvy_sta
CSV: UWI, SRVY_ID, SURVEY_SEQ_NO, MD, INCLINATION, AZIMUTH, TVDSS, SOURCE, <empty>
  UWI -> uwi; SRVY_ID -> survey_id; MD -> md; INCLINATION -> incl;
  AZIMUTH -> azim; TVDSS -> tvd; SOURCE -> source
  station_id -> synthesize (sequence per survey_id)
  SURVEY_SEQ_NO -> ? (maybe station ordering)
  NOTE: strip trailing empty column.

### well_core.csv → dv_well_core
CSV: UWI, CORE_ID, CORE_TYPE, TOP_DEPTH, BASE_DEPTH, RECOVERY_PCT, FORMATION, SOURCE
  UWI -> uwi; CORE_ID -> core_id; CORE_TYPE -> core_type; TOP_DEPTH -> top_depth;
  BASE_DEPTH -> base_depth; RECOVERY_PCT -> recovery_pct;
  FORMATION -> strat_unit_name; SOURCE -> source

### well_production.csv → dv_prod_entity + dv_prod_volume  (SPECIAL: 2 tables)
CSV: UWI, PROD_DATE, PROD_PERIOD, OIL_VOL, GAS_VOL, WATER_VOL, OIL_UNIT,
     GAS_UNIT, WATER_UNIT, SOURCE
  Production is split across two tables in the schema:
  - dv_prod_entity: one row per producing entity (per UWI). Synthesize
    prod_entity_id (e.g. UWI-based), set uwi, prod_entity_type, source.
  - dv_prod_volume: one row per (entity, period, fluid). The CSV has wide
    format (oil/gas/water in one row) — must UNPIVOT to long:
      (prod_entity_id, period_date=PROD_DATE, fluid_type='OIL',
       volume=OIL_VOL, volume_ouom=OIL_UNIT) and same for GAS, WATER.
    So 2572 CSV rows -> up to 3x volume rows.
  This is the most complex transform in the set.

### strat_well_section.csv → dv_strat_interval  (VERIFY target)
CSV: UWI, STRAT_NAME_SET_ID, STRAT_UNIT_ID, INTERP_ID, PICK_LOCATION,
     PICK_DEPTH, PICK_DATE, STRAT_TYPE, INTERP_BY, SOURCE
  Possible this is ANOTHER picks-like table OR feeds dv_strat_interval.
  dv_strat_interval has top_depth/base_depth (interval), but this CSV has
  a single PICK_DEPTH (point). May actually be a second set of formation
  picks rather than intervals. DECIDE next session — could be it belongs in
  dv_well_formation_top too, or a strat picks table not yet identified.

## Transforms needed
1. UWI: already dashed format, matches dv_well. No transform. (VERIFY no
   trailing spaces — the earlier orphan issue. Add LTRIM/RTRIM on load.)
2. Trailing empty columns on the two survey CSVs — strip on read.
3. Production wide->long unpivot (oil/gas/water -> fluid_type rows).
4. Synthesize surrogate IDs where the table needs one the CSV lacks:
   curve_id, station_id, prod_entity_id.
5. operator_name/field_name: dv_well HAS these columns — write directly from
   CSV OPERATOR / FIELD_NAME. No reference-table seeding needed for display.
6. gross_thickness compute for formation tops.

## Loader architecture (proposed — decide next session)
Dedicated page `page_synth_loader.py` OR a script `load_synth_testset.py`.
Per-table flow mirrors the established pattern:
  - Read CSV (pandas), strip trailing empty cols, LTRIM/RTRIM uwi
  - Show mapping preview (CSV col -> dv col) using a per-table MAP dict
  - UWI preflight: for child tables, assert every uwi exists in dv_well;
    report orphans BEFORE insert (this is the key safeguard)
  - Stage to a temp table, bulk insert, then INSERT...SELECT into target
  - Per-table row-count + orphan report

Target DB toggle: DataView_Test first to validate, then DataView.

## Critical safeguards (the lesson)
- UWI preflight on EVERY child load: orphan rows (uwi not in dv_well) are
  flagged and NOT inserted, or the load aborts with a clear message.
- LTRIM/RTRIM uwi on both sides to avoid invisible-whitespace mismatch.
- Load header FIRST so the preflight set exists.
