"""BootstrapService against real Postgres: two processes on one dataset, and a stopped import.

Each case builds its own engine and deletes the checkpoints it committed.
"""

import asyncio
import pathlib
import time
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import QueuePool, delete, text

import usher.composition
from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from usher.composition import SkippedStep, run_bootstrap
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.models.bootstrap import ImportRunRow
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import BootstrapPhase, ImportRunStatus
from usher.domain.enums import TitleKind
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset, ImdbTitle
from usher.ports.events import NullEventPublisher
from usher.services.bootstrap import BootstrapService

_DATASET = "concurrency.probe"


class _Gated(BulkDataset[int]):
    """`count` batches, whose first fetch waits on `gate` -- a download, held open.

    `fetching` is set as the fetch begins, and `fetched_at` records when.
    """

    def __init__(
        self,
        gate: asyncio.Event | None,
        fetching: asyncio.Event,
        revision: str = "etag-1",
        *,
        name: str = _DATASET,
        count: int = 2,
    ) -> None:
        self._gate = gate
        self._fetching = fetching
        self._revision = revision
        self._name = name
        self._count = count
        self.fetched_at: float | None = None

    @property
    def name(self) -> str:
        return self._name

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
        for index in range(start, self._count):
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


async def test_an_import_whose_hold_ends_mid_download_stops_and_says_so(
    postgres_url: str, forget_probe: None
) -> None:
    """The hold's connection is ended while the first fetch waits on a download.

    `idle_session_timeout`, a proxy's idle cut and a restart all end it the same way, and
    the advisory lock goes with it. Nothing checked, so the import went on writing a
    dataset any other process could now take. The next beat confirms the hold, finds it
    gone, and the import is recorded failed -- by a process that took the dataset again
    to write it, and gave it back.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    gate, fetching = asyncio.Event(), asyncio.Event()
    written: list[Sequence[int]] = []
    try:
        async with factory() as session:
            service = BootstrapService(
                PostgresImportRunRepository(session),
                FakeBulkCatalogRepository(),
                session.commit,
                events=NullEventPublisher(),
                phase=BootstrapPhase.CROSSWALK,
                heartbeat=0.05,
            )
            task = asyncio.create_task(
                service.import_dataset(
                    _Gated(gate, fetching),
                    _recorder(written),  # type: ignore[arg-type]
                )
            )
            await asyncio.wait_for(fetching.wait(), 5)
            async with engine.connect() as killer:
                ended = await killer.scalar(
                    text(
                        "SELECT count(pg_terminate_backend(pid)) FROM pg_locks "
                        "WHERE locktype = 'advisory' AND classid = CAST(:ns AS oid) "
                        "AND objid = CAST(hashtext(:dataset) AS oid) AND objsubid = 2"
                    ),
                    {"ns": 0x75736872, "dataset": _DATASET},
                )
            run = await asyncio.wait_for(task, 5)

        async with factory() as reader:
            after = PostgresImportRunRepository(reader)
            stored = await after.get(_DATASET)
            retaken = await after.start(_DATASET, "etag-1")
            await after.release(_DATASET)
    finally:
        await engine.dispose()

    assert ended == 1, "the premise: the one backend holding the dataset was ended"
    assert written == [], "the import wrote after its hold was gone"
    assert run.status is ImportRunStatus.FAILED
    assert (run.error or "").startswith(f"lost the hold on the import of {_DATASET}"), run.error
    assert stored == run
    assert (retaken.revision, retaken.position) == ("etag-1", 0), "the dataset is free again"


_TITLES, _NAMES = "imdb.title.basics", "imdb.credit_names"


@pytest.fixture
async def forget_titles_and_names(postgres_url: str) -> AsyncIterator[None]:
    """Deletes the two checkpoints the case committed for real, however it ends."""
    yield
    engine = build_engine(postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                delete(ImportRunRow).where(ImportRunRow.dataset.in_((_TITLES, _NAMES)))
            )
    finally:
        await engine.dispose()


async def _a_catalog() -> FakeBulkCatalogRepository:
    """One synthetic title, so `credit-names` finds a catalog to join against."""
    catalog = FakeBulkCatalogRepository()
    await catalog.upsert_titles(
        [
            ImdbTitle(
                imdb_id="tt99000101",
                kind=TitleKind.MOVIE,
                name="The Quiet Vacuum",
                original_name=None,
                year=1994,
                end_year=None,
                runtime_minutes=101,
            )
        ]
    )
    return catalog


def _credit_names_from(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, dataset: _Gated
) -> Settings:
    """`run_bootstrap`'s `credit-names` over `dataset`, offline, and the settings to run it."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"the bootstrap reached the network: {request.url}")

    monkeypatch.setattr(
        usher.composition,
        "bulk_client",
        lambda *_, **__: httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )
    monkeypatch.setattr(usher.composition, "IMDbCreditNamesDataset", lambda *_, **__: dataset)
    return Settings(
        database_url=SecretStr("postgresql+asyncpg://usher:usher@127.0.0.1:1/usher"),
        secret_key=SecretStr("0" * 32),
        bulk_data_dir=tmp_path,
    )


