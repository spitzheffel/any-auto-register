"""Cursor 步骤流与两个 ObstacleHandler 的关键分支。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.cancellation import CancellationToken
from core.errors import ConfigError, TransientError
from core.registration.obstacles import FullPageBlockHandler, TurnstileObstacleHandler
from core.registration.steps import (
    CapabilityResolver,
    InMemoryHumanChannel,
    InteractionResponse,
    Obstacle,
    StepContext,
    StepRunner,
)
from platforms.cursor.steps import (
    AwaitAndFillEmailOtp,
    ExtractSessionToken,
    FillIdentityForm,
    MaybePhoneVerify,
    MaybeSetPassword,
    OpenSignupPage,
    build_flow,
)


class FakeElement:
    def __init__(self, visible=True):
        self._visible = visible
        self.value = ""
        self.clicked = False

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return True

    def fill(self, value):
        self.value = value

    def click(self, timeout=None):
        self.clicked = True


class FakePage:
    """按 selector 返回预置元素的最小 Page。"""

    def __init__(self, *, elements=None, url="https://authenticator.cursor.sh/sign-up"):
        self.url = url
        self.elements = dict(elements or {})
        self.keys = []
        self.goto_urls = []
        self.init_scripts = []
        self.filled = {}

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def goto(self, url, **kwargs):
        self.goto_urls.append(url)

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def wait_for_selector(self, selector, timeout=None):
        for key, element in self.elements.items():
            if key in selector or selector in key:
                return element
        raise RuntimeError(f"no element for {selector}")

    def query_selector(self, selector):
        for key, element in self.elements.items():
            if key in selector or selector in key:
                return element
        return None

    def query_selector_all(self, selector):
        element = self.query_selector(selector)
        return [element] if element else []

    def fill(self, selector, value):
        self.filled[selector] = value

    @property
    def keyboard(self):
        page = self

        class Keyboard:
            def press(self, key):
                page.keys.append(key)

        return Keyboard()


def make_sc(page, *, state=None, builders=None, human=None, cancel=None):
    logs = []
    ctx = SimpleNamespace(
        platform_name="cursor",
        platform_display_name="Cursor",
        platform=SimpleNamespace(),
        log=lambda message, **kwargs: logs.append(message),
    )
    sc = StepContext(
        ctx,
        page=page,
        cancel=cancel,
        resolver=CapabilityResolver(ctx, builders=builders or {}, human=human),
        human=human,
        state=state or {"email": "user@example.com", "password": ""},
        attempt_id="t1",
    )
    sc.logs = logs
    return sc


class ScriptedPage(FakePage):
    """按"阶段"切换可见元素的假页面：提交表单进密码页，提交密码进手机页，提交号码进 OTP 页。"""

    STAGES = {
        "signup": {'input[name="firstName"]': None, 'input[name="lastName"]': None, 'input[name="email"]': None},
        "password": {'input[type="password"]': None},
        "phone": {'input[type="tel"]': None},
        "sms_otp": {"one-time-code": None},
        "email_otp": {'input[name="otp"]': None},
    }

    def __init__(self, *, phone_challenge=True):
        super().__init__(elements={})
        self.phone_challenge = phone_challenge
        self.stage = "signup"
        self.transitions = []
        self._apply_stage()

    def _apply_stage(self):
        self.elements = {selector: FakeElement() for selector in self.STAGES[self.stage]}

    def advance(self):
        order = ["signup", "password", "phone", "sms_otp", "email_otp"]
        nxt = order[order.index(self.stage) + 1] if self.stage != "email_otp" else "email_otp"
        if nxt == "phone" and not self.phone_challenge:
            nxt = "email_otp"
        self.transitions.append(f"{self.stage}->{nxt}")
        self.stage = nxt
        self._apply_stage()
        self.url = f"https://authenticator.cursor.sh/{self.stage}"

    def goto(self, url, **kwargs):
        super().goto(url, **kwargs)
        self.stage = "signup"
        self._apply_stage()

    def content(self):
        return "<html></html>"


def run_cursor_flow(page, *, phone, otp_code="246813", cancel=None):
    """用真实 build_flow() + build_obstacles() 跑引擎，只替换页面、能力与 sleep。"""
    from platforms.cursor.obstacles import build_obstacles

    builders = {
        "phone": lambda ctx, step, key: phone,
        "otp": lambda ctx, step, key: (lambda **kw: otp_code),
        "captcha": lambda ctx, step, key: None,
    }
    sc = make_sc(page, builders=builders, cancel=cancel)
    return StepRunner(build_flow(), obstacles=build_obstacles()).run(sc), sc


@pytest.fixture
def scripted_cursor(monkeypatch):
    """把 Cursor 步骤里的真实等待全部旁路：点 Continue 就切到下一阶段，token 直接给。"""
    monkeypatch.setattr("platforms.cursor.steps.time.sleep", lambda seconds: None)
    monkeypatch.setattr("core.registration.obstacles.time.sleep", lambda seconds: None)
    monkeypatch.setattr("platforms.cursor.steps._wait_cf_full_block_clear", lambda page, **kw: None)
    monkeypatch.setattr("platforms.cursor.steps._wait_for_token", lambda page, timeout=60, **kw: "tok_e2e")
    monkeypatch.setattr("platforms.cursor.switch.get_cursor_user_info", lambda token: {"email": "real@cursor.sh"})

    def click_continue(page):
        if isinstance(page, ScriptedPage):
            page.advance()
        return True

    monkeypatch.setattr("platforms.cursor.steps._click_continue", click_continue)
    monkeypatch.setattr("platforms.cursor.dom._click_continue", click_continue)
    # 短信验证码回车后直接进邮箱 OTP 页
    original_press = FakePage.keyboard

    class _Keyboard:
        def __init__(self, page):
            self.page = page

        def press(self, key):
            self.page.keys.append(key)
            if key == "Enter" and isinstance(self.page, ScriptedPage) and self.page.stage == "sms_otp":
                self.page.advance()

    monkeypatch.setattr(FakePage, "keyboard", property(lambda self: _Keyboard(self)))
    yield
    monkeypatch.setattr(FakePage, "keyboard", original_press)


class TestCursorFlowThroughEngine:
    """真实 build_flow + build_obstacles 走引擎：步骤之间的接线、重试策略、租约生命周期。"""

    def test_happy_path_with_phone_challenge(self, scripted_cursor):
        page = ScriptedPage(phone_challenge=True)
        phone = FakePhone()

        result, sc = run_cursor_flow(page, phone=phone)

        assert result["token"] == "tok_e2e"
        assert result["resolved_email"] == "real@cursor.sh"
        assert result["password"]
        assert phone.lease.events == ["send_ok", "wait_code", "release:success"]
        assert "phone" not in sc.resource_refs and "phone_lease" not in sc.handles
        assert page.transitions == ["signup->password", "password->phone", "phone->sms_otp", "sms_otp->email_otp"]

    def test_no_sms_code_fails_the_attempt_once_without_burning_more_numbers(self, scripted_cursor):
        """等不到码：退掉号、抛 transient 让任务层补投；不能原地重试 → 秒回撤 → 再租两个号。"""
        page = ScriptedPage(phone_challenge=True)
        phone = FakePhone(FakeLease(code=""))

        with pytest.raises(TransientError, match="未收到短信验证码"):
            run_cursor_flow(page, phone=phone)

        assert phone.acquired == 1
        assert phone.lease.released is True
        assert phone.lease.events[-1] == "release:unused"
        # 没有回撤：注册页只打开了一次
        assert len(page.goto_urls) == 1

    def test_rejected_number_is_swapped_in_place(self, scripted_cursor, monkeypatch):
        page = ScriptedPage(phone_challenge=True)
        first = FakeLease(activation_id="act_1")
        phone = FakePhone(first, next_leases=[FakeLease(activation_id="act_2")])

        # 第一次提交号码站点不接受（不进 OTP 页）；第二次正常
        calls = {"n": 0}

        def click_continue(page_arg):
            if isinstance(page_arg, ScriptedPage):
                if page_arg.stage == "phone":
                    calls["n"] += 1
                    if calls["n"] == 1:
                        return True
                page_arg.advance()
            return True

        monkeypatch.setattr("platforms.cursor.steps._click_continue", click_continue)

        result, sc = run_cursor_flow(page, phone=phone)

        assert result["token"] == "tok_e2e"
        assert first.events[:3] == ["send_failed:未进入短信验证码页", "release:rejected", "swap"]
        assert phone.lease.activation_id == "act_2"
        assert phone.lease.events[-1] == "release:success"
        assert "phone" not in sc.resource_refs

    def test_without_phone_challenge_phone_step_is_skipped(self, scripted_cursor):
        page = ScriptedPage(phone_challenge=False)
        phone = FakePhone()

        result, sc = run_cursor_flow(page, phone=phone)

        assert result["token"] == "tok_e2e"
        assert phone.acquired == 0


class TestFlowShape:
    def test_contract_is_valid_and_password_is_a_result_key(self):
        flow = build_flow()
        # 历史 bug：生成了随机密码却没回传，账号入库即失联
        assert "password" in flow.result_keys
        assert flow.anchors() == ["OpenSignupPage"]

    def test_only_navigation_step_is_an_anchor(self):
        # 回撤必须落在能从零重建页面状态的步骤上
        for step in build_flow().steps:
            if step.anchor:
                assert step.name == "OpenSignupPage"


class TestOpenSignupPage:
    def test_injects_patch_and_navigates_with_state(self, monkeypatch):
        monkeypatch.setattr("platforms.cursor.steps._wait_cf_full_block_clear", lambda page, **kw: True)
        page = FakePage()
        sc = make_sc(page)
        sc.current_step = "OpenSignupPage"

        OpenSignupPage().run(sc)

        assert page.init_scripts and "MouseEvent" in page.init_scripts[0]
        url = page.goto_urls[0]
        assert "client_id=" in url and "state=" in url and "redirect_uri=" in url
        assert sc.state["signup_url"] == url


class TestFillIdentityForm:
    def test_fills_names_and_email(self):
        email_input = FakeElement()
        page = FakePage(elements={'input[name="email"]': email_input, 'input[name="firstName"]': FakeElement()})
        sc = make_sc(page)

        FillIdentityForm().run(sc)

        assert page.filled['input[name="email"]'] == "user@example.com"
        assert sc.state["first_name"] and sc.state["last_name"]

    def test_missing_form_is_a_site_change(self):
        from core.errors import SiteChangedError

        sc = make_sc(FakePage(elements={}))
        with pytest.raises(SiteChangedError):
            FillIdentityForm().run(sc)


class TestMaybeSetPassword:
    def test_generates_and_returns_password_when_absent(self, monkeypatch):
        monkeypatch.setattr("platforms.cursor.steps._click_continue", lambda page: True)
        page = FakePage(elements={'input[type="password"]': FakeElement()})
        sc = make_sc(page, state={"email": "a@b.c", "password": ""})

        MaybeSetPassword().run(sc)

        assert sc.state["password"]
        # 密码只进黑板，不进会落盘的资源引用
        assert "password" not in sc.resource_refs.get("account", {})

    def test_keeps_requested_password(self, monkeypatch):
        monkeypatch.setattr("platforms.cursor.steps._click_continue", lambda page: True)
        page = FakePage(elements={'input[type="password"]': FakeElement()})
        sc = make_sc(page, state={"email": "a@b.c", "password": "Requested1!"})

        MaybeSetPassword().run(sc)
        assert sc.state["password"] == "Requested1!"

    def test_guard_skips_when_no_password_page(self):
        assert MaybeSetPassword().when(make_sc(FakePage(elements={}))) is False

    def test_submit_marks_half_finished_account_and_checkpoints(self, monkeypatch):
        """点了 Continue 账号就可能已建好：当场记半成品并落检查点，不等补偿。"""
        monkeypatch.setattr("platforms.cursor.steps._click_continue", lambda page: True)
        page = FakePage(elements={'input[type="password"]': FakeElement()})
        sc = make_sc(page, state={"email": "a@b.c", "password": "x"})
        checkpoints = []
        sc._checkpoint_now = lambda: checkpoints.append(dict(sc.resource_refs))

        MaybeSetPassword().run(sc)

        assert sc.resource_refs["account"] == {"email": "a@b.c", "submitted": True}
        assert checkpoints and checkpoints[0]["account"]["submitted"] is True


class FakeLease:
    def __init__(self, *, code="123456", number="+15551230000", activation_id="act_1", phone=None):
        self.phone_number = number
        self.activation_id = activation_id
        self.code = code
        self.released = False
        self.events = []
        self._phone = phone

    def wait_code(self, **kwargs):
        self.events.append("wait_code")
        return self.code

    def report_send_succeeded(self):
        self.events.append("send_ok")

    def report_send_failed(self, reason=""):
        self.events.append(f"send_failed:{reason}")

    def report_code_failed(self, reason=""):
        self.events.append(f"code_failed:{reason}")

    def release(self, outcome="unused"):
        self.released = True
        self.events.append(f"release:{outcome}")

    def swap(self, *, should_stop=None):
        self.release("rejected")
        self.events.append("swap")
        if self._phone is not None:
            return self._phone.acquire(should_stop=should_stop)
        return FakeLease(activation_id="act_2")

    def to_ref(self):
        return {"provider_key": "herosms_api", "activation_id": self.activation_id}


class FakePhone:
    def __init__(self, lease=None, *, next_leases=()):
        self.lease = lease or FakeLease()
        self.lease._phone = self
        self._next = list(next_leases)
        self.acquired = 0
        self.acquire_kwargs: list[dict] = []

    def acquire(self, **kwargs):
        self.acquired += 1
        self.acquire_kwargs.append(dict(kwargs))
        if self.lease.released:
            # 与真实 PhoneVerification 一致：上一个 lease 已释放就租新号，绝不把已释放的 lease 再交出去
            self.lease = self._next.pop(0) if self._next else FakeLease(activation_id=f"act_{self.acquired}")
            self.lease._phone = self
        return self.lease

    def close(self):
        pass


class TestMaybePhoneVerify:
    def _page(self):
        return FakePage(elements={'input[type="tel"]': FakeElement(), "one-time-code": FakeElement()})

    def test_happy_path_releases_lease_and_clears_ref(self, monkeypatch):
        monkeypatch.setattr("platforms.cursor.steps._click_continue", lambda page: True)
        phone = FakePhone()
        sc = make_sc(self._page(), builders={"phone": lambda ctx, step, key: phone})
        checkpoints = []
        sc._checkpoint_now = lambda: checkpoints.append(dict(sc.resource_refs))

        MaybePhoneVerify().run(sc)

        assert sc.state["phone_number"] == "+15551230000"
        assert phone.lease.events == ["send_ok", "wait_code", "release:success"]
        # 租到号立刻落一次检查点（带 ref），成功释放后再落一次（ref 已清）
        assert checkpoints[0]["phone"]["activation_id"] == "act_1"
        assert "phone" not in checkpoints[-1]
        assert "phone" not in sc.resource_refs
        assert "phone_lease" not in sc.handles

    def test_missing_channel_is_a_config_error(self):
        sc = make_sc(self._page(), builders={"phone": lambda ctx, step, key: None})
        with pytest.raises(ConfigError, match="接码渠道"):
            MaybePhoneVerify().run(sc)

    def test_no_code_releases_number_before_transient_retry(self, monkeypatch):
        """同一个号再等一次也不会来码：先退掉它，重试时才会租新号。"""
        monkeypatch.setattr("platforms.cursor.steps._click_continue", lambda page: True)
        phone = FakePhone(FakeLease(code=""))
        sc = make_sc(self._page(), builders={"phone": lambda ctx, step, key: phone})

        with pytest.raises(TransientError):
            MaybePhoneVerify().run(sc)

        assert "code_failed:未收到短信验证码" in phone.lease.events
        assert phone.lease.released is True
        assert phone.lease.events[-1] == "release:unused"
        assert "phone_lease" not in sc.handles
        assert "phone" not in sc.resource_refs

    def test_site_rejecting_number_swaps_and_raises_resource_rejected(self, monkeypatch):
        from core.errors import ResourceRejectedError

        monkeypatch.setattr("platforms.cursor.steps._click_continue", lambda page: True)
        # 页面上只有手机号输入框：提交后永远等不到验证码输入框
        page = FakePage(elements={'input[type="tel"]': FakeElement()})
        first = FakeLease()
        phone = FakePhone(first, next_leases=[FakeLease(activation_id="act_2")])
        sc = make_sc(page, builders={"phone": lambda ctx, step, key: phone})

        with pytest.raises(ResourceRejectedError):
            MaybePhoneVerify().run(sc)

        assert first.events == ["send_failed:未进入短信验证码页", "release:rejected", "swap"]
        # 换来的新号已经挂在上下文里，引擎按 resource_rejected 重试时直接用它
        assert sc.handles["phone_lease"].activation_id == "act_2"
        assert sc.resource_refs["phone"]["activation_id"] == "act_2"

    def test_retry_on_wrong_page_rewinds_instead_of_crashing(self):
        phone = FakePhone()
        sc = make_sc(FakePage(elements={}), builders={"phone": lambda ctx, step, key: phone})

        result = MaybePhoneVerify().run(sc)

        assert result.outcome == "rewind"
        assert result.rewind_to == "OpenSignupPage"
        assert phone.acquired == 0

    def test_swap_carries_the_cancel_token(self, monkeypatch):
        from core.errors import ResourceRejectedError

        monkeypatch.setattr("platforms.cursor.steps._click_continue", lambda page: True)
        page = FakePage(elements={'input[type="tel"]': FakeElement()})
        phone = FakePhone(FakeLease())
        sc = make_sc(page, builders={"phone": lambda ctx, step, key: phone}, cancel=CancellationToken())

        with pytest.raises(ResourceRejectedError):
            MaybePhoneVerify().run(sc)

        # swap() 重租号要重新排队拿验证窗口锁，取消令牌必须跟着传下去
        assert phone.acquire_kwargs[-1].get("should_stop") is sc.cancel

    def test_only_rejected_number_is_retried_in_place(self):
        """等不到码时页面已在 OTP 页，原地重试没意义：交给任务层补投；号码被拒才在原页面换号重试。"""
        from core.errors import ResourceRejectedError
        from core.registration.steps import retry_policy_for

        step = MaybePhoneVerify()
        assert retry_policy_for(step, TransientError("未收到短信验证码")) is None
        policy = retry_policy_for(step, ResourceRejectedError("被拒"))
        assert policy is not None and policy.max_attempts == 2

    def test_compensate_releases_unused_lease(self):
        phone = FakePhone()
        sc = make_sc(self._page(), builders={"phone": lambda ctx, step, key: phone})
        sc.handles["phone_lease"] = phone.lease
        sc.resource_refs["phone"] = phone.lease.to_ref()

        MaybePhoneVerify().compensate(sc)

        assert phone.lease.events[-1] == "release:unused"
        assert "phone" not in sc.resource_refs

    def test_guard_skips_when_no_phone_page(self):
        assert MaybePhoneVerify().when(make_sc(FakePage(elements={}))) is False


class TestAwaitAndFillEmailOtp:
    def test_types_code_and_submits(self):
        page = FakePage(elements={'input[name="otp"]': FakeElement()}, url="https://x/email-verification")
        sc = make_sc(page, builders={"otp": lambda ctx, step, key: (lambda **kw: "246813")})

        AwaitAndFillEmailOtp().run(sc)

        assert page.keys[:6] == list("246813")
        assert page.keys[-1] == "Enter"

    def test_rewinds_to_anchor_when_page_never_arrives(self, monkeypatch):
        monkeypatch.setattr(AwaitAndFillEmailOtp, "_wait_for_otp_input", lambda self, sc, timeout=60: None)
        sc = make_sc(FakePage(elements={}))

        result = AwaitAndFillEmailOtp().run(sc)

        assert result.outcome == "rewind"
        assert result.rewind_to == "OpenSignupPage"

    def test_missing_mailbox_is_a_config_error(self):
        page = FakePage(elements={'input[name="otp"]': FakeElement()}, url="https://x/email-verification")
        sc = make_sc(page, builders={"otp": lambda ctx, step, key: None})

        with pytest.raises(ConfigError, match="mailbox"):
            AwaitAndFillEmailOtp().run(sc)


class TestExtractSessionToken:
    def test_writes_token_and_resolved_email(self, monkeypatch):
        monkeypatch.setattr("platforms.cursor.steps._wait_for_token", lambda page, timeout=60, **kw: "tok_1")
        monkeypatch.setattr(
            "platforms.cursor.switch.get_cursor_user_info",
            lambda token: {"email": "real@cursor.sh"},
        )
        sc = make_sc(FakePage())

        ExtractSessionToken().run(sc)

        assert sc.state["token"] == "tok_1"
        assert sc.state["resolved_email"] == "real@cursor.sh"

    def test_missing_token_is_transient(self, monkeypatch):
        monkeypatch.setattr("platforms.cursor.steps._wait_for_token", lambda page, timeout=60, **kw: "")
        with pytest.raises(TransientError):
            ExtractSessionToken().run(make_sc(FakePage()))


class TestTurnstileObstacle:
    def _handler(self, **kwargs):
        defaults = dict(
            is_visible=lambda page: page.visible,
            click_in_iframe=None,
            get_sitekey=lambda page: "sitekey-1",
            inject_token=lambda page, token: True,
            settle_seconds=0,
            human_wait_seconds=0,
        )
        defaults.update(kwargs)
        return TurnstileObstacleHandler(**defaults)

    def test_in_page_click_wins_before_paying_a_solver(self):
        page = SimpleNamespace(url="https://x", visible=True)
        solver_calls = []

        def click(page_arg, log):
            page_arg.visible = False
            return True

        handler = self._handler(click_in_iframe=click)
        sc = make_sc(page, builders={"captcha": lambda ctx, step, key: solver_calls.append(1)})

        assert handler.resolve(sc, Obstacle("turnstile")) is True
        assert solver_calls == []

    def test_falls_back_to_external_solver(self):
        page = SimpleNamespace(url="https://x", visible=True)

        class Solver:
            def solve(self, challenge):
                page.visible = False
                return SimpleNamespace(value="token-xyz")

        handler = self._handler()
        sc = make_sc(page, builders={"captcha": lambda ctx, step, key: Solver()})

        assert handler.resolve(sc, Obstacle("turnstile")) is True
        assert sc.state["turnstile_token"] == "token-xyz"

    def test_last_resort_asks_a_human_only_when_headed(self):
        page = SimpleNamespace(url="https://x", visible=True)
        human = InMemoryHumanChannel([InteractionResponse(value="done")])

        class DeadSolver:
            def solve(self, challenge):
                raise RuntimeError("solver down")

        handler = self._handler()
        sc = make_sc(page, builders={"captcha": lambda ctx, step, key: DeadSolver()}, human=human)
        sc.ctx.executor_type = "headed"

        handler.resolve(sc, Obstacle("turnstile"))

        assert human.asked and "Turnstile" in human.asked[0].prompt
        assert human.asked[0].kind == "confirm"

    def test_headless_failure_is_risk_control_not_a_15_minute_wait(self):
        from core.errors import RiskControlError

        page = SimpleNamespace(url="https://x", visible=True)
        human = InMemoryHumanChannel([InteractionResponse(value="done")])

        class DeadSolver:
            def solve(self, challenge):
                raise RuntimeError("solver down")

        handler = self._handler()
        sc = make_sc(page, builders={"captcha": lambda ctx, step, key: DeadSolver()}, human=human)
        sc.ctx.executor_type = "headless"

        with pytest.raises(RiskControlError):
            handler.resolve(sc, Obstacle("turnstile"))
        assert human.asked == []

    def test_navigation_away_counts_as_cleared_even_if_iframe_lingers(self):
        """通过后 widget 的 iframe 常常还在 DOM 里：URL 变了就是过了，别再调一次 solver。"""
        page = SimpleNamespace(url="https://x/signup", visible=True)
        solver_calls = []

        def click(page_arg, log):
            page_arg.url = "https://x/password"
            return True

        handler = self._handler(click_in_iframe=click)
        sc = make_sc(page, builders={"captcha": lambda ctx, step, key: solver_calls.append(1)})

        assert handler.resolve(sc, Obstacle("turnstile", {"page_url": "https://x/signup"})) is True
        assert solver_calls == []

    def test_detect_only_fires_when_visible(self):
        handler = self._handler()
        assert handler.detect(make_sc(SimpleNamespace(url="u", visible=False))) is None
        assert handler.detect(make_sc(SimpleNamespace(url="u", visible=True))) is not None

    def test_detect_is_gated_to_configured_steps_before_but_not_after_failure(self):
        """填表之前不探测（注册页可能预渲染 widget iframe）；失败后的重试前一律探测。"""
        handler = self._handler(detect_before_steps=("MaybeSetPassword",))
        page = SimpleNamespace(url="u", visible=True)

        sc = make_sc(page)
        sc.current_step = "FillIdentityForm"
        sc.obstacle_phase = "before"
        assert handler.detect(sc) is None

        sc.current_step = "MaybeSetPassword"
        assert handler.detect(sc) is not None

        sc.current_step = "FillIdentityForm"
        sc.obstacle_phase = "after_failure"
        assert handler.detect(sc) is not None

    def test_poll_stops_early_when_next_stage_is_ready_and_password_box_does_not_count_after_password_page(self):
        from platforms.cursor.obstacles import _next_stage_ready

        page = FakePage(elements={'input[type="password"]': FakeElement()})
        # 提交注册表单之后：出现密码框 = 进入下一阶段
        assert _next_stage_ready(page, "MaybeSetPassword") is True
        # 密码页之后：密码框还在 DOM 里，不算离开上一页，只有 tel / otp 出现才算
        assert _next_stage_ready(page, "MaybePhoneVerify") is False
        assert _next_stage_ready(FakePage(elements={'input[type="tel"]': FakeElement()}), "MaybePhoneVerify") is True

    def test_poll_waits_for_late_turnstile_before_configured_steps(self):
        page = SimpleNamespace(url="u", visible=False)
        appear_at = {"calls": 0}

        def is_visible(page_arg):
            appear_at["calls"] += 1
            # 第三次探测（约 1 秒后）才渲染出来
            return appear_at["calls"] >= 3

        handler = self._handler(
            is_visible=is_visible,
            poll_seconds=5.0,
            settled=lambda page_arg, step: False,
            poll_before_steps=("MaybePhoneVerify",),
        )
        sc = make_sc(page)
        sc.current_step = "MaybePhoneVerify"
        sc.obstacle_phase = "before"

        assert handler.detect(sc) is not None


class TestFullPageBlockObstacle:
    def test_waits_until_cleared(self):
        calls = []
        blocked = {"value": True}

        def wait_clear(page, **kw):
            calls.append(kw)
            blocked["value"] = False

        handler = FullPageBlockHandler(is_blocked=lambda page: blocked["value"], wait_clear=wait_clear)
        sc = make_sc(FakePage())

        # 等待函数不承诺返回值，放行与否看拦截页是否还在
        assert handler.resolve(sc, Obstacle("full_page_block")) is True
        assert calls[0]["timeout"] == 120.0

    def test_still_blocked_after_wait_is_unresolved(self):
        handler = FullPageBlockHandler(is_blocked=lambda page: True, wait_clear=lambda page, **kw: None)
        assert handler.resolve(make_sc(FakePage()), Obstacle("full_page_block")) is False

    def test_not_blocked_means_no_obstacle(self):
        handler = FullPageBlockHandler(is_blocked=lambda page: False, wait_clear=lambda page, **kw: True)
        assert handler.detect(make_sc(FakePage())) is None
