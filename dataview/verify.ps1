$f = "dataview\import_data\page_load_assistant.py"
if (-not (Test-Path $f)) { Write-Host "MISSING $f" -ForegroundColor Red; exit }
$len = (Get-Item $f).Length
$marker = Select-String -Path $f -Pattern "NO INTAKE MODE" -Quiet
if ($len -eq 122130 -and $marker) {
  Write-Host ("OK  {0}  ({1} bytes) — one-box build confirmed. Restart Streamlit." -f $f,$len) -ForegroundColor Green
} else {
  Write-Host ("WRONG  {0}  is {1}, expected 122130; marker found: {2}" -f $f,$len,$marker) -ForegroundColor Red
}
