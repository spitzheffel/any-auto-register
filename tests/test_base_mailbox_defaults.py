from __future__ import annotations

import unittest

from core.base_mailbox import _create_duckmail, _create_yyds_mail


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


class YYDSMailDefaultsTests(unittest.TestCase):
    def test_create_yyds_mail_uses_public_api_default(self) -> None:
        mailbox = _create_yyds_mail(
            {
                "yyds_mail_api_url": "",
                "yyds_mail_api_key": "AC-demo",
                "yyds_mail_domain": "",
                "yyds_mail_address_prefix": "",
            },
            proxy=None,
        )

        self.assertEqual(mailbox.api, "https://maliapi.215.im/v1")
        self.assertEqual(mailbox._headers()["x-api-key"], "AC-demo")

    def test_create_yyds_mail_appends_v1_suffix_when_missing(self) -> None:
        mailbox = _create_yyds_mail(
            {
                "yyds_mail_api_url": "https://maliapi.215.im",
                "yyds_mail_api_key": "AC-demo",
            },
            proxy=None,
        )

        self.assertEqual(mailbox.api, "https://maliapi.215.im/v1")


if __name__ == "__main__":
    unittest.main()
