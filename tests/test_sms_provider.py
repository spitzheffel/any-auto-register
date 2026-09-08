"""SMS provider unit tests."""
from __future__ import annotations

import pytest
from core.base_sms import BaseSmsProvider, SmsActivation, create_sms_provider
from core.phone_verification import OUTCOME_SUCCESS, create_phone_verification
import providers.sms.herosms as hero_module
from providers.sms import number_pool as pool_module
from providers.sms.herosms import HERO_SMS_SERVICES, HeroSmsProvider
from providers.sms.sms_activate import (
    SMS_ACTIVATE_COUNTRIES,
    SMS_ACTIVATE_SERVICES,
    SmsActivateProvider,
)
from providers.sms.smsbower import SmsBowerProvider


@pytest.fixture(autouse=True)
def pool(tmp_path, monkeypatch):
    """每个用例一个干净的号码池（独立缓存文件、独立黑名单）。"""
    fresh = pool_module.HeroSmsNumberPool(
        cache_file_factory=lambda: tmp_path / ".herosms_phone_cache.json"
    )
    monkeypatch.setattr(pool_module, "_shared_pool", fresh)
    monkeypatch.setattr(pool_module, "shared_pool", lambda: fresh)
    # HeroSmsProvider.__init__ 用的是 herosms 模块自己 import 进来的那个名字
    monkeypatch.setattr(hero_module, "shared_pool", lambda: fresh)
    return fresh


class TestSmsActivateServiceMapping:
    def test_cursor_maps_to_ot(self):
        assert SMS_ACTIVATE_SERVICES["cursor"] == "ot"

    def test_chatgpt_maps_to_dr(self):
        assert SMS_ACTIVATE_SERVICES["chatgpt"] == "dr"

    def test_default_exists(self):
        assert "default" in SMS_ACTIVATE_SERVICES

    def test_provider_resolves_platform_and_native_override(self):
        provider = SmsActivateProvider("test")
        assert provider.resolve_service_code(platform="chatgpt") == "dr"
        assert provider.resolve_service_code(platform="cursor") == "ot"
        assert provider.resolve_service_code(platform="unknown") == "ot"
        assert provider.resolve_service_code(platform="chatgpt", service="go") == "go"


class TestHeroSmsServiceMapping:
    def test_known_platforms_and_default(self):
        provider = HeroSmsProvider("test")
        assert HERO_SMS_SERVICES["chatgpt"] == "dr"
        assert provider.resolve_service_code(platform="chatgpt") == "dr"
        assert provider.resolve_service_code(platform="cursor") == "ot"
        assert provider.resolve_service_code(platform="unknown") == "ot"

    def test_explicit_service_overrides_platform(self):
        provider = HeroSmsProvider("test")
        assert provider.resolve_service_code(platform="chatgpt", service="go") == "go"

    def test_configured_fallback_does_not_override_known_platform(self):
        provider = HeroSmsProvider("test", default_service="full")
        assert provider.resolve_service_code(platform="chatgpt") == "dr"
        assert provider.resolve_service_code(platform="cursor") == "ot"
        assert provider.resolve_service_code(platform="unknown") == "full"


class TestSmsActivateCountryMapping:
    def test_us_maps_to_187(self):
        assert SMS_ACTIVATE_COUNTRIES["us"] == "187"

    def test_ru_maps_to_0(self):
        assert SMS_ACTIVATE_COUNTRIES["ru"] == "0"

    def test_th_maps_to_52(self):
        assert SMS_ACTIVATE_COUNTRIES["th"] == "52"

    def test_default_exists(self):
        assert "default" in SMS_ACTIVATE_COUNTRIES


