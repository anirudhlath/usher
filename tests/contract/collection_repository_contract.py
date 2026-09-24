"""Behaviour every `CollectionRepository` implementation must satisfy."""

import uuid
from abc import ABC, abstractmethod

from usher.domain.collection import Collection
from usher.domain.ids import new_id
from usher.ports.repository import CollectionRepository


def collection(tmdb_id: int | None, name: str, **changes: object) -> Collection:
    return Collection.model_validate({"tmdb_id": tmdb_id, "name": name, **changes})


class CollectionSeeder(ABC):
    """Titles and ownership: the two things `CollectionRepository` cannot write."""

    @abstractmethod
    async def movie(self) -> uuid.UUID:
        """A film, returning its title id."""

    @abstractmethod
    async def series(self) -> uuid.UUID:
        """A series, returning its title id.

        `belongs_to_collection` is movies-only, so a series carrying a collection id is
        a defect.
        """

    @abstractmethod
    async def own(
        self, title_id: uuid.UUID, *, available: bool = True, as_episode: bool = False
    ) -> None:
        """A `media_items` row making this title owned.

        `as_episode` writes the row with an `episode_id` set, which is the population
        `list_owned`'s `episode_id IS NULL` clause excludes — most of a real library.
        """

    @abstractmethod
    async def collection_of(self, title_id: uuid.UUID) -> uuid.UUID | None:
        """Read `titles.collection_id` back.

        A test affordance, not a port method: `get` answers a whole franchise and its
        ownership, which is a different question from "which collection is this title
        in".
        """

    @abstractmethod
    async def force_collection(self, title_id: uuid.UUID, collection_id: uuid.UUID) -> None:
        """Link a title to a collection **without** going through `attach_titles`.

        The `kind = 'movie'` filter lives in `attach_titles`, so seeding a series
        through the port would only assert that the writer refused it. `titles`
        deliberately carries no `CHECK (collection_id IS NULL OR kind = 'movie')`, so
        such a row is storable and a reader that trusted the writer would put a series
        on a franchise page.
        """


