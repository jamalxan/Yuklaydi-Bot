from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Optional

import re

import aiohttp
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, FSInputFile

from bot.config import settings
from bot.logger import logger
from bot.services.cache import (
    acquire_single_flight, acquire_user_slot,
    get_file_id, release_single_flight, release_user_slot,
    set_file_id, store_song_meta, store_url_key, store_video_for_shazam,
)
from bot.services.downloader import DownloadError, DownloadResult, download
from bot.services.media import get_temp_path, safe_delete
from bot.services.recognizer import recognize

# Semaphore caps concurrent yt-dlp calls in this worker process (CPU/network guard)
_YT_DLP_SEM: Optional[asyncio.Semaphore] = None


def _get_yt_dlp_sem() -> asyncio.Semaphore:
    global _YT_DLP_SEM
    if _YT_DLP_SEM is None:
        _YT_DLP_SEM = asyncio.Semaphore(settings.yt_dlp_worker_concurrency)
    return _YT_DLP_SEM


async def _try_instagram_photo(bot: Bot, chat_id: int, message_id: int, url: str) -> bool:
    """Fallback: if Instagram post has no video, download and send the photo(s)."""
    m = re.search(r'/(?:p|reel|tv)/([A-Za-z0-9_-]+)', url)
    if not m:
        return False
    shortcode = m.group(1)
    try:
        import instaloader
        loop = asyncio.get_event_loop()

        def _get_post():
            L = instaloader.Instaloader(quiet=True, save_metadata=False, download_comments=False)
            return instaloader.Post.from_shortcode(L.context, shortcode)

        post = await loop.run_in_executor(None, _get_post)
        if post.is_video:
            return False  # video but login needed — caller handles error

        # Collect all image URLs (sidecar carousel or single photo)
        try:
            photo_urls = [node.display_url for node in post.get_sidecar_nodes()]
        except Exception:
            photo_urls = []
        if not photo_urls:
            photo_urls = [post.url]

        async with aiohttp.ClientSession() as session:
            for idx, photo_url in enumerate(photo_urls[:10]):
                async with session.get(photo_url) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.read()
                photo_file = BufferedInputFile(data, filename=f"photo_{idx+1}.jpg")
                await bot.send_photo(chat_id, photo=photo_file,
                                     caption="🤖 @vidyuklaydi_bot" if idx == 0 else None)

        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning(f"Instagram photo fallback: {e}")
        return False


_DL = {
    "downloading": {"uz": "⏳ Yuklanmoqda...", "ru": "⏳ Скачивается...", "en": "⏳ Downloading..."},
    "uploading":   {"uz": "📤 Yuborilmoqda...", "ru": "📤 Отправляется...", "en": "📤 Uploading..."},
    "done":        {"uz": "✅ Tayyor!", "ru": "✅ Готово!", "en": "✅ Done!"},
}

_ERR = {
    "private":     {"uz": "🔒 Bu video shaxsiy yoki o'chirilgan.", "ru": "🔒 Это видео приватное или удалено.", "en": "🔒 This video is private or has been deleted."},
    "geo":         {"uz": "🌍 Bu video hududingizda mavjud emas.", "ru": "🌍 Это видео недоступно в вашем регионе.", "en": "🌍 This video is not available in your region."},
    "age":         {"uz": "🔞 Bu video yoshi cheklangan.", "ru": "🔞 Это видео имеет возрастное ограничение.", "en": "🔞 This video has age restrictions."},
    "unsupported": {"uz": "❌ Ushbu havola qo'llab-quvvatlanmaydi.", "ru": "❌ Эта ссылка не поддерживается.", "en": "❌ This link is not supported."},
    "no_video":    {"uz": "📷 Bu postda video yo'q yoki Instagram login talab qilmoqda.\n💡 Reel havolasini yuboring: instagram.com/reel/...", "ru": "📷 В этом посте нет видео или Instagram требует авторизацию.\n💡 Попробуйте ссылку на Reel: instagram.com/reel/...", "en": "📷 No video in this post or Instagram requires login.\n💡 Try a Reel link: instagram.com/reel/..."},
    "timeout":     {"uz": "⏱ Server bilan aloqa yo'q. Keyinroq urinib ko'ring.", "ru": "⏱ Нет связи с сервером. Попробуйте позже.", "en": "⏱ Connection timed out. Please try again later."},
    "generic":     {"uz": "❌ Yuklab olishda xatolik.", "ru": "❌ Ошибка при скачивании.", "en": "❌ Download failed. Please try again later."},
    "too_large":   {"uz": f"📦 Fayl juda katta (maks {settings.max_file_mb}MB).", "ru": f"📦 Файл слишком большой (макс {settings.max_file_mb}МБ).", "en": f"📦 File too large (max {settings.max_file_mb}MB)."},
}

