from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from bot.config import settings
from bot.logger import logger
from bot.services.downloader import (
    apply_proxy, build_base_opts, format_selector, open_ydl, resolve_output_path, run_ydl,
    strip_media_ext,
)

# Results longer than this (mixes, full albums) are flagged in the result list.
LONG_TRACK_SECONDS = 15 * 60

MIN_QUERY_LEN = 2
MAX_QUERY_LEN = 100

# Dedicated, bounded pool: searches run yt-dlp inside the BOT process, and the
# default executor is shared with aiohttp/DNS — a burst of searches must not
# starve polling.
_SEARCH_POOL: Optional[ThreadPoolExecutor] = None


def _search_pool() -> ThreadPoolExecutor:
    global _SEARCH_POOL
    if _SEARCH_POOL is None:
        _SEARCH_POOL = ThreadPoolExecutor(
            max_workers=max(1, settings.search_executor_workers),
            thread_name_prefix="ytsearch",
        )
    return _SEARCH_POOL


def normalize_query(text: str) -> Optional[str]:
    """Collapse whitespace, cap at MAX_QUERY_LEN; None if too short to search."""
    q = re.sub(r"\s+", " ", text or "").strip()[:MAX_QUERY_LEN].strip()
    if len(q) < MIN_QUERY_LEN or not any(ch.isalnum() for ch in q):
        return None
    return q

_CLEAN_RE = re.compile(
    r"\b(Official\s+)?(Music\s+)?(Video|Audio|Lyric\s+Video|Visualizer|MV|HD|4K|HQ)\b"
    r"|[\(\[]\s*(Official|Audio|Lyrics?|HD|4K|HQ|Full\s+Song|feat\.?\s+[^\)\]]+)\s*[\)\]]"
    r"|#\S+",
    re.IGNORECASE,
)


@dataclass
class SongEntry:
    id: str
    title: str
    uploader: str
    duration: int
    url: str
    thumbnail: Optional[str] = None


def clean_title(title: str) -> str:
    cleaned = _CLEAN_RE.sub("", title)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" -|–")


def format_duration(seconds: int) -> str:
    m, s = divmod(max(0, seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _parse_entry(entry: dict) -> Optional[SongEntry]:
    vid_id = entry.get("id") or entry.get("url", "")
    if not vid_id:
        return None
    url = entry.get("webpage_url") or f"https://www.youtube.com/watch?v={vid_id}"
    return SongEntry(
        id=vid_id,
        title=clean_title(entry.get("title") or "Unknown"),
        uploader=entry.get("uploader") or entry.get("channel") or "Unknown",
        duration=int(entry.get("duration") or 0),
        url=url,
        thumbnail=entry.get("thumbnail"),
    )


def _search_sync(query: str, max_results: int) -> list[dict]:
    opts = build_base_opts(extract_flat=True, skip_download=True, noplaylist=False)
    apply_proxy(opts, "https://www.youtube.com/")
    with open_ydl(opts) as ydl:
        result = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
    return list(result.get("entries") or [])


async def search_songs(query: str, max_results: int = 30) -> list[SongEntry]:
    try:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(_search_pool(), _search_sync, query, max_results)
        entries = [_parse_entry(e) for e in raw if e]
        return [e for e in entries if e is not None]
    except Exception as e:
        logger.error(f"Music search error: {e}")
        return []


def _download_audio_sync(url: str, output_path: str) -> str:
    opts = build_base_opts(
        # "bestaudio/best" picks the highest-bitrate stream regardless of
        # protocol, which on SABR-affected videos means a full 1080p HLS mux
        # just to throw the video away — minutes of throttled download for a
        # 3-minute song. Reuse the same https-preferred selector as video.
        format=format_selector("mp3"),
        outtmpl=strip_media_ext(output_path),
        concurrent_fragment_downloads=4,
        writethumbnail=False,
        postprocessors=[{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    )
    # Same two-pass runner as video: limits checked before download, and a
    # YouTube 403 retries once with the fallback clients.
    info, _, _ = run_ydl(url, opts, "mp3")
    # Return the file yt-dlp really wrote (post-processor output), not a guess.
    return resolve_output_path(info, strip_media_ext(output_path) + ".mp3")


async def download_audio(url: str, output_path: str) -> str:
    return await asyncio.to_thread(_download_audio_sync, url, output_path)
