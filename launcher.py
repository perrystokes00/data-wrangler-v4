r"""
launcher.py — what the Start Menu shortcut actually runs.

Streamlit is a web server, not a desktop app. Double-clicking a shortcut has
to: pick a port nothing else is using, start the server, wait until it is
actually answering, open the browser at it, and then keep the process alive
until the user closes the window.

Run with pythonw.exe so there is no console window. All output goes to a log
file instead — a customer with a blank screen and no log is a support call
with nothing to go on.

WHERE THINGS LIVE, and why they are not all in one place:

  install dir   read-only in practice. Program Files needs admin to write,
                so nothing the app produces can go here. It is also the CWD
                of the server process, which is what makes the shipped
                .streamlit\config.toml win over the customer's own — see
                start_server().
  %LOCALAPPDATA%\DataWranglerV4
                logs, the instance lock, user settings. Per-user and
                writable without elevation. VERSIONED, because v2 may be
                installed on the same machine and must not share any of it.

Getting that wrong is the classic Windows packaging failure: it works for
the developer, who installed to C:\dev, and fails for the customer, who
installed to Program Files.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

try:
    import msvcrt                      # Windows file locking
except ImportError:                    # pragma: no cover - non-Windows dev box
    msvcrt = None                      # type: ignore[assignment]

APP_NAME = "Data Wrangler v4"
ENTRY = "app_v4.py"
DATA_FOLDER = "DataWranglerV4"        # NOT "DataWrangler" — v2 may own that
PORT_FIRST, PORT_LAST = 8501, 8530
STARTUP_TIMEOUT = 90                  # seconds; a cold first run imports a lot

# ONE address, used by the server flag, the readiness poll AND the browser.
# They were three separate literals and it cost an install: the server bound
# 127.0.0.1, the poll checked 127.0.0.1 and passed, and the browser opened
# "localhost" — which Windows resolves to ::1 first. Nothing was listening on
# IPv6, so the launcher logged "serving on 8501" and the customer got "can't
# find page". Anything that must agree should be one value.
BIND_ADDR = "127.0.0.1"

LOCK_NAME = "instance.lock"
PORT_NAME = "running.port"


def app_dir() -> Path:
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Per-user, writable, survives an upgrade, NOT shared with v2."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = Path(base) / DATA_FOLDER
    d.mkdir(parents=True, exist_ok=True)
    return d


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    try:
        with open(data_dir() / "launcher.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    # UNDER pythonw.exe THERE IS NO stdout — sys.stdout is None, and print()
    # raises AttributeError on it. The whole point of this launcher is "no
    # console, log everything", so an unguarded print here can kill the very
    # run it is recording, at the first log line, with nothing on screen.
    # The file write above has already happened; this is only for the case
    # where someone runs the launcher with python.exe to watch it work.
    try:
        print(line)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Instance detection
#
# The old version treated ANY port in 8501-8530 that answered as "us, already
# running". That is wrong the moment a second Streamlit exists on the machine
# — Data Wrangler v2, or a notebook, or another vendor's app. The launcher
# would open the browser at somebody else's server and the customer would be
# looking at the wrong application with no indication anything had gone wrong.
#
# Streamlit's /_stcore/health answers identically for every Streamlit app, so
# HTTP cannot tell them apart either. Instead: one instance holds an exclusive
# lock on a file in OUR data directory, and records its port beside it.
#
# The lock is held by the OS for the life of the process, so a launcher that
# is killed or crashes releases it automatically. That removes the stale-state
# problem a plain PID file has, where a crash leaves a file claiming a port
# that now belongs to something else.
# --------------------------------------------------------------------------
class InstanceLock:
    """Exclusive, self-releasing. acquire() is False when another one holds it."""

    def __init__(self) -> None:
        self._fh = None

    def acquire(self) -> bool:
        path = data_dir() / LOCK_NAME
        try:
            fh = open(path, "a+b")
        except OSError as exc:
            # Cannot lock — do not let that stop the app starting. Worst
            # case we lose duplicate detection, which is a nuisance, not a
            # failure.
            log(f"could not open instance lock ({exc}) — continuing unlocked")
            return True

        if msvcrt is None:
            self._fh = fh
            return True

        try:
            if path.stat().st_size == 0:      # locking needs a byte to lock
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return False                       # someone else is live

        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if msvcrt is not None:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None


def write_port(port: int) -> None:
    try:
        (data_dir() / PORT_NAME).write_text(str(port), encoding="utf-8")
    except OSError as exc:
        log(f"could not record port ({exc})")


def clear_port() -> None:
    try:
        (data_dir() / PORT_NAME).unlink(missing_ok=True)
    except OSError:
        pass


def read_port() -> int | None:
    try:
        return int((data_dir() / PORT_NAME).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def answers(port: int, timeout: float = 0.25) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((BIND_ADDR, port)) == 0


def free_port() -> int | None:
    """First port in the range nothing is listening on.

    Not a fixed 8501: a second copy of the app, a stale process, v2, or any
    other Streamlit on the machine would collide, and the failure looks
    like the app silently not starting.
    """
    for p in range(PORT_FIRST, PORT_LAST + 1):
        if not answers(p):
            return p
    return None


def wait_until_serving(port: int, proc, timeout: int) -> bool:
    """Poll the port until it answers, or the child dies, or we give up.

    Opening the browser on a fixed sleep is the usual shortcut and it shows
    the customer a connection error on a slow machine. Polling costs
    nothing and the first run of a fresh install is genuinely slow.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            log(f"server exited early with code {proc.returncode}")
            return False
        if answers(port, timeout=0.5):
            return True
        time.sleep(0.4)
    log(f"server did not answer within {timeout}s")
    return False


