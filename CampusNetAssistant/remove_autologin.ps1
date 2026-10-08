$ErrorActionPreference = 'Stop'
$TaskName = 'IL AUCT Autologin'

function Say($Text, $Color = 'Gray') { Write-Host $Text -ForegroundColor $Color }

Say ''
Say '  ============================================' 'DarkGray'
Say '    IL AUCT   取消开机加速' 'Cyan'
Say '  ============================================' 'DarkGray'
Say ''

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Say '  [成功] 已取消开机加速' 'Green'
    Say ''
    Say '  自动登录改回由原来的开机自启项负责。' 'Gray'
    Say ''
    exit 0
}
catch {
    Say ''
    Say ('  [提示] ' + $_.Exception.Message) 'Yellow'
    Say ''
    exit 1
}
