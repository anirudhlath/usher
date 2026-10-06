"""The reconcile lane, against port fakes and `FakeSourceAdapter`."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from itertools import groupby, pairwise

import httpx
import pytest
from loguru import logger
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import AwareDatetime, SecretStr

from tests.fakes.emby_harness import instant_sleep
from tests.fakes.emby_server import FakeEmbyServer
from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.event_publisher import FakeEventPublisher
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.source_adapter import FakeSourceAdapter
from tests.fakes.sync_run_repository import FakeSyncRunRepository
from tests.fakes.title_match_repository import FakeTitleMatchRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.adapters.emby.adapter import EmbyAdapter
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.sync import (
    ABANDONED_ERROR,
    CANCELLED_ERROR,
    STALE_AFTER,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunUnit,
    SyncRunUnitStatus,
    WalkStage,
    is_live,
)
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortDataMalformed, PortUnavailable, UsherPortError
from usher.ports.events import ClientEventKind
from usher.ports.source import SourceItem, SourceItemKind, UnitPage, WalkPlan
from usher.services.ingest import IngestService
from usher.services.matching import MatchService
from usher.services.reconcile import (
    CEILING_ERROR_CODE,
    RETRACTION_ERROR_CODE,
    ReconcileService,
    WalkRefused,
    _recorded_failure,
)

T0 = datetime(2026, 7, 1, tzinfo=UTC)
# After every run's `started_at`, which defaults to `now()`.
LATER = datetime(2099, 1, 1, tzinfo=UTC)


def _item(external_id: str, **overrides: object) -> SourceItem:
    fields: dict[str, object] = {
        "external_id": external_id,
        "name": f"Movie {external_id}",
        "kind": SourceItemKind.MOVIE,
        "year": 2021,
        "provider_ids": {"tmdb": f"9{external_id.strip('m')}0"},
    }
    fields.update(overrides)
    return SourceItem(**fields)  # type: ignore[arg-type]


class _Ticks:
    """A clock that moves on a second every time it is read."""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return LATER + timedelta(seconds=self.reads)


class _Timer:
    """A monotonic reading that moves only when the case moves it.

    It starts far from zero, so a reading recorded as it stands is never a duration.
    """

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Fixture:
    def __init__(
        self,
        *,
        batch_size: int = 1_000,
        max_retract_fraction: float = 0.25,
        walkers: int = 4,
        heartbeat_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        timer: Callable[[], float] | None = None,
    ) -> None:
        self.source = Source(
            kind=SourceKind.EMBY,
            name="Living Room Emby",
            base_url="https://emby.invalid",
            credentials_ref="ref-1",
            device_id=str(new_id()),
        )
        self.adapter = FakeSourceAdapter(self.source)
        self.titles = FakeTitleRepository()
        self.matching = FakeTitleMatchRepository(titles=self.titles)
        self.queue = FakeJobQueue()
        self.media_items = FakeMediaItemRepository()
        self.runs = FakeSyncRunRepository()
        self.events = FakeEventPublisher()
        self.commits = 0
        self.rollbacks = 0
        # The adapter's own list, so its `fetched` entries and the `completed`
        # entries `_commit` adds are ordered against each other.
        self.journal = self.adapter.journal
        # Read back at every commit: the newest run's items and heartbeat, and
        # its units' statuses by key.
        self.checkpoints: list[tuple[int, datetime | None]] = []
        self.unit_states: list[dict[str, SyncRunUnitStatus]] = []
        self._completed: set[tuple[uuid.UUID, str]] = set()
        self.ingest = IngestService(
            matcher=MatchService(titles=self.titles, matching=self.matching, queue=self.queue),
            matching=self.matching,
            media_items=self.media_items,
            episodes=FakeEpisodeRepository(),
            queue=self.queue,
        )
        self.service = ReconcileService(
            ingest=self.ingest,
            media_items=self.media_items,
            runs=self.runs,
            events=self.events,
            commit=self._commit,
            rollback=self._rollback,
            batch_size=batch_size,
            max_retract_fraction=max_retract_fraction,
            walkers=walkers,
            heartbeat_seconds=heartbeat_seconds,
            clock=clock if clock is not None else _Ticks(),
            timer=timer if timer is not None else _Timer(),
        )

    async def _commit(self) -> None:
        self.commits += 1
        newest = await self.runs.list_for_source(self.source.id, limit=1)
        if not newest:
            return
        run = newest[0]
        self.checkpoints.append((run.items_seen, run.heartbeat_at))
        units = await self.runs.units_for(run.id)
        self.unit_states.append({unit.unit_key: unit.status for unit in units})
        for unit in units:
            key = (run.id, unit.unit_key)
            if unit.status is SyncRunUnitStatus.COMPLETED and key not in self._completed:
                self._completed.add(key)
                self.journal.append(("completed", unit.unit_key))

    async def _rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def fixture() -> _Fixture:
    return _Fixture()


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter


# -- the success path -------------------------------------------------------


async def test_a_full_walk_stores_everything_and_retracts_what_vanished(
    fixture: _Fixture,
) -> None:
    fixture.adapter.seed(_item("m1"), T0)
    fixture.adapter.seed(_item("m2"), T0)
    fixture.adapter.seed(_item("m3"), T0)
    fixture.adapter.seed(_item("m4"), T0)
    first = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert first.status is SyncRunStatus.COMPLETED
    assert first.items_seen == 4
    assert first.items_retracted == 0
    fixture.adapter.forget("m4")
    second = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert second.status is SyncRunStatus.COMPLETED
    assert second.items_retracted == 1
    gone = await fixture.media_items.get_by_external_id(fixture.source.id, "m4")
    assert gone is not None and gone.available is False
    kept = await fixture.media_items.get_by_external_id(fixture.source.id, "m1")
    assert kept is not None and kept.available is True


async def test_an_item_that_came_back_is_available_again(fixture: _Fixture) -> None:
    """Items that vanish from a source go unavailable, and coming back must undo that.

    The sweep only ever sets false; appearing in a walk is what sets it true again.
    """
    for index in range(4):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    fixture.adapter.forget("m3")
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    fixture.adapter.seed(_item("m3"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    back = await fixture.media_items.get_by_external_id(fixture.source.id, "m3")
    assert back is not None and back.available is True


async def test_every_row_is_stamped_with_the_runs_start_instant(
    fixture: _Fixture,
) -> None:
    """`observed_at=run.started_at`, deterministically.

    A per-row `datetime.now(UTC)` is always *later* than `started_at`, so the sweep's
    `<` still spares everything the run saw and no other case goes red. What it breaks
    is the meaning of the column -- `last_seen_at` stops being "the run that saw this"
    -- and only an equality against the run's own instant notices.
    """
    for index in range(5):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    fixture.service._batch_size = 2
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    for index in range(5):
        stored = await fixture.media_items.get_by_external_id(fixture.source.id, f"m{index}")
        assert stored is not None
        assert stored.last_seen_at == run.started_at, f"m{index} carries its own instant"


async def test_a_walk_longer_than_one_batch_stores_every_item(fixture: _Fixture) -> None:
    """The trailing partial batch.

    Seven items at a batch size of two is three full batches and one of one -- and a
    `_walk` that flushed only on the size threshold silently drops the last page of
    every walk whose item count is not a multiple of the batch size, which is almost all
    of them.
    """
    for index in range(7):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    fixture.service._batch_size = 2
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.items_seen == 7
    assert await fixture.media_items.count_for_source(fixture.source.id) == 7


async def test_a_run_checkpoints_every_batch(fixture: _Fixture) -> None:
    """A full library walk takes hours.

    A run that recorded its counters only at the end tells an operator nothing while it
    is going, and PRD 10's dashboard-3 panel plots exactly those counters.
    """
    for index in range(7):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    fixture.service._batch_size = 2
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    # Four batches, plus the run's insert, the beat that stores its plan, and its final save.
    assert fixture.commits == 7, fixture.commits


async def test_a_run_records_its_matched_and_unmatched_counts(fixture: _Fixture) -> None:
    """PRD 10's dashboard 3.

    A run that reported `items_seen` and left the other two at zero makes "how much of
    this library does Usher actually know" unanswerable from the sync history.
    """
    fixture.adapter.seed(_item("m1"), T0)
    fixture.adapter.seed(_item("m2", name="Home Video 2004", year=None, provider_ids={}), T0)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert (run.items_seen, run.items_matched, run.items_unmatched) == (2, 1, 1)


async def test_the_run_is_recorded_before_the_walk_starts(fixture: _Fixture) -> None:
    """The run row is inserted and committed first, `RUNNING`.

    One that only appeared when the walk finished would leave an operator no way to see
    an in-flight sync, and a killed process no trace at all.
    """
    seen: list[SyncRunStatus] = []
    original = fixture.media_items.upsert_many

    async def _peek(rows: object) -> object:
        stored = await fixture.runs.list_for_source(fixture.source.id)
        seen.extend(run.status for run in stored)
        return await original(rows)  # type: ignore[arg-type]

    fixture.media_items.upsert_many = _peek  # type: ignore[method-assign, assignment]
    fixture.adapter.seed(_item("m1"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert seen == [SyncRunStatus.RUNNING]


# -- the failure paths, which are the point ---------------------------------


async def test_a_walk_that_raises_sweeps_nothing(fixture: _Fixture) -> None:
    """A walk that failed must not sweep.

    A generator that stops because the adapter gave up is indistinguishable from one
    that finished, which is why `list_items` is contracted to raise -- and that
    guarantee is worth nothing if the reconciler sweeps either way. The seeded items
    are all still present on the source; only the transport failed, and a reconciler
    that swept here marks a healthy library unavailable over one flaky request.

    Eight of ten items are flushed before the failure, deliberately: that leaves two
    stale rows, 20% of the source, *under* the 25% ceiling -- so the retraction guard
    does not fire and cannot rescue a sweep that should never have run.
    """
    for index in range(10):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    fixture.service._batch_size = 2
    fixture.adapter.fail_after(8)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.status is SyncRunStatus.FAILED
    assert run.error is not None
    assert run.items_retracted == 0
    for index in range(10):
        stored = await fixture.media_items.get_by_external_id(fixture.source.id, f"m{index}")
        assert stored is not None
        assert stored.available is True, f"m{index} was retracted by a walk that failed"


async def test_a_walk_that_raises_keeps_the_batches_it_already_wrote(
    fixture: _Fixture,
) -> None:
    """The other half of committing per batch.

    A crash costs the batch in flight, never the walk -- a full library walk is hours,
    and re-walking from the start after every transient failure is how a sync never
    finishes.

    The `items_seen` assertions are the ones with teeth, and they are about the
    *durable record* rather than the walk. `SyncRun` is frozen, `_flush` saves an
    evolved copy per batch, and the failure handler evolves whatever binding it holds
    -- so a handler reading the pre-walk run writes `items_seen = 0` over a checkpoint
    that had recorded eight, and PRD 10's dashboard 3 plots that zero.
    """
    for index in range(10):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    fixture.service._batch_size = 2
    fixture.adapter.fail_after(8)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.status is SyncRunStatus.FAILED
    assert run.items_seen == 8
    assert run.items_matched == 8
    assert await fixture.media_items.count_for_source(fixture.source.id) == 8
    stored = await fixture.runs.get(run.id)
    assert stored is not None
    assert stored.items_seen == 8, "the failure handler regressed the checkpoint"


async def test_a_refused_sweep_fails_the_run_and_changes_nothing(
    fixture: _Fixture,
) -> None:
    """The residual `list_items`' contract does not cover.

    A walk that *completes* and returns almost nothing is the shape the retraction
    ceiling exists for.
    """
    for index in range(10):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    for index in range(1, 10):
        fixture.adapter.forget(f"m{index}")
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.status is SyncRunStatus.FAILED
    assert "refusing to mark" in (run.error or "")
    assert run.items_retracted == 0
    stored = await fixture.media_items.get_by_external_id(fixture.source.id, "m5")
    assert stored is not None and stored.available is True


async def test_a_refused_sweep_keeps_the_upserts_the_walk_made(
    fixture: _Fixture,
) -> None:
    """A refusal fails the run, and the run's *writes* must survive it anyway.

    The alternative is the mirror-image bug: a source that has genuinely shrunk below
    the ceiling can never record anything again, because every walk's upsert half is
    rolled back with the sweep's refusal.
    """
    fixture.adapter.seed(_item("m0"), T0)
    fixture.adapter.seed(_item("m1"), T0)
    fixture.adapter.seed(_item("m2"), T0)
    fixture.adapter.seed(_item("m3"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    for index in range(1, 4):
        fixture.adapter.forget(f"m{index}")
    fixture.adapter.seed(_item("m9"), T0)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.status is SyncRunStatus.FAILED
    newcomer = await fixture.media_items.get_by_external_id(fixture.source.id, "m9")
    assert newcomer is not None, "the refusal rolled back the walk's own upserts"
    assert newcomer.available is True


async def test_a_bug_is_not_recorded_as_an_upstream_failure(fixture: _Fixture) -> None:
    """`reconcile` swallows `UsherPortError` so `usher sync` can carry on to the next source.

    Anything else is a bug in this process, and recording it as a failed *sync* hides it
    behind an operational-looking row.
    """

    async def _explode(*args: object, **kwargs: object) -> None:
        raise ZeroDivisionError("a bug, not an outage")

    fixture.media_items.upsert_many = _explode  # type: ignore[method-assign, assignment]
    fixture.adapter.seed(_item("m1"), T0)
    with pytest.raises(ZeroDivisionError):
        await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)


async def test_a_run_that_could_not_reach_the_source_at_all_is_recorded(
    fixture: _Fixture,
) -> None:
    """`usher sync` has to run the other two sources when the first is unreachable.

    So this returns a durable record rather than raising.
    """
    fixture.adapter.go_offline()
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.status is SyncRunStatus.FAILED
    assert run.items_seen == 0
    assert isinstance(run.error, str) and run.error
    stored = await fixture.runs.get(run.id)
    assert stored is not None and stored.status is SyncRunStatus.FAILED


# -- the delta lane ---------------------------------------------------------


async def test_a_delta_walk_uses_the_last_completed_cursor(fixture: _Fixture) -> None:
    """`latest_completed_cursor` is the method, and this is why.

    Resuming from the newest run of *any* status would silently skip everything a
    failed run never reached.
    """
    fixture.adapter.seed(_item("m1"), T0)
    completed = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    fixture.adapter.seed(_item("m2"), LATER)
    fixture.adapter.fail_after(0)
    failed = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    assert failed.status is SyncRunStatus.FAILED
    fixture.adapter.clear_failure()
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    assert run.cursor_at == completed.started_at
    assert run.items_seen == 1, "the failed delta moved the cursor past m2"


async def test_a_delta_walk_resumes_from_the_newest_completed_delta(
    fixture: _Fixture,
) -> None:
    """A completed delta must move the cursor on.

    Otherwise every delta re-walks from the last full run and the lane saves nothing.
    """
    fixture.adapter.seed(_item("m1"), T0)
    full = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    fixture.adapter.seed(_item("m2"), LATER)
    first_delta = await fixture.service.reconcile(
        fixture.source, SyncRunKind.DELTA, fixture.adapter
    )
    assert first_delta.status is SyncRunStatus.COMPLETED
    assert first_delta.cursor_at == full.started_at
    second_delta = await fixture.service.reconcile(
        fixture.source, SyncRunKind.DELTA, fixture.adapter
    )
    assert second_delta.cursor_at == first_delta.started_at
    assert second_delta.cursor_at is not None and first_delta.cursor_at is not None
    assert second_delta.cursor_at > first_delta.cursor_at


async def test_a_full_walk_ignores_every_cursor(fixture: _Fixture) -> None:
    """A full walk ignores every cursor.

    One that inherited a cursor would return only what changed and then sweep, which
    retracts everything it never asked for.
    """
    fixture.adapter.seed(_item("m1"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    # T0, deliberately: it is *older* than the cursor a delta would have
    # inherited, so a full walk that used one would filter it out entirely.
    fixture.adapter.seed(_item("m2"), T0)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.cursor_at is None
    assert run.items_seen == 2


async def test_a_delta_walk_never_sweeps(fixture: _Fixture) -> None:
    """A delta walk returns only what changed, so by construction almost everything is "unseen".

    Sweeping after one retracts the entire library -- and the guard would catch it,
    which would make every delta run fail rather than merely be wrong. Only a full walk
    may retract.
    """
    for index in range(10):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    fixture.adapter.seed(_item("m0"), LATER)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    assert run.status is SyncRunStatus.COMPLETED
    assert run.items_seen == 1, "the delta walked more than what changed"
    assert run.items_retracted == 0
    for index in range(1, 10):
        stored = await fixture.media_items.get_by_external_id(fixture.source.id, f"m{index}")
        assert stored is not None
        assert stored.available is True


async def test_a_delta_walk_under_the_ceiling_still_never_sweeps() -> None:
    """The version of the case above that the retraction ceiling cannot rescue.

    With ten items and one changed, a sweeping delta would retract nine -- 90%,
    refused, so the run merely fails and nothing is lost. Here only two of ten are
    stale, which is under the ceiling: a sweeping delta succeeds and silently retracts
    two available items.
    """
    fixture = _Fixture(batch_size=2)
    for index in range(10):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    for index in range(8):
        fixture.adapter.seed(_item(f"m{index}"), LATER)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    assert run.status is SyncRunStatus.COMPLETED
    assert run.items_seen == 8
    assert run.items_retracted == 0
    for index in (8, 9):
        stored = await fixture.media_items.get_by_external_id(fixture.source.id, f"m{index}")
        assert stored is not None
        assert stored.available is True, f"m{index} was retracted by a delta walk"


# -- the gap-closer's ceiling -----------------------------------------------------
# "Ceiling" is overloaded in this file and the two are unrelated: the retraction one is
# a *fraction* of a source's rows and gates the availability sweep; this one is a count
# of *items* and gates the walk.


async def test_a_delta_an_operator_asked_for_is_not_bounded_by_the_gap_closers_ceiling() -> None:
    """The ceiling is the *lane's*, not `ReconcileService`'s.

    `LaneSupervisor._close_gap` is the one caller nobody typed a command for, so a
    delta it starts against a source Usher has not reached for a month is a walk the
    operator did not ask for and is bounded. `usher sync --kind delta` is an operator
    asking for the whole thing and gets it -- `max_items` defaults to 0, and 0 is
    unlimited.

    Both halves in one case, against **one** source and one cursor, because "the
    unbounded delta completed" is also what a service with no ceiling at all produces.
    The bounded run comes first and fails, which is what leaves the cursor where it was
    for the second.
    """
    fixture = _Fixture(batch_size=4)
    for index in range(12):
        fixture.adapter.seed(_item(f"m{index}"), LATER)
    full = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert full.status is SyncRunStatus.COMPLETED and full.items_seen == 12

    # Under a deadline: the walk stops with its reader blocked on a full queue, and a
    # reader left running would hang the case rather than fail it.
    bounded = await asyncio.wait_for(
        fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter, max_items=5),
        5.0,
    )
    assert bounded.status is SyncRunStatus.FAILED
    assert bounded.items_seen == 5, "the lane's delta stops at its ceiling"

    operator = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    assert operator.status is SyncRunStatus.COMPLETED
    assert operator.items_seen == 12, "`usher sync --kind delta` asked for the whole thing"
    assert operator.cursor_at == full.started_at, (
        "and it resumed from the cursor the bounded run did not move"
    )


async def test_a_delta_whose_whole_answer_is_exactly_the_gap_ceiling_is_not_truncated() -> None:
    """`>`, not `>=`, and the difference is a whole cursor.

    A delta that returned exactly `max_items` items and then ended has not
    been truncated -- there is nothing past the ceiling to come back for --
    so recording it `FAILED` would cost the cursor advance for nothing, and
    the *next* delta would re-walk the identical window. The comparison
    fires on the first item **past** the ceiling, which is then discarded
    unread.
    """
    fixture = _Fixture(batch_size=3)
    for index in range(5):
        fixture.adapter.seed(_item(f"m{index}"), LATER)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    exact = await fixture.service.reconcile(
        fixture.source, SyncRunKind.DELTA, fixture.adapter, max_items=5
    )
    assert exact.status is SyncRunStatus.COMPLETED, (
        "five items against a ceiling of five is a complete answer, not a truncation"
    )
    assert exact.items_seen == 5
    assert exact.error is None

    after = await fixture.service.reconcile(
        fixture.source, SyncRunKind.DELTA, fixture.adapter, max_items=5
    )
    assert after.cursor_at == exact.started_at, (
        "the completed exact-fit delta is what the next one resumes from"
    )


async def test_a_walk_stopped_at_the_gap_ceiling_keeps_every_batch_it_committed() -> None:
    """A ceiling costs the cursor advance and nothing else.

    `_flush` commits per batch (*"a crash costs the batch in flight, never
    the walk"*), so the items the bounded walk did see are durable even
    though the run it belongs to is `FAILED`. Without this the honest
    alternative to a truncated `COMPLETED` would be losing the work, and
    the trade the setting documents would be a different one.

    The trailing partial batch is deliberate: 7 items at a batch size of 3
    is two full batches and a remainder, so a ceiling that only ever fired
    on a batch boundary would commit 6 here.
    """
    fixture = _Fixture(batch_size=3)
    for index in range(20):
        fixture.adapter.seed(_item(f"m{index}"), LATER)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    before = fixture.commits

    run = await asyncio.wait_for(
        fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter, max_items=7),
        5.0,
    )

    assert run.items_seen == 7
    assert fixture.commits > before, "a bounded walk still commits what it saw"
    for index in range(7):
        stored = await fixture.media_items.get_by_external_id(fixture.source.id, f"m{index}")
        assert stored is not None, f"m{index} was seen before the ceiling and must be stored"
    assert (
        await fixture.runs.latest_completed_cursor(fixture.source.id, SyncRunKind.DELTA) is None
    ), "a bounded delta advances no delta-lane cursor"


async def test_a_walk_stopped_at_the_gap_ceiling_sweeps_nothing() -> None:
    """A truncated walk must not reach the availability sweep.

    It has rows it never looked at, and the sweep retracts exactly those.
    """
    fixture = _Fixture(batch_size=3)
    for index in range(20):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    run = await asyncio.wait_for(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, max_items=16),
        5.0,
    )

    assert run.status is SyncRunStatus.FAILED
    assert run.error_code == CEILING_ERROR_CODE, (
        "the premise: this run failed on its ceiling and not on a refused sweep, which is "
        f"the arithmetic that makes the retraction assertions below able to fail: {run.error}"
    )
    assert run.items_retracted == 0
    for index in range(20):
        stored = await fixture.media_items.get_by_external_id(fixture.source.id, f"m{index}")
        assert stored is not None
        assert stored.available is True, f"m{index} was retracted by a walk that stopped early"


async def test_a_walk_stopped_at_the_gap_ceiling_tells_the_operator_what_to_run() -> None:
    """PRD 08's degradation row, at the level an operator's log filter sees.

    ⚠️ **The count and the ceiling are the same number by construction** --
    a truncated walk saw exactly `max_items` -- so the line names the number
    once and names the ceiling by the setting an operator would change. The
    assertion is against **7 of 20 available items**, so a line rendering
    what the source held, or the batch, or the ceiling it was configured
    with under a different arithmetic, all read differently.
    """
    fixture = _Fixture(batch_size=3)
    for index in range(20):
        fixture.adapter.seed(_item(f"m{index}"), LATER)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{level.name}|{message}")
    try:
        run = await asyncio.wait_for(
            fixture.service.reconcile(
                fixture.source, SyncRunKind.DELTA, fixture.adapter, max_items=7
            ),
            5.0,
        )
    finally:
        logger.remove(sink)

    assert run.status is SyncRunStatus.FAILED
    ceilings = [line for line in lines if "ceiling" in line]
    assert len(ceilings) == 1, f"one line per bounded walk, at WARNING: {lines}"
    line = ceilings[0]
    assert line.startswith("WARNING|"), f"an operator acts on this one: {line}"
    assert "Living Room Emby" in line, f"the line names the source it stopped on: {line}"
    assert "7 items" in line, f"the line names how far it got: {line}"
    assert "USHER_PUSH_GAP_MAX_ITEMS" in line, f"the line names the knob: {line}"
    assert "usher sync --kind full" in line, (
        f"a warning that does not say what to run is a dead end: {line}"
    )
    # PRD 08's credentials-are-never-logged rule, with the source name above
    # as the positive control that makes this an absence claim rather than a
    # statement about an empty line.
    assert "emby.invalid" not in line, (
        f"the source's base URL is not an operator's business: {line}"
    )


async def test_the_gap_ceilings_error_is_distinguishable_from_the_dead_mans_switch() -> None:
    """Two different things end a walk early and land in the same `sync_runs` row.

    They mean opposite things. `MAX_PAGES` is `EmbyAdapter`'s dead-man's switch against
    a server that ignores `StartIndex`; exhausting it raises `PortDataMalformed` and is
    a broken upstream to investigate. The gap ceiling is Usher stopping on purpose and
    is closed by one command. An operator reads the sentence; an
    alert rule, a dashboard or a later reader has to be able to tell them
    apart **without parsing English**, which is what `error_code` carrying
    `CEILING_ERROR_CODE` on one and nothing on the other is for.

    Both failures are produced rather than transcribed: the dead-man's switch
    comes out of the **real** `EmbyAdapter` over a handler that never ends,
    so a reworded message in `adapters/emby/adapter.py` cannot silently make
    this case's premise stale.
    """
    fixture = _Fixture(batch_size=3)
    for index in range(10):
        fixture.adapter.seed(_item(f"m{index}"), LATER)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    bounded = await asyncio.wait_for(
        fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter, max_items=4),
        5.0,
    )
    ceiling_error = bounded.error or ""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "t", "User": {"Id": "u"}})
        return httpx.Response(200, json={"Items": [{"Id": "m", "Type": "Movie", "Name": "A"}]})

    emby = EmbyAdapter(
        fixture.source,
        SourceCredentials(username="usher", password=SecretStr("correct-horse-battery")),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=fixture.source.base_url
        ),
        max_pages=2,
    )
    try:
        with pytest.raises(PortDataMalformed) as caught:
            _ = [item async for item in emby.list_items()]
    finally:
        await emby.aclose()
    # `str(exc)`, because that is exactly what `reconcile` writes into the
    # column -- not `repr`, not the class name.
    dead_mans_switch = str(caught.value)
    # The service's own classifier, driven with the real exception rather than
    # with a transcription of its message -- the other arm is the run above,
    # recorded end to end.
    recorded, code = _recorded_failure(caught.value)

    assert bounded.error_code == CEILING_ERROR_CODE
    assert code is None, f"a broken upstream is not a ceiling: {code}"
    assert recorded == dead_mans_switch, (
        "the premise: what would be stored really is the adapter's own message"
    )
    assert CEILING_ERROR_CODE not in dead_mans_switch
    # ...and the other direction, so "distinguishable" is not carried by one
    # token that a later edit could paste into both. `StartIndex` is the
    # dead-man's switch's own discriminator; it appears in neither the
    # ceiling's message nor anywhere it could leak from.
    assert "StartIndex" in dead_mans_switch, (
        f"the positive control: this is the message being told apart: {dead_mans_switch}"
    )
    assert "StartIndex" not in ceiling_error, ceiling_error


# -- telemetry --------------------------------------------------------------


async def test_the_reconcile_span_is_a_child_of_whatever_is_active(
    fixture: _Fixture, spans: InMemorySpanExporter
) -> None:
    """A pipeline triggered by a request nests under that request's server span.

    A service that started a root span -- `tracer.start_span(..., context=Context())`,
    or work handed to a task created before the span existed -- throws that away and
    "what happened in this request" stops being answerable.
    """
    fixture.adapter.seed(_item("m1"), T0)
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("server") as server:
        expected_trace = server.get_span_context().trace_id
        await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    pipeline = [span for span in spans.get_finished_spans() if span.name == "sync.reconcile"]
    assert pipeline, [span.name for span in spans.get_finished_spans()]
    assert pipeline[0].context is not None
    assert pipeline[0].context.trace_id == expected_trace
    assert pipeline[0].parent is not None


async def test_a_failed_run_is_marked_on_its_span(
    fixture: _Fixture, spans: InMemorySpanExporter
) -> None:
    """PRD 10 reads run outcomes off spans as well as off `sync_runs`.

    A failure that only ever reached the database row is invisible in a trace view,
    which is where an operator looks first.
    """
    fixture.adapter.go_offline()
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    pipeline = [span for span in spans.get_finished_spans() if span.name == "sync.reconcile"]
    assert pipeline
    assert pipeline[0].attributes is not None
    assert pipeline[0].attributes.get("usher.failed") is True


# -- the things a fake cannot say -------------------------------------------


def test_the_service_never_imports_a_storage_or_transport_library() -> None:
    """PRD 01's layering rule, at module level.

    `import-linter` already forbids `usher.services -> usher.db`; this catches the other
    half, which no contract expresses: a service reaching for `httpx`, `sqlalchemy` or
    `asyncpg` directly to branch on a failure kind, instead of on `usher.ports.errors`.
    """
    import usher.services.reconcile as module

    source = (module.__file__ or "").replace(".pyc", ".py")
    text = open(source).read()  # noqa: SIM115
    for forbidden in ("httpx", "sqlalchemy", "asyncpg", "usher.db"):
        assert f"import {forbidden}" not in text
        assert f"from {forbidden}" not in text


async def test_reconcile_never_raises_a_port_error(fixture: _Fixture) -> None:
    """Every `UsherPortError` subclass, not just the two the other cases happen to produce.

    A handler that named `PortUnavailable` specifically would let `PortAuthFailed`,
    `PortRateLimited` and `PortDataMalformed` escape, and `usher sync` would stop at the
    first source with an expired credential.
    """

    class _Boom(UsherPortError):
        pass

    for error in (_Boom("custom"), PortUnavailable("gone")):

        async def _raise(*args: object, __exc: BaseException = error, **kwargs: object) -> None:
            raise __exc

        one = _Fixture()
        one.media_items.upsert_many = _raise  # type: ignore[method-assign, assignment]
        one.adapter.seed(_item("m1"), T0)
        run = await one.service.reconcile(one.source, SyncRunKind.FULL, one.adapter)
        assert run.status is SyncRunStatus.FAILED
        assert run.items_retracted == 0


async def test_the_sweep_window_is_the_runs_own_start_instant(
    fixture: _Fixture,
) -> None:
    """`seen_since=run.started_at`, not `now()`.

    With `now()` the window closes *after* the walk wrote its rows, so every row the run
    just stamped is `< seen_since` and the whole source is retracted -- which the guard
    then refuses, turning every successful walk into a failed run.
    """
    seen: list[datetime] = []
    original = fixture.media_items.mark_unseen_unavailable

    async def _record(
        source_id: uuid.UUID, *, seen_since: datetime, max_retract_fraction: float
    ) -> object:
        seen.append(seen_since)
        return await original(
            source_id, seen_since=seen_since, max_retract_fraction=max_retract_fraction
        )

    fixture.media_items.mark_unseen_unavailable = _record  # type: ignore[method-assign, assignment]
    fixture.adapter.seed(_item("m1"), T0)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert seen == [run.started_at]
    assert run.started_at < datetime.now(UTC) + timedelta(seconds=1)


# -- what a walk tells a client --------------------------------------------


async def test_each_batch_publishes_sync_progress() -> None:
    """Per batch, not per run.

    A nightly walk flushes thousands of these and an admin UI's progress bar is the
    point of them; one at the end is a bar that jumps from 0% to 100%.

    Batch size 2 against 3 items, so a per-run publisher reports 1 where a per-batch
    one reports 2 -- the count is only evidence because the denominator is held fixed.
    """
    fixture = _Fixture(batch_size=2)
    for index in range(3):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    progress = [
        event for event in fixture.events.published if event.kind is ClientEventKind.SYNC_PROGRESS
    ]
    assert len(progress) == 2
    assert progress[-1].data["items_seen"] == 3
    assert progress[-1].data["source"] == fixture.source.name
    assert progress[-1].data["kind"] == SyncRunKind.FULL.value


async def test_sync_progress_is_scoped_to_no_title(fixture: _Fixture) -> None:
    """PRD 07 marks it "Admin UI only", and the scoping is what implements that.

    A `?titles=` subscriber never sees one; a detail screen that re-rendered on every
    batch of a walk is the failure the filter exists for.
    """
    fixture.adapter.seed(_item("m0"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    progress = [
        event for event in fixture.events.published if event.kind is ClientEventKind.SYNC_PROGRESS
    ]
    assert progress
    assert all(event.title_id is None and event.episode_id is None for event in progress)


async def test_a_failed_walk_still_reported_the_batches_it_did_finish(
    fixture: _Fixture,
) -> None:
    """The events are published per *flush*.

    So a walk that dies halfway has already told the admin UI how far it got -- which
    is the same reason `_flush` commits per batch.

    Nothing announces the failure itself: PRD 07's SSE table has no such event, and
    `sync_runs` is where a failure is recorded.
    """
    fixture.service._batch_size = 2
    for index in range(4):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    fixture.adapter.fail_after(3)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.status is SyncRunStatus.FAILED
    progress = [
        event for event in fixture.events.published if event.kind is ClientEventKind.SYNC_PROGRESS
    ]
    assert [event.data["items_seen"] for event in progress] == [2]


async def test_a_bounded_walk_records_its_kind_in_a_column_rather_than_as_a_prefix() -> None:
    """The failure *kind* is `sync_runs.error_code`; `error` is prose only.

    Kills the token written as the first word of a free-text column and read back with
    `startswith`. A column an operator can reword is not a column an alert may parse,
    and the two codes differ from each other only by a substring match that any
    reworded sentence can break.
    """
    fixture = _Fixture(batch_size=3)
    for index in range(10):
        fixture.adapter.seed(_item(f"m{index}"), LATER)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    run = await asyncio.wait_for(
        fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter, max_items=4),
        5.0,
    )

    assert run.status is SyncRunStatus.FAILED
    assert run.error_code == CEILING_ERROR_CODE
    assert CEILING_ERROR_CODE not in (run.error or ""), (
        f"the kind is the column, so the sentence must not carry it too: {run.error}"
    )
    assert "usher sync --kind full" in (run.error or ""), (
        "the premise: this is still the bounded walk's own message, so the absence above "
        "is about the prefix rather than about an empty column"
    )


async def test_a_refused_sweep_records_its_kind_in_a_column_rather_than_as_a_prefix() -> None:
    """The refusal's half of the same rule, and the one an operator has a command for.

    `ports/ingest.py` builds this sentence from three numbers and is a standing
    candidate for rewording, which is exactly why the CLI must not read it: the
    kind is `error_code` and the numbers stay prose.
    """
    fixture = _Fixture()
    for index in range(4):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    for index in range(4):
        fixture.adapter.forget(f"m{index}")

    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert run.status is SyncRunStatus.FAILED
    assert run.error_code == RETRACTION_ERROR_CODE
    assert RETRACTION_ERROR_CODE not in (run.error or ""), (
        f"the kind is the column, so the sentence must not carry it too: {run.error}"
    )
    assert "4 of 4" in (run.error or ""), (
        "the premise: the refusal's own numbers survive, so the absence above is about "
        "the prefix rather than about a run that failed for some other reason"
    )
    assert run.items_retracted == 0


async def test_a_transport_failure_carries_no_error_code() -> None:
    """`error_code` is null for every failure an operator has no command for.

    The wrong implementation this kills is a column filled with a catch-all
    member: `--allow-full-retraction` is offered on exactly one kind, and a
    third code meaning "something else" is how it starts being offered on all
    of them.
    """
    fixture = _Fixture()
    fixture.adapter.seed(_item("m0"), T0)
    fixture.adapter.go_offline()

    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert run.status is SyncRunStatus.FAILED
    assert run.error, "the premise: the run really did record a failure"
    assert run.error_code is None


def test_the_two_error_codes_are_pinned_by_value_because_they_are_wire_artefacts() -> None:
    """These strings leave the process, so no in-repo reader can guard them."""
    assert CEILING_ERROR_CODE == "gap_delta_ceiling"
    assert RETRACTION_ERROR_CODE == "availability_ceiling"
    assert CEILING_ERROR_CODE != RETRACTION_ERROR_CODE


# -- a whole-library walk, as a plan ---------------------------------------


def _shelve(
    fixture: _Fixture, library: str, ids: range, *, stage: WalkStage = WalkStage.TITLES
) -> list[str]:
    """Seed `m<id>` for each id into `library`, whose unit the fake plans in `stage`."""
    external_ids = [f"m{index}" for index in ids]
    for external_id in external_ids:
        fixture.adapter.seed(_item(external_id), T0)
        fixture.adapter.place(external_id, library)
    fixture.adapter.stage(library, stage)
    return external_ids


def _fetch_window(journal: list[tuple[str, str]], key: str) -> tuple[int, int]:
    """Where `key`'s first and last fetched items sit in the journal."""
    at = [index for index, entry in enumerate(journal) if entry == ("fetched", key)]
    return at[0], at[-1]


def _progress(fixture: _Fixture) -> list[object]:
    """`items_seen` of every `sync.progress` the walk published, in order."""
    return [
        event.data["items_seen"]
        for event in fixture.events.published
        if event.kind is ClientEventKind.SYNC_PROGRESS
    ]


async def test_a_whole_library_walk_stores_its_plan_and_completes_every_unit() -> None:
    fixture = _Fixture()
    _shelve(fixture, "Films", range(3))
    _shelve(fixture, "Shows", range(10, 15))
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.status is SyncRunStatus.COMPLETED
    assert run.items_seen == 8
    units = await fixture.runs.units_for(run.id)
    assert [
        (unit.unit_key, unit.status, unit.position, unit.items_seen, unit.expected_items)
        for unit in units
    ] == [
        ("library:Films", SyncRunUnitStatus.COMPLETED, 3, 3, 3),
        ("library:Shows", SyncRunUnitStatus.COMPLETED, 5, 5, 5),
    ]
    assert await fixture.media_items.count_for_source(fixture.source.id) == 8


async def test_no_unit_of_a_stage_is_fetched_before_every_unit_ahead_of_it_has_committed() -> None:
    """The stage barrier, with a walker free for every unit.

    Four walkers for three units, so without the barrier all three start at once.
    The episodes library is also planned first.
    """
    fixture = _Fixture(walkers=4)
    _shelve(fixture, "Episodes", range(20, 22), stage=WalkStage.EPISODES)
    _shelve(fixture, "Films", range(4))
    _shelve(fixture, "Shows", range(10, 14))
    plan = await fixture.adapter.plan_walk()
    assert plan.units[0].stage is WalkStage.EPISODES, "the premise: episodes are planned first"
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    first_episode = fixture.journal.index(("fetched", "library:Episodes"))
    titles_done = max(
        fixture.journal.index(("completed", key)) for key in ("library:Films", "library:Shows")
    )
    assert first_episode > titles_done


async def test_within_a_stage_the_largest_unit_is_fetched_first() -> None:
    """One walker, so the fetch order is the claim order."""
    fixture = _Fixture(walkers=1)
    _shelve(fixture, "Shorts", range(2))
    _shelve(fixture, "Films", range(10, 15))
    plan = await fixture.adapter.plan_walk()
    assert [unit.expected_items for unit in plan.units] == [2, 5], (
        "the premise: the plan lists the smaller unit first"
    )
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    fetched = [key for event, key in fixture.journal if event == "fetched"]
    assert fetched == ["library:Films"] * 5 + ["library:Shorts"] * 2


@pytest.mark.parametrize(("walkers", "together"), [(1, False), (2, True)])
async def test_walkers_fetch_units_at_once_up_to_their_number(walkers: int, together: bool) -> None:
    """Two units' fetch windows overlap with two walkers and never with one."""
    fixture = _Fixture(walkers=walkers)
    _shelve(fixture, "Films", range(6))
    _shelve(fixture, "Shows", range(10, 16))
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    films = _fetch_window(fixture.journal, "library:Films")
    shows = _fetch_window(fixture.journal, "library:Shows")
    assert (films[0] < shows[1] and shows[0] < films[1]) is together, (films, shows)


async def test_a_units_pages_are_committed_once_they_add_up_to_a_batch_and_when_it_ends() -> None:
    """Pages of two at a batch of three: a commit after four items, then one at the end.

    A commit per page reads [2, 4, 5], and a commit only at the end [5].
    """
    fixture = _Fixture(batch_size=3)
    _shelve(fixture, "Films", range(5))
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert _progress(fixture) == [4, 5]
    [unit] = await fixture.runs.units_for(run.id)
    assert (unit.position, unit.items_seen, unit.status) == (5, 5, SyncRunUnitStatus.COMPLETED)
    statuses = [states["library:Films"] for states in fixture.unit_states if states]
    assert [status for status, _ in groupby(statuses)] == [
        SyncRunUnitStatus.PENDING,
        SyncRunUnitStatus.RUNNING,
        SyncRunUnitStatus.COMPLETED,
    ]


async def test_a_failing_unit_fails_the_run_and_keeps_every_units_committed_position() -> None:
    """Shows raises after three of its four items: its first page committed, its third dropped.

    One walker, and Films is the larger, so Films has completed before Shows starts.
    """
    fixture = _Fixture(batch_size=2, walkers=1)
    _shelve(fixture, "Films", range(10, 15))
    shows = _shelve(fixture, "Shows", range(4))
    fixture.adapter.fail_unit_after("Shows", 3)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.status is SyncRunStatus.FAILED
    assert "library Shows went away" in (run.error or "")
    assert run.items_seen == 7
    assert run.items_retracted == 0
    stored = await fixture.runs.get(run.id)
    assert stored is not None and stored.items_seen == 7
    units = {unit.unit_key: unit for unit in await fixture.runs.units_for(run.id)}
    films, failed = units["library:Films"], units["library:Shows"]
    assert (films.status, films.position) == (SyncRunUnitStatus.COMPLETED, 5)
    assert (failed.status, failed.position, failed.items_seen) == (SyncRunUnitStatus.FAILED, 2, 2)
    assert await fixture.media_items.get_by_external_id(fixture.source.id, shows[2]) is None


async def test_a_unit_commits_its_last_held_pages_checkpoint() -> None:
    fixture = _Fixture(batch_size=2)
    _shelve(fixture, "Films", range(3))

    async def noted(
        key: str, *, start_index: int = 0, checkpoint: str | None = None
    ) -> AsyncGenerator[UnitPage]:
        for index in range(2):
            yield UnitPage((_item(f"m{index}"),), resume_at=index + 1, checkpoint=f"note-{index}")
        raise PortUnavailable("the page after them failed")

    fixture.adapter.list_unit = noted  # type: ignore[method-assign]
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    (films,) = [u for u in await fixture.runs.units_for(run.id) if u.unit_key == "library:Films"]
    assert (films.position, films.checkpoint, films.status) == (
        2,
        "note-1",
        SyncRunUnitStatus.FAILED,
    )


async def test_a_unit_that_ends_on_a_batch_boundary_keeps_its_last_pages_checkpoint() -> None:
    """Its end commits no page, and that commit must not wipe the note the batch left."""
    fixture = _Fixture(batch_size=2)
    _shelve(fixture, "Films", range(2))

    async def noted(
        key: str, *, start_index: int = 0, checkpoint: str | None = None
    ) -> AsyncGenerator[UnitPage]:
        for index in range(2):
            yield UnitPage((_item(f"m{index}"),), resume_at=index + 1, checkpoint=f"note-{index}")

    fixture.adapter.list_unit = noted  # type: ignore[method-assign]
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert _progress(fixture) == [2, 2], "the premise: the end rode a commit of its own"
    [films] = await fixture.runs.units_for(run.id)
    assert (films.position, films.checkpoint, films.status) == (
        2,
        "note-1",
        SyncRunUnitStatus.COMPLETED,
    )


async def test_the_run_and_its_first_heartbeat_are_committed_before_the_plan_is_made() -> None:
    """A walk that dies while planning still leaves a row saying when it was last alive."""
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    seen: list[tuple[int, datetime | None]] = []
    original = fixture.adapter.plan_walk

    async def _peek() -> WalkPlan:
        [run] = await fixture.runs.list_for_source(fixture.source.id)
        seen.append((fixture.commits, run.heartbeat_at))
        return await original()

    fixture.adapter.plan_walk = _peek  # type: ignore[method-assign]
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    [(commits, heartbeat)] = seen
    assert commits == 1, "the run's insert is committed before the plan is asked for"
    assert heartbeat is not None


async def test_the_heartbeat_beats_while_the_plan_is_made() -> None:
    """Emby counts each library through its retrying page, so a plan can take minutes too."""
    fixture = _Fixture(heartbeat_seconds=0.01)
    _shelve(fixture, "Films", range(2))
    asked, release = asyncio.Event(), asyncio.Event()
    original = fixture.adapter.plan_walk

    async def _held() -> WalkPlan:
        asked.set()
        await release.wait()
        return await original()

    fixture.adapter.plan_walk = _held  # type: ignore[method-assign]
    walk = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    )
    try:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(5):
                while fixture.commits < 4:
                    await asyncio.sleep(0.01)
        assert asked.is_set(), "the premise: the plan was asked for"
        assert fixture.commits >= 4, "fewer than three beats while the plan was made"
        assert fixture.unit_states[:4] == [{}] * 4, "the premise: no plan was stored yet"
        # The first commit is the run's insert; the three after it are beats.
        beats = [beat for _, beat in fixture.checkpoints[1:4] if beat is not None]
        assert len(beats) == 3
        assert beats == sorted(set(beats)), "a beat that did not move the heartbeat"
    finally:
        release.set()
        run = await walk
    assert run.status is SyncRunStatus.COMPLETED
    [unit] = await fixture.runs.units_for(run.id)
    assert unit.status is SyncRunUnitStatus.COMPLETED


