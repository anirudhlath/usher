"""The server process's background lanes.

Driven against a `Pipeline` of port fakes rather than a database, which is
what `composition.UnitOfWork` being a callable buys: the supervisor's own
subject is task lifecycle, and a real Postgres would only make the same
assertions slower. The lanes are exercised against real Postgres in
`tests/integration/test_lanes_in_the_server_process.py`, which is where
"the worker lane really runs inside `create_app`" is settled.

**Two concurrency claims live here and neither is asserted on a count.**
"A lane that crashes does not take the others down" is satisfied by a
supervisor whose other lane never started at all, so the case asserts that
the survivor makes *progress after* the crash. "The lanes run at the same
time" is satisfied by a serialised run of the same events, so the case
measures the two lanes' wall-clock windows and asserts on their observed
intersection-over-union -- the shape `JobQueueContract.overlapping()`
established.
"""

import asyncio
import dataclasses
import inspect
import io
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from tests.fakes.collection_repository import FakeCollectionRepository
from tests.fakes.credential_store import FakeCredentialStore
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.curated_row_repository import FakeCuratedRowRepository
from tests.fakes.embedding import FakeEmbedder
from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.event_publisher import FakeEventPublisher
from tests.fakes.image_repository import FakeImageRepository
from tests.fakes.import_run_repository import FakeImportRunRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.llm_call_repository import FakeLLMCallRepository
from tests.fakes.llm_client import FakeLLMClient
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.raw_payload_store import FakeRawPayloadStore
from tests.fakes.row_provider import FakeRow, FakeRowProvider
from tests.fakes.row_provider_settings_repository import FakeRowProviderSettingsRepository
from tests.fakes.search_index import (
    FakePrefixSuggestIndex,
    FakeSearchIndex,
    FakeSuggestIndex,
)
from tests.fakes.source_adapter import FakeSourceAdapter
from tests.fakes.source_repository import FakeSourceRepository
from tests.fakes.sync_run_repository import FakeSyncRunRepository
from tests.fakes.taste_repository import FakeTasteRepository
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_match_repository import FakeTitleMatchRepository
from tests.fakes.title_neighbor_repository import FakeTitleNeighborRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from tests.unit.rows import Library
from usher.api.lanes import LaneSupervisor
from usher.composition import Pipeline
from usher.config import Settings
from usher.domain.enums import EnrichmentState, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.jobs import Job, JobKind, JobPriority, JobStatus
from usher.domain.rows import BuiltRow, RowCard
from usher.domain.source import Source
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus
from usher.domain.watch import User
from usher.ports.credentials import SourceCredentials
from usher.ports.embedding import Embedder
from usher.ports.events import EventPublisher
from usher.ports.jobs import JobQueue, JobRequest
from usher.ports.llm import LLMClient
from usher.ports.repository import (
    MediaItemRepository,
    RowProviderSettingsRepository,
    SyncRunRepository,
    WatchStateRepository,
)
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.ports.source import (
    SourceAdapter,
    SourceAdapterFactory,
    SourceEvent,
    SourceEventKind,
    SourceItem,
    SourceItemKind,
)
from usher.services.curation_pool import CandidatePoolService
from usher.services.home import SCREEN_STALE_GRACE, HomeService
from usher.services.ingest import IngestService
from usher.services.jobs import DEFAULT_LEASE_SECONDS
from usher.services.matching import MatchService
from usher.services.reconcile import ReconcileService
from usher.services.rows import ROW_PROVIDERS
from usher.services.rows.cache import Freshness, RefreshQueue, RowCache
from usher.services.search import SearchService
from usher.services.similar import SimilarityService
from usher.services.taste import TasteService
from usher.services.watch_sync import WatchStateSyncService

_EMBEDDING_MODEL = "fake:test-embedding"

CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))
USER_ID = new_id()


def _source(name: str, *, enabled: bool = True) -> Source:
    return Source(
        kind=SourceKind.EMBY,
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        credentials_ref=f"ref-{name}",
        device_id=str(new_id()),
        enabled=enabled,
    )


def _item(external_id: str) -> SourceItem:
    return SourceItem(
        external_id=external_id,
        kind=SourceItemKind.MOVIE,
        name="Arrival",
        year=2016,
        provider_ids={"tmdb": "329865"},
    )


class _CrashingAdapter(FakeSourceAdapter):
    """A lane whose `events()` raises something that is not a
    `UsherPortError` -- i.e. a bug, which `PushSupervisor.run` deliberately
    does not catch."""

    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        raise ZeroDivisionError("a bug in the lane")


class _SlowAdapter(FakeSourceAdapter):
    """Records the wall-clock window its lane spent resolving a pushed id.

    Real time, in a public method the real applier calls, because a fake
    that never truly suspends is not a concurrency test: the loop would run
    each task through its whole cycle before starting the next and two
    correct lanes would show no overlap at all.
    """

    def __init__(self, source: Source) -> None:
        super().__init__(source)
        self.window: tuple[float, float] | None = None

    async def get_item(self, external_id: str) -> SourceItem | None:
        started = time.perf_counter()
        try:
            await asyncio.sleep(0.04)
            return await super().get_item(external_id)
        finally:
            self.window = (started, time.perf_counter())


class _Adapters(SourceAdapterFactory):
    """Hands out one `FakeSourceAdapter` per source and remembers them, so a
    case can push an event at a *running* lane's channel."""

    def __init__(self) -> None:
        self.built: dict[str, FakeSourceAdapter] = {}
        self.crashing: set[str] = set()
        self.slow: set[str] = set()
        # Two spellings of one precondition, kept because each side's cases
        # use its own: `_start_lane` builds the adapter and hands it straight
        # to `PushSupervisor.run`, whose first act after connecting is to
        # close the gap, so seeding off `built[...]` afterwards races the
        # very walk the case is trying to observe -- and a case that lost the
        # race would assert against a library of zero items, which is exactly
        # what a broken ceiling produces.
        #
        # `library` is the declarative form (a list of items to seed);
        # `prepare` is the general one (any set-up a case wants), and it runs
        # first so a case can use both.
        self.prepare: Callable[[FakeSourceAdapter], None] | None = None
        self.library: list[tuple[SourceItem, datetime]] = []

    def crash(self, name: str) -> None:
        self.crashing.add(name)

    def stock(self, item: SourceItem, changed_at: datetime) -> None:
        self.library.append((item, changed_at))

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        if source.name in self.crashing:
            kind: type[FakeSourceAdapter] = _CrashingAdapter
        elif source.name in self.slow:
            kind = _SlowAdapter
        else:
            kind = FakeSourceAdapter
        adapter = kind(source)
        if self.prepare is not None:
            self.prepare(adapter)
        for item, changed_at in self.library:
            adapter.seed(item, changed_at)
        self.built[source.name] = adapter
        return adapter


class _CountingQueue(FakeJobQueue):
    """A queue that remembers how often the worker lane asked it things.

    Shared across every unit of work, because the lane builds a fresh
    `Pipeline` per pass and a per-pass queue would reset the counters this
    file's "once, not per pass" case is entirely about.
    """

    def __init__(self) -> None:
        super().__init__()
        self.claims = 0
        self.requeues = 0
        self.requeue_ages: list[float] = []
        # What each pass *asked for*, which is the observable half of
        # `run_once` claiming `list(self._handlers)`: a lane with no model
        # must not ask for `index` work at all.
        self.claimed_kinds: list[tuple[JobKind, ...]] = []

    async def claim(self, kinds: Sequence[JobKind], *, limit: int = 1) -> list[Job]:
        self.claims += 1
        self.claimed_kinds.append(tuple(kinds))
        return await super().claim(kinds, limit=limit)

    async def requeue_running(self, *, older_than_seconds: float = 0.0) -> int:
        self.requeues += 1
        # Recorded, not just counted: the *age* is the whole difference between
        # recovery and theft, and a lane that called this bare would answer
        # "1 requeue" exactly as a correct one does.
        self.requeue_ages.append(older_than_seconds)
        return await super().requeue_running(older_than_seconds=older_than_seconds)


