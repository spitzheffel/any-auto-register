"""Grok 浏览器注册的步骤化流程。

取代旧的整段式 ``GrokBrowserRegister.run``：重试、取消、补偿、检查点、失败现场
全部由引擎提供，平台只写步骤。相比旧实现的实质变化只有三处：

1. **裸 RuntimeError 换成分类错误**。旧实现里"未进入验证码页面"和
   Playwright 自己的 TimeoutError 都落到 ``classify() == "unknown"``，
   于是一次都不重试；现在站点改版是 ``SiteChangedError``（停批留现场），
   页面自己报的错是 ``TransientError``（换个邮箱补投）。
2. **``/tmp/grok_*.png`` 调试转储删掉**。那几行在 Windows 上直接抛
   ``FileNotFoundError``，而且引擎的失败现场包（截图 + 压缩 HTML + 网络记录）
   本来就覆盖它。
3. **密码为空时自己生成一个**。``_prepare_registration_password`` 为了协议路径
   返回 ``""``（协议 worker 自己会 ``_rand_password()``），浏览器路径拿到空串
   会往密码框里填空值然后一路失败到 cookie 超时。这里补上同一个兜底。

页面动作在 dom.py，这里只做编排。
"""
from __future__ import annotations

from core.errors import ConfigError, SiteChangedError, TransientError
from core.registration.steps import Flow, RetryPolicy, Step, StepContext, StepResult

from . import dom
from .core import _rand_password

#: 提交邮箱后等验证码页出现的窗口（旧实现 20s）
OTP_PAGE_TIMEOUT = 20.0
#: 验证码确认后等"姓名/密码"那一页出现的窗口（旧实现 15 轮 × 1s）
NAME_STAGE_TIMEOUT = 15.0
#: 填完姓名后再等密码框单独出现的窗口（旧实现 10 轮 × 1s）
PASSWORD_STAGE_TIMEOUT = 10.0
#: 提交注册后等 sso cookie 落地的窗口（旧实现 60s）
SESSION_COOKIE_TIMEOUT = 60.0


class OpenSignupPage(Step):
    """打开注册入口，并把这一次尝试要用的身份材料定下来。

    anchor：本步骤从零建立页面状态，因此可以作为回撤目标。姓名和兜底密码在这里
    生成而不是在用到它们的步骤里，是因为 ``result_keys`` 要求它们无论后面哪一步
    被 ``when()`` 跳过都必须在黑板上；回撤重跑时重新随机也是对的（新一轮尝试）。
    """

    name = "OpenSignupPage"
    # password 同时是 reads 和 writes：读到空才生成，回撤时引擎据此保留当前值
    reads = ("password",)
    writes = ("given_name", "family_name", "password")
    anchor = True

    def run(self, sc: StepContext) -> StepResult:
        given, family = dom.random_person_name()
        sc.state["given_name"] = given
        sc.state["family_name"] = family
        if not sc.state.get("password"):
            sc.state["password"] = _rand_password()

        dom.open_signup(sc.page, log=sc.log, should_stop=sc.cancel)
        return StepResult.ok()


class SubmitEmail(Step):
    """填邮箱并提交，站点随后把验证码发到邮箱。"""

    name = "SubmitEmail"
    reads = ("email",)
    retry = RetryPolicy(max_attempts=2, categories=("transient",))

    def run(self, sc: StepContext) -> StepResult:
        email = str(sc.state.get("email") or "")
        if not email:
            raise ConfigError("Grok 注册需要邮箱地址，但身份里没有")
        dom.submit_email(sc.page, email, log=sc.log, should_stop=sc.cancel)
        return StepResult.ok()