_NOT_FOUND = {
    "uz": "😔 Qo'shiqni aniqlab bo'lmadi.\n💡 10–15 soniyalik musiqali qism yuboring.",
    "ru": "😔 Не удалось распознать песню.\n💡 Попробуйте 10–15-секундный фрагмент.",
    "en": "😔 Could not recognize the song.\n💡 Try a clear 10–15 second musical clip.",
}


def _make_bot() -> Bot:
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    session = (
        AiohttpSession(api=TelegramAPIServer.from_base(settings.local_api_url, is_local=True))
        if settings.use_local_api
        else AiohttpSession()
    )
    return Bot(token=settings.bot_token, session=session,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))


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


def _log_timing(event: str, **fields) -> None:
    logger.info(json.dumps({"event": event, **fields}))


async def download_task(
    ctx: dict, *,
    user_id: int, chat_id: int, message_id: int,
    url: str, platform: str, quality: str, user_lang: str,
):
    bot = _make_bot()
    temp_path: Optional[str] = None
    lang = user_lang or "en"
    t_total_start = time.monotonic()
    sf_acquired = False

    # Track this active download for per-user fairness
    await acquire_user_slot(user_id)

    try:
        suffix = ".mp3" if quality == "mp3" else ".mp4"
        temp_path = get_temp_path(suffix=suffix)

        await _safe_edit(bot, chat_id, message_id, _DL["downloading"].get(lang, "⏳ Downloading..."))

        # ── Single-flight: prevent duplicate concurrent downloads of same URL ──
        # If another worker is downloading this URL+quality, wait up to job_timeout
        # for the cache key to appear and serve from cache; else download ourselves.
        sf_acquired = await acquire_single_flight(url, quality, ttl=settings.job_timeout)
        if not sf_acquired:
            # Wait for the concurrent download to populate cache. Also keep trying
            # to grab the lock ourselves: if the other worker's download fails fast
            # (bad URL, network error) it releases the lock almost immediately, and
            # without this we'd otherwise sit idle for the full job_timeout before
            # noticing — starving this request for no reason.
            file_id_from_flight: Optional[str] = None
            for _ in range(settings.job_timeout):
                await asyncio.sleep(1)
                file_id_from_flight = await get_file_id(url, quality)
                if file_id_from_flight:
                    break
                sf_acquired = await acquire_single_flight(url, quality, ttl=settings.job_timeout)
                if sf_acquired:
                    break

            if file_id_from_flight:
                # Serve from cache populated by the other worker
                shazam_key = await store_video_for_shazam(file_id_from_flight, source_url=url)
                url_key = await store_url_key(url)
                from bot.keyboards.inline import video_result_keyboard
                kb = video_result_keyboard(url_key, shazam_key, lang)
                if quality == "mp3":
                    await bot.send_audio(chat_id, audio=file_id_from_flight, caption="🤖 @vidyuklaydi_bot")
                else:
                    await bot.send_video(chat_id, video=file_id_from_flight, caption="🤖 @vidyuklaydi_bot",
                                         supports_streaming=True, reply_markup=kb)
                try:
                    await bot.delete_message(chat_id, message_id)
                except Exception:
                    pass
                _log_timing("download_complete", platform=platform, quality=quality,
                            t_extract=0, t_download=0, t_upload=0,
                            t_total=round(time.monotonic() - t_total_start, 2),
                            from_cache=True, single_flight=True, local_api=settings.use_local_api)
                return
            # Neither a cached result nor the lock showed up within job_timeout —
            # fall through and download ourselves. (If the loop above already won
            # the lock, sf_acquired is already True — don't re-acquire, that would
            # fail against our own held key and leak it until TTL expiry.)
            if not sf_acquired:
                sf_acquired = await acquire_single_flight(url, quality, ttl=settings.job_timeout)

        # ── Phase B: download (guarded by per-worker concurrency semaphore) ───
        async with _get_yt_dlp_sem():
            t_dl_start = time.monotonic()
            result: DownloadResult = await download(url, temp_path, quality)
            t_download = time.monotonic() - t_dl_start

        actual_path = result.path
        if not os.path.exists(actual_path) or result.size_bytes == 0:
            raise DownloadError("Download produced empty or missing file", kind="generic")
        if result.size_bytes > settings.max_file_bytes:
            raise DownloadError("Too large", kind="too_large")

        await _safe_edit(bot, chat_id, message_id, _DL["uploading"].get(lang, "📤 Uploading..."))
        await bot.send_chat_action(chat_id, "upload_video" if quality != "mp3" else "upload_voice")

        # ── Phase C: upload ───────────────────────────────────────────────
        t_up_start = time.monotonic()
        input_file = FSInputFile(actual_path)
        size_mb = round(result.size_bytes / 1024 / 1024, 1)

        if quality == "mp3":
            sent = await bot.send_audio(
                chat_id, audio=input_file,
                caption="🤖 @vidyuklaydi_bot",
            )
            assert sent.audio is not None
            file_id = sent.audio.file_id
        else:
            sent = await bot.send_video(
                chat_id, video=input_file,
                caption="🤖 @vidyuklaydi_bot",
                supports_streaming=True,
            )
            assert sent.video is not None
            file_id = sent.video.file_id

        t_upload = time.monotonic() - t_up_start
        t_total = time.monotonic() - t_total_start

        # Cache the file_id (other single-flight waiters will pick this up)
        await set_file_id(url, quality, file_id)

        # Attach inline keyboard for videos
        if quality != "mp3":
            shazam_key = await store_video_for_shazam(file_id, source_url=url)
            url_key = await store_url_key(url)
            from bot.keyboards.inline import video_result_keyboard
            kb = video_result_keyboard(url_key, shazam_key, lang)
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=sent.message_id, reply_markup=kb
                )
            except Exception:
                pass

        # Delete progress message
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            try:
                await _safe_edit(bot, chat_id, message_id, _DL["done"].get(lang, "✅ Done!"))
            except Exception:
                pass

        # Structured timing log
        _log_timing("download_complete",
                    platform=platform, quality=quality, size_mb=size_mb,
                    t_extract=round(result.t_extract, 2),
                    t_download=round(t_download, 2),
                    t_upload=round(t_upload, 2),
                    t_total=round(t_total, 2),
                    from_cache=False,
                    single_flight=True,
                    local_api=settings.use_local_api)

        # DB stats
        try:
            from bot.db.session import async_session_factory
            from bot.db.repo import DownloadRepo, UserRepo
            async with async_session_factory() as session:
                await UserRepo(session).inc_downloads(user_id)
                await UserRepo(session).touch(user_id)
                repo = DownloadRepo(session)
                await repo.inc_daily()
                await repo.create(user_id, url, platform, "video" if quality != "mp3" else "audio")
        except Exception as e:
            logger.warning(f"DB stat update failed: {e}")

        # Track timing in Redis for admin stats
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
        # Instagram photo post fallback: send as photo instead of showing error
        if e.kind == "no_video" and platform == "instagram":
            sent_photo = await _try_instagram_photo(bot, chat_id, message_id, url)
            if sent_photo:
                return
        err = _ERR.get(e.kind, _ERR["generic"]).get(lang, "❌ Error.")
        try:
            await _safe_edit(bot, chat_id, message_id, err)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Download task error: {e}")
        try:
            await _safe_edit(bot, chat_id, message_id, _ERR["generic"].get(lang, "❌ Error."))
        except Exception:
            pass
    finally:
        if temp_path:
            safe_delete(temp_path)
            # Also clean yt-dlp's mp3 output: base path + ".mp3"
            base = temp_path.replace(".mp4", "").replace(".mp3", "")
            for ext in (".mp3", ".m4a", ".webm", ".opus"):
                safe_delete(base + ext)
        if sf_acquired:
            await release_single_flight(url, quality)
        await release_user_slot(user_id)
        await bot.session.close()


