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

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
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
from usher.services.reconcile import CEILING_ERROR_CODE, ReconcileService

T0 = datetime(2026, 7, 1, tzinfo=UTC)
# The ceiling case's two numbers, named so the arithmetic below reads. 200 is
# also `USHER_SOURCE_PAGE_SIZE`'s default, which is a coincidence this case
# does not rely on: the ceiling is counted in *items*, deliberately, because
# `MAX_PAGES` already means something else.
CEILING = 200
PAST_THE_CEILING = 500


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


def _service(
    session: AsyncSession,
    runs: PostgresSyncRunRepository,
    media_items: PostgresMediaItemRepository,
    *,
    batch_size: int,
) -> ReconcileService:
    """The fixture's own wiring, reachable at another batch size.

    Extracted rather than parametrised because exactly one case wants a
    different one: the ceiling case walks 500 items, and 250 flushes of two
    would make it a benchmark of the ingest pipeline instead of a test of
    where the walk stops.
    """
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
        batch_size=batch_size,
    )


@pytest.fixture
def service(
    session: AsyncSession,
    runs: PostgresSyncRunRepository,
    media_items: PostgresMediaItemRepository,
) -> ReconcileService:
    return _service(session, runs, media_items, batch_size=2)


class _Adapter:
    """The smallest `list_items` that satisfies what `ReconcileService` uses.

    Not `FakeSourceAdapter`: that one is a `SourceAdapter` with a session
    model and a watch-state store, and none of it is under test here.
    """

    def __init__(self) -> None:
        self.items: dict[str, SourceItem] = {}
        self.fail_after: int | None = None
        # Every `since` this adapter was asked to walk from, in order.
        # `sync_runs.cursor_at` is the reconciler agreeing with itself; this
        # is the value that actually crossed the port, which is what "the
        # next delta re-requests what the truncated one never reached" is a
        # claim about.
        self.since_calls: list[datetime | None] = []

    def list_items(self, since: datetime | None = None) -> AsyncIterator[SourceItem]:
        self.since_calls.append(since)
        return self._walk()

    async def _walk(self) -> AsyncIterator[SourceItem]:
        for index, item in enumerate(list(self.items.values())):
            if self.fail_after is not None and index >= self.fail_after:
                raise PortUnavailable("source went away mid-walk")
            yield item


@pytest.fixture
def adapter() -> _Adapter:
    return _Adapter()


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    """A provider of this test's own, because `set_meter_provider` is set-once.

    `tests/conftest.py::reset_otel_meter_provider` is what makes a second
    installation take at all -- see `.claude/rules/api-telemetry-and-lanes.md`.
    """
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


def _fraction_points(reader: InMemoryMetricReader, *, outcome: str) -> list[float]:
    """Every `usher.sync.retraction.fraction` sum recorded under `outcome`.

    A list rather than a single value, so a case can assert on *how many*
    records happened: a metric published twice per walk and a metric published
    once are indistinguishable from a lookup that returns the first match.
    """
    data = reader.get_metrics_data()
    found: list[float] = []
    if data is None:
        return found
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != "usher.sync.retraction.fraction":
                    continue
                for point in metric.data.data_points:
                    attrs = dict(point.attributes or {})
                    if attrs.get("outcome") == outcome:
                        found.append(round(float(getattr(point, "sum", 0.0) or 0.0), 6))
    return found


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


async def test_a_refused_sweep_reports_both_numbers_where_an_operator_can_see_them(
    service: ReconcileService,
    media_items: PostgresMediaItemRepository,
    source: Source,
    adapter: _Adapter,
    meter_reader: InMemoryMetricReader,
) -> None:
    """The refusal's numbers reach a series, and the clean sweep reaches it too.

    🔴 **At HEAD the exception carried `would_retract`, `total` and `ceiling`
    and nothing published any of them.** `sync_runs.items_retracted` stores the
    numerator alone, and PRD 10's catalogue had no retraction series at all --
    so *"this library shed nothing"* and *"this guard refused a walk"* were the
    same silence to every dashboard.

    **The clean-sweep arm is not decoration.** A histogram published only on a
    refusal is one an operator cannot distinguish from an exporter that stopped,
    which is the whole reason `usher.sync.retraction.fraction` is recorded on
    every finished full walk. A case that seeded only the refusal would pass
    against exactly that defect.

    **And the refused arm's numerator is `would_retract`, never
    `SweepResult.retracted`.** A refused sweep retracts nothing, so a fraction
    computed from what it *did* would record 0.0 for the one state this series
    exists to make visible. The two differ only when the guard fires.
    """
    for index in range(10):
        adapter.items[f"m{index}"] = _item(f"m{index}")
    first = await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    assert first.status is SyncRunStatus.COMPLETED

    # The clean-sweep arm, and the positive control: a walk that retracts
    # nothing must still publish, and it must publish a real zero.
    swept = _fraction_points(meter_reader, outcome="swept")
    assert swept == [0.0], (
        "a full walk that retracted nothing must still record the series -- "
        "otherwise silence means both 'shed nothing' and 'never ran'"
    )

    # Now approach the ceiling: 9 of 10 gone is 0.9 against the 0.25 default.
    for index in range(1, 10):
        del adapter.items[f"m{index}"]
    refused = await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]

    assert refused.status is SyncRunStatus.FAILED
    message = refused.error or ""
    # All three numbers, in the form an operator reads them: the ceiling is
    # rendered as a **percentage**, not as the `0.25` the setting is spelled
    # with, so an assertion against the raw fraction fails on a correct
    # message. Asserted as substrings of the rendered sentence rather than
    # against a format string, so a reword that keeps the numbers passes.
    assert "9 of 10" in message and "25% ceiling" in message, (
        f"the refusal must carry would_retract, total and ceiling: {message!r}"
    )
    survivor = await media_items.get_by_external_id(source.id, "m5")
    assert survivor is not None and survivor.available is True, (
        "the premise: a refusal retracts nothing, so a fraction read off what "
        "it did would be 0.0 and the series would be a lie"
    )

    assert _fraction_points(meter_reader, outcome="refused") == [0.9], (
        "the refused arm records what the walk *would* have retracted"
    )


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


