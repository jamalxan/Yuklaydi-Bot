"""initial_schema — baseline of the schema the bot used to build with create_all

Revision ID: d5e9eae454d8
Revises:
Create Date: 2026-06-13 21:47:53.790121

Idempotent on purpose: production databases were created by
``Base.metadata.create_all`` + hand-written ``ALTER … IF NOT EXISTS`` before
Alembic owned the schema. Existing tables/columns are left untouched, missing
ones are created, so ``alembic upgrade head`` is safe on old and new DBs.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d5e9eae454d8"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Plain DDL with IF NOT EXISTS: works online and in offline (--sql) mode and
# never touches tables that create_all already built.
_DDL = (
    """CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        tg_id BIGINT NOT NULL,
        username VARCHAR(128),
        first_name VARCHAR(256),
        lang VARCHAR(8) NOT NULL,
        joined_at TIMESTAMPTZ DEFAULT now(),
        last_active TIMESTAMPTZ,
        is_blocked BOOLEAN,
        is_admin BOOLEAN,
        downloads_count INTEGER,
        recognitions_count INTEGER,
        searches_count INTEGER
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_tg_id ON users (tg_id)",
    """CREATE TABLE IF NOT EXISTS downloads (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        url TEXT NOT NULL,
        platform VARCHAR(64),
        kind VARCHAR(32),
        status VARCHAR(32),
        file_id TEXT,
        from_cache BOOLEAN,
        created_at TIMESTAMPTZ DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_downloads_created_at ON downloads (created_at)",
    """CREATE TABLE IF NOT EXISTS song_cache (
        id SERIAL PRIMARY KEY,
        query_hash VARCHAR(64) NOT NULL,
        title TEXT,
        artist TEXT,
        lyrics TEXT,
        created_at TIMESTAMPTZ DEFAULT now()
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_song_cache_query_hash ON song_cache (query_hash)",
    """CREATE TABLE IF NOT EXISTS recognition_log (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        title TEXT,
        artist TEXT,
        found_lyrics BOOLEAN,
        source VARCHAR(32),
        created_at TIMESTAMPTZ DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_recognition_log_user_id ON recognition_log (user_id)",
    """CREATE TABLE IF NOT EXISTS music_searches (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        query TEXT NOT NULL,
        picked_index INTEGER,
        created_at TIMESTAMPTZ DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_music_searches_user_id ON music_searches (user_id)",
    """CREATE TABLE IF NOT EXISTS daily_stats (
        id SERIAL PRIMARY KEY,
        stat_date DATE NOT NULL UNIQUE,
        downloads INTEGER,
        recognitions INTEGER,
        new_users INTEGER,
        searches INTEGER,
        audio_sent INTEGER,
        cache_hits INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS welcome_messages (
        id SERIAL PRIMARY KEY,
        lang VARCHAR(8) NOT NULL UNIQUE,
        text TEXT NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        key VARCHAR(64) PRIMARY KEY,
        value TEXT
    )""",
    # Columns added over time by the old hand-written startup migrations
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(128)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name VARCHAR(256)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMPTZ",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS recognitions_count INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS searches_count INTEGER DEFAULT 0",
    "ALTER TABLE downloads ADD COLUMN IF NOT EXISTS from_cache BOOLEAN DEFAULT FALSE",
    "ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS searches INTEGER DEFAULT 0",
    "ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS audio_sent INTEGER DEFAULT 0",
    "ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS cache_hits INTEGER DEFAULT 0",
)


def upgrade() -> None:
    for stmt in _DDL:
        op.execute(stmt)


def downgrade() -> None:
    # Baseline: dropping every table is never done automatically.
    raise RuntimeError("Refusing to downgrade below the baseline schema (would drop all data).")
