"""Tavily 步骤流：Auth0 托管页 + 必过的 Turnstile + 三选一的后续页面。

这里不碰网络也不起浏览器——用一个按阶段换页的假页面驱动真实的 ``build_flow()``，
所以测的是引擎与步骤之间的契约，不是选择器对不对（那是 ``drift.py`` 的活）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.cancellation import CancellationToken
from core.errors import (
    AttemptCancelled,
    ConfigError,
    ProviderError,
    SiteChangedError,
    TransientError,
)
from core.registration.steps import CapabilityResolver, StepContext, StepRunner
from platforms.tavily import dom, steps

SIGNUP_HREF = "/u/signup/identifier?state=abc123"
SIGNIN_HTML = f'<html><a href="{SIGNUP_HREF}">Sign up</a></html>'
API_KEY = "tvly-dev-abcdefghijklmnopqrstuvwxyz"


class FakeElement:
    def __init__(self, text=""):
        self.text = text
        self.clicks = 0

    def inner_text(self):
        return self.text

    def click(self, **kwargs):
        self.clicks += 1

    def is_visible(self):
        return True


class FakeSolver:
    def __init__(self, token="turnstile_token", raises=None):
        self.token = token
        self.raises = raises
        self.calls = []

    def solve_turnstile(self, url, sitekey):
        self.calls.append((url, sitekey))
        if self.raises is not None:
            raise self.raises
        return self.token


class ScriptedPage:
    """按阶段换页的假页面。

    ``stages`` 是"提交一次就往前走一格"的脚本；每一格决定 URL、页面上有哪些
    输入框、以及 ``content()`` 里有没有 API Key。
    """

    STAGES = {
        "signin": {"url": dom.SIGNIN_URL, "inputs": (), "html": SIGNIN_HTML},
        "signup": {"url": f"{dom.AUTH_URL}{SIGNUP_HREF}", "inputs": dom.EMAIL_INPUT_SELECTORS[:1]},
        "otp": {"url": f"{dom.AUTH_URL}/u/login/code", "inputs": (dom.OTP_INPUT_SELECTOR,)},
        "password": {"url": f"{dom.AUTH_URL}/u/signup/password", "inputs": (dom.PASSWORD_INPUT_SELECTOR,)},
        "verify": {"url": f"{dom.AUTH_URL}/u/verify", "inputs": ()},
        "app": {"url": f"{dom.APP_URL}/home", "inputs": (), "html": f"<html>key: {API_KEY}</html>"},
    }

    def __init__(
        self,
        *,
        script=("signin", "signup", "otp", "password", "app"),
        feedback="",
        body_text="",
        turnstile_state=None,
        api_key_html=None,
    ):
        self.script = list(script)
        self.index = 0
        self.filled = {}
        self.clicked = []
        self.pressed = []
        self.goto_urls = []
        self.injected_tokens = []
        self.reloads = 0
        self.feedback = feedback
        self.body_text = body_text
        self.turnstile_state = turnstile_state or {}
        self.api_key_html = api_key_html
        self._apply()

    # -- 阶段 -----------------------------------------------------------
    @property
    def stage(self):
        return self.script[self.index]

    def _apply(self):
        self.url = self.STAGES[self.stage]["url"]

    def advance(self):
        if self.index < len(self.script) - 1:
            self.index += 1
            self._apply()

    def _inputs(self):
        return self.STAGES[self.stage]["inputs"]

    # -- Page 接口 ------------------------------------------------------
    def goto(self, url, **kwargs):
        self.goto_urls.append(url)
        if url == dom.SIGNIN_URL:
            self.index = 0
        elif SIGNUP_HREF in url:
            self.index = self.script.index("signup")
        elif dom.APP_URL in url and "app" in self.script:
            self.index = self.script.index("app")
        self._apply()

    def content(self):
        stage = self.STAGES[self.stage]
        if self.stage == "app" and self.api_key_html is not None:
            return self.api_key_html
        return stage.get("html", "<html></html>")

    def query_selector(self, selector):
        if selector == 'button[aria-label="Close"]':
            return None
        if selector in dom.PRIMARY_BUTTON_SELECTORS:
            return FakeElement()
        # 引擎里用逗号串查"验证码框或密码框"
        wanted = [part.strip() for part in selector.split(",")]
        for part in wanted:
            if part in self._inputs():
                return FakeElement()
        return None

    def query_selector_all(self, selector):
        if selector in dom.FEEDBACK_SELECTORS and self.feedback:
            return [FakeElement(self.feedback)]
        return []

    def locator(self, selector):
        if selector == "body":
            return FakeElement(self.body_text)
        return FakeElement()

    def fill(self, selector, value):
        self.filled[selector] = value

    def press(self, selector, key):
        self.pressed.append((selector, key))

    def click(self, selector, **kwargs):
        self.clicked.append(selector)
        self.advance()

    def reload(self, **kwargs):
        self.reloads += 1

    def wait_for_selector(self, selector, timeout=None):
        return FakeElement()

    def wait_for_url(self, pattern, timeout=None):
        return None

    def evaluate(self, script, arg=None):
        if "data-captcha-sitekey" in script and "passwordInput" in script:
            return dict(self.turnstile_state)
        if "data-captcha-sitekey" in script:
            return "site_from_dom"
        if "ensureField" in script:
            self.injected_tokens.append(arg)
            return True
        return None


def make_sc(page, *, state=None, builders=None, cancel=None, extra=None):
    logs = []
    ctx = SimpleNamespace(
        platform_name="tavily",
        platform_display_name="Tavily",
        platform=SimpleNamespace(),
        proxy=None,
        extra=dict(extra or {}),
        log=lambda message, **kwargs: logs.append(message),
    )
    sc = StepContext(
        ctx,
        page=page,
        cancel=cancel,
        resolver=CapabilityResolver(ctx, builders=builders or {}),
        state=dict(state or {"email": "user@example.com", "password": "Pw-123456"}),
        attempt_id="t1",
    )
    sc.logs = logs
    return sc


UNSET = object()


def run_tavily_flow(
    page,
    *,
    otp_code="123456",
    link="https://auth.tavily.com/u/verify?ticket=t1",
    solver=UNSET,
    otp_callback=UNSET,
    link_callback=UNSET,
    state=None,
    cancel=None,
    extra=None,
):
    builders = {
        "captcha": lambda ctx, step, key: (FakeSolver() if solver is UNSET else solver),
        "otp": lambda ctx, step, key: ((lambda **kw: otp_code) if otp_callback is UNSET else otp_callback),
        "link": lambda ctx, step, key: ((lambda **kw: link) if link_callback is UNSET else link_callback),
    }
    sc = make_sc(page, state=state, builders=builders, cancel=cancel, extra=extra)
    return StepRunner(steps.build_flow()).run(sc), sc


@pytest.fixture(autouse=True)
def fast_waits(monkeypatch):
    """去掉墙钟等待与那一次真实的 API 校验请求，但保留取消语义。"""
    from core import cancellation

    def instant_sleep(seconds, *, should_stop=None, tick=0.2):
        cancellation._raise_if_stopped(should_stop)

    monkeypatch.setattr(dom, "cancellable_sleep", instant_sleep)
    monkeypatch.setattr("core.registration.steps.cancellable_sleep", instant_sleep)
    # conftest 在 socket 层就拦了外呼，这里换掉是为了让成功路径能跑完
    monkeypatch.setattr(dom, "verify_api_key", lambda api_key, timeout=30: True)
    for name in (
        "SIGNUP_FORM_TIMEOUT",
        "SIGNUP_FORM_RETRY_TIMEOUT",
        "PASSWORD_PAGE_TIMEOUT",
        "POST_SIGNUP_TIMEOUT",
        "RECOVER_PROBE_TIMEOUT",
        "API_KEY_TIMEOUT",
    ):
        monkeypatch.setattr(steps, name, 0.05)


class TestTavilyFlowThroughEngine:
    def test_happy_path_with_the_otp_page(self):
        page = ScriptedPage()

        result, _ = run_tavily_flow(page)

        assert result == {"email": "user@example.com", "password": "Pw-123456", "api_key": API_KEY}
        assert page.filled[dom.OTP_INPUT_SELECTOR] == "123456"
        assert page.filled[dom.PASSWORD_INPUT_SELECTOR] == "Pw-123456"

    def test_happy_path_without_the_otp_page(self):
        """Tavily 按投放二选一：有的账号直接给密码页。"""
        page = ScriptedPage(script=("signin", "signup", "password", "app"))

        result, _ = run_tavily_flow(page)

        assert result["api_key"] == API_KEY
        assert dom.OTP_INPUT_SELECTOR not in page.filled

    def test_the_verify_link_step_runs_only_when_the_site_asks(self):
        page = ScriptedPage(script=("signin", "signup", "password", "verify", "app"))
        seen = []

        result, _ = run_tavily_flow(page, link_callback=lambda **kw: seen.append(1) or f"{dom.APP_URL}/x")

        assert result["api_key"] == API_KEY
        assert seen == [1]

    def test_the_verify_link_step_is_skipped_otherwise(self):
        page = ScriptedPage()
        seen = []

        run_tavily_flow(page, link_callback=lambda **kw: seen.append(1) or "x")

        assert seen == []

    def test_result_has_exactly_the_keys_the_plugin_maps(self):
        from platforms.tavily.plugin import TavilyPlatform

        result, _ = run_tavily_flow(ScriptedPage())
        assert set(result) == {"email", "password", "api_key"}

        mapped = TavilyPlatform()._map_tavily_result(result)
        assert mapped.extra["api_key"] == API_KEY

    def test_the_signup_url_is_read_from_the_signin_page(self):
        """入口 URL 带一次性 state，写死不了。"""
        page = ScriptedPage()

        run_tavily_flow(page)

        assert any(SIGNUP_HREF in url for url in page.goto_urls)


class TestTavilyTurnstile:
    def test_the_token_is_solved_and_injected_before_submitting(self):
        page = ScriptedPage()
        solver = FakeSolver(token="tok_1")

        run_tavily_flow(page, solver=solver)

        assert page.injected_tokens[0] == "tok_1"
        assert solver.calls[0][1] == "site_from_dom"

    def test_a_missing_captcha_channel_is_a_config_error(self):
        """Turnstile 是注册页的必填字段，不是偶发干扰——没有打码渠道就走不下去。"""
        with pytest.raises(ConfigError, match="打码渠道"):
            run_tavily_flow(ScriptedPage(), solver=None)

    def test_a_solver_failure_is_a_provider_error(self):
        solver = FakeSolver(raises=RuntimeError("balance exhausted"))

        with pytest.raises(ProviderError, match="balance exhausted"):
            run_tavily_flow(ScriptedPage(), solver=solver)

    def test_an_empty_token_is_a_provider_error(self):
        with pytest.raises(ProviderError, match="空 token"):
            run_tavily_flow(ScriptedPage(), solver=FakeSolver(token=""))

    def test_the_password_page_challenge_is_re_solved_on_retry(self):
        """密码页提交后没跳转 → 引擎重试 → 这一轮先补 Turnstile 再交。"""
        page = ScriptedPage(
            script=("signin", "signup", "password", "app"),
            turnstile_state={"hasCaptchaDiv": True, "sitekey": "pwd_sitekey"},
        )
        solver = FakeSolver(token="tok_pwd")

        run_tavily_flow(page, solver=solver)

        # 注册页一次 + 密码页一次
        assert len(solver.calls) >= 2
        assert solver.calls[-1][1] == "pwd_sitekey"

    def test_the_sitekey_falls_back_to_the_hardcoded_one(self):
        page = ScriptedPage()
        page.evaluate = lambda script, arg=None: (
            page.injected_tokens.append(arg) or True if "ensureField" in script else ""
        )

        assert dom.get_turnstile_sitekey(page) == dom.TURNSTILE_SITEKEY


class TestTavilyErrorClassification:
    """旧实现这些分支全是裸 RuntimeError，classify() 认作 unknown，一次都不重试。"""

    def test_org_only_signup_is_fatal_not_retryable(self):
        """换多少个邮箱都没用，得改走 OAuth——所以是 ConfigError（致命、停批）。"""
        page = ScriptedPage(
            script=("signin", "signup"),
            body_text="Email/password signup is only available for specific organizations",
        )

        with pytest.raises(ConfigError, match="禁用普通邮箱密码注册"):
            run_tavily_flow(page)

    def test_a_missing_signup_entry_is_a_site_change(self):
        page = ScriptedPage()
        page.STAGES = dict(ScriptedPage.STAGES)
        page.STAGES["signin"] = {"url": dom.SIGNIN_URL, "inputs": (), "html": "<html>no link</html>"}

        with pytest.raises(SiteChangedError, match="未在 Tavily 登录页找到注册入口"):
            run_tavily_flow(page)

    def test_a_missing_email_input_is_a_site_change(self):
        page = ScriptedPage()
        page.STAGES = dict(ScriptedPage.STAGES)
        page.STAGES["signup"] = {"url": f"{dom.AUTH_URL}{SIGNUP_HREF}", "inputs": ()}

        with pytest.raises(SiteChangedError, match="未找到邮箱输入框"):
            run_tavily_flow(page)

    def test_never_reaching_the_code_or_password_page_is_transient(self):
        page = ScriptedPage(script=("signin", "signup"), feedback="Something went wrong")

        with pytest.raises(TransientError, match="Something went wrong"):
            run_tavily_flow(page)

    def test_missing_mailbox_provider_for_the_code_is_a_config_error(self):
        with pytest.raises(ConfigError, match="mailbox provider"):
            run_tavily_flow(ScriptedPage(), otp_callback=None)

    def test_empty_code_is_transient(self):
        with pytest.raises(TransientError, match="未获取到 Tavily 邮箱验证码"):
            run_tavily_flow(ScriptedPage(), otp_callback=lambda **kw: "")

    def test_empty_verify_link_is_transient(self):
        page = ScriptedPage(script=("signin", "signup", "password", "verify", "app"))

        with pytest.raises(TransientError, match="未获取到 Tavily 验证链接"):
            run_tavily_flow(page, link_callback=lambda **kw: "")

    def test_a_missing_api_key_is_transient(self):
        page = ScriptedPage(api_key_html="<html>nothing here</html>")

        with pytest.raises(TransientError, match="未找到 Tavily API Key"):
            run_tavily_flow(page)

    def test_a_dead_api_key_is_transient(self, monkeypatch):
        monkeypatch.setattr(dom, "verify_api_key", lambda api_key, timeout=30: False)

        with pytest.raises(TransientError, match="校验失败"):
            run_tavily_flow(ScriptedPage())

    def test_missing_email_is_a_config_error(self):
        with pytest.raises(ConfigError, match="邮箱地址"):
            run_tavily_flow(ScriptedPage(), state={"email": "", "password": "Pw-1"})


class TestTavilyApiKeyExtraction:
    @staticmethod
    def _page(html):
        return SimpleNamespace(content=lambda: html)

    def test_the_placeholder_key_in_the_docs_is_ignored(self):
        """控制台页面上印着一份示例代码，里面的占位 key 不能当成账号的 key。"""
        page = self._page(f"<html>{dom.API_KEY_PLACEHOLDER}</html>")

        assert dom.extract_api_key(page) is None

    def test_the_longest_match_wins(self):
        page = self._page("<html>tvly-aaaaaaaaaaaaaaaaaaaaaa tvly-bbbbbbbbbbbbbbbbbbbbbbbbbbbb</html>")

        assert dom.extract_api_key(page) == "tvly-bbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    def test_a_page_without_any_key_yields_nothing(self):
        assert dom.extract_api_key(self._page("<html>nothing</html>")) is None


class TestTavilyCancellation:
    def test_cancel_while_waiting_for_the_code_aborts_before_the_password(self):
        page = ScriptedPage()
        token = CancellationToken()

        def slow_otp(**kwargs):
            token.trip("用户停止")
            return "123456"

        with pytest.raises(AttemptCancelled):
            run_tavily_flow(page, cancel=token, otp_callback=slow_otp)

        assert dom.PASSWORD_INPUT_SELECTOR not in page.filled

    def test_the_cancel_token_reaches_both_mail_callbacks(self):
        page = ScriptedPage(script=("signin", "signup", "otp", "password", "verify", "app"))
        seen = {"otp": {}, "link": {}}

        def otp(**kwargs):
            seen["otp"].update(kwargs)
            return "123456"

        def link(**kwargs):
            seen["link"].update(kwargs)
            return f"{dom.APP_URL}/x"

        run_tavily_flow(page, cancel=CancellationToken(), otp_callback=otp, link_callback=link)

        assert "should_stop" in seen["otp"]
        assert "should_stop" in seen["link"]


class TestTavilyFlowShape:
    def test_only_the_navigation_step_is_an_anchor(self):
        flow = steps.build_flow()
        flow.validate()
        assert flow.anchors() == ["OpenSignupPage"]

    def test_captcha_stays_required_while_the_mail_steps_are_conditional(self):
        """Turnstile 每次都要，验证码页与验证链接是站点二选一给的。

        otp / link 从旧路径的 required 降成 conditional：两者都从 mailbox 渠道取，
        而 mailbox 本身仍是 required，所以"任务能不能建"没变。
        """
        from core.requirements import LEVEL_CONDITIONAL, LEVEL_REQUIRED, from_step_flow

        levels = {item.capability: item.level for item in from_step_flow(steps.build_flow(), [])}

        assert levels == {
            "captcha": LEVEL_REQUIRED,
            "otp": LEVEL_CONDITIONAL,
            "link": LEVEL_CONDITIONAL,
        }


class TestTavilyAdapterWiring:
    def test_browser_adapter_uses_the_step_flow_and_keeps_the_preflight(self):
        from platforms.tavily.plugin import TavilyPlatform

        adapter = TavilyPlatform().build_browser_registration_adapter()

        assert adapter.step_flow_builder is not None
        assert adapter.browser_worker_builder is None
        assert adapter.engine_preference == "camoufox"
        assert adapter.allow_engine_fallback is False
        assert adapter.step_flow_builder().name == "tavily.browser_signup"
        # 本地打码器要先起进程，这个钩子不能在迁移里丢掉
        assert adapter.preflight is not None
        assert adapter.use_captcha_for_mailbox is True


class TestTavilyOAuthStillImports:
    def test_oauth_helpers_moved_to_dom_without_breaking_the_oauth_path(self):
        """browser_oauth 原先从 browser_register import 这 6 个名字。"""
        from platforms.tavily import browser_oauth

        for name in (
            "click_oauth_provider",
            "close_marketing_dialog",
            "extract_signup_url",
            "verify_api_key",
            "wait_for_api_key",
            "wait_for_manual_oauth_completion",
        ):
            assert hasattr(dom, name), name
        assert browser_oauth.extract_signup_url is dom.extract_signup_url
