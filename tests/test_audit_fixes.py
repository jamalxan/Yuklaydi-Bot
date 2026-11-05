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
    # Video downloads may exceed one upload: the worker splits them.
    assert opts["max_filesize"] == settings.max_video_bytes
    assert "filesize" in opts["format"]


def test_check_limits_rejects_too_large_video():
    from bot.config import settings
    from bot.services.downloader import DownloadError, check_limits
    info = {"duration": 60, "requested_formats": [{"filesize": settings.max_video_bytes + 1}]}
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


# ── AudD fallback (only with AUDD_TOKEN) ─────────────────────────────────────

def test_parse_audd_result_full_and_unsafe_urls():
    from bot.services.recognizer import _parse_audd_result
    r = _parse_audd_result({
        "title": "Song", "artist": "Artist",
        "apple_music": {"url": "https://music.apple.com/x", "artwork": {"url": "https://img/{w}x{h}.jpg"}},
        "spotify": {"external_urls": {"spotify": "javascript:alert(1)"}},
    })
    assert (r.title, r.artist, r.source) == ("Song", "Artist", "audd")
    assert r.coverart == "https://img/500x500.jpg"
    assert r.apple_url == "https://music.apple.com/x"
    assert r.spotify_url is None, "non-https link must never become a button"
    assert _parse_audd_result({"title": ""}) is None


@pytest.mark.asyncio
async def test_audd_not_called_without_token(tmp_path):
    from bot.config import settings
    from bot.services import recognizer
    media = tmp_path / "a.mp3"
    media.write_bytes(b"x")
    with patch.object(settings, "audd_token", ""), \
         patch.object(recognizer, "probe_duration", AsyncMock(return_value=30.0)), \
         patch.object(recognizer, "_try_recognize_snippet", AsyncMock(return_value=None)), \
         patch.object(recognizer, "_try_audd", AsyncMock()) as audd:
        assert await recognizer.recognize(str(media)) is None
    audd.assert_not_called()


@pytest.mark.asyncio
async def test_audd_used_when_shazam_misses(tmp_path):
    from bot.config import settings
    from bot.services import recognizer
    media = tmp_path / "a.mp3"
    media.write_bytes(b"x")
    found = recognizer.SongResult("T", "A", source="audd")
    with patch.object(settings, "audd_token", "tok"), \
         patch.object(recognizer, "probe_duration", AsyncMock(return_value=30.0)), \
         patch.object(recognizer, "_try_recognize_snippet", AsyncMock(return_value=None)), \
         patch.object(recognizer, "_try_audd", AsyncMock(return_value=found)) as audd:
        assert await recognizer.recognize(str(media)) is found
    audd.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_audd_never_raises_on_network_error(tmp_path):
    from bot.services import recognizer
    snip = tmp_path / "s.wav"
    snip.write_bytes(b"\x00" * 5000)

    class _Boom:
        def __init__(self, *a, **k):
            raise OSError("network down")

    with patch("aiohttp.ClientSession", _Boom):
        assert await recognizer._try_audd(str(snip)) is None


def test_song_card_shows_spotify_and_drops_bad_urls():
    from bot.keyboards.inline import song_card_keyboard
    kb = song_card_keyboard("https://music.apple.com/x", "http://insecure", False, "k", "en",
                            spotify_url="https://open.spotify.com/track/1")
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert urls == ["https://music.apple.com/x", "https://open.spotify.com/track/1"]


# ── Several links in one message ─────────────────────────────────────────────

def test_extract_urls_dedupes_and_caps():
    from bot.handlers.download import MAX_URLS_PER_MESSAGE, extract_urls
    text = ("https://youtu.be/a https://youtu.be/a "
            "https://www.youtube.com/watch?v=b&utm_source=x https://www.youtube.com/watch?v=b "
            + " ".join(f"https://youtu.be/v{i}" for i in range(10)))
    urls, truncated = extract_urls(text)
    assert len(urls) == MAX_URLS_PER_MESSAGE and truncated
    assert urls[0] == "https://youtu.be/a"
    assert sum("v=b" in u for u in urls) == 1
    assert extract_urls("https://youtu.be/a") == (["https://youtu.be/a"], False)


