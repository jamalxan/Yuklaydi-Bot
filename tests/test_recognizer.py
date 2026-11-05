import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bot.services.recognizer import _parse_result


def test_parse_result_full():
    data = {
        "track": {
            "title": "Blinding Lights",
            "subtitle": "The Weeknd",
            "images": {"coverart": "https://example.com/cover.jpg"},
            "hub": {
                "actions": [{"type": "uri", "uri": "https://music.apple.com/song/123"}],
                "options": [{"actions": [{"uri": "https://www.shazam.com/track/123"}]}],
            },
            "url": "https://www.shazam.com/track/123",
            "sections": [
                {
                    "type": "LYRICS",
                    "text": ["I've been running out of time", "I feel it coming"],
                }
            ],
        }
    }
    result = _parse_result(data)
    assert result is not None
    assert result.title == "Blinding Lights"
    assert result.artist == "The Weeknd"
    assert result.coverart == "https://example.com/cover.jpg"
    assert result.apple_url == "https://music.apple.com/song/123"
    assert result.lyrics == "I've been running out of time\nI feel it coming"


def test_parse_result_no_track():
    assert _parse_result({}) is None
    assert _parse_result({"track": None}) is None


def test_parse_result_minimal():
    data = {"track": {"title": "Song", "subtitle": "Artist"}}
    result = _parse_result(data)
    assert result.title == "Song"
    assert result.artist == "Artist"
    assert result.coverart is None
    assert result.lyrics is None


# ── Recognition pipeline: probe_duration stream fallback ─────────────────────

@pytest.mark.asyncio
async def test_probe_duration_format_field():
    """probe_duration reads from format.duration when present."""
    import json
    from bot.services.media import probe_duration

    fake_output = json.dumps({"format": {"duration": "15.5"}, "streams": []})

    async def _fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(fake_output.encode(), b""))
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        dur = await probe_duration("/fake/voice.ogg")
    assert dur == pytest.approx(15.5)


@pytest.mark.asyncio
async def test_probe_duration_stream_fallback():
    """probe_duration falls back to stream duration when format.duration is missing (OGG/Opus)."""
    import json
    from bot.services.media import probe_duration

    fake_output = json.dumps({
        "format": {"duration": "0"},
        "streams": [{"codec_type": "audio", "duration": "12.3"}],
    })

    async def _fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(fake_output.encode(), b""))
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        dur = await probe_duration("/fake/voice.ogg")
    assert dur == pytest.approx(12.3)


@pytest.mark.asyncio
async def test_probe_duration_returns_zero_on_failure():
    from bot.services.media import probe_duration

    async def _bad_exec(*args, **kwargs):
        raise OSError("ffprobe not found")

    with patch("asyncio.create_subprocess_exec", side_effect=_bad_exec):
        dur = await probe_duration("/fake/file.ogg")
    assert dur == 0.0


# ── Recognition pipeline: multi-window strategy ──────────────────────────────

@pytest.mark.asyncio
async def test_recognize_returns_none_on_empty_snippet():
    """If every snippet is < 1000 bytes, recognize returns None (no false match)."""
    from bot.services.recognizer import recognize

    async def _fake_probe(path):
        return 20.0

    async def _fake_snippet(input_path, output_path, snippet_duration, start_override):
        # Write minimal content — too small to recognize
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 500)
        return output_path

    with patch("bot.services.recognizer.probe_duration", _fake_probe), \
         patch("bot.services.recognizer.extract_audio_snippet", _fake_snippet), \
         patch("bot.services.media.safe_delete"):
        result = await recognize("/fake/voice.ogg")

    assert result is None


@pytest.mark.asyncio
async def test_recognize_falls_back_to_second_window():
    """If middle window misses, the start window should be tried."""
    from bot.services.recognizer import recognize

    call_count = [0]

    async def _fake_probe(path):
        return 30.0

    async def _fake_snippet(input_path, output_path, snippet_duration, start_override):
        with open(output_path, "wb") as f:
            f.write(b"\xff" * 5000)
        return output_path

    async def _fake_shazam_recognize(path):
        call_count[0] += 1
        if call_count[0] == 1:
            return {}  # Window 1 misses
        return {"track": {"title": "Found It", "subtitle": "Artist"}}

    with patch("bot.services.recognizer.probe_duration", _fake_probe), \
         patch("bot.services.recognizer.extract_audio_snippet", _fake_snippet), \
         patch("bot.services.recognizer._shazam.recognize", _fake_shazam_recognize), \
         patch("bot.services.media.safe_delete"):
        result = await recognize("/fake/audio.mp3")

    assert result is not None
    assert result.title == "Found It"
    assert call_count[0] == 2


# ── Recognition routing: voice and media go to recognition queue ──────────────

