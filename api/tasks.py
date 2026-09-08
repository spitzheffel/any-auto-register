from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from application.human_channel import notify_answered
from application.tasks_query import TasksQueryService
from infrastructure.task_interactions_repository import (
    STATUS_PENDING,
    TaskInteractionsRepository,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])
service = TasksQueryService()
interactions = TaskInteractionsRepository()


class InteractionAnswer(BaseModel):
    value: str = ""
    values: dict[str, str] = {}
    option: str = ""


@router.get("")
def list_tasks(platform: str = "", status: str = "", page: int = 1, page_size: int = 50):
    return service.list_tasks(platform=platform, status=status, page=page, page_size=page_size)


@router.get("/{task_id}")
def get_task(task_id: str):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/{task_id}/events")
def list_task_events(task_id: str, since: int = 0, limit: int = 200):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return service.list_events(task_id, since=since, limit=limit)


# ---------------------------------------------------------------------------
# 人工介入
# ---------------------------------------------------------------------------

@router.get("/{task_id}/interactions")
def list_task_interactions(task_id: str, status: str = STATUS_PENDING):
    if not service.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    return {"items": interactions.list_for_task(task_id, status=status)}


@router.post("/{task_id}/interactions/{interaction_id}/respond")
def respond_to_interaction(task_id: str, interaction_id: int, body: InteractionAnswer):
    item = interactions.get(interaction_id)
    if not item or item["task_id"] != task_id:
        raise HTTPException(404, "交互请求不存在")

    payload = _validate_answer(item, body)
    # 这里不预判 status：读到 pending 再写并不原子，双击会双写、唤醒两次；
    # 胜负只由仓储层的条件更新裁定，输的一方拿 409。
    if not interactions.answer(interaction_id, payload):
        raise HTTPException(409, "该交互已被回答或已关闭")
    notify_answered(interaction_id)
    return interactions.get(interaction_id)


def _validate_answer(item: dict, body: InteractionAnswer) -> dict:
    kind = item.get("kind")
    schema = item.get("schema") or {}

    if kind == "confirm":
        return {"value": "confirmed"}

    if kind == "choice":
        options = list(schema.get("options") or [])
        if body.option not in options:
            raise HTTPException(400, f"option 必须是以下之一: {options}")
        return {"option": body.option}

    fields = list(schema.get("fields") or [])
    if fields and len(fields) > 1:
        missing = [name for name in fields if not (body.values or {}).get(name)]
        if missing:
            raise HTTPException(400, f"缺少字段: {missing}")
        return {"values": dict(body.values)}

    value = body.value or (body.values or {}).get(fields[0] if fields else "value") or ""
    if not value:
        raise HTTPException(400, "value 不能为空")
    return {"value": value}


# ---------------------------------------------------------------------------
# 失败现场
# ---------------------------------------------------------------------------

def _artifact_root() -> Path:
    # 与任务层写入现场包用的是同一个定义，避免两处路径悄悄不一致
    from application.tasks import _artifacts_root

    return _artifacts_root()


def _safe_artifact_path(root: Path, *parts: str) -> Path:
    """把 URL 片段拼到 root 之下；任何有机会跳出 root 的写法一律按"不存在"处理。

    现场目录里是未脱敏的页面与网络数据。之前用 str.startswith 做前缀比较，
    `data/attempts_backup` 也以 `data/attempts` 开头，`..` 一跳就能读到兄弟目录；
    所以这里先按片段拒绝 `..`、分隔符（Windows 下 `\\` 也是分隔符）和点开头的名字，
    最后再用 resolve 后的父子关系兜底——符号链接、盘符相对路径只有 resolve 才看得出来。
    """
    for part in parts:
        if not part or part.startswith(".") or "/" in part or "\\" in part:
            raise HTTPException(404, "文件不存在")
    root = root.resolve()
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(404, "文件不存在")
    return target


def _resolve_attempt_artifact(task_id: str, attempt_id: str, *rest: str) -> Path:
    if not service.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    # attempt_id 形如 {task_id}-{n}；不校验归属的话，拿任务 A 的 URL 就能读任务 B 的现场
    if not attempt_id.startswith(f"{task_id}-"):
        raise HTTPException(404, "attempt 不存在")
    # 目录布局与 build_step_runtime 一致：{root}/{task_id}/{attempt_id}/{bundle}/{file}
    return _safe_artifact_path(_artifact_root(), task_id, attempt_id, *rest)


@router.get("/{task_id}/attempts/{attempt_id}/artifacts")
def list_attempt_artifacts(task_id: str, attempt_id: str):
    root = _resolve_attempt_artifact(task_id, attempt_id)
    # 没保存过现场的 attempt 是正常情况，给空列表而不是 404
    if not root.is_dir():
        return {"items": []}
    items = []
    for bundle in sorted(root.iterdir()):
        if not bundle.is_dir():
            continue
        items.append(
            {
                "name": bundle.name,
                "files": sorted(child.name for child in bundle.iterdir() if child.is_file()),
            }
        )
    return {"items": items}


@router.get("/{task_id}/attempts/{attempt_id}/artifacts/{bundle}/{filename}")
def download_attempt_artifact(task_id: str, attempt_id: str, bundle: str, filename: str):
    target = _resolve_attempt_artifact(task_id, attempt_id, bundle, filename)
    if not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target)
