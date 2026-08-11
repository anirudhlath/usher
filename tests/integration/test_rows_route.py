"""`POST /admin/rows/regenerate` through a real request against a real queue.

**What only this level can see.** `tests/unit/test_api_rows.py` drives the
route over `FakeJobQueue`, whose seventh documented divergence is that it
counts a no-op re-enqueue as a row written -- so *every* statement about what
a repeat costs is untestable there, and every statement in the route's own
docstring about what a 202 does and does not promise is one of those. Three
things are only true here:

1. **The write is committed.** `get_session` is the request's commit boundary;
   a handler that enqueued and never committed passes every unit case (a fake
   queue is a dict). The row has to still be there afterwards, from another
   connection.
2. **The real `_ENQUEUE` predicate runs.** `WHERE jobs.status <> 'parked' AND
   jobs.priority < excluded.priority` is what makes a repeat free, and
   `updated_at = clock_timestamp()` sits *inside* that `DO UPDATE`, so an
   unchanged `updated_at` is a direct observation of "zero rows written" from
   a route that discards `enqueue`'s return value.
3. **The un-overridden dependency graph resolves**, so the key really is the
   stored household's id rather than a fresh `User.id` a constructor default
   minted -- M7's headline failure arriving one route over.

**This module commits for real, so it cleans up after itself**, and its
footprint is deliberately one predicate wide: `DELETE FROM jobs WHERE kind =
'curate'`. Nothing else in the suite writes that kind, and the alternative a
sibling file uses (`DELETE FROM jobs` plus the default `users` row) would
cascade into `watch_states` another committing file may have left. The `users`
row this route's `DefaultUserIdDep` creates is left standing: it is a
singleton reached by `ON CONFLICT (name) DO NOTHING`, every file that needs it
creates it the same way, and the two files that assert about it delete it
themselves afterwards.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.api.app import create_app
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.users import DEFAULT_USER_NAME
from usher.domain.jobs import JobKind

ROUTE = "/admin/rows/regenerate"
SECRET_KEY = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        # Both lanes off. `dependency_overrides` do not reach the lifespan, so
        # a worker lane here would claim the very `curate` job these cases
        # assert on -- and with `llm_enabled` at its shipped default it would
        # not even register a handler for it, which is a second reason to be
        # explicit rather than lucky.
        push_enabled=False,
        worker_enabled=False,
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the suite's rolled-back one.

    The route commits from its own session in its own transaction, so reading
    back through the suite's shared transaction would be asking a connection
    that cannot see it.
    """
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        await session.execute(text("DELETE FROM jobs WHERE kind = :kind"), {"kind": "curate"})
        await session.commit()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await _wipe(sessions)
    yield
    await _wipe(sessions)


@pytest_asyncio.fixture
async def client(settings: Settings, clean: None) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def _curate_rows(sessions: async_sessionmaker[AsyncSession]) -> list[Row[tuple[object, ...]]]:
    async with sessions() as session:
        return list(
            (
                await session.execute(
                    text(
                        "SELECT key, priority, status, attempts, last_error, traceparent, "
                        "updated_at FROM jobs WHERE kind = 'curate' ORDER BY key"
                    )
                )
            ).all()
        )


