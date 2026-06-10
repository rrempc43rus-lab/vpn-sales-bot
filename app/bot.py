from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Settings
from .db import Database, TRIAL_DURATION_DAYS, plan_inbound_ids
from .interface import get_interface_settings, resolve_asset_path, support_contact_url
from .platega import PlategaClient
from .services import (
    build_proxy_subscription_link,
    build_short_subscription_link,
    handle_successful_order_payment,
    notify_admin,
)
from .xui import XuiClient


STATUS_META: dict[str, tuple[str, str]] = {
    "pending_payment": ("🕒", "Ожидает оплату"),
    "waiting_review": ("🔎", "Проверяем оплату"),
    "paid": ("💳", "Оплачен"),
    "delivered": ("✅", "Подписка активна"),
    "delivery_failed": ("⚠️", "Ошибка выдачи"),
    "cancelled": ("⛔", "Отменен"),
}


def status_badge(status: str) -> str:
    icon, label = STATUS_META.get(status, ("•", status))
    return f"{icon} {label}"


def format_price_rub(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " RUB"


def format_date(value: str | None) -> str:
    if not value:
        return "Не указано"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.strftime("%d.%m.%Y %H:%M")


def format_traffic_gb(value: int) -> str:
    if value <= 0:
        return "Безлимит"
    return f"{value} ГБ"


def privacy_policy_url(settings: Settings) -> str:
    return f"{settings.public_base_url.rstrip('/')}/privacy-policy"


def user_agreement_url(settings: Settings) -> str:
    return f"{settings.public_base_url.rstrip('/')}/user-agreement"


def build_home_text(ui: dict[str, str]) -> str:
    return (
        f"🚀 {ui['brand_name']}\n"
        f"{ui['brand_tagline']}\n\n"
        f"{ui['welcome_text']}\n\n"
        "Что вы получаете:\n"
        f"{ui['features_text']}\n\n"
        "Нажмите кнопку ниже, чтобы выбрать тариф."
    )


def build_about_text(ui: dict[str, str]) -> str:
    return f"{ui['button_about']}\n\n{ui['about_text']}"


def build_setup_text(ui: dict[str, str]) -> str:
    return f"{ui['button_setup']}\n\n{ui['setup_text']}"


def build_plans_text(ui: dict[str, str], server_labels: list[str]) -> str:
    lines = [ui["button_plans"], ""]
    if server_labels:
        lines.append("Наши доступные серверы:")
        lines.extend(f"• {label}" for label in server_labels)
        lines.append("")
    lines.append(ui["plans_intro"])
    return "\n".join(lines)


def plan_text(plan) -> str:
    description = str(plan["description"]).strip()
    lines = [
        f"🔥 {plan['name']}",
        "",
        f"Цена: {format_price_rub(int(plan['price_rub']))}",
        f"Срок действия: {int(plan['duration_days'])} дней",
        f"Трафик: {format_traffic_gb(int(plan['traffic_gb']))}",
    ]
    if description:
        lines.extend(["", description])
    lines.extend(
        [
            "",
            "После оплаты бот автоматически подготовит доступ, отправит подписку и QR-код для подключения.",
        ]
    )
    return "\n".join(lines)


def build_order_price_block(order) -> list[str]:
    if int(order["is_trial"] or 0) == 1:
        return ["Стоимость: бесплатно"]

    base_amount = int(order["base_amount_rub"] or order["amount_rub"])
    discount_rub = int(order["discount_rub"] or 0)
    bonus_applied_rub = int(order["bonus_applied_rub"] or 0)
    discount_percent = int(order["applied_discount_percent"] or 0)

    lines = [f"Базовая цена: {format_price_rub(base_amount)}"]
    if discount_rub > 0:
        lines.append(f"Скидка {discount_percent}%: -{format_price_rub(discount_rub)}")
    if bonus_applied_rub > 0:
        lines.append(f"Списано бонусами: -{format_price_rub(bonus_applied_rub)}")
    if discount_rub > 0 or bonus_applied_rub > 0:
        lines.append(f"К оплате: {format_price_rub(int(order['amount_rub']))}")
    return lines


def orders_text(orders: list, ui: dict[str, str]) -> str:
    if not orders:
        return (
            f"{ui['button_orders']}\n\n"
            "У вас пока нет активных заказов.\n\n"
            "Выберите тариф и бот сразу подготовит новый заказ."
        )

    lines = [ui["button_orders"], ""]
    for row in orders[:8]:
        lines.append(f"{status_badge(str(row['status']))}\n#{row['public_id']} • {row['plan_name']}")
        lines.append("")
    lines.append("Нажмите на нужный заказ ниже, чтобы открыть подробности.")
    return "\n".join(lines).strip()


def order_text(settings: Settings, order, payment_instructions: str) -> str:
    status = str(order["status"])
    automatic_payment = bool(order["payment_url"])
    is_trial = int(order["is_trial"] or 0) == 1
    plan_name = "Пробный VPN на 3 дня" if is_trial else str(order["plan_name"])
    lines = [
        f"{status_badge(status)}",
        f"Заказ #{order['public_id']}",
        "",
        f"Тариф: {plan_name}",
        f"Создан: {format_date(str(order['created_at']))}",
        "",
        *build_order_price_block(order),
    ]

    if is_trial:
        lines.append("Режим: пробный доступ")
    elif order["payment_provider"]:
        lines.append(f"Провайдер оплаты: {order['payment_provider']}")
    if not is_trial and order["payment_status"]:
        lines.append(f"Статус платежа: {order['payment_status']}")

    if is_trial and status in {"pending_payment", "waiting_review", "paid"}:
        lines.extend(
            [
                "",
                "Пробный доступ активируется автоматически.",
                "Если ссылка еще не появилась, откройте заказ снова через несколько секунд.",
            ]
        )
    elif status in {"pending_payment", "waiting_review", "paid"}:
        if automatic_payment:
            lines.extend(
                [
                    "",
                    "Оплатите заказ кнопкой ниже.",
                    "После подтверждения платежа бот сам активирует подписку и пришлет доступ.",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "Как оплатить:",
                    payment_instructions,
                    "",
                    'После оплаты нажмите кнопку "Я оплатил".',
                ]
            )

    if status == "delivered":
        proxy_link = build_proxy_subscription_link(settings, order)
        short_link = build_short_subscription_link(settings, order)
        lines.extend(
            [
                "",
                "Подписка уже готова к использованию.",
                "Откройте ее кнопкой ниже или используйте ссылку:",
            ]
        )
        if proxy_link:
            lines.append(proxy_link)
        if short_link and short_link != proxy_link:
            lines.append(short_link)

    if order["provisioning_error"]:
        lines.extend(["", f"Техническая ошибка: {order['provisioning_error']}"])

    return "\n".join(lines)


def checkout_text(order, payment_instructions: str) -> str:
    automatic_payment = bool(order["payment_url"])
    is_trial = int(order["is_trial"] or 0) == 1
    plan_name = "Пробный VPN на 3 дня" if is_trial else str(order["plan_name"])
    lines = [
        "🧾 Новый заказ",
        "",
        f"Номер: #{order['public_id']}",
        f"Тариф: {plan_name}",
        f"Срок: {int(order['duration_days'])} дней",
        f"Трафик: {format_traffic_gb(int(order['traffic_gb']))}",
        "",
        *build_order_price_block(order),
    ]
    if is_trial:
        lines.extend(
            [
                "",
                "Пробный доступ активируется автоматически.",
                "Бот сам подготовит подписку и отправит ссылку без оплаты.",
            ]
        )
    elif automatic_payment:
        lines.extend(
            [
                "",
                "Нажмите кнопку оплаты ниже.",
                "После подтверждения платежа бот автоматически активирует подписку.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Инструкция по оплате:",
                payment_instructions,
                "",
                'После оплаты нажмите кнопку "Я оплатил".',
            ]
        )
    if int(order["discount_rub"] or 0) > 0 or int(order["bonus_applied_rub"] or 0) > 0:
        lines.extend(["", "Скидки и бонусы уже применены автоматически."])
    return "\n".join(lines)


def support_text(ui: dict[str, str], support_contact: str) -> str:
    return f"{ui['button_support']}\n\n{ui['support_text']}\n\nКонтакт: {support_contact}"


def profile_text(
    settings: Settings,
    ui: dict[str, str],
    profile,
    bot_username: str | None,
    support_contact: str,
    loyalty_settings: dict[str, int],
) -> str:
    referral_code = str(profile["referral_code"] or "").upper()
    bonus_balance = int(profile["referral_bonus_balance_rub"] or 0)
    delivered_orders_count = int(profile["delivered_orders_count"] or 0)
    referred_users_count = int(profile["referred_users_count"] or 0)
    loyalty_percent = loyalty_settings["loyalty_discount_percent"]
    loyalty_threshold = loyalty_settings["loyalty_orders_threshold"]
    referral_reward_percent = loyalty_settings["referral_reward_percent"]
    referral_discount = loyalty_settings["referral_new_user_discount_percent"]
    next_loyalty_step = max(0, loyalty_threshold - delivered_orders_count)

    lines = [
        ui["button_profile"],
        "",
        f"ID: {profile['telegram_id']}",
        f"Имя: {(profile['first_name'] or '').strip() or 'Клиент'}",
        f"Подписок куплено: {delivered_orders_count}",
        f"Реферальный код: {referral_code}",
        "",
        f"Бонусный баланс: {format_price_rub(bonus_balance)}",
        f"Приглашено друзей: {referred_users_count}",
        f"Заработано по рефералке: {format_price_rub(int(profile['total_referral_earned_rub'] or 0))}",
        "",
        "Ваши выгоды:",
        f"• друг получает скидку {referral_discount}% на первый заказ",
        f"• вы получаете {referral_reward_percent}% от первой оплаченной подписки друга",
        f"• бонусами можно оплатить до {loyalty_settings['max_bonus_writeoff_percent']}% следующего заказа",
    ]

    if loyalty_threshold > 0 and loyalty_percent > 0:
        if delivered_orders_count >= loyalty_threshold:
            lines.append(f"• ваша персональная скидка {loyalty_percent}% уже активна")
        else:
            lines.append(
                f"• до постоянной скидки {loyalty_percent}% осталось {next_loyalty_step} оплаченных заказ(а)"
            )

    if profile["inviter_telegram_id"]:
        inviter = profile["inviter_username"] or profile["inviter_telegram_id"]
        lines.extend(["", f"Вас пригласил: {inviter}"])

    if bot_username and referral_code:
        referral_link = f"https://t.me/{bot_username}?start=ref_{referral_code}"
        lines.extend(["", "Ваша ссылка:", referral_link])

    lines.extend(["", f"Поддержка: {support_contact}"])
    return "\n".join(lines)


def main_menu(settings: Settings, ui: dict[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ui["button_buy"], callback_data="plans")],
            [
                InlineKeyboardButton(text=ui["button_plans"], callback_data="plans"),
                InlineKeyboardButton(text=ui["button_orders"], callback_data="orders"),
            ],
            [
                InlineKeyboardButton(text=ui["button_profile"], callback_data="profile"),
                InlineKeyboardButton(text=ui["button_about"], callback_data="about"),
            ],
            [
                InlineKeyboardButton(text=ui["button_setup"], callback_data="setup"),
                InlineKeyboardButton(text=ui["button_support"], callback_data="support"),
            ],
            [
                InlineKeyboardButton(text=ui["button_privacy_policy"], url=privacy_policy_url(settings)),
                InlineKeyboardButton(text=ui["button_user_agreement"], url=user_agreement_url(settings)),
            ],
        ]
    )


