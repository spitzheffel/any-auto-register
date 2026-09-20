"""Step 流程引擎。

把"一段整体式的注册脚本"拆成带元数据的步骤，引擎负责所有横切能力：
按错误类别重试、回撤到 anchor、步间取消、逆序补偿、检查点、三层日志、
以及把 Turnstile / 全页拦截这类随时可能弹出的干扰交给 ObstacleHandler。

数据契约只到键名（reads/writes），不做类型 schema：浏览器流程里步骤之间
最主要的状态是页面本身，那部分永远声明不了（由 anchor 承担），
契约只覆盖黑板里那一小部分——值得做，但不值得重。
"""
from __future__ import annotations

import gzip
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence
from urllib.parse import urlsplit

from core.cancellation import (
    NEVER,
    CancellationToken,
    accepts_should_stop,
    cancellable_sleep,
    poll_until,
)
from core.errors import (
    AttemptCancelled,
    ConfigError,
    RegistrationFailure,
    TransientError,
    classify,
    is_fatal,
)
from core.requirements import CAPABILITY_CHANNEL

from .dom import find_first_selector, find_first_visible

from .runtime import call_log

#: 辅助浏览器会话在 sc.handles 里的键前缀
AUX_BROWSER_PREFIX = "aux_browser:"

# 事件 detail 与检查点里一律脱敏
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "token",
        "session_token",
        "access_token",
        "refresh_token",
        "otp_code",
        "sms_code",
        "api_key",
        "auth_code",
        "phone_number",
    }
)


class StepContractError(ConfigError):
    """步骤没兑现自己声明的 reads/writes —— 实现 bug，不重试。"""


class RewindBudgetExhausted(TransientError):
    """回撤次数用尽。

    回撤是为"验证码没来 / 页面停在错误状态"这类瞬态问题准备的页面级恢复，预算用完
    说明这次尝试的环境（邮箱、号码、代理）不顺，换一次尝试通常就好——按 transient
    分类让任务层补投，而不是当未知失败占掉一个名额。
    """


# ---------------------------------------------------------------------------
# 重试策略
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 1          # 含首次执行
    backoff_seconds: float = 0.0
    categories: tuple[str, ...] = ()   # 空 = 任何可重试类别都适用


# 未声明 retry 的步骤按错误类别套用的默认表
DEFAULT_RETRY_BY_CATEGORY: dict[str, RetryPolicy] = {
    "transient": RetryPolicy(max_attempts=3, backoff_seconds=2.0),
    "resource_rejected": RetryPolicy(max_attempts=2, backoff_seconds=0.0),
    "risk_control": RetryPolicy(max_attempts=2, backoff_seconds=0.0),
    "provider": RetryPolicy(max_attempts=2, backoff_seconds=1.0),
}


def retry_policy_for(step: "Step", error: BaseException) -> RetryPolicy | None:
    """决定某次失败要不要原地重试。"""
    if is_fatal(error):
        return None
    category = classify(error)
    if step.retry is not None:
        if step.retry.categories and category not in step.retry.categories:
            return None
        return step.retry
    return DEFAULT_RETRY_BY_CATEGORY.get(category)


# ---------------------------------------------------------------------------
# 步骤结果
# ---------------------------------------------------------------------------

OK = "ok"
SKIP = "skip"
FAIL = "fail"
REWIND = "rewind"


@dataclass(slots=True)
class StepResult:
    outcome: str = OK
    error: BaseException | None = None
    rewind_to: str = ""
    reason: str = ""

    @classmethod
    def ok(cls) -> "StepResult":
        return cls(OK)

    @classmethod
    def skip(cls, reason: str = "") -> "StepResult":
        return cls(SKIP, reason=reason)

    @classmethod
    def fail(cls, error: BaseException) -> "StepResult":
        return cls(FAIL, error=error)

    @classmethod
    def rewind(cls, to: str, reason: str = "") -> "StepResult":
        return cls(REWIND, rewind_to=to, reason=reason)


# ---------------------------------------------------------------------------
# 步骤
# ---------------------------------------------------------------------------

class Step(ABC):
    #: 步骤名，同时是日志、回撤目标、按步骤绑渠道的标识
    name: str = ""
    #: 依赖的能力（captcha / phone / otp / link / browser ...）
    requires: tuple[str, ...] = ()
    #: 黑板数据契约
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    #: 可作为回撤目标——必须能从零建立页面状态（典型是带导航的步骤）
    anchor: bool = False
    #: 未声明时按错误类别套默认表
    retry: RetryPolicy | None = None

    def when(self, sc: "StepContext") -> bool:
        """守卫：返回 False 时跳过（可选阶段）。"""
        return True

    @abstractmethod
    def run(self, sc: "StepContext") -> StepResult:
        ...

    def compensate(self, sc: "StepContext") -> None:
        """失败 / 回撤时逆序调用，释放本步骤占用的资源。"""
        return None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Step {self.name}>"