class _RecordingReconcile(ReconcileService):
    """A `ReconcileService` that says which walks it was asked for.

    A subclass that **delegates** rather than a spy that returns nothing:
    the gap-closer's guard is only observable as "this walk did not happen",
    and `walks == []` is equally what a `reconcile` that does nothing at all
    produces. The arm that is *not* refused has to walk for real, or the
    positive control is as hollow as the absence claim it protects.
    """

    def __init__(
        self,
        walks: list[tuple[str, SyncRunKind]],
        ceilings: list[int],
        *,
        ingest: IngestService,
        media_items: MediaItemRepository,
        runs: SyncRunRepository,
        events: EventPublisher,
        commit: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(
            ingest=ingest, media_items=media_items, runs=runs, events=events, commit=commit
        )
        self._walks = walks
        self._ceilings = ceilings

    async def reconcile(
        self,
        source: Source,
        kind: SyncRunKind,
        adapter: SourceAdapter,
        *,
        max_items: int = 0,
    ) -> SyncRun:
        self._walks.append((source.name, kind))
        # Recorded as well as counted, for the reason `_CountingQueue`
        # records `older_than_seconds`: the ceiling's correctness is in
        # *what the lane passed*, and "the gap closed" is what a lane
        # passing nothing produces too.
        self._ceilings.append(max_items)
        return await super().reconcile(source, kind, adapter, max_items=max_items)


class _RecordingWatchSync(WatchStateSyncService):
    """The other half of `_close_gap`, recorded on the same terms.

    Without it "the gap-closer *returned*" is unstated: a refusal spelled as
    an early exit from the item lane alone still walks `watch_state()`, which
    is the same unbounded upstream walk one method over.
    """

    def __init__(
        self,
        walks: list[str],
        *,
        media_items: MediaItemRepository,
        watch_states: WatchStateRepository,
        runs: SyncRunRepository,
        queue: JobQueue,
        commit: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(
            media_items=media_items,
            watch_states=watch_states,
            runs=runs,
            queue=queue,
            commit=commit,
        )
        self._walks = walks

    async def sync(self, source: Source, adapter: SourceAdapter, *, user_id: uuid.UUID) -> SyncRun:
        self._walks.append(source.name)
        return await super().sync(source, adapter, user_id=user_id)


@dataclass(slots=True)
class _Fakes:
    """One set of repositories, shared across every unit of work.

    A fresh `Pipeline` per call would model a fresh *session*, which is
    right; fresh repositories would model a fresh *database*, which is not,
    and would make everything a lane wrote invisible to the next assertion.
    """

    sources: FakeSourceRepository
    credentials: FakeCredentialStore
    media_items: FakeMediaItemRepository
    # Shared for the reason the docstring gives, and it is load-bearing for
    # the gap-closer's guard specifically: "has this source ever completed
    # an item-lane run" is a question about the *database*, so a `sync_runs`
    # table minted per unit of work answers "no" for every source forever.
    #
    # **What that costs is measured rather than reasoned about, and it is
    # not what an earlier version of this comment claimed.** Planted
    # (`runs = fakes.runs` back to `runs = FakeSyncRunRepository()`), the
    # gap-closer's case goes **red** -- it does not quietly pass while
    # testing nothing. What makes it impossible to miss is the case's own
    # third arm: `Cellar` has a `COMPLETED` run, so a per-unit-of-work
    # table refuses `Cellar` too, `watch_synced` never fills, and the
    # second `_drain` expires. So the shared table is load-bearing for the
    # *positive control*, and the positive control is what reports.
    #
    # Put another way: `sync_runs` is where a *cursor* lives, so a fresh one
    # per unit of work models a database that forgets every completed walk the
    # instant the session closes -- under which no delta ever has a `since`
    # and "the gap-closer is bounded once a walk has completed" is
    # unobservable.
    runs: FakeSyncRunRepository
    queue: _CountingQueue
    adapters: _Adapters
    events: FakeEventPublisher
    # Which walks the push lanes' gap-closer actually asked for. Two lists
    # rather than one, because `_close_gap` runs two lanes in a stated order
    # and a refusal has to stop both.
    reconciled: list[tuple[str, SyncRunKind]]
    # The `max_items` each of those walks was handed, in the same order.
    # Separate from `reconciled` rather than a third tuple slot, so every
    # assertion written before the ceiling existed still reads.
    gap_ceilings: list[int]
    watch_synced: list[str]
    commits: list[float]
    # When each unit of work was opened. The `rows.refresh` lane's whole
    # premise is that it runs on a session it opened itself rather than on a
    # request's, and a count of opens is what a fake can honestly carry --
    # `tests/integration/test_rows_refresh.py` is where the *session* claim is
    # made against real ones.
    units_of_work: list[float]


def _pipeline(
    fakes: _Fakes,
    settings: Settings,
    providers: Sequence[RowProvider] = ROW_PROVIDERS,
    provider_settings: RowProviderSettingsRepository | None = None,
) -> Pipeline:
    titles = FakeTitleRepository()
    matching = FakeTitleMatchRepository(titles)
    embeddings = FakeTitleEmbeddingRepository()
    neighbors = FakeTitleNeighborRepository()
    queue = fakes.queue
    episodes = FakeEpisodeRepository()
    watch_states = FakeWatchStateRepository()
    runs = fakes.runs
    matcher = MatchService(titles=titles, matching=matching, queue=queue)
    ingest = IngestService(
        matcher=matcher,
        matching=matching,
        media_items=fakes.media_items,
        episodes=episodes,
        queue=queue,
    )

    async def commit() -> None:
        fakes.commits.append(time.perf_counter())

    # Real fakes rather than `None`: `build_worker` constructs
    # `DeriveService` eagerly whenever a provider is present, so an unused
    # slot here would fail at construction instead of at the lane behaviour
    # each of these cases is about.
    people = FakePersonRepository()
    # One instance, three consumers, because that is what the composition root
    # produces: `build_pipeline` and `build_search_service` each construct a
    # `PostgresTasteRepository` over the **same session**, so they read one
    # table. Two independent fakes here would let a case store a centroid
    # through one and search through the other, which is a state no deployment
    # has.
    taste_rows = FakeTasteRepository(watch_states)
    taste = TasteService(
        watch_states=watch_states,
        embeddings=embeddings,
        titles=titles,
        taste=taste_rows,
        embedder=None,
        now=lambda: datetime.now(UTC),
    )
    return Pipeline(
        sources=fakes.sources,
        credentials=fakes.credentials,
        titles=titles,
        matching=matching,
        media_items=fakes.media_items,
        episodes=episodes,
        watch_states=watch_states,
        payloads=FakeRawPayloadStore(),
        bulk=FakeBulkCatalogRepository(),
        import_runs=FakeImportRunRepository(),
        runs=runs,
        queue=queue,
        embeddings=embeddings,
        neighbors=neighbors,
        taste_rows=taste_rows,
        people=people,
        credits=FakeCreditRepository(people, titles),
        collections=FakeCollectionRepository(),
        # A real fake for the same reason `people` above is one: the worker
        # lane's `DeriveService` is constructed eagerly and takes this slot.
        images=FakeImageRepository(),
        adapters=fakes.adapters,
        matcher=matcher,
        ingest=ingest,
        reconcile=_RecordingReconcile(
            fakes.reconciled,
            fakes.gap_ceilings,
            ingest=ingest,
            media_items=fakes.media_items,
            runs=runs,
            events=fakes.events,
            commit=commit,
        ),
        watch=_RecordingWatchSync(
            fakes.watch_synced,
            media_items=fakes.media_items,
            watch_states=watch_states,
            runs=runs,
            queue=queue,
            commit=commit,
        ),
        # Over the port doubles rather than `cast(Any, None)`: no lane reads
        # it, but a field left unset here is one a lane could start reading
        # without this file noticing.
        search=SearchService(
            FakeSearchIndex(),
            FakePrefixSuggestIndex(),
            FakeSuggestIndex(),
            titles,
            fakes.media_items,
            watch_states,
            taste_rows,
            embeddings,
            result_limit=settings.search_result_limit,
        ),
        similar=SimilarityService(
            embeddings, neighbors, titles, commit, embedding_model=_EMBEDDING_MODEL
        ),
        # The real registry unless a case says otherwise. The `rows.refresh`
        # lane composes a whole screen, so its cases substitute a fake
        # provider they can gate and count -- running ten real providers
        # against these fakes would make the case about the providers.
        row_providers=tuple(providers),
        # M9's overrides table. Absent, the fake answers `{}` -- which is the
        # shipped state of the real table and therefore "every provider
        # composes", so every case written before the toggle existed keeps
        # meaning what it meant.
        row_provider_settings=provider_settings or FakeRowProviderSettingsRepository(),
        # Over the port double rather than `cast(Any, None)`, and no longer
        # only on the terms `search` above states: the worker lane *writes*
        # this one whenever it holds an `LLMClient`, because
        # `build_curation_service` takes `rows=pipeline.curated_rows` and a
        # generation replaces the household's shelves through it. Rendering
        # is still only `GET /home`'s.
        curated_rows=FakeCuratedRowRepository(),
        # Over the port double for the same reason, one table over. The
        # worker lane *does* reach this one whenever it holds an
        # `LLMClient` -- `build_curation_service` writes the cost ledger
        # through it -- so an unset field here is an `AttributeError` on
        # the first generation rather than a compile error.
        llm_calls=FakeLLMCallRepository(),
        taste=taste,
        # Read by the worker lane on the same terms as the two above:
        # `build_curation_service` takes `pool=pipeline.pool`, and the pool
        # is the first thing a generation asks for. Still constructed here
        # for the reason `taste` is, which has not changed: the dataclass has
        # no defaults, deliberately, so a field added later is a compile
        # error at every construction site rather than a `None` that
        # surfaces as an `AttributeError` on the one path that reads it.
        pool=CandidatePoolService(titles=titles, embeddings=embeddings, taste=taste, size=8),
        events=fakes.events,
        commit=commit,
    )


def _settings(**overrides: object) -> Settings:
    """The one spelling of this file's `Settings`.

    Extracted from `_supervisor` so a case that wants a `Pipeline` and no
    supervisor can have one without reaching into `LaneSupervisor._work`,
    and without a second literal drifting from this one.
    """
    return Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher",
        secret_key="0" * 32,
        **overrides,  # type: ignore[arg-type]
    )


def _supervisor(
    fakes: _Fakes,
    *,
    worker_idle_seconds: float = 5.0,
    embedder: Embedder | None = None,
    client: LLMClient | None = None,
    rows: RowCache | None = None,
    refreshes: RefreshQueue | None = None,
    providers: Sequence[RowProvider] = ROW_PROVIDERS,
    provider_settings: RowProviderSettingsRepository | None = None,
    **overrides: object,
) -> LaneSupervisor:
    settings = _settings(**overrides)

    @asynccontextmanager
    async def work() -> AsyncIterator[Pipeline]:
        # A real `await` on the way in, which is what the production shape
        # has (`async with sessions()`), so `_settle`'s ten turns are
        # exercising something rather than being decorative.
        await asyncio.sleep(0)
        fakes.units_of_work.append(time.perf_counter())
        yield _pipeline(fakes, settings, providers, provider_settings)

    return LaneSupervisor(
        settings,
        work,
        fakes.events,
        user_id=_user_id,
        embedder=embedder,
        client=client,
        rows=rows,
        refreshes=refreshes,
        idle_seconds=worker_idle_seconds,
    )


async def _user_id() -> uuid.UUID:
    return USER_ID


@pytest.fixture
def fakes() -> _Fakes:
    return _Fakes(
        sources=FakeSourceRepository(),
        credentials=FakeCredentialStore(),
        media_items=FakeMediaItemRepository(),
        runs=FakeSyncRunRepository(),
        queue=_CountingQueue(),
        adapters=_Adapters(),
        events=FakeEventPublisher(),
        reconciled=[],
        gap_ceilings=[],
        watch_synced=[],
        commits=[],
        units_of_work=[],
    )


async def _seed(fakes: _Fakes, source: Source) -> None:
    await fakes.sources.add(source)
    await fakes.credentials.put(source.credentials_ref, CREDENTIALS, owner_id=source.id)


async def _settle() -> None:
    """Let every freshly created task reach its first suspension point.

    Ten turns rather than one: a lane's first act is to open a unit of work,
    then read the source list, then connect -- and a single `sleep(0)`
    yields once, which is enough for a bare mock and not enough for anything
    that actually awaits. Asserting after one turn is how a lane test passes
    against a supervisor that starts nothing.
    """
    for _ in range(10):
        await asyncio.sleep(0)


async def _item_run(fakes: _Fakes, source: Source, status: SyncRunStatus) -> None:
    """One finished `FULL` run for `source`, in whatever state.

    `FULL` rather than `DELTA` because it is the run an operator's
    `usher sync` leaves behind, which is the state the refusal below is
    telling them to reach.
    """
    await fakes.runs.add(
        SyncRun(
            source_id=source.id,
            kind=SyncRunKind.FULL,
            status=status,
            finished_at=datetime.now(UTC),
        )
    )


def _refusals(lines: list[str]) -> list[str]:
    """Every gap-close refusal a `WARNING` sink has captured.

    Filtered on the subject and never on the level, because the sink is
    already the level filter: a refusal downgraded to `DEBUG` -- invisible
    at any shipped `USHER_LOG_LEVEL` -- never reaches this list at all.
    The `WARNING|` prefix each line carries is the other direction, an
    upgrade to `ERROR`.

    **Where that absence is actually reported is the drain, not a count
    assertion**, and an earlier version of this docstring said otherwise.
    Measured by planting the downgrade: the refusal list stays empty, so
    every `_drain` waiting on it expires and the case dies on the
    deadline -- before any assertion below it runs. That is why `_drain`
    takes a `note`: the deadline is the reporting site, so the counts have
    to be in its message.
    """
    return [line for line in lines if "gap" in line]


# -- the push lanes -----------------------------------------------------


async def test_a_lane_is_started_for_each_enabled_source(fakes: _Fakes) -> None:
    await _seed(fakes, _source("A"))
    await _seed(fakes, _source("B"))
    await _seed(fakes, _source("C", enabled=False))
    supervisor = _supervisor(fakes)
    await supervisor.start()
    await _settle()
    try:
        assert supervisor.running_sources() == ["A", "B"]
    finally:
        await supervisor.stop()


async def test_a_source_added_later_gets_a_lane_without_a_restart(fakes: _Fakes) -> None:
    """PRD 08: "Sources live in the database because they are added through
    the admin API. A deployment that needs a compose edit and a restart to
    connect a media server is the wrong shape for this." A lane set fixed at
    startup makes that false for push alone."""
    supervisor = _supervisor(fakes)
    await supervisor.start()
    await _settle()
    try:
        assert supervisor.running_sources() == []
        await _seed(fakes, _source("A"))
        await supervisor.refresh()
        await _settle()
        assert supervisor.running_sources() == ["A"]
    finally:
        await supervisor.stop()


async def test_the_refresher_picks_a_source_up_on_its_own_interval(fakes: _Fakes) -> None:
    """The case above calls `refresh()` by hand, so it passes against a
    supervisor with **no refresh loop at all** -- and a lane set fixed at
    startup is exactly what PRD 08 says a source must not need a restart
    for. This one seeds after `start()` and never calls `refresh()`.

    A real interval rather than zero: `push_source_refresh_seconds` is
    `gt=0`, and a loop that slept for nothing would spin.
    """
    supervisor = _supervisor(fakes, push_source_refresh_seconds=0.01)
    await supervisor.start()
    await _settle()
    try:
        assert supervisor.running_sources() == []
        await _seed(fakes, _source("A"))
        await _drain(lambda: supervisor.running_sources() == ["A"], bound=2.0)
    finally:
        await supervisor.stop()


async def test_a_disabled_source_has_its_lane_cancelled(fakes: _Fakes) -> None:
    """`enabled` is how an operator parks a server that is being rebuilt,
    and a lane that kept reconnecting to it would keep the backoff schedule
    warm against a machine nobody wants touched."""
    source = _source("A")
    await _seed(fakes, source)
    supervisor = _supervisor(fakes)
    await supervisor.start()
    await _settle()
    try:
        assert supervisor.running_sources() == ["A"]
        await fakes.sources.update(source.evolve(enabled=False))
        await supervisor.refresh()
        await _settle()
        assert supervisor.running_sources() == []
        assert fakes.adapters.built["A"]._closed is True
    finally:
        await supervisor.stop()


async def test_stopping_cancels_every_lane_and_closes_every_adapter(fakes: _Fakes) -> None:
    await _seed(fakes, _source("A"))
    supervisor = _supervisor(fakes)
    await supervisor.start()
    await _settle()
    await supervisor.stop()
    assert supervisor.running_sources() == []
    assert supervisor.worker_running() is False
    assert all(adapter._closed for adapter in fakes.adapters.built.values())


async def test_a_source_with_no_credentials_is_skipped_and_the_others_still_run(
    fakes: _Fakes,
) -> None:
    """The same reasoning `usher sync` applies one layer over: an operator
    with two sources needs the second to run when the first's credential row
    has gone missing."""
    broken = _source("A")
    await fakes.sources.add(broken)  # no credential row
    await _seed(fakes, _source("B"))
    supervisor = _supervisor(fakes)
    await supervisor.start()
    await _settle()
    try:
        assert supervisor.running_sources() == ["B"]
    finally:
        await supervisor.stop()


async def test_push_availability_for_a_source_with_no_lane_is_not_probed(
    fakes: _Fakes,
) -> None:
    """`None` is an absence and `False` is a claim, and a supervisor with no
    lane for a source has only the first to offer.

    `GET /admin/sources/{id}/status` renders this straight through, so a
    `False` here turns "nobody has looked" into "push is broken" on every
    admin screen for every source whose lane has not started -- which is
    every source in a `USHER_PUSH_ENABLED=false` deployment.
    """
    supervisor = _supervisor(fakes)
    assert supervisor.push_available(new_id()) is None
    await _seed(fakes, _source("A"))
    await supervisor.start()
    await _settle()
    try:
        source = (await fakes.sources.list_all())[0]
        assert supervisor.push_available(source.id) is False
        assert supervisor.push_available(new_id()) is None
    finally:
        await supervisor.stop()


# -- the gap-closing delta ----------------------------------------------


async def test_a_source_with_no_completed_run_is_not_gap_closed_and_the_operator_is_told(
    fakes: _Fakes,
) -> None:
    """A delta with no cursor is not a delta, it is a full walk wearing a
    delta's name -- and the lane is the one caller nobody typed a command
    for.

    `_start_lane` runs for every enabled source before the refresher's first
    sleep and `PushSupervisor.run` closes the gap immediately after every
    successful connection, so **the first thing a freshly started
    `uvicorn usher.api.app:create_app --factory` does against every enabled
    source is close a gap**. With no completed item-lane run there is no
    cursor, `reconcile` walks `list_items(since=None)`, and that is the whole
    library -- 1,134,919 items over 5,675 pages on the one household this
    project measures, i.e. 7.3-11.8 hours (M10 S1, 2026-08-15;
    `.claude/rules/emby-push-and-ingest.md`). `push_gap_min_interval_seconds`
    cannot help: it bounds *cadence*, and `_Gate.at` is `None` until a gap
    has run, so the first one is never skipped
    (`test_the_first_gap_after_an_outage_is_never_skipped`).

    **Three arms, because the middle one is what makes this a test of the
    cursor rather than of emptiness.** A source with no runs at all is
    refused; a source with a `FAILED` run and no completed one is refused
    identically -- that is the state a killed probe leaves behind, and
    `latest_completed_cursor` is `status = 'completed'` for exactly the
    reason a delta must not resume from a walk that stopped halfway; a
    source with one `COMPLETED` run is gap-closed.

    **The positive control is the third arm and it is the first assertion**:
    a refusal that refuses everything is not a guard, it is an off switch,
    and every other assertion here passes against one.
    """
    atrium, belfry, cellar = _source("Atrium"), _source("Belfry"), _source("Cellar")
    for source in (atrium, belfry, cellar):
        await _seed(fakes, source)
    await _item_run(fakes, belfry, SyncRunStatus.FAILED)
    await _item_run(fakes, cellar, SyncRunStatus.COMPLETED)
    supervisor = _supervisor(fakes)
    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{level.name}|{message}")
    try:
        await supervisor.start()
        # Every lane has *decided*: it either walked or refused. At HEAD all
        # three decide at once, so this returns immediately and the case
        # fails on its own assertions rather than on the drain's deadline.
        #
        # **When it does fail here, the deadline is the reporting site**, so
        # the counts go in its message: a WARNING downgraded to DEBUG never
        # reaches `_refusals`, this condition is never met, and the count
        # assertion below is never evaluated. Measured, not predicted.
        await _drain(
            lambda: len(fakes.reconciled) + len(_refusals(lines)) >= 3,
            note=lambda: (
                f"only {len(fakes.reconciled)} walked and {len(_refusals(lines))} refused "
                f"of 3 sources: reconciled={fakes.reconciled} refusals={_refusals(lines)}"
            ),
        )
        # ...and then the one arm that is gap-closed finishes its pair, so
        # "the refusal returns" is asserted against a watch lane that is
        # observably reachable.
        await _drain(
            lambda: bool(fakes.watch_synced),
            note=lambda: (
                "no source reached the watch lane, so the arm that is *not* refused never "
                f"finished its pair: reconciled={fakes.reconciled}"
            ),
        )
    finally:
        logger.remove(sink)
        await supervisor.stop()

    assert fakes.reconciled == [("Cellar", SyncRunKind.DELTA)], (
        f"a refusal that refuses everything is not a guard, it is an off switch: {fakes.reconciled}"
    )
    assert fakes.watch_synced == ["Cellar"], (
        "the refusal returns from the gap-closer; it does not fall through to the "
        f"watch lane, which walks the same source: {fakes.watch_synced}"
    )
    refusals = _refusals(lines)
    assert len(refusals) == 2, f"one refusal per refused source, at WARNING: {lines}"
    named = {
        name for name in ("Atrium", "Belfry", "Cellar") if any(name in line for line in refusals)
    }
    assert named == {"Atrium", "Belfry"}, f"the refusal names the source it refused: {refusals}"
    for line in refusals:
        assert line.startswith("WARNING|"), f"the refusal is a WARNING, not an ERROR: {line}"
        # The command and nothing more specific. This branch's own S5 spelled
        # the remedy `usher sync --kind full`; the implementation that shipped
        # is `main`'s (issue #9), whose line names `usher sync --source "..."`
        # and the `USHER_PUSH_GAP_CLOSE=always` escape beside it. The claim
        # worth pinning is neither spelling -- it is that **a refusal that does
        # not say what to run is a dead end**, so the assertion is on the
        # command an operator types.
        assert "usher sync" in line, (
            f"a refusal that does not say what to run is a dead end: {line}"
        )
        assert "USHER_PUSH_GAP_CLOSE" in line, (
            f"the refusal names the setting that lifts it, or it reads as a wall: {line}"
        )
        # PRD 08's credentials rule, and `reconcile.py`'s own failure line is
        # the local precedent: the *name* is what an operator typed, and it
        # is the positive control that makes the three absences below claims
        # about redaction rather than about an empty string.
        #
        # **Each of the three has been shown to fire, and the obvious plant
        # shows none of them.** Swapping `source.name` for `source.base_url`
        # dies two assertions earlier, on `named == {"Atrium", "Belfry"}` --
        # `base_url` is lower-cased (`https://atrium.invalid`), so the name
        # is simply absent and this loop is never entered. The plants that
        # reach here keep the line count at 2 *and* the name present, and
        # add one field each: `{source} ({url})` dies on the first line
        # below, `{source} ({password})` on the second, `{source}
        # ({credentials_ref})` on the third. All three KILLED, each on its
        # own assertion -- which is what makes the password line a claim
        # rather than a decoration, since `Source` does not carry a password
        # and no mutation of the *shipped* call site can put one there.
        # `.claude/rules/mutation-sweeps.md` has the round.
        assert atrium.base_url not in line and belfry.base_url not in line
        assert CREDENTIALS.password.get_secret_value() not in line
        assert atrium.credentials_ref not in line and belfry.credentials_ref not in line


async def test_the_gap_closers_delta_carries_the_ceiling_and_the_watch_lane_still_runs_after_it(
    fakes: _Fakes,
) -> None:
    """M10 S6, and the deliberate half of it is the second assertion.

    **The ceiling is the item lane's and only the item lane's.** The watch
    lane derives its own cursor under its own upstream filter
    (`MinDateLastSavedForUser`, 29,005 items against the item lane's 28,934
    over the same 30-day window) and `WatchStateSyncService.sync` takes no
    ceiling at all -- so a gap close whose item walk stopped at
    `USHER_PUSH_GAP_MAX_ITEMS` still runs the watch lane, whole. That is a
    cost, not a free choice: the *startup* a ceiling bounds is bounded on
    the item lane alone, and PRD 03's Reconnect-delta row says so.

    Both halves are needed and neither implies the other. Without the
    first, the lane closes an unbounded gap while looking configured; the
    obvious careful spelling of the second's defect is
    `if run.status is FAILED: return` after the item walk, which passes
    every assertion this file had before this case and quietly stops the
    watch lane on every bounded gap close.
    """
    source = _source("Atrium")
    await _seed(fakes, source)
    await _item_run(fakes, source, SyncRunStatus.COMPLETED)

    def stock(adapter: FakeSourceAdapter) -> None:
        """More items than the ceiling, on the lane's own adapter, so the
        walk genuinely truncates rather than merely being handed a number."""
        for index in range(10):
            adapter.seed(_item(f"a-{index}"), datetime.now(UTC))

    fakes.adapters.prepare = stock
    supervisor = _supervisor(fakes, push_gap_max_items=3)
    try:
        await supervisor.start()
        await _drain(
            lambda: bool(fakes.watch_synced),
            note=lambda: (
                "the item lane's ceiling stopped the watch lane too: "
                f"reconciled={fakes.reconciled} watch_synced={fakes.watch_synced}"
            ),
        )
    finally:
        await supervisor.stop()

    deltas = [
        run
        for run in await fakes.runs.list_for_source(source.id, limit=20)
        if run.kind is SyncRunKind.DELTA
    ]
    assert [run.status for run in deltas] == [SyncRunStatus.FAILED], (
        "the premise: the item walk really did stop at the ceiling, so the watch-lane "
        f"assertion below is about a gap close that truncated: {deltas}"
    )
    assert [run.items_seen for run in deltas] == [3]
    assert fakes.reconciled == [("Atrium", SyncRunKind.DELTA)]
    assert fakes.gap_ceilings == [3], (
        "the gap-closer hands the item lane `USHER_PUSH_GAP_MAX_ITEMS`; a lane passing "
        f"nothing closes an unbounded gap while looking configured: {fakes.gap_ceilings}"
    )
    assert fakes.watch_synced == ["Atrium"], (
        "the ceiling is the item lane's alone -- the watch lane owns a different cursor "
        f"under a different upstream filter and is not bounded by it: {fakes.watch_synced}"
    )
    assert "max_items" not in inspect.signature(WatchStateSyncService.sync).parameters, (
        "and it cannot be, structurally: adding a ceiling to the watch lane is a port-"
        "adjacent signature change, not a quiet argument"
    )


async def test_an_operators_delta_on_a_fresh_source_still_walks(fakes: _Fakes) -> None:
    """The other side of the refusal above, and the reason it lives in
    `LaneSupervisor` rather than in `ReconcileService`.

    `usher sync --kind delta` against a source that has never completed a run
    is an operator asking for a walk of everything, and it must keep working
    -- `cli.py`'s `_sync` calls `pipeline.reconcile.reconcile(source,
    SyncRunKind(kind), adapter)` directly, which is the very object the lane
    refuses *through*. So this drives that same service, on the same fakes,
    and asserts the walk happened: a refusal pushed one layer down would
    break the command with nothing here to notice.
    """
    source = _source("Atrium")
    await _seed(fakes, source)
    adapter = FakeSourceAdapter(source)
    adapter.seed(_item("a-1"), datetime.now(UTC))
    # `_pipeline` directly rather than `LaneSupervisor._work()`: the
    # supervisor is the object this case is deliberately *not* testing, and
    # reaching into its private for a `Pipeline` that `_pipeline` hands out
    # in one call couples the case to a name it has no claim on.
    pipeline = _pipeline(fakes, _settings())
    assert await pipeline.reconcile.cursor_for(source, SyncRunKind.DELTA) is None, (
        "the premise: this source has no cursor, which is what the lane refuses"
    )
    run = await pipeline.reconcile.reconcile(source, SyncRunKind.DELTA, adapter)
    assert run.status is SyncRunStatus.COMPLETED
    assert run.cursor_at is None, "a delta with no completed run walks from no cursor"
    assert run.items_seen == 1, "the operator's delta walked nothing"


async def test_a_deferred_push_event_on_a_cursorless_source_is_refused_and_its_items_are_dropped(
    fakes: _Fakes,
) -> None:
    """`_close_gap` has a **second** caller, and it is on the delivery path.

    `PushSupervisor.run` closes the gap on reconnect *and* whenever an
    applied event comes back `deferred_to_delta` -- an event naming more
    than `push_max_items_per_event` items with no payload
    (`services/push.py`), which is deferred precisely because a request per
    item against a 1.13M-item library is worse than a paged walk. Against a
    cursorless source that walk is now refused, so those items are applied
    **neither inline nor by a walk**: the event is discarded until an
    operator runs `usher sync --kind full`.

    **That is a deliberate trade and it was undocumented and untested until
    this case.** The alternative is the whole-library walk the refusal
    exists to prevent, triggered by an event rather than by a reconnect, so
    the refusal is the right answer -- but "the items are dropped" is a
    behaviour a reader must be able to find, and an absence nothing asserts
    is an absence nobody chose.

    **Two arms, and the second is the positive control.** `Belfry` has a
    `COMPLETED` run, so its deferred event reaches a gap-closer that walks,
    and its two items arrive in the catalog *by the walk* -- which is what
    makes "the deferral really does call `_gap`" a measured fact rather
    than an assumption. Without it, `Atrium`'s empty catalog is equally
    what a supervisor that ignored `deferred_to_delta` entirely produces.

    `push_gap_min_interval_seconds=0.0` because the gate is a **cadence**
    guard and would otherwise skip the second gap on both lanes: `_gap`
    stamps `gate.at` before it delegates, so the deferral's gap is inside
    the default 60 s window opened by the reconnect's.
    """
    atrium, belfry = _source("Atrium"), _source("Belfry")
    for source in (atrium, belfry):
        await _seed(fakes, source)
    await _item_run(fakes, belfry, SyncRunStatus.COMPLETED)
    supervisor = _supervisor(fakes, push_max_items_per_event=1, push_gap_min_interval_seconds=0.0)
    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{level.name}|{message}")
    try:
        await supervisor.start()
        # The reconnect gap settles first, so the deferral's gap below is
        # unambiguously the *second* call on each lane.
        await _drain(
            lambda: len(_refusals(lines)) >= 1 and len(fakes.reconciled) >= 1,
            note=lambda: (
                "neither lane closed its reconnect gap: "
                f"reconciled={fakes.reconciled} refusals={_refusals(lines)}"
            ),
        )
        # Seeded *after* the reconnect walk, and later than the cursor it
        # left behind, so anything that reaches the catalog got there
        # through the second walk rather than the first.
        changed = datetime.now(UTC) + timedelta(hours=1)
        for source in (atrium, belfry):
            adapter = fakes.adapters.built[source.name]
            for external_id in (f"{source.name}-1", f"{source.name}-2"):
                adapter.seed(_item(external_id), changed)
            # Two ids against `push_max_items_per_event=1`, and no payload:
            # `_apply_items` defers rather than resolving them one at a time.
            adapter.push(
                SourceEvent(
                    kind=SourceEventKind.ITEM_UPDATED,
                    external_ids=(f"{source.name}-1", f"{source.name}-2"),
                )
            )
        await _drain(
            lambda: len(_refusals(lines)) >= 2 and len(fakes.reconciled) >= 2,
            note=lambda: (
                "the deferred event never reached a second gap close: "
                f"reconciled={fakes.reconciled} refusals={_refusals(lines)} "
                f"stored={_stored(fakes.media_items)}"
            ),
        )
    finally:
        logger.remove(sink)
        await supervisor.stop()

    assert fakes.reconciled == [("Belfry", SyncRunKind.DELTA)] * 2, (
        "the positive control: a deferred event on a source that *has* a cursor closes a "
        f"second gap, so the deferral path really does reach `_close_gap`: {fakes.reconciled}"
    )
    assert _stored(fakes.media_items) == ["Belfry-1", "Belfry-2"], (
        "the deferred event's items are dropped on a cursorless source -- not applied inline "
        f"and not walked: {_stored(fakes.media_items)}"
    )
    refusals = _refusals(lines)
    assert len(refusals) == 2, (
        f"one refusal for the reconnect gap and one for the deferred event's: {lines}"
    )
    assert all("Atrium" in line for line in refusals), (
        f"both refusals are the cursorless source's: {refusals}"
    )


# -- crash isolation ----------------------------------------------------


async def test_a_lane_that_crashes_does_not_take_the_others_down(fakes: _Fakes) -> None:
    """A `PushSupervisor.run` that raised something that is not a
    `UsherPortError` -- a bug -- must cost its own source. Two lanes sharing
    one `TaskGroup` would take the whole set down, and the server with them
    if the group is awaited in the lifespan.

    **`running_sources() == ["B"]` alone would not test this.** A supervisor
    whose second lane was created and never scheduled reports exactly that,
    and so does one whose lanes are all cancelled a turn later. So the
    assertion is that B makes *progress after* A's crash: an item pushed
    once A's task is already `done()` is ingested through B's own lane.
    """
    await _seed(fakes, _source("A"))
    await _seed(fakes, _source("B"))
    fakes.adapters.crash("A")
    supervisor = _supervisor(fakes)
    await supervisor.start()
    await _settle()
    try:
        assert supervisor.running_sources() == ["B"]
        assert supervisor.crashed_sources() == ["A"]

        survivor = fakes.adapters.built["B"]
        survivor.seed(_item("b-1"), datetime.now(UTC))
        survivor.push(SourceEvent(kind=SourceEventKind.ITEM_UPDATED, external_ids=("b-1",)))
        await _drain(lambda: _stored(fakes.media_items) == ["b-1"])
    finally:
        # And shutdown itself survives the crashed lane. Measured which half
        # carries that: `stop()`'s `return_exceptions=True` alone fails 11
        # cases when removed (every lane is cancelled at teardown, and a
        # cancelled task raises `CancelledError` into `gather`), while
        # `_guard`'s catch survives its own deletion -- the isolation comes
        # from one task per lane, not from the `except`. What `_guard` buys
        # is the log line, which is what the case below pins.
        await supervisor.stop()


async def test_two_lanes_run_at_the_same_time_rather_than_one_after_the_other(
    fakes: _Fakes,
) -> None:
    """The property crash isolation rests on, measured rather than assumed.

    A count -- "both items were ingested" -- is exactly what a supervisor
    that ran A's lane to completion and *then* B's would also produce. This
    records the wall-clock window each lane spent inside its own adapter's
    `get_item`, which is where `PushApplyService` resolves a pushed id, and
    asserts the two intersect: the shape `JobQueueContract.overlapping()`
    established (measured there at 76.2% of the union, and at 62.6% and
    99.3-99.6% by M5's own groups D and E).

    The window is 40 ms of **real** time in a public method on the real code
    path, not a patched private and not `sleep(0)`: a fake that never truly
    suspends lets the loop run each task through its whole cycle before
    starting the next, so two correct lanes would score 0.0 and the case
    would be measuring the fake rather than the supervisor. A serialised
    supervisor scores 0.0 and cannot round up to the floor below.
    """
    await _seed(fakes, _source("A"))
    await _seed(fakes, _source("B"))
    fakes.adapters.slow.update({"A", "B"})
    supervisor = _supervisor(fakes)
    await supervisor.start()
    await _settle()
    try:
        for name in ("A", "B"):
            adapter = fakes.adapters.built[name]
            adapter.seed(_item(f"{name}-1"), datetime.now(UTC))
            adapter.push(
                SourceEvent(kind=SourceEventKind.ITEM_UPDATED, external_ids=(f"{name}-1",))
            )
        await _drain(lambda: _stored(fakes.media_items) == ["A-1", "B-1"])
    finally:
        await supervisor.stop()
    windows = {}
    for name in ("A", "B"):
        adapter = fakes.adapters.built[name]
        assert isinstance(adapter, _SlowAdapter)
        assert adapter.window is not None, f"{name}'s lane never resolved its pushed id"
        windows[name] = adapter.window
    overlap = _intersection_over_union(windows["A"], windows["B"])
    assert overlap > 0.5, f"the two lanes barely overlapped: {overlap:.1%} of their union"


def _intersection_over_union(a: tuple[float, float], b: tuple[float, float]) -> float:
    overlap = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return 0.0 if union <= 0 else overlap / union


async def test_a_crashed_lane_says_so(fakes: _Fakes) -> None:
    """`_guard`'s `except` is not what isolates the other lanes -- one task
    per lane is, measured -- so what it must actually deliver is that the
    crash is *not silent*.

    Without it a crashed lane leaves an unretrieved task exception, which
    CPython reports at garbage-collection time, to stderr, with no source
    name in it. An operator whose second Emby stopped updating would have
    nothing to search for. This is a log assertion on purpose: the log line
    is the deliverable.
    """
    await _seed(fakes, _source("A"))
    fakes.adapters.crash("A")
    supervisor = _supervisor(fakes)
    lines: list[str] = []
    sink = logger.add(lines.append, level="TRACE", serialize=True)
    try:
        await supervisor.start()
        await _drain(lambda: supervisor.crashed_sources() == ["A"])
    finally:
        logger.remove(sink)
        await supervisor.stop()
    crash = [line for line in lines if "crashed" in line]
    assert crash, lines
    assert "A" in crash[0]
    assert "ZeroDivisionError" in crash[0]


async def test_a_lane_that_reached_the_failure_ceiling_releases_its_adapter_and_is_named_as_stopped(
    fakes: _Fakes,
) -> None:
    """The leak M10's S10 closed, and its positive control in the same case.

    A lane whose task finished -- the failure ceiling, or a crash -- used to
    keep its `SourceAdapter` in `_open_adapters` for the process lifetime.
    That adapter is an `EmbyAdapter` holding a live `httpx.AsyncClient`
    **against a server this deployment does not own**, and it went on feeding
    `push_snapshots()`, which is the series PRD 10's "Push down" alert reads.
    A dead lane reporting `delivering=False` forever and a dead lane reporting
    nothing are different alerts.

    Three things are asserted and the fourth is the control:

    * the adapter is closed **exactly once**, not once per refresh tick -- a
      count, because `aclose` is idempotent and a flag cannot tell the two
      apart;
    * it is gone from the push-gauge snapshot, so the series stops;
    * the lane is **not restarted**, which is PRD 08's own remedy ("lean on
      the nightly walk") -- a refresh that replaced it would reconnect forever
      against the buffering proxy the ceiling exists for;
    * and source B, whose lane is **live**, keeps its adapter and its series
      through the same `refresh()`. Without that arm a `refresh` that closed
      every adapter would pass the first three assertions and break push
      entirely, which is the loudest regression this file can ship.
    """
    await _seed(fakes, _source("A"))
    await _seed(fakes, _source("B"))
    fakes.adapters.crash("A")
    supervisor = _supervisor(fakes)
    await supervisor.start()
    try:
        # The premise, asserted before anything else: a lane that never
        # finished cannot pass this case.
        await _drain(
            lambda: supervisor.crashed_sources() == ["A"],
            note=lambda: (
                f"crashed={supervisor.crashed_sources()} running={supervisor.running_sources()}"
            ),
        )
        by_name = {source.name: source for source in await fakes.sources.list_all()}
        stopped = fakes.adapters.built["A"]
        live = fakes.adapters.built["B"]
        assert stopped._closes == 0, "the leak: nothing has released it yet"
        assert set(supervisor.push_snapshots()) == {"A", "B"}

        await supervisor.refresh()

        assert stopped._closes == 1
        assert set(supervisor.push_snapshots()) == {"B"}, "the dead lane stops publishing"
        assert supervisor.push_available(by_name["A"].id) is None, "not probed, not broken"
        assert supervisor.crashed_sources() == ["A"], "F2 still has a state to report"
        assert supervisor.running_sources() == ["B"], "and the lane is not restarted"

        # The control: the live lane is untouched by the same call.
        assert live._closes == 0
        assert supervisor.push_available(by_name["B"].id) is not None

        # And a second tick does not close it again -- the `pop` is what makes
        # the release at-most-once, and a timer-driven refresh is what would
        # otherwise call `aclose` forever.
        await supervisor.refresh()
        assert stopped._closes == 1
    finally:
        await supervisor.stop()


async def test_push_snapshots_report_the_adapters_own_ledger(fakes: _Fakes) -> None:
    """PRD 10's `usher.source.push.reconnects` is fed straight from here, so
    a hard-coded `0` would plot a flat line for every source forever -- the
    exact failure `PushHealth.record_reconnect` had one milestone ago.

    Written against a ledger holding a *non-zero* count, because zero is the
    true value on a first connect and a case that only ever saw one could
    not tell the reader from the constant.
    """
    await _seed(fakes, _source("A"))
    supervisor = _supervisor(fakes)
    await supervisor.start()
    await _settle()
    try:
        adapter = fakes.adapters.built["A"]
        adapter._push_reconnects = 3
        snapshots = supervisor.push_snapshots()
        assert set(snapshots) == {"A"}
        assert snapshots["A"].reconnects == 3
        assert snapshots["A"].delivering is adapter.supports_push
    finally:
        await supervisor.stop()


# -- the reconnect gap-closer -------------------------------------------
#
# The gap-closer runs `reconcile(source, DELTA, adapter)` on every reconnect,
# and a DELTA with no cursor is `list_items(since=None)` -- the whole library.
# So on a deployment that has never completed an item walk, starting the
# process is a full walk of a server the operator may not own, issued by
# `uvicorn` with default settings and no command. `USHER_PUSH_GAP_CLOSE` is
# the switch and `cursored` is the shipped answer; these cases are the three
# arms plus the log-rate one.

# Before any run these cases create, so a stocked item is inside the window a
# completed run's cursor opens.
_SYNCED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_CHANGED_AT = datetime(2026, 1, 2, tzinfo=UTC)
# Every gap-close line carries this, so a case can count *the* lines rather
# than every warning the lane happens to emit.
_GAP_MARKER = "push gap"


async def _completed_walk(fakes: _Fakes, source: Source) -> None:
    """What `usher sync` leaves behind: one completed item-lane run, whose
    `started_at` is the floor every later delta resumes from."""
    await fakes.runs.add(
        SyncRun(
            source_id=source.id,
            kind=SyncRunKind.FULL,
            status=SyncRunStatus.COMPLETED,
            started_at=_SYNCED_AT,
            finished_at=_SYNCED_AT,
        )
    )


async def _walked(fakes: _Fakes, source: Source) -> list[SyncRunKind]:
    return [run.kind for run in await fakes.runs.list_for_source(source.id)]


async def test_a_gap_close_with_no_cursor_does_not_walk_the_whole_library(
    fakes: _Fakes,
) -> None:
    """The shipped default, and the defect it closes.

    `reconcile(source, DELTA, adapter)` reads its `since` from the newest
    *completed* item-lane run; with none, `since` is `None` and the "delta" is
    `list_items(since=None)` -- 1,126,789 items on the household this project
    measures. That is not a gap: a gap is the window a socket was down, and
    with no completed walk the window is the entire library, which is
    `usher sync`'s job rather than a reconnect handler's.

    Asserted on both the walk and the log, and drained on *either* arriving,
    so the case fails on what happened rather than on a timeout.
    """
    source = _source("A")
    await _seed(fakes, source)
    fakes.adapters.stock(_item("emby-1"), _CHANGED_AT)
    sink = io.StringIO()
    supervisor = _supervisor(fakes, worker_enabled=False)
    logger.remove()
    try:
        logger.add(sink, level="WARNING")
        await supervisor.start()
        await _drain(lambda: bool(sink.getvalue()) or bool(_stored(fakes.media_items)))
    finally:
        logger.remove()
        await supervisor.stop()
    assert _stored(fakes.media_items) == [], "the lane walked a library nobody asked it to walk"
    assert await _walked(fakes, source) == [], "the lane opened a sync run for the refused walk"
    logged = sink.getvalue()
    assert _GAP_MARKER in logged, logged
    assert "A" in logged, "the refusal does not name the source it refused"
    assert "usher sync" in logged, "the refusal does not say what to run instead"


async def test_a_gap_close_walks_the_delta_once_a_walk_has_completed(fakes: _Fakes) -> None:
    """And the bound is not "never walk".

    A source with a completed run has a real `since`, so the reconnect delta
    is the bounded thing PRD 03 designed -- Emby does not re-deliver what a
    disconnected client missed, so this is the only cover there is. A fix that
    turned the gap-closer off would break that; this is the case that fails if
    it does.
    """
    source = _source("A")
    await _seed(fakes, source)
    await _completed_walk(fakes, source)
    fakes.adapters.stock(_item("emby-1"), _CHANGED_AT)
    supervisor = _supervisor(fakes, worker_enabled=False)
    await supervisor.start()
    try:
        await _drain(lambda: _stored(fakes.media_items) == ["emby-1"])
    finally:
        await supervisor.stop()
    assert SyncRunKind.DELTA in await _walked(fakes, source)


async def test_push_gap_close_always_walks_uncursored_and_says_so_first(fakes: _Fakes) -> None:
    """The escape hatch for an operator who wants the old behaviour back --
    and it is not silent. The line goes out *before* the walk starts, at
    WARNING, naming the source and naming the size, because an operator who
    finds out from their media server's access log has found out too late."""
    source = _source("A")
    await _seed(fakes, source)
    fakes.adapters.stock(_item("emby-1"), _CHANGED_AT)
    sink = io.StringIO()
    supervisor = _supervisor(fakes, worker_enabled=False, push_gap_close="always")
    logger.remove()
    try:
        logger.add(sink, level="WARNING")
        await supervisor.start()
        await _drain(lambda: _stored(fakes.media_items) == ["emby-1"])
    finally:
        logger.remove()
        await supervisor.stop()
    logged = sink.getvalue()
    assert _GAP_MARKER in logged, logged
    assert "A" in logged, "the warning does not name the source it is about to walk"
    assert "entire library" in logged, "the warning does not say how big the walk is"


async def test_push_gap_close_never_closes_no_gap_at_all(fakes: _Fakes) -> None:
    """The other end of the switch: a deployment pointed at a household it
    does not own, whose walks are an operator's cron and nothing else.

    Costly and stated rather than hidden -- with no gap-closer, a change made
    while the socket was down is not seen until the next walk -- so the line
    is emitted every time the lane declines, at INFO rather than WARNING,
    because it is an answer the operator configured.
    """
    source = _source("A")
    await _seed(fakes, source)
    await _completed_walk(fakes, source)
    fakes.adapters.stock(_item("emby-1"), _CHANGED_AT)
    sink = io.StringIO()
    supervisor = _supervisor(fakes, worker_enabled=False, push_gap_close="never")
    logger.remove()
    try:
        logger.add(sink, level="INFO")
        await _drain_lane(supervisor, sink, fakes)
    finally:
        logger.remove()
        await supervisor.stop()
    assert _stored(fakes.media_items) == [], "`never` still walked"
    assert await _walked(fakes, source) == [SyncRunKind.FULL], "`never` still opened a run"
    assert _GAP_MARKER in sink.getvalue(), sink.getvalue()


async def _drain_lane(supervisor: LaneSupervisor, sink: io.StringIO, fakes: _Fakes) -> None:
    await supervisor.start()
    await _drain(lambda: _GAP_MARKER in sink.getvalue() or bool(_stored(fakes.media_items)))


async def test_the_gap_close_is_logged_per_close_and_not_per_supervisor_poll(
    fakes: _Fakes,
) -> None:
    """A per-lane fact logged in a per-poll function is the ~17,280 warnings a
    day `config-cli-and-deployment.md` records against `build_worker`.

    The refresher re-reads the source list every `push_source_refresh_seconds`
    and the lanes it finds are already running, so a line written there would
    repeat forever while the gap is closed exactly once. Asserting after one
    poll cannot tell "once" from "per poll", so this drains until the
    refresher has demonstrably polled several times -- every one of those
    opens a unit of work -- and asserts the *count*.
    """
    source = _source("A")
    await _seed(fakes, source)
    sink = io.StringIO()
    supervisor = _supervisor(fakes, worker_enabled=False, push_source_refresh_seconds=0.001)
    logger.remove()
    try:
        logger.add(sink, level="WARNING")
        await supervisor.start()
        await _drain(lambda: len(fakes.units_of_work) >= 12)
    finally:
        logger.remove()
        await supervisor.stop()
    logged = sink.getvalue()
    polls = len(fakes.units_of_work)
    assert logged.count(_GAP_MARKER) == 1, f"logged once per poll over {polls} of them: {logged}"


# -- the worker lane ----------------------------------------------------


async def test_the_worker_lane_runs_when_enabled(fakes: _Fakes) -> None:
    """PRD 03's read-through loop only closes if the enrichment happens in
    the process the SSE client is connected to. M5's bus is in-memory, so
    this is what makes `title.updated` reach anybody."""
    supervisor = _supervisor(fakes)
    await supervisor.start()
    await _settle()
    assert supervisor.worker_running() is True
    await supervisor.stop()
    assert supervisor.worker_running() is False


async def test_the_worker_lane_recovers_on_a_lease_and_not_on_every_pass(
    fakes: _Fakes,
) -> None:
    """PRD 08's recovery, and **both** halves of what M9's W1 changed about it.

    `JobQueue.requeue_running`'s `older_than_seconds=0.0` default requeues
    everything currently running. That was the only lever this project had, and
    M9's S3 measured the dead end it leads to: one of three workers died holding
    twenty claims, and pulling that lever would have taken the other two
    workers' **live** claims with it. It is now unsafe inside one process too,
    because one worker holds several claims at a time.

    So two assertions, and the second is the one with teeth:

    - **not per pass**, because recovery is an `UPDATE` scanning
      `status = 'running'` and there is nothing to find between leases; and
    - **never at age zero**, which is the difference between recovery and
      theft. A lane calling `requeue_running()` bare answers "1 requeue" over
      three passes exactly as a correct one does, so counting alone ratifies
      it.

    `idle_seconds` is dialled down so several passes fit in milliseconds: at
    the shipped five seconds this case would take fifteen, and a case that
    asserted after one pass could not tell "once" from "per pass".
    """
    supervisor = _supervisor(fakes, worker_idle_seconds=0.001)
    await supervisor.start()
    await _drain(lambda: fakes.queue.claims >= 3, bound=2.0)
    await supervisor.stop()
    assert fakes.queue.requeues == 1, (
        f"the worker lane requeued {fakes.queue.requeues} times over {fakes.queue.claims} passes"
    )
    assert fakes.queue.requeue_ages == [pytest.approx(DEFAULT_LEASE_SECONDS)], (
        "the lane recovered at an age that would take a live worker's claims: "
        f"{fakes.queue.requeue_ages}"
    )


class _JustBooted:
    """`time`, with `monotonic()` frozen at a small uptime.

    Substituted for the **module's** `time`, never the global one:
    `asyncio`'s own timers resolve `time.monotonic` through `loop.time()` at
    call time, so patching `time.monotonic` globally freezes every sleep in
    the event loop and the case hangs instead of failing.
    """

    def __init__(self, uptime: float) -> None:
        self._uptime = uptime

    def monotonic(self) -> float:
        return self._uptime


async def test_the_worker_lane_recovers_on_its_first_pass_on_a_host_that_just_booted(
    fakes: _Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 **The throttle's origin is the identity element of its own
    comparison, and that made the field above lie.**

    `time.monotonic()` on Linux is seconds since **boot**. Against an origin of
    `0.0`, `now - origin >= lease / 2` is *false* for the first
    `job_lease_seconds / 2` -- 150 s at the shipped lease -- so a
    worker-enabled process started with the machine skips its first recovery
    pass entirely and `/health/ready` answers `recovered_claims: null`, the
    value `LaneReport` documents as *"this process runs no worker"*. That is
    the field lying about the one thing it exists to report, at exactly the
    moment a compose stack comes up holding the previous boot's orphans.

    `.claude/rules/testing-discipline.md` already names the shape: a fixture
    whose origin is the identity element of the operation under test cannot
    distinguish the operation from its absence. Here `0.0` is the identity for
    the subtraction, and every case in this file that recovers passes only
    because the host it runs on has been up for longer than half a lease --
    which is a property of the machine, not of the code.

    So the clock is shimmed rather than trusted, and the assertion is the same
    one `test_the_worker_lane_recovers_on_a_lease_and_not_on_every_pass`
    makes at a normal uptime. Against `0.0` this reports **0** requeues.
    """
    monkeypatch.setattr("usher.api.lanes.time", _JustBooted(10.0))
    supervisor = _supervisor(fakes, worker_idle_seconds=0.001)
    assert _settings().job_lease_seconds / 2 > 10.0, (
        "the premise: the shimmed uptime is inside the window the throttle would skip"
    )

    await supervisor.start()
    try:
        await _drain(lambda: fakes.queue.claims >= 2, bound=2.0)
    finally:
        await supervisor.stop()

    assert fakes.queue.requeues == 1, (
        "a worker started within half a lease of boot never recovered, so its report "
        "of what it took back is `null` -- indistinguishable from a process running no worker"
    )
    # And still exactly once and still at the lease, so the fix did not buy the
    # first pass by removing the throttle.
    assert fakes.queue.requeue_ages == [pytest.approx(DEFAULT_LEASE_SECONDS)]
    assert supervisor.recovered_claims() == 0, "it asked; `null` would mean it never did"


async def test_a_missing_tmdb_key_is_not_re_reported_on_every_pass(fakes: _Fakes) -> None:
    """PRD 08's "TMDb key missing" degradation is worth surfacing once.

    It used to be logged by `build_worker`, which the loop below calls once
    per pass -- so the default no-key deployment produced a `WARNING` every
    `IDLE_SLEEP_SECONDS`, measured at exact 5 s intervals, i.e. ~17,280 a day
    forever. A warning at that rate trains an operator to ignore warnings,
    which is the failure a log level exists to prevent. The line moved to
    `composition.metadata_provider`, which is where the decision is actually
    made and which every composition root calls exactly once per process.

    Asserting after one pass could not tell "once" from "per pass", so this
    drains three the way the requeue case above does -- and it counts *any*
    warning rather than only the TMDb one, because a per-pass line about
    anything else would be the same defect wearing a different sentence.
    """
    sink = io.StringIO()
    supervisor = _supervisor(fakes, worker_idle_seconds=0.001)
    logger.remove()
    try:
        logger.add(sink, level="WARNING")
        await supervisor.start()
        await _drain(lambda: fakes.queue.claims >= 3, bound=2.0)
        await supervisor.stop()
    finally:
        logger.remove()
    logged = sink.getvalue()
    assert "TMDb" not in logged, (
        f"the worker lane re-reported the missing key over {fakes.queue.claims} passes: {logged}"
    )
    assert logged == "", f"the worker lane logged once per pass: {logged}"


async def test_the_lanes_are_settings_gated(fakes: _Fakes) -> None:
    """PRD 01: "A `--worker` entrypoint flag exists from day one so lanes
    can be moved to a separate container later by editing compose, with no
    code change." These settings are that flag."""
    await _seed(fakes, _source("A"))
    supervisor = _supervisor(fakes, push_enabled=False, worker_enabled=False)
    await supervisor.start()
    await _settle()
    try:
        assert supervisor.running_sources() == []
        assert supervisor.worker_running() is False
        assert fakes.adapters.built == {}
    finally:
        await supervisor.stop()


# -- start() opens nothing ----------------------------------------------


async def test_start_creates_tasks_and_never_awaits_a_unit_of_work(fakes: _Fakes) -> None:
    """`create_app`'s lifespan builds an engine and opens no connection, and
    that is load-bearing: `/health` answers 200 with Postgres down while
    `/health/ready` reports 503, verified live against a real container in
    M1. A `start()` that read the source list inline would turn a database
    outage into a failure to boot.

    Driven **one step by hand** rather than timed: `coro.send(None)` raises
    `StopIteration` for a coroutine that never awaited and hands back a
    future for one that parked. No scheduler, no clock, no timeout, and it
    cannot be satisfied by a slow-but-eventually-fine implementation --
    which is the technique group E established for the bus's own
    never-blocks claim. The plan's own draft of `start()` did `await
    self.refresh()`, which fails this on its first line.
    """
    await _seed(fakes, _source("A"))
    supervisor = _supervisor(fakes)
    coro = supervisor.start()
    try:
        with pytest.raises(StopIteration):
            coro.send(None)
        # And the lanes really were created, so this is not passing because
        # `start()` did nothing at all.
        assert supervisor.worker_running() is True
        await _settle()
        assert supervisor.running_sources() == ["A"]
    finally:
        await supervisor.stop()


# -- helpers ------------------------------------------------------------


def _stored(media_items: MediaItemRepository) -> list[str]:
    assert isinstance(media_items, FakeMediaItemRepository)
    return sorted(item.external_id for item in media_items._items.values())


async def _drain(
    until: Callable[[], bool], *, bound: float = 5.0, note: Callable[[], str] | None = None
) -> None:
    """Turn the loop until `until()` holds, bounded so a supervisor that
    never runs the lane fails the case instead of hanging the suite.

    `asyncio.wait_for` cannot bound a coroutine that never yields, which is
    why this is a deadline over `sleep(0)` rather than a timeout around one
    await -- the same reason `tests/integration/test_job_queue.py` bounds
    its claims explicitly.

    **`note` is what the deadline says instead of nothing, and it exists
    because a timeout is the failure mode a mutation most often produces
    here.** Measured: the WARNING-to-DEBUG plant over `_close_gap` empties
    the refusal list, so the *drain* expires and the count assertion the
    case was written for is never reached -- and `"the lane never got
    there"` tells a reader nothing about a log level. A `note` renders the
    same quantities the assertion downstream would have shown.
    """
    deadline = time.perf_counter() + bound
    while time.perf_counter() < deadline:
        if until():
            return
        await asyncio.sleep(0.005)
    detail = "" if note is None else f": {note()}"
    raise AssertionError(f"the lane never got there{detail}")


async def test_the_worker_lane_holds_one_embedder_across_every_pass(fakes: _Fakes) -> None:
    """**The measured failure `composition.embedder` exists to prevent, at
    the layer where the mistake is actually available.**

    `_run_worker` rebuilds the pipeline, the registry and the worker every
    turn of a loop whose floor is `IDLE_SLEEP_SECONDS = 5.0`. A model is the
    one collaborator that must *not* be rebuilt there: the load is 4.84 s
    cold and 0.13 s warm over 65 MB of ONNX, so a per-pass build would spend
    more time loading than running jobs, forever, with nothing in the logs
    saying so. The precedent is on record in this repository at ~17,280 log
    lines a day for a *string*.

    Three passes, not one: a single pass cannot tell "once" from "per pass"
    -- the same shape the requeue and missing-key cases above needed.
    Counted through an embedder that records its own construction, so the
    case fails against any spelling that calls the factory in the loop rather
    than holding what the composition root handed it.
    """
    builds: list[int] = []

    class _Loading(FakeEmbedder):
        def __init__(self) -> None:
            super().__init__()
            builds.append(1)

    supervisor = _supervisor(fakes, worker_idle_seconds=0.001, embedder=_Loading())
    await supervisor.start()
    await _drain(lambda: fakes.queue.claims >= 3, bound=2.0)
    await supervisor.stop()

    assert builds == [1], f"the lane built {len(builds)} embedders over {fakes.queue.claims} passes"


async def test_a_worker_lane_without_an_embedder_never_claims_index_work(
    fakes: _Fakes,
) -> None:
    """The guard, observed through the queue rather than through the wiring.

    `run_once` claims `list(self._handlers)`, so a lane with no model must
    not ask for `index` jobs at all -- claiming one it cannot run either
    crashes on the lookup or parks work whose only problem is that it was
    offered to the wrong process, and a job parked that way needs a human to
    release it. A deployment without the extra leaves that work for one that
    has it, and still has full-text and trigram over all 1.27M titles.
    """
    supervisor = _supervisor(fakes, worker_idle_seconds=0.001)
    await supervisor.start()
    await _drain(lambda: fakes.queue.claims >= 1, bound=2.0)
    await supervisor.stop()

    assert fakes.queue.claimed_kinds
    for kinds in fakes.queue.claimed_kinds:
        assert JobKind.INDEX not in kinds


async def test_a_worker_lane_without_an_llm_client_never_claims_curate_work(
    fakes: _Fakes,
) -> None:
    """The same guard as the embedder's, one lane over, and observed the same
    way: through what the lane *asked the queue for* rather than through the
    wiring.

    `usher.composition.llm_client` answers `(None, no-op)` for
    `USHER_LLM_ENABLED=false`, which is the shipped default, so this is what
    nearly every deployment runs. A lane that claimed `curate` anyway would
    reach a handler it does not have -- and the composition root cannot even
    build the service, because `CurationService`'s client is `LLMClient` and
    not `LLMClient | None`.

    Enqueued rather than asserted against an empty queue: `claimed_kinds`
    records what was asked for whether or not anything was there, but a job
    surviving the pass is the operator-visible half -- curate work waits for a
    process that can run it instead of parking (PRD 08 reserves parking for
    work a human has to look at).
    """
    await fakes.queue.enqueue(
        [JobRequest(kind=JobKind.CURATE, key=str(USER_ID), priority=JobPriority.BACKFILL)]
    )

    supervisor = _supervisor(fakes, worker_idle_seconds=0.001)
    await supervisor.start()
    await _drain(lambda: fakes.queue.claims >= 1, bound=2.0)
    await supervisor.stop()

    assert fakes.queue.claimed_kinds
    for kinds in fakes.queue.claimed_kinds:
        assert JobKind.CURATE not in kinds
    assert [job.status for job in fakes.queue.jobs_of(JobKind.CURATE)] == [JobStatus.PENDING]


async def test_a_worker_lane_with_an_llm_client_claims_curate_work(fakes: _Fakes) -> None:
    """The control that makes the case above evidence rather than a
    tautology, and the only thing that proves the client the composition root
    built ever reaches `build_worker`.

    `LaneSupervisor` carries the client for the reason it carries the
    embedder: both are per-*process* resources, and `_run_worker` rebuilds
    everything else once per pass. A supervisor that accepted one and dropped
    it on the floor passes every other case in this file -- and turns
    `USHER_LLM_ENABLED=true` into a queue that grows forever.

    `INDEX` is asserted absent alongside it so the two cannot drift into "one
    optional collaborator turns both lanes on".
    """
    supervisor = _supervisor(fakes, worker_idle_seconds=0.001, client=FakeLLMClient())
    await supervisor.start()
    await _drain(lambda: fakes.queue.claims >= 1, bound=2.0)
    await supervisor.stop()

    assert fakes.queue.claimed_kinds
    for kinds in fakes.queue.claimed_kinds:
        assert JobKind.CURATE in kinds
        assert JobKind.INDEX not in kinds


# -- the rows.refresh lane ----------------------------------------------
#
# PRD 06's "served stale while refreshing". The two claims that need care are
# **the request never waits** -- settled against `HomeService` in
# `tests/unit/test_services_home_stale.py`, where the coroutine is driven by
# hand -- and **exactly one refresh per key while one is in flight**, which is
# a concurrency claim and therefore needs observed overlap. What overlaps is
# *not* two requests: a stale serve never suspends, which is the whole feature,
# so two of them cannot intersect in wall-clock and a case that asserted they
# did would be asserting the feature is broken. The intersection with teeth is
# **a request against the running refresh**, and that is the pair the case
# below records.


class _GatedRow(FakeRow):
    """A row whose build parks until a case opens the gate, recording the
    wall-clock window it spent inside `build`.

    Real time in a real `await`, for the reason `_SlowAdapter` above states: a
    fake that never truly suspends makes every concurrency window disjoint,
    and "these did not overlap" is then satisfied by the concurrency the case
    is trying to forbid.
    """

    def __init__(self, slug: str, *, cards: Sequence[RowCard] = ()) -> None:
        super().__init__(slug, cards=cards)
        self.gate = asyncio.Event()
        # Set on the way *in*, so a case can wait for the refresh to really be
        # in flight rather than sleeping and hoping. Waiting on the window --
        # which is recorded in the `finally` -- would mean waiting for the
        # thing the case exists to overlap with to be over.
        self.entered = asyncio.Event()
        self.windows: list[tuple[float, float]] = []
        self.failure: Exception | None = None

    async def build(self, ctx: RowContext) -> BuiltRow:
        started = time.perf_counter()
        self.entered.set()
        try:
            await self.gate.wait()
            if self.failure is not None:
                raise self.failure
            return await super().build(ctx)
        finally:
            self.windows.append((started, time.perf_counter()))


def _row_card(name: str) -> RowCard:
    return RowCard(
        title_id=new_id(),
        kind=TitleKind.MOVIE,
        name=name,
        enrichment_state=EnrichmentState.SKELETON,
    )


def _gated_provider(slug: str = "recently-added") -> tuple[FakeRowProvider, _GatedRow]:
    row = _GatedRow(slug, cards=(_row_card(slug),))
    return FakeRowProvider(proposals=(ScoredRow(row=row, score=0.9),), slug_prefix=slug), row


def _overlap(left: tuple[float, float], right: tuple[float, float]) -> float:
    """Seconds the two windows share. Zero when they merely touch."""
    return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))


