"""渠道绑定：一个任务用哪几家 mailbox / sms / captcha / proxy。

解析顺序固定四层，命中层级记录在 ``ResolvedChannel.source`` 里，
供日志标注与前端展示：

    任务步骤级 > 任务级 > 平台默认 > 全局默认（is_default 或首个启用）

历史上三类渠道各有一套解析逻辑（tasks.py 读 mail_provider、helpers.py 读
sms_provider、base_platform 读 captcha_solver），这里统一收口；旧键作为别名读入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAILBOX = "mailbox"
SMS = "sms"
CAPTCHA = "captcha"
PROXY = "proxy"

CHANNEL_KINDS = (MAILBOX, SMS, CAPTCHA, PROXY)

SOURCE_STEP = "task_step"
SOURCE_TASK = "task"
SOURCE_PLATFORM = "platform_default"
SOURCE_GLOBAL = "global_default"
SOURCE_NONE = "unset"

# 旧任务参数键 → 渠道类别（前端切换到 channel_bindings 后仍可读一版）
LEGACY_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    MAILBOX: ("mail_provider",),
    SMS: ("sms_provider", "phone_provider"),
    CAPTCHA: ("captcha_solver",),
    PROXY: ("proxy_provider",),
}

LEGACY_FALLBACK_ALIASES: dict[str, tuple[str, ...]] = {
    MAILBOX: ("mail_provider_fallbacks",),
}

# captcha_solver 的历史特殊值：auto 表示"按策略自动选"，等价于未指定
AUTO_VALUES = {"", "auto", "default"}


@dataclass(slots=True)
class ResolvedChannel:
    kind: str
    key: str = ""
    source: str = SOURCE_NONE
    fallbacks: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "source": self.source,
            "fallbacks": list(self.fallbacks),
        }


@dataclass(slots=True)
class ChannelBinding:
    """单个渠道类别的绑定：任务级 key + 可选的按步骤覆写。"""

    key: str = ""
    fallbacks: tuple[str, ...] = ()
    steps: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.key:
            data["key"] = self.key
        if self.fallbacks:
            data["fallbacks"] = list(self.fallbacks)
        if self.steps:
            data["steps"] = dict(self.steps)
        return data


def _as_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item or "").strip() for item in value if str(item or "").strip())
    return ()


def _normalize_key(value: Any) -> str:
    key = str(value or "").strip()
    return "" if key.lower() in AUTO_VALUES else key


class ChannelBindings:
    """一个任务的渠道绑定集合。"""

    def __init__(self, bindings: dict[str, ChannelBinding] | None = None, *, platform: str = ""):
        self.platform = str(platform or "").strip()
        self._bindings: dict[str, ChannelBinding] = dict(bindings or {})

    # ------------------------------------------------------------------
    @classmethod
    def from_payload(cls, payload: dict | None, *, platform: str = "") -> "ChannelBindings":
        """从任务 payload 解析：优先 channel_bindings，其次 extra 里的旧键。"""
        payload = dict(payload or {})
        extra = dict(payload.get("extra") or {})
        raw = payload.get("channel_bindings") or extra.get("channel_bindings") or {}

        bindings: dict[str, ChannelBinding] = {}
        for kind in CHANNEL_KINDS:
            item = raw.get(kind) if isinstance(raw, dict) else None
            key = ""
            fallbacks: tuple[str, ...] = ()
            steps: dict[str, str] = {}
            if isinstance(item, str):
                key = _normalize_key(item)
            elif isinstance(item, dict):
                key = _normalize_key(item.get("key"))
                fallbacks = _as_list(item.get("fallbacks"))
                steps = {
                    str(step): _normalize_key(value)
                    for step, value in (item.get("steps") or {}).items()
                    if _normalize_key(value)
                }

            if not key:
                for alias in LEGACY_KEY_ALIASES.get(kind, ()):
                    key = _normalize_key(extra.get(alias) or payload.get(alias))
                    if key:
                        break
            if not fallbacks:
                for alias in LEGACY_FALLBACK_ALIASES.get(kind, ()):
                    fallbacks = _as_list(extra.get(alias) or payload.get(alias))
                    if fallbacks:
                        break

            bindings[kind] = ChannelBinding(key=key, fallbacks=fallbacks, steps=steps)

        return cls(bindings, platform=str(platform or payload.get("platform") or ""))

    def to_dict(self) -> dict[str, Any]:
        return {kind: binding.to_dict() for kind, binding in self._bindings.items() if binding.to_dict()}

    def binding(self, kind: str) -> ChannelBinding:
        return self._bindings.get(kind) or ChannelBinding()

    # ------------------------------------------------------------------
    def resolve(
        self,
        kind: str,
        *,
        step: str | None = None,
        platform_defaults: dict[str, str] | None = None,
    ) -> ResolvedChannel:
        """按 步骤级 > 任务级 > 平台默认 > 全局默认 的顺序解析。"""
        binding = self.binding(kind)

        if step:
            step_key = binding.steps.get(step) or binding.steps.get(str(step))
            if step_key:
                return ResolvedChannel(kind, step_key, SOURCE_STEP, binding.fallbacks)

        if binding.key:
            return ResolvedChannel(kind, binding.key, SOURCE_TASK, binding.fallbacks)

        platform_key = _normalize_key((platform_defaults or {}).get(kind))
        if platform_key:
            return ResolvedChannel(kind, platform_key, SOURCE_PLATFORM, binding.fallbacks)

        global_key = _normalize_key(global_default_key(kind))
        if global_key:
            return ResolvedChannel(kind, global_key, SOURCE_GLOBAL, binding.fallbacks)

        return ResolvedChannel(kind, "", SOURCE_NONE, binding.fallbacks)

    def resolve_all(
        self,
        kinds: tuple[str, ...] = CHANNEL_KINDS,
        *,
        platform_defaults: dict[str, str] | None = None,
    ) -> dict[str, ResolvedChannel]:
        return {
            kind: self.resolve(kind, platform_defaults=platform_defaults)
            for kind in kinds
        }


def global_default_key(kind: str) -> str:
    """全局默认 provider（设置页标记为默认的，或首个启用的）。"""
    try:
        from infrastructure.provider_settings_repository import ProviderSettingsRepository

        return str(ProviderSettingsRepository().get_default_provider_key(kind) or "")
    except Exception:
        return ""


def describe_resolution(resolved: dict[str, ResolvedChannel]) -> str:
    """给任务日志用的一行摘要。"""
    labels = {
        MAILBOX: "邮箱",
        SMS: "短信",
        CAPTCHA: "打码",
        PROXY: "代理",
    }
    source_labels = {
        SOURCE_STEP: "步骤指定",
        SOURCE_TASK: "手动指定",
        SOURCE_PLATFORM: "平台默认",
        SOURCE_GLOBAL: "全局默认",
        SOURCE_NONE: "未配置",
    }
    parts = []
    for kind, item in resolved.items():
        if not item.key and item.source == SOURCE_NONE:
            continue
        parts.append(f"{labels.get(kind, kind)}={item.key}（{source_labels.get(item.source, item.source)}）")
    return " ".join(parts)
