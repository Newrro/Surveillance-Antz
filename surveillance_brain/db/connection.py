"""
db/connection.py
================
Async database connection pool management (SQLAlchemy 2.0 + asyncpg).

Exports:
    engine          — global AsyncEngine singleton
    async_session   — async_sessionmaker bound to the engine
    get_session()   — @asynccontextmanager yielding an AsyncSession
    get_db()        — FastAPI dependency (yields AsyncSession)
    init_db()       — optional: ping the engine on startup (used in smoke tests)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine + session factory
#
# pool_size=20  → 20 persistent connections kept warm
# max_overflow=10 → allow short bursts up to 30 total connections
# pool_pre_ping=True → cheap `SELECT 1` before checkout so dead conns are
#                      silently recycled (essential when DB sits behind a
#                      docker NAT).
# ---------------------------------------------------------------------------
engine: AsyncEngine = create_async_engine(
    config.DATABASE_DSN,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
    echo=False,
    future=True,
)

async_session: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
    class_=AsyncSession,
)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """
    Context-managed session.

    Commits automatically on clean exit, rolls back on any exception.

    Usage:
        async with get_session() as session:
            session.add(obj)
    """
    session = async_session()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_db() -> AsyncIterator[AsyncSession]:
    """
    FastAPI dependency — yields a session and always closes it.
    Routes that need DB access should declare `session: AsyncSession = Depends(get_db)`.

    NOTE: This dependency does NOT auto-commit.  Routes that mutate must
    call `await session.commit()` explicitly.  This is intentional — it
    gives route handlers full control over transaction boundaries.
    """
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """
    Lightweight liveness check — opens one connection, runs SELECT 1.
    Used by the lifespan / smoke tests to fail fast if Postgres is down.
    """
    async with engine.connect() as conn:
        await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
    logger.info("Database connection OK (%s)", config.DATABASE_DSN.split("@")[-1])
