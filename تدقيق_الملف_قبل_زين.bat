@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   Smart Zain Checker (Gemini Edition) - 16-Stage Pre-Zain Audit
echo ============================================================
echo.
echo جاري فتح نافذة اختيار ملف الإكسل (Select Excel File)...
python smart_audit_cli.py --pick-file --output-dir "audit_outputs"
echo.
echo تم استخراج تقرير التدقيق الشامل وخطة البحث داخل مجلد: audit_outputs
pause
