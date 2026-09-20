"""取消令牌。

现状是 ``is_cancel_requested()`` 只在调度循环轮询，``platform.register()``
一旦开跑就不可中断（等短信 180 秒、等邮件 120 秒都会跑满）。令牌把"是否该停"
下沉成一个可以传进长等待循环的谓词，并对 DB 查询做节流。

同一批任务共享一个令牌：某次尝试撞上致命错误时，可以顺手停掉兄弟尝试。
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from core.errors import AttemptCancelled


class CancellationToken:
    """可被本地 trip() 或外部谓词（DB 轮询）触发的取消信号。"""

    def __init__(
        self,
        probe: Callable[[], bool] | None = None,
        *,
        throttle_seconds: float = 1.0,
    ):
        self._probe = probe
        self._throttle = max(0.0, float(throttle_seconds))
        self._event = threading.Event()
        self._reason = ""
        self._lock = threading.Lock()
        self._last_probe_at = 0.0
        self._last_probe_result = False

    # ------------------------------------------------------------------
    def trip(self, reason: str = "") -> None:
        """本地触发（例如某次尝试遇到致命错误，停掉整批）。"""
        with self._lock:
            if not self._reason:
                self._reason = reason or "cancelled"
        self._event.set()

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def event(self) -> threading.Event:
        """供需要 wait() 语义的等待方（如 HumanChannel）使用。"""
        return self._event

    def is_cancelled(self) -> bool:
        if self._event.is_set():
            return True
        if self._probe is None:
            return False
        now = time.time()
        if now - self._last_probe_at < self._throttle:
            return self._last_probe_result
        self._last_probe_at = now
        try:
            self._last_probe_result = bool(self._probe())
        except Exception:
            self._last_probe_result = False
        if self._last_probe_result:
            self.trip("外部请求取消")
        return self._last_probe_result

    __call__ = is_cancelled

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise AttemptCancelled(self._reason or "任务已取消")


#: 永不取消的空令牌，供未接入取消的调用方使用
NEVER = CancellationToken(probe=None)


def accepts_should_stop(fn) -> bool:
    """判断某个可调用是否接受 should_stop（未接入的实现退化为跑满 timeout）。

    按签名判断而不是靠捕获 TypeError：后者会把被调函数内部抛的任何 TypeError
    误判成"签名不匹配"并静默退化成不可取消的等待。
    """
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "should_stop" in params:
        return True
    return any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values())


def _raise_if_stopped(should_stop) -> None:
    """should_stop 可以是 CancellationToken，也可以是裸谓词。"""
    if should_stop is None:
        return
    raiser = getattr(should_stop, "raise_if_cancelled", None)
    if raiser is not None:
        raiser()
        return
    if should_stop():
        raise AttemptCancelled("等待期间任务被取消")


def cancellable_sleep(seconds: float, *, should_stop=None, tick: float = 0.2) -> None:
    """可被取消打断的 sleep。

    页面流程里大量的 ``time.sleep(2)`` 是任务取消最主要的延迟来源：令牌早就
    翻了，线程还在睡。按 tick 切片轮询，取消时立刻抛 AttemptCancelled。
    """
    deadline = time.time() + max(0.0, float(seconds))
    while True:
        _raise_if_stopped(should_stop)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(max(0.0, float(tick)), remaining))


def poll_until(predicate: Callable[[], object], *, timeout: float, interval: float = 0.5, should_stop=None):
    """轮询到 predicate 返回真值为止，超时返回 None。

    先判一次再睡：页面往往已经是目标状态，没必要白等一个 interval。
    predicate 自己抛的异常照常上抛——要吞的话在 predicate 里吞，
    在这里统一吞会把"选择器写错了"变成"超时"。
    """
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        _raise_if_stopped(should_stop)
        value = predicate()
        if value:
            return value
        if time.time() >= deadline:
            return None
        cancellable_sleep(min(float(interval), max(0.0, deadline - time.time())), should_stop=should_stop)
