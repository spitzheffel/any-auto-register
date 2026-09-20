"""Tavily（Auth0 托管页）注册流程的选择器与页面动作。

从旧的整段式 ``browser_register.py`` 里抽出来：绝大多数函数原样搬运——它们本来
就是"对页面做一件事"的粒度，只是被一个 90 行的 ``run()`` 串着。搬移时的改动只有
两类：长等待换成可取消的 ``poll_until`` / ``cancellable_sleep``，
以及注入 token 的 JS 改成传参而不是拼字符串。

Tavily 的登录页是 Auth0 Universal Login，注册入口的 URL 带一次性的 ``state``，
只能从 sign-in 页的 HTML 里抠出来——所以流程的第一步是"读 HTML 找入口"，
不是直接 goto 一个写死的 /signup。
"""
from __future__ import annotations

import re

import requests

from core.cancellation import cancellable_sleep, poll_until
from core.oauth_browser import try_click_provider_on_page

APP_URL = "https://app.tavily.com"
AUTH_URL = "https://auth.tavily.com"
SIGNIN_URL = f"{APP_URL}/sign-in"
SEARCH_API_URL = "https://api.tavily.com/search"

TURNSTILE_SITEKEY = "0x4AAAAAAAQFNSW6xordsuIq"

#: Tavily 阶段性地只对特定组织开放邮箱密码注册；命中这两句就别再重试了
ORG_ONLY_SIGNUP_MARKERS = (
    "email/password signup is only available for specific organizations",
    "please use google, github, linkedin, or microsoft to sign up",
)

EMAIL_INPUT_SELECTORS = ('input[name="email"]', 'input[name="username"]')
OTP_INPUT_SELECTOR = 'input[name="code"]'
PASSWORD_INPUT_SELECTOR = 'input[name="password"]'
OTP_OR_PASSWORD_SELECTOR = f"{OTP_INPUT_SELECTOR}, {PASSWORD_INPUT_SELECTOR}"

PRIMARY_BUTTON_SELECTORS = (
    'button[data-action-button-primary="true"]',
    'button[type="submit"][name="action"][value="default"]:not([aria-hidden="true"])',
    'button[type="submit"]:not([aria-hidden="true"])',
)

FEEDBACK_SELECTORS = (
    '[role="alert"]',
    '[data-error-visible="true"]',
    ".ulp-input-error-message",
    ".auth0-global-message",
    ".cf-turnstile-error",
)

#: 注册提交后可能落到的三种页面之一就算"过了"
POST_SIGNUP_URL_MARKERS = ("app.tavily.com", "/verify", "/continue")

API_KEY_PATTERN = r"tvly-[a-zA-Z0-9_-]{20,}"
API_KEY_PLACEHOLDER = "tvly-YOUR_API_KEY"

#: OAuth 流程也用这个（platforms/tavily/browser_oauth.py）
click_oauth_provider = try_click_provider_on_page


# ---------------------------------------------------------------------------
# 入口与表单
# ---------------------------------------------------------------------------

def str_url(page) -> str:
    return str(getattr(page, "url", "") or "")


def extract_signup_url(html: str) -> str | None:
    """从 sign-in 页的 HTML 里抠注册入口——URL 带一次性 state，写死不了。"""
    match = re.search(r'href="(/u/signup/identifier[^"]*)"', html)
    if not match:
        return None
    return f"{AUTH_URL}{match.group(1)}"


def fill_first_input(page, selectors, value: str) -> str | None:
    for selector in selectors:
        if page.query_selector(selector):
            page.fill(selector, value)
            return selector
    return None


def submit_primary_action(page, input_selector: str | None = None) -> bool:
    """点 Auth0 的主按钮；按钮都点不动时退回在输入框上敲回车。"""
    for selector in PRIMARY_BUTTON_SELECTORS:
        if page.query_selector(selector):
            try:
                page.click(selector, no_wait_after=True, timeout=3000)
                return True
            except Exception:
                continue

    if input_selector and page.query_selector(input_selector):
        try:
            page.press(input_selector, "Enter")
            return True
        except Exception:
            return False

    return False


def close_marketing_dialog(page) -> None:
    close_button = page.query_selector('button[aria-label="Close"]')
    if close_button:
        close_button.click()
        cancellable_sleep(1)


