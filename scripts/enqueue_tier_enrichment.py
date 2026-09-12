"""Enqueue the priority tier for enrichment, one keyset page at a time."""

import argparse
import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.config import get_settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.jobs import PostgresJobQueue
from usher.domain.enums import TitleKind
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.title import Title
from usher.ports.jobs import JobQueue, JobRequest

# The vote floor PRD 04 calls tier 1. Named rather than spelled twice, so the
# SQL below and `is_tier_movie` cannot drift on the one number an operator
# would think to change.
TIER_MIN_VOTES = 100

# Keyset, not `OFFSET`: `MediaItemRepository.list_unmatched`'s offset walk is measured
# at 43.7 ms at offset 0 and 388.9 ms at offset 1,126,574 -- linear per page, quadratic
# to drain -- which is the exact shape of walk this is.
_PAGE = """
SELECT id, kind, name, sort_name, imdb_num_votes, tmdb_id
FROM titles
WHERE kind = 'movie'
  AND imdb_num_votes >= :min_votes
  AND tmdb_id IS NOT NULL
  AND (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid))
ORDER BY id
LIMIT :size
"""

#: `(after, size) -> one page of titles, id-ascending`. A callable rather than
#: a port: `TitleRepository` has no read shaped like this, adding one would be
#: a port method whose only caller is a script, and the unit case needs to
#: count the *asks* rather than the rows.
PageReader = Callable[[uuid.UUID | None, int], Awaitable[Sequence[Title]]]


@dataclass(frozen=True, slots=True)
class EnqueueOutcome:
    """What the walk did, in the four numbers an operator has to reconcile.

    `read` and `matched` differ only when the SQL and Python predicates
    disagree, so printing both is how that disagreement becomes visible
    rather than silent. `written` is `enqueue`'s own return value summed —
    below `matched` on a re-run, because an existing `(kind, key)` is
    promoted rather than written.
    """

    pages: int
    read: int
    matched: int
    written: int


def is_tier_movie(title: Title) -> bool:
    """The priority tier, one title at a time.

    `>= TIER_MIN_VOTES` and not `>`: PRD 04's tier is "≥100 votes", and a
    film sitting exactly on the floor is inside it.
    """
    return (
        title.kind is TitleKind.MOVIE
        and title.imdb_num_votes is not None
        and title.imdb_num_votes >= TIER_MIN_VOTES
        and title.tmdb_id is not None
    )


async def enqueue_tier(
    *,
    read_page: PageReader,
    queue: JobQueue,
    commit: Callable[[], Awaitable[None]],
    limit: int,
    page_size: int,
    priority: int = JobPriority.BACKFILL,
) -> EnqueueOutcome:
    """Walk the tier on a keyset cursor and enqueue `JobKind.ENRICH`.

    `JobPriority.BACKFILL` (20) and not `NEW` (50): this is a background
    sweep, and it must lose to the demand promotion a client's read issues
    while it drains. `enqueue`'s `GREATEST` clause means a title this walk
    has already queued is *promoted* by a later demand read rather than
    stuck behind 130,805 others.

    One `enqueue` and one `commit` per page rather than one at the end, so a
    run interrupted at row 90,000 has written 90,000 rows and the next run
    resumes rather than restarts.
    """
    after: uuid.UUID | None = None
    outcome = EnqueueOutcome(pages=0, read=0, matched=0, written=0)
    while outcome.matched < limit:
        remaining = limit - outcome.matched
        page = await read_page(after, min(page_size, remaining))
        outcome = EnqueueOutcome(
            pages=outcome.pages + 1,
            read=outcome.read + len(page),
            matched=outcome.matched,
            written=outcome.written,
        )
        if not page:
            break
        # Before the predicate is consulted and whatever it says. A cursor
        # advanced on the last *enqueued* id cannot get past a page nothing
        # in it clears.
        after = page[-1].id
        requests = [
            JobRequest(kind=JobKind.ENRICH, key=str(title.id), priority=priority)
            for title in page
            if is_tier_movie(title)
        ][:remaining]
        if not requests:
            continue
        written = await queue.enqueue(requests)
        await commit()
        outcome = EnqueueOutcome(
            pages=outcome.pages,
            read=outcome.read,
            matched=outcome.matched + len(requests),
            written=outcome.written + written,
        )
    return outcome


async def _page(session: AsyncSession, after: uuid.UUID | None, size: int) -> list[Title]:
    """One page, as `Title`s carrying only the columns the walk reads.

    Six columns rather than all thirty-one: the predicate reads four of them
    and `JobRequest.key` reads the fifth, `sort_name` is `NOT NULL` on the
    model, and nothing downstream of `enqueue_tier` is handed these objects.
    This is a keyset page, not a hydration — `TitleRepository.list_by_ids` is
    the method for that.
    """
    rows = (
        (
            await session.execute(
                text(_PAGE), {"min_votes": TIER_MIN_VOTES, "after": after, "size": size}
            )
        )
        .mappings()
        .all()
    )
    return [
        Title(
            id=row["id"],
            kind=TitleKind(row["kind"]),
            name=row["name"],
            sort_name=row["sort_name"],
            imdb_num_votes=row["imdb_num_votes"],
            tmdb_id=row["tmdb_id"],
        )
        for row in rows
    ]


async def enqueue(limit: int, page_size: int) -> None:
    settings = get_settings()
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            queue = PostgresJobQueue(
                session,
                max_attempts=settings.job_max_attempts,
                backoff_seconds=settings.job_backoff_seconds,
            )

            async def read_page(after: uuid.UUID | None, size: int) -> Sequence[Title]:
                return await _page(session, after, size)

            outcome = await enqueue_tier(
                read_page=read_page,
                queue=queue,
                commit=session.commit,
                limit=limit,
                page_size=page_size,
            )
            print(
                f"{outcome.pages} pages, {outcome.read} rows read, "
                f"{outcome.matched} in the tier, {outcome.written} jobs written"
            )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=200_000,
        help="stop after this many jobs; the bound is on the next page asked for",
    )
    parser.add_argument("--page-size", type=int, default=1_000)
    arguments = parser.parse_args()
    asyncio.run(enqueue(arguments.limit, arguments.page_size))


if __name__ == "__main__":
    main()
