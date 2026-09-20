"""浏览器流程的公共页面辅助。

Step 平台此前各写一份：cursor 的 Turnstile / CF 全页拦截处理被 openblocklabs
与 windsurf 近乎逐字复制过，chatgpt 又自己写了一套选择器轮询。这里收成一份，
平台侧只留站点特有的常量（选择器、cookie 名、sitekey）。

约定：
- 这里的函数与 ``StepContext`` 无关，只接 ``*, should_stop=None``；
  Step 调用时传 ``should_stop=sc.cancel``，脚本直调时不传。
- 所有等待走 ``core.cancellation`` 的可取消原语，不裸用 ``time.sleep``；
  DOM 动作之间的亚秒抖动（模拟人类操作）例外。
"""
from __future__ import annotations

import random
import time
from urllib.parse import unquote as _unquote

from core.cancellation import cancellable_sleep, poll_until

# CF Turnstile 会检测 CDP 触发的 MouseEvent.screenX == clientX，注入补丁骗过它
MOUSE_EVENT_PATCH = """
(function() {
    var screenX = Math.floor(Math.random() * (1200 - 800 + 1)) + 800;
    var screenY = Math.floor(Math.random() * (600 - 400 + 1)) + 400;
    for (const proto of [MouseEvent.prototype, PointerEvent.prototype]) {
        Object.defineProperty(proto, 'screenX', {
            get: function() { return this.clientX + screenX; },
            configurable: true
        });
        Object.defineProperty(proto, 'screenY', {
            get: function() { return this.clientY + screenY; },
            configurable: true
        });
    }
})();
"""

#: 没有显式传 form_selector 时，用来判断"页面上还有正常表单"的通用选择器
DEFAULT_FORM_SELECTOR = (
    'input[name="email"], input[type="email"], input[name="firstName"], '
    'input[type="password"], input[name="otp"], input[name="code"], '
    'input[autocomplete="one-time-code"]'
)

CF_IFRAME_HOST = "challenges.cloudflare.com"

# 全页拦截的关键词（不含 challenges.cloudflare.com，因为那是内嵌 widget 的 script）
CF_FULL_BLOCK_SIGNALS = (
    "just a moment",
    "checking your browser",
    "verifying you are human",
    "verify you are human",
    "performing security verification",
    "security check to access",
    "ray id",
)

TURNSTILE_MODAL_SIGNALS = (
    "confirm you are human",
    "确认您是真人",
    "we need to confirm you are human",
    "需要确认您是真人",
)


# ---------------------------------------------------------------------------
# 选择器
# ---------------------------------------------------------------------------

def find_first_selector(page, selectors) -> str | None:
    """返回第一个能查到节点的选择器（不判可见性）。"""
    for selector in selectors:
        try:
            node = page.query_selector(selector)
        except Exception:
            node = None
        if node:
            return selector
    return None


def find_first_visible(page, selectors):
    """返回第一个可见节点，附带命中的选择器。"""
    for selector in selectors:
        try:
            element = page.query_selector(selector)
            if element and element.is_visible():
                return element, selector
        except Exception:
            continue
    return None, None


def wait_for_any_selector(
    page, selectors, *, timeout: float = 30, interval: float = 0.5, should_stop=None
) -> str | None:
    return poll_until(
        lambda: find_first_selector(page, selectors),
        timeout=timeout,
        interval=interval,
        should_stop=should_stop,
    )


def click_first(
    page, selectors, *, timeout: float = 10, interval: float = 0.5, should_stop=None
) -> str | None:
    found = wait_for_any_selector(
        page, selectors, timeout=timeout, interval=interval, should_stop=should_stop
    )
    if not found:
        return None
    try:
        page.click(found)
        return found
    except Exception:
        return None


def click_first_visible(page, selectors, *, click_timeout: int = 3000) -> str | None:
    """点第一个可见且可用的节点；没有可点的返回 None（调用方决定要不要回车兜底）。"""
    for selector in selectors:
        try:
            element = page.query_selector(selector)
            if element and element.is_visible() and element.is_enabled():
                element.click(timeout=click_timeout)
                return selector
        except Exception:
            continue
    return None


