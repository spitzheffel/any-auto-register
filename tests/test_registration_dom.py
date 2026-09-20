"""公共页面辅助：选择器轮询、cookie 等待、CF 全页拦截判定。

这些函数原先在 platforms/cursor/dom.py 与 platforms/chatgpt/browser_register.py
各有一份（openblocklabs / windsurf 还各抄了一遍），上收到
core/registration/dom.py 之后由这里兜底。
"""
from __future__ import annotations

import time

import pytest

from core.cancellation import CancellationToken
from core.errors import AttemptCancelled
from core.registration.dom import (
    DEFAULT_FORM_SELECTOR,
    click_first,
    click_first_visible,
    fill_first_visible,
    find_first_selector,
    find_first_visible,
    get_cookies,
    is_cf_full_block,
    read_cookie,
    wait_cf_full_block_clear,
    wait_for_any_selector,
    wait_for_cookie,
)


class FakeElement:
    def __init__(self, *, visible=True, enabled=True):
        self._visible = visible
        self._enabled = enabled
        self.value = None
        self.clicks = 0

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return self._enabled

    def fill(self, value):
        self.value = value

    def click(self, timeout=None):
        self.clicks += 1


class FakePage:
    """按选择器精确匹配；``raises`` 里的选择器模拟 query_selector 抛异常。"""

    def __init__(self, elements=None, *, cookies=(), content="", raises=()):
        self.elements = dict(elements or {})
        self._cookies = list(cookies)
        self._content = content
        self._raises = set(raises)
        self.clicked = []
        self.context = self

    def query_selector(self, selector):
        if selector in self._raises:
            raise RuntimeError("boom")
        return self.elements.get(selector)

    def click(self, selector):
        self.clicked.append(selector)

    def cookies(self):
        return list(self._cookies)

    def content(self):
        return self._content


class TestSelectors:
    def test_find_first_selector_returns_first_hit_in_order(self):
        page = FakePage({"b": FakeElement(), "c": FakeElement()})
        assert find_first_selector(page, ["a", "b", "c"]) == "b"

    def test_find_first_selector_skips_raising_selector(self):
        # 站点改版后某个选择器语法非法：跳过它继续找，而不是整条流程崩掉
        page = FakePage({"c": FakeElement()}, raises={"a"})
        assert find_first_selector(page, ["a", "b", "c"]) == "c"

    def test_find_first_visible_ignores_hidden_nodes(self):
        page = FakePage({"a": FakeElement(visible=False), "b": FakeElement()})
        element, selector = find_first_visible(page, ["a", "b"])
        assert selector == "b" and element is page.elements["b"]

    def test_find_first_visible_returns_none_pair_when_nothing_matches(self):
        assert find_first_visible(FakePage(), ["a"]) == (None, None)

    def test_wait_for_any_selector_returns_none_on_timeout(self):
        started = time.time()
        assert wait_for_any_selector(FakePage(), ["a"], timeout=0.05, interval=0.01) is None
        assert time.time() - started < 1

    def test_wait_for_any_selector_honours_pretripped_token_without_sleeping(self):
        token = CancellationToken()
        token.trip("用户取消")
        started = time.time()
        with pytest.raises(AttemptCancelled):
            wait_for_any_selector(FakePage(), ["a"], timeout=30, should_stop=token)
        assert time.time() - started < 1

    def test_click_first_clicks_the_matched_selector(self):
        page = FakePage({"b": FakeElement()})
        assert click_first(page, ["a", "b"], timeout=0.1) == "b"
        assert page.clicked == ["b"]

    def test_click_first_returns_none_when_nothing_matches(self):
        page = FakePage()
        assert click_first(page, ["a"], timeout=0.05) is None
        assert page.clicked == []

    def test_click_first_visible_skips_disabled_button(self):
        page = FakePage({"a": FakeElement(enabled=False), "b": FakeElement()})
        assert click_first_visible(page, ["a", "b"]) == "b"
        assert page.elements["b"].clicks == 1
        assert page.elements["a"].clicks == 0

    def test_fill_first_visible_fills_only_the_first_one(self):
        page = FakePage({"a": FakeElement(visible=False), "b": FakeElement(), "c": FakeElement()})
        assert fill_first_visible(page, ["a", "b", "c"], "hello") == "b"
        assert page.elements["b"].value == "hello"
        assert page.elements["c"].value is None


