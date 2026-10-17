@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo Starting browsers with proxies...
python open_browsers.py
pause
