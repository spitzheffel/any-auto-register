"""Cursor 注册协议核心实现"""
import re, uuid, json, urllib.parse, random, string
from typing import Optional, Callable

AUTH   = "https://authenticator.cursor.sh"
CURSOR = "https://cursor.com"

ACTION_SUBMIT_EMAIL    = "d0b05a2a36fbe69091c2f49016138171d5c1e4cd"
ACTION_SUBMIT_PASSWORD = "fef846a39073c935bea71b63308b177b113269b7"
ACTION_MAGIC_CODE      = "f9e8ae3d58a7cd11cccbcdbf210e6f2a6a2550dd"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/145.0.0.0 Safari/537.36")

TURNSTILE_SITEKEY = "0x4AAAAAAAMNIvC45A4Wjjln"

_ACTION_FAILURE_HINTS = (
    "invalid captcha",
    "captcha failed",
    "failed captcha",
    "unable to verify",
    "are you human",
    "too many requests",
    "rate limit",
    "access denied",
    "unauthorized",
    "forbidden",
    "user_already_exists",
    "email already",
    "invalid email",
    "account exists",
    "something went wrong",
)


def _rand_password(n=16):
    chars = string.ascii_letters + string.digits + "!@#$"
    return "".join(random.choices(chars, k=n))


def _boundary():
    return "----WebKitFormBoundary" + "".join(
        random.choices(string.ascii_letters + string.digits, k=16))


def _multipart(fields: dict, boundary: str) -> bytes:
    parts = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode()


def _header_get(resp, name: str) -> str:
    headers = getattr(resp, "headers", None) or {}
    getter = getattr(headers, "get", None)
    if callable(getter):
        for key in (name, name.lower(), name.title()):
            value = getter(key)
            if value:
                return str(value)
    items = getattr(headers, "items", None)
    if callable(items):
        target = name.lower()
        for key, value in items():
            if str(key).lower() == target and value:
                return str(value)
    return ""


def action_redirect(resp) -> str:
    return (_header_get(resp, "x-action-redirect") or _header_get(resp, "location")).strip()


