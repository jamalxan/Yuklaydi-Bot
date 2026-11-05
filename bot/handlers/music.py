from __future__ import annotations

import asyncio
import math
import re
from dataclasses import asdict

from aiogram import F, Router
from aiogram.enums import ChatType, ContentType
from aiogram.types import CallbackQuery, Message

from bot.config import settings
from bot.handlers.download import MUSIC_QUEUE, queue_has_room, reserve_slot
from bot.keyboards.inline import audio_result_keyboard, music_search_keyboard
from bot.logger import logger
from bot.services.cache import (
    get_audio_file_id, get_music_search, get_music_search_by_hash,
    release_user_slot, set_music_search, store_song_meta,
)
from bot.services.music_search import SongEntry, normalize_query, search_songs
from bot.texts import T, t
from bot.utils.text import esc

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)

_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

# Backwards-compatible view of the localized strings (tests import this).
_MSGS = {k.split(".", 1)[1]: v for k, v in T.items() if k.startswith("music.")}

# Content types that have their own handler (see handle_unsupported).
_HANDLED_TYPES = {
    ContentType.TEXT, ContentType.VOICE, ContentType.AUDIO,
    ContentType.VIDEO, ContentType.VIDEO_NOTE, ContentType.DOCUMENT,
}

PAGE_SIZE = 10


def _dedupe(entries: list[SongEntry]) -> list[SongEntry]:
    """Drop near-duplicate results by (title, uploader) prefix."""
    seen: set[str] = set()
    unique: list[SongEntry] = []
    for e in entries:
        key = f"{e.title.lower()[:30]}|{e.uploader.lower()[:20]}"
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


@router.message(F.text, lambda m: not m.text.startswith("/") and not _URL_RE.search(m.text))
async def handle_music_search(message: Message, user_lang: str):
    lang = user_lang
    query = normalize_query(message.text)
    if query is None:  # "a", "!!", a lone emoji — not worth a YouTube search
        await message.answer(t("music.query_short", lang))
        return

    # No "searching..." placeholder: the list itself is the first answer
    # (one message instead of send + edit); "typing" covers the wait.
    typing = asyncio.ensure_future(message.bot.send_chat_action(message.chat.id, "typing"))
    typing.add_done_callback(lambda tk: tk.cancelled() or tk.exception())

    # Same query within MUSIC_SEARCH_TTL → no YouTube round-trip at all.
    cached = None
    try:
        cached = await get_music_search(query)
    except Exception as e:
        logger.warning(f"Music search cache read failed: {e}")

    if cached:
        unique = [SongEntry(**e) for e in cached]
        qhash = await set_music_search(query, cached)  # refresh TTL, get hash
    else:
        entries = await search_songs(query, max_results=30)
        if not entries:
            await message.answer(t("music.not_found", lang))
            return
        unique = _dedupe(entries)
        qhash = await set_music_search(query, [asdict(e) for e in unique])

    total_pages = max(1, math.ceil(len(unique) / PAGE_SIZE))
    text, kb = music_search_keyboard(qhash, unique, page=0, total_pages=total_pages, lang=lang)
    header = t("music.results_for", lang, query=esc(query))
    await message.answer(f"{header}\n\n{text}", parse_mode="HTML", reply_markup=kb)

    try:
        from bot.db.session import async_session_factory
        from bot.db.repo import MusicSearchRepo, UserRepo
        async with async_session_factory() as session:
            await MusicSearchRepo(session).log(message.from_user.id, query)
            await UserRepo(session).inc_searches(message.from_user.id)
    except Exception as e:
        logger.warning(f"Music search DB log: {e}")


@router.callback_query(F.data.startswith("msp:"))
async def cb_music_page(callback: CallbackQuery, user_lang: str = "uz"):
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer()
        return

    try:
        qhash, page = parts[1], int(parts[2])
    except ValueError:
        await callback.answer()
        return
    lang = user_lang

    raw = await get_music_search_by_hash(qhash)
    if not raw:
        await callback.answer(t("music.expired", lang), show_alert=True)
        return

    entries = [SongEntry(**e) for e in raw]
    total_pages = max(1, math.ceil(len(entries) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    text, kb = music_search_keyboard(qhash, entries, page=page, total_pages=total_pages, lang=lang)
    # Keep the list's header (the user's query) from the message itself.
    header = t("music.results", lang)
    try:
        current = callback.message.html_text or ""
        if "\n\n" in current:
            header = current.split("\n\n", 1)[0]
    except Exception:
        pass
    try:
        await callback.message.edit_text(f"{header}\n\n{text}", parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("ms:"))
async def cb_music_pick(callback: CallbackQuery, user_lang: str = "uz"):
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer()
        return

    try:
        qhash, idx = parts[1], int(parts[2])
    except ValueError:
        await callback.answer()
        return
    lang = user_lang

    raw = await get_music_search_by_hash(qhash)
    if not raw:
        await callback.answer(t("music.expired", lang), show_alert=True)
        return

    entries = [SongEntry(**e) for e in raw]
    if not 0 <= idx < len(entries):
        await callback.answer(t("err.invalid", lang), show_alert=True)
        return

    entry = entries[idx]

    cached_id = await get_audio_file_id(entry.id)
    if cached_id:
        await callback.answer()
        song_key = await store_song_meta(entry.title, entry.uploader)
        await callback.message.answer_audio(cached_id, reply_markup=audio_result_keyboard(song_key, lang))
        return

    # Reject overlong picks BEFORE a worker is involved (a 1 h "mix" would
    # produce an MP3 Telegram refuses on the cloud API).
    if entry.duration and entry.duration > settings.max_audio_duration:
        await callback.answer(t("err.too_long", lang), show_alert=True)
        return
    if not await queue_has_room(MUSIC_QUEUE):
        await callback.answer(t("err.overloaded", lang), show_alert=True)
        return
    if not await reserve_slot(callback.from_user.id):
        await callback.answer(t("err.user_cap", lang, n=settings.per_user_download_cap), show_alert=True)
        return
    await callback.answer()

    try:
        await callback.message.bot.send_chat_action(callback.message.chat.id, "upload_voice")
        progress_msg = await callback.message.answer(t("music.downloading", lang))
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
            slot_reserved=True,
            _queue_name=MUSIC_QUEUE,
        )
    except Exception as e:
        logger.error(f"Failed to enqueue music_download: {e}")
        await release_user_slot(callback.from_user.id)
        await callback.message.answer(t("music.error", lang))
        return

    try:
        from bot.db.session import async_session_factory
        from bot.db.repo import MusicSearchRepo
        async with async_session_factory() as session:
            await MusicSearchRepo(session).log(callback.from_user.id, f"{entry.uploader} {entry.title}", picked_index=idx)
    except Exception as e:
        logger.warning(f"Music pick DB log: {e}")


# Stickers, photos, locations, contacts… — everything without its own handler.
# Registered last in the last router, so it only sees what nobody else took.
@router.message(~F.text & ~F.voice & ~F.audio & ~F.video & ~F.video_note & ~F.document)
async def handle_unsupported(message: Message, user_lang: str):
    await message.answer(t("music.unsupported", user_lang))
