# Codebase census — C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\data_wrangler\data_wrangler_v4

## What is actually in here — 2,877 file(s), 284 MB

Before asking which PYTHON is dead, see how much of the repo is
python at all. A tree with thousands of files is usually a few
hundred of source and the rest is what the code produced, sitting
where it was written.

| kind | files | MB |
|---|---:|---:|
| code | 543 | 23 |
| data | 71 | 26 |
| artefact | 279 | 12 |
| other | 1,984 | 223 |

### By extension, most files first

| ext | kind | files | MB |
|---|---|---:|---:|
| `(none)` | other | 1,892 | 177.0 |
| `.py` | code | 423 | 8.7 |
| `.pyc` | artefact | 265 | 8.4 |
| `.sql` | code | 65 | 5.7 |
| `.md` | code | 28 | 0.4 |
| `.png` | other | 24 | 25.4 |
| `.json` | data | 23 | 1.5 |
| `.docx` | data | 17 | 0.6 |
| `.txt` | data | 16 | 2.7 |
| `.gdbtable` | other | 16 | 0.3 |
| `.gdbtablx` | other | 16 | 0.1 |
| `.html` | code | 14 | 8.2 |
| `.sample` | other | 14 | 0.0 |
| `.bak` | artefact | 9 | 3.1 |
| `.gdbindexes` | other | 9 | 0.0 |
| `.geojson` | data | 7 | 21.3 |
| `.ps1` | code | 6 | 0.0 |
| `.bat` | code | 5 | 0.0 |
| `(none)` | artefact | 4 | 0.0 |
| `.csv` | data | 3 | 0.1 |
| `.jpg` | other | 3 | 13.5 |
| `.xlsx` | data | 2 | 0.0 |
| `.pdf` | data | 2 | 0.0 |
| `.duckdb` | other | 1 | 4.0 |
| `.toml` | code | 1 | 0.0 |

### By top-level folder

A folder that is nearly all data or artefacts does not belong in a
source package — it belongs beside it, or in .gitignore.

| folder | files | MB |
|---|---:|---:|
| `.git` | 1,901 | 177.0 |
| `dataview` | 505 | 29.0 |
| `tools` | 129 | 1.3 |
| `support` | 75 | 6.8 |
| `sql` | 73 | 5.5 |
| `(root)` | 67 | 12.8 |
| `exports` | 53 | 1.4 |
| `docshape` | 45 | 0.5 |
| `assets` | 13 | 49.3 |
| `documents` | 11 | 0.2 |
| `well_icons` | 2 | 0.0 |
| `__pycache__` | 2 | 0.1 |
| `.streamlit` | 1 | 0.0 |

**279 artefact file(s), 12 MB** — caches, logs, reports, archives, zips. None of it is source. Removing it from the repo changes no behaviour, and it is the cheapest cut available.

---

## The python question — 423 python file(s)
- entry point(s): selftest.py
- **70 LIVE** (reachable from an entry point)
- **170 SCRIPT** (standalone, has a __main__ guard)
- **183 ORPHAN** (unreachable, no __main__)
- 1 file(s) do not parse

## ⚠ READ THIS FIRST — dynamic imports the census cannot follow

Each of these picks a module by a name computed at runtime, so a
module reached ONLY this way looks orphaned. While this list is
non-empty, treat ORPHAN as candidates, not as a delete list.

- `dataview\file_catalog\catalog_doc_capture.py:40` — importlib.import_module(<not a literal>)
- `dataview\file_catalog\page_file_catalog.py:21` — __import__(<not a literal>)
- `dataview\file_catalog\worker_core.py:651` — __import__(<not a literal>)
- `dataview\import_data\bdl_dir3.py:39` — __import__(<not a literal>)
- `dataview\import_data\bdl_dir3.py:42` — __import__(<not a literal>)
- `dataview\import_data\dvpath\dataview\import_data\bulk_dir_loader.py:39` — __import__(<not a literal>)
- `dataview\import_data\dvpath\dataview\import_data\bulk_dir_loader.py:42` — __import__(<not a literal>)
- `dataview\mapping\dv_table_loader.py:410` — __import__(<not a literal>)
- `dataview\migration\synth_docs.py:882` — __import__(<not a literal>)
- `docshape\packs\__init__.py:73` — importlib.import_module(<not a literal>)
- `docshape\packs\__init__.py:125` — importlib.import_module(<not a literal>)
- `selftest.py:177` — importlib.import_module(<not a literal>)

## Files that do not parse

- `dataview\tools\database_scorecard.py` — SyntaxError line 1

## ORPHAN — nothing imports these and they cannot be run

Largest first. For each, grep the repo for its module name before
deleting; a name in the dynamic list above may reach it.

