"""邮箱渠道可选生命周期 hook。"""
from __future__ import annotations

from types import SimpleNamespace

from application.tasks import _notify_mailbox_from_platform
from core.base_mailbox import BaseMailbox, FallbackMailbox, MailboxAccount, notify_mailbox_outcome


class RecordingMailbox(BaseMailbox):
    def __init__(self, email: str = "a@example.com"):
        self.email = email
        self.events: list[tuple] = []

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email=self.email, extra={})

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        return "123456"

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()

    def report_success(self, account: MailboxAccount) -> None:
        self.events.append(("success", account.email))

    def release(self, account: MailboxAccount, reason: str = "") -> None:
        self.events.append(("release", account.email, reason))


class FailingMailbox(RecordingMailbox):
    def get_email(self) -> MailboxAccount:
        raise RuntimeError("创建失败")


class ExplodingMailbox(RecordingMailbox):
    def report_success(self, account: MailboxAccount) -> None:
        raise RuntimeError("hook boom")

    def release(self, account: MailboxAccount, reason: str = "") -> None:
        raise RuntimeError("hook boom")


class TestBaseMailboxHooks:
    def test_default_hooks_are_noop(self):
        mailbox = RecordingMailbox()
        account = mailbox.get_email()
        BaseMailbox.report_success(mailbox, account)
        BaseMailbox.release(mailbox, account, reason="fail")
        assert mailbox.events == []


class TestFallbackMailboxHooks:
    def test_forwards_to_winning_provider(self):
        first = FailingMailbox("one@example.com")
        second = RecordingMailbox("two@example.com")
        fallback = FallbackMailbox([("first", first), ("second", second)])

        account = fallback.get_email()
        fallback.report_success(account)
        fallback.release(account, reason="timeout")

        assert first.events == []
        assert second.events == [
            ("success", "two@example.com"),
            ("release", "two@example.com", "timeout"),
        ]


class TestNotifyMailboxOutcome:
    def test_success_and_failure(self):
        mailbox = RecordingMailbox()
        account = mailbox.get_email()
        identity = SimpleNamespace(mailbox_account=account)

        notify_mailbox_outcome(mailbox, identity, ok=True)
        notify_mailbox_outcome(mailbox, identity, ok=False, reason="otp timeout")

        assert mailbox.events == [
            ("success", "a@example.com"),
            ("release", "a@example.com", "otp timeout"),
        ]

    def test_skips_without_mailbox_or_account(self):
        mailbox = RecordingMailbox()
        identity = SimpleNamespace(mailbox_account=None)
        notify_mailbox_outcome(None, identity, ok=True)
        notify_mailbox_outcome(mailbox, None, ok=False, reason="x")
        notify_mailbox_outcome(mailbox, identity, ok=False, reason="x")
        assert mailbox.events == []

    def test_hook_exception_does_not_raise(self):
        mailbox = ExplodingMailbox()
        account = mailbox.get_email()
        identity = SimpleNamespace(mailbox_account=account)
        logs: list[str] = []
        notify_mailbox_outcome(mailbox, identity, ok=True, log_fn=logs.append)
        notify_mailbox_outcome(mailbox, identity, ok=False, reason="fail", log_fn=logs.append)
        assert len(logs) == 2
        assert all("邮箱生命周期 hook 失败" in item for item in logs)


class TestNotifyFromPlatform:
    def test_reads_last_identity(self):
        mailbox = RecordingMailbox()
        account = mailbox.get_email()
        platform = SimpleNamespace(
            mailbox=mailbox,
            _last_identity=SimpleNamespace(mailbox_account=account),
        )
        _notify_mailbox_from_platform(platform, ok=True)
        _notify_mailbox_from_platform(platform, ok=False, reason="token missing")
        assert mailbox.events == [
            ("success", "a@example.com"),
            ("release", "a@example.com", "token missing"),
        ]

    def test_oauth_path_without_mailbox_account_is_noop(self):
        mailbox = RecordingMailbox()
        platform = SimpleNamespace(
            mailbox=mailbox,
            _last_identity=SimpleNamespace(mailbox_account=None),
        )
        _notify_mailbox_from_platform(platform, ok=True)
        assert mailbox.events == []
