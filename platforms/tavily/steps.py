"""Tavily 浏览器注册的步骤化流程。

取代旧的整段式 ``TavilyBrowserRegister``：重试、取消、补偿、检查点、失败现场
全部由引擎提供，平台只写步骤。实质变化：

1. **裸 RuntimeError 换成分类错误**。尤其是"Tavily 只对特定组织开放邮箱密码
   注册"那条——旧实现抛裸 ``RuntimeError``，``classify()`` 认作 unknown，
   任务层照样一个邮箱一个邮箱地往里填。现在是 ``ConfigError``（致命、停批），
   因为再换多少邮箱都没用，得改走 OAuth。
2. **Turnstile 是必填字段，不是干扰**。所以它在步骤里（``requires=("captcha",)``），
   不在 obstacles 里——挂成 ObstacleHandler 会把 captcha 渠道降成 conditional。
3. **密码页的三轮恢复循环交给引擎**。旧实现手写
   ``_recover_password_challenge``；现在是 ``RetryPolicy`` + 一个把"挑战没加载
   出来"识别出来的恢复动作。

渠道级别与旧路径的差异（``from_adapter`` → ``from_step_flow``）：otp 与 link 从
required 降成 conditional。这是照着代码走的——站点给的是"验证码页**或**密码页"，
验证链接也只在 ``/verify`` 时才要。两者都从 mailbox 渠道取，而 mailbox 本身仍是
required，所以对"任务能不能建"没有实际影响。

页面动作在 dom.py，这里只做编排。
"""
from __future__ import annotations

from core.errors import ConfigError, ProviderError, SiteChangedError, TransientError
from core.registration.steps import Flow, RetryPolicy, Step, StepContext, StepResult

from . import dom

#: 各阶段的等待窗口，全部照搬旧实现的数字
SIGNUP_FORM_TIMEOUT = 15.0
SIGNUP_FORM_RETRY_TIMEOUT = 20.0
PASSWORD_PAGE_TIMEOUT = 30.0
POST_SIGNUP_TIMEOUT = 15.0
RECOVER_PROBE_TIMEOUT = 5.0
API_KEY_TIMEOUT = 20

#: 提交后给 Auth0 换页的时间
SUBMIT_SETTLE_SECONDS = 6.0
SETTLE_SECONDS = 3.0

#: 密码页安全挑战的恢复轮数，同旧实现的 ``_recover_password_challenge``
PASSWORD_RECOVER_ATTEMPTS = 3

ORG_ONLY_MESSAGE = (
    "Tavily 当前已禁用普通邮箱密码注册，仅允许特定组织使用 Email/password，"
    "普通账号需要改走 Google/GitHub/LinkedIn/Microsoft OAuth。"
)


def _solve_turnstile(sc: StepContext, *, sitekey: str = "") -> str:
    """向 captcha 渠道要一个 Turnstile token。"""
    solver = sc.require("captcha")
    if solver is None:
        raise ConfigError("Tavily 注册页必须过 Turnstile，但未配置打码渠道")
    try:
        return solver.solve_turnstile(dom.str_url(sc.page), sitekey or dom.TURNSTILE_SITEKEY) or ""
    except Exception as exc:
        raise ProviderError(f"Turnstile 求解失败: {exc}") from exc


def _raise_if_org_only(sc: StepContext) -> None:
    message = dom.detect_org_only_signup_message(sc.page)
    if message:
        # 致命：换邮箱没用，得改身份方式。旧实现抛裸 RuntimeError，
        # 任务层会继续一个一个邮箱地喂进来。
        raise ConfigError(f"{ORG_ONLY_MESSAGE} 页面原话: {message}")


class OpenSignupPage(Step):
    """从 sign-in 页里抠出带 state 的注册入口，然后进去。

    anchor：本步骤从零建立页面状态，因此可以作为回撤目标。
    """

    name = "OpenSignupPage"
    anchor = True

    def run(self, sc: StepContext) -> StepResult:
        sc.page.goto(dom.SIGNIN_URL, wait_until="networkidle", timeout=30000)
        sc.sleep(2)

        signup_url = dom.extract_signup_url(sc.page.content())
        if not signup_url:
            raise SiteChangedError(f"未在 Tavily 登录页找到注册入口: {dom.str_url(sc.page)}")

        sc.log("进入 Tavily 注册页")
        sc.page.goto(signup_url, wait_until="networkidle", timeout=30000)
        sc.sleep(2)
        return StepResult.ok()


