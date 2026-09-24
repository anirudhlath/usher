"""PRD 03 stage 3, against port fakes.

No database, no network.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.event_publisher import FakeEventPublisher
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.raw_payload_store import FakeRawPayloadStore
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.rows import BuiltRow, DisplayHint, RowCard, RowFamily
from usher.domain.title import WIRE_FIELD_NAMES, Title
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.jobs import JobRequest
from usher.services.enrich import EnrichService
from usher.services.rows.cache import RowCache

_MOVIE_TMDB_ID = 90000550
_SERIES_TMDB_ID = 90001399


@pytest.fixture
def titles() -> FakeTitleRepository:
    return FakeTitleRepository()


@pytest.fixture
def episodes() -> FakeEpisodeRepository:
    return FakeEpisodeRepository()


@pytest.fixture
def payloads() -> FakeRawPayloadStore:
    return FakeRawPayloadStore()


@pytest.fixture
def provider() -> FakeMetadataProvider:
    return FakeMetadataProvider()


@pytest.fixture
def commits() -> list[int]:
    return []


@pytest.fixture
def events() -> FakeEventPublisher:
    return FakeEventPublisher()


@pytest.fixture
def queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def service(
    titles: FakeTitleRepository,
    episodes: FakeEpisodeRepository,
    payloads: FakeRawPayloadStore,
    provider: FakeMetadataProvider,
    commits: list[int],
    events: FakeEventPublisher,
    queue: FakeJobQueue,
) -> EnrichService:
    async def commit() -> None:
        commits.append(1)

    return EnrichService(titles, episodes, payloads, provider, commit, events, queue=queue)


async def _given(
    titles: FakeTitleRepository,
    *,
    state: EnrichmentState,
    tmdb_id: int | None = _MOVIE_TMDB_ID,
    kind: TitleKind = TitleKind.MOVIE,
    **rest: Any,
) -> Title:
    title = Title(
        kind=kind,
        tmdb_id=tmdb_id,
        name="From the source",
        sort_name="From the source",
        enrichment_state=state,
        **rest,
    )
    await titles.add(title)
    return title


# -- the happy path --------------------------------------------------------


async def test_enriching_promotes_a_stub_to_enriched(
    service: EnrichService, titles: FakeTitleRepository
) -> None:
    title = await _given(titles, state=EnrichmentState.STUB)
    await service.enrich(title.id)
    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.enrichment_state is EnrichmentState.ENRICHED
    assert stored.enriched_at is not None
    assert stored.overview


async def test_enriching_promotes_a_skeleton_to_enriched(
    service: EnrichService, titles: FakeTitleRepository
) -> None:
    """The case that actually catches `if new_state > title.enrichment_state`.

    `EnrichmentState` is a `StrEnum`, so `"enriched" > "skeleton"` and
    `"enriched" > "stub"` are both `False`: a direct comparison never downgrades, it
    simply never promotes. A "never downgrades" case cannot see that, because
    `ENRICHED` is the top rung and passes either way.
    """
    title = await _given(titles, state=EnrichmentState.SKELETON)
    await service.enrich(title.id)
    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.enrichment_state is EnrichmentState.ENRICHED


async def test_the_provider_fills_in_what_the_source_only_guessed_at(
    service: EnrichService, titles: FakeTitleRepository
) -> None:
    title = await _given(titles, state=EnrichmentState.STUB)
    await service.enrich(title.id)
    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.name == "A Film"
    assert stored.year == 1988
    assert stored.genres == ("Drama", "Thriller")
    assert stored.tmdb_vote_average == 8.4


async def test_field_provenance_records_which_provider_supplied_what(
    service: EnrichService, titles: FakeTitleRepository
) -> None:
    """PRD 02: "`field_provenance` records which provider supplied each field".

    The service merges the provider's own provenance rather than replacing the stored
    map, so an earlier provider's claims survive.
    """
    title = await _given(
        titles, state=EnrichmentState.SKELETON, field_provenance={"imdb_id": "imdb"}
    )
    await service.enrich(title.id)
    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.field_provenance["imdb_id"] == "imdb"
    assert stored.field_provenance["overview"] == "tmdb"


async def test_a_field_the_provider_did_not_supply_is_left_alone(
    service: EnrichService, titles: FakeTitleRepository, provider: FakeMetadataProvider
) -> None:
    """A payload TMDb has not filled in must not blank what the source already knew.

    The tier is structurally safe because `ENRICHED` is the top rung; the *data* is
    what a partial payload can destroy.
    """
    title = await _given(titles, state=EnrichmentState.STUB, overview="What the source said")
    provider.return_partial()
    await service.enrich(title.id)
    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.overview == "What the source said"
    assert stored.enrichment_state is EnrichmentState.ENRICHED


async def test_a_genre_the_provider_s_vocabulary_cannot_express_survives_enrichment(
    service: EnrichService, titles: FakeTitleRepository
) -> None:
    """A genre TMDb cannot express survives enrichment instead of being deleted.

    `genres` is in `_ENRICHABLE`, so a provider supplying any genre at all replaces the
    whole array -- and IMDb labels like `Film-Noir` have no TMDb equivalent in either id
    space, so enrichment does not re-spell them, it deletes them.

    `Sci-Fi` is the distractor and must *not* survive: TMDb's `Science Fiction` is the
    same concept in the canonical vocabulary, so keeping both would give one title two
    spellings of one thing.
    """
    title = await _given(
        titles,
        state=EnrichmentState.SKELETON,
        genres=("Biography", "Film-Noir", "Sci-Fi"),
    )

    await service.enrich(title.id)

    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.genres == ("Drama", "Thriller", "Biography", "Film-Noir")


async def test_a_provider_that_supplied_no_genre_at_all_still_blanks_nothing(
    service: EnrichService, titles: FakeTitleRepository, provider: FakeMetadataProvider
) -> None:
    """The pre-existing rule this must not disturb.

    `_changes` skips an empty tuple, so a payload with no genres leaves every label
    alone -- including the ones TMDb could have expressed.
    """
    title = await _given(titles, state=EnrichmentState.SKELETON, genres=("Sci-Fi", "Biography"))
    provider.return_partial()

    await service.enrich(title.id)

    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.genres == ("Sci-Fi", "Biography")


async def test_the_service_commits_what_it_wrote(
    service: EnrichService, titles: FakeTitleRepository, commits: list[int]
) -> None:
    """`JobWorker` completes a job and commits *after* the handler returns.

    An uncommitted enrichment would be rolled back by the next failure in the same
    session, and the queue would report the work done.
    """
    title = await _given(titles, state=EnrichmentState.STUB)
    await service.enrich(title.id)
    assert commits


# -- failure costs an error string, not a tier ----------------------------


@pytest.mark.parametrize("tier", list(EnrichmentState))
async def test_a_failed_enrichment_records_the_error_and_keeps_the_tier(
    service: EnrichService,
    titles: FakeTitleRepository,
    provider: FakeMetadataProvider,
    tier: EnrichmentState,
) -> None:
    """Failure does not consume or reset a rung on the ladder.

    A skeleton title whose enrichment failed is still a usable skeleton, and a retry
    needs to know which tier it is working from.

    Every tier, not just `SKELETON`: a failure handler that writes
    `enrichment_state=SKELETON` alongside the error is a no-op against a skeleton
    seed, and `SKELETON` is exactly the value a careless handler reaches for.
    """
    title = await _given(titles, state=tier)
    provider.fail_with(PortUnavailable("TMDb is down"))
    with pytest.raises(PortUnavailable):
        await service.enrich(title.id)
    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.enrichment_state is tier
    assert stored.enrichment_error == "TMDb is down"


async def test_a_failure_re_raises_so_the_worker_decides_backoff_or_park(
    service: EnrichService, titles: FakeTitleRepository, provider: FakeMetadataProvider
) -> None:
    """Swallowing it would complete the job.

    `JobWorker` is the only thing that knows `PortDataMalformed` parks immediately and
    everything else backs off, and it learns which by catching the exception.
    """
    title = await _given(titles, state=EnrichmentState.STUB)
    provider.fail_with(PortDataMalformed("TMDb has no entity at this reference"))
    with pytest.raises(PortDataMalformed):
        await service.enrich(title.id)


async def test_a_failed_enrichment_is_committed_before_it_re_raises(
    service: EnrichService,
    titles: FakeTitleRepository,
    provider: FakeMetadataProvider,
    commits: list[int],
) -> None:
    """`JobWorker._fail` commits the session this service left the error on.

    An uncommitted error row is a job that parks with its reason recorded nowhere.
    """
    title = await _given(titles, state=EnrichmentState.STUB)
    provider.fail_with(PortUnavailable("TMDb is down"))
    with pytest.raises(PortUnavailable):
        await service.enrich(title.id)
    assert commits


async def test_a_successful_enrichment_clears_a_previous_error(
    service: EnrichService, titles: FakeTitleRepository
) -> None:
    """A stale `enrichment_error` on an enriched title reads as "this is broken"."""
    title = await _given(titles, state=EnrichmentState.SKELETON, enrichment_error="TMDb is down")
    await service.enrich(title.id)
    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.enrichment_error is None


async def test_a_title_that_does_not_exist_parks_rather_than_retrying(
    service: EnrichService,
) -> None:
    """A job whose key names a deleted title can never succeed."""
    with pytest.raises(PortDataMalformed):
        await service.enrich(uuid.uuid4())


async def test_a_title_with_no_provider_id_parks_rather_than_retrying(
    service: EnrichService, titles: FakeTitleRepository
) -> None:
    """There is nothing to fetch and no amount of waiting changes that.

    `PortDataMalformed` is what `JobWorker` parks on immediately.
    """
    title = await _given(titles, state=EnrichmentState.SKELETON, tmdb_id=None)
    with pytest.raises(PortDataMalformed):
        await service.enrich(title.id)


async def test_a_title_with_no_provider_id_still_records_why(
    service: EnrichService, titles: FakeTitleRepository
) -> None:
    """Otherwise the only evidence is a parked job.

    The enrichment dashboard reads `enrichment_error`, not the queue.
    """
    title = await _given(titles, state=EnrichmentState.SKELETON, tmdb_id=None)
    with pytest.raises(PortDataMalformed):
        await service.enrich(title.id)
    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.enrichment_error is not None
    assert stored.enrichment_state is EnrichmentState.SKELETON


# -- the payload cache -----------------------------------------------------


async def test_the_provider_payload_is_cached_verbatim(
    service: EnrichService, titles: FakeTitleRepository, payloads: FakeRawPayloadStore
) -> None:
    """The payload is cached so later derivations need no second network call.

    That is `raw_payloads`' stated purpose: Person, Credit, Collection and Image all
    re-derive from what is stored here.
    """
    title = await _given(titles, state=EnrichmentState.STUB)
    await service.enrich(title.id)
    cached = await payloads.get("tmdb", "movie", "90000550")
    assert cached is not None
    assert cached[0]["credits"]["cast"]


async def test_the_cache_key_names_the_id_space(
    service: EnrichService, titles: FakeTitleRepository, payloads: FakeRawPayloadStore
) -> None:
    """The cache key carries the kind, because the two id spaces overlap.

    A key of `(provider, reference)` alone would serve a series the film's payload.
    """
    movie = await _given(titles, state=EnrichmentState.STUB)
    await service.enrich(movie.id)
    assert await payloads.get("tmdb", "movie", "90000550") is not None
    assert await payloads.get("tmdb", "series", "90000550") is None


async def test_a_cached_payload_within_the_ceiling_is_not_refetched(
    service: EnrichService,
    titles: FakeTitleRepository,
    provider: FakeMetadataProvider,
) -> None:
    """TMDb's caching term is a *ceiling*, not a target.

    Refetching every title on every enrichment attempt is what turns a retry storm into
    a rate limit.
    """
    title = await _given(titles, state=EnrichmentState.STUB)
    await service.enrich(title.id)
    provider.reset_calls()
    await service.enrich(title.id)
    assert provider.fetches == 0


async def test_a_payload_older_than_the_ceiling_is_refetched(
    titles: FakeTitleRepository,
    episodes: FakeEpisodeRepository,
    payloads: FakeRawPayloadStore,
    provider: FakeMetadataProvider,
) -> None:
    """The other half: a cached payload does expire.

    A cache with no expiry is a catalog that never learns a film got a sequel, and
    TMDb's terms cap re-fetching rather than forbidding it.
    """

    async def commit() -> None:
        return None

    service = EnrichService(
        titles,
        episodes,
        payloads,
        provider,
        commit,
        FakeEventPublisher(),
        queue=FakeJobQueue(),
        cache_max_age_days=1,
        now=lambda: datetime.now(UTC) + timedelta(days=2),
    )
    title = await _given(titles, state=EnrichmentState.STUB)
    await payloads.put("tmdb", "movie", "90000550", {"id": 90000550, "title": "stale"})
    await service.enrich(title.id)
    assert provider.fetches == 1


# -- the hierarchy ---------------------------------------------------------


async def test_a_series_gets_its_seasons_and_episodes(
    service: EnrichService, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    title = await _given(
        titles, state=EnrichmentState.STUB, kind=TitleKind.SERIES, tmdb_id=_SERIES_TMDB_ID
    )
    await service.enrich(title.id)
    seasons, eps = await episodes.list_for_title(title.id)
    assert seasons and eps


async def test_every_episode_lands_on_the_season_row_the_store_actually_holds(
    service: EnrichService, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """An episode has to carry the season's stored id, not the mapper's minted one.

    The mapper mints a fresh UUIDv7 per `Season`, while a season the catalog already
    holds keeps the id it was inserted with -- so an episode carrying the minted id
    names no row and fails on `fk_episodes_season_id_seasons`, on the *second*
    enrichment rather than the first.
    """
    title = await _given(
        titles, state=EnrichmentState.STUB, kind=TitleKind.SERIES, tmdb_id=_SERIES_TMDB_ID
    )
    await service.enrich(title.id)
    await service.enrich(title.id)
    seasons, eps = await episodes.list_for_title(title.id)
    by_id = {one.id for one in seasons}
    assert eps
    assert {one.season_id for one in eps} <= by_id


async def test_enriching_a_series_does_not_blank_an_episode_a_source_named(
    service: EnrichService, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """Ingest creates an episode from the source's numbers and name; enrichment fills the rest.

    Neither may blank the other's fields, and the nightly walk runs after every
    enrichment, so the failure would be a daily one. `upsert_episodes` owns the rule
    and this is the case that notices if enrichment stops relying on it.
    """
    from usher.domain.episode import Episode, Season

    title = await _given(
        titles, state=EnrichmentState.STUB, kind=TitleKind.SERIES, tmdb_id=_SERIES_TMDB_ID
    )
    season = Season(title_id=title.id, season_number=1)
    await episodes.upsert_seasons([season])
    await episodes.upsert_episodes(
        [
            Episode(
                title_id=title.id,
                season_id=season.id,
                season_number=1,
                episode_number=1,
                name="What the source called it",
                overview="Only the source knows this",
            )
        ]
    )
    await service.enrich(title.id)
    _, eps = await episodes.list_for_title(title.id)
    first = next(one for one in eps if (one.season_number, one.episode_number) == (1, 1))
    assert first.name == "First"
    assert first.overview == "Only the source knows this"


async def test_a_movie_writes_no_seasons_or_episodes(
    service: EnrichService, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """A movie touches neither season nor episode port.

    Without the guard that is two round trips and an `upsert_seasons([])` statement per
    film, on a catalog that is mostly films.
    """
    title = await _given(titles, state=EnrichmentState.STUB)
    episodes.reset_calls()
    await service.enrich(title.id)
    assert episodes.calls == 0


# -- the read-through loop's last step -------------------------------------


async def test_a_successful_enrichment_publishes_title_updated(
    service: EnrichService, titles: FakeTitleRepository, events: FakeEventPublisher
) -> None:
    """PRD 03's read-through loop, closed.

    "Completion publishes a `title.updated` event on a Server-Sent Events
    channel; clients patch in place."
    """
    title = await _given(titles, state=EnrichmentState.STUB)
    await service.enrich(title.id)
    assert [(event.kind, event.title_id) for event in events.published] == [
        (ClientEventKind.TITLE_UPDATED, title.id)
    ]


async def test_a_failed_enrichment_publishes_nothing(
    service: EnrichService,
    titles: FakeTitleRepository,
    provider: FakeMetadataProvider,
    events: FakeEventPublisher,
) -> None:
    """A failure records `enrichment_error` and leaves the tier where it was.

    Telling a client "this changed" would make it refetch an identical stub, once per
    attempt of a backoff schedule.
    """
    title = await _given(titles, state=EnrichmentState.STUB)
    provider.fail_with(PortUnavailable("TMDb is down"))
    with pytest.raises(PortUnavailable):
        await service.enrich(title.id)
    assert events.published == []


# -- and the screens that were already holding the title --------------------


def _cache_holding(title: Title) -> tuple[RowCache, uuid.UUID]:
    """A row cache with one household's shelf already built from `title`.

    The card carries the *pre*-enrichment name and no artwork, which is the
    state this whole mechanism is about: a `because-you-watched` shelf has a
    six-hour TTL, so a title enriched inside that window keeps rendering under
    its skeleton name with a "No artwork on record" placeholder until the TTL
    runs out.
    """
    cache = RowCache(clock=lambda: datetime.now(UTC))
    user_id = uuid.uuid4()
    row = BuiltRow(
        slug="because-you-watched",
        title="Because You Watched",
        family=RowFamily.SIMILARITY,
        display_hint=DisplayHint.PORTRAIT,
        ttl=timedelta(hours=6),
        cards=(
            RowCard(
                title_id=title.id,
                kind=TitleKind.MOVIE,
                name="From the source",
                enrichment_state=EnrichmentState.SKELETON,
            ),
        ),
    )
    cache.put_row(user_id, row.slug, row, ttl=row.ttl)
    cache.put_screen(user_id, (row,), ttl=timedelta(seconds=30))
    return cache, user_id


async def test_a_successful_enrichment_drops_the_cached_rows_holding_the_title(
    titles: FakeTitleRepository,
    episodes: FakeEpisodeRepository,
    payloads: FakeRawPayloadStore,
    provider: FakeMetadataProvider,
    events: FakeEventPublisher,
    queue: FakeJobQueue,
) -> None:
    """`title.updated` is a statement to a *client*, and the console does not refetch.

    So the frame cannot be what repairs a shelf built before the artwork landed: the
    cache this process serves from has to be told as well.
    """
    title = await _given(titles, state=EnrichmentState.SKELETON)
    cache, user_id = _cache_holding(title)

    async def commit() -> None:
        return None

    service = EnrichService(
        titles, episodes, payloads, provider, commit, events, queue=queue, cache=cache
    )
    assert cache.get_row(user_id, "because-you-watched") is not None, (
        "the premise: the shelf is cached before the enrichment runs"
    )

    await service.enrich(title.id)

    assert cache.get_row(user_id, "because-you-watched") is None
    assert cache.get_screen(user_id) is None


async def test_a_failed_enrichment_drops_no_cached_row(
    titles: FakeTitleRepository,
    episodes: FakeEpisodeRepository,
    payloads: FakeRawPayloadStore,
    provider: FakeMetadataProvider,
    events: FakeEventPublisher,
    queue: FakeJobQueue,
) -> None:
    """A failure leaves the tier where it was, so nothing on the card moved.

    The shelf is still correct, and dropping it would make a provider outage a cache
    flush on every attempt of a backoff schedule.
    """
    title = await _given(titles, state=EnrichmentState.STUB)
    cache, user_id = _cache_holding(title)
    provider.fail_with(PortUnavailable("TMDb is down"))

    async def commit() -> None:
        return None

    service = EnrichService(
        titles, episodes, payloads, provider, commit, events, queue=queue, cache=cache
    )
    with pytest.raises(PortUnavailable):
        await service.enrich(title.id)

    assert cache.get_row(user_id, "because-you-watched") is not None
    assert cache.get_screen(user_id) is not None


async def test_a_composition_root_that_serves_no_screens_passes_no_cache(
    service: EnrichService, titles: FakeTitleRepository
) -> None:
    """`None` is `usher work` and `usher sync`: composition roots that compose no screens.

    A service holding a cache nobody serves from would invalidate a dict with no
    reader. The `service` fixture is built without one, so this asserts the default arm
    enriches rather than raising on an absent collaborator.
    """
    title = await _given(titles, state=EnrichmentState.SKELETON)

    await service.enrich(title.id)

    assert (await titles.get(title.id)) is not None


async def test_the_published_event_names_the_fields_that_changed(
    service: EnrichService, titles: FakeTitleRepository, events: FakeEventPublisher
) -> None:
    """The frame names the changed fields, so a client can patch in place.

    A client that had to refetch the whole title to find out what moved is a client
    polling, one request later, so `["*"]` is the answer this case rejects.
    """
    title = await _given(titles, state=EnrichmentState.STUB)
    await service.enrich(title.id)
    fields = events.published[0].data["fields"]
    assert "enrichment_state" in fields
    assert "overview" in fields
    assert "*" not in fields
    # Only what the provider actually supplied. A field it left `None` is
    # "this response did not say", and naming it would send a client to
    # refetch a value that did not move.
    assert "end_year" not in fields


async def test_no_domain_only_field_name_reaches_the_wire(
    service: EnrichService, titles: FakeTitleRepository, events: FakeEventPublisher
) -> None:
    """A changed field travels under its wire name, never its domain attribute name.

    `title.updated`'s payload is the one place where a field *name* travels as data
    rather than as a key, so no DTO, response model or OpenAPI schema constrains it: a
    domain attribute published here names nothing a client can refetch.

    Derived from `WIRE_FIELD_NAMES` rather than naming the renamed fields, because the
    defect is "a domain attribute reached the wire" and a new entry in that mapping is
    then covered by the commit that adds it.
    """
    title = await _given(titles, state=EnrichmentState.STUB)
    await service.enrich(title.id)
    fields = set(events.published[0].data["fields"])

    assert WIRE_FIELD_NAMES, "the premise: at least one field's wire name is not its own"
    leaked = fields & set(WIRE_FIELD_NAMES)
    assert not leaked, f"domain attribute names reached the wire: {sorted(leaked)}"

    # **The premise, and not decoration.** Without it the assertion above is satisfied
    # by a fixture whose provider supplies none of the renamed fields at all -- an empty
    # intersection reads identically to a correct mapping.
    stored = await titles.get(title.id)
    assert stored is not None
    moved = {field for field in WIRE_FIELD_NAMES if getattr(stored, field) is not None}
    assert moved, "the premise: this fixture's provider really does supply a renamed field"
    assert {WIRE_FIELD_NAMES[field] for field in moved} <= fields, (
        "a renamed field the provider supplied is named on the wire under neither spelling"
    )


async def test_a_title_that_does_not_exist_publishes_nothing(
    service: EnrichService, events: FakeEventPublisher
) -> None:
    """The one failure that happens before a `Title` is loaded, so it names no id.

    Parked rather than retried, and silent.
    """
    with pytest.raises(PortDataMalformed):
        await service.enrich(uuid.uuid4())
    assert events.published == []


async def test_the_event_is_published_after_the_commit(
    titles: FakeTitleRepository,
    episodes: FakeEpisodeRepository,
    payloads: FakeRawPayloadStore,
    provider: FakeMetadataProvider,
) -> None:
    """A client patches by refetching the fields the event names.

    A publish that preceded the commit races the client to a row this transaction has
    not written. Asserted as an order rather than as a read: a port fake has no
    transaction, so only real Postgres can show the data consequence.

    The whole tail, not just the pair -- the index enqueue sits between the two, and
    asserting only "commit before publish" would let it drift above the commit, where
    it fingerprints pre-enrichment text and writes a vector the backfill never
    re-claims.
    """
    order: list[str] = []

    async def commit() -> None:
        order.append("commit")

    class _Recording(EventPublisher):
        async def publish(self, event: ClientEvent) -> None:
            order.append("publish")

    service = EnrichService(
        titles, episodes, payloads, provider, commit, _Recording(), queue=_RecordingQueue(order)
    )
    title = await _given(titles, state=EnrichmentState.STUB)
    await service.enrich(title.id)
    assert order == ["commit", "enqueue", "publish"]


class _RecordingQueue(FakeJobQueue):
    """A `FakeJobQueue` that also records *when* it was written to.

    The ordering below is a claim about two collaborators, so it is recorded through
    one rather than through a clock.
    """

    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    async def enqueue(self, requests: Sequence[JobRequest]) -> int:
        self._order.append("enqueue")
        return await super().enqueue(requests)


async def test_a_finished_enrichment_enqueues_one_index_job(
    titles: FakeTitleRepository, service: EnrichService, queue: FakeJobQueue
) -> None:
    """The stage ordering, closed: match, ingest, enrich, index.

    The wrong implementation is the absent one, and it is absent silently: an enriched
    title with no index job produces no error, no log line, no failed job and no
    degraded health check, only a search result set that is quietly wrong.
    """
    title = await _given(titles, state=EnrichmentState.STUB)

    await service.enrich(title.id)

    assert (await queue.depth())[JobKind.INDEX] == 1
    assert [job.key for job in queue.jobs_of(JobKind.INDEX)] == [str(title.id)]


async def test_enrichment_enqueues_index_and_derive_in_one_call(
    titles: FakeTitleRepository, service: EnrichService, queue: FakeJobQueue
) -> None:
    """Two requests, one call, and the call count is the assertion that matters.

    `JobQueue.enqueue` is a staged write -- a temp DDL, a COPY and one
    `INSERT ... SELECT ... ON CONFLICT` -- so a second call here is a second full
    staging cycle per enriched title, and the staging table is a lock on the hot path.

    A case that only asserted both kinds were enqueued is green against the two-call
    version, which is the version somebody writes by adding four lines below the
    existing block.
    """
    title = await _given(titles, state=EnrichmentState.STUB)
    calls: list[int] = []
    original = queue.enqueue

    async def counting(requests: Sequence[JobRequest]) -> int:
        calls.append(len(requests))
        return await original(requests)

    queue.enqueue = counting  # type: ignore[method-assign]

    await service.enrich(title.id)

    assert calls == [2], "one call carrying two requests, not two calls"
    assert (await queue.depth())[JobKind.DERIVE] == 1
    assert [job.key for job in queue.jobs_of(JobKind.DERIVE)] == [str(title.id)]
    assert queue.jobs_of(JobKind.DERIVE)[0].priority == JobPriority.BACKFILL


@pytest.mark.parametrize("rung", [JobPriority.DEMAND, JobPriority.VISIBLE])
async def test_a_demand_enrichment_carries_its_follow_ups_to_its_own_rung(
    titles: FakeTitleRepository,
    service: EnrichService,
    queue: FakeJobQueue,
    rung: JobPriority,
) -> None:
    """A demand-priority enrichment enqueues its `DERIVE` at the same rung.

    `DERIVE` is what writes `images`, so at the sweep's priority a title a client is
    looking at now gets its text promptly and its artwork whenever the sweep arrives.

    Parametrised over both demand rungs rather than `DEMAND` alone: they reach the
    enqueue through one expression, and a spelling that hard-codes `DEMAND` answers
    correctly for a client's *open* while leaving every row it merely scrolled past
    behind the sweep.
    """
    title = await _given(titles, state=EnrichmentState.STUB)

    await service.enrich(title.id, priority=rung)

    assert queue.jobs_of(JobKind.DERIVE)[0].priority == rung
    assert queue.jobs_of(JobKind.INDEX)[0].priority == rung


async def test_an_ingested_titles_follow_ups_stay_at_backfill(
    titles: FakeTitleRepository, service: EnrichService, queue: FakeJobQueue
) -> None:
    """Inherited priority is clamped rather than passed straight through.

    `IngestService` enqueues every newly seen title's `enrich` at `JobPriority.NEW`, so
    an unclamped `priority=job.priority` would put one `derive` *and* one `index` per
    ingested title at `NEW`, ahead of a `match` queue hundreds of thousands deep on a
    first bootstrap.

    `NEW` is the rung that makes this observable: a `max()` and a `min()` disagree here
    and agree on the demand rungs above.
    """
    title = await _given(titles, state=EnrichmentState.STUB)

    await service.enrich(title.id, priority=JobPriority.NEW)

    assert queue.jobs_of(JobKind.DERIVE)[0].priority == JobPriority.BACKFILL
    assert queue.jobs_of(JobKind.INDEX)[0].priority == JobPriority.BACKFILL


async def test_the_index_job_is_enqueued_at_backfill_priority(
    titles: FakeTitleRepository, service: EnrichService, queue: FakeJobQueue
) -> None:
    """Nothing a client renders depends on a search document.

    So this must never sit in front of a `match` or a demand-promoted `enrich`. It is
    also the priority the sweep uses, and `enqueue`'s `WHERE jobs.priority <
    excluded.priority` means the second producer at that rung writes nothing rather
    than rewriting the row. The `JobRequest` default would fail here.
    """
    title = await _given(titles, state=EnrichmentState.STUB)

    await service.enrich(title.id)

    assert queue.jobs_of(JobKind.INDEX)[0].priority == JobPriority.BACKFILL


@pytest.mark.parametrize(
    "state", [EnrichmentState.SKELETON, EnrichmentState.STUB, EnrichmentState.ENRICHED]
)
async def test_a_failed_enrichment_enqueues_nothing(
    titles: FakeTitleRepository,
    service: EnrichService,
    provider: FakeMetadataProvider,
    queue: FakeJobQueue,
    state: EnrichmentState,
) -> None:
    """A failed attempt enqueues no index job.

    The text did not change, so the fingerprint did not change, so the job would find
    the row already current and complete without embedding -- one claim and one staging
    round trip per attempt of a backoff schedule.

    Parametrised over all three rungs, because a handler that reset the tier is
    invisible to a case seeded at that tier.
    """
    title = await _given(titles, state=state)
    provider.fail_with(PortUnavailable("TMDb is down"))

    with pytest.raises(PortUnavailable):
        await service.enrich(title.id)

    assert (await queue.depth())[JobKind.INDEX] == 0


async def test_the_enqueue_happens_after_the_commit(
    titles: FakeTitleRepository,
    episodes: FakeEpisodeRepository,
    payloads: FakeRawPayloadStore,
    provider: FakeMetadataProvider,
) -> None:
    """The one ordering here with a wrong answer and no error attached.

    A worker claiming the index job reads `titles` in a different transaction. Enqueued
    *before* the commit it can run against the pre-enrichment row: it fingerprints the
    old text, stores a vector of it, and -- because the fingerprint matches what it
    embedded -- stops matching the stale predicate, leaving a vector the backfill never
    re-claims.

    Recorded through a collaborator, never a clock. The data consequence is invisible
    to a port fake, which has no transaction; the integration file reads the row back.
    """
    order: list[str] = []

    async def commit() -> None:
        order.append("commit")

    service = EnrichService(
        titles,
        episodes,
        payloads,
        provider,
        commit,
        FakeEventPublisher(),
        queue=_RecordingQueue(order),
    )
    title = await _given(titles, state=EnrichmentState.STUB)

    await service.enrich(title.id)

    assert order == ["commit", "enqueue"]


async def test_enrichment_publishes_no_second_event_for_the_index(
    titles: FakeTitleRepository, service: EnrichService, events: FakeEventPublisher
) -> None:
    """No second event kind: `title.updated` is the channel, and it is already published.

    A second one would be an event with no consumer, which `ports/events.py` refuses by
    name. This case exists because adding one is the obvious "improvement".
    """
    title = await _given(titles, state=EnrichmentState.STUB)

    await service.enrich(title.id)

    assert [event.kind for event in events.published] == [ClientEventKind.TITLE_UPDATED]
