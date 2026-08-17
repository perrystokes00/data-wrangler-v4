# ─────────────────────────────────────────────────────────────────────────
# setup.ps1 — build the environment this repo needs, on THIS machine.
#
# A virtual environment is NOT committed, and cannot usefully be: it hard-
# codes absolute paths into its own launchers (that is why a venv copied
# from data_wrangler_clean produced "Unable to create process using
# ...data_wrangler_clean\venv\Scripts\python.exe") and it carries compiled
# binaries built for one OS, Python version and CPU. The repo ships the
# RECIPE — requirements.txt — and this script cooks it locally.
#
#   .\setup.ps1              first time, or after requirements.txt changes
#   .\setup.ps1 -Recreate    throw the environment away and rebuild it
# ─────────────────────────────────────────────────────────────────────────
param([switch]$Recreate)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$venv = Join-Path $root ".venv"

if ($Recreate -and (Test-Path $venv)) {
    Write-Host "Removing the existing environment..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force $venv
}

if (-not (Test-Path $venv)) {
    Write-Host "Creating .venv ..." -ForegroundColor Cyan
    # `py -3` picks the launcher's default interpreter; change to `py -3.12`
    # to pin a version.
    py -3 -m venv $venv
}

$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv creation failed: no $python" }

Write-Host "Installing dependencies ..." -ForegroundColor Cyan
# Always `python -m pip`, never the pip.exe shim — the shim is the thing
# with the stale path baked in when an environment has been moved.
& $python -m pip install --upgrade pip --quiet
& $python -m pip install -r (Join-Path $root "requirements.txt")

Write-Host ""
Write-Host "Done. Start the app with:" -ForegroundColor Green
Write-Host "    .\run.ps1"
