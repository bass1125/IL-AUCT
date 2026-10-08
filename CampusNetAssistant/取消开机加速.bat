@echo off
setlocal
title IL AUCT Disable Fast Boot

net session >nul 2>&1
if %errorlevel%==0 goto :admin

echo.
echo   Administrator rights are required.
echo   A UAC prompt will appear - please click "Yes".
echo.
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:admin
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0remove_autologin.ps1"
echo.
pause
exit /b
