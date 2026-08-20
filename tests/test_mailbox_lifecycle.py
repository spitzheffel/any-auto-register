"""邮箱渠道可选生命周期 hook。"""
from __future__ import annotations

from types import SimpleNamespace

from application.tasks import _notify_mailbox_from_platform
from core.base_identity import MailboxIdentityProvider
from core.base_mailbox import BaseMailbox, FallbackMailbox, MailboxAccount, notify_mailbox_outcome


class RecordingMailbox(BaseMailbox):
    def __init__(self, email: str = "a@example.com"):
        self.email = email
        self.events: list[tuple] = []

    def get_email(self, platform: str = "") -> MailboxAccount:
        self.events.append(("get_email", platform))
        return MailboxAccount(email=self.email, extra={})

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        return "123456"

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()

    def report_success(self, account: MailboxAccount, platform: str = "") -> None:
        self.events.append(("success", account.email, platform))

    def release(self, account: MailboxAccount, reason: str = "", platform: str = "") -> None:
        self.events.append(("release", account.email, reason, platform))


class FailingMailbox(RecordingMailbox):
    def get_email(self, platform: str = "") -> MailboxAccount:
        self.events.append(("get_email", platform))
        raise RuntimeError("创建失败")


class ExplodingMailbox(RecordingMailbox):
    def report_success(self, account: MailboxAccount, platform: str = "") -> None:
        raise RuntimeError("hook boom")

    def release(self, account: MailboxAccount, reason: str = "", platform: str = "") -> None:
        raise RuntimeError("hook boom")


class TestBaseMailboxHooks:
    def test_default_hooks_are_noop(self):
        mailbox = RecordingMailbox()
        account = mailbox.get_email()
        BaseMailbox.report_success(mailbox, account)
        BaseMailbox.release(mailbox, account, reason="fail")
        assert mailbox.events == [("get_email", "")]


class TestFallbackMailboxHooks:
    def test_forwards_to_winning_provider(self):
        first = FailingMailbox("one@example.com")
        second = RecordingMailbox("two@example.com")
        fallback = FallbackMailbox([("first", first), ("second", second)])

        account = fallback.get_email(platform="trae")
        fallback.report_success(account, platform="trae")
        fallback.release(account, reason="timeout", platform="trae")

        assert first.events == [("get_email", "trae")]
        assert second.events == [
            ("get_email", "trae"),
            ("success", "two@example.com", "trae"),
            ("release", "two@example.com", "timeout", "trae"),
        ]


class TestNotifyMailboxOutcome:
    def test_success_and_failure(self):
        mailbox = RecordingMailbox()
        account = mailbox.get_email()
        identity = SimpleNamespace(mailbox_account=account)

        notify_mailbox_outcome(mailbox, identity, ok=True, platform="cursor")
        notify_mailbox_outcome(mailbox, identity, ok=False, reason="otp timeout", platform="cursor")

        assert mailbox.events == [
            ("get_email", ""),
            ("success", "a@example.com", "cursor"),
            ("release", "a@example.com", "otp timeout", "cursor"),
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
            name="trae",
            mailbox=mailbox,
            _last_identity=SimpleNamespace(mailbox_account=account),
        )
        _notify_mailbox_from_platform(platform, ok=True)
        _notify_mailbox_from_platform(platform, ok=False, reason="token missing")
        assert mailbox.events == [
            ("get_email", ""),
            ("success", "a@example.com", "trae"),
            ("release", "a@example.com", "token missing", "trae"),
        ]

    def test_oauth_path_without_mailbox_account_is_noop(self):
        mailbox = RecordingMailbox()
        platform = SimpleNamespace(
            name="cursor",
            mailbox=mailbox,
            _last_identity=SimpleNamespace(mailbox_account=None),
        )
        _notify_mailbox_from_platform(platform, ok=True)
        assert mailbox.events == []


class TestMailboxIdentityPlatform:
    def test_resolve_passes_platform_from_extra(self):
        mailbox = RecordingMailbox()
        identity = MailboxIdentityProvider(mailbox=mailbox, extra={"platform": "trae"}).resolve()
        assert identity.email == "a@example.com"
        assert mailbox.events == [("get_email", "trae")]
