"""Cursor 协议 Step2/Step3 必须看清 Cursor 是否接受提交，不能丢响应空等 OTP。

同时覆盖：action id 404 轮换、sign-up 页面自动提取新 id、
radar-challenge 手机验证的请求格式（来自 refs 抓包报告）。
"""
from __future__ import annotations

import pytest

from core.errors import SiteChangedError
from platforms.cursor.core import (
    ACTION_ACCEPTED,
    ACTION_MISMATCH,
    ACTION_NOT_FOUND,
    AUTH,
    TURNSTILE_SITEKEY,
    CursorActionIds,
    CursorRegister,
    _KNOWN_ACTIONS,
    _split_phone_number,
    classify_action_response,
    find_action_failure,
    response_preview,
)


class _FakeResp:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class _FakeSession:
    """POST 按队列依次返回（队列项是异常实例时抛出）；GET 按子串路由（用于 action id 自动提取）。"""

    def __init__(self, resp: _FakeResp = None, post_responses=None, get_routes=None):
        self.resp = resp or _FakeResp(200)
        self.post_responses = list(post_responses or [])
        self.get_routes = dict(get_routes or {})
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        if self.post_responses:
            item = self.post_responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return self.resp

    def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, **kwargs})
        for fragment, resp in self.get_routes.items():
            if fragment in url:
                return resp
        return _FakeResp(404)


class _FakeLease:
    def __init__(self, phone_number: str, code: str):
        self.phone_number = phone_number
        self.code = code
        self.events: list[str] = []
        self.wait_code_kwargs: dict = {}

    def wait_code(self, **kwargs) -> str:
        self.events.append("wait_code")
        self.wait_code_kwargs = dict(kwargs)
        return self.code

    def report_send_succeeded(self) -> None:
        self.events.append("send_succeeded")

    def report_send_failed(self, reason: str = "") -> None:
        self.events.append(f"send_failed:{reason}")

    def report_code_failed(self, reason: str = "") -> None:
        self.events.append(f"code_failed:{reason}")

    def release(self, outcome: str = "unused") -> None:
        self.events.append(f"release:{outcome}")


class _FakePhoneVerification:
    def __init__(self, phone_number: str, code: str):
        self.lease = _FakeLease(phone_number, code)
        self.acquire_kwargs: dict = {}

    def acquire(self, **kwargs) -> _FakeLease:
        self.acquire_kwargs = dict(kwargs)
        return self.lease

    def close(self) -> None:
        pass


class _Solver:
    def __init__(self, token: str = "turnstile-token"):
        self.token = token
        self.calls = []

    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        self.calls.append((page_url, site_key))
        return self.token


def _client(resp: _FakeResp = None, logs: list[str] | None = None, session=None) -> CursorRegister:
    client = object.__new__(CursorRegister)
    client.log = (logs if logs is not None else []).append
    client.s = session if session is not None else _FakeSession(resp)
    client.last_action_redirect = ""
    # 测试不落盘
    client._actions = CursorActionIds(cache_path=None)
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


class TestClassifyActionResponse:
    """轮询要区分三种情形：id 不存在（404）、id 存在但不是本步骤的 action、命中。"""

    def test_404_is_not_found(self):
        assert classify_action_response(_FakeResp(404, text="Server action not found.")) == (ACTION_NOT_FOUND, "")

    def test_http_500_is_mismatch(self):
        verdict, detail = classify_action_response(_FakeResp(500, text="next failed"))
        assert verdict == ACTION_MISMATCH
        assert "HTTP 500" in detail

    def test_error_body_is_mismatch(self):
        verdict, detail = classify_action_response(_FakeResp(200, text='1:{"error":"something went wrong"}'))
        assert verdict == ACTION_MISMATCH
        assert detail

    def test_stuck_redirect_is_mismatch(self):
        resp = _FakeResp(303, headers={"Location": f"{AUTH}/sign-up/password?state=abc"})
        verdict, _ = classify_action_response(resp, stuck_on=("/sign-up/password",))
        assert verdict == ACTION_MISMATCH

    def test_clean_response_is_accepted(self):
        assert classify_action_response(_FakeResp(200, text='0:["$@1"]')) == (ACTION_ACCEPTED, "")
        resp = _FakeResp(303, headers={"Location": f"{AUTH}/magic-code?state=abc"})
        assert classify_action_response(resp, stuck_on=("/sign-up/password",)) == (ACTION_ACCEPTED, "")


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


