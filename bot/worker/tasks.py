from __future__ import annotations

import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Optional

import aiohttp
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import BufferedInputFile, FSInputFile, InputMediaPhoto
from arq import cron, func
from arq.connections import RedisSettings

from bot.config import settings
from bot.logger import logger, setup_logging
from bot.services.cache import (
    acquire_single_flight, acquire_user_slot,
    get_file_id, release_single_flight, release_user_slot,
    set_file_id, single_flight_held, store_song_meta, store_url_key, store_video_for_shazam,
)
from bot.services.downloader import DownloadError, DownloadResult, download
from bot.services.media import ffmpeg_path, get_temp_path, safe_delete
from bot.services.recognizer import recognize
from bot.services.delivery import join_file_ids, part_caption
from bot.services.splitter import SplitError, remove_parts, split_video
from bot.texts import T, error_text, t
from bot.utils.text import esc

CAPTION = "🤖 @vidyuklaydi_bot"

# Backwards-compatible views of the localized strings (tests import these).
_DL = {k.split(".", 1)[1]: v for k, v in T.items() if k.startswith("dl.")}
_ERR = {k.split(".", 1)[1]: v for k, v in T.items() if k.startswith("err.")}
_NOT_FOUND = T["shz.not_found"]

# Leave the job enough headroom to tell the user before arq kills it.
_TIMEOUT_HEADROOM = 15

# Semaphore caps concurrent yt-dlp calls in this worker process (CPU/network guard)
_YT_DLP_SEM: Optional[asyncio.Semaphore] = None


def _get_yt_dlp_sem() -> asyncio.Semaphore:
    global _YT_DLP_SEM
    if _YT_DLP_SEM is None:
        _YT_DLP_SEM = asyncio.Semaphore(settings.yt_dlp_worker_concurrency)
    return _YT_DLP_SEM


def _job_deadline() -> float:
    return max(5.0, float(settings.job_timeout - _TIMEOUT_HEADROOM))


# ── Bot / Telegram helpers ────────────────────────────────────────────────────

def _make_bot() -> Bot:
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    # Workers upload multi-MB (up to 2GB via local API) files — the default
    # 60s request timeout aborts slow uploads with "Request timeout error".
    upload_timeout = max(300, settings.job_timeout)
    session = (
        AiohttpSession(api=TelegramAPIServer.from_base(settings.local_api_url, is_local=True),
                       timeout=upload_timeout)
        if settings.use_local_api
        else AiohttpSession(timeout=upload_timeout)
    )
    return Bot(token=settings.bot_token, session=session,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def _get_bot(ctx: dict) -> Bot:
    """Reuse the worker-process Bot (one aiohttp session per process).

    Creating a Bot + TCP session per job is pure churn at high job rates.
    Falls back to a fresh bot if the worker startup hook didn't run (tests).
    """
    bot = ctx.get("bot") if isinstance(ctx, dict) else None
    return bot if bot is not None else _make_bot()


async def worker_startup(ctx: dict) -> None:
    setup_logging()
    ctx["bot"] = _make_bot()
    logger.info("Worker startup: shared Bot session created")


async def worker_shutdown(ctx: dict) -> None:
    bot = ctx.pop("bot", None)
    if bot is not None:
        try:
            await bot.session.close()
        except Exception:
            pass
    logger.info("Worker shutdown: Bot session closed")


async def _flood_retry(coro_factory, max_wait: int = 30):
    """Run a Telegram send; on flood-control RetryAfter wait and retry once.

    Under burst load Telegram answers RetryAfter; without this the job fails
    with an error message even though the content would send fine a moment
    later (user sees an error AND, on client-timeout races, the real content).
    """
    try:
        return await coro_factory()
    except TelegramRetryAfter as e:
        wait = min(int(getattr(e, "retry_after", 5)) + 1, max_wait)
        logger.warning(f"Flood control: retrying send in {wait}s")
        await asyncio.sleep(wait)
        return await coro_factory()


async def _safe_edit(bot: Bot, chat_id: int, message_id: int, text: str, **kwargs) -> None:
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, **kwargs)
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "message is not modified" in err:
            return
        if "message to edit not found" in err or "message_id_invalid" in err:
            return
        raise