def fill_first_visible(page, selectors, value: str, *, settle: float = 0.0) -> str | None:
    """填第一个可见输入框，返回命中的选择器。"""
    element, selector = find_first_visible(page, selectors)
    if element is None:
        return None
    element.fill(value)
    if settle > 0:
        time.sleep(settle)
    return selector


# ---------------------------------------------------------------------------
# Cookie
# ---------------------------------------------------------------------------

def get_cookies(page) -> dict:
    return {c["name"]: c["value"] for c in page.context.cookies()}


def read_cookie(page, name: str, *, unquote_value: bool = False) -> str:
    for cookie in page.context.cookies():
        if cookie["name"] == name:
            value = cookie["value"]
            return _unquote(value) if unquote_value else value
    return ""


def wait_for_cookie(page, name: str, *, timeout: float = 120, should_stop=None, unquote_value: bool = False) -> str:
    """轮询到某个 cookie 出现；超时返回空串（由调用方决定算不算失败）。"""
    found = poll_until(
        lambda: read_cookie(page, name, unquote_value=unquote_value),
        timeout=timeout,
        interval=1.0,
        should_stop=should_stop,
    )
    return found or ""


# ---------------------------------------------------------------------------
# Cloudflare 全页拦截
# ---------------------------------------------------------------------------

def is_cf_full_block(page, *, form_selector: str = DEFAULT_FORM_SELECTOR) -> bool:
    """检测是否被 CF 全页拦截（区别于表单内嵌 Turnstile widget）。

    全页拦截特征：页面只有 CF 挑战，没有正常的表单内容。
    内嵌 Turnstile：页面表单正常显示，只是其中有 CF iframe。
    """
    try:
        content = page.content().lower()
        if any(kw in content for kw in CF_FULL_BLOCK_SIGNALS):
            # 同时确认没有正常表单（有表单 = 只是内嵌 widget，不是全页拦截）
            if not page.query_selector(form_selector):
                return True
    except Exception:
        pass
    return False


def _human_mouse_wander(page, rounds: int = 3) -> None:
    """随机移动鼠标——CF 的被动检测会观察鼠标行为。"""
    try:
        size = page.viewport_size or {"width": 1280, "height": 720}
        for _ in range(rounds):
            page.mouse.move(
                random.randint(100, size["width"] - 100),
                random.randint(100, size["height"] - 100),
            )
            time.sleep(random.uniform(0.1, 0.3))
    except Exception:
        pass


def _human_mouse_click(page, cx: float, cy: float, *, approach: bool = False) -> None:
    """贴近人类的移动 + 按下 + 松开；approach=True 时先从随机位置平滑靠近。"""
    if approach:
        size = page.viewport_size or {"width": 1280, "height": 720}
        cur_x = random.randint(200, max(201, size["width"] - 200))
        cur_y = random.randint(200, max(201, size["height"] - 200))
        page.mouse.move(cur_x, cur_y)
        steps = random.randint(8, 15)
        for i in range(steps):
            t = (i + 1) / steps
            # 贝塞尔式平滑插值 + 抖动
            page.mouse.move(
                cur_x + (cx - cur_x) * t + random.randint(-15, 15),
                cur_y + (cy - cur_y) * t + random.randint(-8, 8),
            )
            time.sleep(random.uniform(0.02, 0.07))
        page.mouse.move(cx, cy)
    else:
        page.mouse.move(cx + random.randint(-5, 5), cy + random.randint(-3, 3))
    time.sleep(random.uniform(0.1, 0.3))
    page.mouse.down()
    time.sleep(random.uniform(0.08, 0.15))
    page.mouse.up()


def _cf_iframe_box(page):
    """找到 CF interstitial iframe 的 bounding box。"""
    for frame in page.frames:
        if CF_IFRAME_HOST in frame.url:
            box = frame.frame_element().bounding_box()
            if box:
                return box
    return None


