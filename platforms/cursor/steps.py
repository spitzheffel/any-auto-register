"""Cursor 浏览器注册的步骤化流程。

取代了旧的整段式 `CursorBrowserRegister.run()`（已删除）：重试、回撤、取消、
补偿、检查点、失败现场全部由引擎统一提供，Turnstile / CF 拦截交给
ObstacleHandler（它们出现的位置不固定）。DOM 辅助函数在 dom.py。
"""
from __future__ import annotations

import json
import random
import string
import time
import urllib.parse as urlparse
import uuid

from core.errors import ConfigError, ResourceRejectedError, SiteChangedError, TransientError
from core.registration.dom import MOUSE_EVENT_PATCH
from core.registration.steps import Flow, RetryPolicy, Step, StepContext, StepResult

from .dom import (
    AUTH,
    _click_continue,
    _wait_cf_full_block_clear,
    _wait_for_token,
)

SIGNUP_CLIENT_ID = "client_01GS6W3C96KW4WRS6Z93JCE2RJ"
SIGNUP_REDIRECT = "https://cursor.com/api/auth/callback"

OTP_SELECTORS = (
    'input[name="otp"]',
    'input[name="code"]',
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[maxlength="1"]',
    'input[type="text"]',
    'input[type="number"]',
)

PHONE_SELECTORS = 'input[type="tel"], input[placeholder*="555"], input[autocomplete="tel"]'


def _random_name() -> str:
    return "".join(random.choices(string.ascii_lowercase, k=5)).capitalize()


def _random_password() -> str:
    return (
        "".join(random.choices(string.ascii_uppercase, k=2))
        + "".join(random.choices(string.digits, k=3))
        + "".join(random.choices(string.ascii_lowercase, k=5))
        + "!"
    )


class OpenSignupPage(Step):
    """打开带 state 的注册页。

    anchor：本步骤从零建立页面状态，因此可以作为回撤目标。
    """

    name = "OpenSignupPage"
    writes = ("signup_url",)
    anchor = True

    def run(self, sc: StepContext) -> StepResult:
        page = sc.page
        sc.page.add_init_script(MOUSE_EVENT_PATCH)

        # 必须带 state(含随机 nonce)访问，WorkOS 才会生成 authorization_session_id；
        # 少了它，form POST 到 /user_management/initiate_login 会 404
        state = urlparse.quote(json.dumps({"returnTo": "/dashboard", "nonce": str(uuid.uuid4())}))
        signup_url = (
            f"{AUTH}/sign-up"
            f"?client_id={SIGNUP_CLIENT_ID}"
            f"&redirect_uri={urlparse.quote(SIGNUP_REDIRECT, safe='')}"
            f"&state={state}"
        )
        sc.log("打开 Cursor 注册页")
        page.goto(signup_url, wait_until="domcontentloaded", timeout=30000)
        _wait_cf_full_block_clear(page, log_fn=sc.log)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

        sc.state["signup_url"] = signup_url
        return StepResult.ok()


class FillIdentityForm(Step):
    name = "FillIdentityForm"
    reads = ("email",)
    writes = ("first_name", "last_name")

    def run(self, sc: StepContext) -> StepResult:
        page = sc.page
        sc.log("等待注册表单...")
        try:
            page.wait_for_selector(
                'input[name="firstName"], input[name="first_name"], input[name="email"]',
                timeout=60000,  # CF Managed Challenge 可能需要较长时间
            )
        except Exception:
            raise SiteChangedError(f"Cursor 注册页未加载表单: {getattr(page, 'url', '')}")

        first, last = _random_name(), _random_name()
        for selector, value in (
            ('input[name="firstName"]', first),
            ('input[name="first_name"]', first),
            ('input[name="lastName"]', last),
            ('input[name="last_name"]', last),
        ):
            element = page.query_selector(selector)
            if element and element.is_visible():
                element.fill(value)
                time.sleep(0.3)

        email = sc.state["email"]
        try:
            page.wait_for_selector('input[name="email"]', timeout=5000)
        except Exception:
            raise SiteChangedError("未找到邮箱输入框")
        sc.log(f"填写邮箱: {email}")
        page.fill('input[name="email"]', email)
        time.sleep(0.5)

        sc.state["first_name"] = first
        sc.state["last_name"] = last
        return StepResult.ok()


