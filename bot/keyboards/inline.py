from __future__ import annotations

from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.services.music_search import SongEntry, format_duration

LANG_FLAGS = {"uz": "🇺🇿 O'zbek", "ru": "🇷🇺 Русский", "en": "🇬🇧 English"}

LISTEN_LABELS = {"uz": "🎧 Tinglash", "ru": "🎧 Слушать", "en": "🎧 Listen"}
LYRICS_LABELS = {"uz": "📝 Qo'shiq matni", "ru": "📝 Текст песни", "en": "📝 Lyrics"}
FIND_SONG_LABELS = {"uz": "🎵 Qo'shiqni top", "ru": "🎵 Найти песню", "en": "🎵 Find song"}
MP3_LABELS = {"uz": "🎧 Audio sifatida olish", "ru": "🎧 Получить аудио", "en": "🎧 Get as audio"}
HD_LABELS = {"uz": "🎬 1080p", "ru": "🎬 1080p", "en": "🎬 1080p"}
DL_SONG_LABELS = {"uz": "⬇️ MP3 yuklab olish", "ru": "⬇️ Скачать MP3", "en": "⬇️ Download MP3"}
RETRY_LABELS = {"uz": "🔁 Qayta", "ru": "🔁 Повторить", "en": "🔁 Retry"}
SUBSCRIBED_LABELS = {"uz": "✅ Obuna bo'ldim", "ru": "✅ Я подписался", "en": "✅ I subscribed"}


def lang_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🇺🇿 O'zbek", callback_data="lang:uz"),
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
    )
    return builder.as_markup()


def video_result_keyboard(url_key: str, shazam_key: str, lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=FIND_SONG_LABELS.get(lang, "🎵 Find song"), callback_data=f"vshz:{shazam_key}"),
        InlineKeyboardButton(text=MP3_LABELS.get(lang, "🎧 MP3"), callback_data=f"vdalt:mp3:{url_key}"),
    )
    return builder.as_markup()


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

    listen_btns = []
    if apple_url:
        listen_btns.append(InlineKeyboardButton(text="🍎 Apple Music", url=apple_url))
    if spotify_url:
        listen_btns.append(InlineKeyboardButton(text="🟢 Spotify", url=spotify_url))
    if shazam_url:
        listen_btns.append(InlineKeyboardButton(text="🎧 Shazam", url=shazam_url))
    for i in range(0, len(listen_btns), 2):
        builder.row(*listen_btns[i:i + 2])

    # callback_data uses 12-char hash key (≤21 bytes total, well under 64-byte limit)
    if has_lyrics:
        builder.row(InlineKeyboardButton(
            text=LYRICS_LABELS.get(lang, "📝 Lyrics"),
            callback_data=f"lyrics:0:{song_key}",
        ))
    if show_download:
        builder.row(InlineKeyboardButton(
            text=DL_SONG_LABELS.get(lang, "⬇️ Download MP3"),
            callback_data=f"dlsong:{song_key}",
        ))
    builder.row(InlineKeyboardButton(
        text=RETRY_LABELS.get(lang, "🔁 Retry"),
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
        dur = format_duration(entry.duration)
        lines.append(f"{num}. <b>{entry.uploader}</b> — {entry.title}  <code>{dur}</code>")

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
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"msp:{qhash}:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"msp:{qhash}:{page+1}"))
    if nav:
        builder.row(*nav)

    return text, builder.as_markup()


def audio_result_keyboard(song_key: str, lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text=LYRICS_LABELS.get(lang, "📝 Lyrics"),
        callback_data=f"lyrics:0:{song_key}",
    ))
    return builder.as_markup()


def channel_join_keyboard(channel_url: str, lang: str) -> InlineKeyboardMarkup:
    join_labels = {"uz": "📢 Kanalga o'tish", "ru": "📢 Перейти в канал", "en": "📢 Join channel"}
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=join_labels.get(lang, "📢 Join"), url=channel_url))
    builder.row(InlineKeyboardButton(
        text=SUBSCRIBED_LABELS.get(lang, "✅ I subscribed"),
        callback_data="channel:check",
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
