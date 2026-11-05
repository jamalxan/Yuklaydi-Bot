"""Split a video into upload-sized parts WITHOUT re-encoding.

The Telegram cloud Bot API refuses uploads above 50 MB. Lowering the
resolution keeps a long video under that, but the user asked for the quality
they picked — so instead the finished MP4 is cut with ``ffmpeg -c copy`` (a
remux, no quality loss, seconds per GB) into equal-length parts that each fit
the upload limit. Cuts land on keyframes, so part sizes vary a little; a part
that still overshoots triggers a retry with more, shorter parts.
"""
from __future__ import annotations

import asyncio
import glob
import math
import os

from bot.logger import logger
from bot.services.media import ffmpeg_path, probe_duration, safe_delete

# Aim each part at this share of the limit: keyframe-aligned cuts and
# bitrate spikes (action scenes) make real parts larger than the average.
PART_HEADROOM = 0.9
MAX_ATTEMPTS = 4


class SplitError(Exception):
    """The file could not be cut into parts that fit the limit."""


def _part_pattern(path: str) -> str:
    return f"{os.path.splitext(path)[0]}_part%03d.mp4"


def part_paths(path: str) -> list[str]:
    """Existing part files produced from *path*, in playback order."""
    base = glob.escape(os.path.splitext(path)[0])
    return sorted(glob.glob(f"{base}_part[0-9][0-9][0-9].mp4"))


def remove_parts(path: str) -> None:
    for p in part_paths(path):
        safe_delete(p)


def plan_parts(size_bytes: int, max_part_bytes: int, headroom: float = PART_HEADROOM) -> int:
    """How many equal-length parts a file of *size_bytes* needs."""
    return max(2, math.ceil(size_bytes / (max_part_bytes * headroom)))


async def _run_segment(path: str, segment_seconds: float) -> None:
    cmd = [
        ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c", "copy",
        "-f", "segment",
        "-segment_time", f"{segment_seconds:.3f}",
        "-reset_timestamps", "1",
        "-segment_format", "mp4",
        # moov atom first: each part starts playing before it fully loads
        "-segment_format_options", "movflags=+faststart",
        _part_pattern(path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise SplitError(f"ffmpeg rc={proc.returncode}: {stderr.decode(errors='replace')[:300]}")


async def split_video(path: str, max_part_bytes: int) -> list[str]:
    """Cut *path* into parts no larger than *max_part_bytes* each.

    Returns ``[path]`` unchanged when it already fits. On success the parts
    are new files next to *path* (``<base>_part000.mp4`` …); the caller owns
    them and should call :func:`remove_parts` when done. Raises SplitError.
    """
    size = os.path.getsize(path)
    if size <= max_part_bytes:
        return [path]

    duration = await probe_duration(path)
    if duration <= 0:
        raise SplitError("unknown duration")

    n = plan_parts(size, max_part_bytes)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        remove_parts(path)
        await _run_segment(path, duration / n)
        parts = part_paths(path)
        if not parts:
            raise SplitError("ffmpeg produced no parts")
        biggest = max(os.path.getsize(p) for p in parts)
        if biggest <= max_part_bytes:
            logger.info(f"Split {size / 1048576:.1f} MiB video into {len(parts)} parts "
                        f"(largest {biggest / 1048576:.1f} MiB, attempt {attempt})")
            return parts
        # Shrink the parts in proportion to the overshoot, plus one.
        n = max(n + 1, math.ceil(n * biggest / (max_part_bytes * PART_HEADROOM)))

    remove_parts(path)
    raise SplitError(f"parts still above {max_part_bytes} bytes after {MAX_ATTEMPTS} attempts")