class TestActionIdRotation:
    def test_step2_rotates_to_next_candidate_on_404(self):
        new_id = "b" * 40
        session = _FakeSession(post_responses=[
            _FakeResp(404, text="Server action not found."),
            _FakeResp(200, text='0:["$@1"]'),
        ])
        logs: list[str] = []
        client = _client(None, logs, session=session)
        client.actions.candidates = {"submit_email": [new_id]}

        client.step2_submit_email("a@example.com", "state")

        posts = [c for c in session.calls if c["method"] == "POST"]
        assert len(posts) == 2
        assert posts[0]["headers"]["next-action"] == _KNOWN_ACTIONS["submit_email"]
        assert posts[1]["headers"]["next-action"] == new_id
        assert client.actions.confirmed["submit_email"] == new_id
        assert any("404" in line for line in logs)

    def test_step4_rotates_and_extracts_code(self):
        new_id = "c" * 40
        session = _FakeSession(post_responses=[
            _FakeResp(404, text="Server action not found."),
            _FakeResp(303, headers={"Location": "https://cursor.com/api/auth/callback?code=AUTH123&state=s"}),
        ])
        client = _client(None, [], session=session)
        client.actions.candidates = {"magic_code": [new_id]}

        code = client.step4_submit_otp("123456", "a@example.com", "state")

        assert code == "AUTH123"
        assert client.actions.confirmed["magic_code"] == new_id

    def test_all_404_triggers_discovery_then_retries(self):
        fresh_id = "d" * 40
        flight = _FakeResp(200, text='x' * 600 + '"\\/_next\\/static\\/chunks\\/app\\/chunk-abc.js"')
        chunk_js = (
            't("' + fresh_id + '")' + 'formData.append("email",v);' + "x" * 3000
        )
        session = _FakeSession(
            post_responses=[
                _FakeResp(404, text="Server action not found."),  # 旧 id
                _FakeResp(200, text='0:["$@1"]'),                  # 提取后的新 id
            ],
            get_routes={
                "/sign-up": flight,
                "chunk-abc.js": _FakeResp(200, text=chunk_js),
            },
        )
        logs: list[str] = []
        client = _client(None, logs, session=session)

        client.step2_submit_email("a@example.com", "state")

        posts = [c for c in session.calls if c["method"] == "POST"]
        assert [p["headers"]["next-action"] for p in posts] == [
            _KNOWN_ACTIONS["submit_email"],
            fresh_id,
        ]
        assert client.actions.confirmed["submit_email"] == fresh_id
        assert any("自动提取" in line for line in logs)

    def test_all_404_without_discovery_raises(self):
        session = _FakeSession(post_responses=[
            _FakeResp(404, text="Server action not found."),
        ])
        # discovery 拿不到页面数据（GET /sign-up 404）
        client = _client(None, [], session=session)
        with pytest.raises(SiteChangedError, match="404"):
            client.step2_submit_email("a@example.com", "state")


