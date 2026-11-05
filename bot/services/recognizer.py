from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import shazamio
from shazamio import Shazam

from bot.logger import logger
from bot.services.media import extract_audio_snippet, get_temp_path, probe_duration, safe_delete

_shazamio_version = getattr(shazamio, "__version__", "unknown")
logger.info(f"[shazam] shazamio version={_shazamio_version}")
_shazam = Shazam()
logger.info(f"[shazam] Shazam() initialised — core_recognizer={type(_shazam.core_recognizer).__name__}")


class SongResult:
    __slots__ = ("title", "artist", "coverart", "apple_url", "shazam_url", "lyrics")

    def __init__(
        self,
        title: str,
        artist: str,
        coverart: Optional[str] = None,
        apple_url: Optional[str] = None,
        shazam_url: Optional[str] = None,
        lyrics: Optional[str] = None,
    ):
        self.title = title
        self.artist = artist
        self.coverart = coverart
        self.apple_url = apple_url
        self.shazam_url = shazam_url
        self.lyrics = lyrics


async def _try_recognize_snippet(media_path: str, snippet_path: str,
                                  start: float, duration: int) -> Optional[SongResult]:
    """Extract snippet at given start/duration and try to recognize it."""
    await extract_audio_snippet(media_path, snippet_path, snippet_duration=duration, start_override=start)
    size = os.path.getsize(snippet_path) if os.path.exists(snippet_path) else 0
    logger.debug(f"[shazam] snippet: start={start:.1f}s dur={duration}s size={size}B path={snippet_path!r}")
    if size < 1000:
        logger.warning(f"[shazam] snippet too small ({size}B), skipping window")
        return None

    t0 = time.monotonic()
    raw_result = None
    for attempt in (1, 2):  # one retry on transient network failure
        try:
            logger.debug(f"[shazam] calling _shazam.recognize({snippet_path!r}) attempt={attempt}")
            raw_result = await _shazam.recognize(snippet_path)
            break
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.warning(f"[shazam] recognize() attempt {attempt} failed after {elapsed:.2f}s: "
                           f"{type(exc).__name__}: {exc}")
            if attempt == 2:
                return None
            await asyncio.sleep(1.5)
    if raw_result is None:
        return None
    elapsed = time.monotonic() - t0
    matches = raw_result.get("matches", [])
    logger.debug(f"[shazam] recognize() returned in {elapsed:.2f}s — matches={len(matches)} has_track={'track' in raw_result}")
    if not matches and "track" not in raw_result:
        logger.debug(f"[shazam] full raw response={raw_result}")

    parsed = _parse_result(raw_result)
    if parsed:
        logger.info(f"[shazam] FOUND: {parsed.title!r} — {parsed.artist!r} (start={start:.1f}s)")
    else:
        logger.debug(f"[shazam] no match for window start={start:.1f}s dur={duration}s")
    return parsed


SHORT_CLIP_SECONDS = 15


def recognition_windows(total_dur: float) -> list[tuple[float, int]]:
    """Distinct (start, duration) windows to try, best first.

    middle 15 s → first 15 s → middle 30 s. On short clips these collapse to
    the same audio, which used to be sent to Shazam three times (six with
    retries); duplicates are removed and clips ≤ 15 s get a single attempt.
    ``start = -1`` means "centre the window" (resolved by extract_audio_snippet).
    """
    if total_dur <= 0:
        # Unknown duration (probe failed): keep the classic strategy.
        return [(-1.0, 15), (0.0, 15), (0.0, 30)]
    if total_dur <= SHORT_CLIP_SECONDS:
        return [(0.0, max(1, int(total_dur)))]

    def window(start: float, dur: int) -> tuple[float, int]:
        dur = min(dur, int(total_dur))
        start = max(0.0, min(start, total_dur - dur))
        return round(start, 1), dur

    candidates = [
        window(total_dur / 2 - 7.5, 15),
        window(0.0, 15),
        window(total_dur / 2 - 15, 30),
    ]
    seen: set[tuple[float, int]] = set()
    unique: list[tuple[float, int]] = []
    for w in candidates:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique


async def recognize(media_path: str) -> Optional[SongResult]:
    """Recognize song using multi-window Shazam search (see recognition_windows).

    Handles OGG/Opus voice notes, short clips, and large video files.
    """
    snippet_path = get_temp_path(suffix=".wav", prefix="shz_")
    try:
        total_dur = await probe_duration(media_path)
        logger.info(f"Recognizing: {media_path} (duration={total_dur:.1f}s)")

        for start, duration in recognition_windows(total_dur):
            result = await _try_recognize_snippet(media_path, snippet_path, start=start, duration=duration)
            if result:
                return result

        logger.info(f"Shazam: no match found for {media_path}")
        return None

    except Exception as e:
        logger.error(f"Shazam recognition error: {e}", exc_info=True)
        return None
    finally:
        safe_delete(snippet_path)


def _parse_result(data: dict) -> Optional[SongResult]:
    track = data.get("track")
    if not track:
        return None

    title = track.get("title", "Unknown")
    artist = track.get("subtitle", "Unknown")
    coverart = track.get("images", {}).get("coverart")

    apple_url = None
    shazam_url = None
    hub = track.get("hub", {})
    for action in hub.get("actions", []):
        if action.get("type") == "uri":
            uri = action.get("uri", "")
            if "music.apple.com" in uri:
                apple_url = uri
    for option in hub.get("options", []):
        for action in option.get("actions", []):
            if "shazam.com" in action.get("uri", ""):
                shazam_url = action["uri"]

    url_obj = track.get("url")
    if url_obj and not shazam_url:
        shazam_url = url_obj

    # Extract embedded lyrics if present
    lyrics_text = None
    for section in track.get("sections", []):
        if section.get("type") == "LYRICS":
            lines = section.get("text", [])
            if lines:
                lyrics_text = "\n".join(lines)
            break

    return SongResult(
        title=title,
        artist=artist,
        coverart=coverart,
        apple_url=apple_url,
        shazam_url=shazam_url,
        lyrics=lyrics_text,
    )
