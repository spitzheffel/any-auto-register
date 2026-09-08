"""显式手机号验证租约。

取代 ``PhoneCallbackController`` 的隐式双阶段 callable（第一次调用租号、
第二次调用等码，靠 ``phase`` 字段区分）：调用方先 ``acquire()`` 拿到
``PhoneLease``，再在其上 ``wait_code()`` / ``report_*()`` / ``release()``，
换号重试用 ``swap()``——不再需要外部篡改内部状态字段。
"""
from __future__ import annotations

import inspect
import logging
import time
from typing import Any, Callable, Optional

import core.base_sms as base_sms
from core.base_sms import (
    _SMS_VERIFY_WINDOW_LOCK,
    BaseSmsProvider,
    SmsActivation,
)
from core.errors import AttemptCancelled, ProviderError

logger = logging.getLogger(__name__)

DEFAULT_CODE_TIMEOUT = 180
#: 兄弟尝试最多把独占验证窗口占到号码寿命耗尽（HeroSMS 20 分钟）；
#: 再等就说明锁被泄漏了，与其永久卡死不如报错让上层换渠道 / 重启
VERIFY_WINDOW_LOCK_TIMEOUT = 1500.0

OUTCOME_SUCCESS = "success"
OUTCOME_REJECTED = "rejected"
OUTCOME_UNUSED = "unused"


def _provider_accepts_should_stop(provider: BaseSmsProvider) -> bool:
    try:
        params = inspect.signature(provider.get_code).parameters
    except (TypeError, ValueError):
        return False
    if "should_stop" in params:
        return True
    return any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values())


class PhoneLease:
    """一次号码租用的生命周期句柄。"""

    def __init__(
        self,
        verification: "PhoneVerification",
        provider: BaseSmsProvider,
        activation: SmsActivation,
    ):
        self._verification = verification
        self._provider = provider
        self._activation = activation
        self._released = False
        self._reported_success = False
        # 延迟上报型 provider 收到码后等外部确认，没人确认时由 release 补报
        self._awaiting_external_success = False

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------
    @property
    def phone_number(self) -> str:
        return str(self._activation.phone_number or "")

    @property
    def activation_id(self) -> str:
        return str(self._activation.activation_id or "")

    @property
    def country(self) -> str:
        return str(self._activation.country or "")

    @property
    def reused(self) -> bool:
        return bool((self._activation.metadata or {}).get("reused"))

    @property
    def released(self) -> bool:
        return self._released

    @property
    def provider_key(self) -> str:
        return self._verification.provider_key

    @property
    def provider(self) -> BaseSmsProvider:
        return self._provider

    # ------------------------------------------------------------------
    # 等码
    # ------------------------------------------------------------------
    def wait_code(self, *, timeout: int | None = None, should_stop: Callable[[], bool] | None = None) -> str:
        """等待验证码；可重复调用（站点让重发时再等一次）。"""
        if self._released:
            raise RuntimeError("PhoneLease 已释放，无法再等待验证码")

        self._log(f"等待短信验证码... (activation_id={self.activation_id})")
        kwargs: dict[str, Any] = {"timeout": DEFAULT_CODE_TIMEOUT if timeout is None else int(timeout)}
        if should_stop is not None and _provider_accepts_should_stop(self._provider):
            kwargs["should_stop"] = should_stop

        code = self._provider.get_code(self.activation_id, **kwargs)
        if code:
            self._log(f"收到验证码: {code}")
            if getattr(self._provider, "auto_report_success_on_code", True):
                self._report_success()
            else:
                self._awaiting_external_success = True
        else:
            self._log(f"⚠️ 未收到验证码: activation_id={self.activation_id}")
        return code

    # ------------------------------------------------------------------
    # 站点侧反馈
    # ------------------------------------------------------------------
    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        self._provider.set_resend_callback(callback)

    def report_send_succeeded(self) -> None:
        """站点已接受该号码。"""
        self._provider.mark_send_succeeded(self.activation_id)

    def report_send_failed(self, reason: str = "") -> None:
        """站点拒绝该号码（通常随后要 swap）。"""
        # 先清标志再通知 provider：通知失败也不能让 release() 把这次当成功补报
        self._awaiting_external_success = False
        self._provider.mark_send_failed(self.activation_id, reason=reason)

    def report_code_failed(self, reason: str = "") -> None:
        """站点拒绝该验证码（同号重发，可再次 wait_code）。"""
        self._awaiting_external_success = False
        self._provider.mark_code_failed(self.activation_id, reason=reason)

    # ------------------------------------------------------------------
    # 结束
    # ------------------------------------------------------------------
    def release(self, outcome: str = OUTCOME_UNUSED) -> None:
        """归还号码。

        outcome:
          success  —— 验证成功，向 provider 上报（可能触发号码复用计数）
          rejected —— 号码被目标站拒绝，直接作废
          unused   —— 收尾兜底：收到过码但无人上报时补报成功，否则作废
        """
        if self._released:
            return
        try:
            if outcome == OUTCOME_SUCCESS:
                # 站点已经接受了验证码，向 provider 上报失败不该让整次注册失败
                try:
                    self._report_success()
                except Exception as exc:
                    self._log(f"⚠️ 上报号码使用成功失败（不影响注册）: activation_id={self.activation_id}: {exc}")
            elif outcome == OUTCOME_REJECTED:
                self._cancel()
            elif self._awaiting_external_success and not getattr(
                self._provider, "auto_report_success_on_code", True
            ):
                try:
                    self._report_success()
                except Exception:
                    pass
            else:
                self._cancel()
        finally:
            self._released = True
            self._verification._on_lease_released(self)

    def swap(self, *, should_stop: Callable[[], bool] | None = None) -> "PhoneLease":
        """作废当前号码并租一个新的（换号重试）。

        重租要重新排队拿验证窗口锁，取消令牌必须跟着传，否则取消在这里最多卡 25 分钟。
        """
        self.release(OUTCOME_REJECTED)
        return self._verification.acquire(should_stop=should_stop)

    def to_ref(self) -> dict:
        """可持久化的引用，供进程重启后回收遗留号码（不含密钥，也不含号码本身）。"""
        return {
            "provider_key": self.provider_key,
            "activation_id": self.activation_id,
            "country": self.country,
            "platform": self._verification.platform,
            "service": self._verification.service,
        }

    # ------------------------------------------------------------------
    def _report_success(self) -> None:
        if self._reported_success:
            return
        self._provider.report_success(self.activation_id)
        self._reported_success = True
        self._awaiting_external_success = False
        self._log(f"短信验证成功，已标记号码完成使用: activation_id={self.activation_id}")

    def _cancel(self) -> None:
        if self._reported_success:
            return
        try:
            self._provider.cancel(self.activation_id)
            self._log(f"已释放未使用号码: activation_id={self.activation_id}")
        except Exception:
            pass

    def _log(self, message: str) -> None:
        self._verification.log(message)


