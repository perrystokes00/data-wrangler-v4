r"""
organize_into_folders.py — group the flat repo into app-domain folders (mapping/,
file_catalog/, import_pipeline/, documents/, loaders/, tools/) WITHOUT breaking imports.

HOW IT STAYS SAFE:
  Your app uses flat imports (import page_well_map). Moving files into folders would break
  those — UNLESS the folders are on sys.path. So this:
    1. Moves files into folders by rule (below)
    2. Injects a sys.path block at the top of app_v3.py that adds every subfolder to the
       path, so `import page_well_map` still resolves after the move.
  Result: organized folders, zero import rewrites.

PREVIEW by default. --apply to move + patch app_v3. Everything is git-tracked, so revertible.

  py organize_into_folders.py            # preview the grouping (moves nothing)
  py organize_into_folders.py --apply

RULES are name-based and CONSERVATIVE. Files that don't match a rule STAY IN ROOT
(safer than guessing). Review the preview and tell me to adjust rules before --apply.
"""
import os, sys, re, shutil

APPLY = "--apply" in sys.argv
ROOT = os.getcwd()

# (folder, [regex rules on basename]).  First match wins. Order matters.
GROUPS = [
    ("mapping", [
        r"^page_well_map", r"^page_wl_map", r"^geography_layers", r"^h3_", r"^run_h3",
        r"geojson", r"^build_county", r"^shapefile", r"^page_region_builder",
        r"protraction", r"^grids\.py$", r"^project_map",
    ]),
    ("file_catalog", [
        r"^page_file_catalog", r"^page_file_manager", r"^page_workbench",
        r"^page_extraction_inspector", r"^worker_core", r"^catalog_", r"^bcp_capture",
        r"^promote_", r"^triage_", r"^pdf_survey", r"^survey_loader", r"^vault_",
        r"^page_triage", r"^page_monitor", r"^scout", r"^curve_",
    ]),
    ("import_pipeline", [
        r"^page_pipeline", r"^pipeline_", r"^entity_seeder", r"^page_dv_importer",
        r"^page_standards_manager", r"^page_dv_export", r"^run_stage", r"^run_promote",
    ]),
    ("documents", [
        r"^page_selected_documents", r"^page_well_documents", r"^file_viewer",
        r"^doc_", r"^page_db_explorer", r"^page_schema_overview",
    ]),
    ("loaders", [
        r"^load_", r"^seed_", r"^walk_", r"^kgs_", r"^las_", r"^ingest_",
        r"^translators", r"^standardize", r"^importer",
    ]),
    ("tools", [
        r"^analyze_", r"^cleanup_", r"^bench", r"^profile_", r"^diag", r"^check_",
        r"^verify_", r"^clone_", r"^clear_", r"^kill_", r"^gen_", r"^make_",
        r"^generate_", r"^setup_", r"^migrate_", r"^validate_", r"^trace_",
    ]),
]

# NEVER move these — entry point + anything that must stay at root
PROTECT = {"app_v3.py", "organize_into_folders.py"}
# existing dirs to leave alone
SKIP_DIRS = {"venv",".venv",".git","__pycache__","modules","download",".vs",
             "geojson","spatial","schema_registry","output","assets","documentation",
             "sql","backup","docs","scripts","seed_catalog","_scratch"}

def classify(basename):
    for folder, rules in GROUPS:
        for rx in rules:
            if re.search(rx, basename, re.I):
                return folder
    return None  # unmatched -> stays in root

# only consider .py files currently in ROOT (top level), not already in subfolders
moves = {}  # folder -> [files]
for fn in os.listdir(ROOT):
    p = os.path.join(ROOT, fn)
    if not os.path.isfile(p) or not fn.endswith(".py"): continue
    if fn in PROTECT: continue
    folder = classify(fn)
    if folder:
        moves.setdefault(folder, []).append(fn)

print(f"{'APPLY' if APPLY else 'PREVIEW'} — organize root .py files into app folders\n")
total = 0
for folder in sorted(moves):
    files = sorted(moves[folder])
    total += len(files)
    print(f"  {folder}/  ({len(files)} files)")
    for f in files[:12]:
        print(f"       {f}")
    if len(files) > 12: print(f"       ... and {len(files)-12} more")
    print()

# count what stays in root
staying = [fn for fn in os.listdir(ROOT)
           if fn.endswith(".py") and os.path.isfile(os.path.join(ROOT,fn))
           and fn not in PROTECT and classify(fn) is None]
print(f"STAYING IN ROOT ({len(staying)} unmatched .py + app_v3.py):")
for f in sorted(staying)[:15]: print(f"   {f}")
if len(staying) > 15: print(f"   ... and {len(staying)-15} more")
print(f"\ntotal to move: {total} files into {len(moves)} folders")

if not APPLY:
    print("\n(preview) nothing moved. Review the grouping; tell me to adjust rules, or")
    print("re-run with --apply to move files + patch app_v3.py's sys.path.")
    sys.exit()

# ---- APPLY ----
# 1) move files
for folder, files in moves.items():
    fdir = os.path.join(ROOT, folder)
    os.makedirs(fdir, exist_ok=True)
    # make it a package (harmless, helps some tooling)
    initf = os.path.join(fdir, "__init__.py")
    if not os.path.exists(initf): open(initf,"w").close()
    for fn in files:
        shutil.move(os.path.join(ROOT, fn), os.path.join(fdir, fn))

# 2) inject sys.path setup into app_v3.py so flat imports still resolve
app = os.path.join(ROOT, "app_v3.py")
src = open(app, encoding="utf-8").read()
if "_DV_SUBFOLDERS" not in src:
    inject = (
        "\n# ─── auto-added by organize_into_folders.py: keep flat imports working ───\n"
        "import os as _dv_os, sys as _dv_sys\n"
        "_DV_ROOT = _dv_os.path.dirname(_dv_os.path.abspath(__file__))\n"
        "_DV_SUBFOLDERS = " + repr(sorted(moves.keys())) + "\n"
        "for _d in [_DV_ROOT] + [_dv_os.path.join(_DV_ROOT, _s) for _s in _DV_SUBFOLDERS]:\n"
        "    if _d not in _dv_sys.path:\n"
        "        _dv_sys.path.insert(0, _d)\n"
        "# ─────────────────────────────────────────────────────────────────────────\n"
    )
    # insert right after the first 'import streamlit as st' line
    marker = "import streamlit as st\n"
    idx = src.find(marker)
    if idx == -1:
        # fallback: after __future__
        marker = "from __future__ import annotations\n"
        idx = src.find(marker)
    idx += len(marker)
    src = src[:idx] + inject + src[idx:]
    open(app + ".bak_organize", "w", encoding="utf-8").write(open(app,encoding="utf-8").read())
    open(app, "w", encoding="utf-8").write(src)
    print("\npatched app_v3.py: added sys.path setup for the new folders")

print(f"\nAPPLIED. moved {total} files into {len(moves)} folders.")
print("RESTART Streamlit and click every page. Flat imports resolve via the injected")
print("sys.path block. If a page errors 'No module named X', that file went to the wrong")
print("folder OR needs its folder added — tell me and we'll fix the rule.")
