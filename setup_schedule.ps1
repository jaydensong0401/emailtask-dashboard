<#
메일 대시보드 자동 실행 — Windows 작업 스케줄러 등록/해제

  .\setup_schedule.ps1            등록
  .\setup_schedule.ps1 -Server    대시보드 서버만 등록 (이미 자동 실행을 쓰고 있을 때)
  .\setup_schedule.ps1 -Status    상태 확인
  .\setup_schedule.ps1 -Remove    해제

등록되는 작업 (현재 사용자, 로그인해 있을 때만 실행 · 관리자 권한 불필요)
  nwmail-quick   10분마다         메일 받기 → 대시보드 갱신   (Claude 사용량 없음)
  nwmail-full    9시·13시·17시    메일 받기 → 업무 정리 → 대시보드 갱신
  nwmail-server  계속 켜 둠       대시보드 서버 http://127.0.0.1:8787 (꺼지면 5분 안에 다시 켬)
PC 가 꺼져 있어 놓친 실행은 켜진 뒤 한 번 실행한다. 창은 뜨지 않는다 (pythonw).
먼저 python set_password.py 로 IMAP 비밀번호를 저장해야 한다.
#>
param([switch]$Remove, [switch]$Status, [switch]$Server)
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$names = @('nwmail-quick', 'nwmail-full', 'nwmail-server')

if ($Status) {
    foreach ($n in $names) {
        $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        if (-not $t) { "{0,-13} 등록 안 됨" -f $n; continue }
        $i = $t | Get-ScheduledTaskInfo
        "{0,-13} {1,-8} 마지막 {2} (결과 {3}) · 다음 {4}" -f $n, $t.State, $i.LastRunTime, $i.LastTaskResult, $i.NextRunTime
    }
    $port = (& python -c "from nwmail.config import load_config; print(load_config().dashboard_port)").Trim()
    try {
        Invoke-WebRequest "http://127.0.0.1:$port/api/ping" -UseBasicParsing -TimeoutSec 3 | Out-Null
        "대시보드 서버  응답함 → http://127.0.0.1:$port/"
    } catch {
        "대시보드 서버  응답 없음 (포트 $port) — 기록: out\server.log"
    }
    return
}

if ($Remove) {
    foreach ($n in $names) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue   # 켜져 있는 서버도 끈다
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            "해제: $n"
        }
    }
    return
}

$python = (& python -c "import sys; print(sys.executable)").Trim()
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe 를 찾지 못했습니다: $pythonw" }
$script = Join-Path $root 'auto.py'

$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited

function New-NwSettings([int]$limitMinutes) {
    # 0 분 = 시간 제한 없음 (서버)
    New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes $limitMinutes)
}

# 대시보드 서버: 5분마다 켜 보되, 이미 켜져 있으면(IgnoreNew) 그대로 둔다 → 꺼지면 5분 안에 다시 켜짐
$serve = Join-Path $root 'serve.py'
$serverTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName 'nwmail-server' -Force -Principal $principal `
    -Description '메일 대시보드: 로컬 서버 (완료·제외·업무삭제 기록). 이 PC 에서만 접속' `
    -Action (New-ScheduledTaskAction -Execute $pythonw -Argument "`"$serve`"" -WorkingDirectory $root) `
    -Trigger $serverTrigger -Settings (New-NwSettings 0) | Out-Null
Start-ScheduledTask -TaskName 'nwmail-server'
"등록: nwmail-server (계속 켜 둠 · 5분마다 확인)"
if ($Server) { return }

$quick = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName 'nwmail-quick' -Force -Principal $principal `
    -Description '메일 대시보드: 10분마다 메일 받기 + 화면 갱신 (Claude 사용 안 함)' `
    -Action (New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`" quick" -WorkingDirectory $root) `
    -Trigger $quick -Settings (New-NwSettings 20) | Out-Null
"등록: nwmail-quick (10분마다)"

# 시각을 바꾸면 대시보드의 '다음 자동 정리' 계산(nwmail/dashboard.py FULL_RUN_TIMES)도 같이 바꾼다
$full = @('09:00', '13:00', '17:00') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
Register-ScheduledTask -TaskName 'nwmail-full' -Force -Principal $principal `
    -Description '메일 대시보드: 메일 받기 + 업무 정리(Claude) + 화면 갱신' `
    -Action (New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`" full" -WorkingDirectory $root) `
    -Trigger $full -Settings (New-NwSettings 90) | Out-Null
"등록: nwmail-full (09:00 · 13:00 · 17:00)"
