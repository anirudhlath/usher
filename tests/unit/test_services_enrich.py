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
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.raw_payload_store import FakeRawPayloadStore
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.errors import PortDataMalformed, PortUnavailable
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
def service(
    titles: FakeTitleRepository,
    episodes: FakeEpisodeRepository,
    payloads: FakeRawPayloadStore,
    provider: FakeMetadataProvider,
    commits: list[int],
) -> EnrichService:
    async def commit() -> None:
        commits.append(1)

    return EnrichService(titles, episodes, payloads, provider, commit)


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
    assert stored.community_rating == 8.4


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