def refill_password(page, password: str) -> bool:
    if not page.query_selector(PASSWORD_INPUT_SELECTOR):
        return False
    page.fill(PASSWORD_INPUT_SELECTOR, password)
    return True


# ---------------------------------------------------------------------------
# 页面反馈
# ---------------------------------------------------------------------------

def normalize_feedback(feedback: str | None) -> str:
    return (feedback or "").replace("’", "'").strip().lower()


def extract_page_feedback(page) -> str:
    messages: list[str] = []
    for selector in FEEDBACK_SELECTORS:
        try:
            nodes = page.query_selector_all(selector)
        except Exception:
            continue
        for node in nodes:
            text = (node.inner_text() or "").strip()
            if text and text not in messages:
                messages.append(text)
    return " | ".join(messages)


def detect_org_only_signup_message(page) -> str | None:
    """Tavily 关掉邮箱密码注册时页面会明说；命中就别再换邮箱白试了。"""
    feedback = extract_page_feedback(page)
    if any(marker in feedback.lower() for marker in ORG_ONLY_SIGNUP_MARKERS):
        return feedback

    try:
        body_text = (page.locator("body").inner_text() or "").strip()
    except Exception:
        body_text = ""
    if any(marker in body_text.lower() for marker in ORG_ONLY_SIGNUP_MARKERS):
        return body_text

    return None


def is_past_signup(page) -> bool:
    current = str_url(page).lower()
    return any(marker in current for marker in POST_SIGNUP_URL_MARKERS)


def needs_email_verification(page) -> bool:
    return "verify" in str_url(page).lower()


def wait_for_post_signup_target(page, *, timeout: float, should_stop=None) -> bool:
    return bool(
        poll_until(
            lambda: is_past_signup(page),
            timeout=timeout,
            interval=0.5,
            should_stop=should_stop,
        )
    )


def wait_for_manual_oauth_completion(page, timeout: int = 300) -> bool:
    """OAuth 流程用：等浏览器落到 app.tavily.com，超时就自己跳一次试试。"""
    if poll_until(
        lambda: APP_URL.split("://", 1)[1] in str(page.url).lower(),
        timeout=timeout,
        interval=1.0,
    ):
        return True
    try:
        page.goto(APP_URL, wait_until="networkidle", timeout=30000)
        return "app.tavily.com" in page.url.lower()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Turnstile
# ---------------------------------------------------------------------------

_SITEKEY_JS = """
() => {
    const node = document.querySelector('[data-captcha-sitekey], .cf-turnstile, [data-sitekey]');
    if (!node) { return ''; }
    return node.getAttribute('data-captcha-sitekey') || node.getAttribute('data-sitekey') || '';
}
"""

_TURNSTILE_STATE_JS = """
() => {
    const passwordInput = document.querySelector('input[name="password"]');
    const widget = document.querySelector('div[data-captcha-sitekey], .cf-turnstile, [data-sitekey]');
    const iframe = document.querySelector(
        'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
    );
    const captchaInput = document.querySelector(
        'input[name="captcha"], input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
    );
    return {
        hasCaptchaDiv: !!widget,
        hasChallengeIframe: !!iframe,
        hasCaptchaInput: !!captchaInput,
        hasTurnstile: typeof window.turnstile !== 'undefined',
        hasPasswordInput: !!passwordInput,
        passwordValueLength: passwordInput ? passwordInput.value.length : 0,
        sitekey: widget
            ? (widget.getAttribute('data-captcha-sitekey') || widget.getAttribute('data-sitekey') || '')
            : '',
    };
}
"""

#: token 作为参数传进去，不拼进脚本字符串——旧实现自己手写了转义
_INJECT_TOKEN_JS = """
(token) => {
    const form = document.querySelector('form') || document.body;
    const names = ['captcha', 'cf-turnstile-response'];

    const ensureField = (name) => {
        let field = document.querySelector(`input[name="${name}"], textarea[name="${name}"]`);
        if (field) { return field; }
        field = document.createElement(name.includes('response') ? 'textarea' : 'input');
        if (field.tagName === 'INPUT') { field.type = 'hidden'; }
        field.name = name;
        form.appendChild(field);
        return field;
    };

    names.forEach((name) => {
        const field = ensureField(name);
        field.value = token;
        field.dispatchEvent(new Event('input', { bubbles: true }));
        field.dispatchEvent(new Event('change', { bubbles: true }));
    });

    if (typeof window._turnstileTokenCallback === 'function') { window._turnstileTokenCallback(token); }
    if (typeof window.turnstileCallback === 'function') { window.turnstileCallback(token); }
    return true;
}
"""


