from __future__ import annotations

import hashlib
import json
from typing import Optional

import redis.asyncio as aioredis

from bot.config import settings

_redis: Optional[aioredis.Redis] = None

FILE_ID_TTL = 60 * 60 * 24 * 30   # 30 days
SONG_TTL = 60 * 60 * 24 * 7        # 7 days
USER_LANG_TTL = 60 * 60 * 24       # 1 day
MUSIC_SEARCH_TTL = 60 * 15         # 15 min
VIDEO_SHAZAM_TTL = 60 * 60         # 1 hour
URL_KEY_TTL = 60 * 60 * 24         # 24 hours
SETTING_TTL = 60 * 5               # 5 min


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            # Fail fast instead of hanging the event loop when Redis stalls
            socket_connect_timeout=5,
            socket_timeout=10,
            retry_on_timeout=True,
            # Detect dead connections left behind by restarts/failovers
            health_check_interval=30,
            # Hard cap so a runaway burst can't open unbounded connections
            max_connections=settings.redis_max_connections,
        )
    return _redis


# ── File ID cache ─────────────────────────────────────────────────────────────

def _fid_key(url: str, quality: str) -> str:
    h = hashlib.sha256(f"{url}|{quality}".encode()).hexdigest()
    return f"fid:{h}"


async def get_file_id(url: str, quality: str) -> Optional[str]:
    return await (await get_redis()).get(_fid_key(url, quality))


async def set_file_id(url: str, quality: str, file_id: str) -> None:
    await (await get_redis()).setex(_fid_key(url, quality), FILE_ID_TTL, file_id)


# ── Song / lyrics cache ───────────────────────────────────────────────────────

def _song_key(title: str, artist: str) -> str:
    h = hashlib.sha256(f"{title.lower()}|{artist.lower()}".encode()).hexdigest()
    return f"song:{h}"


async def get_song_cache(title: str, artist: str) -> Optional[dict]:
    v = await (await get_redis()).get(_song_key(title, artist))
    return json.loads(v) if v else None


async def set_song_cache(title: str, artist: str, data: dict) -> None:
    await (await get_redis()).setex(_song_key(title, artist), SONG_TTL, json.dumps(data))


# ── User language cache ───────────────────────────────────────────────────────

async def get_user_lang(tg_id: int) -> Optional[str]:
    return await (await get_redis()).get(f"ulang:{tg_id}")


async def set_user_lang(tg_id: int, lang: str) -> None:
    await (await get_redis()).setex(f"ulang:{tg_id}", USER_LANG_TTL, lang)


# ── Rate limiting ─────────────────────────────────────────────────────────────

async def check_rate_limit(tg_id: int) -> tuple[bool, int]:
    r = await get_redis()
    key = f"rl:{tg_id}"
    pipe = r.pipeline()
    await pipe.incr(key)
    await pipe.ttl(key)
    results = await pipe.execute()
    count, ttl = results[0], results[1]
    # Set the window on first hit; also repair a key that lost its TTL
    # (crash between INCR and EXPIRE would otherwise rate-limit forever).
    if count == 1 or ttl == -1:
        await r.expire(key, settings.rate_limit_window)
        ttl = settings.rate_limit_window
    if count > settings.rate_limit_requests:
        return False, max(ttl, 1)
    return True, 0


# ── Music search cache ────────────────────────────────────────────────────────

def _mskey(query: str) -> tuple[str, str]:
    h = hashlib.sha256(query.lower().strip().encode()).hexdigest()[:16]
    return h, f"ms:{h}"


async def get_music_search(query: str) -> Optional[list]:
    _, key = _mskey(query)
    v = await (await get_redis()).get(key)
    return json.loads(v) if v else None


async def set_music_search(query: str, entries: list) -> str:
    qhash, key = _mskey(query)
    await (await get_redis()).setex(key, MUSIC_SEARCH_TTL, json.dumps(entries))
    return qhash


async def get_music_search_by_hash(qhash: str) -> Optional[list]:
    v = await (await get_redis()).get(f"ms:{qhash}")
    return json.loads(v) if v else None


# ── Video-for-Shazam short key ────────────────────────────────────────────────

async def store_video_for_shazam(file_id: str, file_suffix: str = ".mp4",
                                 source_url: str = "") -> str:
    import hashlib
    import time
    key = hashlib.md5(f"{file_id}{time.time()}".encode()).hexdigest()[:10]
    r = await get_redis()
    payload: dict = {"fid": file_id, "suf": file_suffix}
    if source_url:
        payload["url"] = source_url
    await r.setex(f"vshz:{key}", VIDEO_SHAZAM_TTL, json.dumps(payload))
    return key


async def get_video_for_shazam(key: str) -> Optional[dict]:
    v = await (await get_redis()).get(f"vshz:{key}")
    return json.loads(v) if v else None


# ── URL short key (for re-download buttons) ───────────────────────────────────

async def store_url_key(url: str) -> str:
    key = hashlib.sha256(url.encode()).hexdigest()[:12]
    r = await get_redis()
    await r.setex(f"vdurl:{key}", URL_KEY_TTL, url)
    return key


