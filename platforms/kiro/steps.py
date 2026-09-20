"""Kiro（AWS Builder ID）浏览器注册的步骤化流程。

取代旧的整段式 ``KiroBrowserRegister``：重试、取消、补偿、检查点、失败现场全部由
引擎提供，平台只写步骤。实质变化：

1. **SPA 的手写状态机换成 ``when()`` + ``RetryPolicy``**。旧实现是一个 300 秒的
   ``while`` 循环，自己维护 ``handled_steps`` 集合和 ``hash_stuck_since`` 计时器，
   靠"同一个 hash 停留超 20 秒就把已处理标记摘掉"来重跑卡住的那一步——那就是引擎的
   ``RetryPolicy`` 手写了一遍。现在每个 SPA 步骤的 ``when()`` 读一次活页面的 hash，
   提交后确认 hash 真的变了（``_submit_and_check_progress``），没变就抛 transient
   让引擎原地重试。
2. **"enter-email 重试超 5 次"升成 ``ResourceRejectedError``**。旧实现的报错文案
   自己写着"邮箱域名可能被 AWS 拒绝"，却抛的是裸 ``RuntimeError``——
   ``classify()`` 认作 unknown，任务层既不重试也不换邮箱。
3. **拿不到 accessToken 现在算失败**。旧实现原样返回空串，落库是一个
   ``check_valid`` 必然为 False 的死账号。

结构性限制，以及和旧实现相比还剩的差距：

引擎的游标只前进，回撤只能退到 anchor（``core/registration/steps.py`` 里那条
"回撤目标必须是 anchor 步骤"）——因为浏览器页面状态没法回滚，退到非 anchor 等于
让那一步在一个谁也不知道是什么的页面上重跑。于是 SPA 的四步按
enter-email → enter-name → verify-email → create-password 线性铺开，各自靠 hash
认领（验证码那步为什么不用 ``when()`` 见 ``AwsVerifyEmail`` 的 docstring）。

- **原地卡住**：已覆盖。四个阶段都检查"提交后 hash 变没变"，不变就原地重试
  （enter-email 5 次，其余 3 次），等价于旧实现那个 20 秒 stuck 检测器，而且更快
  （不用先干等 20 秒）也更早给出分类错误。
- **AWS 往回跳**（比如 verify-email 之后又回 enter-email）：**接不住**。线性游标
  已经走过 enter-email，不会再回去；表现是后续步骤等满超时 → transient → 任务层
  换邮箱补投。旧实现靠同一个 stuck 检测器能接住。这里没做的原因是没有证据说明
  AWS 真会这么跳——旧代码那个检测器治的是卡住，不是回跳。真在失败现场包里见到
  一次，再按下面任一条补：
  1. 把 ``AwsEnterEmail`` 等步骤再挂一份到流程后段（``platforms/chatgpt/steps.py``
     的 ``MaybeAddPhone`` 就是这个写法），能接住一轮回跳，代价是流程变长；
  2. 把整个 SPA 收成一个自带循环的步骤，能接住任意轮，代价是丢掉分阶段的重试、
     检查点与失败现场粒度，等于把旧的状态机搬回来；
  3. 给引擎加子流程（自带游标的嵌套 Flow）。这是通解，但它是引擎改动，
     影响所有平台的安全不变量，不该为一个平台顺手做。

页面动作在 dom.py，这里只做编排。
"""
from __future__ import annotations

from core.errors import ConfigError, ResourceRejectedError, SiteChangedError, TransientError
from core.registration.steps import Flow, RetryPolicy, Step, StepContext, StepResult

from . import dom

#: 各阶段的等待窗口，全部照搬旧实现的数字
BUILDER_ID_CLICK_TIMEOUT = 15.0
AWS_SIGNIN_TIMEOUT = 30.0
AWS_EMAIL_INPUT_TIMEOUT = 15.0
AWS_PROFILE_TIMEOUT = 60.0
NAME_INPUT_TIMEOUT = 15.0
OTP_INPUT_TIMEOUT = 30.0
#: 等 SPA 走到 verify-email 那一步（旧实现靠 300 秒大循环兜着）
VERIFY_STEP_TIMEOUT = 60.0
HASH_CHANGE_TIMEOUT = 12.0
KIRO_RETURN_TIMEOUT = 60.0
TOKEN_TIMEOUT = 20.0