class TestCreateSmsProvider:
    def test_sms_activate(self):
        provider = create_sms_provider("sms_activate", {"sms_activate_api_key": "test123"})
        assert isinstance(provider, SmsActivateProvider)
        assert provider.api_key == "test123"

    def test_sms_activate_missing_key(self):
        with pytest.raises(RuntimeError, match="未配置"):
            create_sms_provider("sms_activate", {})

    def test_herosms(self):
        provider = create_sms_provider("herosms", {"herosms_api_key": "hero123"})
        assert isinstance(provider, HeroSmsProvider)
        assert provider.api_key == "hero123"
        assert provider.default_service == "ot"
        assert provider.default_country == "187"

    def test_herosms_reuse_flag_parses_string_false(self):
        provider = create_sms_provider(
            "herosms",
            {
                "herosms_api_key": "hero123",
                "register_reuse_phone_to_max": "false",
            },
        )
        assert isinstance(provider, HeroSmsProvider)
        assert provider.reuse_phone_to_max is False

    def test_herosms_missing_key(self):
        with pytest.raises(RuntimeError, match="HeroSMS 未配置"):
            create_sms_provider("herosms", {})

    def test_unknown_provider(self):
        with pytest.raises(RuntimeError, match="未知"):
            create_sms_provider("unknown", {})


class _RecordingProvider(BaseSmsProvider):
    """可配置的 SMS provider 替身，记录所有生命周期调用。"""

    def __init__(self, events, *, activation_id="act_1", phone="+15551234567", code="", fail_first=False):
        self.events = events
        self.activation_id = activation_id
        self.phone = phone
        self.code = code
        self.fail_first = fail_first
        self.calls = 0

    def resolve_service_code(self, *, platform: str = "", service: str = "") -> str:
        return service or platform

    def get_number(self, *, platform: str = "", service: str = "", country: str = ""):
        self.calls += 1
        self.events.append(("get_number", self.calls, platform, service, country))
        if self.fail_first and self.calls == 1:
            raise RuntimeError("temporary failure")
        return SmsActivation(activation_id=self.activation_id, phone_number=self.phone)

    def get_code(self, activation_id: str, *, timeout: int = 120, should_stop=None) -> str:
        self.events.append(("get_code", activation_id, timeout))
        return self.code

    def cancel(self, activation_id: str) -> bool:
        self.events.append(("cancel", activation_id))
        return True

    def report_success(self, activation_id: str) -> bool:
        self.events.append(("report_success", activation_id))
        return True

    def mark_send_succeeded(self, activation_id: str) -> None:
        self.events.append(("mark_send_succeeded", activation_id))

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        self.events.append(("mark_send_failed", activation_id, reason))

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        self.events.append(("mark_code_failed", activation_id, reason))


class _ExclusiveWindowProvider(_RecordingProvider):
    """需要独占"租号 → 验证完成"窗口的 provider（等价于号码可复用的厂商）。"""

    requires_exclusive_verification_window = True


class _AutoCountryProvider(_RecordingProvider):
    """只覆写 select_best_country 的第三方 provider：不认识任何厂商前缀配置键。"""

    def __init__(self, events, *, best_country="66", **kwargs):
        super().__init__(events, **kwargs)
        self.best_country = best_country

    def select_best_country(self, *, platform="", service="", log_fn=None):
        self.events.append(("select_best_country", platform, service))
        if log_fn is not None:
            log_fn("正在查询最优国家（价格最低 + 库存充足）...")
            log_fn(f"自动选择最优国家: {self.best_country}")
        return self.best_country

    def fallback_country(self) -> str:
        return "999"


