#!/usr/bin/env python3
"""End-to-end live test: drives the REAL running workers via arq and sends
results to the admin's Telegram chat.

Requires: bot + workers running, Redis up, .env configured.
Usage: python scripts/e2e_live_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import settings  # noqa: E402

CHAT_ID = settings.admin_ids[0]
YT_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # 19s public video


async def wait_for(check, timeout=180, interval=2, label=""):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = await check()
        if v:
            return v
        await asyncio.sleep(interval)
    print(f"  [TIMEOUT] {label} after {timeout}s")
    return None


async def main():
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from arq import create_pool
    from arq.connections import RedisSettings

    from bot.services.cache import get_file_id, get_redis
    from bot.services.downloader import normalize_url

    bot = Bot(token=settings.bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    r = await get_redis()
    results: dict[str, bool] = {}

    url = normalize_url(YT_URL)

    try:
        # ── 1. download_task (fresh download; clear cache first) ──────────
        print("1) download_task: fresh YouTube download")
        await r.delete(*[k async for k in r.scan_iter("fid:*")] or ["_none"])
        m = await bot.send_message(CHAT_ID, "⏳ [E2E] test 1: fresh download...")
        await pool.enqueue_job("download_task", user_id=CHAT_ID, chat_id=CHAT_ID,
                               message_id=m.message_id, url=url, platform="youtube",
                               quality="720", user_lang="uz", _queue_name="arq:queue")
        fid = await wait_for(lambda: get_file_id(url, "720"), timeout=180, label="download")
        results["download_fresh"] = bool(fid)
        print(f"  -> file_id cached: {bool(fid)}")

        # ── 2. duplicate request → served from cache instantly ───────────
        print("2) download_task: duplicate (cache hit path)")
        m2 = await bot.send_message(CHAT_ID, "⏳ [E2E] test 2: duplicate URL (should be instant)...")
        t0 = time.monotonic()
        await pool.enqueue_job("download_task", user_id=CHAT_ID, chat_id=CHAT_ID,
                               message_id=m2.message_id, url=url, platform="youtube",
                               quality="720", user_lang="uz", _queue_name="arq:queue")

        async def _msg_deleted():
            # done when the progress message was deleted by the worker
            try:
                await bot.edit_message_text("probe", chat_id=CHAT_ID, message_id=m2.message_id)
                return False
            except Exception as e:
                return "not found" in str(e).lower() or "message to edit" in str(e).lower()
        done = await wait_for(_msg_deleted, timeout=60, label="cached send")
        dt = time.monotonic() - t0
        results["download_cached"] = bool(done)
        print(f"  -> served from cache in {dt:.1f}s")

        # ── 3. mp3 variant ────────────────────────────────────────────────
        print("3) download_task: mp3 quality")
        m3 = await bot.send_message(CHAT_ID, "⏳ [E2E] test 3: MP3 extract...")
        await pool.enqueue_job("download_task", user_id=CHAT_ID, chat_id=CHAT_ID,
                               message_id=m3.message_id, url=url, platform="youtube",
                               quality="mp3", user_lang="uz", _queue_name="arq:queue")
        fid3 = await wait_for(lambda: get_file_id(url, "mp3"), timeout=180, label="mp3")
        results["download_mp3"] = bool(fid3)
        print(f"  -> mp3 file_id cached: {bool(fid3)}")

        # ── 4. music_download_task (text search → mp3) ────────────────────
        print("4) music search → music_download_task")
        from bot.services.music_search import search_songs
        entries = await search_songs("Ed Sheeran Shape of You", max_results=5)
        results["music_search"] = len(entries) > 0
        print(f"  -> search results: {len(entries)}")
        if entries:
            e = entries[0]
            m4 = await bot.send_message(CHAT_ID, "⏳ [E2E] test 4: music download...")
            await pool.enqueue_job("music_download_task", user_id=CHAT_ID, chat_id=CHAT_ID,
                                   message_id=m4.message_id, video_id=e.id, url=e.url,
                                   title=e.title, artist=e.uploader, duration=e.duration,
                                   thumbnail=e.thumbnail, user_lang="uz",
                                   _queue_name="arq:queue:music")
            from bot.services.cache import get_audio_file_id
            aid = await wait_for(lambda: get_audio_file_id(e.id), timeout=240, label="music dl")
            results["music_download"] = bool(aid)
            print(f"  -> audio file_id cached: {bool(aid)}")

        # ── 5. recognize_task (voice fixture → Shazam) ────────────────────
        print("5) recognize_task: voice fixture via Telegram")
        from aiogram.types import FSInputFile
        voice_msg = await bot.send_voice(CHAT_ID, FSInputFile("tests/fixtures/voice_11s.ogg"),
                                         caption="[E2E] test 5: recognition input")
        m5 = await bot.send_message(CHAT_ID, "🎧 [E2E] test 5: recognizing...")
        await pool.enqueue_job("recognize_task", user_id=CHAT_ID, chat_id=CHAT_ID,
                               message_id=m5.message_id, file_id=voice_msg.voice.file_id,
                               file_suffix=".ogg", user_lang="uz",
                               _queue_name="arq:queue:recognition")

        async def _recog_done():
            # Done only when the worker DELETED the progress message (it does
            # that right before posting the song card). Probe with unique text
            # so "message is not modified" can never fire: with a constant
            # probe text the first call raised "not modified" and this check
            # passed before the worker had even started (false positive).
            # On a worker error the message is edited to an error text and
            # stays editable, so this correctly keeps returning False.
            try:
                await bot.edit_message_text(f"🎧 [E2E] test 5: probe {time.monotonic():.3f}",
                                            chat_id=CHAT_ID, message_id=m5.message_id)
                return False
            except Exception as e:
                s = str(e).lower()
                return "not found" in s or "message to edit" in s
        rec = await wait_for(_recog_done, timeout=120, label="recognition")
        results["recognition"] = bool(rec)
        print(f"  -> recognition finished: {bool(rec)}")

        await bot.send_message(CHAT_ID, "✅ [E2E] all tests dispatched — natijalarni tekshiring")

    finally:
        await pool.aclose()
        await bot.session.close()

    print()
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    ok = all(results.values())
    print(f"\n{'ALL PASS' if ok else 'SOME FAILED'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
