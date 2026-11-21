@echo off
rem Shows the running worker or asks Task Scheduler to start the supervisor.
rem
rem The Bypass switch is what makes this work after a zip download: Windows
rem marks unpacked files "from the Internet" and the default RemoteSigned
rem policy then refuses to load an unsigned start-guard.ps1. The script itself
rem clears that mark, so plain "powershell -File .\start-guard.ps1" works after
rem the first run. Only single-line commands are used here on purpose: cmd.exe
rem misreads labels and parenthesised blocks in a file saved with LF endings,
rem and that is exactly what a GitHub source zip contains.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-guard.ps1" && exit /b 0
echo.
echo The worker was not started. Read the PowerShell text above and README.md.
pause
exit /b 1