class TestPhoneVerification:
    def test_auto_country_uses_resolved_provider_service_code(self, monkeypatch):
        events = []
        logs = []
        # 自动选国的开关与阈值由 provider 自己从配置里吸收，通用层不认识这些键
        provider = HeroSmsProvider.from_config(
            {"herosms_api_key": "hero123", "herosms_auto_country": "true"}
        )

        def fake_best_country(*, service=None, min_stock=20, max_price=0):
            events.append(("best_country", service))
            return "52"

        def fake_get_number(*, platform="", service="", country=""):
            events.append(("get_number", platform, service, country))
            return SmsActivation(activation_id="act_map", phone_number="+66123456789")

        monkeypatch.setattr(provider, "get_best_country", fake_best_country)
        monkeypatch.setattr(provider, "get_number", fake_get_number)
        monkeypatch.setattr(provider, "cancel", lambda activation_id: True)
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)

        phone = create_phone_verification(
            "herosms",
            {
                "herosms_api_key": "hero123",
                "herosms_auto_country": "true",
            },
            platform="chatgpt",
            log_fn=logs.append,
        )

        assert phone.acquire().phone_number == "+66123456789"
        phone.close()
        assert ("best_country", "dr") in events
        assert ("get_number", "chatgpt", "", "52") in events
        assert any("platform=chatgpt service_code=dr" in item for item in logs)
        assert any("正在查询最优国家（价格最低 + 库存充足）..." == item for item in logs)
        assert any("自动选择最优国家: 52" == item for item in logs)

    def test_third_party_provider_selects_country_without_core_changes(self, monkeypatch):
        """新增一家有选国能力的厂商：只覆写 select_best_country，通用层无需改动。"""
        events = []
        logs = []
        provider = _AutoCountryProvider(events, activation_id="act_auto", best_country="66")
        monkeypatch.setattr(
            "core.base_sms.create_sms_provider",
            lambda provider_key, config: provider,
        )

        phone = create_phone_verification(
            "brand_new_sms",
            # 配置里没有任何厂商前缀的选国键，通用层也不会去找
            {"brand_new_sms_api_key": "test"},
            platform="chatgpt",
            service="dr",
            country="187",
            log_fn=logs.append,
        )

        assert phone.acquire().phone_number == "+15551234567"
        phone.close()

        assert ("select_best_country", "chatgpt", "dr") in events
        # provider 选出的国家压过调用方传入的 country="187"
        assert ("get_number", 1, "chatgpt", "dr", "66") in events
        assert any("country=66" in item for item in logs)
        assert any("自动选择最优国家: 66" == item for item in logs)
        # 没声明需要独占验证窗口的 provider 不占锁
        assert phone._verify_lock_acquired is False

    def test_provider_selected_country_falls_back_to_provider_default(self, monkeypatch):
        events = []
        logs = []
        provider = _AutoCountryProvider(
            events, activation_id="act_fallback", best_country="66", fail_first=True
        )
        monkeypatch.setattr(
            "core.base_sms.create_sms_provider",
            lambda provider_key, config: provider,
        )

        phone = create_phone_verification(
            "brand_new_sms",
            {"brand_new_sms_api_key": "test"},
            platform="chatgpt",
            log_fn=logs.append,
        )

        assert phone.acquire().phone_number == "+15551234567"
        phone.close()

        assert ("get_number", 1, "chatgpt", "", "66") in events
        assert ("get_number", 2, "chatgpt", "", "999") in events
        assert any("回退到默认国家(999)" in item for item in logs)

    def test_plain_provider_country_selection_is_untouched(self, monkeypatch):
        events = []
        provider = _RecordingProvider(events, activation_id="act_plain", fail_first=True)
        monkeypatch.setattr(
            "core.base_sms.create_sms_provider",
            lambda provider_key, config: provider,
        )

        phone = create_phone_verification(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            platform="chatgpt",
            country="th",
        )

        # 没有选国能力的 provider：国家原样传下去，失败也不做回退重试
        with pytest.raises(RuntimeError, match="temporary failure"):
            phone.acquire()
        phone.close()
        assert [item for item in events if item[0] == "get_number"] == [
            ("get_number", 1, "chatgpt", "", "th")
        ]

    def test_provider_is_created_lazily_and_close_cancels_pending_activation(self, monkeypatch):
        events = []
        logs = []
        monkeypatch.setattr(
            "core.base_sms.create_sms_provider",
            lambda provider_key, config: _RecordingProvider(events),
        )

        phone = create_phone_verification(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            platform="chatgpt",
            country="us",
            log_fn=logs.append,
        )

        assert events == []
        lease = phone.acquire()
        assert lease.phone_number == "+15551234567"
        phone.close()

        assert ("get_number", 1, "chatgpt", "", "us") in events
        assert ("cancel", "act_1") in events
        assert any("准备租用手机号" in item for item in logs)
        assert any("已成功租到号码" in item for item in logs)
        assert any("已释放未使用号码" in item for item in logs)

    def test_close_does_not_cancel_after_success(self, monkeypatch):
        events = []
        logs = []
        monkeypatch.setattr(
            "core.base_sms.create_sms_provider",
            lambda provider_key, config: _RecordingProvider(events, activation_id="act_2", code="123456"),
        )

        phone = create_phone_verification(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            platform="chatgpt",
            log_fn=logs.append,
        )

        lease = phone.acquire()
        assert lease.wait_code() == "123456"
        phone.close()

        assert ("report_success", "act_2") in events
        assert ("cancel", "act_2") not in events
        assert any("等待短信验证码" in item for item in logs)
        assert any("短信验证成功" in item for item in logs)

    def test_deferred_success_provider_reports_on_close(self, monkeypatch):
        events = []

        class DeferredProvider(_RecordingProvider):
            auto_report_success_on_code = False

        monkeypatch.setattr(
            "core.base_sms.create_sms_provider",
            lambda provider_key, config: DeferredProvider(events, activation_id="act_deferred", code="111222"),
        )

        phone = create_phone_verification("herosms", {"herosms_api_key": "test"}, platform="cursor")
        lease = phone.acquire()
        assert lease.wait_code() == "111222"
        # 延迟上报型 provider 收到码后不立刻上报，等 release 兜底
        assert ("report_success", "act_deferred") not in events
        phone.close()
        assert ("report_success", "act_deferred") in events
        assert ("cancel", "act_deferred") not in events

    def test_first_number_fetch_failure_does_not_poison_future_retries(self, monkeypatch):
        events = []
        provider = _RecordingProvider(events, activation_id="act_retry", phone="+66123456789", code="654321", fail_first=True)
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)

        phone = create_phone_verification(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            platform="chatgpt",
            country="th",
        )

        with pytest.raises(RuntimeError, match="temporary failure"):
            phone.acquire()

        lease = phone.acquire()
        assert lease.phone_number == "+66123456789"
        assert lease.wait_code() == "654321"
        phone.close()
        assert ("report_success", "act_retry") in events

    def test_herosms_number_fetch_failure_releases_verify_lock(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "core.base_sms.create_sms_provider",
            lambda provider_key, config: _ExclusiveWindowProvider(events, fail_first=True),
        )

        phone = create_phone_verification("herosms", {"herosms_api_key": "test"}, platform="chatgpt")

        with pytest.raises(RuntimeError, match="temporary failure"):
            phone.acquire()

        assert phone._verify_lock_acquired is False
        phone.close()

    def test_report_send_succeeded_delegates_to_provider(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "core.base_sms.create_sms_provider",
            lambda provider_key, config: _RecordingProvider(events, activation_id="act_sent"),
        )

        phone = create_phone_verification("herosms", {"herosms_api_key": "test"}, platform="chatgpt")
        lease = phone.acquire()
        lease.report_send_succeeded()
        phone.close()
        assert ("mark_send_succeeded", "act_sent") in events

    def test_swap_cancels_current_number_and_rents_a_new_one(self, monkeypatch):
        events = []
        provider = _ExclusiveWindowProvider(events, activation_id="act_swap")
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)

        phone = create_phone_verification("herosms", {"herosms_api_key": "test"}, platform="chatgpt")
        first = phone.acquire()
        second = first.swap()

        assert first.released is True
        assert second is not first
        assert ("cancel", "act_swap") in events
        assert provider.calls == 2
        # swap 之后验证锁仍然握在新 lease 手上，close 才释放
        assert phone._verify_lock_acquired is True
        phone.close()
        assert phone._verify_lock_acquired is False

    def test_release_success_is_idempotent(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "core.base_sms.create_sms_provider",
            lambda provider_key, config: _RecordingProvider(events, activation_id="act_once"),
        )

        phone = create_phone_verification("sms_activate", {"sms_activate_api_key": "test"}, platform="chatgpt")
        lease = phone.acquire()
        lease.release(OUTCOME_SUCCESS)
        lease.release(OUTCOME_SUCCESS)
        phone.close()

        assert [item for item in events if item[0] == "report_success"] == [("report_success", "act_once")]
        assert ("cancel", "act_once") not in events

    def test_wait_code_forwards_should_stop_when_supported(self, monkeypatch):
        events = []

        class StoppableProvider(_RecordingProvider):
            def get_code(self, activation_id: str, *, timeout: int = 120, should_stop=None) -> str:
                self.events.append(("get_code", activation_id, timeout, should_stop is not None))
                return "" if should_stop and should_stop() else "999888"

        monkeypatch.setattr(
            "core.base_sms.create_sms_provider",
            lambda provider_key, config: StoppableProvider(events, activation_id="act_stop"),
        )

        phone = create_phone_verification("sms_activate", {"sms_activate_api_key": "test"}, platform="chatgpt")
        lease = phone.acquire()
        assert lease.wait_code(should_stop=lambda: True) == ""
        assert ("get_code", "act_stop", 180, True) in events
        phone.close()

    def test_to_ref_round_trips_without_secrets(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "core.base_sms.create_sms_provider",
            lambda provider_key, config: _RecordingProvider(events, activation_id="act_ref"),
        )

        phone = create_phone_verification(
            "sms_activate",
            {"sms_activate_api_key": "secret-key"},
            platform="chatgpt",
        )
        ref = phone.acquire().to_ref()
        phone.close()

        assert ref["provider_key"] == "sms_activate"
        assert ref["activation_id"] == "act_ref"
        assert "secret-key" not in str(ref)

        from core.phone_verification import PhoneVerification

        reclaimed = PhoneVerification.reclaim(ref, config={"sms_activate_api_key": "secret-key"})
        reclaimed.release()
        assert ("cancel", "act_ref") in events


