from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings



def pgbouncer_connect_args(database_url: str) -> dict:
    """asyncpg options for PgBouncer in transaction-pool mode.

    Both caches must be off — asyncpg's own (statement_cache_size) AND
    SQLAlchemy's adapter cache (prepared_statement_cache_size) — and statement
    names must be unique, otherwise pooled server connections raise
    "prepared statement … does not exist / already exists".
    """
    if "pgbouncer" not in database_url:
        return {}
    from uuid import uuid4
    return {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
    }


_connect_args = pgbouncer_connect_args(settings.database_url)

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    pool_recycle=1800,
    echo=False,
    connect_args=_connect_args,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session
