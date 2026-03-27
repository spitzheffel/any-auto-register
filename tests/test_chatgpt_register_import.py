from __future__ import annotations

import importlib
import sys
import unittest


class ChatGPTRegisterImportTests(unittest.TestCase):
    def test_module_imports_without_name_error(self) -> None:
        sys.modules.pop("platforms.chatgpt.register", None)
        module = importlib.import_module("platforms.chatgpt.register")
        self.assertTrue(hasattr(module, "RegistrationEngine"))


if __name__ == "__main__":
    unittest.main()