async def _try_edit(bot: Bot, chat_id: int, message_id: int, text: str, **kwargs) -> None:
    """Best-effort progress update that never raises (used in error paths)."""
    try:
        await _safe_edit(bot, chat_id, message_id, text, **kwargs)
    except Exception:
        pass


async def _drop_progress(bot: Bot, chat_id: int, message_id: int, fallback_text: str) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        await _try_edit(bot, chat_id, message_id, fallback_text)


def _log_timing(event: str, **fields) -> None:
    # Structured fields are merged into the JSON log line by JsonFormatter.
    logger.info(event, extra={"fields": {"event": event, **fields}})


@asynccontextmanager
async def _user_job(bot: Bot, chat_id: int, message_id: int, lang: str):
    """Common envelope for user-facing jobs.

    - asyncio.timeout fires a little BEFORE arq's job_timeout, so the user gets
      an honest "took too long" instead of an eternal "⏳ Downloading…";
    - on cancellation (arq timeout / worker shutdown) the progress message is
      still updated, then the cancellation is re-raised.
    """
    try:
        async with asyncio.timeout(_job_deadline()):
            yield
    except TimeoutError:
        logger.warning(f"Job hit its deadline ({_job_deadline():.0f}s) for chat {chat_id}")
        await _try_edit(bot, chat_id, message_id, t("err.job_timeout", lang))
    except asyncio.CancelledError:
        await asyncio.shield(_try_edit(bot, chat_id, message_id, t("err.job_timeout", lang)))
        raise


async def _record_download(user_id: int, url: str, platform: str, kind: str,
                           from_cache: bool, file_id: Optional[str] = None) -> None:
    """Every delivery — fresh or cached — becomes one row in `downloads`."""
    try:
        from bot.db.repo import DownloadRepo, UserRepo
        from bot.db.session import async_session_factory
        async with async_session_factory() as session:
            users = UserRepo(session)
            await users.inc_downloads(user_id)
            await users.touch(user_id)
            await DownloadRepo(session).create(user_id, url, platform, kind,
                                               from_cache=from_cache, file_id=file_id)
    except Exception as e:
        logger.warning(f"DB stat update failed: {e}")


# ── Instagram photo-post fallback ─────────────────────────────────────────────

async def _try_instagram_photo(bot: Bot, chat_id: int, message_id: int, url: str) -> bool:
    """If an Instagram post has no video, download and send its photo(s)."""
    m = re.search(r'/(?:p|reel|tv)/([A-Za-z0-9_-]+)', url)
    if not m:
        return False
    shortcode = m.group(1)
    try:
        import instaloader

        def _get_post():
            loader = instaloader.Instaloader(quiet=True, save_metadata=False, download_comments=False)
            return instaloader.Post.from_shortcode(loader.context, shortcode)

        post = await asyncio.to_thread(_get_post)
        if post.is_video:
            return False  # video but login needed — caller handles error

        try:
            photo_urls = [node.display_url for node in post.get_sidecar_nodes()]
        except Exception:
            photo_urls = []
        if not photo_urls:
            photo_urls = [post.url]

        photos: list[bytes] = []
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for photo_url in photo_urls[:10]:
                try:
                    async with session.get(photo_url) as resp:
                        if resp.status == 200:
                            photos.append(await resp.read())
                except Exception as e:
                    logger.warning(f"Instagram photo fetch failed: {e}")
        if not photos:
            return False

        if len(photos) == 1:
            await _flood_retry(lambda: bot.send_photo(
                chat_id, photo=BufferedInputFile(photos[0], filename="photo_1.jpg"), caption=CAPTION))
        else:
            media = [
                InputMediaPhoto(media=BufferedInputFile(data, filename=f"photo_{i + 1}.jpg"),
                                caption=CAPTION if i == 0 else None)
                for i, data in enumerate(photos)
            ]
            await _flood_retry(lambda: bot.send_media_group(chat_id, media=media))

        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning(f"Instagram photo fallback: {e}")
        return False


# ── Video / MP3 download ──────────────────────────────────────────────────────