def _without_suspending(coro: Coroutine[object, object, object]) -> object:
    """Drive `coro` one step and require it to have finished.

    `tests/unit/test_services_home_stale.py`'s helper, copied for the reason
    the `_Clock` fixtures in this suite are copied. Here it does a second job:
    the read below happens while the lane is parked inside `build`, so an
    implementation that made that read a *rebuild* -- `read_screen` popping the
    entry it just served, say -- would park on the same gate and **hang the
    whole file** rather than fail it. Driven by hand it fails in microseconds,
    on its own message.
    """
    try:
        coro.send(None)
    except StopIteration as finished:
        return finished.value
    coro.close()
    raise AssertionError(
        "compose suspended: a read that should have been served from the cache "
        "rebuilt instead, and would have parked on the refresh's own gate"
    )


_HOUSEHOLD = User(id=USER_ID, name="default", is_default=True)


def _stale(cache: RowCache, screen: tuple[BuiltRow, ...]) -> None:
    """Plant a screen that is already expired and still inside its grace.

    A negative TTL rather than a stepped clock, because this file's caches run
    on the wall clock the server runs on -- the same shape
    `tests/integration/test_rows_refresh.py` plants with, and the arithmetic
    behind it is pinned in `tests/unit/test_services_home_stale.py`.
    """
    cache.put_screen(_HOUSEHOLD.id, screen, ttl=-timedelta(seconds=1))


