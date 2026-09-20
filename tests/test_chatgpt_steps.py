"""ChatGPT 浏览器注册的 Step 流程。

页面顺序由服务端决定（add_phone 可能在 OTP 后也可能在 about_you 后，about_you
本身也可能不出现），所以这里的重点不是"按顺序跑通"，而是各种页面顺序下步骤的
守卫、换号、回撤、补偿是否都对。

页面状态推导（_derive_registration_state_from_page）用真实实现——它是 URL 优先的，
所以脚本页只要给对 URL；被打桩的只有各页面的提交动作。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.cancellation import CancellationToken
from core.errors import (
    AttemptCancelled,
    ConfigError,
    SiteChangedError,
    TransientError,
)
from core.registration.steps import (
    CapabilityResolver,
    RewindBudgetExhausted,
    StepContext,
    StepRunner,
)
from core.requirements import from_step_flow
from platforms.chatgpt import steps as steps_mod
from platforms.chatgpt.errors import PhoneCodeTimeout, PhoneRejectedError
from platforms.chatgpt.obstacles import build_obstacles
from platforms.chatgpt.steps import build_flow

AUTH = "https://auth.openai.com"

PASSWORD_URL = f"{AUTH}/create-account/password"
LOGIN_PASSWORD_URL = f"{AUTH}/log-in/password"
OTP_URL = f"{AUTH}/email-verification"
ABOUT_YOU_URL = f"{AUTH}/about-you"
ADD_PHONE_URL = f"{AUTH}/add-phone"
HOME_URL = "https://chatgpt.com/"

OK_RESP = {"ok": True, "status": 200, "url": "", "data": None, "text": ""}


# ---------------------------------------------------------------------------
# 假页面 / 假浏览器 / 假号码
# ---------------------------------------------------------------------------

class ScriptedPage:
    """按 URL 走的脚本页：每个被打桩的提交动作调一次 advance()。"""

    def __init__(self, stages):
        self.stages = list(stages)
        self._index = 0
        self.goto_urls = []
        self.transitions = [self.url]

    @property
    def url(self):
        return self.stages[min(self._index, len(self.stages) - 1)]

    def advance(self):
        if self._index < len(self.stages) - 1:
            self._index += 1
        self.transitions.append(self.url)

    # -- Playwright 兼容面（够 dom.py 的推导用即可） --
    def goto(self, url, **kwargs):
        self.goto_urls.append(url)

    def query_selector(self, selector):
        return None

    def evaluate(self, script, *args):
        return False

    def content(self):
        return "<html><body>ok</body></html>"

    def screenshot(self, **kwargs):
        return None

    def on(self, *args, **kwargs):
        return None

    def add_init_script(self, script):
        return None


class FakeSession:
    def __init__(self):
        self.closed = False
        self.engine = "fake"
        self.page = ScriptedPage([f"{AUTH}/oauth/authorize"])

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class FakeLaunchSpec:
    def __init__(self):
        self.sessions = []

    def start(self):
        session = FakeSession()
        self.sessions.append(session)
        return session


class FakeLease:
    def __init__(self, phone, index=1):
        self._phone = phone
        self.phone_number = f"+1555000{index:04d}"
        self.activation_id = f"act_{index}"
        self.index = index
        self.released = False
        self.events = []

    def to_ref(self):
        return {"provider_key": "fake_sms", "activation_id": self.activation_id}

    def release(self, outcome="unused"):
        self.released = True
        self.events.append(f"release:{outcome}")

    def swap(self, **kwargs):
        self.release("rejected")
        self.events.append("swap")
        return self._phone.acquire(**kwargs)


class FakePhone:
    def __init__(self):
        self.leases = []
        self.acquire_kwargs = []

    def acquire(self, **kwargs):
        self.acquire_kwargs.append(kwargs)
        lease = FakeLease(self, index=len(self.leases) + 1)
        self.leases.append(lease)
        return lease

    def close(self):
        pass


def make_sc(page, *, builders=None, cancel=None, state=None):
    logs = []

    def log_fn(message, level="info", event_type="log", detail=None):
        logs.append(message)

    ctx = SimpleNamespace(
        platform_name="chatgpt",
        platform_display_name="ChatGPT",
        platform=SimpleNamespace(),
        proxy=None,
        executor_type="headless",
        log=log_fn,
    )
    sc = StepContext(
        ctx,
        page=page,
        cancel=cancel,
        resolver=CapabilityResolver(ctx, builders=builders or {}),
        state=state or {"email": "user@example.com", "password": "Passw0rd!"},
        attempt_id="t1",
    )
    sc.launch_spec = FakeLaunchSpec()
    sc.logs = logs
    return sc


def make_builders(*, otp_code="123456", phone=None):
    builders = {}
    if otp_code is not None:
        builders["otp"] = lambda ctx, step, key: (lambda **kw: otp_code)
    else:
        builders["otp"] = lambda ctx, step, key: None
    builders["phone"] = lambda ctx, step, key: phone
    builders["link"] = lambda ctx, step, key: None
    builders["captcha"] = lambda ctx, step, key: None
    return builders


OAUTH_RESULT = {
    "account_id": "acct_1",
    "access_token": "at_1",
    "refresh_token": "rt_1",
    "id_token": "id_1",
}


@pytest.fixture
def stub_actions(monkeypatch):
    """打桩各页面的提交动作；每个动作把脚本页推进一格。

    桩的签名跟真实实现对齐（都接 ``should_stop``），否则真实代码加了参数这里
    却继续绿，等于测了一个不存在的接口。每次调用收到的 should_stop 记在
    ``calls["cancel_tokens"]`` 里，由 TestCancellation 断言。
    """
    calls = {"codex": 0, "phone": 0, "about_you": 0, "otp": 0, "password": 0, "cancel_tokens": []}

    def _advance(page, should_stop=None):
        calls["cancel_tokens"].append(should_stop)
        page.advance()
        return dict(OK_RESP, url=page.url)

    monkeypatch.setattr(steps_mod, "_seed_browser_device_id", lambda page, device_id: None)
    monkeypatch.setattr(
        steps_mod, "_start_browser_signup_via_page",
        lambda page, email, log, *, should_stop=None: _advance(page, should_stop),
    )
    monkeypatch.setattr(
        steps_mod, "_start_browser_signup_via_authorize",
        lambda page, email, device_id, log, *, should_stop=None: _advance(page, should_stop),
    )

    def submit_password(page, password, log, *, should_stop=None):
        calls["password"] += 1
        return _advance(page, should_stop)

    def submit_otp(page, code, log, *, should_stop=None):
        calls["otp"] += 1
        return _advance(page, should_stop)

    def submit_about_you(page, log, *, should_stop=None):
        calls["about_you"] += 1
        return _advance(page, should_stop)

    def add_phone(page, lease, **kwargs):
        calls["phone"] += 1
        calls["cancel_tokens"].append(kwargs.get("should_stop"))
        page.advance()
        return {}

    def codex(page, **kwargs):
        calls["codex"] += 1
        calls["cancel_tokens"].append(kwargs.get("should_stop"))
        return dict(OAUTH_RESULT)

    monkeypatch.setattr(steps_mod, "_submit_password_via_page", submit_password)
    monkeypatch.setattr(steps_mod, "_submit_otp_via_page", submit_otp)
    monkeypatch.setattr(steps_mod, "_submit_about_you_via_page", submit_about_you)
    monkeypatch.setattr(steps_mod, "run_add_phone_attempt", add_phone)
    monkeypatch.setattr(steps_mod, "run_codex_oauth", codex)
    monkeypatch.setattr(steps_mod, "_handle_post_signup_onboarding", lambda page, log: None)
    # 真实是 10s；这里只要"轮询过一轮"的语义，不要墙钟
    monkeypatch.setattr(steps_mod, "COMPLETE_SETTLE_SECONDS", 0.05)
    return calls


def run_flow(page, *, builders=None, cancel=None):
    sc = make_sc(page, builders=builders, cancel=cancel)
    runner = StepRunner(build_flow(), obstacles=build_obstacles())
    return sc, runner.run(sc)


# ---------------------------------------------------------------------------
# 流程形状
# ---------------------------------------------------------------------------

class TestFlowShape:
    def test_only_the_navigation_step_is_an_anchor(self):
        assert build_flow().anchors() == ["OpenSignupPage"]

    def test_result_keys_match_what_the_plugin_maps(self):
        from platforms.chatgpt.plugin import ChatGPTPlatform

        flow = build_flow()
        assert set(flow.result_keys) == {
            "email", "password", "account_id", "access_token", "refresh_token", "id_token",
        }
        result = {key: "x" for key in flow.result_keys}
        mapped = ChatGPTPlatform._map_chatgpt_result(object.__new__(ChatGPTPlatform), result)
        assert mapped.user_id == "x" and mapped.token == "x"
        # 旧路径里这些也一直是空值，_map_chatgpt_result 自己兜底
        assert mapped.extra["session_token"] == ""
        assert mapped.extra["workspace_id"] == ""

    def test_requirements_keep_phone_conditional(self):
        # phone 若被提成 required，前端会要求每个 ChatGPT 任务都配接码渠道
        levels = {r.capability: r.level for r in from_step_flow(build_flow(), build_obstacles())}
        assert levels["otp"] == "required"
        assert levels["phone"] == "conditional"
        assert "captcha" not in levels

    def test_add_phone_is_mounted_at_both_appearance_points(self):
        names = [s.name for s in build_flow().steps]
        assert names.index("MaybeAddPhone") < names.index("MaybeAboutYou")
        assert names.index("MaybeAddPhoneAfterAboutYou") > names.index("MaybeAboutYou")


# ---------------------------------------------------------------------------
# 穿引擎跑通
# ---------------------------------------------------------------------------

class TestFlowThroughEngine:
    def test_plain_success(self, stub_actions):
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, ABOUT_YOU_URL, HOME_URL])
        sc, result = run_flow(page, builders=make_builders())

        assert result["account_id"] == "acct_1"
        assert result["email"] == "user@example.com"
        assert stub_actions["phone"] == 0
        assert sc.state["page_type"] == "chatgpt_home"

    def test_about_you_is_skipped_when_the_site_does_not_ask(self, stub_actions):
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, HOME_URL])
        _, result = run_flow(page, builders=make_builders())

        assert stub_actions["about_you"] == 0
        assert result["access_token"] == "at_1"

    def test_add_phone_right_after_otp(self, stub_actions):
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, ADD_PHONE_URL, HOME_URL])
        phone = FakePhone()
        sc, result = run_flow(page, builders=make_builders(phone=phone))

        assert stub_actions["phone"] == 1
        assert len(phone.leases) == 1
        assert phone.leases[0].events == ["release:success"]
        # 号码按成功路径结束后，检查点里不能再留着它
        assert "phone" not in sc.resource_refs
        assert "phone_lease" not in sc.handles
        assert result["account_id"] == "acct_1"

    def test_add_phone_after_about_you(self, stub_actions):
        page = ScriptedPage(
            [f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, ABOUT_YOU_URL, ADD_PHONE_URL, HOME_URL]
        )
        phone = FakePhone()
        sc, _ = run_flow(page, builders=make_builders(phone=phone))

        assert stub_actions["about_you"] == 1
        assert stub_actions["phone"] == 1
        assert len(phone.leases) == 1

    def test_password_step_recovers_from_the_login_page(self, stub_actions, monkeypatch):
        recovered = {"n": 0}

        def recover(page, log):
            recovered["n"] += 1
            page.advance()
            return True

        monkeypatch.setattr(steps_mod, "_recover_signup_password_page", recover)
        page = ScriptedPage([f"{AUTH}/log-in", LOGIN_PASSWORD_URL, PASSWORD_URL, OTP_URL, HOME_URL])
        _, result = run_flow(page, builders=make_builders())

        assert recovered["n"] == 1
        assert stub_actions["password"] == 1
        assert result["account_id"] == "acct_1"

    def test_submitting_the_password_checkpoints_a_half_built_account(self, stub_actions):
        # 进程在提交密码之后被杀，重启要能把这个邮箱捞出来交人工确认
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, HOME_URL])
        sc, _ = run_flow(page, builders=make_builders())

        assert sc.resource_refs["account"] == {"email": "user@example.com", "submitted": True}


class TestFailurePaths:
    def test_missing_otp_channel_is_a_config_error(self, stub_actions):
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, HOME_URL])
        with pytest.raises(ConfigError, match="mailbox provider"):
            run_flow(page, builders=make_builders(otp_code=None))

    def test_empty_otp_is_transient_and_retried_in_place(self, stub_actions):
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, HOME_URL])
        phone = FakePhone()
        with pytest.raises(TransientError, match="未获取到邮箱验证码"):
            run_flow(page, builders=make_builders(otp_code="", phone=phone))

        assert stub_actions["otp"] == 0
        # 验证码都没拿到，不该已经租了号
        assert phone.leases == []

    def test_missing_sms_channel_is_a_config_error(self, stub_actions):
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, ADD_PHONE_URL, HOME_URL])
        with pytest.raises(ConfigError, match="接码渠道"):
            run_flow(page, builders=make_builders(phone=None))

    def test_unknown_page_is_a_site_change(self, stub_actions):
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, f"{AUTH}/brand-new-page"])
        with pytest.raises(SiteChangedError, match="无法识别的页面"):
            run_flow(page, builders=make_builders())

    def test_stuck_on_a_known_page_rewinds_then_gives_up(self, stub_actions):
        # OTP 提交后页面没往前走：认得出的页面 = 瞬态，回撤重来，预算用尽转 transient
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, OTP_URL])
        with pytest.raises(RewindBudgetExhausted):
            run_flow(page, builders=make_builders())


class TestPhoneSwap:
    def _run(self, stub_actions, monkeypatch, error):
        attempts = {"n": 0}

        def add_phone(page, lease, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise error
            page.advance()
            return {}

        monkeypatch.setattr(steps_mod, "run_add_phone_attempt", add_phone)
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, ADD_PHONE_URL, HOME_URL])
        phone = FakePhone()
        sc, result = run_flow(page, builders=make_builders(phone=phone))
        return sc, result, phone, attempts

    def test_rejected_number_is_swapped_and_retried(self, stub_actions, monkeypatch):
        sc, result, phone, attempts = self._run(
            stub_actions, monkeypatch, PhoneRejectedError("手机号被拒绝")
        )
        assert attempts["n"] == 2
        assert len(phone.leases) == 2
        assert phone.leases[0].events == ["release:rejected", "swap"]
        assert phone.leases[1].events == ["release:success"]
        assert result["account_id"] == "acct_1"

    def test_code_timeout_also_swaps(self, stub_actions, monkeypatch):
        # 与 cursor 不同：ChatGPT 的 /add-phone 可以原地重进，换号往往当场就成
        _, result, phone, attempts = self._run(
            stub_actions, monkeypatch, PhoneCodeTimeout("未收到短信验证码")
        )
        assert attempts["n"] == 2
        assert len(phone.leases) == 2
        assert result["account_id"] == "acct_1"

    def test_lease_is_released_when_the_step_finally_fails(self, stub_actions, monkeypatch):
        def always_reject(page, lease, **kwargs):
            raise PhoneRejectedError("手机号被拒绝")

        monkeypatch.setattr(steps_mod, "run_add_phone_attempt", always_reject)
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, ADD_PHONE_URL, HOME_URL])
        phone = FakePhone()
        with pytest.raises(PhoneRejectedError):
            run_flow(page, builders=make_builders(phone=phone))

        # 补偿必须把最后那个号也退掉，否则每次失败漏一个号
        assert all(lease.released for lease in phone.leases)


class TestCancellation:
    def test_cancel_while_waiting_for_the_otp(self, stub_actions):
        token = CancellationToken()

        def otp_builder(ctx, step, key):
            def _cb(**kwargs):
                token.trip("用户取消")
                should_stop = kwargs.get("should_stop")
                if should_stop is not None:
                    should_stop.raise_if_cancelled()
                return "123456"

            return _cb

        builders = make_builders()
        builders["otp"] = otp_builder
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, HOME_URL])
        sc = make_sc(page, builders=builders, cancel=token)

        with pytest.raises(AttemptCancelled):
            StepRunner(build_flow(), obstacles=build_obstacles()).run(sc)

        # 取消发生在 OTP，辅助浏览器不该被开过
        assert sc.launch_spec.sessions == []
        assert stub_actions["codex"] == 0

    def test_every_page_action_receives_the_cancel_token(self, stub_actions):
        """页面动作里都是 20s 级的等跳转循环。

        引擎只在步骤**之间**查取消令牌，令牌传不进去的话，一次取消要等
        当前循环跑完才生效。
        """
        page = ScriptedPage(
            [f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, ABOUT_YOU_URL, ADD_PHONE_URL, HOME_URL]
        )
        sc, _ = run_flow(page, builders=make_builders(phone=FakePhone()))

        tokens = stub_actions["cancel_tokens"]
        assert tokens, "一个页面动作都没被调到"
        assert all(token is sc.cancel for token in tokens), tokens

    def test_otp_callback_receives_the_cancel_token(self, stub_actions):
        seen = {}

        def otp_builder(ctx, step, key):
            def _cb(**kwargs):
                seen.update(kwargs)
                return "123456"

            return _cb

        builders = make_builders()
        builders["otp"] = otp_builder
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, HOME_URL])
        sc, _ = run_flow(page, builders=builders)
        assert seen["should_stop"] is sc.cancel


class TestCodexOAuthStep:
    def test_success_closes_the_aux_browser(self, stub_actions):
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, HOME_URL])
        sc, _ = run_flow(page, builders=make_builders())

        assert len(sc.launch_spec.sessions) == 1
        assert sc.launch_spec.sessions[0].closed is True
        assert sc.aux_pages == {}

    def test_failure_closes_the_aux_browser_through_compensation(self, stub_actions, monkeypatch):
        def boom(page, **kwargs):
            raise TransientError("Codex OAuth 未跟到 callback")

        monkeypatch.setattr(steps_mod, "run_codex_oauth", boom)
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, HOME_URL])
        with pytest.raises(TransientError):
            run_flow(page, builders=make_builders())

    def test_oauth_runs_in_a_separate_page_from_the_signup_one(self, stub_actions, monkeypatch):
        seen = {}

        def codex(page, **kwargs):
            seen["page"] = page
            return dict(OAUTH_RESULT)

        monkeypatch.setattr(steps_mod, "run_codex_oauth", codex)
        page = ScriptedPage([f"{AUTH}/log-in", PASSWORD_URL, OTP_URL, HOME_URL])
        sc, _ = run_flow(page, builders=make_builders())

        # 注册完的上下文做 OAuth 会 NS_BINDING_ABORTED，必须是另一个会话的页面
        assert seen["page"] is not page
        assert seen["page"] is sc.launch_spec.sessions[0].page
