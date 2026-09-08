"""检查点写入与中断资源回收。"""
from __future__ import annotations

from types import SimpleNamespace

import core.checkpoints as checkpoints
from core.checkpoints import (
    TaskCheckpointSink,
    iter_attempt_checkpoints,
    release_attempt_resources,
    sanitize_refs,
    sanitize_state,
)


class FakeTask:
    def __init__(self):
        self._result = {}

    def get_result(self):
        return dict(self._result)

    def set_result(self, data):
        self._result = dict(data)


def test_sanitize_state_drops_unserializable_handles_and_secrets():
    """检查点会经任务接口下发：密码 / token / 手机号一律不落。"""
    state = sanitize_state(
        {"email": "a@b.c", "password": "x", "token": "tok", "phone_number": "+1555", "browser": object()}
    )

    assert state == {"email": "a@b.c"}


def test_sanitize_refs_keeps_only_what_reclaim_needs():
    refs = sanitize_refs(
        {
            "phone": {"provider_key": "herosms_api", "activation_id": "act_1", "phone_number": "+1555"},
            "account": {"email": "a@b.c", "submitted": True, "password": "secret"},
        }
    )

    assert refs["phone"] == {"provider_key": "herosms_api", "activation_id": "act_1"}
    assert refs["account"] == {"email": "a@b.c", "submitted": True}


def test_sink_writes_attempt_entry():
    task = FakeTask()

    def mutate(task_id, fn):
        fn(task)

    sink = TaskCheckpointSink("task_1", mutate=mutate)
    sink.write(
        attempt_id="a1",
        cursor=3,
        step="MaybePhoneVerify",
        state={"email": "a@b.c", "lease": object()},
        resource_refs={"phone": {"provider_key": "herosms_api", "activation_id": "act_1"}},
        channels_used={"sms": "herosms_api"},
        phase="running",
    )

    entry = task.get_result()["attempts"]["a1"]
    assert entry["cursor"] == 3
    assert entry["step"] == "MaybePhoneVerify"
    assert entry["phase"] == "running"
    assert entry["state"] == {"email": "a@b.c"}
    assert entry["resource_refs"]["phone"]["activation_id"] == "act_1"
    assert entry["channels_used"] == {"sms": "herosms_api"}


def test_mark_done_clears_refs_so_reclaim_skips_it():
    task = FakeTask()

    def mutate(task_id, fn):
        fn(task)

    sink = TaskCheckpointSink("task_1", mutate=mutate)
    sink.write(
        attempt_id="a1",
        cursor=7,
        step="ExtractSessionToken",
        state={},
        resource_refs={"phone": {"provider_key": "herosms_api", "activation_id": "act_1"}},
        channels_used={},
    )
    sink.mark_done("a1")

    entry = task.get_result()["attempts"]["a1"]
    assert entry["done"] is True
    assert entry["resource_refs"] == {}


def test_iter_attempt_checkpoints_skips_garbage():
    result = {"attempts": {"a1": {"cursor": 1}, "a2": "not-a-dict"}}
    assert [key for key, _ in iter_attempt_checkpoints(result)] == ["a1"]


class TestReleaseResources:
    def test_releases_phone_lease(self, monkeypatch):
        released = []

        class FakeLease:
            def release(self, outcome="unused"):
                released.append(outcome)

        monkeypatch.setattr(
            "core.phone_verification.PhoneVerification.reclaim",
            classmethod(lambda cls, ref, **kwargs: FakeLease()),
        )

        messages = release_attempt_resources(
            {"resource_refs": {"phone": {"provider_key": "herosms_api", "activation_id": "act_9"}}}
        )

        assert released == ["unused"]
        assert "act_9" in messages[0]

    def test_releases_mailbox_account_with_a_single_provider_instance(self, monkeypatch):
        """按引用里的 provider 单独建实例：不能走 create_mailbox 的 fallback 链，
        FallbackMailbox 认不出该找哪家释放。"""
        released = []
        created = []

        class FakeMailbox:
            def release(self, account, reason="", platform=""):
                released.append((account.email, reason, platform, account.extra.get("mailbox_provider_key")))

        class FakeDefinition:
            driver_type = "moemail_api"

            def get_metadata(self):
                return {}

        monkeypatch.setattr(
            "infrastructure.provider_definitions_repository.ProviderDefinitionsRepository.get_by_key",
            lambda self, provider_type, key: FakeDefinition(),
        )
        monkeypatch.setattr(
            "infrastructure.provider_settings_repository.ProviderSettingsRepository.resolve_runtime_settings",
            lambda self, provider_type, key, overrides=None: {"moemail_key": "k", **(overrides or {})},
        )

        def create_provider(kind, driver_type, config):
            created.append((kind, driver_type, config.get("moemail_key"), config.get("inline")))
            return FakeMailbox()

        monkeypatch.setattr("providers.registry.create_provider", create_provider)

        messages = release_attempt_resources(
            {"resource_refs": {"mailbox": {"provider_key": "moemail_api", "email": "x@y.z"}}},
            platform="cursor",
            extra={"inline": "task-extra"},
        )

        assert created == [("mailbox", "moemail_api", "k", "task-extra")]
        assert released == [("x@y.z", "interrupted", "cursor", "moemail_api")]
        assert "x@y.z" in messages[0]

    def test_phone_reclaim_receives_task_extra(self, monkeypatch):
        seen = {}

        class FakeLease:
            def release(self, outcome="unused"):
                seen["outcome"] = outcome

        def reclaim(cls, ref, **kwargs):
            seen["config"] = kwargs.get("config")
            return FakeLease()

        monkeypatch.setattr("core.phone_verification.PhoneVerification.reclaim", classmethod(reclaim))
        monkeypatch.setattr(
            "core.phone_verification.resolve_sms_settings",
            lambda key, overrides=None: {"provider": key, **(overrides or {})},
        )

        release_attempt_resources(
            {"resource_refs": {"phone": {"provider_key": "herosms_api", "activation_id": "act_1"}}},
            extra={"herosms_api_key": "only-in-task"},
        )

        # 只填在任务参数里的 key 也要传到回收路径，否则重启后 from_config 会因缺 key 失败
        assert seen["config"]["herosms_api_key"] == "only-in-task"
        assert seen["outcome"] == "unused"

    def test_failure_is_reported_not_raised(self, monkeypatch):
        def boom(cls, ref, **kwargs):
            raise RuntimeError("网络不通")

        monkeypatch.setattr(
            "core.phone_verification.PhoneVerification.reclaim",
            classmethod(boom),
        )

        messages = release_attempt_resources(
            {"resource_refs": {"phone": {"provider_key": "herosms_api", "activation_id": "act_x"}}}
        )

        assert "回收手机号失败" in messages[0]

    def test_no_refs_means_nothing_to_do(self):
        assert release_attempt_resources({}) == []
        assert release_attempt_resources({"resource_refs": {}}) == []
