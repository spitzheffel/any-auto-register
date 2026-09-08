"""Task orchestration and persistence helpers."""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from pathlib import Path
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable, NamedTuple, Optional

from sqlmodel import Session, select, func

from core.account_graph import (
    load_account_graphs,
    patch_account_graph,
    recover_lifecycle_status_for_valid_account,
)
from core.base_platform import RegisterConfig
from core.cancellation import CancellationToken
from core.datetime_utils import format_local_clock, serialize_datetime
from core.errors import AttemptCancelled, classify, is_fatal, is_retryable
from core.db import AccountModel, TaskEventModel, TaskLog, TaskModel, engine, save_account
from core.platform_accounts import build_platform_account
from core.registry import get
from infrastructure.platform_runtime import PlatformRuntime


class _AttemptOutcome(NamedTuple):
    """一次注册尝试的结果：取消既不算成功也不算失败；失败要区分能否补投。"""

    status: str  # success | failed | cancelled
    error: str = ""
    retryable: bool = False

    @classmethod
    def succeeded(cls) -> "_AttemptOutcome":
        return cls("success")

    @classmethod
    def cancelled(cls) -> "_AttemptOutcome":
        return cls("cancelled")

    @classmethod
    def failed(cls, error: str, *, retryable: bool) -> "_AttemptOutcome":
        return cls("failed", error, retryable)

TASK_TYPE_REGISTER = "register"
TASK_TYPE_ACCOUNT_CHECK = "account_check"
TASK_TYPE_ACCOUNT_CHECK_ALL = "account_check_all"
TASK_TYPE_PLATFORM_ACTION = "platform_action"

TASK_STATUS_PENDING = "pending"
TASK_STATUS_CLAIMED = "claimed"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_INTERRUPTED = "interrupted"
TASK_STATUS_CANCEL_REQUESTED = "cancel_requested"
TASK_STATUS_CANCELLED = "cancelled"
# 挂起等待人工：仍占用并发槽位（握着活浏览器），靠交互超时兜底
TASK_STATUS_WAITING_HUMAN = "waiting_human"

# TaskLog（按账号的注册记录）状态：进程中断时已提交但没跑完的半成品账号
TASK_LOG_STATUS_PENDING_REVIEW = "pending_review"

TERMINAL_TASK_STATUSES = {
    TASK_STATUS_SUCCEEDED,
    TASK_STATUS_FAILED,
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_CANCELLED,
}
ACTIVE_TASK_STATUSES = {
    TASK_STATUS_CLAIMED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_CANCEL_REQUESTED,
    TASK_STATUS_WAITING_HUMAN,
}

_task_locks: dict[str, threading.Lock] = {}
_task_locks_guard = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _serialize_datetime(value: datetime | None) -> str | None:
    return serialize_datetime(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _dump_json(data: Any) -> str:
    return json.dumps(data or {}, ensure_ascii=False, default=_json_default)


def _task_lock(task_id: str) -> threading.Lock:
    with _task_locks_guard:
        lock = _task_locks.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _task_locks[task_id] = lock
        return lock


def _mutate_task(task_id: str, fn: Callable[[TaskModel], None]) -> Optional[TaskModel]:
    with _task_lock(task_id):
        with Session(engine) as session:
            task = session.get(TaskModel, task_id)
            if not task:
                return None
            fn(task)
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            session.refresh(task)
            return task


def _save_task_log(platform: str, email: str, status: str, error: str = "", detail: dict | None = None) -> None:
    with Session(engine) as session:
        log = TaskLog(
            platform=platform,
            email=email,
            status=status,
            error=error,
            detail_json=_dump_json(detail or {}),
        )
        session.add(log)
        session.commit()


def _task_result_seed(result: dict[str, Any] | None = None) -> dict[str, Any]:
    base = {"errors": [], "cashier_urls": [], "data": None}
    if result:
        base.update(result)
    return base


def _task_account_keys(task_type: str, payload: dict[str, Any]) -> list[str]:
    if task_type in {TASK_TYPE_ACCOUNT_CHECK, TASK_TYPE_PLATFORM_ACTION}:
        account_id = int(payload.get("account_id", 0) or 0)
        if account_id > 0:
            return [f"account:{account_id}"]
    return []


def serialize_task(task: TaskModel) -> dict[str, Any]:
    result = task.get_result()
    # 检查点（attempts）是服务端重启回收用的内部数据，不随任务接口下发
    public_result = {key: value for key, value in result.items() if key != "attempts"}
    progress_total = int(task.progress_total or 0)
    progress_current = int(task.progress_current or 0)
    return {
        "id": task.id,
        "task_id": task.id,
        "type": task.type,
        "platform": task.platform,
        "status": task.status,
        "terminal": task.status in TERMINAL_TASK_STATUSES,
        "cancellable": task.status in {
            TASK_STATUS_PENDING,
            TASK_STATUS_CLAIMED,
            TASK_STATUS_RUNNING,
            TASK_STATUS_CANCEL_REQUESTED,
            TASK_STATUS_WAITING_HUMAN,
        },
        # 只有挂起中的任务才可能有待回答交互，其它状态不必逐任务多查一次交互表
        "pending_interaction": (
            _pending_interaction_summary(task.id) if task.status == TASK_STATUS_WAITING_HUMAN else None
        ),
        "progress": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        "progress_detail": {
            "current": progress_current,
            "total": progress_total,
            "label": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        },
        "success": int(task.success_count or 0),
        "error_count": int(task.error_count or 0),
        "errors": list(result.get("errors", [])),
        "cashier_urls": list(result.get("cashier_urls", [])),
        "data": result.get("data"),
        "result": public_result,
        "error": task.error,
        "created_at": _serialize_datetime(task.created_at),
        "started_at": _serialize_datetime(task.started_at),
        "finished_at": _serialize_datetime(task.finished_at),
        "updated_at": _serialize_datetime(task.updated_at),
    }


def _pending_interaction_summary(task_id: str) -> dict[str, Any] | None:
    try:
        from infrastructure.task_interactions_repository import (
            STATUS_PENDING,
            TaskInteractionsRepository,
        )

        items = TaskInteractionsRepository().list_for_task(task_id, status=STATUS_PENDING)
    except Exception:
        return None
    if not items:
        return None
    item = items[0]
    return {
        "id": item["id"],
        "kind": item["kind"],
        "prompt": item["prompt"],
        "step": item["step"],
        "expires_at": item["expires_at"],
    }


def serialize_event(event: TaskEventModel) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "type": event.type,
        "level": event.level,
        "message": event.message,
        "line": f"[{format_local_clock(event.created_at)}] {event.message}",
        "detail": event.get_detail(),
        "created_at": _serialize_datetime(event.created_at),
    }


