"""Windsurf 浏览器注册的步骤化流程。

取代 ``browser_register.py`` 里的 ``WindsurfBrowserRegister``（那个文件的其余部分
是 Stripe / 支付宝结账流程，和注册无关，原样留着）。重试、取消、补偿、检查点、
失败现场全部由引擎提供，平台只写步骤。实质变化：

1. **Camoufox → Playwright 的回退交给引擎**。旧实现自己写了
   ``try: Camoufox ... except: sync_playwright``，两条分支各建一次上下文、各有一份
   超时设置。现在是适配器上的 ``engine_preference="camoufox"`` +
   ``allow_engine_fallback=True``——四个平台里只有 Windsurf 该开回退，因为它的旧
   实现确实两种内核都跑得通。
2. **裸 RuntimeError 换成分类错误**，``classify()`` 不再全判 unknown。
3. **response 监听提前到锚点步骤**。旧实现在取到验证码之后才挂，靠的是
   "``/email/complete`` 一定晚于填码"这个假设；提前挂没有坏处，还能接住站点
   提前返回的情况。

Windsurf 的特别之处：注册结果**不在 DOM 上**。session token 只出现在
``WindsurfPostAuth`` 的 protobuf 响应里，所以"点完 Create account 之后发生了什么"
只能靠 response 监听 + 一次页内 API 兜底来判定。

页面动作在 dom.py，这里只做编排。
"""
from __future__ import annotations

from core.errors import ConfigError, SiteChangedError, TransientError
from core.registration.steps import Flow, RetryPolicy, Step, StepContext, StepResult

from . import dom

#: 捕获注册响应的 dict 放在 handles 里（Playwright 的 Response 不可序列化，不能上黑板），
#: 键名同时是"监听已挂过"的幂等标记
CAPTURE_HANDLE = "windsurf_auth_responses"

#: 各阶段的等待窗口。页面动作统一 90 秒（旧实现的 set_default_timeout）
SESSION_TIMEOUT = 90.0


def _api(sc: StepContext) -> dom.WindsurfBrowserApi:
    return dom.WindsurfBrowserApi(sc.page, log_fn=sc.log)


class OpenRegisterPage(Step):
    """打开登录页、点进注册页，并挂好注册结果的 response 监听。

    anchor：本步骤从零建立页面状态，因此可以作为回撤目标。这一次尝试要用的姓名也
    在这里定下来——``result_keys`` 要求它无论后面哪一步被跳过都在黑板上。
    """

    name = "OpenRegisterPage"
    reads = ("email",)
    writes = ("name",)
    anchor = True

    def run(self, sc: StepContext) -> StepResult:
        email = str(sc.state.get("email") or "")
        if not email:
            raise ConfigError("Windsurf 注册需要邮箱地址，但身份里没有")
        sc.state["name"] = str(sc.ctx.extra.get("name") or dom.default_name(email))

        captured = sc.handles.get(CAPTURE_HANDLE)
        if captured is None:
            sc.handles[CAPTURE_HANDLE] = dom.capture_auth_responses(sc.page)
        else:
            # 回撤重来：监听还在（同一个 page），但上一轮捕到的不能再算数
            captured["complete"] = {}
            captured["auth"] = {}

        sc.page.set_default_timeout(dom.PAGE_TIMEOUT_MS)
        sc.page.add_init_script(dom.MOUSE_EVENT_PATCH)

        sc.log("打开 Windsurf 登录页并进入注册页")
        sc.page.goto(dom.LOGIN_URL, wait_until="domcontentloaded", timeout=dom.PAGE_TIMEOUT_MS)
        try:
            sc.page.get_by_role("link", name=dom.SIGN_UP_LINK_PATTERN).click()
            sc.page.wait_for_url(dom.REGISTER_URL_PATTERN, timeout=dom.PAGE_TIMEOUT_MS)
            sc.page.wait_for_selector(dom.GIVEN_NAME_SELECTOR, state="visible", timeout=dom.PAGE_TIMEOUT_MS)
        except Exception as exc:
            raise SiteChangedError(f"未进入 Windsurf 注册页: {exc}") from exc
        return StepResult.ok()


class SubmitIdentity(Step):
    """填姓名、邮箱，勾条款，提交。"""

    name = "SubmitIdentity"
    reads = ("email", "name")

    def run(self, sc: StepContext) -> StepResult:
        first_name, last_name = dom.split_name(str(sc.state.get("name") or ""))
        sc.log("填写姓名、邮箱并同意条款")
        try:
            sc.page.locator(dom.GIVEN_NAME_SELECTOR).fill(first_name)
            sc.page.locator(dom.FAMILY_NAME_SELECTOR).fill(last_name)
            sc.page.locator(dom.EMAIL_SELECTOR).fill(str(sc.state.get("email") or ""))
            tos = sc.page.locator(dom.TOS_CHECKBOX_SELECTOR)
            if not tos.is_checked():
                tos.check()
            sc.page.locator(dom.SUBMIT_BUTTON_SELECTOR).click()
            sc.page.wait_for_selector(dom.PASSWORD_SELECTOR, state="visible", timeout=dom.PAGE_TIMEOUT_MS)
        except Exception as exc:
            raise SiteChangedError(f"Windsurf 注册表单提交失败: {exc}") from exc
        return StepResult.ok()


