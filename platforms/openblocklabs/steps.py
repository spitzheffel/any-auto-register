"""OpenBlockLabs（WorkOS AuthKit）浏览器注册的步骤化流程。

取代旧的整段式 ``OpenBlockLabsBrowserRegister``：重试、取消、补偿、检查点、
失败现场全部由引擎提供，平台只写步骤。实质变化：

1. **Cloudflare / Turnstile 交给 ObstacleHandler**。旧实现自己写了
   ``_wait_cf_full_block_clear`` / ``_handle_turnstile`` / ``_click_turnstile_in_iframe``
   （和 cursor 那份逐字相同的 150 行），并在流程里手工插了四处调用。现在装配在
   ``obstacles.py``，引擎在每步前与每次失败后自动探测，出现点一个都不会漏。
2. **裸 RuntimeError 换成分类错误**。尤其是"WorkOS 会话初始化失败 / 中间页
   Not Found"——旧实现抛裸 ``RuntimeError``，``classify()`` 认作 unknown，
   一次都不重试。
3. **``/tmp/openblocks_*.png`` 调试转储删掉**。那几行在 Windows 上直接抛
   ``FileNotFoundError``，而且引擎的失败现场包本来就覆盖它。
4. **"打开页面重试两次"的手写循环换成 ``RetryPolicy``**。

页面动作在 dom.py，这里只做编排。
"""
from __future__ import annotations

import time

from core.errors import ConfigError, SiteChangedError, TransientError
from core.registration.steps import Flow, RetryPolicy, Step, StepContext, StepResult

from . import dom

#: 各阶段的等待窗口，全部照搬旧实现的数字
SIGNUP_SESSION_TIMEOUT = 25.0
EMAIL_INPUT_TIMEOUT = 60.0
PASSWORD_INPUT_TIMEOUT = 20.0
PASSWORD_PAGE_URL_TIMEOUT = 20.0
ADVANCE_TIMEOUT = 40.0
VERIFICATION_READY_TIMEOUT = 5.0
DASHBOARD_TIMEOUT = 60.0
WOS_SESSION_TIMEOUT = 15.0

#: 中间页上"再点一次按钮"的最小间隔，同旧实现
ADVANCE_CLICK_INTERVAL = 1.5


class OpenSignupPage(Step):
    """走 WorkOS 的入口拿 authorization_session_id，再带着它进注册页。

    anchor：本步骤从零建立页面状态，因此可以作为回撤目标。
    ``max_attempts=2`` 就是旧实现里那个 ``for attempt in range(2)``。
    """

    name = "OpenSignupPage"
    # password 同时是 reads 和 writes：读到空才生成，回撤时引擎据此保留当前值
    reads = ("password",)
    writes = ("password", "first_name", "last_name", "session_id")
    anchor = True
    retry = RetryPolicy(max_attempts=2, backoff_seconds=2.0, categories=("transient",))

    def run(self, sc: StepContext) -> StepResult:
        if not sc.state.get("password"):
            sc.state["password"] = dom.random_password()
            sc.log("未提供密码，已自动生成随机密码")
        sc.state["first_name"] = dom.random_name()
        sc.state["last_name"] = dom.random_name()

        # 鼠标事件的 screenX/screenY 补丁：Turnstile 的被动检测会看这两个值
        sc.page.add_init_script(dom.MOUSE_EVENT_PATCH)

        sc.log("打开 OpenBlockLabs 注册页")
        sc.page.goto(dom.ENTRY_URL, wait_until="domcontentloaded", timeout=30000)
        dom.wait_cf_full_block_clear_here(sc.page, log_fn=sc.log, should_stop=sc.cancel)
        self._settle(sc)

        session_id = dom.wait_for_signup_session(
            sc.page, timeout=SIGNUP_SESSION_TIMEOUT, should_stop=sc.cancel
        )
        if not session_id:
            raise TransientError(f"等待 authorization_session_id 超时: {dom.page_url(sc.page)}")
        sc.state["session_id"] = session_id

        target = dom.signup_url(session_id)
        if dom.page_url(sc.page) != target:
            sc.page.goto(target, wait_until="domcontentloaded", timeout=30000)
            dom.wait_cf_full_block_clear_here(sc.page, log_fn=sc.log, should_stop=sc.cancel)
            self._settle(sc)
        return StepResult.ok()

    @staticmethod
    def _settle(sc: StepContext) -> None:
        try:
            sc.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        sc.sleep(2)


