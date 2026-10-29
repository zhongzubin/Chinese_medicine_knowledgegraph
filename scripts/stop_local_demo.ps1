[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot "output\local-demo"
$PidFile = Join-Path $RuntimeDir "pids.json"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Host "没有找到本地演示 PID 文件。"
    return
}

$processInfo = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
foreach ($processId in @($processInfo.api_pid, $processInfo.streamlit_pid)) {
    if (-not $processId) {
        continue
    }
    $process = Get-Process -Id ([int]$processId) -ErrorAction SilentlyContinue
    if (-not $process) {
        continue
    }
    if ($process.Path -ne $PythonExe) {
        Write-Warning "跳过 PID $processId：进程路径与本项目 .venv 不一致。"
        continue
    }
    Stop-Process -Id $process.Id -Force
}

Remove-Item -LiteralPath $PidFile -Force
Write-Host "本地演示已停止。"
