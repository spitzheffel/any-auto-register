"""失败分类。

没有分类，重试策略只能瞎猜：今天所有失败都被转成字符串，配置写错也会
把尝试次数烧满。分类的意义是让上层能回答一个问题——"这次失败该怎么处置"。

    TransientError        原地重试 / 补投尝试（等码超时、临时无号）
    ProviderError         渠道侧故障，换渠道重试（余额不足、solver 挂了）
    ResourceRejectedError 资源被目标站拒绝，换资源重试（号码被拒）
      └ RiskControlError  IP / 指纹被风控，换代理+指纹重跑整段
    SiteChangedError      选择器或流程失效，别重试，报警
    ConfigError           配置缺失或非法，立即停批

``AttemptCancelled`` 不是错误而是控制流信号：取消令牌触发时抛出，
任务层视为"既非成功也非失败"。
"""
from __future__ import annotations


class RegistrationFailure(RuntimeError):
    """所有分类失败的共同基类。"""

    #: 是否值得再试一次（同一资源）
    retryable = False
    #: 是否应立即停掉整批任务
    fatal = False
    #: 上报给 UI / 统计的类别名
    category = "unknown"


class TransientError(RegistrationFailure):
    """临时性故障：等一等再来通常就好了。"""

    retryable = True
    category = "transient"


class ProviderError(RegistrationFailure):
    """渠道自身的问题：余额不足、接口报错、solver 不可用。

    处置是换一家渠道（failover 的挂点），而不是原地干等。
    """

    retryable = True
    category = "provider"


class ResourceRejectedError(RegistrationFailure):
    """目标站点拒绝了这个资源（手机号 / 邮箱域名等），换资源再来。"""

    retryable = True
    category = "resource_rejected"


class RiskControlError(ResourceRejectedError):
    """被风控拦截：换代理 + 指纹后重跑整段才有意义。"""

    category = "risk_control"


class SiteChangedError(RegistrationFailure):
    """选择器 / 接口 / 流程与代码预期不符——重试没用，需要人改代码。"""

    fatal = True
    category = "site_changed"


class ConfigError(RegistrationFailure):
    """配置缺失或非法：不停批的话只会把尝试次数烧光。"""

    fatal = True
    category = "config"


class AttemptCancelled(BaseException):
    """取消令牌触发的控制流信号（不是失败，不计入错误数）。

    继承 BaseException 而非 Exception，避免被业务代码里遍地的
    ``except Exception`` 顺手吞掉。
    """


def classify(exc: BaseException) -> str:
    """返回异常的类别名；未分类的一律 unknown。"""
    if isinstance(exc, AttemptCancelled):
        return "cancelled"
    if isinstance(exc, RegistrationFailure):
        return exc.category
    return "unknown"


def is_fatal(exc: BaseException) -> bool:
    """是否应停掉整批（配置错 / 站点变更）。"""
    return isinstance(exc, RegistrationFailure) and exc.fatal


def is_retryable(exc: BaseException) -> bool:
    """是否值得补投尝试。未分类的异常保守处理：不补投（与历史行为一致）。"""
    return isinstance(exc, RegistrationFailure) and exc.retryable
