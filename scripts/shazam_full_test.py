#!/usr/bin/env python3
"""Comprehensive Shazam/recognition test.

Covers:
  1. Voice/video-note fixtures (positive)
  2. Video WITH music (generated: black video + known song audio)
  3. Edge cases: silence, 1s clip, corrupt file, empty file, no-music video
  4. Timing for every case

Usage: python scripts/shazam_full_test.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.services.media import ffmpeg_path, get_temp_path, safe_delete  # noqa: E402

EXPECT_TITLE = "Oyning"  # all fixtures contain Botir Qodirov — Oyning o'n beshi qorong'u


def _gen(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


async def main():
    from bot.services.recognizer import recognize

    results: list[tuple[str, bool, float, str]] = []
    tmp: list[str] = []

    def temp(suffix):
        p = get_temp_path(suffix=suffix, prefix="shztest_")
        tmp.append(p)
        return p

    # ── Generated media ───────────────────────────────────────────────────
    ff = ffmpeg_path()

    # a) video with music: black 16s video + fixture audio track
    music_video = temp(".mp4")
    _gen([ff, "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=16",
          "-i", "tests/fixtures/voice_16s.ogg",
          "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
          "-shortest", music_video])

    # b) 10s of pure silence
    silent = temp(".wav")
    _gen([ff, "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "10", silent])

    # c) 1s tone (too short / not a song)
    tone = temp(".wav")
    _gen([ff, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", tone])

    # d) corrupt file (random bytes with .mp3 name)
    corrupt = temp(".mp3")
    with open(corrupt, "wb") as f:
        f.write(os.urandom(64_000))

    # e) empty file
    empty = temp(".ogg")
    open(empty, "wb").close()

    cases = [
        # (name, path, expect_found)
        ("voice_7s",        "tests/fixtures/voice_7s.ogg",       True),
        ("voice_11s",       "tests/fixtures/voice_11s.ogg",      True),
        ("voice_16s",       "tests/fixtures/voice_16s.ogg",      True),
        ("video_note_14s",  "tests/fixtures/video_note_14s.mp4", True),
        ("video_with_music", music_video,                        True),
        ("silence_10s",     silent,                              False),
        ("tone_1s",         tone,                                False),
        ("corrupt_mp3",     corrupt,                             False),
        ("empty_file",      empty,                               False),
    ]

    for name, path, expect in cases:
        t0 = time.monotonic()
        try:
            r = await recognize(path)
            dt = time.monotonic() - t0
            found = r is not None
            if expect:
                okay = found and EXPECT_TITLE.lower() in (r.title or "").lower()
                detail = f"{r.title} — {r.artist}" if r else "no match"
            else:
                okay = not found  # must gracefully return None
                detail = "correctly returned None" if okay else f"UNEXPECTED match: {r.title}"
        except Exception as e:
            dt = time.monotonic() - t0
            okay, detail = False, f"RAISED {type(e).__name__}: {str(e)[:80]}"
        results.append((name, okay, dt, detail))
        print(f"[{'PASS' if okay else 'FAIL'}] {name:<16} {dt:5.1f}s  {detail}")

    for p in tmp:
        safe_delete(p)

    passed = sum(1 for _, ok, _, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
