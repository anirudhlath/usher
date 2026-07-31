"""The match stage, against port fakes. No network, no database.

Every case names the tier it exercises and the wrong implementation it
fails, because the ladder's whole value is that it stops at the first hit --
a matcher that tried every tier and took the best answer would resolve a
film by name when its own TMDb id said otherwise.

**The cases that matter are the batch-level ones.** A single-item matcher
is easy to get right; what a page of 500 items exposes is a stub created
twice for one TMDb id, a lookup issued per item, and -- the one that would
have doubled the catalog -- an episode falling through the ladder and
creating a `Title` from its own episode-level provider ids.
"""

import uuid
from collections.abc import Sequence

import pytest

from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.title_match_repository import FakeTitleMatchRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import EnrichmentState, MatchMethod, TitleKind
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.title import Title
from usher.ports.ingest import ProviderRef
from usher.ports.source import SourceItem, SourceItemKind
from usher.services.matching import MatchService


def _item(external_id: str, **overrides: object) -> SourceItem:
    fields: dict[str, object] = {
        "external_id": external_id,
        "name": "Example Movie",
        "kind": SourceItemKind.MOVIE,
        "year": 2021,
        "provider_ids": {},
    }
    fields.update(overrides)
    return SourceItem(**fields)  # type: ignore[arg-type]


class _Fixture:
    """The three fakes plus the service, so a case can reach past the
    service into whichever store it seeded."""

    def __init__(self, matching: FakeTitleMatchRepository | None = None) -> None:
        self.titles = FakeTitleRepository()
        self.matching = matching or FakeTitleMatchRepository()
        self.queue = FakeJobQueue()
        self.service = MatchService(titles=self.titles, matching=self.matching, queue=self.queue)


@pytest.fixture
def fixture() -> _Fixture:
    return _Fixture()


@pytest.fixture
def service(fixture: _Fixture) -> MatchService:
    return fixture.service


# -- the ladder, tier by tier ----------------------------------------------


async def test_a_tmdb_id_wins_over_everything_else(fixture: _Fixture) -> None:
    """Tier 1. The item's name and year both point at a *different* title,
    seeded on purpose: a matcher that took the best of all tiers rather than
    the first hit would resolve this by name."""
    by_id = await fixture.matching.given_title(kind=TitleKind.MOVIE, name="Renamed", tmdb_id=438631)
    await fixture.matching.given_title(kind=TitleKind.MOVIE, name="Example Movie", year=2021)
    outcomes = await fixture.service.match([_item("m1", provider_ids={"tmdb": "438631"})])
    assert outcomes[0].title_id == by_id
    assert outcomes[0].method is MatchMethod.TMDB_ID


async def test_a_tmdb_id_beats_an_imdb_id_pointing_elsewhere(fixture: _Fixture) -> None:
    """The plan's own mutation table names reordering `_PROVIDER_TIERS` as a
    mutation no case caught, and asks for this one rather than a shrug. Two
    provider ids on one item, each resolving to a *different* title: only
    the order of the ladder decides which wins."""
    by_tmdb = await fixture.matching.given_title(
        kind=TitleKind.MOVIE, name="By TMDb", tmdb_id=438631
    )
    by_imdb = await fixture.matching.given_title(
        kind=TitleKind.MOVIE, name="By IMDb", imdb_id="tt1160419"
    )
    outcomes = await fixture.service.match(
        [_item("m1", provider_ids={"tmdb": "438631", "imdb": "tt1160419"})]
    )
    assert outcomes[0].title_id == by_tmdb, "the IMDb tier overtook the TMDb one"
    assert outcomes[0].title_id != by_imdb
    assert outcomes[0].method is MatchMethod.TMDB_ID


async def test_an_imdb_id_is_the_second_tier(fixture: _Fixture) -> None:
    title = await fixture.matching.given_title(
        kind=TitleKind.MOVIE, name="Anything", imdb_id="tt1160419"
    )
    outcomes = await fixture.service.match([_item("m1", provider_ids={"imdb": "tt1160419"})])
    assert (outcomes[0].title_id, outcomes[0].method) == (title, MatchMethod.IMDB_ID)


