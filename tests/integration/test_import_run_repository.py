"""PostgresImportRunRepository against real Postgres."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from tests.contract.import_run_repository_contract import ImportRunRepositoryContract
from usher.db.base import build_engine, build_session_factory
from usher.db.models.bootstrap import ImportRunRow
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import ImportRun
from usher.ports.errors import RepositoryConflict


class _Releasing(PostgresImportRunRepository):
    """Remembers every dataset it held, so the fixture can give each hold and read back.

    A hold is a checked-out connection holding an advisory lock, and one left behind
    would refuse the next case's `start()` of the same dataset.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.started: set[str] = set()

    async def hold(self, dataset: str) -> None:
        await super().hold(dataset)
        self.started.add(dataset)

    async def release_all(self) -> None:
        for dataset in self.started:
            await self.release(dataset)
        await self.release_reads()


class TestPostgresImportRunRepositoryContract(ImportRunRepositoryContract):
    @pytest.fixture
    async def runs(self, session: AsyncSession) -> AsyncIterator[PostgresImportRunRepository]:
        repository = _Releasing(session)
        yield repository
        await repository.release_all()

    @pytest.fixture
    async def rival(self, session: AsyncSession) -> AsyncIterator[PostgresImportRunRepository]:
        repository = _Releasing(session)
        yield repository
        await repository.release_all()


async def test_a_second_run_row_for_one_dataset_is_a_port_error(
    session: AsyncSession,
) -> None:
    """Uq_import_runs_dataset enforces one checkpoint per dataset.

    Two processes bootstrapping the same dataset is an operator mistake, and it must
    surface as RepositoryConflict -- a raw sqlalchemy.exc.IntegrityError escaping here
    would break "db is driven, not driving" the same way it would in
    PostgresTitleRepository.
    """
    runs = _Releasing(session)
    try:
        await runs.start("imdb.title.basics", "etag-1")
        with pytest.raises(RepositoryConflict) as exc_info:
            await runs.save(ImportRun(dataset="imdb.title.basics", revision="etag-9"))
    finally:
        await runs.release_all()
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
            winner_repo = PostgresImportRunRepository(winner)
            winner_run = await winner_repo.start("race.dataset", "etag-1")
            await winner.commit()
            await winner_repo.release("race.dataset")

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
    await runs.release("wikidata.crosswalk")
    await runs.save(run.evolve(position=17, rows_seen=1234, rows_written=1200))
    fetched = await runs.get("wikidata.crosswalk")
    assert fetched is not None
    assert (fetched.position, fetched.rows_seen, fetched.rows_written) == (17, 1234, 1200)
    assert fetched.id == run.id
    assert fetched.started_at.tzinfo is not None


_NAMESPACE = 0x75736872


async def _free(engine: AsyncEngine, dataset: str, *, within: float = 5.0) -> bool:
    """Whether another connection can take `dataset`'s lock within `within` seconds.

    A try-lock, given straight back, rather than a read of `pg_locks`: it names the lock
    the way the repository takes it, so it cannot share a mistake in reading one. Polled,
    because a closed backend lets go of its locks a moment after the client hangs up.
    """
    deadline = asyncio.get_running_loop().time() + within
    async with engine.connect() as probe:
        await probe.execution_options(isolation_level="AUTOCOMMIT")
        while True:
            # `execute`, not `scalar`: the case below replaces `AsyncConnection.scalar`.
            taken = await probe.execute(
                text("SELECT pg_try_advisory_lock(:ns, hashtext(:dataset))"),
                {"ns": _NAMESPACE, "dataset": dataset},
            )
            if taken.scalar_one():
                await probe.execute(
                    text("SELECT pg_advisory_unlock(:ns, hashtext(:dataset))"),
                    {"ns": _NAMESPACE, "dataset": dataset},
                )
                return True
            if asyncio.get_running_loop().time() > deadline:
                return False
            await asyncio.sleep(0.05)


class _Interrupted(BaseException):
    """What a cancellation or a Ctrl-C delivers into an `await`: not an `Exception`."""


