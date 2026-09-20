"""Windsurf 的"漂移面"：站点重新部署后最先失效的那几样东西。

``scripts/platform_drift_check.py`` 拿一份新录的 HAR（以及可选的交互记录）与这里
对比，报告哪些端点、选择器已经对不上。常量一律从活代码 import 而不是抄一份，
否则检查通过只说明两份抄写一致，说明不了线上还能跑。

Windsurf 的注册结果不在 DOM 上：session 只从 ``WindsurfPostAuth`` 的 protobuf
响应里捕获。所以这两个路径（``EMAIL_COMPLETE_PATH`` / ``POST_AUTH_PATH``）
和选择器一样是硬依赖——它们一改，注册会"看上去成功"但拿不到 token。
"""
from __future__ import annotations

from platforms.windsurf.core import SEAT_SERVICE

from .dom import (
    CONFIRM_PASSWORD_SELECTOR,
    EMAIL_COMPLETE_PATH,
    EMAIL_SELECTOR,
    FAMILY_NAME_SELECTOR,
    GIVEN_NAME_SELECTOR,
    OTP_INPUT_SELECTOR,
    PASSWORD_SELECTOR,
    POST_AUTH_PATH,
    SUBMIT_BUTTON_SELECTOR,
    TOS_CHECKBOX_SELECTOR,
    WINDSURF_BASE,
)

_HOST = WINDSURF_BASE.split("://", 1)[1]

#: Windsurf 不是 Next.js server-action 站点，这三项留空让 check_action_ids 直接跳过
ACTION_FIELD_HINTS: dict[str, tuple[str, ...]] = {}
ACTION_HEADER = "next-action"
ACTION_REQUEST_HOST = _HOST
ACTION_REQUEST_PATH = "/account/register"

#: 正常注册一定会打到的端点；HAR 里没出现说明流程变了或录得不完整
EXPECTED_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", _HOST, "/account/login"),
    ("GET", _HOST, "/account/register"),
    ("POST", _HOST, "/_devin-auth/email/start"),
    ("POST", _HOST, EMAIL_COMPLETE_PATH),
    ("POST", _HOST, f"{SEAT_SERVICE}/WindsurfPostAuth"),
)

#: 只有部分投放 / 走到某些分支才出现，缺失不算漂移
OPTIONAL_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("POST", _HOST, f"{SEAT_SERVICE}/GetCurrentUser"),
    ("POST", _HOST, f"{SEAT_SERVICE}/GetPlanStatus"),
    ("POST", _HOST, f"{SEAT_SERVICE}/GetStripeSubscriptionState"),
    ("GET", _HOST, "/profile"),
)

#: 浏览器路径依赖的选择器，按页面阶段分组；
#: 交互记录里没有任何元素能匹配就报"可能失效"
SELECTORS_BY_STAGE: dict[str, tuple[str, ...]] = {
    "name": (GIVEN_NAME_SELECTOR, FAMILY_NAME_SELECTOR),
    "email": (EMAIL_SELECTOR,),
    "tos": (TOS_CHECKBOX_SELECTOR,),
    "password": (PASSWORD_SELECTOR, CONFIRM_PASSWORD_SELECTOR),
    "otp": (OTP_INPUT_SELECTOR,),
    "submit": (SUBMIT_BUTTON_SELECTOR,),
}

#: 注册结果只在这两个响应里，改了名字注册会"看上去成功"但拿不到 token
RESULT_RESPONSE_PATHS: tuple[str, ...] = (EMAIL_COMPLETE_PATH, POST_AUTH_PATH)


def known_actions() -> dict[str, str]:
    return {}


def action_cache_path():
    return None
