"""One batch of a walk, against port fakes.

The single-item cases are the easy half. What a batch exposes -- and what
every case below the "the batch" heading is about -- is an episode whose
series arrives on the *same page* rather than an earlier one, a season
upserted once per episode instead of once per page, and a nightly walk
re-enqueueing enrichment for all 1,126,674 items it just saw.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.title_match_repository import FakeTitleMatchRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import EnrichmentState, HdrFormat, MatchMethod, TitleKind
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind, JobPriority
from usher.ports.jobs import JobRequest
from usher.ports.source import SourceItem, SourceItemKind
from usher.services.ingest import IngestService
from usher.services.matching import MatchService

SOURCE_ID = new_id()
RUN_AT = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)

MOVIE = SourceItem(
    external_id="movie-1",
    name="Example Movie",
    kind=SourceItemKind.MOVIE,
    year=2021,
    provider_ids={"tmdb": "438631"},
    container="mkv",
    video_codec="hevc",
    hdr_format=HdrFormat.DOLBY_VISION,
    width=3840,
    height=2160,
    runtime_seconds=9360,
)
SERIES = SourceItem(
    external_id="series-1",
    name="Example Series",
    kind=SourceItemKind.SERIES,
    year=2011,
    provider_ids={"tvdb": "121361"},
)
EPISODE = SourceItem(
    external_id="episode-1",
    name="Kissed by Fire",
    kind=SourceItemKind.EPISODE,
    provider_ids={"imdb": "tt2178782"},
    container="mkv",
    series_external_id="series-1",
    season_number=3,
    episode_number=5,
)


class _Fixture:
    def __init__(self) -> None:
        self.titles = FakeTitleRepository()
        # Wired to the title repository on purpose: `titles` is one table and
        # `TitleRepository.add` flushes, so a stub the match stage just wrote
        # is visible to the next match read. Unwired, the *second* walk of a
        # series this pipeline had itself stubbed re-created it and failed.
        self.matching = FakeTitleMatchRepository(titles=self.titles)
        self.queue = FakeJobQueue()
        self.media_items = FakeMediaItemRepository()
        self.episodes = FakeEpisodeRepository()
        self.service = IngestService(
            matcher=MatchService(titles=self.titles, matching=self.matching, queue=self.queue),
            matching=self.matching,
            media_items=self.media_items,
            episodes=self.episodes,
            queue=self.queue,
        )


@pytest.fixture
def fixture() -> _Fixture:
    return _Fixture()


@pytest.fixture
def service(fixture: _Fixture) -> IngestService:
    return fixture.service


# -- one item ---------------------------------------------------------------


async def test_a_movie_becomes_a_media_item_carrying_its_quality_facts(
    fixture: _Fixture,
) -> None:
    await fixture.service.ingest_batch(SOURCE_ID, [MOVIE], observed_at=RUN_AT)
    stored = await fixture.media_items.get_by_external_id(SOURCE_ID, "movie-1")
    assert stored is not None
    assert stored.hdr_format is HdrFormat.DOLBY_VISION
    assert stored.container == "mkv"
    assert stored.last_seen_at == RUN_AT
    assert stored.title_id is not None


async def test_every_item_in_a_batch_carries_the_runs_instant_not_its_own(
    fixture: _Fixture,
) -> None:
    """The availability sweep compares `last_seen_at < run.started_at`. If
    each row stamped its own `now()`, `last_seen_at` would stop meaning "the
    run that saw this" and start meaning "when this row happened to be
    written" -- which is not the quantity `ReconcileService` compares
    against, and not one anything else in the pipeline can reproduce."""
    await fixture.service.ingest_batch(SOURCE_ID, [MOVIE, SERIES], observed_at=RUN_AT)
    for external_id in ("movie-1", "series-1"):
        stored = await fixture.media_items.get_by_external_id(SOURCE_ID, external_id)
        assert stored is not None
        assert stored.last_seen_at == RUN_AT


async def test_an_unmatched_movie_is_stored_rather_than_dropped(
    fixture: _Fixture,
) -> None:
    """PRD 02: "Unmatched items are never dropped." A MediaItem with a NULL
    title_id is a legitimate, expected state and the review queue is what it
    is for."""
    orphan = SourceItem(external_id="orphan-1", name="Home Video 2004", kind=SourceItemKind.MOVIE)
    await fixture.service.ingest_batch(SOURCE_ID, [orphan], observed_at=RUN_AT)
    unmatched = await fixture.media_items.list_unmatched(SOURCE_ID)
    assert [item.external_id for item in unmatched] == ["orphan-1"]


async def test_ingesting_the_same_batch_twice_changes_nothing(
    fixture: _Fixture,
) -> None:
    """PRD 03: "Four idempotent, resumable stages. Any stage can be re-run
    without duplicating work." A resumed sync replays the batch that was in
    flight when it died."""
    first = await fixture.service.ingest_batch(SOURCE_ID, [SERIES, EPISODE], observed_at=RUN_AT)
    second = await fixture.service.ingest_batch(SOURCE_ID, [SERIES, EPISODE], observed_at=RUN_AT)
    assert (first.inserted, first.updated) == (2, 0)
    assert (second.inserted, second.updated) == (0, 2)
    assert await fixture.media_items.count_for_source(SOURCE_ID) == 2
    seasons, episodes = await fixture.episodes.list_for_title(
        (await fixture.media_items.get_by_external_id(SOURCE_ID, "series-1")).title_id  # type: ignore[union-attr, arg-type]
    )
    assert len(seasons) == 1
    assert len(episodes) == 1


# -- enrichment triage ------------------------------------------------------


async def test_a_newly_stubbed_title_is_enqueued_for_enrichment(
    fixture: _Fixture,
) -> None:
    """PRD 03: "Newly added to a source" is priority 50. A stub that is never
    enqueued stays a stub forever and the read-through design does nothing."""
    await fixture.service.ingest_batch(SOURCE_ID, [MOVIE], observed_at=RUN_AT)
    enrich = await fixture.queue.claim([JobKind.ENRICH], limit=10)
    assert len(enrich) == 1
    assert enrich[0].priority == JobPriority.NEW


async def test_a_matched_skeleton_is_enqueued_for_enrichment(
    fixture: _Fixture,
) -> None:
    """The path a bootstrapped catalog actually takes. M2 left 1,271,138
    titles at `skeleton`; a walk that only enqueued the stubs it minted
    itself would leave every one of them unenriched forever, and nothing in
    the stub case above can tell."""
    await fixture.matching.given_title(
        kind=TitleKind.MOVIE, name="Example Movie", year=2021, tmdb_id=438631
    )
    await fixture.service.ingest_batch(SOURCE_ID, [MOVIE], observed_at=RUN_AT)
    assert len(await fixture.queue.claim([JobKind.ENRICH], limit=10)) == 1


async def test_an_already_enriched_title_is_not_re_enqueued(
    fixture: _Fixture,
) -> None:
    """A nightly walk sees every item every night. Enqueueing enrichment for
    all 1,126,674 of them each time makes the queue permanently the size of
    the library and starves everything else."""
    await fixture.matching.given_title(
        kind=TitleKind.MOVIE,
        name="Example Movie",
        year=2021,
        tmdb_id=438631,
        enrichment_state=EnrichmentState.ENRICHED,
    )
    await fixture.service.ingest_batch(SOURCE_ID, [MOVIE], observed_at=RUN_AT)
    assert await fixture.queue.claim([JobKind.ENRICH], limit=10) == []


async def test_a_stub_title_is_re_enqueued(fixture: _Fixture) -> None:
    """The `ENRICHMENT_RANK` trap from the other side. `StrEnum` compares
    lexicographically, so `"stub" >= "enriched"` is `True` -- a guard written
    as a direct comparison silently *skips* every stub, which is the
    population most in need of enrichment. ADR-0008."""
    await fixture.matching.given_title(
        kind=TitleKind.MOVIE,
        name="Example Movie",
        year=2021,
        tmdb_id=438631,
        enrichment_state=EnrichmentState.STUB,
    )
    await fixture.service.ingest_batch(SOURCE_ID, [MOVIE], observed_at=RUN_AT)
    assert len(await fixture.queue.claim([JobKind.ENRICH], limit=10)) == 1


# -- episodes ---------------------------------------------------------------


async def test_an_episode_is_hung_off_its_series_title(fixture: _Fixture) -> None:
    """The series is ingested first, in the same batch. `SourceItem` carries
    `series_external_id`, `season_number` and `episode_number` precisely so
    this is possible without a second upstream request."""
    await fixture.service.ingest_batch(SOURCE_ID, [SERIES, EPISODE], observed_at=RUN_AT)
    series_item = await fixture.media_items.get_by_external_id(SOURCE_ID, "series-1")
    episode_item = await fixture.media_items.get_by_external_id(SOURCE_ID, "episode-1")
    assert series_item is not None and episode_item is not None
    assert series_item.title_id is not None
    assert episode_item.title_id == series_item.title_id
    assert episode_item.episode_id is not None
    seasons, episodes = await fixture.episodes.list_for_title(series_item.title_id)
    assert [one.season_number for one in seasons] == [3]
    assert [(e.season_number, e.episode_number) for e in episodes] == [(3, 5)]
    assert episodes[0].id == episode_item.episode_id
    assert episodes[0].name == "Kissed by Fire"


async def test_an_episode_arriving_before_its_series_in_the_same_batch_still_attaches(
    fixture: _Fixture,
) -> None:
    """`SortBy=DateCreated` says nothing about a series preceding its
    episodes, and Emby genuinely returns them interleaved. An implementation
    that built its in-batch series map from the items it had already
    *processed* rather than from the whole page attaches on one ordering and
    silently misses on the other."""
    await fixture.service.ingest_batch(SOURCE_ID, [EPISODE, SERIES], observed_at=RUN_AT)
    episode_item = await fixture.media_items.get_by_external_id(SOURCE_ID, "episode-1")
    series_item = await fixture.media_items.get_by_external_id(SOURCE_ID, "series-1")
    assert episode_item is not None and series_item is not None
    assert episode_item.title_id == series_item.title_id
    assert episode_item.episode_id is not None


async def test_an_episode_whose_series_is_not_yet_known_is_left_unmatched(
    fixture: _Fixture,
) -> None:
    """A walk is sorted by creation date, which offers no guarantee a series
    is seen before its episodes -- and an episode whose series arrives in a
    later page must not be dropped, nor attached to a guess. It goes to the
    review queue and is re-enqueued; the next batch or the next run resolves
    it."""
    await fixture.service.ingest_batch(SOURCE_ID, [EPISODE], observed_at=RUN_AT)
    stored = await fixture.media_items.get_by_external_id(SOURCE_ID, "episode-1")
    assert stored is not None
    assert stored.title_id is None
    assert stored.episode_id is None
    claimed = await fixture.queue.claim([JobKind.MATCH], limit=10)
    assert [job.key for job in claimed] == ["episode-1"]
    assert claimed[0].priority == JobPriority.BACKFILL


async def test_an_unresolved_episode_is_stored_rather_than_dropped(
    fixture: _Fixture,
) -> None:
    """`test_an_unmatched_movie_is_stored_rather_than_dropped`'s episode
    sibling, and the one that bites: 999,827 of this deployment's items are
    episodes, so an implementation that quietly filtered the unresolvable
    ones out of the upsert would lose most of a first walk and the sweep
    would then retract them all on the second."""
    await fixture.service.ingest_batch(SOURCE_ID, [EPISODE], observed_at=RUN_AT)
    unmatched = await fixture.media_items.list_unmatched(SOURCE_ID)
    assert [item.external_id for item in unmatched] == ["episode-1"]
    assert await fixture.media_items.count_for_source(SOURCE_ID) == 1


async def test_an_episode_finds_a_series_ingested_by_an_earlier_batch(
    fixture: _Fixture,
) -> None:
    """The batch-boundary case the one above sets up. `resolve_series_titles`
    is a database read, not an in-batch dict, for exactly this."""
    await fixture.service.ingest_batch(SOURCE_ID, [SERIES], observed_at=RUN_AT)
    await fixture.service.ingest_batch(SOURCE_ID, [EPISODE], observed_at=RUN_AT)
    stored = await fixture.media_items.get_by_external_id(SOURCE_ID, "episode-1")
    assert stored is not None
    assert stored.title_id is not None
    assert stored.episode_id is not None


async def test_an_episode_missing_its_numbers_is_left_unmatched(
    fixture: _Fixture,
) -> None:
    """A `season_number`/`episode_number` of `None` is not a zero. Attaching
    on a defaulted number hangs every such episode off S00E00 of its series
    and collapses them into one row -- and `Episode.episode_number` is
    `ge=0`, so `0` is a legal value the model will happily store."""
    numberless = SourceItem(
        external_id="episode-2",
        name="Unnumbered",
        kind=SourceItemKind.EPISODE,
        series_external_id="series-1",
        season_number=None,
        episode_number=None,
    )
    await fixture.service.ingest_batch(SOURCE_ID, [SERIES, numberless], observed_at=RUN_AT)
    stored = await fixture.media_items.get_by_external_id(SOURCE_ID, "episode-2")
    assert stored is not None
    assert stored.episode_id is None
    assert stored.title_id is None


async def test_an_episode_of_an_unmatched_series_is_left_unmatched(
    fixture: _Fixture,
) -> None:
    """`resolve_series_titles` omits a series it has not matched, and the
    difference between "no such series" and "that series has no title yet"
    must not be papered over: attaching to a `None` title is the one thing
    `media_items.title_id` being nullable makes syntactically possible."""
    unmatchable = SourceItem(
        external_id="series-2", name="No Ids At All", kind=SourceItemKind.SERIES
    )
    orphan = SourceItem(
        external_id="episode-3",
        name="An Episode",
        kind=SourceItemKind.EPISODE,
        series_external_id="series-2",
        season_number=1,
        episode_number=1,
    )
    await fixture.service.ingest_batch(SOURCE_ID, [unmatchable, orphan], observed_at=RUN_AT)
    stored = await fixture.media_items.get_by_external_id(SOURCE_ID, "episode-3")
    assert stored is not None
    assert stored.title_id is None
    assert stored.episode_id is None


async def test_an_attached_episode_reports_how_it_was_resolved(
    fixture: _Fixture,
) -> None:
    """`usher.ingest.items` is labelled by outcome. An episode resolved
    through its series is neither unmatched nor matched by any tier of the
    ladder it never walked, and reporting it as `unmatched` would put
    999,827 items a night in the wrong bucket of PRD 10's panel."""
    result = await fixture.service.ingest_batch(SOURCE_ID, [SERIES, EPISODE], observed_at=RUN_AT)
    by_id = {outcome.external_id: outcome for outcome in result.outcomes}
    assert by_id["episode-1"].method is MatchMethod.SERIES_PARENT
    assert by_id["series-1"].method is MatchMethod.CREATED_STUB
    assert [outcome.external_id for outcome in result.outcomes] == ["series-1", "episode-1"]


