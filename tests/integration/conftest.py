"""Integration fixtures backed by a real PostgreSQL with pgvector.

`postgres_url` builds its schema by running the real Alembic migration
chain (`alembic upgrade head`), not `Base.metadata.create_all`. The
migration is hand-maintained for two kinds of change autogenerate can't
see at all -- CHECK constraint bodies, and the three `set_updated_at`
triggers (see CLAUDE.md's Commands section and the migration's own
comments) -- so a suite that never actually executes it can drift from
`Base.metadata` with nothing to notice. Verified directly: against a
`create_all`-built schema, `SELECT tgname FROM pg_trigger WHERE NOT
tgisinternal` returns nothing -- the triggers whose own migration comment
calls them "what actually guarantees updated_at reflects every write,
regardless of how it was made" had never once run in this suite (see
test_migrations.py).

Running the real migration is real DDL against a real database, much more
expensive than `create_all`/`drop_all` against a from-scratch schema in an
empty one, so it runs once per test session (`postgres_url` is
session-scoped) instead of once per test. Each test still gets a fully
isolated database: `session` opens its own connection, starts a
transaction, and binds a plain `AsyncSession` to it. SQLAlchemy's default
`join_transaction_mode` ("conditional_savepoint") resolves to
"rollback_only" for a connection that already has a plain, non-nested
transaction open, so the session's own flush()/begin_nested() calls all
participate in that one transaction (verified directly, including that
PostgresTitleRepository's own begin_nested() SAVEPOINTs nest correctly
inside it) -- and the whole thing is rolled back afterward, undoing
everything the test did. That replaces 23 full DDL cycles (create every
table, index, and constraint; drop them all again) with one DDL cycle plus
23 cheap connect/transaction/rollback cycles. It also means no fixture
holds mutable state across tests -- each test's isolation comes entirely
from its own connection and transaction, never from resetting something
shared -- which is what would matter for running this suite under
pytest-xdist, should that ever get adopted.
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.command import upgrade
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession

from usher.config import get_settings
from usher.db import models  # noqa: F401  — registers all tables
from usher.db.base import build_engine, build_session_factory

_THIS_DIR = Path(__file__).parent
_ALEMBIC_INI = _THIS_DIR.parent.parent / "alembic.ini"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Every test collected under tests/integration/ needs Docker (this
    directory's whole reason to exist -- see the module docstring). Marking
    it here, once, means Task 10's own literal test functions below don't
    each need a hand-applied `@pytest.mark.integration`, and neither will
    any test a future task adds to this directory. `pytest -m integration`
    / `pytest -m "not integration"` then work as a marker-based equivalent
    of the tests/unit vs tests/integration directory split, for tooling
    (Group F/G's CI) that would rather filter by `-m` than by path.

    `pytest_collection_modifyitems` is *not* directory-scoped the way a
    fixture would be -- pytest calls every conftest.py's implementation of
    this hook with the *entire* session's `items`, not just the ones
    collected from this hook's own directory (verified directly: an
    earlier, unguarded `for item in items: item.add_marker(...)` here
    marked all ~194 tests "integration", including every test under
    tests/unit/ -- `-m integration` selected the whole suite and
    `-m "not integration"` selected nothing). The explicit path check below
    is what actually scopes this to tests/integration/.
    """
    for item in items:
        if item.path.is_relative_to(_THIS_DIR):
            item.add_marker(pytest.mark.integration)


def _upgrade_head(database_url: str) -> None:
    """Runs the real migration chain against a freshly-started container --
    see the module docstring. `env.py` (deliberately -- see its own
    docstring) reads the URL from `usher.config.get_settings()`, never from
    `alembic.ini`, so driving it here means setting the env vars a real
    `alembic upgrade head` invocation would have had, exactly as far as
    `Settings` needs: `USHER_DATABASE_URL` and `USHER_SECRET_KEY` (both
    required, neither has a default). Every `USHER_*`/`OTEL_*` variable is
    saved and restored around the call -- the same isolation
    `tests/conftest.py`'s `clean_environment` gives every test, which
    doesn't help here since this fixture (session scope) runs before that
    one (function scope) ever does for the first test that needs it.
    """
    saved = {key: value for key, value in os.environ.items() if key.startswith(("USHER_", "OTEL_"))}
    for key in saved:
        del os.environ[key]
    os.environ["USHER_DATABASE_URL"] = database_url
    os.environ["USHER_SECRET_KEY"] = "0" * 32
    get_settings.cache_clear()
    try:
        upgrade(Config(str(_ALEMBIC_INI)), "head")
    finally:
        for key in list(os.environ):
            if key.startswith(("USHER_", "OTEL_")):
                del os.environ[key]
        os.environ.update(saved)
        get_settings.cache_clear()


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    # Local import: testcontainers.postgres itself raises a
    # DeprecationWarning at import time (superseded by
    # testcontainers.community.postgres). Deferring the import until this
    # fixture actually runs keeps that warning from firing during
    # collection alone -- `pytest -m "not integration"` still imports this
    # whole conftest module even though it filters every test in it back
    # out, and previously paid the warning for that alone.
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(
        "pgvector/pgvector:pg17",
        username="usher",
        password="usher",  # noqa: S106 -- throwaway container credential, torn down with the container
        dbname="usher",
    ) as pg:
        url = pg.get_connection_url().replace("psycopg2", "asyncpg")
        _upgrade_head(url)
        yield url


@pytest_asyncio.fixture
async def session(postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    async with engine.connect() as conn:
        await conn.begin()
        async with factory(bind=conn) as s:
            yield s
        await conn.rollback()
    await engine.dispose()
