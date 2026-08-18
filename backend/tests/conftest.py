"""Test infrastructure.

Real PostgreSQL and real Redis, in containers, for every test that touches them. Not
SQLite and not fakeredis, because the things this project claims to get right --
``ON CONFLICT DO NOTHING`` against partial unique indexes, ``FOR UPDATE SKIP LOCKED``,
``percentile_cont``, consumer-group semantics -- either do not exist or behave
differently in a substitute. A green suite against a fake would prove nothing about the
system that actually ships.

Schema comes from ``alembic upgrade head``, never ``create_all()``: the tests exercise
the same migration chain that production will run.

Set ``LEDGERLOOP_TEST_DATABASE_URL`` / ``LEDGERLOOP_TEST_REDIS_URL`` to reuse an
already-running stack (much faster to iterate against). Anything they point at will be
TRUNCATEd and FLUSHDBed between tests, so never point them at a database you care about.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ledgerloop.config import Settings, get_settings
from ledgerloop.db.session import build_engine, build_sessionmaker
from ledgerloop.queue.streams import StreamClient, build_redis

TABLES = (
    "exceptions",
    "reconciliation_results",
    "outbox_events",
    "gateway_transactions",
    "ledger_entries",
)


@dataclass(frozen=True)
class Infra:
    database_url: str
    redis_url: str


@pytest.fixture(scope="session")
def infra() -> Iterator[Infra]:
    env_db = os.getenv("LEDGERLOOP_TEST_DATABASE_URL")
    env_redis = os.getenv("LEDGERLOOP_TEST_REDIS_URL")
    if env_db and env_redis:
        yield Infra(env_db, env_redis)
        return

    from testcontainers.community.postgres import PostgresContainer
    from testcontainers.community.redis import RedisContainer

    # Pinned to the versions in docker-compose: testing against a different major than
    # we deploy would make the suite's guarantees about PG-specific behaviour hollow.
    with (
        PostgresContainer("postgres:15-alpine", driver="psycopg") as postgres,
        RedisContainer("redis:7-alpine") as redis,
    ):
        database_url = (
            f"postgresql+asyncpg://{postgres.username}:{postgres.password}"
            f"@{postgres.get_container_host_ip()}:{postgres.get_exposed_port(5432)}"
            f"/{postgres.dbname}"
        )
        redis_url = f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"
        yield Infra(database_url, redis_url)


@pytest.fixture(scope="session")
def migrated(infra: Infra) -> Infra:
    """Apply the migration chain once per session."""
    from alembic.config import Config

    from alembic import command

    os.environ["LEDGERLOOP_DATABASE_URL"] = infra.database_url
    os.environ["LEDGERLOOP_REDIS_URL"] = infra.redis_url
    get_settings.cache_clear()

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", infra.database_url)
    command.upgrade(config, "head")
    return infra


@pytest.fixture
def settings(migrated: Infra) -> Settings:
    """Defaults tuned for tests: short blocks and short windows so a test that must
    observe the sweeper does not have to wait five real minutes to do it."""
    return Settings(
        database_url=migrated.database_url,
        redis_url=migrated.redis_url,
        stream_block_ms=150,
        sweep_interval_s=1,
        unmatched_after_s=1,
        outbox_poll_interval_s=0.02,
        claim_interval_s=0.05,
        claim_min_idle_ms=200,
        log_json=False,
        log_level="WARNING",
    )


@pytest_asyncio.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = build_engine(settings)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_sessionmaker(engine)


@pytest_asyncio.fixture
async def session(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessions() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def clean_state(engine: AsyncEngine, settings: Settings) -> AsyncIterator[None]:
    """Truncate tables and flush Redis before every test.

    RESTART IDENTITY so id sequences begin at 1 in each test: assertions about keyset
    pagination and cursors stay readable, and a test cannot accidentally depend on ids
    left behind by whatever ran before it.
    """
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    redis = build_redis(settings)
    await redis.flushdb()
    await redis.aclose()
    yield


@pytest_asyncio.fixture
async def redis_client(settings: Settings):  # type: ignore[no-untyped-def]
    redis = build_redis(settings)
    yield redis
    await redis.aclose()


@pytest_asyncio.fixture
async def stream(redis_client, settings: Settings) -> StreamClient:  # type: ignore[no-untyped-def]
    client = StreamClient(redis_client, settings)
    await client.ensure_group()
    return client


@pytest_asyncio.fixture
async def api(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """An in-process API client with the real lifespan run.

    ASGITransport does not run lifespan events on its own, so the app is entered
    explicitly -- otherwise app.state would be empty and every dependency would fail in
    a way that has nothing to do with the endpoint under test.
    """
    from ledgerloop.api.app import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ledgerloop.test") as c:
            yield c