def get_turnstile_sitekey(page) -> str:
    """DOM 属性优先，HTML 正则兜底，都没有就用写死的那个。"""
    try:
        sitekey = page.evaluate(_SITEKEY_JS)
    except Exception:
        sitekey = ""
    if sitekey:
        return str(sitekey).strip()

    try:
        html = page.content()
    except Exception:
        html = ""
    match = re.search(r'(?:data-captcha-sitekey|data-sitekey)=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    return TURNSTILE_SITEKEY


def collect_turnstile_state(page) -> dict:
    try:
        state = page.evaluate(_TURNSTILE_STATE_JS)
    except Exception:
        state = {}
    state = state or {}
    return {
        "hasCaptchaDiv": bool(state.get("hasCaptchaDiv")),
        "hasChallengeIframe": bool(state.get("hasChallengeIframe")),
        "hasCaptchaInput": bool(state.get("hasCaptchaInput")),
        "hasTurnstile": bool(state.get("hasTurnstile")),
        "hasPasswordInput": bool(state.get("hasPasswordInput")),
        "passwordValueLength": int(state.get("passwordValueLength") or 0),
        "sitekey": (state.get("sitekey") or "").strip(),
    }


def has_password_challenge_signal(feedback: str | None = None, state: dict | None = None) -> bool:
    lowered = normalize_feedback(feedback)
    if any(
        keyword in lowered
        for keyword in (
            "security challenge",
            "captcha",
            "turnstile",
            "cloudflare",
            "couldn't load the security challenge",
        )
    ):
        return True

    state = state or {}
    return any(
        (
            state.get("hasCaptchaDiv"),
            state.get("hasChallengeIframe"),
            state.get("hasCaptchaInput"),
            state.get("hasTurnstile"),
        )
    )


def format_turnstile_state(state: dict) -> str:
    return (
        f"captchaDiv={'Y' if state.get('hasCaptchaDiv') else 'N'}, "
        f"iframe={'Y' if state.get('hasChallengeIframe') else 'N'}, "
        f"input={'Y' if state.get('hasCaptchaInput') else 'N'}, "
        f"turnstile={'Y' if state.get('hasTurnstile') else 'N'}, "
        f"pwdLen={state.get('passwordValueLength', 0)}"
    )


def refresh_password_page_if_needed(page, feedback: str | None, state: dict) -> bool:
    """挑战组件压根没加载出来时，刷新一次比再点一次提交管用。"""
    if "couldn't load the security challenge" not in normalize_feedback(feedback):
        return False
    if any((state.get("hasCaptchaDiv"), state.get("hasChallengeIframe"), state.get("hasTurnstile"))):
        return False

    try:
        page.reload(wait_until="networkidle", timeout=30000)
        page.wait_for_selector(PASSWORD_INPUT_SELECTOR, timeout=15000)
        cancellable_sleep(2)
        return True
    except Exception:
        return False


def inject_turnstile_token(page, token: str) -> bool:
    try:
        return bool(page.evaluate(_INJECT_TOKEN_JS, token))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# API Key
# ---------------------------------------------------------------------------

def extract_api_key(page) -> str | None:
    try:
        html = page.content()
    except Exception:
        return None
    keys = [key for key in re.findall(API_KEY_PATTERN, html) if key != API_KEY_PLACEHOLDER]
    if not keys:
        return None
    return max(keys, key=len)


def wait_for_api_key(page, timeout: int = 20, *, should_stop=None) -> str | None:
    def attempt():
        close_marketing_dialog(page)
        return extract_api_key(page)

    return poll_until(attempt, timeout=timeout, interval=1.0, should_stop=should_stop)


def verify_api_key(api_key: str, timeout: int = 30) -> bool:
    """拿新 key 打一次真实搜索——Tavily 会先发 key 再异步开通，不验就可能收下一个死 key。"""
    try:
        response = requests.post(
            SEARCH_API_URL,
            json={"api_key": api_key, "query": "api key verification", "max_results": 1},
            timeout=timeout,
        )
    except Exception:
        return False
    return response.status_code == 200
