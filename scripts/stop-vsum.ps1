# 停止视频总结 Web 界面(按监听端口找进程杀掉)
$Port = 7860

$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $conns) {
    Write-Host "没有在跑(端口 $Port 没有监听)"
    exit
}

$pidsToKill = @{}
foreach ($c in $conns) {
    $procId = $c.OwningProcess
    $pidsToKill[$procId] = $true
    $parentId = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue).ParentProcessId
    if ($parentId) {
        $parentProc = Get-Process -Id $parentId -ErrorAction SilentlyContinue
        if ($parentProc -and $parentProc.ProcessName -eq 'uv') {
            $pidsToKill[$parentId] = $true
        }
    }
}

foreach ($p in $pidsToKill.Keys) {
    Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
}

Write-Host "已停止"
