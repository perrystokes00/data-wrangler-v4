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

REM THE LOG, BECAUSE THE CONSOLE WINDOW IS NOT RELIABLE. Start-Process opens a
REM window for the server, but nothing keeps that window alive: if Streamlit
REM exits -- a bad port, a failed import, a syntax error in a page -- the
REM window carrying the traceback closes with it, so the one launch that
REM needed reading is the one that leaves nothing behind. And an intermittent
REM problem is worse: the evidence scrolls past while nobody is watching.
REM
REM Both streams are captured. Streamlit's own messages (the fragment
REM warnings) go to stderr; the map timing lines are print() on stdout.
REM Start-Process refuses to point both at ONE file, hence two.
REM
REM   start.bat log       follow them live
REM   start.bat log all    dump what is already there
set "LOGDIR=%~dp0logs"
set "LOGOUT=%LOGDIR%\dev.out.log"
set "LOGERR=%LOGDIR%\dev.err.log"

REM UTF-8 ON STDOUT, BECAUSE REDIRECTION CHANGES THE ENCODING. A console gets
REM one encoder; a REDIRECTED stdout gets the ANSI codepage (cp1252 here). The
REM map's phase labels carry emoji, so the moment this script started writing
REM to a file instead of a window, print() raised UnicodeEncodeError and took
REM the page down with it -- "Well Map error: 'charmap' codec can't encode
REM character '\U0001f310'". The code no longer depends on this (see _say in
REM page_well_map), but without it the LOG holds "?" where the labels were.
set "PYTHONIOENCODING=utf-8"

REM A repo venv if setup.ps1 has been run, else the PATH python. Never the
REM installed embedded build — see above.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

REM WATCHING SOURCES COSTS SOMETHING, AND ON DEMO DAY IT COSTS TOO MUCH.
REM With the watcher on, every save to a local module makes Streamlit reload
REM it -- page_well_map is 711 KB and takes ~1.2 s to import -- and the repo
REM lives inside OneDrive, which touches mtimes of its own accord. Eighteen
REM edits in one morning is eighteen reloads the running session absorbed.
REM
REM   start.bat start nowatch     no watcher; edits need a restart
REM   start.bat restart nowatch   same, after stopping
REM
REM Which is what .streamlit\config.toml asks for already; this script's
REM --server.fileWatcherType auto is the override, and this turns it off
REM again without editing either file.
REM SPELLED SEVERAL WAYS ON PURPOSE. A switch that silently falls through to
REM the DEFAULT when it is typed the other way is worse than one that errors:
REM the server starts, watching, and looks like the flag did nothing. -NoWatch
REM is the first thing a PowerShell hand types, so it is accepted here too.
set "WATCH=auto"
for %%W in ("nowatch" "no-watch" "-nowatch" "-no-watch" "/nowatch") do (
    if /i "%~2"==%%W set "WATCH=none"
    if /i "%ACTION%"==%%W (set "WATCH=none" & set "ACTION=start")
)

if /i "%ACTION%"=="start"   goto :start
if /i "%ACTION%"=="stop"    goto :stop
if /i "%ACTION%"=="restart" goto :restart
if /i "%ACTION%"=="status"  goto :status
if /i "%ACTION%"=="log"     goto :log
if /i "%ACTION%"=="dev"     goto :dev
if /i "%ACTION%"=="run"     goto :run
if /i "%ACTION%"=="fg"      goto :run
echo Unknown action "%ACTION%".  Use: dev ^| start ^| run ^| stop ^| restart ^| status ^| log
echo   run     stay in THIS window, output here, Ctrl+C to stop
echo   log     follow the background server's output
echo   dev     restart AND follow the log in this window (the usual one)
echo Add "nowatch" to start, run or restart to skip the file watcher.
exit /b 2