def _context(user: User) -> RowContext:
    """A row context for that household.

    The stale-serve path reads none of its repositories -- `HomeService`
    answers out of the cache before the first `propose` -- so the only field
    that matters here is `user`, which is the cache key and the value the
    queue hands to the lane.
    """
    return dataclasses.replace(Library().context(), user=user)


async def test_a_stale_key_is_refreshed_on_the_lanes_own_unit_of_work(
    fakes: _Fakes,
) -> None:
    """The lane drains the queue, rebuilds the screen and replaces the stale
    entry -- and it opens a unit of work of its own to do it.

    A refresh sharing the request's session passes almost every test that does
    not look for it, because the request's session usually still works for a
    moment after the handler returns. Here the assertion is that the lane
    opened one at all; `tests/integration/test_rows_refresh.py` is where it is
    made against real sessions, in both directions.
    """
    cache = RowCache(clock=lambda: datetime.now(UTC))
    queue = RefreshQueue()
    provider, row = _gated_provider()
    row.gate.set()
    _stale(cache, ())
    supervisor = _supervisor(
        fakes,
        rows=cache,
        refreshes=queue,
        providers=[provider],
        # Off so `units_of_work` counts refreshes and nothing else -- the
        # worker lane opens one per pass and the refresher one per interval.
        push_enabled=False,
        worker_enabled=False,
    )
    await supervisor.start()
    try:
        await _settle()
        assert fakes.units_of_work == [], "an idle refresh lane opens no session at all"
        queue.schedule(_HOUSEHOLD)
        await _drain(lambda: queue.pending == frozenset())
    finally:
        await supervisor.stop()

    assert len(fakes.units_of_work) == 1, "the refresh opened its own unit of work"
    read = cache.read_screen(_HOUSEHOLD.id)
    assert read.freshness is Freshness.FRESH
    assert read.screen is not None
    assert [one.slug for one in read.screen] == ["recently-added"]


