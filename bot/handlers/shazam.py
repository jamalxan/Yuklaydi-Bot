from __future__ import annotations

import mimetypes
import os
from typing import Optional

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message

from bot.config import CLOUD_API_GETFILE_MAX_MB, settings
from bot.handlers.download import MUSIC_QUEUE, RECOGNITION_QUEUE, queue_has_room, reserve_slot
from bot.keyboards.inline import lyrics_nav_keyboard
from bot.logger import logger
from bot.services.cache import get_song_meta, release_user_slot
from bot.services.lyrics import get_lyrics, paginate_lyrics
from bot.texts import T, t
from bot.utils.text import esc

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)

# Backwards-compatible view of the localized strings (tests import this).
_MSGS = {
    "recognizing": T["dl.recognizing"],
    "not_found": T["shz.not_found"],
    "no_lyrics": T["shz.no_lyrics"],
    "dl_hint": T["shz.searching"],
    "retry_hint": T["shz.retry_hint"],
}

_TELEGRAM_MAX_MESSAGE = 4096


def _is_media(message: Message) -> bool:
    return bool(message.video or message.video_note or message.voice or message.audio or message.document)


def _document_suffix(mime: Optional[str], file_name: Optional[str]) -> Optional[str]:
    """Extension for an audio/video document; None for anything else (PDF, ZIP…)."""
    mime = (mime or "").lower()
    if not (mime.startswith("audio/") or mime.startswith("video/")):
        return None
    ext = os.path.splitext(file_name or "")[1].lower()
    if ext and len(ext) <= 6 and ext[1:].isalnum():
        return ext
    return mimetypes.guess_extension(mime) or (".mp4" if mime.startswith("video/") else ".mp3")


@router.message(_is_media)
async def handle_media(message: Message, user_lang: str):
    lang = user_lang

    file_id: Optional[str] = None
    file_size: Optional[int] = None
    file_suffix = ".mp4"
    if message.video:
        file_id, file_size = message.video.file_id, message.video.file_size
    elif message.video_note:
        file_id, file_size = message.video_note.file_id, message.video_note.file_size
    elif message.voice:
        file_id, file_size, file_suffix = message.voice.file_id, message.voice.file_size, ".ogg"
    elif message.audio:
        file_id, file_size = message.audio.file_id, message.audio.file_size
        file_suffix = _document_suffix(message.audio.mime_type, message.audio.file_name) or ".mp3"
    elif message.document:
        suffix = _document_suffix(message.document.mime_type, message.document.file_name)
        if suffix is None:  # PDF / ZIP / APK … are not media — don't burn a worker
            await message.answer(t("shz.unsupported_doc", lang))
            return
        file_id, file_size, file_suffix = message.document.file_id, message.document.file_size, suffix

    # The cloud Bot API refuses getFile for files > 20 MB; say so instead of
    # failing later with a misleading "song not recognized".
    if not settings.use_local_api and file_size and file_size > CLOUD_API_GETFILE_MAX_MB * 1024 * 1024:
        await message.answer(t("shz.file_too_big", lang, mb=CLOUD_API_GETFILE_MAX_MB))
        return

    if not await queue_has_room(RECOGNITION_QUEUE):
        await message.answer(t("err.overloaded", lang))
        return

    await message.bot.send_chat_action(message.chat.id, "record_voice")
    progress_msg = await message.answer(t("dl.recognizing", lang))
    try:
        from bot.services.queue import get_arq_pool
        pool = await get_arq_pool()
        await pool.enqueue_job(
            "recognize_task",
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            message_id=progress_msg.message_id,
            file_id=file_id,
            file_suffix=file_suffix,
            user_lang=lang,
            _queue_name="arq:queue:recognition",  # == RECOGNITION_QUEUE (literal for the routing test)
        )
    except Exception as e:
        logger.error(f"Failed to enqueue recognize job: {e}")
        await progress_msg.edit_text(t("shz.not_found", lang))


@router.callback_query(F.data == "shazam:retry")
async def cb_retry(callback: CallbackQuery, user_lang: str):
    await callback.message.answer(t("shz.retry_hint", user_lang))
    await callback.answer()


