"""Behaviour every `CollectionRepository` implementation must satisfy.

`FranchiseProvider`'s whole question is `list_owned`, and PRD 06's signal is
*"you own 2 of 4"* -- two numbers **and** the cards to render. Four of the
nine cases below are about the ways that sentence comes out wrong while still
rendering: a completeness signal that always reads complete, an unavailable
film counted as owned, a franchise you own one of, and a movie's collection
landing on a series.

**Every case names the wrong implementation it rules out.**

Subclass and provide `repository` and `seeder`. The seeder writes the two
things this port cannot -- titles, and the `media_items` rows that make a
title *owned* -- and its `ABC` shape is ADR-0001's argument applied to a test
double: a `Protocol` would let a subclass drift out of the suite silently.
"""

import uuid
from abc import ABC, abstractmethod

from usher.domain.collection import Collection
from usher.ports.repository import CollectionRepository


def collection(tmdb_id: int | None, name: str, **changes: object) -> Collection:
    return Collection.model_validate({"tmdb_id": tmdb_id, "name": name, **changes})


class CollectionSeeder(ABC):
    """Titles and ownership -- the two things `CollectionRepository` cannot
    write and every `list_owned` case needs."""

    @abstractmethod
    async def movie(self) -> uuid.UUID:
        """A film, returning its title id."""

    @abstractmethod
    async def series(self) -> uuid.UUID:
        """A series, returning its title id. Only one case needs one, and it
        is the case that matters: `belongs_to_collection` is movies-only, so a
        series carrying a collection id is a defect."""

    @abstractmethod
    async def own(
        self, title_id: uuid.UUID, *, available: bool = True, as_episode: bool = False
    ) -> None:
        """A `media_items` row making this title owned.

        `as_episode` writes the row with an `episode_id` set, which is the
        population `list_owned`'s `episode_id IS NULL` clause excludes --
        999,827 of the one measured deployment's 1,126,789 items.
        """

    @abstractmethod
    async def collection_of(self, title_id: uuid.UUID) -> uuid.UUID | None:
        """Read `titles.collection_id` back. A test affordance, not a port
        method: the port has no `get`, because `OwnedCollection` answers
        `FranchiseProvider` in one statement and a per-collection read would
        be an N+1 the port *offers*."""


