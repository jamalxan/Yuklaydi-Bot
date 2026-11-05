"""users: is_blocked_bot + lang_chosen; indexes for date-range statistics

Revision ID: 7c41b2e9a0f3
Revises: d5e9eae454d8
Create Date: 2026-11-04 23:30:00

- users.is_blocked_bot — user blocked the bot (broadcast skips them); distinct
  from users.is_blocked (admin ban).
- users.lang_chosen — users are now registered on first contact, so "row
  exists" no longer means "picked a language". Rows that existed before this
  migration were only ever created by the language button → TRUE; new rows
  default to FALSE.
- Indexes so "today" statistics (created_at range predicates) and per-user
  lookups don't scan whole tables.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "7c41b2e9a0f3"
down_revision: Union[str, None] = "d5e9eae454d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES = (
    ("ix_downloads_user_id", "downloads", "user_id"),
    ("ix_downloads_created_at", "downloads", "created_at"),
    ("ix_users_joined_at", "users", "joined_at"),
    ("ix_recognition_log_created_at", "recognition_log", "created_at"),
    ("ix_music_searches_created_at", "music_searches", "created_at"),
)


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_blocked_bot BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS lang_chosen BOOLEAN NOT NULL DEFAULT TRUE")
    op.execute("ALTER TABLE users ALTER COLUMN lang_chosen SET DEFAULT FALSE")
    for name, table, column in _INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})")


def downgrade() -> None:
    for name, _table, _column in _INDEXES:
        if name == "ix_downloads_created_at":
            continue  # belongs to the baseline schema
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS lang_chosen")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_blocked_bot")
