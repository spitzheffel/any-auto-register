"""SMSBower 接码实现 —— API 与 HeroSMS 兼容，仅 base URL 和鉴权规则不同。"""
from __future__ import annotations

import requests

from providers.registry import register_provider
from providers.sms.herosms import HeroSmsProvider


@register_provider("sms", "smsbower_api")
class SmsBowerProvider(HeroSmsProvider):
    """SMSBower provider — API 兼容 HeroSMS，仅 base URL 不同。"""

    BASE_URL = "https://smsbower.page/stubs/handler_api.php"
    PROVIDER_LABEL = "SMSBower"
    CONFIG_PREFIX = "smsbower"

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        # SMSBower 所有接口都需要 api_key（包括 getServicesList、getCountries）
        payload = dict(params)
        if needs_key or self.api_key:
            payload["api_key"] = self.api_key
        resp = requests.get(self.BASE_URL, params=payload, timeout=timeout, proxies=self.proxies)
        self._raise_for_status(resp, action=str(params.get("action") or "unknown"))
        return resp
