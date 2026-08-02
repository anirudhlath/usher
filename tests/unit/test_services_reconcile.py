"""The reconcile lane, against port fakes and `FakeSourceAdapter`.

The three cases that matter are the three failure paths. A reconciler that
sweeps after a failed walk is the one bug the whole `list_items` contract
exists to prevent, restored one layer up.

**`test_a_walk_that_raises_sweeps_nothing` is built so the sweep guard cannot
rescue it, and that is the whole point of its shape.** Both shapes were run
against the mutation (the sweep moved into a `finally:`) and both fail, but
for different reasons, and only one of them is about the hazard:

- *The plan's shape* -- seven items, fail the second walk after three, one
  batch -- writes **nothing** before the failure, so the sweep would retract
  7 of 7. That is 100%, the ADR-0015 ceiling refuses it, and
  `AvailabilitySweepRefused` then escapes the `finally:` and propagates out
  of `reconcile` entirely. The case fails on an uncaught exception rather
  than on its own assertion, and it fails only because the guard's arithmetic
  happened to fire. It never exercises a sweep that *succeeds* after a failed
  walk.
- *The shape below* flushes eight of ten items first, so a `finally:` sweep
  retracts exactly two healthy rows -- 20%, under the ceiling, no refusal, no
  exception, a run that merely reports `FAILED` while two available items
  quietly became unavailable. That is what a real mid-walk network failure
  looks like against a library big enough to need batching, and the case
  fails on `m8 was retracted by a walk that failed`, which is the assertion
  it was written to make.

The guard is not a second line of defence for this. It fires on a *fraction*,
so it catches the catastrophe and misses the quiet one.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.event_publisher import FakeEventPublisher
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.source_adapter import FakeSourceAdapter
from tests.fakes.sync_run_repository import FakeSyncRunRepository
from tests.fakes.title_match_repository import FakeTitleMatchRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.sync import SyncRunKind, SyncRunStatus
from usher.ports.errors import PortUnavailable, UsherPortError
from usher.ports.events import ClientEventKind
from usher.ports.source import SourceItem, SourceItemKind
from usher.services.ingest import IngestService
from usher.services.matching import MatchService
from usher.services.reconcile import ReconcileService

T0 = datetime(2026, 7, 1, tzinfo=UTC)
# After every run's `started_at`, which defaults to `now()`. A "changed
# since the last completed run" fixture has to be later than a wall-clock
# instant taken during the test, so it is a date rather than a plausible
# one -- `FakeSourceAdapter` filters `changed_at < since` exactly as Emby's
# `MinDateLastSaved` does, and a nominally-recent 2026-07-30 is *before* the
# run that just started and would be filtered out of every delta below.
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


class _Fixture:
    def __init__(self, *, batch_size: int = 1_000, max_retract_fraction: float = 0.25) -> None:
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
        self.service = ReconcileService(
            ingest=IngestService(
                matcher=MatchService(titles=self.titles, matching=self.matching, queue=self.queue),
                matching=self.matching,
                media_items=self.media_items,
                episodes=FakeEpisodeRepository(),
                queue=self.queue,
            ),
            media_items=self.media_items,
            runs=self.runs,
            events=self.events,
            commit=self._commit,
            batch_size=batch_size,
            max_retract_fraction=max_retract_fraction,
        )

    async def _commit(self) -> None:
        self.commits += 1


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
    """PRD 02: "Items that vanish from a source get `available = false`" --
    and items that come back must come back. The sweep only ever sets false;
    appearing in a walk is what sets it true again."""
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
    """`observed_at=run.started_at`, deterministically. The plan's mutation
    table predicted `datetime.now(UTC)` here would make the retraction case
    "flaky/wrong"; it does neither, because a per-row `now()` is always
    *later* than `started_at` and the sweep's `<` therefore still spares
    everything the run saw. What it actually breaks is the meaning of the
    column -- `last_seen_at` stops being "the run that saw this" -- and an
    equality assertion against the run's own instant is what notices."""
    for index in range(5):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    fixture.service._batch_size = 2
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    for index in range(5):
        stored = await fixture.media_items.get_by_external_id(fixture.source.id, f"m{index}")
        assert stored is not None
        assert stored.last_seen_at == run.started_at, f"m{index} carries its own instant"


