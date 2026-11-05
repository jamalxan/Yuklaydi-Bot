from __future__ import annotations

import re
from typing import Optional

from bot.config import settings
from bot.logger import logger
from bot.services.cache import get_song_cache, set_song_cache

LYRICS_PAGE_SIZE = 3000


# Page chrome Genius' scraper leaves around the lyrics.
_GENIUS_HEADER = re.compile(r"^\s*\d*\s*Contributors?.*?Lyrics", re.IGNORECASE)
_GENIUS_EMBED = re.compile(r"\d*\s*Embed\s*$", re.IGNORECASE)


def clean_lyrics(text: str) -> str:
    text = _GENIUS_HEADER.sub("", text, count=1)
    text = _GENIUS_EMBED.sub("", text.rstrip())
    text = text.replace("You might also like", "")
    text = re.sub(r"\[.*?\]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def paginate_lyrics(text: str) -> list[str]:
    if len(text) <= LYRICS_PAGE_SIZE:
        return [text]
    pages = []
    while text:
        chunk = text[:LYRICS_PAGE_SIZE]
        last_nl = chunk.rfind("\n")
        if last_nl > LYRICS_PAGE_SIZE // 2:
            chunk = text[:last_nl]
        pages.append(chunk.strip())
        text = text[len(chunk):].lstrip("\n")
    return pages


# Words YouTube uploaders add around song titles (en / uz / ru).
_JUNK_WORDS = (
    r"official(?:\s+music)?(?:\s+video|\s+audio|\s+clip)?|music\s+video|lyric(?:s)?(?:\s+video)?"
    r"|audio|video|visualizer|hd|4k|hq|mv|full\s+song|premiere|premyera"
    r"|rasmiy(?:\s+(?:klip|video|audio))?|matnli|matn\s+bilan|klip|clip|jonli\s+ijro|konsert|concert"
    r"|премьера(?:\s+клипа)?|клип|текст|официальн\w*|аудио|видео"
)
_JUNK_RE = re.compile(rf"\b(?:{_JUNK_WORDS})\b", re.IGNORECASE)
_BRACKETS_RE = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
_FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)
_ARTIST_SUFFIX_RE = re.compile(
    r"(?:\s*-\s*topic|\s+(?:team|official|music|tv|vevo|channel|rasmiy|records?|production|studio|uz))+\s*$",
    re.IGNORECASE,
)
_DASHES_RE = re.compile(r"\s+[-–—]\s+")

_CYR = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h",
})


def _norm_name(name: str) -> str:
    """Comparable form of an artist name: latin, no apostrophes/spaces."""
    import unicodedata
    name = unicodedata.normalize("NFKD", (name or "").lower()).translate(_CYR)
    return "".join(c for c in name if c.isalnum() and c.isascii())


def artist_similar(a: str, b: str) -> bool:
    """Same artist despite spelling: "Sherali Juraev" ~ "Sherali Jo'rayev"."""
    from difflib import SequenceMatcher
    na, nb = _norm_name(a), _norm_name(b)
    if len(na) < 3 or len(nb) < 3:
        return False
    if na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= 0.75


def title_similar(a: str, b: str) -> bool:
    """Same song title, tolerant to spelling/brackets ("Yolg'on" ~ "Yolgon")."""
    from difflib import SequenceMatcher
    na, nb = _norm_name(_strip_junk(a)), _norm_name(_strip_junk(b))
    if not na or not nb:
        return False
    if na == nb or (min(len(na), len(nb)) >= 4 and (na in nb or nb in na)):
        return True
    return SequenceMatcher(None, na, nb).ratio() >= 0.8


def is_same_song(found_title: str, found_artist: str, title: str, artist: str) -> bool:
    """A lyrics hit is used only if BOTH title and artist match: a wrong
    song's lyrics are worse than "not found"."""
    return title_similar(found_title, title) and (not artist or artist_similar(found_artist, artist))


