"""The source-adapter contract against an adapter with no wire format."""

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
