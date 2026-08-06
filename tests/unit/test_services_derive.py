"""`DeriveService` against fakes: no database, no network, no clock.

Every collaborator is a port, which is the point -- ADR-0016's whole claim is
that these three entities come out of a payload M4 already cached, and the
way that stays true is structural rather than disciplinary.

**The central case is
`test_a_series_payload_does_not_attach_its_cast_to_a_movie_with_the_same_tmdb_id`**,
and it is the only one a derivation keyed on the bare integer fails. Every
other case here passes against that defect with the right counts, the right
people and the right idempotence -- which is this milestone's opening
argument arriving inside its own test suite.
"""

import uuid
from typing import Any

import pytest

from tests.fakes.collection_repository import FakeCollectionRepository
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.raw_payload_store import FakeRawPayloadStore
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import TitleKind
from usher.domain.title import Title
from usher.ports.errors import PortUnavailable
from usher.services.derive import DeriveService


def _cast(person_id: int, name: str, *, order: int) -> dict[str, Any]:
    return {
        "id": person_id,
        "name": name,
        "original_name": name,
        "known_for_department": "Acting",
        "character": "Nobody At All",
        "credit_id": f"{person_id:024d}",
        "order": order,
    }


class _Harness:
    """The five ports plus the commit callable, wired together.

    `credits` and `titles` share one store, and `credits` and `people` share
    another, for the reason `CLAUDE.md` records about
    `FakeTitleRepository`/`FakeTitleMatchRepository`: independent stores make
    a *correct* service fail rather than a wrong one pass.
    """

    def __init__(self) -> None:
        self.payloads = FakeRawPayloadStore()
        self.provider = FakeMetadataProvider()
        self.titles = FakeTitleRepository()
        self.people = FakePersonRepository()
        self.credits = FakeCreditRepository(self.people, self.titles)
        self.collections = FakeCollectionRepository()
        self.commits = 0
        self.service = DeriveService(
            payloads=self.payloads,
            provider=self.provider,
            titles=self.titles,
            people=self.people,
            credits=self.credits,
            collections=self.collections,
            commit=self._commit,
        )

    async def _commit(self) -> None:
        self.commits += 1

    async def given_title(
        self, *, kind: TitleKind, tmdb_id: int, name: str = "An Invented Film"
    ) -> uuid.UUID:
        title = Title(kind=kind, name=name, sort_name=name, tmdb_id=tmdb_id)
        await self.titles.add(title)
        self.collections.catalog.kinds[title.id] = kind
        self.collections.catalog.order.append(title.id)
        return title.id

    async def given_payload(self, kind: TitleKind, tmdb_id: int, payload: dict[str, Any]) -> None:
        await self.payloads.put("tmdb", kind.value, str(tmdb_id), payload)


@pytest.fixture
def harness() -> _Harness:
    return _Harness()


async def test_a_series_payload_does_not_attach_its_cast_to_a_movie_with_the_same_tmdb_id(
    harness: _Harness,
) -> None:
    """**This task's central case, and the only one that fails against the
    defect it names.**

    `raw_payloads` has no `title_id` and no foreign key to `titles`, so the
    join back is `(provider, kind, reference)` -- and the payload's own `id`
    field is the bare integer, sitting right there, inviting a resolution that
    drops the kind. ADR-0011: 26,968 measured TMDb ids are live in *both*
    spaces, and every one of them is a series whose cast would be written onto
    a film.

    A derivation keyed on the integer alone passes every other case in this
    file -- the counts are right, the people are right, idempotence holds --
    and produces a catalog where half the television has a film's cast. It
    raises nothing and renders identically to a correct one.
    """
    movie_id = await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000550, name="The Film")
    series_id = await harness.given_title(
        kind=TitleKind.SERIES, tmdb_id=90000550, name="The Series"
    )
    await harness.given_payload(
        TitleKind.MOVIE,
        90000550,
        {
            "id": 90000550,
            "title": "The Film",
            "credits": {"cast": [_cast(93000101, "Film Star", order=0)]},
        },
    )
    await harness.given_payload(
        TitleKind.SERIES,
        90000550,
        {
            "id": 90000550,
            "name": "The Series",
            "credits": {"cast": [_cast(93000102, "Series Star", order=0)]},
        },
    )

    await harness.service.derive_all()

    film_cast = [one.name for one in await harness.credits.list_for_title(movie_id)]
    series_cast = [one.name for one in await harness.credits.list_for_title(series_id)]
    assert film_cast == ["Film Star"]
    assert series_cast == ["Series Star"]


