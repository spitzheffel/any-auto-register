"""Cursor 的横切干扰：Turnstile 与 Cloudflare 全页拦截。

选择器辅助函数在 dom.py，这里只做装配。
原先注册页与密码页各写了一遍 Turnstile 处理逻辑，现在合成一个 handler，
由引擎在步骤前与失败后自动探测，两个出现点都覆盖。
"""
from __future__ import annotations

from core.registration.obstacles import FullPageBlockHandler, TurnstileObstacleHandler

from .dom import (
    TURNSTILE_SITEKEY,
    _click_continue,
    _click_turnstile_in_iframe,
    _get_turnstile_sitekey,
    _inject_turnstile,
    _is_cf_full_block,
    _is_turnstile_modal_visible,
    _wait_cf_full_block_clear,
)

# 各步骤之前"页面已进入下一阶段"的标志：任一出现就说明这一轮没有 Turnstile。
# 密码框只对"提交注册表单之后"算下一阶段；到了密码页之后它还在 DOM 里，
# 不能再拿它当放行信号，否则密码页自己晚弹的 Turnstile 探测不到
_OTP_OR_PHONE_SELECTOR = (
    'input[type="tel"], input[name="otp"], '
    'input[autocomplete="one-time-code"], input[inputmode="numeric"]'
)
_NEXT_STAGE_SELECTORS = {
    "MaybeSetPassword": f'input[type="password"], {_OTP_OR_PHONE_SELECTOR}',
    "MaybePhoneVerify": _OTP_OR_PHONE_SELECTOR,
    "AwaitAndFillEmailOtp": _OTP_OR_PHONE_SELECTOR,
}
_TURNSTILE_STEPS = tuple(_NEXT_STAGE_SELECTORS)


def _next_stage_ready(page, step: str) -> bool:
    selector = _NEXT_STAGE_SELECTORS.get(step)
    if not selector:
        return False
    try:
        element = page.query_selector(selector)
        return bool(element and element.is_visible())
    except Exception:
        return False


def build_obstacles() -> list:
    return [
        FullPageBlockHandler(
            is_blocked=_is_cf_full_block,
            wait_clear=_wait_cf_full_block_clear,
        ),
        TurnstileObstacleHandler(
            is_visible=_is_turnstile_modal_visible,
            click_in_iframe=_click_turnstile_in_iframe,
            get_sitekey=_get_turnstile_sitekey,
            inject_token=_inject_turnstile,
            # 注入 token 后要再点一次 Continue，表单才会带着 token 提交
            on_injected=_click_continue,
            default_sitekey=TURNSTILE_SITEKEY,
            # 旧脚本只在提交表单 / 密码之后才处理 Turnstile，并各等 15s / 12s 判定"没有"：
            # 这里只在这几个出现点之后的步骤前探测与多等，页面一进下一阶段就放行
            poll_seconds=15.0,
            settled=_next_stage_ready,
            poll_before_steps=_TURNSTILE_STEPS,
            detect_before_steps=_TURNSTILE_STEPS,
        ),
    ]
