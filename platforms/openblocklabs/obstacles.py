"""OpenBlockLabs 的横切干扰：Cloudflare 全页拦截与 Turnstile。

旧实现把这两样写在流程里手工调用（``_wait_cf_full_block_clear`` 四处、
``_handle_turnstile`` 两处），而且那 150 行和 ``platforms/cursor/dom.py`` 里的
逐字相同。现在装配成 handler，引擎在每步前与每次失败后自动探测。

**不接打码 provider**：旧实现从没调过 solver，Turnstile 靠 Camoufox 的指纹 +
直接点 iframe 里的 checkbox 过。这里同样不传 ``inject_token`` / ``get_sitekey``，
所以 ``TurnstileObstacleHandler`` 只会走"点 checkbox → 等放行 → 人工兜底"这条路。
需要注意的行为变化：点击失败且 headless 时，引擎按 ``RiskControlError`` 结束这次
尝试（旧实现是干等 90 秒然后**当作过了继续走**，下游必然以"未进入验证码页面"收场）。
"""
from __future__ import annotations

from core.registration.obstacles import FullPageBlockHandler, TurnstileObstacleHandler

from . import dom

#: 这几步之前才探测：注册页会预渲染 Turnstile 的 iframe，
#: 填表之前就去"解"它只会在空表单上点提交
_TURNSTILE_STEPS = ("SubmitSignupForm", "AdvanceToEmailVerification", "AwaitEmailOtp")
#: 提交完表单那一下 Turnstile 可能晚几秒才渲染，探测时多等一会
_TURNSTILE_POLL_STEPS = ("AdvanceToEmailVerification",)
#: 旧实现在中间页循环里每轮等 2 秒判定"有没有 Turnstile"（分支 B 那次等 15 秒）。
#: 这里取 2 秒：晚到的挑战由引擎在本步骤失败重试前的那次探测接住，
#: 每一轮正常流程都白等 15 秒不值当。
TURNSTILE_POLL_SECONDS = 2.0


def _otp_ready(page, step: str) -> bool:
    """验证码框出现了就说明这一轮没有 Turnstile，别再等。"""
    return dom.is_email_verification_ready(page)


def build_obstacles() -> list:
    return [
        FullPageBlockHandler(
            is_blocked=dom.is_cf_full_block_here,
            wait_clear=dom.wait_cf_full_block_clear_here,
        ),
        TurnstileObstacleHandler(
            is_visible=dom.is_turnstile_modal_visible,
            click_in_iframe=dom.click_turnstile_in_iframe,
            poll_seconds=TURNSTILE_POLL_SECONDS,
            settled=_otp_ready,
            poll_before_steps=_TURNSTILE_POLL_STEPS,
            detect_before_steps=_TURNSTILE_STEPS,
        ),
    ]
