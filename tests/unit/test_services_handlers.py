"""The three job handlers, and the remote-search tier only one of them uses.

`JobWorker` is a generic claim/run/park loop; these are the only place its
vocabulary meets the pipeline's. Two properties carry most of the cases:

- **a key that does not parse must be a `UsherPortError`.** `JobWorker` lets
  anything else propagate on purpose, so a `ValueError` from `uuid.UUID`
  would kill the worker instead of parking one job.
- **work that has become impossible completes rather than parks.** PRD 08
  reserves parking for work a human has to look at; an item the source
  deleted is not that.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.event_publisher import FakeEventPublisher
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.raw_payload_store import FakeRawPayloadStore
from tests.fakes.source_adapter import FakeSourceAdapter
from tests.fakes.source_repository import FakeSourceRepository
from tests.fakes.sync_run_repository import FakeSyncRunRepository
from tests.fakes.title_match_repository import FakeTitleMatchRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.enums import EnrichmentState, MatchMethod, SourceKind, TitleKind
from usher.domain.jobs import Job, JobKind
from usher.domain.source import Source
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus
from usher.domain.title import Title
from usher.ports.errors import PortDataMalformed, PortUnavailable, UsherPortError
from usher.ports.ingest import MediaItemUpsert, WatchStateWrite
from usher.ports.llm import LLMUsage
from usher.ports.metadata import MetadataCandidate
from usher.ports.source import (
    SourceAdapter,
    SourceItem,
    SourceItemKind,
    SourceWatchState,
    WatchStateUpdate,
)
from usher.services.curation import CurationReport, CurationService
from usher.services.enrich import EnrichService
from usher.services.handlers import (
    SourceBinding,
    curate_handler,
    enrich_handler,
    match_handler,
    sync_handler,
    watch_history_handler,
    watch_writeback_handler,
)
from usher.services.matching import MatchService
from usher.services.reconcile import ReconcileService
from usher.services.watch_sync import WatchStateSyncService
from usher.services.watch_write import WatchWriteService

_OBSERVED = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
_USER = uuid.UUID("0197a5b0-0000-7000-8000-0000000000ff")


async def _noop() -> None:
    return None


@pytest.fixture
def source() -> Source:
    return Source(
        kind=SourceKind.EMBY,
        name="Living Room",
        base_url="https://emby.example",
        credentials_ref="ref",
        device_id="device",
    )


@pytest.fixture
def adapter(source: Source) -> FakeSourceAdapter:
    return FakeSourceAdapter(source)


@pytest.fixture
def binding(source: Source, adapter: FakeSourceAdapter) -> SourceBinding:
    return SourceBinding(source=source, adapter=adapter)


def _resolver(binding: SourceBinding | None, seen: list[str] | None = None):  # type: ignore[no-untyped-def]
    async def resolve(external_id: str) -> SourceBinding | None:
        if seen is not None:
            seen.append(external_id)
        return binding

    return resolve


# -- enrich ---------------------------------------------------------------


async def test_the_enrich_handler_enriches_the_title_its_key_names() -> None:
    titles = FakeTitleRepository()
    title = Title(kind=TitleKind.MOVIE, tmdb_id=90000550, name="Stub", sort_name="Stub")
    await titles.add(title)
    service = EnrichService(
        titles,
        FakeEpisodeRepository(),
        FakeRawPayloadStore(),
        FakeMetadataProvider(),
        _noop,
        FakeEventPublisher(),
        queue=FakeJobQueue(),
    )
    await enrich_handler(service)(Job(kind=JobKind.ENRICH, key=str(title.id)))
    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.enrichment_state is EnrichmentState.ENRICHED


async def test_an_enrich_key_that_is_not_a_uuid_parks_rather_than_killing_the_worker() -> None:
    """`uuid.UUID("not-a-uuid")` raises a `ValueError`, and `JobWorker`
    deliberately lets anything that is not a `UsherPortError` propagate --
    "a bug in a handler is not an upstream failure". So without this
    translation one corrupted key takes the whole worker process down instead
    of parking its own job."""
    service = EnrichService(
        FakeTitleRepository(),
        FakeEpisodeRepository(),
        FakeRawPayloadStore(),
        FakeMetadataProvider(),
        _noop,
        FakeEventPublisher(),
        queue=FakeJobQueue(),
    )
    with pytest.raises(PortDataMalformed):
        await enrich_handler(service)(Job(kind=JobKind.ENRICH, key="not-a-uuid"))


# -- curate ---------------------------------------------------------------


class _RecordingCuration(CurationService):
    """A `CurationService` that records **which household** it was asked for.

    A subclass rather than a fake because there is no port between the
    handler and the service: `curate_handler`'s whole job is to turn
    `job.key` into the one argument of this one call, so what a case has to
    see is the argument. `__init__` deliberately does not call `super()` --
    every collaborator it would store is unreachable from `generate` here,
    and building the five of them would make these cases about the service.

    `tests/unit/test_composition.py` is where a *real* service runs through a
    *registered* handler, so "the handler calls generate at all" is not
    resting on this double.
    """

    def __init__(self, *, raises: UsherPortError | None = None) -> None:
        self.seen: list[uuid.UUID] = []
        self._raises = raises

    async def generate(self, user_id: uuid.UUID) -> CurationReport:
        self.seen.append(user_id)
        if self._raises is not None:
            raise self._raises
        return CurationReport(
            generation_id=uuid.UUID("0197a5b0-0000-7000-8000-0000000000c0"),
            pool_size=200,
            rows=(),
            dropped={},
            usage=LLMUsage(
                model="test/scripted-1",
                tokens_in=10,
                tokens_out=2,
                cost_usd=Decimal("0"),
                latency_ms=1,
            ),
        )


async def test_the_curate_handler_generates_for_the_household_its_key_names() -> None:
    """**The key is the household, and nothing else in this handler may
    decide which one.**

    `watch_history_handler` one section down takes a `user_id` at
    *construction*, because M4 has one user and a walk's job key is a
    source's `external_id` with no household in it. Curate is the opposite
    shape: `(kind, key)` is what makes two requests for one household buy one
    completion, so the household has to be in the key -- and a handler that
    took the composition root's default user instead would still dedup
    correctly, still park correctly, and quietly write household B's
    generation onto household A's screen.

    So the case asks for a household that is **not** the one every other
    fixture in this file uses, and asserts on the argument rather than on the
    fact that something ran.
    """
    other = uuid.UUID("0197a5b0-0000-7000-8000-0000000000bb")
    assert other != _USER, "the premise: the key names a household the root did not bind"
    service = _RecordingCuration()

    await curate_handler(service)(Job(kind=JobKind.CURATE, key=str(other)))

    assert service.seen == [other]


async def test_a_curate_key_that_is_not_a_uuid_parks_rather_than_killing_the_worker() -> None:
    """`_title_id`'s reason, for a key that is not a title id.

    `uuid.UUID("not-a-uuid")` raises a `ValueError`, and `JobWorker`
    deliberately lets anything that is not a `UsherPortError` propagate -- so
    an unconverted key takes the worker process down instead of parking its
    own job. The conversion is shared with the three title-keyed kinds rather
    than written a fourth time; what differs is the sentence, because "job
    key is not a title id" is wrong about a household.
    """
    service = _RecordingCuration()

    with pytest.raises(PortDataMalformed) as raised:
        await curate_handler(service)(Job(kind=JobKind.CURATE, key="not-a-uuid"))

    # The diagnostics, not the verdict: every kind's unparseable key produces
    # the identical exception type, and the operator reading `jobs.last_error`
    # needs to be told which of two different things the key failed to be.
    assert "user id" in str(raised.value)
    assert service.seen == [], "the key was converted after the service was called"


async def test_the_curate_handler_does_not_absorb_a_failed_generation() -> None:
    """PRD 06: *"failure is non-fatal to the screen and fatal to the job"*.

    The screen half is `CurationService`'s -- a failed generation never
    reaches `replace_for_user`, so last night's rows stand. The job half is
    this line: a handler that caught the raise would `complete()` the job,
    delete its row, and lose a generation with nothing anywhere saying so.
    Parked or backed off is the only honest outcome, and `JobWorker` can only
    decide that from an exception it is allowed to see.

    Driven with `PortDataMalformed` because it is the one `generate` raises
    for both of its own non-upstream conditions (an empty pool; a generation
    that validated to zero rows), and it is the one `JobWorker` parks on
    rather than spending four more completions.
    """
    service = _RecordingCuration(raises=PortDataMalformed("nothing to curate"))

    with pytest.raises(PortDataMalformed):
        await curate_handler(service)(Job(kind=JobKind.CURATE, key=str(_USER)))

    assert service.seen == [_USER], "the premise: the service really was reached"


# -- match ----------------------------------------------------------------


def _matcher(
    titles: FakeTitleRepository, provider: FakeMetadataProvider | None = None
) -> MatchService:
    return MatchService(titles, FakeTitleMatchRepository(titles), FakeJobQueue(), provider)


async def test_the_match_handler_attaches_what_the_remote_search_resolved(
    binding: SourceBinding, adapter: FakeSourceAdapter, source: Source
) -> None:
    titles = FakeTitleRepository()
    known = Title(
        kind=TitleKind.MOVIE, tmdb_id=90000550, name="A Film", sort_name="A Film", year=1999
    )
    await titles.add(known)
    provider = FakeMetadataProvider()
    provider.seed_candidates(
        MetadataCandidate(
            provider_id=90000550, name="A Film", year=1999, kind=TitleKind.MOVIE, popularity=9.0
        )
    )
    item = SourceItem(external_id="emby-1", name="A Film", kind=SourceItemKind.MOVIE, year=1999)
    adapter.seed(item, _OBSERVED)
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many([_upsert(source.id, "emby-1")])

    await match_handler(_matcher(titles, provider), media_items, _resolver(binding))(
        Job(kind=JobKind.MATCH, key="emby-1")
    )

    stored = await media_items.get_by_external_id(source.id, "emby-1")
    assert stored is not None
    assert stored.title_id == known.id


async def test_the_match_handler_mints_a_stub_when_the_catalog_lacks_the_title(
    binding: SourceBinding, adapter: FakeSourceAdapter, source: Source
) -> None:
    """The catalog holds 1,271,138 titles and only 291,737 carry a `tmdb_id`,
    so a confident search result the catalog does not hold is the common case
    -- the same reasoning that makes stub-on-sight load-bearing."""
    titles = FakeTitleRepository()
    provider = FakeMetadataProvider()
    provider.seed_candidates(
        MetadataCandidate(
            provider_id=680, name="A Film", year=1999, kind=TitleKind.MOVIE, popularity=9.0
        )
    )
    adapter.seed(
        SourceItem(external_id="emby-1", name="A Film", kind=SourceItemKind.MOVIE, year=1999),
        _OBSERVED,
    )
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many([_upsert(source.id, "emby-1")])

    await match_handler(_matcher(titles, provider), media_items, _resolver(binding))(
        Job(kind=JobKind.MATCH, key="emby-1")
    )

    stored = await media_items.get_by_external_id(source.id, "emby-1")
    assert stored is not None
    assert stored.title_id is not None
    minted = await titles.get_by_tmdb_id(680, TitleKind.MOVIE)
    assert minted is not None
    assert minted.enrichment_state is EnrichmentState.STUB


async def test_the_match_handler_does_nothing_for_an_item_the_source_no_longer_has(
    binding: SourceBinding, source: Source
) -> None:
    """Parking it would fill the review list with things that are simply
    gone, and a parked job needs a human to release it."""
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many([_upsert(source.id, "emby-gone")])
    await match_handler(
        _matcher(FakeTitleRepository(), FakeMetadataProvider()), media_items, _resolver(binding)
    )(Job(kind=JobKind.MATCH, key="emby-gone"))
    stored = await media_items.get_by_external_id(source.id, "emby-gone")
    assert stored is not None
    assert stored.title_id is None


async def test_the_match_handler_does_nothing_when_no_configured_source_owns_the_key() -> None:
    """`(kind, key)` is unique across sources (`usher.domain.jobs.Job`), so a
    worker cannot assume the job it claimed belongs to the source it happens
    to hold."""
    seen: list[str] = []
    await match_handler(
        _matcher(FakeTitleRepository()), FakeMediaItemRepository(), _resolver(None, seen)
    )(Job(kind=JobKind.MATCH, key="emby-1"))
    assert seen == ["emby-1"]


# -- the remote-search tier itself ----------------------------------------


async def test_an_ambiguous_search_resolves_to_nothing() -> None:
    """PRD 03 stage 5: no *confident* match means the review queue, not a
    coin flip. Two same-name, same-year candidates is what a search for a
    film and its own remake looks like."""
    provider = FakeMetadataProvider()
    provider.seed_candidates(
        MetadataCandidate(
            provider_id=1, name="A Film", year=1999, kind=TitleKind.MOVIE, popularity=90.0
        ),
        MetadataCandidate(
            provider_id=2, name="A Film", year=1999, kind=TitleKind.MOVIE, popularity=1.0
        ),
    )
    outcome = await _matcher(FakeTitleRepository(), provider).match_remote(
        SourceItem(external_id="e", name="A Film", kind=SourceItemKind.MOVIE, year=1999)
    )
    assert outcome.title_id is None
    assert outcome.method is MatchMethod.UNMATCHED


async def test_a_candidate_whose_year_is_far_off_is_not_confident() -> None:
    provider = FakeMetadataProvider()
    provider.seed_candidates(
        MetadataCandidate(
            provider_id=1, name="A Film", year=1979, kind=TitleKind.MOVIE, popularity=90.0
        )
    )
    outcome = await _matcher(FakeTitleRepository(), provider).match_remote(
        SourceItem(external_id="e", name="A Film", kind=SourceItemKind.MOVIE, year=1999)
    )
    assert outcome.title_id is None


async def test_a_year_within_one_is_still_confident() -> None:
    """PRD 03 stage 3's +/-1, applied to the remote tier too: a source and a
    provider disagreeing by one year is common and is not ambiguity."""
    titles = FakeTitleRepository()
    provider = FakeMetadataProvider()
    provider.seed_candidates(
        MetadataCandidate(
            provider_id=1, name="A Film", year=2000, kind=TitleKind.MOVIE, popularity=90.0
        )
    )
    outcome = await _matcher(titles, provider).match_remote(
        SourceItem(external_id="e", name="A Film", kind=SourceItemKind.MOVIE, year=1999)
    )
    assert outcome.title_id is not None
    assert outcome.method is MatchMethod.PROVIDER_SEARCH


async def test_the_search_is_scoped_to_the_items_own_kind() -> None:
    """26,968 TMDb ids are live in both spaces. A series candidate answering
    a movie's search resolves the item to an unrelated show -- and the search
    endpoints are different, so the scoping is also one request instead of
    two."""
    provider = FakeMetadataProvider()
    provider.seed_candidates(
        MetadataCandidate(
            provider_id=1, name="A Film", year=1999, kind=TitleKind.SERIES, popularity=90.0
        )
    )
    outcome = await _matcher(FakeTitleRepository(), provider).match_remote(
        SourceItem(external_id="e", name="A Film", kind=SourceItemKind.MOVIE, year=1999)
    )
    assert outcome.title_id is None


async def test_an_episode_is_never_remotely_searched() -> None:
    """A TMDb title search for "Kissed by Fire" is not a resolution path, and
    999,827 of this library's items are episodes."""
    provider = FakeMetadataProvider()
    provider.seed_candidates(
        MetadataCandidate(
            provider_id=1, name="Kissed by Fire", year=2013, kind=TitleKind.MOVIE, popularity=1.0
        )
    )
    outcome = await _matcher(FakeTitleRepository(), provider).match_remote(
        SourceItem(external_id="e", name="Kissed by Fire", kind=SourceItemKind.EPISODE, year=2013)
    )
    assert outcome.title_id is None
    assert provider.searches == 0


