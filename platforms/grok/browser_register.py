"""Grok (x.ai) 浏览器注册流程（Camoufox）。"""
import random
import re
import string
import time
from typing import Callable, Optional
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox
from platforms.grok.core import TURNSTILE_SITEKEY

ACCOUNTS_URL = "https://accounts.x.ai"
GROK_APP_URL = "https://grok.com"


def _make_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$"
    return "".join(random.choices(chars, k=length))


def _build_proxy_config(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        return {"server": proxy}
    config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


def _get_cookies(page, names: list) -> dict:
    return {c["name"]: c["value"] for c in page.context.cookies() if c["name"] in names}


def _wait_for_cookies(page, names: list, timeout: int = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = _get_cookies(page, names)
        if all(n in found for n in names):
            return found
        time.sleep(1)
    return _get_cookies(page, names)


def _write_debug_html(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(content)


def _click_first(page, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            node = page.query_selector(selector)
            if node and node.is_visible() and node.is_enabled():
                node.click()
                return selector
        except Exception:
            continue
    return None


def _extract_feedback(page) -> str:
    selectors = [
        '[role="alert"]',
        '.error',
        'p[class*="danger"]',
        'p[class*="error"]',
        '[class*="foreground-danger"]',
    ]
    messages: list[str] = []
    for selector in selectors:
        try:
            nodes = page.query_selector_all(selector)
        except Exception:
            nodes = []
        for node in nodes:
            try:
                text = (node.inner_text() or "").strip()
            except Exception:
                text = ""
            if text and text not in messages:
                messages.append(text)
    return " | ".join(messages)


def _feedback_mentions_turnstile(feedback: str) -> bool:
    lowered = (feedback or "").lower()
    return any(
        marker in lowered
        for marker in (
            "turnstile",
            "captcha",
            "cloudflare",
            "verify you are human",
            "security challenge",
        )
    )


def _get_turnstile_state(page) -> dict:
    try:
        state = page.evaluate(
            """
            () => {
                const widget = document.querySelector('[data-sitekey], .cf-turnstile, [data-captcha-sitekey]');
                const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]');
                const response = document.querySelector(
                    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], input[name="captcha"]'
                );
                const text = (document.body?.innerText || '').toLowerCase();
                return {
                    hasWidget: !!widget,
                    hasIframe: !!iframe,
                    hasResponseField: !!response,
                    responseLength: response && typeof response.value === 'string' ? response.value.length : 0,
                    sitekey: widget
                        ? (
                            widget.getAttribute('data-sitekey') ||
                            widget.getAttribute('data-captcha-sitekey') ||
                            ''
                        )
                        : '',
                    bodyText: text,
                };
            }
            """
        )
    except Exception:
        state = {}
    return {
        "hasWidget": bool(state.get("hasWidget")),
        "hasIframe": bool(state.get("hasIframe")),
        "hasResponseField": bool(state.get("hasResponseField")),
        "responseLength": int(state.get("responseLength") or 0),
        "sitekey": (state.get("sitekey") or "").strip(),
        "bodyText": (state.get("bodyText") or "").strip(),
    }


def _get_turnstile_sitekey(page) -> str:
    state = _get_turnstile_state(page)
    if state.get("sitekey"):
        return state["sitekey"]

    html = page.content()
    match = re.search(
        r'(?:data-sitekey|data-captcha-sitekey)=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    return TURNSTILE_SITEKEY


def _install_turnstile_hook(page) -> None:
    page.add_init_script(
        """
        (() => {
            const state = window.__grokTurnstileState = window.__grokTurnstileState || {
                last: null,
                renders: [],
            };

            const wrapTurnstile = (value) => {
                if (!value || typeof value.render !== 'function' || value.__grokWrapped) {
                    return value;
                }
                const originalRender = value.render.bind(value);
                value.render = function(container, options) {
                    try {
                        const payload = {
                            sitekey: String(options?.sitekey || ''),
                            action: String(options?.action || ''),
                            cdata: String(options?.cData || options?.cdata || ''),
                        };
                        state.last = payload;
                        state.renders.push(payload);
                        if (typeof options?.callback === 'function') {
                            window._turnstileTokenCallback = options.callback;
                        }
                    } catch (e) {}
                    return originalRender(container, options);
                };
                value.__grokWrapped = true;
                return value;
            };

            let turnstileRef = wrapTurnstile(window.turnstile);
            Object.defineProperty(window, 'turnstile', {
                configurable: true,
                get() {
                    return turnstileRef;
                },
                set(value) {
                    turnstileRef = wrapTurnstile(value);
                },
            });
        })();
        """
    )


def _get_turnstile_metadata(page) -> dict:
    try:
        meta = page.evaluate(
            """
            () => {
                const state = window.__grokTurnstileState || {};
                const widget = document.querySelector('[data-sitekey], .cf-turnstile, [data-captcha-sitekey]');
                const domSitekey = widget
                    ? (
                        widget.getAttribute('data-sitekey') ||
                        widget.getAttribute('data-captcha-sitekey') ||
                        ''
                    )
                    : '';
                const domAction = widget ? (widget.getAttribute('data-action') || '') : '';
                const domCdata = widget ? (widget.getAttribute('data-cdata') || '') : '';
                const last = state.last || {};
                return {
                    sitekey: String(last.sitekey || domSitekey || ''),
                    action: String(last.action || domAction || ''),
                    cdata: String(last.cdata || domCdata || ''),
                };
            }
            """
        )
    except Exception:
        meta = {}

    sitekey = (meta.get("sitekey") or "").strip()
    if not sitekey:
        sitekey = _get_turnstile_sitekey(page)
    return {
        "sitekey": sitekey or TURNSTILE_SITEKEY,
        "action": (meta.get("action") or "").strip(),
        "cdata": (meta.get("cdata") or "").strip(),
    }


def _turnstile_satisfied(state: dict) -> bool:
    return int(state.get("responseLength") or 0) > 0


def _turnstile_visible(page, state: Optional[dict] = None) -> bool:
    state = state or _get_turnstile_state(page)
    if state.get("hasWidget") or state.get("hasIframe"):
        return True
    if state.get("hasResponseField") and not _turnstile_satisfied(state):
        return True
    text = state.get("bodyText", "")
    return any(
        marker in text
        for marker in (
            "verify you are human",
            "verifying you are human",
            "confirm you are human",
            "cloudflare",
        )
    )


def _inject_turnstile(page, token: str) -> bool:
    safe = token.replace("\\", "\\\\").replace("'", "\\'")
    script = f"""(function() {{
        const token = '{safe}';
        const form = document.querySelector('form') || document.body;
        const names = ['captcha', 'cf-turnstile-response'];

        if (window.turnstile) {{
            const original = window.turnstile;
            window.turnstile = new Proxy(original, {{
                get(target, prop) {{
                    if (prop === 'getResponse') return () => token;
                    if (prop === 'isExpired') return () => false;
                    return Reflect.get(target, prop);
                }}
            }});
        }}

        const callbacks = [
            window._turnstileTokenCallback,
            window.turnstileCallback,
            window.onTurnstileSuccess,
            window.cfTurnstileCallback,
        ];
        callbacks.forEach((fn) => {{
            if (typeof fn === 'function') {{
                try {{ fn(token); }} catch (e) {{}}
            }}
        }});

        names.forEach((name) => {{
            let field = document.querySelector(`input[name="${{name}}"], textarea[name="${{name}}"]`);
            if (!field) {{
                field = document.createElement(name.includes('response') ? 'textarea' : 'input');
                if (field.tagName === 'INPUT') field.type = 'hidden';
                field.name = name;
                form.appendChild(field);
            }}
            field.value = token;
            field.dispatchEvent(new Event('input', {{ bubbles: true }}));
            field.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }});

        try {{
            document.querySelectorAll('iframe').forEach((iframe) => {{
                if (iframe.src && iframe.src.includes('cloudflare.com')) {{
                    iframe.contentWindow.postMessage(JSON.stringify({{
                        source: 'cloudflare-challenge',
                        token: token,
                    }}), '*');
                }}
            }});
        }} catch (e) {{}}

        return true;
    }})();"""
    try:
        return bool(page.evaluate(script))
    except Exception:
        return False


def _wait_for_manual_turnstile(page, log_fn=print, timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    warned = False
    while time.time() < deadline:
        state = _get_turnstile_state(page)
        if _turnstile_satisfied(state):
            return True
        if not _turnstile_visible(page, state):
            return True
        if not warned:
            log_fn(f"请在浏览器中手动点击 Turnstile，最长等待 {timeout} 秒")
            warned = True
        time.sleep(2)
    return False


def _wait_for_auto_turnstile(page, log_fn=print, timeout: int = 45) -> bool:
    deadline = time.time() + timeout
    attempts = 0
    warned = False
    while time.time() < deadline:
        state = _get_turnstile_state(page)
        if _turnstile_satisfied(state):
            return True
        if not _turnstile_visible(page, state):
            return True
        clicked = False
        for frame in page.frames:
            if "challenges.cloudflare.com" not in frame.url:
                continue
            try:
                iframe_el = frame.frame_element()
                box = iframe_el.bounding_box()
            except Exception:
                box = None
            if not box:
                continue
            cx = box["x"] + min(28, max(box["width"] * 0.15, 18))
            cy = box["y"] + (box["height"] / 2)
            try:
                page.mouse.move(cx - 18, cy - 4)
                time.sleep(0.2)
                page.mouse.move(cx, cy)
                time.sleep(0.2)
                page.mouse.click(cx, cy, delay=120)
                clicked = True
                attempts += 1
                if not warned:
                    log_fn(f"自动点击 Turnstile checkbox，最长等待 {timeout} 秒")
                    warned = True
                log_fn(f"已尝试自动点击 Turnstile ({attempts})")
                time.sleep(4)
                break
            except Exception:
                continue
        if not clicked:
            try:
                label = page.locator("text=Verify you are human").first
                box = label.bounding_box()
            except Exception:
                box = None
            if box:
                cx = max(box["x"] - 18, 8)
                cy = box["y"] + (box["height"] / 2)
                try:
                    page.mouse.move(cx - 12, cy)
                    time.sleep(0.2)
                    page.mouse.move(cx, cy)
                    time.sleep(0.2)
                    page.mouse.click(cx, cy, delay=120)
                    clicked = True
                    attempts += 1
                    if not warned:
                        log_fn(f"自动点击 Turnstile checkbox，最长等待 {timeout} 秒")
                        warned = True
                    log_fn(f"已根据文本定位自动点击 Turnstile ({attempts})")
                    time.sleep(4)
                except Exception:
                    clicked = False
        if not clicked:
            time.sleep(2)
    return False


class GrokBrowserRegister:
    def __init__(
        self,
        *,
        captcha=None,
        headless: bool,
        proxy: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
        log_fn: Callable[[str], None] = print,
    ):
        self.captcha = captcha
        self.headless = headless
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.log = log_fn

    def _solve_turnstile(
        self,
        url: str,
        sitekey: str,
        *,
        action: str = "",
        cdata: str = "",
    ) -> Optional[str]:
        if not self.captcha:
            self.log("未配置 Captcha Solver，跳过自动解题")
            return None
        try:
            proxy_hint = "使用任务代理" if self.proxy else "未使用任务代理"
            params_hint = []
            if action:
                params_hint.append(f"action={action}")
            if cdata:
                params_hint.append("cdata=Y")
            detail = f", {', '.join(params_hint)}" if params_hint else ""
            self.log(f"调用 Captcha Solver 解题 ({sitekey[:20]}..., {proxy_hint}{detail})...")
            token = self.captcha.solve_turnstile(
                url,
                sitekey or TURNSTILE_SITEKEY,
                proxy=self.proxy,
                action=action or None,
                cdata=cdata or None,
            )
            if token:
                self.log(f"✅ Solver 返回 token: {token[:50]}...")
            return token
        except Exception as exc:
            self.log(f"⚠️ Captcha Solver 失败: {exc}")
            return None

    def _handle_turnstile(self, page, *, wait_secs: int = 12) -> bool:
        deadline = time.time() + wait_secs
        state = {}
        while time.time() < deadline:
            state = _get_turnstile_state(page)
            if _turnstile_satisfied(state):
                return True
            if _turnstile_visible(page, state):
                break
            time.sleep(1)
        else:
            return True

        metadata = _get_turnstile_metadata(page)
        sitekey = metadata.get("sitekey") or state.get("sitekey") or _get_turnstile_sitekey(page)
        self.log(
            "Turnstile DOM: "
            f"widget={'Y' if state.get('hasWidget') else 'N'}, "
            f"iframe={'Y' if state.get('hasIframe') else 'N'}, "
            f"field={'Y' if state.get('hasResponseField') else 'N'}, "
            f"respLen={state.get('responseLength', 0)}"
        )
        self.log(f"检测到 Turnstile challenge，sitekey={sitekey[:24]}...")
        self.log("先尝试在当前页面自动点击 Turnstile")
        if _wait_for_auto_turnstile(page, self.log, timeout=20):
            return True
        if metadata.get("action") or metadata.get("cdata"):
            self.log(
                "Turnstile 参数: "
                f"action={metadata.get('action') or '-'}, "
                f"cdata={'Y' if metadata.get('cdata') else 'N'}"
            )
        self.log("当前页面自动点击未完成，再尝试本地 Solver 注入 token")
        token = self._solve_turnstile(
            page.url,
            sitekey,
            action=metadata.get("action", ""),
            cdata=metadata.get("cdata", ""),
        )
        if token and _inject_turnstile(page, token):
            self.log("已注入 Turnstile token")
            time.sleep(2)
            if _turnstile_satisfied(_get_turnstile_state(page)):
                return True

        self.log("Solver 未完成，重新尝试当前页面自动点击")
        if _wait_for_auto_turnstile(page, self.log, timeout=30):
            return True

        self.log("自动解题未完成，回退到人工点击")
        return _wait_for_manual_turnstile(page, self.log, timeout=120)

    def _submit_complete_signup(self, page, password: str, btn_sel: str) -> None:
        password_sel = 'input[name="password"], input[type="password"]'
        submit_selectors = [
            'button:has-text("Complete sign up")',
            'button:has-text("Sign up")',
            'button[type="submit"]',
            btn_sel,
        ]

        for attempt in range(1, 3):
            if page.query_selector(password_sel):
                page.fill(password_sel, password)

            if not self._handle_turnstile(page):
                raise RuntimeError("未完成 Turnstile 验证")

            clicked = _click_first(page, submit_selectors)
            if not clicked and page.query_selector(password_sel):
                page.press(password_sel, "Enter")
            time.sleep(4)

            if not page.query_selector(password_sel):
                return

            feedback = _extract_feedback(page)
            state = _get_turnstile_state(page)
            if feedback:
                self.log(f"提交后页面提示: {feedback}")
            if attempt < 2 and (_turnstile_visible(page, state) or _feedback_mentions_turnstile(feedback)):
                self.log("提交后仍停留在注册页，重新处理 Turnstile 并重试一次")
                continue
            return

    def run(self, email: str, password: str) -> dict:
        use_password = password or _make_password()
        proxy = _build_proxy_config(self.proxy)
        launch_opts = {"headless": self.headless}
        if proxy:
            launch_opts["proxy"] = proxy

        with Camoufox(**launch_opts) as browser:
            page = browser.new_page()
            _install_turnstile_hook(page)
            self.log("打开 Grok 注册页")
            page.goto(f"{ACCOUNTS_URL}/sign-up", wait_until="networkidle", timeout=30000)
            time.sleep(2)

            # Click "Sign up with email" if it exists
            btn_email_sel = 'button:has-text("Sign up with email")'
            if page.query_selector(btn_email_sel):
                page.click(btn_email_sel)
                time.sleep(2)

            # Step 1: fill email
            email_sel = 'input[type="email"], input[name="email"], input[name="username"]'
            page.wait_for_selector(email_sel, timeout=15000)
            page.fill(email_sel, email)

            btn_sel = 'button[type="submit"], button[data-testid="continue"]'
            if page.query_selector(btn_sel):
                page.click(btn_sel)
            time.sleep(3)

            # OTP sent to email
            try:
                page.wait_for_selector('input[name="code"], input[placeholder*="code"], input[placeholder*="Code"]', timeout=20000)
            except Exception:
                fb = ""
                for sel in ['[role="alert"]', '.error']:
                    el = page.query_selector(sel)
                    if el:
                        fb = el.inner_text()
                        break
                raise RuntimeError(f"未进入验证码页面: {fb or page.url}")

            if not self.otp_callback:
                raise RuntimeError("Grok 注册需要邮箱验证码但未提供 otp_callback")
            self.log("等待 Grok 验证码")
            code = self.otp_callback()
            if not code:
                raise RuntimeError("未获取到验证码")

            code_sel = 'input[name="code"], input[data-input-otp="true"]'
            if not page.query_selector(code_sel):
                code_sel = 'input[placeholder*="code"], input[placeholder*="Code"]'
            
            # The input expects 6 characters without hyphen
            clean_code = code.replace("-", "")
            try:
                page.fill(code_sel, clean_code, force=True)
            except:
                page.locator(code_sel).press_sequentially(clean_code)

            confirm_btn = 'button:has-text("Confirm email")'
            if page.query_selector(confirm_btn):
                page.click(confirm_btn, force=True)
            elif page.query_selector(btn_sel):
                page.click(btn_sel, force=True)
            time.sleep(3)

            # May need name + password
            self.log("等待姓名/密码填写步骤")
            for _ in range(15):
                if page.query_selector('input[name="given_name"], input[placeholder*="First"], input[name="password"], input[type="password"]'):
                    break
                time.sleep(1)
            else:
                self.log("未检测到姓名或密码输入框，保存截图到 /tmp/grok_debug.png")
                page.screenshot(path="/tmp/grok_debug.png")
                _write_debug_html("/tmp/grok_debug.html", page.content())

            # DEBUG SCREENSHOT
            page.screenshot(path="/tmp/grok_name_pass.png")

            if page.query_selector('input[name="given_name"], input[name="givenName"], input[placeholder*="First"]'):
                first = ''.join(random.choices(string.ascii_lowercase, k=5)).capitalize()
                last = ''.join(random.choices(string.ascii_lowercase, k=5)).capitalize()
                fname_sel = 'input[name="given_name"], input[name="givenName"], input[placeholder*="First"]'
                if page.query_selector(fname_sel):
                    page.fill(fname_sel, first)
                lname_sel = 'input[name="family_name"], input[name="familyName"], input[placeholder*="Last"]'
                if page.query_selector(lname_sel):
                    page.fill(lname_sel, last)
                # DO NOT CLICK YET if password is also on screen
                if not page.query_selector('input[name="password"], input[type="password"]'):
                    if page.query_selector(btn_sel):
                        page.click(btn_sel)
                    time.sleep(2)

            for _ in range(10):
                if page.query_selector('input[name="password"], input[type="password"]'):
                    break
                time.sleep(1)

            if page.query_selector('input[name="password"], input[type="password"]'):
                self._submit_complete_signup(page, use_password, btn_sel)

            # Wait for sso cookie
            self.log("等待 Grok sso cookie")
            cookies = _wait_for_cookies(page, ["sso"], timeout=60)
            sso = cookies.get("sso", "")
            if not sso:
                self.log("未获取到 sso cookie，保存截图到 /tmp/grok_fail_final.png")
                feedback = _extract_feedback(page)
                if feedback:
                    self.log(f"失败页提示: {feedback}")
                page.screenshot(path="/tmp/grok_fail_final.png")
                _write_debug_html("/tmp/grok_fail_final.html", page.content())
                raise RuntimeError("未获取到 Grok sso cookie")
            sso_rw = _get_cookies(page, ["sso-rw"]).get("sso-rw", "")
            self.log(f"注册成功: {email}")
            return {"email": email, "password": use_password, "sso": sso, "sso_rw": sso_rw}