def test_handle_media_routes_voice_to_recognition_queue():
    """handle_media must enqueue to arq:queue:recognition, not arq:queue."""
    import ast
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "bot" / "handlers" / "shazam.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    enqueue_calls_in_handle_media = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_media":
            for child in ast.walk(node):
                if isinstance(child, ast.Await):
                    expr = child.value
                    if isinstance(expr, ast.Call):
                        func = expr.func
                        if isinstance(func, ast.Attribute) and func.attr == "enqueue_job":
                            # Extract keyword args
                            kw = {k.arg: k.value for k in expr.keywords}
                            if "_queue_name" in kw:
                                val = kw["_queue_name"]
                                if isinstance(val, ast.Constant):
                                    enqueue_calls_in_handle_media.append(val.value)

    assert enqueue_calls_in_handle_media, "handle_media must call enqueue_job with _queue_name"
    assert all(q == "arq:queue:recognition" for q in enqueue_calls_in_handle_media), \
        f"Expected arq:queue:recognition but got {enqueue_calls_in_handle_media}"


def test_handle_media_is_media_filter_covers_all_types():
    """_is_media must match voice, audio, video, video_note, document."""
    from bot.handlers.shazam import _is_media
    from aiogram.types import Message

    for attr in ("voice", "audio", "video", "video_note", "document"):
        msg = MagicMock(spec=Message)
        # Set only the current attribute to a truthy value
        for a in ("voice", "audio", "video", "video_note", "document"):
            setattr(msg, a, None if a != attr else MagicMock())
        assert _is_media(msg), f"_is_media should return True for message with {attr}"


# ── source_url stored for cache-hit videos ───────────────────────────────────

@pytest.mark.asyncio
async def test_store_video_for_shazam_persists_source_url():
    """store_video_for_shazam must store source_url so recognize fallback works."""
    from unittest.mock import AsyncMock, patch

    stored = {}

    class _FakeRedis:
        async def setex(self, key, ttl, value):
            stored[key] = value
        async def get(self, key):
            return stored.get(key)

    with patch("bot.services.cache.get_redis", new=AsyncMock(return_value=_FakeRedis())):
        from bot.services.cache import store_video_for_shazam, get_video_for_shazam
        key = await store_video_for_shazam("file123", source_url="https://youtube.com/watch?v=abc")
        data = await get_video_for_shazam(key)

    assert data is not None
    assert data["fid"] == "file123"
    assert data.get("url") == "https://youtube.com/watch?v=abc"


@pytest.mark.asyncio
async def test_store_video_for_shazam_without_source_url():
    """store_video_for_shazam without source_url still works (no url key in payload)."""

    stored = {}

    class _FakeRedis:
        async def setex(self, key, ttl, value):
            stored[key] = value
        async def get(self, key):
            return stored.get(key)

    with patch("bot.services.cache.get_redis", new=AsyncMock(return_value=_FakeRedis())):
        from bot.services.cache import store_video_for_shazam, get_video_for_shazam
        key = await store_video_for_shazam("file456")
        data = await get_video_for_shazam(key)

    assert data is not None
    assert data["fid"] == "file456"
    assert "url" not in data


# ── _download_for_recognize: Telegram → yt-dlp fallback ─────────────────────

@pytest.mark.asyncio
async def test_download_for_recognize_uses_telegram_first():
    """Primary path: download from Telegram if get_file succeeds."""
    import tempfile
    from bot.worker.tasks import _download_for_recognize

    bot = MagicMock()
    file_info = MagicMock()
    file_info.file_path = "/fake/voice.ogg"
    bot.get_file = AsyncMock(return_value=file_info)

    async def _fake_download(file_path, destination):
        with open(destination, "wb") as f:
            f.write(b"\xff" * 1000)

    bot.download_file = AsyncMock(side_effect=_fake_download)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tf:
        temp_path = tf.name
    os.unlink(temp_path)

    try:
        result = await _download_for_recognize(bot, "fid_abc", ".ogg", temp_path)
        assert result is True
        assert os.path.exists(temp_path)
        assert os.path.getsize(temp_path) > 0
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


@pytest.mark.asyncio
async def test_download_for_recognize_falls_back_to_yt_dlp():
    """If Telegram get_file fails, fall back to yt-dlp re-download from source_url."""
    import tempfile
    from bot.worker.tasks import _download_for_recognize
    from bot.services.downloader import DownloadResult

    bot = MagicMock()
    bot.get_file = AsyncMock(side_effect=Exception("API error: file too large"))
    bot.download_file = AsyncMock()

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        temp_path = tf.name
    os.unlink(temp_path)

    produced_paths = []

    def _fake_clip_sync(url, output_path):
        with open(output_path, "wb") as f:
            f.write(b"\xff" * 2000)
        produced_paths.append(output_path)
        return output_path

    with patch("bot.services.downloader._download_audio_clip_sync", side_effect=_fake_clip_sync):
        result = await _download_for_recognize(
            bot, "fid_abc", ".mp4", temp_path, source_url="https://youtube.com/watch?v=abc"
        )

    # Primary path failed (get_file raised), fallback was attempted
    bot.get_file.assert_called_once()
    # Fallback succeeded → result should be True and file exists at temp_path
    assert result is True
    if os.path.exists(temp_path):
        os.unlink(temp_path)
    assert produced_paths and produced_paths[0].endswith("_clip.m4a")
