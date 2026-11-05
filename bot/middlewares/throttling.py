from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.config import settings
from bot.services.cache import check_callback_rate, check_rate_limit, get_user_lang
from bot.texts import t

logger = logging.getLogger("yuklaydi.throttle")


async def _lang(user_id: int, data: Dict[str, Any]) -> str:
    """Throttling runs before UserLangMiddleware — read the language from Redis."""
    if data.get("user_lang"):
        return data["user_lang"]
    try:
        return await get_user_lang(user_id) or settings.default_lang
    except Exception:
        return settings.default_lang


class ThrottlingMiddleware(BaseMiddleware):
    """Per-user rate limit for messages AND button presses.

    Kept first in the chain so floods are rejected with one Redis round-trip
    before any DB session opens. Redis errors fail open (allow) — an outage
    must not turn every update into an error.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.id in settings.admin_ids:
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            try:
                allowed, reason = await check_callback_rate(user.id, event.data or "")
            except Exception as e:
                logger.warning(f"Callback rate check skipped (Redis error): {e}")
                return await handler(event, data)
            if allowed:
                return await handler(event, data)
            try:
                if reason == "rate":
                    await event.answer(t("throttle.callback", await _lang(user.id, data)))
                else:
                    await event.answer()  # duplicate tap: just stop the spinner
            except Exception:
                pass
            return None

        if isinstance(event, Message):
            try:
                allowed, remaining = await check_rate_limit(user.id)
            except Exception as e:
                logger.warning(f"Rate limit check skipped (Redis error): {e}")
                return await handler(event, data)
            if not allowed:
                # In a group the notice would be noise for everyone else.
                if getattr(getattr(event, "chat", None), "type", ChatType.PRIVATE) == ChatType.PRIVATE:
                    await event.answer(t("throttle", await _lang(user.id, data), sec=remaining))
                return None
        return await handler(event, data)