async def test_a_deployment_with_no_provider_configured_has_no_remote_tier() -> None:
    """PRD 08: "TMDb key missing -> Bootstrap Phase 3 skipped". The same
    degradation one stage over -- no key, no tier 4, and no crash."""
    outcome = await _matcher(FakeTitleRepository()).match_remote(
        SourceItem(external_id="e", name="A Film", kind=SourceItemKind.MOVIE, year=1999)
    )
    assert outcome.title_id is None
    assert outcome.method is MatchMethod.UNMATCHED


# -- watch history ---------------------------------------------------------


async def test_the_watch_history_handler_backfills_the_item_its_key_names(
    binding: SourceBinding, adapter: FakeSourceAdapter, source: Source
) -> None:
    titles = FakeTitleRepository()
    title = Title(kind=TitleKind.MOVIE, name="A Film", sort_name="A Film")
    await titles.add(title)
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many([_upsert(source.id, "emby-1", title_id=title.id)])
    # The item as well as the state: `get_watch_state` answers `None` for an
    # id the source does not hold, exactly as the port requires, so a state
    # seeded against no item is a source that has deleted it.
    adapter.seed(
        SourceItem(external_id="emby-1", name="A Film", kind=SourceItemKind.MOVIE), _OBSERVED
    )
    adapter.seed_state(
        SourceWatchState(external_id="emby-1", position_seconds=0, played=True, play_count=7)
    )
    watch_states = FakeWatchStateRepository()
    service = WatchStateSyncService(
        media_items, watch_states, FakeSyncRunRepository(), FakeJobQueue(), _noop
    )

    await watch_history_handler(service, _resolver(binding), user_id=_USER)(
        Job(kind=JobKind.WATCH_HISTORY, key="emby-1")
    )

    stored = await watch_states.get_for_title(_USER, title.id)
    assert stored is not None
    assert stored.play_count == 7


