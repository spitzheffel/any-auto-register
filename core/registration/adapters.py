from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .models import RegistrationCapability, RegistrationContext, RegistrationArtifacts, RegistrationResult


@dataclass(slots=True)
class OtpSpec:
    keyword: str = ""
    timeout: int | None = None
    code_pattern: str | None = None
    wait_message: str = "等待验证码..."
    success_label: str = "验证码"


@dataclass(slots=True)
class LinkSpec:
    keyword: str = ""
    timeout: int | None = None
    wait_message: str = "等待验证链接邮件..."
    success_label: str = "验证链接"
    preview_chars: int = 80


@dataclass(slots=True)
class BrowserRegistrationAdapter:
    result_mapper: Callable[[RegistrationContext, Any], RegistrationResult]
    browser_worker_builder: Callable[[RegistrationContext, RegistrationArtifacts], Any] | None = None
    browser_register_runner: Callable[[Any, RegistrationContext, RegistrationArtifacts], Any] | None = None
    oauth_runner: Callable[[RegistrationContext], Any] | None = None
    capability: RegistrationCapability = field(default_factory=RegistrationCapability)
    otp_spec: OtpSpec | None = None
    link_spec: LinkSpec | None = None
    use_captcha_for_mailbox: bool = False
    preflight: Callable[[RegistrationContext], None] | None = None
    # 提供步骤流的平台走 StepRunner；未提供的仍走上面的 worker 路径
    step_flow_builder: Callable[[], Any] | None = None
    obstacle_builder: Callable[[], Any] | None = None
    engine_preference: str = ""
    allow_engine_fallback: bool = False


@dataclass(slots=True)
class ProtocolMailboxAdapter:
    result_mapper: Callable[[RegistrationContext, Any], RegistrationResult]
    worker_builder: Callable[[RegistrationContext, RegistrationArtifacts], Any]
    register_runner: Callable[[Any, RegistrationContext, RegistrationArtifacts], Any]
    capability: RegistrationCapability = field(default_factory=RegistrationCapability)
    otp_spec: OtpSpec | None = None
    link_spec: LinkSpec | None = None
    use_captcha: bool = False
    use_executor: bool = False
    use_phone: bool = False
    preflight: Callable[[RegistrationContext], None] | None = None


@dataclass(slots=True)
class ProtocolOAuthAdapter:
    oauth_runner: Callable[[RegistrationContext], Any]
    result_mapper: Callable[[RegistrationContext, Any], RegistrationResult]
    capability: RegistrationCapability = field(default_factory=RegistrationCapability)
    preflight: Callable[[RegistrationContext], None] | None = None
