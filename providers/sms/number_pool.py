"""HeroSMS / SMSBower 的短寿命号码复用池。

这是该厂商 API 的实现细节（号码只活 20 分钟、进程内单槽、按 sms_key 去重），
不是通用能力：对外只经 ``BaseSmsProvider`` 的通用接口暴露。别的厂商若也支持
复用，自带自己的池实现即可。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

HERO_SMS_PHONE_LIFETIME = 20 * 60

# 取号失败的国家在一段时间内不再被"最优国家"选中，避免同一任务反复踩同一个坑
HERO_SMS_COUNTRY_BLACKLIST_TTL = 10 * 60


def hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _project_data_dir() -> Path:
    root = Path(__file__).resolve().parent.parent.parent
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def default_cache_file() -> Path:
    return _project_data_dir() / ".herosms_phone_cache.json"


class HeroSmsNumberPool:
    """进程内单槽号码缓存 + 国家黑名单，带磁盘持久化。

    单槽语义与身份校验（api_key_hash + service + country）保持历史行为：
    同一时刻只保留一个可复用号码，身份不匹配即视为未命中。
    """

    def __init__(self, cache_file_factory=None):
        self._cache: dict | None = None
        self._cache_lock = threading.Lock()
        self._blacklist: dict[tuple[str, str, str], float] = {}
        self._blacklist_lock = threading.Lock()
        self._cache_file_factory = cache_file_factory or default_cache_file

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------
    @property
    def lock(self) -> threading.Lock:
        """供 provider 在多步操作间保持一致性。"""
        return self._cache_lock

    def cache_file(self) -> Path:
        return self._cache_file_factory()

    def snapshot(self) -> dict | None:
        """当前槽内缓存（不做身份校验，用于按 activation_id 匹配）。"""
        return self._cache

    def set_snapshot(self, cache: dict | None) -> None:
        """仅供测试预置状态。"""
        self._cache = cache

    @staticmethod
    def identity(api_key: str, service: str, country: str) -> dict:
        return {
            "api_key_hash": hash_secret(api_key),
            "service": str(service),
            "country": str(country),
        }

    def load(self, identity: dict, *, phone_success_max: int) -> dict | None:
        """取出可复用的缓存；已过期 / 已停用 / 达上限时返回 None。"""
        if self._cache is not None:
            cache = self._cache
        else:
            path = self.cache_file()
            if not path.exists():
                return None
            try:
                cache = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None

        if any(str(cache.get(key) or "") != str(value) for key, value in identity.items()):
            return None
        elapsed = time.time() - float(cache.get("acquired_at") or 0)
        if elapsed >= HERO_SMS_PHONE_LIFETIME or cache.get("reuse_stopped"):
            self.clear()
            return None
        if phone_success_max > 0 and int(cache.get("use_count") or 0) >= phone_success_max:
            cache["reuse_stopped"] = True
            cache["stop_reason"] = f"success max reached ({phone_success_max})"
            self.save(cache)
            return None
        cache["used_codes"] = set(cache.get("used_codes") or [])
        cache["attempted_sms_keys"] = set(cache.get("attempted_sms_keys") or [])
        self._cache = cache
        return cache

    def save(self, cache: dict | None) -> None:
        self._cache = cache
        path = self.cache_file()
        if cache is None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        serializable = dict(cache)
        serializable["used_codes"] = sorted(serializable.get("used_codes") or [])
        serializable["attempted_sms_keys"] = sorted(serializable.get("attempted_sms_keys") or [])
        serializable.pop("client", None)
        path.write_text(json.dumps(serializable, ensure_ascii=False), encoding="utf-8")

    def clear(self) -> None:
        self.save(None)

    def stop_reuse(self, reason: str) -> None:
        with self._cache_lock:
            cache = self._cache
            if not cache:
                return
            cache["reuse_stopped"] = True
            cache["stop_reason"] = reason
            self.save(cache)

    def peek_country(self, identity: dict, *, phone_success_max: int) -> str:
        """当前持有且仍可复用的号码所在国家（没有则空串）。

        国家不参与匹配：调用方正是想知道该沿用哪个国家。
        """
        with self._cache_lock:
            cache = self._cache
            if not cache or cache.get("reuse_stopped"):
                return ""
            if str(cache.get("api_key_hash") or "") != str(identity.get("api_key_hash") or ""):
                return ""
            if str(cache.get("service") or "") != str(identity.get("service") or ""):
                return ""
            if time.time() - float(cache.get("acquired_at") or 0) >= HERO_SMS_PHONE_LIFETIME:
                return ""
            if phone_success_max > 0 and int(cache.get("use_count") or 0) >= phone_success_max:
                return ""
            return str(cache.get("country") or "")

    def reuse_info(self, identity: dict, *, phone_success_max: int) -> dict:
        with self._cache_lock:
            cache = self._cache or self.load(identity, phone_success_max=phone_success_max) or {}
            if not cache:
                return {"alive": False}
            remaining = max(
                0,
                int(HERO_SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))),
            )
            return {
                "alive": remaining > 0 and not bool(cache.get("reuse_stopped")),
                "phone_number": cache.get("phone_number", ""),
                "use_count": int(cache.get("use_count") or 0),
                "remaining_seconds": remaining,
                "reuse_stopped": bool(cache.get("reuse_stopped")),
                "stop_reason": cache.get("stop_reason", ""),
            }

    def record_attempt(self, cache: dict, candidate: dict | None, *, failed: bool) -> None:
        """把刚用过的验证码 / sms_key 记进缓存，避免复用时重复消费同一条短信。"""
        candidate = candidate or {}
        code = str(candidate.get("code") or "")
        sms_key = str(candidate.get("sms_key") or "")
        used_codes = set(cache.get("used_codes") or [])
        attempted_sms_keys = set(cache.get("attempted_sms_keys") or [])
        if code:
            used_codes.add(code)
        if sms_key:
            attempted_sms_keys.add(sms_key)
        cache["used_codes"] = used_codes
        cache["attempted_sms_keys"] = attempted_sms_keys
        if failed:
            cache["last_failed_reason"] = "invalid otp"

    # ------------------------------------------------------------------
    # 国家黑名单
    # ------------------------------------------------------------------
    def blacklist_country(self, identity_hash: str, service: str, country: str) -> None:
        if not country:
            return
        with self._blacklist_lock:
            self._blacklist[(identity_hash, str(service), str(country))] = (
                time.time() + HERO_SMS_COUNTRY_BLACKLIST_TTL
            )

    def is_country_blacklisted(self, identity_hash: str, service: str, country: str) -> bool:
        key = (identity_hash, str(service), str(country))
        with self._blacklist_lock:
            expires_at = self._blacklist.get(key)
            if expires_at is None:
                return False
            if expires_at <= time.time():
                self._blacklist.pop(key, None)
                return False
            return True

    def reset_blacklist(self) -> None:
        with self._blacklist_lock:
            self._blacklist.clear()


# 进程级共享实例：号码槽与缓存文件都是单份，身份校验在 identity 里做
_shared_pool = HeroSmsNumberPool()


def shared_pool() -> HeroSmsNumberPool:
    return _shared_pool