def start_server(entry: Path, port: int):
    # Streamlit's own config precedence is: ~/.streamlit/config.toml, then
    # <cwd>/.streamlit/config.toml, then environment, then CLI flags. CWD IS
    # THE INSTALL DIRECTORY below, which is the whole reason the shipped
    # config.toml applies at all — run the server from anywhere else and the
    # customer's own config (or v2's) decides the theme.
    #
    # The flags that must not be left to a config file are repeated on the
    # command line, where nothing can override them.
    env = dict(os.environ)
    env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    env["STREAMLIT_SERVER_HEADLESS"] = "true"
    env["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] = "false"
    # NOTE: setting HOME here does nothing on Windows — ntpath.expanduser
    # reads USERPROFILE and ignores HOME — and USERPROFILE is always already
    # set, so the old setdefault was a no-op. Overriding USERPROFILE for real
    # would move every "~" the app resolves, including file dialogs, so the
    # shipped config.toml carries this instead.

    # python.exe, not pythonw.exe, for the CHILD: streamlit needs a real
    # stdout. It is hidden below instead.
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe":
        cand = py.with_name("python.exe")
        if cand.exists():
            py = cand

    cmd = [str(py), "-m", "streamlit", "run", str(entry),
           "--server.port", str(port),
           "--server.address", BIND_ADDR,      # localhost only, not the LAN
           "--server.headless", "true",
           "--browser.gatherUsageStats", "false",
           "--server.fileWatcherType", "none"]
    log(f"starting: {' '.join(cmd)}")

    logfile = open(data_dir() / "server.log", "a", encoding="utf-8",
                   errors="replace")
    creation = 0
    if os.name == "nt":
        creation = subprocess.CREATE_NO_WINDOW      # hide the console
    return subprocess.Popen(cmd, cwd=str(entry.parent), env=env,
                            stdout=logfile, stderr=subprocess.STDOUT,
                            creationflags=creation)


def open_browser(url: str) -> bool:
    """Open the default browser at url, and LEAVE A RECORD of what happened.

    webbrowser.open returns a bool that both call sites used to discard, so a
    silent False — which is what "no runnable browser found" looks like — was
    indistinguishable from success. And under pythonw.exe there is no console
    to print a traceback to, so an exception raised in here ended the run with
    the log stopping dead after "serving on N" and nothing to explain it.

    A Start Menu shortcut and an interactive `python launcher.py` do not hand
    the process the same environment. The lines below are what tells those two
    apart after the fact.
    """
    log(f"opening browser at {url}")
    log(f"  python={sys.executable}")
    log(f"  stdout={'None (pythonw)' if sys.stdout is None else 'present'}")
    log(f"  BROWSER={os.environ.get('BROWSER') or '(unset)'}")
    try:
        browser = webbrowser.get()
        log(f"  webbrowser.get() -> {type(browser).__name__} "
            f"name={getattr(browser, 'name', '(n/a)')!r}")
    except Exception as exc:
        # Not fatal on its own — .open() re-resolves internally and can still
        # succeed. Recorded because when it does NOT, "could not locate
        # runnable browser" here is the entire answer.
        log(f"  webbrowser.get() failed: {type(exc).__name__}: {exc}")
    log(f"  webbrowser._tryorder={getattr(webbrowser, '_tryorder', None)!r}")

    try:
        ok = webbrowser.open(url)
        log(f"  webbrowser.open returned {ok!r}")
    except Exception as exc:
        import traceback
        log(f"  webbrowser.open raised {type(exc).__name__}: {exc}")
        log("  " + traceback.format_exc().replace("\n", "\n  ").rstrip())
        ok = False

    if not ok and os.name == "nt":
        # FALLBACK — logged as one, so the lines above stay the diagnosis
        # rather than being papered over. os.startfile hands the URL to the
        # shell, which is what webbrowser's own WindowsDefault does; the
        # difference is that it does not depend on _tryorder having been
        # populated for this environment.
        try:
            os.startfile(url)                  # type: ignore[attr-defined]
            log("  os.startfile fallback: OK")
            ok = True
        except Exception as exc:
            log(f"  os.startfile fallback failed: {type(exc).__name__}: {exc}")

    if not ok:
        log(f"  BROWSER DID NOT OPEN — open this by hand: {url}")
    return ok


def main() -> int:
    here = app_dir()
    entry = here / ENTRY
    if not entry.exists():
        log(f"FATAL: {ENTRY} not found beside the launcher ({here})")
        return 2

    lock = InstanceLock()
    if not lock.acquire():
        # Second shortcut click. Re-open OUR tab rather than starting a
        # second server that will bind a different port and confuse
        # everyone about which one holds their session.
        port = read_port()
        if port is not None and answers(port):
            log(f"already serving on {port} — reopening browser")
            open_browser(f"http://{BIND_ADDR}:{port}")
            return 0
        # Lock held but nothing answering: the other instance is still
        # starting up. Opening a second server here is how you end up with
        # two, so say so and stop.
        log("another instance is starting — nothing to reopen yet")
        return 5

    proc = None
    try:
        port = free_port()
        if port is None:
            log(f"no free port in {PORT_FIRST}-{PORT_LAST}")
            return 3

        proc = start_server(entry, port)

        if not wait_until_serving(port, proc, STARTUP_TIMEOUT):
            log("startup failed — see server.log")
            try:
                proc.terminate()
            except Exception:
                pass
            return 4

        write_port(port)
        log(f"serving on {port}")
        open_browser(f"http://{BIND_ADDR}:{port}")
        log("holding the server open — next log line is at shutdown")

        # Hold the process open. Closing the browser tab does NOT stop a
        # Streamlit server, so something has to own its lifetime; this
        # launcher does, and Task Manager shows one process to end rather
        # than an orphaned server nobody can find.
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
        log("server stopped")
        return 0
    finally:
        clear_port()
        lock.release()
        # Do not leave the server behind. proc.wait() returns when the user
        # stops it, but if this launcher exits any other way the child keeps
        # the port — and the next launch silently lands on a different one.
        # That is why 8501 was still occupied an hour after its launcher had
        # gone and the second start came up on 8502.
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
