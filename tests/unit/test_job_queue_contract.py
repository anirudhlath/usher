"""The shared contract, against the in-memory implementation.

Half of a pair, and the weaker half by construction. A dict cannot express
`SELECT ... FOR UPDATE SKIP LOCKED`, so
`test_two_workers_never_claim_the_same_job` is **skipped** here rather than
passed -- see `tests/fakes/job_queue.py`'s docstring for the full list of
what this run does not prove, and `tests/integration/test_job_queue.py` for
where each of those is actually closed.
"""

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
