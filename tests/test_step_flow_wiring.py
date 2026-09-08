"""生产装配链路的集成测试：run_step_flow × 真实 RegistrationContext / BasePlatform.log / TaskLogger。

单元测试里的 fake logger 接受 **kwargs、步骤测试绕过引擎直接调 run()，
把"结构化事件在生产链路被吞掉"和"失败步骤不补偿"这两类问题全遮住了。
这里用假浏览器内核把整条生产路径串起来跑一遍。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import core.browser_engine as browser_engine
from application.tasks import (
    TaskLogger,
    build_step_runtime,
    create_task,
    get_task,
    list_task_events,
)
from core.base_platform import BasePlatform, RegisterConfig
from core.base_sms import _SMS_VERIFY_WINDOW_LOCK
from core.cancellation import CancellationToken
from core.channel_bindings import ChannelBindings
from core.errors import TransientError
from core.registration.adapters import BrowserRegistrationAdapter
from core.registration.flows import run_step_flow
from core.registration.models import RegistrationContext
from core.registration.steps import Flow, Step, StepResult


# ---------------------------------------------------------------------------
# 假浏览器内核 / 假平台
# ---------------------------------------------------------------------------

class _FakePage:
    url = "https://example.test/signup"

    def screenshot(self, **kwargs):
        return None

    def content(self):
        return "<html></html>"

    def on(self, *args, **kwargs):
        return None


class _FakeSession:
    def __init__(self):
        self.closed = False

    def new_page(self):
        return _FakePage()

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _WiringPlatform(BasePlatform):
    """绕过 __init__ 的 DB 能力查询，只保留 log / runtime 这条生产链路。"""

    name = "wiring"
    display_name = "Wiring"

    def __init__(self):
        self.config = RegisterConfig(executor_type="headless")
        self._log_fn = print
        self.runtime = None

    def check_valid(self, account):
        return True


class _FakeLease:
    def __init__(self, phone, *, code="123456"):
        self._phone = phone
        self.phone_number = "+15550001111"
        self.activation_id = "act_1"
        self.released = False
        self.code = code
        self.events: list[str] = []

    def wait_code(self, **kwargs):
        return self.code

    def report_send_succeeded(self):
        self.events.append("send_ok")

    def report_code_failed(self, reason=""):
        self.events.append("code_failed")

    def release(self, outcome="unused"):
        if not self.released:
            self.released = True
            self.events.append(f"release:{outcome}")
            self._phone.lock_released()

    def to_ref(self):
        return {"provider_key": "herosms_api", "activation_id": self.activation_id}


def lock_is_free_from_another_thread(timeout: float = 0.5) -> bool:
    """RLock 在持有线程上永远可重入，"锁已释放"只能从别的线程探测才有意义。"""
    outcome: dict[str, bool] = {}

    def probe():
        acquired = _SMS_VERIFY_WINDOW_LOCK.acquire(timeout=timeout)
        outcome["ok"] = acquired
        if acquired:
            _SMS_VERIFY_WINDOW_LOCK.release()

    worker = threading.Thread(target=probe)
    worker.start()
    worker.join(timeout=timeout + 2)
    return outcome.get("ok") is True


class _FakePhone:
    """模拟 PhoneVerification：acquire 时拿进程级验证锁，release / close 时放掉。"""

    provider_key = "herosms_api"

    def __init__(self, *, code="123456"):
        self.code = code
        self.lease = None
        self.closed = False
        self.lease_released_before_close = None
        self._holding = False

    def acquire(self, **kwargs):
        if self.lease is not None and not self.lease.released:
            return self.lease
        assert lock_is_free_from_another_thread(1.0), "验证锁被上一次尝试带走了"
        assert _SMS_VERIFY_WINDOW_LOCK.acquire(timeout=1)
        self._holding = True
        self.lease = _FakeLease(self, code=self.code)
        return self.lease

    def lock_released(self):
        if self._holding:
            self._holding = False
            _SMS_VERIFY_WINDOW_LOCK.release()

    def close(self):
        self.closed = True
        # 记下 close 时 lease 是否已经被补偿释放：区分"步骤补偿放的"还是"收尾 close 兜底放的"
        self.lease_released_before_close = self.lease.released if self.lease is not None else None
        if self.lease is not None and not self.lease.released:
            self.lease.release("unused")


class _PhoneStep(Step):
    """最小版 MaybePhoneVerify：租号 → 等码 → 成功释放；没码就退号再抛 Transient。"""

    name = "PhoneStep"
    requires = ("phone",)
    writes = ("phone_number",)

    def run(self, sc):
        phone = sc.require("phone")
        lease = sc.handles.get("phone_lease")
        if lease is None or lease.released:
            lease = phone.acquire()
            sc.handles["phone_lease"] = lease
            sc.resource_refs["phone"] = lease.to_ref()
            sc.checkpoint_now()
        sc.state["phone_number"] = lease.phone_number
        code = lease.wait_code()
        if not code:
            lease.report_code_failed()
            lease.release("unused")
            sc.handles.pop("phone_lease", None)
            sc.resource_refs.pop("phone", None)
            raise TransientError("未收到短信验证码")
        lease.release("success")
        sc.handles.pop("phone_lease", None)
        sc.resource_refs.pop("phone", None)
        sc.checkpoint_now()
        return StepResult.ok()

    def compensate(self, sc):
        lease = sc.handles.pop("phone_lease", None)
        if lease is not None and not lease.released:
            lease.release("unused")
        sc.resource_refs.pop("phone", None)


class _TokenStep(Step):
    name = "TokenStep"
    writes = ("token",)

    def run(self, sc):
        sc.state["token"] = "secret-session-token"
        return StepResult.ok()


def _adapter(phone: _FakePhone, steps: list[Step]):
    flow = Flow("wiring", steps, initial_keys=("email", "password"), result_keys=("token",))
    return BrowserRegistrationAdapter(
        result_mapper=lambda ctx, raw: raw,
        step_flow_builder=lambda: flow,
        obstacle_builder=lambda: [],
    ), phone


@pytest.fixture
def fake_engine(monkeypatch):
    sessions: list[_FakeSession] = []

    @contextmanager
    def _start_browser(**kwargs):
        session = _FakeSession()
        sessions.append(session)
        yield session
        session.close()

    monkeypatch.setattr(browser_engine, "start_browser", _start_browser)
    return sessions


def _run(phone: _FakePhone, steps: list[Step], *, tmp_path, checkpoint_writes: list | None = None):
    """走生产装配：create_task → TaskLogger → build_step_runtime → BasePlatform.log → run_step_flow。"""
    task = create_task(task_type="register", platform="wiring", payload={"extra": {}}, progress_total=1)
    logger = TaskLogger(task["id"])
    runtime = build_step_runtime(
        logger,
        attempt_id=f"{task['id']}-1",
        cancel_token=CancellationToken(),
        bindings=ChannelBindings.from_payload({"extra": {}}),
    )
    runtime.artifact_root = str(tmp_path)
    if checkpoint_writes is not None:
        real_write = runtime.checkpoint.write

        def recording_write(**kwargs):
            checkpoint_writes.append(dict(kwargs))
            return real_write(**kwargs)

        runtime.checkpoint.write = recording_write

    platform = _WiringPlatform()
    platform.set_logger(logger.log)
    platform.set_runtime(runtime)

    # 生产里 phone 由 build_phone_verification 造；这里只替换这一个 builder，其余链路照旧
    from core.registration import steps as steps_mod

    original = steps_mod.default_capability_builders
    builders = original()
    builders["phone"] = lambda ctx, step, key: phone
    steps_mod.default_capability_builders = lambda: builders
    try:
        ctx = RegistrationContext(
            platform_name="wiring",
            platform_display_name="Wiring",
            platform=platform,
            identity=SimpleNamespace(email="user@example.com"),
            config=platform.config,
            email="user@example.com",
            password="Passw0rd!",
            log_fn=platform.log,
            runtime=runtime,
        )
        adapter, _ = _adapter(phone, steps)
        try:
            result = run_step_flow(ctx, adapter)
            error = None
        except BaseException as exc:  # noqa: BLE001 - AttemptCancelled 是 BaseException
            result, error = None, exc
    finally:
        steps_mod.default_capability_builders = original
    return task["id"], runtime, result, error


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

class TestStructuredEventsReachTheDatabase:
    def test_step_events_keep_type_level_and_detail(self, fake_engine, tmp_path):
        phone = _FakePhone()
        task_id, runtime, result, error = _run(phone, [_PhoneStep(), _TokenStep()], tmp_path=tmp_path)

        assert error is None and result == {"token": "secret-session-token"}
        events = list_task_events(task_id)
        types = [event["type"] for event in events]
        assert "step_started" in types and "step_ok" in types
        started = next(event for event in events if event["type"] == "step_started")
        assert started["detail"]["step"] == "PhoneStep"
        assert started["detail"]["attempt_id"] == f"{task_id}-1"

    def test_failure_event_carries_error_level_and_artifacts(self, fake_engine, tmp_path):
        phone = _FakePhone(code="")
        task_id, runtime, result, error = _run(phone, [_PhoneStep(), _TokenStep()], tmp_path=tmp_path)

        assert isinstance(error, TransientError)
        failed = [event for event in list_task_events(task_id) if event["type"] == "step_failed"]
        assert failed, "step_failed 没有以结构化事件落库"
        assert failed[0]["level"] == "error"
        assert failed[0]["detail"]["category"] == "transient"
        assert failed[0]["detail"]["artifacts"].startswith(str(tmp_path))


class TestFailureReleasesResources:
    def test_no_code_releases_lease_and_the_verify_lock(self, fake_engine, tmp_path):
        phone = _FakePhone(code="")
        task_id, runtime, result, error = _run(phone, [_PhoneStep(), _TokenStep()], tmp_path=tmp_path)

        assert isinstance(error, TransientError)
        assert phone.lease.released is True
        assert phone.closed is True
        # 锁必须回到可获取状态：这就是"下一个任务卡死"那条链的断点。
        # 必须从别的线程探测——RLock 在本线程上永远可重入，同线程 acquire 什么都证明不了
        assert lock_is_free_from_another_thread(), "_SMS_VERIFY_WINDOW_LOCK 仍被泄漏的租约持有"

    def test_cancel_mid_step_compensates_the_running_step(self, fake_engine, tmp_path):
        from core.errors import AttemptCancelled

        class _CancelAfterAcquire(_PhoneStep):
            name = "PhoneStep"

            def run(self, sc):
                phone = sc.require("phone")
                lease = phone.acquire()
                sc.handles["phone_lease"] = lease
                sc.resource_refs["phone"] = lease.to_ref()
                raise AttemptCancelled("用户取消")

        phone = _FakePhone()
        task_id, runtime, result, error = _run(phone, [_CancelAfterAcquire(), _TokenStep()], tmp_path=tmp_path)

        assert isinstance(error, AttemptCancelled)
        assert phone.lease.released is True
        # 是步骤补偿放的号，不是收尾 close() 兜底放的：close 时 lease 必须已经 released
        assert phone.lease_released_before_close is True
        assert lock_is_free_from_another_thread()


class _ExclusiveProvider:
    """最小版 HeroSMS 形态的 provider：要求独占验证窗口，get_code 可配置为永不来码。"""

    requires_exclusive_verification_window = True
    auto_report_success_on_code = True
    provider_key = "fake_exclusive"

    def __init__(self, *, code="654321"):
        self.code = code
        self.events: list[str] = []

    def resolve_service_code(self, *, platform="", service=""):
        return "ot"

    def peek_reusable_country(self, **kwargs):
        return ""

    def select_best_country(self, **kwargs):
        return ""

    def fallback_country(self):
        return ""

    def get_number(self, *, platform="", service="", country=""):
        from core.base_sms import SmsActivation

        self.events.append("get_number")
        return SmsActivation(activation_id="act_real", phone_number="+15550002222", country=country)

    def get_code(self, activation_id, *, timeout=180, should_stop=None):
        self.events.append("get_code")
        return self.code

    def set_resend_callback(self, callback):
        return None

    def mark_send_succeeded(self, activation_id):
        self.events.append("send_ok")

    def mark_send_failed(self, activation_id, reason=""):
        self.events.append("send_failed")

    def mark_code_failed(self, activation_id, reason=""):
        self.events.append("code_failed")

    def report_success(self, activation_id):
        self.events.append("report_success")
        return True

    def cancel(self, activation_id):
        self.events.append("cancel")
        return True


class TestRealPhoneVerificationLockPath:
    """走真实 PhoneVerification / PhoneLease：验证窗口锁由它们自己取与放。"""

    def _real_phone(self, monkeypatch, provider):
        import core.base_sms as base_sms
        from core.phone_verification import PhoneVerification

        monkeypatch.setattr(base_sms, "create_sms_provider", lambda key, config: provider)
        return PhoneVerification("fake_exclusive", {}, platform="wiring", log_fn=lambda *a, **k: None)

    def test_no_code_path_releases_number_and_lock_through_real_lease(self, fake_engine, tmp_path, monkeypatch):
        provider = _ExclusiveProvider(code="")
        phone = self._real_phone(monkeypatch, provider)

        task_id, runtime, result, error = _run(phone, [_PhoneStep(), _TokenStep()], tmp_path=tmp_path)

        assert isinstance(error, TransientError)
        # 号码退掉、不上报成功
        assert "cancel" in provider.events and "report_success" not in provider.events
        assert phone.active_lease is None
        # 锁回到可获取状态，且是真实 _acquire_verify_lock / _release_verify_lock 那条链放的
        assert phone._verify_lock_acquired is False
        assert lock_is_free_from_another_thread()

    def test_cancel_while_holding_number_is_released_by_close_all(self, fake_engine, tmp_path, monkeypatch):
        from core.errors import AttemptCancelled

        provider = _ExclusiveProvider()
        phone = self._real_phone(monkeypatch, provider)

        class _CancelAfterAcquire(Step):
            name = "PhoneStep"
            requires = ("phone",)

            def run(self, sc):
                lease = sc.require("phone").acquire()
                sc.handles["phone_lease"] = lease
                raise AttemptCancelled("用户取消")

        task_id, runtime, result, error = _run(phone, [_CancelAfterAcquire(), _TokenStep()], tmp_path=tmp_path)

        assert isinstance(error, AttemptCancelled)
        # 这个步骤没写 compensate，靠 run_step_flow 收尾的 resolver.close_all() → PhoneVerification.close()
        assert provider.events[-1] == "cancel"
        assert phone.active_lease is None
        assert phone._verify_lock_acquired is False
        assert lock_is_free_from_another_thread()

    def test_lock_timeout_surfaces_as_provider_error_instead_of_hanging(self, monkeypatch):
        import core.phone_verification as pv
        from core.errors import ProviderError

        provider = _ExclusiveProvider()
        phone = self._real_phone(monkeypatch, provider)

        # 模拟锁被别的线程泄漏：在另一个线程里拿住不放
        holder_ready = threading.Event()
        holder_release = threading.Event()

        def hold():
            _SMS_VERIFY_WINDOW_LOCK.acquire()
            holder_ready.set()
            holder_release.wait(5)
            _SMS_VERIFY_WINDOW_LOCK.release()

        thread = threading.Thread(target=hold)
        thread.start()
        holder_ready.wait(2)
        try:
            with pytest.raises(ProviderError, match="验证窗口"):
                phone._acquire_verify_lock(provider, timeout=1.2)
            assert provider.events == []
        finally:
            holder_release.set()
            thread.join(timeout=3)
        assert pv.VERIFY_WINDOW_LOCK_TIMEOUT >= 60


class TestCheckpointLifecycle:
    def test_phone_ref_is_written_while_held_and_cleared_after_success(self, fake_engine, tmp_path):
        from sqlmodel import Session

        from core.db import TaskModel, engine

        writes: list[dict] = []
        phone = _FakePhone()
        task_id, runtime, result, error = _run(
            phone, [_PhoneStep(), _TokenStep()], tmp_path=tmp_path, checkpoint_writes=writes
        )

        assert error is None
        # 租到号的那一刻就有一份带 ref 的 running 检查点——等码窗口内进程被杀也能回收
        running = [w for w in writes if w.get("phase") == "running"]
        assert running and running[0]["resource_refs"]["phone"]["activation_id"] == "act_1"
        # 成功释放后 ref 已清，重启回收不会再去 cancel 一个已完成的 activation
        assert "phone" not in writes[-1]["resource_refs"]

        with Session(engine) as session:
            task = session.get(TaskModel, task_id)
            attempts = task.get_result()["attempts"]
        checkpoint = attempts[f"{task_id}-1"]
        assert "token" not in checkpoint["state"]
        assert "password" not in checkpoint["state"]
        assert "phone_number" not in checkpoint["state"]


class TestSecretsStayOutOfTheApi:
    def test_serialize_task_hides_attempts(self, fake_engine, tmp_path):
        phone = _FakePhone()
        task_id, runtime, result, error = _run(phone, [_PhoneStep(), _TokenStep()], tmp_path=tmp_path)

        assert error is None
        payload = get_task(task_id)
        assert "attempts" not in payload["result"]
        dumped = repr(payload)
        assert "secret-session-token" not in dumped
        assert "Passw0rd!" not in dumped
