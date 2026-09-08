from __future__ import annotations

import uuid
from contextlib import nullcontext

from .adapters import BrowserRegistrationAdapter, ProtocolMailboxAdapter, ProtocolOAuthAdapter
from .helpers import (
    build_link_callback,
    build_otp_callback,
    build_phone_verification,
    ensure_identity_email,
    ensure_mailbox_identity,
    ensure_oauth_browser_reuse,
    ensure_oauth_executor_allowed,
)
from .models import RegistrationArtifacts, RegistrationContext, RegistrationResult


def run_step_flow(ctx: RegistrationContext, adapter: BrowserRegistrationAdapter) -> dict:
    """走 Step 引擎的浏览器注册：浏览器会话由这里开、由这里关。"""
    from core.browser_engine import start_browser
    from core.registration.runtime import StepRuntime
    from core.registration.steps import (
        ArtifactCollector,
        CapabilityResolver,
        NetworkRecorder,
        StepContext,
        StepRunner,
    )

    # 任务层注入的运行时；脚本直调时用一份空运行时（不取消、无人工通道、不落检查点）
    runtime = ctx.runtime if ctx.runtime is not None else StepRuntime()
    attempt_id = str(runtime.attempt_id or uuid.uuid4().hex[:12])

    flow = adapter.step_flow_builder()
    obstacles = adapter.obstacle_builder() if adapter.obstacle_builder else []

    artifacts = None
    if runtime.artifact_root:
        artifacts = ArtifactCollector(f"{runtime.artifact_root}/{attempt_id}", network=NetworkRecorder())

    with start_browser(
        engine=adapter.engine_preference or str(ctx.extra.get("browser_engine") or ""),
        headless=(ctx.executor_type != "headed"),
        proxy=ctx.proxy,
        log_fn=ctx.log,
        allow_fallback=adapter.allow_engine_fallback,
    ) as session:
        page = session.new_page()
        if artifacts is not None:
            artifacts.attach(page)

        resolver = CapabilityResolver(ctx, bindings=runtime.bindings, human=runtime.human)
        _apply_mail_specs(resolver, adapter)
        sc = StepContext(
            ctx,
            page=page,
            cancel=runtime.cancel,
            resolver=resolver,
            human=runtime.human,
            state={"email": ctx.identity.email or "", "password": ctx.password or ""},
            attempt_id=attempt_id,
        )
        sc.handles["browser_session"] = session

        runner = StepRunner(
            flow,
            obstacles=obstacles,
            checkpoint=runtime.checkpoint,
            artifacts=artifacts,
            hold_on_failure=runtime.hold_on_failure,
        )
        try:
            return runner.run(sc, start_at=runtime.start_at, state=runtime.state)
        finally:
            # 实际命中的渠道回写给任务层（统计与账号元数据要用）
            runtime.channels_used.update(sc.resolver.channels_used)
            # 失败 / 取消时仍持有的租约与会话在这里兜底关闭
            sc.resolver.close_all()


def _apply_mail_specs(resolver, adapter: BrowserRegistrationAdapter) -> None:
    """适配器声明的 OtpSpec / LinkSpec（关键词、超时、提示文案）在 Step 路径同样生效。"""
    otp_spec = adapter.otp_spec
    if otp_spec is not None:
        resolver.register(
            "otp",
            lambda ctx, step, key: build_otp_callback(
                ctx,
                keyword=otp_spec.keyword,
                timeout=otp_spec.timeout,
                code_pattern=otp_spec.code_pattern,
                wait_message=otp_spec.wait_message,
                success_label=otp_spec.success_label,
            ),
        )
    link_spec = adapter.link_spec
    if link_spec is not None:
        resolver.register(
            "link",
            lambda ctx, step, key: build_link_callback(
                ctx,
                keyword=link_spec.keyword,
                timeout=link_spec.timeout,
                wait_message=link_spec.wait_message,
                success_label=link_spec.success_label,
                preview_chars=link_spec.preview_chars,
            ),
        )