async def test_a_beat_that_fails_while_the_plan_is_made_cancels_the_plan_first() -> None:
    """However the wait for a plan ends, the plan is not left running: here a beat raised."""
    fixture = _Fixture(heartbeat_seconds=0.01)
    _shelve(fixture, "Films", range(2))
    release, cancelled = asyncio.Event(), asyncio.Event()
    original = fixture.adapter.plan_walk

    async def _held() -> WalkPlan:
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return await original()

    commit = fixture.service._commit

    async def _commit_until_the_first_beat() -> None:
        if fixture.commits == 1:
            raise ConnectionError("the database went away")
        await commit()

    fixture.adapter.plan_walk = _held  # type: ignore[method-assign]
    fixture.service._commit = _commit_until_the_first_beat
    with pytest.raises(ConnectionError, match="the database went away"):
        async with asyncio.timeout(5):
            await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert fixture.commits == 1, "the premise: the run's insert committed, and the beat did not"
    assert cancelled.is_set(), "the walk ended with its plan still being made"


async def test_every_commit_of_a_planned_walk_moves_the_heartbeat() -> None:
    fixture = _Fixture(batch_size=2)
    _shelve(fixture, "Films", range(5))
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    rises = [
        (before[1], after[1])
        for before, after in pairwise(fixture.checkpoints)
        if after[0] > before[0]
    ]
    assert len(rises) == 3, "the premise: three commits that each brought items"
    for earlier, later in rises:
        assert earlier is not None and later is not None and later > earlier


