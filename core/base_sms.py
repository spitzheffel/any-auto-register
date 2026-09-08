"""接码服务基类 + 工厂。

各厂商实现（SMS-Activate / HeroSMS / SMSBower / 人工接码）在 ``providers/sms/`` 下，
经 ``providers.registry`` 注册后由 ``create_sms_provider`` 按 driver_type 创建。
本模块不得在顶层 import ``providers.*``：core 是被 providers 依赖的一方，反向引用会
让"新增一家厂商不改 core"的约束失效。
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

from core.errors import ConfigError


@dataclass
class SmsActivation:
    """Represents an active phone number rental."""
    activation_id: str
    phone_number: str
    country: str = ""
    metadata: dict = field(default_factory=dict)


# 声明 requires_exclusive_verification_window 的 provider 共享这把锁：它约束的是
# "租号 → 验证完成"这段窗口（进程级号码缓存不允许两个尝试同时插进来）。
_SMS_VERIFY_WINDOW_LOCK = threading.RLock()


class BaseSmsProvider(ABC):
    """Base class for SMS verification code providers."""

    auto_report_success_on_code = True
    # 号码缓存等进程级共享状态要求整个验证窗口串行时置 True（见 _SMS_VERIFY_WINDOW_LOCK）
    requires_exclusive_verification_window = False
    # 未显式指定国家时用的国家；子类在 __init__ 里按自己的配置覆盖
    default_country = ""

    def resolve_service_code(self, *, platform: str = "", service: str = "") -> str:
        """把系统平台名解析为供应商原生服务码；显式 service 优先。"""
        return str(service or platform or "").strip().lower()

    def peek_reusable_country(self, *, platform: str = "", service: str = "") -> str:
        """当前已握着、还能再用一次的号码所在国家（没有则空串）。

        调用方据此跳过选国：已经有可复用号码时换国家只会错过缓存。
        默认无复用能力 —— 恒为空串。
        """
        return ""

    def select_best_country(
        self,
        *,
        platform: str = "",
        service: str = "",
        log_fn: Callable[[str], None] | None = None,
    ) -> str:
        """本次租号自动挑选的国家（不自动选国 / 没挑出来时返回空串）。

        要不要启用、按什么条件挑、挑的过程说些什么，全由 provider 自己决定；
        ``log_fn`` 用于把过程讲给调用方的日志（不传则静默）。
        """
        return ""

    def fallback_country(self) -> str:
        """provider 自选的国家取号失败后，回退重试用的国家。"""
        return str(self.default_country or "")

    @abstractmethod
    def get_number(
        self,
        *,
        platform: str = "",
        service: str = "",
        country: str = "",
    ) -> SmsActivation:
        """按系统平台租用号码；service 可显式覆盖供应商原生服务码。"""
        ...

    @abstractmethod
    def get_code(
        self,
        activation_id: str,
        *,
        timeout: int = 120,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        """Wait for and return the SMS verification code.

        ``should_stop`` 由取消令牌提供，轮询循环每轮检查一次，返回 True 即提前放弃。
        """
        ...

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        """Cancel/release an activation. Returns True on success."""
        ...

    def report_success(self, activation_id: str) -> bool:
        """Report that the code was used successfully (optional)."""
        return True

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        """Optional hook used by providers that can request upstream resend."""
        return None

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        """Optional hook used when the target service rejects a received code."""
        return None

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        """Optional hook used when the target service rejects the rented phone."""
        return None

    def mark_send_succeeded(self, activation_id: str) -> None:
        """Optional hook used when the target service accepts the rented phone."""
        return None

    def get_reuse_info(self) -> dict:
        """Return provider-specific reuse state for task scheduling."""
        return {}

    def has_reusable_capacity(self) -> bool:
        """当前是否还握着能再用一次的号码。

        调度器据此决定"成功数已达标但号码没用完时要不要再发一个尝试"，
        默认 False —— 不认识任何厂商，只问这个通用问题。
        """
        return False

    def oversubscribe_policy(self, settings: dict | None = None) -> dict:
        """调度策略：

        - ``compensate_failures``：失败是否自动补投尝试，直到达成成功目标
          （号码可复用的厂商重试成本低，值得补；一次性号码不补）
        - ``reuse_to_max`` + ``extra_max``：号码用不完时最多额外成功几个

        默认三项全关，调度行为与普通模式一致。
        """
        return {"compensate_failures": False, "extra_max": 0, "reuse_to_max": False}


def probe_phone_reuse(provider_key: str, config: dict | None = None) -> tuple[bool, dict]:
    """通用复用探测：provider 说自己还握着可复用号码时返回 (True, info)。

    调度器只问这一个问题，不认识任何厂商；不支持复用的 provider 恒为 False。
    """
    try:
        provider = create_sms_provider(provider_key, dict(config or {}))
    except Exception:
        return False, {"alive": False}
    try:
        if not provider.has_reusable_capacity():
            return False, provider.get_reuse_info() or {"alive": False}
        return True, provider.get_reuse_info()
    except Exception:
        return False, {"alive": False}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# provider_key 可以是用户自定义的（DB 里挂到某个 driver_type 上），
# 这里只保留历史短名到 driver_type 的兜底映射
_LEGACY_SMS_DRIVER_ALIASES = {
    "sms_activate": "sms_activate_api",
    "herosms": "herosms_api",
    "smsbower": "smsbower_api",
}


def resolve_sms_driver_type(provider_key: str) -> str:
    """把 provider_key 解析成 registry 里的 driver_type（优先查 DB definition）。"""
    key = str(provider_key or "").strip()
    if not key:
        return ""
    try:
        from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

        definition = ProviderDefinitionsRepository().get_by_key("sms", key)
        if definition and str(definition.driver_type or "").strip():
            return str(definition.driver_type).strip()
    except Exception:
        pass
    return _LEGACY_SMS_DRIVER_ALIASES.get(key, key)


def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """Create an SMS provider instance from config (走统一 registry)。"""
    # 延迟 import：registry 会扫描 providers/*，而那些模块反过来依赖本模块的基类
    from providers.registry import create_provider

    driver_type = resolve_sms_driver_type(provider_key)
    try:
        return create_provider("sms", driver_type, dict(config or {}))
    except ValueError as exc:
        # 配置指向了不存在的 driver：停批，不要把尝试次数烧在同一个错误上
        raise ConfigError(f"未知的接码服务: {provider_key}") from exc


# 号码租用的生命周期（取号 / 等码 / 归还）见 core.phone_verification.PhoneVerification。
