"""ChatGPT 浏览器注册的步骤化流程。

取代旧的整段式 ``_browser_registration_flow``（一个 12 轮的状态机循环）：
重试、回撤、取消、补偿、检查点、失败现场全部由引擎提供，平台只写步骤。

与 cursor 的两点不同：

1. **页面顺序由服务端决定**。add_phone 可能出现在 OTP 之后，也可能出现在
   about_you 之后（风控挑战才弹），所以每个步骤的 ``when()`` 都读一次活页面的
   ``page_type``，而不是按固定顺序假定。黑板上的 ``page_type`` 只是给检查点和
   失败现场看的副本。
2. **Codex OAuth 要换一个干净浏览器**。注册完的上下文直接做 OAuth 会
   NS_BINDING_ABORTED，旧实现为此自己 new 了第二个 Camoufox（引擎完全看不见）。
   现在走 ``sc.open_aux_browser``，会话归引擎管，失败现场里也有它的截图。

页面动作在 dom.py / about_you.py / phone.py / codex_oauth.py，这里只做编排。
"""
from __future__ import annotations

import uuid

from core.errors import ConfigError, SiteChangedError, TransientError
from core.registration.steps import Flow, RetryPolicy, Step, StepContext, StepResult

from .about_you import _submit_about_you_via_page
from .codex_oauth import run_codex_oauth
from .constants import CHATGPT_APP, OPENAI_AUTH
from .dom import (
    _derive_registration_state_from_page,
    _handle_post_signup_onboarding,
    _is_about_you,
    _is_add_phone,
    _is_email_otp,
    _is_password_registration,
    _is_registration_complete,
    _recover_signup_password_page,
    _seed_browser_device_id,
    _start_browser_signup_via_authorize,
    _start_browser_signup_via_page,
    _submit_otp_via_page,
    _submit_password_via_page,
)
from .errors import PhoneCodeTimeout, PhoneRejectedError, error_from_auth_text
from .phone import run_add_phone_attempt

#: Codex OAuth 用的辅助浏览器名字（handles / 现场包里按这个名字归档）
AUX_BROWSER = "codex_oauth"

PAGE_LOGIN_PASSWORD = "login_password"

#: 最后一步确认"注册已落地"的观察窗口。站点跳到 chatgpt.com 前会过一两次重定向，
#: 太短会把正常的慢跳判成卡住。
COMPLETE_SETTLE_SECONDS = 10


def _refresh(sc: StepContext, page=None) -> dict:
    """读一次活页面的状态，并把 page_type 同步到黑板。

    黑板上的副本进检查点与现场包 meta；判断一律用返回的这份，因为页面随时可能
    在我们等待的这几秒里自己往前走了。
    """
    state = _derive_registration_state_from_page(page if page is not None else sc.page)
    sc.state["page_type"] = str(state.get("page_type") or "")
    sc.state["current_url"] = str(state.get("current_url") or "")
    return state


def _raise_if_failed(resp: dict, *, prefix: str) -> None:
    """把 dom / about_you 的伪响应翻译成分类错误。"""
    if resp.get("ok"):
        return
    raise error_from_auth_text(resp.get("text"), prefix=prefix)


class OpenSignupPage(Step):
    """打开注册入口并填邮箱。

    anchor：本步骤从零建立页面状态，因此可以作为回撤目标。
    """

    name = "OpenSignupPage"
    reads = ("email",)
    writes = ("page_type", "device_id")
    anchor = True

    def run(self, sc: StepContext) -> StepResult:
        device_id = str(sc.state.get("device_id") or uuid.uuid4())
        _seed_browser_device_id(sc.page, device_id)
        sc.state["device_id"] = device_id

        email = sc.state["email"]
        try:
            _start_browser_signup_via_page(sc.page, email, sc.log, should_stop=sc.cancel)
        except SiteChangedError as exc:
            # 页面驱动的入口找不到表单时，退回 authorize 接口那条路——
            # 两条入口 OpenAI 会按投放轮换，任一条能进就行
            sc.log(f"页面入口失败，回退 authorize 入口: {exc}", level="warning")
            _start_browser_signup_via_authorize(sc.page, email, device_id, sc.log, should_stop=sc.cancel)

        _refresh(sc)
        return StepResult.ok()


