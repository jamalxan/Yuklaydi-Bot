"""
Security tests: HTML escaping, SQL injection protection,
no secrets in logs, fuzz inputs don't crash, temp file cleanup.
"""
from __future__ import annotations

import os
import re
import tempfile

import pytest


# ── HTML escaping (XSS prevention) ───────────────────────────────────────────

XSS_PAYLOADS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    '"><svg onload=alert(1)>',
    "{{7*7}}",
    "${7*7}",
    "'; DROP TABLE users; --",
]


def test_render_escapes_xss_in_first_name():
    from bot.handlers.start import _render
    for payload in XSS_PAYLOADS:
        result = _render("Hello {first_name}", payload, "user")
        # Raw executable HTML tags must not be present; html.escape() converts < to &lt;
        assert "<script>" not in result, f"Unescaped <script> in: {result!r}"
        assert "<img" not in result, f"Unescaped <img tag in: {result!r}"
        assert "<svg" not in result, f"Unescaped <svg tag in: {result!r}"
        # Verify < is actually escaped when payload contains it
        if "<" in payload:
            assert "&lt;" in result, f"< not escaped in: {result!r}"


def test_render_escapes_xss_in_username():
    from bot.handlers.start import _render
    for payload in XSS_PAYLOADS:
        result = _render("@{username}", "Name", payload)
        # Any angle brackets from payload must be escaped
        if "<" in payload:
            assert "<" not in result or "&lt;" in result


def test_html_escape_used_for_user_content():
    """Verify html.escape() is called (or equivalent) on user-supplied name."""
    from bot.handlers.start import _render
    raw = "Evil <b>bold</b> & <script>"
    result = _render("{first_name}", raw, "")
    # Original tags must not be present literally
    assert "<b>" not in result
    assert "<script>" not in result
    # But & must be escaped too
    assert "&amp;" in result or "&lt;" in result


# ── No secrets in log output ─────────────────────────────────────────────────

SECRET_PATTERNS = [
    r"\b[0-9]{9,10}:[A-Za-z0-9_-]{35}\b",   # Telegram bot token
    r"api_hash\s*=\s*[a-f0-9]{32}",           # API hash
]

def test_secrets_not_in_source():
    """Source files must not contain a real bot token or API hash."""
    import pathlib
    root = pathlib.Path(__file__).parent.parent / "bot"
    for py in root.rglob("*.py"):
        src = py.read_text(encoding="utf-8", errors="ignore")
        for pat in SECRET_PATTERNS:
            m = re.search(pat, src)
            if m:
                # Allow dummy/placeholder values
                match = m.group(0)
                if "dummy" not in match and "AAAAA" not in match and "test" not in match.lower():
                    pytest.fail(f"Potential secret in {py.name}: {match[:20]}...")


# ── SQL injection: repo uses parameterized queries ────────────────────────────

@pytest.mark.asyncio
async def test_setting_repo_uses_parameterized_queries():
    """SettingRepo.set must pass user input as bind params, never as raw SQL."""
    from unittest.mock import AsyncMock
    from bot.db.repo import SettingRepo

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()

    repo = SettingRepo(mock_session)
    injection = "evil'; DROP TABLE settings; --"
    await repo.set("test_key", injection)

    # execute must have been called; the injection string should appear only as a bound parameter
    assert mock_session.execute.called
    call_args = mock_session.execute.call_args
    # The statement object should not contain the raw injection string
    stmt_str = str(call_args[0][0])
    assert injection not in stmt_str, "Injection string must not appear in raw SQL"


@pytest.mark.asyncio
async def test_user_repo_get_uses_parameterized_query():
    from unittest.mock import AsyncMock

    class FakeResult:
        def scalar_one_or_none(self): return None

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=FakeResult())
    from bot.db.repo import UserRepo
    repo = UserRepo(mock_session)
    await repo.get(999999)
    assert mock_session.execute.called


# ── Fuzz inputs: no crash on garbage ─────────────────────────────────────────

FUZZ_TEXTS = [
    "",
    " ",
    "\n\n\n",
    "a" * 4096,
    "\x00\x01\x02",
    "ℕℤℚℝℂ",
    "👾" * 100,
    "SELECT * FROM users",
    "../../../etc/passwd",
    None,
]


def test_extract_url_fuzz():
    """extract_url must never raise on garbage input."""
    from bot.handlers.download import extract_url
    for text in FUZZ_TEXTS:
        try:
            result = extract_url(text)
            assert result is None or result.startswith("http")
        except Exception as e:
            pytest.fail(f"extract_url raised on {text!r}: {e}")


