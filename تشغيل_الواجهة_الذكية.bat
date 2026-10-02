@echo off
chcp 65001 >nul
cd /d "E:\Projects\smart_zainchecker_gemini"
echo ============================================================
echo   Smart Zain Checker (Gemini Edition) - تشغيل الواجهة الذكية
echo   المجلد النشط: E:\Projects\smart_zainchecker_gemini
echo ============================================================
echo.
echo جاري إيقاف أي نسخة سابقة على المنفذ 5050...
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 5050 -ErrorAction SilentlyContinue; if ($c) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }"
echo.
echo جاري تشغيل خادم الواجهة الذكية وفتح المتصفح على http://127.0.0.1:5050 ...
python -m zain_checker.web_bridge
pause
