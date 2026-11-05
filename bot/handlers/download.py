from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, ReplyParameters

from bot.config import settings
from bot.db.repo import DownloadRepo, UserRepo
from bot.keyboards.inline import video_result_keyboard  # noqa: F401  (re-export for older imports)
from bot.logger import logger
from bot.services.cache import (
    get_file_id, get_queue_depth, get_url_by_key, get_video_for_shazam, get_video_meta,
    release_user_slot, try_acquire_user_slot,
)
from bot.services.delivery import (
    audio_caption, progress_caption, register_video_buttons, send_cached_video,
    split_file_ids, video_presentation,
)
from bot.services.downloader import QUALITY_LADDER, detect_platform, has_cookies, normalize_url
from bot.texts import T, t

router = Router()
# Links work in private chats AND in groups the bot was added to ("add to
# group" button). Text search and media recognition stay private-only.
router.message.filter(F.chat.type.in_({ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP}))

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

DOWNLOAD_QUEUE = "arq:queue"
RECOGNITION_QUEUE = "arq:queue:recognition"
MUSIC_QUEUE = "arq:queue:music"

# Only these qualities can be requested through a button (callback_data is
# client-controlled; anything else would just pollute cache keys).
VIDEO_QUALITIES = frozenset(str(q) for q in QUALITY_LADDER)
ALLOWED_QUALITIES = VIDEO_QUALITIES | {"mp3"}

CAPTION = f"👉 @{settings.bot_username}"

# Backwards-compatible views of the localized strings (tests import these).
_DL = {k.split(".", 1)[1]: v for k, v in T.items() if k.startswith("dl.")}
_ERR = {k.split(".", 1)[1]: v for k, v in T.items() if k.startswith("err.")}


MAX_URLS_PER_MESSAGE = 5


def default_quality(platform: Optional[str]) -> str:
    """First delivery quality: fast 360p on YouTube, best up to 720p elsewhere."""
    if platform == "youtube":
        return settings.youtube_default_quality
    return settings.default_video_quality


def _is_group(message: Message) -> bool:
    return getattr(getattr(message, "chat", None), "type", ChatType.PRIVATE) in (
        ChatType.GROUP, ChatType.SUPERGROUP)


def _bg(coro) -> None:
    """Cosmetic call (chat action) that must not delay the enqueue."""
    task = asyncio.ensure_future(coro)
    task.add_done_callback(lambda tk: tk.cancelled() or tk.exception())


def extract_url(text: str) -> Optional[str]:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


def extract_urls(text: str, limit: int = MAX_URLS_PER_MESSAGE) -> tuple[list[str], bool]:
    """Distinct links in order (deduped by normalized form), capped at *limit*.

    Returns (urls, truncated). The same video pasted twice — or once with
    tracking parameters, or as youtu.be and youtube.com — is downloaded once.
    """
    seen: set[str] = set()
    urls: list[str] = []
    truncated = False
    for m in URL_RE.finditer(text or ""):
        raw = m.group(0)
        key = normalize_url(raw) if detect_platform(raw) else raw
        if key in seen:
            continue
        if len(urls) >= limit:
            truncated = True
            break
        seen.add(key)
        urls.append(raw)
    return urls, truncated


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
                            queue: str = DOWNLOAD_QUEUE, progress: str = "text",
                            reply_to: Optional[int] = None,
                            restore_caption: Optional[str] = None) -> None:
    from bot.services.queue import get_arq_pool
    pool = await get_arq_pool()
    # Only sent when set, so a worker from the previous deploy can still run it.
    extra = {"restore_caption": restore_caption} if restore_caption else {}
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
        progress=progress,
        reply_to=reply_to,
        _queue_name=queue,
        **extra,
    )


async def _record_cache_hit(user_id: int, url: str, platform: str, kind: str,
                            user_repo: UserRepo, download_repo: DownloadRepo) -> None:
    try:
        await user_repo.inc_downloads(user_id)
        await download_repo.create(user_id, url, platform, kind, from_cache=True)
    except Exception as e:
        logger.warning(f"Cache-hit stat write failed: {e}")


async def send_cached(bot, chat_id: int, url: str, platform: str, quality: str, cached: str,
                      lang: str, *, full: bool = False, reply_to: Optional[int] = None):
    """Deliver an already-uploaded video by file_id, with caption and buttons."""
    caption, kb = await video_presentation(url, platform, quality, lang, full=full)
    extra = ({"reply_parameters": ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)}
             if reply_to else {})
    sent = await send_cached_video(bot, chat_id, cached, caption, lang, reply_markup=kb, **extra)
    await register_video_buttons(url, cached)
    return sent


# Outcomes of one link. Errors are reported once per message by handle_url;
# STOP outcomes end processing of the remaining links (no point retrying them).
_STOP = frozenset({"overloaded", "user_cap"})


