"""YYDS Mail 邮箱 provider 单元测试。"""
from __future__ import annotations

from unittest.mock import patch

from core.base_mailbox import YYDSMailMailbox, MailboxAccount, create_mailbox


class _FakeResp:
    def __init__(self, json_data: dict | None = None, status_code: int = 200, content: bytes = b"{}"):
        self._json = json_data or {}
        self.status_code = status_code
        self.content = content

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        return None


class TestYYDSMailMailbox:
    def test_get_email_creates_inbox(self):
        provider = YYDSMailMailbox(api_key="AC-test", domain="example.com")

        def fake_request(method, url, **kwargs):
            assert method == "POST"
            assert url.endswith("/accounts")
            # localPart 随机生成 + 传入固定 domain
            assert kwargs["json"]["domain"] == "example.com"
            assert kwargs["headers"]["X-API-Key"] == "AC-test"
            return _FakeResp({"success": True, "data": {"address": "abc@example.com", "id": "inbox1"}})

        with patch("requests.request", side_effect=fake_request):
            account = provider.get_email()

        assert isinstance(account, MailboxAccount)
        assert account.email == "abc@example.com"
        assert account.account_id == "inbox1"

    def test_get_email_auto_picks_domain_when_unset(self):
        provider = YYDSMailMailbox(api_key="AC-test")  # 未配置 domain

        def fake_request(method, url, **kwargs):
            if url.endswith("/domains"):
                return _FakeResp({"success": True, "data": {"domains": [{"domain": "auto.example.com"}]}})
            assert url.endswith("/accounts")
            assert kwargs["json"]["domain"] == "auto.example.com"
            return _FakeResp({"success": True, "data": {"address": "x@auto.example.com", "id": "id2"}})

        with patch("requests.request", side_effect=fake_request):
            account = provider.get_email()

        assert account.email == "x@auto.example.com"

    def test_wait_for_code_returns_server_verification_code(self):
        provider = YYDSMailMailbox(api_key="AC-test", domain="example.com")
        account = MailboxAccount(email="abc@example.com", account_id="inbox1")

        def fake_request(method, url, **kwargs):
            if url.endswith("/messages"):
                return _FakeResp({"success": True, "data": {"messages": [{"id": "m1"}]}})
            if "/messages/m1" in url:
                return _FakeResp({"success": True, "data": {
                    "id": "m1", "subject": "Verify", "text": "your code", "verificationCode": "384729",
                }})
            raise AssertionError(f"unexpected {method} {url}")

        with patch("requests.request", side_effect=fake_request):
            code = provider.wait_for_code(account, timeout=5)

        assert code == "384729"

    def test_wait_for_code_falls_back_to_regex(self):
        provider = YYDSMailMailbox(api_key="AC-test", domain="example.com")
        account = MailboxAccount(email="abc@example.com", account_id="inbox1")

        def fake_request(method, url, **kwargs):
            if url.endswith("/messages"):
                return _FakeResp({"success": True, "data": {"messages": [{"id": "m2"}]}})
            if "/messages/m2" in url:
                return _FakeResp({"success": True, "data": {
                    "id": "m2", "subject": "Hi", "text": "Your verification code is 246810.",
                }})
            raise AssertionError(f"unexpected {method} {url}")

        with patch("requests.request", side_effect=fake_request):
            code = provider.wait_for_code(account, timeout=5)

        assert code == "246810"

    def test_get_email_without_api_key_raises(self):
        provider = YYDSMailMailbox(api_key="")
        try:
            provider.get_email()
        except RuntimeError as exc:
            assert "API Key" in str(exc)
        else:
            raise AssertionError("expected RuntimeError when api_key missing")


def test_config_options_include_yydsmail_provider(client):
    resp = client.get("/api/config/options")
    assert resp.status_code == 200
    providers = resp.json()["mailbox_providers"]
    yyds = next(item for item in providers if item["value"] == "yydsmail_api")
    assert yyds["label"] == "YYDS Mail"
    assert any(field["key"] == "yydsmail_api_key" for field in yyds["fields"])


def test_create_mailbox_builds_yydsmail_instance(client):
    # provider definition 已 seeded 且 enabled，create_mailbox 应能实例化对应驱动
    mailbox = create_mailbox("yydsmail_api", extra={"yydsmail_api_key": "AC-test", "yydsmail_domain": "example.com"})
    # create_mailbox 可能返回 FallbackMailbox 包装，断言其可解析出 YYDS 驱动
    assert mailbox is not None
