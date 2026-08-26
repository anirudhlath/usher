"""PRD 03's demand lane, for the screens rather than for one title.

`JobPriority.VISIBLE` was defined in M4 with the comment *"in a row the client
just requested (M5)"* and the promotion clause in the enqueue statement
(`SET priority = GREATEST(...)`) was written against it in the same milestone.
M5 then shipped the *singular* half — `GET /titles/{id}` promotes the title it
answers to `JobPriority.DEMAND` (`services/titles.py`) — and nothing was ever
built for the plural. Measured 2026-08-26: `browse` and `rows/base` did not
import `JobPriority` at all, `search` named it only in a docstring, and the one
call site of `VISIBLE` in the tree was `watch_write.py`, which is a write path.

**The consequence is the shape of the catalog rather than a missed feature.**
1,139,982 of 1,273,313 titles were `skeleton`, so ~89% of what a browse page
can return is a name and a year — and paging past it was the one interaction
guaranteed never to improve it. A user could only fix a skeleton by opening it,
one at a time, on the single surface that had been wired.

**This is a service and not four call sites.** The rule wanted is *any read
surface that returns titles it intends to draw*, so a surface added later
inherits it instead of re-deciding it; `/browse`, `/search`, `/search/suggest`
and `/home` are what exist today and are not the definition.

**Why this is safe to call on every page view**, which is the question a
promotion on a hot read path has to answer:

- **Nothing to promote costs nothing.** `seen` returns before touching the
  queue when no title is below `ENRICHED`, and on a warm catalog that is the
  ordinary case. `JobQueue.enqueue` is a staged write — a temp DDL, a COPY and
  one `INSERT ... SELECT ... ON CONFLICT` — so an unconditional call would be
  a full staging cycle per request that writes nothing.
- **A repeat is free at the database.** The enqueue statement promotes with
  `GREATEST` under `AND jobs.priority < excluded.priority`, so a second page
  view of the same rows finds nothing left to promote and writes no row.
- **`parked` stays parked.** The same statement carries
  `WHERE jobs.status <> 'parked'`, so a title TMDb has permanently refused is
  not revived by being scrolled past — which, without that clause, is how a
  screen turns one dead title into an unbounded retry loop.

**What this deliberately does not do is bound the page.** The caller passes
what it is about to draw and that is already bounded by the surface's own
limit; a second cap here would be a policy this service cannot see the units
of. The open question that remains is *volume across page views* — a client
paging quickly enqueues each page once — and it wants a measurement rather
than an assumed constant. Issue #73 records it as open rather than answered.
"""

import uuid
from collections.abc import Iterable, Sequence

from usher.domain.enums import ENRICHMENT_RANK, EnrichmentState
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.title import Title
from usher.ports.jobs import JobQueue, JobRequest
from usher.ports.repository import TitleRepository
from usher.telemetry import current_traceparent

_FINISHED = ENRICHMENT_RANK[EnrichmentState.ENRICHED]


class VisibilityService:
    """Promote the skeletons a client was just shown."""

    def __init__(self, queue: JobQueue, titles: TitleRepository) -> None:
        # The *same* queue the worker claims from, which only a composition
        # root can know -- `EnrichService` takes its own for this reason and
        # says so. A defaulted queue here is how a screen ends up promoting
        # into an object nothing ever claims from, on the one path whose
        # failure mode is that nothing visibly happens.
        self._queue = queue
        # Only `seen_ids` reads this. Required rather than optional because an
        # optional repository makes "this deployment cannot resolve ids" and
        # "these ids are all enriched" the same answer -- zero — and the
        # surface that needs it most is the one with the strongest intent
        # signal.
        self._titles = titles

    async def seen(self, titles: Iterable[Title]) -> int:
        """Enqueue one `enrich` at `VISIBLE` per unfinished title. Returns how
        many were promoted.

        The guard is `ENRICHMENT_RANK`, never `state is SKELETON`: there are
        three rungs and the direct spelling strands every `stub` on a screen
        forever. It is also never a `>` comparison on the enum itself --
        `EnrichmentState` is a `StrEnum`, so `ENRICHED > SKELETON` is `False`
        and a guard spelled that way promotes nothing at all, silently
        (ADR-0008).

        Deduplicated because one title can sit on two shelves of one composed
        screen, and the count returned is read as "titles promoted".
        """
        return await self._promote(titles)

    async def seen_ids(self, title_ids: Sequence[uuid.UUID]) -> int:
        """`seen`, for a surface that never held a `Title`.

        `GET /search` is the case this exists for: `SearchResult` carries
        `title_id` and no `enrichment_state` (issue #52), so the surface with
        the strongest intent signal in the API cannot answer "is this a
        skeleton" from what it already has. One `WHERE id = ANY(...)` on the
        primary key, bounded by the caller's own result limit.

        **Empty in, nothing read.** A query that matched nothing is the
        ordinary answer, and the read is as much a per-request cost as the
        write the guard in `_promote` already covers.

        An id the catalog no longer holds is simply absent from the answer --
        `list_by_ids` promises only what it holds, because a title deleted
        between an index write and a search read is ordinary. Dropped rather
        than raised: a search that 500s over one stale hit is a worse failure
        than the stale hit.
        """
        if not title_ids:
            return 0
        return await self._promote(await self._titles.list_by_ids(list(title_ids)))

    async def _promote(self, titles: Iterable[Title]) -> int:
        unfinished: dict[str, None] = {}
        for title in titles:
            if ENRICHMENT_RANK[title.enrichment_state] < _FINISHED:
                unfinished[str(title.id)] = None
        if not unfinished:
            return 0
        traceparent = current_traceparent()
        await self._queue.enqueue(
            [
                JobRequest(
                    kind=JobKind.ENRICH,
                    key=key,
                    priority=JobPriority.VISIBLE,
                    traceparent=traceparent,
                )
                for key in unfinished
            ]
        )
        return len(unfinished)