class FillIdentityForm(Step):
    """填姓名（有就填）和邮箱。"""

    name = "FillIdentityForm"
    reads = ("email", "first_name", "last_name")

    def run(self, sc: StepContext) -> StepResult:
        email = str(sc.state.get("email") or "")
        if not email:
            raise ConfigError("OpenBlockLabs 注册需要邮箱地址，但身份里没有")

        for selector, value in (
            (dom.FIRST_NAME_SELECTOR, str(sc.state.get("first_name") or "")),
            (dom.LAST_NAME_SELECTOR, str(sc.state.get("last_name") or "")),
        ):
            if sc.page.query_selector(selector):
                sc.page.fill(selector, value)

        used, current = dom.fill_visible_input(
            sc.page, dom.EMAIL_INPUT_SELECTORS, email,
            timeout=EMAIL_INPUT_TIMEOUT, should_stop=sc.cancel,
        )
        if not used:
            raise SiteChangedError(f"邮箱输入失败: current={current!r}, url={dom.page_url(sc.page)}")
        sc.log(f"已填写邮箱: {used}")
        return StepResult.ok()


class SubmitSignupForm(Step):
    """填密码并提交。

    WorkOS 按投放走两条路：密码框和邮箱同页（一次提交），或者先提交邮箱、
    再单独给一个 ``/sign-up/password`` 页。两条都在这里，因为它们互斥——
    拆成两个带 ``when()`` 的步骤反而要处理"第一个跑完之后第二个的守卫还成不成立"。
    """

    name = "SubmitSignupForm"
    reads = ("password",)
    retry = RetryPolicy(max_attempts=2, categories=("transient",))

    def run(self, sc: StepContext) -> StepResult:
        password = str(sc.state.get("password") or "")
        inline, _ = dom.find_visible_element(sc.page, dom.PASSWORD_INPUT_SELECTORS)

        if inline is None:
            # 邮箱先单独提交，密码在下一页
            if not dom.click_visible_button(sc.page, dom.SUBMIT_BUTTON_SELECTORS, should_stop=sc.cancel):
                raise SiteChangedError(f"未找到可点的继续按钮: {dom.page_url(sc.page)}")
            sc.log("已点击继续按钮")
            dom.wait_cf_full_block_clear_here(sc.page, timeout=30, log_fn=sc.log, should_stop=sc.cancel)
            try:
                sc.page.wait_for_url(f"**{dom.PASSWORD_URL_MARKER}**", timeout=int(PASSWORD_PAGE_URL_TIMEOUT * 1000))
            except Exception:
                pass

        used, current = dom.fill_visible_input(
            sc.page, dom.PASSWORD_INPUT_SELECTORS, password,
            timeout=PASSWORD_INPUT_TIMEOUT, should_stop=sc.cancel,
        )
        if not used:
            raise SiteChangedError(f"密码输入失败: current_len={len(current)}, url={dom.page_url(sc.page)}")
        sc.log(f"已填写密码: {used}")

        if not dom.click_visible_button(sc.page, dom.SUBMIT_BUTTON_SELECTORS, should_stop=sc.cancel):
            raise SiteChangedError(f"未找到可点的提交按钮: {dom.page_url(sc.page)}")
        sc.log("已点击提交按钮")
        return StepResult.ok()


