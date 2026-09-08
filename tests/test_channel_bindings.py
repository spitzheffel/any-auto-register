"""渠道绑定的四层解析顺序与旧键兼容。"""
from __future__ import annotations

import pytest

from core.channel_bindings import (
    CAPTCHA,
    MAILBOX,
    SMS,
    SOURCE_GLOBAL,
    SOURCE_NONE,
    SOURCE_PLATFORM,
    SOURCE_STEP,
    SOURCE_TASK,
    ChannelBindings,
    describe_resolution,
)
import core.channel_bindings as bindings_module


@pytest.fixture()
def no_global_defaults(monkeypatch):
    monkeypatch.setattr(bindings_module, "global_default_key", lambda kind: "")


def test_step_level_wins_over_task_level(no_global_defaults):
    bindings = ChannelBindings.from_payload({
        "channel_bindings": {
            CAPTCHA: {"key": "yescaptcha_api", "steps": {"HandleTurnstile": "local_solver"}},
        }
    })

    step = bindings.resolve(CAPTCHA, step="HandleTurnstile")
    assert (step.key, step.source) == ("local_solver", SOURCE_STEP)

    task = bindings.resolve(CAPTCHA, step="SubmitPayment")
    assert (task.key, task.source) == ("yescaptcha_api", SOURCE_TASK)


def test_task_level_wins_over_platform_and_global(monkeypatch):
    monkeypatch.setattr(bindings_module, "global_default_key", lambda kind: "global_key")
    bindings = ChannelBindings.from_payload({"channel_bindings": {SMS: "smsbower_api"}})

    resolved = bindings.resolve(SMS, platform_defaults={SMS: "platform_key"})
    assert (resolved.key, resolved.source) == ("smsbower_api", SOURCE_TASK)


def test_platform_default_wins_over_global(monkeypatch):
    monkeypatch.setattr(bindings_module, "global_default_key", lambda kind: "global_key")
    bindings = ChannelBindings.from_payload({})

    resolved = bindings.resolve(CAPTCHA, platform_defaults={CAPTCHA: "twocaptcha_api"})
    assert (resolved.key, resolved.source) == ("twocaptcha_api", SOURCE_PLATFORM)


def test_falls_back_to_global_default(monkeypatch):
    monkeypatch.setattr(bindings_module, "global_default_key", lambda kind: "moemail_api")
    resolved = ChannelBindings.from_payload({}).resolve(MAILBOX)

    assert (resolved.key, resolved.source) == ("moemail_api", SOURCE_GLOBAL)


def test_unset_when_nothing_configured(no_global_defaults):
    resolved = ChannelBindings.from_payload({}).resolve(SMS)

    assert resolved.key == ""
    assert resolved.source == SOURCE_NONE
    assert resolved.configured is False


class TestLegacyAliases:
    def test_mail_provider_and_fallbacks(self, no_global_defaults):
        bindings = ChannelBindings.from_payload({
            "extra": {
                "mail_provider": "moemail_api",
                "mail_provider_fallbacks": "freemail_api, testmail_api",
            }
        })
        resolved = bindings.resolve(MAILBOX)

        assert (resolved.key, resolved.source) == ("moemail_api", SOURCE_TASK)
        assert resolved.fallbacks == ("freemail_api", "testmail_api")

    def test_sms_provider_and_phone_provider(self, no_global_defaults):
        assert ChannelBindings.from_payload({"extra": {"sms_provider": "herosms_api"}}).resolve(SMS).key == "herosms_api"
        assert ChannelBindings.from_payload({"extra": {"phone_provider": "smsbower_api"}}).resolve(SMS).key == "smsbower_api"

    def test_captcha_solver_auto_means_unspecified(self, no_global_defaults):
        resolved = ChannelBindings.from_payload({"extra": {"captcha_solver": "auto"}}).resolve(CAPTCHA)

        assert resolved.key == ""
        assert resolved.source == SOURCE_NONE

    def test_explicit_channel_bindings_beat_legacy_keys(self, no_global_defaults):
        bindings = ChannelBindings.from_payload({
            "channel_bindings": {SMS: {"key": "smsbower_api"}},
            "extra": {"sms_provider": "herosms_api"},
        })

        assert bindings.resolve(SMS).key == "smsbower_api"


def test_describe_resolution_labels_sources(monkeypatch):
    monkeypatch.setattr(bindings_module, "global_default_key", lambda kind: "moemail_api" if kind == MAILBOX else "")
    bindings = ChannelBindings.from_payload({"extra": {"sms_provider": "herosms_api"}})
    summary = describe_resolution(bindings.resolve_all((MAILBOX, SMS)))

    assert "邮箱=moemail_api（全局默认）" in summary
    assert "短信=herosms_api（手动指定）" in summary
