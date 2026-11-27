@echo off
REM Stop MinerHearth.exe (and the old MinerDashboard.exe entry) from starting automatically at sign-in.
start "" "%~dp0MinerHearth.exe" --autostart off
