"""PRD 03 stage 3, against port fakes. No database, no network.

Two invariants carry most of these cases. **ADR-0008:** the enrichment tier
and an enrichment failure are orthogonal -- a failed attempt records
`Title.enrichment_error` and leaves the tier exactly where it was, and every
tier comparison goes through `ENRICHMENT_RANK` because `EnrichmentState` is a
`StrEnum` and `ENRICHED > STUB` is `False`. **ADR-0016:** the provider's
response is cached verbatim, which is what lets M7 and M9 derive
`Person`/`Credit`/`Collection`/`Image` later with no second network call.
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
from usher.domain.title import WIRE_FIELD_NAMES, Title
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.jobs import JobRequest
from usher.services.enrich import EnrichService

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
    `"enriched" > "stub"` are **both** `False` -- a direct comparison does not
    downgrade anything, it simply never promotes at all (ADR-0008). The plan's
    own mutation table pointed this at a "never downgrades" case, which cannot
    see it: `ENRICHED` is the top rung, so that case passes either way."""
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
    """PRD 02: "so a second metadata provider can be added later without
    ambiguity". The service merges the provider's own provenance rather than
    replacing the stored map, so an earlier provider's claims survive."""
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
    """A payload TMDb has not filled in must not blank what the source
    already knew. This is the failure `test_enrichment_never_downgrades_a_tier`
    is really about -- the tier is structurally safe because `ENRICHED` is the
    top rung, and the *data* is what a partial payload can destroy."""
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
    """**Issue #30's larger half, and it is measured rather than inferred.**
    `genres` is in `_ENRICHABLE`, so a provider that supplies any genre at all
    replaced the whole array -- and IMDb's `Biography`, `Film-Noir`,
    `Game-Show`, `Musical`, `Short`, `Sport` and `Adult` have **no TMDb
    equivalent in either id space**, so enrichment did not re-spell them, it
    deleted them.

    Measured against the real `title.basics.tsv.gz` and the live catalog on
    2026-08-19: of 132,116 enriched titles the dump also gives genres for,
    **53,724 (40.7%) lost at least one IMDb label** -- 69,160 deletions, of
    which **11,466** are of a concept TMDb cannot express. `Film-Noir` was
    deleted 827 times and survived **0**. The control is what makes it the
    enrichment boundary and not something else: **0 of 1,021,623** skeletons
    lost a label.

    `Sci-Fi` is the distractor and it must *not* survive: TMDb's `Science
    Fiction` is the same concept in the canonical vocabulary, so keeping both
    would give one title two spellings of one thing -- exactly what the read
    side is entitled to assume no title carries.
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
    """The pre-existing rule this change must not disturb: `_changes` skips an
    empty tuple, so a payload with no genres leaves every label alone --
    including the ones TMDb *could* have expressed. 1,581 of the live
    catalog's enriched titles are in exactly that state."""
    title = await _given(titles, state=EnrichmentState.SKELETON, genres=("Sci-Fi", "Biography"))
    provider.return_partial()

    await service.enrich(title.id)

    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.genres == ("Sci-Fi", "Biography")


