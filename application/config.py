from __future__ import annotations

from application.provider_definitions import ProviderDefinitionsService
from application.platforms import PlatformsService, collect_platform_choice_options
from application.provider_settings import ProviderSettingsService
from infrastructure.config_repository import ConfigRepository


class ConfigService:
    def __init__(self, repository: ConfigRepository | None = None):
        self.repository = repository or ConfigRepository()
        self.provider_definitions = ProviderDefinitionsService()
        self.provider_settings = ProviderSettingsService()
        self.platforms = PlatformsService()

    def get_config(self) -> dict[str, str]:
        return self.repository.get_flat()

    def update_config(self, data: dict[str, str]) -> dict:
        updated = self.repository.update_flat(data)
        return {"ok": True, "updated": updated}

    def get_options(self) -> dict:
        from core.browser_engine import DEFAULT_ENGINE, engine_spec, supported_engines

        platform_options = collect_platform_choice_options(self.platforms.list_platforms())
        # 内核列表由注册表下发：前端不再各自硬编码一份，新注册的内核自动出现在下拉里
        engine_labels = {"playwright": "Playwright", "patchright": "Patchright", "cloakbrowser": "CloakBrowser", "camoufox": "Camoufox"}
        browser_engine_options = []
        for name in supported_engines():
            spec = engine_spec(name)
            label = engine_labels.get(name, name)
            if name == DEFAULT_ENGINE:
                label = f"{label}（默认）"
            elif spec is not None and spec.family != "chromium":
                label = f"{label}（{spec.family} 系，仅隐身流程）"
            browser_engine_options.append({"label": label, "value": name})
        return {
            "browser_engine_options": browser_engine_options,
            "mailbox_providers": self.provider_definitions.list_definitions("mailbox", enabled_only=True),
            "captcha_providers": self.provider_definitions.list_definitions("captcha", enabled_only=True),
            "sms_providers": self.provider_definitions.list_definitions("sms", enabled_only=True),
            "mailbox_drivers": self.provider_definitions.list_driver_templates("mailbox"),
            "captcha_drivers": self.provider_definitions.list_driver_templates("captcha"),
            "sms_drivers": self.provider_definitions.list_driver_templates("sms"),
            "captcha_policy": self.provider_settings.get_captcha_policy(),
            "mailbox_settings": self.provider_settings.list_settings("mailbox"),
            "captcha_settings": self.provider_settings.list_settings("captcha"),
            "sms_settings": self.provider_settings.list_settings("sms"),
            **platform_options,
        }