async def test_a_series_never_receives_a_collection_id(harness: _Harness) -> None:
    """`belongs_to_collection` is a field of `/movie/{id}` with no `/tv/{id}`
    counterpart, so a series row carrying one is a defect wherever it came
    from.

    **The movie is seeded in the same page on purpose**: a series derived
    alone passes trivially, because there is no collection anywhere to attach.
    The way this ships wrong is a walk that reads the collection once per
    *page* instead of once per payload, or a dict keyed on the bare `tmdb_id`
    again -- and both need a page holding one of each to be visible.
    """
    movie_id = await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000560)
    series_id = await harness.given_title(kind=TitleKind.SERIES, tmdb_id=90000561)
    await harness.given_payload(
        TitleKind.MOVIE,
        90000560,
        {
            "id": 90000560,
            "title": "The Film",
            "belongs_to_collection": {"id": 98000001, "name": "An Invented Collection"},
        },
    )
    await harness.given_payload(TitleKind.SERIES, 90000561, {"id": 90000561, "name": "The Series"})

    await harness.service.derive_all()

    linked = harness.collections.catalog.collection_ids
    assert linked.get(movie_id) is not None, "the movie is linked"
    assert linked.get(series_id) is None, "the series is not"


async def test_deriving_twice_leaves_one_row_per_credit(harness: _Harness) -> None:
    """PRD 08's redelivery rule, and `JobWorker.startup()` requeues everything
    left `running`, so this is ordinary rather than hypothetical.

    Idempotence comes from two mechanisms and this case sees both: people
    dedupe on `tmdb_id` (never on `Person.id`, which the derivation mints
    fresh per sighting, so an id-keyed upsert grows a copy of every actor per
    pass), and credits are a scoped replace.
    """
    title_id = await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000570)
    await harness.given_payload(
        TitleKind.MOVIE,
        90000570,
        {
            "id": 90000570,
            "title": "The Film",
            "credits": {"cast": [_cast(93000111, "Someone Invented", order=0)]},
        },
    )

    await harness.service.derive_all()
    await harness.service.derive_all()

    assert len(await harness.credits.list_for_title(title_id)) == 1
    assert len(await harness.people.resolve_tmdb_ids([93000111])) == 1


async def test_a_credit_dropped_from_a_refreshed_payload_is_dropped_from_the_table(
    harness: _Harness,
) -> None:
    """The case that kills the `ON CONFLICT DO NOTHING` implementation, and
    nothing else does.

    An upsert is idempotent and it never deletes, so a miscredited actor
    corrected upstream survives in `credits` forever -- and therefore in
    `credit_names`, and therefore in `search_document`, so a search for the
    wrong actor keeps returning the film after the correction. Both
    idempotence cases above stay green against it.

    **Asserted on `credit_names` as well as on `credits`**, because those are
    the two things that can disagree and the array is what the search document
    reads.
    """
    title_id = await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000580)
    await harness.given_payload(
        TitleKind.MOVIE,
        90000580,
        {
            "id": 90000580,
            "title": "The Film",
            "credits": {
                "cast": [
                    _cast(93000121, "Correctly Credited", order=0),
                    _cast(93000122, "Wrongly Credited", order=1),
                ]
            },
        },
    )
    await harness.service.derive_all()
    assert len(await harness.credits.list_for_title(title_id)) == 2

    # The upstream correction.
    await harness.given_payload(
        TitleKind.MOVIE,
        90000580,
        {
            "id": 90000580,
            "title": "The Film",
            "credits": {"cast": [_cast(93000121, "Correctly Credited", order=0)]},
        },
    )
    await harness.service.derive_all()

    stored = [one.name for one in await harness.credits.list_for_title(title_id)]
    assert stored == ["Correctly Credited"]
    assert (await harness.titles.credit_names_for([title_id]))[title_id] == ("Correctly Credited",)


async def test_a_title_whose_credits_all_disappeared_is_cleared_not_skipped(
    harness: _Harness,
) -> None:
    """The row shape a re-derivation cannot repair, and the reason the delete
    scope is `title_ids` rather than the rows being written.

    A title that contributes *no* rows is invisible to a scope derived from
    the batch, so its stale credits and its stale `credit_names` survive every
    future derivation. `TitleNeighborRepository.replace` makes the identical
    argument one table over.
    """
    title_id = await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000590)
    await harness.given_payload(
        TitleKind.MOVIE,
        90000590,
        {
            "id": 90000590,
            "title": "The Film",
            "credits": {"cast": [_cast(93000131, "Someone Invented", order=0)]},
        },
    )
    await harness.service.derive_all()
    assert len(await harness.credits.list_for_title(title_id)) == 1

    await harness.given_payload(
        TitleKind.MOVIE, 90000590, {"id": 90000590, "title": "The Film", "credits": {"cast": []}}
    )
    await harness.service.derive_all()

    assert await harness.credits.list_for_title(title_id) == []
    assert (await harness.titles.credit_names_for([title_id]))[title_id] == ()