#: enter-email 原地重试的次数，同旧实现的 ``enter_email_retries > 5``
ENTER_EMAIL_ATTEMPTS = 5
#: 其余 SPA 步骤"提交后页面没前进"的原地重试次数。
#: 旧实现对所有阶段用的是同一个 20 秒 stuck 检测器 + 无上限重跑（到 300 秒总超时为止），
#: 这里给个明确的上限，免得一个改了版的页面把整个尝试拖满。
SUBMIT_ATTEMPTS = 3


def _wait_past_aws_signin(sc: StepContext) -> None:
    """等 AWS 从认账号页放行——要么进注册 SPA，要么直接回 kiro（老账号）。"""
    sc.poll(
        lambda: dom.is_on_aws_profile(sc.page) or dom.is_on_kiro(sc.page),
        timeout=AWS_PROFILE_TIMEOUT,
        interval=1.0,
    )


def _submit_and_check_progress(sc: StepContext) -> bool:
    """点下一步，返回 hash 是否真的变了。

    AWS 的表单校验失败时不报错也不换页，只是静静地待在原地；不检查的话下一个
    步骤的 ``when()`` 会是 False，整条流程一路跑到最后才以"没拿到 token"收场。
    """
    before = dom.url_hash(sc.page)
    dom.click_submit(sc.page, log=sc.log, should_stop=sc.cancel)
    return bool(
        sc.poll(
            lambda: dom.url_hash(sc.page) != before,
            timeout=HASH_CHANGE_TIMEOUT,
            interval=0.5,
        )
    )


class OpenKiroSignin(Step):
    """打开 Kiro 登录页，选 AWS Builder ID，等跳到 AWS。

    anchor：本步骤从零建立页面状态，因此可以作为回撤目标。这一次尝试要用的姓名
    和兜底密码也在这里定下来——``result_keys`` 要求它们无论后面哪一步被跳过都在
    黑板上。
    """

    name = "OpenKiroSignin"
    # password 同时是 reads 和 writes：读到空才生成，回撤时引擎据此保留当前值
    reads = ("password",)
    writes = ("password", "name")
    anchor = True

    def run(self, sc: StepContext) -> StepResult:
        if not sc.state.get("password"):
            sc.state["password"] = dom.random_password()
        sc.state["name"] = dom.random_name()

        sc.log("打开 Kiro 登录页")
        sc.page.goto(dom.KIRO_SIGNIN_URL, wait_until="domcontentloaded", timeout=30000)
        sc.sleep(dom.SETTLE_SECONDS)

        sc.log("选择 AWS Builder ID 登录方式")
        dom.click_text(
            sc.page,
            dom.BUILDER_ID_TEXTS,
            log=sc.log,
            timeout=BUILDER_ID_CLICK_TIMEOUT,
            should_stop=sc.cancel,
        )
        sc.sleep(dom.SETTLE_SECONDS)

        # Kiro 的选项卡 UI 有时还要再点一次箭头才真的跳走；点不到也不算错
        dom.click_submit(sc.page, log=sc.log, timeout=5, should_stop=sc.cancel)
        sc.sleep(dom.SETTLE_SECONDS)

        sc.log("等待 AWS 登录页...")
        sc.poll(
            lambda: dom.is_on_aws_signin(sc.page) or dom.is_on_aws_profile(sc.page),
            timeout=AWS_SIGNIN_TIMEOUT,
            interval=1.0,
        )
        if not (dom.is_on_aws_signin(sc.page) or dom.is_on_aws_profile(sc.page)):
            raise SiteChangedError(f"未跳转到 AWS 登录页: {dom.page_url(sc.page)}")

        sc.sleep(dom.SETTLE_SECONDS)
        return StepResult.ok()


