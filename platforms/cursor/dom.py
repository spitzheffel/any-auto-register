"""Cursor 注册页的站点特有常量与 DOM 辅助。

通用实现（Turnstile / CF 全页拦截 / 选择器轮询 / cookie 等待）已上收到
``core/registration/dom.py``；这里只留 Cursor 自己的选择器、cookie 名与 sitekey，
以及 steps.py / obstacles.py 依赖的那几个下划线名字。
"""
from __future__ import annotations

from core.registration.dom import (
    click_first_visible,
    click_turnstile_in_iframe,
    get_turnstile_sitekey,
    inject_turnstile,
    is_cf_full_block,
    is_turnstile_modal_visible,
    read_cookie,
    wait_cf_full_block_clear,
    wait_for_cookie,
)

AUTH = "https://authenticator.cursor.sh"
TURNSTILE_SITEKEY = "0x4AAAAAAAMNIvC45A4Wjjln"

#: 会话 token 所在的 cookie
SESSION_COOKIE = "WorkosCursorSessionToken"

#: 页面上还有这些输入框 = 只是内嵌 widget，不是 CF 全页拦截
FORM_SELECTOR = (
    'input[name="email"], input[name="firstName"], input[name="otp"], input[name="code"]'
)

CONTINUE_SELECTORS = (
    'button[data-action-button-primary="true"]',
    'button[type="submit"]:not([aria-hidden="true"])',
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button:has-text("Sign up")',
    'button[type="submit"]',
    "button",
)


def _click_continue(page) -> bool:
    """尝试点击 Continue/Next/Sign up 按钮；返回 False 时调用方用 Enter 兜底。"""
    return click_first_visible(page, CONTINUE_SELECTORS) is not None


def _get_token_from_cookies(page) -> str:
    return read_cookie(page, SESSION_COOKIE, unquote_value=True)


def _wait_for_token(page, timeout: int = 120, *, should_stop=None) -> str:
    return wait_for_cookie(page, SESSION_COOKIE, timeout=timeout, should_stop=should_stop, unquote_value=True)


def _is_cf_full_block(page) -> bool:
    return is_cf_full_block(page, form_selector=FORM_SELECTOR)


def _wait_cf_full_block_clear(page, timeout: int = 120, log_fn=print, *, should_stop=None) -> None:
    wait_cf_full_block_clear(page, timeout, log_fn, is_blocked=_is_cf_full_block, should_stop=should_stop)


def _get_turnstile_sitekey(page) -> str:
    return get_turnstile_sitekey(page, default=TURNSTILE_SITEKEY)


def _inject_turnstile(page, token: str) -> bool:
    return inject_turnstile(page, token)


def _is_turnstile_modal_visible(page) -> bool:
    return is_turnstile_modal_visible(page)


def _click_turnstile_in_iframe(page, log_fn=print) -> bool:
    return click_turnstile_in_iframe(page, log_fn)
