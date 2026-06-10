from __future__ import annotations

import io
import sqlite3
from typing import Any

import qrcode
from aiogram import Bot
from aiogram.types import BufferedInputFile, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .config import Settings
from .db import Database, plan_inbound_ids
from .interface import get_interface_settings, resolve_asset_path, support_contact_url
from .xui import ProvisionedClient, XuiClient, rewrite_local_url


async def notify_admin(bot: Bot, admin_telegram_id: int | None, text: str) -> None:
    if not admin_telegram_id:
        return
    await bot.send_message(admin_telegram_id, text)


async def notify_referral_reward(bot: Bot, reward: dict[str, int | str] | None) -> None:
    if reward is None:
        return
    await bot.send_message(
        int(reward["inviter_telegram_id"]),
        (
            f"🎁 Вам начислен бонус {reward['reward_rub']} RUB "
            f"({reward['reward_percent']}% от оплаты {reward['paid_amount_rub']} RUB) за приглашенного друга.\n"
            f"Текущий бонусный баланс: {reward['bonus_balance_rub']} RUB."
        ),
    )


async def deliver_order(
    db: Database,
    xui: XuiClient,
    order: sqlite3.Row,
) -> ProvisionedClient:
    provisioned = await xui.provision_client(
        telegram_id=int(order["telegram_id"]),
        order_code=str(order["public_id"]),
        duration_days=int(order["duration_days"]),
        traffic_gb=int(order["traffic_gb"]),
        inbound_ids=plan_inbound_ids(order),
    )
    db.complete_order(
        int(order["id"]),
        status="delivered",
        xui_email=provisioned.email,
        subscription_url=provisioned.subscription_url,
        links=provisioned.links,
        error=None,
    )
    return provisioned


async def handle_successful_order_payment(
    bot: Bot,
    settings: Settings,
    db: Database,
    xui: XuiClient,
    order_id: int,
    *,
    payment_note: str | None = None,
    admin_message: str | None = None,
) -> sqlite3.Row | None:
    order = db.get_order(order_id)
    if order is None:
        return None
    if str(order["status"]) == "delivered":
        return order

    if str(order["status"]) != "paid":
        db.update_order_status(order_id, "paid", payment_note=payment_note)
        order = db.get_order(order_id)
        if order is None:
            return None

    try:
        await deliver_order(db, xui, order)
    except Exception as exc:  # noqa: BLE001
        db.complete_order(
            order_id,
            status="delivery_failed",
            xui_email="",
            subscription_url=None,
            links=[],
            error=str(exc),
        )
        raise

    completed = db.get_order(order_id)
    if completed is None:
        return None

    await send_delivery_message(bot, settings, db, int(completed["telegram_id"]), completed)
    await notify_referral_reward(bot, db.apply_referral_reward(order_id))
    await notify_admin(
        bot,
        settings.admin_telegram_id,
        admin_message or f"Заказ #{completed['public_id']} выдан пользователю {completed['telegram_id']}.",
    )
    return completed


def build_short_subscription_link(settings: Settings, order: sqlite3.Row) -> str | None:
    if not order["xui_subscription_url"]:
        return None
    return f"{settings.public_base_url.rstrip('/')}/s/{order['public_id']}"


def build_proxy_subscription_link(settings: Settings, order: sqlite3.Row) -> str | None:
    if not order["xui_subscription_url"]:
        return None
    return f"{settings.public_base_url.rstrip('/')}/sub/{order['public_id']}"


def build_qr_png(payload: str) -> bytes:
    image = qrcode.make(payload)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def delivery_keyboard(
    db: Database,
    settings: Settings,
    order: sqlite3.Row,
) -> InlineKeyboardMarkup | None:
    ui = get_interface_settings(db, settings)
    rows: list[list[InlineKeyboardButton]] = []
    proxy_link = build_proxy_subscription_link(settings, order)
    short_link = build_short_subscription_link(settings, order)

    if proxy_link:
        rows.append([InlineKeyboardButton(text=ui["button_open_subscription"], url=proxy_link)])
    if short_link and short_link != proxy_link:
        rows.append([InlineKeyboardButton(text=ui["button_short_link"], url=short_link)])

    support_url = support_contact_url(db.get_settings().get("support_contact", settings.support_contact))
    if support_url:
        rows.append([InlineKeyboardButton(text=ui["button_write_support"], url=support_url)])

    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_delivery_message(
    bot: Bot,
    settings: Settings,
    db: Database,
    telegram_id: int,
    order: sqlite3.Row,
) -> None:
    ui = get_interface_settings(db, settings)
    short_link = build_short_subscription_link(settings, order)
    proxy_link = build_proxy_subscription_link(settings, order)

    full_subscription_url = None
    if order["xui_subscription_url"]:
        full_subscription_url = rewrite_local_url(
            settings.public_base_url,
            str(order["xui_subscription_url"]),
        )

    delivery_image_path = resolve_asset_path(ui.get("delivery_image_path"))
    if delivery_image_path and delivery_image_path.exists():
        await bot.send_photo(telegram_id, FSInputFile(str(delivery_image_path)))

    lines = [
        f"✅ {ui['brand_name']}",
        "",
        ui["delivery_text"],
        "",
        f"Заказ: #{order['public_id']}",
        f"Тариф: {order['plan_name']}",
    ]
    if proxy_link:
        lines.extend(["", f"Ссылка для обновления: {proxy_link}"])
    if short_link and short_link != proxy_link:
        lines.append(f"Короткая ссылка: {short_link}")
    if full_subscription_url and full_subscription_url not in {proxy_link, short_link}:
        lines.append(f"Резервная ссылка: {full_subscription_url}")

    await bot.send_message(
        telegram_id,
        "\n".join(lines),
        reply_markup=delivery_keyboard(db, settings, order),
    )

    qr_payload = proxy_link or short_link or full_subscription_url
    if qr_payload:
        png = build_qr_png(str(qr_payload))
        await bot.send_photo(
            telegram_id,
            BufferedInputFile(png, filename=f"vpn-{order['public_id']}.png"),
            caption="QR-код для быстрого подключения.",
        )


def template_context(**kwargs: Any) -> dict[str, Any]:
    return kwargs
