from __future__ import annotations

from typing import Optional

from aiogram import Router
from aiogram.types import ErrorEvent, InaccessibleMessage, Message

from bot.logger import logger
from bot.middlewares.i18n import resolve_lang
from bot.texts import T, t

router = Router()

# Backwards-compatible view (tests import this).
GENERIC_ERRORS = T["error.generic"]


@router.error()
async def global_error_handler(event: ErrorEvent):
    logger.exception(f"Unhandled error: {event.exception}", exc_info=event.exception)
    try:
        update = event.update
        msg: Message | InaccessibleMessage | None = None
        user_id: Optional[int] = None

        if update.message:
            msg = update.message
            user_id = msg.from_user.id if msg.from_user else None
        elif update.callback_query:
            msg = update.callback_query.message
            user_id = update.callback_query.from_user.id

        if isinstance(msg, Message):
            # Redis → DB-less lookup; resolve_lang never raises (default_lang on error)
            lang = await resolve_lang(user_id)
            await msg.answer(t("error.generic", lang))
    except Exception as e:
        logger.error(f"Error in error handler: {e}")
