from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile

from bot.logger import logger

_FFMPEG_PATH: str | None = None
_FFPROBE_PATH: str | None = None

_WIN_SEARCH_DIRS = [
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\ProgramData\chocolatey\bin",
    r"C:\tools\ffmpeg\bin",
]


def _find_binary(name: str) -> str:
    """Return the path to ffmpeg/ffprobe, searching PATH and common Windows dirs."""
    found = shutil.which(name)
    if found:
        return found
    for d in _WIN_SEARCH_DIRS:
        candidate = os.path.join(d, name + ".exe")
        if os.path.isfile(candidate):
            return candidate
    return name  # fallback — let subprocess raise a useful error


def ffmpeg_path() -> str:
    global _FFMPEG_PATH
    if _FFMPEG_PATH is None:
        _FFMPEG_PATH = _find_binary("ffmpeg")
    return _FFMPEG_PATH


def ffprobe_path() -> str:
    global _FFPROBE_PATH
    if _FFPROBE_PATH is None:
        _FFPROBE_PATH = _find_binary("ffprobe")
    return _FFPROBE_PATH


async def probe_duration(input_path: str) -> float:
    """Return media duration in seconds via ffprobe (0.0 on failure).

    Checks format duration first, then falls back to the first audio/video
    stream — OGG/Opus and some containers only expose duration in streams.
    """
    try:
        probe_cmd = [
            ffprobe_path(), "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            input_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *probe_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        raw = stdout.decode(errors="replace")
        if proc.returncode != 0:
            logger.warning(f"[probe] ffprobe error rc={proc.returncode}: {stderr.decode(errors='replace')[:300]}")
        info = json.loads(raw) if raw.strip() else {}
        fmt_dur = info.get("format", {}).get("duration", "")
        dur = float(fmt_dur or 0) if fmt_dur else 0.0
        source = "format"
        if not dur:
            for stream in info.get("streams", []):
                stream_dur = stream.get("duration")
                if stream_dur:
                    dur = float(stream_dur)
                    source = f"stream[{stream.get('codec_type','?')}]"
                    break
        fsize = os.path.getsize(input_path) if os.path.exists(input_path) else "missing"
        logger.debug(f"[probe] {input_path!r} dur={dur:.3f}s src={source} fsize={fsize}B")
        return dur
    except Exception as exc:
        logger.warning(f"[probe] failed for {input_path!r}: {exc}")
        return 0.0


async def extract_audio_snippet(
    input_path: str,
    output_path: str,
    snippet_duration: int = 15,
    start_override: float = -1.0,
) -> str:
    """Extract a mono 16kHz audio snippet from the media file.

    start_override: exact start position in seconds; -1.0 = auto (middle).
    """
    duration = await probe_duration(input_path) or 30.0

    if start_override >= 0:
        start = start_override
        snippet_duration = min(snippet_duration, max(1, int(duration - start_override)))
    else:
        start = max(0.0, (duration / 2) - (snippet_duration / 2))
        if duration < snippet_duration:
            start = 0.0
            snippet_duration = max(1, int(duration))

    cmd = [
        ffmpeg_path(), "-y",
        "-ss", str(start),
        "-i", input_path,
        "-t", str(snippet_duration),
        "-ac", "1",
        "-ar", "16000",
        "-vn",
        output_path,
    ]
    logger.debug(f"[snippet] ffmpeg start={start:.1f}s dur={snippet_duration}s → {output_path!r}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(f"[snippet] ffmpeg error (rc={proc.returncode}): {stderr.decode(errors='replace')[:400]}")
    else:
        out_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        logger.debug(f"[snippet] ok size={out_size}B")
    return output_path


def _delete_once(path: str) -> bool:
    """Try to delete *path*. True = gone (or never existed); False = locked."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except PermissionError:
        return False  # Windows: still held by the HTTP client (WinError 32)
    except Exception as e:
        logger.warning(f"Failed to delete temp file {path}: {e}")
        return True  # not retryable


def safe_delete(path: str | None) -> None:
    """Delete a temp file without ever blocking or raising.

    One attempt only — this is called from async code, and the old
    time.sleep() retry froze the event loop. A file that is still locked is
    left for the periodic cleanup task (see safe_delete_async for retries).
    """
    if not path:
        return
    if not _delete_once(path):
        logger.debug(f"Temp file locked, leaving for cleanup task: {path}")


async def safe_delete_async(path: str | None, attempts: int = 3, delay: float = 0.4) -> None:
    """Async delete with non-blocking retries for briefly locked files."""
    if not path:
        return
    for attempt in range(attempts):
        if _delete_once(path):
            return
        if attempt < attempts - 1:
            await asyncio.sleep(delay)
    logger.warning(f"Temp file still locked, leaving for cleanup task: {path}")


# Only files we create ourselves are ever swept (see get_temp_path callers).
TEMP_PREFIXES = ("ydl_", "shz_", "shz_in_", "mus_", "th_", "exp_")


def temp_dir() -> str:
    """Private scratch dir for our temp files: <download_dir>/tmp.

    download_dir itself is shared with the Local Bot API (its files live
    there) and historically held cookies.txt — the cleanup job must never
    touch anything outside this subdirectory.
    """
    from bot.config import settings
    path = os.path.join(settings.download_dir, "tmp")
    os.makedirs(path, exist_ok=True)
    return path


async def cleanup_old_files(directory: str | None = None, max_age_seconds: int = 3600) -> int:
    """Delete our own stale temp files (prefix-matched) from the tmp dir.

    Returns the number of files removed. Anything without one of
    TEMP_PREFIXES — cookies, Local Bot API data, user files — is left alone.
    """
    import time
    directory = directory or temp_dir()
    now = time.time()
    removed = 0
    try:
        for fname in os.listdir(directory):
            if not fname.startswith(TEMP_PREFIXES):
                continue
            fpath = os.path.join(directory, fname)
            try:
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > max_age_seconds:
                    if _delete_once(fpath) and not os.path.exists(fpath):
                        removed += 1
            except OSError:
                continue
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
    return removed


def get_temp_path(suffix: str = ".mp4", prefix: str = "ydl_") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix, dir=temp_dir())
    os.close(fd)
    # Remove the empty placeholder so yt-dlp/ffmpeg can write to this path cleanly.
    # The unique name is reserved; no other mkstemp call will reuse it in this process.
    try:
        os.unlink(path)
    except OSError:
        pass
    return path
