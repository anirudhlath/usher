"""Ports are ABCs (ADR-0001), not Protocols: an incomplete implementation
must fail at instantiation, not at the call site."""

import uuid
from abc import ABC
from collections.abc import Sequence

import pytest

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.embedding import Embedder
from usher.ports.llm import LLMClient
from usher.ports.metadata import MetadataProvider
from usher.ports.repository import TitleRepository
from usher.ports.search import SearchIndex
from usher.ports.source import SourceAdapter

ALL_PORTS: list[type[ABC]] = [
    SourceAdapter,
    MetadataProvider,
    SearchIndex,
    Embedder,
    LLMClient,
    TitleRepository,
]


@pytest.mark.parametrize("port", ALL_PORTS)
def test_port_cannot_be_instantiated_directly(port: type[ABC]) -> None:
    with pytest.raises(TypeError):
        port()


@pytest.mark.parametrize("port", ALL_PORTS)
def test_port_declares_abstract_methods(port: type[ABC]) -> None:
    assert port.__abstractmethods__


def test_incomplete_implementation_fails_at_instantiation() -> None:
    class Incomplete(Embedder):
        pass

    with pytest.raises(TypeError, match="abstract"):
        Incomplete()  # type: ignore[abstract]  # verifying the runtime rejection ABC enforces


def test_complete_implementation_instantiates() -> None:
    class Fake(Embedder):
        @property
        def model_name(self) -> str:
            return "fake"

        @property
        def dimension(self) -> int:
            return 3

        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [[0.0, 0.0, 0.0] for _ in texts]

    assert Fake().dimension == 3


# --- TitleRepository (the port, not the domain model) -----------------------
#
# Repositories are ports too (ADR-0009): usher.services may not import
# usher.db, so a service that needs persistence can only depend on this ABC.
# FakeTitleRepository is not a throwaway instantiation check — it is the
# in-memory double services get unit-tested against from M4 onward, standing
# in for usher.db.repositories.title.PostgresTitleRepository the same way a
# fake Embedder above stands in for a real one.


class FakeTitleRepository(TitleRepository):
    """In-memory TitleRepository port, keyed the same way the real
    Postgres-backed PostgresTitleRepository (Task 10) is: by id, with
    tmdb_id and imdb_id as secondary lookups."""

    def __init__(self) -> None:
        self._titles: dict[uuid.UUID, Title] = {}

    async def add(self, title: Title) -> None:
        self._titles[title.id] = title

    async def get(self, title_id: uuid.UUID) -> Title | None:
        return self._titles.get(title_id)

    async def get_by_tmdb_id(self, tmdb_id: int) -> Title | None:
        for title in self._titles.values():
            if title.tmdb_id == tmdb_id:
                return title
        return None

    async def get_by_imdb_id(self, imdb_id: str) -> Title | None:
        for title in self._titles.values():
            if title.imdb_id == imdb_id:
                return title
        return None

    async def count_by_state(self) -> dict[EnrichmentState, int]:
        counts: dict[EnrichmentState, int] = {}
        for title in self._titles.values():
            counts[title.enrichment_state] = counts.get(title.enrichment_state, 0) + 1
        return counts


@pytest.fixture
def fake_title_repository() -> FakeTitleRepository:
    return FakeTitleRepository()


def test_complete_title_repository_implementation_instantiates() -> None:
    assert isinstance(FakeTitleRepository(), TitleRepository)


async def test_title_repository_add_then_get_round_trips(
    fake_title_repository: FakeTitleRepository,
) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=438631)
    await fake_title_repository.add(title)
    assert await fake_title_repository.get(title.id) == title


async def test_title_repository_get_returns_none_for_unknown_id(
    fake_title_repository: FakeTitleRepository,
) -> None:
    assert await fake_title_repository.get(new_id()) is None


async def test_title_repository_get_by_tmdb_id_finds_the_title(
    fake_title_repository: FakeTitleRepository,
) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=438631)
    await fake_title_repository.add(title)
    found = await fake_title_repository.get_by_tmdb_id(438631)
    assert found is not None
    assert found.id == title.id


async def test_title_repository_get_by_imdb_id_finds_the_title(
    fake_title_repository: FakeTitleRepository,
) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", imdb_id="tt1160419")
    await fake_title_repository.add(title)
    found = await fake_title_repository.get_by_imdb_id("tt1160419")
    assert found is not None
    assert found.id == title.id


async def test_title_repository_count_by_state_reports_the_catalog(
    fake_title_repository: FakeTitleRepository,
) -> None:
    for i in range(3):
        await fake_title_repository.add(
            Title(kind=TitleKind.MOVIE, name=f"Film {i}", sort_name=f"Film {i}")
        )
    counts = await fake_title_repository.count_by_state()
    assert counts[EnrichmentState.SKELETON] == 3
