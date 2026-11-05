from __future__ import annotations

import re
from typing import Optional

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message

from bot.config import settings
from bot.db.repo import DownloadRepo, UserRepo
from bot.keyboards.inline import video_result_keyboard
from bot.logger import logger
from bot.services.cache import (
    get_file_id, get_queue_depth, get_url_by_key, get_video_for_shazam,
    release_user_slot, store_url_key, store_video_for_shazam, try_acquire_user_slot,
)
from bot.services.downloader import detect_platform, has_cookies, normalize_url
from bot.texts import T, t

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

DOWNLOAD_QUEUE = "arq:queue"
RECOGNITION_QUEUE = "arq:queue:recognition"
MUSIC_QUEUE = "arq:queue:music"

# Only these qualities can be requested through a button (callback_data is
# client-controlled; anything else would just pollute cache keys).
ALLOWED_QUALITIES = frozenset({"720", "1080", "mp3"})

CAPTION = "🤖 @vidyuklaydi_bot"

# Backwards-compatible views of the localized strings (tests import these).
_DL = {k.split(".", 1)[1]: v for k, v in T.items() if k.startswith("dl.")}
_ERR = {k.split(".", 1)[1]: v for k, v in T.items() if k.startswith("err.")}


def extract_url(text: str) -> Optional[str]:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


async def queue_has_room(queue: str) -> bool:
    """Backpressure for ANY of the three queues."""
    try:
        return await get_queue_depth(queue) < settings.max_queue_depth
    except Exception as e:
        logger.warning(f"Queue depth check failed ({queue}): {e}")
        return True


async def reserve_slot(user_id: int) -> bool:
    """Reserve a per-user download slot at enqueue time (released by the worker)."""
    try:
        return await try_acquire_user_slot(user_id, settings.per_user_download_cap)
    except Exception as e:
        logger.warning(f"Slot reservation skipped (Redis error): {e}")
        return True


async def _enqueue_download(chat_id: int, message_id: int, user_id: int,
                            url: str, platform: str, quality: str, lang: str,
                            queue: str = DOWNLOAD_QUEUE) -> None:
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
        slot_reserved=True,
        _queue_name=queue,
    )


async def _record_cache_hit(user_id: int, url: str, platform: str, kind: str,
                            user_repo: UserRepo, download_repo: DownloadRepo) -> None:
    try:
        await user_repo.inc_downloads(user_id)
        await download_repo.create(user_id, url, platform, kind, from_cache=True)
    except Exception as e:
        logger.warning(f"Cache-hit stat write failed: {e}")


@router.message(lambda m: bool(extract_url(m.text or "")))
async def handle_url(message: Message, user_lang: str, user_repo: UserRepo, download_repo: DownloadRepo):
    url = extract_url(message.text)
    if not url:
        return

    lang = user_lang
    platform = detect_platform(url)

    # Only known platforms (exact host match) are downloadable. Arbitrary URLs
    # would let anyone make the server fetch internal addresses (SSRF).
    if platform is None:
        await message.answer(t("err.unsupported", lang))
        return

    normalized = normalize_url(url)

    # Instagram is login-gated platform-wide: without cookies every attempt
    # burns ~30s of worker time and fails. Fail fast with an honest message.
    if platform == "instagram" and not has_cookies():
        logger.warning("Instagram request rejected: no cookies configured "
                       "(set COOKIES_FILE or COOKIES_FROM_BROWSER)")
        await message.answer(t("err.ig_disabled", lang))
        return

    # Cache hit → instant send, no worker needed
    cached_id = await get_file_id(normalized, "720")
    if cached_id:
        shazam_key = await store_video_for_shazam(cached_id, source_url=normalized)
        url_key = await store_url_key(normalized)
        await message.answer_video(
            cached_id,
            caption=CAPTION,
            reply_markup=video_result_keyboard(url_key, shazam_key, lang),
        )
        await _record_cache_hit(message.from_user.id, normalized, platform, "video",
                                user_repo, download_repo)
        return

    if not await queue_has_room(DOWNLOAD_QUEUE):
        await message.answer(t("err.overloaded", lang))
        return

    if not await reserve_slot(message.from_user.id):
        await message.answer(t("err.user_cap", lang, n=settings.per_user_download_cap))
        return

    try:
        await message.bot.send_chat_action(message.chat.id, "upload_video")
        progress_msg = await message.answer(t("dl.queued", lang))
        await _enqueue_download(
            chat_id=message.chat.id,
            message_id=progress_msg.message_id,
            user_id=message.from_user.id,
            url=normalized,
            platform=platform,
            quality="720",
            lang=lang,
        )
    except Exception as e:
        logger.error(f"Failed to enqueue download: {e}")
        await release_user_slot(message.from_user.id)
        await message.answer(t("err.generic", lang))