class SubmitPassword(Step):
    """密码页：注册密码，或站点把我们甩到已有账号登录页时的恢复。"""

    name = "SubmitPassword"
    reads = ("password", "page_type")
    writes = ("page_type",)
    retry = RetryPolicy(max_attempts=2, categories=("transient",))

    def when(self, sc: StepContext) -> bool:
        state = _refresh(sc)
        return _is_password_registration(state) or str(state.get("page_type") or "") == PAGE_LOGIN_PASSWORD

    def run(self, sc: StepContext) -> StepResult:
        # 幂等入口：重试进来时页面可能已经过了密码页（上一轮其实提交成功了）
        state = _refresh(sc)
        if not (_is_password_registration(state) or str(state.get("page_type") or "") == PAGE_LOGIN_PASSWORD):
            sc.log("页面已不在密码页，跳过提交")
            return StepResult.ok()

        password = str(sc.state.get("password") or "")
        if not password:
            raise ConfigError("ChatGPT 注册需要密码，但任务没有提供")

        if str(state.get("page_type") or "") == PAGE_LOGIN_PASSWORD:
            if _recover_signup_password_page(sc.page, sc.log):
                sc.log("已从登录密码页恢复回注册流程")
                state = _refresh(sc)
            else:
                from .codex_oauth import _submit_oauth_password_direct

                sc.log("注册流程落到已有账号登录密码页，按登录继续认证")
                _raise_if_failed(
                    _submit_oauth_password_direct(sc.page, password, sc.log, should_stop=sc.cancel),
                    prefix="登录密码页提交失败: ",
                )
                self._mark_submitted(sc)
                _refresh(sc)
                return StepResult.ok()

        _raise_if_failed(
            _submit_password_via_page(sc.page, password, sc.log, should_stop=sc.cancel),
            prefix="密码页提交失败: ",
        )
        self._mark_submitted(sc)
        _refresh(sc)
        return StepResult.ok()

    @staticmethod
    def _mark_submitted(sc: StepContext) -> None:
        """提交了密码，站点侧账号就可能已经建好。

        立刻记成半成品并落检查点：进程在这之后被杀，重启才能把它捞出来交人工确认，
        而不是留下一个查无此人的邮箱。
        """
        account = sc.resource_refs.setdefault("account", {})
        account["email"] = sc.state.get("email", "")
        account["submitted"] = True
        sc.checkpoint_now()


class AwaitEmailOtp(Step):
    name = "AwaitEmailOtp"
    requires = ("otp",)
    reads = ("page_type",)
    writes = ("page_type",)
    retry = RetryPolicy(max_attempts=2, categories=("transient",))

    def when(self, sc: StepContext) -> bool:
        return _is_email_otp(_refresh(sc))

    def run(self, sc: StepContext) -> StepResult:
        if not _is_email_otp(_refresh(sc)):
            sc.log("页面已过验证码页，跳过")
            return StepResult.ok()

        otp_callback = sc.require("otp")
        if otp_callback is None:
            raise ConfigError("ChatGPT 注册需要邮箱验证码，但未配置 mailbox provider")

        sc.log("等待邮箱验证码")
        code = str(sc.call_with_cancel(otp_callback) or "").strip()
        if not code:
            raise TransientError("未获取到邮箱验证码")

        _raise_if_failed(
            _submit_otp_via_page(sc.page, code, sc.log, should_stop=sc.cancel),
            prefix="验证码校验失败: ",
        )
        _refresh(sc)
        return StepResult.ok()


