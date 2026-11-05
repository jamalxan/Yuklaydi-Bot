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
from bot.logger import logger

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


def _format_selector(quality: str, max_bytes: Optional[int] = None, audio_abr: int = 192) -> str:
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
    q = quality.lower().rstrip("p")
    h = int(q) if q.isdigit() and 100 <= int(q) <= 2160 else 720
    f = _size_filter(max_bytes)
    return (
        # 1) Separate video+audio over plain https (DASH): the only way to get
        #    real 720p/1080p from YouTube — its combined mp4 tops out at 360p.
        #    Merged with "-c copy", no transcode; m4a audio first for mp4.
        #    Audio ≤ audio_abr kbps first: "best m4a" can be a 384 kbps 5.1
        #    track (≈ 30 MB on a 10-minute video) that alone blows the limit.
        f"bv[protocol!*=m3u8][height<={h}]{f}+ba[protocol!*=m3u8][ext=m4a][abr<=?{audio_abr}]"
        f"/bv[protocol!*=m3u8][height<={h}]{f}+ba[protocol!*=m3u8][abr<=?{audio_abr}]"
        f"/bv[protocol!*=m3u8][height<={h}]{f}+ba[protocol!*=m3u8][ext=m4a]"
        f"/bv[protocol!*=m3u8][height<={h}]{f}+ba[protocol!*=m3u8]"
        # 2) Combined https file (TikTok, Instagram, Facebook, X usually).
        f"/b[protocol!*=m3u8][ext=mp4][height<={h}]{f}/b[protocol!*=m3u8][height<={h}]{f}"
        # 3) HLS only as a last resort (throttled hosts), then anything.
        f"/bv[height<={h}]{f}+ba/b[height<={h}]{f}/b{f}/best"
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


YOUTUBE_PRIMARY_CLIENTS = ("default",)
# Retry sets, in order, when the primary client's stream URLs answer 403
# (PO-token / SABR-gated videos). Measured on two such videos: only
# web_embedded still served downloadable DASH (HD) streams; the legacy set
# works almost everywhere but tops out at a 360p combined file.
YOUTUBE_FALLBACK_CLIENT_SETS = (
    ("web_embedded",),
    ("android", "web_safari", "web"),
)
YOUTUBE_FALLBACK_CLIENTS = YOUTUBE_FALLBACK_CLIENT_SETS[-1]  # legacy name


def youtube_extractor_args(clients: tuple[str, ...] = YOUTUBE_PRIMARY_CLIENTS) -> dict:
    # yt-dlp's own "default" client set (kept current by yt-dlp releases).
    # The old hard-coded ["android", "web_safari", "web"] list starved format
    # selection: web/web_safari returned no formats without a PO token and
    # android only offered 360p, so every "720p"/"1080p" download was 360p.
    # Measured on one 635 s video: default → 33 formats (https up to 2160p),
    # old list → 5 formats (max 360p).
    return {"youtube": {"player_client": list(clients)}}


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
    elif "unsupported url" in msg or "no suitable extractor" in msg or "no suitable infoextractor" in msg:
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


VIDEO_FORMAT_SORT = ("res", "vcodec:h264", "acodec:aac")


def _build_opts(output_path: str, quality: str) -> dict:
    opts = build_base_opts(
        outtmpl=output_path,
        format=_format_selector(quality, None if quality == "mp3" else settings.max_video_bytes),
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
        # Abort mid-transfer if the server sends more than we could deliver
        # (bigger than one upload is fine: the worker splits it into parts).
        opts["max_filesize"] = settings.max_video_bytes
        # Among formats the selector already allows (height/size/protocol caps
        # still win), prefer h264 + aac: they play inline in Telegram, on iOS
        # and on older Android without a transcode; vp9/av1 often do not.
        opts["format_sort"] = list(VIDEO_FORMAT_SORT)
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


_MEDIA_EXTS = (".mp4", ".mp3", ".m4a", ".webm", ".opus", ".ogg", ".mkv")


def strip_media_ext(path: str) -> str:
    """Drop a trailing media extension (yt-dlp appends the real one itself)."""
    root, ext = os.path.splitext(path)
    return root if ext.lower() in _MEDIA_EXTS else path


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
    if est is not None and est > settings.max_video_bytes:
        raise DownloadError(f"Estimated size {est}B > limit", kind="too_large")


# Partial / bookkeeping files yt-dlp may leave next to the real output.
_JUNK_MARKERS = (".part", ".ytdl", ".temp", ".tmp", ".aria2")  # .aria2 = aria2c control file


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

# Stream-fetch failures worth one retry with other YouTube clients. A 403
# reaches us verbatim from yt-dlp's own downloader, but when ffmpeg does the
# fetching (download_ranges clips, HLS) only its exit status survives.
# How a dead stream URL (YouTube 403) surfaces, per downloader: yt-dlp's own
# ("HTTP Error 403"), ffmpeg for HLS/DASH ("ffmpeg exited with code"), and
# aria2c — used in the Docker image — which reports only its exit code
# (22 = bad HTTP response). Without the aria2c marker the client fallback
# never ran in production.
_RETRYABLE_MARKERS = (
    "http error 403", "ffmpeg exited with code", "unable to download video data",
    "aria2c exited with code",
)


def _is_forbidden(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _RETRYABLE_MARKERS)


def _remove_partials(outtmpl) -> None:
    """Delete .part/.ytdl leftovers of a failed attempt before retrying.

    A retry may pick a different format; letting yt-dlp "resume" a stale
    .part from another format would corrupt the output. yt-dlp rewrites
    ``opts["outtmpl"]`` in place into a dict, so both forms are accepted.
    """
    if isinstance(outtmpl, dict):
        outtmpl = outtmpl.get("default") or ""
    outtmpl = str(outtmpl or "")
    if not outtmpl:
        return
    base = outtmpl.split("%(", 1)[0]
    base = strip_media_ext(base) if "%(" not in outtmpl else base.rstrip(".")
    folder, prefix = os.path.split(base)
    try:
        for name in os.listdir(folder or "."):
            if name.startswith(prefix) and _is_junk(name):
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass
    except OSError:
        pass


# Lower resolutions tried (same metadata, no re-extract) when the requested
# one would exceed the upload limit — a 92 MB 720p video should arrive as
# 480p/360p, not as "file too large".
_HEIGHT_LADDER = (720, 480, 360, 240)
# Per height: normal audio first, then speech-grade audio. On a 43-minute
# video the 129 kbps track alone is 41 MB; 48 kbps (16 MB) + 480p fits 50 MB.
_AUDIO_TIERS = (192, 64)

# Keys a previous format selection leaves on the info dict. They must go
# before re-selecting, or estimate_size would read the old pick's sizes.
_SELECTION_KEYS = (
    "requested_formats", "requested_downloads", "format_id", "format", "ext",
    "filesize", "filesize_approx", "tbr", "vbr", "abr", "width", "height",
    "resolution", "fps", "vcodec", "acodec", "protocol", "url", "manifest_url",
    "fragments", "dynamic_range", "format_note", "language",
)


def fallback_heights(quality: str) -> list[int]:
    """Heights below the requested video quality, best first ([] for mp3)."""
    if quality == "mp3":
        return []
    top = 1080 if quality in ("1080", "1080p") else 720
    return [h for h in _HEIGHT_LADDER if h < top]


def _reselect_smaller(ydl, info: dict, quality: str, max_bytes: int) -> dict:
    """Re-run format selection on *info* for lower heights; first that fits wins.

    Raises the original too_large DownloadError when nothing fits.
    """
    top = 1080 if quality in ("1080", "1080p") else 720
    # The requested height itself with lighter audio comes first.
    for h in [top] + fallback_heights(quality):
        for abr in _AUDIO_TIERS:
            if h == top and abr == _AUDIO_TIERS[0]:
                continue  # that is the selection that was just rejected
            sel = _format_selector(str(h), max_bytes, audio_abr=abr)
            ydl.format_selector = ydl.build_format_selector(sel)
            candidate = {k: v for k, v in info.items() if k not in _SELECTION_KEYS}
            try:
                candidate = ydl.process_ie_result(candidate, download=False)
            except yt_dlp.utils.DownloadError:
                continue  # no format at this height
            if not isinstance(candidate, dict):
                continue
            try:
                check_limits(candidate, quality)
            except DownloadError as e:
                if e.kind == "too_large":
                    continue
                raise
            logger.info(f"Requested {quality}p too large; delivering ≤{h}p with ≤{abr} kbps audio")
            return candidate
    raise DownloadError("No format fits the upload limit", kind="too_large")


# aria2c's first request carries no Range header, and googlevideo serves
# Range-less requests at ~30 KB/s (measured: 300 KB in 9.6 s, 10 MB in 318 s
# via curl). yt-dlp's native downloader always fetches in Range chunks and is
# not throttled. aria2c only recovers the loss when there are many pieces to
# parallelise: 68 MB took 20 s with aria2c vs 45 s native, while 10 MB took
# 12 s vs ~4 s and a 300 KB audio track 10 s vs <1 s. Streams below this
# size therefore go through the native downloader.
ARIA2C_MIN_BYTES = 25 * 1024 * 1024


def expected_stream_bytes(info: dict) -> Optional[int]:
    """Size of the largest stream yt-dlp is about to fetch, or None if unknown."""
    fmts = info.get("requested_formats") or [info]
    sizes = [f.get("filesize") or f.get("filesize_approx") for f in fmts]
    if not sizes or not all(isinstance(s, (int, float)) and s > 0 for s in sizes):
        return None
    return int(max(sizes))


def _pick_downloader(params: dict, info: dict) -> None:
    """Drop the external downloader for this run when the streams are small.

    *params* is the very dict handed to ``YoutubeDL`` (it keeps a reference,
    not a copy) and is consulted again at download time, so the switch takes
    effect without re-extracting.
    """
    if not params.get("external_downloader"):
        return
    size = expected_stream_bytes(info)
    if size is not None and size < ARIA2C_MIN_BYTES:
        params["external_downloader"] = None


def run_ydl(url: str, opts: dict, quality: Optional[str] = None) -> tuple[dict, float, float]:
    """Two-pass yt-dlp run shared by every download path.

    Pass 1 extracts metadata and selects formats; ``check_limits`` (when
    *quality* is given) rejects oversized/overlong media before any byte is
    fetched. Pass 2 downloads the resolved info dict without re-extracting.
    On a YouTube 403 the whole run is retried once with the fallback clients.
    Returns (info, t_extract, t_download); raises DownloadError.
    """
    attempts = [dict(opts)]  # copy: _pick_downloader edits the dict yt-dlp holds
    if platform_for_host(safe_hostname(url) or "") == "youtube":
        for clients in YOUTUBE_FALLBACK_CLIENT_SETS:
            fallback = dict(opts)
            fallback["extractor_args"] = youtube_extractor_args(clients)
            attempts.append(fallback)

    for i, o in enumerate(attempts):
        t0 = time.monotonic()
        try:
            with yt_dlp.YoutubeDL(o) as ydl:
                info = ydl.extract_info(url, download=False)
                if not isinstance(info, dict):
                    raise DownloadError("No media info", kind="generic")
                if quality:
                    try:
                        check_limits(info, quality)
                    except DownloadError as e:
                        if e.kind != "too_large" or not fallback_heights(quality):
                            raise
                        info = _reselect_smaller(ydl, info, quality, settings.max_video_bytes)
                t_extract = time.monotonic() - t0
                _pick_downloader(o, info)
                t1 = time.monotonic()
                processed = ydl.process_ie_result(info, download=True)
                return (processed if isinstance(processed, dict) else info,
                        t_extract, time.monotonic() - t1)
        except yt_dlp.utils.DownloadError as e:
            if i + 1 < len(attempts) and _is_forbidden(e):
                logger.warning(f"YouTube 403 with {o['extractor_args']['youtube']['player_client']}; "
                               f"retrying with fallback clients: {url}")
                _remove_partials(o.get("outtmpl"))
                continue
            raise classify_download_error(e) from e
    raise DownloadError("unreachable", kind="generic")  # pragma: no cover


def _download_sync(url: str, output_path: str, quality: str) -> DownloadResult:
    info, t_extract, t_download = run_ydl(url, _build_opts(output_path, quality), quality)
    expected = (strip_media_ext(output_path) + ".mp3") if quality == "mp3" else output_path
    out = resolve_output_path(info, expected)
    size = os.path.getsize(out) if os.path.isfile(out) else 0
    limit = settings.max_file_bytes if quality == "mp3" else settings.max_video_bytes
    if size > limit:
        raise DownloadError("Too large", kind="too_large")
    return DownloadResult(out, t_extract, t_download, size)


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
    info, _, _ = run_ydl(url, opts)
    return resolve_output_path(info, output_path)


async def download_audio_clip(url: str, output_path: str) -> str:
    url = await resolve_shortlink(url)
    return await asyncio.to_thread(_download_audio_clip_sync, url, output_path)