class TestActionIdPoisoning:
    """轮询不能把"不 404 的错误 action"当正解缓存，否则 discover() 再也不会触发。"""

    def test_mismatch_candidate_is_rejected_and_rotation_continues(self):
        wrong_id = "a" * 40
        right_id = "b" * 40
        session = _FakeSession(post_responses=[
            _FakeResp(404, text="Server action not found."),           # 硬编码旧 id
            _FakeResp(200, text='1:{"error":"something went wrong"}'),  # 存在但错配的 action
            _FakeResp(200, text='0:["$@1"]'),                           # 命中
        ])
        logs: list[str] = []
        client = _client(None, logs, session=session)
        client.actions.candidates = {"submit_email": [wrong_id, right_id]}

        client.step2_submit_email("a@example.com", "state")

        posts = [c["headers"]["next-action"] for c in session.calls if c["method"] == "POST"]
        assert posts == [_KNOWN_ACTIONS["submit_email"], wrong_id, right_id]
        assert client.actions.confirmed["submit_email"] == right_id
        assert client.actions.rejected["submit_email"] == [wrong_id]
        assert any("标记失效" in line for line in logs)
        # 下次轮询直接跳过已失效的 id
        assert wrong_id not in client.actions.ordered("submit_email")

    def test_business_failure_on_confirmed_id_is_not_a_mismatch(self):
        """已确认的 action 返回"邮箱已存在"这类业务拒绝：id 没问题，不能标失效，
        更不能拿同一份凭据继续喷给别的候选。"""
        confirmed = "d" * 40
        session = _FakeSession(post_responses=[
            _FakeResp(200, text='1:{"error":"user_already_exists"}'),
        ])
        client = _client(None, [], session=session)
        client.actions.confirmed = {"submit_email": confirmed}
        client.actions.candidates = {"*": ["e" * 40]}

        with pytest.raises(RuntimeError) as excinfo:
            client.step2_submit_email("a@example.com", "state")

        assert not isinstance(excinfo.value, SiteChangedError)
        assert "user_already_exists" in str(excinfo.value)
        assert client.actions.confirmed["submit_email"] == confirmed
        assert "submit_email" not in client.actions.rejected
        assert len([c for c in session.calls if c["method"] == "POST"]) == 1

    def test_poisoned_confirmed_id_is_evicted(self):
        poisoned = "c" * 40
        session = _FakeSession(post_responses=[
            _FakeResp(500, text="Internal Server Error"),  # 被污染的 confirmed
            _FakeResp(404, text="Server action not found."),  # 硬编码旧 id
        ])
        client = _client(None, [], session=session)
        client.actions.confirmed = {"submit_email": poisoned}

        with pytest.raises(SiteChangedError):
            client.step2_submit_email("a@example.com", "state")

        assert "submit_email" not in client.actions.confirmed
        assert client.actions.rejected["submit_email"] == [poisoned]

    def test_sensitive_step_never_touches_wildcard_pool(self):
        pool_id = "d" * 40
        actions = CursorActionIds(cache_path=None)
        actions.candidates = {"*": [pool_id]}
        assert pool_id in actions.ordered("submit_email")
        assert pool_id not in actions.ordered("submit_password")

        session = _FakeSession(post_responses=[
            _FakeResp(404, text="Server action not found."),
        ])
        client = _client(None, [], session=session)
        client.actions.candidates = {"*": [pool_id]}
        with pytest.raises(SiteChangedError):
            client.step3_submit_password("Passw0rd!", "a@example.com", "state", captcha_solver=_Solver())

        posts = [c["headers"]["next-action"] for c in session.calls if c["method"] == "POST"]
        assert posts == [_KNOWN_ACTIONS["submit_password"]]

    def test_exhausted_candidates_discover_once_then_raise_site_changed(self):
        pool_id = "e" * 40
        flight = _FakeResp(200, text='x' * 600 + '"/_next/static/chunks/app/chunk-abc.js"')
        # 归类不出步骤的候选只进通配池
        chunk_js = 't("' + pool_id + '")' + "x" * 3000
        session = _FakeSession(
            post_responses=[
                _FakeResp(404, text="Server action not found."),  # 硬编码旧 id
                _FakeResp(500, text="Internal Server Error"),     # 提取到的候选：存在但错配
            ],
            get_routes={
                "/sign-up": flight,
                "chunk-abc.js": _FakeResp(200, text=chunk_js),
            },
        )
        client = _client(None, [], session=session)
        discover_calls: list[int] = []
        real_discover = client.actions.discover

        def counting_discover(sess, log):
            discover_calls.append(1)
            return real_discover(sess, log)

        client.actions.discover = counting_discover

        with pytest.raises(SiteChangedError, match="未接受本次提交"):
            client.step2_submit_email("a@example.com", "state")

        assert discover_calls == [1]
        posts = [c["headers"]["next-action"] for c in session.calls if c["method"] == "POST"]
        assert posts == [_KNOWN_ACTIONS["submit_email"], pool_id]
        assert "submit_email" not in client.actions.confirmed
        assert client.actions.rejected["submit_email"] == [pool_id]

    def test_discover_resets_rejected_ids(self):
        stale = "f" * 40
        actions = CursorActionIds(cache_path=None)
        actions.rejected = {"submit_email": [stale]}
        session = _FakeSession(get_routes={
            "/sign-up": _FakeResp(200, text='x' * 600 + '"/_next/static/chunks/app/chunk-abc.js"'),
            "chunk-abc.js": _FakeResp(200, text='t("' + stale + '")' + "x" * 3000),
        })
        assert actions.discover(session, (lambda msg: None)) is True
        assert actions.rejected == {}


