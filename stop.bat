@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto stop_venv

where py >nul 2>&1
if not errorlevel 1 (
  py -3.12 -I -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
  if not errorlevel 1 goto stop_py
)

if exist "%LocalAppData%\Programs\Python\Python312\python.exe" goto stop_installed

if exist "%ProgramFiles%\Python312\python.exe" goto stop_machine

where python >nul 2>&1
if not errorlevel 1 (
  python -I -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
  if not errorlevel 1 goto stop_path
)

echo Python was not found.
set "TASK_EXIT_CODE=1"
goto show_result

:stop_venv
".venv\Scripts\python.exe" stop.py
set "TASK_EXIT_CODE=%ERRORLEVEL%"
goto show_result

:stop_py
py -3.12 stop.py
set "TASK_EXIT_CODE=%ERRORLEVEL%"
goto show_result

:stop_installed
"%LocalAppData%\Programs\Python\Python312\python.exe" stop.py
set "TASK_EXIT_CODE=%ERRORLEVEL%"
goto show_result

:stop_machine
"%ProgramFiles%\Python312\python.exe" stop.py
set "TASK_EXIT_CODE=%ERRORLEVEL%"
goto show_result

:stop_path
python stop.py
set "TASK_EXIT_CODE=%ERRORLEVEL%"

:show_result
pause
exit /b %TASK_EXIT_CODE%
