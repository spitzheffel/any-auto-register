"""渠道需求推导、创建任务前置校验与渠道元数据。"""
from __future__ import annotations

import pytest

from application.platforms import PlatformsService, is_channel_configured
from application.tasks import MissingChannelError, resolve_task_channels
from core.requirements import (
    LEVEL_CONDITIONAL,
    LEVEL_REQUIRED,
    Requirement,
    from_adapter,
    from_step_flow,
    merge_requirements,
)
from infrastructure.provider_settings_repository import ProviderSettingsRepository


def _capabilities(items) -> dict[str, str]:
    return {item.capability: item.level for item in items}


class TestFromStepFlow:
    def test_guarded_steps_are_conditional(self):
        from platforms.cursor.obstacles import build_obstacles
        from platforms.cursor.steps import build_flow

        levels = _capabilities(from_step_flow(build_flow(), build_obstacles()))

        # 邮箱验证码每次都要；手机验证只在风控挑战时出现
        assert levels["otp"] == LEVEL_REQUIRED
        assert levels["phone"] == LEVEL_CONDITIONAL
        # Turnstile 不一定弹，但弹了就要 solver
        assert levels["captcha"] == LEVEL_CONDITIONAL

    def test_merge_prefers_the_stronger_level(self):
        merged = _capabilities(
            merge_requirements(
                [
                    Requirement("captcha", LEVEL_CONDITIONAL),
                    Requirement("captcha", LEVEL_REQUIRED),
                ]
            )
        )
        assert merged["captcha"] == LEVEL_REQUIRED


class TestFromAdapter:
    def test_protocol_adapter_declares_otp_and_captcha(self):
        from core.registration import ProtocolMailboxAdapter, OtpSpec

        adapter = ProtocolMailboxAdapter(
            result_mapper=lambda ctx, raw: raw,
            worker_builder=lambda ctx, artifacts: None,
            register_runner=lambda worker, ctx, artifacts: None,
            otp_spec=OtpSpec(),
            use_captcha=True,
            use_phone=True,
        )
        levels = _capabilities(from_adapter(adapter))

        assert levels["otp"] == LEVEL_REQUIRED
        assert levels["captcha"] == LEVEL_REQUIRED
        assert levels["phone"] == LEVEL_CONDITIONAL
        assert levels["mailbox"] == LEVEL_REQUIRED

    def test_oauth_needs_no_channels(self):
        from core.registration import BrowserRegistrationAdapter

        adapter = BrowserRegistrationAdapter(result_mapper=lambda ctx, raw: raw)
        assert from_adapter(adapter, identity_provider="oauth_browser") == []

    def test_browser_mode_flags_phone_as_conditional(self):
        from core.registration import BrowserRegistrationAdapter, OtpSpec

        adapter = BrowserRegistrationAdapter(result_mapper=lambda ctx, raw: raw, otp_spec=OtpSpec())
        levels = _capabilities(from_adapter(adapter, browser_mode=True))

        assert levels["phone"] == LEVEL_CONDITIONAL


