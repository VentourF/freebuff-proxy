# Freebuff2API 反代启动脚本
# 用法: powershell -ExecutionPolicy Bypass -File start.ps1
# 注意: 注册服务 (register_service.py) 在宿主机本地单独运行, 用 start-register.ps1 启动
# 配置项（按需修改）：
$env:PORT = '8787'                    # 监听端口
$env:HOST = '0.0.0.0'                 # 监听地址（0.0.0.0 = 局域网可访问）
$env:FREEBUFF_API_KEY = 'your-api-key'   # 访问本 API 的密钥（客户端填这个）
$env:FREEBUFF_DEBUG = 'true'          # 调试日志（生产可改 false）
$env:NODE_USE_ENV_PROXY = '1'         # 让 Node fetch 走下面的代理

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
    $env:HTTPS_PROXY = $Proxy
    $env:HTTP_PROXY  = $Proxy
    Write-Host "[proxy] 使用代理: $Proxy" -ForegroundColor Cyan
} else {
    $env:HTTPS_PROXY = ''; $env:HTTP_PROXY = ''
    Write-Host "[proxy] 未探测到代理, 直连" -ForegroundColor Yellow
}
$env:NO_PROXY = 'localhost,127.0.0.1,host.docker.internal'

Set-Location $PSScriptRoot
Write-Host "[start] freebuff2api listening on http://0.0.0.0:$env:PORT  (Ctrl+C 停止)" -ForegroundColor Green
node server.js
