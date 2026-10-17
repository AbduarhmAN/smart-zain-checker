@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo Starting Smart Zain Checker (4-Service Multi-Process Architecture)...
python start_services.py
pause
