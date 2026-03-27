from __future__ import annotations

import unittest

from core.base_mailbox import _create_duckmail


class DuckMailDefaultsTests(unittest.TestCase):
    def test_create_duckmail_falls_back_to_public_defaults_when_config_is_blank(self) -> None:
        mailbox = _create_duckmail(
            {
                "duckmail_api_url": "",
                "duckmail_provider_url": "",
                "duckmail_bearer": "",
            },
            proxy=None,
        )

        self.assertEqual(mailbox.api, "https://www.duckmail.sbs")
        self.assertEqual(mailbox.provider_url, "https://api.duckmail.sbs")
        self.assertEqual(mailbox.bearer, "kevin273945")

    def test_duckmail_authorization_header_accepts_prefixed_token(self) -> None:
        mailbox = _create_duckmail(
            {
                "duckmail_api_url": "https://www.duckmail.sbs",
                "duckmail_provider_url": "https://api.duckmail.sbs",
                "duckmail_bearer": "Bearer abc123",
            },
            proxy=None,
        )

        self.assertEqual(mailbox._common_headers()["authorization"], "Bearer abc123")


if __name__ == "__main__":
    unittest.main()