async def _send_cached(bot: Bot, chat_id: int, message_id: int,
                       url: str, quality: str, file_id: str, lang: str) -> None:
    """Send a cached file_id (or all parts of a split video) and clean up
    the progress message."""
    if quality == "mp3":
        await _flood_retry(lambda: bot.send_audio(chat_id, audio=file_id, caption=CAPTION))
    else:
        from bot.services.delivery import send_cached_video, split_file_ids
        shazam_key = await store_video_for_shazam(split_file_ids(file_id)[0], source_url=url)
        url_key = await store_url_key(url)
        from bot.keyboards.inline import video_result_keyboard
        kb = video_result_keyboard(url_key, shazam_key, lang)
        await send_cached_video(
            bot, chat_id, file_id, CAPTION, lang, reply_markup=kb,
            send=lambda **kw: _flood_retry(lambda: bot.send_video(**kw)))
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def _wait_for_other_worker(url: str, quality: str) -> Optional[str]:
    """Wait while another worker downloads the same URL.

    Returns its cached file_id, or None once the lock is gone without a cache
    entry (the other job failed) — the caller then downloads itself. Bounded
    by the job deadline, so it never starts a duplicate download while the
    first one is still running (the old fixed 60 s budget did exactly that).
    """
    while True:
        await asyncio.sleep(2)
        file_id = await get_file_id(url, quality)
        if file_id:
            return file_id
        if not await single_flight_held(url, quality):
            return await get_file_id(url, quality)