async def test_an_imdb_id_beats_a_tvdb_id_pointing_elsewhere(fixture: _Fixture) -> None:
    """The second half of the ordering property. Without it, swapping the
    IMDb and TVDb rows of `_PROVIDER_TIERS` survives the whole suite."""
    by_imdb = await fixture.matching.given_title(
        kind=TitleKind.SERIES, name="By IMDb", imdb_id="tt0944947"
    )
    await fixture.matching.given_title(kind=TitleKind.SERIES, name="By TVDb", tvdb_id=121361)
    outcomes = await fixture.service.match(
        [
            _item(
                "s1",
                kind=SourceItemKind.SERIES,
                provider_ids={"imdb": "tt0944947", "tvdb": "121361"},
            )
        ]
    )
    assert outcomes[0].title_id == by_imdb
    assert outcomes[0].method is MatchMethod.IMDB_ID


async def test_a_tvdb_id_is_the_third_tier(fixture: _Fixture) -> None:
    """M2 linked 50,793 titles to a tvdb_id, and Emby series routinely carry
    `ProviderIds.Tvdb` with no TMDb id at all -- so a ladder that stopped at
    IMDb would push most television to the review queue."""
    title = await fixture.matching.given_title(
        kind=TitleKind.SERIES, name="Anything", tvdb_id=121361
    )
    outcomes = await fixture.service.match(
        [_item("s1", kind=SourceItemKind.SERIES, provider_ids={"tvdb": "121361"})]
    )
    assert (outcomes[0].title_id, outcomes[0].method) == (title, MatchMethod.TVDB_ID)


async def test_a_tvdb_ref_carries_no_kind(fixture: _Fixture) -> None:
    """`TitleMatchRepository`'s own contract answers a TVDb ref with
    `kind=None` and its Postgres statement never filters on kind at all --
    TVDb series ids are one namespace. A service that stamped the item's
    kind onto the ref would build a key the repository can answer but that
    no *other* caller's ref ever equals, so a movie and a series carrying
    the same TVDb id would be two lookups instead of one."""
    seen: list[Sequence[ProviderRef]] = []
    original = fixture.matching.match_by_provider_ids

    async def _record(refs: Sequence[ProviderRef]) -> dict[ProviderRef, uuid.UUID]:
        seen.append(list(refs))
        return await original(refs)

    fixture.matching.match_by_provider_ids = _record  # type: ignore[method-assign]
    await fixture.service.match(
        [_item("s1", kind=SourceItemKind.SERIES, provider_ids={"tvdb": "121361"})]
    )
    assert seen == [[ProviderRef(provider="tvdb", value="121361", kind=None)]]


async def test_name_and_year_are_the_fourth_tier(fixture: _Fixture) -> None:
    title = await fixture.matching.given_title(
        kind=TitleKind.MOVIE, name="Example Movie", year=2021
    )
    outcomes = await fixture.service.match([_item("m1")])
    assert (outcomes[0].title_id, outcomes[0].method) == (title, MatchMethod.NAME_YEAR)


async def test_a_trusted_provider_id_the_catalog_lacks_creates_a_stub(
    fixture: _Fixture,
) -> None:
    """PRD 03's stub-on-sight. The catalog holds 1,271,138 titles but only
    291,737 with a `tmdb_id`, so a source item naming a TMDb id Usher has
    never seen is common rather than exotic -- and a TMDb or IMDb id is an
    identity claim strong enough to create a canonical title on."""
    outcomes = await fixture.service.match(
        [_item("m1", provider_ids={"tmdb": "999999", "imdb": "tt9999999"})]
    )
    assert outcomes[0].method is MatchMethod.CREATED_STUB
    assert outcomes[0].title_id is not None
    created = await fixture.titles.get(outcomes[0].title_id)
    assert created is not None
    assert created.enrichment_state is EnrichmentState.STUB
    assert created.tmdb_id == 999999
    assert created.imdb_id == "tt9999999"
    assert created.kind is TitleKind.MOVIE


