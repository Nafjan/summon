@echo off
setlocal
rem Windows-safe dispatcher entry point. Do not rely on the .py file association:
rem it may target an older Python that cannot parse Summon's Python 3.10+ syntax.
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Summon requires Python 3.10 or later. Install a current Python and retry. 1>&2
  exit /b 1
)
py -3 "%~dp0run_subagent.py" %*
exit /b %ERRORLEVEL%
