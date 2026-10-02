@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   Smart Zain Checker - توليد الشيت النظيف قبل البحث
echo ============================================================
echo.
python smart_audit_cli.py --pick-file --clean-sheet
echo.
pause
