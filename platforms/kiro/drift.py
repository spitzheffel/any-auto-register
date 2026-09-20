"""Kiro 的"漂移面"：站点重新部署后最先失效的那几样东西。

``scripts/platform_drift_check.py`` 拿一份新录的 HAR（以及可选的交互记录）与这里
对比，报告哪些端点、选择器已经对不上。常量一律从活代码 import 而不是抄一份，
否则检查通过只说明两份抄写一致，说明不了线上还能跑。

Kiro 自己没多少东西，漂的是 AWS：注册 SPA 的 hash 步骤名一改，
``steps.py`` 里四个 ``when()`` 全部失效且**不报错**（只是全被跳过），
所以这里把步骤名也一并声明出来。
"""
from __future__ import annotations

from .dom import (
    AWS_PROFILE_DOMAIN,
    AWS_SIGNIN_DOMAIN,
    EMAIL_INPUT_SELECTORS,
    KIRO_URL,
    NAME_INPUT_SELECTORS,
    OTP_INPUT_SELECTORS,
    PASSWORD_INPUT_SELECTORS,
    STEP_CREATE_PASSWORD,
    STEP_ENTER_EMAIL,
    STEP_ENTER_NAME,
    STEP_VERIFY_EMAIL,
    SUBMIT_TEXTS,
)

_KIRO_HOST = KIRO_URL.split("://", 1)[1]

#: Kiro 不是 Next.js server-action 站点，这三项留空让 check_action_ids 直接跳过
ACTION_FIELD_HINTS: dict[str, tuple[str, ...]] = {}
ACTION_HEADER = "next-action"
ACTION_REQUEST_HOST = _KIRO_HOST
ACTION_REQUEST_PATH = "/signin"

#: 正常注册一定会打到的端点；HAR 里没出现说明流程变了或录得不完整
EXPECTED_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", _KIRO_HOST, "/signin"),
    ("GET", AWS_PROFILE_DOMAIN, "/"),
)

#: 只有部分投放 / 走到某些分支才出现，缺失不算漂移
OPTIONAL_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", f"us-east-1.{AWS_SIGNIN_DOMAIN}.amazon.com", "/"),
    ("GET", _KIRO_HOST, "/"),
)

#: 浏览器路径依赖的选择器，按页面阶段分组；
#: 交互记录里没有任何元素能匹配就报"可能失效"
SELECTORS_BY_STAGE: dict[str, tuple[str, ...]] = {
    "email": tuple(EMAIL_INPUT_SELECTORS),
    "name": tuple(NAME_INPUT_SELECTORS),
    "otp": tuple(OTP_INPUT_SELECTORS),
    "password": tuple(PASSWORD_INPUT_SELECTORS),
}

#: SPA 的步骤名写在 URL hash 里，steps.py 的四个 when() 全靠它认页面
SIGNUP_HASH_STEPS: tuple[str, ...] = (
    STEP_ENTER_EMAIL,
    STEP_ENTER_NAME,
    STEP_VERIFY_EMAIL,
    STEP_CREATE_PASSWORD,
)

#: "下一步"按钮的文案候选；AWS 换文案时按文本点击会全线失效
SUBMIT_BUTTON_TEXTS = SUBMIT_TEXTS


def known_actions() -> dict[str, str]:
    return {}


def action_cache_path():
    return None