async def test_a_walk_longer_than_one_batch_stores_every_item(fixture: _Fixture) -> None:
    """The trailing partial batch. Seven items at a batch size of two is
    three full batches and one of one -- and a `_walk` that flushed only on
    the size threshold silently drops the last page of every walk whose item
    count is not a multiple of the batch size, which is almost all of them."""
    for index in range(7):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    fixture.service._batch_size = 2
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.items_seen == 7
    assert await fixture.media_items.count_for_source(fixture.source.id) == 7


async def test_a_run_checkpoints_every_batch(fixture: _Fixture) -> None:
    """1,126,674 items is hours. A run that recorded its counters only at the
    end tells an operator nothing while it is going, and PRD 10's dashboard-3
    panel plots exactly those counters."""
    for index in range(7):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    fixture.service._batch_size = 2
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    # Four batches, plus the run's own insert and its final save.
    assert fixture.commits >= 6, fixture.commits


async def test_a_run_records_its_matched_and_unmatched_counts(fixture: _Fixture) -> None:
    """PRD 10's dashboard 3. A run that reported `items_seen` and left the
    other two at zero makes "how much of this library does Usher actually
    know" unanswerable from the sync history."""
    fixture.adapter.seed(_item("m1"), T0)
    fixture.adapter.seed(_item("m2", name="Home Video 2004", year=None, provider_ids={}), T0)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert (run.items_seen, run.items_matched, run.items_unmatched) == (2, 1, 1)


async def test_the_run_is_recorded_before_the_walk_starts(fixture: _Fixture) -> None:
    """A run row that only appears once the walk finishes leaves an operator
    with no way to see an in-flight sync, and leaves a killed process with no
    trace at all. It is inserted and committed first, `RUNNING`."""
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
    """**The failure this milestone is most warned about.** A generator that
    stops because the adapter gave up is indistinguishable from one that
    finished, which is why `list_items` is contracted to raise -- and that
    guarantee is worth exactly nothing if the reconciler sweeps either way.

    The seeded items are all still present on the source; only the transport
    failed. A reconciler that swept here marks a healthy 1,126,674-item
    library unavailable over one flaky request.

    Eight of ten items are flushed before the failure, deliberately: that
    leaves two stale rows, 20% of the source, *under* the 25% ceiling -- so
    the ADR-0015 guard does not fire and cannot rescue a sweep that should
    never have run. Verified by mutation: with the sweep in a `finally:`,
    this case fails on `m8 was retracted by a walk that failed`.
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
    """The other half of committing per batch. A crash costs the batch in
    flight, never the walk -- 1,126,674 items is hours, and re-walking from
    the start after every transient failure is how a sync never finishes.

    The `items_seen` assertions are the ones with teeth, and they are about
    the *durable record* rather than the walk. `SyncRun` is frozen, `_flush`
    saves an evolved copy per batch, and the failure handler evolves whatever
    binding it holds -- so a handler reading the pre-walk run writes
    `items_seen = 0` over a checkpoint that had recorded eight, and PRD 10's
    dashboard 3 plots that zero. Found by running it; `BootstrapService`
    documents the identical trap.
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
    """The residual `list_items`' contract does not cover: a walk that
    *completes* and returns almost nothing. ADR-0015."""
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
    """A refusal fails the run, and the run's *writes* must survive it
    anyway. The alternative is the mirror-image bug: a source that has
    genuinely shrunk below the ceiling can never record anything again,
    because every walk's upsert half is rolled back with the sweep's
    refusal."""
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
    """`reconcile` swallows `UsherPortError` so `usher sync` can carry on to
    the next source. Anything else is a bug in this process, and recording it
    as a failed *sync* hides it behind an operational-looking row."""

    async def _explode(*args: object, **kwargs: object) -> None:
        raise ZeroDivisionError("a bug, not an outage")

    fixture.media_items.upsert_many = _explode  # type: ignore[method-assign, assignment]
    fixture.adapter.seed(_item("m1"), T0)
    with pytest.raises(ZeroDivisionError):
        await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)


async def test_a_run_that_could_not_reach_the_source_at_all_is_recorded(
    fixture: _Fixture,
) -> None:
    """`usher sync` across three sources needs the second and third to run
    when the first is unreachable, so this returns a durable record rather
    than raising."""
    fixture.adapter.go_offline()
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.status is SyncRunStatus.FAILED
    assert run.items_seen == 0
    assert isinstance(run.error, str) and run.error
    stored = await fixture.runs.get(run.id)
    assert stored is not None and stored.status is SyncRunStatus.FAILED


# -- the delta lane ---------------------------------------------------------


