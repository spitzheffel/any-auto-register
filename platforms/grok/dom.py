"""Grok (accounts.x.ai) 注册页的选择器与页面动作。

从旧的整段式 ``browser_register.py`` 里抽出来：这里每个函数只对页面做一件事，
不含流程编排（在 steps.py），也不含浏览器生命周期（引擎管）。

站点没有稳定的 data-testid，选择器只能靠 name / type / placeholder 三件套兜，
所以每一组都是候选列表而不是一个逗号串——``drift.py`` 盯的也是这几组。
逗号串交给 Playwright 时命中顺序由 DOM 决定，列表则由我们决定，
验证码那种"先试 name=code 再试 placeholder"的偏好只有列表能表达。
"""
from __future__ import annotations

import random
import string

from core.cancellation import cancellable_sleep
from core.errors import SiteChangedError
from core.registration.dom import (
    click_first_visible,
    fill_first_visible,
    find_first_selector,
    read_cookie,
    wait_for_any_selector,
)

ACCOUNTS_URL = "https://accounts.x.ai"
GROK_APP_URL = "https://grok.com"
SIGNUP_URL = f"{ACCOUNTS_URL}/sign-up"

#: 注册入口偶尔先给一个渠道选择页，有邮箱按钮就先点掉
SIGNUP_WITH_EMAIL_SELECTORS = ('button:has-text("Sign up with email")',)

EMAIL_INPUT_SELECTORS = (
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
)

#: 各步通用的"下一步"按钮
CONTINUE_SELECTORS = (
    'button[type="submit"]',
    'button[data-testid="continue"]',
)

OTP_INPUT_SELECTORS = (
    'input[name="code"]',
    'input[data-input-otp="true"]',
    'input[placeholder*="code"]',
    'input[placeholder*="Code"]',
)

OTP_CONFIRM_SELECTORS = ('button:has-text("Confirm email")',) + CONTINUE_SELECTORS

GIVEN_NAME_SELECTORS = (
    'input[name="given_name"]',
    'input[name="givenName"]',
    'input[placeholder*="First"]',
)

FAMILY_NAME_SELECTORS = (
    'input[name="family_name"]',
    'input[name="familyName"]',
    'input[placeholder*="Last"]',
)

PASSWORD_INPUT_SELECTORS = (
    'input[name="password"]',
    'input[type="password"]',
)

SIGNUP_SUBMIT_SELECTORS = (
    'button:has-text("Complete sign up")',
    'button:has-text("Sign up")',
) + CONTINUE_SELECTORS

#: 验证码之后的一页可能只要姓名、只要密码，也可能两样一起给
NAME_OR_PASSWORD_SELECTORS = GIVEN_NAME_SELECTORS + PASSWORD_INPUT_SELECTORS

ERROR_TEXT_SELECTORS = ('[role="alert"]', '.error')

SESSION_COOKIE = "sso"
SESSION_RW_COOKIE = "sso-rw"

#: 页面是 React 渲染的，提交后要给它一点时间换页再去查下一组选择器
SETTLE_SECONDS = 3.0
#: 注册入口本身的渲染窗口
EMAIL_STAGE_TIMEOUT = 15.0


def random_person_name() -> tuple[str, str]:
    """站点只校验非空，随机 5 个小写字母首字母大写即可（同旧实现）。"""
    def one() -> str:
        return "".join(random.choices(string.ascii_lowercase, k=5)).capitalize()

    return one(), one()


def extract_error_text(page) -> str:
    """把页面上的报错文案捞出来，给失败信息用。"""
    for selector in ERROR_TEXT_SELECTORS:
        try:
            element = page.query_selector(selector)
            if element:
                text = (element.inner_text() or "").strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


def open_signup(page, *, log=print, should_stop=None) -> None:
    log("打开 Grok 注册页")
    page.goto(SIGNUP_URL, wait_until="networkidle", timeout=30000)
    cancellable_sleep(2, should_stop=should_stop)

    if click_first_visible(page, SIGNUP_WITH_EMAIL_SELECTORS):
        cancellable_sleep(2, should_stop=should_stop)