async def test_a_writer_waiting_on_its_walkers_still_heartbeats() -> None:
    """A page under retry can take minutes, and the run must not look dead meanwhile.

    Each beat falls due as the timer passes the heartbeat. A heartbeat of a hundredth
    of a second only sets how often the waiting writer looks.
    """
    timer = _Timer()
    fixture = _Fixture(heartbeat_seconds=0.01, timer=timer)
    _shelve(fixture, "Films", range(2))
    release = fixture.adapter.hold("Films")
    walk = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    )
    try:
        await _until(lambda: bool(fixture.adapter.unit_starts))
        start = len(fixture.checkpoints)
        for beat in range(1, 4):
            timer.now += 0.015
            await _until_checkpoints(fixture, start + beat)
        assert fixture.journal == [], "the premise: nothing was fetched while the writer waited"
        beats = [beat for _, beat in fixture.checkpoints[start:] if beat is not None]
        assert len(beats) == 3, "one beat for each heartbeat the timer passed, and no more"
        assert beats == sorted(set(beats)), "a beat that did not move the heartbeat"
    finally:
        release.set()
        run = await walk
    assert run.status is SyncRunStatus.COMPLETED


async def test_pages_that_keep_arriving_below_a_batch_still_let_the_heartbeat_move() -> None:
    """A beat falls due a fixed time after the last commit, not after a silence.

    Forty pages of one item, each two-fifths of a heartbeat after the last on the
    timer, never fill a batch, so a writer that beat only when nothing arrived would
    not beat once in the walk. No page waits on wall time.
    """
    heartbeat = 60.0
    timer = _Timer()
    fixture = _Fixture(heartbeat_seconds=heartbeat, timer=timer)
    _shelve(fixture, "Films", range(1))

    async def _trickle(
        key: str, *, start_index: int = 0, checkpoint: str | None = None
    ) -> AsyncGenerator[UnitPage]:
        for index in range(40):
            timer.now += 0.4 * heartbeat
            yield UnitPage((_item(f"t{index}"),), resume_at=index + 1)

    fixture.adapter.list_unit = _trickle  # type: ignore[method-assign]
    await asyncio.wait_for(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter), 5.0
    )
    carried = [seen for seen, _ in fixture.checkpoints if seen]
    assert carried[0] == 40, "the premise: the unit's forty items rode one commit"
    beats = [beat for seen, beat in fixture.checkpoints[2:] if seen == 0]
    assert len(beats) >= 2, "no beat while pages kept arriving"