async def test_credit_names_holds_the_top_ten_billed_and_not_the_whole_cast(
    harness: _Harness,
) -> None:
    """Two cutoffs, and they are deliberately not the same number.

    `credits` stores cast where `order < 50`; `credit_names` stores the **top
    ten** plus every stored crew name. The gap is the point: `credit_names`
    feeds the embedding and the tsvector, and both degrade with length. Two
    hundred names at ~2 tokens each quadruples a ~115-token document and takes
    the film's own name from ~4% of the text to under 1%, and a class-B lexeme
    set twenty times the size of class A is one where a two-word query
    accumulates against the wrong thing.

    Seeded with thirty cast, `order` **descending** through the array, so an
    implementation that slices before sorting keeps the *worst* ten and one
    that slices after keeps the right ten. Both return ten names.
    """
    title_id = await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000600)
    await harness.given_payload(
        TitleKind.MOVIE,
        90000600,
        {
            "id": 90000600,
            "title": "The Film",
            "credits": {
                "cast": [
                    _cast(93000200 + order, f"Billed {order:02d}", order=order)
                    for order in reversed(range(30))
                ]
            },
        },
    )

    await harness.service.derive_all()

    names = (await harness.titles.credit_names_for([title_id]))[title_id]
    assert names == tuple(f"Billed {order:02d}" for order in range(10))
    assert len(await harness.credits.list_for_title(title_id, limit=100)) == 30


async def test_credit_names_puts_crew_after_the_billed_cast(harness: _Harness) -> None:
    """Order is the ranking. Cast first, in billing order, then crew -- a
    director ahead of the lead actor is a class-B ordering nobody chose."""
    title_id = await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000610)
    await harness.given_payload(
        TitleKind.MOVIE,
        90000610,
        {
            "id": 90000610,
            "title": "The Film",
            "credits": {
                "cast": [_cast(93000301, "Top Billed", order=0)],
                "crew": [
                    {
                        "id": 93000302,
                        "name": "A Director",
                        "original_name": "A Director",
                        "job": "Director",
                        "department": "Directing",
                        "credit_id": f"{93000302:024d}",
                    }
                ],
            },
        },
    )

    await harness.service.derive_all()

    assert (await harness.titles.credit_names_for([title_id]))[title_id] == (
        "Top Billed",
        "A Director",
    )


async def test_a_payload_naming_no_title_in_the_catalog_is_skipped_rather_than_raising(
    harness: _Harness,
) -> None:
    """`raw_payloads` outlives `titles` -- there is no foreign key between
    them -- so a payload for a title deleted since the fetch is ordinary, not
    poison.

    Same call `IndexService` makes: *a job for work that has since become
    impossible completes rather than parks*. An implementation that raised
    would let one deleted title abort a whole derivation page, and the page
    after it, forever.
    """
    kept = await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000620)
    await harness.given_payload(
        TitleKind.MOVIE,
        90000620,
        {
            "id": 90000620,
            "title": "The Film",
            "credits": {"cast": [_cast(93000401, "Someone Invented", order=0)]},
        },
    )
    await harness.given_payload(
        TitleKind.MOVIE,
        90000621,
        {
            "id": 90000621,
            "title": "A Deleted Film",
            "credits": {"cast": [_cast(93000402, "Nobody At All", order=0)]},
        },
    )

    report = await harness.service.derive_all()

    assert len(await harness.credits.list_for_title(kept)) == 1
    assert report.payloads_read == 2
    assert report.titles_derived == 1


async def test_deriving_makes_no_provider_fetch(harness: _Harness) -> None:
    """ADR-0016's whole claim, asserted rather than described.

    That ADR kept `raw_payloads` because *"three later milestones re-derive
    entities from a payload M4 already holds -- with no second network
    call"*, and `ports/metadata.py` wrote the same note from the other end.
    This is where the note is presented.

    The provider is held for `to_derivation` alone, and `to_derivation` is a
    pure function. A derivation that called `fetch` would re-request the whole
    enriched tier against a rate limit to read data already sitting in a JSONB
    column -- so the fake is armed to raise, and the assertion is on the call
    count as well, because a `fetch` inside a `try` would swallow the raise
    and still spend the request.
    """
    await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000630)
    await harness.given_payload(
        TitleKind.MOVIE,
        90000630,
        {
            "id": 90000630,
            "title": "The Film",
            "credits": {"cast": [_cast(93000501, "Someone Invented", order=0)]},
        },
    )
    harness.provider.fail_with(PortUnavailable("the provider must not be reached"))

    await harness.service.derive_all()

    assert harness.provider.fetches == 0
    assert harness.provider.searches == 0


