"""Trae.ai 浏览器注册的步骤化流程。

取代旧的整段式 ``TraeBrowserRegister.run``：重试、取消、补偿、检查点、失败现场
全部由引擎提供，平台只写步骤。相比旧实现的实质变化有四处：

1. **裸 RuntimeError 换成分类错误**。旧实现里"未找到邮箱输入框"和"Trae 拒绝
   发送验证码"都落到 ``classify() == "unknown"``，于是一次都不重试，任务层也
   分不清该换邮箱还是该停批。
2. **"点了按钮"不再靠假设**。旧实现在 JS 兜底点击那一支无条件置
   ``send_clicked = True``——JS 里没找到节点也算点成功，于是键盘兜底永远不会跑。
   现在 JS 返回实际结果（见 ``dom.click_leaf_text``）。
3. **拿 token 的"再试一次"交给引擎**。旧实现手写"调一次、睡 5 秒、再调一次"，
   现在是 ``RetryPolicy(max_attempts=2, backoff_seconds=5)``，失败现场也能留下。
4. **send_code 响应监听挂在锚点步骤上**，用 handles 保证回撤重来时不会重复挂。

页面动作在 dom.py，这里只做编排。
"""
from __future__ import annotations

from core.errors import ConfigError, ResourceRejectedError, SiteChangedError, TransientError
from core.registration.steps import Flow, RetryPolicy, Step, StepContext, StepResult

from . import dom

#: 捕获 send_code 响应的列表放在 handles 里（Playwright 的 Response 不可序列化，
#: 不能上黑板），键名同时是"监听已挂过"的幂等标记
SEND_CODE_HANDLE = "send_code_responses"

#: 各阶段的等待窗口，全部照搬旧实现的数字
EMAIL_STAGE_TIMEOUT = 20.0
SEND_CODE_CLICK_TIMEOUT = 15.0
SEND_CODE_RESPONSE_TIMEOUT = 15.0
OTP_STAGE_TIMEOUT = 60.0
SIGNUP_CLICK_TIMEOUT = 10.0
SIGNUP_SETTLE_SECONDS = 3.0
LEAVE_SIGNUP_TIMEOUT = 30.0
LEAVE_SIGNUP_SETTLE_SECONDS = 2.0


class OpenSignupPage(Step):
    """挂上 send_code 监听、打开注册页、定好这次要用的密码。

    anchor：本步骤从零建立页面状态，因此可以作为回撤目标。监听必须在 ``goto``
    之前挂——发码请求可能在首屏就打出去。
    """

    name = "OpenSignupPage"
    # password 同时是 reads 和 writes：读到空才生成，回撤时引擎据此保留当前值
    reads = ("password",)
    writes = ("password",)
    anchor = True

    def run(self, sc: StepContext) -> StepResult:
        responses = sc.handles.get(SEND_CODE_HANDLE)
        if responses is None:
            sc.handles[SEND_CODE_HANDLE] = dom.capture_send_code_responses(sc.page)
        else:
            # 回撤重来：监听还在（同一个 page），但上一轮的响应不能再算数
            responses.clear()

        if not sc.state.get("password"):
            sc.state["password"] = dom.random_password()

        dom.open_signup(sc.page, log=sc.log, should_stop=sc.cancel)
        return StepResult.ok()


class SubmitEmail(Step):
    """填邮箱、点 Send Code，并检查服务端是不是把这封信拒了。

    ``max_attempts=1``：被拒的原因（邮箱已注册 / 域名黑名单 / 频率限制）都跟这个
    邮箱地址绑定，原地重试只会再被拒一次。让这次尝试结束，任务层换邮箱补投。
    """

    name = "SubmitEmail"
    reads = ("email",)
    retry = RetryPolicy(max_attempts=1)

    def run(self, sc: StepContext) -> StepResult:
        email = str(sc.state.get("email") or "")
        if not email:
            raise ConfigError("Trae 注册需要邮箱地址，但身份里没有")

        element = dom.wait_for_visible_input(
            sc.page, dom.EMAIL_INPUT_SELECTORS, timeout=EMAIL_STAGE_TIMEOUT, should_stop=sc.cancel
        )
        if element is None:
            raise SiteChangedError(f"未找到 Trae 邮箱输入框: {sc.page.url}")

        sc.log(f"填写邮箱: {email}")
        dom.click_and_fill(element, email, should_stop=sc.cancel)

        sc.log("发送验证码...")
        clicked = dom.click_leaf_text(
            sc.page,
            dom.SEND_CODE_TEXT,
            log=sc.log,
            timeout=SEND_CODE_CLICK_TIMEOUT,
            should_stop=sc.cancel,
        )
        if not clicked:
            sc.log("未能点击 Send Code，尝试 Tab+Enter", level="warning")
            dom.press_keys(sc.page, "Tab", "Enter", should_stop=sc.cancel)

        self._check_send_result(sc)
        return StepResult.ok()

    def _check_send_result(self, sc: StepContext) -> None:
        """看一眼服务端怎么答的，免得被拒之后还空等一整个验证码超时。"""
        responses = sc.handles.get(SEND_CODE_HANDLE) or []
        sc.poll(lambda: bool(responses), timeout=SEND_CODE_RESPONSE_TIMEOUT)
        if not responses:
            # 旧实现同样只是记一笔：真没发出去的话下一步等不到验证码框，那里才判失败
            sc.log("未捕获到 send_code 请求，可能没有真正点到发送按钮", level="warning")
            return

        rejection = dom.send_code_rejection(responses)
        if rejection:
            raise ResourceRejectedError(f"Trae 拒绝发送验证码: {rejection}")
        sc.log("验证码已发送，等待邮件...")


