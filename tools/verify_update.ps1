# verify_update.ps1 — confirm the 2026-07-31 update landed correctly.
# Run from the repo root:  .\verify_update.ps1
$expect = @{
  "dataview\import_data\synonym_store.py"       = 34290
  "dataview\import_data\page_load_assistant.py" = 86076
  "dataview\import_data\bulk_dir_loader.py"     = 251958
  "install_synonyms.py"                         = 4289
  "extend_synonyms.py"                          = 7923
}
$bad = 0
foreach ($f in $expect.Keys | Sort-Object) {
  if (-not (Test-Path $f)) {
    Write-Host ("MISSING  {0}" -f $f) -ForegroundColor Red; $bad++; continue
  }
  $len = (Get-Item $f).Length
  if ($len -eq $expect[$f]) {
    Write-Host ("OK       {0}  ({1} bytes)" -f $f, $len) -ForegroundColor Green
  } else {
    Write-Host ("WRONG    {0}  is {1}, expected {2}" -f $f, $len, $expect[$f]) -ForegroundColor Red
    $bad++
  }
}
Write-Host ""
if ($bad -eq 0) {
  Write-Host "All files current. Restart Streamlit." -ForegroundColor Green
} else {
  Write-Host ("{0} file(s) wrong — re-extract the zip over the repo root." -f $bad) -ForegroundColor Red
}