class TestSmsActivateProviderCountryResolution:
    def test_get_number_accepts_numeric_country_id(self, monkeypatch):
        captured = {}

        def fake_request(self, action: str, **params):
            captured["action"] = action
            captured["params"] = params
            return "NO_NUMBERS"

        monkeypatch.setattr(SmsActivateProvider, "_request", fake_request)
        provider = SmsActivateProvider("test123", default_country="ru")

        with pytest.raises(RuntimeError, match="NO_NUMBERS|无可用号码"):
            provider.get_number(platform="chatgpt", country="52")

        assert captured["action"] == "getNumber"
        assert captured["params"]["service"] == "dr"
        assert captured["params"]["country"] == "52"


class TestHeroSmsProvider:
    def test_cursor_uses_ot_service_code(self, monkeypatch):
        calls = []

        class FakeResp:
            text = '{"activationId":"act_cursor","phoneNumber":"5551234","countryPhoneCode":"1"}'

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "activationId": "act_cursor",
                    "phoneNumber": "5551234",
                    "countryPhoneCode": "1",
                }

        def fake_get(url, params, timeout=30, proxies=None):
            calls.append(params)
            return FakeResp()

        monkeypatch.setattr("providers.sms.herosms.requests.get", fake_get)
        provider = HeroSmsProvider("hero123")
        provider.get_number(platform="cursor", country="187")

        assert calls[0]["service"] == "ot"

    def test_get_number_uses_v2_json(self, monkeypatch, pool):
        calls = []

        class FakeResp:
            text = '{"activationId":"act_1","phoneNumber":"5551234","countryPhoneCode":"1","activationCost":"0.6"}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"activationId": "act_1", "phoneNumber": "5551234", "countryPhoneCode": "1", "activationCost": "0.6"}

        def fake_get(url, params, timeout=30, proxies=None):
            calls.append(params)
            return FakeResp()

        monkeypatch.setattr("providers.sms.herosms.requests.get", fake_get)
        provider = HeroSmsProvider("hero123")
        activation = provider.get_number(platform="chatgpt", country="187")

        assert activation.activation_id == "act_1"
        assert activation.phone_number == "+15551234"
        number_call = next(item for item in calls if item["action"] == "getNumberV2")
        assert number_call["service"] == "dr"
        assert pool.snapshot()["service"] == "dr"

    def test_get_number_falls_back_to_v1_text(self, monkeypatch):
        calls = []

        class FakeResp:
            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

            def json(self):
                raise ValueError("not json")

        def fake_get(url, params, timeout=30, proxies=None):
            calls.append(params["action"])
            if params["action"] == "getNumberV2":
                return FakeResp("BAD")
            return FakeResp("ACCESS_NUMBER:act_2:15557654321")

        monkeypatch.setattr("providers.sms.herosms.requests.get", fake_get)
        provider = HeroSmsProvider("hero123")
        activation = provider.get_number(platform="chatgpt", country="187")

        assert activation.activation_id == "act_2"
        assert activation.phone_number == "+15557654321"
        assert [item for item in calls if item != "getPrices"] == ["getNumberV2", "getNumber"]

    def test_http_error_does_not_leak_api_key(self, monkeypatch):
        secret = "hero-secret-key"

        class FakeResp:
            status_code = 404

            def raise_for_status(self):
                raise hero_module.requests.HTTPError(
                    f"404 for https://hero-sms.com/?api_key={secret}"
                )

        monkeypatch.setattr(
            "providers.sms.herosms.requests.get",
            lambda *args, **kwargs: FakeResp(),
        )
        provider = HeroSmsProvider(secret)

        with pytest.raises(RuntimeError) as exc_info:
            provider._request({"action": "getNumberV2"})

        assert secret not in str(exc_info.value)
        assert "HTTP 404" in str(exc_info.value)
        assert "getNumberV2" in str(exc_info.value)

    def test_get_code_skips_attempted_sms_event(self, monkeypatch, pool):
        pool.set_snapshot({
            "api_key_hash": hero_module._hash_secret("hero123"),
            "service": "dr",
            "country": "187",
            "activation_id": "act_3",
            "phone_number": "+15550000000",
            "acquired_at": hero_module.time.time(),
            "use_count": 0,
            "used_codes": set(),
            "attempted_sms_keys": set(),
            "reuse_stopped": False,
        })
        provider = HeroSmsProvider("hero123")
        first = {"status": "ok", "code": "111111", "sms_key": "sms_1", "allow_same_code": True}
        second = {"status": "ok", "code": "222222", "sms_key": "sms_2", "allow_same_code": True}
        results = [first, second]

        monkeypatch.setattr(provider, "get_status_v2", lambda activation_id: results.pop(0))
        monkeypatch.setattr(provider, "get_status", lambda activation_id: {"status": "wait_code"})
        monkeypatch.setattr(provider, "get_active_activations", lambda: [])
        monkeypatch.setattr(provider, "request_resend_sms", lambda activation_id: True)

        assert provider.get_code("act_3", timeout=1) == "111111"
        provider.mark_code_failed("act_3", "invalid otp")
        assert provider.get_code("act_3", timeout=1) == "222222"

    def test_mark_send_succeeded_sets_sms_sent_status(self, monkeypatch):
        calls = []
        provider = HeroSmsProvider("hero123")
        monkeypatch.setattr(provider, "set_status", lambda activation_id, status: calls.append((activation_id, status)) or "ACCESS_READY")

        provider.mark_send_succeeded("act_4")

        assert calls == [("act_4", 1)]

    def test_mark_code_failed_triggers_openai_and_herosms_resend(self, monkeypatch, pool):
        pool.set_snapshot({
            "api_key_hash": hero_module._hash_secret("hero123"),
            "service": "dr",
            "country": "187",
            "activation_id": "act_5",
            "phone_number": "+15550000000",
            "acquired_at": hero_module.time.time(),
            "use_count": 0,
            "used_codes": set(),
            "attempted_sms_keys": set(),
            "reuse_stopped": False,
        })
        events = []
        provider = HeroSmsProvider("hero123")
        provider.last_code_result = {"code": "333333", "sms_key": "sms_3"}
        provider.set_resend_callback(lambda: events.append(("openai_resend",)))
        monkeypatch.setattr(provider, "request_resend_sms", lambda activation_id: events.append(("hero_resend", activation_id)) or True)

        provider.mark_code_failed("act_5", "invalid otp")

        assert ("openai_resend",) in events
        assert ("hero_resend", "act_5") in events
        assert "333333" in pool.snapshot()["used_codes"]
        assert "sms_3" in pool.snapshot()["attempted_sms_keys"]

    def test_report_success_finishes_activation_when_reuse_disabled(self, monkeypatch, pool):
        pool.set_snapshot({
            "api_key_hash": hero_module._hash_secret("hero123"),
            "service": "dr",
            "country": "187",
            "activation_id": "act_6",
            "phone_number": "+15550000000",
            "acquired_at": hero_module.time.time(),
            "use_count": 0,
            "used_codes": set(),
            "attempted_sms_keys": set(),
            "reuse_stopped": False,
        })
        events = []
        provider = HeroSmsProvider("hero123", reuse_phone_to_max=False)
        provider.last_code_result = {"code": "444444", "sms_key": "sms_4"}
        monkeypatch.setattr(provider, "finish_activation", lambda activation_id: events.append(("finish", activation_id)) or True)

        assert provider.report_success("act_6") is True

        assert events == [("finish", "act_6")]
        assert pool.snapshot() is None


