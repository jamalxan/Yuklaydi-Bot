"""Fast YouTube search for the music list (~0.3-0.7 s instead of ~3.5 s).

yt-dlp's ``ytsearchN:`` builds a whole YoutubeDL, loads a YouTube page for
its client config and only then asks the search endpoint. Here the client
context is fetched once (from a results page, which already carries the
first 20 hits) and reused for later queries, which then cost one POST to
the same endpoint youtube.com itself calls.

Everything here is best effort: any failure returns None and the caller
falls back to yt-dlp, so a YouTube layout change degrades speed, never search.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Optional
from urllib.parse import quote_plus

import aiohttp

from bot.logger import logger

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
_API = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
_VIDEOS_ONLY = "EgIQAQ%3D%3D"  # "type: video" filter, URL-encoded for the page
_VIDEOS_ONLY_API = "EgIQAQ=="
_CONTEXT_TTL = 6 * 3600
_TIMEOUT = aiohttp.ClientTimeout(total=6, connect=3)

_CONTEXT_RE = re.compile(r'"INNERTUBE_CONTEXT":(\{.+?\}),"INNERTUBE_CONTEXT_CLIENT_NAME"')
_INITIAL_DATA_RE = re.compile(r"var ytInitialData\s*=\s*(\{.+?\});</script>", re.DOTALL)

_session: Optional[aiohttp.ClientSession] = None
_context: Optional[dict] = None
_context_at = 0.0
_context_lock: Optional[asyncio.Lock] = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=_TIMEOUT,
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
        )
    return _session


async def close() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def parse_duration(text: Optional[str]) -> int:
    """"4:39" -> 279, "1:02:03" -> 3723, None/garbage -> 0."""
    if not text:
        return 0
    total = 0
    for part in str(text).strip().split(":"):
        if not part.isdigit():
            return 0
        total = total * 60 + int(part)
    return total


def _text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return str(node["simpleText"])
    return "".join(str(r.get("text", "")) for r in node.get("runs") or [] if isinstance(r, dict))


def parse_results(data: dict) -> list[dict]:
    """videoRenderer items of a search response -> plain dicts."""
    out: list[dict] = []
    try:
        sections = (data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]
                    ["sectionListRenderer"]["contents"])
    except (KeyError, TypeError):
        return out
    for section in sections:
        items = ((section or {}).get("itemSectionRenderer") or {}).get("contents") or []
        for item in items:
            v = (item or {}).get("videoRenderer")
            if not v or not v.get("videoId"):
                continue
            thumbs = ((v.get("thumbnail") or {}).get("thumbnails") or [])
            out.append({
                "id": v["videoId"],
                "title": _text(v.get("title")),
                "uploader": _text(v.get("ownerText")) or _text(v.get("longBylineText")),
                "duration": parse_duration(_text(v.get("lengthText"))),
                "thumbnail": thumbs[-1].get("url") if thumbs and isinstance(thumbs[-1], dict) else None,
            })
    return out


async def _bootstrap(query: str) -> Optional[list[dict]]:
    """Load a results page: remember the client context, return its hits."""
    global _context, _context_at
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={_VIDEOS_ONLY}"
    async with _get_session().get(url) as resp:
        if resp.status != 200:
            logger.info(f"yt_search: results page HTTP {resp.status}")
            return None
        html = await resp.text()
    m = _CONTEXT_RE.search(html)
    if m:
        try:
            _context = json.loads(m.group(1))
            _context_at = time.monotonic()
        except ValueError:
            _context = None
    d = _INITIAL_DATA_RE.search(html)
    if not d:
        return None
    try:
        return parse_results(json.loads(d.group(1)))
    except ValueError:
        return None


async def _api_search(query: str, context: dict) -> Optional[list[dict]]:
    client = context.get("client") or {}
    headers = {
        "Origin": "https://www.youtube.com",
        "X-YouTube-Client-Name": "1",
        "X-YouTube-Client-Version": str(client.get("clientVersion", "")),
        "X-Goog-Visitor-Id": str(client.get("visitorData", "")),
    }
    body = {"context": context, "query": query, "params": _VIDEOS_ONLY_API}
    async with _get_session().post(_API, json=body, headers=headers) as resp:
        if resp.status != 200:
            logger.info(f"yt_search: API HTTP {resp.status}")
            return None
        data = await resp.json(content_type=None)
    results = parse_results(data)
    return results or None


def _fresh_context() -> Optional[dict]:
    return _context if (_context and time.monotonic() - _context_at < _CONTEXT_TTL) else None


async def search(query: str) -> Optional[list[dict]]:
    """Up to ~20 video hits for *query*, or None (caller falls back)."""
    global _context, _context_lock
    if _context_lock is None:
        _context_lock = asyncio.Lock()
    try:
        context = _fresh_context()
        if context is not None:
            results = await _api_search(query, context)
            if results:
                return results
            _context = None  # stale or rejected: rebuild below
        async with _context_lock:
            # Searches that queued here while another one rebuilt the context
            # use it instead of loading one results page after another.
            context = _fresh_context()
            if context is None:
                return await _bootstrap(query)
        return await _api_search(query, context)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as e:
        logger.info(f"yt_search failed, falling back to yt-dlp: {type(e).__name__}: {e}")
        return None
