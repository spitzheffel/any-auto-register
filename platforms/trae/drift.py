"""Trae 的"漂移面"：站点重新部署后最先失效的那几样东西。

``scripts/platform_drift_check.py`` 拿一份新录的 HAR（以及可选的交互记录）与这里
对比，报告哪些端点、选择器已经对不上。常量一律从活代码 import 而不是抄一份，
否则检查通过只说明两份抄写一致，说明不了线上还能跑。

Trae 走 ByteDance Passport，不是 Next.js server-action 站点，所以 action 相关的
声明全是空的，检查只覆盖端点与选择器。按钮不是 ``<button>``（见 dom.py），
选择器面里只有输入框——"Send Code" / "Sign Up" 是按可见文本找的，
漂了只能靠真实跑一次发现。
"""
from __future__ import annotations

from .core import AID, API_SG, BASE_URL
from .dom import (
    EMAIL_INPUT_SELECTORS,
    OTP_INPUT_SELECTORS,
    PASSWORD_INPUT_SELECTORS,
    SEND_CODE_PATH,
    TRAE_API_SG,
    TRAE_PASSPORT_DOMAIN,
    TRAE_URL,
)

assert BASE_URL == f"https://{TRAE_PASSPORT_DOMAIN}", "协议路径与浏览器路径的 passport 域名必须一致"
assert API_SG == TRAE_API_SG, "协议路径与浏览器路径的 API 域名必须一致"

_WEB_HOST = TRAE_URL.split("://", 1)[1]
_PASSPORT_HOST = TRAE_PASSPORT_DOMAIN
_API_HOST = TRAE_API_SG.split("://", 1)[1]

#: Trae 没有 server action 层，这三项留空让 check_action_ids 直接跳过
ACTION_FIELD_HINTS: dict[str, tuple[str, ...]] = {}
ACTION_HEADER = "x-bogus"
ACTION_REQUEST_HOST = _PASSPORT_HOST
ACTION_REQUEST_PATH = SEND_CODE_PATH

#: 正常注册一定会打到的端点；HAR 里没出现说明流程变了或录得不完整
EXPECTED_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", _WEB_HOST, "/sign-up"),
    ("POST", _PASSPORT_HOST, SEND_CODE_PATH),
    ("POST", _PASSPORT_HOST, "/cloudide/api/v3/trae/Login"),
    ("POST", _API_HOST, "/cloudide/api/v3/common/GetUserToken"),
)

#: 只有部分投放 / 走到某些分支才出现，缺失不算漂移
OPTIONAL_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("POST", _PASSPORT_HOST, "/passport/web/email/register"),
    ("POST", _PASSPORT_HOST, "/cloudide/api/v3/trae/CheckLogin"),
    ("GET", _WEB_HOST, "/"),
)

#: 浏览器路径依赖的选择器，按页面阶段分组；
#: 交互记录里没有任何元素能匹配就报"可能失效"
SELECTORS_BY_STAGE: dict[str, tuple[str, ...]] = {
    "email": tuple(EMAIL_INPUT_SELECTORS),
    "otp": tuple(OTP_INPUT_SELECTORS),
    "password": tuple(PASSWORD_INPUT_SELECTORS),
}

#: 协议路径写死的应用 id，漂了协议注册直接 400
KNOWN_APP_ID = AID


def known_actions() -> dict[str, str]:
    return {}


def action_cache_path():
    return None
