"""Regression tests for the adversarial review of the IzlaydiBot-parity work.

Each test pins one finding: stuck "⏳" captions, group noise, the 360p
single-file path, HLS-only buttons, search bootstrap serialisation,
transliteration edge cases and rolling-deploy tolerance.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


# ── Downloader: 360p is one file, HLS-only steps get no button ───────────────

def test_360p_prefers_the_single_progressive_file():
    from bot.services.downloader import _format_selector
    assert _format_selector("360").split("/")[0] == "b[protocol!*=m3u8][ext=mp4][height<=360]"
    assert _format_selector("360", dim="width").split("/")[0] == "b[protocol!*=m3u8][ext=mp4][width<=360]"
    # Higher steps still need the DASH merge (YouTube's single file stops at 360p).
    assert _format_selector("720").startswith("bv[protocol!*=m3u8][height<=720]")
    assert _format_selector("480").startswith("bv[")


def test_hls_only_steps_get_no_button():
    from bot.services.downloader import available_qualities
    info = {"formats": [
        {"vcodec": "avc1", "ext": "mp4", "width": 640, "height": 360, "protocol": "https"},
        {"vcodec": "avc1", "ext": "mp4", "width": 1280, "height": 720, "protocol": "m3u8_native"},
        {"vcodec": "avc1", "ext": "mp4", "width": 1920, "height": 1080, "protocol": "m3u8_native"},
        {"vcodec": "avc1", "ext": "mp4", "width": 854, "height": 480, "protocol": "http_dash_segments"},
    ]}
    assert available_qualities(info) == [360, 480]


# ── Transliteration edge cases ───────────────────────────────────────────────

@pytest.mark.parametrize("latin,cyrillic", [
    ("🎬 1080p", "🎬 1080p"),
    ("aeroport", "аэропорт"),
    ("poeziya", "поэзия"),
    ("ideal", "идеал"),
    ("‘video’", "‘видео’"),
    ("YO'Q", "ЙЎҚ"),
    ("DIQQAT", "ДИҚҚАТ"),
    ("tog'", "тоғ"),
    ("o'zbek", "ўзбек"),
    ("10 MB, MP3", "10 MB, MP3"),
])
def test_transliteration_edge_cases(latin, cyrillic):
    from bot.utils.translit import to_cyrillic
    assert to_cyrillic(latin) == cyrillic


# ── Groups: foreign links never reach the middlewares ────────────────────────

def _msg(chat_type, text):
    return SimpleNamespace(text=text, chat=SimpleNamespace(id=-100, type=chat_type))


def test_group_news_links_do_not_match_the_link_handler():
    from bot.handlers.download import _wants_link
    assert _wants_link(_msg("supergroup", "news https://kun.uz/x")) is False
    assert _wants_link(_msg("group", "see https://youtu.be/dQw4w9WgXcQ")) is True
    assert _wants_link(_msg("private", "https://kun.uz/x")) is True   # gets "unsupported"
    assert _wants_link(_msg("supergroup", "no links here")) is False


@pytest.mark.parametrize("chat_type,told", [("supergroup", False), ("private", True)])
async def test_throttle_notice_only_in_private(chat_type, told):
    from aiogram.types import Message
    from bot.middlewares import throttling
    event = MagicMock(spec=Message)
    event.from_user = SimpleNamespace(id=5)
    event.chat = SimpleNamespace(type=chat_type)
    event.answer = AsyncMock()
    handler = AsyncMock()
    with patch.object(throttling, "check_rate_limit", AsyncMock(return_value=(False, 3))):
        assert await throttling.ThrottlingMiddleware()(handler, event, {"user_lang": "uz"}) is None
    handler.assert_not_awaited()
    assert event.answer.await_count == (1 if told else 0)


@pytest.mark.parametrize("chat_type,registered", [("group", False), ("private", True)])
async def test_group_members_are_not_registered_as_users(chat_type, registered):
    from bot.middlewares import db

    @contextlib.asynccontextmanager
    async def session():
        yield MagicMock()

    handler = AsyncMock(return_value="ok")
    data = {"event_from_user": SimpleNamespace(id=1, is_bot=False),
            "event_chat": SimpleNamespace(type=chat_type)}
    with patch.object(db, "async_session_factory", session), \
         patch.object(db, "register_user", AsyncMock()) as reg:
        assert await db.DbMiddleware()(handler, object(), data) == "ok"
    assert reg.await_count == (1 if registered else 0)


# ── Quality change: never leave a stuck "⏳" caption ─────────────────────────

def _callback(data, *, chat_type="private", user_id=42, age=timedelta(minutes=5), link_owner=None):
    from aiogram.types import Message
    cb = MagicMock()
    cb.data = data
    cb.from_user = SimpleNamespace(id=user_id)
    cb.answer = AsyncMock()
    cb.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=42 if chat_type == "private" else -100, type=chat_type)
    msg.message_id = 77
    msg.video = object()
    msg.date = datetime.now(timezone.utc) - age
    msg.html_text = "🍿 <code>Zoo</code>\n\n📺 360p"
    msg.reply_markup = "OLD_KB"
    msg.edit_caption = AsyncMock()
    msg.delete = AsyncMock()
    if link_owner is None:
        msg.reply_to_message = None
    else:
        link = MagicMock(spec=Message)
        link.message_id = 500
        link.from_user = SimpleNamespace(id=link_owner)
        msg.reply_to_message = link
    cb.message = msg
    return cb, msg


def _handler_patches(download, *, cached=None, enqueue=None):
    return [
        patch.object(download, "get_url_by_key", AsyncMock(return_value=YT)),
        patch.object(download, "get_file_id", AsyncMock(return_value=cached)),
        patch.object(download, "get_video_meta", AsyncMock(return_value={"title": "Zoo"})),
        patch.object(download, "queue_has_room", AsyncMock(return_value=True)),
        patch.object(download, "reserve_slot", AsyncMock(return_value=True)),
        patch.object(download, "release_user_slot", AsyncMock()),
        patch.object(download, "_enqueue_download", enqueue or AsyncMock()),
        patch.object(download, "send_cached", AsyncMock()),
        patch.object(download, "_record_cache_hit", AsyncMock()),
    ]


async def test_failed_enqueue_gives_the_caption_and_buttons_back():
    from bot.handlers import download
    cb, msg = _callback("vq:480:abcdef123456")
    enqueue = AsyncMock(side_effect=RuntimeError("redis down"))
    release = AsyncMock()
    with contextlib.ExitStack() as stack:
        for p in _handler_patches(download, enqueue=enqueue):
            stack.enter_context(p)
        stack.enter_context(patch.object(download, "release_user_slot", release))
        await download.cb_video_quality(cb, "uz", MagicMock(), MagicMock())
    last = msg.edit_caption.await_args_list[-1].kwargs
    assert last == {"caption": "🍿 <code>Zoo</code>\n\n📺 360p", "reply_markup": "OLD_KB"}
    release.assert_awaited_once_with(42)
    cb.answer.assert_awaited()


async def test_flood_wait_on_the_caption_edit_falls_back_to_a_message():
    from bot.handlers import download
    cb, msg = _callback("vq:480:abcdef123456")
    msg.edit_caption = AsyncMock(side_effect=RuntimeError("Too Many Requests: retry after 5"))
    enqueue = AsyncMock()
    with contextlib.ExitStack() as stack:
        for p in _handler_patches(download, enqueue=enqueue):
            stack.enter_context(p)
        await download.cb_video_quality(cb, "uz", MagicMock(), MagicMock())
    kw = enqueue.await_args.kwargs
    assert kw["progress"] == "text" and kw["message_id"] == 9 and kw["restore_caption"] is None
    cb.answer.assert_awaited()


async def test_caption_mode_job_carries_the_original_caption():
    from bot.handlers import download
    cb, msg = _callback("vq:480:abcdef123456")
    enqueue = AsyncMock()
    with contextlib.ExitStack() as stack:
        for p in _handler_patches(download, enqueue=enqueue):
            stack.enter_context(p)
        await download.cb_video_quality(cb, "uz", MagicMock(), MagicMock())
    kw = enqueue.await_args.kwargs
    assert kw["progress"] == "caption" and kw["restore_caption"] == "🍿 <code>Zoo</code>\n\n📺 360p"


async def test_video_older_than_two_days_is_kept_not_replaced():
    from bot.handlers import download
    cb, msg = _callback("vq:720:abcdef123456", age=timedelta(hours=50))
    send = AsyncMock()
    with contextlib.ExitStack() as stack:
        for p in _handler_patches(download, cached="FID720"):
            stack.enter_context(p)
        stack.enter_context(patch.object(download, "send_cached", send))
        await download.cb_video_quality(cb, "uz", MagicMock(), MagicMock())
    send.assert_awaited_once()
    msg.delete.assert_not_awaited()

    cb, msg = _callback("vq:720:abcdef123456", age=timedelta(hours=50))
    enqueue = AsyncMock()
    with contextlib.ExitStack() as stack:
        for p in _handler_patches(download, enqueue=enqueue):
            stack.enter_context(p)
        await download.cb_video_quality(cb, "uz", MagicMock(), MagicMock())
    msg.edit_caption.assert_not_awaited()
    assert enqueue.await_args.kwargs["progress"] == "text"


@pytest.mark.parametrize("tapper,deleted", [(42, True), (7, False)])
async def test_only_the_link_owner_replaces_a_group_video(tapper, deleted):
    from bot.handlers import download
    cb, msg = _callback("vq:720:abcdef123456", chat_type="supergroup", user_id=tapper, link_owner=42)
    send = AsyncMock()
    with contextlib.ExitStack() as stack:
        for p in _handler_patches(download, cached="FID720"):
            stack.enter_context(p)
        stack.enter_context(patch.object(download, "send_cached", send))
        await download.cb_video_quality(cb, "uz", MagicMock(), MagicMock())
    assert send.await_args.kwargs["reply_to"] == 500     # stays under the member's link
    assert msg.delete.await_count == (1 if deleted else 0)


async def test_group_quality_job_threads_under_the_link():
    from bot.handlers import download
    cb, msg = _callback("vq:480:abcdef123456", chat_type="group", user_id=7, link_owner=42)
    enqueue = AsyncMock()
    with contextlib.ExitStack() as stack:
        for p in _handler_patches(download, enqueue=enqueue):
            stack.enter_context(p)
        await download.cb_video_quality(cb, "uz", MagicMock(), MagicMock())
    kw = enqueue.await_args.kwargs
    assert kw["reply_to"] == 500 and kw["progress"] == "text"
    progress_kw = cb.bot.send_message.await_args.kwargs
    assert progress_kw["reply_parameters"].message_id == 77   # status sits under the video


async def test_job_enqueue_omits_restore_caption_when_unset():
    """A worker from the previous deploy must still accept text-mode jobs."""
    from bot.handlers import download
    pool = MagicMock()
    pool.enqueue_job = AsyncMock()
    with patch("bot.services.queue.get_arq_pool", AsyncMock(return_value=pool)):
        await download._enqueue_download(1, 2, 3, YT, "youtube", "360", "uz")
        assert "restore_caption" not in pool.enqueue_job.await_args.kwargs
        await download._enqueue_download(1, 2, 3, YT, "youtube", "480", "uz",
                                         progress="caption", restore_caption="CAP")
        assert pool.enqueue_job.await_args.kwargs["restore_caption"] == "CAP"


# ── Worker: restore when the old video cannot be deleted ─────────────────────

class _Bot:
    def __init__(self, fail_delete=False):
        self.calls = []
        self.fail_delete = fail_delete

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "delete_message" and self.fail_delete:
                raise RuntimeError("message can't be deleted")
            if name == "send_video":
                return SimpleNamespace(video=SimpleNamespace(file_id="NEWFID"), message_id=900)
            return True
        return call


async def _run_caption_job(tmp_path, bot, **extra):
    from bot.services.downloader import DownloadResult
    from bot.worker import tasks
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1000)
    with patch.object(tasks, "get_file_id", AsyncMock(return_value=None)), \
         patch.object(tasks, "acquire_single_flight", AsyncMock(return_value=True)), \
         patch.object(tasks, "release_single_flight", AsyncMock()), \
         patch.object(tasks, "release_user_slot", AsyncMock()), \
         patch.object(tasks, "set_file_id", AsyncMock()), \
         patch.object(tasks, "_record_download", AsyncMock()), \
         patch.object(tasks, "get_temp_path", return_value=str(tmp_path / "out.mp4")), \
         patch.object(tasks, "download", AsyncMock(return_value=DownloadResult(
             str(video), 1.0, 2.0, 1000, {"title": "Zoo", "qualities": [360, 480]}))), \
         patch("bot.services.cache.cached_qualities", AsyncMock(return_value={"360", "480"})), \
         patch("bot.services.delivery.register_video_buttons", AsyncMock()):
        await tasks.download_task({"bot": bot}, user_id=1, chat_id=2, message_id=77,
                                  url=YT, platform="youtube", quality="480", user_lang="uz",
                                  slot_reserved=True, progress="caption", **extra)


async def test_undeletable_old_video_gets_its_caption_back(tmp_path):
    bot = _Bot(fail_delete=True)
    await _run_caption_job(tmp_path, bot, restore_caption="🍿 <code>Zoo</code>\n\n📺 360p")
    names = [c[0] for c in bot.calls]
    assert names.index("send_video") < names.index("delete_message")
    restore = [c for c in bot.calls if c[0] == "edit_message_caption"][-1]
    assert restore[2]["caption"] == "🍿 <code>Zoo</code>\n\n📺 360p"
    assert restore[2]["message_id"] == 77 and restore[2]["reply_markup"] is not None


async def test_job_from_a_newer_bot_with_unknown_arguments_still_runs(tmp_path):
    bot = _Bot()
    await _run_caption_job(tmp_path, bot, some_future_flag=True)
    assert any(c[0] == "send_video" for c in bot.calls)


async def test_failed_quality_change_restores_the_exact_original_caption(tmp_path):
    from bot.services.downloader import DownloadError
    from bot.worker import tasks
    bot = _Bot()
    with patch.object(tasks, "get_file_id", AsyncMock(return_value=None)), \
         patch.object(tasks, "acquire_single_flight", AsyncMock(return_value=True)), \
         patch.object(tasks, "release_single_flight", AsyncMock()), \
         patch.object(tasks, "release_user_slot", AsyncMock()), \
         patch.object(tasks, "get_temp_path", return_value=str(tmp_path / "out.mp4")), \
         patch.object(tasks, "download", AsyncMock(side_effect=DownloadError("x", kind="too_large"))), \
         patch("bot.services.delivery.video_presentation", AsyncMock(return_value=("SHORT", "KB"))):
        await tasks.download_task({"bot": bot}, user_id=1, chat_id=2, message_id=77,
                                  url=YT, platform="youtube", quality="1080", user_lang="uz",
                                  slot_reserved=True, progress="caption",
                                  restore_caption="🍿 <code>Zoo</code>\n\n🔗 x\n\n📺 720p")
    restore = next(c for c in bot.calls if c[0] == "edit_message_caption")
    assert restore[2]["caption"].endswith("📺 720p") and restore[2]["reply_markup"] == "KB"


# ── Fast search: one page load for a burst of cold searches ──────────────────

async def test_cold_search_burst_loads_the_results_page_once():
    from bot.services import yt_search

    async def bootstrap(query):
        await asyncio.sleep(0.05)
        yt_search._context = {"client": {}}
        yt_search._context_at = time.monotonic()
        return [{"id": "page"}]

    api = AsyncMock(return_value=[{"id": "api"}])
    with patch.object(yt_search, "_context", None), \
         patch.object(yt_search, "_context_lock", None), \
         patch.object(yt_search, "_bootstrap", AsyncMock(side_effect=bootstrap)) as boot, \
         patch.object(yt_search, "_api_search", api):
        results = await asyncio.gather(*(yt_search.search(f"q{i}") for i in range(4)))
    assert boot.await_count == 1
    assert api.await_count == 3
    assert sorted(r[0]["id"] for r in results) == ["api", "api", "api", "page"]