def _strip_junk(text: str) -> str:
    def bracket(m: re.Match) -> str:
        if _FEAT_RE.match(" " + m.group(1)):
            return ""
        inner = _JUNK_RE.sub("", m.group(1)).strip(" -|,.")
        return f"({inner})" if inner else ""
    text = _BRACKETS_RE.sub(bracket, text)
    text = re.split(r"\s+\|\s+|\s*\|\s*|\s*#", text)[0]
    text = _JUNK_RE.sub("", text)
    text = _FEAT_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -|,.")


def _clean_search_query(title: str, artist: str) -> tuple[str, str]:
    """Turn a YouTube title/channel into (song title, artist) for lyrics APIs.

    "Sherali Juraev - Gulbadan Rasmiy Matnli ()" / "Sherali Juraev Team"
    -> ("Gulbadan", "Sherali Juraev").
    """
    artist = _ARTIST_SUFFIX_RE.sub("", _FEAT_RE.sub("", artist or "")).strip()
    artist = re.split(r"\s+(?:&|x|vs\.?)\s+", artist, flags=re.IGNORECASE)[0].strip()
    title = title or ""
    parts = _DASHES_RE.split(title, maxsplit=1)
    if len(parts) == 2:
        left, right = (p.strip() for p in parts)
        if not artist or artist_similar(_strip_junk(left), artist):
            artist, title = _strip_junk(left) or artist, right   # "Artist - Title"
        elif artist_similar(_strip_junk(right), artist):
            title = left                                          # "Title - Artist"
    clean_title = _strip_junk(title) or title.strip()
    return clean_title, artist


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


