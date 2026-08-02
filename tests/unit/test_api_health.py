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
        available: dict[uuid.UUID, bool | None] | None = None,
    ) -> None:
        super().__init__(
            Settings(
                database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
                secret_key="0123456789abcdef0123456789abcdef",
                push_enabled=False,
                worker_enabled=False,
            ),
            _no_work,
            NullEventPublisher(),
            user_id=_no_user,
        )
        self._reported = push or []
        self._worker_reported = worker
        self._available = available or {}

    def running_sources(self) -> list[str]:
        return self._reported

    def worker_running(self) -> bool:
        return self._worker_reported

    def push_available(self, source_id: uuid.UUID) -> bool | None:
        return self._available.get(source_id)


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
    async with _client_with_lanes(_Lanes(push=["Living Room Emby"], worker=True)) as client:
        body = (await client.get("/health/ready")).json()
    assert body["lanes"] == {"push": ["Living Room Emby"], "worker": True}


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
    ("push", "worker"),
    [([], False), ([], True), (["A"], False), (["A", "B"], True)],
)
async def test_no_lane_state_can_change_the_readiness_verdict(
    push: list[str], worker: bool
) -> None:
    """Every combination of lane state, one verdict.

    This is the case the two mutations in the plan's table land on: putting
    `push` inside `ReadinessChecks` makes `all(checks.model_dump().values())`
    pick it up automatically, and `... and lanes.running_sources()` does it
    by hand. Both change the answer for at least one row below; the
    database is unreachable throughout, so `checks` is constant and the
    lanes are the only thing varying.
    """
    async with _client_with_lanes(_Lanes(push=push, worker=worker)) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"] == {"database": False, "migrations": False}


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
