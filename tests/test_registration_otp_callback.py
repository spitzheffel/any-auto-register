"""build_otp_callback 不得在同一次注册里重复返回同一封邮件的旧验证码。"""
from __future__ import annotations

from types import SimpleNamespace

from core.base_platform import RegisterConfig
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
