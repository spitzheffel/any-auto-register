from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from infrastructure.tasks_read_repository import TasksReadRepository


class TasksReadRepositoryDatetimeTests(unittest.TestCase):
    def test_get_coerces_serialized_task_datetimes_back_to_datetime(self) -> None:
        payload = {
            "id": "task_123",
            "type": "register",
            "platform": "trae",
            "status": "running",
            "progress": "1/3",
            "progress_detail": {"current": 1, "total": 3, "label": "1/3"},
            "success": 0,
            "error_count": 0,
            "errors": [],
            "cashier_urls": [],
            "error": "",
            "created_at": "2026-03-27T05:37:00Z",
            "started_at": "2026-03-27T05:38:00+00:00",
            "finished_at": None,
            "updated_at": "2026-03-27T05:39:00.123456Z",
            "result": {},
        }

        with patch("infrastructure.tasks_read_repository.get_task", return_value=payload):
            item = TasksReadRepository().get("task_123")

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.created_at, datetime(2026, 3, 27, 5, 37, 0, tzinfo=timezone.utc))
        self.assertEqual(item.started_at, datetime(2026, 3, 27, 5, 38, 0, tzinfo=timezone.utc))
        self.assertEqual(item.updated_at, datetime(2026, 3, 27, 5, 39, 0, 123456, tzinfo=timezone.utc))

    def test_list_events_coerces_serialized_event_datetimes_back_to_datetime(self) -> None:
        payload = [
            {
                "id": 1,
                "task_id": "task_123",
                "type": "log",
                "level": "info",
                "message": "hello",
                "line": "[13:37:00] hello",
                "detail": {},
                "created_at": "2026-03-27T05:40:00Z",
            }
        ]

        with patch("infrastructure.tasks_read_repository.list_task_events", return_value=payload):
            items = TasksReadRepository().list_events("task_123")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].created_at, datetime(2026, 3, 27, 5, 40, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
