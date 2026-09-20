"""Grok 步骤流：接线、错误分类与旧整段式路径修掉的那几处。

这里不碰网络也不起浏览器——用一个按阶段换元素的假页面驱动真实的
``build_flow()`` + ``build_obstacles()``，所以测的是引擎与步骤之间的契约，
不是选择器对不对（那是 ``drift.py`` 的活）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.cancellation import CancellationToken
from core.errors import AttemptCancelled, ConfigError, SiteChangedError, TransientError
from core.registration.steps import CapabilityResolver, StepContext, StepRunner
from platforms.grok import dom, steps


class FakeElement:
    """点击按钮会推进页面阶段——``click_first_visible`` 点的是元素而不是 page。"""

    def __init__(self, *, page=None, selector="", advances=False, visible=True):
        self.page = page
        self.selector = selector
        self.advances = advances
        self._visible = visible
        self.value = ""
        self.clicks = 0
        self.text = ""

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return True

    def fill(self, value):
        self.value = value
        if self.page is not None:
            self.page.filled[self.selector] = value

    def click(self, timeout=None, force=False):
        self.clicks += 1
        if self.page is not None:
            self.page.clicked.append(self.selector)
            if self.advances:
                self.page.advance()

    def inner_text(self):
        return self.text


class FakeContext:
    def __init__(self, cookies=None):
        self._cookies = list(cookies or [])

    def cookies(self):
        return list(self._cookies)

    def set(self, name, value):
        self._cookies = [c for c in self._cookies if c["name"] != name]
        self._cookies.append({"name": name, "value": value})


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def press_sequentially(self, text):
        self.page.sequential[self.selector] = text


class ScriptedPage:
    """按阶段换可见元素的假页面。

    选择器**精确**匹配（不做子串匹配）：grok 的 ``input[name="password"]`` 与
    ``input[type="password"]`` 是两个不同的候选，松匹配会把它们混成一个，
    测出来的命中顺序就不作数了。
    """

    STAGES = {
        "signup": (
            'input[type="email"]',
            'button[type="submit"]',
        ),
        "otp": (
            'input[name="code"]',
            'button:has-text("Confirm email")',
        ),
        "name_password": (
            'input[name="given_name"]',
            'input[name="family_name"]',
            'input[name="password"]',
            'input[type="checkbox"]',
            'button:has-text("Sign up")',
        ),
        "password_only": (
            'input[name="password"]',
            'button:has-text("Sign up")',
        ),
        "name_only": (
            'input[name="given_name"]',
            'input[name="family_name"]',
            'button[type="submit"]',
        ),
        "done": (),
    }

    def __init__(self, *, script=("signup", "otp", "name_password", "done"), sso="sso_token", fill_raises=False):
        self.script = list(script)
        self.stage_index = 0
        self.context = FakeContext()
        self.goto_urls = []
        self.filled = {}
        self.sequential = {}
        self.clicked = []
        self.stages_seen = []
        self.error_text = ""
        self.sso = sso
        self.fill_raises = fill_raises
        self.elements = {}
        self._apply_stage()

    # -- 阶段 -----------------------------------------------------------
    @property
    def stage(self):
        return self.script[self.stage_index]

    def _apply_stage(self):
        self.stages_seen.append(self.stage)
        self.elements = {
            selector: FakeElement(page=self, selector=selector, advances=selector.startswith("button"))
            for selector in self.STAGES[self.stage]
        }
        self.url = f"{dom.ACCOUNTS_URL}/{self.stage}"
        if self.stage == "done" and self.sso:
            self.context.set(dom.SESSION_COOKIE, self.sso)
            self.context.set(dom.SESSION_RW_COOKIE, self.sso + "_rw")

    def advance(self):
        if self.stage_index < len(self.script) - 1:
            self.stage_index += 1
            self._apply_stage()

    # -- Page 接口 ------------------------------------------------------
    def goto(self, url, **kwargs):
        self.goto_urls.append(url)
        self.stage_index = 0
        self._apply_stage()

    def query_selector(self, selector):
        if selector == '[role="alert"]' and self.error_text:
            element = FakeElement()
            element.text = self.error_text
            return element
        return self.elements.get(selector)

    def query_selector_all(self, selector):
        element = self.elements.get(selector)
        return [element] if element else []

    def fill(self, selector, value, force=False):
        if self.fill_raises:
            raise RuntimeError("填不进去")
        self.filled[selector] = value

    def locator(self, selector):
        return FakeLocator(self, selector)

    def click(self, selector, force=False):
        self.clicked.append(selector)
        self.advance()

    def content(self):
        return "<html><body>grok</body></html>"


def make_sc(page, *, state=None, builders=None, cancel=None):
    logs = []
    ctx = SimpleNamespace(
        platform_name="grok",
        platform_display_name="Grok",
        platform=SimpleNamespace(),
        proxy=None,
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


def run_grok_flow(page, *, otp_code="ABC-123", state=None, cancel=None, otp_callback=UNSET):
    """真实 build_flow() + build_obstacles() 走引擎，只替换页面与能力。

    ``otp_callback=None`` 表示"没有配 mailbox provider"，和"没传这个参数"是两件事，
    所以默认值用哨兵而不是 None。
    """
    from platforms.grok.obstacles import build_obstacles

    callback = (lambda **kw: otp_code) if otp_callback is UNSET else otp_callback
    builders = {"otp": lambda ctx, step, key: callback}
    sc = make_sc(page, state=state, builders=builders, cancel=cancel)
    return StepRunner(steps.build_flow(), obstacles=build_obstacles()).run(sc), sc


@pytest.fixture(autouse=True)
def fast_waits(monkeypatch):
    """去掉墙钟等待，但保留取消语义。

    直接把 ``cancellable_sleep`` 换成 no-op 会顺手关掉取消检查，
    那样"等待期间被取消"的测试就假过了。
    """
    from core import cancellation

    def instant_sleep(seconds, *, should_stop=None, tick=0.2):
        cancellation._raise_if_stopped(should_stop)

    monkeypatch.setattr(dom, "cancellable_sleep", instant_sleep)
    monkeypatch.setattr("core.registration.steps.cancellable_sleep", instant_sleep)
    monkeypatch.setattr("core.registration.obstacles.time.sleep", lambda seconds: None)
    monkeypatch.setattr(steps, "OTP_PAGE_TIMEOUT", 0.05)
    monkeypatch.setattr(steps, "NAME_STAGE_TIMEOUT", 0.05)
    monkeypatch.setattr(steps, "PASSWORD_STAGE_TIMEOUT", 0.05)
    monkeypatch.setattr(steps, "SESSION_COOKIE_TIMEOUT", 0.05)


class TestGrokFlowThroughEngine:
    def test_happy_path(self):
        page = ScriptedPage()

        result, sc = run_grok_flow(page)

        assert result["sso"] == "sso_token"
        assert result["sso_rw"] == "sso_token_rw"
        assert result["email"] == "user@example.com"
        assert result["given_name"] and result["family_name"]
        assert page.stages_seen[:1] == ["signup"]
        assert page.stage == "done"

    def test_result_has_exactly_the_keys_the_plugin_maps(self):
        from platforms.grok.plugin import GrokPlatform

        result, _ = run_grok_flow(ScriptedPage())
        assert set(result) == {"email", "password", "sso", "sso_rw", "given_name", "family_name"}

        mapped = GrokPlatform()._map_grok_result(result)
        assert mapped.password == result["password"]
        assert mapped.extra["sso"] == "sso_token"
        # 旧路径这两个键永远是空串：worker 的返回 dict 里根本没有它们
        assert mapped.extra["given_name"] == result["given_name"]

    def test_empty_password_is_generated_not_submitted_blank(self):
        """``_prepare_registration_password`` 为协议路径返回空串，浏览器路径要自己兜底。"""
        page = ScriptedPage()

        result, _ = run_grok_flow(page, state={"email": "user@example.com", "password": ""})

        assert len(result["password"]) >= 12
        assert page.filled['input[name="password"]'] == result["password"]

    def test_caller_supplied_password_is_kept(self):
        page = ScriptedPage()

        result, _ = run_grok_flow(page, state={"email": "user@example.com", "password": "Given-1234"})

        assert result["password"] == "Given-1234"
        assert page.filled['input[name="password"]'] == "Given-1234"

    def test_otp_code_is_stripped_of_hyphens(self):
        page = ScriptedPage()

        run_grok_flow(page, otp_code="ABC-123")

        assert page.filled['input[name="code"]'] == "ABC123"

    def test_otp_falls_back_to_typing_when_fill_is_refused(self):
        """分格 OTP 控件的真实 input 常被覆盖层挡住，force fill 也会抛。

        只有验证码走 ``page.fill(force=True)``，其余输入框走元素的 fill，
        所以这里只有验证码那一步会退化成逐字符敲，流程照常走完。
        """
        page = ScriptedPage(fill_raises=True)

        result, _ = run_grok_flow(page, otp_code="ABC-123")

        assert page.sequential['input[name="code"]'] == "ABC123"
        assert result["sso"] == "sso_token"

    def test_name_stage_is_skipped_when_only_password_is_asked(self):
        page = ScriptedPage(script=("signup", "otp", "password_only", "done"))

        result, _ = run_grok_flow(page)

        assert result["sso"] == "sso_token"
        assert 'input[name="given_name"]' not in page.filled
        # 姓名仍然进结果：锚点步骤生成，不依赖页面问不问
        assert result["given_name"]

    def test_password_stage_is_skipped_when_only_the_name_is_asked(self):
        page = ScriptedPage(script=("signup", "otp", "name_only", "done"))

        result, _ = run_grok_flow(page)

        assert result["sso"] == "sso_token"
        assert page.filled['input[name="given_name"]'] == result["given_name"]
        assert 'input[name="password"]' not in page.filled

    def test_name_and_password_on_one_page_are_submitted_together(self):
        """两样同页时姓名步骤不能先点下一步，否则密码丢了。"""
        page = ScriptedPage()

        run_grok_flow(page)

        # 只点了一次提交：密码步骤那一次
        assert page.clicked.count('button:has-text("Sign up")') == 1


class TestGrokErrorClassification:
    """旧实现这些分支全是裸 RuntimeError，classify() 认作 unknown，一次都不重试。"""

    def test_page_feedback_becomes_transient_so_the_task_layer_retries(self):
        page = ScriptedPage(script=("signup", "signup", "done"))
        page.error_text = "This email is already registered"

        with pytest.raises(TransientError, match="already registered"):
            run_grok_flow(page)

    def test_silent_missing_otp_page_is_a_site_change(self):
        page = ScriptedPage(script=("signup", "signup", "done"))

        with pytest.raises(SiteChangedError, match="未进入验证码页面"):
            run_grok_flow(page)

    def test_missing_mailbox_provider_is_a_config_error(self):
        page = ScriptedPage()

        with pytest.raises(ConfigError, match="mailbox provider"):
            run_grok_flow(page, otp_callback=None)

    def test_empty_code_is_transient(self):
        page = ScriptedPage()

        with pytest.raises(TransientError, match="未获取到 Grok 邮箱验证码"):
            run_grok_flow(page, otp_callback=lambda **kw: "")

    def test_missing_session_cookie_is_transient_and_carries_the_page_error(self):
        page = ScriptedPage(sso="")
        page.error_text = "Something went wrong"

        with pytest.raises(TransientError, match="Something went wrong"):
            run_grok_flow(page)

    def test_missing_email_is_a_config_error(self):
        page = ScriptedPage()

        with pytest.raises(ConfigError, match="邮箱地址"):
            run_grok_flow(page, state={"email": "", "password": ""})


class TestGrokCancellation:
    def test_cancel_while_waiting_for_the_code_aborts_without_touching_cookies(self):
        page = ScriptedPage()
        token = CancellationToken()

        def slow_otp(**kwargs):
            token.trip("用户停止")
            return "ABC-123"

        with pytest.raises(AttemptCancelled):
            run_grok_flow(page, cancel=token, otp_callback=slow_otp)

        assert 'input[name="password"]' not in page.filled

    def test_the_cancel_token_reaches_the_otp_callback(self):
        page = ScriptedPage()
        seen = {}

        def otp(**kwargs):
            seen.update(kwargs)
            return "ABC-123"

        run_grok_flow(page, cancel=CancellationToken(), otp_callback=otp)

        assert "should_stop" in seen


class TestGrokFlowShape:
    def test_only_the_navigation_step_is_an_anchor(self):
        flow = steps.build_flow()
        flow.validate()
        assert flow.anchors() == ["OpenSignupPage"]

    def test_requirements_are_otp_only(self):
        from core.requirements import LEVEL_REQUIRED, from_step_flow
        from platforms.grok.obstacles import build_obstacles

        levels = {item.capability: item.level for item in from_step_flow(steps.build_flow(), build_obstacles())}

        assert levels == {"otp": LEVEL_REQUIRED}
        # 全页拦截 handler 不向 CapabilityResolver 要 captcha，
        # 否则前端会凭空要求每个 Grok 任务配打码服务
        assert "captcha" not in levels

    def test_password_survives_a_rewind(self):
        """password 声明成读改写，回撤后不能换一个新的（站点可能已经用旧的建好号了）。"""
        step = steps.OpenSignupPage()
        assert "password" in step.reads and "password" in step.writes


class TestGrokAdapterWiring:
    def test_browser_adapter_uses_the_step_flow(self):
        from platforms.grok.plugin import GrokPlatform

        adapter = GrokPlatform().build_browser_registration_adapter()

        assert adapter.step_flow_builder is not None
        assert adapter.obstacle_builder is not None
        assert adapter.browser_worker_builder is None
        assert adapter.engine_preference == "camoufox"
        assert adapter.allow_engine_fallback is False
        assert adapter.step_flow_builder().name == "grok.browser_signup"


class TestGrokObstacles:
    def test_full_block_only_when_the_challenge_replaced_the_form(self):
        from platforms.grok.obstacles import _is_cf_full_block

        blocked = SimpleNamespace(
            content=lambda: "<html>Just a moment...</html>",
            query_selector=lambda selector: None,
        )
        widget = SimpleNamespace(
            content=lambda: "<html>Just a moment...</html>",
            query_selector=lambda selector: object(),
        )
        normal = SimpleNamespace(
            content=lambda: "<html>sign up</html>",
            query_selector=lambda selector: object(),
        )

        assert _is_cf_full_block(blocked) is True
        assert _is_cf_full_block(widget) is False
        assert _is_cf_full_block(normal) is False
