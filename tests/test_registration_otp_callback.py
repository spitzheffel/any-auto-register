"""build_otp_callback 不得在同一次注册里重复返回同一封邮件的旧验证码，
且要把取消令牌透传到接受它的邮箱实现。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.base_platform import RegisterConfig
from core.errors import AttemptCancelled
from core.generic_http_mailbox import GenericHttpMailbox
from core.registration import RegistrationContext
from core.registration.helpers import build_otp_callback


class _FakeMailbox:
    """按到达顺序投递邮件，wait_for_code 只返回未在 before_ids 中的那封。"""

    def __init__(self, mails: list[tuple[str, str]]):
        self._mails = mails
        self._delivered = 0
        self.before_ids_seen: list[set] = []

    def deliver_next(self) -> None:
        self._delivered = min(self._delivered + 1, len(self._mails))

    def wait_for_code(self, account, keyword="", timeout=120, before_ids=None, code_pattern=None):
        seen = set(before_ids or [])
        self.before_ids_seen.append(seen)
        for mail_id, code in self._mails[: self._delivered]:
            if mail_id not in seen:
                return code
        return ""

    def get_current_ids(self, account) -> set:
        return {mail_id for mail_id, _ in self._mails[: self._delivered]}


def _make_ctx(mailbox) -> RegistrationContext:
    return RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(mailbox=mailbox),
        identity=SimpleNamespace(
            email="user@example.com",
            mailbox_account=SimpleNamespace(email="user@example.com", account_id="acct"),
            before_ids={"old-1"},
        ),
        config=RegisterConfig(executor_type="headless", extra={}),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )


def test_second_request_skips_already_consumed_mail():
    mailbox = _FakeMailbox([("mail-1", "111111"), ("mail-2", "222222")])
    otp_cb = build_otp_callback(_make_ctx(mailbox))

    mailbox.deliver_next()
    assert otp_cb() == "111111"

    mailbox.deliver_next()
    assert otp_cb() == "222222"


def test_initial_before_ids_snapshot_is_preserved():
    mailbox = _FakeMailbox([("old-1", "999999"), ("mail-1", "111111")])
    mailbox.deliver_next()
    mailbox.deliver_next()

    otp_cb = build_otp_callback(_make_ctx(mailbox))

    assert otp_cb() == "111111"
    assert mailbox.before_ids_seen[0] == {"old-1"}


def test_failed_lookup_does_not_grow_seen_set():
    mailbox = _FakeMailbox([("mail-1", "111111")])
    otp_cb = build_otp_callback(_make_ctx(mailbox))

    assert otp_cb() == ""

    mailbox.deliver_next()
    assert otp_cb() == "111111"


def test_legacy_mailbox_without_should_stop_is_still_callable_with_token():
    # 第三方/旧实现没接 should_stop 时，回调不能把它硬塞进去导致 TypeError
    mailbox = _FakeMailbox([("mail-1", "111111")])
    mailbox.deliver_next()
    otp_cb = build_otp_callback(_make_ctx(mailbox))

    assert otp_cb(should_stop=lambda: True) == "111111"


def test_should_stop_reaches_real_provider_and_cancel_propagates():
    mailbox = GenericHttpMailbox(
        pipeline_config={"list_emails": {"method": "GET", "path": "/messages"}},
        settings={"api_url": "http://127.0.0.1:9", "email": "user@example.com"},
    )
    otp_cb = build_otp_callback(_make_ctx(mailbox), timeout=5)
    checks: list[int] = []

    def should_stop() -> bool:
        checks.append(1)
        return len(checks) >= 2

    with patch.object(mailbox, "_execute_step", return_value={"messages": []}) as execute:
        with pytest.raises(AttemptCancelled):
            otp_cb(should_stop=should_stop)
    # 取信一次、第二次询问令牌即中断，没有跑到 5s 超时
    assert execute.call_count == 1
    assert len(checks) == 2
