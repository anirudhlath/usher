"""Behaviour every `JobQueue` implementation must satisfy.

One case -- `test_two_workers_never_claim_the_same_job` -- needs two
genuinely concurrent sessions and is **skipped** unless the subclass sets
`requires_concurrency = True` and supplies a `concurrent_claims` harness.
`FakeJobQueue` leaves it `False` and its run claims nothing about locking,
which is honest: a single-threaded dict cannot express `SKIP LOCKED`, and a
case that "passed" for it would ratify a plain `SELECT` in the real
implementation.

**The harness asserts on *observed* overlap, not on a count.** A count a
serialised run would also produce proves nothing -- M3 deleted a
single-flight lock and watched the concurrency test pass five runs in a row,
because nothing in it ever truly awaited and the event loop ran each task
through its whole cycle before starting the next. So `ClaimWindow` carries
the wall-clock interval each claim actually occupied, and the case fails if
those intervals do not overlap, whatever the claim counts say.

Two test-only hooks the port deliberately does not carry:

- `clear_backoff`, because advancing past a backoff by sleeping would make
  this suite take the backoff schedule in real time, and because nothing in
  `src/` would ever call it.
- `concurrent_claims`, for the reason above.

Both are fixtures the subclass supplies, so the port stays free of methods
that exist only for tests.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import pytest

from usher.domain.jobs import JobKind, JobPriority, JobStatus
from usher.ports.jobs import JobQueue, JobRequest

ClearBackoff = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ClaimWindow:
    """One claimer's result plus the wall-clock interval its claim occupied.

    `started_at`/`finished_at` come from `time.monotonic()` around the claim
    itself. They are what makes "these two claims really did overlap" an
    assertion rather than an assumption -- see the module docstring.
    """

    keys: tuple[str, ...]
    started_at: float
    finished_at: float


class ConcurrentClaimHarness(ABC):
    """Runs several claims against one queue with genuine overlap.

    Supplied by an implementation whose store can actually express
    concurrent claims. The harness owns the sessions, because the shape that
    matters -- one Postgres backend per claimer, each in its own open
    transaction -- is not something the contract can construct on a port's
    behalf.
    """

    @abstractmethod
    async def run(self, *, keys: Sequence[str], claimers: int, limit: int = 1) -> list[ClaimWindow]:
        """Enqueue one pending job per key (visibly, i.e. committed), then
        run `claimers` claims that genuinely overlap in time.

        Must raise rather than hang if a claim blocks: an implementation
        whose claim is `FOR UPDATE` without `SKIP LOCKED` makes the second
        claimer wait on the first's uncommitted row lock forever, and a test
        that hangs reports nothing.
        """


def overlapping(windows: Sequence[ClaimWindow]) -> bool:
    """Did every claim's interval overlap every other's?

    The property a serialised run cannot fake. Two claims that ran one after
    the other have disjoint intervals however similar their results look.
    """
    return all(
        one.started_at < other.finished_at and other.started_at < one.finished_at
        for index, one in enumerate(windows)
        for other in windows[index + 1 :]
    )


class JobQueueContract:
    requires_concurrency: bool = False

    @pytest.fixture
    def concurrent_claims(self) -> ConcurrentClaimHarness | None:
        """Overridden by an implementation that can express real concurrency.

        Returns `None` rather than raising so the skip below is what a
        single-threaded implementation reports, instead of a fixture error
        that reads like a broken suite.
        """
        return None

    async def test_enqueued_work_is_claimable(self, queue: JobQueue) -> None:
        written = await queue.enqueue(
            [JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)]
        )
        assert written == 1
        claimed = await queue.claim([JobKind.ENRICH])
        assert [job.key for job in claimed] == ["t1"]
        assert claimed[0].status is JobStatus.RUNNING
        assert claimed[0].kind is JobKind.ENRICH
        assert claimed[0].attempts == 0

    async def test_enqueue_carries_the_traceparent_it_was_given(self, queue: JobQueue) -> None:
        """PRD 10's "why did the title I just opened take 45 seconds" is the
        worker's span linked back to the request that enqueued the work. A
        queue that drops the header makes that link unrecoverable, and
        nothing else in the pipeline would notice."""
        parent = "00-d14524c7eba73194c64d589cdd69488a-770641a119523a53-01"
        await queue.enqueue(
            [
                JobRequest(
                    kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW, traceparent=parent
                )
            ]
        )
        claimed = await queue.claim([JobKind.ENRICH])
        assert claimed[0].traceparent == parent

    async def test_an_empty_enqueue_is_a_no_op(self, queue: JobQueue) -> None:
        """A batch that matched nothing is the common case for a delta walk."""
        assert await queue.enqueue([]) == 0

    async def test_claiming_an_empty_queue_returns_nothing(self, queue: JobQueue) -> None:
        assert await queue.claim([JobKind.ENRICH]) == []

    async def test_a_claim_only_takes_the_kinds_it_asked_for(self, queue: JobQueue) -> None:
        """A worker pool that runs only `enrich` must not claim and then
        abandon every `match` job in the queue."""
        await queue.enqueue([JobRequest(kind=JobKind.MATCH, key="m1", priority=JobPriority.NEW)])
        assert await queue.claim([JobKind.ENRICH]) == []
        assert (await queue.depth())[JobKind.MATCH] == 1, "and it stays claimable by its own worker"

    async def test_a_claim_respects_its_limit(self, queue: JobQueue) -> None:
        """`job_batch_size` is what bounds a worker's in-flight work. A claim
        that ignored `limit` would take the whole 1,126,674-job backlog in
        one transaction."""
        await queue.enqueue(
            [
                JobRequest(kind=JobKind.ENRICH, key=f"t{index}", priority=JobPriority.NEW)
                for index in range(5)
            ]
        )
        assert len(await queue.claim([JobKind.ENRICH], limit=2)) == 2

    async def test_a_claimed_job_is_not_claimed_again(self, queue: JobQueue) -> None:
        """The single-worker half of `test_two_workers_never_claim_the_same_job`
        -- expressible everywhere, including in a fake. It catches a claim
        that forgot to write `status = 'running'` at all, which no amount of
        locking would fix."""
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
        assert len(await queue.claim([JobKind.ENRICH])) == 1
        assert await queue.claim([JobKind.ENRICH]) == []

    async def test_a_job_is_claimed_by_priority_then_age(self, queue: JobQueue) -> None:
        """`ORDER BY priority DESC, created_at ASC`, and both halves are
        asserted.

        Ascending priority serves background backfill ahead of a title a
        client is waiting on. No age tiebreak starves the oldest job at a
        given priority forever -- which is why `old-high` and `new-high`
        share a priority and are enqueued in separate calls, so their
        `created_at` genuinely differs and the assertion is on a full
        ordering rather than on set membership.
        """
        await queue.enqueue(
            [JobRequest(kind=JobKind.ENRICH, key="old-high", priority=JobPriority.DEMAND)]
        )
        await queue.enqueue(
            [JobRequest(kind=JobKind.ENRICH, key="old-low", priority=JobPriority.BACKFILL)]
        )
        await queue.enqueue(
            [JobRequest(kind=JobKind.ENRICH, key="new-high", priority=JobPriority.DEMAND)]
        )
        claimed = await queue.claim([JobKind.ENRICH], limit=3)
        assert [job.key for job in claimed] == ["old-high", "new-high", "old-low"]

    async def test_completing_a_job_removes_it_from_the_queue(self, queue: JobQueue) -> None:
        """`complete` deletes the row -- asserted through `requeue_running`,
        not through `depth`.

        `depth` counts `pending`, and a claimed job is already not pending, so
        a `complete` that did nothing at all still leaves `depth` at zero. Only
        a call that can see a `running` row tells "deleted" from "left
        claimed", and `requeue_running` is that call.
        """
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
        claimed = await queue.claim([JobKind.ENRICH])
        await queue.complete(claimed[0].id)
        assert await queue.requeue_running() == 0, "the row is gone, not merely still running"
        assert await queue.depth() == dict.fromkeys(JobKind, 0)
        assert await queue.claim([JobKind.ENRICH]) == []

    async def test_completing_an_unknown_job_is_not_an_error(self, queue: JobQueue) -> None:
        """A worker whose claim was requeued out from under it by a restart
        still calls `complete` when its work finishes."""
        await queue.complete(uuid.uuid4())

    async def test_a_failed_job_is_retried_after_a_backoff(
        self, queue: JobQueue, clear_backoff: ClearBackoff
    ) -> None:
        """Re-claiming instantly turns one broken upstream into a hot loop
        against it.

        Both directions are asserted: the job is held back while `run_after`
        stands, and it is claimable again once `run_after` is cleared. The
        second half is what stops "the implementation parked it" from passing
        the first.
        """
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
        claimed = await queue.claim([JobKind.ENRICH])
        job = await queue.fail(claimed[0].id, error="upstream said no", retryable=True)
        assert job is not None
        assert job.status is JobStatus.PENDING
        assert job.attempts == 1
        assert job.last_error == "upstream said no"
        assert job.run_after is not None, "a retry with no run_after is an instant re-claim"
        assert await queue.claim([JobKind.ENRICH]) == []
        await clear_backoff()
        assert [job.key for job in await queue.claim([JobKind.ENRICH])] == ["t1"]

    async def test_a_retry_keeps_its_place_in_the_queue(self, queue: JobQueue) -> None:
        """A failure is not a demotion. A job a client is waiting on that
        failed once must not fall behind the background backfill it was
        ahead of."""
        await queue.enqueue(
            [JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.DEMAND)]
        )
        claimed = await queue.claim([JobKind.ENRICH])
        job = await queue.fail(claimed[0].id, error="upstream said no", retryable=True)
        assert job is not None
        assert job.priority == JobPriority.DEMAND

    async def test_failing_an_unknown_job_returns_none(self, queue: JobQueue) -> None:
        """A worker whose claim was requeued out from under it by a restart
        -- the port's own words for this case. It must not raise, and it must
        not resurrect a row."""
        assert await queue.fail(uuid.uuid4(), error="gone", retryable=True) is None

    async def test_a_job_is_parked_after_the_attempt_ceiling_with_its_error(
        self, queue: JobQueue, clear_backoff: ClearBackoff
    ) -> None:
        """PRD 08: "after N attempts a job is *parked* with its error, not
        retried forever and not silently dropped."
        """
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
        job = None
        for _ in range(10):
            claimed = await queue.claim([JobKind.ENRICH], limit=1)
            if not claimed:
                await clear_backoff()
                claimed = await queue.claim([JobKind.ENRICH], limit=1)
            if not claimed:
                break
            job = await queue.fail(claimed[0].id, error="upstream said no", retryable=True)
        assert job is not None
        assert job.status is JobStatus.PARKED
        assert job.last_error == "upstream said no"
        assert job.run_after is None, "a parked job is not also waiting on a backoff"
        assert [parked.key for parked in await queue.parked()] == ["t1"]
        assert (await queue.depth())[JobKind.ENRICH] == 0, "parked work is not queue depth"

    async def test_malformed_data_parks_immediately_rather_than_backing_off(
        self, queue: JobQueue
    ) -> None:
        """`PortDataMalformed`'s own docstring: "the upstream answered, and
        the answer was wrong. Retrying does not help." An implementation that
        backs it off first produces five identical failures and delays a
        human seeing it by the whole backoff schedule. Asserting on
        `attempts == 1` is what distinguishes this from the ceiling park --
        that one reports the ceiling."""
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
        claimed = await queue.claim([JobKind.ENRICH])
        job = await queue.fail(claimed[0].id, error="TMDb returned a list", retryable=False)
        assert job is not None
        assert job.status is JobStatus.PARKED
        assert job.attempts == 1
        assert job.last_error == "TMDb returned a list"

    async def test_a_parked_job_is_not_claimed(
        self, queue: JobQueue, clear_backoff: ClearBackoff
    ) -> None:
        """A claim query missing `status = 'pending'` retries poison forever
        -- which is the failure parking exists to prevent, restored."""
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
        claimed = await queue.claim([JobKind.ENRICH])
        await queue.fail(claimed[0].id, error="bad", retryable=False)
        await clear_backoff()
        assert await queue.claim([JobKind.ENRICH]) == []

    async def test_a_parked_job_is_not_requeued_by_a_restart(self, queue: JobQueue) -> None:
        """`requeue_running` keyed on anything looser than `status = running`
        un-parks poison on every restart, which is the same failure as a
        claim that forgot the status filter, arriving through the recovery
        path instead."""
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
        claimed = await queue.claim([JobKind.ENRICH])
        await queue.fail(claimed[0].id, error="bad", retryable=False)
        assert await queue.requeue_running() == 0
        assert [parked.key for parked in await queue.parked()] == ["t1"]

    async def test_parked_jobs_are_bounded(self, queue: JobQueue) -> None:
        """The admin listing is a page, not the whole poison population."""
        for index in range(3):
            await queue.enqueue(
                [JobRequest(kind=JobKind.ENRICH, key=f"t{index}", priority=JobPriority.NEW)]
            )
        for claimed in await queue.claim([JobKind.ENRICH], limit=3):
            await queue.fail(claimed.id, error="bad", retryable=False)
        assert len(await queue.parked(limit=2)) == 2
        assert len(await queue.parked()) == 3

    async def test_nothing_is_parked_before_anything_fails(self, queue: JobQueue) -> None:
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
        assert await queue.parked() == []

    async def test_enqueueing_the_same_work_twice_does_not_duplicate_it(
        self, queue: JobQueue
    ) -> None:
        """A nightly walk enqueues a match job per item. Without
        `(kind, key)` uniqueness the second night's walk adds 1,126,674 more
        on top of the first night's."""
        request = JobRequest(kind=JobKind.MATCH, key="m1", priority=JobPriority.NEW)
        await queue.enqueue([request])
        await queue.enqueue([request])
        assert (await queue.depth())[JobKind.MATCH] == 1

    async def test_the_same_key_under_two_kinds_is_two_jobs(self, queue: JobQueue) -> None:
        """`key` is the *kind's* own identifier -- a `MediaItem.id` for
        `match` and for `watch_history` alike -- so uniqueness keyed on `key`
        alone would silently drop one of the two."""
        await queue.enqueue(
            [
                JobRequest(kind=JobKind.MATCH, key="shared", priority=JobPriority.NEW),
                JobRequest(kind=JobKind.WATCH_HISTORY, key="shared", priority=JobPriority.NEW),
            ]
        )
        depth = await queue.depth()
        assert depth[JobKind.MATCH] == 1
        assert depth[JobKind.WATCH_HISTORY] == 1

    async def test_a_duplicate_inside_one_batch_is_tolerated(self, queue: JobQueue) -> None:
        request = JobRequest(kind=JobKind.MATCH, key="m1", priority=JobPriority.NEW)
        await queue.enqueue([request, request])
        assert (await queue.depth())[JobKind.MATCH] == 1

    async def test_the_highest_priority_wins_inside_one_batch(self, queue: JobQueue) -> None:
        """Promote-never-demote has to hold within a batch as well as across
        batches: one walk can see the same item twice, once incidentally and
        once because a client asked for it."""
        await queue.enqueue(
            [
                JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.BACKFILL),
                JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.DEMAND),
                JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW),
            ]
        )
        claimed = await queue.claim([JobKind.ENRICH])
        assert claimed[0].priority == JobPriority.DEMAND

    async def test_re_enqueueing_at_a_higher_priority_promotes_the_existing_job(
        self, queue: JobQueue
    ) -> None:
        """M5's demand promotion, mechanically. `ON CONFLICT DO NOTHING`
        makes it impossible without a schema change."""
        await queue.enqueue(
            [JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.BACKFILL)]
        )
        await queue.enqueue(
            [JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.DEMAND)]
        )
        claimed = await queue.claim([JobKind.ENRICH])
        assert claimed[0].priority == JobPriority.DEMAND

    async def test_re_enqueueing_at_a_lower_priority_does_not_demote(self, queue: JobQueue) -> None:
        """`SET priority = excluded.priority` lets a background backfill
        sweep demote the job a client is blocked on."""
        await queue.enqueue(
            [JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.DEMAND)]
        )
        await queue.enqueue(
            [JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.BACKFILL)]
        )
        claimed = await queue.claim([JobKind.ENRICH])
        assert claimed[0].priority == JobPriority.DEMAND

    async def test_re_enqueueing_does_not_reset_the_age_tiebreak(self, queue: JobQueue) -> None:
        """A job re-seen by every nightly walk must not be pushed behind
        everything enqueued since. `created_at` is the starvation guard, and
        an upsert that refreshed it would defeat it silently -- the job stays
        claimable the whole time, it just never gets claimed."""
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="old", priority=JobPriority.NEW)])
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="new", priority=JobPriority.NEW)])
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="old", priority=JobPriority.NEW)])
        claimed = await queue.claim([JobKind.ENRICH], limit=2)
        assert [job.key for job in claimed] == ["old", "new"]

    async def test_re_enqueueing_does_not_unpark(
        self, queue: JobQueue, clear_backoff: ClearBackoff
    ) -> None:
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
        claimed = await queue.claim([JobKind.ENRICH])
        await queue.fail(claimed[0].id, error="bad", retryable=False)
        await queue.enqueue(
            [JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.DEMAND)]
        )
        await clear_backoff()
        assert await queue.claim([JobKind.ENRICH]) == []
        assert [parked.key for parked in await queue.parked()] == ["t1"]

    async def test_re_enqueueing_a_parked_job_reports_nothing_written(
        self, queue: JobQueue
    ) -> None:
        """The return value is "rows written", and a parked row is not
        written. A count that included it would make a walk's "enqueued
        1,126,674 jobs" log line count work it declined to touch."""
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
        claimed = await queue.claim([JobKind.ENRICH])
        await queue.fail(claimed[0].id, error="bad", retryable=False)
        assert (
            await queue.enqueue(
                [JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.DEMAND)]
            )
            == 0
        )

    async def test_requeue_running_recovers_jobs_from_an_unclean_shutdown(
        self, queue: JobQueue
    ) -> None:
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
        await queue.claim([JobKind.ENRICH])
        assert await queue.requeue_running() == 1
        assert [job.key for job in await queue.claim([JobKind.ENRICH])] == ["t1"]

    async def test_requeue_running_keeps_the_attempt_count_and_the_error(
        self, queue: JobQueue, clear_backoff: ClearBackoff
    ) -> None:
        """A job that keeps killing its worker must still reach the attempt
        ceiling. Clearing `attempts` on requeue turns a crash loop into an
        infinite one, which is the failure parking exists to end -- and it
        would be invisible, because the job stays perfectly claimable
        throughout."""
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
        claimed = await queue.claim([JobKind.ENRICH])
        await queue.fail(claimed[0].id, error="upstream said no", retryable=True)
        await clear_backoff()
        await queue.claim([JobKind.ENRICH])
        assert await queue.requeue_running() == 1
        await clear_backoff()
        recovered = await queue.claim([JobKind.ENRICH])
        assert [job.attempts for job in recovered] == [1]
        assert recovered[0].last_error == "upstream said no"

    async def test_requeue_running_leaves_pending_work_alone(self, queue: JobQueue) -> None:
        """It returns "how many", and a count inflated by every pending job
        makes "recovered 1,126,674 claims" the log line after every clean
        restart."""
        await queue.enqueue([JobRequest(kind=JobKind.ENRICH, key="t1", priority=JobPriority.NEW)])
        assert await queue.requeue_running() == 0

    async def test_depth_reports_every_kind_including_the_empty_ones(self, queue: JobQueue) -> None:
        """A `GROUP BY` returns only non-empty kinds, and a Prometheus gauge
        that stops reporting a series is indistinguishable from one reporting
        zero."""
        await queue.enqueue([JobRequest(kind=JobKind.MATCH, key="m1", priority=JobPriority.NEW)])
        assert set(await queue.depth()) == set(JobKind)
        assert (await queue.depth())[JobKind.ENRICH] == 0

    async def test_depth_does_not_count_claimed_work(self, queue: JobQueue) -> None:
        """`usher.jobs.queued` is what is waiting for a worker. Counting
        in-flight work in it makes a queue that is draining perfectly look
        stuck."""
        await queue.enqueue([JobRequest(kind=JobKind.MATCH, key="m1", priority=JobPriority.NEW)])
        await queue.claim([JobKind.MATCH])
        assert (await queue.depth())[JobKind.MATCH] == 0

    async def test_two_workers_never_claim_the_same_job(
        self, concurrent_claims: ConcurrentClaimHarness | None
    ) -> None:
        """`FOR UPDATE` without `SKIP LOCKED` serialises the workers instead
        of distributing them; a plain `SELECT` followed by an `UPDATE` hands
        the same row to both, and the job runs twice.

        Two assertions, and the second is the one that matters. "Exactly one
        claimer got the job" is also what a *serialised* pair of claims
        produces, so it proves nothing on its own -- hence
        `overlapping(...)`, which fails unless the two claims genuinely
        occupied the same instant. The harness raises rather than hangs when
        a claim blocks, which is what an unskipped `FOR UPDATE` does.

        Skipped for any implementation that does not set
        `requires_concurrency` -- see the module docstring for why pretending
        otherwise would be worse than skipping.
        """
        if not self.requires_concurrency:
            pytest.skip("this implementation cannot express concurrent claims")
        assert concurrent_claims is not None, "requires_concurrency needs a harness"
        windows = await concurrent_claims.run(keys=["t1"], claimers=2)
        assert sum(len(window.keys) for window in windows) == 1, [w.keys for w in windows]
        assert overlapping(windows), f"the claims did not overlap: {windows}"