class CollectionRepositoryContract:
    async def test_a_collection_is_updated_rather_than_duplicated_on_a_second_pass(
        self, repository: CollectionRepository
    ) -> None:
        """Rules out an upsert keyed on `Collection.id`.

        The derivation mints a fresh UUIDv7 per sighting, so that grows a duplicate
        franchise per pass — and a batch names one collection once per member film, so
        the duplicate arrives inside a single call rather than only across two.
        """
        first = await repository.upsert_many([collection(98_000_010, "An Invented Collection")])
        again = await repository.upsert_many([collection(98_000_010, "A Renamed Collection")])
        assert (first.inserted, first.updated) == (1, 0)
        assert (again.inserted, again.updated) == (0, 1)
        assert len(await repository.resolve_tmdb_ids([98_000_010])) == 1

    async def test_a_duplicate_collection_inside_one_batch_is_tolerated(
        self, repository: CollectionRepository
    ) -> None:
        """Deduplication is required rather than defensive.

        A batch names one franchise once per member film, so a two-film collection is
        already a duplicate before anything unusual has happened. Without
        `SELECT DISTINCT ON` the real implementation answers `CardinalityViolationError`.
        """
        result = await repository.upsert_many(
            [collection(98_000_011, "First Name"), collection(98_000_011, "Last Name")]
        )
        assert (result.inserted, result.updated) == (1, 0)

    async def test_resolve_omits_ids_it_does_not_have(
        self, repository: CollectionRepository
    ) -> None:
        """Absent means "no such collection", never "not asked".

        A resolve that mints an id for an unknown `tmdb_id` hands the derivation a
        `collection_id` no row carries -- accepted silently by a dict, and a foreign-key
        violation one statement later in Postgres.
        """
        await repository.upsert_many([collection(98_000_012, "An Invented Collection")])
        resolved = await repository.resolve_tmdb_ids([98_000_012, 98_000_013])
        assert set(resolved) == {98_000_012}

    async def test_attaching_a_collection_to_a_series_is_refused(
        self, repository: CollectionRepository, seeder: CollectionSeeder
    ) -> None:
        """Rules out writing `collection_id` onto a series from a movie's own franchise.

        Both halves are asserted in one batch: an implementation that refuses the whole
        batch when it sees a series also leaves the series untouched, so asserting only
        that would pass against a derivation that silently stops linking anything.
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
        """Rules out an unconditional `SET`.

        `titles` carries a stored generated tsvector and a GIN index, so an `UPDATE`
        that assigns regardless recomputes both per movie per derivation pass and leaves
        a dead row version for a value that did not change. Returning changed rather
        than touched is the only way that is observable. The first call's count also
        rules out `<>` in place of `IS DISTINCT FROM`: the stored value is NULL on a
        first attach, so `<>` writes nothing on exactly the pass that matters.
        """
        await repository.upsert_many([collection(98_000_015, "An Invented Collection")])
        collection_id = (await repository.resolve_tmdb_ids([98_000_015]))[98_000_015]
        movie_id = await seeder.movie()

        assert await repository.attach_titles([(movie_id, collection_id)]) == 1
        assert await repository.attach_titles([(movie_id, collection_id)]) == 0

    async def test_attaching_does_not_clear_links_outside_the_batch(
        self, repository: CollectionRepository, seeder: CollectionSeeder
    ) -> None:
        """Rules out a scoped write that is not scoped.

        One that NULLs every title it was not given unlinks the whole catalog the first
        time the derivation runs over one page.
        """
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
        """A collection with exactly one owned member is not a franchise row.

        It is a single film with a subtitle. Rules out `>= 1` in place of
        `>= min_owned`, with the one-owned collection seeded alongside a two-owned one
        so the wrong answer is longer rather than empty.
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
        """`ORDER BY e.owned_count DESC`, which needs two eligible collections to see.

        `Collection.id` is a UUIDv7 minted at validation time, so `ORDER BY c.id` is
        derivation order: under it the screen's franchise rows are decided by whichever
        franchise TMDb described first. The provider cannot recover that, because its
        score saturates at four owned members and two franchises above the ceiling tie.
        The distractor is seeded first so it carries the lower id.
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
        """Rules out two implementations at once, both of which read as working.

        A join on `media_items.title_id` alone reads the wrong population, since most of
        a library's rows are episode-level; one that ignores `available` counts an
        unavailable film as owned, overstating in the direction nobody checks. Seeded so
        the wrong answer clears the floor and the right one does not.
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
        """Rules out `title_ids` filtered to the owned subset.

        "You own 2 of 4" would read "2 of 2" — a completeness signal that always reads
        complete says nothing. `OwnedCollection` carries the two lists rather than two
        counts for this reason; the counts are `len()`, so they cannot disagree.
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

    async def test_a_collection_the_household_owns_one_of_is_still_readable_by_id(
        self, repository: CollectionRepository, seeder: CollectionSeeder
    ) -> None:
        """`list_owned` and `get` answer two different questions.

        `min_owned` keeps a one-owned franchise off the home screen, but asking for that
        collection by id is legitimate — the client followed a link from the film's own
        page. So the scoped read carries no `min_owned` at all, and re-applying it would
        404 the franchise a household has barely started. The premise is asserted rather
        than assumed: `list_owned()` really does exclude this collection.
        """
        await repository.upsert_many([collection(98_000_022, "Barely Started")])
        collection_id = (await repository.resolve_tmdb_ids([98_000_022]))[98_000_022]

        members = [await seeder.movie() for _ in range(4)]
        for member in members:
            await repository.attach_titles([(member, collection_id)])
        await seeder.own(members[0])

        assert [one.collection_id for one in await repository.list_owned()] == [], (
            "the premise: at one owned member this franchise is below list_owned's floor"
        )

        found = await repository.get(collection_id)
        assert found is not None
        assert found.collection_id == collection_id
        assert found.name == "Barely Started"
        assert found.owned_title_ids == frozenset({members[0]})
        assert set(found.title_ids) == set(members)
        assert len(found.title_ids) == 4

    async def test_a_collection_the_household_owns_none_of_is_a_real_answer(
        self, repository: CollectionRepository, seeder: CollectionSeeder
    ) -> None:
        """Zero owned is a fact, not an absence.

        The wrong implementation this kills is an inner join to `media_items`,
        under which a franchise nobody in the house owns any of is
        indistinguishable from a franchise that does not exist -- and the route
        above turns the second into a 404. "You own 0 of 7" is exactly the
        answer a client following a link from a film it *does* own needs.
        """
        await repository.upsert_many([collection(98_000_023, "Owned By Nobody")])
        collection_id = (await repository.resolve_tmdb_ids([98_000_023]))[98_000_023]
        members = [await seeder.movie() for _ in range(3)]
        for member in members:
            await repository.attach_titles([(member, collection_id)])

        found = await repository.get(collection_id)
        assert found is not None
        assert found.owned_title_ids == frozenset()
        assert len(found.title_ids) == 3

    async def test_a_scoped_read_counts_only_available_title_level_items(
        self, repository: CollectionRepository, seeder: CollectionSeeder
    ) -> None:
        """`owned` means an available, title-level media item in this statement too.

        The clause is written here rather than inherited from `list_owned`'s. Rules out
        a join on `media_items.title_id` alone, which reads the wrong population, and
        one that ignores `available`, which overstates. Collections hold only movies, so
        no episode can match today — which is exactly why the clause has to be written
        down rather than implied. Seeded so a wrong answer is longer than the right one.
        """
        await repository.upsert_many([collection(98_000_024, "Three Kinds Of Owned")])
        collection_id = (await repository.resolve_tmdb_ids([98_000_024]))[98_000_024]

        genuine = await seeder.movie()
        unavailable = await seeder.movie()
        episode_level = await seeder.movie()
        for member in (genuine, unavailable, episode_level):
            await repository.attach_titles([(member, collection_id)])
        await seeder.own(genuine)
        await seeder.own(unavailable, available=False)
        await seeder.own(episode_level, as_episode=True)

        found = await repository.get(collection_id)
        assert found is not None
        assert found.owned_title_ids == frozenset({genuine})
        assert len(found.title_ids) == 3

    async def test_a_series_that_got_a_collection_id_anyway_is_not_a_member(
        self, repository: CollectionRepository, seeder: CollectionSeeder
    ) -> None:
        """The reader filters series out too, not just `attach_titles`.

        `titles` deliberately carries no `CHECK (collection_id IS NULL OR kind =
        'movie')`, so the row is storable by anything else that touches the column, and
        `force_collection` writes it past the writer. The series is seeded owned, so an
        unfiltered read reports "you own 2 of 2" for one film and a television show.
        """
        await repository.upsert_many([collection(98_000_025, "One Film And A Series")])
        collection_id = (await repository.resolve_tmdb_ids([98_000_025]))[98_000_025]
        movie_id = await seeder.movie()
        series_id = await seeder.series()
        await repository.attach_titles([(movie_id, collection_id)])
        await seeder.force_collection(series_id, collection_id)
        assert await seeder.collection_of(series_id) == collection_id, (
            "the plant did not land: force_collection has to bypass attach_titles' kind filter"
        )
        await seeder.own(movie_id)
        await seeder.own(series_id)

        found = await repository.get(collection_id)
        assert found is not None
        assert tuple(found.title_ids) == (movie_id,)
        assert found.owned_title_ids == frozenset({movie_id})

    async def test_an_unknown_collection_id_is_none(self, repository: CollectionRepository) -> None:
        """`None` rather than an empty `OwnedCollection`.

        The route turns the two into different status codes: 404 for a franchise the
        catalog does not hold, 200 with `owned_count: 0` for one it holds and the
        household owns none of. An empty shell for both collapses them into one 200
        about nothing.
        """
        assert await repository.get(new_id()) is None

    async def test_an_empty_collection_batch_is_a_no_op(
        self, repository: CollectionRepository
    ) -> None:
        result = await repository.upsert_many([])
        assert (result.inserted, result.updated) == (0, 0)
        assert await repository.attach_titles([]) == 0