class MaybeAddPhone(Step):
    """手机验证页是可选的：OpenAI 的风控挑战才会出现。

    可能在 OTP 之后弹，也可能在 about_you 之后弹，所以流程里挂了两次（见
    ``build_flow``），两次共用同一个租约句柄。

    与 cursor 不同：等不到码时这里**换号重试**而不是直接让尝试失败——
    ChatGPT 的 /add-phone 页可以原地重进，换个号往往当场就成了。
    """

    name = "MaybeAddPhone"
    requires = ("phone",)
    reads = ("device_id", "page_type")
    writes = ("page_type", "phone_number")
    # 对应旧实现的 max_phone_attempts=3
    retry = RetryPolicy(max_attempts=3, categories=("resource_rejected", "transient"))

    def __init__(self, name: str = "MaybeAddPhone"):
        self.name = name

    def when(self, sc: StepContext) -> bool:
        return _is_add_phone(_refresh(sc))

    def run(self, sc: StepContext) -> StepResult:
        if not _is_add_phone(_refresh(sc)):
            sc.log("页面已不在手机验证页，跳过")
            sc.state.setdefault("phone_number", "")
            return StepResult.ok()

        phone = sc.require("phone")
        if phone is None:
            raise ConfigError(
                "ChatGPT 注册遇到手机号验证，但未配置接码渠道。"
                "请在任务参数或设置页配置 SMS provider，或改用 headed 模式人工完成验证。"
            )

        lease = sc.handles.get("phone_lease")
        if lease is None or lease.released:
            lease = sc.call_with_cancel(phone.acquire)
            self._hold(sc, lease)
        sc.state["phone_number"] = str(lease.phone_number or "")

        # 重试进来时页面可能停在别处（上一轮换号前跳走了），先回到 add-phone
        if "add-phone" not in str(getattr(sc.page, "url", "") or ""):
            sc.page.goto(f"{OPENAI_AUTH}/add-phone", wait_until="domcontentloaded", timeout=30000)

        try:
            run_add_phone_attempt(
                sc.page,
                lease,
                log=sc.log,
                resume_url=f"{CHATGPT_APP}/",
                should_stop=sc.cancel,
            )
        except (PhoneRejectedError, PhoneCodeTimeout) as exc:
            # 这个号不行：当场换一个，让引擎带着新号重试本步骤
            sc.log(f"更换手机号后重试: {exc}", level="warning")
            self._hold(sc, sc.call_with_cancel(lease.swap))
            raise

        lease.release("success")
        # 号码已按成功路径结束，检查点里不能再留着它，
        # 否则重启回收会把一个已用掉的号当遗留资源退回去
        sc.handles.pop("phone_lease", None)
        sc.resource_refs.pop("phone", None)
        sc.checkpoint_now()
        _refresh(sc)
        return StepResult.ok()

    @staticmethod
    def _hold(sc: StepContext, lease) -> None:
        """记下正在占用的租约并立刻落检查点——等码的几分钟正是进程最可能被杀的窗口。"""
        sc.handles["phone_lease"] = lease
        sc.resource_refs["phone"] = lease.to_ref()
        sc.checkpoint_now()

    def compensate(self, sc: StepContext) -> None:
        lease = sc.handles.pop("phone_lease", None)
        if lease is not None and not lease.released:
            lease.release("unused")
        sc.resource_refs.pop("phone", None)


class MaybeAboutYou(Step):
    """about_you 页是可选的：部分投放直接跳过。"""

    name = "MaybeAboutYou"
    reads = ("page_type",)
    writes = ("page_type",)

    def when(self, sc: StepContext) -> bool:
        return _is_about_you(_refresh(sc))

    def run(self, sc: StepContext) -> StepResult:
        if not _is_about_you(_refresh(sc)):
            sc.log("页面已过 about_you，跳过")
            return StepResult.ok()

        if "about-you" not in str(getattr(sc.page, "url", "") or ""):
            sc.page.goto(f"{OPENAI_AUTH}/about-you", wait_until="domcontentloaded", timeout=30000)

        _raise_if_failed(
            _submit_about_you_via_page(sc.page, sc.log, should_stop=sc.cancel),
            prefix="about_you 提交失败: ",
        )
        _refresh(sc)
        return StepResult.ok()


