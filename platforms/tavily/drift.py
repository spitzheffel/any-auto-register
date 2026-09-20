"""Tavily 的"漂移面"：站点重新部署后最先失效的那几样东西。

``scripts/platform_drift_check.py`` 拿一份新录的 HAR（以及可选的交互记录）与这里
对比，报告哪些端点、选择器已经对不上。常量一律从活代码 import 而不是抄一份，
否则检查通过只说明两份抄写一致，说明不了线上还能跑。

Tavily 的登录页是 Auth0 Universal Login，不是 Next.js server-action 站点，
所以 action 相关的声明全是空的。最容易无声失效的是那条抠注册入口的正则
（``extract_signup_url``）——Auth0 换一版模板，注册就直接卡在第一步。
"""
from __future__ import annotations

from .dom import (
    API_KEY_PATTERN,
    APP_URL,
    AUTH_URL,
    EMAIL_INPUT_SELECTORS,
    ORG_ONLY_SIGNUP_MARKERS,
    OTP_INPUT_SELECTOR,
    PASSWORD_INPUT_SELECTOR,
    PRIMARY_BUTTON_SELECTORS,
    SEARCH_API_URL,
    TURNSTILE_SITEKEY,
)

_APP_HOST = APP_URL.split("://", 1)[1]
_AUTH_HOST = AUTH_URL.split("://", 1)[1]
_API_HOST = SEARCH_API_URL.split("://", 1)[1].split("/", 1)[0]

#: Auth0 不是 server-action 站点，这三项留空让 check_action_ids 直接跳过
ACTION_FIELD_HINTS: dict[str, tuple[str, ...]] = {}
ACTION_HEADER = "next-action"
ACTION_REQUEST_HOST = _AUTH_HOST
ACTION_REQUEST_PATH = "/u/signup/identifier"

#: 正常注册一定会打到的端点；HAR 里没出现说明流程变了或录得不完整
EXPECTED_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", _APP_HOST, "/sign-in"),
    ("GET", _AUTH_HOST, "/u/signup/identifier"),
    ("POST", _AUTH_HOST, "/u/signup/identifier"),
    ("POST", _API_HOST, "/search"),
)

#: 只有部分投放 / 走到某些分支才出现，缺失不算漂移
OPTIONAL_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", _AUTH_HOST, "/u/login/code"),
    ("GET", _AUTH_HOST, "/u/signup/password"),
    ("GET", _AUTH_HOST, "/u/verify"),
    ("GET", _APP_HOST, "/home"),
)

#: 浏览器路径依赖的选择器，按页面阶段分组；
#: 交互记录里没有任何元素能匹配就报"可能失效"
SELECTORS_BY_STAGE: dict[str, tuple[str, ...]] = {
    "email": tuple(EMAIL_INPUT_SELECTORS),
    "otp": (OTP_INPUT_SELECTOR,),
    "password": (PASSWORD_INPUT_SELECTOR,),
    "submit": tuple(PRIMARY_BUTTON_SELECTORS[:2]),
}

#: 注册入口的 URL 带一次性 state，只能从 sign-in 页的 HTML 里抠；这条正则漂了就卡在第一步
SIGNUP_LINK_PATTERN = r'href="(/u/signup/identifier[^"]*)"'
#: Turnstile 的默认 sitekey（页面上读不到时用）
DEFAULT_TURNSTILE_SITEKEY = TURNSTILE_SITEKEY
#: 控制台上 API Key 的形状
API_KEY_REGEX = API_KEY_PATTERN
#: "只对特定组织开放邮箱密码注册"的页面原话；文案变了这条致命判定就失灵
ORG_ONLY_MARKERS = ORG_ONLY_SIGNUP_MARKERS


def known_actions() -> dict[str, str]:
    return {}


def action_cache_path():
    return None