async def test_a_unit_that_ends_on_a_batch_boundary_still_says_where_its_plan_stands() -> None:
    """Its end commits an empty batch to save the unit `completed`, and that is a frame.

    The empty batch moved no item counter, but it moved `units_done`: four items in
    batches of two are three frames, the last saying the unit is done.
    """
    fixture = _Fixture(batch_size=2)
    _shelve(fixture, "Films", range(4))
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    frames = [
        (event.data["items_seen"], event.data["units_done"])
        for event in fixture.events.published
        if event.kind is ClientEventKind.SYNC_PROGRESS
    ]
    assert frames == [(2, 0), (4, 0), (4, 1)]
    [unit] = await fixture.runs.units_for(run.id)
    assert unit.status is SyncRunUnitStatus.COMPLETED


async def test_a_unit_that_yields_no_page_is_still_saved_completed() -> None:
    """Emby's seed when the account is watching nothing: planned, and no page at all.

    Its end alone commits it `completed`, at the position it started from.
    """
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    for external_id in _shelve(fixture, "Watching", range(10, 11), stage=WalkStage.SEED):
        fixture.adapter.forget(external_id)
    plan = await fixture.adapter.plan_walk()
    assert [(unit.key, unit.stage, unit.expected_items) for unit in plan.units] == [
        ("library:Films", WalkStage.TITLES, 2),
        ("library:Watching", WalkStage.SEED, 0),
    ], "the premise: an empty seed unit, planned"
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.status is SyncRunStatus.COMPLETED
    assert ("fetched", "library:Watching") not in fixture.journal, "the premise: it fetched nothing"
    units = {unit.unit_key: unit for unit in await fixture.runs.units_for(run.id)}
    seed = units["library:Watching"]
    assert (seed.status, seed.position, seed.items_seen) == (SyncRunUnitStatus.COMPLETED, 0, 0)


