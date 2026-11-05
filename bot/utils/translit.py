"""Uzbek Latin -> Uzbek Cyrillic transliteration for the bot's own texts.

The fourth interface language ("oz", Ўзбекча) is generated from the Latin
Uzbek strings instead of being maintained by hand, so the two can never drift
apart. Markup and names that must stay as they are (HTML tags, entities,
``{placeholders}``, @handles, URLs and domains, acronyms such as MP3, brand
names such as YouTube) are protected and copied through unchanged.
"""
from __future__ import annotations

import re

# Apostrophe variants used for o'/g' and the tutuq belgisi (ma'lumot).
_APOS = "'ʻʼ‘’`"

_BRANDS = (
    "Apple Music", "Yuklaydi Bot", "Twitter/X", "YouTube", "TikTok", "Instagram",
    "Facebook", "Twitter", "Pinterest", "Vimeo", "Reddit", "Shazam", "Spotify",
    "Telegram", "Genius", "Yuklaydi", "Reel", "Reels",
)

_PROTECT_RE = re.compile(
    r"<[^>]+>"                                   # HTML tags
    r"|&#?[A-Za-z0-9]+;"                         # HTML entities
    r"|\{[^{}]*\}"                               # format placeholders
    r"|@\w+"                                     # @handles
    r"|https?://\S+"                             # URLs
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:com|uz|be|ru|org|net|io|me|it)\b[^\s,;)]*"  # bare domains
    r"|\b(?:" + "|".join(re.escape(b) for b in _BRANDS) + r")\b"
    r"|\b[A-Z0-9]*[A-Z][A-Z0-9]*\d[A-Z0-9]*\b"   # MP3, H264 ...
    r"|\b\d+(?:p|K|fps|kbps)\b"                  # 1080p, 4K ...
    # Only known acronyms: an all-caps Uzbek word (YO'Q, DIQQAT) is text.
    r"|\b(?:MB|GB|KB|HD|ID|URL|API|GIF|FPS|PDF|SMS|OK)\b"
)

_SINGLE = {
    "a": "а", "b": "б", "c": "с", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "ҳ",
    "i": "и", "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п",
    "q": "қ", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "х",
    "y": "й", "z": "з",
}
_Y_VOWEL = {"a": "я", "o": "ё", "u": "ю", "e": "е"}


def _case(src: str, cyr: str) -> str:
    return cyr.upper() if src.isupper() else cyr


def _segment(seg: str) -> str:
    out: list[str] = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        lc = c.lower()
        nxt = seg[i + 1] if i + 1 < n else ""
        prev = seg[i - 1] if i > 0 else ""
        # o' -> ў, g' -> ғ. Word-final g' is common (tog', bog'), word-final
        # o' is not: there the mark is a closing quote (‘video’).
        after = seg[i + 2] if i + 2 < n else ""
        if lc in ("o", "g") and nxt and nxt in _APOS and (lc == "g" or after.isalpha()):
            out.append(_case(c, "ў" if lc == "o" else "ғ"))
            i += 2
            continue
        # sh -> ш, ch -> ч
        if lc in ("s", "c") and nxt.lower() == "h":
            out.append(_case(c, "ш" if lc == "s" else "ч"))
            i += 2
            continue
        # ya / yo / yu / ye -> я / ё / ю / е (but y + o' is й + ў)
        if lc == "y" and nxt.lower() in _Y_VOWEL:
            after = seg[i + 2] if i + 2 < n else ""
            if not (nxt.lower() == "o" and after and after in _APOS):
                out.append(_case(c, _Y_VOWEL[nxt.lower()]))
                i += 2
                continue
        # e -> э at the start of a word and after a vowel (aeroport, poeziya)
        if lc == "e" and (not (prev.isalpha() or (prev and prev in _APOS))
                          or prev.lower() in "aoue"):
            out.append(_case(c, "э"))
            i += 1
            continue
        # tutuq belgisi after a letter -> ъ
        if c in _APOS and prev.isalpha() and nxt.isalpha():
            out.append("ъ")
            i += 1
            continue
        if lc in _SINGLE:
            out.append(_case(c, _SINGLE[lc]))
        else:
            out.append(c)
        i += 1
    return "".join(out)


def to_cyrillic(text: str) -> str:
    """Transliterate Uzbek Latin *text*, leaving protected tokens untouched."""
    if not text:
        return text
    out: list[str] = []
    last = 0
    for m in _PROTECT_RE.finditer(text):
        out.append(_segment(text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_segment(text[last:]))
    return "".join(out)
