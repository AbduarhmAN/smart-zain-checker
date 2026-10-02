@echo off
cd /d "%~dp0"
python -B app.py --port 5051 --bridge-port 8767 --no-telegram --no-proxy-workers
pause
