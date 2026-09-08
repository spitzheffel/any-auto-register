from __future__ import annotations

from application.tasks import _resolve_oversubscribe_policy, _resolve_sms_provider_for_task
from infrastructure.provider_settings_repository import ProviderSettingsRepository


def test_resolve_sms_provider_for_task_uses_saved_herosms_default(client):
    repo = ProviderSettingsRepository()
    repo.save(
        setting_id=None,
        provider_type="sms",
        provider_key="herosms_api",
        display_name="HeroSMS",
        auth_mode="api_key",
        enabled=True,
        is_default=True,
        config={
            "herosms_default_service": "dr",
            "herosms_default_country": "187",
            "register_phone_extra_max": "3",
        },
        auth={"herosms_api_key": "hero123"},
        metadata={},
    )

    provider_key, settings = _resolve_sms_provider_for_task({})

    assert provider_key == "herosms_api"
    assert settings["herosms_api_key"] == "hero123"
    assert settings["herosms_default_service"] == "dr"


def test_resolve_sms_provider_for_task_allows_inline_override():
    provider_key, settings = _resolve_sms_provider_for_task({
        "sms_provider": "herosms_api",
        "herosms_api_key": "inline",
        "herosms_default_country": "52",
    })

    assert provider_key == "herosms_api"
    assert settings["herosms_api_key"] == "inline"
    assert settings["herosms_default_country"] == "52"


class TestOversubscribePolicy:
    """调度器只问 provider 通用问题，不认识任何厂商名。"""

    def test_reusable_provider_reports_compensation_and_oversubscribe(self):
        policy = _resolve_oversubscribe_policy("herosms_api", {
            "herosms_api_key": "hero123",
            "register_reuse_phone_to_max": "true",
            "register_phone_extra_max": "3",
        })

        assert policy["compensate_failures"] is True
        assert policy["reuse_to_max"] is True
        assert policy["extra_max"] == 3

    def test_reuse_disabled_still_compensates_failures(self):
        # 号码可复用的厂商即使关掉超发，失败也值得补投（重试几乎不额外花钱）
        policy = _resolve_oversubscribe_policy("herosms_api", {
            "herosms_api_key": "hero123",
            "register_reuse_phone_to_max": "false",
        })

        assert policy["compensate_failures"] is True
        assert policy["reuse_to_max"] is False

    def test_one_shot_provider_has_no_special_scheduling(self):
        policy = _resolve_oversubscribe_policy("sms_activate_api", {"sms_activate_api_key": "act123"})

        assert policy["compensate_failures"] is False
        assert policy["reuse_to_max"] is False
        assert policy["extra_max"] == 0

    def test_unconfigured_provider_yields_empty_policy(self):
        assert _resolve_oversubscribe_policy("herosms_api", {}) == {}
        assert _resolve_oversubscribe_policy("", {}) == {}