class Flow:
    """一条步骤流水线 + 数据契约。"""

    def __init__(
        self,
        name: str,
        steps: Sequence[Step],
        *,
        result_keys: tuple[str, ...] = (),
        initial_keys: tuple[str, ...] = (),
    ):
        self.name = name
        self.steps = list(steps)
        self.result_keys = tuple(result_keys)
        self.initial_keys = tuple(initial_keys)
        self.validate()

    # ------------------------------------------------------------------
    def validate(self) -> None:
        """构建期契约校验：读的键必须有人写过。"""
        seen = set(self.initial_keys)
        names: set[str] = set()
        for step in self.steps:
            if not step.name:
                raise StepContractError(f"流程 {self.name} 中存在未命名步骤: {step!r}")
            if step.name in names:
                raise StepContractError(f"流程 {self.name} 中步骤名重复: {step.name}")
            names.add(step.name)

            missing = [key for key in step.reads if key not in seen]
            if missing:
                raise StepContractError(
                    f"流程 {self.name} 步骤 {step.name} 读取了没有来源的键: {missing}"
                    f"（可用: {sorted(seen)}）"
                )
            seen.update(step.writes)

        missing_results = [key for key in self.result_keys if key not in seen]
        if missing_results:
            raise StepContractError(
                f"流程 {self.name} 声明的结果键无人写出: {missing_results}"
            )

    def index_of(self, step_name: str) -> int:
        for index, step in enumerate(self.steps):
            if step.name == step_name:
                return index
        raise StepContractError(f"流程 {self.name} 中不存在步骤: {step_name}")

    def anchors(self) -> list[str]:
        return [step.name for step in self.steps if step.anchor]


# ---------------------------------------------------------------------------
# 人工介入
# ---------------------------------------------------------------------------

CONFIRM = "confirm"
INPUT = "input"
CHOICE = "choice"
CAPTCHA_KIND = "captcha"

ON_TIMEOUT_ABORT = "abort"
ON_TIMEOUT_SKIP = "skip"
ON_TIMEOUT_RETRY = "retry"


@dataclass(slots=True)
class InteractionRequest:
    kind: str = CONFIRM
    prompt: str = ""
    fields: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    context: dict[str, Any] = field(default_factory=dict)
    timeout: int = 900              # 挂起会占用并发槽位，必须有超时
    on_timeout: str = ON_TIMEOUT_ABORT
    screenshot: bool = False


@dataclass(slots=True)
class InteractionResponse:
    value: str = ""
    values: dict[str, str] = field(default_factory=dict)
    option: str = ""
    timed_out: bool = False
    cancelled: bool = False


class HumanChannel(Protocol):
    """把问题推给操作员并阻塞等待回答。"""

    def ask(self, request: InteractionRequest, *, cancel: CancellationToken) -> InteractionResponse:
        ...


class InMemoryHumanChannel:
    """内存实现：预置答案，供单测与 headed 调试。"""

    def __init__(self, answers: Iterable[InteractionResponse] | None = None):
        self.answers = list(answers or [])
        self.asked: list[InteractionRequest] = []

    def ask(self, request: InteractionRequest, *, cancel: CancellationToken = NEVER) -> InteractionResponse:
        self.asked.append(request)
        if cancel.is_cancelled():
            return InteractionResponse(cancelled=True)
        if self.answers:
            return self.answers.pop(0)
        return InteractionResponse(timed_out=True)

    def ask_captcha(self, prompt: str, challenge) -> str:
        """ManualCaptcha 用的便捷入口。"""
        response = self.ask(
            InteractionRequest(
                kind=CAPTCHA_KIND,
                prompt=prompt,
                context={"page_url": getattr(challenge, "page_url", "")},
            )
        )
        return response.value


# ---------------------------------------------------------------------------
# 能力解析
# ---------------------------------------------------------------------------

