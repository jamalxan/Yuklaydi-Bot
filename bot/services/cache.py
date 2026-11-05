from __future__ import annotations

import hashlib
import json
import secrets
from typing import Optional

import redis.asyncio as aioredis

from bot.config import settings

_redis: Optional[aioredis.Redis] = None

FILE_ID_TTL = 60 * 60 * 24 * 30   # 30 days
SONG_TTL = 60 * 60 * 24 * 7        # 7 days
USER_LANG_TTL = 60 * 60 * 24       # 1 day
MUSIC_SEARCH_TTL = 60 * 15         # 15 min
# Buttons under a delivered video (quality / audio / find song) keep working
# this long; they used to die after 1 h ("Video topilmadi").
VIDEO_SHAZAM_TTL = 60 * 60 * 24 * 3  # 3 days
URL_KEY_TTL = 60 * 60 * 24 * 3       # 3 days
VIDEO_META_TTL = FILE_ID_TTL         # title/qualities live as long as the file_ids
SETTING_TTL = 60 * 5               # 5 min


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_dsn,
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


async def cached_qualities(url: str, qualities) -> set[str]:
    """Which of *qualities* already have a file_id for *url* (one round-trip)."""
    qs = [str(q) for q in qualities]
    if not qs:
        return set()
    values = await (await get_redis()).mget([_fid_key(url, q) for q in qs])
    return {q for q, v in zip(qs, values) if v}


# -- Video metadata (caption + quality buttons for cache hits) ----------------

def url_key_for(url: str) -> str:
    """Stable 12-hex key of a canonical URL (fits any callback_data)."""
    return hashlib.sha256(url.encode()).hexdigest()[:12]


async def set_video_meta(url: str, meta: dict) -> None:
    keep = {k: meta.get(k) for k in ("title", "uploader", "duration", "width", "height",
                                     "qualities", "thumbnail") if meta.get(k)}
    await (await get_redis()).setex(f"vmeta:{url_key_for(url)}", VIDEO_META_TTL,
                                    json.dumps(keep, ensure_ascii=False))


async def get_video_meta(url: str) -> dict:
    v = await (await get_redis()).get(f"vmeta:{url_key_for(url)}")
    try:
        return json.loads(v) if v else {}
    except ValueError:
        return {}


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
                                 source_url: str = "", key: Optional[str] = None) -> str:
    # Callers that build the keyboard BEFORE the upload pass the URL key, so
    # the buttons ride on the video itself instead of a second edit call.
    key = key or secrets.token_hex(5)  # 10 hex chars, fits callback_data
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
    key = url_key_for(url)
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

# Auto-expire if a worker dies mid-job. Must outlive the longest job, or the
# counter vanishes while a download is still running and the cap leaks.
_SLOT_TTL = settings.job_timeout + 60


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


# Atomic "reserve a slot if under the cap" — called at ENQUEUE time so jobs
# still waiting in the queue count against the user's cap.
_TRY_ACQUIRE_SLOT_LUA = """
local v = tonumber(redis.call('GET', KEYS[1]) or '0')
if v >= tonumber(ARGV[1]) then return 0 end
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return 1
"""


async def try_acquire_user_slot(user_id: int, cap: int) -> bool:
    """Reserve one download slot; False if the user already has *cap* active."""
    r = await get_redis()
    key = f"ucap:{user_id}"
    try:
        return bool(await r.eval(_TRY_ACQUIRE_SLOT_LUA, 1, key, cap, _SLOT_TTL))
    except Exception:
        # Fallback for Redis builds without EVAL (tests/mocks): non-atomic
        v = await r.get(key)
        if v and int(v) >= cap:
            return False
        await acquire_user_slot(user_id)
        return True


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


async def single_flight_held(url: str, quality: str) -> bool:
    """True while another worker holds the download lock for url+quality."""
    h = hashlib.sha256(f"{url}|{quality}".encode()).hexdigest()
    return bool(await (await get_redis()).exists(f"sf:{h}"))


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


# ── Ban list (checked on every update — must be O(1), no DB hit) ─────────────

_BAN_SET = "banned_users"


async def is_banned(user_id: int) -> bool:
    return bool(await (await get_redis()).sismember(_BAN_SET, str(user_id)))


async def set_banned(user_id: int, banned: bool) -> None:
    r = await get_redis()
    if banned:
        await r.sadd(_BAN_SET, str(user_id))
    else:
        await r.srem(_BAN_SET, str(user_id))


async def load_banned(user_ids: list[int]) -> None:
    """Replace the Redis ban set with the DB truth (called at startup)."""
    r = await get_redis()
    pipe = r.pipeline()
    await pipe.delete(_BAN_SET)
    if user_ids:
        await pipe.sadd(_BAN_SET, *[str(u) for u in user_ids])
    await pipe.execute()


# ── "User seen" marker (throttles the per-update DB upsert) ──────────────────

USER_SEEN_TTL = 60 * 60  # upsert a user's profile at most once per hour


async def mark_user_seen(user_id: int) -> bool:
    """True if this is the first sighting within USER_SEEN_TTL (caller upserts)."""
    ok = await (await get_redis()).set(f"useen:{user_id}", "1", nx=True, ex=USER_SEEN_TTL)
    return bool(ok)


async def forget_user_seen(user_id: int) -> None:
    await (await get_redis()).delete(f"useen:{user_id}")


# ── Callback throttling ──────────────────────────────────────────────────────

async def check_callback_rate(user_id: int, data: str) -> tuple[bool, str]:
    """Return (allowed, reason). reason is "debounce" or "rate" when blocked.

    Identical (user, callback_data) presses within callback_debounce_seconds
    are dropped; on top of that a softer per-user window limit applies.
    """
    r = await get_redis()
    dkey = "cbd:" + hashlib.sha256(f"{user_id}|{data}".encode()).hexdigest()[:24]
    if not await r.set(dkey, "1", nx=True, ex=max(1, settings.callback_debounce_seconds)):
        return False, "debounce"
    key = f"rlc:{user_id}"
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, settings.rate_limit_window)
    if count > settings.callback_rate_limit_requests:
        return False, "rate"
    return True, ""


# ── Broadcast lock / progress ─────────────────────────────────────────────────

BROADCAST_LOCK = "broadcast:lock"
BROADCAST_STATE = "broadcast:state"


async def acquire_broadcast_lock(owner: str, ttl: int = 6 * 3600) -> bool:
    return bool(await (await get_redis()).set(BROADCAST_LOCK, owner, nx=True, ex=ttl))


async def release_broadcast_lock() -> None:
    await (await get_redis()).delete(BROADCAST_LOCK)


# ── Health ────────────────────────────────────────────────────────────────────

async def ping_redis() -> bool:
    try:
        return await (await get_redis()).ping()
    except Exception:
        return False
