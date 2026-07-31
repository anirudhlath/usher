"""The source-adapter contract against an adapter with no wire format.

The companion run is tests/unit/test_adapters_emby_contract.py, which
executes the identical assertions against the real EmbyAdapter over an
in-memory Emby. Both are needed: this one proves the assertions are not
secretly Emby-shaped, that one proves they survive a wire format.
"""

from collections.abc import AsyncIterator

import pytest_asyncio

from tests.contract.source_adapter_contract import SourceAdapterContract
from tests.contract.source_harness import SourceHarness
from tests.fakes.source_adapter import FakeSourceHarness


class TestFakeSourceAdapter(SourceAdapterContract):
    @pytest_asyncio.fixture
    async def harness(self) -> AsyncIterator[SourceHarness]:
        harness = FakeSourceHarness()
        try:
            yield harness
        finally:
            await harness.aclose()
