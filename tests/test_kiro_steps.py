"""Kiro 步骤流：AWS Builder ID 的多域跳转 + hash 驱动的注册 SPA。

这里不碰网络也不起浏览器——用一个按"URL 脚本"推进的假页面驱动真实的
``build_flow()``，所以测的是引擎与步骤之间的契约，不是选择器对不对
（那是 ``drift.py`` 的活）。
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
from platforms.kiro import dom, steps

PROFILE = f"https://{dom.AWS_PROFILE_DOMAIN}/#/signup"
SIGNIN = f"https://us-east-1.{dom.AWS_SIGNIN_DOMAIN}.amazon.com/signin"

#: 一次正常的新账号注册会经过的 URL
NEW_ACCOUNT_SCRIPT = (
    dom.KIRO_SIGNIN_URL,
    SIGNIN,
    f"{PROFILE}/{dom.STEP_ENTER_EMAIL}",
    f"{PROFILE}/{dom.STEP_VERIFY_EMAIL}",
    f"{PROFILE}/{dom.STEP_CREATE_PASSWORD}",
    f"{dom.KIRO_URL}/",
)


class FakeElement:
    def __init__(self, *, page=None, selector="", value="", visible=True):
        self.page = page
        self.selector = selector
        self._value = value
        self._visible = visible
        self.clicks = 0

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return True

    def input_value(self):
        return self._value

    def fill(self, value):
        self._value = value
        if self.page is not None:
            self.page.filled[self.selector] = value

    def click(self, timeout=None, force=False):
        self.clicks += 1


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
        self.page.click_text(self.text)


class ScriptedPage:
    """按一串 URL 前进的假页面。

    每次"点下一步"就走到脚本的下一个 URL；输入框按当前 URL 决定该有哪些。
    选择器精确匹配——AWS 的候选列表里 ``input[type="email"]`` 和
    ``input[name="email"]`` 是两个不同的东西，松匹配会把命中顺序测糊。
    """

    def __init__(self, *, script=NEW_ACCOUNT_SCRIPT, tokens=None, stuck_at=None, stuck_rounds=0):
        self.script = list(script)
        self.index = 0
        self.filled = {}
        self.clicked_texts = []
        self.keys = []
        self.goto_urls = []
        self.visible_texts = dom.BUILDER_ID_TEXTS + dom.SUBMIT_TEXTS
        self.tokens = tokens if tokens is not None else {
            "CognitoIdentityServiceProvider.abc.accessToken": "at_1",
            "CognitoIdentityServiceProvider.abc.refreshToken": "rt_1",
            "CognitoIdentityServiceProvider.abc.idToken": "it_1",
        }
        #: 在某个步骤上"提交后 hash 不变"的次数，用来复刻 AWS 的静默校验失败
        self.stuck_at = stuck_at
        self.stuck_rounds = stuck_rounds
        self._apply()

    # -- 位置 -----------------------------------------------------------
    def _apply(self):
        self.url = self.script[self.index]

    def advance(self):
        step = dom.current_step(self)
        if self.stuck_at and step == self.stuck_at and self.stuck_rounds > 0:
            self.stuck_rounds -= 1
            return
        if self.index < len(self.script) - 1:
            self.index += 1
            self._apply()

    def click_text(self, text):
        self.clicked_texts.append(text)
        if text in dom.SUBMIT_TEXTS:
            self.advance()

    # -- 输入框 ---------------------------------------------------------
    def _selectors_here(self):
        step = dom.current_step(self)
        if dom.is_on_aws_signin(self):
            return {dom.EMAIL_INPUT_SELECTORS[1]: ""}
        if step == dom.STEP_ENTER_EMAIL:
            # AWS 已经把邮箱带过来了，只缺姓名
            return {dom.EMAIL_INPUT_SELECTORS[1]: "prefilled@example.com", dom.NAME_INPUT_SELECTORS[0]: ""}
        if step == dom.STEP_ENTER_NAME:
            return {dom.NAME_INPUT_SELECTORS[0]: ""}
        if step == dom.STEP_VERIFY_EMAIL:
            return {dom.OTP_INPUT_SELECTORS[1]: ""}
        if step == dom.STEP_CREATE_PASSWORD:
            return {dom.PASSWORD_INPUT_SELECTORS[0]: ""}
        return {}

    # -- Page 接口 ------------------------------------------------------
    def goto(self, url, **kwargs):
        self.goto_urls.append(url)
        self.index = 0
        self._apply()

    def query_selector(self, selector):
        here = self._selectors_here()
        if selector in here:
            return FakeElement(page=self, selector=selector, value=here[selector])
        if selector == dom.SUBMIT_BUTTON_SELECTOR:
            return FakeElement(page=self, selector=selector)
        return None

    def query_selector_all(self, selector):
        element = self.query_selector(selector)
        return [element] if element else []

    def locator(self, selector):
        text = selector.split('"')[1] if '"' in selector else selector
        return FakeLocator(self, text)

    def evaluate(self, script, arg=None):
        if "localStorage" in script:
            return dict(self.tokens)
        if "createTreeWalker" in script:
            for text in arg or ():
                if text in self.visible_texts:
                    self.click_text(text)
                    return True
            return False
        if "dispatchEvent" in script:
            self.filled[dom.NAME_INPUT_SELECTORS[0]] = arg
            return True
        return None

    @property
    def keyboard(self):
        page = self

        class Keyboard:
            def press(self, key):
                page.keys.append(key)

        return Keyboard()

    def content(self):
        return "<html><body>kiro</body></html>"


def make_sc(page, *, state=None, builders=None, cancel=None):
    logs = []
    ctx = SimpleNamespace(
        platform_name="kiro",
        platform_display_name="Kiro",
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


def run_kiro_flow(page, *, otp_code="123456", state=None, cancel=None, otp_callback=UNSET):
    callback = (lambda **kw: otp_code) if otp_callback is UNSET else otp_callback
    builders = {"otp": lambda ctx, step, key: callback}
    sc = make_sc(page, state=state, builders=builders, cancel=cancel)
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
        "BUILDER_ID_CLICK_TIMEOUT",
        "AWS_SIGNIN_TIMEOUT",
        "AWS_EMAIL_INPUT_TIMEOUT",
        "AWS_PROFILE_TIMEOUT",
        "NAME_INPUT_TIMEOUT",
        "OTP_INPUT_TIMEOUT",
        "VERIFY_STEP_TIMEOUT",
        "HASH_CHANGE_TIMEOUT",
        "KIRO_RETURN_TIMEOUT",
        "TOKEN_TIMEOUT",
    ):
        monkeypatch.setattr(steps, name, 0.05)


class TestKiroFlowThroughEngine:
    def test_happy_path_for_a_new_account(self):
        page = ScriptedPage()

        result, _ = run_kiro_flow(page)

        assert result["accessToken"] == "at_1"
        assert result["refreshToken"] == "rt_1"
        assert result["idToken"] == "it_1"
        assert result["name"] and " " in result["name"]
        assert page.keys == list("123456")

    def test_result_has_exactly_the_keys_the_plugin_maps(self):
        from platforms.kiro.plugin import KiroPlatform

        result, _ = run_kiro_flow(ScriptedPage())
        assert set(result) == {"email", "password", "name", "accessToken", "refreshToken", "idToken"}

        mapped = KiroPlatform()._map_kiro_result(result)
        assert mapped.token == "at_1"
        assert mapped.extra["refreshToken"] == "rt_1"
        # 旧路径这个键永远是空串：worker 的返回 dict 里根本没有 name
        assert mapped.extra["name"] == result["name"]
        # 旧路径同样返回空串的三项照旧
        assert mapped.extra["sessionToken"] == ""
        assert mapped.extra["clientId"] == ""

    def test_empty_password_is_generated_to_the_aws_policy(self):
        result, _ = run_kiro_flow(ScriptedPage(), state={"email": "user@example.com", "password": ""})

        password = result["password"]
        assert any(c.isupper() for c in password)
        assert any(c.isdigit() for c in password)
        assert any(c.islower() for c in password)
        assert password.endswith("!")

    def test_caller_supplied_password_is_kept(self):
        page = ScriptedPage()

        result, _ = run_kiro_flow(page, state={"email": "user@example.com", "password": "Given-1234!"})

        assert result["password"] == "Given-1234!"
        assert page.filled[dom.PASSWORD_INPUT_SELECTORS[0]] == "Given-1234!"

    def test_the_prefilled_email_is_left_alone(self):
        """AWS 自己带过来的邮箱不该被覆盖——覆盖等于把注册对象换掉了。"""
        page = ScriptedPage()

        run_kiro_flow(page)

        assert dom.EMAIL_INPUT_SELECTORS[1] not in page.filled or (
            page.filled[dom.EMAIL_INPUT_SELECTORS[1]] == "user@example.com"
        )

    def test_the_separate_name_step_is_used_when_aws_asks_for_it(self):
        page = ScriptedPage(
            script=(
                dom.KIRO_SIGNIN_URL,
                SIGNIN,
                f"{PROFILE}/{dom.STEP_ENTER_EMAIL}",
                f"{PROFILE}/{dom.STEP_ENTER_NAME}",
                f"{PROFILE}/{dom.STEP_VERIFY_EMAIL}",
                f"{dom.KIRO_URL}/",
            )
        )

        result, _ = run_kiro_flow(page)

        assert result["accessToken"] == "at_1"
        assert page.filled[dom.NAME_INPUT_SELECTORS[0]] == result["name"]

    def test_an_existing_account_skips_the_signup_spa_entirely(self):
        """这个邮箱 AWS 已经认识：直接登回 kiro，没有注册 SPA 也没有验证码。"""
        page = ScriptedPage(script=(dom.KIRO_SIGNIN_URL, SIGNIN, f"{dom.KIRO_URL}/"))
        seen = []

        result, _ = run_kiro_flow(page, otp_callback=lambda **kw: seen.append(1) or "123456")

        assert result["accessToken"] == "at_1"
        assert seen == [], "老账号不该去邮箱要验证码"


class TestKiroEnterEmailRetry:
    def test_a_silent_validation_failure_is_retried_in_place(self):
        """AWS 校验不过时不报错也不换页，只能靠"提交后 hash 没变"发现。"""
        page = ScriptedPage(stuck_at=dom.STEP_ENTER_EMAIL, stuck_rounds=2)

        result, sc = run_kiro_flow(page)

        assert result["accessToken"] == "at_1"
        assert sc.state["enter_email_attempts"] == 3

    def test_exhausted_retries_become_resource_rejected(self):
        """旧实现这里抛裸 RuntimeError，文案自己写着"邮箱域名可能被拒"却没人能据此换邮箱。"""
        page = ScriptedPage(stuck_at=dom.STEP_ENTER_EMAIL, stuck_rounds=99)

        with pytest.raises(ResourceRejectedError, match="邮箱域名可能在黑名单上"):
            run_kiro_flow(page)

    def test_the_retry_budget_matches_the_old_loop(self):
        page = ScriptedPage(stuck_at=dom.STEP_ENTER_EMAIL, stuck_rounds=99)
        sc = None

        try:
            _, sc = run_kiro_flow(page)
        except ResourceRejectedError:
            pass

        assert steps.ENTER_EMAIL_ATTEMPTS == 5


class TestKiroStalledSteps:
    """AWS 的表单校验失败是静默的：不报错、不换页。

    旧实现对**所有**阶段都有一个 20 秒 stuck 检测器（同一 hash 停留过久就重跑那一步）；
    迁移时这条不能只给 enter-email 留着，否则姓名页 / 密码页一旦静默失败，
    流程会一路跑到后面才以一个既误导又不可重试的错误收场。
    """

    @pytest.mark.parametrize(
        "stuck_at, message",
        [
            (dom.STEP_ENTER_NAME, "enter-name 提交后页面没有前进"),
            (dom.STEP_CREATE_PASSWORD, "create-password 提交后页面没有前进"),
        ],
    )
    def test_a_stalled_stage_is_retried_then_reported_as_transient(self, stuck_at, message):
        page = ScriptedPage(
            script=(
                dom.KIRO_SIGNIN_URL,
                SIGNIN,
                f"{PROFILE}/{dom.STEP_ENTER_EMAIL}",
                f"{PROFILE}/{dom.STEP_ENTER_NAME}",
                f"{PROFILE}/{dom.STEP_VERIFY_EMAIL}",
                f"{PROFILE}/{dom.STEP_CREATE_PASSWORD}",
                f"{dom.KIRO_URL}/",
            ),
            stuck_at=stuck_at,
            stuck_rounds=99,
        )

        with pytest.raises(TransientError, match=message):
            run_kiro_flow(page)

    @pytest.mark.parametrize("stuck_at", [dom.STEP_ENTER_NAME, dom.STEP_CREATE_PASSWORD])
    def test_a_stage_that_recovers_within_the_budget_lets_the_flow_finish(self, stuck_at):
        page = ScriptedPage(
            script=(
                dom.KIRO_SIGNIN_URL,
                SIGNIN,
                f"{PROFILE}/{dom.STEP_ENTER_EMAIL}",
                f"{PROFILE}/{dom.STEP_ENTER_NAME}",
                f"{PROFILE}/{dom.STEP_VERIFY_EMAIL}",
                f"{PROFILE}/{dom.STEP_CREATE_PASSWORD}",
                f"{dom.KIRO_URL}/",
            ),
            stuck_at=stuck_at,
            stuck_rounds=steps.SUBMIT_ATTEMPTS - 1,
        )

        result, _ = run_kiro_flow(page)

        assert result["accessToken"] == "at_1"

    def test_a_password_page_without_any_input_is_not_treated_as_stalled(self):
        """有的投放根本不问密码：那时"hash 没变"是正常的，不能当成提交失败。

        这里单独驱动这一步而不是整条流程：假页面只在"点了提交"时才前进，
        没法模拟"AWS 自己把我们带走"，跑整条流程测的就变成假页面了。
        """
        page = ScriptedPage(script=(f"{PROFILE}/{dom.STEP_CREATE_PASSWORD}",))
        page._selectors_here = lambda: {}
        sc = make_sc(page)
        step = steps.AwsCreatePassword()

        assert step.when(sc) is True
        assert step.run(sc).outcome == "ok"
        assert dom.PASSWORD_INPUT_SELECTORS[0] not in page.filled
        assert page.clicked_texts == [], "一个框都没有就不该去点提交"


class TestKiroErrorClassification:
    """旧实现这些分支全是裸 RuntimeError，classify() 认作 unknown，一次都不重试。"""

    def test_never_reaching_aws_is_a_site_change(self):
        page = ScriptedPage(script=(dom.KIRO_SIGNIN_URL,))

        with pytest.raises(SiteChangedError, match="未跳转到 AWS 登录页"):
            run_kiro_flow(page)

    def test_missing_mailbox_provider_is_a_config_error(self):
        with pytest.raises(ConfigError, match="mailbox provider"):
            run_kiro_flow(ScriptedPage(), otp_callback=None)

    def test_empty_code_is_transient(self):
        with pytest.raises(TransientError, match="未获取到 Kiro 邮箱验证码"):
            run_kiro_flow(ScriptedPage(), otp_callback=lambda **kw: "")

    def test_missing_email_is_a_config_error(self):
        with pytest.raises(ConfigError, match="邮箱地址"):
            run_kiro_flow(ScriptedPage(), state={"email": "", "password": ""})

    def test_never_returning_to_kiro_is_transient(self):
        """SPA 全跑完了却没被送回 app.kiro.dev——账号建没建成无从判断。"""
        page = ScriptedPage(
            script=(
                dom.KIRO_SIGNIN_URL,
                SIGNIN,
                f"{PROFILE}/{dom.STEP_ENTER_EMAIL}",
                f"{PROFILE}/{dom.STEP_VERIFY_EMAIL}",
                f"{PROFILE}/{dom.STEP_CREATE_PASSWORD}",
                f"{PROFILE}/limbo",
            )
        )

        with pytest.raises(TransientError, match="未跳转回应用"):
            run_kiro_flow(page)

    def test_an_empty_token_store_is_a_failure_not_a_dead_account(self):
        """旧实现原样返回空 accessToken，落库是一个 check_valid 必然为 False 的死账号。"""
        page = ScriptedPage(tokens={})

        with pytest.raises(TransientError, match="accessToken"):
            run_kiro_flow(page)


class TestKiroCancellation:
    def test_cancel_while_waiting_for_the_code_aborts_before_the_password_step(self):
        page = ScriptedPage()
        token = CancellationToken()

        def slow_otp(**kwargs):
            token.trip("用户停止")
            return "123456"

        with pytest.raises(AttemptCancelled):
            run_kiro_flow(page, cancel=token, otp_callback=slow_otp)

        assert dom.PASSWORD_INPUT_SELECTORS[0] not in page.filled

    def test_the_cancel_token_reaches_the_otp_callback(self):
        seen = {}

        def otp(**kwargs):
            seen.update(kwargs)
            return "123456"

        run_kiro_flow(ScriptedPage(), cancel=CancellationToken(), otp_callback=otp)

        assert "should_stop" in seen


class TestKiroFlowShape:
    def test_only_the_navigation_step_is_an_anchor(self):
        flow = steps.build_flow()
        flow.validate()
        assert flow.anchors() == ["OpenKiroSignin"]

    def test_the_mailbox_otp_channel_stays_required(self):
        """验证码步骤要是用 when() 当守卫，渠道就会从 required 降成 conditional，
        前端于是不再要求 Kiro 任务配接码邮箱——可新账号注册每次都要验证码。
        """
        from core.requirements import LEVEL_REQUIRED, from_step_flow

        levels = {item.capability: item.level for item in from_step_flow(steps.build_flow(), [])}

        from core.registration.steps import Step

        assert levels == {"otp": LEVEL_REQUIRED}
        # 这就是 from_step_flow 判 conditional 的那一条：重写了 when 就降级
        assert steps.AwsVerifyEmail.when is Step.when
        assert steps.AwsEnterName.when is not Step.when


class TestKiroAdapterWiring:
    def test_browser_adapter_uses_the_step_flow(self):
        from platforms.kiro.plugin import KiroPlatform

        adapter = KiroPlatform().build_browser_registration_adapter()

        assert adapter.step_flow_builder is not None
        assert adapter.browser_worker_builder is None
        assert adapter.engine_preference == "camoufox"
        assert adapter.allow_engine_fallback is False
        assert adapter.step_flow_builder().name == "kiro.browser_signup"


class TestKiroTokenReading:
    def test_token_keys_are_matched_case_insensitively_under_a_random_prefix(self):
        page = ScriptedPage(
            tokens={
                "CognitoIdentityServiceProvider.9x8y.LastAuthUser": "user@example.com",
                "CognitoIdentityServiceProvider.9x8y.AccessToken": "AT",
                "CognitoIdentityServiceProvider.9x8y.IdToken": "IT",
            }
        )

        assert dom.read_cognito_tokens(page) == {"accessToken": "AT", "refreshToken": "", "idToken": "IT"}

    def test_no_access_token_means_nothing_usable(self):
        page = ScriptedPage(tokens={"CognitoIdentityServiceProvider.9x8y.refreshToken": "RT"})

        assert dom.read_cognito_tokens(page) == {}
