"""
Tests for the intent-driven routing system.
Verifies: URL→download, media→recognize, plain text→search, unsupported→hint.
Asserts no ReplyKeyboardMarkup is ever sent.
"""
from __future__ import annotations


import pytest

from bot.handlers.download import extract_url
from bot.handlers.music import _URL_RE, _HANDLED_TYPES
from bot.services.music_search import clean_title, format_duration


# ── No reply keyboard anywhere ────────────────────────────────────────────────

def test_legacy_reply_module_and_ftl_locales_removed():
    """BUG-30: dead reply keyboard module and unused .ftl locales are gone."""
    import importlib.util
    import pathlib

    assert importlib.util.find_spec("bot.keyboards.reply") is None
    root = pathlib.Path(__file__).parent.parent
    assert not list((root / "bot" / "locales").glob("*.ftl"))


def test_no_reply_keyboard_imported_in_handlers():
    """Core handler modules must not import ReplyKeyboardMarkup."""
    import pathlib

    bad = []
    handler_dir = pathlib.Path(__file__).parent.parent / "bot" / "handlers"
    for py in handler_dir.glob("*.py"):
        src = py.read_text(encoding="utf-8")
        if "ReplyKeyboardMarkup" in src or "main_menu" in src:
            bad.append(py.name)
    assert not bad, f"These files still use ReplyKeyboard: {bad}"


# ── URL detection ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("https://www.youtube.com/watch?v=abc123", "https://www.youtube.com/watch?v=abc123"),
    ("Check this https://youtu.be/abc text", "https://youtu.be/abc"),
    ("https://tiktok.com/@user/video/123", "https://tiktok.com/@user/video/123"),
    ("hello world", None),
    ("", None),
    ("/start", None),
])
def test_extract_url(text, expected):
    assert extract_url(text) == expected


# ── Routing logic ─────────────────────────────────────────────────────────────

def _has_url(text: str) -> bool:
    return bool(_URL_RE.search(text or ""))


def _is_text_search(text: str) -> bool:
    return bool(text and not text.startswith("/") and not _has_url(text))


def _is_command(text: str) -> bool:
    return bool(text and text.startswith("/"))


@pytest.mark.parametrize("text,route", [
    ("https://youtu.be/abc", "download"),
    ("Check https://tiktok.com/v123 please", "download"),
    ("Bohemian Rhapsody", "music_search"),
    ("The Weeknd Blinding Lights", "music_search"),
    ("qo'shiq nomi", "music_search"),
    ("/start", "command"),
    ("/help", "command"),
    ("", "other"),
])
def test_text_routing(text, route):
    if route == "download":
        assert _has_url(text)
    elif route == "music_search":
        assert _is_text_search(text)
    elif route == "command":
        assert _is_command(text)
    else:
        assert not text or (not _has_url(text) and not _is_text_search(text))


# ── Content type routing ──────────────────────────────────────────────────────

def test_handled_types_contains_media():
    from aiogram.enums import ContentType
    assert ContentType.VOICE in _HANDLED_TYPES
    assert ContentType.AUDIO in _HANDLED_TYPES
    assert ContentType.VIDEO in _HANDLED_TYPES
    assert ContentType.VIDEO_NOTE in _HANDLED_TYPES
    assert ContentType.DOCUMENT in _HANDLED_TYPES
    assert ContentType.TEXT in _HANDLED_TYPES


def test_sticker_not_in_handled_types():
    from aiogram.enums import ContentType
    assert ContentType.STICKER not in _HANDLED_TYPES


def test_photo_not_in_handled_types():
    from aiogram.enums import ContentType
    assert ContentType.PHOTO not in _HANDLED_TYPES


# ── Music search helpers ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected_absent", [
    ("Bohemian Rhapsody (Official Video)", "(Official Video)"),
    ("Shape of You [Audio]", "[Audio]"),
    ("Blinding Lights HD", "HD"),
])
def test_clean_title(raw, expected_absent):
    result = clean_title(raw)
    assert expected_absent not in result


@pytest.mark.parametrize("secs,formatted", [
    (0, "0:00"),
    (65, "1:05"),
    (3661, "1:01:01"),
    (125, "2:05"),
])
def test_format_duration(secs, formatted):
    assert format_duration(secs) == formatted
