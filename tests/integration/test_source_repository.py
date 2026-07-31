"""PostgresSourceRepository against real Postgres."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.source_repository_contract import SourceRepositoryContract, _source
from usher.db.repositories.source import PostgresSourceRepository
from usher.ports.errors import RepositoryConflict


class TestPostgresSourceRepositoryContract(SourceRepositoryContract):
    @pytest.fixture
    def repo(self, session: AsyncSession) -> PostgresSourceRepository:
        return PostgresSourceRepository(session)


async def test_the_session_survives_a_conflict(session: AsyncSession) -> None:
    """The SAVEPOINT, not just the translation: without it Postgres leaves
    the whole transaction aborted and the caller's very next statement
    raises PendingRollbackError instead of running. `SourceService.register`
    is exactly such a caller -- it writes the credential on this same
    session immediately afterwards."""
    repo = PostgresSourceRepository(session)
    source = _source()
    await repo.add(source)
    with pytest.raises(RepositoryConflict):
        await repo.add(source)
    assert await repo.get(source.id) is not None