def plans_keyboard(db: Database, ui: dict[str, str]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for plan in db.list_plans(active_only=True):
        buttons.append(
            [InlineKeyboardButton(text=f"{plan['name']} • {format_price_rub(int(plan['price_rub']))}", callback_data=f"plan:{plan['id']}")]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                text=f"🎁 Пробный VPN на {TRIAL_DURATION_DAYS} дня",
                callback_data="trial",
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(text=ui["button_profile"], callback_data="profile"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def plan_keyboard(plan_id: int, ui: dict[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выбрать этот тариф", callback_data=f"buy:{plan_id}")],
            [
                InlineKeyboardButton(text=f"{ui['button_plans']} назад", callback_data="plans"),
                InlineKeyboardButton(text=ui["button_support"], callback_data="support"),
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
        ]
    )


def orders_keyboard(orders: list, ui: dict[str, str]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for row in orders[:8]:
        icon = STATUS_META.get(str(row["status"]), ("•", ""))[0]
        buttons.append([InlineKeyboardButton(text=f"{icon} #{row['public_id']}", callback_data=f"order:{row['public_id']}")])
    buttons.append([InlineKeyboardButton(text=ui["button_buy"], callback_data="plans")])
    buttons.append(
        [
            InlineKeyboardButton(text=ui["button_profile"], callback_data="profile"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def order_keyboard(settings: Settings, order, support_contact: str, ui: dict[str, str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    payment_url = str(order["payment_url"] or "").strip()
    status = str(order["status"])
    is_trial = int(order["is_trial"] or 0) == 1

    if not is_trial and payment_url and status in {"pending_payment", "waiting_review"}:
        rows.append([InlineKeyboardButton(text="💳 Оплатить", url=payment_url)])
    elif not is_trial and status in {"pending_payment", "waiting_review", "paid"}:
        rows.append([InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{order['public_id']}")])

    proxy_link = build_proxy_subscription_link(settings, order)
    short_link = build_short_subscription_link(settings, order)
    if status == "delivered" and proxy_link:
        rows.append([InlineKeyboardButton(text=ui["button_open_subscription"], url=proxy_link)])
    if status == "delivered" and short_link and short_link != proxy_link:
        rows.append([InlineKeyboardButton(text=ui["button_short_link"], url=short_link)])

    support_url = support_contact_url(support_contact)
    if support_url:
        rows.append([InlineKeyboardButton(text=ui["button_write_support"], url=support_url)])

    rows.append(
        [
            InlineKeyboardButton(text="📦 К подпискам", callback_data="orders"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_keyboard(ui: dict[str, str], share_url: str | None, support_contact: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if share_url:
        rows.append([InlineKeyboardButton(text="🎁 Пригласить друга", url=share_url)])
    support_url = support_contact_url(support_contact)
    rows.append(
        [
            InlineKeyboardButton(text=ui["button_plans"], callback_data="plans"),
            InlineKeyboardButton(text=ui["button_orders"], callback_data="orders"),
        ]
    )
    if support_url:
        rows.append([InlineKeyboardButton(text=ui["button_write_support"], url=support_url)])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_referral_share_url(bot_username: str | None, referral_code: str, ui: dict[str, str]) -> str | None:
    if not bot_username or not referral_code:
        return None
    referral_link = f"https://t.me/{bot_username}?start=ref_{referral_code}"
    share_text = (
        f"Подключайся к {ui['brand_name']}.\n"
        "По моей ссылке ты получишь скидку на первый заказ."
    )
    return f"https://t.me/share/url?url={quote(referral_link, safe='')}&text={quote(share_text, safe='')}"


def extract_start_referral_code(message: Message) -> str | None:
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return None
    payload = parts[1].strip()
    if not payload.startswith("ref_"):
        return None
    code = payload[4:].strip().upper()
    return code or None


def build_router(settings: Settings, db: Database, bot: Bot, xui: XuiClient, platega: PlategaClient) -> Router:
    router = Router()
    bot_username_cache: str | None = None

    async def safe_answer(callback: CallbackQuery, text: str | None = None, *, show_alert: bool = False) -> None:
        with suppress(TelegramBadRequest):
            await callback.answer(text, show_alert=show_alert)

    async def ensure_user(message: Message | CallbackQuery):
        user = message.from_user
        if user is None:
            return None
        return db.upsert_user(user.id, user.username, user.first_name, user.last_name)

    def ui() -> dict[str, str]:
        return get_interface_settings(db, settings)

    def get_support_contact() -> str:
        return db.get_settings().get("support_contact", settings.support_contact)

    def get_payment_instructions() -> str:
        return db.get_settings().get("payment_instructions", settings.payment_instructions)

    async def get_available_server_labels() -> list[str]:
        active_inbound_ids = {
            inbound_id
            for plan in db.list_plans(active_only=True)
            for inbound_id in plan_inbound_ids(plan)
        }
        if not active_inbound_ids:
            return []
        labels: list[str] = []
        for inbound in await xui.list_inbounds():
            if inbound.id in active_inbound_ids:
                labels.append(inbound.label.strip())
        return labels

    async def get_bot_username() -> str | None:
        nonlocal bot_username_cache
        if bot_username_cache:
            return bot_username_cache
        me = await bot.get_me()
        bot_username_cache = me.username
        return bot_username_cache

    async def send_home_message(chat_id: int, *, with_banner: bool = False, extra_notice: str | None = None) -> None:
        current_ui = ui()
        banner_path = resolve_asset_path(current_ui.get("hero_image_path"))
        if with_banner and banner_path and banner_path.exists():
            await bot.send_photo(chat_id, FSInputFile(str(banner_path)))
        text = build_home_text(current_ui)
        if extra_notice:
            text = f"{extra_notice}\n\n{text}"
        await bot.send_message(chat_id, text, reply_markup=main_menu(settings, current_ui))

    async def show_profile(target: Message | CallbackQuery) -> None:
        current_ui = ui()
        support_contact = get_support_contact()
        profile = db.get_user_profile_by_telegram_id(target.from_user.id)
        if profile is None:
            text = "Профиль пока недоступен. Попробуйте еще раз через пару секунд."
            if isinstance(target, CallbackQuery):
                await safe_answer(target, text, show_alert=True)
            else:
                await target.answer(text)
            return

        bot_username = await get_bot_username()
        share_url = build_referral_share_url(bot_username, str(profile["referral_code"]), current_ui)
        text = profile_text(settings, current_ui, profile, bot_username, support_contact, db.get_loyalty_settings())
        markup = profile_keyboard(current_ui, share_url, support_contact)
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, reply_markup=markup)
            await safe_answer(target)
        else:
            await target.answer(text, reply_markup=markup)

    @router.message(CommandStart())
    async def start_handler(message: Message) -> None:
        await ensure_user(message)
        referral_code = extract_start_referral_code(message)
        referral_notice = None
        if referral_code and db.attach_referral(message.from_user.id, referral_code):
            referral_notice = "🎁 Реферальная скидка активирована. Она применится к вашему первому заказу автоматически."
        await send_home_message(message.chat.id, with_banner=True, extra_notice=referral_notice)

    @router.callback_query(F.data == "menu")
    async def menu_handler(callback: CallbackQuery) -> None:
        await ensure_user(callback)
        current_ui = ui()
        await callback.message.edit_text(build_home_text(current_ui), reply_markup=main_menu(settings, current_ui))
        await safe_answer(callback)

    @router.callback_query(F.data == "profile")
    async def profile_handler(callback: CallbackQuery) -> None:
        await ensure_user(callback)
        await show_profile(callback)

    @router.callback_query(F.data == "about")
    async def about_handler(callback: CallbackQuery) -> None:
        await ensure_user(callback)
        current_ui = ui()
        await callback.message.edit_text(
            build_about_text(current_ui),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=current_ui["button_buy"], callback_data="plans")],
                    [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
                ]
            ),
        )
        await safe_answer(callback)

    @router.callback_query(F.data == "setup")
    async def setup_handler(callback: CallbackQuery) -> None:
        await ensure_user(callback)
        current_ui = ui()
        await callback.message.edit_text(
            build_setup_text(current_ui),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=current_ui["button_buy"], callback_data="plans")],
                    [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
                ]
            ),
        )
        await safe_answer(callback)

    @router.callback_query(F.data == "plans")
    async def plans_handler(callback: CallbackQuery) -> None:
        await ensure_user(callback)
        current_ui = ui()
        plans = db.list_plans(active_only=True)
        if not plans:
            await callback.message.edit_text(
                "Сейчас активных тарифов нет.\n\nСначала добавьте inbound в 3x-ui и создайте тариф в админке.",
                reply_markup=main_menu(settings, current_ui),
            )
            await safe_answer(callback)
            return
        server_labels = await get_available_server_labels()
        await callback.message.edit_text(
            build_plans_text(current_ui, server_labels),
            reply_markup=plans_keyboard(db, current_ui),
        )
        await safe_answer(callback)

    @router.callback_query(F.data == "trial")
    async def trial_handler(callback: CallbackQuery) -> None:
        await ensure_user(callback)
        current_ui = ui()
        user = db.get_user_by_telegram_id(callback.from_user.id)
        if user is None:
            await safe_answer(callback, "Не удалось активировать пробный доступ", show_alert=True)
            return

        if db.has_used_trial(int(user["id"])):
            await safe_answer(callback, "Пробный доступ уже был активирован ранее", show_alert=True)
            return

        try:
            order = db.create_trial_order(int(user["id"]))
        except ValueError as exc:
            message = str(exc)
            if "already used" in message.lower():
                message = "Пробный доступ уже был активирован ранее"
            elif "paid plan is configured" in message.lower():
                message = "Пробный режим пока недоступен. Сначала нужен хотя бы один активный платный тариф."
            await safe_answer(callback, message, show_alert=True)
            return

        db.update_order_payment(
            int(order["id"]),
            provider="trial",
            provider_status="CONFIRMED",
            payment_method="TRIAL",
            currency="RUB",
            raw_payload={"type": "trial", "duration_days": TRIAL_DURATION_DAYS},
        )

        try:
            await handle_successful_order_payment(
                bot,
                settings,
                db,
                xui,
                int(order["id"]),
                payment_note="Trial activated",
                admin_message=(
                    f"Trial access for {TRIAL_DURATION_DAYS} days was activated for user "
                    f"{order['telegram_id']} (order #{order['public_id']})."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            await notify_admin(
                bot,
                settings.admin_telegram_id,
                f"Trial delivery failed for order #{order['public_id']}: {exc}",
            )
            failed_order = db.get_order_by_public_id(str(order["public_id"]))
            if failed_order is not None:
                await callback.message.edit_text(
                    order_text(settings, failed_order, get_payment_instructions()),
                    reply_markup=order_keyboard(settings, failed_order, get_support_contact(), current_ui),
                )
            await safe_answer(callback, "Пробный доступ создан, но выдача пока не удалась", show_alert=True)
            return

        completed_order = db.get_order_by_public_id(str(order["public_id"]))
        if completed_order is not None:
            await callback.message.edit_text(
                order_text(settings, completed_order, get_payment_instructions()),
                reply_markup=order_keyboard(settings, completed_order, get_support_contact(), current_ui),
            )
        await safe_answer(callback, f"Пробный доступ на {TRIAL_DURATION_DAYS} дня активирован")

    @router.callback_query(F.data.startswith("plan:"))
    async def plan_detail_handler(callback: CallbackQuery) -> None:
        await ensure_user(callback)
        current_ui = ui()
        plan_id = int(callback.data.split(":", 1)[1])
        plan = db.get_plan(plan_id)
        if plan is None or not int(plan["is_active"]):
            await safe_answer(callback, "Тариф недоступен", show_alert=True)
            return
        await callback.message.edit_text(plan_text(plan), reply_markup=plan_keyboard(plan_id, current_ui))
        await safe_answer(callback)

    @router.callback_query(F.data == "orders")
    async def orders_handler(callback: CallbackQuery) -> None:
        await ensure_user(callback)
        current_ui = ui()
        user = db.get_user_by_telegram_id(callback.from_user.id)
        if user is None:
            await safe_answer(callback, "Пользователь не найден", show_alert=True)
            return
        orders = db.list_user_orders(int(user["id"]))
        await callback.message.edit_text(orders_text(orders, current_ui), reply_markup=orders_keyboard(orders, current_ui))
        await safe_answer(callback)

    @router.callback_query(F.data.startswith("order:"))
    async def order_detail_handler(callback: CallbackQuery) -> None:
        await ensure_user(callback)
        current_ui = ui()
        public_id = callback.data.split(":", 1)[1]
        order = db.get_order_by_public_id(public_id)
        if order is None or int(order["telegram_id"]) != callback.from_user.id:
            await safe_answer(callback, "Заказ не найден", show_alert=True)
            return
        await callback.message.edit_text(
            order_text(settings, order, get_payment_instructions()),
            reply_markup=order_keyboard(settings, order, get_support_contact(), current_ui),
        )
        await safe_answer(callback)

    @router.callback_query(F.data == "support")
    async def support_handler(callback: CallbackQuery) -> None:
        await ensure_user(callback)
        current_ui = ui()
        support_contact = get_support_contact()
        support_url = support_contact_url(support_contact)
        keyboard_rows = []
        if support_url:
            keyboard_rows.append([InlineKeyboardButton(text=current_ui["button_write_support"], url=support_url)])
        keyboard_rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu")])
        await callback.message.edit_text(
            support_text(current_ui, support_contact),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )
        await safe_answer(callback)

    @router.callback_query(F.data.startswith("buy:"))
    async def buy_handler(callback: CallbackQuery) -> None:
        await ensure_user(callback)
        current_ui = ui()
        user = db.get_user_by_telegram_id(callback.from_user.id)
        if user is None:
            await safe_answer(callback, "Не удалось создать заказ", show_alert=True)
            return
        plan_id = int(callback.data.split(":", 1)[1])
        plan = db.get_plan(plan_id)
        if plan is None or not int(plan["is_active"]):
            await safe_answer(callback, "Тариф недоступен", show_alert=True)
            return

        order = db.create_order(int(user["id"]), int(plan["id"]))
        if settings.platega_enabled:
            try:
                payment = await platega.create_payment(
                    order_ref=str(order["public_id"]),
                    amount_rub=int(order["amount_rub"]),
                    description=f"Оплата VPN заказа #{order['public_id']}",
                )
            except Exception as exc:  # noqa: BLE001
                db.update_order_status(int(order["id"]), "cancelled", payment_note=f"Platega create payment failed: {exc}")
                await notify_admin(
                    bot,
                    settings.admin_telegram_id,
                    f"Не удалось создать платеж Platega для заказа #{order['public_id']}: {exc}",
                )
                await callback.message.edit_text(
                    "Не удалось создать страницу оплаты. Попробуйте еще раз чуть позже или напишите в поддержку.",
                    reply_markup=main_menu(settings, current_ui),
                )
                await safe_answer(callback, "Платежная страница недоступна", show_alert=True)
                return

            db.update_order_payment(
                int(order["id"]),
                provider="platega",
                transaction_id=payment.transaction_id,
                provider_status=payment.status,
                payment_url=payment.redirect_url,
                payment_method=payment.payment_method,
                currency="RUB",
                raw_payload=payment.raw_payload,
            )
            refreshed = db.get_order_by_public_id(str(order["public_id"]))
            if refreshed is not None:
                order = refreshed

        await callback.message.edit_text(
            checkout_text(order, get_payment_instructions()),
            reply_markup=order_keyboard(settings, order, get_support_contact(), current_ui),
        )
        await safe_answer(callback, "Заказ создан")

    @router.callback_query(F.data.startswith("paid:"))
    async def paid_handler(callback: CallbackQuery) -> None:
        await ensure_user(callback)
        current_ui = ui()
        public_id = callback.data.split(":", 1)[1]
        order = db.get_order_by_public_id(public_id)
        if order is None or int(order["telegram_id"]) != callback.from_user.id:
            await safe_answer(callback, "Заказ не найден", show_alert=True)
            return

        if int(order["is_trial"] or 0) == 1 or str(order["payment_provider"]) == "trial":
            try:
                await handle_successful_order_payment(
                    bot,
                    settings,
                    db,
                    xui,
                    int(order["id"]),
                    payment_note="Trial access confirmed",
                    admin_message=(
                        f"Trial access was confirmed for user {order['telegram_id']} "
                        f"(order #{order['public_id']})."
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                await notify_admin(
                    bot,
                    settings.admin_telegram_id,
                    f"Trial recovery failed for order #{order['public_id']}: {exc}",
                )
                failed_order = db.get_order_by_public_id(public_id)
                if failed_order is not None:
                    with suppress(TelegramBadRequest):
                        await callback.message.edit_text(
                            order_text(settings, failed_order, get_payment_instructions()),
                            reply_markup=order_keyboard(settings, failed_order, get_support_contact(), current_ui),
                        )
                await safe_answer(callback, "Пробный доступ пока не удалось активировать", show_alert=True)
                return

            updated_trial_order = db.get_order_by_public_id(public_id)
            if updated_trial_order is not None:
                with suppress(TelegramBadRequest):
                    await callback.message.edit_text(
                        order_text(settings, updated_trial_order, get_payment_instructions()),
                        reply_markup=order_keyboard(settings, updated_trial_order, get_support_contact(), current_ui),
                    )
            await safe_answer(callback, "Пробный доступ активирован")
            return

        if str(order["payment_provider"]) == "platega" and order["payment_transaction_id"]:
            try:
                payload = await platega.get_transaction(str(order["payment_transaction_id"]))
            except Exception as exc:  # noqa: BLE001
                await safe_answer(callback, f"Не удалось проверить оплату: {exc}", show_alert=True)
                return

            provider_status = str(payload.get("status") or order["payment_status"] or "").upper()
            db.update_order_payment(
                int(order["id"]),
                provider_status=provider_status,
                payment_url=str(payload.get("redirect") or order["payment_url"] or ""),
                payment_method=str(payload.get("paymentMethod") or order["payment_method"] or ""),
                raw_payload=payload,
            )

            if provider_status == "CONFIRMED":
                try:
                    await handle_successful_order_payment(
                        bot,
                        settings,
                        db,
                        xui,
                        int(order["id"]),
                        payment_note="Platega payment confirmed",
                        admin_message=f"✅ Оплата подтверждена: {order['telegram_id']}",
                    )
                except Exception as exc:  # noqa: BLE001
                    await notify_admin(
                        bot,
                        settings.admin_telegram_id,
                        f"Оплата заказа #{order['public_id']} подтверждена, но выдача не удалась: {exc}",
                    )
                    updated_failed = db.get_order_by_public_id(public_id)
                    if updated_failed is not None:
                        await callback.message.edit_text(
                            order_text(settings, updated_failed, get_payment_instructions()),
                            reply_markup=order_keyboard(settings, updated_failed, get_support_contact(), current_ui),
                        )
                    await safe_answer(callback, "Оплата принята, но выдача пока не удалась", show_alert=True)
                    return

                updated_paid = db.get_order_by_public_id(public_id)
                if updated_paid is not None:
                    await callback.message.edit_text(
                        order_text(settings, updated_paid, get_payment_instructions()),
                        reply_markup=order_keyboard(settings, updated_paid, get_support_contact(), current_ui),
                    )
                await safe_answer(callback, "Оплата подтверждена")
                return

            if provider_status in {"CANCELED", "CHARGEBACK"} and str(order["status"]) != "delivered":
                db.update_order_status(int(order["id"]), "cancelled", payment_note=f"Platega status: {provider_status}")

            updated_order = db.get_order_by_public_id(public_id)
            if updated_order is not None:
                await callback.message.edit_text(
                    order_text(settings, updated_order, get_payment_instructions()),
                    reply_markup=order_keyboard(settings, updated_order, get_support_contact(), current_ui),
                )
            if provider_status in {"CANCELED", "CHARGEBACK"}:
                await safe_answer(callback, "Платеж отменен или отклонен", show_alert=True)
            else:
                await safe_answer(callback, "Платеж пока не подтвержден", show_alert=True)
            return

        db.update_order_status(int(order["id"]), "waiting_review")
        updated_order = db.get_order_by_public_id(public_id)
        if updated_order is None:
            await safe_answer(callback, "Заказ не найден", show_alert=True)
            return
        await callback.message.edit_text(
            order_text(settings, updated_order, get_payment_instructions()),
            reply_markup=order_keyboard(settings, updated_order, get_support_contact(), current_ui),
        )
        await notify_admin(
            bot,
            settings.admin_telegram_id,
            (
                f"Новый запрос на проверку оплаты VPN: #{public_id}\n"
                f"Сумма к оплате: {format_price_rub(int(updated_order['amount_rub']))}"
            ),
        )
        await safe_answer(callback, "Отправлено на проверку")

    return router


def build_dispatcher(
    settings: Settings,
    db: Database,
    bot: Bot,
    xui: XuiClient,
    platega: PlategaClient,
) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(settings, db, bot, xui, platega))
    return dispatcher
