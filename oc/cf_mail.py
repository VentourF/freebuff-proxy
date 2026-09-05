# -*- coding: utf-8 -*-
r"""Cloudflare Temp Email 直连客户端 (不走 DDG 转发)。

MailClient 的 CF 直建域名路径：
  1. POST /admin/new_address  → 创建邮箱，拿 address + jwt
  2. GET  /api/parsed_mails   → 用 jwt 拉信列表
  3. extract_code             → 从邮件正文抽取验证码 (支持 6/8 位)

用法:
  from cf_mail import CfMailClient
  client = CfMailClient()
  email = client.create_address()          # 创建临时邮箱
  code  = client.poll_verify_code(email)   # 轮询验证码
"""
from __future__ import annotations

import random
import re
import string
import time
from email import message_from_string
from email.policy import default as default_policy
from typing import Any, Optional

from curl_cffi import requests as cffi_requests


# ---------------- 日志格式化 ----------------
_C_RESET = "\033[0m"
_C_TIME = "\033[90m"
_C_TAG = "\033[36m"
_C_INFO = "\033[37m"
_C_OK = "\033[32m"
_C_WARN = "\033[33m"
_C_ERR = "\033[31m"
_C_DEBUG = "\033[90m"


def _fmt_log(tag: str, msg: str, color: str = _C_INFO) -> str:
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    return f"{_C_TIME}{ts}{_C_RESET} {_C_TAG}[{tag}]{_C_RESET} {color}{msg}{_C_RESET}"


# Web 日志推送 handler (由 web_app.py 设置, 非空时日志同时推送到 WebSocket)
_log_handler = None


def set_log_handler(fn):
    """设置全局日志 handler, 用于 Web 界面实时推送日志。"""
    global _log_handler
    _log_handler = fn


# 颜色码 -> level 映射 (反向推断)
_COLOR_LEVEL = {_C_OK: "ok", _C_ERR: "err", _C_WARN: "warn", _C_DEBUG: "debug", _C_INFO: "info"}


def _emit_log(tag: str, msg: str, color: str = _C_INFO):
    """输出日志: 如果有 Web handler 则推送, 否则 print 到终端"""
    if _log_handler:
        level = _COLOR_LEVEL.get(color, "info")
        _log_handler(tag, msg, level)
    else:
        print(_fmt_log(tag, msg, color), flush=True)


# ---------------- 默认配置 (直连，无代理) ----------------
# ⚠️ 占位默认值：真实部署请通过 web_config.json 传入 api_base / admin_password / domains，
#    或实例化 CfMailClient 时显式指定。仓库不提交真实 CF worker 配置。
CF_MAIL_API_BASE = "https://your-cf-mail-worker.example.workers.dev"
CF_MAIL_ADMIN_PASSWORD = "CHANGE_ME"
CF_MAIL_DOMAINS = ["mail.example.com"]
REGISTER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ---------------- 工具函数 ----------------
def random_mailbox_name() -> str:
    """生成真实人名风格的邮箱前缀 (避免随机字符串被风控)。
    多种模式: firstname.lastname, flastname, lastname.firstname, 名字+年份 等。
    """
    first_names = [
        "alex", "sam", "jordan", "taylor", "morgan", "casey", "riley", "jamie",
        "chris", "nick", "max", "leo", "ryan", "tom", "ben", "dan", "jack", "luke",
        "mia", "emma", "ava", "lily", "nora", "isla", "zoe", "ivy", "amy", "kate",
        "liam", "noah", "ethan", "lucas", "mason", "logan", "owen", "carter",
        "aria", "ella", "luna", "ruby", "jade", "sage", "hazel", "willow",
        "oliver", "henry", "leo", "finn", "emma", "charlie", "daniel", "james",
    ]
    last_names = [
        "smith", "jones", "brown", "davis", "wilson", "miller", "moore", "taylor",
        "lee", "clark", "lewis", "walker", "hall", "young", "king", "wright",
        "hill", "green", "adams", "baker", "carter", "cooper", "bell", "ward",
        "rivera", "ross", "powell", "owens", "perry", "butler", "foster", "reyes",
        "nelson", "morgan", "murphy", "rice", "rossi", "khan", "shah", "patel",
    ]
    f = random.choice(first_names)
    l = random.choice(last_names)
    mode = random.randint(0, 6)
    if mode == 0:
        # firstname.lastname
        return f"{f}.{l}"
    if mode == 1:
        # firstname.lastname + 2-3 位数字
        return f"{f}.{l}{random.randint(1, 99)}"
    if mode == 2:
        # flastname (首字母+姓)
        return f"{f[0]}{l}"
    if mode == 3:
        # firstname_lastname 风格用连字符 (邮箱前缀允许)
        return f"{f}-{l}{random.randint(1, 99)}"
    if mode == 4:
        # firstname + 出生年份 (85-99)
        year = random.randint(85, 99)
        return f"{f}{year}{random.randint(0, 9)}"
    if mode == 5:
        # lastname.firstname
        return f"{l}.{f}"
    # 名字 + 随机短词
    words = ["dev", "code", "tech", "art", "web", "lab", "hq", "io", "app", "ux"]
    return f"{f}.{random.choice(words)}{random.randint(1, 99)}"