async def test_a_delta_that_hits_its_ceiling_records_failed_so_the_next_delta_does_not_skip_what_it_missed(  # noqa: E501
    session: AsyncSession,
    runs: PostgresSyncRunRepository,
    media_items: PostgresMediaItemRepository,
    source: Source,
    adapter: _Adapter,
) -> None:
    """M10 S6, and the reason the ceiling may not let its run complete.

    `latest_completed_cursor` is `started_at` of the newest **completed**
    run in the lane, so a delta that stopped at a ceiling and recorded
    `COMPLETED` would advance the cursor to its own start instant — and
    everything past the ceiling would never be requested by any delta
    again. Nothing in `src/` schedules the nightly full reconcile that would
    otherwise cover it (M9's boundary call 6), so on a shipped deployment
    with no cron a truncated-and-completed delta is a hole with no closer.

    **Three arms, in this order, because each is a different claim and the
    third is the one the task exists for.**

    1. The walk really stopped: exactly `CEILING` items were committed and
       are readable, and the item past the ceiling is not there. Committed,
       not merely counted — `_flush` commits per batch, so a ceiling costs
       the cursor advance and nothing else.
    2. The run is `FAILED` with a ceiling-shaped `error`, read back off the
       row rather than off the returned object.
    3. A **second** delta re-requests from the *original* cursor rather than
       from the truncated run's `started_at`, asserted on the `since` that
       crossed the port.

    **Real Postgres rather than the fake arm**, because the property is
    `latest_completed_cursor`'s `WHERE status = 'completed'` and a dict has
    no such predicate.
    """
    service = _service(session, runs, media_items, batch_size=30)
    adapter.items["seed"] = _item("seed")
    completed = await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    assert completed.status is SyncRunStatus.COMPLETED, (
        "the premise: there is a completed run for a delta to resume from"
    )
    adapter.items.clear()
    for index in range(PAST_THE_CEILING):
        adapter.items[f"m{index}"] = _item(f"m{index}")

    truncated = await service.reconcile(
        source,
        SyncRunKind.DELTA,
        adapter,  # type: ignore[arg-type]
        max_items=CEILING,
    )

    # -- arm 1: the walk stopped where it said, and kept what it saw -------
    assert truncated.items_seen == CEILING, (
        f"the ceiling is counted in items and is exact: {truncated.items_seen}"
    )
    for external_id in ("m0", f"m{CEILING - 1}"):
        stored = await media_items.get_by_external_id(source.id, external_id)
        assert stored is not None, (
            f"{external_id} was seen before the ceiling and `_flush` commits per batch, "
            "so a bounded walk must not cost the items it did see"
        )
    assert await media_items.get_by_external_id(source.id, f"m{CEILING}") is None, (
        "the first item past the ceiling was never committed"
    )

    # -- arm 2: and it is recorded as a failure, with a named reason -------
    assert truncated.status is SyncRunStatus.FAILED
    assert (truncated.error or "").startswith(CEILING_ERROR_CODE), truncated.error
    stored_run = await runs.get(truncated.id)
    assert stored_run is not None
    assert stored_run.status is SyncRunStatus.FAILED, (
        "the durable row is what `latest_completed_cursor` reads, not the returned object"
    )

    # -- arm 3: so the next delta re-requests what this one never reached --
    assert truncated.started_at > completed.started_at, (
        "the premise: the truncated run's own start instant is later than the cursor, "
        "so 'resumed from the cursor' and 'resumed from the truncated run' are different "
        "answers this case can tell apart"
    )
    adapter.since_calls.clear()
    second = await service.reconcile(
        source,
        SyncRunKind.DELTA,
        adapter,  # type: ignore[arg-type]
        max_items=CEILING,
    )
    assert second.cursor_at == completed.started_at, (
        "the truncated delta advanced the cursor to its own start instant, so everything "
        f"past its ceiling is never requested again: {second.cursor_at}"
    )
    assert adapter.since_calls == [completed.started_at], (
        f"the `since` that crossed the port is the original cursor: {adapter.since_calls}"
    )


def test_the_service_is_constructed_from_ports_only() -> None:
    """ADR-0009, restated where the concrete repositories are in scope: this
    file wires `ReconcileService` entirely out of `Postgres*` classes and the
    service itself imports none of them. `import-linter` enforces the module
    graph; this is the assembly actually running."""
    import usher.services.reconcile as module

    assert "usher.db" not in (module.__doc__ or "")
    assert not any(name.startswith("Postgres") for name in vars(module) if not name.startswith("_"))
