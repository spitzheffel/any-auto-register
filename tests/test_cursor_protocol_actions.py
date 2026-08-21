"""Cursor 协议 Step2/Step3 必须看清 Cursor 是否接受提交，不能丢响应空等 OTP。"""
from __future__ import annotations

import pytest

from platforms.cursor.core import (
    AUTH,
    TURNSTILE_SITEKEY,
    CursorRegister,
    find_action_failure,
    response_preview,
)


class _FakeResp:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class _FakeSession:
    def __init__(self, resp: _FakeResp):
        self.resp = resp
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.resp


class _Solver:
    def __init__(self, token: str = "turnstile-token"):
        self.token = token
        self.calls = []

    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        self.calls.append((page_url, site_key))
        return self.token


def _client(resp: _FakeResp, logs: list[str] | None = None) -> CursorRegister:
    client = object.__new__(CursorRegister)
    client.log = (logs if logs is not None else []).append
    client.s = _FakeSession(resp)
    return client


class TestFindActionFailure:
    def test_http_error_is_failure(self):
        resp = _FakeResp(403, text="blocked")
        assert "HTTP 403" in find_action_failure(resp)

    def test_captcha_error_in_body_is_failure(self):
        resp = _FakeResp(200, text='1:{"error":"invalid captcha token"}')
        assert find_action_failure(resp)

    def test_password_page_redirect_is_stuck(self):
        resp = _FakeResp(303, headers={"Location": "https://authenticator.cursor.sh/sign-up/password?state=abc"})
        failure = find_action_failure(resp, stuck_on=("/sign-up/password",))
        assert "sign-up/password" in failure

    def test_magic_code_redirect_is_ok(self):
        resp = _FakeResp(303, headers={"Location": "https://authenticator.cursor.sh/magic-code?state=abc"})
        assert find_action_failure(resp, stuck_on=("/sign-up/password",)) == ""

    def test_plain_200_without_error_is_ok(self):
        resp = _FakeResp(200, text='0:["$@1"]')
        assert find_action_failure(resp, stuck_on=("/sign-up/password",)) == ""

    def test_preview_includes_action_redirect_header(self):
        resp = _FakeResp(200, headers={"x-action-redirect": "/sign-up/password"})
        preview = response_preview(resp)
        assert "HTTP 200" in preview
        assert "/sign-up/password" in preview


class TestStepSubmit:
    def test_step2_raises_on_http_error(self):
        client = _client(_FakeResp(500, text="next failed"))
        with pytest.raises(RuntimeError, match="Step2 提交邮箱 失败"):
            client.step2_submit_email("a@example.com", "state")

    def test_step3_raises_on_empty_turnstile_token(self):
        logs: list[str] = []
        client = _client(_FakeResp(200), logs)
        with pytest.raises(RuntimeError, match="Turnstile token 为空"):
            client.step3_submit_password("Passw0rd!", "a@example.com", "state", captcha_solver=_Solver(""))
        assert client.s.calls == []

    def test_step3_uses_signup_page_url_for_turnstile(self):
        solver = _Solver("tok-123")
        logs: list[str] = []
        client = _client(_FakeResp(200, text='0:["$@1"]'), logs)
        client.step3_submit_password("Passw0rd!", "a@example.com", "encoded-state", captcha_solver=solver)

        assert solver.calls == [(f"{AUTH}/sign-up?state=encoded-state", TURNSTILE_SITEKEY)]
        assert any("Step3 提交密码 响应: HTTP 200" in line for line in logs)
        assert any("Turnstile token 已获取 (7 字符)" in line for line in logs)

    def test_step3_raises_when_cursor_rejects_captcha(self):
        client = _client(_FakeResp(200, text='1:{"error":"invalid captcha"}'))
        with pytest.raises(RuntimeError, match="未接受本次提交"):
            client.step3_submit_password("Passw0rd!", "a@example.com", "state", captcha_solver=_Solver())
