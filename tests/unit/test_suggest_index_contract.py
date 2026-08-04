"""`FakeSuggestIndex` against the shared `SuggestIndex` contract.

Four of the five cases run; the candidate-cap case is skipped, because this
class computes edit distance over its whole dict and a pass would be a claim
about a latency cliff it cannot have. That skip is the honest answer and it
is visible in pytest's summary, which a silently-passing assertion would not
be.
"""

import uuid

import pytest

from tests.contract.suggest_index_contract import SuggestIndexContract
from tests.fakes.search_index import FakeSuggestIndex
from usher.ports.search import SuggestIndex


class TestFakeSuggestIndex(SuggestIndexContract):
    supports_candidate_cap = False

    @pytest.fixture
    def index(self) -> FakeSuggestIndex:
        return FakeSuggestIndex()

    async def given_title(self, index: SuggestIndex, *, name: str, popularity: float) -> uuid.UUID:
        assert isinstance(index, FakeSuggestIndex)
        return index.given(name=name, popularity=popularity)
