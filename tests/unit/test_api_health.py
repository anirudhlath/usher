"""The degraded-readiness path -- deliberately not in tests/integration/:
it needs no real Postgres (a connection refused on a port nothing listens
on fails the same way an actually-down database would, from the app's
perspective), so it belongs where the rest of this suite's Docker-free
tests live rather than paying for a container it doesn't need.

Neither the plan nor the originally-shipped tests asserted this path at
all -- the happy-path test in tests/integration/test_health.py only
proves readiness works when Postgres is reachable, which is exactly why
a 200-with-degraded-body response (rather than the 503 below) went
undebated for as long as it did.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from asgi_lifespan import LifespanManager
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from usher.api.app import create_app
from usher.api.deps import (
    get_lane_supervisor,
    get_session,
    get_source_adapter_factory,
    get_source_service,
)
from usher.api.lanes import LaneSupervisor
from usher.api.routers.health import router as health_router
from usher.composition import Pipeline
from usher.config import Settings
from usher.ports.events import NullEventPublisher
from usher.services.events import InMemoryEventBus


@pytest.fixture
async def client_against_unreachable_database() -> AsyncIterator[AsyncClient]:
    settings = Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key="0123456789abcdef0123456789abcdef",
    )
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_ready_returns_503_when_database_unreachable(
    client_against_unreachable_database: AsyncClient,
) -> None:
    response = await client_against_unreachable_database.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] is False


async def test_health_stays_ok_even_when_database_unreachable(
    client_against_unreachable_database: AsyncClient,
) -> None:
    """The liveness/readiness split's entire point: a database outage must
    not affect liveness, so this and the 503 test above use the same
    unreachable-database app to prove the difference directly rather than
    asserting it in isolation."""
    response = await client_against_unreachable_database.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_create_app_builds_the_client_event_bus() -> None:
    """`get_reconcile_service` resolves `EventPublisher` off `app.state.events`
    on every request that walks a source, so an app without one 500s at
    request time rather than at start-up. Built in `create_app` rather than in
    the lifespan for the reason `settings` is: it holds no connection, no
    thread, and nothing to dispose.
    """
    settings = Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key="0123456789abcdef0123456789abcdef",
        sse_buffer_size=8,
        sse_queue_size=4,
    )
    app = create_app(settings)
    bus = app.state.events
    assert isinstance(bus, InMemoryEventBus)
    assert bus.subscribers == 0
    # The two settings are read rather than defaulted -- a knob that
    # validates and then influences nothing is the thing config.py's own
    # comment forbids.
    assert (bus._buffer.maxlen, bus._queue_size) == (8, 4)


class _Lanes(LaneSupervisor):
    """A supervisor that reports rather than runs.

    A subclass rather than a duck type, so the `Depends` override really is
    a `LaneSupervisor` and a signature change on the real one breaks this
    file rather than being tolerated by a mock.
    """

    def __init__(
        self,
        *,
        push: list[str] | None = None,
        worker: bool = False,
        crashed: list[str] | None = None,
        recovered: int | None = None,
        recovered_when: datetime | None = None,
        available: dict[uuid.UUID, bool | None] | None = None,
    ) -> None:
        super().__init__(
            _settings(),
            _no_work,
            NullEventPublisher(),
            user_id=_no_user,
        )
        self._reported = push or []
        self._worker_reported = worker
        self._crashed = crashed or []
        self._recovered = recovered
        self._recovered_when = recovered_when
        self._available = available or {}

    def running_sources(self) -> list[str]:
        return self._reported

    def worker_running(self) -> bool:
        return self._worker_reported

    def crashed_sources(self) -> list[str]:
        return self._crashed

    def recovered_claims(self) -> int | None:
        return self._recovered

    def recovered_at(self) -> datetime | None:
        return self._recovered_when

    def push_available(self, source_id: uuid.UUID) -> bool | None:
        return self._available.get(source_id)


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key="0123456789abcdef0123456789abcdef",
        push_enabled=False,
        worker_enabled=False,
    )


@asynccontextmanager
async def _no_work() -> AsyncIterator[Pipeline]:
    raise AssertionError("a readiness check must not open a unit of work")
    yield  # pragma: no cover  -- unreachable; makes this a generator


async def _no_user() -> uuid.UUID:
    raise AssertionError("a readiness check must not resolve the default user")


@asynccontextmanager
async def _client_with_lanes(lanes: LaneSupervisor) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key="0123456789abcdef0123456789abcdef",
        push_enabled=False,
        worker_enabled=False,
    )
    app = create_app(settings)
    app.dependency_overrides[get_lane_supervisor] = lambda: lanes
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_readiness_reports_the_lanes() -> None:
    stamp = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    lanes = _Lanes(
        push=["Living Room Emby"],
        worker=True,
        crashed=["Attic Emby"],
        recovered=20,
        recovered_when=stamp,
    )
    async with _client_with_lanes(lanes) as client:
        body = (await client.get("/health/ready")).json()
    assert body["lanes"] == {
        "push": ["Living Room Emby"],
        "worker": True,
        "crashed_sources": ["Attic Emby"],
        "recovered_claims": 20,
        # The literal wire spelling, not `stamp.isoformat()`: pydantic renders
        # a UTC datetime with a `Z` and `isoformat()` renders `+00:00`, so the
        # derived form asserts what Python does rather than what a client
        # receives.
        "recovered_at": "2026-08-19T12:00:00Z",
    }


async def test_a_process_that_runs_no_worker_reports_no_orphan_count_rather_than_zero() -> None:
    """**`null`, not `0`, and the difference is a claim.**

    `USHER_WORKER_ENABLED=false` beside a `usher work` container is the split
    topology PRD 08 prices, and this process never calls `recover()` at all --
    so `0` would assert *"no orphans"* about a question it never asked, on the
    one endpoint an operator reads to find out. `SourceStatus.push_available`
    is the precedent: `None` means **not probed**.

    Driven against a **real** `LaneSupervisor` rather than the `_Lanes` stub
    above, because a stub returning `None` because it was told to says nothing
    about what the shipped supervisor reports. Its unit of work and its user
    reader both raise, so a readiness check that went looking fails loudly.
    """
    lanes = LaneSupervisor(_settings(), _no_work, NullEventPublisher(), user_id=_no_user)
    assert lanes.worker_running() is False, "the premise: this process runs no worker lane"

    async with _client_with_lanes(lanes) as client:
        body = (await client.get("/health/ready")).json()

    assert body["lanes"]["recovered_claims"] is None
    assert body["lanes"]["recovered_at"] is None

    # **The control, and it is the assertion with teeth.** `is None` is
    # satisfied by a field that can only ever be `null` -- a `bool` reported
    # as `None`, a serialiser dropping a zero. The same route, one stub over,
    # has to answer `0` for a process that asked and found nothing, because
    # "asked and found none" and "never asked" are the two answers this field
    # exists to distinguish.
    async with _client_with_lanes(_Lanes(worker=True, recovered=0)) as client:
        asked = (await client.get("/health/ready")).json()
    assert asked["lanes"]["recovered_claims"] == 0


async def test_a_source_whose_push_is_down_does_not_make_this_process_unready() -> None:
    """**The correction PRD 08 needs.** A readiness check that failed
    because Emby is down would take Usher out of a load balancer for a
    reason restarting Usher cannot fix -- which is the exact argument M1's
    liveness/readiness split is built on, and PRD 08's own failure table
    says an unreachable source leaves the catalog "fully browsable".

    Driven against a *reachable* database so the only thing that could
    degrade it is the lane report. The database this app points at is not
    reachable, so the assertion is inverted: readiness is 503 for the
    database and its `status` still moves with `checks` alone -- see
    `test_no_lane_state_can_change_the_readiness_verdict` below, which is
    the half that has teeth.
    """
    async with _client_with_lanes(_Lanes(push=[], worker=True)) as client:
        body = (await client.get("/health/ready")).json()
    assert body["lanes"]["push"] == []
    assert body["lanes"]["worker"] is True


@pytest.mark.parametrize(
    ("push", "worker", "crashed", "recovered", "recovered_when"),
    [
        ([], False, [], None, None),
        ([], True, [], 0, None),
        (["A"], False, ["B"], 20, datetime(2026, 8, 19, 12, 0, tzinfo=UTC)),
        (["A", "B"], True, [], None, datetime(2026, 8, 19, 12, 0, tzinfo=UTC)),
        ([], True, ["A", "B"], 1, datetime(2026, 8, 19, 12, 0, tzinfo=UTC)),
    ],
)
async def test_no_lane_state_can_change_the_readiness_verdict(
    push: list[str],
    worker: bool,
    crashed: list[str],
    recovered: int | None,
    recovered_when: datetime | None,
) -> None:
    """Every combination of lane state, one verdict.

    This is the case the mutations in the plan's table land on: putting any
    one of the **five** lane fields inside `ReadinessChecks` makes
    `all(checks.model_dump().values())` pick it up automatically, and
    `... and lanes.running_sources()` does it by hand. The database is
    unreachable throughout, so `checks` is constant and the lanes are the only
    thing varying.

    **The `checks` assertion is what has teeth here, not the status code**,
    and saying so is the point: this app is already 503, so a folded field
    cannot move the verdict in *this* file at all -- what it does move is the
    contents of `checks`, which the exact-equality below refuses to grow.
    `tests/integration/test_health.py::
    test_a_process_with_no_lanes_running_is_still_ready` is the other half,
    where a reachable database means a folded `crashed_sources: []`,
    `recovered_claims: null` or `recovered_at: null` is a **falsy** member of
    `all(...)` and turns a 200 into a 503.

    M10's F2 added the last three parameters. Each varies over both a falsy
    and a truthy value, so a fold is caught wherever it happens to land.
    """
    lanes = _Lanes(
        push=push,
        worker=worker,
        crashed=crashed,
        recovered=recovered,
        recovered_when=recovered_when,
    )
    async with _client_with_lanes(lanes) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"] == {"database": False, "migrations": False}
    # And every one of them really is in the body, so the equality above is
    # refusing a *move* rather than passing because the field does not exist.
    assert set(body["lanes"]) == {
        "push",
        "worker",
        "crashed_sources",
        "recovered_claims",
        "recovered_at",
    }


async def test_readiness_never_touches_a_source() -> None:
    """Docker's healthcheck polls this every 2 s in the shipped compose
    file, against an upstream PRD 01 measures at 1-5 s per request. A probe
    here is a request per poll per source, forever -- and it would take the
    process out of a load balancer for a reason restarting it cannot fix.

    Asserted on the route's own dependency graph rather than on "no adapter
    was built": a probe added to `ready` would have to reach a
    `SourceAdapterFactory` or a `SourceService`, and a recording factory
    proves nothing when the override is never resolved in the first place.
    The `_Lanes` stub above is the second half -- its unit of work and its
    user reader both raise, so a readiness check that opened a session or
    wrote the default user row fails loudly rather than slowly.
    """
    ready = next(
        route
        for route in health_router.routes
        if isinstance(route, APIRoute) and route.path == "/health/ready"
    )
    resolved = _flatten(ready.dependant)
    assert get_source_adapter_factory not in resolved
    assert get_source_service not in resolved
    # And the graph is not empty, so this is not passing because the walk
    # found nothing: readiness really does resolve a session.
    assert get_session in resolved


def _flatten(dependant: Dependant) -> set[object]:
    """Every callable in a route's dependency tree.

    FastAPI 0.121 has no public `get_flat_dependant`, so this walks
    `Dependant.dependencies` itself -- three lines, and pinned by the
    positive assertion above rather than trusted."""
    found: set[object] = {dependant.call}
    for sub in dependant.dependencies:
        found |= _flatten(sub)
    return found
