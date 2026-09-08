"""SMS-Activate 接码实现（https://sms-activate.guru）。"""
from __future__ import annotations

import time
from typing import Callable

import requests

from core.base_sms import BaseSmsProvider, SmsActivation
from core.errors import ConfigError, ProviderError, TransientError
from providers.registry import register_provider

SMS_ACTIVATE_SERVICES = {
    "cursor": "ot",
    "chatgpt": "dr",
    "openai": "dr",
    "google": "go",
    "microsoft": "mg",
    "default": "ot",
}

SMS_ACTIVATE_COUNTRIES = {
    "ru": "0",
    "us": "187",
    "uk": "16",
    "in": "22",
    "id": "6",
    "ph": "4",
    "th": "52",
    "br": "73",
    "default": "0",
}


def _resolve_sms_activate_country_id(country: str, default_country: str) -> str:
    raw = str(country or default_country or "").strip().lower()
    if not raw:
        raw = "default"
    if raw.isdigit():
        return raw
    return SMS_ACTIVATE_COUNTRIES.get(raw, SMS_ACTIVATE_COUNTRIES["default"])


@register_provider("sms", "sms_activate_api")
class SmsActivateProvider(BaseSmsProvider):
    """SMS-Activate (sms-activate.guru) provider."""

    BASE_URL = "https://api.sms-activate.guru/stubs/handler_api.php"

    @classmethod
    def from_config(cls, config: dict) -> 'SmsActivateProvider':
        api_key = str(config.get("sms_activate_api_key", "") or "").strip()
        if not api_key:
            raise ConfigError("SMS-Activate 未配置 API Key")
        return cls(
            api_key=api_key,
            default_country=str(
                config.get("sms_country")
                or config.get("sms_activate_country")
                or config.get("sms_activate_default_country")
                or ""
            ),
            proxy=str(config.get("sms_proxy") or config.get("proxy") or "") or None,
        )

    def __init__(self, api_key: str, *, default_country: str = "", proxy: str = None):
        self.api_key = api_key
        self.default_country = default_country or "ru"
        self._proxy = {"http": proxy, "https": proxy} if proxy else None

    def _request(self, action: str, **params) -> str:
        params["api_key"] = self.api_key
        params["action"] = action
        resp = requests.get(
            self.BASE_URL,
            params=params,
            timeout=20,
            proxies=self._proxy,
        )
        resp.raise_for_status()
        return resp.text.strip()

    def get_balance(self) -> float:
        result = self._request("getBalance")
        if result.startswith("ACCESS_BALANCE:"):
            return float(result.split(":")[1])
        raise RuntimeError(f"SMS-Activate getBalance failed: {result}")

    def resolve_service_code(self, *, platform: str = "", service: str = "") -> str:
        explicit = str(service or "").strip().lower()
        if explicit:
            return explicit
        platform_name = str(platform or "").strip().lower()
        return SMS_ACTIVATE_SERVICES.get(platform_name, SMS_ACTIVATE_SERVICES["default"])

    def get_number(
        self,
        *,
        platform: str = "",
        service: str = "",
        country: str = "",
    ) -> SmsActivation:
        service_code = self.resolve_service_code(platform=platform, service=service)
        country_id = _resolve_sms_activate_country_id(country, self.default_country)

        result = self._request("getNumber", service=service_code, country=country_id)
        if result.startswith("ACCESS_NUMBER:"):
            parts = result.split(":")
            return SmsActivation(
                activation_id=parts[1],
                phone_number=parts[2],
                country=country or self.default_country,
            )

        if "NO_NUMBERS" in result:
            raise TransientError(f"SMS-Activate: 当前无可用号码 (service={service_code}, country={country_id})")
        if "NO_BALANCE" in result:
            raise ProviderError("SMS-Activate: 余额不足")
        raise ProviderError(f"SMS-Activate getNumber failed: {result}")

    def get_code(
        self,
        activation_id: str,
        *,
        timeout: int = 120,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if should_stop is not None and should_stop():
                return ""
            result = self._request("getStatus", id=activation_id)
            if result.startswith("STATUS_OK:"):
                return result.split(":")[1]
            if result == "STATUS_WAIT_CODE":
                time.sleep(3)
                continue
            if result == "STATUS_WAIT_RETRY":
                self._request("setStatus", id=activation_id, status="6")
                time.sleep(3)
                continue
            if result == "STATUS_CANCEL":
                return ""
            time.sleep(3)

        self.cancel(activation_id)
        return ""

    def cancel(self, activation_id: str) -> bool:
        result = self._request("setStatus", id=activation_id, status="8")
        return "ACCESS" in result

    def report_success(self, activation_id: str) -> bool:
        result = self._request("setStatus", id=activation_id, status="6")
        return "ACCESS" in result