def wait_cf_full_block_clear(
    page,
    timeout: float = 120,
    log_fn=print,
    *,
    is_blocked=None,
    should_stop=None,
) -> None:
    """等待 CF 全页拦截消失，并主动点击 Interstitial Turnstile checkbox。

    CF 全页拦截分两种：
    1. Interactive Turnstile：显示 checkbox，需要点击
    2. Managed Challenge：no-checkbox 被动验证，显示圆圈/加载中，就等
    """
    blocked = is_blocked or is_cf_full_block
    deadline = time.time() + timeout
    warned = False
    clicked = False
    while time.time() < deadline:
        if not blocked(page):
            break
        if not warned:
            log_fn("检测到 Cloudflare 全页拦截，尝试点击验证 checkbox...")
            warned = True
        _human_mouse_wander(page)
        if not clicked:
            try:
                box = _cf_iframe_box(page)
            except Exception:
                box = None
            if box:
                try:
                    cx = box["x"] + 24
                    cy = box["y"] + box["height"] / 2
                    # 先"看几秒页面"再缓慢移过去，纯瞬移会被判成脚本
                    cancellable_sleep(random.uniform(1.5, 3.0), should_stop=should_stop)
                    _human_mouse_click(page, cx, cy, approach=True)
                    log_fn(f"点击 Interstitial checkbox 坐标: ({cx:.0f}, {cy:.0f})")
                    clicked = True
                    cancellable_sleep(3, should_stop=should_stop)
                    continue
                except Exception:
                    pass
            # iframe 还没加载，稍等再试
            cancellable_sleep(1, should_stop=should_stop)
        else:
            # 已点击，等待 CF 被动验证完成（Managed Challenge 可能需要较长时间）
            cancellable_sleep(2, should_stop=should_stop)


# ---------------------------------------------------------------------------
# 内嵌 Turnstile
# ---------------------------------------------------------------------------

