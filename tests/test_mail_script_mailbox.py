"""Mail 脚本接口邮箱 provider 单元测试。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.base_mailbox import MailboxAccount, create_mailbox
from core.mail_script_mailbox import MailScriptMailbox, _script_api_root


class _FakeResp:
    def __init__(self, json_data: dict | None = None, status_code: int = 200, content: bytes | None = None):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self.content = content if content is not None else b"{}"

    def json(self) -> dict:
        return self._json


def _mailbox() -> MailScriptMailbox:
    return MailScriptMailbox(
        base_url="http://127.0.0.1:3000",
        api_key="mlk_test",
        group="register-pool",
    )


class TestMailScriptMailbox:
    def test_api_root_avoids_duplicate_suffix(self):
        assert _script_api_root("http://127.0.0.1:3000") == "http://127.0.0.1:3000/api/v1/keys"
        assert _script_api_root("http://127.0.0.1:3000/api/v1/keys/") == "http://127.0.0.1:3000/api/v1/keys"

    def test_get_email_leases_with_group_and_platform(self):
        provider = _mailbox()
        calls: list[tuple] = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            assert method == "POST"
            assert url.endswith("/inboxes/lease")
            assert kwargs["json"] == {"group": "register-pool", "platform": "trae"}
            assert kwargs["headers"]["Authorization"] == "Bearer mlk_test"
            return _FakeResp({"leaseId": "lease-1", "email": "a@outlook.com", "platform": "trae"})

        with patch("core.mail_script_mailbox.requests.request", side_effect=fake_request):
            account = provider.get_email(platform="trae")

        assert account.email == "a@outlook.com"
        assert account.account_id == "lease-1"
        assert len(calls) == 1

    def test_get_email_without_platform_omits_platform_field(self):
        provider = _mailbox()

        def fake_request(method, url, **kwargs):
            assert kwargs["json"] == {"group": "register-pool"}
            return _FakeResp({"leaseId": "lease-g", "email": "a@outlook.com"})

        with patch("core.mail_script_mailbox.requests.request", side_effect=fake_request):
            account = provider.get_email()

        assert account.email == "a@outlook.com"
        assert account.account_id == "lease-g"

    def test_peek_email_hits_capabilities_only(self):
        provider = _mailbox()
        urls: list[str] = []

        def fake_request(method, url, **kwargs):
            urls.append(url)
            assert method == "GET"
            assert url.endswith("/capabilities")
            return _FakeResp({"version": 1, "scopes": ["inbox.lease", "mail.read"]})

        with patch("core.mail_script_mailbox.requests.request", side_effect=fake_request):
            assert provider.peek_email() == ""
        assert urls == ["http://127.0.0.1:3000/api/v1/keys/capabilities"]

    def test_wait_for_code_extracts_digits_from_body(self):
        provider = _mailbox()
        account = MailboxAccount(email="a@outlook.com", account_id="lease-1")

        def fake_request(method, url, **kwargs):
            if url.endswith("/messages"):
                return _FakeResp({"messages": [{"id": "m1"}]})
            if url.endswith("/messages/m1"):
                return _FakeResp({"id": "m1", "text": "your code is 384729", "html": ""})
            raise AssertionError(f"unexpected {method} {url}")

        with patch("core.mail_script_mailbox.requests.request", side_effect=fake_request):
            with patch("core.mail_script_mailbox.time.sleep"):
                code = provider.wait_for_code(account, timeout=5)
        assert code == "384729"

    def test_success_and_release_use_lease_id(self):
        provider = _mailbox()
        account = MailboxAccount(email="a@outlook.com", account_id="lease-1")
        urls: list[tuple[str, str]] = []

        def fake_request(method, url, **kwargs):
            urls.append((method, url))
            if url.endswith("/success"):
                assert kwargs.get("json") is None
                return _FakeResp({"leaseId": "lease-1", "status": "kept"})
            if url.endswith("/release"):
                assert kwargs.get("json") == {"reason": "otp timeout"}
                return _FakeResp({"leaseId": "lease-1", "status": "released"})
            raise AssertionError(url)

        with patch("core.mail_script_mailbox.requests.request", side_effect=fake_request):
            provider.report_success(account, platform="trae")
            provider.release(account, reason="otp timeout", platform="trae")

        assert urls == [
            ("POST", "http://127.0.0.1:3000/api/v1/keys/inboxes/lease-1/success"),
            ("POST", "http://127.0.0.1:3000/api/v1/keys/inboxes/lease-1/release"),
        ]

    def test_pool_exhausted_raises(self):
        provider = _mailbox()

        def fake_request(method, url, **kwargs):
            return _FakeResp(
                {"code": "INBOX_POOL_EXHAUSTED", "message": "该分组没有可租用的邮箱"},
                status_code=409,
            )

        with patch("core.mail_script_mailbox.requests.request", side_effect=fake_request):
            with pytest.raises(RuntimeError, match="没有可租用"):
                provider.get_email(platform="cursor")


def test_config_options_include_mail_script_provider(client):
    resp = client.get("/api/config/options")
    assert resp.status_code == 200
    providers = resp.json()["mailbox_providers"]
    item = next(row for row in providers if row["value"] == "mail_script_api")
    assert item["label"] == "Mail 脚本接口"
    keys = {field["key"] for field in item["fields"]}
    assert {"mail_script_base_url", "mail_script_api_key", "mail_script_group"} <= keys


def test_create_mailbox_builds_mail_script_instance(client):
    mailbox = create_mailbox(
        "mail_script_api",
        extra={
            "mail_script_base_url": "http://127.0.0.1:3000",
            "mail_script_api_key": "mlk_test",
            "mail_script_group": "register-pool",
        },
    )
    inner = mailbox
    if hasattr(mailbox, "providers"):
        inner = mailbox.providers[0][1]
    assert isinstance(inner, MailScriptMailbox)
    assert inner.group == "register-pool"
