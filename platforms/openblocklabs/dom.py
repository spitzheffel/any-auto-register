"""OpenBlockLabs（WorkOS AuthKit）注册页的选择器与页面动作。

从旧的整段式 ``browser_register.py`` 里抽出来。搬移时最大的收获是**删重复**：
Cloudflare 全页拦截、Turnstile iframe 点击、鼠标事件补丁那 150 行，
和 ``platforms/cursor/`` 里那份逐字相同，现在统一走 ``core/registration/dom.py``，
这里只留站点特有的选择器和 WorkOS 那套会话初始化。

WorkOS 的注册入口要先访问 ``auth.openblocklabs.com/?client_id=...`` 拿一个
``authorization_session_id``，再带着它进 ``/sign-up``；直接 goto /sign-up 会
拿到 Not Found。
"""
from __future__ import annotations

import random
import string
from urllib.parse import parse_qs, quote, urlparse

from core.cancellation import cancellable_sleep, poll_until
from core.errors import SiteChangedError
from core.registration.dom import (
    MOUSE_EVENT_PATCH,
    click_turnstile_in_iframe,
    is_cf_full_block,
    is_turnstile_modal_visible,
    wait_cf_full_block_clear,
    wait_for_cookie,
)

__all__ = [
    "MOUSE_EVENT_PATCH",
    "AUTH_BASE",
    "DASHBOARD",
    "DASHBOARD_HOST",
    "CLIENT_ID",
    "REDIRECT_URI",
    "ENTRY_URL",
    "SESSION_COOKIE",
    "EMAIL_INPUT_SELECTORS",
    "PASSWORD_INPUT_SELECTORS",
    "SUBMIT_BUTTON_SELECTORS",
    "CONTINUE_SELECTORS",
    "OTP_INPUT_SELECTORS",
    "FIRST_NAME_SELECTOR",
    "LAST_NAME_SELECTOR",
    "FORM_SELECTOR",
    "NOT_FOUND_MARKERS",
    "signup_url",
    "random_name",
    "random_password",
    "find_visible_element",
    "wait_for_visible_element",
    "read_input_value",
    "fill_visible_input",
    "click_visible_button",
    "click_continue",
    "extract_authorization_session_id",
    "wait_for_signup_session",
    "is_email_verification_ready",
    "is_not_found_page",
    "fill_otp_by_keyboard",
    "wait_for_wos_session",
    "is_cf_full_block_here",
    "wait_cf_full_block_clear_here",
    "is_turnstile_modal_visible",
    "click_turnstile_in_iframe",
]

AUTH_BASE = "https://auth.openblocklabs.com"
DASHBOARD = "https://dashboard.openblocklabs.com"
DASHBOARD_HOST = DASHBOARD.split("://", 1)[1]
CLIENT_ID = "client_01K8YDZSSKDMK8GYTEHBAW4N4S"
REDIRECT_URI = quote(f"{DASHBOARD}/auth/callback", safe="")
ENTRY_URL = f"{AUTH_BASE}/?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}"

SESSION_COOKIE = "wos-session"

EMAIL_INPUT_SELECTORS = (
    'input[name="email"]',
    'input[type="email"]',
    'input[autocomplete="email"]',
    'input[placeholder*="email" i]',
)

PASSWORD_INPUT_SELECTORS = (
    'input[name="password"]',
    'input[type="password"]',
    'input[autocomplete="new-password"]',
    'input[placeholder*="password" i]',
)

SUBMIT_BUTTON_SELECTORS = (
    'button[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("Sign up")',
)

#: 比 SUBMIT_BUTTON_SELECTORS 更宽松的兜底，最后一档是"页面上任何按钮"
CONTINUE_SELECTORS = (
    'button[type="submit"]:not([aria-hidden="true"])',
    'button:has-text("Continue")',
    'button:has-text("Sign up")',
    'button:has-text("Next")',
    'button[type="submit"]',
    "button",
)

OTP_INPUT_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[data-test="otp-input"]',
    'input[data-index="0"]',
)

