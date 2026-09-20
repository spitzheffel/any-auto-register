"""Trae.ai 注册页的选择器与页面动作。

从旧的整段式 ``browser_register.py`` 里抽出来：每个函数只对页面做一件事，
不含流程编排（在 steps.py），也不含浏览器生命周期（引擎管）。

Trae 前端是 ByteDance 那套组件库，按钮不是 ``<button>`` 而是带文字的 div，
所以"点 Send Code / Sign Up"没法用 CSS 选择器表达，只能按可见文本找叶子节点——
``click_leaf_text`` 就是旧实现那三层兜底（Playwright ``text=`` → JS 遍历叶子 →
键盘）的封装。API 请求带 X-Bogus/X-Gnarly 签名头，浏览器自动生成，不用管。
"""
from __future__ import annotations

import random
import string

from core.cancellation import cancellable_sleep, poll_until
from core.registration.dom import find_first_visible

TRAE_URL = "https://www.trae.ai"
TRAE_PASSPORT_DOMAIN = "ug-normal.trae.ai"
TRAE_API_SG = "https://api-sg-central.trae.ai"
SEND_CODE_PATH = "/passport/web/email/send_code"
SIGNUP_URL = f"{TRAE_URL}/sign-up"
SIGNUP_URL_MARKER = "sign-up"

EMAIL_INPUT_SELECTORS = (
    'input[placeholder="Email"]',
    'input[type="email"]',
    'input[name="email"]',
    'input[placeholder*="email" i]',
)

OTP_INPUT_SELECTORS = (
    'input[placeholder="Verification code"]',
    'input[placeholder*="verification" i]',
    'input[placeholder*="code" i]',
    'input[name="code"]',
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
)

PASSWORD_INPUT_SELECTORS = (
    'input[placeholder="Password"]',
    'input[type="password"]',
    'input[name="password"]',
)

SEND_CODE_TEXT = "Send Code"
#: 大小写两种写法站点都出现过
SIGNUP_TEXTS = ("Sign Up", "Sign up")

#: 组件渲染 / 提交后的缓冲
SETTLE_SECONDS = 2.0
CLICK_SETTLE_SECONDS = 0.5

#: 遍历叶子节点点击指定文本；文本由调用方插入，只来自本模块的常量
_CLICK_LEAF_JS = """
(wanted) => {
    for (const el of document.querySelectorAll('*')) {
        if (el.children.length === 0 && wanted.includes(el.textContent.trim())) {
            el.click();
            return true;
        }
    }
    return false;
}
"""


def random_password() -> str:
    """按 Trae 的密码策略造一个：2 大写 + 3 数字 + 5 小写 + 符号。

    和协议路径的 ``core._rand_password``（14 位纯随机）不是一回事——那个不保证
    含大写和符号，浏览器表单的前端校验会当场拒掉。
    """
    return (
        "".join(random.choices(string.ascii_uppercase, k=2))
        + "".join(random.choices(string.digits, k=3))
        + "".join(random.choices(string.ascii_lowercase, k=5))
        + "!"
    )


def send_code_rejection(responses: list) -> str | None:
    """从捕获的 send_code 响应里取出被拒原因，成功或无法解析时返回 None。"""
    for resp in reversed(responses):
        try:
            body = resp.json()
        except Exception:
            continue
        if not isinstance(body, dict):
            continue
        if body.get("message") == "success":
            return None
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        return str(
            data.get("description")
            or data.get("error_code")
            or body.get("message")
            or "未知错误"
        )
    return None


def capture_send_code_responses(page) -> list:
    """挂上 response 监听并返回收集用的列表。

    必须在 ``goto`` 之前挂：发码请求可能在首屏就打出去。同一个 page 只能挂一次，
    重复挂会把同一条响应记两遍，调用方（步骤）用 handles 保证幂等。
    """
    responses: list = []
    page.on(
        "response",
        lambda resp: responses.append(resp) if SEND_CODE_PATH in resp.url else None,
    )
    return responses


def open_signup(page, *, log=print, should_stop=None) -> None:
    log("打开 Trae 注册页")
    page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=30000)
    cancellable_sleep(SETTLE_SECONDS, should_stop=should_stop)


def wait_for_visible_input(page, selectors, *, timeout: float, interval: float = 0.5, should_stop=None):
    """等第一个**可见**的候选输入框；Trae 的表单是分步渲染的，隐藏副本常年在 DOM 里。"""
    return poll_until(
        lambda: find_first_visible(page, selectors)[0],
        timeout=timeout,
        interval=interval,
        should_stop=should_stop,
    )


def click_and_fill(element, value: str, *, should_stop=None) -> None:
    """先点再填：受控组件不先聚焦的话 fill 出来的值不会进 React state。"""
    element.click()
    element.fill(value)
    cancellable_sleep(CLICK_SETTLE_SECONDS, should_stop=should_stop)


