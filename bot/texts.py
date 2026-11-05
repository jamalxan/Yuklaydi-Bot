"""Single source of every user-facing string (uz / ru / en).

Rules:
- Every key MUST exist in all three languages (enforced by tests/test_i18n.py).
- Values may contain HTML (the bot uses parse_mode=HTML) and ``{placeholders}``
  filled by :func:`t`. Placeholder VALUES coming from outside (titles, names)
  must be escaped by the caller with ``bot.utils.text.esc``.
"""
from __future__ import annotations

from bot.config import settings

LANGS = ("uz", "ru", "en")

T: dict[str, dict[str, str]] = {
    # ── Download progress ────────────────────────────────────────────────────
    "dl.downloading": {"uz": "⏳ Yuklanmoqda...", "ru": "⏳ Скачивается...", "en": "⏳ Downloading..."},
    "dl.queued":      {"uz": "📋 Navbatda...", "ru": "📋 В очереди...", "en": "📋 Queued..."},
    "dl.too_many_links": {"uz": "ℹ️ Bitta xabardan faqat birinchi {n} ta havola yuklanadi.", "ru": "ℹ️ Из одного сообщения скачиваются только первые {n} ссылок.", "en": "ℹ️ Only the first {n} links of a message are downloaded."},
    "dl.uploading":   {"uz": "📤 Yuborilmoqda...", "ru": "📤 Отправляется...", "en": "📤 Uploading..."},
    "dl.uploading_part": {"uz": "📤 Yuborilmoqda: {i}/{n}-qism...", "ru": "📤 Отправляется часть {i}/{n}...", "en": "📤 Uploading part {i}/{n}..."},
    "dl.part":        {"uz": "🎞 {i}/{n}-qism", "ru": "🎞 Часть {i}/{n}", "en": "🎞 Part {i}/{n}"},
    "dl.done":        {"uz": "✅ Tayyor!", "ru": "✅ Готово!", "en": "✅ Done!"},
    "dl.recognizing": {"uz": "🎧 Musiqa aniqlanmoqda...", "ru": "🎧 Распознаётся музыка...", "en": "🎧 Recognizing music..."},

    # ── Download errors (keys after "err." match DownloadError.kind) ─────────
    "err.private":     {"uz": "🔒 Bu video shaxsiy yoki o'chirilgan.", "ru": "🔒 Это видео приватное или удалено.", "en": "🔒 This video is private or has been deleted."},
    "err.geo":         {"uz": "🌍 Bu video hududingizda mavjud emas.", "ru": "🌍 Это видео недоступно в вашем регионе.", "en": "🌍 This video is not available in your region."},
    "err.age":         {"uz": "🔞 Bu video yoshi cheklangan.", "ru": "🔞 Это видео имеет возрастное ограничение.", "en": "🔞 This video has age restrictions."},
    "err.too_large":   {"uz": "📦 Fayl juda katta (maks {mb} MB).", "ru": "📦 Файл слишком большой (макс. {mb} МБ).", "en": "📦 File too large (max {mb} MB)."},
    "err.too_long":    {"uz": "⏱ Juda uzun: {minutes} daqiqagacha bo'lgan audio yuklanadi.", "ru": "⏱ Слишком длинно: скачиваем аудио до {minutes} минут.", "en": "⏱ Too long: audio up to {minutes} minutes is supported."},
    "err.unsupported": {"uz": "❌ Ushbu havola qo'llab-quvvatlanmaydi.", "ru": "❌ Эта ссылка не поддерживается.", "en": "❌ This link is not supported."},
    "err.no_video":    {"uz": "📷 Bu postda video yo'q yoki Instagram login talab qilmoqda.\n💡 Reel havolasini yuboring: instagram.com/reel/...", "ru": "📷 В этом посте нет видео или Instagram требует авторизацию.\n💡 Попробуйте ссылку на Reel: instagram.com/reel/...", "en": "📷 No video in this post or Instagram requires login.\n💡 Try a Reel link: instagram.com/reel/..."},
    "err.timeout":     {"uz": "⏱ Server bilan aloqa yo'q. Keyinroq urinib ko'ring.", "ru": "⏱ Нет связи с сервером. Попробуйте позже.", "en": "⏱ Connection timed out. Please try again later."},
    "err.job_timeout": {"uz": "⏱ Yuklash juda uzoq davom etdi va to'xtatildi. Qisqaroq video yuboring yoki keyinroq urinib ko'ring.", "ru": "⏱ Загрузка заняла слишком много времени и была остановлена. Попробуйте видео покороче или позже.", "en": "⏱ The download took too long and was stopped. Try a shorter video or again later."},
    "err.bot_check":   {"uz": "🤖 YouTube tekshiruvi tufayli yuklab bo'lmadi. Birozdan so'ng qayta urinib ko'ring.", "ru": "🤖 YouTube требует проверку. Попробуйте чуть позже.", "en": "🤖 YouTube bot-check hit. Please try again in a bit."},
    "err.generic":     {"uz": "❌ Yuklab olishda xatolik. Keyinroq urinib ko'ring.", "ru": "❌ Ошибка при скачивании. Попробуйте позже.", "en": "❌ Download failed. Please try again later."},
    "err.overloaded":  {"uz": "🔄 Hozir server band. Bir oz kuting.", "ru": "🔄 Сервер перегружен. Попробуйте позже.", "en": "🔄 Server overloaded. Please try again shortly."},
    "err.user_cap":    {"uz": "⏳ Sizda {n} ta faol yuklash bor. Ular tugashini kuting.", "ru": "⏳ У вас уже {n} активных загрузок. Дождитесь их завершения.", "en": "⏳ You already have {n} active downloads. Please wait for them to finish."},
    "err.ig_disabled": {
        "uz": "📷 Instagram yuklab olish hozircha texnik sozlashda. Tez orada ishga tushadi.\n💡 Hozircha YouTube havolalarini yuborishingiz mumkin.",
        "ru": "📷 Скачивание из Instagram пока настраивается. Скоро заработает.\n💡 Пока можно отправлять ссылки YouTube.",
        "en": "📷 Instagram downloads are being set up. Coming soon.\n💡 Meanwhile you can send YouTube links.",
    },
    "err.link_expired":    {"uz": "Havola muddati tugagan. Havolani qaytadan yuboring.", "ru": "Ссылка устарела. Отправьте её ещё раз.", "en": "Link expired. Please send it again."},
    "err.video_not_found": {"uz": "Video topilmadi. Uni qaytadan yuklab oling.", "ru": "Видео не найдено. Скачайте его заново.", "en": "Video not found. Please download it again."},
    "err.video_process":   {"uz": "😔 Videoni qayta ishlab bo'lmadi. Keyinroq urinib ko'ring.", "ru": "😔 Не удалось обработать видео. Попробуйте позже.", "en": "😔 Could not process the video. Please try again later."},
    "err.invalid":         {"uz": "Noto'g'ri tanlov.", "ru": "Неверный выбор.", "en": "Invalid choice."},

    # ── Recognition (Shazam) ─────────────────────────────────────────────────
    "shz.not_found":       {"uz": "😔 Qo'shiqni aniqlab bo'lmadi.\n💡 10–15 soniyalik musiqali qism yuboring.", "ru": "😔 Не удалось распознать песню.\n💡 Попробуйте 10–15-секундный фрагмент.", "en": "😔 Could not recognize the song.\n💡 Try a clear 10–15 second musical clip."},
    "shz.no_lyrics":       {"uz": "📜 Qo'shiq matni topilmadi.", "ru": "📜 Текст песни не найден.", "en": "📜 Lyrics not found."},
    "shz.searching":       {"uz": "🔍 Qidirilmoqda...", "ru": "🔍 Поиск...", "en": "🔍 Searching..."},
    "shz.retry_hint":      {"uz": "🎵 Ovozli xabar, video yoki audio fayl yuboring.", "ru": "🎵 Отправьте голосовое, видео или аудиофайл.", "en": "🎵 Send a voice message, video, or audio file."},
    "shz.file_too_big":    {"uz": "📦 Fayl juda katta. {mb} MB gacha bo'lgan qismini yuboring.", "ru": "📦 Файл слишком большой. Отправьте фрагмент до {mb} МБ.", "en": "📦 File too large. Send a part up to {mb} MB."},
    "shz.unsupported_doc": {"uz": "📎 Bu fayl turi qo'llab-quvvatlanmaydi. Audio yoki video fayl yuboring.", "ru": "📎 Этот тип файла не поддерживается. Отправьте аудио или видео.", "en": "📎 This file type is not supported. Send an audio or video file."},
    "shz.session_expired": {"uz": "Sessiya tugagan. Qaytadan qidiring.", "ru": "Сессия устарела. Повторите поиск.", "en": "Session expired. Please search again."},
    "shz.song_not_found":  {"uz": "😔 Qo'shiq YouTube'da topilmadi.", "ru": "😔 Песня не найдена на YouTube.", "en": "😔 Song not found on YouTube."},
    "shz.song_error":      {"uz": "❌ Qo'shiqni olishda xatolik. Keyinroq urinib ko'ring.", "ru": "❌ Ошибка при получении песни. Попробуйте позже.", "en": "❌ Error fetching the song. Please try again later."},

    # ── Music search ─────────────────────────────────────────────────────────
    "music.searching":   {"uz": "🔍 Qidirilmoqda...", "ru": "🔍 Поиск...", "en": "🔍 Searching..."},
    "music.not_found":   {"uz": "😔 Hech narsa topilmadi. Boshqacha yozing.", "ru": "😔 Ничего не найдено. Попробуйте иначе.", "en": "😔 Nothing found. Try a different spelling."},
    "music.downloading": {"uz": "⏳ Yuklanmoqda...", "ru": "⏳ Скачивается...", "en": "⏳ Downloading..."},
    "music.uploading":   {"uz": "📤 Yuborilmoqda...", "ru": "📤 Отправляется...", "en": "📤 Uploading..."},
    "music.error":       {"uz": "❌ Xatolik. Keyinroq urinib ko'ring.", "ru": "❌ Ошибка. Попробуйте позже.", "en": "❌ Error. Please try again later."},
    "music.expired":     {"uz": "Sessiya tugagan. Qaytadan qidiring.", "ru": "Сессия устарела. Повторите поиск.", "en": "Session expired. Please search again."},
    "music.results":     {"uz": "🎵 <b>Natijalar:</b>", "ru": "🎵 <b>Результаты:</b>", "en": "🎵 <b>Results:</b>"},
    "music.query_short": {"uz": "✏️ Qo'shiq nomini to'liqroq yozing.", "ru": "✏️ Напишите название песни подробнее.", "en": "✏️ Please type a longer song name."},
    "music.unsupported": {
        "uz": "📎 Havola → video, matn → qidiruv, ovozli xabar → qo'shiq aniqlash 🎵",
        "ru": "📎 Ссылка → видео, текст → поиск, голосовое → распознать 🎵",
        "en": "📎 Link → video, text → search, voice → recognize 🎵",
    },

    # ── Start / language / help ──────────────────────────────────────────────
    "start.choose_lang": {
        "uz": "👋 Assalomu alaykum! / Добро пожаловать! / Welcome!\n\nTilni tanlang / Выберите язык / Choose your language:",
        "ru": "👋 Assalomu alaykum! / Добро пожаловать! / Welcome!\n\nTilni tanlang / Выберите язык / Choose your language:",
        "en": "👋 Assalomu alaykum! / Добро пожаловать! / Welcome!\n\nTilni tanlang / Выберите язык / Choose your language:",
    },
    "start.lang_saved":  {"uz": "✅ Til saqlandi!", "ru": "✅ Язык сохранён!", "en": "✅ Language saved!"},
    "start.lang_prompt": {"uz": "🌐 Tilni tanlang:", "ru": "🌐 Выберите язык:", "en": "🌐 Select language:"},
    "help": {
        "uz": (
            "ℹ️ <b>Yuklaydi Bot</b>\n\n"
            "📥 <b>Video yuklash:</b>\n"
            "YouTube, TikTok, Instagram, Facebook, Twitter/X, Pinterest, Vimeo yoki Reddit havolasini yuboring.\n\n"
            "🎵 <b>Qo'shiq topish:</b>\n"
            "Ovozli xabar, video yoki audio faylni yuboring — qo'shiq aniqlanadi.\n\n"
            "🔍 <b>Musiqa qidirish:</b>\n"
            "Qo'shiq nomini yozing — MP3 yuboramiz.\n\n"
            "<i>⚠️ Kontent shaxsiy foydalanish uchun. Platforma shartlariga rioya qiling.</i>"
        ),
        "ru": (
            "ℹ️ <b>Yuklaydi Bot</b>\n\n"
            "📥 <b>Скачать видео:</b>\n"
            "Отправьте ссылку на YouTube, TikTok, Instagram, Facebook, Twitter/X, Pinterest, Vimeo или Reddit.\n\n"
            "🎵 <b>Найти песню:</b>\n"
            "Отправьте голосовое, видео или аудио — определим песню.\n\n"
            "🔍 <b>Поиск музыки:</b>\n"
            "Напишите название песни — пришлём MP3.\n\n"
            "<i>⚠️ Контент для личного использования. Соблюдайте условия платформ.</i>"
        ),
        "en": (
            "ℹ️ <b>Yuklaydi Bot</b>\n\n"
            "📥 <b>Download Video:</b>\n"
            "Send a link from YouTube, TikTok, Instagram, Facebook, Twitter/X, Pinterest, Vimeo or Reddit.\n\n"
            "🎵 <b>Find Song:</b>\n"
            "Send a voice message, video, or audio — we'll identify the song.\n\n"
            "🔍 <b>Music Search:</b>\n"
            "Type a song name — we'll send the MP3.\n\n"
            "<i>⚠️ Content is for personal use. Respect platform terms of service.</i>"
        ),
    },

    # ── Rate limit ───────────────────────────────────────────────────────────
    "throttle":          {"uz": "⏱ Biroz sekinroq! Iltimos, {sec} soniya kuting.", "ru": "⏱ Помедленнее! Подождите {sec} сек.", "en": "⏱ Slow down! Please wait {sec} seconds."},
    "throttle.callback": {"uz": "⏱ Juda tez! Bir oz kuting.", "ru": "⏱ Слишком быстро! Подождите немного.", "en": "⏱ Too fast! Please wait a moment."},

    # ── Required channel ─────────────────────────────────────────────────────
    "channel.join":           {"uz": "📢 Botdan foydalanish uchun kanalga obuna bo'ling:", "ru": "📢 Подпишитесь на канал, чтобы пользоваться ботом:", "en": "📢 Subscribe to the channel to use the bot:"},
    "channel.join_btn":       {"uz": "📢 Kanalga o'tish", "ru": "📢 Перейти в канал", "en": "📢 Join channel"},
    "channel.check_btn":      {"uz": "✅ Obuna bo'ldim", "ru": "✅ Я подписался", "en": "✅ I subscribed"},
    "channel.confirmed":      {"uz": "✅ Obuna tasdiqlandi!", "ru": "✅ Подписка подтверждена!", "en": "✅ Subscription confirmed!"},
    "channel.not_subscribed": {"uz": "❌ Siz hali obuna bo'lmagansiz!", "ru": "❌ Вы ещё не подписались!", "en": "❌ You haven't subscribed yet!"},
    "channel.check_failed":   {"uz": "❌ Tekshirib bo'lmadi. Keyinroq urinib ko'ring.", "ru": "❌ Не удалось проверить. Попробуйте позже.", "en": "❌ Could not check. Please try again later."},

    # ── Global error ─────────────────────────────────────────────────────────
    "error.generic": {"uz": "❌ Xatolik yuz berdi. Iltimos, keyinroq urinib ko'ring.", "ru": "❌ Произошла ошибка. Попробуйте позже.", "en": "❌ An error occurred. Please try again later."},

    # ── Keyboard labels ──────────────────────────────────────────────────────
    "btn.listen":    {"uz": "🎧 Tinglash", "ru": "🎧 Слушать", "en": "🎧 Listen"},
    "btn.lyrics":    {"uz": "📝 Qo'shiq matni", "ru": "📝 Текст песни", "en": "📝 Lyrics"},
    "btn.find_song": {"uz": "🎵 Qo'shiqni top", "ru": "🎵 Найти песню", "en": "🎵 Find song"},
    "btn.mp3":       {"uz": "🎧 Audio sifatida olish", "ru": "🎧 Получить аудио", "en": "🎧 Get as audio"},
    "btn.hd":        {"uz": "🎬 1080p", "ru": "🎬 1080p", "en": "🎬 1080p"},
    "btn.dl_song":   {"uz": "⬇️ MP3 yuklab olish", "ru": "⬇️ Скачать MP3", "en": "⬇️ Download MP3"},
    "btn.retry":     {"uz": "🔁 Qayta", "ru": "🔁 Повторить", "en": "🔁 Retry"},
    "btn.long":      {"uz": "⚠️ uzun", "ru": "⚠️ длинное", "en": "⚠️ long"},

    # ── Bot command menu descriptions ────────────────────────────────────────
    "cmd.start": {"uz": "Botni ishga tushirish", "ru": "Запустить бота", "en": "Start the bot"},
    "cmd.lang":  {"uz": "Tilni o'zgartirish 🌐", "ru": "Сменить язык 🌐", "en": "Change language 🌐"},
    "cmd.help":  {"uz": "Yordam 📖", "ru": "Помощь 📖", "en": "Help 📖"},
}