@router.callback_query(F.data.startswith("vdalt:"))
async def cb_video_alt(callback: CallbackQuery, user_lang: str, user_repo: UserRepo, download_repo: DownloadRepo):
    """Re-download as MP3 (or 1080p)."""
    parts = (callback.data or "").split(":", 2)
    if len(parts) < 3 or parts[1] not in ALLOWED_QUALITIES:
        await callback.answer()
        return

    quality, url_key = parts[1], parts[2]
    lang = user_lang

    url = await get_url_by_key(url_key)
    if not url:
        await callback.answer(t("err.link_expired", lang), show_alert=True)
        return
    platform = detect_platform(url)
    if platform is None:  # defensive: keys are only ever created for valid URLs
        await callback.answer(t("err.unsupported", lang), show_alert=True)
        return

    cached_id = await get_file_id(url, quality)
    if cached_id:
        if quality == "mp3":
            await callback.message.answer_audio(cached_id, caption=CAPTION)
            kind = "audio"
        else:
            shazam_key = await store_video_for_shazam(cached_id, source_url=url)
            await callback.message.answer_video(
                cached_id, caption=CAPTION,
                reply_markup=video_result_keyboard(url_key, shazam_key, lang),
            )
            kind = "video"
        await _record_cache_hit(callback.from_user.id, url, platform, kind, user_repo, download_repo)
        await callback.answer()
        return

    if not await queue_has_room(DOWNLOAD_QUEUE):
        await callback.answer(t("err.overloaded", lang), show_alert=True)
        return
    if not await reserve_slot(callback.from_user.id):
        await callback.answer(t("err.user_cap", lang, n=settings.per_user_download_cap), show_alert=True)
        return

    try:
        progress_msg = await callback.message.answer(t("dl.queued", lang))
        await _enqueue_download(
            chat_id=callback.message.chat.id,
            message_id=progress_msg.message_id,
            user_id=callback.from_user.id,
            url=url,
            platform=platform,
            quality=quality,
            lang=lang,
        )
    except Exception as e:
        logger.error(f"Failed to enqueue alt download: {e}")
        await release_user_slot(callback.from_user.id)
        await callback.message.answer(t("err.generic", lang))

    await callback.answer()


@router.callback_query(F.data.startswith("vshz:"))
async def cb_video_shazam(callback: CallbackQuery, user_lang: str):
    """Find the song inside a previously downloaded video."""
    key = (callback.data or "").split(":", 1)[1]
    lang = user_lang

    video_data = await get_video_for_shazam(key)
    if not video_data:
        await callback.answer(t("err.video_not_found", lang), show_alert=True)
        return

    if not await queue_has_room(RECOGNITION_QUEUE):
        await callback.answer(t("err.overloaded", lang), show_alert=True)
        return

    progress_msg = await callback.message.answer(t("dl.recognizing", lang))
    try:
        from bot.services.queue import get_arq_pool
        pool = await get_arq_pool()
        await pool.enqueue_job(
            "recognize_task",
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id,
            message_id=progress_msg.message_id,
            file_id=video_data["fid"],
            file_suffix=video_data.get("suf", ".mp4"),
            user_lang=lang,
            source_url=video_data.get("url", ""),
            _queue_name=RECOGNITION_QUEUE,
        )
    except Exception as e:
        logger.error(f"Failed to enqueue vshz recognize: {e}")
        await progress_msg.edit_text(t("err.video_process", lang))

    await callback.answer()