async def test_an_item_with_no_usable_id_and_no_name_match_is_unmatched(
    fixture: _Fixture,
) -> None:
    """PRD 03 stage 5. A bare name is not an identity claim, so this lands in
    the review queue rather than fabricating a title -- and it is *not*
    dropped, which PRD 02 states as a rule."""
    outcomes = await fixture.service.match([_item("m1", name="Unknown Thing", year=None)])
    assert outcomes[0].title_id is None
    assert outcomes[0].method is MatchMethod.UNMATCHED


async def test_an_unmatched_item_is_enqueued_for_a_remote_search(fixture: _Fixture) -> None:
    """PRD 03's tier 4 is "TMDb search API as a last resort" -- one network
    call per unmatched item, and a first full walk produces those in the
    hundreds of thousands. Running it inline makes the walk's duration a
    function of TMDb's rate limit, so it is queued at BACKFILL priority and
    the queue's concurrency is what bounds it."""
    await fixture.service.match([_item("m1", name="Unknown Thing", year=None)])
    claimed = await fixture.queue.claim([JobKind.MATCH], limit=10)
    assert [job.key for job in claimed] == ["m1"]
    assert claimed[0].priority == JobPriority.BACKFILL


async def test_a_matched_item_is_not_enqueued_for_a_remote_search(fixture: _Fixture) -> None:
    """The other half. Enqueueing every item rather than every *unmatched*
    one makes the queue permanently 1,126,674 rows deep and starves
    everything behind it."""
    await fixture.matching.given_title(kind=TitleKind.MOVIE, name="Example Movie", year=2021)
    await fixture.service.match([_item("m1")])
    assert await fixture.queue.claim([JobKind.MATCH], limit=10) == []


# -- the batch, which is where this goes wrong ------------------------------


async def test_a_stub_created_for_one_item_is_reused_by_the_next_in_the_same_batch(
    fixture: _Fixture,
) -> None:
    """A movie and its two alternate cuts arrive in one page carrying the
    same TMDb id. A matcher that created a stub per item would produce three
    titles, two of which then fail on `ix_titles_tmdb_id_kind` -- or worse,
    succeed and fragment the watch history."""
    outcomes = await fixture.service.match(
        [
            _item("m1", provider_ids={"tmdb": "999999"}),
            _item("m2", provider_ids={"tmdb": "999999"}),
            _item("m3", provider_ids={"tmdb": "999999"}),
        ]
    )
    assert outcomes[0].title_id == outcomes[1].title_id == outcomes[2].title_id
    assert await fixture.titles.count_by_state() == {
        EnrichmentState.SKELETON: 0,
        EnrichmentState.STUB: 1,
        EnrichmentState.ENRICHED: 0,
    }


async def test_a_stub_is_written_once_for_a_batch_that_shares_one_tmdb_id(
    fixture: _Fixture,
) -> None:
    """The case above does **not** pin the in-batch cache, and the plan's
    mutation table predicted that it did. Measured: deleting the `created`
    dict entirely leaves all 25 other cases green, because the second `add`
    raises `RepositoryConflict` on `ix_titles_tmdb_id_kind` and the race
    handler then attaches to the stub the first item just created -- the
    same id, by a much more expensive route.

    So the property is a count, not an identity: three items must cost one
    write, not one write plus two conflicting writes and two recovery reads.
    Against Postgres each of those failed inserts is also a SAVEPOINT
    round trip, and a multi-version library is where they arrive in bulk.
    """
    writes = 0
    original = fixture.titles.add

    async def _counted(title: Title) -> None:
        nonlocal writes
        writes += 1
        await original(title)

    fixture.titles.add = _counted  # type: ignore[method-assign]
    await fixture.service.match(
        [_item(f"m{index}", provider_ids={"tmdb": "999999"}) for index in range(3)]
    )
    assert writes == 1, writes


