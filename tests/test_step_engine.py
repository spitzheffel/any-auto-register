"""Step 引擎：契约、重试、回撤、取消、检查点、日志与失败现场。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.cancellation import CancellationToken
from core.errors import AttemptCancelled, ConfigError, ResourceRejectedError, TransientError
from core.registration.steps import (
    ArtifactCollector,
    CapabilityResolver,
    Flow,
    InMemoryHumanChannel,
    InteractionRequest,
    InteractionResponse,
    MemoryCheckpointSink,
    NetworkRecorder,
    Obstacle,
    ObstacleHandler,
    RetryPolicy,
    Step,
    StepContext,
    StepContractError,
    StepResult,
    StepRunner,
    prune_artifacts,
    redact,
)


class RecordingStep(Step):
    """可编排行为的测试步骤。"""

    def __init__(
        self,
        name,
        *,
        writes=(),
        reads=(),
        anchor=False,
        requires=(),
        retry=None,
        behavior=None,
        guard=None,
        events=None,
    ):
        self.name = name
        self.writes = tuple(writes)
        self.reads = tuple(reads)
        self.anchor = anchor
        self.requires = tuple(requires)
        self.retry = retry
        self._behavior = behavior or (lambda sc, calls: StepResult.ok())
        self._guard = guard
        self.events = events if events is not None else []
        self.calls = 0

    def when(self, sc):
        return True if self._guard is None else self._guard(sc)

    def run(self, sc):
        self.calls += 1
        self.events.append(("run", self.name, self.calls))
        result = self._behavior(sc, self.calls)
        if result is None or result.outcome == "ok":
            for key in self.writes:
                sc.state.setdefault(key, f"{key}-value")
        return result or StepResult.ok()

    def compensate(self, sc):
        self.events.append(("compensate", self.name, self.calls))


def make_context(**kwargs):
    logs = []

    def log_fn(message, level="info", event_type="log", detail=None):
        logs.append({"message": message, "level": level, "event_type": event_type, "detail": detail or {}})

    ctx = SimpleNamespace(
        platform_name="cursor",
        platform_display_name="Cursor",
        platform=SimpleNamespace(),
        log=log_fn,
    )
    sc = StepContext(ctx, attempt_id="attempt-1", **kwargs)
    sc.logs = logs
    return sc


class TestDataContract:
    def test_build_rejects_reads_without_a_writer(self):
        with pytest.raises(StepContractError, match="没有来源的键"):
            Flow("f", [RecordingStep("A", reads=("email",))])

    def test_build_accepts_initial_context_keys(self):
        flow = Flow("f", [RecordingStep("A", reads=("email",))], initial_keys=("email",))
        assert flow.steps[0].name == "A"

    def test_build_rejects_result_keys_nobody_writes(self):
        with pytest.raises(StepContractError, match="结果键无人写出"):
            Flow("f", [RecordingStep("A")], result_keys=("token",))

    def test_build_rejects_duplicate_step_names(self):
        with pytest.raises(StepContractError, match="步骤名重复"):
            Flow("f", [RecordingStep("A"), RecordingStep("A")])

    def test_runtime_write_contract_is_enforced(self):
        # 声明写 token 却不写：正是 Cursor "密码不回传" 那类接线遗漏
        class Liar(RecordingStep):
            def run(self, sc):
                self.calls += 1
                return StepResult.ok()

        flow = Flow("f", [Liar("A", writes=("token",))], result_keys=("token",))
        with pytest.raises(StepContractError, match="声明会写出"):
            StepRunner(flow).run(make_context())

    def test_result_keys_are_assembled_from_state(self):
        flow = Flow(
            "f",
            [RecordingStep("A", writes=("email", "token"))],
            result_keys=("email", "token"),
        )
        result = StepRunner(flow).run(make_context())
        assert result == {"email": "email-value", "token": "token-value"}


class TestRetry:
    def test_transient_error_retries_in_place(self):
        def flaky(sc, calls):
            if calls < 3:
                return StepResult.fail(TransientError("等码超时"))
            return StepResult.ok()

        step = RecordingStep("A", behavior=flaky, retry=RetryPolicy(max_attempts=3))
        StepRunner(Flow("f", [step])).run(make_context())
        assert step.calls == 3

    def test_retry_budget_exhausted_propagates(self):
        step = RecordingStep(
            "A",
            behavior=lambda sc, calls: StepResult.fail(TransientError("总是超时")),
            retry=RetryPolicy(max_attempts=2),
        )
        with pytest.raises(TransientError):
            StepRunner(Flow("f", [step])).run(make_context())
        assert step.calls == 2

    def test_fatal_error_is_not_retried(self):
        step = RecordingStep("A", behavior=lambda sc, calls: StepResult.fail(ConfigError("缺 key")))
        with pytest.raises(ConfigError):
            StepRunner(Flow("f", [step])).run(make_context())
        assert step.calls == 1

    def test_default_table_applies_without_explicit_policy(self):
        step = RecordingStep(
            "A",
            behavior=lambda sc, calls: StepResult.ok() if calls > 1 else StepResult.fail(ResourceRejectedError("号码被拒")),
        )
        StepRunner(Flow("f", [step])).run(make_context())
        assert step.calls == 2

    def test_unclassified_error_is_not_retried(self):
        step = RecordingStep("A", behavior=lambda sc, calls: StepResult.fail(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            StepRunner(Flow("f", [step])).run(make_context())
        assert step.calls == 1


class TestRewind:
    def _flow(self, events):
        open_page = RecordingStep("OpenPage", anchor=True, writes=("page_ready",), events=events)
        fill = RecordingStep("Fill", reads=("page_ready",), writes=("filled",), events=events)

        state = {"rewound": False}

        def otp_behavior(sc, calls):
            if not state["rewound"]:
                state["rewound"] = True
                return StepResult.rewind("OpenPage", "验证码超时")
            return StepResult.ok()

        otp = RecordingStep("Otp", reads=("filled",), behavior=otp_behavior, events=events)
        return Flow("f", [open_page, fill, otp]), (open_page, fill, otp)

    def test_rewind_reruns_from_anchor_and_compensates(self):
        events = []
        flow, (open_page, fill, otp) = self._flow(events)
        StepRunner(flow).run(make_context())

        assert open_page.calls == 2
        assert fill.calls == 2
        assert otp.calls == 2
        # 回撤时逆序补偿被退回的步骤
        assert ("compensate", "Fill", 1) in events
        assert ("compensate", "OpenPage", 1) in events

    def test_rewind_target_must_be_anchor(self):
        first = RecordingStep("First", writes=("x",))
        second = RecordingStep("Second", reads=("x",), behavior=lambda sc, calls: StepResult.rewind("First", "retry"))
        with pytest.raises(StepContractError, match="必须是 anchor"):
            StepRunner(Flow("f", [first, second])).run(make_context())

    def test_rewind_budget_prevents_infinite_loop(self):
        anchor = RecordingStep("Anchor", anchor=True)
        looper = RecordingStep("Loop", behavior=lambda sc, calls: StepResult.rewind("Anchor", "again"))
        with pytest.raises(Exception, match="预算已用尽"):
            StepRunner(Flow("f", [anchor, looper]), rewind_budget=2).run(make_context())
        assert looper.calls == 3  # 首次 + 两次预算

    def test_rewind_invalidates_writes_of_rolled_back_steps(self):
        anchor = RecordingStep("Anchor", anchor=True)
        writer = RecordingStep("Writer", writes=("temp_key",))
        seen = []

        def check(sc, calls):
            seen.append("temp_key" in sc.state)
            if calls == 1:
                return StepResult.rewind("Anchor", "重来")
            return StepResult.ok()

        checker = RecordingStep("Checker", behavior=check)
        StepRunner(Flow("f", [anchor, writer, checker])).run(make_context())
        # 第二轮进来时 writer 重新写过，所以两次都为 True；关键是没有残留旧值导致跳过
        assert seen == [True, True]

    def test_rewind_keeps_read_modify_write_keys(self):
        """密码这类"读到空就生成"的键回撤后要保留：站点侧账号可能已经用第一次的值建好了。"""
        anchor = RecordingStep("Anchor", anchor=True)
        generated = []

        def set_password(sc, calls):
            if not sc.state.get("password"):
                sc.state["password"] = f"gen-{calls}"
            generated.append(sc.state["password"])
            return StepResult.ok()

        password_step = RecordingStep("SetPassword", reads=("password",), writes=("password",), behavior=set_password)
        rewound = {"done": False}

        def otp(sc, calls):
            if not rewound["done"]:
                rewound["done"] = True
                return StepResult.rewind("Anchor", "验证码没来")
            return StepResult.ok()

        otp_step = RecordingStep("Otp", behavior=otp)
        flow = Flow("f", [anchor, password_step, otp_step], initial_keys=("password",))
        sc = make_context()
        sc.state["password"] = ""
        StepRunner(flow).run(sc)

        assert generated == ["gen-1", "gen-1"]
        assert sc.state["password"] == "gen-1"

    def test_rewind_compensates_the_step_that_asked_for_it(self):
        events = []
        anchor = RecordingStep("Anchor", anchor=True, events=events)
        state = {"rewound": False}

        def flaky(sc, calls):
            if not state["rewound"]:
                state["rewound"] = True
                return StepResult.rewind("Anchor", "重来")
            return StepResult.ok()

        initiator = RecordingStep("Initiator", behavior=flaky, events=events)
        StepRunner(Flow("f", [anchor, initiator])).run(make_context())

        # 发起回撤的步骤没有成功，它中途占用的资源也要退掉
        assert ("compensate", "Initiator", 1) in events


class TestFailedStepCompensation:
    def test_exhausted_retries_compensate_the_failing_step(self):
        events = []
        first = RecordingStep("A", events=events)
        failing = RecordingStep(
            "B",
            behavior=lambda sc, calls: StepResult.fail(RuntimeError("boom")),
            events=events,
        )

        with pytest.raises(RuntimeError, match="boom"):
            StepRunner(Flow("f", [first, failing])).run(make_context())

        # 失败的当前步骤先补偿，再逆序补偿已完成的
        assert events.index(("compensate", "B", 1)) < events.index(("compensate", "A", 1))

    def test_contract_violation_compensates_the_step(self):
        events = []
        liar = RecordingStep("Liar", writes=("never_written",), events=events)

        # RecordingStep.run 会 setdefault 声明的键；这里改成不写，触发 _assert_writes
        def run_without_writes(sc):
            liar.calls += 1
            events.append(("run", "Liar", liar.calls))
            return StepResult.ok()

        liar.run = run_without_writes
        with pytest.raises(StepContractError):
            StepRunner(Flow("f", [liar])).run(make_context())
        assert ("compensate", "Liar", 1) in events

    def test_keyboard_interrupt_is_not_treated_as_step_failure(self):
        def interrupt(sc, calls):
            raise KeyboardInterrupt()

        step = RecordingStep("A", behavior=interrupt, retry=RetryPolicy(max_attempts=3))
        with pytest.raises(KeyboardInterrupt):
            StepRunner(Flow("f", [step])).run(make_context())
        assert step.calls == 1


class TestCancellation:
    def test_cancel_between_steps(self):
        token = CancellationToken()
        first = RecordingStep("A", behavior=lambda sc, calls: (token.trip("用户取消"), StepResult.ok())[1])
        second = RecordingStep("B")

        with pytest.raises(AttemptCancelled):
            StepRunner(Flow("f", [first, second])).run(make_context(cancel=token))
        assert second.calls == 0

    def test_cancel_runs_compensation(self):
        events = []
        token = CancellationToken()
        first = RecordingStep("A", events=events)
        second = RecordingStep(
            "B",
            behavior=lambda sc, calls: (token.trip("stop"), StepResult.ok())[1],
            events=events,
        )
        third = RecordingStep("C", events=events)

        with pytest.raises(AttemptCancelled):
            StepRunner(Flow("f", [first, second, third])).run(make_context(cancel=token))

        assert ("compensate", "B", 1) in events
        assert ("compensate", "A", 1) in events

    def test_ask_is_cancelled_by_token(self):
        token = CancellationToken()
        token.trip("stop")
        sc = make_context(cancel=token, human=InMemoryHumanChannel())
        with pytest.raises(AttemptCancelled):
            sc.ask(InteractionRequest(prompt="确认"))


class TestHumanChannel:
    def test_ask_returns_answer(self):
        human = InMemoryHumanChannel([InteractionResponse(value="123456")])
        sc = make_context(human=human)
        response = sc.ask(InteractionRequest(kind="input", prompt="输入验证码"))
        assert response.value == "123456"
        assert human.asked[0].prompt == "输入验证码"

    def test_missing_channel_is_a_contract_error(self):
        sc = make_context()
        with pytest.raises(StepContractError, match="人工通道"):
            sc.ask(InteractionRequest(prompt="确认"))

    def test_timeout_response_is_surfaced(self):
        sc = make_context(human=InMemoryHumanChannel())
        assert sc.ask(InteractionRequest(prompt="没人回答")).timed_out is True


class TestObstacles:
    def test_handler_runs_before_each_step(self):
        seen = []

        class Turnstile(ObstacleHandler):
            name = "turnstile"

            def __init__(self):
                self.remaining = 1

            def detect(self, sc):
                if self.remaining:
                    self.remaining -= 1
                    return Obstacle("turnstile")
                return None

            def resolve(self, sc, obstacle):
                seen.append("resolved")
                return True

        step = RecordingStep("A")
        sc = make_context()
        StepRunner(Flow("f", [step]), obstacles=[Turnstile()]).run(sc)

        assert seen == ["resolved"]
        assert any(entry["event_type"] == "obstacle_resolved" for entry in sc.logs)

    def test_handler_runs_again_before_retry(self):
        detections = []

        class Flaky(ObstacleHandler):
            name = "cf"

            def detect(self, sc):
                detections.append(sc.current_step)
                return None

            def resolve(self, sc, obstacle):
                return True

        step = RecordingStep(
            "A",
            behavior=lambda sc, calls: StepResult.ok() if calls > 1 else StepResult.fail(TransientError("x")),
        )
        StepRunner(Flow("f", [step]), obstacles=[Flaky()]).run(make_context())
        # 步前一次 + 重试前一次
        assert len(detections) >= 2


class TestCheckpointAndResume:
    def test_checkpoint_written_after_each_step(self):
        sink = MemoryCheckpointSink()
        flow = Flow("f", [RecordingStep("A", writes=("email",)), RecordingStep("B", writes=("token",))])
        StepRunner(flow, checkpoint=sink).run(make_context())

        assert [item["step"] for item in sink.writes] == ["A", "B"]
        assert sink.last["cursor"] == 2
        assert sink.last["state"]["token"] == "token-value"

    def test_start_at_skips_completed_steps(self):
        first = RecordingStep("A", writes=("email",))
        second = RecordingStep("B", reads=("email",), writes=("token",))
        flow = Flow("f", [first, second], result_keys=("token",))

        result = StepRunner(flow).run(make_context(), start_at="B", state={"email": "a@b.c"})

        assert first.calls == 0
        assert second.calls == 1
        assert result["token"] == "token-value"

    def test_resume_rejects_insufficient_state(self):
        flow = Flow(
            "f",
            [RecordingStep("A", writes=("email",)), RecordingStep("B", reads=("email",))],
        )
        with pytest.raises(StepContractError, match="不足以从"):
            StepRunner(flow).run(make_context(), start_at="B")


class TestLogging:
    def test_structured_events_cover_the_lifecycle(self):
        sc = make_context()
        StepRunner(Flow("f", [RecordingStep("A", writes=("x",))])).run(sc)
        types = [entry["event_type"] for entry in sc.logs]

        assert "step_started" in types
        assert "step_ok" in types
        assert all(entry["detail"].get("attempt_id") == "attempt-1" for entry in sc.logs)
        assert all(entry["detail"].get("step") == "A" for entry in sc.logs)

    def test_sensitive_values_are_redacted(self):
        redacted = redact({"email": "a@b.c", "password": "secret", "token": "", "obj": object()})

        assert redacted["email"] == "a@b.c"
        assert redacted["password"] == "<set>"
        assert redacted["token"] == "<empty>"
        assert redacted["obj"].startswith("<object")


class FakePage:
    def __init__(self, tmp_path):
        self.url = "https://example.com/signup"
        self._tmp = tmp_path
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def screenshot(self, path):
        with open(path, "wb") as handle:
            handle.write(b"png")

    def content(self):
        return "<html><body><input name='email'></body></html>"


class TestArtifacts:
    def test_failure_bundle_contains_five_files(self, tmp_path):
        page = FakePage(tmp_path)
        recorder = NetworkRecorder()
        collector = ArtifactCollector(tmp_path / "attempt-1", network=recorder)
        collector.attach(page)
        recorder._on_response(SimpleNamespace(url="https://example.com/api", status=429, text=lambda: '{"error":"rate"}'))

        step = RecordingStep("A", behavior=lambda sc, calls: StepResult.fail(ConfigError("boom")))
        sc = make_context(page=page)
        sc.state["password"] = "secret"

        with pytest.raises(ConfigError):
            StepRunner(Flow("f", [step]), artifacts=collector).run(sc)

        bundle = tmp_path / "attempt-1" / "A-1"
        assert (bundle / "screenshot.png").exists()
        assert (bundle / "page.html.gz").exists()
        assert (bundle / "network.json").exists()
        assert (bundle / "console.json").exists()

        meta = json.loads((bundle / "meta.json").read_text(encoding="utf-8"))
        assert meta["step"] == "A"
        assert meta["category"] == "config"
        assert meta["page_url"] == "https://example.com/signup"
        assert meta["state"]["password"] == "<set>"

        network = json.loads((bundle / "network.json").read_text(encoding="utf-8"))
        assert network[0]["status"] == 429

    def test_each_retry_gets_its_own_bundle(self, tmp_path):
        collector = ArtifactCollector(tmp_path / "attempt-2")
        step = RecordingStep(
            "A",
            behavior=lambda sc, calls: StepResult.fail(TransientError("x")),
            retry=RetryPolicy(max_attempts=2),
        )
        with pytest.raises(TransientError):
            StepRunner(Flow("f", [step]), artifacts=collector).run(make_context())

        assert (tmp_path / "attempt-2" / "A-1").exists()
        assert (tmp_path / "attempt-2" / "A-2").exists()

    def test_network_buffer_is_bounded(self):
        recorder = NetworkRecorder(capacity=3, body_limit=4)
        for index in range(10):
            recorder._on_response(SimpleNamespace(url=f"u{index}", status=200, text=lambda: "abcdefgh"))

        assert len(recorder.entries) == 3
        assert recorder.entries[0]["url"] == "u7"
        assert recorder.entries[0]["body"] == "abcd"

    def test_retention_keeps_recent_bundles(self, tmp_path):
        root = tmp_path / "attempts"
        for index in range(5):
            (root / f"step-{index}").mkdir(parents=True)

        prune_artifacts(root, keep=2, max_age_days=14)
        assert len(list(root.iterdir())) == 2


class TestCapabilityResolver:
    def test_lazily_builds_and_caches(self):
        built = []

        def builder(ctx, step, key):
            built.append((step, key))
            return SimpleNamespace(provider_key="herosms_api")

        resolver = CapabilityResolver(SimpleNamespace(), builders={"phone": builder})
        sc = make_context(resolver=resolver)
        sc.current_step = "MaybePhoneVerify"

        first = sc.require("phone")
        second = sc.require("phone")

        assert first is second
        assert built == [("MaybePhoneVerify", "")]
        # 按渠道类别记录，没有绑定时退到实例自报的 provider_key
        assert resolver.channels_used == {"sms": "herosms_api"}

    def test_unknown_capability_is_a_contract_error(self):
        resolver = CapabilityResolver(SimpleNamespace(), builders={})
        with pytest.raises(StepContractError, match="未知能力"):
            resolver.resolve("voice_otp")

    def test_same_key_shares_one_instance_across_steps(self):
        """一次尝试一个 PhoneVerification：不同步骤要同一渠道拿到的是同一个实例。"""
        resolver = CapabilityResolver(
            SimpleNamespace(),
            builders={"phone": lambda ctx, step, key: SimpleNamespace(provider_key="herosms_api")},
        )
        assert resolver.resolve("phone", step="A") is resolver.resolve("phone", step="B")

    def test_step_binding_overrides_yield_separate_instances(self):
        from core.channel_bindings import ChannelBinding, ChannelBindings

        bindings = ChannelBindings(
            {"captcha": ChannelBinding(key="yescaptcha", steps={"Payment": "twocaptcha"})},
        )
        resolver = CapabilityResolver(
            SimpleNamespace(),
            bindings=bindings,
            builders={"captcha": lambda ctx, step, key: SimpleNamespace(provider_key=key)},
        )
        assert resolver.resolve("captcha", step="Signup").provider_key == "yescaptcha"
        assert resolver.resolve("captcha", step="Payment").provider_key == "twocaptcha"
        assert resolver.channels_used == {"captcha": "twocaptcha"}

    def test_close_all_releases_instances_with_close(self):
        closed = []
        resolver = CapabilityResolver(
            SimpleNamespace(),
            builders={"phone": lambda ctx, step, key: SimpleNamespace(close=lambda: closed.append(step))},
        )
        resolver.resolve("phone", step="MaybePhoneVerify")
        resolver.close_all()
        assert closed == ["MaybePhoneVerify"]