class CapabilityResolver:
    """能力名 → 构建函数的映射，按需惰性构建并缓存。

    provider key 由 ChannelBindings 按 步骤级 > 任务级 > 平台默认 > 全局默认 解析，
    builder 只负责用解析出来的 key 造实例。缓存按 ``能力:key`` 分片：同一流程里
    同一个 key 只有一个实例（一次尝试一个 PhoneVerification），不同步骤覆写成
    不同 key 时才会各造一个。新增一种能力 = 往映射表里加一项。
    """

    def __init__(
        self,
        ctx,
        *,
        bindings=None,
        builders: dict[str, Callable[[Any, str, str], Any]] | None = None,
        human: HumanChannel | None = None,
    ):
        runtime = getattr(ctx, "runtime", None)
        self.ctx = ctx
        self.bindings = bindings if bindings is not None else getattr(runtime, "bindings", None)
        self.human = human if human is not None else getattr(runtime, "human", None)
        self._builders = dict(builders or default_capability_builders())
        self._cache: dict[str, Any] = {}
        # 解析结果按 (能力, 步骤) 记住：没有显式绑定时 resolve 要查一次全局默认（DB），
        # 每步前的干扰探测都会 require("captcha")，不能每次都查
        self._resolved_keys: dict[tuple[str, str], str] = {}
        #: 实际命中的渠道 key（按渠道类别 mailbox / sms / captcha 记），供检查点与统计消费
        self.channels_used: dict[str, str] = {}

    def register(self, name: str, builder: Callable[[Any, str, str], Any]) -> None:
        self._builders[name] = builder

    def _provider_key_for(self, name: str, step: str) -> str:
        channel = CAPABILITY_CHANNEL.get(name, "")
        if self.bindings is None or not channel:
            return ""
        cache_key = (name, step)
        if cache_key not in self._resolved_keys:
            self._resolved_keys[cache_key] = str(self.bindings.resolve(channel, step=step or None).key or "")
        return self._resolved_keys[cache_key]

    def resolve(self, name: str, *, step: str = "") -> Any:
        builder = self._builders.get(name)
        if builder is None:
            raise StepContractError(f"未知能力: {name}（已注册: {sorted(self._builders)}）")

        channel = CAPABILITY_CHANNEL.get(name, "")
        provider_key = self._provider_key_for(name, step)

        cache_key = f"{name}:{provider_key}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        instance = builder(self.ctx, step, provider_key)
        self._cache[cache_key] = instance
        self._record_channel(channel or name, provider_key or getattr(instance, "provider_key", ""))
        return instance

    def _record_channel(self, channel: str, key: Any) -> None:
        if key:
            self.channels_used[channel] = str(key)

    def close_all(self) -> None:
        """流程收尾：关闭所有带 close() 的能力实例（租约、会话）。"""
        for instance in list(self._cache.values()):
            close = getattr(instance, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception:
                pass
        self._cache.clear()


def default_capability_builders() -> dict[str, Callable[[Any, str, str], Any]]:
    """builder(ctx, step, provider_key)：key 已由 CapabilityResolver 按渠道绑定解析好。"""
    from .helpers import build_link_callback, build_otp_callback, build_phone_verification

    def _human(ctx):
        return getattr(getattr(ctx, "runtime", None), "human", None)

    return {
        "phone": lambda ctx, step, key: build_phone_verification(
            ctx, platform=ctx.platform_name, provider_key=key, human=_human(ctx)
        ),
        "otp": lambda ctx, step, key: build_otp_callback(ctx),
        "link": lambda ctx, step, key: build_link_callback(ctx),
        "captcha": lambda ctx, step, key: ctx.platform._make_captcha(provider_key=key, human=_human(ctx)),
    }


# ---------------------------------------------------------------------------
# 横切干扰
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Obstacle:
    name: str
    detail: dict[str, Any] = field(default_factory=dict)


class ObstacleHandler(ABC):
    """随时可能弹出的干扰（Turnstile、全页拦截、中途要手机号）。

    新增题型或挑战页 = 新增一个 handler，不动任何 Step。
    """

    name: str = ""
    writes: tuple[str, ...] = ()

    @abstractmethod
    def detect(self, sc: "StepContext") -> Obstacle | None:
        ...

    @abstractmethod
    def resolve(self, sc: "StepContext", obstacle: Obstacle) -> bool:
        ...


# ---------------------------------------------------------------------------
# 检查点
# ---------------------------------------------------------------------------

class CheckpointSink(Protocol):
    def write(
        self,
        *,
        attempt_id: str,
        cursor: int,
        step: str,
        state: dict,
        resource_refs: dict,
        channels_used: dict,
        phase: str = "done",
    ) -> None:
        ...


class NullCheckpointSink:
    def write(self, **kwargs) -> None:  # noqa: D401 - 空实现
        return None


class MemoryCheckpointSink:
    """单测用：记录每次写入。"""

    def __init__(self):
        self.writes: list[dict] = []

    def write(self, **kwargs) -> None:
        self.writes.append(dict(kwargs))

    @property
    def last(self) -> dict:
        return self.writes[-1] if self.writes else {}


# ---------------------------------------------------------------------------
# 失败现场
# ---------------------------------------------------------------------------

class NetworkRecorder:
    """同源 XHR/fetch 的有界环形缓冲。

    平台自己 API 的 429 / error_code 是最直接的失败分类判据，比在 DOM 里
    猜报错文案可靠得多。
    """

    RESOURCE_TYPES = ("xhr", "fetch")

    def __init__(self, *, capacity: int = 50, body_limit: int = 8192):
        self.capacity = capacity
        self.body_limit = body_limit
        self.entries: list[dict] = []
        self._page = None

    def attach(self, page) -> None:
        if page is None:
            return
        self._page = page
        try:
            page.on("response", self._on_response)
        except Exception:
            pass

    def _wanted(self, response) -> bool:
        """只留同源 XHR/fetch：脚本、图片、字体会把 50 条环形缓冲冲得只剩噪音。"""
        request = getattr(response, "request", None)
        resource_type = str(getattr(request, "resource_type", "") or "").lower()
        if resource_type and resource_type not in self.RESOURCE_TYPES:
            return False
        page_url = _safe_page_url(self._page)
        url = str(getattr(response, "url", "") or "")
        if not page_url or not url:
            return True
        return urlsplit(url).netloc == urlsplit(page_url).netloc

    def _on_response(self, response) -> None:
        try:
            if not self._wanted(response):
                return
            entry = {
                "url": str(getattr(response, "url", ""))[:500],
                "status": int(getattr(response, "status", 0) or 0),
                "at": time.time(),
            }
            try:
                entry["body"] = str(response.text())[: self.body_limit]
            except Exception:
                entry["body"] = ""
            self.entries.append(entry)
            if len(self.entries) > self.capacity:
                del self.entries[0 : len(self.entries) - self.capacity]
        except Exception:
            pass


class ArtifactCollector:
    """步骤失败时采集现场包（截图 / DOM / 网络 / 控制台 / 元信息）。"""

    def __init__(self, root: Path | str, *, network: NetworkRecorder | None = None):
        self.root = Path(root)
        self.network = network
        self.console: list[dict] = []

    def attach(self, page) -> None:
        if self.network is not None:
            self.network.attach(page)
        if page is None:
            return
        try:
            page.on("console", self._on_console)
        except Exception:
            pass

    def _on_console(self, message) -> None:
        try:
            self.console.append(
                {
                    "type": str(getattr(message, "type", "")),
                    "text": str(getattr(message, "text", ""))[:1000],
                }
            )
            if len(self.console) > 50:
                del self.console[0 : len(self.console) - 50]
        except Exception:
            pass

    def capture(self, sc: "StepContext", step: Step, attempt: int, error: BaseException | None) -> str:
        directory = self.root / f"{step.name}-{attempt}"
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception:
            return ""

        page = sc.page
        if page is not None:
            try:
                page.screenshot(path=str(directory / "screenshot.png"))
            except Exception:
                pass
            try:
                html = page.content()
                with gzip.open(directory / "page.html.gz", "wt", encoding="utf-8") as handle:
                    handle.write(html)
            except Exception:
                pass

        aux_pages = dict(getattr(sc, "aux_pages", None) or {})
        for aux_name, aux_page in aux_pages.items():
            safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in str(aux_name))
            try:
                aux_page.screenshot(path=str(directory / f"screenshot-{safe}.png"))
            except Exception:
                pass
            try:
                with gzip.open(directory / f"page-{safe}.html.gz", "wt", encoding="utf-8") as handle:
                    handle.write(aux_page.content())
            except Exception:
                pass

        self._dump(directory / "network.json", (self.network.entries if self.network else []))
        self._dump(directory / "console.json", self.console)
        self._dump(
            directory / "meta.json",
            {
                "step": step.name,
                "attempt": attempt,
                "category": classify(error) if error else "",
                "error": str(error) if error else "",
                "page_url": _safe_page_url(page),
                "state": redact(sc.state),
                "channels_used": dict(getattr(sc.resolver, "channels_used", {}) or {}),
                "aux_pages": {name: _safe_page_url(p) for name, p in aux_pages.items()},
            },
        )
        return str(directory)

    @staticmethod
    def _dump(path: Path, payload: Any) -> None:
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        except Exception:
            pass


