from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from platforms.chatgpt.http_client import OpenAIHTTPClient


class ChatGPTIPLocationTests(unittest.TestCase):
    def test_check_ip_location_blocks_known_unsupported_regions(self) -> None:
        client = OpenAIHTTPClient()
        response = Mock()
        response.text = "fl=29f1\nloc=CN\n"

        with patch.object(client, "get", return_value=response):
            self.assertEqual(client.check_ip_location(), (False, "CN"))

    def test_check_ip_location_allows_unknown_region_when_trace_request_fails(self) -> None:
        client = OpenAIHTTPClient()

        with patch.object(client, "get", side_effect=RuntimeError("boom")):
            self.assertEqual(client.check_ip_location(), (True, None))


if __name__ == "__main__":
    unittest.main()
