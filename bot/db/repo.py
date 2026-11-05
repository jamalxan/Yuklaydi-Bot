from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import AsyncIterator, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import (
    Download, MusicSearch, RecognitionLog,
    Setting, SongCache, User, WelcomeMessage,
)

logger = logging.getLogger("yuklaydi.repo")

SUPPORTED_LANGS = ("uz", "oz", "ru", "en")  # oz = Uzbek Cyrillic


# ── Time helpers: ONE definition of "today" for every statistic ──────────────

def _stats_zone():
    try:
        return ZoneInfo(settings.stats_tz)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown STATS_TZ=%r (is tzdata installed?) — using UTC", settings.stats_tz)
        return timezone.utc


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def local_today() -> date:
    return utcnow().astimezone(_stats_zone()).date()


def today_bounds(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """[local midnight, next local midnight) of the stats zone, as aware UTC datetimes.

    Range predicates (created_at >= start AND created_at < end) can use the
    created_at indexes; func.date(created_at) == today could not, and mixed
    date.today()/utcnow() put 00:00–05:00 Tashkent activity on the wrong day.
    """
    tz = _stats_zone()
    now = (now or utcnow()).astimezone(tz)
    start_local = datetime.combine(now.date(), time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def guess_lang(language_code: Optional[str]) -> str:
    code = (language_code or "").split("-")[0].lower()
    return code if code in SUPPORTED_LANGS else settings.default_lang


# ── Users ─────────────────────────────────────────────────────────────────────

class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_seen(self, tg_id: int, username: Optional[str] = None,
                          first_name: Optional[str] = None,
                          lang: Optional[str] = None) -> bool:
        """Register/refresh a user in ONE statement. Returns True if newly created.

        INSERT … ON CONFLICT DO UPDATE has no SELECT→INSERT race (the old
        get_or_create raised UniqueViolation on a double tap). ``xmax = 0`` is
        true only for a freshly inserted row.
        """
        stmt = insert(User).values(
            tg_id=tg_id, username=username, first_name=first_name,
            lang=lang or settings.default_lang, lang_chosen=False,
            last_active=func.now(), is_blocked=False, is_blocked_bot=False,
            is_admin=False, downloads_count=0, recognitions_count=0, searches_count=0,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[User.tg_id],
            set_={
                "username": stmt.excluded.username,
                "first_name": stmt.excluded.first_name,
                "last_active": func.now(),
                # They are talking to us again, so they unblocked the bot.
                "is_blocked_bot": False,
            },
        ).returning(literal_column("(xmax = 0)").label("inserted"))
        inserted = bool((await self.session.execute(stmt)).scalar_one())
        await self.session.commit()
        return inserted

    async def get_or_create(self, tg_id: int, username: Optional[str] = None,
                            first_name: Optional[str] = None, lang: str = "uz") -> User:
        """Race-free get-or-create (kept for callers of the old API)."""
        await self.upsert_seen(tg_id, username=username, first_name=first_name, lang=lang)
        user = await self.get(tg_id)
        if user is None:
            raise RuntimeError(f"user {tg_id} missing right after upsert")
        return user

    async def get(self, tg_id: int) -> Optional[User]:
        result = await self.session.execute(select(User).where(User.tg_id == tg_id))
        return result.scalar_one_or_none()

    async def touch(self, tg_id: int) -> None:
        await self.session.execute(
            update(User).where(User.tg_id == tg_id).values(last_active=func.now())
        )
        await self.session.commit()

    async def set_lang(self, tg_id: int, lang: str) -> None:
        await self.session.execute(
            update(User).where(User.tg_id == tg_id).values(lang=lang, lang_chosen=True)
        )
        await self.session.commit()

    async def set_blocked(self, tg_id: int, blocked: bool) -> None:
        """Ban/unban — works for IDs that never talked to the bot (row is created)."""
        stmt = insert(User).values(
            tg_id=tg_id, lang=settings.default_lang, lang_chosen=False,
            is_blocked=blocked, is_blocked_bot=False, is_admin=False,
            downloads_count=0, recognitions_count=0, searches_count=0,
        )
        stmt = stmt.on_conflict_do_update(index_elements=[User.tg_id], set_={"is_blocked": blocked})
        await self.session.execute(stmt)
        await self.session.commit()

    async def set_blocked_bot(self, tg_ids: list[int]) -> None:
        """Mark users who blocked the bot so broadcasts stop retrying them."""
        if not tg_ids:
            return
        await self.session.execute(
            update(User).where(User.tg_id.in_(tg_ids)).values(is_blocked_bot=True)
        )
        await self.session.commit()

    async def banned_ids(self) -> list[int]:
        r = await self.session.execute(select(User.tg_id).where(User.is_blocked.is_(True)))
        return list(r.scalars().all())

    async def inc_downloads(self, tg_id: int) -> None:
        await self.session.execute(
            update(User).where(User.tg_id == tg_id).values(downloads_count=User.downloads_count + 1)
        )
        await self.session.commit()

    async def inc_recognitions(self, tg_id: int) -> None:
        await self.session.execute(
            update(User).where(User.tg_id == tg_id).values(recognitions_count=User.recognitions_count + 1)
        )
        await self.session.commit()

    async def inc_searches(self, tg_id: int) -> None:
        await self.session.execute(
            update(User).where(User.tg_id == tg_id).values(searches_count=User.searches_count + 1)
        )
        await self.session.commit()

    async def count_all(self) -> int:
        r = await self.session.execute(select(func.count()).select_from(User))
        return r.scalar_one()

    # Broadcast recipients: not banned by us, not blocked the bot.
    _RECIPIENT = and_(User.is_blocked.is_not(True), User.is_blocked_bot.is_not(True))

    async def count_broadcast_recipients(self) -> int:
        r = await self.session.execute(select(func.count()).select_from(User).where(self._RECIPIENT))
        return r.scalar_one()

    async def broadcast_ids_after(self, last_tg_id: int, limit: int = 1000) -> list[int]:
        """Keyset page of recipients ordered by tg_id — resumable and O(page)."""
        r = await self.session.execute(
            select(User.tg_id)
            .where(self._RECIPIENT, User.tg_id > last_tg_id)
            .order_by(User.tg_id)
            .limit(limit)
        )
        return list(r.scalars().all())

    async def stream_for_export(self, batch: int = 1000) -> AsyncIterator[User]:
        """Yield users in batches instead of loading the whole table into memory."""
        result = await self.session.stream_scalars(
            select(User).order_by(User.id).execution_options(yield_per=batch)
        )
        async for user in result:
            yield user


# ── Downloads ─────────────────────────────────────────────────────────────────

class DownloadRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, user_id: int, url: str, platform: str, kind: str,
                     status: str = "done", from_cache: bool = False,
                     file_id: Optional[str] = None) -> Download:
        dl = Download(user_id=user_id, url=url, platform=platform, kind=kind,
                      status=status, from_cache=from_cache, file_id=file_id)
        self.session.add(dl)
        await self.session.commit()
        return dl

    async def count_all(self) -> int:
        r = await self.session.execute(select(func.count()).select_from(Download))
        return r.scalar_one()

    async def top_platforms(self, limit: int = 5) -> list:
        r = await self.session.execute(
            select(Download.platform, func.count().label("cnt"))
            .group_by(Download.platform)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return list(r.fetchall())


class RecognitionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def log(self, user_id: int, title: str, artist: str,
                  found_lyrics: bool = False, source: str = "shazam") -> None:
        rec = RecognitionLog(user_id=user_id, title=title, artist=artist,
                             found_lyrics=found_lyrics, source=source)
        self.session.add(rec)
        await self.session.commit()


class MusicSearchRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def log(self, user_id: int, query: str, picked_index: Optional[int] = None) -> None:
        row = MusicSearch(user_id=user_id, query=query, picked_index=picked_index)
        self.session.add(row)
        await self.session.commit()


# ── Statistics: one session, five aggregate queries ──────────────────────────

@dataclass
class StatsSnapshot:
    users_total: int = 0
    users_today: int = 0
    users_week: int = 0
    active_7: int = 0
    active_30: int = 0
    downloads_total: int = 0
    downloads_today: int = 0
    cache_today: int = 0
    music_today: int = 0
    recognitions_total: int = 0
    recognitions_today: int = 0
    searches_total: int = 0
    searches_today: int = 0
    top_platforms: list = field(default_factory=list)


class StatsRepo:
    """Single source of truth for admin statistics: the event tables.

    daily_stats is no longer written or read (it double-counted cache hits and
    used the server's local date); the table is left in place untouched until
    the owner decides whether to drop it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def snapshot(self, now: Optional[datetime] = None) -> StatsSnapshot:
        now = now or utcnow()
        start, end = today_bounds(now)
        today = and_(Download.created_at >= start, Download.created_at < end)
        s = StatsSnapshot()

        u = (await self.session.execute(select(
            func.count(),
            func.count().filter(and_(User.joined_at >= start, User.joined_at < end)),
            func.count().filter(User.joined_at >= now - timedelta(days=7)),
            func.count().filter(User.last_active >= now - timedelta(days=7)),
            func.count().filter(User.last_active >= now - timedelta(days=30)),
        ).select_from(User))).one()
        s.users_total, s.users_today, s.users_week, s.active_7, s.active_30 = u

        d = (await self.session.execute(select(
            func.count(),
            func.count().filter(today),
            func.count().filter(and_(today, Download.from_cache.is_(True))),
            func.count().filter(and_(today, Download.kind == "music")),
        ).select_from(Download))).one()
        s.downloads_total, s.downloads_today, s.cache_today, s.music_today = d

        r = (await self.session.execute(select(
            func.count(),
            func.count().filter(and_(RecognitionLog.created_at >= start, RecognitionLog.created_at < end)),
        ).select_from(RecognitionLog))).one()
        s.recognitions_total, s.recognitions_today = r

        m = (await self.session.execute(select(
            func.count(),
            func.count().filter(and_(MusicSearch.created_at >= start, MusicSearch.created_at < end)),
        ).select_from(MusicSearch).where(MusicSearch.picked_index.is_(None)))).one()
        s.searches_total, s.searches_today = m

        s.top_platforms = await DownloadRepo(self.session).top_platforms(5)
        return s


# ── Songs / welcome / settings ───────────────────────────────────────────────

class SongCacheRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def make_hash(title: str, artist: str) -> str:
        return hashlib.sha256(f"{title.lower()}|{artist.lower()}".encode()).hexdigest()

    async def get(self, title: str, artist: str) -> Optional[SongCache]:
        h = self.make_hash(title, artist)
        r = await self.session.execute(select(SongCache).where(SongCache.query_hash == h))
        return r.scalar_one_or_none()

    async def save(self, title: str, artist: str, lyrics: str) -> None:
        h = self.make_hash(title, artist)
        stmt = (
            insert(SongCache)
            .values(query_hash=h, title=title, artist=artist, lyrics=lyrics)
            .on_conflict_do_update(index_elements=["query_hash"], set_={"lyrics": lyrics})
        )
        await self.session.execute(stmt)
        await self.session.commit()


class WelcomeRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, lang: str) -> Optional[str]:
        r = await self.session.execute(
            select(WelcomeMessage.text).where(WelcomeMessage.lang == lang)
        )
        return r.scalar_one_or_none()

    async def set(self, lang: str, text: str) -> None:
        stmt = (
            insert(WelcomeMessage)
            .values(lang=lang, text=text)
            .on_conflict_do_update(index_elements=["lang"], set_={"text": text, "updated_at": func.now()})
        )
        await self.session.execute(stmt)
        await self.session.commit()


class SettingRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str, default: str = "") -> str:
        r = await self.session.execute(select(Setting.value).where(Setting.key == key))
        v = r.scalar_one_or_none()
        return v if v is not None else default

    async def set(self, key: str, value: str) -> None:
        stmt = (
            insert(Setting)
            .values(key=key, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )
        await self.session.execute(stmt)
        await self.session.commit()
