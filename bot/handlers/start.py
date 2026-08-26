from __future__ import annotations

import html

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.db.repo import UserRepo, WelcomeRepo
from bot.keyboards.inline import lang_keyboard
from bot.services.cache import set_user_lang

router = Router()

HELP_TEXTS = {
    "uz": (
        "ℹ️ <b>Yuklaydi Bot</b>\n\n"
        "📥 <b>Video yuklash:</b>\n"
        "YouTube, TikTok, Instagram, Facebook, Twitter/X va boshqa saytlardan havola yuboring. "
        "Bir xabarda bir nechta havola yuborsangiz ham — hammasini alohida yuklab beramiz.\n\n"
        "🎵 <b>Qo'shiq topish:</b>\n"
        "Ovozli xabar, video yoki audio faylni yuboring — qo'shiq aniqlanadi.\n\n"
        "🔍 <b>Musiqa qidirish:</b>\n"
        "Qo'shiq nomini yozing — MP3 yuboramiz.\n\n"
        "<i>⚠️ Kontent shaxsiy foydalanish uchun. Platforma shartlariga rioya qiling.</i>"
    ),
    "ru": (
        "ℹ️ <b>Yuklaydi Bot</b>\n\n"
        "📥 <b>Скачать видео:</b>\n"
        "Отправьте ссылку на YouTube, TikTok, Instagram, Facebook, Twitter/X и другие. "
        "Можно отправить сразу несколько ссылок одним сообщением — обработаем каждую.\n\n"
        "🎵 <b>Найти песню:</b>\n"
        "Отправьте голосовое, видео или аудио — определим песню.\n\n"
        "🔍 <b>Поиск музыки:</b>\n"
        "Напишите название песни — пришлём MP3.\n\n"
        "<i>⚠️ Контент для личного использования. Соблюдайте условия платформ.</i>"
    ),
    "en": (
        "ℹ️ <b>Yuklaydi Bot</b>\n\n"
        "📥 <b>Download Video:</b>\n"
        "Send a link from YouTube, TikTok, Instagram, Facebook, Twitter/X and more. "
        "You can send several links in one message — each comes back separately.\n\n"
        "🎵 <b>Find Song:</b>\n"
        "Send a voice message, video, or audio — we'll identify the song.\n\n"
        "🔍 <b>Music Search:</b>\n"
        "Type a song name — we'll send the MP3.\n\n"
        "<i>⚠️ Content is for personal use. Respect platform terms of service.</i>"
    ),
}

_DEFAULT_WELCOME = {
    "uz": (
        "👋 Salom, <b>{first_name}</b>!\n\n"
        "🎬 Havola yuboring — video yuklab beraman.\n"
        "🎵 Qo'shiq nomi yozing — MP3 yuboraman.\n"
        "🎤 Ovozli xabar yuboring — qo'shiqni topib beraman.\n\n"
        "<i>🤖 @vidyuklaydi_bot</i>"
    ),
    "ru": (
        "👋 Привет, <b>{first_name}</b>!\n\n"
        "🎬 Отправьте ссылку — скачаю видео.\n"
        "🎵 Напишите название песни — пришлю MP3.\n"
        "🎤 Отправьте голосовое — найду песню.\n\n"
        "<i>🤖 @vidyuklaydi_bot</i>"
    ),
    "en": (
        "👋 Hello, <b>{first_name}</b>!\n\n"
        "🎬 Send a link — I'll download the video.\n"
        "🎵 Type a song name — I'll send the MP3.\n"
        "🎤 Send a voice message — I'll find the song.\n\n"
        "<i>🤖 @vidyuklaydi_bot</i>"
    ),
}


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


@router.message(Command("start"))
async def cmd_start(message: Message, user_repo: UserRepo, user_lang: str):
    user = await user_repo.get(message.from_user.id)
    if user is None:
        await message.answer(
            "👋 Assalomu alaykum! / Добро пожаловать! / Welcome!\n\nPlease choose your language:",
            reply_markup=lang_keyboard(),
        )
    else:
        lang = user.lang
        template = await _get_welcome(lang)
        text = _render(template, message.from_user.first_name or "", message.from_user.username or "")
        await message.answer(text, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("lang:"))
async def cb_lang_select(callback: CallbackQuery, user_repo: UserRepo):
    lang = callback.data.split(":")[1]
    if lang not in ("uz", "ru", "en"):
        await callback.answer()
        return

    await user_repo.get_or_create(
        callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        lang=lang,
    )
    await user_repo.set_lang(callback.from_user.id, lang)
    await set_user_lang(callback.from_user.id, lang)

    saved = {"uz": "✅ Til saqlandi!", "ru": "✅ Язык сохранён!", "en": "✅ Language saved!"}
    await callback.message.edit_text(saved.get(lang, "✅ Saved!"))

    template = await _get_welcome(lang)
    text = _render(template, callback.from_user.first_name or "", callback.from_user.username or "")
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@router.message(Command("lang"))
async def cmd_lang(message: Message, user_lang: str):
    prompts = {"uz": "🌐 Tilni tanlang:", "ru": "🌐 Выберите язык:", "en": "🌐 Select language:"}
    await message.answer(prompts.get(user_lang, "🌐 Select language:"), reply_markup=lang_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message, user_lang: str):
    await message.answer(HELP_TEXTS.get(user_lang, HELP_TEXTS["en"]), parse_mode="HTML")
