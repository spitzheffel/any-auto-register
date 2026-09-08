"""Playwright 执行器 - 支持 headless/headed 模式"""
from ..base_executor import BaseExecutor, Response


class PlaywrightExecutor(BaseExecutor):
    def __init__(self, proxy: str = None, headless: bool = True, engine: str = ""):
        super().__init__(proxy)
        self.headless = headless
        self.engine = engine
        self._session = None
        self._browser = None
        self._context = None
        self._page = None
        self._init()

    def _init(self):
        from core.browser_engine import (
            DEFAULT_ENGINE,
            FAMILY_CHROMIUM,
            engine_family,
            engine_spec,
            resolve_engine,
            start_browser,
        )
        from core.errors import ConfigError

        # 这个执行器的启动参数与 UA 都是 Chromium 专属：全局把内核设成 camoufox（Firefox 系）
        # 只该影响显式走 browser_engine 的隐身流程，不能顺手把这里也切过去
        try:
            resolved = resolve_engine(self.engine, lambda message: None)
        except ConfigError:
            resolved = DEFAULT_ENGINE
        if engine_family(resolved) != FAMILY_CHROMIUM:
            print(f"[PlaywrightExecutor] 内核 {resolved} 不是 Chromium 系，改用 {DEFAULT_ENGINE}")
            resolved = DEFAULT_ENGINE

        self._session = start_browser(
            engine=resolved,
            headless=self.headless,
            proxy=self.proxy,
            launch_args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        self._browser = self._session.browser

        # 设置更长的默认超时
        context_opts = {"viewport": {"width": 1280, "height": 720}}
        # 隐身内核（cloakbrowser）的指纹由二进制保证一致性，覆盖 UA 反而穿帮；非 Chromium 系同理
        spec = engine_spec(self._session.engine)
        if not (spec and spec.stealth) and self._session.family == FAMILY_CHROMIUM:
            context_opts["user_agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self._context = self._browser.new_context(**context_opts)
        self._context.set_default_timeout(60000)  # 60秒默认超时
        self._page = self._context.new_page()

    def get(self, url, *, headers=None, params=None) -> Response:
        import urllib.parse
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        if headers:
            self._page.set_extra_http_headers(headers)
        resp = self._page.goto(url)
        return Response(
            status_code=resp.status,
            text=self._page.content(),
            headers=dict(resp.headers),
            cookies=self.get_cookies(),
        )

    def post(self, url, *, headers=None, params=None, data=None, json=None) -> Response:
        import urllib.parse, json as _json
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        post_data = None
        content_type = "application/x-www-form-urlencoded"
        if json is not None:
            post_data = _json.dumps(json)
            content_type = "application/json"
        elif data:
            post_data = urllib.parse.urlencode(data)
        h = {"Content-Type": content_type}
        if headers:
            h.update(headers)
        resp = self._page.request.post(url, headers=h, data=post_data)
        return Response(
            status_code=resp.status,
            text=resp.text(),
            headers=dict(resp.headers),
            cookies=self.get_cookies(),
        )

    def get_cookies(self) -> dict:
        return {c["name"]: c["value"] for c in self._context.cookies()}

    def set_cookies(self, cookies: dict, domain: str = ".example.com") -> None:
        page_url = self._page.url if self._page else None
        if page_url and page_url.startswith("http"):
            self._context.add_cookies([
                {"name": k, "value": v, "url": page_url} for k, v in cookies.items()
            ])
        else:
            self._context.add_cookies([
                {"name": k, "value": v, "domain": domain, "path": "/"} for k, v in cookies.items()
            ])
    
    # 浏览器操作方法
    def goto(self, url: str, **kwargs):
        """导航到 URL"""
        return self._page.goto(url, **kwargs)
    
    def fill(self, selector: str, value: str, **kwargs):
        """填充输入框"""
        return self._page.fill(selector, value, **kwargs)
    
    def click(self, selector: str, **kwargs):
        """点击元素"""
        return self._page.click(selector, **kwargs)
    
    def wait_for_selector(self, selector: str, **kwargs):
        """等待元素出现"""
        return self._page.wait_for_selector(selector, **kwargs)
    
    def query_selector(self, selector: str):
        """查询元素"""
        return self._page.query_selector(selector)
    
    def query_selector_all(self, selector: str):
        """查询所有匹配元素"""
        return self._page.query_selector_all(selector)
    
    def evaluate(self, script: str, *args):
        """执行 JavaScript"""
        return self._page.evaluate(script, *args)
    
    def content(self) -> str:
        """获取页面 HTML"""
        return self._page.content()
    
    @property
    def url(self) -> str:
        """当前页面 URL"""
        return self._page.url
    
    def press(self, selector: str, key: str, **kwargs):
        """按键"""
        return self._page.press(selector, key, **kwargs)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
            self._browser = None
            self._context = None
            self._page = None