async def test_two_items_sharing_a_tmdb_id_across_kinds_get_two_stubs(
    fixture: _Fixture,
) -> None:
    """ADR-0011 in the in-batch cache. TMDb's movie and series id spaces
    overlap on 26,968 ids, so a `created` map keyed on the bare id -- rather
    than on the whole `ProviderRef`, kind included -- would hand a series
    the movie's stub and attach the household's watch history to the wrong
    production."""
    outcomes = await fixture.service.match(
        [
            _item("m1", provider_ids={"tmdb": "550"}),
            _item("s1", kind=SourceItemKind.SERIES, provider_ids={"tmdb": "550"}),
        ]
    )
    assert outcomes[0].title_id != outcomes[1].title_id
    movie = await fixture.titles.get(outcomes[0].title_id or uuid.UUID(int=0))
    series = await fixture.titles.get(outcomes[1].title_id or uuid.UUID(int=0))
    assert movie is not None and movie.kind is TitleKind.MOVIE
    assert series is not None and series.kind is TitleKind.SERIES


async def test_every_item_in_a_batch_gets_one_outcome_in_order(fixture: _Fixture) -> None:
    """`IngestService` joins outcomes back to items positionally and by
    `external_id`; an implementation that dropped the items it found nothing
    for -- the same defect `TitleMatchRepository`'s own contract forbids one
    layer down -- would silently lose them from the walk entirely."""
    items = [
        _item("m1", provider_ids={"tmdb": "999999"}),
        _item("m2", name="Nothing Matches This", year=None),
        _item("s1", kind=SourceItemKind.SERIES, name="Nor This", year=None),
    ]
    outcomes = await fixture.service.match(items)
    assert [outcome.external_id for outcome in outcomes] == ["m1", "m2", "s1"]


async def test_matching_a_page_costs_a_bounded_number_of_lookups(fixture: _Fixture) -> None:
    """The scale property, asserted rather than hoped for. 500 items must
    not be 500 round trips: the service issues one provider-id lookup and
    one name+year lookup per batch, whatever the batch holds."""
    for index in range(500):
        await fixture.matching.given_title(
            kind=TitleKind.MOVIE, name=f"Movie {index}", tmdb_id=index + 1
        )
    fixture.matching.reset_calls()
    outcomes = await fixture.service.match(
        [_item(f"m{index}", provider_ids={"tmdb": str(index + 1)}) for index in range(500)]
    )
    assert all(outcome.title_id is not None for outcome in outcomes)
    assert fixture.matching.calls <= 2, fixture.matching.calls


async def test_a_batch_of_unmatched_items_is_one_enqueue(fixture: _Fixture) -> None:
    """The other per-item round trip. 500 unmatched items on a first walk
    must be one `enqueue` call carrying 500 requests, not 500 calls."""
    calls = 0
    original = fixture.queue.enqueue

    async def _counted(requests: Sequence[object]) -> int:
        nonlocal calls
        calls += 1
        return await original(requests)  # type: ignore[arg-type]

    fixture.queue.enqueue = _counted  # type: ignore[method-assign]
    await fixture.service.match(
        [_item(f"m{index}", name=f"Unknown {index}", year=None) for index in range(500)]
    )
    assert calls == 1
    assert len(await fixture.queue.claim([JobKind.MATCH], limit=1000)) == 500


# -- episodes never walk this ladder ---------------------------------------


async def test_an_episode_is_never_matched_by_its_own_provider_ids(
    fixture: _Fixture,
) -> None:
    """The batch-level catastrophe. An Emby episode carries the *episode's*
    own ids -- `{"Imdb": "tt2178782", "Tvdb": "4517466"}`, verified against
    the live payload -- not its series'. TVDb's episode and series id spaces
    are different namespaces that overlap numerically, and
    `TitleMatchRepository`'s TVDb lookup deliberately does not filter on
    kind, so an episode fed through the ladder resolves to whichever
    unrelated series happens to hold that integer. 999,827 of this
    deployment's items are episodes.
    """
    unrelated = await fixture.matching.given_title(
        kind=TitleKind.SERIES, name="Something Else Entirely", tvdb_id=4517466
    )
    outcomes = await fixture.service.match(
        [
            _item(
                "e1",
                kind=SourceItemKind.EPISODE,
                name="Kissed by Fire",
                year=2013,
                provider_ids={"imdb": "tt2178782", "tvdb": "4517466"},
                series_external_id="series-1",
                season_number=3,
                episode_number=5,
            )
        ]
    )
    assert outcomes[0].title_id != unrelated
    assert outcomes[0].title_id is None
    assert outcomes[0].method is MatchMethod.UNMATCHED


