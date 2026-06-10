from __future__ import annotations

import asyncio
import os
from pathlib import Path

from aiogram import Bot


def load_env_file(path: str) -> None:
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


load_env_file("/opt/vpn-sales-bot/.env")

from app.config import load_settings
from app.db import Database
from app.services import send_delivery_message


async def main() -> None:
    settings = load_settings()
    db = Database(settings.db_path)
    bot = Bot(settings.bot_token)
    try:
        orders = [
            row
            for row in db.list_orders()
            if str(row["status"]) == "delivered" and row["xui_subscription_url"]
        ]
        print(f"delivered_with_subscription={len(orders)}")
        for order in orders:
            await send_delivery_message(bot, settings, int(order["telegram_id"]), order)
            print(f"resent order={order['public_id']} telegram_id={order['telegram_id']}")
    finally:
        await bot.session.close()


asyncio.run(main())
