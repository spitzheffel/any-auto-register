"""Step 引擎与任务层之间的运行时契约。

任务层每次尝试构建一个 ``StepRuntime``，经 ``BasePlatform.set_runtime`` 注入，
``register()`` 再把它挂到 ``RegistrationContext.runtime`` 上。流程层、能力构建、
provider 工厂都从这一个对象取日志、取消令牌、人工通道、检查点与渠道绑定，
不再靠 ``platform.step_runtime`` 动态属性或往 ``config.extra`` 里塞活对象。
"""
from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from core.cancellation import NEVER, CancellationToken


@dataclass
class StepRuntime:
    attempt_id: str = ""
    #: 结构化日志入口（接受 level / event_type / detail 关键字参数）
    log: Callable[..., None] | None = None
    cancel: CancellationToken = NEVER
    human: Any = None
    checkpoint: Any = None
    artifact_root: str = ""
    #: core.channel_bindings.ChannelBindings —— 任务创建时钉住的渠道选择
    bindings: Any = None
    hold_on_failure: bool = False
    start_at: str | None = None
    state: dict | None = None
    #: 出参：实际命中的渠道（按渠道类别 mailbox / sms / captcha 记）
    channels_used: dict[str, str] = field(default_factory=dict)
    #: 出参：流程开始前就已占用的外部资源（例如身份阶段分配的邮箱）
    resource_refs: dict[str, Any] = field(default_factory=dict)


_STRUCTURED_CACHE: dict[Any, bool] = {}


def accepts_structured_log(fn: Callable[..., Any] | None) -> bool:
    """日志函数是否接受 level / event_type / detail 关键字参数。

    按签名判断而不是靠捕获 TypeError：后者会把日志函数内部的任何 TypeError
    误判成"签名不匹配"并静默吞掉事件。
    """
    if fn is None:
        return False
    key = getattr(fn, "__func__", fn)
    # 只缓存有名字的函数（bound method / 模块级函数）：每次调用新建的 lambda / partial
    # 缓存下来只会让字典无限长，而它们本来就不是生产路径上的日志入口
    cacheable = getattr(key, "__name__", "<lambda>") != "<lambda>" and not isinstance(key, functools.partial)
    if cacheable:
        cached = _STRUCTURED_CACHE.get(key)
        if cached is not None:
            return cached
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        result = False
    else:
        result = any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()) or (
            "level" in params
        )
    if cacheable:
        _STRUCTURED_CACHE[key] = result
    return result


def call_log(fn: Callable[..., Any] | None, message: str, **kwargs: Any) -> None:
    """把结构化日志交给 fn；fn 只收一个参数时把级别拼进文本，不丢事件。"""
    if fn is None:
        return
    if kwargs and accepts_structured_log(fn):
        fn(message, **kwargs)
        return
    level = str(kwargs.get("level") or "")
    if level in ("warning", "error"):
        message = f"[{level}] {message}"
    fn(message)
