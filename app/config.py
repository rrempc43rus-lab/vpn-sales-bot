from __future__ import annotations

import os
from dataclasses import dataclass


def _must(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


@dataclass(slots=True)
class Settings:
    app_host: str
    app_port: int
    public_base_url: str
    app_secret_key: str
    admin_username: str
    admin_password: str
    bot_token: str
    bot_name: str
    support_contact: str
    payment_instructions: str
    admin_telegram_id: int | None
    xui_base_url: str
    xui_username: str
    xui_password: str
    db_path: str
    platega_base_url: str
    platega_merchant_id: str | None
    platega_secret: str | None
    platega_payment_method: int | None
    platega_return_url: str | None
    platega_failed_url: str | None

    @property
    def platega_enabled(self) -> bool:
        return bool(self.platega_merchant_id and self.platega_secret)


def load_settings() -> Settings:
    admin_telegram_raw = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
    admin_telegram_id = int(admin_telegram_raw) if admin_telegram_raw else None
    platega_payment_method_raw = os.getenv("PLATEGA_PAYMENT_METHOD", "").strip()
    platega_payment_method = int(platega_payment_method_raw) if platega_payment_method_raw else None
    return Settings(
        app_host=os.getenv("APP_HOST", "0.0.0.0"),
        app_port=int(os.getenv("APP_PORT", "8085")),
        public_base_url=_must("PUBLIC_BASE_URL", "http://127.0.0.1:8085"),
        app_secret_key=_must("APP_SECRET_KEY"),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=_must("ADMIN_PASSWORD"),
        bot_token=_must("BOT_TOKEN"),
        bot_name=os.getenv("BOT_NAME", "My VPN"),
        support_contact=os.getenv("SUPPORT_CONTACT", "@JOLASEKATeam"),
        payment_instructions=os.getenv(
            "PAYMENT_INSTRUCTIONS",
            'Send payment and then press "I paid" in the bot.',
        ),
        admin_telegram_id=admin_telegram_id,
        xui_base_url=_must("XUI_BASE_URL"),
        xui_username=_must("XUI_USERNAME"),
        xui_password=_must("XUI_PASSWORD"),
        db_path=os.getenv("DB_PATH", "/opt/vpn-sales-bot/data/app.db"),
        platega_base_url=os.getenv("PLATEGA_BASE_URL", "https://app.platega.io").rstrip("/"),
        platega_merchant_id=_optional("PLATEGA_MERCHANT_ID"),
        platega_secret=_optional("PLATEGA_SECRET"),
        platega_payment_method=platega_payment_method,
        platega_return_url=_optional("PLATEGA_RETURN_URL"),
        platega_failed_url=_optional("PLATEGA_FAILED_URL"),
    )
