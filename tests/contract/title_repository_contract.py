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

from datetime import UTC, datetime

import pytest

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import TitleRepository


class TitleRepositoryContract:
    async def test_add_then_get_round_trips(self, repo: TitleRepository) -> None:
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
        # test checks.
        title = Title(
            kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", year=2021, tmdb_id=438631
        )
        await repo.add(title)
        fetched = await repo.get(title.id)
        assert fetched is not None
        assert fetched.name == "Dune"
        assert fetched.tmdb_id == 438631
        assert fetched.enrichment_state is EnrichmentState.SKELETON

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

    async def test_add_rejects_a_duplicate_tmdb_id(self, repo: TitleRepository) -> None:
        """tmdb_id/imdb_id/tvdb_id are unique-indexed attributes (PRD 02,
        db/models/title.py's partial unique indexes) — a *different* id
        carrying a tmdb_id already in use is still a conflict, the same way
        the real repository's IntegrityError translation treats it. A fake
        that only checked `title.id` would let a service add two rows for
        the same TMDb title in tests while the real repository rejects it
        in production — precisely the divergence Task 10 was warned about.

        Also pins two properties beyond "a conflict was raised": the
        `.constraint` attribute names the specific index that fired (a
        service can't otherwise tell "retarget this id" from "look up the
        row already holding this tmdb_id" apart), and the message never
        claims `second`'s own id already exists -- it doesn't; the *field*
        collided, not the id. Measured bug this pins: the message used to
        read "title {second.id} already exists" unconditionally, which is
        false here -- that id was never the problem. (The message may
        still *name* second.id, to say which add() call failed -- that's
        fine; claiming it already exists is what was wrong.)
        """
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=438631)
        second = Title(
            kind=TitleKind.MOVIE, name="Dune (dup)", sort_name="Dune (dup)", tmdb_id=438631
        )
        await repo.add(first)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.add(second)
        assert exc_info.value.constraint == "ix_titles_tmdb_id"
        assert "already exists" not in str(exc_info.value)

    async def test_add_rejects_a_duplicate_imdb_id(self, repo: TitleRepository) -> None:
        """Same property as test_add_rejects_a_duplicate_tmdb_id, for the
        imdb_id branch -- exercised separately, not folded into a single
        parametrized case, so a typo swapping which field
        `_provider_id_conflict` (the fake) or Postgres (the real
        repository) actually checks can't pass by accident."""
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="tt1160419")
        second = Title(
            kind=TitleKind.MOVIE, name="Dune (dup)", sort_name="Dune (dup)", imdb_id="tt1160419"
        )
        await repo.add(first)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.add(second)
        assert exc_info.value.constraint == "ix_titles_imdb_id"
        assert "already exists" not in str(exc_info.value)

    async def test_add_rejects_a_duplicate_tvdb_id(self, repo: TitleRepository) -> None:
        """Same property, for the tvdb_id branch -- see
        test_add_rejects_a_duplicate_imdb_id's docstring."""
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tvdb_id=121361)
        second = Title(
            kind=TitleKind.MOVIE, name="Dune (dup)", sort_name="Dune (dup)", tvdb_id=121361
        )
        await repo.add(first)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.add(second)
        assert exc_info.value.constraint == "ix_titles_tvdb_id"
        assert "already exists" not in str(exc_info.value)

    async def test_get_returns_none_for_unknown_id(self, repo: TitleRepository) -> None:
        assert await repo.get(new_id()) is None

    async def test_get_by_tmdb_id_finds_the_title(self, repo: TitleRepository) -> None:
        title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=438631)
        await repo.add(title)
        found = await repo.get_by_tmdb_id(438631)
        assert found is not None
        assert found.id == title.id

    async def test_get_by_imdb_id_finds_the_title(self, repo: TitleRepository) -> None:
        title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="tt1160419")
        await repo.add(title)
        found = await repo.get_by_imdb_id("tt1160419")
        assert found is not None
        assert found.id == title.id

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

    async def test_update_rejects_a_conflicting_tmdb_id(self, repo: TitleRepository) -> None:
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=1)
        second = Title(kind=TitleKind.MOVIE, name="Arrival", sort_name="Arrival", tmdb_id=2)
        await repo.add(first)
        await repo.add(second)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.update(second.evolve(tmdb_id=1))
        assert exc_info.value.constraint == "ix_titles_tmdb_id"

    async def test_update_rejects_a_conflicting_imdb_id(self, repo: TitleRepository) -> None:
        """Same property as test_update_rejects_a_conflicting_tmdb_id, for
        the imdb_id branch -- see test_add_rejects_a_duplicate_imdb_id's
        docstring for why this is a separate case, not a parametrized one.
        """
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="tt1160419")
        second = Title(
            kind=TitleKind.MOVIE, name="Arrival", sort_name="Arrival", imdb_id="tt0104652"
        )
        await repo.add(first)
        await repo.add(second)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.update(second.evolve(imdb_id="tt1160419"))
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
