@echo off
rem Stop the status bar cleanly (signals the watchdog to quit and not relaunch).
cd /d "%~dp0"
type nul > ".stop"
echo Stopping status bar... (the watchdog will close it within ~1 second)
