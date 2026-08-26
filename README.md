# @vidyuklaydi_bot — Video Downloader + Song Finder

A production-grade Telegram bot that:
1. **Downloads videos** from YouTube, TikTok, Instagram, Facebook, Twitter/X, Pinterest, and any yt-dlp-supported site
2. **Recognizes songs** from video, audio, voice messages, and video notes via Shazam — with lyrics

## Features
- 3 languages: Uzbek, Russian, English
- Local Bot API server (2 GB file limit, fast `file://` uploads)
- Redis `file_id` cache — repeat requests are near-instant
- Single-flight dedup — if 10 users paste the same link at once, only one worker
  downloads it; the rest get served from cache the moment it lands
- Multiple links in one message — paste several TikTok/Instagram/YouTube URLs
  at once, each comes back as its own result (capped at 5 per message)
- Async arq worker queue — bot never blocks, with separate queues/lanes for
  downloads, song recognition, and music search so one doesn't starve another
- Rate limiting per user
- Admin stats, broadcast, ban/unban
- Song recognition: Shazam first, with an optional AudD.io fallback (set
  `AUDD_TOKEN`) for the tracks Shazam's database misses — regional releases,
  remixes, covers. Results link out to Apple Music, Spotify, and Shazam, plus
  lyrics (Genius, with Musixmatch as a fallback) and one-tap MP3 download

---

## Environment Variables

Copy `.env.example` to `.env` and fill in:

| Variable | Description | How to get |
|---|---|---|
| `BOT_TOKEN` | Telegram bot token | [@BotFather](https://t.me/BotFather) → /newbot |
| `TELEGRAM_API_ID` | Telegram API ID | [my.telegram.org](https://my.telegram.org) |
| `TELEGRAM_API_HASH` | Telegram API hash | [my.telegram.org](https://my.telegram.org) |
| `GENIUS_TOKEN` | Genius API token | [genius.com/api-clients](https://genius.com/api-clients) |
| `MUSIXMATCH_TOKEN` | Musixmatch API token (optional lyrics fallback) | [developer.musixmatch.com](https://developer.musixmatch.com) |
| `AUDD_TOKEN` | AudD.io API token (optional Shazam fallback) | [dashboard.audd.io](https://dashboard.audd.io) |
| `ADMIN_IDS` | Comma-separated Telegram user IDs | Your own Telegram ID |
| `DATABASE_URL` | PostgreSQL connection URL | Set in docker-compose |
| `REDIS_URL` | Redis connection URL | Set in docker-compose |
| `LOCAL_API_URL` | Local Bot API server URL | `http://telegram-bot-api:8081` |
| `DOWNLOAD_DIR` | Path for temporary downloads | `/downloads` |
| `MAX_FILE_MB` | Max upload size in MB | `2000` |
| `WORKER_CONCURRENCY` | arq jobs per worker | `5` |

---

## AWS Lightsail Deploy (Ubuntu)

### 1. Prepare the server

```bash
# SSH into your Lightsail instance
ssh ubuntu@<your-ip>

# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
newgrp docker

# Install Docker Compose plugin
sudo apt-get install -y docker-compose-plugin
```

### 2. Clone and configure

```bash
git clone https://github.com/youruser/vidyuklaydi_bot.git
cd vidyuklaydi_bot
cp .env.example .env
nano .env  # fill in all secrets
```

### 3. Log out the bot from the Telegram cloud API

**Important:** Before starting the local API server, you must log the bot out of the cloud:

```bash
curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/logout"
```

### 4. Start all services

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### 5. Check status

```bash
docker compose ps
docker compose logs -f bot
```

### 6. Set up log rotation (already configured in prod compose)

### 7. Weekly yt-dlp update

Add to crontab (`crontab -e`):
```
0 4 * * 1 docker compose exec worker pip install -U yt-dlp
```

---

## Instagram / Age-gated content

Mount a `cookies.txt` (Netscape format) into the worker at `/downloads/cookies.txt`. yt-dlp will use it automatically.

---

## Scaling

To run more workers:
```bash
docker compose up -d --scale worker=5
```

---

## Local Development

```bash
# Install deps (requires ffmpeg installed locally)
pip install -r requirements.txt

# Run tests
pytest

# Start the bot (requires .env with valid credentials)
python -m bot.main
```

---

## Architecture

```
Telegram ←→ Local Bot API server (Docker, 2GB limit)
                    ↓
          Bot process (aiogram, long polling)
                    ↓
           arq task queue (Redis)
                    ↓
           Worker(s) (yt-dlp, ffmpeg, shazamio)
                    ↓
           PostgreSQL (stats, cache)
```
