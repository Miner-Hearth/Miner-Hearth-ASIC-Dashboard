@echo off
REM Rebuild MinerHearth.exe on Windows (only needed if you change the source).
REM Requires 64-bit Python 3.9+ on PATH. Installs PyInstaller + pywebview into a local .venv-build folder.
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=py -3"
where py >nul 2>&1 || set "PY=python"
if not exist ".venv-build\Scripts\python.exe" (
  %PY% -m venv .venv-build || goto :fail
)
".venv-build\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv-build\Scripts\python.exe" -m pip install -r packaging\requirements-build.txt || goto :fail
".venv-build\Scripts\python.exe" -m PyInstaller --noconfirm --clean --distpath . --workpath build packaging\MinerHearth.spec || goto :fail
echo.
echo Built %~dp0MinerHearth.exe
exit /b 0
:fail
echo Build failed.
exit /b 1
