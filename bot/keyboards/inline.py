from __future__ import annotations

from typing import Iterable, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.services.music_search import LONG_TRACK_SECONDS, SongEntry, format_duration
from bot.config import settings
from bot.texts import LANG_NAMES, LANGS, T, t
from bot.utils.text import esc, safe_https_url

LANG_FLAGS = LANG_NAMES

# Label dicts (views into bot.texts.T — the single source of strings)
LISTEN_LABELS = T["btn.listen"]
LYRICS_LABELS = T["btn.lyrics"]
FIND_SONG_LABELS = T["btn.find_song"]
MP3_LABELS = T["btn.mp3"]
HD_LABELS = T["btn.hd"]
DL_SONG_LABELS = T["btn.dl_song"]
RETRY_LABELS = T["btn.retry"]
SUBSCRIBED_LABELS = T["channel.check_btn"]


def lang_keyboard() -> InlineKeyboardMarkup:
    """Four languages in a 2x2 grid: Latin and Cyrillic Uzbek side by side."""
    builder = InlineKeyboardBuilder()
    buttons = [InlineKeyboardButton(text=LANG_NAMES[code], callback_data=f"lang:{code}")
               for code in LANGS]
    for i in range(0, len(buttons), 2):
        builder.row(*buttons[i:i + 2])
    return builder.as_markup()


def add_to_group_url() -> str:
    return f"https://t.me/{settings.bot_username}?startgroup=true"


def welcome_keyboard(lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=t("btn.add_group", lang), url=add_to_group_url()))
    return builder.as_markup()


def quality_label(quality, ready: bool) -> str:
    """"🎬 480p" when the file is ready, "🎬 480p (⏳)" when it must be fetched."""
    return f"🎬 {quality}p" if ready else f"🎬 {quality}p (⏳)"


def video_keyboard(
    url_key: str,
    lang: str,
    qualities: Iterable[int] = (),
    ready: Iterable[str] = (),
    shazam_key: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Buttons under a delivered video.

    Row(s) 1: one button per available quality (two per row); qualities that
    are not cached yet carry an hourglass. Last row: audio + find the song.
    """
    ready_set = {str(q) for q in ready}
    builder = InlineKeyboardBuilder()
    q_buttons = [
        InlineKeyboardButton(text=quality_label(q, str(q) in ready_set),
                             callback_data=f"vq:{q}:{url_key}")
        for q in qualities
    ]
    for i in range(0, len(q_buttons), 2):
        builder.row(*q_buttons[i:i + 2])
    builder.row(
        InlineKeyboardButton(text=t("btn.audio", lang), callback_data=f"vdalt:mp3:{url_key}"),
        InlineKeyboardButton(text=t("btn.find_song", lang),
                             callback_data=f"vshz:{shazam_key or url_key}"),
    )
    return builder.as_markup()


def video_result_keyboard(url_key: str, shazam_key: str, lang: str) -> InlineKeyboardMarkup:
    """Audio + find-song only (no quality row)."""
    return video_keyboard(url_key, lang, shazam_key=shazam_key)


def song_card_keyboard(
    apple_url: Optional[str],
    shazam_url: Optional[str],
    has_lyrics: bool,
    song_key: str,
    lang: str,
    show_download: bool = True,
    spotify_url: Optional[str] = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    # Links come from Shazam/AudD: only valid https URLs become buttons —
    # one bad URL would make Telegram reject the whole card.
    listen_btns = []
    for label, url in (("🍎 Apple Music", apple_url), ("🟢 Spotify", spotify_url), ("🎧 Shazam", shazam_url)):
        url = safe_https_url(url)
        if url:
            listen_btns.append(InlineKeyboardButton(text=label, url=url))
    if listen_btns:
        builder.row(*listen_btns)

    # callback_data uses 12-char hash key (≤21 bytes total, well under 64-byte limit)
    if has_lyrics:
        builder.row(InlineKeyboardButton(
            text=t("btn.lyrics", lang),
            callback_data=f"lyrics:0:{song_key}",
        ))
    if show_download:
        builder.row(InlineKeyboardButton(
            text=t("btn.dl_song", lang),
            callback_data=f"dlsong:{song_key}",
        ))
    builder.row(InlineKeyboardButton(
        text=t("btn.retry", lang),
        callback_data="shazam:retry",
    ))
    return builder.as_markup()


def lyrics_nav_keyboard(page: int, total: int, song_key: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="◀️", callback_data=f"lyrics:{page-1}:{song_key}"))
    if page < total - 1:
        row.append(InlineKeyboardButton(text="▶️", callback_data=f"lyrics:{page+1}:{song_key}"))
    if row:
        builder.row(*row)
    return builder.as_markup()


def music_search_keyboard(
    qhash: str,
    entries: list[SongEntry],
    page: int,
    total_pages: int,
    lang: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build numbered list text + inline keyboard for music search results."""
    start = page * 10
    page_entries = entries[start: start + 10]

    lines = []
    for i, entry in enumerate(page_entries, start=1):
        num = start + i
        dur = format_duration(entry.duration) if entry.duration else ""
        long_mark = f" {t('btn.long', lang)}" if entry.duration > LONG_TRACK_SECONDS else ""
        # The YouTube title usually already is "Artist - Song"; fall back to
        # "uploader - title" only when the title names no artist.
        name = entry.title if " - " in entry.title or not entry.uploader else f"{entry.uploader} - {entry.title}"
        # YouTube titles/uploaders are attacker-controlled: escape before HTML
        lines.append(f"{num}. {esc(name)}  {dur}{long_mark}".rstrip())

    text = "\n".join(lines)

    builder = InlineKeyboardBuilder()
    # Number buttons (2 rows of 5)
    nums = [
        InlineKeyboardButton(
            text=str(start + i + 1),
            callback_data=f"ms:{qhash}:{start + i}",
        )
        for i in range(len(page_entries))
    ]
    # Split into rows of 5
    for i in range(0, len(nums), 5):
        builder.row(*nums[i:i+5])

    # Pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"msp:{qhash}:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"msp:{qhash}:{page+1}"))
    if nav:
        builder.row(*nav)

    return text, builder.as_markup()


def audio_result_keyboard(song_key: str, lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text=t("btn.lyrics", lang),
        callback_data=f"lyrics:0:{song_key}",
    ))
    return builder.as_markup()


