@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM ===========================================================================
REM start.bat - start / stop the DEV Streamlit server.
REM
REM   start.bat            same as "start.bat start"
REM   start.bat start      start it
REM   start.bat stop       stop it
REM   start.bat restart    stop then start
REM   start.bat status     is it running, and on what
REM
REM WHY THE FLAGS ARE HERE AND NOT IN config.toml
REM   .streamlit\config.toml is SHIPPED INSIDE THE INSTALLER, and the installed
REM   app needs headless=true (launcher.py opens the browser itself, so without
REM   it the customer gets two tabs) and fileWatcherType=none (nothing is
REM   editing source on a customer machine). Those are exactly wrong for
REM   development. Command-line flags outrank the config file, so this script
REM   overrides them per-run and the shipped config stays correct.
REM
REM     --server.headless false          open the browser like it used to
REM     --server.fileWatcherType auto    watch sources, so the "Source file
REM                                      changed / Rerun" prompt comes back
REM
REM   runOnSave is deliberately NOT set: true would rerun automatically and
REM   you would never see the prompt you asked to have back.
REM
REM WHY EnableDelayedExpansion IS ON
REM   `set /p DWPID=<file` followed by %DWPID% INSIDE THE SAME parenthesised
REM   block does not work: cmd expands %DWPID% when it PARSES the block, which
REM   is before set /p has run, so the value is empty. That silently broke
REM   stop-by-pidfile (it killed nothing and fell through to the netstat
REM   fallback) and made status print a blank PID. !DWPID! expands at execution
REM   time, which is what these blocks need.
REM
REM WHY THE INTERPRETER IS PINNED
REM   'python' means whatever won the PATH race. On 16 Aug a Node.js install
REM   put a bare C:\Python314 ahead of the runtime that actually has streamlit,
REM   and every launch died with "No module named streamlit". Naming the
REM   interpreter here makes this script immune to the next installer that
REM   does the same thing; the PATH fallback keeps it working elsewhere.
REM
REM WHY IT IS NO LONGER THE INSTALLED ONE
REM   This pointed at "C:\Program Files\Data Wrangler v4\python\python.exe" —
REM   the INSTALLED, EMBEDDED interpreter. Its sys.path carries the DEPLOYED
REM   app folder but not the script's own directory, so this dev launcher ran
REM   the repo's app_v4.py while importing `dataview` from the deployment.
REM   Silent: the app starts, and edits made in the repo appear to do nothing.
REM   This is a DEV script for THIS repo, so it uses a normal interpreter and
REM   never the installed one. app_v4.py also inserts its own root at sys.path
REM   position 0, so a stray launcher cannot re-create the problem.
REM ===========================================================================

set "APP=app_v4.py"
set "PORT=8501"
set "PIDFILE=%~dp0.dev_pid"
set "SELF=%~nx0"

REM A repo venv if setup.ps1 has been run, else the PATH python. Never the
REM installed embedded build — see above.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

if /i "%ACTION%"=="start"   goto :start
if /i "%ACTION%"=="stop"    goto :stop
if /i "%ACTION%"=="restart" goto :restart
if /i "%ACTION%"=="status"  goto :status
echo Unknown action "%ACTION%".  Use: start ^| stop ^| restart ^| status
exit /b 2

REM ---------------------------------------------------------------------------
:start
if not exist "%~dp0%APP%" (
    echo ERROR: %APP% not found next to this script - %~dp0
    exit /b 1
)

REM Refuse rather than pile a second server on top. A stale one holding 8501
REM is why a launch silently lands on 8502 and you end up looking at the wrong
REM instance without knowing it.
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo Something is already listening on port %PORT%.
    echo Run "%SELF% stop" first, or "%SELF% status" to see what it is.
    exit /b 1
)

echo Starting %APP% on port %PORT% ...
echo Using %PY%

REM Start via PowerShell purely to capture the PID. A new console window opens
REM so Streamlit's own output stays visible - that log is worth having in view.
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "(Start-Process -FilePath '%PY%' -ArgumentList @('-m','streamlit','run','%APP%','--server.port','%PORT%','--server.headless','false','--server.fileWatcherType','auto') -WorkingDirectory '%~dp0.' -PassThru).Id"`) do set "DWPID=%%P"

if not defined DWPID (
    echo ERROR: could not start %PY%
    echo   If that path does not exist, the pinned interpreter has moved -
    echo   find the one with streamlit and update PY at the top of this script.
    exit /b 1
)

> "%PIDFILE%" echo %DWPID%
echo Started, PID %DWPID%.  Browser should open at http://localhost:%PORT%
echo Stop it with:  %SELF% stop
exit /b 0

REM ---------------------------------------------------------------------------
:stop
set "KILLED="

REM Preferred route: the PID we recorded at start.
if exist "%PIDFILE%" (
    set "DWPID="
    set /p DWPID=<"%PIDFILE%"
    if defined DWPID (
        REM /T because streamlit spawns children; killing only the parent
        REM leaves the server holding the port - exactly the orphan case.
        taskkill /PID !DWPID! /T /F >nul 2>&1
        if not errorlevel 1 (
            echo Stopped PID !DWPID!.
            set "KILLED=1"
        )
    )
    del "%PIDFILE%" >nul 2>&1
)

REM Fallback: whatever is actually holding the port. Covers a server started
REM some other way, or one whose pid file was lost.
for /f "tokens=5" %%A in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    taskkill /PID %%A /T /F >nul 2>&1
    if not errorlevel 1 (
        echo Stopped PID %%A holding port %PORT%.
        set "KILLED=1"
    )
)

if not defined KILLED echo Nothing was running on port %PORT%.
exit /b 0

REM ---------------------------------------------------------------------------
:restart
call "%~f0" stop
REM A socket does not free instantly; give it a moment before rebinding.
timeout /t 2 /nobreak >nul
call "%~f0" start
exit /b 0

REM ---------------------------------------------------------------------------
:status
echo Port %PORT%:
netstat -ano | findstr /r /c:":%PORT% .*LISTENING"
if errorlevel 1 echo   nothing listening
if exist "%PIDFILE%" (
    set "DWPID="
    set /p DWPID=<"%PIDFILE%"
    echo Recorded PID: !DWPID!
) else (
    echo Recorded PID: none
)
exit /b 0