async def download_task(
    ctx: dict, *,
    user_id: int, chat_id: int, message_id: int,
    url: str, platform: str, quality: str, user_lang: str,
    slot_reserved: bool = False,
):
    bot = _get_bot(ctx)
    own_bot = bot is not ctx.get("bot")
    temp_path: Optional[str] = None
    split_source: Optional[str] = None
    lang = user_lang or settings.default_lang
    t_total_start = time.monotonic()
    sf_acquired = False
    kind = "audio" if quality == "mp3" else "video"

    # Jobs enqueued by current handlers reserved their slot at enqueue time;
    # jobs from an older deploy did not.
    if not slot_reserved:
        await acquire_user_slot(user_id)

    try:
        async with _user_job(bot, chat_id, message_id, lang):
            try:
                # Fast path: another job for the same URL may have finished
                # while this one waited in the queue.
                cached = await get_file_id(url, quality)
                if cached:
                    await _send_cached(bot, chat_id, message_id, url, quality, cached, lang)
                    await _record_download(user_id, url, platform, kind, from_cache=True, file_id=cached)
                    _log_timing("download_complete", platform=platform, quality=quality,
                                t_total=round(time.monotonic() - t_total_start, 2),
                                from_cache=True, single_flight_wait=False,
                                local_api=settings.use_local_api)
                    return

                await _safe_edit(bot, chat_id, message_id, t("dl.downloading", lang))

                # Single-flight: one download per URL+quality across all workers.
                sf_acquired = await acquire_single_flight(url, quality, ttl=settings.job_timeout)
                waited = False
                while not sf_acquired:
                    waited = True
                    other = await _wait_for_other_worker(url, quality)
                    if other:
                        await _send_cached(bot, chat_id, message_id, url, quality, other, lang)
                        await _record_download(user_id, url, platform, kind, from_cache=True, file_id=other)
                        _log_timing("download_complete", platform=platform, quality=quality,
                                    t_total=round(time.monotonic() - t_total_start, 2),
                                    from_cache=True, single_flight_wait=True,
                                    local_api=settings.use_local_api)
                        return
                    sf_acquired = await acquire_single_flight(url, quality, ttl=settings.job_timeout)

                temp_path = get_temp_path(suffix=".mp3" if quality == "mp3" else ".mp4")
                async with _get_yt_dlp_sem():
                    t_dl_start = time.monotonic()
                    result: DownloadResult = await download(url, temp_path, quality)
                    t_download = time.monotonic() - t_dl_start

                actual_path = result.path
                if not os.path.exists(actual_path) or result.size_bytes == 0:
                    raise DownloadError("Download produced empty or missing file", kind="generic")

                # Too big for one upload: video is cut into parts at full
                # quality (no re-encode); audio has no such fallback.
                upload_paths = [actual_path]
                if result.size_bytes > settings.max_file_bytes:
                    if quality == "mp3" or not settings.split_large_videos:
                        raise DownloadError("Too large", kind="too_large")
                    split_source = actual_path
                    try:
                        upload_paths = await split_video(actual_path, settings.max_file_bytes)
                    except SplitError as e:
                        logger.warning(f"Split failed for {url}: {e}")
                        raise DownloadError("Too large", kind="too_large") from e

                await _safe_edit(bot, chat_id, message_id, t("dl.uploading", lang))
                await bot.send_chat_action(chat_id, "upload_voice" if quality == "mp3" else "upload_video")

                t_up_start = time.monotonic()
                n_parts = len(upload_paths)
                file_ids: list[str] = []
                for i, part in enumerate(upload_paths, 1):
                    input_file = FSInputFile(part)
                    if n_parts > 1:
                        await _try_edit(bot, chat_id, message_id,
                                        t("dl.uploading_part", lang, i=i, n=n_parts))
                    if quality == "mp3":
                        sent = await _flood_retry(lambda: bot.send_audio(chat_id, audio=input_file, caption=CAPTION))
                        media = sent.audio
                    else:
                        cap = part_caption(CAPTION, i, n_parts, lang)
                        sent = await _flood_retry(lambda: bot.send_video(
                            chat_id, video=input_file, caption=cap, supports_streaming=True))
                        media = sent.video
                    if media is None:
                        raise RuntimeError("Telegram response has no media object")
                    file_ids.append(media.file_id)
                file_id = join_file_ids(file_ids)
                t_upload = time.monotonic() - t_up_start
                t_total = time.monotonic() - t_total_start

                # Cache first: single-flight waiters poll for this key.
                await set_file_id(url, quality, file_id)

                if quality != "mp3":
                    shazam_key = await store_video_for_shazam(file_ids[0], source_url=url)
                    url_key = await store_url_key(url)
                    from bot.keyboards.inline import video_result_keyboard
                    try:
                        await bot.edit_message_reply_markup(
                            chat_id=chat_id, message_id=sent.message_id,
                            reply_markup=video_result_keyboard(url_key, shazam_key, lang),
                        )
                    except Exception:
                        pass

                await _drop_progress(bot, chat_id, message_id, t("dl.done", lang))

                _log_timing("download_complete",
                            platform=platform, quality=quality,
                            size_mb=round(result.size_bytes / 1024 / 1024, 1),
                            t_extract=round(result.t_extract, 2),
                            t_download=round(t_download, 2),
                            t_upload=round(t_upload, 2),
                            t_total=round(t_total, 2),
                            from_cache=False, single_flight_wait=waited,
                            local_api=settings.use_local_api)

                await _record_download(user_id, url, platform, kind, from_cache=False, file_id=file_id)

                try:
                    from bot.services.cache import get_redis
                    r = await get_redis()
                    pipe = r.pipeline()
                    await pipe.incrbyfloat("stat:t_total_sum", t_total)
                    await pipe.incr("stat:t_total_count")
                    await pipe.execute()
                except Exception:
                    pass

            except DownloadError as e:
                # Instagram photo post fallback: send as photo instead of an error
                if e.kind == "no_video" and platform == "instagram":
                    if await _try_instagram_photo(bot, chat_id, message_id, url):
                        return
                logger.info(f"Download failed ({e.kind}): {str(e)[:200]}")
                limit_mb = (settings.max_video_bytes if kind == "video" else settings.max_file_bytes) // 1048576
                await _try_edit(bot, chat_id, message_id, error_text(e.kind, lang, mb=limit_mb))
            except Exception as e:
                logger.error(f"Download task error: {e}", exc_info=True)
                await _try_edit(bot, chat_id, message_id, t("err.generic", lang))
    finally:
        if split_source:
            remove_parts(split_source)
        if temp_path:
            base = os.path.splitext(temp_path)[0]
            for path in {temp_path, *(base + ext for ext in (".mp3", ".mp4", ".m4a", ".webm", ".opus", ".mkv"))}:
                safe_delete(path)
        if sf_acquired:
            await release_single_flight(url, quality)
        await release_user_slot(user_id)
        if own_bot:
            await bot.session.close()