# Default /start welcome (admin can override per language in the DB).
# {first_name} / {username} are filled by handlers.start._render (escaped).
DEFAULT_WELCOME: dict[str, str] = {
    "uz": (
        "👋 Salom, <b>{first_name}</b>!\n\n"
        "🎬 Havola yuboring — video yuklab beraman.\n"
        "🎵 Qo'shiq nomi yozing — MP3 yuboraman.\n"
        "🎤 Ovozli xabar yuboring — qo'shiqni topib beraman.\n\n"
        "<i>🤖 @vidyuklaydi_bot</i>"
    ),
    "ru": (
        "👋 Привет, <b>{first_name}</b>!\n\n"
        "🎬 Отправьте ссылку — скачаю видео.\n"
        "🎵 Напишите название песни — пришлю MP3.\n"
        "🎤 Отправьте голосовое — найду песню.\n\n"
        "<i>🤖 @vidyuklaydi_bot</i>"
    ),
    "en": (
        "👋 Hello, <b>{first_name}</b>!\n\n"
        "🎬 Send a link — I'll download the video.\n"
        "🎵 Type a song name — I'll send the MP3.\n"
        "🎤 Send a voice message — I'll find the song.\n\n"
        "<i>🤖 @vidyuklaydi_bot</i>"
    ),
}


def t(key: str, lang: str | None = None, **fmt) -> str:
    """Look up *key* in *lang* (fallback: default_lang → en) and format it."""
    entry = T[key]
    text = entry.get(lang or "") or entry.get(settings.default_lang) or entry["en"]
    if "{mb}" in text and "mb" not in fmt:
        fmt["mb"] = settings.max_file_mb
    if "{minutes}" in text and "minutes" not in fmt:
        fmt["minutes"] = settings.max_audio_duration // 60
    return text.format(**fmt) if fmt else text


def error_text(kind: str, lang: str | None, **fmt) -> str:
    """Localized message for a DownloadError.kind (unknown kinds → generic)."""
    key = f"err.{kind}"
    return t(key if key in T else "err.generic", lang, **fmt)