async def _download_for_recognize(bot: Bot, file_id: str, file_suffix: str,
                                   temp_path: str, source_url: str = "") -> bool:
    """Download media for recognition. Returns True on success."""
    use_local = settings.use_local_api
    logger.debug(f"[recognize_dl] file_id={file_id!r} suffix={file_suffix!r} use_local_api={use_local} local_url={settings.local_api_url!r}")
    # Primary path: download from Telegram (works for files ≤ 20 MB on cloud API)
    try:
        file_info = await bot.get_file(file_id)
        logger.debug(f"[recognize_dl] get_file ok → file_path={file_info.file_path!r} file_size={getattr(file_info,'file_size',None)}")
        if not file_info.file_path:
            raise RuntimeError("getFile returned no file_path")
        await bot.download_file(file_info.file_path, destination=temp_path)
        size = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
        logger.debug(f"[recognize_dl] download_file → local_size={size}B dest={temp_path!r}")
        if size > 0:
            logger.debug(f"[recognize_dl] SUCCESS via Telegram ({size//1024}KB)")
            return True
        logger.warning("[recognize_dl] download succeeded but file is empty")
    except Exception as e:
        logger.warning(f"[recognize_dl] Telegram getFile FAILED: {type(e).__name__}: {e}")

    # Fallback: re-extract audio from original URL via yt-dlp (bypasses 20 MB limit)
    if source_url:
        try:
            from bot.services.downloader import _download_sync
            audio_path = temp_path.replace(file_suffix, ".m4a")
            loop = asyncio.get_event_loop()
            from functools import partial
            res = await loop.run_in_executor(
                None,
                partial(_download_sync, source_url, audio_path, "mp3"),
            )
            if os.path.exists(res.path) and res.size_bytes > 0:
                import shutil
                shutil.move(res.path, temp_path)
                logger.info(f"Recognize: downloaded {res.size_bytes//1024}KB via yt-dlp from {source_url}")
                return True
        except Exception as e:
            logger.warning(f"Recognize: yt-dlp audio fallback failed: {e}")

    return False


