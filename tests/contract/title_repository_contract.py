"""Shared behavioural contract every `TitleRepository` implementation must satisfy.

The technique PRD 08 calls out for `SourceAdapter`: one parametrised test class.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import cast

import pytest

from usher.domain.enums import EnrichmentState, ProductionStatus, TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import (
    BrowseCursorPosition,
    BrowseSort,
    TitleGenres,
    TitleReference,
    TitleRepository,
)
from usher.ports.search import FilterNotSupported


class TitleRepositoryContract:
    @pytest.fixture
    def collection_id(self) -> uuid.UUID:
        """A collection id `test_add_then_get_round_trips` may store.

        Overridable, because `titles.collection_id` has a real foreign key to
        `collections`: a bare `new_id()` is a `ForeignKeyViolationError` against the
        real repository and passes silently against a fake that is a dict. Same shape
        `EpisodeRepositoryContract` has for `title_id` -- the fake takes the default,
        the Postgres subclass overrides it with the id of a row it seeded.
        """
        return new_id()

    async def test_add_then_get_round_trips(
        self, repo: TitleRepository, collection_id: uuid.UUID
    ) -> None:
        # Not `assert fetched == title`: that passes only against a fake that
        # preserves created_at/updated_at verbatim, so a freshly-constructed
        # Title round-trips byte-for-byte and the real repository does not.
        title = Title(
            kind=TitleKind.SERIES,
            tmdb_id=90001399,
            imdb_id="tt99000030",
            tvdb_id=91000030,
            name="A Synthetic Series",
            original_name="A Synthetic Series",
            sort_name="A Synthetic Series",
            year=2011,
            release_date=date(2011, 4, 17),
            end_year=2019,
            overview="Nine noble families fight for control of the mythical land of Westeros.",
            tagline="A Synthetic First Episode",
            runtime_minutes=57,
            status=ProductionStatus.ENDED,
            genres=("Drama", "Fantasy"),
            keywords=("dragon", "king"),
            original_language="en",
            spoken_languages=("en",),
            origin_countries=("US", "GB"),
            content_rating="TV-MA",
            tmdb_vote_average=8.4,
            tmdb_vote_count=22000,
            tmdb_popularity=369.5,
            collection_id=collection_id,
            enrichment_state=EnrichmentState.ENRICHED,
            enrichment_error=None,
            enriched_at=datetime(2024, 1, 1, tzinfo=UTC),
            field_provenance={"overview": "tmdb"},
        )
        await repo.add(title)
        fetched = await repo.get(title.id)
        assert fetched is not None
        assert fetched.model_dump(exclude={"created_at", "updated_at"}) == title.model_dump(
            exclude={"created_at", "updated_at"}
        )

    async def test_created_at_is_not_taken_from_the_caller(self, repo: TitleRepository) -> None:
        """Postgres is the authoritative clock for created_at.

        add()'s _to_row excludes it from the INSERT entirely, so the database's own
        server_default assigns it, never whatever the caller's Title happened to carry
        -- a stale retry, a deliberately backdated import, or (as here) a plain
        constructor call that hardcodes one.
        """
        backdated = datetime(2020, 1, 1, tzinfo=UTC)
        title = Title(
            kind=TitleKind.MOVIE,
            name="Dune",
            sort_name="Dune",
            created_at=backdated,
            updated_at=backdated,
        )
        await repo.add(title)
        fetched = await repo.get(title.id)
        assert fetched is not None
        assert fetched.created_at != backdated

    async def test_created_at_is_stable_across_updates(self, repo: TitleRepository) -> None:
        """Re-enrichment scheduling is built on updated_at.

        Which only means anything if created_at never moves once a title exists.
        Deliberately tampers with created_at on the incoming Title (not just leaves it
        untouched via evolve(), which would pass even without the fix, since evolve()
        never changes a field it isn't told to): update() must ignore it regardless,
        the same way the real repository's update() never looks at it.
        """
        title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
        await repo.add(title)
        first = await repo.get(title.id)
        assert first is not None
        tampered = first.evolve(
            enrichment_state=EnrichmentState.ENRICHED,
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        await repo.update(tampered)
        second = await repo.get(title.id)
        assert second is not None
        assert second.created_at == first.created_at

    async def test_add_rejects_a_duplicate_id(self, repo: TitleRepository) -> None:
        title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
        await repo.add(title)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.add(title)
        # Structured, not just "some conflict happened" -- a service can
        # branch on which constraint fired without parsing the message.
        assert exc_info.value.constraint == "pk_titles"

    async def test_add_rejects_a_duplicate_tmdb_id_of_the_same_kind(
        self, repo: TitleRepository
    ) -> None:
        """tmdb_id is unique *per kind*.

        Two movies claiming one TMDb movie id is still a conflict, and the constraint
        that fires is the composite index.

        The final assertion is about the message: "title {second.id} already exists" is
        false here -- `second`'s own id was never the problem, its tmdb_id collided with
        a *different* row. The message may still name `second.id` to say which add()
        call failed; claiming that id already exists is what is wrong.
        """
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=90000100)
        second = Title(
            kind=TitleKind.MOVIE, name="Dune (dup)", sort_name="Dune (dup)", tmdb_id=90000100
        )
        await repo.add(first)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.add(second)
        assert exc_info.value.constraint == "ix_titles_tmdb_id_kind"
        assert "already exists" not in str(exc_info.value)

    async def test_a_movie_and_a_series_may_share_a_tmdb_id(self, repo: TitleRepository) -> None:
        """TMDb ids are live in both namespaces at once.

        Under a single-column unique index this call raises RepositoryConflict and
        television loses its tmdb_id wholesale. Delete the `kind` column from the index
        and this fails.
        """
        movie = Title(kind=TitleKind.MOVIE, name="Pride", sort_name="Pride", tmdb_id=1)
        series = Title(kind=TitleKind.SERIES, name="Pride", sort_name="Pride", tmdb_id=1)
        await repo.add(movie)
        await repo.add(series)
        assert (await repo.get(movie.id)) is not None
        assert (await repo.get(series.id)) is not None

    async def test_add_rejects_a_duplicate_imdb_id(self, repo: TitleRepository) -> None:
        """Same property as the duplicate-tmdb_id case, for the imdb_id branch.

        Exercised separately rather than folded into a single parametrized case, so a
        typo swapping which field `_provider_id_conflict` (the fake) or Postgres (the
        real repository) actually checks can't pass by accident.
        """
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="tt99000100")
        second = Title(
            kind=TitleKind.MOVIE, name="Dune (dup)", sort_name="Dune (dup)", imdb_id="tt99000100"
        )
        await repo.add(first)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.add(second)
        assert exc_info.value.constraint == "ix_titles_imdb_id"
        assert "already exists" not in str(exc_info.value)

    async def test_add_rejects_a_duplicate_tvdb_id(self, repo: TitleRepository) -> None:
        """Same property, for the tvdb_id branch."""
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tvdb_id=91000030)
        second = Title(
            kind=TitleKind.MOVIE, name="Dune (dup)", sort_name="Dune (dup)", tvdb_id=91000030
        )
        await repo.add(first)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.add(second)
        assert exc_info.value.constraint == "ix_titles_tvdb_id"
        assert "already exists" not in str(exc_info.value)

    async def test_get_returns_none_for_unknown_id(self, repo: TitleRepository) -> None:
        assert await repo.get(new_id()) is None

    async def test_get_by_tmdb_id_disambiguates_by_kind(self, repo: TitleRepository) -> None:
        """Without the `kind` argument this method has no correct answer.

        When both namespaces hold the id, the Postgres implementation raised a raw
        sqlalchemy.exc.MultipleResultsFound straight out of the port, which `db is
        driven, not driving` exists to prevent.
        """
        movie = Title(
            kind=TitleKind.MOVIE, name="Fight Club", sort_name="Fight Club", tmdb_id=90000550
        )
        series = Title(kind=TitleKind.SERIES, name="Bron", sort_name="Bron", tmdb_id=90000550)
        await repo.add(movie)
        await repo.add(series)
        found_movie = await repo.get_by_tmdb_id(90000550, TitleKind.MOVIE)
        found_series = await repo.get_by_tmdb_id(90000550, TitleKind.SERIES)
        assert found_movie is not None and found_movie.id == movie.id
        assert found_series is not None and found_series.id == series.id

    async def test_get_by_tmdb_id_finds_the_title(self, repo: TitleRepository) -> None:
        title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=90000100)
        await repo.add(title)
        found = await repo.get_by_tmdb_id(90000100, TitleKind.MOVIE)
        assert found is not None
        assert found.id == title.id

    async def test_get_by_imdb_id_finds_the_title(self, repo: TitleRepository) -> None:
        title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="tt99000100")
        await repo.add(title)
        found = await repo.get_by_imdb_id("tt99000100")
        assert found is not None
        assert found.id == title.id

    async def test_get_by_tmdb_id_of_none_finds_nothing(self, repo: TitleRepository) -> None:
        """tmdb_id's own type is `int`, not `int | None`.

        But a caller holding a genuinely optional value can still reach this with `None`
        if it bypasses mypy at the call site (a stray `# type: ignore`, `cast`, ...).
        Both implementations compile "tmdb_id == None" straight through -- Postgres as
        `IS NULL`, the fake as a plain `==` -- which matches whichever null-provider-id
        title happens to come first, not "the title with this id": the opposite of what
        this method promises.
        """
        await repo.add(Title(kind=TitleKind.MOVIE, name="Home Video", sort_name="Home Video"))
        assert await repo.get_by_tmdb_id(None, TitleKind.MOVIE) is None  # type: ignore[arg-type]

    async def test_get_by_imdb_id_of_none_finds_nothing(self, repo: TitleRepository) -> None:
        """Same property as test_get_by_tmdb_id_of_none_finds_nothing, for imdb_id."""
        await repo.add(Title(kind=TitleKind.MOVIE, name="Home Video", sort_name="Home Video"))
        assert await repo.get_by_imdb_id(None) is None  # type: ignore[arg-type]

    async def test_titles_without_provider_ids_are_allowed(self, repo: TitleRepository) -> None:
        title = Title(kind=TitleKind.MOVIE, name="Home Video 1998", sort_name="Home Video 1998")
        await repo.add(title)
        assert (await repo.get(title.id)) is not None

    async def test_update_mutates_an_existing_title(self, repo: TitleRepository) -> None:
        title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
        await repo.add(title)
        enriched = title.evolve(enrichment_state=EnrichmentState.ENRICHED)
        await repo.update(enriched)
        fetched = await repo.get(title.id)
        assert fetched is not None
        assert fetched.enrichment_state is EnrichmentState.ENRICHED

    async def test_update_rejects_an_unknown_id(self, repo: TitleRepository) -> None:
        title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
        with pytest.raises(RepositoryNotFound):
            await repo.update(title)

    async def test_update_rejects_a_conflicting_tmdb_id_of_the_same_kind(
        self, repo: TitleRepository
    ) -> None:
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=1)
        second = Title(kind=TitleKind.MOVIE, name="Arrival", sort_name="Arrival", tmdb_id=2)
        await repo.add(first)
        await repo.add(second)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.update(second.evolve(tmdb_id=1))
        assert exc_info.value.constraint == "ix_titles_tmdb_id_kind"

    async def test_update_rejects_a_conflicting_imdb_id(self, repo: TitleRepository) -> None:
        """Same property as the conflicting-tmdb_id case, for the imdb_id branch.

        A separate case rather than a parametrized one, for the reason
        test_add_rejects_a_duplicate_imdb_id gives.
        """
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="tt99000100")
        second = Title(
            kind=TitleKind.MOVIE, name="Arrival", sort_name="Arrival", imdb_id="tt99000180"
        )
        await repo.add(first)
        await repo.add(second)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.update(second.evolve(imdb_id="tt99000100"))
        assert exc_info.value.constraint == "ix_titles_imdb_id"

    async def test_update_rejects_a_conflicting_tvdb_id(self, repo: TitleRepository) -> None:
        """Same property, for the tvdb_id branch."""
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tvdb_id=1)
        second = Title(kind=TitleKind.MOVIE, name="Arrival", sort_name="Arrival", tvdb_id=2)
        await repo.add(first)
        await repo.add(second)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.update(second.evolve(tvdb_id=1))
        assert exc_info.value.constraint == "ix_titles_tvdb_id"

    async def test_update_clearing_provider_ids_to_none_is_allowed(
        self, repo: TitleRepository
    ) -> None:
        """update() may clear a field to None/().

        Worth pinning separately from test_update_mutates_an_existing_title, since a
        naive fix for the conflict-detection cases above could plausibly treat None as
        just another value to compare, rejecting a clear as a false conflict between two
        titles that both have tmdb_id=None.
        """
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=1)
        second = Title(kind=TitleKind.MOVIE, name="Arrival", sort_name="Arrival", tmdb_id=2)
        await repo.add(first)
        await repo.add(second)
        await repo.update(first.evolve(tmdb_id=None, genres=()))
        fetched = await repo.get(first.id)
        assert fetched is not None
        assert fetched.tmdb_id is None
        assert fetched.genres == ()

    async def test_count_by_state_reports_the_catalog(self, repo: TitleRepository) -> None:
        for i in range(3):
            await repo.add(Title(kind=TitleKind.MOVIE, name=f"Film {i}", sort_name=f"Film {i}"))
        counts = await repo.count_by_state()
        assert counts[EnrichmentState.SKELETON] == 3
        assert counts[EnrichmentState.ENRICHED] == 0

    async def test_count_by_state_is_never_sparse(self, repo: TitleRepository) -> None:
        await repo.add(Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune"))
        counts = await repo.count_by_state()
        assert counts[EnrichmentState.ENRICHED] == 0
        assert counts[EnrichmentState.STUB] == 0
        assert set(counts) == set(EnrichmentState)

    async def test_resolve_tmdb_ids_keeps_the_two_id_spaces_apart(
        self, repo: TitleRepository
    ) -> None:
        """The kind-scoping rule arriving at the *reverse* lookup.

        Which is the direction a derivation gets wrong. `get_by_tmdb_id` already takes a
        kind and the case above pins it. What is new here is a walk that starts from a
        **payload** and has to find its title: `raw_payloads` has no `title_id`, the
        join back is `(provider, kind, reference)`, and the payload's own `id` field is
        the bare integer sitting right there. TMDb ids are live in both spaces, so a
        resolver keyed on the integer alone attaches a series' cast to a film -- with
        the right counts, the right people, and nothing to say so.

        The wrong implementation this kills is one that drops `kind` from its
        predicate. It is seeded with both spaces holding the same integer,
        because a resolver asked about one space in isolation answers
        correctly either way.
        """
        movie = Title(
            kind=TitleKind.MOVIE,
            name="The Quiet Vacuum",
            sort_name="The Quiet Vacuum",
            tmdb_id=90000550,
        )
        series = Title(
            kind=TitleKind.SERIES,
            name="A Quiet Signal",
            sort_name="A Quiet Signal",
            tmdb_id=90000550,
        )
        await repo.add(movie)
        await repo.add(series)

        assert await repo.resolve_tmdb_ids(TitleKind.MOVIE, [90000550]) == {90000550: movie.id}
        assert await repo.resolve_tmdb_ids(TitleKind.SERIES, [90000550]) == {90000550: series.id}

    async def test_resolve_tmdb_ids_answers_a_whole_page_in_one_call(
        self, repo: TitleRepository
    ) -> None:
        """A batch rather than one, for `PersonRepository.resolve_tmdb_ids`' reason.

        A derivation page is 500 payloads and a lookup per payload is the round-trip-
        per-item shape batching exists to remove.

        **An id naming no title is absent from the answer, never `None` and
        never an error.** `raw_payloads` outlives `titles` -- there is no
        foreign key between them -- so a payload for a title deleted since the
        fetch is ordinary, and the caller skips it. An implementation that
        raised would make one deleted title abort a whole derivation page.
        """
        first = Title(kind=TitleKind.MOVIE, name="One", sort_name="One", tmdb_id=90000601)
        second = Title(kind=TitleKind.MOVIE, name="Two", sort_name="Two", tmdb_id=90000602)
        await repo.add(first)
        await repo.add(second)

        found = await repo.resolve_tmdb_ids(TitleKind.MOVIE, [90000601, 90000602, 90000603])

        assert found == {90000601: first.id, 90000602: second.id}

    async def test_resolve_tmdb_ids_of_nothing_asks_nothing(self, repo: TitleRepository) -> None:
        """PRD 08's empty-database rule at the port.

        A derivation page whose payloads are all series reaches the movie branch with an
        empty list, and `WHERE tmdb_id = ANY('{}')` is a statement issued to learn
        nothing.
        """
        assert await repo.resolve_tmdb_ids(TitleKind.MOVIE, []) == {}


class TitleRepositoryOwnedContract:
    """`list_owned_by_tag`, the read two row providers are built on.

    A separate mixin because it is the one `TitleRepository` method whose
    answer depends on a table `titles` does not contain: a subclass must
    supply `own`, which makes a title playable the way its own backend spells
    it -- a set for the fake, a real `media_items` row for Postgres.

    Every case here seeds a **distractor the wrong implementation ranks
    first**, because the wrong implementations are all populated: a catalog
    read that forgets the ownership join returns twenty beautifully-shaped
    cards nobody can play, and one that forgets the ordering returns the right
    set in insertion order.
    """

    @pytest.fixture
    def repo(self) -> TitleRepository:  # pragma: no cover - supplied by subclasses
        raise NotImplementedError

    @pytest.fixture
    def own(self) -> object:  # pragma: no cover - supplied by subclasses
        raise NotImplementedError

    @staticmethod
    def _tagged(
        name: str,
        *,
        genres: tuple[str, ...] = (),
        keywords: tuple[str, ...] = (),
        popularity: float | None = None,
        vote_count: int | None = None,
        kind: TitleKind = TitleKind.MOVIE,
    ) -> Title:
        return Title(
            id=new_id(),
            kind=kind,
            name=name,
            sort_name=name.lower(),
            genres=genres,
            keywords=keywords,
            # The builder's own keyword names are test-local vocabulary and
            # stay; only the `Title` fields they feed moved.
            tmdb_popularity=popularity,
            tmdb_vote_count=vote_count,
            enrichment_state=EnrichmentState.ENRICHED,
        )

    async def test_an_unowned_title_is_absent_however_popular_it_is(
        self, repo: TitleRepository, own: object
    ) -> None:
        """**The distractor `SeasonalProvider`'s own case seeds**, one layer down.

        The *best* match in the catalog: highest popularity, exact genre, and no copy.
        The wrong implementation matches the predicate against the whole catalog, of
        which the household can play none, in a correctly-shaped and beautifully-themed
        row. It is `[0]` under that implementation and absent under this one, so the
        assertion is positional and cannot be satisfied by membership.
        """
        best = self._tagged("An Unowned Masterpiece", genres=("Horror",), popularity=99.0)
        owned = self._tagged("An Owned Horror", genres=("Horror",), popularity=1.0)
        await repo.add(best)
        await repo.add(owned)
        await own(owned.id)  # type: ignore[operator]

        rows = await repo.list_owned_by_tag(genre="Horror")

        assert [row.id for row in rows] == [owned.id]

    async def test_the_answer_is_ordered_by_popularity_rather_than_by_insertion(
        self, repo: TitleRepository, own: object
    ) -> None:
        """Insertion order is id order, because every id here is a UUIDv7.

        So a fixture seeded best-first is satisfied by `ORDER BY id` and by no ordering
        at all. These are seeded worst-first.
        """
        worst = self._tagged("Least", genres=("Horror",), popularity=1.0)
        middle = self._tagged("Middling", genres=("Horror",), popularity=5.0)
        best = self._tagged("Most", genres=("Horror",), popularity=9.0)
        for one in (worst, middle, best):
            await repo.add(one)
            await own(one.id)  # type: ignore[operator]

        rows = await repo.list_owned_by_tag(genre="Horror")

        assert [row.id for row in rows] == [best.id, middle.id, worst.id]

    async def test_vote_count_orders_titles_whose_popularity_is_unknown(
        self, repo: TitleRepository, own: object
    ) -> None:
        """`titles.tmdb_popularity` is NULL on every row of a bootstrap-only catalog.

        So an ordering with only that key is an ordering by `id` on the deployment most
        likely to exist. Seeded worst-first again, and with the *popular* title third so a
        `NULLS FIRST` default -- Postgres's, under `DESC` -- puts the two
        unknowns above it and fails.
        """
        quiet = self._tagged("Barely Voted", genres=("Horror",), vote_count=5)
        loud = self._tagged("Much Voted", genres=("Horror",), vote_count=500_000)
        known = self._tagged("Known Popular", genres=("Horror",), popularity=3.0)
        for one in (quiet, loud, known):
            await repo.add(one)
            await own(one.id)  # type: ignore[operator]

        rows = await repo.list_owned_by_tag(genre="Horror")

        assert [row.id for row in rows] == [known.id, loud.id, quiet.id]

    async def test_a_keyword_predicate_does_not_match_the_genres_array(
        self, repo: TitleRepository, own: object
    ) -> None:
        """Two arrays, two predicates.

        The wrong implementation searches whichever array it was written against and
        answers plausibly for the other -- a "christmas" row of films whose *genre*
        happens to be the word is populated and wrong.
        """
        by_keyword = self._tagged("A Keyworded Film", keywords=("christmas",))
        by_genre = self._tagged("A Genred Film", genres=("christmas",))
        for one in (by_keyword, by_genre):
            await repo.add(one)
            await own(one.id)  # type: ignore[operator]

        rows = await repo.list_owned_by_tag(keyword="christmas")

        assert [row.id for row in rows] == [by_keyword.id]

    async def test_both_predicates_together_narrow_rather_than_widen(
        self, repo: TitleRepository, own: object
    ) -> None:
        """The natural wrong spelling is `OR`.

        On a window carrying both a genre and a keyword that returns the union -- a
        strictly larger, less relevant row that still looks correct.
        """
        both = self._tagged("Both", genres=("Horror",), keywords=("slasher",))
        genre_only = self._tagged("Genre Only", genres=("Horror",))
        keyword_only = self._tagged("Keyword Only", keywords=("slasher",))
        for one in (both, genre_only, keyword_only):
            await repo.add(one)
            await own(one.id)  # type: ignore[operator]

        rows = await repo.list_owned_by_tag(genre="Horror", keyword="slasher")

        assert [row.id for row in rows] == [both.id]

    async def test_a_request_with_no_predicate_answers_with_nothing(
        self, repo: TitleRepository, own: object
    ) -> None:
        """An unpredicated call would be the popular-titles fallback in a query's clothes.

        The library is deliberately populated and owned, so the empty answer is the port
        declining rather than the fixture being empty.
        """
        for index in range(3):
            one = self._tagged(f"Owned {index}", genres=("Horror",), popularity=float(index))
            await repo.add(one)
            await own(one.id)  # type: ignore[operator]

        assert await repo.list_owned_by_tag() == []

    async def test_the_limit_is_honoured_and_keeps_the_best(
        self, repo: TitleRepository, own: object
    ) -> None:
        """A limit applied before the ordering keeps whichever rows the scan reached first.

        The same failure `list_recent`'s own limit case is about.
        """
        seeded = []
        for index in range(5):
            one = self._tagged(f"Owned {index}", genres=("Horror",), popularity=float(index))
            await repo.add(one)
            await own(one.id)  # type: ignore[operator]
            seeded.append(one)

        rows = await repo.list_owned_by_tag(genre="Horror", limit=2)

        assert [row.id for row in rows] == [seeded[4].id, seeded[3].id]

    async def test_a_series_owned_only_through_its_episodes_is_owned(
        self, repo: TitleRepository, own: object
    ) -> None:
        """The divergence from `owned_title_ids`, asserted rather than commented.

        A series' copies are its episode files, so a semi-join carrying `episode_id IS
        NULL` -- which that method does carry, for its own good reason -- reports every
        series in the library as unowned, and every row built on this read becomes
        films-only on a library that is mostly episodes.

        The distractor is a series with **no** copy at all, so this cannot pass by an
        implementation that dropped the ownership join entirely.
        """
        watched = self._tagged("An Owned Series", genres=("Horror",), kind=TitleKind.SERIES)
        absent = self._tagged("An Unowned Series", genres=("Horror",), kind=TitleKind.SERIES)
        await repo.add(watched)
        await repo.add(absent)
        await own(watched.id, episode=True)  # type: ignore[operator]

        rows = await repo.list_owned_by_tag(genre="Horror")

        assert [row.id for row in rows] == [watched.id]


#: A limit comfortably above every fixture below, so a case that is not about
#: the cap is not silently about it either. `limit` has no default on the port
#: -- deliberately, see `list_unwatched_candidates` -- so every call states its
#: own bound, and `test_the_limit_keeps_the_best_rather_than_the_first_found`
#: is the one case that passes something smaller than what it seeded.
_ROOMY = 50

#: Makes a title playable, the way each arm spells it -- a list entry for the
#: fake, a real `media_items` row for Postgres.
Own = Callable[..., Awaitable[None]]

#: Writes one `watch_states` row: `watch(user_id, title_id=..., played=...)`
#: or `watch(user_id, episode_id=..., played=...)`.
Watch = Callable[..., Awaitable[None]]

#: Mints one episode of a series, returning its id.
EpisodeOf = Callable[[uuid.UUID], Awaitable[uuid.UUID]]


class TitleRepositoryCandidateContract:
    """`list_unwatched_candidates`, the read `CandidatePoolService` is built on."""

    @pytest.fixture
    def repo(self) -> TitleRepository:  # pragma: no cover - supplied by subclasses
        raise NotImplementedError

    @pytest.fixture
    def own(self) -> Own:  # pragma: no cover - supplied by subclasses
        raise NotImplementedError

    @pytest.fixture
    def watch(self) -> Watch:  # pragma: no cover - supplied by subclasses
        raise NotImplementedError

    @pytest.fixture
    def episode_of(self) -> EpisodeOf:  # pragma: no cover - supplied by subclasses
        raise NotImplementedError

    @pytest.fixture
    def user_id(self) -> uuid.UUID:  # pragma: no cover - supplied by subclasses
        raise NotImplementedError

    @pytest.fixture
    def other_user_id(self) -> uuid.UUID:  # pragma: no cover - supplied by subclasses
        raise NotImplementedError

    @staticmethod
    def _candidate(
        name: str,
        *,
        genres: tuple[str, ...] = (),
        vote_count: int | None = None,
        kind: TitleKind = TitleKind.MOVIE,
        title_id: uuid.UUID | None = None,
        enrichment_state: EnrichmentState = EnrichmentState.ENRICHED,
    ) -> Title:
        """One catalog row, with an id nameable for the tiebreak case."""
        return Title(
            id=title_id if title_id is not None else new_id(),
            kind=kind,
            name=name,
            sort_name=name.lower(),
            genres=genres,
            # The builder's own keyword names are test-local vocabulary and
            # stay; only the `Title` fields they feed moved.
            tmdb_vote_count=vote_count,
            enrichment_state=enrichment_state,
        )

    async def test_a_title_the_household_finished_is_not_a_candidate(
        self, repo: TitleRepository, user_id: uuid.UUID, own: Own, watch: Watch
    ) -> None:
        """**The distractor is the best row in the catalog**: owned, most voted, seen.

        Under a dropped or inverted exclusion it is `[0]` -- the pool's most prominent
        member is the film the household finished last week, and the model writes a
        reason for it. Positional, so membership cannot satisfy it.
        """
        seen = self._candidate("Already Finished", vote_count=900_000)
        fresh = self._candidate("Never Opened", vote_count=10)
        for one in (seen, fresh):
            await repo.add(one)
            await own(one.id)
        await watch(user_id, title_id=seen.id, played=True)

        rows = await repo.list_unwatched_candidates(user_id, limit=_ROOMY)

        assert [row.id for row in rows] == [fresh.id]

    async def test_a_watched_episode_takes_its_series_out_of_the_pool(
        self,
        repo: TitleRepository,
        user_id: uuid.UUID,
        own: Own,
        watch: Watch,
        episode_of: EpisodeOf,
    ) -> None:
        """The episode/title split, on the read whose whole job is to subtract.

        A watched *episode*'s `watch_states` row carries `episode_id` and a NULL
        `title_id`, so an exclusion spelled `ws.title_id = titles.id` never matches one
        -- and most of a television-heavy source is episodes. The wrong implementation
        puts every series the household is midway through into the pool, forever.

        The distractor is a second series with no state at all, so the case cannot pass
        by an implementation that excluded every series.
        """
        midway = self._candidate("A Series In Progress", kind=TitleKind.SERIES, vote_count=900_000)
        untouched = self._candidate("A Series Never Opened", kind=TitleKind.SERIES, vote_count=10)
        for one in (midway, untouched):
            await repo.add(one)
            await own(one.id)
        await watch(user_id, episode_id=await episode_of(midway.id), played=True)

        rows = await repo.list_unwatched_candidates(user_id, limit=_ROOMY)

        assert [row.id for row in rows] == [untouched.id]

    async def test_a_title_started_and_abandoned_is_still_a_candidate(
        self, repo: TitleRepository, user_id: uuid.UUID, own: Own, watch: Watch
    ) -> None:
        """`played`, never "has a watch state".

        `played_title_ids`' rule, arriving at the read that has to agree with it. A sync
        writes a row per item it observed, so "has a state" is the owned library: under
        that spelling the pool holds only titles the household does **not** own, which
        is a plausible-looking pool of things to seek out and nothing to play. The
        abandoned title is seeded as the *most* voted so it is `[0]` when it is
        correctly kept and absent when it is not.
        """
        abandoned = self._candidate("Twelve Minutes In", vote_count=900_000)
        untouched = self._candidate("Never Opened", vote_count=10)
        for one in (abandoned, untouched):
            await repo.add(one)
            await own(one.id)
        await watch(user_id, title_id=abandoned.id, played=False)

        rows = await repo.list_unwatched_candidates(user_id, limit=_ROOMY)

        assert [row.id for row in rows] == [abandoned.id, untouched.id]

    async def test_another_households_history_does_not_shrink_this_ones_pool(
        self,
        repo: TitleRepository,
        user_id: uuid.UUID,
        other_user_id: uuid.UUID,
        own: Own,
        watch: Watch,
    ) -> None:
        """The `user_id` predicate, invisible on a single-household deployment.

        Without it one member's history empties another's pool, and the
        household with the most watching decides what everyone else is
        offered. The other household's title is the most voted, so it is
        `[0]` when the predicate holds and absent when it does not.
        """
        theirs = self._candidate("Finished By Somebody Else", vote_count=900_000)
        mine = self._candidate("Untouched By Anyone", vote_count=10)
        for one in (theirs, mine):
            await repo.add(one)
            await own(one.id)
        await watch(other_user_id, title_id=theirs.id, played=True)

        rows = await repo.list_unwatched_candidates(user_id, limit=_ROOMY)

        assert [row.id for row in rows] == [theirs.id, mine.id]

    async def test_an_owned_title_outranks_an_unowned_one_however_voted(
        self, repo: TitleRepository, user_id: uuid.UUID, own: Own
    ) -> None:
        """Both halves of *"owned or popular"*, in one assertion.

        The unowned title is the most voted in the catalog by five orders of
        magnitude, so it is `[0]` under an ordering with no ownership key --
        and it is still *present*, because PRD 06 says the pool spans the
        whole catalog rather than the library, so suggestions can include
        things to seek out. An implementation that filtered to owned titles
        returns one row and fails on the same assertion.
        """
        unowned = self._candidate("A Masterpiece Nobody Here Owns", vote_count=900_000)
        owned = self._candidate("A Quiet Film On The Shelf", vote_count=3)
        for one in (unowned, owned):
            await repo.add(one)
        await own(owned.id)

        rows = await repo.list_unwatched_candidates(user_id, limit=_ROOMY)

        assert [row.id for row in rows] == [owned.id, unowned.id]

    async def test_a_series_owned_only_through_its_episodes_ranks_as_owned(
        self, repo: TitleRepository, user_id: uuid.UUID, own: Own
    ) -> None:
        """`list_owned_by_tag`'s divergence from `owned_title_ids`.

        Asserted again here because this read makes its own ownership decision.

        A series' copies are its episode files, so a semi-join carrying
        `episode_id IS NULL` ranks every series in the library alongside the
        catalog's strangers. The distractor is the most-voted title in the
        catalog with no copy at all, so a dropped join fails too.
        """
        stranger = self._candidate("An Unowned Stranger", vote_count=900_000)
        series = self._candidate("An Owned Series", kind=TitleKind.SERIES, vote_count=3)
        for one in (stranger, series):
            await repo.add(one)
        await own(series.id, episode=True)

        rows = await repo.list_unwatched_candidates(user_id, limit=_ROOMY)

        assert [row.id for row in rows] == [series.id, stranger.id]

    async def test_a_copy_the_source_has_retracted_does_not_rank_as_owned(
        self, repo: TitleRepository, user_id: uuid.UUID, own: Own
    ) -> None:
        """`media_items.available` is a column the availability sweep *writes*.

        `mark_unseen_unavailable` sets it false for an item a walk stopped seeing -- so
        a row with `available = false` is the ordinary state of a film the household
        deleted, not an exotic one.

        The wrong implementation ranks it first, because it is still a
        `media_items` row: the pool then leads with the title the household
        most recently got rid of, and the model writes a shelf around it.
        Positional, with a genuinely owned distractor of *lower* vote count so
        an implementation that dropped the ownership key entirely fails too.

        **Deleting `available.is_(True)` survives every other case here**, because `own`
        writes `available = true` and nothing else ever writes the column.
        """
        retracted = self._candidate("Deleted From The Server", vote_count=900_000)
        kept = self._candidate("Still On The Shelf", vote_count=3)
        for one in (retracted, kept):
            await repo.add(one)
        await own(retracted.id, available=False)
        await own(kept.id)

        rows = await repo.list_unwatched_candidates(user_id, limit=_ROOMY)

        assert [row.id for row in rows] == [kept.id, retracted.id]

    async def test_a_genre_the_household_watches_outranks_a_more_voted_stranger(
        self, repo: TitleRepository, user_id: uuid.UUID, own: Own
    ) -> None:
        """The genre-affinity key, the only household-shaped signal in the ordering.

        The distractor is a title of another genre with five orders of
        magnitude more votes: `[0]` when the key is dropped, and the affinity
        title is `[0]` when it is honoured. **Both are asserted**, because an
        implementation that used the genres as a *filter* would answer with
        one row -- and would then hand an empty pool to every household whose
        affinities are empty, which is every household with no history.
        """
        stranger = self._candidate("A Very Popular Comedy", genres=("Comedy",), vote_count=900_000)
        affine = self._candidate("A Quiet Western", genres=("Western",), vote_count=3)
        for one in (stranger, affine):
            await repo.add(one)
            await own(one.id)

        rows = await repo.list_unwatched_candidates(user_id, genres=("Western",), limit=_ROOMY)

        assert [row.id for row in rows] == [affine.id, stranger.id]

    async def test_with_no_affinities_the_order_is_the_vote_count(
        self, repo: TitleRepository, user_id: uuid.UUID, own: Own
    ) -> None:
        """The shipped default's own case.

        `genres=()` is what a household with no watch history produces, and it must
        leave a usable order rather than collapsing one.

        Seeded worst-first so `ORDER BY id` and no ordering at all are the
        reverse of the answer, and the unknown count is seeded *first* so
        Postgres's `NULLS FIRST` default under `DESC` puts it top and fails.

        **A title voted *zero* times is here because without it the "unknown last" rule
        is unobservable on the fake arm**: the natural Python spelling collapses a NULL
        to `0` via `-(vote_count or 0)`, and with no genuine zero in the fixture that
        collapse produces the identical list. Seeded second, so the two also disagree on
        id order.
        """
        unknown = self._candidate("Never Rated", vote_count=None)
        never_voted = self._candidate("Rated By Nobody", vote_count=0)
        quiet = self._candidate("Barely Voted", vote_count=5)
        loud = self._candidate("Much Voted", vote_count=500_000)
        for one in (unknown, never_voted, quiet, loud):
            await repo.add(one)
            await own(one.id)
        assert unknown.id < never_voted.id, (
            "the fixture must make id order and the unknown/zero split disagree"
        )

        rows = await repo.list_unwatched_candidates(user_id, genres=(), limit=_ROOMY)

        assert [row.id for row in rows] == [loud.id, quiet.id, never_voted.id, unknown.id]

    async def test_two_titles_alike_in_everything_are_ordered_by_id(
        self, repo: TitleRepository, user_id: uuid.UUID, own: Own
    ) -> None:
        """The tiebreak, and it is the whole of the pool's stability.

        The prompt addresses candidates by small integer index, so a pool whose ties
        resolve to "whatever the storage returned" is a pool whose index 7 is a
        different film on a re-read -- and the service's index->UUID map is then a map
        of nothing.

        Why ties are ordinary rather than exotic here, and why losing the tail changes
        the pool's *membership* rather than only its order, is argued once on
        `TitleRepository.list_unwatched_candidates` and deliberately not restated.

        The two are inserted in **descending** id order, so insertion order -- heap
        order on a freshly-seeded table, dict order in the fake -- is the reverse of the
        answer.
        """
        first, second = new_id(), new_id()
        assert first < second, "the fixture must know its own id order"
        later = self._candidate("Seeded First", vote_count=7, title_id=second)
        earlier = self._candidate("Seeded Second", vote_count=7, title_id=first)
        for one in (later, earlier):
            await repo.add(one)
            await own(one.id)

        rows = await repo.list_unwatched_candidates(user_id, limit=_ROOMY)

        assert [row.id for row in rows] == [first, second]

    async def test_the_limit_keeps_the_best_rather_than_the_first_found(
        self, repo: TitleRepository, user_id: uuid.UUID, own: Own
    ) -> None:
        """A limit applied before the ordering keeps whichever rows the scan reached first.

        And this limit is the pool *size*. Seeded worst-first, so a limit honoured
        before the sort answers with the two least-voted titles.
        """
        seeded = []
        for index in range(5):
            one = self._candidate(f"Candidate {index}", vote_count=index)
            await repo.add(one)
            await own(one.id)
            seeded.append(one)

        rows = await repo.list_unwatched_candidates(user_id, limit=2)

        assert [row.id for row in rows] == [seeded[4].id, seeded[3].id]

    async def test_a_household_that_has_watched_nothing_still_gets_a_pool(
        self, repo: TitleRepository, user_id: uuid.UUID, own: Own
    ) -> None:
        """A cold start is the *normal* state, not a degraded one.

        PRD 06's own words, and an implementation whose exclusion joined rather than
        anti-joined answers with nothing at all here.

        Positional rather than `len(rows) > 0`, which is satisfied by
        returning the whole table in physical order.
        """
        quiet = self._candidate("Barely Voted", vote_count=5)
        loud = self._candidate("Much Voted", vote_count=500_000)
        for one in (quiet, loud):
            await repo.add(one)
            await own(one.id)

        rows = await repo.list_unwatched_candidates(user_id, limit=_ROOMY)

        assert [row.id for row in rows] == [loud.id, quiet.id]

    async def test_a_skeleton_is_as_eligible_a_candidate_as_an_enriched_title(
        self, repo: TitleRepository, user_id: uuid.UUID, own: Own
    ) -> None:
        """The tier the pool is mostly made of."""
        enriched = self._candidate("Enriched And Quiet", vote_count=5)
        skeleton = self._candidate(
            "A Skeleton Everybody Voted For",
            vote_count=500_000,
            enrichment_state=EnrichmentState.SKELETON,
        )
        for one in (enriched, skeleton):
            await repo.add(one)
            await own(one.id)
        assert enriched.id < skeleton.id, (
            "the premise: the answer must be the reverse of id order, or "
            "`ORDER BY id` alone would produce it"
        )

        rows = await repo.list_unwatched_candidates(user_id, limit=_ROOMY)

        assert [row.id for row in rows] == [skeleton.id, enriched.id]


#: A page walk's runaway bound. A keyset relaxed from `>` to `>=` re-serves its
#: boundary row at every break and never terminates, so a walk with no bound
#: hangs where it should fail -- the shape `.claude/rules/testing-discipline.md`
#: records for the event bus, arriving in a loop instead of an await.
_MAX_PAGES = 20

#: The shared browse population, in the order the fixture seeds it -- which is
#: id order, and is not the answer's order under any of the four sorts. Each
#: sort's key carries a **tie** and, where the column is nullable, a group of
#: **NULLs**, so one fixture exercises the `id` tail and the `IS NOT NULL` leg
#: for every member of the enum at once.
_BROWSE_POPULATION: tuple[tuple[str, int | None, float | None, int | None], ...] = (
    # name, year, popularity, vote_count
    ("Delta", 1999, 3.0, 40),
    ("Alpha", None, None, None),
    ("Foxtrot", 2010, None, 5),
    ("Bravo", None, 9.0, None),
    ("Echo", 1999, 1.0, 900),
    ("Charlie", 2010, None, None),
)

#: What each sort makes of `_BROWSE_POPULATION`, by name.
_BROWSE_EXPECTED: dict[str, tuple[str, ...]] = {
    "name": ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"),
    "year": ("Foxtrot", "Charlie", "Delta", "Echo", "Alpha", "Bravo"),
    "popularity": ("Bravo", "Delta", "Echo", "Alpha", "Foxtrot", "Charlie"),
    "vote_count": ("Echo", "Delta", "Foxtrot", "Alpha", "Bravo", "Charlie"),
}


class TitleRepositoryBrowseContract:
    """`browse` and `browse_facets` -- the read `GET /browse` is built on."""

    @pytest.fixture
    def repo(self) -> TitleRepository:  # pragma: no cover - supplied by subclasses
        raise NotImplementedError

    @pytest.fixture
    def own(self) -> Own:  # pragma: no cover - supplied by subclasses
        raise NotImplementedError

    @staticmethod
    def _browsable(
        name: str,
        *,
        genres: tuple[str, ...] = (),
        keywords: tuple[str, ...] = (),
        year: int | None = None,
        popularity: float | None = None,
        vote_count: int | None = None,
        title_id: uuid.UUID | None = None,
    ) -> Title:
        """One catalog row, with an id nameable for the tiebreak cases.

        `title_id` is a parameter for `_candidate`'s reason: `new_id()` is
        monotonic, so a fixture that mints in insertion order makes
        `ORDER BY id` and "no ordering at all" the same answer.
        """
        return Title(
            id=title_id if title_id is not None else new_id(),
            kind=TitleKind.MOVIE,
            name=name,
            sort_name=name.lower(),
            genres=genres,
            keywords=keywords,
            year=year,
            # The builder's own keyword names are test-local vocabulary and
            # stay; only the `Title` fields they feed moved.
            tmdb_popularity=popularity,
            tmdb_vote_count=vote_count,
            enrichment_state=EnrichmentState.ENRICHED,
        )

    async def _seed_population(self, repo: TitleRepository) -> dict[str, Title]:
        """`_BROWSE_POPULATION`, seeded in its declared order."""
        seeded: dict[str, Title] = {}
        for name, year, popularity, vote_count in _BROWSE_POPULATION:
            one = self._browsable(name, year=year, popularity=popularity, vote_count=vote_count)
            await repo.add(one)
            seeded[name] = one
        return seeded

    @staticmethod
    async def _walk(
        repo: TitleRepository, *, sort: BrowseSort, limit: int, **filters: object
    ) -> list[Title]:
        """Every row `sort` reaches, one keyset page at a time.

        The walk stops on an **empty** page rather than on a short one, which
        is the repository-level shape of `over_fetch`'s argument one layer up:
        a population whose size is an exact multiple of `limit` has a full last page,
        and reading "full" as "there is more" is precisely the off-by-one the keyset
        exists to remove. Here that costs one extra request; at the route it costs a
        client a round trip to learn it has finished, which is why the route
        over-fetches instead.
        """
        collected: list[Title] = []
        after: BrowseCursorPosition | None = None
        for _ in range(_MAX_PAGES):
            page = await repo.browse(sort=sort, after=after, limit=limit, **filters)  # type: ignore[arg-type]
            if not page:
                return collected
            collected += page
            after = BrowseSort.position_of(page[-1], sort=sort)
        raise AssertionError(
            f"the walk did not terminate in {_MAX_PAGES} pages, which is what a "
            "keyset relaxed from `>` to `>=` does: it re-serves its own boundary "
            f"row forever. Collected {[one.name for one in collected]}"
        )

    async def test_a_row_inserted_before_the_cursor_between_two_pages_neither_duplicates_nor_drops(
        self, repo: TitleRepository
    ) -> None:
        """PRD 07's stated reason for the whole design, as a test.

        *"Offset paging is not offered -- it degrades badly over a 1.3M-row catalog and
        produces duplicates under concurrent writes."* This case is the second half.

        Page 1 is served, a row is inserted that sorts **inside** it, page 2 is
        served from the cursor. Under a keyset the client sees the pre-insert
        population exactly once and simply never sees the new row -- it landed
        behind the cursor. Under `OFFSET 3` page 2 begins one row too late:
        the last row of page 1 comes back a second time and the last row of the
        population is never served at all.

        The premise is asserted rather than assumed, because a row inserted
        *after* the cursor makes this case vacuous -- it would be a page-2 row
        under both spellings, and both would pass.
        """
        seeded = [self._browsable(name) for name in ("Alpha", "Bravo", "Charlie", "Delta", "Echo")]
        for one in seeded:
            await repo.add(one)

        first = await repo.browse(sort=BrowseSort.NAME, limit=3)
        assert [one.name for one in first] == ["Alpha", "Bravo", "Charlie"], (
            "the premise: page 1 is the head of the order"
        )

        inserted = self._browsable("Bravissimo")
        await repo.add(inserted)
        boundary = BrowseSort.position_of(first[-1], sort=BrowseSort.NAME)
        assert isinstance(boundary.key, str) and inserted.sort_name < boundary.key, (
            "the premise: the new row sorts *before* the cursor, i.e. into the "
            "page the client has already been served. A row after the cursor is "
            "an ordinary page-2 row and both spellings answer it identically"
        )

        second = await repo.browse(sort=BrowseSort.NAME, after=boundary, limit=3)

        served = [one.id for one in first] + [one.id for one in second]
        assert len(served) == len(set(served)), (
            f"a row was served twice: {[one.name for one in first + second]}"
        )
        assert served == [one.id for one in seeded], (
            "the two pages together are the pre-insert population, in order, "
            f"once each: {[one.name for one in first + second]}"
        )

    @pytest.mark.parametrize("sort", list(BrowseSort))
    @pytest.mark.parametrize("limit", [2, 4])
    async def test_a_walk_returns_the_whole_population_exactly_once(
        self, repo: TitleRepository, sort: BrowseSort, limit: int
    ) -> None:
        """The paged walk and the unpaged read are the same list.

        **`limit=2` over six rows is the exact-exhaustion arm and it is not
        decoration.** The off-by-one this design exists to remove is invisible outside
        `count % limit == 0`: at `limit=4` the population partitions 4 + 2 and a wrong
        terminating rule still looks right. Both arms run over the same fixture so the
        difference is the arithmetic and nothing else.

        The premise guards the trap a UUIDv7 primary key sets: `ORDER BY id` and `ORDER
        BY <the real key>` agree by accident, and then a walk that ignored the sort
        entirely would satisfy this.
        """
        await self._seed_population(repo)

        whole = await repo.browse(sort=sort, limit=_ROOMY)
        assert len(whole) == len(_BROWSE_POPULATION), "the premise: the fixture is all there"
        assert [one.id for one in whole] != sorted(one.id for one in whole), (
            "the premise: this sort's answer is not id order, or a walk that "
            "ignored the sort key would satisfy every assertion below"
        )

        walked = await self._walk(repo, sort=sort, limit=limit)

        assert [one.id for one in walked] == [one.id for one in whole], (
            f"the walk and the single page disagree: {[one.name for one in walked]} "
            f"against {[one.name for one in whole]}"
        )

    @pytest.mark.parametrize("sort", list(BrowseSort))
    async def test_the_order_is_the_sort_key_with_nulls_last_and_the_id_tail(
        self, repo: TitleRepository, sort: BrowseSort
    ) -> None:
        """The four orders, spelled out.

        One fixture, four expectations, and every one of them is a different
        mutation: reverse a direction, take Postgres's `DESC` default of NULLS
        FIRST, drop the `id` tail, or read the column next door, and exactly
        one of these lists moves. `_BROWSE_EXPECTED` is hard-coded rather than
        recomputed, because an expectation derived by re-implementing the sort
        is an assertion that the test agrees with itself.

        The `name` arm is the one whose key cannot be NULL, which is why it is
        here rather than only in the nullable cases below: a `nulls_last` that
        was really a `coalesce` to a sentinel would pass three of these four.
        """
        await self._seed_population(repo)

        rows = await repo.browse(sort=sort, limit=_ROOMY)

        assert [one.name for one in rows] == list(_BROWSE_EXPECTED[sort.value])

    @pytest.mark.parametrize(
        "sort", [BrowseSort.YEAR, BrowseSort.POPULARITY, BrowseSort.VOTE_COUNT]
    )
    async def test_a_page_boundary_inside_the_unkeyed_group_does_not_drop_the_rest_of_it(
        self, repo: TitleRepository, sort: BrowseSort
    ) -> None:
        """The NULL trap, and it is the quietest defect in this port."""
        keys: dict[str, tuple[int | None, float | None, int | None]] = {
            "year": (2001, None, None),
            "popularity": (None, 9.0, None),
            "vote_count": (None, None, 900),
        }
        year, popularity, vote_count = keys[sort.value]
        keyed = [
            self._browsable(
                f"Keyed {index}", year=year, popularity=popularity, vote_count=vote_count
            )
            for index in range(2)
        ]
        unkeyed = [self._browsable(f"Unkeyed {index}") for index in range(3)]
        for one in [*keyed, *unkeyed]:
            await repo.add(one)

        first = await repo.browse(sort=sort, limit=2)
        boundary = BrowseSort.position_of(first[-1], sort=sort)
        assert boundary.key is not None, "the premise: page 1 is the keyed group"
        second = await repo.browse(sort=sort, after=boundary, limit=2)
        boundary = BrowseSort.position_of(second[-1], sort=sort)
        assert boundary.key is None, (
            "the premise: the cursor now names an *unkeyed* row, which is the "
            "only position from which the NULL comparison can be observed"
        )

        third = await repo.browse(sort=sort, after=boundary, limit=2)

        assert [one.id for one in first + second + third] == [
            one.id for one in [*keyed, *unkeyed]
        ], (
            "the unkeyed group is served after the keyed one, whole: "
            f"{[one.name for one in first + second + third]}"
        )

    async def test_every_sort_the_enum_declares_is_served(self, repo: TitleRepository) -> None:
        """A member added to `BrowseSort` with no order behind it must fail here.

        Rather than fall back to something plausible. The floor on the member count is
        the premise: an enum that lost three members would make an "every member works"
        loop trivially true, which is the `len(x) > 0` failure arriving at a `for`.
        """
        assert len(list(BrowseSort)) >= 4, "the premise: there are sorts to be exhaustive about"
        await self._seed_population(repo)

        for sort in BrowseSort:
            rows = await repo.browse(sort=sort, limit=_ROOMY)
            assert len(rows) == len(_BROWSE_POPULATION), f"{sort} answered {len(rows)} rows"

    async def test_a_sort_this_port_cannot_express_raises_rather_than_being_ignored(
        self, repo: TitleRepository
    ) -> None:
        """`FilterNotSupported`'s own argument, applied to the sort.

        An ignored order answers with *more* rows in some other sequence, and more rows
        reads as working.

        The `cast` is the point rather than a wart. `BrowseSort` is closed, so
        the only way to reach this arm is the way a route reaches it -- a
        string that is not a member, arriving through an annotation that says
        it is one. The catalog is deliberately populated, so the refusal is
        the port declining rather than the fixture being empty.
        """
        assert "runtime" not in {one.value for one in BrowseSort}, (
            "the premise: this really is not a member, or the case is about something else entirely"
        )
        await self._seed_population(repo)
        assert await repo.browse(sort=BrowseSort.NAME, limit=_ROOMY), (
            "the premise: a supported sort answers, so an exception below is "
            "about the sort and not about an empty catalog"
        )

        with pytest.raises(FilterNotSupported):
            await repo.browse(sort=cast(BrowseSort, "runtime"), limit=_ROOMY)

    async def test_the_genre_filter_matches_the_genres_array_and_not_the_keywords_beside_it(
        self, repo: TitleRepository
    ) -> None:
        """Two arrays, one predicate.

        The wrong implementation searches whichever it was written against and answers
        plausibly for the other.
        """
        by_genre = self._browsable("A Genred Film", genres=("Horror",))
        by_keyword = self._browsable("A Keyworded Film", keywords=("Horror",))
        for one in (by_genre, by_keyword):
            await repo.add(one)

        rows = await repo.browse(sort=BrowseSort.NAME, genre="Horror", limit=_ROOMY)

        assert [one.id for one in rows] == [by_genre.id]

    async def test_the_genre_filter_answers_one_concept_across_both_source_spellings(
        self, repo: TitleRepository
    ) -> None:
        """Two importers spell one genre concept two different ways.

        `titles.genres` is written by two importers with no shared vocabulary: the IMDb
        bulk phase spells it `Sci-Fi` and `EnrichService` spells it `Science Fiction`,
        and the two never co-occur on a row. Exact containment therefore answers half a
        concept under either spelling, and looks completely right doing it.

        **Both arms are asserted, and the order is the assertion.** A filter
        that expanded only the canonical spelling would pass a
        `?genre=Science Fiction` case and still serve a bookmarked
        `?genre=Sci-Fi` its old half — so each spelling is asked for the whole
        population, positionally, under `sort=name`.

        The Comedy row is the premise that the expansion is a *union of one
        concept* and not simply a widened filter: it must be absent from both
        answers.
        """
        # Single-word-distinct names, because the two arms of this contract do not agree
        # about spaces: Python compares `"a "` before `"an"` and Postgres's default
        # collation ignores the space at the primary level, so "A Fused…"/"A
        # Skeleton…"/"An Enriched…" is a *different* order on the two implementations
        # and the difference is about the collation rather than about anything this case
        imdb_spelling = self._browsable("Alpha Skeleton Space Opera", genres=("Sci-Fi",))
        tmdb_spelling = self._browsable("Bravo Enriched Space Opera", genres=("Science Fiction",))
        fused = self._browsable("Charlie Fused Series Label", genres=("Sci-Fi & Fantasy",))
        unrelated = self._browsable("Zulu Comedy", genres=("Comedy",))
        for one in (imdb_spelling, tmdb_spelling, fused, unrelated):
            await repo.add(one)

        under_imdb = await repo.browse(sort=BrowseSort.NAME, genre="Sci-Fi", limit=_ROOMY)
        under_tmdb = await repo.browse(sort=BrowseSort.NAME, genre="Science Fiction", limit=_ROOMY)

        assert [one.id for one in under_imdb] == [imdb_spelling.id, tmdb_spelling.id, fused.id]
        assert [one.id for one in under_tmdb] == [imdb_spelling.id, tmdb_spelling.id, fused.id]

    async def test_a_genre_outside_the_vocabulary_filters_exactly_as_it_did(
        self, repo: TitleRepository
    ) -> None:
        """The column is open even though the vocabulary is Usher-owned.

        An expansion that dropped an unmapped label — or widened it to everything —
        would be invisible on the labels the map does name.
        """
        tagged = self._browsable("A Sword And Sandal Film", genres=("Sword & Sandal",))
        other = self._browsable("Zed Comedy", genres=("Comedy",))
        for one in (tagged, other):
            await repo.add(one)

        rows = await repo.browse(sort=BrowseSort.NAME, genre="Sword & Sandal", limit=_ROOMY)

        assert [one.id for one in rows] == [tagged.id]

    async def test_the_year_filter_is_exact_and_the_two_filters_intersect(
        self, repo: TitleRepository
    ) -> None:
        """The natural wrong spelling of two filters is `OR`.

        Which answers with a strictly larger, less relevant page that still looks right.
        """
        both = self._browsable("Both", genres=("Horror",), year=1999)
        genre_only = self._browsable("Genre Only", genres=("Horror",), year=2001)
        year_only = self._browsable("Year Only", genres=("Comedy",), year=1999)
        for one in (both, genre_only, year_only):
            await repo.add(one)

        assert [
            one.id for one in await repo.browse(sort=BrowseSort.NAME, year=1999, limit=_ROOMY)
        ] == [
            both.id,
            year_only.id,
        ]
        rows = await repo.browse(sort=BrowseSort.NAME, genre="Horror", year=1999, limit=_ROOMY)
        assert [one.id for one in rows] == [both.id]

    async def test_owned_means_an_available_title_level_copy(
        self, repo: TitleRepository, own: Own
    ) -> None:
        """The two readings of "owned", settled by a fixture rather than by a join.

        `MediaItemRepository.owned_title_ids` carries `episode_id IS NULL` and counts a
        retracted copy; `list_owned_by_tag` requires `available` and carries no episode
        bound. Browse takes one leg from each, and each of the two distractors here is
        the row the *other* reading would have answered with:

        - **the retracted copy**, which `owned_title_ids`' reading keeps and a "show me
          what I can play" filter must not;
        - **the series owned only through its episode files**, which
          `list_owned_by_tag`'s reading keeps and a title-level screen must not.

        All three arms are asserted, because `owned=False` is the complement rather than
        "no predicate": a two-valued flag would make *unset* and *the user asked for
        unowned* the same request, and the `None` arm would then be untestable.
        """
        playable = self._browsable("Playable")
        retracted = self._browsable("Retracted")
        episodes_only = self._browsable("Episodes Only")
        nothing = self._browsable("Nothing")
        for one in (playable, retracted, episodes_only, nothing):
            await repo.add(one)
        await own(playable.id)
        await own(retracted.id, available=False)
        await own(episodes_only.id, episode=True)

        owned_rows = await repo.browse(sort=BrowseSort.NAME, owned=True, limit=_ROOMY)
        unowned_rows = await repo.browse(sort=BrowseSort.NAME, owned=False, limit=_ROOMY)
        every_row = await repo.browse(sort=BrowseSort.NAME, limit=_ROOMY)

        assert [one.id for one in owned_rows] == [playable.id]
        assert [one.id for one in unowned_rows] == [
            episodes_only.id,
            nothing.id,
            retracted.id,
        ]
        assert len(every_row) == 4, "the premise: with no predicate all four are reachable"

    async def test_the_limit_keeps_the_head_of_the_order_rather_than_the_first_found(
        self, repo: TitleRepository
    ) -> None:
        """A limit applied before the ordering keeps whichever rows the scan reached first.

        On a freshly-seeded table that is insertion order.
        """
        await self._seed_population(repo)

        rows = await repo.browse(sort=BrowseSort.NAME, limit=2)

        assert [one.name for one in rows] == ["Alpha", "Bravo"]

    async def test_the_genre_facet_is_counted_without_its_own_predicate(
        self, repo: TitleRepository
    ) -> None:
        """**The facet's whole job**: it counts outside its own filter.

        With `genre=Horror` active the genre facet must still say how many comedies
        there are, or the client cannot navigate anywhere: a facet folded back onto its
        own filter answers "how many Horror films are Horror", which is the size of the
        page already on screen.

        The assertion is that the map is *unchanged* by activating the filter,
        which is the strongest form and the one that catches the fold-back on
        its other entries -- the count of the active genre itself cannot move,
        so a case asserting only that would pass against the defect.
        """
        for index in range(3):
            await repo.add(self._browsable(f"Horror {index}", genres=("Horror",)))
        for index in range(2):
            await repo.add(self._browsable(f"Comedy {index}", genres=("Comedy",)))

        unfiltered = await repo.browse_facets()
        assert dict(unfiltered.genres) == {"Horror": 3, "Comedy": 2}, "the premise"

        filtered = await repo.browse_facets(genre="Horror")

        assert dict(filtered.genres) == dict(unfiltered.genres)

    async def test_the_genre_facet_offers_one_button_per_concept_not_per_spelling(
        self, repo: TitleRepository
    ) -> None:
        """The facet bar offers one button per concept, not one per spelling.

        `GET /browse?facets=true` offering `Sci-Fi` and `Science Fiction` as two buttons
        for one concept means whichever a viewer presses silently loses the other.

        The assertion is on the **whole map**, which is what catches the two
        wrong implementations that both look right: one that emits the
        canonical key *alongside* the spellings it collapsed, and one that
        keeps whichever spelling it saw last.

        The fused TMDb television label is here because it is the case that
        makes a facet count larger than the sum of its parts legitimately: it
        names two concepts and is counted under both.
        """
        for index in range(3):
            await repo.add(self._browsable(f"Skeleton {index}", genres=("Sci-Fi",)))
        for index in range(2):
            await repo.add(self._browsable(f"Enriched {index}", genres=("Science Fiction",)))
        await repo.add(self._browsable("A Series", genres=("Sci-Fi & Fantasy",)))
        await repo.add(self._browsable("A Reality Show", genres=("Reality-TV",)))

        facets = await repo.browse_facets()

        assert dict(facets.genres) == {"Science Fiction": 6, "Fantasy": 1, "Reality": 1}

    async def test_a_facet_count_is_the_size_of_the_page_that_button_would_serve(
        self, repo: TitleRepository
    ) -> None:
        """The facet and the filter are one rule read twice.

        This is the case that fails if they drift apart. A count collapsed into a
        canonical label the filter does not expand -- or expanded by a filter the facet
        does not collapse -- leaves a button whose number is not the number of rows
        pressing it produces.
        """
        for index in range(3):
            await repo.add(self._browsable(f"Skeleton {index}", genres=("Sci-Fi",)))
        for index in range(2):
            await repo.add(self._browsable(f"Enriched {index}", genres=("Science Fiction",)))

        facets = await repo.browse_facets()
        rows = await repo.browse(sort=BrowseSort.NAME, genre="Science Fiction", limit=_ROOMY)

        assert facets.genres["Science Fiction"] == len(rows) == 5

    async def test_a_facet_the_request_named_is_zero_under_its_canonical_label(
        self, repo: TitleRepository
    ) -> None:
        """`browse_facets`' "never a sparse dict", after the collapse.

        A client that filtered on a legacy spelling must get its zero back under the key
        the rest of the map is written in, or the entry is both absent and present
        depending on how you look.
        """
        await repo.add(self._browsable("A Comedy", genres=("Comedy",)))

        facets = await repo.browse_facets(genre="Sci-Fi")

        assert dict(facets.genres) == {"Comedy": 1, "Science Fiction": 0}

    async def test_the_year_facet_is_counted_without_its_own_predicate(
        self, repo: TitleRepository
    ) -> None:
        """`test_the_genre_facet_is_counted_without_its_own_predicate`'s twin.

        A separate case because the two facets are two statements: a drop-your-own-
        predicate rule applied to one of them and forgotten for the other is exactly the
        shape a shared docstring hides.
        """
        for index in range(2):
            await repo.add(self._browsable(f"Nineties {index}", year=1999))
        await repo.add(self._browsable("Noughties", year=2000))
        await repo.add(self._browsable("Undated"))

        unfiltered = await repo.browse_facets()
        assert dict(unfiltered.years) == {1999: 2, 2000: 1}, (
            "the premise, and its second half: a title with no year is in no "
            "bucket rather than in a null one"
        )

        filtered = await repo.browse_facets(year=1999)

        assert dict(filtered.years) == dict(unfiltered.years)

    async def test_the_facets_keep_every_predicate_that_is_not_their_own(
        self, repo: TitleRepository, own: Own
    ) -> None:
        """The other half of the rule, and the one a "drop the filters" shortcut gets wrong.

        The genre facet drops the *genre* predicate and keeps the year and ownership
        ones.

        Without this, a facet bar computed over the whole catalog reports
        counts the client's own filters make unreachable -- 4,000 comedies
        beside a filter that would answer with three.
        """
        owned_1999 = self._browsable("Owned Nineties", genres=("Comedy",), year=1999)
        unowned_1999 = self._browsable("Unowned Nineties", genres=("Horror",), year=1999)
        owned_2000 = self._browsable("Owned Noughties", genres=("Comedy",), year=2000)
        for one in (owned_1999, unowned_1999, owned_2000):
            await repo.add(one)
        await own(owned_1999.id)
        await own(owned_2000.id)

        facets = await repo.browse_facets(genre="Comedy", year=1999, owned=True)

        assert dict(facets.genres) == {"Comedy": 1}, (
            "the genre facet drops `genre` and keeps `year=1999` and `owned`, "
            "so the unowned horror of 1999 and the owned comedy of 2000 are "
            "both out of it"
        )
        assert dict(facets.years) == {1999: 1, 2000: 1}, (
            "the year facet drops `year` and keeps `genre=Comedy` and `owned`, "
            "so the owned comedy of 2000 is counted -- that bucket is the whole "
            "point of the facet, it is where the client can navigate to -- and "
            "the unowned horror of 1999 is not"
        )

    async def test_a_genre_the_request_named_is_present_at_zero_rather_than_absent(
        self, repo: TitleRepository
    ) -> None:
        """`count_by_state`'s *"never a sparse dict"* rule.

        Narrowed to the values the request itself named -- a genre vocabulary is open,
        so "every possible key" is not a thing this can promise.

        A `GROUP BY` returns only the values that have rows, so the defect is
        a **missing key**, which a client cannot tell apart from a filter it
        did not send. The fixture is the reachable shape rather than a
        nonsense one: `genre=Horror&year=1999` over a catalog whose only
        horror film is from 1998.
        """
        await repo.add(self._browsable("Old Horror", genres=("Horror",), year=1998))
        await repo.add(self._browsable("New Comedy", genres=("Comedy",), year=1999))

        facets = await repo.browse_facets(genre="Horror", year=1999)

        assert dict(facets.genres) == {"Comedy": 1, "Horror": 0}

    async def test_a_year_the_request_named_is_present_at_zero_rather_than_absent(
        self, repo: TitleRepository
    ) -> None:
        """The year facet's half of the same rule.

        Separate for the reason the two "without its own predicate" cases are separate.
        """
        await repo.add(self._browsable("Old Horror", genres=("Horror",), year=1998))
        await repo.add(self._browsable("New Comedy", genres=("Comedy",), year=1999))

        facets = await repo.browse_facets(year=1899)

        assert dict(facets.years) == {1998: 1, 1999: 1, 1899: 0}


class TitleRepositoryGenreSweepContract:
    """`list_genres_page` and `replace_genres`.

    The narrow projection the write-time genre backfill walks, and the batched write it
    lands.
    """

    @pytest.fixture
    def repo(self) -> TitleRepository:  # pragma: no cover - supplied by subclasses
        raise NotImplementedError

    @staticmethod
    def _titled(name: str, *genres: str) -> Title:
        return Title(
            kind=TitleKind.MOVIE,
            name=name,
            sort_name=f"sweep {name.lower()}",
            genres=genres,
            enrichment_state=EnrichmentState.ENRICHED,
        )

    async def test_the_page_walk_returns_every_title_once_in_id_order(
        self, repo: TitleRepository
    ) -> None:
        """The whole population, ordered, with no row served twice.

        Asserted as a walk rather than as one page, because `>=` and `>` differ only at
        a boundary two abutting pages have and one page does not.
        """
        added = [self._titled(f"Title {index}", "Drama") for index in range(5)]
        for title in added:
            await repo.add(title)

        seen: list[uuid.UUID] = []
        after: uuid.UUID | None = None
        while True:
            page = await repo.list_genres_page(limit=2, after=after)
            if not page:
                break
            seen.extend(row.id for row in page)
            after = page[-1].id

        assert seen == sorted(title.id for title in added)

    async def test_the_page_carries_the_labels_and_nothing_else(
        self, repo: TitleRepository
    ) -> None:
        """A projection, not an entity.

        The sweep reads the whole catalog and has no use for thirty-three columns of
        each row.
        """
        title = self._titled("The Quiet Vacuum", "Sci-Fi", "Drama")
        await repo.add(title)

        page = await repo.list_genres_page(limit=10)

        assert [(row.id, row.genres) for row in page] == [(title.id, ("Sci-Fi", "Drama"))]

    async def test_a_title_with_no_genres_is_still_in_the_walk(self, repo: TitleRepository) -> None:
        """The population is every title, not every title with a genre.

        A read filtered on `cardinality(genres) > 0` would be a second, silent
        definition of "affected" living in SQL, next to the one in `usher.domain.genres`
        -- the `_FINGERPRINT_SQL` failure shape, one column over.
        """
        bare = self._titled("Untagged")
        await repo.add(bare)

        page = await repo.list_genres_page(limit=10)

        assert [(row.id, row.genres) for row in page] == [(bare.id, ())]

    async def test_replacing_genres_writes_the_rows_that_differ(
        self, repo: TitleRepository
    ) -> None:
        title = self._titled("The Quiet Vacuum", "Sci-Fi")
        await repo.add(title)

        written = await repo.replace_genres([TitleGenres(id=title.id, genres=("Science Fiction",))])

        assert written == 1
        stored = await repo.get(title.id)
        assert stored is not None
        assert stored.genres == ("Science Fiction",)

    async def test_replacing_genres_with_what_the_row_already_holds_writes_nothing(
        self, repo: TitleRepository
    ) -> None:
        """The idempotence guard, in the statement rather than only in the caller.

        A re-run over a normalised catalog must be observably free, and `rowcount` is
        what an operator reads to believe it.
        """
        title = self._titled("The Quiet Vacuum", "Science Fiction")
        await repo.add(title)

        written = await repo.replace_genres([TitleGenres(id=title.id, genres=("Science Fiction",))])

        assert written == 0

    async def test_a_batch_writes_only_its_changed_members(self, repo: TitleRepository) -> None:
        """The count is per row, not per batch — a batch of ten carrying one change reports one."""
        changed = self._titled("Changed", "Sci-Fi")
        same = self._titled("Same", "Drama")
        await repo.add(changed)
        await repo.add(same)

        written = await repo.replace_genres(
            [
                TitleGenres(id=changed.id, genres=("Science Fiction",)),
                TitleGenres(id=same.id, genres=("Drama",)),
            ]
        )

        assert written == 1
        still = await repo.get(same.id)
        assert still is not None
        assert still.genres == ("Drama",)

    async def test_an_empty_batch_writes_nothing_and_asks_nothing(
        self, repo: TitleRepository
    ) -> None:
        """A page that changed nothing must not reach the database at all.

        An `UPDATE ... FROM (VALUES)` with no rows is a syntax error, so this is a real
        refusal rather than a tidiness case.
        """
        assert await repo.replace_genres([]) == 0

    async def test_replacing_genres_for_a_title_that_is_gone_changes_nothing(
        self, repo: TitleRepository
    ) -> None:
        """`raw_payloads` outlives `titles` and so does a page read a moment ago.

        An id naming no row is absent from the count, never an error.
        """
        assert await repo.replace_genres([TitleGenres(id=new_id(), genres=("Drama",))]) == 0


class TitleRepositoryNaturalKeyContract:
    """`resolve_natural_keys` -- the read a restore is built on.

    Every case here seeds through `add`, so the fixture is the same object
    on both arms: a `Title`. What differs is what the two implementations
    resolve *with* — a scan over a dict against three index probes joined
    inside one statement — which is why the "one statement" half of the
    port's promise lives in `tests/integration/test_title_repository.py` and
    not here. A fake has no round trip to count.
    """

    @staticmethod
    def _reference(title: Title) -> TitleReference:
        """The reference an artifact would carry for `title`.

        Spelled here rather than imported from `usher.db.backup_identity`,
        deliberately: a contract suite that built its probes with the module
        under test would be asserting that a function agrees with itself.
        `test_the_reference_a_title_is_carried_as_names_every_key_the_row_has`
        in `tests/unit/test_backup_identity.py` is what pins the builder
        against this spelling.
        """
        return TitleReference(
            kind=title.kind, id=title.id, imdb_id=title.imdb_id, tmdb_id=title.tmdb_id
        )

    async def test_a_reference_resolves_to_the_id_this_catalog_holds(
        self, repo: TitleRepository
    ) -> None:
        """The whole point: the artifact's id is not the answer, the target's is.

        `db/repositories/bulk.py` mints a fresh UUIDv7 per staged row, so two catalogs
        built from one dump agree on `imdb_id` and on nothing else.
        """
        held = Title(kind=TitleKind.MOVIE, name="Held", sort_name="Held", imdb_id="tt99000101")
        await repo.add(held)

        carried = TitleReference(kind=TitleKind.MOVIE, id=new_id(), imdb_id="tt99000101")
        assert carried.id != held.id, "the premise: the artifact carries a different id"

        assert await repo.resolve_natural_keys([carried]) == {carried: held.id}

    async def test_a_title_with_no_imdb_id_resolves_on_the_tmdb_rung(
        self, repo: TitleRepository
    ) -> None:
        """The fallback the live catalog needs, because some titles carry no `imdb_id`.

        Two references in one call, so the case is a statement about the *ladder* rather
        than about a repository that only ever looks at `tmdb_id` -- the sibling
        resolving on the rung above is the control.
        """
        no_imdb = Title(
            kind=TitleKind.MOVIE, name="Crosswalked", sort_name="Crosswalked", tmdb_id=99000201
        )
        has_imdb = Title(
            kind=TitleKind.MOVIE, name="Skeleton", sort_name="Skeleton", imdb_id="tt99000202"
        )
        await repo.add(no_imdb)
        await repo.add(has_imdb)
        assert no_imdb.imdb_id is None, "the premise: this title really has no imdb_id"

        first, second = self._reference(no_imdb), self._reference(has_imdb)
        carried = [
            TitleReference(kind=first.kind, id=new_id(), tmdb_id=first.tmdb_id),
            TitleReference(kind=second.kind, id=new_id(), imdb_id=second.imdb_id),
        ]

        assert await repo.resolve_natural_keys(carried) == {
            carried[0]: no_imdb.id,
            carried[1]: has_imdb.id,
        }

    async def test_the_tmdb_rung_is_namespaced_by_kind(self, repo: TitleRepository) -> None:
        """TMDb keys movies and series in separate spaces, and the two overlap.

        So `tmdb_id` alone resolves a series' watch state onto whichever film shares the
        integer. Both rows are seeded and both references asked in one call, so a
        resolver that dropped `kind` cannot pass by answering one of them.
        """
        film = Title(kind=TitleKind.MOVIE, name="Film", sort_name="Film", tmdb_id=99000301)
        show = Title(kind=TitleKind.SERIES, name="Show", sort_name="Show", tmdb_id=99000301)
        await repo.add(film)
        await repo.add(show)
        assert film.id != show.id, "the premise: the shared integer really is two rows"

        carried = [
            TitleReference(kind=TitleKind.MOVIE, id=new_id(), tmdb_id=99000301),
            TitleReference(kind=TitleKind.SERIES, id=new_id(), tmdb_id=99000301),
        ]

        assert await repo.resolve_natural_keys(carried) == {
            carried[0]: film.id,
            carried[1]: show.id,
        }

    async def test_an_imdb_id_that_differs_only_in_case_resolves_to_nothing(
        self, repo: TitleRepository
    ) -> None:
        """A provider id is not a name, so nothing folds it.

        The premise is the exact spelling resolving in the same call, which
        is what stops this passing against a repository that resolves
        nothing at all.
        """
        held = Title(kind=TitleKind.MOVIE, name="Held", sort_name="Held", imdb_id="tt99000401")
        await repo.add(held)

        exact = TitleReference(kind=TitleKind.MOVIE, id=new_id(), imdb_id="tt99000401")
        upper = TitleReference(kind=TitleKind.MOVIE, id=new_id(), imdb_id="TT99000401")

        assert await repo.resolve_natural_keys([exact, upper]) == {exact: held.id}

    async def test_a_raw_id_resolves_only_against_a_target_that_already_holds_it(
        self, repo: TitleRepository
    ) -> None:
        """A title with neither provider id is a first-class citizen, and they exist.

        The artifact carries the UUID and the target is *asked* rather than trusted.
        Both arms in one case: the id the catalog holds, and one it does not.
        """
        orphan = Title(kind=TitleKind.MOVIE, name="Orphan", sort_name="Orphan")
        await repo.add(orphan)
        assert orphan.imdb_id is None and orphan.tmdb_id is None, (
            "the premise: this fixture really has neither provider id"
        )

        held = self._reference(orphan)
        stranger = TitleReference(kind=TitleKind.MOVIE, id=new_id())

        assert await repo.resolve_natural_keys([held, stranger]) == {held: orphan.id}

    async def test_the_imdb_rung_wins_over_the_tmdb_one(self, repo: TitleRepository) -> None:
        """Precedence, asserted rather than left to whichever row a `UNION` returns first.

        The fixture is the state a merge leaves behind: two rows, one holding
        the `imdb_id` the artifact carries and one holding its `tmdb_id`.
        Rare, and it is exactly the case a ladder exists to be unambiguous
        about.
        """
        by_imdb = Title(
            kind=TitleKind.MOVIE, name="By IMDb", sort_name="By IMDb", imdb_id="tt99000501"
        )
        by_tmdb = Title(kind=TitleKind.MOVIE, name="By TMDb", sort_name="By TMDb", tmdb_id=99000502)
        await repo.add(by_imdb)
        await repo.add(by_tmdb)
        assert by_imdb.id != by_tmdb.id, "the premise: the two rungs name two rows"

        carried = TitleReference(
            kind=TitleKind.MOVIE, id=new_id(), imdb_id="tt99000501", tmdb_id=99000502
        )

        assert await repo.resolve_natural_keys([carried]) == {carried: by_imdb.id}

    async def test_a_reference_this_catalog_does_not_hold_is_absent(
        self, repo: TitleRepository
    ) -> None:
        """Absent, never mapped to `None`.

        The port's contract, and what `usher.db.backup_identity.resolve_titles` turns
        into a named refusal.

        The premise is a sibling that does resolve in the same call.
        """
        held = Title(kind=TitleKind.MOVIE, name="Held", sort_name="Held", imdb_id="tt99000601")
        await repo.add(held)

        found = self._reference(held)
        missing = TitleReference(kind=TitleKind.MOVIE, id=new_id(), imdb_id="tt99000602")

        answers = await repo.resolve_natural_keys([found, missing])

        assert answers == {found: held.id}
        assert missing not in answers

    async def test_an_empty_batch_asks_nothing(self, repo: TitleRepository) -> None:
        """`unnest` over an empty array is a round trip to learn nothing.

        `resolve_tmdb_ids`' own guard, one method over.
        """
        assert await repo.resolve_natural_keys([]) == {}

    async def test_a_repeated_reference_is_answered_once(self, repo: TitleRepository) -> None:
        """A household names one title once per watch state.

        So a batch carrying it twice is the ordinary shape rather than a defect.

        Two probes for one reference would make the ordinal disagree with the caller's
        list, which is the failure mode the mapping cannot survive.
        """
        held = Title(kind=TitleKind.MOVIE, name="Held", sort_name="Held", imdb_id="tt99000701")
        await repo.add(held)
        carried = self._reference(held)

        assert await repo.resolve_natural_keys([carried, carried, carried]) == {carried: held.id}
