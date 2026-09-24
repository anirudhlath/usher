"""BootstrapService against real Postgres: two processes on one dataset, and a stopped import.

Each case builds its own engine and deletes the checkpoint it committed.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Sequence

import pytest
from sqlalchemy import delete

from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from usher.db.base import build_engine, build_session_factory
from usher.db.models.bootstrap import ImportRunRow
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import BootstrapPhase, ImportRunStatus
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset
from usher.ports.events import NullEventPublisher
from usher.services.bootstrap import BootstrapService

_DATASET = "concurrency.probe"


class _Gated(BulkDataset[int]):
    """Two batches, whose first fetch waits on `gate` -- a download, held open.

    `fetching` is set as the fetch begins, and `fetched_at` records when.
    """

    def __init__(
        self, gate: asyncio.Event | None, fetching: asyncio.Event, revision: str = "etag-1"
    ) -> None:
        self._gate = gate
        self._fetching = fetching
        self._revision = revision
        self.fetched_at: float | None = None

    @property
    def name(self) -> str:
        return _DATASET

    @property
    def attribution(self) -> str:
        return "synthetic, never redistributed"

    async def revision(self) -> str:
        return self._revision

    def batches(
        self, *, resume_from: BulkCursor | None = None, revision: str | None = None
    ) -> AsyncIterator[BulkBatch[int]]:
        return self._iter(resume_from.position if resume_from else 0)

    async def _iter(self, start: int) -> AsyncIterator[BulkBatch[int]]:
        self.fetched_at = time.monotonic()
        self._fetching.set()
        if self._gate is not None:
            await self._gate.wait()
        for index in range(start, 2):
            yield BulkBatch(
                rows=(index,),
                cursor=BulkCursor(revision=self._revision, position=index + 1, rows_seen=index + 1),
            )

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def forget_probe(postgres_url: str) -> AsyncIterator[None]:
    """Deletes the checkpoint the case committed for real, however it ends."""
    yield
    engine = build_engine(postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(delete(ImportRunRow).where(ImportRunRow.dataset == _DATASET))
    finally:
        await engine.dispose()


def _service(runs: PostgresImportRunRepository, commit: object) -> BootstrapService:
    return BootstrapService(
        runs,
        FakeBulkCatalogRepository(),
        commit,  # type: ignore[arg-type]
        events=NullEventPublisher(),
        phase=BootstrapPhase.CROSSWALK,
    )


def _recorder(into: list[Sequence[int]]) -> object:
    async def write(rows: Sequence[int]) -> int:
        into.append(rows)
        return len(rows)

    return write


async def test_a_second_process_concedes_while_the_first_downloads_and_writes_nothing(
    postgres_url: str, forget_probe: None
) -> None:
    """The first holds the dataset from `start()` until it returns, so the second leaves it.

    Committing `start()` before the first fetch made the `RUNNING` row visible, and a
    second process adopted it and wrote every batch beside the first. Its whole import
    here runs inside the first one's download -- the windows are recorded and asserted
    to nest -- and it fetches nothing, writes nothing and returns the holder's row. Once
    the first returns, the hold is gone and a third run takes the checkpoint.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    gate, first_fetching, second_fetching = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first_written: list[Sequence[int]] = []
    second_written: list[Sequence[int]] = []
    try:
        async with factory() as first_session, factory() as second_session:
            first = _Gated(gate, first_fetching)
            first_task = asyncio.create_task(
                _service(
                    PostgresImportRunRepository(first_session), first_session.commit
                ).import_dataset(first, _recorder(first_written))  # type: ignore[arg-type]
            )
            await asyncio.wait_for(first_fetching.wait(), 5)

            second = _service(PostgresImportRunRepository(second_session), second_session.commit)
            second_began = time.monotonic()
            conceded = await asyncio.wait_for(
                second.import_dataset(
                    _Gated(None, second_fetching),
                    _recorder(second_written),  # type: ignore[arg-type]
                ),
                5,
            )
            second_ended = time.monotonic()
            released = time.monotonic()
            gate.set()
            finished = await asyncio.wait_for(first_task, 5)

            third = _service(PostgresImportRunRepository(second_session), second_session.commit)
            resumed = await third.import_dataset(
                _Gated(None, asyncio.Event()),
                _recorder(second_written),  # type: ignore[arg-type]
            )
    finally:
        await engine.dispose()

    assert first.fetched_at is not None
    assert first.fetched_at <= second_began <= second_ended <= released, (
        "the premise: the second import ran inside the first one's download",
        (first.fetched_at, second_began, second_ended, released),
    )
    assert not second_fetching.is_set(), "the second process fetched"
    assert second_written == [], "the second process wrote"
    assert second.conceded == frozenset({_DATASET})
    assert (conceded.id, conceded.status, conceded.error) == (
        finished.id,
        ImportRunStatus.RUNNING,
        None,
    )
    assert first_written == [(0,), (1,)]
    assert (finished.status, finished.position) == (ImportRunStatus.COMPLETED, 2)
    assert third.conceded == frozenset()
    assert (resumed.id, resumed.status, resumed.position) == (
        finished.id,
        ImportRunStatus.COMPLETED,
        2,
    )


async def test_an_import_cancelled_in_its_first_fetch_leaves_a_completed_one_standing(
    postgres_url: str, forget_probe: None
) -> None:
    """Ctrl-C or SIGTERM during the download of a new revision, over a finished import.

    Nothing of the new snapshot landed, so the checkpoint still describes the catalog:
    `completed`, at the revision and position it completed, which blocks no phase. It
    read `running` at position 0 of the new revision, and every dependent phase was
    skipped. The hold is released on the way out, so the next run takes it at once.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    try:
        async with factory() as seed:
            repository = PostgresImportRunRepository(seed)
            started = await repository.start(_DATASET, "etag-0")
            completed = started.evolve(
                status=ImportRunStatus.COMPLETED, position=2, rows_seen=2, rows_written=2
            )
            await repository.save(completed)
            await seed.commit()
            await repository.release(_DATASET)

        gate, fetching = asyncio.Event(), asyncio.Event()
        written: list[Sequence[int]] = []
        async with factory() as session:
            task = asyncio.create_task(
                _service(PostgresImportRunRepository(session), session.commit).import_dataset(
                    _Gated(gate, fetching, "etag-1"),
                    _recorder(written),  # type: ignore[arg-type]
                )
            )
            await asyncio.wait_for(fetching.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        async with factory() as reader:
            after = PostgresImportRunRepository(reader)
            stored = await after.get(_DATASET)
            retaken = await after.start(_DATASET, "etag-1")
            await after.release(_DATASET)
    finally:
        await engine.dispose()

    assert written == [], "the premise: nothing of the new snapshot landed"
    assert stored is not None
    assert stored == completed.evolve(heartbeat_at=stored.heartbeat_at)
    assert (retaken.revision, retaken.position) == ("etag-1", 0)
