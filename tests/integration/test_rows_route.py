"""`/admin/rows/*` through real requests against real Postgres:
`POST .../regenerate` (M8) and `GET`/`PUT .../providers` (M9).

**The provider half is here because the claim it has to make is not about a
response.** M7's boundary call 9 refused `row_provider_settings` on the ground
that a toggle nothing reads is worse than no toggle, so what this file proves
for those two routes is that **disabling a provider removes its shelf from the
next `GET /home`** -- across a session boundary, across a process that never
saw the request, and without the ~30 s screen cache hiding either. None of that
is expressible against a fake: `FakeRowProviderSettingsRepository` is a dict
with no transaction, and a unit app's `GET /home` composes over a `Library`
rather than over the table the write went to.

## `POST /admin/rows/regenerate`

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
footprint is deliberately narrow: `DELETE FROM jobs WHERE kind = 'curate'` and
`DELETE FROM row_provider_settings` (which ships empty, so emptying it *is* the
shipped state), plus the two titles and one source the `screen` fixture plants
and deletes by id -- and, since issue #73, the `enrich` jobs the `GET /home`
reads below promote for those same two titles. Nothing else in the suite writes
that job kind or that
table, and the alternative a sibling file uses (`DELETE FROM jobs` plus the
default `users` row) would cascade into `watch_states` another committing file
may have left. The `users` row `DefaultUserIdDep` creates is left standing: it
is a singleton reached by `ON CONFLICT (name) DO NOTHING`, every file that
needs it creates it the same way, and the two files that assert about it delete
it themselves afterwards.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

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
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.row_provider_settings import (
    PostgresRowProviderSettingsRepository,
)
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.db.users import DEFAULT_USER_NAME, ensure_default_user
from usher.domain.enums import SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert, WatchStateWrite
from usher.services.rows import ROW_PROVIDERS

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
    """Two predicates now, and the second is as narrow as the first.

    `row_provider_settings` is the table E2's routes write, it ships **empty**
    and it is never seeded (M1's `m09a`, and PRD 09 item 9's *"deliberately not
    seeded with ten slugs"*), so "no rows at all" is its shipped state and
    `DELETE FROM` it restores exactly that. Emptying it is also what makes the
    provider cases' premise -- *every provider answers `enabled` on a virgin
    database* -- a fact rather than a hope about test ordering.
    """
    async with sessions() as session:
        await session.execute(text("DELETE FROM jobs WHERE kind = :kind"), {"kind": "curate"})
        await session.execute(text("DELETE FROM row_provider_settings"))
        await session.commit()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    await _wipe(sessions)
    yield
    await _wipe(sessions)


@pytest.fixture
def app(settings: Settings, clean: None) -> FastAPI:
    """The app itself, so a case can reach `app.state.row_cache`.

    Split out of `client` for exactly one case -- the cross-process one below,
    which has to stand in for `_SCREEN_TTL` elapsing without sleeping 30 s.
    """
    return create_app(settings)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
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


# ---------------------------------------------------------------------------
# `GET`/`PUT /admin/rows/providers` (E2) -- the toggle, and the screen it has
# to reach.
#
# **The routes are the easy part.** M7's boundary call 9 refused
# `row_provider_settings` on the ground that *"a table with ten rows all
# reading `enabled = true` is indistinguishable from no table, right up until
# an operator finds it and expects toggling it to do something"*, so a route
# that writes a row nothing reads discharges the refusal in form and not in
# substance. What only this level can see is the substance: the filter reads
# the table on the **next request's** session, and the ~30 s screen cache does
# not hide the change.
# ---------------------------------------------------------------------------

PROVIDERS = "/admin/rows/providers"


@pytest_asyncio.fixture
async def household(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """The singleton default user, created before any request runs.

    Created here rather than left to `get_default_user` because the watch state
    below has to be keyed to the household the route will resolve -- a fixture
    that minted its own `User.id` would seed a Continue Watching shelf for a
    household `GET /home` never asks about, and the positive control would fail
    for a reason that has nothing to do with this feature.
    """
    async with sessions() as session:
        user_id = await ensure_default_user(session)
        await session.commit()
    return user_id


@dataclass(frozen=True, slots=True)
class _Screen:
    """The two titles the fixture below plants, so a case can name them."""

    resuming: uuid.UUID
    arrived: uuid.UUID


@pytest_asyncio.fixture
async def screen(
    sessions: async_sessionmaker[AsyncSession], household: uuid.UUID
) -> AsyncIterator[_Screen]:
    """A household with a genuinely non-empty `continue-watching` shelf, and a
    second shelf beside it.

    **Two titles, because one cannot tell "the toggle worked" from "the screen
    went empty".** `resuming` is owned and part-way through, which is
    `list_in_progress`' whole predicate (`NOT played AND position_seconds >
    0`); `arrived` is owned and freshly added, which is what
    `RecentlyAddedProvider` fires on. The second is the control that survives
    the toggle.

    Committed for real and deleted by id afterwards. **`watch_states` is
    deleted explicitly and `media_items` is not**, which is measured rather
    than assumed: this fixture's first run died in teardown on
    `fk_watch_states_title_id_titles`, so that FK is `NO ACTION` while
    `media_items`' cascades. The `sources` row goes last for the same reason.
    The `users` row is the singleton every file in this suite reaches by
    `ON CONFLICT (name) DO NOTHING` and is left standing.
    """
    source = Source(
        kind=SourceKind.EMBY,
        name=f"providers-probe-{new_id()}",
        base_url="https://providers.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    resuming = Title(
        id=new_id(), kind=TitleKind.MOVIE, name="Halfway Through", sort_name="halfway through"
    )
    arrived = Title(id=new_id(), kind=TitleKind.MOVIE, name="Just Landed", sort_name="just landed")
    now = datetime.now(UTC)
    async with sessions() as session:
        await PostgresSourceRepository(session).add(source)
        titles = PostgresTitleRepository(session)
        await titles.add(resuming)
        await titles.add(arrived)
        await PostgresMediaItemRepository(session).upsert_many(
            [
                MediaItemUpsert(
                    source_id=source.id,
                    external_id=f"providers-probe-{title.id}",
                    title_id=title.id,
                    episode_id=None,
                    container="mkv",
                    video_codec=None,
                    audio_codec=None,
                    width=None,
                    height=None,
                    hdr_format=None,
                    audio_channels=None,
                    file_size_bytes=None,
                    runtime_seconds=7_200,
                    added_at=now,
                    last_seen_at=now,
                )
                for title in (resuming, arrived)
            ]
        )
        await PostgresWatchStateRepository(session).set_from_client(
            WatchStateWrite(
                user_id=household,
                title_id=resuming.id,
                episode_id=None,
                position_seconds=1_800,
                played=False,
            )
        )
        await session.commit()
    try:
        yield _Screen(resuming=resuming.id, arrived=arrived.id)
    finally:
        async with sessions() as session:
            for title_id in (resuming.id, arrived.id):
                await session.execute(
                    text("DELETE FROM watch_states WHERE title_id = :id"), {"id": title_id}
                )
                # `GET /home` promotes every skeleton it draws (issue #73) and
                # `get_session` commits at the end of a successful request, so
                # the `_slugs` reads above leave an `enrich` row per title.
                # **Before the title** -- the job's `key` is the title's id as
                # text, so once the title row is gone nothing identifies it.
                await session.execute(
                    text(
                        "DELETE FROM jobs WHERE kind = 'enrich' AND key IN "
                        "(SELECT id::text FROM titles WHERE id = :id)"
                    ),
                    {"id": title_id},
                )
                await session.execute(text("DELETE FROM titles WHERE id = :id"), {"id": title_id})
            await session.execute(text("DELETE FROM sources WHERE id = :id"), {"id": source.id})
            await session.commit()


async def _slugs(client: AsyncClient) -> list[str]:
    response = await client.get("/home")
    assert response.status_code == 200, response.text
    return [row["slug"] for row in response.json()["rows"]]


async def _stored_overrides(sessions: async_sessionmaker[AsyncSession]) -> dict[str, bool]:
    async with sessions() as session:
        rows = (
            await session.execute(text("SELECT slug_prefix, enabled FROM row_provider_settings"))
        ).all()
    return {row.slug_prefix: row.enabled for row in rows}


async def test_a_disabled_provider_stops_appearing_on_the_home_screen(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession], screen: _Screen
) -> None:
    """**The centre of this task**, and the reason M7 refused the table at all:
    a toggle nothing reads is worse than no toggle.

    Three reds, in order, and each names a different wrong implementation:

    1. **No route.** `PUT` answers 404 from the router itself.
    2. **An unfiltered provider list.** `get_home_service` returns
       `HomeService(cache=cache)` and `HomeService`'s own default is
       `ROW_PROVIDERS`, so the write lands, the read never happens, and the
       shelf is still there. This is the state the whole task exists to leave
       behind.
    3. **A stale screen.** With the `RowCache.clear()` deleted, the second
       `GET /home` answers out of the ~30 s screen the first one cached and the
       shelf survives for half a minute -- which is the shape of "it works when
       I try it by hand and not in the test", and vice versa.

    **The first assertion is the positive control and it is not decoration.**
    An absent `continue-watching` is also what an empty household produces, so
    without it every later assertion is satisfied by a fixture that seeded
    nothing -- a false green this repository has shipped before
    (`ContinueWatchingProvider`'s fourth named wrong implementation is its
    sibling). `recently-added` is the second control, in the other direction:
    it says the screen still composes, so "the slug is gone" is a statement
    about one provider rather than about the composer having stopped.
    """
    before = await _slugs(client)
    assert "continue-watching" in before, (
        "the fixture's in-progress title produced no shelf, so nothing below can fail"
    )
    assert "recently-added" in before, "the fixture's fresh arrival produced no shelf"

    toggled = await client.put(f"{PROVIDERS}/continue-watching", json={"enabled": False})

    assert toggled.status_code == 200, toggled.text
    assert toggled.json() == {"slug": "continue-watching", "enabled": False}
    after = await _slugs(client)
    assert "continue-watching" not in after, (
        "the provider is disabled in the table and still composing a shelf"
    )
    assert "recently-added" in after, "the toggle took the rest of the screen with it"


async def test_a_toggle_committed_by_another_process_reaches_the_next_screen(
    client: AsyncClient,
    app: FastAPI,
    sessions: async_sessionmaker[AsyncSession],
    screen: _Screen,
) -> None:
    """**The filter reads the table, not something the `PUT` left in memory --
    and the ~30 s window in between is asserted rather than hidden.**

    The headline case writes and reads through one process, so it is satisfied
    by a route that stashed the disabled slug on `app.state` beside the cache:
    that would work perfectly until a restart, and then silently re-enable
    every provider anybody had switched off. Here the row is committed by
    `PostgresRowProviderSettingsRepository` on a session of its own, exactly as
    a second replica or an operator's `psql` would, and no request has ever
    named this slug.

    **The middle assertion is the cost this task restates rather than widens.**
    A write that did not go through this process cannot clear this process's
    `RowCache`, so the shelf survives for up to `_SCREEN_TTL` -- the
    cross-process gap `services/rows/cache.py` records in full, and the same
    bound a push-lane invalidation already has. Asserting it is what stops the
    next reader mistaking the gap for this case being flaky, and what makes the
    final assertion a statement about the *filter* rather than about a cache
    that happened to be empty.

    `cache.clear()` stands in for those 30 s passing: `create_app` builds its
    cache over `datetime.now(UTC)` and a real wall clock cannot be advanced.
    `usher home` reads the same table through the same join
    (`tests/integration/test_cli_pipeline.py`), which is the third process this
    argument is really about.
    """
    assert "continue-watching" in await _slugs(client), "the fixture seeded no shelf to remove"

    async with sessions() as session:
        await PostgresRowProviderSettingsRepository(session).set_enabled(
            "continue-watching", enabled=False
        )
        await session.commit()

    assert "continue-watching" in await _slugs(client), (
        "the ~30 s cached screen is the documented cross-process window; "
        "a write elsewhere cannot clear this process's RowCache"
    )

    app.state.row_cache.clear()

    assert "continue-watching" not in await _slugs(client), (
        "the composer is filtering on process state rather than on the table"
    )
    assert "recently-added" in await _slugs(client)


async def test_the_listing_and_the_toggle_round_trip_through_real_postgres(
    client: AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """`GET` and `PUT` over the real repository, and the row read back on
    another connection.

    What only this level can see is the **upsert**: `ON CONFLICT (slug_prefix)
    DO UPDATE` is what makes a second toggle one row rather than an
    `IntegrityError`, and `FakeRowProviderSettingsRepository` is a dict, so it
    cannot fail either way. Three writes over two slugs must leave two rows.

    The virgin-database arm is asserted against `{p.slug_prefix for p in
    ROW_PROVIDERS}` rather than a literal for the reason the unit case is: an
    eleventh provider must appear on this surface with no edit here.
    """
    listed = await client.get(PROVIDERS)
    assert listed.status_code == 200, listed.text
    assert {one["slug"] for one in listed.json()} == {one.slug_prefix for one in ROW_PROVIDERS}
    assert all(one["enabled"] for one in listed.json()), "a virgin table disabled something"
    assert await _stored_overrides(sessions) == {}, "a read wrote a row"

    await client.put(f"{PROVIDERS}/seasonal", json={"enabled": False})
    await client.put(f"{PROVIDERS}/seasonal", json={"enabled": True})
    await client.put(f"{PROVIDERS}/rediscover", json={"enabled": False})

    assert await _stored_overrides(sessions) == {"seasonal": True, "rediscover": False}
    settled = {one["slug"]: one["enabled"] for one in (await client.get(PROVIDERS)).json()}
    assert settled["seasonal"] is True
    assert settled["rediscover"] is False
    assert len(settled) == len(ROW_PROVIDERS), "the listing shrank to the rows in the table"
