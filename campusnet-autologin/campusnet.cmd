@echo off
rem ============================================================
rem  Campus Network Auto-Login -- command line entry
rem
rem  Open a terminal in this folder and run, or just double-click
rem  this file:
rem    campusnet.cmd status            show current network status
rem    campusnet.cmd doctor            diagnose why auto-login failed
rem    campusnet.cmd login             log in once, manually
rem    campusnet.cmd login --force     force a re-authentication
rem    campusnet.cmd login --verbose   with full request logging
rem ============================================================
rem switch the console to UTF-8 so Chinese output shows correctly
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"

cd /d "D:\i_Lian\campusnet-autologin\campusnet"
"C:\Users\bass\AppData\Local\Python\pythoncore-3.14-64\python.exe" -m campusnet %*
echo.
pause
