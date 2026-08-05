"""Shared behavioural contract every `TitleRepository` implementation must
satisfy — the technique PRD 08 calls out for `SourceAdapter` ("One
parametrised test class every SourceAdapter must pass... it either passes
the same tests the [other] adapter passes, or the port was wrong"), applied
here to the port M1 actually ships: `FakeTitleRepository`
(tests/fakes/title_repository.py, used to unit-test services with no
network or database) and `PostgresTitleRepository`
(usher.db.repositories.title, the real, SQLAlchemy-backed implementation).

A fake and a real implementation that merely have matching method
signatures are not interchangeable — only running the *same* assertions
against both proves it. Two hand-maintained copies of these assertions
(one in tests/unit against the fake, one in tests/integration against
Postgres) would drift the moment someone updated one and not the other;
this module exists so there is exactly one copy to update.

Not a test module itself: `TitleRepositoryContract` deliberately doesn't
start with `Test`, so pytest's default collection (`python_classes =
Test*`) never tries to instantiate it directly -- which would fail anyway,
since it has no `repo` fixture of its own. Subclass it and provide `repo`:

    class TestFakeTitleRepository(TitleRepositoryContract):
        @pytest.fixture
        def repo(self) -> FakeTitleRepository:
            return FakeTitleRepository()

See tests/unit/test_title_repository_contract.py (no Docker) and
tests/integration/test_title_repository.py's
`TestPostgresTitleRepositoryContract` (real Postgres) for the two
concrete subclasses.
"""

import uuid
from datetime import UTC, date, datetime

import pytest

from usher.domain.enums import EnrichmentState, ProductionStatus, TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import TitleRepository


