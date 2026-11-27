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

set "HOST=%~1"
if "%HOST%"=="" set "HOST=192.168.1.100"

echo.
echo Probing %HOST% — this prints raw responses from the miner APIs.
echo Copy the output if you need help diagnosing a missing NerdOCTAxe.
echo.

%PY% autotune.py --probe "%HOST%"
set "ERR=%ERRORLEVEL%"
echo.
pause
exit /b %ERR%
