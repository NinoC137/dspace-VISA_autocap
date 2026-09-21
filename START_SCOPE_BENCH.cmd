@echo off
setlocal
set "BENCH_PY=%~dp0scope_bench\.venv\Scripts\pythonw.exe"
if not exist "%BENCH_PY%" (
  echo Run SETUP_SCOPE_BENCH.cmd first.
  pause
  exit /b 1
)
start "" "%BENCH_PY%" "%~dp0scope_bench\ui.py"
