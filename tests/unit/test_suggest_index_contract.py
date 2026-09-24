"""`FakeSuggestIndex` against the typo-tolerant `SuggestIndex` contract."""

import uuid

import pytest

from tests.contract.suggest_index_contract import TypoTolerantSuggestIndexContract
from tests.fakes.search_index import FakeSuggestIndex
from usher.ports.search import SuggestIndex


class TestFakeSuggestIndex(TypoTolerantSuggestIndexContract):
    supports_candidate_cap = False

    @pytest.fixture
    def index(self) -> FakeSuggestIndex:
        return FakeSuggestIndex()

    async def given_title(self, index: SuggestIndex, *, name: str, popularity: float) -> uuid.UUID:
        assert isinstance(index, FakeSuggestIndex)
        return index.given(name=name, popularity=popularity)
