# Dead sections — C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\data_wrangler\data_wrangler_v4

4,579 functions · 4,098 reachable from app_v4.py, selftest.py and module-level code

## CERTAIN — unreachable AND never named — 160, 5,087 lines

Both tests agree: no path from an entry point reaches these, and
their names appear nowhere else in any file. Largest first — the
big ones are where a rewrite left a whole cone behind.

| lines | where | function |
|---:|---|---|
| 436 | `dataview\file_catalog\page_workbench.py:5894` | `_pipeline_stages` |
| 223 | `dataview\core\fk.py:1375` | `auto_seed_fk_oracle` |
| 203 | `dataview\file_catalog\page_file_manager.py:854` | `_tab_assign` |
| 157 | `dataview\file_catalog\page_workbench.py:4346` | `_tab_headers` |
| 151 | `dataview\file_catalog\page_file_manager.py:3566` | `_tab_office_extract` |
| 116 | `dataview\core\fk_resolution.py:183` | `render_reconciliation` |
| 115 | `dataview\file_catalog\dlis_catalog.py:375` | `fast_dlis_meta` |
| 97 | `tools\seed_catalog.py:1189` | `validate_catalog` |
| 90 | `dataview\core\delete_util.py:73` | `build_delete_plan` |
| 89 | `dataview\file_catalog\dv_catalog_adapter.py:264` | `auto_assign_next_batch` |
| 80 | `dataview\import_data\page_dir_loader.py:701` | `_colmap_stage` |
| 79 | `dataview\file_catalog\las_catalog.py:951` | `export_files` |
| 78 | `dataview\core\fk.py:993` | `insert_missing_parent_rows` |
| 77 | `dataview\file_catalog\catalog_rules.py:786` | `bootstrap_well` |
| 77 | `dataview\file_catalog\file_inventory.py:309` | `_scan_file` |
| 77 | `dataview\import_data\page_dir_loader.py:835` | `_fk_stage` |
| 74 | `dataview\file_catalog\catalog_rules.py:867` | `extract_and_score_inventory` |
| 74 | `dataview\file_catalog\las_catalog.py:1066` | `get_catalog_summary` |
| 73 | `dataview\file_catalog\dlis_catalog.py:1246` | `catalog_dlis_directory` |
| 73 | `dataview\file_catalog\las_catalog.py:627` | `catalog_directory` |
| 73 | `dataview\file_catalog\segy_catalog.py:855` | `catalog_segy_directory` |
| 67 | `dataview\file_catalog\dlis_catalog.py:1321` | `catalog_lis_directory` |
| 64 | `dataview\file_catalog\file_inventory_governance.py:496` | `create_group_and_assign` |
| 62 | `dataview\file_catalog\las_catalog.py:288` | `create_well_from_las` |
| 60 | `dataview\file_catalog\doc_catalog_store.py:340` | `render_catalog_widget` |
| 60 | `dataview\file_catalog\file_inventory.py:953` | `start_background_scoring` |
| 53 | `dataview\file_catalog\las_catalog.py:160` | `ensure_catalog_schema` |
| 51 | `dataview\mapping\mapping_studio.py:857` | `build_table_spec` |
| 48 | `dataview\file_catalog\file_header_catalog.py:498` | `get_catalog_headers` |
| 48 | `dataview\file_catalog\file_header_store.py:183` | `store_dlis_headers` |
| 48 | `dataview\mapping\page_well_map.py:1192` | `_qry_gom_status_codes` |
| 45 | `dataview\core\licence.py:151` | `get_licence_status` |
| 45 | `dataview\import_data\page_dir_loader.py:523` | `build_load_bundle` |
| 44 | `ppdm\ppdm_promote.py:329` | `add_reference_values` |
| 43 | `dataview\file_catalog\catalog_rules.py:275` | `extract_segy_fields` |
| 38 | `dataview\core\fk.py:29` | `build_fk_dependency_graph` |
| 38 | `dataview\file_catalog\worker_core.py:1005` | `_docx_uwi` |
| 38 | `dataview\import_data\page_dir_loader.py:783` | `_validate_stage` |
| 38 | `dataview\mapping\mapping_studio.py:910` | `save_mapping` |
| 37 | `dataview\file_catalog\file_header_store.py:233` | `store_lis_headers` |

## REVIEW — unreachable, but the name IS used — 282, 6,404 lines

The call graph could not reach these, but something names them.
Either the graph missed a link — a module alias, a method call, a
Streamlit page invoked as `mod.run(engine)` — or the only callers
are themselves dead. Read before believing.

