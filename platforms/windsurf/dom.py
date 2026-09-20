"""Windsurf 注册页的选择器、页面动作与页内 API。

从 ``browser_register.py`` 里抽出来的**只有注册那一段**：那个文件还装着 Stripe /
支付宝的结账流程（``WindsurfStripeCheckoutBrowser`` 一家），和注册没关系，原样留着。

Windsurf 的注册页是 React 表单，但"建账号"那一下的结果不在 DOM 里——
session 是从 ``WindsurfPostAuth`` 的 protobuf 响应里捕获的，所以流程必须在点
"Create account" 之前挂好 response 监听。
"""
from __future__ import annotations

import json
import re

from core.cancellation import poll_until
from core.registration.dom import MOUSE_EVENT_PATCH
from platforms.windsurf.core import (
    SEAT_SERVICE,
    WINDSURF_BASE,
    _field_string,
    _field_varint,
    parse_current_user_response,
    parse_plan_status_response,
    parse_post_auth_response,
    parse_stripe_subscription_state,
    summarize_account_state,
)

__all__ = [
    "MOUSE_EVENT_PATCH",
    "WINDSURF_BASE",
    "LOGIN_URL",
    "REGISTER_URL_PATTERN",
    "SIGN_UP_LINK_PATTERN",
    "CREATE_ACCOUNT_BUTTON_PATTERN",
    "GIVEN_NAME_SELECTOR",
    "FAMILY_NAME_SELECTOR",
    "EMAIL_SELECTOR",
    "TOS_CHECKBOX_SELECTOR",
    "SUBMIT_BUTTON_SELECTOR",
    "PASSWORD_SELECTOR",
    "CONFIRM_PASSWORD_SELECTOR",
    "OTP_INPUT_SELECTOR",
    "OTP_ANY_INPUT_SELECTOR",
    "EMAIL_COMPLETE_PATH",
    "POST_AUTH_PATH",
    "PAGE_TIMEOUT_MS",
    "default_name",
    "split_name",
    "extract_code",
    "fill_otp",
    "capture_auth_responses",
    "WindsurfBrowserApi",
]

LOGIN_URL = f"{WINDSURF_BASE}/account/login"
REGISTER_URL_PATTERN = re.compile(r"/account/register")
SIGN_UP_LINK_PATTERN = re.compile(r"Sign up", re.I)
CREATE_ACCOUNT_BUTTON_PATTERN = re.compile(r"Create account", re.I)

GIVEN_NAME_SELECTOR = 'input[autocomplete="given-name"]'
FAMILY_NAME_SELECTOR = 'input[autocomplete="family-name"]'
EMAIL_SELECTOR = 'input[type="email"]'
TOS_CHECKBOX_SELECTOR = "#auth1-agree-tos"
SUBMIT_BUTTON_SELECTOR = 'button[type="submit"]'
PASSWORD_SELECTOR = 'input[name="password"]'
CONFIRM_PASSWORD_SELECTOR = 'input[name="confirmPassword"]'
OTP_INPUT_SELECTOR = 'input[autocomplete="one-time-code"]'
#: 分格 OTP 控件：后面几格的 autocomplete 是 off
OTP_ANY_INPUT_SELECTOR = 'input[autocomplete="one-time-code"], input[autocomplete="off"]'

#: 注册结果只在这两个响应里，DOM 上看不到
EMAIL_COMPLETE_PATH = "/_devin-auth/email/complete"
POST_AUTH_PATH = "SeatManagementService/WindsurfPostAuth"

#: Windsurf 的页面动作普遍慢，旧实现统一设了 90 秒
PAGE_TIMEOUT_MS = 90000

CODE_PATTERN = re.compile(r"\b(\d{6})\b")


def default_name(email: str) -> str:
    """从邮箱本地部分造一个像样的姓名（注册表单要求非空）。"""
    local = (email or "").split("@", 1)[0].strip()
    cleaned = "".join(ch if ch.isalpha() else " " for ch in local).strip()
    if cleaned:
        return " ".join(part.capitalize() for part in cleaned.split()[:2])
    return "Windsurf User"


def split_name(name: str) -> tuple[str, str]:
    """拆成 first / last，并去掉字母以外的字符。

    Windsurf 的姓名框拒收数字与符号，而邮箱本地部分常常带一串数字，
    直接填会被前端挡下来且不给提示。
    """
    parts = [part for part in re.split(r"\s+", str(name or "").strip()) if part]
    sanitized: list[str] = []
    for part in parts:
        cleaned = re.sub(r"[^A-Za-z]+", "", part)
        if cleaned:
            sanitized.append(cleaned.capitalize())
    if not sanitized:
        return "Windsurf", "User"
    if len(sanitized) == 1:
        token = sanitized[0]
        if len(token) >= 8:
            return token[: min(8, len(token))], token[min(8, len(token)) :] or "User"
        return token, "User"
    return sanitized[0], " ".join(sanitized[1:])


def extract_code(raw: str) -> str:
    """从整封邮件正文里抠出 6 位验证码；抠不到返回空串，由调用方定性。"""
    match = CODE_PATTERN.search(str(raw or ""))
    return match.group(1) if match else ""


