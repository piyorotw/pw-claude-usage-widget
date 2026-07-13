@echo off
rem Launch the status bar (via watchdog) silently using the local venv.
cd /d "%~dp0"
if exist ".stop" del ".stop"
start "" ".venv\Scripts\pythonw.exe" "watchdog.pyw"
