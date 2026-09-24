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

    One wait on each of `BootstrapService`'s two retry paths, the second after `start()`
    and before any batch -- WDQS timing out on page 0. `observe` runs as each `revision()`
    and each fetch begins, where a real one sends a `HEAD` or downloads for minutes.
    """

    def __init__(self, observe: Callable[[str], Awaitable[None]]) -> None:
        self._revision_failures = 1
        self._fetch_failures = 1
        self._observe = observe

    @property
    def name(self) -> str:
        return _DATASET

    @property
    def attribution(self) -> str:
        return "synthetic, never redistributed"

    async def revision(self) -> str:
        await self._observe("revision")
        if self._revision_failures:
            self._revision_failures -= 1
            raise PortUnavailable("HEAD failed: ConnectError")
        return "etag-1"

    def batches(
        self, *, resume_from: BulkCursor | None = None, revision: str | None = None
    ) -> AsyncIterator[BulkBatch[int]]:
        return self._iter(resume_from.position if resume_from else 0)

    async def _iter(self, start: int) -> AsyncIterator[BulkBatch[int]]:
        await self._observe("fetch")
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
    """Every `HEAD`, wait and fetch finds the importer's connections `idle`, holding no xid.

    The case reads the checkpoint first, as `run_bootstrap`'s `blocked()` does, so the
    first `revision()` follows a transaction the *caller* left open. From `start()` on
    there are two connections, the session's and the hold's, and from the first fetch on
    another connection reads the checkpoint `running`.
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
            ).import_dataset(_FailsOnceEach(observe), write)
    finally:
        await importer.dispose()
        await observer.dispose()

    assert run.status is ImportRunStatus.COMPLETED
    assert written == [(0,), (1,)], "the premise: both batches landed, once each"
    assert len(pids) == 2, "the premise: the session's connection and the hold's"
    idle, both_idle = [("idle", None)], [("idle", None), ("idle", None)]
    assert observed == [
        # Nothing started yet, and the caller's read is over before the first `HEAD`.
        ("revision", idle, None),
        ("wait", idle, None),
        ("revision", idle, None),
        # The first fetch: the start is committed, so another process sees it.
        ("fetch", both_idle, "running"),
        ("wait", both_idle, "running"),
        ("fetch", both_idle, "running"),
    ]
