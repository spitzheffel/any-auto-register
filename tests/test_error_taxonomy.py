"""失败分类与取消令牌。"""
from __future__ import annotations

import threading

import pytest

from core.cancellation import CancellationToken
from core.errors import (
    AttemptCancelled,
    ConfigError,
    ProviderError,
    ResourceRejectedError,
    RiskControlError,
    SiteChangedError,
    TransientError,
    classify,
    is_fatal,
    is_retryable,
)


class TestClassification:
    @pytest.mark.parametrize(
        "exc,category,retryable,fatal",
        [
            (TransientError("x"), "transient", True, False),
            (ProviderError("x"), "provider", True, False),
            (ResourceRejectedError("x"), "resource_rejected", True, False),
            (RiskControlError("x"), "risk_control", True, False),
            (SiteChangedError("x"), "site_changed", False, True),
            (ConfigError("x"), "config", False, True),
        ],
    )
    def test_category_and_policy(self, exc, category, retryable, fatal):
        assert classify(exc) == category
        assert is_retryable(exc) is retryable
        assert is_fatal(exc) is fatal

    def test_unclassified_exception_is_conservative(self):
        exc = RuntimeError("boom")
        assert classify(exc) == "unknown"
        # 未分类的既不补投也不停批，与历史行为一致
        assert is_retryable(exc) is False
        assert is_fatal(exc) is False

    def test_risk_control_is_a_resource_rejection(self):
        assert isinstance(RiskControlError("x"), ResourceRejectedError)

    def test_cancellation_is_not_a_failure(self):
        assert classify(AttemptCancelled()) == "cancelled"
        # 继承 BaseException，避免被业务代码的 except Exception 吞掉
        assert not isinstance(AttemptCancelled(), Exception)


class TestPlatformExceptionsAreClassified:
    def test_phone_rejection_is_resource_rejected(self):
        from platforms.chatgpt.browser_register import PhoneRejectedError

        exc = PhoneRejectedError("手机号被拒绝")
        assert isinstance(exc, ResourceRejectedError)
        assert is_retryable(exc) is True

    def test_registration_config_errors_are_fatal(self):
        from core.registration.errors import (
            BrowserReuseRequiredError,
            CaptchaConfigurationError,
            IdentityResolutionError,
            OtpTimeoutError,
            RegistrationUnsupportedError,
        )

        for exc_cls in (
            CaptchaConfigurationError,
            IdentityResolutionError,
            BrowserReuseRequiredError,
            RegistrationUnsupportedError,
        ):
            assert is_fatal(exc_cls("x")) is True, exc_cls

        assert is_retryable(OtpTimeoutError("x")) is True

    def test_missing_sms_api_key_is_config_error(self):
        from providers.sms.herosms import HeroSmsProvider

        with pytest.raises(ConfigError):
            HeroSmsProvider.from_config({})


class TestCancellationToken:
    def test_local_trip(self):
        token = CancellationToken()
        assert token.is_cancelled() is False
        token.trip("停批")
        assert token.is_cancelled() is True
        assert token.reason == "停批"

    def test_probe_is_throttled(self):
        calls = []

        def probe():
            calls.append(1)
            return False

        token = CancellationToken(probe, throttle_seconds=60)
        for _ in range(5):
            token.is_cancelled()
        assert len(calls) == 1

    def test_probe_trip_sticks(self):
        state = {"cancelled": True}
        token = CancellationToken(lambda: state["cancelled"], throttle_seconds=0)
        assert token.is_cancelled() is True
        state["cancelled"] = False
        # 已经触发过就不再回退
        assert token.is_cancelled() is True

    def test_raise_if_cancelled(self):
        token = CancellationToken()
        token.raise_if_cancelled()
        token.trip("stop")
        with pytest.raises(AttemptCancelled):
            token.raise_if_cancelled()

    def test_exposes_event_for_blocking_waiters(self):
        token = CancellationToken()
        assert isinstance(token.event, threading.Event)
        token.trip()
        assert token.event.is_set() is True

    def test_is_usable_as_should_stop_predicate(self):
        token = CancellationToken()
        should_stop = token
        assert should_stop() is False
        token.trip()
        assert should_stop() is True
