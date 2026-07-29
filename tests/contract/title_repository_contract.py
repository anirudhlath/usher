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

import pytest

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import TitleRepository


class TitleRepositoryContract:
    async def test_add_then_get_round_trips(self, repo: TitleRepository) -> None:
        title = Title(
            kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", year=2021, tmdb_id=438631
        )
        await repo.add(title)
        fetched = await repo.get(title.id)
        assert fetched is not None
        assert fetched.name == "Dune"
        assert fetched.tmdb_id == 438631
        assert fetched.enrichment_state is EnrichmentState.SKELETON

    async def test_add_rejects_a_duplicate_id(self, repo: TitleRepository) -> None:
        title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
        await repo.add(title)
        with pytest.raises(RepositoryConflict):
            await repo.add(title)

    async def test_add_rejects_a_duplicate_tmdb_id(self, repo: TitleRepository) -> None:
        """tmdb_id/imdb_id/tvdb_id are unique-indexed attributes (PRD 02,
        db/models/title.py's partial unique indexes) — a *different* id
        carrying a tmdb_id already in use is still a conflict, the same way
        the real repository's IntegrityError translation treats it. A fake
        that only checked `title.id` would let a service add two rows for
        the same TMDb title in tests while the real repository rejects it
        in production — precisely the divergence Task 10 was warned about.
        """
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=438631)
        second = Title(
            kind=TitleKind.MOVIE, name="Dune (dup)", sort_name="Dune (dup)", tmdb_id=438631
        )
        await repo.add(first)
        with pytest.raises(RepositoryConflict):
            await repo.add(second)

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

    async def test_update_rejects_a_conflicting_provider_id(self, repo: TitleRepository) -> None:
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=1)
        second = Title(kind=TitleKind.MOVIE, name="Arrival", sort_name="Arrival", tmdb_id=2)
        await repo.add(first)
        await repo.add(second)
        with pytest.raises(RepositoryConflict):
            await repo.update(second.evolve(tmdb_id=1))

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