@pytest.mark.asyncio
async def test_multi_link_stops_at_user_cap_and_reports_once():
    from bot.handlers import download
    from bot.texts import t

    message = MagicMock()
    message.text = " ".join(f"https://youtu.be/v{i}" for i in range(4)) + " https://evil.com/x"
    message.from_user = SimpleNamespace(id=77)
    message.chat = SimpleNamespace(id=77)
    message.bot.send_chat_action = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    slots = iter([True, True, False, False])
    enqueue = AsyncMock()

    with patch.object(download, "get_file_id", AsyncMock(return_value=None)), \
         patch.object(download, "queue_has_room", AsyncMock(return_value=True)), \
         patch.object(download, "reserve_slot", AsyncMock(side_effect=lambda uid: next(slots))), \
         patch.object(download, "_enqueue_download", enqueue):
        await download.handle_url(message, "en", MagicMock(), MagicMock())

    assert enqueue.await_count == 2
    texts = [c.args[0] for c in message.answer.await_args_list]
    cap_msg = t("err.user_cap", "en", n=download.settings.per_user_download_cap)
    assert texts.count(cap_msg) == 1, "cap error reported once, then processing stops"
    assert t("err.unsupported", "en") not in texts, "links after the cap are not processed"


@pytest.mark.asyncio
async def test_multi_link_unsupported_reported_once_others_still_queued():
    from bot.handlers import download
    from bot.texts import t

    message = MagicMock()
    message.text = "https://evil.com/a https://youtu.be/ok https://other.bad/b"
    message.from_user = SimpleNamespace(id=78)
    message.chat = SimpleNamespace(id=78)
    message.bot.send_chat_action = AsyncMock()
    message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1))
    enqueue = AsyncMock()
    with patch.object(download, "get_file_id", AsyncMock(return_value=None)), \
         patch.object(download, "queue_has_room", AsyncMock(return_value=True)), \
         patch.object(download, "reserve_slot", AsyncMock(return_value=True)), \
         patch.object(download, "_enqueue_download", enqueue):
        await download.handle_url(message, "uz", MagicMock(), MagicMock())
    assert enqueue.await_count == 1
    texts = [c.args[0] for c in message.answer.await_args_list]
    assert texts.count(t("err.unsupported", "uz")) == 1


# ── h264/aac preference ──────────────────────────────────────────────────────

def test_video_opts_prefer_h264_aac_but_mp3_untouched():
    from bot.services.downloader import _build_opts
    assert _build_opts("/t/x.mp4", "720")["format_sort"] == ["res", "vcodec:h264", "acodec:aac"]
    assert "format_sort" not in _build_opts("/t/x.mp3", "mp3")


# ── YouTube quality + 403 client fallback (found in live runs) ──────────────

def test_youtube_uses_default_client_first():
    from bot.services.downloader import build_base_opts
    assert build_base_opts()["extractor_args"]["youtube"]["player_client"] == ["default"]


def test_video_selector_prefers_separate_https_streams_for_hd():
    from bot.services.downloader import _format_selector
    sel = _format_selector("720", None)
    first = sel.split("/")[0]
    assert first.startswith("bv[protocol!*=m3u8][height<=720]") and "+ba" in first, \
        "YouTube's combined mp4 tops out at 360p: DASH video+audio must be tried first"
    assert "[height<=1080]" in _format_selector("1080", None).split("/")[0]


class _FakeYDL:
    """Records the player_client of each attempt; first one 403s."""
    calls: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return {"duration": 10, "filesize": 1000, "requested_downloads": []}

    def process_ie_result(self, info, download=True):
        import yt_dlp
        clients = self.opts["extractor_args"]["youtube"]["player_client"]
        _FakeYDL.calls.append(clients)
        if clients == ["default"]:
            raise yt_dlp.utils.DownloadError("ERROR: unable to download video data: HTTP Error 403: Forbidden")
        return info


def test_run_ydl_retries_youtube_403_with_fallback_clients(tmp_path):
    from bot.services import downloader as d
    _FakeYDL.calls = []
    part = tmp_path / "ydl_x.f247.webm.part"
    part.write_bytes(b"stale")
    opts = d.build_base_opts(outtmpl=str(tmp_path / "ydl_x.mp4"))
    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        info, _, _ = d.run_ydl("https://www.youtube.com/watch?v=abc", opts, "720")
    assert _FakeYDL.calls == [["default"], list(d.YOUTUBE_FALLBACK_CLIENT_SETS[0])]
    assert not part.exists(), "stale .part from the failed attempt must be removed"


