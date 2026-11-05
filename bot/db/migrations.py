"""Database bootstrap: Alembic is the only schema tool; this module runs it.

Startup calls :func:`upgrade_database`, which
1. waits (bounded) for the database to accept connections,
2. runs ``alembic upgrade head`` in a worker thread (Alembic's env.py uses
   ``asyncio.run`` and can't share the bot's event loop),
3. seeds default rows (idempotent ``ON CONFLICT DO NOTHING``).

Any failure raises — the caller must not start the bot on a broken schema.
"""
from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from bot.texts import DEFAULT_WELCOME, LEGACY_DEFAULT_WELCOMES

logger = logging.getLogger("yuklaydi.migrations")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DEFAULT_SETTINGS = {
    "required_channel_id": "",
    "required_channel_title": "",
    "required_channel_url": "",
}


def _alembic_upgrade_head() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(_PROJECT_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_PROJECT_ROOT, "alembic"))
    cfg.attributes["configure_logger"] = False  # keep the app's JSON logging
    command.upgrade(cfg, "head")


async def wait_for_db(engine: AsyncEngine, attempts: int = 10, delay: float = 3.0) -> None:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception as e:  # connection refused, DNS, auth …
            last = e
            logger.warning(f"Database not ready (attempt {attempt}/{attempts}): {e}")
            await asyncio.sleep(delay)
    raise RuntimeError(f"Database unavailable after {attempts} attempts: {last}")


async def seed_defaults(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for lang, txt in DEFAULT_WELCOME.items():
            await conn.execute(
                text("INSERT INTO welcome_messages (lang, text) VALUES (:lang, :text) "
                     "ON CONFLICT (lang) DO NOTHING"),
                {"lang": lang, "text": txt},
            )
            # A row still holding an older built-in default was never edited
            # by an admin: move it to the current default.
            for legacy in LEGACY_DEFAULT_WELCOMES:
                old = legacy.get(lang)
                if old and old != txt:
                    await conn.execute(
                        text("UPDATE welcome_messages SET text = :new "
                             "WHERE lang = :lang AND text = :old"),
                        {"lang": lang, "new": txt, "old": old},
                    )
        for key, value in _DEFAULT_SETTINGS.items():
            await conn.execute(
                text("INSERT INTO settings (key, value) VALUES (:key, :value) "
                     "ON CONFLICT (key) DO NOTHING"),
                {"key": key, "value": value},
            )


async def upgrade_database(engine: AsyncEngine | None = None) -> None:
    if engine is None:
        from bot.db.session import engine as default_engine
        engine = default_engine
    await wait_for_db(engine)
    await asyncio.to_thread(_alembic_upgrade_head)
    await seed_defaults(engine)
    logger.info("DB migrations complete (alembic head)")