async def test_a_refresh_composes_the_registry_minus_what_an_operator_disabled(
    fakes: _Fakes,
) -> None:
    """**The hole a route-only toggle leaves, and it is the one that reopens
    itself** (M9 E2).

    `PUT /admin/rows/providers/{slug}` clears `RowCache`, so the next request
    composes without the disabled provider and caches that. Thirty seconds
    later the screen is stale, a read serves it and schedules a refresh -- and a
    lane composing the unfiltered `pipeline.row_providers` writes the disabled
    shelf straight back into the same cache. The route looks like it worked and
    the shelf returns, on a timer, which is exactly the *"an operator finds it
    and expects toggling it to do something"* failure M7's boundary call 9
    refused the table over.

    **Two providers, because one cannot distinguish a filter from a lane that
    composed nothing.** `recently-added` has to be on the refreshed screen, or
    "the disabled slug is absent" is satisfied by a lane that failed, by a
    queue nothing drained, and by a cache entry nobody replaced. The single
    equality below carries both halves; the fixture registering two is what
    makes it able to.
    """
    cache = RowCache(clock=lambda: datetime.now(UTC))
    queue = RefreshQueue()
    stored = FakeRowProviderSettingsRepository()
    await stored.set_enabled("seasonal", enabled=False)
    off, off_row = _gated_provider(slug="seasonal")
    kept, kept_row = _gated_provider(slug="recently-added")
    off_row.gate.set()
    kept_row.gate.set()
    _stale(cache, ())
    supervisor = _supervisor(
        fakes,
        rows=cache,
        refreshes=queue,
        providers=[off, kept],
        provider_settings=stored,
        push_enabled=False,
        worker_enabled=False,
    )
    await supervisor.start()
    try:
        queue.schedule(_HOUSEHOLD)
        await _drain(lambda: queue.pending == frozenset())
    finally:
        await supervisor.stop()

    read = cache.read_screen(_HOUSEHOLD.id)
    assert read.screen is not None
    assert [one.slug for one in read.screen] == ["recently-added"], (
        "the refresh re-composed a provider a stored row disables"
    )


