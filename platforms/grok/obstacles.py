"""Grok 的横切干扰：Cloudflare 全页拦截。

accounts.x.ai 在 Cloudflare 后面（协议路径为此备着 ``TURNSTILE_SITEKEY``），
注册途中任何一步都可能撞上 "Just a moment"。旧整段式路径没有处理，表现是某个
等待超时后报"未找到输入框"——一个本该等一等就过去的拦截被当成站点改版。

**不加 TurnstileObstacleHandler**：浏览器路径用 Camoufox 正是为了让 Turnstile
自己过，旧实现里一次都没碰过打码。加上去会让 ``describe_requirements`` 凭空多出
一个 conditional captcha 渠道，前端跟着要求配打码服务。等真在失败现场包里见到
Turnstile 弹窗再加。
"""
from __future__ import annotations

from core.registration.dom import is_cf_full_block, wait_cf_full_block_clear
from core.registration.obstacles import FullPageBlockHandler

from .dom import EMAIL_INPUT_SELECTORS, OTP_INPUT_SELECTORS, PASSWORD_INPUT_SELECTORS

#: 页面上还有这些输入框 = 正常页面，不是全页拦截
FORM_SELECTOR = ", ".join(
    EMAIL_INPUT_SELECTORS[:2] + PASSWORD_INPUT_SELECTORS[:1] + OTP_INPUT_SELECTORS[:2]
)


def _is_cf_full_block(page) -> bool:
    return is_cf_full_block(page, form_selector=FORM_SELECTOR)


def _wait_cf_full_block_clear(page, timeout: float = 120, log_fn=print, *, should_stop=None) -> None:
    wait_cf_full_block_clear(page, timeout, log_fn, is_blocked=_is_cf_full_block, should_stop=should_stop)


def build_obstacles() -> list:
    return [
        FullPageBlockHandler(
            is_blocked=_is_cf_full_block,
            wait_clear=_wait_cf_full_block_clear,
        ),
    ]