class TitleRepositoryContract:
    @pytest.fixture
    def collection_id(self) -> uuid.UUID:
        """A collection id `test_add_then_get_round_trips` may store.

        Overridable, and M7 is why: `titles.collection_id` gained a real
        foreign key to `collections` in `fd7c3a5b9e12`, so the bare
        `new_id()` this case used to inline is a `ForeignKeyViolationError`
        against the real repository and passes silently against a fake that
        is a dict. Same shape `EpisodeRepositoryContract` already has for
        `title_id`: the fake takes the default, and the Postgres subclass
        overrides it with the id of a row it seeded.
        """
        return new_id()

    async def test_add_then_get_round_trips(
        self, repo: TitleRepository, collection_id: uuid.UUID
    ) -> None:
        # Not `assert fetched == title`: an earlier version of this test (in
        # tests/unit/test_ports.py, before the contract suite existed) did
        # exactly that, and it only worked by accident, against the fake
        # alone -- the fake used to preserve created_at/updated_at verbatim,
        # so a freshly-constructed Title round-tripped byte-for-byte. Neither
        # implementation does that (deliberately -- see
        # test_created_at_is_not_taken_from_the_caller below): Postgres is
        # the authoritative clock for both columns, and the fake now stamps
        # them itself to match. A full-equality assertion here would fail
        # against both, for a reason that has nothing to do with what this
        # test checks -- excluded from the comparison below, not from the
        # round trip: both are still set on the constructed Title, just not
        # compared.
        #
        # Every other field of the 31 is set to a non-default value and
        # compared -- the original version of this test only checked 3
        # (name, tmdb_id, enrichment_state), which would miss a broken
        # mapping in any of the other 28.
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
            community_rating=8.4,
            vote_count=22000,
            popularity=369.5,
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
        """Postgres is the authoritative clock for created_at: add()'s
        _to_row excludes it from the INSERT entirely, so the database's own
        server_default assigns it, never whatever the caller's Title
        happened to carry -- a stale retry, a deliberately backdated import,
        or (as here) a plain constructor call that hardcodes one. Measured
        divergence this pins: the fake used to honour the caller's value
        verbatim (`created_at_is_callers: fake=True`), while the real,
        Postgres-backed repository never did (`real=False`)."""
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
        """M4 builds re-enrichment scheduling on updated_at -- which only
        means anything if created_at never moves once a title exists.
        Deliberately tampers with created_at on the incoming Title (not
        just leaves it untouched via evolve(), which would pass this
        assertion even without the fix, since evolve() alone never changes
        a field it isn't told to): update() must ignore it regardless,
        the same way the real repository's update() never even looks at
        created_at on the incoming row (see title.py's update())."""
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
        """tmdb_id is unique *per kind* (ADR-0011), so two movies claiming
        one TMDb movie id is still a conflict — and the constraint that
        fires is now the composite index.

        The final assertion pins a measured bug, not a style preference:
        the message used to read "title {second.id} already exists"
        unconditionally, which is false here -- `second`'s own id was
        never the problem, its tmdb_id collided with a *different* row.
        The message may still name `second.id` to say which add() call
        failed; claiming that id already exists is what was wrong.
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
        """The measurement ADR-0011 rests on: 26,968 TMDb ids are live in
        both namespaces at once. Under M1's single-column index this call
        raised RepositoryConflict and 47.3% of TV lost its tmdb_id during
        Phase 2. Delete the `kind` column from the index and this fails."""
        movie = Title(kind=TitleKind.MOVIE, name="Pride", sort_name="Pride", tmdb_id=1)
        series = Title(kind=TitleKind.SERIES, name="Pride", sort_name="Pride", tmdb_id=1)
        await repo.add(movie)
        await repo.add(series)
        assert (await repo.get(movie.id)) is not None
        assert (await repo.get(series.id)) is not None

    async def test_add_rejects_a_duplicate_imdb_id(self, repo: TitleRepository) -> None:
        """Same property as test_add_rejects_a_duplicate_tmdb_id_of_the_same_kind,
        for the imdb_id branch -- exercised separately, not folded into a single
        parametrized case, so a typo swapping which field
        `_provider_id_conflict` (the fake) or Postgres (the real
        repository) actually checks can't pass by accident."""
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
        """Same property, for the tvdb_id branch -- see
        test_add_rejects_a_duplicate_imdb_id's docstring."""
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
        """Without the `kind` argument this method has no correct answer
        when both namespaces hold the id — the Postgres implementation
        raised a raw sqlalchemy.exc.MultipleResultsFound straight out of the
        port, which `db is driven, not driving` exists to prevent."""
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
        """tmdb_id's own type is `int`, not `int | None` -- but a caller
        holding a genuinely optional value (e.g. `Title.tmdb_id` itself)
        can still reach this with `None` if it ever bypasses mypy at the
        call site (a stray `# type: ignore`, `cast`, ...). Both
        implementations compile "tmdb_id == None" straight through --
        Postgres as `IS NULL`, the fake as a plain `==` -- which matches
        whichever null-provider-id title happens to come first, not "the
        title with this id": the opposite of what this method promises.
        Measured without the guard: this returned an arbitrary
        null-tmdb_id title instead of None, in both implementations.
        """
        await repo.add(Title(kind=TitleKind.MOVIE, name="Home Video", sort_name="Home Video"))
        assert await repo.get_by_tmdb_id(None, TitleKind.MOVIE) is None  # type: ignore[arg-type]

    async def test_get_by_imdb_id_of_none_finds_nothing(self, repo: TitleRepository) -> None:
        """Same property as test_get_by_tmdb_id_of_none_finds_nothing, for
        imdb_id."""
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
        """Same property as test_update_rejects_a_conflicting_tmdb_id_of_the_same_kind,
        for the imdb_id branch -- see test_add_rejects_a_duplicate_imdb_id's
        docstring for why this is a separate case, not a parametrized one.
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
        """update() clearing a field to None/() was untested -- worth
        pinning separately from test_update_mutates_an_existing_title,
        since a naive fix for the conflict-detection tests above could plausibly
        treat None as just another value to compare, rejecting a clear as
        a false conflict between two titles that both have tmdb_id=None."""
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
        """ADR-0011 arriving at the *reverse* lookup, which is the direction a
        derivation gets wrong.

        `get_by_tmdb_id` already takes a kind and the case above pins it. What
        is new here is a walk that starts from a **payload** and has to find
        its title: `raw_payloads` has no `title_id`, the join back is
        `(provider, kind, reference)`, and the payload's own `id` field is the
        bare integer sitting right there. 26,968 measured TMDb ids are live in
        both spaces, so a resolver keyed on the integer alone attaches a
        series' cast to a film -- with the right counts, the right people, and
        nothing to say so.

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
        """A batch rather than one, for `PersonRepository.resolve_tmdb_ids`'
        reason: a derivation page is 500 payloads and a lookup per payload is
        the round-trip-per-item shape batching exists to remove.

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
        """PRD 08's empty-database rule at the port. A derivation page whose
        payloads are all series reaches the movie branch with an empty list,
        and `WHERE tmdb_id = ANY('{}')` is a statement issued to learn
        nothing."""
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
            popularity=popularity,
            vote_count=vote_count,
            enrichment_state=EnrichmentState.ENRICHED,
        )

    async def test_an_unowned_title_is_absent_however_popular_it_is(
        self, repo: TitleRepository, own: object
    ) -> None:
        """**The distractor `SeasonalProvider`'s own case seeds**, one layer
        down: the *best* match in the catalog, highest popularity, exact
        genre, and no copy.

        The wrong implementation matches the predicate against the whole
        catalog -- 1.27M titles, of which the household can play none, in a
        correctly-shaped and beautifully-themed row. It is `[0]` under that
        implementation and absent under this one, so the assertion is
        positional and cannot be satisfied by membership.
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
        """Insertion order is id order -- every id here is a UUIDv7 -- so a
        fixture seeded best-first is satisfied by `ORDER BY id` and by no
        ordering at all. These are seeded worst-first.
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
        """`titles.popularity` was measured NULL on all 1,271,138 rows of a
        bootstrap-only catalog, so an ordering with only that key is an
        ordering by `id` on the deployment most likely to exist.

        Seeded worst-first again, and with the *popular* title third so a
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
        """Two arrays, two predicates. The wrong implementation searches
        whichever array it was written against and answers plausibly for the
        other -- a "christmas" row of films whose *genre* happens to be the
        word is populated and wrong.
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
        """The natural wrong spelling is `OR`, which on a window carrying both
        a genre and a keyword returns the union -- a strictly larger, less
        relevant row that still looks correct."""
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
        """An unpredicated call is "the library ordered by popularity", which
        is the popular-titles fallback wearing a query's clothes. The library
        is deliberately populated and owned, so the empty answer is the port
        declining rather than the fixture being empty."""
        for index in range(3):
            one = self._tagged(f"Owned {index}", genres=("Horror",), popularity=float(index))
            await repo.add(one)
            await own(one.id)  # type: ignore[operator]

        assert await repo.list_owned_by_tag() == []

    async def test_the_limit_is_honoured_and_keeps_the_best(
        self, repo: TitleRepository, own: object
    ) -> None:
        """A limit applied before the ordering keeps whichever rows the scan
        reached first, which is the same failure `list_recent`'s own limit
        case is about."""
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
        """**The divergence from `owned_title_ids`, asserted rather than
        commented.** A series' copies are its episode files, so a semi-join
        carrying `episode_id IS NULL` -- which that method does carry, for its
        own good reason -- reports every series in the library as unowned, and
        every row built on this read becomes films-only on a library that is
        89% episodes.

        The distractor is a series with **no** copy at all, so this cannot
        pass by an implementation that dropped the ownership join entirely.
        """
        watched = self._tagged("An Owned Series", genres=("Horror",), kind=TitleKind.SERIES)
        absent = self._tagged("An Unowned Series", genres=("Horror",), kind=TitleKind.SERIES)
        await repo.add(watched)
        await repo.add(absent)
        await own(watched.id, episode=True)  # type: ignore[operator]

        rows = await repo.list_owned_by_tag(genre="Horror")

        assert [row.id for row in rows] == [watched.id]
