#!/usr/bin/env python3
"""Full-system stress + speed test through the REAL running bot/workers.

  1. Video→Shazam pipeline: upload a music video, recognize via worker (file_id path)
  2. Parallel downloads: 3 different YouTube videos at once through the queue
  3. Cache hit latency for an already-downloaded URL
  4. Redis ops latency under 2000-command burst
  5. Queue depth sanity after everything drains

Usage: python scripts/stress_full_test.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import settings  # noqa: E402

CHAT = settings.admin_ids[0]

# Three small public YouTube videos (distinct URLs → parallel yt-dlp)
PARALLEL_URLS = [
    "https://www.youtube.com/watch?v=jNQXAC9IVRw",   # "Me at the zoo", 19s
    "https://www.youtube.com/watch?v=C0DPdy98e4c",   # "TEST VIDEO", 20s
    "https://www.youtube.com/watch?v=0lJykuiS_9s",   # 88s, small
]


async def wait_for(check, timeout=240, interval=2, label=""):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = await check()
        if v:
            return v
        await asyncio.sleep(interval)
    print(f"  [TIMEOUT] {label}")
    return None


async def main():
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.types import FSInputFile
    from arq import create_pool
    from arq.connections import RedisSettings

    from bot.services.cache import get_file_id, get_queue_depth, get_redis
    from bot.services.downloader import normalize_url
    from bot.services.media import ffmpeg_path, get_temp_path, safe_delete

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    r = await get_redis()
    results: dict[str, tuple[bool, str]] = {}

    try:
        # ── 1. Video → Shazam via real worker (Telegram file_id path) ─────
        print("1) video→Shazam pipeline through recognition worker")
        mv = get_temp_path(suffix=".mp4", prefix="stress_mv_")
        subprocess.run([ffmpeg_path(), "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=16",
                        "-i", "tests/fixtures/voice_16s.ogg", "-c:v", "libx264",
                        "-preset", "ultrafast", "-c:a", "aac", "-shortest", mv],
                       check=True, capture_output=True)
        sent = await bot.send_video(CHAT, FSInputFile(mv), caption="[STRESS] video→Shazam input")
        m1 = await bot.send_message(CHAT, "🎧 [STRESS] recognizing from video...")
        t0 = time.monotonic()
        await pool.enqueue_job("recognize_task", user_id=CHAT, chat_id=CHAT,
                               message_id=m1.message_id, file_id=sent.video.file_id,
                               file_suffix=".mp4", user_lang="uz",
                               _queue_name="arq:queue:recognition")

        async def _recog_done():
            # Probe with UNIQUE text: "message is not modified" then can't
            # happen, so the only failure meaning "done" is message-deleted
            # (worker deletes the progress message when it posts the song card).
            try:
                await bot.edit_message_text(f"🎧 [STRESS] probe {time.monotonic():.3f}",
                                            chat_id=CHAT, message_id=m1.message_id)
                return False
            except Exception as e:
                s = str(e).lower()
                return "not found" in s or "message to edit" in s
        done = await wait_for(_recog_done, timeout=120, label="video shazam")
        dt = time.monotonic() - t0
        results["video_shazam_pipeline"] = (bool(done), f"{dt:.1f}s")
        safe_delete(mv)
        print(f"  -> {bool(done)} in {dt:.1f}s")

        # ── 2. Three parallel downloads through the queue ─────────────────
        print("2) 3 parallel downloads (distinct URLs)")
        urls = [normalize_url(u) for u in PARALLEL_URLS]
        for u in urls:
            h = __import__("hashlib").sha256(f"{u}|720".encode()).hexdigest()
            await r.delete(f"fid:{h}")
        msgs = []
        for i, u in enumerate(urls):
            m = await bot.send_message(CHAT, f"⏳ [STRESS] parallel dl {i+1}/3...")
            msgs.append(m)
        t0 = time.monotonic()
        for m, u in zip(msgs, urls):
            await pool.enqueue_job("download_task", user_id=CHAT, chat_id=CHAT,
                                   message_id=m.message_id, url=u, platform="youtube",
                                   quality="720", user_lang="uz", _queue_name="arq:queue")

        async def _all_cached():
            vals = await asyncio.gather(*[get_file_id(u, "720") for u in urls])
            return all(vals)
        done2 = await wait_for(_all_cached, timeout=300, label="parallel downloads")
        dt2 = time.monotonic() - t0
        results["parallel_downloads"] = (bool(done2), f"3 videos in {dt2:.1f}s")
        print(f"  -> all cached: {bool(done2)} in {dt2:.1f}s")

        # ── 3. Cache-hit latency (handler fast path equivalent) ───────────
        print("3) cache-hit latency")
        t0 = time.monotonic()
        fid = await get_file_id(urls[0], "720")
        lat_ms = (time.monotonic() - t0) * 1000
        results["cache_latency"] = (bool(fid) and lat_ms < 100, f"{lat_ms:.1f}ms")
        print(f"  -> {lat_ms:.2f}ms")

        # ── 4. Redis burst: 2000 pipelined ops ────────────────────────────
        print("4) redis 2000-op burst")
        t0 = time.monotonic()
        pipe = r.pipeline()
        for i in range(2000):
            await pipe.setex(f"bench:{i}", 60, "x")
        await pipe.execute()
        dt4 = time.monotonic() - t0
        ops = 2000 / dt4
        results["redis_burst"] = (ops > 5000, f"{ops:,.0f} ops/s")
        print(f"  -> {ops:,.0f} ops/s")
        pipe2 = r.pipeline()
        for i in range(2000):
            await pipe2.delete(f"bench:{i}")
        await pipe2.execute()

        # ── 5. Queues fully drained ───────────────────────────────────────
        print("5) queue drain check")
        async def _drained():
            depths = [await get_queue_depth(q) for q in
                      ("arq:queue", "arq:queue:recognition", "arq:queue:music")]
            return all(d == 0 for d in depths)
        drained = await wait_for(_drained, timeout=120, label="queue drain")
        results["queues_drained"] = (bool(drained), "all depths 0" if drained else "stuck jobs!")
        print(f"  -> drained: {bool(drained)}")

    finally:
        await pool.aclose()
        await bot.session.close()

    print()
    for k, (ok, detail) in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {k:<22} {detail}")
    ok_all = all(ok for ok, _ in results.values())
    print(f"\n{'ALL PASS' if ok_all else 'SOME FAILED'}")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    asyncio.run(main())
