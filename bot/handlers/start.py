from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.db.repo import SUPPORTED_LANGS, UserRepo, WelcomeRepo
from bot.keyboards.inline import lang_keyboard
from bot.services.cache import set_user_lang
from bot.texts import DEFAULT_WELCOME, T, t

logger = logging.getLogger("yuklaydi.start")

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)

HELP_TEXTS = T["help"]
_DEFAULT_WELCOME = DEFAULT_WELCOME


def _render(template: str, first_name: str, username: str) -> str:
    return (
        template
        .replace("{first_name}", html.escape(first_name or ""))
        .replace("{username}", html.escape(username or ""))
    )


async def _get_welcome(lang: str) -> str:
    try:
        from bot.db.session import async_session_factory
        async with async_session_factory() as session:
            text = await WelcomeRepo(session).get(lang)
            if text:
                return text
    except Exception:
        pass
    return _DEFAULT_WELCOME.get(lang, _DEFAULT_WELCOME["en"])


async def send_welcome(message: Message, lang: str, first_name: str, username: str) -> None:
    """Send the (admin-editable) welcome; fall back to the default if it is broken.

    A custom welcome with invalid HTML would otherwise make /start fail for
    every single user.
    """
    template = await _get_welcome(lang)
    try:
        await message.answer(_render(template, first_name, username), parse_mode="HTML")
    except TelegramBadRequest as e:
        logger.error(f"Custom welcome for '{lang}' is invalid, using default: {e}")
        default = _DEFAULT_WELCOME.get(lang, _DEFAULT_WELCOME["en"])
        await message.answer(_render(default, first_name, username), parse_mode="HTML")


@router.message(Command("start"))
async def cmd_start(message: Message, user_repo: UserRepo, user_lang: str):
    user = await user_repo.get(message.from_user.id)
    if user is None or not user.lang_chosen:
        await message.answer(t("start.choose_lang", user_lang), reply_markup=lang_keyboard())
        return
    await send_welcome(message, user.lang, message.from_user.first_name or "", message.from_user.username or "")


@router.callback_query(F.data.startswith("lang:"))
async def cb_lang_select(callback: CallbackQuery, user_repo: UserRepo):
    lang = callback.data.split(":", 1)[1]
    if lang not in SUPPORTED_LANGS:
        await callback.answer()
        return

    await user_repo.upsert_seen(
        callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        lang=lang,
    )
    await user_repo.set_lang(callback.from_user.id, lang)
    await set_user_lang(callback.from_user.id, lang)

    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(t("start.lang_saved", lang))
        except TelegramBadRequest:
            pass
        await send_welcome(callback.message, lang,
                           callback.from_user.first_name or "", callback.from_user.username or "")
    await callback.answer()


@router.message(Command("lang"))
async def cmd_lang(message: Message, user_lang: str):
    await message.answer(t("start.lang_prompt", user_lang), reply_markup=lang_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message, user_lang: str):
    await message.answer(t("help", user_lang), parse_mode="HTML")