def test_run_ydl_does_not_retry_non_youtube_or_non_403():
    import yt_dlp
    from bot.services import downloader as d
    _FakeYDL.calls = []
    with patch("yt_dlp.YoutubeDL", _FakeYDL), pytest.raises(d.DownloadError):
        d.run_ydl("https://www.tiktok.com/@u/video/1", d.build_base_opts(outtmpl="/t/ydl_y.mp4"), "720")
    assert _FakeYDL.calls == [["default"]]

    class _Unavailable(_FakeYDL):
        def process_ie_result(self, info, download=True):
            _FakeYDL.calls.append("x")
            raise yt_dlp.utils.DownloadError("ERROR: Video unavailable")

    _FakeYDL.calls = []
    with patch("yt_dlp.YoutubeDL", _Unavailable), pytest.raises(d.DownloadError):
        d.run_ydl("https://www.youtube.com/watch?v=abc", d.build_base_opts(outtmpl="/t/ydl_z.mp4"))
    assert _FakeYDL.calls == ["x"], "a non-retryable error must not trigger a second attempt"


def test_run_ydl_checks_limits_before_download():
    from bot.config import settings
    from bot.services import downloader as d

    class _Huge(_FakeYDL):
        def extract_info(self, url, download=False):
            return {"duration": settings.max_audio_duration + 10}

    _FakeYDL.calls = []
    with patch("yt_dlp.YoutubeDL", _Huge), pytest.raises(d.DownloadError) as ei:
        d.run_ydl("https://www.youtube.com/watch?v=abc", d.build_base_opts(outtmpl="/t/m.mp3"), "mp3")
    assert ei.value.kind == "too_long" and _FakeYDL.calls == []


def test_remove_partials_accepts_ytdlp_rewritten_dict(tmp_path):
    from bot.services.downloader import _remove_partials
    (tmp_path / "shz_a.f251.webm.part").write_bytes(b"x")
    (tmp_path / "shz_a.m4a").write_bytes(b"keep")
    (tmp_path / "shz_other.part").write_bytes(b"other job")
    _remove_partials({"default": str(tmp_path / "shz_a.%(ext)s"), "chapter": "x"})
    left = {p.name for p in tmp_path.iterdir()}
    assert left == {"shz_a.m4a", "shz_other.part"}


# ── Oversized video falls back to a lower resolution (found in live bot run) ─

@pytest.mark.parametrize("quality,height", [("720", 720), ("1080", 1080), ("480", 480), ("360p", 360), ("junk", 720)])
def test_format_selector_honours_numeric_height(quality, height):
    from bot.services.downloader import _format_selector
    assert f"[height<={height}]" in _format_selector(quality, None).split("/")[0]


def test_format_selector_prefers_modest_audio_bitrate():
    from bot.services.downloader import _format_selector
    assert "[abr<=?192]" in _format_selector("720", None).split("/")[0]


def test_fallback_heights():
    from bot.services.downloader import fallback_heights
    assert fallback_heights("720") == [480, 360, 240]
    assert fallback_heights("1080") == [720, 480, 360, 240]
    assert fallback_heights("mp3") == []


class _LadderYDL(_FakeYDL):
    """Sizes by selected height: only ≤ 360p fits a 50 MB limit."""
    SIZES = {720: 90, 480: 60, 360: 28, 240: 12}

    def __init__(self, opts):
        super().__init__(opts)
        self.format_selector = opts["format"]

    def build_format_selector(self, spec):
        return spec

    def _pick(self):
        import re
        h = int(re.search(r"height<=(\d+)", self.format_selector).group(1))
        return {"duration": 600, "filesize": self.SIZES[h] * 1024 * 1024, "height": h}

    def extract_info(self, url, download=False):
        return {"duration": 600, "formats": [1], **self._pick()}

    def process_ie_result(self, info, download=True):
        _FakeYDL.calls.append(self._pick()["height"] if not download else "dl")
        return {**info, **self._pick()}


def test_too_large_video_is_delivered_at_lower_resolution():
    from bot.config import settings
    from bot.services import downloader as d
    _FakeYDL.calls = []
    opts = d.build_base_opts(outtmpl="/t/ydl_l.mp4", format=d._format_selector("720", None))
    with patch.object(settings, "max_file_mb", 50), patch.object(settings, "split_large_videos", False), patch("yt_dlp.YoutubeDL", _LadderYDL):
        info, _, _ = d.run_ydl("https://www.tiktok.com/@u/video/1", opts, "720")
    assert info["height"] == 360
    # 720 with light audio, then 480 (normal, light), then 360 fits.
    assert _FakeYDL.calls == [720, 480, 480, 360, "dl"]


