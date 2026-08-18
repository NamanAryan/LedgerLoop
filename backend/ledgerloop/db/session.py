"""Async engine and session factory.

Isolation level: the engine runs at PostgreSQL's default READ COMMITTED, and that
is deliberate. Every write path in this service is a short single-statement-shaped
transaction whose correctness comes from a unique constraint (ingestion idempotency,
worker result uniqueness), not from snapshot stability across statements. READ
COMMITTED plus ON CONFLICT gives us the guarantee without paying for serialisable
retries under concurrent workers. Any transaction that needs something stronger
must say so explicitly at its own call site, with a comment saying why.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ledgerloop.config import Settings, get_settings


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    return create_async_engine(
        str(settings.database_url),
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # A connection killed by a PG restart fails fast on checkout, not mid-request.
        pool_pre_ping=True,
    )


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        expire_on_commit=False,  # response models read attributes after commit
        autoflush=False,  # flushes are explicit; no surprise INSERTs mid-read
    )


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = build_sessionmaker(get_engine())
    return _sessionmaker


async def session_scope() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, rolled back on any exception."""
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
