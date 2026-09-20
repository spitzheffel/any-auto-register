from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.base_platform import RegisterConfig
from platforms.chatgpt.plugin import (
    ChatGPTPlatform,
    _assert_complete_oauth_callback,
    _generate_chatgpt_registration_password,
)


def test_assert_complete_oauth_callback_accepts_complete_payload():
    _assert_complete_oauth_callback({
        "account_id": "acct_123",
        "access_token": "at_123",
        "refresh_token": "rt_123",
        "id_token": "id_123",
    })


def test_assert_complete_oauth_callback_accepts_nextauth_payload():
    # NextAuth 流程只回 account_id + access_token，没有 refresh_token / id_token
    _assert_complete_oauth_callback({
        "account_id": "acct_123",
        "access_token": "at_123",
        "refresh_token": "",
        "id_token": "",
    })


def test_assert_complete_oauth_callback_rejects_partial_payload():
    with pytest.raises(RuntimeError, match="完整 OAuth callback"):
        _assert_complete_oauth_callback({
            "account_id": "acct_123",
            "access_token": "",
            "refresh_token": "",
            "id_token": "",
        })


def test_generate_chatgpt_registration_password_meets_openai_strength_requirements():
    for _ in range(8):
        password = _generate_chatgpt_registration_password()
        assert len(password) >= 12
        assert any(ch.islower() for ch in password)
        assert any(ch.isupper() for ch in password)
        assert any(ch.isdigit() for ch in password)
        assert any(ch in ",._!@#" for ch in password)


def test_chatgpt_platform_preserves_user_supplied_password():
    platform = object.__new__(ChatGPTPlatform)
    assert platform._prepare_registration_password("Secret123!") == "Secret123!"


def test_protocol_mailbox_mapper_rejects_partial_oauth_result():
    platform = object.__new__(ChatGPTPlatform)
    platform.mailbox = None
    platform.config = RegisterConfig()
    adapter = ChatGPTPlatform.build_protocol_mailbox_adapter(platform)
    ctx = SimpleNamespace(password="Secret123!", proxy=None, log=lambda message: None)
    result = SimpleNamespace(
        email="user@example.com",
        password="Secret123!",
        account_id="acct_123",
        access_token="",
        refresh_token="",
        id_token="",
        session_token="sess_123",
        workspace_id="",
    )

    with pytest.raises(RuntimeError, match="完整 OAuth callback"):
        adapter.result_mapper(ctx, result)


def test_browser_adapter_runs_through_the_step_engine():
    """适配器必须带 step_flow_builder。

    它是 core/registration/flows.py 里新旧两条路径的分叉点：没它就回到旧的
    整段式 worker，取消令牌 / 检查点 / 失败现场 / 人工通道全部拿不到。
    """
    platform = object.__new__(ChatGPTPlatform)
    adapter = platform.build_browser_registration_adapter()

    assert adapter.step_flow_builder is not None
    assert adapter.obstacle_builder is not None
    assert adapter.browser_worker_builder is None
    # Camoufox 的指纹体系是过 OpenAI 风控的前提，不接受静默换成 Chromium
    assert adapter.engine_preference == "camoufox"
    assert adapter.allow_engine_fallback is False
    assert adapter.engine_options == {"geoip": True}
    assert adapter.step_flow_builder().name == "chatgpt.browser_signup"
