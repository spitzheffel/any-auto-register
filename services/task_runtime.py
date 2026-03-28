"""Persistent task runtime backed by child worker processes."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import subprocess
import sys
import threading
import time

from application.tasks import (
    TASK_STATUS_CANCELLED,
    TASK_STATUS_CANCEL_REQUESTED,
    TASK_STATUS_INTERRUPTED,
    TERMINAL_TASK_STATUSES,
    claim_next_runnable_task,
    force_finish_task,
    get_task,
    mark_incomplete_tasks_interrupted,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_MODULE = "services.task_worker"


@dataclass(slots=True)
class TaskWorkerState:
    process: subprocess.Popen
    platform: str = ""
    account_keys: set[str] = field(default_factory=set)
    cancel_requested: bool = False


class TaskRuntime:
    def __init__(self, *, max_parallel_tasks: int = 3, max_parallel_per_platform: int = 1, poll_interval: float = 0.5):
        self.max_parallel_tasks = max_parallel_tasks
        self.max_parallel_per_platform = max_parallel_per_platform
        self.poll_interval = poll_interval
        self._running = False
        self._dispatcher: threading.Thread | None = None
        self._workers: dict[str, TaskWorkerState] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            mark_incomplete_tasks_interrupted()
            self._dispatcher = threading.Thread(target=self._loop, daemon=True, name="task-runtime")
            self._dispatcher.start()
            print("[TaskRuntime] 已启动")

    def stop(self) -> None:
        with self._lock:
            self._running = False
            active = list(self._workers.items())
        for task_id, state in active:
            self._terminate_worker(task_id, state)
            force_finish_task(
                task_id,
                status=TASK_STATUS_INTERRUPTED,
                error="服务停止时任务进程被终止",
                event_message="任务进程已在服务停止时被终止",
            )
        print("[TaskRuntime] 停止中")

    def wake_up(self) -> None:
        # Polling loop wakes quickly already; this method exists as an explicit runtime hook.
        return

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            state = self._workers.get(task_id)
            if not state:
                return False
            state.cancel_requested = True
        self._terminate_worker(task_id, state)
        force_finish_task(
            task_id,
            status=TASK_STATUS_CANCELLED,
            error="任务已被强制终止",
            event_message="任务进程已被强制终止",
        )
        return True

    def _loop(self) -> None:
        while self._running:
            self._reap_workers()
            with self._lock:
                available_slots = self.max_parallel_tasks - len(self._workers)
                running_platform_counts: dict[str, int] = {}
                busy_account_keys: set[str] = set()
                for state in self._workers.values():
                    if state.platform:
                        running_platform_counts[state.platform] = running_platform_counts.get(state.platform, 0) + 1
                    busy_account_keys.update(state.account_keys)
            while available_slots > 0 and self._running:
                task_info = claim_next_runnable_task(
                    running_platform_counts=running_platform_counts,
                    busy_account_keys=busy_account_keys,
                    max_parallel_per_platform=self.max_parallel_per_platform,
                )
                if not task_info:
                    break
                task_id = task_info["id"]
                try:
                    process = self._spawn_worker(task_id)
                except Exception as exc:
                    force_finish_task(
                        task_id,
                        status=TASK_STATUS_INTERRUPTED,
                        error=f"任务进程启动失败: {exc}",
                        event_message=f"任务进程启动失败: {exc}",
                        level="error",
                    )
                    break
                with self._lock:
                    self._workers[task_id] = TaskWorkerState(
                        process=process,
                        platform=str(task_info.get("platform", "") or ""),
                        account_keys=set(task_info.get("account_keys") or []),
                    )
                    if task_info.get("platform"):
                        running_platform_counts[str(task_info["platform"])] = running_platform_counts.get(str(task_info["platform"]), 0) + 1
                    busy_account_keys.update(set(task_info.get("account_keys") or []))
                available_slots -= 1
            time.sleep(self.poll_interval)
        self._reap_workers()

    def _spawn_worker(self, task_id: str) -> subprocess.Popen:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        popen_kwargs: dict = {
            "args": [sys.executable, "-m", WORKER_MODULE, task_id],
            "cwd": str(PROJECT_ROOT),
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            popen_kwargs["start_new_session"] = True
        return subprocess.Popen(**popen_kwargs)

    def _terminate_worker(self, task_id: str, state: TaskWorkerState) -> None:
        process = state.process
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=10,
                )
            else:
                os.killpg(process.pid, 9)
        except Exception as exc:
            print(f"[TaskRuntime] 终止任务进程失败 {task_id}: {exc}")

    def _reap_workers(self) -> None:
        finished: list[tuple[str, TaskWorkerState, int]] = []
        with self._lock:
            for task_id, state in list(self._workers.items()):
                exit_code = state.process.poll()
                if exit_code is None:
                    continue
                finished.append((task_id, state, int(exit_code)))
                self._workers.pop(task_id, None)

        for task_id, state, exit_code in finished:
            task = get_task(task_id)
            if not task:
                continue
            if task["status"] in TERMINAL_TASK_STATUSES:
                continue
            if state.cancel_requested or task["status"] == TASK_STATUS_CANCEL_REQUESTED:
                force_finish_task(
                    task_id,
                    status=TASK_STATUS_CANCELLED,
                    error="任务已被强制终止",
                    event_message="任务进程已被强制终止",
                )
                continue
            message = "任务进程异常退出"
            if exit_code != 0:
                message = f"{message} (exit code {exit_code})"
            force_finish_task(
                task_id,
                status=TASK_STATUS_INTERRUPTED,
                error=message,
                event_message=message,
                level="error" if exit_code != 0 else "warning",
            )


task_runtime = TaskRuntime()
