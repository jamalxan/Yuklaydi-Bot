# Yuklaydi Bot — Kod auditi va Texnik topshiriq (TZ)

| | |
|---|---|
| **Loyiha** | `@vidyuklaydi_bot` (Yuklaydi-Bot) |
| **Hujjat turi** | Kod auditi natijasi + tuzatish bo'yicha Texnik topshiriq |
| **Sana** | 2026-11-04 |
| **Ko'rib chiqilgan commit** | `8b2ace3` (master) |
| **Qamrov** | `bot/` (barcha modullar), `docker-compose*.yml`, `Dockerfile`, `requirements.txt`, `.env.example`, `tests/` |
| **Testlar holati** | `pytest` — **157 passed** (22.5 s). Testlar o'tadi, lekin quyidagi xatoliklarning aksariyatini qamramaydi |

---

## 0. Umumiy xulosa

Loyiha arxitekturasi to'g'ri tanlangan: bot jarayoni yengil, og'ir ishlar arq navbati orqali uchta alohida
worker'ga (yuklash / aniqlash / musiqa) uzatiladi. Redis'da file_id keshi, single-flight, backpressure bor.
Ammo audit **3 ta kritik**, **11 ta yuqori**, **20 dan ortiq o'rta/past** darajali muammoni aniqladi. Eng
jiddiylari:

1. **SSRF** — platformani aniqlash URL ichidagi istalgan joyda substring qidiradi; `http://127.0.0.1:8081/x.com`
   "twitter" deb qabul qilinadi va yt-dlp ichki manzilga so'rov yuboradi (amalda tekshirildi).
2. **Ban ishlamaydi** — `is_blocked` faqat DB'ga yoziladi, hech qayerda tekshirilmaydi.
3. **Disk tozalash hech qachon ishlamaydi** — `cron_jobs = []`; agar yoqilsa ham `cookies.txt`ni o'chiradi.

**Xulosa:** production'ga chiqarishdan oldin 1-bosqich (Kritik + Yuqori) tuzatilishi shart.

### Darajalar

| Daraja | Ma'nosi |
|---|---|
| 🔴 **Kritik** | Xavfsizlik teshigi, ma'lumot yo'qolishi yoki asosiy funksiya ishlamasligi. Darhol tuzatish |
| 🟠 **Yuqori** | Foydalanuvchiga ko'rinadigan xato, noto'g'ri statistika, DoS xavfi. Joriy sprintda |
| 🟡 **O'rta** | Barqarorlik, unumdorlik, UX kamchiligi. Keyingi sprintda |
| ⚪ **Past** | Kod sifati, tozalik, hujjat |

Har bir topilma yonida holat ko'rsatilgan: **[TASDIQLANGAN]** — kod/ishga tushirish orqali isbotlangan;
**[EHTIMOLIY]** — kod mantiqidan kelib chiqadi, real muhitda tekshirish kerak.

---

## 1. Tizim tavsifi (joriy holat)

### 1.1 Komponentlar

```
Telegram ⇄ (ixtiyoriy) Local Bot API :8081
              │
        bot (aiogram 3, polling)  ── middlewares: Throttling → Db → UserLang → ChannelCheck
              │  routers: errors → start → admin → download → shazam → music
              ▼
        Redis (FSM, kesh, rate-limit, arq navbatlari)
              │
   ┌──────────┼───────────────────────────┐
   ▼          ▼                           ▼
worker     worker-recognition          worker-music
arq:queue  arq:queue:recognition       arq:queue:music
download_task  recognize_task          music_download_task
              │
        PostgreSQL (+ PgBouncer)
```

### 1.2 Funksiyalar ro'yxati

| Kod | Funksiya | Kirish nuqtasi | Fon vazifasi |
|---|---|---|---|
| F1 | `/start`, til tanlash | `start.py: cmd_start, cb_lang_select` | — |
| F2 | `/help`, `/lang` | `start.py: cmd_help, cmd_lang` | — |
| F3 | Havola orqali video yuklash | `download.py: handle_url` | `download_task` |
| F4 | MP3 sifatida qayta yuklash (`vdalt:`) | `download.py: cb_video_alt` | `download_task` |
| F5 | Video ichidagi qo'shiqni topish (`vshz:`) | `download.py: cb_video_shazam` | `recognize_task` |
| F6 | Ovoz/video/audio orqali qo'shiq aniqlash | `shazam.py: handle_media` | `recognize_task` |
| F7 | Matn bo'yicha musiqa qidirish | `music.py: handle_music_search, cb_music_page` | — |
| F8 | Tanlangan qo'shiqni MP3 yuklash | `music.py: cb_music_pick`, `shazam.py: cb_download_song` | `music_download_task` |
| F9 | Qo'shiq matni (lyrics) | `shazam.py: cb_lyrics` | — |
| F10 | Majburiy kanalga obuna | `middlewares/channel.py`, `admin.py: cb_channel_check` | — |
| F11 | Rate limit | `middlewares/throttling.py` | — |
| F12 | Admin panel (statistika, broadcast, welcome, export, ban, kanal) | `admin.py` | — |
| F13 | Navbat, kesh, single-flight, foydalanuvchi limiti | `services/cache.py`, `worker/tasks.py` | — |
| F14 | Ma'lumotlar bazasi va statistika | `db/*` | — |
| F15 | Log va xatolarni qayta ishlash | `logger.py`, `handlers/errors.py` | — |
| F16 | Infratuzilma / deploy | `docker-compose*.yml`, `Dockerfile` | — |

---

## 2. Topilmalar jadvali (qisqa)

