@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0backend\scripts\start-project.ps1" %*
if errorlevel 1 (
    echo.
    echo Project startup failed. Review the error above.
    pause
    exit /b 1
)
exit /b 0
