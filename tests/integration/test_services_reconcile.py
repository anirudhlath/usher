"""`ReconcileService` against real Postgres, for the one thing the fakes
cannot say about a refused sweep: whether the session survives it.

`FakeMediaItemRepository` raises `AvailabilitySweepRefused` from Python and
has no transaction, so "the refusal left the session usable" is true there by
construction. Against Postgres it is a real question -- any statement error
aborts the whole transaction until a `ROLLBACK`, and `reconcile` writes the
`FAILED` run row *after* the refusal. If the refusal came out of a failed
statement rather than out of a successful `SELECT`, that write would raise
`PendingRollbackError` and the run would vanish, which is the mirror-image of
the bug ADR-0015 exists to prevent: the sweep declines to retract, and the
record that says so is lost.

`commit` is `session.flush` here rather than `session.commit`: the
integration fixture owns one connection-bound transaction that it rolls back,
which is what gives each test its isolation. Committing inside it would
defeat that. The property under test is the *ordering* of the writes, not
their durability.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes.event_publisher import FakeEventPublisher
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.sync import PostgresSyncRunRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.sync import SyncRunKind, SyncRunStatus
from usher.ports.errors import PortUnavailable
from usher.ports.source import SourceItem, SourceItemKind
from usher.services.ingest import IngestService
from usher.services.matching import MatchService
from usher.services.reconcile import ReconcileService

T0 = datetime(2026, 7, 1, tzinfo=UTC)


def _item(external_id: str) -> SourceItem:
    return SourceItem(
        external_id=external_id,
        name=f"Movie {external_id}",
        kind=SourceItemKind.MOVIE,
        year=2021,
        provider_ids={"tmdb": f"9{external_id.strip('m')}0"},
    )


@pytest_asyncio.fixture
async def source(session: AsyncSession) -> Source:
    row = Source(
        kind=SourceKind.EMBY,
        name="Reconcile Source",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    await PostgresSourceRepository(session).add(row)
    return row


@pytest.fixture
def runs(session: AsyncSession) -> PostgresSyncRunRepository:
    return PostgresSyncRunRepository(session)


@pytest.fixture
def media_items(session: AsyncSession) -> PostgresMediaItemRepository:
    return PostgresMediaItemRepository(session)


@pytest.fixture
def service(
    session: AsyncSession,
    runs: PostgresSyncRunRepository,
    media_items: PostgresMediaItemRepository,
) -> ReconcileService:
    titles = PostgresTitleRepository(session)
    matching = PostgresTitleMatchRepository(session)
    queue = PostgresJobQueue(session, max_attempts=5, backoff_seconds=30.0)
    return ReconcileService(
        ingest=IngestService(
            matcher=MatchService(titles=titles, matching=matching, queue=queue),
            matching=matching,
            media_items=media_items,
            episodes=PostgresEpisodeRepository(session),
            queue=queue,
        ),
        media_items=media_items,
        events=FakeEventPublisher(),
        runs=runs,
        commit=session.flush,
        batch_size=2,
    )


class _Adapter:
    """The smallest `list_items` that satisfies what `ReconcileService` uses.

    Not `FakeSourceAdapter`: that one is a `SourceAdapter` with a session
    model and a watch-state store, and none of it is under test here.
    """

    def __init__(self) -> None:
        self.items: dict[str, SourceItem] = {}
        self.fail_after: int | None = None

    def list_items(self, since: datetime | None = None) -> AsyncIterator[SourceItem]:
        return self._walk()

    async def _walk(self) -> AsyncIterator[SourceItem]:
        for index, item in enumerate(list(self.items.values())):
            if self.fail_after is not None and index >= self.fail_after:
                raise PortUnavailable("source went away mid-walk")
            yield item


@pytest.fixture
def adapter() -> _Adapter:
    return _Adapter()


async def test_a_refused_sweep_still_records_a_failed_run(
    service: ReconcileService,
    runs: PostgresSyncRunRepository,
    media_items: PostgresMediaItemRepository,
    source: Source,
    adapter: _Adapter,
) -> None:
    """The one this file exists for. A refusal must leave the session usable
    for the `FAILED` row that explains it -- and it does, because the guard is
    evaluated in Python after a successful `SELECT` rather than by a statement
    that fails."""
    for index in range(10):
        adapter.items[f"m{index}"] = _item(f"m{index}")
    await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    for index in range(1, 10):
        del adapter.items[f"m{index}"]
    run = await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    assert run.status is SyncRunStatus.FAILED
    assert "refusing to mark" in (run.error or "")
    stored = await runs.get(run.id)
    assert stored is not None, "the refusal poisoned the session and lost the run row"
    assert stored.status is SyncRunStatus.FAILED
    survivor = await media_items.get_by_external_id(source.id, "m5")
    assert survivor is not None and survivor.available is True


async def test_a_full_walk_retracts_and_restores_against_real_sql(
    service: ReconcileService,
    media_items: PostgresMediaItemRepository,
    source: Source,
    adapter: _Adapter,
) -> None:
    """The sweep's own SQL, driven by the service that decides when it runs.
    `mark_unseen_unavailable`'s `last_seen_at < :seen_since` and the upsert's
    `available = true` are two statements in two repositories, and only a run
    exercises the handoff between them."""
    for index in range(8):
        adapter.items[f"m{index}"] = _item(f"m{index}")
    await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    del adapter.items["m7"]
    second = await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    assert second.status is SyncRunStatus.COMPLETED
    assert second.items_retracted == 1
    gone = await media_items.get_by_external_id(source.id, "m7")
    assert gone is not None and gone.available is False
    adapter.items["m7"] = _item("m7")
    third = await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    assert third.items_retracted == 0
    back = await media_items.get_by_external_id(source.id, "m7")
    assert back is not None and back.available is True


async def test_a_walk_that_raises_leaves_every_row_available(
    service: ReconcileService,
    media_items: PostgresMediaItemRepository,
    source: Source,
    adapter: _Adapter,
) -> None:
    """The headline property, against the real sweep statement. Eight of ten
    items are written before the failure, so a sweep that ran anyway would
    retract two -- 20%, under the ceiling, and `UPDATE ... SET available =
    false` would commit it."""
    for index in range(10):
        adapter.items[f"m{index}"] = _item(f"m{index}")
    await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    adapter.fail_after = 8
    run = await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    assert run.status is SyncRunStatus.FAILED
    assert run.items_retracted == 0
    for index in range(10):
        stored = await media_items.get_by_external_id(source.id, f"m{index}")
        assert stored is not None
        assert stored.available is True, f"m{index} was retracted by a walk that failed"


async def test_a_run_that_failed_does_not_move_the_delta_cursor(
    service: ReconcileService,
    runs: PostgresSyncRunRepository,
    source: Source,
    adapter: _Adapter,
) -> None:
    """`latest_completed_cursor` against real SQL. A delta resuming from a run
    that failed halfway skips everything it never reached, silently -- and the
    filter that prevents it lives in a `WHERE status = 'completed'` no fake
    can vouch for."""
    adapter.items["m0"] = _item("m0")
    completed = await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    adapter.items["m1"] = _item("m1")
    adapter.fail_after = 0
    failed = await service.reconcile(source, SyncRunKind.DELTA, adapter)  # type: ignore[arg-type]
    assert failed.status is SyncRunStatus.FAILED
    adapter.fail_after = None
    delta = await service.reconcile(source, SyncRunKind.DELTA, adapter)  # type: ignore[arg-type]
    assert delta.cursor_at == completed.started_at
    assert await runs.latest_completed_cursor(source.id, SyncRunKind.DELTA) is not None


def test_the_service_is_constructed_from_ports_only() -> None:
    """ADR-0009, restated where the concrete repositories are in scope: this
    file wires `ReconcileService` entirely out of `Postgres*` classes and the
    service itself imports none of them. `import-linter` enforces the module
    graph; this is the assembly actually running."""
    import usher.services.reconcile as module

    assert "usher.db" not in (module.__doc__ or "")
    assert not any(name.startswith("Postgres") for name in vars(module) if not name.startswith("_"))
