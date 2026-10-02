@echo off
setlocal EnableExtensions
REM ===========================================================================
REM  one-click-migrate engine launcher (Windows)
REM
REM  Pure ASCII + CRLF on purpose: cmd.exe parses batch files by byte
REM  offset, and non-ASCII bytes desync the parser. All Chinese text lives
REM  in the Python engine - this file only selects an interpreter.
REM
REM  Override the interpreter with OC_MIGRATE_PYTHON (e.g. a project venv).
REM ===========================================================================

set "SRC=%~dp0..\src"
if not exist "%SRC%\migrate_engine" (
  echo [ERROR] engine not found: %SRC%\migrate_engine
  exit /b 6
)

set "PY=%OC_MIGRATE_PYTHON%"
set "PYTHONUTF8=1"

if not "%PY%"=="" goto :run

REM Prefer the py launcher (ships with python.org installers). Not every
REM minimal Windows box has it, so fall back to python / py -3 in order.
where py >nul 2>&1 && set "PY=py"
if "%PY%"=="" where python >nul 2>&1 && set "PY=python"
if "%PY%"=="" (
  echo [ERROR] No Python 3.11+ found. Set OC_MIGRATE_PYTHON to a venv python.
  exit /b 6
)

:run
"%PY%" -c "import sys; sys.path.insert(0, r'%SRC%'); from migrate_engine.cli import main; sys.exit(main())" %*
exit /b %errorlevel%