def _live_cache(api_key: str = "hero123", *, service: str = "dr", country: str = "187", **overrides) -> dict:
    cache = {
        "api_key_hash": hero_module._hash_secret(api_key),
        "service": service,
        "country": country,
        "activation_id": "act_reuse",
        "phone_number": "+447858792530",
        "acquired_at": hero_module.time.time(),
        "use_count": 1,
        "used_codes": set(),
        "attempted_sms_keys": set(),
        "reuse_stopped": False,
    }
    cache.update(overrides)
    return cache


class TestCountrySelectionHooks:
    def test_base_provider_declares_no_country_capability(self):
        provider = _RecordingProvider([])

        assert provider.peek_reusable_country(platform="chatgpt") == ""
        assert provider.select_best_country(platform="chatgpt") == ""
        assert provider.fallback_country() == ""
        assert provider.requires_exclusive_verification_window is False

    def test_fallback_country_is_the_provider_default_country(self):
        assert SmsActivateProvider("test", default_country="th").fallback_country() == "th"
        assert HeroSmsProvider("hero123").fallback_country() == "187"
        assert HeroSmsProvider("hero123", default_country="52").fallback_country() == "52"

    def test_auto_country_is_off_unless_configured(self):
        provider = HeroSmsProvider.from_config({"herosms_api_key": "hero123"})

        assert provider.auto_country is False
        assert provider.select_best_country(platform="chatgpt") == ""

    def test_hero_from_config_absorbs_auto_country_settings(self):
        provider = HeroSmsProvider.from_config({
            "herosms_api_key": "hero123",
            "herosms_country": "52",
            "herosms_auto_country": "true",
            "herosms_auto_country_min_stock": "7",
            "herosms_auto_country_max_price": "0.35",
        })

        assert provider.auto_country is True
        assert provider.auto_country_min_stock == 7
        assert provider.auto_country_max_price == 0.35
        assert provider.fallback_country() == "52"

    def test_smsbower_from_config_absorbs_its_own_prefixed_keys(self):
        provider = SmsBowerProvider.from_config({
            "smsbower_api_key": "bower123",
            "smsbower_auto_country": "true",
            "smsbower_auto_country_min_stock": "3",
        })

        assert provider.auto_country is True
        assert provider.auto_country_min_stock == 3
        assert provider.auto_country_max_price == 0
        assert provider.requires_exclusive_verification_window is True

    def test_select_best_country_logs_and_swallows_failure(self, monkeypatch):
        logs = []
        provider = HeroSmsProvider.from_config({
            "herosms_api_key": "hero123",
            "herosms_auto_country": "true",
        })

        def boom(*, service=None, min_stock=20, max_price=0):
            raise RuntimeError("api down")

        monkeypatch.setattr(provider, "get_best_country", boom)
        assert provider.select_best_country(platform="chatgpt", log_fn=logs.append) == ""
        assert logs[-1] == "智能国家选择失败(api down)，使用默认配置"

        monkeypatch.setattr(
            provider,
            "get_best_country",
            lambda *, service=None, min_stock=20, max_price=0: None,
        )
        logs.clear()
        assert provider.select_best_country(platform="chatgpt", log_fn=logs.append) == ""
        assert logs[-1] == "未找到满足条件的国家，使用默认配置"

    def test_select_best_country_passes_configured_thresholds(self, monkeypatch):
        captured = {}
        provider = HeroSmsProvider.from_config({
            "herosms_api_key": "hero123",
            "herosms_auto_country": "1",
            "herosms_auto_country_min_stock": "9",
            "herosms_auto_country_max_price": "0.2",
        })

        def fake_best_country(*, service=None, min_stock=20, max_price=0):
            captured.update(service=service, min_stock=min_stock, max_price=max_price)
            return "52"

        monkeypatch.setattr(provider, "get_best_country", fake_best_country)

        assert provider.select_best_country(platform="chatgpt") == "52"
        assert captured == {"service": "dr", "min_stock": 9, "max_price": 0.2}