async def test_a_delta_with_no_cursor_walks_the_plan_and_never_sweeps() -> None:
    """A console-triggered first sync.

    The row the source no longer has is a fifth of what Usher holds, under the
    retraction ceiling, so only the rule that a delta never sweeps can spare it.
    """
    fixture = _Fixture()
    _shelve(fixture, "Films", range(4))
    await fixture.ingest.ingest_batch(fixture.source.id, [_item("m90")], observed_at=T0)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    assert run.cursor_at is None, "the premise: no walk has completed, so there is no cursor"
    assert [unit.unit_key for unit in await fixture.runs.units_for(run.id)] == ["library:Films"]
    assert run.status is SyncRunStatus.COMPLETED
    assert run.items_retracted == 0
    kept = await fixture.media_items.get_by_external_id(fixture.source.id, "m90")
    assert kept is not None and kept.available is True


async def test_a_delta_with_a_cursor_keeps_the_single_walk() -> None:
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    delta = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    assert delta.cursor_at is not None, "the premise: the full walk gave the delta its cursor"
    assert delta.planned is False
    assert await fixture.runs.units_for(delta.id) == []


async def test_a_bounded_walk_keeps_the_single_walk_even_with_no_cursor() -> None:
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    run = await fixture.service.reconcile(
        fixture.source, SyncRunKind.DELTA, fixture.adapter, max_items=10
    )
    assert run.cursor_at is None, "the premise: a cursorless delta, which would otherwise plan"
    assert run.status is SyncRunStatus.COMPLETED
    assert run.planned is False
    assert await fixture.runs.units_for(run.id) == []


async def test_a_walk_told_not_to_plan_keeps_the_single_walk() -> None:
    """The gap-closer's walk, which stays one stream even unbounded and cursorless."""
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    run = await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False
    )
    assert run.status is SyncRunStatus.COMPLETED
    assert run.items_seen == 2
    assert run.planned is False
    assert await fixture.runs.units_for(run.id) == []


async def test_a_failing_unit_cancels_the_walkers_still_fetching() -> None:
    """Films fails at once while Shows is held; the walk ends without waiting for Shows."""
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    _shelve(fixture, "Shows", range(10, 12))
    fixture.adapter.fail_unit_after("Films", 0)
    release = fixture.adapter.hold("Shows")
    try:
        async with asyncio.timeout(5):
            run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    finally:
        release.set()
    assert run.status is SyncRunStatus.FAILED
    walkers = [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), "__qualname__", "") == "ReconcileService._fetch"
    ]
    assert walkers == []


async def test_a_bug_in_a_walker_is_raised_not_recorded() -> None:
    """A walker hands the writer whatever it raised, so a bug ends the walk loudly.

    A walker that died of it silently would leave the writer waiting for an end that
    never comes, heartbeating forever; the deadline turns that into a failure.
    """
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))

    def _broken(
        key: str, *, start_index: int = 0, checkpoint: str | None = None
    ) -> AsyncGenerator[UnitPage]:
        raise ZeroDivisionError("a bug, not an outage")

    fixture.adapter.list_unit = _broken  # type: ignore[method-assign]
    with pytest.raises(ZeroDivisionError):
        async with asyncio.timeout(5):
            await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)


def test_a_service_with_no_walkers_is_refused() -> None:
    """No walker would fetch, and the writer would wait on an empty queue forever."""
    with pytest.raises(ValueError, match="at least one walker"):
        _Fixture(walkers=0)


@pytest.mark.parametrize("heartbeat_seconds", [0, -0.5, float("nan")])
def test_a_service_whose_heartbeat_is_not_positive_is_refused(heartbeat_seconds: float) -> None:
    """Its beat would always be due, so the writer would beat and never read its queue."""
    with pytest.raises(ValueError, match=f"a positive period, not {heartbeat_seconds} seconds"):
        _Fixture(heartbeat_seconds=heartbeat_seconds)


# -- an unfinished whole-library walk: resumed, refused or closed ------------

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


