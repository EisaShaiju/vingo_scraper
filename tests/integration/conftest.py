"""A real Postgres, because the offline suite cannot see the ORM at all.

345 tests passed while the console returned an empty catalog twice. Both bugs
lived at boundaries this suite could not express:

  1. three columns declared on `Seller` instead of `Listing` -- the migration
     was right, the mapped class was wrong, and nothing ever asked the mapped
     class a question;
  2. `lead_score` reading `listing.images`, a lazy relationship, which raises
     MissingGreenlet under the async engine.

Neither is a gap in care. With no engine anywhere in `tests/`, every
async-engine behaviour -- lazy loads, `selectinload` correctness, identity-map
staleness -- is a class of failure the offline suite *cannot* express.

**sqlite cannot substitute.** The schema uses JSONB, native enums and UUID
columns, and the bugs are specific to the async Postgres driver.

What is actually required is a **real Postgres** we may create and drop tables
in, with the migrations applied by `alembic` rather than by `create_all()` --
building the schema from the models would let a wrong model pass, which is
bug 1 tested in a way that cannot fail.

Docker is one way to get that, not the requirement. In order of preference:

    VINGO_TEST_DATABASE_URL=postgresql+psycopg://...  pytest tests/integration
        any Postgres you point it at -- a scratch database on the VM, a local
        install, a colleague's. Nothing to install.

    pytest tests/integration -q
        falls back to a disposable postgres:16 container if Docker is running.

    pytest tests/ -q --ignore=tests/integration
        the offline suite, unchanged and fast.

With neither a URL nor Docker these skip, so nobody's `pytest` run breaks.

**Point VINGO_TEST_DATABASE_URL at a scratch database, never at `comet`.** The
fixture drops and recreates the public schema on every session.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid

import pytest

IMAGE = "postgres:16-alpine"
PORT = 55432          # not 5432: must not collide with a local Postgres
PASSWORD = "integration-only-never-a-real-secret"  # noqa: S105
DB = "comet_test"
CONTAINER = f"comet-itest-{uuid.uuid4().hex[:8]}"

URL = f"postgresql+psycopg://postgres:{PASSWORD}@127.0.0.1:{PORT}/{DB}"


def _docker() -> str | None:
    exe = shutil.which("docker")
    if not exe:
        return None
    try:
        subprocess.run([exe, "info"], capture_output=True, timeout=20, check=True)
    except Exception:
        return None       # installed but not running
    return exe


@pytest.fixture(scope="session")
def pg_url() -> str:
    """A Postgres with every migration applied.

    An explicitly supplied database wins: it needs no Docker, and it can be the
    same Postgres 16 production runs on, which is a closer match than anything
    local.
    """
    given = os.environ.get("VINGO_TEST_DATABASE_URL", "").strip()
    if given:
        if given.rsplit("/", 1)[-1].split("?")[0] in {"comet", "postgres"}:
            pytest.fail(
                "VINGO_TEST_DATABASE_URL points at a real database. This "
                "fixture DROPS the public schema -- point it at a scratch one, "
                "e.g. comet_test."
            )
        _reset_schema(given)
        _migrate(given)
        return given

    exe = _docker()
    if exe is None:
        pytest.skip(
            "no Postgres for integration tests -- set VINGO_TEST_DATABASE_URL "
            "to a scratch database, or start Docker"
        )

    subprocess.run(
        [exe, "run", "-d", "--rm", "--name", CONTAINER,
         "-e", f"POSTGRES_PASSWORD={PASSWORD}", "-e", f"POSTGRES_DB={DB}",
         "-p", f"{PORT}:5432", IMAGE],
        check=True, capture_output=True,
    )
    try:
        _wait_ready(exe)
        _migrate(URL)
        yield URL
    finally:
        subprocess.run([exe, "rm", "-f", CONTAINER], capture_output=True)


def _wait_ready(exe: str, timeout_s: int = 60) -> None:
    """Postgres accepts TCP before it accepts queries -- poll the real thing."""
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        done = subprocess.run(
            [exe, "exec", CONTAINER, "pg_isready", "-U", "postgres", "-d", DB],
            capture_output=True, text=True,
        )
        if done.returncode == 0:
            time.sleep(1.0)       # initdb restarts once after first readiness
            return
        last = (done.stdout or "") + (done.stderr or "")
        time.sleep(0.5)
    raise RuntimeError(f"postgres never became ready: {last[:200]}")


def _reset_schema(url: str) -> None:
    """Start from nothing, so a half-migrated leftover cannot mask a failure."""
    import psycopg

    sync = url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(sync, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _migrate(url: str) -> None:
    """Run alembic exactly as production does -- not `create_all`.

    `Base.metadata.create_all()` would build the schema from the models, so a
    model that disagreed with the migrations would still pass. That is bug 1,
    tested in a way that cannot fail. The migrations are the source of truth
    here precisely because the model was the thing that was wrong.
    """
    env = {**os.environ, "VINGO_DATABASE_URL": url, "VINGO_DATABASE_MIGRATION_URL": url}
    done = subprocess.run(
        ["alembic", "upgrade", "head"],
        env=env, capture_output=True, text=True,
    )
    if done.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed:\n{done.stdout}\n{done.stderr}")


@pytest.fixture
async def session(pg_url):
    """An AsyncSession on the real database, rolled back after each test."""
    from sqlalchemy import NullPool
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    # NullPool: each test gets its own event loop, and a pooled connection
    # held across loops is its own class of confusing failure.
    engine = create_async_engine(pg_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()
    await engine.dispose()


@pytest.fixture
def wire_app(pg_url, monkeypatch):
    """Point the application's own session machinery at the test database.

    The API builds its engine from `settings`, so overriding the setting is
    what makes `_listings` run for real rather than against a stand-in -- which
    is the entire point: the monkeypatched `_listings` in the offline suite is
    what let both bugs through.
    """
    from vingo_scraper.config import settings
    from vingo_scraper.db import session as session_mod

    monkeypatch.setattr(settings, "database_url", pg_url, raising=False)
    monkeypatch.setattr(session_mod, "_engine", None, raising=False)
    monkeypatch.setattr(session_mod, "_sessionmaker", None, raising=False)
    yield pg_url