async def test_a_delta_walk_uses_the_last_completed_cursor(fixture: _Fixture) -> None:
    """Resuming from the newest run of *any* status would skip everything a
    failed run never reached, silently. `latest_completed_cursor` is the
    method, and this is why."""
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
    """A completed delta must move the cursor on, or every delta re-walks
    from the last full run and the lane saves nothing."""
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
    """A full walk that inherited a cursor would return only what changed and
    then sweep -- the exact combination ADR-0015 exists to make unreachable."""
    fixture.adapter.seed(_item("m1"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    # T0, deliberately: it is *older* than the cursor a delta would have
    # inherited, so a full walk that used one would filter it out entirely.
    fixture.adapter.seed(_item("m2"), T0)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.cursor_at is None
    assert run.items_seen == 2


async def test_a_delta_walk_never_sweeps(fixture: _Fixture) -> None:
    """A delta walk returns only what changed, so by construction almost
    everything is "unseen". Sweeping after one retracts the entire library --
    and the guard would catch it, which would make every delta run fail
    rather than merely be wrong. Only a full walk may retract."""
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
    """The version of the case above that the ADR-0015 guard cannot rescue.
    With ten items and one changed, a sweeping delta would retract nine --
    90%, refused, so the run merely fails and nothing is lost. Here only two
    of ten are stale, which is under the ceiling: a sweeping delta succeeds
    and silently retracts two available items."""
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


# -- telemetry --------------------------------------------------------------


async def test_the_reconcile_span_is_a_child_of_whatever_is_active(
    fixture: _Fixture, spans: InMemorySpanExporter
) -> None:
    """M1 wired `FastAPIInstrumentor` specifically so a pipeline triggered by
    a request nests under that request's server span. A service that started
    a root span -- `tracer.start_span(..., context=Context())`, or work
    handed to a task created before the span existed -- throws that away and
    "what happened in this request" stops being answerable."""
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
    """PRD 10 reads run outcomes off spans as well as off `sync_runs`. A
    failure that only ever reached the database row is invisible in a trace
    view, which is where an operator looks first."""
    fixture.adapter.go_offline()
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    pipeline = [span for span in spans.get_finished_spans() if span.name == "sync.reconcile"]
    assert pipeline
    assert pipeline[0].attributes is not None
    assert pipeline[0].attributes.get("usher.failed") is True


# -- the things a fake cannot say -------------------------------------------


def test_the_service_never_imports_a_storage_or_transport_library() -> None:
    """ADR-0009 and PRD 01's layering rule, at module level. `import-linter`
    already forbids `usher.services -> usher.db`; this catches the other
    half, which no contract expresses: a service reaching for `httpx`,
    `sqlalchemy` or `asyncpg` directly to branch on a failure kind, instead
    of on `usher.ports.errors`."""
    import usher.services.reconcile as module

    source = (module.__file__ or "").replace(".pyc", ".py")
    text = open(source).read()  # noqa: SIM115
    for forbidden in ("httpx", "sqlalchemy", "asyncpg", "usher.db"):
        assert f"import {forbidden}" not in text
        assert f"from {forbidden}" not in text


async def test_reconcile_never_raises_a_port_error(fixture: _Fixture) -> None:
    """Every `UsherPortError` subclass, not just the two the other cases
    happen to produce. A handler that named `PortUnavailable` specifically
    would let `PortAuthFailed`, `PortRateLimited` and `PortDataMalformed`
    escape, and `usher sync` would stop at the first source with an expired
    credential."""

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
    """`seen_since=run.started_at`, not `now()`. With `now()` the window
    closes *after* the walk wrote its rows, so every row the run just stamped
    is `< seen_since` and the whole source is retracted -- which the guard
    then refuses, turning every successful walk into a failed run."""
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
    """Per batch, not per run. A nightly walk of the one measured library
    flushes 1,127 of these and an admin UI's progress bar is the point of
    them; one at the end is a bar that jumps from 0% to 100%.

    Batch size 2 against 3 items, so a per-run publisher reports 1 where a
    per-batch one reports 2 -- the count is only evidence because the
    denominator is held fixed."""
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
    """PRD 07 marks it "Admin UI only", and the scoping is what implements
    that: a `?titles=` subscriber never sees one. A detail screen that
    re-rendered on each of a walk's 1,127 batches is the failure the filter
    exists for."""
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
    """The events are published per *flush*, so a walk that dies halfway has
    already told the admin UI how far it got -- which is the same reason
    `_flush` commits per batch. Nothing announces the failure itself: PRD
    07's SSE table has no such event, and `sync_runs` is where a failure is
    recorded."""
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
