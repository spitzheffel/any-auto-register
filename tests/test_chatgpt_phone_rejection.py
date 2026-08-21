"""号码级拒绝的识别与换号重试回归测试。"""
from __future__ import annotations

import pytest

browser_register = pytest.importorskip("platforms.chatgpt.browser_register")

PhoneRejectedError = browser_register.PhoneRejectedError
_is_phone_rejected_error = browser_register._is_phone_rejected_error


class TestIsPhoneRejectedError:
    def test_recently_used_is_phone_level(self):
        assert _is_phone_rejected_error("This phone number was recently used. Please try again later.")

    def test_already_in_use_is_phone_level(self):
        assert _is_phone_rejected_error("That phone number is already in use.")

    def test_invalid_phone_number_is_phone_level(self):
        assert _is_phone_rejected_error("Invalid phone number")

    def test_invalid_otp_code_is_not_phone_level(self):
        assert not _is_phone_rejected_error("Invalid OTP code")

    def test_expired_code_is_not_phone_level(self):
        assert not _is_phone_rejected_error("The code you entered has expired")

    def test_empty_is_not_phone_level(self):
        assert not _is_phone_rejected_error("")


class _FakePhoneCallback:
    def __init__(self):
        self.events: list[tuple[str, str]] = []
        self.phase = "need_code"
        self.activation = object()
        self.completed = True

    def mark_send_failed(self, reason: str = "") -> None:
        self.events.append(("send_failed", reason))

    def mark_code_failed(self, reason: str = "") -> None:
        self.events.append(("code_failed", reason))

    def cleanup(self) -> None:
        self.events.append(("cleanup", ""))


class TestAddPhoneRetry:
    def test_phone_rejection_triggers_new_number(self, monkeypatch):
        phone_callback = _FakePhoneCallback()
        attempts = []

        def fake_attempt(page, callback, **kwargs):
            attempts.append(len(attempts))
            if len(attempts) == 1:
                raise PhoneRejectedError("手机号被拒绝: This phone number was recently used.")
            return {"page_type": "oauth_callback"}

        monkeypatch.setattr(browser_register, "_do_add_phone_attempt", fake_attempt)

        result = browser_register._handle_add_phone_challenge(
            page=None,
            phone_callback=phone_callback,
            device_id="dev",
            user_agent="ua",
            log=lambda message: None,
        )

        assert result == {"page_type": "oauth_callback"}
        assert len(attempts) == 2
        # 换号前必须作废旧号码并复位到 need_number，否则会再次拿到同一个号
        assert ("cleanup", "") in phone_callback.events
        assert phone_callback.phase == "need_number"
        assert phone_callback.activation is None
        assert phone_callback.completed is False

    def test_non_retryable_error_propagates(self, monkeypatch):
        phone_callback = _FakePhoneCallback()

        def fake_attempt(page, callback, **kwargs):
            raise RuntimeError("未找到手机号输入框")

        monkeypatch.setattr(browser_register, "_do_add_phone_attempt", fake_attempt)

        with pytest.raises(RuntimeError, match="未找到手机号输入框"):
            browser_register._handle_add_phone_challenge(
                page=None,
                phone_callback=phone_callback,
                device_id="dev",
                user_agent="ua",
                log=lambda message: None,
            )

    def test_gives_up_after_max_attempts(self, monkeypatch):
        phone_callback = _FakePhoneCallback()

        def fake_attempt(page, callback, **kwargs):
            raise PhoneRejectedError("手机号被拒绝: recently used")

        monkeypatch.setattr(browser_register, "_do_add_phone_attempt", fake_attempt)

        with pytest.raises(PhoneRejectedError):
            browser_register._handle_add_phone_challenge(
                page=None,
                phone_callback=phone_callback,
                device_id="dev",
                user_agent="ua",
                log=lambda message: None,
                max_phone_attempts=2,
            )

        assert phone_callback.events.count(("cleanup", "")) == 2
