from __future__ import annotations

import importlib
import sys
import unittest


class ChatGPTHttpClientImportTests(unittest.TestCase):
    def test_module_imports_without_name_error(self) -> None:
        sys.modules.pop("platforms.chatgpt.http_client", None)
        module = importlib.import_module("platforms.chatgpt.http_client")
        self.assertTrue(hasattr(module, "OpenAIHTTPClient"))


if __name__ == "__main__":
    unittest.main()
