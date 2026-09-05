# -*- coding: utf-8 -*-
"""freebuff 账号注册服务
复用仓库内 oc/ 的 GitHub 协议注册链路 + freebuff OAuth 浏览器授权 + cli 逆向链路。

流程:
  1. (可选) CF 临时邮箱自动创建
  2. GitHub 协议注册 (OC main.OpenCodeClient.github_signup)
  3. Playwright 无头浏览器: 带 GitHub cookie 走 freebuff OAuth 授权
     (GitHub authorize -> freebuff.com/api/auth/callback/github -> session cookie)
  4. 逆向链路: POST /api/auth/cli/code -> 浏览器 /onboard?auth_code 绑定
     -> GET /api/auth/cli/status 拿 authToken
  recaptcha 拦截时进入 wait_manual 模式, 返回授权链接让用户手动完成。

启动: python register_service.py   ->  http://127.0.0.1:8899
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
import time
import uuid
from typing import Optional
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
# OC 协议链路目录: 容器内为 /app/oc (register_service/Dockerfile COPY)，本地为仓库 oc/ 目录
OC_DIR = os.environ.get("OC_DIR") or os.path.normpath(os.path.join(BASE, "..", "oc"))
if OC_DIR not in sys.path:
    sys.path.insert(0, OC_DIR)

# 容器化支持: Docker 内无桌面, headless=True 且跳过弹窗手动模式 (走链接授权)
REG_HEADLESS = os.environ.get("REG_HEADLESS", "0") == "1"

# Windows 下强制 UTF-8 输出, 避免 GBK 无法编码 emoji 导致崩溃
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

import main as oc_main
import cf_mail

# ============ freebuff OAuth 常量覆盖 (OC 默认是 opencode) ============
oc_main.GITHUB_OAUTH_CLIENT_ID = "Ov23liBmPZjygPRVpLQs"
oc_main.GITHUB_OAUTH_REDIRECT = "https://freebuff.com/api/auth/callback/github"
oc_main.GITHUB_OAUTH_SCOPE = "read:user user:email"

FREEBUFF_WEB = "https://freebuff.com"
DEFAULT_PROXY = os.environ.get("REG_PROXY", "http://127.0.0.1:7897")
# 代理轮换池：REG_PROXIES 逗号分隔（优先级高于 REG_PROXY），每次随机选一个
PROXY_POOL = os.environ.get("REG_PROXIES", "").split(",") if os.environ.get("REG_PROXIES") else [DEFAULT_PROXY]
DEFAULT_COUNTRY = "GB"

# 线程级任务上下文：允许按注册任务临时指定自定义代理
_TASK_CTX = threading.local()


def _pick_proxy() -> str:
    """返回当前任务代理；空字符串表示直连，不使用代理。"""
    custom = getattr(_TASK_CTX, "proxy", None)
    if custom is not None:
        return custom
    return random.choice(PROXY_POOL)


def _set_task_proxy(proxy: str) -> None:
    """为当前注册任务线程设置自定义代理 (仅对该线程内后续调用生效)。"""
    _TASK_CTX.proxy = proxy or ""

# 常见美国时区（与代理出口 IP 一致，避免时区指纹暴露）
_US_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Phoenix",
    "America/Detroit",
]
# 常见桌面 viewport（宽 x 高）
_DESKTOP_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
    {"width": 1920, "height": 1200},
    {"width": 1680, "height": 1050},
    {"width": 1600, "height": 900},
    {"width": 1280, "height": 800},
]

def _random_timezone() -> str:
    """随机选一个美国时区。"""
    return random.choice(_US_TIMEZONES)

def _random_viewport() -> dict:
    """随机选一个桌面 viewport。"""
    return random.choice(_DESKTOP_VIEWPORTS)


# ============ CloakBrowser 替换 OC 的 camoufox (DataDome 求解更稳定) ============


# ============ CloakBrowser 替换 OC 的 camoufox (DataDome 求解更稳定) ============
def _cloak_solve_datadome(self, signup_url: str, return_to: str = ""):
    """CloakBrowser 版 DataDome 403 求解, 替代 OC 原 camoufox 版。

    行为与原版一致: 浏览器打开 signup 页 -> DataDome 挑战自动通过 ->
    等待表单渲染 -> 导出 github.com cookies + HTML 回灌协议 session。
    返回模拟的 Response (200 + html)。
    """
    from cloakbrowser import launch

    self.log("检测到 DataDome 风控 (403)，启动 CloakBrowser 无头求解...")

    browser = None
    html = ""
    cookies = []
    current_url = ""
    used_proxy = ""
    try:
        proxy = _pick_proxy()
        used_proxy = proxy
        tz = _random_timezone()
        vp = _random_viewport()
        launch_kwargs = {
            "headless": True,
            "timezone": tz,
            "locale": "en-US",
            "humanize": True,
            "args": ["--fingerprint-platform=windows"],
        }
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = launch(**launch_kwargs)
        page = browser.new_page(viewport=vp)

        # Step 1: 预热 github.com (拿初始 cookie)
        self.log("CloakBrowser 预热 github.com...")
        try:
            page.goto("https://github.com", wait_until="networkidle", timeout=30000)
        except Exception:
            pass

        # Step 2: 访问 signup 页面
        self.log(f"CloakBrowser 访问 signup: {signup_url[:100]}")
        try:
            page.goto(signup_url, wait_until="networkidle", timeout=60000)
        except Exception:
            pass

        # Step 3: 等待 DataDome 挑战自动通过
        self.log("等待 DataDome 处理...")
        page.wait_for_timeout(8000)
        current_url = page.url
        self.log(f"CloakBrowser 当前: {current_url}")

        # Step 4: 等待 signup 表单渲染, 未渲染则刷新重试 (挑战有时卡住)
        email_input = None
        for refresh_round in range(4):
            for wait_round in range(3):
                try:
                    page.wait_for_selector('input[name="user[email]"], #email', timeout=8000)
                    email_input = page.query_selector('input[name="user[email]"], #email')
                    if email_input:
                        break
                except Exception:
                    pass
                self.log(f"  等待 signup 表单渲染 ({refresh_round+1}-{wait_round+1})...")
                page.wait_for_timeout(2000)
            if email_input:
                break
            self.log(f"表单未渲染, 刷新页面重试 ({refresh_round+1}/4)...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(4000)

        if not email_input:
            # 保存 HTML 用于调试
            try:
                debug_html = page.content()
                debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug", "signup_debug.html")
                os.makedirs(os.path.dirname(debug_path), exist_ok=True)
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(debug_html)
                self.log(f"[调试] signup 页 HTML 已保存: {debug_path} ({len(debug_html)} 字符)")
            except Exception:
                pass
            raise oc_main.ProtocolError(
                f"CloakBrowser 未能通过 DataDome 挑战 (signup 表单未渲染)。"
                f"请用真实浏览器打开后刷新完成挑战:\n{signup_url}\n"
                f"拿到页面后复制 datadome cookie 值 (从 DevTools -> Application -> Cookies), 再重试。"
            )

        html = page.content()
        current_url = page.url
        cookies = page.context.cookies()

        new_datadome = ""
        for c in cookies:
            if c["name"] == "datadome":
                new_datadome = c["value"]
                break
        self.log(f"CloakBrowser 获取 datadome cookie: {new_datadome[:40]}...")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    # 同步浏览器 cookies 到协议 session (与 OC 原版一致)
    # 关键: 必须同步 _gh_sess — signup 页的 authenticity_token 绑定会话,
    # POST /signup 必须用同一个 _gh_sess 才能通过 CSRF 校验,否则返回 422
    # 统一用 .github.com domain,避免和服务器 set-cookie 的 domain 冲突产生重复 cookie
    for cookie_info in cookies:
        name = cookie_info["name"]
        value = cookie_info["value"]
        domain = cookie_info.get("domain", ".github.com")
        if "github.com" in domain:
            try:
                del self.session.cookies[name]
            except Exception:
                pass
            self.session.cookies.set(name, value, domain=".github.com")

    # 构造一个模拟的 Response 对象, 包含 CloakBrowser 返回的 HTML
    import requests as _req_module
    fake_response = _req_module.Response()
    fake_response.status_code = 200
    fake_response.url = current_url or signup_url
    fake_response._content = html.encode("utf-8")
    fake_response.encoding = "utf-8"

    self.log(f"CloakBrowser 成功加载 signup 页面: {len(html)} 字符")
    return fake_response


oc_main.OpenCodeClient._solve_datadome = _cloak_solve_datadome

app = FastAPI(title="freebuff register service")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

TASKS: dict = {}
_LOCK = threading.Lock()


def _log(t: dict, msg: str, level: str = "info"):
    t["logs"].append({"ts": time.strftime("%H:%M:%S"), "level": level, "msg": msg})
    print(f"[{t['id']}] {msg}")


def _load_oc_config() -> dict:
    cfg_path = os.path.join(OC_DIR, "web_config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return {}
    # 容器内 (REG_HEADLESS=1): web_config.json 里的 127.0.0.1 代理指向容器自身,
    # 必须替换为宿主机代理地址 (REG_PROXY=host.docker.internal:7897)
    if REG_HEADLESS:
        cfg["proxy"] = DEFAULT_PROXY
    return cfg


def _save_oc_config(cfg: dict) -> None:
    """写回 OC 的 web_config.json（保留原代理/country，只更新传入字段）。"""
    cfg_path = os.path.join(OC_DIR, "web_config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cur = json.load(f)
    except Exception:
        cur = {}
    cur.update(cfg)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)


def _export_github_cookies(session) -> list:
    """从 curl_cffi session 导出 GitHub 域 cookie (playwright 格式)。"""
    out = []
    try:
        for c in session.cookies.jar:
            dom = (c.domain or "")
            if "github.com" not in dom:
                continue
            rest = getattr(c, "rest", {}) or {}
            out.append({
                "name": c.name,
                "value": c.value,
                "domain": dom.lstrip(".") if dom.startswith(".") else dom,
                "path": c.path or "/",
                "secure": bool(getattr(c, "secure", True)),
                "httpOnly": bool(rest.get("HttpOnly", False)),
                "sameSite": "Lax",
            })
    except Exception as e:
        print("[cookie export warn]", e)
    return out


def _wait_freebuff_session(ctx, timeout: float = 60.0) -> Optional[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for c in ctx.cookies():
            if c["name"] == "__Secure-next-auth.session-token" and "freebuff.com" in c.get("domain", ""):
                return c["value"]
        time.sleep(1)
    return None


# GitHub authorize 按钮选择器 (按优先级逐个尝试, 只匹配授权按钮)
# 注意: 绝不能包含 form[action*="oauth/authorize"] button[type="submit"] 这类
# 模糊选择器——它会匹配到表单里的 deny/Cancel 按钮, 导致 access_denied。
AUTHORIZE_BTN_SELECTORS = [
    "button[name='authorize']",
    "input[name='authorize']",
    "#js-oauth-authorize-btn",
    "form[action*='oauth/authorize'] button[name='authorize']",
    "form[action*='oauth/authorize'] input[name='authorize']",
]


def _detect_auth_state(page) -> tuple:
    """检测 GitHub OAuth 页面状态, 基于 DOM 而非 URL 字符串。
    返回 (state, url):
      freebuff   - 已跳转到 freebuff.com (授权完成)
      login_form - 出现登录表单 (#login_field), 需要浏览器内登录
      authorize  - 出现授权确认按钮, 可点击
      captcha    - captcha / DataDome 挑战页
      unknown    - 其他 (可能未渲染完成)
    """
    url = page.url
    # 必须按 host 判断: authorize URL 的 redirect_uri / return_to 查询参数
    # 里就含有字面 "freebuff.com", 子串匹配会误判为已跳转
    try:
        netloc = (urlparse(url).netloc or "").lower()
    except Exception:
        netloc = ""
    if netloc == "freebuff.com" or netloc.endswith(".freebuff.com"):
        # NextAuth 回调失败会重定向到 /api/auth/signin?error=Callback
        if "error=callback" in url.lower():
            return "callback_error", url
        return "freebuff", url
    try:
        if page.locator("#login_field").count() > 0:
            return "login_form", url
    except Exception:
        pass
    for sel in AUTHORIZE_BTN_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                return "authorize", url
        except Exception:
            pass
    low = url.lower()
    if "captcha" in low or "challenge" in low or "datadome" in low:
        return "captcha", url
    return "unknown", url


def browser_authorize_freebuff(t: dict, client) -> dict:
    """用 CloakBrowser (源码级反检测 Chromium) 完成 freebuff OAuth 授权, 返回 session token。

    不再直连自定义 OAuth App 的 authorize URL: 自定义 App 生成的 code 在
    freebuff 服务端换 token 会失败 (client_secret 不匹配, 回调报 error=Callback)。
    改为走 freebuff 官方登录:
      1. 注入协议注册的 GitHub cookies (剔除 datadome, 其与产生它的指纹绑定,
         换新浏览器实例注入反而触发挑战)
      2. github.com/login 检查登录态, cookie 无效则用协议注册的账号密码登录
      3. 打开 freebuff.com/login 点击官方 "Continue with GitHub"
      4. GitHub 授权页 (freebuff 官方 App) -> 状态机点击 Authorize
      5. 跳回 freebuff.com -> 提取 __Secure-next-auth.session-token
    """
    from cloakbrowser import launch

    gh_cookies = _export_github_cookies(client.session)
    username = getattr(client, "_signup_username", "") or ""
    password = getattr(client, "_signup_password", "") or ""
    _log(t, f"CloakBrowser 授权 freebuff (cookies: {len(gh_cookies)} 个, 账号: {username})...")

    debug_dir = os.path.join(BASE, "debug")
    try:
        os.makedirs(debug_dir, exist_ok=True)
    except Exception:
        debug_dir = BASE

    browser = None
    session_token = None
    final_url = ""
    used_proxy = ""
    try:
        # 有头模式: freebuff 登录页的 reCAPTCHA v3 对纯自动化评分极低 (0.1-0.2),
        # 真人点击才能通过 signup-challenge, 故弹出窗口由操作者点击一次
        # 容器模式 (REG_HEADLESS=1) 无桌面, 用 headless + 自动重试, 失败走链接授权
        # 指纹加强: 随机美国时区 + 伪装 windows 平台 + 随机桌面 viewport + 代理轮换
        proxy = _pick_proxy()
        used_proxy = proxy
        tz = _random_timezone()
        vp = _random_viewport()
        _log(t, f"CloakBrowser 授权: proxy={proxy or '直连'} timezone={tz} viewport={vp['width']}x{vp['height']}", "info")
        launch_kwargs = {
            "headless": REG_HEADLESS,
            "timezone": tz,
            "locale": "en-US",
            "humanize": True,
            "args": ["--fingerprint-platform=windows"],
        }
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = launch(**launch_kwargs)
        ctx = browser.new_context(locale="en-US", timezone_id=tz, viewport=vp)
        try:
            # 注入协议注册的 GitHub cookies (剔除 datadome, 其与产生它的指纹绑定,
            # 换新浏览器实例注入反而触发挑战)
            # 同时预置 freebuff 人机验证通过标记: 服务端在校验通过后会下发两个 HttpOnly cookie
            #   freebuff_signup_challenge=ok  +  freebuff_signup_recaptcha_v3=ok:<score>
            # (抓包确认: 手动通过时 score=0.6)。这两个是 unsigned cookie, callback 阶段
            # 只读它们判断是否通过验证, 因此注入正确的 ok 前缀值即可绕过 reCAPTCHA v3 评分。
            gh_cookie_list = [c for c in gh_cookies if c.get("name") != "datadome"]
            gh_cookie_list += [
                {"name": "freebuff_signup_challenge", "value": "ok",
                 "domain": "freebuff.com", "path": "/",
                 "secure": True, "httpOnly": True, "sameSite": "Lax"},
                {"name": "freebuff_signup_recaptcha_v3", "value": "ok:0.9",
                 "domain": "freebuff.com", "path": "/",
                 "secure": True, "httpOnly": True, "sameSite": "Lax"},
            ]
            ctx.add_cookies(gh_cookie_list)
        except Exception as e:
            _log(t, f"cookie 注入警告: {e}", "warn")

        # 拦截 signup-challenge 上报: 前端仍会执行 Turnstile/reCAPTCHA 求解, 但把 token
        # 上报给服务端的那一步 abort 掉, 避免服务端把上面的 ok cookie 覆盖成 failed
        # (自动化 token 评分低时服务端返回 403 并下发 failed cookie)。
        def _abort_signup_challenge(route):
            _log(t, "已拦截 signup-challenge 上报 (abort), 使用预置 ok cookie", "info")
            try:
                route.abort()
            except Exception:
                route.continue_()
        try:
            ctx.route("**/api/auth/signup-challenge", _abort_signup_challenge)
        except Exception as e:
            _log(t, f"signup-challenge 拦截注册失败: {e}", "warn")

        page = ctx.new_page()

        # 监听 auth 请求/响应, 失败时输出详细错误
        _auth_captures = []
        _auth_requests = []
        # signup-challenge 明细: 每次点击后单独输出, 便于区分
        #   - 未触发 POST (reCAPTCHA/Turnstile 未产出 token → 服务端报 recaptcha_missing)
        #   - 触发但被拒 (token 评分低 → 服务端报 recaptcha_invalid)
        _challenge_log = []
        def _on_auth_request(req):
            u = req.url
            if "freebuff.com" in u and "/api/auth/" in u:
                _auth_requests.append(u[:200])
                if "signup-challenge" in u:
                    try:
                        post = (req.post_data or "")[:400]
                    except Exception:
                        post = ""
                    _challenge_log.append({"type": "req", "detail": f"{req.method} {u[:160]}", "post": post})
        def _on_auth_response(resp):
            u = resp.url
            if "freebuff.com" in u and "/api/auth/" in u:
                try:
                    body = resp.text()[:600]
                except Exception:
                    body = ""
                _auth_captures.append({"status": resp.status, "url": u[:200], "body": body})
                if "signup-challenge" in u:
                    _challenge_log.append({"type": "resp", "detail": f"{resp.status} {u[:160]}", "body": body})
        page.on("request", _on_auth_request)
        page.on("response", _on_auth_response)

        # ---------- 1. GitHub 登录态: cookie 无效则浏览器内登录 ----------
        login_attempts = 0
        page.goto("https://github.com/login", wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        for _ in range(6):
            try:
                if page.locator("#login_field").count() == 0:
                    _log(t, "GitHub 登录态有效 (cookie 生效)", "ok")
                    break
            except Exception:
                pass
            login_attempts += 1
            _log(t, f"GitHub 未登录 (检测到登录表单, 第 {login_attempts} 次登录尝试)...", "warn")
            if login_attempts > 3:
                _log(t, "GitHub 登录多次失败, 放弃自动登录", "err")
                break
            try:
                page.fill("#login_field", username, timeout=10000)
                page.fill("#password", password, timeout=10000)
                page.click('input[type="submit"]', timeout=10000)
                _log(t, "已提交 GitHub 登录表单", "ok")
            except Exception as e:
                _log(t, f"登录表单填写失败: {e}", "warn")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            time.sleep(3)

        # ---------- 2+3. freebuff 官方 GitHub 登录: 自动点击 + 失败自动重试 ----------
        # reCAPTCHA v3 对自动化评分不稳定, 自动点击失败 (signup-challenge 403) 会
        # 跳回 login?error=xxx; 每次重试重新加载页面 (重新执行 reCAPTCHA) 再点,
        # 部分情况下第二次/第三次点击可通过。全部失败才回退手动模式。
        MAX_AUTO_CLICKS = 8
        session_token = None
        for attempt in range(MAX_AUTO_CLICKS):
            _log(t, f"=== freebuff 登录尝试 {attempt + 1}/{MAX_AUTO_CLICKS} ===", "info")
            try:
                page.goto(FREEBUFF_WEB + "/login", wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                _log(t, f"打开 freebuff 登录页失败: {e}", "warn")
                continue

            # a) 等 Turnstile 通过, 按钮解除 disabled
            gh_btn = None
            try:
                page.wait_for_selector("button", timeout=15000)
                for _ in range(45):
                    gh_btn = page.locator("button").filter(
                        has_text=re.compile("github", re.I)).first
                    if gh_btn.count() > 0 and not gh_btn.is_disabled():
                        break
                    page.wait_for_timeout(1000)
            except Exception:
                gh_btn = None
            if not (gh_btn and gh_btn.count() > 0 and not gh_btn.is_disabled()):
                _log(t, f"GitHub 按钮不可用 (Turnstile 未通过, 第 {attempt+1} 次)", "warn")
                continue

            # b) 模拟真人点击 (hover 停顿后点击)
            _chal_seen = len(_challenge_log)
            try:
                gh_btn.hover()
                page.wait_for_timeout(800)
                gh_btn.click(timeout=10000)
                _log(t, f"自动点击 Continue with GitHub (第 {attempt+1}/{MAX_AUTO_CLICKS})", "info")
            except Exception as e:
                _log(t, f"自动点击失败: {e}", "warn")
                continue

            # c) 等跳转 GitHub 授权页 (signup-challenge 由前端自动触发)
            # 两种可能: ① 正常跳转 github.com 授权页; ② 已授权过 → 直接跳回
            # freebuff 非 login 页 (首页) 且 session 已设置 = 本次点击即成功
            jumped = False
            for _ in range(25):  # 最长 25s
                page.wait_for_timeout(1000)
                u = page.url
                if re.match(r"^https://github\.com/", u):
                    _log(t, f"已跳转 GitHub: {u[:100]}", "ok")
                    jumped = True
                    break
                if "freebuff.com" in u and "/login" not in u:
                    tok = _wait_freebuff_session(ctx, timeout=2)
                    if tok:
                        session_token = tok
                        _log(t, f"点击后直接跳转 freebuff 且 session 已就绪: {u[:80]}", "ok")
                        jumped = True
                        break
            if not jumped:
                u = page.url[:120]
                if "freebuff.com" in u and "error=" in u:
                    _log(t, f"freebuff 拒绝 ({u.split('?')[-1][:60]}), 自动重试...", "warn")
                elif "freebuff.com" in u and "/login" not in u:
                    _log(t, f"点击后跳转 freebuff 但 session 未就绪 (第 {attempt+1} 次, url={u}), 交给状态机", "warn")
                    jumped = True  # 进入状态机处理
                else:
                    _log(t, f"点击后未跳转 GitHub (第 {attempt+1} 次, url={u})", "warn")

            # 输出本轮 signup-challenge 明细 (区分未触发/missing 与评分低/invalid)
            new_chal = _challenge_log[_chal_seen:]
            if new_chal:
                for _e in new_chal:
                    if _e["type"] == "req":
                        _log(t, f"[challenge] {_e['detail']} post={_e['post']}", "info")
                    else:
                        _log(t, f"[challenge] {_e['detail']} body={_e['body'][:200]}", "info")
            else:
                _log(t, "本轮未触发 signup-challenge 请求 (reCAPTCHA/Turnstile 未产出 token)", "warn")

            if session_token:
                break
            if not jumped:
                continue

            # 状态机前: 若已跳到 GitHub 授权页, 先等 Authorize 按钮渲染出来,
            # 避免首次 _detect_auth_state 判成 unknown 触发一次多余 reload
            try:
                if "/login/oauth/authorize" in page.url:
                    page.get_by_role("button", name=re.compile(r"Authorize", re.I)).first.wait_for(
                        state="visible", timeout=8000)
                    _log(t, "GitHub 授权页 Authorize 按钮已渲染", "info")
            except Exception:
                pass

            # d) 状态机: 授权页 / 登录表单 / captcha / callback
            for round_no in range(12):
                state, url = _detect_auth_state(page)
                final_url = url
                if state == "callback_error":
                    _log(t, "freebuff 服务端 GitHub OAuth 回调失败 (error=Callback)。"
                            "说明 GitHub OAuth App 的 Client secret 与 freebuff 服务端 "
                            "GITHUB_CLIENT_SECRET 不一致或 App 已被禁用, 请到 "
                            "github.com/settings/developers 检查 App (client_id="
                            + oc_main.GITHUB_OAUTH_CLIENT_ID + ") 并同步 secret。", "err")
                    break
                if state == "freebuff":
                    # 必须确认 session cookie 已设置才算完成
                    tok = _wait_freebuff_session(ctx, timeout=4)
                    if tok:
                        session_token = tok
                        _log(t, f"已跳转 freebuff 且 session 已就绪: {url[:80]}", "ok")
                        break
                    # 检测 reCAPTCHA 验证提示 ("Please complete the verification check")
                    try:
                        body_text = page.inner_text("body")[:1500]
                        if "verification check" in body_text and "please complete" in body_text.lower():
                            _log(t, "检测到 reCAPTCHA 验证提示, 自动重试点击...", "warn")
                            break
                    except Exception:
                        pass
                    if "error=" in url:
                        _log(t, f"freebuff 登录被拒 ({url.split('?')[-1][:60]}), 自动重试...", "warn")
                        break  # 跳出状态机 → 外层重新加载登录页重试
                    _log(t, f"在 freebuff 页面但 session 未就绪 (url={url[:80]}), 继续等待...", "warn")
                    page.wait_for_timeout(1500)
                    continue
                if state == "login_form":
                    login_attempts += 1
                    _log(t, f"检测到 GitHub 登录表单 (第 {login_attempts} 次登录尝试)...", "warn")
                    if login_attempts > 3:
                        _log(t, "连续多次登录未成功, 放弃自动登录", "err")
                        break
                    try:
                        page.fill("#login_field", username, timeout=10000)
                        page.fill("#password", password, timeout=10000)
                        page.click('input[type="submit"]', timeout=10000)
                        _log(t, "已提交 GitHub 登录表单", "ok")
                    except Exception as e:
                        _log(t, f"登录表单填写失败: {e}", "warn")
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    time.sleep(3)
                    continue
                if state == "authorize":
                    _clicked = False
                    for sel in AUTHORIZE_BTN_SELECTORS:
                        try:
                            el = page.locator(sel).first
                            if el.count() > 0 and el.is_visible(timeout=3000):
                                el.click(timeout=10000)
                                _log(t, f"已点击 Authorize 按钮 ({sel})", "ok")
                                _clicked = True
                                break
                        except Exception:
                            continue
                    if not _clicked:
                        # 文本兜底: 只匹配含 "Authorize" 文本的按钮, 不会误点 Cancel
                        try:
                            btn = page.get_by_role(
                                "button", name=re.compile(r"Authorize", re.I)).first
                            btn.click(timeout=10000)
                            _log(t, "已通过文本匹配点击 Authorize 按钮", "ok")
                            _clicked = True
                        except Exception as e:
                            _log(t, f"点击 Authorize 失败: {e}", "warn")
                    try:
                        # 授权后 GitHub 302 回 freebuff: 同时检测 URL 变化与 session cookie
                        # - URL 跳到 freebuff (callback/首页) → 立即确认 session
                        # - URL 跳到 login?error=xxx → 提前失败, 交给外层自动重试
                        # - session cookie 出现 → 完成
                        for _ in range(30):
                            tok = _wait_freebuff_session(ctx, timeout=2)
                            if tok:
                                session_token = tok
                                _log(t, "Authorize 后 session 已就绪", "ok")
                                break
                            u = page.url
                            if "freebuff.com" in u:
                                if "/login" not in u:
                                    tok = _wait_freebuff_session(ctx, timeout=3)
                                    if tok:
                                        session_token = tok
                                        _log(t, f"跳回 freebuff 且 session 已就绪: {u[:80]}", "ok")
                                        break
                                elif "error=" in u:
                                    _log(t, f"freebuff 拒绝 ({u.split('?')[-1][:60]}), 自动重试...", "warn")
                                    break
                            page.wait_for_timeout(1000)
                    except Exception:
                        pass
                    continue
                if state == "captcha":
                    _log(t, "检测到 captcha/DataDome 拦截, 等待挑战自动通过...", "warn")
                    page.wait_for_timeout(8000)
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=20000)
                    except Exception:
                        pass
                    time.sleep(2)
                    continue
                # unknown: 页面未渲染完或非预期页面, 等一会再刷新
                _log(t, f"页面未就绪 ({round_no+1}/12, url={url[:100]}), 等待后刷新...", "warn")
                page.wait_for_timeout(4000)
                try:
                    page.reload(wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    pass
                time.sleep(2)
            if session_token:
                break

        # 兜底: 自动重试全部失败 → 手动模式, 弹出窗口循环等待操作者点击
        # 失败 (跳回 login?error=missing 等) 不关闭浏览器, 更新提示后重新等待点击,
        # 直到成功 (跳转 freebuff.com 且 session 就绪) 或超时。
        # 容器模式 (REG_HEADLESS=1) 无桌面窗口, 跳过手动点击, 直接走链接授权
        if not session_token and REG_HEADLESS:
            _log(t, "容器模式 (无桌面): 自动点击失败, 将生成手动授权链接", "warn")
        elif not session_token:
            _log(t, "自动点击多次失败, 进入手动模式: 请在弹出的窗口手动点击 Continue with GitHub", "warn")
            manual_deadline = time.time() + 300
            try:
                page.goto(FREEBUFF_WEB + "/login", wait_until="domcontentloaded", timeout=60000)
                while time.time() < manual_deadline:
                    # 确保停在 freebuff 登录页 (错误页/授权页时重新导航)
                    if "github.com" in page.url or ("freebuff.com" in page.url
                                                    and "/login" not in page.url):
                        page.goto(FREEBUFF_WEB + "/login",
                                  wait_until="domcontentloaded", timeout=60000)
                    # 等按钮可用 (Turnstile 通过)
                    gh_btn = None
                    try:
                        page.wait_for_selector("button", timeout=15000)
                        for _ in range(45):
                            gh_btn = page.locator("button").filter(
                                has_text=re.compile("github", re.I)).first
                            if gh_btn.count() > 0 and not gh_btn.is_disabled():
                                break
                            page.wait_for_timeout(1000)
                    except Exception:
                        gh_btn = None
                    if not (gh_btn and gh_btn.count() > 0 and not gh_btn.is_disabled()):
                        _log(t, "GitHub 按钮不可用 (Turnstile 未通过)", "warn")
                        page.wait_for_timeout(5000)
                        continue
                    # 注入/更新提示横幅
                    try:
                        page.evaluate("""() => {
                            let d = document.getElementById('fb-click-hint');
                            const s = document.createElement('style');
                            s.textContent = '@keyframes fbBlink{50%{opacity:.5}}';
                            document.head.appendChild(s);
                            if (!d) {
                                d = document.createElement('div');
                                d.id = 'fb-click-hint';
                                d.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:999999;'
                                    + 'background:#16a34a;color:#fff;font:700 22px/1.5 system-ui;'
                                    + 'padding:16px 24px;text-align:center;box-shadow:0 4px 16px rgba(0,0,0,.4);'
                                    + 'animation:fbBlink 1s infinite';
                                document.body.prepend(d);
                            }
                            d.textContent = '👉 请用鼠标手动点击下方 "Continue with GitHub" 按钮 (如被拒请再点一次)';
                            document.title = '⚠ 请在窗口内点击 Continue with GitHub';
                        }""")
                    except Exception:
                        pass
                    _log(t, "等待操作者手动点击 Continue with GitHub...", "warn")
                    # 等待用户点击 (signup-challenge/signin 请求或跳转 GitHub)
                    clicked = False
                    _prev = len(_auth_requests)
                    while time.time() < manual_deadline:
                        page.wait_for_timeout(2000)
                        if "github.com" in page.url:
                            clicked = True
                            _log(t, "检测到页面跳转 GitHub (用户已点击)", "ok")
                            break
                        if any("signup-challenge" in u
                               for u in _auth_requests[_prev:]):
                            clicked = True
                            _log(t, "检测到 signup-challenge 请求 (用户已点击)", "ok")
                            break
                        if any("signin" in u for u in _auth_requests[_prev:]):
                            clicked = True
                            _log(t, "检测到 signin 请求 (用户已点击)", "ok")
                            break
                    if not clicked:
                        _log(t, "等待操作者手动点击超时", "err")
                        break
                    # 等跳转 GitHub 授权页
                    try:
                        page.wait_for_url(re.compile(r"^https://github\.com/"), timeout=30000)
                        _log(t, f"已跳转 GitHub: {page.url[:100]}", "info")
                    except Exception:
                        _log(t, f"点击后未跳转 GitHub (当前: {page.url[:100]}), 重新等待点击...", "warn")
                        continue
                    # 状态机: 授权页 / callback / session
                    for round_no in range(12):
                        state, url = _detect_auth_state(page)
                        if state == "freebuff":
                            tok = _wait_freebuff_session(ctx, timeout=4)
                            if tok:
                                session_token = tok
                                _log(t, f"已跳转 freebuff 且 session 已就绪: {url[:80]}", "ok")
                                break
                            if "error=" in url:
                                _log(t, f"freebuff 登录被拒 ({url.split('?')[-1][:60]}), 请再次手动点击", "warn")
                                break
                            page.wait_for_timeout(3000)
                            continue
                        if state == "authorize":
                            try:
                                btn = page.get_by_role(
                                    "button", name=re.compile(r"Authorize", re.I)).first
                                btn.click(timeout=10000)
                                _log(t, "已自动点击 Authorize 按钮", "ok")
                            except Exception as e:
                                _log(t, f"点击 Authorize 失败: {e}", "warn")
                            try:
                                page.wait_for_url("**freebuff.com**", timeout=25000)
                            except Exception:
                                pass
                            time.sleep(2)
                            continue
                        if state == "callback_error":
                            _log(t, "freebuff 服务端 GitHub OAuth 回调失败 (error=Callback)", "err")
                            break
                        page.wait_for_timeout(3000)
                    if session_token:
                        break
                    # 失败 → 循环: 重新导航登录页并等待再次点击
                    _log(t, "本轮未获得 session, 将重新等待操作者再次点击...", "warn")
            except Exception as e:
                _log(t, f"手动模式异常: {e}", "warn")

        session_token = _wait_freebuff_session(ctx, timeout=45)
        final_url = page.url

        # 失败时保存截图 + HTML 用于诊断
        if not session_token:
            _log(t, "[auth] freebuff auth 请求记录:", "warn")
            for cap in _auth_captures:
                _log(t, f"[auth] {cap['status']} {cap['url']}", "warn")
                if cap["body"]:
                    _log(t, f"[auth]   body: {cap['body'][:400]}", "warn")
            try:
                shot = os.path.join(debug_dir, f"auth_fail_{t['id']}.png")
                page.screenshot(path=shot, full_page=False)
                _log(t, f"[诊断] 截图已保存: {shot}", "warn")
                html = os.path.join(debug_dir, f"auth_fail_{t['id']}.html")
                with open(html, "w", encoding="utf-8", errors="replace") as f:
                    f.write(page.content())
                _log(t, f"[诊断] HTML 已保存: {html}", "warn")
            except Exception as e:
                _log(t, f"[诊断] 保存失败: {e}", "warn")
    except Exception as e:
        _log(t, f"CloakBrowser 授权异常: {e}", "err")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    if not session_token:
        _log(t, f"授权未获得 session (url={final_url[:120]})", "warn")
        return {"ok": False, "reason": "no_session", "url": final_url}
    _log(t, "freebuff 授权成功, session cookie 已获取", "ok")
    return {"ok": True, "session_token": session_token}


def bind_auth_code_and_fetch_token(t: dict, session_token: str) -> Optional[dict]:
    """逆向链路: cli/code -> /onboard 绑定 -> cli/status 拿 authToken。"""
    fingerprint_id = "freebuff-reg-" + uuid.uuid4().hex[:10]
    s = oc_main.requests.Session(impersonate="chrome142")
    s.headers.update({
        "User-Agent": oc_main.UA,
        "Accept": "*/*",
        "Content-Type": "application/json",
    })
    s.cookies.set("__Secure-next-auth.session-token", session_token,
                  domain="freebuff.com", path="/")
    s.cookies.set("__Host-next-auth.csrf-token",
                  "x", domain="freebuff.com", path="/")

    _log(t, "POST /api/auth/cli/code ...")
    r = s.post(FREEBUFF_WEB + "/api/auth/cli/code",
               json={"fingerprintId": fingerprint_id}, timeout=30)
    if r.status_code != 200:
        _log(t, f"cli/code 失败: {r.status_code} {r.text[:200]}", "err")
        return None
    data = r.json()
    auth_code = (data.get("loginUrl") or "").split("auth_code=")[-1]
    fingerprint_hash = data.get("fingerprintHash", "")
    expires_at = data.get("expiresAt", "")
    _log(t, "浏览器打开 /onboard 完成 auth_code 绑定 ...")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        browser_proxy = _pick_proxy()
        ctx_kwargs = {"proxy": {"server": browser_proxy}} if browser_proxy else {}
        ctx = browser.new_context(**ctx_kwargs)
        ctx.add_cookies([{
            "name": "__Secure-next-auth.session-token",
            "value": session_token, "domain": "freebuff.com", "path": "/",
            "secure": True, "httpOnly": True,
        }])
        page = ctx.new_page()
        try:
            page.goto(FREEBUFF_WEB + f"/onboard?auth_code={auth_code}",
                      wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
        except Exception as e:
            _log(t, f"onboard 绑定页警告: {e}", "warn")
        browser.close()

    _log(t, "GET /api/auth/cli/status ...")
    r2 = s.get(FREEBUFF_WEB + "/api/auth/cli/status", params={
        "fingerprintId": fingerprint_id,
        "fingerprintHash": fingerprint_hash,
        "expiresAt": expires_at,
    }, timeout=30)
    if r2.status_code != 200 or not r2.json().get("user"):
        _log(t, f"cli/status 失败: {r2.status_code} {r2.text[:300]}", "err")
        return None
    user = r2.json()["user"]
    _log(t, f"✅ authToken 已获取: {user.get('name')} ({user.get('email')})", "ok")
    return {
        "userId": user.get("id"),
        "name": user.get("name"),
        "email": user.get("email"),
        "authToken": user.get("authToken"),
    }


def run_registration(t: dict):
    try:
        t["status"] = "running"
        cfg = _load_oc_config()
        # 任务 payload 明确带 proxy 时：空字符串就是直连，不回退配置代理
        proxy = t["proxy"] if "proxy" in t else ""
        _set_task_proxy(proxy)
        country = t.get("country") or cfg.get("country") or DEFAULT_COUNTRY

        # ---------- 1. 邮箱 ----------
        email = t.get("email") or ""
        mail_client = None
        if t.get("auto_mail") or not email:
            _log(t, "创建 CF 临时邮箱 ...")
            domains = cfg.get("cf_selected_domains") or cfg.get("cf_domains")
            mail_client = cf_mail.CfMailClient(
                api_base=cfg.get("cf_api_base"),
                admin_password=cfg.get("cf_admin_password"),
                domains=domains,
                proxy=proxy,
            )
            email = mail_client.create_address()
            t["email"] = email
            _log(t, f"邮箱就绪: {email}", "ok")

        password = t.get("password") or oc_main.generate_password() \
            if hasattr(oc_main, "generate_password") else "Fb@" + uuid.uuid4().hex[:10] + "!"
        username = t.get("username") or oc_main.generate_username()
        t["password"], t["username"] = password, username
        _log(t, f"账号: {username} / {email}")

        def code_provider(hint: str = "") -> str:
            _log(t, "等待 GitHub 8 位验证码 ...")
            if mail_client:
                code = mail_client.poll_verify_code(email, timeout=180, interval=3.0, code_length=8)
                if not code:
                    raise oc_main.ProtocolError("GitHub 验证码获取超时")
                _log(t, "验证码已获取", "ok")
                return code
            # 无邮箱客户端（自定义外部邮箱）→ 等待页面手动输入
            _log(t, "使用自定义邮箱，请在管理页面手动输入 GitHub 验证码", "warn")
            t["status"] = "wait_code"
            t["_code_value"] = ""
            ev = t.get("_code_event")
            if ev is None:
                ev = threading.Event()
                t["_code_event"] = ev
            else:
                ev.clear()
            deadline = time.monotonic() + 300  # 5 分钟超时
            while time.monotonic() < deadline:
                if ev.wait(2.0):
                    break
            code = str(t.get("_code_value") or "").strip()
            t["_code_value"] = ""
            if not code:
                raise oc_main.ProtocolError("GitHub 验证码等待超时（5 分钟未输入）")
            t["status"] = "running"
            _log(t, f"验证码已获取: {code}", "ok")
            return code

        oc_main.set_log_handler(lambda tag, msg, level: _log(t, msg, level))
        client = oc_main.OpenCodeClient(proxy=proxy, code_provider=code_provider, verbose=True)
        client._signup_email = email
        client._signup_password = password
        client._signup_username = username

        # ---------- 2. GitHub 协议注册 (DataDome 自动处理, 风控拒绝自动换账号重试一次) ----------
        _log(t, "GitHub 协议注册 (DataDome 自动处理)...")
        for _attempt in range(2):
            try:
                client.github_signup(email, password, username, country, None)
                break
            except oc_main.ProtocolError as e:
                if _attempt == 0:
                    _log(t, f"GitHub 注册被风控拒绝, 换新邮箱/账号重试一次: {e}", "warn")
                    try:
                        email = mail_client.create_address()
                        t["email"] = email
                    except Exception:
                        pass
                    username = oc_main.generate_username()
                    password = oc_main.generate_password() \
                        if hasattr(oc_main, "generate_password") \
                        else "Fb@" + uuid.uuid4().hex[:10] + "!"
                    t["password"], t["username"] = password, username
                    client._signup_email = email
                    client._signup_password = password
                    client._signup_username = username
                    _log(t, f"重试账号: {username} / {email}")
                else:
                    raise
        _log(t, "GitHub 注册 + 登录完成", "ok")

        # ---------- 3. freebuff OAuth 授权 ----------
        auth = browser_authorize_freebuff(t, client)
        if not auth.get("ok"):
            # 生成手动授权链接
            s = oc_main.requests.Session(impersonate="chrome142")
            r = s.post(FREEBUFF_WEB + "/api/auth/cli/code",
                       json={"fingerprintId": "manual-" + uuid.uuid4().hex[:10]}, timeout=30)
            if r.status_code == 200:
                d = r.json()
                t["manual_url"] = d.get("loginUrl", "")
                t["fingerprint"] = {
                    "fingerprintId": d.get("fingerprintId"),
                    "fingerprintHash": d.get("fingerprintHash"),
                    "expiresAt": d.get("expiresAt"),
                }
                _log(t, f"等待手动授权: {t['manual_url'][:80]}...", "warn")
                t["status"] = "wait_manual"
                return
            raise oc_main.ProtocolError("授权失败且无法生成手动链接")

        # ---------- 4. 逆向链路拿 token ----------
        result = bind_auth_code_and_fetch_token(t, auth["session_token"])
        if not result:
            # 手动兜底: 仍可生成手动链接
            s = oc_main.requests.Session(impersonate="chrome142")
            r = s.post(FREEBUFF_WEB + "/api/auth/cli/code",
                       json={"fingerprintId": "manual-" + uuid.uuid4().hex[:10]}, timeout=30)
            if r.status_code == 200:
                d = r.json()
                t["manual_url"] = d.get("loginUrl", "")
                t["fingerprint"] = {
                    "fingerprintId": d.get("fingerprintId"),
                    "fingerprintHash": d.get("fingerprintHash"),
                    "expiresAt": d.get("expiresAt"),
                }
                t["status"] = "wait_manual"
                return
            raise oc_main.ProtocolError("逆向链路失败且无法生成手动链接")

        result["email"] = email
        result["username"] = username
        result["password"] = password
        t["result"] = result
        t["status"] = "success"

        # ---------- 5. 自动写入凭据文件 (worker 热加载, 无需手动入库/重启) ----------
        _save_credentials(t, result, email)
    except Exception as e:
        _log(t, f"失败: {e}", "err")
        t["status"] = "failed"
        t["error"] = str(e)


def _save_credentials(t: dict, result: dict, email: str = "") -> None:
    """把注册结果自动写入 credentials/freebuff_credentials.json (worker 热加载生效)。"""
    try:
        cred_path = os.path.join(os.path.dirname(BASE), "credentials",
                                 "freebuff_credentials.json")
        with open(cred_path, "r", encoding="utf-8") as f:
            creds = json.load(f)
        creds.setdefault("accounts", {})[result["userId"]] = {
            "email": email or result.get("email", ""),
            "name": result.get("name") or "",
            "authToken": result["authToken"],
            "password": result.get("password") or t.get("password", ""),
            "registeredAt": int(time.time() * 1000),
        }
        with open(cred_path, "w", encoding="utf-8") as f:
            json.dump(creds, f, ensure_ascii=False, indent=2)
        _log(t, "✅ 账号已自动写入 credentials/freebuff_credentials.json (worker 将热加载)", "ok")
    except Exception as e:
        _log(t, f"自动写入凭据文件失败: {e}", "warn")


# ================= FastAPI 端点 =================

@app.post("/test_proxy")
def test_proxy(payload: dict):
    """通过指定代理访问地理IP服务，返回出口 IP/地区/延迟/是否代理IP。"""
    proxy = str((payload or {}).get("proxy") or "").strip()
    t0 = time.monotonic()
    try:
        # 显式指定空代理字符串让 curl_cffi 忽略环境变量 HTTP_PROXY，实现真直连
        s = oc_main.requests.Session(
            impersonate="chrome",
            timeout=20,
            proxies={"http": proxy or "", "https": proxy or ""},
        )
        r = s.get(
            "http://ip-api.com/json/?fields=query,country,countryCode,regionName,city,isp,proxy,hosting",
            timeout=20,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            return {"ok": False, "error": f"地理IP服务返回 HTTP {r.status_code}"}
        d = r.json()
        return {
            "ok": True,
            "latency_ms": elapsed_ms,
            "ip": d.get("query"),
            "country": d.get("country"),
            "countryCode": d.get("countryCode"),
            "region": d.get("regionName"),
            "city": d.get("city"),
            "isp": d.get("isp"),
            "proxyFlag": bool(d.get("proxy")),
            "hosting": bool(d.get("hosting")),
        }
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.get("/")
def root():
    return {"service": "freebuff register service", "tasks": len(TASKS)}


@app.post("/register")
def register(payload: dict):
    task_id = uuid.uuid4().hex[:12]
    t = {
        "id": task_id,
        "status": "pending",
        "logs": [],
        "email": (payload or {}).get("email", ""),
        "password": (payload or {}).get("password", ""),
        "username": (payload or {}).get("username", ""),
        "country": (payload or {}).get("country", ""),
        "proxy": (payload or {}).get("proxy", ""),
        "auto_mail": bool((payload or {}).get("auto_mail", True)),
        "result": None,
        "error": "",
        "manual_url": "",
        "fingerprint": None,
    }
    with _LOCK:
        TASKS[task_id] = t
    threading.Thread(target=run_registration, args=(t,), daemon=True).start()
    return {"task_id": task_id}


@app.get("/status/{task_id}")
def status(task_id: str):
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    return {
        "status": t["status"],
        "logs": t["logs"][-100:],
        "result": t.get("result"),
        "error": t.get("error", ""),
        "manual_url": t.get("manual_url", ""),
        "email": t.get("email", ""),
        "username": t.get("username", ""),
        "password": t.get("password", "") if t.get("status") in ("wait_manual", "success") else "",
    }


@app.post("/complete/{task_id}")
def complete(task_id: str):
    """手动授权完成后的收尾: 轮询 cli/status 拿 token。"""
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    if t["status"] != "wait_manual":
        raise HTTPException(400, f"task status is {t['status']}")
    fp = t.get("fingerprint") or {}
    if not fp:
        raise HTTPException(400, "no fingerprint")

    _log(t, "手动授权完成, 轮询 cli/status ...")
    s = oc_main.requests.Session(impersonate="chrome142")
    r = s.get(FREEBUFF_WEB + "/api/auth/cli/status", params={
        "fingerprintId": fp["fingerprintId"],
        "fingerprintHash": fp["fingerprintHash"],
        "expiresAt": fp["expiresAt"],
    }, timeout=30)
    if r.status_code == 200 and r.json().get("user"):
        user = r.json()["user"]
        _log(t, f"✅ authToken 已获取: {user.get('name')}", "ok")
        t["result"] = {
            "userId": user.get("id"),
            "name": user.get("name"),
            "email": user.get("email"),
            "authToken": user.get("authToken"),
            "username": t.get("username", ""),
            "password": t.get("password", ""),
            "email": t.get("email") or user.get("email"),
        }
        t["status"] = "success"
        # 自动写入凭据文件 (手动授权完成路径)
        _save_credentials(t, t["result"], t.get("email", ""))
        return t["result"]
    _log(t, f"cli/status 仍未就绪: {r.status_code} {r.text[:200]}", "warn")
    raise HTTPException(400, "authorization not completed yet, try again in a few seconds")


@app.post("/submit_code/{task_id}")
def submit_code(task_id: str, payload: dict):
    """手动提交 GitHub 验证码（自定义邮箱场景）。"""
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    if t["status"] != "wait_code":
        raise HTTPException(400, f"task status is {t['status']}, expected wait_code")
    code = str((payload or {}).get("code") or "").strip()
    if not code:
        raise HTTPException(400, "code is required")
    t["_code_value"] = code
    ev = t.get("_code_event")
    if ev:
        ev.set()
    _log(t, f"收到手动验证码: {code}", "ok")
    return {"ok": True}


@app.get("/cf_config")
def get_cf_config():
    """读取 web_config.json 中的 CF 邮箱配置及默认国家/代理。"""
    cfg = _load_oc_config()
    return {
        "proxy": cfg.get("proxy", ""),
        "country": cfg.get("country", ""),
        "cf_api_base": cfg.get("cf_api_base", ""),
        "cf_admin_password": cfg.get("cf_admin_password", ""),
        "cf_domains": cfg.get("cf_domains", []),
        "cf_selected_domains": cfg.get("cf_selected_domains", []),
    }


@app.post("/cf_config")
def save_cf_config(payload: dict):
    """保存 web_config.json 中的 CF 邮箱配置及默认国家/代理。"""
    allowed = {"proxy", "country", "cf_api_base", "cf_admin_password",
               "cf_domains", "cf_selected_domains"}
    update = {}
    for k in allowed:
        if k in payload:
            update[k] = payload[k]
    # 域名列表归一化为字符串列表
    for k in ("cf_domains", "cf_selected_domains"):
        if k in update and isinstance(update[k], list):
            update[k] = [str(d).strip() for d in update[k] if str(d).strip()]
    _save_oc_config(update)
    return {"ok": True, "config": {**get_cf_config(), **update}}


@app.post("/cf/fetch_domains")
def cf_fetch_domains(payload: dict):
    """从 CF Worker 拉取可用域名列表（多端点探测）。"""
    api_base = str((payload or {}).get("api_base") or "").strip().rstrip("/")
    admin_password = str((payload or {}).get("admin_password") or "").strip()
    proxy = str((payload or {}).get("proxy") or "").strip()
    if not api_base:
        raise HTTPException(400, "api_base is required")
    session = oc_main.requests.Session(
        impersonate="chrome",
        timeout=20,
        # 如果不传代理，用空字符串直连（忽略环境变量 HTTP_PROXY）
        proxies={"http": proxy or "", "https": proxy or ""},
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    if admin_password:
        headers["x-admin-auth"] = admin_password

    domains = []

    def _normalize(raw) -> list[str]:
        out = []
        seen = set()
        items = raw if isinstance(raw, list) else []
        for item in items:
            if isinstance(item, str):
                v = item.strip()
            elif isinstance(item, dict):
                v = str(item.get("value") or item.get("domain") or item.get("label") or "").strip()
            else:
                v = str(item or "").strip()
            if not v or v.lower() in seen:
                continue
            seen.add(v.lower())
            out.append(v)
        return out

    # 尝试 1: /open_api/settings
    if not domains:
        try:
            r = session.get(f"{api_base}/open_api/settings", headers=headers, timeout=15)
            if r.status_code == 200:
                data = r.json() if r.content else {}
                if isinstance(data, dict):
                    domains = _normalize(data.get("domains") or [])
                    if not domains:
                        domains = _normalize(data.get("defaultDomains") or data.get("DEFAULT_DOMAINS") or [])
        except Exception:
            pass

    # 尝试 2: /admin/show_password
    if not domains and admin_password:
        try:
            r = session.get(f"{api_base}/admin/show_password", headers=headers, timeout=12)
            if r.status_code == 200:
                data = r.json() if r.text else {}
                if isinstance(data, dict):
                    for key in ("domains", "address_domains", "enableDomains", "enable_domains"):
                        v = data.get(key)
                        if isinstance(v, list) and v:
                            domains = [str(d).strip().lower() for d in v if str(d).strip()]
                            break
                        if isinstance(v, str) and v:
                            domains = [d.strip().lower() for d in v.split(",") if d.strip()]
                            break
        except Exception:
            pass

    # 尝试 3: /admin/settings
    if not domains and admin_password:
        try:
            r = session.get(f"{api_base}/admin/settings", headers=headers, timeout=12)
            if r.status_code == 200:
                data = r.json() if r.text else {}
                if isinstance(data, dict):
                    for key in ("domains", "address_domains", "enableDomains"):
                        v = data.get(key)
                        if isinstance(v, list) and v:
                            domains = [str(d).strip().lower() for d in v if str(d).strip()]
                            break
        except Exception:
            pass

    # 尝试 4: /api/settings
    if not domains:
        try:
            r = session.get(f"{api_base}/api/settings", headers=headers, timeout=12)
            if r.status_code == 200:
                data = r.json() if r.text else {}
                if isinstance(data, dict):
                    v = data.get("domains") or data.get("address_domains")
                    if isinstance(v, list) and v:
                        domains = [str(d).strip().lower() for d in v if str(d).strip()]
        except Exception:
            pass

    if not domains:
        return {"ok": False, "message": "此 CF 服务版本未暴露域名列表端点，请手动添加域名"}
    # 去重保序
    seen = set()
    uniq = []
    for d in domains:
        if d and d not in seen:
            seen.add(d)
            uniq.append(d)
    return {"ok": True, "domains": uniq}


if __name__ == "__main__":
    # 0.0.0.0: 容器内通过 host.docker.internal 访问
    mode = "有头(弹窗手动点击)" if not REG_HEADLESS else "无头(链接授权)"
    print("=" * 56)
    print("[register_service] freebuff 账号注册服务启动中...")
    print(f"[register_service] http://0.0.0.0:8899 | 代理 {DEFAULT_PROXY} | {mode}")
    print("[register_service] Ctrl+C 停止")
    print("=" * 56)
    uvicorn.run(app, host="0.0.0.0", port=8899, log_level="info", access_log=False)
