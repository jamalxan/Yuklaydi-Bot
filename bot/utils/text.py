"""Small text helpers shared by handlers and workers."""
from __future__ import annotations

import html
from typing import Optional


def esc(value: Optional[object]) -> str:
    """HTML-escape any external string before it goes into a parse_mode=HTML message.

    The bot runs with DefaultBotProperties(parse_mode=HTML), so every value
    that comes from YouTube, Shazam, lyrics APIs, channel titles or users
    must pass through here — a bare ``<`` or ``&`` makes Telegram reject the
    whole message with "can't parse entities".
    """
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def safe_https_url(url: Optional[str]) -> Optional[str]:
    """Only https URLs may become Telegram URL buttons.

    External services (Shazam, AudD) supply these links; an invalid or
    non-https one would make Telegram reject the whole song card.
    """
    if not url or not isinstance(url, str) or len(url) > 2048:
        return None
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname:
        return None
    return url.strip()
