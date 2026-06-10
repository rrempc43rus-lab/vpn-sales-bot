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
    "brand_tagline": "Быстрое подключение • Стабильный доступ • Поддержка в Telegram",
    "welcome_text": "Подключение к VPN за пару минут: выберите тариф, оплатите заказ и сразу получите готовую подписку.",
    "features_text": (
        "• готовая ссылка для iPhone, Android, Windows и macOS\n"
        "• QR-код для подключения в одно касание\n"
        "• стабильные серверы без ручной настройки\n"
        "• продление и поддержка прямо в Telegram"
    ),
    "about_text": (
        "• быстрое подключение без сложной ручной настройки\n"
        "• одна подписка для телефона, планшета и ПК\n"
        "• выдача доступа сразу после оплаты\n"
        "• удобное продление без лишних шагов\n"
        "• поддержка всегда в одном сообщении"
    ),
    "setup_text": (
        "1. Выберите тариф в меню.\n"
        "2. Оплатите заказ удобным способом.\n"
        "3. После подтверждения бот отправит подписку и QR-код.\n"
        "4. Откройте подписку в приложении и включите VPN."
    ),
    "plans_intro": (
        "Выберите тариф ниже — бот сразу подготовит заказ.\n\n"
        "В каждом тарифе уже есть:\n"
        "• готовая подписка для быстрого входа\n"
        "• QR-код для подключения в 1 касание\n"
        "• короткая ссылка для обновления\n"
        "• помощь в Telegram, если что-то не работает"
    ),
    "support_text": (
        "Если подписка не обновляется или VPN не подключается:\n"
        "1. Обновите подписку в приложении.\n"
        "2. Перезапустите приложение и импортируйте подписку заново.\n"
        "3. Если проблема осталась, напишите нам — поможем вручную."
    ),
    "delivery_text": (
        "Подписка активирована.\n\n"
        "1. Нажмите кнопку «Открыть подписку».\n"
        "2. Импортируйте профиль в приложение.\n"
        "3. Включите VPN и пользуйтесь.\n\n"
        "Если что-то не открылось, напишите в поддержку."
    ),
    "button_buy": "🚀 Подключить VPN",
    "button_plans": "🌍 Серверы",
    "button_orders": "📦 Подписки",
    "button_profile": "👤 Кабинет",
    "button_about": "✨ Почему мы",
    "button_setup": "📲 Как подключить",
    "button_support": "🛟 Поддержка",
    "button_open_subscription": "🚀 Открыть VPN",
    "button_short_link": "🔗 Резервная ссылка",
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
