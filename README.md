# @vidyuklaydi_bot — Video Downloader + Song Finder

A production-grade Telegram bot that:
1. **Downloads videos** from YouTube, TikTok, Instagram, Facebook, Twitter/X, Pinterest, and any yt-dlp-supported site
2. **Recognizes songs** from video, audio, voice messages, and video notes via Shazam — with lyrics

## Features
- 3 languages: Uzbek, Russian, English
- Local Bot API server (2 GB file limit, fast `file://` uploads)
- Redis `file_id` cache — repeat requests are near-instant
- Async arq worker queue — bot never blocks
- Rate limiting per user
- Admin stats, broadcast, ban/unban

---

## Environment Variables

Copy `.env.example` to `.env` and fill in:

| Variable | Description | How to get |
|---|---|---|
| `BOT_TOKEN` | Telegram bot token | [@BotFather](https://t.me/BotFather) → /newbot |
| `TELEGRAM_API_ID` | Telegram API ID | [my.telegram.org](https://my.telegram.org) |
| `TELEGRAM_API_HASH` | Telegram API hash | [my.telegram.org](https://my.telegram.org) |
| `GENIUS_TOKEN` | Genius API token | [genius.com/api-clients](https://genius.com/api-clients) |
| `ADMIN_IDS` | Comma-separated Telegram user IDs | Your own Telegram ID |
| `DATABASE_URL` | PostgreSQL connection URL | Set in docker-compose |
| `REDIS_URL` | Redis connection URL | Set in docker-compose |
| `LOCAL_API_URL` | Local Bot API server URL | `http://telegram-bot-api:8081` |
| `DOWNLOAD_DIR` | Path for temporary downloads | `/downloads` |
| `MAX_FILE_MB` | Max upload size in MB (>50 needs the Local Bot API; cloud API is clamped to 50) | `500` |
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

Put a `cookies.txt` (Netscape format) at `./secrets/cookies.txt`. Compose mounts
`./secrets` read-only at `/app/secrets`, and yt-dlp picks it up automatically
(`COOKIES_FILE` overrides the path). Do **not** keep it under `/downloads`: that
volume is shared with the Bot API server, and the cleanup cron sweeps it.

## Blocked platforms (TikTok) — proxy

Some networks block TikTok (TLS handshakes to `www.tiktok.com` time out and the
`v16/v19-webapp-prime` video CDNs never connect). Set `PROXY_URL` to an HTTP or
SOCKS5 proxy outside that network (`socks5://host:1080`,
`http://user:pass@host:3128`); `PROXY_PLATFORMS` (default `tiktok`) lists the
platforms that go through it, everything else stays direct. yt-dlp passes the
proxy to aria2c as well. The TikTok extractor also needs `curl_cffi` (in
requirements) for TLS impersonation.

## Database migrations

Alembic is the only schema tool. The bot runs `alembic upgrade head` itself on
startup, and the migrations are idempotent. Manual run: `alembic upgrade head`.
New change: `alembic revision -m "..."`, then write the migration by hand.

## Production secrets

`docker-compose.prod.yml` refuses to start unless `POSTGRES_PASSWORD` and
`REDIS_PASSWORD` are set. The base file has no hard-coded passwords, and the
Bot API port is bound to `127.0.0.1` only.

## Large files (up to 500 MB)

Telegram's cloud Bot API caps uploads at 50 MB. Larger files go through the
`telegram-bot-api` container (`LOCAL_API_URL`, needs `TELEGRAM_API_ID` /
`TELEGRAM_API_HASH` from my.telegram.org). Defaults: `MAX_FILE_MB=500`,
`JOB_TIMEOUT=900`.

Videos bigger than one upload (50 MB on the cloud API) keep the quality the
user picked: the finished MP4 is cut with `ffmpeg -c copy` (no re-encode,
seconds per GB) into equal parts that each fit, sent as "Part 1/N", "Part
2/N"… and cached together, so the next request re-sends all parts instantly.
`MAX_VIDEO_MB` (default 500) caps the total video download;
`SPLIT_LARGE_VIDEOS=false` restores the old behaviour (a lower resolution
480p → 360p → 240p that fits one upload). Only above `MAX_VIDEO_MB` does the
bot fall back to a lower resolution.

Disk: each in-flight download can take ~2× the file size (streams + merged
file). Budget about `replicas × YT_DLP_WORKER_CONCURRENCY × 2 × MAX_FILE_MB`
of free space on the `downloads` volume (3 × 4 × 2 × 500 MB ≈ 12 GB).

## Temp-file cleanup

Workers write temporary files only to `DOWNLOAD_DIR/tmp`. One arq cron job
(`cleanup_task`, every 30 min, `unique=True`) deletes stale files there and
never touches anything else.

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
pip install -r requirements-dev.txt

# Run tests (offline; network tests are opt-in)
pytest -m "not network"

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
