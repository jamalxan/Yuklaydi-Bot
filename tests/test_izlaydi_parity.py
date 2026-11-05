"""Regression tests for the @IzlaydiBot-parity work.

Covers: canonical cache keys, the quality ladder (vertical video included),
captions and keyboards under delivered media, the fourth language (Uzbek
Cyrillic), group chats, the quality-change flow, fast music search parsing,
and the speed-relevant ordering in the worker (buttons ride on the upload,
the song card does not wait for lyrics).
"""
from __future__ import annotations

import os
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Canonical cache keys ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "https://youtu.be/dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?si=xyz&t=42",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1&index=3",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ?feature=share",
    "https://youtube.com/live/dQw4w9WgXcQ",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
])
def test_every_youtube_form_shares_one_cache_key(raw):
    from bot.services.downloader import normalize_url
    assert normalize_url(raw) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.mark.parametrize("raw", [
    "https://www.instagram.com/reel/Chunk8-jurw/?igsh=abc",
    "https://www.instagram.com/reels/Chunk8-jurw/",
    "https://www.instagram.com/p/Chunk8-jurw/",
    "https://instagram.com/tv/Chunk8-jurw",
    "https://www.instagram.com/some.user/reel/Chunk8-jurw/?utm_source=ig_web_copy_link",
])
def test_every_instagram_form_shares_one_cache_key(raw):
    from bot.services.downloader import normalize_url
    assert normalize_url(raw) == "https://www.instagram.com/p/Chunk8-jurw/"


def test_non_canonical_urls_only_lose_tracking():
    from bot.services.downloader import normalize_url
    assert normalize_url("https://www.tiktok.com/@u/video/1?utm_source=x") == "https://www.tiktok.com/@u/video/1"
    # not an 11-char id: generic cleanup only, never invented
    assert normalize_url("https://www.youtube.com/watch?v=abc&si=1") == "https://www.youtube.com/watch?v=abc"
    assert normalize_url("https://www.youtube.com/@channel") == "https://www.youtube.com/@channel"


# ── Quality ladder ────────────────────────────────────────────────────────────

def _fmt(w, h, vcodec="avc1", ext="mp4"):
    return {"width": w, "height": h, "vcodec": vcodec, "ext": ext}


def test_quality_buckets_use_the_short_side():
    from bot.services.downloader import available_qualities, quality_bucket
    assert [quality_bucket(x) for x in (144, 240, 360, 480, 720, 1080, 2160)] == [360, 360, 360, 480, 720, 1080, None]
    vertical = {"formats": [_fmt(360, 640), _fmt(720, 1280), _fmt(1080, 1920), _fmt(48, 27, ext="mhtml"),
                            {"vcodec": "none", "acodec": "mp4a"}]}
    assert available_qualities(vertical) == [360, 720, 1080]
    landscape = {"formats": [_fmt(426, 240), _fmt(854, 480), _fmt(3840, 2160)]}
    assert available_qualities(landscape) == [360, 480]


def test_vertical_video_selector_caps_the_width():
    from bot.services.downloader import _format_selector, is_portrait
    sel = _format_selector("720", 10_000, dim="width")
    assert "[width<=720]" in sel and "[height<=" not in sel
    assert "[height<=360]" in _format_selector("360")
    assert is_portrait({"requested_formats": [_fmt(720, 1280), {"vcodec": "none"}]})
    assert not is_portrait({"width": 1280, "height": 720, "vcodec": "avc1"})


def test_media_meta_feeds_captions_and_buttons():
    from bot.services.downloader import media_meta
    info = {"title": "  Me at   the zoo ", "uploader": "jawed", "duration": 19.4,
            "thumbnail": "https://i.ytimg.com/x.jpg", "width": 320, "height": 240, "vcodec": "avc1",
            "formats": [_fmt(320, 240), _fmt(640, 480)]}
    meta = media_meta(info)
    assert meta["title"] == "Me at the zoo"
    assert meta["uploader"] == "jawed" and meta["duration"] == 19
    assert meta["qualities"] == [360, 480] and meta["quality"] == 360