class AwaitEmailOtp(Step):
    """等验证码输入框、取码、填码。

    ``max_attempts=1``：原地重试会再向邮箱要一封**新**邮件，而站点只发过一封，
    于是白等满一个 OTP 超时才失败。取码失败就让这次尝试结束，由任务层补投。
    """

    name = "AwaitEmailOtp"
    requires = ("otp",)
    retry = RetryPolicy(max_attempts=1)

    def run(self, sc: StepContext) -> StepResult:
        sc.log("等待邮箱验证码...")
        element = dom.wait_for_visible_input(
            sc.page, dom.OTP_INPUT_SELECTORS, timeout=OTP_STAGE_TIMEOUT, interval=1.0, should_stop=sc.cancel
        )
        if element is None:
            raise SiteChangedError(f"未出现 Trae 验证码输入框: {sc.page.url}")

        otp_callback = sc.require("otp")
        if otp_callback is None:
            raise ConfigError("Trae 注册需要邮箱验证码，但未配置 mailbox provider")

        code = sc.call_with_cancel(otp_callback)
        if not code:
            raise TransientError("未获取到 Trae 邮箱验证码")

        sc.log(f"填写验证码: {code}")
        dom.click_and_fill(element, str(code).strip(), should_stop=sc.cancel)
        return StepResult.ok()


class SubmitSignup(Step):
    """填密码、点 Sign Up、等页面离开注册页。

    密码框找不到不算失败：Trae 有过"只要验证码"的投放。真正的判定是下一步能不能
    换到 token。
    """

    name = "SubmitSignup"
    reads = ("password",)

    def run(self, sc: StepContext) -> StepResult:
        sc.log("填写密码...")
        element, _ = dom.find_first_visible(sc.page, dom.PASSWORD_INPUT_SELECTORS)
        if element is None:
            sc.log("未找到密码输入框，按只要验证码的流程继续", level="warning")
        else:
            dom.click_and_fill(element, str(sc.state.get("password") or ""), should_stop=sc.cancel)

        sc.log("提交注册...")
        clicked = dom.click_leaf_text(
            sc.page,
            dom.SIGNUP_TEXTS,
            log=sc.log,
            timeout=SIGNUP_CLICK_TIMEOUT,
            should_stop=sc.cancel,
        )
        if not clicked:
            sc.log("未能点击 Sign Up，尝试回车", level="warning")
            dom.press_keys(sc.page, "Enter", should_stop=sc.cancel)

        sc.sleep(SIGNUP_SETTLE_SECONDS)
        sc.log("等待注册完成...")
        sc.poll(
            lambda: not dom.is_on_signup_page(sc.page),
            timeout=LEAVE_SIGNUP_TIMEOUT,
            interval=1.0,
        )
        sc.sleep(LEAVE_SIGNUP_SETTLE_SECONDS)
        return StepResult.ok()


class ExtractToken(Step):
    """用注册后的 session 换 Cloud-IDE JWT——这是"账号可用"的唯一凭据。

    ``backoff_seconds=5`` 复刻旧实现的"没拿到就睡 5 秒再调一次"，区别是失败这次
    也会留下现场包。
    """

    name = "ExtractToken"
    writes = ("token", "user_id", "region")
    retry = RetryPolicy(max_attempts=2, backoff_seconds=5.0, categories=("transient",))

    def run(self, sc: StepContext) -> StepResult:
        sc.log("提取 Trae token...")
        token, user_id, region = dom.get_cloudide_token(sc.page, sc.log, should_stop=sc.cancel)
        if not token:
            raise TransientError("未获取到 Trae token，账号无法使用")

        sc.state["token"] = token
        sc.state["user_id"] = user_id
        sc.state["region"] = region
        sc.log(f"注册成功: {sc.state.get('email')}")
        return StepResult.ok()


def build_flow() -> Flow:
    return Flow(
        "trae.browser_signup",
        [
            OpenSignupPage(),
            SubmitEmail(),
            AwaitEmailOtp(),
            SubmitSignup(),
            ExtractToken(),
        ],
        initial_keys=("email", "password"),
        # cashier_url / ai_pay_host / final_url 不在这里：旧实现返回的就是空串，
        # _map_trae_result 已经用 .get(..., "") 兜底
        result_keys=("email", "password", "token", "user_id", "region"),
    )
