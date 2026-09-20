"""OAuth callback 换 token：state 校验与跳过校验的回退。

浏览器里跟到的 callback 偶尔会丢掉 state（站点自己重定向过一手），但 URL 确实是
我们的 redirect_uri。早期实现在这种情况下直接 import `oauth` 的私有名自己拼了一遍
token 交换，于是两处的 claims 解析各走各的——改一处漏一处。现在统一走
`submit_callback_url(verify_state=False)`，这里盯住那条回退路径。
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from platforms.chatgpt import codex_oauth, oauth

CALLBACK = "http://localhost:1455/auth/callback"


def _id_token(email: str = "user@example.com", account_id: str = "acct_1") -> str:
    def seg(payload: dict) -> str:
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    claims = {"email": email, "https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


TOKEN_RESPONSE = {
    "access_token": "at_1",
    "refresh_token": "rt_1",
    "id_token": _id_token(),
    "expires_in": 3600,
}


def _oauth_start(state: str = "expected-state"):
    return SimpleNamespace(
        state=state,
        code_verifier="verifier",
        redirect_uri=CALLBACK,
        client_id="client_1",
        auth_url="https://auth.openai.com/oauth/authorize",
    )


@pytest.fixture
def posted(monkeypatch):
    """替掉真正的 token 交换（conftest 会在 socket 层拦外呼）。"""
    calls = []

    def _post_form(url, data, timeout=30, proxy_url=None):
        calls.append({"url": url, "data": dict(data), "proxy_url": proxy_url})
        return dict(TOKEN_RESPONSE)

    monkeypatch.setattr(oauth, "_post_form", _post_form)
    return calls


class TestSubmitCallbackUrl:
    def test_state_mismatch_is_rejected_by_default(self, posted):
        with pytest.raises(ValueError, match="state mismatch"):
            oauth.submit_callback_url(
                callback_url=f"{CALLBACK}?code=c1&state=other",
                expected_state="expected-state",
                code_verifier="verifier",
                redirect_uri=CALLBACK,
                client_id="client_1",
            )
        assert posted == [], "state 没过就不该去换 token"

    def test_missing_state_is_rejected_by_default(self, posted):
        with pytest.raises(ValueError, match="missing \\?state="):
            oauth.submit_callback_url(
                callback_url=f"{CALLBACK}?code=c1",
                expected_state="expected-state",
                code_verifier="verifier",
                redirect_uri=CALLBACK,
                client_id="client_1",
            )

    def test_verify_state_false_accepts_a_missing_or_wrong_state(self, posted):
        for url in (f"{CALLBACK}?code=c1", f"{CALLBACK}?code=c1&state=other"):
            raw = oauth.submit_callback_url(
                callback_url=url,
                expected_state="expected-state",
                code_verifier="verifier",
                redirect_uri=CALLBACK,
                client_id="client_1",
                verify_state=False,
            )
            result = json.loads(raw)
            assert result["access_token"] == "at_1"
            assert result["account_id"] == "acct_1"
            assert result["email"] == "user@example.com"

        assert len(posted) == 2
        assert posted[0]["data"]["code"] == "c1"
        assert posted[0]["data"]["grant_type"] == "authorization_code"

    def test_code_is_still_required_without_state_verification(self, posted):
        # 放宽的只有 state；没有 code 就没得换，照旧报错
        with pytest.raises(ValueError, match="missing \\?code="):
            oauth.submit_callback_url(
                callback_url=f"{CALLBACK}?state=other",
                expected_state="expected-state",
                code_verifier="verifier",
                redirect_uri=CALLBACK,
                client_id="client_1",
                verify_state=False,
            )
        assert posted == []

    def test_oauth_error_in_the_callback_still_raises(self, posted):
        with pytest.raises(RuntimeError, match="oauth error"):
            oauth.submit_callback_url(
                callback_url=f"{CALLBACK}?error=access_denied&error_description=nope",
                expected_state="expected-state",
                code_verifier="verifier",
                redirect_uri=CALLBACK,
                client_id="client_1",
                verify_state=False,
            )


class FakePage:
    def __init__(self, url):
        self.url = url

    def evaluate(self, *args, **kwargs):
        return None

    def reload(self, **kwargs):
        return None

    def query_selector(self, selector):
        return None

    def query_selector_all(self, selector):
        return []

    def wait_for_timeout(self, ms):
        return None


class TestBrowserCallbackFallback:
    def _run(self, monkeypatch, url, *, calls):
        def fake_submit(**kwargs):
            calls.append(kwargs)
            if kwargs.get("verify_state", True):
                raise ValueError("state mismatch")
            return json.dumps({"access_token": "at_1", "account_id": "acct_1"})

        monkeypatch.setattr(oauth, "submit_callback_url", fake_submit)
        monkeypatch.setattr(codex_oauth.time, "sleep", lambda seconds: None)
        return codex_oauth._complete_oauth_in_browser(
            FakePage(url), _oauth_start(), None, lambda message: None
        )

    def test_state_mismatch_retries_without_state_verification(self, monkeypatch):
        calls = []
        result = self._run(monkeypatch, f"{CALLBACK}?code=c1&state=stale", calls=calls)

        assert result == {"access_token": "at_1", "account_id": "acct_1"}
        # 先按严格模式试一次，失败后才放宽——不能一上来就不校验
        assert [c.get("verify_state", True) for c in calls] == [True, False]
        assert all(c["callback_url"].startswith(CALLBACK) for c in calls)

    def test_the_relaxed_retry_is_scoped_to_our_own_redirect_uri(self, monkeypatch):
        """非 localhost 的 callback 不得走放宽路径。

        这里不驱动整个 ``_complete_oauth_in_browser``：它在不成功时会跑满
        4 轮重试预算，没有快速结束的路径。直接盯源码里的那一句守卫，
        它改了这条测试就该红。
        """
        import inspect

        source = inspect.getsource(codex_oauth._complete_oauth_in_browser)
        assert 'if "state" in str(ve) and "localhost" in url:' in source
        # 守卫里的 localhost 是写死的，没有跟着 oauth_start.redirect_uri 走；
        # redirect_uri 一旦改成别的主机，放宽路径会静默失效而不是报错
        assert CALLBACK.startswith("http://localhost")
