from __future__ import annotations

from types import SimpleNamespace

from core.base_platform import RegisterConfig
from core.registration import (
    BrowserRegistrationAdapter,
    BrowserRegistrationFlow,
    ProtocolMailboxAdapter,
    ProtocolMailboxFlow,
    RegistrationContext,
    RegistrationResult,
)
from core.registration.helpers import build_phone_verification
import core.registration.flows as flows_module
import core.registration.helpers as helpers_module
from infrastructure.provider_settings_repository import ProviderSettingsRepository


class FakePhoneVerification:
    """最小 PhoneVerification 替身：记录 acquire / wait_code / close 顺序。"""

    def __init__(self, events, label):
        self.events = events
        self.label = label
        self.closed = False

    def acquire(self):
        self.events.append(("acquire", self.label))
        return SimpleNamespace(
            phone_number="18885551234",
            wait_code=lambda **kwargs: "123456",
        )

    def close(self):
        self.closed = True
        self.events.append(("close", self.label))


def test_browser_flow_wires_phone_and_closes_it(monkeypatch):
    events = []

    def fake_build(ctx, *, platform=None, service=None):
        events.append(("build", platform, service))
        return FakePhoneVerification(events, platform)

    monkeypatch.setattr(flows_module, "build_phone_verification", fake_build)

    ctx = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(mailbox=None),
        identity=SimpleNamespace(
            email="user@example.com",
            has_mailbox=True,
            identity_provider="mailbox",
        ),
        config=RegisterConfig(executor_type="headless", extra={}),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )

    def build_worker(ctx, artifacts):
        assert artifacts.phone is not None
        return SimpleNamespace(phone=artifacts.phone)

    def run_worker(worker, ctx, artifacts):
        lease = worker.phone.acquire()
        events.append(("number", lease.phone_number))
        events.append(("code", lease.wait_code()))
        return {"email": ctx.identity.email, "password": ctx.password}

    adapter = BrowserRegistrationAdapter(
        result_mapper=lambda ctx, raw: RegistrationResult(email=raw["email"], password=raw["password"]),
        browser_worker_builder=build_worker,
        browser_register_runner=run_worker,
    )

    result = BrowserRegistrationFlow(adapter).run(ctx)

    assert result.email == "user@example.com"
    assert ("build", "chatgpt", None) in events
    assert ("number", "18885551234") in events
    assert ("code", "123456") in events
    assert events[-1] == ("close", "chatgpt")


def test_browser_flow_closes_phone_when_worker_raises(monkeypatch):
    events = []
    monkeypatch.setattr(
        flows_module,
        "build_phone_verification",
        lambda ctx, **kwargs: FakePhoneVerification(events, "chatgpt"),
    )

    ctx = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(mailbox=None),
        identity=SimpleNamespace(email="user@example.com", has_mailbox=True, identity_provider="mailbox"),
        config=RegisterConfig(executor_type="headless", extra={}),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )

    def boom(worker, ctx, artifacts):
        raise RuntimeError("注册失败")

    adapter = BrowserRegistrationAdapter(
        result_mapper=lambda ctx, raw: RegistrationResult(email="", password=""),
        browser_worker_builder=lambda ctx, artifacts: SimpleNamespace(),
        browser_register_runner=boom,
    )

    try:
        BrowserRegistrationFlow(adapter).run(ctx)
    except RuntimeError:
        pass
    else:  # pragma: no cover - 守卫
        raise AssertionError("worker 异常应向上传播")

    assert ("close", "chatgpt") in events


def _protocol_ctx() -> RegistrationContext:
    return RegistrationContext(
        platform_name="cursor",
        platform_display_name="Cursor",
        platform=SimpleNamespace(mailbox=None),
        identity=SimpleNamespace(
            email="user@example.com",
            has_mailbox=True,
            identity_provider="mailbox",
        ),
        config=RegisterConfig(executor_type="protocol", extra={}),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )


def test_protocol_flow_wires_phone_when_use_phone(monkeypatch):
    events = []

    def fake_build(ctx, *, platform=None, service=None):
        events.append(("build", platform, service))
        return FakePhoneVerification(events, platform)

    monkeypatch.setattr(flows_module, "build_phone_verification", fake_build)

    def build_worker(ctx, artifacts):
        assert artifacts.phone is not None
        return SimpleNamespace(phone=artifacts.phone)

    def run_worker(worker, ctx, artifacts):
        lease = worker.phone.acquire()
        events.append(("number", lease.phone_number))
        return {"email": ctx.identity.email, "password": ctx.password}

    adapter = ProtocolMailboxAdapter(
        result_mapper=lambda ctx, raw: RegistrationResult(email=raw["email"], password=raw["password"]),
        worker_builder=build_worker,
        register_runner=run_worker,
        use_phone=True,
    )

    result = ProtocolMailboxFlow(adapter).run(_protocol_ctx())

    assert result.email == "user@example.com"
    assert ("build", "cursor", None) in events
    assert ("number", "18885551234") in events
    assert events[-1] == ("close", "cursor")


def test_protocol_flow_skips_phone_by_default(monkeypatch):
    def fail_build(ctx, **kwargs):
        raise AssertionError("use_phone 未开启时不应构建 phone 渠道")

    monkeypatch.setattr(flows_module, "build_phone_verification", fail_build)

    adapter = ProtocolMailboxAdapter(
        result_mapper=lambda ctx, raw: RegistrationResult(email=raw["email"], password=raw["password"]),
        worker_builder=lambda ctx, artifacts: SimpleNamespace(),
        register_runner=lambda worker, ctx, artifacts: {"email": ctx.identity.email, "password": ctx.password},
    )

    result = ProtocolMailboxFlow(adapter).run(_protocol_ctx())
    assert result.email == "user@example.com"


def test_saved_default_service_is_fallback_not_task_override(client, monkeypatch):
    ProviderSettingsRepository().save(
        setting_id=None,
        provider_type="sms",
        provider_key="herosms_api",
        display_name="HeroSMS",
        auth_mode="apikey",
        enabled=True,
        is_default=True,
        config={
            "herosms_default_service": "full",
            "herosms_default_country": "187",
        },
        auth={"herosms_api_key": "hero123"},
        metadata={},
    )
    captured = {}

    def fake_create_phone_verification(provider_key, config, **kwargs):
        captured.update(provider_key=provider_key, config=config, **kwargs)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(helpers_module, "create_phone_verification", fake_create_phone_verification)
    ctx = RegistrationContext(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(mailbox=None),
        identity=SimpleNamespace(email="user@example.com"),
        config=RegisterConfig(
            executor_type="headless",
            extra={"sms_provider": "herosms_api"},
        ),
        email="user@example.com",
        password="Secret123!",
        log_fn=lambda message: None,
    )

    build_phone_verification(ctx, platform="chatgpt")
    assert captured["platform"] == "chatgpt"
    assert captured["service"] == ""
    assert captured["config"]["herosms_default_service"] == "full"

    ctx.config.extra["sms_service"] = "go"
    build_phone_verification(ctx, platform="chatgpt")
    assert captured["service"] == "go"