| lines | where | function |
|---:|---|---|
| 313 | `dataview\core\fk_entity.py:986` | `insert_entity_rows` |
| 284 | `dataview\mapping\dv_table_loader.py:504` | `load_table` |
| 186 | `dataview\file_catalog\dlis_catalog.py:933` | `_catalog_dlis_from_header` |
| 180 | `dataview\file_catalog\las_catalog.py:445` | `catalog_file` |
| 166 | `tools\seed_catalog.py:476` | `seed_table` |
| 154 | `dataview\file_catalog\las_catalog.py:706` | `search_catalog` |
| 127 | `shapefile_to_geography.py:116` | `load_shapefile_geography` |
| 124 | `dataview\file_catalog\catalog_rules.py:122` | `extract_las_fields` |
| 121 | `tools\seed_catalog.py:827` | `_build_openrowset_tsql` |
| 113 | `tools\seed_catalog.py:950` | `seed_all_server` |
| 105 | `dataview\file_catalog\witsml_catalog.py:189` | `load_witsml` |
| 99 | `dataview\file_catalog\dlis_catalog.py:1121` | `_catalog_lis_from_header` |
| 98 | `dataview\file_catalog\page_workbench.py:4505` | `_load_seis` |
| 89 | `tools\seed_catalog.py:1068` | `sort_entries_by_fk` |
| 87 | `dataview\file_catalog\csv_catalog.py:91` | `classify_csv` |
| 85 | `dataview\core\fk_entity.py:1302` | `_insert_node_rows` |
| 85 | `dataview\file_catalog\las_catalog.py:353` | `parse_las_header` |
| 84 | `dataview\core\fk_entity.py:779` | `build_entity_mapping` |
| 84 | `dataview\import_data\staging.py:809` | `ingest_from_path` |
| 79 | `dataview\mapping\mapping_studio.py:1080` | `scan_directory` |
| 65 | `dataview\import_data\synonym_store.py:299` | `refresh_attributes` |
| 65 | `synonyms\synonym_store_v4.py:299` | `refresh_attributes` |
| 62 | `dataview\core\fk.py:587` | `load_fk_samples` |
| 60 | `dataview\core\db_dialect.py:318` | `normalize_sql` |
| 60 | `dataview\core\fk_entity.py:717` | `get_entity_table_cols` |

## Unreached, but the NAME appears as a string — 39

Could be dict-dispatched or passed as a callback. Check before removing.

- `dataview\file_catalog\survey_loader.py:126` — `load_directional_survey` (72 lines)
- `tools\parallel_crawl.py:53` — `crawl` (47 lines)
- `docshape\packs\overlay.py:161` — `__init__` (22 lines)
- `dataview\file_catalog\pdf_db_loader.py:298` — `load_directional_survey` (17 lines)
- `dataview\mapping\mapping_studio.py:566` — `primary_key` (17 lines)
- `tools\generate_dataview_schema.py:127` — `__init__` (12 lines)
- `docshape\backends\mssql.py:37` — `__init__` (11 lines)
- `docshape\readers\tables.py:623` — `__init__` (11 lines)
- `tools\generate_dataview_schema.py:91` — `__init__` (11 lines)
- `dataview\reference_tables\boem_area_codes.py:86` — `area_name` (10 lines)
- `dataview\file_catalog\shape_loader.py:540` — `__init__` (9 lines)
- `dataview\mapping\mapping_studio.py:337` — `filename_clue` (9 lines)
- `docshape\backends\snowflake.py:45` — `__init__` (9 lines)
- `docshape\backends\oracle.py:46` — `__init__` (8 lines)
- `dataview\core\fk_entity.py:55` — `__init__` (7 lines)

## Unused imports

405 across 233 file(s). Individually trivial; together they are the fossil record of what a file used to do.

