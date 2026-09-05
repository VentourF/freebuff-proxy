# -*- coding: utf-8 -*-
"""
OpenCode (opencode.ai) 协议客户端

基于 har/ 抓包还原的完整流程:
1. 打开 OpenCode 授权 (OpenAuth)
2. 跳转 GitHub OAuth
3. 注册 / 登录 GitHub
4. 邮箱验证码 (launch code)
5. 授权 OpenCode GitHub App
6. 回调拿到 opencode auth cookie
7. 进入 workspace，解析 Default API Key (sk-...)

用法:
  python main.py login    --email you@example.com --password 'Pass123!'
  python main.py register (交互输入邮箱, 用户名/密码自动生成)
  python main.py register --email you@example.com --username myuser --password 'Pass123!'
  python main.py full     (同 register)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import string
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, unquote, quote

from curl_cffi import requests
from bs4 import BeautifulSoup


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

# Chrome 浏览器默认请求头，补全后不容易触发 GitHub DataDome 风控
CHROME_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-mobile": "?0",
    "upgrade-insecure-requests": "1",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
    "sec-fetch-user": "?1",
}

GITHUB_OAUTH_CLIENT_ID = "Iv23liOTxMmED77mtyGd"
GITHUB_OAUTH_REDIRECT = "https://auth.opencode.ai/github/callback"
GITHUB_OAUTH_SCOPE = "read:user user:email"

OPENCODE_AUTH = "https://opencode.ai/auth"
OPENCODE_AUTH_CALLBACK = "https://opencode.ai/auth/callback"
OPENAUTH_AUTHORIZE = "https://auth.opencode.ai/authorize"
OPENAUTH_GITHUB = "https://auth.opencode.ai/github/authorize"

# SolidStart server actions (from opencode frontend bundle)
SERVER_FN_CREATE_KEY = "444825072757feb3b2ec98a3260e2c32488cb05899076c0afb36b9eb5142bc62"
SERVER_FN_LIST_KEYS = "c22cd964237ba79f2f9b95faa2a14b804f870d1bab49279463379cc6a0fd0c85"
SERVER_FN_REMOVE_KEY = "48baebd35f970b8dc3a658e6f9cc953efd731a7f8a6376012c9bc1802cec787d"


@dataclass
class ApiKeyInfo:
    id: str = ""
    name: str = ""
    key: str = ""
    key_display: str = ""
    user_id: str = ""
    email: str = ""
    workspace_id: str = ""


@dataclass
class AuthResult:
    success: bool
    api_key: Optional[ApiKeyInfo] = None
    workspace_id: str = ""
    auth_cookie: str = ""
    message: str = ""
    raw: dict = field(default_factory=dict)


def generate_username() -> str:
    """自动生成随机用户名 (GitHub 规则: 字母数字和连字符, 不允许下划线/点/连续连字符)。
    采用多种风格随机切换, 避免固定一种模式。
    """
    first_names = [
        "alex", "sam", "jordan", "taylor", "morgan", "casey", "riley", "jamie",
        "chris", "nick", "max", "leo", "ryan", "tom", "ben", "dan", "jack", "luke",
        "mia", "emma", "ava", "lily", "nora", "isla", "zoe", "ivy", "amy", "kate",
        "liam", "noah", "ethan", "lucas", "mason", "logan", "owen", "carter",
        "aria", "ella", "luna", "ruby", "jade", "sage", "hazel", "willow",
    ]
    last_names = [
        "smith", "jones", "brown", "davis", "wilson", "miller", "moore", "taylor",
        "lee", "clark", "lewis", "walker", "hall", "young", "king", "wright",
        "hill", "green", "adams", "baker", "carter", "cooper", "bell", "ward",
        "rivera", "ross", "powell", "owens", "perry", "butler", "foster", "reyes",
    ]
    adj = ["cool", "fast", "sharp", "bright", "dark", "wild", "calm", "bold",
           "keen", "swift", "lucky", "happy", "smart", "brave", "cosmic", "cyber",
           "quiet", "silent", "rapid", "clear", "warm", "crisp", "vivid", "noble"]
    noun = ["panda", "tiger", "fox", "hawk", "wolf", "bear", "lion", "eagle",
            "star", "moon", "cloud", "storm", "river", "ocean", "phoenix", "dragon",
            "raven", "falcon", "otter", "lynx", "cobra", "viper", "ember", "frost"]

    mode = random.randint(0, 5)
    if mode == 0:
        # adj-noun-数字
        digits = "".join(random.choices(string.digits, k=random.randint(3, 5)))
        return f"{random.choice(adj)}-{random.choice(noun)}-{digits}"
    if mode == 1:
        # 名字-姓-数字
        return f"{random.choice(first_names)}-{random.choice(last_names)}-{random.randint(10, 9999)}"
    if mode == 2:
        # 名字+数字 (短)
        return f"{random.choice(first_names)}{random.randint(100, 9999)}"
    if mode == 3:
        # 名字-单词-数字
        return f"{random.choice(first_names)}-{random.choice(noun)}{random.randint(10, 999)}"
    if mode == 4:
        # 姓-名字-数字
        return f"{random.choice(last_names)}-{random.choice(first_names)}{random.randint(10, 999)}"
    # 名字首字母 + 姓 + 数字
    f = random.choice(first_names)
    return f"{f[0]}-{random.choice(last_names)}-{random.randint(100, 9999)}"


def generate_password(length: int = 20) -> str:
    """自动生成强密码"""
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    # 确保至少包含大写、小写、数字、特殊字符
    pwd = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$%^&*"),
    ]
    pwd += [random.choice(chars) for _ in range(length - 4)]
    random.shuffle(pwd)
    return "".join(pwd)


class ProtocolError(Exception):
    pass


# ---------------- 日志格式化 (模块级, 供非类内调用使用) ----------------
# ANSI 颜色码 (Windows 10+ 支持)
_C_RESET = "\033[0m"
_C_TIME = "\033[90m"       # 灰色 - 时间戳
_C_TAG = "\033[36m"        # 青色 - 标签
_C_INFO = "\033[37m"       # 白色 - 普通信息
_C_OK = "\033[32m"         # 绿色 - 成功
_C_WARN = "\033[33m"       # 黄色 - 警告
_C_ERR = "\033[31m"        # 红色 - 错误/失败
_C_REQ = "\033[34m"        # 蓝色 - HTTP 请求
_C_REDIRECT = "\033[35m"   # 紫色 - 重定向
_C_DEBUG = "\033[90m"      # 灰色 - 调试


# Web 日志推送 handler (由 web_app.py 设置, 非空时日志同时推送到 WebSocket)
_log_handler = None


def set_log_handler(fn):
    """设置全局日志 handler, 用于 Web 界面实时推送日志。
    fn 签名: fn(tag: str, msg: str, level: str) -> None
    level: info/ok/warn/err/debug
    """
    global _log_handler
    _log_handler = fn


_LEVEL_COLOR = {
    "info": _C_INFO,
    "ok": _C_OK,
    "warn": _C_WARN,
    "err": _C_ERR,
    "debug": _C_DEBUG,
}


def _fmt_log(tag: str, msg: str, color: str = _C_INFO) -> str:
    """格式化日志: 时间 标签 消息(带颜色)"""
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    return f"{_C_TIME}{ts}{_C_RESET} {_C_TAG}[{tag}]{_C_RESET} {color}{msg}{_C_RESET}"


def _emit(tag: str, msg: str, level: str = "info"):
    """输出日志: 如果有 Web handler 则推送, 否则 print 到终端"""
    if _log_handler:
        _log_handler(tag, msg, level)
    else:
        color = _LEVEL_COLOR.get(level, _C_INFO)
        print(_fmt_log(tag, msg, color), flush=True)


def log_info(tag: str, msg: str):
    _emit(tag, msg, "info")


def log_ok(tag: str, msg: str):
    _emit(tag, msg, "ok")


def log_warn(tag: str, msg: str):
    _emit(tag, msg, "warn")


def log_err(tag: str, msg: str):
    _emit(tag, msg, "err")


def log_debug(tag: str, msg: str):
    _emit(tag, msg, "debug")


class OpenCodeClient:
    def __init__(
        self,
        proxy: Optional[str] = None,
        timeout: int = 30,
        code_provider: Optional[Callable[[str], str]] = None,
        verbose: bool = True,
    ):
        self.timeout = timeout
        self.verbose = verbose
        self.code_provider = code_provider or self._default_code_provider
        self.session = requests.Session(impersonate="chrome142")
        self.session.headers.update(CHROME_HEADERS)
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

        self.oauth_state_opencode: str = ""
        self.oauth_state_github: str = ""
        self.workspace_id: str = ""
        self.api_key: Optional[ApiKeyInfo] = None

    # ---------------- utils ----------------
    @staticmethod
    def _is_noise_log(msg: str) -> bool:
        """判断日志是否属于低价值 HTTP 细节(Web 端视为噪音过滤)。
        保留关键阶段: 启动/注册/登录/验证/授权/workspace/API Key 等。
        """
        raw = msg.lstrip()
        low = raw.lower()
        head = raw.split(" ")[0].lower()
        if head in ("get", "post") and not any(k in low for k in ("注册", "登录", "验证", "错误", "失败")):
            return True
        if raw.startswith("-> redirect") or raw.startswith("set-cookie") or raw.startswith("cookies:") or raw.startswith("current"):
            return True
        if raw.startswith("当前 url:") or raw.startswith("当前 URL:") or raw.startswith("url="):
            return True
        if raw.startswith("[调试]") or raw.startswith("  [调试]"):
            return True
        if "opencode state =" in low or "github state =" in low or "github oauth url =" in low:
            return True
        if raw.startswith("  应用于 ") or raw.startswith("  set-cookie"):
            return True
        return False

    def log(self, *args):
        if not self.verbose:
            return
        msg = " ".join(str(a) for a in args)
        if self._is_noise_log(msg):
            return
        # 按内容判断 level (用于 Web 推送着色)
        lower = msg.lower()
        if lower.startswith(("get ", "post ")) and ("403" in msg or "422" in msg or "500" in msg):
            level = "err"
        elif any(k in lower for k in ("警告", "失败", "错误", "error", "warn", "422", "403")):
            level = "warn"
        elif any(k in lower for k in ("成功", "ok", "available", "workspace =", "api key")):
            level = "ok"
        elif msg.lstrip().startswith("[调试]") or msg.lstrip().startswith("  [调试]"):
            level = "debug"
        else:
            level = "info"
        _emit("OC", msg, level)

    @staticmethod
    def _default_code_provider(hint: str = "") -> str:
        tip = hint or "请输入邮箱验证码 / launch code"
        return input(f"{tip}: ").strip()

    def _get(self, url: str, retries: int = 3, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", False)
        last_err = None
        for attempt in range(retries):
            try:
                r = self.session.get(url, **kwargs)
                self.log(f"GET {r.status_code} {url[:120]}")
                return r
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    self.log(f"  GET 重试 {attempt+1}/{retries}: {str(e)[:80]}")
                    time.sleep(1 + attempt)
        raise last_err

    def _post(self, url: str, retries: int = 3, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", False)
        last_err = None
        for attempt in range(retries):
            try:
                r = self.session.post(url, **kwargs)
                self.log(f"POST {r.status_code} {url[:120]}")
                return r
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    self.log(f"  POST 重试 {attempt+1}/{retries}: {str(e)[:80]}")
                    time.sleep(1 + attempt)
        raise last_err

    def _follow(self, resp: requests.Response, limit: int = 12) -> requests.Response:
        """跟随 3xx，保留跨域 cookie。"""
        count = 0
        while resp.status_code in (301, 302, 303, 307, 308):
            if count >= limit:
                break
            loc = resp.headers.get("Location")
            if not loc:
                break
            next_url = urljoin(resp.url, loc)
            self.log(f"  -> redirect {resp.status_code} => {next_url[:140]}")
            resp = self._get(next_url)
            count += 1
        return resp

    @staticmethod
    def _soup(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    @staticmethod
    def _hidden_fields(soup: BeautifulSoup, form=None) -> dict:
        root = form or soup
        data = {}
        for inp in root.select("input"):
            name = inp.get("name")
            if not name:
                continue
            typ = (inp.get("type") or "text").lower()
            if typ in ("submit", "button", "image"):
                continue
            data[name] = inp.get("value") or ""
        return data

    @staticmethod
    def _pick_form(soup: BeautifulSoup, action_substr: str = "", method: str = "post"):
        for form in soup.find_all("form"):
            if method and (form.get("method") or "get").lower() != method.lower():
                continue
            action = form.get("action") or ""
            if action_substr and action_substr not in action:
                continue
            return form
        forms = soup.find_all("form")
        return forms[0] if forms else None

    def _extract_authenticity(self, html: str) -> dict:
        soup = self._soup(html)
        form = self._pick_form(soup) or soup
        fields = self._hidden_fields(soup, form)
        # fallback regex
        if "authenticity_token" not in fields:
            m = re.search(
                r'name="authenticity_token"[^>]*value="([^"]+)"|'
                r'value="([^"]+)"[^>]*name="authenticity_token"',
                html,
            )
            if m:
                fields["authenticity_token"] = m.group(1) or m.group(2)
        for key in ("timestamp", "timestamp_secret"):
            if key not in fields:
                m = re.search(rf'name="{key}"[^>]*value="([^"]+)"', html)
                if m:
                    fields[key] = m.group(1)
        # honeypot required_field_*
        for m in re.finditer(r'name="(required_field_[^"]+)"', html):
            fields.setdefault(m.group(1), "")
        return fields

    # ---------------- OpenCode OpenAuth ----------------
    def start_opencode_oauth(self) -> str:
        """
        启动 OpenCode 授权，返回 GitHub OAuth authorize URL。
        """
        self.log("启动 OpenCode 授权...")
        r = self._follow(self._get(OPENCODE_AUTH))
        # 可能停在 auth.opencode.ai/authorize
        if "auth.opencode.ai/authorize" not in r.url and r.status_code == 200:
            # 已经在 authorize 页
            pass

        # 从当前 URL 或跳转链解析 state
        qs = parse_qs(urlparse(r.url).query)
        self.oauth_state_opencode = (qs.get("state") or [""])[0]
        if not self.oauth_state_opencode:
            # 重新构造
            self.oauth_state_opencode = str(uuid.uuid4())
            r = self._follow(
                self._get(
                    OPENAUTH_AUTHORIZE,
                    params={
                        "client_id": "app",
                        "redirect_uri": OPENCODE_AUTH_CALLBACK,
                        "response_type": "code",
                        "state": self.oauth_state_opencode,
                    },
                )
            )

        self.log(f"opencode state = {self.oauth_state_opencode}")

        # 点击 GitHub 登录
        r = self._get(OPENAUTH_GITHUB, headers={"Referer": r.url})
        # 302 -> github oauth
        if r.status_code not in (301, 302, 303, 307, 308):
            raise ProtocolError(f"github/authorize 未跳转: {r.status_code}")
        github_url = urljoin(r.url, r.headers["Location"])
        qs = parse_qs(urlparse(github_url).query)
        self.oauth_state_github = (qs.get("state") or [""])[0]
        self.log(f"github state = {self.oauth_state_github}")
        self.log(f"github oauth url = {github_url[:160]}")
        return github_url

    # ---------------- GitHub 注册 / 登录 ----------------
    def _solve_datadome(self, signup_url: str, return_to: str = "") -> requests.Response:
        """
        自动解决 DataDome 403 挑战:
        用 camoufox (基于 Firefox 的反检测浏览器) 无头模式访问 signup 页面，
        自动执行 DataDome JS 挑战并获取有效 cookie，
        直接返回加载后的 signup 页面 HTML。
        camoufox 无头模式真正无窗口, 且能绕过 DataDome 指纹检测。
        """
        from camoufox.sync_api import Camoufox

        self.log("检测到 DataDome 风控 (403)，启动 camoufox 无头求解...")

        with Camoufox(headless=True) as browser:
            page = browser.new_page()

            # Step 1: 预热 github.com (拿初始 cookie)
            self.log("camoufox 预热 github.com...")
            try:
                page.goto("https://github.com", wait_until="networkidle", timeout=30000)
            except Exception:
                pass

            # Step 2: 访问 signup 页面
            self.log(f"camoufox 访问 signup: {signup_url[:100]}")
            try:
                page.goto(signup_url, wait_until="networkidle", timeout=60000)
            except Exception:
                pass

            # Step 3: 等待 DataDome 处理
            self.log("等待 DataDome 处理...")
            page.wait_for_timeout(8000)

            current_url = page.url
            self.log(f"camoufox 当前: {current_url}")

            # 如果仍在 captcha 页,再等等
            for _ in range(3):
                if "captcha-delivery" not in current_url and "geo.captcha" not in current_url:
                    break
                self.log("仍在 captcha 页,再等 10 秒...")
                page.wait_for_timeout(10000)
                current_url = page.url

            if "captcha-delivery" in current_url or "geo.captcha" in current_url:
                raise ProtocolError("DataDome captcha 未通过")

            # 如果落到 login 或别处,再主动跳一次 signup
            if "/signup" not in current_url:
                self.log("不在 signup,主动跳转...")
                try:
                    page.goto(signup_url, wait_until="networkidle", timeout=30000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)
                current_url = page.url

            # 确认能找到邮箱输入框,说明 signup 页真的加载成功了
            # camoufox 下页面 JS 渲染可能较慢, 多等一会
            # DataDome 挑战有时卡住不渲染, 刷新页面可重新执行挑战 (真实浏览器验证有效)
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
                debug_html = page.content()
                debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signup_debug.html")
                try:
                    with open(debug_path, "w", encoding="utf-8") as f:
                        f.write(debug_html)
                    self.log(f"[调试] signup 页 HTML 已保存: {debug_path} ({len(debug_html)} 字符)")
                except Exception:
                    pass
                # 手动兜底: DataDome 挑战在 headless 下可能无法自动完成,
                # 把 signup URL 抛给上层, 由操作者用真实浏览器打开+刷新后回填 datadome cookie
                raise ProtocolError(
                    f"camoufox 未能通过 DataDome 挑战 (signup 表单未渲染)。"
                    f"请用真实浏览器打开后刷新完成挑战:\n{signup_url}\n"
                    f"拿到页面后复制 datadome cookie 值 (从 DevTools -> Application -> Cookies), 再重试。"
                )

            # 拿页面 HTML 和 cookies
            html = page.content()
            cookies = page.context.cookies()

            new_datadome = ""
            for c in cookies:
                if c["name"] == "datadome":
                    new_datadome = c["value"]
                    break
            self.log(f"camoufox 获取 datadome cookie: {new_datadome[:40]}...")

            # 同步 camoufox 拿到的所有 github.com 域 cookie 到 session
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

        # 构造一个模拟的 Response 对象，包含 camoufox 返回的 HTML
        import requests as _req_module
        fake_response = _req_module.Response()
        fake_response.status_code = 200
        fake_response.url = current_url or signup_url
        fake_response._content = html.encode("utf-8")
        fake_response.encoding = "utf-8"

        self.log(f"camoufox 成功加载 signup 页面: {len(html)} 字符")
        return fake_response

    def _warmup_github(self, return_to: str = "") -> requests.Response:
        """
        预热 GitHub session: 访问 login 页面获取初始 Cookie，再跟随重定向到 signup。
        真实浏览器不会直接跳 signup，必须先经过 login 页拿到 _device_id, _gh_sess 等。
        返回最终的 Response（通常在 signup 页面）。
        """
        self.log("预热 GitHub session (模拟浏览器访问 login 页)...")
        login_url = "https://github.com/login"
        if return_to:
            login_url += "?" + urlencode(
                {"client_id": GITHUB_OAUTH_CLIENT_ID, "return_to": return_to}
            )
        # 首次访问 github.com/login 是 cross-site 导航
        headers = {
            "sec-fetch-site": "none",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "sec-fetch-user": "?1",
        }
        r = self._follow(self._get(login_url, headers=headers))
        self.log(f"预热完成, cookies: {len(self.session.cookies)} 个, url: {r.url[:120]}")
        return r

    def github_signup(
        self,
        email: str,
        password: str,
        username: str,
        country: str = "VN",
        verification_code: Optional[str] = None,
    ) -> None:
        """
        GitHub 注册 + 邮箱验证。
        注意: 可能触发 DataDome / Octocaptcha，纯协议环境下不一定总能过。
        """
        return_to = (
            "/login/oauth/authorize?"
            + urlencode(
                {
                    "client_id": GITHUB_OAUTH_CLIENT_ID,
                    "redirect_uri": GITHUB_OAUTH_REDIRECT,
                    "response_type": "code",
                    "scope": GITHUB_OAUTH_SCOPE,
                    "state": self.oauth_state_github or str(uuid.uuid4()),
                }
            )
        )

        # warmup: 访问 login → 302 重定向 → signup（拿到完整 Cookie）
        r = self._warmup_github(return_to)
        if r.status_code == 403 or "captcha-delivery" in r.text.lower() or "datadome" in r.text.lower():
            # DataDome 拦截，让用户提供 datadome cookie
            signup_url = "https://github.com/signup?" + urlencode({"return_to": return_to})
            r = self._solve_datadome(signup_url)
        if r.status_code != 200:
            raise ProtocolError(f"打开注册页失败: {r.status_code}")

        # 如果 warmup 没有落在 signup 页面，手动跳转
        if "signup" not in r.url:
            signup_url = "https://github.com/signup?" + urlencode({"return_to": return_to})
            self.log("打开 GitHub 注册页...")
            r = self._follow(self._get(signup_url))
            if r.status_code == 403 or "captcha-delivery" in r.text.lower() or "datadome" in r.text.lower():
                r = self._solve_datadome(signup_url)
            if r.status_code != 200:
                raise ProtocolError(f"打开注册页失败: {r.status_code}")

        # 可选: 用户名可用性
        try:
            chk = self._get(
                "https://github.com/signup_check_new/username",
                params={"value": username},
                headers={"Accept": "*/*", "X-Requested-With": "XMLHttpRequest"},
            )
            self.log("username check:", chk.text.strip()[:120])
        except Exception as ex:
            self.log("username check skip:", ex)

        fields = self._extract_authenticity(r.text)
        if not fields.get("authenticity_token"):
            raise ProtocolError("注册页未找到 authenticity_token")

        form = {
            "authenticity_token": fields["authenticity_token"],
            "return_to": return_to,
            "invitation_token": "",
            "repo_invitation_token": "",
            "user[email]": email,
            "user[password]": password,
            "user[login]": username,
            "user_signup[country]": country,
            "filter": "",
            "user_signup[copilot_opt_in]": "1",
            "user_signup[marketing_consent]": "0",
            "octocaptcha-token": "datadome-suppressed",
            "timestamp": fields.get("timestamp", str(int(time.time() * 1000))),
            "timestamp_secret": fields.get("timestamp_secret", ""),
        }
        # honeypot
        for k, v in fields.items():
            if k.startswith("required_field_"):
                form[k] = ""

        self.log(f"提交注册 email={email} user={username}")
        r = self._post(
            "https://github.com/signup?" + urlencode({"return_to": return_to}),
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://github.com",
                "Referer": signup_url,
            },
        )
        self.log(f"POST /signup => {r.status_code}, Location={r.headers.get('Location', '')[:140]}, url={r.url[:140]}")
        # curl_cffi 的 r.headers.get_list 不存在,用 r.cookies 获取响应设置的 cookies
        # 关键: POST /signup 返回新的 _gh_sess,必须正确应用到 session
        post_signup_cookies = {}
        try:
            # curl_cffi Response.cookies 是 Cookies 对象
            for name in r.cookies.keys():
                post_signup_cookies[name] = r.cookies.get(name, "")
        except Exception:
            pass
        for name, val in post_signup_cookies.items():
            self.log(f"  set-cookie: {name}={val[:60]}...")
        # 调试: 422 时输出响应体片段 (看错误原因)
        if r.status_code == 422:
            err_m = re.search(r'(?:error|flash)[^>]*>([^<]{5,200})<', r.text, re.I)
            if err_m:
                self.log(f"  422 错误: {err_m.group(1).strip()}")
            self.log(f"  422 body: {r.text[:1500]}")
        # POST /signup 成功后,服务器会 set 新的 _gh_sess (domain=.github.com)
        # 需要清掉所有旧 _gh_sess (包括 Chrome 设置的 domain=github.com 和预热的 domain=.github.com)
        # 然后用 POST /signup 返回的新 _gh_sess
        try:
            jar = self.session.cookies.jar
            to_remove = [c for c in jar if c.name == "_gh_sess"]
            for c in to_remove:
                jar.clear(c.domain, c.path, c.name)
            if to_remove:
                self.log(f"  清理 {len(to_remove)} 个旧 _gh_sess cookie (domains: {[c.domain for c in to_remove]})")
        except Exception as _e:
            self.log(f"  清理 cookie 失败(忽略): {_e}")
        # 应用 POST /signup 返回的新 cookies (特别是 _gh_sess)
        new_gh_sess = post_signup_cookies.get("_gh_sess", "")
        if new_gh_sess:
            self.session.cookies.set("_gh_sess", new_gh_sess, domain=".github.com")
            self.log(f"  应用新 _gh_sess: {new_gh_sess[:40]}...")
        new_datadome = post_signup_cookies.get("datadome", "")
        if new_datadome:
            try:
                del self.session.cookies["datadome"]
            except Exception:
                pass
            self.session.cookies.set("datadome", new_datadome, domain=".github.com")
        # 注册成功通常 302 到 /account_verifications,不要盲目 _follow (可能被引到 login)
        # 先检查原始响应
        loc = r.headers.get("Location") or ""
        if r.status_code in (301, 302, 303, 307, 308) and "account_verifications" in loc:
            # 直接跳到验证页
            self.log("注册成功,跳转到邮箱验证页")
            # 调试: 输出当前 _gh_sess 值 (前40字符)
            gh_sess = self.session.cookies.get("_gh_sess") or ""
            self.log(f"  _gh_sess (前40): {gh_sess[:40]}...")
            self.log(f"  当前 cookies: {list(self.session.cookies.keys())}")
            # GET account_verifications,不跟随重定向,先看原始响应
            verify_url = urljoin(r.url, loc)
            r2 = self._get(verify_url)
            self.log(f"  GET account_verifications => {r2.status_code}, Location={r2.headers.get('Location', '')[:140]}")
            # 如果被重定向到首页,说明 session 丢了 — 尝试用 POST /signup 响应里的 set-cookie 手动设置
            if r2.status_code in (301, 302, 303, 307, 308) and "account_verifications" not in (r2.headers.get("Location") or ""):
                self.log("  会话丢失,尝试手动从 POST /signup 响应提取 set-cookie")
                # 手动解析 set-cookie 并设置
                try:
                    set_cookies = r.headers.get_list("set-cookie")
                except Exception:
                    set_cookies = [r.headers.get("set-cookie", "")]
                for sc in set_cookies:
                    if "_gh_sess=" in sc:
                        # 提取 _gh_sess 值
                        m_val = re.search(r'_gh_sess=([^;]+)', sc)
                        if m_val:
                            new_sess = m_val.group(1)
                            self.log(f"  手动设置新 _gh_sess: {new_sess[:40]}...")
                            try:
                                del self.session.cookies["_gh_sess"]
                            except Exception:
                                pass
                            self.session.cookies.set("_gh_sess", new_sess, domain=".github.com")
                    if "datadome=" in sc:
                        m_val = re.search(r'datadome=([^;]+)', sc)
                        if m_val:
                            try:
                                del self.session.cookies["datadome"]
                            except Exception:
                                pass
                            self.session.cookies.set("datadome", m_val.group(1), domain=".github.com")
                # 重试 GET
                r2 = self._get(verify_url)
                self.log(f"  重试 GET account_verifications => {r2.status_code}, Location={r2.headers.get('Location', '')[:140]}")
            r = self._follow(r2)
            self.log(f"  跟随后 url={r.url[:140]}")
        elif "account_verifications" in r.url:
            pass  # 已经在验证页
        elif r.status_code in (301, 302, 303, 307, 308):
            # 其他重定向 (如 login),跟随看看
            self.log(f"POST /signup 重定向到: {loc[:140]}")
            r = self._follow(r)
        if "account_verifications" not in r.url and "account_verifications" not in (r.headers.get("Location") or ""):
            # 可能直接成功或被风控
            if r.status_code == 200 and "account_verifications" in r.text:
                pass
            elif "login" in r.url:
                self.log("注册后跳到登录，尝试直接登录")
                self.github_login(email, password, return_to=return_to)
                return
            else:
                # 再请求一次验证页
                r = self._follow(
                    self._get(
                        "https://github.com/account_verifications",
                        params={"return_to": return_to},
                    )
                )

        if "account_verifications" not in r.url:
            # 有些环境注册后直接 success flash
            self.log(f"当前 URL: {r.url}")
            if "oauth/authorize" in r.url or "login" in r.url:
                return
            raise ProtocolError(f"未进入邮箱验证页: {r.url}")

        self._verify_email(return_to, verification_code)
        # 验证后通常需要重新登录
        self.github_login(email, password, return_to=return_to)

    def _verify_email(self, return_to: str, verification_code: Optional[str] = None) -> None:
        """邮箱验证。优先用确认链接 (GET),fallback 用 POST 表单。"""
        # 方案 A: 如果 code_provider 是自动邮箱客户端,优先尝试提取确认链接
        # 确认链接格式: https://github.com/account_verifications/confirm/{token}/{code}
        # 直接 GET 这个链接即可完成验证,绕过 CSRF/Turbo 问题
        confirm_link = None
        code = verification_code

        # 尝试从自动邮箱客户端拿确认链接
        if not code:
            # 检查 code_provider 是否绑定了 CfMailClient
            try:
                from cf_mail import CfMailClient, extract_confirm_link, extract_code
                # 如果 code_provider 是闭包绑定了 mail_client,尝试直接调 poll_verify_link
                # 这里用一个 trick: 检查 code_provider 的 __closure__
                closure = self.code_provider.__closure__ or ()
                _mc = None
                for cell in closure:
                    if isinstance(cell.cell_contents, CfMailClient):
                        _mc = cell.cell_contents
                        break
                if _mc:
                    # 拿到 mail_client,提取确认链接
                    # 需要邮箱地址
                    for addr in _mc._mailboxes.keys():
                        self.log(f"尝试从邮箱 {addr} 提取确认链接...")
                        link = _mc.poll_verify_link(addr, timeout=180, interval=3.0)
                        if link:
                            confirm_link = link
                            # 同时提取验证码 (后续登录可能需要)
                            box = _mc._mailboxes.get(addr)
                            if box:
                                msg = _mc.fetch_latest_message(box)
                                if msg:
                                    code = extract_code(msg, length=8)
                        break
            except ImportError:
                pass
            except Exception as e:
                self.log(f"  提取确认链接失败: {e}")

        # 方案 B: 如果没有确认链接,用 code_provider 拿验证码
        if not confirm_link and not code:
            self.log("等待邮箱验证码 (launch code)...")
            code = self.code_provider(
                "请输入 GitHub 发到邮箱的 8 位验证码(launch code)"
            )
            code = re.sub(r"\s+", "", code)
            if len(code) < 6:
                raise ProtocolError(f"验证码格式异常: {code!r}")

        # 方案 A: 直接 GET 确认链接完成验证
        if confirm_link:
            self.log(f"GET 确认链接完成验证: {confirm_link[:80]}...")
            r = self._get(confirm_link, headers={
                "Referer": "https://github.com/account_verifications",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            })
            r = self._follow(r)
            self.log(f"确认链接验证完成, url={r.url[:140]}, status={r.status_code}")
            # 检查是否成功 (通常重定向到 oauth/authorize 或 dashboard)
            if r.status_code in (200, 302) and "account_verifications" not in r.url:
                self.log("邮箱验证成功 (确认链接方式)")
                return
            # 如果没成功,继续尝试 POST 方案
            self.log(f"确认链接未完成验证,尝试 POST 方案...")

        # 方案 B: POST 表单提交验证码
        if not code:
            raise ProtocolError("无法获取验证码")

        # GET 验证页,提取表单字段
        verify_page_url = "https://github.com/account_verifications?" + urlencode({"return_to": return_to})
        verify_page = self._get(verify_page_url)
        if verify_page.status_code != 200:
            raise ProtocolError(f"无法加载验证页: {verify_page.status_code}")

        # 调试: 保存验证页 HTML 用于分析
        try:
            debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_page_debug.html")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(verify_page.text)
            self.log(f"  [调试] 验证页 HTML 已保存: {debug_path} ({len(verify_page.text)} 字符)")
        except Exception:
            pass

        # 1. 尝试从 form 提取 (标准方式)
        verify_fields = self._extract_authenticity(verify_page.text)
        token = verify_fields.get("authenticity_token", "")
        # 2. fallback: 直接 regex (适配多种 HTML 写法)
        if not token:
            m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', verify_page.text)
            if m:
                token = m.group(1)
        if not token:
            m = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', verify_page.text)
            if m:
                token = m.group(1)
        # 3. fallback: meta tag 或 JS 中的 token
        if not token:
            # <meta name="csrf-token" content="..."> 或 window._csrfToken
            m = re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', verify_page.text)
            if m:
                token = m.group(1)
        if not token:
            # 搜索所有可能的 authenticity_token 出现位置
            matches = re.findall(r'authenticity_token["\']?\s*(?:value=)?["\']([^"\']{20,})["\']', verify_page.text)
            if matches:
                token = matches[0]
        # 4. 最终 fallback: 搜索 data-attribute
        if not token:
            m = re.search(r'data-authenticity-token=["\']([^"\']+)["\']', verify_page.text)
            if m:
                token = m.group(1)
        self.log(f"  提取 authenticity_token: {token[:20]}..." if token else "  [警告] 未找到 authenticity_token")
        if not token:
            # 输出页面所有 form 和 input 信息帮助调试
            soup = self._soup(verify_page.text)
            forms_info = []
            for i, form in enumerate(soup.find_all("form")):
                inputs = [{"name": inp.get("name"), "type": inp.get("type"), "value": (inp.get("value") or "")[:30]} for inp in form.find_all("input")]
                forms_info.append({"form_idx": i, "action": form.get("action", ""), "method": form.get("method", ""), "inputs": inputs[:10]})
            self.log(f"  [调试] 页面 forms: {json.dumps(forms_info, ensure_ascii=False)[:500]}")

        # account_verifications 是 React/Turbo 表单
        # 关键: 从验证页 hidden input 提取实际的 return_to 值 (含 new_signup=true 等)
        form_return_to = return_to
        m_rt = re.search(r'name="return_to"\s+value="([^"]+)"', verify_page.text)
        if m_rt:
            form_return_to = unquote(m_rt.group(1))
            self.log(f"  表单 return_to: {form_return_to[:80]}...")

        # fetch-nonce meta (Turbo 请求可能需要)
        fetch_nonce = ""
        m_nonce = re.search(r'<meta\s+name="fetch-nonce"\s+content="([^"]+)"', verify_page.text)
        if m_nonce:
            fetch_nonce = m_nonce.group(1)

        pairs = [("return_to", form_return_to)]
        for ch in code:
            pairs.append(("launch_code[]", ch))

        # GitHub Turbo 表单用 fetch() 提交
        post_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/vnd.turbo-stream.html, text/html, application/xhtml+xml",
            "Origin": "https://github.com",
            "Referer": verify_page_url,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if fetch_nonce:
            post_headers["X-Requested-With"] = "XMLHttpRequest"

        r = self._post(
            "https://github.com/account_verifications",
            data=pairs,
            headers=post_headers,
        )
        if r.status_code == 422:
            # 验证码错误 — 输出响应体用于诊断
            self.log(f"  [调试] 422 body (前2000): {r.text[:2000]}")
            # 保存完整响应
            try:
                debug_422 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_422_debug.html")
                with open(debug_422, "w", encoding="utf-8") as f:
                    f.write(r.text)
                self.log(f"  [调试] 422 响应已保存: {debug_422}")
            except Exception:
                pass
            try:
                err_msg = ""
                soup = self._soup(r.text)
                flash = soup.select_one(".flash-error, .flash-warn, .flash-alert")
                if flash:
                    err_msg = flash.get_text(strip=True)
            except Exception:
                err_msg = ""
            raise ProtocolError(f"邮箱验证码错误 (422): {err_msg or '验证码无效或已过期'}")
        r = self._follow(r)
        # success flash: Your account was created successfully
        self.log(f"验证完成，当前: {r.url[:140]}")

    def github_login(
        self,
        email: str,
        password: str,
        return_to: Optional[str] = None,
    ) -> None:
        if not return_to:
            return_to = (
                "/login/oauth/authorize?"
                + urlencode(
                    {
                        "client_id": GITHUB_OAUTH_CLIENT_ID,
                        "redirect_uri": GITHUB_OAUTH_REDIRECT,
                        "response_type": "code",
                        "scope": GITHUB_OAUTH_SCOPE,
                        "state": self.oauth_state_github or str(uuid.uuid4()),
                    }
                )
            )

        # warmup: 访问 login → 拿到完整 Cookie
        self._warmup_github(return_to)

        login_url = "https://github.com/login?" + urlencode(
            {
                "client_id": GITHUB_OAUTH_CLIENT_ID,
                "return_to": return_to,
            }
        )
        self.log("打开 GitHub 登录页...")
        r = self._follow(self._get(login_url))
        if r.status_code != 200:
            raise ProtocolError(f"打开登录页失败: {r.status_code}")

        fields = self._extract_authenticity(r.text)
        token = fields.get("authenticity_token")
        if not token:
            raise ProtocolError("登录页未找到 authenticity_token")

        form = {
            "commit": "Sign in",
            "authenticity_token": token,
            "add_account": "",
            "login": email,
            "password": password,
            "webauthn-conditional": "undefined",
            "javascript-support": "true",
            "webauthn-support": "supported",
            "webauthn-iuvpaa-support": "supported",
            "return_to": return_to,
            "allow_signup": "",
            "client_id": GITHUB_OAUTH_CLIENT_ID,
            "integration": "",
            "timestamp": fields.get("timestamp", str(int(time.time() * 1000))),
            "timestamp_secret": fields.get("timestamp_secret", ""),
        }
        for k, v in fields.items():
            if k.startswith("required_field_"):
                form[k] = ""

        r = self._post(
            "https://github.com/session",
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://github.com",
                "Referer": login_url,
            },
        )
        self.log(f"POST /session => {r.status_code}, Location={r.headers.get('Location', '')[:140]}")
        r = self._follow(r)
        self.log(f"  跟随后 url={r.url[:140]}")

        # 登录成功会有 user_session cookie
        if "user_session" not in self.session.cookies:
            # 有时 domain 匹配问题
            if "Incorrect" in r.text:
                raise ProtocolError("GitHub 登录失败: 账号或密码错误")
            if "two-factor" in r.url or "two-factor" in r.text:
                raise ProtocolError("账号开启了 2FA，当前脚本未实现 TOTP/SMS 二次验证")
            # 调试: 输出页面片段看原因
            self.log(f"  登录后页面片段: {r.text[:400]}")
            self.log("警告: 未检测到 user_session，继续尝试 OAuth 授权")

        self.log("GitHub 登录成功(或已有会话)")

    # ---------------- GitHub OAuth 授权 OpenCode ----------------
    def github_authorize_opencode(self) -> str:
        """
        授权 OpenCode 应用，返回最终落到 opencode workspace 的响应 HTML / URL。
        """
        params = {
            "client_id": GITHUB_OAUTH_CLIENT_ID,
            "redirect_uri": GITHUB_OAUTH_REDIRECT,
            "response_type": "code",
            "scope": GITHUB_OAUTH_SCOPE,
            "state": self.oauth_state_github or str(uuid.uuid4()),
        }
        # 新注册场景可能带 new_signup=true
        auth_url = "https://github.com/login/oauth/authorize?" + urlencode(params)
        self.log("打开 OAuth 授权页...")
        r = self._follow(self._get(auth_url))

        # 若已授权过，可能直接 302 到 callback
        if "auth.opencode.ai/github/callback" in r.url or "opencode.ai/auth/callback" in r.url:
            return self._finish_opencode_callback(r)

        if r.status_code != 200:
            raise ProtocolError(f"OAuth 授权页异常: {r.status_code} {r.url}")

        # 需要点 Authorize
        soup = self._soup(r.text)
        form = None
        for f in soup.find_all("form"):
            action = f.get("action") or ""
            if "oauth/authorize" in action or f.find("button", {"name": "authorize"}) or f.find(
                "input", {"name": "authorize"}
            ):
                form = f
                break
        if form is None:
            # 有时页面用 meta refresh 已授权
            m = re.search(
                r'url=(https://auth\.opencode\.ai/github/callback\?[^"\']+)', r.text
            )
            if m:
                r = self._follow(self._get(m.group(1).replace("&amp;", "&")))
                return self._finish_opencode_callback(r)
            raise ProtocolError("未找到 OAuth 授权表单，可能未登录或页面结构变更")

        data = self._hidden_fields(soup, form)
        data["authorize"] = "1"
        action = form.get("action") or "/login/oauth/authorize"
        post_url = urljoin("https://github.com", action)

        r = self._post(
            post_url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://github.com",
                "Referer": auth_url,
            },
        )
        # 成功页含 meta refresh 到 callback
        if r.status_code == 200:
            m = re.search(
                r'url=(https://auth\.opencode\.ai/github/callback\?[^"\'>\s]+)', r.text
            )
            if m:
                cb = m.group(1).replace("&amp;", "&")
                r = self._follow(self._get(cb))
            else:
                r = self._follow(r)
        else:
            r = self._follow(r)

        return self._finish_opencode_callback(r)

    def _finish_opencode_callback(self, resp: requests.Response) -> str:
        """
        从 github callback / opencode callback 跟随到 workspace，返回最终 HTML。
        检测循环重定向: opencode 服务端偶发 session 未建立时会 auth → authorize → auth 死循环。
        """
        r = resp
        seen_urls: set = set()
        # 继续跟到 workspace
        for _ in range(10):
            # 循环检测: 同一 (status, url) 出现两次 => 死循环, 立即停止
            key = (r.status_code, r.url)
            if key in seen_urls:
                raise ProtocolError(f"opencode 回调循环重定向: {r.url[:120]}")
            seen_urls.add(key)
            if r.status_code in (301, 302, 303, 307, 308):
                r = self._follow(r, limit=1)
                continue
            if r.status_code == 200 and "/workspace/" in r.url:
                break
            if r.status_code == 200 and "opencode.ai/auth" in r.url:
                r = self._follow(self._get("https://opencode.ai/auth"))
                continue
            break

        # 若还没到 workspace，主动走 /auth (带循环检测)
        if "/workspace/" not in r.url:
            for _ in range(2):
                r = self._follow(self._get("https://opencode.ai/auth"))
                key = (r.status_code, r.url)
                if key in seen_urls:
                    raise ProtocolError(f"opencode 回调循环重定向: {r.url[:120]}")
                seen_urls.add(key)
                if "/workspace/" in r.url:
                    break

        m = re.search(r"/workspace/(wrk_[A-Z0-9]+)", r.url)
        if m:
            self.workspace_id = m.group(1)
            self.log(f"workspace = {self.workspace_id}")
        else:
            self.log(f"警告: 未识别 workspace, url={r.url}")

        html = r.text if r.status_code == 200 else ""
        if not html and self.workspace_id:
            r = self._get(f"https://opencode.ai/workspace/{self.workspace_id}")
            r = self._follow(r)
            html = r.text
        return html

    # ---------------- 解析 / 创建 API Key ----------------
    def parse_api_key_from_html(self, html: str) -> Optional[ApiKeyInfo]:
        """
        从 workspace SSR HTML 中解析 Default API Key。
        抓包样例:
          {id:"key_...",name:"Default API Key",key:"sk-...",userID:"usr_...",email:"...",keyDisplay:"sk-..."}
        """
        # 完整对象
        m = re.search(
            r'\{id:"(key_[^"]+)",name:"([^"]+)",key:"(sk-[^"]+)"[^}]*?'
            r'userID:"([^"]*)"[^}]*?email:"([^"]*)"[^}]*?keyDisplay:"([^"]*)"',
            html,
        )
        if m:
            info = ApiKeyInfo(
                id=m.group(1),
                name=m.group(2),
                key=m.group(3),
                user_id=m.group(4),
                email=m.group(5),
                key_display=m.group(6),
                workspace_id=self.workspace_id,
            )
            self.api_key = info
            return info

        # 宽松: 任意 sk-
        m = re.search(r'key:"(sk-[A-Za-z0-9]+)"', html)
        if m:
            info = ApiKeyInfo(key=m.group(1), workspace_id=self.workspace_id, name="Default API Key")
            self.api_key = info
            return info

        m = re.search(r"(sk-[A-Za-z0-9]{20,})", html)
        if m:
            info = ApiKeyInfo(key=m.group(1), workspace_id=self.workspace_id)
            self.api_key = info
            return info
        return None

    def fetch_workspace_and_key(self, workspace_id: Optional[str] = None) -> ApiKeyInfo:
        wid = workspace_id or self.workspace_id
        if not wid:
            # 通过 /auth 拿
            r = self._follow(self._get("https://opencode.ai/auth"))
            m = re.search(r"/workspace/(wrk_[A-Z0-9]+)", r.url)
            if not m:
                raise ProtocolError("无法获取 workspace，可能未登录 OpenCode")
            wid = m.group(1)
            self.workspace_id = wid
            html = r.text
        else:
            r = self._follow(self._get(f"https://opencode.ai/workspace/{wid}"))
            html = r.text

        info = self.parse_api_key_from_html(html)
        if not info:
            raise ProtocolError("workspace 页面未解析到 API Key，可尝试 create_key()")
        return info

    def create_key(self, name: str = "Default API Key") -> Optional[str]:
        """
        调用 SolidStart server action key.create。
        首次 OAuth 通常已自动创建，一般不必调用。
        """
        # FormData 风格 action
        url = f"https://opencode.ai/_server"
        # SolidStart 通常 POST body 为序列化参数; 表单场景直接 form fields
        # 前端: createKey form action，字段名以页面为准，常见 name
        files = None
        data = {
            "name": name,
        }
        headers = {
            "Accept": "*/*",
            "Referer": f"https://opencode.ai/workspace/{self.workspace_id}",
            "Origin": "https://opencode.ai",
        }
        # id 作为 query
        r = self.session.post(
            url,
            params={"id": SERVER_FN_CREATE_KEY},
            data=data,
            headers=headers,
            timeout=self.timeout,
        )
        self.log(f"create_key status={r.status_code} body={r.text[:300]}")
        # 再拉 workspace
        try:
            info = self.fetch_workspace_and_key()
            return info.key
        except ProtocolError:
            return None

    def auth_cookie_value(self) -> str:
        # curl_cffi cookies 迭代返回字符串名，直接按名取值
        return self.session.cookies.get("auth") or ""

    # ---------------- 高层流程 ----------------
    def run_login_flow(self, email: str, password: str) -> AuthResult:
        """已有 GitHub 账号: 授权并拿 key。"""
        try:
            self.start_opencode_oauth()
            self.github_login(email, password)
            html = self.github_authorize_opencode()
            info = self.parse_api_key_from_html(html) or self.fetch_workspace_and_key()
            return AuthResult(
                success=True,
                api_key=info,
                workspace_id=self.workspace_id,
                auth_cookie=self.auth_cookie_value(),
                message="ok",
                raw=asdict(info) if info else {},
            )
        except Exception as e:
            return AuthResult(success=False, message=str(e))

    def run_register_flow(
        self,
        email: str,
        password: str,
        username: str,
        country: str = "VN",
        verification_code: Optional[str] = None,
    ) -> AuthResult:
        """新注册 GitHub + 授权 OpenCode + 拿 key。直接走接口,Chrome 仅用于过 DataDome。"""
        try:
            self.start_opencode_oauth()
            # github_signup 内部: warmup -> 若 403 则 _solve_datadome (Chrome 过 DataDome 拿 cookie) -> POST /signup 接口
            self.github_signup(email, password, username, country, verification_code)
            # OAuth 授权 + 回调到 opencode workspace
            # 注意: opencode 服务端偶发循环重定向, 这里重试只重走 OAuth 授权, 不重新注册
            html = ""
            last_err: Optional[Exception] = None
            for attempt in range(3):
                try:
                    if attempt > 0:
                        # 重试: 重新启动 opencode oauth 拿新 state (旧的已消费), GitHub 已登录无需重注册
                        self.log(f"OAuth 回调重试 ({attempt+1}/3): 重新启动 opencode oauth...")
                        # 清理 opencode 域的脏 cookies (保留 github 登录态)
                        for ck_name in ("auth",):
                            try:
                                del self.session.cookies[ck_name]
                            except Exception:
                                pass
                        self.start_opencode_oauth()
                        time.sleep(1)
                    html = self.github_authorize_opencode()
                    if self.workspace_id:
                        break
                    # 没拿到 workspace 也算失败, 重试
                    raise ProtocolError(f"未识别 workspace, url={getattr(html, 'url', '')[:100] if html else 'empty'}")
                except ProtocolError as e:
                    last_err = e
                    if attempt < 2:
                        self.log(f"OAuth 回调失败 (尝试 {attempt+1}/3): {str(e)[:100]}, 重试...")
                        time.sleep(2)
                    else:
                        raise
            info = self.parse_api_key_from_html(html) or self.fetch_workspace_and_key()
            return AuthResult(
                success=True,
                api_key=info,
                workspace_id=self.workspace_id,
                auth_cookie=self.auth_cookie_value(),
                message="ok",
                raw=asdict(info) if info else {},
            )
        except Exception as e:
            return AuthResult(success=False, message=str(e))

    def run_github_only_flow(
        self,
        email: str,
        password: str,
        username: str,
        country: str = "VN",
        verification_code: Optional[str] = None,
    ) -> AuthResult:
        """纯 GitHub 注册 (不绑定 OpenCode, 不拿 API Key)。"""
        try:
            self.github_signup(email, password, username, country, verification_code)
            self.log("GitHub 注册+登录完成")
            return AuthResult(
                success=True,
                api_key=None,
                workspace_id="",
                auth_cookie="",
                message="ok",
                raw={"email": email, "username": username},
            )
        except Exception as e:
            return AuthResult(success=False, message=str(e))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="OpenCode 注册/授权/获取 API Key 协议工具")
    p.add_argument("--proxy", default=None, help="HTTP 代理, 如 http://127.0.0.1:7890")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--code", default=None, help="邮箱验证码(register 时可用)")
    p.add_argument("--out", default="apikey.json", help="单次结果输出文件")
    p.add_argument("--db", default="account/accounts.json", help="账号库 JSON 文件 (注册成功后追加写入)")

    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--email", required=True)
        sp.add_argument("--password", required=True)

    sp = sub.add_parser("login", help="已有 GitHub 账号登录授权拿 key")
    add_common(sp)

    sp = sub.add_parser("register", help="注册 GitHub 并授权拿 key")
    sp.add_argument("--email", default=None, help="邮箱(不填则交互输入,或 --auto-mail 自动创建)")
    sp.add_argument("--username", default=None, help="GitHub 用户名(不填则自动生成)")
    sp.add_argument("--password", default=None, help="密码(不填则自动生成)")
    sp.add_argument("--country", default="VN")
    sp.add_argument("--auto-mail", action="store_true", help="自动创建 CF 临时邮箱并轮询验证码(全程免输入)")

    sp = sub.add_parser("full", help="同 register")
    sp.add_argument("--email", default=None, help="邮箱(不填则交互输入,或 --auto-mail 自动创建)")
    sp.add_argument("--username", default=None, help="GitHub 用户名(不填则自动生成)")
    sp.add_argument("--password", default=None, help="密码(不填则自动生成)")
    sp.add_argument("--country", default="VN")
    sp.add_argument("--auto-mail", action="store_true", help="自动创建 CF 临时邮箱并轮询验证码(全程免输入)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    # 启用 Windows 终端 ANSI 颜色支持
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

    # --auto-mail: 自动创建 CF 临时邮箱 + 自动轮询验证码 (全程免输入)
    mail_client = None
    auto_code_provider = None
    if getattr(args, "auto_mail", False):
        from cf_mail import CfMailClient
        mail_client = CfMailClient()
        log_info("MAIL", "自动创建 CF 临时邮箱")
        auto_email = mail_client.create_address()
        log_ok("MAIL", f"创建邮箱成功: {auto_email}")
        # 覆盖 --email
        args.email = auto_email
        # 自动轮询验证码的 provider
        _mc = mail_client
        def auto_code_provider(hint: str = "") -> str:
            log_info("MAIL", "开始轮询验证码 (最多等 180 秒)...")
            code = _mc.poll_verify_code(auto_email, timeout=180, interval=3.0, code_length=8)
            if not code:
                raise ProtocolError(f"自动拉取验证码超时: {auto_email}")
            return code
        auto_code_provider = auto_code_provider

    client = OpenCodeClient(
        proxy=args.proxy,
        verbose=not args.quiet,
        code_provider=auto_code_provider,
    )

    if args.cmd == "login":
        result = client.run_login_flow(args.email, args.password)
        reg_username = None
        reg_password = args.password
    else:
        # register / full: 邮箱交互输入，用户名和密码自动生成
        email = args.email or input(_fmt_log("IN", "请输入邮箱: ", _C_WARN)).strip()
        username = args.username or generate_username()
        password = args.password or generate_password()
        log_info("OC", f"自动生成用户名: {username}")
        log_info("OC", f"自动生成密码: {password}")

        result = client.run_register_flow(
            email=email,
            password=password,
            username=username,
            country=getattr(args, "country", "VN"),
            verification_code=args.code,
        )
        reg_username = username
        reg_password = password

    payload = {
        "success": result.success,
        "message": result.message,
        "workspace_id": result.workspace_id,
        "auth_cookie": result.auth_cookie[:32] + "..." if result.auth_cookie else "",
        "api_key": asdict(result.api_key) if result.api_key else None,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(
            {
                **payload,
                "auth_cookie": result.auth_cookie,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # 注册成功后落库到 accounts.json (追加,不覆盖)
    if result.success and result.api_key:
        log_ok("OC", f"API KEY: {result.api_key.key}")
        _save_to_db(args.db, result, reg_username, reg_password)
        return 0
    return 1


def _save_to_db(db_path: str, result: AuthResult, username: Optional[str], password: str, email: str = "", mode: str = "opencode") -> None:
    """把注册成功的账号信息追加到账号库 JSON 文件。
    格式: [{email, username, password, api_key, workspace_id, auth_cookie, mode, created_at}, ...]
    mode: "opencode" | "github"
    """
    from datetime import datetime, timezone, timedelta
    # 读取已有数据 (文件不存在则空列表)
    accounts: list = []
    if os.path.exists(db_path):
        try:
            with open(db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                accounts = data
            elif isinstance(data, dict) and "accounts" in data:
                accounts = data["accounts"]
        except Exception:
            accounts = []
    # 构造新记录
    tz = timezone(timedelta(hours=8))
    record_email = email or (result.api_key.email if result.api_key else "")
    record = {
        "email": record_email,
        "username": username or "",
        "password": password or "",
        "api_key": result.api_key.key if result.api_key else "",
        "api_key_id": result.api_key.id if result.api_key else "",
        "key_display": result.api_key.key_display if result.api_key else "",
        "user_id": result.api_key.user_id if result.api_key else "",
        "workspace_id": result.workspace_id or "",
        "auth_cookie": result.auth_cookie or "",
        "mode": mode,
        "created_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 去重: 同邮箱只保留最新 (更新而非追加)
    accounts = [a for a in accounts if a.get("email") != record["email"]]
    accounts.append(record)
    # 写入 (自动创建目录)
    db_dir = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(db_dir, exist_ok=True)
    with open(db_path, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    log_ok("DB", f"已落库到 {db_path} (共 {len(accounts)} 条账号)")


if __name__ == "__main__":
    sys.exit(main())
