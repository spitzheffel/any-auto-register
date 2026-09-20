"""OpenBlockLabs 的"漂移面"：站点重新部署后最先失效的那几样东西。

``scripts/platform_drift_check.py`` 拿一份新录的 HAR（以及可选的交互记录）与这里
对比，报告哪些端点、选择器已经对不上。常量一律从活代码 import 而不是抄一份，
否则检查通过只说明两份抄写一致，说明不了线上还能跑。

真正会把流程打死的是 ``CLIENT_ID``：它写死在代码里，WorkOS 那边换了应用就整条
注册链路失效，而表现只是"等 authorization_session_id 超时"。
"""
from __future__ import annotations

from .dom import (
    AUTH_BASE,
    CLIENT_ID,
    DASHBOARD_HOST,
    EMAIL_INPUT_SELECTORS,
    OTP_INPUT_SELECTORS,
    PASSWORD_INPUT_SELECTORS,
)

_AUTH_HOST = AUTH_BASE.split("://", 1)[1]

#: WorkOS 不是 Next.js server-action 站点，这三项留空让 check_action_ids 直接跳过
ACTION_FIELD_HINTS: dict[str, tuple[str, ...]] = {}
ACTION_HEADER = "next-action"
ACTION_REQUEST_HOST = _AUTH_HOST
ACTION_REQUEST_PATH = "/sign-up"

#: 正常注册一定会打到的端点；HAR 里没出现说明流程变了或录得不完整
EXPECTED_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", _AUTH_HOST, "/"),
    ("GET", _AUTH_HOST, "/sign-up"),
    ("GET", DASHBOARD_HOST, "/auth/callback"),
)

#: 只有部分投放 / 走到某些分支才出现，缺失不算漂移
OPTIONAL_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", _AUTH_HOST, "/sign-up/password"),
    ("GET", _AUTH_HOST, "/sign-up/email-verification"),
    ("GET", DASHBOARD_HOST, "/"),
)

#: 浏览器路径依赖的选择器，按页面阶段分组；
#: 交互记录里没有任何元素能匹配就报"可能失效"
SELECTORS_BY_STAGE: dict[str, tuple[str, ...]] = {
    "email": tuple(EMAIL_INPUT_SELECTORS),
    "password": tuple(PASSWORD_INPUT_SELECTORS),
    "otp": tuple(OTP_INPUT_SELECTORS),
}

#: 写死在代码里的 WorkOS 应用 id，漂了整条注册链路失效
KNOWN_CLIENT_ID = CLIENT_ID


def known_actions() -> dict[str, str]:
    return {}


def action_cache_path():
    return None