def prune_artifacts(root: Path | str, *, keep: int = 20, max_age_days: int = 14) -> int:
    """保留策略：root 下最近 keep 个尝试目录 + TTL（按目录 mtime），返回删除数量。

    目录是按尝试（attempt_id）划分的，不区分平台与步骤；由服务启动与每次任务结束时调用。
    """
    import shutil

    root = Path(root)
    if not root.exists():
        return 0
    removed = 0
    cutoff = time.time() - max_age_days * 86400
    for attempt_dir in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
        if not attempt_dir.is_dir():
            continue
        if attempt_dir.stat().st_mtime < cutoff:
            shutil.rmtree(attempt_dir, ignore_errors=True)
            removed += 1

    survivors = [item for item in root.iterdir() if item.is_dir()]
    survivors.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in survivors[keep:]:
        shutil.rmtree(stale, ignore_errors=True)
        removed += 1
    return removed


def redact(state: dict) -> dict:
    """事件 / meta 里把敏感值降级为"存在与否"。"""
    result: dict[str, Any] = {}
    for key, value in (state or {}).items():
        if key in SENSITIVE_KEYS:
            result[key] = "<set>" if value else "<empty>"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        else:
            result[key] = f"<{type(value).__name__}>"
    return result


def _safe_page_url(page) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 步骤上下文
# ---------------------------------------------------------------------------

