@echo off
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel% equ 0 (
  py -3 run.py %*
) else (
  python run.py %*
)
if errorlevel 1 (
  echo Review the message above and docs\QUICK-START.md. Install Python 3.12 or newer if needed.
  pause
)