class SubmitPassword(Step):
    """密码页：密码 + 确认密码。"""

    name = "SubmitPassword"
    reads = ("password",)

    def run(self, sc: StepContext) -> StepResult:
        password = str(sc.state.get("password") or "")
        if not password:
            raise ConfigError("Windsurf 注册需要密码，但上游没有生成")

        sc.log("填写 Windsurf 密码")
        try:
            sc.page.locator(dom.PASSWORD_SELECTOR).fill(password)
            sc.page.locator(dom.CONFIRM_PASSWORD_SELECTOR).fill(password)
            sc.page.locator(dom.SUBMIT_BUTTON_SELECTOR).click()
            sc.page.wait_for_selector(dom.OTP_INPUT_SELECTOR, state="visible", timeout=dom.PAGE_TIMEOUT_MS)
        except Exception as exc:
            raise SiteChangedError(f"Windsurf 密码页提交失败: {exc}") from exc
        return StepResult.ok()


class AwaitEmailOtp(Step):
    """取码、填进分格控件、点 Create account。

    ``max_attempts=1``：原地重试会再向邮箱要一封**新**邮件，而站点只发过一封，
    于是白等满一个 OTP 超时才失败。
    """

    name = "AwaitEmailOtp"
    requires = ("otp",)
    retry = RetryPolicy(max_attempts=1)

    def run(self, sc: StepContext) -> StepResult:
        otp_callback = sc.require("otp")
        if otp_callback is None:
            raise ConfigError("Windsurf 注册需要邮箱验证码，但未配置 mailbox provider")

        raw = sc.call_with_cancel(otp_callback)
        code = dom.extract_code(raw)
        if not code:
            raise TransientError(f"无法从邮件内容中提取 Windsurf 6 位验证码: {str(raw or '')[:200]}")
        sc.log(f"获取 Windsurf 验证码: {code}")

        sc.log("填写邮箱验证码并创建账号")
        dom.fill_otp(sc.page, code)
        sc.page.get_by_role("button", name=dom.CREATE_ACCOUNT_BUTTON_PATTERN).click()
        return StepResult.ok()


class ResolveSession(Step):
    """从捕获的响应里取 session；没捕到就用页面会话补兑换一次。

    ``email`` 同时是 reads 和 writes：站点返回的规范化邮箱优先于我们提交的那个。
    """

    name = "ResolveSession"
    reads = ("email",)
    writes = ("email", "auth_token", "session_token", "account_id", "org_id", "user_id")
    retry = RetryPolicy(max_attempts=1)

    def run(self, sc: StepContext) -> StepResult:
        captured = sc.handles.get(CAPTURE_HANDLE) or {}
        auth = dom.wait_for_session(captured, timeout=SESSION_TIMEOUT, should_stop=sc.cancel)

        complete = dict(captured.get("complete") or {})
        auth_token = str(complete.get("token") or "")
        if not auth and auth_token:
            sc.log("页面未捕获 WindsurfPostAuth 响应，使用页面会话补充兑换 session")
            auth = _api(sc).post_auth(auth_token)

        if not (auth or {}).get("session_token"):
            raise TransientError(
                f"Windsurf 页面注册未拿到 session_token，当前页面: {dom.visible_body_text(sc.page)}"
            )

        sc.state["email"] = str(complete.get("email") or sc.state.get("email") or "")
        sc.state["auth_token"] = auth_token
        sc.state["session_token"] = str(auth.get("session_token") or "")
        sc.state["account_id"] = str(auth.get("account_id") or "")
        sc.state["org_id"] = str(auth.get("org_id") or "")
        sc.state["user_id"] = str(complete.get("user_id") or "")
        return StepResult.ok()


class LoadAccountState(Step):
    """读一遍账号状态：套餐、额度、订阅——落库的 extra 就是它。"""

    name = "LoadAccountState"
    reads = ("email", "session_token", "account_id", "org_id", "user_id")
    writes = ("account_overview", "state_summary", "user_id")
    retry = RetryPolicy(max_attempts=2, backoff_seconds=2.0, categories=("transient",))

    def run(self, sc: StepContext) -> StepResult:
        state = _api(sc).load_account_state(
            session_token=str(sc.state.get("session_token") or ""),
            account_id=str(sc.state.get("account_id") or ""),
            org_id=str(sc.state.get("org_id") or ""),
            fallback_email=str(sc.state.get("email") or ""),
        )
        summary = dict(state.get("summary") or {})
        overview = dict(summary.get("account_overview") or {})

        sc.state["account_overview"] = overview
        sc.state["state_summary"] = summary
        if not sc.state.get("user_id"):
            sc.state["user_id"] = str((overview.get("remote_user") or {}).get("user_id") or "")

        sc.log(
            f"Windsurf 浏览器注册成功: {sc.state.get('email')} "
            f"plan={overview.get('plan_name', 'unknown')} "
            f"quota={overview.get('remaining_credits', '-')}"
        )
        return StepResult.ok()


def build_flow() -> Flow:
    return Flow(
        "windsurf.browser_signup",
        [
            OpenRegisterPage(),
            SubmitIdentity(),
            SubmitPassword(),
            AwaitEmailOtp(),
            ResolveSession(),
            LoadAccountState(),
        ],
        initial_keys=("email", "password"),
        result_keys=(
            "email",
            "password",
            "name",
            "user_id",
            "auth_token",
            "session_token",
            "account_id",
            "org_id",
            "account_overview",
            "state_summary",
        ),
    )
