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

from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from usher.api.app import create_app
from usher.config import Settings
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