class TestActionIdDiscovery:
    def test_discover_classifies_candidates_by_context(self):
        password_id = "e" * 40
        email_id = "f" * 40
        otp_id = "0" * 40  # 全 0 也应是合法 hex
        chunk_js = (
            't("' + password_id + '")' + 'formData.append("captchaToken",v);' + "x" * 3000
            + 't("' + email_id + '")' + 'formData.append("email",v);' + "x" * 3000
            + 't("' + otp_id + '")' + 'formData.append("otp",v);' + "x" * 3000
        )
        session = _FakeSession(get_routes={
            "/sign-up": _FakeResp(200, text='x' * 600 + '"/_next/static/chunks/app/chunk-abc.js"'),
            "chunk-abc.js": _FakeResp(200, text=chunk_js),
        })
        actions = CursorActionIds(cache_path=None)

        assert actions.discover(session, (lambda msg: None)) is True
        assert actions.candidates["submit_password"] == [password_id]
        assert actions.candidates["submit_email"] == [email_id]
        assert actions.candidates["magic_code"] == [otp_id]
        assert set(actions.candidates["*"]) == {password_id, email_id, otp_id}

    def test_discover_rejects_cloudflare_challenge_page(self):
        session = _FakeSession(get_routes={
            "/sign-up": _FakeResp(200, text="<html><title>Just a moment...</title>" + "x" * 2000),
        })
        actions = CursorActionIds(cache_path=None)
        assert actions.discover(session, (lambda msg: None)) is False