# -- the batch --------------------------------------------------------------


async def test_two_series_worth_of_episodes_land_under_their_own_series(
    fixture: _Fixture,
) -> None:
    """The batch-level version of "every series has an S01E01". A walk sorted
    by creation date interleaves shows, so one page routinely carries two
    S01E01s -- and a resolve scoped to a single title, or a season map keyed
    on the number alone, hangs one show's episode off the other's."""
    other_series = SourceItem(
        external_id="series-2",
        name="Other Series",
        kind=SourceItemKind.SERIES,
        year=2015,
        provider_ids={"tvdb": "999999"},
    )
    first = SourceItem(
        external_id="e-a",
        name="A Pilot",
        kind=SourceItemKind.EPISODE,
        series_external_id="series-1",
        season_number=1,
        episode_number=1,
    )
    second = SourceItem(
        external_id="e-b",
        name="Another Pilot",
        kind=SourceItemKind.EPISODE,
        series_external_id="series-2",
        season_number=1,
        episode_number=1,
    )
    await fixture.service.ingest_batch(
        SOURCE_ID, [SERIES, other_series, first, second], observed_at=RUN_AT
    )
    stored = {
        key: await fixture.media_items.get_by_external_id(SOURCE_ID, key)
        for key in ("series-1", "series-2", "e-a", "e-b")
    }
    assert stored["e-a"] is not None and stored["e-b"] is not None
    assert stored["series-1"] is not None and stored["series-2"] is not None
    assert stored["e-a"].title_id == stored["series-1"].title_id
    assert stored["e-b"].title_id == stored["series-2"].title_id
    assert stored["e-a"].episode_id != stored["e-b"].episode_id
    assert stored["e-a"].episode_id is not None


