from __future__ import annotations

from typing import List
from typing_extensions import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    database_url: str = "postgresql+asyncpg://postgres:1@localhost:5432/vidbot"

    # Redis
    redis_url: str = "redis://localhost:6379/0"
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
    max_file_mb: int = 50
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

    # Fairness / scale
    per_user_download_cap: int = 3       # max simultaneous fresh downloads per user
    yt_dlp_worker_concurrency: int = 4   # max concurrent yt-dlp calls per worker process
    job_timeout: int = 300               # arq job hard timeout (seconds)
    single_flight_wait: int = 60         # max seconds a duplicate-URL job waits for cache
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

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def use_local_api(self) -> bool:
        return bool(self.local_api_url)


settings = Settings()