async def test_the_watch_history_handler_does_nothing_for_an_unowned_key() -> None:
    seen: list[str] = []
    service = WatchStateSyncService(
        FakeMediaItemRepository(),
        FakeWatchStateRepository(),
        FakeSyncRunRepository(),
        FakeJobQueue(),
        _noop,
    )
    await watch_history_handler(service, _resolver(None, seen), user_id=_USER)(
        Job(kind=JobKind.WATCH_HISTORY, key="emby-1")
    )
    assert seen == ["emby-1"]


# -- sync -------------------------------------------------------------------


class _RecordingReconcile(ReconcileService):
    """A `ReconcileService` that records **which source and lane** it was
    asked to walk, without touching any of its five real collaborators.

    Same shape as `_RecordingCuration` above and for the same reason: there
    is no port between `sync_handler` and the service it drives, so what a
    case has to see is the argument, and `__init__` deliberately does not
    call `super()`.
    """

    def __init__(self, log: list[str], *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[uuid.UUID, SyncRunKind]] = []
        self._log = log
        self._boom = raises

    async def reconcile(self, source: Source, kind: SyncRunKind, adapter: SourceAdapter) -> SyncRun:
        self.calls.append((source.id, kind))
        self._log.append("reconcile")
        if self._boom is not None:
            raise self._boom
        return SyncRun(source_id=source.id, kind=kind, status=SyncRunStatus.COMPLETED)


