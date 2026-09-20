"""Kiro（AWS Builder ID）注册页的选择器与页面动作。

从旧的整段式 ``browser_register.py`` 里抽出来：每个函数只对页面做一件事，
不含流程编排（在 steps.py），也不含浏览器生命周期（引擎管）。

注册要穿三个域：``app.kiro.dev`` 起步 → ``*.signin.aws`` 认账号 →
``profile.aws.amazon.com`` 的注册 SPA（步骤写在 URL hash 里）→ 再跳回 kiro.dev
拿 Cognito token。AWS 的按钮文案会随投放换（Continue / Next / Verify /
Create account），所以"点下一步"是按可见文本找，不是选择器。
"""
from __future__ import annotations

import random
import string

from core.cancellation import cancellable_sleep, poll_until
from core.registration.dom import find_first_visible

KIRO_URL = "https://app.kiro.dev"
KIRO_SIGNIN_URL = f"{KIRO_URL}/signin"
KIRO_DOMAIN = "kiro.dev"
AWS_SIGNIN_DOMAIN = "signin.aws"
AWS_PROFILE_DOMAIN = "profile.aws.amazon.com"

EMAIL_INPUT_SELECTORS = (
    'input[placeholder*="username@example.com"]',
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
)

NAME_INPUT_SELECTORS = (
    'input[placeholder*="Maria"]',
    'input[placeholder*="name" i]',
    'input[name="name"]',
    'input[name="fullName"]',
)

OTP_INPUT_SELECTORS = (
    'input[placeholder*="6"]',
    'input[name="otp"]',
    'input[name="code"]',
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[maxlength="6"]',
)

PASSWORD_INPUT_SELECTORS = (
    'input[type="password"]',
    'input[name="password"]',
)

SUBMIT_BUTTON_SELECTOR = 'button[type="submit"]:not([disabled])'

#: "下一步"按钮的文案候选，顺序即优先级
SUBMIT_TEXTS = ("Continue", "Next", "Verify", "Create account", "Sign in", "Submit")
#: JS 兜底只认前四个——"Sign in" / "Submit" 在页面上重名的概率太高
SUBMIT_TEXTS_STRICT = SUBMIT_TEXTS[:4]

BUILDER_ID_TEXTS = ("Builder ID", "AWS Builder ID")

#: profile.aws 注册 SPA 的步骤标记，就写在 URL hash 里
STEP_ENTER_EMAIL = "enter-email"
STEP_ENTER_NAME = "enter-name"
STEP_VERIFY_EMAIL = "verify-email"
STEP_CREATE_PASSWORD = "create-password"

#: SPA 渲染 / 提交后的缓冲
SETTLE_SECONDS = 2.0
RENDER_SECONDS = 1.5

#: 遍历文本节点点击精确匹配的那一个；文本作为参数传进去，不拼进 JS
_CLICK_TEXT_JS = """
(wanted) => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    let node;
    while (node = walker.nextNode()) {
        if (wanted.includes(node.textContent.trim())) {
            const el = node.parentElement;
            if (el) { el.click(); return true; }
        }
    }
    return false;
}
"""

#: 姓名框既没有稳定的 name 也没有稳定的 placeholder 时的兜底：按容器类名猜
_FILL_NAME_JS = """
(value) => {
    const inputs = document.querySelectorAll('input[type="text"], input:not([type])');
    for (const inp of inputs) {
        const ph = inp.placeholder || '';
        if (ph.includes('Maria') || inp.closest('[class*="name"]') || inp.closest('[class*="Name"]')) {
            inp.focus();
            inp.value = value;
            inp.dispatchEvent(new Event('input', {bubbles: true}));
            inp.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }
    }
    return false;
}
"""

_READ_LOCAL_STORAGE_JS = """
() => {
    const out = {};
    for (const k of Object.keys(localStorage)) {
        out[k] = localStorage.getItem(k);
    }
    return out;
}
"""


def random_name() -> str:
    def one() -> str:
        return "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 7))).capitalize()

    return f"{one()} {one()}"


def random_password() -> str:
    """按 AWS 的密码策略造一个：2 大写 + 3 数字 + 5 小写 + 符号。"""
    return (
        "".join(random.choices(string.ascii_uppercase, k=2))
        + "".join(random.choices(string.digits, k=3))
        + "".join(random.choices(string.ascii_lowercase, k=5))
        + "!"
    )


# ---------------------------------------------------------------------------
# 位置判定
# ---------------------------------------------------------------------------

def page_url(page) -> str:
    return str(getattr(page, "url", "") or "")


def url_hash(page) -> str:
    url = page_url(page)
    return url.split("#")[-1] if "#" in url else ""


def current_step(page) -> str:
    """返回 SPA 当前步骤（hash 的最后一段），不在 SPA 上时是空串。"""
    if AWS_PROFILE_DOMAIN not in page_url(page):
        return ""
    return url_hash(page).split("/")[-1]


def is_on_kiro(page) -> bool:
    url = page_url(page)
    return KIRO_DOMAIN in url and AWS_PROFILE_DOMAIN not in url and AWS_SIGNIN_DOMAIN not in url


def is_on_aws_signin(page) -> bool:
    return AWS_SIGNIN_DOMAIN in page_url(page)


