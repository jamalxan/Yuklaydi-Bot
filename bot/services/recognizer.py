from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import shazamio
from shazamio import Shazam

from bot.config import settings
from bot.logger import logger
from bot.utils.text import safe_https_url
from bot.services.media import extract_audio_snippet, get_temp_path, probe_duration, safe_delete

_shazamio_version = getattr(shazamio, "__version__", "unknown")
logger.info(f"[shazam] shazamio version={_shazamio_version}")
_shazam = Shazam()
logger.info(f"[shazam] Shazam() initialised — core_recognizer={type(_shazam.core_recognizer).__name__}")


AUDD_ENDPOINT = "https://api.audd.io/"
AUDD_TIMEOUT = 15


class SongResult:
    __slots__ = ("title", "artist", "coverart", "apple_url", "shazam_url",
                 "spotify_url", "lyrics", "source")

    def __init__(
        self,
        title: str,
        artist: str,
        coverart: Optional[str] = None,
        apple_url: Optional[str] = None,
        shazam_url: Optional[str] = None,
        lyrics: Optional[str] = None,
        spotify_url: Optional[str] = None,
        source: str = "shazam",
    ):
        self.title = title
        self.artist = artist
        self.coverart = coverart
        self.apple_url = apple_url
        self.shazam_url = shazam_url
        self.lyrics = lyrics
        self.spotify_url = spotify_url
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

        # Shazam misses some tracks entirely (regional releases, remixes,
        # covers). With an AudD.io key configured, retry once on the snippet
        # already extracted for the last window — no extra ffmpeg call.
        # No key → no request, no cost.
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
    """Recognize via AudD.io (fallback only). Never raises."""
    try:
        if not os.path.isfile(snippet_path) or os.path.getsize(snippet_path) < 1000:
            return None
        import aiohttp

        with open(snippet_path, "rb") as fh:
            audio = fh.read()
        form = aiohttp.FormData()
        form.add_field("api_token", settings.audd_token)
        form.add_field("return", "apple_music,spotify")
        form.add_field("file", audio, filename="snippet.wav", content_type="audio/wav")

        t0 = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=AUDD_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(AUDD_ENDPOINT, data=form) as resp:
                if resp.status != 200:
                    logger.warning(f"[audd] HTTP {resp.status}")
                    return None
                payload = await resp.json(content_type=None)
        logger.debug(f"[audd] {time.monotonic() - t0:.2f}s status={payload.get('status')}")
    except Exception as exc:
        # The token is in the form body, never in the URL — safe to log exc.
        logger.warning(f"[audd] request failed: {type(exc).__name__}: {exc}")
        return None

    if not isinstance(payload, dict) or payload.get("status") != "success":
        if isinstance(payload, dict) and payload.get("error"):
            logger.warning(f"[audd] API error: {payload['error']}")
        return None
    result = payload.get("result")
    return _parse_audd_result(result) if isinstance(result, dict) else None


def _parse_audd_result(result: dict) -> Optional[SongResult]:
    title = (result.get("title") or "").strip()
    artist = (result.get("artist") or "").strip()
    if not title:
        return None
    apple = result.get("apple_music") if isinstance(result.get("apple_music"), dict) else {}
    spotify = result.get("spotify") if isinstance(result.get("spotify"), dict) else {}

    coverart = None
    artwork = apple.get("artwork") if isinstance(apple.get("artwork"), dict) else {}
    if artwork.get("url"):
        coverart = str(artwork["url"]).replace("{w}", "500").replace("{h}", "500")
    else:
        images = (spotify.get("album") or {}).get("images") or []
        if images and isinstance(images[0], dict):
            coverart = images[0].get("url")

    return SongResult(
        title=title,
        artist=artist or "Unknown",
        coverart=safe_https_url(coverart),
        apple_url=safe_https_url(apple.get("url")),
        shazam_url=None,
        lyrics=None,
        spotify_url=safe_https_url((spotify.get("external_urls") or {}).get("spotify")),
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
