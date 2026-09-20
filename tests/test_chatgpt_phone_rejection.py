"""号码级拒绝的识别与换号重试。

注册主流程的换号重试现在由 Step 引擎的 RetryPolicy 负责
（见 tests/test_chatgpt_steps.py::TestPhoneSwap）；这里盯的是文案判定，
以及 codex_oauth 里那条不在引擎内的 add_phone 路径。
"""
from __future__ import annotations

import pytest

from core.errors import ConfigError, SiteChangedError

phone_mod = pytest.importorskip("platforms.chatgpt.phone")

PhoneCodeTimeout = phone_mod.PhoneCodeTimeout
PhoneRejectedError = phone_mod.PhoneRejectedError
_is_phone_rejected_error = phone_mod._is_phone_rejected_error


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


class _FakeLease:
    """记录 release / swap 的 PhoneLease 替身。"""

    def __init__(self, phone, index: int):
        self.phone = phone
        self.index = index
        self.released_as: str | None = None

    @property
    def phone_number(self) -> str:
        return f"+1555000{self.index}"

    def release(self, outcome: str = "unused") -> None:
        self.released_as = outcome
        self.phone.events.append(("release", outcome, self.index))

    def swap(self):
        self.release("rejected")
        return self.phone.acquire()


class _FakePhoneVerification:
    def __init__(self):
        self.events: list[tuple] = []
        self.leases: list[_FakeLease] = []

    def acquire(self) -> _FakeLease:
        lease = _FakeLease(self, len(self.leases) + 1)
        self.leases.append(lease)
        self.events.append(("acquire", lease.index))
        return lease

    def close(self) -> None:
        self.events.append(("close",))


class TestAddPhoneWithRetries:
    """codex_oauth 里的 add_phone：那段 OAuth 状态机不在 Step 引擎内，自带循环。"""

    def test_phone_rejection_triggers_new_number(self, monkeypatch):
        phone = _FakePhoneVerification()
        attempts = []

        def fake_attempt(page, lease, **kwargs):
            attempts.append(lease)
            if len(attempts) == 1:
                raise PhoneRejectedError("手机号被拒绝: This phone number was recently used.")
            return {"page_type": "oauth_callback"}

        monkeypatch.setattr(phone_mod, "_do_add_phone_attempt", fake_attempt)

        result = phone_mod.run_add_phone_with_retries(None, phone, log=lambda message: None)

        assert result == {"page_type": "oauth_callback"}
        assert len(attempts) == 2
        # 换号必须走 swap：旧号码以 rejected 归还，再租一个新的
        assert attempts[0] is not attempts[1]
        assert attempts[0].released_as == "rejected"
        assert phone.events == [("acquire", 1), ("release", "rejected", 1), ("acquire", 2)]

    def test_code_timeout_also_triggers_new_number(self, monkeypatch):
        phone = _FakePhoneVerification()
        attempts = []

        def fake_attempt(page, lease, **kwargs):
            attempts.append(lease)
            if len(attempts) == 1:
                raise PhoneCodeTimeout("未收到短信验证码")
            return {"page_type": "oauth_callback"}

        monkeypatch.setattr(phone_mod, "_do_add_phone_attempt", fake_attempt)
        phone_mod.run_add_phone_with_retries(None, phone, log=lambda message: None)
        assert len(attempts) == 2

    def test_site_layout_error_propagates_without_burning_numbers(self, monkeypatch):
        # 找不到输入框是站点改版，换号一样没用，不该白烧号码
        phone = _FakePhoneVerification()

        def fake_attempt(page, lease, **kwargs):
            raise SiteChangedError("未找到手机号输入框")

        monkeypatch.setattr(phone_mod, "_do_add_phone_attempt", fake_attempt)

        with pytest.raises(SiteChangedError, match="未找到手机号输入框"):
            phone_mod.run_add_phone_with_retries(None, phone, log=lambda message: None)

        assert [event for event in phone.events if event[0] == "acquire"] == [("acquire", 1)]

    def test_gives_up_after_max_attempts(self, monkeypatch):
        phone = _FakePhoneVerification()

        def fake_attempt(page, lease, **kwargs):
            raise PhoneRejectedError("手机号被拒绝: recently used")

        monkeypatch.setattr(phone_mod, "_do_add_phone_attempt", fake_attempt)

        with pytest.raises(PhoneRejectedError):
            phone_mod.run_add_phone_with_retries(
                None, phone, log=lambda message: None, max_phone_attempts=2
            )

        # 两次尝试 => 两次租号，其中第一个号在换号时被作废
        assert [event for event in phone.events if event[0] == "acquire"] == [("acquire", 1), ("acquire", 2)]
        assert phone.leases[0].released_as == "rejected"

    def test_missing_phone_channel_is_a_config_error(self):
        # 没配接码渠道是配置问题：致命、不重试，直接停批
        with pytest.raises(ConfigError, match="未配置接码渠道"):
            phone_mod.run_add_phone_with_retries(None, None, log=lambda message: None)