@pytest.mark.parametrize("take", ["start", "hold_for_reading"])
async def test_a_hold_interrupted_after_its_lock_was_granted_leaves_no_lock_in_the_pool(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch, take: str
) -> None:
    """`close()` hands the connection back to the pool with the advisory lock still on it.

    The next checkout of that connection -- by anything at all -- then holds the dataset
    for as long as the pool keeps it, and every `start()` elsewhere is refused. The hold
    invalidates it instead, as `release()` does, so its backend ends and the lock with it.
    A read's shared lock refuses every `start()` the same way.
    """
    engine = build_engine(postgres_url, pool_size=1, max_overflow=0)
    factory = build_session_factory(engine)
    granted: list[bool] = []
    original = AsyncConnection.scalar

    async def granted_then_interrupted(
        self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        result = await original(self, statement, *args, **kwargs)
        if "pg_try_advisory_lock" in str(statement):
            granted.append(bool(result) and not await _free(engine_b, _PROBE, within=0))
            raise _Interrupted
        return result

    engine_b = build_engine(postgres_url)
    try:
        async with factory() as session:
            runs = PostgresImportRunRepository(session)
            monkeypatch.setattr(AsyncConnection, "scalar", granted_then_interrupted)
            try:
                with pytest.raises(_Interrupted):
                    if take == "start":
                        await runs.start(_PROBE, "etag-1")
                    else:
                        await runs.hold_for_reading(_PROBE)
            finally:
                monkeypatch.undo()
        assert granted == [True], "the premise: the lock was granted before the interruption"
        assert await _free(engine_b, _PROBE), "the pooled connection kept the dataset's lock"
    finally:
        await engine.dispose()
        await engine_b.dispose()


@pytest.mark.parametrize("give_back", ["release", "release_reads"])
async def test_a_give_back_interrupted_before_its_unlock_leaves_no_lock_in_the_pool(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch, give_back: str
) -> None:
    """The twin of the case above, on the way out: the unlock never ran.

    Closed, the connection would go back to the pool with the lock still on it, so a
    give-back that fails ends the backend instead.
    """
    engine = build_engine(postgres_url, pool_size=1, max_overflow=0)
    factory = build_session_factory(engine)
    held: list[bool] = []
    original = AsyncConnection.execute

    async def interrupted_before_unlocking(
        self: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if self.sync_engine is engine.sync_engine and "pg_advisory_unlock" in str(statement):
            held.append(not await _free(engine_b, _PROBE, within=0))
            raise _Interrupted
        return await original(self, statement, *args, **kwargs)

    engine_b = build_engine(postgres_url)
    try:
        async with factory() as session:
            runs = PostgresImportRunRepository(session)
            if give_back == "release":
                await runs.hold(_PROBE)
            else:
                assert await runs.hold_for_reading(_PROBE) is True
            monkeypatch.setattr(AsyncConnection, "execute", interrupted_before_unlocking)
            try:
                with pytest.raises(_Interrupted):
                    if give_back == "release":
                        await runs.release(_PROBE)
                    else:
                        await runs.release_reads()
            finally:
                monkeypatch.undo()
        assert held == [True], "the premise: the lock was still held when the unlock failed"
        assert await _free(engine_b, _PROBE), "the pooled connection kept the dataset's lock"
    finally:
        await engine.dispose()
        await engine_b.dispose()


_PROBE = "movielens.genome"


async def test_touch_confirms_a_hold_and_a_read_whose_keys_hash_negative(
    session: AsyncSession,
) -> None:
    """`pg_locks` shows each key as an unsigned `oid`; `hashtext` is a signed `int4`.

    Read with the wrong sign, every dataset whose name hashes negative -- `movielens.genome`
    among them -- is never seen held, and `touch` would stop a live import as lost. A read
    takes the very key a hold does, of either sign, so each refuses the other.
    """
    runs, rival = _Releasing(session), _Releasing(session)
    try:
        signs = await session.scalar(
            text("SELECT array[hashtext(:negative) < 0, hashtext(:positive) > 0]"),
            {"negative": _PROBE, "positive": "imdb.title.basics"},
        )
        assert signs == [True, True], "the premise: one key of each sign"
        await runs.hold(_PROBE)
        assert await rival.hold_for_reading(_PROBE) is False
        await runs.touch(_PROBE)
        await rival.hold("imdb.title.basics")
        assert await rival.hold_for_reading("tmdb.ids.movie") is True
        await runs.release(_PROBE)
        assert await rival.hold_for_reading(_PROBE) is True
        await rival.touch("imdb.title.basics")
        with pytest.raises(RepositoryConflict):
            await runs.hold(_PROBE)
    finally:
        await runs.release_all()
        await rival.release_all()


async def _end_the_backend_holding(engine: AsyncEngine, dataset: str) -> int:
    """End whichever backend holds `dataset`, as `idle_session_timeout` would; how many."""
    async with engine.connect() as killer:
        ended = await killer.scalar(
            text(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_locks "
                "WHERE locktype = 'advisory' AND classid = CAST(:ns AS oid) "
                "AND objid = CAST(hashtext(:dataset) AS oid) AND objsubid = 2"
            ),
            {"ns": _NAMESPACE, "dataset": dataset},
        )
    return int(ended or 0)


async def test_a_hold_whose_backend_ended_is_refused_by_touch_and_taken_again_by_hold(
    session: AsyncSession, postgres_url: str
) -> None:
    """`idle_session_timeout` ends the hold's connection, and the lock goes with it.

    Nothing noticed: the import carried on as the holder of a dataset anybody could now
    take. `touch` now confirms the hold on its own connection, so it refuses, and drops
    it; `hold` then takes the dataset afresh.
    """
    runs = _Releasing(session)
    engine = build_engine(postgres_url)
    try:
        await runs.start(_PROBE, "etag-1")
        await runs.touch(_PROBE)
        ended = await _end_the_backend_holding(engine, _PROBE)
        assert ended == 1, "the premise: exactly one backend held the lock, and it ended"
        with pytest.raises(RepositoryConflict, match="lost the hold"):
            await runs.touch(_PROBE)
        assert await _free(engine, _PROBE), "the dataset is anybody's"
        await runs.hold(_PROBE)
        assert not await _free(engine, _PROBE, within=0), "taken again"
    finally:
        await runs.release_all()
        await engine.dispose()


@pytest.mark.parametrize("give_back", ["release", "release_reads"])
async def test_a_lock_whose_backend_ended_is_given_back_without_a_raise(
    session: AsyncSession, postgres_url: str, give_back: str
) -> None:
    """A server restart ends the backend, and every lock on it with it.

    Unlocking on that connection raised the disconnect out of the `finally` that gives a
    hold or a phase's reads back, over whatever the import had recorded. The lock is
    already gone, so there is nothing left to give back.
    """
    runs = _Releasing(session)
    engine = build_engine(postgres_url)
    try:
        if give_back == "release":
            await runs.hold(_PROBE)
        else:
            assert await runs.hold_for_reading(_PROBE) is True
            assert await runs.hold_for_reading("imdb.title.basics") is True
        ended = await _end_the_backend_holding(engine, _PROBE)
        assert ended == 1, "the premise: exactly one backend held the lock, and it ended"
        try:
            if give_back == "release":
                await runs.release(_PROBE)
            else:
                await runs.release_reads()
        except DBAPIError as exc:
            pytest.fail(f"giving back a lock whose backend ended raised: {exc!r}")
        assert await _free(engine, _PROBE), "the dataset is anybody's"
    finally:
        await runs.release_all()
        await engine.dispose()


async def test_a_hold_that_ended_unnoticed_is_taken_again_by_the_next_hold(
    session: AsyncSession, postgres_url: str
) -> None:
    """A failure is recorded by taking the hold, and the one it finds may be long dead.

    Kept as it was, the failure would be written by a process holding nothing; `hold`
    confirms a hold it already has, and takes a dead one again.
    """
    runs = _Releasing(session)
    engine = build_engine(postgres_url)
    try:
        await runs.hold(_PROBE)
        ended = await _end_the_backend_holding(engine, _PROBE)
        assert ended == 1, "the premise: exactly one backend held the lock, and it ended"
        await runs.hold(_PROBE)
        await runs.touch(_PROBE)
        assert not await _free(engine, _PROBE, within=0), "taken again"
    finally:
        await runs.release_all()
        await engine.dispose()


async def test_a_read_whose_backend_ended_is_refused_by_touch_and_every_read_dropped(
    session: AsyncSession, postgres_url: str
) -> None:
    """The reads' connection ends the way a hold's does, and a phase went on joining.

    Against a dataset any process could by then be importing. `touch` confirms the reads
    beside the hold, and a lost one drops them all -- they were on the one connection.
    """
    runs = _Releasing(session)
    engine = build_engine(postgres_url)
    try:
        await runs.start("imdb.credit_names", "etag-1")
        assert await runs.hold_for_reading(_PROBE) is True
        assert await runs.hold_for_reading("imdb.title.basics") is True
        await runs.touch("imdb.credit_names")
        ended = await _end_the_backend_holding(engine, _PROBE)
        assert ended == 1, "the premise: exactly one backend read the dataset, and it ended"
        with pytest.raises(RepositoryConflict) as lost:
            await runs.touch("imdb.credit_names")
        assert str(lost.value) == (
            "lost the shared hold on imdb.title.basics, which this reads: its connection ended"
        ), "the first read confirmed, on the one connection both were on"
        assert await _free(engine, _PROBE), "the dataset is anybody's"
        assert await _free(engine, "imdb.title.basics"), "and so is every other read"
        await runs.touch("imdb.credit_names")
    finally:
        await runs.release_all()
        await engine.dispose()
