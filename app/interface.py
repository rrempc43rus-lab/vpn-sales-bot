from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import UploadFile

from .config import Settings

if TYPE_CHECKING:
    from .db import Database


DEFAULT_INTERFACE_SETTINGS: dict[str, str] = {
    "brand_name": "My VPN",
    "brand_tagline": "Fast • Private • Stable",
    "welcome_text": "Современный VPN-сервис с быстрым подключением и простой выдачей доступа.",
    "features_text": (
        "• стабильная подписка для телефона и ПК\n"
        "• быстрое подключение по ссылке или QR-коду\n"
        "• удобное обновление подписки\n"
        "• поддержка прямо в Telegram"
    ),
    "about_text": (
        "• быстрое подключение без ручной настройки\n"
        "• один профиль для телефона, планшета и ПК\n"
        "• подписка обновляется по короткой ссылке\n"
        "• доступ выдается прямо в Telegram\n"
        "• поддержка находится в одном касании"
    ),
    "setup_text": (
        "1. Выберите тариф.\n"
        "2. Оплатите заказ кнопкой оплаты.\n"
        "3. После подтверждения платежа бот отправит подписку и QR-код.\n"
        "4. Откройте подписку в приложении и включите VPN."
    ),
    "plans_intro": (
        "Выберите подходящий тариф ниже. Все серверы уже доступны внутри подписки.\n\n"
        "В каждом тарифе уже есть:\n"
        "• готовая подписка\n"
        "• QR-код для быстрого входа\n"
        "• короткая ссылка для обновления\n"
        "• выдача доступа через Telegram"
    ),
    "support_text": (
        "Если подписка не обновляется или VPN не подключается:\n"
        "1. Обновите подписку в приложении.\n"
        "2. Если не помогло, удалите профиль и импортируйте его заново.\n"
        "3. Если вопрос остался, напишите в поддержку."
    ),
    "delivery_text": (
        "Подписка успешно активирована.\n\n"
        "1. Нажмите кнопку «Открыть подписку».\n"
        "2. Импортируйте профиль в приложение.\n"
        "3. Включите VPN и пользуйтесь."
    ),
    "button_buy": "🚀 Подключить VPN",
    "button_plans": "Серверы",
    "button_orders": "📦 Мои подписки",
    "button_profile": "👤 Профиль",
    "button_about": "⚡ Почему мы",
    "button_setup": "🛠️ Как подключить",
    "button_support": "💬 Поддержка",
    "button_open_subscription": "🚀 Открыть подписку",
    "button_short_link": "🔗 Короткая ссылка",
    "button_write_support": "💬 Написать в поддержку",
    "button_privacy_policy": "🔒 Политика",
    "button_user_agreement": "📑 Соглашение",
    "referral_reward_percent": "15",
    "referral_new_user_discount_percent": "10",
    "loyalty_discount_percent": "7",
    "loyalty_orders_threshold": "3",
    "max_bonus_writeoff_percent": "30",
    "hero_image_path": "static/bot-hero.png",
    "delivery_image_path": "",
}

UPLOAD_DIR = Path("media") / "bot-assets"


def default_app_settings(settings: Settings) -> dict[str, str]:
    return {
        **DEFAULT_INTERFACE_SETTINGS,
        "payment_instructions": settings.payment_instructions,
        "support_contact": settings.support_contact,
        "brand_name": settings.bot_name,
    }


def get_interface_settings(db: "Database", settings: Settings) -> dict[str, str]:
    values = default_app_settings(settings)
    values.update(db.get_settings())
    return values


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_asset_path(asset_ref: str | None) -> Path | None:
    if not asset_ref:
        return None
    path = Path(asset_ref)
    if not path.is_absolute():
        path = project_root() / path
    return path


def asset_web_path(asset_ref: str | None) -> str | None:
    if not asset_ref:
        return None
    normalized = asset_ref.replace("\\", "/").lstrip("/")
    if normalized.startswith(("static/", "media/")):
        return f"/{normalized}"
    return None


def support_contact_url(contact: str) -> str | None:
    contact = contact.strip()
    if not contact or contact == "@change_me":
        return None
    if contact.startswith("@"):
        return f"https://t.me/{contact[1:]}"
    if contact.startswith(("http://", "https://")):
        return contact
    if "@" in contact:
        return f"mailto:{contact}"
    return None


async def save_uploaded_asset(upload: UploadFile, slot: str) -> str:
    suffix = Path(upload.filename or "").suffix.lower() or ".png"
    relative = UPLOAD_DIR / f"{slot}-{uuid4().hex[:12]}{suffix}"
    absolute = project_root() / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)
    content = await upload.read()
    absolute.write_bytes(content)
    return str(relative).replace("\\", "/")