| file | KB | days since change |
|---|---:|---:|
| `dataview\mapping\page_well_map.py` | 471 | 1 |
| `dataview\file_catalog\page_file_manager.py` | 180 | 18 |
| `dataview\import_data\page_bulk.py` | 173 | 27 |
| `dataview\file_catalog\page_file_inventory_gov.py` | 162 | 27 |
| `dataview\file_catalog\page_file_inventory.py` | 157 | 27 |
| `dataview\mapping\page_well_map_docs.py` | 156 | 17 |
| `dataview\import_data\page_load_assistant.py` | 136 | 3 |
| `dataview\import_data\page_pipeline.py` | 136 | 24 |
| `dataview\import_data\pla_dir3.py` | 119 | 6 |
| `dataview\import_data\exporters.py` | 75 | 27 |
| `dataview\file_catalog\page_file_browser.py` | 72 | 27 |
| `dataview\core\fk.py` | 71 | 27 |
| `dataview\file_catalog\page_well_documents.py` | 71 | 1 |
| `dataview\file_catalog\page_file_catalog.py` | 64 | 27 |
| `dataview\mapping\mapping_studio.py` | 59 | 27 |
| `docshape\packs\petroleum.py` | 58 | 1 |
| `dataview\import_data\page_import_osdu.py` | 58 | 27 |
| `app_v4.py` | 55 | 4 |
| `dataview\file_catalog\dlis_catalog.py` | 55 | 27 |
| `dataview\file_catalog\catalog_rules.py` | 50 | 27 |
| `dataview\file_catalog\las_catalog.py` | 49 | 27 |
| `dataview\file_catalog\file_inventory.py` | 42 | 1 |
| `dataview\file_catalog\inv_workbench.py` | 40 | 27 |
| `dataview\mapping\page_mapping_studio.py` | 39 | 27 |
| `dataview\file_catalog\segy_catalog.py` | 39 | 27 |
| `dataview\reference_tables\page_rules.py` | 39 | 27 |
| `dataview\mapping\dv_table_loader.py` | 37 | 27 |
| `dataview\db_explorer\page_db_explorer.py` | 36 | 27 |
| `dataview\file_catalog\page_dv_catalog.py` | 36 | 27 |
| `page_ppdm_promote.py` | 36 | 14 |
| `dataview\import_data\synonym_store.py` | 35 | 6 |
| `dataview\db_explorer\page_federation_search.py` | 34 | 27 |
| `dataview\region_builder\page_region_builder.py` | 34 | 27 |
| `synonym_store_v4.py` | 33 | 6 |
| `dataview\file_catalog\page_docshape.py` | 32 | 11 |
| `dataview\file_catalog\las_loader.py` | 32 | 27 |
| `documents\file_viewer.py` | 32 | 27 |
| `dataview\file_catalog\file_inventory_governance.py` | 32 | 27 |
| `synonym_store_v2.py` | 31 | 6 |
| `dataview\file_catalog\page_shapefile_catalog.py` | 30 | 27 |
| `dataview\core\validate.py` | 30 | 27 |
| `dataview\file_catalog\page_monitor.py` | 29 | 27 |
| `dataview\import_data\page_import_shapefile.py` | 27 | 27 |
| `dataview\import_data\page_import_witsml.py` | 27 | 27 |
| `dataview\file_catalog\file_header_catalog.py` | 26 | 27 |
| `dataview\file_catalog\page_file_workbench.py` | 25 | 27 |
| `dataview\import_data\gom_well_loader.py` | 23 | 27 |
| `dataview\file_catalog\file_header_store.py` | 22 | 27 |
| `dataview\core\fk_resolve_panel.py` | 21 | 27 |
| `dataview\mapping\dv_spatial_loader.py` | 21 | 27 |
| `dataview\import_data\page_import_rrc.py` | 21 | 27 |
| `dataview\import_data\file_gate.py` | 20 | 20 |
| `dataview\import_data\page_import_rrc_shp.py` | 19 | 27 |
| `dataview\migration\page_migrate.py` | 19 | 14 |
| `dataview\file_catalog\page_extraction_inspector.py` | 18 | 27 |
| `dataview\import_data\load_diagnostics.py` | 17 | 22 |
| `dataview\reference_tables\page_standards_manager.py` | 17 | 24 |
| `dataview\mapping\page_ppdm_map.py` | 17 | 27 |
| `dataview\import_data\pdf_field_review.py` | 16 | 20 |
| `dataview\mapping\h3_grids.py` | 16 | 13 |
| `dataview\import_data\entity_seeder.py` | 16 | 27 |
| `tools\repair_promote_functions.py` | 16 | 27 |
| `dataview\file_catalog\doc_catalog_store.py` | 16 | 27 |
| `dataview\file_catalog\gom_dir_srvy_loader.py` | 15 | 27 |
| `dataview\file_catalog\dv_catalog_adapter.py` | 15 | 27 |
| `dataview\core\fk_resolution.py` | 15 | 27 |
| `docshape\store.py` | 14 | 11 |
| `dataview\file_catalog\page_selected_documents.py` | 14 | 27 |
| `documents\page_selected_documents.py` | 14 | 27 |
| `dataview\reference_tables\dv_standards_seed.py` | 13 | 27 |
| `dataview\file_catalog\page_catalog_search.py` | 13 | 27 |
| `dataview\import_data\entity_map_seed.py` | 12 | 27 |
| `dataview\file_catalog\audit_log.py` | 12 | 27 |
| `dataview\file_catalog\work_queue.py` | 12 | 27 |
| `dataview\reference_tables\ref_seeder.py` | 12 | 27 |
| `dataview\core\ppdm_agent.py` | 12 | 27 |
| `dataview\import_data\staging_repair.py` | 10 | 22 |
| `dataview\import_data\page_import_gom_dir_srvy.py` | 10 | 27 |
| `dataview\import_data\load_router.py` | 10 | 21 |
| `docshape\propose.py` | 9 | 10 |
| `dataview\import_data\page_import_gom.py` | 9 | 27 |
| `dataview\mapping\geography_layers.py` | 9 | 9 |
| `dataview\reference_tables\page_seed.py` | 9 | 27 |
| `dataview\mapping\federation_map.py` | 9 | 27 |
| `dataview\file_catalog\catalog_docs.py` | 9 | 27 |
| `dataview\import_data\staging_qa.py` | 8 | 17 |
| `tools\analyze_dead_files.py` | 8 | 27 |
| `dataview\core\licence.py` | 8 | 27 |
| `dataview\file_catalog\inv_email.py` | 8 | 27 |
| `tools\walk_bulk.py` | 8 | 27 |
| `dataview\core\dw_utils.py` | 8 | 27 |
| `tools\walk_and_load.py` | 8 | 27 |
| `dataview\mapping\us_geo.py` | 8 | 27 |
| `dataview\core\fk_catalog.py` | 7 | 27 |
| `compare_extractors.py` | 7 | 17 |
| `dataview\core\page_licence.py` | 7 | 27 |
| `dataview\core\catalog_dialect.py` | 7 | 27 |
| `docshape\backends\snowflake.py` | 7 | 11 |
| `dataview\mapping\located_documents.py` | 6 | 1 |
| `docshape\packs\legal.py` | 6 | 11 |
| `docshape\backends\mssql.py` | 6 | 11 |
| `tools\load_ks_header_to_gold.py` | 6 | 27 |
| `tools\walk_petroleum.py` | 6 | 27 |
| `dataview\core\config.py` | 6 | 27 |
| `docshape\backends\base.py` | 6 | 11 |
| `dataview\file_catalog\add_segy_fastpath.py` | 6 | 27 |
| `docshape\backends\oracle.py` | 6 | 11 |
| `dataview\import_data\export_bcp.py` | 5 | 27 |
| `dataview\reference_tables\value_standardize.py` | 5 | 27 |
| `dataview\db_explorer\page_schema_overview.py` | 5 | 27 |
| `tools\gold_rebuild.py` | 5 | 27 |
| `tools\walk_fast.py` | 5 | 27 |
| `tools\delete_ks_by_kid.py` | 5 | 27 |
| `tools\ks_coord_fill_fast.py` | 5 | 27 |
| `tools\cleanup_tier4_orphans.py` | 5 | 27 |
| `dataview\import_data\pipeline_batch_ui.py` | 4 | 27 |
| `dataview\file_catalog\inv_auth.py` | 4 | 27 |
| `tools\ks_coord_fill.py` | 4 | 27 |
| `dataview\file_catalog\merge_bcp_capture.py` | 4 | 27 |
| `dataview\region_builder\petroleum_regions.py` | 4 | 27 |
| `tools\cleanup_tier2_junk.py` | 4 | 27 |
| `dataview\region_builder\state_regions.py` | 4 | 27 |
| `tools\reconcile_dupes_tier1.py` | 4 | 27 |
| `tools\delete_ks_no_docs.py` | 4 | 27 |
| `tools\analyze_tier4_orphans.py` | 4 | 27 |
| `tools\seed_refs.py` | 4 | 27 |
| `dataview\import_data\file_catalog_load_to_db_patch.py` | 4 | 18 |
| `docshape\backends\duck.py` | 3 | 11 |
| `tools\docs_per_well.py` | 3 | 27 |
| `dataview\reference_tables\boem_status_codes.py` | 3 | 27 |
| `tools\seed_uom.py` | 3 | 27 |
| `tools\delete_ks_uwi15.py` | 3 | 27 |
| `dataview\file_catalog\probe_capture.py` | 3 | 18 |
| `probe_capture.py` | 3 | 18 |
| `tools\kgs_las.py` | 3 | 27 |
| `sql\check_well.py` | 3 | 27 |
| `tools\finish_gold_insert.py` | 3 | 27 |
| `tools\bcp_probe.py` | 3 | 27 |
| `tools\index_well_master.py` | 3 | 27 |
| `tools\diff_dupe_copies.py` | 3 | 27 |
| `tools\ks_in_gold.py` | 3 | 27 |
| `tools\seed_refs2.py` | 3 | 27 |
| `dataview\reference_tables\boem_area_codes.py` | 3 | 27 |
| `dataview\file_catalog\extract_matched_wells.py` | 3 | 27 |
| `tools\cleanup_tier3_versions.py` | 3 | 27 |
| `tools\tidy_scratch.py` | 2 | 27 |
| `tools\who_locks.py` | 2 | 27 |
| `tools\profile_files.py` | 2 | 27 |
| `dataview\file_catalog\ensure_catalog_columns.py` | 2 | 27 |
| `tools\gold_fix_uwi14.py` | 2 | 27 |
| `tools\clear_stuck_state.py` | 2 | 27 |
| `tools\recover.py` | 2 | 27 |
| `sql\rebuild_db.py` | 2 | 27 |
| `tools\rebuild_db.py` | 2 | 27 |
| `tools\walk_test.py` | 2 | 27 |
| `dataview\file_catalog\promote_timing.py` | 2 | 27 |
| `tools\clear_seismic.py` | 2 | 27 |
| `docshape\backends\__init__.py` | 2 | 11 |
| `tools\missing_files.py` | 2 | 27 |
| `tools\ks_gold_uwi14_now.py` | 2 | 27 |
| `tools\slow_files.py` | 2 | 27 |
| `dataview\import_data\run_stage.py` | 2 | 27 |
| `tools\kill_all_py.py` | 2 | 27 |
| `tools\reconcile.py` | 2 | 27 |
| `tools\ks_in_gold_fast.py` | 2 | 27 |
| `tools\cleanup_now.py` | 2 | 27 |
| `tools\gold_key_check.py` | 1 | 27 |
| `dataview\file_catalog\vault_run.py` | 1 | 27 |
| `dataview\import_data\run_promote.py` | 1 | 27 |
| `tools\kill_pid.py` | 1 | 27 |
| `dataview\db_explorer\page_data_model.py` | 1 | 27 |
| `dataview\core\ui_helpers.py` | 1 | 27 |
| `tools\debug_survey.py` | 1 | 27 |
| `tools\skip_detail.py` | 1 | 27 |
| `tools\breakdown.py` | 0 | 27 |
| `run_load_assistant.py` | 0 | 8 |
| `dataview\tools\database_scorecard.py` | 0 | 1 |
| `dataview\db_explorer\__init__.py` | 0 | 27 |
| `dataview\import_data\page_ai_importer.py` | 0 | 27 |
| `dataview\migration\__init__.py` | 0 | 14 |
| `dataview\region_builder\__init__.py` | 0 | 27 |
| `documents\__init__.py` | 0 | 27 |
| `tools\__init__.py` | 0 | 27 |