def create_task(
    *,
    task_type: str,
    platform: str,
    payload: dict[str, Any],
    progress_total: int = 1,
    result_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    task = TaskModel(
        id=task_id,
        type=task_type,
        platform=platform,
        status=TASK_STATUS_PENDING,
        payload_json=_dump_json(payload),
        result_json=_dump_json(_task_result_seed(result_seed)),
        progress_current=0,
        progress_total=max(int(progress_total or 0), 0),
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()
        session.refresh(task)
    append_task_event(task.id, f"任务已创建: {task_type}", event_type="state")
    return serialize_task(task)


def create_register_task(payload: dict[str, Any]) -> dict[str, Any]:
    count = max(int(payload.get("count", 1) or 1), 1)
    payload = resolve_task_channels(dict(payload))
    return create_task(
        task_type=TASK_TYPE_REGISTER,
        platform=str(payload.get("platform", "")),
        payload=payload,
        progress_total=count,
    )


class MissingChannelError(ValueError):
    """必需渠道未配置——发起前就拒掉，别让任务跑几分钟再炸。"""

    def __init__(self, missing: list[dict[str, Any]]):
        self.missing = missing
        names = "、".join(str(item.get("channel") or item.get("capability")) for item in missing)
        super().__init__(f"以下必需渠道未配置: {names}")


def resolve_task_channels(payload: dict[str, Any]) -> dict[str, Any]:
    """把渠道绑定在创建任务时就钉住，并校验必需渠道已配置。

    钉住之后运行期不再重查全局默认：用户在表单里看到的就是实际会跑的。
    """
    from application.platforms import PlatformsService, is_channel_configured
    from core.channel_bindings import ChannelBindings

    platform = str(payload.get("platform", ""))
    extra = dict(payload.get("extra") or {})
    executor_type = str(payload.get("executor_type", "protocol") or "protocol")
    identity_provider = str(extra.get("identity_provider", "mailbox") or "mailbox")

    bindings = ChannelBindings.from_payload(payload, platform=platform)
    payload["channel_bindings"] = bindings.to_dict()

    try:
        described = PlatformsService().describe_requirements(
            platform,
            executor_type=executor_type,
            identity_provider=identity_provider,
        )
    except Exception:
        return payload

    resolved: dict[str, Any] = {}
    missing: list[dict[str, Any]] = []
    for requirement in described.get("requirements", []):
        channel = requirement.get("channel")
        if not channel or channel in resolved:
            continue
        item = bindings.resolve(channel)
        resolved[channel] = {"key": item.key, "source": item.source}
        # 表单里随任务提交的凭据也算已配置：只填在任务 extra、没存设置页的用户不能被误拒
        if requirement.get("level") == "required" and not is_channel_configured(
            channel, item.key, dict(payload.get("extra") or {})
        ):
            missing.append({"channel": channel, "capability": requirement.get("capability")})

    if missing:
        raise MissingChannelError(missing)

    payload["resolved_channels"] = resolved
    return payload


def create_account_check_task(account_id: int) -> dict[str, Any]:
    platform = ""
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if model:
            platform = model.platform
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK,
        platform=platform,
        payload={"account_id": int(account_id)},
        progress_total=1,
    )


def create_account_check_all_task(platform: str = "", limit: int = 50) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform=platform,
        payload={"platform": platform, "limit": int(limit or 50)},
        progress_total=max(int(limit or 50), 1),
    )


def create_platform_action_task(payload: dict[str, Any]) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform=str(payload.get("platform", "")),
        payload=payload,
        progress_total=1,
    )


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        return serialize_task(task) if task else None


def list_tasks(*, platform: str = "", status: str = "", page: int = 1, page_size: int = 50) -> dict[str, Any]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    with Session(engine) as session:
        q = select(TaskModel)
        total_q = select(func.count()).select_from(TaskModel)
        if platform:
            q = q.where(TaskModel.platform == platform)
            total_q = total_q.where(TaskModel.platform == platform)
        if status:
            q = q.where(TaskModel.status == status)
            total_q = total_q.where(TaskModel.status == status)
        q = q.order_by(TaskModel.created_at.desc())
        total = int(session.exec(total_q).one() or 0)
        items = session.exec(q.offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "page": page, "items": [serialize_task(item) for item in items]}


def list_task_events(task_id: str, *, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 500)
    with Session(engine) as session:
        q = (
            select(TaskEventModel)
            .where(TaskEventModel.task_id == task_id)
            .where(TaskEventModel.id > since)
            .order_by(TaskEventModel.id)
            .limit(limit)
        )
        items = session.exec(q).all()
    return [serialize_event(item) for item in items]


def append_task_event(task_id: str, message: str, *, event_type: str = "log", level: str = "info", detail: dict | None = None) -> dict[str, Any]:
    with Session(engine) as session:
        event = TaskEventModel(
            task_id=task_id,
            type=event_type,
            level=level,
            message=message,
            detail_json=_dump_json(detail or {}),
        )
        session.add(event)
        session.commit()
        session.refresh(event)
    return serialize_event(event)


