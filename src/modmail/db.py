"""Database engine and session management.

This module is the single place that knows how to reach the database. Changing
``DATABASE_URL`` from SQLite to PostgreSQL requires no change anywhere else in
the codebase.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from modmail.models import Base

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create the process-wide engine. Safe to call once at startup."""
    global _engine, _session_factory

    if _engine is not None:
        return _engine

    _engine = create_async_engine(
        database_url,
        echo=echo,
        future=True,
        # SQLite needs a longer timeout when the bot and a future log viewer
        # both touch the file.
        connect_args={"timeout": 30} if database_url.startswith("sqlite") else {},
    )
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    logger.debug("Database engine created for %s", database_url.split("://", 1)[0])
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine is not initialised. Call init_engine() first.")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Session factory is not initialised. Call init_engine() first.")
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Provide a session that commits on success and rolls back on error."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def create_all() -> None:
    """Create tables that do not exist yet.

    Phase 1 uses ``create_all`` for speed. When the schema starts to change,
    replace this with Alembic migrations; the models already carry a naming
    convention so generated migrations are stable.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema ready.")


async def dispose_engine() -> None:
    """Close pooled connections. Called on shutdown and between tests."""
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
