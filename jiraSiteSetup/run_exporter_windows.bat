@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher "py" was not found. Install Python 3.10 or newer first.
  pause
  exit /b 1
)

if not exist .venv (
  py -m venv .venv
  if errorlevel 1 goto :error
)

call .venv\Scripts\activate.bat
py -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :error

py jira_source_exporter.py %*
set EXITCODE=%ERRORLEVEL%
pause
exit /b %EXITCODE%

:error
echo.
echo Setup or export failed.
pause
exit /b 1