def mark_incomplete_tasks_interrupted() -> None:
    with Session(engine) as session:
        non_terminal = [TASK_STATUS_PENDING] + list(ACTIVE_TASK_STATUSES)
        tasks = session.exec(
            select(TaskModel).where(TaskModel.status.in_(non_terminal))
        ).all()
        # commit 会让实例过期，出了 session 再读 task.id 会抛 DetachedInstanceError，
        # 导致有未完成任务时服务根本起不来
        task_ids = []
        for task in tasks:
            task_ids.append(task.id)
            task.status = TASK_STATUS_INTERRUPTED
            task.error = task.error or "任务在服务重启后被中断"
            task.finished_at = _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
        session.commit()
    for task_id in task_ids:
        append_task_event(
            task_id,
            "任务在服务重启后被标记为中断",
            event_type="state",
            level="warning",
        )
        # 等待中的人工交互已经没有线程在接收回答，留着只会让 UI 误以为还能回答
        try:
            from infrastructure.task_interactions_repository import TaskInteractionsRepository

            TaskInteractionsRepository().cancel_pending_for_task(task_id)
        except Exception as exc:
            append_task_event(task_id, f"清理遗留人工交互失败: {exc}", event_type="state", level="warning")
    if task_ids:
        # 退号 / 释放邮箱要打第三方接口（每个都可能等到超时），不能挡在服务启动路径上；
        # 状态已经落库，回收晚几秒没有影响
        worker = threading.Thread(
            target=_reclaim_quietly,
            args=(list(task_ids),),
            name="reclaim-interrupted-attempts",
            daemon=True,
        )
        worker.start()


def _reclaim_quietly(task_ids: list[str]) -> None:
    try:
        reclaim_interrupted_attempts(task_ids)
    except Exception as exc:  # noqa: BLE001 - 后台回收失败只能记日志
        print(f"[TaskRuntime] 回收中断尝试资源失败: {exc}")


def reclaim_interrupted_attempts(task_ids: list[str] | None = None) -> int:
    """回收中断尝试占着的外部资源。

    进程挂掉时浏览器状态没了、续跑没意义，但租出去的号码还在计费、邮箱还被占着。
    检查点里记了资源 id，这里逐个退掉；已提交注册但没跑完的账号记一条 TaskLog 供人工处理。
    """
    from core.checkpoints import iter_attempt_checkpoints, release_attempt_resources

    reclaimed = 0
    with Session(engine) as session:
        query = select(TaskModel).where(TaskModel.status == TASK_STATUS_INTERRUPTED)
        if task_ids:
            query = query.where(TaskModel.id.in_(list(task_ids)))
        tasks = session.exec(query).all()
        pending = [
            (task.id, task.platform, task.get_result(), dict(task.get_payload().get("extra") or {}))
            for task in tasks
        ]

    for task_id, platform, result, task_extra in pending:
        for attempt_id, checkpoint in iter_attempt_checkpoints(result):
            if checkpoint.get("done"):
                # 成功收尾的尝试：号码已上报成功、邮箱已通知，再回收只会误伤
                continue
            messages = release_attempt_resources(checkpoint, platform=platform, extra=task_extra)
            for message in messages:
                append_task_event(task_id, message, event_type="state", level="warning")
                reclaimed += 1
            account = dict(checkpoint.get("resource_refs", {}).get("account") or {})
            if account.get("submitted"):
                # 与任务状态 interrupted 区分开：这条记录的是"半成品账号待人工核对"
                _save_task_log(
                    platform,
                    str(account.get("email") or ""),
                    TASK_LOG_STATUS_PENDING_REVIEW,
                    error="注册已提交但未完成，需人工确认",
                    detail={"attempt_id": attempt_id, "checkpoint_step": checkpoint.get("step", "")},
                )
    return reclaimed


def request_cancel(task_id: str) -> Optional[dict[str, Any]]:
    changed: list[bool] = []
    task = _mutate_task(
        task_id,
        lambda model: changed.append(_request_cancel_mutation(model)),
    )
    # 挂起等待人工的任务：把待回答的交互一并作废，否则 runner 还在阻塞
    try:
        from infrastructure.task_interactions_repository import TaskInteractionsRepository

        TaskInteractionsRepository().cancel_pending_for_task(task_id)
    except Exception:
        pass
    if not task:
        return None
    if any(changed):
        append_task_event(task_id, "已请求取消任务", event_type="state", level="warning")
    return serialize_task(task)


def _request_cancel_mutation(task: TaskModel) -> bool:
    """执行取消状态流转，返回是否真的改变了任务状态。"""
    if task.status in TERMINAL_TASK_STATUSES:
        return False
    if task.status == TASK_STATUS_PENDING:
        task.status = TASK_STATUS_CANCELLED
        task.finished_at = _utcnow()
        task.error = task.error or "任务在开始前被取消"
        return True
    if task.status == TASK_STATUS_CANCEL_REQUESTED:
        return False
    task.status = TASK_STATUS_CANCEL_REQUESTED
    return True


def claim_next_runnable_task(
    *,
    running_platform_counts: dict[str, int] | None = None,
    busy_account_keys: set[str] | None = None,
    max_parallel_per_platform: int = 1,
) -> Optional[dict[str, Any]]:
    running_platform_counts = dict(running_platform_counts or {})
    busy_account_keys = set(busy_account_keys or set())
    with Session(engine) as session:
        tasks = session.exec(
            select(TaskModel)
            .where(TaskModel.status == TASK_STATUS_PENDING)
            .order_by(TaskModel.created_at)
        ).all()
        for task in tasks:
            payload = task.get_payload()
            platform = task.platform or str(payload.get("platform", "") or "")
            account_keys = _task_account_keys(task.type, payload)
            if platform and running_platform_counts.get(platform, 0) >= max_parallel_per_platform:
                continue
            if account_keys and busy_account_keys.intersection(account_keys):
                continue
            task.status = TASK_STATUS_CLAIMED
            task.started_at = task.started_at or _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            return {"id": task.id, "platform": platform, "account_keys": account_keys}
    return None


