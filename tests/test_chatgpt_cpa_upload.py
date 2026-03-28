from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from platforms.chatgpt.cpa_upload import upload_to_cpa


class _FakeMime:
    def addpart(self, **kwargs) -> None:
        self.kwargs = kwargs

    def close(self) -> None:
        return None


class ChatGPTCpaUploadTests(unittest.TestCase):
    def test_upload_uses_task_proxy_when_config_enabled(self) -> None:
        response = Mock(status_code=201)

        with patch("platforms.chatgpt.cpa_upload.CurlMime", return_value=_FakeMime()), patch(
            "platforms.chatgpt.cpa_upload.cffi_requests.post",
            return_value=response,
        ) as mock_post:
            ok, msg = upload_to_cpa(
                {"email": "demo@example.com"},
                api_url="https://cpa.example.com",
                api_key="token",
                proxy="http://127.0.0.1:7890",
                use_proxy=True,
            )

        self.assertTrue(ok)
        self.assertEqual(msg, "上传成功")
        self.assertEqual(
            mock_post.call_args.kwargs["proxies"],
            {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
        )

    def test_upload_stays_direct_when_proxy_disabled(self) -> None:
        response = Mock(status_code=201)

        with patch("platforms.chatgpt.cpa_upload.CurlMime", return_value=_FakeMime()), patch(
            "platforms.chatgpt.cpa_upload.cffi_requests.post",
            return_value=response,
        ) as mock_post:
            ok, msg = upload_to_cpa(
                {"email": "demo@example.com"},
                api_url="https://cpa.example.com",
                api_key="token",
                proxy="http://127.0.0.1:7890",
                use_proxy=False,
            )

        self.assertTrue(ok)
        self.assertEqual(msg, "上传成功")
        self.assertIsNone(mock_post.call_args.kwargs["proxies"])


if __name__ == "__main__":
    unittest.main()