def test_nothing_fits_still_reports_too_large():
    from bot.config import settings
    from bot.services import downloader as d
    _FakeYDL.calls = []
    opts = d.build_base_opts(outtmpl="/t/ydl_m.mp4", format=d._format_selector("720", None))
    with patch.object(settings, "max_file_mb", 5), patch.object(settings, "split_large_videos", False), \
         patch("yt_dlp.YoutubeDL", _LadderYDL), \
         pytest.raises(d.DownloadError) as ei:
        d.run_ydl("https://www.tiktok.com/@u/video/1", opts, "720")
    assert ei.value.kind == "too_large" and "dl" not in _FakeYDL.calls


# ── 500 MB limit with the Local Bot API ──────────────────────────────────────

def test_local_api_allows_500mb_and_cloud_is_clamped():
    from bot.config import Settings
    local = Settings(local_api_url="http://telegram-bot-api:8081", max_file_mb=500, job_timeout=900)
    assert local.max_file_mb == 500 and local.max_file_bytes == 500 * 1024 * 1024
    assert Settings(local_api_url="", max_file_mb=500).max_file_mb == 50
    assert Settings(local_api_url="http://x:8081", max_file_mb=5000).max_file_mb == 2000


def test_short_job_timeout_for_big_files_warns(caplog):
    from bot.config import Settings, recommended_job_timeout
    assert recommended_job_timeout(500) == 750 and recommended_job_timeout(50) == 300
    with caplog.at_level(logging.WARNING, logger="yuklaydi.config"):
        Settings(local_api_url="http://x:8081", max_file_mb=500, job_timeout=300)
    assert any("JOB_TIMEOUT" in r.getMessage() for r in caplog.records)


def test_compose_defaults_are_500mb_and_900s():
    src = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "MAX_FILE_MB: ${MAX_FILE_MB:-500}" in src and "JOB_TIMEOUT: ${JOB_TIMEOUT:-900}" in src


def test_too_large_message_shows_current_limit():
    from bot.config import settings
    from bot.texts import t
    with patch.object(settings, "max_file_mb", 500):
        assert "500" in t("err.too_large", "uz")


# ── Lyrics (found in live bot run: Genius crashed, Uzbek titles never matched) ─

@pytest.mark.parametrize("title,artist,expected", [
    ("Sherali Juraev - Gulbadan Rasmiy Matnli ()", "Sherali Juraev Team", ("Gulbadan", "Sherali Juraev")),
    ("Yulduz Usmonova - Aldama (Official Video)", "Yulduz Usmonova", ("Aldama", "Yulduz Usmonova")),
    ("Ummon - Ketma | Official Music Video", "UMMON", ("Ketma", "Ummon")),
    ("Shape of You [Official Video]", "Ed Sheeran", ("Shape of You", "Ed Sheeran")),
    ("Song (feat. Someone) (Lyrics)", "Artist - Topic", ("Song", "Artist")),
    ("Track (Remix)", "DJ", ("Track (Remix)", "DJ")),
])
def test_clean_search_query_handles_youtube_titles(title, artist, expected):
    from bot.services.lyrics import _clean_search_query
    assert _clean_search_query(title, artist) == expected


@pytest.mark.parametrize("a,b,same", [
    ("Sherali Juraev", "Sherali Jo'rayev", True),
    ("Шерали Жураев", "Sherali Juraev", True),
    ("Yulduz Usmonova", "Yulduz Usmonova", True),
    ("Massa", "Ummon", False),
    ("Ed Sheeran", "Ed", False),
])
def test_artist_similar(a, b, same):
    from bot.services.lyrics import artist_similar
    assert artist_similar(a, b) is same


def test_wrong_song_is_rejected():
    from bot.services.lyrics import is_same_song
    assert is_same_song("Gulbadan", "Sherali Jo'rayev", "Gulbadan", "Sherali Juraev")
    assert not is_same_song("Freestyle 3", "Massa", "Ketma", "Ummon")
    assert not is_same_song("Biz ne dertler", "Yulduz Usmonova", "Aldama", "Yulduz Usmonova")