async def test_a_read_during_an_in_flight_refresh_schedules_nothing_and_they_overlap(
    fakes: _Fakes,
) -> None:
    """**The concurrency claim, with observed overlap rather than a count.**

    "Exactly one refresh for one key" is also what a serialised pair produces,
    so the case records the wall-clock interval the refresh occupied and the
    interval a second read occupied, and asserts they genuinely intersect
    before asserting there was one build.

    The pair is a *read against the refresh* rather than two reads, and that
    is not a weakening. A stale serve never suspends -- driven by hand in
    `tests/unit/test_services_home_stale.py` -- so two of them are disjoint by
    construction and `asyncio.gather` over them would produce exactly the
    disjoint windows `.claude/rules/rows-and-genome.md` records as the trap.
    What the dedup has to survive is a request arriving *while the refresh
    runs*, which is the window a queue that cleared its key at `take()` leaves
    open: that spelling schedules a second full compose over the same
    household, and it is invisible to any case that only counts.
    """
    cache = RowCache(clock=lambda: datetime.now(UTC))
    queue = RefreshQueue()
    provider, row = _gated_provider()
    _stale(cache, ())
    service = HomeService(providers=[provider], cache=cache, refresh=queue.schedule)
    supervisor = _supervisor(
        fakes,
        rows=cache,
        refreshes=queue,
        providers=[provider],
        push_enabled=False,
        worker_enabled=False,
    )
    await supervisor.start()
    try:
        first = await service.compose(_context(_HOUSEHOLD))
        assert first == (), "the first read was served the stale screen"
        assert queue.depth == 1, "and handed the key over"
        # Wait for the lane to be *inside* the build, not merely to have taken
        # the key: the window this case intersects against is the refresh's,
        # and a key off the queue is not yet a refresh in flight.
        await _drain(row.entered.is_set)

        started = time.perf_counter()
        served = _without_suspending(service.compose(_context(_HOUSEHOLD)))
        read_window = (started, time.perf_counter())

        assert queue.depth == 0, "a key already being refreshed must not be queued again"
        assert queue.dropped == 0, "and it was deduplicated, not dropped"
        assert served == (), "the household was served the stale screen it had"

        row.gate.set()
        await _drain(lambda: queue.pending == frozenset())
    finally:
        row.gate.set()
        await supervisor.stop()

    assert len(row.windows) == 1, "one build, over one key"
    assert _overlap(row.windows[0], read_window) > 0.0, (
        f"the read {read_window} and the refresh {row.windows[0]} did not overlap, "
        "so 'one refresh' is what a serialised pair would also produce"
    )


