"""Integration fixtures backed by a real PostgreSQL with pgvector."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.postgres import PostgresContainer

from usher.db import models  # noqa: F401  — registers all tables
from usher.db.base import Base, build_engine, build_session_factory

_THIS_DIR = Path(__file__).parent


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


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    with PostgresContainer(
        "pgvector/pgvector:pg17",
        username="usher",
        password="usher",  # noqa: S106 -- throwaway container credential, torn down with the container
        dbname="usher",
    ) as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session(postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = build_engine(postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = build_session_factory(engine)
    async with factory() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
