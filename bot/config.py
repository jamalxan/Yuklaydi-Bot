from __future__ import annotations

from typing import List
from typing_extensions import Annotated

import logging

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


CLOUD_API_MAX_MB = 50
CLOUD_API_GETFILE_MAX_MB = 20
LOCAL_API_MAX_MB = 2000  # Local Bot API server upload limit


def recommended_job_timeout(max_file_mb: int) -> int:
    """Seconds a worst-case job needs for a file of *max_file_mb*."""
    return 300 + max(0, max_file_mb - 50)  # getFile download limit on the cloud API


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    bot_token: str
    telegram_api_id: int
    telegram_api_hash: str
    local_api_url: str = ""  # empty = use standard Telegram cloud API

    # Database
    database_url: str = "postgresql+asyncpg://bot:bot@localhost:5432/yuklaydi_bot"

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_password: str = ""  # optional; merged into redis_url (see redis_dsn)
    redis_max_connections: int = 512

    # DB pool (per process; route through PgBouncer when scaling out)
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # Lyrics
    genius_token: str = ""
    musixmatch_token: str = ""
    audd_token: str = ""

    # Bot config
    admin_ids: Annotated[List[int], NoDecode] = []
    default_lang: str = "uz"
    # Upload limit. The Telegram cloud Bot API caps bot uploads at 50 MB; only
    # a Local Bot API server (LOCAL_API_URL) allows up to 2000 MB. The
    # validator below clamps the value so a copied .env can't promise 2 GB on
    # the cloud API (downloads would succeed and then fail on upload).
    max_file_mb: int = 50
    max_audio_duration: int = 1200       # seconds; longer tracks are refused up-front
    download_dir: str = "./downloads"
    worker_concurrency: int = 5
    cookies_file: str = ""
    # e.g. "chrome" / "firefox" / "edge" — yt-dlp reads cookies straight from
    # the browser profile. Needed for Instagram (login-gated since 2024+) and
    # helps YouTube dodge bot-checks. Empty = disabled.
    cookies_from_browser: str = ""

    # Rate limiting
    rate_limit_requests: int = 10
    rate_limit_window: int = 60
    callback_rate_limit_requests: int = 30   # button presses per window
    callback_debounce_seconds: int = 3       # identical (user, data) presses ignored

    # Music search runs yt-dlp in the bot process; bound its thread pool so a
    # search burst can't starve the event loop's default executor.
    search_executor_workers: int = 4

    # Statistics day boundary ("today" = local midnight in this zone)
    stats_tz: str = "Asia/Tashkent"

    # Fairness / scale
    per_user_download_cap: int = 3       # max simultaneous fresh downloads per user
    yt_dlp_worker_concurrency: int = 4   # max concurrent yt-dlp calls per worker process
    job_timeout: int = 300               # arq job hard timeout (seconds)
    max_queue_depth: int = 100_000       # reject new jobs above this depth (backpressure)

    # Logging
    log_level: str = "INFO"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @model_validator(mode="after")
    def _clamp_cloud_limits(self):
        if not self.local_api_url and self.max_file_mb > CLOUD_API_MAX_MB:
            logging.getLogger("yuklaydi.config").warning(
                "MAX_FILE_MB=%s ignored: cloud Bot API upload limit is %s MB "
                "(set LOCAL_API_URL to use a Local Bot API server)",
                self.max_file_mb, CLOUD_API_MAX_MB,
            )
            self.max_file_mb = CLOUD_API_MAX_MB
        if self.max_file_mb > LOCAL_API_MAX_MB:
            logging.getLogger("yuklaydi.config").warning(
                "MAX_FILE_MB=%s capped to the Local Bot API limit of %s MB",
                self.max_file_mb, LOCAL_API_MAX_MB,
            )
            self.max_file_mb = LOCAL_API_MAX_MB
        # Big files need time: download + ffmpeg merge + upload through the
        # Local Bot API. Rough floor: 300 s plus 1 s per MB above 50 MB.
        needed = recommended_job_timeout(self.max_file_mb)
        if self.job_timeout < needed:
            logging.getLogger("yuklaydi.config").warning(
                "JOB_TIMEOUT=%ss is short for MAX_FILE_MB=%s; large files may "
                "time out. Recommended: JOB_TIMEOUT>=%s", self.job_timeout,
                self.max_file_mb, needed,
            )
        return self

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def use_local_api(self) -> bool:
        return bool(self.local_api_url)

    @property
    def redis_dsn(self) -> str:
        """REDIS_URL with REDIS_PASSWORD injected when the URL carries none."""
        return _with_redis_password(self.redis_url, self.redis_password)


def _with_redis_password(url: str, password: str) -> str:
    if not password:
        return url
    from urllib.parse import quote, urlsplit, urlunsplit
    parts = urlsplit(url)
    if parts.password:
        return url
    user = parts.username or ""
    host = parts.hostname or "localhost"
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


settings = Settings()
