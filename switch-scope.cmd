@echo off
REM ===================================================================
REM  One-click oscilloscope switch (PC-side config + Raspberry Pi pointer)
REM
REM  Double-click        -> list profiles and usage
REM  With arguments      -> run directly, e.g.
REM      switch-scope.cmd tek             select Tektronix MSO58B
REM      switch-scope.cmd mxo38           select Rohde & Schwarz MXO38
REM      switch-scope.cmd tek --dry-run   show what would change, write nothing
REM      switch-scope.cmd list            list profiles
REM      switch-scope.cmd show            show the active connection parameters
REM
REM  NOTE: keep this file ASCII-only and CRLF-terminated. cmd.exe mis-parses
REM  LF-only line endings and non-ASCII bytes; all Chinese output is printed
REM  by switch_scope.py instead.
REM ===================================================================
setlocal
set "HERE=%~dp0"
set "PY=%HERE%scope_bench\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if "%~1"=="" (
    "%PY%" "%HERE%scope_bench\switch_scope.py" list
    echo.
    echo Usage: switch-scope.cmd tek             select Tektronix MSO58B
    echo        switch-scope.cmd mxo38           select Rohde ^& Schwarz MXO38
    echo        switch-scope.cmd tek --dry-run   show changes only
    echo        switch-scope.cmd show            show active parameters
    echo.
    pause
    endlocal
    exit /b 0
)

"%PY%" "%HERE%scope_bench\switch_scope.py" %*
set RC=%ERRORLEVEL%
if not "%SWITCH_SCOPE_NO_PAUSE%"=="1" pause
endlocal & exit /b %RC%