class SubmitIdentityForm(Step):
    name = "SubmitIdentityForm"
    reads = ("email",)

    def run(self, sc: StepContext) -> StepResult:
        sc.log("点击 Continue")
        if not _click_continue(sc.page):
            sc.log("未找到按钮，使用 Enter 提交")
            sc.page.keyboard.press("Enter")
        # Turnstile 若在此弹出，由 ObstacleHandler 在下一步之前处理
        time.sleep(2)
        return StepResult.ok()


class MaybeSetPassword(Step):
    """密码页是可选的：出现就填，不出现就跳过。"""

    name = "MaybeSetPassword"
    reads = ("password",)
    writes = ("password",)

    def when(self, sc: StepContext) -> bool:
        try:
            sc.page.wait_for_selector('input[type="password"]', timeout=8000)
            return True
        except Exception:
            return False

    def run(self, sc: StepContext) -> StepResult:
        page = sc.page
        use_password = str(sc.state.get("password") or "") or _random_password()
        sc.log("检测到密码设置页，填写密码...")
        for element in page.query_selector_all('input[type="password"]'):
            if element.is_visible():
                element.fill(use_password)
                time.sleep(0.3)

        # 密码必须写回黑板：历史版本在这里生成了随机密码却没回传，账号入库即失联
        sc.state["password"] = use_password

        time.sleep(0.5)
        if not _click_continue(page):
            sc.log("未找到按钮，使用 Enter 提交密码")
            page.keyboard.press("Enter")
        # 提交了密码，站点侧账号就可能已经建好：立刻记成半成品并落检查点，
        # 进程在这之后崩掉时重启才能把它捞出来交人工确认
        account = sc.resource_refs.setdefault("account", {})
        account["email"] = sc.state.get("email", "")
        account["submitted"] = True
        sc.checkpoint_now()
        time.sleep(2)
        return StepResult.ok()


class MaybePhoneVerify(Step):
    """手机验证页是可选的：Cursor 的风控挑战才会出现。

    只有"号码被站点拒绝"值得在原页面上换号重试（页面还停在手机号输入框）；
    "等不到短信"时页面已经进了 OTP 页，原地重试没有意义——直接让这次尝试失败，
    由任务层按补投预算换邮箱、换号重来一次。
    """

    name = "MaybePhoneVerify"
    requires = ("phone",)
    writes = ("phone_number",)
    retry = RetryPolicy(max_attempts=2, categories=("resource_rejected",))

    def when(self, sc: StepContext) -> bool:
        page = sc.page
        try:
            element = page.query_selector(PHONE_SELECTORS)
            if not element:
                page.wait_for_selector('input[type="tel"]', timeout=4000)
                element = page.query_selector('input[type="tel"]')
            return bool(element and element.is_visible())
        except Exception:
            return False

    def run(self, sc: StepContext) -> StepResult:
        page = sc.page
        phone = sc.require("phone")
        if phone is None:
            raise ConfigError(
                "Cursor 注册需要手机号验证，但未配置接码渠道。"
                "请在任务参数或设置页配置 SMS provider，或改用人工完成验证。"
            )

        # 重试进来时页面可能已经不在手机号页（上一轮没收到码、停在 OTP 页）：
        # 在错误的页面上填号只会崩在 element.click()，回到注册页重来更有用
        element = page.query_selector(PHONE_SELECTORS)
        if element is None:
            return StepResult.rewind("OpenSignupPage", "重试时手机号输入框已不存在")

        lease = sc.handles.get("phone_lease")
        if lease is None or lease.released:
            lease = phone.acquire(should_stop=sc.cancel)
            self._hold(sc, lease)

        sc.state["phone_number"] = lease.phone_number
        sc.log(f"检测到手机号验证页，填写手机号: {lease.phone_number[:4]}****")

        element.click()
        element.fill(str(lease.phone_number).strip())
        time.sleep(0.5)
        _click_continue(page)
        time.sleep(3)

        try:
            page.wait_for_selector(
                'input[autocomplete="one-time-code"], input[inputmode="numeric"], input[maxlength="1"]',
                timeout=30000,
            )
        except Exception:
            # 站点不接受这个号：作废并换一个，让引擎按 resource_rejected 在原页面重试
            lease.report_send_failed("未进入短信验证码页")
            self._hold(sc, lease.swap(should_stop=sc.cancel))
            raise ResourceRejectedError("提交手机号后未进入短信验证码页，已换号")

        lease.report_send_succeeded()
        code = lease.wait_code(should_stop=sc.cancel)
        if not code:
            # 同一个号再等一次也不会来码：退掉它；本步骤对 transient 不原地重试，
            # 尝试整体失败后任务层补投一次（新邮箱 + 新号）
            lease.report_code_failed("未收到短信验证码")
            self._drop(sc, lease)
            raise TransientError("未收到短信验证码")

        sc.log(f"填写短信验证码: {code}")
        for digit in str(code).strip():
            page.keyboard.press(digit)
            time.sleep(0.1)
        time.sleep(1)
        page.keyboard.press("Enter")
        time.sleep(3)
        lease.release("success")
        # 号码已按成功路径结束，检查点里不能再留着它，否则重启回收会把它当遗留资源退掉
        sc.handles.pop("phone_lease", None)
        sc.resource_refs.pop("phone", None)
        sc.checkpoint_now()
        return StepResult.ok()

    @staticmethod
    def _hold(sc: StepContext, lease) -> None:
        """记下正在占用的租约，并立刻落检查点——等码的几分钟正是进程最可能被杀的窗口。"""
        sc.handles["phone_lease"] = lease
        sc.resource_refs["phone"] = lease.to_ref()
        sc.checkpoint_now()

    @staticmethod
    def _drop(sc: StepContext, lease) -> None:
        if not lease.released:
            lease.release("unused")
        sc.handles.pop("phone_lease", None)
        sc.resource_refs.pop("phone", None)
        sc.checkpoint_now()

    def compensate(self, sc: StepContext) -> None:
        lease = sc.handles.pop("phone_lease", None)
        if lease is not None and not lease.released:
            lease.release("unused")
        sc.resource_refs.pop("phone", None)


