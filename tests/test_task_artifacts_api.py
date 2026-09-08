"""失败现场（artifacts）接口：只能读到本任务、本 attempt 目录下的文件。"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session

import api.tasks as tasks_api
from core.db import TaskModel, engine

TASK_ID = "task_artifacts"
OTHER_TASK_ID = "task_other"


@pytest.fixture()
def artifacts_root(tmp_path, monkeypatch) -> Path:
    """把接口的现场根目录指到 tmp_path/attempts，旁边放一个同前缀的兄弟目录当诱饵。"""
    root = tmp_path / "attempts"
    monkeypatch.setattr(tasks_api, "_artifact_root", lambda: root)

    # 布局与 build_step_runtime 一致：{root}/{task_id}/{attempt_id}/{bundle}/{file}
    bundle = root / TASK_ID / f"{TASK_ID}-1" / "final"
    bundle.mkdir(parents=True)
    (bundle / "page.html").write_text("<html>mine</html>", encoding="utf-8")
    (bundle / "network.json").write_text("[]", encoding="utf-8")

    other_bundle = root / OTHER_TASK_ID / f"{OTHER_TASK_ID}-1" / "final"
    other_bundle.mkdir(parents=True)
    (other_bundle / "page.html").write_text("<html>not yours</html>", encoding="utf-8")

    # 与 attempts 同前缀的兄弟目录：`attempt_id="..", bundle="attempts_backup"` 正好能拼到它
    sibling = tmp_path / "attempts_backup"
    sibling.mkdir(parents=True)
    (sibling / "dump.txt").write_text("top secret", encoding="utf-8")
    # 根目录下的散文件：`bundle=".."` 能拼到它，而且仍在 root 之内，只靠 resolve 兜底拦不住
    (root / "index.txt").write_text("top secret", encoding="utf-8")
    return root


@pytest.fixture()
def tasks_in_db():
    # 用终态直接落库：pending 任务会被 client 拉起的 task_runtime 抢走执行
    with Session(engine) as session:
        session.add(TaskModel(id=TASK_ID, type="register", platform="cursor", status="failed"))
        session.add(TaskModel(id=OTHER_TASK_ID, type="register", platform="cursor", status="failed"))
        session.commit()


def _list_url(task_id: str, attempt_id: str) -> str:
    return f"/api/tasks/{task_id}/attempts/{attempt_id}/artifacts"


def _file_url(task_id: str, attempt_id: str, bundle: str, filename: str) -> str:
    return f"/api/tasks/{task_id}/attempts/{attempt_id}/artifacts/{bundle}/{filename}"


def _assert_rejected(resp) -> None:
    # 没匹配到 API 路由的请求会落到 SPA 的 index.html（200），所以要确认 404 是接口自己给的 JSON
    assert resp.status_code == 404
    assert "detail" in resp.json()
    assert "top secret" not in resp.text
    assert "not yours" not in resp.text


class TestHappyPath:
    def test_lists_bundles_and_files(self, client, artifacts_root, tasks_in_db):
        resp = client.get(_list_url(TASK_ID, f"{TASK_ID}-1"))

        assert resp.status_code == 200
        assert resp.json() == {"items": [{"name": "final", "files": ["network.json", "page.html"]}]}

    def test_downloads_file(self, client, artifacts_root, tasks_in_db):
        resp = client.get(_file_url(TASK_ID, f"{TASK_ID}-1", "final", "page.html"))

        assert resp.status_code == 200
        assert resp.text == "<html>mine</html>"

    def test_attempt_without_artifacts_lists_nothing(self, client, artifacts_root, tasks_in_db):
        resp = client.get(_list_url(TASK_ID, f"{TASK_ID}-7"))

        assert resp.status_code == 200
        assert resp.json() == {"items": []}

    def test_missing_file_is_404_not_500(self, client, artifacts_root, tasks_in_db):
        resp = client.get(_file_url(TASK_ID, f"{TASK_ID}-1", "final", "nope.html"))

        assert resp.status_code == 404

    def test_unknown_task_is_404(self, client, artifacts_root):
        resp = client.get(_list_url("ghost", "ghost-1"))

        assert resp.status_code == 404


class TestTraversalIsRejected:
    """`..` 用 %2E%2E 传，否则 httpx 在客户端就把点段归一化掉了，到不了服务端。"""

    def test_dotdot_attempt_cannot_list_parent_directory(self, client, artifacts_root, tasks_in_db):
        resp = client.get(_list_url(TASK_ID, "%2E%2E"))

        _assert_rejected(resp)
        assert "attempts_backup" not in resp.text

    def test_dotdot_cannot_reach_sibling_directory(self, client, artifacts_root, tasks_in_db):
        resp = client.get(_file_url(TASK_ID, "%2E%2E", "attempts_backup", "dump.txt"))

        _assert_rejected(resp)

    def test_backslash_segment_cannot_escape_on_windows(self, client, artifacts_root, tasks_in_db):
        # 前缀合法、只靠反斜杠往上跳：Windows 上 `\\` 也是分隔符，路径段校验必须一起拒掉
        attempt = f"{TASK_ID}-1%5C..%5C.."
        resp = client.get(_file_url(TASK_ID, attempt, "attempts_backup", "dump.txt"))

        _assert_rejected(resp)

    def test_bundle_segment_cannot_climb_out(self, client, artifacts_root, tasks_in_db):
        # attempt_id 完全合法，靶子换到 bundle 段
        resp = client.get(_file_url(TASK_ID, f"{TASK_ID}-1", "%2E%2E", "index.txt"))

        _assert_rejected(resp)

    def test_dot_prefixed_segments_are_rejected(self, client, artifacts_root, tasks_in_db):
        hidden = artifacts_root / TASK_ID / f"{TASK_ID}-1" / ".hidden"
        hidden.mkdir()
        (hidden / "x.txt").write_text("x", encoding="utf-8")

        resp = client.get(_file_url(TASK_ID, f"{TASK_ID}-1", ".hidden", "x.txt"))

        _assert_rejected(resp)


class TestAttemptMustBelongToTask:
    def test_listing_another_tasks_attempt_is_404(self, client, artifacts_root, tasks_in_db):
        resp = client.get(_list_url(TASK_ID, f"{OTHER_TASK_ID}-1"))

        assert resp.status_code == 404

    def test_downloading_another_tasks_file_is_404(self, client, artifacts_root, tasks_in_db):
        resp = client.get(_file_url(TASK_ID, f"{OTHER_TASK_ID}-1", "final", "page.html"))

        assert resp.status_code == 404
        assert "not yours" not in resp.text

    def test_prefix_must_include_the_dash(self, client, artifacts_root, tasks_in_db):
        # "task_artifacts" 是 "task_artifacts_v2-1" 的前缀，但那不是本任务的 attempt
        with Session(engine) as session:
            session.add(TaskModel(id=f"{TASK_ID}_v2", type="register", platform="cursor", status="failed"))
            session.commit()
        bundle = artifacts_root / f"{TASK_ID}_v2" / f"{TASK_ID}_v2-1" / "final"
        bundle.mkdir(parents=True)
        (bundle / "page.html").write_text("v2", encoding="utf-8")

        resp = client.get(_file_url(TASK_ID, f"{TASK_ID}_v2-1", "final", "page.html"))

        assert resp.status_code == 404
