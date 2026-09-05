# Freebuff 注册服务本地启动脚本（不依赖 Docker）
# 用法: powershell -ExecutionPolicy Bypass -File start-register.ps1
# 说明:
#   - 注册服务在宿主机本地运行 (FastAPI, 端口 8899), worker 容器经 host.docker.internal:8899 访问
#   - 依赖: OC 协议链路 (仓库 oc\main.py + cf_mail.py) + CloakBrowser/Camoufox
#   - REG_HEADLESS=1 无头(链接授权); 设 0 走有头弹窗手动授权
# OC 目录: 仓库内 oc/（与 register_service/ 同级）
$env:OC_DIR = Join-Path $PSScriptRoot 'oc'
$env:REG_HEADLESS = '1'

# ---- 代理自动探测: 显式指定 > Windows 系统代理 > 常见工具端口 ----
$ProxyOverride = ''                   # 留空=自动; 手动填如 http://127.0.0.1:7897

function Get-SystemProxy {
    try {
        $reg = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
        if ($reg.ProxyEnable -ne 1 -or -not $reg.ProxyServer) { return $null }
        $ps = $reg.ProxyServer.Trim()
        if ($ps -match '^http=') {
            if ($ps -match 'https=([^;]+)') { return $Matches[1].Trim() }
            if ($ps -match 'http=([^;]+)') { return $Matches[1].Trim() }
        }
        if ($ps -match '^socks=') { return $null }   # socks 专用形式不通用, 交给端口探测
        if ($ps -notmatch '^https?://') { $ps = "http://$ps" }
        return $ps
    } catch { return $null }
}

function Test-Port {
    param([int]$Port)
    try {
        $c = [System.Net.Sockets.TcpClient]::new()
        if ($c.ConnectAsync('127.0.0.1', $Port).Wait(250)) { $c.Close(); return $true }
        $c.Close()
    } catch {}
    return $false
}

if ($ProxyOverride) {
    $Proxy = $ProxyOverride
} elseif ($sysProxy = Get-SystemProxy) {
    $Proxy = $sysProxy
} else {
    $Port = @(7897, 7890, 10809, 2080, 8888, 1080) | Where-Object { Test-Port $_ } | Select-Object -First 1
    $Proxy = if ($Port) { "http://127.0.0.1:$Port" } else { '' }
}

$env:REG_PROXY = $Proxy
if ($env:REG_PROXY) { Write-Host "[proxy] REG_PROXY = $env:REG_PROXY" -ForegroundColor Cyan }
else { Write-Host "[proxy] REG_PROXY = 直连（未探测到代理）" -ForegroundColor Yellow }
# 代理轮换池（可选，逗号分隔多个代理；CloakBrowser 授权时随机选一个）
# $env:REG_PROXIES = 'http://127.0.0.1:7897'

Set-Location $PSScriptRoot
Write-Host "[start-register] freebuff register service on http://0.0.0.0:8899  (Ctrl+C 停止)" -ForegroundColor Green
python -u register_service/register_service.py
