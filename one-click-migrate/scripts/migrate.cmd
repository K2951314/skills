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
if "%PY%"=="" set "PY=py"
set "PYTHONUTF8=1"

"%PY%" -c "import sys; sys.path.insert(0, r'%SRC%'); from migrate_engine.cli import main; sys.exit(main())" %*
exit /b %errorlevel%
