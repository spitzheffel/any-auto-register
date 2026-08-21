"""自建 Mail 脚本接口（/api/v1/keys）邮箱渠道。"""
from __future__ import annotations

import re
import time
import requests

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link, _normalize_api_base_url

SCRIPT_API_SUFFIX = "/api/v1/keys"
DEFAULT_GROUP = "register-pool"
_CODE_RE = re.compile(r"(?<!#)(?<!\d)(\d{6})(?!\d)")


def _script_api_root(base_url: str) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        raise RuntimeError("Mail 脚本接口地址未配置")
    normalized = _normalize_api_base_url(raw, default=raw, label="Mail 脚本接口地址")
    if normalized.lower().endswith(SCRIPT_API_SUFFIX):
        return normalized
    return f"{normalized}{SCRIPT_API_SUFFIX}"


class MailScriptMailbox(BaseMailbox):
    """Bearer API Key 租号、读信，占用由远端按 (邮箱, platform) 管理。"""

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        group: str = "",
        proxy: str = None,
    ):
        self.api_key = str(api_key or "").strip()
        self.group = str(group or "").strip() or DEFAULT_GROUP
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.api_root = _script_api_root(base_url) if str(base_url or "").strip() else ""

    @classmethod
    def from_config(cls, config: dict) -> "MailScriptMailbox":
        return cls(
            base_url=config.get("mail_script_base_url", ""),
            api_key=config.get("mail_script_api_key", ""),
            group=config.get("mail_script_group", ""),
            proxy=config.get("proxy") or None,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> dict:
        if not self.api_key:
            raise RuntimeError("Mail 脚本接口 API Key 未配置")
        if not self.api_root:
            raise RuntimeError("Mail 脚本接口地址未配置")
        url = f"{self.api_root}{path}"
        resp = requests.request(
            method,
            url,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=kwargs.pop("timeout", 15),
            **kwargs,
        )
        body: dict = {}
        if resp.content:
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    body = parsed
            except ValueError:
                body = {}
        code = str(body.get("code") or "")
        message = str(body.get("message") or body.get("error") or "").strip()
        if resp.status_code == 409 or code == "INBOX_POOL_EXHAUSTED":
            raise RuntimeError(message or "Mail 脚本接口该分组没有可租用的邮箱")
        if resp.status_code == 404 or code == "INBOX_LEASE_NOT_FOUND":
            raise RuntimeError(message or "Mail 脚本接口租约不存在或已结束")
        if resp.status_code >= 400:
            raise RuntimeError(message or f"Mail 脚本接口失败: HTTP {resp.status_code}")
        return body

    def _lease_id(self, account: MailboxAccount) -> str:
        extra = dict(getattr(account, "extra", None) or {})
        resource = dict(extra.get("provider_resource") or {})
        metadata = dict(resource.get("metadata") or {})
        lease_id = str(
            getattr(account, "account_id", "")
            or metadata.get("lease_id")
            or extra.get("lease_id")
            or ""
        ).strip()
        if not lease_id:
            raise RuntimeError("Mail 脚本接口缺少 leaseId")
        return lease_id

    def _list_messages(self, account: MailboxAccount) -> list[dict]:
        data = self._request("GET", f"/inboxes/{self._lease_id(account)}/messages")
        return [item for item in (data.get("messages") or []) if isinstance(item, dict)]

    def _get_message(self, account: MailboxAccount, message_id: str) -> dict:
        return self._request("GET", f"/inboxes/{self._lease_id(account)}/messages/{message_id}") or {}

    @staticmethod
    def _message_text(detail: dict) -> str:
        html = detail.get("html")
        if isinstance(html, (list, tuple)):
            html_text = " ".join(str(item or "") for item in html)
        else:
            html_text = str(html or "")
        return " ".join([
            str(detail.get("subject") or ""),
            str(detail.get("from") or ""),
            str(detail.get("text") or ""),
            html_text,
        ])

    def peek_email(self, platform: str = "") -> str:
        data = self._request("GET", "/capabilities")
        scopes = data.get("scopes") or []
        if "inbox.lease" not in scopes:
            raise RuntimeError("Mail 脚本接口不支持租号（缺少 inbox.lease）")
        return ""

    def get_email(self, platform: str = "") -> MailboxAccount:
        plat = str(platform or "").strip().lower()
        payload = {"group": self.group}
        if plat:
            payload["platform"] = plat
        data = self._request("POST", "/inboxes/lease", json=payload)
        email = str(data.get("email") or "").strip()
        lease_id = str(data.get("leaseId") or "").strip()
        if not email or not lease_id:
            raise RuntimeError("Mail 脚本接口租号失败：未返回 email 或 leaseId")
        return MailboxAccount(
            email=email,
            account_id=lease_id,
            extra={
                "lease_id": lease_id,
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "mail_script_api",
                    "resource_type": "mailbox",
                    "resource_identifier": lease_id,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "lease_id": lease_id,
                        "platform": plat,
                        "group": self.group,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {str(msg.get("id") or "") for msg in self._list_messages(account) if msg.get("id")}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
    ) -> str:
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                for msg in self._list_messages(account):
                    mid = str(msg.get("id") or "")
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    detail = self._get_message(account, mid)
                    text = self._message_text(detail)
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    if code_pattern:
                        match = re.search(code_pattern, text)
                        if match:
                            return match.group(1) if match.groups() else match.group(0)
                        continue
                    match = _CODE_RE.search(text)
                    if match:
                        return match.group(1)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
    ) -> str:
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                for msg in self._list_messages(account):
                    mid = str(msg.get("id") or "")
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    detail = self._get_message(account, mid)
                    link = _extract_verification_link(self._message_text(detail), keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")

    def report_success(self, account: MailboxAccount, platform: str = "") -> None:
        self._request("POST", f"/inboxes/{self._lease_id(account)}/success")

    def release(self, account: MailboxAccount, reason: str = "", platform: str = "") -> None:
        kwargs = {}
        note = str(reason or "").strip()
        if note:
            kwargs["json"] = {"reason": note}
        self._request("POST", f"/inboxes/{self._lease_id(account)}/release", **kwargs)