def fill_otp(page, code: str) -> None:
    """分格控件逐格填；只有一个框时整串填进去。"""
    inputs = [item for item in page.locator(OTP_ANY_INPUT_SELECTOR).all() if item.is_visible()]
    if len(inputs) >= len(code):
        for item, char in zip(inputs, code):
            item.fill(char)
        return
    page.locator(OTP_INPUT_SELECTOR).fill(code)


def capture_auth_responses(page) -> dict:
    """挂 response 监听，把注册结果收进一个 dict。

    必须在点 "Create account" **之前**挂：session token 只在那一下的响应里出现，
    页面上任何地方都读不到。同一个 page 只能挂一次，调用方（步骤）用 handles 保证幂等。

    返回的 dict 有两个键：``complete``（邮箱注册完成的 JSON）和 ``auth``
    （WindsurfPostAuth 解出来的 session 上下文）。
    """
    captured: dict = {"complete": {}, "auth": {}}

    def on_response(response):
        try:
            if EMAIL_COMPLETE_PATH in response.url and response.status == 200:
                data = response.json()
                if isinstance(data, dict):
                    captured["complete"] = data
            elif POST_AUTH_PATH in response.url and response.status == 200:
                parsed = parse_post_auth_response(response.body())
                if parsed.get("session_token"):
                    captured["auth"] = parsed
        except Exception:
            pass

    page.on("response", on_response)
    return captured


def wait_for_session(captured: dict, *, timeout: float, should_stop=None) -> dict:
    return poll_until(
        lambda: captured.get("auth") if (captured.get("auth") or {}).get("session_token") else None,
        timeout=timeout,
        interval=1.0,
        should_stop=should_stop,
    ) or {}


def visible_body_text(page, limit: int = 500) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)[:limit]
    except Exception:
        return ""


def _headers(*, content_type: str, referer: str, account_id: str = "", org_id: str = "") -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "content-type": content_type,
        "origin": WINDSURF_BASE,
        "referer": f"{WINDSURF_BASE}{referer}",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }
    if content_type == "application/proto":
        headers["connect-protocol-version"] = "1"
    if account_id:
        headers["x-devin-account-id"] = account_id
    if org_id:
        headers["x-devin-primary-org-id"] = org_id
    return headers


class WindsurfBrowserApi:
    """用页面自己的会话打 Windsurf 的接口。

    页面驱动的注册只需要两件事：session 没被 response 监听捕获到时补一次兑换
    （``post_auth``），以及注册完读一遍账号状态（``load_account_state``）。
    旧实现里还挂着 ``fetch_connections`` / ``check_user_login_method`` /
    ``start_email_signup`` / ``complete_email_signup`` / ``subscribe_to_plan``
    五个方法——浏览器路径走的是 UI，一个都没调过（协议路径用的是
    ``platforms/windsurf/core.py`` 里的同名实现），搬运时一并删掉。
    """

    def __init__(self, page, log_fn=print):
        self.page = page
        self.log = log_fn

    def _json_post(self, path: str, payload: dict, *, referer: str = "/account/register") -> dict:
        response = self.page.request.post(
            f"{WINDSURF_BASE}{path}",
            headers=_headers(content_type="application/json", referer=referer),
            data=json.dumps(payload),
        )
        if response.status >= 400:
            raise RuntimeError(f"{path} 失败: HTTP {response.status} {response.text()[:200]}")
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"{path} 返回格式异常")
        return data

    def _proto_post(
        self,
        method: str,
        body: bytes,
        *,
        account_id: str = "",
        org_id: str = "",
        referer: str = "/profile",
    ) -> bytes:
        response = self.page.request.post(
            f"{WINDSURF_BASE}{SEAT_SERVICE}/{method}",
            headers=_headers(
                content_type="application/proto",
                referer=referer,
                account_id=account_id,
                org_id=org_id,
            ),
            data=body,
        )
        if response.status >= 400:
            raise RuntimeError(f"{method} 失败: HTTP {response.status} {response.text()[:200]}")
        return response.body()

    def post_auth(self, auth_token: str) -> dict[str, str]:
        self.log("兑换 Windsurf session")
        content = self._proto_post("WindsurfPostAuth", _field_string(1, auth_token), referer="/account/register")
        data = parse_post_auth_response(content)
        if not data.get("session_token"):
            raise RuntimeError("Windsurf 未返回 session_token")
        return data

    def load_account_state(self, *, session_token: str, account_id: str, org_id: str, fallback_email: str) -> dict:
        auth_body = _field_string(1, session_token)
        current_user = parse_current_user_response(
            self._proto_post("GetCurrentUser", auth_body, account_id=account_id, org_id=org_id)
        )
        plan_status = parse_plan_status_response(
            self._proto_post(
                "GetPlanStatus",
                auth_body + _field_varint(2, 1),
                account_id=account_id,
                org_id=org_id,
                referer="/subscription/usage",
            )
        )
        try:
            stripe_state = parse_stripe_subscription_state(
                self._proto_post(
                    "GetStripeSubscriptionState",
                    auth_body,
                    account_id=account_id,
                    org_id=org_id,
                    referer="/subscription/manage-plan",
                )
            )
        except Exception as exc:
            # 订阅状态读不到不影响账号可用性，记下来继续
            stripe_state = {"error": str(exc)}

        state = {
            "current_user": current_user,
            "plan_status": plan_status,
            "stripe_subscription": stripe_state,
        }
        state["summary"] = summarize_account_state(state, fallback_email=fallback_email)
        return state
