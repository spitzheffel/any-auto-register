"""OpenBlockLabs 步骤流：WorkOS 会话初始化 + 两条密码分支 + 中间页。

这里不碰网络也不起浏览器——用一个按阶段换页的假页面驱动真实的
``build_flow()`` + ``build_obstacles()``，所以测的是引擎与步骤之间的契约，
不是选择器对不对（那是 ``drift.py`` 的活）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.cancellation import CancellationToken
from core.errors import AttemptCancelled, ConfigError, SiteChangedError, TransientError
from core.registration.steps import CapabilityResolver, StepContext, StepRunner
from platforms.openblocklabs import dom, obstacles, steps

SESSION_ID = "auth_sess_01ABC"
WOS = "wos_cookie_value"

#: 姓名的选择器在 dom 里是一个逗号串，假页面按拆开后的单个选择器建元素
FIRST_NAME_INPUT = 'input[name="first_name"]'
LAST_NAME_INPUT = 'input[name="last_name"]'


class FakeElement:
    def __init__(self, *, page=None, selector="", kind="text", visible=True, enabled=True):
        self.page = page
        self.selector = selector
        self.kind = kind
        self._visible = visible
        self._enabled = enabled
        self.value = ""
        self.clicks = 0
        self.attrs = {}

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return self._enabled

    def input_value(self):
        return self.value

    def get_attribute(self, name):
        return self.attrs.get(name)

    def fill(self, value):
        self.value = value
        if self.page is not None and value:
            self.page.filled[self.selector] = value

    def type(self, value, delay=None):
        self.fill(value)

    def click(self, timeout=None, force=False):
        self.clicks += 1
        if self.page is not None:
            self.page.clicked.append(self.selector)
            if self.kind == "button":
                self.page.advance()

    def scroll_into_view_if_needed(self, timeout=None):
        return None

    def evaluate(self, script, arg=None):
        if "value" in script and arg is not None:
            self.fill(arg)
            return arg
        if "el.click()" in script:
            self.click()
            return None
        return self.value


class FakeContext:
    def __init__(self):
        self._cookies = []

    def cookies(self):
        return list(self._cookies)

    def set(self, name, value):
        self._cookies = [c for c in self._cookies if c["name"] != name]
        self._cookies.append({"name": name, "value": value})


class ScriptedPage:
    """按阶段换页的假页面。

    ``entry`` → WorkOS 入口（URL 上带 authorization_session_id）
    ``form``  → 注册表单（``inline_password`` 决定密码框在不在同一页）
    ``password`` → 单独的密码页
    ``interstitial`` → 需要再点一次继续的中间页
    ``otp``   → 验证码页
    ``dashboard`` → 落地页，带 wos-session
    """

    def __init__(self, *, script=None, inline_password=True, session_id=SESSION_ID, not_found_at=None, wos=WOS):
        self.inline_password = inline_password
        self.session_id = session_id
        self.not_found_at = not_found_at
        self.wos = wos
        self.script = list(
            script
            if script is not None
            else ("entry", "form", "otp", "dashboard")
            if inline_password
            else ("entry", "form", "password", "otp", "dashboard")
        )
        self.index = 0
        self.context = FakeContext()
        self.filled = {}
        self.clicked = []
        self.typed = []
        self.goto_urls = []
        self.init_scripts = []
        self.frames = []
        self._apply()

    # -- 阶段 -----------------------------------------------------------
    @property
    def stage(self):
        return self.script[self.index]

    def _apply(self):
        stage = self.stage
        if stage == "entry":
            self.url = f"{dom.AUTH_BASE}/?client_id={dom.CLIENT_ID}&authorization_session_id={self.session_id}"
        elif stage == "form":
            self.url = dom.signup_url(self.session_id)
        elif stage == "password":
            self.url = f"{dom.AUTH_BASE}{dom.PASSWORD_URL_MARKER}"
        elif stage == "interstitial":
            self.url = f"{dom.AUTH_BASE}/sign-up/continue"
        elif stage == "otp":
            self.url = f"{dom.AUTH_BASE}/sign-up/{dom.EMAIL_VERIFICATION_URL_MARKER}"
        else:
            self.url = f"{dom.DASHBOARD}/home"
            if self.wos:
                self.context.set(dom.SESSION_COOKIE, self.wos)

    def advance(self):
        if self.index < len(self.script) - 1:
            self.index += 1
            self._apply()

    def _inputs(self):
        stage = self.stage
        if stage == "form":
            fields = {
                dom.EMAIL_INPUT_SELECTORS[0]: "text",
                FIRST_NAME_INPUT: "text",
                LAST_NAME_INPUT: "text",
            }
            if self.inline_password:
                fields[dom.PASSWORD_INPUT_SELECTORS[0]] = "password"
            return fields
        if stage == "password":
            return {dom.PASSWORD_INPUT_SELECTORS[0]: "password"}
        if stage == "otp":
            return {dom.OTP_INPUT_SELECTORS[0]: "text"}
        return {}

    def _buttons(self):
        return () if self.stage in ("entry", "dashboard") else (dom.SUBMIT_BUTTON_SELECTORS[0],)

    # -- Page 接口 ------------------------------------------------------
    def add_init_script(self, script):
        self.init_scripts.append(script)

    def goto(self, url, **kwargs):
        self.goto_urls.append(url)
        if url == dom.ENTRY_URL:
            self.index = 0
        elif dom.signup_url(self.session_id) == url and "form" in self.script:
            self.index = self.script.index("form")
        self._apply()

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def wait_for_url(self, pattern, timeout=None):
        return None

    def content(self):
        return "<html><body>openblocklabs</body></html>"

    def query_selector(self, selector):
        if selector == 'input[name="authorization_session_id"][value]':
            return None
        found = self.query_selector_all(selector)
        return found[0] if found else None

    def query_selector_all(self, selector):
        parts = [p.strip() for p in selector.split(",")]
        out = []
        for part in parts:
            if part in self._inputs():
                element = FakeElement(page=self, selector=part, kind=self._inputs()[part])
                element.attrs["type"] = "password" if self._inputs()[part] == "password" else "text"
                out.append(element)
            if part in self._buttons():
                out.append(FakeElement(page=self, selector=part, kind="button"))
            if part == 'input:not([type="hidden"])':
                for name, kind in self._inputs().items():
                    element = FakeElement(page=self, selector=name, kind=kind)
                    element.attrs["type"] = "password" if kind == "password" else "text"
                    out.append(element)
        return out

    def fill(self, selector, value):
        self.filled[selector] = value

    def locator(self, selector):
        if selector == "body":
            text = "The requested resource was not found" if self.stage == self.not_found_at else "openblocklabs"
            return SimpleNamespace(inner_text=lambda: text)
        return SimpleNamespace(inner_text=lambda: "")

    def evaluate(self, script, arg=None):
        return None

    @property
    def keyboard(self):
        page = self

        class Keyboard:
            def type(self, text):
                page.typed.append(text)

        return Keyboard()

    @property
    def viewport_size(self):
        return {"width": 1280, "height": 720}


def make_sc(page, *, state=None, builders=None, cancel=None):
    logs = []
    ctx = SimpleNamespace(
        platform_name="openblocklabs",
        platform_display_name="OpenBlockLabs",
        platform=SimpleNamespace(),
        proxy=None,
        executor_type="headless",
        extra={},
        log=lambda message, **kwargs: logs.append(message),
    )
    sc = StepContext(
        ctx,
        page=page,
        cancel=cancel,
        resolver=CapabilityResolver(ctx, builders=builders or {}),
        state=dict(state or {"email": "user@example.com", "password": ""}),
        attempt_id="t1",
    )
    sc.logs = logs
    return sc


UNSET = object()


def run_obl_flow(page, *, otp_code="123456", state=None, cancel=None, otp_callback=UNSET):
    callback = (lambda **kw: otp_code) if otp_callback is UNSET else otp_callback
    builders = {"otp": lambda ctx, step, key: callback, "captcha": lambda ctx, step, key: None}
    sc = make_sc(page, state=state, builders=builders, cancel=cancel)
    return StepRunner(steps.build_flow(), obstacles=obstacles.build_obstacles()).run(sc), sc


@pytest.fixture(autouse=True)
def fast_waits(monkeypatch):
    """去掉墙钟等待，但保留取消语义。"""
    from core import cancellation

    def instant_sleep(seconds, *, should_stop=None, tick=0.2):
        cancellation._raise_if_stopped(should_stop)

    monkeypatch.setattr(dom, "cancellable_sleep", instant_sleep)
    monkeypatch.setattr("core.registration.dom.cancellable_sleep", instant_sleep)
    monkeypatch.setattr("core.registration.steps.cancellable_sleep", instant_sleep)
    monkeypatch.setattr("core.registration.obstacles.time.sleep", lambda seconds: None)
    for name in (
        "SIGNUP_SESSION_TIMEOUT",
        "EMAIL_INPUT_TIMEOUT",
        "PASSWORD_INPUT_TIMEOUT",
        "PASSWORD_PAGE_URL_TIMEOUT",
        "ADVANCE_TIMEOUT",
        "VERIFICATION_READY_TIMEOUT",
        "DASHBOARD_TIMEOUT",
        "WOS_SESSION_TIMEOUT",
    ):
        monkeypatch.setattr(steps, name, 0.05)
    monkeypatch.setattr(steps, "ADVANCE_CLICK_INTERVAL", 0.0)
    # Turnstile handler 在这一步之前会多等一会看挑战晚不晚到；测试里没有挑战，别白等
    monkeypatch.setattr(obstacles, "TURNSTILE_POLL_SECONDS", 0.01)


class TestOpenBlockLabsFlowThroughEngine:
    def test_happy_path_with_the_inline_password_field(self):
        page = ScriptedPage(inline_password=True)

        result, _ = run_obl_flow(page)

        assert result["wos_session"] == WOS
        assert result["email"] == "user@example.com"
        assert page.filled[dom.EMAIL_INPUT_SELECTORS[0]] == "user@example.com"
        assert page.filled[dom.PASSWORD_INPUT_SELECTORS[0]] == result["password"]
        assert page.typed == ["123456"]

    def test_happy_path_with_the_separate_password_page(self):
        """WorkOS 的另一条投放：邮箱先单独提交，密码在下一页。"""
        page = ScriptedPage(inline_password=False)

        result, _ = run_obl_flow(page)

        assert result["wos_session"] == WOS
        assert page.filled[dom.PASSWORD_INPUT_SELECTORS[0]] == result["password"]

    def test_an_interstitial_page_is_clicked_through(self):
        page = ScriptedPage(script=("entry", "form", "interstitial", "otp", "dashboard"))

        result, _ = run_obl_flow(page)

        assert result["wos_session"] == WOS

    def test_result_has_exactly_the_keys_the_plugin_maps(self):
        from platforms.openblocklabs.plugin import OpenBlockLabsPlatform

        result, _ = run_obl_flow(ScriptedPage())
        assert set(result) == {"email", "password", "wos_session"}

        mapped = OpenBlockLabsPlatform()._map_openblocklabs_result(result)
        assert mapped.token == WOS
        assert mapped.extra == {"wos_session": WOS}

    def test_empty_password_is_generated_to_the_workos_policy(self):
        result, _ = run_obl_flow(ScriptedPage(), state={"email": "user@example.com", "password": ""})

        password = result["password"]
        assert any(c.isupper() for c in password)
        assert any(c.isdigit() for c in password)
        assert any(c.islower() for c in password)
        assert password.endswith("!")

    def test_caller_supplied_password_is_kept(self):
        page = ScriptedPage()

        result, _ = run_obl_flow(page, state={"email": "user@example.com", "password": "Given-1234!"})

        assert result["password"] == "Given-1234!"
        assert page.filled[dom.PASSWORD_INPUT_SELECTORS[0]] == "Given-1234!"

    def test_the_signup_url_carries_the_workos_session_id(self):
        """直接 goto /sign-up 会拿到 Not Found，必须带上入口给的 session id。"""
        page = ScriptedPage()

        _, sc = run_obl_flow(page)

        assert sc.state["session_id"] == SESSION_ID
        assert any(SESSION_ID in url for url in page.goto_urls)

    def test_the_mouse_event_patch_is_installed(self):
        """Turnstile 的被动检测会看 screenX/screenY。"""
        page = ScriptedPage()

        run_obl_flow(page)

        assert page.init_scripts and "screenX" in page.init_scripts[0]

    def test_the_names_are_filled_when_the_form_asks(self):
        page = ScriptedPage()

        _, sc = run_obl_flow(page)

        assert page.filled[dom.FIRST_NAME_SELECTOR] == sc.state["first_name"]
        assert page.filled[dom.LAST_NAME_SELECTOR] == sc.state["last_name"]


class TestOpenBlockLabsErrorClassification:
    """旧实现这些分支全是裸 RuntimeError，classify() 认作 unknown，一次都不重试。"""

    def test_a_not_found_entry_page_is_a_site_change(self):
        page = ScriptedPage(session_id="", not_found_at="entry")

        with pytest.raises(SiteChangedError, match="Not Found"):
            run_obl_flow(page)

    def test_a_missing_session_id_is_transient(self):
        page = ScriptedPage(session_id="")

        with pytest.raises(TransientError, match="authorization_session_id"):
            run_obl_flow(page)

    def test_a_not_found_interstitial_is_a_site_change(self):
        """中间页上什么都没有（WorkOS 会话过期就长这样），再点也点不出验证码页。"""
        page = ScriptedPage(script=("entry", "form", "interstitial", "otp", "dashboard"), not_found_at="interstitial")
        original = page._buttons
        # 只有中间页没按钮：表单页还得提交得出去
        page._buttons = lambda: () if page.stage == "interstitial" else original()

        with pytest.raises(SiteChangedError, match="中间页返回 Not Found"):
            run_obl_flow(page)

    def test_missing_mailbox_provider_is_a_config_error(self):
        with pytest.raises(ConfigError, match="mailbox provider"):
            run_obl_flow(ScriptedPage(), otp_callback=None)

    def test_empty_code_is_transient(self):
        with pytest.raises(TransientError, match="未获取到 OpenBlockLabs 邮箱验证码"):
            run_obl_flow(ScriptedPage(), otp_callback=lambda **kw: "")

    def test_missing_email_is_a_config_error(self):
        with pytest.raises(ConfigError, match="邮箱地址"):
            run_obl_flow(ScriptedPage(), state={"email": "", "password": ""})

    def test_never_reaching_the_dashboard_is_transient(self):
        page = ScriptedPage(script=("entry", "form", "otp"))

        with pytest.raises(TransientError, match="未跳转到 dashboard"):
            run_obl_flow(page)

    def test_a_missing_session_cookie_is_transient(self):
        page = ScriptedPage(wos="")

        with pytest.raises(TransientError, match=dom.SESSION_COOKIE):
            run_obl_flow(page)


class TestOpenBlockLabsInputHardening:
    def test_the_value_is_read_back_after_filling(self):
        """WorkOS 的受控组件常吞掉 fill；不回读就会带着空邮箱一路走下去。"""
        page = ScriptedPage()
        page.index = page.script.index("form")
        page._apply()

        used, current = dom.fill_visible_input(page, dom.EMAIL_INPUT_SELECTORS, "a@b.c", timeout=0.05)

        assert used == dom.EMAIL_INPUT_SELECTORS[0]
        assert current == ""

    def test_a_field_that_refuses_every_write_reports_its_current_value(self):
        class Stubborn(FakeElement):
            def fill(self, value):
                return None

            def type(self, value, delay=None):
                return None

            def evaluate(self, script, arg=None):
                return "stuck"

            def input_value(self):
                return "stuck"

        page = ScriptedPage()
        page.query_selector_all = lambda selector: (
            [Stubborn(page=page, selector=dom.EMAIL_INPUT_SELECTORS[0])]
            if selector == dom.EMAIL_INPUT_SELECTORS[0]
            else []
        )

        used, current = dom.fill_visible_input(page, dom.EMAIL_INPUT_SELECTORS, "a@b.c", timeout=0.05)

        assert used == ""
        assert current == "stuck"

    def test_hidden_duplicates_left_in_the_dom_are_skipped(self):
        """WorkOS 把上一步的输入框留在 DOM 里并隐藏，只取第一个会拿到那个。"""
        page = ScriptedPage()
        hidden = FakeElement(page=page, selector="x", visible=False)
        shown = FakeElement(page=page, selector="x")
        page.query_selector_all = lambda selector: [hidden, shown]

        element, _ = dom.find_visible_element(page, ("x",))

        assert element is shown


class TestOpenBlockLabsCancellation:
    def test_cancel_during_the_otp_step_stops_before_collecting_the_session(self):
        """取消在 OTP 步骤里生效：验证码那一下已经发出去了（拿都拿了），
        但后面的"等 dashboard、取 wos-session"一步都不会跑。
        """
        page = ScriptedPage()
        token = CancellationToken()
        builders = {
            "otp": lambda ctx, step, key: (lambda **kw: token.trip("用户停止") or "123456"),
            "captcha": lambda ctx, step, key: None,
        }
        sc = make_sc(page, builders=builders, cancel=token)

        with pytest.raises(AttemptCancelled):
            StepRunner(steps.build_flow(), obstacles=obstacles.build_obstacles()).run(sc)

        assert "wos_session" not in sc.state
        assert not any("注册成功" in line for line in sc.logs)

    def test_the_cancel_token_reaches_the_otp_callback(self):
        seen = {}

        def otp(**kwargs):
            seen.update(kwargs)
            return "123456"

        run_obl_flow(ScriptedPage(), cancel=CancellationToken(), otp_callback=otp)

        assert "should_stop" in seen


class TestOpenBlockLabsFlowShape:
    def test_only_the_navigation_step_is_an_anchor(self):
        flow = steps.build_flow()
        flow.validate()
        assert flow.anchors() == ["OpenSignupPage"]

    def test_turnstile_only_adds_a_conditional_captcha_channel(self):
        from core.requirements import LEVEL_CONDITIONAL, LEVEL_REQUIRED, from_step_flow
        levels = {item.capability: item.level for item in from_step_flow(steps.build_flow(), obstacles.build_obstacles())}

        assert levels == {"otp": LEVEL_REQUIRED, "captcha": LEVEL_CONDITIONAL}

    def test_the_cloudflare_helpers_come_from_core_not_a_local_copy(self):
        """旧实现抄了 cursor 那 150 行；重复的那份是这次迁移删掉的主要内容。"""
        import core.registration.dom as core_dom

        assert dom.click_turnstile_in_iframe is core_dom.click_turnstile_in_iframe
        assert dom.is_turnstile_modal_visible is core_dom.is_turnstile_modal_visible
        assert dom.MOUSE_EVENT_PATCH is core_dom.MOUSE_EVENT_PATCH


class TestOpenBlockLabsAdapterWiring:
    def test_browser_adapter_uses_the_step_flow(self):
        from platforms.openblocklabs.plugin import OpenBlockLabsPlatform

        adapter = OpenBlockLabsPlatform().build_browser_registration_adapter()

        assert adapter.step_flow_builder is not None
        assert adapter.obstacle_builder is not None
        assert adapter.browser_worker_builder is None
        assert adapter.engine_preference == "camoufox"
        assert adapter.allow_engine_fallback is False
        assert adapter.step_flow_builder().name == "openblocklabs.browser_signup"
