"""Alembic environment.

Migrations run against VINGO_DATABASE_MIGRATION_URL (session/direct, :5432),
NOT the runtime URL (transaction pooler, :6543). The pooler cannot serve DDL
and advisory locks reliably. See docs/supabase.md.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from vingo_scraper.db.models import Base
from vingo_scraper.db.session import ensure_compatible_event_loop, migration_url

# psycopg3 async cannot run on Windows' default ProactorEventLoop.
ensure_compatible_event_loop()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# alembic.ini is read by configparser, which treats '%' as interpolation
# syntax. A percent-encoded password (Supabase requires encoding '@' as %40)
# therefore explodes unless the '%' is doubled on the way in. SQLAlchemy sees
# the correctly single-escaped value after configparser unescapes it.
config.set_main_option("sqlalchemy.url", migration_url().replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
