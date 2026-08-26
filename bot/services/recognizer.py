from __future__ import annotations

import os
import time
from typing import Optional

import shazamio
from shazamio import Shazam

from bot.config import settings
from bot.logger import logger
from bot.services.media import extract_audio_snippet, get_temp_path, probe_duration, safe_delete

_shazamio_version = getattr(shazamio, "__version__", "unknown")
logger.info(f"[shazam] shazamio version={_shazamio_version}")
_shazam = Shazam()
logger.info(f"[shazam] Shazam() initialised — core_recognizer={type(_shazam.core_recognizer).__name__}")

AUDD_ENDPOINT = "https://api.audd.io/"


class SongResult:
    __slots__ = ("title", "artist", "coverart", "apple_url", "shazam_url", "spotify_url", "lyrics", "source")

    def __init__(
        self,
        title: str,
        artist: str,
        coverart: Optional[str] = None,
        apple_url: Optional[str] = None,
        shazam_url: Optional[str] = None,
        spotify_url: Optional[str] = None,
        lyrics: Optional[str] = None,
        source: str = "shazam",
    ):
        self.title = title
        self.artist = artist
        self.coverart = coverart
        self.apple_url = apple_url
        self.shazam_url = shazam_url
        self.spotify_url = spotify_url
        self.lyrics = lyrics
        self.source = source


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
    try:
        logger.debug(f"[shazam] calling _shazam.recognize({snippet_path!r})")
        raw_result = await _shazam.recognize(snippet_path)
        elapsed = time.monotonic() - t0
        matches = raw_result.get("matches", [])
        logger.debug(f"[shazam] recognize() returned in {elapsed:.2f}s — matches={len(matches)} has_track={'track' in raw_result}")
        if not matches and "track" not in raw_result:
            logger.debug(f"[shazam] full raw response={raw_result}")
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.error(f"[shazam] recognize() RAISED after {elapsed:.2f}s: {type(exc).__name__}: {exc}", exc_info=True)
        return None

    parsed = _parse_result(raw_result)
    if parsed:
        logger.info(f"[shazam] FOUND: {parsed.title!r} — {parsed.artist!r} (start={start:.1f}s)")
    else:
        logger.debug(f"[shazam] no match for window start={start:.1f}s dur={duration}s")
    return parsed


async def recognize(media_path: str) -> Optional[SongResult]:
    """Recognize song using multi-window Shazam search.

    Strategy: middle 15s → start 15s → full 30s.
    Handles OGG/Opus voice notes, short clips, and large video files.
    """
    snippet_path = get_temp_path(suffix=".wav", prefix="shz_")
    try:
        total_dur = await probe_duration(media_path)
        logger.info(f"Recognizing: {media_path} (duration={total_dur:.1f}s)")

        # Window 1: middle of the file (best for song identification)
        result = await _try_recognize_snippet(media_path, snippet_path, start=-1.0, duration=15)
        if result:
            return result

        # Window 2: from the very beginning (catches intros)
        result = await _try_recognize_snippet(media_path, snippet_path, start=0.0, duration=15)
        if result:
            return result

        # Window 3: 30-second window from middle (more context for Shazam)
        mid_start = max(0.0, (total_dur / 2) - 15) if total_dur > 30 else 0.0
        result = await _try_recognize_snippet(media_path, snippet_path, start=mid_start, duration=30)
        if result:
            return result

        # Shazam's fingerprint DB misses some tracks entirely (regional releases,
        # remixes, covers, background music). If an AudD.io key is configured, try
        # it as a fallback on the widest snippet we already extracted (window 3) —
        # no extra ffmpeg call needed.
        if settings.audd_token:
            result = await _try_audd(snippet_path)
            if result:
                logger.info(f"[audd] FOUND: {result.title!r} — {result.artist!r}")
                return result

        logger.info(f"Shazam: no match found for {media_path}")
        return None

    except Exception as e:
        logger.error(f"Shazam recognition error: {e}", exc_info=True)
        return None
    finally:
        safe_delete(snippet_path)


async def _try_audd(snippet_path: str) -> Optional[SongResult]:
    """Recognize via AudD.io — used only as a fallback when Shazam finds nothing."""
    if not os.path.exists(snippet_path) or os.path.getsize(snippet_path) < 1000:
        return None
    try:
        import aiohttp

        data = aiohttp.FormData()
        data.add_field("api_token", settings.audd_token)
        data.add_field("return", "apple_music,spotify")
        with open(snippet_path, "rb") as f:
            data.add_field("file", f.read(), filename="snippet.wav", content_type="audio/wav")

        t0 = time.monotonic()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                AUDD_ENDPOINT, data=data, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[audd] HTTP {resp.status}")
                    return None
                payload = await resp.json()
        logger.debug(f"[audd] responded in {time.monotonic() - t0:.2f}s: status={payload.get('status')}")
    except Exception as exc:
        logger.warning(f"[audd] request failed: {type(exc).__name__}: {exc}")
        return None

    if payload.get("status") != "success" or not payload.get("result"):
        return None
    return _parse_audd_result(payload["result"])


def _parse_audd_result(result: dict) -> SongResult:
    apple = result.get("apple_music") or {}
    spotify = result.get("spotify") or {}

    coverart = apple.get("artwork", {}).get("url") if apple.get("artwork") else None
    if coverart:
        coverart = coverart.replace("{w}", "500").replace("{h}", "500")
    elif spotify.get("album", {}).get("images"):
        coverart = spotify["album"]["images"][0].get("url")

    return SongResult(
        title=result.get("title") or "Unknown",
        artist=result.get("artist") or "Unknown",
        coverart=coverart,
        apple_url=apple.get("url"),
        spotify_url=spotify.get("external_urls", {}).get("spotify") if spotify else None,
        lyrics=result.get("lyrics") or None,
        source="audd",
    )


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