def test_youtube_is_delivered_fast_first():
    from bot.config import settings
    from bot.handlers.download import default_quality
    assert default_quality("youtube") == settings.youtube_default_quality == "360"
    assert default_quality("tiktok") == settings.default_video_quality == "720"


# ── Uzbek Cyrillic (4th language) ────────────────────────────────────────────

@pytest.mark.parametrize("latin,cyrillic", [
    ("Qo'shiq nomini to'liqroq yozing", "Қўшиқ номини тўлиқроқ ёзинг"),
    ("Bu video shaxsiy yoki o'chirilgan.", "Бу видео шахсий ёки ўчирилган."),
    ("yo'q qorong'u Ma'lumot eski yetdi", "йўқ қоронғу Маълумот эски етди"),
    ("Ishonch choy", "Ишонч чой"),
])
def test_transliteration(latin, cyrillic):
    from bot.utils.translit import to_cyrillic
    assert to_cyrillic(latin) == cyrillic


def test_transliteration_keeps_markup_names_and_placeholders():
    from bot.utils.translit import to_cyrillic
    src = "<b>{first_name}</b> YouTube'da MP3 {mb} MB @vidyuklaydi_bot https://t.me/x?a=b instagram.com/reel/..."
    out = to_cyrillic(src)
    for token in ("<b>", "</b>", "{first_name}", "YouTube", "MP3", "{mb}", "MB", "@vidyuklaydi_bot",
                  "https://t.me/x?a=b", "instagram.com/reel/..."):
        assert token in out
    assert "да" in out  # the Uzbek suffix after the brand is transliterated


def test_every_text_has_cyrillic_with_same_placeholders_and_tags():
    from bot.texts import DEFAULT_WELCOME, LANGS, T
    assert LANGS == ("uz", "oz", "ru", "en")
    ph = re.compile(r"\{[^{}]*\}")
    tag = re.compile(r"</?[a-z]+>")
    for key, entry in T.items():
        assert "oz" in entry, key
        assert sorted(ph.findall(entry["oz"])) == sorted(ph.findall(entry["uz"])), key
        assert sorted(tag.findall(entry["oz"])) == sorted(tag.findall(entry["uz"])), key
    assert "Салом" in DEFAULT_WELCOME["oz"] and "{first_name}" in DEFAULT_WELCOME["oz"]


def test_language_picker_has_four_languages_in_two_rows():
    from bot.db.repo import SUPPORTED_LANGS
    from bot.keyboards.inline import lang_keyboard
    rows = lang_keyboard().inline_keyboard
    assert [len(r) for r in rows] == [2, 2]
    codes = [b.callback_data.split(":", 1)[1] for r in rows for b in r]
    assert codes == ["uz", "oz", "ru", "en"]
    assert set(codes) == set(SUPPORTED_LANGS)


def test_welcome_has_add_to_group_button():
    from bot.config import settings
    from bot.keyboards.inline import welcome_keyboard
    btn = welcome_keyboard("uz").inline_keyboard[0][0]
    assert btn.url == f"https://t.me/{settings.bot_username}?startgroup=true"
    assert "Guruhga" in btn.text


def test_old_default_welcome_is_upgradable():
    from bot.texts import DEFAULT_WELCOME, LEGACY_DEFAULT_WELCOMES
    assert LEGACY_DEFAULT_WELCOMES and LEGACY_DEFAULT_WELCOMES[0]["uz"] != DEFAULT_WELCOME["uz"]


# ── Captions and keyboards under delivered media ─────────────────────────────

