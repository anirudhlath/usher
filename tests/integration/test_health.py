"""Liveness and readiness endpoints against a real Postgres.

The `client` fixture wraps the app in `asgi_lifespan.LifespanManager`.
`httpx.ASGITransport` only implements the ASGI "http" protocol, not
"lifespan" (verified directly against its source) -- FastAPI's own docs
say so too (Advanced -> Async Tests): "HTTPX's AsyncClient will not
trigger [lifespan events] automatically." Without this, `create_app`'s
lifespan -- which builds the engine and sets `app.state.session_factory`
-- never runs, and `/health/ready` would raise `AttributeError` on
`request.app.state.session_factory` instead of exercising the real
database check these tests are for.
"""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.api.app import create_app
from usher.api.routers.health import _check_migrations
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind, JobPriority

SECRET_KEY = "0123456789abcdef0123456789abcdef"

#: What `lanes` reads for a process running none of them, spelled once so both
#: cases below assert the **whole** mapping. Whole, not per key, because the
#: mutation these two exist to kill is a lane field *moving* into
#: `ReadinessChecks` -- where `all(checks.model_dump().values())` picks it up
#: and every falsy value here (`[]`, `False`, `None`) turns this 200 into a
#: 503. A per-key assertion is satisfied by a field that is in both models.
_NO_LANES = {
    "push": [],
    "worker": False,
    "crashed_sources": [],
    # `null`, never `0`: this process runs no worker, so it never called
    # `recover()` and has not measured "no orphans" -- it has not asked.
    "recovered_claims": None,
    "recovered_at": None,
}