def click_leaf_text(page, texts, *, log=print, timeout: float, should_stop=None) -> bool:
    """按可见文本点按钮，三层兜底。

    1. Playwright 的 ``text="..."`` 取最后一个匹配（表单底部那个才是按钮，
       页面顶部常有同名的说明文字）；
    2. 上面那条在按钮被遮挡时会抛，退回 JS 里自己遍历叶子节点；
    3. 两条都没成返回 False，由调用方决定要不要用键盘兜底。
    """
    wanted = (texts,) if isinstance(texts, str) else tuple(texts)

    def attempt():
        for text in wanted:
            try:
                element = page.locator(f'text="{text}"').last
                if element.is_visible():
                    element.click()
                    log(f"已点击 {text}")
                    return True
            except Exception:
                pass
        try:
            if page.evaluate(_CLICK_LEAF_JS, list(wanted)):
                log(f"已点击 {wanted[0]} (JS)")
                return True
        except Exception:
            pass
        return None

    return bool(poll_until(attempt, timeout=timeout, interval=1.0, should_stop=should_stop))


def press_keys(page, *keys, should_stop=None) -> None:
    for key in keys:
        page.keyboard.press(key)
        cancellable_sleep(0.3, should_stop=should_stop)


def is_on_signup_page(page) -> bool:
    url = str(getattr(page, "url", "") or "")
    return SIGNUP_URL_MARKER in url or "trae.ai" not in url


def get_cloudide_token(page, log_fn=print, *, should_stop=None) -> tuple[str, str, str]:
    """注册完成后用浏览器 session 换 Cloud-IDE JWT。

    三步同 ``core.py``：Login 建 IDE session → GetUserToken 拿 JWT →
    CheckLogin 拿 UserId / Region。每一步都吞异常：拿不到 token 由调用方判定，
    中间某一步失败还有 cookie / JWT payload 两层兜底能补出 user_id。
    """
    token = ""
    user_id = ""
    region = ""

    # step4: Trae Login（建立 IDE session）
    try:
        log_fn("调用 Trae Login API...")
        page.evaluate(
            f"""
        async () => {{
            await fetch("https://{TRAE_PASSPORT_DOMAIN}/cloudide/api/v3/trae/Login?type=email", {{
                method: "POST",
                headers: {{"content-type": "application/json"}},
                credentials: "include",
                body: JSON.stringify({{
                    "UtmSource": "", "UtmMedium": "", "UtmCampaign": "",
                    "UtmTerm": "", "UtmContent": "", "BDVID": "",
                    "LoginChannel": "ide_platform"
                }})
            }});
        }}
        """
        )
        cancellable_sleep(1, should_stop=should_stop)
    except Exception as exc:
        log_fn(f"Trae Login 失败: {exc}")

    # step5: GetUserToken → Cloud-IDE JWT
    try:
        log_fn("获取 Cloud-IDE JWT token...")
        result = page.evaluate(
            f"""
        async () => {{
            const r = await fetch("{TRAE_API_SG}/cloudide/api/v3/common/GetUserToken", {{
                method: "POST",
                headers: {{"content-type": "application/json"}},
                credentials: "include",
                body: JSON.stringify({{}})
            }});
            return await r.json();
        }}
        """
        )
        token = (result or {}).get("Result", {}).get("Token", "") or ""
        if token:
            log_fn(f"获取到 Cloud-IDE JWT (长度={len(token)})")
    except Exception as exc:
        log_fn(f"GetUserToken 失败: {exc}")

    # step6: CheckLogin → userId / Region
    if token:
        try:
            result2 = page.evaluate(
                f"""
            async () => {{
                const r = await fetch("https://{TRAE_PASSPORT_DOMAIN}/cloudide/api/v3/trae/CheckLogin", {{
                    method: "POST",
                    headers: {{
                        "content-type": "application/json",
                        "Authorization": "Cloud-IDE-JWT {token}"
                    }},
                    credentials: "include",
                    body: JSON.stringify({{"GetAIPayHost": true, "GetNickNameEditStatus": true}})
                }});
                return await r.json();
            }}
            """
            )
            res = (result2 or {}).get("Result", {})
            user_id = str(res.get("UserId", "") or res.get("userId", ""))
            region = res.get("Region", "") or ""
        except Exception as exc:
            log_fn(f"CheckLogin 失败: {exc}")

    # 兜底：从 Cookie 提取 user_id
    if not user_id:
        try:
            cookies = {c["name"]: c["value"] for c in page.context.cookies()}
            user_id = cookies.get("user_id", cookies.get("userId", ""))
        except Exception:
            pass

    # 终极兜底：从 JWT payload 解出 id
    if not user_id and token:
        user_id = _user_id_from_jwt(token)

    return token, user_id, region


def _user_id_from_jwt(token: str) -> str:
    import base64
    import json

    try:
        payload = token.split(".")[1]
        payload += "==" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return str(data.get("data", {}).get("id", ""))
    except Exception:
        return ""