class AdvanceToEmailVerification(Step):
    """把中间页点过去，直到验证码页出现。

    WorkOS 在提交和验证码页之间可能插一到两个"再点一次继续"的中间页。旧实现在这里
    自己轮询 CF 拦截和 Turnstile；现在那两样归 ObstacleHandler 管——本步骤失败重试前
    引擎会先跑一遍探测，所以这里只负责"看到按钮就再点一下"。
    """

    name = "AdvanceToEmailVerification"
    retry = RetryPolicy(max_attempts=2, categories=("transient",))

    def run(self, sc: StepContext) -> StepResult:
        last_click_at = [0.0]

        def attempt():
            if dom.is_email_verification_ready(sc.page):
                return True
            if dom.is_not_found_page(sc.page):
                raise SiteChangedError(f"中间页返回 Not Found: {dom.page_url(sc.page)}")

            now = time.time()
            if now - last_click_at[0] >= ADVANCE_CLICK_INTERVAL:
                element, used = dom.find_visible_element(
                    sc.page, dom.SUBMIT_BUTTON_SELECTORS, require_enabled=True
                )
                if element is not None:
                    try:
                        element.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    try:
                        element.click(timeout=3000)
                        sc.log(f"检测到中间页，继续点击按钮: {used}")
                        last_click_at[0] = now
                    except Exception:
                        pass
            return None

        if not sc.poll(attempt, timeout=ADVANCE_TIMEOUT, interval=0.5):
            raise TransientError(f"未进入验证码页面: {dom.page_url(sc.page)}")
        return StepResult.ok()


class AwaitEmailOtp(Step):
    """验证码页：取码、敲进分格控件、提交。

    ``max_attempts=1``：原地重试会再向邮箱要一封**新**邮件，而站点只发过一封，
    于是白等满一个 OTP 超时才失败。
    """

    name = "AwaitEmailOtp"
    requires = ("otp",)
    retry = RetryPolicy(max_attempts=1)

    def run(self, sc: StepContext) -> StepResult:
        sc.sleep(2)
        if not sc.poll(
            lambda: dom.is_email_verification_ready(sc.page),
            timeout=VERIFICATION_READY_TIMEOUT,
            interval=0.2,
        ):
            raise SiteChangedError(f"未进入验证码页面: {dom.page_url(sc.page)}")

        otp_callback = sc.require("otp")
        if otp_callback is None:
            raise ConfigError("OpenBlockLabs 注册需要邮箱验证码，但未配置 mailbox provider")

        sc.log("等待 OpenBlockLabs 验证码")
        code = sc.call_with_cancel(otp_callback)
        if not code:
            raise TransientError("未获取到 OpenBlockLabs 邮箱验证码")

        if not dom.fill_otp_by_keyboard(sc.page, str(code).replace("-", "")):
            raise SiteChangedError(f"验证码填写失败: {dom.page_url(sc.page)}")

        dom.click_continue(sc.page)
        sc.sleep(5)
        return StepResult.ok()


class AwaitDashboard(Step):
    """等跳到 dashboard 并拿到 wos-session——这是"注册真的成功了"的凭据。"""

    name = "AwaitDashboard"
    writes = ("wos_session",)
    retry = RetryPolicy(max_attempts=1)

    def run(self, sc: StepContext) -> StepResult:
        if not sc.poll(
            lambda: dom.DASHBOARD_HOST in dom.page_url(sc.page),
            timeout=DASHBOARD_TIMEOUT,
            interval=1.0,
        ):
            raise TransientError(f"OpenBlockLabs 注册后未跳转到 dashboard: {dom.page_url(sc.page)}")

        session = dom.wait_for_wos_session(sc.page, timeout=WOS_SESSION_TIMEOUT, should_stop=sc.cancel)
        if not session:
            raise TransientError(f"未获取到 {dom.SESSION_COOKIE} cookie: {dom.page_url(sc.page)}")

        sc.state["wos_session"] = session
        sc.log(f"注册成功: {sc.state.get('email')}")
        return StepResult.ok()


def build_flow() -> Flow:
    return Flow(
        "openblocklabs.browser_signup",
        [
            OpenSignupPage(),
            FillIdentityForm(),
            SubmitSignupForm(),
            AdvanceToEmailVerification(),
            AwaitEmailOtp(),
            AwaitDashboard(),
        ],
        initial_keys=("email", "password"),
        result_keys=("email", "password", "wos_session"),
    )