def test_video_keyboard_marks_qualities_that_must_be_fetched():
    from bot.keyboards.inline import video_keyboard
    kb = video_keyboard("abcdef123456", "uz", [360, 480, 720, 1080], {"360", "720"})
    rows = [[(b.text, b.callback_data) for b in r] for r in kb.inline_keyboard]
    assert rows[0] == [("🎬 360p", "vq:360:abcdef123456"), ("🎬 480p (⏳)", "vq:480:abcdef123456")]
    assert rows[1] == [("🎬 720p", "vq:720:abcdef123456"), ("🎬 1080p (⏳)", "vq:1080:abcdef123456")]
    assert [c for _, c in rows[2]] == ["vdalt:mp3:abcdef123456", "vshz:abcdef123456"]
    assert all(len(b.callback_data.encode()) <= 64 for r in kb.inline_keyboard for b in r)


def test_video_keyboard_without_qualities_keeps_audio_and_find_song():
    from bot.keyboards.inline import video_keyboard
    rows = video_keyboard("k", "ru", []).inline_keyboard
    assert len(rows) == 1 and rows[0][0].text == "🎶 Аудио"


def test_captions_escape_titles_and_show_details_on_quality_change():
    from bot.config import settings
    from bot.services.delivery import audio_caption, progress_caption, video_caption
    plain = video_caption("A <b> & B")
    assert plain == f"🍿 A &lt;b&gt; &amp; B\n\n👉 @{settings.bot_username}"
    full = video_caption("Zoo", "https://www.youtube.com/watch?v=jNQXAC9IVRw", "480", full=True)
    assert full.splitlines()[0] == "🍿 <code>Zoo</code>"
    assert "🔗 https://www.youtube.com/watch?v=jNQXAC9IVRw" in full and "📺 480p" in full
    assert video_caption("") == f"👉 @{settings.bot_username}"
    assert audio_caption() == f"👉 @{settings.bot_username}"
    assert progress_caption("Zoo", "uz").endswith("Yuklanmoqda...")


def test_only_youtube_offers_quality_buttons():
    from bot.services.delivery import offered_qualities
    assert offered_qualities("youtube", {"qualities": [360, 720]}) == [360, 720]
    assert offered_qualities("instagram", {"qualities": [360, 720]}) == []


def test_music_list_matches_the_numbered_layout():
    from bot.keyboards.inline import music_search_keyboard
    from bot.services.music_search import SongEntry
    entries = [SongEntry(id=f"v{i}", title=f"Artist - Song {i}", uploader="Chan", duration=279,
                         url=f"https://youtu.be/v{i}") for i in range(12)]
    entries[1] = SongEntry(id="v1", title="Song <1>", uploader="Singer", duration=0, url="https://youtu.be/v1")
    text, kb = music_search_keyboard("qh", entries, page=0, total_pages=2, lang="uz")
    lines = text.splitlines()
    assert lines[0] == "1. Artist - Song 0  4:39"
    assert lines[1] == "2. Singer - Song &lt;1&gt;"
    rows = kb.inline_keyboard
    assert [b.text for b in rows[0]] == ["1", "2", "3", "4", "5"]
    assert [b.text for b in rows[-1]] == ["➡️"]
    _, kb2 = music_search_keyboard("qh", entries, page=1, total_pages=2, lang="uz")
    assert [b.text for b in kb2.inline_keyboard[-1]] == ["⬅️"]


def test_clean_title_drops_empty_brackets():
    from bot.services.music_search import clean_title
    assert clean_title("Botir Qodirov - Oyning o'n beshi qorong'u (Official Video)") == \
        "Botir Qodirov - Oyning o'n beshi qorong'u"


# ── Fast YouTube search parsing ───────────────────────────────────────────────