async def recognize_task(
    ctx: dict, *,
    user_id: int, chat_id: int, message_id: int,
    file_id: str, file_suffix: str, user_lang: str,
    source_url: str = "",
):
    bot = _make_bot()
    temp_path: Optional[str] = None
    lang = user_lang or "en"

    try:
        temp_path = get_temp_path(suffix=file_suffix, prefix="shz_in_")
        ok = await _download_for_recognize(bot, file_id, file_suffix, temp_path, source_url)
        if not ok:
            logger.error(f"Recognize: failed to download file {file_id}")
            await _safe_edit(bot, chat_id, message_id, _NOT_FOUND.get(lang, _NOT_FOUND["en"]))
            return

        await bot.send_chat_action(chat_id, "record_voice")
        result = await recognize(temp_path)

        if not result:
            await _safe_edit(bot, chat_id, message_id, _NOT_FOUND.get(lang, _NOT_FOUND["en"]))
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
            from bot.db.session import async_session_factory
            from bot.db.repo import RecognitionRepo, SongCacheRepo, UserRepo
            async with async_session_factory() as session:
                if lyrics:
                    await SongCacheRepo(session).save(result.title, result.artist, lyrics)
                await RecognitionRepo(session).inc_daily()
                await RecognitionRepo(session).log(user_id, result.title, result.artist,
                                                    found_lyrics=bool(lyrics), source=result.source)
                await UserRepo(session).inc_recognitions(user_id)
                await UserRepo(session).touch(user_id)
        except Exception as e:
            logger.warning(f"Song DB save failed: {e}")

        from bot.keyboards.inline import song_card_keyboard
        song_key = await store_song_meta(result.title, result.artist)
        kb = song_card_keyboard(
            apple_url=result.apple_url,
            shazam_url=result.shazam_url,
            spotify_url=result.spotify_url,
            has_lyrics=bool(lyrics),
            song_key=song_key,
            lang=lang,
        )
        caption = f"🎵 <b>{result.title}</b>\n👤 {result.artist}"

        if result.coverart:
            try:
                await bot.delete_message(chat_id, message_id)
                await bot.send_photo(chat_id, photo=result.coverart, caption=caption,
                                     parse_mode="HTML", reply_markup=kb)
            except Exception:
                await _safe_edit(bot, chat_id, message_id, caption, parse_mode="HTML", reply_markup=kb)
        else:
            await _safe_edit(bot, chat_id, message_id, caption, parse_mode="HTML", reply_markup=kb)

    except Exception as e:
        logger.error(f"Recognize task error: {e}")
        try:
            await _safe_edit(bot, chat_id, message_id, _NOT_FOUND.get(lang, _NOT_FOUND["en"]))
        except Exception:
            pass
    finally:
        if temp_path:
            safe_delete(temp_path)
        await bot.session.close()