def test_clean_lyrics_strips_genius_page_chrome():
    from bot.services.lyrics import clean_lyrics
    raw = "1 ContributorTranslationsEnglishGulbadan Lyrics[Kuplet]\nKuylarim yetgaymikan\nYou might also like\nsatr2 12Embed"
    assert clean_lyrics(raw) == "Kuylarim yetgaymikan\n\nsatr2"


def test_genius_constructed_with_supported_kwargs_only():
    """Genius(user_agent=...) raised TypeError on lyricsgenius 3.0.1 — every lookup failed."""
    import inspect
    import re
    import lyricsgenius

    sig = inspect.signature(lyricsgenius.Genius.__init__)
    src = (ROOT / "bot" / "services" / "lyrics.py").read_text(encoding="utf-8")
    for call in re.findall(r"lyricsgenius\.Genius\((.*?)\)\n", src, flags=re.S):
        for kw in re.findall(r"(\w+)=", call):
            assert kw in sig.parameters, f"Genius() does not accept {kw!r}"


@pytest.mark.asyncio
async def test_genius_result_for_other_song_is_ignored():
    from bot.config import settings
    from bot.services import lyrics as L

    other = SimpleNamespace(title="Freestyle 3", artist="Massa", lyrics="la la la " * 20)

    class _G:
        def __init__(self, *a, **k):
            self._session = SimpleNamespace(headers={})
            self.skip_non_songs = True

        def search_song(self, *a, **k):
            return other

    with patch.object(settings, "genius_token", "x"), patch("lyricsgenius.Genius", _G):
        assert await L._search_genius("Ketma", "Ummon") is None



def test_youtube_403_tries_web_embedded_then_legacy_clients():
    import yt_dlp
    from bot.services import downloader as d

    class _AllButLegacy403(_FakeYDL):
        def process_ie_result(self, info, download=True):
            clients = self.opts["extractor_args"]["youtube"]["player_client"]
            _FakeYDL.calls.append(clients)
            if clients != list(d.YOUTUBE_FALLBACK_CLIENT_SETS[-1]):
                raise yt_dlp.utils.DownloadError("ERROR: ffmpeg exited with code 3436169992")
            return info

    _FakeYDL.calls = []
    with patch("yt_dlp.YoutubeDL", _AllButLegacy403):
        d.run_ydl("https://youtu.be/abc", d.build_base_opts(outtmpl="/t/ydl_q.mp4"), "720")
    assert _FakeYDL.calls == [["default"], ["web_embedded"], ["android", "web_safari", "web"]]


def test_ladder_tries_lighter_audio_before_lower_video():
    """43-minute video: 480p + 129 kbps audio = 75 MB, 480p + 48 kbps = 49.6 MB."""
    import re
    from bot.config import settings
    from bot.services import downloader as d

    class _AudioYDL(_LadderYDL):
        def _pick(self):
            h = int(re.search(r"height<=(\d+)", self.format_selector).group(1))
            abr = int(re.search(r"abr<=\?(\d+)", self.format_selector).group(1))
            video = {720: 50.7, 480: 34.0, 360: 22.5, 240: 12.7}[h]
            audio = 41.4 if abr >= 128 else 15.6
            return {"duration": 2558, "filesize": int((video + audio) * 1e6), "height": h, "abr": abr}

    _FakeYDL.calls = []
    opts = d.build_base_opts(outtmpl="/t/ydl_a.mp4", format=d._format_selector("720", None))
    with patch.object(settings, "max_file_mb", 50), patch.object(settings, "split_large_videos", False), patch("yt_dlp.YoutubeDL", _AudioYDL):
        info, _, _ = d.run_ydl("https://www.tiktok.com/@u/video/1", opts, "720")
    assert (info["height"], info["abr"]) == (480, 64)



# ── Large videos: split at full quality instead of downgrading ──────────────

def test_video_limit_allows_splitting_beyond_one_upload():
    from bot.config import Settings
    cloud = Settings(max_file_mb=50, max_video_mb=500, job_timeout=900)
    assert cloud.max_file_bytes == 50 * 1024 * 1024
    assert cloud.max_video_bytes == 500 * 1024 * 1024
    off = Settings(max_file_mb=50, max_video_mb=500, split_large_videos=False)
    assert off.max_video_bytes == off.max_file_bytes
    assert Settings(max_video_mb=10_000).max_video_mb == 2000