# ── Recognition ───────────────────────────────────────────────────────────────

async def _download_for_recognize(bot: Bot, file_id: str, file_suffix: str,
                                  temp_path: str, source_url: str = "") -> bool:
    """Download media for recognition. Returns True on success."""
    logger.debug(f"[recognize_dl] file_id={file_id!r} suffix={file_suffix!r} local_api={settings.use_local_api}")
    # Primary path: download from Telegram (cloud API: files ≤ 20 MB only)
    try:
        file_info = await bot.get_file(file_id)
        if not file_info.file_path:
            raise RuntimeError("getFile returned no file_path")
        await bot.download_file(file_info.file_path, destination=temp_path)
        size = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
        if size > 0:
            logger.debug(f"[recognize_dl] via Telegram ({size // 1024}KB)")
            return True
        logger.warning("[recognize_dl] download succeeded but file is empty")
    except Exception as e:
        logger.warning(f"[recognize_dl] Telegram getFile FAILED: {type(e).__name__}: {e}")

    # Fallback: short audio clip from the original URL (bypasses the 20 MB limit).
    if source_url:
        clip_path = os.path.splitext(temp_path)[0] + "_clip.m4a"
        try:
            from bot.services.downloader import download_audio_clip
            async with _get_yt_dlp_sem():
                produced = await download_audio_clip(source_url, clip_path)
            if os.path.isfile(produced) and os.path.getsize(produced) > 0:
                os.replace(produced, temp_path)
                logger.info(f"Recognize: fetched audio clip via yt-dlp from {source_url}")
                return True
        except Exception as e:
            logger.warning(f"Recognize: yt-dlp audio fallback failed: {e}")
        finally:
            base = os.path.splitext(clip_path)[0]
            for ext in (".m4a", ".webm", ".opus", ".mp4", ".mp3"):
                safe_delete(base + ext)

    return False


async def recognize_task(
    ctx: dict, *,
    user_id: int, chat_id: int, message_id: int,
    file_id: str, file_suffix: str, user_lang: str,
    source_url: str = "",
):
    bot = _get_bot(ctx)
    own_bot = bot is not ctx.get("bot")
    temp_path: Optional[str] = None
    lang = user_lang or settings.default_lang

    try:
        async with _user_job(bot, chat_id, message_id, lang):
            try:
                temp_path = get_temp_path(suffix=file_suffix, prefix="shz_in_")
                if not await _download_for_recognize(bot, file_id, file_suffix, temp_path, source_url):
                    logger.error(f"Recognize: failed to download file {file_id}")
                    await _safe_edit(bot, chat_id, message_id, t("shz.not_found", lang))
                    return

                await bot.send_chat_action(chat_id, "record_voice")
                result = await recognize(temp_path)
                if not result:
                    await _safe_edit(bot, chat_id, message_id, t("shz.not_found", lang))
                    return

                lyrics = result.lyrics
                if not lyrics:
                    from bot.services.lyrics import get_lyrics
                    lyrics = await get_lyrics(result.title, result.artist)

                from bot.services.cache import set_song_cache
                await set_song_cache(result.title, result.artist, {
                    "coverart": result.coverart,
                    "apple_url": result.apple_url,
                    "shazam_url": result.shazam_url,
                    "spotify_url": result.spotify_url,
                    "lyrics": lyrics,
                })

                try:
                    from bot.db.repo import RecognitionRepo, SongCacheRepo, UserRepo
                    from bot.db.session import async_session_factory
                    async with async_session_factory() as session:
                        if lyrics:
                            await SongCacheRepo(session).save(result.title, result.artist, lyrics)
                        await RecognitionRepo(session).log(user_id, result.title, result.artist,
                                                           found_lyrics=bool(lyrics),
                                                           source=result.source)
                        await UserRepo(session).inc_recognitions(user_id)
                        await UserRepo(session).touch(user_id)
                except Exception as e:
                    logger.warning(f"Song DB save failed: {e}")

                from bot.keyboards.inline import song_card_keyboard
                song_key = await store_song_meta(result.title, result.artist)
                kb = song_card_keyboard(
                    apple_url=result.apple_url,
                    shazam_url=result.shazam_url,
                    has_lyrics=bool(lyrics),
                    song_key=song_key,
                    lang=lang,
                    spotify_url=result.spotify_url,
                )
                # Shazam metadata is external text: escape before HTML.
                caption = f"🎵 <b>{esc(result.title)}</b>\n👤 {esc(result.artist)}"

                if result.coverart:
                    try:
                        await _flood_retry(lambda: bot.send_photo(
                            chat_id, photo=result.coverart, caption=caption,
                            parse_mode="HTML", reply_markup=kb))
                        try:
                            await bot.delete_message(chat_id, message_id)
                        except Exception:
                            pass
                        return
                    except Exception as e:
                        logger.warning(f"Cover art send failed, falling back to text: {e}")
                await _safe_edit(bot, chat_id, message_id, caption, parse_mode="HTML", reply_markup=kb)

            except Exception as e:
                logger.error(f"Recognize task error: {e}", exc_info=True)
                await _try_edit(bot, chat_id, message_id, t("shz.not_found", lang))
    finally:
        if temp_path:
            safe_delete(temp_path)
        if own_bot:
            await bot.session.close()


