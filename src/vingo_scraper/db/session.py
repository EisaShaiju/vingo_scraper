"""Engine and session construction.

Read docs/supabase.md before changing anything here. Two settings in this file
exist solely to survive Supabase's transaction pooler, and getting either wrong
produces failures that only appear under concurrency -- clean in dev, broken in
production.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from vingo_scraper.config import settings

log = structlog.get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def ensure_compatible_event_loop() -> None:
    """Windows ships ProactorEventLoop, which psycopg3 cannot drive in async
    mode ("Psycopg cannot use the 'ProactorEventLoop'").

    Must be called BEFORE asyncio.run(), so every sync entry point that will
    touch the database invokes it first. Harmless everywhere else.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def is_pooled(url: str) -> bool:
    """Is this URL pointing at a connection pooler rather than Postgres?

    Both of the workarounds below are correct through Supavisor and wrong
    against a plain server, so the engine has to know which it is talking to.
    """
    return ":6543" in url or "pooler." in url


def build_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    """Build the engine, configured for whichever side of a pooler we are on.

    **Through a pooler** (Supabase/Supavisor), two workarounds are required:

    1. `prepare_threshold=None` DISABLES psycopg3's prepared statements.
       Supavisor multiplexes connections, so a statement prepared on one
       backend is absent on the next. psycopg prepares automatically after a
       few executions, so the failure appears only once a query gets hot --
       passing tests, then intermittent `prepared statement "_pg3_N" does not
       exist` under load. `None` disables; `0` means "prepare immediately",
       the exact opposite.
    2. `NullPool`, because the pooler already is the pool. Layering
       SQLAlchemy's pool on top holds server-side connections open and burns
       the connection budget for no benefit.

    **Against Postgres directly** (the VM), both are actively harmful. There
    is no pooler to confuse, so prepared statements are free speed; and
    `NullPool` would open a fresh TCP connection and re-authenticate for every
    single request in a long-running process. A real pool is the whole point
    of having the database on localhost.
    """
    url = url or settings.database_url
    if not url:
        raise RuntimeError(
            "VINGO_DATABASE_URL is not set -- see docs/vm-deployment.md"
        )

    connect_args, kwargs = engine_options(url)
    return create_async_engine(url, echo=echo, connect_args=connect_args, **kwargs)


def engine_options(url: str) -> tuple[dict, dict]:
    """(connect_args, create_async_engine kwargs) for this URL.

    Split out so the choice is assertable directly. SQLAlchemy does not expose
    `connect_args` back off a built engine, and a test that reaches into its
    internals to check would break on an upgrade while proving little.
    """
    # Fail fast rather than hanging the console for 30s when the database is
    # unreachable: the UI has to learn quickly so it can disable the controls
    # that would spend money.
    connect_args: dict = {"connect_timeout": 5}
    kwargs: dict = {}

    if is_pooled(url):
        connect_args["prepare_threshold"] = None
        kwargs["poolclass"] = NullPool
    else:
        kwargs.update(
            pool_size=5,
            max_overflow=5,
            # Recycle before anything upstream decides a connection is idle.
            pool_recycle=1800,
            pool_pre_ping=True,
        )
    return connect_args, kwargs


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    if _sessionmaker is None:
        _engine = build_engine()
        _sessionmaker = async_sessionmaker(
            _engine, class_=AsyncSession, expire_on_commit=False
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope. Commits on success, rolls back on error."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def migration_url() -> str:
    """Synchronous URL for Alembic.

    Migrations need a real session (DDL, advisory locks), which the transaction
    pooler cannot give us -- hence a separate URL on :5432. Falls back to the
    runtime URL only so a local Postgres works without extra config; against
    Supabase the two must differ.
    """
    url = settings.database_migration_url or settings.database_url
    if not url:
        raise RuntimeError("no database URL configured for migrations")
    # Returned as-is: psycopg3 (postgresql+psycopg://) drives both sync and
    # async engines, and alembic/env.py runs it through an async engine.
    return url
