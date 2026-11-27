@echo off
REM Start MinerHearth.exe automatically when you sign in to Windows (per-user, no admin needed).
REM Also removes the old v1.6/v1.7 "MinerAutotune" (MinerDashboard.exe) autostart entry.
start "" "%~dp0MinerHearth.exe" --autostart on