async def _process_one_url(message: Message, url: str, lang: str,
                           user_repo: UserRepo, download_repo: DownloadRepo) -> str:
    """Queue (or serve from cache) one link. Returns "ok" or an error kind."""
    platform = detect_platform(url)
    # Only known platforms (exact host match) are downloadable. Arbitrary URLs
    # would let anyone make the server fetch internal addresses (SSRF).
    if platform is None:
        return "unsupported"

    normalized = normalize_url(url)
    group = _is_group(message)
    reply_to = message.message_id if group else None

    # Instagram is login-gated platform-wide: without cookies every attempt
    # burns ~30s of worker time and fails. Fail fast with an honest message.
    if platform == "instagram" and not has_cookies():
        logger.warning("Instagram request rejected: no cookies configured "
                       "(set COOKIES_FILE or COOKIES_FROM_BROWSER)")
        return "ig_disabled"

    quality = default_quality(platform)

    # Cache hit → instant send, no worker needed
    cached_id = await get_file_id(normalized, quality)
    if cached_id:
        await send_cached(message.bot, message.chat.id, normalized, platform, quality, cached_id,
                          lang, reply_to=reply_to)
        await _record_cache_hit(message.from_user.id, normalized, platform, "video",
                                user_repo, download_repo)
        return "ok"

    if not await queue_has_room(DOWNLOAD_QUEUE):
        return "overloaded"

    if not await reserve_slot(message.from_user.id):
        return "user_cap"

    try:
        _bg(message.bot.send_chat_action(message.chat.id, "upload_video"))
        if group:
            progress_msg = await message.reply(t("dl.downloading", lang))
        else:
            progress_msg = await message.answer(t("dl.downloading", lang))
        await _enqueue_download(
            chat_id=message.chat.id,
            message_id=progress_msg.message_id,
            user_id=message.from_user.id,
            url=normalized,
            platform=platform,
            quality=quality,
            lang=lang,
            reply_to=reply_to,
        )
    except Exception as e:
        logger.error(f"Failed to enqueue download: {e}")
        await release_user_slot(message.from_user.id)
        return "generic"
    return "ok"


def _wants_link(message: Message) -> bool:
    """Private chat: any link (other sites get an "unsupported" answer).
    Group: only links to supported platforms. Everything else is filtered
    out here, before the middlewares, so a news link in a group is never
    throttled, answered or turned into a user row."""
    text = message.text or ""
    if not _is_group(message):
        return bool(extract_url(text))
    return any(detect_platform(m.group(0)) for m in URL_RE.finditer(text))


@router.message(_wants_link)
async def handle_url(message: Message, user_lang: str, user_repo: UserRepo, download_repo: DownloadRepo):
    """Every link in the message (up to MAX_URLS_PER_MESSAGE) is downloaded.

    Each link still takes its own per-user slot, so a pasted list cannot
    bypass PER_USER_DOWNLOAD_CAP; once the cap or the queue limit is hit the
    remaining links are skipped and the user is told once. In groups, links
    to other sites are ignored silently (people share news links there).
    """
    urls, truncated = extract_urls(message.text)
    if not urls:
        return
    lang = user_lang
    group = _is_group(message)
    if group:
        urls = [u for u in urls if detect_platform(u)]
        if not urls:
            return
    answer = message.reply if group else message.answer

    reported: set[str] = set()
    for url in urls:
        outcome = await _process_one_url(message, url, lang, user_repo, download_repo)
        if outcome == "ok" or outcome in reported:
            continue
        reported.add(outcome)
        n = settings.per_user_download_cap
        await answer(t("err.user_cap", lang, n=n) if outcome == "user_cap" else t(f"err.{outcome}", lang))
        if outcome in _STOP:
            break

    if truncated:
        await answer(t("dl.too_many_links", lang, n=MAX_URLS_PER_MESSAGE))


