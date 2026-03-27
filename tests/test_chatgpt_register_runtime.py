from __future__ import annotations

import unittest

from platforms.chatgpt.register import RegistrationEngine


class _DummyService:
    service_type = type("ST", (), {"value": "duckmail"})()

    def create_email(self, config=None):
        return {"email": "test@example.com", "service_id": "abc"}


class ChatGPTRegisterRuntimeTests(unittest.TestCase):
    def test_log_does_not_require_removed_db_helpers(self) -> None:
        engine = RegistrationEngine(email_service=_DummyService(), task_uuid="task-1")
        engine._log("hello")
        self.assertTrue(any("hello" in line for line in engine.logs))

    def test_mark_email_as_registered_is_noop_without_legacy_db_helpers(self) -> None:
        engine = RegistrationEngine(email_service=_DummyService())
        engine.email = "test@example.com"
        engine.email_info = {"service_id": "abc"}
        engine._mark_email_as_registered()


if __name__ == "__main__":
    unittest.main()
