"""浏览器内核注册表。

新增一个内核（nodriver、DrissionPage、远程浏览器服务…）= 在本文件末尾追加一次
``register_engine(name, launcher, family=...)``；目前没有外部模块的自动发现机制，
注册必须发生在 import 本模块之后、首次 ``start_browser`` 之前。前端下拉与
``/api/config/options`` 的 ``browser_engine_options`` 都从 ``supported_engines()`` 读。

``resolve_engine(preferred)`` 的候选顺序（高 → 低）：
  1. 调用方传入的 preferred（流程层由平台适配器的 engine_preference 或
     任务级 RegisterConfig.extra["browser_engine"] 给出，谁先非空用谁）
  2. 全局配置 config_store["browser_engine"]（PUT /api/config 可改）
  3. 环境变量 ANY_AR_BROWSER_ENGINE
  4. 默认 playwright

回退规则：候选不可用时，Chromium 系回退到默认的 playwright；firefox 系
（camoufox）**不会**换成 Chromium——各平台的隐身流程依赖它的指纹体系，换了
只会换来一堆风控失败——而是抛 ConfigError 让任务层停批。allow_fallback=False
时任何不可用都直接抛错。

页面操作 API 统一约定为 Playwright 兼容的 Page：现有四个内核都满足。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlparse

from core.errors import ConfigError

CONFIG_KEY = "browser_engine"
ENV_KEY = "ANY_AR_BROWSER_ENGINE"

DEFAULT_ENGINE = "playwright"
FAMILY_CHROMIUM = "chromium"
FAMILY_FIREFOX = "firefox"


def normalize_engine(value) -> str:
    """清洗引擎名：空 / auto / default 视为未指定。"""
    engine = str(value or "").strip().lower()
    if engine in ("", "auto", "default"):
        return ""
    return engine


def build_proxy_config(proxy: str | None) -> dict | None:
    """把代理 URL 转成 Playwright 的 proxy 配置（用户名密码拆出来）。

    历史上这段在 8 个平台各拷贝了一份，现在收敛到这里。
    """
    raw = str(proxy or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if not parsed.hostname:
        # 解析不出主机就原样交给 Playwright 报错——配置写错应该炸，而不是静默裸奔
        return {"server": raw}
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server = f"{server}:{parsed.port}"
    config: dict[str, str] = {"server": server}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

class BrowserSession:
    """统一封装启动结果。

    playwright / patchright 需要保持 sync_playwright driver 存活并在关闭时 stop()；
    cloakbrowser 与 camoufox 自管 driver，只需 close() browser。
    """

    def __init__(self, engine: str, browser, driver=None, *, family: str = FAMILY_CHROMIUM):
        self.engine = engine
        self.browser = browser
        self.family = family
        self._driver = driver
        self._closed = False

    # -- 内核无关的会话能力 ------------------------------------------------
    def new_page(self, **kwargs):
        """新建页面（Playwright 兼容 Page）。"""
        if hasattr(self.browser, "new_page"):
            return self.browser.new_page(**kwargs)
        context = self.browser.new_context(**kwargs)
        return context.new_page()

    def add_init_script(self, script: str, *, page=None) -> None:
        """注入页面初始化脚本（指纹补丁等）。"""
        target = page if page is not None else self.browser
        try:
            target.add_init_script(script)
        except Exception:
            pass

    def export_cookies(self, page=None) -> list:
        """导出 cookie，用于 OAuth 浏览器复用 / 交接给协议段。"""
        for candidate in (page, getattr(page, "context", None), self.browser):
            if candidate is None:
                continue
            getter = getattr(candidate, "cookies", None)
            if callable(getter):
                try:
                    return list(getter())
                except Exception:
                    continue
        return []

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.browser is not None:
                self.browser.close()
        finally:
            if self._driver is not None:
                try:
                    self._driver.stop()
                except Exception:
                    pass

    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


#: 历史名字，外部仍在用
EngineSession = BrowserSession


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class EngineSpec:
    name: str
    launcher: Callable[..., BrowserSession]
    family: str = FAMILY_CHROMIUM
    stealth: bool = False
    probe: Callable[[], bool] | None = None


_ENGINES: dict[str, EngineSpec] = {}


def register_engine(
    name: str,
    launcher: Callable[..., BrowserSession],
    *,
    family: str = FAMILY_CHROMIUM,
    stealth: bool = False,
    probe: Callable[[], bool] | None = None,
) -> None:
    _ENGINES[str(name).strip().lower()] = EngineSpec(
        name=str(name).strip().lower(),
        launcher=launcher,
        family=family,
        stealth=stealth,
        probe=probe,
    )


def supported_engines() -> tuple[str, ...]:
    return tuple(sorted(_ENGINES))


def engine_spec(name: str) -> EngineSpec | None:
    return _ENGINES.get(normalize_engine(name))


def engine_family(name: str) -> str:
    spec = engine_spec(name)
    return spec.family if spec else FAMILY_CHROMIUM


def _global_configured_engine() -> str:
    try:
        from .config_store import config_store
        return normalize_engine(config_store.get(CONFIG_KEY))
    except Exception:
        return ""


def _import_available(module_name: str) -> bool:
    try:
        __import__(module_name)
    except Exception:
        return False
    return True


def _engine_available(engine: str) -> bool:
    spec = _ENGINES.get(normalize_engine(engine))
    if spec is None:
        return False
    if spec.probe is None:
        return True
    try:
        return bool(spec.probe())
    except Exception:
        return False


def resolve_engine(
    preferred: str = "",
    log_fn: Callable[[str], None] = print,
    *,
    allow_fallback: bool = True,
) -> str:
    """按优先级选引擎；不可用时在**同一 family 内**回退。

    allow_fallback=False 时不可用直接抛错（调用方明确要求某个指纹体系）。
    """
    candidates: list[str] = []
    for value in (
        normalize_engine(preferred),
        _global_configured_engine(),
        normalize_engine(os.environ.get(ENV_KEY)),
    ):
        if value and value not in candidates:
            candidates.append(value)

    for engine in candidates:
        if engine not in _ENGINES:
            log_fn(f"[browser-engine] 未知引擎 `{engine}` 已忽略（可选: {' / '.join(supported_engines())}）")
            continue
        if _engine_available(engine):
            if engine != DEFAULT_ENGINE:
                log_fn(f"[browser-engine] 使用 {engine} 内核")
            return engine

        family = engine_family(engine)
        # 内核没装是配置 / 环境问题：分类为 ConfigError 让任务层立即停批，而不是把整批尝试烧光
        if not allow_fallback:
            raise ConfigError(f"[browser-engine] {engine} 不可用（未安装或未授权），且调用方禁止回退")
        if family != FAMILY_CHROMIUM:
            # 指纹体系互不替代：firefox 系不可用时换 Chromium 只会换来一堆风控失败
            raise ConfigError(
                f"[browser-engine] {engine} 不可用（未安装或未授权），"
                f"且 {family} 系内核无同族替代，请安装后重试"
            )
        log_fn(f"[browser-engine] {engine} 不可用（未安装或未授权），回退 {DEFAULT_ENGINE}")

    return DEFAULT_ENGINE


def start_browser(
    *,
    engine: str = "",
    headless: bool = True,
    proxy: Optional[str] = None,
    launch_args: Optional[list] = None,
    log_fn: Callable[[str], None] = print,
    allow_fallback: bool = True,
    **extra,
) -> BrowserSession:
    """按引擎启动浏览器，返回 BrowserSession。"""
    resolved = resolve_engine(engine, log_fn, allow_fallback=allow_fallback)
    spec = _ENGINES.get(resolved)
    if spec is None:  # pragma: no cover - resolve_engine 已兜底
        raise RuntimeError(f"[browser-engine] 未注册的引擎: {resolved}")
    return spec.launcher(
        headless=headless,
        proxy=proxy,
        launch_args=launch_args,
        log_fn=log_fn,
        **extra,
    )


@dataclass(frozen=True)
class BrowserLaunchSpec:
    """一次尝试里复用的浏览器启动参数。

    ``run_step_flow`` 开主会话时把参数记下来，步骤想再开一个隔离会话时
    （典型是 ChatGPT 注册完必须换干净上下文做 Codex OAuth）按同样的引擎 /
    代理 / 无头设置开，不用把这堆参数再往下穿一遍。
    """

    engine: str = ""
    headless: bool = True
    proxy: Optional[str] = None
    log_fn: Callable[[str], None] = print
    allow_fallback: bool = True
    #: 引擎特有的额外参数（例如 camoufox 的 geoip）
    options: dict = field(default_factory=dict)

    def start(self) -> BrowserSession:
        return start_browser(
            engine=self.engine,
            headless=self.headless,
            proxy=self.proxy,
            log_fn=self.log_fn,
            allow_fallback=self.allow_fallback,
            **self.options,
        )


# ---------------------------------------------------------------------------
# 内置内核
# ---------------------------------------------------------------------------

def _launch_playwright_family(module_name: str, engine_name: str):
    def launcher(*, headless=True, proxy=None, launch_args=None, log_fn=print, **_extra) -> BrowserSession:
        if module_name == "patchright":
            from patchright.sync_api import sync_playwright
        else:
            from playwright.sync_api import sync_playwright

        driver = sync_playwright().start()
        launch_opts: dict = {"headless": headless}
        proxy_config = build_proxy_config(proxy)
        if proxy_config:
            # Chromium 不接受 server 里内嵌的 user:pass，必须拆成 username / password
            launch_opts["proxy"] = proxy_config
        if launch_args:
            launch_opts["args"] = list(launch_args)
        browser = driver.chromium.launch(**launch_opts)
        return BrowserSession(engine_name, browser, driver=driver, family=FAMILY_CHROMIUM)

    return launcher


def _launch_cloakbrowser(*, headless=True, proxy=None, launch_args=None, log_fn=print, **_extra) -> BrowserSession:
    # cloakbrowser 自带 stealth 启动参数，外部注入反而可能破坏指纹一致性，故不透传 launch_args
    from cloakbrowser import launch as cloak_launch

    browser = cloak_launch(headless=headless, proxy=proxy)
    return BrowserSession("cloakbrowser", browser, family=FAMILY_CHROMIUM)


def _launch_camoufox(*, headless=True, proxy=None, launch_args=None, log_fn=print, **extra) -> BrowserSession:
    """Camoufox（Firefox 系）：各平台注册流程原本各自 import 的那一个。"""
    from camoufox.sync_api import Camoufox

    launch_opts: dict = {"headless": headless}
    proxy_config = build_proxy_config(proxy)
    if proxy_config:
        launch_opts["proxy"] = proxy_config
        if extra.get("geoip"):
            launch_opts["geoip"] = True
    if launch_args:
        launch_opts["args"] = list(launch_args)

    # Camoufox 的上下文管理器返回 browser；这里手工进入以便交给 BrowserSession 管生命周期
    manager = Camoufox(**launch_opts)
    browser = manager.__enter__()
    session = BrowserSession("camoufox", browser, family=FAMILY_FIREFOX)
    session._manager = manager  # type: ignore[attr-defined]

    original_close = session.close

    def close_with_manager() -> None:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            original_close()

    session.close = close_with_manager  # type: ignore[method-assign]
    return session


register_engine(
    "playwright",
    _launch_playwright_family("playwright", "playwright"),
    family=FAMILY_CHROMIUM,
    probe=lambda: _import_available("playwright"),
)
register_engine(
    "patchright",
    _launch_playwright_family("patchright", "patchright"),
    family=FAMILY_CHROMIUM,
    probe=lambda: _import_available("patchright"),
)
register_engine(
    "cloakbrowser",
    _launch_cloakbrowser,
    family=FAMILY_CHROMIUM,
    stealth=True,
    probe=lambda: _import_available("cloakbrowser"),
)
register_engine(
    "camoufox",
    _launch_camoufox,
    family=FAMILY_FIREFOX,
    stealth=True,
    probe=lambda: _import_available("camoufox"),
)

#: 兼容旧引用（现在是动态视图的快照）
SUPPORTED_ENGINES = supported_engines()
