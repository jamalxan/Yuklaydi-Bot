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


def safe_delete(path: str) -> None:
    if not path:
        return
    # On Windows a just-uploaded file can still be held by the HTTP client for
    # a moment (WinError 32) — retry briefly before giving up. Leftovers are
    # swept by the hourly cleanup task either way.
    import time
    for attempt in range(3):
        try:
            if os.path.exists(path):
                os.remove(path)
            return
        except PermissionError:
            if attempt < 2:
                time.sleep(0.4)
        except Exception as e:
            logger.warning(f"Failed to delete temp file {path}: {e}")
            return
    logger.warning(f"Temp file still locked, leaving for cleanup task: {path}")


async def cleanup_old_files(directory: str, max_age_seconds: int = 3600) -> None:
    import time
    now = time.time()
    try:
        for fname in os.listdir(directory):
            fpath = os.path.join(directory, fname)
            if os.path.isfile(fpath):
                if now - os.path.getmtime(fpath) > max_age_seconds:
                    safe_delete(fpath)
    except Exception as e:
        logger.error(f"Cleanup error: {e}")


def get_temp_path(suffix: str = ".mp4", prefix: str = "ydl_") -> str:
    from bot.config import settings
    os.makedirs(settings.download_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix, dir=settings.download_dir)
    os.close(fd)
    # Remove the empty placeholder so yt-dlp/ffmpeg can write to this path cleanly.
    # The unique name is reserved; no other mkstemp call will reuse it in this process.
    try:
        os.unlink(path)
    except OSError:
        pass
    return path