class ConfirmSignupComplete(Step):
    """确认注册已落地，顺手处理注册后的引导弹窗。

    旧实现靠"同一个状态签名出现第 3 次就报卡住"来兜底；这里分两种情况：
    认得出页面但顺序不对 = 瞬态，回撤重来；完全认不出 = 站点改版，留现场停批。
    """

    name = "ConfirmSignupComplete"
    reads = ("page_type",)
    writes = ("page_type",)
    retry = RetryPolicy(max_attempts=1)

    def run(self, sc: StepContext) -> StepResult:
        def _done():
            state = _refresh(sc)
            return state if _is_registration_complete(state) else None

        if sc.poll(_done, timeout=COMPLETE_SETTLE_SECONDS, interval=1.0) is not None:
            _handle_post_signup_onboarding(sc.page, sc.log)
            _refresh(sc)
            sc.log("注册流程已完成")
            return StepResult.ok()

        page_type = str(sc.state.get("page_type") or "")
        current_url = str(sc.state.get("current_url") or "")
        if not page_type:
            raise SiteChangedError(f"注册流程停在无法识别的页面: {current_url[:200]}")
        return StepResult.rewind("OpenSignupPage", f"注册状态卡住: page={page_type}")


class CodexOAuth(Step):
    """在全新浏览器里跑 Codex CLI OAuth 换 token。

    注册完的上下文做 OAuth 会 NS_BINDING_ABORTED，所以必须换一个会话。
    成功时自己关掉辅助浏览器；失败时留给 compensate 关——引擎是先采集现场再补偿的，
    提前关掉的话现场包里就只剩一张空白截图。
    """

    name = "CodexOAuth"
    requires = ("otp",)
    reads = ("email", "password")
    writes = ("account_id", "access_token", "refresh_token", "id_token")
    retry = RetryPolicy(max_attempts=2, categories=("transient",))

    def run(self, sc: StepContext) -> StepResult:
        otp_callback = sc.require("otp")
        if otp_callback is None:
            raise ConfigError("ChatGPT OAuth 需要邮箱验证码，但未配置 mailbox provider")

        # phone 故意不写进 requires：那会把它从 conditional 提成 required，
        # 前端会要求每个 ChatGPT 任务都配接码渠道。OAuth 里 add_phone 只是偶发。
        phone = sc.require("phone")

        page = sc.open_aux_browser(AUX_BROWSER)
        result = run_codex_oauth(
            page,
            email=str(sc.state.get("email") or ""),
            password=str(sc.state.get("password") or ""),
            otp_callback=otp_callback,
            phone=phone,
            proxy=sc.ctx.proxy,
            log=sc.log,
            should_stop=sc.cancel,
        )
        sc.close_aux_browser(AUX_BROWSER)

        for key in self.writes:
            sc.state[key] = str(result.get(key) or "")
        sc.log(f"OAuth 完成: account_id={sc.state['account_id']}")
        return StepResult.ok()

    def compensate(self, sc: StepContext) -> None:
        sc.close_aux_browser(AUX_BROWSER)


def build_flow() -> Flow:
    return Flow(
        "chatgpt.browser_signup",
        [
            OpenSignupPage(),
            SubmitPassword(),
            AwaitEmailOtp(),
            # add_phone 的两个出现点：OTP 之后、about_you 之后。
            # 引擎的游标只前进，所以两处各挂一次，靠 when() 决定哪次真跑。
            MaybeAddPhone(),
            MaybeAboutYou(),
            MaybeAddPhone("MaybeAddPhoneAfterAboutYou"),
            ConfirmSignupComplete(),
            CodexOAuth(),
        ],
        initial_keys=("email", "password"),
        # session_token / workspace_id / cookies / profile 不在这里：
        # _map_chatgpt_result 已经用 .get(..., "") 兜底，和旧路径一样是空值
        result_keys=("email", "password", "account_id", "access_token", "refresh_token", "id_token"),
    )
