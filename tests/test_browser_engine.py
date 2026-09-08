"""browser_engine：可选 Chromium 内核（playwright/patchright/cloakbrowser）解析与启动。"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import core.browser_engine as be
from core.browser_engine import (
    CONFIG_KEY,
    ENV_KEY,
    EngineSession,
    normalize_engine,
    resolve_engine,
)
from core.config_store import config_store


@pytest.fixture(autouse=True)
def _clean_engine_sources(monkeypatch):
    """每个用例独立：清全局配置与环境变量，避免互相污染。"""
    monkeypatch.delenv(ENV_KEY, raising=False)
    config_store.set(CONFIG_KEY, "")
    yield


class TestNormalizeEngine:
    def test_empty_and_aliases_mean_unspecified(self):
        assert normalize_engine("") == ""
        assert normalize_engine(None) == ""
        assert normalize_engine("auto") == ""
        assert normalize_engine("default") == ""

    def test_strips_and_lowercases(self):
        assert normalize_engine("  Playwright ") == "playwright"
        assert normalize_engine("CloakBrowser") == "cloakbrowser"


class TestResolveEngine:
    def test_defaults_to_playwright(self):
        assert resolve_engine() == "playwright"

    def test_preferred_beats_global_and_env(self, monkeypatch):
        config_store.set(CONFIG_KEY, "patchright")
        monkeypatch.setenv(ENV_KEY, "cloakbrowser")
        assert resolve_engine("cloakbrowser") == "cloakbrowser"

    def test_global_config_beats_env(self, monkeypatch):
        config_store.set(CONFIG_KEY, "patchright")
        monkeypatch.setenv(ENV_KEY, "cloakbrowser")
        assert resolve_engine() == "patchright"

    def test_env_is_last_fallback(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, "patchright")
        assert resolve_engine() == "patchright"

    def test_unknown_engine_is_ignored(self):
        logs: list[str] = []
        assert resolve_engine("chrome-151", log_fn=logs.append) == "playwright"
        assert any("未知引擎" in line for line in logs)

    def test_unavailable_engine_falls_back_to_playwright(self, monkeypatch):
        monkeypatch.setattr(be, "_engine_available", lambda engine: engine == "playwright")
        logs: list[str] = []
        assert resolve_engine("cloakbrowser", log_fn=logs.append) == "playwright"
        assert any("不可用" in line for line in logs)

    def test_unspecified_alias_falls_through_to_next_source(self, monkeypatch):
        monkeypatch.setenv(ENV_KEY, "patchright")
        # auto / default 不算显式指定，应继续走 env
        assert resolve_engine("auto") == "patchright"


class TestEngineSession:
    def test_close_closes_browser_and_stops_driver(self):
        events: list[str] = []
        browser = SimpleNamespace(close=lambda: events.append("browser"))
        driver = SimpleNamespace(stop=lambda: events.append("driver"))
        session = EngineSession("patchright", browser, driver=driver)
        session.close()
        assert events == ["browser", "driver"]

    def test_close_swallows_driver_stop_error(self):
        events: list[str] = []

        def boom():
            raise RuntimeError("stop failed")

        browser = SimpleNamespace(close=lambda: events.append("browser"))
        session = EngineSession("playwright", browser, driver=SimpleNamespace(stop=boom))
        session.close()  # 不应抛错
        assert events == ["browser"]

    def test_close_without_driver(self):
        browser = SimpleNamespace(close=lambda: None)
        EngineSession("cloakbrowser", browser).close()


class TestStartBrowser:
    def _fake_pw_modules(self, monkeypatch, name: str, launch_recorder: list):
        driver = SimpleNamespace(
            chromium=SimpleNamespace(launch=lambda **kw: (launch_recorder.append(kw), "FAKE_BROWSER")[1]),
            stop=lambda: launch_recorder.append("driver.stop"),
        )
        sync_mod = types.ModuleType(f"{name}.sync_api")
        sync_mod.sync_playwright = lambda: SimpleNamespace(start=lambda: driver)
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, f"{name}.sync_api", sync_mod)
        return driver

    def test_playwright_launch_options(self, monkeypatch):
        monkeypatch.setattr(be, "_engine_available", lambda engine: True)
        recorder: list = []
        self._fake_pw_modules(monkeypatch, "playwright", recorder)

        session = be.start_browser(
            engine="playwright",
            headless=False,
            proxy="http://127.0.0.1:7890",
            launch_args=["--no-sandbox"],
            log_fn=lambda msg: None,
        )

        assert session.engine == "playwright"
        assert session.browser == "FAKE_BROWSER"
        opts = recorder[0]
        assert opts["headless"] is False
        assert opts["proxy"] == {"server": "http://127.0.0.1:7890"}
        assert opts["args"] == ["--no-sandbox"]

    def test_playwright_splits_authenticated_proxy(self, monkeypatch):
        """Chromium 不认 server 里内嵌的 user:pass，带认证的付费网关必须拆开传。"""
        monkeypatch.setattr(be, "_engine_available", lambda engine: True)
        recorder: list = []
        self._fake_pw_modules(monkeypatch, "playwright", recorder)

        be.start_browser(engine="playwright", proxy="http://user:pass@1.2.3.4:8080", log_fn=lambda msg: None)

        assert recorder[0]["proxy"] == {"server": "http://1.2.3.4:8080", "username": "user", "password": "pass"}

    def test_patchright_engine_uses_patchright_driver(self, monkeypatch):
        monkeypatch.setattr(be, "_engine_available", lambda engine: True)
        recorder: list = []
        self._fake_pw_modules(monkeypatch, "patchright", recorder)

        session = be.start_browser(engine="patchright", log_fn=lambda msg: None)

        assert session.engine == "patchright"
        assert session.browser == "FAKE_BROWSER"
        assert session._driver is not None

    def test_cloakbrowser_launch_without_driver(self, monkeypatch):
        monkeypatch.setattr(be, "_engine_available", lambda engine: True)
        recorder: dict = {}

        def fake_launch(**kw):
            recorder.update(kw)
            return "CLOAK_BROWSER"

        cloak_mod = types.ModuleType("cloakbrowser")
        cloak_mod.launch = fake_launch
        monkeypatch.setitem(sys.modules, "cloakbrowser", cloak_mod)

        session = be.start_browser(
            engine="cloakbrowser",
            headless=True,
            proxy="http://u:p@1.2.3.4:8080",
            launch_args=["--should-be-ignored"],
            log_fn=lambda msg: None,
        )

        assert session.engine == "cloakbrowser"
        assert session.browser == "CLOAK_BROWSER"
        assert session._driver is None
        # cloakbrowser 代理直接收字符串，launch_args 不透传（保持其 stealth 一致性）
        assert recorder["proxy"] == "http://u:p@1.2.3.4:8080"
        assert recorder["headless"] is True
        assert "args" not in recorder


class TestPlaywrightExecutorEngine:
    def _install_fake_session(self, monkeypatch, engine: str):
        from core.browser_engine import EngineSession

        context_kwargs: dict = {}
        pages: list[SimpleNamespace] = []

        page = SimpleNamespace(url="about:blank")
        context = SimpleNamespace(
            new_page=lambda: page,
            set_default_timeout=lambda ms: context_kwargs.update(timeout=ms),
            cookies=lambda: [],
        )
        browser = SimpleNamespace(new_context=lambda **kw: (context_kwargs.update(kw), context)[1])
        session = EngineSession(engine, browser, driver=None)

        captured: dict = {}

        def fake_start_browser(**kw):
            captured.update(kw)
            return session

        monkeypatch.setattr(be, "start_browser", fake_start_browser)
        return captured, context_kwargs, session

    def test_executor_passes_engine_and_skips_ua_for_cloak(self, monkeypatch):
        from core.executors.playwright import PlaywrightExecutor

        captured, context_kwargs, session = self._install_fake_session(monkeypatch, "cloakbrowser")
        executor = PlaywrightExecutor(headless=True, engine="cloakbrowser")

        assert captured["engine"] == "cloakbrowser"
        assert executor._session is session
        # cloakbrowser：不覆盖 UA（指纹由二进制保证）
        assert "user_agent" not in context_kwargs
        assert context_kwargs["viewport"] == {"width": 1280, "height": 720}

    def test_executor_keeps_ua_for_playwright(self, monkeypatch):
        from core.executors.playwright import PlaywrightExecutor

        _, context_kwargs, _ = self._install_fake_session(monkeypatch, "playwright")
        PlaywrightExecutor(headless=True)
        assert "user_agent" in context_kwargs

    def test_executor_close_is_idempotent(self, monkeypatch):
        from core.executors.playwright import PlaywrightExecutor

        _, _, session = self._install_fake_session(monkeypatch, "playwright")
        closes: list[int] = []
        session.browser = SimpleNamespace(
            new_context=lambda **kw: SimpleNamespace(
                new_page=lambda: SimpleNamespace(url="about:blank"),
                set_default_timeout=lambda ms: None,
            ),
            close=lambda: closes.append(1),
        )
        executor = PlaywrightExecutor(headless=True)
        executor.close()
        executor.close()  # 第二次不应抛错
        assert closes == [1]


class TestEngineRegistry:
    def test_new_engine_needs_only_one_registration(self, monkeypatch):
        registry = dict(be._ENGINES)
        monkeypatch.setattr(be, "_ENGINES", registry)
        launched: dict = {}

        def fake_launcher(**kw):
            launched.update(kw)
            return be.BrowserSession("nodriver", "FAKE", family=be.FAMILY_CHROMIUM)

        be.register_engine("nodriver", fake_launcher, family=be.FAMILY_CHROMIUM)

        assert "nodriver" in be.supported_engines()
        session = be.start_browser(engine="nodriver", headless=False, log_fn=lambda m: None)
        assert session.engine == "nodriver"
        assert launched["headless"] is False

    def test_camoufox_is_registered_as_firefox_family(self):
        assert "camoufox" in be.supported_engines()
        assert be.engine_family("camoufox") == be.FAMILY_FIREFOX
        assert be.engine_family("playwright") == be.FAMILY_CHROMIUM

    def test_firefox_engine_does_not_silently_fall_back_to_chromium(self, monkeypatch):
        # 指纹体系互不替代：camoufox 不可用时宁可报错，也不能悄悄换 Chromium
        monkeypatch.setattr(be, "_engine_available", lambda engine: engine != "camoufox")
        with pytest.raises(RuntimeError, match="无同族替代"):
            be.resolve_engine("camoufox", log_fn=lambda m: None)

    def test_explicit_opt_out_of_fallback(self, monkeypatch):
        monkeypatch.setattr(be, "_engine_available", lambda engine: False)
        with pytest.raises(RuntimeError, match="禁止回退"):
            be.resolve_engine("playwright", log_fn=lambda m: None, allow_fallback=False)


class TestBuildProxyConfig:
    def test_splits_credentials(self):
        assert be.build_proxy_config("http://user:pass@1.2.3.4:8080") == {
            "server": "http://1.2.3.4:8080",
            "username": "user",
            "password": "pass",
        }

    def test_without_credentials(self):
        assert be.build_proxy_config("socks5://1.2.3.4:1080") == {"server": "socks5://1.2.3.4:1080"}

    def test_bare_host_port_defaults_to_http(self):
        assert be.build_proxy_config("1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}

    def test_empty_is_none(self):
        assert be.build_proxy_config("") is None
        assert be.build_proxy_config(None) is None

    def test_unparseable_is_passed_through_to_fail_loudly(self):
        # 配置写错要让 Playwright 报错，而不是静默不走代理
        assert be.build_proxy_config("http://") == {"server": "http://"}


class TestBrowserSessionPort:
    def test_new_page_prefers_direct_api(self):
        page = SimpleNamespace(url="about:blank")
        session = be.BrowserSession("camoufox", SimpleNamespace(new_page=lambda **kw: page))
        assert session.new_page() is page

    def test_new_page_falls_back_to_context(self):
        page = SimpleNamespace(url="about:blank")
        browser = SimpleNamespace(new_context=lambda **kw: SimpleNamespace(new_page=lambda: page))
        assert be.BrowserSession("playwright", browser).new_page() is page

    def test_export_cookies_from_page_context(self):
        cookies = [{"name": "WorkosCursorSessionToken", "value": "x"}]
        page = SimpleNamespace(context=SimpleNamespace(cookies=lambda: cookies))
        session = be.BrowserSession("camoufox", SimpleNamespace())
        assert session.export_cookies(page) == cookies

    def test_close_is_idempotent(self):
        closes: list[int] = []
        session = be.BrowserSession("playwright", SimpleNamespace(close=lambda: closes.append(1)))
        session.close()
        session.close()
        assert closes == [1]

    def test_context_manager_closes(self):
        closes: list[int] = []
        with be.BrowserSession("playwright", SimpleNamespace(close=lambda: closes.append(1))):
            pass
        assert closes == [1]


class TestOAuthBrowserEngine:
    def test_plain_fallback_branch_uses_engine_session(self, monkeypatch):
        import core.oauth_browser as ob

        monkeypatch.setattr(ob, "_detect_running_chrome_cdp", lambda: "")
        monkeypatch.setattr(ob, "_detect_chrome_user_data_dir", lambda: "")
        monkeypatch.setattr(ob, "_relaunch_chrome_with_debug_port", lambda port: False)
        monkeypatch.setattr(
            be, "resolve_engine", lambda preferred="", log_fn=print: "cloakbrowser"
        )

        events: list[str] = []
        session = SimpleNamespace(
            engine="cloakbrowser",
            browser=SimpleNamespace(
                new_context=lambda **kw: SimpleNamespace(
                    new_page=lambda: SimpleNamespace(url="about:blank"),
                    close=lambda: events.append("context.close"),
                ),
                close=lambda: events.append("browser.close"),
            ),
            close=lambda: events.append("session.close"),
        )
        captured: dict = {}

        def fake_start_browser(**kw):
            captured.update(kw)
            return session

        monkeypatch.setattr(be, "start_browser", fake_start_browser)

        with ob.OAuthBrowser(engine="cloakbrowser", log_fn=lambda msg: None) as browser:
            assert browser._resolved_engine == "cloakbrowser"
            assert browser._session is session

        assert captured["engine"] == "cloakbrowser"
        assert "session.close" in events