class TaskLogger:
    def __init__(self, task_id: str):
        self.task_id = task_id

    def log(self, message: str, *, level: str = "info", event_type: str = "log", detail: dict | None = None) -> None:
        append_task_event(
            self.task_id,
            message,
            event_type=event_type,
            level=level,
            detail=detail,
        )
        try:
            print(f"[task:{self.task_id}] {message}")
        except (UnicodeEncodeError, OSError):
            # 控制台编码（Windows GBK 服务）吞不下 ✓ 之类字符时，日志已经落库，别让它把注册结果炸掉
            pass

    def mark_running(self) -> None:
        def _update(task: TaskModel) -> None:
            task.status = TASK_STATUS_RUNNING
            task.started_at = task.started_at or _utcnow()

        _mutate_task(self.task_id, _update)
        self.log("任务已开始执行", event_type="state")

    def is_cancel_requested(self) -> bool:
        with Session(engine) as session:
            task = session.get(TaskModel, self.task_id)
            return bool(task and task.status == TASK_STATUS_CANCEL_REQUESTED)

    def set_progress(self, current: int, total: Optional[int] = None) -> None:
        current = max(int(current), 0)

        def _update(task: TaskModel) -> None:
            task.progress_current = current
            if total is not None:
                task.progress_total = max(int(total), 0)

        _mutate_task(self.task_id, _update)

    def record_success(self) -> None:
        def _update(task: TaskModel) -> None:
            task.success_count += 1

        _mutate_task(self.task_id, _update)

    def record_error(self, error: str) -> None:
        def _update(task: TaskModel) -> None:
            task.error_count += 1
            result = task.get_result()
            errors = list(result.get("errors", []))
            errors.append(error)
            result["errors"] = errors
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def add_cashier_url(self, url: str) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            urls = list(result.get("cashier_urls", []))
            urls.append(url)
            result["cashier_urls"] = urls
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def set_result_data(self, data: Any) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            result["data"] = data
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def mark_waiting_human(self) -> None:
        def _update(task: TaskModel) -> None:
            if task.status == TASK_STATUS_RUNNING:
                task.status = TASK_STATUS_WAITING_HUMAN

        _mutate_task(self.task_id, _update)

    def mark_resumed(self) -> None:
        # waiting_human 是任务级状态，但挂起是尝试级的：并发 >1 时一个尝试拿到回答，
        # 另一个可能仍在等，只有没有任何 pending 交互了才翻回 running
        try:
            from infrastructure.task_interactions_repository import STATUS_PENDING, TaskInteractionsRepository

            if TaskInteractionsRepository().list_for_task(self.task_id, status=STATUS_PENDING):
                return
        except Exception:
            pass

        def _update(task: TaskModel) -> None:
            if task.status == TASK_STATUS_WAITING_HUMAN:
                task.status = TASK_STATUS_RUNNING

        _mutate_task(self.task_id, _update)

    def finish(self, status: str, *, error: str = "") -> None:
        def _update(task: TaskModel) -> None:
            task.status = status
            task.finished_at = _utcnow()
            if error:
                task.error = error

        _mutate_task(self.task_id, _update)
        event_level = "error" if status == TASK_STATUS_FAILED else ("warning" if status in {TASK_STATUS_INTERRUPTED, TASK_STATUS_CANCELLED} else "info")
        self.log(
            f"任务结束: {status}",
            level=event_level,
            event_type="state",
            detail={"status": status, "error": error},
        )


def _auto_push_any2api(task_logger: TaskLogger, account) -> None:
    """注册成功后自动推送账号到 Any2API（如果已配置）。"""
    try:
        from core.any2api_sync import push_account_to_any2api
        push_account_to_any2api(account, log_fn=task_logger.log)
    except Exception as exc:
        task_logger.log(f"  [Any2API] 自动推送异常: {exc}", level="warning")


