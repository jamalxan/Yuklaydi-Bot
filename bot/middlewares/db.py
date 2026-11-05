from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from bot.db.repo import DownloadRepo, SongCacheRepo, UserRepo, guess_lang
from bot.db.session import async_session_factory
from bot.services.cache import forget_user_seen, mark_user_seen

logger = logging.getLogger("yuklaydi.db_mw")


class DbMiddleware(BaseMiddleware):
    """Open one DB session per handled event and register the user.

    Every user is upserted on first contact (then at most once per hour, gated
    by a Redis ``useen:`` marker). Previously users only reached the DB after
    pressing a language button, so stats, broadcast and ban silently skipped
    everyone who just sent a link.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with async_session_factory() as session:
            user_repo = UserRepo(session)
            data["session"] = session
            data["user_repo"] = user_repo
            data["download_repo"] = DownloadRepo(session)
            data["song_repo"] = SongCacheRepo(session)

            tg_user: Optional[User] = data.get("event_from_user")
            if tg_user is not None and not tg_user.is_bot:
                await register_user(user_repo, tg_user)
            return await handler(event, data)


async def register_user(user_repo: UserRepo, tg_user: User) -> None:
    try:
        first_sighting = await mark_user_seen(tg_user.id)
    except Exception:
        first_sighting = True  # Redis down: upsert anyway (idempotent)
    if not first_sighting:
        return
    try:
        await user_repo.upsert_seen(
            tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            lang=guess_lang(tg_user.language_code),
        )
    except Exception as e:
        logger.warning(f"User upsert failed for {tg_user.id}: {e}")
        try:
            await user_repo.session.rollback()
            await forget_user_seen(tg_user.id)  # retry on the next update
        except Exception:
            pass
