from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from bot.config import settings
from bot.services.cache import is_banned

logger = logging.getLogger("yuklaydi.ban")


class BanMiddleware(BaseMiddleware):
    """Silently drop every update from a banned user (outer middleware on dp.update).

    Runs before routing, so a banned user's messages, media and button presses
    never reach a handler and never enqueue a job. The check is one Redis
    SISMEMBER; the set mirrors users.is_blocked (loaded at startup, updated by
    the admin ban/unban action). Admin IDs are never blocked.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user: Optional[User] = data.get("event_from_user")
        if user is not None and user.id not in settings.admin_ids:
            try:
                if await is_banned(user.id):
                    return None
            except Exception as e:  # Redis down → fail open, never block everyone
                logger.warning(f"Ban check skipped (Redis error): {e}")
        return await handler(event, data)
