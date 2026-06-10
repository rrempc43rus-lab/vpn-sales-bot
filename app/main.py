from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from aiogram import Bot
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from .bot import build_dispatcher
from .config import load_settings
from .db import Database
from .platega import PlategaClient
from .web import build_router, mount_static
from .xui import XuiClient


settings = load_settings()
db = Database(settings.db_path)
db.init(settings)
bot = Bot(settings.bot_token)
xui = XuiClient(settings)
platega = PlategaClient(settings)
dispatcher = build_dispatcher(settings, db, bot, xui, platega)
polling_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global polling_task
    polling_task = asyncio.create_task(dispatcher.start_polling(bot))
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await dispatcher.stop_polling()
        if polling_task is not None:
            polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await polling_task
        await platega.close()
        await xui.close()
        await bot.session.close()


app = FastAPI(title="VPN Sales Bot", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    same_site="lax",
    https_only=False,
)
mount_static(app)
app.include_router(build_router(settings=settings, db=db, bot=bot, xui=xui, platega=platega))
