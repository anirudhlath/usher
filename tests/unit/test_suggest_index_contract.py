"""`FakeSuggestIndex` against the typo-tolerant `SuggestIndex` contract.

`TypoTolerantSuggestIndexContract` extends `SuggestIndexContract`, so all five
cases are collected here; the candidate-cap one is skipped, because this class
computes edit distance over its whole dict and a pass would be a claim about a
latency cliff it cannot have. That skip is the honest answer and it is visible
in pytest's summary, which a silently-passing assertion would not be.

**The other implementation of the base contract is Postgres-only**, in
`tests/integration/test_adapters_search_prefix.py`. `PostgresPrefixSuggestIndex`
has no fake: its cases are about which index the planner takes, and an
in-memory double of a prefix probe is `str.startswith` asserting against
`str.startswith`.
"""

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
