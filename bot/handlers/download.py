from __future__ import annotations

import re
from typing import Optional

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from bot.config import settings
from bot.db.repo import DownloadRepo, UserRepo
from bot.keyboards.inline import video_result_keyboard
from bot.logger import logger
from bot.services.cache import (
    get_active_downloads, get_file_id, get_queue_depth,
    get_url_by_key, get_video_for_shazam,
    store_url_key, store_video_for_shazam,
)
from bot.services.downloader import detect_platform, has_cookies, normalize_url

router = Router()

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

_DL = {
    "downloading": {"uz": "⏳ Yuklanmoqda...", "ru": "⏳ Скачивается...", "en": "⏳ Downloading..."},
    "queued":      {"uz": "📋 Navbatda...", "ru": "📋 В очереди...", "en": "📋 Queued..."},
    "uploading":   {"uz": "📤 Yuborilmoqda...", "ru": "📤 Отправляется...", "en": "📤 Uploading..."},
    "cached":      {"uz": "⚡ Keshdan!", "ru": "⚡ Из кэша!", "en": "⚡ Cached!"},
    "recognizing": {"uz": "🎧 Musiqa aniqlanmoqda...", "ru": "🎧 Распознаётся музыка...", "en": "🎧 Recognizing music..."},
}

_ERR = {
    "private":     {"uz": "🔒 Bu video shaxsiy yoki o'chirilgan.", "ru": "🔒 Это видео приватное или удалено.", "en": "🔒 This video is private or has been deleted."},
    "geo":         {"uz": "🌍 Bu video hududingizda mavjud emas.", "ru": "🌍 Это видео недоступно в вашем регионе.", "en": "🌍 This video is not available in your region."},
    "age":         {"uz": "🔞 Bu video yoshi cheklangan.", "ru": "🔞 Это видео imeeт возрастное ограничение.", "en": "🔞 This video has age restrictions."},
    "too_large":   {"uz": f"📦 Fayl juda katta (maks {settings.max_file_mb}MB).", "ru": f"📦 Файл слишком большой (макс {settings.max_file_mb}МБ).", "en": f"📦 File too large (max {settings.max_file_mb}MB)."},
    "unsupported": {"uz": "❌ Ushbu havola qo'llab-quvvatlanmaydi.", "ru": "❌ Эта ссылка не поддерживается.", "en": "❌ This link is not supported."},
    "generic":     {"uz": "❌ Yuklab olishda xatolik. Keyinroq urinib ko'ring.", "ru": "❌ Ошибка при скачивании. Попробуйте позже.", "en": "❌ Download failed. Please try again later."},
    "overloaded":  {"uz": "🔄 Hozir server band. Bir oz kuting.", "ru": "🔄 Сервер перегружен. Попробуйте позже.", "en": "🔄 Server overloaded. Please try again shortly."},
    "ig_disabled": {
        "uz": "📷 Instagram yuklab olish hozircha texnik sozlashda. Tez orada ishga tushadi.\n💡 Hozircha YouTube havolalarini yuborishingiz mumkin.",
        "ru": "📷 Скачивание из Instagram пока настраивается. Скоро заработает.\n💡 Пока можно отправлять ссылки YouTube.",
        "en": "📷 Instagram downloads are being set up. Coming soon.\n💡 Meanwhile you can send YouTube links.",
    },
}


def extract_url(text: str) -> Optional[str]:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


def _ack_text(lang: str, active: int, cap: int) -> str:
    """Return the right ack message: downloading or queued."""
    if active < cap:
        return _DL["downloading"].get(lang, "⏳ Downloading...")
    return _DL["queued"].get(lang, "📋 Queued...")


async def _enqueue_download(chat_id: int, message_id: int, user_id: int,
                             url: str, platform: str, quality: str, lang: str,
                             queue: str = "arq:queue") -> None:
    from bot.services.queue import get_arq_pool
    pool = await get_arq_pool()
    await pool.enqueue_job(
        "download_task",
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
        url=url,
        platform=platform,
        quality=quality,
        user_lang=lang,
        _queue_name=queue,
    )


