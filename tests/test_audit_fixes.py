"""Regression tests for docs/AUDIT_TZ.md (section 4: mandatory tests).

Every test exercises the REAL production function — no inline copies of the
logic under test. Redis is replaced by a small in-memory fake; Telegram and
the DB by mocks.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import pathlib
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

ROOT = pathlib.Path(__file__).parent.parent


# ── In-memory Redis fake (only the commands the code under test uses) ────────

class FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set] = {}

    async def eval(self, *a, **k):  # force the non-Lua fallback paths
        raise Exception("EVAL not supported by fake")

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = str(value)
        return True

    async def incr(self, key):
        self.kv[key] = str(int(self.kv.get(key, 0)) + 1)
        return int(self.kv[key])

    async def decr(self, key):
        self.kv[key] = str(int(self.kv.get(key, 0)) - 1)
        return int(self.kv[key])

    async def expire(self, key, ttl):
        return True

    async def delete(self, *keys):
        for key in keys:
            self.kv.pop(key, None)
            self.sets.pop(key, None)

    async def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    async def srem(self, key, *members):
        self.sets.get(key, set()).difference_update(members)

    async def sismember(self, key, member):
        return member in self.sets.get(key, set())

    def pipeline(self):
        return _FakePipe(self)


class _FakePipe:
    def __init__(self, r):
        self.r = r

    def __getattr__(self, name):
        return getattr(self.r, name)

    async def execute(self):
        return []


@pytest.fixture
def fake_redis():
    r = FakeRedis()

    async def _get():
        return r

    with patch("bot.services.cache.get_redis", _get):
        yield r


def _tg_method():
    return MagicMock()


# ── 1. SSRF: platform detection is hostname-based ────────────────────────────

POSITIVE_URLS = [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://youtube.com/shorts/abc123", "youtube"),
    ("https://m.youtube.com/watch?v=x", "youtube"),
    ("https://music.youtube.com/watch?v=x", "youtube"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://www.instagram.com/reel/Cxyz/", "instagram"),
    ("https://instagram.com/p/Cxyz/", "instagram"),
    ("https://www.tiktok.com/@user/video/123", "tiktok"),
    ("https://vm.tiktok.com/ZMabc/", "tiktok"),
    ("https://www.facebook.com/watch/?v=1", "facebook"),
    ("https://fb.watch/abc/", "facebook"),
    ("https://twitter.com/u/status/1", "twitter"),
    ("https://x.com/u/status/1", "twitter"),
    ("https://www.pinterest.com/pin/1/", "pinterest"),
    ("https://pin.it/abc", "pinterest"),
    ("HTTPS://WWW.YOUTUBE.COM/watch?v=x", "youtube"),
]

NEGATIVE_URLS = [
    "http://127.0.0.1/youtube.com",
    "http://localhost:8081/youtube.com/watch",
    "http://169.254.169.254/latest/meta-data/?youtube.com",
    "http://10.0.0.5/instagram.com",
    "http://[::1]/youtube.com",
    "https://youtube.com.evil.com/watch?v=x",
    "https://evil.com/?u=https://youtube.com/watch",
    "https://notyoutube.com/watch?v=x",
    "https://youtube.com@evil.com/watch",
    "https://user:pass@youtube.com/watch?v=x",
    "https://youtube.com:8443/watch?v=x",
    "ftp://youtube.com/watch",
    "file:///etc/passwd#youtube.com",
    "https://evil-instagram.com/p/x",
    "https://instagram.com.attacker.net/p/x",
    "javascript:alert('youtube.com')",
    "https://2130706433/youtube.com",
    "",
]


@pytest.mark.parametrize("url,platform", POSITIVE_URLS)
def test_detect_platform_accepts_real_hosts(url, platform):
    from bot.services.downloader import detect_platform
    assert detect_platform(url) == platform


@pytest.mark.parametrize("url", NEGATIVE_URLS)
def test_detect_platform_rejects_ssrf(url):
    from bot.services.downloader import detect_platform
    assert detect_platform(url) is None


def test_ytdlp_opts_restrict_extractors_and_exclude_generic():
    from bot.services.downloader import build_base_opts
    opts = build_base_opts()
    allowed = opts.get("allowed_extractors")
    assert allowed, "allowed_extractors must be set"
    import re
    assert not any(re.fullmatch(p, "generic") for p in allowed), "generic IE must be excluded"
    assert any(re.fullmatch(p, "youtube") for p in allowed)


@pytest.mark.asyncio
async def test_shortlink_redirect_to_foreign_host_is_rejected():
    from bot.services import downloader
    from bot.services.downloader import DownloadError

    class _Resp:
        def __init__(self, status, location):
            self.status, self.headers = status, {"Location": location}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def head(self, url, **k):
            return _Resp(302, "http://169.254.169.254/latest/meta-data")

        get = head

    with patch("aiohttp.ClientSession", _Session):
        with pytest.raises(DownloadError) as ei:
            await downloader.resolve_shortlink("https://pin.it/abc")
    assert ei.value.kind == "unsupported"


# ── 2. Banned user is ignored ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_banned_user_is_ignored(fake_redis):
    from bot.middlewares.ban import BanMiddleware
    from bot.services.cache import set_banned

    await set_banned(555, True)
    handler = AsyncMock(return_value="handled")
    mw = BanMiddleware()

    assert await mw(handler, MagicMock(), {"event_from_user": SimpleNamespace(id=555)}) is None
    handler.assert_not_called()

    assert await mw(handler, MagicMock(), {"event_from_user": SimpleNamespace(id=556)}) == "handled"

    await set_banned(555, False)
    assert await mw(handler, MagicMock(), {"event_from_user": SimpleNamespace(id=555)}) == "handled"


@pytest.mark.asyncio
async def test_ban_check_fails_open_and_never_blocks_admin():
    from bot.config import settings
    from bot.middlewares.ban import BanMiddleware

    handler = AsyncMock(return_value="ok")
    with patch("bot.middlewares.ban.is_banned", AsyncMock(side_effect=ConnectionError("down"))):
        assert await BanMiddleware()(handler, MagicMock(), {"event_from_user": SimpleNamespace(id=1)}) == "ok"
    admin = next(iter(settings.admin_ids))
    with patch("bot.middlewares.ban.is_banned", AsyncMock(return_value=True)):
        assert await BanMiddleware()(handler, MagicMock(), {"event_from_user": SimpleNamespace(id=admin)}) == "ok"


def test_ban_middleware_registered_as_outer_update_middleware():
    src = (ROOT / "bot" / "main.py").read_text(encoding="utf-8")
    assert "dp.update.outer_middleware(BanMiddleware())" in src


# ── 3. Cleanup keeps cookies and non-temp files ──────────────────────────────

@pytest.mark.asyncio
async def test_cleanup_keeps_cookies_and_non_temp(tmp_path):
    from bot.services.media import cleanup_old_files

    old = time.time() - 7200
    keep = ["cookies.txt", "important.mp4", "botapi_file.bin"]
    drop = ["ydl_abc.mp4", "shz_x.m4a", "mus_y.mp3", "th_z.jpg", "exp_u.csv"]
    for name in keep + drop:
        p = tmp_path / name
        p.write_bytes(b"x")
        os.utime(p, (old, old))
    fresh = tmp_path / "ydl_fresh.mp4"
    fresh.write_bytes(b"x")

    removed = await cleanup_old_files(str(tmp_path), max_age_seconds=3600)

    assert removed == len(drop)
    left = {p.name for p in tmp_path.iterdir()}
    assert set(keep) <= left
    assert "ydl_fresh.mp4" in left
    assert not (set(drop) & left)


def test_temp_files_live_in_tmp_subdir(tmp_path):
    from bot.config import settings
    from bot.services import media
    with patch.object(settings, "download_dir", str(tmp_path)):
        path = media.get_temp_path(".mp4")
    assert pathlib.Path(path).parent == tmp_path / "tmp"


def test_default_cookie_location_is_outside_download_dir():
    from bot.config import settings
    from bot.services.downloader import cookies_path
    with patch.object(settings, "cookies_file", ""):
        p = cookies_path()
    assert p is None or "secrets" in p or not p.startswith(settings.download_dir)


# ── 4. User is created without choosing a language ───────────────────────────

@pytest.mark.asyncio
async def test_user_created_without_lang_choice(fake_redis):
    from bot.middlewares.db import register_user

    repo = MagicMock()
    repo.upsert_seen = AsyncMock(return_value=True)
    tg_user = SimpleNamespace(id=42, username="ali", first_name="Ali", language_code="ru", is_bot=False)

    await register_user(repo, tg_user)
    repo.upsert_seen.assert_awaited_once_with(42, username="ali", first_name="Ali", lang="ru")

    # Second update within the hour: no extra DB write
    await register_user(repo, tg_user)
    assert repo.upsert_seen.await_count == 1


@pytest.mark.asyncio
async def test_failed_user_upsert_is_retried_next_update(fake_redis):
    from bot.middlewares.db import register_user

    repo = MagicMock()
    repo.session.rollback = AsyncMock()
    repo.upsert_seen = AsyncMock(side_effect=[RuntimeError("db down"), True])
    tg_user = SimpleNamespace(id=43, username=None, first_name="B", language_code=None, is_bot=False)

    await register_user(repo, tg_user)
    await register_user(repo, tg_user)
    assert repo.upsert_seen.await_count == 2


def test_guess_lang_falls_back_to_default():
    from bot.config import settings
    from bot.db.repo import guess_lang
    assert guess_lang("en-US") == "en"
    assert guess_lang("de") == settings.default_lang
    assert guess_lang(None) == settings.default_lang


# ── 5. Per-user cap is enforced at enqueue time ──────────────────────────────

@pytest.mark.asyncio
async def test_per_user_cap_rejects_extra_jobs(fake_redis):
    from bot.services.cache import release_user_slot, try_acquire_user_slot

    assert [await try_acquire_user_slot(7, 3) for _ in range(5)] == [True, True, True, False, False]
    await release_user_slot(7)
    assert await try_acquire_user_slot(7, 3) is True
    assert await try_acquire_user_slot(7, 3) is False
    # Release never goes negative
    for _ in range(10):
        await release_user_slot(7)
    assert int(fake_redis.kv["ucap:7"]) == 0


def test_slot_lua_scripts_are_atomic_check_and_set():
    from bot.services import cache
    assert "GET" in cache._TRY_ACQUIRE_SLOT_LUA and "INCR" in cache._TRY_ACQUIRE_SLOT_LUA
    assert "v > 0" in cache._RELEASE_SLOT_LUA


# ── 6. HTML escaping of external strings ─────────────────────────────────────

def test_html_escape_in_search_results_and_song_card():
    from bot.handlers.shazam import build_lyrics_page
    from bot.keyboards.inline import music_search_keyboard
    from bot.services.music_search import SongEntry

    evil = SongEntry(id="v1", title="<b>x</b> & <script>", uploader="A<i>", duration=200, url="https://youtu.be/v1")
    text, _ = music_search_keyboard("h", [evil], page=0, total_pages=1, lang="uz")
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "A&lt;i&gt;" in text

    page = build_lyrics_page("T<b>", "Ar&t", ["line <x>\n" * 2000], 0)
    assert len(page) <= 4096
    assert "T&lt;b&gt;" in page and "Ar&amp;t" in page
    assert "<x>" not in page


def test_recognize_caption_is_escaped():
    src = (ROOT / "bot" / "worker" / "tasks.py").read_text(encoding="utf-8")
    assert "esc(result.title)" in src and "esc(result.artist)" in src


def test_esc_helper():
    from bot.utils.text import esc
    assert esc("<a href='x'>&") == "&lt;a href='x'&gt;&amp;"
    assert esc(None) == ""


def test_long_tracks_are_marked_in_results():
    from bot.keyboards.inline import music_search_keyboard
    from bot.services.music_search import LONG_TRACK_SECONDS, SongEntry
    from bot.texts import t

    long_e = SongEntry(id="v", title="mix", uploader="dj", duration=LONG_TRACK_SECONDS + 1, url="https://youtu.be/v")
    text, _ = music_search_keyboard("h", [long_e], page=0, total_pages=1, lang="en")
    assert t("btn.long", "en") in text


# ── 7. Invalid welcome HTML is never saved ───────────────────────────────────

@pytest.mark.asyncio
async def test_welcome_invalid_html_not_saved():
    from bot.handlers.admin import fsm_welcome_new_text

    message = MagicMock()
    message.text = "<b>broken"
    message.caption = None
    message.html_text = "<b>broken"
    calls = []

    async def answer(text, **kw):
        calls.append(text)
        if text.startswith("👁"):
            raise TelegramBadRequest(method=_tg_method(), message="can't parse entities")

    message.answer = answer
    state = MagicMock()
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()

    await fsm_welcome_new_text(message, state)

    state.update_data.assert_not_called()
    state.set_state.assert_not_called()
    assert any("HTML" in c for c in calls)


@pytest.mark.asyncio
async def test_welcome_non_text_message_is_rejected():
    from bot.handlers.admin import fsm_welcome_new_text

    message = MagicMock(text=None, caption=None, html_text="")
    message.answer = AsyncMock()
    state = MagicMock(update_data=AsyncMock(), set_state=AsyncMock())
    await fsm_welcome_new_text(message, state)
    state.update_data.assert_not_called()
    message.answer.assert_awaited_once()


# ── 8. yt-dlp opts carry max_filesize; limits checked before download ───────

def test_ytdlp_opts_have_max_filesize():
    from bot.config import settings
    from bot.services.downloader import _build_opts
    opts = _build_opts("/tmp/x.mp4", "720")
    assert opts["max_filesize"] == settings.max_file_bytes
    assert "filesize" in opts["format"]


def test_check_limits_rejects_too_large_video():
    from bot.config import settings
    from bot.services.downloader import DownloadError, check_limits
    info = {"duration": 60, "requested_formats": [{"filesize": settings.max_file_bytes + 1}]}
    with pytest.raises(DownloadError) as ei:
        check_limits(info, "720")
    assert ei.value.kind == "too_large"
    check_limits({"duration": 60, "filesize": 1000}, "720")  # small → OK
    check_limits({"duration": 60}, "720")                    # unknown → let max_filesize guard


# ── 9. Music duration limit ──────────────────────────────────────────────────

def test_music_duration_limit():
    from bot.config import settings
    from bot.services.downloader import DownloadError, check_limits
    with pytest.raises(DownloadError) as ei:
        check_limits({"duration": settings.max_audio_duration + 1}, "mp3")
    assert ei.value.kind == "too_long"
    check_limits({"duration": 180}, "mp3")


def test_too_long_error_is_localized():
    from bot.texts import t
    for lang in ("uz", "ru", "en"):
        assert "{" not in t("err.too_long", lang)


# ── 10. Broadcast: RetryAfter retried, Forbidden counted as blocked ─────────

@pytest.mark.asyncio
async def test_broadcast_retry_after_and_forbidden():
    from bot.services.broadcast import BroadcastProgress, run_broadcast

    attempts: dict[int, int] = {}

    async def copy_message(chat_id, **kw):
        attempts[chat_id] = attempts.get(chat_id, 0) + 1
        if chat_id == 2 and attempts[chat_id] == 1:
            raise TelegramRetryAfter(method=_tg_method(), message="flood", retry_after=3)
        if chat_id == 3:
            raise TelegramForbiddenError(method=_tg_method(), message="blocked")
        if chat_id == 4:
            raise TelegramBadRequest(method=_tg_method(), message="chat not found")

    bot = MagicMock()
    bot.copy_message = copy_message
    pages = {0: [1, 2, 3, 4, 5]}
    sleeps: list[float] = []
    blocked_marked: list[int] = []
    saved: list[dict] = []

    async def fetch_page(last_id, limit):
        return pages.get(last_id, [])

    async def mark_blocked(ids):
        blocked_marked.extend(ids)

    async def save_progress(p):
        saved.append(p.to_dict())

    async def sleep(s):
        sleeps.append(s)

    progress = await run_broadcast(
        bot, from_chat_id=1, message_id=1, reply_markup=None,
        progress=BroadcastProgress(), fetch_page=fetch_page,
        mark_blocked=mark_blocked, save_progress=save_progress, rate=25, sleep=sleep,
    )

    assert attempts[2] == 2, "RetryAfter must retry the SAME user"
    assert progress.delivered == 3       # 1, 2 (after retry), 5
    assert progress.blocked == 1 and blocked_marked == [3]
    assert progress.failed == 1          # 4
    assert progress.last_id == 5
    assert any(s >= 3 for s in sleeps), "must wait retry_after"
    assert sleeps.count(1 / 25) == 5, "rate pacing per recipient"
    assert saved and saved[-1]["last_id"] == 5


@pytest.mark.asyncio
async def test_broadcast_resumes_from_saved_progress():
    from bot.services.broadcast import BroadcastProgress, run_broadcast

    sent = []
    bot = MagicMock()
    bot.copy_message = AsyncMock(side_effect=lambda chat_id, **kw: sent.append(chat_id))
    all_ids = [10, 20, 30, 40]

    async def fetch_page(last_id, limit):
        return [i for i in all_ids if i > last_id][:limit]

    start = BroadcastProgress.from_dict({"last_id": 20, "delivered": 2})
    p = await run_broadcast(
        bot, from_chat_id=1, message_id=1, reply_markup=None, progress=start,
        fetch_page=fetch_page, mark_blocked=AsyncMock(), save_progress=AsyncMock(),
        sleep=AsyncMock(),
    )
    assert sent == [30, 40]
    assert p.delivered == 4


def test_broadcast_task_registered_with_long_timeout():
    from bot.worker.tasks import BROADCAST_TIMEOUT, WorkerSettings
    names = {getattr(f, "name", getattr(f, "__name__", "")) for f in WorkerSettings.functions}
    assert "broadcast_task" in names
    assert BROADCAST_TIMEOUT >= 3600


def test_parse_cta_accepts_only_https():
    from bot.handlers.admin import parse_cta
    assert parse_cta("Join - https://t.me/x") == ("Join", "https://t.me/x")
    assert parse_cta("Bad - javascript:alert(1)") is None
    assert parse_cta("Bad - http://insecure.example") is None
    assert parse_cta("no separator") is None


# ── 11. Cache hits are counted in stats ──────────────────────────────────────

@pytest.mark.asyncio
async def test_stats_cache_hits_counted():
    from bot.handlers.download import _record_cache_hit

    user_repo = MagicMock(inc_downloads=AsyncMock())
    download_repo = MagicMock(create=AsyncMock())
    await _record_cache_hit(1, "https://youtu.be/x", "youtube", "video", user_repo, download_repo)
    user_repo.inc_downloads.assert_awaited_once_with(1)
    download_repo.create.assert_awaited_once_with(1, "https://youtu.be/x", "youtube", "video", from_cache=True)


@pytest.mark.asyncio
async def test_stats_snapshot_counts_from_event_tables():
    from sqlalchemy.dialects import postgresql
    from bot.db.repo import StatsRepo

    statements = []

    class _Res:
        def __init__(self, n):
            self.n = n

        def one(self):
            return tuple(range(self.n))

        def fetchall(self):
            return [("youtube", 5)]

    async def execute(stmt):
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        statements.append(sql)
        n = 5 if "FROM users" in sql else 4 if "FROM downloads" in sql and "GROUP BY" not in sql else 2
        return _Res(n)

    session = MagicMock(execute=execute)
    snap = await StatsRepo(session).snapshot(datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc))
    joined = "\n".join(statements)
    assert "daily_stats" not in joined
    assert "from_cache IS true" in joined
    assert snap.top_platforms == [("youtube", 5)]


def test_today_bounds_use_tashkent_midnight():
    from bot.config import settings
    from bot.db.repo import today_bounds
    with patch.object(settings, "stats_tz", "Asia/Tashkent"):
        # 20:00 UTC on Jan 1 = 01:00 Jan 2 in Tashkent (UTC+5)
        start, end = today_bounds(datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc))
    assert start == datetime(2026, 1, 1, 19, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 2, 19, 0, tzinfo=timezone.utc)


# ── 12. Throttle message uses the user's language ────────────────────────────

@pytest.mark.asyncio
async def test_throttle_message_uses_user_lang():
    from aiogram.types import Message
    from bot.middlewares.throttling import ThrottlingMiddleware
    from bot.texts import t

    msg = MagicMock(spec=Message)
    msg.from_user = SimpleNamespace(id=999001)
    msg.answer = AsyncMock()
    handler = AsyncMock()

    with patch("bot.middlewares.throttling.check_rate_limit", AsyncMock(return_value=(False, 7))), \
         patch("bot.middlewares.throttling.get_user_lang", AsyncMock(return_value="ru")):
        await ThrottlingMiddleware()(handler, msg, {})

    handler.assert_not_called()
    msg.answer.assert_awaited_once_with(t("throttle", "ru", sec=7))


@pytest.mark.asyncio
async def test_callback_debounce_and_rate_limit(fake_redis):
    from bot.config import settings
    from bot.services.cache import check_callback_rate

    assert await check_callback_rate(1, "ms:a:1") == (True, "")
    assert await check_callback_rate(1, "ms:a:1") == (False, "debounce")
    with patch.object(settings, "callback_rate_limit_requests", 3):
        results = [await check_callback_rate(1, f"msp:a:{i}") for i in range(4)]
    assert results[-1] == (False, "rate")


@pytest.mark.asyncio
async def test_callback_throttle_fails_open_on_redis_error():
    from aiogram.types import CallbackQuery
    from bot.middlewares.throttling import ThrottlingMiddleware

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = SimpleNamespace(id=999002)
    cb.data = "x"
    handler = AsyncMock(return_value="ok")
    with patch("bot.middlewares.throttling.check_callback_rate", AsyncMock(side_effect=ConnectionError)):
        assert await ThrottlingMiddleware()(handler, cb, {}) == "ok"


# ── 13. Non-media documents are rejected ─────────────────────────────────────

@pytest.mark.parametrize("mime,name,expected", [
    ("audio/mpeg", "song.mp3", ".mp3"),
    ("video/mp4", "clip.MP4", ".mp4"),
    ("audio/ogg", None, ".ogg"),
    ("application/pdf", "doc.pdf", None),
    ("application/zip", "a.zip", None),
    ("text/plain", "song.mp3", None),
    (None, "x.mp3", None),
])
def test_document_non_media_rejected(mime, name, expected):
    from bot.handlers.shazam import _document_suffix
    result = _document_suffix(mime, name)
    if expected is None:
        assert result is None
    else:
        assert result is not None and result.lower() in (expected, ".oga", ".opus")


# ── 14. Music search uses the cache ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_music_search_uses_cache():
    from dataclasses import asdict
    from bot.handlers import music
    from bot.services.music_search import SongEntry

    entry = SongEntry(id="v1", title="Song", uploader="Artist", duration=180, url="https://youtu.be/v1")
    progress = MagicMock(edit_text=AsyncMock())
    message = MagicMock()
    message.text = "  Some   Song "
    message.from_user = SimpleNamespace(id=5)
    message.chat = SimpleNamespace(id=5)
    message.bot.send_chat_action = AsyncMock()
    message.answer = AsyncMock(return_value=progress)

    search = AsyncMock()
    with patch.object(music, "get_music_search", AsyncMock(return_value=[asdict(entry)])), \
         patch.object(music, "set_music_search", AsyncMock(return_value="qh")), \
         patch.object(music, "search_songs", search), \
         patch("bot.db.session.async_session_factory", side_effect=RuntimeError("no db")):
        await music.handle_music_search(message, "uz")

    search.assert_not_called()
    progress.edit_text.assert_awaited_once()
    assert "Song" in progress.edit_text.await_args.args[0]


@pytest.mark.parametrize("raw,expected_none", [("a", True), ("!!", True), ("  ", True), ("ok song", False)])
def test_normalize_query_rejects_trivial(raw, expected_none):
    from bot.services.music_search import normalize_query
    assert (normalize_query(raw) is None) is expected_none


# ── 15. Admin FSM handles non-text messages ──────────────────────────────────

@pytest.mark.parametrize("text", [None, "", "abc", "-5", "0", "1" * 20])
def test_admin_fsm_non_text_message(text):
    from bot.handlers.admin import parse_user_id
    assert parse_user_id(text) is None


def test_admin_parse_user_id_valid():
    from bot.handlers.admin import parse_user_id
    assert parse_user_id(" 123456789 ") == 123456789


@pytest.mark.asyncio
async def test_admin_cannot_be_banned():
    from bot.config import settings
    from bot.handlers.admin import fsm_ban_id

    admin = next(iter(settings.admin_ids))
    message = MagicMock(text=str(admin))
    message.answer = AsyncMock()
    state = MagicMock(get_data=AsyncMock(return_value={"ban_action": "ban"}), clear=AsyncMock())
    with patch("bot.services.cache.set_banned", AsyncMock()) as sb:
        await fsm_ban_id(message, state)
    sb.assert_not_called()


# ── 16. Every locale key exists in every language ────────────────────────────

def test_all_locale_keys_present():
    from bot.texts import LANGS, T
    missing = [(k, lang) for k, v in T.items() for lang in LANGS if not v.get(lang)]
    assert not missing, f"Missing translations: {missing}"


def test_t_falls_back_and_formats():
    from bot.texts import t
    assert t("err.user_cap", "ru", n=3)
    assert t("err.user_cap", "xx", n=3) == t("err.user_cap", "uz", n=3)


# ── Extra: config, logging, recognition windows, routing invariants ──────────

def test_cloud_api_clamps_max_file_mb(caplog):
    from bot.config import Settings
    with caplog.at_level(logging.WARNING):
        s = Settings(local_api_url="", max_file_mb=2000)
    assert s.max_file_mb == 50
    s2 = Settings(local_api_url="http://telegram-bot-api:8081", max_file_mb=2000)
    assert s2.max_file_mb == 2000


@pytest.mark.parametrize("url,pw,expected", [
    ("redis://redis:6379/0", "", "redis://redis:6379/0"),
    ("redis://redis:6379/0", "p@ss/w", "redis://:p%40ss%2Fw@redis:6379/0"),
    ("redis://:already@redis:6379/0", "other", "redis://:already@redis:6379/0"),
])
def test_redis_dsn_injects_password(url, pw, expected):
    from bot.config import _with_redis_password
    assert _with_redis_password(url, pw) == expected


def test_json_formatter_emits_valid_json():
    from bot.logger import JsonFormatter
    try:
        raise ValueError('bad "quote"\nnewline')
    except ValueError:
        import sys
        rec = logging.LogRecord("yuklaydi", logging.ERROR, __file__, 1, 'msg with "quotes"\nand newline',
                                None, sys.exc_info())
    rec.fields = {"job": "download", "t_total": 1.5}
    line = JsonFormatter().format(rec)
    assert "\n" not in line
    data = json.loads(line)
    assert data["message"].startswith("msg with")
    assert data["job"] == "download" and "ValueError" in data["exc"]


@pytest.mark.parametrize("dur,expected", [
    (0, [(-1.0, 15), (0.0, 15), (0.0, 30)]),
    (10, [(0.0, 10)]),
    (15, [(0.0, 15)]),
    (20, [(2.5, 15), (0.0, 15), (0.0, 20)]),
])
def test_recognition_windows_dedup(dur, expected):
    from bot.services.recognizer import recognition_windows
    assert recognition_windows(dur) == expected


def test_recognition_windows_long_track_three_distinct():
    from bot.services.recognizer import recognition_windows
    w = recognition_windows(240)
    assert len(w) == len(set(w)) == 3


def test_handlers_are_private_chat_only():
    for name in ("start", "download", "shazam", "music", "admin"):
        src = (ROOT / "bot" / "handlers" / f"{name}.py").read_text(encoding="utf-8")
        assert "ChatType.PRIVATE" in src, f"{name}.py must filter private chats"


def test_no_hardcoded_passwords_in_compose():
    src = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for line in src.splitlines():
        if "bot_password" in line:
            assert "${POSTGRES_PASSWORD:-bot_password}" in line
    prod = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD:?" in prod and "REDIS_PASSWORD:?" in prod
    assert '"127.0.0.1:8081:8081"' in src


def test_alembic_baseline_is_not_empty():
    versions = ROOT / "alembic" / "versions"
    srcs = [p.read_text(encoding="utf-8") for p in versions.glob("*.py")]
    assert any("CREATE TABLE IF NOT EXISTS users" in s for s in srcs)
    assert any("is_blocked_bot" in s for s in srcs)


def test_no_create_all_at_runtime():
    for py in (ROOT / "bot").rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "create_all":
                pytest.fail(f"metadata.create_all used in {py} — Alembic is the only schema tool")


def test_pgbouncer_connect_args():
    from bot.db.session import pgbouncer_connect_args
    args = pgbouncer_connect_args("postgresql+asyncpg://u:p@pgbouncer:5432/db")
    assert args["statement_cache_size"] == 0
    assert args["prepared_statement_cache_size"] == 0
    assert callable(args["prepared_statement_name_func"])


# ── Found during live yt-dlp run ─────────────────────────────────────────────

def test_no_suitable_extractor_is_unsupported():
    from bot.services.downloader import classify_download_error
    err = Exception("ERROR: No suitable extractor found for URL http://127.0.0.1:8081/x")
    assert classify_download_error(err).kind == "unsupported"


@pytest.mark.parametrize("path,expected", [
    ("/t/shz_a_clip.m4a", "/t/shz_a_clip"),
    ("/t/ydl_a.mp4", "/t/ydl_a"),
    ("/t/mus_a.mp3", "/t/mus_a"),
    ("/t/x.webm", "/t/x"),
    ("/t/x.txt", "/t/x.txt"),
])
def test_strip_media_ext(path, expected):
    from bot.services.downloader import strip_media_ext
    assert strip_media_ext(path) == expected


# ── Admin is never walled by the required-channel check ─────────────────────

@pytest.mark.asyncio
async def test_channel_wall_never_blocks_admin_fsm_text():
    from aiogram.types import Message
    from bot.config import settings
    from bot.middlewares.channel import ChannelCheckMiddleware

    admin = next(iter(settings.admin_ids))
    msg = MagicMock(spec=Message)
    msg.from_user = SimpleNamespace(id=admin)
    msg.text = "-1001234567890"          # e.g. channel ID typed in the admin FSM
    handler = AsyncMock(return_value="handled")
    channel_lookup = AsyncMock(return_value="-1001234567890")
    with patch("bot.middlewares.channel._get_channel_id", channel_lookup):
        assert await ChannelCheckMiddleware()(handler, msg, {}) == "handled"
    channel_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_channel_wall_still_blocks_non_member_user():
    from aiogram.types import Message
    from bot.middlewares.channel import ChannelCheckMiddleware

    msg = MagicMock(spec=Message)
    msg.from_user = SimpleNamespace(id=424242)
    msg.text = "https://youtu.be/x"
    msg.answer = AsyncMock()
    bot = MagicMock()
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="left"))
    msg.bot = bot
    handler = AsyncMock()
    with patch("bot.middlewares.channel._get_channel_id", AsyncMock(return_value="-100123")), \
         patch("bot.middlewares.channel.get_cached_membership", AsyncMock(return_value=None)), \
         patch("bot.middlewares.channel.set_cached_membership", AsyncMock()):
        await ChannelCheckMiddleware()(handler, msg, {"bot": bot, "user_lang": "uz"})
    handler.assert_not_called()
