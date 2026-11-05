# Recon: @IzlaydiBot (Telegram bot, 601,680 users)

Clean-room notes from the user's own Telegram account (Telegram Web A), 2026-11-06.
Flows and layout only. Wording below is paraphrased; the clone writes its own copy.

## Onboarding
- /start (new user) -> one message "choose language" (Russian prompt) with 4 buttons:
  Oʻzbekcha | Ўзбекча | Русский | English (Uzbek Latin AND Uzbek Cyrillic).
- After picking a language -> welcome text: greeting + bullet list of 3 abilities
  (find music by lyrics/title/artist; download Instagram/YouTube/TikTok video and its
  music; find full song from voice/video/audio) + one inline URL button "add to group".
- Bot description ("What can this bot do?") repeats the same three abilities.

## Video link (YouTube watch / Shorts)
- No visible "queued/downloading" message for a cached link; reply arrives within the same minute.
- Reply = video, caption "🍿 <title>" (custom emoji), keyboard:
  - row 1: one button per available quality: "🎬 360p" (already delivered/cached, no badge),
    "🎬 480p (⏳)" (not cached yet -> hourglass badge). Shorts showed 360p + 480p; the 240p
    "Me at the zoo" showed only 360p.
  - row 2: "🎶 Audio".
- Inferred: default delivery is a fast low quality (360p); higher qualities on demand.

## Quality button (480p ⏳)
- The SAME message's caption is edited to "<title>\n\n⏳ Downloading..." and its keyboard removed
  (progress lives in the original message, no new progress message).
- ~20 s later a NEW video message: caption
  "🍿 <code>title</code>\n\n🔗 <source url>\n\n📺 480p\n\n👉 @bot" + one button "💬 add to group".
- Then the original (360p) message is DELETED.

## Audio button
- A soft "subscribe to sponsor channels" photo message appears within ~0.8 s
  ("subscribe once, never asked again", buttons ➕ Subscribe x2, ✅ Confirm). It is NOT a hard gate:
  the audio arrives anyway and the placeholder is deleted afterwards.
- Audio message: title = video title, performer = uploader ("jawed"), duration 0:19,
  caption "👉 @bot", no buttons.

## Text music search
- Plain text "Botir Qodirov Oyning o'n beshi" -> ONE message after ~1.1 s (no "searching..." placeholder):
  header = the query, then numbered list "N. <YouTube title> m:ss" (10 per page),
  buttons 1..10 (two rows of five) + "➡️" (next page).

## Not yet probed (browser froze on low RAM; Telethon login needs the user)
- Instagram reel, TikTok, voice recognition card, picking a search result, /help, groups.

## Build verification (2026-11-06)
- Offline suite: 410 passed (29 new review regressions in tests/test_review_fixes.py).
- Live, through the real bot and workers: e2e 6/6 PASS, stress 5/5 PASS (3 parallel YouTube downloads in 52 s, cache hit 0.9 ms).
- 360p now selects YouTube format 18 (one H.264+AAC file, no ffmpeg merge). Warm download of a 28.5 MB video: 7.1-8.8 s vs 8.5-17.3 s for the 134+140 merge.
- android_vr as a fast extractor was measured and rejected. It is not faster here (8.8-10.5 s) and gets HTTP 403 on stream URLs, which forces the slow retry.
- Remaining speed lever: extraction (~7-9 s per fresh YouTube link). Reusing a YoutubeDL instance measured 4.2-6.3 s. It needs per-thread instances, because yt-dlp is not thread-safe.