async def music_download_task(
    ctx: dict, *,
    user_id: int, chat_id: int, message_id: int,
    video_id: str, url: str, title: str, artist: str,
    duration: int = 0, thumbnail: Optional[str] = None, user_lang: str = "en",
):
    bot = _make_bot()
    temp_audio: Optional[str] = None
    temp_thumb: Optional[str] = None
    lang = user_lang or "en"

    try:
        await _safe_edit(bot, chat_id, message_id,
                         {"uz": "⏳ Yuklanmoqda...", "ru": "⏳ Скачивается...", "en": "⏳ Downloading..."}.get(lang, "⏳"))
        await bot.send_chat_action(chat_id, "upload_voice")

        temp_audio = get_temp_path(suffix=".mp3", prefix="mus_")
        from bot.services.music_search import download_audio
        await download_audio(url, temp_audio)

        if not os.path.exists(temp_audio):
            raise RuntimeError("Audio file missing after download")

        thumb_input = None
        if thumbnail:
            try:
                import aiohttp
                temp_thumb = get_temp_path(suffix=".jpg", prefix="th_")
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(thumbnail) as resp:
                        if resp.status == 200:
                            with open(temp_thumb, "wb") as f:
                                f.write(await resp.read())
                            thumb_input = FSInputFile(temp_thumb)
                        else:
                            logger.warning(f"Thumbnail download failed with status {resp.status}: {thumbnail}")
            except Exception as e:
                logger.warning(f"Failed to download thumbnail: {e}")
                thumb_input = None

        await _safe_edit(bot, chat_id, message_id,
                         {"uz": "📤 Yuborilmoqda...", "ru": "📤 Отправляется...", "en": "📤 Uploading..."}.get(lang, "📤"))

        from bot.keyboards.inline import audio_result_keyboard
        song_key = await store_song_meta(title, artist)
        kb = audio_result_keyboard(song_key, lang)

        sent = await bot.send_audio(
            chat_id,
            audio=FSInputFile(temp_audio),
            title=title,
            performer=artist,
            duration=duration or None,
            thumbnail=thumb_input,
            caption="🤖 @vidyuklaydi_bot",
            reply_markup=kb,
        )

        from bot.services.cache import set_audio_file_id
        assert sent.audio is not None
        await set_audio_file_id(video_id, sent.audio.file_id)

        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            try:
                await _safe_edit(bot, chat_id, message_id, "✅")
            except Exception:
                pass

        try:
            from bot.db.session import async_session_factory
            from bot.db.repo import MusicSearchRepo, UserRepo
            async with async_session_factory() as session:
                await MusicSearchRepo(session).inc_daily_audio()
                await UserRepo(session).touch(user_id)
        except Exception as e:
            logger.warning(f"Music download DB: {e}")

    except Exception as e:
        logger.error(f"Music download task error: {e}")
        try:
            await _safe_edit(bot, chat_id, message_id,
                             {"uz": "❌ Xatolik.", "ru": "❌ Ошибка.", "en": "❌ Error."}.get(lang, "❌"))
        except Exception:
            pass
    finally:
        if temp_audio:
            safe_delete(temp_audio)
        if temp_thumb:
            safe_delete(temp_thumb)
        await bot.session.close()


async def cleanup_task(ctx: dict):
    from bot.services.media import cleanup_old_files
    await cleanup_old_files(settings.download_dir, max_age_seconds=3600)
    logger.info("Cleanup task completed")


class WorkerSettings:
    """Default worker: handles downloads from arq:queue."""
    from arq.connections import RedisSettings as _RS
    functions = [download_task, recognize_task, music_download_task, cleanup_task]
    redis_settings = _RS.from_dsn(settings.redis_url)
    queue_name = "arq:queue"
    max_jobs = settings.worker_concurrency
    job_timeout = settings.job_timeout
    keep_result = 60
    # Periodic disk cleanup every hour
    cron_jobs: list[Any] = []


class RecognitionWorkerSettings:
    """Dedicated worker for voice/Shazam recognition — separate lane from downloads."""
    from arq.connections import RedisSettings as _RS
    functions = [recognize_task, cleanup_task]
    redis_settings = _RS.from_dsn(settings.redis_url)
    queue_name = "arq:queue:recognition"
    max_jobs = settings.worker_concurrency
    job_timeout = settings.job_timeout
    keep_result = 60


class MusicWorkerSettings:
    """Dedicated worker for music search downloads — separate lane."""
    from arq.connections import RedisSettings as _RS
    functions = [music_download_task, cleanup_task]
    redis_settings = _RS.from_dsn(settings.redis_url)
    queue_name = "arq:queue:music"
    max_jobs = settings.worker_concurrency
    job_timeout = settings.job_timeout
    keep_result = 60