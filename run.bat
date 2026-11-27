@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Prefer the Windows Python launcher, then python, then python3.
set "PY=py -3"
where py >nul 2>&1
if errorlevel 1 (
  set "PY=python"
  where python >nul 2>&1
  if errorlevel 1 (
    set "PY=python3"
    where python3 >nul 2>&1
    if errorlevel 1 (
      echo Python 3 was not found on PATH.
      echo Install it from https://www.python.org/downloads/
      echo and tick "Add python.exe to PATH" on the first installer screen.
      exit /b 1
    )
  )
)

if not exist "logs" mkdir "logs"

echo.
echo Starting Miner Hearth (source version, browser dashboard).
echo When you see "Dashboard listening", open this in your browser:
echo    http://localhost:8080
echo Leave this window open. Press Ctrl+C to stop.
echo.

%PY% autotune.py --config "%~dp0config.json" %*
set "ERR=%ERRORLEVEL%"
exit /b %ERR%