def build_lyrics_page(title: str, artist: str, pages: list[str], page: int) -> str:
    """Escaped lyrics message; guaranteed ≤ 4096 chars after escaping."""
    total = len(pages)
    header = f"📜 <b>{esc(title)}</b> — {esc(artist)}\n"
    if total > 1:
        header += f"📄 {page + 1}/{total}\n"
    body = esc(pages[page])
    room = _TELEGRAM_MAX_MESSAGE - len(header) - 1
    if len(body) > room:
        # Escaping can grow text (& → &amp;); trim on a line break, never mid-entity.
        cut = body.rfind("\n", 0, room)
        if cut <= 0:
            cut = body.rfind(" ", 0, room)
        if cut <= 0:
            cut = room
            amp = body.rfind("&", 0, cut)
            if amp != -1 and ";" not in body[amp:cut]:
                cut = amp
        body = body[:cut]
    return header + "\n" + body


@router.callback_query(F.data.startswith("lyrics:"))
async def cb_lyrics(callback: CallbackQuery, user_lang: str = "uz"):
    parts = (callback.data or "").split(":", 2)
    if len(parts) < 3:
        await callback.answer()
        return

    try:
        page = int(parts[1])
    except ValueError:
        await callback.answer()
        return
    song_key = parts[2]
    lang = user_lang

    meta = await get_song_meta(song_key)
    if not meta:
        await callback.answer(t("shz.session_expired", lang), show_alert=True)
        return

    title, artist = meta["title"], meta["artist"]

    lyrics = await get_lyrics(title, artist)
    if not lyrics:
        await callback.answer(t("shz.no_lyrics", lang), show_alert=False)
        return

    pages = paginate_lyrics(lyrics)
    total = len(pages)
    page = max(0, min(page, total - 1))
    text = build_lyrics_page(title, artist, pages, page)

    kb = lyrics_nav_keyboard(page, total, song_key) if total > 1 else None
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("dlsong:"))
async def cb_download_song(callback: CallbackQuery, user_lang: str = "uz"):
    """Download MP3 of a recognized song (from song card)."""
    parts = (callback.data or "").split(":", 1)
    if len(parts) < 2:
        await callback.answer()
        return

    song_key = parts[1]
    lang = user_lang

    meta = await get_song_meta(song_key)
    if not meta:
        await callback.answer(t("shz.session_expired", lang), show_alert=True)
        return

    title, artist = meta["title"], meta["artist"]

    if not await queue_has_room(MUSIC_QUEUE):
        await callback.answer(t("err.overloaded", lang), show_alert=True)
        return

    await callback.answer()
    progress_msg = await callback.message.answer(t("shz.searching", lang))

    try:
        from bot.services.music_search import search_songs

        entries = await search_songs(f"{artist} {title}", max_results=5)
        if not entries:
            await progress_msg.edit_text(t("shz.song_not_found", lang))
            return

        entry = entries[0]
        if entry.duration and entry.duration > settings.max_audio_duration:
            await progress_msg.edit_text(t("err.too_long", lang))
            return
        if not await reserve_slot(callback.from_user.id):
            await progress_msg.edit_text(t("err.user_cap", lang, n=settings.per_user_download_cap))
            return

        from bot.services.queue import get_arq_pool
        pool = await get_arq_pool()
        try:
            await pool.enqueue_job(
                "music_download_task",
                user_id=callback.from_user.id,
                chat_id=callback.message.chat.id,
                message_id=progress_msg.message_id,
                video_id=entry.id,
                url=entry.url,
                title=title,
                artist=artist,
                duration=entry.duration,
                thumbnail=entry.thumbnail,
                user_lang=lang,
                slot_reserved=True,
                _queue_name=MUSIC_QUEUE,
            )
        except Exception:
            await release_user_slot(callback.from_user.id)
            raise
    except Exception as e:
        logger.error(f"dlsong task error: {e}")
        await progress_msg.edit_text(t("shz.song_error", lang))