async def test_an_import_begun_while_a_phase_reads_its_dataset_is_left_alone(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    forget_titles_and_names: None,
) -> None:
    """`credit-names` joins the titles while another process starts importing them.

    Checked once as the phase began, the import went unseen and rewrote the titles under
    the join. The phase holds them shared until it ends: the import, its whole attempt
    inside the phase's fetch, concedes, fetches nothing and writes nothing -- and once
    the phase ends, a third run imports them.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    gate, reading, importing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    phase = _Gated(gate, reading, name=_NAMES, count=0)
    settings = _credit_names_from(monkeypatch, tmp_path, phase)
    written: list[Sequence[int]] = []
    try:
        async with factory() as reader_session, factory() as importer_session:
            phase_task = asyncio.create_task(
                run_bootstrap(
                    await _a_catalog(),
                    PostgresImportRunRepository(reader_session),
                    reader_session.commit,
                    settings,
                    BootstrapPhase.CREDIT_NAMES,
                    report=lambda _: None,
                    events=NullEventPublisher(),
                )
            )
            await asyncio.wait_for(reading.wait(), 5)
            # Its session gave its connection back at the last commit.
            assert isinstance(engine.pool, QueuePool)
            held_by_the_phase = engine.pool.checkedout()

            importer = _service(
                PostgresImportRunRepository(importer_session), importer_session.commit
            )
            began = time.monotonic()
            conceded = await asyncio.wait_for(
                importer.import_dataset(
                    _Gated(None, importing, name=_TITLES),
                    _recorder(written),  # type: ignore[arg-type]
                ),
                5,
            )
            ended = time.monotonic()
            released = time.monotonic()
            gate.set()
            outcome = await asyncio.wait_for(phase_task, 5)

            third = _service(PostgresImportRunRepository(importer_session), importer_session.commit)
            imported = await third.import_dataset(
                _Gated(None, asyncio.Event(), name=_TITLES),
                _recorder(written),  # type: ignore[arg-type]
            )
    finally:
        await engine.dispose()

    assert phase.fetched_at is not None
    assert phase.fetched_at <= began <= ended <= released, (
        "the premise: the import was attempted inside the phase's fetch",
        (phase.fetched_at, began, ended, released),
    )
    assert held_by_the_phase == 2, "beside its session: the connection its reads are on, its hold's"
    assert importer.conceded == frozenset({_TITLES})
    assert (conceded.dataset, conceded.status) == (_TITLES, ImportRunStatus.FAILED)
    assert not importing.is_set(), "the import fetched"
    assert outcome.succeeded, outcome
    assert (imported.status, imported.position) == (ImportRunStatus.COMPLETED, 2)
    assert third.conceded == frozenset()
    assert written == [(0,), (1,)], "only the third run wrote"


async def test_a_phase_begun_while_its_dataset_is_being_imported_is_skipped(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    forget_titles_and_names: None,
) -> None:
    """The reverse: `imdb` is mid-download in one process when `credit-names` starts.

    A refresh's row reads `completed` until its first batch lands, so only the hold says
    the titles are being rewritten. The phase's shared read is refused, so it is skipped
    before its first request -- inside the import's fetch, recorded and asserted -- and
    runs once the import has ended.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    gate, importing, fetched = asyncio.Event(), asyncio.Event(), asyncio.Event()
    settings = _credit_names_from(
        monkeypatch, tmp_path, _Gated(None, fetched, name=_NAMES, count=0)
    )
    written: list[Sequence[int]] = []
    try:
        async with factory() as importer_session, factory() as reader_session:
            seed = PostgresImportRunRepository(importer_session)
            completed = (await seed.start(_TITLES, "etag-0")).evolve(
                status=ImportRunStatus.COMPLETED, position=2, rows_seen=2, rows_written=2
            )
            await seed.save(completed)
            await importer_session.commit()
            await seed.release(_TITLES)

            titles = _Gated(gate, importing, name=_TITLES)
            import_task = asyncio.create_task(
                _service(
                    PostgresImportRunRepository(importer_session), importer_session.commit
                ).import_dataset(titles, _recorder(written))  # type: ignore[arg-type]
            )
            await asyncio.wait_for(importing.wait(), 5)

            reader = PostgresImportRunRepository(reader_session)
            began = time.monotonic()
            skipped = await asyncio.wait_for(
                run_bootstrap(
                    await _a_catalog(),
                    reader,
                    reader_session.commit,
                    settings,
                    BootstrapPhase.CREDIT_NAMES,
                    report=lambda _: None,
                    events=NullEventPublisher(),
                ),
                5,
            )
            ended = time.monotonic()
            stored_meanwhile = await reader.get(_TITLES)
            released = time.monotonic()
            gate.set()
            finished = await asyncio.wait_for(import_task, 5)

            after = await run_bootstrap(
                await _a_catalog(),
                reader,
                reader_session.commit,
                settings,
                BootstrapPhase.CREDIT_NAMES,
                report=lambda _: None,
                events=NullEventPublisher(),
            )
    finally:
        await engine.dispose()

    assert titles.fetched_at is not None
    assert titles.fetched_at <= began <= ended <= released, (
        "the premise: the phase ran inside the import's fetch",
        (titles.fetched_at, began, ended, released),
    )
    assert stored_meanwhile is not None
    assert stored_meanwhile.status is ImportRunStatus.COMPLETED, (
        "the premise: the row alone says nothing is being imported"
    )
    assert skipped.unfinished == (
        SkippedStep(BootstrapPhase.CREDIT_NAMES, (), (BootstrapPhase.CREDIT_NAMES,), (_TITLES,)),
    )
    assert (finished.status, finished.position) == (ImportRunStatus.COMPLETED, 2)
    assert written == [(0,), (1,)]
    assert after.succeeded, after
    assert fetched.is_set(), "the phase ran once the import had ended"