async def test_ingest_costs_a_bounded_number_of_writes_per_batch(
    fixture: _Fixture,
) -> None:
    """500 episodes must not be 1,500 round trips. The batched form issues
    one season upsert, one season resolve, one episode upsert and one episode
    resolve, whatever the batch holds."""
    episodes = [
        SourceItem(
            external_id=f"e{index}",
            name=f"Episode {index}",
            kind=SourceItemKind.EPISODE,
            series_external_id="series-1",
            season_number=1,
            episode_number=index,
        )
        for index in range(500)
    ]
    await fixture.service.ingest_batch(SOURCE_ID, [SERIES], observed_at=RUN_AT)
    fixture.episodes.reset_calls()
    fixture.matching.reset_calls()
    fixture.media_items.reset_calls()
    await fixture.service.ingest_batch(SOURCE_ID, episodes, observed_at=RUN_AT)
    assert fixture.episodes.calls <= 4, fixture.episodes.calls
    assert fixture.matching.calls <= 3, fixture.matching.calls
    assert fixture.media_items.calls <= 2, fixture.media_items.calls


async def test_ingest_enqueues_once_per_batch(fixture: _Fixture) -> None:
    """A page of 500 unresolvable episodes is one `enqueue` carrying 500
    requests, not 500 calls -- and the enrichment and re-match requests share
    it, because they are one statement's worth of work."""
    calls: list[int] = []
    original = fixture.queue.enqueue

    async def _counted(requests: Sequence[JobRequest]) -> int:
        calls.append(len(requests))
        return await original(requests)

    fixture.queue.enqueue = _counted  # type: ignore[method-assign]
    episodes = [
        SourceItem(
            external_id=f"e{index}",
            name=f"Episode {index}",
            kind=SourceItemKind.EPISODE,
            series_external_id="series-nope",
            season_number=1,
            episode_number=index,
        )
        for index in range(500)
    ]
    await fixture.service.ingest_batch(SOURCE_ID, [MOVIE, *episodes], observed_at=RUN_AT)
    assert calls == [501], calls