| ID | Daraja | Funksiya | Qisqa tavsif | Fayl |
|---|---|---|---|---|
| BUG-01 | 🔴 | F3 | SSRF: platforma tekshiruvi substring bo'yicha, ichki manzillar o'tib ketadi | `services/downloader.py:18-61` |
| BUG-02 | 🔴 | F12 | Ban hech qayerda tekshirilmaydi | `db/models.py:24`, `main.py:108-116` |
| BUG-03 | 🔴 | F13/F16 | Disk tozalash rejalashtirilmagan; yoqilsa `cookies.txt`ni o'chiradi | `worker/tasks.py:653`, `services/media.py:156` |
| BUG-04 | 🟠 | F1/F14 | Foydalanuvchi faqat til tugmasini bossa DB'ga yoziladi | `handlers/start.py:115` |
| BUG-05 | 🟠 | F3/F4/F8 | `per_user_download_cap` amalda qo'llanmaydi; callback'lar throttling'dan tashqarida | `download.py:127`, `throttling.py:19` |
| BUG-06 | 🟠 | F7/F9/F6/F10/F12 | HTML escape yo'q: `<`, `>`, `&` bor nomlar xabarni buzadi | `keyboards/inline.py:104` va b. |
| BUG-07 | 🟠 | F12/F1 | Admin noto'g'ri HTML'li welcome saqlasa — `/start` hammaga buziladi | `admin.py:401-449`, `start.py:105` |
| BUG-08 | 🟠 | F3 | Fayl hajmi yuklab bo'lingandan keyin tekshiriladi (`max_filesize` yo'q) | `downloader.py:165`, `tasks.py:273` |
| BUG-09 | 🟠 | F8 | Musiqa yuklashda hajm/davomiylik cheklovi yo'q | `tasks.py:554-596` |
| BUG-10 | 🟠 | F12 | Broadcast: `RetryAfter` qayta urinilmaydi, bloklaganlar belgilanmaydi, jarayon xotirada | `admin.py:292-351` |
| BUG-11 | 🟠 | F16 | Prod compose: parol va Redis sozlamalari bir-biriga mos emas | `docker-compose.prod.yml` |
| BUG-12 | 🟠 | F14/F12 | "Kesh (bugun)" har doim 0; kunlik raqamlar UTC/lokal aralash | `repo.py:83-159`, `tasks.py:348` |
| BUG-13 | 🟠 | F11/F2/F15 | Throttling i18n'dan oldin — rate-limit xabari doim default tilda | `main.py:111-115` |
| BUG-14 | 🟠 | F3/F6/F8 | Job timeout'da foydalanuvchi "Yuklanmoqda…" holatida qoladi | `tasks.py:204-391` |
| BUG-15 | 🟡 | F10 | Kanal tekshiruvi callback'larda ishlamaydi; tasdiq xabari doim inglizcha | `main.py:116`, `admin.py:685,690` |
| BUG-16 | 🟡 | F6 | Har qanday hujjat (PDF, ZIP) aniqlashga yuboriladi; 20 MB limit haqida xabar yo'q | `shazam.py:44-46`, `tasks.py:400` |
| BUG-17 | 🟡 | F7 | Qidiruv keshi yoziladi, lekin o'qilmaydi; yt-dlp qidiruvi bot jarayonida | `music.py:57`, `cache.py:108` |
| BUG-18 | 🟡 | F12 | `message.text` `None` bo'lsa ban/kanal FSM qulaydi | `admin.py:546,617` |
| BUG-19 | 🟡 | F10 | Kanalda bot admin ekanligi tekshirilmaydi → majburiy obuna jimgina o'chadi | `admin.py:612-638`, `channel.py:70-74` |
| BUG-20 | 🟡 | F13 | `ucap` TTL (300 s) `job_timeout`dan qisqa bo'lishi mumkin; single-flight kutish budjeti qisqa | `cache.py:185`, `tasks.py:244` |
| BUG-21 | 🟡 | F15 | Global xato xabari doim inglizcha; log formati JSON'ni buzadi | `errors.py:22`, `logger.py:14` |
| BUG-22 | 🟡 | F2/F3/F9 | Ko'p joyda qattiq yozilgan inglizcha matnlar; ruscha matnda lotin harflari | `shazam.py:90,131,146,167` va b. |
| BUG-23 | 🟡 | F14 | `get_or_create` poyga holati (IntegrityError) | `repo.py:21-29` |
| BUG-24 | 🟡 | F13 | `safe_delete` event loop'ni `time.sleep` bilan bloklaydi | `media.py:149` |
| BUG-25 | 🟡 | F8/F7 | Musiqa yuklash/qidiruvida cookie va JS-runtime sozlamalari video yuklashdan farq qiladi | `music_search.py:60-110` |
| BUG-26 | 🟡 | F6 | Qisqa kliplarda bir xil oyna 3 marta Shazam'ga yuboriladi | `recognizer.py:92-103` |
| BUG-27 | 🟡 | F11/F1 | Redis ishlamasa butun bot to'xtaydi (fail-open yo'q); til keshi qayta to'ldirilmaydi | `i18n.py:36-44`, `throttling.py:26` |
| BUG-28 | 🟡 | Hammasi | Guruh chatlarida chat turi filtrlanmagan | barcha router'lar |
| BUG-29 | 🟡 | F16 | Konteyner root'da, 8081 port ochiq, PgBouncer healthcheck doim "healthy" | `docker-compose.yml`, `Dockerfile` |
| BUG-30 | ⚪ | — | O'lik kod va ishlatilmagan bog'liqliklar | `keyboards/reply.py`, `locales/`, `requirements.txt` |
| BUG-31 | ⚪ | — | `assert` biznes-mantiqda; `asyncio.get_event_loop()` eskirgan; `datetime.utcnow()` eskirgan | bir nechta |
| BUG-32 | ⚪ | F14 | Alembic bor, lekin ishlatilmaydi; `create_all` + qo'lda ALTER | `main.py:77-83`, `db/migrations.py` |

---

## 3. Funksiyalar bo'yicha Texnik topshiriq

Har bir bo'lim formati: **Joriy holat → Muammo(lar) → Talab(lar) → Qabul mezonlari**.

---

### F3. Havola orqali video yuklash

#### BUG-01 🔴 SSRF — platforma tekshiruvi aylanib o'tiladi [TASDIQLANGAN]

**Joriy holat.** `detect_platform()` (`bot/services/downloader.py:56-61`) URL'ning **butun matnida**
`re.search(r"x\.com", url)` kabi qidiradi. Kommentariyda (`download.py:88-90`) bu SSRF'dan himoya
ekanligi aytilgan, lekin himoya ishlamaydi.

Amaliy tekshiruv natijasi:

```
http://169.254.169.254/latest?x=youtube.com -> youtube
http://127.0.0.1:8081/x.com                 -> twitter
https://www.netflix.com/title/1             -> twitter   ("netfli[x.com]")
https://microsoft.com/a                     -> twitter   ("microsof[t.co]m")
```

Bunday URL yt-dlp'ning generic extractor'iga tushadi va worker ichki tarmoqdagi manzilga (Local Bot API,
Redis HTTP, cloud metadata `169.254.169.254`) so'rov yuboradi.