class MaybeSubmitAwsEmail(Step):
    """signin.aws 的"先报上邮箱"页。新账号会被送进注册 SPA。"""

    name = "MaybeSubmitAwsEmail"
    reads = ("email",)

    def when(self, sc: StepContext) -> bool:
        return dom.is_on_aws_signin(sc.page)

    def run(self, sc: StepContext) -> StepResult:
        email = str(sc.state.get("email") or "")
        if not email:
            raise ConfigError("Kiro 注册需要邮箱地址，但身份里没有")

        sc.log(f"填写邮箱: {email}")
        if not dom.fill_input_wait(
            sc.page, dom.EMAIL_INPUT_SELECTORS, email,
            timeout=AWS_EMAIL_INPUT_TIMEOUT, should_stop=sc.cancel,
        ):
            raise SiteChangedError(f"未找到 AWS 邮箱输入框: {dom.page_url(sc.page)}")

        sc.sleep(0.5)
        dom.click_submit(sc.page, log=sc.log, should_stop=sc.cancel)
        sc.sleep(3)

        sc.log("等待进入 AWS 注册流程...")
        _wait_past_aws_signin(sc)
        return StepResult.ok()


class MaybeAwsPassword(Step):
    """还停在 signin.aws = 这个邮箱 AWS 已经认识了，走的是登录的密码页。"""

    name = "MaybeAwsPassword"
    reads = ("password",)

    def when(self, sc: StepContext) -> bool:
        return dom.is_on_aws_signin(sc.page)

    def run(self, sc: StepContext) -> StepResult:
        sc.log("检测到密码输入页，填写密码...")
        dom.fill_input_wait(
            sc.page, dom.PASSWORD_INPUT_SELECTORS, str(sc.state.get("password") or ""),
            timeout=10, should_stop=sc.cancel,
        )
        sc.sleep(0.5)
        dom.click_submit(sc.page, log=sc.log, timeout=5, should_stop=sc.cancel)
        sc.sleep(3)
        _wait_past_aws_signin(sc)
        return StepResult.ok()


class AwsEnterEmail(Step):
    """SPA 第一步：确认邮箱 + 填姓名（AWS 把两样放在同一页）。

    ``max_attempts=5`` 复刻旧实现的 ``enter_email_retries > 5``：AWS 对这一步的
    校验失败是静默的，只能靠"提交后 hash 没变"发现，重试就是重填重交。
    """

    name = "AwsEnterEmail"
    reads = ("email", "name")
    # 次数只能记在黑板上：Flow.validate 不允许 reads 里出现"本步骤自己才写"的键，
    # 所以只声明 writes，读的时候 .get 兜底
    writes = ("enter_email_attempts",)
    retry = RetryPolicy(max_attempts=ENTER_EMAIL_ATTEMPTS, categories=("transient",))

    def when(self, sc: StepContext) -> bool:
        return dom.current_step(sc.page) == dom.STEP_ENTER_EMAIL

    def run(self, sc: StepContext) -> StepResult:
        attempt = int(sc.state.get("enter_email_attempts") or 0) + 1
        sc.state["enter_email_attempts"] = attempt
        sc.log(f"AWS 步骤: 确认邮箱 + 填写姓名 (第{attempt}次)")
        sc.sleep(dom.RENDER_SECONDS)

        # 邮箱通常 AWS 自己已经带过来了，空着才填
        element, _ = dom.find_first_visible(sc.page, dom.EMAIL_INPUT_SELECTORS)
        if element is not None:
            try:
                if not (element.input_value() or ""):
                    element.fill(str(sc.state.get("email") or ""))
            except Exception:
                pass

        dom.fill_name(
            sc.page, str(sc.state.get("name") or ""),
            log=sc.log, timeout=NAME_INPUT_TIMEOUT, should_stop=sc.cancel,
        )
        sc.sleep(0.5)
        if _submit_and_check_progress(sc):
            return StepResult.ok()

        if attempt >= ENTER_EMAIL_ATTEMPTS:
            # 试满了还在原地 = AWS 不收这个邮箱域名。声明的 retry 只认 transient，
            # 所以这条不会被原地重试，直接结束尝试让任务层换邮箱补投。
            raise ResourceRejectedError(
                f"AWS 反复拒绝 {sc.state.get('email')}，邮箱域名可能在黑名单上: {dom.page_url(sc.page)}"
            )
        raise TransientError(f"AWS {dom.STEP_ENTER_EMAIL} 提交后页面没有前进: {dom.page_url(sc.page)}")


