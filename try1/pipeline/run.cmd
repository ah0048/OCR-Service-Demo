@echo off
cd /d "%~dp0.."
python -m pipeline.front_id %*
