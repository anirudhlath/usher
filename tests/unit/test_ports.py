"""Ports are ABCs (ADR-0001), not Protocols: an incomplete implementation
must fail at instantiation, not at the call site."""

from abc import ABC
from collections.abc import Sequence
from decimal import Decimal

import pytest

from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.embedding import Embedder
from usher.ports.errors import PortAuthFailed, PortRateLimited, PortUnavailable, UsherPortError
from usher.ports.llm import LLMClient, LLMPurpose, LLMUsage
from usher.ports.metadata import MetadataCandidate, MetadataProvider
from usher.ports.repository import TitleRepository
from usher.ports.search import SearchIndex, SearchMode, SearchRequest
from usher.ports.source import SourceAdapter, SourceNotSupported

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

        async def aclose(self) -> None:
            pass

    assert Fake().dimension == 3


# --- Error taxonomy (usher.ports.errors) -------------------------------


def test_source_not_supported_is_a_usher_port_error() -> None:
    """Reparented under UsherPortError so a service can catch the shared
    base without knowing every port's own exception names."""
    assert issubclass(SourceNotSupported, UsherPortError)


@pytest.mark.parametrize("error", [PortUnavailable, PortAuthFailed, PortRateLimited])
def test_port_errors_are_usher_port_errors(error: type[UsherPortError]) -> None:
    assert issubclass(error, UsherPortError)


def test_port_rate_limited_carries_retry_after() -> None:
    assert PortRateLimited(retry_after=30.0).retry_after == 30.0


def test_port_rate_limited_retry_after_defaults_to_none() -> None:
    assert PortRateLimited().retry_after is None


# --- LLMUsage / LLMPurpose ------------------------------------------------


def test_llm_usage_is_a_real_equatable_value() -> None:
    """Was a plain class with only a generated __init__, so equality was
    identity. Now a frozen dataclass: two calls that recorded the same
    numbers compare equal, which is what a test asserting on usage
    actually wants."""
    a = LLMUsage(
        model="gpt-4", tokens_in=10, tokens_out=5, cost_usd=Decimal("0.01"), latency_ms=200
    )
    b = LLMUsage(
        model="gpt-4", tokens_in=10, tokens_out=5, cost_usd=Decimal("0.01"), latency_ms=200
    )
    assert a == b


def test_llm_usage_cost_is_decimal_not_float() -> None:
    usage = LLMUsage(
        model="gpt-4", tokens_in=1, tokens_out=1, cost_usd=Decimal("0.001"), latency_ms=1
    )
    assert isinstance(usage.cost_usd, Decimal)


def test_llm_purpose_is_a_closed_string_vocabulary() -> None:
    assert {p.value for p in LLMPurpose} == {"curation", "query_expansion"}


# --- SearchMode ------------------------------------------------------------


def test_search_mode_fused_is_reachable() -> None:
    """The bug this replaced: `semantic: bool` could not express a third
    "fused" option, even though RRF fusion is the actual design
    (ADR-0002), not a hypothetical alongside full-text and semantic."""
    request = SearchRequest(query="dune", mode=SearchMode.FUSED)
    assert request.mode is SearchMode.FUSED


# --- MetadataCandidate -------------------------------------------------


def test_metadata_candidate_uses_the_canonical_kind_vocabulary() -> None:
    """The bug this replaced: search() returning list[dict[str, Any]] made
    the match stage index into TMDb's own keys, including its movie/TV
    divergence. MetadataCandidate normalises that away before it ever
    reaches M4."""
    candidate = MetadataCandidate(
        provider_id=438631, name="Dune", year=2021, kind=TitleKind.MOVIE, popularity=95.2
    )
    assert candidate.kind is TitleKind.MOVIE


# --- TitleRepository (the port, not the domain model) -----------------------
#
# Repositories are ports too (ADR-0009): usher.services may not import
# usher.db, so a service that needs persistence can only depend on this ABC.
# FakeTitleRepository (tests/fakes/title_repository.py) is not a throwaway
# instantiation check -- it is the in-memory double services get unit-tested
# against from M4 onward, standing in for
# usher.db.repositories.title.PostgresTitleRepository the same way a fake
# Embedder above stands in for a real one. It lives outside this module so
# M4 can import it without dragging in this file's fixtures and parametrized
# tests.


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


async def test_title_repository_add_rejects_a_duplicate_id(
    fake_title_repository: FakeTitleRepository,
) -> None:
    """The bug this closes: add() used to silently overwrite on a
    duplicate id -- exactly what a service "updating" a title by
    re-adding it would do, passing this fake while the real,
    Postgres-backed repository raises IntegrityError on the same call."""
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    await fake_title_repository.add(title)
    with pytest.raises(ValueError):
        await fake_title_repository.add(title)


async def test_title_repository_update_mutates_an_existing_title(
    fake_title_repository: FakeTitleRepository,
) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    await fake_title_repository.add(title)
    enriched = title.evolve(enrichment_state=EnrichmentState.ENRICHED)
    await fake_title_repository.update(enriched)
    fetched = await fake_title_repository.get(title.id)
    assert fetched is not None
    assert fetched.enrichment_state is EnrichmentState.ENRICHED


async def test_title_repository_update_rejects_an_unknown_id(
    fake_title_repository: FakeTitleRepository,
) -> None:
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    with pytest.raises(ValueError):
        await fake_title_repository.update(title)


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


async def test_title_repository_count_by_state_is_never_sparse(
    fake_title_repository: FakeTitleRepository,
) -> None:
    """The bug this closes: a bare counts[EnrichmentState.ENRICHED] was a
    latent KeyError whenever nothing was enriched yet -- real GROUP BY only
    returns rows that exist, and the old fake matched that by coincidence,
    not by a documented contract."""
    await fake_title_repository.add(Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune"))
    counts = await fake_title_repository.count_by_state()
    assert counts[EnrichmentState.ENRICHED] == 0
    assert counts[EnrichmentState.STUB] == 0
    assert set(counts) == set(EnrichmentState)