def test_fast_search_parses_video_renderers():
    from bot.services.yt_search import parse_duration, parse_results
    data = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {"sectionListRenderer": {
        "contents": [{"itemSectionRenderer": {"contents": [
            {"adSlotRenderer": {}},
            {"videoRenderer": {"videoId": "qrcCiRjlEiE",
                               "title": {"runs": [{"text": "Shahzoda - Yurak"}]},
                               "ownerText": {"runs": [{"text": "Shahzoda"}]},
                               "lengthText": {"simpleText": "3:36"},
                               "thumbnail": {"thumbnails": [{"url": "a"}, {"url": "b"}]}}},
            {"videoRenderer": {"videoId": "x2", "title": {"simpleText": "Live"}}},
        ]}}]}}}}}
    hits = parse_results(data)
    assert hits[0] == {"id": "qrcCiRjlEiE", "title": "Shahzoda - Yurak", "uploader": "Shahzoda",
                       "duration": 216, "thumbnail": "b"}
    assert hits[1]["duration"] == 0
    assert parse_results({"contents": {}}) == []
    assert parse_duration("1:02:03") == 3723 and parse_duration("LIVE") == 0


async def test_search_songs_prefers_fast_path_and_falls_back():
    from bot.services import music_search, yt_search
    fast = AsyncMock(return_value=[{"id": "abc", "title": "T (Official Video)", "uploader": "U",
                                     "duration": 100, "thumbnail": None}])
    with patch.object(yt_search, "search", fast), \
         patch.object(music_search, "proxy_for", return_value=None):
        res = await music_search.search_songs("q")
    assert [e.id for e in res] == ["abc"] and res[0].title == "T"

    slow = MagicMock(return_value=[{"id": "zzz", "title": "Slow", "uploader": "U", "duration": 1}])
    with patch.object(yt_search, "search", AsyncMock(return_value=None)), \
         patch.object(music_search, "proxy_for", return_value=None), \
         patch.object(music_search, "_search_sync", slow):
        res = await music_search.search_songs("q")
    assert [e.id for e in res] == ["zzz"]


# ── Handlers: default quality, groups, quality change ────────────────────────

def _message(chat_type="private", text="https://youtu.be/dQw4w9WgXcQ"):
    m = MagicMock()
    m.text = text
    m.message_id = 555
    m.from_user = SimpleNamespace(id=42)
    m.chat = SimpleNamespace(id=-100 if chat_type != "private" else 42, type=chat_type)
    m.bot.send_chat_action = AsyncMock()
    m.answer = AsyncMock(return_value=SimpleNamespace(message_id=7))
    m.reply = AsyncMock(return_value=SimpleNamespace(message_id=8))
    return m


async def test_youtube_link_is_queued_at_360p():
    from bot.handlers import download
    msg = _message()
    get_fid = AsyncMock(return_value=None)
    enqueue = AsyncMock()
    with patch.object(download, "get_file_id", get_fid), \
         patch.object(download, "queue_has_room", AsyncMock(return_value=True)), \
         patch.object(download, "reserve_slot", AsyncMock(return_value=True)), \
         patch.object(download, "_enqueue_download", enqueue):
        await download.handle_url(msg, "uz", MagicMock(), MagicMock())
    get_fid.assert_awaited_once_with("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "360")
    kw = enqueue.await_args.kwargs
    assert kw["quality"] == "360" and kw["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert kw["reply_to"] is None
    assert msg.answer.await_args.args[0] == "⏳ Yuklanmoqda..."


async def test_group_link_replies_under_the_message_and_ignores_other_sites():
    from bot.handlers import download
    msg = _message("supergroup", "news https://kun.uz/x and https://youtu.be/dQw4w9WgXcQ")
    enqueue = AsyncMock()
    with patch.object(download, "get_file_id", AsyncMock(return_value=None)), \
         patch.object(download, "queue_has_room", AsyncMock(return_value=True)), \
         patch.object(download, "reserve_slot", AsyncMock(return_value=True)), \
         patch.object(download, "_enqueue_download", enqueue):
        await download.handle_url(msg, "uz", MagicMock(), MagicMock())
    assert enqueue.await_count == 1
    assert enqueue.await_args.kwargs["reply_to"] == 555
    msg.reply.assert_awaited_once()          # progress threads under the link
    msg.answer.assert_not_awaited()          # no "unsupported link" noise in the group


async def test_group_with_only_foreign_links_is_silent():
    from bot.handlers import download
    msg = _message("group", "https://kun.uz/news")
    await download.handle_url(msg, "uz", MagicMock(), MagicMock())
    msg.reply.assert_not_awaited()
    msg.answer.assert_not_awaited()


def _callback(data, video=True):
    cb = MagicMock()
    cb.data = data
    cb.from_user = SimpleNamespace(id=42)
    cb.answer = AsyncMock()
    cb.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))
    from aiogram.types import Message
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=42, type="private")
    msg.message_id = 77
    msg.video = object() if video else None
    msg.edit_caption = AsyncMock()
    msg.delete = AsyncMock()
    cb.message = msg
    return cb, msg