class _RecordingWatch(WatchStateSyncService):
    """`WatchStateSyncService`'s twin of `_RecordingReconcile` above."""

    def __init__(self, log: list[str], *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []
        self._log = log
        self._boom = raises

    async def sync(self, source: Source, adapter: SourceAdapter, *, user_id: uuid.UUID) -> SyncRun:
        self.calls.append((source.id, user_id))
        self._log.append("watch")
        if self._boom is not None:
            raise self._boom
        return SyncRun(
            source_id=source.id, kind=SyncRunKind.WATCH_STATE, status=SyncRunStatus.COMPLETED
        )


class _Opener:
    """`composition.open_adapter`, bound to one answer and recording who asked.

    A class rather than a closure, so a case can read `.calls` back the way
    `FakeSourceRepository.calls` already lets one read a lookup count back --
    the shape `test_the_sync_handler_completes_for_a_source_that_no_longer_
    exists` needs to prove the adapter factory was never reached at all.
    """

    def __init__(self, adapter: SourceAdapter | None) -> None:
        self._adapter = adapter
        self.calls: list[uuid.UUID] = []

    async def __call__(self, source: Source) -> SourceAdapter | None:
        self.calls.append(source.id)
        return self._adapter


async def test_the_sync_handler_walks_the_item_lane_then_the_watch_lane(
    source: Source, adapter: FakeSourceAdapter
) -> None:
    """PRD 03's ordering, restated at the handler: a watch lane that ran
    before the items existed would resolve every state against a
    `MediaItem` that does not exist yet and count it unmatched."""
    sources = FakeSourceRepository()
    await sources.add(source)
    events: list[str] = []
    reconcile = _RecordingReconcile(events)
    watch = _RecordingWatch(events)
    opener = _Opener(adapter)

    await sync_handler(sources, reconcile, watch, opener, user_id=_USER)(
        Job(kind=JobKind.SYNC, key=f"{source.id}:full")
    )

    assert events == ["reconcile", "watch"]
    assert reconcile.calls == [(source.id, SyncRunKind.FULL)]
    assert watch.calls == [(source.id, _USER)]
    assert opener.calls == [source.id]


async def test_the_lane_in_the_key_is_the_lane_reconcile_is_asked_for(
    source: Source, adapter: FakeSourceAdapter
) -> None:
    """The composite key is the only channel lane selection reaches the
    handler through -- a `delta` request and a `full` request differ in
    nothing else."""
    sources = FakeSourceRepository()
    await sources.add(source)
    events: list[str] = []
    reconcile = _RecordingReconcile(events)
    watch = _RecordingWatch(events)

    await sync_handler(sources, reconcile, watch, _Opener(adapter), user_id=_USER)(
        Job(kind=JobKind.SYNC, key=f"{source.id}:delta")
    )

    assert reconcile.calls == [(source.id, SyncRunKind.DELTA)]


async def test_the_sync_handler_closes_the_adapter_even_when_reconcile_raises(
    source: Source, adapter: FakeSourceAdapter
) -> None:
    """`aclose()` in a `finally`, the rule `usher.cli._sync` already
    documents: one adapter is one connection pool, and a walk that raises
    would otherwise leak it for the rest of the process.

    `ReconcileService.reconcile` itself "never raises a `UsherPortError`" --
    it records a `FAILED` run instead -- so the exception that reaches this
    handler in production is a genuine bug, and `JobWorker` deliberately lets
    it propagate rather than swallowing it as an upstream failure. That is
    reproduced here with a bare `RuntimeError` rather than a `UsherPortError`,
    which is also what proves the watch lane never runs after it.
    """
    sources = FakeSourceRepository()
    await sources.add(source)
    events: list[str] = []
    reconcile = _RecordingReconcile(events, raises=RuntimeError("boom"))
    watch = _RecordingWatch(events)

    with pytest.raises(RuntimeError):
        await sync_handler(sources, reconcile, watch, _Opener(adapter), user_id=_USER)(
            Job(kind=JobKind.SYNC, key=f"{source.id}:delta")
        )

    assert events == ["reconcile"], "the watch lane ran after the item lane raised"
    # The port's own `aclose` contract: afterwards every method raises
    # `PortUnavailable` rather than whatever the underlying transport would.
    with pytest.raises(PortUnavailable):
        await adapter.get_item("anything")


async def test_a_sync_key_with_no_lane_parks_rather_than_killing_the_worker() -> None:
    """`"{source_id}"` alone, with no `:lane` -- a plausible corruption from
    something that dropped the separator -- must not reach `str.partition`
    and silently resolve to an empty lane."""
    with pytest.raises(PortDataMalformed):
        await sync_handler(
            FakeSourceRepository(),
            _RecordingReconcile([]),
            _RecordingWatch([]),
            _Opener(None),
            user_id=_USER,
        )(Job(kind=JobKind.SYNC, key=str(uuid.uuid4())))


async def test_a_sync_key_whose_source_id_does_not_parse_parks_rather_than_killing_the_worker() -> (
    None
):
    """`uuid.UUID("not-a-uuid")` raises a `ValueError`, and `JobWorker`
    deliberately lets anything that is not a `UsherPortError` propagate."""
    with pytest.raises(PortDataMalformed) as raised:
        await sync_handler(
            FakeSourceRepository(),
            _RecordingReconcile([]),
            _RecordingWatch([]),
            _Opener(None),
            user_id=_USER,
        )(Job(kind=JobKind.SYNC, key="not-a-uuid:delta"))
    assert "source id" in str(raised.value)


async def test_a_sync_key_naming_watch_state_as_its_own_lane_is_malformed() -> None:
    """`SyncRunKind.WATCH_STATE` parses as a `SyncRunKind` and is still not a
    triggerable lane: the watch lane is never a thing an operator asks for on
    its own, only the second half of every triggered sync."""
    with pytest.raises(PortDataMalformed):
        await sync_handler(
            FakeSourceRepository(),
            _RecordingReconcile([]),
            _RecordingWatch([]),
            _Opener(None),
            user_id=_USER,
        )(Job(kind=JobKind.SYNC, key=f"{uuid.uuid4()}:watch_state"))


async def test_a_sync_key_naming_an_unknown_lane_is_malformed() -> None:
    with pytest.raises(PortDataMalformed):
        await sync_handler(
            FakeSourceRepository(),
            _RecordingReconcile([]),
            _RecordingWatch([]),
            _Opener(None),
            user_id=_USER,
        )(Job(kind=JobKind.SYNC, key=f"{uuid.uuid4()}:nightly"))


async def test_the_sync_handler_completes_for_a_source_that_no_longer_exists() -> None:
    """A job for work that has since become impossible completes rather than
    parks -- PRD 08 reserves parking for work a human must look at, and a
    source deleted between enqueue and claim is simply gone."""
    events: list[str] = []
    opener = _Opener(None)

    await sync_handler(
        FakeSourceRepository(),
        _RecordingReconcile(events),
        _RecordingWatch(events),
        opener,
        user_id=_USER,
    )(Job(kind=JobKind.SYNC, key=f"{uuid.uuid4()}:delta"))

    assert events == [], "no source to walk, so neither lane may run"
    assert opener.calls == [], "no source to build an adapter for"


async def test_the_sync_handler_completes_for_a_source_disabled_since_it_was_enqueued() -> None:
    """The race the route's own 409 cannot close by itself: the queue can
    hold this job behind a head-of-line-blocking walk for minutes, long
    enough for an operator to disable the source after the healthy 202 and
    before the worker ever claims the row. `SourceRegistry.resolve` already
    makes this guard for `match` and `watch_history`
    (`composition.py`); this is the same rule for the kind that reaches a
    source by id instead of asking a resolver.
    """
    sources = FakeSourceRepository()
    await sources.add(
        Source(
            kind=SourceKind.EMBY,
            name="Parked Mid-Flight",
            base_url="https://emby.example",
            credentials_ref="ref",
            device_id="device",
            enabled=False,
        )
    )
    disabled = (await sources.list_all())[0]
    events: list[str] = []
    opener = _Opener(None)

    await sync_handler(
        sources, _RecordingReconcile(events), _RecordingWatch(events), opener, user_id=_USER
    )(Job(kind=JobKind.SYNC, key=f"{disabled.id}:delta"))

    assert events == [], "a disabled source must not be walked, however it got that way"
    assert opener.calls == [], "no adapter should be built for a source the worker declines"


async def test_the_sync_handler_completes_when_the_credential_row_has_gone(source: Source) -> None:
    """`composition.open_adapter` answers `None` for exactly this and already
    logs why -- an operator with three sources needs the second and third to
    run when the first's credential has gone."""
    sources = FakeSourceRepository()
    await sources.add(source)
    events: list[str] = []
    opener = _Opener(None)

    await sync_handler(
        sources, _RecordingReconcile(events), _RecordingWatch(events), opener, user_id=_USER
    )(Job(kind=JobKind.SYNC, key=f"{source.id}:delta"))

    assert events == [], "no adapter, so neither lane may run"
    assert opener.calls == [source.id]


# -- the outbound write-back -----------------------------------------------


class _RecordingAdapter(FakeSourceAdapter):
    """`FakeSourceAdapter` plus a ledger of the writes it was asked to make.

    Every case below asserts on `pushes` rather than on `recorded(...)`,
    because the fake's own `push_watch_state` *stores* the state it was
    given -- so "the source's state is unchanged" is satisfied by a push
    that happened to write what was already there, and "the adapter was not
    called" is the claim these cases are actually making. `raises` is here
    for the two propagation cases, which need the failure to come out of
    `push_watch_state` itself rather than out of `_ready()`: an offline
    adapter raises from `get_item` first, which would pass a propagation
    assertion having never reached the write.
    """

    def __init__(self, source: Source, *, raises: BaseException | None = None) -> None:
        super().__init__(source)
        self.pushes: list[tuple[str, WatchStateUpdate]] = []
        self._raises = raises

    async def push_watch_state(self, external_id: str, state: WatchStateUpdate) -> None:
        self.pushes.append((external_id, state))
        if self._raises is not None:
            raise self._raises
        await super().push_watch_state(external_id, state)


def _write_service(
    watch_states: FakeWatchStateRepository,
    media_items: FakeMediaItemRepository,
    queue: FakeJobQueue,
) -> WatchWriteService:
    return WatchWriteService(
        watch_states=watch_states,
        media_items=media_items,
        queue=queue,
        events=FakeEventPublisher(),
        commit=_noop,
    )


async def test_a_write_back_pushes_the_state_the_row_holds_now_not_the_one_that_enqueued_it(
    source: Source,
) -> None:
    """The property that makes coalescing true rather than merely claimed.

    `(kind, key)` is unique, so five `PUT`s during one minute of playback
    are **one** row on the queue -- and the row carries no payload, so the
    handler re-reads the household's current state when it runs. Two writes
    here, and the second is what has to arrive.

    Fails against any implementation that carries the state on the job: the
    queue would hold the first write's position and the source would be told
    60 seconds after the household had reached 900. It is also what makes a
    retry idempotent, because the handler replays nothing.
    """
    adapter = _RecordingAdapter(source)
    adapter.seed(
        SourceItem(external_id="emby-1", name="A Film", kind=SourceItemKind.MOVIE), _OBSERVED
    )
    titles = FakeTitleRepository()
    title = Title(kind=TitleKind.MOVIE, name="A Film", sort_name="A Film")
    await titles.add(title)
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many([_upsert(source.id, "emby-1", title_id=title.id)])
    watch_states = FakeWatchStateRepository()
    queue = FakeJobQueue()
    service = _write_service(watch_states, media_items, queue)

    await service.set_for_title(user_id=_USER, title_id=title.id, position_seconds=60, played=False)
    await service.set_for_title(user_id=_USER, title_id=title.id, position_seconds=900, played=True)

    claimed = await queue.claim([JobKind.WATCH_WRITEBACK], limit=10)
    # The premise, and the reason the case can say anything at all: two
    # presses left one job, so what the handler is handed cannot distinguish
    # them and only the row can.
    assert [job.key for job in claimed] == ["emby-1"]

    await watch_writeback_handler(
        watch_states,
        media_items,
        _resolver(SourceBinding(source=source, adapter=adapter)),
        user_id=_USER,
    )(claimed[0])

    assert adapter.pushes == [("emby-1", WatchStateUpdate(position_seconds=900, played=True))]


async def test_a_write_back_for_an_episode_sends_the_episodes_own_row(source: Source) -> None:
    """An episode's `media_items` row carries its series' `title_id` *and*
    its `episode_id`, and `watch_states` permits exactly one -- so the pair
    has to collapse with the episode winning.

    Fails against a handler that reads `get_for_title`: it would push the
    series' progress, which on this library is one row standing in for up to
    20,000 episode files. Both rows are seeded and they disagree, so the
    assertion cannot be satisfied by reading either at random.
    """
    adapter = _RecordingAdapter(source)
    adapter.seed(
        SourceItem(external_id="emby-ep", name="An Episode", kind=SourceItemKind.EPISODE), _OBSERVED
    )
    title_id, episode_id = uuid.uuid4(), uuid.uuid4()
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many(
        [_upsert(source.id, "emby-ep", title_id=title_id, episode_id=episode_id)]
    )
    watch_states = FakeWatchStateRepository()
    await watch_states.set_from_client(
        WatchStateWrite(
            user_id=_USER, title_id=title_id, episode_id=None, position_seconds=11, played=False
        )
    )
    await watch_states.set_from_client(
        WatchStateWrite(
            user_id=_USER, title_id=None, episode_id=episode_id, position_seconds=222, played=True
        )
    )

    await watch_writeback_handler(
        watch_states,
        media_items,
        _resolver(SourceBinding(source=source, adapter=adapter)),
        user_id=_USER,
    )(Job(kind=JobKind.WATCH_WRITEBACK, key="emby-ep"))

    assert adapter.pushes == [("emby-ep", WatchStateUpdate(position_seconds=222, played=True))]


async def test_a_write_back_completes_when_no_configured_source_addresses_its_key(
    source: Source,
) -> None:
    """`(kind, key)` is unique across sources, so a worker cannot assume the
    job it claimed belongs to a server this household still has.

    Completing rather than parking: a removed server is not work a human has
    to look at. The assertion is that **nothing was written**, because "it
    did not raise" is also what a handler pushing to the wrong server
    produces.
    """
    adapter = _RecordingAdapter(source)
    seen: list[str] = []

    await watch_writeback_handler(
        FakeWatchStateRepository(), FakeMediaItemRepository(), _resolver(None, seen), user_id=_USER
    )(Job(kind=JobKind.WATCH_WRITEBACK, key="emby-1"))

    assert seen == ["emby-1"]
    assert adapter.pushes == []


async def test_a_write_back_completes_for_an_item_the_source_no_longer_has(
    source: Source,
) -> None:
    """`WatchWriteService` enqueues retracted copies on purpose -- an
    unmounted drive is the common cause and the copy usually comes back -- and
    that bargain only holds if the handler completes for one that has really
    gone.

    Fails against a handler that pushes anyway: `EmbySession.ok` raises
    `PortUnavailable` for every status at or above 400, so a write at a
    deleted item is five backed-off attempts and then a parked job, for work
    that will never become possible.
    """
    adapter = _RecordingAdapter(source)  # seeded with no items: the source has none
    title_id = uuid.uuid4()
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many([_upsert(source.id, "emby-gone", title_id=title_id)])
    watch_states = FakeWatchStateRepository()
    await watch_states.set_from_client(
        WatchStateWrite(
            user_id=_USER, title_id=title_id, episode_id=None, position_seconds=42, played=False
        )
    )

    await watch_writeback_handler(
        watch_states,
        media_items,
        _resolver(SourceBinding(source=source, adapter=adapter)),
        user_id=_USER,
    )(Job(kind=JobKind.WATCH_WRITEBACK, key="emby-gone"))

    assert adapter.pushes == []


async def test_a_write_back_with_no_local_row_sends_nothing_rather_than_zeroes(
    source: Source,
) -> None:
    """`WatchStateUpdate` has no "leave it alone" spelling, so a push
    assembled from an absent row reports position 0 and `Played: false` --
    and Emby's `UserData` route applies that body verbatim, which is the
    finding behind `Played` being named even when it is not changing.

    So the wrong implementation here does not fail loudly; it erases the
    household's progress on the server on behalf of a household that never
    wrote any.
    """
    adapter = _RecordingAdapter(source)
    adapter.seed(
        SourceItem(external_id="emby-1", name="A Film", kind=SourceItemKind.MOVIE), _OBSERVED
    )
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many([_upsert(source.id, "emby-1", title_id=uuid.uuid4())])

    await watch_writeback_handler(
        FakeWatchStateRepository(),
        media_items,
        _resolver(SourceBinding(source=source, adapter=adapter)),
        user_id=_USER,
    )(Job(kind=JobKind.WATCH_WRITEBACK, key="emby-1"))

    assert adapter.pushes == []


async def test_a_write_back_for_an_unmatched_copy_sends_nothing(source: Source) -> None:
    """`MediaItem.title_id` is deliberately nullable -- the review queue is
    where unmatched copies sit -- so "matched to nothing" is an ordinary
    state and there is no row to send."""
    adapter = _RecordingAdapter(source)
    adapter.seed(
        SourceItem(external_id="emby-1", name="A Film", kind=SourceItemKind.MOVIE), _OBSERVED
    )
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many([_upsert(source.id, "emby-1")])

    await watch_writeback_handler(
        FakeWatchStateRepository(),
        media_items,
        _resolver(SourceBinding(source=source, adapter=adapter)),
        user_id=_USER,
    )(Job(kind=JobKind.WATCH_WRITEBACK, key="emby-1"))

    assert adapter.pushes == []


@pytest.mark.parametrize(
    "failure",
    [
        PortUnavailable("the server is down"),
        PortDataMalformed("the server answered something else"),
    ],
    ids=["unavailable-backs-off", "malformed-parks"],
)
async def test_a_failed_write_back_propagates_rather_than_being_swallowed(
    source: Source, failure: UsherPortError
) -> None:
    """Nothing is caught in the handler, for the reason `curate_handler`'s
    docstring gives: `JobWorker` parks `PortDataMalformed` and backs
    everything else off, and it can only do either with an exception it is
    allowed to see.

    A handler that absorbed one would `complete()` the job, delete its row
    and lose the write silently -- which is what PRD 03's *"best effort"* is
    most often misread as licensing. Both arms, because a bare
    `except UsherPortError: return` swallows the two the worker treats
    differently and one arm alone cannot see that.

    The failure comes out of `push_watch_state` itself rather than out of an
    offline adapter's `_ready()`, so the assertion is about the write and not
    about the read in front of it -- and `pushes` records the attempt, which
    is what makes "it reached the source and failed" distinguishable from
    "it never got there".
    """
    adapter = _RecordingAdapter(source, raises=failure)
    adapter.seed(
        SourceItem(external_id="emby-1", name="A Film", kind=SourceItemKind.MOVIE), _OBSERVED
    )
    title_id = uuid.uuid4()
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many([_upsert(source.id, "emby-1", title_id=title_id)])
    watch_states = FakeWatchStateRepository()
    await watch_states.set_from_client(
        WatchStateWrite(
            user_id=_USER, title_id=title_id, episode_id=None, position_seconds=7, played=False
        )
    )

    with pytest.raises(type(failure)):
        await watch_writeback_handler(
            watch_states,
            media_items,
            _resolver(SourceBinding(source=source, adapter=adapter)),
            user_id=_USER,
        )(Job(kind=JobKind.WATCH_WRITEBACK, key="emby-1"))

    assert [key for key, _ in adapter.pushes] == ["emby-1"]


def _upsert(
    source_id: uuid.UUID,
    external_id: str,
    *,
    title_id: uuid.UUID | None = None,
    episode_id: uuid.UUID | None = None,
) -> MediaItemUpsert:
    return MediaItemUpsert(
        source_id=source_id,
        external_id=external_id,
        title_id=title_id,
        episode_id=episode_id,
        container=None,
        video_codec=None,
        audio_codec=None,
        width=None,
        height=None,
        hdr_format=None,
        audio_channels=None,
        file_size_bytes=None,
        runtime_seconds=None,
        added_at=None,
        last_seen_at=_OBSERVED,
    )
