from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from functools import partial
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import yt_dlp

from bot.config import settings

# ── Platform detection ────────────────────────────────────────────────────────

SUPPORTED_PLATFORMS = {
    "youtube":   [r"youtu\.be", r"youtube\.com", r"youtube-nocookie\.com"],
    "tiktok":    [r"tiktok\.com", r"vm\.tiktok\.com"],
    "instagram": [r"instagram\.com", r"instagr\.am"],
    "facebook":  [r"facebook\.com", r"fb\.watch", r"fb\.com"],
    "twitter":   [r"twitter\.com", r"x\.com", r"t\.co"],
    "pinterest": [r"pinterest\.com", r"pin\.it"],
    "vimeo":     [r"vimeo\.com"],
    "reddit":    [r"reddit\.com", r"redd\.it"],
}

_STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "si", "igshid", "feature", "pp", "ref", "t",
}

# Check once at startup whether aria2c is available
_ARIA2C = shutil.which("aria2c")


class DownloadError(Exception):
    kind: str = "generic"

    def __init__(self, message: str, kind: str = "generic"):
        super().__init__(message)
        self.kind = kind


class DownloadResult:
    __slots__ = ("path", "t_extract", "t_download", "size_bytes")

    def __init__(self, path: str, t_extract: float, t_download: float, size_bytes: int):
        self.path = path
        self.t_extract = t_extract
        self.t_download = t_download
        self.size_bytes = size_bytes


def detect_platform(url: str) -> Optional[str]:
    for platform, patterns in SUPPORTED_PLATFORMS.items():
        for pat in patterns:
            if re.search(pat, url, re.IGNORECASE):
                return platform
    return None


