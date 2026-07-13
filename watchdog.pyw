"""Watchdog: keep the status bar alive no matter what.

Launches main.py and relaunches it whenever it exits (crash, segfault, or being
killed by the shell). Combined with the in-process self-heal in bar.py, the strip
survives both window destruction and full process death.

Stop everything cleanly by creating the sentinel file `.stop` (stop.bat does this).
"""
import ctypes
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STOP = HERE / ".stop"
LOG = HERE / "statusbar.log"
ERROR_ALREADY_EXISTS = 183


def _single_instance() -> bool:
    """Return False if another watchdog is already running (named mutex)."""
    if sys.platform != "win32":
        return True
    ctypes.windll.kernel32.CreateMutexW(None, False, "StatusBar_Watchdog_singleton")
    return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS

pyw = HERE / ".venv" / "Scripts" / "pythonw.exe"
PY = str(pyw if pyw.exists() else sys.executable)
MAIN = str(HERE / "main.py")


def log(msg: str):
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%H:%M:%S} [watchdog] {msg}\n")
    except OSError:
        pass


def main():
    if not _single_instance():
        log("another watchdog is already running — exiting")
        return
    STOP.unlink(missing_ok=True)          # clear any stale sentinel
    log("watchdog start")
    fails = 0
    while not STOP.exists():
        start = time.time()
        proc = subprocess.Popen([PY, MAIN], cwd=str(HERE))
        # wait for the child, but stay responsive to the stop sentinel
        while proc.poll() is None:
            if STOP.exists():
                proc.terminate()
                log("stop requested - terminating strip")
                STOP.unlink(missing_ok=True)
                return
            time.sleep(0.5)
        # child exited on its own
        alive = time.time() - start
        fails = fails + 1 if alive < 5 else 0
        backoff = min(30, 2 * fails) if fails else 2
        log(f"strip exited code={proc.returncode} after {alive:.0f}s — relaunch in {backoff}s")
        time.sleep(backoff)
    STOP.unlink(missing_ok=True)
    log("watchdog stop")


if __name__ == "__main__":
    main()
