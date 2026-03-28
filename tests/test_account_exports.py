from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from application.account_exports import AccountExportsService
from domain.accounts import AccountExportSelection, AccountRecord


def _make_token(payload: dict) -> str:
    import base64

    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


class _Repo:
    def __init__(self, items: list[AccountRecord]):
        self._items = items

    def select_for_export(self, selection: AccountExportSelection) -> list[AccountRecord]:
        return self._items


class AccountExportsTests(unittest.TestCase):
    def test_export_chatgpt_sub2api_account_matches_expected_shape(self) -> None:
        token = _make_token(
            {
                "exp": 1775462129,
                "iat": 1774598129,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "332f3c88-5d99-4f67-bc85-ad0e2f8b413f",
                    "chatgpt_user_id": "user-q5TdgogHc0fMvABbWS9qSuKV",
                },
            }
        )
        item = AccountRecord(
            id=1,
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            user_id="",
            credentials=[
                {"scope": "platform", "key": "access_token", "value": token},
                {"scope": "platform", "key": "refresh_token", "value": "refresh-demo"},
                {"scope": "platform", "key": "client_id", "value": "app_demo"},
            ],
            created_at=datetime(2026, 3, 27, 8, 4, 37, tzinfo=timezone.utc),
            updated_at=datetime(2026, 3, 27, 9, 4, 37, tzinfo=timezone.utc),
        )

        artifact = AccountExportsService(repository=_Repo([item])).export_chatgpt_sub2api_account(
            AccountExportSelection(platform="chatgpt", ids=[1])
        )

        payload = json.loads(artifact.content)
        self.assertEqual(artifact.media_type, "application/json")
        self.assertIn("exported_at", payload)
        self.assertTrue(str(payload["exported_at"]).endswith("Z"))
        self.assertEqual(payload["proxies"], [])
        self.assertEqual(len(payload["accounts"]), 1)

        account = payload["accounts"][0]
        self.assertEqual(account["name"], "demo@example.com")
        self.assertEqual(account["platform"], "openai")
        self.assertEqual(account["type"], "oauth")
        self.assertEqual(account["credentials"]["chatgpt_account_id"], "332f3c88-5d99-4f67-bc85-ad0e2f8b413f")
        self.assertEqual(account["credentials"]["chatgpt_user_id"], "user-q5TdgogHc0fMvABbWS9qSuKV")
        self.assertEqual(account["credentials"]["client_id"], "app_demo")
        self.assertEqual(account["credentials"]["expires_at"], 1775462129)
        self.assertEqual(account["credentials"]["expires_in"], 864000)
        self.assertEqual(account["credentials"]["organization_id"], "")
        self.assertEqual(
            account["credentials"]["model_mapping"],
            {
                "gpt-5.4": "gpt-5.4",
                "gpt-5.3-codex": "gpt-5.3-codex",
            },
        )
        self.assertEqual(account["extra"], {"email": "demo@example.com"})
        self.assertEqual(account["concurrency"], 1)
        self.assertEqual(account["priority"], 0)
        self.assertEqual(account["rate_multiplier"], 1)
        self.assertTrue(account["auto_pause_on_expired"])


if __name__ == "__main__":
    unittest.main()