def test_720p_that_fits_the_video_limit_is_not_downgraded():
    """92 MB 720p on the cloud API: kept at 720p (the worker splits it)."""
    from bot.config import settings
    from bot.services import downloader as d
    _FakeYDL.calls = []
    opts = d.build_base_opts(outtmpl="/t/ydl_s.mp4", format=d._format_selector("720", None))
    with patch.object(settings, "max_file_mb", 50), patch.object(settings, "split_large_videos", True), \
         patch.object(settings, "max_video_mb", 500), patch("yt_dlp.YoutubeDL", _LadderYDL):
        info, _, _ = d.run_ydl("https://www.tiktok.com/@u/video/1", opts, "720")
    assert info["height"] == 720


def test_plan_parts_and_captions():
    from bot.services.delivery import join_file_ids, part_caption, split_file_ids
    from bot.services.splitter import plan_parts
    mb = 1024 * 1024
    assert plan_parts(92 * mb, 50 * mb) == 3          # 92 / (50 * 0.9) → 3
    assert plan_parts(51 * mb, 50 * mb) == 2
    assert plan_parts(500 * mb, 50 * mb) == 12
    ids = ["AAA-_1", "BBB_2", "CCC3"]
    assert split_file_ids(join_file_ids(ids)) == ids
    assert split_file_ids("single") == ["single"]
    assert part_caption("cap", 1, 1, "uz") == "cap"
    assert part_caption("cap", 2, 3, "uz").endswith("2/3-qism")


@pytest.mark.asyncio
async def test_send_cached_video_sends_every_part_keyboard_on_last():
    from bot.services.delivery import send_cached_video
    sent = []

    async def _send(**kw):
        sent.append(kw)

    await send_cached_video(None, 7, "id1|id2|id3", "cap", "en", reply_markup="KB", send=_send)
    assert [k["video"] for k in sent] == ["id1", "id2", "id3"]
    assert [k["reply_markup"] for k in sent] == [None, None, "KB"]
    assert sent[0]["caption"].endswith("Part 1/3")


def _make_test_video(path, seconds=12):
    import subprocess
    from bot.services.media import ffmpeg_path
    subprocess.run([
        ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-b:v", "2M",
        "-c:a", "aac", "-shortest", path,
    ], check=True)


@pytest.mark.asyncio
async def test_split_video_parts_fit_and_keep_codec(tmp_path):
    import json
    import os
    import subprocess
    from bot.services.media import ffprobe_path, probe_duration
    from bot.services.splitter import part_paths, remove_parts, split_video

    src = str(tmp_path / "ydl_abc.mp4")
    _make_test_video(src)
    size = os.path.getsize(src)
    limit = size // 3 + 1

    parts = await split_video(src, limit)
    assert len(parts) >= 3 and parts == part_paths(src)
    assert all(os.path.getsize(p) <= limit for p in parts)
    total = sum([await probe_duration(p) for p in parts])
    assert abs(total - await probe_duration(src)) < 1.0      # nothing lost
    probe = json.loads(subprocess.run(
        [ffprobe_path(), "-v", "error", "-show_streams", "-of", "json", parts[0]],
        capture_output=True, text=True).stdout)
    codecs = {st["codec_type"]: st for st in probe["streams"]}
    assert codecs["video"]["codec_name"] == "h264" and codecs["video"]["height"] == 360  # same quality
    assert codecs["audio"]["codec_name"] == "aac"

    remove_parts(src)
    assert part_paths(src) == []


@pytest.mark.asyncio
async def test_split_video_small_file_untouched(tmp_path):
    import os
    from bot.services.splitter import part_paths, split_video
    src = str(tmp_path / "ydl_small.mp4")
    _make_test_video(src, seconds=2)
    assert await split_video(src, os.path.getsize(src)) == [src]
    assert part_paths(src) == []


def test_download_sync_accepts_video_above_one_upload(tmp_path):
    """A 60 MB video on the cloud API is returned for splitting, not rejected."""
    from bot.config import settings
    from bot.services import downloader as d
    out = tmp_path / "ydl_big.mp4"
    out.write_bytes(b"\0" * (60 * 1024 * 1024))
    with patch.object(settings, "max_file_mb", 50), patch.object(settings, "split_large_videos", True),          patch.object(settings, "max_video_mb", 500),          patch.object(d, "run_ydl", return_value=({"filepath": str(out)}, 0.1, 0.2)):
        r = d._download_sync("https://youtu.be/x", str(out), "720")
        assert r.size_bytes == 60 * 1024 * 1024
        with patch.object(settings, "split_large_videos", False), pytest.raises(d.DownloadError):
            d._download_sync("https://youtu.be/x", str(out), "720")


