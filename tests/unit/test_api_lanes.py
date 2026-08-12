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
import io
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
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
from tests.fakes.search_index import FakeSearchIndex, FakeSuggestIndex
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
from usher.domain.watch import User
from usher.ports.credentials import SourceCredentials
from usher.ports.embedding import Embedder
from usher.ports.jobs import JobRequest
from usher.ports.llm import LLMClient
from usher.ports.repository import MediaItemRepository, RowProviderSettingsRepository
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
from usher.services.matching import MatchService
from usher.services.reconcile import ReconcileService
from usher.services.rows import ROW_PROVIDERS
from usher.services.rows.cache import Freshness, RefreshQueue, RowCache
from usher.services.search import SearchService
from usher.services.similar import SimilarityService
from usher.services.taste import TasteService
from usher.services.watch_sync import WatchStateSyncService

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

    def crash(self, name: str) -> None:
        self.crashing.add(name)

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        if source.name in self.crashing:
            kind: type[FakeSourceAdapter] = _CrashingAdapter
        elif source.name in self.slow:
            kind = _SlowAdapter
        else:
            kind = FakeSourceAdapter
        adapter = kind(source)
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
        return await super().requeue_running(older_than_seconds=older_than_seconds)


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
    queue: _CountingQueue
    adapters: _Adapters
    events: FakeEventPublisher
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
    runs = FakeSyncRunRepository()
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
    taste = TasteService(
        watch_states=watch_states,
        embeddings=embeddings,
        titles=titles,
        taste=FakeTasteRepository(watch_states),
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
        taste_rows=FakeTasteRepository(watch_states),
        people=people,
        credits=FakeCreditRepository(people, titles),
        collections=FakeCollectionRepository(),
        # A real fake for the same reason `people` above is one: the worker
        # lane's `DeriveService` is constructed eagerly and takes this slot.
        images=FakeImageRepository(),
        adapters=fakes.adapters,
        matcher=matcher,
        ingest=ingest,
        reconcile=ReconcileService(
            ingest=ingest,
            media_items=fakes.media_items,
            runs=runs,
            events=fakes.events,
            commit=commit,
        ),
        watch=WatchStateSyncService(
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
            FakeSuggestIndex(),
            titles,
            fakes.media_items,
            watch_states,
            result_limit=settings.search_result_limit,
        ),
        similar=SimilarityService(embeddings, neighbors, titles, commit),
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
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher",
        secret_key="0" * 32,
        **overrides,  # type: ignore[arg-type]
    )

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
        queue=_CountingQueue(),
        adapters=_Adapters(),
        events=FakeEventPublisher(),
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


async def test_the_worker_lane_requeues_abandoned_claims_once_not_every_pass(
    fakes: _Fakes,
) -> None:
    """PRD 08's "startup requeues anything left `in_progress`", and it is a
    *startup* call rather than a per-pass one.

    `JobQueue.requeue_running`'s default `older_than_seconds=0.0` requeues
    **everything** currently running, which is correct at exactly one worker
    -- so a lane that called it every poll would steal a second worker's
    live claims every five seconds. `usher work` calls it once before its
    loop for the same reason; this is the property that keeps the two
    composition roots honest with each other.

    `idle_seconds` is dialled down so several passes fit in milliseconds:
    at the shipped five seconds this case would take fifteen, and a case
    that asserted after one pass could not tell "once" from "per pass".
    """
    supervisor = _supervisor(fakes, worker_idle_seconds=0.001)
    await supervisor.start()
    await _drain(lambda: fakes.queue.claims >= 3, bound=2.0)
    await supervisor.stop()
    assert fakes.queue.requeues == 1, (
        f"the worker lane requeued {fakes.queue.requeues} times over {fakes.queue.claims} passes"
    )


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


async def _drain(until: Callable[[], bool], *, bound: float = 5.0) -> None:
    """Turn the loop until `until()` holds, bounded so a supervisor that
    never runs the lane fails the case instead of hanging the suite.

    `asyncio.wait_for` cannot bound a coroutine that never yields, which is
    why this is a deadline over `sleep(0)` rather than a timeout around one
    await -- the same reason `tests/integration/test_job_queue.py` bounds
    its claims explicitly.
    """
    deadline = time.perf_counter() + bound
    while time.perf_counter() < deadline:
        if until():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the lane never got there")


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
