"""Enqueue the priority tier for enrichment, one keyset page at a time.

**Not a test.** It writes one `jobs` row per title into a real database, so
it never runs in CI and it is not part of the package. `usher work` is what
drains what this enqueues, and *that* is the thing that spends the TMDb
budget — this script only writes rows.

    export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
    export USHER_SECRET_KEY="<32+ char secret>"
    uv run python scripts/enqueue_tier_enrichment.py --limit 500
    uv run usher work

There is no `usher enrich --backfill` and this script is deliberately not one:
`src/usher/cli.py`'s thirteen subcommands do not include an `enrich`, PRD 09
assigns no such command, and `CLAUDE.md` forbids inventing tooling. The
capability ships as an operations script, the shape `scripts/measure_rows.py`
already has.

**The tier is `kind = 'movie' AND imdb_num_votes >= 100 AND tmdb_id IS NOT
NULL`,
and the third conjunct is a correctness property rather than an
optimisation.** 161,789 movies carried `>= 100` votes when this was
measured; only **130,806**
also carry a `tmdb_id` (measured 2026-08-11 on the M9 catalog). For the other
30,983, `EnrichService._ref_for` raises `PortDataMalformed`, whose contract in
queue form is `retryable=False` — the job parks on its **first** attempt and
needs a human to release it. Dropping the conjunct buys 30,983 parked rows,
no data, and a `usher sync-status` an operator can no longer read.

**Two spellings of the predicate, on purpose, and the Python one is the
authority.** `_PAGE` narrows in SQL so the walk does not drag 1,272,367 rows
across the wire; `is_tier_movie` is applied again to every row the page
reader hands back, and it is what decides whether a job is written. The
asymmetry is the point: a SQL predicate that is accidentally *too wide* — the
one risk above, and the one that costs 30,983 parked rows — is caught by the
Python guard and costs nothing, while a SQL predicate that is too narrow
costs coverage the next run picks up. The two must agree, so keep them in
view of each other; `tests/unit/test_scripts_enqueue_tier_enrichment.py`
pins the Python half and nothing pins the SQL half but this sentence.

**The bound is in the iterator.** `--limit` is subtracted from the size of
the *next page asked for*, so the walk stops reading, not merely stops
writing. `CLAUDE.md`'s rule ("the bound has to be in the iterator, not in
`max_pages`") is about a live run against a real server, and it applies to a
walk over 130,806 rows for the same reason. `max_pages` does not exist here
anyway — it is an Emby adapter concept (`adapters/emby/adapter.py:251`) and
exhausting it raises `PortDataMalformed`.

**The cursor advances on the last id of the page, unconditionally**, before
the predicate is consulted. A cursor advanced on the last *enqueued* id
cannot get past a page nothing in it clears, which on this catalog is the
ordinary page rather than the exotic one.

**Re-running is cheap and that is what makes the full run resumable.**
`enqueue` deduplicates on `(kind, key)` and promotes rather than duplicates,
so a second pass over the same prefix writes nothing new; and a title already
enriched inside `USHER_ENRICH_CACHE_MAX_AGE_DAYS` (default 30) re-reads its
`raw_payloads` row and spends **no TMDb request** when the job runs — measured
2026-08-11, twenty already-cached titles re-enqueued by id and drained for
zero outbound requests.

⚠️ **The predicate is not stationary: enriching a title can remove it from the
tier, so a second run of this script does not select the same rows.**
`tmdb_vote_count` is in `EnrichService._ENRICHABLE`; the bulk loader writes
IMDb `numVotes` into that column through `apply_ratings` and enrichment
overwrites it with TMDb's own `vote_count`, which is a different electorate.
**That dual write is what ADR-0040 is about**, and `m10a` renamed `vote_count`
to `tmdb_vote_count`, which made an IMDb writer landing there legible without
stopping it.

✅ **ADR-0040's Task 2 stopped that dual write, which briefly emptied this
predicate, and the repair is the measurement below (2026-08-19).**
`apply_ratings` now writes `imdb_num_votes`/`imdb_average_rating`, and nothing
else fills `tmdb_vote_count`: `upsert_titles` omits it from its `DO UPDATE`
list and `link_crosswalk` writes only `tmdb_popularity`. So a tier spelled
`tmdb_vote_count >= 100` reads **NULL >= 100 on every row of a freshly
bootstrapped catalog** — zero rows, and the enrichment crawl this script exists
to start could not start itself. Filed as issue #42.

**The repair is a restoration, not a re-choice, and that is measured rather
than argued.** The concern that deferred it was that `>= 100` against IMDb
counts selects a different tier from `>= 100` against TMDb ones — the two
scales differ by ~38x (paired: median TMDb 15 against median IMDb 576 over the
same 130,647 enriched rows). That concern does not survive contact with the
catalog, because **the tier was always de facto an IMDb-votes tier**: the
column held IMDb `numVotes` on exactly the skeleton rows where every
unenriched candidate lives, so `>= 100` has been reading IMDb counts for as
long as this script has existed. Measured on the deployed 1,272,870-title
catalog after the ADR-0040 rebuild:

| predicate | recorded 2026-08-11 | `imdb_num_votes` today |
|---|---|---|
| `kind='movie' AND >= 100` | 161,789 | **162,196** (+0.25%) |
| `+ tmdb_id IS NOT NULL` | 130,806 | **131,113** (+0.23%) |

Both within a quarter of a percent of the figures every downstream statistic in
`.claude/rules/tmdb-and-enrichment.md` is quoted against, and the residual is
eight days of vote growth. So those statistics remain valid, which is precisely
what a re-choice would have destroyed. The same restoration ADR-0040 made for
the eval sampling frame, one predicate over.

⚠️ **What did change is stationarity, and in the right direction.** The old
spelling was non-stationary *by construction* — `tmdb_vote_count` is in
`EnrichService._ENRICHABLE`, so enriching a title rewrote the very column the
tier selected on and a second run selected different rows. `imdb_num_votes` has
one writer, `apply_ratings`, which no crawl touches; the tier now moves only
when IMDb publishes a new dump.

**Two spellings of the predicate, on purpose, and the Python one is the
authority.** `_PAGE` narrows in SQL so the walk does not drag 1,272,367 rows
across the wire; `is_tier_movie` is applied again to every row the page
reader hands back, and it is what decides whether a job is written. The
asymmetry is the point: a SQL predicate that is accidentally *too wide* — the
one risk above, and the one that costs 30,983 parked rows — is caught by the
Python guard and costs nothing, while a SQL predicate that is too narrow
costs coverage the next run picks up. The two must agree, so keep them in
view of each other; `tests/unit/test_scripts_enqueue_tier_enrichment.py`
pins the Python half and nothing pins the SQL half but this sentence.

**The bound is in the iterator.** `--limit` is subtracted from the size of
the *next page asked for*, so the walk stops reading, not merely stops
writing. `CLAUDE.md`'s rule ("the bound has to be in the iterator, not in
`max_pages`") is about a live run against a real server, and it applies to a
walk over 130,806 rows for the same reason. `max_pages` does not exist here
anyway — it is an Emby adapter concept (`adapters/emby/adapter.py:251`) and
exhausting it raises `PortDataMalformed`.

**The cursor advances on the last id of the page, unconditionally**, before
the predicate is consulted. A cursor advanced on the last *enqueued* id
cannot get past a page nothing in it clears, which on this catalog is the
ordinary page rather than the exotic one.

**Re-running is cheap and that is what makes the full run resumable.**
`enqueue` deduplicates on `(kind, key)` and promotes rather than duplicates,
so a second pass over the same prefix writes nothing new; and a title already
enriched inside `USHER_ENRICH_CACHE_MAX_AGE_DAYS` (default 30) re-reads its
`raw_payloads` row and spends **no TMDb request** when the job runs — measured
2026-08-11, twenty already-cached titles re-enqueued by id and drained for
zero outbound requests.

⚠️ **The predicate is not stationary: enriching a title can remove it from the
tier, so a second run of this script does not select the same rows.**
`tmdb_vote_count` is in `EnrichService._ENRICHABLE`; the bulk loader writes
IMDb `numVotes` into that column through `apply_ratings` and enrichment
overwrites it with TMDb's own `vote_count`, which is a different electorate.
**That dual write is what ADR-0040 is about**, and `m10a` renamed `vote_count`
to `tmdb_vote_count`, which made an IMDb writer landing there legible without
stopping it.

🔴 **ADR-0040's Task 2 then stopped it, and in doing so emptied this script's
predicate on any catalog that has not been enriched yet. Read this before
running it on a new deployment (2026-08-19).** `apply_ratings` now writes
`imdb_num_votes`/`imdb_average_rating`, and nothing else fills
`tmdb_vote_count`: `upsert_titles` omits it from its `DO UPDATE` list and
`link_crosswalk` writes only `tmdb_popularity`. So after a fresh
`usher bootstrap --phase all`, `tmdb_vote_count` is **NULL on every row**, the
tier is `kind = 'movie' AND NULL >= 100 AND ...` — **zero rows** — and the
enrichment crawl this script exists to start cannot start itself. On a catalog
bootstrapped before `m10a` the column still holds its pre-existing mixed values
(540,275 rows on the deployed one, measured 2026-08-19), so the tier still
selects there; the break is a fresh-install one, which is the direction hardest
to notice.

**Not repaired in the commit that found it, deliberately.** The tier this script
has always *meant* is "movies a lot of people on IMDb have voted on", which is
`imdb_num_votes >= 100` — the same restoration ADR-0040 made for the eval
sampling frame, one predicate over. But that is a behaviour change to what a
crawl fetches, it moves the population every downstream tier statistic in
`.claude/rules/tmdb-and-enrichment.md` is quoted against, and it deserves the
failing test and the re-measurement the frame re-anchor got rather than a
docstring edit. It is owed, it is named here and in ADR-0040's Consequences, and
until it lands **an operator bootstrapping a new catalog must not expect this
script to enqueue anything.**

Measured 2026-08-11 over 537
enriched tier movies: **80 still carry `>= 100` (14.9%)**. ⚠️ The scale gap this
paragraph used to quote beside that — *"median TMDb count 16 against a median
IMDb 581"* — was two populations, the 16 over the 537 enriched here and the 581
over the tier that was *not* enriched; the paired figure is ~38x (median TMDb 15
against median frozen IMDb 576 over the same 130,647 rows, M9 S3).
**The keyset cursor is what makes that safe** —
a row can leave the tier only by being enriched, which happens only after the
cursor passed it, so nothing is skipped and the walk still terminates. An
`OFFSET` walk over the same shrinking population would skip rows silently.

**What a full run costs, measured rather than estimated** (2026-08-11, a
systematic 1-in-261 sample of 500 titles, 0.38% of the tier, drained through
one `usher work`): **130,806 fetches, ~3.5 h of wall clock at one worker**
(10.38 rps achieved against a bucket set to 30 — the bucket cannot bind on a
sequential worker), **~1.0 GiB into `raw_payloads`**, and **two follow-up jobs
per enriched title**, the `INDEX` half of which nothing claims unless
`USHER_EMBEDDING_ENABLED` is on. Evidence in
`.claude/rules/tmdb-and-enrichment.md`.
"""

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

# Keyset, not `OFFSET`: `MediaItemRepository.list_unmatched`'s offset walk is
# measured at 43.7 ms at offset 0 and 388.9 ms at offset 1,126,574 -- linear
# per page, quadratic to drain -- which is the exact shape of walk this is.
#
# `:after IS NULL OR id > :after` rather than two statements, because the
# first page and every later page differ by one predicate and a second
# constant is a second thing to get wrong.
# `:min_votes` is a bound parameter and not an f-string hole. It is the one
# number that appears on both sides of the two-spellings split above, so
# interpolating it would be the cheapest possible drift *and* the thing ruff
# `S608` refuses -- a rule this repository keeps in `[tool.ruff.lint] select`
# and does not `noqa` away for a statement it could parameterise instead.
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
