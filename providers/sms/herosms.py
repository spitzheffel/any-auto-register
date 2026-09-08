"""HeroSMS 接码实现（https://hero-sms.com/stubs/handler_api.php）。

带重发、短信事件去重和短寿命号码复用；号码复用池在 ``providers.sms.number_pool``。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Callable

import requests

from core.base_sms import _SMS_VERIFY_WINDOW_LOCK, BaseSmsProvider, SmsActivation
from core.errors import ConfigError, ProviderError, TransientError
from providers.registry import register_provider
from providers.sms.number_pool import HERO_SMS_PHONE_LIFETIME, shared_pool

logger = logging.getLogger(__name__)

HERO_SMS_SERVICES = {
    "chatgpt": "dr",
    "openai": "dr",
    "cursor": "ot",
}
HERO_SMS_DEFAULT_SERVICE = "ot"
HERO_SMS_DEFAULT_COUNTRY = "187"

# 号码复用缓存/黑名单/落盘都在 providers.sms.number_pool 里（厂商实现细节）；
# 验证窗口锁是跨厂商的通用设施，定义在 core.base_sms 里。


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first_int(settings: dict, keys: tuple[str, ...], default: int) -> int:
    """按顺序取第一个填了值的键；显式的 0 也是值，不能被 ``or`` 链当成没填。"""
    for key in keys:
        value = settings.get(key)
        if value in (None, ""):
            continue
        return _safe_int(value, default)
    return default


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _normalize_hero_proxy(proxy: str | None) -> str | None:
    proxy = str(proxy or "").strip()
    if not proxy or proxy.startswith("singbox://"):
        return None
    return proxy


def _parse_hero_status_text(text: str) -> dict:
    text = str(text or "").strip()
    if text == "STATUS_WAIT_CODE":
        return {"status": "wait_code"}
    if text.startswith("STATUS_WAIT_RETRY"):
        return {"status": "wait_retry", "raw": text}
    if text == "STATUS_WAIT_RESEND":
        return {"status": "wait_resend"}
    if text.startswith("STATUS_OK:"):
        return {"status": "ok", "code": text.split(":", 1)[1]}
    if text == "STATUS_CANCEL":
        return {"status": "cancel"}
    return {"status": "unknown", "raw": text}


def _canonical_sms_event_fields(event_fields: dict | None) -> dict:
    event_fields = event_fields or {}
    canonical: dict[str, str] = {}
    channel = str(event_fields.get("channel") or "").strip()
    if channel:
        canonical["channel"] = channel
    sms_time = (
        event_fields.get("dateTime")
        or event_fields.get("date")
        or event_fields.get("smsDate")
        or event_fields.get("smsTime")
        or ""
    )
    if sms_time:
        canonical["time"] = str(sms_time)
    text = event_fields.get("text") or event_fields.get("smsText")
    if text:
        canonical["text"] = str(text)
    if channel == "call":
        for key in ("from", "url"):
            if event_fields.get(key):
                canonical[key] = str(event_fields[key])
    if not sms_time:
        for key in ("repeated", "activationStatus", "verificationType"):
            if event_fields.get(key) is not None:
                canonical[key] = str(event_fields[key])
    return canonical


def _has_real_sms_time(event_fields: dict | None) -> bool:
    raw_time = (
        (event_fields or {}).get("dateTime")
        or (event_fields or {}).get("date")
        or (event_fields or {}).get("smsDate")
        or (event_fields or {}).get("smsTime")
        or ""
    )
    raw_time = str(raw_time).strip()
    return bool(raw_time and raw_time not in {"0", "0000-00-00 00:00:00", "0000-00-00T00:00:00"})


def _sms_event_key(activation_id: str, code: str, event_fields: dict | None) -> str:
    identity = {"activation_id": str(activation_id), "code": str(code)}
    identity.update(_canonical_sms_event_fields(event_fields))
    raw = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _make_sms_candidate(activation_id: str, source: str, code, event_fields: dict | None = None) -> dict | None:
    code = str(code or "").strip()
    if not code or code in {"null", "None"}:
        return None
    canonical = _canonical_sms_event_fields(event_fields)
    sms_key = _sms_event_key(activation_id, code, event_fields) if event_fields else ""
    return {
        "status": "ok",
        "code": code,
        "source": source,
        "sms_key": sms_key,
        "sms_time": canonical.get("time", ""),
        "sms_text": canonical.get("text", ""),
        "allow_same_code": _has_real_sms_time(event_fields),
    }


def _candidate_is_attempted(candidate: dict, used_codes: set, attempted_sms_keys: set) -> bool:
    sms_key = str(candidate.get("sms_key") or "")
    code = str(candidate.get("code") or "")
    if sms_key and sms_key in attempted_sms_keys:
        return True
    return bool(code in used_codes and not candidate.get("allow_same_code"))


@register_provider("sms", "herosms_api")
class HeroSmsProvider(BaseSmsProvider):
    """HeroSMS provider with resend, SMS event dedupe, and short-lived phone reuse."""

    BASE_URL = "https://hero-sms.com/stubs/handler_api.php"
    PROVIDER_LABEL = "HeroSMS"
    CONFIG_PREFIX = "herosms"
    auto_report_success_on_code = False
    # 号码复用池是进程级的，两个尝试同时租号会互相抢号
    requires_exclusive_verification_window = True

    @classmethod
    def from_config(cls, config: dict) -> 'HeroSmsProvider':
        prefix = cls.CONFIG_PREFIX
        api_key = str(config.get(f"{prefix}_api_key", "") or "").strip()
        if not api_key:
            raise ConfigError(f"{cls.PROVIDER_LABEL} 未配置 API Key")
        return cls(
            api_key=api_key,
            default_service=str(config.get(f"{prefix}_default_service") or HERO_SMS_DEFAULT_SERVICE),
            default_country=str(
                config.get("sms_country")
                or config.get(f"{prefix}_country")
                or config.get(f"{prefix}_default_country")
                or HERO_SMS_DEFAULT_COUNTRY
            ),
            max_price=_safe_float(config.get(f"{prefix}_max_price"), -1),
            proxy=str(config.get("sms_proxy") or config.get("proxy") or "") or None,
            auto_country=_safe_bool(config.get(f"{prefix}_auto_country"), False),
            auto_country_min_stock=_safe_int(config.get(f"{prefix}_auto_country_min_stock"), 20),
            auto_country_max_price=_safe_float(config.get(f"{prefix}_auto_country_max_price"), 0),
            # 默认关闭：OpenAI 风控不允许同号短时间内绑多个账号，而号码只活 20 分钟，
            # 复用只会把窗口耗在注定被拒的号上
            reuse_phone_to_max=_safe_bool(config.get("register_reuse_phone_to_max"), False),
            phone_success_max=max(0, _first_int(config, ("register_phone_extra_max", "register_phone_success_max"), 3)),
        )

    def __init__(
        self,
        api_key: str,
        *,
        default_service: str = HERO_SMS_DEFAULT_SERVICE,
        default_country: str = HERO_SMS_DEFAULT_COUNTRY,
        max_price: float = -1,
        proxy: str | None = None,
        reuse_phone_to_max: bool = True,
        phone_success_max: int = 3,
        auto_country: bool = False,
        auto_country_min_stock: int = 20,
        auto_country_max_price: float = 0,
    ):
        self.api_key = str(api_key or "").strip()
        self.default_service = str(default_service or HERO_SMS_DEFAULT_SERVICE).strip()
        self.default_country = str(default_country or HERO_SMS_DEFAULT_COUNTRY).strip()
        self.max_price = float(max_price or -1)
        self.auto_country = bool(auto_country)
        self.auto_country_min_stock = _safe_int(auto_country_min_stock, 20)
        self.auto_country_max_price = _safe_float(auto_country_max_price, 0)
        self.proxy = _normalize_hero_proxy(proxy)
        self.proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        self.reuse_phone_to_max = bool(reuse_phone_to_max)
        self.phone_success_max = max(0, int(phone_success_max or 0))
        self.openai_resend_callback: Callable[[], None] | None = None
        self.last_code_result: dict | None = None
        self.current_activation: SmsActivation | None = None
        self.pool = shared_pool()

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        payload = dict(params)
        if needs_key:
            payload["api_key"] = self.api_key
        resp = requests.get(self.BASE_URL, params=payload, timeout=timeout, proxies=self.proxies)
        self._raise_for_status(resp, action=str(params.get("action") or "unknown"))
        return resp

    def _raise_for_status(self, resp: requests.Response, *, action: str) -> None:
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            # 渠道侧 HTTP 错误归入 provider 类：任务层据此补投 / 换渠道，而不是当未知失败
            raise ProviderError(
                f"{self.PROVIDER_LABEL} 请求失败: HTTP {resp.status_code} (action={action})"
            ) from None

    def resolve_service_code(self, *, platform: str = "", service: str = "") -> str:
        explicit = str(service or "").strip().lower()
        if explicit:
            return explicit
        platform_name = str(platform or "").strip().lower()
        if platform_name in HERO_SMS_SERVICES:
            return HERO_SMS_SERVICES[platform_name]
        return str(self.default_service or HERO_SMS_DEFAULT_SERVICE).strip().lower() or HERO_SMS_DEFAULT_SERVICE

    def get_balance(self) -> float:
        text = self._request({"action": "getBalance"}).text.strip()
        if text.startswith("ACCESS_BALANCE:"):
            return float(text.split(":", 1)[1])
        raise RuntimeError(f"HeroSMS getBalance failed: {text}")

    def get_services(self, country: str | int | None = None, lang: str = "cn") -> list:
        params = {"action": "getServicesList", "lang": lang}
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params, needs_key=False).json()
        if isinstance(data, dict) and data.get("status") == "success":
            return list(data.get("services") or [])
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 可能是 {"dr": {"name": "OpenAI", ...}, ...} 格式
            result = []
            for key, value in data.items():
                if key in ("status", "message", "error"):
                    continue
                if isinstance(value, dict):
                    if "code" not in value:
                        value["code"] = key
                    result.append(value)
                elif isinstance(value, str):
                    result.append({"code": key, "name": value})
            if result:
                return result
        raise RuntimeError("HeroSMS getServicesList returned unexpected response")

    def get_countries(self) -> list:
        data = self._request({"action": "getCountries"}, needs_key=False).json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 检查是否是错误响应 {"status":0,"message":"No access","data":[]}
            if data.get("status") == 0 or data.get("message") == "No access":
                raise RuntimeError(f"SMS API access denied: {data.get('message', 'unknown')}")
            # HeroSMS 可能返回 {"0": {"id": 0, "eng": "Russia"}, ...} 格式
            result = []
            for key, value in data.items():
                if key in ("status", "message", "data", "error"):
                    continue
                if isinstance(value, dict):
                    if "id" not in value:
                        value["id"] = key
                    result.append(value)
                elif isinstance(value, str):
                    result.append({"id": key, "eng": value, "name": value})
            if result:
                return result
        raise RuntimeError("SMS getCountries returned unexpected response")

    def get_prices(self, service: str | None = None, country: str | int | None = None) -> dict:
        params = {"action": "getPrices"}
        if service:
            params["service"] = service
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params).json()
        if isinstance(data, dict):
            return data
        raise RuntimeError("HeroSMS getPrices returned unexpected response")

    def get_top_countries(self, service: str | None = None) -> list[dict]:
        """获取指定服务按价格排序的国家列表（含价格和库存）。

        优先使用 getTopCountriesByServiceRank API，降级到 getPrices 全量解析。
        返回格式: [{"country": "66", "name": "Thailand", "price": 0.12, "count": 150}, ...]
        """
        service_code = str(service or self.default_service or HERO_SMS_DEFAULT_SERVICE).strip()

        # 策略1: 使用 getTopCountriesByServiceRank（HeroSMS 专用排名接口）
        for action in ("getTopCountriesByServiceRank", "getTopCountriesByService"):
            try:
                data = self._request({"action": action, "service": service_code}).json()
                rows = self._parse_top_countries_response(data)
                if rows:
                    rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
                    return rows
            except Exception:
                continue

        # 策略2: 从 getPrices 全量数据中解析
        try:
            prices = self.get_prices(service=service_code)
            rows = []
            for country_id, services in prices.items():
                if not isinstance(services, dict):
                    continue
                svc_data = services.get(service_code)
                if not isinstance(svc_data, dict):
                    continue
                price = svc_data.get("cost") or svc_data.get("price")
                count = svc_data.get("count") or svc_data.get("qty") or svc_data.get("available")
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None and count > 0:
                    rows.append({"country": str(country_id), "price": price, "count": count})
            rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
            return rows
        except Exception:
            return []

    def _parse_top_countries_response(self, data) -> list[dict]:
        """解析 getTopCountriesByServiceRank 响应。"""
        rows = []
        items = data
        # 可能嵌套在 data/result 键下
        if isinstance(data, dict):
            items = data.get("data") or data.get("result") or data.get("response") or data
        if isinstance(items, dict):
            # {country_id: {price, count, ...}} 格式
            for key, value in items.items():
                if not isinstance(value, dict):
                    continue
                try:
                    country_id = str(int(key))
                except (TypeError, ValueError):
                    continue
                price = value.get("price") or value.get("cost") or value.get("retail_price")
                count = value.get("count") or value.get("qty") or value.get("available") or value.get("stock")
                name = value.get("name") or value.get("countryName") or value.get("country_name") or ""
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": country_id, "name": str(name), "price": price, "count": count})
        elif isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                country_id = item.get("country") or item.get("countryId") or item.get("country_id") or item.get("id")
                if country_id is None:
                    continue
                price = item.get("price") or item.get("cost") or item.get("retail_price") or item.get("retailPrice")
                count = item.get("count") or item.get("qty") or item.get("available") or item.get("stock") or item.get("total")
                name = item.get("name") or item.get("countryName") or item.get("country_name") or item.get("title") or ""
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": str(country_id), "name": str(name), "price": price, "count": count})
        return rows

    def get_best_country(self, service: str | None = None, *, min_stock: int = 20, max_price: float = 0) -> str | None:
        """自动选择最优国家：价格最低且库存充足。

        Args:
            service: 服务代码（默认使用 self.default_service）
            min_stock: 最低库存要求（默认 20）
            max_price: 最高价格限制（0 表示不限）

        Returns:
            最优国家 ID 字符串，或 None（无可用国家）
        """
        # HeroSMS/SMSBower 中已验证对 OpenAI 走 SMS（非 WhatsApp）的国家白名单
        # OpenAI 2025年起对绝大多数国家改用 WhatsApp 验证
        # 目前只有泰国确认走 SMS
        ALLOWED_COUNTRIES = {
            "52",   # Thailand (已验证走SMS)
        }

        service_code = str(service or self.default_service or "")

        try:
            rows = self.get_top_countries(service=service)
        except Exception as exc:
            logger.warning("get_best_country 查询失败: %s", exc)
            return None

        if not rows:
            return None

        def _candidate(row: dict) -> str:
            country_id = str(row.get("country") or "")
            if country_id not in ALLOWED_COUNTRIES:
                return ""
            # 刚刚取号失败过的国家先跳过，别在同一个任务里反复重试
            if self._is_country_blacklisted(service_code, country_id):
                return ""
            if max_price > 0 and (row.get("price") or 0) > max_price:
                return ""
            return country_id

        for row in rows:
            country_id = _candidate(row)
            if country_id and (row.get("count") or 0) >= min_stock:
                return country_id

        # 如果没有满足 min_stock 的，放宽到 count > 0
        for row in rows:
            country_id = _candidate(row)
            if country_id and (row.get("count") or 0) > 0:
                return country_id

        return None

    def _api_key_hash(self) -> str:
        return _hash_secret(self.api_key)

    def _blacklist_country(self, service: str, country: str) -> None:
        self.pool.blacklist_country(self._api_key_hash(), service, country)

    def _is_country_blacklisted(self, service: str, country: str) -> bool:
        return self.pool.is_country_blacklisted(self._api_key_hash(), service, country)

    def peek_reusable_country(self, *, platform: str = "", service: str = "") -> str:
        """返回当前已持有、且可继续复用的号码所在国家（没有则空串）。

        供调用方在挑选国家之前判断：已经握着可复用号码时，再去查"最优国家"
        既浪费时间，还会因为国家不匹配而错过缓存。
        """
        if not self.reuse_phone_to_max:
            return ""
        service_code = self.resolve_service_code(platform=platform, service=service)
        return self.pool.peek_country(
            self._cache_identity(service_code, ""),
            phone_success_max=self.phone_success_max,
        )

    def select_best_country(
        self,
        *,
        platform: str = "",
        service: str = "",
        log_fn: Callable[[str], None] | None = None,
    ) -> str:
        """开启自动选国时挑价格最低且库存充足的国家，否则返回空串。"""
        if not self.auto_country:
            return ""
        log = log_fn if log_fn is not None else (lambda message: None)
        log("正在查询最优国家（价格最低 + 库存充足）...")
        try:
            best = self.get_best_country(
                service=self.resolve_service_code(platform=platform, service=service),
                min_stock=self.auto_country_min_stock,
                max_price=self.auto_country_max_price,
            )
        except Exception as exc:
            log(f"智能国家选择失败({exc})，使用默认配置")
            return ""
        if best:
            log(f"自动选择最优国家: {best}")
            return str(best)
        log("未找到满足条件的国家，使用默认配置")
        return ""

    def _cache_identity(self, service: str, country: str) -> dict:
        return self.pool.identity(self.api_key, service, country)

    def _request_number_raw(self, service: str, country: str) -> dict:
        common = {"service": service, "country": country}

        # 动态获取该国家该服务的实际价格，用实际价格作为 maxPrice
        # 这样能确保拿到物理号码（而不是被分配虚拟号码）
        effective_max_price = self.max_price if self.max_price > 0 else 1
        try:
            prices = self.get_prices(service=service, country=country)
            # getPrices 返回格式: {country_id: {service_code: {cost, count}}}
            country_prices = prices.get(str(country)) or prices.get(country) or {}
            service_prices = country_prices.get(service) or {}
            actual_cost = service_prices.get("cost") or service_prices.get("price")
            if actual_cost is not None:
                actual_cost = float(actual_cost)
                # 用实际价格的 3 倍作为 maxPrice（留足余量），但不超过用户配置的上限
                dynamic_max = round(actual_cost * 3, 4)
                if self.max_price > 0:
                    effective_max_price = min(self.max_price, max(dynamic_max, 0.2))
                else:
                    effective_max_price = max(dynamic_max, 0.2)
        except Exception:
            pass  # 查询失败就用默认值

        common["maxPrice"] = effective_max_price

        v2_error = ""
        try:
            resp = self._request({"action": "getNumberV2", **common})
            try:
                data = resp.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("activationId"):
                return data
            v2_error = resp.text.strip()[:200]
        except Exception as exc:
            v2_error = str(exc)

        # 如果 NO_NUMBERS 且 maxPrice 低于用户配置的上限，提高 maxPrice 重试
        if "NO_NUMBERS" in v2_error and self.max_price > 0 and effective_max_price < self.max_price:
            common["maxPrice"] = self.max_price
            try:
                resp = self._request({"action": "getNumberV2", **common})
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("activationId"):
                    return data
                v2_error = resp.text.strip()[:200]
            except Exception as exc:
                v2_error = str(exc)

        try:
            text = self._request({"action": "getNumber", **common}).text.strip()
            if text.startswith("ACCESS_NUMBER:"):
                parts = text.split(":", 2)
                if len(parts) == 3:
                    return {
                        "activationId": parts[1],
                        "phoneNumber": parts[2],
                        "countryPhoneCode": "",
                        "activationCost": None,
                    }
            raise RuntimeError(text[:200])
        except Exception as exc:
            message = f"{self.PROVIDER_LABEL} 获取号码失败: V2={v2_error}; V1={exc}"
            combined = f"{v2_error} {exc}".upper()
            # NO_NUMBERS 是临时缺货（换国家/等一会儿），NO_BALANCE 是渠道侧问题
            if "NO_BALANCE" in combined:
                raise ProviderError(message) from exc
            if "NO_NUMBERS" in combined:
                raise TransientError(message) from exc
            raise ProviderError(message) from exc

    @staticmethod
    def _format_phone(number_info: dict) -> str:
        raw = str(number_info.get("phoneNumber") or "").strip()
        country_phone_code = str(number_info.get("countryPhoneCode") or "").strip()
        if raw.startswith("+"):
            return raw
        if country_phone_code and raw.startswith(country_phone_code):
            return f"+{raw}"
        if country_phone_code:
            return f"+{country_phone_code}{raw}"
        return f"+{raw}"

    def get_number(
        self,
        *,
        platform: str = "",
        service: str = "",
        country: str = "",
    ) -> SmsActivation:
        service_code = self.resolve_service_code(platform=platform, service=service)
        country_id = str(country or self.default_country or HERO_SMS_DEFAULT_COUNTRY).strip()
        with _SMS_VERIFY_WINDOW_LOCK:
            with self.pool.lock:
                cache = (
                    self.pool.load(
                        self._cache_identity(service_code, country_id),
                        phone_success_max=self.phone_success_max,
                    )
                    if self.reuse_phone_to_max
                    else None
                )
                if cache:
                    activation = SmsActivation(
                        activation_id=str(cache["activation_id"]),
                        phone_number=str(cache["phone_number"]),
                        country=country_id,
                        metadata={"reused": True, "use_count": int(cache.get("use_count") or 0)},
                    )
                    self.current_activation = activation
                    return activation

                try:
                    number_info = self._request_number_raw(service_code, country_id)
                except Exception:
                    self._blacklist_country(service_code, country_id)
                    raise
                activation_id = str(number_info.get("activationId") or "")
                phone = self._format_phone(number_info)
                if not activation_id or not phone.strip("+"):
                    raise RuntimeError("HeroSMS 返回的号码信息不完整")
                cache = {
                    **self._cache_identity(service_code, country_id),
                    "activation_id": activation_id,
                    "phone_number": phone,
                    "acquired_at": time.time(),
                    "use_count": 0,
                    "used_codes": set(),
                    "attempted_sms_keys": set(),
                    "reuse_stopped": False,
                    "stop_reason": "",
                }
                self.pool.save(cache)
                activation = SmsActivation(
                    activation_id=activation_id,
                    phone_number=phone,
                    country=country_id,
                    metadata={"reused": False, "number_info": number_info},
                )
                self.current_activation = activation
                return activation

    def get_status(self, activation_id: str) -> dict:
        return _parse_hero_status_text(self._request({"action": "getStatus", "id": activation_id}).text)

    def get_status_v2(self, activation_id: str) -> dict:
        resp = self._request({"action": "getStatusV2", "id": activation_id})
        text = resp.text.strip()
        try:
            data = resp.json()
        except ValueError:
            return _parse_hero_status_text(text)
        if isinstance(data, str):
            return _parse_hero_status_text(data)
        if not isinstance(data, dict):
            return {"status": "unknown", "raw": data}
        raw_status = data.get("status")
        if isinstance(raw_status, str):
            parsed = _parse_hero_status_text(raw_status)
            if parsed.get("status") != "unknown":
                return parsed
        for channel in ("sms", "call"):
            item = data.get(channel)
            if isinstance(item, dict):
                candidate = _make_sms_candidate(
                    activation_id,
                    f"getStatusV2.{channel}",
                    item.get("code"),
                    {
                        "channel": channel,
                        "dateTime": item.get("dateTime"),
                        "text": item.get("text"),
                        "from": item.get("from"),
                        "url": item.get("url"),
                        "verificationType": data.get("verificationType"),
                    },
                )
                if candidate:
                    return candidate
        return {"status": "wait_code", "raw": data}

    def get_active_activations(self, start: int = 0, limit: int = 20) -> list:
        data = self._request({"action": "getActiveActivations", "start": start, "limit": limit}).json()
        if isinstance(data, dict) and "data" in data:
            return list(data.get("data") or [])
        return []

    def set_status(self, activation_id: str, status: int) -> str:
        return self._request({"action": "setStatus", "id": activation_id, "status": status}).text.strip()

    def cancel_activation(self, activation_id: str) -> bool:
        try:
            resp = self._request({"action": "cancelActivation", "id": activation_id})
            if resp.status_code == 204 or "ACCESS_CANCEL" in resp.text:
                return True
        except Exception:
            pass
        try:
            return "ACCESS_CANCEL" in self.set_status(activation_id, 8)
        except Exception:
            return False

    def finish_activation(self, activation_id: str) -> bool:
        try:
            resp = self._request({"action": "finishActivation", "id": activation_id})
            text = resp.text.strip()
            return resp.status_code in (200, 204) or "ACCESS" in text
        except Exception:
            try:
                return "ACCESS" in self.set_status(activation_id, 6)
            except Exception:
                return False

    def request_resend_sms(self, activation_id: str) -> bool:
        try:
            self.set_status(activation_id, 3)
            return True
        except Exception:
            return False

    def wait_for_code(
        self,
        activation_id: str,
        *,
        timeout: int = 180,
        poll_interval: int = 3,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict | None:
        deadline = time.time() + timeout
        start = time.time()
        last_hero_resend = start
        openai_resent = False
        warned_v2 = False
        while time.time() < deadline:
            if should_stop is not None and should_stop():
                return None
            with self.pool.lock:
                cache = self.pool.snapshot() or {}
                used_codes = set(cache.get("used_codes") or [])
                attempted_sms_keys = set(cache.get("attempted_sms_keys") or [])

            for source in ("v2", "v1", "active"):
                try:
                    candidate = None
                    if source == "v2":
                        result = self.get_status_v2(activation_id)
                        if result.get("status") == "cancel":
                            return None
                        if result.get("status") == "ok":
                            candidate = result
                    elif source == "v1":
                        result = self.get_status(activation_id)
                        if result.get("status") == "cancel":
                            return None
                        if result.get("status") == "ok":
                            candidate = _make_sms_candidate(activation_id, "getStatus", result.get("code"))
                    else:
                        for item in self.get_active_activations():
                            if str(item.get("activationId")) == str(activation_id):
                                candidate = _make_sms_candidate(
                                    activation_id,
                                    "getActiveActivations",
                                    item.get("smsCode"),
                                    {
                                        "channel": "sms",
                                        "smsText": item.get("smsText"),
                                        "activationStatus": item.get("activationStatus"),
                                        "repeated": item.get("repeated"),
                                        "dateTime": item.get("dateTime"),
                                        "date": item.get("date") or item.get("smsDate") or item.get("smsTime"),
                                    },
                                )
                                break
                    if candidate and not _candidate_is_attempted(candidate, used_codes, attempted_sms_keys):
                        return candidate
                except Exception as exc:
                    if source == "v2" and not warned_v2:
                        logger.warning("HeroSMS getStatusV2 failed: %s", exc)
                        warned_v2 = True
                    else:
                        logger.debug("HeroSMS status check failed via %s: %s", source, exc)

            elapsed = time.time() - start
            if not openai_resent and elapsed >= 90 and self.openai_resend_callback:
                try:
                    self.openai_resend_callback()
                except Exception as exc:
                    logger.warning("OpenAI phone resend callback failed: %s", exc)
                self.request_resend_sms(activation_id)
                last_hero_resend = time.time()
                openai_resent = True
            elif time.time() - last_hero_resend >= 30:
                self.request_resend_sms(activation_id)
                last_hero_resend = time.time()

            time.sleep(poll_interval)
        return None

    def get_code(
        self,
        activation_id: str,
        *,
        timeout: int = 120,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        wait_timeout = timeout
        with self.pool.lock:
            cache = self.pool.snapshot() or {}
            if cache and str(cache.get("activation_id")) == str(activation_id):
                remaining = int(HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0)))
                wait_timeout = max(timeout, remaining, 60)
        candidate = self.wait_for_code(activation_id, timeout=wait_timeout, should_stop=should_stop)
        self.last_code_result = candidate
        return str((candidate or {}).get("code") or "")

    def cancel(self, activation_id: str) -> bool:
        try:
            return self.cancel_activation(activation_id)
        finally:
            with self.pool.lock:
                cache = self.pool.snapshot()
                if cache and str(cache.get("activation_id")) == str(activation_id):
                    self.pool.clear()

    def report_success(self, activation_id: str) -> bool:
        should_finish = False
        should_clear_cache = False
        handled_cached_activation = False
        with self.pool.lock:
            cache = self.pool.snapshot()
            if cache and str(cache.get("activation_id")) == str(activation_id):
                handled_cached_activation = True
                cache["use_count"] = int(cache.get("use_count") or 0) + 1
                self._record_last_attempt(cache, failed=False)
                remaining = HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))
                if not self.reuse_phone_to_max:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = "reuse disabled"
                    should_finish = True
                    should_clear_cache = True
                elif self.phone_success_max > 0 and int(cache["use_count"]) >= self.phone_success_max:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = f"success max reached ({self.phone_success_max})"
                    should_finish = True
                elif remaining <= 30:
                    cache["reuse_stopped"] = True
                    cache["stop_reason"] = "phone lifetime nearly expired"
                    should_finish = True
                    should_clear_cache = True
                self.pool.save(cache)
                if should_clear_cache:
                    self.pool.clear()
        if handled_cached_activation:
            if should_finish:
                self.finish_activation(activation_id)
            return True
        return self.finish_activation(activation_id)

    def _record_last_attempt(self, cache: dict, *, failed: bool) -> None:
        self.pool.record_attempt(cache, self.last_code_result, failed=failed)

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        with self.pool.lock:
            cache = self.pool.snapshot()
            if cache and str(cache.get("activation_id")) == str(activation_id):
                self._record_last_attempt(cache, failed=True)
                self.pool.save(cache)
        if self.openai_resend_callback:
            try:
                self.openai_resend_callback()
            except Exception:
                pass
        self.request_resend_sms(activation_id)

    def mark_send_succeeded(self, activation_id: str) -> None:
        try:
            self.set_status(activation_id, 1)
        except Exception:
            pass

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        reason_text = str(reason or "").lower()
        if any(keyword in reason_text for keyword in ("limit", "already", "too many", "exceeded", "maximum", "上限", "已达")):
            self.pool.stop_reuse("phone limit reached")
        else:
            self.pool.stop_reuse(reason or "phone rejected")

    def set_resend_callback(self, callback: Callable[[], None] | None) -> None:
        self.openai_resend_callback = callback

    def get_reuse_info(self) -> dict:
        return self.pool.reuse_info(
            self._cache_identity(self.default_service, self.default_country),
            phone_success_max=self.phone_success_max,
        )

    def has_reusable_capacity(self) -> bool:
        """当前是否握着还能再用一次的号码（供调度器决定是否超发）。"""
        if not self.reuse_phone_to_max:
            return False
        return bool(self.get_reuse_info().get("alive"))

    def oversubscribe_policy(self, settings: dict | None = None) -> dict:
        settings = dict(settings or {})
        return {
            # 号码可在 20 分钟内复用，失败重试几乎不额外花钱，值得补投
            "compensate_failures": True,
            "extra_max": max(0, _first_int(settings, ("register_phone_extra_max", "register_phone_success_max"), self.phone_success_max)),
            "reuse_to_max": bool(self.reuse_phone_to_max),
        }