## SCRIPT — runnable, but nothing imports them

One-off tools, probes and maintenance jobs. These are NOT dead by
default — decide one at a time, and consider moving the keepers to
a `scripts/` folder so the package tree holds only package code.

| file | KB | days since change |
|---|---:|---:|
| `dataview\import_data\dvpath\dataview\import_data\bulk_dir_loader.py` | 249 | 6 |
| `dataview\import_data\bdl_dir3.py` | 248 | 6 |
| `dataview\import_data\promote.py` | 100 | 27 |
| `dataview\import_data\pipeline_run - Copy.py` | 98 | 13 |
| `dataview\import_data\page_dir_loader.py` | 86 | 17 |
| `dataview\file_catalog\doc_flow.py` | 77 | 2 |
| `dataview\core\fk_entity.py` | 62 | 27 |
| `tools\seed_catalog.py` | 53 | 27 |
| `dataview\migration\synth_docs.py` | 48 | 13 |
| `dataview\file_catalog\table_shapes.py` | 47 | 12 |
| `ppdm_promote.py` | 44 | 14 |
| `dataview\import_data\staging.py` | 42 | 27 |
| `tools\generate_dataview_testdata.py` | 41 | 27 |
| `dataview\mapping\well_path.py` | 40 | 4 |
| `tools\generate_dataview_schema.py` | 36 | 27 |
| `dataview\migration\promote_ppdm.py` | 36 | 14 |
| `dataview\file_catalog\promote_ppdm.py` | 34 | 14 |
| `tools\seed_political.py` | 32 | 27 |
| `dataview\file_catalog\doc_store.py` | 32 | 12 |
| `dataview\import_data\pdf_document_loader.py` | 31 | 17 |
| `dataview\migration\synth_data.py` | 31 | 13 |
| `dataview\file_catalog\doc_assess.py` | 31 | 2 |
| `dataview\core\setup_database.py` | 30 | 27 |
| `sql\setup_database.py` | 30 | 27 |
| `dataview\import_data\mapping.py` | 30 | 27 |
| `dataview\file_catalog\format_library.py` | 29 | 27 |
| `tools\load_kgs.py` | 28 | 27 |
| `tools\diagnose_transport.py` | 26 | 27 |
| `dataview\file_catalog\extract_dump.py` | 24 | 12 |
| `tools\gen_synthetic_completions.py` | 24 | 27 |
| `tools\make_test_dataset.py` | 22 | 27 |
| `sql\setup_dataview.py` | 21 | 27 |
| `tools\setup_dataview.py` | 21 | 27 |
| `dataview\core\schema.py` | 20 | 27 |
| `tools\well_report.py` | 19 | 1 |
| `tools\load_well_master.py` | 19 | 27 |
| `tools\build_schema_domain.py` | 18 | 27 |
| `tools\load_survey_pdfs.py` | 18 | 27 |
| `dataview\import_data\docx_document_loader.py` | 18 | 23 |
| `tools\build_fk_catalog.py` | 17 | 27 |
| `tools\gen_schema_docs.py` | 17 | 22 |
| `dataview\mapping\well_path_sql.py` | 16 | 4 |
| `dataview\migration\ppdm_model.py` | 16 | 14 |
| `dataview\tools\scorecard.py` | 16 | 1 |
| `codebase_census.py` | 15 | 0 |
| `dataview\file_catalog\page_las.py` | 15 | 27 |
| `tools\seed_references.py` | 15 | 27 |
| `dataview\mapping\populate_dv_well_protraction_area.py` | 15 | 27 |
| `tools\parallel_crawl.py` | 15 | 27 |
| `tools\load_rrc_maf016.py` | 15 | 27 |
| `tools\load_nd_gdb.py` | 14 | 27 |
| `tools\make_test_dataset_all.py` | 14 | 27 |
| `dataview\import_data\lis_header_loader.py` | 14 | 20 |
| `dataview\migration\synonyms.py` | 14 | 14 |
| `dataview\import_data\dlis_header_loader.py` | 14 | 20 |
| `dataview\import_data\load_ledger.py` | 13 | 3 |
| `dataview\import_data\pipeline_profiler.py` | 13 | 27 |
| `dataview\migration\db_source.py` | 13 | 14 |
| `tools\validate_v_well.py` | 13 | 27 |
| `header_probe.py` | 13 | 0 |
| `sql\schema_sync.py` | 13 | 27 |
| `sql\clone_schema.py` | 13 | 27 |
| `tools\clone_schema.py` | 13 | 27 |
| `db_scorecard.py` | 13 | 22 |
| `tools\bulk_runner.py` | 13 | 27 |
| `dataview\file_catalog\vocab_check.py` | 13 | 4 |
| `tools\generate_snapshot.py` | 13 | 27 |
| `tools\load_rrc_w1_permits.py` | 13 | 27 |
| `dataview\core\delete_util.py` | 12 | 27 |
| `tools\generate_core_images.py` | 12 | 27 |
| `tools\sync_schema.py` | 12 | 20 |
| `tools\generate_osdu_wells.py` | 12 | 27 |
| `tools\make_well_shapefile.py` | 12 | 27 |
| `sql\setup_wranglerview.py` | 12 | 27 |
| `tools\setup_wranglerview.py` | 12 | 27 |
| `dataview\file_catalog\enrich_from_dbf.py` | 11 | 27 |
| `dead_code.py` | 11 | 21 |
| `dataview\file_catalog\extract_probe.py` | 11 | 13 |
| `tools\ingest_to_snowflake_fast.py` | 11 | 27 |
| `dataview\region_builder\migrate_petroleum_regions.py` | 10 | 27 |
| `tools\migrate_petroleum_regions.py` | 10 | 27 |
| `sql\build_catalog_mirror.py` | 10 | 27 |
| `dataview\region_builder\migrate_state_regions.py` | 10 | 27 |
| `tools\migrate_state_regions.py` | 10 | 27 |
| `tools\build_fixtures.py` | 10 | 23 |
| `dataview\mapping\project_map.py` | 10 | 27 |
| `tools\dev_resume.py` | 10 | 27 |
| `dataview\file_catalog\worker_pool.py` | 10 | 27 |
| `shapefile_to_geography.py` | 10 | 17 |
| `dataview\import_data\prep_rrc_texas.py` | 10 | 27 |
| `dataview\tools\purge_source.py` | 10 | 3 |
| `tools\purge_source.py` | 10 | 3 |
| `tools\load_ok_csv.py` | 10 | 27 |
| `sql\validate_v_gom_well_attributes.py` | 10 | 27 |
| `tools\gen_schema_catalog.py` | 9 | 26 |
| `dataview\reference_tables\standardize_well_attrs.py` | 9 | 27 |
| `tools\recatalog_seis.py` | 9 | 27 |
| `dataview\migration\column_rules.py` | 9 | 14 |
| `dataview\file_catalog\resolve_log_identity.py` | 9 | 27 |
| `tools\clear_catalog.py` | 9 | 27 |
| `tools\generate_county_boundaries.py` | 9 | 27 |
| `dataview\mapping\build_geojson_from_snowflake.py` | 9 | 27 |
| `capture_probe.py` | 9 | 1 |
| `dataview\mapping\spatial_seeder.py` | 8 | 27 |
| `dataview\mapping\boem_geo.py` | 8 | 27 |
| `extend_synonyms_round4.py` | 8 | 6 |
| `tools\deploy_federation.py` | 8 | 27 |
| `tools\ingest_to_snowflake.py` | 8 | 27 |
| `extend_synonyms.py` | 8 | 6 |
| `extend_synonyms_v2.py` | 8 | 6 |
| `tools\copy_reference_data.py` | 7 | 27 |
| `extend_synonyms_round3.py` | 7 | 6 |
| `tools\generate_licence.py` | 7 | 27 |
| `tools\validate_h3_views.py` | 7 | 27 |
| `dataview\import_data\pipeline_proc_runner.py` | 7 | -0 |
| `sql\build_well_documents_view.py` | 7 | 27 |
| `dataview\mapping\refresh_demo_grids.py` | 7 | 27 |
| `tools\load_well_header_csv.py` | 7 | 27 |
| `dataview\mapping\run_h3.py` | 7 | 27 |
| `run_h3.py` | 7 | 27 |
| `dataview\import_data\witsml_header_loader.py` | 7 | 25 |
| `dataview\file_catalog\bcp_capture_bench.py` | 7 | 27 |
| `segy_lines_to_wgs84.py` | 7 | 9 |
| `support\docs\load_core_photos.py` | 7 | 27 |
| `tools\load_core_photos.py` | 7 | 27 |
| `tools\gen_scout_tickets.py` | 6 | 25 |
| `tools\seed_las_catalog.py` | 6 | 27 |
| `tools\seed_queue.py` | 6 | 27 |
| `tools\poc_seis3d_geom.py` | 6 | 27 |
| `tools\provenance_audit.py` | 6 | 27 |
| `tools\validate_h3_backfill.py` | 6 | 27 |
| `sql\load_preflight.py` | 6 | 27 |
| `dataview\mapping\populate_h3.py` | 6 | 27 |
| `tools\las_triage.py` | 5 | 27 |
| `dataview\import_data\upload_to_snowflake.py` | 5 | 27 |
| `dataview\import_data\export_for_snowflake.py` | 5 | 27 |
| `tools\copy_views.py` | 5 | 27 |
| `dataview\import_data\page_pipeline_tools.py` | 5 | 27 |
| `tools\seed_deep_refs.py` | 5 | 27 |
| `xy_to_latlong.py` | 5 | 9 |
| `tools\las_report_all.py` | 5 | 27 |
| `tools\classify_dir.py` | 5 | 27 |
| `dataview\file_catalog\show_headers.py` | 5 | 2 |
| `probe_seismic_pill.py` | 4 | 9 |
| `docshape\__main__.py` | 4 | 11 |
| `sql\clone_db.py` | 4 | 27 |
| `tools\clone_db.py` | 4 | 27 |
| `tools\bench_capture_parallel.py` | 4 | 27 |
| `dataview\core\hash_keys.py` | 4 | 27 |
| `install_synonyms_v3.py` | 4 | 6 |
| `install_synonyms_v2.py` | 4 | 6 |
| `install_synonyms.py` | 4 | 6 |
| `tools\las_report.py` | 4 | 27 |
| `tools\deploy_deep_fixes.py` | 4 | 27 |
| `tools\bench_capture.py` | 4 | 27 |
| `tools\gen_relax_ddl.py` | 4 | 23 |
| `tools\las_scan.py` | 4 | 27 |
| `dataview\import_data\pdf_probe.py` | 3 | 20 |
| `tools\profile_capture.py` | 3 | 27 |
| `dataview\file_catalog\extract_by_list.py` | 3 | 27 |
| `tools\run_fixture_triage.py` | 3 | 27 |
| `find_dt.py` | 3 | 16 |
| `tools\compare_extract.py` | 3 | 27 |
| `tools\bench_profile.py` | 3 | 27 |
| `dataview\file_catalog\run.py` | 3 | 27 |
| `tools\deploy.py` | 2 | 27 |
| `dataview\file_catalog\force_capture.py` | 2 | 27 |
| `tools\recapture.py` | 1 | 27 |
| `tools\copy_n_files.py` | 1 | 27 |
| `dataview\import_data\repromote.py` | 1 | 27 |