class _Clock:
    """A clock that reads whatever the case last set."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _given_walk(
    fixture: _Fixture,
    *,
    heartbeat_at: datetime | None,
    status: SyncRunStatus = SyncRunStatus.RUNNING,
    kind: SyncRunKind = SyncRunKind.FULL,
    units: Sequence[tuple[str, SyncRunUnitStatus, int]] = (),
    error: str | None = None,
    finished_at: datetime | None = None,
    planned: bool = True,
) -> SyncRun:
    """An unfinished walk, as a killed or a failed attempt left it.

    A whole-library walk's unless `planned` is false. Each unit is `(library, status,
    position)`, planned in `TITLES`.
    """
    run = SyncRun(
        source_id=fixture.source.id,
        kind=kind,
        status=status,
        error=error,
        heartbeat_at=heartbeat_at,
        started_at=T0,
        finished_at=finished_at,
        planned=planned,
    )
    await fixture.runs.add(run)
    if units:
        await fixture.runs.add_units(
            [
                SyncRunUnit(
                    run_id=run.id,
                    unit_key=f"library:{library}",
                    stage=WalkStage.TITLES,
                    label=f"library {library}",
                    position=position,
                    items_seen=position,
                    status=unit_status,
                )
                for library, unit_status, position in units
            ]
        )
    return run


async def test_a_failed_whole_library_walk_resumes_in_place_from_each_units_position() -> None:
    """The same row and `started_at`, and nothing fetched or counted twice.

    The first attempt leaves Films complete at 5 and Shows failed at 2. The second
    fetches Shows' last two items only, and its sweep spares all nine rows: under a
    fresh `started_at` it would retract the seven an earlier instant stamped, more
    than the ceiling allows, and the run would fail.
    """
    fixture = _Fixture(batch_size=2, walkers=1)
    _shelve(fixture, "Films", range(10, 15))
    _shelve(fixture, "Shows", range(4))
    fixture.adapter.fail_unit_after("Shows", 3)
    first = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert first.status is SyncRunStatus.FAILED, "the premise: the first attempt failed"
    fixture.adapter.clear_failure()
    fixture.adapter.unit_starts.clear()

    second = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert (second.id, second.started_at) == (first.id, first.started_at)
    assert second.status is SyncRunStatus.COMPLETED
    assert (second.error, second.error_code) == (None, None)
    assert second.items_seen == 9
    assert second.items_retracted == 0
    assert fixture.adapter.unit_starts == [("library:Shows", 2)]
    units = await fixture.runs.units_for(first.id)
    assert [(unit.unit_key, unit.status, unit.position) for unit in units] == [
        ("library:Films", SyncRunUnitStatus.COMPLETED, 5),
        ("library:Shows", SyncRunUnitStatus.COMPLETED, 4),
    ]
    assert [run.id for run in await fixture.runs.list_for_source(fixture.source.id)] == [first.id]


@pytest.mark.parametrize(
    ("age", "refused"),
    [
        (timedelta(minutes=10) - timedelta(seconds=1), True),
        (timedelta(minutes=10) - timedelta(microseconds=1), True),
        (timedelta(minutes=10), False),
    ],
)
async def test_a_running_walk_is_refused_until_its_heartbeat_is_ten_minutes_old(
    age: timedelta, refused: bool
) -> None:
    """A killed walk's row still says `running`; only its heartbeat tells it from a live one.

    Ten minutes old, it is closed, and that close is committed before the claim's commit.
    """
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(3))
    walking = await _given_walk(
        fixture, heartbeat_at=NOW - age, units=[("Films", SyncRunUnitStatus.RUNNING, 2)]
    )
    if refused:
        with pytest.raises(
            WalkRefused, match="a whole-library walk of Living Room Emby counts as live"
        ):
            await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
        assert fixture.journal == [], "a refused walk asked the source for something"
        assert await fixture.runs.get(walking.id) == walking, "a refused walk wrote the live row"
        assert len(await fixture.runs.list_for_source(fixture.source.id)) == 1
        return
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert (run.id, run.status) == (walking.id, SyncRunStatus.COMPLETED)
    assert fixture.checkpoints[0] == (0, NOW - age), "the close was not committed first"
    assert fixture.checkpoints[1] == (0, NOW), "the claim's commit kept the dead walk's heartbeat"
    assert fixture.adapter.unit_starts == [("library:Films", 2)]


@pytest.mark.parametrize(
    ("quiet_for", "said"),
    [
        (timedelta(0), "0 s ago, and if its process has stopped, it can be resumed in 10 min"),
        (
            timedelta(seconds=40.75),
            "40 s ago, and if its process has stopped, it can be resumed in 9 min 20 s",
        ),
        (
            timedelta(minutes=1),
            "1 min ago, and if its process has stopped, it can be resumed in 9 min",
        ),
        (
            timedelta(minutes=10) - timedelta(seconds=1),
            "9 min 59 s ago, and if its process has stopped, it can be resumed in 1 s",
        ),
        (
            -timedelta(seconds=30),
            "0 s ago, and if its process has stopped, it can be resumed in 10 min 30 s",
        ),
    ],
)
async def test_a_refusal_says_how_long_the_live_walk_has_been_quiet_and_has_left(
    quiet_for: timedelta, said: str
) -> None:
    """An interrupted walk stays `running` until it goes stale, and nothing on screen says so.

    So the refusal does, in durations: the heartbeat's age rounded down, and the time
    left rounded up, so the two add up to the whole ten minutes. A heartbeat stamped
    by a clock ahead of this one reads as 0 s old, and the time left is still exact.
    """
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(3))
    await _given_walk(
        fixture, heartbeat_at=NOW - quiet_for, units=[("Films", SyncRunUnitStatus.RUNNING, 2)]
    )
    with pytest.raises(WalkRefused) as refused:
        await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert str(refused.value) == (
        f"a whole-library walk of Living Room Emby counts as live: its last heartbeat was {said}"
    )


async def test_a_refused_walk_is_marked_on_its_span(spans: InMemorySpanExporter) -> None:
    """Beside `usher.sync.truncated`: Usher declined, and nothing upstream failed.

    The walk resumed once the live one has gone stale carries no such mark.
    """
    clock = _Clock(NOW)
    fixture = _Fixture(clock=clock)
    _shelve(fixture, "Films", range(3))
    await _given_walk(fixture, heartbeat_at=NOW, units=[("Films", SyncRunUnitStatus.RUNNING, 2)])
    with pytest.raises(WalkRefused):
        await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    clock.now = NOW + STALE_AFTER
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.status is SyncRunStatus.COMPLETED
    pipeline = [span for span in spans.get_finished_spans() if span.name == "sync.reconcile"]
    assert [(span.attributes or {}).get("usher.sync.refused") for span in pipeline] == [
        True,
        None,
    ]


async def test_a_failed_walk_resumes_however_fresh_its_heartbeat() -> None:
    """A `failed` row recorded its own end, so its heartbeat says nothing about a live walk."""
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(3))
    failed = await _given_walk(
        fixture,
        heartbeat_at=NOW,
        status=SyncRunStatus.FAILED,
        error="GET /Users/{user_id}/Items returned HTTP 502",
        units=[("Films", SyncRunUnitStatus.FAILED, 2)],
    )
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert (run.id, run.status, run.error) == (failed.id, SyncRunStatus.COMPLETED, None)


async def test_a_resumed_walk_reads_running_again_so_a_second_walk_is_refused() -> None:
    """The claim's commit stores the resumed row `running`, with no end, as a live walk's.

    Left `failed`, the row would read to a second walk as one to resume, and the two
    would walk it together; left with its last attempt's end, it would say it had
    finished while it walks.
    """
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(3))
    failed = await _given_walk(
        fixture,
        heartbeat_at=NOW - timedelta(hours=1),
        status=SyncRunStatus.FAILED,
        error="GET /Users/{user_id}/Items returned HTTP 502",
        finished_at=NOW - timedelta(hours=1),
        units=[("Films", SyncRunUnitStatus.FAILED, 1)],
    )
    release = fixture.adapter.hold("Films")
    first = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    )
    second: asyncio.Task[SyncRun] | None = None
    try:
        async with asyncio.timeout(5):
            while fixture.commits < 1:
                await asyncio.sleep(0.01)
        claimed = await fixture.runs.get(failed.id)
        assert claimed is not None
        assert claimed.status is SyncRunStatus.RUNNING, "the claim left the resumed row failed"
        assert claimed.finished_at is None, "the claim kept the failed attempt's end"
        second = asyncio.create_task(
            fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
        )
        done, _ = await asyncio.wait({second}, timeout=1)
        assert second in done, "a second walk was not refused while a failed walk resumed"
        assert isinstance(second.exception(), WalkRefused)
    finally:
        release.set()
        if second is not None and not second.done():
            second.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await second
        run = await first
    assert (run.id, run.status) == (failed.id, SyncRunStatus.COMPLETED)


async def test_a_walk_killed_while_planning_is_closed_once_its_heartbeat_is_stale() -> None:
    """Its run and heartbeat were committed and its units never were.

    Refused while the heartbeat is fresh, as a walk still planning must be; then closed
    and replaced, because a run with no plan has nothing to resume.
    """
    clock = _Clock(NOW)
    fixture = _Fixture(clock=clock)
    _shelve(fixture, "Films", range(2))
    planning = await _given_walk(fixture, heartbeat_at=NOW)
    clock.now = NOW + timedelta(minutes=10) - timedelta(seconds=1)
    with pytest.raises(WalkRefused):
        await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    clock.now = NOW + timedelta(minutes=10)

    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert run.id != planning.id
    assert run.status is SyncRunStatus.COMPLETED
    assert [unit.unit_key for unit in await fixture.runs.units_for(run.id)] == ["library:Films"]
    closed = await fixture.runs.get(planning.id)
    assert closed is not None
    assert (closed.status, closed.error) == (SyncRunStatus.FAILED, ABANDONED_ERROR)


async def test_a_cursorless_delta_killed_while_planning_is_closed() -> None:
    """A delta with no cursor walks the plan, and its row carries a heartbeat from its insert.

    Killed before its plan was stored, the row has no units to resume, so the next walk
    closes it and starts afresh.
    """
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(2))
    planning = await _given_walk(
        fixture, heartbeat_at=NOW - timedelta(minutes=10), kind=SyncRunKind.DELTA
    )
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    assert run.id != planning.id
    closed = await fixture.runs.get(planning.id)
    assert closed is not None
    assert (closed.status, closed.error) == (SyncRunStatus.FAILED, ABANDONED_ERROR), (
        "a cursorless delta killed while planning was left running"
    )


@pytest.mark.parametrize("kind", [SyncRunKind.FULL, SyncRunKind.DELTA])
async def test_a_full_walk_an_older_release_left_running_is_closed_by_the_next_walk(
    kind: SyncRunKind,
) -> None:
    """`running`, with no heartbeat and so not planned: `m10h` reads a walk from before both so.

    The next item walk of its source, of either kind, closes it and starts afresh, since
    it has no plan to resume.
    """
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(2))
    legacy = await _given_walk(fixture, heartbeat_at=None, planned=False)

    run = await fixture.service.reconcile(fixture.source, kind, fixture.adapter)

    assert run.id != legacy.id
    assert run.status is SyncRunStatus.COMPLETED
    closed = await fixture.runs.get(legacy.id)
    assert closed is not None
    assert (closed.status, closed.error, closed.error_code, closed.finished_at) == (
        SyncRunStatus.FAILED,
        ABANDONED_ERROR,
        None,
        NOW,
    )


async def test_a_failed_walk_with_no_plan_keeps_its_own_error() -> None:
    """Closed already, so it is not relabelled: its error says why that walk failed.

    It has no units to resume, so a fresh walk starts and leaves it as it was.
    """
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    failed = await _given_walk(
        fixture,
        heartbeat_at=None,
        status=SyncRunStatus.FAILED,
        error="GET /Users/{user_id}/Items returned HTTP 502",
    )
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.id != failed.id
    assert await fixture.runs.get(failed.id) == failed


@pytest.mark.parametrize("kind", [SyncRunKind.FULL, SyncRunKind.DELTA])
async def test_a_live_single_walk_neither_refuses_a_whole_library_walk_nor_is_resumed(
    kind: SyncRunKind,
) -> None:
    """A walk's claim reads only planned walks, so a single walk's row is passed over.

    Read as a claim, the row is live and would refuse the walk. It is left as it was.
    """
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(2))
    single = await _given_walk(fixture, heartbeat_at=NOW, kind=kind, planned=False)
    run = await fixture.service.reconcile(fixture.source, kind, fixture.adapter)
    assert run.id != single.id
    assert [unit.unit_key for unit in await fixture.runs.units_for(run.id)] == ["library:Films"], (
        "the premise: a walk of the plan"
    )
    assert await fixture.runs.get(single.id) == single


async def test_a_live_planned_delta_still_refuses_a_second_after_the_gap_closer_walks() -> None:
    """The gap-closer's single walk adds a newer delta row, unplanned and with no units.

    A delta's claim reads only planned walks, so that row neither hides the live walk
    nor lets a second one start beside it.
    """
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(2))
    live = await _given_walk(
        fixture,
        heartbeat_at=NOW,
        kind=SyncRunKind.DELTA,
        units=[("Films", SyncRunUnitStatus.RUNNING, 1)],
    )
    gap = await fixture.service.reconcile(
        fixture.source, SyncRunKind.DELTA, fixture.adapter, max_items=1, plan=False
    )
    assert gap.started_at > live.started_at, "the premise: the gap-closer's row is the newer"
    assert (gap.status, gap.planned) == (SyncRunStatus.FAILED, False), (
        "the premise: the gap-closer left an unfinished single walk's row"
    )
    assert gap.heartbeat_at is not None, "the premise: only `planned` keeps the row out"

    with pytest.raises(WalkRefused):
        await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)

    assert await fixture.runs.get(live.id) == live, "a refused walk wrote the live row"
    listed = {run.id for run in await fixture.runs.list_for_source(fixture.source.id)}
    assert listed == {live.id, gap.id}, "a second walk started beside the live one"


async def test_a_failed_planned_delta_still_resumes_in_place_after_the_gap_closer_walks() -> None:
    """Its unit failed at 2, and the walk after the gap-closer's continues it from 2."""
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(3))
    failed = await _given_walk(
        fixture,
        heartbeat_at=NOW - timedelta(hours=1),
        status=SyncRunStatus.FAILED,
        kind=SyncRunKind.DELTA,
        units=[("Films", SyncRunUnitStatus.FAILED, 2)],
    )
    gap = await fixture.service.reconcile(
        fixture.source, SyncRunKind.DELTA, fixture.adapter, max_items=1, plan=False
    )
    assert gap.started_at > failed.started_at, "the premise: the gap-closer's row is the newer"
    assert (gap.status, gap.planned) == (SyncRunStatus.FAILED, False), (
        "the premise: the gap-closer left an unfinished single walk's row"
    )
    assert gap.heartbeat_at is not None, "the premise: only `planned` keeps the row out"
    fixture.adapter.unit_starts.clear()

    run = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)

    assert (run.id, run.status) == (failed.id, SyncRunStatus.COMPLETED), "the walk restarted"
    assert fixture.adapter.unit_starts == [("library:Films", 2)]


async def test_a_walk_whose_sweep_was_refused_walks_again_rather_than_resuming() -> None:
    """Four rows the walk never saw are two thirds of the source, so the sweep refuses.

    Every unit completed, so a resume would re-run the sweep on what that walk saw.
    """
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    await fixture.ingest.ingest_batch(
        fixture.source.id, [_item(f"m9{index}") for index in range(4)], observed_at=T0
    )
    refused = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert refused.error_code == RETRACTION_ERROR_CODE, "the premise: the sweep refused"
    fixture.journal.clear()

    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert run.id != refused.id
    fetched = [key for event, key in fixture.journal if event == "fetched"]
    assert fetched == ["library:Films"] * 2, "the library was not read again"
    assert await fixture.runs.get(refused.id) == refused


async def test_a_resumed_unit_that_yields_nothing_completes() -> None:
    """A unit whose committed position is already its end: one empty walk, then complete."""
    fixture = _Fixture()
    _shelve(fixture, "Films", range(3))
    failed = await _given_walk(
        fixture,
        heartbeat_at=None,
        status=SyncRunStatus.FAILED,
        units=[("Films", SyncRunUnitStatus.RUNNING, 3)],
    )
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert (run.id, run.status, run.items_seen) == (failed.id, SyncRunStatus.COMPLETED, 0)
    assert fixture.adapter.unit_starts == [("library:Films", 3)]
    assert fixture.adapter.unit_checkpoints == [None], "it was stored with no checkpoint"
    [unit] = await fixture.runs.units_for(run.id)
    assert (unit.status, unit.position) == (SyncRunUnitStatus.COMPLETED, 3)


async def test_a_resumed_unit_is_handed_its_committed_checkpoint() -> None:
    fixture = _Fixture()
    run = await _given_walk(
        fixture,
        heartbeat_at=None,
        status=SyncRunStatus.FAILED,
        units=[("Films", SyncRunUnitStatus.FAILED, 2)],
        finished_at=T0,
    )
    (films,) = await fixture.runs.units_for(run.id)
    await fixture.runs.save_unit(films.evolve(checkpoint="note-1"))
    handed: list[tuple[int, str | None]] = []

    async def noting(
        key: str, *, start_index: int = 0, checkpoint: str | None = None
    ) -> AsyncGenerator[UnitPage]:
        handed.append((start_index, checkpoint))
        return
        yield

    fixture.adapter.list_unit = noting  # type: ignore[method-assign]
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert handed == [(2, "note-1")]


async def test_an_emby_unit_resumes_judged_by_the_checkpoint_its_walk_stored() -> None:
    """The real adapter's note, through the writer and the store, judges the resume.

    Three hundred films a second apart, in pages of 100. The first attempt commits
    Films at `StartIndex=150` with `m199`'s creation time, then fails asking at 200.
    Eighty films then leave the library: resumed at 150 unjudged, the walk would read
    `m230` on and never `m200` to `m229`.
    """
    fixture = _Fixture(batch_size=100, walkers=1)
    server = FakeEmbyServer()
    server.add_view("films", "Films")
    for index in range(300):
        film = _item(f"m{index:03d}", added_at=T0 + timedelta(seconds=index))
        server.add_item(film, T0)
        server.place(film.external_id, "films")
    emby = EmbyAdapter(
        fixture.source,
        SourceCredentials(username=server.username, password=SecretStr(server.password)),
        client=httpx.AsyncClient(transport=server.transport(), base_url=fixture.source.base_url),
        page_size=100,
        sleep=instant_sleep,
    )
    created = T0 + timedelta(seconds=199) - datetime(1970, 1, 1, tzinfo=UTC)
    lines: list[str] = []
    try:
        server.fail_after = 200
        first = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, emby)
        assert first.status is SyncRunStatus.FAILED, "the premise: the first attempt failed"
        (films,) = [
            u for u in await fixture.runs.units_for(first.id) if u.unit_key == "titles:films"
        ]
        assert (films.position, films.checkpoint, films.status) == (
            150,
            str(created // timedelta(microseconds=1)),
            SyncRunUnitStatus.FAILED,
        )
        for index in range(80):
            server.remove_item(f"m{index:03d}")
        server.fail_after = None
        handle = logger.add(lines.append, level="WARNING", format="{message}")
        try:
            second = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, emby)
        finally:
            logger.remove(handle)
    finally:
        await emby.aclose()
    assert (second.id, second.status) == (first.id, SyncRunStatus.COMPLETED)
    assert [line.rstrip("\n") for line in lines] == [
        "Living Room Emby's listing moved past the overlap before StartIndex=150; "
        "reading again from StartIndex=50"
    ]
    missing = [
        f"m{index:03d}"
        for index in range(80, 300)
        if await fixture.media_items.get_by_external_id(fixture.source.id, f"m{index:03d}") is None
    ]
    assert missing == []


async def test_a_cursored_delta_walks_beside_a_live_whole_library_walk() -> None:
    """Only a whole-library walk claims; a delta with a cursor never waits on one."""
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(2))
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    live = await _given_walk(
        fixture,
        heartbeat_at=NOW,
        kind=SyncRunKind.DELTA,
        units=[("Films", SyncRunUnitStatus.RUNNING, 1)],
    )
    delta = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    assert delta.cursor_at is not None, "the premise: the full walk gave the delta its cursor"
    assert delta.status is SyncRunStatus.COMPLETED
    assert await fixture.runs.get(live.id) == live


# -- after_seed: the watch lane, as soon as the seed has committed -----------


async def test_the_hook_runs_once_the_seed_has_committed_and_before_any_title_is_fetched() -> None:
    fixture = _Fixture()
    _shelve(fixture, "Watched", range(2), stage=WalkStage.SEED)
    _shelve(fixture, "Films", range(10, 13))

    async def after_seed(beat: Callable[[], Awaitable[None]]) -> None:
        fixture.journal.append(("after_seed", "-"))

    await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, after_seed=after_seed
    )

    assert fixture.journal.count(("after_seed", "-")) == 1
    hook = fixture.journal.index(("after_seed", "-"))
    assert fixture.journal.index(("completed", "library:Watched")) < hook
    assert hook < fixture.journal.index(("fetched", "library:Films"))


async def test_a_plan_without_a_seed_never_runs_the_hook() -> None:
    fixture = _Fixture()
    _shelve(fixture, "Films", range(3))
    calls: list[str] = []

    async def after_seed(beat: Callable[[], Awaitable[None]]) -> None:
        calls.append("after_seed")

    await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, after_seed=after_seed
    )
    assert fixture.adapter.unit_starts == [("library:Films", 0)], "the premise: the plan was walked"
    assert calls == []


@pytest.mark.parametrize(
    ("kind", "max_items"),
    [(SyncRunKind.DELTA, 0), (SyncRunKind.FULL, 10)],
    ids=["a cursored delta", "a full walk with max_items"],
)
async def test_a_walk_that_is_not_planned_never_runs_the_hook(
    kind: SyncRunKind, max_items: int
) -> None:
    """The library has a seed, and the planned walk ahead of this one ran the hook.

    That walk is also what gives the delta its cursor.
    """
    fixture = _Fixture()
    _shelve(fixture, "Watched", range(2), stage=WalkStage.SEED)
    _shelve(fixture, "Films", range(10, 13))
    calls: list[str] = []

    async def after_seed(beat: Callable[[], Awaitable[None]]) -> None:
        calls.append("after_seed")

    await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, after_seed=after_seed
    )
    assert calls == ["after_seed"], "the premise: a planned walk of this library runs the hook"
    calls.clear()

    run = await fixture.service.reconcile(
        fixture.source, kind, fixture.adapter, max_items=max_items, after_seed=after_seed
    )

    assert run.status is SyncRunStatus.COMPLETED, "the premise: the walk ran to its end"
    assert await fixture.runs.units_for(run.id) == [], "the premise: the walk was not planned"
    assert calls == []


async def test_a_seed_that_fails_never_runs_the_hook() -> None:
    """The walk ends `failed` at the seed; the resume, once its seed commits, runs the hook."""
    fixture = _Fixture()
    _shelve(fixture, "Watched", range(2), stage=WalkStage.SEED)
    _shelve(fixture, "Films", range(10, 13))
    fixture.adapter.fail_unit_after("Watched", 1)
    calls: list[str] = []

    async def after_seed(beat: Callable[[], Awaitable[None]]) -> None:
        calls.append("after_seed")

    run = await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, after_seed=after_seed
    )

    assert fixture.adapter.unit_starts == [("library:Watched", 0)], (
        "the premise: the seed, and nothing after it, was walked"
    )
    assert run.status is SyncRunStatus.FAILED
    assert calls == []
    fixture.adapter.clear_failure()
    resumed = await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, after_seed=after_seed
    )
    assert resumed.id == run.id, "the premise: the second attempt resumed the first"
    assert resumed.status is SyncRunStatus.COMPLETED
    assert calls == ["after_seed"]


async def test_a_resume_whose_seed_had_completed_still_runs_the_hook() -> None:
    """The attempt that committed the seed may have died before its watch run did.

    The hook records how many unit walks had been asked for when it ran: one, the
    seed, on the first attempt; none on the resume, which skips the seed.
    """
    fixture = _Fixture()
    _shelve(fixture, "Watched", range(2), stage=WalkStage.SEED)
    _shelve(fixture, "Films", range(10, 13))
    fixture.adapter.fail_unit_after("Films", 1)
    calls: list[int] = []

    async def after_seed(beat: Callable[[], Awaitable[None]]) -> None:
        calls.append(len(fixture.adapter.unit_starts))

    first = await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, after_seed=after_seed
    )
    assert first.status is SyncRunStatus.FAILED, "the premise: the walk failed after its seed"
    fixture.adapter.clear_failure()
    fixture.adapter.unit_starts.clear()

    second = await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, after_seed=after_seed
    )

    assert second.id == first.id, "the premise: the second attempt resumed the first"
    assert fixture.adapter.unit_starts == [("library:Films", 0)]
    assert calls == [1, 0]


async def test_the_hook_is_handed_a_beat_that_moves_the_walks_heartbeat() -> None:
    """A watch walk in the hook can outlast `STALE_AFTER`; its beats keep this walk alive.

    The clock stands at `NOW` through the seed's commits and moves just before the beat,
    so only a beat that saves and commits can leave the moved instant on the run.
    """
    clock = _Clock(NOW)
    fixture = _Fixture(clock=clock)
    _shelve(fixture, "Watched", range(2), stage=WalkStage.SEED)
    _shelve(fixture, "Films", range(10, 13))
    committed: list[tuple[int, datetime | None]] = []

    async def after_seed(beat: Callable[[], Awaitable[None]]) -> None:
        committed.append(fixture.checkpoints[-1])
        clock.now = NOW + STALE_AFTER
        await beat()
        committed.append(fixture.checkpoints[-1])

    await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, after_seed=after_seed
    )

    assert committed[0] == (2, NOW), "the premise: the seed's commits beat at NOW"
    assert committed[1] == (2, NOW + STALE_AFTER)


# -- where a planned walk stands: its frames, and each unit's duration -------


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    """A provider of this test's own; `tests/conftest.py` resets the set-once global."""
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


def _unit_durations(reader: InMemoryMetricReader) -> dict[tuple[str, str], tuple[int, float]]:
    """`usher.sync.unit.duration` as `{(source, stage): (count, sum)}`."""
    found: dict[tuple[str, str], tuple[int, float]] = {}
    data = reader.get_metrics_data()
    for resource in data.resource_metrics if data is not None else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != "usher.sync.unit.duration":
                    continue
                for point in metric.data.data_points:
                    assert isinstance(point, HistogramDataPoint)
                    labels = dict(point.attributes or {})
                    found[(str(labels["source"]), str(labels["stage"]))] = (point.count, point.sum)
    return found


async def test_every_frame_of_a_planned_walk_says_where_its_plan_stands() -> None:
    """The seed's own frames say `seed`; from its last commit on, the walk is in `titles`.

    Watched holds the seed and no count, as Emby's seed does, so `items_expected` is
    Films' three alone. `items_seen` leads each frame as the premise of its order.
    """
    fixture = _Fixture(batch_size=2)
    _shelve(fixture, "Watched", range(3), stage=WalkStage.SEED)
    _shelve(fixture, "Films", range(10, 13))
    fixture.adapter.uncounted("Watched")

    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    frames = [
        (
            event.data["items_seen"],
            event.data["stage"],
            event.data["units_done"],
            event.data["units_total"],
            event.data["items_expected"],
        )
        for event in fixture.events.published
        if event.kind is ClientEventKind.SYNC_PROGRESS
    ]
    assert frames == [
        (2, "seed", 0, 2, 3),
        (3, "titles", 1, 2, 3),
        (5, "titles", 1, 2, 3),
        (6, "titles", 2, 2, 3),
    ]


async def test_a_single_walks_frames_carry_no_plan() -> None:
    """Every frame has the same keys; a walk without a plan says `None` for all four."""
    fixture = _Fixture(batch_size=2)
    for index in range(3):
        fixture.adapter.seed(_item(f"m{index}"), T0)

    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False)

    frames = [
        event.data
        for event in fixture.events.published
        if event.kind is ClientEventKind.SYNC_PROGRESS
    ]
    assert [frame["items_seen"] for frame in frames] == [2, 3], "the premise: two commits"
    assert {
        (frame["stage"], frame["units_done"], frame["units_total"], frame["items_expected"])
        for frame in frames
    } == {(None, None, None, None)}


async def test_a_unit_is_timed_from_its_walkers_claim_to_its_last_commit(
    meter_reader: InMemoryMetricReader,
) -> None:
    """Once per completed unit, under its stage, from the claim rather than the stage's start.

    One walker, so Shorts is not claimed until Films' last page has been fetched, after
    the hold: the 90 s that pass while Films is held are Films' alone, and Shorts records
    none of them. They pass on the timer while the wall clock stands still, so a duration
    read off the clock is 0.
    """
    timer = _Timer()
    fixture = _Fixture(batch_size=1, walkers=1, clock=_Clock(NOW), timer=timer)
    _shelve(fixture, "Watched", range(1), stage=WalkStage.SEED)
    _shelve(fixture, "Films", range(10, 12))
    _shelve(fixture, "Shorts", range(20, 21))
    release = fixture.adapter.hold("Films")

    walk = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    )
    async with asyncio.timeout(5):
        while ("library:Films", 0) not in fixture.adapter.unit_starts:
            await asyncio.sleep(0)
    timer.now += 90
    release.set()
    run = await asyncio.wait_for(walk, 5)

    assert run.status is SyncRunStatus.COMPLETED, "the premise: the walk finished"
    assert fixture.adapter.unit_starts == [
        ("library:Watched", 0),
        ("library:Films", 0),
        ("library:Shorts", 0),
    ], "the premise: Films, the larger, was claimed before Shorts"
    assert _unit_durations(meter_reader) == {
        (fixture.source.name, "seed"): (1, 0.0),
        (fixture.source.name, "titles"): (2, 90.0),
    }


async def test_a_unit_that_fails_records_no_duration(meter_reader: InMemoryMetricReader) -> None:
    """Films completes and is timed; Shorts, claimed after it, fails and records nothing.

    One walker, so Shorts is not claimed until Films' last page has been fetched, after
    the hold. Films is held for 90 s on the timer, so its point cannot pass for one Shorts
    recorded at no cost.
    """
    timer = _Timer()
    fixture = _Fixture(batch_size=1, walkers=1, clock=_Clock(NOW), timer=timer)
    _shelve(fixture, "Films", range(10, 12))
    _shelve(fixture, "Shorts", range(20, 21))
    fixture.adapter.fail_unit_after("Shorts", 0)
    release = fixture.adapter.hold("Films")

    walk = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    )
    async with asyncio.timeout(5):
        while ("library:Films", 0) not in fixture.adapter.unit_starts:
            await asyncio.sleep(0)
    timer.now += 90
    release.set()
    run = await asyncio.wait_for(walk, 5)

    assert run.status is SyncRunStatus.FAILED, "the premise: Shorts failed the walk"
    assert fixture.adapter.unit_starts == [("library:Films", 0), ("library:Shorts", 0)], (
        "the premise: Shorts was claimed, after Films"
    )
    assert _unit_durations(meter_reader) == {(fixture.source.name, "titles"): (1, 90.0)}


# -- a cancelled walk --------------------------------------------------------


async def _until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until `condition` holds, failing rather than hanging."""
    for _ in range(10_000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never held")


async def _until_async(condition: Callable[[], bool]) -> None:
    """`_until`, sleeping between checks, for a writer that wakes on wall time to look."""
    for _ in range(1_000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the condition never held")


async def _until_checkpoints(fixture: _Fixture, count: int) -> None:
    """`_until_async` for the fixture's commits to have recorded `count` checkpoints."""
    await _until_async(lambda: len(fixture.checkpoints) >= count)


async def test_a_cancelled_single_walk_closes_its_run_and_stays_cancelled(
    fixture: _Fixture,
) -> None:
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False)
    listing = asyncio.Event()

    async def stalled(since: AwareDatetime | None = None) -> AsyncGenerator[SourceItem]:
        listing.set()
        await asyncio.Event().wait()
        yield _item("m1")

    fixture.adapter.list_items = stalled  # type: ignore[method-assign]
    task = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    )
    async with asyncio.timeout(5):
        await listing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(5):
            await task

    stored = await fixture.runs.latest_run(fixture.source.id, SyncRunKind.DELTA)
    assert stored is not None
    assert (stored.status, stored.error) == (SyncRunStatus.FAILED, CANCELLED_ERROR)
    assert fixture.rollbacks == 1


async def test_a_cancelled_planned_walk_is_resumed_at_once(fixture: _Fixture) -> None:
    _shelve(fixture, "Films", range(3))
    release = fixture.adapter.hold("Films")
    task = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    )
    await _until(lambda: bool(fixture.adapter.unit_starts))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(5):
            await task
    cancelled = await fixture.runs.latest_run(fixture.source.id, SyncRunKind.FULL)
    assert cancelled is not None
    assert (cancelled.status, cancelled.error) == (SyncRunStatus.FAILED, CANCELLED_ERROR)

    release.set()
    resumed = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert (resumed.id, resumed.status) == (cancelled.id, SyncRunStatus.COMPLETED)


async def test_a_walk_whose_close_fails_still_ends_cancelled(fixture: _Fixture) -> None:
    async def reset() -> None:
        raise RuntimeError("connection reset")

    fixture.service._rollback = reset  # the close's own failure, not the walk's
    _shelve(fixture, "Films", range(3))
    fixture.adapter.hold("Films")
    task = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    )
    await _until(lambda: bool(fixture.adapter.unit_starts))
    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(5):
                await task
    finally:
        logger.remove(sink)
    stored = await fixture.runs.latest_run(fixture.source.id, SyncRunKind.FULL)
    assert stored is not None
    assert stored.status is SyncRunStatus.RUNNING, "the premise: nothing closed it"
    assert [line.rstrip("\n") for line in lines] == [
        f"a cancelled sync of {fixture.source.name} could not close its run {stored.id} "
        "(connection reset); it counts as live until its heartbeat is 10 minutes old"
    ]


# -- every item walk beats, and the next one closes a dead one ---------------


async def test_a_single_walk_is_stored_with_a_heartbeat_and_not_planned(fixture: _Fixture) -> None:
    run = await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False
    )
    assert fixture.checkpoints[0][1] is not None, "the run's first commit carried no heartbeat"
    stored = await fixture.runs.get(run.id)
    assert stored is not None and stored.planned is False


async def test_a_planned_walk_is_stored_planned(fixture: _Fixture) -> None:
    _shelve(fixture, "Films", range(2))
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    stored = await fixture.runs.get(run.id)
    assert stored is not None and stored.planned is True


async def test_a_single_walk_beats_while_a_page_is_slow() -> None:
    """Each beat falls due as the timer passes the heartbeat, while no item arrives.

    A heartbeat of a hundredth of a second only sets how often the waiting writer looks;
    the timer, moved one and a half heartbeats at a time, says when a beat is due.
    """
    timer = _Timer()
    fixture = _Fixture(heartbeat_seconds=0.01, timer=timer)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False)
    listing, release = asyncio.Event(), asyncio.Event()

    async def slow(since: AwareDatetime | None = None) -> AsyncGenerator[SourceItem]:
        listing.set()
        await release.wait()
        yield _item("m1")

    fixture.adapter.list_items = slow  # type: ignore[method-assign]
    before = len(fixture.checkpoints)
    task = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    )
    await asyncio.wait_for(listing.wait(), 5)
    for beat in range(1, 3):
        timer.now += 0.015
        await _until_checkpoints(fixture, before + 1 + beat)
    release.set()
    run = await task
    # The last commit closes the run, and a close moves no heartbeat.
    beats = [heartbeat for _, heartbeat in fixture.checkpoints[before:-1]]
    assert run.status is SyncRunStatus.COMPLETED
    assert len(beats) == 4, "the insert, two beats and the batch, and nothing more"
    assert (
        all(b is not None for b in beats)
        and beats == sorted(beats)  # type: ignore[type-var]
        and len(set(beats)) == len(beats)
    )


async def test_items_that_keep_arriving_below_a_batch_still_let_a_single_walk_beat() -> None:
    """A single walk's beat falls due a fixed time after its last commit, as a plan's does.

    Forty items, each two-fifths of a heartbeat after the last on the timer, never fill
    a batch, so a writer that beat only when nothing arrived would not beat once. Each
    item waits a turn of the loop, so the writer takes it before the timer moves again.
    """
    heartbeat = 60.0
    timer = _Timer()
    fixture = _Fixture(heartbeat_seconds=heartbeat, timer=timer)

    async def trickle(since: AwareDatetime | None = None) -> AsyncGenerator[SourceItem]:
        for index in range(40):
            timer.now += 0.4 * heartbeat
            yield _item(f"t{index}")
            await asyncio.sleep(0)

    fixture.adapter.list_items = trickle  # type: ignore[method-assign]
    run = await asyncio.wait_for(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False),
        5.0,
    )
    assert run.status is SyncRunStatus.COMPLETED
    carried = [seen for seen, _ in fixture.checkpoints if seen]
    assert carried[0] == 40, "the premise: the forty items rode one commit"
    beats = [beat for seen, beat in fixture.checkpoints[1:] if seen == 0]
    assert len(beats) >= 2, "no beat while items kept arriving"


async def test_a_single_walk_beats_with_each_batch() -> None:
    fixture = _Fixture(batch_size=2)
    for index in range(4):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    before = len(fixture.checkpoints)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False)
    batches = [cp for cp in fixture.checkpoints[before:] if cp[0] in (2, 4)]
    assert [seen for seen, _ in batches][:2] == [2, 4]
    assert batches[0][1] is not None and batches[0][1] < batches[1][1]  # type: ignore[operator]


async def test_a_single_walks_listing_error_drops_the_partial_batch() -> None:
    fixture = _Fixture(batch_size=2)
    for index in range(5):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    fixture.adapter.fail_after(3)
    run = await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False
    )
    assert (run.status, run.items_seen) == (SyncRunStatus.FAILED, 2)


async def test_a_truncated_single_walk_does_not_wait_for_the_listing(fixture: _Fixture) -> None:
    async def endless(since: AwareDatetime | None = None) -> AsyncGenerator[SourceItem]:
        for index in range(3):
            yield _item(f"m{index}")
        await asyncio.Event().wait()

    fixture.adapter.list_items = endless  # type: ignore[method-assign]
    run = await asyncio.wait_for(
        fixture.service.reconcile(
            fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False, max_items=2
        ),
        5.0,
    )
    assert (run.status, run.error_code, run.items_seen) == (
        SyncRunStatus.FAILED,
        CEILING_ERROR_CODE,
        2,
    )


async def test_a_truncated_single_walk_closes_its_listing() -> None:
    """The reader the walk cancels closes its listing, which ends a listing's read-ahead."""
    fixture = _Fixture(batch_size=2)
    closed: list[str] = []
    # Held, so only an explicit close can run a listing's `finally`: one dropped would
    # be closed by the loop's finalizer whether or not the reader closed it.
    listings: list[AsyncGenerator[SourceItem]] = []

    async def endless() -> AsyncGenerator[SourceItem]:
        try:
            for index in range(100):
                yield _item(f"m{index}")
        finally:
            closed.append("closed")

    def listing(since: AwareDatetime | None = None) -> AsyncGenerator[SourceItem]:
        listings.append(endless())
        return listings[-1]

    fixture.adapter.list_items = listing  # type: ignore[method-assign]
    run = await asyncio.wait_for(
        fixture.service.reconcile(
            fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False, max_items=2
        ),
        5.0,
    )
    assert run.error_code == CEILING_ERROR_CODE, "the premise: the walk stopped at its ceiling"
    assert len(listings) == 1, "the premise: one listing, opened by this walk"
    assert closed == ["closed"]


@pytest.mark.parametrize("heartbeat_at", [NOW - STALE_AFTER, None])
async def test_a_dead_item_walk_is_closed_abandoned_before_the_next_walk(
    heartbeat_at: datetime | None,
) -> None:
    fixture = _Fixture(clock=lambda: NOW)
    dead = SyncRun(
        source_id=fixture.source.id,
        kind=SyncRunKind.DELTA,
        heartbeat_at=heartbeat_at,
        started_at=T0,
    )
    await fixture.runs.add(dead)
    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        await fixture.service.reconcile(
            fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False
        )
    finally:
        logger.remove(sink)
    closed = await fixture.runs.get(dead.id)
    assert closed is not None
    assert (closed.status, closed.error, closed.finished_at) == (
        SyncRunStatus.FAILED,
        ABANDONED_ERROR,
        NOW,
    )
    assert [line.rstrip("\n") for line in lines] == [
        "closed 1 item walk(s) of Living Room Emby whose process had stopped"
    ]


async def test_a_live_single_walk_is_left_running() -> None:
    fixture = _Fixture(clock=lambda: NOW)
    live = SyncRun(
        source_id=fixture.source.id,
        kind=SyncRunKind.DELTA,
        heartbeat_at=NOW - STALE_AFTER + timedelta(microseconds=1),
        started_at=T0,
    )
    await fixture.runs.add(live)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False)
    assert await fixture.runs.get(live.id) == live


async def test_a_dead_planned_walk_is_closed_then_resumed_in_place() -> None:
    fixture = _Fixture(clock=lambda: NOW)
    _shelve(fixture, "Films", range(2))
    dead = await _given_walk(
        fixture, heartbeat_at=NOW - STALE_AFTER, units=[("Films", SyncRunUnitStatus.PENDING, 0)]
    )
    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    finally:
        logger.remove(sink)
    assert (run.id, run.status) == (dead.id, SyncRunStatus.COMPLETED)
    assert "closed 1 item walk(s) of Living Room Emby whose process had stopped" in [
        line.rstrip("\n") for line in lines
    ]


async def test_a_walk_closes_no_watch_run_and_no_other_sources_walk() -> None:
    """Both are dead and neither is this walk's to close, so nothing is, and nothing is logged.

    The watch lane closes its own runs, and another source's walks are that source's.
    """
    fixture = _Fixture(clock=_Clock(NOW))
    watch = SyncRun(
        source_id=fixture.source.id, kind=SyncRunKind.WATCH_STATE, heartbeat_at=None, started_at=T0
    )
    elsewhere = SyncRun(source_id=new_id(), kind=SyncRunKind.FULL, heartbeat_at=None, started_at=T0)
    for one in (watch, elsewhere):
        await fixture.runs.add(one)
    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        run = await fixture.service.reconcile(
            fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False
        )
    finally:
        logger.remove(sink)
    assert run.status is SyncRunStatus.COMPLETED, "the premise: the walk ran to its end"
    assert lines == []
    assert await fixture.runs.get(watch.id) == watch
    assert await fixture.runs.get(elsewhere.id) == elsewhere


async def test_a_walk_refused_by_a_live_walk_still_commits_its_close() -> None:
    """The close is committed before the claim, so a refusal there leaves the dead walk closed.

    The caller of a refused walk commits nothing, so a close riding a later commit is lost.
    """
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(2))
    live = await _given_walk(
        fixture, heartbeat_at=NOW, units=[("Films", SyncRunUnitStatus.RUNNING, 1)]
    )
    dead = SyncRun(
        source_id=fixture.source.id, kind=SyncRunKind.DELTA, heartbeat_at=None, started_at=T0
    )
    await fixture.runs.add(dead)
    with pytest.raises(WalkRefused):
        await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert await fixture.runs.get(live.id) == live, "the premise: the live walk was left alone"
    closed = await fixture.runs.get(dead.id)
    assert closed is not None
    assert (closed.status, closed.error) == (SyncRunStatus.FAILED, ABANDONED_ERROR)
    assert fixture.commits == 1, "the close was not committed before the refusal"


async def test_the_claim_judges_a_walk_at_the_instant_its_close_did() -> None:
    """One instant for both, so a walk the close left alone as live is refused.

    Its heartbeat is half a second from stale at the walk's first clock read. Judged at
    a second read, a second later, it would be taken for dead and resumed.
    """
    clock = _Ticks()
    fixture = _Fixture(clock=clock)
    _shelve(fixture, "Films", range(3))
    first_read = LATER + timedelta(seconds=1)
    walking = await _given_walk(
        fixture,
        heartbeat_at=first_read - STALE_AFTER + timedelta(milliseconds=500),
        units=[("Films", SyncRunUnitStatus.RUNNING, 2)],
    )
    assert clock.reads == 0, "the premise: the walk's first read is the clock's first"
    assert is_live(walking, first_read), "the premise: live at the first read"
    assert not is_live(walking, first_read + timedelta(seconds=1)), "the premise: dead at a second"
    with pytest.raises(WalkRefused):
        await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert await fixture.runs.get(walking.id) == walking


async def test_a_single_walk_whose_batches_commit_often_enough_never_beats() -> None:
    """A beat falls due `heartbeat_seconds` after the last commit, not after the last beat.

    Four batches, each two-fifths of a heartbeat after the last on the timer, need none;
    a beat counted from the walk's start would be due by the fourth. Each item waits on
    its own gate, so no wall time decides it.
    """
    heartbeat = 60.0
    timer = _Timer()
    fixture = _Fixture(batch_size=1, heartbeat_seconds=heartbeat, timer=timer)
    gates = [asyncio.Event() for _ in range(4)]

    async def gated(since: AwareDatetime | None = None) -> AsyncGenerator[SourceItem]:
        for index, gate in enumerate(gates):
            await gate.wait()
            yield _item(f"m{index}")

    fixture.adapter.list_items = gated  # type: ignore[method-assign]
    walk = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False)
    )
    origin = timer.now
    for index, gate in enumerate(gates):
        if index:
            timer.now += 0.4 * heartbeat
        gate.set()
        await _until_checkpoints(fixture, 2 + index)
    run = await asyncio.wait_for(walk, 5)
    assert run.status is SyncRunStatus.COMPLETED
    assert timer.now - origin > heartbeat, "the premise: the walk outlasted a heartbeat"
    # The insert, four batches and the close, and no commit of a heartbeat alone.
    assert [seen for seen, _ in fixture.checkpoints] == [0, 1, 2, 3, 4, 4]


async def test_a_single_walks_reader_reads_no_more_than_a_batch_ahead() -> None:
    """While the writer commits a batch, the listing waits: a batch queued, an item in hand.

    An unbounded queue would read the whole listing into memory behind a slow database.
    """
    fixture = _Fixture(batch_size=2)
    yielded = 0

    async def counted(since: AwareDatetime | None = None) -> AsyncGenerator[SourceItem]:
        nonlocal yielded
        for index in range(20):
            yielded += 1
            yield _item(f"m{index}")

    fixture.adapter.list_items = counted  # type: ignore[method-assign]
    commit = fixture.service._commit
    held, release = asyncio.Event(), asyncio.Event()

    async def _commit_held_at_the_first_batch() -> None:
        if fixture.commits == 1:
            held.set()
            await release.wait()
        await commit()

    fixture.service._commit = _commit_held_at_the_first_batch
    walk = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False)
    )
    try:
        await asyncio.wait_for(held.wait(), 5)
        await asyncio.sleep(0.05)
        # The batch being committed, a batch queued, and one item waiting to join it.
        assert yielded == 2 + 2 + 1
    finally:
        release.set()
        run = await walk
    assert (run.status, run.items_seen) == (SyncRunStatus.COMPLETED, 20)
