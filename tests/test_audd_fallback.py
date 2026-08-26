"""
Tests for the AudD.io fallback recognizer: used when Shazam finds no match and
AUDD_TOKEN is configured. Covers response parsing and the recognize() wiring
(tried only after all three Shazam windows miss, only when a token is set).
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── _parse_audd_result: response → SongResult ─────────────────────────────────

def test_parse_audd_result_full():
    from bot.services.recognizer import _parse_audd_result

    result = _parse_audd_result({
        "title": "Blinding Lights",
        "artist": "The Weeknd",
        "apple_music": {
            "url": "https://music.apple.com/song/123",
            "artwork": {"url": "https://example.com/{w}x{h}bb.jpg"},
        },
        "spotify": {
            "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
        },
    })

    assert result.title == "Blinding Lights"
    assert result.artist == "The Weeknd"
    assert result.apple_url == "https://music.apple.com/song/123"
    assert result.spotify_url == "https://open.spotify.com/track/abc"
    assert result.coverart == "https://example.com/500x500bb.jpg"
    assert result.source == "audd"


def test_parse_audd_result_minimal():
    from bot.services.recognizer import _parse_audd_result

    result = _parse_audd_result({"title": "Song", "artist": "Artist"})
    assert result.title == "Song"
    assert result.artist == "Artist"
    assert result.apple_url is None
    assert result.spotify_url is None
    assert result.coverart is None
    assert result.source == "audd"


def test_parse_audd_result_spotify_cover_fallback():
    """When Apple Music has no artwork, fall back to Spotify's album image."""
    from bot.services.recognizer import _parse_audd_result

    result = _parse_audd_result({
        "title": "Song",
        "artist": "Artist",
        "spotify": {"album": {"images": [{"url": "https://spotify.example/cover.jpg"}]}},
    })
    assert result.coverart == "https://spotify.example/cover.jpg"


# ── _try_audd: HTTP wiring ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_try_audd_returns_none_without_token_file():
    """No snippet on disk → bail out before making any HTTP request."""
    from bot.services.recognizer import _try_audd
    result = await _try_audd("/nonexistent/path.wav")
    assert result is None


@pytest.mark.asyncio
async def test_try_audd_parses_success_response(tmp_path):
    from bot.services.recognizer import _try_audd

    snippet = tmp_path / "snippet.wav"
    snippet.write_bytes(b"\x00" * 5000)

    fake_payload = {
        "status": "success",
        "result": {"title": "Found Song", "artist": "Found Artist"},
    }

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=fake_payload)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session), \
         patch("bot.services.recognizer.settings") as mock_settings:
        mock_settings.audd_token = "fake_token"
        result = await _try_audd(str(snippet))

    assert result is not None
    assert result.title == "Found Song"
    assert result.artist == "Found Artist"
    assert result.source == "audd"


@pytest.mark.asyncio
async def test_try_audd_returns_none_on_no_match(tmp_path):
    from bot.services.recognizer import _try_audd

    snippet = tmp_path / "snippet.wav"
    snippet.write_bytes(b"\x00" * 5000)

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"status": "success", "result": None})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session), \
         patch("bot.services.recognizer.settings") as mock_settings:
        mock_settings.audd_token = "fake_token"
        result = await _try_audd(str(snippet))

    assert result is None


@pytest.mark.asyncio
async def test_try_audd_handles_http_error_gracefully(tmp_path):
    from bot.services.recognizer import _try_audd

    snippet = tmp_path / "snippet.wav"
    snippet.write_bytes(b"\x00" * 5000)

    with patch("aiohttp.ClientSession", side_effect=Exception("network down")), \
         patch("bot.services.recognizer.settings") as mock_settings:
        mock_settings.audd_token = "fake_token"
        result = await _try_audd(str(snippet))

    assert result is None


# ── recognize(): only tries AudD after all Shazam windows miss ────────────────

@pytest.mark.asyncio
async def test_recognize_tries_audd_only_after_shazam_misses():
    from bot.services.recognizer import recognize, SongResult

    async def _fake_probe(path):
        return 30.0

    async def _fake_snippet(input_path, output_path, snippet_duration, start_override):
        with open(output_path, "wb") as f:
            f.write(b"\xff" * 5000)
        return output_path

    async def _fake_shazam_recognize(path):
        return {}  # every Shazam window misses

    audd_result = SongResult(title="AudD Song", artist="AudD Artist", source="audd")
    fake_audd = AsyncMock(return_value=audd_result)

    with patch("bot.services.recognizer.probe_duration", _fake_probe), \
         patch("bot.services.recognizer.extract_audio_snippet", _fake_snippet), \
         patch("bot.services.recognizer._shazam.recognize", _fake_shazam_recognize), \
         patch("bot.services.recognizer._try_audd", fake_audd), \
         patch("bot.services.recognizer.settings") as mock_settings, \
         patch("bot.services.media.safe_delete"):
        mock_settings.audd_token = "fake_token"
        result = await recognize("/fake/audio.mp3")

    fake_audd.assert_called_once()
    assert result is audd_result


@pytest.mark.asyncio
async def test_recognize_skips_audd_when_no_token_configured():
    from bot.services.recognizer import recognize

    async def _fake_probe(path):
        return 30.0

    async def _fake_snippet(input_path, output_path, snippet_duration, start_override):
        with open(output_path, "wb") as f:
            f.write(b"\xff" * 5000)
        return output_path

    async def _fake_shazam_recognize(path):
        return {}

    fake_audd = AsyncMock()

    with patch("bot.services.recognizer.probe_duration", _fake_probe), \
         patch("bot.services.recognizer.extract_audio_snippet", _fake_snippet), \
         patch("bot.services.recognizer._shazam.recognize", _fake_shazam_recognize), \
         patch("bot.services.recognizer._try_audd", fake_audd), \
         patch("bot.services.recognizer.settings") as mock_settings, \
         patch("bot.services.media.safe_delete"):
        mock_settings.audd_token = ""
        result = await recognize("/fake/audio.mp3")

    fake_audd.assert_not_called()
    assert result is None
