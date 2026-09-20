"""Grok 的"漂移面"：站点重新部署后最先失效的那几样东西。

``scripts/platform_drift_check.py`` 拿一份新录的 HAR（以及可选的交互记录）与这里
对比，报告哪些端点、选择器已经对不上。常量一律从活代码 import 而不是抄一份，
否则检查通过只说明两份抄写一致，说明不了线上还能跑。

accounts.x.ai 是 Next.js 站点，协议路径靠一个写死的 server action id
(``core.NEXT_ACTION``) 打 ``/sign-up``；浏览器路径不碰它，但它漂了协议路径就废，
所以这里把它一并声明出来。
"""
from __future__ import annotations

from .core import ACCOUNTS_URL as _CORE_ACCOUNTS_URL
from .core import NEXT_ACTION
from .dom import (
    ACCOUNTS_URL,
    EMAIL_INPUT_SELECTORS,
    GIVEN_NAME_SELECTORS,
    GROK_APP_URL,
    OTP_INPUT_SELECTORS,
    PASSWORD_INPUT_SELECTORS,
    SIGNUP_SUBMIT_SELECTORS,
)

assert ACCOUNTS_URL == _CORE_ACCOUNTS_URL, "协议路径与浏览器路径的 accounts 域名必须一致"

_ACCOUNTS_HOST = ACCOUNTS_URL.split("://", 1)[1]
_APP_HOST = GROK_APP_URL.split("://", 1)[1]

#: server action 的请求头与落点；字段名用于在 HAR 里认出是哪一个 action
ACTION_HEADER = "next-action"
ACTION_REQUEST_HOST = _ACCOUNTS_HOST
ACTION_REQUEST_PATH = "/sign-up"
ACTION_FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "signup": ("email", "password", "givenName", "familyName"),
}

#: 正常注册一定会打到的端点；HAR 里没出现说明流程变了或录得不完整
EXPECTED_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", _ACCOUNTS_HOST, "/sign-up"),
    ("POST", _ACCOUNTS_HOST, "/sign-up"),
)

#: 只有部分投放 / 走到某些分支才出现，缺失不算漂移
OPTIONAL_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", _ACCOUNTS_HOST, "/sign-in"),
    ("GET", _APP_HOST, "/"),
)

#: 浏览器路径依赖的选择器，按页面阶段分组；
#: 交互记录里没有任何元素能匹配就报"可能失效"
SELECTORS_BY_STAGE: dict[str, tuple[str, ...]] = {
    "email": tuple(EMAIL_INPUT_SELECTORS),
    "otp": tuple(OTP_INPUT_SELECTORS),
    "given_name": tuple(GIVEN_NAME_SELECTORS),
    "password": tuple(PASSWORD_INPUT_SELECTORS),
    "signup_submit": tuple(SIGNUP_SUBMIT_SELECTORS[:2]),
}


def known_actions() -> dict[str, str]:
    return {"signup": NEXT_ACTION}


def action_cache_path():
    return None
