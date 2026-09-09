@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo Starting Local Video Cutter...

if exist "LocalVideoCutter.exe" goto launch_executable

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -I -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
  if not errorlevel 1 goto launch_venv
)

where py >nul 2>&1
if not errorlevel 1 (
  py -3.12 -I -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
  if not errorlevel 1 goto launch_py
)

if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
  goto launch_installed
)

if exist "%ProgramFiles%\Python312\python.exe" (
  goto launch_machine
)

where python >nul 2>&1
if not errorlevel 1 (
  python -I -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
  if not errorlevel 1 goto launch_path
)

where winget >nul 2>&1
if errorlevel 1 (
  echo Python 3.12 is required and Windows Package Manager was not found.
  echo Install App Installer from Microsoft Store, then run start.bat again.
  goto failed
)

echo Python 3.12 is missing. Installing it with Windows Package Manager...
winget install --id Python.Python.3.12 --exact --source winget --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
if errorlevel 1 (
  winget install --id Python.Python.3.12 --exact --source winget --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
)

if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
  goto launch_installed
)
if exist "%ProgramFiles%\Python312\python.exe" (
  goto launch_machine
)
where py >nul 2>&1
if not errorlevel 1 (
  py -3.12 -I -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>&1
  if not errorlevel 1 goto launch_py
)

echo Python 3.12 could not be installed or located.
goto failed

:launch_venv
".venv\Scripts\python.exe" launcher_windows.py
set "TASK_EXIT_CODE=%ERRORLEVEL%"
goto finished

:launch_py
py -3.12 launcher_windows.py
set "TASK_EXIT_CODE=%ERRORLEVEL%"
goto finished

:launch_installed
"%LocalAppData%\Programs\Python\Python312\python.exe" launcher_windows.py
set "TASK_EXIT_CODE=%ERRORLEVEL%"
goto finished

:launch_machine
"%ProgramFiles%\Python312\python.exe" launcher_windows.py
set "TASK_EXIT_CODE=%ERRORLEVEL%"
goto finished

:launch_path
python launcher_windows.py
set "TASK_EXIT_CODE=%ERRORLEVEL%"
goto finished

:failed
set "TASK_EXIT_CODE=1"
goto show_error

:launch_executable
"LocalVideoCutter.exe"
exit /b %ERRORLEVEL%

:finished
if "%TASK_EXIT_CODE%"=="0" exit /b 0

:show_error
echo.
echo Startup stopped. Review the message above.
pause
exit /b %TASK_EXIT_CODE%
