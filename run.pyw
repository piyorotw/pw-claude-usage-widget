"""Silent launcher (double-clickable, no console). Starts the watchdog + strip."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
(HERE / ".stop").unlink(missing_ok=True)
pyw = HERE / ".venv" / "Scripts" / "pythonw.exe"
python = pyw if pyw.exists() else Path(sys.executable)
subprocess.Popen([str(python), str(HERE / "watchdog.pyw")], cwd=str(HERE))
