"""人工接码 —— 用自己的手机号完成验证。

验证"人也是一种 provider"：从流程的角度，它和 HeroSMS 没有区别，
只是 get_number / get_code 的答案来自操作员而不是 HTTP 接口。
"""
from __future__ import annotations

from typing import Callable

from core.base_sms import BaseSmsProvider, SmsActivation
from core.cancellation import CancellationToken
from core.errors import AttemptCancelled, ConfigError, TransientError
from providers.registry import register_provider


@register_provider("sms", "manual_sms")
class ManualSmsProvider(BaseSmsProvider):
    PROVIDER_LABEL = "人工接码"
    auto_report_success_on_code = True

    def __init__(self, human_channel=None):
        self.human_channel = human_channel
        self._counter = 0

    @classmethod
    def from_config(cls, config: dict) -> 'ManualSmsProvider':
        return cls(human_channel=(config or {}).get("human_channel"))

    # ------------------------------------------------------------------
    def get_number(self, *, platform: str = "", service: str = "", country: str = "") -> SmsActivation:
        answer = self._ask(
            kind="input",
            prompt=f"请输入用于 {platform or '本次注册'} 验证的手机号（含国家码，如 +8613800138000）",
            field="phone_number",
        )
        if not answer:
            raise TransientError("人工未提供手机号")
        self._counter += 1
        return SmsActivation(
            activation_id=f"manual-{self._counter}",
            phone_number=answer,
            country=country,
            metadata={"manual": True},
        )

    def get_code(self, activation_id: str, *, timeout: int = 120, should_stop: Callable[[], bool] | None = None) -> str:
        return self._ask(
            kind="input",
            prompt="请输入收到的短信验证码",
            field="code",
            timeout=timeout,
            should_stop=should_stop,
        )

    def cancel(self, activation_id: str) -> bool:
        return True

    def report_success(self, activation_id: str) -> bool:
        return True

    # ------------------------------------------------------------------
    def _ask(
        self,
        *,
        kind: str,
        prompt: str,
        field: str,
        timeout: int = 900,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        if self.human_channel is None:
            raise ConfigError(
                "人工接码需要可用的人工通道：请在 Web 界面发起任务（终端模式下不可用）"
            )
        from core.registration.steps import InteractionRequest

        request = InteractionRequest(kind=kind, prompt=prompt, fields=(field,), timeout=timeout)
        # 取消令牌透传给通道：任务取消时立刻从等待中醒来，而不是等到交互超时
        cancel = should_stop if isinstance(should_stop, CancellationToken) else None
        if cancel is None and should_stop is not None:
            cancel = CancellationToken(should_stop, throttle_seconds=0.5)
        response = self.human_channel.ask(request, cancel=cancel) if cancel is not None else self.human_channel.ask(request)
        if getattr(response, "cancelled", False):
            # 取消是控制流信号，不是失败：不该计入错误数、也不该触发补投
            raise AttemptCancelled("人工接码等待期间任务被取消")
        if getattr(response, "timed_out", False):
            raise TransientError("人工接码超时未回答")
        return str(response.value or response.values.get(field, "") or "").strip()