async def test_the_service_commits_what_it_wrote(
    service: EnrichService, titles: FakeTitleRepository, commits: list[int]
) -> None:
    """`JobWorker` completes a job and commits *after* the handler returns, so
    an uncommitted enrichment would be rolled back by the next failure in the
    same session -- and the queue would report the work done."""
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
    """ADR-0008: "failure does not consume or reset a rung on the ladder". A
    skeleton title whose enrichment failed is still a perfectly usable
    skeleton -- genres, ratings and runtime did not stop being true because
    the next attempt failed -- and a retry needs to know which tier it is
    working from.

    **Every tier, not just `SKELETON`.** Found by mutation: a failure handler
    that writes `enrichment_state=SKELETON` alongside the error is invisible
    to a case seeded with a skeleton, because the write is a no-op there --
    and `SKELETON` is exactly the value a careless handler would reach for.
    The plan's own test had that shape, and so did this one.
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
    """Swallowing it would complete the job. `JobWorker` is the only thing
    that knows `PortDataMalformed` parks immediately and everything else backs
    off, and it learns which by catching the exception."""
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
    """`JobWorker._fail` commits after `queue.fail`, but the session it
    commits is the one this service left the error on -- so an uncommitted
    error row is a job that parks with its reason recorded nowhere."""
    title = await _given(titles, state=EnrichmentState.STUB)
    provider.fail_with(PortUnavailable("TMDb is down"))
    with pytest.raises(PortUnavailable):
        await service.enrich(title.id)
    assert commits


async def test_a_successful_enrichment_clears_a_previous_error(
    service: EnrichService, titles: FakeTitleRepository
) -> None:
    """A stale `enrichment_error` on an enriched title reads as "this is
    broken" on every dashboard that renders it."""
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
    `PortDataMalformed` is what `JobWorker` parks on immediately."""
    title = await _given(titles, state=EnrichmentState.SKELETON, tmdb_id=None)
    with pytest.raises(PortDataMalformed):
        await service.enrich(title.id)


async def test_a_title_with_no_provider_id_still_records_why(
    service: EnrichService, titles: FakeTitleRepository
) -> None:
    """Otherwise the only evidence is a parked job, and PRD 02's enrichment
    dashboard reads `enrichment_error`, not the queue."""
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
    """What makes deferring Person/Credit/Collection/Image to M7 and M9
    honest: they re-derive from this without a second network call. PRD 02's
    stated purpose for `raw_payloads`, and ADR-0016."""
    title = await _given(titles, state=EnrichmentState.STUB)
    await service.enrich(title.id)
    cached = await payloads.get("tmdb", "movie", "90000550")
    assert cached is not None
    assert cached[0]["credits"]["cast"]


async def test_the_cache_key_names_the_id_space(
    service: EnrichService, titles: FakeTitleRepository, payloads: FakeRawPayloadStore
) -> None:
    """ADR-0011 in the cache: TMDb's movie and series id spaces overlap on
    26,968 ids, so a key of `(provider, reference)` alone would serve a
    series the film's cached payload."""
    movie = await _given(titles, state=EnrichmentState.STUB)
    await service.enrich(movie.id)
    assert await payloads.get("tmdb", "movie", "90000550") is not None
    assert await payloads.get("tmdb", "series", "90000550") is None


async def test_a_cached_payload_within_the_ceiling_is_not_refetched(
    service: EnrichService,
    titles: FakeTitleRepository,
    provider: FakeMetadataProvider,
) -> None:
    """TMDb's caching term is a *ceiling*, not a target. Refetching every
    title on every enrichment attempt is what turns a retry storm into a rate
    limit."""
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
    """The other half. A cache with no expiry is a catalog that never learns
    a film got a sequel, and TMDb's terms cap re-fetching at six months
    rather than forbidding it."""

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
    """The defect no port fake can see on its own, and the reason this is
    asserted directly. The mapper mints a fresh UUIDv7 per `Season`; a season
    the catalog already holds keeps the id it was inserted with, so an
    episode carrying the *minted* id names no row and fails on
    `fk_episodes_season_id_seasons` -- on the **second** enrichment, not the
    first. `IngestService._ensure_seasons` re-reads for exactly this reason
    and the plan's Task 22 does not mention it."""
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
    """Ingest creates an episode from the source's own numbers and name;
    enrichment fills the rest. Neither may blank the other's fields -- and
    the nightly walk runs after every enrichment, so the failure is a daily
    one. `upsert_episodes` owns the rule; this is the case that would notice
    if enrichment stopped relying on it."""
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
    """Not a tautology: the guard being absent means two round trips per
    movie against 94,438 of them, plus an `upsert_seasons([])` statement per
    title on a catalog that is two thirds films."""
    title = await _given(titles, state=EnrichmentState.STUB)
    episodes.reset_calls()
    await service.enrich(title.id)
    assert episodes.calls == 0