async def test_a_refresh_that_raises_leaves_the_stale_screen_and_names_the_lane(
    fakes: _Fakes,
) -> None:
    """A crashed refresh must cost the refresh and nothing else.

    Three things, and the third is the one a `while True` gets wrong: the
    stale entry survives so the next request is still served, the key is
    released so the household is not locked out of refreshes for the life of
    the process, and the failure is logged **with the lane's name**. Without
    the last, a lane that died leaves an unretrieved task exception CPython
    reports at GC time, to stderr, with no source in it -- the shape `_guard`
    exists for, arriving through a loop instead of through a task.
    """
    cache = RowCache(clock=lambda: datetime.now(UTC))
    queue = RefreshQueue()
    provider, row = _gated_provider()
    row.failure = ZeroDivisionError("a bug in a provider")
    row.gate.set()
    _stale(cache, ())
    supervisor = _supervisor(
        fakes,
        rows=cache,
        refreshes=queue,
        providers=[provider],
        push_enabled=False,
        worker_enabled=False,
    )
    lines: list[str] = []
    sink = logger.add(lines.append, level="TRACE", serialize=True)
    try:
        queue.schedule(_HOUSEHOLD)
        await supervisor.start()
        await _drain(lambda: queue.pending == frozenset())
        assert supervisor.rows_refreshing(), "one bad refresh must not end the lane"
    finally:
        logger.remove(sink)
        await supervisor.stop()

    failure = [line for line in lines if "rows.refresh" in line]
    assert failure, lines
    assert "ZeroDivisionError" in failure[0]
    stale = cache.read_screen(_HOUSEHOLD.id, grace=SCREEN_STALE_GRACE)
    assert stale.freshness is Freshness.STALE, "the stale entry must still be servable"


