"""Trae 步骤流：接线、send_code 响应判定与旧整段式路径修掉的那几处。

这里不碰网络也不起浏览器——用一个按阶段换元素的假页面驱动真实的 ``build_flow()``，
所以测的是引擎与步骤之间的契约，不是选择器对不对（那是 ``drift.py`` 的活）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.cancellation import CancellationToken
from core.errors import (
    AttemptCancelled,
    ConfigError,
    ResourceRejectedError,
    SiteChangedError,
    TransientError,
)
from core.registration.steps import CapabilityResolver, StepContext, StepRunner
from platforms.trae import dom, steps


class FakeElement:
    def __init__(self, *, page=None, selector="", visible=True):
        self.page = page
        self.selector = selector
        self._visible = visible
        self.clicks = 0

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return True

    def click(self, timeout=None, force=False):
        self.clicks += 1

    def fill(self, value):
        if self.page is not None:
            self.page.filled[self.selector] = value


class FakeResponse:
    """Playwright Response 的最小替身：只用到 ``url`` 和 ``json()``。"""

    def __init__(self, body, url=None):
        self.url = url or f"https://{dom.TRAE_PASSPORT_DOMAIN}{dom.SEND_CODE_PATH}"
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeContext:
    def __init__(self, cookies=None):
        self._cookies = list(cookies or [])

    def cookies(self):
        return list(self._cookies)


class FakeLocator:
    def __init__(self, page, text):
        self.page = page
        self.text = text

    @property
    def last(self):
        return self

    def is_visible(self):
        return self.text in self.page.visible_texts

    def click(self):
        self.page.click_leaf(self.text)


class ScriptedPage:
    """按阶段换可见输入框与可点文本的假页面。

    选择器精确匹配：``input[placeholder="Password"]`` 与 ``input[type="password"]``
    是两个不同的候选，松匹配会把命中顺序测糊。
    """

    STAGES = {
        "email": {
            "inputs": ('input[placeholder="Email"]',),
            "texts": (dom.SEND_CODE_TEXT,),
        },
        "otp": {
            "inputs": ('input[placeholder="Verification code"]', 'input[placeholder="Password"]'),
            "texts": ("Sign Up",),
        },
        "done": {"inputs": (), "texts": ()},
    }

    def __init__(
        self,
        *,
        script=("email", "otp", "done"),
        token="jwt.eyJkYXRhIjogeyJpZCI6ICI5OSJ9fQ.sig",
        leaf_click_works=True,
    ):
        self.script = list(script)
        self.stage_index = 0
        self.context = FakeContext()
        self.goto_urls = []
        self.filled = {}
        self.clicked_texts = []
        self.keys = []
        self.listeners = []
        self.evaluated = []
        self.token = token
        self.leaf_click_works = leaf_click_works
        self.elements = {}
        self.visible_texts = ()
        self._apply_stage()

    # -- 阶段 -----------------------------------------------------------
    @property
    def stage(self):
        return self.script[self.stage_index]

    def _apply_stage(self):
        spec = self.STAGES[self.stage]
        self.elements = {
            selector: FakeElement(page=self, selector=selector) for selector in spec["inputs"]
        }
        self.visible_texts = spec["texts"]
        self.url = f"{dom.TRAE_URL}/sign-up" if self.stage != "done" else f"{dom.TRAE_URL}/ide"

    def advance(self):
        if self.stage_index < len(self.script) - 1:
            self.stage_index += 1
            self._apply_stage()

    def click_leaf(self, text):
        self.clicked_texts.append(text)
        if text in dom.SIGNUP_TEXTS or text == dom.SEND_CODE_TEXT:
            self.advance()

    # -- Page 接口 ------------------------------------------------------
    def on(self, event, handler):
        self.listeners.append((event, handler))

    def emit_response(self, response):
        for event, handler in self.listeners:
            if event == "response":
                handler(response)

    def goto(self, url, **kwargs):
        self.goto_urls.append(url)
        self.stage_index = 0
        self._apply_stage()

    def query_selector(self, selector):
        return self.elements.get(selector)

    def locator(self, selector):
        text = selector.split('"')[1] if '"' in selector else selector
        return FakeLocator(self, text)

    def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))
        if "querySelectorAll" in script:
            if not self.leaf_click_works:
                return False
            for text in arg or ():
                if text in self.visible_texts:
                    self.click_leaf(text)
                    return True
            return False
        if "GetUserToken" in script:
            return {"Result": {"Token": self.token}}
        if "CheckLogin" in script:
            return {"Result": {"UserId": "u_42", "Region": "sg"}}
        return None

    @property
    def keyboard(self):
        page = self

        class Keyboard:
            def press(self, key):
                page.keys.append(key)

        return Keyboard()

    def content(self):
        return "<html><body>trae</body></html>"


def make_sc(page, *, state=None, builders=None, cancel=None):
    logs = []
    ctx = SimpleNamespace(
        platform_name="trae",
        platform_display_name="Trae.ai",
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

SUCCESS_RESPONSE = {"message": "success"}


def run_trae_flow(page, *, otp_code="123456", state=None, cancel=None, otp_callback=UNSET, send_code=SUCCESS_RESPONSE):
    """真实 build_flow() 走引擎，只替换页面与能力。

    ``send_code`` 是站点对发码请求的应答：``None`` 表示一条都没捕获到。
    """
    callback = (lambda **kw: otp_code) if otp_callback is UNSET else otp_callback
    builders = {"otp": lambda ctx, step, key: callback}
    sc = make_sc(page, state=state, builders=builders, cancel=cancel)

    if send_code is not None:
        # 点 Send Code 时站点回一条响应——真实环境里由 page.on("response") 送达
        original = page.click_leaf

        def click_leaf(text):
            original(text)
            if text == dom.SEND_CODE_TEXT:
                page.emit_response(FakeResponse(send_code))

        page.click_leaf = click_leaf

    return StepRunner(steps.build_flow()).run(sc), sc


@pytest.fixture(autouse=True)
def fast_waits(monkeypatch):
    """去掉墙钟等待，但保留取消语义。"""
    from core import cancellation

    def instant_sleep(seconds, *, should_stop=None, tick=0.2):
        cancellation._raise_if_stopped(should_stop)

    monkeypatch.setattr(dom, "cancellable_sleep", instant_sleep)
    monkeypatch.setattr("core.registration.steps.cancellable_sleep", instant_sleep)
    for name in (
        "EMAIL_STAGE_TIMEOUT",
        "SEND_CODE_CLICK_TIMEOUT",
        "SEND_CODE_RESPONSE_TIMEOUT",
        "OTP_STAGE_TIMEOUT",
        "SIGNUP_CLICK_TIMEOUT",
        "LEAVE_SIGNUP_TIMEOUT",
    ):
        monkeypatch.setattr(steps, name, 0.05)


class TestTraeFlowThroughEngine:
    def test_happy_path(self):
        page = ScriptedPage()

        result, _ = run_trae_flow(page)

        assert result["token"] == page.token
        assert result["user_id"] == "u_42"
        assert result["region"] == "sg"
        assert result["email"] == "user@example.com"
        assert page.clicked_texts == [dom.SEND_CODE_TEXT, "Sign Up"]
        assert page.filled['input[placeholder="Verification code"]'] == "123456"

    def test_result_has_exactly_the_keys_the_plugin_maps(self):
        from platforms.trae.plugin import TraePlatform

        result, _ = run_trae_flow(ScriptedPage())
        assert set(result) == {"email", "password", "token", "user_id", "region"}

        mapped = TraePlatform()._map_trae_result(result)
        assert mapped.token == result["token"]
        assert mapped.user_id == "u_42"
        assert mapped.region == "sg"
        # 旧实现返回的就是空串，映射靠 .get 兜底
        assert mapped.extra == {"cashier_url": "", "ai_pay_host": "", "final_url": ""}

    def test_empty_password_is_generated_to_trae_policy(self):
        """``_prepare_registration_password`` 为协议路径返回空串，浏览器路径要自己兜底。"""
        page = ScriptedPage()

        result, _ = run_trae_flow(page, state={"email": "user@example.com", "password": ""})

        password = result["password"]
        assert page.filled['input[placeholder="Password"]'] == password
        # 前端校验要大写 + 数字 + 小写 + 符号，协议路径那个 14 位纯随机过不了
        assert any(c.isupper() for c in password)
        assert any(c.isdigit() for c in password)
        assert any(c.islower() for c in password)
        assert password.endswith("!")

    def test_caller_supplied_password_is_kept(self):
        page = ScriptedPage()

        result, _ = run_trae_flow(page, state={"email": "user@example.com", "password": "Given-1234!"})

        assert result["password"] == "Given-1234!"
        assert page.filled['input[placeholder="Password"]'] == "Given-1234!"

    def test_the_response_listener_is_installed_before_navigating(self):
        """发码请求可能在首屏就打出去，监听挂晚了就捕获不到。"""
        page = ScriptedPage()
        order = []
        original_on, original_goto = page.on, page.goto
        page.on = lambda event, handler: (order.append("on"), original_on(event, handler))[1]
        page.goto = lambda url, **kw: (order.append("goto"), original_goto(url, **kw))[1]

        run_trae_flow(page)

        assert order == ["on", "goto"]

    def test_missing_password_field_is_not_fatal(self):
        """Trae 有过"只要验证码"的投放：密码框缺席不该让注册失败。"""
        page = ScriptedPage()
        page.STAGES = dict(ScriptedPage.STAGES)
        page.STAGES["otp"] = {"inputs": ('input[placeholder="Verification code"]',), "texts": ("Sign Up",)}

        result, _ = run_trae_flow(page)

        assert result["token"] == page.token
        assert 'input[placeholder="Password"]' not in page.filled


class TestTraeSendCodeRejection:
    def test_rejected_send_becomes_resource_rejected_not_a_silent_otp_timeout(self):
        page = ScriptedPage()

        with pytest.raises(ResourceRejectedError, match="email has been registered"):
            run_trae_flow(
                page,
                send_code={"message": "error", "data": {"description": "email has been registered"}},
            )

    def test_error_code_is_used_when_there_is_no_description(self):
        page = ScriptedPage()

        with pytest.raises(ResourceRejectedError, match="1057"):
            run_trae_flow(page, send_code={"message": "error", "data": {"error_code": 1057}})

    def test_rejection_is_not_retried_in_place(self):
        """同一个邮箱再点一次发码只会再被拒；重试白烧一轮。"""
        page = ScriptedPage()

        with pytest.raises(ResourceRejectedError):
            run_trae_flow(page, send_code={"message": "error", "data": {"description": "blocked"}})

        assert page.clicked_texts.count(dom.SEND_CODE_TEXT) == 1

    def test_uncaptured_send_is_only_a_warning(self):
        """旧实现在这里也只是记一笔：真没发出去的话下一步等不到验证码框。"""
        page = ScriptedPage()

        result, sc = run_trae_flow(page, send_code=None)

        assert result["token"] == page.token
        assert any("未捕获到 send_code" in line for line in sc.logs)

    def test_unparsable_bodies_are_skipped_until_a_readable_one(self):
        assert dom.send_code_rejection([]) is None
        assert dom.send_code_rejection([FakeResponse(ValueError("not json"))]) is None
        assert dom.send_code_rejection([FakeResponse(SUCCESS_RESPONSE)]) is None
        # 最后一条才算数：重发时前面那条成功的不能盖掉后面被拒的
        assert dom.send_code_rejection(
            [FakeResponse(SUCCESS_RESPONSE), FakeResponse({"message": "rate limited"})]
        ) == "rate limited"


class TestTraeErrorClassification:
    """旧实现这些分支全是裸 RuntimeError，classify() 认作 unknown，一次都不重试。"""

    def test_missing_email_input_is_a_site_change(self):
        page = ScriptedPage(script=("done",))

        with pytest.raises(SiteChangedError, match="未找到 Trae 邮箱输入框"):
            run_trae_flow(page)

    def test_missing_otp_input_is_a_site_change(self):
        page = ScriptedPage(script=("email", "done"))

        with pytest.raises(SiteChangedError, match="未出现 Trae 验证码输入框"):
            run_trae_flow(page)

    def test_missing_mailbox_provider_is_a_config_error(self):
        with pytest.raises(ConfigError, match="mailbox provider"):
            run_trae_flow(ScriptedPage(), otp_callback=None)

    def test_empty_code_is_transient(self):
        with pytest.raises(TransientError, match="未获取到 Trae 邮箱验证码"):
            run_trae_flow(ScriptedPage(), otp_callback=lambda **kw: "")

    def test_missing_email_is_a_config_error(self):
        with pytest.raises(ConfigError, match="邮箱地址"):
            run_trae_flow(ScriptedPage(), state={"email": "", "password": ""})

    def test_missing_token_is_transient_and_retried_once(self):
        page = ScriptedPage(token="")

        with pytest.raises(TransientError, match="未获取到 Trae token"):
            run_trae_flow(page)

        # 旧实现手写的"调一次、睡 5 秒、再调一次"现在是引擎的 RetryPolicy
        get_user_token_calls = [s for s, _ in page.evaluated if "GetUserToken" in s]
        assert len(get_user_token_calls) == 2


class TestTraeLeafTextClicking:
    def test_javascript_fallback_reports_whether_it_actually_clicked(self):
        """旧实现无条件把 JS 兜底算成功，于是键盘兜底永远跑不到。"""
        page = ScriptedPage(leaf_click_works=False)
        page.visible_texts = ()

        clicked = dom.click_leaf_text(page, dom.SEND_CODE_TEXT, log=lambda m: None, timeout=0.05)

        assert clicked is False
        # JS 兜底确实跑过（不是"超时前一次都没试"）
        assert any("querySelectorAll" in script for script, _ in page.evaluated)
        assert page.clicked_texts == []

    def test_keyboard_fallback_runs_when_no_button_can_be_clicked(self):
        page = ScriptedPage(leaf_click_works=False)
        page.STAGES = dict(ScriptedPage.STAGES)
        page.STAGES["email"] = {"inputs": ('input[placeholder="Email"]',), "texts": ()}
        page.goto(dom.SIGNUP_URL)

        with pytest.raises(SiteChangedError):
            # 点不到发送按钮 → Tab+Enter 兜底 → 验证码框不会出现 → 这里失败
            run_trae_flow(page, send_code=None)

        assert page.keys == ["Tab", "Enter"]


class TestTraeCancellation:
    def test_cancel_while_waiting_for_the_code_aborts_before_submitting(self):
        page = ScriptedPage()
        token = CancellationToken()

        def slow_otp(**kwargs):
            token.trip("用户停止")
            return "123456"

        with pytest.raises(AttemptCancelled):
            run_trae_flow(page, cancel=token, otp_callback=slow_otp)

        assert "Sign Up" not in page.clicked_texts

    def test_the_cancel_token_reaches_the_otp_callback(self):
        seen = {}

        def otp(**kwargs):
            seen.update(kwargs)
            return "123456"

        run_trae_flow(ScriptedPage(), cancel=CancellationToken(), otp_callback=otp)

        assert "should_stop" in seen


class TestTraeFlowShape:
    def test_only_the_navigation_step_is_an_anchor(self):
        flow = steps.build_flow()
        flow.validate()
        assert flow.anchors() == ["OpenSignupPage"]

    def test_requirements_are_otp_only(self):
        from core.requirements import LEVEL_REQUIRED, from_step_flow

        levels = {item.capability: item.level for item in from_step_flow(steps.build_flow(), [])}

        assert levels == {"otp": LEVEL_REQUIRED}

    def test_the_response_listener_is_not_installed_twice_on_rewind(self):
        """回撤重跑锚点：监听还是同一个 page 上那一个，但旧响应要清掉。"""
        page = ScriptedPage()
        sc = make_sc(page)
        step = steps.OpenSignupPage()

        step.run(sc)
        sc.handles[steps.SEND_CODE_HANDLE].append(FakeResponse(SUCCESS_RESPONSE))
        step.run(sc)

        assert len(page.listeners) == 1
        assert sc.handles[steps.SEND_CODE_HANDLE] == []


class TestTraeAdapterWiring:
    def test_browser_adapter_uses_the_step_flow(self):
        from platforms.trae.plugin import TraePlatform

        adapter = TraePlatform().build_browser_registration_adapter()

        assert adapter.step_flow_builder is not None
        assert adapter.browser_worker_builder is None
        assert adapter.engine_preference == "camoufox"
        assert adapter.allow_engine_fallback is False
        assert adapter.step_flow_builder().name == "trae.browser_signup"


class TestTraeTokenExtraction:
    def test_user_id_falls_back_to_the_cookie_then_the_jwt_payload(self):
        page = ScriptedPage()
        page.evaluate = lambda script, arg=None: (
            {"Result": {"Token": page.token}} if "GetUserToken" in script else None
        )
        page.context = FakeContext([{"name": "user_id", "value": "from_cookie"}])

        token, user_id, region = dom.get_cloudide_token(page, lambda m: None)

        assert token == page.token
        assert user_id == "from_cookie"
        assert region == ""

    def test_the_jwt_payload_is_the_last_resort(self):
        page = ScriptedPage()
        page.evaluate = lambda script, arg=None: (
            {"Result": {"Token": page.token}} if "GetUserToken" in script else None
        )

        _, user_id, _ = dom.get_cloudide_token(page, lambda m: None)

        assert user_id == "99"