def email_domain(email: str) -> str:
    text = str(email or "").strip().lower()
    if "@" not in text:
        return ""
    return text.rsplit("@", 1)[-1].strip()


def extract_content(data: dict[str, Any]) -> tuple[str, str]:
    """从邮件 dict 提取 (text, html)"""
    text = str(data.get("text_content") or data.get("text") or data.get("body") or data.get("content") or "")
    html = str(data.get("html_content") or data.get("html") or data.get("html_body") or data.get("body_html") or "")
    if text or html:
        return text, html
    raw = data.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        return "", ""
    try:
        parsed = message_from_string(raw, policy=default_policy)
    except Exception:
        return raw, ""
    plain: list[str] = []
    html_parts: list[str] = []
    for part in (parsed.walk() if parsed.is_multipart() else [parsed]):
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_content()
        except Exception:
            payload = ""
        if part.get_content_type() == "text/html":
            html_parts.append(str(payload))
        else:
            plain.append(str(payload))
    return "\n".join(plain), "\n".join(html_parts)


def _extract_text_candidates(value: Any) -> list[str]:
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, list):
        for v in value:
            out.extend(_extract_text_candidates(v))
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_extract_text_candidates(v))
    return out


def message_matches_email(data: dict[str, Any], email: str) -> bool:
    target = str(email or "").strip().lower()
    candidates: list[str] = []
    for key in ("to", "mailTo", "receiver", "receivers", "address", "email", "envelope_to"):
        if key in data:
            candidates.extend(_extract_text_candidates(data.get(key)))
    return (
        not target
        or not candidates
        or any(target in str(item).strip().lower() for item in candidates if str(item).strip())
    )


def extract_code(message: dict[str, Any], length: int = 8) -> Optional[str]:
    """从邮件抽取验证码。GitHub launch code 是 8 位，OpenAI OTP 是 6 位。"""
    content = (
        f"{message.get('subject', '')}\n"
        f"{message.get('text_content', '')}\n"
        f"{message.get('html_content', '')}"
    ).strip()
    if not content:
        return None
    pat = rf"\b(\d{{{length}}})\b"
    # 1) 带标签提示 (适配 GitHub: "the code below:" / "launch code" / "code is")
    m = re.search(
        rf"(?:Verification code|code is|code below|代码为|验证码|launch code)[:\s]*(\d{{{length}}})",
        content, re.I,
    )
    if m:
        return m.group(1)
    # 2) HTML 样式块
    m = re.search(rf"background-color:\s*#F3F3F3[^>]*>[\s\S]*?(\d{{{length}}})[\s\S]*?</p>", content, re.I)
    if m:
        return m.group(1)
    # 3) GitHub 邮件: 标题含 "launch code" 时，正文第一个 8 位数字即为验证码
    if "launch code" in content.lower():
        m = re.search(pat, content)
        if m and m.group(1) not in ("177010", "000000"):
            return m.group(1)
    # 4) 纯数字 (排除已知干扰码)
    for code in re.findall(pat, content):
        if code and code not in ("177010", "000000"):
            return code
    return None


def extract_confirm_link(message: dict[str, Any]) -> Optional[str]:
    """从 GitHub 邮件提取验证确认链接。
    格式: https://github.com/account_verifications/confirm/{token}/{code}
    """
    content = (
        f"{message.get('text_content', '')}\n"
        f"{message.get('html_content', '')}"
    ).strip()
    if not content:
        return None
    m = re.search(
        r'https?://github\.com/account_verifications/confirm/[a-f0-9\-]+/\d+',
        content,
        re.I,
    )
    if m:
        return m.group(0)
    return None


