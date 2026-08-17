@echo off
REM Opens a NEW PowerShell window in the app folder with the venv activated.
REM All setup runs INSIDE the new window (passed via -Command), not this one.
start "DataWrangler" pwsh -NoExit -ExecutionPolicy Bypass -Command ^
 "Set-Location 'C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\data_wrangler\data_wrangler_v4'; .\venv\Scripts\Activate.ps1; Clear-Host"