REM ---------------------------------------------------------------------------
REM FOREGROUND. The server runs in the window you typed this in: its output
REM arrives where you are already looking, and Ctrl+C stops it. No pid file,
REM because there is no process to go hunting for afterwards.
REM
REM This exists because the background window is not dependable — it belongs
REM to a process Windows is free to close the moment Streamlit exits, which is
REM precisely when its last words matter.
:run
if not exist "%~dp0%APP%" (
    echo ERROR: %APP% not found next to this script - %~dp0
    exit /b 1
)
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo Something is already listening on port %PORT%.
    echo Run "%SELF% stop" first, or "%SELF% status" to see what it is.
    exit /b 1
)
echo Running %APP% on port %PORT% in THIS window.  Ctrl+C to stop.
echo Using %PY%
echo.
"%PY%" -m streamlit run "%~dp0%APP%" --server.port %PORT% --server.headless false --server.fileWatcherType %WATCH%
exit /b %ERRORLEVEL%

REM ---------------------------------------------------------------------------
REM Follow what the BACKGROUND server is printing. Both streams, merged and
REM tagged, because the interesting lines are split across them: Streamlit's
REM warnings land on stderr and the map's timing lines on stdout.
:dev
REM ONE COMMAND, BECAUSE TWO IS WHERE IT WENT WRONG. "start.bat log" only
REM FOLLOWS a file; it starts nothing. Run against a stopped server it
REM quietly tails yesterday's log, which looks exactly like an app that
REM opened a terminal and did not launch -- reported as precisely that.
call "%~f0" restart
if errorlevel 1 exit /b 1

REM VERIFY IT ACTUALLY CAME UP BEFORE TAILING. restart reported "Nothing was
REM running" while a server still held the port, then started a second one
REM whose pid was never captured -- so dev fell through to the log and sat
REM there looking like a hang. Tailing a file for a server that did not start
REM is the same silent failure this action was added to remove.
set "DWUP="
for /l %%N in (1,1,20) do (
    if not defined DWUP (
        netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
        if not errorlevel 1 set "DWUP=1"
        if not defined DWUP ping -n 2 127.0.0.1 >nul 2>&1
    )
)
if not defined DWUP (
    echo.
    echo ERROR: nothing is listening on port %PORT% after 20 tries.
    echo   The server did not start. Check %LOGERR% for the traceback,
    echo   or run "%SELF% run" to see it in this window.
    exit /b 1
)
echo.
goto :log

REM ---------------------------------------------------------------------------
:log
REM UTF-8 BOTH WAYS OR THE LOG IS UNREADABLE. The server writes UTF-8
REM (PYTHONIOENCODING above, because a REDIRECTED stdout otherwise gets
REM cp1252 and the emoji in the phase labels crashed print()). Windows
REM PowerShell 5.1 then READS it back as the ANSI codepage, so the same
REM emoji came out as "M-pM-^_M-^TM-^6" -- correct in the file, mangled only
REM in the window. chcp sets the console to UTF-8 so it can display them,
REM -Encoding UTF8 tells Get-Content how to decode. Both are needed.
REM
REM BUT ONLY ON THE DUMP BRANCH. Adding -Encoding to the -Wait branch is
REM what stopped it streaming: "I get Using ...python.exe but no streaming,
REM only when I exit do I see streaming" -- output appearing all at once on
REM exit is a buffer flushing. The dump branch reads a finite file and exits,
REM so buffering there is invisible and the decode is worth having.
REM
REM The follow branch sets [Console]::OutputEncoding instead, which fixes the
REM WRITE side without putting an encoding reader in front of the tail. Any
REM emoji that still mangle in the live view are mangled in the WINDOW only:
REM the file is correct UTF-8, and "start.bat log all" shows it properly.
REM Streaming is the job; the emoji are decoration.
chcp 65001 >nul 2>&1
if not exist "%LOGOUT%" if not exist "%LOGERR%" (
    echo No log yet at %LOGDIR%.
    echo   "%SELF% start" writes one; "%SELF% run" prints to the window instead.
    exit /b 1
)
if /i "%~2"=="all" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "Get-Content -Encoding UTF8 -Path '%LOGOUT%','%LOGERR%' -ErrorAction SilentlyContinue"
    exit /b 0
)
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if errorlevel 1 (
    echo NOTE: nothing is listening on port %PORT% - the server is NOT running.
    echo       What follows is the log from the PREVIOUS run. It will not grow.
    echo       Use "%SELF% dev" to start it and follow it in one step.
    echo.
)
echo Following %LOGDIR%\dev.*.log  -  Ctrl+C to stop watching (server keeps running).
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Content -Path '%LOGOUT%','%LOGERR%' -Tail 40 -Wait -ErrorAction SilentlyContinue"
exit /b 0

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