## Same filename in more than one place

Usually an archived copy. Two files with one name is how a fix
lands in the copy nobody runs.

- **build_catalog_mirror.py**
    - `dataview\file_catalog\build_catalog_mirror.py` (11 KB) · LIVE
    - `sql\build_catalog_mirror.py` (10 KB)
- **bulk_dir_loader.py**
    - `dataview\import_data\bulk_dir_loader.py` (39 KB) · LIVE
    - `dataview\import_data\dvpath\dataview\import_data\bulk_dir_loader.py` (249 KB)
- **clear_catalog.py**
    - `dataview\file_catalog\clear_catalog.py` (15 KB) · LIVE
    - `tools\clear_catalog.py` (9 KB)
- **clone_db.py**
    - `sql\clone_db.py` (4 KB)
    - `tools\clone_db.py` (4 KB)
- **clone_schema.py**
    - `sql\clone_schema.py` (13 KB)
    - `tools\clone_schema.py` (13 KB)
- **file_viewer.py**
    - `dataview\file_catalog\file_viewer.py` (32 KB) · LIVE
    - `documents\file_viewer.py` (32 KB)
- **load_core_photos.py**
    - `support\docs\load_core_photos.py` (7 KB)
    - `tools\load_core_photos.py` (7 KB)
- **migrate_petroleum_regions.py**
    - `dataview\region_builder\migrate_petroleum_regions.py` (10 KB)
    - `tools\migrate_petroleum_regions.py` (10 KB)
