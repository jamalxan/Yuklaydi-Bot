from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import settings
from bot.db.repo import SettingRepo
from bot.db.session import async_session_factory
from bot.services.cache import (
    get_cached_membership, get_setting, set_cached_membership, set_setting_cache,
)
from bot.texts import T, t
from bot.utils.text import esc

logger = logging.getLogger("yuklaydi.channel")

# Commands that always work, compared EXACTLY (``/startxyz`` is not ``/start``).
_SKIP_COMMANDS = {"/start", "/admin", "/lang", "/help", "/statistics"}

# Commands that still work for non-subscribers, but are followed by the join
# prompt: /start is the first thing a new user sends, and if it says nothing
# about the channel the wall looks broken until they paste a link.
_PROMPT_AFTER_COMMANDS = {"/start"}

# Callbacks that must work for non-subscribers: the subscription check itself,
# the language picker and the admin panel.
_SKIP_CALLBACK_PREFIXES = ("channel:check", "lang:", "adm:")

# Backwards-compatible views of the localized strings (tests import these).
_JOIN_MSGS = T["channel.join"]
_JOIN_BTN = T["channel.join_btn"]
_CHECK_BTN = T["channel.check_btn"]


def command_of(text: Optional[str]) -> Optional[str]:
    """'/start@MyBot payload' → '/start'; None for non-commands."""
    if not text or not text.startswith("/"):
        return None
    return text.split()[0].split("@")[0].lower()


async def _get_channel_id() -> str:
    cached = await get_setting("required_channel_id")
    if cached is not None:
        return cached
    try:
        async with async_session_factory() as session:
            val = await SettingRepo(session).get("required_channel_id", "")
        await set_setting_cache("required_channel_id", val)
        return val
    except Exception:
        return ""


class ChannelCheckMiddleware(BaseMiddleware):
    """Require a channel subscription for messages AND button presses."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Admins are never walled: their FSM steps (broadcast text, ban ID,
        # channel ID, welcome text) are plain messages, and an admin who is
        # not a member of the channel would otherwise be locked out of the
        # panel — including while setting the channel for the first time.
        from_user = getattr(event, "from_user", None)
        if from_user is not None and from_user.id in settings.admin_ids:
            return await handler(event, data)

        is_callback = isinstance(event, CallbackQuery)
        if is_callback:
            if (event.data or "").startswith(_SKIP_CALLBACK_PREFIXES):
                return await handler(event, data)
        else:
            cmd = command_of(getattr(event, "text", None))
            if cmd in _PROMPT_AFTER_COMMANDS:
                result = await handler(event, data)
                if await self._is_member(event) is False:
                    await self._send_join_prompt(event, data.get("user_lang", "uz"))
                return result
            if cmd in _SKIP_COMMANDS:
                return await handler(event, data)

        if await self._is_member(event) is not False:
            return await handler(event, data)

        lang = data.get("user_lang", "uz")
        if is_callback:
            try:
                await event.answer(t("channel.join", lang), show_alert=True)
            except Exception:
                pass
            target: Optional[Message] = event.message if isinstance(event.message, Message) else None
            if target is None:
                return None
        else:
            target = event
        await self._send_join_prompt(target, lang)
        return None  # don't call the handler — stop processing

    @staticmethod
    async def _is_member(event: TelegramObject) -> Optional[bool]:
        """True/False, or None when there is nothing to enforce (no channel
        configured, no user) or the check failed — callers fail open on None."""
        try:
            ch_id_str = await _get_channel_id()
        except Exception:
            ch_id_str = ""
        if not ch_id_str:
            return None

        user_id = event.from_user.id if getattr(event, "from_user", None) else None
        if not user_id:
            return None

        # Redis-cached membership — get_chat_member is a Telegram API round-trip
        # and must NOT run on every message at high load. Cache errors fall
        # back to the live API check.
        try:
            cached = await get_cached_membership(user_id)
        except Exception:
            cached = None
        if cached is not None:
            return cached

        try:
            member = await event.bot.get_chat_member(int(ch_id_str), user_id)
            is_member = member.status in ("member", "administrator", "creator")
        except Exception as e:
            # Fail open: a misconfigured channel must not lock every user
            # out. The admin panel verifies the bot is a channel admin
            # before saving, so this only fires on transient API errors.
            logger.warning(f"get_chat_member failed for channel {ch_id_str}: {e}")
            return None
        try:
            await set_cached_membership(user_id, is_member)
        except Exception:
            pass
        return is_member

    @staticmethod
    async def _send_join_prompt(target: Message, lang: str) -> None:
        try:
            async with async_session_factory() as session:
                repo = SettingRepo(session)
                ch_url = await repo.get("required_channel_url", "")
                ch_title = await repo.get("required_channel_title", "")
        except Exception:
            ch_url, ch_title = "", ""

        builder = InlineKeyboardBuilder()
        if ch_url:
            builder.row(InlineKeyboardButton(text=t("channel.join_btn", lang), url=ch_url))
        builder.row(InlineKeyboardButton(text=t("channel.check_btn", lang), callback_data="channel:check"))

        text = t("channel.join", lang) + (f"\n\n<b>{esc(ch_title)}</b>" if ch_title else "")
        await target.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())
