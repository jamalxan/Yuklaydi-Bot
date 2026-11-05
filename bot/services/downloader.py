from __future__ import annotations

import asyncio
import ipaddress
import os
import shutil
import time
from typing import Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlsplit, urlunparse

import yt_dlp

from bot.config import settings

# ── Platform detection ────────────────────────────────────────────────────────
#
# SECURITY: matching is done on the parsed hostname with a domain-boundary
# check (host == d or host.endswith("." + d)). A substring/regex search over
# the whole URL let "http://127.0.0.1:8081/x.com" or "netflix.com" through to
# yt-dlp's generic extractor, i.e. a server-side request forgery primitive.

PLATFORM_DOMAINS: dict[str, tuple[str, ...]] = {
    "youtube":   ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "tiktok":    ("tiktok.com",),  # covers vm.tiktok.com / www.tiktok.com
    "instagram": ("instagram.com", "instagr.am"),
    "facebook":  ("facebook.com", "fb.watch", "fb.com"),
    "twitter":   ("twitter.com", "x.com", "t.co"),
    "pinterest": ("pinterest.com", "pin.it"),
    "vimeo":     ("vimeo.com",),
    "reddit":    ("reddit.com", "redd.it"),
}

# Backwards-compatible alias (older code/tests import this name).
SUPPORTED_PLATFORMS = PLATFORM_DOMAINS

# Short-link hosts whose redirect target is resolved (and re-validated) by us
# before yt-dlp sees the URL, because yt-dlp only follows them through the
# generic extractor, which is disabled.
SHORTLINK_HOSTS = frozenset({"pin.it", "fb.watch", "redd.it", "instagr.am"})

# yt-dlp extractors we allow (regex, full-match against lower-cased IE_NAME).
# "generic" is deliberately absent: it fetches arbitrary URLs.
ALLOWED_EXTRACTORS: list[str] = [
    r"youtube(:.+)?",
    r"tiktok(:.+)?", r"vm\.tiktok",
    r"instagram(:.+)?",
    r"facebook", r"facebook:reel",
    r"twitter(:(card|shortener|broadcast|amplify))?",
    r"pinterest",
    r"vimeo(:.+)?",
    r"reddit",
]

_ALLOWED_PORTS = {None, 80, 443}


def safe_hostname(url: str) -> Optional[str]:
    """Return the lower-cased hostname if *url* is a plain public http(s) URL.

    Rejects: non-http(s) schemes, userinfo (user@host), IP-literal hosts,
    non-standard ports and anything urlsplit cannot parse.
    """
    try:
        parts = urlsplit((url or "").strip())
        if parts.scheme.lower() not in ("http", "https"):
            return None
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            return None
        if parts.port not in _ALLOWED_PORTS:  # .port raises ValueError if invalid
            return None
        host = (parts.hostname or "").rstrip(".").lower()
    except (ValueError, TypeError):
        return None
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return None  # IP literal — never a supported platform
    except ValueError:
        pass
    return host


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def platform_for_host(host: str) -> Optional[str]:
    for platform, domains in PLATFORM_DOMAINS.items():
        if any(_host_matches(host, d) for d in domains):
            return platform
    return None


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
    """Map a URL to a supported platform name, or None if not allowed."""
    host = safe_hostname(url)
    if host is None:
        return None
    return platform_for_host(host)


async def resolve_shortlink(url: str, max_hops: int = 5, timeout: float = 10.0) -> str:
    """Follow redirects of known short-link hosts, validating every hop.

    Each Location must again be a supported-platform URL (detect_platform), so
    a short link can never bounce the worker to an internal or third-party
    address. Non-shortlink URLs are returned unchanged.
    """
    host = safe_hostname(url)
    if host is None or host not in SHORTLINK_HOSTS:
        return url

    import aiohttp

    current = url
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        for _ in range(max_hops):
            async with session.get(current, allow_redirects=False) as resp:
                if resp.status not in (301, 302, 303, 307, 308):
                    return current
                location = resp.headers.get("Location", "")
            nxt = urljoin(current, location)
            if detect_platform(nxt) is None:
                raise DownloadError(f"Short link redirects off-platform: {nxt[:120]}", kind="unsupported")
            current = nxt
            if safe_hostname(current) not in SHORTLINK_HOSTS:
                return current
    raise DownloadError("Too many redirects", kind="unsupported")


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

