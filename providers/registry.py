"""Unified provider registry — auto-discovery + factory.

Usage::

    from providers.registry import register_provider, create_provider, load_all

    @register_provider("captcha", "yescaptcha_api")
    class YesCaptcha(BaseCaptcha):
        @classmethod
        def from_config(cls, config: dict) -> 'YesCaptcha':
            ...

    # At startup
    load_all()

    # Runtime creation
    instance = create_provider("captcha", "yescaptcha_api", config)

类别不是写死的：``providers/`` 下新建一个子包（例如 ``providers/voice_otp/``）
就会被 ``load_all()`` 自动发现，无需改动本文件。
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any

logger = logging.getLogger(__name__)

_registry: dict[str, dict[str, type]] = {}
#: 历史 driver 名 → 现名（DB 里旧的自定义 definition 可能还写着旧名）
_aliases: dict[str, dict[str, str]] = {}

_loaded = False


def register_provider(provider_type: str, driver_type: str, *, aliases: tuple[str, ...] = ()):
    """Decorator that registers a provider class in the global registry."""
    def decorator(cls):
        _registry.setdefault(provider_type, {})[driver_type] = cls
        for alias in aliases:
            register_alias(provider_type, alias, driver_type)
        return cls
    return decorator


def register_alias(provider_type: str, alias: str, driver_type: str) -> None:
    alias = str(alias or "").strip()
    if alias and alias != driver_type:
        _aliases.setdefault(provider_type, {})[alias] = driver_type


def resolve_driver_type(provider_type: str, driver_type: str) -> str:
    """把历史别名翻成当前注册名；不是别名就原样返回。"""
    key = str(driver_type or "").strip()
    return _aliases.get(provider_type, {}).get(key, key)


def get_provider_class(provider_type: str, driver_type: str) -> type | None:
    """Look up a registered provider class.  Returns *None* if not found."""
    load_all()
    return _registry.get(provider_type, {}).get(resolve_driver_type(provider_type, driver_type))


def create_provider(provider_type: str, driver_type: str, config: dict) -> Any:
    """Unified factory — create a provider instance from DB config.

    The provider class must expose a ``from_config(config)`` classmethod.
    """
    cls = get_provider_class(provider_type, driver_type)
    if cls is None:
        known = ", ".join(sorted(list_registered(provider_type))) or "(空)"
        raise ValueError(f"未注册的 provider: {provider_type}/{driver_type}（已注册: {known}）")
    factory = getattr(cls, "from_config", None)
    if factory is None:
        raise TypeError(f"{cls.__name__} 缺少 from_config 类方法")
    return factory(config)


def list_registered(provider_type: str) -> dict[str, type]:
    """Return ``{driver_type: cls}`` for a given provider type."""
    load_all()
    return dict(_registry.get(provider_type, {}))


def list_types() -> list[str]:
    """已注册的 provider 类别（mailbox / captcha / sms / proxy / …）。"""
    load_all()
    return sorted(_registry.keys())


def load_all() -> None:
    """Scan and import every provider module under ``providers/``."""
    global _loaded
    if _loaded:
        return
    # 先置位：provider 模块导入过程中若回调进来（create_provider），避免无限递归
    _loaded = True

    import providers

    for _finder, package_name, is_pkg in pkgutil.iter_modules(
        providers.__path__, providers.__name__ + "."
    ):
        if not is_pkg:
            continue
        try:
            package = importlib.import_module(package_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load provider package %s: %s", package_name, exc)
            continue
        for _f, module_name, _i in pkgutil.iter_modules(
            package.__path__, package.__name__ + "."
        ):
            try:
                importlib.import_module(module_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load provider module %s: %s", module_name, exc)