def normalize_url(url: str) -> str:
    """Strip tracking params and fragment so URL variants share one cache key."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        cleaned = {k: v for k, v in params.items() if k not in _STRIP_PARAMS}
        new_query = urlencode(cleaned, doseq=True)
        return urlunparse(parsed._replace(query=new_query, fragment=""))
    except Exception:
        return url


# ── Format selectors (progressive-first = no merge = faster) ─────────────────

def _format_selector(quality: str) -> str:
    """
    Priority: non-HLS progressive mp4 (no ffmpeg merge) → non-HLS adaptive merge
    → HLS at the same target (last resort, only when no direct-https stream
    exists) → bare best.

    YouTube's "SABR-only streaming" rollout (mid-2026) has removed direct-https
    adaptive formats for most clients on many videos, leaving only HLS
    (m3u8_native). HLS segments on this CDN are throttled to ~30-100KB/s, so a
    720p HLS download can take 15-20 minutes — far past any reasonable job
    timeout. `protocol!*=m3u8` forces yt-dlp to prefer the (usually 360p) direct
    https stream, which is not throttled, before falling back to slow HLS.
    """
    if quality == "mp3":
        return (
            "ba[protocol!*=m3u8][ext=m4a]/ba[protocol!*=m3u8]"
            # No audio-only https stream → smallest non-HLS combined stream
            # (height-capped so we don't drag down a 1080p file just to throw
            # the video half away).
            "/b[protocol!*=m3u8][height<=480]/ba/b/best"
        )
    elif quality in ("1080", "1080p"):
        return (
            "b[protocol!*=m3u8][ext=mp4][height<=1080]/b[protocol!*=m3u8][height<=1080]"
            "/bv*[protocol!*=m3u8][height<=1080]+ba[protocol!*=m3u8]"
            "/b[ext=mp4][height<=1080]/b[height<=1080]"
            "/bv*[height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba/b/best"
        )
    else:  # default 720p
        return (
            "b[protocol!*=m3u8][ext=mp4][height<=720]/b[protocol!*=m3u8][height<=720]"
            "/bv*[protocol!*=m3u8][height<=720]+ba[protocol!*=m3u8]"
            "/b[ext=mp4][height<=720]/b[height<=720]"
            "/bv*[height<=720]+ba[ext=m4a]/bv*[height<=720]+ba/b/best"
        )


def format_selector(quality: str) -> str:
    """Public wrapper around _format_selector for reuse by other modules."""
    return _format_selector(quality)


def cookies_path() -> Optional[str]:
    """Path to a YouTube cookies.txt if one is configured and present, else None."""
    path = settings.cookies_file or os.path.join(settings.download_dir, "cookies.txt")
    return path if path and os.path.exists(path) else None


def youtube_extractor_args() -> dict:
    # Multiple clients: if one gets bot-checked, yt-dlp tries the next.
    return {"youtube": {"player_client": ["android", "web_safari", "web"]}}


def classify_download_error(exc: "yt_dlp.utils.DownloadError") -> "DownloadError":
    """Map a raw yt-dlp exception to a DownloadError with a stable, localizable kind."""
    msg = str(exc).lower()
    if "private" in msg or "removed" in msg or "deleted" in msg:
        return DownloadError(str(exc), kind="private")
    elif "geo" in msg or "not available in your country" in msg:
        return DownloadError(str(exc), kind="geo")
    elif "timed out" in msg or "read timed out" in msg or "connection timed out" in msg or "curl: (28)" in msg:
        return DownloadError(str(exc), kind="timeout")
    elif "connection" in msg or "network" in msg or "errno" in msg:
        return DownloadError(str(exc), kind="timeout")
    elif any(x in msg for x in ("age restriction", "age-restricted", "age gate", "age limit", "age_verify")):
        return DownloadError(str(exc), kind="age")
    elif "confirm you" in msg and "bot" in msg:
        # YouTube bot-check — distinct from Instagram's "no video in post" (also
        # matched generic "sign in"/"login" text below); needs its own honest message.
        return DownloadError(str(exc), kind="bot_check")
    elif "there is no video in this post" in msg or "empty media response" in msg or "login required" in msg:
        return DownloadError(str(exc), kind="no_video")
    elif "sign in" in msg or "login" in msg or "log in" in msg:
        return DownloadError(str(exc), kind="no_video")
    elif "unsupported url" in msg:
        return DownloadError(str(exc), kind="unsupported")
    else:
        return DownloadError(str(exc), kind="generic")


# ── yt-dlp options builder ────────────────────────────────────────────────────

def _build_opts(output_path: str, quality: str) -> dict:
    opts: dict = {
        "outtmpl": output_path,
        "format": _format_selector(quality),
        "merge_output_format": "mp4",
        # Speed. Kept modest (not 16) because hammering a throttled HLS host
        # with many parallel connections triggers more resets/timeouts than it
        # saves — see _format_selector for why HLS is now a last resort.
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 5,
        # No unnecessary work
        "noplaylist": True,
        "writethumbnail": False,
        "writesubtitles": False,
        "writeinfojson": False,
        "no_warnings": True,
        "quiet": True,
        "noprogress": True,
        # YouTube: try multiple clients (one may dodge bot-check where another fails)
        "extractor_args": youtube_extractor_args(),
        # node.js JS runtime for YouTube n-challenge solving (avoids throttled URLs)
        "js_runtimes": {"node": {}},
        # Download EJS challenge-solver script from GitHub on first use (cached)
        "remote_components": {"ejs:github"},
        # Tie-break among equally-eligible formats toward h264/aac — plays inline
        # everywhere (Telegram's own preview, iOS, older Android WebViews) without
        # a server-side transcode, unlike vp9/av1 muxed into an mp4 container.
        # Doesn't override the explicit height caps in the format selector above.
        "format_sort": ["res", "vcodec:h264", "acodec:aac"],
    }

    # aria2c: multi-connection HTTP downloads (big win for single-file MP4s)
    if _ARIA2C:
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {
            "aria2c": [
                "-x16", "-s16", "-k1M",
                "--min-split-size=1M",
                "--max-connection-per-server=16",
                "--optimize-concurrent-downloads=true",
                "--auto-file-renaming=false",
                "--summary-interval=0",
                "--quiet",
            ]
        }

    if quality == "mp3":
        opts["format"] = format_selector("mp3")
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
        opts["outtmpl"] = output_path.replace(".mp4", "").replace(".mp3", "")
    else:
        # No transcode for video — remux/mux only (skip for MP3 which needs re-encode)
        opts["postprocessor_args"] = {"default": ["-c", "copy"]}

    cpath = cookies_path()
    if cpath:
        opts["cookiefile"] = cpath

    return opts


# ── Sync download (runs in executor) ─────────────────────────────────────────

def _download_sync(url: str, output_path: str, quality: str) -> DownloadResult:
    opts = _build_opts(output_path, quality)
    t_start = time.monotonic()
    t_extract = 0.0

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Single pass: extract_info(download=True) — no double fetch
            # We hook the progress callback to measure the extract vs download split
            _info_done = [False]
            _t_info = [t_start]

            orig_process = ydl.process_ie_result

            def _patched_process(ie_result, *args, **kwargs):
                if not _info_done[0]:
                    _info_done[0] = True
                    _t_info[0] = time.monotonic()
                return orig_process(ie_result, *args, **kwargs)

            ydl.process_ie_result = _patched_process

            ydl.extract_info(url, download=True)

            t_extract = _t_info[0] - t_start
            t_download = time.monotonic() - _t_info[0]

            if quality == "mp3":
                # yt-dlp appends .mp3 to the outtmpl (which has no extension)
                base = output_path.replace(".mp4", "").replace(".mp3", "")
                out = base + ".mp3"
            else:
                out = output_path

            # yt-dlp may save to a different extension (e.g. .webm instead of .mp4).
            # Scan the download dir for a recently created file with the same stem.
            if not os.path.exists(out) or os.path.getsize(out) == 0:
                stem = os.path.splitext(out)[0]
                parent = os.path.dirname(out)
                for fname in os.listdir(parent):
                    candidate = os.path.join(parent, fname)
                    if (fname.startswith(os.path.basename(stem))
                            and os.path.isfile(candidate)
                            and os.path.getsize(candidate) > 0):
                        out = candidate
                        break

            size = os.path.getsize(out) if os.path.exists(out) else 0
            return DownloadResult(out, t_extract, t_download, size)

    except yt_dlp.utils.DownloadError as e:
        raise classify_download_error(e) from e


async def download(url: str, output_path: str, quality: str = "720") -> DownloadResult:
    """Async wrapper — runs blocking yt-dlp in a thread pool executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, partial(_download_sync, url, output_path, quality)
    )


# ── Legacy sync extract_info (used only for metadata-first quality picker) ───

def _extract_info_sync(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 15,
        "extractor_args": youtube_extractor_args(),
        "js_runtimes": {"node": {}},
        "remote_components": {"ejs:github"},
    }
    cpath = cookies_path()
    if cpath:
        opts["cookiefile"] = cpath
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


async def extract_info(url: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_extract_info_sync, url))
