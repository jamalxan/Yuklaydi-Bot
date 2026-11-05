from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from bot.config import settings
from bot.services.cache import get_user_lang, set_user_lang

logger = logging.getLogger("yuklaydi.i18n")


async def resolve_lang(user_id: Optional[int], user_repo=None) -> str:
    """Redis → DB → default. Never raises; a DB hit re-fills the Redis cache."""
    if not user_id:
        return settings.default_lang
    try:
        cached = await get_user_lang(user_id)
        if cached:
            return cached
    except Exception as e:
        logger.warning(f"Lang cache read failed: {e}")
    if user_repo is not None:
        try:
            user = await user_repo.get(user_id)
            if user and user.lang:
                try:
                    await set_user_lang(user_id, user.lang)
                except Exception:
                    pass
                return user.lang
        except Exception as e:
            logger.warning(f"Lang DB read failed: {e}")
    return settings.default_lang


class UserLangMiddleware(BaseMiddleware):
    """Attach user_lang to handler data from Redis cache (fast path) or DB."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user: Optional[User] = data.get("event_from_user") or getattr(event, "from_user", None)
        data["user_lang"] = await resolve_lang(user.id if user else None, data.get("user_repo"))
        return await handler(event, data)
