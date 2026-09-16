"""PostgresImportRunRepository against real Postgres."""

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.import_run_repository_contract import ImportRunRepositoryContract
from usher.db.base import build_engine, build_session_factory
from usher.db.models.bootstrap import ImportRunRow
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
    """Uq_import_runs_dataset enforces one checkpoint per dataset.

    Two processes bootstrapping the same dataset is an operator mistake, and it must
    surface as RepositoryConflict -- a raw sqlalchemy.exc.IntegrityError escaping here
    would break "db is driven, not driving" the same way it would in
    PostgresTitleRepository.
    """
    runs = PostgresImportRunRepository(session)
    await runs.start("imdb.title.basics", "etag-1")
    with pytest.raises(RepositoryConflict) as exc_info:
        await runs.save(ImportRun(dataset="imdb.title.basics", revision="etag-9"))
    assert exc_info.value.constraint == "uq_import_runs_dataset"


async def test_the_session_survives_a_conflict_for_the_callers_next_statement(
    postgres_url: str,
) -> None:
    """Pins the bug against a real two-process race.

    `save()` translated the `uq_import_runs_dataset` IntegrityError to
    `RepositoryConflict` correctly (see the test above), but never rolled back -- so
    Postgres left the *session's* transaction aborted, and the very next statement on it
    raised.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    try:
        async with factory() as winner, factory() as loser:
            # "winner" claims the dataset first and really commits -- the
            # process that won the race.
            winner_run = await PostgresImportRunRepository(winner).start("race.dataset", "etag-1")
            await winner.commit()

            # "loser" replays exactly what its own start() does the instant it --
            # correctly, at the moment it checked -- believed no row existed yet for
            # this dataset: build a fresh ImportRun (a new id) and save() it.
            loser_repo = PostgresImportRunRepository(loser)
            with pytest.raises(RepositoryConflict) as exc_info:
                await loser_repo.save(ImportRun(dataset="race.dataset", revision="etag-1"))
            assert exc_info.value.constraint == "uq_import_runs_dataset"

            # The bug: without the fix in save()'s except block, this next
            # call on the *same* session raised PendingRollbackError instead
            # of returning -- Postgres leaves a session's transaction
            # aborted after an uncaught statement error until an explicit
            # rollback, and save() never issued one.
            recovered = await loser_repo.get("race.dataset")
            assert recovered is not None
            # It's the *winner's* row -- the loser never got one of its own,
            # which is exactly the state BootstrapService's except handler
            # needs to see to record a FAILED run without inventing a
            # duplicate that would itself violate uq_import_runs_dataset.
            assert recovered.id == winner_run.id
    finally:
        async with factory() as cleanup:
            await cleanup.execute(
                delete(ImportRunRow).where(ImportRunRow.dataset == "race.dataset")
            )
            await cleanup.commit()
        await engine.dispose()


async def test_round_trips_every_field(session: AsyncSession) -> None:
    """_to_domain feeds all 11 columns into model_validate under extra="forbid".

    a column added without a matching field fails here, loudly, rather than being
    dropped.
    """
    runs = PostgresImportRunRepository(session)
    run = await runs.start("wikidata.crosswalk", "2026-07-30")
    await runs.save(run.evolve(position=17, rows_seen=1234, rows_written=1200))
    fetched = await runs.get("wikidata.crosswalk")
    assert fetched is not None
    assert (fetched.position, fetched.rows_seen, fetched.rows_written) == (17, 1234, 1200)
    assert fetched.id == run.id
    assert fetched.started_at.tzinfo is not None
