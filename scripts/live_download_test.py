#!/usr/bin/env python3
"""Live download smoke test: runs the real downloader against real platform URLs.

Usage:
    python scripts/live_download_test.py [platform ...]
    platforms: youtube youtube_mp3 tiktok instagram (default: all)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_URLS = {
    # Short, public, stable test videos
    "youtube":     ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "720"),   # "Me at the zoo", 19s
    "youtube_mp3": ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "mp3"),
    "tiktok":      ("https://www.tiktok.com/@tiktok/video/7231338487075638570", "720"),
    "instagram":   ("https://www.instagram.com/reel/Chunk8-jurw/", "720"),  # @instagram, 5 s
}


async def run_one(name: str, url: str, quality: str) -> bool:
    from bot.services.downloader import DownloadError, download, normalize_url
    from bot.services.media import get_temp_path, safe_delete

    suffix = ".mp3" if quality == "mp3" else ".mp4"
    path = get_temp_path(suffix=suffix, prefix=f"livetest_{name}_")
    t0 = time.monotonic()
    try:
        result = await download(normalize_url(url), path, quality)
        dt = time.monotonic() - t0
        size_mb = result.size_bytes / 1024 / 1024
        okay = result.size_bytes > 10_000
        status = "OK  " if okay else "FAIL"
        print(f"[{status}] {name:<12} {size_mb:6.2f}MB in {dt:5.1f}s "
              f"(extract={result.t_extract:.1f}s dl={result.t_download:.1f}s) -> {os.path.basename(result.path)}")
        safe_delete(result.path)
        return okay
    except DownloadError as e:
        dt = time.monotonic() - t0
        print(f"[FAIL] {name:<12} DownloadError kind={e.kind} after {dt:.1f}s: {str(e)[:160]}")
        return False
    except Exception as e:
        dt = time.monotonic() - t0
        print(f"[FAIL] {name:<12} {type(e).__name__} after {dt:.1f}s: {str(e)[:160]}")
        return False
    finally:
        safe_delete(path)


async def main():
    wanted = sys.argv[1:] or list(TEST_URLS)
    results = {}
    for name in wanted:
        if name not in TEST_URLS:
            print(f"[SKIP] unknown platform: {name}")
            continue
        url, quality = TEST_URLS[name]
        results[name] = await run_one(name, url, quality)

    print()
    passed = sum(results.values())
    print(f"{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