class SubmitEmailWithCaptcha(Step):
    """填邮箱、过 Turnstile、提交，等站点给出验证码页或密码页。

    Turnstile 在这里是**必填字段**而不是偶发干扰：不带 token 提交，Auth0 直接
    退回原页且不报错。所以 captcha 写进 ``requires``，渠道级别是 required。
    """

    name = "SubmitEmailWithCaptcha"
    requires = ("captcha",)
    reads = ("email",)
    retry = RetryPolicy(max_attempts=2, categories=("transient", "provider"))

    def run(self, sc: StepContext) -> StepResult:
        email = str(sc.state.get("email") or "")
        if not email:
            raise ConfigError("Tavily 注册需要邮箱地址，但身份里没有")

        email_selector = dom.fill_first_input(sc.page, dom.EMAIL_INPUT_SELECTORS, email)
        if not email_selector:
            raise SiteChangedError(f"Tavily 注册页未找到邮箱输入框: {dom.str_url(sc.page)}")

        sc.log("处理注册页 Turnstile")
        token = _solve_turnstile(sc, sitekey=dom.get_turnstile_sitekey(sc.page))
        if not token:
            raise ProviderError("注册页 Turnstile 求解返回空 token")
        dom.inject_turnstile_token(sc.page, token)

        dom.submit_primary_action(sc.page, email_selector)
        sc.sleep(SUBMIT_SETTLE_SECONDS)

        if self._wait_for_form(sc, SIGNUP_FORM_TIMEOUT):
            return StepResult.ok()

        # Auth0 偶尔要多点一次主按钮才真的提交
        _raise_if_org_only(sc)
        dom.submit_primary_action(sc.page)
        sc.sleep(SETTLE_SECONDS)
        if self._wait_for_form(sc, SIGNUP_FORM_RETRY_TIMEOUT):
            return StepResult.ok()

        _raise_if_org_only(sc)
        feedback = dom.extract_page_feedback(sc.page)
        raise TransientError(f"未进入验证码/密码页面: {feedback or dom.str_url(sc.page)}")

    @staticmethod
    def _wait_for_form(sc: StepContext, timeout: float) -> bool:
        return bool(
            sc.poll(
                lambda: sc.page.query_selector(dom.OTP_OR_PASSWORD_SELECTOR) is not None,
                timeout=timeout,
                interval=0.5,
            )
        )


class MaybeEmailOtp(Step):
    """验证码页。Tavily 按投放二选一：要么先要验证码，要么直接给密码页。"""

    name = "MaybeEmailOtp"
    requires = ("otp",)
    retry = RetryPolicy(max_attempts=1)

    def when(self, sc: StepContext) -> bool:
        return sc.page.query_selector(dom.OTP_INPUT_SELECTOR) is not None

    def run(self, sc: StepContext) -> StepResult:
        otp_callback = sc.require("otp")
        if otp_callback is None:
            raise ConfigError("当前流程需要邮箱验证码，但未配置 mailbox provider")

        sc.log("等待邮箱验证码")
        code = sc.call_with_cancel(otp_callback)
        if not code:
            raise TransientError("未获取到 Tavily 邮箱验证码")

        sc.page.fill(dom.OTP_INPUT_SELECTOR, code)
        dom.submit_primary_action(sc.page, dom.OTP_INPUT_SELECTOR)
        sc.sleep(SETTLE_SECONDS)
        return StepResult.ok()