async def test_deriving_an_empty_cache_writes_nothing_and_reports_zero(
    harness: _Harness,
) -> None:
    """PRD 08's rule at the service rather than at the CLI: *every one of them
    has to work against an empty database*. The report is all zeroes and
    nothing raises -- and no page is committed, because there was no page."""
    report = await harness.service.derive_all()

    assert report.payloads_read == 0
    assert report.titles_derived == 0
    assert report.people_written == 0
    assert report.collections_written == 0
    assert harness.commits == 0


async def test_a_page_is_one_transaction_and_a_walk_is_several(harness: _Harness) -> None:
    """One transaction per **page**, not per title and not per run.

    Per title is a round trip per row of a walk over the whole cache. Per run
    holds one transaction open for the length of a full derivation, and its
    locks with it. The page is `iterate`'s page, which is also the unit
    `after` advances by, so a process killed mid-walk resumes at a page
    boundary and re-derives at most one page -- free, because the write is a
    replace.
    """
    for index in range(4):
        await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000640 + index)
        await harness.given_payload(
            TitleKind.MOVIE, 90000640 + index, {"id": 90000640 + index, "title": f"Film {index}"}
        )

    await harness.service.derive_all(page_size=2)

    assert harness.commits == 2, "four payloads at a page of two is two transactions"


async def test_a_limit_stops_the_walk_without_draining_the_cache(harness: _Harness) -> None:
    """`usher derive --backfill --limit N` is an operator bounding a run on a
    box they care about, and a limit that only bounded the *report* would be a
    flag that reads like a bound and is not."""
    for index in range(6):
        await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000660 + index)
        await harness.given_payload(
            TitleKind.MOVIE, 90000660 + index, {"id": 90000660 + index, "title": f"Film {index}"}
        )

    report = await harness.service.derive_all(page_size=2, limit=3)

    assert report.payloads_read == 4, "the page that crosses the limit is finished, not truncated"


async def test_deriving_one_title_reads_one_key_and_not_the_whole_cache(
    harness: _Harness,
) -> None:
    """The `derive` job's unit of work, and what makes it a `JobKind` at all.

    Everything a derivation needs for title X is in exactly one row of
    `raw_payloads`, found by one key -- `("tmdb", X.kind.value,
    str(X.tmdb_id))`. No other title's payload is read and no aggregate is
    computed, which is the test `SimilarityService`'s rebuild fails and why
    that one is deliberately not a kind.
    """
    title_id = await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000670)
    await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000671)
    await harness.given_payload(
        TitleKind.MOVIE,
        90000670,
        {
            "id": 90000670,
            "title": "The Film",
            "credits": {"cast": [_cast(93000601, "Someone Invented", order=0)]},
        },
    )
    await harness.given_payload(
        TitleKind.MOVIE,
        90000671,
        {
            "id": 90000671,
            "title": "Another Film",
            "credits": {"cast": [_cast(93000602, "Somebody Else", order=0)]},
        },
    )

    await harness.service.derive(title_id)

    assert len(await harness.credits.list_for_title(title_id)) == 1
    assert (
        await harness.credits.list_for_title(
            (await harness.titles.resolve_tmdb_ids(TitleKind.MOVIE, [90000671]))[90000671]
        )
        == []
    )


async def test_deriving_a_title_with_no_cached_payload_completes_rather_than_raising(
    harness: _Harness,
) -> None:
    """A title enriched before `credits` joined `*_APPEND_TO_RESPONSE`, or one
    whose cache entry was never written. Nothing to derive is not a failure --
    parking it would need a human to release work whose only problem is that
    there is none."""
    title_id = await harness.given_title(kind=TitleKind.MOVIE, tmdb_id=90000680)
    await harness.service.derive(title_id)
    assert await harness.credits.list_for_title(title_id) == []


async def test_deriving_a_title_the_catalog_no_longer_holds_completes(
    harness: _Harness,
) -> None:
    """`handlers.py`'s standing rule, at the third kind that needs it: a job
    for work that has since become impossible completes rather than parks."""
    await harness.service.derive(uuid.uuid4())


async def test_deriving_a_title_with_no_tmdb_id_completes(harness: _Harness) -> None:
    """A stub minted from an Emby item carrying only an IMDb id has no TMDb
    reference at all, so there is no cache key to read. 979,401 of the one
    measured catalog's 1,271,138 titles carry no `tmdb_id`; this is the common
    case, not the odd one."""
    title = Title(kind=TitleKind.MOVIE, name="A Stub", sort_name="A Stub")
    await harness.titles.add(title)
    await harness.service.derive(title.id)
    assert await harness.credits.list_for_title(title.id) == []
