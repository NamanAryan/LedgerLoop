"""Alembic environment. Async (asyncpg) so migrations use the same driver as the app.

There is no ``Base.metadata.create_all()`` anywhere in this project. The migration
chain is the only thing that has ever created a table, in dev, in test, and in prod,
so "works on my machine" and "works after deploy" are the same code path.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from alembic import context
from ledgerloop.config import get_settings
from ledgerloop.db import models  # noqa: F401  -- import registers every table on Base
from ledgerloop.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", str(get_settings().database_url))

target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # Native enums are created explicitly in migrations, never auto-emitted.
        include_object=lambda obj, name, type_, reflected, parent: True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    _configure(connection)
    # DDL runs in one transaction: a failed migration leaves the schema untouched
    # rather than half-applied. PostgreSQL supports transactional DDL; use it.
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
