"""
dev_resume.py  —  PPDM Loader Dev Shortcut
===========================================
Runs the pipeline up to the Match & Map stage (stage 4) without the UI,
then writes a .dev_resume.pkl file next to app.py.

When you next open/refresh the Streamlit app it will auto-load that state
and drop you straight into the mapping grid.

Usage:
    python tools/dev_resume.py                          # uses defaults below
    python tools/dev_resume.py --file my_wells.csv      # override source file
    python tools/dev_resume.py --table well_bore        # override target table
    python tools/dev_resume.py --stage 3                # stop at normalize (stage 3)

Stages:
    1 = connected
    2 = staged
    3 = normalized
    4 = mapping built  (default)
"""

import argparse
import os
import pickle
import sys
import types


# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Config — edit these defaults to match your environment ──────────────────
DEFAULTS = dict(
    server       = r"PERRY\SQLEXPRESS",
    database     = "PPDM39_DEMO_1",
    driver       = "ODBC Driver 17 for SQL Server",
    windows_auth = True,
    username     = "",
    password     = "",
    source_file  = r"C:\Users\perry\OneDrive\Documents\PPDM\SampleData\Synthetic_Data\well_header\Well_header_test.csv",
    schema_json  = r"C:\Users\perry\OneDrive\Documents\PPDM\claude_ppdm\schema_registry\ppdm_39_schema_domain.json",
    target_table = "well",
    schema_variant = "PPDM 3.9",
    delimiter    = ",",
    encoding     = "utf-8-sig",
    stop_stage   = 4,   # 1=connect, 2=stage, 3=normalize, 4=mapping
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)