@pytest.fixture
def app(postgres_url: str) -> FastAPI:
    settings = Settings(
        database_url=postgres_url,
        secret_key="0123456789abcdef0123456789abcdef",
        # Reported by `/health/ready` either way; off here so this file's
        # subject stays the two checks the status code *is* gated on, and so
        # a worker lane does not claim jobs another file is asserting on.
        # `tests/integration/test_lanes_in_the_server_process.py` is where
        # they are turned on against this same real database.
        push_enabled=False,
        worker_enabled=False,
    )
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_health_is_liveness_only(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_reports_database_connectivity(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] is True
    # postgres_url runs the real migration chain (see conftest.py), so a
    # correctly-migrated database also reports migrations: true here --
    # not just database connectivity.
    assert body["checks"]["migrations"] is True


async def test_a_process_with_no_lanes_running_is_still_ready(client: AsyncClient) -> None:
    """**The correction PRD 08 needs, and the only place it has teeth.**

    A readiness check that failed because no push lane was up would take
    this process out of a load balancer for a reason restarting it cannot
    fix -- the exact argument M1's liveness/readiness split is built on, and
    PRD 08's own failure table says an unreachable source leaves the catalog
    "fully browsable".

    It has to be *here* rather than in `tests/unit/test_api_health.py`,
    because that file's app points at an unreachable database and is
    therefore already 503: `all(checks) and lanes.running_sources()` and
    `push` moved inside `ReadinessChecks` both leave it 503 and survive.
    Against a reachable database with no lanes running, both turn a 200 into
    a 503 and both die.
    """
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["lanes"] == _NO_LANES


async def test_a_third_lane_kind_changes_neither_the_status_code_nor_the_report(
    app: FastAPI, client: AsyncClient
) -> None:
    """**M9 adds a `rows.refresh` lane, and readiness must not notice.**

    The lane is running in this very process -- it is gated on `create_app`
    building a cache and a queue, not on a setting, so it is up even with
    `push_enabled=False, worker_enabled=False`. Two mutations are available
    the moment a third kind exists, and both die here and nowhere else:
    reporting it inside `ReadinessChecks`, where `all(checks.model_dump()
    .values())` picks it up automatically and a screen refresh starts deciding
    whether a load balancer sends traffic; and folding it into
    `running_sources()`, which is what `lanes.push` is and which would then
    name something that is not a source.

    It has to be here rather than in `tests/unit/test_api_health.py` for the
    reason `test_a_process_with_no_lanes_running_is_still_ready` states: that
    file's app points at an unreachable database and is already 503, so both
    mutations survive every case in it.
    """
    assert app.state.lanes.rows_refreshing() is True, (
        "the lane is not running, so this case would pass against anything"
    )

    response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["lanes"] == _NO_LANES


async def test_openapi_schema_is_served(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Usher"


# -- the orphan-recovery report (M10 F2) ---------------------------------

#: Bounded, because "the lane never ran" is otherwise a hang rather than a
#: failure. Generous against `IDLE_SLEEP_SECONDS = 5.0`: the worker lane's
#: first pass is immediate, so a working lane answers in milliseconds and only
#: a broken one waits.
_BOUND_SECONDS = 20.0

#: A claim nobody is working on, planted as a **raw `INSERT`** because that is
#: the only way to own `jobs.updated_at`: every statement in
#: `PostgresJobQueue` stamps it `clock_timestamp()` itself, so a row enqueued
#: and claimed through the port is by construction fresh and can never be
#: older than the lease. Backdated an hour, which is past
#: `USHER_JOB_LEASE_SECONDS`' 300 s default without moving the setting -- the
#: recovery this case is about is the shipped one, not a tuned one.
_PLANT_AN_ORPHAN = """
INSERT INTO jobs (id, kind, key, priority, status, attempts, created_at, updated_at)
VALUES (
    :id, :kind, :key, :priority, 'running', 0,
    clock_timestamp() - interval '1 hour',
    clock_timestamp() - interval '1 hour'
)
"""

#: Read from a session of its own, so what it reports is committed state
#: rather than the planting transaction's own uncommitted view.
_IS_AN_ORPHAN = """
SELECT status, updated_at <= clock_timestamp() - make_interval(secs => :lease) AS stale
FROM jobs WHERE key = :key
"""


@pytest.fixture
def worker_app(postgres_url: str) -> FastAPI:
    """The one app in this file that runs a lane.

    `worker_enabled=True` is the whole difference from `app` above: recovery
    happens inside `_run_worker`, so a process with the switch off never asks
    the question and honestly reports `null`.
    """
    return create_app(
        Settings(
            database_url=postgres_url,
            secret_key=SECRET_KEY,
            push_enabled=False,
            worker_enabled=True,
        )
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Real, separately-committing sessions -- not this suite's usual
    rolled-back one. The lane under test commits from another task, so a case
    that watched it through one shared transaction would see nothing."""
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        await session.execute(text("DELETE FROM jobs"))
        await session.execute(text("DELETE FROM users WHERE name = 'default'"))
        await session.commit()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Either side, because a run that died between the two would otherwise
    leave a `running` row that makes the next run's count wrong."""
    await _wipe(sessions)
    yield
    await _wipe(sessions)


async def test_a_recovered_orphan_is_reported_in_the_body_and_moves_no_status_code(
    worker_app: FastAPI, sessions: async_sessionmaker[AsyncSession], clean: None
) -> None:
    """**M9's S3 condition, survivable since ADR-0037's lease and until now
    unobservable.** A worker died holding claims; another worker took them
    back; an operator watching `/health/ready` saw `worker: true` throughout
    and nothing else. `JobWorker.recover()` has returned the count since W1
    and both callers threw it away.

    The number is the one `recover()` measured, never a fresh query: this
    endpoint is polled every 2 s by the shipped compose healthcheck and makes
    **no upstream request and no extra statement at all**, and a
    `SELECT count(*) ... WHERE status = 'running'` per poll would scan a table
    with no index on that value (`ix_jobs_claim` is partial on `pending`,
    `ix_jobs_parked` on `parked`) -- M4 measured it at 1,126,674 rows.

    **And the status code does not move**, which is the half `LaneReport`
    exists to guarantee: 200 with a non-zero count in the body.

    Its positive control is the assertion **before** the pass, from a second
    session, that the planted row really was `running` and really was older
    than the lease -- a row recovery could not see produces `0`, and `0` is
    also what a broken report produces.
    """
    settings = worker_app.state.settings
    key = f"an-orphan-{new_id()}"
    async with sessions() as session:
        await session.execute(
            text(_PLANT_AN_ORPHAN),
            {
                "id": new_id(),
                "kind": JobKind.MATCH.value,
                "key": key,
                "priority": int(JobPriority.NEW),
            },
        )
        await session.commit()

    async with sessions() as session:
        planted = (
            await session.execute(
                text(_IS_AN_ORPHAN), {"lease": settings.job_lease_seconds, "key": key}
            )
        ).one()
    assert planted.status == "running", "the premise: recovery only ever looks at `running` rows"
    assert planted.stale is True, (
        "the premise: the claim is older than the lease, so recovery can see it -- "
        "a row it cannot see recovers 0, and 0 is what a broken report says too"
    )

    async with LifespanManager(worker_app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            deadline = time.perf_counter() + _BOUND_SECONDS
            while True:
                response = await client.get("/health/ready")
                body = response.json()
                if body["lanes"]["recovered_claims"] or time.perf_counter() >= deadline:
                    break
                await asyncio.sleep(0.05)

    assert response.status_code == 200, body
    assert body["status"] == "ready"
    assert body["lanes"]["worker"] is True
    assert body["lanes"]["recovered_claims"] == 1, (
        "the orphan was never taken back, or the number `recover()` returned was discarded again"
    )
    assert body["lanes"]["recovered_at"] is not None


async def test_a_worker_that_asked_and_found_nothing_reports_zero_not_null_and_not_one(
    worker_app: FastAPI, sessions: async_sessionmaker[AsyncSession], clean: None
) -> None:
    """**The case that makes the number a count of claims rather than of
    passes**, and the third value this field has to be able to take.

    `null` / `0` / non-zero are three different statements -- never asked,
    asked and found none, took some back -- and the two cases beside this one
    pin the first and the third. Without this one, a counter incremented
    *before* `recover()` rather than from its **return value** reports `1` for
    a pass that recovered nothing and `1` for a pass that recovered one, and
    the case above cannot tell them apart because both answers are `1`.

    `recovered_at` staying `null` is the other half: it is the instant of the
    last pass that **found** something, not of the last pass.
    """
    async with sessions() as session:
        empty = (await session.execute(text("SELECT count(*) FROM jobs"))).scalar_one()
    assert empty == 0, "the premise: there is nothing here for recovery to take back"

    async with LifespanManager(worker_app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            deadline = time.perf_counter() + _BOUND_SECONDS
            while True:
                response = await client.get("/health/ready")
                body = response.json()
                # Until the first pass returns, `null` is the honest answer
                # here too -- the lane exists but has not asked yet.
                if body["lanes"]["recovered_claims"] is not None:
                    break
                if time.perf_counter() >= deadline:
                    break
                await asyncio.sleep(0.05)

    assert response.status_code == 200, body
    assert body["lanes"]["worker"] is True
    assert body["lanes"]["recovered_claims"] == 0
    assert body["lanes"]["recovered_at"] is None


async def test_check_migrations_detects_a_mismatch(session: AsyncSession) -> None:
    """Uses the transaction-isolated `session` fixture directly, not the
    `app`/`client` fixtures above: corrupting `alembic_version` needs to
    be visible to the very next query on the *same* connection (ordinary
    read-your-own-writes within one open transaction), not committed and
    visible cross-connection to the app's own separately-built engine --
    and the rollback this fixture does afterward means the shared,
    session-scoped `postgres_url` database is left exactly as every other
    test in this session expects to find it.
    """
    await session.execute(text("UPDATE alembic_version SET version_num = 'deadbeefcafe'"))
    assert await _check_migrations(session) is False
