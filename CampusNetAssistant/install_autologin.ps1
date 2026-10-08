$ErrorActionPreference = 'Stop'
$TaskName = 'IL AUCT Autologin'
$ExePath  = 'D:\i_Lian\CampusNetAssistant\IL AUCT.exe'
$CfgPath  = Join-Path $env:APPDATA 'campusnet\config.json'

function Say($Text, $Color = 'Gray') { Write-Host $Text -ForegroundColor $Color }

Say ''
Say '  ============================================' 'DarkGray'
Say '    IL AUCT   开机加速' 'Cyan'
Say '  ============================================' 'DarkGray'
Say ''

try {
    if (-not (Test-Path -LiteralPath $ExePath)) {
        throw ('找不到主程序：' + $ExePath)
    }

    $Argument  = 'watch --interval 3 --config "' + $CfgPath + '"'
    $Sid       = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value

    $Action    = New-ScheduledTaskAction -Execute $ExePath -Argument $Argument
    $Trigger   = New-ScheduledTaskTrigger -AtLogOn
    $Settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    $Principal = New-ScheduledTaskPrincipal -UserId $Sid -LogonType Interactive -RunLevel Highest

    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force -ErrorAction Stop | Out-Null

    Say '  [成功] 已开启开机加速' 'Green'
    Say ''
    Say '  登录时会第一时间把守护拉起来，不再排在桌面之后，' 'Gray'
    Say '  通常能提前十几秒通网。重启一次即可看到效果。' 'Gray'
    Say ''
    Say '  原来的开机自启项会保留作兜底，两个一起启动也不冲突。' 'DarkGray'
    Say '  想取消：运行「取消开机加速.bat」' 'DarkGray'
    Say ''
    exit 0
}
catch {
    Say ''
    Say ('  [失败] ' + $_.Exception.Message) 'Red'
    Say ''
    Say '  请把上面的错误信息截图发给开发者。' 'DarkGray'
    Say ''
    exit 1
}
