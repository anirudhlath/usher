"""`VisibilityService` — the read surfaces' half of PRD 03's demand lane.

`JobPriority.VISIBLE` was defined in M4 carrying the comment *"in a row the
client just requested (M5)"* and had exactly one call site in the tree by
2026-08-26, in `watch_write.py`, which is not a read surface. So every catalog
screen returned skeletons it never promoted, and the only way to enrich one was
to open it — `GET /titles/{id}` one title at a time, against a catalog that is
1,139,982 skeletons.
"""

import uuid
from collections.abc import Sequence

from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.title import Title
from usher.ports.jobs import JobRequest
from usher.services.visibility import VisibilityService


def _title(name: str, *, state: EnrichmentState = EnrichmentState.SKELETON) -> Title:
    return Title(
        kind=TitleKind.MOVIE,
        name=name,
        sort_name=name,
        enrichment_state=state,
    )


async def test_a_skeleton_a_client_was_shown_is_promoted_to_visible() -> None:
    queue = FakeJobQueue()
    skeleton = _title("A skeleton")

    promoted = await VisibilityService(queue, FakeTitleRepository()).seen([skeleton])

    assert promoted == 1
    assert [job.key for job in queue.jobs_of(JobKind.ENRICH)] == [str(skeleton.id)]
    assert queue.jobs_of(JobKind.ENRICH)[0].priority == JobPriority.VISIBLE


async def test_an_already_enriched_title_is_not_promoted() -> None:
    """`TitleReadService`'s guard, in the plural. A screen is mostly enriched
    titles on a warm catalog, so promoting them would be one wasted row per
    card forever -- and `enqueue`'s `AND jobs.priority < excluded.priority`
    would not even record it, so the cost would be invisible as well as
    pointless."""
    queue = FakeJobQueue()

    promoted = await VisibilityService(queue, FakeTitleRepository()).seen(
        [_title("Done", state=EnrichmentState.ENRICHED)]
    )

    assert promoted == 0
    assert queue.jobs_of(JobKind.ENRICH) == []


async def test_a_stub_is_promoted_because_a_stub_is_not_finished() -> None:
    """Three rungs, not two. `ENRICHMENT_RANK` is what separates them, and the
    spelling `state is SKELETON` -- which is the obvious one and reads
    correctly -- silently strands every stub on a screen forever."""
    queue = FakeJobQueue()

    promoted = await VisibilityService(queue, FakeTitleRepository()).seen(
        [_title("Half", state=EnrichmentState.STUB)]
    )

    assert promoted == 1
    assert queue.jobs_of(JobKind.ENRICH)[0].priority == JobPriority.VISIBLE


async def test_a_page_with_nothing_to_promote_does_not_touch_the_queue_at_all() -> None:
    """**The cost guard, and it is the reason this is safe to call per page
    view.** `JobQueue.enqueue` is a staged write -- a temp DDL, a COPY and one
    `INSERT ... SELECT ... ON CONFLICT` -- so calling it with an empty list is
    a full staging cycle that writes nothing, per request, on the hot read
    path. M6 already had to fix that shape of cost once when `stg_jobs`' shared
    name turned out to be an ACCESS EXCLUSIVE lock on the hot path, measured at
    819 ms of mutual waiting.

    A warm catalog's screens are mostly enriched, so this is the *ordinary*
    path rather than an edge case: on a fully enriched page the whole mechanism
    costs one list comprehension.
    """
    queue = FakeJobQueue()
    calls: list[Sequence[JobRequest]] = []
    original = queue.enqueue

    async def counting(requests: Sequence[JobRequest]) -> int:
        calls.append(requests)
        return await original(requests)

    queue.enqueue = counting  # type: ignore[method-assign]

    promoted = await VisibilityService(queue, FakeTitleRepository()).seen(
        [_title("Done", state=EnrichmentState.ENRICHED)]
    )

    assert promoted == 0
    assert calls == [], "an empty promotion must not reach the queue at all"


