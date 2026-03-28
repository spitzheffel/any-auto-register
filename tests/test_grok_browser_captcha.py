from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from core.registration import RegistrationArtifacts
from platforms.grok.browser_register import GrokBrowserRegister, _turnstile_visible
from platforms.grok.core import TURNSTILE_SITEKEY
from platforms.grok.plugin import GrokPlatform


class GrokBrowserCaptchaTests(unittest.TestCase):
    def test_browser_adapter_enables_mailbox_captcha_and_passes_solver(self) -> None:
        platform = GrokPlatform()
        adapter = platform.build_browser_registration_adapter()
        solver = object()
        ctx = SimpleNamespace(
            executor_type="headed",
            proxy="http://127.0.0.1:7890",
            identity=SimpleNamespace(identity_provider="mailbox"),
            log=lambda _message: None,
        )
        artifacts = RegistrationArtifacts(captcha_solver=solver)

        worker = adapter.browser_worker_builder(ctx, artifacts)

        self.assertTrue(adapter.use_captcha_for_mailbox)
        self.assertIs(worker.captcha, solver)
        self.assertEqual(worker.proxy, "http://127.0.0.1:7890")

    def test_solve_turnstile_uses_injected_solver(self) -> None:
        solver = Mock()
        solver.solve_turnstile.return_value = "token-123"
        worker = GrokBrowserRegister(
            captcha=solver,
            headless=True,
            proxy="http://127.0.0.1:7890",
            log_fn=lambda _message: None,
        )

        token = worker._solve_turnstile(
            "https://accounts.x.ai/sign-up",
            "",
            action="managed",
            cdata="opaque-data",
        )

        self.assertEqual(token, "token-123")
        solver.solve_turnstile.assert_called_once_with(
            "https://accounts.x.ai/sign-up",
            TURNSTILE_SITEKEY,
            proxy="http://127.0.0.1:7890",
            action="managed",
            cdata="opaque-data",
        )

    def test_hidden_turnstile_response_field_counts_as_visible_challenge(self) -> None:
        visible = _turnstile_visible(
            None,
            {
                "hasWidget": False,
                "hasIframe": False,
                "hasResponseField": True,
                "responseLength": 0,
                "bodyText": "",
            },
        )

        self.assertTrue(visible)


if __name__ == "__main__":
    unittest.main()