@router.callback_query(F.data.startswith("vq:"))
async def cb_video_quality(callback: CallbackQuery, user_lang: str,
                           user_repo: UserRepo, download_repo: DownloadRepo):
    """Another quality of a delivered video.

    Cached → the new file is sent at once. Otherwise the progress shows in
    the caption of the video the button sits under; the worker sends the new
    file and removes the old message.
    """
    parts = (callback.data or "").split(":", 2)
    if len(parts) < 3 or parts[1] not in VIDEO_QUALITIES:
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
    msg = callback.message if isinstance(callback.message, Message) else None
    chat = getattr(callback.message, "chat", None)
    chat_id = chat.id if chat is not None else callback.from_user.id
    # In a group the video answers the member's link; the new quality answers
    # it too, so it stays in the same thread / forum topic.
    link = getattr(msg, "reply_to_message", None) if msg is not None else None
    reply_to = link.message_id if isinstance(link, Message) else None
    replace = _may_replace(msg, link, callback.from_user.id)

    cached_id = await get_file_id(url, quality)
    if cached_id:
        await callback.answer()
        await send_cached(callback.bot, chat_id, url, platform, quality, cached_id, lang,
                          full=True, reply_to=reply_to)
        if replace:
            try:
                await msg.delete()
            except TelegramBadRequest:
                pass
        await _record_cache_hit(callback.from_user.id, url, platform, "video", user_repo, download_repo)
        return

    if not await queue_has_room(DOWNLOAD_QUEUE):
        await callback.answer(t("err.overloaded", lang), show_alert=True)
        return
    if not await reserve_slot(callback.from_user.id):
        await callback.answer(t("err.user_cap", lang, n=settings.per_user_download_cap), show_alert=True)
        return

    progress, progress_id, restore = "text", None, None
    try:
        if replace and msg.video is not None:
            meta = {}
            try:
                meta = await get_video_meta(url)
            except Exception as e:
                logger.warning(f"Video meta read failed: {e}")
            try:
                restore = msg.html_text or None
                await msg.edit_caption(caption=progress_caption(meta.get("title"), lang), reply_markup=None)
                progress, progress_id = "caption", msg.message_id
            except Exception as e:  # bad request, flood wait, network
                logger.info(f"Quality progress falls back to a message: {e}")
        if progress_id is None:
            under = msg.message_id if (msg is not None and _is_group(msg)) else None
            progress_id = (await callback.bot.send_message(
                chat_id, t("dl.downloading", lang), **_reply_kw(under))).message_id
        await _enqueue_download(
            chat_id=chat_id, message_id=progress_id, user_id=callback.from_user.id,
            url=url, platform=platform, quality=quality, lang=lang, progress=progress,
            reply_to=reply_to, restore_caption=restore if progress == "caption" else None,
        )
    except Exception as e:
        logger.error(f"Failed to enqueue quality download: {e}")
        await release_user_slot(callback.from_user.id)
        if progress == "caption":
            try:  # the video must not keep the "⏳" caption without buttons
                await msg.edit_caption(caption=restore, reply_markup=msg.reply_markup)
            except Exception as e2:
                logger.debug(f"Caption restore failed: {e2}")
        try:
            await callback.bot.send_message(chat_id, t("err.generic", lang))
        except Exception as e2:
            logger.debug(f"Error notice failed: {e2}")
    await callback.answer()


# Telegram refuses to delete messages older than 48 hours.
_DELETE_WINDOW = timedelta(hours=47)


def _reply_kw(message_id: Optional[int]) -> dict:
    if not message_id:
        return {}
    return {"reply_parameters": ReplyParameters(message_id=message_id, allow_sending_without_reply=True)}


def _may_replace(msg: Optional[Message], link, user_id: int) -> bool:
    """May a quality change take the place of *msg* (delete it)?

    Not when Telegram would refuse the delete, and in a group only for the
    member whose link the video answers: anyone else gets the new quality as
    an extra message, so nobody can make someone else's video disappear.
    """
    if msg is None:
        return False
    date = getattr(msg, "date", None)
    if isinstance(date, datetime):
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - date > _DELETE_WINDOW:
            return False
    if _is_group(msg):
        owner = getattr(link, "from_user", None) if isinstance(link, Message) else None
        return owner is not None and owner.id == user_id
    return True


@router.callback_query(F.data.startswith("vdalt:"))
async def cb_video_alt(callback: CallbackQuery, user_lang: str, user_repo: UserRepo, download_repo: DownloadRepo):
    """Audio of a delivered video ("🎶 Audio"); older keyboards also asked for 720/1080 here."""
    parts = (callback.data or "").split(":", 2)
    if len(parts) < 3 or parts[1] not in ALLOWED_QUALITIES | {"720", "1080"}:
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
    chat_id = callback.message.chat.id

    cached_id = await get_file_id(url, quality)
    if cached_id:
        if quality == "mp3":
            await callback.message.answer_audio(cached_id, caption=audio_caption())
            kind = "audio"
        else:
            await send_cached(callback.bot, chat_id, url, platform, quality, cached_id, lang, full=True)
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

    await callback.answer()
    try:
        progress_msg = await callback.message.answer(t("dl.downloading", lang))
        await _enqueue_download(
            chat_id=chat_id,
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


async def _video_for_shazam(key: str) -> Optional[dict]:
    """vshz payload; for URL-keyed buttons rebuilt from the file_id cache."""
    data = await get_video_for_shazam(key)
    if data:
        return data
    url = await get_url_by_key(key)
    if not url:
        return None
    platform = detect_platform(url)
    for q in [default_quality(platform), *sorted(VIDEO_QUALITIES)]:
        cached = await get_file_id(url, q)
        if cached:
            return {"fid": split_file_ids(cached)[0], "suf": ".mp4", "url": url}
    return None


@router.callback_query(F.data.startswith("vshz:"))
async def cb_video_shazam(callback: CallbackQuery, user_lang: str):
    """Find the song inside a previously downloaded video."""
    key = (callback.data or "").split(":", 1)[1]
    lang = user_lang

    video_data = await _video_for_shazam(key)
    if not video_data:
        await callback.answer(t("err.video_not_found", lang), show_alert=True)
        return

    if not await queue_has_room(RECOGNITION_QUEUE):
        await callback.answer(t("err.overloaded", lang), show_alert=True)
        return

    await callback.answer()
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
