# Freebuff2API Docker 启动脚本（自动跟随宿主机代理）
# 用法: powershell -ExecutionPolicy Bypass -File start-docker.ps1
# 说明: 容器读不到 Windows 注册表, 本脚本在宿主机探测系统代理/常见端口,
#       转换为 host.docker.internal:端口 注入 HTTPS_PROXY, 再拉起 compose。
#       首次构建较慢; 之后可改用 docker compose up -d 直拉。

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

if ($Proxy) {
    $Port = [regex]::Match($Proxy, ':(\d+)/?$').Groups[1].Value
    $env:HTTPS_PROXY = "http://host.docker.internal:$Port"
    $env:HTTP_PROXY  = "http://host.docker.internal:$Port"
    Write-Host "[proxy] 容器代理: http://host.docker.internal:$Port (来自宿主机: $Proxy)" -ForegroundColor Cyan
} else {
    $env:HTTPS_PROXY = ''; $env:HTTP_PROXY = ''
    Write-Host "[proxy] 未探测到代理, 容器直连" -ForegroundColor Yellow
}
$env:NO_PROXY = 'localhost,127.0.0.1,host.docker.internal'

Set-Location $PSScriptRoot
Write-Host "[docker] building & starting freebuff2api ..." -ForegroundColor Green
docker compose up -d --build
