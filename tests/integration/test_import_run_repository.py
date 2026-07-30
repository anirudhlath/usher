"""PostgresImportRunRepository against real Postgres."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.import_run_repository_contract import ImportRunRepositoryContract
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import ImportRun
from usher.ports.errors import RepositoryConflict


class TestPostgresImportRunRepositoryContract(ImportRunRepositoryContract):
    @pytest.fixture
    def runs(self, session: AsyncSession) -> PostgresImportRunRepository:
        return PostgresImportRunRepository(session)


async def test_a_second_run_row_for_one_dataset_is_a_port_error(
    session: AsyncSession,
) -> None:
    """uq_import_runs_dataset enforces one checkpoint per dataset. Two
    processes bootstrapping the same dataset is an operator mistake, and it
    must surface as RepositoryConflict -- a raw sqlalchemy.exc.IntegrityError
    escaping here would break "db is driven, not driving" the same way it
    would in PostgresTitleRepository."""
    runs = PostgresImportRunRepository(session)
    await runs.start("imdb.title.basics", "etag-1")
    with pytest.raises(RepositoryConflict) as exc_info:
        await runs.save(ImportRun(dataset="imdb.title.basics", revision="etag-9"))
    assert exc_info.value.constraint == "uq_import_runs_dataset"


async def test_round_trips_every_field(session: AsyncSession) -> None:
    """_to_domain feeds all 11 columns into model_validate under
    extra="forbid" -- a column added without a matching field fails here,
    loudly, rather than being dropped."""
    runs = PostgresImportRunRepository(session)
    run = await runs.start("wikidata.crosswalk", "2026-07-30")
    await runs.save(run.evolve(position=17, rows_seen=1234, rows_written=1200))
    fetched = await runs.get("wikidata.crosswalk")
    assert fetched is not None
    assert (fetched.position, fetched.rows_seen, fetched.rows_written) == (17, 1234, 1200)
    assert fetched.id == run.id
    assert fetched.started_at.tzinfo is not None
