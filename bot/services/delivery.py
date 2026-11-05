"""Sending a (possibly multi-part) cached video.

A video bigger than the upload limit is delivered as several parts. Their
Telegram file_ids are cached together under the usual url+quality key,
joined with ``|`` (file_ids are base64url and never contain it), so a cache
hit re-sends every part, not only the first.
"""
from __future__ import annotations

from typing import Iterable, Optional

from aiogram import Bot

from bot.texts import t

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
    lang: Optional[str], reply_markup=None, send=None,
) -> None:
    """Send every part of a cached video; the keyboard goes on the last part.

    *send* defaults to ``bot.send_video``; callers pass their flood-retry
    wrapper as ``send=lambda **kw: retry(lambda: bot.send_video(**kw))``.
    """
    ids = split_file_ids(cached)
    send = send or (lambda **kw: bot.send_video(**kw))
    for i, file_id in enumerate(ids, 1):
        await send(
            chat_id=chat_id, video=file_id,
            caption=part_caption(caption, i, len(ids), lang),
            supports_streaming=True,
            reply_markup=reply_markup if i == len(ids) else None,
        )