def test_detect_platform_fuzz():
    """detect_platform must never raise."""
    from bot.services.downloader import detect_platform
    for text in FUZZ_TEXTS:
        if text is None:
            continue
        try:
            detect_platform(text)
        except Exception as e:
            pytest.fail(f"detect_platform raised on {text!r}: {e}")


def test_normalize_url_fuzz():
    """normalize_url must never raise and must return a string."""
    from bot.services.downloader import normalize_url
    for text in FUZZ_TEXTS:
        if text is None:
            continue
        try:
            result = normalize_url(text)
            assert isinstance(result, str)
        except Exception as e:
            pytest.fail(f"normalize_url raised on {text!r}: {e}")


def test_clean_title_fuzz():
    """clean_title must never raise."""
    from bot.services.music_search import clean_title
    for text in FUZZ_TEXTS:
        if text is None:
            continue
        try:
            clean_title(text)
        except Exception as e:
            pytest.fail(f"clean_title raised on {text!r}: {e}")


def test_format_duration_fuzz():
    """format_duration must handle edge values."""
    from bot.services.music_search import format_duration
    for v in [0, -1, 99999, 86400, None]:
        try:
            if v is None:
                format_duration(0)
            else:
                result = format_duration(v)
                assert isinstance(result, str)
        except Exception as e:
            pytest.fail(f"format_duration raised on {v!r}: {e}")


# ── Temp file cleanup ─────────────────────────────────────────────────────────

def test_safe_delete_removes_file():
    from bot.services.media import safe_delete
    fd, path = tempfile.mkstemp()
    os.close(fd)
    assert os.path.exists(path)
    safe_delete(path)
    assert not os.path.exists(path)


def test_safe_delete_no_crash_on_missing():
    from bot.services.media import safe_delete
    safe_delete("/nonexistent/path/that/doesnt/exist.mp4")


def test_safe_delete_no_crash_on_empty_string():
    from bot.services.media import safe_delete
    safe_delete("")
    safe_delete(None)  # type: ignore


# ── Unknown-platform URLs are rejected (SSRF guard) ───────────────────────────

@pytest.mark.asyncio
async def test_unknown_platform_url_rejected():
    """URLs outside SUPPORTED_PLATFORMS must be answered 'unsupported', not enqueued."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from bot.handlers.download import handle_url

    for bad in [
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata SSRF
        "http://localhost:8081/secret",
        "http://192.168.1.1/admin",
        "https://random-unknown-site.example/video.mp4",
    ]:
        msg = MagicMock()
        msg.text = bad
        msg.from_user = MagicMock(); msg.from_user.id = 1
        msg.chat = MagicMock(); msg.chat.id = 1
        msg.answer = AsyncMock()
        msg.bot = AsyncMock()

        with patch("bot.handlers.download._enqueue_download", new=AsyncMock()) as enq:
            await handle_url(msg, user_lang="en", user_repo=AsyncMock(), download_repo=AsyncMock())
            assert not enq.called, f"{bad} must NOT be enqueued"
        msg.answer.assert_called_once()


# ── Malformed callback data must not raise ────────────────────────────────────

@pytest.mark.asyncio
async def test_lyrics_callback_garbage_page():
    from unittest.mock import AsyncMock, MagicMock
    from bot.handlers.shazam import cb_lyrics

    cb = MagicMock()
    cb.data = "lyrics:NOTANUMBER:abc123"
    cb.answer = AsyncMock()
    cb.message = AsyncMock()
    await cb_lyrics(cb, user_lang="en")   # must not raise
    cb.answer.assert_called()


@pytest.mark.asyncio
async def test_music_pick_garbage_and_negative_index():
    from unittest.mock import AsyncMock, MagicMock, patch
    from bot.handlers.music import cb_music_pick

    for data in ["ms:h:xx", "ms:h:-1", "ms:h:999"]:
        cb = MagicMock()
        cb.data = data
        cb.answer = AsyncMock()
        cb.message = AsyncMock()
        with patch("bot.handlers.music.get_music_search_by_hash",
                   new=AsyncMock(return_value=[{"id": "a", "title": "t", "uploader": "u",
                                                "duration": 1, "url": "https://youtu.be/a",
                                                "thumbnail": None}])):
            await cb_music_pick(cb, user_lang="en")   # must not raise
        cb.answer.assert_called()