def has_turnstile_iframe(page) -> bool:
    """检测页面中是否有 Turnstile iframe（包括内嵌 widget）。"""
    try:
        for frame in page.frames:
            if CF_IFRAME_HOST in frame.url:
                return True
        return bool(
            page.evaluate(
                """() => {
                    const iframes = document.querySelectorAll('iframe');
                    for (const f of iframes) {
                        if (f.src && f.src.includes('challenges.cloudflare.com')) return true;
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return False


def is_turnstile_modal_visible(page) -> bool:
    """检测 Turnstile 挑战是否可见（用 body 文字，因为 iframe 延迟加载）。"""
    try:
        content = page.content().lower()
        if any(s in content for s in TURNSTILE_MODAL_SIGNALS):
            return True
        # 也检查 iframe（有些环境下 iframe 会有 src）
        return has_turnstile_iframe(page)
    except Exception:
        return False


def get_turnstile_sitekey(page, default: str = "") -> str:
    try:
        sitekey = page.evaluate(
            """() => {
                const node = document.querySelector('[data-sitekey], .cf-turnstile, [data-captcha-sitekey]');
                return node ? (node.getAttribute('data-sitekey') || node.getAttribute('data-captcha-sitekey') || '') : '';
            }"""
        )
        if sitekey:
            return sitekey.strip()
    except Exception:
        pass
    return default


def inject_turnstile(page, token: str) -> bool:
    """注入 Turnstile token，兼容 explicit 渲染模式。"""
    safe = token.replace("\\", "\\\\").replace("'", "\\'")
    script = f"""(function() {{
        const token = '{safe}';

        // 1. override window.turnstile API（explicit 模式下服务端会调用 getResponse）
        if (window.turnstile) {{
            const orig = window.turnstile;
            window.turnstile = new Proxy(orig, {{
                get(target, prop) {{
                    if (prop === 'getResponse') return () => token;
                    if (prop === 'isExpired') return () => false;
                    return Reflect.get(target, prop);
                }}
            }});
        }}

        // 2. 触发所有已注册的 callback
        const fns = [
            window._turnstileTokenCallback,
            window.turnstileCallback,
            window.onTurnstileSuccess,
            window.cfTurnstileCallback,
        ];
        fns.forEach(fn => {{ if (typeof fn === 'function') {{ try {{ fn(token); }} catch(e) {{}} }} }});

        // 3. 注入 hidden input（兼容 form submit 模式）
        const names = ['captcha', 'cf-turnstile-response'];
        const form = document.querySelector('form') || document.body;
        names.forEach(name => {{
            let f = document.querySelector('input[name="' + name + '"], textarea[name="' + name + '"]');
            if (!f) {{ f = document.createElement('input'); f.type = 'hidden'; f.name = name; form.appendChild(f); }}
            f.value = token;
            f.dispatchEvent(new Event('input', {{bubbles: true}}));
            f.dispatchEvent(new Event('change', {{bubbles: true}}));
        }});

        // 4. 尝试直接触发 Turnstile 内部 callback（通过 iframe postMessage）
        try {{
            document.querySelectorAll('iframe').forEach(iframe => {{
                if (iframe.src && iframe.src.includes('cloudflare.com')) {{
                    iframe.contentWindow.postMessage(JSON.stringify({{
                        source: 'cloudflare-challenge',
                        token: token,
                    }}), '*');
                }}
            }});
        }} catch(e) {{}}

        return true;
    }})();"""
    return bool(page.evaluate(script))


def click_turnstile_in_iframe(page, log_fn=print, *, should_stop=None) -> bool:
    """在浏览器里直接找到 Turnstile iframe 并点击 checkbox。

    Turnstile iframe 内部使用 closed Shadow DOM，JS querySelector 访问不到。
    改用 bounding box 坐标点击：checkbox 在 iframe 左侧约 1/4 处。
    返回 True 表示点了（不代表 Turnstile 已通过）。
    """
    def _find_frame():
        for frame in page.frames:
            if CF_IFRAME_HOST in frame.url:
                return frame
        return None

    cf_frame_obj = poll_until(_find_frame, timeout=15, interval=0.5, should_stop=should_stop)
    if not cf_frame_obj:
        log_fn("未找到 Cloudflare iframe frame，跳过直接点击")
        return False

    log_fn(f"找到 Turnstile frame: {cf_frame_obj.url[:80]}...")

    iframe_el = None
    for el in page.query_selector_all("iframe"):
        try:
            if "cloudflare.com" in (el.get_attribute("src") or ""):
                iframe_el = el
                break
        except Exception:
            continue
    if not iframe_el:
        # src 可能为空（动态设置），用 frame owner element
        try:
            iframe_el = cf_frame_obj.frame_element()
        except Exception:
            pass

    if iframe_el:
        try:
            # 等 bounding box 有效：iframe 渲染要时间，height=0 时点了也没用
            def _ready_box():
                b = iframe_el.bounding_box()
                return b if (b and b["height"] > 10 and b["y"] > 0) else None

            box = poll_until(_ready_box, timeout=10, interval=1.0, should_stop=should_stop)
            if box:
                cx = box["x"] + 24
                cy = box["y"] + box["height"] / 2
                _human_mouse_click(page, cx, cy)
                log_fn(f"点击 Turnstile checkbox 坐标: ({cx:.0f}, {cy:.0f})")
                cancellable_sleep(1.5, should_stop=should_stop)
                # 还没通过就再试一次偏右
                if is_turnstile_modal_visible(page):
                    page.mouse.move(cx + 12, cy)
                    time.sleep(0.1)
                    page.mouse.down()
                    time.sleep(0.1)
                    page.mouse.up()
                    cancellable_sleep(1, should_stop=should_stop)
                return True
            log_fn("bounding box 无效（height=0），跳过点击")
        except Exception as exc:
            log_fn(f"bounding box 点击失败: {exc}")

    # 兜底：用 Playwright frame 内坐标点击（相对于 frame）
    try:
        log_fn("尝试 frame 内坐标点击...")
        cf_frame_obj.locator("body").click(position={"x": 24, "y": 32}, timeout=5000)
        log_fn("frame 内坐标点击成功")
        return True
    except Exception as exc:
        log_fn(f"frame 内点击失败: {exc}")

    log_fn("所有点击方式均失败")
    return False