# ---------------- CF 直连客户端 ----------------
class CfMailClient:
    """Cloudflare Temp Email 直连客户端 (无 DDG 转发)。"""

    def __init__(
        self,
        api_base: str = None,
        admin_password: str = None,
        domains: list = None,
        proxy: str = "",
    ):
        self.api_base = (api_base or CF_MAIL_API_BASE).rstrip("/")
        self.admin_password = admin_password or CF_MAIL_ADMIN_PASSWORD
        self.domains = [str(d).strip().lower() for d in (domains or CF_MAIL_DOMAINS) if str(d).strip()]
        self.proxy = proxy or ""
        self._session: Optional[cffi_requests.Session] = None
        # email -> {token, address, domain}
        self._mailboxes: dict = {}
        self._prefer_parsed: Optional[bool] = None

    def _get_session(self) -> cffi_requests.Session:
        if self._session is None:
            kwargs = {"impersonate": "chrome", "timeout": 30, "verify": False}
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            self._session = cffi_requests.Session(**kwargs)
        return self._session

    def _request(
        self,
        method: str,
        path: str,
        headers: dict = None,
        params: dict = None,
        payload: dict = None,
        expected: tuple = (200,),
    ) -> Any:
        s = self._get_session()
        merged = {
            "Content-Type": "application/json",
            "User-Agent": REGISTER_USER_AGENT,
            **(headers or {}),
        }
        url = f"{self.api_base}{path}"
        r = s.request(
            method.upper(),
            url,
            headers=merged,
            params=params,
            json=payload,
        )
        if r.status_code not in expected:
            raise RuntimeError(
                f"Mail 请求失败: {method} {path}, HTTP {r.status_code}, body={r.text[:300]}"
            )
        if r.status_code == 204:
            return {}
        try:
            return r.json()
        except Exception:
            return {}

    # ---- 创建邮箱 ----
    def create_address(self, domain: str = None) -> str:
        """POST /admin/new_address 创建临时邮箱，返回 address。"""
        dom = (domain or random.choice(self.domains)).strip().lower()
        data = self._request(
            "POST",
            "/admin/new_address",
            headers={"x-admin-auth": self.admin_password},
            payload={
                "enablePrefix": True,
                "name": random_mailbox_name(),
                "domain": dom,
            },
        )
        address = str(data.get("address") or "").strip()
        token = str(data.get("jwt") or "").strip()
        if not address or not token:
            raise RuntimeError(f"CF 创建邮箱缺少 address/jwt (domain={dom}): {data}")
        used_domain = email_domain(address) or dom
        self._mailboxes[address.lower()] = {
            "address": address,
            "token": token,
            "domain": used_domain,
        }
        return address

    # ---- 拉信列表 ----
    def _list_items(self, token: str) -> list[dict]:
        auth = {"Authorization": f"Bearer {token}"}
        if self._prefer_parsed is not False:
            try:
                data = self._request(
                    "GET",
                    "/api/parsed_mails",
                    headers=auth,
                    params={"limit": 20, "offset": 0},
                    expected=(200,),
                )
                self._prefer_parsed = True
                raw = list(data.get("results") or []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                return [x for x in raw if isinstance(x, dict)]
            except Exception as e:
                if "404" in str(e) or "HTTP 404" in str(e):
                    self._prefer_parsed = False
        data = self._request(
            "GET",
            "/api/mails",
            headers=auth,
            params={"limit": 20, "offset": 0},
        )
        raw = list(data.get("results") or []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        return [x for x in raw if isinstance(x, dict)]

    def _fetch_raw_mail(self, token: str, mail_id: Any) -> dict:
        if mail_id is None or mail_id == "":
            return {}
        auth = {"Authorization": f"Bearer {token}"}
        try:
            data = self._request("GET", f"/api/mail/{mail_id}", headers=auth)
            return data if isinstance(data, dict) else {}
        except Exception:
            try:
                data = self._request("GET", f"/api/mails/{mail_id}", headers=auth)
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

    def _fill_body(self, item: dict, token: str) -> tuple[dict, str, str]:
        text, html = extract_content(item)
        if text or html:
            return item, text, html
        mid = item.get("id") if item.get("id") is not None else item.get("_id")
        if self._prefer_parsed and mid is not None:
            try:
                detail = self._request(
                    "GET",
                    f"/api/parsed_mail/{mid}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if isinstance(detail, dict):
                    item = {**item, **detail}
                    text, html = extract_content(item)
            except Exception:
                pass
        if not text and not html and mid is not None:
            detail = self._fetch_raw_mail(token, mid)
            if detail:
                item = {**item, **detail}
                if not item.get("raw") and isinstance(detail.get("source"), str):
                    item["raw"] = detail["source"]
                if not item.get("raw") and isinstance(detail.get("raw"), str):
                    item["raw"] = detail["raw"]
                text, html = extract_content(item)
        return item, text, html

    def fetch_latest_message(self, mailbox: dict) -> Optional[dict]:
        token = mailbox.get("token") or ""
        address = str(mailbox.get("address") or "")
        items = self._list_items(token)
        if not items:
            return None
        messages = [item for item in items if message_matches_email(item, address)]
        if not messages:
            messages = items
        item = messages[0]
        item, text, html = self._fill_body(item, token)
        sender = item.get("from") or item.get("sender") or item.get("source") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {
            "mailbox": address,
            "message_id": str(item.get("id") or item.get("_id") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": str(sender),
            "text_content": text,
            "html_content": html,
            "raw": item,
            "created_at": item.get("created_at") or item.get("date") or item.get("createdAt"),
        }

    # ---- 轮询验证码 ----
    def poll_verify_code(
        self,
        email: str,
        timeout: int = 120,
        interval: float = 2.0,
        since: float = None,
        code_length: int = 8,
    ) -> Optional[str]:
        """轮询邮箱直到拿到验证码，返回 code 字符串或 None。"""
        box = self._mailboxes.get(str(email or "").strip().lower())
        if not box or not box.get("token"):
            _emit_log("MAIL", f"邮箱 {email} 无 jwt，无法拉信（需先 create_address）", _C_ERR)
            return None
        seen: set = set()
        deadline = time.monotonic() + float(timeout)
        poll_i = 0
        while time.monotonic() < deadline:
            poll_i += 1
            try:
                message = self.fetch_latest_message(box)
            except Exception as e:
                if poll_i == 1 or poll_i % 10 == 0:
                    _emit_log("MAIL", f"拉信失败: {e}", _C_WARN)
                time.sleep(max(0.5, float(interval)))
                continue
            if message:
                ref = str(message.get("message_id") or message.get("subject") or "")
                text_len = len(message.get("text_content") or "") + len(message.get("html_content") or "")
                seen_key = f"{ref}|{text_len}"
                if seen_key not in seen:
                    seen.add(seen_key)
                    if poll_i == 1 or text_len > 0:
                        subj = (message.get("subject") or "")[:80]
                        _emit_log("MAIL", f"收件箱命中: id={ref} subject={subj!r} body={text_len}B", _C_DEBUG)
                    code = extract_code(message, length=code_length)
                    if code:
                        _emit_log("MAIL", f"抽码成功: {code}", _C_OK)
                        return code
            time.sleep(max(0.5, float(interval)))
        return None

    # ---- 轮询验证确认链接 (GitHub 特有) ----
    def poll_verify_link(
        self,
        email: str,
        timeout: int = 120,
        interval: float = 2.0,
    ) -> Optional[str]:
        """轮询邮箱直到拿到 GitHub 验证确认链接，返回 URL 或 None。
        链接格式: https://github.com/account_verifications/confirm/{token}/{code}
        直接 GET 这个链接即可完成验证，无需 POST 表单。
        """
        box = self._mailboxes.get(str(email or "").strip().lower())
        if not box or not box.get("token"):
            _emit_log("MAIL", f"邮箱 {email} 无 jwt，无法拉信（需先 create_address）", _C_ERR)
            return None
        seen: set = set()
        deadline = time.monotonic() + float(timeout)
        poll_i = 0
        while time.monotonic() < deadline:
            poll_i += 1
            try:
                message = self.fetch_latest_message(box)
            except Exception as e:
                if poll_i == 1 or poll_i % 10 == 0:
                    _emit_log("MAIL", f"拉信失败: {e}", _C_WARN)
                time.sleep(max(0.5, float(interval)))
                continue
            if message:
                ref = str(message.get("message_id") or message.get("subject") or "")
                text_len = len(message.get("text_content") or "") + len(message.get("html_content") or "")
                seen_key = f"{ref}|{text_len}"
                if seen_key not in seen:
                    seen.add(seen_key)
                    if poll_i == 1 or text_len > 0:
                        subj = (message.get("subject") or "")[:80]
                        _emit_log("MAIL", f"收件箱命中: id={ref} subject={subj!r} body={text_len}B", _C_DEBUG)
                    link = extract_confirm_link(message)
                    if link:
                        _emit_log("MAIL", f"提取确认链接成功: {link[:80]}...", _C_OK)
                        return link
            time.sleep(max(0.5, float(interval)))
        return None


if __name__ == "__main__":
    # 自测: 创建邮箱并打印
    c = CfMailClient()
    addr = c.create_address()
    _emit_log("MAIL", f"创建邮箱: {addr}", _C_OK)
    _emit_log("MAIL", f"邮箱列表: {list(c._mailboxes.keys())}", _C_INFO)
