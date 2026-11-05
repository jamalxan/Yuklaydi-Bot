"""Broadcast engine — pure logic, injected I/O (unit-testable without Telegram/DB).

Runs inside the arq worker (``broadcast_task``), not in the bot's update
handler: a 100k-user broadcast takes over an hour and must survive a restart.
Progress (keyset offset + counters) is persisted after every page, so a
re-run continues where the previous attempt stopped.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Optional

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

logger = logging.getLogger("yuklaydi.broadcast")

RATE_PER_SECOND = 25          # Telegram's safe bulk rate for one bot
PROGRESS_EVERY = 500          # admin status message is edited every N recipients
PAGE_SIZE = 1000
MAX_RETRY_AFTER_ATTEMPTS = 3


@dataclass
class BroadcastProgress:
    last_id: int = 0
    delivered: int = 0
    failed: int = 0
    blocked: int = 0

    @property
    def processed(self) -> int:
        return self.delivered + self.failed + self.blocked

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "BroadcastProgress":
        d = d or {}
        return cls(**{k: int(d.get(k, 0) or 0) for k in ("last_id", "delivered", "failed", "blocked")})


async def send_one(bot: Any, tg_id: int, from_chat_id: int, message_id: int,
                   reply_markup: Any = None,
                   sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> str:
    """Deliver to one user. Returns "ok", "blocked" or "failed".

    RetryAfter → wait the requested time and retry THE SAME user (the old
    loop counted it as a failure and moved on, silently skipping people).
    """
    for _ in range(MAX_RETRY_AFTER_ATTEMPTS):
        try:
            await bot.copy_message(chat_id=tg_id, from_chat_id=from_chat_id,
                                   message_id=message_id, reply_markup=reply_markup)
            return "ok"
        except TelegramRetryAfter as e:
            await sleep(float(getattr(e, "retry_after", 5)) + 0.5)
        except TelegramForbiddenError:
            return "blocked"     # user blocked the bot / deactivated
        except TelegramBadRequest as e:
            logger.debug(f"Broadcast bad request for {tg_id}: {e}")
            return "failed"      # chat not found, etc.
        except Exception as e:
            logger.warning(f"Broadcast send to {tg_id} failed: {e}")
            return "failed"
    return "failed"


async def run_broadcast(
    bot: Any, *,
    from_chat_id: int,
    message_id: int,
    reply_markup: Any,
    progress: BroadcastProgress,
    fetch_page: Callable[[int, int], Awaitable[list[int]]],
    mark_blocked: Callable[[list[int]], Awaitable[None]],
    save_progress: Callable[[BroadcastProgress], Awaitable[None]],
    on_progress: Optional[Callable[[BroadcastProgress], Awaitable[None]]] = None,
    rate: int = RATE_PER_SECOND,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> BroadcastProgress:
    interval = 1.0 / max(1, rate)
    while True:
        ids = await fetch_page(progress.last_id, PAGE_SIZE)
        if not ids:
            return progress
        blocked: list[int] = []
        for tg_id in ids:
            status = await send_one(bot, tg_id, from_chat_id, message_id, reply_markup, sleep=sleep)
            if status == "ok":
                progress.delivered += 1
            elif status == "blocked":
                progress.blocked += 1
                blocked.append(tg_id)
            else:
                progress.failed += 1
            progress.last_id = tg_id
            if on_progress and progress.processed % PROGRESS_EVERY == 0:
                try:
                    await on_progress(progress)
                except Exception:
                    pass
            await sleep(interval)
        if blocked:
            await mark_blocked(blocked)
        await save_progress(progress)