# -- the read-through loop's last step -------------------------------------


async def test_a_successful_enrichment_publishes_title_updated(
    service: EnrichService, titles: FakeTitleRepository, events: FakeEventPublisher
) -> None:
    """PRD 03's read-through loop, closed: "Completion publishes a
    `title.updated` event on a Server-Sent Events channel; clients patch in
    place. No polling on either side of the system."
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
    """A failure records `enrichment_error` and leaves the tier exactly where
    it was (ADR-0008). Telling a client "this changed" would make it refetch
    an identical stub, and it would do so on every attempt of a backoff
    schedule."""
    title = await _given(titles, state=EnrichmentState.STUB)
    provider.fail_with(PortUnavailable("TMDb is down"))
    with pytest.raises(PortUnavailable):
        await service.enrich(title.id)
    assert events.published == []


async def test_the_published_event_names_the_fields_that_changed(
    service: EnrichService, titles: FakeTitleRepository, events: FakeEventPublisher
) -> None:
    """PRD 07: "`title.updated` | Title id + changed fields | Patch in
    place." A client that had to refetch the whole title to find out what
    moved is a client polling, one request later -- so `["*"]` is the answer
    this case exists to reject."""
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
    """**The case ADR-0040's rename needed and this file did not have.**

    `title.updated`'s payload is the one place in the system where a field
    *name* travels as data rather than as a key, so no DTO, no response model
    and no OpenAPI schema constrains it -- and the rename's first commit
    published `tmdb_vote_average`, `tmdb_vote_count` and `tmdb_popularity`,
    three names that appear in no response body a client can refetch, while
    every case in this file stayed green. The sibling above is why: it asserts
    `"overview" in`, `"end_year" not in` and `"*" not in`, and all three are
    true of a payload whose rating names moved.

    **Derived from `WIRE_FIELD_NAMES` rather than naming the three**, because
    the defect is *"a domain attribute reached the wire"* and not *"these
    three did"*. A fourth entry added to that mapping is covered here on the
    same commit that adds it, with nothing to remember; three literals would
    be three literals plus whatever the fourth rename forgot.
    """
    title = await _given(titles, state=EnrichmentState.STUB)
    await service.enrich(title.id)
    fields = set(events.published[0].data["fields"])

    assert WIRE_FIELD_NAMES, "the premise: at least one field's wire name is not its own"
    leaked = fields & set(WIRE_FIELD_NAMES)
    assert not leaked, f"domain attribute names reached the wire: {sorted(leaked)}"

    # **The premise, and not decoration.** Without it the assertion above is
    # satisfied by a fixture whose provider supplies none of the renamed
    # fields at all -- an empty intersection reads identically to a correct
    # mapping. Read off the *stored* title so it is derived too: `_given`
    # seeds a stub carrying none of these, so a renamed field with a value
    # after enrichment is one this provider really did supply.
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
    """The one failure that happens *before* a `Title` is loaded, so it
    cannot even name an id. Parked rather than retried (`PortDataMalformed`),
    and silent."""
    with pytest.raises(PortDataMalformed):
        await service.enrich(uuid.uuid4())
    assert events.published == []


async def test_the_event_is_published_after_the_commit(
    titles: FakeTitleRepository,
    episodes: FakeEpisodeRepository,
    payloads: FakeRawPayloadStore,
    provider: FakeMetadataProvider,
) -> None:
    """A client patches by refetching the fields the event names, so a
    publish that preceded the commit races it to a row this transaction has
    not written.

    Asserted as an *order*, not as a read, and the difference is worth
    stating: a port fake has no transaction, so the data consequence is
    genuinely invisible here and only real Postgres can show it. The order is
    the property the transaction makes matter, and it is observable -- which
    is one more than the plan expected the unit suite to manage.

    The whole tail, not just the pair: M6 puts the index enqueue between the
    two, and asserting only "commit before publish" would let it drift back
    above the commit -- where it fingerprints the pre-enrichment text and
    writes a vector the backfill never re-claims.
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

    The ordering below is a claim about two collaborators, so it is recorded
    through one rather than through a clock -- the same shape
    `test_the_commit_happens_before_the_publish` already uses one case up.
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
    """PRD 03's stage ordering, closed: match, ingest, enrich, index.

    The wrong implementation is the absent one, and it is absent *silently*
    -- an enriched title with no index job produces no error, no log line, no
    failed job and no degraded health check. It produces a search result set
    that is quietly wrong, which is the one thing this milestone must not get
    wrong.
    """
    title = await _given(titles, state=EnrichmentState.STUB)

    await service.enrich(title.id)

    assert (await queue.depth())[JobKind.INDEX] == 1
    assert [job.key for job in queue.jobs_of(JobKind.INDEX)] == [str(title.id)]


