from __future__ import annotations

from core.config_store import config_store
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository


class ConfigRepository:
    BASE_KEYS = {
        "laoudo_auth", "laoudo_email", "laoudo_account_id",
        "yescaptcha_key", "twocaptcha_key", "solver_url",
        "default_executor", "default_captcha_solver",
        "default_identity_provider", "default_oauth_provider", "oauth_email_hint",
        "chrome_user_data_dir", "chrome_cdp_url",
        "duckmail_api_url", "duckmail_provider_url", "duckmail_bearer",
        "freemail_api_url", "freemail_admin_token", "freemail_username", "freemail_password",
        "moemail_api_url", "moemail_username", "moemail_password", "moemail_session_token",
        "mail_provider",
        "cfworker_api_url", "cfworker_admin_token", "cfworker_domain", "cfworker_fingerprint",
        "cpa_api_url", "cpa_api_key", "cpa_use_proxy",
        "team_manager_url", "team_manager_key",
    }

    def __init__(self, definitions: ProviderDefinitionsRepository | None = None):
        self.definitions = definitions or ProviderDefinitionsRepository()

    def get_allowed_keys(self) -> set[str]:
        keys = set(self.BASE_KEYS)
        for provider_type in ("mailbox", "captcha"):
            for definition in self.definitions.list_by_type(provider_type, enabled_only=False):
                for field in definition.get_fields():
                    field_key = str(field.get("key") or "").strip()
                    if field_key:
                        keys.add(field_key)
        return keys

    def get_flat(self) -> dict[str, str]:
        data = config_store.get_all()
        allowed = self.get_allowed_keys()
        return {
            key: str(value or "")
            for key, value in data.items()
            if key in allowed
        }

    def update_flat(self, data: dict[str, str]) -> list[str]:
        allowed = self.get_allowed_keys()
        safe = {key: value for key, value in data.items() if key in allowed}
        config_store.set_many(safe)
        return list(safe.keys())