class PhoneVerification:
    """按需租号的服务对象：一次注册尝试持有一个实例，其下可有多个 lease。"""

    def __init__(
        self,
        provider_key: str,
        config: dict,
        *,
        platform: str = "",
        service: str = "",
        country: str = "",
        log_fn: Callable[[str], None] | None = None,
    ):
        self.provider_key = str(provider_key or "").strip()
        self.config = dict(config or {})
        self.platform = str(platform or "").strip().lower()
        self.service = str(service or "").strip().lower()
        self.country = country
        self.log = log_fn or logger.info
        self._provider: Optional[BaseSmsProvider] = None
        self._lease: Optional[PhoneLease] = None
        self._verify_lock_acquired = False

    @property
    def provider(self) -> BaseSmsProvider:
        if self._provider is None:
            # 走模块属性而非直接绑定，工厂被替换（测试 / 阶段 2 registry 化）时能生效
            self._provider = base_sms.create_sms_provider(self.provider_key, self.config)
        return self._provider

    @property
    def active_lease(self) -> Optional[PhoneLease]:
        return self._lease

    def acquire(self, *, should_stop: Callable[[], bool] | None = None) -> PhoneLease:
        """租一个号码；已持有未释放的 lease 时直接返回它。"""
        if self._lease is not None and not self._lease.released:
            return self._lease

        provider = self.provider
        self._acquire_verify_lock(provider, should_stop=should_stop)
        try:
            activation = self._rent_number(provider)
        except Exception:
            self._release_verify_lock()
            raise

        lease = PhoneLease(self, provider, activation)
        self._lease = lease
        reuse_label = "复用号码" if lease.reused else "新号码"
        self.log(
            f"已成功租到号码({reuse_label}): {lease.phone_number} "
            f"(activation_id={lease.activation_id})"
        )
        return lease

    def close(self) -> None:
        """收尾：释放仍在手上的号码与验证锁。"""
        lease = self._lease
        if lease is not None and not lease.released:
            lease.release(OUTCOME_UNUSED)
        self._release_verify_lock()

    # ------------------------------------------------------------------
    def _rent_number(self, provider: BaseSmsProvider) -> SmsActivation:
        service_code = provider.resolve_service_code(platform=self.platform, service=self.service)

        # 国家优先级：provider 已握着的可复用号码 > provider 自动选出的国家 > 调用方指定。
        # 已经握着可复用号码时不再选国：再查一次"最优国家"不但白等十几秒，
        # 还会因为国家不匹配而错过复用缓存，白白多取一个号。
        provider_country = provider.peek_reusable_country(
            platform=self.platform,
            service=self.service,
        )
        if not provider_country:
            provider_country = provider.select_best_country(
                platform=self.platform,
                service=self.service,
                log_fn=self.log,
            )
        effective_country = provider_country or self.country

        # 只认通用键；厂商前缀键由各 provider 在 from_config 里自己消化
        country_label = effective_country or self.config.get("sms_country") or "default"
        self.log(
            "已进入 add_phone，准备租用手机号: "
            f"provider={self.provider_key} platform={self.platform or 'unknown'} "
            f"service_code={service_code or 'default'} country={country_label}"
        )
        self.log(f"正在从 {self.provider_key} 获取手机号...")

        try:
            return provider.get_number(
                platform=self.platform,
                service=self.service,
                country=effective_country,
            )
        except Exception:
            # provider 自选的国家取号失败时，回退到调用方/配置指定的国家再试一次
            fallback_country = (
                self.country
                or self.config.get("sms_country")
                or provider.fallback_country()
                or ""
            )
            if provider_country and effective_country != fallback_country and fallback_country:
                self.log(
                    f"自动选择的国家({effective_country})获取号码失败，"
                    f"回退到默认国家({fallback_country})..."
                )
                return provider.get_number(
                    platform=self.platform,
                    service=self.service,
                    country=fallback_country,
                )
            raise

    def _acquire_verify_lock(
        self,
        provider: BaseSmsProvider,
        *,
        should_stop: Callable[[], bool] | None = None,
        timeout: float = VERIFY_WINDOW_LOCK_TIMEOUT,
    ) -> None:
        if self._verify_lock_acquired:
            return
        if not provider.requires_exclusive_verification_window:
            return

        deadline = time.time() + max(1.0, float(timeout))
        waited_notice = False
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise ProviderError(
                    f"等待 {self.provider_key} 独占验证窗口超时（{int(timeout)}s）——"
                    "上一次验证可能没有释放锁"
                )
            if _SMS_VERIFY_WINDOW_LOCK.acquire(timeout=min(1.0, remaining)):
                self._verify_lock_acquired = True
                return
            if should_stop is not None and should_stop():
                raise AttemptCancelled("等待短信验证窗口期间任务被取消")
            if not waited_notice:
                waited_notice = True
                self.log(f"[SMS] 其它尝试正占用 {self.provider_key} 的验证窗口，排队等待...")

    def _release_verify_lock(self) -> None:
        if not self._verify_lock_acquired:
            return
        self._verify_lock_acquired = False
        try:
            _SMS_VERIFY_WINDOW_LOCK.release()
        except RuntimeError as exc:
            # RLock 只能由持有线程释放；跨线程收尾时记下来而不是把收尾流程炸掉
            self.log(f"[SMS] 验证窗口锁释放失败: {exc}")

    def _on_lease_released(self, lease: PhoneLease) -> None:
        if self._lease is lease:
            self._lease = None
        self._release_verify_lock()

    # ------------------------------------------------------------------
    @classmethod
    def reclaim(
        cls,
        ref: dict,
        *,
        config: dict | None = None,
        log_fn: Callable[[str], None] | None = None,
    ) -> Optional[PhoneLease]:
        """从持久化引用重建 lease，用于进程重启后退还遗留号码。

        不进入验证窗口（不取验证锁），只为了能调用 release()。
        """
        ref = dict(ref or {})
        provider_key = str(ref.get("provider_key") or "").strip()
        activation_id = str(ref.get("activation_id") or "").strip()
        if not provider_key or not activation_id:
            return None

        settings = dict(config) if config is not None else resolve_sms_settings(provider_key)
        verification = cls(
            provider_key,
            settings,
            platform=str(ref.get("platform") or ""),
            service=str(ref.get("service") or ""),
            country=str(ref.get("country") or ""),
            log_fn=log_fn,
        )
        activation = SmsActivation(
            activation_id=activation_id,
            phone_number=str(ref.get("phone_number") or ""),
            country=str(ref.get("country") or ""),
        )
        lease = PhoneLease(verification, verification.provider, activation)
        verification._lease = lease
        return lease


def resolve_sms_settings(provider_key: str, overrides: dict | None = None) -> dict:
    """从 provider 设置仓储取该 SMS provider 的运行时配置。"""
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    key = str(provider_key or "").strip()
    if not key:
        return dict(overrides or {})
    definition = ProviderDefinitionsRepository().get_by_key("sms", key)
    if not definition:
        return dict(overrides or {})
    return ProviderSettingsRepository().resolve_runtime_settings("sms", key, overrides or {})


def create_phone_verification(
    provider_key: str,
    config: dict,
    *,
    platform: str = "",
    service: str = "",
    country: str = "",
    log_fn: Callable[[str], None] | None = None,
) -> PhoneVerification:
    """构造 PhoneVerification（与旧 create_phone_callbacks 对应的入口）。"""
    return PhoneVerification(
        provider_key,
        config,
        platform=platform,
        service=service,
        country=country,
        log_fn=log_fn,
    )
