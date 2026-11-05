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
from bot.db.migrations import upgrade_database
from bot.db.session import engine
from bot.handlers import admin, download, errors, shazam, start
from bot.handlers import music as music_handler
from bot.logger import setup_logging
from bot.middlewares.ban import BanMiddleware
from bot.middlewares.channel import ChannelCheckMiddleware
from bot.middlewares.db import DbMiddleware
from bot.middlewares.i18n import UserLangMiddleware
from bot.middlewares.throttling import ThrottlingMiddleware
from bot.services.cache import load_banned, ping_redis
from bot.texts import LANGS, t

logger = logging.getLogger("yuklaydi.main")


def _commands(lang: str) -> list[BotCommand]:
    return [
        BotCommand(command="start", description=t("cmd.start", lang)),
        BotCommand(command="lang", description=t("cmd.lang", lang)),
        BotCommand(command="help", description=t("cmd.help", lang)),
    ]


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
        logger.warning(f"Local API: OFF — cloud API {settings.max_file_mb}MB limit in effect")
    logger.info(f"Bot started: @{me.username} (id={me.id})")

    redis_ok = await ping_redis()
    logger.info(f"Redis: {'OK' if redis_ok else 'FAILED'}")

    os.makedirs(settings.download_dir, exist_ok=True)

    # Schema is owned by Alembic. If the database can't be brought to "head"
    # the bot must NOT start half-working — raise, exit non-zero, let the
    # supervisor (docker restart policy / start_all.ps1) retry.
    await upgrade_database()
    logger.info("Database ready")

    # Ban list → Redis set used by BanMiddleware
    try:
        from bot.db.repo import UserRepo
        from bot.db.session import async_session_factory
        async with async_session_factory() as session:
            banned = await UserRepo(session).banned_ids()
        await load_banned(banned)
        logger.info(f"Ban list loaded: {len(banned)} user(s)")
    except Exception as e:
        logger.error(f"Ban list load failed: {e}")

    # Command menu per language (Telegram picks by the user's client language)
    try:
        await bot.set_my_commands(_commands(settings.default_lang), scope=BotCommandScopeDefault())
        for lang in LANGS:
            if lang == "oz":
                continue  # not an ISO 639-1 code: Telegram rejects it (Cyrillic users send "uz")
            await bot.set_my_commands(_commands(lang), scope=BotCommandScopeDefault(), language_code=lang)
        logger.info("Bot commands registered")
    except Exception as e:
        logger.warning(f"set_my_commands failed: {e}")


async def on_shutdown(bot: Bot):
    logger.info("Shutting down...")
    from bot.services.queue import close_arq_pool
    await close_arq_pool()
    from bot.services import yt_search
    await yt_search.close()
    await bot.session.close()
    await engine.dispose()


async def main():
    setup_logging()

    bot = make_bot()
    storage = RedisStorage.from_url(settings.redis_dsn)
    dp = Dispatcher(storage=storage)

    # Middlewares (order matters)
    # 1. Ban: outer middleware on every update — banned users never reach routing.
    dp.update.outer_middleware(BanMiddleware())
    # 2. Throttling FIRST among inner middlewares — floods are rejected with
    #    one Redis round-trip before any DB session or user lookup happens.
    #    It reads the user's language from Redis itself (BUG-13).
    for observer in (dp.message, dp.callback_query):
        observer.middleware(ThrottlingMiddleware())
        observer.middleware(DbMiddleware())          # session + user upsert
        observer.middleware(UserLangMiddleware())
        observer.middleware(ChannelCheckMiddleware())

    # Routers — order determines handler priority
    dp.include_router(errors.router)
    dp.include_router(start.router)       # Commands: /start /lang /help
    dp.include_router(start.group_router)  # /start in a group: one-line hint
    dp.include_router(admin.router)       # Admin-only: /admin /statistics + FSM
    dp.include_router(admin.user_router)  # channel:check (any user)
    dp.include_router(download.router)    # URL messages + vdalt/vshz callbacks
    dp.include_router(shazam.router)      # Media messages + lyrics/dlsong callbacks
    dp.include_router(music_handler.router)  # Plain text search + unsupported

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info("Starting polling...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