class AwsEnterName(Step):
    """SPA 第二步：单独的姓名页（邮箱页没一起问的时候才出现）。

    和 ``AwsEnterEmail`` 一样要确认"提交后 hash 真的变了"：AWS 的校验失败是静默的，
    不检查的话下一步会在原地等满超时才报错，而那时报的是"没走到验证码步骤"——
    一个既误导又不可重试的结论。
    """

    name = "AwsEnterName"
    reads = ("name",)
    retry = RetryPolicy(max_attempts=SUBMIT_ATTEMPTS, categories=("transient",))

    def when(self, sc: StepContext) -> bool:
        return dom.current_step(sc.page) == dom.STEP_ENTER_NAME

    def run(self, sc: StepContext) -> StepResult:
        sc.log("AWS 步骤: 填写姓名")
        sc.sleep(dom.RENDER_SECONDS)
        dom.fill_name(
            sc.page, str(sc.state.get("name") or ""),
            log=sc.log, timeout=NAME_INPUT_TIMEOUT, should_stop=sc.cancel,
        )
        if not _submit_and_check_progress(sc):
            raise TransientError(f"AWS {dom.STEP_ENTER_NAME} 提交后页面没有前进: {dom.page_url(sc.page)}")
        sc.sleep(dom.SETTLE_SECONDS)
        return StepResult.ok()


class AwsVerifyEmail(Step):
    """SPA 第三步：邮箱验证码。

    **故意不重写 ``when()``**。``from_step_flow`` 按"是否重写 when"判可选阶段，
    重写了这个渠道就从 required 降成 conditional，前端于是不再要求 Kiro 任务配
    接码邮箱——可新账号注册每次都要验证码，那是错的。这里的"可能不跑"只有一种
    情况：这个邮箱 AWS 已经认识，压根没进注册 SPA；那不是"阶段可选"，
    是"这次根本不是在注册"，所以写成 ``run()`` 里的 skip 而不是守卫。

    ``max_attempts=1``：原地重试会再向邮箱要一封**新**邮件，而 AWS 只发过一封，
    于是白等满一个 OTP 超时才失败。
    """

    name = "AwsVerifyEmail"
    requires = ("otp",)
    retry = RetryPolicy(max_attempts=1)

    def run(self, sc: StepContext) -> StepResult:
        if dom.is_on_kiro(sc.page):
            sc.log("已经回到 Kiro，说明这个邮箱是老账号直接登录，跳过验证码")
            return StepResult.skip("已有账号直接登录")

        sc.poll(
            lambda: dom.current_step(sc.page) == dom.STEP_VERIFY_EMAIL or dom.is_on_kiro(sc.page),
            timeout=VERIFY_STEP_TIMEOUT,
            interval=1.0,
        )
        if dom.is_on_kiro(sc.page):
            return StepResult.skip("已有账号直接登录")
        if dom.current_step(sc.page) != dom.STEP_VERIFY_EMAIL:
            raise SiteChangedError(f"AWS 没有走到验证码步骤: {dom.page_url(sc.page)}")

        sc.log("AWS 步骤: 填写验证码")
        element = dom.wait_for_visible_input(
            sc.page, dom.OTP_INPUT_SELECTORS, timeout=OTP_INPUT_TIMEOUT, interval=1.0, should_stop=sc.cancel
        )
        if element is None:
            raise SiteChangedError(f"未出现 AWS 验证码输入框: {dom.page_url(sc.page)}")

        otp_callback = sc.require("otp")
        if otp_callback is None:
            raise ConfigError("Kiro 注册需要邮箱验证码，但未配置 mailbox provider")

        code = sc.call_with_cancel(otp_callback)
        if not code:
            raise TransientError("未获取到 Kiro 邮箱验证码")

        sc.log(f"填写验证码: {code}")
        element.click()
        dom.type_digits(sc.page, code, should_stop=sc.cancel)
        sc.sleep(0.5)
        dom.click_submit(sc.page, log=sc.log, timeout=5, should_stop=sc.cancel)
        sc.sleep(dom.SETTLE_SECONDS)
        return StepResult.ok()


