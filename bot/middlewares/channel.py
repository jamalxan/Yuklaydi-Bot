from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.db.repo import SettingRepo
from bot.db.session import async_session_factory
from bot.services.cache import (
    get_cached_membership, get_setting, set_cached_membership, set_setting_cache,
)


_SKIP_COMMANDS = {"/start", "/admin", "/lang", "/help", "/statistics"}

_JOIN_MSGS = {
    "uz": "📢 Botdan foydalanish uchun kanalga obuna bo'ling:",
    "ru": "📢 Подпишитесь на канал, чтобы пользоваться ботом:",
    "en": "📢 Subscribe to the channel to use the bot:",
}
_JOIN_BTN = {"uz": "📢 Kanalga o'tish", "ru": "📢 Перейти в канал", "en": "📢 Join channel"}
_CHECK_BTN = {"uz": "✅ Obuna bo'ldim", "ru": "✅ Я подписался", "en": "✅ I subscribed"}


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
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        # Skip command messages that should always work
        if event.text and any(event.text.startswith(cmd) for cmd in _SKIP_COMMANDS):
            return await handler(event, data)

        ch_id_str = await _get_channel_id()
        if not ch_id_str:
            return await handler(event, data)

        user_id = event.from_user.id if event.from_user else None
        if not user_id:
            return await handler(event, data)

        # Redis-cached membership — get_chat_member is a Telegram API round-trip
        # and must NOT run on every message at high load. Cache errors fail
        # open to the live API check.
        try:
            cached = await get_cached_membership(user_id)
        except Exception:
            cached = None
        if cached is True:
            return await handler(event, data)

        if cached is None:
            try:
                member = await event.bot.get_chat_member(int(ch_id_str), user_id)
                is_member = member.status in ("member", "administrator", "creator")
            except Exception:
                return await handler(event, data)
            try:
                await set_cached_membership(user_id, is_member)
            except Exception:
                pass
            if is_member:
                return await handler(event, data)

        # User not subscribed — send join prompt
        lang = data.get("user_lang", "uz")
        try:
            async with async_session_factory() as session:
                repo = SettingRepo(session)
                ch_url = await repo.get("required_channel_url", "")
                ch_title = await repo.get("required_channel_title", "")
        except Exception:
            ch_url, ch_title = "", ""

        builder = InlineKeyboardBuilder()
        if ch_url:
            builder.row(InlineKeyboardButton(
                text=_JOIN_BTN.get(lang, _JOIN_BTN["en"]),
                url=ch_url,
            ))
        builder.row(InlineKeyboardButton(
            text=_CHECK_BTN.get(lang, _CHECK_BTN["en"]),
            callback_data="channel:check",
        ))

        await event.answer(
            _JOIN_MSGS.get(lang, _JOIN_MSGS["en"]) + (f"\n\n<b>{ch_title}</b>" if ch_title else ""),
            parse_mode="HTML",
            reply_markup=builder.as_markup(),
        )
        # Don't call handler — stop processing
