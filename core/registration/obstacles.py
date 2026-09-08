"""可复用的横切干扰处理器。

Turnstile、全页拦截这类东西在流程里出现的位置不固定（Cursor 注册页会弹一次、
密码页后可能再弹一次），做成 Step 就得在每个可能的位置都插一遍；做成
ObstacleHandler 由引擎在每步前后自动探测，新增题型只加 handler，不动任何 Step。

这里的两个 handler 用选择器回调参数化，各平台传入自己的探测/点击函数即可复用。
"""
from __future__ import annotations

import time
from typing import Callable

from core.base_captcha import CaptchaChallenge, ChallengeKind
from core.errors import AttemptCancelled, RiskControlError, is_fatal

from .steps import CONFIRM, InteractionRequest, Obstacle, ObstacleHandler, StepContext

TURNSTILE = "turnstile"
FULL_PAGE_BLOCK = "full_page_block"


class TurnstileObstacleHandler(ObstacleHandler):
    """解决链：页内点 checkbox → 外部 solver 注入 token → 人工确认。

    页内点击优先是有原因的：外部 solver 自己开浏览器拿到的 token，
    与本地这次会话的指纹不匹配时经常被判无效。
    """

    name = TURNSTILE
    writes = ("turnstile_token",)

    def __init__(
        self,
        *,
        is_visible: Callable[[object], bool],
        click_in_iframe: Callable[[object, Callable[[str], None]], bool] | None = None,
        get_sitekey: Callable[[object], str] | None = None,
        inject_token: Callable[[object, str], bool] | None = None,
        on_injected: Callable[[object], None] | None = None,
        default_sitekey: str = "",
        settle_seconds: float = 3.0,
        human_wait_seconds: float = 90.0,
        poll_seconds: float = 0.0,
        settled: Callable[[object, str], bool] | None = None,
        poll_before_steps: tuple[str, ...] = (),
        detect_before_steps: tuple[str, ...] | None = None,
    ):
        self._is_visible = is_visible
        self._click_in_iframe = click_in_iframe
        self._get_sitekey = get_sitekey
        self._inject_token = inject_token
        self._on_injected = on_injected
        self._default_sitekey = default_sitekey
        self._settle = settle_seconds
        self._human_wait = human_wait_seconds
        # 提交表单后 Turnstile 可能晚几秒才渲染：在指定步骤前多等一会，
        # 但页面一旦进入下一阶段（settled(page, step)）就立刻放行，不为没出现的挑战白等
        self._poll = max(0.0, float(poll_seconds))
        self._settled = settled
        self._poll_before_steps = tuple(poll_before_steps)
        # 只在这些步骤之前探测（失败后的重试前一律探测）：注册页可能预渲染 widget 的 iframe，
        # 填表之前就去"解"它只会在空表单上点提交
        self._detect_before_steps = tuple(detect_before_steps) if detect_before_steps is not None else self._poll_before_steps

    # ------------------------------------------------------------------
    def detect(self, sc: StepContext) -> Obstacle | None:
        page = sc.page
        if page is None:
            return None
        phase = str(getattr(sc, "obstacle_phase", "") or "before")
        if phase == "before" and self._detect_before_steps and sc.current_step not in self._detect_before_steps:
            return None
        if self._is_visible(page):
            return Obstacle(self.name, {"page_url": _page_url(page)})

        should_poll = (
            phase == "before"
            and self._poll > 0
            and (not self._poll_before_steps or sc.current_step in self._poll_before_steps)
        )
        if not should_poll:
            return None
        deadline = time.time() + self._poll
        while time.time() < deadline:
            sc.cancel.raise_if_cancelled()
            if self._settled is not None:
                try:
                    if self._settled(page, sc.current_step):
                        return None
                except Exception:
                    pass
            time.sleep(0.5)
            if self._is_visible(page):
                return Obstacle(self.name, {"page_url": _page_url(page)})
        return None

    def resolve(self, sc: StepContext, obstacle: Obstacle) -> bool:
        page = sc.page
        url_before = str(obstacle.detail.get("page_url") or _page_url(page))

        if self._click_in_iframe is not None:
            try:
                if self._click_in_iframe(page, sc.log):
                    # 点完 checkbox 到通过通常要几秒：多等一会再判定，别急着去花钱调 solver
                    if self._wait_cleared(sc, page, url_before, self._settle + 5.0):
                        return True
            except AttemptCancelled:
                raise
            except Exception as exc:
                sc.log(f"Turnstile 页内点击失败: {exc}", level="warning")

        token = self._solve_with_provider(sc, page)
        if token and self._inject_token is not None:
            sc.state["turnstile_token"] = token
            if self._inject_token(page, token):
                if self._on_injected is not None:
                    self._on_injected(page)
                if self._wait_cleared(sc, page, url_before, self._settle):
                    return True

        return self._wait_for_human(sc, page, url_before)

    def _cleared(self, page, url_before: str) -> bool:
        """挑战已过：widget 不再可见，或页面已经离开了出现挑战的那个 URL。

        通过后 widget 的 iframe 往往在导航前仍留在 DOM 里，只看"是否可见"会误判成
        没过、再去调一次付费 solver 并在错误的页面上点提交。
        """
        if not self._is_visible(page):
            return True
        current_url = _page_url(page)
        return bool(url_before and current_url and current_url != url_before)

    def _wait_cleared(self, sc: StepContext, page, url_before: str, seconds: float) -> bool:
        deadline = time.time() + max(0.0, seconds)
        while True:
            if self._cleared(page, url_before):
                return True
            if time.time() >= deadline:
                return False
            sc.cancel.raise_if_cancelled()
            time.sleep(0.5)

    # ------------------------------------------------------------------
    def _solve_with_provider(self, sc: StepContext, page) -> str:
        try:
            solver = sc.require("captcha")
        except Exception as exc:
            # headless 下没有 solver 就没有任何过关手段：配置错误直接停批，
            # 不要每次尝试都开浏览器、填表、撞 Turnstile 再按风控失败补投一轮
            if is_fatal(exc) and str(getattr(sc.ctx, "executor_type", "") or "") != "headed":
                raise
            sc.log(f"未取得验证码 provider: {exc}", level="warning")
            return ""
        if solver is None:
            return ""

        sitekey = ""
        if self._get_sitekey is not None:
            try:
                sitekey = self._get_sitekey(page) or ""
            except Exception:
                sitekey = ""
        sitekey = sitekey or self._default_sitekey

        try:
            solution = solver.solve(
                CaptchaChallenge(
                    kind=ChallengeKind.TURNSTILE,
                    page_url=str(getattr(page, "url", "") or ""),
                    site_key=sitekey,
                )
            )
            return str(getattr(solution, "value", "") or "")
        except AttemptCancelled:
            raise
        except Exception as exc:
            sc.log(f"外部 solver 解题失败: {exc}", level="warning")
            return ""

    def _wait_for_human(self, sc: StepContext, page, url_before: str = "") -> bool:
        """最后一级兜底。

        只有 headed（操作员看得见浏览器）才值得等人：有人工通道就明确问一句，
        没有就盲等。headless 下没人能点那个 checkbox，挂着等 15 分钟只会占死并发槽，
        直接按风控失败抛出，让任务层换代理重跑。
        """
        executor_type = str(getattr(sc.ctx, "executor_type", "") or "")
        if executor_type != "headed":
            raise RiskControlError("Turnstile 自动解题失败（headless 无法人工介入）")

        if sc.human is not None:
            sc.ask(
                InteractionRequest(
                    kind=CONFIRM,
                    prompt="请在浏览器中完成 Turnstile 验证后点击确认",
                    context={"page_url": _page_url(page)},
                    screenshot=True,
                )
            )
            return self._cleared(page, url_before)

        sc.log(f"⚠️ 自动解题失败，等待人工通过（最多 {int(self._human_wait)} 秒）...", level="warning")
        deadline = time.time() + self._human_wait
        while time.time() < deadline:
            sc.cancel.raise_if_cancelled()
            if self._cleared(page, url_before):
                return True
            time.sleep(2)
        return False


class FullPageBlockHandler(ObstacleHandler):
    """Cloudflare 全页拦截：等它自己放行（含鼠标轨迹模拟）。"""

    name = FULL_PAGE_BLOCK

    def __init__(
        self,
        *,
        is_blocked: Callable[[object], bool],
        wait_clear: Callable[..., bool],
        timeout: float = 120.0,
    ):
        self._is_blocked = is_blocked
        self._wait_clear = wait_clear
        self._timeout = timeout

    def detect(self, sc: StepContext) -> Obstacle | None:
        if sc.page is None or not self._is_blocked(sc.page):
            return None
        return Obstacle(self.name, {"page_url": _page_url(sc.page)})

    def resolve(self, sc: StepContext, obstacle: Obstacle) -> bool:
        # 等待函数本身不承诺返回值，放行与否以拦截页是否还在为准
        self._wait_clear(sc.page, timeout=self._timeout, log_fn=sc.log)
        return not self._is_blocked(sc.page)


def _page_url(page) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""
