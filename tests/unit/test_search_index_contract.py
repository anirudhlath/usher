"""`FakeSearchIndex` against the shared `SearchIndex` contract. No Docker.

The integration half -- `PostgresSearchIndex`, real `tsquery`, real
`ts_rank`, real HNSW -- runs the identical suite in
`tests/integration/test_adapters_search_postgres.py`. A fake and a real
implementation with matching signatures are not interchangeable; only the
same assertions against both prove it.
"""

import pytest

from tests.contract.search_index_contract import SearchIndexContract
from tests.fakes.search_index import FakeSearchIndex
from usher.ports.search import SearchDocument


class TestFakeSearchIndex(SearchIndexContract):
    # It does dot products over `SearchDocument.vector`, so it expresses the
    # four semantic and fusion cases -- which are the most delicate logic in
    # this milestone, and would otherwise run only under Docker.
    supports_semantic = True
    # A dict of documents holds no `media_items` and no `enrichment_state`,
    # so `owned_only` and `min_enrichment` are genuinely inexpressible here.
    # Named rather than silently ignored: that is the whole case.
    unsupported_filter = "owned_only"

    @pytest.fixture
    def index(self) -> FakeSearchIndex:
        return FakeSearchIndex()

    async def given_title_row(self, document: SearchDocument) -> None:
        """Nothing to arrange: there is no foreign key onto a `titles` row
        this dict has never heard of, and no generated column to seed."""
        return None
