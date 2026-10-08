@echo off
rem ============================================================
rem  Campus Network Auto-Login -- background watcher
rem  Keeps the campus portal session alive and reconnects when
rem  the connection drops.
rem
rem  You can double-click this file to run the watcher directly.
rem  Log: %APPDATA%\campusnet\watch.log
rem ============================================================
setlocal

set "PY=C:\Users\bass\AppData\Local\Python\pythoncore-3.14-64\python.exe"
set "REPO=D:\i_Lian\campusnet-autologin\campusnet"
set "CFG=%APPDATA%\campusnet\config.json"
set "LOG=%APPDATA%\campusnet\watch.log"

rem force UTF-8 so the log file stays readable in any editor,
rem and unbuffered so lines show up as they happen
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

if not exist "%APPDATA%\campusnet" mkdir "%APPDATA%\campusnet"

rem keep one previous log so the file never grows without bound
if exist "%LOG%" move /y "%LOG%" "%LOG%.old" >nul 2>&1

cd /d "%REPO%"
"%PY%" -m campusnet watch --interval 3 --config "%CFG%" >> "%LOG%" 2>&1