async def test_enrichment_enqueues_index_and_derive_in_one_call(
    titles: FakeTitleRepository, service: EnrichService, queue: FakeJobQueue
) -> None:
    """Two requests, **one call**, and the call count is the assertion that
    matters.

    `JobQueue.enqueue` is a staged write -- a temp DDL, a COPY and one
    `INSERT ... SELECT ... ON CONFLICT` -- so a second `await
    self._queue.enqueue([...])` here is a second full staging cycle per
    enriched title, on the path M6 already had to fix once for exactly this
    shape of cost (`stg_jobs`' shared name was an ACCESS EXCLUSIVE lock on the
    hot path, measured at 819 ms of mutual waiting).

    **A case that only asserted both kinds were enqueued is green against the
    two-call version**, which is the version somebody writes by adding four
    lines below the existing block. So this counts the calls.
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


async def test_the_index_job_is_enqueued_at_backfill_priority(
    titles: FakeTitleRepository, service: EnrichService, queue: FakeJobQueue
) -> None:
    """Nothing a client renders depends on a search document, so this must
    never sit in front of a `match` or a demand-promoted `enrich`. It is also
    the priority the sweep uses, and `enqueue`'s `WHERE jobs.priority <
    excluded.priority` is why that matters: at the same priority the second
    producer writes nothing rather than rewriting the row.

    Fails: the `JobRequest` default (`JobPriority.NEW`), which on a first walk
    puts one background job per enriched title ahead of every match.
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
    """ADR-0008: a failed attempt records `enrichment_error` and leaves the
    tier exactly where it was. The text did not change, so the fingerprint did
    not change, so the job would find the row already current and complete
    without embedding -- one claim and one staging round trip per attempt of a
    backoff schedule.

    Parametrised over all three rungs, for the reason this file already
    parametrises its failure cases: a handler that reset the tier is invisible
    to a test seeded at that tier.
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

    A worker claiming the index job reads `titles` in a different
    transaction. Enqueued *before* the commit, the job can run against the
    pre-enrichment row: it fingerprints the old text, stores a vector of the
    old text, and -- because the fingerprint matches what it embedded --
    **stops matching the stale predicate**. A permanently stale vector the
    backfill will never re-claim, produced by the enqueue that exists to
    prevent one.

    Recorded through a collaborator, never a clock -- the same shape
    `test_the_commit_happens_before_the_publish` uses. **The data consequence
    is genuinely invisible to a port fake**, which has no transaction at all;
    `tests/integration/test_services_enrich.py` is where the row is read back.
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
    """Boundary call 5, pinned rather than left to a comment. PRD 09 asks M6
    to publish `title.updated` "rather than inventing a channel", and it is
    published three lines up already. A second one would be an event with no
    consumer, which `ports/events.py` calls out by name: "no member nothing
    emits".

    This case exists because the obvious "improvement" is to add one, and it
    would look like satisfying the roadmap.
    """
    title = await _given(titles, state=EnrichmentState.STUB)

    await service.enrich(title.id)

    assert [event.kind for event in events.published] == [ClientEventKind.TITLE_UPDATED]
