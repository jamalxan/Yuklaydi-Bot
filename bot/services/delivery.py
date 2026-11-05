"""Sending a (possibly multi-part) cached video.

A video bigger than the upload limit is delivered as several parts. Their
Telegram file_ids are cached together under the usual url+quality key,
joined with ``|`` (file_ids are base64url and never contain it), so a cache
hit re-sends every part, not only the first.
"""
from __future__ import annotations

import asyncio
from typing import Iterable, Optional

from aiogram import Bot

from bot.config import settings
from bot.logger import logger
from bot.texts import t
from bot.utils.text import esc

PART_SEP = "|"


def join_file_ids(file_ids: Iterable[str]) -> str:
    return PART_SEP.join(file_ids)


def split_file_ids(cached: str) -> list[str]:
    return [f for f in cached.split(PART_SEP) if f]


def part_caption(caption: str, index: int, total: int, lang: Optional[str]) -> str:
    """Caption for part *index* (1-based) of *total*; unchanged for one part."""
    if total <= 1:
        return caption
    return f"{caption}\n{t('dl.part', lang, i=index, n=total)}"


async def send_cached_video(
    bot: Bot, chat_id: int, cached: str, caption: str,
    lang: Optional[str], reply_markup=None, send=None, **extra,
):
    """Send every part of a cached video; the keyboard goes on the last part.

    *send* defaults to ``bot.send_video``; callers pass their flood-retry
    wrapper as ``send=lambda **kw: retry(lambda: bot.send_video(**kw))``.
    *extra* (e.g. reply_parameters) goes to the first part. Returns the last
    sent message.
    """
    ids = split_file_ids(cached)
    send = send or (lambda **kw: bot.send_video(**kw))
    sent = None
    for i, file_id in enumerate(ids, 1):
        sent = await send(
            chat_id=chat_id, video=file_id,
            caption=part_caption(caption, i, len(ids), lang),
            supports_streaming=True,
            reply_markup=reply_markup if i == len(ids) else None,
            **(extra if i == 1 else {}),
        )
    return sent


# -- How a delivered video / audio looks ----------------------------------------

def bot_handle() -> str:
    return f"@{settings.bot_username}"


def video_caption(title: Optional[str], url: str = "", quality: Optional[str] = None,
                  full: bool = False) -> str:
    """Caption of a delivered video (HTML).

    Default delivery: the title and the bot handle. A quality picked from the
    buttons (*full*) also names the source link and the quality.
    """
    lines = []
    if title:
        lines.append(f"🍿 <code>{esc(title)}</code>" if full else f"🍿 {esc(title)}")
    if full and url:
        lines.append(f"🔗 {esc(url)}")
    if full and quality and str(quality).isdigit():
        lines.append(f"📺 {quality}p")
    lines.append(f"👉 {bot_handle()}")
    return "\n\n".join(lines)


def audio_caption() -> str:
    return f"👉 {bot_handle()}"


def progress_caption(title: Optional[str], lang: Optional[str]) -> str:
    """Caption shown on a video while another quality of it is fetched."""
    head = f"🍿 {esc(title)}\n\n" if title else ""
    return head + t("dl.downloading", lang)


def offered_qualities(platform: Optional[str], meta: dict) -> list[int]:
    """Quality buttons are offered where the source has a real ladder (YouTube)."""
    if platform != "youtube":
        return []
    return [int(q) for q in (meta.get("qualities") or []) if str(q).isdigit()]


async def video_presentation(url: str, platform: Optional[str], quality: str,
                             lang: Optional[str], meta: Optional[dict] = None,
                             full: bool = False):
    """(caption, keyboard) for a video, from fresh or cached metadata."""
    from bot.keyboards.inline import video_keyboard
    from bot.services.cache import cached_qualities, get_video_meta, url_key_for

    if meta is None:
        try:
            meta = await get_video_meta(url)
        except Exception as e:
            logger.warning(f"Video meta read failed: {e}")
            meta = {}
    qualities = offered_qualities(platform, meta)
    ready: set[str] = set()
    if qualities:
        try:
            ready = await cached_qualities(url, qualities)
        except Exception as e:
            logger.warning(f"Cached-quality lookup failed: {e}")
    if quality:
        ready.add(str(quality))
    kb = video_keyboard(url_key_for(url), lang or settings.default_lang, qualities, ready)
    return video_caption(meta.get("title"), url, quality, full=full), kb


async def register_video_buttons(url: str, file_id: str, meta: Optional[dict] = None) -> None:
    """Make the buttons under a delivered video resolvable (one key per URL)."""
    from bot.services.cache import set_video_meta, store_url_key, store_video_for_shazam, url_key_for

    jobs = [store_url_key(url),
            store_video_for_shazam(split_file_ids(file_id)[0], source_url=url, key=url_key_for(url))]
    if meta and (meta.get("title") or meta.get("qualities")):
        jobs.append(set_video_meta(url, meta))
    results = await asyncio.gather(*jobs, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.warning(f"Video button registration failed: {r}")
