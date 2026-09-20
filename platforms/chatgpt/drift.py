"""ChatGPT 的"漂移面"：站点重新部署后最先失效的那几样东西。

``scripts/platform_drift_check.py`` 拿一份新录的 HAR（以及可选的交互记录）与这里
对比，报告哪些端点、选择器已经对不上。常量一律从活代码 import 而不是抄一份，
否则检查通过只说明两份抄写一致，说明不了线上还能跑。

与 cursor 的差别：OpenAI 不是 Next.js server-action 站点，没有 next-action id
这一层，所以 action 相关的声明全是空的，检查只覆盖端点与选择器。
"""
from __future__ import annotations

from .about_you import ABOUT_YOU_SUBMIT_SELECTORS
from .constants import CHATGPT_APP, OPENAI_AUTH
from .dom import (
    EMAIL_INPUT_SELECTORS,
    EMAIL_SUBMIT_SELECTORS,
    OTP_INPUT_SELECTORS,
    PASSWORD_INPUT_SELECTORS,
    PASSWORD_SUBMIT_SELECTORS,
)
from .phone import PHONE_INPUT_SELECTORS, PHONE_SEND_SELECTORS

_AUTH_HOST = OPENAI_AUTH.split("://", 1)[1]
_APP_HOST = CHATGPT_APP.split("://", 1)[1]

#: OpenAI 没有 server action 层，这三项留空让 check_action_ids 直接跳过
ACTION_FIELD_HINTS: dict[str, tuple[str, ...]] = {}
ACTION_HEADER = "next-action"
ACTION_REQUEST_HOST = _AUTH_HOST
ACTION_REQUEST_PATH = "/log-in"

#: 正常注册一定会打到的端点；HAR 里没出现说明流程变了或录得不完整
EXPECTED_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", _AUTH_HOST, "/log-in"),
    ("GET", _AUTH_HOST, "/create-account/password"),
    ("GET", _AUTH_HOST, "/email-verification"),
    ("POST", _AUTH_HOST, "/api/accounts/user/register"),
    ("POST", _AUTH_HOST, "/api/accounts/email-otp/validate"),
    ("POST", _AUTH_HOST, "/oauth/token"),
)

#: 只有部分投放 / 触发风控时才出现，缺失不算漂移
OPTIONAL_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", _AUTH_HOST, "/about-you"),
    ("GET", _AUTH_HOST, "/add-phone"),
    ("POST", _AUTH_HOST, "/api/accounts/create_account"),
    ("POST", _AUTH_HOST, "/api/accounts/workspace/select"),
    ("POST", _AUTH_HOST, "/api/accounts/organization/select"),
    ("GET", _APP_HOST, "/"),
)

#: 浏览器路径依赖的选择器，按页面阶段分组；
#: 交互记录里没有任何元素能匹配就报"可能失效"
SELECTORS_BY_STAGE: dict[str, tuple[str, ...]] = {
    "email": tuple(EMAIL_INPUT_SELECTORS[:4]),
    "email_submit": tuple(EMAIL_SUBMIT_SELECTORS[:4]),
    "password": tuple(PASSWORD_INPUT_SELECTORS[:3]),
    "password_submit": tuple(PASSWORD_SUBMIT_SELECTORS[:4]),
    "otp": tuple(OTP_INPUT_SELECTORS[:4]),
    "about_you_submit": tuple(ABOUT_YOU_SUBMIT_SELECTORS[:4]),
    "phone": tuple(PHONE_INPUT_SELECTORS[:4]),
    "phone_send": tuple(PHONE_SEND_SELECTORS[:4]),
}


def known_actions() -> dict[str, str]:
    return {}


def action_cache_path():
    return None
