from __future__ import annotations

import math
import re
from dataclasses import asdict

from aiogram import Router
from aiogram.enums import ContentType
from aiogram.types import CallbackQuery, Message

from bot.keyboards.inline import audio_result_keyboard, music_search_keyboard
from bot.logger import logger
from bot.services.cache import (
    get_audio_file_id, get_music_search_by_hash, set_music_search, store_song_meta,
)
from bot.services.music_search import SongEntry, search_songs

router = Router()

_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

_MSGS = {
    "searching":    {"uz": "🔍 Qidirilmoqda...",                    "ru": "🔍 Поиск...",                        "en": "🔍 Searching..."},
    "not_found":    {"uz": "😔 Hech narsa topilmadi. Boshqacha yozing.", "ru": "😔 Ничего не найдено. Попробуйте иначе.", "en": "😔 Nothing found. Try a different spelling."},
    "downloading":  {"uz": "⏳ Yuklanmoqda...",                      "ru": "⏳ Скачивается...",                   "en": "⏳ Downloading..."},
    "error":        {"uz": "❌ Xatolik. Keyinroq urinib ko'ring.",    "ru": "❌ Ошибка. Попробуйте позже.",        "en": "❌ Error. Please try again later."},
    "expired":      {"uz": "Sessiya tugagan. Qaytadan qidiring.",     "ru": "Сессия устарела. Повторите поиск.",   "en": "Session expired. Please search again."},
    "results":      {"uz": "🎵 <b>Natijalar:</b>",                   "ru": "🎵 <b>Результаты:</b>",               "en": "🎵 <b>Results:</b>"},
    "hint":         {
        "uz": "📎 Havola yuboring (video yuklash) yoki qo'shiq nomini yozing (qidirish) 🎵",
        "ru": "📎 Отправьте ссылку (видео) или напишите название песни (поиск) 🎵",
        "en": "📎 Send a link (download video) or type a song name (search) 🎵",
    },
    "unsupported":  {
        "uz": "📎 Havola → video, matn → qidiruv, ovozli xabar → qo'shiq aniqlash 🎵",
        "ru": "📎 Ссылка → видео, текст → поиск, голосовое → распознать 🎵",
        "en": "📎 Link → video, text → search, voice → recognize 🎵",
    },
}

_HANDLED_TYPES = {
    ContentType.TEXT, ContentType.VOICE, ContentType.AUDIO,
    ContentType.VIDEO, ContentType.VIDEO_NOTE, ContentType.DOCUMENT,
}


@router.message(lambda m: bool(m.text and not m.text.startswith("/") and not _URL_RE.search(m.text)))
async def handle_music_search(message: Message, user_lang: str):
    query = message.text.strip()
    if not query:
        return
    lang = user_lang or "en"

    await message.bot.send_chat_action(message.chat.id, "typing")
    progress_msg = await message.answer(_MSGS["searching"].get(lang, "🔍 Searching..."))

    entries = await search_songs(query, max_results=30)
    if not entries:
        await progress_msg.edit_text(_MSGS["not_found"].get(lang, "😔 Nothing found."))
        return

    # De-duplicate by (title, uploader) similarity
    seen: set[str] = set()
    unique: list[SongEntry] = []
    for e in entries:
        key = f"{e.title.lower()[:30]}|{e.uploader.lower()[:20]}"
        if key not in seen:
            seen.add(key)
            unique.append(e)

    qhash = await set_music_search(query, [asdict(e) for e in unique])
    total_pages = max(1, math.ceil(len(unique) / 10))
    text, kb = music_search_keyboard(qhash, unique, page=0, total_pages=total_pages, lang=lang)
    header = _MSGS["results"].get(lang, "🎵 <b>Results:</b>")
    await progress_msg.edit_text(f"{header}\n\n{text}", parse_mode="HTML", reply_markup=kb)

    try:
        from bot.db.session import async_session_factory
        from bot.db.repo import MusicSearchRepo, UserRepo
        async with async_session_factory() as session:
            repo = MusicSearchRepo(session)
            await repo.log(message.from_user.id, query)
            await repo.inc_daily()
        async with async_session_factory() as session:
            await UserRepo(session).inc_searches(message.from_user.id)
    except Exception as e:
        logger.warning(f"Music search DB log: {e}")


@router.callback_query(lambda c: c.data and c.data.startswith("msp:"))
async def cb_music_page(callback: CallbackQuery, user_lang: str):
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return

    try:
        qhash, page = parts[1], int(parts[2])
    except ValueError:
        await callback.answer()
        return
    lang = user_lang or "en"

    raw = await get_music_search_by_hash(qhash)
    if not raw:
        await callback.answer(_MSGS["expired"].get(lang, "Expired"), show_alert=True)
        return

    entries = [SongEntry(**e) for e in raw]
    total_pages = max(1, math.ceil(len(entries) / 10))
    page = max(0, min(page, total_pages - 1))

    text, kb = music_search_keyboard(qhash, entries, page=page, total_pages=total_pages, lang=lang)
    header = _MSGS["results"].get(lang, "🎵 <b>Results:</b>")
    try:
        await callback.message.edit_text(f"{header}\n\n{text}", parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("ms:"))
async def cb_music_pick(callback: CallbackQuery, user_lang: str):
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return

    try:
        qhash, idx = parts[1], int(parts[2])
    except ValueError:
        await callback.answer()
        return
    lang = user_lang or "en"

    raw = await get_music_search_by_hash(qhash)
    if not raw:
        await callback.answer(_MSGS["expired"].get(lang, "Expired"), show_alert=True)
        return

    entries = [SongEntry(**e) for e in raw]
    if not 0 <= idx < len(entries):
        await callback.answer("Invalid", show_alert=True)
        return

    entry = entries[idx]
    await callback.answer()

    cached_id = await get_audio_file_id(entry.id)
    if cached_id:
        song_key = await store_song_meta(entry.title, entry.uploader)
        kb = audio_result_keyboard(song_key, lang)
        await callback.message.answer_audio(cached_id, reply_markup=kb)
        return

    await callback.message.bot.send_chat_action(callback.message.chat.id, "upload_voice")
    progress_msg = await callback.message.answer(_MSGS["downloading"].get(lang, "⏳ Downloading..."))

    try:
        from bot.services.queue import get_arq_pool
        pool = await get_arq_pool()
        await pool.enqueue_job(
            "music_download_task",
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id,
            message_id=progress_msg.message_id,
            video_id=entry.id,
            url=entry.url,
            title=entry.title,
            artist=entry.uploader,
            duration=entry.duration,
            thumbnail=entry.thumbnail,
            user_lang=lang,
            _queue_name="arq:queue:music",
        )
    except Exception as e:
        logger.error(f"Failed to enqueue music_download: {e}")
        await progress_msg.edit_text(_MSGS["error"].get(lang, "❌ Error."))

    try:
        from bot.db.session import async_session_factory
        from bot.db.repo import MusicSearchRepo
        async with async_session_factory() as session:
            await MusicSearchRepo(session).log(callback.from_user.id, f"{entry.uploader} {entry.title}", picked_index=idx)
    except Exception as e:
        logger.warning(f"Music pick DB log: {e}")


@router.message(lambda m: m.content_type not in _HANDLED_TYPES)
async def handle_unsupported(message: Message, user_lang: str):
    lang = user_lang or "en"
    await message.answer(_MSGS["unsupported"].get(lang, _MSGS["unsupported"]["en"]))
