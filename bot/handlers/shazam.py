from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from bot.keyboards.inline import lyrics_nav_keyboard
from bot.logger import logger
from bot.services.cache import get_song_meta
from bot.services.lyrics import get_lyrics, paginate_lyrics

router = Router()

_MSGS = {
    "recognizing": {"uz": "🎧 Musiqa aniqlanmoqda...", "ru": "🎧 Распознаётся музыка...", "en": "🎧 Recognizing music..."},
    "not_found":   {"uz": "😔 Qo'shiqni aniqlab bo'lmadi.\n💡 10–15 soniyalik musiqali qism yuboring.", "ru": "😔 Не удалось распознать песню.\n💡 Попробуйте 10–15-секундный фрагмент.", "en": "😔 Could not recognize the song.\n💡 Try a clear 10–15 second musical clip."},
    "no_lyrics":   {"uz": "📜 Qo'shiq matni topilmadi.", "ru": "📜 Текст песни не найден.", "en": "📜 Lyrics not found."},
    "dl_hint":     {"uz": "🔍 Qidirilmoqda...", "ru": "🔍 Поиск...", "en": "🔍 Searching..."},
    "retry_hint":  {"uz": "🎵 Ovozli xabar, video yoki audio fayl yuboring.", "ru": "🎵 Отправьте голосовое, видео или аудиофайл.", "en": "🎵 Send a voice message, video, or audio file."},
}


def _is_media(message: Message) -> bool:
    return bool(message.video or message.video_note or message.voice or message.audio or message.document)


@router.message(_is_media)
async def handle_media(message: Message, user_lang: str):
    lang = user_lang or "en"
    await message.bot.send_chat_action(message.chat.id, "record_voice")
    progress_msg = await message.answer(_MSGS["recognizing"].get(lang, "🎧 Recognizing..."))

    file_id = None
    file_suffix = ".mp4"
    if message.video:
        file_id = message.video.file_id
    elif message.video_note:
        file_id = message.video_note.file_id
    elif message.voice:
        file_id = message.voice.file_id
        file_suffix = ".ogg"
    elif message.audio:
        file_id = message.audio.file_id
        file_suffix = ".mp3"
    elif message.document:
        file_id = message.document.file_id
        file_suffix = ".bin"

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
            _queue_name="arq:queue:recognition",
        )
    except Exception as e:
        logger.error(f"Failed to enqueue recognize job: {e}")
        await progress_msg.edit_text(_MSGS["not_found"].get(lang, "😔 Could not recognize."))


@router.callback_query(lambda c: c.data == "shazam:retry")
async def cb_retry(callback: CallbackQuery, user_lang: str):
    lang = user_lang or "en"
    await callback.message.answer(_MSGS["retry_hint"].get(lang, "🎵 Send a media file."))
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("lyrics:"))
async def cb_lyrics(callback: CallbackQuery, user_lang: str):
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer()
        return

    try:
        page = int(parts[1])
    except ValueError:
        await callback.answer()
        return
    song_key = parts[2]
    lang = user_lang or "en"

    meta = await get_song_meta(song_key)
    if not meta:
        await callback.answer("Session expired. Please search again.", show_alert=True)
        return

    title = meta["title"]
    artist = meta["artist"]

    lyrics = await get_lyrics(title, artist)
    if not lyrics:
        await callback.answer(_MSGS["no_lyrics"].get(lang, "📜 Lyrics not found."), show_alert=False)
        return

    pages = paginate_lyrics(lyrics)
    total = len(pages)
    page = max(0, min(page, total - 1))

    header = f"📜 <b>{title}</b> — {artist}\n"
    if total > 1:
        header += f"📄 {page + 1}/{total}\n"
    text = header + "\n" + pages[page]

    kb = lyrics_nav_keyboard(page, total, song_key) if total > 1 else None
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("dlsong:"))
async def cb_download_song(callback: CallbackQuery, user_lang: str):
    """Download MP3 of a recognized song (from song card)."""
    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        await callback.answer()
        return

    song_key = parts[1]
    lang = user_lang or "en"

    meta = await get_song_meta(song_key)
    if not meta:
        await callback.answer("Session expired. Please search again.", show_alert=True)
        return

    title = meta["title"]
    artist = meta["artist"]

    await callback.answer()
    progress_msg = await callback.message.answer(_MSGS["dl_hint"].get(lang, "🔍 Searching..."))

    try:
        from bot.services.music_search import search_songs
        from bot.services.queue import get_arq_pool

        entries = await search_songs(f"{artist} {title}", max_results=5)
        if not entries:
            await progress_msg.edit_text("😔 Song not found on YouTube.")
            return

        entry = entries[0]
        pool = await get_arq_pool()
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
            _queue_name="arq:queue:music",
        )
    except Exception as e:
        logger.error(f"dlsong task error: {e}")
        await progress_msg.edit_text("❌ Error fetching song.")
