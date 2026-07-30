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

from usher.api.app import create_app
from usher.config import Settings


@pytest.fixture
def app(postgres_url: str) -> FastAPI:
    settings = Settings(
        database_url=postgres_url,
        secret_key="0123456789abcdef0123456789abcdef",  # noqa: S106 -- throwaway test value, not a real credential
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


async def test_openapi_schema_is_served(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Usher"