@router.message(lambda m: bool(extract_url(m.text or "")))
async def handle_url(message: Message, user_lang: str, user_repo: UserRepo, download_repo: DownloadRepo):
    url = extract_url(message.text)
    if not url:
        return

    lang = user_lang or "en"
    platform = detect_platform(url)
    normalized = normalize_url(url)

    # Only known platforms are downloadable. Passing arbitrary URLs to
    # yt-dlp's generic extractor would let anyone make the server fetch
    # internal/private addresses (SSRF) and waste workers on dead links.
    if platform is None:
        await message.answer(_ERR["unsupported"].get(lang, _ERR["unsupported"]["en"]))
        return

    # Instagram is login-gated platform-wide: without cookies every attempt
    # burns ~30s of worker time and fails. Fail fast with an honest message.
    if platform == "instagram" and not has_cookies():
        logger.warning("Instagram request rejected: no cookies configured "
                       "(set COOKIES_FILE or COOKIES_FROM_BROWSER)")
        await message.answer(_ERR["ig_disabled"].get(lang, _ERR["ig_disabled"]["en"]))
        return

    # Cache hit → instant send, no worker needed
    cached_id = await get_file_id(normalized, "720")
    if cached_id:
        shazam_key = await store_video_for_shazam(cached_id, source_url=normalized)
        url_key = await store_url_key(normalized)
        kb = video_result_keyboard(url_key, shazam_key, lang)
        await message.answer_video(
            cached_id,
            caption="🤖 @vidyuklaydi_bot",
            reply_markup=kb,
        )
        await user_repo.inc_downloads(message.from_user.id)
        await download_repo.inc_daily(cache_hit=True)
        return

    # Backpressure: reject if queue is at capacity
    depth = await get_queue_depth("arq:queue")
    if depth >= settings.max_queue_depth:
        await message.answer(_ERR["overloaded"].get(lang, "🔄 Server overloaded."))
        return

    # Per-user concurrency ack (queued vs downloading)
    active = await get_active_downloads(message.from_user.id)
    await message.bot.send_chat_action(message.chat.id, "upload_video")
    progress_msg = await message.answer(_ack_text(lang, active, settings.per_user_download_cap))

    try:
        await _enqueue_download(
            chat_id=message.chat.id,
            message_id=progress_msg.message_id,
            user_id=message.from_user.id,
            url=normalized,
            platform=platform or "unknown",
            quality="720",
            lang=lang,
        )
    except Exception as e:
        logger.error(f"Failed to enqueue download: {e}")
        await progress_msg.edit_text(_ERR["generic"].get(lang, "❌ Error."))


@router.callback_query(lambda c: c.data and c.data.startswith("vdalt:"))
async def cb_video_alt(callback: CallbackQuery, user_lang: str, user_repo: UserRepo, download_repo: DownloadRepo):
    """Re-download as MP3 or 1080p."""
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer()
        return

    quality = parts[1]
    url_key = parts[2]
    lang = user_lang or "en"

    url = await get_url_by_key(url_key)
    if not url:
        await callback.answer({"uz": "Havola muddati tugagan", "ru": "Ссылка устарела", "en": "Link expired"}.get(lang, "Link expired"), show_alert=True)
        return

    cached_id = await get_file_id(url, quality)
    if cached_id:
        if quality == "mp3":
            await callback.message.answer_audio(cached_id)
        else:
            await callback.message.answer_video(cached_id)
        await callback.answer()
        return

    progress_msg = await callback.message.answer(_DL["downloading"].get(lang, "⏳ Downloading..."))

    try:
        await _enqueue_download(
            chat_id=callback.message.chat.id,
            message_id=progress_msg.message_id,
            user_id=callback.from_user.id,
            url=url,
            platform=detect_platform(url) or "unknown",
            quality=quality,
            lang=lang,
        )
    except Exception as e:
        logger.error(f"Failed to enqueue alt download: {e}")
        await progress_msg.edit_text(_ERR["generic"].get(lang, "❌ Error."))

    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("vshz:"))
async def cb_video_shazam(callback: CallbackQuery, user_lang: str):
    """Find the song inside a previously downloaded video."""
    key = callback.data.split(":", 1)[1]
    lang = user_lang or "en"

    video_data = await get_video_for_shazam(key)
    if not video_data:
        await callback.answer({"uz": "Video topilmadi", "ru": "Видео не найдено", "en": "Video not found"}.get(lang, "Not found"), show_alert=True)
        return

    file_id = video_data["fid"]
    file_suffix = video_data.get("suf", ".mp4")
    source_url = video_data.get("url", "")

    progress_msg = await callback.message.answer(_DL["recognizing"].get(lang, "🎧 Recognizing..."))

    try:
        from bot.services.queue import get_arq_pool
        pool = await get_arq_pool()
        await pool.enqueue_job(
            "recognize_task",
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id,
            message_id=progress_msg.message_id,
            file_id=file_id,
            file_suffix=file_suffix,
            user_lang=lang,
            source_url=source_url,
            _queue_name="arq:queue:recognition",
        )
    except Exception as e:
        logger.error(f"Failed to enqueue vshz recognize: {e}")
        await progress_msg.edit_text("😔 Could not process video.")

    await callback.answer()
