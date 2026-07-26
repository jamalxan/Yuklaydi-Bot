from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, BotCommandScopeDefault

from bot.config import settings
from bot.db.migrations import run_migrations
from bot.db.models import Base
from bot.db.session import engine
from bot.handlers import admin, download, errors, shazam, start
from bot.handlers import music as music_handler
from bot.logger import setup_logging
from bot.middlewares.channel import ChannelCheckMiddleware
from bot.middlewares.db import DbMiddleware
from bot.middlewares.i18n import UserLangMiddleware
from bot.middlewares.throttling import ThrottlingMiddleware
from bot.services.cache import ping_redis

logger = logging.getLogger("yuklaydi.main")

_COMMANDS = {
    "uz": [
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="lang", description="Tilni o'zgartirish 🌐"),
        BotCommand(command="help", description="Yordam 📖"),
    ],
    "ru": [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="lang", description="Сменить язык 🌐"),
        BotCommand(command="help", description="Помощь 📖"),
    ],
    "en": [
        BotCommand(command="start", description="Start the bot"),
        BotCommand(command="lang", description="Change language 🌐"),
        BotCommand(command="help", description="Help 📖"),
    ],
}


def make_bot() -> Bot:
    if settings.use_local_api:
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.local_api_url, is_local=True))
        logger.info(f"Using Local Bot API: {settings.local_api_url}")
    else:
        session = AiohttpSession()
        logger.info("Using Telegram cloud API (50MB file limit)")

    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def on_startup(bot: Bot):
    me = await bot.get_me()
    if settings.use_local_api:
        logger.info(f"Local API: ON ({settings.local_api_url}) — limit {settings.max_file_mb}MB")
    else:
        logger.warning("Local API: OFF — cloud API 50MB limit in effect")
    logger.info(f"Bot started: @{me.username} (id={me.id})")

    redis_ok = await ping_redis()
    logger.info(f"Redis: {'OK' if redis_ok else 'FAILED'}")

    os.makedirs(settings.download_dir, exist_ok=True)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await run_migrations(engine)
        logger.info("Database ready")
    except Exception as e:
        logger.error(f"DB setup failed: {e}")

    # Register bot commands (default / all languages)
    try:
        await bot.set_my_commands(_COMMANDS["en"], scope=BotCommandScopeDefault())
        logger.info("Bot commands registered")
    except Exception as e:
        logger.warning(f"set_my_commands failed: {e}")


async def on_shutdown(bot: Bot):
    logger.info("Shutting down...")
    from bot.services.queue import close_arq_pool
    await close_arq_pool()
    await bot.session.close()
    await engine.dispose()


async def main():
    setup_logging()

    bot = make_bot()
    storage = RedisStorage.from_url(settings.redis_url)
    dp = Dispatcher(storage=storage)

    # Middlewares (order matters)
    # Throttling FIRST — spam is rejected with one Redis round-trip before any
    # DB session or user lookup happens (critical under request floods).
    dp.message.middleware(ThrottlingMiddleware())
    dp.message.middleware(DbMiddleware())
    dp.callback_query.middleware(DbMiddleware())
    dp.message.middleware(UserLangMiddleware())
    dp.callback_query.middleware(UserLangMiddleware())
    dp.message.middleware(ChannelCheckMiddleware())

    # Routers — order determines handler priority
    dp.include_router(errors.router)
    dp.include_router(start.router)       # Commands: /start /lang /help
    dp.include_router(admin.router)       # Commands: /admin /statistics + FSM
    dp.include_router(download.router)    # URL messages + vdalt/vshz callbacks
    dp.include_router(shazam.router)      # Media messages + lyrics/dlsong callbacks
    dp.include_router(music_handler.router)  # Plain text search + unsupported

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info("Starting polling...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