async def test_quality_change_progress_lives_in_the_video_caption():
    from bot.handlers import download
    cb, msg = _callback("vq:480:abcdef123456")
    enqueue = AsyncMock()
    with patch.object(download, "get_url_by_key", AsyncMock(return_value="https://www.youtube.com/watch?v=dQw4w9WgXcQ")), \
         patch.object(download, "get_file_id", AsyncMock(return_value=None)), \
         patch.object(download, "get_video_meta", AsyncMock(return_value={"title": "Zoo"})), \
         patch.object(download, "queue_has_room", AsyncMock(return_value=True)), \
         patch.object(download, "reserve_slot", AsyncMock(return_value=True)), \
         patch.object(download, "_enqueue_download", enqueue):
        await download.cb_video_quality(cb, "uz", MagicMock(), MagicMock())
    caption = msg.edit_caption.await_args.kwargs["caption"]
    assert caption.startswith("🍿 Zoo") and caption.endswith("Yuklanmoqda...")
    assert msg.edit_caption.await_args.kwargs["reply_markup"] is None
    kw = enqueue.await_args.kwargs
    assert kw["progress"] == "caption" and kw["message_id"] == 77 and kw["quality"] == "480"
    cb.bot.send_message.assert_not_awaited()


async def test_cached_quality_is_sent_at_once_and_replaces_the_old_video():
    from bot.handlers import download
    cb, msg = _callback("vq:720:abcdef123456")
    send = AsyncMock()
    with patch.object(download, "get_url_by_key", AsyncMock(return_value="https://www.youtube.com/watch?v=dQw4w9WgXcQ")), \
         patch.object(download, "get_file_id", AsyncMock(return_value="FID720")), \
         patch.object(download, "send_cached", send), \
         patch.object(download, "_record_cache_hit", AsyncMock()):
        await download.cb_video_quality(cb, "uz", MagicMock(), MagicMock())
    assert send.await_args.args[5] == "FID720" and send.await_args.kwargs["full"] is True
    msg.delete.assert_awaited_once()


async def test_forged_quality_is_ignored():
    from bot.handlers import download
    cb, msg = _callback("vq:9999:abcdef123456")
    with patch.object(download, "get_url_by_key", AsyncMock()) as lookup:
        await download.cb_video_quality(cb, "uz", MagicMock(), MagicMock())
    lookup.assert_not_awaited()
    cb.answer.assert_awaited_once()


async def test_find_song_works_from_the_url_key_alone():
    from bot.handlers import download
    with patch.object(download, "get_video_for_shazam", AsyncMock(return_value=None)), \
         patch.object(download, "get_url_by_key", AsyncMock(return_value="https://www.youtube.com/watch?v=dQw4w9WgXcQ")), \
         patch.object(download, "get_file_id", AsyncMock(side_effect=lambda u, q: "F1|F2" if q == "360" else None)):
        data = await download._video_for_shazam("abcdef123456")
    assert data == {"fid": "F1", "suf": ".mp4", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}


async def test_channel_wall_never_fires_in_groups():
    from bot.middlewares.channel import ChannelCheckMiddleware
    handler = AsyncMock(return_value="handled")
    event = SimpleNamespace(from_user=SimpleNamespace(id=1), chat=SimpleNamespace(type="supergroup"),
                            text="https://youtu.be/x")
    mw = ChannelCheckMiddleware()
    with patch.object(ChannelCheckMiddleware, "_is_member", AsyncMock(return_value=False)):
        assert await mw(handler, event, {"user_lang": "uz"}) == "handled"


