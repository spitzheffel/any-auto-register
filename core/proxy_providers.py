"""动态代理 IP 提供者 — 具体实现已迁移到 providers/proxy/

支持两种模式:
  1. 静态代理: 从数据库读取固定代理列表（现有逻辑）
  2. 动态代理: 从第三方 API 实时获取代理 IP

动态代理 provider 通过 provider_settings 配置；如果未配置则自动回退到静态代理池。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

import requests  # noqa: F401 - 既有测试以 core.proxy_providers.requests.get 为 patch 目标

logger = logging.getLogger(__name__)


class BaseProxyProvider(ABC):
    """动态代理提供者基类。"""

    @abstractmethod
    def get_proxy(self) -> Optional[str]:
        """获取一个代理 URL，格式: http://host:port 或 http://user:pass@host:port。
        返回 None 表示无可用代理。"""
        ...


# ---------------------------------------------------------------------------
# Lazy re-exports for backward compatibility
# (concrete classes now live under providers/proxy/)
# ---------------------------------------------------------------------------
_LAZY_IMPORTS = {
    "ApiExtractProvider": "providers.proxy.api_extract",
    "RotatingProxyProvider": "providers.proxy.rotating_gateway",
}


def __getattr__(name: str):
    module_path = _LAZY_IMPORTS.get(name)
    if module_path is not None:
        import importlib
        mod = importlib.import_module(module_path)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_proxy_provider(provider_key: str, config: dict) -> BaseProxyProvider:
    """根据 provider_key 和配置创建代理提供者（走统一 registry）。"""
    from providers.registry import create_provider

    key = str(provider_key or "").strip()
    try:
        from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

        definition = ProviderDefinitionsRepository().get_by_key("proxy", key)
        if definition and str(definition.driver_type or "").strip():
            key = str(definition.driver_type).strip()
    except Exception:
        pass

    from providers.registry import get_provider_class

    # 先判 driver 是否存在，再交给 from_config：否则 from_config 里的配置解析 ValueError
    # 会被误报成"未知 provider"
    if get_provider_class("proxy", key) is None:
        raise RuntimeError(f"未知的代理 provider: {provider_key}")
    return create_provider("proxy", key, dict(config or {}))


def get_dynamic_proxy(extra: dict | None = None) -> Optional[str]:
    """尝试从配置的动态代理 provider 获取代理。

    如果未配置动态代理，返回 None（回退到静态代理池）。
    """
    try:
        from infrastructure.provider_settings_repository import ProviderSettingsRepository
        repo = ProviderSettingsRepository()
        settings = repo.list_enabled("proxy")
        for setting in settings:
            if not setting.enabled:
                continue
            config = setting.get_config()
            auth = setting.get_auth()
            merged = {**config, **auth, **(extra or {})}
            try:
                provider = create_proxy_provider(setting.provider_key, merged)
                proxy = provider.get_proxy()
                if proxy:
                    return proxy
            except Exception as exc:
                logger.debug(f"[ProxyProvider] {setting.provider_key} 获取失败: {exc}")
                continue
    except Exception:
        pass
    return None
