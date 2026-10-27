@echo off
setlocal
rem Works from the project root or from the api folder (like start.cmd).
cd /d "%~dp0"
if not exist "run_local.py" cd /d "%~dp0.."
if not exist "run_local.py" (
    echo run_local.py was not found next to this file or in the folder above it.
    goto :failed
)

rem Find Python: prefer "python", fall back to the "py" launcher.
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
    where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo Python is not installed or is not on PATH. Install Python 3.11+ from python.org.
    goto :failed
)

rem First run: create .venv and install dependencies automatically.
if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up the environment. This can take several minutes...
    %PY% run_local.py --setup %*
) else (
    %PY% run_local.py %*
)
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo API startup failed. Review the message above.
pause
exit /b 1
