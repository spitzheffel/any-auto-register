"""ChatGPT 的横切干扰：Cloudflare 全页拦截。

auth.openai.com 在 Cloudflare 后面，注册途中任何一步都可能撞上 "Just a moment"。
旧整段式路径没有处理，表现是某个等待超时后报"未找到输入框"——一个本该等一等
就过去的拦截，被当成站点改版停掉整批。

**没有 Turnstile handler**：旧实现 3900 行里 turnstile 出现 0 次，加上去会让
``describe_requirements`` 凭空多出一个 conditional captcha 渠道，前端跟着要求
配打码服务。等真在现场包里见到 Turnstile 再加，别先验地加。
"""
from __future__ import annotations

from core.registration.dom import is_cf_full_block, wait_cf_full_block_clear
from core.registration.obstacles import FullPageBlockHandler

from .dom import EMAIL_INPUT_SELECTORS, OTP_INPUT_SELECTORS, PASSWORD_INPUT_SELECTORS

#: 页面上还有这些输入框 = 只是内嵌 widget / 正常页面，不是全页拦截
FORM_SELECTOR = ", ".join(
    EMAIL_INPUT_SELECTORS[:3] + PASSWORD_INPUT_SELECTORS[:1] + OTP_INPUT_SELECTORS[:2]
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