- `dataview\import_data\page_pipeline.py` — json, dataview.core.ui_helpers.pill, dataview.core.schema.load_schema_from_string, dataview.import_data.mapping.build_transform_sql, dataview.import_data.mapping.serialise_mapping, dataview.import_data.mapping.restore_mapping, dataview.import_data.mapping.save_entity_mapping, dataview.import_data.mapping.restore_entity_mapping …
- `dataview\file_catalog\page_file_manager.py` — dataview.file_catalog.file_inventory_governance.has_any_user, dataview.file_catalog.file_inventory_governance.authenticate_user, dataview.file_catalog.audit_log.audit_assign, dataview.file_catalog.audit_log.audit_reassign, dataview.file_catalog.audit_log.audit_remove_assign, dataview.file_catalog.audit_log.audit_skip, dataview.file_catalog.audit_log.audit_crawl, dataview.file_catalog.audit_log.audit_clear …
- `docshape\backends\__init__.py` — __future__.annotations, docshape.backends.base.Backend, docshape.backends.base.TEXT, docshape.backends.base.TEXT_LONG, docshape.backends.base.NUMBER, docshape.backends.base.INT, docshape.backends.base.BIGINT, docshape.backends.base.TIMESTAMP …
- `dataview\file_catalog\page_dv_catalog.py` — __future__.annotations, threading, dataview.file_catalog.file_inventory.ensure_inventory_schema, dataview.file_catalog.file_inventory.crawl_paths, dataview.file_catalog.las_catalog.parse_las_header, dataview.file_catalog.las_catalog.get_file_curves, dataview.file_catalog.dlis_catalog.parse_dlis_header, matplotlib.pyplot
- `dataview\file_catalog\file_header_store.py` — __future__.annotations, dataview.core.catalog_dialect.now_expr, dataview.core.catalog_dialect.varchar, dataview.core.catalog_dialect.timestamp_type, dataview.core.catalog_dialect.timestamp_default, dataview.core.catalog_dialect.if_not_exists_table, dataview.core.catalog_dialect.select_top
- `dataview\file_catalog\inv_workbench.py` — dataview.file_catalog.las_catalog.parse_las_header, dataview.file_catalog.las_catalog.catalog_file, dataview.file_catalog.dlis_catalog.catalog_dlis_file, dataview.file_catalog.dlis_catalog.catalog_lis_file, dataview.file_catalog.segy_catalog.catalog_segy_file, dataview.file_catalog.p190_catalog.catalog_p190_file
- `docshape\__init__.py` — docshape.engine.recognise.Recogniser, docshape.engine.recognise.to_number, docshape.engine.recognise.INTERNAL_KEYS, docshape.packs.load, docshape.packs.validate, docshape.packs.available
- `tools\load_survey_pdfs.py` — __future__.annotations, os, re, uuid, pathlib.Path, pdfplumber
- `dataview\core\fk_entity.py` — __future__.annotations, re, dataview.core.fk.check_fk_violations, types.SimpleNamespace
- `dataview\file_catalog\format_library.py` — __future__.annotations, urllib.parse, typing.Any, typing.Generator
- `dataview\import_data\exporters.py` — __future__.annotations, datetime.datetime, datetime.timezone, xlsxwriter
- `dataview\import_data\gom_well_loader.py` — __future__.annotations, datetime.datetime, datetime.datetime, datetime.date

## Statements after a return / raise

- `dataview\mapping\page_well_map_docs.py:989` — unreachable, the block returns at line 985
- `dataview\mapping\page_well_map_docs.py:1593` — unreachable, the block returns at line 1589

## Abandoned session-state keys — 14

Written and never read. In a Streamlit app this is where dead features hide: the flag survives, the code that honoured it does not, and nothing complains.

- `_auto_tray_uwis` — set at `dataview\mapping\page_well_map.py:2610` and 2 other place(s)
- `_fs_elapsed` — set at `dataview\db_explorer\page_federation_search.py:175`
- `ai_filter_sql_where` — set at `dataview\mapping\page_well_map.py:6272`
- `app_mode` — set at `dataview\file_catalog\inv_auth.py:46`
- `catalog_filter_set_at` — set at `dataview\mapping\page_well_map_docs.py:3362`
- `catalog_filter_uwi` — set at `dataview\mapping\page_well_map_docs.py:3361`
- `ds_pending` — set at `dataview\file_catalog\page_docshape.py:517` and 1 other place(s)
- `fp_vault_run` — set at `dataview\file_catalog\page_workbench.py:5577`
- `hx_well_batch` — set at `dataview\file_catalog\page_file_manager.py:3271`
- `las_batch_results` — set at `dataview\file_catalog\page_las.py:368`
- `mapping_grid_tbl` — set at `dataview\import_data\page_pipeline.py:1258`
- `mon_promote_out` — set at `dataview\file_catalog\page_monitor.py:522` and 1 other place(s)
- `mon_vault_out` — set at `dataview\file_catalog\page_monitor.py:563` and 2 other place(s)
- `wb_enrich_offset` — set at `dataview\file_catalog\page_workbench.py:264`

---

**What this cannot find:** unreachable BRANCHES inside live functions. A stage that now always takes one path still carries the other, and no static tool can tell. For that, run the app and the pipeline under `coverage run`, then `coverage html` — the red lines in a live function are the rest of the answer.