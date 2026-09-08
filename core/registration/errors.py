from __future__ import annotations

from core.errors import ConfigError, TransientError


class RegistrationError(RuntimeError):
    """注册流程基础异常。"""


class IdentityResolutionError(RegistrationError, ConfigError):
    """身份解析失败（缺邮箱/未配置 identity provider）——重试无用，先改配置。"""


class CaptchaConfigurationError(RegistrationError, ConfigError):
    """验证码配置不可用。"""


class OtpTimeoutError(RegistrationError, TransientError):
    """验证码等待超时——换一次尝试通常就好了。"""


class BrowserReuseRequiredError(RegistrationError, ConfigError):
    """无头 OAuth 缺少可复用浏览器会话。"""


class RegistrationUnsupportedError(RegistrationError, ConfigError):
    """当前平台或执行器不支持该注册路径。"""