def submit_email(page, email: str, *, log=print, should_stop=None, timeout: float = EMAIL_STAGE_TIMEOUT) -> None:
    if not wait_for_any_selector(page, EMAIL_INPUT_SELECTORS, timeout=timeout, should_stop=should_stop):
        raise SiteChangedError(f"未找到 Grok 注册页邮箱输入框: {page.url}")
    if fill_first_visible(page, EMAIL_INPUT_SELECTORS, email) is None:
        raise SiteChangedError(f"Grok 邮箱输入框不可见: {page.url}")

    if click_first_visible(page, CONTINUE_SELECTORS) is None:
        # 旧实现在按钮缺失时也继续（有些投放是回车提交），保持一致，只是留个痕
        log("未找到提交按钮，依赖表单自身提交")
    cancellable_sleep(SETTLE_SECONDS, should_stop=should_stop)


def wait_for_otp_input(page, *, timeout: float = 20, should_stop=None) -> str | None:
    return wait_for_any_selector(page, OTP_INPUT_SELECTORS, timeout=timeout, should_stop=should_stop)


def fill_otp_code(page, code: str) -> None:
    """填验证码。

    邮件里是 ``ABC-123`` 这种带连字符的，输入框只收 6 个字符。分格式的 OTP
    控件（``data-input-otp``）常把真实 input 藏在覆盖层下面，所以 ``force=True``
    绕过可操作性检查；再不行就逐字符敲。
    """
    selector = find_first_selector(page, OTP_INPUT_SELECTORS)
    if selector is None:
        raise SiteChangedError(f"未找到 Grok 验证码输入框: {page.url}")

    clean_code = code.replace("-", "")
    try:
        page.fill(selector, clean_code, force=True)
    except Exception:
        page.locator(selector).press_sequentially(clean_code)


def confirm_otp(page, *, should_stop=None) -> None:
    for selector in OTP_CONFIRM_SELECTORS:
        try:
            if page.query_selector(selector):
                page.click(selector, force=True)
                break
        except Exception:
            continue
    cancellable_sleep(SETTLE_SECONDS, should_stop=should_stop)


def wait_for_name_or_password(page, *, timeout: float = 15, should_stop=None) -> str | None:
    return wait_for_any_selector(
        page, NAME_OR_PASSWORD_SELECTORS, timeout=timeout, interval=1.0, should_stop=should_stop
    )


def has_name_input(page) -> bool:
    return find_first_selector(page, GIVEN_NAME_SELECTORS) is not None


def has_password_input(page) -> bool:
    return find_first_selector(page, PASSWORD_INPUT_SELECTORS) is not None


def fill_name(page, given: str, family: str) -> None:
    fill_first_visible(page, GIVEN_NAME_SELECTORS, given)
    fill_first_visible(page, FAMILY_NAME_SELECTORS, family)


def fill_password(page, password: str) -> None:
    if fill_first_visible(page, PASSWORD_INPUT_SELECTORS, password) is None:
        raise SiteChangedError(f"Grok 密码输入框不可见: {page.url}")


def accept_checkboxes(page) -> int:
    """勾掉页面上所有复选框（服务条款之类），逐个吞异常。"""
    try:
        boxes = page.query_selector_all('input[type="checkbox"]')
    except Exception:
        return 0
    clicked = 0
    for box in boxes:
        try:
            box.click(force=True)
            clicked += 1
        except Exception:
            continue
    return clicked


def submit_signup(page, *, log=print, should_stop=None) -> None:
    if click_first_visible(page, SIGNUP_SUBMIT_SELECTORS) is None:
        log("未找到注册提交按钮，依赖表单自身提交")
    cancellable_sleep(SETTLE_SECONDS, should_stop=should_stop)


def read_session_cookies(page) -> dict:
    return {
        SESSION_COOKIE: read_cookie(page, SESSION_COOKIE),
        SESSION_RW_COOKIE: read_cookie(page, SESSION_RW_COOKIE),
    }
