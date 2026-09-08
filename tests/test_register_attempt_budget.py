"""普通模式（非号码复用）的通用补投预算。

历史行为：非 HeroSMS 模式一次瞬态失败就只交付 count−1 个。现在可重试类失败
（等码超时 / 临时无号 / 号码被拒）在 count + ceil(count/2) 的预算内补投，
未分类的失败照旧占名额、不补。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import application.tasks as tasks
from application.tasks import TaskLogger, create_task, get_task
from core.base_platform import Account, RegisterConfig
from core.errors import ConfigError, TransientError


class _FakePlatform:
    name = "budget"
    display_name = "Budget"
    mailbox = None
    _last_identity = None

    def __init__(self, script):
        self.config = RegisterConfig(executor_type="protocol")
        self._script = script
        self.runtime = None

    def set_logger(self, log):
        self._log_fn = log

    def set_runtime(self, runtime):
        self.runtime = runtime

    def register(self, email=None, password=None):
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return Account(platform=self.name, email=outcome, password="pw")


@pytest.fixture
def harness(monkeypatch):
    """把 _execute_register_task 周围的外部依赖全部替换掉，只留调度循环本身。"""
    attempts: list[int] = []

    def build(script, platform_cls=_FakePlatform):
        platform_holder = {"platform": platform_cls(list(script))}

        def _build_platform_instance(platform_name, payload, logger, resolved_proxy=None, shared_mailbox=None):
            attempts.append(len(attempts) + 1)
            return platform_holder["platform"]

        monkeypatch.setattr(tasks, "_build_platform_instance", _build_platform_instance)
        monkeypatch.setattr(tasks, "get", lambda name: _FakePlatform)
        monkeypatch.setattr(tasks, "save_account", lambda account: None)
        monkeypatch.setattr(tasks, "_save_task_log", lambda *a, **kw: None)
        monkeypatch.setattr(tasks, "_auto_upload_cpa", lambda *a, **kw: None)
        monkeypatch.setattr(tasks, "_auto_push_any2api", lambda *a, **kw: None)
        monkeypatch.setattr(tasks, "_auto_followup_windsurf_payment", lambda **kw: None)
        monkeypatch.setattr(tasks, "_notify_mailbox_from_platform", lambda *a, **kw: None)
        monkeypatch.setattr(tasks, "_log_resolved_channels", lambda payload, logger: None)
        monkeypatch.setattr("core.proxy_pool.proxy_pool", SimpleNamespace(
            get_next=lambda: None, report_success=lambda p: None, report_fail=lambda p: None,
        ))
        return attempts

    return build


def _run(count: int) -> tuple[dict, str]:
    payload = {
        "platform": "budget",
        "count": count,
        "executor_type": "protocol",
        "extra": {"identity_provider": "oauth_browser"},
    }
    task = create_task(task_type="register", platform="budget", payload=payload, progress_total=count)
    logger = TaskLogger(task["id"])
    tasks._execute_register_task(payload, logger)
    return get_task(task["id"]), task["id"]


def test_transient_failure_is_compensated_within_budget(harness):
    attempts = harness([TransientError("等码超时"), "a@x.io", "b@x.io"])

    task, _ = _run(count=2)

    assert attempts == [1, 2, 3]
    assert task["status"] == "succeeded"
    assert task["success"] == 2
    assert task["progress_detail"] == {"current": 2, "total": 2, "label": "2/2"}


def test_unclassified_failure_still_consumes_a_slot(harness):
    attempts = harness([RuntimeError("莫名其妙"), "a@x.io"])

    task, _ = _run(count=2)

    # 未分类失败不补投：两个名额、两次尝试，一成一败
    assert attempts == [1, 2]
    assert task["status"] == "succeeded"
    assert task["success"] == 1
    assert task["error_count"] == 1


def test_budget_caps_compensation(harness):
    attempts = harness([TransientError("1"), TransientError("2"), TransientError("3")])

    task, _ = _run(count=1)

    # count=1 → 预算 1 + ceil(1/2) = 2 次，第三个脚本项不会被用到
    assert attempts == [1, 2]
    assert task["status"] == "failed"
    assert task["progress_detail"]["current"] == 1


def test_failed_attempt_checkpoint_is_marked_done_so_restart_reclaim_skips_it(harness):
    """失败的尝试在进程内已经补偿过：它的检查点要标 done，重启回收不能再对同一号码 cancel 一次。"""
    from sqlmodel import Session

    from core.db import TaskModel, engine

    class _WritesCheckpointThenFails(_FakePlatform):
        def register(self, email=None, password=None):
            self.runtime.checkpoint.write(
                attempt_id=self.runtime.attempt_id,
                cursor=3,
                step="MaybePhoneVerify",
                state={},
                resource_refs={"phone": {"provider_key": "herosms_api", "activation_id": "act_1"}},
                channels_used={},
                phase="running",
            )
            raise TransientError("等码超时")

    attempts = harness([], platform_cls=_WritesCheckpointThenFails)

    task, task_id = _run(count=1)

    # count=1 的预算是 2 次，两次都写了检查点、都失败
    assert attempts == [1, 2]

    with Session(engine) as session:
        checkpoints = session.get(TaskModel, task_id).get_result()["attempts"]
    entry = checkpoints[f"{task_id}-1"]
    assert entry["done"] is True
    assert entry["resource_refs"] == {}
    assert task["status"] == "failed"


def test_fatal_error_stops_the_batch(harness):
    attempts = harness([ConfigError("Key 未配置"), "never@x.io", "never2@x.io"])

    task, _ = _run(count=3)

    assert attempts == [1]
    assert task["status"] == "failed"
    assert "Key 未配置" in task["error"]
