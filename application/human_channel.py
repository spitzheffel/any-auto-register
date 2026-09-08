"""落库版人工通道。

runner 线程在这里原地阻塞：浏览器会话、号码租约、solver 实例都还活在内存里，
人回答之后直接继续——进程不死就不需要反序列化任何东西。
落盘的只有交互请求、任务状态、检查点与回答本身。
"""
from __future__ import annotations

import threading
import time

from core.registration.steps import (
    InteractionRequest,
    InteractionResponse,
    ON_TIMEOUT_ABORT,
)
from infrastructure.task_interactions_repository import (
    STATUS_ANSWERED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    TaskInteractionsRepository,
)

#: API 回答后用来立刻唤醒等待线程（跨进程时靠 DB 轮询兜底）
_WAITERS: dict[int, threading.Event] = {}
_WAITERS_LOCK = threading.Lock()

POLL_INTERVAL_SECONDS = 1.0


def notify_answered(interaction_id: int) -> None:
    with _WAITERS_LOCK:
        event = _WAITERS.get(int(interaction_id))
    if event is not None:
        event.set()


class DbHumanChannel:
    """把问题落库、置任务为 waiting_human、阻塞等待回答。"""

    def __init__(
        self,
        task_id: str,
        *,
        repository: TaskInteractionsRepository | None = None,
        on_waiting=None,
        on_resumed=None,
        log=None,
        screenshot_dir: str = "",
    ):
        self.task_id = task_id
        self.repository = repository or TaskInteractionsRepository()
        self._on_waiting = on_waiting
        self._on_resumed = on_resumed
        self._log = log
        self.screenshot_dir = screenshot_dir

    # ------------------------------------------------------------------
    def ask(self, request: InteractionRequest, *, cancel=None) -> InteractionResponse:
        interaction_id = self.repository.create(
            task_id=self.task_id,
            attempt_id=str((request.context or {}).get("attempt_id") or ""),
            step=str((request.context or {}).get("step") or ""),
            kind=request.kind,
            prompt=request.prompt,
            schema={"fields": list(request.fields), "options": list(request.options)},
            context=dict(request.context or {}),
            timeout_seconds=request.timeout,
        )

        event = threading.Event()
        try:
            # 注册必须和 finally 里的 pop 成对：_on_waiting 抛出时 event 才不会留在 _WAITERS 里
            with _WAITERS_LOCK:
                _WAITERS[interaction_id] = event

            if self._on_waiting:
                self._on_waiting(interaction_id, request)
            if self._log:
                self._log(f"等待人工处理: {request.prompt}", level="warning", event_type="waiting_human")

            return self._wait(interaction_id, request, event, cancel)
        except BaseException:
            # 等待线程已经不在了（取消信号 / 回调抛错），别让 UI 上留一条没人接的待办
            try:
                self.repository.finish(interaction_id, STATUS_CANCELLED)
            except Exception:
                pass
            raise
        finally:
            with _WAITERS_LOCK:
                _WAITERS.pop(interaction_id, None)
            if self._on_resumed:
                self._on_resumed(interaction_id)

    # ------------------------------------------------------------------
    def _wait(self, interaction_id: int, request: InteractionRequest, event, cancel) -> InteractionResponse:
        deadline = time.time() + max(1, int(request.timeout))
        while True:
            if event.wait(POLL_INTERVAL_SECONDS):
                answered = self.repository.get(interaction_id) or {}
                if answered.get("status") == STATUS_ANSWERED:
                    return _to_response(answered.get("response") or {})
                event.clear()

            if cancel is not None and cancel.is_cancelled():
                self.repository.finish(interaction_id, STATUS_CANCELLED)
                return InteractionResponse(cancelled=True)

            # DB 兜底：回答可能来自另一个进程
            if self.repository.status_of(interaction_id) != STATUS_PENDING:
                return self._settled_response(interaction_id)

            if time.time() >= deadline:
                if not self.repository.finish(interaction_id, STATUS_EXPIRED):
                    # 条件更新没命中，说明行已经不是 pending——最可能是回答恰好在这一刻落库。
                    # 人已经填了值，不能因为差几毫秒就把它当超时丢掉。
                    return self._settled_response(interaction_id)
                if self._log:
                    self._log(
                        f"人工交互超时（{request.timeout}s），按 {request.on_timeout} 处理",
                        level="warning",
                        event_type="waiting_human",
                    )
                return InteractionResponse(timed_out=True)

    def _settled_response(self, interaction_id: int) -> InteractionResponse:
        """行已离开 pending 之后，按落库的最终状态给出响应。"""
        item = self.repository.get(interaction_id) or {}
        status = item.get("status")
        if status == STATUS_ANSWERED:
            return _to_response(item.get("response") or {})
        return InteractionResponse(
            cancelled=status == STATUS_CANCELLED,
            timed_out=status != STATUS_CANCELLED,
        )

    # ------------------------------------------------------------------
    def ask_captcha(self, prompt: str, challenge) -> str:
        """ManualCaptcha 的入口（人也是一种 provider）。"""
        response = self.ask(
            InteractionRequest(
                kind="captcha",
                prompt=prompt,
                fields=("value",),
                context={"page_url": getattr(challenge, "page_url", "")},
                screenshot=True,
            )
        )
        return response.value


def _to_response(payload: dict) -> InteractionResponse:
    return InteractionResponse(
        value=str(payload.get("value") or ""),
        values=dict(payload.get("values") or {}),
        option=str(payload.get("option") or ""),
    )


def timeout_action(request: InteractionRequest) -> str:
    return request.on_timeout or ON_TIMEOUT_ABORT
