"""`SearchQueryRetention` against the real table."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.composition import search_query_scope
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.search_query import _PRUNE, PostgresSearchQueryRepository
from usher.domain.ids import new_id
from usher.ports.repository import SearchQueryRecord, SearchQueryRepository
from usher.ports.search import SearchMode
from usher.services.scheduler import RETENTION_PERIOD, SearchQueryRetention, SearchQueryScope

#: The instant every case reasons from. Not `now()` -- a case whose boundary
#: moves while it runs is a case that changes its answer at midnight, which is
#: the whole reason `prune` takes a value rather than an interval.
NOW = datetime(2026, 8, 27, 18, 30, 43, tzinfo=UTC)
WINDOW = timedelta(days=90)


def _clock() -> datetime:
    return NOW


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = new_id()
    await session.execute(
        text("INSERT INTO users (id, name) VALUES (CAST(:id AS uuid), :name)"),
        {"id": user_id, "name": f"viewer-{user_id}"},
    )
    return user_id


async def _seed_title(session: AsyncSession) -> uuid.UUID:
    title_id = new_id()
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name) "
            "VALUES (CAST(:id AS uuid), 'movie', 'An Invented Title', 'An Invented Title')"
        ),
        {"id": title_id},
    )
    return title_id


def _record(*, at: datetime, user_id: uuid.UUID) -> SearchQueryRecord:
    return SearchQueryRecord(
        id=new_id(),
        at=at,
        user_id=user_id,
        query="the quiet vacuum",
        mode=SearchMode.FULL_TEXT,
        result_count=1,
        latency_ms=1,
    )


def _scope_over(repository: SearchQueryRepository) -> SearchQueryScope:
    """A scope yielding a repository bound to this test's transaction, without committing.

    This suite's isolation is a rolled-back transaction, so a scope that committed
    would leak rows into the session-scoped container and take down whichever file
    ran next -- the shape `.claude/rules/fixtures-and-fakes.md` records for
    route-driven tests. The commit itself is the last case's subject and uses the
    real scope.
    """

    @asynccontextmanager
    async def open() -> AsyncIterator[SearchQueryRepository]:
        yield repository

    return open


@pytest.fixture
def repository(session: AsyncSession) -> PostgresSearchQueryRepository:
    return PostgresSearchQueryRepository(session)


@pytest.fixture
async def user_id(session: AsyncSession) -> uuid.UUID:
    return await _seed_user(session)


def _job(
    repository: SearchQueryRepository, *, batch: int = 10_000, window: timedelta = WINDOW
) -> SearchQueryRetention:
    return SearchQueryRetention(
        _scope_over(repository), window=window, batch=batch, period=RETENTION_PERIOD, now=_clock
    )


# : A ceiling on any `SearchQueryRetention.run()` this file drives.
DRAIN_DEADLINE = 5.0


async def _drain(job: SearchQueryRetention) -> None:
    """`job.run()`, bounded.

    See `DRAIN_DEADLINE`.
    """
    await asyncio.wait_for(job.run(), DRAIN_DEADLINE)


async def _count(session: AsyncSession) -> int:
    found = await session.execute(text("SELECT count(*) FROM search_queries"))
    return int(found.scalar_one())


async def test_the_retention_job_deletes_only_rows_past_the_cutoff(
    session: AsyncSession, repository: PostgresSearchQueryRepository, user_id: uuid.UUID
) -> None:
    """Four rows at 91, 90, 89 and 0 days, and only the 91-day one may go.

    - **The 89-day row is the control.** A job with an off-by-one on the interval,
      and one that simply deleted everything, both pass a case that only checks the
      old row disappeared.
    - **The row at *exactly* 90 days is the second control**, because `<` and `<=`
      are one character and both read as correct. The statement is
      `at < now() - interval '90 days'`, so a row answered at exactly the cutoff is
      inside the window and stays.
    """
    ages = {
        days: _record(at=NOW - timedelta(days=days), user_id=user_id) for days in (91, 90, 89, 0)
    }
    for record in ages.values():
        await repository.record(record)
    assert await _count(session) == 4

    await _drain(_job(repository))

    survived = await session.execute(text("SELECT id FROM search_queries"))
    assert {row[0] for row in survived} == {ages[90].id, ages[89].id, ages[0].id}


async def test_the_oldest_row_comes_back_with_a_timezone(
    repository: PostgresSearchQueryRepository, user_id: uuid.UUID
) -> None:
    """A naive `last_done()` is a `TypeError` at the tick, not a wrong number.

    `Scheduler._due_now` subtracts the reading from `datetime.now(UTC)`, and
    `search_queries.at` is `TIMESTAMP WITH TIME ZONE`, so asyncpg hands back an aware
    value -- a fact about *this column* that a case asserting `tzinfo is not None` on
    a hand-built datetime cannot establish.

    Both the port method and the job's own reading are checked, because the job adds
    a `window` to what the port answered and `aware + timedelta` is aware while
    nothing would have caught a naive value passing straight through.
    """
    await repository.record(_record(at=NOW - timedelta(days=200), user_id=user_id))

    oldest = await repository.oldest()
    assert oldest is not None
    assert oldest.tzinfo is not None, "min(at) came back naive; the due comparison would raise"
    assert oldest == NOW - timedelta(days=200)

    reading = await _job(repository).last_done()
    assert reading is not None and reading.tzinfo is not None
    assert reading == NOW - timedelta(days=110), "min(at) + 90 days, and it is inside `now`"


async def test_an_empty_table_reads_as_satisfied_now_rather_than_never_built(
    repository: PostgresSearchQueryRepository,
) -> None:
    """`min()` over an empty table is one row holding `NULL`, not no row.

    A `scalar_one_or_none()` here would raise rather than answer `None`, and this is
    the arm that tells them apart against the real driver. The reading is `now`,
    which is what stops an idle deployment pruning nothing on every tick forever.
    """
    assert await repository.oldest() is None
    assert await _job(repository).last_done() == NOW


async def test_a_live_shaped_population_wholly_inside_the_window_is_not_due_and_deletes_nothing(
    session: AsyncSession, repository: PostgresSearchQueryRepository, user_id: uuid.UUID
) -> None:
    """A populated table wholly inside the window is not due and deletes nothing.

    What matters is the *shape* -- a populated table, every row inside the window --
    never the cardinality, so the size is named once here and the assertions are
    computed from what was stored.

    **The rows are inserted newest-first on purpose.** Ids are UUIDv7 and therefore
    monotonic in insertion order, so seeding oldest-first would put `min(at)` on the
    lowest id and let `ORDER BY id LIMIT 1` pass as `min(at)` by accident. Seeded
    this way the oldest row carries the *highest* id.
    """
    rows = 109
    oldest_age = timedelta(days=25)
    # Ascending age, so the first row written is the newest and the last is
    # the oldest -- which is what puts `min(at)` on the highest id.
    ages = [oldest_age * index / (rows - 1) for index in range(rows)]
    for age in ages:
        await repository.record(_record(at=NOW - age, user_id=user_id))
    await session.flush()

    # Premises, read back through the port rather than taken from the
    # literals above: the population is the size claimed, and its oldest row
    # is inside the window -- without which "not due" is vacuous.
    assert await _count(session) == rows
    oldest = await repository.oldest()
    assert oldest is not None
    assert NOW - oldest == oldest_age
    assert NOW - oldest < WINDOW
    # The seeding order itself is a premise, so it is asserted rather than
    # left in the docstring: the oldest row must carry the *highest* id, or
    # `ORDER BY id LIMIT 1` and `min(at)` agree and this fixture has no teeth
    # against a reader that confuses them.
    by_age = await session.execute(text("SELECT id FROM search_queries ORDER BY at LIMIT 1"))
    by_id = await session.execute(text("SELECT id FROM search_queries ORDER BY id LIMIT 1"))
    assert by_age.scalar_one() != by_id.scalar_one()

    job = _job(repository)
    # Not due: `min(min(at) + window, now)` caps at `now` for a table already
    # satisfying the rule, so the age the scheduler subtracts is zero.
    assert await job.last_done() == NOW

    await _drain(job)
    assert await _count(session) == rows


async def test_the_prune_takes_no_household_and_no_title_with_it(
    session: AsyncSession, repository: PostgresSearchQueryRepository, user_id: uuid.UUID
) -> None:
    """`search_queries` is a leaf, asserted against the real foreign keys.

    Both point *outward* -- `fk_search_queries_user_id_users` is `ON DELETE
    RESTRICT`, `fk_search_queries_clicked_title_id_titles` is `ON DELETE SET
    NULL` -- and nothing references these rows. The premise is that the row
    being deleted really did name both: a click is attributed to a real title
    first, so `clicked_title_id` is non-`NULL` when the row goes.

    The delete rules are read off `pg_constraint` rather than transcribed,
    because the claim *"the delete cannot cascade"* rests on them and a
    migration could change one without any case here noticing.
    """
    rules = await session.execute(
        text(
            "SELECT conname, confdeltype FROM pg_constraint "
            "WHERE conrelid = 'search_queries'::regclass AND contype = 'f'"
        )
    )
    # `confdeltype` is `"char"`, which asyncpg hands back as `bytes` rather
    # than `str` -- decoded here rather than compared against a `b"r"` literal,
    # because the letters are what PostgreSQL's own documentation names.
    assert {name: rule.decode() for name, rule in rules} == {
        "fk_search_queries_user_id_users": "r",
        "fk_search_queries_clicked_title_id_titles": "n",
    }
    title_id = await _seed_title(session)
    record = _record(at=NOW - timedelta(days=365), user_id=user_id)
    await repository.record(record)
    await repository.record_outcome(
        record.id, user_id=user_id, clicked_title_id=title_id, played=True
    )
    users_before = (await session.execute(text("SELECT count(*) FROM users"))).scalar_one()
    titles_before = (await session.execute(text("SELECT count(*) FROM titles"))).scalar_one()

    await _drain(_job(repository))

    assert await _count(session) == 0
    assert (await session.execute(text("SELECT count(*) FROM users"))).scalar_one() == users_before
    assert (
        await session.execute(text("SELECT count(*) FROM titles"))
    ).scalar_one() == titles_before


async def test_the_chunked_delete_walks_the_index_oldest_first(
    session: AsyncSession, repository: PostgresSearchQueryRepository, user_id: uuid.UUID
) -> None:
    """Two claims one statement makes and nothing else can see."""
    # Deliberately not age order. The two oldest expired rows are written
    # third and fourth, so the first two a heap walk reaches are the two
    # *youngest* expired ones -- and `new_id()` is UUIDv7, so allocating the
    # records in this same order puts `id` order on the seeding order too.
    ages = {
        days: _record(at=NOW - timedelta(days=days), user_id=user_id)
        for days in (150, 100, 400, 300, 200, 10)
    }
    for record in ages.values():
        await repository.record(record)

    expired = {"before": NOW - WINDOW}
    # Two premises, asserted rather than described, because each is a free ride an
    # unordered statement could take: the physical order a sequential scan hands back
    # (`ctid`) and the key order a UUIDv7 makes monotonic (`id`) must both disagree with
    # age order.
    free_rides = (
        "SELECT id FROM search_queries WHERE at < CAST(:before AS timestamptz) "
        "ORDER BY ctid LIMIT 2",
        "SELECT id FROM search_queries WHERE at < CAST(:before AS timestamptz) ORDER BY id LIMIT 2",
    )
    for statement in free_rides:
        taken = await session.execute(text(statement), expired)
        assert {row[0] for row in taken} == {ages[150].id, ages[100].id}, (
            f"{statement} now names the same rows `ORDER BY at` does -- the fixture has been "
            "reseeded into age order and dropping the inner ORDER BY stops being visible here"
        )

    assert await repository.prune(before=NOW - WINDOW, limit=2) == 2

    survived = await session.execute(text("SELECT id FROM search_queries"))
    assert {row[0] for row in survived} == {ages[200].id, ages[150].id, ages[100].id, ages[10].id}

    await session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = await session.execute(
        text("EXPLAIN " + _PRUNE.text).bindparams(
            bindparam("before", type_=DateTime(timezone=True))
        ),
        {**expired, "limit": 2},
    )
    assert "ix_search_queries_at" in "\n".join(row[0] for row in plan)


# The only case in this directory that leaves rows *committed* on a second engine, which
# is the whole point of it -- and long enough for autovacuum's autoanalyze to read them.
@pytest.mark.leaks_statistics("search_queries", "users")
async def test_the_prune_commits_each_chunk_where_a_composition_root_wired_it(
    postgres_url: str, session: AsyncSession
) -> None:
    """The one claim a rolled-back suite cannot make, so this case owns its own engine."""
    engine = build_engine(postgres_url)
    sessions = build_session_factory(engine)
    real = search_query_scope(sessions)
    seen_midway: list[int] = []

    @asynccontextmanager
    async def watching() -> AsyncIterator[SearchQueryRepository]:
        async with real() as queries:
            yield queries
        # After the real scope's `__aexit__`, i.e. after its commit, and on a
        # connection that never saw the writing session.
        async with sessions() as observer:
            found = await observer.execute(text("SELECT count(*) FROM search_queries"))
            seen_midway.append(int(found.scalar_one()))

    try:
        async with sessions() as writer:
            user_id = await _seed_user(writer)
            repository = PostgresSearchQueryRepository(writer)
            for days in (400, 350, 300, 250, 200, 150, 10):
                await repository.record(_record(at=NOW - timedelta(days=days), user_id=user_id))
            await writer.commit()

        job = SearchQueryRetention(
            watching, window=WINDOW, batch=2, period=RETENTION_PERIOD, now=_clock
        )
        await _drain(job)

        assert seen_midway == [5, 3, 1, 1], (
            "seven rows, six expired, a batch of two: each chunk's deletion has to be "
            "committed before the next one opens, and the fourth chunk is the empty "
            "terminator"
        )
        async with sessions() as observer:
            found = await observer.execute(text("SELECT count(*) FROM search_queries"))
            assert int(found.scalar_one()) == 1
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(text("DELETE FROM search_queries"))
            await cleanup.execute(text("DELETE FROM users"))
            await cleanup.commit()
        await engine.dispose()