def _auto_upload_cpa(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "chatgpt":
        return
    try:
        from core.config_store import config_store

        cpa_url = config_store.get("cpa_api_url", "")
        if cpa_url:
            from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa

            class _AccountProxy:
                pass

            target = _AccountProxy()
            target.email = account.email
            extra = account.extra or {}
            target.access_token = extra.get("access_token") or account.token
            target.refresh_token = extra.get("refresh_token", "")
            target.id_token = extra.get("id_token", "")
            target.session_token = extra.get("session_token", "")
            target.user_id = account.user_id or ""
            target.account_id = account.user_id or ""
            target.cookies = extra.get("cookies", "")

            token_data = generate_token_json(target)
            ok, msg = upload_to_cpa(token_data)
            task_logger.log(f"  [CPA] {'✓ ' + msg if ok else '✗ ' + msg}")
    except Exception as exc:
        task_logger.log(f"  [CPA] 自动上传异常: {exc}", level="warning")


def _build_platform_instance(platform_name: str, payload: dict[str, Any], logger: TaskLogger, resolved_proxy: str | None = None, shared_mailbox=None):
    from core.base_identity import normalize_identity_provider
    from core.base_mailbox import create_mailbox

    executor_type = str(payload.get("executor_type", "protocol") or "protocol")
    captcha_solver = str(payload.get("captcha_solver", "auto") or "auto")
    extra = dict(payload.get("extra") or {})
    config = RegisterConfig(
        executor_type=executor_type,
        captcha_solver=captcha_solver,
        proxy=resolved_proxy,
        extra=extra,
    )
    identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
    mailbox = shared_mailbox
    if mailbox is None and identity_provider == "mailbox":
        if not extra.get("mail_provider"):
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
        mailbox = create_mailbox(
            provider=extra.get("mail_provider", ""),
            extra=extra,
            proxy=resolved_proxy,
        )

    platform_cls = get(platform_name)
    platform = platform_cls(config=config, mailbox=mailbox)
    if hasattr(platform, "set_logger"):
        platform.set_logger(logger.log)
    else:
        platform._log_fn = logger.log
    return platform


def build_step_runtime(
    logger: TaskLogger,
    *,
    attempt_id: str,
    cancel_token,
    bindings=None,
    hold_on_failure: bool = False,
):
    """一次尝试的运行时契约：日志、取消令牌、人工通道、检查点、现场包目录、渠道绑定。"""
    from application.human_channel import DbHumanChannel
    from core.checkpoints import TaskCheckpointSink
    from core.registration.runtime import StepRuntime

    human = DbHumanChannel(
        logger.task_id,
        on_waiting=lambda interaction_id, request: logger.mark_waiting_human(),
        on_resumed=lambda interaction_id: logger.mark_resumed(),
        log=logger.log,
    )
    return StepRuntime(
        attempt_id=attempt_id,
        log=logger.log,
        cancel=cancel_token,
        human=human,
        checkpoint=TaskCheckpointSink(logger.task_id, mutate=_mutate_task),
        # 现场包按任务分目录（data/attempts/{task_id}/{attempt_id}/...）：
        # 保留策略按任务数算，一个 count=30 的任务不会把自己前面的失败现场清掉
        artifact_root=str(_artifacts_root() / logger.task_id),
        bindings=bindings,
        hold_on_failure=hold_on_failure,
    )


def _artifacts_root() -> Path:
    root = Path(__file__).resolve().parent.parent / "data" / "attempts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _notify_mailbox_from_platform(platform, *, ok: bool, reason: str = "", log_fn=None) -> None:
    from core.base_mailbox import notify_mailbox_outcome

    notify_mailbox_outcome(
        getattr(platform, "mailbox", None),
        getattr(platform, "_last_identity", None),
        ok=ok,
        reason=reason,
        platform=str(getattr(platform, "name", "") or ""),
        log_fn=log_fn,
    )


def _run_single_account_check(account_id: int, logger: TaskLogger | None = None) -> tuple[bool, dict[str, Any]]:
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            raise ValueError("账号不存在")
        plugin = get(model.platform)(config=RegisterConfig())
        account = build_platform_account(session, model)

    valid = plugin.check_valid(account)
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if model:
            model.updated_at = _utcnow()
            current_graph = load_account_graphs(session, [account_id]).get(account_id, {})
            summary_updates = {"checked_at": _utcnow_iso(), "valid": bool(valid)}
            if hasattr(plugin, "get_last_check_overview"):
                summary_updates.update(plugin.get_last_check_overview() or {})
            lifecycle_status = None
            if valid:
                lifecycle_status = recover_lifecycle_status_for_valid_account(current_graph)
            patch_account_graph(
                session,
                model,
                lifecycle_status=lifecycle_status,
                summary_updates=summary_updates,
            )
            session.add(model)
            session.commit()

    result = {"account_id": account_id, "valid": bool(valid), "platform": account.platform, "email": account.email}
    if logger:
        logger.log(f"{account.email}: {'有效' if valid else '失效'}")
    return valid, result


def execute_task(task_id: str) -> None:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        if not task:
            return
        task_type = task.type
        payload = task.get_payload()

    logger = TaskLogger(task_id)
    logger.mark_running()

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务在启动后立即被取消")
        return

    handlers: dict[str, Callable[[dict[str, Any], TaskLogger], None]] = {
        TASK_TYPE_REGISTER: _execute_register_task,
        TASK_TYPE_ACCOUNT_CHECK: _execute_account_check_task,
        TASK_TYPE_ACCOUNT_CHECK_ALL: _execute_account_check_all_task,
        TASK_TYPE_PLATFORM_ACTION: _execute_platform_action_task,
    }
    handler = handlers.get(task_type)
    if not handler:
        logger.finish(TASK_STATUS_FAILED, error=f"未知任务类型: {task_type}")
        return
    try:
        handler(payload, logger)
    finally:
        if task_type == TASK_TYPE_REGISTER:
            prune_attempt_artifacts()
        _forget_task_lock(task_id)


def _forget_task_lock(task_id: str) -> None:
    """任务结束后释放它的互斥锁条目，避免 _task_locks 随任务数无限增长。"""
    with _task_locks_guard:
        lock = _task_locks.get(task_id)
        if lock is not None and not lock.locked():
            _task_locks.pop(task_id, None)


def prune_attempt_artifacts() -> int:
    """失败现场包只保留最近的一批：每次注册任务结束与服务启动时各清一次。"""
    from core.registration.steps import prune_artifacts

    try:
        return prune_artifacts(_artifacts_root())
    except Exception:
        return 0


def _resolve_sms_provider_for_task(extra: dict[str, Any], bindings=None) -> tuple[str, dict[str, Any]]:
    from core.channel_bindings import SMS, ChannelBindings
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    settings_repo = ProviderSettingsRepository()
    definitions_repo = ProviderDefinitionsRepository()
    if bindings is None:
        bindings = ChannelBindings.from_payload({"extra": extra})
    provider_key = bindings.resolve(SMS).key
    if not provider_key:
        provider_key = "sms_activate" if extra.get("sms_activate_api_key") else ""
    definition = definitions_repo.get_by_key("sms", provider_key) if provider_key else None
    settings = settings_repo.resolve_runtime_settings("sms", provider_key, extra) if definition else dict(extra)
    return provider_key, settings


def _collect_channels_used(platform, payload: dict[str, Any]) -> dict[str, str]:
    """实际命中的渠道：优先 Step 引擎记录的，退回创建时钉住的解析值。

    两者可能不同——FallbackMailbox 会在主 provider 失败后换一家，
    统计要按真正出邮箱的那家算。
    """
    channels: dict[str, str] = {}
    for kind, item in dict(payload.get("resolved_channels") or {}).items():
        key = str((item or {}).get("key") or "")
        if key:
            channels[kind] = key

    # Step 引擎按渠道类别记录实际命中的 key（CapabilityResolver 已做能力→渠道映射）
    runtime = getattr(platform, "runtime", None)
    for channel, key in dict(getattr(runtime, "channels_used", None) or {}).items():
        if key:
            channels[channel] = str(key)

    identity = getattr(platform, "_last_identity", None)
    account = getattr(identity, "mailbox_account", None)
    actual_mailbox = str((getattr(account, "extra", None) or {}).get("mailbox_provider_key") or "")
    if actual_mailbox:
        channels["mailbox"] = actual_mailbox
    return channels


def _log_resolved_channels(payload: dict[str, Any], logger: TaskLogger) -> None:
    """任务日志首行写清本次用了谁、来源是哪层，出问题时不用翻配置猜。"""
    from core.channel_bindings import ResolvedChannel, describe_resolution

    resolved = dict(payload.get("resolved_channels") or {})
    if not resolved:
        return
    summary = describe_resolution(
        {
            kind: ResolvedChannel(kind, str(item.get("key") or ""), str(item.get("source") or ""))
            for kind, item in resolved.items()
        }
    )
    if summary:
        logger.log(f"本任务渠道: {summary}", detail={"resolved_channels": resolved})


def _resolve_oversubscribe_policy(provider_key: str, settings: dict[str, Any]) -> dict[str, Any]:
    """问 provider 要超发策略；provider 不可用或不支持复用时返回空策略。

    调度器不认识任何厂商名——号码能不能复用、能多发几个，都由 provider 自己回答。
    """
    if not provider_key:
        return {}
    try:
        from core.base_sms import create_sms_provider

        provider = create_sms_provider(provider_key, dict(settings or {}))
    except Exception:
        return {}
    try:
        policy = provider.oversubscribe_policy(settings) or {}
    except Exception:
        return {}
    return {
        "compensate_failures": bool(policy.get("compensate_failures")),
        "extra_max": max(_int_config(policy.get("extra_max"), 0), 0),
        "reuse_to_max": bool(policy.get("reuse_to_max")),
    }


def _bool_config(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _int_config(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _auto_followup_windsurf_payment(
    *,
    platform_name: str,
    payload: dict[str, Any],
    platform,
    account,
    logger: "TaskLogger",
) -> None:
    if platform_name != "windsurf":
        return
    executor_type = str(payload.get("executor_type", "") or "").strip()
    use_browser = executor_type in {"headless", "headed"}
    if not use_browser:
        extra_cfg = dict(payload.get("extra") or {})
        if not _bool_config(extra_cfg.get("auto_payment_link"), True):
            return
    if not str(getattr(account, "password", "") or "").strip() and use_browser:
        logger.log("Windsurf 注册后自动升级已跳过: 账号缺少密码", level="error")
        return
    extra = dict(payload.get("extra") or {})
    turnstile_token = str(extra.get("turnstile_token") or "").strip()
    if use_browser:
        action_id = "payment_link_browser"
        params = {
            "timeout": _int_config(extra.get("windsurf_payment_timeout"), 240),
            "headless": "true" if _bool_config(extra.get("windsurf_payment_headless"), False) else "false",
            "payment_channel": "checkout",
        }
        if turnstile_token:
            params["turnstile_token"] = turnstile_token
    else:
        action_id = "payment_link"
        params = {}
        if turnstile_token:
            params["turnstile_token"] = turnstile_token
    logger.log("注册成功，开始自动生成 Windsurf Pro Trial Stripe 链接")
    try:
        result = platform.execute_action(action_id, account, params)
    except Exception as exc:
        message = f"Windsurf 注册后自动升级失败: {exc}"
        logger.record_error(message)
        logger.log(message, level="error")
        return
    if not result.get("ok"):
        message = f"Windsurf 注册后自动升级失败: {result.get('error') or 'unknown error'}"
        logger.record_error(message)
        logger.log(message, level="error")
        return
    data = dict(result.get("data") or {})
    if data:
        merged_extra = dict(getattr(account, "extra", {}) or {})
        merged_extra.update(data)
        account.extra = merged_extra
        save_account(account)
    cashier_url = str(data.get("cashier_url") or data.get("url") or "").strip()
    if cashier_url:
        logger.log(f"Windsurf 自动升级链接已生成: {cashier_url}")
        logger.add_cashier_url(cashier_url)


def _execute_register_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    from core.proxy_pool import proxy_pool

    count = max(int(payload.get("count", 1) or 1), 1)
    concurrency = min(max(int(payload.get("concurrency", 1) or 1), 1), count, 5)
    platform_name = str(payload.get("platform", ""))
    email = payload.get("email") or None
    password = payload.get("password") or None
    proxy = payload.get("proxy") or None
    extra = dict(payload.get("extra") or {})
    # 渠道绑定只在这里解析一次（含创建时钉住的 channel_bindings 与 extra 旧键），
    # 之后所有解析路径都读同一个对象，不再各自回落到全局默认
    from core.channel_bindings import MAILBOX, ChannelBindings

    bindings = ChannelBindings.from_payload(payload, platform=platform_name)
    sms_provider_key, sms_settings = _resolve_sms_provider_for_task(extra, bindings=bindings)
    # 只有浏览器注册流程会去租号，协议流程压根不碰短信，
    # 套用号码复用的超发逻辑只会污染进度和日志
    executor_type = str(payload.get("executor_type", "protocol") or "protocol").strip().lower()
    phone_capable = executor_type in {"headless", "headed"}
    oversubscribe = _resolve_oversubscribe_policy(sms_provider_key, sms_settings) if phone_capable else {}
    extra_max = max(int(oversubscribe.get("extra_max") or 0), 0)
    # 补投：失败不占用成功名额，继续投尝试直到达成目标
    compensate_enabled = bool(oversubscribe.get("compensate_failures"))
    # 超发：号码还能复用时，允许多成功几个
    oversubscribe_enabled = compensate_enabled and bool(oversubscribe.get("reuse_to_max"))
    target_success = count
    max_success = count + extra_max if oversubscribe_enabled else count
    progress_total = max_success
    # 尝试预算：号码复用模式沿用 3 倍上限；普通模式给 count 之外再补 ceil(count/2) 次，
    # 只用于补投"可重试"类失败（等码超时、临时无号、号码被拒），配置错 / 站点变更不补
    if compensate_enabled:
        max_attempts = max(max_success * 3, 1)
    else:
        max_attempts = count + max(1, math.ceil(count / 2))

    logger.set_progress(0, progress_total)
    _log_resolved_channels(payload, logger)
    if compensate_enabled:
        logger.log(
            f"号码复用模式({sms_provider_key}): 成功目标 {target_success}，失败自动补尝试，"
            f"号码仍可复用时最多额外成功 {extra_max if oversubscribe_enabled else 0} 个"
        )
    else:
        logger.log(f"注册 {count} 个账号，可重试类失败最多补投 {max_attempts - count} 次")

    try:
        get(platform_name)
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    success = 0
    errors: list[str] = []
    fatal_errors: list[str] = []
    # 整批共享一个取消令牌：外部取消与"某次尝试撞上致命错误"都从这里生效
    cancel_token = CancellationToken(logger.is_cancel_requested)

    # Pre-create a shared mailbox instance for the entire task to avoid
    # concurrent initialization issues (e.g. MoeMail auto-registering
    # multiple provider accounts simultaneously).
    shared_mailbox = None
    try:
        from core.base_identity import normalize_identity_provider
        from core.base_mailbox import create_mailbox

        identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
        if identity_provider == "mailbox":
            # 邮箱 provider 以钉住的绑定为准；写回 extra 只为 identity 快照等旧读者
            extra["mail_provider"] = bindings.resolve(MAILBOX).key
            shared_mailbox = create_mailbox(
                provider=extra.get("mail_provider", ""),
                extra=extra,
                proxy=proxy or None,
            )
    except Exception as exc:
        logger.log(f"邮箱初始化失败: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=f"邮箱初始化失败: {exc}")
        return

    # 每次尝试新建的 platform 实例从 payload["extra"] 取配置：让它看到的 mail_provider
    # 与上面钉住的绑定一致（identity 快照等旧读者仍直读这个键）
    payload = {**payload, "extra": dict(extra)}

    def _do_one(index: int) -> _AttemptOutcome:
        if cancel_token.is_cancelled():
            return _AttemptOutcome.cancelled()
        resolved_proxy = proxy or proxy_pool.get_next()
        platform = _build_platform_instance(platform_name, payload, logger, resolved_proxy=resolved_proxy, shared_mailbox=shared_mailbox)
        attempt_id = f"{logger.task_id}-{index + 1}"
        runtime = build_step_runtime(
            logger,
            attempt_id=attempt_id,
            cancel_token=cancel_token,
            bindings=bindings,
            hold_on_failure=(executor_type == "headed"),
        )
        platform.set_runtime(runtime)
        mailbox_notified = False
        try:
            # 两种模式都可能补投，用 "第 N 次尝试" 而不是 "第 N/count 个账号"
            logger.log(f"开始第 {index + 1} 次注册尝试 (成功目标 {target_success}，尝试上限 {max_attempts})")
            if resolved_proxy:
                logger.log(f"使用代理: {resolved_proxy}")
            account = platform.register(email=email, password=password)
            channels_used = _collect_channels_used(platform, payload)
            if channels_used:
                account.extra = {**dict(account.extra or {}), "channels": channels_used}
            save_account(account)
            _notify_mailbox_from_platform(platform, ok=True, log_fn=logger.log)
            mailbox_notified = True
            _auto_followup_windsurf_payment(
                platform_name=platform_name,
                payload=payload,
                platform=platform,
                account=account,
                logger=logger,
            )
            if resolved_proxy:
                proxy_pool.report_success(resolved_proxy)
            logger.record_success()
            logger.log(f"✓ 注册成功: {account.email}")
            _save_task_log(platform_name, account.email, "success", detail={"channels": channels_used})
            _auto_upload_cpa(logger, account)
            _auto_push_any2api(logger, account)
            extra = dict(account.extra or {})
            overview = dict(extra.get("account_overview") or {})
            cashier_url = str(extra.get("cashier_url") or overview.get("cashier_url") or "")
            if cashier_url:
                logger.log(f"  [升级链接] {cashier_url}")
                logger.add_cashier_url(cashier_url)
            return _AttemptOutcome.succeeded()
        except AttemptCancelled as exc:
            if not mailbox_notified:
                _notify_mailbox_from_platform(platform, ok=False, reason="任务已取消", log_fn=logger.log)
            reason = cancel_token.reason or str(exc) or "取消请求"
            logger.log(f"尝试已中止: {reason}", level="warning")
            return _AttemptOutcome.cancelled()
        except Exception as exc:
            if resolved_proxy:
                proxy_pool.report_fail(resolved_proxy)
            error = str(exc)
            category = classify(exc)
            if not mailbox_notified:
                _notify_mailbox_from_platform(platform, ok=False, reason=error, log_fn=logger.log)
            logger.record_error(error)
            logger.log(f"✗ 注册失败[{category}]: {error}", level="error")
            _save_task_log(
                platform_name,
                email or "",
                "failed",
                error=error,
                detail={"category": category, "channels": _collect_channels_used(platform, payload)},
            )
            if is_fatal(exc):
                # 配置错 / 站点变更：再投多少次都是同样的结果，顺手停掉在飞的兄弟尝试
                cancel_token.trip(f"{category}: {error}")
                fatal_errors.append(error)
                logger.log(f"该失败属于 {category} 类，已停止投递后续尝试", level="error")
            return _AttemptOutcome.failed(error, retryable=is_retryable(exc))
        finally:
            # 走到这里说明尝试在进程内结束了：成功路径已上报、失败 / 取消路径已补偿并通知邮箱。
            # 检查点里残留的引用不再代表"仍被占用"，重启回收要跳过它——回收只针对进程死掉时在飞的尝试
            try:
                runtime.checkpoint.mark_done(attempt_id)
            except Exception as exc:  # noqa: BLE001 - 收尾标记失败不能反过来把结果吞掉
                logger.log(f"标记尝试收尾失败（不影响结果）: {exc}", level="warning")

    try:
        submitted = 0
        # 不可补投的失败（未分类 / 渠道故障之外的硬失败）占掉一个名额
        hard_failures = 0
        futures: dict[Any, int] = {}

        # 该判定会在调度循环里被重复求值，用签名去重避免同一状态刷屏
        reuse_logged: set[tuple[str, int]] = set()

        def _phone_still_reusable() -> bool:
            if not oversubscribe_enabled:
                return False

            try:
                from core.base_sms import probe_phone_reuse

                alive, info = probe_phone_reuse(sms_provider_key, sms_settings)
                if alive:
                    phone = str(info.get("phone_number") or "")
                    use_count = int(info.get("use_count") or 0)
                    if (phone, use_count) not in reuse_logged:
                        reuse_logged.add((phone, use_count))
                        logger.log(
                            "号码仍可复用: "
                            f"{phone[:5]}**** "
                            f"剩余 {int(info.get('remaining_seconds') or 0)} 秒，"
                            f"已成功 {use_count} 次"
                        )
                return bool(alive)
            except Exception:
                return False

        def _should_submit_more() -> bool:
            if submitted >= max_attempts or cancel_token.is_cancelled():
                return False
            if not compensate_enabled:
                # 名额 = 成功 + 硬失败 + 在飞；可重试失败让出名额，由预算内的新尝试补位
                return success + hard_failures + len(futures) < count
            if success + len(futures) >= max_success:
                return False
            if success < target_success:
                return True
            if success >= max_success:
                return False
            return _phone_still_reusable()

        def _progress_current() -> int:
            if compensate_enabled:
                return min(success, progress_total)
            return min(success + hard_failures, progress_total)

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            while _should_submit_more() and len(futures) < concurrency:
                futures[pool.submit(_do_one, submitted)] = submitted
                submitted += 1

            while futures:
                done, _ = wait(set(futures.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future, None)
                    outcome: _AttemptOutcome = future.result()
                    if outcome.status == "success":
                        success += 1
                    elif outcome.status == "failed":
                        errors.append(outcome.error)
                        if not outcome.retryable:
                            hard_failures += 1
                        elif submitted >= max_attempts:
                            # 预算已经用完，这次可重试失败没人再补，名额就此作废
                            hard_failures += 1
                        else:
                            logger.log(f"该失败可补投，将在预算内追加一次尝试（已投 {submitted}/{max_attempts}）")
                    logger.set_progress(_progress_current(), progress_total)
                while _should_submit_more() and len(futures) < concurrency:
                    futures[pool.submit(_do_one, submitted)] = submitted
                    submitted += 1
                if cancel_token.is_cancelled() and not futures:
                    break
        if not cancel_token.is_cancelled():
            # 收尾把进度补满：预算耗尽后仍空着的名额也已经没有后续了
            logger.set_progress(progress_total if not compensate_enabled else _progress_current(), progress_total)
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    logger.set_result_data({
        "target_count": target_success,
        "attempts": submitted,
        "success": success,
        "fail": len(errors),
        "extra_success": max(0, success - target_success),
        "phone_reuse": bool(compensate_enabled),
    })
    summary = f"完成: 成功 {success} 个, 失败 {len(errors)} 个"
    logger.log(summary, event_type="summary")
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    final_status = TASK_STATUS_FAILED if errors and success == 0 else TASK_STATUS_SUCCEEDED
    final_error = "" if final_status == TASK_STATUS_SUCCEEDED else errors[0]
    # 致命错误（配置 / 站点变更）即使有成功也要把原因顶到任务上，否则只能翻日志
    if fatal_errors and final_status == TASK_STATUS_SUCCEEDED:
        logger.log(f"注意: 本批出现致命错误并已停止投递 — {fatal_errors[0]}", level="warning")
    elif fatal_errors:
        final_error = fatal_errors[0]
    logger.finish(final_status, error=final_error)


def _execute_platform_action_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    command_platform = str(payload.get("platform", ""))
    account_id = int(payload.get("account_id", 0) or 0)
    action_id = str(payload.get("action_id", ""))
    params = dict(payload.get("params") or {})
    runtime = PlatformRuntime()
    result = runtime.execute_action(
        type("Command", (), {
            "platform": command_platform,
            "account_id": account_id,
            "action_id": action_id,
            "params": params,
        })()
    )
    if not result.ok:
        logger.record_error(result.error)
        logger.finish(TASK_STATUS_FAILED, error=result.error)
        return
    logger.set_result_data(result.data)
    message = ""
    if isinstance(result.data, dict):
        message = str(result.data.get("message", "") or "")
    if message:
        logger.log(message, event_type="summary")
    logger.set_progress(1, 1)
    logger.finish(TASK_STATUS_SUCCEEDED)


def _execute_account_check_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    account_id = int(payload.get("account_id", 0) or 0)
    if account_id <= 0:
        logger.finish(TASK_STATUS_FAILED, error="缺少 account_id")
        return
    try:
        _, result = _run_single_account_check(account_id, logger)
        logger.set_result_data(result)
        logger.set_progress(1, 1)
        logger.finish(TASK_STATUS_SUCCEEDED)
    except Exception as exc:
        logger.record_error(str(exc))
        logger.finish(TASK_STATUS_FAILED, error=str(exc))


def _execute_account_check_all_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    platform = str(payload.get("platform", "") or "")
    limit = max(int(payload.get("limit", 50) or 50), 1)

    with Session(engine) as session:
        q = select(AccountModel)
        if platform:
            q = q.where(AccountModel.platform == platform)
        q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        accounts = session.exec(q.limit(limit)).all()

    total = len(accounts)
    logger.set_progress(0, total)
    if total == 0:
        logger.set_result_data({"valid": 0, "invalid": 0, "error": 0})
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    results = {"valid": 0, "invalid": 0, "error": 0}
    completed = 0
    for model in accounts:
        if logger.is_cancel_requested():
            logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
            return
        try:
            valid, _ = _run_single_account_check(int(model.id or 0), logger)
            if valid:
                results["valid"] += 1
            else:
                results["invalid"] += 1
        except Exception as exc:
            results["error"] += 1
            logger.record_error(str(exc))
            logger.log(f"{model.email}: 检测异常 {exc}", level="error")
        completed += 1
        logger.set_progress(completed, total)
    logger.set_result_data(results)
    logger.finish(TASK_STATUS_SUCCEEDED)