class TestCookies:
    def test_get_cookies_maps_name_to_value(self):
        page = FakePage(cookies=[{"name": "a", "value": "1"}, {"name": "b", "value": "2"}])
        assert get_cookies(page) == {"a": "1", "b": "2"}

    def test_read_cookie_can_percent_decode(self):
        page = FakePage(cookies=[{"name": "tok", "value": "a%3A%3Ab"}])
        assert read_cookie(page, "tok") == "a%3A%3Ab"
        assert read_cookie(page, "tok", unquote_value=True) == "a::b"

    def test_read_cookie_missing_returns_empty(self):
        assert read_cookie(FakePage(), "nope") == ""

    def test_wait_for_cookie_returns_empty_string_on_timeout(self):
        # 返回空串而不是抛错：是不是失败由调用方判断（cursor 靠它区分"还没登上"）
        assert wait_for_cookie(FakePage(), "tok", timeout=0.05) == ""

    def test_wait_for_cookie_returns_value_once_it_appears(self):
        page = FakePage()
        calls = {"n": 0}
        original = page.cookies

        def cookies():
            calls["n"] += 1
            if calls["n"] >= 3:
                return [{"name": "tok", "value": "v1"}]
            return original()

        page.cookies = cookies
        assert wait_for_cookie(page, "tok", timeout=5) == "v1"


CF_HTML = "<html><body>Just a moment... Ray ID: abc</body></html>"


class TestCloudflareFullBlock:
    def test_cf_text_without_form_is_a_full_block(self):
        assert is_cf_full_block(FakePage(content=CF_HTML)) is True

    def test_cf_text_with_form_is_only_an_embedded_widget(self):
        # 表单还在 = 页面正常，只是内嵌了 Turnstile widget，不该当成全页拦截
        page = FakePage({DEFAULT_FORM_SELECTOR: FakeElement()}, content=CF_HTML)
        assert is_cf_full_block(page) is False

    def test_form_selector_is_per_platform(self):
        page = FakePage({'input[name="custom"]': FakeElement()}, content=CF_HTML)
        assert is_cf_full_block(page) is True
        assert is_cf_full_block(page, form_selector='input[name="custom"]') is False

    def test_normal_page_is_not_blocked(self):
        assert is_cf_full_block(FakePage(content="<html>hello</html>")) is False

    def test_content_failure_is_not_reported_as_blocked(self):
        class Broken(FakePage):
            def content(self):
                raise RuntimeError("navigating")

        assert is_cf_full_block(Broken()) is False

    def test_wait_clear_returns_as_soon_as_is_blocked_flips(self):
        page = FakePage(content=CF_HTML)
        page.frames = []
        page.viewport_size = {"width": 800, "height": 600}
        seen = {"n": 0}

        def is_blocked(_page):
            seen["n"] += 1
            return seen["n"] < 3

        logs = []
        started = time.time()
        wait_cf_full_block_clear(page, 10, logs.append, is_blocked=is_blocked)
        assert seen["n"] == 3
        assert any("Cloudflare" in line for line in logs)
        assert time.time() - started < 8

    def test_wait_clear_is_interrupted_by_cancellation(self):
        page = FakePage(content=CF_HTML)
        page.frames = []
        page.viewport_size = {"width": 800, "height": 600}
        token = CancellationToken()
        token.trip("用户取消")
        started = time.time()
        with pytest.raises(AttemptCancelled):
            wait_cf_full_block_clear(page, 120, lambda *_: None, is_blocked=lambda _p: True, should_stop=token)
        assert time.time() - started < 3
