"""人工介入：落库、唤醒、超时、取消，以及"人也是一种 provider"。"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from application.human_channel import DbHumanChannel, notify_answered
from core.cancellation import CancellationToken
from core.errors import TransientError
from core.registration.steps import (
    Flow,
    InteractionRequest,
    InteractionResponse,
    RetryPolicy,
    Step,
    StepContext,
    StepResult,
    StepRunner,
)
from infrastructure.task_interactions_repository import (
    STATUS_ANSWERED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    TaskInteractionsRepository,
)


@pytest.fixture()
def repo():
    """表由 conftest 的 _reset_db 建好，不需要拉起整个应用。"""
    return TaskInteractionsRepository()


class TestRepository:
    def test_create_and_list_pending(self, repo):
        interaction_id = repo.create(
            task_id="task_1",
            kind="input",
            prompt="请输入验证码",
            schema={"fields": ["code"]},
            context={"page_url": "https://x"},
        )

        items = repo.list_for_task("task_1", status=STATUS_PENDING)
        assert len(items) == 1
        assert items[0]["id"] == interaction_id
        assert items[0]["schema"]["fields"] == ["code"]
        assert items[0]["context"]["page_url"] == "https://x"

    def test_answer_transitions_status(self, repo):
        interaction_id = repo.create(task_id="task_1", kind="input", prompt="p")
        assert repo.answer(interaction_id, {"value": "123456"}) is True

        item = repo.get(interaction_id)
        assert item["status"] == STATUS_ANSWERED
        assert item["response"]["value"] == "123456"
        assert item["answered_at"] is not None

        # 第二次回答不能覆盖第一次的值
        assert repo.answer(interaction_id, {"value": "again"}) is False
        assert repo.get(interaction_id)["response"]["value"] == "123456"

    def test_concurrent_answers_only_one_wins(self, repo):
        """UI 双击 / 两个操作员同时提交：条件更新保证恰好一个成功，落库的是赢家的值。"""
        interaction_id = repo.create(task_id="task_1", kind="input", prompt="p")
        workers = 6
        barrier = threading.Barrier(workers)
        outcomes: list[tuple[int, bool]] = []
        outcomes_lock = threading.Lock()

        def submit(n: int):
            barrier.wait()
            ok = repo.answer(interaction_id, {"value": f"v{n}"})
            with outcomes_lock:
                outcomes.append((n, ok))

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert len(outcomes) == workers
        winners = [n for n, ok in outcomes if ok]
        assert len(winners) == 1
        assert repo.get(interaction_id)["response"]["value"] == f"v{winners[0]}"

    def test_finish_does_not_overwrite_an_answer(self, repo):
        interaction_id = repo.create(task_id="task_1", prompt="p")
        assert repo.answer(interaction_id, {"value": "confirmed"}) is True

        assert repo.finish(interaction_id, STATUS_EXPIRED) is False
        assert repo.get(interaction_id)["status"] == STATUS_ANSWERED

    def test_finish_marks_pending_row(self, repo):
        interaction_id = repo.create(task_id="task_1", prompt="p")

        assert repo.finish(interaction_id, STATUS_CANCELLED) is True
        assert repo.get(interaction_id)["status"] == STATUS_CANCELLED
        assert repo.finish(interaction_id, STATUS_EXPIRED) is False

    def test_cancel_pending_for_task(self, repo):
        repo.create(task_id="task_1", prompt="a")
        repo.create(task_id="task_1", prompt="b")
        repo.create(task_id="task_2", prompt="c")

        assert repo.cancel_pending_for_task("task_1") == 2
        assert repo.list_for_task("task_1", status=STATUS_PENDING) == []
        assert len(repo.list_for_task("task_2", status=STATUS_PENDING)) == 1

    def test_cancel_pending_for_task_skips_answered_rows(self, repo):
        answered_id = repo.create(task_id="task_1", prompt="a")
        repo.create(task_id="task_1", prompt="b")
        repo.answer(answered_id, {"value": "confirmed"})

        assert repo.cancel_pending_for_task("task_1") == 1
        assert repo.get(answered_id)["status"] == STATUS_ANSWERED


class TestRespondApi:
    def _waiting_task(self) -> str:
        """直接落一条 waiting_human 的任务：pending 任务会被 task_runtime 抢走执行。"""
        from sqlmodel import Session

        from core.db import TaskModel, engine

        task_id = "task_respond_api"
        with Session(engine) as session:
            session.add(TaskModel(id=task_id, type="register", platform="cursor", status="waiting_human"))
            session.commit()
        return task_id

    def test_second_respond_gets_409_and_wakes_waiter_once(self, client, repo, monkeypatch):
        import api.tasks as tasks_api

        task_id = self._waiting_task()
        interaction_id = repo.create(task_id=task_id, kind="input", prompt="验证码", schema={"fields": ["code"]})
        notified: list[int] = []
        monkeypatch.setattr(tasks_api, "notify_answered", lambda iid: notified.append(iid))

        url = f"/api/tasks/{task_id}/interactions/{interaction_id}/respond"
        first = client.post(url, json={"value": "111111"})
        second = client.post(url, json={"value": "222222"})

        assert first.status_code == 200
        assert first.json()["status"] == STATUS_ANSWERED
        assert first.json()["response"] == {"value": "111111"}
        assert second.status_code == 409
        assert notified == [interaction_id]
        assert repo.get(interaction_id)["response"]["value"] == "111111"

    def test_respond_to_someone_elses_interaction_is_404(self, client, repo):
        task_id = self._waiting_task()
        interaction_id = repo.create(task_id="another_task", prompt="确认")

        resp = client.post(f"/api/tasks/{task_id}/interactions/{interaction_id}/respond", json={})

        assert resp.status_code == 404
        assert repo.get(interaction_id)["status"] == STATUS_PENDING


class TestDbHumanChannel:
    def test_answer_wakes_the_waiting_thread(self, repo):
        channel = DbHumanChannel("task_ask")
        result = {}

        def ask():
            result["response"] = channel.ask(
                InteractionRequest(kind="input", prompt="验证码", fields=("code",), timeout=20)
            )

        thread = threading.Thread(target=ask)
        thread.start()

        interaction_id = _wait_for_pending(repo, "task_ask")
        repo.answer(interaction_id, {"value": "998877"})
        notify_answered(interaction_id)
        thread.join(timeout=10)

        assert result["response"].value == "998877"

    def test_status_flips_to_waiting_and_back(self, repo):
        states = []
        channel = DbHumanChannel(
            "task_state",
            on_waiting=lambda iid, req: states.append("waiting"),
            on_resumed=lambda iid: states.append("resumed"),
        )

        thread = threading.Thread(
            target=lambda: channel.ask(InteractionRequest(prompt="确认", timeout=20))
        )
        thread.start()
        interaction_id = _wait_for_pending(repo, "task_state")
        repo.answer(interaction_id, {"value": "confirmed"})
        notify_answered(interaction_id)
        thread.join(timeout=10)

        assert states == ["waiting", "resumed"]

    def test_timeout_marks_expired(self, repo):
        channel = DbHumanChannel("task_timeout")
        response = channel.ask(InteractionRequest(prompt="没人管", timeout=1))

        assert response.timed_out is True
        items = repo.list_for_task("task_timeout")
        assert items[0]["status"] == STATUS_EXPIRED

    def test_answer_landing_at_the_deadline_is_not_lost(self, repo, monkeypatch):
        """回答恰好在超时那一刻落库：finish(EXPIRED) 的条件更新影响 0 行，此时必须把回答带回去。"""
        channel = DbHumanChannel("task_race", repository=repo)
        real_finish = repo.finish
        finish_results: list[bool] = []

        def finish_after_answer_lands(interaction_id, status):
            if status == STATUS_EXPIRED:
                # 模拟 API 在 waiter 判定超时与执行 finish 之间把回答写进去
                assert repo.answer(interaction_id, {"value": "late-but-valid"}) is True
            result = real_finish(interaction_id, status)
            finish_results.append(result)
            return result

        monkeypatch.setattr(repo, "finish", finish_after_answer_lands)

        response = channel.ask(InteractionRequest(kind="input", prompt="验证码", fields=("code",), timeout=1))

        assert finish_results == [False]
        assert response.timed_out is False
        assert response.value == "late-but-valid"
        assert repo.list_for_task("task_race")[0]["status"] == STATUS_ANSWERED

    def test_on_waiting_failure_does_not_leak_waiter(self, repo):
        from application import human_channel

        def boom(interaction_id, request):
            raise RuntimeError("置 waiting_human 失败")

        resumed: list[int] = []
        channel = DbHumanChannel("task_leak", repository=repo, on_waiting=boom, on_resumed=resumed.append)

        with pytest.raises(RuntimeError, match="waiting_human"):
            channel.ask(InteractionRequest(prompt="确认", timeout=5))

        interaction_id = repo.list_for_task("task_leak")[0]["id"]
        with human_channel._WAITERS_LOCK:
            assert interaction_id not in human_channel._WAITERS
        assert resumed == [interaction_id]

    def test_cancel_token_releases_the_waiter(self, repo):
        token = CancellationToken()
        channel = DbHumanChannel("task_cancel")
        result = {}

        thread = threading.Thread(
            target=lambda: result.update(
                response=channel.ask(InteractionRequest(prompt="等着", timeout=30), cancel=token)
            )
        )
        thread.start()
        _wait_for_pending(repo, "task_cancel")
        token.trip("用户取消")
        thread.join(timeout=10)

        assert result["response"].cancelled is True
        assert repo.list_for_task("task_cancel")[0]["status"] == STATUS_CANCELLED


class TestManualProvidersAreJustProviders:
    def test_manual_sms_asks_twice(self):
        from providers.sms.manual import ManualSmsProvider

        asked = []

        class Channel:
            def ask(self, request, **kwargs):
                asked.append(request.prompt)
                value = "+8613800138000" if "手机号" in request.prompt else "445566"
                return InteractionResponse(value=value)

        provider = ManualSmsProvider(human_channel=Channel())
        activation = provider.get_number(platform="cursor")
        code = provider.get_code(activation.activation_id)

        assert activation.phone_number == "+8613800138000"
        assert code == "445566"
        assert len(asked) == 2

    def test_manual_sms_without_channel_is_a_config_error(self):
        from core.errors import ConfigError
        from providers.sms.manual import ManualSmsProvider

        with pytest.raises(ConfigError, match="人工通道"):
            ManualSmsProvider().get_number()

    def test_manual_captcha_uses_the_channel_instead_of_stdin(self):
        from core.base_captcha import CaptchaChallenge, ChallengeKind
        from providers.captcha.manual import ManualCaptcha

        class Channel:
            def ask_captcha(self, prompt, challenge):
                return "token-from-human"

        solver = ManualCaptcha.from_config({"human_channel": Channel()})
        solution = solver.solve(CaptchaChallenge(kind=ChallengeKind.TURNSTILE, page_url="https://x"))

        assert solution.token == "token-from-human"


class FailingStep(Step):
    name = "Flaky"

    def __init__(self):
        self.calls = 0

    def run(self, sc):
        self.calls += 1
        if self.calls == 1:
            return StepResult.fail(TransientError("第一次失败"))
        return StepResult.ok()


class TestHoldOnFailure:
    def _context(self, human):
        ctx = SimpleNamespace(
            platform_name="cursor",
            platform_display_name="Cursor",
            platform=SimpleNamespace(),
            log=lambda message, **kwargs: None,
        )
        return StepContext(ctx, human=human, attempt_id="a1")

    def test_operator_can_choose_retry(self):
        from core.registration.steps import InMemoryHumanChannel

        human = InMemoryHumanChannel([InteractionResponse(option="retry")])
        step = FailingStep()
        step.retry = RetryPolicy(max_attempts=1)

        StepRunner(Flow("f", [step]), hold_on_failure=True).run(self._context(human))

        assert step.calls == 2
        assert human.asked[0].kind == "choice"
        assert "retry" in human.asked[0].options

    def test_operator_can_choose_skip(self):
        from core.registration.steps import InMemoryHumanChannel

        human = InMemoryHumanChannel([InteractionResponse(option="skip")])
        step = FailingStep()
        step.retry = RetryPolicy(max_attempts=1)

        StepRunner(Flow("f", [step]), hold_on_failure=True).run(self._context(human))
        assert step.calls == 1

    def test_no_human_channel_means_normal_failure(self):
        step = FailingStep()
        step.retry = RetryPolicy(max_attempts=1)

        with pytest.raises(TransientError):
            StepRunner(Flow("f", [step]), hold_on_failure=True).run(self._context(None))


def _wait_for_pending(repo: TaskInteractionsRepository, task_id: str, timeout: float = 5.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        items = repo.list_for_task(task_id, status=STATUS_PENDING)
        if items:
            return int(items[0]["id"])
        time.sleep(0.05)
    raise AssertionError(f"没有等到 {task_id} 的待处理交互")