if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1

REM Start via PowerShell purely to capture the PID, with both streams sent to
REM files. The console window this used to rely on is not a place to keep a
REM log: it closes when the process does, taking the traceback with it.
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "(Start-Process -FilePath '%PY%' -ArgumentList @('-m','streamlit','run','%APP%','--server.port','%PORT%','--server.headless','false','--server.fileWatcherType','%WATCH%') -WorkingDirectory '%~dp0.' -RedirectStandardOutput '%LOGOUT%' -RedirectStandardError '%LOGERR%' -PassThru).Id"`) do set "DWPID=%%P"

if not defined DWPID (
    echo ERROR: could not start %PY%
    echo   If that path does not exist, the pinned interpreter has moved -
    echo   find the one with streamlit and update PY at the top of this script.
    exit /b 1
)

> "%PIDFILE%" echo %DWPID%
echo Started, PID %DWPID%.  Browser should open at http://localhost:%PORT%
echo Output:        %SELF% log        (or "%SELF% run" to keep it in this window)
echo Stop it with:  %SELF% stop
exit /b 0

REM ---------------------------------------------------------------------------
:stop
set "KILLED="
set "FOUND="

REM Preferred route: the PID we recorded at start.
if exist "%PIDFILE%" (
    set "DWPID="
    set /p DWPID=<"%PIDFILE%"
    if defined DWPID (
        set "FOUND=1"
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
    set "FOUND=1"
    taskkill /PID %%A /T /F >nul 2>&1
    if not errorlevel 1 (
        echo Stopped PID %%A holding port %PORT%.
        set "KILLED=1"
    )
)

REM THE PORT IS THE TRUTH, NOT TASKKILL'S EXIT CODE. This used to print
REM "Nothing was running on port %PORT%." whenever the kill FAILED, because
REM the only flag it kept was set inside `if not errorlevel 1` - so a denied
REM or partial kill, the one case worth shouting about, reported the same
REM words as a clean idle box. You then restart, the port is still held,
REM and the new server silently attaches to nothing.
REM
REM taskkill's code cannot answer this on its own either: it succeeds
REM against a pid that had already exited while the real server still holds
REM the port, and fails on a process owned by another user that then dies
REM anyway. So ask the port, after giving the handle a moment to release.
ping -n 2 127.0.0.1 >nul 2>&1
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo *** PORT %PORT% IS STILL HELD. The stop did NOT work. ***
    for /f "tokens=5" %%A in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
        echo     still listening: PID %%A
    )
    echo     Close it by hand -- Task Manager, or: taskkill /PID [pid] /T /F
    echo     Then run "%SELF% stop" again.
    exit /b 1
)

if not defined FOUND (
    echo Nothing was running on port %PORT%.
) else (
    if not defined KILLED echo Port %PORT% is free ^(the process had already gone^).
)
exit /b 0

REM ---------------------------------------------------------------------------
:restart
call "%~f0" stop
REM A socket does not free instantly; give it a moment before rebinding.
timeout /t 2 /nobreak >nul
REM FORWARD THE SECOND ARGUMENT. Without it "restart nowatch" stopped the
REM server and started a WATCHING one, which is the failure that looks like
REM the switch does not work rather than like a script that dropped it.
call "%~f0" start %~2
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