# ── Music (search pick → MP3) ─────────────────────────────────────────────────

_THUMB_MAX_DOWNLOAD = 2 * 1024 * 1024   # never pull more than 2 MB for a thumbnail
_THUMB_MAX_BYTES = 200 * 1024           # Telegram: JPEG, ≤ 200 KB, ≤ 320 px


async def _prepare_thumbnail(url: str) -> Optional[str]:
    """Fetch and shrink a thumbnail to Telegram's limits; None if not possible."""
    raw_path = get_temp_path(suffix=".img", prefix="th_")
    out_path = get_temp_path(suffix=".jpg", prefix="th_")
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"Thumbnail HTTP {resp.status}: {url}")
                    return None
                data = await resp.content.read(_THUMB_MAX_DOWNLOAD + 1)
        if not data or len(data) > _THUMB_MAX_DOWNLOAD:
            return None
        with open(raw_path, "wb") as f:
            f.write(data)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_path(), "-y", "-loglevel", "error", "-i", raw_path,
            "-vf", "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease",
            "-q:v", "5", "-frames:v", "1", out_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=15)
        if proc.returncode != 0 or not os.path.isfile(out_path):
            return None
        if os.path.getsize(out_path) > _THUMB_MAX_BYTES:
            safe_delete(out_path)
            return None
        return out_path
    except Exception as e:
        logger.warning(f"Thumbnail skipped: {e}")
        safe_delete(out_path)
        return None
    finally:
        safe_delete(raw_path)


