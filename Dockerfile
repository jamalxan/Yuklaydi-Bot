# Node.js for yt-dlp's EJS (YouTube n-challenge) solver: yt-dlp needs a
# current Node (>= 20); the distro "nodejs" package may lag behind, so the
# binary is copied from the official image instead.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim

# aria2 = multi-connection parallel downloader (cuts HTTP download time ~50-80%)
# ffmpeg = mux/remux; only installed as fallback — we avoid transcoding
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Unprivileged runtime user. The entrypoint fixes ownership of the shared
# /downloads volume (named volumes created by older images are root-owned)
# and then drops privileges with setpriv.
RUN useradd --system --create-home --uid 10001 app \
    && mkdir -p /downloads/tmp /app/secrets \
    && chown -R app:app /downloads /app \
    && chmod +x /app/docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "-m", "bot.main"]
