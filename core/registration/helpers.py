from __future__ import annotations

from typing import Any

from core.base_mailbox import accepts_should_stop
from core.phone_verification import PhoneVerification, create_phone_verification

from .errors import BrowserReuseRequiredError, IdentityResolutionError, RegistrationUnsupportedError
from .models import RegistrationContext


def has_reusable_oauth_browser(identity: Any) -> bool:
    return bool((getattr(identity, "chrome_user_data_dir", "") or "").strip() or (getattr(identity, "chrome_cdp_url", "") or "").strip())


def resolve_timeout(extra: dict[str, Any], keys: tuple[str, ...], default: int) -> int:
    for key in keys:
        value = extra.get(key)
        if value not in (None, ""):
            return int(value)
    return int(default)


def ensure_identity_email(ctx: RegistrationContext, message: str) -> None:
    if not getattr(ctx.identity, "email", ""):
        raise IdentityResolutionError(message)


def ensure_mailbox_identity(ctx: RegistrationContext, message: str) -> None:
    if not getattr(ctx.identity, "has_mailbox", False):
        raise IdentityResolutionError(message)


def ensure_oauth_executor_allowed(ctx: RegistrationContext, allowed_executor_types: tuple[str, ...] | None, message: str | None = None) -> None:
    if not allowed_executor_types:
        return
    if ctx.executor_type not in allowed_executor_types:
        expected = ", ".join(allowed_executor_types)
        raise RegistrationUnsupportedError(message or f"{ctx.platform_display_name} 当前 OAuth 仅支持 executor_type={expected}")


def ensure_oauth_browser_reuse(ctx: RegistrationContext, message: str) -> None:
    if not has_reusable_oauth_browser(ctx.identity):
        raise BrowserReuseRequiredError(message)


def build_otp_callback(
    ctx: RegistrationContext,
    *,
    keyword: str = "",
    timeout: int | None = None,
    code_pattern: str | None = None,
    wait_message: str = "等待验证码...",
    success_label: str = "验证码",
):
    mailbox = getattr(ctx.platform, "mailbox", None)
    mail_acct = getattr(ctx.identity, "mailbox_account", None)
    if not mailbox or not mail_acct:
        return None

    # 同一次注册可能多次要码（注册 + OAuth 登录）。before_ids 只是身份创建时的
    # 一次性快照，如果不累积，第二次要码会重新扫到上一封验证邮件并返回旧验证码。
    seen_ids: set = set(getattr(ctx.identity, "before_ids", None) or set())

    def otp_cb(*, should_stop=None):
        ctx.log(wait_message)
        kwargs = {"keyword": keyword, "before_ids": set(seen_ids)}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if code_pattern:
            kwargs["code_pattern"] = code_pattern
        if should_stop is not None and accepts_should_stop(mailbox.wait_for_code):
            kwargs["should_stop"] = should_stop
        code = mailbox.wait_for_code(mail_acct, **kwargs)
        if code:
            ctx.log(f"{success_label}: {code}")
            try:
                seen_ids.update(mailbox.get_current_ids(mail_acct) or set())
            except Exception:
                pass
        return code

    return otp_cb


def build_phone_verification(
    ctx: RegistrationContext,
    *,
    platform: str | None = None,
    service: str | None = None,
    provider_key: str = "",
    human=None,
) -> PhoneVerification | None:
    from core.channel_bindings import SMS, ChannelBindings
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    extra = ctx.extra
    settings_repo = ProviderSettingsRepository()
    definitions_repo = ProviderDefinitionsRepository()

    provider_key = str(provider_key or "").strip()
    source = "capability binding"
    if not provider_key:
        # 任务创建时钉住的绑定优先；没有运行时（脚本直调）再从 extra 旧键解析
        bindings = getattr(getattr(ctx, "runtime", None), "bindings", None)
        if bindings is None:
            bindings = ChannelBindings.from_payload({"extra": extra}, platform=ctx.platform_name)
        resolved = bindings.resolve(SMS)
        provider_key = resolved.key
        source = resolved.source
    if not provider_key and extra.get("sms_activate_api_key"):
        provider_key = "sms_activate"
        source = "legacy sms_activate_api_key"
    if not provider_key:
        ctx.log("[SMS] 未配置 SMS provider（任务参数/全局默认/历史兼容字段都为空），phone=None — 注册到 add_phone 步骤将抛错")
        return None

    definition = definitions_repo.get_by_key("sms", provider_key)
    merged = settings_repo.resolve_runtime_settings("sms", provider_key, extra) if definition else dict(extra)

    auth_fields = []
    if definition:
        auth_fields = [
            str(field.get("key") or "").strip()
            for field in definition.get_fields()
            if str(field.get("category") or "").strip() == "auth"
        ]
    if auth_fields and not any(str(merged.get(field_key, "")).strip() for field_key in auth_fields):
        ctx.log(f"[SMS] provider={provider_key} (来源={source}) 已找到 definition，但认证字段 {auth_fields} 全部为空，phone=None")
        return None

    if ctx.proxy and not str(merged.get("sms_proxy") or merged.get("proxy") or "").strip():
        merged["sms_proxy"] = ctx.proxy
    if human is None:
        human = getattr(getattr(ctx, "runtime", None), "human", None)
    if human is not None:
        # 人工接码 provider 从这里拿到通道；只进本次的 config 副本
        merged["human_channel"] = human

    # 只认通用键：厂商前缀键（herosms_*/smsbower_*/sms_activate_*）由各 provider
    # 自己在 from_config 里读，不再在通用层堆 or 链
    country = str(merged.get("sms_country") or merged.get("phone_country") or "").strip()
    platform_name = str(platform or ctx.platform_name or "").strip().lower()
    explicit_service = str(service or extra.get("sms_service") or "").strip().lower()
    ctx.log(
        f"[SMS] phone 渠道已就绪: provider={provider_key}(来源={source}) "
        f"platform={platform_name or 'unknown'} "
        f"service_override={explicit_service or 'none'} "
        f"默认country={country or 'default'}"
    )
    return create_phone_verification(
        provider_key,
        merged,
        platform=platform_name,
        service=explicit_service,
        country=country,
        log_fn=ctx.log,
    )


def build_link_callback(
    ctx: RegistrationContext,
    *,
    keyword: str = "",
    timeout: int | None = None,
    wait_message: str = "等待验证链接邮件...",
    success_label: str = "验证链接",
    preview_chars: int = 80,
):
    mailbox = getattr(ctx.platform, "mailbox", None)
    mail_acct = getattr(ctx.identity, "mailbox_account", None)
    if not mailbox or not mail_acct:
        return None

    def link_cb(*, should_stop=None):
        ctx.log(wait_message)
        before_ids = mailbox.get_current_ids(mail_acct)
        kwargs = {"keyword": keyword, "before_ids": before_ids}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if should_stop is not None and accepts_should_stop(mailbox.wait_for_link):
            kwargs["should_stop"] = should_stop
        link = mailbox.wait_for_link(mail_acct, **kwargs)
        if link:
            preview = link if len(link) <= preview_chars else f"{link[:preview_chars]}..."
            ctx.log(f"{success_label}: {preview}")
        return link

    return link_cb
