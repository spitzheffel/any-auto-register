"""Cursor 注册协议核心实现"""
import json
import os
import random
import re
import string
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Callable

from core.errors import ProviderError, ResourceRejectedError, SiteChangedError, TransientError
from core.phone_verification import OUTCOME_REJECTED, OUTCOME_SUCCESS, OUTCOME_UNUSED

AUTH   = "https://authenticator.cursor.sh"
CURSOR = "https://cursor.com"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/145.0.0.0 Safari/537.36")

TURNSTILE_SITEKEY = "0x4AAAAAAAMNIvC45A4Wjjln"

# ---------------------------------------------------------------------------
# Next.js server action ID 管理
#
# /sign-up 每一步（提交邮箱/密码/OTP）都是 server action 调用：
#   POST /sign-up + next-action: <action_id>
# action_id 会随 Cursor 前端重新部署而变化，旧 id 直接 404 "Server action
# not found"（2026-08-21 断过一次）。策略分三层：
#   1. 磁盘缓存里上次实测可用的 id（data/.cursor_action_ids.json）
#   2. 下面硬编码的历史 id 兜底
#   3. 全部 404 时现场从 sign-up 页面 RSC 数据 + JS chunk 提取新 id 候选，
#      按上下文关键词排序后逐个试（404 换下一个）
# 只有响应被判定为"确实是本步骤的 action"的 id 才会写回缓存；非 404 但内容
# 错配的 id 记为该步骤的失效 id，避免下次再拿它试。
# ---------------------------------------------------------------------------

_KNOWN_ACTIONS = {
    "submit_email":    "d0b05a2a36fbe69091c2f49016138171d5c1e4cd",
    "submit_password": "fef846a39073c935bea71b63308b177b113269b7",
    "magic_code":      "f9e8ae3d58a7cd11cccbcdbf210e6f2a6a2550dd",
}

# 这些步骤会把明文密码 / 一次性 captcha token POST 出去，不能拿通配池里
# 来路不明的 action 去试——通配池是整站所有 server action，命中概率低、泄露面大
_POOL_EXCLUDED_STEPS = frozenset({"submit_password"})

_ACTION_ID_RE = re.compile(r"['\"]([0-9a-f]{40})['\"]")
_CHUNK_URL_RE = re.compile(r"/_next/static/chunks/[^\"'\s\\]+?\.js")


def _default_action_cache_path() -> Path:
    # 项目根下的 data/（与号码缓存、失败现场同一目录），不是 platforms/data/
    return Path(__file__).resolve().parent.parent.parent / "data" / ".cursor_action_ids.json"


def _extract_chunk_urls(flight: str) -> list[str]:
    # flight 数据是 JSON 转义过的，\/ 也要还原
    normalized = str(flight or "").replace("\\/", "/")
    return sorted(set(_CHUNK_URL_RE.findall(normalized)))


def _fetch_signup_flight(session, log) -> str:
    """RSC 方式拉 sign-up 页面数据（HTML 导航会被 CF 质询，RSC 请求不会）。"""
    try:
        r = session.get(f"{AUTH}/sign-up", headers={
            "user-agent": UA,
            "RSC": "1",
            "accept": "text/x-component",
            "referer": f"{AUTH}/",
        }, allow_redirects=False)
    except Exception as exc:
        log(f"[action-id] 拉取 sign-up 页面数据失败: {exc}")
        return ""
    text = str(getattr(r, "text", "") or "")
    status = int(getattr(r, "status_code", 0) or 0)
    if status != 200 or len(text) < 500 or "just a moment" in text[:2000].lower():
        log(f"[action-id] sign-up 页面数据不可用 (HTTP {status}, {len(text)} 字节)，可能被 Cloudflare 拦截")
        return ""
    return text


def _fetch_static_text(session, path_url: str) -> str:
    url = path_url if path_url.startswith("http") else f"{AUTH}{path_url}"
    try:
        r = session.get(url, headers={"user-agent": UA, "accept": "*/*"}, allow_redirects=False)
    except Exception:
        return ""
    if int(getattr(r, "status_code", 0) or 0) != 200:
        return ""
    return str(getattr(r, "text", "") or "")


