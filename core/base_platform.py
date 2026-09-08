"""平台插件基类"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
import random
import string
import time

from core.registration import BrowserRegistrationFlow, ProtocolMailboxFlow, ProtocolOAuthFlow, RegistrationContext, RegistrationResult
from core.registration.runtime import call_log


# 支付/试用链接这一组能力被统一到 generate_link(_browser) 之前，各平台各叫各的；
# 调用方（application/tasks.py 的注册后自动升级、前端动作菜单、历史任务记录）
# 仍在用旧 id，这里做一次转译，避免它们撞上 NotImplementedError。
LEGACY_ACTION_ALIASES = {
    "payment_link": "generate_link",
    "generate_trial_link": "generate_link",
    "generate_checkout_link": "generate_link",
    "payment_link_browser": "generate_link_browser",
    "generate_trial_link_browser": "generate_link_browser",
}


class AccountStatus(str, Enum):
    REGISTERED   = "registered"
    TRIAL        = "trial"
    SUBSCRIBED   = "subscribed"
    EXPIRED      = "expired"
    INVALID      = "invalid"


@dataclass
class Account:
    platform: str
    email: str
    password: str
    user_id: str = ""
    region: str = ""
    token: str = ""
    status: AccountStatus = AccountStatus.REGISTERED
    trial_end_time: int = 0       # unix timestamp
    extra: dict = field(default_factory=dict)  # 平台自定义字段
    created_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class RegisterConfig:
    """注册任务配置"""
    executor_type: str = "protocol"   # protocol | headless | headed
    captcha_solver: str = "auto"  # auto | <provider_key> | manual
    proxy: Optional[str] = None
    extra: dict = field(default_factory=dict)


class BasePlatform(ABC):
    # 子类必须定义
    name: str = ""
    display_name: str = ""
    version: str = "1.0.0"
    # 平台能力由数据库表提供；类上不再保留业务配置默认值。
    supported_executors: list = []
    supported_identity_modes: list = []
    supported_oauth_providers: list = []
    protocol_captcha_order: tuple[str, ...] = ()
    # Declarative capabilities - override in subclasses
    capabilities: list[str] = []
    # Per-capability label/param overrides - override in subclasses
    capability_overrides: dict[str, dict] = {}

    def __init__(self, config: RegisterConfig = None):
        from core.registry import get_platform_capabilities

        self.config = config or RegisterConfig()
        self._log_fn = print
        # core.registration.runtime.StepRuntime，由任务层每次尝试注入
        self.runtime = None
        capabilities = get_platform_capabilities(self.name) if self.name else {}
        self.supported_executors = list(capabilities.get("supported_executors", [])) or list(self.supported_executors)
        self.supported_identity_modes = list(capabilities.get("supported_identity_modes", [])) or list(self.supported_identity_modes)
        self.supported_oauth_providers = list(capabilities.get("supported_oauth_providers", [])) or list(self.supported_oauth_providers)
        db_caps = list(capabilities.get("capabilities", []))
        if db_caps:
            self.capabilities = db_caps
        if self.config.executor_type not in self.supported_executors:
            raise NotImplementedError(
                f"{self.display_name} 暂不支持 '{self.config.executor_type}' 执行器，"
                f"当前支持: {self.supported_executors}"
            )

    def set_logger(self, logger):
        self._log_fn = logger or print

    def set_runtime(self, runtime) -> None:
        """任务层注入本次尝试的运行时（日志 / 取消 / 人工通道 / 检查点 / 渠道绑定）。"""
        self.runtime = runtime

    def log(self, message: str, **kwargs):
        call_log(self._log_fn, message, **kwargs)

    def _make_random_password(self, length: int = 16, charset: Optional[str] = None) -> str:
        chars = charset or (string.ascii_letters + string.digits + "!@#$")
        return "".join(random.choices(chars, k=length))

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or self._make_random_password()

    def _should_require_identity_email(self) -> bool:
        return self._get_identity_provider_name() != "oauth_browser"

    def _browser_registration_label(self, identity) -> str:
        return getattr(identity, "email", "") or "(oauth)"

    def build_browser_registration_adapter(self):
        return None

    def build_protocol_mailbox_adapter(self):
        return None

    def build_protocol_oauth_adapter(self):
        return None

    def _account_from_registration_result(self, result: RegistrationResult) -> Account:
        status = result.status or AccountStatus.REGISTERED
        if isinstance(status, str):
            try:
                status = AccountStatus(status)
            except Exception:
                status = AccountStatus.REGISTERED
        return Account(
            platform=self.name,
            email=result.email,
            password=result.password,
            user_id=result.user_id,
            region=result.region,
            token=result.token,
            status=status,
            trial_end_time=result.trial_end_time,
            extra=dict(result.extra or {}),
        )

    def register(self, email: str = None, password: str = None) -> Account:
        resolved_password = self._prepare_registration_password(password)
        identity = self._resolve_identity(email, require_email=self._should_require_identity_email())
        self._record_mailbox_ref(identity)
        ctx = RegistrationContext(
            platform_name=self.name,
            platform_display_name=self.display_name,
            platform=self,
            identity=identity,
            config=self.config,
            email=email,
            password=resolved_password,
            log_fn=self.log,
            runtime=self.runtime,
        )

        if (self.config.executor_type or "") in ("headless", "headed"):
            self.log(f"使用浏览器模式注册: {self._browser_registration_label(identity)}")
            adapter = self.build_browser_registration_adapter()
            if adapter is None:
                raise NotImplementedError(f"{self.display_name} 未实现浏览器注册适配器")
            result = BrowserRegistrationFlow(adapter).run(ctx)
            return self._attach_identity_metadata(self._account_from_registration_result(result), identity)

        if getattr(identity, "identity_provider", "") == "oauth_browser":
            adapter = self.build_protocol_oauth_adapter()
            if adapter is None:
                raise RuntimeError(
                    f"{self.display_name} 当前仅浏览器模式支持 oauth_browser，请使用受支持的浏览器执行器"
                )
            result = ProtocolOAuthFlow(adapter).run(ctx)
            return self._attach_identity_metadata(self._account_from_registration_result(result), identity)

        self.log(f"邮箱: {identity.email}")
        adapter = self.build_protocol_mailbox_adapter()
        if adapter is None:
            raise NotImplementedError(f"{self.display_name} 未实现协议邮箱注册适配器")
        result = ProtocolMailboxFlow(adapter).run(ctx)
        return self._attach_identity_metadata(self._account_from_registration_result(result), identity)

    def _record_mailbox_ref(self, identity) -> None:
        """身份阶段分到的邮箱在流程开始前就已被占用：记进运行时引用，进程崩溃后重启才能释放。"""
        runtime = self.runtime
        account = getattr(identity, "mailbox_account", None)
        if runtime is None or account is None or not getattr(account, "email", ""):
            return
        provider_key = str((getattr(account, "extra", None) or {}).get("mailbox_provider_key") or "")
        if not provider_key:
            bindings = getattr(runtime, "bindings", None)
            if bindings is not None:
                try:
                    from core.channel_bindings import MAILBOX

                    provider_key = str(bindings.resolve(MAILBOX).key or "")
                except Exception:
                    provider_key = ""
        if not provider_key:
            provider_key = str((self.config.extra or {}).get("mail_provider") or "")
        runtime.resource_refs["mailbox"] = {
            "provider_key": provider_key,
            "email": str(account.email),
            "account_id": str(getattr(account, "account_id", "") or ""),
        }

    def describe_requirements(
        self,
        executor_type: str = "",
        identity_provider: str = "mailbox",
    ) -> list:
        """本平台在给定执行器 / 身份模式下需要哪些渠道。

        从代码本身推导（步骤的 requires 或 adapter 的声明），
        不再依赖手工维护的 capabilities 列表——那玩意儿一定会和代码脱节。
        """
        from core.requirements import from_adapter, from_step_flow

        executor_type = str(executor_type or self.config.executor_type or "protocol").strip().lower()
        browser_mode = executor_type in {"headless", "headed"}

        adapter = None
        try:
            if browser_mode:
                adapter = self.build_browser_registration_adapter()
            elif identity_provider == "oauth_browser":
                adapter = self.build_protocol_oauth_adapter()
            else:
                adapter = self.build_protocol_mailbox_adapter()
        except Exception:
            adapter = None

        step_flow_builder = getattr(adapter, "step_flow_builder", None)
        if step_flow_builder is not None and identity_provider != "oauth_browser":
            obstacle_builder = getattr(adapter, "obstacle_builder", None)
            obstacles = obstacle_builder() if obstacle_builder else ()
            requirements = from_step_flow(step_flow_builder(), obstacles)
            if identity_provider == "mailbox":
                from core.requirements import LEVEL_REQUIRED, Requirement, merge_requirements

                requirements = merge_requirements(
                    requirements + [Requirement("mailbox", LEVEL_REQUIRED, reason="身份来自邮箱 provider")]
                )
            return requirements

        return from_adapter(adapter, identity_provider=identity_provider, browser_mode=browser_mode)

    @abstractmethod
    def check_valid(self, account: Account) -> bool:
        """检测账号是否有效"""
        ...

    def get_trial_url(self, account: Account) -> Optional[str]:
        """生成试用激活链接（可选实现）"""
        return None

    def get_platform_actions(self) -> list:
        """
        Return platform-supported extra operation list, each item format:
        {"id": str, "label": str, "params": [{"key": str, "label": str, "type": str}]}
        
        For backward compatibility, this now uses the capability system.
        Override this method in platform classes for custom actions.
        """
        # Use capability system if capabilities are defined
        if hasattr(self, 'capabilities') and self.capabilities:
            return self.get_capability_actions()
        
        # Fallback to empty list for platforms that haven't migrated yet
        return []

    def get_desktop_state(self) -> dict:
        return {
            "available": False,
            "message": f"{self.display_name or self.name} 暂未提供桌面应用状态探测",
        }

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        """
        Execute platform-specific action, return {"ok": bool, "data": any, "error": str}
        """
        # Try to handle as standard capability first
        if action_id in self.capabilities:
            return self._handle_capability(action_id, account, params)

        aliased = LEGACY_ACTION_ALIASES.get(action_id, "")
        if aliased and aliased in self.capabilities:
            return self._handle_capability(aliased, account, params)

        # Fallback to platform-specific implementation
        raise NotImplementedError(f"Platform {self.name} does not support action: {action_id}")
    
    def _handle_capability(self, capability_id: str, account: Account, params: dict) -> dict:
        """Handle standard capabilities with default implementations."""
        try:
            if capability_id == "query_state":
                return self._handle_query_state(account, params)
            elif capability_id == "refresh_token":
                return self._handle_refresh_token(account, params)
            elif capability_id == "generate_link":
                return self._handle_generate_link(account, params)
            elif capability_id == "switch_desktop":
                return self._handle_switch_desktop(account, params)
            elif capability_id == "upload_cpa":
                return self._handle_upload_cpa(account, params)
            elif capability_id == "upload_tm":
                return self._handle_upload_tm(account, params)
            elif capability_id == "check_trial":
                return self._handle_check_trial(account, params)
            elif capability_id == "generate_link_browser":
                return self._handle_generate_link_browser(account, params)
            elif capability_id == "create_api_key":
                return self._handle_create_api_key(account, params)
            else:
                # Fall back to platform-specific implementation
                return self._execute_platform_action(capability_id, account, params)
        except NotImplementedError:
            # If platform doesn't implement the capability, return error
            return {"ok": False, "error": f"Capability {capability_id} not implemented for {self.display_name}"}
    
    def _execute_platform_action(self, action_id: str, account: Account, params: dict) -> dict:
        """Override this method in platform classes for custom actions."""
        raise NotImplementedError(f"Platform {self.name} does not implement action: {action_id}")
    
    # Default handlers for standard capabilities
    def _handle_query_state(self, account: Account, params: dict) -> dict:
        """Default query_state handler - calls get_quota or returns basic state."""
        quota_data = self.get_quota(account)
        if quota_data:
            return {"ok": True, "data": quota_data}
        return {
            "ok": True, 
            "data": {
                "status": account.status.value,
                "user_id": account.user_id,
                "email": account.email,
                "platform": account.platform,
            }
        }
    
    def _handle_refresh_token(self, account: Account, params: dict) -> dict:
        """Default refresh_token handler - platform should override."""
        raise NotImplementedError(f"Token refresh not implemented for {self.display_name}")
    
    def _handle_generate_link(self, account: Account, params: dict) -> dict:
        """Default generate_link handler - calls get_trial_url."""
        trial_url = self.get_trial_url(account)
        if trial_url:
            return {"ok": True, "data": {"url": trial_url, "message": "Trial link generated"}}
        raise NotImplementedError(f"Link generation not implemented for {self.display_name}")
    
    def _handle_switch_desktop(self, account: Account, params: dict) -> dict:
        """Default switch_desktop handler - platform should override."""
        raise NotImplementedError(f"Desktop switch not implemented for {self.display_name}")
    
    def _handle_upload_cpa(self, account: Account, params: dict) -> dict:
        """Default upload_cpa handler - platform should override."""
        raise NotImplementedError(f"CPA upload not implemented for {self.display_name}")
    
    def _handle_upload_tm(self, account: Account, params: dict) -> dict:
        """Default upload_tm handler - platform should override."""
        raise NotImplementedError(f"Team Manager upload not implemented for {self.display_name}")
    
    def _handle_check_trial(self, account: Account, params: dict) -> dict:
        """Default check_trial handler - platform should override."""
        raise NotImplementedError(f"Trial check not implemented for {self.display_name}")
    
    def _handle_generate_link_browser(self, account: Account, params: dict) -> dict:
        """Default generate_link_browser handler - platform should override."""
        raise NotImplementedError(f"Browser link generation not implemented for {self.display_name}")
    
    def _handle_create_api_key(self, account: Account, params: dict) -> dict:
        """Default create_api_key handler - platform should override."""
        raise NotImplementedError(f"API key creation not implemented for {self.display_name}")
    
    def get_platform_capabilities(self) -> list:
        """Return the platform's declared capabilities."""
        return list(getattr(self, 'capabilities', []))
    
    def get_capability_actions(self) -> list:
        """
        Return actions list for backward compatibility.
        Maps capabilities to action definitions, with per-platform overrides.
        """
        from .capability_registry import CapabilityRegistry
        
        overrides = getattr(self, 'capability_overrides', {}) or {}
        actions = []
        for cap_id in self.get_platform_capabilities():
            definition = CapabilityRegistry.get_definition(cap_id)
            if definition:
                override = overrides.get(cap_id, {})
                action = {
                    "id": definition.id,
                    "label": override.get("label", definition.label),
                    "params": override.get("params", definition.param_schema),
                    "sync": override.get("sync", True),
                }
                actions.append(action)
        return actions

    def get_quota(self, account: Account) -> dict:
        """查询账号配额（可选实现）"""
        return {}

    def _make_executor(self):
        """根据 config 创建执行器"""
        from .executors.protocol import ProtocolExecutor
        t = self.config.executor_type
        engine = str((getattr(self.config, "extra", {}) or {}).get("browser_engine") or "")
        if t == "protocol":
            return ProtocolExecutor(proxy=self.config.proxy)
        elif t == "headless":
            from .executors.playwright import PlaywrightExecutor
            return PlaywrightExecutor(proxy=self.config.proxy, headless=True, engine=engine)
        elif t == "headed":
            from .executors.playwright import PlaywrightExecutor
            return PlaywrightExecutor(proxy=self.config.proxy, headless=False, engine=engine)
        raise ValueError(f"未知执行器类型: {t}")

    def _make_captcha(self, **kwargs):
        """根据 config 创建验证码解决器。

        人工通道只进本次调用的 config 副本（ManualCaptcha 从中取），不写回 config.extra。
        """
        from .base_captcha import create_captcha_solver

        provider_key = str(kwargs.get("provider_key") or "").strip()
        if not provider_key:
            provider_key = self._resolve_captcha_solver()
        self._prepare_captcha_provider(provider_key)
        human = kwargs.get("human")
        if human is None:
            human = getattr(self.runtime, "human", None)
        config = dict(self.config.extra or {})
        if human is not None:
            config["human_channel"] = human
        return create_captcha_solver(provider_key, config)

    def _has_configured_captcha(self, solver_name: str) -> bool:
        from .base_captcha import has_captcha_configured

        return has_captcha_configured(solver_name, self.config.extra)

    def _resolve_captcha_solver(self) -> str:
        from core.errors import ConfigError

        candidates = self._get_captcha_solver_candidates()
        if candidates:
            return candidates[0]

        # 缺 provider 是配置问题：分类为 ConfigError 让任务层停批，而不是把尝试烧光
        if self.config.executor_type in {"headless", "headed"}:
            raise ConfigError("浏览器模式未配置默认验证码 provider，请先在设置页启用并设为默认")
        raise ConfigError("协议模式未配置可用的验证码 provider，请先启用并配置至少一个验证码 provider")

    def _get_captcha_solver_candidates(self) -> list[str]:
        from core.channel_bindings import CAPTCHA, ChannelBindings

        # 任务创建时钉住的绑定优先；没有运行时（脚本直调）再从 extra 旧键解析
        bindings = getattr(self.runtime, "bindings", None)
        if bindings is None:
            bindings = ChannelBindings.from_payload(
                {"extra": {**(self.config.extra or {}), "captcha_solver": self.config.captcha_solver}},
                platform=self.name,
            )
        requested = bindings.binding(CAPTCHA).key.lower()
        if requested:
            if not self._has_configured_captcha(requested):
                from core.errors import ConfigError

                raise ConfigError(f"{requested} 未配置，无法创建验证码解决器")
            return [requested]

        if self.config.executor_type in {"headless", "headed"}:
            # 浏览器模式只用全局默认（页内注入 token，候选链意义不大）
            browser_key = bindings.resolve(CAPTCHA).key
            if browser_key and self._has_configured_captcha(browser_key):
                return [browser_key]
            return []

        candidates: list[str] = []

        def _append_candidate(key: str) -> None:
            normalized = str(key or "").strip().lower()
            if not normalized or normalized == "manual" or normalized in candidates:
                return
            if self._has_configured_captcha(normalized):
                candidates.append(normalized)

        try:
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            repo = ProviderSettingsRepository()
            for item in repo.list_enabled("captcha"):
                _append_candidate(getattr(item, "provider_key", ""))
        except Exception:
            for solver_name in self.protocol_captcha_order:
                _append_candidate(solver_name)

        for solver_name in self.protocol_captcha_order:
            _append_candidate(solver_name)

        return candidates

    def _prepare_captcha_provider(self, provider_key: str) -> None:
        from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

        key = str(provider_key or "").strip().lower()
        if not key:
            return

        definition = ProviderDefinitionsRepository().get_by_key("captcha", key)
        if not definition:
            return

        if str(definition.driver_type or "").strip().lower() == "local_solver":
            from services.solver_manager import start

            start()

    def solve_turnstile_with_fallback(self, page_url: str, site_key: str) -> str:
        errors: list[str] = []
        candidates = self._get_captcha_solver_candidates()
        if not candidates:
            raise RuntimeError("未找到可用的 Turnstile 验证码 provider")

        for provider_key in candidates:
            try:
                self.log(f"尝试 Turnstile provider: {provider_key}")
                solver = self._make_captcha(provider_key=provider_key)
                token = str(solver.solve_turnstile(page_url, site_key) or "").strip()
                if token:
                    return token
                raise RuntimeError("未返回有效 token")
            except Exception as exc:
                errors.append(f"{provider_key}: {exc}")
                self.log(f"Turnstile provider 失败: {provider_key} -> {exc}")

        raise RuntimeError("；".join(errors))

    def _get_identity_provider_name(self) -> str:
        from .base_identity import normalize_identity_provider
        return normalize_identity_provider(self.config.extra.get("identity_provider", "mailbox"))

    def _get_identity_provider(self):
        from .base_identity import create_identity_provider

        mode = self._get_identity_provider_name()
        if mode not in self.supported_identity_modes:
            raise NotImplementedError(
                f"{self.display_name} 暂不支持 identity_provider='{mode}'，"
                f"当前支持: {self.supported_identity_modes}"
            )
        return create_identity_provider(
            mode,
            mailbox=getattr(self, "mailbox", None),
            extra={**(self.config.extra or {}), "platform": self.name},
        )

    def _resolve_identity(self, email: str = None, *, require_email: bool = True):
        identity = self._get_identity_provider().resolve(email)
        self._last_identity = identity
        if require_email and not identity.email:
            raise ValueError(
                f"{self.display_name} 注册流程未获取到可用邮箱，"
                f"请提供 email 或配置支持的 identity_provider"
            )
        return identity

    def _build_identity_snapshot(self, identity) -> dict:
        snapshot = {
            "identity_provider": getattr(identity, "identity_provider", "") or "",
            "resolved_email": getattr(identity, "email", "") or "",
            "oauth_provider": getattr(identity, "oauth_provider", "") or "",
            "chrome_user_data_dir": getattr(identity, "chrome_user_data_dir", "") or "",
            "chrome_cdp_url": getattr(identity, "chrome_cdp_url", "") or "",
            "metadata": dict(getattr(identity, "metadata", {}) or {}),
        }
        mailbox_account = getattr(identity, "mailbox_account", None)
        if mailbox_account:
            mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {})
            snapshot["mailbox"] = {
                # 记实际出邮箱的那家：FallbackMailbox 可能在主 provider 失败后换了一家，
                # 记请求键会让事后排查对不上号
                "provider": (
                    mailbox_extra.get("mailbox_provider_key")
                    or (self.config.extra or {}).get("mail_provider", "")
                ),
                "email": getattr(mailbox_account, "email", "") or "",
                "account_id": str(getattr(mailbox_account, "account_id", "") or ""),
            }
            if isinstance(mailbox_extra.get("provider_account"), dict):
                snapshot["provider_account"] = mailbox_extra["provider_account"]
            if isinstance(mailbox_extra.get("provider_resource"), dict):
                snapshot["provider_resource"] = mailbox_extra["provider_resource"]
        return snapshot

    def _attach_identity_metadata(self, account: Account, identity=None) -> Account:
        actual_identity = identity or getattr(self, "_last_identity", None)
        if not actual_identity:
            return account
        extra = dict(account.extra or {})
        identity_snapshot = self._build_identity_snapshot(actual_identity)
        extra["identity"] = identity_snapshot
        if identity_snapshot.get("mailbox"):
            extra["verification_mailbox"] = identity_snapshot["mailbox"]
        provider_accounts = list(extra.get("provider_accounts", []) or [])
        if isinstance(identity_snapshot.get("provider_account"), dict):
            provider_accounts.append(identity_snapshot["provider_account"])
        if provider_accounts:
            extra["provider_accounts"] = provider_accounts
        provider_resources = list(extra.get("provider_resources", []) or [])
        if isinstance(identity_snapshot.get("provider_resource"), dict):
            provider_resources.append(identity_snapshot["provider_resource"])
        if provider_resources:
            extra["provider_resources"] = provider_resources
        account.extra = extra
        return account
