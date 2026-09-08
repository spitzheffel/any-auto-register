"""Cursor 的"漂移面"：站点重新部署后最先失效的那几样东西，集中声明给 drift 检查用。

`scripts/platform_drift_check.py` 拿一份新录的 HAR（以及可选的交互记录）与这里对比，
报告哪些 server action id、端点、选择器已经对不上。声明放在平台包里，
是为了让"站点变了要看哪里"和"代码里实际用的是什么"始终是同一份。
"""
from __future__ import annotations

from .core import AUTH, CURSOR, _KNOWN_ACTIONS, _default_action_cache_path
from .obstacles import _NEXT_STAGE_SELECTORS
from .steps import OTP_SELECTORS, PHONE_SELECTORS

#: 表单字段名 → 步骤键：从 HAR 里的 multipart 请求体反推每个 next-action 属于哪一步
ACTION_FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "submit_email": ("email",),
    "submit_password": ("password",),
    "magic_code": ("code", "otp"),
}

#: 协议路径会打到的端点（method, host, path 前缀）；HAR 里没出现说明流程变了或录得不完整
EXPECTED_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("POST", AUTH.split("://", 1)[1], "/sign-up"),
    ("GET", AUTH.split("://", 1)[1], "/sign-up"),
    ("GET", CURSOR.split("://", 1)[1], "/api/auth/callback"),
)

#: 只有触发风控挑战时才会出现，缺失不算漂移
OPTIONAL_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("POST", AUTH.split("://", 1)[1], "/radar-challenge/send"),
    ("POST", AUTH.split("://", 1)[1], "/radar-challenge/verify"),
)

#: 浏览器路径依赖的输入框选择器，按页面阶段分组；交互记录里没有任何元素能匹配就报"可能失效"
SELECTORS_BY_STAGE: dict[str, tuple[str, ...]] = {
    "signup": (
        'input[name="firstName"], input[name="first_name"]',
        'input[name="lastName"], input[name="last_name"]',
        'input[name="email"]',
    ),
    "password": ('input[type="password"]',),
    "phone": (PHONE_SELECTORS,),
    "otp": tuple(OTP_SELECTORS[:4]),
    "next_stage": tuple(_NEXT_STAGE_SELECTORS.values()),
}

#: 服务端 action 请求的识别方式：带这个 header 的 POST 到 sign-up 页
ACTION_HEADER = "next-action"
ACTION_REQUEST_HOST = AUTH.split("://", 1)[1]
ACTION_REQUEST_PATH = "/sign-up"


def known_actions() -> dict[str, str]:
    return dict(_KNOWN_ACTIONS)


def action_cache_path():
    return _default_action_cache_path()