def is_on_aws_profile(page) -> bool:
    return AWS_PROFILE_DOMAIN in page_url(page)


# ---------------------------------------------------------------------------
# 点击与填写
# ---------------------------------------------------------------------------

def js_click_by_text(page, texts) -> bool:
    wanted = [texts] if isinstance(texts, str) else list(texts)
    try:
        return bool(page.evaluate(_CLICK_TEXT_JS, wanted))
    except Exception:
        return False


def click_text(page, texts, *, log=print, timeout: float = 15, should_stop=None) -> bool:
    """按可见文本点击，Playwright locator 优先、JS 文本遍历兜底。"""
    wanted = (texts,) if isinstance(texts, str) else tuple(texts)

    def attempt():
        for text in wanted:
            try:
                element = page.locator(f'text="{text}"').last
                if element.is_visible():
                    element.click()
                    log(f"点击了 {text}")
                    return True
            except Exception:
                continue
        if js_click_by_text(page, wanted):
            log(f"点击了 {wanted[0]} (JS)")
            return True
        return None

    return bool(poll_until(attempt, timeout=timeout, interval=0.5, should_stop=should_stop))


def click_submit(page, *, log=print, timeout: float = 8, should_stop=None) -> bool:
    """点"下一步"：文本优先 → ``button[type=submit]`` → JS 文本遍历。"""

    def attempt():
        for text in SUBMIT_TEXTS:
            try:
                element = page.locator(f'text="{text}"').last
                if element.is_visible():
                    element.click()
                    return True
            except Exception:
                continue
        try:
            element = page.query_selector(SUBMIT_BUTTON_SELECTOR)
            if element and element.is_visible():
                element.click()
                return True
        except Exception:
            pass
        if js_click_by_text(page, SUBMIT_TEXTS_STRICT):
            return True
        return None

    clicked = bool(poll_until(attempt, timeout=timeout, interval=0.5, should_stop=should_stop))
    if clicked:
        log("已点击下一步")
    return clicked


def wait_for_visible_input(page, selectors, *, timeout: float, interval: float = 0.5, should_stop=None):
    return poll_until(
        lambda: find_first_visible(page, selectors)[0],
        timeout=timeout,
        interval=interval,
        should_stop=should_stop,
    )


def fill_input_wait(page, selectors, value: str, *, timeout: float = 20, should_stop=None) -> bool:
    element = wait_for_visible_input(page, selectors, timeout=timeout, should_stop=should_stop)
    if element is None:
        return False
    element.click()
    element.fill(value)
    return True


def fill_name(page, name: str, *, log=print, timeout: float = 15, should_stop=None) -> bool:
    """填姓名，并**确认真的填进去了**。

    AWS 的姓名框是受控组件，``fill`` 偶尔只改 DOM 不进 state，所以回读
    ``input_value()``；两条路都不成再退回 JS 直接派发 input/change 事件。
    """

    def attempt():
        element, _ = find_first_visible(page, NAME_INPUT_SELECTORS)
        if element is None:
            return None
        try:
            element.click()
            element.fill(name)
            if element.input_value():
                return True
        except Exception:
            return None
        return None

    if poll_until(attempt, timeout=timeout, interval=0.5, should_stop=should_stop):
        log(f"填写姓名: {name}")
        return True

    log("未能填写姓名，尝试 JS 方式")
    try:
        return bool(page.evaluate(_FILL_NAME_JS, name))
    except Exception:
        return False


def fill_all_password_fields(page, password: str, *, should_stop=None) -> int:
    """密码页常有"密码 + 确认密码"两个框，两个都填。"""
    fields = []
    for selector in PASSWORD_INPUT_SELECTORS:
        try:
            fields.extend([el for el in page.query_selector_all(selector) if el.is_visible()])
        except Exception:
            continue
    filled = 0
    for field in fields:
        try:
            field.click()
            field.fill(password)
            filled += 1
        except Exception:
            continue
    if filled:
        cancellable_sleep(0.5, should_stop=should_stop)
    return filled


def type_digits(page, code: str, *, should_stop=None) -> None:
    """OTP 是分格控件：必须逐位敲键盘，整串 fill 只会进第一格。"""
    for digit in str(code).strip():
        page.keyboard.press(digit)
        cancellable_sleep(0.1, should_stop=should_stop)


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def read_cognito_tokens(page) -> dict:
    """从 localStorage 里按键名模糊匹配 Cognito 的三个 token。

    键名带随机的 client id 前缀，匹配不到就返回空 dict（调用方负责轮询）。
    """
    try:
        stored = page.evaluate(_READ_LOCAL_STORAGE_JS) or {}
    except Exception:
        return {}

    access = refresh = id_token = ""
    for key, value in stored.items():
        lowered = key.lower()
        if "accesstoken" in lowered and not access:
            access = value
        if "refreshtoken" in lowered and not refresh:
            refresh = value
        if "idtoken" in lowered and not id_token:
            id_token = value
    if not access:
        return {}
    return {"accessToken": access, "refreshToken": refresh or "", "idToken": id_token or ""}


def wait_for_cognito_tokens(page, *, timeout: float = 20, should_stop=None) -> dict:
    return poll_until(
        lambda: read_cognito_tokens(page) or None,
        timeout=timeout,
        interval=2.0,
        should_stop=should_stop,
    ) or {}