def test_aria2c_failure_triggers_youtube_client_fallback():
    """In Docker yt-dlp downloads through aria2c; a 403 there is only
    "aria2c exited with code 22" and must still trigger the fallback."""
    import yt_dlp
    from bot.services import downloader as d

    class _Aria403(_FakeYDL):
        def process_ie_result(self, info, download=True):
            clients = self.opts["extractor_args"]["youtube"]["player_client"]
            _FakeYDL.calls.append(clients)
            if clients == ["default"]:
                raise yt_dlp.utils.DownloadError("ERROR: aria2c exited with code 22")
            return info

    _FakeYDL.calls = []
    with patch("yt_dlp.YoutubeDL", _Aria403):
        d.run_ydl("https://youtu.be/abc", d.build_base_opts(outtmpl="/t/ydl_r.mp4"), "720")
    assert _FakeYDL.calls == [["default"], ["web_embedded"]]


def test_aria2_control_file_is_junk(tmp_path):
    from bot.services import downloader as d
    (tmp_path / "ydl_x.mp4.aria2").write_text("x")
    (tmp_path / "ydl_x.f137.mp4.aria2").write_text("x")
    d._remove_partials(str(tmp_path / "ydl_x.mp4"))
    assert list(tmp_path.iterdir()) == []


# ── aria2c only pays off on big streams ──────────────────────────────────────
# googlevideo throttles Range-less requests to ~30 KB/s. aria2c sends its
# first request without a Range header, so a 300 KB audio track took ~10 s
# and a 10 MB stream ~12 s (vs <1 s / ~4 s for yt-dlp's chunked native
# downloader). On a 68 MB stream aria2c still wins 2x (20 s vs 45 s).

class _SizedYDL(_FakeYDL):
    """Records which downloader was active when the download pass ran."""
    sizes: list = []
    seen: list = []

    def extract_info(self, url, download=False):
        fmts = [{"format_id": str(i), "filesize": s} for i, s in enumerate(_SizedYDL.sizes)]
        return {"duration": 10, "filesize": sum(x or 0 for x in _SizedYDL.sizes), "requested_formats": fmts}

    def process_ie_result(self, info, download=True):
        _SizedYDL.seen.append(self.opts.get("external_downloader"))
        return info


@pytest.mark.parametrize("sizes, expected", [
    ([300_000, 220_000], None),                        # short clip: native
    ([10 * 1024 * 1024, 1_000_000], None),             # mid-size: native
    ([68 * 1024 * 1024, 9_000_000], "aria2c"),         # big stream: aria2c
    ([None, 1_000], "aria2c"),                         # unknown size: keep aria2c
])
def test_run_ydl_uses_native_downloader_for_small_streams(sizes, expected):
    from bot.services import downloader as d
    _SizedYDL.sizes, _SizedYDL.seen = sizes, []
    opts = d.build_base_opts(outtmpl="/t/sz.mp4", external_downloader="aria2c")
    with patch("yt_dlp.YoutubeDL", _SizedYDL):
        d.run_ydl("https://www.youtube.com/watch?v=abc", opts, "720")
    assert _SizedYDL.seen == [expected]
    assert opts.get("external_downloader") == "aria2c", "caller's opts must not be mutated"


def test_run_ydl_single_format_size_decides_downloader():
    from bot.services import downloader as d

    class _Single(_SizedYDL):
        def extract_info(self, url, download=False):
            return {"duration": 10, "filesize_approx": 2_000_000}

    _SizedYDL.seen = []
    with patch("yt_dlp.YoutubeDL", _Single):
        d.run_ydl("https://www.tiktok.com/@u/video/1",
                  d.build_base_opts(outtmpl="/t/sz1.mp4", external_downloader="aria2c"), "720")
    assert _SizedYDL.seen == [None]


