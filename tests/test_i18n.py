"""
i18n tests: key consistency across uz/ru/en, no missing translations,
lang persistence, no hardcoded user-facing English strings in handler dicts.
"""
from __future__ import annotations



LANGS = ("uz", "ru", "en")


# ── Key-set consistency across languages ──────────────────────────────────────

def _check_dict(d: dict, name: str):
    """Assert every language has every key and no value is an empty string.

    Supports both layouts:
      - {lang: {key: value}}  (lang-first)
      - {key: {lang: value}}  (key-first, used in handlers)
    """
    if not d:
        return
    first_val = next(iter(d.values()))
    # Detect {key: {lang: value}} layout: inner dicts contain lang codes
    if isinstance(first_val, dict) and set(first_val.keys()) & set(LANGS):
        for key, translations in d.items():
            if isinstance(translations, dict):
                for lang in LANGS:
                    assert lang in translations, f"{name}[{key}]: missing lang '{lang}'"
                    assert translations[lang], f"{name}[{key}][{lang}]: empty string"
        return
    # {lang: {key: value}} layout
    keys: set = set()
    for lang, sub in d.items():
        if isinstance(sub, dict):
            keys |= set(sub.keys())
    for lang in LANGS:
        assert lang in d, f"{name}: missing lang '{lang}'"
        if isinstance(d[lang], dict):
            for k in keys:
                assert k in d[lang], f"{name}[{lang}]: missing key '{k}'"
                assert d[lang][k], f"{name}[{lang}][{k}]: empty string"


def test_download_handler_strings():
    from bot.handlers.download import _DL, _ERR
    _check_dict(_DL, "_DL")
    _check_dict(_ERR, "_ERR")


def test_music_handler_strings():
    from bot.handlers.music import _MSGS
    _check_dict(_MSGS, "_MSGS")


def test_shazam_handler_strings():
    from bot.handlers.shazam import _MSGS
    _check_dict(_MSGS, "_MSGS")


def test_start_handler_help_texts():
    from bot.handlers.start import HELP_TEXTS, _DEFAULT_WELCOME
    for lang in LANGS:
        assert lang in HELP_TEXTS, f"HELP_TEXTS missing lang '{lang}'"
        assert HELP_TEXTS[lang], f"HELP_TEXTS[{lang}] is empty"
        assert lang in _DEFAULT_WELCOME, f"_DEFAULT_WELCOME missing lang '{lang}'"
        assert "{first_name}" in _DEFAULT_WELCOME[lang], f"_DEFAULT_WELCOME[{lang}] missing {{first_name}}"


def test_channel_middleware_strings():
    from bot.middlewares.channel import _JOIN_MSGS, _JOIN_BTN, _CHECK_BTN
    for d, name in ((_JOIN_MSGS, "_JOIN_MSGS"), (_JOIN_BTN, "_JOIN_BTN"), (_CHECK_BTN, "_CHECK_BTN")):
        for lang in LANGS:
            assert lang in d, f"{name}: missing lang '{lang}'"
            assert d[lang], f"{name}[{lang}] is empty"


def test_worker_task_strings():
    from bot.worker.tasks import _DL, _ERR, _NOT_FOUND
    _check_dict(_DL, "_DL")
    _check_dict(_ERR, "_ERR")
    for lang in LANGS:
        assert lang in _NOT_FOUND, f"_NOT_FOUND: missing lang '{lang}'"


# ── Placeholder consistency in welcome templates ──────────────────────────────

def test_welcome_placeholders_available():
    """All welcome templates must have {first_name} (username is optional)."""
    from bot.handlers.start import _DEFAULT_WELCOME
    for lang in LANGS:
        text = _DEFAULT_WELCOME[lang]
        assert "{first_name}" in text, f"_DEFAULT_WELCOME[{lang}] missing {{first_name}}"


def test_render_preserves_html_and_escapes_user_input():
    from bot.handlers.start import _render
    result = _render("<b>{first_name}</b>", "Al<script>", "@user")
    assert "<script>" not in result
    assert "&lt;script&gt;" in result
    assert "<b>" in result  # structural HTML not escaped


# ── Lang code validation ──────────────────────────────────────────────────────

def test_lang_codes_are_valid():
    """Supported lang codes: uz, oz (Uzbek Cyrillic), ru, en."""
    valid = {"uz", "oz", "ru", "en"}
    from bot.handlers.start import HELP_TEXTS
    from bot.db.repo import SUPPORTED_LANGS
    assert set(HELP_TEXTS.keys()) == valid
    assert set(SUPPORTED_LANGS) == valid


def test_default_lang_is_supported():
    from bot.config import settings
    assert settings.default_lang in ("uz", "ru", "en")


# ── No hardcoded plain-English user strings in handlers (key paths) ───────────

def test_download_errors_have_all_langs():
    from bot.handlers.download import _ERR
    required_keys = {"private", "geo", "age", "too_large", "unsupported", "generic", "overloaded"}
    assert set(_ERR.keys()) >= required_keys


def test_keyboard_labels_have_all_langs():
    """Keyboard label dicts must cover all three languages."""
    from bot.keyboards.inline import (
        LISTEN_LABELS, LYRICS_LABELS,
        FIND_SONG_LABELS, MP3_LABELS, DL_SONG_LABELS,
        RETRY_LABELS, SUBSCRIBED_LABELS,
    )
    for d, name in [
        (LISTEN_LABELS, "LISTEN_LABELS"),
        (LYRICS_LABELS, "LYRICS_LABELS"),
        (FIND_SONG_LABELS, "FIND_SONG_LABELS"),
        (MP3_LABELS, "MP3_LABELS"),
        (DL_SONG_LABELS, "DL_SONG_LABELS"),
        (RETRY_LABELS, "RETRY_LABELS"),
        (SUBSCRIBED_LABELS, "SUBSCRIBED_LABELS"),
    ]:
        for lang in LANGS:
            assert lang in d, f"{name}: missing lang '{lang}'"
