"""验证码解决器基类 — 具体实现已迁移到 providers/captcha/

题型是数据不是方法：新增 reCAPTCHA / hCaptcha / Arkose 等只需加一个
``ChallengeKind`` 常量与对应的 handler，solver 用 ``SUPPORTED_KINDS`` 声明能力，
基类与既有实现都不用改。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ChallengeKind:
    TURNSTILE = "turnstile"
    IMAGE = "image"


@dataclass(slots=True)
class CaptchaChallenge:
    """一道待解的验证码题。"""

    kind: str
    page_url: str = ""
    site_key: str = ""
    payload: str = ""           # 图片验证码的 base64 等题面数据
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CaptchaSolution:
    """解题结果：token 类（Turnstile 等）或 text 类（图片验证码）。"""

    token: str = ""
    text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def value(self) -> str:
        return self.token or self.text


class BaseCaptcha(ABC):
    #: 该 solver 能处理的题型；留空表示"什么都不支持"
    SUPPORTED_KINDS: tuple[str, ...] = ()

    @abstractmethod
    def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution:
        """解一道题；不支持的题型抛 NotImplementedError。"""
        ...

    @classmethod
    def supports(cls, kind: str) -> bool:
        return str(kind) in cls.SUPPORTED_KINDS

    # -- 兼容既有平台调用点的薄包装（不再是抽象方法） ------------------
    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        return self.solve(
            CaptchaChallenge(kind=ChallengeKind.TURNSTILE, page_url=page_url, site_key=site_key)
        ).value

    def solve_image(self, image_b64: str) -> str:
        return self.solve(
            CaptchaChallenge(kind=ChallengeKind.IMAGE, payload=image_b64)
        ).value


# ---------------------------------------------------------------------------
# Lazy re-exports for backward compatibility
# (concrete classes now live under providers/captcha/)
# ---------------------------------------------------------------------------
_LAZY_IMPORTS = {
    "YesCaptcha": "providers.captcha.yescaptcha",
    "TwoCaptcha": "providers.captcha.twocaptcha",
    "ManualCaptcha": "providers.captcha.manual",
    "LocalSolverCaptcha": "providers.captcha.local_solver",
}


def __getattr__(name: str):
    module_path = _LAZY_IMPORTS.get(name)
    if module_path is not None:
        import importlib
        mod = importlib.import_module(module_path)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _definition_auth_fields(definition) -> list[str]:
    if not definition:
        return []
    return [
        str(field.get("key") or "")
        for field in definition.get_fields()
        if str(field.get("category") or "") == "auth" and str(field.get("key") or "")
    ]


def has_captcha_configured(provider_key: str, extra: dict | None = None) -> bool:
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    key = str(provider_key or "").strip()
    if key == "manual":
        return True

    definition = ProviderDefinitionsRepository().get_by_key("captcha", key)
    if not definition or not definition.enabled:
        return False

    merged = ProviderSettingsRepository().resolve_runtime_settings("captcha", key, extra or {})
    auth_fields = _definition_auth_fields(definition)
    if not auth_fields:
        return True
    return any(str(merged.get(field_key, "")).strip() for field_key in auth_fields)


def create_captcha_solver(provider_key: str, extra: dict | None = None) -> BaseCaptcha:
    from core.registration.errors import CaptchaConfigurationError
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository
    from providers.registry import create_provider

    key = str(provider_key or "").strip().lower()
    if key == "manual":
        return create_provider("captcha", "manual", dict(extra or {}))

    definition = ProviderDefinitionsRepository().get_by_key("captcha", key)
    if not definition or not definition.enabled:
        raise CaptchaConfigurationError(f"验证码 provider 不存在或未启用: {key}")
    merged = ProviderSettingsRepository().resolve_runtime_settings("captcha", key, extra or {})
    driver_type = str(definition.driver_type or key).lower()
    return create_provider("captcha", driver_type, merged)