async def _stored_household(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessions() as session:
        stored = (
            await session.execute(
                text("SELECT id FROM users WHERE name = :name"), {"name": DEFAULT_USER_NAME}
            )
        ).scalar_one()
    return uuid.UUID(str(stored))


def _queue(session: AsyncSession) -> PostgresJobQueue:
    """The **real** queue, for the two cases that have to put the row into a
    state only a worker reaches.

    Neither `claim` nor `fail` is reachable through any route, and driving
    them through `FakeJobQueue` would put the row in the fake's dict rather
    than in the table the next request writes to.
    """
    return PostgresJobQueue(session, max_attempts=5, backoff_seconds=0.01)


async def test_a_regeneration_commits_a_job_for_the_stored_household(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The route's whole contract, against the queue it actually writes to.

    Read back on a **different connection**, which is the assertion a fake
    cannot make: a handler that enqueued and never committed leaves a green
    unit file and an empty `jobs` table.

    The key is compared against the `users` row rather than against the
    response's own value, so the two cannot agree by construction. `User.id`
    is `default_factory=new_id`, so a wiring that built a `User` instead of
    reading one would produce a syntactically perfect 202 naming a household
    that has never existed -- `api/deps.py::get_default_user` records that as
    this milestone's headline failure arriving through a constructor default,
    and this is the same failure one route along.
    """
    response = await client.post(ROUTE)
    household = await _stored_household(sessions)

    assert response.status_code == 202
    assert response.json() == {"kind": "curate", "key": str(household)}
    rows = await _curate_rows(sessions)
    assert [(str(row.key), row.priority, row.status, row.attempts) for row in rows] == [
        (str(household), 100, "pending", 0)
    ]
    assert rows[0].traceparent is not None, "the worker has no request to link back to"


async def test_asking_twice_writes_nothing_the_second_time(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """PRD 06's *"one modest completion per user per day"*, measured rather
    than asserted about a count the route never sees.

    `updated_at = clock_timestamp()` lives inside `_ENQUEUE`'s `DO UPDATE`,
    which is gated on `jobs.priority < excluded.priority` -- so a repeat at the
    same rung takes no branch that could move it. An unchanged `updated_at` is
    therefore the row saying zero rows were written, which is the number
    `FakeJobQueue` gets wrong (it answers 1) and the reason this case cannot
    live in the unit file.

    The 202 is unconditional on all of that, which is the other half: an
    operator pressing the button twice has not made a mistake, and `enqueue`
    cannot tell this request from the first anyway.

    **`traceparent` is asserted unchanged for the same reason, and it is the
    consequence this route's docstring had to grow a fourth bullet for.**
    `traceparent = COALESCE(excluded.traceparent, jobs.traceparent)` sits
    inside that same `DO UPDATE`, and `_ENQUEUE`'s own comment names the one
    escape from it -- *"a demand promotion (M5) raises the priority and
    therefore does write"*. This route always enqueues at `DEMAND`, the top of
    the scale, so that escape is unreachable here and no repeat can ever
    repoint the link: the worker's span links back to whichever press created
    the row, not to the one an operator just made. That is not a defect --
    the run that happens *is* the first press's -- but it is a property the
    `updated_at` assertion above already forces and nothing stated, which is
    the shape of thing that gets rediscovered as a surprise.
    """
    first = await client.post(ROUTE)
    before = await _curate_rows(sessions)

    second = await client.post(ROUTE)

    after = await _curate_rows(sessions)
    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json() == second.json()
    assert len(after) == 1, "two requests for one household are one row"
    assert after[0].updated_at == before[0].updated_at, (
        "the repeat rewrote the row, so `WHERE jobs.priority < excluded.priority` is not holding"
    )
    assert before[0].traceparent is not None, "the first press left no link to repoint"
    assert after[0].traceparent == before[0].traceparent, (
        "the repeat repointed the trace link, which a request at `DEMAND` cannot do"
    )


async def test_a_repeat_while_the_generation_runs_is_accepted_and_then_discarded(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """**The sharpest limit on what this 202 means**, and the one the route's
    docstring rests on.

    `status = 'running'` appears nowhere in `_ENQUEUE`'s `WHERE`, so a repeat
    arriving mid-generation is coalesced into the run already in flight -- and
    `complete()` then deletes that row, so the *requested* generation never
    happens and the caller was told 202. Measured here at `DEMAND` against
    `DEMAND`, which is the only pair this route can produce: 0 rows written,
    the row left `('running', 100)`, and nothing at all afterwards.

    That is the wanted answer for a cost rule and it is a genuine limit, so it
    is pinned at the route rather than only in `tests/integration/
    test_job_queue.py`: a client needing a generation *newer than* one in
    flight has to arrange that above the queue, because no return value here
    distinguishes the two.
    """
    await client.post(ROUTE)
    async with sessions() as session:
        claimed = await _queue(session).claim([JobKind.CURATE], limit=1)
        await session.commit()
    assert [job.status.value for job in claimed] == ["running"]

    response = await client.post(ROUTE)

    assert response.status_code == 202
    running = await _curate_rows(sessions)
    assert [(row.status, row.priority) for row in running] == [("running", 100)]

    async with sessions() as session:
        await _queue(session).complete(claimed[0].id)
        await session.commit()
    assert await _curate_rows(sessions) == [], "the repeat went with the run it was folded into"


async def test_a_parked_generation_is_accepted_and_left_exactly_as_it_was(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """PRD 08: *"Re-enqueueing does not un-park... and a parked job's priority
    is not promoted behind their back either."*

    A household whose candidate pool cannot be served parks
    (`CurationService.generate` raises `PortDataMalformed` for an empty pool,
    which `JobWorker` parks immediately), and asking again releases nothing --
    `_ENQUEUE`'s `WHERE jobs.status <> 'parked'` is absolute, and at `DEMAND`
    it is the *only* clause doing the work, since the priority half would let
    this write through if the row had parked at a lower rung.

    So this is the shape of "accepted" that delivers nothing until an operator
    intervenes, and the whole row is compared before and after rather than just
    the status: `updated_at` is what says no branch was taken at all, and
    `last_error` is what an operator is actually reading. A route that "helped"
    by clearing the error, or by re-enqueueing at a rung above the parked one,
    fails on one of the two.
    """
    await client.post(ROUTE)
    async with sessions() as session:
        queue = _queue(session)
        [claimed] = await queue.claim([JobKind.CURATE], limit=1)
        await queue.fail(claimed.id, error="no candidate survived the pool", retryable=False)
        await session.commit()
    before = await _curate_rows(sessions)
    assert [(row.status, row.priority) for row in before] == [("parked", 100)]

    response = await client.post(ROUTE)

    assert response.status_code == 202
    assert await _curate_rows(sessions) == before, "asking again moved a parked row"
    assert before[0].last_error == "no candidate survived the pool"