async def test_a_nightly_re_walk_of_an_enriched_library_enqueues_nothing(
    fixture: _Fixture,
) -> None:
    """The steady state, and the one that decides whether the queue is a
    queue or a copy of the library. Every item matched, every title enriched:
    a second walk must produce no jobs at all."""
    await fixture.matching.given_title(
        kind=TitleKind.MOVIE,
        name="Example Movie",
        year=2021,
        tmdb_id=438631,
        enrichment_state=EnrichmentState.ENRICHED,
    )
    series_title = await fixture.matching.given_title(
        kind=TitleKind.SERIES,
        name="Example Series",
        year=2011,
        tvdb_id=121361,
        enrichment_state=EnrichmentState.ENRICHED,
    )
    assert series_title is not None
    await fixture.service.ingest_batch(SOURCE_ID, [MOVIE, SERIES, EPISODE], observed_at=RUN_AT)
    await fixture.service.ingest_batch(SOURCE_ID, [MOVIE, SERIES, EPISODE], observed_at=RUN_AT)
    assert await fixture.queue.depth() == dict.fromkeys(JobKind, 0)


async def test_the_batch_reports_its_own_matched_and_unmatched_counts(
    fixture: _Fixture,
) -> None:
    """`SyncRun` carries `items_matched`/`items_unmatched`, and the batch is
    the only thing that already knows them. The alternative -- a
    `list_unmatched` query per batch to recover a number just computed -- is
    a round trip per batch for an answer in hand."""
    orphan = SourceItem(external_id="orphan-1", name="Home Video 2004", kind=SourceItemKind.MOVIE)
    result = await fixture.service.ingest_batch(
        SOURCE_ID, [MOVIE, SERIES, EPISODE, orphan], observed_at=RUN_AT
    )
    assert (result.inserted, result.updated) == (4, 0)
    assert (result.matched, result.unmatched) == (3, 1)