**Talablar.**
1. `detect_platform()` faqat `urlparse(url).hostname` bilan ishlashi kerak (kichik harfga o'tkazilgan).
2. Moslik **domen chegarasi** bo'yicha: `host == d or host.endswith("." + d)`. Regex substring taqiqlanadi.
3. Ruxsat etilgan domenlar ro'yxati aniq:
   `youtube.com, youtu.be, youtube-nocookie.com, tiktok.com, instagram.com, instagr.am, facebook.com,
   fb.watch, fb.com, twitter.com, x.com, t.co, pinterest.com, pin.it, vimeo.com, reddit.com, redd.it`.
4. Sxema faqat `http`/`https`; userinfo (`user@host`) va IP-literal host rad etiladi; port faqat
   bo'sh, 80 yoki 443.
5. Qo'shimcha himoya qatlami (defense-in-depth): worker'da yt-dlp'ga
   `allowed_extractors` opsiyasi orqali faqat platformaga mos extractor'lar ruxsat etiladi
   (`["youtube", "youtube:tab", "TikTok", "Instagram", "Facebook", "Twitter", "Pinterest", "Vimeo", "Reddit"]`
   va ularning variantlari), `generic` extractor o'chiriladi.
6. Qisqartirilgan havolalar (`t.co`, `pin.it`, `vm.tiktok.com`, `fb.watch`) redirect'dan keyin ham
   yakuniy host ruxsat ro'yxatida bo'lishi tekshiriladi (5-band buni yt-dlp darajasida ta'minlaydi).

**Qabul mezonlari.**
- Yuqoridagi 4 ta URL uchun `detect_platform()` → `None`, bot "❌ Ushbu havola qo'llab-quvvatlanmaydi" javobini beradi.
- `https://m.youtube.com/...`, `https://www.tiktok.com/...`, `https://x.com/...` → to'g'ri platforma.
- `tests/test_security.py`ga parametrlangan test qo'shiladi (kamida 15 ta ijobiy va 15 ta salbiy holat).

#### BUG-08 🟠 Fayl hajmi yuklab bo'lingandan keyin tekshiriladi [TASDIQLANGAN]

**Joriy holat.** `_build_opts()` (`downloader.py:165-222`) da `max_filesize` yo'q. Hajm faqat
`tasks.py:273` da, fayl to'liq diskka tushgandan keyin tekshiriladi. 2 soatlik 720p video bir necha GB
bo'lishi, worker vaqtini (300 s) va diskni band qilishi mumkin.

**Talablar.**
1. yt-dlp opsiyalariga `max_filesize = settings.max_file_bytes` qo'shilsin.
2. Format selektoriga `[filesize<?{N}]` yoki `[filesize_approx<?{N}]` filtrlari qo'shilsin (N = limit).
3. `max_file_mb` qiymati `use_local_api=False` bo'lsa avtomatik **50** ga cheklansin (`config.py`da
   validator). `.env.example`dagi `MAX_FILE_MB=2000` faqat Local API bilan ishlashi izohda aytilsin.
4. Limitdan oshgan holat `DownloadError(kind="too_large")` sifatida qaytsin, foydalanuvchiga `too_large`
   xabari ko'rsatilsin.

**Qabul mezonlari.** 50 MB limit bilan 1 GB'lik video so'ralganda worker 10 s ichida `too_large`
xabarini beradi, diskda 5 MB'dan katta qoldiq fayl qolmaydi.

#### BUG-14 🟠 Job timeout'da progress xabari osilib qoladi [EHTIMOLIY]

**Joriy holat.** arq `job_timeout` oshganda vazifani `asyncio.CancelledError` bilan bekor qiladi.
`download_task`, `recognize_task`, `music_download_task` faqat `Exception`ni ushlaydi; `finally` ishlaydi,
lekin foydalanuvchi xabari "⏳ Yuklanmoqda..." holatida qoladi.

**Talablar.**
1. Har bir task tanasini `asyncio.timeout(settings.job_timeout - 15)` bilan o'rash; `TimeoutError`da
   foydalanuvchiga `timeout` xabari yuborish.
2. `except asyncio.CancelledError:` blokida xabarni yangilashga harakat qilib, so'ng `raise`.

**Qabul mezonlari.** `JOB_TIMEOUT=20` bilan sekin video yuborilganda foydalanuvchi 20 s ichida aniq
xato xabarini oladi.

#### F3 bo'yicha boshqa kamchiliklar

| # | Daraja | Muammo | Talab |
|---|---|---|---|
| F3-a | 🟡 | `vdalt:` callback'dagi `quality` oq ro'yxat bilan tekshirilmaydi (`download.py:152`) — soxta callback kesh kalitlarini ifloslantiradi | `quality in {"720", "1080", "mp3"}` bo'lmasa rad etish |
| F3-b | 🟡 | `cb_video_alt`da backpressure (`max_queue_depth`) tekshiruvi yo'q | `handle_url`dagi kabi navbat chuqurligini tekshirish |
| F3-c | 🟡 | `get_queue_depth` faqat `arq:queue`ni o'lchaydi; music/recognition navbatlari cheksiz | Har bir enqueue nuqtasida tegishli navbat chuqurligi tekshirilsin |
| F3-d | ⚪ | `platform or "unknown"` (`download.py:135`) — `platform` bu yerda hech qachon `None` emas | O'chirish |
| F3-e | ⚪ | Keshdan `vdalt` javobida (`download.py:162-168`) klaviatura yo'q, oddiy yuklashda bor — nomuvofiq | Bir xil UX |
| F3-f | 🟡 | Instagram rasm fallback'i (`tasks.py:40-83`): aiohttp'da timeout yo'q, rasmlar bittadan yuboriladi | `ClientTimeout(total=20)`, `send_media_group` |
| F3-g | 🟡 | `_download_sync` qidiruvida (`downloader.py:273-282`) `.part`/`.ytdl` fayl tanlanishi mumkin [EHTIMOLIY] | `.part`, `.ytdl`, `.temp` kengaytmalarini chiqarib tashlash; afzalroq — `info["requested_downloads"][0]["filepath"]`dan yo'lni olish |

---

### F4. MP3 sifatida qayta yuklash

Asosiy muammolar F3 va BUG-05 da. Qo'shimcha:

- **Talab:** MP3 uchun ham davomiylik cheklovi bo'lsin (BUG-09 bilan bir xil), aks holda 3 soatlik video
  192 kbps MP3 ga o'giriladi (~250 MB) va yuborilmaydi.
- **Talab:** keshdan yuborilgan MP3 (`tasks.py:191-193`) ham `_flood_retry` ichida (hozir shunday) va
  `caption` bir xil formatda bo'lsin.

---

### F5 / F6. Qo'shiqni aniqlash (Shazam)

#### BUG-16 🟡 Istalgan hujjat aniqlashga yuboriladi; 20 MB limit haqida xabar yo'q [TASDIQLANGAN]

**Joriy holat.** `shazam.py:44-46`: har qanday `document` (PDF, ZIP, APK) `.bin` sifatida navbatga
qo'yiladi va ffmpeg/ffprobe orqali ishlanadi. Cloud API'da `getFile` 20 MB'dan katta fayllarni
bermaydi (`tasks.py:400`), foydalanuvchi esa "qo'shiq aniqlanmadi" degan **noto'g'ri** xabarni oladi.

**Talablar.**
1. `document` faqat `mime_type` `audio/*` yoki `video/*` bo'lsa qabul qilinsin; aks holda
   `unsupported` xabari.
2. `file_size > 20 MB` va `use_local_api=False` bo'lsa, navbatga qo'ymasdan aniq xabar:
   "📦 Fayl juda katta. 20 MB gacha bo'lgan qismini yuboring."
3. Fayl kengaytmasi `mime_type`dan aniqlansin (`.bin` o'rniga).

**Qabul mezonlari.** PDF yuborilganda bot uni navbatga qo'ymaydi; 30 MB video uchun hajm xabari chiqadi.

#### BUG-26 🟡 Qisqa kliplarda takroriy so'rovlar [TASDIQLANGAN]

`recognizer.py:92-103`: 10 s'lik ovozli xabar uchun uchala "oyna" ham `start=0, duration≈10` ga teng —
bitta audio Shazam'ga 3 marta (retry bilan 6 martagacha) yuboriladi.

**Talab.** Oynalar ro'yxati `total_dur` asosida hisoblanib, `(start, duration)` juftligi bo'yicha
takrorlar olib tashlansin. `total_dur <= 15` bo'lsa — faqat bitta urinish.

#### F5/F6 bo'yicha boshqa kamchiliklar

| # | Daraja | Muammo | Talab |
|---|---|---|---|
| F6-a | 🟠 | Natija kartasida `title`/`artist` HTML escape qilinmagan (`tasks.py:498`) | BUG-06 ga qarang |
| F6-b | 🟡 | yt-dlp fallback (`tasks.py:416-432`) semafor tashqarisida va to'liq MP3 transkod qiladi | `_get_yt_dlp_sem()` ichida; transkodsiz `bestaudio` + `download_sections` bilan faqat 60 s olish |
| F6-c | 🟡 | `temp_path.replace(file_suffix, ".m4a")` — katalog nomida shu qator bo'lsa noto'g'ri yo'l | `os.path.splitext` ishlatish |
| F6-d | ⚪ | `cb_video_shazam` xatosida inglizcha "😔 Could not process video." (`download.py:222`) | Lokalizatsiya |

---

### F7. Matn bo'yicha musiqa qidirish

#### BUG-17 🟡 Qidiruv keshi ishlatilmaydi; yt-dlp bot jarayonida ishlaydi [TASDIQLANGAN]

**Joriy holat.**
- `handle_music_search` (`music.py:57`) har safar `search_songs(query, 30)` chaqiradi. `cache.get_music_search(query)`
  mavjud (`cache.py:108`), lekin hech qayerda chaqirilmaydi — bir xil so'rov har safar YouTube'ga ketadi.
- Qidiruv bot jarayonining default thread pool'ida ishlaydi; ommaviy so'rovlarda polling sekinlashadi.
- `cb_download_song` (`shazam.py:144`) ham bot jarayonida yt-dlp qidiruvi qiladi.
- Har qanday matn ("salom", "ok", emoji) YouTube qidiruviga aylanadi.

**Talablar.**
1. Qidiruvdan oldin `get_music_search(query)` tekshirilsin; topilsa YouTube'ga murojaat qilinmasin.
2. Minimal so'rov uzunligi — 2 belgi, maksimal — 100 belgi; undan uzun matn kesilsin.
3. Qidiruv uchun alohida cheklangan executor (`ThreadPoolExecutor(max_workers=N)`, N konfiguratsiyadan)
   yoki qidiruv ham arq navbatiga o'tkazilsin.
4. Natija ro'yxatida davomiyligi > 15 daqiqa bo'lgan yozuvlar (mix, albom) belgilansin yoki filtrlansin.

**Qabul mezonlari.** Bir xil so'rov 15 daqiqa ichida ikkinchi marta yuborilganda yt-dlp chaqirilmaydi
(log/mock orqali tekshiriladi).

#### F7 bo'yicha boshqa kamchiliklar

| # | Daraja | Muammo | Talab |
|---|---|---|---|
| F7-a | 🟠 | Natijalar ro'yxatida `uploader`/`title` escape qilinmagan (`inline.py:104`) — `<`/`&` bor nom xabarni buzadi, foydalanuvchi "Xatolik" oladi | BUG-06 |
| F7-b | 🟡 | `music.py:143` — "Invalid" inglizcha | Lokalizatsiya |
| F7-c | ⚪ | `_search_sync` cookie/extractor_args ishlatmaydi (BUG-25) | Umumiy opsiya quruvchi |

---

### F8. Tanlangan qo'shiqni MP3 yuklash

#### BUG-09 🟠 Hajm va davomiylik cheklovi yo'q [TASDIQLANGAN]

**Joriy holat.** `music_download_task` (`tasks.py:523-632`) fayl hajmini tekshirmaydi; `download_audio`
(`music_search.py:84-116`) `max_filesize`siz. 1 soatlik "mix" tanlansa ~85 MB MP3 hosil bo'ladi,
cloud API'da upload xato bilan tugaydi, foydalanuvchi "❌ Xatolik." oladi.

**Talablar.**
1. `duration > settings.max_audio_duration` (yangi sozlama, default 1200 s) bo'lsa — navbatga qo'yishdan
   **oldin** handler'da rad etish va aniq xabar.
2. Yuklangandan keyin `os.path.getsize() > max_file_bytes` → `too_large` xabari.
3. `download_audio` haqiqiy chiqish fayl yo'lini qaytarsin (hozir taxminiy `output_path` qaytaradi).
4. Thumbnail yuklash (`tasks.py:568`) uchun `ClientTimeout(total=10)` va 200 KB cheklov; Telegram talabi —
   JPEG, ≤320 px, ≤200 KB. Mos kelmasa — thumbnail yuborilmasin.
5. Keshdan yuborish (`tasks.py:546`) ham `_flood_retry` orqali bo'lsin.

**Qabul mezonlari.** 60 daqiqalik video tanlanganda foydalanuvchi 2 s ichida "juda uzun" xabarini oladi,
worker'ga vazifa tushmaydi.

#### BUG-25 🟡 Opsiyalar nomuvofiqligi [TASDIQLANGAN]

`_download_audio_sync` va `_search_sync`da `cookiesfrombrowser`, `js_runtimes`, `remote_components`
yo'q (`downloader._build_opts`da bor). Natijada musiqa yuklash YouTube bot-check'iga tez-tez tushadi.

**Talab.** Barcha yt-dlp chaqiruvlari uchun yagona `build_base_opts()` funksiyasi yaratilsin
(`_apply_cookies`, `extractor_args`, `js_runtimes`, timeout'lar bir joyda).

---

### F9. Qo'shiq matni (lyrics)

| # | Daraja | Muammo | Talab | Qabul mezoni |
|---|---|---|---|---|
| F9-a | 🟠 | `cb_lyrics` (`shazam.py:105-108`) sarlavha va **matnning o'zini** `parse_mode=HTML` bilan escape'siz yuboradi | `html.escape(title)`, `html.escape(artist)`, `html.escape(page)` | `<3` yoki `R&B` bor matn to'g'ri ko'rsatiladi |
| F9-b | 🟡 | "Session expired…" inglizcha (`shazam.py:90,131`), "😔 Song not found on YouTube." (`:146`), "❌ Error fetching song." (`:167`) | `_MSGS`ga 3 tilda qo'shish | Har uch tilda to'g'ri |
| F9-c | 🟡 | Har safar yangi `aiohttp.ClientSession` va `lyricsgenius.Genius` obyekti yaratiladi (`lyrics.py:60,95,141,155`) | Modul darajasida bitta sessiya (lazy) | — |
| F9-d | ⚪ | Escape'dan keyin sahifa 4096 belgidan oshishi mumkin | Pagination escape'dan **keyin** yoki `LYRICS_PAGE_SIZE=3000` + sarlavha < 4096 ekanini kafolatlash | — |

---

### F10. Majburiy kanalga obuna

#### BUG-15 🟡 Callback'lar tekshirilmaydi; tasdiq xabari doim inglizcha [TASDIQLANGAN]

**Joriy holat.**
- `ChannelCheckMiddleware` faqat `dp.message`ga ulangan (`main.py:116`). Obuna bo'lmagan foydalanuvchi
  eski xabardagi tugmalar orqali (`ms:`, `vdalt:`, `dlsong:`) bot funksiyalaridan foydalana oladi.
- `cb_channel_check` (`admin.py:685,690`) `{...}.get("en")` — til doim inglizcha.
- Kanal sarlavhasi (`channel.py:104`) va admin javobidagi `chat.title` (`admin.py:633`) escape qilinmagan.
- `_SKIP_COMMANDS` `startswith` bilan tekshiriladi — `/startxyz` ham o'tadi.

**Talablar.**
1. Middleware `dp.callback_query`ga ham ulansin; `channel:check`, `lang:*`, `adm:*` callback'lari istisno.
2. `cb_channel_check` `user_lang` parametrini olsin va mos tilda javob bersin.
3. Barcha `ch_title`/`chat.title` — `html.escape`.
4. Komandalar `message.text.split()[0].split("@")[0]` bo'yicha aniq solishtirilsin.

#### BUG-19 🟡 Bot kanalda admin ekanligi tekshirilmaydi [EHTIMOLIY]

`fsm_channel_set` faqat `get_chat()` chaqiradi; ochiq kanal uchun bu bot admin bo'lmasa ham ishlaydi.
Keyin `get_chat_member` xato beradi va middleware **fail-open** (`channel.py:73-74`) — majburiy obuna
jimgina o'chadi. Yopiq kanal uchun `invite` bo'sh qoladi va foydalanuvchiga "Kanalga o'tish" tugmasi
chiqmaydi.

**Talablar.**
1. Saqlashdan oldin `get_chat_member(chat.id, bot.id)` → status `administrator` bo'lishi tekshirilsin;
   bo'lmasa saqlanmasin va admin'ga aniq xabar berilsin.
2. `chat.username` yo'q bo'lsa `create_chat_invite_link()` orqali havola yaratilsin yoki admin'dan
   so'ralsin.

---

### F11. Rate limit

#### BUG-13 🟠 Rate-limit xabari doim default tilda [TASDIQLANGAN]

`ThrottlingMiddleware` (`main.py:111`) `UserLangMiddleware`dan (`main.py:114`) **oldin** ishlaydi,
shuning uchun `data["user_lang"]` hali yo'q va har doim `default_lang` ishlatiladi.

**Talab.** Throttling ichida tilni Redis'dan (`get_user_lang`) arzon o'qish, yoki throttling'ni
UserLang'dan keyinga ko'chirish (UserLang DB'ga faqat kesh miss'da murojaat qiladi).

#### BUG-05 🟠 Per-user limit va callback throttling yo'q [TASDIQLANGAN]

**Joriy holat.**
- `per_user_download_cap` faqat "Navbatda..." / "Yuklanmoqda..." matnini tanlash uchun ishlatiladi
  (`download.py:127`). Hech qanday rad etish yo'q.
- `ThrottlingMiddleware` `Message` bo'lmagan hodisalarni o'tkazib yuboradi (`throttling.py:19`).
  Foydalanuvchi "🎧 Audio sifatida olish" yoki raqam tugmasini sekundiga o'nlab marta bossa, har biri
  alohida arq vazifasi bo'ladi.

**Talablar.**
1. Enqueue'dan oldin `get_active_downloads(user_id) >= per_user_download_cap` bo'lsa rad etish va
   "⏳ Sizda N ta faol yuklash bor, tugashini kuting" xabari. Slot **enqueue paytida** band qilinsin
   (hozir worker'da band qilinadi — navbatda turgan vazifalar hisobga olinmaydi).
2. Callback'lar uchun alohida, yumshoqroq rate-limit (masalan 30/min) — `dp.callback_query.middleware`.
3. Bir xil `(user_id, callback_data)` 3 s ichida takrorlansa — e'tiborsiz qoldirish (debounce).

**Qabul mezonlari.** Bir foydalanuvchi 10 ta havola ketma-ket yuborsa, faqat 3 tasi navbatga tushadi.

---

### F12. Admin panel

#### BUG-02 🔴 Ban ishlamaydi [TASDIQLANGAN]

**Joriy holat.** `set_blocked()` `users.is_blocked`ni yangilaydi, lekin `is_blocked` faqat
`get_all_ids()` (broadcast) va CSV eksportida o'qiladi. Hech bir middleware yoki handler bloklangan
foydalanuvchini to'xtatmaydi. Bundan tashqari, ban faqat DB'da mavjud foydalanuvchiga qo'llanadi
(BUG-04 tufayli ko'p foydalanuvchi DB'da yo'q).

**Talablar.**
1. Yangi `BanMiddleware` (`dp.update.outer_middleware`) — Redis'da `ban:{tg_id}` kalitini tekshiradi
   (TTL'siz yoki uzoq TTL); bloklangan foydalanuvchi hodisalari jimgina tashlab yuboriladi.
2. `set_blocked()` DB bilan birga Redis kalitini yozadi/o'chiradi. Bot ishga tushganda barcha
   bloklanganlar Redis'ga yuklanadi.
3. Ban DB'da yo'q ID uchun ham ishlasin (`upsert` bilan foydalanuvchi yozuvi yaratiladi).
4. Admin ID'lar ban qilinmasin.

**Qabul mezonlari.** Ban qilingan foydalanuvchi havola, matn, ovoz yoki callback yuborganda bot javob
bermaydi va navbatga vazifa tushmaydi; unban'dan keyin darhol ishlaydi.

#### BUG-07 🟠 Noto'g'ri HTML'li welcome `/start`ni buzadi [EHTIMOLIY]

**Joriy holat.**
- `fsm_welcome_new_text` (`admin.py:406`) `message.text`ni oladi — admin Telegram'da qalin/kursiv
  qilib yozgan formatlash yo'qoladi; qo'lda yozilgan `<b>` esa tekshirilmaydi.
- Preview (`admin.py:416`) noto'g'ri HTML bo'lsa `TelegramBadRequest` → global xato; lekin saqlash
  tugmasi oldingi qadamda emas, shuning uchun bu holat ko'pincha to'xtaydi. Biroq "yarim to'g'ri" HTML
  (masalan `<b>` yopilmagan) saqlanib qolsa, `cmd_start` (`start.py:105`) **har bir foydalanuvchi**
  uchun xato beradi.
- Joriy matn `<code>{current}</code>` ichida escape'siz ko'rsatiladi (`admin.py:392`) — default
  matnda `<b>` bor, Telegram `code` ichida teglarni qabul qilmaydi → tahrirlash ekrani ochilmasligi mumkin.

**Talablar.**
1. Kiruvchi matn `message.html_text` orqali olinsin (Telegram formatlash HTML'ga o'giriladi).
2. Saqlashdan oldin matn admin'ning o'ziga `parse_mode=HTML` bilan sinov sifatida yuborilsin; xato
   bo'lsa — saqlanmasin, xato sababi ko'rsatilsin.
3. Joriy matn `html.escape(current)` bilan ko'rsatilsin.
4. `cmd_start`da yuborish xato bersa, default matnga fallback qilinsin.

#### BUG-10 🟠 Broadcast ishonchsiz [TASDIQLANGAN]

**Joriy holat** (`admin.py:292-351`).
- Butun yuborish handler ichida, bitta korutinada; bot qayta ishga tushsa jarayon yo'qoladi, natija
  noma'lum.
- `TelegramRetryAfter` umumiy `except Exception`ga tushadi va "muvaffaqiyatsiz" deb hisoblanadi — xabar
  qayta yuborilmaydi.
- `TelegramForbiddenError` (bot bloklangan) bo'lganda foydalanuvchi belgilanmaydi — har bir broadcast
  ularga qayta urinadi.
- Jarayon davomida admin'ga progress ko'rsatilmaydi; 100 000 foydalanuvchida ~67 daqiqa "📤 Yuborilmoqda...".
- Ikki admin bir vaqtda broadcast boshlashi mumkin.

**Talablar.**
1. Broadcast arq'dagi alohida vazifa (`broadcast_task`) sifatida ishlasin; holat (offset, delivered,
   failed) Redis'da saqlanib, qayta ishga tushganda davom ettirilsin.
2. `RetryAfter` → `retry_after` soniya kutib, **o'sha** foydalanuvchiga qayta urinish.
3. `Forbidden` → `users.is_blocked_bot = true` (yangi ustun, `is_blocked`dan farqli) va keyingi
   broadcast'larda o'tkazib yuborish.
4. Har 500 xabarda admin xabarini progress bilan yangilash.
5. Global lock (`SET broadcast:lock NX`) — bir vaqtda bitta broadcast.
6. Foydalanuvchi ID'lari `yield_per(1000)` bilan qismlab o'qilsin.

#### BUG-18 🟡 FSM'da `message.text = None` [TASDIQLANGAN]

`fsm_ban_id` (`admin.py:546`) va `fsm_channel_set` (`admin.py:617`) `message.text.strip()` chaqiradi.
Admin rasm yoki stiker yuborsa `AttributeError` → global xato, FSM holati tozalanmaydi.

**Talab.** `(message.text or "").strip()`, bo'sh bo'lsa — qayta so'rash.

#### F12 bo'yicha boshqa kamchiliklar

| # | Daraja | Muammo | Talab |
|---|---|---|---|
| F12-a | 🟡 | `_build_stats` 13 ta ketma-ket so'rov, 4 ta sessiya | Bitta sessiya, `asyncio.gather` yoki bitta SQL |
| F12-b | 🟡 | Eksport barcha foydalanuvchilarni xotiraga yuklaydi (`repo.py:107`) | `yield_per`, stream'li CSV |
| F12-c | ⚪ | `cmd_statistics`da admin tekshiruvi ikki marta (filter + ichida) | Bittasini qoldirish |
| F12-d | ⚪ | CTA URL faqat `startswith("http")` bilan tekshiriladi | `urlparse` + `https` sxema; tugma matni ≤ 64 belgi |
| F12-e | ⚪ | Xato matni (`{e}`) to'g'ridan-to'g'ri admin'ga — HTML parse rejimida buzilishi mumkin | `html.escape(str(e))` |

---

### F1 / F14. Foydalanuvchi ro'yxati va statistika

#### BUG-04 🟠 Foydalanuvchi DB'ga faqat til tugmasini bossa yoziladi [TASDIQLANGAN]

**Joriy holat.** `UserRepo.get_or_create()` faqat `cb_lang_select`da chaqiriladi (`start.py:115`).
`/start` bosib, tilni tanlamasdan havola yuborgan foydalanuvchi:
- DB'da yo'q → `inc_downloads`, `inc_searches`, `touch` 0 qatorni yangilaydi (jim yo'qotish);
- broadcast'ga tushmaydi, ban qilib bo'lmaydi, "Jami foydalanuvchilar" noto'g'ri.

**Talablar.**
1. `DbMiddleware` (yoki yangi `UserMiddleware`) har bir private-chat hodisasida foydalanuvchini
   `INSERT ... ON CONFLICT (tg_id) DO UPDATE SET username, first_name, last_active` bilan upsert qilsin.
   Yuklamani kamaytirish uchun Redis'da `useen:{tg_id}` (TTL 1 soat) bo'lsa — o'tkazib yuborish.
2. `new_users` kunlik hisobi upsert natijasida (`xmax = 0`) aniqlansin.

**Qabul mezonlari.** Tilni tanlamagan foydalanuvchi havola yuborgandan keyin `users` jadvalida paydo
bo'ladi va `downloads_count = 1`.

#### BUG-12 🟠 Statistika noto'g'ri [TASDIQLANGAN]

**Joriy holat.**
1. "⚡ Kesh (bugun)" `downloads.from_cache = true` qatorlarni sanaydi (`repo.py:151-159`), lekin
   `DownloadRepo.create()` hech qachon `from_cache=True` bilan chaqirilmaydi. Keshdan yuborishlar
   `downloads` jadvaliga **umuman yozilmaydi** (`download.py:114-115`, `tasks.py:224-229, 253-260`).
   Natija: kesh ko'rsatkichi doim `0/N`.
2. "Yuklamalar bugun" (`downloads` jadvali) va `daily_stats.downloads` (kesh ham hisoblanadi) turli
   raqam beradi. `daily_stats` jadvali umuman o'qilmaydi.
3. Kun chegarasi aralash: `date.today()` (server lokal vaqti, `repo.py:112,171,...`),
   `datetime.utcnow().date()` (`repo.py:83,141,...`), `func.date(created_at)` (DB sessiya vaqt zonasi).
   UTC+5 (Toshkent) uchun har kuni 00:00–05:00 oralig'idagi ma'lumot noto'g'ri kunga tushadi.
4. `func.date(created_at) == today` indeksdan foydalanmaydi (to'liq skan).

**Talablar.**
1. Barcha yuklashlar (kesh ham) `downloads` jadvaliga `from_cache` bilan yozilsin — bot handler'dagi
   kesh yo'li ham, worker'dagi kesh yo'li ham.
2. Statistika uchun yagona vaqt zonasi: `settings.stats_tz = "Asia/Tashkent"`. "Bugun" =
   `[tz_midnight, tz_midnight + 1 day)` oralig'i, `created_at >= :start AND created_at < :end`
   (indeksdan foydalanadi).
3. Yoki `daily_stats`ni yagona manba qilish, yoki uni olib tashlash — ikki parallel manba bo'lmasin.
4. `downloads.user_id`, `downloads.created_at` uchun indekslar mavjudligini tekshirish
   (`user_id`da indeks yo'q).

#### BUG-23 🟡 `get_or_create` poyga holati [EHTIMOLIY]

`repo.py:21-29`: SELECT → INSERT. Foydalanuvchi til tugmasini ikki marta tez bossa, ikkinchi INSERT
`UniqueViolation` beradi. **Talab:** `INSERT ... ON CONFLICT DO NOTHING RETURNING` yoki BUG-04'dagi upsert.

#### BUG-32 ⚪ Migratsiyalar

`alembic/` bor, `alembic/versions/` `.gitignore`da (lekin bitta fayl lokal mavjud), ishga tushishda esa
`Base.metadata.create_all` + qo'lda `ALTER ... IF NOT EXISTS` ishlatiladi. Xatolar `debug` darajada
yutiladi (`migrations.py:61`), DB xatosi bo'lsa bot baribir ishga tushadi (`main.py:82-83`).

**Talablar.** Alembic'ni yagona migratsiya vositasi qilish, `versions/`ni repozitoriyga qo'shish,
startup'da `alembic upgrade head`; DB tayyor bo'lmasa — jarayon xato kodi bilan to'xtasin.

---

### F13. Navbat, kesh va worker'lar

#### BUG-03 🔴 Disk tozalash ishlamaydi va xavfli [TASDIQLANGAN]

**Joriy holat.**
- `WorkerSettings.cron_jobs: list = []` (`tasks.py:653`), kommentariy esa "Periodic disk cleanup every
  hour" deydi. `cleanup_task` hech qayerdan chaqirilmaydi. Worker crash/timeout'da qolgan fayllar
  `downloads/` hajmida to'planadi (shared volume — Local Bot API fayllari ham shu yerda).
- `cleanup_old_files(settings.download_dir, 3600)` katalogdagi **barcha** 1 soatdan eski fayllarni
  o'chiradi. `cookies_path()` default yo'li `download_dir/cookies.txt` (`downloader.py:122`), README ham
  cookie'ni `/downloads/cookies.txt`ga qo'yishni aytadi → cron yoqilgan zahoti cookie fayli 1 soatdan keyin
  o'chadi va Instagram/YouTube yuklash buziladi.

**Talablar.**
1. Vaqtinchalik fayllar alohida `download_dir/tmp/` ichida yaratilsin (`get_temp_path`).
2. Tozalash faqat `tmp/` ichida va faqat ma'lum prefikslar (`ydl_`, `shz_`, `shz_in_`, `mus_`, `th_`) bo'yicha.
3. `cron_jobs = [cron(cleanup_task, minute={0, 30}, unique=True)]` — faqat asosiy `WorkerSettings`da.
4. Cookie fayli default yo'li `download_dir`dan tashqariga (`/app/secrets/cookies.txt`) ko'chirilsin,
   README yangilansin.

**Qabul mezonlari.** `tmp/`dagi 2 soatlik fayl 30 daqiqa ichida o'chadi; `cookies.txt` va boshqa
fayllar saqlanib qoladi.

#### BUG-20 🟡 Slot va single-flight vaqtlari [EHTIMOLIY]

- `ucap:{user}` TTL qattiq 300 s (`cache.py:185`), `JOB_TIMEOUT` esa sozlanadi. `JOB_TIMEOUT=600`
  bo'lsa hisoblagich ish davomida yo'qoladi va limit buziladi. **Talab:** `_SLOT_TTL = settings.job_timeout + 60`.
- Single-flight kutish budjeti `min(60, job_timeout//3)` (`tasks.py:244`). Birinchi worker 60 s'dan
  ko'p yuklasa, ikkinchisi ham yuklashni boshlaydi (lock hali band — `sf_acquired=False`) → takroriy
  yuklash. **Talab:** kutish budjeti lock TTL'iga yaqin bo'lsin yoki ikkinchi vazifa arq `Retry(defer=...)`
  bilan qayta navbatga qo'yilsin.

#### BUG-24 🟡 `safe_delete` event loop'ni bloklaydi [TASDIQLANGAN]

`media.py:142-149` — `time.sleep(0.4)` ikki martagacha async kontekstda. **Talab:** async variant
(`await asyncio.sleep`) yoki `asyncio.to_thread(safe_delete, path)`.

#### F13 bo'yicha boshqa kamchiliklar

| # | Daraja | Muammo | Talab |
|---|---|---|---|
| F13-a | ⚪ | `assert sent.audio is not None` (`tasks.py:289,297,599`) — `python -O`da o'chadi | `if ... is None: raise RuntimeError(...)` |
| F13-b | ⚪ | `asyncio.get_event_loop()` korutina ichida (`downloader.py:293,317`, `music_search.py:75,120`, `lyrics.py:111`, `tasks.py:48,420`, `admin.py:313`) | `asyncio.get_running_loop()` yoki `asyncio.to_thread` |
| F13-c | 🟡 | `WorkerSettings.redis_settings` import paytida hisoblanadi; Redis paroli (prod) URL'da bo'lmasa ulanmaydi | BUG-11 bilan birga |
| F13-d | ⚪ | `store_video_for_shazam` ichida takroriy `import hashlib` va MD5 | `secrets.token_hex(5)` |

---

### F15. Log va xatolar

#### BUG-21 🟡 [TASDIQLANGAN]

1. `global_error_handler` (`errors.py:22`) `lang = "en"` — foydalanuvchi tili hech qachon ishlatilmaydi.
   **Talab:** `update` dan `from_user.id` → `get_user_lang()`; Redis xato bersa `default_lang`.
2. Log formati (`logger.py:14`) JSON'ga o'xshaydi, lekin `message` escape qilinmaydi: xabarda `"` yoki
   yangi qator bo'lsa (exception matnlari, `_log_timing` JSON) log parse qilinmaydi.
   **Talab:** `json.dumps` asosidagi haqiqiy JSON formatter (yoki `requirements.txt`da bor `structlog`).
3. `logger.exception` stack-trace'ni ko'p qatorli matn sifatida chiqaradi — JSON log bilan mos emas.

---

### F2 / umumiy. Lokalizatsiya

#### BUG-22 🟡 [TASDIQLANGAN]

| Joy | Muammo |
|---|---|
| `download.py:35` | Ruscha `"имеeт"` — lotin `e` aralashgan |
| `shazam.py:90,131,146,167`, `download.py:222`, `music.py:143` | Qattiq yozilgan inglizcha matnlar |
| `main.py:87` | Komandalar faqat `en` tilida o'rnatiladi; `_COMMANDS["uz"]`, `["ru"]` ishlatilmaydi |
| `bot/locales/*.ftl` | 3 ta `.ftl` fayl va `aiogram-i18n`, `fluent.runtime` bog'liqliklari bor, lekin kodda ishlatilmaydi; matnlar har bir modulda dict sifatida takrorlangan (`_DL`, `_ERR` ikki faylda) |

**Talablar.**
1. Barcha foydalanuvchi matnlari bitta manbaga (`.ftl` + aiogram-i18n, yoki bitta `texts.py`) ko'chirilsin.
2. `set_my_commands` har bir til uchun `language_code` bilan chaqirilsin.
3. Test: har bir kalit uch tilda mavjud ekanini tekshiruvchi test (`test_i18n.py` kengaytiriladi).

---

### Umumiy: Redis bog'liqligi va guruh chatlari

#### BUG-27 🟡 Redis'ga qattiq bog'liqlik [EHTIMOLIY]

`UserLangMiddleware` (`i18n.py:36`) va `ThrottlingMiddleware` (`throttling.py:26`) Redis xatosini
ushlamaydi → Redis qisqa vaqt ishlamasa **har bir** hodisa xato beradi. Shuningdek, til DB'dan
o'qilganda Redis keshi qayta to'ldirilmaydi (`i18n.py:40-44`) — 24 soatdan keyin har bir xabarda DB so'rovi.

**Talablar.** Ikkala middleware ham Redis xatosida fail-open (default til / ruxsat); DB'dan o'qilgan til
`set_user_lang` bilan keshlansin.

#### BUG-28 🟡 Guruh chatlari [EHTIMOLIY]

Hech bir router'da `F.chat.type == "private"` filtri yo'q. Bot guruhga qo'shilsa va privacy mode
o'chirilgan bo'lsa (yoki bot admin bo'lsa) har bir matnli xabar YouTube qidiruviga aylanadi.

**Talab.** Barcha foydalanuvchi router'lariga `F.chat.type == ChatType.PRIVATE` filtri; guruhda faqat
havola yoki botga mention qilinganda ishlash (agar guruh qo'llab-quvvatlanishi kerak bo'lsa).

---

### F16. Infratuzilma / deploy

#### BUG-11 🟠 Prod compose sozlamalari nomuvofiq [TASDIQLANGAN]

| Muammo | Fayl |
|---|---|
| Prod'da `POSTGRES_PASSWORD` majburiy o'zgaruvchi, lekin `bot`/`worker*` `DATABASE_URL` va `pgbouncer` `DB_PASSWORD` asosiy compose'da `bot_password` deb qattiq yozilgan → parol boshqacha bo'lsa hech kim ulanolmaydi | `docker-compose.yml` (bot/worker/pgbouncer `environment`) |
| Prod'da `REDIS_PASSWORD` qo'yilsa, `REDIS_URL: redis://redis:6379/0` parolsiz qoladi → bot va worker'lar ulanolmaydi | o'sha |
| Prod Redis `command` override `--maxmemory 2gb --maxmemory-policy volatile-lru`ni yo'qotadi | `docker-compose.prod.yml` |
| Prod'da faqat `worker` replicas=3; `worker-recognition`, `worker-music` loglarida rotation yo'q | `docker-compose.prod.yml` |

**Talablar.** Barcha parollar `.env`dan (`${POSTGRES_PASSWORD}`, `${REDIS_PASSWORD}`) olinsin va
`DATABASE_URL`/`REDIS_URL` shu o'zgaruvchilardan quriladi; Redis prod buyrug'i maxmemory sozlamalarini
saqlasin; har bir servis uchun log rotation.

**Qabul mezoni.** `POSTGRES_PASSWORD=xyz REDIS_PASSWORD=abc docker compose -f docker-compose.yml -f docker-compose.prod.yml up`
bilan barcha servislar `healthy`, bot `/start`ga javob beradi.

#### BUG-29 🟡 Xavfsizlik gigiyenasi [TASDIQLANGAN]

| Muammo | Talab |
|---|---|
| `telegram-bot-api` `8081:8081` tashqi interfeysga ochiq | `127.0.0.1:8081:8081` yoki port'ni umuman ochmaslik |
| Konteynerlar root foydalanuvchida | `Dockerfile`da `useradd app` + `USER app` |
| PgBouncer healthcheck `... \|\| true` — doim healthy | `\|\| true`ni olib tashlash |
| `version: "3.9"` eskirgan | O'chirish |
| Debian `nodejs` paketi yt-dlp EJS uchun eski bo'lishi mumkin [EHTIMOLIY] | Node versiyasini tekshirish (≥ 20), kerak bo'lsa NodeSource |
| PgBouncer transaction rejimida faqat asyncpg `statement_cache_size=0`; SQLAlchemy'ning `prepared_statement_cache_size` ham 0 bo'lishi tavsiya etiladi [EHTIMOLIY] | `session.py:12` ga qo'shish |
| Test bog'liqliklari (`pytest*`, `httpx`) prod image'ga o'rnatiladi | `requirements-dev.txt` |

---

### BUG-06 🟠 HTML escape — umumiy talab [TASDIQLANGAN]

Bot `DefaultBotProperties(parse_mode=HTML)` bilan ishlaydi. Tashqi manbadan kelgan quyidagi qiymatlar
escape'siz HTML'ga qo'yiladi:

| Joy | Qiymat |
|---|---|
| `keyboards/inline.py:104` | YouTube `uploader`, `title` (qidiruv natijalari) |
| `handlers/shazam.py:105,108` | `title`, `artist`, lyrics matni |
| `worker/tasks.py:498` | Shazam `title`, `artist` |
| `middlewares/channel.py:104` | Kanal sarlavhasi |
| `handlers/admin.py:392,417,633` | Joriy welcome, preview, kanal nomi |

Natija: `Tom & Jerry`, `<3`, `Rock'n'Roll <Live>` kabi nomlarda Telegram `Bad Request: can't parse entities`
qaytaradi, foydalanuvchi "❌ Xatolik" ko'radi.

**Talab.** Barcha tashqi satrlar `html.escape()` orqali o'tsin. Yagona yordamchi funksiya
`bot/utils/text.py: esc(s: str | None) -> str`.

**Qabul mezoni.** Test: nomida `<`, `>`, `&` bo'lgan `SongEntry` uchun `music_search_keyboard` matni
Telegram HTML parser'idan o'tadi (aiogram `html_decoration` yoki snapshot test).

---

### BUG-30 / BUG-31 ⚪ Kod tozaligi

| Element | Holat | Talab |
|---|---|---|
| `bot/keyboards/reply.py` | Stub, ishlatilmaydi | O'chirish |
| `inline.channel_join_keyboard` | Ishlatilmaydi (middleware o'zi quradi) | Middleware shu funksiyani ishlatsin yoki o'chirish |
| `downloader.extract_info` | "Legacy", ishlatilmaydi | O'chirish |
| `music.handle_unsupported` filtri | `_HANDLED_TYPES` ro'yxati stiker/foto uchun ishlaydi, lekin router tartibiga bog'liq | Aniq `F.sticker \| F.photo \| ...` filtri |
| `requirements.txt` | `aiogram-i18n`, `fluent.runtime`, `structlog`, `tenacity` ishlatilmaydi; `alembic` ishlatilmaydi | Ishlatish yoki o'chirish |
| `datetime.utcnow()` | Python 3.12'da eskirgan (`repo.py` 6 joyda) | `datetime.now(timezone.utc)` |

---

## 4. Testlar bo'yicha talablar

Joriy 157 ta test asosan "yaxshi yo'l" va fuzz'larni qamraydi. Quyidagi testlar **majburiy** qo'shilsin:

| Test | Tekshiradi |
|---|---|
| `test_detect_platform_rejects_ssrf` | BUG-01: substring/IP/port/userinfo bypass'lari |
| `test_banned_user_is_ignored` | BUG-02: message va callback |
| `test_cleanup_keeps_cookies_and_non_temp` | BUG-03 |
| `test_user_created_without_lang_choice` | BUG-04 |
| `test_per_user_cap_rejects_extra_jobs` | BUG-05 |
| `test_html_escape_in_search_results_and_song_card` | BUG-06 |
| `test_welcome_invalid_html_not_saved` | BUG-07 |
| `test_ytdlp_opts_have_max_filesize` | BUG-08 |
| `test_music_duration_limit` | BUG-09 |
| `test_broadcast_retry_after_and_forbidden` | BUG-10 |
| `test_stats_cache_hits_counted` | BUG-12 |
| `test_throttle_message_uses_user_lang` | BUG-13 |
| `test_document_non_media_rejected` | BUG-16 |
| `test_music_search_uses_cache` | BUG-17 |
| `test_admin_fsm_non_text_message` | BUG-18 |
| `test_all_locale_keys_present` | BUG-22 |

Har bir tuzatish TDD tartibida: avval qizil test, keyin kod.

---

## 5. Bajarish rejasi (ustuvorlik bo'yicha)

| Bosqich | Topilmalar | Taxminiy hajm |
|---|---|---|
| **1. Xavfsizlik va kritik** (darhol) | BUG-01, BUG-02, BUG-03, BUG-11 | 1–2 kun |
| **2. Foydalanuvchiga ko'rinadigan xatolar** | BUG-04, BUG-05, BUG-06, BUG-07, BUG-08, BUG-09, BUG-13, BUG-14 | 3–4 kun |
| **3. Admin va statistika** | BUG-10, BUG-12, BUG-15, BUG-18, BUG-19 | 2–3 kun |
| **4. Barqarorlik va unumdorlik** | BUG-16, BUG-17, BUG-20, BUG-21, BUG-23–BUG-29 | 3 kun |
| **5. Tozalash** | BUG-22, BUG-30, BUG-31, BUG-32 | 1–2 kun |

Har bir bosqich oxirida: `pytest` to'liq yashil, `scripts/e2e_live_test.py` real bot bilan o'tadi.

---

## 6. Ijobiy tomonlar (saqlab qolish kerak)

- Uch alohida navbat (download / recognition / music) — bir turdagi yuklama boshqasini to'xtatmaydi.
- File_id keshi + single-flight lock — bir xil URL takroran yuklanmaydi.
- Backpressure (`max_queue_depth`) va per-process yt-dlp semafori.
- `release_user_slot`da atomik Lua skript (manfiy hisoblagichdan himoya).
- Rate-limit kalitida TTL yo'qolishini tiklash (`cache.py:93`).
- `_flood_retry` — `RetryAfter`da qayta urinish (worker'da).
- HLS throttling muammosi uchun format selektor yaxshi o'ylangan va hujjatlangan.
- Kanal a'zoligi Redis'da keshlanadi; obuna bo'lmaganlar uchun qisqa TTL.
- `_render()`da `first_name`/`username` escape qilinadi va bu test bilan qamralgan.
- SQL so'rovlar to'liq parametrlangan (SQLAlchemy Core/ORM).

---

## 7. Mas'ul shaxsga savollar

1. Guruh chatlari qo'llab-quvvatlanishi kerakmi yoki bot faqat private chat uchunmi? (BUG-28)
2. Statistika uchun qaysi vaqt zonasi asos — `Asia/Tashkent`mi? (BUG-12)
3. Musiqa uchun maksimal davomiylik qancha bo'lsin — 20 daqiqa mos keladimi? (BUG-09)
4. Production'da Local Bot API ishlatiladimi (2 GB) yoki cloud API (50 MB)? `MAX_FILE_MB` shunga qarab belgilanadi. (BUG-08)
5. `daily_stats` jadvali kelajakda grafiklar uchun kerakmi yoki olib tashlansinmi? (BUG-12)

---

## 8. Bajarilish holati (2026-11-05)

Barcha 32 ta topilma yopildi. Har biri uchun regression test bor
(`tests/test_audit_fixes.py`, TZ §4'dagi 16 ta majburiy test shu faylda).
Offlayn to'plam: `pytest -m "not network"` — **yashil**.

| ID | Holat | Qanday yopildi |
|---|---|---|
| BUG-01 | ✅ | Hostname bo'yicha domen chegarasi; IP, userinfo, nostandart port rad etiladi; yt-dlp `allowed_extractors` (generic yo'q); qisqa havolalarning har bir redirect'i tekshiriladi |
| BUG-02 | ✅ | Redis ban to'plami + `dp.update` outer `BanMiddleware`; admin hech qachon bloklanmaydi; Redis ishlamasa fail-open |
| BUG-03 | ✅ | `cleanup_task` arq cron (30 daqiqa, `unique`); faqat `DOWNLOAD_DIR/tmp` va prefiksli fayllar; cookie `./secrets/`da |
| BUG-04 | ✅ | Birinchi xabardayoq upsert (soatiga 1 marta, `useen:` belgisi); `lang_chosen` ustuni |
| BUG-05 | ✅ | Slot navbatga qo'yishda atomar (Lua) band qilinadi; callback debounce + rate limit |
| BUG-06 | ✅ | `esc()` barcha tashqi matnlarda; lyrics ≤ 4096 belgi; song card havolalari faqat `https` |
| BUG-07 | ✅ | `message.html_text`; preview yuborilmasa saqlanmaydi; `/start` xato bo'lsa standart matn |
| BUG-08 | ✅ | Ikki bosqichli yt-dlp: metadata → `check_limits` → yuklash; `max_filesize`; cloud API'da 50 MB clamp |
| BUG-09 | ✅ | MP3 ≤ `MAX_AUDIO_DURATION` (20 daqiqa) yuklashdan oldin tekshiriladi; ro'yxatda uzun treklar belgilanadi |
| BUG-10 | ✅ | Broadcast arq worker'da, Redis'da progress, qayta ishga tushsa davom etadi; `RetryAfter` o'sha userga qayta; bloklaganlar `is_blocked_bot` |
| BUG-11 | ✅ | Parollar faqat env'dan; prod'da `POSTGRES_PASSWORD`/`REDIS_PASSWORD` majburiy; `REDIS_URL`ga parol avtomatik qo'shiladi |
| BUG-12 | ✅ | Statistika event jadvallaridan, `Asia/Tashkent` kun chegarasi, kesh hitlari `from_cache=True` bilan sanaladi; `daily_stats` yozilmaydi |
| BUG-13 | ✅ | Throttling tilni Redis'dan oladi |
| BUG-14 | ✅ | `_user_job`: `asyncio.timeout` + `CancelledError` → progress xabari xato matniga almashtiriladi |
| BUG-15 | ✅ | Kanal tekshiruvi callback'larda ham; tasdiq lokalizatsiya qilingan; **admin hech qachon bloklanmaydi** |
| BUG-16 | ✅ | Faqat `audio/*`, `video/*` hujjatlar; cloud API'da > 20 MB uchun oldindan xabar; 90 s audio klip zaxirasi |
| BUG-17 | ✅ | Qidiruv keshi o'qiladi; qidiruv cheklangan thread pool'da |
| BUG-18 | ✅ | `parse_user_id`/`parse_channel_ref` `None`ga chidamli; noto'g'ri kiritishda holat saqlanadi |
| BUG-19 | ✅ | Kanal saqlashdan oldin bot admin ekanligi tekshiriladi |
| BUG-20 | ✅ | `ucap` TTL = `job_timeout + 60`; single-flight kutish lock yo'qolguncha |
| BUG-21 | ✅ | Global xato foydalanuvchi tilida; `JsonFormatter` — har satr yaroqli JSON |
| BUG-22 | ✅ | Barcha matnlar `bot/texts.py`da (uz/ru/en), kalitlar to'liqligi test bilan |
| BUG-23 | ✅ | `INSERT … ON CONFLICT … RETURNING (xmax = 0)` |
| BUG-24 | ✅ | `safe_delete` bloklamaydi; `safe_delete_async` |
| BUG-25 | ✅ | Hamma yt-dlp chaqiruvlari `build_base_opts` + umumiy `run_ydl` orqali |
| BUG-26 | ✅ | `recognition_windows` takrorlarni olib tashlaydi; ≤ 15 s klip — bitta so'rov |
| BUG-27 | ✅ | Ban, throttling, til, kanal — Redis xatosida fail-open; til keshi qayta to'ldiriladi |
| BUG-28 | ✅ | Barcha router'lar `ChatType.PRIVATE` |
| BUG-29 | ✅ | Konteyner `app` (uid 10001) foydalanuvchisida; 8081 faqat `127.0.0.1`; PgBouncer healthcheck haqiqiy |
| BUG-30 | ✅ | `reply.py`, `.ftl`, ishlatilmagan paketlar olib tashlandi; test kutubxonalari `requirements-dev.txt`da |
| BUG-31 | ✅ | `assert`lar, `get_event_loop()`, `utcnow()` almashtirildi |
| BUG-32 | ✅ | Alembic yagona sxema vositasi; idempotent baseline; startup'da `upgrade head` |

### Audit davomida qo'shimcha topilgan va tuzatilganlar

- **YouTube "720p/1080p" aslida 360p edi.** Qattiq yozilgan `player_client`
  ro'yxati (`android, web_safari, web`) PO token'siz faqat 360p formatini
  berardi. Endi yt-dlp'ning `default` klienti + DASH (alohida video+audio,
  `-c copy`) birinchi; YouTube 403 qaytarsa bir marta eski klientlar bilan
  qayta uriniladi (`run_ydl`), eski urinishning `.part` fayllari o'chiriladi.
- Bloklangan URL "generic" emas, "unsupported" deb ko'rsatiladi; klip fayli
  ikki kengaytmali (`.m4a.mp4`) bo'lib qolmaydi.
- AudD.io zaxirasi (faqat `AUDD_TOKEN` bilan), Spotify tugmasi; bitta xabarda
  5 tagacha havola (har biri per-user limitga kiradi); h264/aac afzal.

### Jonli tekshirilgan

- Haqiqiy Redis: Lua slot (50 parallel so'rov → aynan 3), ban, debounce, lock'lar.
- Haqiqiy Postgres: `upgrade`/`downgrade`/`upgrade`, eski `create_all` bazasidan
  o'tish, upsert, keyset sahifalash, export, statistika.
- Haqiqiy YouTube: 720p/1080p (h264+aac), MP3, 90 s klip, qidiruv, musiqa,
  403 zaxirasi, 6 soatlik trek rad etilishi, thumbnail, SSRF bloki.

### Hali tekshirilmagan

- Docker image build va entrypoint (`setpriv`), PgBouncer orqali ulanish.
- Telegram'da jonli bot va broadcast (ikkinchi polling nusxasi xavfi sababli).
- TikTok (lokal tarmoqda bloklangan), Instagram (cookie kerak).

### Mas'ul shaxs qarorlari (§7) — qabul qilingan standartlar

1. Bot faqat private chat. 2. `Asia/Tashkent`. 3. 20 daqiqa. 4. `LOCAL_API_URL`
bo'lsa 2000 MB, bo'lmasa 50 MB (avtomatik). 5. `daily_stats` saqlanadi, lekin
yozilmaydi — o'chirish alohida migratsiya bilan.