async def test_an_episode_never_creates_a_title_stub(fixture: _Fixture) -> None:
    """The same defect's larger half. An episode carrying an IMDb id the
    catalog lacks -- which every episode does, since `tvEpisode` is
    deliberately excluded from M2's bootstrap -- would otherwise create a
    `Title` named after the episode. At 999,827 episodes that is a catalog
    of junk roughly the size of the real one, each row also enqueued for a
    TMDb enrichment that can never succeed."""
    await fixture.service.match(
        [
            _item(
                "e1",
                kind=SourceItemKind.EPISODE,
                name="Kissed by Fire",
                provider_ids={"imdb": "tt2178782"},
            )
        ]
    )
    assert await fixture.titles.count_by_state() == dict.fromkeys(EnrichmentState, 0)


async def test_an_episode_is_not_enqueued_for_a_remote_search(fixture: _Fixture) -> None:
    """An episode is resolved by attaching it to its series (`IngestService`),
    never by searching TMDb for a title called "Kissed by Fire". Enqueueing
    one per episode is 999,827 jobs per walk that no handler can ever
    complete; `IngestService` enqueues a `match` job for the episodes whose
    series it genuinely could not resolve, which is a far smaller set."""
    await fixture.service.match([_item("e1", kind=SourceItemKind.EPISODE, name="Kissed by Fire")])
    assert await fixture.queue.claim([JobKind.MATCH], limit=10) == []


# -- malformed upstream data must not abort a walk --------------------------


async def test_a_non_numeric_tmdb_id_does_not_create_an_idless_stub(
    fixture: _Fixture,
) -> None:
    """A source is free to report `ProviderIds.Tmdb: "unknown"`. Creating a
    stub anyway produces a title carrying *no* provider id at all, from a
    bare name -- exactly what tier 5 is scoped to forbid, arriving through
    the back door."""
    outcomes = await fixture.service.match(
        [_item("m1", name="Home Video 2004", year=None, provider_ids={"tmdb": "unknown"})]
    )
    assert outcomes[0].method is MatchMethod.UNMATCHED
    assert outcomes[0].title_id is None
    assert await fixture.titles.count_by_state() == dict.fromkeys(EnrichmentState, 0)


async def test_a_malformed_imdb_id_does_not_abort_the_batch(fixture: _Fixture) -> None:
    """`Title.imdb_id` is pattern-validated (`^tt\\d{7,8}$`), so handing it a
    source's stray string raises `ValidationError` -- which is not a
    `UsherPortError`, so `ReconcileService` deliberately does not catch it
    and one bad row in 1,126,674 kills every sync of that source, forever.
    The item is matched on whatever else it carries and the unusable id is
    dropped."""
    outcomes = await fixture.service.match(
        [
            _item("m1", provider_ids={"imdb": "nonsense", "tmdb": "999999"}),
            _item("m2", name="Home Video 2004", year=None, provider_ids={"imdb": "tt-nope"}),
        ]
    )
    assert outcomes[0].method is MatchMethod.CREATED_STUB
    created = await fixture.titles.get(outcomes[0].title_id or uuid.UUID(int=0))
    assert created is not None
    assert created.imdb_id is None
    assert created.tmdb_id == 999999
    assert outcomes[1].method is MatchMethod.UNMATCHED


