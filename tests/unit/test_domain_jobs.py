"""The queue's domain vocabulary."""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usher.domain.ids import new_id
from usher.domain.jobs import Job, JobKind, JobPriority, JobStatus


def test_a_job_is_pending_with_no_attempts_by_default() -> None:
    job = Job(kind=JobKind.ENRICH, key=str(new_id()))
    assert job.status is JobStatus.PENDING
    assert job.attempts == 0
    assert job.last_error is None
    assert isinstance(job.id, uuid.UUID)


def test_there_is_no_done_status() -> None:
    """A completed job's row is deleted, not marked. Keeping 1.1M terminal
    rows makes PRD 10's queue-depth panel a scan over a table that only
    grows, and it is the reason `complete()` on the port returns nothing to
    inspect."""
    assert set(JobStatus) == {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.PARKED}


def test_priorities_match_the_prd_table_and_higher_is_more_urgent() -> None:
    """PRD 03's read-through table. The direction is load-bearing: a queue
    ordered `ORDER BY priority` ascending would serve background backfill
    ahead of a title a client is waiting on, and the numbers alone do not
    say which way round the ORDER BY goes."""
    assert JobPriority.DEMAND.value == 100
    assert JobPriority.VISIBLE.value == 80
    assert JobPriority.NEW.value == 50
    assert JobPriority.BACKFILL.value == 20
    assert JobPriority.DEMAND > JobPriority.VISIBLE > JobPriority.NEW > JobPriority.BACKFILL


def test_priority_ordering_is_arithmetic_not_lexicographic() -> None:
    """The trap `ENRICHMENT_RANK` exists for (ADR-0008), avoided here by the
    type rather than by a side table: `StrEnum` members compare as strings,
    so a string-valued scale would order "100" < "20" < "50" < "80" and put
    DEMAND last. `IntEnum` is what makes `GREATEST(priority, excluded.
    priority)` and `ORDER BY priority DESC` mean what they read as."""
    assert [p.value for p in sorted(JobPriority)] == [20, 50, 80, 100]
    assert sorted(str(p.value) for p in JobPriority) == ["100", "20", "50", "80"]
    # And a member *is* an int, which is what lets it be bound straight into
    # an `integer` column by asyncpg's strictly-typed driver without a cast.
    assert isinstance(JobPriority.NEW, int)


def test_a_priority_outside_the_scale_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Job(kind=JobKind.MATCH, key="k", priority=101)
    with pytest.raises(ValidationError):
        Job(kind=JobKind.MATCH, key="k", priority=-1)


def test_a_negative_attempt_count_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Job(kind=JobKind.MATCH, key="k", attempts=-1)


def test_an_empty_key_is_rejected() -> None:
    """`(kind, key)` is the dedup target. An empty key would collapse every
    job of a kind onto one row -- 1.1M match jobs becoming one."""
    with pytest.raises(ValidationError):
        Job(kind=JobKind.MATCH, key="")


def test_the_seven_kinds_this_tree_ships() -> None:
    """Each kind arrives with the artefacts it maintains, not before.

    M4's version asserted three members and explained the absence: "a job
    kind whose handler is a stub is a queue that grows forever". M6 retired
    that reasoning for `index` rather than reversing it, shipping the
    handler, the enqueue and the draining backfill together, and M7 does the
    same for `derive` -- the member, the handler, the enqueue site and
    `usher derive` in one commit.

    **`curate` is the first member whose handler is registered
    conditionally**, which is a different question from the rule above and
    does not weaken it: the handler exists in every build, and
    `composition.build_worker` withholds the *registration* from a deployment
    with no `LLMClient`, exactly as it withholds `index` from one with no
    embedder. What M4 forbade was a member with no handler anywhere.

    **`watch_writeback` arrived across two commits and the rule held anyway.**
    M9's D7 owns the enqueue -- the four watch-write routes cannot enqueue a
    kind that does not exist -- and D8 owns the handler and the unconditional
    registration. Between them there really was a member no worker claimed,
    which is exactly the queue M4 forbade; the marker that said so is struck
    because D8 has landed, and what is left is the ordinary rule. Had D8 been
    dropped, the member would have gone with it.

    An exact set rather than a membership check, so an eighth kind cannot be
    added without this list moving and someone reading that rule.
    """
    assert set(JobKind) == {
        JobKind.MATCH,
        JobKind.ENRICH,
        JobKind.WATCH_HISTORY,
        JobKind.INDEX,
        JobKind.DERIVE,
        JobKind.CURATE,
        JobKind.WATCH_WRITEBACK,
    }


def test_every_member_of_every_enum_is_its_stored_value() -> None:
    """These three enums reach `enum_column`, which stores each member's
    `.value`. A member whose value drifted from its wire spelling would be
    written to Postgres under the new spelling and silently stop matching
    the partial-index predicates (`WHERE status = 'pending'`) written
    against the old one.

    `index` is the most exposed of the four: it is a SQL keyword and a
    plausible thing to "clarify" to `search_index`, at which point every row
    a previous release wrote is unclaimable and `claim` reports an empty
    queue rather than an error.
    """
    assert {k.value for k in JobKind} == {
        "match",
        "enrich",
        "watch_history",
        "index",
        "derive",
        "curate",
        "watch_writeback",
    }
    assert {s.value for s in JobStatus} == {"pending", "running", "parked"}


def test_a_job_carries_a_traceparent_so_a_slow_title_is_one_query() -> None:
    """PRD 10: "why did the title I just opened take 45 seconds" is one
    query. The enqueue happens inside a request's span and the execution
    happens in a worker minutes later, so the only thing that joins them is
    the W3C trace context carried on the row."""
    job = Job(
        kind=JobKind.ENRICH,
        key="k",
        traceparent="00-d14524c7eba73194c64d589cdd69488a-770641a119523a53-01",
    )
    assert job.traceparent is not None


def test_a_job_is_frozen() -> None:
    job = Job(kind=JobKind.MATCH, key="k")
    with pytest.raises(ValidationError):
        job.attempts = 3  # type: ignore[misc]
    assert job.evolve(attempts=3, run_after=datetime.now(UTC)).attempts == 3


def test_evolve_revalidates_a_promoted_priority() -> None:
    """The promotion clause is `SET priority = GREATEST(...)`, and the
    domain-side equivalent is an `.evolve()`. `model_copy(update=...)` would
    accept 500 without complaint; `.evolve()` re-runs the bound."""
    job = Job(kind=JobKind.MATCH, key="k")
    assert job.evolve(priority=JobPriority.DEMAND).priority == 100
    with pytest.raises(ValidationError):
        job.evolve(priority=500)


def test_a_job_rejects_an_unknown_field() -> None:
    """`extra="forbid"`. `Job(kind=..., key=..., attempt=1)` -- singular --
    would otherwise construct a job whose attempt counter is 0."""
    with pytest.raises(ValidationError):
        Job(
            kind=JobKind.MATCH,
            key="k",
            attempt=1,  # type: ignore[call-arg]  # deliberate typo of attempts
        )
