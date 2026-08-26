"""
Tests for multi-URL support: a message with several links should surface
every link, not just the first one extract_url (singular) returns.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.handlers.download import MAX_URLS_PER_MESSAGE, extract_url, extract_urls


# ── extract_urls: pure parsing ────────────────────────────────────────────────

def test_extract_urls_single_link():
    urls = extract_urls("check this out https://youtu.be/abc123")
    assert urls == ["https://youtu.be/abc123"]


def test_extract_urls_multiple_links_in_order():
    text = (
        "https://www.tiktok.com/@user/video/111 "
        "and also https://youtu.be/222 "
        "https://www.instagram.com/reel/333/"
    )
    urls = extract_urls(text)
    assert urls == [
        "https://www.tiktok.com/@user/video/111",
        "https://youtu.be/222",
        "https://www.instagram.com/reel/333/",
    ]


def test_extract_urls_deduplicates():
    text = "https://youtu.be/abc https://youtu.be/abc https://youtu.be/abc"
    urls = extract_urls(text)
    assert urls == ["https://youtu.be/abc"]


def test_extract_urls_caps_at_max():
    urls_in = " ".join(f"https://youtu.be/vid{i}" for i in range(MAX_URLS_PER_MESSAGE + 10))
    urls = extract_urls(urls_in)
    assert len(urls) == MAX_URLS_PER_MESSAGE


def test_extract_urls_empty_for_no_links():
    assert extract_urls("hello world") == []
    assert extract_urls("") == []
    assert extract_urls(None) == []


def test_extract_url_still_returns_first_match_only():
    """extract_url (singular) is a stable, separately-tested API — must keep
    returning just the first URL, unaffected by the new extract_urls helper."""
    text = "https://youtu.be/first https://youtu.be/second"
    assert extract_url(text) == "https://youtu.be/first"


# ── handle_url: processes every extracted link ────────────────────────────────

@pytest.mark.asyncio
async def test_handle_url_processes_every_link():
    from bot.handlers import download as dl_mod

    message = MagicMock()
    message.text = "https://youtu.be/one https://youtu.be/two https://youtu.be/three"
    message.from_user = MagicMock(id=42)
    message.chat = MagicMock(id=100)
    message.bot = AsyncMock()
    message.answer = AsyncMock()

    processed = []

    async def fake_process_one_url(msg, url, lang, user_repo, download_repo):
        processed.append(url)

    with patch.object(dl_mod, "_process_one_url", side_effect=fake_process_one_url):
        await dl_mod.handle_url(message, "en", MagicMock(), MagicMock())

    assert processed == [
        "https://youtu.be/one",
        "https://youtu.be/two",
        "https://youtu.be/three",
    ]


@pytest.mark.asyncio
async def test_handle_url_noop_when_no_links():
    from bot.handlers import download as dl_mod

    message = MagicMock()
    message.text = "no links here"

    with patch.object(dl_mod, "_process_one_url", new=AsyncMock()) as mock_process:
        await dl_mod.handle_url(message, "en", MagicMock(), MagicMock())

    mock_process.assert_not_called()
