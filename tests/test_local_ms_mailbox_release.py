"""本地微软邮箱池 release hook。"""
from __future__ import annotations

from pathlib import Path

from core.local_ms_mailbox import LocalMicrosoftMailboxPool


def _pool(tmp_path: Path, *, allow_reuse: bool = False) -> LocalMicrosoftMailboxPool:
    return LocalMicrosoftMailboxPool(
        pool_text="one@outlook.com----pass1\ntwo@outlook.com----pass2",
        state_file=str(tmp_path / "state.json"),
        allow_reuse=allow_reuse,
    )


def test_release_returns_mailbox_to_pool(tmp_path):
    pool = _pool(tmp_path)
    first = pool.get_email()
    assert first.email == "one@outlook.com"
    second = pool.get_email()
    assert second.email == "two@outlook.com"

    pool.release(first, reason="token missing")
    reused = pool.get_email()
    assert reused.email == "one@outlook.com"


def test_release_is_noop_when_reuse_allowed(tmp_path):
    pool = _pool(tmp_path, allow_reuse=True)
    first = pool.get_email()
    pool.release(first, reason="fail")
    again = pool.get_email()
    assert again.email == first.email


def test_release_missing_key_does_not_raise(tmp_path):
    pool = _pool(tmp_path)
    pool.get_email()
    from core.base_mailbox import MailboxAccount

    pool.release(MailboxAccount(email="unknown@outlook.com", account_id="unknown@outlook.com"))
