@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_VL120.ps1" -Run
set "SWEEP_EXIT=%ERRORLEVEL%"
pause
exit /b %SWEEP_EXIT%