async def test_a_negative_year_does_not_abort_the_batch(fixture: _Fixture) -> None:
    """`Title.year` is `ge=0`. Same failure shape as the IMDb pattern above,
    and the same fix: the stub is created without the unusable field."""
    outcomes = await fixture.service.match([_item("m1", year=-1, provider_ids={"tmdb": "999999"})])
    assert outcomes[0].method is MatchMethod.CREATED_STUB
    created = await fixture.titles.get(outcomes[0].title_id or uuid.UUID(int=0))
    assert created is not None
    assert created.year is None


# -- losing a race ----------------------------------------------------------


class _BlindFirstLookup(FakeTitleMatchRepository):
    """Answers the first `match_by_provider_ids` of the process with nothing.

    That is the race, made deterministic: another worker committed the title
    between this batch's read and its write, so the ladder misses and the
    `add` conflicts. Nothing in a single-threaded fake can produce that
    interleaving on its own.
    """

    def __init__(self) -> None:
        super().__init__()
        self._blinded = False

    async def match_by_provider_ids(
        self, refs: Sequence[ProviderRef]
    ) -> dict[ProviderRef, uuid.UUID]:
        if not self._blinded:
            self._blinded = True
            return {}
        return await super().match_by_provider_ids(refs)


async def test_a_stub_that_loses_a_race_attaches_to_the_winner() -> None:
    """`RepositoryConflict.constraint` exists for exactly this: two workers
    creating the same stub, one losing on `ix_titles_tmdb_id_kind`. The
    loser must look up the winner and attach, not fail the item -- the
    alternative is an item in the review queue whose title exists."""
    fixture = _Fixture()
    existing = Title(kind=TitleKind.MOVIE, name="Winner", sort_name="Winner", tmdb_id=999999)
    await fixture.titles.add(existing)
    # The match repository does not know about it, so tier 1 misses and the
    # service tries to create -- which is the race, deterministically.
    outcomes = await fixture.service.match([_item("m1", provider_ids={"tmdb": "999999"})])
    assert outcomes[0].title_id == existing.id
    assert outcomes[0].method is MatchMethod.CREATED_STUB


async def test_a_tvdb_only_stub_that_loses_a_race_attaches_to_the_winner() -> None:
    """`TitleRepository` has no `get_by_tvdb_id`, and Emby series routinely
    carry a TVDb id and nothing else -- so a conflict handler built only
    from `get_by_tmdb_id`/`get_by_imdb_id` re-raises for the *common* series
    shape and fails the whole batch. The second-chance read goes back
    through `TitleMatchRepository`, which answers all three providers."""
    fixture = _Fixture(matching=_BlindFirstLookup())
    existing = Title(kind=TitleKind.SERIES, name="Winner", sort_name="Winner", tvdb_id=121361)
    await fixture.titles.add(existing)
    await fixture.matching.given_title(
        kind=TitleKind.SERIES, name="Winner", tvdb_id=121361, title_id=existing.id
    )
    outcomes = await fixture.service.match(
        [_item("s1", kind=SourceItemKind.SERIES, provider_ids={"tvdb": "121361"})]
    )
    assert outcomes[0].title_id == existing.id


async def test_a_stub_race_nobody_can_explain_is_raised_not_swallowed() -> None:
    """The residual. A conflict on a constraint no second read can resolve
    is a real inconsistency, and `ReconcileService` records it as a failed
    run with its message -- which is loud, safe, and self-healing on the
    next walk. Silently marking the item unmatched would hide a catalog
    Usher can no longer write to."""
    from usher.ports.errors import RepositoryConflict

    fixture = _Fixture(matching=_BlindFirstLookup())
    existing = Title(kind=TitleKind.SERIES, name="Winner", sort_name="Winner", tvdb_id=121361)
    await fixture.titles.add(existing)
    # Deliberately *not* seeded into the match repository, so the
    # second-chance read finds nothing either.
    with pytest.raises(RepositoryConflict):
        await fixture.service.match(
            [_item("s1", kind=SourceItemKind.SERIES, provider_ids={"tvdb": "121361"})]
        )
