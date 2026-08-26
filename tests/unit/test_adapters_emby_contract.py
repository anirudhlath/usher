"""The source-adapter contract, against the real EmbyAdapter.

The same file of assertions that `tests/unit/test_source_adapter_contract.py`
runs against an adapter with no wire format at all. Both runs are needed:
that one proves the assertions are not secretly Emby-shaped, this one proves
they survive a serialisation. Neither alone is evidence.

No Docker and no network -- the whole thing rides on an in-memory transport,
which is why the load-bearing suite stays in the fast lane.
"""

from collections.abc import AsyncIterator

import pytest_asyncio

from tests.contract.source_adapter_contract import SourceAdapterContract
from tests.contract.source_harness import SourceHarness
from tests.fakes.emby_harness import EmbyHarness


class TestEmbyAdapter(SourceAdapterContract):
    @pytest_asyncio.fixture
    async def harness(self) -> AsyncIterator[SourceHarness]:
        harness = EmbyHarness()
        try:
            yield harness
        finally:
            await harness.aclose()


def test_both_implementations_run_the_same_assertions() -> None:
    """A contract suite is only evidence if both subclasses actually run all
    of it. Nothing stops a subclass from overriding a case with a weaker one
    -- so this asserts neither does, and that the count is not silently
    drifting as cases are added.

    **The count has to move in the same commit as the cases**, or the suite
    stays red -- which is the design of this guard rather than friction with
    it. M3 shipped `== 39`, M4 took it to 43, M5's six push cases took it to
    49, and issue #41's `start_index`-offsets-the-filtered-stream case takes
    it to 50.

    Exactly one of the 50 skips on this subclass:
    `test_events_raises_source_not_supported_when_push_is_unavailable`,
    because `EmbyAdapter` has no state in which `events()` raises
    `SourceNotSupported` -- it always has a channel to offer and finds out
    afterwards whether it delivers. `TestFakeSourceAdapter` runs all 50.
    """
    from tests.unit.test_source_adapter_contract import TestFakeSourceAdapter

    cases = {name for name in dir(SourceAdapterContract) if name.startswith("test_")}
    assert len(cases) == 50
    for subclass in (TestEmbyAdapter, TestFakeSourceAdapter):
        overridden = {
            name
            for name in cases
            if getattr(subclass, name) is not getattr(SourceAdapterContract, name)
        }
        assert overridden == set(), f"{subclass.__name__} overrides {overridden}"