# ── Worker: speed-relevant ordering ──────────────────────────────────────────

class _FakeBot:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "send_video":
                return SimpleNamespace(video=SimpleNamespace(file_id="NEWFID"), message_id=900)
            if name == "send_audio":
                return SimpleNamespace(audio=SimpleNamespace(file_id="AUDFID"), message_id=901)
            if name == "send_photo":
                return SimpleNamespace(message_id=902)
            return True
        return call


async def test_buttons_ride_on_the_upload_and_status_is_removed(tmp_path):
    from bot.services.downloader import DownloadResult
    from bot.worker import tasks

    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1000)
    meta = {"title": "Zoo", "uploader": "jawed", "duration": 19, "width": 640, "height": 360,
            "qualities": [360, 480], "thumbnail": ""}
    bot = _FakeBot()
    with patch.object(tasks, "get_file_id", AsyncMock(return_value=None)), \
         patch.object(tasks, "acquire_single_flight", AsyncMock(return_value=True)), \
         patch.object(tasks, "release_single_flight", AsyncMock()), \
         patch.object(tasks, "release_user_slot", AsyncMock()), \
         patch.object(tasks, "set_file_id", AsyncMock()) as set_fid, \
         patch.object(tasks, "_record_download", AsyncMock()), \
         patch.object(tasks, "get_temp_path", return_value=str(tmp_path / "out.mp4")), \
         patch.object(tasks, "download", AsyncMock(return_value=DownloadResult(str(video), 1.0, 2.0, 1000, meta))), \
         patch("bot.services.cache.cached_qualities", AsyncMock(return_value=set())), \
         patch("bot.services.delivery.register_video_buttons", AsyncMock()) as reg:
        await tasks.download_task({"bot": bot}, user_id=1, chat_id=2, message_id=3,
                                  url="https://www.youtube.com/watch?v=jNQXAC9IVRw", platform="youtube",
                                  quality="360", user_lang="uz", slot_reserved=True)
    names = [c[0] for c in bot.calls]
    assert "edit_message_reply_markup" not in names, "keyboard must ride on send_video"
    sv = next(c for c in bot.calls if c[0] == "send_video")
    kb = sv[2]["reply_markup"]
    assert [b.text for b in kb.inline_keyboard[0]] == ["🎬 360p", "🎬 480p (⏳)"]
    assert sv[2]["caption"].startswith("🍿 Zoo")
    assert sv[2]["width"] == 640 and sv[2]["height"] == 360 and sv[2]["duration"] == 19
    assert ("delete_message", (2, 3), {}) in bot.calls
    set_fid.assert_awaited_once_with("https://www.youtube.com/watch?v=jNQXAC9IVRw", "360", "NEWFID")
    reg.assert_awaited_once()


async def test_quality_change_replaces_the_old_video(tmp_path):
    from bot.services.downloader import DownloadResult
    from bot.worker import tasks

    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1000)
    bot = _FakeBot()
    with patch.object(tasks, "get_file_id", AsyncMock(return_value=None)), \
         patch.object(tasks, "acquire_single_flight", AsyncMock(return_value=True)), \
         patch.object(tasks, "release_single_flight", AsyncMock()), \
         patch.object(tasks, "release_user_slot", AsyncMock()), \
         patch.object(tasks, "set_file_id", AsyncMock()), \
         patch.object(tasks, "_record_download", AsyncMock()), \
         patch.object(tasks, "get_temp_path", return_value=str(tmp_path / "out.mp4")), \
         patch.object(tasks, "download", AsyncMock(return_value=DownloadResult(
             str(video), 1.0, 2.0, 1000, {"title": "Zoo", "qualities": [360, 480]}))), \
         patch("bot.services.cache.cached_qualities", AsyncMock(return_value={"360"})), \
         patch("bot.services.delivery.register_video_buttons", AsyncMock()):
        await tasks.download_task({"bot": bot}, user_id=1, chat_id=2, message_id=77,
                                  url="https://www.youtube.com/watch?v=jNQXAC9IVRw", platform="youtube",
                                  quality="480", user_lang="uz", slot_reserved=True, progress="caption")
    sv = next(c for c in bot.calls if c[0] == "send_video")
    assert "📺 480p" in sv[2]["caption"] and "🔗 https://www.youtube.com/watch?v=jNQXAC9IVRw" in sv[2]["caption"]
    assert ("delete_message", (2, 77), {}) in bot.calls
    assert not any(c[0] == "edit_message_text" for c in bot.calls), "caption-mode never edits text"


