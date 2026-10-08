"""Application configuration, loaded from environment variables (and `.env` in development).

Secrets are never hard-coded: every credential is read from the environment.
Carrier credentials are normally stored encrypted in the database (entered via the admin UI);
`<CARRIER_CODE>_CREDENTIALS_JSON` environment variables are an optional bootstrap fallback.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application -----------------------------------------------------------------------
    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "Trekiva Logistics"
    app_url: str = "https://localhost"
    """Public HTTPS URL of this app (as configured in shopify.app.toml)."""
    log_level: str = "INFO"
    log_json: bool = True

    # --- Shopify ---------------------------------------------------------------------------
    shopify_client_id: str = ""
    shopify_client_secret: SecretStr = SecretStr("")
    shopify_api_version: str = "2026-07"
    shopify_expiring_offline_tokens: bool = True
    shopify_shop_allowlist: str = ""
    """Comma-separated *.myshopify.com domains allowed to use this private app. Empty = any."""
    shopify_http_timeout_seconds: float = 20.0

    # --- Infrastructure --------------------------------------------------------------------
    database_url: str = "postgresql+psycopg://trekiva:trekiva@localhost:5432/trekiva"
    database_pool_size: int = 10
    redis_url: str = "redis://localhost:6379/0"

    # --- Security --------------------------------------------------------------------------
    encryption_keys: SecretStr = SecretStr("")
    """Comma-separated Fernet keys. The first key encrypts; all keys decrypt (rotation)."""
    dev_auth_bypass_shop: str | None = None
    """Development only: treat admin API calls as authenticated for this shop domain."""

    # --- Carriers --------------------------------------------------------------------------
    allow_mock_carriers: bool = True
    carrier_http_timeout_seconds: float = 20.0
    carrier_offer_timeout_seconds: float = 15.0

    # --- Review checks ---------------------------------------------------------------------
    geoip_city_db_path: str = ""
    """Path to a MaxMind GeoLite2-City (or GeoIP2-City) .mmdb file, used by the location risk
    check. Empty = the IP comparison is skipped (the billing-address comparison still runs)."""

    # --- Alerts ----------------------------------------------------------------------------
    alert_slack_webhook_url: SecretStr = SecretStr("")
    """Slack incoming-webhook URL. Empty = Slack alerts off."""
    alert_email_to: str = ""
    """Comma-separated recipients. Empty = email alerts off."""
    alert_email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    alert_min_severity: Literal["WARNING", "ERROR", "CRITICAL"] = "WARNING"
    alert_cooldown_minutes: int = 30
    """After an alert is sent, further alerts with the same fingerprint wait this long and are
    then sent together as one message."""

    # --- Monitoring ------------------------------------------------------------------------
    sentry_dsn: SecretStr = SecretStr("")
    """Empty = Sentry off."""
    sentry_environment: str = ""
    """Defaults to APP_ENV."""
    sentry_release: str = ""
    sentry_traces_sample_rate: float = 0.0
    metrics_token: SecretStr = SecretStr("")
    """Bearer token for GET /metrics. Empty = endpoint disabled (404)."""
    ops_stuck_minutes: int = 30
    """Health checks flag work that has made no progress for this long."""
    ops_queue_backlog_threshold: int = 500

    # --- Admin UI --------------------------------------------------------------------------
    admin_ui_dist: str = "../admin-ui/dist"

    @field_validator("shopify_api_version")
    @classmethod
    def _check_api_version(cls, value: str) -> str:
        parts = value.split("-")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError("SHOPIFY_API_VERSION must look like YYYY-MM")
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def shop_allowlist(self) -> frozenset[str]:
        return frozenset(
            s.strip().lower() for s in self.shopify_shop_allowlist.split(",") if s.strip()
        )

    def is_shop_allowed(self, shop_domain: str) -> bool:
        allowlist = self.shop_allowlist
        return not allowlist or shop_domain.lower() in allowlist

    @property
    def fernet_keys(self) -> list[str]:
        return [k.strip() for k in self.encryption_keys.get_secret_value().split(",") if k.strip()]

    @property
    def alert_email_recipients(self) -> list[str]:
        return [a.strip() for a in self.alert_email_to.split(",") if a.strip()]

    def mock_carriers_enabled(self) -> bool:
        return self.allow_mock_carriers and not self.is_production

    @staticmethod
    def carrier_env_credentials(carrier_code: str) -> dict[str, Any] | None:
        """Optional bootstrap credentials from `<CODE>_CREDENTIALS_JSON`."""
        raw = os.environ.get(f"{carrier_code.upper()}_CREDENTIALS_JSON")
        if not raw:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"{carrier_code.upper()}_CREDENTIALS_JSON must be a JSON object")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
