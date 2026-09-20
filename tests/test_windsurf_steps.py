"""Windsurf 步骤流：表单三连 + 只在网络响应里出现的注册结果。

这里不碰网络也不起浏览器——用一个假页面驱动真实的 ``build_flow()``，
``WindsurfPostAuth`` 的 protobuf 用 ``core._field_string`` 真造一份，
所以捕获与解析这条链路是真的在跑。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.cancellation import CancellationToken
from core.errors import AttemptCancelled, ConfigError, SiteChangedError, TransientError
from core.registration.steps import CapabilityResolver, StepContext, StepRunner
from platforms.windsurf import dom, steps
from platforms.windsurf.core import _field_string

SESSION_TOKEN = "devin-session-token$session"
AUTH_TOKEN = "auth1_result"
ACCOUNT_ID = "account-123"
ORG_ID = "org-456"

OVERVIEW = {"plan_name": "Free", "remaining_credits": 25, "remote_user": {"user_id": "u_from_overview"}}
SUMMARY = {"account_overview": OVERVIEW, "plan": "free"}


def post_auth_payload(session_token=SESSION_TOKEN) -> bytes:
    return b"".join(
        [
            _field_string(1, session_token),
            _field_string(3, AUTH_TOKEN),
            _field_string(4, ACCOUNT_ID),
            _field_string(5, ORG_ID),
        ]
    )


class FakeResponse:
    def __init__(self, url, *, status=200, json_body=None, body=b""):
        self.url = url
        self.status = status
        self._json = json_body
        self._body = body

    def json(self):
        return self._json

    def body(self):
        return self._body


class FakeLocator:
    def __init__(self, page, selector, *, count=1):
        self.page = page
        self.selector = selector
        self._count = count

    def fill(self, value):
        self.page.filled.setdefault(self.selector, []).append(value)

    def click(self):
        self.page.clicked.append(self.selector)
        self.page.advance()

    def check(self):
        self.page.checked.append(self.selector)

    def is_checked(self):
        return self.selector in self.page.checked

    def is_visible(self):
        return True

    def all(self):
        return [FakeLocator(self.page, f"{self.selector}#{i}") for i in range(self._count)]

    def inner_text(self, timeout=None):
        return self.page.body_text


class ScriptedPage:
    """按"提交一次前进一格"的脚本推进的假页面。

    阶段决定哪些 ``wait_for_selector`` 能过；点提交按钮就往前走一格。
    """

    STAGES = ("login", "register", "password", "otp", "done")

    #: 每一阶段"下一步会等的那个选择器"是否已经就绪
    READY = {
        "register": dom.GIVEN_NAME_SELECTOR,
        "password": dom.PASSWORD_SELECTOR,
        "otp": dom.OTP_INPUT_SELECTOR,
    }

    def __init__(self, *, otp_boxes=6, missing_stage=None, body_text="Something went wrong"):
        self.index = 0
        self.otp_boxes = otp_boxes
        self.missing_stage = missing_stage
        self.body_text = body_text
        self.filled = {}
        self.clicked = []
        self.checked = []
        self.goto_urls = []
        self.init_scripts = []
        self.listeners = []
        self.timeouts = []
        self.roles_clicked = []

    @property
    def stage(self):
        return self.STAGES[self.index]

    def advance(self):
        if self.index < len(self.STAGES) - 1:
            self.index += 1

    # -- Page 接口 ------------------------------------------------------
    def set_default_timeout(self, ms):
        self.timeouts.append(ms)

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def goto(self, url, **kwargs):
        self.goto_urls.append(url)
        self.index = 0

    def get_by_role(self, role, name=None):
        page = self
        label = getattr(name, "pattern", str(name))

        class RoleLocator:
            def click(self_inner):
                page.roles_clicked.append((role, label))
                page.advance()

        return RoleLocator()

    def wait_for_url(self, pattern, timeout=None):
        if self.stage == "login":
            raise RuntimeError(f"still on {self.stage}")
        return None

    def wait_for_selector(self, selector, state=None, timeout=None):
        if self.stage == self.missing_stage:
            raise RuntimeError(f"no {selector} on {self.stage}")
        if self.READY.get(self.stage) == selector:
            return FakeLocator(self, selector)
        raise RuntimeError(f"{selector} not ready on {self.stage}")

    def locator(self, selector):
        if selector == dom.OTP_ANY_INPUT_SELECTOR:
            return FakeLocator(self, selector, count=self.otp_boxes)
        return FakeLocator(self, selector)

    def on(self, event, handler):
        self.listeners.append((event, handler))

    def emit(self, response):
        for event, handler in self.listeners:
            if event == "response":
                handler(response)


class FakeApi:
    def __init__(self, *, post_auth=None, state=None, post_auth_raises=None, state_raises=None):
        self.post_auth_result = post_auth
        self.state_result = state if state is not None else {"summary": dict(SUMMARY)}
        self.post_auth_raises = post_auth_raises
        self.state_raises = state_raises
        self.post_auth_calls = []
        self.state_calls = []

    def post_auth(self, auth_token):
        self.post_auth_calls.append(auth_token)
        if self.post_auth_raises is not None:
            raise self.post_auth_raises
        return dict(self.post_auth_result or {})

    def load_account_state(self, **kwargs):
        self.state_calls.append(kwargs)
        if self.state_raises is not None:
            raise self.state_raises
        return dict(self.state_result)


def make_sc(page, *, state=None, builders=None, cancel=None, extra=None):
    logs = []
    ctx = SimpleNamespace(
        platform_name="windsurf",
        platform_display_name="Windsurf",
        platform=SimpleNamespace(),
        proxy=None,
        executor_type="headless",
        extra=dict(extra or {}),
        log=lambda message, **kwargs: logs.append(message),
    )
    sc = StepContext(
        ctx,
        page=page,
        cancel=cancel,
        resolver=CapabilityResolver(ctx, builders=builders or {}),
        state=dict(state or {"email": "user42@example.com", "password": "Pw-123456"}),
        attempt_id="t1",
    )
    sc.logs = logs
    return sc


UNSET = object()


def run_windsurf_flow(
    page,
    *,
    otp_raw="Your code is 123456",
    state=None,
    cancel=None,
    otp_callback=UNSET,
    extra=None,
    emit_on_create=True,
    complete_body=None,
):
    """真实 build_flow() 走引擎；点 Create account 时把站点那两条响应喂进监听。"""
    callback = (lambda **kw: otp_raw) if otp_callback is UNSET else otp_callback
    builders = {"otp": lambda ctx, step, key: callback}
    sc = make_sc(page, state=state, builders=builders, cancel=cancel, extra=extra)

    if emit_on_create:
        original = page.get_by_role

        def get_by_role(role, name=None):
            locator = original(role, name=name)
            label = getattr(name, "pattern", str(name))
            if "Create account" in label:
                inner_click = locator.click

                def click():
                    inner_click()
                    page.emit(
                        FakeResponse(
                            f"{dom.WINDSURF_BASE}{dom.EMAIL_COMPLETE_PATH}",
                            json_body=complete_body
                            if complete_body is not None
                            else {"token": AUTH_TOKEN, "email": "user42@example.com", "user_id": "u_from_complete"},
                        )
                    )
                    page.emit(
                        FakeResponse(
                            f"{dom.WINDSURF_BASE}/x/{dom.POST_AUTH_PATH}",
                            body=post_auth_payload(),
                        )
                    )

                locator.click = click
            return locator

        page.get_by_role = get_by_role

    return StepRunner(steps.build_flow()).run(sc), sc


@pytest.fixture(autouse=True)
def fast_waits(monkeypatch):
    """去掉墙钟等待，但保留取消语义。"""
    from core import cancellation

    def instant_sleep(seconds, *, should_stop=None, tick=0.2):
        cancellation._raise_if_stopped(should_stop)

    monkeypatch.setattr("core.cancellation.time.sleep", lambda seconds: None)
    monkeypatch.setattr("core.registration.steps.cancellable_sleep", instant_sleep)
    monkeypatch.setattr(steps, "SESSION_TIMEOUT", 0.05)


@pytest.fixture
def api(monkeypatch):
    stub = FakeApi()
    monkeypatch.setattr(steps, "_api", lambda sc: stub)
    return stub


class TestWindsurfFlowThroughEngine:
    def test_happy_path(self, api):
        page = ScriptedPage()

        result, _ = run_windsurf_flow(page)

        assert result["session_token"] == SESSION_TOKEN
        assert result["auth_token"] == AUTH_TOKEN
        assert result["account_id"] == ACCOUNT_ID
        assert result["org_id"] == ORG_ID
        assert result["account_overview"] == OVERVIEW
        assert result["state_summary"] == SUMMARY
        # 捕到了 PostAuth 就不必再补兑换一次
        assert api.post_auth_calls == []

    def test_result_has_exactly_the_keys_the_plugin_maps(self, api):
        from platforms.windsurf.plugin import WindsurfPlatform

        result, _ = run_windsurf_flow(ScriptedPage())
        assert set(result) == {
            "email", "password", "name", "user_id", "auth_token",
            "session_token", "account_id", "org_id", "account_overview", "state_summary",
        }

        mapped = WindsurfPlatform()._map_windsurf_result(result)
        assert mapped.token == SESSION_TOKEN
        assert mapped.user_id == "u_from_complete"
        assert mapped.extra["account_overview"] == OVERVIEW

    def test_the_name_is_derived_from_the_email_and_sanitized(self, api):
        page = ScriptedPage()

        _, sc = run_windsurf_flow(page, state={"email": "sypt1kgb@example.com", "password": "Pw-1"})

        # 本地部分里的数字当成分隔符 → "sypt kgb" → 两段各首字母大写
        assert sc.state["name"] == "Sypt Kgb"
        # 姓名框拒收数字，填进去的必须是清洗过的
        assert page.filled[dom.GIVEN_NAME_SELECTOR] == ["Sypt"]
        assert page.filled[dom.FAMILY_NAME_SELECTOR] == ["Kgb"]

    def test_an_all_digit_local_part_still_yields_a_usable_name(self, api):
        page = ScriptedPage()

        _, sc = run_windsurf_flow(page, state={"email": "12345@example.com", "password": "Pw-1"})

        assert sc.state["name"] == "Windsurf User"
        assert page.filled[dom.GIVEN_NAME_SELECTOR] == ["Windsurf"]

    def test_an_explicit_name_from_the_task_wins(self, api):
        page = ScriptedPage()

        _, sc = run_windsurf_flow(page, extra={"name": "Ada Lovelace"})

        assert sc.state["name"] == "Ada Lovelace"
        assert page.filled[dom.GIVEN_NAME_SELECTOR] == ["Ada"]
        assert page.filled[dom.FAMILY_NAME_SELECTOR] == ["Lovelace"]

    def test_the_terms_checkbox_is_ticked_and_both_password_fields_filled(self, api):
        page = ScriptedPage()

        result, _ = run_windsurf_flow(page)

        assert dom.TOS_CHECKBOX_SELECTOR in page.checked
        assert page.filled[dom.PASSWORD_SELECTOR] == [result["password"]]
        assert page.filled[dom.CONFIRM_PASSWORD_SELECTOR] == [result["password"]]

    def test_the_response_listener_is_installed_before_navigating(self, api):
        """session token 只在点 Create account 那一下的响应里；监听挂晚了就永远拿不到。"""
        page = ScriptedPage()
        order = []
        original_on, original_goto = page.on, page.goto
        page.on = lambda event, handler: (order.append("on"), original_on(event, handler))[1]
        page.goto = lambda url, **kw: (order.append("goto"), original_goto(url, **kw))[1]

        run_windsurf_flow(page)

        assert order == ["on", "goto"]

    def test_the_six_digit_code_is_split_across_the_otp_boxes(self, api):
        page = ScriptedPage(otp_boxes=6)

        run_windsurf_flow(page, otp_raw="Windsurf code: 246813 (expires soon)")

        boxes = {k: v for k, v in page.filled.items() if k.startswith(dom.OTP_ANY_INPUT_SELECTOR)}
        assert [v[0] for _, v in sorted(boxes.items())] == list("246813")

    def test_a_single_input_takes_the_whole_code(self, api):
        page = ScriptedPage(otp_boxes=1)

        run_windsurf_flow(page, otp_raw="123456")

        assert page.filled[dom.OTP_INPUT_SELECTOR] == ["123456"]


class TestWindsurfSessionResolution:
    def test_a_missing_post_auth_response_is_recovered_through_the_page_session(self, api):
        """旧实现的这条兜底：只捕到 /email/complete 时用页面会话补兑换一次。"""
        api.post_auth_result = {
            "session_token": SESSION_TOKEN,
            "account_id": ACCOUNT_ID,
            "org_id": ORG_ID,
        }
        page = ScriptedPage()

        def get_by_role(role, name=None):
            label = getattr(name, "pattern", str(name))
            locator = ScriptedPage.get_by_role(page, role, name=name)
            if "Create account" in label:
                inner = locator.click

                def click():
                    inner()
                    page.emit(
                        FakeResponse(
                            f"{dom.WINDSURF_BASE}{dom.EMAIL_COMPLETE_PATH}",
                            json_body={"token": AUTH_TOKEN, "email": "user42@example.com"},
                        )
                    )

                locator.click = click
            return locator

        page.get_by_role = get_by_role

        result, _ = run_windsurf_flow(page, emit_on_create=False)

        assert api.post_auth_calls == [AUTH_TOKEN]
        assert result["session_token"] == SESSION_TOKEN

    def test_nothing_captured_at_all_is_transient(self, api):
        page = ScriptedPage(body_text="Please try again")

        with pytest.raises(TransientError, match="Please try again"):
            run_windsurf_flow(page, emit_on_create=False)

        assert api.post_auth_calls == [], "连 auth_token 都没有就别去补兑换"

    def test_the_site_normalized_email_wins_over_the_one_we_submitted(self, api):
        page = ScriptedPage()

        result, _ = run_windsurf_flow(
            page, complete_body={"token": AUTH_TOKEN, "email": "User42@Example.com", "user_id": "u1"}
        )

        assert result["email"] == "User42@Example.com"

    def test_the_user_id_falls_back_to_the_account_overview(self, api):
        page = ScriptedPage()

        result, _ = run_windsurf_flow(page, complete_body={"token": AUTH_TOKEN, "email": "user42@example.com"})

        assert result["user_id"] == "u_from_overview"


class TestWindsurfErrorClassification:
    """旧实现这些分支全是裸 RuntimeError，classify() 认作 unknown，一次都不重试。"""

    def test_never_reaching_the_register_page_is_a_site_change(self, api):
        page = ScriptedPage(missing_stage="register")

        with pytest.raises(SiteChangedError, match="未进入 Windsurf 注册页"):
            run_windsurf_flow(page)

    def test_a_form_that_does_not_advance_is_a_site_change(self, api):
        page = ScriptedPage(missing_stage="password")

        with pytest.raises(SiteChangedError, match="注册表单提交失败"):
            run_windsurf_flow(page)

    def test_a_password_page_that_does_not_advance_is_a_site_change(self, api):
        page = ScriptedPage(missing_stage="otp")

        with pytest.raises(SiteChangedError, match="密码页提交失败"):
            run_windsurf_flow(page)

    def test_a_mail_without_a_six_digit_code_is_transient(self, api):
        with pytest.raises(TransientError, match="6 位验证码"):
            run_windsurf_flow(ScriptedPage(), otp_raw="no digits here")

    def test_missing_mailbox_provider_is_a_config_error(self, api):
        with pytest.raises(ConfigError, match="mailbox provider"):
            run_windsurf_flow(ScriptedPage(), otp_callback=None)

    def test_missing_email_is_a_config_error(self, api):
        with pytest.raises(ConfigError, match="邮箱地址"):
            run_windsurf_flow(ScriptedPage(), state={"email": "", "password": "Pw-1"})

    def test_missing_password_is_a_config_error(self, api):
        with pytest.raises(ConfigError, match="密码"):
            run_windsurf_flow(ScriptedPage(), state={"email": "user@example.com", "password": ""})


class TestWindsurfCancellation:
    def test_cancel_while_waiting_for_the_code_aborts_before_creating_the_account(self, api):
        page = ScriptedPage()
        token = CancellationToken()

        def slow_otp(**kwargs):
            token.trip("用户停止")
            return "123456"

        with pytest.raises(AttemptCancelled):
            run_windsurf_flow(page, cancel=token, otp_callback=slow_otp)

        assert api.state_calls == []

    def test_the_cancel_token_reaches_the_otp_callback(self, api):
        seen = {}

        def otp(**kwargs):
            seen.update(kwargs)
            return "123456"

        run_windsurf_flow(ScriptedPage(), cancel=CancellationToken(), otp_callback=otp)

        assert "should_stop" in seen


class TestWindsurfFlowShape:
    def test_only_the_navigation_step_is_an_anchor(self):
        flow = steps.build_flow()
        flow.validate()
        assert flow.anchors() == ["OpenRegisterPage"]

    def test_requirements_are_otp_only(self):
        from core.requirements import LEVEL_REQUIRED, from_step_flow

        levels = {item.capability: item.level for item in from_step_flow(steps.build_flow(), [])}

        assert levels == {"otp": LEVEL_REQUIRED}

    def test_the_response_listener_is_not_installed_twice_on_rewind(self, api):
        """回撤重跑锚点：监听还是同一个 page 上那一个，但旧响应要清掉。"""
        page = ScriptedPage()
        sc = make_sc(page)
        step = steps.OpenRegisterPage()

        step.run(sc)
        sc.handles[steps.CAPTURE_HANDLE]["auth"] = {"session_token": "stale"}
        step.run(sc)

        assert len(page.listeners) == 1
        assert sc.handles[steps.CAPTURE_HANDLE] == {"complete": {}, "auth": {}}


class TestWindsurfAdapterWiring:
    def test_browser_adapter_uses_the_step_flow_and_keeps_the_engine_fallback(self):
        from platforms.windsurf.plugin import WindsurfPlatform

        adapter = WindsurfPlatform().build_browser_registration_adapter()

        assert adapter.step_flow_builder is not None
        assert adapter.browser_worker_builder is None
        assert adapter.engine_preference == "camoufox"
        # 旧实现本来就是"先试 Camoufox，抛异常再退回 Playwright"，四个平台里只有这个开回退
        assert adapter.allow_engine_fallback is True
        assert adapter.step_flow_builder().name == "windsurf.browser_signup"
        assert adapter.otp_spec.code_pattern == r"\b(\d{6})\b"


class TestWindsurfPaymentCodeUntouched:
    def test_the_stripe_checkout_flow_still_lives_in_browser_register(self):
        """迁移只动注册那一段；结账那一套（800 多行）原样留在 browser_register.py。"""
        import platforms.windsurf.browser_register as browser_register

        assert hasattr(browser_register, "WindsurfStripeCheckoutBrowser")
        assert hasattr(browser_register, "generate_checkout_link_via_windsurf_ui")
        assert not hasattr(browser_register, "WindsurfBrowserRegister")
        assert not hasattr(browser_register, "WindsurfBrowserApi")
