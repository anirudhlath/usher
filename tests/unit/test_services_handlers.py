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

import pytest

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.raw_payload_store import FakeRawPayloadStore
from tests.fakes.source_adapter import FakeSourceAdapter
from tests.fakes.sync_run_repository import FakeSyncRunRepository
from tests.fakes.title_match_repository import FakeTitleMatchRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.enums import EnrichmentState, MatchMethod, SourceKind, TitleKind
from usher.domain.jobs import Job, JobKind
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.errors import PortDataMalformed
from usher.ports.ingest import MediaItemUpsert
from usher.ports.metadata import MetadataCandidate
from usher.ports.source import SourceItem, SourceItemKind, SourceWatchState
from usher.services.enrich import EnrichService
from usher.services.handlers import (
    SourceBinding,
    enrich_handler,
    match_handler,
    watch_history_handler,
)
from usher.services.matching import MatchService
from usher.services.watch_sync import WatchStateSyncService

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
        titles, FakeEpisodeRepository(), FakeRawPayloadStore(), FakeMetadataProvider(), _noop
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
    )
    with pytest.raises(PortDataMalformed):
        await enrich_handler(service)(Job(kind=JobKind.ENRICH, key="not-a-uuid"))


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


def _upsert(
    source_id: uuid.UUID, external_id: str, *, title_id: uuid.UUID | None = None
) -> MediaItemUpsert:
    return MediaItemUpsert(
        source_id=source_id,
        external_id=external_id,
        title_id=title_id,
        episode_id=None,
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
