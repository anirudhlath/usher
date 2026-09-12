"""The shared contract, against the in-memory implementation."""

import pytest
import pytest_asyncio

from tests.contract.job_queue_contract import ClearBackoff, JobQueueContract
from tests.fakes.job_queue import FakeJobQueue


class TestFakeJobQueue(JobQueueContract):
    @pytest.fixture
    def queue(self) -> FakeJobQueue:
        return FakeJobQueue(max_attempts=5, backoff_seconds=1.0)

    @pytest_asyncio.fixture
    async def clear_backoff(self, queue: FakeJobQueue) -> ClearBackoff:
        return queue.clear_backoff
