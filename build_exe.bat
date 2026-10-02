@echo off
chcp 65001 > nul
echo ========================================================
echo   Building Standalone ZainChecker.exe with PyInstaller
echo ========================================================
echo.

pyinstaller --clean --noconfirm zain_checker.spec

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ========================================================
    echo   BUILD SUCCESSFUL! (100% Self-Contained Binary)
    echo   Executable located at: dist\ZainChecker.exe
    echo   No external folders or files required!
    echo ========================================================
) else (
    echo.
    echo [ERROR] Build failed! Check the output above.
)
pause