async def test_a_duplicate_inside_one_batch_is_one_row_and_two_outcomes(
    fixture: _Fixture,
) -> None:
    """`list_items`' own contract permits the same item twice in one walk, so
    the counts a batch reports and the rows it writes legitimately disagree.
    An implementation that assumed they matched would report a re-sync as
    growth on PRD 10's "library growth per week" panel."""
    result = await fixture.service.ingest_batch(SOURCE_ID, [MOVIE, MOVIE], observed_at=RUN_AT)
    assert (result.inserted, result.updated) == (1, 0)
    assert (result.matched, result.unmatched) == (2, 0)
    assert await fixture.media_items.count_for_source(SOURCE_ID) == 1


async def test_an_empty_batch_touches_nothing(fixture: _Fixture) -> None:
    """A walk's last page is routinely empty, and so is a delta walk that
    found no changes. Neither should cost a statement."""
    fixture.matching.reset_calls()
    fixture.episodes.reset_calls()
    fixture.media_items.reset_calls()
    result = await fixture.service.ingest_batch(SOURCE_ID, [], observed_at=RUN_AT)
    assert (result.inserted, result.updated, result.matched, result.unmatched) == (0, 0, 0, 0)
    assert fixture.matching.calls == 0
    assert fixture.episodes.calls == 0
    # `upsert_many` short-circuits an empty batch on both implementations, so
    # without the guard in `ingest_batch` this is the only assertion that
    # notices -- an empty page must not reach a repository at all.
    assert fixture.media_items.calls == 0
    assert await fixture.queue.depth() == dict.fromkeys(JobKind, 0)


async def test_a_manual_resolution_survives_the_next_walk(fixture: _Fixture) -> None:
    """The review queue's whole point. An operator attaches an unmatched item
    by hand; tonight's walk upserts it again with `title_id=None`, because
    nothing about the source changed. `upsert_many`'s COALESCE is what keeps
    the resolution, and this is the service-level case that a walk really
    does pass `None` rather than re-deriving it."""
    orphan = SourceItem(external_id="orphan-1", name="Home Video 2004", kind=SourceItemKind.MOVIE)
    await fixture.service.ingest_batch(SOURCE_ID, [orphan], observed_at=RUN_AT)
    stored = await fixture.media_items.get_by_external_id(SOURCE_ID, "orphan-1")
    assert stored is not None
    resolved_to = uuid.UUID(int=1)
    await fixture.media_items.attach_title(stored.id, title_id=resolved_to, episode_id=None)
    await fixture.service.ingest_batch(SOURCE_ID, [orphan], observed_at=RUN_AT)
    again = await fixture.media_items.get_by_external_id(SOURCE_ID, "orphan-1")
    assert again is not None
    assert again.title_id == resolved_to
