"""Ports are ABCs (ADR-0001), not Protocols: an incomplete implementation
must fail at instantiation, not at the call site."""

from abc import ABC
from collections.abc import Sequence
from decimal import Decimal

import pytest

from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import TitleKind
from usher.ports.embedding import Embedder
from usher.ports.errors import (
    PortAuthFailed,
    PortRateLimited,
    PortUnavailable,
    RepositoryConflict,
    RepositoryNotFound,
    UsherPortError,
)
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


@pytest.mark.parametrize(
    "error",
    [
        PortUnavailable,
        PortAuthFailed,
        PortRateLimited,
        RepositoryConflict,
        RepositoryNotFound,
    ],
)
def test_port_errors_are_usher_port_errors(error: type[UsherPortError]) -> None:
    """A service must be able to catch UsherPortError alone and handle every
    port failure, without importing httpx or sqlalchemy -- which would break
    the `adapters are driven, not driving` and `db is driven, not driving`
    contracts that ADR-0009 rests on."""
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
#
# The behavioural suite that used to live here (add/get round trip, reject
# duplicate, update, count_by_state, ...) moved to
# tests/contract/title_repository_contract.py (Task 10), so the identical
# assertions run against both this fake and the real, Postgres-backed
# PostgresTitleRepository instead of two hand-maintained copies drifting
# apart -- see tests/unit/test_title_repository_contract.py and
# tests/integration/test_title_repository.py's
# TestPostgresTitleRepositoryContract. What's left here is the one check
# with no real-repository counterpart to share it with: the ABC-shape
# assertion this whole file is about.


def test_complete_title_repository_implementation_instantiates() -> None:
    assert isinstance(FakeTitleRepository(), TitleRepository)