FIRST_NAME_SELECTOR = 'input[name="first_name"], input[placeholder*="First"]'
LAST_NAME_SELECTOR = 'input[name="last_name"], input[placeholder*="Last"]'

#: 页面上还有这些输入框 = 正常页面，不是 Cloudflare 全页拦截
FORM_SELECTOR = 'input[name="email"], input[type="email"], input[type="password"]'

#: WorkOS 会话没初始化好时中间页就这两句话
NOT_FOUND_MARKERS = ("the requested resource was not found", "not found")

EMAIL_VERIFICATION_URL_MARKER = "email-verification"
PASSWORD_URL_MARKER = "/sign-up/password"


def signup_url(session_id: str) -> str:
    return f"{AUTH_BASE}/sign-up?redirect_uri={REDIRECT_URI}&authorization_session_id={session_id}"


def random_name() -> str:
    return "".join(random.choices(string.ascii_lowercase, k=5)).capitalize()


def random_password() -> str:
    """按 WorkOS 的密码策略造一个：2 大写 + 3 数字 + 5 小写 + 符号。"""
    return (
        "".join(random.choices(string.ascii_uppercase, k=2))
        + "".join(random.choices(string.digits, k=3))
        + "".join(random.choices(string.ascii_lowercase, k=5))
        + "!"
    )


# ---------------------------------------------------------------------------
# Cloudflare（站点特有的表单选择器，其余逻辑在 core/registration/dom.py）
# ---------------------------------------------------------------------------

def is_cf_full_block_here(page) -> bool:
    return is_cf_full_block(page, form_selector=FORM_SELECTOR)


def wait_cf_full_block_clear_here(page, timeout: float = 120, log_fn=print, *, should_stop=None) -> None:
    wait_cf_full_block_clear(page, timeout, log_fn, is_blocked=is_cf_full_block_here, should_stop=should_stop)


# ---------------------------------------------------------------------------
# 元素查找与填写
# ---------------------------------------------------------------------------

def find_visible_element(page, selectors, *, require_enabled: bool = False):
    """返回第一个可见（可选：可用）的元素，附带命中的选择器。

    逐个 ``query_selector_all`` 而不是 ``query_selector``：WorkOS 的表单常把上一步的
    输入框留在 DOM 里并隐藏，只取第一个会拿到那个隐藏的。
    """
    for selector in selectors:
        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue
        for element in elements:
            try:
                if not element.is_visible():
                    continue
                if require_enabled and not element.is_enabled():
                    continue
                return element, selector
            except Exception:
                continue
    return None, ""


def wait_for_visible_element(page, selectors, *, timeout: float = 15, require_enabled: bool = False, should_stop=None):
    """等到有可见元素为止；超时返回 ``(None, "")``。

    poll 的返回值就是那一对，不再查第二遍——页面随时可能在两次查询之间换页。
    """
    def attempt():
        element, used = find_visible_element(page, selectors, require_enabled=require_enabled)
        return (element, used) if element is not None else None

    return poll_until(attempt, timeout=timeout, interval=0.2, should_stop=should_stop) or (None, "")


def read_input_value(element) -> str:
    try:
        return element.input_value()
    except Exception:
        try:
            return element.evaluate("(el) => el.value || ''")
        except Exception:
            return ""


#: 受控组件不认 element.fill：绕到原生 value setter 上再手动派发事件
_FORCE_VALUE_JS = """(el, nextValue) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    if (setter) setter.call(el, nextValue);
    else el.value = nextValue;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    return el.value || '';
}"""


