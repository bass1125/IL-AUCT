' ============================================================
'  Campus Network Auto-Login -- silent boot launcher
'
'  No window, no console. Starts the background watcher.
'
'  To DISABLE: delete this file from the Startup folder
'              (press Win+R, type: shell:startup, then Enter)
'              The program itself stays in D:\i_Lian untouched.
'
'  Log: %APPDATA%\campusnet\watch.log
' ============================================================
Option Explicit
On Error Resume Next

Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /c ""D:\i_Lian\campusnet-autologin\run-campusnet.cmd""", 0, False