async def test_the_refresh_lane_is_not_a_source_lane(fakes: _Fakes) -> None:
    """**A third lane kind must not change what `running_sources()` means.**

    That list is what `/health/ready` reports as `lanes.push`, and it is also
    the mutation surface for "readiness gates on the lanes": a refresh lane
    that joined it would put a screen refresh into a load balancer's decision.
    `tests/integration/test_health.py` is where the status code half is
    settled, against a reachable database; this is the supervisor's own half.
    """
    cache = RowCache(clock=lambda: datetime.now(UTC))
    supervisor = _supervisor(
        fakes, rows=cache, refreshes=RefreshQueue(), push_enabled=False, worker_enabled=False
    )
    await supervisor.start()
    await _settle()
    try:
        assert supervisor.rows_refreshing() is True
        assert supervisor.running_sources() == []
        assert supervisor.crashed_sources() == []
        assert supervisor.worker_running() is False
    finally:
        await supervisor.stop()
    assert supervisor.rows_refreshing() is False, "stop() takes the refresh lane with it"


async def test_no_cache_means_no_refresh_lane(fakes: _Fakes) -> None:
    """The control for the case above, and the reason the lane is gated on
    being handed the pair rather than on a setting.

    `usher work` builds a supervisor that serves no screens, so it holds
    neither cache nor queue and must start no refresh lane -- a lane polling a
    queue nothing can ever fill is a task and a log line with no reader.
    """
    supervisor = _supervisor(fakes)
    await supervisor.start()
    await _settle()
    try:
        assert supervisor.rows_refreshing() is False
    finally:
        await supervisor.stop()


async def test_the_refresh_is_a_root_span_linked_to_the_request_that_served_stale(
    fakes: _Fakes,
) -> None:
    """PRD 10's `rows.refresh`, and the two invariants that move with it.

    **A root with a `Link`, never a child.** The request that served the stale
    screen has usually already returned, so a child span of a finished parent
    misstates causality -- the same reason a worker's `job.*` is a root, and
    the convention PRD 10 already specifies. Asserted as *parentage*: a refresh
    that nested still produces valid ids, still exports, and still carries the
    name the document asks for. The schedule happens inside a span here so
    there is a context to be wrongly parented to -- without one, "root" is
    what an implementation with no ambient span produces anyway and the
    assertion could not fail.

    **And its `row.build` spans have no `home.compose` parent at all.** PRD 10
    said "the number of `row.build` children of a `home.compose` is the number
    of misses"; a background refresh builds outside any request, so
    `HomeService.rebuild` opens none and the sentence is corrected in the same
    commit. A refresh that minted one would nest perfectly and quietly double
    the `home.compose` count on every dashboard reading it as "requests that
    composed".

    Written because a documented span nobody emits is the trace-side
    permanently-empty panel, indistinguishable from a quiet system.
    """
    exporter = InMemorySpanExporter()
    tracers = TracerProvider()
    tracers.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(tracers)

    cache = RowCache(clock=lambda: datetime.now(UTC))
    queue = RefreshQueue()
    provider, row = _gated_provider()
    row.gate.set()
    _stale(cache, ())
    service = HomeService(providers=[provider], cache=cache, refresh=queue.schedule)
    supervisor = _supervisor(
        fakes,
        rows=cache,
        refreshes=queue,
        providers=[provider],
        push_enabled=False,
        worker_enabled=False,
    )
    await supervisor.start()
    try:
        with trace.get_tracer("test").start_as_current_span("GET /home") as request:
            _without_suspending(service.compose(_context(_HOUSEHOLD)))
            request_id = request.get_span_context().span_id
        await _drain(lambda: queue.pending == frozenset())
    finally:
        await supervisor.stop()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert "rows.refresh" in spans, sorted(spans)
    refresh = spans["rows.refresh"]
    assert refresh.parent is None, "the refresh nested under a request that had already returned"
    assert [link.context.span_id for link in refresh.links] == [request_id]
    assert refresh.attributes is not None
    assert refresh.attributes["usher.home.rows"] == 1

    assert "home.compose" not in spans, (
        "a refresh minted a home.compose -- PRD 10 counts those as compositions a request paid for"
    )
    build = spans["row.build"]
    assert build.parent is not None
    assert build.parent.span_id == refresh.context.span_id