def fill_visible_input(page, selectors, value: str, *, timeout: float = 15, should_stop=None) -> tuple[str, str]:
    """填第一个可见输入框并**回读确认**。

    返回 ``(命中的选择器, "")``；三轮都没填进去时返回 ``("", 当前值)``，
    由调用方决定报什么错。
    """
    element, used = wait_for_visible_element(page, selectors, timeout=timeout, should_stop=should_stop)
    if element is None:
        return "", ""

    try:
        element.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    for _ in range(3):
        for action in (
            lambda: element.click(timeout=3000),
            lambda: element.fill(""),
            lambda: element.type(value, delay=80),
        ):
            try:
                action()
            except Exception:
                continue
        if read_input_value(element) == value:
            return used, ""

        try:
            element.evaluate(_FORCE_VALUE_JS, value)
        except Exception:
            pass
        if read_input_value(element) == value:
            return used, ""

        cancellable_sleep(0.4, should_stop=should_stop)

    return "", read_input_value(element)


def click_visible_button(page, selectors, *, timeout: float = 15, should_stop=None) -> str:
    """点第一个可见且可用的按钮；原生点击不成就走 JS click。返回命中的选择器。"""
    element, used = wait_for_visible_element(
        page, selectors, timeout=timeout, require_enabled=True, should_stop=should_stop
    )
    if element is None:
        return ""

    try:
        element.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    for _ in range(2):
        try:
            element.click(timeout=3000)
            return used
        except Exception:
            pass
        try:
            element.evaluate("(el) => el.click()")
            return used
        except Exception:
            pass
        cancellable_sleep(0.2, should_stop=should_stop)

    return ""


def click_continue(page) -> bool:
    element, _ = find_visible_element(page, CONTINUE_SELECTORS, require_enabled=True)
    if element is None:
        return False
    try:
        element.click(timeout=3000)
        return True
    except Exception:
        return False


def fill_otp_by_keyboard(page, code: str) -> bool:
    """分格 OTP 控件：点住第一个可见的非邮箱/非密码输入框，然后整串敲进去。"""
    try:
        candidates = page.query_selector_all('input[autocomplete="one-time-code"], input:not([type="hidden"])')
    except Exception:
        return False

    for element in candidates:
        try:
            if not element.is_visible():
                continue
            if element.get_attribute("type") in ("email", "password"):
                continue
            element.click()
            break
        except Exception:
            continue

    try:
        page.keyboard.type(code)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 页面状态
# ---------------------------------------------------------------------------

def page_url(page) -> str:
    return str(getattr(page, "url", "") or "")


def body_text(page) -> str:
    try:
        return (page.locator("body").inner_text() or "").strip().lower()
    except Exception:
        return ""


def is_not_found_page(page) -> bool:
    text = body_text(page)
    if not text:
        return False
    return text == "not found" or NOT_FOUND_MARKERS[0] in text


def is_email_verification_ready(page) -> bool:
    if EMAIL_VERIFICATION_URL_MARKER in page_url(page):
        return True
    element, _ = find_visible_element(page, OTP_INPUT_SELECTORS)
    return element is not None


def extract_authorization_session_id(url: str) -> str:
    try:
        parsed = urlparse(url)
        return (parse_qs(parsed.query).get("authorization_session_id") or [""])[0]
    except Exception:
        return ""


def wait_for_signup_session(page, *, timeout: float = 30, should_stop=None) -> str:
    """等 WorkOS 把 ``authorization_session_id`` 给出来（URL 上或隐藏字段里）。

    中途发现页面是 Not Found 就立刻停——那说明会话初始化失败，再等也没有。
    """

    def attempt():
        session_id = extract_authorization_session_id(page_url(page))
        if session_id:
            return session_id
        try:
            hidden = page.query_selector('input[name="authorization_session_id"][value]')
            if hidden and (hidden.get_attribute("value") or "").strip():
                return hidden.get_attribute("value")
        except Exception:
            pass
        if is_not_found_page(page):
            # 会话初始化失败，再等也没有；这是站点侧的结构问题
            raise SiteChangedError(f"WorkOS 会话初始化失败，页面返回 Not Found: {page_url(page)}")
        return None

    return poll_until(attempt, timeout=timeout, interval=0.5, should_stop=should_stop) or ""


def wait_for_wos_session(page, *, timeout: float = 15, should_stop=None) -> str:
    return wait_for_cookie(page, SESSION_COOKIE, timeout=timeout, should_stop=should_stop)
