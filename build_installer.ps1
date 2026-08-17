<#
build_installer.ps1 — turn a dist folder into a Windows installer.

    .\build_installer.ps1 -DistDir dist\data_wrangler_20260807
    .\build_installer.ps1 -DistDir dist\... -SkipInno      # payload only

WHAT THIS DOES, and why each step exists
----------------------------------------
The customer will not have Python. So the installer carries its own: the
EMBEDDABLE distribution, a ~10 MB zip Microsoft publishes for exactly this
purpose. It has no installer of its own, touches no registry, and does not
collide with any Python already on the machine — which matters, because
"it broke my other Python" is the worst possible first impression.

  1. download the embeddable Python zip and unpack it into staging
  2. UNCOMMENT `import site` in the ._pth file  ← the step everyone misses
  3. bootstrap pip into it
  4. pip install the requirements INTO that private Python
  5. copy the dist tree beside it
  6. hand the whole staging folder to Inno Setup

STEP 2 IS THE ONE THAT WASTES AN AFTERNOON. The embeddable build ships with
`import site` commented out in pythonXXX._pth, which disables
site-packages entirely. pip appears to install fine and then nothing can be
imported. Uncommenting it is the whole fix.

WHAT THIS DOES NOT DO
---------------------
The Microsoft ODBC Driver for SQL Server is NOT redistributable inside your
installer, and the app cannot talk to SQL Server without it. The Inno script
detects it and points the user at Microsoft's download. Same for bcp and
sqlcmd, which several code paths shell out to.
#>

param(
    [Parameter(Mandatory = $true)][string]$DistDir,
    [string]$Staging       = "build\payload",
    [string]$PythonVersion = "3.12.7",
    [string]$Requirements  = "requirements.txt",
    [switch]$SkipInno,
    [string]$InnoScript    = "installer.iss"
)

$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "!!  $m" -ForegroundColor Yellow }

if (-not (Test-Path $DistDir)) { throw "dist folder not found: $DistDir" }
if (-not (Test-Path $Requirements)) {
    throw "$Requirements not found. Generate one first — see the note at the end."
}

# ── 1 · staging ─────────────────────────────────────────────────────────
if (Test-Path $Staging) {
    Say "clearing $Staging"
    Remove-Item -Recurse -Force $Staging
}
New-Item -ItemType Directory -Path $Staging -Force | Out-Null

$pyDir = Join-Path $Staging "python"
New-Item -ItemType Directory -Path $pyDir -Force | Out-Null

# ── 2 · embeddable python ───────────────────────────────────────────────
$zipName = "python-$PythonVersion-embed-amd64.zip"
$zipUrl  = "https://www.python.org/ftp/python/$PythonVersion/$zipName"
$zipPath = Join-Path "build" $zipName
if (-not (Test-Path $zipPath)) {
    Say "downloading $zipName"
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath
} else {
    Say "using cached $zipName"
}
Say "unpacking embeddable python"
Expand-Archive -Path $zipPath -DestinationPath $pyDir -Force

# ── 3 · THE STEP EVERYONE MISSES ────────────────────────────────────────
# Without this, site-packages is disabled and nothing pip installs can be
# imported. The symptom is "ModuleNotFoundError: streamlit" after a pip
# install that reported success.
$pth = Get-ChildItem -Path $pyDir -Filter "python*._pth" | Select-Object -First 1
if (-not $pth) { throw "no ._pth file in the embeddable python — layout changed?" }
Say "enabling site-packages in $($pth.Name)"
$content = Get-Content $pth.FullName
$content = $content -replace '^\s*#\s*import\s+site\s*$', 'import site'
if ($content -notcontains 'import site') { $content += 'import site' }
# The app and its packages both need to be importable.
if ($content -notcontains 'Lib\site-packages') { $content += 'Lib\site-packages' }
if ($content -notcontains '..\app') { $content += '..\app' }
$content | Set-Content $pth.FullName -Encoding ASCII

# ── 4 · pip ─────────────────────────────────────────────────────────────
$getPip = Join-Path "build" "get-pip.py"
if (-not (Test-Path $getPip)) {
    Say "downloading get-pip.py"
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
}
$pyExe = Join-Path $pyDir "python.exe"
Say "bootstrapping pip"
& $pyExe $getPip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "get-pip failed" }

Say "installing requirements (this is the slow part)"
& $pyExe -m pip install --no-warn-script-location -r $Requirements
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# Trim what ships. Tests and caches inside site-packages are dead weight —
# often 100 MB+ across pandas, pyarrow and friends.
Say "trimming site-packages"
$sp = Join-Path $pyDir "Lib\site-packages"
Get-ChildItem -Path $sp -Recurse -Directory -Include "__pycache__", "tests", "test" `
    -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# ── 5 · the app ─────────────────────────────────────────────────────────
$appDir = Join-Path $Staging "app"
Say "copying $DistDir -> $appDir"
Copy-Item -Path $DistDir -Destination $appDir -Recurse -Force
foreach ($f in @("launcher.py")) {
    if (Test-Path $f) { Copy-Item $f -Destination $appDir -Force }
    else { Warn "$f not found beside this script — the shortcut will not work" }
}

# ── 6 · smoke test the payload BEFORE building an installer ─────────────
# An installer that installs a broken payload wastes a customer's time and
# yours. This is the same isolation test as running selftest inside dist,
# but against the private python that will actually ship.
Say "smoke test: import streamlit with the bundled python"
& $pyExe -c "import streamlit, pandas, sqlalchemy; print('imports OK')"
if ($LASTEXITCODE -ne 0) { throw "bundled python cannot import the app's dependencies" }

if (Test-Path (Join-Path $appDir "selftest.py")) {
    Say "smoke test: selftest --tier imports against the payload"
    Push-Location $appDir
    & $pyExe "selftest.py" "--tier" "imports"
    $rc = $LASTEXITCODE
    Pop-Location
    if ($rc -ne 0) { Warn "selftest reported failures — check before shipping" }
} else {
    Warn "selftest.py not in the dist — build it with --keep selftest.py to get this check"
}

$size = (Get-ChildItem $Staging -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Say ("payload ready: {0:N0} MB in {1}" -f $size, $Staging)

# ── 7 · installer ───────────────────────────────────────────────────────
if ($SkipInno) { Say "-SkipInno given; stopping here"; exit 0 }

$iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $iscc) {
    Warn "Inno Setup 6 not found. Install from https://jrsoftware.org/isdl.php"
    Warn "then re-run, or compile $InnoScript by hand."
    exit 1
}
Say "compiling $InnoScript"
& $iscc $InnoScript
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Say "done — see build\output\"

<#
GENERATING requirements.txt
---------------------------
Do NOT use `pip freeze` from your dev environment: it will pin everything
you have ever installed, including the probes' dependencies and whatever a
notebook pulled in. Start from what the dist actually imports:

    python -c "import ast,os,sys;
    mods=set()
    for d,_,fs in os.walk(sys.argv[1]):
        for f in fs:
            if f.endswith('.py'):
                try: t=ast.parse(open(os.path.join(d,f),encoding='utf-8',errors='ignore').read())
                except SyntaxError: continue
                for n in ast.walk(t):
                    if isinstance(n,ast.Import): mods|={a.name.split('.')[0] for a in n.names}
                    elif isinstance(n,ast.ImportFrom) and n.module and not n.level:
                        mods.add(n.module.split('.')[0])
    print('\n'.join(sorted(mods)))" dist\data_wrangler_20260807

then remove the stdlib and your own packages, and pin the rest to the
versions you have been running.
#>
