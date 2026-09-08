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