class AwaitAndFillEmailOtp(Step):
    name = "AwaitAndFillEmailOtp"
    requires = ("otp",)
    reads = ("email",)

    def run(self, sc: StepContext) -> StepResult:
        page = sc.page
        sc.log("等待验证码输入框...")
        otp_input = self._wait_for_otp_input(sc)
        if otp_input is None:
            # 页面既没到验证码页也没报错：回到注册页从头来一次，比在这里干等有用
            return StepResult.rewind("OpenSignupPage", f"未出现验证码输入框 (url={getattr(page, 'url', '')})")

        otp_callback = sc.require("otp")
        if otp_callback is None:
            raise ConfigError("Cursor 注册需要邮箱验证码，但未配置 mailbox provider")

        sc.log("等待邮箱验证码")
        code = sc.call_with_cancel(otp_callback)
        if not code:
            raise TransientError("未获取到邮箱验证码")
        sc.log(f"验证码: {code}")

        try:
            otp_input.click()
            time.sleep(0.3)
        except Exception:
            pass
        for digit in str(code).strip():
            page.keyboard.press(digit)
            time.sleep(random.uniform(0.08, 0.2))
        time.sleep(1)
        # WorkOS 6 格子会自动提交，没提交就补一个 Enter
        if "email-verification" in str(getattr(page, "url", "")):
            page.keyboard.press("Enter")
        time.sleep(5)
        return StepResult.ok()

    def _wait_for_otp_input(self, sc: StepContext, timeout: int = 60):
        page = sc.page
        deadline = time.time() + timeout
        while time.time() < deadline:
            sc.cancel.raise_if_cancelled()
            url = str(getattr(page, "url", "") or "")
            selectors = OTP_SELECTORS if ("email-verification" in url or "verify" in url) else OTP_SELECTORS[:2]
            for selector in selectors:
                element = page.query_selector(selector)
                if element and element.is_visible():
                    return element
            time.sleep(1)
        return None


class ExtractSessionToken(Step):
    name = "ExtractSessionToken"
    reads = ("email",)
    writes = ("token", "resolved_email")

    def run(self, sc: StepContext) -> StepResult:
        sc.log("等待 WorkosCursorSessionToken")
        token = _wait_for_token(sc.page, timeout=60, should_stop=sc.cancel)
        if not token:
            raise TransientError("未获取到 WorkosCursorSessionToken")

        from platforms.cursor.switch import get_cursor_user_info

        user_info = get_cursor_user_info(token) or {}
        resolved_email = user_info.get("email") or sc.state["email"]
        sc.log(f"注册成功: {resolved_email}")

        sc.state["token"] = token
        sc.state["resolved_email"] = resolved_email
        return StepResult.ok()



def build_flow() -> Flow:
    return Flow(
        "cursor.browser_signup",
        [
            OpenSignupPage(),
            FillIdentityForm(),
            SubmitIdentityForm(),
            MaybeSetPassword(),
            MaybePhoneVerify(),
            AwaitAndFillEmailOtp(),
            ExtractSessionToken(),
        ],
        initial_keys=("email", "password"),
        result_keys=("email", "password", "token", "resolved_email"),
    )