class CollectionRepositoryContract:
    async def test_a_collection_is_updated_rather_than_duplicated_on_a_second_pass(
        self, repository: CollectionRepository
    ) -> None:
        """The wrong implementation this kills: an upsert keyed on
        `Collection.id`. The derivation mints a fresh UUIDv7 per sighting, so
        that grows a duplicate franchise per pass -- and a batch names one
        collection **once per member film**, so the duplicate arrives inside a
        single call rather than only across two.
        """
        first = await repository.upsert_many([collection(98_000_010, "An Invented Collection")])
        again = await repository.upsert_many([collection(98_000_010, "A Renamed Collection")])
        assert (first.inserted, first.updated) == (1, 0)
        assert (again.inserted, again.updated) == (0, 1)
        assert len(await repository.resolve_tmdb_ids([98_000_010])) == 1

    async def test_a_duplicate_collection_inside_one_batch_is_tolerated(
        self, repository: CollectionRepository
    ) -> None:
        """Required rather than defensive, and for a sharper reason than
        `people`'s: a batch names one franchise once per member film, so a
        two-film collection is already a duplicate before anything unusual has
        happened. Without `SELECT DISTINCT ON` the real implementation answers
        `CardinalityViolationError`."""
        result = await repository.upsert_many(
            [collection(98_000_011, "First Name"), collection(98_000_011, "Last Name")]
        )
        assert (result.inserted, result.updated) == (1, 0)

    async def test_resolve_omits_ids_it_does_not_have(
        self, repository: CollectionRepository
    ) -> None:
        """Absent means "no such collection", never "not asked". A resolve
        that mints an id for an unknown `tmdb_id` hands the derivation a
        `collection_id` no row carries -- accepted silently by a dict, and a
        foreign-key violation one statement later in Postgres."""
        await repository.upsert_many([collection(98_000_012, "An Invented Collection")])
        resolved = await repository.resolve_tmdb_ids([98_000_012, 98_000_013])
        assert set(resolved) == {98_000_012}

    async def test_attaching_a_collection_to_a_series_is_refused(
        self, repository: CollectionRepository, seeder: CollectionSeeder
    ) -> None:
        """The front matter's fourth named wrong implementation: writes
        `collection_id` onto a *series* from a movie's
        `belongs_to_collection`.

        **Both halves are asserted, in one batch, and that is the point.** An
        implementation that refuses the whole batch when it sees a series also
        leaves the series untouched, so a case asserting only that passes
        against a derivation that silently stops linking anything. The movie
        must be linked *and* the series must not.
        """
        await repository.upsert_many([collection(98_000_014, "An Invented Collection")])
        collection_id = (await repository.resolve_tmdb_ids([98_000_014]))[98_000_014]
        movie_id = await seeder.movie()
        series_id = await seeder.series()

        await repository.attach_titles([(movie_id, collection_id), (series_id, collection_id)])

        assert await seeder.collection_of(movie_id) == collection_id
        assert await seeder.collection_of(series_id) is None

    async def test_reattaching_an_unchanged_link_writes_nothing(
        self, repository: CollectionRepository, seeder: CollectionSeeder
    ) -> None:
        """The wrong implementation this kills: an unconditional `SET`.

        `titles` carries `search_document`, a stored generated tsvector
        measured at 4.06x on the write path, plus a GIN index -- so an
        `UPDATE` that assigns regardless recomputes both per movie per
        derivation pass and produces a dead row version for each, for a value
        that did not change. Returning *changed* rather than *touched* is the
        only way that is observable, so the assertion is on the count.

        The first call's count is asserted too, and it is the half that kills
        `<>` in place of `IS DISTINCT FROM`: the stored value is NULL on a
        first attach and `NULL <> :x` is NULL, so `<>` writes nothing on
        exactly the pass that matters and reports zero both times.
        """
        await repository.upsert_many([collection(98_000_015, "An Invented Collection")])
        collection_id = (await repository.resolve_tmdb_ids([98_000_015]))[98_000_015]
        movie_id = await seeder.movie()

        assert await repository.attach_titles([(movie_id, collection_id)]) == 1
        assert await repository.attach_titles([(movie_id, collection_id)]) == 0

    async def test_attaching_does_not_clear_links_outside_the_batch(
        self, repository: CollectionRepository, seeder: CollectionSeeder
    ) -> None:
        """The wrong implementation this kills: a scoped write that is not
        scoped -- one that NULLs every title it was not given, which unlinks
        the whole catalog the first time the derivation runs over one page."""
        await repository.upsert_many(
            [collection(98_000_016, "First Franchise"), collection(98_000_017, "Second Franchise")]
        )
        ids = await repository.resolve_tmdb_ids([98_000_016, 98_000_017])
        first_movie = await seeder.movie()
        second_movie = await seeder.movie()

        await repository.attach_titles([(first_movie, ids[98_000_016])])
        await repository.attach_titles([(second_movie, ids[98_000_017])])

        assert await seeder.collection_of(first_movie) == ids[98_000_016]

    async def test_a_collection_with_one_owned_member_is_absent(
        self, repository: CollectionRepository, seeder: CollectionSeeder
    ) -> None:
        """The front matter's per-provider distractor for Franchise: "a
        collection with exactly one owned member".

        A franchise you own one of is not a franchise row -- it is a single
        film with a subtitle. The wrong implementation this kills is `>= 1` in
        place of `>= min_owned`, and the one-owned collection is seeded
        alongside a two-owned one so the wrong answer is *longer* rather than
        empty.
        """
        await repository.upsert_many(
            [collection(98_000_018, "Owns Two"), collection(98_000_019, "Owns One")]
        )
        ids = await repository.resolve_tmdb_ids([98_000_018, 98_000_019])

        for _ in range(2):
            owned = await seeder.movie()
            await repository.attach_titles([(owned, ids[98_000_018])])
            await seeder.own(owned)
        lonely = await seeder.movie()
        await repository.attach_titles([(lonely, ids[98_000_019])])
        await seeder.own(lonely)

        listed = await repository.list_owned()
        assert [one.collection_id for one in listed] == [ids[98_000_018]]

    async def test_owned_collections_are_ranked_by_how_much_of_them_is_owned(
        self, repository: CollectionRepository, seeder: CollectionSeeder
    ) -> None:
        """`ORDER BY e.owned_count DESC` -- and deleting it **survived the
        whole suite** until this case existed.

        Every other case in this class returns at most one eligible
        collection, so the sort had nothing to order and `ORDER BY c.id` was
        indistinguishable from it. `Collection.id` is a UUIDv7 minted at
        validation time, so id order is derivation order: under the mutation
        the screen's franchise rows are decided by whichever franchise TMDb
        happened to describe first.

        **The provider cannot recover this.** `FranchiseProvider` reads with
        `limit=_CANDIDATES` and emits the first `_MAX_ROWS` that still have
        something unplayed, and its score *saturates* at four owned members --
        so two franchises above the ceiling tie on score and the SQL order is
        the only thing that decided which reached the screen.

        The distractor is "Owns Two", seeded first so it carries the lower id:
        a two-member franchise leading a shelf whose whole premise is "you own
        2 of 4" completeness.
        """
        await repository.upsert_many(
            [collection(98_000_034, "Owns Two"), collection(98_000_035, "Owns Four")]
        )
        ids = await repository.resolve_tmdb_ids([98_000_034, 98_000_035])
        assert ids[98_000_034] < ids[98_000_035], (
            "the fixture must make id order and owned-count order disagree"
        )

        for _ in range(2):
            owned = await seeder.movie()
            await repository.attach_titles([(owned, ids[98_000_034])])
            await seeder.own(owned)
        for _ in range(4):
            owned = await seeder.movie()
            await repository.attach_titles([(owned, ids[98_000_035])])
            await seeder.own(owned)

        listed = await repository.list_owned()
        assert [one.collection_id for one in listed] == [ids[98_000_035], ids[98_000_034]]

        capped = await repository.list_owned(limit=1)
        assert [one.collection_id for one in capped] == [ids[98_000_035]]

    async def test_owned_counts_only_available_title_level_items(
        self, repository: CollectionRepository, seeder: CollectionSeeder
    ) -> None:
        """Two wrong implementations at once, both of which read as working:
        a join on `media_items.title_id` alone, and one that ignores
        `available`.

        An unavailable film reads as owned under the second, so "you own 2 of
        4" is wrong in the direction nobody checks -- it overstates. And
        `media_items` holds 999,827 episode rows on the one measured
        deployment, so a join without `episode_id IS NULL` reads the wrong
        population entirely.

        Seeded so the wrong answer clears the floor and the right one does
        not: one genuinely owned member, one unavailable, one owned only
        through an episode-level row.
        """
        await repository.upsert_many([collection(98_000_020, "An Invented Collection")])
        collection_id = (await repository.resolve_tmdb_ids([98_000_020]))[98_000_020]

        genuine = await seeder.movie()
        unavailable = await seeder.movie()
        episode_level = await seeder.movie()
        for member in (genuine, unavailable, episode_level):
            await repository.attach_titles([(member, collection_id)])
        await seeder.own(genuine)
        await seeder.own(unavailable, available=False)
        await seeder.own(episode_level, as_episode=True)

        assert await repository.list_owned() == []

    async def test_a_collection_reports_members_it_does_not_own(
        self, repository: CollectionRepository, seeder: CollectionSeeder
    ) -> None:
        """The wrong implementation this kills: `title_ids` filtered to the
        owned subset, so "you own 2 of 4" reads "2 of 2" -- a completeness
        signal that always reads complete, which is a signal that says
        nothing.

        `OwnedCollection` carries the two lists rather than two counts for
        this reason; the counts are `len()`, so they cannot disagree with what
        they count.
        """
        await repository.upsert_many([collection(98_000_021, "An Invented Collection")])
        collection_id = (await repository.resolve_tmdb_ids([98_000_021]))[98_000_021]

        members = [await seeder.movie() for _ in range(4)]
        for member in members:
            await repository.attach_titles([(member, collection_id)])
        for member in members[:2]:
            await seeder.own(member)

        listed = await repository.list_owned()
        assert len(listed) == 1
        assert len(listed[0].title_ids) == 4
        assert listed[0].owned_title_ids == frozenset(members[:2])
        assert set(listed[0].title_ids) == set(members)

    async def test_an_empty_collection_batch_is_a_no_op(
        self, repository: CollectionRepository
    ) -> None:
        result = await repository.upsert_many([])
        assert (result.inserted, result.updated) == (0, 0)
        assert await repository.attach_titles([]) == 0
