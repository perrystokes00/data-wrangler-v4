<#
    tidy_root.ps1 — sort the repo root into folders. DRY RUN by default.

        .\tidy_root.ps1              # show what would move, change nothing
        .\tidy_root.ps1 -Apply       # do it

    WHY BOTHER
    ----------
    A cluttered root is not untidiness, it is a hazard: a fresh session (human
    or AI) opening the repo cannot tell an entry point from a probe written
    once in July. This codebase has already paid for that — a stale
    `sql\build_catalog_mirror.py` sat beside the live one, and editing it would
    have had no effect.

    NOTHING IS DELETED. Dead files move to _attic\ so a mistake is a move
    back, not a restore from backup. Delete _attic by hand once the app has
    run happily for a week.

    ⚠ THE ROOT IS ON sys.path. Moving a module that something imports by bare
    name breaks the app, so the KEEP list below is deliberately generous and
    every uncertain file goes to tools\ (still importable via a path) rather
    than _attic. Run selftest afterwards.
#>
param([switch]$Apply)

$ErrorActionPreference = "Stop"

# ── MUST STAY AT ROOT ──────────────────────────────────────────────────────
# Entry points, things imported by bare name, config, and packaging inputs
# whose paths are written into installer.iss / build_installer.ps1.
$Keep = @(
    "app_v4.py",                  # the Streamlit app
    "launcher.py",                # installed entry point (make_dist --keep)
    "selftest.py",                # five-tier regression harness
    "check_mirror_registry.py",   # imported by selftest's invariants tier
    "make_dist.py", "make_icon.py", "make_map.py",
    "build_installer.ps1", "installer.iss",
    "start.bat", "start_cmd.bat", "run.ps1", "setup.ps1",
    "start_data_wrangler.bat", "stop_data_wrangler.bat",
    "start_federation_map.bat", "run_watcher.bat",
    "requirements.txt", ".env", ".gitignore", ".dev_pid",
    "user_prefs.json",            # the map's saved places
    "README.md", "EULA.txt", "CLAUDE.md"
)

# ── tools\ — real utilities, kept, just not at root ────────────────────────
$Tools = @(
    "reload_wy_master.py",            # WY_WOGCC reload via CAPINO
    "backfill_master_h3_bcp.py",      # the bcp version — the one to use
    "load_teapot_seismic_geometry.py",
    "reconcile_reference_csv.py", "export_table_csv.py",
    "whose_id.py", "reconcile_orphans.py", "quarantine_orphans.py",
    "codebase_census.py", "db_scorecard.py",
    "shapefile_to_geography.py", "segy_lines_to_wgs84.py", "xy_to_latlong.py",
    "run_h3.py", "find_dt.py", "compare_extractors.py",
    "extend_synonyms_round4.py", "run_load_assistant.py",
    "verify.ps1", "verify_update.ps1"
)

# ── sql\ ───────────────────────────────────────────────────────────────────
$Sql = @("INVENTORY_ID FILE_PATH FILE_NAME FILE_EX.sql")

# ── docs\ — handovers and census output ────────────────────────────────────
$Docs = @(
    "handoff_2026-07-16.md", "handoff_2026-07-16_addendum.md",
    "handoff_2026-07-17_addendum2.md",
    "census.md", "census2.md", "dead.md", "python make_dist.md"
)

# ── reports\ — generated output that belongs under C:\Bulk\reports ─────────
$Reports = @(
    "scorecard.html", "scorecard_DataView_Demo.html",
    "scorecard_synth_docs.html", "scorecard_after_recogniser.html",
    "scorecard_before_documents.html",
    "database_scorecard.txt", "card.txt", "schema_dump.txt",
    "gaps.csv", "DISCOVER.csv", "DISCOVER_CSV.csv",
    "loaded_modules.txt", "seismic_lines.geojson", "seismic.duckdb"
)

# ── _attic\ — dead or one-off. NOT deleted. ────────────────────────────────
$Attic = @(
    # this week's scratch, written during diagnosis
    "fix.py", "probe.py", "code_block.py", "vwell.txt",
    # superseded: the pyodbc H3 backfill, 225 rows/sec against bcp's 27,000
    "backfill_master_h3.py",
    # one-off probes from July, each written for a bug now closed
    "header_probe.py", "capture_probe.py", "probe_capture.py",
    "probe_seismic_pill.py",
    # dead-code analysis output, superseded by census2.md
    "dead_code.py", "dead_sections.py", "dead.txt", "dead_all.txt",
    # PowerShell redirection accidents — zero bytes, named after cmdlets
    "Get-ChildItem", "Get-Content", "Get-NetTCPConnection"
)

$here = Get-Location
$plan = @()
foreach ($f in Get-ChildItem -File) {
    $n = $f.Name
    # A PSCustomObject, NOT a nested array. `$plan += ,@($n,$grp)` looks
    # right and is not: PowerShell flattens it, so $i[0] indexes into the
    # STRING and yields "I" — which is exactly the "Cannot move item because
    # the item at 'I' does not exist" this script produced on its first run.
    $dest =
        if     ($Keep    -contains $n) { $null }
        elseif ($Tools   -contains $n) { "tools" }
        elseif ($Sql     -contains $n) { "sql" }
        elseif ($Docs    -contains $n) { "docs" }
        elseif ($Reports -contains $n) { "reports" }
        elseif ($Attic   -contains $n) { "_attic" }
        else {
            # UNCLASSIFIED — anything this script has not been told about
            # stays where it is. A tidier that moves files it does not
            # recognise is how a working app breaks quietly.
            "(left alone)"
        }
    if ($null -ne $dest) {
        $plan += [PSCustomObject]@{ Name = $n; Dest = $dest }
    }
}

"" ; "ROOT TIDY — $(if($Apply){'APPLYING'}else{'DRY RUN'})" ; ("-" * 62)
foreach ($grp in @("tools","sql","docs","reports","_attic","(left alone)")) {
    $items = @($plan | Where-Object { $_.Dest -eq $grp })
    if ($items.Count -eq 0) { continue }
    "" ; "  -> $grp  ($($items.Count))"
    foreach ($i in $items) { "       $($i.Name)" }
}
"" ; "  kept at root: $($Keep.Count) known entry points / config" ; ""

if (-not $Apply) {
    "-- dry run; re-run with -Apply to move. Nothing is deleted: dead files"
    "   go to _attic\ so a mistake is a move back."
    return
}

$moved = 0
foreach ($grp in @("tools","sql","docs","reports","_attic")) {
    $items = @($plan | Where-Object { $_.Dest -eq $grp })
    if ($items.Count -eq 0) { continue }
    if (-not (Test-Path $grp)) { New-Item -ItemType Directory -Path $grp | Out-Null }
    foreach ($i in $items) {
        if (-not (Test-Path -LiteralPath $i.Name)) { continue }   # already moved
        Move-Item -LiteralPath $i.Name -Destination (Join-Path $grp $i.Name) -Force
        $moved++
    }
}
"" ; "moved $moved file(s)."
"NOW RUN:  python selftest.py"
"   — the root is on sys.path, so a module moved out of it that something"
"     imported by bare name will fail on import, not at runtime."