def _size_filter(max_bytes: Optional[int]) -> str:
    """yt-dlp filter that drops formats known to exceed *max_bytes*.

    ``<?`` lets formats with an unknown size through; the explicit pre-download
    estimate (check_limits) and the max_filesize option catch those.
    """
    if not max_bytes:
        return ""
    return f"[filesize<?{max_bytes}][filesize_approx<?{max_bytes}]"


def _format_selector(quality: str, max_bytes: Optional[int] = None) -> str:
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

    With *max_bytes* every sized alternative is restricted to formats that fit;
    the final unfiltered ``best`` stays so an oversized video produces a clear
    "too_large" from check_limits instead of "requested format not available".
    """
    if quality == "mp3":
        return (
            "ba[protocol!*=m3u8][ext=m4a]/ba[protocol!*=m3u8]"
            # No audio-only https stream → smallest non-HLS combined stream
            # (height-capped so we don't drag down a 1080p file just to throw
            # the video half away).
            "/b[protocol!*=m3u8][height<=480]/ba/b/best"
        )
    h = 1080 if quality in ("1080", "1080p") else 720
    f = _size_filter(max_bytes)
    return (
        f"b[protocol!*=m3u8][ext=mp4][height<={h}]{f}/b[protocol!*=m3u8][height<={h}]{f}"
        f"/bv*[protocol!*=m3u8][height<={h}]{f}+ba[protocol!*=m3u8]"
        f"/b[ext=mp4][height<={h}]{f}/b[height<={h}]{f}"
        f"/bv*[height<={h}]{f}+ba[ext=m4a]/bv*[height<={h}]{f}+ba/b{f}/best"
    )


def format_selector(quality: str, max_bytes: Optional[int] = None) -> str:
    """Public wrapper around _format_selector for reuse by other modules."""
    return _format_selector(quality, max_bytes)


def cookies_path() -> Optional[str]:
    """Path to a cookies.txt if one is configured and present, else None.

    Lookup order: COOKIES_FILE → ./secrets/cookies.txt (default; kept OUT of
    the download dir) → legacy <download_dir>/cookies.txt. The cleanup job only
    touches <download_dir>/tmp, so the legacy location is no longer at risk.
    """
    candidates = [
        settings.cookies_file,
        os.path.join(os.getcwd(), "secrets", "cookies.txt"),
        os.path.join(settings.download_dir, "cookies.txt"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def has_cookies() -> bool:
    """True if any cookie source is configured (file or browser profile)."""
    return bool(cookies_path() or settings.cookies_from_browser)


def youtube_extractor_args() -> dict:
    # Multiple clients: if one gets bot-checked, yt-dlp tries the next.
    return {"youtube": {"player_client": ["android", "web_safari", "web"]}}


def classify_download_error(exc: BaseException) -> "DownloadError":
    """Map a raw yt-dlp exception to a DownloadError with a stable, localizable kind."""
    msg = str(exc).lower()
    if "private" in msg or "removed" in msg or "deleted" in msg:
        return DownloadError(str(exc), kind="private")
    elif "geo" in msg or "not available in your country" in msg:
        return DownloadError(str(exc), kind="geo")
    elif "larger than max-filesize" in msg:
        return DownloadError(str(exc), kind="too_large")
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
    elif "unsupported url" in msg or "no suitable infoextractor" in msg:
        return DownloadError(str(exc), kind="unsupported")
    else:
        return DownloadError(str(exc), kind="generic")


# ── yt-dlp options builder ────────────────────────────────────────────────────

def build_base_opts(**overrides) -> dict:
    """Options shared by EVERY yt-dlp call (video, audio, search, recognize).

    One place for cookies, the YouTube client list, the JS runtime, timeouts
    and the extractor allow-list — music search/download used to skip cookies
    and the JS runtime and hit YouTube bot-checks that video downloads avoid.
    """
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 5,
        "noplaylist": True,
        # YouTube: try multiple clients (one may dodge bot-check where another fails)
        "extractor_args": youtube_extractor_args(),
        # node.js JS runtime for YouTube n-challenge solving (avoids throttled URLs)
        "js_runtimes": {"node": {}},
        # Download EJS challenge-solver script from GitHub on first use (cached)
        "remote_components": {"ejs:github"},
        # SSRF defence-in-depth: platform extractors only, never "generic"
        "allowed_extractors": list(ALLOWED_EXTRACTORS),
    }
    _apply_cookies(opts)
    opts.update(overrides)
    return opts


def _build_opts(output_path: str, quality: str) -> dict:
    opts = build_base_opts(
        outtmpl=output_path,
        format=_format_selector(quality, None if quality == "mp3" else settings.max_file_bytes),
        merge_output_format="mp4",
        # Speed. Kept modest (not 16) because hammering a throttled HLS host
        # with many parallel connections triggers more resets/timeouts than it
        # saves — see _format_selector for why HLS is now a last resort.
        concurrent_fragment_downloads=4,
        writethumbnail=False,
        writesubtitles=False,
        writeinfojson=False,
    )

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
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
        opts["outtmpl"] = strip_media_ext(output_path)
    else:
        # Abort mid-transfer if the server sends more than we could upload.
        opts["max_filesize"] = settings.max_file_bytes
        # No transcode for video — remux/mux only (skip for MP3 which needs re-encode)
        opts["postprocessor_args"] = {"default": ["-c", "copy"]}

    return opts


def _apply_cookies(opts: dict) -> None:
    """Attach cookie sources to yt-dlp opts (cookies.txt and/or browser)."""
    cpath = cookies_path()
    if cpath:
        opts["cookiefile"] = cpath
    if settings.cookies_from_browser:
        # ("chrome",) tuple form = browser name without profile/keyring specs
        opts["cookiesfrombrowser"] = (settings.cookies_from_browser,)


def strip_media_ext(path: str) -> str:
    """Drop a trailing .mp4/.mp3 (yt-dlp appends the real extension itself)."""
    root, ext = os.path.splitext(path)
    return root if ext.lower() in (".mp4", ".mp3") else path


# ── Size / duration limits (checked before any byte is downloaded) ───────────

MP3_BYTES_PER_SECOND = 192_000 // 8  # 192 kbps output


def estimate_size(info: dict) -> Optional[int]:
    """Best-effort byte size of the selected format(s); None if unknown."""
    formats = info.get("requested_formats") or [info]
    duration = float(info.get("duration") or 0)
    total = 0
    for f in formats:
        size = f.get("filesize") or f.get("filesize_approx")
        if not size and f.get("tbr") and duration:
            size = float(f["tbr"]) * 1000 / 8 * duration
        if not size:
            return None
        total += int(size)
    return total


def check_limits(info: dict, quality: str) -> None:
    """Raise DownloadError for media we could never deliver."""
    duration = float(info.get("duration") or 0)
    if quality == "mp3":
        if duration > settings.max_audio_duration:
            raise DownloadError(f"Audio too long: {duration:.0f}s", kind="too_long")
        if duration * MP3_BYTES_PER_SECOND > settings.max_file_bytes:
            raise DownloadError("Estimated MP3 too large", kind="too_large")
        return
    est = estimate_size(info)
    if est is not None and est > settings.max_file_bytes:
        raise DownloadError(f"Estimated size {est}B > limit", kind="too_large")


# Partial / bookkeeping files yt-dlp may leave next to the real output.
_JUNK_MARKERS = (".part", ".ytdl", ".temp", ".tmp")


def _is_junk(fname: str) -> bool:
    low = fname.lower()
    return any(low.endswith(m) or (m + ".") in low or (m + "-") in low for m in _JUNK_MARKERS)


def resolve_output_path(info: Optional[dict], expected: str) -> str:
    """Find the file yt-dlp actually produced.

    Prefer the authoritative path from info["requested_downloads"] (it already
    reflects remux/extract-audio postprocessors); fall back to a same-stem scan
    that skips .part/.ytdl/.temp leftovers.
    """
    for rd in (info or {}).get("requested_downloads") or []:
        fp = rd.get("filepath")
        if fp and os.path.isfile(fp) and os.path.getsize(fp) > 0:
            return fp
    if os.path.isfile(expected) and os.path.getsize(expected) > 0:
        return expected
    stem = os.path.basename(os.path.splitext(expected)[0])
    parent = os.path.dirname(expected) or "."
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return expected
    for fname in names:
        if not fname.startswith(stem) or _is_junk(fname):
            continue
        candidate = os.path.join(parent, fname)
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return expected


# ── Sync download (runs in a worker thread) ──────────────────────────────────

def _download_sync(url: str, output_path: str, quality: str) -> DownloadResult:
    opts = _build_opts(output_path, quality)
    t_start = time.monotonic()

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Pass 1: metadata + format selection only, so oversized/overlong
            # media is rejected before a single media byte is downloaded.
            info = ydl.extract_info(url, download=False)
            if not isinstance(info, dict):
                raise DownloadError("No media info", kind="generic")
            check_limits(info, quality)
            t_extract = time.monotonic() - t_start

            # Pass 2: download the already-resolved info dict (no re-extract).
            t_dl = time.monotonic()
            processed = ydl.process_ie_result(info, download=True)
            info = processed if isinstance(processed, dict) else info
            t_download = time.monotonic() - t_dl

        expected = (strip_media_ext(output_path) + ".mp3") if quality == "mp3" else output_path
        out = resolve_output_path(info, expected)
        size = os.path.getsize(out) if os.path.isfile(out) else 0
        if size > settings.max_file_bytes:
            raise DownloadError("Too large", kind="too_large")
        return DownloadResult(out, t_extract, t_download, size)

    except yt_dlp.utils.DownloadError as e:
        raise classify_download_error(e) from e


async def download(url: str, output_path: str, quality: str = "720") -> DownloadResult:
    """Async wrapper — resolves short links, then runs yt-dlp in a thread."""
    url = await resolve_shortlink(url)
    return await asyncio.to_thread(_download_sync, url, output_path, quality)


# ── Short audio clip for recognition (no transcode, first 90 s only) ─────────

RECOGNIZE_CLIP_SECONDS = 90


def _download_audio_clip_sync(url: str, output_path: str) -> str:
    """Grab just the first RECOGNIZE_CLIP_SECONDS of the best audio stream.

    Used when Telegram won't hand us the original file (> 20 MB on the cloud
    API). Shazam needs seconds, not the whole track — no full download and no
    MP3 transcode.
    """
    from yt_dlp.utils import download_range_func

    opts = build_base_opts(
        format="ba[protocol!*=m3u8]/ba/b[height<=480]/b",
        outtmpl=strip_media_ext(output_path) + ".%(ext)s",
        download_ranges=download_range_func([], [(0, RECOGNIZE_CLIP_SECONDS)]),
        force_keyframes_at_cuts=False,
        writethumbnail=False,
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        raise classify_download_error(e) from e
    return resolve_output_path(info if isinstance(info, dict) else None, output_path)


async def download_audio_clip(url: str, output_path: str) -> str:
    url = await resolve_shortlink(url)
    return await asyncio.to_thread(_download_audio_clip_sync, url, output_path)
