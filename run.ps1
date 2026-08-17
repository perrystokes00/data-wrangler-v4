# ─────────────────────────────────────────────────────────────────────────
# run.ps1 — start DataWrangler using this repo's own environment.
#
# Calls the venv's python DIRECTLY rather than activating and hoping: an
# activated shell still resolves `streamlit` to whatever .exe shim it finds
# first, and a moved environment's shim points at a python that no longer
# exists. `python -m streamlit` cannot pick the wrong interpreter.
# ─────────────────────────────────────────────────────────────────────────
param([int]$Port = 8502, [string]$App = "app_v4.py")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "No environment yet. Run .\setup.ps1 first." -ForegroundColor Yellow
    exit 1
}

# A previous instance holding the port is the single most common reason a
# code change "didn't take" — Streamlit keeps its modules loaded, so the
# old build serves on. Clear it rather than starting a second one.
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "Port $Port is in use — stopping the old instance." -ForegroundColor Yellow
    $busy | Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
}

& $python -m streamlit run $App --server.port $Port