async def music_download_task(
    ctx: dict, *,
    user_id: int, chat_id: int, message_id: int,
    video_id: str, url: str, title: str, artist: str,
    duration: int = 0, thumbnail: Optional[str] = None, user_lang: str = "en",
    slot_reserved: bool = False,
):
    bot = _get_bot(ctx)
    own_bot = bot is not ctx.get("bot")
    temp_audio: Optional[str] = None
    produced: Optional[str] = None
    temp_thumb: Optional[str] = None
    lang = user_lang or settings.default_lang

    if not slot_reserved:
        await acquire_user_slot(user_id)

    try:
        async with _user_job(bot, chat_id, message_id, lang):
            try:
                from bot.keyboards.inline import audio_result_keyboard
                from bot.services.cache import get_audio_file_id, set_audio_file_id

                song_key = await store_song_meta(title, artist)
                kb = audio_result_keyboard(song_key, lang)

                # Job may have queued behind an identical one
                cached_audio = await get_audio_file_id(video_id)
                if cached_audio:
                    await _flood_retry(lambda: bot.send_audio(
                        chat_id, audio=cached_audio, caption=CAPTION, reply_markup=kb))
                    try:
                        await bot.delete_message(chat_id, message_id)
                    except Exception:
                        pass
                    await _record_download(user_id, url, "youtube", "music", from_cache=True, file_id=cached_audio)
                    return

                if duration and duration > settings.max_audio_duration:
                    raise DownloadError("Track too long", kind="too_long")

                await _safe_edit(bot, chat_id, message_id, t("music.downloading", lang))
                await bot.send_chat_action(chat_id, "upload_voice")

                temp_audio = get_temp_path(suffix=".mp3", prefix="mus_")
                from bot.services.music_search import download_audio
                async with _get_yt_dlp_sem():
                    produced = await download_audio(url, temp_audio)

                if not produced or not os.path.isfile(produced):
                    raise DownloadError("Audio file missing after download", kind="generic")
                if os.path.getsize(produced) > settings.max_file_bytes:
                    raise DownloadError("Too large", kind="too_large")

                if thumbnail:
                    temp_thumb = await _prepare_thumbnail(thumbnail)

                await _safe_edit(bot, chat_id, message_id, t("music.uploading", lang))
                sent = await _flood_retry(lambda: bot.send_audio(
                    chat_id,
                    audio=FSInputFile(produced),
                    title=title,
                    performer=artist,
                    duration=duration or None,
                    thumbnail=FSInputFile(temp_thumb) if temp_thumb else None,
                    caption=CAPTION,
                    reply_markup=kb,
                ))
                if sent.audio is None:
                    raise RuntimeError("Telegram response has no audio object")
                await set_audio_file_id(video_id, sent.audio.file_id)

                await _drop_progress(bot, chat_id, message_id, "✅")
                await _record_download(user_id, url, "youtube", "music", from_cache=False,
                                       file_id=sent.audio.file_id)

            except DownloadError as e:
                logger.info(f"Music download failed ({e.kind}): {str(e)[:200]}")
                await _try_edit(bot, chat_id, message_id, error_text(e.kind, lang))
            except Exception as e:
                logger.error(f"Music download task error: {e}", exc_info=True)
                await _try_edit(bot, chat_id, message_id, t("music.error", lang))
    finally:
        for path in {temp_audio, produced, temp_thumb}:
            if path:
                safe_delete(path)
        await release_user_slot(user_id)
        if own_bot:
            await bot.session.close()


# ── Broadcast ─────────────────────────────────────────────────────────────────

BROADCAST_TIMEOUT = 12 * 3600


def _cta_markup(cta_text: Optional[str], cta_url: Optional[str]):
    if not (cta_text and cta_url):
        return None
    from aiogram.types import InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=cta_text, url=cta_url))
    return b.as_markup()


