@echo off
setlocal
if not exist "%~dp0scope_bench\.venv\Scripts\python.exe" (
  python -m venv "%~dp0scope_bench\.venv"
  if errorlevel 1 goto failed
)
"%~dp0scope_bench\.venv\Scripts\python.exe" -m pip install -r "%~dp0scope_bench\requirements.txt"
if errorlevel 1 goto failed
echo Setup complete. Open START_SCOPE_BENCH.cmd.
exit /b 0
:failed
echo Setup failed.
pause
exit /b 1
