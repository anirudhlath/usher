"""Behaviour every `PersonRepository` implementation must satisfy.

PRD 02: *"People are canonical entities, so 'more from this director' is a
join rather than a string match."* Every case here is about one of the two
ways that claim fails -- an identity that collapses two people into one, or a
recurrence count that ranks the wrong person first.

**Every case names the wrong implementation it rules out**, which is the rule
M6 put on `adapters/search/postgres.py` and which this milestone applies to
nine providers at once: a test whose docstring cannot name what it kills is a
test that kills nothing.

Subclass and provide `repository` plus a `seeder`. The seeder exists because
`list_recurring_for_user` reads four tables this port cannot write --
`watch_states`, `episodes`, `credits` and `titles` -- so the suite has to be
able to build a household's history through something. Its `ABC` shape is
deliberate: a `Protocol` would let a subclass drift out of the suite silently,
which is ADR-0001's argument applied to a test double.
"""

import uuid
from abc import ABC, abstractmethod

from usher.domain.people import CreditKind, Person
from usher.ports.repository import PersonRepository


def person(tmdb_id: int | None, name: str, **changes: object) -> Person:
    return Person.model_validate({"tmdb_id": tmdb_id, "name": name, "sort_name": name, **changes})


class PersonHistorySeeder(ABC):
    """Everything a `PersonRepositoryContract` case needs and the port cannot
    write.

    Deliberately not "give me a session": the fake has no session, and a
    seeder shaped around one would make the whole suite Postgres-only, which
    is the opposite of what a contract suite is for.
    """

    @abstractmethod
    async def movie(self) -> uuid.UUID:
        """A film, returning its title id."""

    @abstractmethod
    async def series_with_episodes(self, count: int) -> tuple[uuid.UUID, list[uuid.UUID]]:
        """A series and `count` of its episodes, returning `(title_id,
        episode_ids)`. The episodes are what an episode-level watch state
        names, and their series is what the credit hangs off."""

    @abstractmethod
    async def credit(
        self,
        *,
        person_id: uuid.UUID,
        title_id: uuid.UUID,
        kind: CreditKind = CreditKind.CAST,
        job: str | None = None,
        character: str | None = None,
    ) -> None:
        """One credit row.

        `character` is here so a case can seed several credits that land in
        **one** `(person, kind, job)` group -- one actor playing three parts,
        which TMDb genuinely emits. Two credits differing by `job` do *not*
        share a group, so they cannot express the count defect at all; see
        `test_recurring_people_are_ranked_by_distinct_watched_titles`.
        """

    @abstractmethod
    async def stored(self, person_id: uuid.UUID) -> Person:
        """Read one stored person back.

        A test affordance and deliberately **not** a port method: Task 6
        settled that `PersonRepository` has no `get(person_id)`, because
        nothing in M7 reads one person by id and `GET /people/{id}` is M9's.
        The suite still has to assert on a stored column -- `COALESCE` on
        `known_for_department` is unobservable otherwise, since
        `RecurringPerson` does not carry it -- so the read-back lives here,
        where it costs the shipped surface nothing.
        """

    @abstractmethod
    async def watched(
        self,
        *,
        user_id: uuid.UUID,
        title_id: uuid.UUID | None = None,
        episode_id: uuid.UUID | None = None,
        played: bool = True,
    ) -> None:
        """One watch state.

        `title_id` and `episode_id` are mutually exclusive, matching
        `ck_watch_states_exactly_one_target` and
        `WatchStateSyncService._watch_target`'s collapse: an episode's row is
        `(episode_id = ..., title_id = NULL)`.
        """