def main():
    p = argparse.ArgumentParser(description="PPDM Loader dev resume helper")
    p.add_argument("--server",   default=DEFAULTS["server"])
    p.add_argument("--database", default=DEFAULTS["database"])
    p.add_argument("--file",     default=DEFAULTS["source_file"])
    p.add_argument("--schema",   default=DEFAULTS["schema_json"])
    p.add_argument("--table",    default=DEFAULTS["target_table"])
    p.add_argument("--stage",    type=int, default=DEFAULTS["stop_stage"])
    args = p.parse_args()

    cfg = dict(DEFAULTS)
    cfg["server"]       = args.server
    cfg["database"]     = args.database
    cfg["source_file"]  = args.file
    cfg["schema_json"]  = args.schema
    cfg["target_table"] = args.table
    cfg["stop_stage"]   = args.stage

    state = {}

    # ── Stage 1: Connect ────────────────────────────────────────────────────
    print(f"[1/4] Connecting to {cfg['server']} / {cfg['database']} ...")
    from dataview.core.db import DBConfig, connect
    db_cfg = DBConfig(
        server       = cfg["server"],
        database     = cfg["database"],
        driver       = cfg["driver"],
        windows_auth = cfg["windows_auth"],
        username     = cfg["username"],
        password     = cfg["password"],
    )
    result = connect(db_cfg)
    if not result.ok:
        print(f"  ✗ Connection failed: {result.message}")
        sys.exit(1)
    print(f"  ✓ Connected")
    # Keep engine in state for use during this script.
    # _save() strips it before pickling; app.py reconnects on load.
    state["engine"]         = result.engine
    state["_resume_db_cfg"] = cfg   # connection params for app.py to reconnect
    state["demo"]           = False
    state["schema_variant"] = cfg["schema_variant"]
    state["stage"]          = 1
    if cfg["stop_stage"] <= 1:
        _save(state); return

    # ── Stage 2: Ingest + Load to Staging ───────────────────────────────────
    print(f"[2/4] Ingesting {os.path.basename(cfg['source_file'])} ...")
    from dataview.import_data.staging import ingest_file, load_to_staging
    import pandas as pd

    with open(cfg["source_file"], "rb") as f:
        file_bytes = f.read()

    ingest = ingest_file(
        file_bytes,
        filename  = os.path.basename(cfg["source_file"]),
        delimiter = cfg["delimiter"],
        encoding  = cfg["encoding"],
    )
    if not ingest.ok:
        print(f"  ✗ Ingest failed: {ingest.message}")
        sys.exit(1)

    print(f"  Staging {ingest.col_count} columns ...")
    sr = load_to_staging(state["engine"], ingest)
    if not sr.ok:
        print(f"  ✗ Staging failed: {sr.message}")
        sys.exit(1)

    _stg_full = sr.table_name or ""
    if "." in _stg_full:
        stg_schema, stg_table = _stg_full.split(".", 1)
    else:
        stg_schema, stg_table = "stg", _stg_full

    _prev = _preview_csv(ingest, n=500)
    staging_df = pd.DataFrame(_prev) if _prev else pd.DataFrame(columns=ingest.columns)

    state["staging_df"]      = staging_df
    state["stg_schema"]      = stg_schema
    state["stg_table"]       = stg_table
    state["stg_name"]        = stg_table
    state["src_filename"]    = os.path.basename(cfg["source_file"])
    state["_stg_parse_key"]  = f"{os.path.basename(cfg['source_file'])}_{cfg['delimiter']}_{cfg['encoding']}_\""
    state["norm_df"]         = None
    state["col_mapping"]     = None
    state["fk_checked"]      = False
    state["stage"]           = 2
    print(f"  ✓ Staged to {sr.table_name} ({sr.rows_loaded} rows)")
    if cfg["stop_stage"] <= 2:
        _save(state); return

    # ── Stage 3: Load schema + Normalize ────────────────────────────────────
    print(f"[3/4] Loading schema and normalizing ...")
    from dataview.core.schema import load_schema_from_dict
    import json

    schema_path = cfg["schema_json"]
    if not os.path.exists(schema_path):
        # Try to find it relative to APP_DIR
        for candidate in [
            os.path.join(APP_DIR, "ppdm39_schema.json"),
            os.path.join(APP_DIR, "schema", "ppdm39_schema.json"),
        ]:
            if os.path.exists(candidate):
                schema_path = candidate
                break
        else:
            print(f"  ✗ Schema file not found: {schema_path}")
            sys.exit(1)

    # utf-8-sig handles BOM that some JSON files have
    with open(schema_path, encoding="utf-8-sig") as f:
        ppdm_schema = load_schema_from_dict(json.load(f))
    if not ppdm_schema:
        print("  ✗ Schema load failed")
        sys.exit(1)

    # get_table() returns a TableDef — extract .columns from it
    target_table = cfg["target_table"]
    tbl_def = ppdm_schema.get_table(target_table)
    if not tbl_def:
        tbl_def = ppdm_schema.get_table(target_table.lower())
    if not tbl_def:
        print(f"  ✗ Table '{target_table}' not found in schema")
        sys.exit(1)
    target_cols = tbl_def.columns   # list of ColumnDef
    print(f"  ✓ Schema loaded: {len(target_cols)} cols for {target_table}")

    from dataview.import_data.normalize import normalize_server
    norm_result = normalize_server(
        state["engine"], "raw_data", staging_df,
        schema_col_types={c.column_name: getattr(c, "data_type", "") for c in target_cols},
        schema="stg"
    )
    if norm_result.ok and norm_result.df is not None:
        norm_df = norm_result.df
        print(f"  ✓ Normalized: {len(norm_df)} rows, {len(norm_df.columns)} cols")
    else:
        norm_df = staging_df
        print(f"  ⚠ Normalize skipped/failed: {getattr(norm_result, 'message', '')} — using staging df")

    state["ppdm_schema"]   = ppdm_schema
    state["target_table"]  = cfg["target_table"]
    state["target_cols"]   = target_cols
    state["norm_df"]       = norm_df
    state["stage"]         = 3
    if cfg["stop_stage"] <= 3:
        _save(state); return

    # ── Stage 4: Build mapping ───────────────────────────────────────────────
    print(f"[4/4] Building column mapping ...")
    from dataview.import_data.mapping import build_mapping

    src_cols = list(norm_df.columns)
    col_mapping = build_mapping(cfg["target_table"], target_cols, src_cols)

    matched = sum(1 for m in col_mapping.mapped if m.source_col)
    print(f"  ✓ Mapping built: {matched}/{len(col_mapping.mapped)} columns matched")

    state["col_mapping"]           = col_mapping
    state["_mapping_src_cols"]     = sorted(src_cols)
    state["_mapping_built_for_parse_key"] = state.get("_stg_parse_key")
    state["stage"]                 = 4   # mapping stage

    _save(state)


def _preview_csv(ingest, n=500):
    """Read first n rows from ingest CSV."""
    try:
        import csv, io
        with open(ingest.csv_path, encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            rows = []
            for i, row in enumerate(reader):
                if i >= n:
                    break
                rows.append(dict(row))
        return rows
    except Exception:
        return []


def _save(state):
    import pandas as pd
    out_dir  = APP_DIR
    out_pkl  = os.path.join(out_dir, ".dev_resume.pkl")
    safe = {}
    df_keys = []
    for k, v in state.items():
        if k == "engine":
            continue
        if isinstance(v, pd.DataFrame):
            # Save DataFrames as CSV to avoid StringDtype pickle issues
            csv_path = os.path.join(out_dir, f".dev_resume_{k}.csv")
            v.to_csv(csv_path, index=False, encoding="utf-8-sig")
            safe[k] = f"__CSV__{csv_path}"
            df_keys.append(k)
        else:
            safe[k] = v
    with open(out_pkl, "wb") as f:
        pickle.dump(safe, f)
    stage = state.get("stage", 0)
    stage_names = {1: "Connect", 2: "Upload & Stage", 3: "Normalize", 4: "Match & Map"}
    print(f"\n✅ Saved state — app will open at Stage {stage + 1}: {stage_names.get(stage, '')}")
    print(f"   Run: streamlit run app.py  (or refresh browser if already running)")


if __name__ == "__main__":
    main()
