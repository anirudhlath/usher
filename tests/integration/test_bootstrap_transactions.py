"""What the importer's connection holds while `BootstrapService` waits to retry.

Read from a second connection in `pg_stat_activity`; each case deletes its checkpoint.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

import pytest
from sqlalchemy import delete, event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from usher.db.base import build_engine, build_session_factory
from usher.db.models.bootstrap import ImportRunRow
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import BootstrapPhase, ImportRunStatus
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset
from usher.ports.errors import PortUnavailable
from usher.ports.events import NullEventPublisher
from usher.services.bootstrap import BootstrapService, RetryPolicy

_DATASET = "transactions.probe"


class _FailsOnceEach(BulkDataset[int]):
    """`revision()` fails once, then the first fetch fails once.

    One wait on each of `BootstrapService`'s two retry paths. The second comes after
    `start()` has written the checkpoint and before any batch has committed it -- WDQS
    timing out on page 0 -- which is the write the waits used to hold uncommitted.
    `on_fetch` runs as each fetch begins, where a real one downloads or queries for
    minutes.
    """

    def __init__(self, on_fetch: Callable[[], Awaitable[None]]) -> None:
        self._revision_failures = 1
        self._fetch_failures = 1
        self._on_fetch = on_fetch

    @property
    def name(self) -> str:
        return _DATASET

    @property
    def attribution(self) -> str:
        return "synthetic, never redistributed"

    async def revision(self) -> str:
        if self._revision_failures:
            self._revision_failures -= 1
            raise PortUnavailable("HEAD failed: ConnectError")
        return "etag-1"

    def batches(
        self, *, resume_from: BulkCursor | None = None, revision: str | None = None
    ) -> AsyncIterator[BulkBatch[int]]:
        return self._iter(resume_from.position if resume_from else 0)

    async def _iter(self, start: int) -> AsyncIterator[BulkBatch[int]]:
        await self._on_fetch()
        for index in range(start, 2):
            if self._fetch_failures:
                self._fetch_failures -= 1
                raise PortUnavailable("WDQS returned HTTP 504")
            yield BulkBatch(
                rows=(index,), cursor=BulkCursor(revision="etag-1", position=index + 1, rows_seen=0)
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


def _backend_pids(engine: AsyncEngine) -> list[int]:
    """Every server pid `engine` connects, recorded as it connects.

    Read off the driver rather than with `SELECT pg_backend_pid()`, which would open the
    very transaction this module is looking for.
    """
    pids: list[int] = []

    def on_connect(dbapi_connection: Any, _: object) -> None:
        pids.append(dbapi_connection.driver_connection.get_server_pid())

    event.listen(engine.sync_engine, "connect", on_connect)
    return pids


async def test_no_retry_waits_inside_a_transaction(postgres_url: str, forget_probe: None) -> None:
    """Every wait and every fetch finds the importer's connection `idle`, holding no xid.

    Before: `idle in transaction` through all of them, with an xid from the first fetch
    on -- `start()`'s flushed `RUNNING` row, invisible to another connection until the
    first batch committed. The first wait follows the read `run_bootstrap` makes before
    any import (`blocked()` reads the checkpoints), so it is a transaction the *caller*
    left open that the wait has to end.
    """
    importer, observer = build_engine(postgres_url), build_engine(postgres_url)
    pids = _backend_pids(importer)
    observed: list[tuple[str, list[tuple[str, str | None]], str | None]] = []

    async def observe(moment: str) -> None:
        async with observer.connect() as conn:
            activity = (
                await conn.execute(
                    text(
                        "SELECT state, backend_xid::text FROM pg_stat_activity "
                        "WHERE pid = ANY(:pids)"
                    ),
                    {"pids": pids},
                )
            ).all()
            status = (
                await conn.execute(
                    text("SELECT status FROM import_runs WHERE dataset = :dataset"),
                    {"dataset": _DATASET},
                )
            ).scalar_one_or_none()
        observed.append((moment, [(row.state, row.backend_xid) for row in activity], status))

    async def wait(_: float) -> None:
        await observe("wait")

    async def fetch() -> None:
        await observe("fetch")

    written: list[Sequence[int]] = []

    async def write(rows: Sequence[int]) -> int:
        written.append(rows)
        return len(rows)

    try:
        async with build_session_factory(importer)() as session:
            runs = PostgresImportRunRepository(session)
            assert await runs.get(_DATASET) is None, "the premise: no checkpoint yet"
            run = await BootstrapService(
                runs,
                FakeBulkCatalogRepository(),
                session.commit,
                events=NullEventPublisher(),
                phase=BootstrapPhase.CROSSWALK,
                retry=RetryPolicy(first_delay=0.0, max_delay=0.0),
                sleep=wait,
            ).import_dataset(_FailsOnceEach(fetch), write)
    finally:
        await importer.dispose()
        await observer.dispose()

    assert run.status is ImportRunStatus.COMPLETED
    assert written == [(0,), (1,)], "the premise: both batches landed, once each"
    assert len(pids) == 1, "the premise: the importer used exactly one connection"
    idle = [("idle", None)]
    assert observed == [
        # The revision's wait: nothing started yet, and the caller's read ended.
        ("wait", idle, None),
        # The first fetch: the start is committed, so another process sees it.
        ("fetch", idle, "running"),
        ("wait", idle, "running"),
        ("fetch", idle, "running"),
    ]
