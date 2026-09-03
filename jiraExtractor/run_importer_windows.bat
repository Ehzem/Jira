@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  set PY=py
) else (
  set PY=python
)

%PY% -m pip install -r requirements.txt
if errorlevel 1 goto :error

%PY% validate_package.py "%~1"
if errorlevel 1 goto :error

%PY% jira_destination_importer.py %*
set EXIT_CODE=%errorlevel%
echo.
if %EXIT_CODE%==0 (
  echo Importer finished. Review the report folder shown above.
) else (
  echo Importer stopped with exit code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%

:error
echo.
echo Setup or package validation failed. Make sure Python 3.10 or newer is installed.
pause
exit /b 1