class BrowserRegistrationFlow:
    def __init__(self, adapter: BrowserRegistrationAdapter):
        self.adapter = adapter

    def run(self, ctx: RegistrationContext) -> RegistrationResult:
        if self.adapter.preflight:
            self.adapter.preflight(ctx)

        if getattr(ctx.identity, "identity_provider", "") == "oauth_browser":
            capability = self.adapter.capability
            ensure_oauth_executor_allowed(
                ctx,
                capability.oauth_allowed_executor_types,
            )
            if capability.oauth_headless_requires_browser_reuse and ctx.executor_type == "headless":
                ensure_oauth_browser_reuse(
                    ctx,
                    f"{ctx.platform_display_name} 无头 OAuth 需要配置 chrome_user_data_dir 或 chrome_cdp_url，以便复用本机已登录的浏览器会话",
                )
            if self.adapter.oauth_runner:
                raw = self.adapter.oauth_runner(ctx)
                return self.adapter.result_mapper(ctx, raw)

        if self.adapter.capability.browser_mailbox_requires_email:
            ensure_identity_email(ctx, f"{ctx.platform_display_name} 浏览器模式需要邮箱地址")
        if self.adapter.capability.browser_mailbox_requires_mailbox:
            ensure_mailbox_identity(ctx, f"{ctx.platform_display_name} 浏览器邮箱注册依赖 mailbox provider")

        if self.adapter.step_flow_builder is not None:
            raw = run_step_flow(ctx, self.adapter)
            return self.adapter.result_mapper(ctx, raw)

        artifacts = RegistrationArtifacts()
        if self.adapter.use_captcha_for_mailbox and getattr(ctx.identity, "identity_provider", "") == "mailbox":
            artifacts.captcha_solver = ctx.platform._make_captcha()
        if self.adapter.otp_spec:
            artifacts.otp_callback = build_otp_callback(
                ctx,
                keyword=self.adapter.otp_spec.keyword,
                timeout=self.adapter.otp_spec.timeout,
                code_pattern=self.adapter.otp_spec.code_pattern,
                wait_message=self.adapter.otp_spec.wait_message,
                success_label=self.adapter.otp_spec.success_label,
            )
        if self.adapter.link_spec:
            artifacts.verification_link_callback = build_link_callback(
                ctx,
                keyword=self.adapter.link_spec.keyword,
                timeout=self.adapter.link_spec.timeout,
                wait_message=self.adapter.link_spec.wait_message,
                success_label=self.adapter.link_spec.success_label,
                preview_chars=self.adapter.link_spec.preview_chars,
            )
        artifacts.phone = build_phone_verification(ctx, platform=ctx.platform_name)

        try:
            worker = self.adapter.browser_worker_builder(ctx, artifacts) if self.adapter.browser_worker_builder else None
            if worker is None or self.adapter.browser_register_runner is None:
                raise RuntimeError(f"{ctx.platform_display_name} 未实现浏览器注册适配器")
            raw = self.adapter.browser_register_runner(worker, ctx, artifacts)
            artifacts.raw_result = raw
            return self.adapter.result_mapper(ctx, raw)
        finally:
            if artifacts.phone is not None:
                artifacts.phone.close()


class ProtocolMailboxFlow:
    def __init__(self, adapter: ProtocolMailboxAdapter):
        self.adapter = adapter

    def run(self, ctx: RegistrationContext) -> RegistrationResult:
        if self.adapter.preflight:
            self.adapter.preflight(ctx)
        if self.adapter.capability.protocol_mailbox_requires_email:
            ensure_identity_email(ctx, f"{ctx.platform_display_name} 注册流程依赖 mailbox provider，当前未获取到邮箱账号")
        if self.adapter.capability.protocol_mailbox_requires_mailbox:
            ensure_mailbox_identity(ctx, f"{ctx.platform_display_name} 注册流程依赖 mailbox provider，当前未获取到邮箱账号")

        artifacts = RegistrationArtifacts()
        if self.adapter.use_captcha:
            artifacts.captcha_solver = ctx.platform._make_captcha()
        if self.adapter.otp_spec:
            artifacts.otp_callback = build_otp_callback(
                ctx,
                keyword=self.adapter.otp_spec.keyword,
                timeout=self.adapter.otp_spec.timeout,
                code_pattern=self.adapter.otp_spec.code_pattern,
                wait_message=self.adapter.otp_spec.wait_message,
                success_label=self.adapter.otp_spec.success_label,
            )
        if self.adapter.link_spec:
            artifacts.verification_link_callback = build_link_callback(
                ctx,
                keyword=self.adapter.link_spec.keyword,
                timeout=self.adapter.link_spec.timeout,
                wait_message=self.adapter.link_spec.wait_message,
                success_label=self.adapter.link_spec.success_label,
                preview_chars=self.adapter.link_spec.preview_chars,
            )
        if self.adapter.use_phone:
            artifacts.phone = build_phone_verification(ctx, platform=ctx.platform_name)

        executor_cm = ctx.platform._make_executor() if self.adapter.use_executor else nullcontext(None)
        try:
            with executor_cm as executor:
                artifacts.executor = executor
                worker = self.adapter.worker_builder(ctx, artifacts)
                raw = self.adapter.register_runner(worker, ctx, artifacts)
                artifacts.raw_result = raw
                return self.adapter.result_mapper(ctx, raw)
        finally:
            if artifacts.phone is not None:
                artifacts.phone.close()


class ProtocolOAuthFlow:
    def __init__(self, adapter: ProtocolOAuthAdapter):
        self.adapter = adapter

    def run(self, ctx: RegistrationContext) -> RegistrationResult:
        if self.adapter.preflight:
            self.adapter.preflight(ctx)
        ensure_oauth_executor_allowed(
            ctx,
            self.adapter.capability.oauth_allowed_executor_types,
        )
        if self.adapter.capability.oauth_headless_requires_browser_reuse and ctx.executor_type == "headless":
            ensure_oauth_browser_reuse(
                ctx,
                f"{ctx.platform_display_name} 无头 OAuth 需要配置 chrome_user_data_dir 或 chrome_cdp_url，以便复用本机已登录的浏览器会话",
            )
        raw = self.adapter.oauth_runner(ctx)
        return self.adapter.result_mapper(ctx, raw)