class AwsCreatePassword(Step):
    """SPA 第四步：设密码。有的投放不问，问了就是"密码 + 确认密码"两个框。

    只有真填了密码才要求页面前进：一个框都没有时旧实现也只是把这一步标记掉、
    什么都不提交，那种情况下"hash 没变"是正常的。
    """

    name = "AwsCreatePassword"
    reads = ("password",)
    retry = RetryPolicy(max_attempts=SUBMIT_ATTEMPTS, categories=("transient",))

    def when(self, sc: StepContext) -> bool:
        return dom.current_step(sc.page) == dom.STEP_CREATE_PASSWORD

    def run(self, sc: StepContext) -> StepResult:
        sc.log("AWS 步骤: 设置密码")
        sc.sleep(1)
        if not dom.fill_all_password_fields(sc.page, str(sc.state.get("password") or ""), should_stop=sc.cancel):
            sc.log("密码页没有可填的输入框，跳过提交")
            return StepResult.ok()

        if not _submit_and_check_progress(sc):
            raise TransientError(f"AWS {dom.STEP_CREATE_PASSWORD} 提交后页面没有前进: {dom.page_url(sc.page)}")
        sc.sleep(dom.SETTLE_SECONDS)
        return StepResult.ok()


class AwaitKiroReturn(Step):
    """等 AWS 把我们送回 app.kiro.dev——回去了才说明账号真的建成了。"""

    name = "AwaitKiroReturn"
    retry = RetryPolicy(max_attempts=1)

    def run(self, sc: StepContext) -> StepResult:
        sc.log("等待跳回 Kiro...")
        sc.poll(lambda: dom.is_on_kiro(sc.page), timeout=KIRO_RETURN_TIMEOUT, interval=1.0)
        if not dom.is_on_kiro(sc.page):
            raise TransientError(f"Kiro 注册未跳转回应用: {dom.page_url(sc.page)}")
        sc.sleep(3)
        return StepResult.ok()


class ExtractTokens(Step):
    """从 localStorage 取 Cognito token。

    旧实现拿不到就返回空串，落库是一个 ``check_valid`` 必然为 False 的死账号；
    现在按 transient 失败，交给任务层补投。
    """

    name = "ExtractTokens"
    writes = ("accessToken", "refreshToken", "idToken")
    retry = RetryPolicy(max_attempts=2, backoff_seconds=3.0, categories=("transient",))

    def run(self, sc: StepContext) -> StepResult:
        sc.log("提取 Kiro 访问令牌...")
        tokens = dom.wait_for_cognito_tokens(sc.page, timeout=TOKEN_TIMEOUT, should_stop=sc.cancel)
        if not tokens.get("accessToken"):
            raise TransientError(f"未从 localStorage 取到 Kiro accessToken: {dom.page_url(sc.page)}")

        for key in self.writes:
            sc.state[key] = str(tokens.get(key) or "")
        sc.log(f"注册成功: {sc.state.get('email')}")
        return StepResult.ok()


def build_flow() -> Flow:
    return Flow(
        "kiro.browser_signup",
        [
            OpenKiroSignin(),
            MaybeSubmitAwsEmail(),
            MaybeAwsPassword(),
            AwsEnterEmail(),
            AwsEnterName(),
            AwsVerifyEmail(),
            AwsCreatePassword(),
            AwaitKiroReturn(),
            ExtractTokens(),
        ],
        initial_keys=("email", "password"),
        # sessionToken / csrfToken / clientId / clientSecret 不在这里：
        # 旧实现返回的就是空串，_map_kiro_result 已经用 .get(..., "") 兜底
        result_keys=("email", "password", "name", "accessToken", "refreshToken", "idToken"),
    )
