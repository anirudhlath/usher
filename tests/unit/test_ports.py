"""Ports are ABCs (ADR-0001), not Protocols: an incomplete implementation
must fail at instantiation, not at the call site."""

from abc import ABC
from collections.abc import Sequence

import pytest

from usher.ports.embedding import Embedder
from usher.ports.llm import LLMClient
from usher.ports.metadata import MetadataProvider
from usher.ports.search import SearchIndex
from usher.ports.source import SourceAdapter

ALL_PORTS: list[type[ABC]] = [SourceAdapter, MetadataProvider, SearchIndex, Embedder, LLMClient]


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
