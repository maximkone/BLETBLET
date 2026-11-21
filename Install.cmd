@echo off
rem Registers the per-user Task Scheduler entry. It stays disabled until you
rem run: Enable-ScheduledTask -TaskName "OpenChamber Voice"
rem The Bypass switch and the single-line style: see the notes in Start.cmd.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-guard.ps1" && exit /b 0
echo.
echo The task was not registered. Read the PowerShell text above and README.md.
pause
exit /b 1
