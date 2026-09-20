"""StepContext 的运行时原语：可取消等待与 should_stop 注入。

步骤里一律用这几个方法，不要裸 time.sleep / while True——引擎只在步骤之间
检查取消令牌，步骤内部的长等待必须自己响应，否则一次取消要等到当前步骤跑完。
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from core.cancellation import CancellationToken
from core.errors import AttemptCancelled, TransientError
from core.registration.steps import (
    Flow,
    RetryPolicy,
    Step,
    StepContext,
    StepContractError,
    StepResult,
    StepRunner,
)


def make_sc(*, page=None, cancel=None):
    logs = []

    def log_fn(message, level="info", event_type="log", detail=None):
        logs.append(message)

    ctx = SimpleNamespace(
        platform_name="demo",
        platform_display_name="Demo",
        platform=SimpleNamespace(),
        log=log_fn,
    )
    sc = StepContext(ctx, page=page, cancel=cancel, attempt_id="attempt-1")
    sc.logs = logs
    return sc


class FakePage:
    def __init__(self, url="https://example.com/start", elements=None):
        self.url = url
        self.elements = dict(elements or {})

    def query_selector(self, selector):
        return self.elements.get(selector)


class VisibleElement:
    def is_visible(self):
        return True


class HiddenElement:
    def is_visible(self):
        return False


class TestSleep:
    def test_sleep_runs_to_completion_without_a_token(self):
        started = time.time()
        make_sc().sleep(0.2)
        assert 0.15 <= time.time() - started < 2

    def test_sleep_returns_immediately_for_a_tripped_token(self):
        token = CancellationToken()
        token.trip("用户取消")
        started = time.time()
        with pytest.raises(AttemptCancelled, match="用户取消"):
            make_sc(cancel=token).sleep(30)
        assert time.time() - started < 1

    def test_sleep_is_interrupted_midway(self):
        # 真正要保的行为：令牌在睡到一半时翻，线程不该睡满
        token = CancellationToken()
        timer = threading.Timer(0.2, token.trip, args=("中途取消",))
        timer.start()
        started = time.time()
        try:
            with pytest.raises(AttemptCancelled):
                make_sc(cancel=token).sleep(30)
        finally:
            timer.cancel()
        assert time.time() - started < 3


class TestPoll:
    def test_poll_returns_the_first_truthy_value(self):
        calls = {"n": 0}

        def predicate():
            calls["n"] += 1
            return "ready" if calls["n"] >= 3 else None

        assert make_sc().poll(predicate, timeout=5, interval=0.01) == "ready"
        assert calls["n"] == 3

    def test_poll_checks_once_before_sleeping(self):
        started = time.time()
        assert make_sc().poll(lambda: "now", timeout=30, interval=10) == "now"
        assert time.time() - started < 1

    def test_poll_returns_none_on_timeout(self):
        assert make_sc().poll(lambda: None, timeout=0.05, interval=0.01) is None

    def test_poll_propagates_predicate_errors(self):
        # 不吞异常：吞了会把"选择器写错了"伪装成"超时"
        def boom():
            raise ValueError("选择器非法")

        with pytest.raises(ValueError, match="选择器非法"):
            make_sc().poll(boom, timeout=1)

    def test_poll_raises_on_cancellation(self):
        token = CancellationToken()
        token.trip("用户取消")
        with pytest.raises(AttemptCancelled):
            make_sc(cancel=token).poll(lambda: None, timeout=30)


class TestWaitForSelector:
    def test_returns_the_matched_selector(self):
        page = FakePage(elements={"b": VisibleElement()})
        assert make_sc(page=page).wait_for_selector(["a", "b"], timeout=1, interval=0.01) == "b"

    def test_visible_false_accepts_hidden_nodes(self):
        page = FakePage(elements={"a": HiddenElement()})
        sc = make_sc(page=page)
        assert sc.wait_for_selector(["a"], timeout=0.05) is None
        assert sc.wait_for_selector(["a"], timeout=0.05, visible=False) == "a"

    def test_accepts_a_bare_string(self):
        page = FakePage(elements={"a": VisibleElement()})
        assert make_sc(page=page).wait_for_selector("a", timeout=1) == "a"

    def test_can_target_another_page(self):
        # 辅助浏览器的页面不是 sc.page，要能显式指定
        main = FakePage()
        other = FakePage(elements={"a": VisibleElement()})
        sc = make_sc(page=main)
        assert sc.wait_for_selector(["a"], timeout=0.05) is None
        assert sc.wait_for_selector(["a"], timeout=1, page=other) == "a"


class TestWaitForUrl:
    def test_true_once_the_substring_appears(self):
        page = FakePage(url="https://example.com/start")
        sc = make_sc(page=page)

        def flip():
            page.url = "https://example.com/done"

        threading.Timer(0.1, flip).start()
        assert sc.wait_for_url("/done", timeout=5) is True

    def test_false_on_timeout(self):
        assert make_sc(page=FakePage()).wait_for_url("/done", timeout=0.05) is False


class TestCallWithCancel:
    def test_injects_for_explicit_keyword(self):
        seen = {}

        def callback(*, should_stop=None):
            seen["should_stop"] = should_stop
            return "code"

        sc = make_sc(cancel=CancellationToken())
        assert sc.call_with_cancel(callback) == "code"
        assert seen["should_stop"] is sc.cancel

    def test_injects_for_var_keyword(self):
        seen = {}

        def callback(**kwargs):
            seen.update(kwargs)
            return "code"

        sc = make_sc()
        sc.call_with_cancel(callback)
        assert seen["should_stop"] is sc.cancel

    def test_skips_callbacks_that_do_not_accept_it(self):
        # 老的 mailbox 实现不接 should_stop，硬塞会 TypeError
        assert make_sc().call_with_cancel(lambda: "code") == "code"

    def test_does_not_override_an_explicit_argument(self):
        seen = {}

        def callback(*, should_stop=None):
            seen["should_stop"] = should_stop

        sentinel = object()
        make_sc().call_with_cancel(callback, should_stop=sentinel)
        assert seen["should_stop"] is sentinel

    def test_forwards_positional_and_keyword_arguments(self):
        def callback(a, b, *, c, should_stop=None):
            return (a, b, c, should_stop is not None)

        assert make_sc().call_with_cancel(callback, 1, 2, c=3) == (1, 2, 3, True)


class _Failing(Step):
    name = "Failing"
    retry = RetryPolicy(max_attempts=3, backoff_seconds=30)

    def __init__(self):
        self.calls = 0

    def run(self, sc):
        self.calls += 1
        raise TransientError("暂时失败")


class TestRunnerBackoffIsCancellable:
    def test_cancel_during_retry_backoff_stops_immediately(self):
        # 重试间隔 30s：取消时不该等它睡完
        token = CancellationToken()
        token.trip("用户取消")
        step = _Failing()
        sc = make_sc(cancel=token)
        started = time.time()
        with pytest.raises(AttemptCancelled):
            StepRunner(Flow("f", [step])).run(sc)
        assert time.time() - started < 2
        assert step.calls <= 1


# ---------------------------------------------------------------------------
# 辅助浏览器
# ---------------------------------------------------------------------------

class CapturePage(FakePage):
    def __init__(self, url="https://aux.example/start"):
        super().__init__(url=url)
        self.shots = []

    def screenshot(self, path):
        self.shots.append(path)
        with open(path, "wb") as handle:
            handle.write(b"png")

    def content(self):
        return "<html><body>aux</body></html>"

    def on(self, event, handler):
        pass


class FakeSession:
    def __init__(self, engine="fake-engine"):
        self.engine = engine
        self.closed = False
        self.pages = []

    def new_page(self):
        page = CapturePage()
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class FakeLaunchSpec:
    def __init__(self):
        self.sessions = []

    def start(self):
        session = FakeSession()
        self.sessions.append(session)
        return session


def make_aux_sc(*, cancel=None):
    sc = make_sc(page=FakePage(), cancel=cancel)
    sc.launch_spec = FakeLaunchSpec()
    return sc


class TestAuxBrowser:
    def test_open_registers_handle_and_page(self):
        sc = make_aux_sc()
        page = sc.open_aux_browser("codex_oauth")
        assert sc.aux_pages["codex_oauth"] is page
        assert sc.handles["aux_browser:codex_oauth"] is sc.launch_spec.sessions[0]
        assert any("aux_browser_opened" in str(line) or "辅助浏览器" in str(line) for line in sc.logs)

    def test_open_without_a_launch_spec_is_a_contract_error(self):
        # 单测 / 脚本直调没有主会话时，明确报错比开出一个野浏览器好
        sc = make_sc()
        with pytest.raises(StepContractError, match="辅助浏览器"):
            sc.open_aux_browser("x")

    def test_open_is_idempotent_and_does_not_leak_on_retry(self):
        # 步骤重试会再次进 run()，同名再开必须先把上一轮关掉
        sc = make_aux_sc()
        sc.open_aux_browser("x")
        sc.open_aux_browser("x")
        first, second = sc.launch_spec.sessions
        assert first.closed is True
        assert second.closed is False
        assert len(sc.aux_pages) == 1

    def test_open_respects_cancellation(self):
        token = CancellationToken()
        token.trip("用户取消")
        sc = make_aux_sc(cancel=token)
        with pytest.raises(AttemptCancelled):
            sc.open_aux_browser("x")
        assert sc.launch_spec.sessions == []

    def test_close_is_idempotent(self):
        sc = make_aux_sc()
        sc.open_aux_browser("x")
        sc.close_aux_browser("x")
        sc.close_aux_browser("x")
        assert sc.launch_spec.sessions[0].closed is True
        assert sc.aux_pages == {}

    def test_close_swallows_session_errors(self):
        sc = make_aux_sc()
        sc.open_aux_browser("x")

        def boom():
            raise RuntimeError("browser already gone")

        sc.handles["aux_browser:x"].close = boom
        sc.close_aux_browser("x")
        assert "aux_browser:x" not in sc.handles

    def test_close_all_closes_every_session(self):
        sc = make_aux_sc()
        sc.open_aux_browser("a")
        sc.open_aux_browser("b")
        sc.close_aux_browsers()
        assert all(s.closed for s in sc.launch_spec.sessions)
        assert sc.aux_pages == {}
        assert not [k for k in sc.handles if str(k).startswith("aux_browser:")]

    def test_main_browser_session_handle_is_untouched(self):
        sc = make_aux_sc()
        sc.handles["browser_session"] = object()
        sc.open_aux_browser("a")
        sc.close_aux_browsers()
        assert "browser_session" in sc.handles


class _OpensAux(Step):
    name = "OpensAux"

    def __init__(self, *, fail=False):
        self._fail = fail
        self.page = None

    def run(self, sc):
        self.page = sc.open_aux_browser("codex_oauth")
        if self._fail:
            raise TransientError("OAuth 失败")
        return StepResult.ok()

    def compensate(self, sc):
        sc.close_aux_browser("codex_oauth")


class TestAuxBrowserThroughRunner:
    def test_runner_injects_the_artifact_collector(self, tmp_path):
        from core.registration.steps import ArtifactCollector

        collector = ArtifactCollector(tmp_path / "attempt-1")
        sc = make_aux_sc()
        step = _OpensAux()
        StepRunner(Flow("f", [step]), artifacts=collector).run(sc)
        # attach 挂上了 = 辅助页的 console / 网络也进现场包
        assert step.page is not None
        assert sc._artifacts is None  # 跑完清掉，不留悬挂引用

    def test_failed_step_compensation_closes_the_aux_browser(self):
        sc = make_aux_sc()
        step = _OpensAux(fail=True)
        with pytest.raises(TransientError):
            StepRunner(Flow("f", [step])).run(sc)
        assert sc.launch_spec.sessions[0].closed is True

    def test_artifact_bundle_includes_the_aux_page(self, tmp_path):
        import json

        from core.registration.steps import ArtifactCollector

        collector = ArtifactCollector(tmp_path / "attempt-1")
        sc = make_aux_sc()
        sc.resolver = SimpleNamespace(channels_used={})
        step = _OpensAux()
        step.run(sc)

        directory = collector.capture(sc, step, 1, TransientError("boom"))
        files = {p.name for p in __import__("pathlib").Path(directory).iterdir()}
        assert "screenshot-codex_oauth.png" in files
        assert "page-codex_oauth.html.gz" in files
        meta = json.loads((__import__("pathlib").Path(directory) / "meta.json").read_text(encoding="utf-8"))
        assert meta["aux_pages"] == {"codex_oauth": "https://aux.example/start"}
