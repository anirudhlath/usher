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

from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.api.app import create_app
from usher.api.routers.health import _check_migrations
from usher.config import Settings


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
    assert body["lanes"] == {"push": [], "worker": False}


async def test_openapi_schema_is_served(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Usher"


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
