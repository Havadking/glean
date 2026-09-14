# 启动视频总结 Web 界面(已在跑就只开浏览器,不重复启动)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Port = 7860
$Url = "http://127.0.0.1:$Port"

function Test-ServerUp {
    try {
        Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 | Out-Null
        return $true
    } catch {
        return $false
    }
}

if (Test-ServerUp) {
    Start-Process $Url
    exit
}

$LogDir = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Start-Process -FilePath 'uv' -ArgumentList 'run', 'vsum', 'ui' `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir 'ui.log') `
    -RedirectStandardError (Join-Path $LogDir 'ui.err.log')

$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    if (Test-ServerUp) {
        Start-Process $Url
        exit
    }
    Start-Sleep -Milliseconds 500
}

Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.MessageBox]::Show(
    "启动超时,看看 logs\ui.err.log 里的报错。",
    "视频总结启动失败"
) | Out-Null