async def test_failed_quality_change_restores_the_original_video(tmp_path):
    from bot.services.downloader import DownloadError
    from bot.worker import tasks

    bot = _FakeBot()
    with patch.object(tasks, "get_file_id", AsyncMock(return_value=None)), \
         patch.object(tasks, "acquire_single_flight", AsyncMock(return_value=True)), \
         patch.object(tasks, "release_single_flight", AsyncMock()), \
         patch.object(tasks, "release_user_slot", AsyncMock()), \
         patch.object(tasks, "get_temp_path", return_value=str(tmp_path / "out.mp4")), \
         patch.object(tasks, "download", AsyncMock(side_effect=DownloadError("x", kind="too_large"))), \
         patch("bot.services.delivery.video_presentation", AsyncMock(return_value=("CAP", "KB"))):
        await tasks.download_task({"bot": bot}, user_id=1, chat_id=2, message_id=77,
                                  url="https://www.youtube.com/watch?v=jNQXAC9IVRw", platform="youtube",
                                  quality="1080", user_lang="uz", slot_reserved=True, progress="caption")
    restore = next(c for c in bot.calls if c[0] == "edit_message_caption")
    assert restore[2]["caption"] == "CAP" and restore[2]["reply_markup"] == "KB"
    notice = next(c for c in bot.calls if c[0] == "send_message")
    assert "katta" in notice[1][1]
    assert not any(c[0] == "delete_message" for c in bot.calls)


async def test_song_card_is_sent_before_the_lyrics_lookup():
    from bot.worker import tasks

    order = []
    bot = _FakeBot()
    real_getattr = bot.__getattr__

    def tracking(name):
        fn = real_getattr(name)

        async def wrapped(*a, **k):
            order.append(name)
            return await fn(*a, **k)
        return wrapped
    bot.__getattr__ = tracking  # type: ignore[assignment]

    result = SimpleNamespace(title="Song", artist="Singer", lyrics=None, coverart="https://img/c.jpg",
                             apple_url=None, shazam_url=None, spotify_url=None, source="shazam")

    async def slow_lyrics(title, artist):
        order.append("get_lyrics")
        return "la la la"

    class Wrapper:
        def __getattr__(self, name):
            return tracking(name)

    w = Wrapper()
    with patch.object(tasks, "_download_for_recognize", AsyncMock(return_value=True)), \
         patch.object(tasks, "recognize", AsyncMock(return_value=result)), \
         patch.object(tasks, "store_song_meta", AsyncMock(return_value="sk")), \
         patch("bot.services.cache.set_song_cache", AsyncMock()), \
         patch("bot.services.lyrics.get_lyrics", slow_lyrics), \
         patch("bot.db.session.async_session_factory", side_effect=RuntimeError("no db")):
        await tasks.recognize_task({"bot": w}, user_id=1, chat_id=2, message_id=3,
                                   file_id="F", file_suffix=".ogg", user_lang="uz")
    assert order.index("send_photo") < order.index("get_lyrics"), order
    assert "edit_message_reply_markup" in order[order.index("get_lyrics"):], "lyrics button added later"
