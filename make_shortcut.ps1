<#
바탕화면에 '메일 대시보드' 바로가기를 만든다 (더블클릭하면 대시보드가 열린다).

  .\make_shortcut.ps1           만들기 (이미 있으면 덮어씀)
  .\make_shortcut.ps1 -Remove   지우기

바로가기는 open_dashboard.py 를 창 없이(pythonw) 실행한다. 서버가 꺼져 있으면
먼저 켜고 열기 때문에, PC 를 막 켠 직후에 눌러도 된다.
#>
param([switch]$Remove)
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$link = Join-Path ([Environment]::GetFolderPath('Desktop')) '메일 대시보드.lnk'

if ($Remove) {
    if (Test-Path $link) { Remove-Item $link; "지움: $link" } else { "없음: $link" }
    return
}

$python = (& python -c "import sys; print(sys.executable)").Trim()
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe 를 찾지 못했습니다: $pythonw" }

# 아이콘은 브라우저 것을 빌려 쓴다 (웹 화면이 열린다는 걸 보여준다). 없으면 파이썬 아이콘
$icon = @(
    "$env:ProgramFiles (x86)\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $icon) { $icon = $pythonw }

$s = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
$s.TargetPath = $pythonw
$s.Arguments = "`"$(Join-Path $root 'open_dashboard.py')`""
$s.WorkingDirectory = $root
$s.IconLocation = "$icon,0"
$s.Description = '메일 업무 대시보드 열기 (http://127.0.0.1:8787)'
$s.Save()

"만듦: $link"
"  더블클릭하면 대시보드가 열립니다."