class TestHeroSmsPeekReusableCountry:
    def test_returns_cached_country_for_same_service(self, pool):
        pool.set_snapshot(_live_cache(country="16"))
        provider = HeroSmsProvider("hero123")

        assert provider.peek_reusable_country(platform="chatgpt") == "16"

    def test_ignores_cache_of_a_different_service(self, pool):
        pool.set_snapshot(_live_cache(service="ot"))
        provider = HeroSmsProvider("hero123")

        assert provider.peek_reusable_country(platform="chatgpt") == ""

    def test_ignores_stopped_or_exhausted_cache(self, pool):
        provider = HeroSmsProvider("hero123", phone_success_max=2)

        pool.set_snapshot(_live_cache(reuse_stopped=True))
        assert provider.peek_reusable_country(platform="chatgpt") == ""

        pool.set_snapshot(_live_cache(use_count=2))
        assert provider.peek_reusable_country(platform="chatgpt") == ""

    def test_ignores_expired_cache(self, pool):
        expired_at = hero_module.time.time() - pool_module.HERO_SMS_PHONE_LIFETIME - 1
        pool.set_snapshot(_live_cache(acquired_at=expired_at))
        provider = HeroSmsProvider("hero123")

        assert provider.peek_reusable_country(platform="chatgpt") == ""

    def test_returns_empty_when_reuse_disabled(self, pool):
        pool.set_snapshot(_live_cache(country="16"))
        provider = HeroSmsProvider("hero123", reuse_phone_to_max=False)

        assert provider.peek_reusable_country(platform="chatgpt") == ""


