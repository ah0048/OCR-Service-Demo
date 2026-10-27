@echo off
setlocal
cd /d "%~dp0.."

where docker >nul 2>&1
if errorlevel 1 (
    echo Docker is not installed or is not on PATH. Install Docker Desktop first.
    goto :failed
)
docker info >nul 2>&1
if errorlevel 1 (
    echo Docker's engine is not ready. Start Docker Desktop and wait for it to finish starting.
    goto :failed
)

if /i "%~1"=="--build" goto :build
if not "%~1"=="" (
    echo Usage: start_docker_cuda.cmd [--build]
    goto :failed
)
docker image inspect front-id-api:production >nul 2>&1
if errorlevel 1 goto :build
docker compose -f compose.yaml -f compose.cuda.yaml up -d --no-build
goto :started

:build
docker compose -f compose.yaml -f compose.cuda.yaml up -d --build

:started
if errorlevel 1 goto :failed
echo.
echo API container started in CUDA mode. Models may still be warming up.
echo Open http://localhost:8000 when the API is ready.
echo Press Ctrl+C to stop viewing logs; the container keeps running.
echo To stop the API: docker compose -f compose.yaml -f compose.cuda.yaml stop
docker compose -f compose.yaml -f compose.cuda.yaml logs --since 1m -f
exit /b 0

:failed
echo.
echo API startup failed. Review the message above.
pause
exit /b 1

