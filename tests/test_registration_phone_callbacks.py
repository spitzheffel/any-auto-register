from __future__ import annotations

from types import SimpleNamespace

from core.base_platform import RegisterConfig
from core.registration import BrowserRegistrationAdapter, BrowserRegistrationFlow, RegistrationContext, RegistrationResult
from core.registration.helpers import build_phone_callbacks
import core.registration.flows as flows_module
import core.registration.helpers as helpers_module
from infrastructure.provider_settings_repository import ProviderSettingsRepository


def test_browser_flow_wires_phone_callback_and_runs_cleanup(monkeypatch):
    events = []

    def fake_build_phone_callbacks(ctx, *, platform=None, service=None):
        events.append(("build", platform, service))
        return (
            lambda: "18885551234",
            lambda: events.append(("cleanup", platform, service)),
        )

    monkeypatch.setattr(flows_module, "build_phone_callbacks", fake_build_phone_callbacks)

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
        assert callable(artifacts.phone_callback)
        return SimpleNamespace(phone_callback=artifacts.phone_callback)

    def run_worker(worker, ctx, artifacts):
        events.append(("callback", worker.phone_callback()))
        return {"email": ctx.identity.email, "password": ctx.password}

    adapter = BrowserRegistrationAdapter(
        result_mapper=lambda ctx, raw: RegistrationResult(email=raw["email"], password=raw["password"]),
        browser_worker_builder=build_worker,
        browser_register_runner=run_worker,
    )

    result = BrowserRegistrationFlow(adapter).run(ctx)

    assert result.email == "user@example.com"
    assert ("build", "chatgpt", None) in events
    assert ("callback", "18885551234") in events
    assert ("cleanup", "chatgpt", None) in events


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

    def fake_create_phone_callbacks(provider_key, config, **kwargs):
        captured.update(provider_key=provider_key, config=config, **kwargs)
        return (lambda: "", lambda: None)

    monkeypatch.setattr(helpers_module, "create_phone_callbacks", fake_create_phone_callbacks)
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

    build_phone_callbacks(ctx, platform="chatgpt")
    assert captured["platform"] == "chatgpt"
    assert captured["service"] == ""
    assert captured["config"]["herosms_default_service"] == "full"

    ctx.config.extra["sms_service"] = "go"
    build_phone_callbacks(ctx, platform="chatgpt")
    assert captured["service"] == "go"