async def _search_lrclib(title: str, artist: str) -> Optional[str]:
    """lrclib.net — free open lyrics API, no auth, no scraping."""
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Exact match endpoint first
            async with session.get(
                "https://lrclib.net/api/get",
                params={"artist_name": artist, "track_name": title},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    text = (data or {}).get("plainLyrics") or ""
                    if len(text) > 50:
                        return clean_lyrics(text)
            # Fuzzy search fallback
            async with session.get(
                "https://lrclib.net/api/search",
                params={"q": f"{artist} {title}"},
            ) as resp:
                if resp.status != 200:
                    return None
                hits = await resp.json()
        for hit in (hits or [])[:10]:
            text = hit.get("plainLyrics") or ""
            if len(text) > 50 and is_same_song(hit.get("trackName") or "", hit.get("artistName") or "",
                                               title, artist):
                return clean_lyrics(text)
    except Exception as e:
        logger.warning(f"lrclib lookup failed: {str(e)[:120]}")
    return None


async def _search_genius(title: str, artist: str) -> Optional[str]:
    if not settings.genius_token:
        return None
    try:
        import asyncio
        import lyricsgenius

        genius = lyricsgenius.Genius(
            settings.genius_token,
            timeout=10,
            retries=1,
            verbose=False,
            remove_section_headers=True,
        )
        genius.skip_non_songs = True
        # Cloudflare 403s non-browser UAs on the lyrics pages — make sure the
        # underlying requests session presents a browser UA either way.
        try:
            genius._session.headers["User-Agent"] = _BROWSER_UA
        except Exception:
            pass

        loop = asyncio.get_running_loop()
        song = await loop.run_in_executor(
            None,
            lambda: genius.search_song(title, artist, get_full_info=False),
        )
        if song and song.lyrics:
            if not is_same_song(getattr(song, "title", ""), getattr(song, "artist", ""), title, artist):
                logger.info(f"Genius returned a different song ({song.title!r} by {song.artist!r}) "
                            f"for {title!r} / {artist!r}; ignored")
                return None
            return clean_lyrics(song.lyrics)
    except Exception as e:
        err = str(e)
        if "403" in err or "429" in err:
            logger.warning(f"Genius blocked ({err[:60]}), skipping")
        else:
            logger.warning(f"Genius lookup failed: {err[:120]}")
    return None


async def _search_musixmatch(title: str, artist: str) -> Optional[str]:
    if not settings.musixmatch_token:
        return None
    try:
        import aiohttp
        params = {
            "format": "json",
            "namespace": "lyrics_richsynced",
            "q_track": title,
            "q_artist": artist,
            "apikey": settings.musixmatch_token,
            "f_has_lyrics": "1",
            "page_size": "1",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.musixmatch.com/ws/1.1/track.search",
                params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

        track_list = (data.get("message", {}).get("body", {}) or {}).get("track_list", [])
        if not track_list:
            return None

        track_id = track_list[0]["track"]["track_id"]
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.musixmatch.com/ws/1.1/track.lyrics.get",
                params={"track_id": track_id, "apikey": settings.musixmatch_token, "format": "json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

        lyrics_body = (data.get("message", {}).get("body", {}) or {}).get("lyrics", {}).get("lyrics_body", "")
        if lyrics_body and len(lyrics_body) > 50:
            return clean_lyrics(lyrics_body)
    except Exception as e:
        logger.warning(f"Musixmatch lookup failed: {e}")
    return None


async def _search_lrclib_by_title(title: str, artist: str) -> Optional[str]:
    """lrclib search by track name only; accept a hit whose artist matches fuzzily."""
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://lrclib.net/api/search", params={"track_name": title}) as resp:
                if resp.status != 200:
                    return None
                hits = await resp.json()
        for hit in (hits or [])[:20]:
            text = hit.get("plainLyrics") or ""
            if len(text) > 50 and is_same_song(hit.get("trackName") or "", hit.get("artistName") or "",
                                               title, artist):
                return clean_lyrics(text)
    except Exception as e:
        logger.warning(f"lrclib title search failed: {str(e)[:120]}")
    return None


async def _search_genius_by_title(title: str, artist: str) -> Optional[str]:
    """Genius search by title; take the first hit whose primary artist matches fuzzily."""
    if not settings.genius_token:
        return None
    try:
        import asyncio
        import lyricsgenius

        genius = lyricsgenius.Genius(settings.genius_token, timeout=10, retries=1, verbose=False,
                                     remove_section_headers=True)
        try:
            genius._session.headers["User-Agent"] = _BROWSER_UA
        except Exception:
            pass
        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(None, lambda: genius.search_songs(title, per_page=10))
        for hit in (found or {}).get("hits", []):
            song = hit.get("result") or {}
            name = (song.get("primary_artist") or {}).get("name") or ""
            url = song.get("url")
            if url and is_same_song(song.get("title") or "", name, title, artist):
                text = await loop.run_in_executor(
                    None, lambda: genius.lyrics(song_url=url, remove_section_headers=True))
                if text and len(text) > 50:
                    return clean_lyrics(text)
                break
    except Exception as e:
        logger.warning(f"Genius title search failed: {str(e)[:120]}")
    return None


async def get_lyrics(title: str, artist: str) -> Optional[str]:
    """Fetch lyrics: cache → lrclib → Genius → title-only fuzzy (lrclib, Genius) → Musixmatch."""
    cached = await get_song_cache(title, artist)
    if cached and cached.get("lyrics"):
        return cached["lyrics"]

    clean_title, clean_artist = _clean_search_query(title, artist)

    lyrics = await _search_lrclib(clean_title, clean_artist)

    if not lyrics:
        lyrics = await _search_genius(clean_title, clean_artist)

    # Exact queries miss transliteration variants (Juraev / Jo'rayev /
    # Жураев): search by title only and accept a fuzzily matching artist.
    if not lyrics and clean_artist:
        lyrics = await _search_lrclib_by_title(clean_title, clean_artist)
    if not lyrics and clean_artist:
        lyrics = await _search_genius_by_title(clean_title, clean_artist)

    if not lyrics and settings.musixmatch_token:
        lyrics = await _search_musixmatch(clean_title, clean_artist)

    if lyrics:
        await set_song_cache(title, artist, {"lyrics": lyrics})

    return lyrics