- **migrate_state_regions.py**
    - `dataview\region_builder\migrate_state_regions.py` (10 KB)
    - `tools\migrate_state_regions.py` (10 KB)
- **page_selected_documents.py**
    - `dataview\file_catalog\page_selected_documents.py` (14 KB)
    - `documents\page_selected_documents.py` (14 KB)
- **probe_capture.py**
    - `dataview\file_catalog\probe_capture.py` (3 KB)
    - `probe_capture.py` (3 KB)
- **promote_ppdm.py**
    - `dataview\file_catalog\promote_ppdm.py` (34 KB)
    - `dataview\migration\promote_ppdm.py` (36 KB)
- **purge_source.py**
    - `dataview\tools\purge_source.py` (10 KB)
    - `tools\purge_source.py` (10 KB)
- **rebuild_db.py**
    - `sql\rebuild_db.py` (2 KB)
    - `tools\rebuild_db.py` (2 KB)
- **run_h3.py**
    - `dataview\mapping\run_h3.py` (7 KB)
    - `run_h3.py` (7 KB)
- **setup_database.py**
    - `dataview\core\setup_database.py` (30 KB)
    - `sql\setup_database.py` (30 KB)
- **setup_dataview.py**
    - `sql\setup_dataview.py` (21 KB)
    - `tools\setup_dataview.py` (21 KB)
