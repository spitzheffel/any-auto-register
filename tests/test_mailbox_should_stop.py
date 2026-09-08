"""邮箱等码/等链接轮询必须能被取消令牌中断，而不是跑满 timeout。

build_otp_callback / build_link_callback 只在 accepts_should_stop(...) 为真时才把
should_stop 传下来；任何一个实现漏接，取消任务时邮箱等待就会白跑满 120s。
所以这里用签名断言把"全部实现都接了"钉死，再用真实 provider 验证中断行为。
"""
from __future__ import annotations

import inspect
import time
from unittest.mock import patch

import pytest

from core.base_mailbox import BaseMailbox, FallbackMailbox, MailboxAccount, poll_until
from core.errors import AttemptCancelled
from core.generic_http_mailbox import GenericHttpMailbox
from providers.registry import list_registered


class _StopAfter:
    """第 n 次被询问时开始返回 True 的取消令牌，并记录被询问次数。"""

    def __init__(self, n: int):
        self.n = n
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls >= self.n


def _generic_http_mailbox() -> GenericHttpMailbox:
    # fixed 邮箱模式 + 无 auth 步骤：get_email 不发请求，取信只经过 _execute_step
    return GenericHttpMailbox(
        pipeline_config={"list_emails": {"method": "GET", "path": "/messages"}},
        settings={"api_url": "http://127.0.0.1:9", "email": "u@example.com"},
    )


def _registered_mailbox_classes() -> dict[str, type]:
    classes = dict(list_registered("mailbox"))
    classes["<fallback>"] = FallbackMailbox
    return classes


class TestSignatures:
    def test_every_registered_mailbox_accepts_should_stop(self):
        classes = _registered_mailbox_classes()
        # 少于 15 个说明 registry 没加载全，下面的遍历会变成假阳性
        assert len(classes) >= 15, sorted(classes)

        missing: list[str] = []
        for key, cls in classes.items():
            assert issubclass(cls, BaseMailbox), key
            for method_name in ("wait_for_code", "wait_for_link"):
                params = inspect.signature(getattr(cls, method_name)).parameters
                if "should_stop" not in params:
                    missing.append(f"{key}:{cls.__name__}.{method_name}")
        assert not missing, missing

    def test_base_class_declares_should_stop(self):
        for method in (BaseMailbox.wait_for_code, BaseMailbox.wait_for_link):
            param = inspect.signature(method).parameters["should_stop"]
            assert param.default is None


class TestPollUntil:
    def test_cancel_before_sleep_never_sleeps(self):
        fetch_calls: list[int] = []

        def fetch():
            fetch_calls.append(1)
            return None

        stop = _StopAfter(2)
        with patch("core.base_mailbox.time.sleep") as sleep:
            with pytest.raises(AttemptCancelled, match="任务被取消"):
                poll_until(fetch, timeout=120, interval=3, should_stop=stop)
        assert len(fetch_calls) == 1
        sleep.assert_not_called()

    def test_cancel_after_sleep_is_caught_before_next_fetch(self):
        fetch_calls: list[int] = []

        def fetch():
            fetch_calls.append(1)
            return None

        stop = _StopAfter(3)
        with patch("core.base_mailbox.time.sleep") as sleep:
            with pytest.raises(AttemptCancelled):
                poll_until(fetch, timeout=120, interval=3, should_stop=stop)
        assert len(fetch_calls) == 1
        sleep.assert_called_once_with(3)

    def test_returns_first_truthy_result(self):
        results = iter([None, "", "123456"])
        with patch("core.base_mailbox.time.sleep"):
            assert poll_until(lambda: next(results), timeout=120, interval=3) == "123456"

    def test_times_out_with_message(self):
        with pytest.raises(TimeoutError, match=r"等待验证码超时 \(0\.05s\)"):
            poll_until(lambda: None, timeout=0.05, interval=0.01, timeout_message="等待验证码超时")

    def test_fetch_errors_propagate(self):
        def fetch():
            raise RuntimeError("缺少 Graph token")

        with pytest.raises(RuntimeError, match="Graph token"):
            poll_until(fetch, timeout=120, interval=3)

    def test_without_should_stop_behaves_like_before(self):
        results = iter([None, "ok"])
        with patch("core.base_mailbox.time.sleep") as sleep:
            assert poll_until(lambda: next(results), timeout=120, interval=3, should_stop=None) == "ok"
        sleep.assert_called_once_with(3)