# ── Admin panel keyboards ──────────────────────────────────────────────────────

def admin_main_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Statistika", callback_data="adm:stats"),
        InlineKeyboardButton(text="📢 Broadcast", callback_data="adm:broadcast"),
    )
    builder.row(
        InlineKeyboardButton(text="✏️ Xush kelibsiz xabari", callback_data="adm:welcome"),
        InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="adm:export"),
    )
    builder.row(
        InlineKeyboardButton(text="🚫 Ban / Unban", callback_data="adm:ban"),
        InlineKeyboardButton(text="📺 Kanal", callback_data="adm:channel"),
    )
    builder.row(InlineKeyboardButton(text="❌ Yopish", callback_data="adm:close"))
    return builder.as_markup()


def admin_back_keyboard(to: str = "adm:main") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data=to))
    return builder.as_markup()


def admin_broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Yuborish", callback_data="adm:bc_send"),
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm:bc_cancel"),
    )
    return builder.as_markup()


def admin_welcome_lang_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🇺🇿 O'zbek", callback_data="adm:wl_uz"),
        InlineKeyboardButton(text="🇺🇿 Ўзбекча", callback_data="adm:wl_oz"),
    )
    builder.row(
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="adm:wl_ru"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="adm:wl_en"),
    )
    builder.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:main"))
    return builder.as_markup()


def admin_welcome_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Saqlash", callback_data="adm:wl_save"),
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm:welcome"),
    )
    return builder.as_markup()


def admin_ban_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🚫 Ban", callback_data="adm:do_ban"),
        InlineKeyboardButton(text="✅ Unban", callback_data="adm:do_unban"),
    )
    builder.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:main"))
    return builder.as_markup()


def admin_channel_keyboard(has_channel: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Kanal o'rnatish", callback_data="adm:ch_set"))
    if has_channel:
        builder.row(InlineKeyboardButton(text="🗑 Kanal o'chirish", callback_data="adm:ch_remove"))
    builder.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:main"))
    return builder.as_markup()


def admin_broadcast_skip_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⏩ O'tkazib yuborish", callback_data="adm:bc_skip_btn"))
    builder.row(InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm:bc_cancel"))
    return builder.as_markup()