class SetPassword(Step):
    """密码页。提交后要么换页，要么就是安全挑战没过——后者靠重试恢复。

    ``requires=("captcha",)``：恢复那一轮要重新求解 Turnstile。
    """

    name = "SetPassword"
    requires = ("captcha",)
    reads = ("password",)
    retry = RetryPolicy(max_attempts=PASSWORD_RECOVER_ATTEMPTS, categories=("transient", "provider"))

    def run(self, sc: StepContext) -> StepResult:
        if dom.is_past_signup(sc.page):
            sc.log("页面已过密码页，跳过设置密码")
            return StepResult.ok()

        password = str(sc.state.get("password") or "")
        if not sc.poll(
            lambda: sc.page.query_selector(dom.PASSWORD_INPUT_SELECTOR) is not None,
            timeout=PASSWORD_PAGE_TIMEOUT,
            interval=0.5,
        ):
            raise SiteChangedError(f"未到达 Tavily 注册密码页: {dom.str_url(sc.page)}")

        sc.log("设置 Tavily 密码")
        self._recover_challenge_if_needed(sc, password)

        if not dom.refill_password(sc.page, password):
            raise SiteChangedError(f"Tavily 密码输入框不可填: {dom.str_url(sc.page)}")

        sc.sleep(1)
        dom.submit_primary_action(sc.page, dom.PASSWORD_INPUT_SELECTOR)
        sc.sleep(5)

        if dom.wait_for_post_signup_target(sc.page, timeout=POST_SIGNUP_TIMEOUT, should_stop=sc.cancel):
            return StepResult.ok()

        feedback = dom.extract_page_feedback(sc.page)
        raise TransientError(f"Tavily 密码提交后没有跳转: {feedback or dom.str_url(sc.page)}")

    def _recover_challenge_if_needed(self, sc: StepContext, password: str) -> None:
        """重试进来时先看一眼密码页上的安全挑战，能补就补。

        旧实现把这段写成一个自带三轮循环的 ``_recover_password_challenge``；
        循环那部分现在是引擎的 ``RetryPolicy``，这里只留"补一次"的动作。
        """
        feedback = dom.extract_page_feedback(sc.page)
        state = dom.collect_turnstile_state(sc.page)
        if not feedback and not dom.has_password_challenge_signal(state=state):
            return

        sc.log(f"密码页 DOM: {dom.format_turnstile_state(state)}")
        if feedback:
            sc.log(f"密码页提示: {feedback}")

        if dom.refresh_password_page_if_needed(sc.page, feedback, state):
            feedback = dom.extract_page_feedback(sc.page)
            state = dom.collect_turnstile_state(sc.page)

        if not dom.has_password_challenge_signal(feedback, state):
            return

        token = _solve_turnstile(sc, sitekey=state.get("sitekey") or dom.get_turnstile_sitekey(sc.page))
        if token and dom.inject_turnstile_token(sc.page, token):
            sc.log("已注入密码页 Turnstile token")


class MaybeVerifyLink(Step):
    """邮件里的验证链接。只有站点把我们停在 ``/verify`` 时才需要。"""

    name = "MaybeVerifyLink"
    requires = ("link",)
    retry = RetryPolicy(max_attempts=1)

    def when(self, sc: StepContext) -> bool:
        return dom.needs_email_verification(sc.page)

    def run(self, sc: StepContext) -> StepResult:
        link_callback = sc.require("link")
        if link_callback is None:
            raise ConfigError("当前流程需要邮件验证链接，但未配置 mailbox provider")

        sc.log("等待 Tavily 验证链接")
        verify_url = sc.call_with_cancel(link_callback)
        if not verify_url:
            raise TransientError("未获取到 Tavily 验证链接")

        sc.page.goto(verify_url, wait_until="networkidle", timeout=60000)
        sc.page.wait_for_url("**/app.tavily.com/**", timeout=60000)
        sc.sleep(SETTLE_SECONDS)
        return StepResult.ok()


class ExtractApiKey(Step):
    """从控制台页面上抠 API Key，并打一次真实搜索确认它活着。"""

    name = "ExtractApiKey"
    writes = ("api_key",)
    retry = RetryPolicy(max_attempts=2, backoff_seconds=3.0, categories=("transient",))

    def run(self, sc: StepContext) -> StepResult:
        sc.log("提取并验证 Tavily API Key")
        timeout = int(sc.ctx.extra.get("api_key_timeout", API_KEY_TIMEOUT) or API_KEY_TIMEOUT)

        dom.close_marketing_dialog(sc.page)
        api_key = dom.wait_for_api_key(sc.page, timeout=timeout, should_stop=sc.cancel)
        if not api_key:
            # 注册完偶尔停在中间页，主动跳一次控制台
            try:
                sc.page.goto(dom.APP_URL, wait_until="networkidle", timeout=30000)
                sc.sleep(SETTLE_SECONDS)
            except Exception:
                pass
            api_key = dom.wait_for_api_key(sc.page, timeout=timeout, should_stop=sc.cancel)

        if not api_key:
            raise TransientError(f"未找到 Tavily API Key: {dom.str_url(sc.page)}")
        if not dom.verify_api_key(api_key):
            raise TransientError("Tavily API Key 校验失败（拿到了但打不通搜索接口）")

        sc.state["api_key"] = api_key
        sc.log(f"注册成功: {sc.state.get('email')}")
        return StepResult.ok()


def build_flow() -> Flow:
    return Flow(
        "tavily.browser_signup",
        [
            OpenSignupPage(),
            SubmitEmailWithCaptcha(),
            MaybeEmailOtp(),
            SetPassword(),
            MaybeVerifyLink(),
            ExtractApiKey(),
        ],
        initial_keys=("email", "password"),
        result_keys=("email", "password", "api_key"),
    )