async def test_a_page_of_skeletons_is_one_call_carrying_many_requests() -> None:
    """The same staging argument as `EnrichService._apply`'s follow-ups, at
    page scale rather than at two: a request per skeleton is one full staging
    cycle per card.

    **A case that only asserted every title was enqueued is green against the
    per-title version**, which is the version somebody writes by moving the
    enqueue inside the loop. So this counts the calls.
    """
    queue = FakeJobQueue()
    page = [_title(f"Skeleton {n}") for n in range(20)]
    calls: list[int] = []
    original = queue.enqueue

    async def counting(requests: Sequence[JobRequest]) -> int:
        calls.append(len(requests))
        return await original(requests)

    queue.enqueue = counting  # type: ignore[method-assign]

    promoted = await VisibilityService(queue, FakeTitleRepository()).seen(page)

    assert promoted == 20
    assert calls == [20], "one call carrying twenty requests, not twenty calls"


async def test_one_title_twice_on_a_screen_is_promoted_once() -> None:
    """A title can appear on two shelves of one composed screen -- `/home`
    builds nine row providers over one catalog and nothing stops a film being
    both recently-added and a genre affinity. `enqueue` is keyed on
    `(kind, key)` so the duplicate is harmless at the database, but it is a
    row COPYed and then discarded, and the count this returns is read by a
    caller as "titles promoted"."""
    queue = FakeJobQueue()
    twice = _title("On two shelves")

    promoted = await VisibilityService(queue, FakeTitleRepository()).seen([twice, twice])

    assert promoted == 1
    assert [job.key for job in queue.jobs_of(JobKind.ENRICH)] == [str(twice.id)]


# -- the id-based entry point ---------------------------------------------


async def test_ids_are_resolved_before_they_are_judged() -> None:
    """`GET /search` never holds a `Title`.

    `SearchResult` carries `title_id`, `kind`, `name`, `year`, `popularity`,
    `owned` and `score` and **no `enrichment_state`** (issue #52), so the one
    surface that most obviously knows what a client is looking for cannot
    answer "is this a skeleton" from what it already has. `seen_ids` resolves
    them; `seen` stays the entry point for a caller that has hydrated titles
    already and must not pay a second read for them.
    """
    titles = FakeTitleRepository()
    skeleton = _title("A skeleton")
    done = _title("Already done", state=EnrichmentState.ENRICHED)
    await titles.add(skeleton)
    await titles.add(done)
    queue = FakeJobQueue()

    promoted = await VisibilityService(queue, titles).seen_ids([skeleton.id, done.id])

    assert promoted == 1
    assert [job.key for job in queue.jobs_of(JobKind.ENRICH)] == [str(skeleton.id)]


async def test_an_id_the_catalog_no_longer_holds_is_dropped_rather_than_raising() -> None:
    """`list_by_ids` answers only what it holds -- a title deleted between an
    index write and a search read is ordinary, and the port says so. A search
    that 500s because one stale hit came back is a worse failure than the stale
    hit."""
    titles = FakeTitleRepository()
    queue = FakeJobQueue()

    promoted = await VisibilityService(queue, titles).seen_ids([uuid.uuid4()])

    assert promoted == 0
    assert queue.jobs_of(JobKind.ENRICH) == []


async def test_no_ids_reads_nothing_and_enqueues_nothing() -> None:
    """The read is as much a per-request cost as the write, and an empty result
    set is the ordinary answer to a query that matched nothing."""
    titles = FakeTitleRepository()
    reads: list[object] = []
    original = titles.list_by_ids

    async def counting(title_ids: Sequence[uuid.UUID]) -> list[Title]:
        reads.append(title_ids)
        return await original(title_ids)

    titles.list_by_ids = counting  # type: ignore[method-assign]

    promoted = await VisibilityService(FakeJobQueue(), titles).seen_ids([])

    assert promoted == 0
    assert reads == [], "an empty page must not reach the repository either"