# ── cookies: yt-dlp must never write back to the master file ─────────────────
# YoutubeDL.close() saves the jar to ``cookiefile``. In Docker secrets/ is a
# read-only mount, so every Instagram download died with "[Errno 30]
# Read-only file system: '/app/secrets/cookies.txt'"; natively, parallel
# runs would race on one file. Each run gets a private throwaway copy.

def test_run_ydl_hands_yt_dlp_a_private_cookie_copy(tmp_path):
    import os
    from bot.services import downloader as d
    master = tmp_path / "cookies.txt"
    master.write_text("# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tx\n")
    before = master.read_text()

    class _Rec(_FakeYDL):
        seen: list = []

        def process_ie_result(self, info, download=True):
            p = self.opts["cookiefile"]
            _Rec.seen.append((p, os.path.isfile(p)))
            with open(p, "a") as fh:  # what yt-dlp does on close()
                fh.write("# saved\n")
            return info

    opts = d.build_base_opts(outtmpl=str(tmp_path / "v.mp4"), cookiefile=str(master))
    with patch("yt_dlp.YoutubeDL", _Rec):
        d.run_ydl("https://www.instagram.com/reel/abc/", opts, "720")
    (path, existed), = _Rec.seen
    assert existed and path != str(master), "yt-dlp must get a copy, not the master"
    assert not os.path.exists(path), "the copy is removed after the run"
    assert master.read_text() == before, "master cookie file must stay untouched"


def test_open_ydl_without_cookies_passes_opts_through():
    from bot.services import downloader as d
    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        with d.open_ydl({"quiet": True}) as ydl:
            assert ydl.opts == {"quiet": True}


def test_instagram_http_400_means_post_is_gone():
    """Instagram's media-info endpoint answers 400 for deleted/unavailable
    posts (seen on two reels from 2018/2024); generic text would be misleading."""
    import yt_dlp
    from bot.services.downloader import classify_download_error
    err = yt_dlp.utils.DownloadError(
        "ERROR: [Instagram] BkfuX9UB-eK: Video info extraction failed: HTTP Error 400: Bad Request")
    assert classify_download_error(err).kind == "private"
    other = yt_dlp.utils.DownloadError("ERROR: [youtube] x: HTTP Error 400: Bad Request")
    assert classify_download_error(other).kind == "generic"


# ── per-platform proxy ───────────────────────────────────────────────────────
# TikTok is blocked at ISP level where the bot runs (TLS handshakes to
# www.tiktok.com time out, the v16/v19 video CDNs never connect), so TikTok
# traffic can be routed through a proxy while YouTube/Instagram stay direct.

class _ProxyYDL(_FakeYDL):
    seen: list = []

    def process_ie_result(self, info, download=True):
        _ProxyYDL.seen.append(self.opts.get("proxy"))
        return info


@pytest.mark.parametrize("platforms, url, expected", [
    ("tiktok", "https://www.tiktok.com/@u/video/1", "socks5://proxy:1080"),
    ("tiktok", "https://vm.tiktok.com/ZMabc/", "socks5://proxy:1080"),
    ("tiktok", "https://www.youtube.com/watch?v=abc", None),
    ("tiktok, instagram", "https://www.instagram.com/reel/abc/", "socks5://proxy:1080"),
    ("all", "https://www.youtube.com/watch?v=abc", "socks5://proxy:1080"),
])
def test_proxy_applied_only_to_configured_platforms(platforms, url, expected):
    from bot.config import settings
    from bot.services import downloader as d
    _ProxyYDL.seen = []
    with patch.object(settings, "proxy_url", "socks5://proxy:1080"), \
         patch.object(settings, "proxy_platforms", platforms), \
         patch("yt_dlp.YoutubeDL", _ProxyYDL):
        d.run_ydl(url, d.build_base_opts(outtmpl="/t/px.mp4"), "720")
    assert _ProxyYDL.seen == [expected]


def test_no_proxy_url_means_direct_everywhere():
    from bot.config import settings
    from bot.services import downloader as d
    _ProxyYDL.seen = []
    with patch.object(settings, "proxy_url", ""), patch.object(settings, "proxy_platforms", "all"), \
         patch("yt_dlp.YoutubeDL", _ProxyYDL):
        d.run_ydl("https://www.tiktok.com/@u/video/1", d.build_base_opts(outtmpl="/t/px2.mp4"), "720")
    assert _ProxyYDL.seen == [None]
    assert d.proxy_for("https://www.tiktok.com/@u/video/1") is None
