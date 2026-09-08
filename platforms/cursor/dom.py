"""Cursor 注册页的 DOM / Turnstile / Cloudflare 辅助函数。

被 Step 流程（steps.py）与 ObstacleHandler 装配（obstacles.py）共用；
旧的整段式 CursorBrowserRegister 已由 Step 引擎取代并删除。
"""
import random
import time
from urllib.parse import unquote

AUTH = "https://authenticator.cursor.sh"
CURSOR = "https://cursor.com"
TURNSTILE_SITEKEY = "0x4AAAAAAAMNIvC45A4Wjjln"


def _get_turnstile_sitekey(page) -> str:
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
    return TURNSTILE_SITEKEY


def _inject_turnstile(page, token: str) -> bool:
    """注入 Turnstile token，兼容 explicit 渲染模式（Cursor 使用此模式）。"""
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


def _click_continue(page) -> bool:
    """尝试点击 Continue/Next/Sign up 按钮，兜底用 Enter。"""
    for sel in [
        'button[data-action-button-primary="true"]',
        'button[type="submit"]:not([aria-hidden="true"])',
        'button:has-text("Continue")',
        'button:has-text("Next")',
        'button:has-text("Sign up")',
        'button[type="submit"]',
        'button',
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible() and el.is_enabled():
                el.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


def _get_token_from_cookies(page) -> str:
    for cookie in page.context.cookies():
        if cookie["name"] == "WorkosCursorSessionToken":
            return unquote(cookie["value"])
    return ""


def _wait_for_token(page, timeout: int = 120, *, should_stop=None) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        tok = _get_token_from_cookies(page)
        if tok:
            return tok
        if should_stop is not None and should_stop():
            from core.errors import AttemptCancelled

            raise AttemptCancelled("等待会话 token 期间任务被取消")
        time.sleep(1)
    return ""


def _is_cf_full_block(page) -> bool:
    """检测是否被 CF 全页拦截（区别于表单内嵌 Turnstile widget）。
    
    全页拦截特征：页面只有 CF 挑战，没有正常的表单内容。
    内嵌 Turnstile：页面表单正常显示，只是其中有 CF iframe。
    """
    try:
        content = page.content().lower()
        # 全页拦截的关键词（不含 challenges.cloudflare.com，因为那是 widget 的 script）
        full_block_signals = [
            "just a moment",
            "checking your browser",
            "verifying you are human",
            "verify you are human",
            "performing security verification",
            "security check to access",
            "ray id",
        ]
        if any(kw in content for kw in full_block_signals):
            # 同时确认没有正常表单（有表单=只是内嵌 widget，不是全页拦截）
            has_form = bool(page.query_selector('input[name="email"], input[name="firstName"], input[name="otp"], input[name="code"]'))
            if not has_form:
                return True
    except Exception:
        pass
    return False


def _wait_cf_full_block_clear(page, timeout: int = 120, log_fn=print) -> None:
    """等待 CF 全页拦截消失，并主动点击 Interstitial Turnstile checkbox。
    
    CF 全页拦截分两种：
    1. Interactive Turnstile：显示 checkbox，需要点击
    2. Managed Challenge： no-checkbox 被动验证，显示圆圈/加载中，就等
    """
    deadline = time.time() + timeout
    warned = False
    clicked = False
    while time.time() < deadline:
        if not _is_cf_full_block(page):
            break
        if not warned:
            log_fn("检测到 Cloudflare 全页拦截，尝试点击验证 checkbox...")            
            warned = True
        # 模拟人类鼠标移动（CF 被动检测会观察鼠标行为）
        try:
            w = page.viewport_size or {"width": 1280, "height": 720}
            for _ in range(3):
                page.mouse.move(
                    random.randint(100, w["width"] - 100),
                    random.randint(100, w["height"] - 100)
                )
                time.sleep(random.uniform(0.1, 0.3))
        except Exception:
            pass
        # 尝试找 CF interstitial Turnstile iframe 并点击
        if not clicked:
            try:
                for frame in page.frames:
                    if "challenges.cloudflare.com" in frame.url:
                        iframe_el = frame.frame_element()
                        box = iframe_el.bounding_box()
                        if box:
                            cx = box["x"] + 24
                            cy = box["y"] + box["height"] / 2
                            # 模拟人类行为：先等几秒"看页面"，再缓慢移动到目标
                            time.sleep(random.uniform(1.5, 3.0))
                            # 从当前位置平滑移动到 checkbox（分多步）
                            w = page.viewport_size or {"width": 1280, "height": 720}
                            cur_x = random.randint(200, w["width"] - 200)
                            cur_y = random.randint(200, w["height"] - 200)
                            page.mouse.move(cur_x, cur_y)
                            steps = random.randint(8, 15)
                            for i in range(steps):
                                t = (i + 1) / steps
                                # 贝塞尔曲线平滑插值
                                mid_x = cur_x + (cx - cur_x) * t + random.randint(-15, 15)
                                mid_y = cur_y + (cy - cur_y) * t + random.randint(-8, 8)
                                page.mouse.move(mid_x, mid_y)
                                time.sleep(random.uniform(0.02, 0.07))
                            # 最终移到目标并点击
                            page.mouse.move(cx, cy)
                            time.sleep(random.uniform(0.1, 0.3))
                            page.mouse.down()
                            time.sleep(random.uniform(0.08, 0.15))
                            page.mouse.up()
                            log_fn(f"✅ 点击 Interstitial checkbox 坐标: ({cx:.0f}, {cy:.0f})")
                            clicked = True
                            time.sleep(3)
                            break
            except Exception:
                pass
            if not clicked:
                # iframe 还没加载，稍等再试
                time.sleep(1)
        else:
            # 已点击，等待 CF 被动验证完成（Managed Challenge 可能需要较長时间）
            time.sleep(2)


def _has_turnstile_iframe(page) -> bool:
    """检测页面中是否有 Turnstile iframe（包括内嵌 widget）。"""
    try:
        for frame in page.frames:
            if "challenges.cloudflare.com" in frame.url:
                return True
        result = page.evaluate(
            """() => {
                const iframes = document.querySelectorAll('iframe');
                for (const f of iframes) {
                    if (f.src && f.src.includes('challenges.cloudflare.com')) return true;
                }
                return false;
            }"""
        )
        return bool(result)
    except Exception:
        return False

def _is_turnstile_modal_visible(page) -> bool:
    """检测 Turnstile 挑战是否可见（使用 body 文字，因为 iframe 延迟加载）。"""
    try:
        content = page.content().lower()
        signals = [
            "confirm you are human",
            "确认您是真人",
            "we need to confirm you are human",
            "需要确认您是真人",
        ]
        if any(s in content for s in signals):
            return True
        # 也检查 iframe（有些环境下 iframe 会有 src）
        return _has_turnstile_iframe(page)
    except Exception:
        return False


def _click_turnstile_in_iframe(page, log_fn=print) -> bool:
    """在注册 Camoufox 浏览器里直接找到 Turnstile iframe 并点击 checkbox。
    
    Turnstile iframe 内部使用 closed Shadow DOM，JS querySelector 无法访问。
    改用 bounding box 坐标点击：checkbox 在 iframe 左侧约 1/4 处。
    返回 True 表示点击了（不代表 Turnstile 已通过）。
    """
    # 等待 iframe 的 frame 出现在 page.frames 列表中
    deadline = time.time() + 15
    cf_frame_obj = None  # playwright Frame 对象
    while time.time() < deadline:
        for frame in page.frames:
            if "challenges.cloudflare.com" in frame.url:
                cf_frame_obj = frame
                break
        if cf_frame_obj:
            break
        time.sleep(0.5)

    if not cf_frame_obj:
        log_fn("未找到 Cloudflare iframe frame，跳过直接点击")
        return False

    log_fn(f"找到 Turnstile frame: {cf_frame_obj.url[:80]}...")

    # 找到 iframe DOM 元素，获取 bounding box
    iframe_el = None
    for el in page.query_selector_all("iframe"):
        try:
            src = el.get_attribute("src") or ""
            if "cloudflare.com" in src:
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
            # 等待 bounding box 有效（iframe 渲染需要时间，height=0 时不能点击）
            box = None
            for _ in range(10):
                b = iframe_el.bounding_box()
                if b and b["height"] > 10 and b["y"] > 0:
                    box = b
                    break
                time.sleep(1)
            if box:
                # checkbox 在 iframe 左侧，大约 x=24, y=center
                cx = box["x"] + 24
                cy = box["y"] + box["height"] / 2
                # 模拟人类点击：移动到目标再按下/松开
                page.mouse.move(cx + random.randint(-5, 5), cy + random.randint(-3, 3))
                time.sleep(random.uniform(0.1, 0.25))
                page.mouse.down()
                time.sleep(random.uniform(0.08, 0.15))
                page.mouse.up()
                log_fn(f"✅ 点击 Turnstile checkbox 坐标: ({cx:.0f}, {cy:.0f})")
                time.sleep(1.5)
                # 如果还没通过，再试一次偏右
                if _is_turnstile_modal_visible(page):
                    page.mouse.move(cx + 12, cy)
                    time.sleep(0.1)
                    page.mouse.down()
                    time.sleep(0.1)
                    page.mouse.up()
                    time.sleep(1)
                return True
            else:
                log_fn("bounding box 无效（height=0），跳过点击")
        except Exception as e:
            log_fn(f"bounding box 点击失败: {e}")

    # 兜底：用 Playwright frame 内坐标点击（相对于 frame）
    try:
        log_fn("尝试 frame 内坐标点击...")
        cf_frame_obj.locator("body").click(position={"x": 24, "y": 32}, timeout=5000)
        log_fn("✅ frame 内坐标点击成功")
        return True
    except Exception as e:
        log_fn(f"frame 内点击失败: {e}")

    log_fn("所有点击方式均失败")
    return False

