"""待人工处理交互的仓储。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import update
from sqlmodel import Session, select

from core.db import TaskInteractionModel, engine

STATUS_PENDING = "pending"
STATUS_ANSWERED = "answered"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    # 与任务接口同一口径：SQLite 读回的是 naive UTC，不带 Z 前端会按本地时区解释
    from core.datetime_utils import serialize_datetime

    return serialize_datetime(value)


def serialize_interaction(model: TaskInteractionModel) -> dict:
    return {
        "id": model.id,
        "task_id": model.task_id,
        "attempt_id": model.attempt_id,
        "step": model.step,
        "kind": model.kind,
        "prompt": model.prompt,
        "schema": model.get_schema(),
        "context": model.get_context(),
        "status": model.status,
        "response": model.get_response(),
        "created_at": _iso(model.created_at),
        "expires_at": _iso(model.expires_at),
        "answered_at": _iso(model.answered_at),
    }


class TaskInteractionsRepository:
    def create(
        self,
        *,
        task_id: str,
        attempt_id: str = "",
        step: str = "",
        kind: str = "confirm",
        prompt: str = "",
        schema: dict | None = None,
        context: dict | None = None,
        timeout_seconds: int = 900,
    ) -> int:
        model = TaskInteractionModel(
            task_id=task_id,
            attempt_id=attempt_id,
            step=step,
            kind=kind,
            prompt=prompt,
            expires_at=_utcnow() + timedelta(seconds=max(1, int(timeout_seconds))),
        )
        model.set_schema(schema or {})
        model.set_context(context or {})
        with Session(engine) as session:
            session.add(model)
            session.commit()
            session.refresh(model)
            return int(model.id)

    def get(self, interaction_id: int) -> Optional[dict]:
        with Session(engine) as session:
            model = session.get(TaskInteractionModel, interaction_id)
            return serialize_interaction(model) if model else None

    def list_for_task(self, task_id: str, *, status: str = "") -> list[dict]:
        with Session(engine) as session:
            query = select(TaskInteractionModel).where(TaskInteractionModel.task_id == task_id)
            if status:
                query = query.where(TaskInteractionModel.status == status)
            items = session.exec(query.order_by(TaskInteractionModel.id.desc())).all()
            return [serialize_interaction(item) for item in items]

    def answer(self, interaction_id: int, response: dict) -> bool:
        """把 pending 行翻成 answered，返回是否由本次调用完成了翻转。

        UI 双击、两个操作员同时提交时，"先读再写"的两次读都不持锁，都会看到 pending
        然后双写；只有把 status='pending' 放进 UPDATE 的 WHERE 里，才能保证恰好一个赢。
        """
        return self._leave_pending(
            interaction_id,
            STATUS_ANSWERED,
            response_json=json.dumps(response or {}, ensure_ascii=False),
        )

    def finish(self, interaction_id: int, status: str) -> bool:
        """把 pending 行置为 expired/cancelled；返回 False 表示行已被别处翻走（多半是回答刚落库）。"""
        return self._leave_pending(interaction_id, status)

    def _leave_pending(self, interaction_id: int, status: str, **values) -> bool:
        with Session(engine) as session:
            result = session.exec(
                update(TaskInteractionModel)
                .where(
                    TaskInteractionModel.id == interaction_id,
                    TaskInteractionModel.status == STATUS_PENDING,
                )
                .values(status=status, answered_at=_utcnow(), **values)
                .execution_options(synchronize_session=False)
            )
            session.commit()
            return result.rowcount == 1

    def cancel_pending_for_task(self, task_id: str) -> int:
        with Session(engine) as session:
            result = session.exec(
                update(TaskInteractionModel)
                .where(
                    TaskInteractionModel.task_id == task_id,
                    TaskInteractionModel.status == STATUS_PENDING,
                )
                .values(status=STATUS_CANCELLED, answered_at=_utcnow())
                .execution_options(synchronize_session=False)
            )
            session.commit()
            return int(result.rowcount or 0)

    def is_expired(self, interaction_id: int) -> bool:
        with Session(engine) as session:
            model = session.get(TaskInteractionModel, interaction_id)
            if not model or not model.expires_at:
                return False
            return _as_aware(model.expires_at) <= _utcnow()

    def status_of(self, interaction_id: int) -> str:
        with Session(engine) as session:
            model = session.get(TaskInteractionModel, interaction_id)
            return model.status if model else ""