- **setup_wranglerview.py**
    - `sql\setup_wranglerview.py` (12 KB)
    - `tools\setup_wranglerview.py` (12 KB)
- **well_report.py**
    - `dataview\tools\well_report.py` (23 KB) · LIVE
    - `tools\well_report.py` (19 KB)

## LIVE — reachable, and what pulls each one in

A module imported by exactly one other is a candidate for merging;
one imported by many is load-bearing and worth reading before any
change.

| module | KB | imported by |
|---|---:|---:|
| `dataview\file_catalog\page_workbench.py` | 315 | 6 |
| `dataview\import_data\pipeline_run.py` | 113 | 11 |
| `dataview\file_catalog\promote_catalog.py` | 90 | 7 |
| `dataview\file_catalog\pdf_survey_catalog.py` | 71 | 8 |
| `dataview\file_catalog\file_summarizer.py` | 66 | 10 |
| `dataview\file_catalog\extract_core.py` | 60 | 5 |
| `dataview\file_catalog\worker_core.py` | 57 | 19 |
| `dataview\core\db_dialect.py` | 44 | 2 |
| `dataview\import_data\bulk_dir_loader.py` | 39 | 17 |
| `dataview\file_catalog\shape_loader.py` | 39 | 1 |
| `docshape\readers\tables.py` | 37 | 5 |
| `dataview\mapping\shapefile_catalog.py` | 36 | 6 |
| `dataview\file_catalog\file_viewer.py` | 32 | 6 |
| `dataview\file_catalog\pdf_db_loader.py` | 32 | 3 |
| `dataview\file_catalog\enrich_file_headers.py` | 31 | 6 |
| `dataview\file_catalog\dv_office_loader.py` | 29 | 2 |
| `dataview\file_catalog\page_triage.py` | 28 | 2 |
| `dataview\file_catalog\triage_inventory.py` | 27 | 5 |
| `dataview\file_catalog\catalog_scorecard.py` | 27 | 1 |
| `dataview\import_data\normalize.py` | 26 | 4 |
| `dataview\file_catalog\bcp_capture.py` | 26 | 5 |
| `dataview\file_catalog\vault_copy.py` | 25 | 2 |
| `dataview\core\schema_introspect.py` | 24 | 13 |
| `dataview\file_catalog\p190_catalog.py` | 24 | 8 |
| `dataview\tools\well_report.py` | 23 | 3 |
| `dataview\file_catalog\json_well_log_catalog.py` | 23 | 5 |
| `selftest.py` | 22 | ENTRY |
| `dataview\core\db.py` | 21 | 16 |
| `dataview\file_catalog\vault_organizer.py` | 21 | 3 |
| `dataview\reference_tables\user_rules.py` | 20 | 3 |
| `dataview\file_catalog\extract_petro.py` | 18 | 2 |
| `dataview\core\demo_reset.py` | 18 | 2 |
| `dataview\file_catalog\promote_fk_review.py` | 17 | 1 |
| `dataview\file_catalog\promotion_lineage.py` | 17 | 4 |
| `dataview\file_catalog\clear_catalog.py` | 15 | 1 |
| `dataview\file_catalog\catalog_capture.py` | 14 | 13 |
| `dataview\file_catalog\segy_header.py` | 14 | 4 |
| `dataview\file_catalog\witsml_catalog.py` | 12 | 5 |
| `dataview\file_catalog\build_catalog_mirror.py` | 11 | 5 |
| `dataview\file_catalog\collect_final_documents.py` | 11 | 2 |
| `docshape\engine\recognise.py` | 11 | 13 |
| `dataview\file_catalog\lis_catalog.py` | 10 | 4 |
| `docshape\packs\overlay.py` | 10 | 7 |
| `dataview\file_catalog\catalog_doc_capture.py` | 10 | 3 |
| `dataview\file_catalog\scout_pdf_reader.py` | 10 | 1 |
| `docshape\readers\segy.py` | 10 | 2 |
| `dataview\file_catalog\catalog_readiness.py` | 9 | 1 |
| `dataview\file_catalog\survey_loader.py` | 9 | 1 |
| `dataview\import_data\las_header_loader.py` | 8 | 4 |
| `dataview\file_catalog\page_run.py` | 8 | 1 |
| `dataview\file_catalog\csv_catalog.py` | 7 | 1 |
| `dataview\file_catalog\crs_from_segy.py` | 7 | 2 |
| `docshape\packs\__init__.py` | 7 | 10 |
| `docshape\readers\las.py` | 7 | 2 |
| `dataview\file_catalog\current_run_scorecard.py` | 6 | 1 |
| `dataview\core\path_identity.py` | 5 | 4 |
| `dataview\file_catalog\page_vault.py` | 4 | 1 |
| `dataview\core\fingerprint.py` | 4 | 5 |
| `docshape\readers\__init__.py` | 4 | 7 |
| `dataview\file_catalog\seis_filename_parser.py` | 3 | 5 |
| `docshape\__init__.py` | 1 | 3 |
| `dataview\core\db_pool.py` | 1 | 6 |
| `docshape\engine\__init__.py` | 0 | — |
| `dataview\mapping\__init__.py` | 0 | 8 |
| `dataview\import_data\__init__.py` | 0 | 28 |
| `dataview\tools\__init__.py` | 0 | — |
| `dataview\__init__.py` | 0 | — |
| `dataview\core\__init__.py` | 0 | 7 |
| `dataview\reference_tables\__init__.py` | 0 | 1 |
| `dataview\file_catalog\__init__.py` | 0 | 41 |