class PersonRepositoryContract:
    async def test_two_people_who_share_a_name_are_two_people(
        self, repository: PersonRepository
    ) -> None:
        """The wrong implementation this kills: dedupes by `name` rather than
        by `tmdb_id`, collapsing two directors who share one.

        ADR-0003 is the rule -- identity is Usher's own UUIDv7 and `tmdb_id`
        is an indexed attribute, never identity -- and this is what it buys. A
        name-keyed implementation returns one id here, every credit for both
        directors hangs off one person, and the result renders as a plausible,
        populated, wrong "more from this director" row.
        """
        await repository.upsert_many(
            [person(93_000_010, "Another Invention"), person(93_000_011, "Another Invention")]
        )
        resolved = await repository.resolve_tmdb_ids([93_000_010, 93_000_011])
        assert len(set(resolved.values())) == 2

    async def test_a_person_is_updated_rather_than_duplicated_on_a_second_pass(
        self, repository: PersonRepository
    ) -> None:
        """Keyed on `tmdb_id`, not on `Person.id`. The derivation mints a
        fresh UUIDv7 per sighting exactly as ingest does for seasons, so an
        id-keyed upsert inserts a duplicate row per pass and the catalog grows
        a copy of every actor every time `usher derive` runs.

        The rename assertion is the second half and kills the mirror mistake:
        `name = COALESCE(excluded.name, people.name)`, under which a corrected
        name is unfixable. `name` is `NOT NULL` and always supplied, so there
        is no null to preserve.
        """
        first = await repository.upsert_many([person(93_000_012, "Someone Invented")])
        again = await repository.upsert_many([person(93_000_012, "Someone Renamed")])
        assert (first.inserted, first.updated) == (1, 0)
        assert (again.inserted, again.updated) == (0, 1)
        resolved = await repository.resolve_tmdb_ids([93_000_012])
        assert len(resolved) == 1

    async def test_upsert_never_blanks_a_known_for_department(
        self, repository: PersonRepository, seeder: PersonHistorySeeder
    ) -> None:
        """The `COALESCE` rule, and here it is **required rather than
        defensive** -- which is the difference from `upsert_seasons`, where it
        guards against a later walk.

        Measured against the recorded payloads: a `credits.cast[]` entry
        carries `known_for_department` and a `created_by[]` entry does not. So
        the same person arrives with it and without it *inside one derivation
        pass over one series*, and an unconditional assignment blanks an
        actor's department the moment they also created a show -- silently, on
        a field `PeopleProvider` reads.

        Read back through the seeder rather than through a `get`, because
        there is no `get`: nothing in M7 reads one person by id and
        `GET /people/{id}` is M9's, and `RecurringPerson` does not carry this
        column. A port method added only so a test could assert is the
        liability `SearchIndex`' docstring already refuses.
        """
        await repository.upsert_many(
            [person(93_000_013, "An Invented Creator", known_for_department="Directing")]
        )
        await repository.upsert_many([person(93_000_013, "An Invented Creator")])

        resolved = await repository.resolve_tmdb_ids([93_000_013])
        stored = await seeder.stored(resolved[93_000_013])
        assert stored.known_for_department == "Directing"

    async def test_recurring_people_are_ranked_by_distinct_watched_titles(
        self,
        repository: PersonRepository,
        seeder: PersonHistorySeeder,
        user_id: uuid.UUID,
    ) -> None:
        """The front matter's ranking failure, seeded so the wrong answer is
        confident rather than empty.

        Person A has **three credits on one watched title** -- one actor
        playing three parts, which TMDb genuinely emits. Person B has one
        credit on each of two watched titles. `count(DISTINCT c.title_id)`
        scores A at 1, below the recurrence floor, and B at 2;
        `count(*)` scores A at 3 and returns a fully populated row, ranked
        first, about somebody the household has seen once.

        A is the distractor and the assertion is on **position**, never
        membership: `assert b in {...}` is satisfied by returning both in
        physical order.

        **The three credits differ by `character` and not by `job`, and that
        is the whole of whether this case works.** The read groups by
        `(person_id, name, kind, job)`, so "a writer who is also a producer, a
        director and an editor" -- the seeding the milestone plan specifies
        here -- lands in **four separate groups of one row each**, where
        `count(*)` and `count(DISTINCT title_id)` agree exactly and the case
        cannot tell them apart. Measured: injected into the fake, the
        `count(*)` defect survived this suite entirely under that seeding.
        Several parts in one film share one group and are the shape that
        discriminates.
        """
        await repository.upsert_many(
            [
                person(93_000_020, "Three Parts One Film"),
                person(93_000_021, "One Part Two Films"),
            ]
        )
        ids = await repository.resolve_tmdb_ids([93_000_020, 93_000_021])
        crowded, spread = ids[93_000_020], ids[93_000_021]

        first_film = await seeder.movie()
        second_film = await seeder.movie()
        for part in ("A Twin", "The Other Twin", "Their Double"):
            await seeder.credit(person_id=crowded, title_id=first_film, character=part)
        for film in (first_film, second_film):
            await seeder.credit(person_id=spread, title_id=film, character="A Detective")
        await seeder.watched(user_id=user_id, title_id=first_film)
        await seeder.watched(user_id=user_id, title_id=second_film)

        ranked = await repository.list_recurring_for_user(user_id)
        assert ranked, "the household watched two films with credits; the row must not be empty"
        assert ranked[0].person_id == spread
        assert ranked[0].watched_title_count == 2
        assert crowded not in {one.person_id for one in ranked}, (
            "three parts in one film is one distinct title, which is below min_titles"
        )

    async def test_watched_episodes_of_one_series_count_as_one_title(
        self,
        repository: PersonRepository,
        seeder: PersonHistorySeeder,
        user_id: uuid.UUID,
    ) -> None:
        """Twelve watched episodes of one series is one title, not twelve.

        The wrong implementation this kills: a count over rows joined from
        `watch_states`, which -- on a library where 999,827 of 1,126,674 items
        are episodes -- makes every series regular the household's most
        "recurring" person by an order of magnitude.

        Seeded against a distractor that would otherwise be beaten: a person
        genuinely on two watched films. Under `count(*)` the series regular
        scores twelve and wins; under `count(DISTINCT c.title_id)` they score
        one, fall below `min_titles`, and are absent entirely.
        """
        await repository.upsert_many(
            [person(93_000_022, "A Series Regular"), person(93_000_023, "On Two Films")]
        )
        ids = await repository.resolve_tmdb_ids([93_000_022, 93_000_023])
        regular, spread = ids[93_000_022], ids[93_000_023]

        series_id, episode_ids = await seeder.series_with_episodes(12)
        await seeder.credit(person_id=regular, title_id=series_id)
        for episode_id in episode_ids:
            await seeder.watched(user_id=user_id, episode_id=episode_id)

        first_film = await seeder.movie()
        second_film = await seeder.movie()
        for film in (first_film, second_film):
            await seeder.credit(person_id=spread, title_id=film)
            await seeder.watched(user_id=user_id, title_id=film)

        ranked = await repository.list_recurring_for_user(user_id)
        counts = {one.person_id: one.watched_title_count for one in ranked}
        assert counts.get(regular) is None, (
            "one series watched twelve times is one distinct title, which is below min_titles"
        )
        assert ranked[0].person_id == spread
        assert ranked[0].watched_title_count == 2

    async def test_an_episode_watch_state_reaches_its_series_credits(
        self,
        repository: PersonRepository,
        seeder: PersonHistorySeeder,
        user_id: uuid.UUID,
    ) -> None:
        """The wrong implementation this kills: reading only
        `watch_states.title_id`.

        An episode-level watch state carries `title_id IS NULL` and an
        `episode_id`; the series is on `episodes.title_id`. Without the join
        arm the implementation returns a People row about **films only**,
        which on a mostly-television library is populated, correctly shaped,
        and about the wrong half of the catalog. **A suite whose fixtures are
        all movies scores that implementation green**, which is why this case
        seeds no title-level watch state at all.

        Two series rather than one, because the recurrence floor is two
        distinct titles -- so a film-only implementation returns *nothing*
        here rather than something smaller.
        """
        await repository.upsert_many([person(93_000_024, "A Television Actor")])
        person_id = (await repository.resolve_tmdb_ids([93_000_024]))[93_000_024]

        for _ in range(2):
            series_id, episode_ids = await seeder.series_with_episodes(1)
            await seeder.credit(person_id=person_id, title_id=series_id)
            await seeder.watched(user_id=user_id, episode_id=episode_ids[0])

        ranked = await repository.list_recurring_for_user(user_id)
        assert [one.person_id for one in ranked] == [person_id], (
            "an episode's watch state carries title_id IS NULL; its series is on episodes.title_id"
        )
        assert ranked[0].watched_title_count == 2

    async def test_another_users_history_does_not_leak(
        self,
        repository: PersonRepository,
        seeder: PersonHistorySeeder,
        user_id: uuid.UUID,
        other_user_id: uuid.UUID,
    ) -> None:
        """Seeds the *other* user with the larger history, so an
        implementation ignoring `user_id` returns more rather than fewer --
        the failure that reads as working.

        This user has watched one film; the other has watched three, all
        credited to a person this user has never seen. An unscoped read
        returns that person at the top with a count of three.
        """
        await repository.upsert_many(
            [person(93_000_025, "Mine"), person(93_000_026, "Somebody Elses")]
        )
        ids = await repository.resolve_tmdb_ids([93_000_025, 93_000_026])
        mine, theirs = ids[93_000_025], ids[93_000_026]

        for _ in range(2):
            film = await seeder.movie()
            await seeder.credit(person_id=mine, title_id=film)
            await seeder.watched(user_id=user_id, title_id=film)
        for _ in range(3):
            film = await seeder.movie()
            await seeder.credit(person_id=theirs, title_id=film)
            await seeder.watched(user_id=other_user_id, title_id=film)

        ranked = await repository.list_recurring_for_user(user_id)
        assert [one.person_id for one in ranked] == [mine]

    async def test_an_unplayed_watch_state_is_not_history(
        self,
        repository: PersonRepository,
        seeder: PersonHistorySeeder,
        user_id: uuid.UUID,
    ) -> None:
        """`played` is the predicate, not "has a watch state". A row with
        `played = false, position_seconds = 0` is a state a sync created and
        nobody watched -- the same distinction `ContinueWatchingProvider`'s
        distractor is about, one provider over.

        The person is credited on three films and the user has a watch state
        for all three, but only one is played -- so `WHERE played` dropped
        gives a count of three and a row that survives the floor, while the
        correct answer is one and the person is absent.
        """
        await repository.upsert_many([person(93_000_027, "Never Actually Watched")])
        person_id = (await repository.resolve_tmdb_ids([93_000_027]))[93_000_027]

        films = [await seeder.movie() for _ in range(3)]
        for index, film in enumerate(films):
            await seeder.credit(person_id=person_id, title_id=film)
            await seeder.watched(user_id=user_id, title_id=film, played=index == 0)

        assert await repository.list_recurring_for_user(user_id) == []

    async def test_a_person_below_the_recurrence_floor_is_absent(
        self,
        repository: PersonRepository,
        seeder: PersonHistorySeeder,
        user_id: uuid.UUID,
    ) -> None:
        """PRD 06's word is *recurring*, and one appearance is not a
        recurrence. The front matter's distractor for this provider is exactly
        this: "a person with one credit, against one with four".

        The one-film person is seeded **second**, so an implementation with
        `>= 1` in place of `>= min_titles` returns them in a position an
        ordering-blind assertion would still accept -- the assertion is on the
        exact list, not on membership.
        """
        await repository.upsert_many(
            [person(93_000_028, "On Four Films"), person(93_000_029, "On One Film")]
        )
        ids = await repository.resolve_tmdb_ids([93_000_028, 93_000_029])
        recurring, once = ids[93_000_028], ids[93_000_029]

        films = [await seeder.movie() for _ in range(4)]
        for film in films:
            await seeder.credit(person_id=recurring, title_id=film)
            await seeder.watched(user_id=user_id, title_id=film)
        await seeder.credit(person_id=once, title_id=films[0])

        ranked = await repository.list_recurring_for_user(user_id)
        assert [one.person_id for one in ranked] == [recurring]
        assert ranked[0].watched_title_count == 4

    async def test_resolve_omits_ids_it_does_not_have(self, repository: PersonRepository) -> None:
        """Absent means "no such person", never "not asked".

        The wrong implementation this kills: a resolve that mints an id for an
        unknown `tmdb_id`. That hands the derivation a `person_id` no row
        carries -- which the fake accepts silently and Postgres rejects with a
        foreign-key violation one statement later, so the defect ships out of
        a green unit run.
        """
        await repository.upsert_many([person(93_000_014, "Someone Invented")])
        resolved = await repository.resolve_tmdb_ids([93_000_014, 93_000_015])
        assert set(resolved) == {93_000_014}

    async def test_a_duplicate_person_inside_one_batch_is_tolerated(
        self, repository: PersonRepository, seeder: PersonHistorySeeder
    ) -> None:
        """One derivation pass spans many titles and a working actor is on
        several of them, so this is the common case rather than the odd one.
        Without `SELECT DISTINCT ON` the real implementation answers
        `CardinalityViolationError: ON CONFLICT DO UPDATE command cannot
        affect row a second time`. Last-wins, matching the port's stated rule.
        """
        result = await repository.upsert_many(
            [person(93_000_016, "First"), person(93_000_016, "Last")]
        )
        assert (result.inserted, result.updated) == (1, 0)
        resolved = await repository.resolve_tmdb_ids([93_000_016])
        assert (await seeder.stored(resolved[93_000_016])).name == "Last"

    async def test_two_people_with_no_tmdb_id_are_two_people(
        self, repository: PersonRepository
    ) -> None:
        """The partial index's other half, and the fake's easiest bug: a dict
        keyed on `tmdb_id` collapses every `None` onto one entry. In Postgres
        the property comes free from the index being *partial*."""
        result = await repository.upsert_many([person(None, "Nameless"), person(None, "Other")])
        assert (result.inserted, result.updated) == (2, 0)

    async def test_an_empty_person_batch_is_a_no_op(self, repository: PersonRepository) -> None:
        result = await repository.upsert_many([])
        assert (result.inserted, result.updated) == (0, 0)