def _compact_text(text: str, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    return compact[:limit]


def response_preview(resp, limit: int = 240) -> str:
    status = getattr(resp, "status_code", "?")
    loc = action_redirect(resp)
    body = _compact_text(getattr(resp, "text", "") or "", limit)
    parts = [f"HTTP {status}"]
    if loc:
        parts.append(f"redirect={loc[:160]}")
    if body:
        parts.append(f"body={body}")
    return " | ".join(parts)


def _quoted_error_values(text: str) -> list[str]:
    values = []
    for match in re.finditer(
        r'"(?:error(?:_description|_message|Message)?|message|digest|code)"\s*:\s*"((?:\\.|[^"\\])*)"',
        text or "",
        re.I,
    ):
        raw = match.group(1).replace('\\"', '"')
        if raw and raw.lower() not in {"ok", "success", "none", "null"}:
            values.append(raw)
    return values


def find_action_failure(resp, *, stuck_on: tuple[str, ...] = ()) -> str:
    """从 Next.js server action 响应里挑失败信号。没有把握时返回空，避免误杀成功路径。"""
    status = int(getattr(resp, "status_code", 0) or 0)
    text = str(getattr(resp, "text", "") or "")
    loc = action_redirect(resp)
    if status >= 400:
        return response_preview(resp)
    blob = f"{loc}\n{text}".lower()
    for hint in _ACTION_FAILURE_HINTS:
        if hint in blob:
            return f"检测到失败信号 `{hint}`: {response_preview(resp)}"
    for value in _quoted_error_values(text):
        lowered = value.lower()
        if "error" in lowered or any(hint in lowered for hint in _ACTION_FAILURE_HINTS):
            return f"{value}: {response_preview(resp)}"
    if loc and stuck_on:
        loc_l = loc.lower()
        if any(marker.lower() in loc_l for marker in stuck_on):
            return f"仍停留在 {stuck_on[0]}: {response_preview(resp)}"
    return ""


class CursorRegister:
    def __init__(self, proxy: str = None, log_fn: Callable = print):
        from curl_cffi import requests as curl_req
        self.log = log_fn
        self.s = curl_req.Session(impersonate="safari17_0")
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}

    def _base_headers(self, next_action, referer, boundary=None):
        ct = f"multipart/form-data; boundary={boundary}" if boundary else "application/x-www-form-urlencoded"
        return {
            "user-agent": UA,
            "accept": "text/x-component",
            "content-type": ct,
            "origin": AUTH,
            "referer": referer,
            "next-action": next_action,
            "next-router-state-tree": "%5B%22%22%2C%7B%22children%22%3A%5B%22(main)%22%2C%7B%22children%22%3A%5B%22(root)%22%2C%7B%22children%22%3A%5B%22(sign-in)%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%5D%7D%5D%7D%5D%7D%5D%7D%5D",
        }

    def _post_action(self, next_action, referer, fields, *, step: str, stuck_on: tuple[str, ...] = ()):
        bd = _boundary()
        resp = self.s.post(
            f"{AUTH}/sign-up",
            headers=self._base_headers(next_action, referer, boundary=bd),
            data=_multipart(fields, bd),
            allow_redirects=False,
        )
        preview = response_preview(resp)
        self.log(f"{step} 响应: {preview}")
        failure = find_action_failure(resp, stuck_on=stuck_on)
        if failure:
            raise RuntimeError(f"{step} 失败，Cursor 未接受本次提交: {failure}")
        return resp

    def step1_get_session(self):
        nonce = str(uuid.uuid4())
        state = {"returnTo": "https://cursor.com/dashboard", "nonce": nonce}
        state_encoded = urllib.parse.quote(urllib.parse.quote(json.dumps(state)))
        url = f"{AUTH}/?state={state_encoded}"
        self.s.get(url, headers={"user-agent": UA, "accept": "text/html"}, allow_redirects=True)
        state_cookie_name = None
        for cookie in self.s.cookies.jar:
            if 'state-' in cookie.name:
                state_cookie_name = cookie.name
                break
        return state_encoded, state_cookie_name

    def step2_submit_email(self, email, state_encoded):
        referer = f"{AUTH}/sign-up?state={state_encoded}"
        self._post_action(
            ACTION_SUBMIT_EMAIL,
            referer,
            {"1_state": state_encoded, "email": email},
            step="Step2 提交邮箱",
            stuck_on=("/sign-in",),
        )

    def step3_submit_password(self, password, email, state_encoded, captcha_solver=None):
        referer = f"{AUTH}/sign-up?state={state_encoded}"
        captcha_token = ""
        if captcha_solver:
            self.log("获取 Turnstile token...")
            # token 跟页面 URL 绑定，要用实际报名页，不能只用站点根路径
            captcha_token = str(captcha_solver.solve_turnstile(referer, TURNSTILE_SITEKEY) or "").strip()
            if not captcha_token:
                raise RuntimeError("Turnstile token 为空，无法提交密码，Cursor 不会发 OTP")
            self.log(f"Turnstile token 已获取 ({len(captcha_token)} 字符)")
        self._post_action(
            ACTION_SUBMIT_PASSWORD,
            referer,
            {
                "1_state": state_encoded,
                "email": email,
                "password": password,
                "captchaToken": captcha_token,
            },
            step="Step3 提交密码",
            stuck_on=("/sign-up/password",),
        )

    def step4_submit_otp(self, otp, email, state_encoded):
        bd = _boundary()
        referer = f"{AUTH}/sign-up?state={state_encoded}"
        body = _multipart({"1_state": state_encoded, "email": email, "otp": otp}, bd)
        r = self.s.post(f"{AUTH}/sign-up",
                        headers=self._base_headers(ACTION_MAGIC_CODE, referer, boundary=bd),
                        data=body, allow_redirects=False)
        loc = r.headers.get("location", "")
        m = re.search(r'code=([\w-]+)', loc)
        return m.group(1) if m else ""

    def step5_get_token(self, auth_code, state_encoded):
        url = f"{CURSOR}/api/auth/callback?code={auth_code}&state={state_encoded}"
        self.s.get(url, headers={"user-agent": UA, "accept": "text/html"}, allow_redirects=False)
        for cookie in self.s.cookies.jar:
            if cookie.name == "WorkosCursorSessionToken":
                return urllib.parse.unquote(cookie.value)
        self.s.get(url, headers={"user-agent": UA}, allow_redirects=True)
        for cookie in self.s.cookies.jar:
            if cookie.name == "WorkosCursorSessionToken":
                return urllib.parse.unquote(cookie.value)
        return ""

# CursorBrowserRegister 统一从 browser_register.py 导入，避免代码重复
from platforms.cursor.browser_register import CursorBrowserRegister  # noqa: F401