class StepContext:
    def __init__(
        self,
        ctx,
        *,
        page=None,
        cancel: CancellationToken | None = None,
        resolver: CapabilityResolver | None = None,
        human: HumanChannel | None = None,
        state: dict | None = None,
        attempt_id: str = "",
        launch_spec: Any = None,
    ):
        runtime = getattr(ctx, "runtime", None)
        self.ctx = ctx
        self.runtime = runtime
        self.page = page
        self.cancel = cancel or getattr(runtime, "cancel", None) or NEVER
        self.human = human if human is not None else getattr(runtime, "human", None)
        self.resolver = resolver or CapabilityResolver(ctx, human=self.human)
        #: 可序列化黑板（进检查点）
        self.state: dict[str, Any] = dict(state or {})
        #: 不可序列化的活对象：lease、solver 实例、浏览器会话
        self.handles: dict[str, Any] = {}
        #: core.browser_engine.BrowserLaunchSpec —— 主会话的启动参数，开辅助会话要用
        self.launch_spec = launch_spec
        #: 辅助浏览器的页面（名字 -> page），失败现场包会一并截图
        self.aux_pages: dict[str, Any] = {}
        #: 资源引用（activation_id / 邮箱账号），供重启后回收
        self.resource_refs: dict[str, Any] = dict(getattr(runtime, "resource_refs", None) or {})
        self.attempt_id = attempt_id or str(getattr(runtime, "attempt_id", "") or "")
        self.current_step = ""
        #: 干扰探测当前处于哪个阶段（before / after_failure），供 handler 决定要不要多等
        self.obstacle_phase = ""
        # 结构化日志直接走任务层入口；没有运行时（脚本直调 / 单测）退到 ctx.log
        self._log_fn = getattr(runtime, "log", None) or getattr(ctx, "log", print)
        # 由 StepRunner 注入：步骤中途占用外部资源后立刻落检查点
        self._checkpoint_now: Callable[[], None] | None = None
        # 由 StepRunner 注入：辅助浏览器开出来后也挂上现场采集
        self._artifacts: Any = None

    # ------------------------------------------------------------------
    def require(self, name: str) -> Any:
        return self.resolver.resolve(name, step=self.current_step)

    def ask(self, request: InteractionRequest) -> InteractionResponse:
        if self.human is None:
            raise StepContractError("当前运行环境未提供人工通道（HumanChannel）")
        self.cancel.raise_if_cancelled()
        request.context.setdefault("step", self.current_step)
        request.context.setdefault("attempt_id", self.attempt_id)
        response = self.human.ask(request, cancel=self.cancel)
        if response.cancelled:
            raise AttemptCancelled("人工介入等待期间任务被取消")
        return response

    def checkpoint_now(self) -> None:
        """步骤中途显式落一次检查点（租到号码之后、释放之后）。"""
        if self._checkpoint_now is not None:
            self._checkpoint_now()

    # ------------------------------------------------------------------
    # 可取消等待：步骤里一律用这几个，不要裸 time.sleep / while True
    # ------------------------------------------------------------------

    def sleep(self, seconds: float) -> None:
        cancellable_sleep(seconds, should_stop=self.cancel)

    def poll(self, predicate: Callable[[], Any], *, timeout: float, interval: float = 0.5) -> Any:
        return poll_until(predicate, timeout=timeout, interval=interval, should_stop=self.cancel)

    def wait_for_selector(
        self,
        selectors,
        *,
        timeout: float = 30,
        interval: float = 0.5,
        page: Any = None,
        visible: bool = True,
    ) -> str | None:
        """等到任一选择器命中，返回命中的那一个。"""
        target = self.page if page is None else page
        if isinstance(selectors, str):
            selectors = (selectors,)
        if visible:
            probe = lambda: find_first_visible(target, selectors)[1]  # noqa: E731
        else:
            probe = lambda: find_first_selector(target, selectors)  # noqa: E731
        return self.poll(probe, timeout=timeout, interval=interval)

    def wait_for_url(self, substring: str, *, timeout: float = 60, interval: float = 1.0, page: Any = None) -> bool:
        target = self.page if page is None else page
        return bool(
            self.poll(
                lambda: substring in str(getattr(target, "url", "") or ""),
                timeout=timeout,
                interval=interval,
            )
        )

    def call_with_cancel(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """调用可能支持 should_stop 的回调（otp_callback / wait_code / acquire ...）。

        按签名判断而不是试错：不支持的实现照旧跑满自己的 timeout。
        """
        if accepts_should_stop(fn):
            kwargs.setdefault("should_stop", self.cancel)
        return fn(*args, **kwargs)

    # ------------------------------------------------------------------
    # 辅助浏览器：主会话之外再开一个隔离会话
    # ------------------------------------------------------------------

    @staticmethod
    def _aux_key(name: str) -> str:
        return f"{AUX_BROWSER_PREFIX}{name}"

    def open_aux_browser(self, name: str = "aux"):
        """按主会话的参数再开一个隔离浏览器，返回它的 page。

        用在"必须换一个干净上下文"的场景：ChatGPT 注册完的上下文直接做 OAuth
        会 NS_BINDING_ABORTED，旧实现为此在 worker 里自己 new 了第二个 Camoufox，
        引擎完全看不见它。走这里开出来的会话记在 handles 里，步骤的 compensate
        与 run_step_flow 的 finally 都会兜底关，失败现场包也会带上它的截图。
        """
        if self.launch_spec is None:
            raise StepContractError("当前运行环境未记录浏览器启动参数，无法开辅助浏览器")
        # 幂等：重试进来时先把上一轮的关掉，否则每次重试泄漏一个浏览器进程
        self.close_aux_browser(name)
        self.cancel.raise_if_cancelled()
        session = self.launch_spec.start()
        page = session.new_page()
        self.handles[self._aux_key(name)] = session
        self.aux_pages[name] = page
        if self._artifacts is not None:
            self._artifacts.attach(page)
        self.emit(
            "aux_browser_opened",
            {"name": name, "engine": str(getattr(session, "engine", ""))},
            message=f"打开辅助浏览器 {name}",
        )
        return page

    def close_aux_browser(self, name: str = "aux") -> None:
        session = self.handles.pop(self._aux_key(name), None)
        self.aux_pages.pop(name, None)
        if session is None:
            return
        try:
            session.close()
        except Exception:
            pass
        self.emit("aux_browser_closed", {"name": name}, message=f"关闭辅助浏览器 {name}")

    def close_aux_browsers(self) -> None:
        """兜底关掉所有辅助会话（补偿没跑到时由 run_step_flow 的 finally 调用）。"""
        prefix_len = len(AUX_BROWSER_PREFIX)
        for key in [k for k in list(self.handles) if str(k).startswith(AUX_BROWSER_PREFIX)]:
            self.close_aux_browser(str(key)[prefix_len:])
        self.aux_pages.clear()

    def log(self, message: str, *, level: str = "info", detail: dict | None = None) -> None:
        payload = {"step": self.current_step, "attempt_id": self.attempt_id}
        payload.update(detail or {})
        text = f"[{self.current_step}] {message}" if self.current_step else message
        call_log(self._log_fn, text, level=level, detail=payload)

    def emit(self, event_type: str, detail: dict | None = None, *, level: str = "info", message: str = "") -> None:
        payload = {"step": self.current_step, "attempt_id": self.attempt_id}
        payload.update(detail or {})
        call_log(self._log_fn, message or event_type, level=level, event_type=event_type, detail=payload)


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class StepRunner:
    def __init__(
        self,
        flow: Flow,
        *,
        obstacles: Sequence[ObstacleHandler] = (),
        checkpoint: CheckpointSink | None = None,
        artifacts: ArtifactCollector | None = None,
        rewind_budget: int = 2,
        hold_on_failure: bool = False,
    ):
        self.flow = flow
        self.obstacles = list(obstacles)
        self.checkpoint = checkpoint or NullCheckpointSink()
        self.artifacts = artifacts
        self.rewind_budget = max(0, int(rewind_budget))
        # headed 半自动场景：重试耗尽时不直接放弃，先问操作员怎么办
        self.hold_on_failure = bool(hold_on_failure)

    # ------------------------------------------------------------------
    def run(self, sc: StepContext, *, start_at: str | None = None, state: dict | None = None) -> dict:
        if state:
            sc.state.update(state)
        cursor = self.flow.index_of(start_at) if start_at else 0
        if start_at:
            self._assert_resumable(cursor, sc)

        executed: list[Step] = []
        snapshots: dict[str, dict] = {}
        rewinds: dict[str, int] = {}
        # 正在执行、尚未成功的步骤：失败 / 取消 / 契约校验不过时它不在 executed 里，
        # 但它可能已经租了号、提交了表单，必须一并补偿
        current: Step | None = None

        def _checkpoint_now() -> None:
            if current is not None:
                self._write_checkpoint(sc, cursor, current, phase="running")

        sc._checkpoint_now = _checkpoint_now
        sc._artifacts = self.artifacts

        try:
            while cursor < len(self.flow.steps):
                step = self.flow.steps[cursor]
                sc.current_step = step.name
                sc.cancel.raise_if_cancelled()

                self._run_obstacles(sc, phase="before")

                if not step.when(sc):
                    sc.emit("step_skipped", {"reason": "guard"}, message=f"跳过步骤 {step.name}")
                    cursor += 1
                    continue

                if step.anchor:
                    snapshots[step.name] = dict(sc.state)

                current = step
                result = self._execute_with_retry(sc, step)

                if result.outcome == REWIND:
                    # 发起回撤的步骤没有成功，它占用的资源也要退掉
                    self._compensate(sc, step)
                    current = None
                    cursor = self._do_rewind(sc, step, result, executed, snapshots, rewinds)
                    continue

                if result.outcome == SKIP:
                    current = None
                    cursor += 1
                    continue

                executed.append(step)
                current = None
                self._write_checkpoint(sc, cursor, step)
                cursor += 1

            sc.current_step = ""
            return self._collect_result(sc)
        except BaseException:
            # 取消与失败都要逆序补偿：退号退款、释放邮箱、记下半成品账号
            if current is not None and current not in executed:
                self._compensate(sc, current)
            self._compensate_all(sc, executed)
            raise
        finally:
            sc._checkpoint_now = None
            sc._artifacts = None

    # ------------------------------------------------------------------
    def _execute_with_retry(self, sc: StepContext, step: Step) -> StepResult:
        attempt = 0
        while True:
            attempt += 1
            started = time.time()
            sc.emit("step_started", {"attempt": attempt}, message=f"步骤开始: {step.name}")
            self._assert_reads(sc, step)

            try:
                result = step.run(sc)
            except AttemptCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一转成 StepResult 走分派
                result = StepResult.fail(exc)

            elapsed = round(time.time() - started, 2)

            if result.outcome == OK:
                self._assert_writes(sc, step)
                sc.emit(
                    "step_ok",
                    {"elapsed": elapsed, "attempt": attempt, "writes": sorted(step.writes)},
                    message=f"步骤完成: {step.name} ({elapsed}s)",
                )
                return result

            if result.outcome in (SKIP, REWIND):
                sc.emit(
                    "step_rewind" if result.outcome == REWIND else "step_skipped",
                    {"elapsed": elapsed, "reason": result.reason, "to": result.rewind_to},
                    message=f"步骤 {step.name} -> {result.outcome} {result.rewind_to or result.reason}",
                )
                return result

            error = result.error or RegistrationFailure(f"步骤 {step.name} 失败")
            category = classify(error)
            artifact_dir = self._capture(sc, step, attempt, error)
            sc.emit(
                "step_failed",
                {
                    "elapsed": elapsed,
                    "attempt": attempt,
                    "category": category,
                    "error": str(error),
                    "artifacts": artifact_dir,
                },
                level="error",
                message=f"步骤失败: {step.name} [{category}] {error}",
            )

            policy = retry_policy_for(step, error)
            if policy is None or attempt >= policy.max_attempts:
                held = self._hold_for_human(sc, step, error)
                if held is not None:
                    return held
                raise error

            # 重试前再探测一次干扰：多数"莫名其妙的失败"其实是弹了个 Turnstile
            self._run_obstacles(sc, phase="after_failure")
            if policy.backoff_seconds:
                self._sleep(sc, policy.backoff_seconds)
            sc.emit(
                "step_retry",
                {"attempt": attempt + 1, "category": category},
                message=f"重试步骤 {step.name} (第 {attempt + 1} 次)",
            )

    def _hold_for_human(self, sc: StepContext, step: Step, error: BaseException) -> StepResult | None:
        """重试耗尽时驻留等操作员定夺；返回 None 表示照常抛错。"""
        if not self.hold_on_failure or sc.human is None or is_fatal(error):
            return None

        anchors = self.flow.anchors()
        options = ["retry", "skip", "abort"] + [f"rewind:{name}" for name in anchors]
        request = InteractionRequest(
            kind=CHOICE,
            prompt=f"步骤 {step.name} 失败（{classify(error)}）: {error}。请选择后续处理",
            options=tuple(options),
            context={"step": step.name, "attempt_id": sc.attempt_id},
            screenshot=True,
        )
        response = sc.ask(request)
        if response.timed_out:
            # 没人回答时按请求声明的超时策略走：skip 放行，其它一律放弃（retry 会无限等下去）
            if request.on_timeout == ON_TIMEOUT_SKIP:
                self._compensate(sc, step)
                return StepResult.skip("人工确认超时，按策略跳过")
            return None

        choice = response.option or "abort"
        if choice == "retry":
            sc.emit("step_retry", {"source": "human"}, message=f"操作员选择重试 {step.name}")
            return self._execute_with_retry(sc, step)
        if choice == "skip":
            # 步骤已经失败过，跳过前把它占用的资源退掉
            self._compensate(sc, step)
            return StepResult.skip("操作员选择跳过")
        if choice.startswith("rewind:"):
            return StepResult.rewind(choice.split(":", 1)[1], "操作员选择回撤")
        return None

    def _sleep(self, sc: StepContext, seconds: float) -> None:
        sc.sleep(seconds)

    # ------------------------------------------------------------------
    def _do_rewind(
        self,
        sc: StepContext,
        step: Step,
        result: StepResult,
        executed: list[Step],
        snapshots: dict[str, dict],
        rewinds: dict[str, int],
    ) -> int:
        target = result.rewind_to
        target_index = self.flow.index_of(target)
        target_step = self.flow.steps[target_index]
        if not target_step.anchor:
            # 浏览器页面状态无法回滚，退到非 anchor 会让步骤在错误的页面上执行
            raise StepContractError(f"回撤目标必须是 anchor 步骤: {target}")

        used = rewinds.get(target, 0)
        if used >= self.rewind_budget:
            raise RewindBudgetExhausted(
                f"回撤到 {target} 的预算已用尽（{self.rewind_budget} 次），最后一次原因: {result.reason}"
            )
        rewinds[target] = used + 1

        # "读改写"键（例如密码：读到空就生成、再写回）在回撤后要保留当前值——
        # 站点侧账号可能已经用第一次生成的密码建好了，重跑再生成一个新的就对不上了
        preserved: dict[str, Any] = {}
        for done in executed:
            if self.flow.index_of(done.name) < target_index:
                continue
            for key in set(done.reads) & set(done.writes):
                if key in sc.state:
                    preserved[key] = sc.state[key]

        # 逆序补偿 target 之后已执行的步骤，并作废它们写出的键
        while executed:
            done = executed[-1]
            if self.flow.index_of(done.name) < target_index:
                break
            executed.pop()
            self._compensate(sc, done)
            for key in done.writes:
                sc.state.pop(key, None)

        snapshot = snapshots.get(target)
        if snapshot is not None:
            sc.state = dict(snapshot)
        sc.state.update(preserved)

        sc.emit(
            "step_rewind",
            {"from": step.name, "to": target, "reason": result.reason, "count": rewinds[target]},
            level="warning",
            message=f"回撤到 {target}: {result.reason}",
        )
        return target_index

    # ------------------------------------------------------------------
    def _run_obstacles(self, sc: StepContext, *, phase: str) -> None:
        sc.obstacle_phase = phase
        for handler in self.obstacles:
            try:
                obstacle = handler.detect(sc)
            except Exception as exc:
                # 探测器自己坏了不能无声：否则干扰永远"没出现"
                sc.emit(
                    "obstacle_detect_failed",
                    {"obstacle": handler.name, "error": str(exc)},
                    level="warning",
                    message=f"干扰探测失败: {handler.name}: {exc}",
                )
                continue
            if obstacle is None:
                continue
            sc.emit(
                "obstacle_detected",
                {"obstacle": obstacle.name, "phase": phase, "detail": obstacle.detail},
                message=f"检测到干扰: {obstacle.name}",
            )
            resolved = False
            try:
                resolved = bool(handler.resolve(sc, obstacle))
            except (AttemptCancelled, RegistrationFailure):
                # 已分类的失败（风控 / 配置）是 handler 的明确判决，交给引擎按类别处置
                raise
            except Exception as exc:
                sc.emit(
                    "obstacle_failed",
                    {"obstacle": obstacle.name, "error": str(exc)},
                    level="warning",
                    message=f"干扰处理失败: {obstacle.name}: {exc}",
                )
            if resolved:
                sc.emit(
                    "obstacle_resolved",
                    {"obstacle": obstacle.name, "phase": phase},
                    message=f"干扰已处理: {obstacle.name}",
                )
            else:
                sc.emit(
                    "obstacle_unresolved",
                    {"obstacle": obstacle.name, "phase": phase},
                    level="warning",
                    message=f"干扰未能处理，继续执行: {obstacle.name}",
                )

    # ------------------------------------------------------------------
    def _assert_reads(self, sc: StepContext, step: Step) -> None:
        missing = [key for key in step.reads if key not in sc.state]
        if missing:
            raise StepContractError(f"步骤 {step.name} 缺少输入键: {missing}")

    def _assert_writes(self, sc: StepContext, step: Step) -> None:
        missing = [key for key in step.writes if key not in sc.state]
        if missing:
            raise StepContractError(f"步骤 {step.name} 声明会写出 {missing}，但执行后没有")

    def _assert_resumable(self, cursor: int, sc: StepContext) -> None:
        """续跑前校验：游标之后要读的键，检查点里有或后续步骤会写。"""
        provided = set(sc.state) | set(self.flow.initial_keys)
        missing: list[str] = []
        for step in self.flow.steps[cursor:]:
            missing.extend(key for key in step.reads if key not in provided)
            provided.update(step.writes)
        if missing:
            raise StepContractError(
                f"检查点状态不足以从 {self.flow.steps[cursor].name} 续跑，缺少: {sorted(set(missing))}"
            )

    # ------------------------------------------------------------------
    def _compensate(self, sc: StepContext, step: Step) -> None:
        try:
            step.compensate(sc)
        except Exception as exc:
            sc.emit(
                "compensate_failed",
                {"step": step.name, "error": str(exc)},
                level="warning",
                message=f"补偿失败: {step.name}: {exc}",
            )

    def _compensate_all(self, sc: StepContext, executed: list[Step]) -> None:
        for step in reversed(executed):
            self._compensate(sc, step)
        executed.clear()

    def _capture(self, sc: StepContext, step: Step, attempt: int, error: BaseException | None) -> str:
        if self.artifacts is None:
            return ""
        try:
            return self.artifacts.capture(sc, step, attempt, error)
        except Exception:
            return ""

    def _write_checkpoint(self, sc: StepContext, cursor: int, step: Step, *, phase: str = "done") -> None:
        """phase=done：步骤已完成，游标指向下一步；phase=running：步骤中途，游标仍指向它。"""
        try:
            self.checkpoint.write(
                attempt_id=sc.attempt_id,
                cursor=cursor + 1 if phase == "done" else cursor,
                step=step.name,
                phase=phase,
                state=dict(sc.state),
                resource_refs=dict(sc.resource_refs),
                channels_used=dict(getattr(sc.resolver, "channels_used", {}) or {}),
            )
        except Exception as exc:
            # 检查点写不进去不能让注册失败，但必须留痕：重启回收依赖它
            sc.emit(
                "checkpoint_failed",
                {"error": str(exc), "phase": phase},
                level="warning",
                message=f"检查点写入失败: {exc}",
            )

    def _collect_result(self, sc: StepContext) -> dict:
        missing = [key for key in self.flow.result_keys if key not in sc.state]
        if missing:
            raise StepContractError(f"流程 {self.flow.name} 结束时缺少结果键: {missing}")
        return {key: sc.state.get(key) for key in self.flow.result_keys}