class TestGenericHttpMailboxCancel:
    def test_wait_for_code_cancelled_on_second_check_within_one_second(self):
        mailbox = _generic_http_mailbox()
        account = MailboxAccount(email="u@example.com")
        stop = _StopAfter(2)
        list_calls: list[dict] = []

        def never_any_mail(step):
            list_calls.append(step)
            return {"messages": []}

        started = time.monotonic()
        # 不 mock sleep：如果取消没生效，这里会真睡 3s 再超时，用时间断言把它抓出来
        with patch.object(mailbox, "_execute_step", side_effect=never_any_mail):
            with pytest.raises(AttemptCancelled):
                mailbox.wait_for_code(account, timeout=5, should_stop=stop)
        assert time.monotonic() - started < 1
        assert len(list_calls) == 1
        assert stop.calls == 2

    def test_wait_for_link_cancelled_after_first_sleep(self):
        mailbox = _generic_http_mailbox()
        account = MailboxAccount(email="u@example.com")
        stop = _StopAfter(3)

        with patch.object(mailbox, "_execute_step", return_value={"messages": []}) as execute:
            with patch("core.base_mailbox.time.sleep") as sleep:
                with pytest.raises(AttemptCancelled):
                    mailbox.wait_for_link(account, timeout=120, should_stop=stop)
        assert execute.call_count == 1
        sleep.assert_called_once_with(3)

    def test_without_should_stop_still_times_out(self):
        mailbox = _generic_http_mailbox()
        account = MailboxAccount(email="u@example.com")

        with patch.object(mailbox, "_execute_step", return_value={"messages": []}):
            with patch("core.base_mailbox.time.sleep"):
                with pytest.raises(TimeoutError, match=r"等待验证码超时 \(0\.05s\)"):
                    mailbox.wait_for_code(account, timeout=0.05)


class _RecordingMailbox(BaseMailbox):
    def __init__(self):
        self.received: list[tuple] = []

    def get_email(self, platform: str = "") -> MailboxAccount:
        return MailboxAccount(email="r@example.com", extra={})

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()

    def wait_for_code(self, account, keyword="", timeout=120, before_ids=None,
                      code_pattern=None, should_stop=None) -> str:
        self.received.append(("code", should_stop))
        return "000000"

    def wait_for_link(self, account, keyword="", timeout=120, before_ids=None, should_stop=None) -> str:
        self.received.append(("link", should_stop))
        return "https://example.com/verify"


class TestFallbackMailboxForwarding:
    def test_forwards_same_should_stop_object_to_resolved_provider(self):
        inner = _RecordingMailbox()
        fallback = FallbackMailbox([("inner", inner)])
        account = fallback.get_email()
        stop = _StopAfter(99)

        fallback.wait_for_code(account, should_stop=stop)
        fallback.wait_for_link(account, should_stop=stop)

        assert inner.received == [("code", stop), ("link", stop)]

    def test_omits_should_stop_when_not_given(self):
        inner = _RecordingMailbox()
        fallback = FallbackMailbox([("inner", inner)])
        account = fallback.get_email()

        fallback.wait_for_code(account)

        assert inner.received == [("code", None)]

    def test_cancel_propagates_through_fallback_to_real_provider(self):
        inner = _generic_http_mailbox()
        fallback = FallbackMailbox([("generic", inner)])
        account = fallback.get_email()
        stop = _StopAfter(2)

        with patch.object(inner, "_execute_step", return_value={"messages": []}):
            with pytest.raises(AttemptCancelled):
                fallback.wait_for_code(account, timeout=5, should_stop=stop)
        assert stop.calls == 2
