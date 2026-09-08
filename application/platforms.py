from __future__ import annotations

from infrastructure.platform_runtime import PlatformRuntime


EXECUTOR_LABELS = {
    "protocol": "协议模式",
    "headless": "后台浏览器自动",
    "headed": "可视浏览器自动",
}

IDENTITY_MODE_LABELS = {
    "mailbox": "系统邮箱",
    "oauth_browser": "第三方账号",
}

OAUTH_PROVIDER_LABELS = {
    "google": "Google",
    "github": "GitHub",
    "microsoft": "Microsoft",
    "linkedin": "LinkedIn",
    "apple": "Apple",
    "x": "X",
    "builderid": "Builder ID",
}


def _choice_options(values: list[str], labels: dict[str, str]) -> list[dict]:
    return [
        {"value": value, "label": labels.get(value, value)}
        for value in values
        if str(value or "").strip()
    ]


def collect_platform_choice_options(platforms: list[dict]) -> dict[str, list[dict]]:
    executor_values: list[str] = []
    identity_values: list[str] = []
    oauth_values: list[str] = []
    for item in platforms:
        for value in item.get("supported_executors", []) or []:
            if value not in executor_values:
                executor_values.append(value)
        for value in item.get("supported_identity_modes", []) or []:
            if value not in identity_values:
                identity_values.append(value)
        for value in item.get("supported_oauth_providers", []) or []:
            if value not in oauth_values:
                oauth_values.append(value)
    return {
        "executor_options": _choice_options(executor_values, EXECUTOR_LABELS),
        "identity_mode_options": _choice_options(identity_values, IDENTITY_MODE_LABELS),
        "oauth_provider_options": _choice_options(oauth_values, OAUTH_PROVIDER_LABELS),
    }


class PlatformsService:
    def __init__(self, runtime: PlatformRuntime | None = None):
        self.runtime = runtime or PlatformRuntime()

    def list_platforms(self) -> list[dict]:
        result = []
        for item in self.runtime.list_platforms():
            result.append(
                {
                    "name": item.name,
                    "display_name": item.display_name,
                    "version": item.version,
                    "supported_executors": item.capabilities.supported_executors,
                    "supported_identity_modes": item.capabilities.supported_identity_modes,
                    "supported_oauth_providers": item.capabilities.supported_oauth_providers,
                    "supported_executor_options": _choice_options(item.capabilities.supported_executors, EXECUTOR_LABELS),
                    "supported_identity_mode_options": _choice_options(item.capabilities.supported_identity_modes, IDENTITY_MODE_LABELS),
                    "supported_oauth_provider_options": _choice_options(item.capabilities.supported_oauth_providers, OAUTH_PROVIDER_LABELS),
                }
            )
        return result

    def get_desktop_state(self, platform: str) -> dict:
        return self.runtime.get_desktop_state(platform)

    def describe_requirements(
        self,
        platform: str,
        *,
        executor_type: str = "protocol",
        identity_provider: str = "mailbox",
        channel_bindings: dict | None = None,
        extra: dict | None = None,
    ) -> dict:
        """本次任务需要哪些渠道 + 每类按当前选择会解析到谁、配没配。

        ``channel_bindings`` / ``extra`` 是用户在表单里的当前选择；不传时退化为
        全局默认。``configured`` 必须按用户实际选的那家算，否则表单里选了 A、
        提示却在说默认的 B 配没配。
        """
        from core.base_platform import RegisterConfig
        from core.channel_bindings import CHANNEL_KINDS, ChannelBindings
        from core.registry import get

        platform_cls = get(platform)
        instance = platform_cls(config=RegisterConfig(executor_type=executor_type))
        requirements = instance.describe_requirements(executor_type, identity_provider)

        bindings = ChannelBindings.from_payload(
            {"channel_bindings": channel_bindings or {}, "extra": extra or {}},
            platform=platform,
        )
        # 不带任何绑定的解析结果单独算一次：前端要能同时显示"你选的"和"不选会用哪家"
        defaults = ChannelBindings.from_payload({}, platform=platform)
        needed_channels = {item.channel for item in requirements if item.channel}
        channels = {}
        for kind in CHANNEL_KINDS:
            if kind not in needed_channels:
                continue
            resolved = bindings.resolve(kind)
            channels[kind] = {
                "selected": resolved.key,
                "resolved_default": defaults.resolve(kind).key,
                "source": resolved.source,
                "configured": is_channel_configured(kind, resolved.key, extra),
            }

        return {
            "platform": platform,
            "executor_type": executor_type,
            "identity_provider": identity_provider,
            "requirements": [item.to_dict() for item in requirements],
            "channels": channels,
        }


def is_channel_configured(kind: str, provider_key: str, extra: dict | None = None) -> bool:
    """该 provider 的认证字段是否填了（没有认证字段的视为已配置）。

    ``extra`` 是随任务提交的表单值，和运行期一样叠在已保存设置之上：
    只在表单里填了 key、没存设置页的用户不能被判成未配置。
    """
    if not provider_key:
        return False
    try:
        from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
        from infrastructure.provider_settings_repository import ProviderSettingsRepository

        definition = ProviderDefinitionsRepository().get_by_key(kind, provider_key)
        if not definition or not definition.enabled:
            return False
        auth_fields = [
            str(field.get("key") or "")
            for field in definition.get_fields()
            if str(field.get("category") or "") == "auth" and str(field.get("key") or "")
        ]
        if not auth_fields:
            return True
        merged = ProviderSettingsRepository().resolve_runtime_settings(kind, provider_key, extra or {})
        return any(str(merged.get(key, "")).strip() for key in auth_fields)
    except Exception:
        return False
