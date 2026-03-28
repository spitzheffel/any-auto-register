from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from core.base_captcha import LocalSolverCaptcha


class LocalSolverCaptchaTests(unittest.TestCase):
    @patch("time.sleep", return_value=None)
    @patch("requests.get")
    def test_solve_turnstile_forwards_proxy_to_solver_api(self, mock_get: Mock, _sleep: Mock) -> None:
        create_response = Mock()
        create_response.raise_for_status.return_value = None
        create_response.json.return_value = {"taskId": "task-1"}

        result_response = Mock()
        result_response.status_code = 200
        result_response.json.return_value = {
            "status": "ready",
            "solution": {"token": "token-123"},
        }

        mock_get.side_effect = [create_response, result_response]

        solver = LocalSolverCaptcha("http://localhost:8889")

        token = solver.solve_turnstile(
            "https://accounts.x.ai/sign-up",
            "sitekey-123",
            proxy="http://127.0.0.1:7890",
            action="managed",
            cdata="opaque-data",
        )

        self.assertEqual(token, "token-123")
        self.assertEqual(mock_get.call_args_list[0].kwargs["params"]["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(mock_get.call_args_list[0].kwargs["params"]["action"], "managed")
        self.assertEqual(mock_get.call_args_list[0].kwargs["params"]["cdata"], "opaque-data")


if __name__ == "__main__":
    unittest.main()
