@echo off
cd /d "%~dp0.."
python -m api.server --device cuda --port 8000 %*