async def get_url_by_key(key: str) -> Optional[str]:
    return await (await get_redis()).get(f"vdurl:{key}")


# ── Audio file_id cache (music search) ───────────────────────────────────────

async def get_audio_file_id(video_id: str) -> Optional[str]:
    return await (await get_redis()).get(f"aud:{video_id}")


async def set_audio_file_id(video_id: str, file_id: str) -> None:
    await (await get_redis()).setex(f"aud:{video_id}", FILE_ID_TTL, file_id)


# ── Settings cache ────────────────────────────────────────────────────────────

async def get_setting(key: str) -> Optional[str]:
    v = await (await get_redis()).get(f"cfg:{key}")
    return v


async def set_setting_cache(key: str, value: str) -> None:
    await (await get_redis()).setex(f"cfg:{key}", SETTING_TTL, value)


async def invalidate_setting(key: str) -> None:
    await (await get_redis()).delete(f"cfg:{key}")


# ── Per-user concurrency slot (prevents one user starving workers) ─────────────

_SLOT_TTL = 300  # auto-expire in 5 min if worker crashes


async def get_active_downloads(user_id: int) -> int:
    r = await get_redis()
    v = await r.get(f"ucap:{user_id}")
    return int(v) if v else 0


async def acquire_user_slot(user_id: int) -> None:
    r = await get_redis()
    pipe = r.pipeline()
    await pipe.incr(f"ucap:{user_id}")
    await pipe.expire(f"ucap:{user_id}", _SLOT_TTL)
    await pipe.execute()


# Atomic floor-at-zero decrement — a GET/DECR pair races under concurrency
# and can push the counter negative, permanently freeing extra slots.
_RELEASE_SLOT_LUA = """
local v = tonumber(redis.call('GET', KEYS[1]) or '0')
if v > 0 then return redis.call('DECR', KEYS[1]) end
return 0
"""


async def release_user_slot(user_id: int) -> None:
    r = await get_redis()
    try:
        await r.eval(_RELEASE_SLOT_LUA, 1, f"ucap:{user_id}")
    except Exception:
        # Fallback for Redis builds without EVAL (tests/mocks)
        key = f"ucap:{user_id}"
        v = await r.get(key)
        if v and int(v) > 0:
            await r.decr(key)


# ── Single-flight lock (prevents N workers downloading the same URL) ───────────

async def acquire_single_flight(url: str, quality: str, ttl: int = 120) -> bool:
    """Returns True if this caller won the lock (should download).
    Returns False if another worker is already downloading this URL+quality."""
    h = hashlib.sha256(f"{url}|{quality}".encode()).hexdigest()
    r = await get_redis()
    ok = await r.set(f"sf:{h}", "1", nx=True, ex=ttl)
    return bool(ok)


async def release_single_flight(url: str, quality: str) -> None:
    h = hashlib.sha256(f"{url}|{quality}".encode()).hexdigest()
    await (await get_redis()).delete(f"sf:{h}")


# ── Queue depth monitoring ────────────────────────────────────────────────────

async def get_queue_depth(queue_name: str = "arq:queue") -> int:
    r = await get_redis()
    # arq uses sorted sets (ZADD/ZCARD) for its queues, not lists
    v = await r.zcard(queue_name)
    return v or 0


# ── Song metadata short-key (for safe callback_data ≤ 64 bytes) ──────────────

async def store_song_meta(title: str, artist: str) -> str:
    """Persist title+artist in Redis; return 12-char hex key for callback_data."""
    h = hashlib.sha256(f"{title}|{artist}".encode()).hexdigest()[:12]
    r = await get_redis()
    await r.setex(f"smeta:{h}", SONG_TTL, json.dumps({"title": title, "artist": artist}))
    return h


async def get_song_meta(h: str) -> Optional[dict]:
    v = await (await get_redis()).get(f"smeta:{h}")
    return json.loads(v) if v else None


# ── Channel-membership cache ──────────────────────────────────────────────────
# get_chat_member is a Telegram API round-trip; without caching it runs on
# EVERY message when a required channel is configured.

_MEMBER_TTL = 600  # 10 min — re-check membership at most every 10 minutes


async def get_cached_membership(user_id: int) -> Optional[bool]:
    v = await (await get_redis()).get(f"chmem:{user_id}")
    if v is None:
        return None
    return v == "1"


async def set_cached_membership(user_id: int, is_member: bool) -> None:
    # Non-members get a short TTL so a fresh subscription is noticed quickly
    ttl = _MEMBER_TTL if is_member else 30
    await (await get_redis()).setex(f"chmem:{user_id}", ttl, "1" if is_member else "0")


async def invalidate_membership(user_id: int) -> None:
    await (await get_redis()).delete(f"chmem:{user_id}")


# ── Health ────────────────────────────────────────────────────────────────────

async def ping_redis() -> bool:
    try:
        return await (await get_redis()).ping()
    except Exception:
        return False