def _scan_chunk_for_actions(js: str) -> list[tuple[str, str]]:
    """扫描 JS chunk 里的 40 位 hex action id，用邻近代码里的字段名做粗归类。

    归类不保证准确，运行时会靠 404 轮询自动纠错，这里只决定尝试顺序。
    """
    results: list[tuple[str, str]] = []
    for m in _ACTION_ID_RE.finditer(js):
        aid = m.group(1)
        ctx = js[max(0, m.start() - 1200): m.end() + 1200]
        if "captchaToken" in ctx:
            step = "submit_password"
        elif re.search(r'["\']otp["\']', ctx):
            step = "magic_code"
        elif re.search(r'["\']email["\']', ctx) and '"password"' not in ctx:
            step = "submit_email"
        else:
            step = ""
        results.append((aid, step))
    return results


class CursorActionIds:
    """server action id 的缓存与自动提取。cache_path=None 时纯内存（测试用）。"""

    def __init__(self, cache_path: Path | str | None = None):
        self.cache_path = Path(cache_path) if cache_path else None
        self.confirmed: dict[str, str] = {}
        self.candidates: dict[str, list[str]] = {}
        # step -> 在该步骤上返回过非 404 错配响应的 id
        self.rejected: dict[str, list[str]] = {}
        self._load()

    @staticmethod
    def _load_id_lists(raw) -> dict[str, list[str]]:
        if not isinstance(raw, dict):
            return {}
        return {
            k: [str(x) for x in v if x]
            for k, v in raw.items()
            if isinstance(v, list)
        }

    def _load(self):
        if not self.cache_path:
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        confirmed = data.get("confirmed") or {}
        self.confirmed = {k: str(v) for k, v in confirmed.items() if k in _KNOWN_ACTIONS and v}
        self.candidates = self._load_id_lists(data.get("candidates"))
        self.rejected = self._load_id_lists(data.get("rejected"))

    def _save(self):
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({
                "confirmed": self.confirmed,
                "candidates": self.candidates,
                "rejected": self.rejected,
                "updated_at": int(time.time()),
            }, ensure_ascii=False, indent=2)
            # 多个尝试可能同时写：先写临时文件再替换，读到半截 JSON 会让 _load 静默丢掉全部缓存
            tmp_path = self.cache_path.with_name(f"{self.cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, self.cache_path)
        except Exception:
            pass

    def ordered(self, step: str) -> list[str]:
        dead = set(self.rejected.get(step) or ())
        ids: list[str] = []
        for source in (self.confirmed.get(step), _KNOWN_ACTIONS.get(step), *(self.candidates.get(step) or [])):
            if source and source not in ids and source not in dead:
                ids.append(source)
        if step in _POOL_EXCLUDED_STEPS:
            return ids
        # 归类不明的候选放最后，靠轮询兜底
        for extra in self.candidates.get("*") or []:
            if extra not in ids and extra not in dead:
                ids.append(extra)
        return ids

    def record(self, step: str, action_id: str):
        if not action_id:
            return
        dead = self.rejected.get(step) or []
        if self.confirmed.get(step) == action_id and action_id not in dead:
            return
        self.confirmed[step] = action_id
        if action_id in dead:
            self.rejected[step] = [x for x in dead if x != action_id]
        self._save()

    def reject(self, step: str, action_id: str):
        """该 id 存在但不是 step 要的 action：以后不再拿它试这一步。

        若它正是当前 confirmed，说明缓存已被早先的错误判定污染，一并清掉。
        """
        if not action_id:
            return
        dead = self.rejected.setdefault(step, [])
        changed = False
        if action_id not in dead:
            dead.append(action_id)
            changed = True
        if self.confirmed.get(step) == action_id:
            del self.confirmed[step]
            changed = True
        if changed:
            self._save()

    def discover(self, session, log) -> bool:
        """现场提取 action id 候选：RSC 数据 → JS chunk → 40 位 hex。"""
        flight = _fetch_signup_flight(session, log)
        if not flight:
            return False
        chunks = _extract_chunk_urls(flight)
        if not chunks:
            log("[action-id] sign-up 页面数据中未找到 JS chunk 引用，无法自动提取")
            return False
        per_step: dict[str, list[str]] = {}
        pool: list[str] = []
        for chunk_url in chunks[:24]:
            js = _fetch_static_text(session, chunk_url)
            if not js:
                continue
            for aid, step_key in _scan_chunk_for_actions(js):
                if aid in pool:
                    continue
                pool.append(aid)
                if step_key:
                    per_step.setdefault(step_key, []).append(aid)
        if not pool:
            log(f"[action-id] 已扫描 {len(chunks)} 个 chunk，未发现 action id 候选")
            return False
        # 新候选里不存在的旧 confirmed 已经过期，清掉
        valid = set(pool) | {v for vs in per_step.values() for v in vs}
        self.confirmed = {k: v for k, v in self.confirmed.items() if v in valid}
        self.candidates = {k: list(v) for k, v in per_step.items() if v}
        self.candidates["*"] = pool
        # 失效记录是对上一版部署的判断。重新提取后全部重置：正确 id 若因一次
        # 临时故障（429 / 5xx）被误标，只有这里能让它翻身，否则该步骤会永久瘫掉
        self.rejected = {}
        self._save()
        log("[action-id] 已提取 {} 个 action id 候选: {}".format(
            len(pool),
            ", ".join(f"{k}×{len(v)}" for k, v in per_step.items()) or "均为未归类",
        ))
        return True


_ACTION_FAILURE_HINTS = (
    "invalid captcha",
    "captcha failed",
    "failed captcha",
    "unable to verify",
    "are you human",
    "too many requests",
    "rate limit",
    "access denied",
    "unauthorized",
    "forbidden",
    "user_already_exists",
    "email already",
    "invalid email",
    "account exists",
    "something went wrong",
)


def _rand_password(n=16):
    chars = string.ascii_letters + string.digits + "!@#$"
    return "".join(random.choices(chars, k=n))


def _boundary():
    return "----WebKitFormBoundary" + "".join(
        random.choices(string.ascii_letters + string.digits, k=16))


def _multipart(fields: dict, boundary: str) -> bytes:
    parts = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode()


def _header_get(resp, name: str) -> str:
    headers = getattr(resp, "headers", None) or {}
    getter = getattr(headers, "get", None)
    if callable(getter):
        for key in (name, name.lower(), name.title()):
            value = getter(key)
            if value:
                return str(value)
    items = getattr(headers, "items", None)
    if callable(items):
        target = name.lower()
        for key, value in items():
            if str(key).lower() == target and value:
                return str(value)
    return ""


def action_redirect(resp) -> str:
    return (_header_get(resp, "x-action-redirect") or _header_get(resp, "location")).strip()


def _compact_text(text: str, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    return compact[:limit]


def response_preview(resp, limit: int = 240) -> str:
    status = getattr(resp, "status_code", "?")
    loc = action_redirect(resp)
    body = _compact_text(getattr(resp, "text", "") or "", limit)
    parts = [f"HTTP {status}"]
    if loc:
        parts.append(f"redirect={loc[:160]}")
    if body:
        parts.append(f"body={body}")
    return " | ".join(parts)


def _quoted_error_values(text: str) -> list[str]:
    values = []
    for match in re.finditer(
        r'"(?:error(?:_description|_message|Message)?|message|digest|code)"\s*:\s*"((?:\\.|[^"\\])*)"',
        text or "",
        re.I,
    ):
        raw = match.group(1).replace('\\"', '"')
        if raw and raw.lower() not in {"ok", "success", "none", "null"}:
            values.append(raw)
    return values


def find_action_failure(resp, *, stuck_on: tuple[str, ...] = ()) -> str:
    """从 Next.js server action 响应里挑失败信号。没有把握时返回空，避免误杀成功路径。"""
    status = int(getattr(resp, "status_code", 0) or 0)
    text = str(getattr(resp, "text", "") or "")
    loc = action_redirect(resp)
    if status >= 400:
        return response_preview(resp)
    blob = f"{loc}\n{text}".lower()
    for hint in _ACTION_FAILURE_HINTS:
        if hint in blob:
            return f"检测到失败信号 `{hint}`: {response_preview(resp)}"
    for value in _quoted_error_values(text):
        lowered = value.lower()
        if "error" in lowered or any(hint in lowered for hint in _ACTION_FAILURE_HINTS):
            return f"{value}: {response_preview(resp)}"
    if loc and stuck_on:
        loc_l = loc.lower()
        if any(marker.lower() in loc_l for marker in stuck_on):
            return f"仍停留在 {stuck_on[0]}: {response_preview(resp)}"
    return ""


ACTION_NOT_FOUND = "not_found"
ACTION_MISMATCH = "mismatch"
ACTION_REJECTED = "rejected"
ACTION_ACCEPTED = "accepted"


def classify_action_response(resp, *, stuck_on: tuple[str, ...] = (), trusted: bool = False) -> tuple[str, str]:
    """判定一次 server action 提交落在哪种情形，返回 (类别, 失败详情)。

    404 是 Next.js 对不存在的 action id 的固定回应。非 404 但带失败信号时要分两种：
    - 该 id 此前已被确认是本步骤的 action（trusted）且不是 5xx：失败是业务拒绝
      （邮箱已存在、验证码错、限流），id 没问题，不能因此把它标失效、更不能拿凭据去试别的 id；
    - 该 id 只是候选，或已确认的 id 返回 5xx（更像被历史误判污染的缓存）：当作错配换下一个。
    """
    status = int(getattr(resp, "status_code", 0) or 0)
    if status == 404:
        return ACTION_NOT_FOUND, ""
    failure = find_action_failure(resp, stuck_on=stuck_on)
    if not failure:
        return ACTION_ACCEPTED, ""
    if trusted and status < 500:
        return ACTION_REJECTED, failure
    return ACTION_MISMATCH, failure


class CursorRegister:
    def __init__(self, proxy: str = None, log_fn: Callable = print, action_cache_path=None):
        from curl_cffi import requests as curl_req
        self.log = log_fn
        self.s = curl_req.Session(impersonate="safari17_0")
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self._actions = CursorActionIds(action_cache_path or _default_action_cache_path())
        # 最近一次 server action 的重定向目标（含 x-action-redirect），
        # 用于识别流程是否被重定向到 radar-challenge 手机验证
        self.last_action_redirect = ""

    @property
    def actions(self) -> CursorActionIds:
        # 兼容测试里用 object.__new__ 构造、不走 __init__ 的情况
        inst = getattr(self, "_actions", None)
        if inst is None:
            inst = CursorActionIds(_default_action_cache_path())
            self._actions = inst
        return inst

    def _base_headers(self, next_action, referer, boundary=None):
        ct = f"multipart/form-data; boundary={boundary}" if boundary else "application/x-www-form-urlencoded"
        return {
            "user-agent": UA,
            "accept": "text/x-component",
            "content-type": ct,
            "origin": AUTH,
            "referer": referer,
            "next-action": next_action,
            "next-router-state-tree": "%5B%22%22%2C%7B%22children%22%3A%5B%22(main)%22%2C%7B%22children%22%3A%5B%22(root)%22%2C%7B%22children%22%3A%5B%22(sign-in)%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%5D%7D%5D%7D%5D%7D%5D%7D%5D",
        }

    def _resolve_action(self, step_key: str, referer, fields, *, step: str, stuck_on: tuple[str, ...] = ()):
        """按候选顺序提交 server action，直到某个候选的响应被判定为本步骤的 action。

        404 与内容错配都换下一个候选；只有判定命中的 id 才 record()。否则站点重新部署后，
        池里第一个碰巧不 404 的错误 action 会被当成正解缓存，之后 discover() 再无机会触发。
        候选耗尽 → discover() 一次再轮询一轮；仍失败抛 SiteChangedError 让任务层停批，
        而不是每次尝试都把凭据喷给一遍错误 action。
        """
        tried: list[str] = []
        last_failure = ""
        discovered = False
        for attempt in (1, 2):
            for action_id in self.actions.ordered(step_key):
                if action_id in tried:
                    continue
                tried.append(action_id)
                bd = _boundary()
                resp = self.s.post(
                    f"{AUTH}/sign-up",
                    headers=self._base_headers(action_id, referer, boundary=bd),
                    data=_multipart(fields, bd),
                    allow_redirects=False,
                )
                verdict, detail = classify_action_response(
                    resp,
                    stuck_on=stuck_on,
                    trusted=(self.actions.confirmed.get(step_key) == action_id),
                )
                if verdict == ACTION_ACCEPTED:
                    self.actions.record(step_key, action_id)
                    return resp
                if verdict == ACTION_NOT_FOUND:
                    self.log(f"{step}: action id {action_id[:12]}… 已失效 (404 Server action not found)，尝试下一个候选")
                    continue
                if verdict == ACTION_REJECTED:
                    # 已确认的 action 拒绝了这次提交：是业务失败，不是 id 问题，按普通失败让任务层重试
                    raise RuntimeError(f"{step} 失败: {detail}")
                last_failure = detail
                self.actions.reject(step_key, action_id)
                self.log(f"{step}: action id {action_id[:12]}… 响应不像本步骤的 action，标记失效并尝试下一个候选: {detail}")
            if attempt == 1:
                self.log(f"{step}: 已知 action id 全部不可用，Cursor 前端应已重新部署，尝试自动提取新 id...")
                discovered = self.actions.discover(self.s, self.log)
                if not discovered:
                    break
        advice = "Cursor 注册流程可能已变更，请人工核对 action id 或改用浏览器模式注册。"
        if last_failure:
            raise SiteChangedError(
                f"{step} 失败，Cursor 未接受本次提交：已尝试 {len(tried)} 个 action id 候选均不匹配"
                f"（最后一次: {last_failure}）。{advice}"
            )
        detail = "提取到的新 id 也全部 404。" if discovered else "且自动提取新 id 未成功。"
        raise SiteChangedError(
            f"{step} 失败：所有 action id 均返回 404（Server action not found 或被 Cloudflare 拦截），"
            f"{detail}{advice}"
        )

    def _post_action(self, step_key: str, referer, fields, *, step: str, stuck_on: tuple[str, ...] = ()):
        resp = self._resolve_action(step_key, referer, fields, step=step, stuck_on=stuck_on)
        self.log(f"{step} 响应: {response_preview(resp)}")
        redirect = action_redirect(resp)
        if redirect:
            self.last_action_redirect = redirect
        return resp

    def step1_get_session(self):
        nonce = str(uuid.uuid4())
        state = {"returnTo": "https://cursor.com/dashboard", "nonce": nonce}
        state_encoded = urllib.parse.quote(urllib.parse.quote(json.dumps(state)))
        url = f"{AUTH}/?state={state_encoded}"
        self.s.get(url, headers={"user-agent": UA, "accept": "text/html"}, allow_redirects=True)
        state_cookie_name = None
        for cookie in self.s.cookies.jar:
            if 'state-' in cookie.name:
                state_cookie_name = cookie.name
                break
        return state_encoded, state_cookie_name

    def step2_submit_email(self, email, state_encoded):
        referer = f"{AUTH}/sign-up?state={state_encoded}"
        self._post_action(
            "submit_email",
            referer,
            {"1_state": state_encoded, "email": email},
            step="Step2 提交邮箱",
            stuck_on=("/sign-in",),
        )

    def step3_submit_password(self, password, email, state_encoded, captcha_solver=None):
        referer = f"{AUTH}/sign-up?state={state_encoded}"
        captcha_token = ""
        if captcha_solver:
            self.log("获取 Turnstile token...")
            # token 跟页面 URL 绑定，要用实际报名页，不能只用站点根路径
            captcha_token = str(captcha_solver.solve_turnstile(referer, TURNSTILE_SITEKEY) or "").strip()
            if not captcha_token:
                raise RuntimeError("Turnstile token 为空，无法提交密码，Cursor 不会发 OTP")
            self.log(f"Turnstile token 已获取 ({len(captcha_token)} 字符)")
        self._post_action(
            "submit_password",
            referer,
            {
                "1_state": state_encoded,
                "email": email,
                "password": password,
                "captchaToken": captcha_token,
            },
            step="Step3 提交密码",
            stuck_on=("/sign-up/password",),
        )

    def step4_submit_otp(self, otp, email, state_encoded):
        referer = f"{AUTH}/sign-up?state={state_encoded}"
        resp = self._resolve_action(
            "magic_code",
            referer,
            {"1_state": state_encoded, "email": email, "otp": otp},
            step="Step4 提交 OTP",
        )
        self.log(f"Step4 提交 OTP 响应: {response_preview(resp)}")
        return self.extract_auth_code(resp)

    def extract_auth_code(self, resp) -> str:
        """从 server action 响应的重定向里提取 OAuth code。"""
        loc = action_redirect(resp) if resp is not None else ""
        if loc:
            self.last_action_redirect = loc
        m = re.search(r'code=([\w-]+)', loc or "")
        return m.group(1) if m else ""

    def step5_get_token(self, auth_code, state_encoded):
        url = f"{CURSOR}/api/auth/callback?code={auth_code}&state={state_encoded}"
        self.s.get(url, headers={"user-agent": UA, "accept": "text/html"}, allow_redirects=False)
        for cookie in self.s.cookies.jar:
            if cookie.name == "WorkosCursorSessionToken":
                return urllib.parse.unquote(cookie.value)
        self.s.get(url, headers={"user-agent": UA}, allow_redirects=True)
        for cookie in self.s.cookies.jar:
            if cookie.name == "WorkosCursorSessionToken":
                return urllib.parse.unquote(cookie.value)
        return ""

    # ------------------------------------------------------------------
    # radar-challenge 手机号验证（协议模式接码）
    #
    # 端点与字段格式来自抓包报告 refs/report-2026-08-30-grok-cursor-auto.md：
    #   POST /radar-challenge/send   multipart: _1_country_code, _1_phone_number,
    #                                 _1_pending_authentication_token
    #   POST /radar-challenge/verify multipart: _1_code, _1_verification_id
    # ------------------------------------------------------------------

    def _radar_challenge_headers(self, boundary: str) -> dict:
        return {
            "user-agent": UA,
            "accept": "text/x-component",
            "content-type": f"multipart/form-data; boundary={boundary}",
            "origin": AUTH,
            "referer": f"{AUTH}/radar-challenge",
        }

    def run_radar_challenge(
        self,
        phone,
        redirect_url: str = "",
        *,
        should_stop: Callable[[], bool] | None = None,
    ):
        """走协议通道完成手机号短信验证，返回 verify 的响应。

        should_stop 透传给租号与等码：HeroSMS 等码最长 20 分钟，不传的话任务取消对协议路径无效。
        """
        pending_token = _extract_challenge_token(redirect_url)
        lease = phone.acquire(should_stop=should_stop)
        # 所有退出路径都经由 finally 里这唯一一次 release：成功 → success，号码被站点拒绝 →
        # rejected，其余 → unused。收到码之后的失败分支必须先 report_code_failed 清掉
        # lease 上"等待外部确认成功"的状态，否则 release/close 会把未验证的号码当成功上报
        outcome = OUTCOME_UNUSED
        try:
            phone_number = str(lease.phone_number or "").strip()
            if not phone_number:
                raise ProviderError("接码服务未返回手机号，无法通过手机验证")
            country_code, national_number = _split_phone_number(phone_number)
            if not country_code:
                raise ProviderError(f"无法识别手机号国家区号: {phone_number}")

            self.log(f"radar-challenge: 向 {country_code} {national_number[:4]}**** 发送短信验证码")
            send_fields = {
                "_1_country_code": country_code,
                "_1_phone_number": national_number,
            }
            if pending_token:
                send_fields["_1_pending_authentication_token"] = pending_token
            bd = _boundary()
            send_resp = self.s.post(
                f"{AUTH}/radar-challenge/send",
                headers=self._radar_challenge_headers(bd),
                data=_multipart(send_fields, bd),
                allow_redirects=False,
            )
            self.log(f"radar-challenge/send 响应: {response_preview(send_resp)}")
            send_status = int(getattr(send_resp, "status_code", 0) or 0)
            if send_status >= 400:
                lease.report_send_failed(f"HTTP {send_status}")
                outcome = OUTCOME_REJECTED
                # 站点不收这个号：按"资源被拒"分类，任务层换号补投
                raise ResourceRejectedError(
                    f"发送短信验证码失败 (HTTP {send_status}): {response_preview(send_resp)}"
                )
            lease.report_send_succeeded()
            verification_id = _extract_verification_id(send_resp)
            if verification_id:
                self.log(f"radar-challenge: verification_id={verification_id[:12]}…")

            code = str(lease.wait_code(should_stop=should_stop) or "").strip()
            if not code:
                lease.report_code_failed("等待短信验证码超时")
                raise TransientError("未收到短信验证码，无法通过手机验证")
            verify_fields = {"_1_code": code}
            if verification_id:
                verify_fields["_1_verification_id"] = verification_id
            bd2 = _boundary()
            try:
                verify_resp = self.s.post(
                    f"{AUTH}/radar-challenge/verify",
                    headers=self._radar_challenge_headers(bd2),
                    data=_multipart(verify_fields, bd2),
                    allow_redirects=False,
                )
            except BaseException as exc:
                lease.report_code_failed(f"verify 请求失败: {exc}")
                raise
            self.log(f"radar-challenge/verify 响应: {response_preview(verify_resp)}")
            verify_failure = _radar_verify_failure(verify_resp)
            if verify_failure:
                lease.report_code_failed(verify_failure)
                raise TransientError(f"短信验证码校验失败: {verify_failure}")
            outcome = OUTCOME_SUCCESS
            return verify_resp
        finally:
            lease.release(outcome)

    def is_radar_challenge_redirect(self, redirect_url: str = "") -> bool:
        return "radar-challenge" in str(redirect_url or "").lower()


# 常用国家区号（E.164），把接码服务给的完整号码拆成 国家码 + 本地号
_COUNTRY_CODES = frozenset("""
1 7 20 27 30 31 32 33 34 36 39 40 41 43 44 45 46 47 48 49 51 52 53 54 55 56 57 58
60 61 62 63 64 65 66 81 82 84 86 90 91 92 93 94 95 98
211 212 213 216 218 220 221 222 223 224 225 226 227 228 229 230 231 232 233 234
235 236 237 238 239 240 241 242 243 244 245 246 248 249 250 251 252 253 254 255
256 257 258 260 261 262 263 264 265 266 267 268 269 290 291 297 298 299
350 351 352 353 354 355 356 357 358 359 370 371 372 373 374 375 376 377 378 380
381 382 383 385 386 387 389 420 421 423
500 501 502 503 504 505 506 507 508 509
590 591 592 593 594 595 596 597 598 599
670 672 673 674 675 676 677 678 679 680 681 682 683 685 686 687 688 689 690 691 692
850 852 853 855 856 870 880 886
960 961 962 963 964 965 966 967 968 970 971 972 973 974 975 976 977
992 993 994 995 996 998
""".split())


def _split_phone_number(number: str) -> tuple[str, str]:
    """'+441510496xxx' -> ('+44', '1510496xxx')，长前缀优先。"""
    digits = re.sub(r"\D", "", str(number or ""))
    for width in (3, 2, 1):
        if len(digits) > width and digits[:width] in _COUNTRY_CODES:
            return f"+{digits[:width]}", digits[width:]
    return "", digits


def _extract_challenge_token(redirect_url: str) -> str:
    for name in ("pending_authentication_token", "pendingAuthenticationToken", "token"):
        m = re.search(rf"[?&]{name}=([^&]+)", redirect_url or "")
        if m:
            return urllib.parse.unquote(m.group(1))
    return ""


# 通用失败提示之外，verify 接口拒绝验证码时常见的几种说法
_RADAR_CODE_FAILURE_HINTS = (
    "invalid code",
    "incorrect code",
    "invalid verification code",
    "code is invalid",
    "code has expired",
    "code expired",
    "wrong code",
)


def _radar_verify_failure(resp) -> str:
    """radar-challenge/verify 响应里的失败信号；没有把握时返回空，避免误杀成功路径。"""
    failure = find_action_failure(resp)
    if failure:
        return failure
    blob = f"{action_redirect(resp)}\n{getattr(resp, 'text', '') or ''}".lower()
    for hint in _RADAR_CODE_FAILURE_HINTS:
        if hint in blob:
            return f"检测到验证码错误信号 `{hint}`: {response_preview(resp)}"
    return ""


def _extract_verification_id(resp) -> str:
    text = str(getattr(resp, "text", "") or "")
    m = re.search(r"(vrf_[A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if isinstance(data, dict):
        for key in ("verification_id", "verificationId", "id"):
            value = data.get(key)
            if value:
                return str(value)
    for header in ("x-action-redirect", "location"):
        m = re.search(r"(vrf_[A-Za-z0-9_-]+)", _header_get(resp, header))
        if m:
            return m.group(1)
    return ""