class TestRadarChallenge:
    def test_split_phone_number(self):
        assert _split_phone_number("+441510496123") == ("+44", "1510496123")
        assert _split_phone_number("+12425551234") == ("+1", "2425551234")
        assert _split_phone_number("85212345678") == ("+852", "12345678")
        assert _split_phone_number("") == ("", "")

    def test_radar_challenge_uses_reported_field_format(self):
        session = _FakeSession(post_responses=[
            _FakeResp(200, text='{"verification_id":"vrf_test123"}'),
            _FakeResp(303, headers={"Location": "https://authenticator.cursor.sh/success"}),
        ])
        logs: list[str] = []
        client = _client(None, logs, session=session)
        phone = _FakePhoneVerification("+441510496123", "336992")

        resp = client.run_radar_challenge(
            phone,
            "https://authenticator.cursor.sh/radar-challenge?pending_authentication_token=TOK1",
        )
        assert resp is not None
        assert phone.lease.events == ["send_succeeded", "wait_code", "release:success"]

        send_call, verify_call = [c for c in session.calls if c["method"] == "POST"]
        assert send_call["url"].endswith("/radar-challenge/send")
        send_body = send_call["data"].decode()
        assert 'name="_1_country_code"\r\n\r\n+44' in send_body
        assert 'name="_1_phone_number"\r\n\r\n1510496123' in send_body
        assert 'name="_1_pending_authentication_token"\r\n\r\nTOK1' in send_body

        assert verify_call["url"].endswith("/radar-challenge/verify")
        verify_body = verify_call["data"].decode()
        assert 'name="_1_code"\r\n\r\n336992' in verify_body
        assert 'name="_1_verification_id"\r\n\r\nvrf_test123' in verify_body

    def test_radar_challenge_raises_without_phone_number(self):
        client = _client(_FakeResp(200))
        phone = _FakePhoneVerification("", "")
        with pytest.raises(RuntimeError, match="未返回手机号"):
            client.run_radar_challenge(phone, "https://authenticator.cursor.sh/radar-challenge")
        # 没拿到号也要把 lease 归还，不能留给收尾的 close() 猜
        assert phone.lease.events == ["release:unused"]

    def test_send_rejected_reports_and_releases_rejected(self):
        session = _FakeSession(post_responses=[_FakeResp(400, text="phone not allowed")])
        client = _client(None, [], session=session)
        phone = _FakePhoneVerification("+441510496123", "336992")

        with pytest.raises(RuntimeError, match="发送短信验证码失败"):
            client.run_radar_challenge(phone, "https://authenticator.cursor.sh/radar-challenge")

        assert phone.lease.events == ["send_failed:HTTP 400", "release:rejected"]
        assert len([c for c in session.calls if c["method"] == "POST"]) == 1

    def test_empty_code_reports_code_failed_and_never_reports_success(self):
        session = _FakeSession(post_responses=[_FakeResp(200, text='{"verification_id":"vrf_1"}')])
        client = _client(None, [], session=session)
        phone = _FakePhoneVerification("+441510496123", "")

        with pytest.raises(RuntimeError, match="未收到短信验证码"):
            client.run_radar_challenge(phone, "https://authenticator.cursor.sh/radar-challenge")

        assert phone.lease.events == [
            "send_succeeded",
            "wait_code",
            "code_failed:等待短信验证码超时",
            "release:unused",
        ]
        assert "release:success" not in phone.lease.events
        # 没有码就不该去打 verify
        assert len([c for c in session.calls if c["method"] == "POST"]) == 1

    def test_verify_exception_reports_code_failed_then_reraises(self):
        session = _FakeSession(post_responses=[
            _FakeResp(200, text='{"verification_id":"vrf_1"}'),
            ConnectionError("boom"),
        ])
        client = _client(None, [], session=session)
        phone = _FakePhoneVerification("+441510496123", "336992")

        with pytest.raises(ConnectionError, match="boom"):
            client.run_radar_challenge(phone, "https://authenticator.cursor.sh/radar-challenge")

        assert phone.lease.events == [
            "send_succeeded",
            "wait_code",
            "code_failed:verify 请求失败: boom",
            "release:unused",
        ]

    def test_verify_rejecting_code_reports_code_failed(self):
        session = _FakeSession(post_responses=[
            _FakeResp(200, text='{"verification_id":"vrf_1"}'),
            _FakeResp(200, text='1:{"message":"Invalid verification code"}'),
        ])
        client = _client(None, [], session=session)
        phone = _FakePhoneVerification("+441510496123", "000000")

        with pytest.raises(RuntimeError, match="短信验证码校验失败"):
            client.run_radar_challenge(phone, "https://authenticator.cursor.sh/radar-challenge")

        events = phone.lease.events
        assert events[:2] == ["send_succeeded", "wait_code"]
        assert events[2].startswith("code_failed:")
        assert events[3] == "release:unused"
        assert "release:success" not in events

    def test_should_stop_is_forwarded_to_acquire_and_wait_code(self):
        session = _FakeSession(post_responses=[
            _FakeResp(200, text='{"verification_id":"vrf_1"}'),
            _FakeResp(303, headers={"Location": "https://authenticator.cursor.sh/success"}),
        ])
        client = _client(None, [], session=session)
        phone = _FakePhoneVerification("+441510496123", "336992")
        should_stop = lambda: False  # noqa: E731

        client.run_radar_challenge(
            phone,
            "https://authenticator.cursor.sh/radar-challenge",
            should_stop=should_stop,
        )

        assert phone.acquire_kwargs["should_stop"] is should_stop
        assert phone.lease.wait_code_kwargs["should_stop"] is should_stop
        assert phone.lease.events[-1] == "release:success"

    def test_should_stop_defaults_to_none_for_legacy_callers(self):
        session = _FakeSession(post_responses=[
            _FakeResp(200, text='{"verification_id":"vrf_1"}'),
            _FakeResp(303, headers={"Location": "https://authenticator.cursor.sh/success"}),
        ])
        client = _client(None, [], session=session)
        phone = _FakePhoneVerification("+441510496123", "336992")

        client.run_radar_challenge(phone, "https://authenticator.cursor.sh/radar-challenge")

        assert phone.acquire_kwargs == {"should_stop": None}
        assert phone.lease.wait_code_kwargs == {"should_stop": None}

    def test_is_radar_challenge_redirect(self):
        client = _client(_FakeResp(200))
        assert client.is_radar_challenge_redirect("https://authenticator.cursor.sh/radar-challenge?x=1")
        assert not client.is_radar_challenge_redirect("https://authenticator.cursor.sh/email-verification")
