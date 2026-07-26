from __future__ import annotations

"""Shared arq Redis pool.

Creating an arq pool per update (create_pool → enqueue → aclose) opens and
tears down a TCP connection on every message. At high RPS this exhausts
ephemeral ports and floods Redis with connection churn. One process-wide
pool is created lazily and reused for every enqueue.
"""

import asyncio
from typing import Optional

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from bot.config import settings

_pool: Optional[ArqRedis] = None
_pool_lock = asyncio.Lock()


async def get_arq_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:  # double-checked under lock
                _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool


async def close_arq_pool() -> None:
    global _pool
    if _pool is not None:
        try:
            await _pool.aclose()
        except Exception:
            pass
        _pool = None