class TestDescribeRequirementsApi:
    def test_cursor_modes_differ(self, client):
        service = PlatformsService()

        protocol = service.describe_requirements("cursor", executor_type="protocol")
        headless = service.describe_requirements("cursor", executor_type="headless")
        oauth = service.describe_requirements("cursor", executor_type="headed", identity_provider="oauth_browser")

        assert "sms" in protocol["channels"]
        assert "mailbox" in headless["channels"]
        # OAuth 复用已登录浏览器，不需要任何渠道
        assert oauth["requirements"] == []
        assert oauth["channels"] == {}

    def test_endpoint_reports_configured_state(self, client):
        response = client.get(
            "/api/platforms/cursor/requirements",
            params={"executor_type": "headless", "identity_provider": "mailbox"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["platform"] == "cursor"
        assert all("configured" in item for item in body["channels"].values())

    def test_unknown_platform_is_404(self, client):
        assert client.get("/api/platforms/nope/requirements").status_code == 404

    def test_infrastructure_error_is_500_not_404(self, client, monkeypatch):
        # DB 挂了不能伪装成"平台不存在"，否则前端会当成正常空需求
        from api import platforms as platforms_api

        def boom(*args, **kwargs):
            raise RuntimeError("db is down")

        monkeypatch.setattr(platforms_api.service, "describe_requirements", boom)
        assert client.get("/api/platforms/cursor/requirements").status_code == 500

    def test_selected_follows_user_choice(self, client):
        ProviderSettingsRepository().save(
            setting_id=None,
            provider_type="sms",
            provider_key="herosms_api",
            display_name="HeroSMS",
            auth_mode="apikey",
            enabled=True,
            is_default=True,
            config={},
            auth={"herosms_api_key": "hero123"},
            metadata={},
        )
        service = PlatformsService()

        default = service.describe_requirements("cursor", executor_type="protocol")
        assert default["channels"]["sms"]["selected"] == "herosms_api"
        assert default["channels"]["sms"]["resolved_default"] == "herosms_api"
        assert default["channels"]["sms"]["source"] == "global_default"
        assert default["channels"]["sms"]["configured"] is True

        # 用户手动选了另一家（没配认证）：selected 跟着走，configured 按它算，
        # resolved_default 仍然是全局默认那家
        chosen = service.describe_requirements(
            "cursor",
            executor_type="protocol",
            channel_bindings={"sms": {"key": "smsbower_api"}},
        )
        assert chosen["channels"]["sms"]["selected"] == "smsbower_api"
        assert chosen["channels"]["sms"]["source"] == "task"
        assert chosen["channels"]["sms"]["resolved_default"] == "herosms_api"
        assert chosen["channels"]["sms"]["configured"] is False

    def test_endpoint_accepts_provider_query_params(self, client):
        response = client.get(
            "/api/platforms/cursor/requirements",
            params={"executor_type": "protocol", "sms_provider": "smsbower_api"},
        )
        assert response.status_code == 200
        sms = response.json()["channels"]["sms"]
        assert sms["selected"] == "smsbower_api"
        assert sms["source"] == "task"


class TestChannelConfiguredProbe:
    def test_unconfigured_provider(self, client):
        assert is_channel_configured("sms", "herosms_api") is False
        assert is_channel_configured("sms", "") is False

    def test_extra_key_counts_as_configured(self, client):
        # 只在表单里填了 key、没存设置页：和运行期一样叠上 extra 后应视为已配置
        assert is_channel_configured("sms", "herosms_api", {"herosms_api_key": "from-form"}) is True
        assert is_channel_configured("sms", "herosms_api", {"unrelated": "x"}) is False

    def test_configured_provider(self, client):
        ProviderSettingsRepository().save(
            setting_id=None,
            provider_type="sms",
            provider_key="herosms_api",
            display_name="HeroSMS",
            auth_mode="apikey",
            enabled=True,
            is_default=True,
            config={},
            auth={"herosms_api_key": "hero123"},
            metadata={},
        )
        assert is_channel_configured("sms", "herosms_api") is True


class TestResolveTaskChannels:
    def _seed_mailbox(self):
        ProviderSettingsRepository().save(
            setting_id=None,
            provider_type="mailbox",
            provider_key="moemail_api",
            display_name="MoeMail",
            auth_mode="password",
            enabled=True,
            is_default=True,
            config={"moemail_api_url": "https://moemail.example"},
            auth={"moemail_password": "pw"},
            metadata={},
        )

    def test_pins_resolved_channels_into_payload(self, client):
        self._seed_mailbox()
        payload = resolve_task_channels(
            {"platform": "cursor", "executor_type": "protocol", "extra": {"captcha_solver": "manual"}}
        )

        assert payload["channel_bindings"]["captcha"]["key"] == "manual"
        assert payload["resolved_channels"]["captcha"]["key"] == "manual"
        assert payload["resolved_channels"]["captcha"]["source"] == "task"
        assert payload["resolved_channels"]["mailbox"]["source"] == "global_default"

    def test_rejects_missing_required_channel(self, client):
        # 什么都没配：邮箱是必需渠道，应当在创建任务时就拒掉
        with pytest.raises(MissingChannelError) as exc:
            resolve_task_channels({"platform": "cursor", "executor_type": "protocol", "extra": {}})

        channels = {item["channel"] for item in exc.value.missing}
        assert "mailbox" in channels

    def test_conditional_channel_does_not_block(self, client):
        self._seed_mailbox()
        payload = resolve_task_channels(
            {"platform": "cursor", "executor_type": "headless", "extra": {"captcha_solver": "manual"}}
        )
        # 短信是 conditional：没配也允许创建，只是跑到那一步才可能失败
        assert payload["resolved_channels"]["sms"]["key"] == ""

    def test_legacy_keys_still_work(self, client):
        self._seed_mailbox()
        payload = resolve_task_channels(
            {
                "platform": "cursor",
                "executor_type": "protocol",
                "extra": {"mail_provider": "moemail_api", "captcha_solver": "manual"},
            }
        )
        assert payload["resolved_channels"]["mailbox"]["key"] == "moemail_api"
        assert payload["resolved_channels"]["mailbox"]["source"] == "task"