async def broadcast_task(
    ctx: dict, *,
    broadcast_id: str, admin_chat_id: int, status_message_id: int,
    from_chat_id: int, message_id: int,
    cta_text: Optional[str] = None, cta_url: Optional[str] = None,
    total: int = 0,
):
    """Deliver an admin broadcast; resumable (state in Redis) and rate-limited."""
    import json

    from bot.db.repo import UserRepo
    from bot.db.session import async_session_factory
    from bot.services.broadcast import BroadcastProgress, run_broadcast
    from bot.services.cache import BROADCAST_STATE, get_redis, release_broadcast_lock

    bot = _get_bot(ctx)
    own_bot = bot is not ctx.get("bot")
    r = await get_redis()
    state_key = f"{BROADCAST_STATE}:{broadcast_id}"
    raw = await r.get(state_key)
    progress = BroadcastProgress.from_dict(json.loads(raw) if raw else None)
    started = time.monotonic()
    if raw:
        logger.info(f"Broadcast {broadcast_id} resuming from tg_id>{progress.last_id}")

    async def fetch_page(last_id: int, limit: int) -> list[int]:
        async with async_session_factory() as session:
            return await UserRepo(session).broadcast_ids_after(last_id, limit)

    async def mark_blocked(ids: list[int]) -> None:
        try:
            async with async_session_factory() as session:
                await UserRepo(session).set_blocked_bot(ids)
        except Exception as e:
            logger.warning(f"Broadcast: mark blocked failed: {e}")

    async def save_progress(p: BroadcastProgress) -> None:
        await r.set(state_key, json.dumps(p.to_dict()), ex=BROADCAST_TIMEOUT * 2)

    def report(p: BroadcastProgress, done: bool) -> str:
        head = "📊 <b>Broadcast yakunlandi</b>" if done else "📤 <b>Broadcast davom etmoqda...</b>"
        return (
            f"{head}\n\n"
            f"📨 Jarayon: <b>{p.processed}</b> / {total or '?'}\n"
            f"✅ Yetkazildi: <b>{p.delivered}</b>\n"
            f"🚫 Botni bloklagan: <b>{p.blocked}</b>\n"
            f"⚠️ Muvaffaqiyatsiz: <b>{p.failed}</b>\n"
            f"⏱ Vaqt: <b>{int(time.monotonic() - started)}s</b>"
        )

    async def on_progress(p: BroadcastProgress) -> None:
        await _try_edit(bot, admin_chat_id, status_message_id, report(p, done=False), parse_mode="HTML")

    try:
        progress = await run_broadcast(
            bot,
            from_chat_id=from_chat_id, message_id=message_id,
            reply_markup=_cta_markup(cta_text, cta_url),
            progress=progress,
            fetch_page=fetch_page, mark_blocked=mark_blocked,
            save_progress=save_progress, on_progress=on_progress,
        )
        await r.delete(state_key)
        await release_broadcast_lock()
        from bot.keyboards.inline import admin_back_keyboard
        await _try_edit(bot, admin_chat_id, status_message_id, report(progress, done=True),
                        parse_mode="HTML", reply_markup=admin_back_keyboard())
        logger.info(f"Broadcast {broadcast_id} done: {progress.to_dict()}")
    except asyncio.CancelledError:
        # Worker shutdown/timeout: keep state + lock so the retry resumes.
        await asyncio.shield(save_progress(progress))
        raise
    finally:
        if own_bot:
            await bot.session.close()


# ── Maintenance ───────────────────────────────────────────────────────────────

async def cleanup_task(ctx: dict):
    """Sweep our own stale temp files (<download_dir>/tmp, known prefixes only)."""
    from bot.services.media import cleanup_old_files
    removed = await cleanup_old_files(max_age_seconds=3600)
    logger.info(f"Cleanup task completed: {removed} stale temp file(s) removed")


def _redis_settings() -> RedisSettings:
    # redis_dsn injects REDIS_PASSWORD when REDIS_URL has none (prod compose)
    return RedisSettings.from_dsn(settings.redis_dsn)


class WorkerSettings:
    """Default worker: downloads from arq:queue; also owns the cleanup cron."""
    functions = [
        download_task, recognize_task, music_download_task, cleanup_task,
        # Long-running; retried (and resumed from Redis state) after a restart.
        func(broadcast_task, name="broadcast_task", timeout=BROADCAST_TIMEOUT, max_tries=5),
    ]
    redis_settings = _redis_settings()
    queue_name = "arq:queue"
    max_jobs = settings.worker_concurrency
    job_timeout = settings.job_timeout
    keep_result = 60
    on_startup = worker_startup
    on_shutdown = worker_shutdown
    # Every 30 min; unique=True → one run across all replicas.
    cron_jobs = [cron(cleanup_task, minute={0, 30}, unique=True, run_at_startup=True)]


class RecognitionWorkerSettings:
    """Dedicated worker for voice/Shazam recognition — separate lane from downloads."""
    functions = [recognize_task]
    redis_settings = _redis_settings()
    queue_name = "arq:queue:recognition"
    max_jobs = settings.worker_concurrency
    job_timeout = settings.job_timeout
    keep_result = 60
    on_startup = worker_startup
    on_shutdown = worker_shutdown


class MusicWorkerSettings:
    """Dedicated worker for music search downloads — separate lane."""
    functions = [music_download_task]
    redis_settings = _redis_settings()
    queue_name = "arq:queue:music"
    max_jobs = settings.worker_concurrency
    job_timeout = settings.job_timeout
    keep_result = 60
    on_startup = worker_startup
    on_shutdown = worker_shutdown