class AwaitEmailOtp(Step):
    """等验证码页、取码、填码、确认。

    ``max_attempts=1``：原地重试会再向邮箱要一封**新**邮件，而站点只发过一封，
    于是白等满一个 OTP 超时才失败。取码失败就让这次尝试结束，由任务层换邮箱补投。
    """

    name = "AwaitEmailOtp"
    requires = ("otp",)
    retry = RetryPolicy(max_attempts=1)

    def run(self, sc: StepContext) -> StepResult:
        if dom.wait_for_otp_input(sc.page, timeout=OTP_PAGE_TIMEOUT, should_stop=sc.cancel) is None:
            feedback = dom.extract_error_text(sc.page)
            if feedback:
                # 页面自己说了原因（邮箱已注册 / 域名被拒 / 频率限制），换个邮箱补投
                raise TransientError(f"未进入验证码页面: {feedback}")
            raise SiteChangedError(f"未进入验证码页面: {sc.page.url}")

        otp_callback = sc.require("otp")
        if otp_callback is None:
            raise ConfigError("Grok 注册需要邮箱验证码，但未配置 mailbox provider")

        sc.log("等待 Grok 验证码")
        code = sc.call_with_cancel(otp_callback)
        if not code:
            raise TransientError("未获取到 Grok 邮箱验证码")

        dom.fill_otp_code(sc.page, code)
        dom.confirm_otp(sc.page, should_stop=sc.cancel)

        # 下一页可能只要姓名、只要密码，也可能两样一起给；等它出现，
        # 由后面两步的 when() 分别认领。没等到不算失败（旧实现也只是记一笔），
        # 真正的判定留给 sso cookie。
        if dom.wait_for_name_or_password(sc.page, timeout=NAME_STAGE_TIMEOUT, should_stop=sc.cancel) is None:
            sc.log("未检测到姓名或密码输入框，继续等会话 cookie", level="warning")
        return StepResult.ok()


class MaybeFillName(Step):
    """姓名页：有姓名框才填。

    密码框同页时不点下一步——两样要一起提交。
    """

    name = "MaybeFillName"
    reads = ("given_name", "family_name")

    def when(self, sc: StepContext) -> bool:
        return dom.has_name_input(sc.page)

    def run(self, sc: StepContext) -> StepResult:
        dom.fill_name(sc.page, str(sc.state.get("given_name") or ""), str(sc.state.get("family_name") or ""))
        if not dom.has_password_input(sc.page):
            dom.submit_signup(sc.page, log=sc.log, should_stop=sc.cancel)
        return StepResult.ok()


class MaybeSetPassword(Step):
    """密码页：填密码、勾掉条款复选框、提交。"""

    name = "MaybeSetPassword"
    reads = ("password",)

    def when(self, sc: StepContext) -> bool:
        # 姓名页提交后密码框才渲染出来，所以这里要等而不是只查一次
        return bool(sc.poll(lambda: dom.has_password_input(sc.page), timeout=PASSWORD_STAGE_TIMEOUT, interval=1.0))

    def run(self, sc: StepContext) -> StepResult:
        dom.fill_password(sc.page, str(sc.state.get("password") or ""))
        dom.accept_checkboxes(sc.page)
        dom.submit_signup(sc.page, log=sc.log, should_stop=sc.cancel)
        return StepResult.ok()


class AwaitSessionCookie(Step):
    """等 sso cookie 落地——这是"注册真的成功了"的唯一凭据。

    ``max_attempts=1``：这一步本身就是一个 60s 轮询，再来一轮只是把失败推迟一分钟。
    """

    name = "AwaitSessionCookie"
    writes = ("sso", "sso_rw")
    retry = RetryPolicy(max_attempts=1)

    def run(self, sc: StepContext) -> StepResult:
        sc.log("等待 Grok sso cookie")
        sc.poll(
            lambda: dom.read_session_cookies(sc.page).get(dom.SESSION_COOKIE),
            timeout=SESSION_COOKIE_TIMEOUT,
            interval=1.0,
        )
        cookies = dom.read_session_cookies(sc.page)
        sso = cookies.get(dom.SESSION_COOKIE) or ""
        if not sso:
            feedback = dom.extract_error_text(sc.page)
            suffix = f": {feedback}" if feedback else ""
            raise TransientError(f"未获取到 Grok sso cookie{suffix}")

        sc.state["sso"] = sso
        sc.state["sso_rw"] = cookies.get(dom.SESSION_RW_COOKIE) or ""
        sc.log(f"注册成功: {sc.state.get('email')}")
        return StepResult.ok()


def build_flow() -> Flow:
    return Flow(
        "grok.browser_signup",
        [
            OpenSignupPage(),
            SubmitEmail(),
            AwaitEmailOtp(),
            MaybeFillName(),
            MaybeSetPassword(),
            AwaitSessionCookie(),
        ],
        initial_keys=("email", "password"),
        result_keys=("email", "password", "sso", "sso_rw", "given_name", "family_name"),
    )