class TestHeroSmsCountryBlacklist:
    def test_failed_country_is_skipped_by_best_country(self, monkeypatch):
        provider = HeroSmsProvider("hero123")
        monkeypatch.setattr(
            provider,
            "get_top_countries",
            lambda service=None: [{"country": "52", "price": 0.1, "count": 100}],
        )

        assert provider.get_best_country(service="dr") == "52"

        provider._blacklist_country("dr", "52")
        assert provider.get_best_country(service="dr") is None

    def test_blacklist_is_scoped_to_service(self, monkeypatch):
        provider = HeroSmsProvider("hero123")
        monkeypatch.setattr(
            provider,
            "get_top_countries",
            lambda service=None: [{"country": "52", "price": 0.1, "count": 100}],
        )
        provider._blacklist_country("dr", "52")

        assert provider.get_best_country(service="ot") == "52"

    def test_blacklist_entry_expires(self, monkeypatch):
        provider = HeroSmsProvider("hero123")
        provider._blacklist_country("dr", "52")
        assert provider._is_country_blacklisted("dr", "52") is True

        future = hero_module.time.time() + pool_module.HERO_SMS_COUNTRY_BLACKLIST_TTL + 1
        # 黑名单过期判断在 number_pool 里读 time.time()；hero_module.time 就是 time 模块本身，
        # 这里打的补丁对两边同时生效
        monkeypatch.setattr(hero_module.time, "time", lambda: future)
        assert provider._is_country_blacklisted("dr", "52") is False

    def test_get_number_blacklists_country_on_failure(self, monkeypatch):
        provider = HeroSmsProvider("hero123")

        def boom(service, country):
            raise RuntimeError("NO_NUMBERS")

        monkeypatch.setattr(provider, "_request_number_raw", boom)

        with pytest.raises(RuntimeError, match="NO_NUMBERS"):
            provider.get_number(platform="chatgpt", country="52")

        assert provider._is_country_blacklisted("dr", "52") is True


class TestSmsActivation:
    def test_dataclass(self):
        a = SmsActivation(activation_id="123", phone_number="+79001234567")
        assert a.activation_id == "123"
        assert a.phone_number == "+79001234567"
        assert a.country == ""

    def test_with_country(self):
        a = SmsActivation(activation_id="1", phone_number="+1555", country="us")
        assert a.country == "us"
