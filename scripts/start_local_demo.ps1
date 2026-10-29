[CmdletBinding()]
param(
    [int]$ApiPort = 8000,
    [int]$StreamlitPort = 8501,
    [int]$StartupTimeoutSeconds = 900,
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot "output\local-demo"
$PidFile = Join-Path $RuntimeDir "pids.json"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

function Test-TcpPort {
    param(
        [string]$HostName,
        [int]$Port
    )
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $client.Connect($HostName, $Port)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Wait-HttpReady {
    param(
        [string]$Url,
        [int]$TimeoutSeconds,
        [System.Diagnostics.Process]$Process
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) {
            throw "服务进程提前退出，请检查 $RuntimeDir 下的日志。"
        }
        try {
            $response = Invoke-WebRequest -Uri $Url -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                return
            }
        }
        catch {
            Start-Sleep -Seconds 2
        }
    }
    throw "等待服务就绪超时：$Url"
}

Set-Location $ProjectRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "未找到 uv。请先安装 uv 后重试。"
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot ".env"))) {
    throw "未找到 .env。请从 .env.example 创建并配置模型与 Neo4j。"
}
if (Test-Path -LiteralPath $PidFile) {
    throw "检测到已有本地演示 PID 文件。请先运行 scripts\stop_local_demo.ps1。"
}
if (-not (Test-TcpPort "127.0.0.1" 6379)) {
    throw "Redis 未监听 127.0.0.1:6379，请先启动 Redis 服务。"
}
if (-not (Test-TcpPort "127.0.0.1" 7687)) {
    throw "Neo4j 未监听 127.0.0.1:7687，请先启动 Neo4j。"
}
if (Test-TcpPort "127.0.0.1" $ApiPort) {
    throw "端口 $ApiPort 已被占用，请先停止占用进程或指定其他 -ApiPort。"
}
if (Test-TcpPort "127.0.0.1" $StreamlitPort) {
    throw "端口 $StreamlitPort 已被占用，请先停止占用进程或指定其他 -StreamlitPort。"
}

$env:UV_CACHE_DIR = Join-Path $ProjectRoot ".uv-cache"
$env:GRAPH_QA_AUTH_REQUIRED = "true"
$env:GRAPH_QA_REDIS_URL = "redis://127.0.0.1:6379/0"
$env:GRAPH_QA_STREAMLIT_API_URL = "http://127.0.0.1:$ApiPort"

$apiKey = (
    [guid]::NewGuid().ToString("N") +
    [guid]::NewGuid().ToString("N")
).Substring(0, 64)
$env:GRAPH_QA_API_KEYS_JSON = @{
    "local-demo" = $apiKey
} | ConvertTo-Json -Compress
$env:GRAPH_QA_STREAMLIT_API_KEY = $apiKey

Write-Host "同步锁定依赖..."
& uv sync --locked
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $PythonExe)) {
    throw "依赖同步失败。"
}

New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
$apiOut = Join-Path $RuntimeDir "api.out.log"
$apiErr = Join-Path $RuntimeDir "api.err.log"
$uiOut = Join-Path $RuntimeDir "streamlit.out.log"
$uiErr = Join-Path $RuntimeDir "streamlit.err.log"
$apiProcess = $null
$uiProcess = $null

try {
    Write-Host "启动 API..."
    $apiProcess = Start-Process `
        -FilePath $PythonExe `
        -ArgumentList @(
            "-m", "uvicorn", "graph_qa.api:app",
            "--host", "127.0.0.1",
            "--port", "$ApiPort"
        ) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $apiOut `
        -RedirectStandardError $apiErr `
        -PassThru

    Wait-HttpReady `
        -Url "http://127.0.0.1:$ApiPort/health" `
        -TimeoutSeconds $StartupTimeoutSeconds `
        -Process $apiProcess

    Write-Host "启动 Streamlit..."
    $uiProcess = Start-Process `
        -FilePath $PythonExe `
        -ArgumentList @(
            "-m", "streamlit", "run", "graph_qa/streamlit_app.py",
            "--server.address", "127.0.0.1",
            "--server.port", "$StreamlitPort",
            "--server.headless", "true"
        ) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $uiOut `
        -RedirectStandardError $uiErr `
        -PassThru

    Wait-HttpReady `
        -Url "http://127.0.0.1:$StreamlitPort" `
        -TimeoutSeconds 120 `
        -Process $uiProcess

    @{
        api_pid = $apiProcess.Id
        streamlit_pid = $uiProcess.Id
        api_url = "http://127.0.0.1:$ApiPort"
        streamlit_url = "http://127.0.0.1:$StreamlitPort"
    } | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding UTF8

    Write-Host ""
    Write-Host "本地演示已启动："
    Write-Host "  API:       http://127.0.0.1:$ApiPort"
    Write-Host "  Streamlit: http://127.0.0.1:$StreamlitPort"
    Write-Host "  日志目录:  $RuntimeDir"
    Write-Host "停止服务:  pwsh -File scripts\stop_local_demo.ps1"
    if ($OpenBrowser) {
        Start-Process "http://127.0.0.1:$StreamlitPort"
    }
}
catch {
    if ($uiProcess -and -not $uiProcess.HasExited) {
        Stop-Process -Id $uiProcess.Id -Force
    }
    if ($apiProcess -and -not $apiProcess.HasExited) {
        Stop-Process -Id $apiProcess.Id -Force
    }
    throw
}
